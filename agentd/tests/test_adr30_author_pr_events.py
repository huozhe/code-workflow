"""ADR-30 / #173: author-sent PR events are not the counterpart's cue."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import agentd.design_loop as design_loop_mod
from agentd.config import Config
from agentd.db import Store
from agentd.design_loop import DesignLoop
from agentd.fsm import TERMINAL_STATES
from agentd.gitops import role_branch_name

_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "adr30_author_review.json"
)


@pytest.fixture(autouse=True)
def _clear_spent_prs() -> None:
    design_loop_mod._spent_prs.clear()
    yield
    design_loop_mod._spent_prs.clear()


def _cfg(tmp: Path) -> Config:
    return Config(
        raw={
            "host": {"owner": "huozhe"},
            "gateway": {"login": "huozhegateway"},
            "agents": {
                "claude": {"login": "huozheclaude"},
                "grok": {"login": "huozhegrok"},
            },
        },
        root=tmp,
    )


def _design_ref(issue: int) -> str:
    return role_branch_name("huozhe/code-workflow", issue, "architect")


def _seed(
    store: Store,
    *,
    issue: int = 169,
    pr: int = 170,
    state: str = "DESIGN_REVIEW",
    silent_turns: int = 0,
) -> str:
    sk = f"huozhe/code-workflow#{issue}"
    store.upsert_session(
        session_key=sk,
        project_key="huozhe/code-workflow",
        repo="huozhe/code-workflow",
        issue_num=issue,
        state=state,
        architect="huozheclaude",
        developer="huozhegrok",
        created_at=1,
        updated_at=1,
        design_pr=pr,
    )
    if silent_turns:
        store.update_session_fields(sk, silent_turns=silent_turns)
    return sk


def _insert(
    store: Store,
    *,
    did: str,
    event: str,
    action: str | None,
    sender: str,
    issue: int,
    payload: dict[str, Any],
) -> None:
    store.insert_delivery(
        delivery_id=did,
        event=event,
        action=action,
        repo="huozhe/code-workflow",
        issue_num=issue,
        sender=sender,
        payload=json.dumps(payload).encode(),
        status="deferred",
    )


def _loop(store: Store, tmp: Path, *, fetch_pr: Any | None = None) -> DesignLoop:
    kw: dict[str, Any] = {}
    if fetch_pr is not None:
        kw["fetch_pr"] = fetch_pr
    loop = DesignLoop(
        store,
        _cfg(tmp),
        supervisor=object(),
        dispatch_turns=True,
        github_token="tok",
        fetch_threads=lambda **k: None,
        fetch_diff=lambda **k: None,
        **kw,
    )
    dispatched: list[dict[str, Any]] = []

    def _spy(**kw: Any) -> dict[str, Any]:
        dispatched.append(kw)
        return {"status": "done", "summary": "ok", "public_actions": [{"n": 1}]}

    loop._dispatch_turn = _spy  # type: ignore[method-assign]
    loop.dispatched = dispatched  # type: ignore[attr-defined]
    return loop


def _wire_author_review(*, body: object = None, state: str = "commented") -> dict:
    """Wire shape from #169: state lower-case, body is JSON null."""
    raw = json.loads(_FIXTURE.read_text())
    raw["review"]["body"] = body
    raw["review"]["state"] = state
    return raw


def test_five_author_thread_replies_dispatch_no_turn(tmp_path: Path) -> None:
    """Acceptance (1): five architect-sent commented reviews → zero developer turns.

    Must fail first: four of these dispatch on today's code. Assert the absence
    of a turn, never the quality of a summary.
    """
    store = Store(tmp_path / "state.db")
    sk = _seed(store, silent_turns=0)
    loop = _loop(store, tmp_path)
    payload = json.loads(_FIXTURE.read_text())
    assert payload["review"]["state"] == "commented"
    assert payload["review"]["body"] is None

    for i in range(5):
        _insert(
            store,
            did=f"d-reply-{i}",
            event="pull_request_review",
            action="submitted",
            sender="huozheclaude",
            issue=170,
            payload=payload,
        )
    loop.process_deferred_batch(limit=10)
    assert loop.dispatched == []  # type: ignore[attr-defined]
    sess = store.get_session(sk)
    assert sess is not None
    assert int(sess.get("silent_turns") or 0) == 0
    assert store.count_by_status().get("done", 0) == 5
    assert store.count_by_status().get("routed", 0) == 0
    store.close()


