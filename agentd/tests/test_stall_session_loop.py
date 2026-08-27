"""M3-B: zero-thread-progress with real fetch boundary (not invented webhook fields)."""

from __future__ import annotations

import json
from pathlib import Path

from agentd.config import Config
from agentd.db import SCHEMA_VERSION, Store
from agentd.github_fetch import PrReviewThreadSnapshot
from agentd.session_loop import SessionLoop


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


def _realistic_changes_requested(*, head: str) -> bytes:
    """Webhook body shaped like GitHub — no stall invent fields."""
    return json.dumps(
        {
            "action": "submitted",
            "review": {"state": "changes_requested", "id": 1},
            "pull_request": {
                "number": 9,
                "title": "Design: RFC",
                "html_url": "https://example/pr/9",
                "head": {"sha": head},
                "base": {"ref": "main"},
            },
            "repository": {"full_name": "huozhe/code-workflow"},
            "sender": {"login": "huozhegrok"},
        }
    ).encode()


def _snap(open_ids: list[str], *, head: str = "deadbeef") -> PrReviewThreadSnapshot:
    return PrReviewThreadSnapshot(
        open_thread_ids=list(open_ids),
        unresolved_count=len(open_ids),
        all_thread_ids=list(open_ids),
        base_ref="main",
        head_oid=head,
    )


def test_zero_thread_escalates_via_fetch_boundary_fp_disarmed(tmp_path: Path) -> None:
    """Exit: 3 rounds, nothing resolved, static head → zero-thread only.

    Fingerprint is skipped when diff_stat is unavailable OR threads observed
    with static head never arm — here we supply a real frozen diff_stat so
    fingerprint *could* run, but static head keeps it disarmed.
    """
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
    # Same unresolved threads every round; frozen diff.
    open_ids = ["PRRT_thread_a", "PRRT_thread_b"]
    static_head = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

    def fake_threads(*, repo, pr_number, token):
        assert repo == "huozhe/code-workflow"
        assert pr_number == 9
        assert token == "tok"
        return _snap(open_ids, head=static_head)

    def fake_diff(*, repo, base, head, token):
        assert base == "main"
        return "file.py|modified|1+0-"

    loop = SessionLoop(
        store,
        cfg,
        supervisor=None,
        dispatch_turns=False,
        post_comment=lambda **k: posts.append(k["body"]) or 8001,
        gateway_token="gw",
        fetch_threads=fake_threads,
        fetch_diff=fake_diff,
        github_token="tok",
    )

    for i in range(3):
        store.insert_delivery(
            delivery_id=f"d-cr-{i}",
            event="pull_request_review",
            action="submitted",
            repo="huozhe/code-workflow",
            issue_num=9,
            sender="huozhegrok",
            payload=_realistic_changes_requested(head=static_head),
            status="deferred",
        )
        loop.process_deferred_batch()

    sess = store.get_session(sk)
    assert sess is not None
    assert sess["state"] == "PAUSED_HUMAN", sess
    reason = str(sess.get("paused_reason") or "")
    assert "zero thread" in reason, reason
    assert "fingerprint" not in reason
    # Fingerprint never armed (static head)
    assert not str(sess.get("progress_fp") or "").startswith("A:")
    assert posts and "@huozhe" in posts[0]
    store.close()


def test_thread_resolution_prevents_zero_thread_escalate(tmp_path: Path) -> None:
    """Control: resolving threads resets counter — no escalate on round 3."""
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
    # Round sequence: both open → both open → only one open (one resolved).
    snaps = [
        _snap(["t1", "t2"]),
        _snap(["t1", "t2"]),
        _snap(["t1"]),  # t2 resolved this round
    ]
    idx = {"i": 0}

    def fake_threads(**k):
        s = snaps[min(idx["i"], len(snaps) - 1)]
        idx["i"] += 1
        return s

    posts: list = []
    loop = SessionLoop(
        store,
        cfg,
        supervisor=None,
        dispatch_turns=False,
        post_comment=lambda **k: posts.append(1) or 1,
        gateway_token="gw",
        fetch_threads=fake_threads,
        fetch_diff=lambda **k: "frozen",
        github_token="tok",
    )
    head = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    for i in range(3):
        store.insert_delivery(
            delivery_id=f"d-ok-{i}",
            event="pull_request_review",
            action="submitted",
            repo="huozhe/code-workflow",
            issue_num=10,
            sender="huozhegrok",
            payload=_realistic_changes_requested(head=head),
            status="deferred",
        )
        loop.process_deferred_batch()

    sess = store.get_session(sk)
    assert sess["state"] != "PAUSED_HUMAN", sess.get("paused_reason")
    # After resolve on round 3, counter should be 0 (resolved > 0)
    assert int(sess["zero_thread_rounds"] or 0) == 0
    assert not posts
    store.close()


def test_missing_fetch_skips_signals_no_countdown(tmp_path: Path) -> None:
    """When GraphQL fails, do not escalate on a blind 3-round timer."""
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    now = 1_700_000_000
    sk = "huozhe/code-workflow#11"
    store.upsert_session(
        session_key=sk,
        repo="huozhe/code-workflow",
        issue_num=11,
        state="DESIGN_REVIEW",
        architect="huozheclaude",
        developer="huozhegrok",
        created_at=now,
        updated_at=now,
        design_pr=11,
    )
    posts: list = []
    loop = SessionLoop(
        store,
        cfg,
        supervisor=None,
        dispatch_turns=False,
        post_comment=lambda **k: posts.append(1) or 1,
        gateway_token="gw",
        fetch_threads=lambda **k: None,
        fetch_diff=lambda **k: None,
        github_token="tok",
    )
    head = "cccccccccccccccccccccccccccccccccccccccc"
    for i in range(5):
        store.insert_delivery(
            delivery_id=f"d-skip-{i}",
            event="pull_request_review",
            action="submitted",
            repo="huozhe/code-workflow",
            issue_num=11,
            sender="huozhegrok",
            payload=_realistic_changes_requested(head=head),
            status="deferred",
        )
        loop.process_deferred_batch()
    sess = store.get_session(sk)
    assert sess["state"] != "PAUSED_HUMAN"
    assert int(sess.get("zero_thread_rounds") or 0) == 0
    assert not posts
    store.close()


def test_schema_v4_stall_open_threads_column(tmp_path: Path) -> None:
    store = Store(tmp_path / "s.db")
    assert store._schema_version() == SCHEMA_VERSION
    assert "stall_open_threads" in store._table_columns("sessions")
    store.close()
