"""#85: no ordinary turn on a terminal session; teardown turns still run."""

from __future__ import annotations

import json
from pathlib import Path

from agentd.config import Config
from agentd.db import Store
from agentd.design_loop import DesignLoop


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


def _seed(store: Store, *, state: str, issue: int = 49) -> str:
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
    )
    store.upsert_runner(
        "huozhe/code-workflow",
        container_id="c1",
        endpoint="127.0.0.1:9",
        token="tok",
        tier="hot",
    )
    return sk


def _insert_comment(
    store: Store,
    *,
    did: str,
    issue: int,
    sender: str,
    body: str = "post-close note",
) -> None:
    payload = json.dumps(
        {
            "action": "created",
            "issue": {"number": issue, "title": "session", "state": "closed"},
            "comment": {"body": body, "user": {"login": sender}},
            "repository": {"full_name": "huozhe/code-workflow"},
            "sender": {"login": sender},
        }
    ).encode()
    store.insert_delivery(
        delivery_id=did,
        event="issue_comment",
        action="created",
        repo="huozhe/code-workflow",
        issue_num=issue,
        sender=sender,
        payload=payload,
        status="deferred",
    )


def _insert_reopen(store: Store, *, did: str, issue: int) -> None:
    payload = json.dumps(
        {
            "action": "reopened",
            "issue": {
                "number": issue,
                "title": "session",
                "state": "open",
                "author_association": "OWNER",
                "labels": [{"name": "agentd"}],
            },
            "repository": {"full_name": "huozhe/code-workflow"},
            "sender": {"login": "huozhe"},
        }
    ).encode()
    store.insert_delivery(
        delivery_id=did,
        event="issues",
        action="reopened",
        repo="huozhe/code-workflow",
        issue_num=issue,
        sender="huozhe",
        payload=payload,
        status="deferred",
    )


class _Supervisor:
    def __init__(self) -> None:
        self.calls = 0

    def ensure_session(self, **k):  # noqa: ANN003
        self.calls += 1


class _RecordingClient:
    calls: list[tuple[str, dict]] = []

    def __init__(self, *a, **k):  # noqa: ANN002, ANN003
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):  # noqa: ANN002
        return False

    def call(self, method, params=None):  # noqa: ANN001
        self.calls.append((method, dict(params or {})))
        return {"status": "done", "summary": "ok", "public_actions": []}


def _loop(store: Store, tmp: Path, supervisor) -> DesignLoop:
    import agentd.design_loop as dl

    _RecordingClient.calls = []
    loop = DesignLoop(
        store, _cfg(tmp), supervisor=supervisor, dispatch_turns=True, gateway_token="gw"
    )
    orig = dl.RunnerClient
    dl.RunnerClient = _RecordingClient  # type: ignore[misc]
    try:
        loop.process_deferred_batch()
    finally:
        dl.RunnerClient = orig  # type: ignore[misc]
    return loop


def _delivery_status(store: Store, did: str) -> str:
    row = store._conn.execute(
        "SELECT status FROM deliveries WHERE delivery_id=?", (did,)
    ).fetchone()
    assert row is not None
    return str(row["status"])


