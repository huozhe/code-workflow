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
    extract_verification_block,
    neutralize_bare_verification_ticks,
    reinsert_verification_block,
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
    body_from: str | None = "old",
    body_changed: bool = True,
) -> dict:
    changes: dict = {}
    if body_changed:
        if body_from is None:
            # Explicit missing from (malformed / truncated delivery).
            changes["body"] = {}
        else:
            changes["body"] = {"from": body_from}
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


def test_set_checkbox_survives_stray_close_sentinel() -> None:
    """replace(block) must not mangle when prose quotes a close sentinel."""
    prose = "## Goal\n\nSee `<!-- /agentd:verification -->` in the spec.\n\n"
    block = render_verification_block(steps=["a"], merged_prs=[1], checked=False)
    body = prose + block + "\n"
    out = set_checkbox_in_body(body, checked=True)
    assert out.startswith("## Goal")
    assert "See `<!-- /agentd:verification -->`" in out
    assert checkbox_is_checked(out) is True
    assert out.count(CHECKBOX_CHECKED) == 1


def test_reinsert_verification_block() -> None:
    block = render_verification_block(steps=["x"], merged_prs=[1], checked=True)
    body = "## Goal\n\nOnly prose.\n"
    out = reinsert_verification_block(body, block)
    assert extract_verification_block(out) == block
    assert "## Goal" in out
    assert checkbox_is_checked(out) is True


def test_owner_tick_records_verified_at(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    sk = _seed(store)
    prev = _body(checked=False)
    body = _body(checked=True)
    _insert(
        store,
        "d-owner-tick",
        _edited_payload(issue=58, sender="huozhe", body=body, body_from=prev),
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
    prev = _body(checked=True)
    body = _body(checked=False)
    _insert(
        store,
        "d-owner-untick",
        _edited_payload(issue=58, sender="huozhe", body=body, body_from=prev),
        issue=58,
        sender="huozhe",
    )
    DesignLoop(store, cfg, supervisor=None, dispatch_turns=False).process_deferred_batch()
    assert store.get_session(sk)["verified_at"] is None
    store.close()


def test_agent_tick_restores_and_warns(tmp_path: Path) -> None:
    """Agent flips unchecked → checked; restore to pre-edit (was=False)."""
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    sk = _seed(store, verified_at=None)
    prev = _body(checked=False)
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
        get_issue_body_fn=lambda **_: body,
        patch_issue_body_fn=fake_patch,
        post_comment=fake_comment,
    )
    _insert(
        store,
        "d-agent-tick",
        _edited_payload(
            issue=58, sender="huozheclaude", body=body, body_from=prev
        ),
        issue=58,
        sender="huozheclaude",
    )
    loop.process_deferred_batch()
    assert store.get_session(sk)["verified_at"] is None
    assert len(patches) == 1
    assert checkbox_is_checked(patches[0]["body"]) is False
    assert "1. pull" in patches[0]["body"]
    assert len(comments) == 1
    assert "restored" in comments[0]["body"].lower()
    assert "changes.body.from" in comments[0]["body"]
    store.close()


def test_agent_untick_restores_checked_from_prev(tmp_path: Path) -> None:
    """#65 / #89 regression: owner tick recorded; agent untick restores up."""
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    sk = _seed(store, verified_at=int(time.time()))
    prev = _body(checked=True)
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
        get_issue_body_fn=lambda **_: body,
        patch_issue_body_fn=fake_patch,
        post_comment=fake_comment,
    )
    _insert(
        store,
        "d-agent-untick",
        _edited_payload(
            issue=58, sender="huozhegrok", body=body, body_from=prev
        ),
        issue=58,
        sender="huozhegrok",
    )
    loop.process_deferred_batch()
    assert store.get_session(sk)["verified_at"] is not None
    assert len(patches) == 1
    assert checkbox_is_checked(patches[0]) is True
    store.close()


