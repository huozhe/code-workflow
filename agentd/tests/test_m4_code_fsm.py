"""M4-1: code-half FSM + Feature PR by developer branch (§8.1 / #51)."""

from __future__ import annotations

import json
from pathlib import Path

from agentd.config import Config
from agentd.db import Store
from agentd.design_loop import DesignLoop
from agentd.fsm import CODE_STATES, transition
from agentd.gitops import is_feature_head_ref, role_branch_name


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


def _feature_ref(issue: int = 100) -> str:
    return role_branch_name("huozhe/code-workflow", issue, "developer")


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


def test_code_half_fsm_table() -> None:
    assert transition("IMPLEMENTING", "feature_pr_opened").new_state == "CODE_REVIEW"
    assert (
        transition("CODE_REVIEW", "code_changes_requested").new_state == "CODE_REWORK"
    )
    assert transition("CODE_REWORK", "feature_revised").new_state == "CODE_REVIEW"
    assert transition("CODE_REVIEW", "merge_authorized").new_state == "MERGING"
    assert (
        transition("MERGING", "feature_merged").new_state == "AWAITING_VERIFICATION"
    )
    # Re-enterable: second Feature PR returns to IMPLEMENTING (§8.1 / #51)
    t = transition("AWAITING_VERIFICATION", "feature_pr_opened")
    assert t is not None
    assert t.new_state == "IMPLEMENTING"
    assert "CODE_REVIEW" in CODE_STATES
    assert "AWAITING_VERIFICATION" in CODE_STATES


def test_is_feature_head_ref_developer_branch_only() -> None:
    repo = "huozhe/code-workflow"
    assert is_feature_head_ref(role_branch_name(repo, 7, "developer"), repo)
    assert not is_feature_head_ref(role_branch_name(repo, 7, "architect"), repo)
    assert not is_feature_head_ref("feat/other", repo)


