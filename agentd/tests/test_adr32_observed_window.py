"""ADR-32: loop-safety asked of the turn, not of the triggering delivery.

Acceptance (1)-(9). (10) is live sign-off and (11) is owner data repair; both
are recorded in the ADR rather than tested here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agentd.config import Config
from agentd.db import Store
from agentd.design_loop import DesignLoop
from agentd.fsm import TERMINAL_STATES
from agentd.gitops import role_branch_name
from agentd.verification import render_verification_block

REPO = "huozhe/code-workflow"
ISSUE = 63
DESIGN_PR = 148
FEATURE_PR = 149


def _cfg(tmp: Path) -> Config:
    return Config(
        raw={
            "host": {"owner": "huozhe"},
            "gateway": {"login": "huozhegateway"},
            "budgets": {"silent_turn_limit": 3},
            "agents": {
                "claude": {"login": "huozheclaude"},
                "grok": {"login": "huozhegrok"},
            },
        },
        root=tmp,
    )


def _seed(
    store: Store,
    *,
    state: str = "CODE_REVIEW",
    silent_turns: int = 0,
    paused_reason: str | None = None,
) -> str:
    sk = f"{REPO}#{ISSUE}"
    store.upsert_session(
        session_key=sk,
        project_key=REPO,
        repo=REPO,
        issue_num=ISSUE,
        state=state,
        architect="huozheclaude",
        developer="huozhegrok",
        created_at=1,
        updated_at=1,
        design_pr=DESIGN_PR,
    )
    fields: dict[str, Any] = {"feature_pr": FEATURE_PR}
    if silent_turns:
        fields["silent_turns"] = silent_turns
    if paused_reason is not None:
        fields["paused_reason"] = paused_reason
    store.update_session_fields(sk, **fields)
    return sk


def _received_at(store: Store, delivery_id: str) -> int:
    row = store._conn.execute(
        "SELECT received_at FROM deliveries WHERE delivery_id = ?", (delivery_id,)
    ).fetchone()
    assert row is not None
    return int(row["received_at"])


def _comment_payload(*, sender: str, number: int, author: str) -> dict[str, Any]:
    """issue_comment on a PR — the t-9e31cfd2f1f1 trigger shape.

    ``author`` is the PR author: ADR-30 drops author-sent PR events before the
    kind is even computed, so the commenter must be the counterpart.
    """
    return {
        "action": "created",
        "issue": {
            "number": number,
            "pull_request": {"url": f"https://api.github.com/pulls/{number}"},
            "user": {"login": author},
        },
        "comment": {"body": "looking at it", "user": {"login": sender}},
        "sender": {"login": sender},
        "repository": {"full_name": REPO},
    }


def _feature_sync_payload(*, sender: str = "huozhegrok") -> dict[str, Any]:
    return {
        "action": "synchronize",
        "pull_request": {
            "number": FEATURE_PR,
            "user": {"login": "huozhegrok"},
            "head": {"sha": "def", "ref": role_branch_name(REPO, ISSUE, "developer")},
            "merged": False,
        },
        "sender": {"login": sender},
        "repository": {"full_name": REPO},
    }


def _design_open_payload() -> dict[str, Any]:
    return {
        "action": "opened",
        "pull_request": {
            "number": DESIGN_PR,
            "title": "Design: x",
            "user": {"login": "huozheclaude"},
            "head": {"sha": "abc", "ref": role_branch_name(REPO, ISSUE, "architect")},
            "merged": False,
        },
        "sender": {"login": "huozheclaude"},
        "repository": {"full_name": REPO},
    }


def _insert(
    store: Store,
    *,
    did: str,
    event: str,
    action: str | None,
    sender: str,
    issue: int,
    payload: dict[str, Any],
    status: str = "deferred",
) -> None:
    store.insert_delivery(
        delivery_id=did,
        event=event,
        action=action,
        repo=REPO,
        issue_num=issue,
        sender=sender,
        payload=json.dumps(payload).encode(),
        status=status,
    )


def _loop(
    store: Store,
    tmp: Path,
    *,
    plant: dict[str, Any] | None = None,
    turn_len: int = 98,
    public_actions: list | None = None,
) -> DesignLoop:
    """DesignLoop whose dispatch stub writes a real turn row.

    ``started_at`` is set to the **triggering delivery's own** ``received_at``:
    both columns are whole seconds and they collide in practice (live turns
    ``t-a9044e280f21`` and ``t-797b18feee34``). Acceptance (3) needs that
    collision, so every test here has it.
    """
    loop = DesignLoop(
        store,
        _cfg(tmp),
        supervisor=object(),
        dispatch_turns=True,
        github_token="tok",
        fetch_threads=lambda **k: None,
        fetch_diff=lambda **k: None,
    )
    dispatched: list[str] = []

    def _spy(
        *,
        session_key: str,
        role: str,
        turn_id: str,
        delivery_id: str,
        dig: dict[str, Any],
        issue_num: int,
    ) -> dict[str, Any]:
        started = _received_at(store, delivery_id)
        store.insert_turn(
            turn_id=turn_id,
            session_key=session_key,
            role=role,
            delivery_id=delivery_id,
            started_at=started,
            ended_at=None,
            status=None,
            summary=None,
        )
        if plant is not None:
            # The webhook this turn produced, in the table while the drain is
            # still blocked here (ingress is accept-and-queue, ADR-10).
            store.insert_delivery(
                delivery_id=f"{turn_id}-mid",
                event=plant["event"],
                action=plant["action"],
                repo=REPO,
                issue_num=plant["issue"],
                sender=plant["sender"],
                payload=json.dumps(plant["payload"]).encode(),
                status="queued",
            )
            store._conn.execute(
                "UPDATE deliveries SET received_at = ? WHERE delivery_id = ?",
                (started + plant.get("offset", 91), f"{turn_id}-mid"),
            )
            store._conn.commit()
        store.finish_turn(
            turn_id,
            ended_at=started + turn_len,
            status="done",
            summary="ok",
            public_actions="[]",
        )
        dispatched.append(turn_id)
        return {
            "status": "done",
            "summary": "ok",
            "public_actions": (
                [{"kind": "comment"}] if public_actions is None else public_actions
            ),
        }

    loop._dispatch_turn = _spy  # type: ignore[method-assign]
    loop.dispatched = dispatched  # type: ignore[attr-defined]
    return loop


_MID_FEATURE_SYNC = {
    "event": "pull_request",
    "action": "synchronize",
    "issue": FEATURE_PR,
    "sender": "huozhegrok",
    "payload": _feature_sync_payload(),
    "offset": 91,
}


def _drive(loop: DesignLoop, store: Store, *, did: str, sender: str) -> None:
    _insert(
        store,
        did=did,
        event="issue_comment",
        action="created",
        sender=sender,
        issue=FEATURE_PR,
        payload=_comment_payload(sender=sender, number=FEATURE_PR, author="huozhegrok"),
    )
    loop.process_deferred_batch(limit=1)


def test_mid_turn_progress_webhook_resets_silent_turns(tmp_path: Path) -> None:
    """(1) A turn that produces a progress webhook mid-flight resets the counter.

    Reproduces t-9e31cfd2f1f1: issue_comment trigger from a non-owner, the
    session's own feature PR synchronised at +91 s. Asserts the **counter**, not
    the absence of an escalation — the latter passes for two turns regardless.
    Today this is 1.
    """
    store = Store(tmp_path / "state.db")
    sk = _seed(store)
    loop = _loop(store, tmp_path, plant=_MID_FEATURE_SYNC)
    _drive(loop, store, did="d-trigger", sender="huozheclaude")

    assert len(loop.dispatched) == 1  # type: ignore[attr-defined]
    sess = store.get_session(sk)
    assert sess is not None
    assert int(sess["silent_turns"]) == 0
    store.close()


def test_three_productive_turns_do_not_escalate(tmp_path: Path) -> None:
    """(2) The unit of the bug is the run, not the turn.

    (1) alone passes an implementation that resets one turn in three.
    """
    store = Store(tmp_path / "state.db")
    sk = _seed(store)
    loop = _loop(store, tmp_path, plant=_MID_FEATURE_SYNC)
    for i in range(3):
        _drive(loop, store, did=f"d-trigger-{i}", sender="huozheclaude")

    assert len(loop.dispatched) == 3  # type: ignore[attr-defined]
    sess = store.get_session(sk)
    assert sess is not None
    assert int(sess["silent_turns"]) == 0
    assert sess["state"] != "PAUSED_HUMAN"
    assert not sess["paused_reason"]
    store.close()


def test_triggering_delivery_is_not_its_own_progress(tmp_path: Path) -> None:
    """(3) Same shape as (1) with nothing in the window: the counter increments.

    The fixture pins ``received_at == started_at`` (see :func:`_loop`). Without
    that collision an implementation using ``>=`` passes this item, which is the
    exact error it exists to catch.
    """
    store = Store(tmp_path / "state.db")
    sk = _seed(store)
    loop = _loop(store, tmp_path, plant=None)
    _drive(loop, store, did="d-trigger", sender="huozheclaude")

    turn_id = loop.dispatched[0]  # type: ignore[attr-defined]
    turn = store.get_turn(turn_id)
    assert turn is not None
    assert int(turn["started_at"]) == _received_at(store, "d-trigger")

    sess = store.get_session(sk)
    assert sess is not None
    assert int(sess["silent_turns"]) == 1
    store.close()


def test_backlog_delivery_in_the_same_second_is_not_this_turn_s_progress(
    tmp_path: Path,
) -> None:
    """(3), second half: the bound is what excludes work the turn did not do.

    A progress-kind delivery that arrived **before** the turn but in the same
    whole second as ``started_at`` — an ordinary collision, both columns are
    ``int(time.time())`` — must not count. This is the assertion that fails on
    an inclusive lower bound; the first half cannot catch it, because the
    trigger term resets on a progress-kind trigger anyway.
    """
    store = Store(tmp_path / "state.db")
    sk = _seed(store)
    loop = _loop(store, tmp_path, plant=None)
    _insert(
        store,
        did="d-trigger",
        event="issue_comment",
        action="created",
        sender="huozheclaude",
        issue=FEATURE_PR,
        payload=_comment_payload(
            sender="huozheclaude", number=FEATURE_PR, author="huozhegrok"
        ),
    )
    # Backlog: queued, so the drain does not take it, and stamped at the exact
    # second the turn will start.
    _insert(
        store,
        did="d-backlog",
        event="pull_request",
        action="synchronize",
        sender="huozhegrok",
        issue=FEATURE_PR,
        payload=_feature_sync_payload(),
        status="queued",
    )
    started = _received_at(store, "d-trigger")
    store._conn.execute(
        "UPDATE deliveries SET received_at = ? WHERE delivery_id = ?",
        (started, "d-backlog"),
    )
    store._conn.commit()

    loop.process_deferred_batch(limit=1)

    turn = store.get_turn(loop.dispatched[0])  # type: ignore[attr-defined]
    assert turn is not None
    assert int(turn["started_at"]) == _received_at(store, "d-backlog")
    sess = store.get_session(sk)
    assert sess is not None
    assert int(sess["silent_turns"]) == 1
    store.close()


def test_progress_kind_trigger_still_resets(tmp_path: Path) -> None:
    """(4) The trigger term is retained.

    A turn woken by ``design_pr_opened`` with nothing in the window and no state
    change must still reset. Fails against a two-term reading that measures only
    the window and the state, and nothing else in this suite detects that.
    """
    store = Store(tmp_path / "state.db")
    sk = _seed(store, state="DESIGN_REVIEW", silent_turns=2)
    loop = _loop(store, tmp_path, plant=None)
    _insert(
        store,
        did="d-design-open",
        event="pull_request",
        action="opened",
        sender="huozheclaude",
        issue=DESIGN_PR,
        payload=_design_open_payload(),
    )
    loop.process_deferred_batch(limit=1)

    sess = store.get_session(sk)
    assert sess is not None
    assert int(sess["silent_turns"]) == 0
    store.close()


def test_public_actions_never_reset_pr42_b1(tmp_path: Path) -> None:
    """(5) PR #42 B1: claims are diagnostic, never a reset.

    Named for #42 so a later simplification pass does not fold this into the
    observed-progress rule.
    """
    store = Store(tmp_path / "state.db")
    sk = _seed(store, silent_turns=2)
    loop = _loop(
        store,
        tmp_path,
        plant=None,
        public_actions=[{"kind": "api_write"}, {"kind": "comment"}],
    )
    _drive(loop, store, did="d-trigger", sender="huozheclaude")

    sess = store.get_session(sk)
    assert sess is not None
    assert int(sess["silent_turns"]) == 3
    assert sess["state"] == "PAUSED_HUMAN"
    reason = str(sess["paused_reason"])
    assert "api_write" in reason
    # (e): the window was examined and found empty, so the claim is allowed.
    assert "no matching event arrived (P1)" in reason
    store.close()


def test_terminal_session_delivery_is_done_not_deferred(tmp_path: Path) -> None:
    """(6) The shape on disk today: CLOSED with a stale paused_reason.

    Assert the **delivery status** — "no turn dispatched" passes against the bug,
    because a deferred delivery dispatches no turn either.
    """
    store = Store(tmp_path / "state.db")
    _seed(store, state="CLOSED", paused_reason="stall: silent_turns — 3 …")
    loop = _loop(store, tmp_path)
    _drive(loop, store, did="d-closed", sender="huozheclaude")

    assert loop.dispatched == []  # type: ignore[attr-defined]
    assert store.count_by_status().get("done", 0) == 1
    assert store.count_by_status().get("deferred", 0) == 0
    store.close()


def test_terminal_gate_still_runs_for_a_delivery_that_does_not_defer(
    tmp_path: Path,
) -> None:
    """(7) (c) added a check; it did not relocate #85's gate.

    A terminal session with **no** ``paused_reason`` does not take the defer
    branch at all, so only the post-FSM gate can stop it. This fails if (c) is
    implemented by moving that gate into the defer branch rather than adding a
    second one.

    Note on the ADR's wording: (7) is described there as failing on a move
    because a move would read the pre-transition state. It cannot — ``issues``/
    ``closed`` returns from ``_handle_session_issue_closed`` long before the FSM,
    and no other webhook kind transitions a live session into TERMINAL_STATES, so
    the post-FSM state is terminal exactly when the pre-FSM state already was.
    The gate's position is still worth keeping; this is what can be asserted.
    """
    store = Store(tmp_path / "state.db")
    _seed(store, state="CLOSED", paused_reason=None)
    loop = _loop(store, tmp_path)
    _drive(loop, store, did="d-terminal-routed", sender="huozheclaude")

    assert loop.dispatched == []  # type: ignore[attr-defined]
    assert store.count_by_status().get("done", 0) == 1
    assert store.count_by_status().get("deferred", 0) == 0
    store.close()


def _verification_block(*, checked: bool) -> str:
    return render_verification_block(
        steps=["pull", "test"], merged_prs=[FEATURE_PR], checked=checked
    )


def _close_loop(store: Store, tmp: Path) -> DesignLoop:
    return DesignLoop(
        store,
        _cfg(tmp),
        supervisor=None,
        dispatch_turns=False,
        post_comment=lambda **k: 1,
        reopen_issue_fn=lambda **k: None,
        get_issue_fn=lambda **k: {
            "body": _verification_block(checked=True),
            "state": "closed",
        },
        gateway_token="gw",
    )


def _seed_for_close(
    store: Store, tmp: Path, *, issue: int, paused_reason: str | None
) -> str:
    sk = f"{REPO}#{issue}"
    store.upsert_session(
        session_key=sk,
        project_key=REPO,
        repo=REPO,
        issue_num=issue,
        state="AWAITING_VERIFICATION",
        architect="huozheclaude",
        developer="huozhegrok",
        created_at=1,
        updated_at=1,
    )
    store.update_session_fields(sk, verified_at=1)
    if paused_reason is not None:
        store.update_session_fields(sk, paused_reason=paused_reason)
    d = tmp / "projects" / "huozhe__code-workflow" / "sessions" / str(issue)
    d.mkdir(parents=True, exist_ok=True)
    (d / "keep").write_text("x", encoding="utf-8")
    return sk


def _drive_close(store: Store, tmp: Path, *, issue: int, did: str) -> None:
    _insert(
        store,
        did=did,
        event="issues",
        action="closed",
        sender="huozhe",
        issue=issue,
        payload={
            "action": "closed",
            "issue": {
                "number": issue,
                "state": "closed",
                "state_reason": "completed",
                "title": "session work",
                "user": {"login": "huozhe"},
                "body": _verification_block(checked=True),
            },
            "sender": {"login": "huozhe"},
            "repository": {"full_name": REPO},
        },
    )
    _close_loop(store, tmp).process_deferred_batch()


def test_close_clears_paused_reason(tmp_path: Path) -> None:
    """(8) The real close path clears the reason.

    Driven through ``issues.closed`` rather than a direct field write: the
    binding is on the close, and a test that calls ``update_session_fields``
    itself cannot fail when the close is reverted.
    """
    store = Store(tmp_path / "state.db")
    sk = _seed_for_close(
        store, tmp_path, issue=63, paused_reason="stall: silent_turns — 3 …"
    )
    _drive_close(store, tmp_path, issue=63, did="d-close-paused")

    sess = store.get_session(sk)
    assert sess is not None
    assert sess["state"] in TERMINAL_STATES
    assert not sess["paused_reason"]
    store.close()


def test_close_of_never_paused_session_is_unaffected(tmp_path: Path) -> None:
    """(8), second half: a session that was never paused still closes clean."""
    store = Store(tmp_path / "state.db")
    sk = _seed_for_close(store, tmp_path, issue=64, paused_reason=None)
    _drive_close(store, tmp_path, issue=64, did="d-close-clean")

    sess = store.get_session(sk)
    assert sess is not None
    assert sess["state"] in TERMINAL_STATES
    assert not sess["paused_reason"]
    assert sess["classification"] == "VERIFIED"
    store.close()


def test_counter_write_is_one_statement(tmp_path: Path) -> None:
    """(9) Atomicity as a query, not as a race.

    All three counters move in a single ``UPDATE``: two callers holding the same
    stale snapshot cannot lose one. A threading test would be flaky and prove
    less. ``turn_count`` and ``consec_agent_turns`` are included, or (b)'s
    justification — "required before any threading work" — is not met.
    """
    store = Store(tmp_path / "state.db")
    sk = _seed(store, silent_turns=2)

    statements: list[str] = []

    def _trace(sql: str) -> None:
        if "UPDATE sessions" in " ".join(sql.split()):
            statements.append(sql)

    store._conn.set_trace_callback(_trace)
    out = store.bump_turn_counters(sk, silent="inc")
    store._conn.set_trace_callback(None)

    assert len(statements) == 1
    sql = statements[0]
    assert "turn_count = turn_count + 1" in sql
    assert "consec_agent_turns + 1" in sql
    assert "silent_turns + 1" in sql
    assert out == {"turn_count": 1, "consec_agent_turns": 1, "silent_turns": 3}

    # Two callers off the same stale snapshot: neither update is lost.
    store.bump_turn_counters(sk, silent="inc")
    sess = store.get_session(sk)
    assert sess is not None
    assert int(sess["turn_count"]) == 2
    assert int(sess["silent_turns"]) == 4
    store.close()
