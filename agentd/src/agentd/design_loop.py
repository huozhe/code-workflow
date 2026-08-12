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
from agentd.gitops import (
    is_design_head_ref,
    is_feature_head_ref,
    parse_role_branch,
    project_dir_name,
    project_key_from_repo,
    project_path,
    role_branch_name,
)
from agentd.github_fetch import (
    PrReviewThreadSnapshot,
    fetch_diff_stat,
    fetch_pr_review_threads,
)
from agentd.github_write import (
    format_escalation_comment,
    get_issue_body,
    patch_issue_body,
    post_issue_comment,
    reopen_issue,
)
from agentd.verification import (
    checkbox_is_checked,
    default_steps_for_session,
    extract_verification_block,
    reinsert_verification_block,
    set_checkbox_in_body,
    upsert_verification_block,
)
from agentd.intake import evaluate_intake
from agentd.keychain import get_password
from agentd.loop_safety import (
    BudgetState,
    SilentTurnTracker,
    StallTracker,
    progress_fingerprint,
)

# Review webhook *parts* — not turn drivers (#49). Verdict lives on
# pull_request_review.submitted; inline comments and thread resolve/unresolve
# are components of that review. Turning each part burned ~10 empty turns on
# session #47 and false-tripped silent-turn (#42). Mark terminal (done), never
# leave deferred. Standalone "Add single comment" still wakes once: GitHub
# always emits review.submitted (state COMMENTED) for that path.
_REVIEW_PART_EVENTS = frozenset(
    {
        "pull_request_review_comment",
        "pull_request_review_thread",
    }
)

# §8.4 merge-auth transient retries (PR #54 B1): delivery_id → attempt count.
# In-memory is enough — restart resets the counter (more retries, not less).
_merge_auth_attempts: dict[str, int] = {}
_MERGE_AUTH_MAX_ATTEMPTS = 5

# Webhook kinds that are observed GitHub progress (P1) — reset silent_turns
# even when the FSM string does not change (e.g. design_revised while already
# DESIGN_REVIEW). Claims in public_actions never reset (PR #42 B1).
_OBSERVED_PROGRESS_KINDS = frozenset(
    {
        "issue_opened",
        "design_pr_opened",
        "design_revised",
        "design_changes_requested",
        "design_approved",
        "design_merged",
        "feature_pr_opened",
        "feature_revised",
        "code_changes_requested",
        "merge_authorized",
        "feature_merged",
        "owner_reply",
    }
)
from agentd.refusals import CapacityRefusal, StructuralRefusal
from agentd.routing import RouteAction, provenance_footer, route_for_recipient
from agentd.rpc_client import RunnerClient
from agentd.supervisor import SessionSupervisor

log = logging.getLogger("agentd.design_loop")

# Serialize turns per (project, role) — one CLI conversation per role (#20).
_role_locks: dict[str, threading.Lock] = {}
_role_locks_guard = threading.Lock()
# After a gateway RPC timeout the runner may still be inside the turn.
# Hold the role busy until started_at + deadline_s so a second dispatch
# does not interleave (#34). Cleared when the wait elapses.
_role_busy_until: dict[str, float] = {}

# Short-lived compare cache: same head ⇒ same tree; review *threads* can
# resolve without a new commit, so they are never cached (PR #29 NB2).
_STALL_DIFF_CACHE: dict[str, str] = {}



def _lock_for_project_role(project_key: str, role: str) -> threading.Lock:
    key = f"{project_key}::{role}"
    with _role_locks_guard:
        if key not in _role_locks:
            _role_locks[key] = threading.Lock()
        return _role_locks[key]


def _role_key(project_key: str, role: str) -> str:
    return f"{project_key}::{role}"