def test_lost_owner_tick_is_not_reverted_by_agent_step_refinement(
    tmp_path: Path,
) -> None:
    """B1: gateway was down for owner tick; agent refines steps — keep tick.

    verified_at is None (missed delivery) but body was already checked before
    and after the agent edit. Restore must not use verified_at as authority.
    """
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    _seed(store, verified_at=None)

    prev_body = _body(checked=True, steps=["pull", "test"])
    new_body = _body(checked=True, steps=["pull", "test", "smoke the CLI"])

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
        "d-lost-tick",
        _edited_payload(
            issue=58,
            sender="huozheclaude",
            body=new_body,
            body_from=prev_body,
        ),
        issue=58,
        sender="huozheclaude",
    )
    loop.process_deferred_batch()
    assert patches == [], "gateway must not revert a tick it never recorded"
    assert comments == []
    store.close()


def test_agent_deletes_block_restored_from_prev(tmp_path: Path) -> None:
    """B2: deleting the whole block is restored from changes.body.from."""
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    _seed(store, verified_at=int(time.time()))
    prev = _body(checked=True, steps=["pull", "test"])
    # Remaining prose must stay ≥ 0.5× prev so ADR-15's sanity floor
    # (damage, not a block-only delete) does not fire. Real issues are
    # mostly goal/checklist; this fixture used to be block-dominated.
    new_body = (
        "## Goal\n\nSession work — block deleted by agent.\n\n"
        + ("Keep the session goal and checklist intact. " * 20)
        + "\n"
    )
    patches: list[str] = []
    comments: list[str] = []

    def fake_patch(*, repo, issue_num, body, token):  # noqa: ANN001
        patches.append(body)

    def fake_comment(*, repo, issue_num, body, token):  # noqa: ANN001
        comments.append(body)
        return 1

    loop = DesignLoop(
        store,
        cfg,
        supervisor=None,
        dispatch_turns=False,
        gateway_token="gw",
        get_issue_body_fn=lambda **_: new_body,
        patch_issue_body_fn=fake_patch,
        post_comment=fake_comment,
    )
    _insert(
        store,
        "d-del-block",
        _edited_payload(
            issue=58, sender="huozheclaude", body=new_body, body_from=prev
        ),
        issue=58,
        sender="huozheclaude",
    )
    loop.process_deferred_batch()
    assert len(patches) == 1
    assert extract_verification_block(patches[0]) is not None
    assert checkbox_is_checked(patches[0]) is True
    assert "Session work — block deleted by agent." in patches[0]
    assert len(comments) == 1
    assert "block" in comments[0].lower()
    store.close()


def test_agent_refines_steps_no_patch_when_checkbox_ok(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    _seed(store, verified_at=None)
    prev = _body(checked=False, steps=["scaffold"])
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
        _edited_payload(
            issue=58, sender="huozheclaude", body=body, body_from=prev
        ),
        issue=58,
        sender="huozheclaude",
    )
    loop.process_deferred_batch()
    assert patches == []
    assert comments == []
    store.close()


def test_agent_adds_preticked_block_where_none_existed(tmp_path: Path) -> None:
    """B3 P3: agent inserts a whole pre-ticked block → force unchecked."""
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    _seed(store, verified_at=None)
    prev = "## Goal\n\nSession work. No block yet.\n"
    body = _body(checked=True, steps=["agent wrote steps"])
    patches: list[str] = []
    comments: list[str] = []

    def fake_patch(*, repo, issue_num, body, token):  # noqa: ANN001
        patches.append(body)

    def fake_comment(*, repo, issue_num, body, token):  # noqa: ANN001
        comments.append(body)
        return 1

    loop = DesignLoop(
        store,
        cfg,
        supervisor=None,
        dispatch_turns=False,
        gateway_token="gw",
        get_issue_body_fn=lambda **_: body,
        patch_issue_body_fn=fake_patch,
        post_comment=fake_comment,
    )
    _insert(
        store,
        "d-p3",
        _edited_payload(
            issue=58, sender="huozheclaude", body=body, body_from=prev
        ),
        issue=58,
        sender="huozheclaude",
    )
    loop.process_deferred_batch()
    assert len(patches) == 1
    assert checkbox_is_checked(patches[0]) is False
    assert "agent wrote steps" in patches[0]
    assert len(comments) == 1
    assert "without prior owner tick" in comments[0]
    store.close()


