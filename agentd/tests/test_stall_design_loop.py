"""M3-B: zero-thread-progress through DesignLoop with fingerprint disarmed."""

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


def _changes_requested_payload(*, head: str, threads_resolved: int = 0) -> bytes:
    return json.dumps(
        {
            "action": "submitted",
            "review": {"state": "changes_requested", "id": 1},
            "pull_request": {
                "number": 9,
                "title": "Design: RFC",
                "html_url": "https://example/pr/9",
                "head": {"sha": head},
            },
            "repository": {"full_name": "huozhe/code-workflow"},
            "sender": {"login": "huozhegrok"},
            # Stall inputs on the digest path (§9.3) — frozen progress, static head.
            "open_thread_ids": ["thread-a"],
            "unresolved_count": 1,
            "threads_resolved": threads_resolved,
            "diff_stat": "1 file changed, 1 insertion(+)",
        }
    ).encode()


def test_zero_thread_escalates_via_design_loop_fingerprint_disarmed(
    tmp_path: Path,
) -> None:
    """Exit: stalling review rounds escalate via zero-thread while fp unarmed."""
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    now = 1_700_000_000
    sk = "huozhe/code-workflow#9"
    store.upsert_session(
        session_key=sk,
        repo="huozhe/code-workflow",
        issue_num=9,
        state="DESIGN_REVIEW",
        architect="huozheclaude",
        developer="huozhegrok",
        created_at=now,
        updated_at=now,
        design_pr=9,
    )
    posts: list[str] = []

    def fake_post(*, repo, issue_num, body, token):  # noqa: ANN001
        posts.append(body)
        return 7001

    loop = DesignLoop(
        store,
        cfg,
        supervisor=None,
        dispatch_turns=False,
        post_comment=fake_post,
        gateway_token="gw",
    )

    static_head = "deadbeef00000000000000000000000000000001"
    for i in range(3):
        store.insert_delivery(
            delivery_id=f"d-cr-{i}",
            event="pull_request_review",
            action="submitted",
            repo="huozhe/code-workflow",
            issue_num=9,
            sender="huozhegrok",
            payload=_changes_requested_payload(head=static_head, threads_resolved=0),
            status="deferred",
        )
        loop.process_deferred_batch()

    sess = store.get_session(sk)
    assert sess is not None
    assert sess["state"] == "PAUSED_HUMAN", sess
    reason = str(sess.get("paused_reason") or "")
    assert "zero thread" in reason, reason
    assert "fingerprint" not in reason
    # Fingerprint never armed (no A: prefix)
    fp = str(sess.get("progress_fp") or "")
    assert not fp.startswith("A:"), fp
    assert posts, "escalation comment should have been posted"
    assert "@huozhe" in posts[0]
    assert "zero thread" in posts[0] or "zero thread" in reason
    esc = store.get_open_escalation(sk)
    assert esc is not None and esc.get("comment_id") == 7001
    store.close()


def test_thread_resolution_resets_zero_thread_counter(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    now = 1_700_000_000
    sk = "huozhe/code-workflow#10"
    store.upsert_session(
        session_key=sk,
        repo="huozhe/code-workflow",
        issue_num=10,
        state="DESIGN_REVIEW",
        architect="huozheclaude",
        developer="huozhegrok",
        created_at=now,
        updated_at=now,
        design_pr=10,
    )
    loop = DesignLoop(
        store,
        cfg,
        supervisor=None,
        dispatch_turns=False,
        post_comment=lambda **k: 1,  # noqa: ARG005
        gateway_token="gw",
    )
    head = "abc"
    # Two empty rounds
    for i in range(2):
        store.insert_delivery(
            delivery_id=f"d-z-{i}",
            event="pull_request_review",
            action="submitted",
            repo="huozhe/code-workflow",
            issue_num=10,
            sender="huozhegrok",
            payload=_changes_requested_payload(head=head, threads_resolved=0),
            status="deferred",
        )
        loop.process_deferred_batch()
    assert int(store.get_session(sk)["zero_thread_rounds"] or 0) == 2

    # A round that resolves a thread resets the counter
    store.insert_delivery(
        delivery_id="d-z-resolved",
        event="pull_request_review",
        action="submitted",
        repo="huozhe/code-workflow",
        issue_num=10,
        sender="huozhegrok",
        payload=_changes_requested_payload(head=head, threads_resolved=1),
        status="deferred",
    )
    loop.process_deferred_batch()
    sess = store.get_session(sk)
    assert sess["state"] != "PAUSED_HUMAN"
    assert int(sess["zero_thread_rounds"] or 0) == 0
    store.close()
