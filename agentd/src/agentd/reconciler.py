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
from agentd.design_loop import CLOSE_RECONCILE_PREFIX
from agentd.fsm import transition

log = logging.getLogger("agentd.reconciler")

CONTAINER_AGE_FLOOR_S = 10 * 60
INFLIGHT_TURN_MAX_AGE_S = 900  # matches gateway turn_deadline_s default
RECONCILE_INTERVAL_S = 5 * 60
SYNTHESIS_CAP = 50

ListContainers = Callable[[], list[dict[str, Any]]]
RemoveContainer = Callable[[str], None]
NudgeFn = Callable[[], None]
FetchSnapshot = Callable[[dict[str, Any]], dict[str, Any] | None]
EscalateFn = Callable[..., None]


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
        out.append(
            {
                "id": str(obj.get("Id") or ""),
                "project": str(labels.get("agentd.project") or ""),
                "started_at": _parse_started_at(
                    str((obj.get("State") or {}).get("StartedAt") or "")
                ),
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
        nudge: NudgeFn | None = None,
        fetch_snapshot: FetchSnapshot | None = None,
        escalate: EscalateFn | None = None,
        interval_s: float = RECONCILE_INTERVAL_S,
        now_fn: Callable[[], int] | None = None,
    ) -> None:
        self.store = store
        self.list_containers = list_containers or list_managed_containers
        self.remove_container = remove_container or _docker_rm
        self.nudge = nudge
        self.fetch_snapshot = fetch_snapshot
        self.escalate = escalate
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
            "adopted=%d escalated=%d in %dms",
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

        self._sweep_github(report, dry_run=dry_run)
        if not dry_run and self.nudge:
            self.nudge()
        self._log_pass(report, containers=len(containers), kept=n_kept, started=t0)
        return report

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
            except Exception:
                log.exception(
                    "sweep fetch failed session=%s", sess.get("session_key")
                )
                continue
            if not snap:
                continue
            remaining = self._apply_snapshot(
                sess, snap, report, dry_run=dry_run, remaining=remaining
            )
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
        paused = str(sess.get("paused_reason") or "")

        if issue_state == "open" and paused.startswith(CLOSE_RECONCILE_PREFIX):
            report["holds_lifted"] = int(report["holds_lifted"]) + 1
            if not dry_run:
                stripped = paused.removeprefix(CLOSE_RECONCILE_PREFIX) or None
                self.store.update_session_fields(sk, paused_reason=stripped)
                log.info("close-reconcile lift session=%s (issue open)", sk)

        if issue_state == "closed":
            report["escalated"] = int(report["escalated"]) + 1
            if not dry_run and self.escalate:
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

        body = snap.get("issue_body")
        if body and bool(sess.get("verified_at")) != ("[x]" in str(body).lower() or "[X]" in str(body)):
            # cheap drift signal — do not write verified_at
            log.info(
                "body drift session=%s verified_at=%s",
                sk,
                sess.get("verified_at"),
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
                    "user": {"login": author},
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
                "pull_request": {"number": node.get("number")},
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
