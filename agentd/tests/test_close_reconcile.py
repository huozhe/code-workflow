"""ADR-17 / #90: ticked body + NULL verified_at does not classify VERIFIED."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import ClassVar

from agentd.config import Config
from agentd.db import Store
from agentd.design_loop import (
    CLOSE_RECONCILE_GRACE_S,
    CLOSE_RECONCILE_PREFIX,
    DesignLoop,
    _close_grace_warned,
)
from agentd.verification import (
    checkbox_is_checked,
    render_verification_block,
)


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


def _block(*, checked: bool) -> str:
    return render_verification_block(
        steps=["pull", "test"],
        merged_prs=[59, 60],
        checked=checked,
    )


def _body(*, checked: bool) -> str:
    return "## Goal\n\nSession work.\n\n" + _block(checked=checked) + "\n"


def _seed(
    store: Store,
    tmp: Path,
    *,
    state: str = "AWAITING_VERIFICATION",
    issue: int = 90,
    verified_at: int | None = None,
    paused_reason: str | None = None,
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
    )
    extra: dict = {}
    if verified_at is not None:
        extra["verified_at"] = verified_at
    if paused_reason is not None:
        extra["paused_reason"] = paused_reason
        extra["resume_state"] = "AWAITING_VERIFICATION"
    if extra:
        store.update_session_fields(sk, **extra)
    store.upsert_runner(
        "huozhe/code-workflow",
        container_id="c1",
        endpoint="127.0.0.1:9",
        token="tok",
        tier="hot",
    )
    d = tmp / "projects" / "huozhe__code-workflow" / "sessions" / str(issue)
    d.mkdir(parents=True, exist_ok=True)
    (d / "keep").write_text("x", encoding="utf-8")
    return sk


def _closed_payload(*, body: str, issue: int = 90) -> dict:
    return {
        "action": "closed",
        "issue": {
            "number": issue,
            "state": "closed",
            "title": "session",
            "body": body,
        },
        "repository": {"full_name": "huozhe/code-workflow"},
        "sender": {"login": "huozhe"},
    }


def _insert_close(
    store: Store,
    *,
    did: str,
    body: str,
    issue: int = 90,
    received_at: int | None = None,
) -> None:
    store.insert_delivery(
        delivery_id=did,
        event="issues",
        action="closed",
        repo="huozhe/code-workflow",
        issue_num=issue,
        sender="huozhe",
        payload=json.dumps(_closed_payload(body=body, issue=issue)).encode(),
        status="deferred",
    )
    if received_at is not None:
        store._conn.execute(
            "UPDATE deliveries SET received_at = ? WHERE delivery_id = ?",
            (int(received_at), did),
        )
        store._conn.commit()


def _insert_reopen(
    store: Store,
    *,
    did: str,
    issue: int = 90,
    sender: str = "huozhe",
) -> None:
    payload = json.dumps(
        {
            "action": "reopened",
            "issue": {
                "number": issue,
                "state": "open",
                "title": "session",
                "author_association": "OWNER",
                "labels": [{"name": "agentd"}],
            },
            "repository": {"full_name": "huozhe/code-workflow"},
            "sender": {"login": sender},
        }
    ).encode()
    store.insert_delivery(
        delivery_id=did,
        event="issues",
        action="reopened",
        repo="huozhe/code-workflow",
        issue_num=issue,
        sender=sender,
        payload=payload,
        status="deferred",
    )


def _insert_owner_reply(
    store: Store,
    *,
    did: str,
    issue: int = 90,
    issue_state: str = "open",
) -> None:
    payload = json.dumps(
        {
            "action": "created",
            "issue": {"number": issue, "title": "session", "state": issue_state},
            "comment": {"body": "continue the work", "user": {"login": "huozhe"}},
            "repository": {"full_name": "huozhe/code-workflow"},
            "sender": {"login": "huozhe"},
        }
    ).encode()
    store.insert_delivery(
        delivery_id=did,
        event="issue_comment",
        action="created",
        repo="huozhe/code-workflow",
        issue_num=issue,
        sender="huozhe",
        payload=payload,
        status="deferred",
    )


def _status(store: Store, did: str) -> str:
    row = store._conn.execute(
        "SELECT status FROM deliveries WHERE delivery_id=?", (did,)
    ).fetchone()
    assert row is not None
    return str(row["status"])


class _RecordingClient:
    calls: ClassVar[list[tuple[str, dict]]] = []

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def call(self, method, params=None):
        self.calls.append((method, dict(params or {})))
        return {"status": "done", "summary": "ok", "public_actions": []}


def _loop(
    store: Store,
    tmp: Path,
    *,
    dispatch: bool = False,
    posts: list | None = None,
    patches: list | None = None,
    gets: list | None = None,
    live_body: str | None = None,
    live_state: str = "closed",
    get_issue_fn=None,
) -> DesignLoop:
    def _post(**k):
        if posts is not None:
            posts.append(k)
        return 1

    def _patch(**k):
        if patches is not None:
            patches.append(k)
        if live_body is not None:
            pass

    def _get(**k):
        if gets is not None:
            gets.append(k)
        return live_body if live_body is not None else _body(checked=True)

    def _get_issue(**k):
        if gets is not None:
            gets.append(k)
        return {
            "body": live_body if live_body is not None else _body(checked=True),
            "state": live_state,
        }

    return DesignLoop(
        store,
        _cfg(tmp),
        supervisor=object() if dispatch else None,
        dispatch_turns=dispatch,
        post_comment=_post,
        patch_issue_body_fn=_patch,
        get_issue_body_fn=_get,
        get_issue_fn=get_issue_fn if get_issue_fn is not None else _get_issue,
        gateway_token="gw",
    )


def test_ticked_null_verified_at_within_grace_stays_deferred(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(store, tmp_path, verified_at=None)
    _close_grace_warned.clear()
    now = int(time.time())
    _insert_close(
        store, did="d-grace", body=_body(checked=True), received_at=now - 60
    )
    posts: list = []
    _loop(store, tmp_path, posts=posts).process_deferred_batch()
    assert _status(store, "d-grace") == "deferred"
    sess = store.get_session(sk)
    assert sess is not None
    assert sess.get("classification") in (None, "")
    assert sess["state"] == "AWAITING_VERIFICATION"
    assert posts == []
    store.close()


def test_grace_survives_attempt_counter_reset(tmp_path: Path) -> None:
    """Daemon restart mid-window must not fire the gate (durable received_at)."""
    store = Store(tmp_path / "state.db")
    sk = _seed(store, tmp_path, verified_at=None)
    _close_grace_warned.clear()
    now = int(time.time())
    _insert_close(
        store, did="d-restart", body=_body(checked=True), received_at=now - 120
    )
    _loop(store, tmp_path).process_deferred_batch()
    assert _status(store, "d-restart") == "deferred"
    import agentd.design_loop as dl

    dl._delivery_attempts.clear()
    _loop(store, tmp_path).process_deferred_batch()
    assert _status(store, "d-restart") == "deferred"
    assert store.get_session(sk)["state"] == "AWAITING_VERIFICATION"
    store.close()


def test_ticked_null_verified_at_after_grace_escalates_and_lowers(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(store, tmp_path, verified_at=None)
    _close_grace_warned.clear()
    now = int(time.time())
    _insert_close(
        store,
        did="d-gate",
        body=_body(checked=True),
        received_at=now - CLOSE_RECONCILE_GRACE_S - 5,
    )
    posts: list = []
    patches: list = []
    live = _body(checked=True)
    _loop(
        store, tmp_path, posts=posts, patches=patches, live_body=live
    ).process_deferred_batch()
    assert _status(store, "d-gate") == "done"
    sess = store.get_session(sk)
    assert sess is not None
    assert sess.get("classification") in (None, "")
    assert sess["state"] == "PAUSED_HUMAN"
    assert sess["resume_state"] == "AWAITING_VERIFICATION"
    assert str(sess["paused_reason"]).startswith(CLOSE_RECONCILE_PREFIX)
    assert len(patches) == 1
    assert checkbox_is_checked(patches[0]["body"], strict=True) is False
    assert "<!-- agentd:gateway" not in patches[0]["body"]
    assert len(posts) == 1
    assert "reopen" in posts[0]["body"].lower()
    assert "tick" in posts[0]["body"].lower()
    assert "Any reply from you" not in posts[0]["body"]
    body_l = posts[0]["body"].lower()
    assert "closed" in body_l
    assert "continue" in body_l
    esc = store.get_open_escalation(sk)
    assert esc is not None
    store.close()


def test_already_down_skips_patch_still_escalates(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(store, tmp_path, verified_at=None)
    _close_grace_warned.clear()
    now = int(time.time())
    _insert_close(
        store,
        did="d-down",
        body=_body(checked=True),
        received_at=now - CLOSE_RECONCILE_GRACE_S - 5,
    )
    patches: list = []
    _loop(
        store,
        tmp_path,
        patches=patches,
        live_body=_body(checked=False),
    ).process_deferred_batch()
    assert patches == []
    assert store.get_session(sk)["state"] == "PAUSED_HUMAN"
    store.close()


def test_failed_read_skips_patch_still_escalates(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(store, tmp_path, verified_at=None)
    _close_grace_warned.clear()
    now = int(time.time())
    _insert_close(
        store,
        did="d-getfail",
        body=_body(checked=True),
        received_at=now - CLOSE_RECONCILE_GRACE_S - 5,
    )
    patches: list = []

    def boom(**_):
        raise RuntimeError("github down")

    DesignLoop(
        store,
        _cfg(tmp_path),
        supervisor=None,
        dispatch_turns=False,
        post_comment=lambda **k: 1,
        patch_issue_body_fn=lambda **k: patches.append(k),
        get_issue_body_fn=boom,
        gateway_token="gw",
    ).process_deferred_batch()
    assert patches == []
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["state"] == "PAUSED_HUMAN"
    store.close()


def test_verified_at_set_still_verified(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(store, tmp_path, verified_at=int(time.time()))
    _insert_close(store, did="d-ok", body=_body(checked=True))
    _loop(store, tmp_path).process_deferred_batch()
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["classification"] == "VERIFIED"
    assert sess["state"] == "CLOSED"
    store.close()


def test_already_classified_skips_gate(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(store, tmp_path, verified_at=None)
    store.update_session_fields(sk, classification="VERIFIED")
    _insert_close(store, did="d-class", body=_body(checked=True))
    _loop(store, tmp_path).process_deferred_batch()
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["classification"] == "VERIFIED"
    assert sess["state"] == "CLOSED"
    store.close()


def test_later_close_closes_escalation(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(store, tmp_path, verified_at=int(time.time()))
    store.open_escalation(session_key=sk, role="system", reason="prior")
    _insert_close(store, did="d-esc", body=_body(checked=True))
    _loop(store, tmp_path).process_deferred_batch()
    assert store.get_open_escalation(sk) is None
    store.close()


def test_owner_reopen_lifts_hold_no_dispatch(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(
        store,
        tmp_path,
        state="PAUSED_HUMAN",
        paused_reason=f"{CLOSE_RECONCILE_PREFIX}hold",
    )
    store.open_escalation(session_key=sk, role="system", reason="hold")
    _insert_reopen(store, did="d-reopen", sender="huozhe")
    import agentd.design_loop as dl

    _RecordingClient.calls = []
    orig = dl.RunnerClient
    dl.RunnerClient = _RecordingClient  # type: ignore[misc]
    try:
        _loop(store, tmp_path, dispatch=True).process_deferred_batch()
    finally:
        dl.RunnerClient = orig  # type: ignore[misc]
    sess = store.get_session(sk)
    assert sess is not None
    assert _status(store, "d-reopen") == "done"
    assert sess["state"] == "PAUSED_HUMAN"
    assert sess["resume_state"] == "AWAITING_VERIFICATION"
    assert not str(sess["paused_reason"] or "").startswith(CLOSE_RECONCILE_PREFIX)
    assert store.get_open_escalation(sk) is not None
    assert store.count_turns(sk) == 0
    assert not any(m == "turn.dispatch" for m, _ in _RecordingClient.calls)
    store.close()


def test_non_owner_reopen_lifts_hold_done_not_deferred(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(
        store,
        tmp_path,
        state="PAUSED_HUMAN",
        paused_reason=f"{CLOSE_RECONCILE_PREFIX}hold",
    )
    store.open_escalation(session_key=sk, role="system", reason="hold")
    _insert_reopen(store, did="d-agent-reopen", sender="huozhegrok")
    import agentd.design_loop as dl

    _RecordingClient.calls = []
    orig = dl.RunnerClient
    dl.RunnerClient = _RecordingClient  # type: ignore[misc]
    try:
        _loop(store, tmp_path, dispatch=True).process_deferred_batch()
    finally:
        dl.RunnerClient = orig  # type: ignore[misc]
    sess = store.get_session(sk)
    assert sess is not None
    assert _status(store, "d-agent-reopen") == "done"
    assert sess["state"] == "PAUSED_HUMAN"
    assert sess["resume_state"] == "AWAITING_VERIFICATION"
    assert not str(sess["paused_reason"] or "").startswith(CLOSE_RECONCILE_PREFIX)
    assert store.get_open_escalation(sk) is not None
    assert store.count_turns(sk) == 0
    assert not any(m == "turn.dispatch" for m, _ in _RecordingClient.calls)
    store.close()


def test_owner_reply_after_owner_reopen_resumes(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(
        store,
        tmp_path,
        state="PAUSED_HUMAN",
        paused_reason=f"{CLOSE_RECONCILE_PREFIX}hold",
    )
    store.open_escalation(session_key=sk, role="system", reason="hold")
    _insert_reopen(store, did="d-reopen", sender="huozhe")
    _insert_owner_reply(store, did="d-reply")
    _loop(store, tmp_path, dispatch=False).process_deferred_batch()
    sess = store.get_session(sk)
    assert sess is not None
    assert _status(store, "d-reopen") == "done"
    assert _status(store, "d-reply") == "routed"
    assert sess["state"] == "AWAITING_VERIFICATION"
    assert sess.get("paused_reason") is None
    assert sess.get("resume_state") is None
    assert store.get_open_escalation(sk) is None
    store.close()


def test_owner_reply_after_agent_reopen_resumes(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(
        store,
        tmp_path,
        state="PAUSED_HUMAN",
        paused_reason=f"{CLOSE_RECONCILE_PREFIX}hold",
    )
    store.open_escalation(session_key=sk, role="system", reason="hold")
    _insert_reopen(store, did="d-agent-reopen", sender="huozhegrok")
    _insert_owner_reply(store, did="d-reply")
    _loop(store, tmp_path, dispatch=False).process_deferred_batch()
    sess = store.get_session(sk)
    assert sess is not None
    assert _status(store, "d-agent-reopen") == "done"
    assert _status(store, "d-reply") == "routed"
    assert sess["state"] == "AWAITING_VERIFICATION"
    assert sess.get("paused_reason") is None
    assert store.get_open_escalation(sk) is None
    store.close()


def test_hold_refuses_owner_reply_resume(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(
        store,
        tmp_path,
        state="PAUSED_HUMAN",
        paused_reason=f"{CLOSE_RECONCILE_PREFIX}hold",
    )
    store.open_escalation(session_key=sk, role="system", reason="hold")
    payload = json.dumps(
        {
            "action": "created",
            "issue": {"number": 90, "title": "session", "state": "closed"},
            "comment": {"body": "ok go", "user": {"login": "huozhe"}},
            "repository": {"full_name": "huozhe/code-workflow"},
            "sender": {"login": "huozhe"},
        }
    ).encode()
    store.insert_delivery(
        delivery_id="d-reply",
        event="issue_comment",
        action="created",
        repo="huozhe/code-workflow",
        issue_num=90,
        sender="huozhe",
        payload=payload,
        status="deferred",
    )
    _loop(store, tmp_path, dispatch=True).process_deferred_batch()
    assert store.count_turns(sk) == 0
    assert _status(store, "d-reply") == "done"
    assert store.get_session(sk)["state"] == "PAUSED_HUMAN"
    assert store.get_open_escalation(sk) is not None
    store.close()


def test_mirror_unticked_verified_at_within_grace_stays_deferred(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(store, tmp_path, verified_at=int(time.time()))
    _close_grace_warned.clear()
    now = int(time.time())
    _insert_close(
        store, did="d-m-grace", body=_body(checked=False), received_at=now - 60
    )
    posts: list = []
    _loop(store, tmp_path, posts=posts).process_deferred_batch()
    assert _status(store, "d-m-grace") == "deferred"
    sess = store.get_session(sk)
    assert sess is not None
    assert sess.get("classification") in (None, "")
    assert sess["state"] == "AWAITING_VERIFICATION"
    assert posts == []
    store.close()


def test_mirror_after_grace_reread_ticked_is_verified(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(store, tmp_path, verified_at=int(time.time()))
    _close_grace_warned.clear()
    now = int(time.time())
    _insert_close(
        store,
        did="d-m-up",
        body=_body(checked=False),
        received_at=now - CLOSE_RECONCILE_GRACE_S - 5,
    )
    _loop(
        store, tmp_path, live_body=_body(checked=True), live_state="closed"
    ).process_deferred_batch()
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["classification"] == "VERIFIED"
    assert sess["state"] == "CLOSED"
    assert _status(store, "d-m-up") == "done"
    store.close()


def test_mirror_after_grace_reread_down_is_abandoned(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(store, tmp_path, verified_at=int(time.time()))
    _close_grace_warned.clear()
    now = int(time.time())
    _insert_close(
        store,
        did="d-m-down",
        body=_body(checked=False),
        received_at=now - CLOSE_RECONCILE_GRACE_S - 5,
    )
    _loop(
        store, tmp_path, live_body=_body(checked=False), live_state="closed"
    ).process_deferred_batch()
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["classification"] == "ABANDONED"
    assert sess["state"] == "CLOSED"
    store.close()


def test_mirror_fetch_fail_classifies_from_payload(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(store, tmp_path, verified_at=int(time.time()))
    _close_grace_warned.clear()
    now = int(time.time())
    _insert_close(
        store,
        did="d-m-fail",
        body=_body(checked=False),
        received_at=now - CLOSE_RECONCILE_GRACE_S - 5,
    )

    def boom(**_):
        raise RuntimeError("github down")

    DesignLoop(
        store,
        _cfg(tmp_path),
        supervisor=None,
        dispatch_turns=False,
        post_comment=lambda **k: 1,
        get_issue_fn=boom,
        gateway_token="gw",
    ).process_deferred_batch()
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["classification"] == "ABANDONED"
    assert sess["state"] == "CLOSED"
    store.close()


def test_mirror_open_issue_cancels_no_classify(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(store, tmp_path, verified_at=int(time.time()))
    _close_grace_warned.clear()
    now = int(time.time())
    _insert_close(
        store,
        did="d-m-open",
        body=_body(checked=False),
        received_at=now - CLOSE_RECONCILE_GRACE_S - 5,
    )
    posts: list = []
    _loop(
        store,
        tmp_path,
        posts=posts,
        live_body=_body(checked=True),
        live_state="open",
    ).process_deferred_batch()
    assert _status(store, "d-m-open") == "done"
    sess = store.get_session(sk)
    assert sess is not None
    assert sess.get("classification") in (None, "")
    assert sess["state"] == "AWAITING_VERIFICATION"
    assert posts == []
    store.close()


def test_adr17_open_issue_no_escalate(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(store, tmp_path, verified_at=None)
    _close_grace_warned.clear()
    now = int(time.time())
    _insert_close(
        store,
        did="d-17-open",
        body=_body(checked=True),
        received_at=now - CLOSE_RECONCILE_GRACE_S - 5,
    )
    posts: list = []
    _loop(
        store,
        tmp_path,
        posts=posts,
        live_body=_body(checked=True),
        live_state="open",
    ).process_deferred_batch()
    assert _status(store, "d-17-open") == "done"
    sess = store.get_session(sk)
    assert sess is not None
    assert sess.get("classification") in (None, "")
    assert sess["state"] == "AWAITING_VERIFICATION"
    assert posts == []
    store.close()


def test_withdraw_during_grace_classifies_abandoned(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(store, tmp_path, verified_at=int(time.time()))
    _close_grace_warned.clear()
    now = int(time.time())
    _insert_close(
        store, did="d-wd", body=_body(checked=False), received_at=now - 60
    )
    _loop(store, tmp_path).process_deferred_batch()
    assert _status(store, "d-wd") == "deferred"
    store.update_session_fields(sk, verified_at=None)
    _loop(store, tmp_path).process_deferred_batch()
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["classification"] == "ABANDONED"
    assert sess["state"] == "CLOSED"
    store.close()
