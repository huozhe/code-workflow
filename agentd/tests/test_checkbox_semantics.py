"""M5-1 / §10.2: issues.edited checkbox record + agent-edit restore."""

from __future__ import annotations

import json
import time
from pathlib import Path

from agentd.config import Config
from agentd.db import Store
from agentd.design_loop import DesignLoop
from agentd.verification import (
    CHECKBOX_CHECKED,
    CHECKBOX_UNCHECKED,
    checkbox_is_checked,
    render_verification_block,
    set_checkbox_in_body,
)


def _cfg(tmp: Path, *, owner: str = "huozhe") -> Config:
    return Config(
        raw={
            "host": {"owner": owner},
            "gateway": {"login": "huozhegateway"},
            "agents": {
                "claude": {"login": "huozheclaude"},
                "grok": {"login": "huozhegrok"},
            },
            "intake": {"mode": "all", "actors": "collaborators"},
        },
        root=tmp,
    )


def _seed(
    store: Store,
    *,
    state: str = "AWAITING_VERIFICATION",
    issue: int = 58,
    verified_at: int | None = None,
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
    if verified_at is not None:
        store.update_session_fields(sk, verified_at=verified_at)
    store.upsert_runner(
        "huozhe/code-workflow",
        container_id="c1",
        endpoint="127.0.0.1:9",
        token="tok",
        tier="hot",
    )
    return sk


def _body(*, checked: bool = False, steps: list[str] | None = None) -> str:
    return (
        "## Goal\n\nSession work.\n\n"
        + render_verification_block(
            steps=steps or ["pull", "test"],
            merged_prs=[59, 60],
            checked=checked,
        )
        + "\n"
    )


def _edited_payload(
    *,
    issue: int,
    sender: str,
    body: str,
    body_changed: bool = True,
) -> dict:
    changes: dict = {}
    if body_changed:
        changes["body"] = {"from": "old"}
    return {
        "action": "edited",
        "issue": {
            "number": issue,
            "state": "open",
            "body": body,
            "title": "session",
        },
        "changes": changes,
        "repository": {"full_name": "huozhe/code-workflow"},
        "sender": {"login": sender},
    }


def _insert(store: Store, did: str, payload: dict, *, issue: int, sender: str) -> None:
    store.insert_delivery(
        delivery_id=did,
        event="issues",
        action="edited",
        repo="huozhe/code-workflow",
        issue_num=issue,
        sender=sender,
        payload=json.dumps(payload).encode(),
        status="deferred",
    )


def test_set_checkbox_preserves_steps() -> None:
    body = _body(checked=False, steps=["alpha", "beta"])
    out = set_checkbox_in_body(body, checked=True)
    assert "1. alpha" in out
    assert "2. beta" in out
    assert CHECKBOX_CHECKED in out
    assert checkbox_is_checked(out) is True
    back = set_checkbox_in_body(out, checked=False)
    assert CHECKBOX_UNCHECKED in back
    assert "1. alpha" in back


def test_owner_tick_records_verified_at(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    sk = _seed(store)
    body = _body(checked=True)
    _insert(
        store,
        "d-owner-tick",
        _edited_payload(issue=58, sender="huozhe", body=body),
        issue=58,
        sender="huozhe",
    )
    DesignLoop(store, cfg, supervisor=None, dispatch_turns=False).process_deferred_batch()
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["verified_at"] is not None
    assert int(sess["verified_at"]) > 0
    store.close()


def test_owner_untick_clears_verified_at(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    sk = _seed(store, verified_at=int(time.time()) - 10)
    body = _body(checked=False)
    _insert(
        store,
        "d-owner-untick",
        _edited_payload(issue=58, sender="huozhe", body=body),
        issue=58,
        sender="huozhe",
    )
    DesignLoop(store, cfg, supervisor=None, dispatch_turns=False).process_deferred_batch()
    assert store.get_session(sk)["verified_at"] is None
    store.close()


def test_agent_tick_restores_and_warns(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    sk = _seed(store, verified_at=None)
    body = _body(checked=True)
    patches: list[dict] = []
    comments: list[dict] = []

    def fake_patch(*, repo, issue_num, body, token):  # noqa: ANN001
        patches.append({"body": body, "token": token})

    def fake_comment(*, repo, issue_num, body, token):  # noqa: ANN001
        comments.append({"body": body})
        return 1

    loop = DesignLoop(
        store,
        cfg,
        supervisor=None,
        dispatch_turns=False,
        gateway_token="gw",
        patch_issue_body_fn=fake_patch,
        post_comment=fake_comment,
    )
    _insert(
        store,
        "d-agent-tick",
        _edited_payload(issue=58, sender="huozheclaude", body=body),
        issue=58,
        sender="huozheclaude",
    )
    loop.process_deferred_batch()
    assert store.get_session(sk)["verified_at"] is None
    assert len(patches) == 1
    assert checkbox_is_checked(patches[0]["body"]) is False
    assert "1. pull" in patches[0]["body"]  # steps preserved
    assert len(comments) == 1
    assert "restored" in comments[0]["body"].lower()
    assert "§10.2" in comments[0]["body"] or "10.2" in comments[0]["body"]
    store.close()


def test_agent_untick_after_owner_restores_checked(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    sk = _seed(store, verified_at=int(time.time()))
    body = _body(checked=False)
    patches: list[str] = []

    def fake_patch(*, repo, issue_num, body, token):  # noqa: ANN001
        patches.append(body)

    def fake_comment(*, repo, issue_num, body, token):  # noqa: ANN001
        return 1

    loop = DesignLoop(
        store,
        cfg,
        supervisor=None,
        dispatch_turns=False,
        gateway_token="gw",
        patch_issue_body_fn=fake_patch,
        post_comment=fake_comment,
    )
    _insert(
        store,
        "d-agent-untick",
        _edited_payload(issue=58, sender="huozhegrok", body=body),
        issue=58,
        sender="huozhegrok",
    )
    loop.process_deferred_batch()
    assert store.get_session(sk)["verified_at"] is not None
    assert len(patches) == 1
    assert checkbox_is_checked(patches[0]) is True
    store.close()


def test_agent_refines_steps_no_patch_when_checkbox_ok(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    _seed(store, verified_at=None)
    body = _body(checked=False, steps=["human-runnable step A"])
    patches: list = []
    comments: list = []

    loop = DesignLoop(
        store,
        cfg,
        supervisor=None,
        dispatch_turns=False,
        gateway_token="gw",
        patch_issue_body_fn=lambda **kw: patches.append(kw),  # noqa: ARG005
        post_comment=lambda **kw: comments.append(kw) or 1,  # noqa: ARG005
    )
    _insert(
        store,
        "d-agent-steps",
        _edited_payload(issue=58, sender="huozheclaude", body=body),
        issue=58,
        sender="huozheclaude",
    )
    loop.process_deferred_batch()
    assert patches == []
    assert comments == []
    store.close()


def test_gateway_edit_ignored(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    sk = _seed(store)
    body = _body(checked=True)  # would look like a tick if misclassified
    patches: list = []
    loop = DesignLoop(
        store,
        cfg,
        supervisor=None,
        dispatch_turns=False,
        gateway_token="gw",
        patch_issue_body_fn=lambda **kw: patches.append(kw),  # noqa: ARG005
    )
    _insert(
        store,
        "d-gw",
        _edited_payload(issue=58, sender="huozhegateway", body=body),
        issue=58,
        sender="huozhegateway",
    )
    loop.process_deferred_batch()
    assert store.get_session(sk)["verified_at"] is None
    assert patches == []
    store.close()


def test_owner_who_is_agent_not_recorded(tmp_path: Path) -> None:
    """§10.2 second test: agent listed as owner must not stamp verified_at."""
    store = Store(tmp_path / "state.db")
    # Misconfig: owner == grok agent login
    cfg = _cfg(tmp_path, owner="huozhegrok")
    sk = _seed(store)
    # Session developer is also huozhegrok → is_agent
    body = _body(checked=True)
    patches: list = []
    comments: list = []
    loop = DesignLoop(
        store,
        cfg,
        supervisor=None,
        dispatch_turns=False,
        gateway_token="gw",
        patch_issue_body_fn=lambda **kw: patches.append(kw.get("body")),  # noqa: ARG005
        post_comment=lambda **kw: comments.append(kw) or 1,  # noqa: ARG005
    )
    _insert(
        store,
        "d-owner-agent",
        _edited_payload(issue=58, sender="huozhegrok", body=body),
        issue=58,
        sender="huozhegrok",
    )
    loop.process_deferred_batch()
    assert store.get_session(sk)["verified_at"] is None
    # Treated as agent tamper while AWAITING → restore unchecked + warn
    assert len(patches) == 1
    assert checkbox_is_checked(patches[0]) is False
    assert len(comments) == 1
    store.close()


def test_title_only_edit_noop(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    sk = _seed(store)
    _insert(
        store,
        "d-title",
        _edited_payload(
            issue=58, sender="huozhe", body=_body(checked=True), body_changed=False
        ),
        issue=58,
        sender="huozhe",
    )
    DesignLoop(store, cfg, supervisor=None, dispatch_turns=False).process_deferred_batch()
    assert store.get_session(sk)["verified_at"] is None
    store.close()


def test_agent_edit_outside_awaiting_no_restore(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    sk = _seed(store, state="IMPLEMENTING", verified_at=None)
    body = _body(checked=True)
    patches: list = []
    loop = DesignLoop(
        store,
        cfg,
        supervisor=None,
        dispatch_turns=False,
        gateway_token="gw",
        patch_issue_body_fn=lambda **kw: patches.append(kw),  # noqa: ARG005
    )
    _insert(
        store,
        "d-impl",
        _edited_payload(issue=58, sender="huozheclaude", body=body),
        issue=58,
        sender="huozheclaude",
    )
    loop.process_deferred_batch()
    assert store.get_session(sk)["verified_at"] is None
    assert patches == []
    store.close()


def test_config_agent_logins_excludes_gateway(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    assert "huozheclaude" in cfg.agent_logins()
    assert "huozhegrok" in cfg.agent_logins()
    assert "huozhegateway" not in cfg.agent_logins()
    assert "huozhegateway" in cfg.all_bot_logins()
