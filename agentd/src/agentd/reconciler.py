"""§11.2 reconciler: M6-1a local inventory + M6-1b GitHub sweep (ADR-20/21)."""

from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from agentd.db import Store
from agentd.digest import json_obj
from agentd.fsm import transition
from agentd.gitops import (
    is_design_head_ref,
    is_feature_head_ref,
    parse_role_branch,
    role_branch_name,
)
from agentd.rpc_client import ProbeResult
from agentd.rpc_client import probe_runner as _probe_runner_default
from agentd.session_loop import (
    CLOSE_RECONCILE_PREFIX,
    _close_reconcile_held,
    _inflight_turn_ids,
)
from agentd.verification import checkbox_is_checked

log = logging.getLogger("agentd.reconciler")

CONTAINER_AGE_FLOOR_S = 10 * 60
INFLIGHT_TURN_MAX_AGE_S = 900  # matches gateway turn_deadline_s default
RECONCILE_INTERVAL_S = 5 * 60
SYNTHESIS_CAP = 50
RESUME_MAX_AGE_S = 3600
# Matched pair with process_resuming_turns (session_loop.py): states that
# dispatch no turns. Reconciler retires open turns here; drain refuses
# resume and defers back. Keep both lists in lockstep (ADR-27).
NON_RUNNING_STATES = frozenset({"PAUSED_HUMAN", "TEARDOWN", "CLOSED"})
RESUME_MAX_ATTEMPTS = 2

ListContainers = Callable[[], list[dict[str, Any]]]
ProbeFn = Callable[[dict[str, Any]], ProbeResult]
RemoveContainer = Callable[[str], None]
NudgeFn = Callable[[], None]
FetchSnapshot = Callable[[dict[str, Any]], dict[str, Any] | None]
FetchOpenPrs = Callable[[str, str], list[dict[str, Any]]]
EscalateFn = Callable[..., None]
ResumeTurnFn = Callable[[dict[str, Any]], None]
NotifyMissedFn = Callable[[str, list[dict[str, Any]]], None]


def pragma_integrity_check(db_path: Path) -> str:
    """Read PRAGMA without Store() — Store raises on a bad file."""
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        return str(row[0]) if row else "failed"
    except sqlite3.Error as exc:
        return f"error: {exc}"
    finally:
        conn.close()


def _parse_started_at(raw: str) -> int:
    s = (raw or "").strip()
    if not s:
        return 0
    s = s.replace("Z", "+00:00")
    if "." in s:
        head, tail = s.split(".", 1)
        sign = "+" if "+" in tail else ("-" if "-" in tail[1:] else "")
        if sign:
            frac, _, tz = tail.partition(sign)
            tz = sign + tz
        else:
            frac, tz = tail, "+00:00"
        s = f"{head}.{(frac + '000000')[:6]}{tz}"
    try:
        return int(datetime.fromisoformat(s).timestamp())
    except ValueError:
        return 0


def _cid_match(a: str, b: str) -> bool:
    if not a or not b:
        return False
    return a == b or a.startswith(b) or b.startswith(a)


def list_managed_containers() -> list[dict[str, Any]]:
    r = subprocess.run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            "label=agentd.managed=true",
            "-q",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    ids = [x.strip() for x in (r.stdout or "").split() if x.strip()]
    if not ids or r.returncode != 0:
        if r.returncode != 0:
            raise RuntimeError(f"docker ps failed: {(r.stderr or '')[:200]}")
        return []
    r2 = subprocess.run(
        ["docker", "inspect", *ids],
        check=False,
        capture_output=True,
        text=True,
    )
    if r2.returncode != 0:
        raise RuntimeError(f"docker inspect failed: {(r2.stderr or '')[:200]}")
    data = json.loads(r2.stdout or "[]")
    out: list[dict[str, Any]] = []
    for obj in data:
        labels = (obj.get("Config") or {}).get("Labels") or {}
        state = obj.get("State") or {}
        out.append(
            {
                "id": str(obj.get("Id") or ""),
                "project": str(labels.get("agentd.project") or ""),
                "started_at": _parse_started_at(str(state.get("StartedAt") or "")),
                # ADR-34: not the trigger — the probe is. It buys the `stopped`
                # reason without a socket, and `--restart unless-stopped` means
                # a rebooted container is running, so this is never a proxy for
                # serviceable.
                "running": bool(state.get("Running")),
            }
        )
    return out