def test_fixture_is_wire_commented_and_body_none() -> None:
    """Acceptance (2): at least one case is a decompressed stored payload shape."""
    raw = json.loads(_FIXTURE.read_text())
    assert raw["review"]["state"] == "commented"
    assert raw["review"]["body"] is None
    assert raw["sender"]["login"] == raw["pull_request"]["user"]["login"]


def test_author_synchronize_still_routes_design_revised(tmp_path: Path) -> None:
    """Acceptance (3): pull_request.synchronize by the PR author still routes.

    Fails if the rule is written at #173's prose width (any PR-scoped event).
    """
    store = Store(tmp_path / "state.db")
    sk = _seed(store, state="DESIGN_REWORK")
    loop = _loop(store, tmp_path)
    issue, pr = 169, 170
    _insert(
        store,
        did="d-sync",
        event="pull_request",
        action="synchronize",
        sender="huozheclaude",
        issue=pr,
        payload={
            "action": "synchronize",
            "pull_request": {
                "number": pr,
                "user": {"login": "huozheclaude"},
                "head": {"sha": "abc", "ref": _design_ref(issue)},
                "merged": False,
            },
            "sender": {"login": "huozheclaude"},
            "repository": {"full_name": "huozhe/code-workflow"},
        },
    )
    loop.process_deferred_batch(limit=5)
    assert len(loop.dispatched) == 1  # type: ignore[attr-defined]
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["state"] == "DESIGN_REVIEW"
    store.close()


def test_counterpart_empty_body_commented_review_still_routes(
    tmp_path: Path,
) -> None:
    """Acceptance (4): design_loop.py:74–79 — standalone inline comment wake.

    Counterpart-sent empty-body commented review must still route. A check that
    drops every empty-body COMMENTED review makes this path invisible.
    """
    store = Store(tmp_path / "state.db")
    _seed(store)
    loop = _loop(store, tmp_path)
    _insert(
        store,
        did="d-peer-cmt",
        event="pull_request_review",
        action="submitted",
        sender="huozhegrok",
        issue=170,
        payload={
            "action": "submitted",
            "review": {"id": 1, "state": "commented", "body": None},
            "pull_request": {
                "number": 170,
                "user": {"login": "huozheclaude"},
                "head": {"sha": "abc", "ref": _design_ref(169)},
            },
            "sender": {"login": "huozhegrok"},
            "repository": {"full_name": "huozhe/code-workflow"},
        },
    )
    loop.process_deferred_batch(limit=5)
    assert len(loop.dispatched) == 1  # type: ignore[attr-defined]
    store.close()


def test_author_dismissed_dispatches_no_turn_peer_dismissed_routes(
    tmp_path: Path,
) -> None:
    """Acceptance (5): dismissed is in the class; action==submitted would miss it."""
    store = Store(tmp_path / "state.db")
    _seed(store)
    loop = _loop(store, tmp_path)
    pr_payload = {
        "number": 170,
        "user": {"login": "huozheclaude"},
        "head": {"sha": "abc", "ref": _design_ref(169)},
    }
    _insert(
        store,
        did="d-auth-dismiss",
        event="pull_request_review",
        action="dismissed",
        sender="huozheclaude",
        issue=170,
        payload={
            "action": "dismissed",
            "review": {"id": 2, "state": "dismissed", "body": None},
            "pull_request": pr_payload,
            "sender": {"login": "huozheclaude"},
            "repository": {"full_name": "huozhe/code-workflow"},
        },
    )
    _insert(
        store,
        did="d-peer-dismiss",
        event="pull_request_review",
        action="dismissed",
        sender="huozhegrok",
        issue=170,
        payload={
            "action": "dismissed",
            "review": {"id": 3, "state": "dismissed", "body": None},
            "pull_request": pr_payload,
            "sender": {"login": "huozhegrok"},
            "repository": {"full_name": "huozhe/code-workflow"},
        },
    )
    loop.process_deferred_batch(limit=10)
    ids = [c["delivery_id"] for c in loop.dispatched]  # type: ignore[attr-defined]
    assert "d-auth-dismiss" not in ids
    assert "d-peer-dismiss" in ids
    store.close()


