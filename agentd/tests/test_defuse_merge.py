"""ADR-24: defuse Feature PR closing keywords before merge_authorized."""

from __future__ import annotations

import json
from pathlib import Path

from agentd.config import Config
from agentd.db import Store
from agentd.design_loop import DesignLoop
from agentd.gitops import role_branch_name


def _cfg(tmp: Path) -> Config:
    return Config(
        raw={
            "host": {"owner": "huozhe"},
            "gateway": {"login": "huozhegateway"},
            "agents": {
                "claude": {"login": "huozheclaude"},
                "grok": {"login": "huozhegrok"},
            },
            "repos": {"huozhe/code-workflow": {"required_checks": ["ci/test"]}},
        },
        root=tmp,
    )


def _seed(store: Store, *, issue: int = 100, pr: int = 60) -> str:
    sk = f"huozhe/code-workflow#{issue}"
    store.upsert_session(
        session_key=sk,
        project_key="huozhe/code-workflow",
        repo="huozhe/code-workflow",
        issue_num=issue,
        state="CODE_REVIEW",
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


def _approve_and_drain(
    store: Store,
    tmp: Path,
    *,
    body: str,
    patch_fn,
    issue: int = 100,
    pr: int = 60,
    extra_sessions: list[int] | None = None,
) -> DesignLoop:
    for n in extra_sessions or []:
        store.upsert_session(
            session_key=f"huozhe/code-workflow#{n}",
            project_key="huozhe/code-workflow",
            repo="huozhe/code-workflow",
            issue_num=n,
            state="CLOSED",
            architect="huozheclaude",
            developer="huozhegrok",
            created_at=1,
            updated_at=1,
        )
    feat_ref = role_branch_name("huozhe/code-workflow", issue, "developer")
    head = "abc123def456"

    def fake_get(url: str, *, token: str):
        if url.endswith("/reviews"):
            return [
                {
                    "user": {"login": "huozheclaude"},
                    "state": "APPROVED",
                    "commit_id": head,
                }
            ]
        if f"/pulls/{pr}" in url and not url.endswith("/reviews"):
            return {
                "number": pr,
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
    loop = DesignLoop(
        store,
        _cfg(tmp),
        supervisor=None,
        dispatch_turns=False,
        post_comment=lambda **k: 1,
        gateway_token="gw",
        get_issue_body_fn=lambda **k: body,
        patch_issue_body_fn=patch_fn,
    )
    try:
        store.insert_delivery(
            delivery_id="d-auth",
            event="pull_request_review",
            action="submitted",
            repo="huozhe/code-workflow",
            issue_num=pr,
            sender="huozheclaude",
            payload=json.dumps(
                {
                    "action": "submitted",
                    "review": {
                        "id": 9,
                        "state": "APPROVED",
                        "commit_id": head,
                    },
                    "pull_request": {
                        "number": pr,
                        "head": {"sha": head, "ref": feat_ref},
                    },
                    "repository": {"full_name": "huozhe/code-workflow"},
                    "sender": {"login": "huozheclaude"},
                }
            ).encode(),
            status="deferred",
        )
        loop.process_deferred_batch()
    finally:
        vmod.verify_feature_merge = real  # type: ignore[assignment]
    return loop


def test_fixes_session_issue_is_rewritten_then_authorized(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(store)
    patches: list[str] = []

    def patch(**k):
        patches.append(k["body"])

    _approve_and_drain(store, tmp_path, body="Fixes #100", patch_fn=patch)
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["state"] == "MERGING"
    assert patches == ["Refs #100"]
    assert not sess.get("paused_reason")
    store.close()


def test_list_item_keyword_only(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    _seed(store)
    patches: list[str] = []
    _approve_and_drain(
        store, tmp_path, body="- Closes #100.", patch_fn=lambda **k: patches.append(k["body"])
    )
    assert patches == ["- Refs #100."]
    store.close()


def test_near_miss_does_not_patch(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    _seed(store, issue=118)
    store.update_session_fields("huozhe/code-workflow#118", feature_pr=60)
    patches: list = []
    body = (
        "Amends **ADR-21** so the closed-issue marker re-arms on the "
        "**reopen event**, not on a sweep observation. Closes the wording "
        "half of **#118**."
    )
    _approve_and_drain(
        store, tmp_path, body=body, issue=118, patch_fn=lambda **k: patches.append(k)
    )
    assert patches == []
    assert store.get_session("huozhe/code-workflow#118")["state"] == "MERGING"
    store.close()


def test_non_session_issue_does_not_patch(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    _seed(store)
    patches: list = []
    _approve_and_drain(
        store, tmp_path, body="Closes #57", patch_fn=lambda **k: patches.append(k)
    )
    assert patches == []
    assert store.get_session("huozhe/code-workflow#100")["state"] == "MERGING"
    store.close()


def test_other_session_issue_is_rewritten(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    _seed(store)
    patches: list[str] = []
    _approve_and_drain(
        store,
        tmp_path,
        body="Closes #32",
        extra_sessions=[32],
        patch_fn=lambda **k: patches.append(k["body"]),
    )
    assert patches == ["Refs #32"]
    store.close()


def test_patch_failure_refuses_and_escalates(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(store)

    def boom(**k):
        raise RuntimeError("patch failed")

    _approve_and_drain(store, tmp_path, body="Fixes #100", patch_fn=boom)
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["state"] == "PAUSED_HUMAN"
    assert sess.get("paused_reason")
    store.close()
