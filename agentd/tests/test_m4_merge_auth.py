"""M4-2: merge_authorized wiring + unauthorized feature_merged escalate."""

from __future__ import annotations

import json
from pathlib import Path

from agentd.config import Config
from agentd.db import Store
from agentd.gitops import role_branch_name
from agentd.session_loop import SessionLoop


def _cfg(tmp: Path, *, required_checks: list[str] | None = None) -> Config:
    raw: dict = {
        "host": {"owner": "huozhe"},
        "gateway": {"login": "huozhegateway"},
        "agents": {
            "claude": {"login": "huozheclaude"},
            "grok": {"login": "huozhegrok"},
        },
        "intake": {"mode": "all", "actors": "collaborators"},
        "repos": {
            "huozhe/code-workflow": {
                "required_checks": required_checks
                if required_checks is not None
                else ["ci/test"],
            }
        },
    }
    return Config(raw=raw, root=tmp)


def _feature_ref(issue: int = 100) -> str:
    return role_branch_name("huozhe/code-workflow", issue, "developer")


def _seed(
    store: Store,
    *,
    state: str = "CODE_REVIEW",
    issue: int = 100,
    pr: int = 60,
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
    store.update_session_fields(sk, feature_pr=pr)
    store.upsert_runner(
        "huozhe/code-workflow",
        container_id="c1",
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


def test_config_required_checks() -> None:
    cfg = Config(
        raw={"repos": {"o/r": {"required_checks": ["ci/test", "lint"]}}},
        root=Path("/tmp"),
    )
    assert cfg.required_checks("o/r") == ["ci/test", "lint"]
    assert cfg.required_checks("missing") == []


def test_architect_approve_emits_merge_authorized(tmp_path: Path) -> None:
    """Architect APPROVED + full §8.4 → CODE_REVIEW → MERGING (Developer acts)."""
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    sk = _seed(store, state="CODE_REVIEW")
    feat_ref = _feature_ref(100)
    head = "abc123def456"
    posts: list[str] = []

    def fake_get(url: str, *, token: str):
        if url.endswith("/reviews"):
            return [
                {
                    "user": {"login": "huozheclaude"},
                    "state": "APPROVED",
                    "commit_id": head,
                }
            ]
        if "/pulls/60" in url and not url.endswith("/reviews"):
            return {
                "number": 60,
                "head": {"sha": head, "ref": feat_ref},
                "mergeable_state": "clean",
            }
        if url.endswith("/check-runs"):
            return {
                "check_runs": [
                    {
                        "name": "ci/test",
                        "status": "completed",
                        "conclusion": "success",
                    }
                ]
            }
        if url.endswith("/status"):
            return {"statuses": [], "state": "success"}
        raise AssertionError(url)

    import agentd.verify as vmod

    real = vmod.verify_feature_merge

    def patched(**kwargs):
        kwargs = dict(kwargs)
        kwargs["http_get"] = fake_get
        kwargs["token"] = kwargs.get("token") or "tok"
        return real(**kwargs)

    vmod.verify_feature_merge = patched  # type: ignore[assignment]
    try:
        loop = SessionLoop(
            store,
            cfg,
            supervisor=None,
            dispatch_turns=False,
            post_comment=lambda **k: posts.append(k.get("body", "")) or 1,
            gateway_token="gw",
            get_issue_body_fn=lambda **k: "",
            patch_issue_body_fn=lambda **k: None,
        )
        _insert(
            store,
            did="d-feat-approve",
            event="pull_request_review",
            action="submitted",
            sender="huozheclaude",
            issue=60,
            payload={
                "action": "submitted",
                "review": {
                    "id": 9,
                    "state": "APPROVED",
                    "commit_id": head,
                },
                "pull_request": {
                    "number": 60,
                    "head": {"sha": head, "ref": feat_ref},
                },
                "repository": {"full_name": "huozhe/code-workflow"},
                "sender": {"login": "huozheclaude"},
            },
        )
        loop.process_deferred_batch()
    finally:
        vmod.verify_feature_merge = real  # type: ignore[assignment]

    sess = store.get_session(sk)
    assert sess is not None
    assert sess["state"] == "MERGING"
    # No escalation
    assert not sess.get("paused_reason")
    store.close()


def test_failed_check_escalates_permanent(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    sk = _seed(store, state="CODE_REVIEW")
    feat_ref = _feature_ref(100)
    head = "abc123"
    posts: list[str] = []

    def fake_get(url: str, *, token: str):
        if url.endswith("/reviews"):
            return [
                {
                    "user": {"login": "huozheclaude"},
                    "state": "APPROVED",
                    "commit_id": head,
                }
            ]
        if "/pulls/60" in url:
            return {
                "number": 60,
                "head": {"sha": head, "ref": feat_ref},
                "mergeable_state": "clean",
            }
        if url.endswith("/check-runs"):
            return {
                "check_runs": [
                    {
                        "name": "ci/test",
                        "status": "completed",
                        "conclusion": "failure",
                    }
                ]
            }
        if url.endswith("/status"):
            return {"statuses": [], "state": "failure"}
        raise AssertionError(url)

    import agentd.verify as vmod

    real = vmod.verify_feature_merge

    def patched(**kwargs):
        kwargs = dict(kwargs)
        kwargs["http_get"] = fake_get
        kwargs["token"] = "tok"
        return real(**kwargs)

    vmod.verify_feature_merge = patched  # type: ignore[assignment]
    try:
        loop = SessionLoop(
            store,
            cfg,
            supervisor=None,
            dispatch_turns=False,
            post_comment=lambda **k: posts.append(k.get("body", "")) or 1,
            gateway_token="gw",
            github_token="gw-read",
        )
        _insert(
            store,
            did="d-fail-check",
            event="pull_request_review",
            action="submitted",
            sender="huozheclaude",
            issue=60,
            payload={
                "action": "submitted",
                "review": {"id": 1, "state": "APPROVED", "commit_id": head},
                "pull_request": {
                    "number": 60,
                    "head": {"sha": head, "ref": feat_ref},
                },
                "repository": {"full_name": "huozhe/code-workflow"},
                "sender": {"login": "huozheclaude"},
            },
        )
        loop.process_deferred_batch()
    finally:
        vmod.verify_feature_merge = real  # type: ignore[assignment]

    sess = store.get_session(sk)
    assert sess["state"] == "PAUSED_HUMAN"
    reason = str(sess.get("paused_reason") or "")
    assert "permanent" in reason
    assert posts and "@huozhe" in posts[0]
    store.close()


def test_unknown_mergeable_leaves_deferred(tmp_path: Path) -> None:
    """PR #54 B1: unknown mergeable_state must not pause/page the owner."""
    import agentd.session_loop as dl

    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path, required_checks=[])
    sk = _seed(store, state="CODE_REVIEW")
    feat_ref = _feature_ref(100)
    head = "abc123"
    posts: list[str] = []
    dl._merge_auth_attempts.clear()

    def fake_get(url: str, *, token: str):
        if url.endswith("/reviews"):
            return [
                {
                    "user": {"login": "huozheclaude"},
                    "state": "APPROVED",
                    "commit_id": head,
                }
            ]
        if "/pulls/60" in url:
            return {
                "number": 60,
                "head": {"sha": head, "ref": feat_ref},
                "mergeable_state": "unknown",
            }
        raise AssertionError(url)

    import agentd.verify as vmod

    real = vmod.verify_feature_merge

    def patched(**kwargs):
        kwargs = dict(kwargs)
        kwargs["http_get"] = fake_get
        kwargs["token"] = "tok"
        return real(**kwargs)

    vmod.verify_feature_merge = patched  # type: ignore[assignment]
    try:
        loop = SessionLoop(
            store,
            cfg,
            supervisor=None,
            dispatch_turns=False,
            post_comment=lambda **k: posts.append(k.get("body", "")) or 1,
            gateway_token="gw",
            github_token="gw-read",
        )
        _insert(
            store,
            did="d-unknown",
            event="pull_request_review",
            action="submitted",
            sender="huozheclaude",
            issue=60,
            payload={
                "action": "submitted",
                "review": {"id": 1, "state": "APPROVED", "commit_id": head},
                "pull_request": {
                    "number": 60,
                    "head": {"sha": head, "ref": feat_ref},
                },
                "repository": {"full_name": "huozhe/code-workflow"},
                "sender": {"login": "huozheclaude"},
            },
        )
        loop.process_deferred_batch()
    finally:
        vmod.verify_feature_merge = real  # type: ignore[assignment]

    sess = store.get_session(sk)
    # Still CODE_REVIEW — no escalate
    assert sess["state"] == "CODE_REVIEW"
    assert not sess.get("paused_reason")
    assert posts == []
    # Delivery left deferred for retry
    assert store.count_by_status().get("deferred", 0) == 1
    assert store.count_by_status().get("done", 0) == 0
    store.close()


def test_transient_exhausted_retries_escalate(tmp_path: Path) -> None:
    import agentd.session_loop as dl

    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path, required_checks=[])
    sk = _seed(store, state="CODE_REVIEW")
    feat_ref = _feature_ref(100)
    head = "abc123"
    posts: list[str] = []
    dl._merge_auth_attempts.clear()

    def fake_get(url: str, *, token: str):
        if url.endswith("/reviews"):
            return [
                {
                    "user": {"login": "huozheclaude"},
                    "state": "APPROVED",
                    "commit_id": head,
                }
            ]
        if "/pulls/60" in url:
            return {
                "number": 60,
                "head": {"sha": head, "ref": feat_ref},
                "mergeable_state": "unknown",
            }
        raise AssertionError(url)

    import agentd.verify as vmod

    real = vmod.verify_feature_merge

    def patched(**kwargs):
        kwargs = dict(kwargs)
        kwargs["http_get"] = fake_get
        kwargs["token"] = "tok"
        return real(**kwargs)

    vmod.verify_feature_merge = patched  # type: ignore[assignment]
    try:
        loop = SessionLoop(
            store,
            cfg,
            supervisor=None,
            dispatch_turns=False,
            post_comment=lambda **k: posts.append(k.get("body", "")) or 1,
            gateway_token="gw",
            github_token="gw-read",
        )
        _insert(
            store,
            did="d-exhaust",
            event="pull_request_review",
            action="submitted",
            sender="huozheclaude",
            issue=60,
            payload={
                "action": "submitted",
                "review": {"id": 1, "state": "APPROVED", "commit_id": head},
                "pull_request": {
                    "number": 60,
                    "head": {"sha": head, "ref": feat_ref},
                },
                "repository": {"full_name": "huozhe/code-workflow"},
                "sender": {"login": "huozheclaude"},
            },
        )
        for _ in range(dl._MERGE_AUTH_MAX_ATTEMPTS):
            loop.process_deferred_batch()
    finally:
        vmod.verify_feature_merge = real  # type: ignore[assignment]

    sess = store.get_session(sk)
    assert sess["state"] == "PAUSED_HUMAN"
    reason = str(sess.get("paused_reason") or "")
    assert "was transient" in reason or "attempts" in reason
    assert posts and "@huozhe" in posts[0]
    store.close()


def test_merge_auth_uses_gateway_token(
    tmp_path: Path, allows_github_post: list
) -> None:
    """PR #54 NB: privileged verify reads use gateway credential, not agent PAT."""
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path, required_checks=[])
    _seed(store, state="CODE_REVIEW")
    feat_ref = _feature_ref(100)
    head = "abc123"
    seen_token: list[str | None] = []

    def fake_get(url: str, *, token: str):
        seen_token.append(token)
        if url.endswith("/reviews"):
            return [
                {
                    "user": {"login": "huozheclaude"},
                    "state": "APPROVED",
                    "commit_id": head,
                }
            ]
        if "/pulls/60" in url:
            return {
                "number": 60,
                "head": {"sha": head, "ref": feat_ref},
                "mergeable_state": "clean",
            }
        raise AssertionError(url)

    import agentd.verify as vmod

    real = vmod.verify_feature_merge

    def patched(**kwargs):
        seen_token.append(kwargs.get("token"))
        kwargs = dict(kwargs)
        kwargs["http_get"] = fake_get
        return real(**kwargs)

    vmod.verify_feature_merge = patched  # type: ignore[assignment]
    bodies: list[dict] = []
    patches: list[dict] = []

    def fake_body(**kwargs):
        bodies.append(kwargs)
        return "Please merge.\n\nCloses #100\n"

    def fake_patch(**kwargs):
        patches.append(kwargs)

    try:
        loop = SessionLoop(
            store,
            cfg,
            supervisor=None,
            dispatch_turns=False,
            gateway_token="gw",
            github_token="gateway-read-token",
            get_issue_body_fn=fake_body,
            patch_issue_body_fn=fake_patch,
        )
        _insert(
            store,
            did="d-tok",
            event="pull_request_review",
            action="submitted",
            sender="huozheclaude",
            issue=60,
            payload={
                "action": "submitted",
                "review": {"id": 1, "state": "APPROVED", "commit_id": head},
                "pull_request": {
                    "number": 60,
                    "head": {"sha": head, "ref": feat_ref},
                },
                "repository": {"full_name": "huozhe/code-workflow"},
                "sender": {"login": "huozheclaude"},
            },
        )
        loop.process_deferred_batch()
    finally:
        vmod.verify_feature_merge = real  # type: ignore[assignment]

    assert "gateway-read-token" in seen_token
    assert bodies and bodies[0]["issue_num"] == 60
    assert patches and "Refs #100" in str(patches[0].get("body") or "")
    assert "Closes #100" not in str(patches[0].get("body") or "")
    store.close()