def _is_rpc_timeout(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    # socket.timeout is TimeoutError on 3.10+; OSError may still say "timed out".
    return "timed out" in str(exc).lower()


def _append_host_transcript(path: Path, record: dict[str, Any]) -> None:
    """Append one JSON line to the host-side transcript (§6.3 / #34)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")


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
        post_comment: Any | None = None,
        reopen_issue_fn: Any | None = None,
        gateway_token: str | None = None,
        fetch_threads: Any | None = None,
        fetch_diff: Any | None = None,
        github_token: str | None = None,
        get_issue_body_fn: Any | None = None,
        patch_issue_body_fn: Any | None = None,
    ) -> None:
        self.store = store
        self.config = config
        self.supervisor = supervisor
        self.dispatch_turns = dispatch_turns
        # Injectables for unit tests (M3-A write / M3-B fetch boundary).
        self._post_comment = post_comment
        self._reopen_issue = reopen_issue_fn
        self._gateway_token = gateway_token
        self._fetch_threads = fetch_threads
        self._fetch_diff = fetch_diff
        self._github_token = github_token
        self._get_issue_body = get_issue_body_fn
        self._patch_issue_body = patch_issue_body_fn

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

        # Remap PR-keyed deliveries to the originating issue session (M3-D).
        # GitHub shares one number space: webhook "issue_num" for PR events is
        # the *PR* number, not the agentd issue session key.
        issue_num, session_key, sess = self._resolve_session_for_delivery(
            event=event,
            repo=repo,
            issue_num=int(issue_num),
            data=data,
        )
        # Defaults until session row exists; after that session bindings win (§5.3)
        default_arch = self.config.agent_login("claude") or "huozheclaude"
        default_dev = self.config.agent_login("grok") or "huozhegrok"

        if sess is None:
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
            except CapacityRefusal as e:
                # Transient: leave deferred for retry when a HOT slot frees.
                # Typed — never match message text (#35).
                log.warning(
                    "ensure_session deferred (capacity): %s — %s",
                    session_key,
                    e,
                )
                return
            except StructuralRefusal as e:
                self._handle_structural_refusal(
                    session_key=session_key,
                    repo=repo,
                    issue_num=int(issue_num),
                    delivery_id=delivery_id,
                    reason=str(e),
                    architect=default_arch,
                    developer=default_dev,
                )
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

        # #49: one review → one turn. Parts are terminal without a turn (not
        # deferred — redelivery would re-wake and queue_depth 0 hides them).
        if event in _REVIEW_PART_EVENTS:
            self.store.set_delivery_status(delivery_id, "done")
            log.info(
                "delivery done id=%s event=%s action=%s — review part, no turn "
                "(coalesce onto pull_request_review.submitted)",
                delivery_id,
                event,
                action,
            )
            return

        architect = str(sess.get("architect") or default_arch)
        developer = str(sess.get("developer") or default_dev)
        role_logins = {"architect": architect, "developer": developer}
        # Include gateway login so §9.1 rule 6 cannot treat it as a human collaborator
        # (PR #27 B2 / Architect: without this, gateway comments route as human-or-other).
        bot_logins = {architect, developer} | self.config.all_bot_logins()

        # §10.3 / #36: only the human owner may close a *session* issue.
        # Non-session closes (e.g. Fixes #NN on a maintenance PR) never reach
        # here — no sessions row ⇒ dropped above.
        if event == "issues" and action == "closed":
            self._handle_session_issue_closed(
                session_key=session_key,
                sess=sess,
                repo=repo,
                issue_num=int(issue_num),
                delivery_id=delivery_id,
                sender=sender,
                bot_logins=bot_logins,
            )
            return

        # §10.2 / M5-1: checkbox record + agent-edit restore (no agent turn).
        if event == "issues" and action == "edited":
            self._handle_session_issue_edited(
                session_key=session_key,
                sess=sess,
                repo=repo,
                issue_num=int(issue_num),
                delivery_id=delivery_id,
                sender=sender,
                data=data,
            )
            return

        state = str(sess.get("state") or "PLANNING")
        paused = state == "PAUSED_HUMAN" or bool(sess.get("paused_reason"))

        budget = BudgetState(
            turn_count=int(sess.get("turn_count") or 0),
            consec_agent_turns=int(sess.get("consec_agent_turns") or 0),
            review_rounds=int(sess.get("review_rounds") or 0),
        )
        # Do not re-escalate while already paused — owner reply is the only exit.
        if not paused:
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
        kind = self._event_kind(event, action, data, sender, repo=repo)

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
            dig["kind"] = kind

        # §8.4 Feature merge auth (M4-2): Architect APPROVED is not enough —
        # required_checks + mergeable_state must pass before merge_authorized.
        # Gateway verifies only; Developer merges (opposite of §8.3).
        # Reads use gateway credential (#29 NB1 / PR #54 NB).
        if kind == "feature_approved_unverified":
            pr_num = dig.get("pr") or sess.get("feature_pr")
            head = dig.get("head_sha")
            from agentd.verify import verify_feature_merge

            check = verify_feature_merge(
                repo=repo,
                pr_number=int(pr_num or 0),
                expected_approver_login=architect,
                head_sha=str(head) if head else None,
                token=self._github_api_token(),
                required_checks=self.config.required_checks(repo),
            )
            if not check.ok:
                if check.transient:
                    n = int(_merge_auth_attempts.get(delivery_id, 0)) + 1
                    _merge_auth_attempts[delivery_id] = n
                    if n < _MERGE_AUTH_MAX_ATTEMPTS:
                        log.warning(
                            "feature merge_auth deferred (transient) id=%s "
                            "attempt=%s/%s: %s",
                            delivery_id,
                            n,
                            _MERGE_AUTH_MAX_ATTEMPTS,
                            check.reason,
                        )
                        # Leave status=deferred for next drain cycle.
                        return
                    _merge_auth_attempts.pop(delivery_id, None)
                    log.warning(
                        "feature merge_auth exhausted retries id=%s: %s",
                        delivery_id,
                        check.reason,
                    )
                    self._escalate(
                        session_key,
                        "system",
                        f"feature merge authorization still not ready after "
                        f"{n} attempts (was transient): {check.reason}",
                    )
                    self.store.set_delivery_status(delivery_id, "done")
                    return
                _merge_auth_attempts.pop(delivery_id, None)
                log.warning(
                    "feature merge_authorized blocked (permanent) id=%s: %s",
                    delivery_id,
                    check.reason,
                )
                self._escalate(
                    session_key,
                    "system",
                    f"unverified feature merge authorization (permanent): "
                    f"{check.reason}",
                )
                self.store.set_delivery_status(delivery_id, "done")
                return
            _merge_auth_attempts.pop(delivery_id, None)
            kind = "merge_authorized"
            dig["kind"] = kind
            dig["merge_auth"] = check.reason
            # Log configured checks so ops can compare to ruleset (M4-3 #51 note:
            # ruleset may lack required_status_checks while config lists names).
            log.info(
                "feature merge_authorized id=%s pr=%s required_checks=%s detail=%s",
                delivery_id,
                pr_num,
                self.config.required_checks(repo),
                check.reason,
            )

        # Only the Design PR merge advances DESIGN_APPROVED → IMPLEMENTING (§8.3).
        if kind == "design_merged":
            tracked = sess.get("design_pr")
            event_pr = dig.get("pr")
            if not tracked or not event_pr or int(event_pr) != int(tracked):
                log.info(
                    "drop id=%s: merge of pr=%s is not design_pr=%s",
                    delivery_id,
                    event_pr,
                    tracked,
                )
                self.store.set_delivery_status(delivery_id, "dropped")
                return

        # Feature PR merge: tracked PR only; branch ledger cleanup; §8.4 bypass
        # outside MERGING escalates (PR #53 Architect review / M4-2).
        if kind == "feature_merged":
            tracked = sess.get("feature_pr")
            event_pr = dig.get("pr")
            if not tracked or not event_pr or int(event_pr) != int(tracked):
                log.info(
                    "drop id=%s: merge of pr=%s is not feature_pr=%s",
                    delivery_id,
                    event_pr,
                    tracked,
                )
                self.store.set_delivery_status(delivery_id, "dropped")
                return
            # Branch is gone on GitHub (agent deleted after merge) — ledger truth.
            branch_ref = role_branch_name(repo, int(issue_num), "developer")
            n_rm = self.store.mark_artifact_removed(
                session_key=session_key, ref=branch_ref, kind="branch"
            )
            log.info(
                "feature_merged branch artifact removed session=%s ref=%s n=%s",
                session_key,
                branch_ref,
                n_rm,
            )
            if state == "AWAITING_VERIFICATION":
                # Redelivery after successful advance — terminal, no re-turn.
                self.store.set_delivery_status(delivery_id, "done")
                return
            if state != "MERGING":
                self._escalate(
                    session_key,
                    "system",
                    f"unauthorized Feature PR merge: state={state} expected MERGING "
                    f"(merged without merge_authorized / §8.4 bypass)",
                )
                self.store.set_delivery_status(delivery_id, "done")
                return

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
        # DEFER: leave queued — no FSM (session paused for non-owner).
        if decision.action == RouteAction.DEFER:
            log.info("route defer id=%s reason=%s", delivery_id, decision.reason)
            return

        if decision.reset_consec:
            # Owner activity resets agent-turn budget and silent-turn dead-end (#39).
            self.store.update_session_fields(
                session_key, consec_agent_turns=0, silent_turns=0
            )

        # M3-A / §8.5: owner reply while paused → unpause, inject reply, resume turn.
        if paused and kind == "owner_reply":
            self._resume_from_escalation(
                session_key=session_key,
                sess=sess,
                dig=dig,
                data=data,
                delivery_id=delivery_id,
                issue_num=int(issue_num),
            )
            return

        # P1: gateway drives FSM from *observed* GitHub events even when routing
        # drops the turn (self-echo). Architect merge of the Design PR is sent by
        # the Architect identity — recipient is also Architect (§8.3), so without
        # this the merge never advances DESIGN_APPROVED → IMPLEMENTING (M3-D).
        state_changed = False
        tr = transition(state, kind)
        if tr:
            fields: dict[str, Any] = {"state": tr.new_state}
            if tr.freeze_roles:
                fields["roles_locked"] = 1
            if kind == "design_pr_opened" and dig.get("pr"):
                fields["design_pr"] = dig["pr"]
            if kind == "feature_pr_opened" and dig.get("pr"):
                fields["feature_pr"] = dig["pr"]
            self.store.update_session_fields(session_key, **fields)
            state = tr.new_state
            state_changed = True
            log.info("fsm %s → %s (%s)", session_key, tr.new_state, tr.note)
            # M5-0 / #50: structural §10.1 block so issues.closed can classify.
            # Gateway authors the scaffold (reliability); Architect may refine
            # steps between sentinels on the AWAITING_VERIFICATION turn.
            if kind == "feature_merged" and tr.new_state == "AWAITING_VERIFICATION":
                sess_after = self.store.get_session(session_key) or sess
                self._ensure_verification_block(
                    repo=repo,
                    issue_num=int(issue_num),
                    design_pr=sess_after.get("design_pr"),
                    feature_pr=sess_after.get("feature_pr") or dig.get("pr"),
                )

        # Stall only when a turn would be routed — self-echo under §5.3 adapter
        # swap must not advance zero-thread (M3-D NB1).
        if (
            decision.action != RouteAction.DROP
            and kind
            in (
                "design_changes_requested",
                "design_revised",
                "code_changes_requested",
                "feature_revised",
            )
        ):
            stalled = self._observe_stall_signals(
                session_key=session_key,
                sess=sess,
                dig=dig,
                kind=kind,
                delivery_id=delivery_id,
                repo=repo,
            )
            if stalled:
                return
            sess = self.store.get_session(session_key) or sess

        # DROP after FSM: no agent turn (self-echo, own-artifact, …).
        if decision.action == RouteAction.DROP:
            self.store.set_delivery_status(delivery_id, "done")
            log.info(
                "route drop after fsm id=%s reason=%s kind=%s state=%s",
                delivery_id,
                decision.reason,
                kind,
                state,
            )
            return

        # Dispatch turn to the session runner
        if self.dispatch_turns and self.supervisor and sess.get("endpoint"):
            turn_id = "t-" + uuid.uuid4().hex[:12]
            turn_result = self._dispatch_turn(
                session_key=session_key,
                role=recipient_role,
                turn_id=turn_id,
                delivery_id=delivery_id,
                dig=dig,
                issue_num=int(issue_num),
            )
            status = (turn_result or {}).get("status")
            # Role still held after prior gateway timeout — leave deferred so
            # the single drain thread is not blocked (PR #37 B1 / #34).
            if status == "role_busy":
                return
            # Gateway timeout: runner may still finish; do not budget a phantom
            # failed turn (#34). Successful / other failed paths count once.
            if status != "gateway_timeout":
                budget.after_agent_turn()
                fields_upd: dict[str, Any] = {
                    "turn_count": budget.turn_count,
                    "consec_agent_turns": budget.consec_agent_turns,
                }
                # #39 / PR #42 B1: count silent turns on *observed* progress only.
                # public_actions are claims (tool_use) — diagnostic, not a reset.
                silent = SilentTurnTracker(
                    silent_count=int(sess.get("silent_turns") or 0),
                    threshold=int(self.config.silent_turn_limit),
                )
                actions = (turn_result or {}).get("public_actions") or []
                if not isinstance(actions, list):
                    actions = []
                observed = state_changed or kind in _OBSERVED_PROGRESS_KINDS
                breach = silent.after_turn(
                    public_actions=actions,
                    observed_progress=observed,
                    status=str(status) if status else None,
                )
                fields_upd["silent_turns"] = silent.silent_count
                self.store.update_session_fields(session_key, **fields_upd)
                if breach:
                    self._escalate(session_key, "system", breach)
                    self.store.set_delivery_status(delivery_id, "done")
                    return

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
    ) -> dict[str, Any] | None:
        """Dispatch one turn. Returns result dict (status/summary) or None."""
        sess = self.store.get_session(session_key) or {}
        repo = str(sess.get("repo") or "")
        project_key = str(sess.get("project_key") or project_key_from_repo(repo))
        runner = self.store.get_runner(project_key) or self.store.get_runner_for_session(
            session_key
        )
        if not runner:
            return None
        endpoint = str(runner["endpoint"])
        host, _, port_s = endpoint.partition(":")
        bearer = str(runner["token"])
        deadline_s = int(self.config.turn_deadline_s)
        rpc_timeout_s = float(self.config.rpc_timeout_s)
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
        transcript_path = role_base / "transcript.jsonl"

        rkey = _role_key(project_key, role)
        # Residual busy after a prior gateway timeout: do **not** sleep on the
        # single drain thread (PR #37 B1). Leave the delivery deferred; next
        # drain cycle retries when busy expires.
        busy_until = _role_busy_until.get(rkey, 0.0)
        now = time.time()
        if busy_until > now:
            retry_in = busy_until - now
            log.warning(
                "role busy after gateway timeout project=%s role=%s "
                "retry_in=%.1fs — leaving delivery deferred",
                project_key,
                role,
                retry_in,
            )
            return {
                "status": "role_busy",
                "summary": f"role busy; retry_in={retry_in:.1f}s",
            }
        if rkey in _role_busy_until:
            _role_busy_until.pop(rkey, None)

        started = time.time()  # float: residual busy-until needs sub-second accuracy
        self.store.insert_turn(
            turn_id=turn_id,
            session_key=session_key,
            role=role,
            delivery_id=delivery_id,
            started_at=int(started),
            ended_at=None,
            status=None,
            summary=None,
        )
        def _on_runner_notify(method: str, params: dict[str, Any]) -> None:
            # Runner → gateway: artifact.register (M3-C / §14.2).
            if method != "artifact.register":
                return
            ref = str(params.get("ref") or "").strip()
            if not ref:
                return
            art_role = str(params.get("role") or role)
            kind = str(params.get("kind") or "scratch")
            self.store.register_artifact(
                session_key=session_key,
                role=art_role,
                kind=kind,
                ref=ref,
            )
            log.info(
                "artifact.register notify session=%s role=%s kind=%s ref=%s",
                session_key,
                art_role,
                kind,
                ref,
            )

        lock = _lock_for_project_role(project_key, role)
        # True only after turn.dispatch is in flight — connect failures are not
        # mid-turn, so they must not mark the role busy (#34).
        call_started = False
        try:
            with lock:
                with RunnerClient(
                    host,
                    int(port_s),
                    bearer,
                    timeout_s=rpc_timeout_s,
                    on_notification=_on_runner_notify,
                ) as cli:
                    call_started = True
                    # #45: FSM state must reach the runner prompt (role obligations).
                    session_state = str(sess.get("state") or "PLANNING")
                    result = cli.call(
                        "turn.dispatch",
                        {
                            "turn_id": turn_id,
                            "role": role,
                            "session_state": session_state,
                            "deadline_s": deadline_s,
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
            ended = int(time.time())
            mid_turn_timeout = call_started and _is_rpc_timeout(exc)
            status = "gateway_timeout" if mid_turn_timeout else "failed"
            summary = str(exc)[:500]
            if mid_turn_timeout:
                log.error(
                    "turn.dispatch gateway timeout id=%s role=%s "
                    "deadline_s=%s rpc_timeout_s=%s: %s",
                    turn_id,
                    role,
                    deadline_s,
                    rpc_timeout_s,
                    exc,
                )
                # Runner may still be inside the turn; keep role busy until
                # the deadline the runner was given (not cancel — out of scope).
                _role_busy_until[rkey] = started + float(deadline_s)
            else:
                log.exception("turn.dispatch failed: %s", exc)
            self.store.finish_turn(
                turn_id,
                ended_at=ended,
                status=status,
                summary=summary,
                public_actions="[]",
            )
            try:
                _append_host_transcript(
                    transcript_path,
                    {
                        "ts": ended,
                        "turn_id": turn_id,
                        "role": role,
                        "kind": dig.get("kind"),
                        "status": status,
                        "summary": summary,
                        "public_actions": [],
                        "source": "gateway",
                    },
                )
            except OSError as texc:
                log.warning("host transcript append failed: %s", texc)
            return {"status": status, "summary": summary, "public_actions": []}

        ended = int(time.time())
        status = str((result or {}).get("status") or "done")
        summary = str((result or {}).get("summary") or "")[:2000]
        raw_actions = (result or {}).get("public_actions") or []
        if not isinstance(raw_actions, list):
            raw_actions = []
        actions_json = json.dumps(raw_actions, separators=(",", ":"))[:8000]
        self.store.finish_turn(
            turn_id,
            ended_at=ended,
            status=status,
            summary=summary,
            public_actions=actions_json,
        )

        # Model-reported artifacts are optional extras; primary ledger is
        # supervisor-observed at ensure_session (M3-C).
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
            "turn complete id=%s public_actions=%s footer=%s",
            turn_id,
            len(raw_actions),
            provenance_footer(session_key=session_key, role=role, turn_id=turn_id),
        )
        out = {"status": status, "summary": summary, **(result or {})}
        out["public_actions"] = raw_actions
        return out

    def _github_api_token(self) -> str | None:
        """Token for gateway-initiated GitHub *reads* (stall observation).

        Prefer the gateway credential so the audit trail matches who is
        observing (PR #29 NB1). Agent PATs are fallback only if gateway is
        missing — stall reads must not silently die when gateway is mint-only.
        """
        if self._github_token is not None:
            return self._github_token or None
        return (
            self._gateway_github_token()
            or get_password("claude-bot")
            or get_password("grok-bot")
        )

    def _observe_stall_signals(
        self,
        *,
        session_key: str,
        sess: dict[str, Any],
        dig: dict[str, Any],
        kind: str,
        delivery_id: str,
        repo: str,
    ) -> bool:
        """§9.3 fingerprint + zero-thread. Returns True if session escalated.

        Inputs come from GitHub (GraphQL threads + REST compare), not invented
        webhook fields. When observation fails, signals are **skipped** and
        logged — never replaced with a constant placeholder countdown.
        """
        raw_fp = str(sess.get("progress_fp") or "")
        armed = raw_fp.startswith("A:")
        stored_fp = raw_fp[2:] if armed else (raw_fp or None)
        stall = StallTracker(
            last_fp=stored_fp or None,
            fp_repeat=int(sess.get("progress_repeat") or 0),
            zero_thread_rounds=int(sess.get("zero_thread_rounds") or 0),
            seen_head_change=armed,
        )
        head = dig.get("head_sha")
        pr_num = dig.get("pr") or sess.get("design_pr")
        token = self._github_api_token()

        # --- real inputs (fetch boundary; tests inject here) ---
        # reviewThreads: always fetch — resolve does not move head.
        if self._fetch_threads is not None:
            snap = self._fetch_threads(
                repo=repo, pr_number=int(pr_num or 0), token=token
            )
        else:
            snap = fetch_pr_review_threads(
                repo=repo, pr_number=int(pr_num or 0), token=token
            )

        base = (snap.base_ref if snap else None) or "main"
        head_for_diff = str(head or (snap.head_oid if snap else "") or "")
        diff_key = f"{repo}#{int(pr_num or 0)}@{base}...{head_for_diff}"
        diff_stat: str | None = _STALL_DIFF_CACHE.get(diff_key)
        if diff_stat is None and head_for_diff:
            if self._fetch_diff is not None:
                diff_stat = self._fetch_diff(
                    repo=repo, base=base, head=head_for_diff, token=token
                )
            else:
                diff_stat = fetch_diff_stat(
                    repo=repo, base=base, head=head_for_diff, token=token
                )
            if diff_stat is not None:
                _STALL_DIFF_CACHE[diff_key] = diff_stat
                # pop(..., None): concurrent eviction can race under multi-project
                # delivery threads; KeyError would abort stall observation.
                while len(_STALL_DIFF_CACHE) > 64:
                    _STALL_DIFF_CACHE.pop(next(iter(_STALL_DIFF_CACHE)), None)

        open_ids: list[str] = []
        threads_observed = False
        if snap is not None:
            threads_observed = True
            open_ids = list(snap.open_thread_ids)
        unresolved = len(open_ids)

        prior_ids: set[str] = set()
        raw_prior = sess.get("stall_open_threads")
        if raw_prior:
            try:
                loaded = json.loads(str(raw_prior))
                if isinstance(loaded, list):
                    prior_ids = {str(x) for x in loaded}
            except json.JSONDecodeError:
                prior_ids = set()
        current_ids = set(open_ids)
        # Delta only once we have a prior snapshot. First observed round has
        # prior_ids empty → threads_resolved=0 by design (PR #29 NB3): absence
        # of a previous snapshot is not evidence that anything was resolved.
        # At threshold 3 that only means the counter can sit at 1 after the
        # first real observation — not a false escalate.
        if threads_observed and prior_ids:
            threads_resolved = len(prior_ids - current_ids)
        else:
            threads_resolved = 0

        reason: str | None = None
        # Fingerprint only when we have real progress material (§9.3).
        if threads_observed and diff_stat is not None:
            fp = progress_fingerprint(
                open_thread_ids=open_ids,
                unresolved_count=unresolved,
                diff_stat=diff_stat,
            )
            reason = stall.observe_fingerprint(fp, str(head) if head else None)
        else:
            log.warning(
                "stall fingerprint skipped session=%s threads_ok=%s diff_ok=%s "
                "(no placeholder hash — would be a blind countdown)",
                session_key,
                threads_observed,
                diff_stat is not None,
            )

        # Zero-thread: only on CHANGES_REQUESTED, only when we observed threads.
        if not reason and kind == "design_changes_requested":
            if threads_observed:
                reason = stall.observe_review_round(
                    threads_resolved=threads_resolved
                )
            else:
                log.warning(
                    "stall zero-thread skipped session=%s: no reviewThreads snapshot",
                    session_key,
                )

        if reason:
            self._escalate(session_key, "system", reason)
            self.store.set_delivery_status(delivery_id, "done")
            return True

        fp_store = (
            f"A:{stall.last_fp}" if stall.seen_head_change else (stall.last_fp or "")
        )
        fields_stall: dict[str, Any] = {
            "progress_fp": fp_store,
            "progress_repeat": stall.fp_repeat,
            "zero_thread_rounds": stall.zero_thread_rounds,
        }
        if threads_observed:
            fields_stall["stall_open_threads"] = json.dumps(sorted(current_ids))
        if kind == "design_changes_requested":
            fields_stall["review_rounds"] = int(sess.get("review_rounds") or 0) + 1
        self.store.update_session_fields(session_key, **fields_stall)
        return False

    def _gateway_github_token(self) -> str | None:
        """Gateway voice only — never an agent PAT (PR #27 B2 / ADR-11).

        Keychain account ``gateway`` (service ``agentd``). Env override:
        ``AGENTD_SECRET_GATEWAY``. No fallback to claude-bot / grok-bot.
        """
        if self._gateway_token is not None:
            # Explicit inject (tests may pass "" to force failure).
            return self._gateway_token or None
        return get_password("gateway")

    def _ensure_verification_block(
        self,
        *,
        repo: str,
        issue_num: int,
        design_pr: Any,
        feature_pr: Any,
    ) -> None:
        """Upsert §10.1 verification block into the session issue body (#50).

        Failure is logged only — merge already advanced; missing block is the
        pre-M5-0 world and must not roll back AWAITING_VERIFICATION.
        """
        token = self._gateway_github_token()
        if not token:
            log.warning(
                "verification block skipped issue=%s: no gateway token",
                issue_num,
            )
            return
        prs: list[int | str] = []
        for n in (design_pr, feature_pr):
            if n is None or n == "":
                continue
            prs.append(n)
        steps = default_steps_for_session(design_pr=design_pr, feature_pr=feature_pr)
        try:
            get_fn = self._get_issue_body or get_issue_body
            patch_fn = self._patch_issue_body or patch_issue_body
            current = get_fn(repo=repo, issue_num=int(issue_num), token=token)
            # Refresh Merged PRs / steps but keep an already-ticked box.
            new_body = upsert_verification_block(
                current,
                steps=steps,
                merged_prs=prs,
                not_covered=None,
                preserve_checkbox=True,
            )
            if new_body == current and extract_verification_block(current):
                log.info(
                    "verification block already present issue=%s",
                    issue_num,
                )
                return
            patch_fn(
                repo=repo,
                issue_num=int(issue_num),
                body=new_body,
                token=token,
            )
            log.info(
                "verification block written issue=%s design_pr=%s feature_pr=%s",
                issue_num,
                design_pr,
                feature_pr,
            )
        except Exception as exc:  # noqa: BLE001 — structural best-effort
            log.error(
                "verification block write failed issue=%s: %s",
                issue_num,
                exc,
            )

    def _handle_structural_refusal(
        self,
        *,
        session_key: str,
        repo: str,
        issue_num: int,
        delivery_id: str,
        reason: str,
        architect: str,
        developer: str,
    ) -> None:
        """§8.5 once per project for layout/image/allowlist failure (#35).

        Shape: create the issue session in PAUSED_HUMAN (no runner endpoint),
        post one gateway escalation, open project_blocks so ensure_session
        short-circuits without re-running worktree add, mark delivery done.
        """
        project_key = project_key_from_repo(repo)
        now = int(time.time())
        sess = self.store.get_session(session_key)
        if not sess:
            self.store.upsert_session(
                session_key=session_key,
                project_key=project_key,
                repo=repo,
                issue_num=int(issue_num),
                state="PAUSED_HUMAN",
                architect=architect,
                developer=developer,
                created_at=now,
                updated_at=now,
                paused_reason=reason[:500],
            )
            self.store.update_session_fields(
                session_key,
                resume_state="PLANNING",
            )
        else:
            self.store.update_session_fields(
                session_key,
                state="PAUSED_HUMAN",
                paused_reason=reason[:500],
                resume_state=str(sess.get("state") or "PLANNING"),
            )

        block = self.store.get_open_project_block(project_key)
        if block:
            log.warning(
                "structural refusal (project already blocked) project=%s session=%s: %s",
                project_key,
                session_key,
                reason,
            )
            self.store.set_delivery_status(delivery_id, "done")
            return

        # First structural failure for this project — escalate once.
        self._escalate(session_key, "system", reason)
        esc = self.store.get_open_escalation(session_key)
        comment_id = esc.get("comment_id") if esc else None
        self.store.open_project_block(
            project_key=project_key,
            reason=reason,
            session_key=session_key,
            issue_num=int(issue_num),
            comment_id=int(comment_id) if comment_id is not None else None,
        )
        self.store.set_delivery_status(delivery_id, "done")
        log.error(
            "structural refusal project=%s session=%s — escalated, delivery done: %s",
            project_key,
            session_key,
            reason,
        )

    def _handle_session_issue_edited(
        self,
        *,
        session_key: str,
        sess: dict[str, Any],
        repo: str,
        issue_num: int,
        delivery_id: str,
        sender: str,
        data: dict[str, Any],
    ) -> None:
        """§10.2: record owner checkbox flip; restore agent tampering (M5-1).

        Classification at issues.closed (M5-2/3) must read the checkbox from the
        **closed payload's issue.body** — GitHub is source of truth (P1).
        ``verified_at`` is only an audit record of when this gateway observed a
        valid owner tick; it is never the authority for restore or classification.

        Agent restore compares checkbox state **across this edit** via
        ``changes.body.from`` (PR #65 B1). Local ``verified_at`` is not used for
        restore: a missed delivery while the daemon is down must not let a later
        agent step-refine untick a real owner mark.

        Gateway body edits are ignored (#64 Architect note): the gateway is a
        bot but not an agent, and it authors the verification scaffold.
        """
        changes = data.get("changes") if isinstance(data.get("changes"), dict) else {}
        if "body" not in changes:
            # Title/label/etc. — not a checkbox concern.
            self.store.set_delivery_status(delivery_id, "done")
            return

        issue = data.get("issue") if isinstance(data.get("issue"), dict) else {}
        body = issue.get("body") if isinstance(issue.get("body"), str) else ""
        state = str(sess.get("state") or "")
        owner = str(self.config.owner or "")
        sender_l = (sender or "").lower()
        owner_l = owner.lower()

        # Agents = session roles + configured agent identities. Not gateway.
        agent_ids = {
            str(sess.get("architect") or "").lower(),
            str(sess.get("developer") or "").lower(),
        } | {a.lower() for a in self.config.agent_logins() if a}
        agent_ids.discard("")
        gw = self.config.gateway_login
        gw_l = gw.lower() if gw else ""

        if gw_l and sender_l == gw_l:
            log.info(
                "issues.edited by gateway session=%s — ignore (§10.1 scaffold)",
                session_key,
            )
            self.store.set_delivery_status(delivery_id, "done")
            return

        is_agent = sender_l in agent_ids
        is_owner = sender_l == owner_l and bool(owner_l)
        have_checked = checkbox_is_checked(body)
        already_recorded = bool(sess.get("verified_at"))

        # Agent body edit while AWAITING_VERIFICATION → restore only if *this*
        # edit changed the checkbox / removed the block (B1/B2).
        if is_agent and state == "AWAITING_VERIFICATION":
            self._restore_agent_verification_edit(
                session_key=session_key,
                repo=repo,
                issue_num=int(issue_num),
                delivery_id=delivery_id,
                sender=sender,
                owner=owner,
                body=body,
                changes=changes,
            )
            return

        # Record flip only: owner AND not an agent identity (§10.2).
        if is_owner and not is_agent:
            if have_checked is True and not already_recorded:
                self.store.update_session_fields(
                    session_key, verified_at=int(time.time())
                )
                log.info(
                    "verification checkbox recorded session=%s sender=%s",
                    session_key,
                    sender,
                )
            elif have_checked is False and already_recorded:
                self.store.update_session_fields(session_key, verified_at=None)
                log.info(
                    "verification checkbox cleared session=%s sender=%s",
                    session_key,
                    sender,
                )
            else:
                log.info(
                    "issues.edited by owner session=%s checked=%s verified_at=%s",
                    session_key,
                    have_checked,
                    sess.get("verified_at"),
                )
        elif is_owner and is_agent:
            log.warning(
                "issues.edited: owner login %s is also an agent identity — "
                "checkbox flip NOT recorded (§10.2 second test) session=%s",
                sender,
                session_key,
            )
        else:
            log.info(
                "issues.edited session=%s sender=%s — no checkbox record "
                "(not owner, or not agent-restore path)",
                session_key,
                sender,
            )

        self.store.set_delivery_status(delivery_id, "done")

    def _restore_agent_verification_edit(
        self,
        *,
        session_key: str,
        repo: str,
        issue_num: int,
        delivery_id: str,
        sender: str,
        owner: str,
        body: str,
        changes: dict[str, Any],
    ) -> None:
        """Restore checkbox/block using changes.body.from (PR #65 B1/B2)."""
        body_change = changes.get("body")
        prev: str | None = None
        if isinstance(body_change, dict):
            raw_from = body_change.get("from")
            if isinstance(raw_from, str):
                prev = raw_from

        if prev is None:
            # Missing before-image: do nothing. A missed restore costs one
            # re-tick; a wrong restore can ABANDON a verified session (B1).
            log.warning(
                "issues.edited by agent session=%s — no changes.body.from; "
                "skip restore (safe default)",
                session_key,
            )
            self.store.set_delivery_status(delivery_id, "done")
            return

        was = checkbox_is_checked(prev)
        now = checkbox_is_checked(body)
        prev_block = extract_verification_block(prev)
        curr_block = extract_verification_block(body)

        restored: str | None = None
        reason = ""

        if prev_block is not None and curr_block is None:
            # B2: whole block deleted — re-splice prior block; keep agent prose.
            restored = reinsert_verification_block(body, prev_block)
            reason = "verification block removed"
        elif prev_block is None:
            log.info(
                "issues.edited by agent session=%s — no prior block in "
                "changes.body.from; nothing to restore",
                session_key,
            )
            self.store.set_delivery_status(delivery_id, "done")
            return
        elif was is not None and now is not None and now != was:
            # Checkbox flipped by this edit — restore to pre-edit value.
            restored = set_checkbox_in_body(body, checked=was)
            reason = f"checkbox {was} → {now}"
        elif was is not None and now is None:
            # Line removed inside an otherwise-present block.
            restored = set_checkbox_in_body(body, checked=was)
            reason = "checkbox line removed"
        else:
            log.info(
                "issues.edited by agent session=%s — checkbox unchanged across "
                "edit (steps refine OK) was=%s now=%s",
                session_key,
                was,
                now,
            )
            self.store.set_delivery_status(delivery_id, "done")
            return

        if restored is None or restored == body:
            self.store.set_delivery_status(delivery_id, "done")
            return

        try:
            patch_fn = self._patch_issue_body or patch_issue_body
            patch_fn(
                repo=repo,
                issue_num=int(issue_num),
                body=restored,
                token=self._gateway_github_token(),
            )
        except Exception as exc:  # noqa: BLE001
            log.error("checkbox restore failed session=%s: %s", session_key, exc)

        owner_tag = f"@{owner}" if owner else "the owner"
        restore_state = checkbox_is_checked(restored)
        state_label = (
            "checked"
            if restore_state is True
            else ("unchecked" if restore_state is False else "block restored")
        )
        warn = (
            f"**agentd** restored the §10.1 verification "
            f"{'block' if 'block' in reason else 'checkbox'} after an agent "
            f"body edit while `AWAITING_VERIFICATION` (§10.2).\n\n"
            f"Sender: `{sender}`\n"
            f"Reason: {reason}\n"
            f"Restored to: `{state_label}` (pre-edit body via "
            f"`changes.body.from` — not local `verified_at`).\n\n"
            f"Only {owner_tag} may tick the box — and only when that "
            f"login is not also a configured agent identity."
        )
        try:
            if self._post_comment is not None:
                self._post_comment(
                    repo=repo,
                    issue_num=int(issue_num),
                    body=warn,
                    token=self._gateway_github_token(),
                )
            else:
                post_issue_comment(
                    repo=repo,
                    issue_num=int(issue_num),
                    body=warn,
                    token=self._gateway_github_token(),
                )
        except Exception as exc:  # noqa: BLE001
            log.error(
                "checkbox warning comment failed session=%s: %s",
                session_key,
                exc,
            )
        log.info(
            "issues.edited agent restore session=%s reason=%s restored=%s",
            session_key,
            reason,
            state_label,
        )
        self.store.set_delivery_status(delivery_id, "done")

    def _handle_session_issue_closed(
        self,
        *,
        session_key: str,
        sess: dict[str, Any],
        repo: str,
        issue_num: int,
        delivery_id: str,
        sender: str,
        bot_logins: set[str],
    ) -> None:
        """§10.3: session issue closed — owner only; agent close → reopen + escalate.

        Choice (stated for #36): **reopen** non-owner closes. Leaving the issue
        closed would make the session's terminal GitHub state a lie and would
        still be the teardown trigger once M5 lands. Reopen restores truth;
        §8.5 tells the owner the verification gate was bypassed.
        """
        owner = str(self.config.owner or "")
        sender_l = sender.lower()
        owner_l = owner.lower()
        bots_l = {b.lower() for b in bot_logins if b}

        if sender_l == owner_l:
            # Legitimate terminal signal. M5 will run teardown on this path;
            # until then record and do not dispatch an agent "cleanup" turn.
            log.info(
                "issues.closed by owner session=%s issue=%s — human gate OK "
                "(teardown reserved for M5)",
                session_key,
                issue_num,
            )
            self.store.set_delivery_status(delivery_id, "done")
            return

        # Agent / gateway / other non-owner closed a session issue.
        who = "agent" if sender_l in bots_l else "non-owner"
        reason = (
            f"§10.3 violation: {who} `{sender}` closed session issue "
            f"`{session_key}`. Only `@{owner}` may close a session issue "
            f"(issues.closed is the sole teardown trigger). "
            f"Gateway reopened the issue; no teardown. "
            f"Reply after confirming work, or leave paused."
        )
        log.error(
            "issues.closed by %s=%s session=%s — reopening and escalating",
            who,
            sender,
            session_key,
        )
        try:
            if self._reopen_issue is not None:
                self._reopen_issue(
                    repo=repo,
                    issue_num=int(issue_num),
                    token=self._gateway_github_token(),
                )
            else:
                reopen_issue(
                    repo=repo,
                    issue_num=int(issue_num),
                    token=self._gateway_github_token(),
                )
        except Exception as exc:  # noqa: BLE001
            log.exception(
                "reopen failed session=%s: %s — still escalating",
                session_key,
                exc,
            )
            reason = f"{reason} [reopen_failed: {exc}]"

        state = str(sess.get("state") or "PLANNING")
        paused = state == "PAUSED_HUMAN" or bool(sess.get("paused_reason"))
        if not paused:
            self._escalate(session_key, "system", reason)
        else:
            log.warning(
                "session already paused; reopen done without re-escalating session=%s",
                session_key,
            )
        self.store.set_delivery_status(delivery_id, "done")

    def _escalate(self, session_key: str, role: str, reason: str) -> None:
        """§8.5: pause, post @owner comment, record escalation with comment_id."""
        sess = self.store.get_session(session_key) or {}
        prev_state = str(sess.get("state") or "PLANNING")
        if prev_state == "PAUSED_HUMAN":
            prev_state = str(sess.get("resume_state") or "PLANNING")
        repo = str(sess.get("repo") or "")
        issue_num = sess.get("issue_num")

        comment_id: int | None = None
        body = format_escalation_comment(
            owner=self.config.owner,
            session_key=session_key,
            state=prev_state,
            role=role,
            reason=reason,
        )
        try:
            if self._post_comment is not None:
                comment_id = int(
                    self._post_comment(
                        repo=repo,
                        issue_num=int(issue_num or 0),
                        body=body,
                        token=self._gateway_github_token(),
                    )
                )
            else:
                comment_id = post_issue_comment(
                    repo=repo,
                    issue_num=int(issue_num or 0),
                    body=body,
                    token=self._gateway_github_token(),
                )
        except Exception as exc:  # noqa: BLE001
            # Still pause — never silent about the failure (P5).
            log.exception(
                "escalation comment failed session=%s: %s — session still paused",
                session_key,
                exc,
            )
            reason = f"{reason} [comment_post_failed: {exc}]"

        self.store.open_escalation(
            session_key=session_key,
            role=role,
            reason=reason,
            comment_id=comment_id,
        )
        self.store.update_session_fields(
            session_key,
            state="PAUSED_HUMAN",
            paused_reason=reason,
            resume_state=prev_state,
        )
        log.warning(
            "escalation session=%s reason=%s comment_id=%s resume=%s",
            session_key,
            reason,
            comment_id,
            prev_state,
        )

    def _resume_from_escalation(
        self,
        *,
        session_key: str,
        sess: dict[str, Any],
        dig: dict[str, Any],
        data: dict[str, Any],
        delivery_id: str,
        issue_num: int,
    ) -> None:
        """Owner issue_comment while PAUSED_HUMAN → close escalation, dispatch."""
        open_esc = self.store.get_open_escalation(session_key)
        resume_state = str(sess.get("resume_state") or "PLANNING")
        if resume_state == "PAUSED_HUMAN":
            resume_state = "PLANNING"
        esc_role = str((open_esc or {}).get("role") or "architect")
        if esc_role == "system":
            esc_role = "architect"
        if esc_role not in ("architect", "developer"):
            esc_role = "architect"

        comment_body = ""
        if isinstance(data.get("comment"), dict):
            comment_body = str(data["comment"].get("body") or "")
        dig = dict(dig)
        dig["kind"] = "owner_reply"
        dig["owner_reply"] = comment_body[:8000]
        dig["escalation_reason"] = str(
            (open_esc or {}).get("reason") or sess.get("paused_reason") or ""
        )
        dig["resumed_from"] = "PAUSED_HUMAN"
        dig["resume_state"] = resume_state

        n = self.store.close_escalation(session_key)
        project_key = str(
            sess.get("project_key") or project_key_from_repo(str(sess.get("repo") or ""))
        )
        if project_key:
            self.store.close_project_block(project_key)
        self.store.update_session_fields(
            session_key,
            state=resume_state,
            paused_reason=None,
            resume_state=None,
            consec_agent_turns=0,
            silent_turns=0,  # owner engagement resets dead-end counter (#39)
        )
        log.info(
            "escalation closed session=%s rows=%s → %s (owner reply)",
            session_key,
            n,
            resume_state,
        )

        # Structural pause left no runner — retry ensure_session after owner
        # fixed the host (project_block cleared above) (#35).
        if self.supervisor and not sess.get("endpoint"):
            try:
                self.supervisor.ensure_session(
                    session_key=session_key,
                    repo=str(sess.get("repo") or ""),
                    issue_num=int(issue_num),
                    architect_login=str(sess.get("architect") or "") or None,
                    developer_login=str(sess.get("developer") or "") or None,
                )
                sess = self.store.get_session(session_key) or sess
            except CapacityRefusal as e:
                log.warning(
                    "ensure_session deferred on resume (capacity) %s: %s",
                    session_key,
                    e,
                )
                self.store.set_delivery_status(delivery_id, "deferred")
                return
            except StructuralRefusal as e:
                # Re-wedge: escalate again (new block).
                self._handle_structural_refusal(
                    session_key=session_key,
                    repo=str(sess.get("repo") or ""),
                    issue_num=int(issue_num),
                    delivery_id=delivery_id,
                    reason=str(e),
                    architect=str(sess.get("architect") or "huozheclaude"),
                    developer=str(sess.get("developer") or "huozhegrok"),
                )
                return
            except Exception:
                log.exception("ensure_session on resume failed %s", session_key)
                self.store.set_delivery_status(delivery_id, "deferred")
                return

        if self.dispatch_turns and self.supervisor and sess.get("endpoint"):
            turn_id = "t-" + uuid.uuid4().hex[:12]
            turn_result = self._dispatch_turn(
                session_key=session_key,
                role=esc_role,
                turn_id=turn_id,
                delivery_id=delivery_id,
                dig=dig,
                issue_num=issue_num,
            )
            status = (turn_result or {}).get("status")
            if status == "role_busy":
                return
            if status != "gateway_timeout":
                budget = BudgetState(
                    turn_count=int(sess.get("turn_count") or 0),
                    consec_agent_turns=0,
                    review_rounds=int(sess.get("review_rounds") or 0),
                )
                budget.after_agent_turn()
                # Owner unpause is observed progress; silent counter stays 0.
                silent = SilentTurnTracker(
                    silent_count=0,
                    threshold=int(self.config.silent_turn_limit),
                )
                actions = (turn_result or {}).get("public_actions") or []
                if not isinstance(actions, list):
                    actions = []
                breach = silent.after_turn(
                    public_actions=actions,
                    observed_progress=True,
                    status=str(status) if status else None,
                )
                self.store.update_session_fields(
                    session_key,
                    turn_count=budget.turn_count,
                    consec_agent_turns=budget.consec_agent_turns,
                    silent_turns=silent.silent_count,
                )
                if breach:
                    self._escalate(session_key, "system", breach)
                    self.store.set_delivery_status(delivery_id, "done")
                    return

        self.store.set_delivery_status(delivery_id, "routed")
        log.info(
            "delivery routed id=%s session=%s role=%s kind=owner_reply (resume)",
            delivery_id,
            session_key,
            esc_role,
        )

    def _resolve_session_for_delivery(
        self,
        *,
        event: str,
        repo: str,
        issue_num: int,
        data: dict[str, Any],
    ) -> tuple[int, str, dict[str, Any] | None]:
        """Map delivery → (issue_num, session_key, session_or_None).

        1. Supervisor branch ``agentd/<proj>/<issue>/<role>`` (head.ref)
        2. ``sessions.design_pr == PR number`` for PR-keyed comments/reviews
        3. Fallback: treat issue_num as the issue session key
        """
        pr = data.get("pull_request") if isinstance(data.get("pull_request"), dict) else {}
        head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
        head_ref = head.get("ref") if head else None

        # PR-keyed events nest pull_request (incl. review threads, #49).
        if event in (
            "pull_request",
            "pull_request_review",
            "pull_request_review_comment",
            "pull_request_review_thread",
        ):
            parsed = parse_role_branch(str(head_ref) if head_ref else None, repo)
            if parsed is not None:
                issue_num = parsed[0]
                sk = f"{repo}#{int(issue_num)}"
                return issue_num, sk, self.store.get_session(sk)

        # issue_comment on a PR: payload.issue.pull_request is present and
        # issue.number is the PR number (not the agentd issue).
        pr_number: int | None = None
        if event == "issue_comment":
            issue = data.get("issue") if isinstance(data.get("issue"), dict) else {}
            if isinstance(issue.get("pull_request"), dict):
                try:
                    pr_number = int(issue.get("number"))
                except (TypeError, ValueError):
                    pr_number = None
        elif event in (
            "pull_request",
            "pull_request_review",
            "pull_request_review_comment",
            "pull_request_review_thread",
        ):
            try:
                pr_number = int(pr.get("number") or issue_num)
            except (TypeError, ValueError):
                pr_number = int(issue_num)

        if pr_number is not None:
            # Direct session key may already match (issue == pr number — rare).
            sk = f"{repo}#{int(issue_num)}"
            sess = self.store.get_session(sk)
            if sess is not None:
                return int(issue_num), sk, sess
            by_design = self.store.get_session_by_design_pr(repo, pr_number)
            if by_design is not None:
                real_issue = int(by_design["issue_num"])
                log.info(
                    "session remap via design_pr=%s → %s (event=%s)",
                    pr_number,
                    by_design["session_key"],
                    event,
                )
                return real_issue, str(by_design["session_key"]), by_design
            by_feature = self.store.get_session_by_feature_pr(repo, pr_number)
            if by_feature is not None:
                real_issue = int(by_feature["issue_num"])
                log.info(
                    "session remap via feature_pr=%s → %s (event=%s)",
                    pr_number,
                    by_feature["session_key"],
                    event,
                )
                return real_issue, str(by_feature["session_key"]), by_feature

        sk = f"{repo}#{int(issue_num)}"
        return int(issue_num), sk, self.store.get_session(sk)

    def _is_design_pr(
        self,
        *,
        repo: str,
        pr: dict[str, Any],
        issue_num: int | None = None,
    ) -> bool:
        """Mechanical Design PR detection (M3-D B1).

        Primary: ``head.ref`` is the supervisor Architect branch
        ``agentd/<owner__repo>/<issue>/architect``. Title heuristic is fallback
        only — model-written titles are not a control plane signal (§9.1).
        """
        head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
        head_ref = str(head.get("ref") or "")
        title = str(pr.get("title") or "")
        title_hit = "design" in title.lower() or "rfc" in title.lower()
        branch_hit = is_design_head_ref(head_ref, repo)
        # If head_ref is an agentd role branch for developer, never treat as design.
        parsed = parse_role_branch(head_ref, repo)
        if parsed is not None and parsed[1] == "developer":
            if title_hit:
                log.warning(
                    "design title on developer branch head_ref=%s title=%r — treating as feature",
                    head_ref,
                    title,
                )
            return False
        if branch_hit:
            if not title_hit:
                log.info(
                    "design PR by branch head_ref=%s (title has no design/rfc: %r)",
                    head_ref,
                    title,
                )
            return True
        if title_hit:
            log.warning(
                "design PR by title fallback only head_ref=%s title=%r "
                "(expected branch agentd/%s/<issue>/architect)",
                head_ref,
                title,
                project_dir_name(repo),
            )
            return True
        return False

    def _is_feature_pr(
        self,
        *,
        repo: str,
        pr: dict[str, Any],
    ) -> bool:
        """Mechanical Feature PR detection (M4-1) — developer branch only (#31).

        No title heuristic: model-written titles are not a control-plane signal.
        """
        head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
        head_ref = str(head.get("ref") or "")
        return is_feature_head_ref(head_ref, repo)

    def _event_kind(
        self,
        event: str,
        action: str | None,
        data: dict[str, Any],
        sender: str,
        *,
        repo: str = "",
    ) -> str:
        if event == "issues" and action in ("opened", "reopened", "labeled"):
            return "issue_opened"
        if event == "pull_request":
            pr = data.get("pull_request") or {}
            is_design = self._is_design_pr(repo=repo, pr=pr)
            is_feature = self._is_feature_pr(repo=repo, pr=pr)
            if action == "opened" and is_design:
                return "design_pr_opened"
            if action == "opened" and is_feature:
                return "feature_pr_opened"
            if action == "synchronize" and is_design:
                return "design_revised"
            if action == "synchronize" and is_feature:
                return "feature_revised"
            # Merge; session design_pr / feature_pr match is enforced later.
            if action == "closed" and pr.get("merged") and is_design:
                return "design_merged"
            if action == "closed" and pr.get("merged") and is_feature:
                return "feature_merged"
        if event == "pull_request_review":
            review = data.get("review") or {}
            pr = data.get("pull_request") or {}
            st = str(review.get("state") or "").upper()
            is_feature = self._is_feature_pr(repo=repo, pr=pr)
            is_design = self._is_design_pr(repo=repo, pr=pr)
            if st == "CHANGES_REQUESTED":
                if is_feature:
                    return "code_changes_requested"
                if is_design:
                    return "design_changes_requested"
            if st == "APPROVED":
                if is_feature:
                    # Unverified until full §8.4 (M4-2); kind is diagnostic until then.
                    return "feature_approved_unverified"
                if is_design:
                    # Unverified until §8.4 check runs (design path)
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
        # Happy path (§8.2 / §8.3 / §8.4):
        #   issue → architect drafts Design PR
        #   design_pr_opened / revised → developer reviews
        #   design_approved → architect merges (merge actor is Architect)
        #   design_merged → developer implements (Architect merge is self-echo;
        #     routing architect here left sessions stuck in IMPLEMENTING — M4-4)
        #   feature_pr_opened / revised → architect reviews
        #   code_changes_requested → developer fixes
        #   merge_authorized → developer merges (opposite actor from §8.3)
        #   feature_merged → architect (verification block / idle)
        if kind in (
            "issue_opened",
            "design_changes_requested",
            "design_approved",
            "merge_design",
            "feature_pr_opened",
            "feature_revised",
            "feature_merged",
        ):
            return "architect", architect
        if kind in (
            "design_pr_opened",
            "design_revised",
            "design_merged",
            "code_changes_requested",
            "merge_authorized",
            "feature_approved_unverified",
        ):
            return "developer", developer
        # Default: route to the role that is not the sender bot
        if sender.lower() == architect.lower():
            return "developer", developer
        return "architect", architect