def test_feature_pr_open_by_developer_branch_not_title(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    sk = "huozhe/code-workflow#100"
    store.upsert_session(
        session_key=sk,
        project_key="huozhe/code-workflow",
        repo="huozhe/code-workflow",
        issue_num=100,
        state="IMPLEMENTING",
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
    loop = DesignLoop(store, cfg, supervisor=None, dispatch_turns=False)
    feat_ref = _feature_ref(100)
    _insert(
        store,
        did="d-feat-open",
        event="pull_request",
        action="opened",
        sender="huozhegrok",
        issue=60,  # PR number
        payload={
            "action": "opened",
            "pull_request": {
                "number": 60,
                "title": "Add caching",  # no "feature" keyword required
                "head": {"sha": "abc", "ref": feat_ref},
            },
            "repository": {"full_name": "huozhe/code-workflow"},
            "sender": {"login": "huozhegrok"},
        },
    )
    loop.process_deferred_batch()
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["state"] == "CODE_REVIEW"
    assert int(sess.get("feature_pr") or 0) == 60
    store.close()


def test_code_review_rework_round(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    sk = "huozhe/code-workflow#100"
    feat_ref = _feature_ref(100)
    store.upsert_session(
        session_key=sk,
        project_key="huozhe/code-workflow",
        repo="huozhe/code-workflow",
        issue_num=100,
        state="CODE_REVIEW",
        architect="huozheclaude",
        developer="huozhegrok",
        created_at=1,
        updated_at=1,
    )
    store.update_session_fields(sk, feature_pr=60)
    store.upsert_runner(
        "huozhe/code-workflow",
        container_id="c1",
        endpoint="127.0.0.1:9",
        token="tok",
        tier="hot",
    )
    loop = DesignLoop(store, cfg, supervisor=None, dispatch_turns=False)
    head = "deadbeef"

    _insert(
        store,
        did="d-cr",
        event="pull_request_review",
        action="submitted",
        sender="huozheclaude",
        issue=60,
        payload={
            "action": "submitted",
            "review": {"id": 1, "state": "changes_requested", "commit_id": head},
            "pull_request": {
                "number": 60,
                "head": {"sha": head, "ref": feat_ref},
            },
            "repository": {"full_name": "huozhe/code-workflow"},
            "sender": {"login": "huozheclaude"},
        },
    )
    loop.process_deferred_batch()
    assert store.get_session(sk)["state"] == "CODE_REWORK"

    _insert(
        store,
        did="d-push",
        event="pull_request",
        action="synchronize",
        sender="huozhegrok",
        issue=60,
        payload={
            "action": "synchronize",
            "pull_request": {
                "number": 60,
                "head": {"sha": "cafebabe", "ref": feat_ref},
            },
            "repository": {"full_name": "huozhe/code-workflow"},
            "sender": {"login": "huozhegrok"},
        },
    )
    loop.process_deferred_batch()
    assert store.get_session(sk)["state"] == "CODE_REVIEW"
    store.close()


def test_merging_to_awaiting_on_feature_merged(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    sk = "huozhe/code-workflow#100"
    feat_ref = _feature_ref(100)
    store.upsert_session(
        session_key=sk,
        project_key="huozhe/code-workflow",
        repo="huozhe/code-workflow",
        issue_num=100,
        state="MERGING",
        architect="huozheclaude",
        developer="huozhegrok",
        created_at=1,
        updated_at=1,
    )
    store.update_session_fields(sk, feature_pr=60)
    store.upsert_runner(
        "huozhe/code-workflow",
        container_id="c1",
        endpoint="127.0.0.1:9",
        token="tok",
        tier="hot",
    )
    loop = DesignLoop(store, cfg, supervisor=None, dispatch_turns=False)
    _insert(
        store,
        did="d-merge",
        event="pull_request",
        action="closed",
        sender="huozhegrok",
        issue=60,
        payload={
            "action": "closed",
            "pull_request": {
                "number": 60,
                "merged": True,
                "head": {"sha": "abc", "ref": feat_ref},
            },
            "repository": {"full_name": "huozhe/code-workflow"},
            "sender": {"login": "huozhegrok"},
        },
    )
    loop.process_deferred_batch()
    assert store.get_session(sk)["state"] == "AWAITING_VERIFICATION"
    store.close()


def test_awaiting_verification_reenter_implementing(tmp_path: Path) -> None:
    """§8.1: second Feature PR under one issue returns to IMPLEMENTING."""
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    sk = "huozhe/code-workflow#100"
    store.upsert_session(
        session_key=sk,
        project_key="huozhe/code-workflow",
        repo="huozhe/code-workflow",
        issue_num=100,
        state="AWAITING_VERIFICATION",
        architect="huozheclaude",
        developer="huozhegrok",
        created_at=1,
        updated_at=1,
    )
    store.update_session_fields(sk, feature_pr=60)
    store.upsert_runner(
        "huozhe/code-workflow",
        container_id="c1",
        endpoint="127.0.0.1:9",
        token="tok",
        tier="hot",
    )
    loop = DesignLoop(store, cfg, supervisor=None, dispatch_turns=False)
    feat_ref = _feature_ref(100)
    _insert(
        store,
        did="d-feat2",
        event="pull_request",
        action="opened",
        sender="huozhegrok",
        issue=61,
        payload={
            "action": "opened",
            "pull_request": {
                "number": 61,
                "title": "More work",
                "head": {"sha": "new", "ref": feat_ref},
            },
            "repository": {"full_name": "huozhe/code-workflow"},
            "sender": {"login": "huozhegrok"},
        },
    )
    loop.process_deferred_batch()
    sess = store.get_session(sk)
    assert sess["state"] == "IMPLEMENTING"
    assert int(sess.get("feature_pr") or 0) == 61
    store.close()


def test_feature_merged_wrong_pr_dropped(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    sk = "huozhe/code-workflow#100"
    feat_ref = _feature_ref(100)
    store.upsert_session(
        session_key=sk,
        project_key="huozhe/code-workflow",
        repo="huozhe/code-workflow",
        issue_num=100,
        state="MERGING",
        architect="huozheclaude",
        developer="huozhegrok",
        created_at=1,
        updated_at=1,
    )
    store.update_session_fields(sk, feature_pr=60)
    loop = DesignLoop(store, cfg, supervisor=None, dispatch_turns=False)
    _insert(
        store,
        did="d-wrong",
        event="pull_request",
        action="closed",
        sender="huozhegrok",
        issue=99,
        payload={
            "action": "closed",
            "pull_request": {
                "number": 99,
                "merged": True,
                "head": {"sha": "x", "ref": feat_ref},
            },
            "repository": {"full_name": "huozhe/code-workflow"},
            "sender": {"login": "huozhegrok"},
        },
    )
    loop.process_deferred_batch()
    # Wrong PR: either dropped or state unchanged
    assert store.get_session(sk)["state"] == "MERGING"
    store.close()


def test_pick_recipient_code_half() -> None:
    loop = DesignLoop.__new__(DesignLoop)
    role, login = DesignLoop._pick_recipient(
        loop, "feature_pr_opened", "IMPLEMENTING", "arch", "dev", "dev"
    )
    assert role == "architect" and login == "arch"
    role, login = DesignLoop._pick_recipient(
        loop, "code_changes_requested", "CODE_REVIEW", "arch", "dev", "arch"
    )
    assert role == "developer" and login == "dev"
    role, login = DesignLoop._pick_recipient(
        loop, "merge_authorized", "CODE_REVIEW", "arch", "dev", "arch"
    )
    assert role == "developer" and login == "dev"
