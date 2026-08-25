"""ADR-37 / #211: recover a dropped synchronize from head-SHA drift."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import agentd.design_loop as design_loop_mod
from agentd.config import Config
from agentd.db import Store, decompress_payload
from agentd.design_loop import DesignLoop
from agentd.gitops import role_branch_name
from agentd.reconciler import Reconciler

_REPO = "huozhe/code-workflow"
_X = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_Y = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


@pytest.fixture(autouse=True)
def _clear_spent() -> None:
    design_loop_mod._spent_prs.clear()
    yield
    design_loop_mod._spent_prs.clear()


def _cfg(tmp: Path) -> Config:
    return Config(
        raw={
            "host": {"owner": "huozhe"},
            "gateway": {"login": "huozhegateway"},
            "agents": {
                "claude": {"login": "huozheclaude"},
                "grok": {"login": "huozhegrok"},
            },
        },
        root=tmp,
    )


def _seed(
    store: Store,
    *,
    issue: int = 57,
    pr: int = 207,
    state: str = "DESIGN_REWORK",
    design_pr_head: str | None = _X,
    feature_pr: int | None = None,
    feature_pr_head: str | None = None,
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
        design_pr=pr,
    )
    fields: dict[str, Any] = {}
    if design_pr_head is not None:
        fields["design_pr_head"] = design_pr_head
    if feature_pr is not None:
        fields["feature_pr"] = feature_pr
    if feature_pr_head is not None:
        fields["feature_pr_head"] = feature_pr_head
    if silent_turns:
        fields["silent_turns"] = silent_turns
    if fields:
        store.update_session_fields(sk, **fields)
    return sk


def _design_snap(*, pr: int = 207, sha: str = _Y, merged: bool = False) -> dict[str, Any]:
    return {
        "issue_state": "open",
        "issue_body": "",
        "issue_title": "t",
        "issue_html_url": f"https://github.com/{_REPO}/issues/57",
        "issue_labels": [],
        "nodes": [
            {
                "id": "PR_design",
                "kind": "pull_request",
                "number": pr,
                "author": "huozheclaude",
                "title": "RFC",
                "head_ref": role_branch_name(_REPO, 57, "architect"),
                "head_sha": sha,
                "merged": merged,
            }
        ],
        "design_merged": merged,
        "feature_merged": False,
        "design_head_sha": sha,
        "feature_head_sha": "",
    }


def _feature_snap(*, pr: int = 213, sha: str = _Y) -> dict[str, Any]:
    return {
        "issue_state": "open",
        "issue_body": "",
        "issue_title": "t",
        "issue_html_url": f"https://github.com/{_REPO}/issues/57",
        "issue_labels": [],
        "nodes": [
            {
                "id": "PR_feat",
                "kind": "pull_request",
                "number": pr,
                "author": "huozhegrok",
                "title": "feat",
                "head_ref": role_branch_name(_REPO, 57, "developer"),
                "head_sha": sha,
                "merged": False,
            }
        ],
        "design_merged": False,
        "feature_merged": False,
        "design_head_sha": "",
        "feature_head_sha": sha,
    }


def _sweep(store: Store, snap: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
    rec = Reconciler(
        store,
        list_containers=list,
        remove_container=lambda _c: None,
        fetch_snapshot=lambda sess: dict(snap),
    )
    return rec.reconcile_once(dry_run=dry_run)


def _loop(store: Store, tmp: Path, *, fetch_pr: Any | None = None) -> DesignLoop:
    kw: dict[str, Any] = {}
    if fetch_pr is not None:
        kw["fetch_pr"] = fetch_pr
    loop = DesignLoop(
        store,
        _cfg(tmp),
        supervisor=object(),
        dispatch_turns=True,
        github_token="tok",
        fetch_threads=lambda **k: None,
        fetch_diff=lambda **k: None,
        **kw,
    )
    dispatched: list[dict[str, Any]] = []

    def _spy(**kw: Any) -> dict[str, Any]:
        dispatched.append(kw)
        return {"status": "done", "summary": "ok", "public_actions": [{"n": 1}]}

    loop._dispatch_turn = _spy  # type: ignore[method-assign]
    loop.dispatched = dispatched  # type: ignore[attr-defined]
    return loop


def _insert_sync(
    store: Store,
    *,
    did: str,
    pr: int,
    sha: str,
    sender: str = "huozheclaude",
    ref: str | None = None,
) -> None:
    store.insert_delivery(
        delivery_id=did,
        event="pull_request",
        action="synchronize",
        repo=_REPO,
        issue_num=pr,
        sender=sender,
        payload=json.dumps(
            {
                "action": "synchronize",
                "pull_request": {
                    "number": pr,
                    "user": {"login": sender},
                    "head": {
                        "sha": sha,
                        "ref": ref or role_branch_name(_REPO, 57, "architect"),
                    },
                    "merged": False,
                },
                "sender": {"login": sender},
                "repository": {"full_name": _REPO},
            }
        ).encode(),
        status="deferred",
    )


def test_rework_head_drift_enqueues_one_synchronize_without_adopt(
    tmp_path: Path,
) -> None:
    """Acceptance (1): must fail first against today's synthesized=0."""
    store = Store(tmp_path / "state.db")
    sk = _seed(store)
    report = _sweep(store, _design_snap())
    queued = [dict(r) for r in store.list_queued()]
    assert report["synthesized"] == 1
    assert len(queued) == 1
    row = queued[0]
    assert row["event"] == "pull_request"
    assert row["action"] == "synchronize"
    assert row["delivery_id"] == f"recon:sync:207:{_Y}"
    assert store.get_session(sk)["state"] == "DESIGN_REWORK"
    store.close()


