"""M5-0 / #50: §10.1 verification block render, upsert, and feature_merged write."""

from __future__ import annotations

import json
from pathlib import Path

from agentd.config import Config
from agentd.db import Store
from agentd.design_loop import DesignLoop
from agentd.gitops import role_branch_name
from agentd.verification import (
    CHECKBOX_CHECKED,
    CHECKBOX_UNCHECKED,
    ORDERING_LINE,
    SENTINEL_CLOSE,
    SENTINEL_OPEN,
    checkbox_is_checked,
    default_steps_for_session,
    extract_verification_block,
    format_merged_prs,
    render_verification_block,
    upsert_verification_block,
)


def test_render_contains_sentinels_checkbox_ordering() -> None:
    block = render_verification_block(
        steps=["pull", "test"],
        merged_prs=[59, 60],
        not_covered="SSO",
        checked=False,
    )
    assert block.startswith(SENTINEL_OPEN)
    assert block.endswith(SENTINEL_CLOSE)
    assert "## Verification Protocol" in block
    assert "1. pull" in block
    assert "2. test" in block
    assert format_merged_prs([59, 60]) in block
    assert "**Not covered:** SSO" in block
    assert CHECKBOX_UNCHECKED in block
    assert ORDERING_LINE in block
    assert checkbox_is_checked(block) is False


def test_upsert_appends_without_touching_human_prose() -> None:
    body = "## Goal\n\nDo the thing.\n\nOwner notes stay put.\n"
    out = upsert_verification_block(
        body,
        steps=["a"],
        merged_prs=[1],
        not_covered="x",
    )
    assert out.startswith("## Goal")
    assert "Owner notes stay put." in out
    assert extract_verification_block(out)
    # Second upsert replaces only the block
    out2 = upsert_verification_block(
        out,
        steps=["b", "c"],
        merged_prs=[1, 2],
        not_covered="y",
    )
    assert out2.count(SENTINEL_OPEN) == 1
    assert "1. b" in out2
    assert "2. c" in out2
    assert "**Merged PRs:** #1, #2" in out2
    assert "Owner notes stay put." in out2
    assert "## Goal" in out2


def test_upsert_preserves_checked_box() -> None:
    body = render_verification_block(
        steps=["old"],
        merged_prs=[9],
        checked=True,
    )
    assert CHECKBOX_CHECKED in body
    out = upsert_verification_block(
        "intro\n" + body + "\ntrailer",
        steps=["new"],
        merged_prs=[9, 10],
        preserve_checkbox=True,
    )
    assert CHECKBOX_CHECKED in out
    assert "1. new" in out
    assert "intro" in out
    assert "trailer" in out


def test_checkbox_is_checked_absent() -> None:
    assert checkbox_is_checked("no block here") is None
    assert checkbox_is_checked(render_verification_block(checked=False)) is False
    assert checkbox_is_checked(render_verification_block(checked=True)) is True


def test_default_steps_mention_prs() -> None:
    steps = default_steps_for_session(design_pr=59, feature_pr=60)
    joined = " ".join(steps)
    assert "#59" in joined
    assert "#60" in joined


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
            "repos": {"huozhe/code-workflow": {"required_checks": []}},
        },
        root=tmp,
    )