def test_agent_inserts_tick_into_block_without_line(tmp_path: Path) -> None:
    """B3 P4: block had no checkbox line; agent adds - [x] → force unchecked."""
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    _seed(store, verified_at=None)
    # Prior block without the Human Verification line.
    prev = (
        "## Goal\n\n"
        "<!-- agentd:verification v1 -->\n"
        "## Verification Protocol\n\n"
        "1. pull\n\n"
        "**Merged PRs:** #59\n"
        "<!-- /agentd:verification -->\n"
    )
    body = _body(checked=True, steps=["pull"])
    patches: list[str] = []
    comments: list = []

    loop = DesignLoop(
        store,
        cfg,
        supervisor=None,
        dispatch_turns=False,
        gateway_token="gw",
        get_issue_body_fn=lambda **_: body,
        patch_issue_body_fn=lambda **kw: patches.append(kw["body"]),  # noqa: ARG005
        post_comment=lambda **kw: comments.append(kw) or 1,  # noqa: ARG005
    )
    _insert(
        store,
        "d-p4",
        _edited_payload(
            issue=58, sender="huozheclaude", body=body, body_from=prev
        ),
        issue=58,
        sender="huozheclaude",
    )
    loop.process_deferred_batch()
    assert len(patches) == 1
    assert checkbox_is_checked(patches[0]) is False
    assert len(comments) == 1
    store.close()


def test_agent_removes_unticked_line_restored(tmp_path: Path) -> None:
    """B4: agent deletes the unticked checkbox line — restore [ ]."""
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    _seed(store, verified_at=None)
    prev = _body(checked=False, steps=["pull", "test"])
    # Block still present but no Human Verification line.
    body = (
        "## Goal\n\nSession work.\n\n"
        "<!-- agentd:verification v1 -->\n"
        "## Verification Protocol\n\n"
        "1. pull\n"
        "2. test\n"
        "3. smoke the CLI\n\n"
        "**Merged PRs:** #59, #60\n"
        "**Not covered:** _(none)_\n\n"
        "*Tick this box before closing…*\n"
        "<!-- /agentd:verification -->\n"
    )
    patches: list[str] = []
    comments: list[str] = []

    loop = DesignLoop(
        store,
        cfg,
        supervisor=None,
        dispatch_turns=False,
        gateway_token="gw",
        get_issue_body_fn=lambda **_: body,
        patch_issue_body_fn=lambda **kw: patches.append(kw["body"]),  # noqa: ARG005
        post_comment=lambda **kw: comments.append(kw["body"]) or 1,  # noqa: ARG005
    )
    _insert(
        store,
        "d-b4",
        _edited_payload(
            issue=58, sender="huozheclaude", body=body, body_from=prev
        ),
        issue=58,
        sender="huozheclaude",
    )
    loop.process_deferred_batch()
    assert len(patches) == 1
    assert checkbox_is_checked(patches[0], strict=True) is False
    assert CHECKBOX_UNCHECKED in patches[0]
    assert "smoke the CLI" in patches[0]
    assert len(comments) == 1
    store.close()


def test_agent_bare_tick_outside_sentinels_neutralized(tmp_path: Path) -> None:
    """B5: bare - [x] with no protocol block → uncheck and warn."""
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    _seed(store, verified_at=None)
    prev = "## Goal\n\nNo block yet.\n"
    body = (
        "## Goal\n\nNo block yet.\n\n"
        "- [x] Human Verification Complete\n"
    )
    patches: list[str] = []
    comments: list[str] = []

    loop = DesignLoop(
        store,
        cfg,
        supervisor=None,
        dispatch_turns=False,
        gateway_token="gw",
        get_issue_body_fn=lambda **_: body,
        patch_issue_body_fn=lambda **kw: patches.append(kw["body"]),  # noqa: ARG005
        post_comment=lambda **kw: comments.append(kw["body"]) or 1,  # noqa: ARG005
    )
    _insert(
        store,
        "d-b5",
        _edited_payload(
            issue=58, sender="huozheclaude", body=body, body_from=prev
        ),
        issue=58,
        sender="huozheclaude",
    )
    loop.process_deferred_batch()
    assert len(patches) == 1
    assert checkbox_is_checked(patches[0], strict=True) is None
    assert checkbox_is_checked(patches[0], strict=False) is False
    assert CHECKBOX_UNCHECKED in patches[0]
    assert CHECKBOX_CHECKED not in patches[0]
    assert len(comments) == 1
    assert "bare" in comments[0].lower() or "outside" in comments[0].lower()
    store.close()


