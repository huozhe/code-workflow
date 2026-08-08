"""M3-D: design-half exit path — issue → Design PR → approve → merge (§8 / #13).

No Docker: FSM + routing + §8.4 verify + delivery status with injected webhooks.
dispatch_turns=False — proves gateway state machine without agent CLI.
"""

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

    import agentd.design_loop as dl

    original_verify = None
    from agentd import verify as verify_mod

    posts: list[str] = []
    loop = DesignLoop(
        store,
        cfg,
        supervisor=None,
        dispatch_turns=False,
        post_comment=lambda **k: posts.append(k.get("body", "")) or 1,
        gateway_token="gw",
    )

    # Patch verify used inside design_loop (imported lazily in method)
    import agentd.verify as vmod

    real = vmod.verify_design_approval

    def patched(**kwargs):
        kwargs = dict(kwargs)
        kwargs["http_get"] = fake_get
        kwargs["token"] = kwargs.get("token") or "tok"
        return real(**kwargs)

    vmod.verify_design_approval = patched  # type: ignore[assignment]
    try:
        # 1) Architect opened Design PR
        _insert(
            store,
            did="d-pr-open",
            event="pull_request",
            action="opened",
            sender="huozheclaude",
            payload={
                "action": "opened",
                "pull_request": {
                    "number": 50,
                    "title": "Design: caching layer RFC",
                    "html_url": "https://github.com/huozhe/code-workflow/pull/50",
                    "head": {"sha": head},
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
                    "title": "Design: caching layer RFC",
                    "head": {"sha": head},
                },
                "repository": {"full_name": "huozhe/code-workflow"},
                "sender": {"login": "huozhegrok"},
            },
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
            payload={
                "action": "closed",
                "pull_request": {
                    "number": 50,
                    "title": "Design: caching layer RFC",
                    "merged": True,
                    "head": {"sha": head},
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
    loop = DesignLoop(store, cfg, supervisor=None, dispatch_turns=False)
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


def test_design_approved_routes_to_architect_merge_actor() -> None:
    """§8.3: after Developer approval, next turn is Architect (merge)."""
    from agentd.design_loop import DesignLoop

    loop = DesignLoop.__new__(DesignLoop)
    role, login = DesignLoop._pick_recipient(
        loop,
        "design_approved",
        "DESIGN_REVIEW",
        "huozheclaude",
        "huozhegrok",
        "huozhegrok",
    )
    assert role == "architect"
    assert login == "huozheclaude"
    role2, _ = DesignLoop._pick_recipient(
        loop,
        "design_pr_opened",
        "PLANNING",
        "huozheclaude",
        "huozhegrok",
        "huozheclaude",
    )
    assert role2 == "developer"