def test_unauthorized_feature_merged_escalates(tmp_path: Path) -> None:
    """feature_merged in CODE_REVIEW (no merge_authorized) → escalate, not stuck silent."""
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path, required_checks=[])
    sk = _seed(store, state="CODE_REVIEW", pr=60)
    feat_ref = _feature_ref(100)
    # Ledger row as supervisor would register
    store.register_artifact(
        session_key=sk,
        role="developer",
        kind="branch",
        ref=feat_ref,
    )
    posts: list[str] = []
    loop = SessionLoop(
        store,
        cfg,
        supervisor=None,
        dispatch_turns=False,
        post_comment=lambda **k: posts.append(k.get("body", "")) or 1,
        gateway_token="gw",
    )
    _insert(
        store,
        did="d-unauth-merge",
        event="pull_request",
        action="closed",
        sender="huozhegrok",
        issue=60,
        payload={
            "action": "closed",
            "pull_request": {
                "number": 60,
                "merged": True,
                "head": {"sha": "x", "ref": feat_ref},
            },
            "repository": {"full_name": "huozhe/code-workflow"},
            "sender": {"login": "huozhegrok"},
        },
    )
    loop.process_deferred_batch()
    sess = store.get_session(sk)
    assert sess["state"] == "PAUSED_HUMAN"
    reason = str(sess.get("paused_reason") or "")
    assert "unauthorized" in reason.lower()
    assert "MERGING" in reason
    # Branch artifact marked removed even on unauthorized path
    open_branches = [
        a
        for a in store.list_artifacts(sk, open_only=True)
        if a["kind"] == "branch"
    ]
    assert open_branches == []
    assert posts and "@huozhe" in posts[0]
    store.close()


