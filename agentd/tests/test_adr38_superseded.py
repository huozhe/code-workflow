"""ADR-38 / #214: superseded approval is not a permanent fault; defer has a clock."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

import agentd.design_loop as design_loop_mod
import agentd.verify as vmod
from agentd.config import Config
from agentd.db import SCHEMA_VERSION, Store
from agentd.design_loop import DesignLoop
from agentd.gitops import role_branch_name
from agentd.verify import verify_design_approval, verify_feature_merge

_REPO = "huozhe/code-workflow"
_HEAD = "abc123def456"
_OLD = "oldsha0000001"


@pytest.fixture(autouse=True)
def _clear() -> None:
    design_loop_mod._spent_prs.clear()
    design_loop_mod._merge_auth_attempts.clear()
    yield
    design_loop_mod._spent_prs.clear()
    design_loop_mod._merge_auth_attempts.clear()


def _cfg(tmp: Path, *, required_checks: list[str] | None = None) -> Config:
    return Config(
        raw={
            "host": {"owner": "huozhe"},
            "gateway": {"login": "huozhegateway"},
            "agents": {
                "claude": {"login": "huozheclaude"},
                "grok": {"login": "huozhegrok"},
            },
            "repos": {_REPO: {"required_checks": required_checks or []}},
        },
        root=tmp,
    )


def _feat_ref(issue: int = 57) -> str:
    return role_branch_name(_REPO, issue, "developer")


def _arch_ref(issue: int = 57) -> str:
    return role_branch_name(_REPO, issue, "architect")


def _rev(login: str, state: str, head: str = _HEAD) -> dict[str, Any]:
    return {"user": {"login": login}, "state": state, "commit_id": head}


def _reviews_get(
    reviews: list[dict[str, Any]],
    *,
    head: str = _HEAD,
    mergeable_state: str = "clean",
    failed_check: bool = False,
):
    def fake_get(url: str, *, token: str):
        if url.endswith("/reviews"):
            return reviews
        if "/pulls/" in url and not url.endswith("/reviews"):
            return {
                "number": 213,
                "head": {"sha": head},
                "mergeable_state": mergeable_state,
            }
        if url.endswith("/check-runs"):
            conclusion = "failure" if failed_check else "success"
            return {
                "check_runs": [
                    {
                        "name": "pytest",
                        "status": "completed",
                        "conclusion": conclusion,
                    }
                ]
            }
        if url.endswith("/status"):
            return {"statuses": [], "state": "failure" if failed_check else "success"}
        raise AssertionError(url)

    return fake_get


def test_changes_requested_is_superseded_not_permanent() -> None:
    """(1)/(3) helper: latest CR → superseded, not a retry, not ok."""
    c = verify_feature_merge(
        repo=_REPO,
        pr_number=213,
        expected_approver_login="huozheclaude",
        head_sha=_HEAD,
        token="tok",
        required_checks=[],
        http_get=_reviews_get(
            [_rev("huozheclaude", "APPROVED"), _rev("huozheclaude", "CHANGES_REQUESTED")]
        ),
    )
    assert c.ok is False
    assert c.transient is False
    assert c.superseded is True


def test_dismissed_is_superseded() -> None:
    """(3) DISMISSED is the same classification as CHANGES_REQUESTED."""
    c = verify_feature_merge(
        repo=_REPO,
        pr_number=213,
        expected_approver_login="huozheclaude",
        head_sha=_HEAD,
        token="tok",
        required_checks=[],
        http_get=_reviews_get([_rev("huozheclaude", "DISMISSED")]),
    )
    assert c.ok is False
    assert c.superseded is True
    assert c.transient is False


def test_approved_on_stale_sha_is_superseded() -> None:
    """(4) APPROVED on X, live head Y → superseded."""
    c = verify_feature_merge(
        repo=_REPO,
        pr_number=213,
        expected_approver_login="huozheclaude",
        head_sha=_HEAD,
        token="tok",
        required_checks=[],
        http_get=_reviews_get(
            [_rev("huozheclaude", "APPROVED", head=_OLD)],
            head=_HEAD,
        ),
    )
    assert c.ok is False
    assert c.superseded is True
    assert c.transient is False


def test_no_review_still_escalates_classification() -> None:
    """(5) no review from the expected approver is permanent, not superseded."""
    c = verify_feature_merge(
        repo=_REPO,
        pr_number=213,
        expected_approver_login="huozheclaude",
        head_sha=_HEAD,
        token="tok",
        required_checks=[],
        http_get=_reviews_get([]),
    )
    assert c.ok is False
    assert c.superseded is False
    assert c.transient is False
    assert "no review" in c.reason


def test_failed_required_check_is_not_superseded() -> None:
    """(5) concluded failure still permanent. Anti-permissive."""
    c = verify_feature_merge(
        repo=_REPO,
        pr_number=213,
        expected_approver_login="huozheclaude",
        head_sha=_HEAD,
        token="tok",
        required_checks=["pytest"],
        http_get=_reviews_get(
            [_rev("huozheclaude", "APPROVED")],
            failed_check=True,
        ),
    )
    assert c.ok is False
    assert c.superseded is False
    assert c.transient is False


def test_commented_does_not_displace_approved() -> None:
    """(7) [APPROVED@head, COMMENTED@head] still authorizes. Order matters."""
    c = verify_feature_merge(
        repo=_REPO,
        pr_number=213,
        expected_approver_login="huozheclaude",
        head_sha=_HEAD,
        token="tok",
        required_checks=[],
        http_get=_reviews_get(
            [_rev("huozheclaude", "APPROVED"), _rev("huozheclaude", "COMMENTED")]
        ),
    )
    assert c.ok is True
    assert c.superseded is False


def test_design_half_changes_requested_is_superseded() -> None:
    """(6) same helper, design caller."""
    c = verify_design_approval(
        repo=_REPO,
        pr_number=207,
        expected_approver_login="huozhegrok",
        head_sha=_HEAD,
        token="tok",
        http_get=_reviews_get(
            [_rev("huozhegrok", "APPROVED"), _rev("huozhegrok", "CHANGES_REQUESTED")]
        ),
    )
    assert c.ok is False
    assert c.superseded is True


def _seed(
    store: Store,
    *,
    state: str = "CODE_REVIEW",
    issue: int = 57,
    feature_pr: int = 213,
    design_pr: int = 207,
    paused_reason: str | None = None,
    resume_state: str | None = None,
    silent_turns: int = 0,
) -> str:
    sk = f"{_REPO}#{issue}"
    store.upsert_session(
        session_key=sk,
        project_key=_REPO,
        repo=_REPO,
        issue_num=issue,
        state=state,
        architect="huozheclaude",
        developer="huozhegrok",
        created_at=1,
        updated_at=1,
        design_pr=design_pr,
    )
    fields: dict[str, Any] = {"feature_pr": feature_pr, "silent_turns": silent_turns}
    if paused_reason is not None:
        fields["paused_reason"] = paused_reason
    if resume_state is not None:
        fields["resume_state"] = resume_state
    store.update_session_fields(sk, **fields)
    return sk


def _insert_review(
    store: Store,
    *,
    did: str,
    state: str,
    sender: str,
    pr: int,
    ref: str,
    head: str = _HEAD,
    author: str,
    issue_session: int = 57,
) -> None:
    store.insert_delivery(
        delivery_id=did,
        event="pull_request_review",
        action="submitted",
        repo=_REPO,
        issue_num=pr,
        sender=sender,
        payload=json.dumps(
            {
                "action": "submitted",
                "review": {"id": 1, "state": state, "commit_id": head},
                "pull_request": {
                    "number": pr,
                    "user": {"login": author},
                    "head": {"sha": head, "ref": ref},
                },
                "repository": {"full_name": _REPO},
                "sender": {"login": sender},
            }
        ).encode(),
        status="deferred",
    )


class _Sup:
    def ensure_session(self, **kwargs: Any) -> None:
        return None


def _loop(store: Store, tmp: Path, *, fake_get: Any) -> DesignLoop:
    real_feat = vmod.verify_feature_merge
    real_des = vmod.verify_design_approval

    def patch_feat(**kwargs: Any):
        kwargs = dict(kwargs)
        kwargs["http_get"] = fake_get
        kwargs["token"] = "tok"
        return real_feat(**kwargs)

    def patch_des(**kwargs: Any):
        kwargs = dict(kwargs)
        kwargs["http_get"] = fake_get
        kwargs["token"] = "tok"
        return real_des(**kwargs)

    vmod.verify_feature_merge = patch_feat  # type: ignore[assignment]
    vmod.verify_design_approval = patch_des  # type: ignore[assignment]
    posts: list[str] = []
    loop = DesignLoop(
        store,
        _cfg(tmp),
        supervisor=_Sup(),
        dispatch_turns=True,
        post_comment=lambda **k: posts.append(str(k.get("body") or "")) or 1,
        gateway_token="gw",
        github_token="tok",
        fetch_threads=lambda **k: None,
        fetch_diff=lambda **k: None,
    )
    dispatched: list[dict[str, Any]] = []

    def _spy(**kw: Any) -> dict[str, Any]:
        dispatched.append(kw)
        return {"status": "done", "summary": "ok", "public_actions": [{"n": 1}]}

    loop._dispatch_turn = _spy  # type: ignore[method-assign]
    loop.dispatched = dispatched  # type: ignore[attr-defined]
    loop.posts = posts  # type: ignore[attr-defined]
    loop._unpatch = (real_feat, real_des)  # type: ignore[attr-defined]
    return loop


def _unpatch(loop: DesignLoop) -> None:
    real_feat, real_des = loop._unpatch  # type: ignore[attr-defined]
    vmod.verify_feature_merge = real_feat
    vmod.verify_design_approval = real_des


def _status(store: Store, did: str) -> str:
    row = store._conn.execute(
        "SELECT status FROM deliveries WHERE delivery_id=?", (did,)
    ).fetchone()
    assert row is not None
    return str(row["status"])


def _esc_count(store: Store) -> int:
    row = store._conn.execute("SELECT COUNT(*) AS n FROM escalations").fetchone()
    return int(row["n"]) if row else 0


def test_feature_cr_delivery_is_dropped_session_unpaused(tmp_path: Path) -> None:
    """(1) feature_approved_unverified + live CR → dropped, CODE_REVIEW, no pause."""
    store = Store(tmp_path / "state.db")
    sk = _seed(store)
    fake = _reviews_get(
        [_rev("huozheclaude", "APPROVED"), _rev("huozheclaude", "CHANGES_REQUESTED")]
    )
    loop = _loop(store, tmp_path, fake_get=fake)
    try:
        _insert_review(
            store,
            did="d-appr",
            state="APPROVED",
            sender="huozheclaude",
            pr=213,
            ref=_feat_ref(),
            author="huozhegrok",
        )
        loop.process_deferred_batch()
    finally:
        _unpatch(loop)
    sess = store.get_session(sk)
    assert sess is not None
    assert _status(store, "d-appr") == "dropped"
    assert sess["state"] == "CODE_REVIEW"
    assert sess.get("paused_reason") is None
    assert _esc_count(store) == 0
    store.close()


def test_approval_then_cr_one_batch_one_rework_turn(tmp_path: Path) -> None:
    """(2) live shape: two deliveries, one batch, approval first, one Developer turn."""
    store = Store(tmp_path / "state.db")
    sk = _seed(store, silent_turns=0)
    fake = _reviews_get(
        [_rev("huozheclaude", "APPROVED"), _rev("huozheclaude", "CHANGES_REQUESTED")]
    )
    loop = _loop(store, tmp_path, fake_get=fake)
    try:
        _insert_review(
            store,
            did="d-appr",
            state="APPROVED",
            sender="huozheclaude",
            pr=213,
            ref=_feat_ref(),
            author="huozhegrok",
        )
        _insert_review(
            store,
            did="d-cr",
            state="CHANGES_REQUESTED",
            sender="huozheclaude",
            pr=213,
            ref=_feat_ref(),
            author="huozhegrok",
        )
        store._conn.execute(
            "UPDATE deliveries SET received_at = 10 WHERE delivery_id = 'd-appr'"
        )
        store._conn.execute(
            "UPDATE deliveries SET received_at = 20 WHERE delivery_id = 'd-cr'"
        )
        store._conn.commit()
        loop.process_deferred_batch()
    finally:
        _unpatch(loop)
    sess = store.get_session(sk)
    assert sess is not None
    assert _status(store, "d-appr") == "dropped"
    assert sess["state"] == "CODE_REWORK"
    assert sess.get("paused_reason") is None
    dispatched = loop.dispatched  # type: ignore[attr-defined]
    assert len(dispatched) == 1
    assert dispatched[0]["role"] == "developer"
    assert int(sess.get("silent_turns") or 0) == 0
    store.close()


def test_design_half_cr_drops_and_does_not_pause(tmp_path: Path) -> None:
    """(6) design_approved_unverified + Developer CR → dropped, unpaused."""
    store = Store(tmp_path / "state.db")
    sk = _seed(store, state="DESIGN_REVIEW")
    fake = _reviews_get(
        [_rev("huozhegrok", "APPROVED"), _rev("huozhegrok", "CHANGES_REQUESTED")]
    )
    loop = _loop(store, tmp_path, fake_get=fake)
    try:
        _insert_review(
            store,
            did="d-des",
            state="APPROVED",
            sender="huozhegrok",
            pr=207,
            ref=_arch_ref(),
            author="huozheclaude",
        )
        loop.process_deferred_batch()
    finally:
        _unpatch(loop)
    sess = store.get_session(sk)
    assert sess is not None
    assert _status(store, "d-des") == "dropped"
    assert sess["state"] == "DESIGN_REVIEW"
    assert sess.get("paused_reason") is None
    assert _esc_count(store) == 0
    store.close()


def _defer_row(store: Store, did: str) -> sqlite3.Row:
    row = store._conn.execute(
        "SELECT defer_count, next_attempt_at, status FROM deliveries WHERE delivery_id=?",
        (did,),
    ).fetchone()
    assert row is not None
    return row


def test_paused_defer_backs_off_and_is_skipped_in_between(tmp_path: Path) -> None:
    """(8) widening interval; list_deferred returns the row zero times while waiting."""
    store = Store(tmp_path / "state.db")
    _seed(
        store,
        state="PAUSED_HUMAN",
        paused_reason="unverified feature merge authorization (permanent): x",
        resume_state="CODE_REVIEW",
    )
    store.insert_delivery(
        delivery_id="d-park",
        event="issue_comment",
        action="created",
        repo=_REPO,
        issue_num=57,
        sender="huozheclaude",
        payload=json.dumps(
            {
                "action": "created",
                "issue": {"number": 57, "title": "t", "state": "open"},
                "comment": {"body": "note", "user": {"login": "huozheclaude"}},
                "repository": {"full_name": _REPO},
                "sender": {"login": "huozheclaude"},
            }
        ).encode(),
        status="deferred",
    )
    loop = DesignLoop(
        store,
        _cfg(tmp_path),
        supervisor=None,
        dispatch_turns=False,
        gateway_token="gw",
        github_token="tok",
    )
    waits: list[int] = []
    for i, expected in enumerate((5, 10, 20, 40, 80, 160, 300, 300), start=1):
        t0 = int(time.time())
        loop.process_deferred_batch()
        row = _defer_row(store, "d-park")
        assert int(row["defer_count"]) == i
        wait = int(row["next_attempt_at"]) - t0
        waits.append(wait)
        assert expected - 1 <= wait <= expected + 1
        if i == 1:
            ids = [str(r["delivery_id"]) for r in store.list_deferred(limit=20)]
            assert "d-park" not in ids
        store._conn.execute(
            "UPDATE deliveries SET next_attempt_at = 0 WHERE delivery_id = 'd-park'"
        )
        store._conn.commit()
    assert waits[0] <= 6
    store.close()


def test_parked_row_does_not_consume_list_deferred_head_slot(tmp_path: Path) -> None:
    """(9) 21 deferred, oldest parked, limit=20 → twenty-first is returned."""
    store = Store(tmp_path / "state.db")
    future = int(time.time()) + 3600
    for i in range(21):
        store.insert_delivery(
            delivery_id=f"d-{i}",
            event="ping",
            action=None,
            repo=_REPO,
            issue_num=i,
            sender="u",
            payload=b"{}",
            status="deferred",
        )
        store._conn.execute(
            "UPDATE deliveries SET received_at = ? WHERE delivery_id = ?",
            (i + 1, f"d-{i}"),
        )
    store._conn.execute(
        "UPDATE deliveries SET next_attempt_at = ? WHERE delivery_id = 'd-0'",
        (future,),
    )
    store._conn.commit()
    rows = store.list_deferred(limit=20)
    ids = [str(r["delivery_id"]) for r in rows]
    assert "d-0" not in ids
    assert "d-20" in ids
    assert len(ids) == 20
    store.close()


def test_owner_reply_clears_clock(tmp_path: Path) -> None:
    """(10) columns only after _resume_from_escalation."""
    store = Store(tmp_path / "state.db")
    _seed(
        store,
        state="PAUSED_HUMAN",
        paused_reason="unverified feature merge authorization (permanent): x",
        resume_state="CODE_REVIEW",
    )
    store.insert_delivery(
        delivery_id="d-park",
        event="issue_comment",
        action="created",
        repo=_REPO,
        issue_num=57,
        sender="huozheclaude",
        payload=json.dumps(
            {
                "action": "created",
                "issue": {"number": 57},
                "comment": {"body": "x", "user": {"login": "huozheclaude"}},
                "repository": {"full_name": _REPO},
                "sender": {"login": "huozheclaude"},
            }
        ).encode(),
        status="deferred",
    )
    store._conn.execute(
        "UPDATE deliveries SET next_attempt_at = ?, defer_count = 4 WHERE delivery_id = 'd-park'",
        (int(time.time()) + 300,),
    )
    store._conn.commit()
    store.insert_delivery(
        delivery_id="d-owner",
        event="issue_comment",
        action="created",
        repo=_REPO,
        issue_num=57,
        sender="huozhe",
        payload=json.dumps(
            {
                "action": "created",
                "issue": {"number": 57, "title": "t", "state": "open"},
                "comment": {"body": "go", "user": {"login": "huozhe"}},
                "repository": {"full_name": _REPO},
                "sender": {"login": "huozhe"},
            }
        ).encode(),
        status="deferred",
    )
    loop = DesignLoop(
        store,
        _cfg(tmp_path),
        supervisor=_Sup(),
        dispatch_turns=True,
        post_comment=lambda **k: 1,
        gateway_token="gw",
        github_token="tok",
        fetch_threads=lambda **k: None,
    )
    loop._dispatch_turn = lambda **k: {  # type: ignore[method-assign]
        "status": "done",
        "summary": "ok",
        "public_actions": [{"n": 1}],
    }
    loop.process_deferred_batch()
    row = _defer_row(store, "d-park")
    assert int(row["next_attempt_at"]) == 0
    assert int(row["defer_count"]) == 0
    store.close()


def test_next_drain_after_resume_routes_parked_row(tmp_path: Path) -> None:
    """(10′) next drain, not the same batch, routes the cleared row."""
    store = Store(tmp_path / "state.db")
    sk = _seed(
        store,
        state="PAUSED_HUMAN",
        paused_reason="unverified feature merge authorization (permanent): x",
        resume_state="CODE_REVIEW",
    )
    fake = _reviews_get([_rev("huozheclaude", "CHANGES_REQUESTED")])
    _insert_review(
        store,
        did="d-cr",
        state="CHANGES_REQUESTED",
        sender="huozheclaude",
        pr=213,
        ref=_feat_ref(),
        author="huozhegrok",
    )
    store._conn.execute(
        "UPDATE deliveries SET next_attempt_at = ?, defer_count = 3 WHERE delivery_id = 'd-cr'",
        (int(time.time()) + 300,),
    )
    store._conn.commit()
    store.insert_delivery(
        delivery_id="d-owner",
        event="issue_comment",
        action="created",
        repo=_REPO,
        issue_num=57,
        sender="huozhe",
        payload=json.dumps(
            {
                "action": "created",
                "issue": {"number": 57, "title": "t", "state": "open"},
                "comment": {"body": "go", "user": {"login": "huozhe"}},
                "repository": {"full_name": _REPO},
                "sender": {"login": "huozhe"},
            }
        ).encode(),
        status="deferred",
    )
    loop = _loop(store, tmp_path, fake_get=fake)
    try:
        loop.process_deferred_batch()
        sess = store.get_session(sk)
        assert sess is not None
        assert sess["state"] == "CODE_REVIEW"
        row = _defer_row(store, "d-cr")
        assert int(row["next_attempt_at"]) == 0
        dispatched_after_resume = list(loop.dispatched)  # type: ignore[attr-defined]
        loop.process_deferred_batch()
    finally:
        _unpatch(loop)
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["state"] == "CODE_REWORK"
    dispatched = loop.dispatched  # type: ignore[attr-defined]
    assert len(dispatched) > len(dispatched_after_resume)
    assert any(d["role"] == "developer" for d in dispatched)
    store.close()


def test_v10_to_v11_preserves_deliveries(tmp_path: Path) -> None:
    """(11) migration preserves every deliveries row and its status."""
    path = tmp_path / "state.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE deliveries (
          delivery_id TEXT PRIMARY KEY,
          event TEXT NOT NULL,
          action TEXT,
          repo TEXT NOT NULL DEFAULT '',
          issue_num INTEGER,
          sender TEXT NOT NULL DEFAULT '',
          received_at INTEGER NOT NULL,
          payload BLOB NOT NULL,
          status TEXT NOT NULL DEFAULT 'queued'
        );
        CREATE TABLE sessions (
          session_key TEXT PRIMARY KEY,
          project_key TEXT NOT NULL,
          repo TEXT NOT NULL,
          issue_num INTEGER NOT NULL,
          state TEXT NOT NULL,
          paused_reason TEXT,
          resume_state TEXT,
          stall_open_threads TEXT,
          architect TEXT NOT NULL,
          developer TEXT NOT NULL,
          roles_locked INTEGER NOT NULL DEFAULT 0,
          design_pr INTEGER,
          feature_pr INTEGER,
          design_pr_head TEXT,
          feature_pr_head TEXT,
          turn_count INTEGER NOT NULL DEFAULT 0,
          consec_agent_turns INTEGER NOT NULL DEFAULT 0,
          review_rounds INTEGER NOT NULL DEFAULT 0,
          progress_fp TEXT,
          progress_repeat INTEGER NOT NULL DEFAULT 0,
          zero_thread_rounds INTEGER NOT NULL DEFAULT 0,
          silent_turns INTEGER NOT NULL DEFAULT 0,
          gh_watermark INTEGER,
          verified_at INTEGER,
          classification TEXT,
          closed_issue_escalated_at INTEGER,
          created_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL
        );
        CREATE TABLE circuit_breaker (
          id INTEGER PRIMARY KEY CHECK (id = 1),
          disk_paused INTEGER NOT NULL DEFAULT 0,
          reason TEXT,
          updated_at INTEGER NOT NULL
        );
        INSERT INTO circuit_breaker(id, disk_paused, reason, updated_at)
        VALUES (1, 0, NULL, 0);
        INSERT INTO deliveries(
          delivery_id, event, action, repo, issue_num, sender,
          received_at, payload, status
        ) VALUES
          ('d-queued', 'ping', NULL, '', NULL, 'u', 1, X'7B7D', 'queued'),
          ('d-deferred', 'ping', NULL, '', NULL, 'u', 2, X'7B7D', 'deferred'),
          ('d-routed', 'ping', NULL, '', NULL, 'u', 3, X'7B7D', 'routed'),
          ('d-dropped', 'ping', NULL, '', NULL, 'u', 4, X'7B7D', 'dropped'),
          ('d-done', 'ping', NULL, '', NULL, 'u', 5, X'7B7D', 'done'),
          ('d-failed', 'ping', NULL, '', NULL, 'u', 6, X'7B7D', 'failed');
        PRAGMA user_version = 10;
        """
    )
    conn.commit()
    conn.close()

    store = Store(path)
    assert store._schema_version() == 11
    assert SCHEMA_VERSION == 11
    assert store.delivery_count() == 6
    counts = store.count_by_status()
    assert counts.get("queued") == 1
    assert counts.get("deferred") == 1
    assert counts.get("routed") == 1
    assert counts.get("dropped") == 1
    assert counts.get("done") == 1
    assert counts.get("failed") == 1
    cols = store._table_columns("deliveries")
    assert "next_attempt_at" in cols
    assert "defer_count" in cols
    store.close()
