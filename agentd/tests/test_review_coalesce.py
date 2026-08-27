"""#49: one review wakes one turn — inline comments/threads are terminal parts."""

from __future__ import annotations

import json
from pathlib import Path

from agentd.config import Config
from agentd.db import Store
from agentd.digest import build_digest
from agentd.gitops import role_branch_name
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
            "intake": {"mode": "all", "actors": "collaborators"},
        },
        root=tmp,
    )


def _design_ref(issue: int = 47) -> str:
    return role_branch_name("huozhe/code-workflow", issue, "architect")


def _seed_session(store: Store, *, issue: int = 47, pr: int = 48) -> str:
    sk = f"huozhe/code-workflow#{issue}"
    store.upsert_session(
        session_key=sk,
        project_key="huozhe/code-workflow",
        repo="huozhe/code-workflow",
        issue_num=issue,
        state="DESIGN_REVIEW",
        architect="huozheclaude",
        developer="huozhegrok",
        created_at=1,
        updated_at=1,
        design_pr=pr,
    )
    store.upsert_runner(
        "huozhe/code-workflow",
        container_id="c-fake",
        endpoint="127.0.0.1:9",
        token="tok",
        tier="hot",
    )
    return sk


def _insert(
    store: Store,
    *,
    did: str,
    event: str,
    action: str | None,
    payload: dict,
    sender: str,
    issue: int,
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


def test_seven_inline_comments_plus_review_is_one_routed_turn(tmp_path: Path) -> None:
    """#47 baseline: 7 comments → 7 empty turns. After #49: comments done, 1 review routed."""
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    issue, pr = 47, 48
    sk = _seed_session(store, issue=issue, pr=pr)
    design_ref = _design_ref(issue)
    head = "deadbeef"

    loop = SessionLoop(store, cfg, supervisor=None, dispatch_turns=False)

    for i in range(7):
        _insert(
            store,
            did=f"d-cmt-{i}",
            event="pull_request_review_comment",
            action="created",
            sender="huozhegrok",
            issue=pr,
            payload={
                "action": "created",
                "pull_request": {
                    "number": pr,
                    "title": "Design RFC",
                    "head": {"sha": head, "ref": design_ref},
                },
                "comment": {
                    "id": 1000 + i,
                    "body": f"nit {i}",
                    "pull_request_review_id": 9001,
                    "path": "docs/design.md",
                },
                "repository": {"full_name": "huozhe/code-workflow"},
                "sender": {"login": "huozhegrok"},
            },
        )
        _insert(
            store,
            did=f"d-thr-{i}",
            event="pull_request_review_thread",
            action="resolved",
            sender="huozheclaude",
            issue=pr,
            payload={
                "action": "resolved",
                "pull_request": {
                    "number": pr,
                    "head": {"sha": head, "ref": design_ref},
                },
                "thread": {"id": f"PRRT_{i}", "is_resolved": True},
                "repository": {"full_name": "huozhe/code-workflow"},
                "sender": {"login": "huozheclaude"},
            },
        )

    _insert(
        store,
        did="d-review",
        event="pull_request_review",
        action="submitted",
        sender="huozhegrok",
        issue=pr,
        payload={
            "action": "submitted",
            "review": {
                "id": 9001,
                "state": "changes_requested",
                "body": "please address nits",
                "commit_id": head,
            },
            "pull_request": {
                "number": pr,
                "title": "Design RFC",
                "head": {"sha": head, "ref": design_ref},
            },
            "repository": {"full_name": "huozhe/code-workflow"},
            "sender": {"login": "huozhegrok"},
        },
    )

    # Process all 15 deliveries (7 comments + 7 threads + 1 review)
    n = loop.process_deferred_batch(limit=50)
    assert n == 15

    counts = store.count_by_status()
    # Parts terminal as done — never left deferred, never routed as turns
    assert counts.get("deferred", 0) == 0
    assert counts.get("done", 0) == 14  # 7 comments + 7 threads
    assert counts.get("routed", 0) == 1  # the review.submitted only
    assert counts.get("dropped", 0) == 0

    # FSM advanced once on the review verdict
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["state"] == "DESIGN_REWORK"
    store.close()


def test_review_part_without_session_still_not_stuck_deferred(tmp_path: Path) -> None:
    """No session → existing no-session drop; must not leave deferred forever."""
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    loop = SessionLoop(store, cfg, supervisor=None, dispatch_turns=False)
    _insert(
        store,
        did="d-orphan-cmt",
        event="pull_request_review_comment",
        action="created",
        sender="huozhegrok",
        issue=99,
        payload={
            "action": "created",
            "pull_request": {
                "number": 99,
                "head": {"sha": "x", "ref": "feat/other"},
            },
            "comment": {"id": 1, "body": "x"},
            "repository": {"full_name": "huozhe/code-workflow"},
            "sender": {"login": "huozhegrok"},
        },
    )
    loop.process_deferred_batch()
    # No session and not intake → dropped (pre-existing path)
    assert store.count_by_status().get("dropped", 0) == 1
    assert store.count_by_status().get("deferred", 0) == 0
    store.close()


def test_review_submitted_commented_still_routes_once(tmp_path: Path) -> None:
    """Standalone 'Add single comment' still wakes via review.submitted (COMMENTED)."""
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    issue, pr = 47, 48
    _seed_session(store, issue=issue, pr=pr)
    design_ref = _design_ref(issue)
    loop = SessionLoop(store, cfg, supervisor=None, dispatch_turns=False)

    _insert(
        store,
        did="d-single-cmt",
        event="pull_request_review_comment",
        action="created",
        sender="huozhegrok",
        issue=pr,
        payload={
            "action": "created",
            "pull_request": {
                "number": pr,
                "head": {"sha": "abc", "ref": design_ref},
            },
            "comment": {"id": 55, "body": "one thought", "pull_request_review_id": 77},
            "repository": {"full_name": "huozhe/code-workflow"},
            "sender": {"login": "huozhegrok"},
        },
    )
    _insert(
        store,
        did="d-single-rev",
        event="pull_request_review",
        action="submitted",
        sender="huozhegrok",
        issue=pr,
        payload={
            "action": "submitted",
            "review": {"id": 77, "state": "commented", "body": "one thought"},
            "pull_request": {
                "number": pr,
                "head": {"sha": "abc", "ref": design_ref},
            },
            "repository": {"full_name": "huozhe/code-workflow"},
            "sender": {"login": "huozhegrok"},
        },
    )
    loop.process_deferred_batch(limit=10)
    counts = store.count_by_status()
    assert counts.get("done", 0) == 1  # the part
    assert counts.get("routed", 0) == 1  # the review
    # COMMENTED does not advance FSM
    assert store.get_session(f"huozhe/code-workflow#{issue}")["state"] == "DESIGN_REVIEW"
    store.close()


def test_digest_review_carries_comment_ref() -> None:
    d = build_digest(
        event="pull_request_review",
        action="submitted",
        repo="o/r",
        issue_num=1,
        sender="dev",
        payload={
            "review": {"id": 42, "state": "changes_requested"},
            "pull_request": {"number": 9, "head": {"sha": "s", "ref": "b"}},
        },
    )
    assert d["review_id"] == 42
    assert "42" in str(d.get("review_comments"))
    assert "comments" in str(d.get("review_comments"))