def test_authorized_feature_merged_marks_branch_and_awaits(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path, required_checks=[])
    sk = _seed(store, state="MERGING", pr=60)
    feat_ref = _feature_ref(100)
    store.register_artifact(
        session_key=sk,
        role="developer",
        kind="branch",
        ref=feat_ref,
    )
    loop = SessionLoop(store, cfg, supervisor=None, dispatch_turns=False)
    _insert(
        store,
        did="d-auth-merge",
        event="pull_request",
        action="closed",
        sender="huozhegrok",
        issue=60,
        payload={
            "action": "closed",
            "pull_request": {
                "number": 60,
                "merged": True,
                "head": {"sha": "x", "ref": feat_ref},
            },
            "repository": {"full_name": "huozhe/code-workflow"},
            "sender": {"login": "huozhegrok"},
        },
    )
    loop.process_deferred_batch()
    sess = store.get_session(sk)
    assert sess["state"] == "AWAITING_VERIFICATION"
    open_branches = [
        a
        for a in store.list_artifacts(sk, open_only=True)
        if a["kind"] == "branch"
    ]
    assert open_branches == []
    all_b = [
        a
        for a in store.list_artifacts(sk, open_only=False)
        if a["kind"] == "branch"
    ]
    assert all_b and all_b[0]["removed_at"] is not None
    store.close()