def test_author_sent_verdict_routes_and_logs_warning(
    tmp_path: Path, caplog: Any
) -> None:
    """Acceptance (6): unreachable through GitHub; alarm rather than drop.

    Author-sent CHANGES_REQUESTED is a self-echo after the FSM (recipient is
    the author). The drop at :443 must not fire — P1 still records the kind.
    """
    import logging

    store = Store(tmp_path / "state.db")
    sk = _seed(store, state="DESIGN_REVIEW")
    loop = _loop(store, tmp_path)
    _insert(
        store,
        did="d-auth-cr",
        event="pull_request_review",
        action="submitted",
        sender="huozheclaude",
        issue=170,
        payload=_wire_author_review(state="changes_requested", body="nits"),
    )
    with caplog.at_level(logging.WARNING, logger="agentd.design_loop"):
        loop.process_deferred_batch(limit=5)
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["state"] == "DESIGN_REWORK"
    assert any("author-sent" in r.getMessage() for r in caplog.records)
    store.close()


def test_spent_changes_requested_records_fsm_and_dispatches_no_turn(
    tmp_path: Path,
) -> None:
    """Acceptance (7): counterpart CHANGES_REQUESTED drained after merge."""
    store = Store(tmp_path / "state.db")
    sk = _seed(store, state="DESIGN_REVIEW")
    loop = _loop(
        store,
        tmp_path,
        fetch_pr=lambda **k: {"merged": True, "state": "closed"},
    )
    _insert(
        store,
        did="d-spent-cr",
        event="pull_request_review",
        action="submitted",
        sender="huozhegrok",
        issue=170,
        payload={
            "action": "submitted",
            "review": {"id": 9, "state": "changes_requested", "body": "nits"},
            "pull_request": {
                "number": 170,
                "user": {"login": "huozheclaude"},
                "head": {"sha": "abc", "ref": _design_ref(169)},
            },
            "sender": {"login": "huozhegrok"},
            "repository": {"full_name": "huozhe/code-workflow"},
        },
    )
    loop.process_deferred_batch(limit=5)
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["state"] == "DESIGN_REWORK"
    assert sess["state"] not in TERMINAL_STATES
    assert loop.dispatched == []  # type: ignore[attr-defined]
    store.close()


def test_spent_synchronize_records_fsm_and_dispatches_no_turn(
    tmp_path: Path,
) -> None:
    """Acceptance (7): #169's 18:11:43 synchronize after the PR merged."""
    store = Store(tmp_path / "state.db")
    sk = _seed(store, state="DESIGN_REWORK")
    loop = _loop(
        store,
        tmp_path,
        fetch_pr=lambda **k: {"merged": True, "state": "closed"},
    )
    _insert(
        store,
        did="d-spent-sync",
        event="pull_request",
        action="synchronize",
        sender="huozheclaude",
        issue=170,
        payload={
            "action": "synchronize",
            "pull_request": {
                "number": 170,
                "user": {"login": "huozheclaude"},
                "head": {"sha": "abc", "ref": _design_ref(169)},
                "merged": False,
            },
            "sender": {"login": "huozheclaude"},
            "repository": {"full_name": "huozhe/code-workflow"},
        },
    )
    loop.process_deferred_batch(limit=5)
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["state"] == "DESIGN_REVIEW"
    assert sess["state"] not in TERMINAL_STATES
    assert loop.dispatched == []  # type: ignore[attr-defined]
    store.close()


def test_owner_comment_and_review_on_owner_authored_pr_still_route(
    tmp_path: Path,
) -> None:
    """ADR-30 (a⁗) / acceptance (6): owner is not dropped.

    sender == author == owner. Both events dispatch one turn. The unpause
    path never reaches this gate (session issue has no issue.pull_request).
    """
    store = Store(tmp_path / "state.db")
    _seed(store)
    loop = _loop(store, tmp_path)
    _insert(
        store,
        did="d-owner-cmt",
        event="issue_comment",
        action="created",
        sender="huozhe",
        issue=170,
        payload={
            "action": "created",
            "issue": {
                "number": 170,
                "user": {"login": "huozhe"},
                "pull_request": {"url": "https://api.github.com/repos/x/pulls/170"},
            },
            "comment": {"id": 9, "body": "please proceed"},
            "sender": {"login": "huozhe"},
            "repository": {"full_name": "huozhe/code-workflow"},
        },
    )
    _insert(
        store,
        did="d-owner-rev",
        event="pull_request_review",
        action="submitted",
        sender="huozhe",
        issue=170,
        payload={
            "action": "submitted",
            "review": {"id": 8, "state": "commented", "body": None},
            "pull_request": {
                "number": 170,
                "user": {"login": "huozhe"},
                "head": {"sha": "abc", "ref": _design_ref(169)},
            },
            "sender": {"login": "huozhe"},
            "repository": {"full_name": "huozhe/code-workflow"},
        },
    )
    loop.process_deferred_batch(limit=10)
    ids = [c["delivery_id"] for c in loop.dispatched]  # type: ignore[attr-defined]
    assert ids == ["d-owner-cmt", "d-owner-rev"] or set(ids) == {
        "d-owner-cmt",
        "d-owner-rev",
    }
    store.close()