def test_second_pass_inserts_zero(tmp_path: Path) -> None:
    """Acceptance (2): INSERT OR IGNORE on recon:sync:<pr>:<sha>."""
    store = Store(tmp_path / "state.db")
    _seed(store)
    snap = _design_snap()
    assert _sweep(store, snap)["synthesized"] == 1
    assert _sweep(store, snap)["synthesized"] == 0
    assert len(store.list_queued()) == 1
    store.close()


def test_synth_payload_has_author_and_sha(tmp_path: Path) -> None:
    """Acceptance (3): user.login, sender, head.sha — the #209 hole must fail this."""
    store = Store(tmp_path / "state.db")
    _seed(store)
    _sweep(store, _design_snap())
    row = dict(store.list_queued()[0])
    data = json.loads(decompress_payload(row["payload"]))
    assert data["pull_request"]["user"]["login"] == "huozheclaude"
    assert data["sender"]["login"] == "huozheclaude"
    assert data["pull_request"]["head"]["sha"] == _Y
    store.close()


def test_duplicate_sha_dispatches_no_turn(tmp_path: Path) -> None:
    """Acceptance (4): payload SHA == watermark → spent."""
    store = Store(tmp_path / "state.db")
    sk = _seed(store, state="DESIGN_REVIEW", design_pr_head=_Y, silent_turns=0)
    loop = _loop(
        store,
        tmp_path,
        fetch_pr=lambda **k: {"merged": False, "state": "open", "head_sha": _Y},
    )
    _insert_sync(store, did="d-dup", pr=207, sha=_Y)
    loop.process_deferred_batch(limit=5)
    assert loop.dispatched == []  # type: ignore[attr-defined]
    sess = store.get_session(sk)
    assert sess is not None
    assert int(sess.get("silent_turns") or 0) == 0
    assert sess["state"] == "DESIGN_REVIEW"
    store.close()


def test_stale_sha_dispatches_no_turn_even_when_unequal(tmp_path: Path) -> None:
    """Acceptance (4′): payload X, watermark Y, live Y. Fails *not equal → take*."""
    store = Store(tmp_path / "state.db")
    sk = _seed(store, state="DESIGN_REVIEW", design_pr_head=_Y)
    loop = _loop(
        store,
        tmp_path,
        fetch_pr=lambda **k: {"merged": False, "state": "open", "head_sha": _Y},
    )
    _insert_sync(store, did="d-stale", pr=207, sha=_X)
    loop.process_deferred_batch(limit=5)
    assert loop.dispatched == []  # type: ignore[attr-defined]
    assert store.get_session(sk)["state"] == "DESIGN_REVIEW"
    store.close()


def test_live_head_mismatch_from_watermark_still_routes(tmp_path: Path) -> None:
    """Acceptance (5): payload == live != watermark → design_revised, one turn."""
    store = Store(tmp_path / "state.db")
    sk = _seed(store, state="DESIGN_REWORK", design_pr_head=_X)
    loop = _loop(
        store,
        tmp_path,
        fetch_pr=lambda **k: {"merged": False, "state": "open", "head_sha": _Y},
    )
    _insert_sync(store, did="d-fwd", pr=207, sha=_Y)
    loop.process_deferred_batch(limit=5)
    assert len(loop.dispatched) == 1  # type: ignore[attr-defined]
    assert loop.dispatched[0]["role"] == "developer"  # type: ignore[attr-defined]
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["state"] == "DESIGN_REVIEW"
    assert sess["design_pr_head"] == _Y
    store.close()