def _docker_rm(container_id: str) -> None:
    subprocess.run(
        ["docker", "rm", "-f", container_id],
        check=False,
        capture_output=True,
        text=True,
    )


class Reconciler:
    """Inventory managed containers; nudge the dispatcher. No drain."""

    def __init__(
        self,
        store: Store,
        *,
        list_containers: ListContainers | None = None,
        remove_container: RemoveContainer | None = None,
        probe_runner: ProbeFn | None = None,
        nudge: NudgeFn | None = None,
        fetch_snapshot: FetchSnapshot | None = None,
        fetch_open_prs: FetchOpenPrs | None = None,
        escalate: EscalateFn | None = None,
        resume_turn: ResumeTurnFn | None = None,
        notify_missed: NotifyMissedFn | None = None,
        resume_max_age_s: int = RESUME_MAX_AGE_S,
        interval_s: float = RECONCILE_INTERVAL_S,
        now_fn: Callable[[], int] | None = None,
    ) -> None:
        self.store = store
        self.list_containers = list_containers or list_managed_containers
        self.remove_container = remove_container or _docker_rm
        self.probe_runner = probe_runner or _probe_runner_default
        self.nudge = nudge
        self.fetch_snapshot = fetch_snapshot
        self.fetch_open_prs = fetch_open_prs
        self.escalate = escalate
        self.resume_turn = resume_turn
        self.notify_missed = notify_missed
        self.resume_max_age_s = int(resume_max_age_s)
        self.interval_s = interval_s
        self._now = now_fn or (lambda: int(time.time()))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="agentd-reconciler", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.interval_s + 1)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.reconcile_once()
            except Exception:
                log.exception("reconcile_once failed")
            self._stop.wait(self.interval_s)

    def _log_pass(self, report: dict[str, Any], *, containers: int, kept: int, started: float) -> None:
        ms = int((time.monotonic() - started) * 1000)
        log.info(
            "reconcile pass containers=%d kept=%d spared=%d removed=%d cleared=%d "
            "open_turns=%d closed_live=%d nodes=%d synthesized=%d capped=%d "
            "adopted=%d escalated=%d retired=%d resumed=%d "
            "attached=%d unattached=%d probe_skipped=%d in %dms",
            containers,
            kept,
            len(report["spared"]),
            len(report["removed"]),
            len(report["cleared_rows"]),
            len(report["open_turns"]),
            len(report["closed_live"]),
            int(report.get("nodes_seen") or 0),
            int(report.get("synthesized") or 0),
            int(report.get("capped") or 0),
            int(report.get("adopted") or 0),
            int(report.get("escalated") or 0),
            int(report.get("retired") or 0),
            int(report.get("resumed") or 0),
            len(report.get("attached") or []),
            len(report.get("unattached") or []),
            len(report.get("probe_skipped") or []),
            ms,
        )

    def reconcile_once(self, *, dry_run: bool = False) -> dict[str, Any]:
        t0 = time.monotonic()
        report: dict[str, Any] = {
            "orphans": [],
            "spared": [],
            "removed": [],
            "cleared_rows": [],
            "missing_runners": [],
            "stale_runners": [],
            "open_turns": self.store.list_open_turns(),
            "closed_live": self.store.list_live_sessions_with_closed_delivery(),
            "nodes_seen": 0,
            "synthesized": 0,
            "capped": 0,
            "adopted": 0,
            "escalated": 0,
            "holds_lifted": 0,
            "retired": 0,
            "resumed": 0,
            "attached": [],
            "unattached": [],
            "probe_skipped": [],
        }
        for row in report["closed_live"]:
            sk = str(row.get("session_key") or "")
            if sk:
                row["open_artifacts"] = self.store.count_open_artifacts(sk)

        try:
            containers = self.list_containers()
        except Exception:
            log.exception("container inventory failed — no removals this pass")
            report["inventory_error"] = True
            self._sweep_github(report, dry_run=dry_run)
            self._handle_open_turns(report, dry_run=dry_run)
            if not dry_run and self.nudge:
                self.nudge()
            self._log_pass(report, containers=0, kept=0, started=t0)
            return report

        now = int(self._now())
        live = self.store.live_project_keys()
        inflight = self.store.projects_with_open_turns(
            now=now, max_age_s=INFLIGHT_TURN_MAX_AGE_S
        )
        runners = {str(r["project_key"]): r for r in self.store.list_runners()}
        seen_ids = [str(c.get("id") or "") for c in containers]
        probe_targets: dict[str, dict[str, Any]] = {}
        n_kept = 0

        for c in containers:
            cid = str(c.get("id") or "")
            pk = str(c.get("project") or "")
            started = int(c.get("started_at") or 0)
            age = now - started
            decision = self._decide(
                project=pk,
                container_id=cid,
                age_s=age,
                started_at=started,
                live=pk in live if pk else False,
                inflight=pk in inflight if pk else False,
                runner=runners.get(pk) if pk else None,
            )
            entry = {
                "id": cid,
                "project": pk,
                "age_s": age,
                "action": decision["action"],
                "reason": decision["reason"],
            }
            if decision["action"] == "keep":
                n_kept += 1
                # One probe per *project* per pass (ADR-34 acceptance (10)).
                # `_decide` only returns `keep` for the container the runners
                # row names, so today this dict never collides — dedupe here
                # anyway rather than depend on that staying true.
                if pk:
                    probe_targets.setdefault(pk, c)
            elif decision["action"] == "spare":
                report["spared"].append(entry)
            elif decision["action"] == "remove":
                report["orphans"].append(entry)
                if not dry_run:
                    self.remove_container(cid)
                    report["removed"].append(entry)
                    log.warning(
                        "reconcile removed container=%s project=%s reason=%s",
                        cid,
                        pk,
                        decision["reason"],
                    )
            elif decision["action"] == "missing_runner":
                report["missing_runners"].append(entry)
                if not dry_run:
                    self.remove_container(cid)
                    report["removed"].append(entry)
                    log.warning(
                        "reconcile removed tokenless container=%s project=%s",
                        cid,
                        pk,
                    )

        for pk, row in runners.items():
            rid = str(row.get("container_id") or "")
            if rid and not any(_cid_match(rid, sid) for sid in seen_ids if sid):
                stale = {"project": pk, "container_id": rid}
                report["stale_runners"].append(stale)
                if not dry_run:
                    self.store.delete_runner(pk)
                    report["cleared_rows"].append(stale)
                    log.info("reconcile cleared stale runner project=%s", pk)

        self._probe_attachments(
            report,
            probe_targets,
            runners=runners,
            inflight=inflight,
            now=now,
            dry_run=dry_run,
        )
        self._sweep_github(report, dry_run=dry_run)
        self._handle_open_turns(report, dry_run=dry_run)
        if not dry_run and self.nudge:
            self.nudge()
        self._log_pass(report, containers=len(containers), kept=n_kept, started=t0)
        return report

    def _probe_attachments(
        self,
        report: dict[str, Any],
        targets: dict[str, dict[str, Any]],
        *,
        runners: dict[str, dict[str, Any]],
        inflight: Any,
        now: int,
        dry_run: bool,
    ) -> None:
        """§11.2 step 7: attach and record. Never repair, never remove (ADR-34).

        The probe writes one column and one log line. It does not touch `tier`
        — recording COLD for a container that is still running would free a
        §6.6 admission slot whose RAM is still resident — and removal never
        learns about it, because an unreachable runner is the ordinary state of
        a container that was stopped on purpose and is repairable on demand by
        the next `ensure_session` (ADR-25).
        """
        for pk, c in targets.items():
            cid = str(c.get("id") or "")
            if pk in inflight:
                # The turn *is* the attachment evidence, and a runner busy
                # inside one can miss a 2 s timeout and produce a WARNING that
                # lies. A false negative is worse than no sample here.
                report["probe_skipped"].append(
                    {"project": pk, "id": cid, "reason": "open turn"}
                )
                continue
            row = runners.get(pk)
            if row is None:
                continue
            if not bool(c.get("running")):
                # `stopped` is reached without a socket: connecting to a stopped
                # container is a certain failure, so paying for it is noise, and
                # the reason must not arrive by relabelling a connect error.
                self._record_unattached(report, pk, cid, "stopped")
                continue
            try:
                res = self.probe_runner(row)
            except Exception:
                # One project's probe must not abort the pass. Unreachable with
                # the default `probe_runner`, which converts every transport and
                # protocol failure into a ProbeResult and raises nothing — this
                # guards an *injected* seam from killing the reconcile thread.
                # `probe_error` is in ADR-34's reason list for that reason.
                log.exception("probe raised project=%s container=%s", pk, cid)
                self._record_unattached(report, pk, cid, "probe_error")
                continue
            if not res.serviceable:
                self._record_unattached(report, pk, cid, res.reason)
                continue
            payload = res.payload or {}
            report["attached"].append(
                {
                    "project": pk,
                    "id": cid,
                    "reason": res.reason,
                    "rss_bytes": payload.get("rss_bytes"),
                    "cli_rss_kb": payload.get("cli_rss_kb"),
                    # #212: rss_bytes is now the live VmRSS and cli_rss_kb is
                    # sampled at probe time. The peak is kept but named, and
                    # sampled_at lets a consumer reject a stale reading rather
                    # than assume freshness.
                    "rss_peak_bytes": payload.get("rss_peak_bytes"),
                    "sampled_at": payload.get("sampled_at"),
                }
            )
            # A dry run probes — that is how `agentctl reconcile --dry-run`
            # shows attachment state — but stamps nothing.
            if not dry_run:
                self.store.touch_runner_seen(pk, now=now)

    def _record_unattached(
        self, report: dict[str, Any], project: str, container_id: str, reason: str
    ) -> None:
        report["unattached"].append(
            {"project": project, "id": container_id, "reason": reason}
        )
        log.warning(
            "reconcile runner unattached project=%s container=%s reason=%s "
            "(no repair here — next ensure_session promotes; ADR-25/ADR-34)",
            project,
            container_id,
            reason,
        )

    def _handle_open_turns(self, report: dict[str, Any], *, dry_run: bool) -> None:
        now = int(self._now())
        for turn in self.store.list_open_turns():
            tid = str(turn.get("turn_id") or "")
            if not tid:
                continue
            try:
                self._apply_open_turn(turn, report, now=now, dry_run=dry_run)
            except Exception:
                log.exception("open-turn handle failed turn=%s", tid)
        report["open_turns"] = self.store.list_open_turns()

    def _apply_open_turn(
        self,
        turn: dict[str, Any],
        report: dict[str, Any],
        *,
        now: int,
        dry_run: bool,
    ) -> None:
        tid = str(turn["turn_id"])
        if tid in _inflight_turn_ids:
            return
        str(turn.get("session_key") or "")
        state = str(turn.get("session_state") or "")
        started = int(turn.get("started_at") or 0)
        age = now - started if started > 0 else self.resume_max_age_s + 1
        attempts = int(turn.get("resume_attempts") or 0)
        not_running = state in NON_RUNNING_STATES or not state
        too_old = age >= self.resume_max_age_s
        if not_running or too_old:
            reason = (
                f"session {state or 'unknown'} is not running"
                if not_running
                else f"age {age}s >= resume_max_age_s {self.resume_max_age_s}"
            )
            report["retired"] = int(report["retired"]) + 1
            if dry_run:
                return
            self._retire_turn(turn, reason, escalate=False)
            return

        if str(turn.get("status") or "") == "resuming":
            return
        if attempts >= RESUME_MAX_ATTEMPTS:
            report["retired"] = int(report["retired"]) + 1
            if dry_run:
                return
            self._retire_turn(
                turn, "resume failed twice", escalate=True
            )
            return

        if dry_run:
            report["resumed"] = int(report["resumed"]) + 1
            return
        self.store.mark_turn_resuming(tid)
        report["resumed"] = int(report["resumed"]) + 1

    def _retire_turn(
        self, turn: dict[str, Any], reason: str, *, escalate: bool
    ) -> None:
        tid = str(turn["turn_id"])
        sk = str(turn.get("session_key") or "")
        self.store.finish_turn(
            tid,
            ended_at=int(self._now()),
            status="interrupted",
            summary=reason[:500],
        )
        log.warning("reconcile retired turn=%s session=%s reason=%s", tid, sk, reason)
        notice = {
            "turn_id": tid,
            "session_key": sk,
            "role": turn.get("role"),
            "reason": reason,
        }
        if self.notify_missed and sk:
            try:
                self.notify_missed(sk, [notice])
            except Exception:
                log.exception("notify_missed failed session=%s", sk)
        if escalate and self.escalate and sk:
            self.escalate(
                sk,
                f"interrupted turn {tid} retired after failed resume: {reason}",
                hold=False,
            )

    def _sweep_github(self, report: dict[str, Any], *, dry_run: bool) -> None:
        if self.fetch_snapshot is None:
            return
        try:
            sessions = self.store.list_nonterminal_sessions()
        except Exception:
            log.exception("list_nonterminal_sessions failed")
            return
        remaining = SYNTHESIS_CAP
        for sess in sessions:
            try:
                snap = self.fetch_snapshot(sess)
                if not snap:
                    continue
                remaining = self._apply_snapshot(
                    sess, snap, report, dry_run=dry_run, remaining=remaining
                )
            except Exception:
                log.exception(
                    "sweep session failed session=%s", sess.get("session_key")
                )
                continue
        if int(report.get("capped") or 0):
            log.warning(
                "reconcile synthesis cap=%s hit leftover=%s",
                SYNTHESIS_CAP,
                report["capped"],
            )

    def _apply_snapshot(
        self,
        sess: dict[str, Any],
        snap: dict[str, Any],
        report: dict[str, Any],
        *,
        dry_run: bool,
        remaining: int,
    ) -> int:
        sk = str(sess.get("session_key") or "")
        issue_state = str(snap.get("issue_state") or "")
        held = _close_reconcile_held(str(sess.get("state") or ""), sess)

        if issue_state == "open":
            if sess.get("closed_issue_escalated_at") and not dry_run:
                self.store.update_session_fields(
                    sk, closed_issue_escalated_at=None
                )
                sess["closed_issue_escalated_at"] = None
            if held:
                report["holds_lifted"] = int(report["holds_lifted"]) + 1
                if not dry_run:
                    paused = str(sess.get("paused_reason") or "")
                    stripped = paused.removeprefix(CLOSE_RECONCILE_PREFIX) or None
                    self.store.update_session_fields(sk, paused_reason=stripped)
                    sess["paused_reason"] = stripped
                    log.info("close-reconcile lift session=%s (issue open)", sk)

        if issue_state == "closed" and not sess.get("closed_issue_escalated_at"):
            report["escalated"] = int(report["escalated"]) + 1
            if not dry_run:
                noted = int(self._now())
                self.store.update_session_fields(
                    sk, closed_issue_escalated_at=noted
                )
                sess["closed_issue_escalated_at"] = noted
                if self.escalate:
                    reason = (
                        f"GitHub issue is closed; session is still {sess.get('state')} "
                        f"(reconcile sweep). Nothing classified; session left live."
                    )
                    self.escalate(sk, reason, hold=True)
            # still run set-diff for comments; never synthesize the close

        if snap.get("feature_merged"):
            self._adopt_forward(sess, "feature_merged", report, dry_run=dry_run)
        if snap.get("design_merged"):
            self._adopt_forward(sess, "design_merged", report, dry_run=dry_run)

        remaining = self._synth_head_drift(
            sess, snap, report, dry_run=dry_run, remaining=remaining
        )
        remaining = self._synth_untracked_opened(
            sess, report, dry_run=dry_run, remaining=remaining
        )

        body = snap.get("issue_body")
        if isinstance(body, str) and body:
            box = checkbox_is_checked(body, strict=True)
            stamped = bool(sess.get("verified_at"))
            if (box is True) != stamped:
                log.info(
                    "body drift session=%s verified_at=%s box=%s",
                    sk,
                    sess.get("verified_at"),
                    box,
                )

        nodes = list(snap.get("nodes") or [])
        report["nodes_seen"] = int(report["nodes_seen"]) + len(nodes)
        created_floor = int(sess.get("created_at") or 0)
        for node in nodes:
            nid = str(node.get("id") or "")
            kind = str(node.get("kind") or "")
            if not nid or kind in {"issue", "issues.edited", "issues.closed"}:
                continue
            created = node.get("created_at")
            if created is not None and int(created) < created_floor:
                continue
            if self.store.has_delivery_node(nid):
                continue
            if remaining <= 0:
                report["capped"] = int(report["capped"]) + 1
                continue
            report["synthesized"] = int(report["synthesized"]) + 1
            remaining -= 1
            if dry_run:
                continue
            self._synthesize_node(sess, snap, node)

        return remaining

    def _adopt_forward(
        self,
        sess: dict[str, Any],
        event_kind: str,
        report: dict[str, Any],
        *,
        dry_run: bool,
    ) -> None:
        sk = str(sess.get("session_key") or "")
        state = str(sess.get("state") or "")
        step = transition(state, event_kind)
        if step is None:
            return
        report["adopted"] = int(report["adopted"]) + 1
        if not dry_run:
            self.store.update_session_fields(sk, state=step.new_state)
            sess["state"] = step.new_state
            log.info(
                "reconcile adopted session=%s %s → %s via %s",
                sk,
                state,
                step.new_state,
                event_kind,
            )

    def _synth_head_drift(
        self,
        sess: dict[str, Any],
        snap: dict[str, Any],
        report: dict[str, Any],
        *,
        dry_run: bool,
        remaining: int,
    ) -> int:
        """ADR-37: enqueue pull_request.synchronize when a tracked PR head moved."""
        state = str(sess.get("state") or "")
        sk = str(sess.get("session_key") or "")
        for pr_key, sha_key, merged_key, wm_key, rework in (
            (
                "design_pr",
                "design_head_sha",
                "design_merged",
                "design_pr_head",
                "DESIGN_REWORK",
            ),
            (
                "feature_pr",
                "feature_head_sha",
                "feature_merged",
                "feature_pr_head",
                "CODE_REWORK",
            ),
        ):
            if snap.get(merged_key):
                continue
            pr_num = sess.get(pr_key)
            if not pr_num:
                continue
            live = str(snap.get(sha_key) or "")
            if not live:
                continue
            stored = str(sess.get(wm_key) or "")
            if stored == live:
                continue
            if not stored and state != rework:
                if not dry_run:
                    self.store.update_session_fields(sk, **{wm_key: live})
                    sess[wm_key] = live
                continue
            if remaining <= 0:
                report["capped"] = int(report["capped"]) + 1
                continue
            if dry_run:
                report["synthesized"] = int(report["synthesized"]) + 1
                remaining -= 1
                continue
            inserted = self._synthesize_synchronize(
                sess, snap, pr_number=int(pr_num), head_sha=live
            )
            if inserted:
                report["synthesized"] = int(report["synthesized"]) + 1
                remaining -= 1
                log.info(
                    "reconcile head drift session=%s pr=%s %s → %s (#211)",
                    sk,
                    pr_num,
                    stored or "NULL",
                    live,
                )
        return remaining

    def _synthesize_synchronize(
        self,
        sess: dict[str, Any],
        snap: dict[str, Any],
        *,
        pr_number: int,
        head_sha: str,
    ) -> bool:
        repo = str(sess.get("repo") or "")
        issue_num = int(sess.get("issue_num") or 0)
        node: dict[str, Any] = {}
        for n in snap.get("nodes") or []:
            if not isinstance(n, dict):
                continue
            if n.get("kind") != "pull_request":
                continue
            try:
                if int(n.get("number") or 0) == pr_number:
                    node = n
                    break
            except (TypeError, ValueError):
                continue
        author = str(node.get("author") or "")
        payload = {
            "action": "synchronize",
            "pull_request": {
                "node_id": str(node.get("id") or ""),
                "number": pr_number,
                "merged": False,
                "title": str(node.get("title") or ""),
                "html_url": f"https://github.com/{repo}/pull/{pr_number}",
                "user": {"login": author},
                "head": {
                    "ref": str(node.get("head_ref") or ""),
                    "sha": head_sha,
                },
            },
            "sender": {"login": author},
            "repository": {"full_name": repo},
        }
        return self.store.insert_delivery(
            delivery_id=f"recon:sync:{pr_number}:{head_sha}",
            event="pull_request",
            action="synchronize",
            repo=repo,
            issue_num=issue_num,
            sender=author,
            payload=json.dumps(payload).encode(),
            status="queued",
        )

    def _synth_untracked_opened(
        self,
        sess: dict[str, Any],
        report: dict[str, Any],
        *,
        dry_run: bool,
        remaining: int,
    ) -> int:
        """ADR-40: enqueue pull_request.opened for an untracked role-branch PR."""
        if self.fetch_open_prs is None:
            return remaining
        state = str(sess.get("state") or "")
        repo = str(sess.get("repo") or "")
        sk = str(sess.get("session_key") or "")
        try:
            issue_num = int(sess.get("issue_num") or 0)
        except (TypeError, ValueError):
            return remaining
        if not repo or not issue_num:
            return remaining
        owner, _, _ = repo.partition("/")
        for pr_key, role, kind in (
            ("design_pr", "architect", "design_pr_opened"),
            ("feature_pr", "developer", "feature_pr_opened"),
        ):
            if sess.get(pr_key):
                continue
            if transition(state, kind) is None:
                continue
            if remaining <= 0:
                report["capped"] = int(report["capped"]) + 1
                continue
            branch = role_branch_name(repo, issue_num, role)
            head = f"{owner}:{branch}"
            try:
                listed = self.fetch_open_prs(repo, head) or []
            except Exception:
                log.exception(
                    "untracked PR fetch failed session=%s half=%s", sk, role
                )
                continue
            matches: list[dict[str, Any]] = []
            for pr in listed:
                if not isinstance(pr, dict):
                    continue
                if pr.get("merged") or str(pr.get("state") or "open") != "open":
                    continue
                href = str(json_obj(pr.get("head")).get("ref") or "")
                owned = (
                    is_design_head_ref(href, repo)
                    if role == "architect"
                    else is_feature_head_ref(href, repo)
                )
                if not owned:
                    continue
                parsed = parse_role_branch(href, repo)
                if parsed is None or parsed[0] != issue_num:
                    continue
                matches.append(pr)
            if not matches:
                continue
            matches.sort(key=lambda p: int(p.get("number") or 0))
            if len(matches) > 1:
                extras = [int(p.get("number") or 0) for p in matches[1:]]
                log.warning(
                    "reconcile untracked opened session=%s half=%s extra_prs=%s",
                    sk,
                    role,
                    extras,
                )
            chosen = matches[0]
            if dry_run:
                report["synthesized"] = int(report["synthesized"]) + 1
                remaining -= 1
                continue
            inserted = self._synthesize_opened(sess, chosen)
            if inserted:
                report["synthesized"] = int(report["synthesized"]) + 1
                remaining -= 1
                sha = str(json_obj(chosen.get("head")).get("sha") or "")
                log.info(
                    "reconcile untracked opened session=%s half=%s pr=%s "
                    "branch=%s head=%s (#229)",
                    sk,
                    role,
                    int(chosen.get("number") or 0),
                    branch,
                    sha,
                )
        return remaining

    def _synthesize_opened(self, sess: dict[str, Any], pr: dict[str, Any]) -> bool:
        repo = str(sess.get("repo") or "")
        issue_num = int(sess.get("issue_num") or 0)
        head = json_obj(pr.get("head"))
        sha = str(head.get("sha") or "")
        ref = str(head.get("ref") or "")
        number = int(pr.get("number") or 0)
        user = json_obj(pr.get("user"))
        author = str(user.get("login") or "")
        payload = {
            "action": "opened",
            "pull_request": {
                "node_id": str(pr.get("node_id") or ""),
                "number": number,
                "merged": False,
                "title": str(pr.get("title") or ""),
                "html_url": str(
                    pr.get("html_url") or f"https://github.com/{repo}/pull/{number}"
                ),
                "user": {"login": author},
                "head": {"ref": ref, "sha": sha},
            },
            "sender": {"login": author},
            "repository": {"full_name": repo},
        }
        return self.store.insert_delivery(
            delivery_id=f"recon:open:{number}:{sha}",
            event="pull_request",
            action="opened",
            repo=repo,
            issue_num=issue_num,
            sender=author,
            payload=json.dumps(payload).encode(),
            status="queued",
        )

    def _synthesize_node(
        self, sess: dict[str, Any], snap: dict[str, Any], node: dict[str, Any]
    ) -> None:
        nid = str(node["id"])
        kind = str(node.get("kind") or "")
        author = str(node.get("author") or "")
        repo = str(sess.get("repo") or "")
        issue_num = int(sess.get("issue_num") or 0)
        if kind == "comment":
            event, action = "issue_comment", "created"
            payload = {
                "action": "created",
                "issue": {
                    "number": issue_num,
                    "node_id": str(snap.get("issue_node_id") or ""),
                    "state": str(snap.get("issue_state") or ""),
                    "title": str(snap.get("issue_title") or ""),
                    "html_url": str(snap.get("issue_html_url") or ""),
                    "labels": list(snap.get("issue_labels") or []),
                },
                "comment": {
                    "node_id": nid,
                    "body": str(node.get("body") or ""),
                    "user": {"login": author},
                },
                "sender": {"login": author},
                "repository": {"full_name": repo},
            }
        elif kind == "pull_request":
            event, action = "pull_request", "opened"
            payload = {
                "action": "opened",
                "pull_request": {
                    "node_id": nid,
                    "number": node.get("number"),
                    "merged": bool(node.get("merged")),
                    "title": str(node.get("title") or ""),
                    "user": {"login": author},
                    "head": {
                        "ref": str(node.get("head_ref") or ""),
                        "sha": str(node.get("head_sha") or ""),
                    },
                },
                "sender": {"login": author},
                "repository": {"full_name": repo},
            }
        elif kind == "review":
            event, action = "pull_request_review", "submitted"
            payload = {
                "action": "submitted",
                "review": {
                    "node_id": nid,
                    "state": str(node.get("state") or ""),
                    "user": {"login": author},
                },
                "pull_request": {
                    "number": node.get("number"),
                    "title": str(node.get("title") or ""),
                    "head": {"ref": str(node.get("head_ref") or "")},
                    # #209: without this, _pr_author_login returns None and
                    # ADR-30's author-sent guard short-circuits, so the author's
                    # own review re-dispatches a turn to the counterpart.
                    # It is the PR's author — `author` here is the *review's*,
                    # and using it would make every synthesized review look
                    # author-sent, silently stalling the loop instead.
                    "user": {"login": str(node.get("pr_author") or "")},
                },
                "sender": {"login": author},
                "repository": {"full_name": repo},
            }
        else:
            return
        self.store.insert_delivery(
            delivery_id=f"recon:{nid}",
            event=event,
            action=action,
            repo=repo,
            issue_num=issue_num,
            sender=author,
            payload=json.dumps(payload).encode(),
            status="queued",
        )

    def _decide(
        self,
        *,
        project: str,
        container_id: str,
        age_s: int,
        started_at: int,
        live: bool,
        inflight: bool,
        runner: dict[str, Any] | None,
    ) -> dict[str, str]:
        young = started_at <= 0 or age_s < CONTAINER_AGE_FLOOR_S
        if live and runner and _cid_match(str(runner.get("container_id") or ""), container_id):
            return {"action": "keep", "reason": "live runner"}
        if young:
            return {"action": "spare", "reason": "age < 10m"}
        if inflight:
            return {"action": "spare", "reason": "open turn"}
        if live and not runner:
            return {"action": "missing_runner", "reason": "no runners row"}
        if live and runner:
            return {"action": "remove", "reason": "extra container"}
        return {"action": "remove", "reason": "no live session"}
