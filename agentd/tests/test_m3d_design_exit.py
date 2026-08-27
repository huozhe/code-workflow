"""M3-D: design-half exit path — issue → Design PR → approve → merge (§8 / #13).

No Docker: FSM + routing + §8.4 verify + delivery status with injected webhooks.
dispatch_turns=False — proves gateway state machine without agent CLI.
"""

from __future__ import annotations

import json
from pathlib import Path

from agentd.config import Config
from agentd.db import Store
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


def _insert(
    store: Store,
    *,
    did: str,
    event: str,
    action: str | None,
    payload: dict,
    sender: str,
    issue: int = 100,
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


def _design_head_ref(issue: int = 100) -> str:
    from agentd.gitops import role_branch_name

    return role_branch_name("huozhe/code-workflow", issue, "architect")


def test_design_half_happy_path_to_implementing(tmp_path: Path) -> None:
    """Exit condition skeleton: PLANNING → DESIGN_REVIEW → APPROVED → IMPLEMENTING."""
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    sk = "huozhe/code-workflow#100"
    now = 1_700_000_000
    store.upsert_session(
        session_key=sk,
        project_key="huozhe/code-workflow",
        repo="huozhe/code-workflow",
        issue_num=100,
        state="PLANNING",
        architect="huozheclaude",
        developer="huozhegrok",
        created_at=now,
        updated_at=now,
    )
    design_ref = _design_head_ref(100)
    # Fake runner endpoint so get_session join is happy; no turns dispatched.
    store.upsert_runner(
        "huozhe/code-workflow",
        container_id="c-fake",
        endpoint="127.0.0.1:9",
        token="tok",
        tier="hot",
    )

    # §8.4 verify inject — Developer approved on head
    head = "abc123def456"

    def fake_get(url: str, *, token: str):
        if url.endswith("/reviews"):
            return [
                {
                    "user": {"login": "huozhegrok"},
                    "state": "APPROVED",
                    "commit_id": head,
                }
            ]
        return {"head": {"sha": head}, "number": 50}

    posts: list[str] = []
    loop = SessionLoop(
        store,
        cfg,
        supervisor=None,
        dispatch_turns=False,
        post_comment=lambda **k: posts.append(k.get("body", "")) or 1,
        gateway_token="gw",
    )

    # Patch verify used inside session_loop (imported lazily in method)
    import agentd.verify as vmod

    real = vmod.verify_design_approval

    def patched(**kwargs):
        kwargs = dict(kwargs)
        kwargs["http_get"] = fake_get
        kwargs["token"] = kwargs.get("token") or "tok"
        return real(**kwargs)

    vmod.verify_design_approval = patched  # type: ignore[assignment]
    try:
        # 1) Architect opened Design PR — recognised by head.ref (not title).
        # delivery issue_num is the *PR* number (GitHub webhook shape); branch
        # embeds the real issue (100).
        _insert(
            store,
            did="d-pr-open",
            event="pull_request",
            action="opened",
            sender="huozheclaude",
            issue=50,  # PR number as webhook would store
            payload={
                "action": "opened",
                "pull_request": {
                    "number": 50,
                    "title": "Add caching layer",  # no "design"/"rfc" in title
                    "html_url": "https://github.com/huozhe/code-workflow/pull/50",
                    "head": {"sha": head, "ref": design_ref},
                    "user": {"login": "huozheclaude"},
                },
                "repository": {"full_name": "huozhe/code-workflow"},
                "sender": {"login": "huozheclaude"},
            },
        )
        loop.process_deferred_batch()
        sess = store.get_session(sk)
        assert sess is not None
        assert sess["state"] == "DESIGN_REVIEW"
        assert int(sess.get("roles_locked") or 0) == 1
        assert int(sess.get("design_pr") or 0) == 50

        # 2) Developer approved on current head
        _insert(
            store,
            did="d-approve",
            event="pull_request_review",
            action="submitted",
            sender="huozhegrok",
            payload={
                "action": "submitted",
                "review": {
                    "state": "APPROVED",
                    "id": 1,
                    "commit_id": head,
                    "user": {"login": "huozhegrok"},
                },
                "pull_request": {
                    "number": 50,
                    "title": "Add caching layer",
                    "head": {"sha": head, "ref": design_ref},
                },
                "repository": {"full_name": "huozhe/code-workflow"},
                "sender": {"login": "huozhegrok"},
            },
            issue=50,
        )
        loop.process_deferred_batch()
        sess = store.get_session(sk)
        assert sess["state"] == "DESIGN_APPROVED", sess.get("paused_reason")

        # 3) Architect merges Design PR
        _insert(
            store,
            did="d-merge",
            event="pull_request",
            action="closed",
            sender="huozheclaude",
            issue=50,
            payload={
                "action": "closed",
                "pull_request": {
                    "number": 50,
                    "title": "Add caching layer",
                    "merged": True,
                    "head": {"sha": head, "ref": design_ref},
                    "user": {"login": "huozheclaude"},
                },
                "repository": {"full_name": "huozhe/code-workflow"},
                "sender": {"login": "huozheclaude"},
            },
        )
        loop.process_deferred_batch()
        sess = store.get_session(sk)
        assert sess["state"] == "IMPLEMENTING"

        # Deliveries became terminal work (routed/done), not stuck deferred
        counts = store.count_by_status()
        assert counts.get("deferred", 0) == 0
        assert (counts.get("routed") or 0) + (counts.get("done") or 0) >= 3
    finally:
        vmod.verify_design_approval = real  # type: ignore[assignment]
    store.close()


def test_non_design_merge_does_not_advance(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    sk = "huozhe/code-workflow#101"
    store.upsert_session(
        session_key=sk,
        repo="huozhe/code-workflow",
        issue_num=101,
        state="DESIGN_APPROVED",
        architect="huozheclaude",
        developer="huozhegrok",
        created_at=1,
        updated_at=1,
        design_pr=50,
    )
    loop = SessionLoop(store, cfg, supervisor=None, dispatch_turns=False)
    # Merge of a different PR (feature) must not move DESIGN_APPROVED
    _insert(
        store,
        did="d-feat-merge",
        event="pull_request",
        action="closed",
        sender="huozhegrok",
        issue=101,
        payload={
            "action": "closed",
            "pull_request": {
                "number": 99,
                "title": "feat: implement caching",
                "merged": True,
                "head": {"sha": "fff"},
            },
            "repository": {"full_name": "huozhe/code-workflow"},
            "sender": {"login": "huozhegrok"},
        },
    )
    loop.process_deferred_batch()
    assert store.get_session(sk)["state"] == "DESIGN_APPROVED"
    store.close()


def test_issue_comment_on_design_pr_resolves_session(tmp_path: Path) -> None:
    """PR-keyed issue_comment maps to session via design_pr (M3-D approval NB)."""
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    sk = "huozhe/code-workflow#100"
    store.upsert_session(
        session_key=sk,
        repo="huozhe/code-workflow",
        issue_num=100,
        state="DESIGN_REVIEW",
        architect="huozheclaude",
        developer="huozhegrok",
        created_at=1,
        updated_at=1,
        design_pr=50,
    )
    loop = SessionLoop(
        store,
        cfg,
        supervisor=None,
        dispatch_turns=False,
        post_comment=lambda **k: 1,
        gateway_token="gw",
    )
    # Owner comments *on the Design PR* (webhook issue_num = 50 = PR number)
    store.insert_delivery(
        delivery_id="d-pr-comment",
        event="issue_comment",
        action="created",
        repo="huozhe/code-workflow",
        issue_num=50,
        sender="huozhe",
        payload=json.dumps(
            {
                "action": "created",
                "issue": {
                    "number": 50,
                    "pull_request": {
                        "url": "https://api.github.com/repos/huozhe/code-workflow/pulls/50"
                    },
                },
                "comment": {"id": 1, "body": "lgtm path"},
                "repository": {"full_name": "huozhe/code-workflow"},
                "sender": {"login": "huozhe"},
            }
        ).encode(),
        status="deferred",
    )
    loop.process_deferred_batch()
    # Must not drop as "no session for …#50"
    assert store.count_by_status().get("dropped", 0) == 0
    assert store.get_session(sk) is not None
    # Owner comment while not paused → routed or done
    counts = store.count_by_status()
    assert (counts.get("routed") or 0) + (counts.get("done") or 0) >= 1
    store.close()


def test_review_comment_remaps_via_design_pr(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    sk = "huozhe/code-workflow#7"
    design_ref = _design_head_ref(7)
    store.upsert_session(
        session_key=sk,
        repo="huozhe/code-workflow",
        issue_num=7,
        state="DESIGN_REVIEW",
        architect="huozheclaude",
        developer="huozhegrok",
        created_at=1,
        updated_at=1,
        design_pr=88,
    )
    loop = SessionLoop(store, cfg, supervisor=None, dispatch_turns=False)
    store.insert_delivery(
        delivery_id="d-inline",
        event="pull_request_review_comment",
        action="created",
        repo="huozhe/code-workflow",
        issue_num=88,
        sender="huozhegrok",
        payload=json.dumps(
            {
                "action": "created",
                "pull_request": {
                    "number": 88,
                    "title": "Add caching",
                    "head": {"sha": "x", "ref": design_ref},
                },
                "comment": {"id": 9, "body": "nit", "path": "a.py"},
                "repository": {"full_name": "huozhe/code-workflow"},
                "sender": {"login": "huozhegrok"},
            }
        ).encode(),
        status="deferred",
    )
    loop.process_deferred_batch()
    assert store.count_by_status().get("dropped", 0) == 0
    store.close()


def test_design_pr_by_branch_not_title() -> None:
    from agentd.gitops import role_branch_name
    from agentd.session_loop import SessionLoop

    loop = SessionLoop.__new__(SessionLoop)
    repo = "huozhe/code-workflow"
    ref = role_branch_name(repo, 7, "architect")
    assert loop._is_design_pr(
        repo=repo,
        pr={"title": "Add caching", "head": {"ref": ref, "sha": "x"}},
    )
    # Developer branch is never Design even with "design" in title
    dev = role_branch_name(repo, 7, "developer")
    assert not loop._is_design_pr(
        repo=repo,
        pr={"title": "Redesign the cache", "head": {"ref": dev, "sha": "x"}},
    )
    # Title fallback when head is not an agentd branch
    assert loop._is_design_pr(
        repo=repo,
        pr={"title": "Design: RFC", "head": {"ref": "feat/other", "sha": "x"}},
    )


def test_design_approved_routes_to_architect_merge_actor() -> None:
    """§8.3: after Developer approval, next turn is Architect (merge)."""
    from agentd.session_loop import SessionLoop

    loop = SessionLoop.__new__(SessionLoop)
    role, login = SessionLoop._pick_recipient(
        loop,
        "design_approved",
        "DESIGN_REVIEW",
        "huozheclaude",
        "huozhegrok",
        "huozhegrok",
    )
    assert role == "architect"
    assert login == "huozheclaude"
    role2, _ = SessionLoop._pick_recipient(
        loop,
        "design_pr_opened",
        "PLANNING",
        "huozheclaude",
        "huozhegrok",
        "huozheclaude",
    )
    assert role2 == "developer"