def test_checkbox_strict_ignores_bare_tick() -> None:
    body = "## Goal\n\n- [x] Human Verification Complete\n"
    assert checkbox_is_checked(body, strict=True) is None
    assert checkbox_is_checked(body, strict=False) is True
    assert checkbox_is_checked(_body(checked=True), strict=True) is True


def test_failed_patch_does_not_claim_restored(tmp_path: Path) -> None:
    """NB: do not tell the owner we restored if PATCH failed."""
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    _seed(store, verified_at=None)
    prev = _body(checked=False)
    body = _body(checked=True)
    comments: list = []

    def boom(**kw):  # noqa: ANN001, ARG001
        raise RuntimeError("github down")

    loop = DesignLoop(
        store,
        cfg,
        supervisor=None,
        dispatch_turns=False,
        gateway_token="gw",
        get_issue_body_fn=lambda **_: body,
        patch_issue_body_fn=boom,
        post_comment=lambda **kw: comments.append(kw) or 1,  # noqa: ARG005
    )
    _insert(
        store,
        "d-patch-fail",
        _edited_payload(
            issue=58, sender="huozheclaude", body=body, body_from=prev
        ),
        issue=58,
        sender="huozheclaude",
    )
    loop.process_deferred_batch()
    assert comments == []
    store.close()


def test_missing_body_from_skips_restore(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    _seed(store, verified_at=None)
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
        "d-no-from",
        _edited_payload(
            issue=58, sender="huozheclaude", body=body, body_from=None
        ),
        issue=58,
        sender="huozheclaude",
    )
    loop.process_deferred_batch()
    assert patches == []
    store.close()