def test_closed_comment_no_turn_no_session_dir(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(store, state="CLOSED")
    _insert_comment(store, did="d-closed", issue=49, sender="huozheclaude")
    sup = _Supervisor()
    _loop(store, tmp_path, sup)
    assert store.count_turns(sk) == 0
    assert _delivery_status(store, "d-closed") == "done"
    assert not (tmp_path / "projects" / "huozhe__code-workflow" / "sessions" / "49").exists()
    assert sup.calls == 0
    store.close()


def test_teardown_comment_no_ordinary_turn(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(store, state="TEARDOWN")
    _insert_comment(store, did="d-td", issue=49, sender="huozhe")
    _loop(store, tmp_path, _Supervisor())
    assert store.count_turns(sk) == 0
    assert _delivery_status(store, "d-td") == "done"
    assert not (tmp_path / "projects" / "huozhe__code-workflow" / "sessions" / "49").exists()
    store.close()


def test_reopen_on_closed_is_done_not_dropped(tmp_path: Path) -> None:
    """issues.reopened still processes (P1); it must not be swallowed as dropped."""
    store = Store(tmp_path / "state.db")
    sk = _seed(store, state="CLOSED")
    _insert_reopen(store, did="d-reopen", issue=49)
    _loop(store, tmp_path, _Supervisor())
    assert _delivery_status(store, "d-reopen") == "done"
    assert store.count_turns(sk) == 0
    assert store.get_session(sk) is not None
    store.close()


def test_gateway_restore_comment_no_turn_owner_still_wakes(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(store, state="AWAITING_VERIFICATION", issue=84)
    gw_body = (
        "**agentd** restored the §10.1 verification checkbox.\n"
        f"<!-- agentd:gateway session={sk} -->"
    )
    _insert_comment(
        store, did="d-gw", issue=84, sender="huozhegateway", body=gw_body
    )
    _loop(store, tmp_path, _Supervisor())
    assert store.count_turns(sk) == 0
    assert _delivery_status(store, "d-gw") == "done"

    _insert_comment(store, did="d-owner", issue=84, sender="huozhe", body="ok")
    _loop(store, tmp_path, _Supervisor())
    assert store.count_turns(sk) >= 1
    store.close()


def _insert_feature_changes_requested(store: Store, *, did: str, issue: int) -> None:
    from agentd.gitops import role_branch_name

    ref = role_branch_name("huozhe/code-workflow", issue, "developer")
    payload = json.dumps(
        {
            "action": "submitted",
            "review": {"state": "changes_requested", "id": 1},
            "pull_request": {
                "number": 900,
                "title": "feat",
                "html_url": "https://example/pr/900",
                "head": {"ref": ref, "sha": "a" * 40},
                "base": {"ref": "main"},
            },
            "repository": {"full_name": "huozhe/code-workflow"},
            "sender": {"login": "huozheclaude"},
        }
    ).encode()
    store.insert_delivery(
        delivery_id=did,
        event="pull_request_review",
        action="submitted",
        repo="huozhe/code-workflow",
        issue_num=900,
        sender="huozheclaude",
        payload=payload,
        status="deferred",
    )


def test_closed_late_review_does_not_observe_stall(tmp_path: Path) -> None:
    """#93 B1: terminal gate sits above the stall observer."""
    store = Store(tmp_path / "state.db")
    sk = _seed(store, state="CLOSED")
    _insert_feature_changes_requested(store, did="d-cr", issue=49)
    seen: list[str] = []
    fsm_calls: list[tuple[str, str]] = []

    import agentd.design_loop as dl

    orig_tr = dl.transition

    def spy_tr(state: str, kind: str):
        fsm_calls.append((state, kind))
        return orig_tr(state, kind)

    dl.transition = spy_tr  # type: ignore[misc]
    loop = DesignLoop(
        store,
        _cfg(tmp_path),
        supervisor=_Supervisor(),
        dispatch_turns=True,
        gateway_token="gw",
    )
    loop._observe_stall_signals = (  # type: ignore[method-assign]
        lambda **k: seen.append(str(k.get("kind"))) or False
    )
    orig_client = dl.RunnerClient
    dl.RunnerClient = _RecordingClient  # type: ignore[misc]
    try:
        loop.process_deferred_batch()
    finally:
        dl.RunnerClient = orig_client  # type: ignore[misc]
        dl.transition = orig_tr  # type: ignore[misc]

    assert seen == []
    assert ("CLOSED", "code_changes_requested") in fsm_calls
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["state"] == "CLOSED"
    assert sess.get("paused_reason") is None
    assert store.count_turns(sk) == 0
    assert _delivery_status(store, "d-cr") == "done"
    store.close()


def test_escalate_on_closed_session_is_noop(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(store, state="CLOSED")
    posts: list = []
    loop = DesignLoop(
        store,
        _cfg(tmp_path),
        supervisor=None,
        dispatch_turns=False,
        post_comment=lambda **k: posts.append(k) or 1,
    )
    loop._escalate(sk, "system", "should not land on a closed issue")
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["state"] == "CLOSED"
    assert sess.get("paused_reason") is None
    assert posts == []
    store.close()