def test_code_rework_feature_drift_synths_architect_turn(tmp_path: Path) -> None:
    """Acceptance (6): CODE_REWORK + Feature head drift → feature_revised."""
    store = Store(tmp_path / "state.db")
    sk = _seed(
        store,
        state="CODE_REWORK",
        design_pr_head=None,
        feature_pr=213,
        feature_pr_head=_X,
    )
    report = _sweep(store, _feature_snap())
    assert report["synthesized"] == 1
    loop = _loop(
        store,
        tmp_path,
        fetch_pr=lambda **k: {"merged": False, "state": "open", "head_sha": _Y},
    )
    loop._process_one(store.list_queued()[0])
    assert len(loop.dispatched) == 1  # type: ignore[attr-defined]
    assert loop.dispatched[0]["role"] == "architect"  # type: ignore[attr-defined]
    assert store.get_session(sk)["state"] == "CODE_REVIEW"
    store.close()


def test_check_suite_does_not_advance_or_dispatch(tmp_path: Path) -> None:
    """Acceptance (7): Option C must fail. Suite arrived; session stayed REWORK."""
    store = Store(tmp_path / "state.db")
    sk = _seed(store)
    loop = _loop(store, tmp_path)
    store.insert_delivery(
        delivery_id="d-suite",
        event="check_suite",
        action="completed",
        repo=_REPO,
        issue_num=57,
        sender="github",
        payload=json.dumps(
            {
                "action": "completed",
                "check_suite": {"head_sha": _Y},
                "repository": {"full_name": _REPO},
            }
        ).encode(),
        status="deferred",
    )
    loop.process_deferred_batch(limit=5)
    assert store.get_session(sk)["state"] == "DESIGN_REWORK"
    kinds = [
        (d.get("dig") or {}).get("kind")
        for d in loop.dispatched  # type: ignore[attr-defined]
    ]
    assert "design_revised" not in kinds
    store.close()


def test_null_watermark_review_stamps_rework_synths(tmp_path: Path) -> None:
    """Acceptance (8): NULL+REVIEW stamps; NULL+REWORK synths once."""
    store = Store(tmp_path / "state.db")
    sk_rev = _seed(
        store, issue=10, pr=11, state="DESIGN_REVIEW", design_pr_head=None
    )
    snap_rev = _design_snap(pr=11, sha=_Y)
    snap_rev["issue_html_url"] = f"https://github.com/{_REPO}/issues/10"
    rec = Reconciler(
        store,
        list_containers=list,
        remove_container=lambda _c: None,
        fetch_snapshot=lambda sess: (
            snap_rev if sess.get("issue_num") == 10 else _design_snap(pr=207)
        ),
    )
    sk_rw = _seed(store, issue=57, pr=207, state="DESIGN_REWORK", design_pr_head=None)
    rec.reconcile_once()
    assert store.get_session(sk_rev)["design_pr_head"] == _Y
    ids = {str(r["delivery_id"]) for r in store.list_queued()}
    assert f"recon:sync:11:{_Y}" not in ids
    assert f"recon:sync:207:{_Y}" in ids
    assert store.get_session(sk_rw)["state"] == "DESIGN_REWORK"
    store.close()


def test_merged_design_pr_does_not_synth_and_still_adopts(tmp_path: Path) -> None:
    """Acceptance (9): merged + moved head synthesizes nothing; step 4 adopt runs."""
    store = Store(tmp_path / "state.db")
    sk = _seed(store, state="DESIGN_APPROVED", design_pr_head=_X)
    store.insert_delivery(
        delivery_id="opened",
        event="pull_request",
        action="opened",
        repo=_REPO,
        issue_num=207,
        sender="huozheclaude",
        payload=json.dumps(
            {"pull_request": {"node_id": "PR_design", "number": 207}}
        ).encode(),
        status="done",
    )
    snap = _design_snap(merged=True)
    report = _sweep(store, snap)
    assert report["synthesized"] == 0
    assert report["adopted"] == 1
    assert store.get_session(sk)["state"] == "IMPLEMENTING"
    assert store.list_queued() == []
    store.close()