def test_gateway_edit_ignored(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    sk = _seed(store)
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
    cfg = _cfg(tmp_path, owner="huozhegrok")
    sk = _seed(store)
    prev = _body(checked=False)
    body = _body(checked=True)
    patches: list = []
    comments: list = []
    loop = DesignLoop(
        store,
        cfg,
        supervisor=None,
        dispatch_turns=False,
        gateway_token="gw",
        get_issue_body_fn=lambda **_: body,
        patch_issue_body_fn=lambda **kw: patches.append(kw.get("body")),  # noqa: ARG005
        post_comment=lambda **kw: comments.append(kw) or 1,  # noqa: ARG005
    )
    _insert(
        store,
        "d-owner-agent",
        _edited_payload(
            issue=58, sender="huozhegrok", body=body, body_from=prev
        ),
        issue=58,
        sender="huozhegrok",
    )
    loop.process_deferred_batch()
    assert store.get_session(sk)["verified_at"] is None
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
            issue=58,
            sender="huozhe",
            body=_body(checked=True),
            body_changed=False,
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
    prev = _body(checked=False)
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
        _edited_payload(
            issue=58, sender="huozheclaude", body=body, body_from=prev
        ),
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


def test_adr15_corrected_body_survives_stale_corrupting_delivery(
    tmp_path: Path,
) -> None:
    """#49: drain the 25-char wreck after a refined correction — no PATCH."""
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    _seed(store, verified_at=None)
    prev = _body(checked=False, steps=["scaffold"])
    wreck = "@/tmp/issue49_new_body.md"
    assert len(wreck) < 0.5 * len(prev)
    corrected = _body(checked=False, steps=["human-runnable refine"])
    patches: list = []
    fetches: list[int] = []

    loop = DesignLoop(
        store,
        cfg,
        supervisor=None,
        dispatch_turns=False,
        gateway_token="gw",
        get_issue_body_fn=lambda **_: fetches.append(1) or corrected,
        patch_issue_body_fn=lambda **kw: patches.append(kw),  # noqa: ARG005
        post_comment=lambda **kw: 1,  # noqa: ARG005
    )
    _insert(
        store,
        "d-wreck",
        _edited_payload(
            issue=58, sender="huozheclaude", body=wreck, body_from=prev
        ),
        issue=58,
        sender="huozheclaude",
    )
    loop.process_deferred_batch()
    assert fetches == [1]
    assert patches == []
    assert store.get_session("huozhe/code-workflow#58")["state"] == (
        "AWAITING_VERIFICATION"
    )
    store.close()


def test_adr15_owner_tick_in_window_skips_patch(tmp_path: Path) -> None:
    """Owner ticked on a later body; stale B2 must not overwrite or stamp."""
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    sk = _seed(store, verified_at=None)
    prev = _body(checked=False)
    wreck = "## Goal\n\nblock deleted by agent.\n"
    current = _body(checked=True)
    patches: list = []

    loop = DesignLoop(
        store,
        cfg,
        supervisor=None,
        dispatch_turns=False,
        gateway_token="gw",
        get_issue_body_fn=lambda **_: current,
        patch_issue_body_fn=lambda **kw: patches.append(kw),  # noqa: ARG005
        post_comment=lambda **kw: 1,  # noqa: ARG005
    )
    _insert(
        store,
        "d-tick-window",
        _edited_payload(
            issue=58, sender="huozheclaude", body=wreck, body_from=prev
        ),
        issue=58,
        sender="huozheclaude",
    )
    loop.process_deferred_batch()
    assert patches == []
    assert store.get_session(sk)["verified_at"] is None
    store.close()


def test_adr15_redelivery_patches_at_most_once(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    _seed(store, verified_at=None)
    prev = _body(checked=False)
    body = _body(checked=True)
    live = {"body": body}
    patches: list[str] = []

    def fake_get(**_):  # noqa: ANN003
        return live["body"]

    def fake_patch(*, repo, issue_num, body, token):  # noqa: ANN001
        patches.append(body)
        live["body"] = body

    loop = DesignLoop(
        store,
        cfg,
        supervisor=None,
        dispatch_turns=False,
        gateway_token="gw",
        get_issue_body_fn=fake_get,
        patch_issue_body_fn=fake_patch,
        post_comment=lambda **kw: 1,  # noqa: ARG005
    )
    payload = _edited_payload(
        issue=58, sender="huozheclaude", body=body, body_from=prev
    )
    _insert(store, "d-once", payload, issue=58, sender="huozheclaude")
    loop.process_deferred_batch()
    assert len(patches) == 1
    store.set_delivery_status("d-once", "deferred")
    loop.process_deferred_batch()
    assert len(patches) == 1
    store.close()


def test_adr15_collapsed_current_escalates_no_patch(tmp_path: Path) -> None:
    """Wreck still live at drain time → escalate, do not splice onto it."""
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    sk = _seed(store, verified_at=None)
    prev = _body(checked=False, steps=["scaffold"])
    wreck = "@/tmp/issue49_new_body.md"
    patches: list = []
    comments: list[str] = []

    loop = DesignLoop(
        store,
        cfg,
        supervisor=None,
        dispatch_turns=False,
        gateway_token="gw",
        get_issue_body_fn=lambda **_: wreck,
        patch_issue_body_fn=lambda **kw: patches.append(kw),  # noqa: ARG005
        post_comment=lambda **kw: comments.append(kw["body"]) or 1,  # noqa: ARG005
    )
    _insert(
        store,
        "d-floor",
        _edited_payload(
            issue=58, sender="huozheclaude", body=wreck, body_from=prev
        ),
        issue=58,
        sender="huozheclaude",
    )
    loop.process_deferred_batch()
    assert patches == []
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["state"] == "PAUSED_HUMAN"
    assert sess["resume_state"] == "AWAITING_VERIFICATION"
    assert comments and "Gateway wrote nothing" in comments[0]
    assert "collapsed" in comments[0]
    store.close()


def test_agent_untick_without_verified_at_does_not_restore_up(
    tmp_path: Path, caplog
) -> None:
    """#89: agent-made was=True has no authority. Leave the body; log loudly."""
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    sk = _seed(store, verified_at=None)
    prev = _body(checked=True)
    body = _body(checked=False)
    patches: list = []
    comments: list = []

    loop = DesignLoop(
        store,
        cfg,
        supervisor=None,
        dispatch_turns=False,
        gateway_token="gw",
        get_issue_body_fn=lambda **_: body,
        patch_issue_body_fn=lambda **kw: patches.append(kw),  # noqa: ARG005
        post_comment=lambda **kw: comments.append(kw) or 1,  # noqa: ARG005
    )
    _insert(
        store,
        "d-forge-untick",
        _edited_payload(
            issue=58, sender="huozhegrok", body=body, body_from=prev
        ),
        issue=58,
        sender="huozhegrok",
    )
    with caplog.at_level("WARNING"):
        loop.process_deferred_batch()
    assert store.get_session(sk)["verified_at"] is None
    assert patches == []
    assert comments == []
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "raise refused" in text
    assert "branch=restore-up" in text
    assert "was=True" in text
    assert "now=False" in text
    assert "verified_at=None" in text
    assert "huozhegrok" in text
    store.close()


def test_agent_tick_then_untick_queued_does_not_restore_up(tmp_path: Path) -> None:
    """#89 live shape: both edits queued; box ends unchecked, no restore-up PATCH."""
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    sk = _seed(store, verified_at=None)
    unchecked = _body(checked=False)
    ticked = _body(checked=True)
    live = {"body": unchecked}
    patches: list[str] = []

    def fake_get(**_):  # noqa: ANN003
        return live["body"]

    def fake_patch(*, repo, issue_num, body, token):  # noqa: ANN001
        patches.append(body)
        live["body"] = body

    loop = DesignLoop(
        store,
        cfg,
        supervisor=None,
        dispatch_turns=False,
        gateway_token="gw",
        get_issue_body_fn=fake_get,
        patch_issue_body_fn=fake_patch,
        post_comment=lambda **kw: 1,  # noqa: ARG005
    )
    _insert(
        store,
        "d-tick",
        _edited_payload(
            issue=58, sender="huozhegrok", body=ticked, body_from=unchecked
        ),
        issue=58,
        sender="huozhegrok",
    )
    _insert(
        store,
        "d-untick",
        _edited_payload(
            issue=58, sender="huozhegrok", body=unchecked, body_from=ticked
        ),
        issue=58,
        sender="huozhegrok",
    )
    loop.process_deferred_batch()
    assert store.get_session(sk)["verified_at"] is None
    assert checkbox_is_checked(live["body"], strict=True) is False
    assert all(checkbox_is_checked(p, strict=True) is not True for p in patches)
    store.close()


def test_agent_tick_then_delete_line_queued_does_not_restore_up(
    tmp_path: Path, caplog
) -> None:
    """#91 B1: delete the line, don't untick — B4 must not materialise a tick."""
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    sk = _seed(store, verified_at=None)
    s0 = _body(checked=False)
    s1 = _body(checked=True)
    s2 = s1.replace(CHECKBOX_CHECKED + "\n", "")
    assert checkbox_is_checked(s2, strict=True) is None
    live = {"body": s2}
    patches: list[str] = []

    def fake_get(**_):  # noqa: ANN003
        return live["body"]

    def fake_patch(*, repo, issue_num, body, token):  # noqa: ANN001
        patches.append(body)
        live["body"] = body

    loop = DesignLoop(
        store,
        cfg,
        supervisor=None,
        dispatch_turns=False,
        gateway_token="gw",
        get_issue_body_fn=fake_get,
        patch_issue_body_fn=fake_patch,
        post_comment=lambda **kw: 1,  # noqa: ARG005
    )
    _insert(
        store,
        "d-A",
        _edited_payload(issue=58, sender="huozhegrok", body=s1, body_from=s0),
        issue=58,
        sender="huozhegrok",
    )
    _insert(
        store,
        "d-B",
        _edited_payload(issue=58, sender="huozhegrok", body=s2, body_from=s1),
        issue=58,
        sender="huozhegrok",
    )
    with caplog.at_level("WARNING"):
        loop.process_deferred_batch()
    assert store.get_session(sk)["verified_at"] is None
    assert checkbox_is_checked(live["body"], strict=True) is not True
    assert all(checkbox_is_checked(p, strict=True) is not True for p in patches)
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "raise refused" in text
    assert "branch=B4" in text
    store.close()


def test_agent_tick_then_delete_block_queued_does_not_restore_up(
    tmp_path: Path,
) -> None:
    """Same rule, B2 spelling: delete the whole block after an agent tick."""
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    sk = _seed(store, verified_at=None)
    s0 = _body(checked=False)
    s1 = _body(checked=True)
    s2 = (
        "## Goal\n\nSession work — block deleted by agent.\n\n"
        + ("Keep the session goal and checklist intact. " * 20)
        + "\n"
    )
    live = {"body": s2}
    patches: list[str] = []

    def fake_get(**_):  # noqa: ANN003
        return live["body"]

    def fake_patch(*, repo, issue_num, body, token):  # noqa: ANN001
        patches.append(body)
        live["body"] = body

    loop = DesignLoop(
        store,
        cfg,
        supervisor=None,
        dispatch_turns=False,
        gateway_token="gw",
        get_issue_body_fn=fake_get,
        patch_issue_body_fn=fake_patch,
        post_comment=lambda **kw: 1,  # noqa: ARG005
    )
    _insert(
        store,
        "d-A-block",
        _edited_payload(issue=58, sender="huozhegrok", body=s1, body_from=s0),
        issue=58,
        sender="huozhegrok",
    )
    _insert(
        store,
        "d-B-block",
        _edited_payload(issue=58, sender="huozhegrok", body=s2, body_from=s1),
        issue=58,
        sender="huozhegrok",
    )
    loop.process_deferred_batch()
    assert store.get_session(sk)["verified_at"] is None
    assert checkbox_is_checked(live["body"], strict=True) is not True
    assert all(checkbox_is_checked(p, strict=True) is not True for p in patches)
    store.close()


def test_agent_deletes_ticked_line_with_verified_at_restores_up(
    tmp_path: Path,
) -> None:
    """Owner tick recorded; agent deletes the line — B4 still restores up."""
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    sk = _seed(store, verified_at=int(time.time()))
    prev = _body(checked=True)
    body = prev.replace(CHECKBOX_CHECKED + "\n", "")
    patches: list[str] = []

    loop = DesignLoop(
        store,
        cfg,
        supervisor=None,
        dispatch_turns=False,
        gateway_token="gw",
        get_issue_body_fn=lambda **_: body,
        patch_issue_body_fn=lambda **kw: patches.append(kw["body"]),  # noqa: ARG005
        post_comment=lambda **kw: 1,  # noqa: ARG005
    )
    _insert(
        store,
        "d-b4-owner",
        _edited_payload(
            issue=58, sender="huozhegrok", body=body, body_from=prev
        ),
        issue=58,
        sender="huozhegrok",
    )
    loop.process_deferred_batch()
    assert store.get_session(sk)["verified_at"] is not None
    assert len(patches) == 1
    assert checkbox_is_checked(patches[0], strict=True) is True
    store.close()


def test_adr15_get_failure_skips_restore(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    _seed(store, verified_at=None)
    prev = _body(checked=False)
    body = _body(checked=True)
    patches: list = []

    def boom(**_):  # noqa: ANN003
        raise RuntimeError("github down")

    loop = DesignLoop(
        store,
        cfg,
        supervisor=None,
        dispatch_turns=False,
        gateway_token="gw",
        get_issue_body_fn=boom,
        patch_issue_body_fn=lambda **kw: patches.append(kw),  # noqa: ARG005
    )
    _insert(
        store,
        "d-get-fail",
        _edited_payload(
            issue=58, sender="huozheclaude", body=body, body_from=prev
        ),
        issue=58,
        sender="huozheclaude",
    )
    loop.process_deferred_batch()
    assert patches == []
    store.close()
