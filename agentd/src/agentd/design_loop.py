"""Design-loop orchestration — deferred deliveries → sessions → turns (M3)."""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from agentd.config import Config
from agentd.db import Store, decompress_payload
from agentd.digest import build_digest, digest_to_markdown
from agentd.fsm import transition
from agentd.gitops import project_key_from_repo, project_path
from agentd.intake import evaluate_intake
from agentd.loop_safety import BudgetState, StallTracker, progress_fingerprint
from agentd.routing import RouteAction, provenance_footer, route_for_recipient
from agentd.rpc_client import RunnerClient
from agentd.supervisor import SessionSupervisor

log = logging.getLogger("agentd.design_loop")

# Serialize turns per (project, role) — one CLI conversation per role (#20).
_role_locks: dict[str, threading.Lock] = {}
_role_locks_guard = threading.Lock()


def _lock_for_project_role(project_key: str, role: str) -> threading.Lock:
    key = f"{project_key}::{role}"
    with _role_locks_guard:
        if key not in _role_locks:
            _role_locks[key] = threading.Lock()
        return _role_locks[key]


def _payload_dict(raw: bytes) -> dict[str, Any]:
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


class DesignLoop:
    """Processes deferred deliveries into session turns for the design half."""

    def __init__(
        self,
        store: Store,
        config: Config,
        supervisor: SessionSupervisor | None = None,
        *,
        dispatch_turns: bool = True,
    ) -> None:
        self.store = store
        self.config = config
        self.supervisor = supervisor
        self.dispatch_turns = dispatch_turns

    def process_deferred_batch(self, limit: int = 20) -> int:
        n = 0
        for row in self.store.list_deferred(limit=limit):
            try:
                self._process_one(row)
                n += 1
            except Exception:
                log.exception("design_loop failed delivery=%s", row["delivery_id"])
        return n

    def _process_one(self, row) -> None:
        delivery_id = str(row["delivery_id"])
        event = str(row["event"])
        action = str(row["action"]) if row["action"] is not None else None
        repo = str(row["repo"] or "")
        issue_num = row["issue_num"]
        sender = str(row["sender"] or "")
        raw = decompress_payload(row["payload"])
        data = _payload_dict(raw)

        if not repo or issue_num is None:
            self.store.set_delivery_status(delivery_id, "dropped")
            return

        session_key = f"{repo}#{int(issue_num)}"
        # Defaults until session row exists; after that session bindings win (§5.3)
        default_arch = self.config.agent_login("claude") or "huozheclaude"
        default_dev = self.config.agent_login("grok") or "huozhegrok"

        sess = self.store.get_session(session_key)
        if sess is None:
            # §4.3: only an intake-passing *issues* event may create a session.
            # issue_comment / PR / push on a non-session issue must not conjure one
            # (#16 — deferred backlog must not spawn 9 containers).
            if not self._may_create_session(event, action, raw):
                self.store.set_delivery_status(delivery_id, "dropped")
                log.info(
                    "drop id=%s: no session for %s and event is not intake-passing issues",
                    delivery_id,
                    session_key,
                )
                return
            if self.supervisor is None:
                log.warning("no supervisor; cannot create session %s", session_key)
                return
            try:
                self.supervisor.ensure_session(
                    session_key=session_key,
                    repo=repo,
                    issue_num=int(issue_num),
                    architect_login=default_arch,
                    developer_login=default_dev,
                )
            except RuntimeError as e:
                # Capacity refusal: leave deferred for retry when a HOT slot frees.
                # Warning only — no traceback every ~5s drain cycle.
                if "max_hot_containers" in str(e):
                    log.warning(
                        "ensure_session deferred (capacity): %s — %s",
                        session_key,
                        e,
                    )
                    return
                log.exception("ensure_session failed %s", session_key)
                return
            except Exception:
                log.exception("ensure_session failed %s", session_key)
                return
            sess = self.store.get_session(session_key)
            if sess:
                self.store.update_session_fields(session_key, state="PLANNING")
                tr = transition("INTAKE", "session_created")
                if tr:
                    self.store.update_session_fields(session_key, state=tr.new_state)
            sess = self.store.get_session(session_key)

        if not sess:
            return

        architect = str(sess.get("architect") or default_arch)
        developer = str(sess.get("developer") or default_dev)
        role_logins = {"architect": architect, "developer": developer}
        bot_logins = {architect, developer}

        state = str(sess.get("state") or "PLANNING")
        paused = state == "PAUSED_HUMAN" or bool(sess.get("paused_reason"))

        budget = BudgetState(
            turn_count=int(sess.get("turn_count") or 0),
            consec_agent_turns=int(sess.get("consec_agent_turns") or 0),
            review_rounds=int(sess.get("review_rounds") or 0),
        )
        breach = budget.breach()
        if breach:
            self._escalate(session_key, "system", breach)
            self.store.set_delivery_status(delivery_id, "done")
            return

        dig = build_digest(
            event=event,
            action=action,
            repo=repo,
            issue_num=int(issue_num),
            sender=sender,
            payload=data,
        )
        kind = self._event_kind(event, action, data, sender)

        # §8.4: do not advance on unverified APPROVED (M3-3)
        if kind == "design_approved_unverified":
            pr_num = dig.get("pr") or sess.get("design_pr")
            head = dig.get("head_sha")
            # Developer approves Design PR; verify Developer on current head
            from agentd.keychain import get_password
            from agentd.verify import verify_design_approval

            check = verify_design_approval(
                repo=repo,
                pr_number=int(pr_num or 0),
                expected_approver_login=developer,
                head_sha=str(head) if head else None,
                token=get_password("claude-bot") or get_password("grok-bot"),
            )
            if not check.ok:
                log.warning(
                    "design_approved blocked id=%s: %s", delivery_id, check.reason
                )
                self._escalate(
                    session_key,
                    "system",
                    f"unverified design approval: {check.reason}",
                )
                self.store.set_delivery_status(delivery_id, "done")
                return
            kind = "design_approved"

        recipient_role, recipient_login = self._pick_recipient(
            kind, state, architect, developer, sender
        )
        other = developer if recipient_role == "architect" else architect
        body = None
        if isinstance(data.get("comment"), dict):
            body = data["comment"].get("body")
        decision = route_for_recipient(
            sender=sender,
            recipient_login=recipient_login,
            recipient_role=recipient_role,
            other_bot_login=other,
            owner=self.config.owner,
            body=body,
            session_paused=paused,
            role_logins=role_logins,
            bot_logins=bot_logins,
        )
        if decision.action == RouteAction.DROP:
            self.store.set_delivery_status(delivery_id, "dropped")
            log.info("route drop id=%s reason=%s", delivery_id, decision.reason)
            return
        if decision.action == RouteAction.DEFER:
            log.info("route defer id=%s reason=%s", delivery_id, decision.reason)
            return

        if decision.reset_consec:
            self.store.update_session_fields(session_key, consec_agent_turns=0)

        tr = transition(state, kind)
        if tr:
            fields: dict[str, Any] = {"state": tr.new_state}
            if tr.freeze_roles:
                fields["roles_locked"] = 1
            if kind == "design_pr_opened" and dig.get("pr"):
                fields["design_pr"] = dig["pr"]
            self.store.update_session_fields(session_key, **fields)
            state = tr.new_state
            log.info("fsm %s → %s (%s)", session_key, tr.new_state, tr.note)

        if kind in ("design_changes_requested", "design_revised"):
            raw_fp = str(sess.get("progress_fp") or "")
            armed = raw_fp.startswith("A:")
            stored_fp = raw_fp[2:] if armed else (raw_fp or None)
            stall = StallTracker(
                last_fp=stored_fp,
                fp_repeat=int(sess.get("progress_repeat") or 0),
                zero_thread_rounds=int(sess.get("zero_thread_rounds") or 0),
                seen_head_change=armed,
            )
            head = dig.get("head_sha")
            fp = progress_fingerprint(
                open_thread_ids=[],
                unresolved_count=0,
                diff_stat=str(dig.get("pr") or "") + "|" + str(dig.get("title") or ""),
            )
            reason = stall.observe_fingerprint(fp, str(head) if head else None)
            if not reason and kind == "design_changes_requested":
                reason = stall.observe_review_round(threads_resolved=0)
            if reason:
                self._escalate(session_key, "system", reason)
                self.store.set_delivery_status(delivery_id, "done")
                return
            fp_store = (
                f"A:{stall.last_fp}" if stall.seen_head_change else (stall.last_fp or "")
            )
            self.store.update_session_fields(
                session_key,
                progress_fp=fp_store,
                progress_repeat=stall.fp_repeat,
                zero_thread_rounds=stall.zero_thread_rounds,
                review_rounds=int(sess.get("review_rounds") or 0) + 1,
            )

        # Dispatch turn to the session runner
        if self.dispatch_turns and self.supervisor and sess.get("endpoint"):
            turn_id = "t-" + uuid.uuid4().hex[:12]
            self._dispatch_turn(
                session_key=session_key,
                role=recipient_role,
                turn_id=turn_id,
                delivery_id=delivery_id,
                dig=dig,
                issue_num=int(issue_num),
            )
            budget.after_agent_turn()
            self.store.update_session_fields(
                session_key,
                turn_count=budget.turn_count,
                consec_agent_turns=budget.consec_agent_turns,
            )

        self.store.set_delivery_status(delivery_id, "routed")
        log.info(
            "delivery routed id=%s session=%s role=%s kind=%s",
            delivery_id,
            session_key,
            recipient_role,
            kind,
        )

    def _dispatch_turn(
        self,
        *,
        session_key: str,
        role: str,
        turn_id: str,
        delivery_id: str,
        dig: dict[str, Any],
        issue_num: int,
    ) -> None:
        sess = self.store.get_session(session_key) or {}
        repo = str(sess.get("repo") or "")
        project_key = str(sess.get("project_key") or project_key_from_repo(repo))
        runner = self.store.get_runner(project_key) or self.store.get_runner_for_session(
            session_key
        )
        if not runner:
            return
        endpoint = str(runner["endpoint"])
        host, _, port_s = endpoint.partition(":")
        bearer = str(runner["token"])
        # Project layout: sessions/<issue>/<role>/…
        role_base = (
            project_path(self.config.root, repo or project_key)
            / "sessions"
            / str(int(issue_num))
            / role
        )
        digest_path = role_base / "context" / f"digest-{turn_id}.md"
        digest_path.parent.mkdir(parents=True, exist_ok=True)
        # Per-issue framing for multi-issue single CLI conversation (#20).
        dig_md = digest_to_markdown(dig)
        framed = (
            f"# Active issue: {session_key}\n"
            f"# Project: {project_key}\n"
            f"# Role: {role}\n\n"
            f"{dig_md}"
        )
        digest_path.write_text(framed, encoding="utf-8")

        started = int(time.time())
        self.store.insert_turn(
            turn_id=turn_id,
            session_key=session_key,
            role=role,
            delivery_id=delivery_id,
            started_at=started,
            ended_at=None,
            status=None,
            summary=None,
        )
        lock = _lock_for_project_role(project_key, role)
        try:
            with lock:
                with RunnerClient(host, int(port_s), bearer, timeout_s=120) as cli:
                    result = cli.call(
                        "turn.dispatch",
                        {
                            "turn_id": turn_id,
                            "role": role,
                            "deadline_s": 900,
                            "event": dig,
                            "context": {
                                "worktree": (
                                    f"/srv/agentd/sessions/{int(issue_num)}/"
                                    f"{role}/worktrees/issue-{int(issue_num)}"
                                ),
                                "digest": (
                                    f"/srv/agentd/sessions/{int(issue_num)}/"
                                    f"{role}/context/digest-{turn_id}.md"
                                ),
                                "session_key": session_key,
                                "project_key": project_key,
                                "issue_num": int(issue_num),
                            },
                            "budget": {},
                        },
                    )
        except Exception as exc:
            log.exception("turn.dispatch failed: %s", exc)
            self.store.insert_turn(
                turn_id=turn_id + "-err",
                session_key=session_key,
                role=role,
                delivery_id=delivery_id,
                started_at=started,
                ended_at=int(time.time()),
                status="failed",
                summary=str(exc)[:500],
            )
            return

        ended = int(time.time())
        status = str((result or {}).get("status") or "done")
        summary = str((result or {}).get("summary") or "")[:2000]
        with self.store._lock:
            self.store._conn.execute(
                "UPDATE turns SET ended_at=?, status=?, summary=? WHERE turn_id=?",
                (ended, status, summary, turn_id),
            )
            self.store._conn.commit()

        for art in (result or {}).get("artifacts") or []:
            if isinstance(art, dict) and art.get("ref"):
                self.store.register_artifact(
                    session_key=session_key,
                    role=role,
                    kind=str(art.get("kind") or "scratch"),
                    ref=str(art["ref"]),
                )

        if status == "needs_human":
            self._escalate(session_key, role, summary or "needs_human")

        # Provenance footer helper for agent comments (agents should append; we log it)
        log.info(
            "turn complete id=%s footer=%s",
            turn_id,
            provenance_footer(session_key=session_key, role=role, turn_id=turn_id),
        )

    def _escalate(self, session_key: str, role: str, reason: str) -> None:
        self.store.open_escalation(session_key=session_key, role=role, reason=reason)
        self.store.update_session_fields(
            session_key, state="PAUSED_HUMAN", paused_reason=reason
        )
        log.warning("escalation session=%s reason=%s", session_key, reason)

    def _event_kind(
        self,
        event: str,
        action: str | None,
        data: dict[str, Any],
        sender: str,
    ) -> str:
        if event == "issues" and action in ("opened", "reopened", "labeled"):
            return "issue_opened"
        if event == "pull_request":
            pr = data.get("pull_request") or {}
            title = str(pr.get("title") or "")
            is_design = "design" in title.lower() or "rfc" in title.lower()
            if action == "opened" and is_design:
                return "design_pr_opened"
            if action == "synchronize" and is_design:
                return "design_revised"
            if action == "closed" and pr.get("merged"):
                return "design_merged"
        if event == "pull_request_review":
            review = data.get("review") or {}
            st = str(review.get("state") or "").upper()
            if st == "CHANGES_REQUESTED":
                return "design_changes_requested"
            if st == "APPROVED":
                # Unverified until §8.4 check runs
                return "design_approved_unverified"
        if event == "issue_comment" and action == "created":
            if sender.lower() == self.config.owner.lower():
                return "owner_reply"
        return f"{event}.{action or 'none'}"

    def _may_create_session(
        self, event: str, action: str | None, payload: bytes
    ) -> bool:
        """True only for §4.3 intake-passing issues open/reopen/labeled."""
        decision = evaluate_intake(
            event=event,
            action=action,
            payload=payload,
            config=self.config,
        )
        return decision is not None and decision.accepted

    def _pick_recipient(
        self,
        kind: str,
        state: str,
        architect: str,
        developer: str,
        sender: str,
    ) -> tuple[str, str]:
        # Happy path: issue → architect; design_pr → developer; changes → architect; etc.
        if kind in ("issue_opened", "design_changes_requested", "design_merged", "merge_design"):
            return "architect", architect
        if kind in ("design_pr_opened", "design_revised", "design_approved"):
            return "developer", developer
        # Default: route to the role that is not the sender bot
        if sender.lower() == architect.lower():
            return "developer", developer
        return "architect", architect