def test_feature_merged_writes_verification_block(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    issue = 58
    sk = f"huozhe/code-workflow#{issue}"
    store.upsert_session(
        session_key=sk,
        project_key="huozhe/code-workflow",
        repo="huozhe/code-workflow",
        issue_num=issue,
        state="MERGING",
        architect="huozheclaude",
        developer="huozhegrok",
        created_at=1,
        updated_at=1,
    )
    store.update_session_fields(sk, design_pr=59, feature_pr=60)
    store.upsert_runner(
        "huozhe/code-workflow",
        container_id="c1",
        endpoint="127.0.0.1:9",
        token="tok",
        tier="hot",
    )
    feat_ref = role_branch_name("huozhe/code-workflow", issue, "developer")
    store.register_artifact(
        session_key=sk, role="developer", kind="branch", ref=feat_ref
    )

    bodies: dict[int, str] = {issue: "## Session goal\n\nShip M5-0.\n"}
    patches: list[dict] = []

    def fake_get(*, repo, issue_num, token):  # noqa: ANN001
        assert token == "gw-tok"
        assert repo == "huozhe/code-workflow"
        return bodies[int(issue_num)]

    def fake_patch(*, repo, issue_num, body, token):  # noqa: ANN001
        assert token == "gw-tok"
        patches.append({"repo": repo, "issue_num": issue_num, "body": body})
        bodies[int(issue_num)] = body

    loop = DesignLoop(
        store,
        cfg,
        supervisor=None,
        dispatch_turns=False,
        gateway_token="gw-tok",
        get_issue_body_fn=fake_get,
        patch_issue_body_fn=fake_patch,
    )
    store.insert_delivery(
        delivery_id="d-m5-0",
        event="pull_request",
        action="closed",
        repo="huozhe/code-workflow",
        issue_num=60,
        sender="huozhegrok",
        payload=json.dumps(
            {
                "action": "closed",
                "pull_request": {
                    "number": 60,
                    "merged": True,
                    "head": {"sha": "x", "ref": feat_ref},
                },
                "repository": {"full_name": "huozhe/code-workflow"},
                "sender": {"login": "huozhegrok"},
            }
        ).encode(),
        status="deferred",
    )
    loop.process_deferred_batch()
    sess = store.get_session(sk)
    assert sess["state"] == "AWAITING_VERIFICATION"
    assert len(patches) == 1
    block = extract_verification_block(patches[0]["body"])
    assert block is not None
    assert SENTINEL_OPEN in block
    assert CHECKBOX_UNCHECKED in block
    assert ORDERING_LINE in block
    assert "**Merged PRs:** #59, #60" in block
    assert "Ship M5-0." in patches[0]["body"]
    store.close()


def test_feature_merged_idempotent_when_block_unchanged(tmp_path: Path) -> None:
    """Second authorized path not re-entered (AWAITING redelivery), but
    _ensure itself no-ops when body already matches scaffold."""
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    issue = 70
    sk = f"huozhe/code-workflow#{issue}"
    store.upsert_session(
        session_key=sk,
        project_key="huozhe/code-workflow",
        repo="huozhe/code-workflow",
        issue_num=issue,
        state="MERGING",
        architect="huozheclaude",
        developer="huozhegrok",
        created_at=1,
        updated_at=1,
    )
    store.update_session_fields(sk, design_pr=1, feature_pr=2)
    store.upsert_runner(
        "huozhe/code-workflow",
        container_id="c1",
        endpoint="127.0.0.1:9",
        token="tok",
        tier="hot",
    )
    feat_ref = role_branch_name("huozhe/code-workflow", issue, "developer")
    store.register_artifact(
        session_key=sk, role="developer", kind="branch", ref=feat_ref
    )

    scaffold = upsert_verification_block(
        "## Goal\n",
        steps=default_steps_for_session(design_pr=1, feature_pr=2),
        merged_prs=[1, 2],
    )
    bodies = {issue: scaffold}
    patches: list = []

    def fake_get(*, repo, issue_num, token):  # noqa: ANN001, ARG001
        return bodies[int(issue_num)]

    def fake_patch(*, repo, issue_num, body, token):  # noqa: ANN001, ARG001
        patches.append(body)
        bodies[int(issue_num)] = body

    loop = DesignLoop(
        store,
        cfg,
        supervisor=None,
        dispatch_turns=False,
        gateway_token="gw",
        get_issue_body_fn=fake_get,
        patch_issue_body_fn=fake_patch,
    )
    store.insert_delivery(
        delivery_id="d-idem",
        event="pull_request",
        action="closed",
        repo="huozhe/code-workflow",
        issue_num=2,
        sender="huozhegrok",
        payload=json.dumps(
            {
                "action": "closed",
                "pull_request": {
                    "number": 2,
                    "merged": True,
                    "head": {"sha": "x", "ref": feat_ref},
                },
                "repository": {"full_name": "huozhe/code-workflow"},
                "sender": {"login": "huozhegrok"},
            }
        ).encode(),
        status="deferred",
    )
    loop.process_deferred_batch()
    assert store.get_session(sk)["state"] == "AWAITING_VERIFICATION"
    assert patches == []  # body already equal → no PATCH
    store.close()


def test_architect_obligation_mentions_verification_block() -> None:
    import sys

    runner = Path(__file__).resolve().parents[1] / "docker" / "session-runner"
    sys.path.insert(0, str(runner))
    from agentd_runner.turn import role_obligation

    text = role_obligation("architect", "AWAITING_VERIFICATION")
    assert "verification" in text.lower()
    assert "agentd:verification" in text
    assert "Do not close" in text