def test_pick_merge_authorized_is_developer() -> None:
    """§8.4: Developer merges — opposite of Design PR merge actor."""
    loop = SessionLoop.__new__(SessionLoop)
    role, login = SessionLoop._pick_recipient(
        loop, "merge_authorized", "CODE_REVIEW", "huozheclaude", "huozhegrok", "huozheclaude"
    )
    assert role == "developer"
    assert login == "huozhegrok"


def test_blocked_unresolved_threads_escalate_on_first_observation(
    tmp_path: Path,
) -> None:
    """ADR-26 (1): retry counter never leaves zero — no wait for max attempts."""
    import agentd.session_loop as dl
    from agentd.github_fetch import PrReviewThreadSnapshot

    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path, required_checks=[])
    sk = _seed(store, state="CODE_REVIEW")
    feat_ref = _feature_ref(100)
    head = "abc123"
    posts: list[str] = []
    dl._merge_auth_attempts.clear()

    def fake_get(url: str, *, token: str):
        if url.endswith("/reviews"):
            return [
                {
                    "user": {"login": "huozheclaude"},
                    "state": "APPROVED",
                    "commit_id": head,
                }
            ]
        if "/pulls/60" in url:
            return {
                "number": 60,
                "head": {"sha": head, "ref": feat_ref},
                "mergeable_state": "blocked",
            }
        raise AssertionError(url)

    snap = PrReviewThreadSnapshot(
        open_thread_ids=["t1", "t2"],
        unresolved_count=2,
        all_thread_ids=["t1", "t2"],
    )

    import agentd.verify as vmod

    real = vmod.verify_feature_merge

    def patched(**kwargs):
        kwargs = dict(kwargs)
        kwargs["http_get"] = fake_get
        kwargs["token"] = "tok"
        kwargs["fetch_threads"] = lambda **_k: snap
        return real(**kwargs)

    vmod.verify_feature_merge = patched  # type: ignore[assignment]
    try:
        loop = SessionLoop(
            store,
            cfg,
            supervisor=None,
            dispatch_turns=False,
            post_comment=lambda **k: posts.append(k.get("body", "")) or 1,
            gateway_token="gw",
            github_token="gw-read",
        )
        _insert(
            store,
            did="d-threads",
            event="pull_request_review",
            action="submitted",
            sender="huozheclaude",
            issue=60,
            payload={
                "action": "submitted",
                "review": {"id": 1, "state": "APPROVED", "commit_id": head},
                "pull_request": {
                    "number": 60,
                    "head": {"sha": head, "ref": feat_ref},
                },
                "repository": {"full_name": "huozhe/code-workflow"},
                "sender": {"login": "huozheclaude"},
            },
        )
        loop.process_deferred_batch()
    finally:
        vmod.verify_feature_merge = real  # type: ignore[assignment]

    sess = store.get_session(sk)
    assert sess["state"] == "PAUSED_HUMAN"
    reason = str(sess.get("paused_reason") or "")
    assert "2 unresolved review thread" in reason
    assert "was transient" not in reason
    assert "d-threads" not in dl._merge_auth_attempts
    assert store.count_by_status().get("done", 0) == 1
    assert store.count_by_status().get("deferred", 0) == 0
    assert posts and "@huozhe" in posts[0]
    store.close()