def test_spent_pr_fetch_skipped_once_already_merged(tmp_path: Path) -> None:
    """Merged is a one-way door: do not re-GET a PR already seen merged."""
    store = Store(tmp_path / "state.db")
    _seed(store, state="DESIGN_REWORK")
    calls: list[int] = []

    def fetch(**kw: Any) -> dict[str, str | bool]:
        calls.append(int(kw["pr_number"]))
        return {"merged": True, "state": "closed"}

    loop = _loop(store, tmp_path, fetch_pr=fetch)
    for i in range(2):
        _insert(
            store,
            did=f"d-spent-sync-{i}",
            event="pull_request",
            action="synchronize",
            sender="huozheclaude",
            issue=170,
            payload={
                "action": "synchronize",
                "pull_request": {
                    "number": 170,
                    "user": {"login": "huozheclaude"},
                    "head": {"sha": "abc", "ref": _design_ref(169)},
                    "merged": False,
                },
                "sender": {"login": "huozheclaude"},
                "repository": {"full_name": "huozhe/code-workflow"},
            },
        )
        loop.process_deferred_batch(limit=5)
    assert calls == [170]
    store.close()


def test_closed_then_reopened_pr_is_not_spent_forever(tmp_path: Path) -> None:
    """Closed is not a one-way door — a closed PR can reopen; re-GET it."""
    store = Store(tmp_path / "state.db")
    _seed(store, state="DESIGN_REWORK")
    calls: list[int] = []
    pr_state: dict[str, str | bool] = {"merged": False, "state": "closed"}

    def fetch(**kw: Any) -> dict[str, str | bool]:
        calls.append(1)
        return dict(pr_state)

    loop = _loop(store, tmp_path, fetch_pr=fetch)
    payload = {
        "action": "synchronize",
        "pull_request": {
            "number": 170,
            "user": {"login": "huozheclaude"},
            "head": {"sha": "abc", "ref": _design_ref(169)},
            "merged": False,
        },
        "sender": {"login": "huozheclaude"},
        "repository": {"full_name": "huozhe/code-workflow"},
    }
    _insert(
        store,
        did="d-closed",
        event="pull_request",
        action="synchronize",
        sender="huozheclaude",
        issue=170,
        payload=payload,
    )
    loop.process_deferred_batch(limit=5)
    assert loop.dispatched == []  # type: ignore[attr-defined]
    assert calls == [1]

    pr_state.update({"merged": False, "state": "open"})
    _insert(
        store,
        did="d-reopened",
        event="pull_request",
        action="synchronize",
        sender="huozheclaude",
        issue=170,
        payload=payload,
    )
    loop.process_deferred_batch(limit=5)
    assert len(loop.dispatched) == 1  # type: ignore[attr-defined]
    assert loop.dispatched[0]["delivery_id"] == "d-reopened"  # type: ignore[attr-defined]
    assert calls == [1, 1]
    store.close()


def test_author_issue_comment_on_pr_dispatches_no_turn(tmp_path: Path) -> None:
    """PR-level issue_comment from the PR author is in the class; session issue is not."""
    store = Store(tmp_path / "state.db")
    _seed(store)
    loop = _loop(store, tmp_path)
    _insert(
        store,
        did="d-pr-comment",
        event="issue_comment",
        action="created",
        sender="huozheclaude",
        issue=170,
        payload={
            "action": "created",
            "issue": {
                "number": 170,
                "user": {"login": "huozheclaude"},
                "pull_request": {"url": "https://api.github.com/repos/x/pulls/170"},
            },
            "comment": {"id": 1, "body": "reworked"},
            "sender": {"login": "huozheclaude"},
            "repository": {"full_name": "huozhe/code-workflow"},
        },
    )
    loop.process_deferred_batch(limit=5)
    assert loop.dispatched == []  # type: ignore[attr-defined]
    store.close()
