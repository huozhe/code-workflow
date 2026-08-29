"""ADR-40 / #229: take *_pr_opened without the spent-SHA arms; discover untracked PRs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import agentd.session_loop as session_loop_mod
from agentd.config import Config
from agentd.db import Store, decompress_payload
from agentd.gitops import role_branch_name
from agentd.reconciler import Reconciler
from agentd.session_loop import SessionLoop

_REPO = "huozhe/code-workflow"
_X = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_Y = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
_ISSUE = 159
_DESIGN_PR = 227
_FEAT_PR = 232


@pytest.fixture(autouse=True)
def _clear_spent() -> None:
    session_loop_mod._spent_prs.clear()
    yield
    session_loop_mod._spent_prs.clear()


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


def _arch_ref(issue: int = _ISSUE) -> str:
    return role_branch_name(_REPO, issue, "architect")


def _dev_ref(issue: int = _ISSUE) -> str:
    return role_branch_name(_REPO, issue, "developer")


def _seed(
    store: Store,
    *,
    issue: int = _ISSUE,
    state: str = "PLANNING",
    design_pr: int | None = None,
    feature_pr: int | None = None,
    silent_turns: int = 0,
    paused_reason: str | None = None,
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
        design_pr=design_pr,
        paused_reason=paused_reason,
    )
    fields: dict[str, Any] = {}
    if feature_pr is not None:
        fields["feature_pr"] = feature_pr
    if silent_turns:
        fields["silent_turns"] = silent_turns
    if fields:
        store.update_session_fields(sk, **fields)
    return sk


def _loop(store: Store, tmp: Path, *, live_sha: str = _Y) -> SessionLoop:
    loop = SessionLoop(
        store,
        _cfg(tmp),
        supervisor=object(),
        dispatch_turns=True,
        github_token="tok",
        fetch_threads=lambda **k: None,
        fetch_diff=lambda **k: None,
        fetch_pr=lambda **k: {"merged": False, "state": "open", "head_sha": live_sha},
    )
    dispatched: list[dict[str, Any]] = []

    def _spy(**kw: Any) -> dict[str, Any]:
        dispatched.append(kw)
        return {"status": "done", "summary": "ok", "public_actions": [{"n": 1}]}

    loop._dispatch_turn = _spy  # type: ignore[method-assign]
    loop.dispatched = dispatched  # type: ignore[attr-defined]
    return loop


def _opened_payload(
    *,
    pr: int,
    sha: str,
    ref: str,
    sender: str,
    node_id: str = "PR_node",
) -> bytes:
    return json.dumps(
        {
            "action": "opened",
            "pull_request": {
                "node_id": node_id,
                "number": pr,
                "merged": False,
                "title": "t",
                "html_url": f"https://github.com/{_REPO}/pull/{pr}",
                "user": {"login": sender},
                "head": {"sha": sha, "ref": ref},
            },
            "sender": {"login": sender},
            "repository": {"full_name": _REPO},
        }
    ).encode()


def _insert_opened(
    store: Store,
    *,
    did: str,
    pr: int,
    sha: str,
    ref: str,
    sender: str,
    node_id: str = "PR_node",
    status: str = "deferred",
) -> None:
    store.insert_delivery(
        delivery_id=did,
        event="pull_request",
        action="opened",
        repo=_REPO,
        issue_num=pr,
        sender=sender,
        payload=_opened_payload(pr=pr, sha=sha, ref=ref, sender=sender, node_id=node_id),
        status=status,
    )


def _insert_sync(store: Store, *, did: str, pr: int, sha: str, ref: str, sender: str) -> None:
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
                    "head": {"sha": sha, "ref": ref},
                    "merged": False,
                },
                "sender": {"login": sender},
                "repository": {"full_name": _REPO},
            }
        ).encode(),
        status="deferred",
    )


def _gh_pr(
    *,
    number: int,
    ref: str,
    sha: str = _Y,
    author: str = "huozhegrok",
    node_id: str | None = None,
    merged: bool = False,
    state: str = "open",
    title: str = "feat",
) -> dict[str, Any]:
    return {
        "number": number,
        "node_id": node_id or f"PR_{number}",
        "title": title,
        "html_url": f"https://github.com/{_REPO}/pull/{number}",
        "merged": merged,
        "state": state,
        "user": {"login": author},
        "head": {"ref": ref, "sha": sha},
    }


def _empty_snap() -> dict[str, Any]:
    return {
        "issue_state": "open",
        "issue_body": "",
        "issue_title": "t",
        "issue_html_url": f"https://github.com/{_REPO}/issues/{_ISSUE}",
        "issue_labels": [],
        "nodes": [],
        "design_merged": False,
        "feature_merged": False,
        "design_head_sha": "",
        "feature_head_sha": "",
    }


def _snap_with_feature_node(*, sha: str = _Y) -> dict[str, Any]:
    snap = _empty_snap()
    snap["nodes"] = [
        {
            "id": f"PR_{_FEAT_PR}",
            "kind": "pull_request",
            "number": _FEAT_PR,
            "author": "huozhegrok",
            "title": "feat",
            "head_ref": _dev_ref(),
            "head_sha": sha,
            "merged": False,
        }
    ]
    snap["feature_head_sha"] = sha
    return snap


def _sweep(
    store: Store,
    *,
    fetch_open_prs: Any,
    fetch_snapshot: Any | None = None,
) -> dict[str, Any]:
    rec = Reconciler(
        store,
        list_containers=list,
        remove_container=lambda _c: None,
        fetch_snapshot=fetch_snapshot or (lambda sess: _empty_snap()),
        fetch_open_prs=fetch_open_prs,
    )
    return rec.reconcile_once()


def _drain_queued(loop: SessionLoop, store: Store) -> None:
    for row in list(store.list_queued()):
        loop._process_one(row)


def test_opened_at_x_live_y_takes_design_half(tmp_path: Path) -> None:
    """Acceptance (1). Ordering is the fixture: payload X, live Y, then drain."""
    store = Store(tmp_path / "state.db")
    sk = _seed(store, state="PLANNING")
    live_sha, payload_sha = _Y, _X
    assert live_sha != payload_sha
    loop = _loop(store, tmp_path, live_sha=live_sha)
    _insert_opened(
        store,
        did="d-open-design",
        pr=_DESIGN_PR,
        sha=payload_sha,
        ref=_arch_ref(),
        sender="huozheclaude",
    )
    loop.process_deferred_batch(limit=5)
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["state"] == "DESIGN_REVIEW"
    assert sess["design_pr"] == _DESIGN_PR
    assert int(sess["roles_locked"] or 0) == 1
    assert sess["design_pr_head"] == live_sha
    assert len(loop.dispatched) == 1  # type: ignore[attr-defined]
    assert loop.dispatched[0]["role"] == "developer"  # type: ignore[attr-defined]
    store.close()


def test_opened_at_x_live_y_takes_code_half(tmp_path: Path) -> None:
    """Acceptance (2). Same ordering on the half that lost the event entirely."""
    store = Store(tmp_path / "state.db")
    sk = _seed(store, state="IMPLEMENTING", design_pr=_DESIGN_PR)
    live_sha, payload_sha = _Y, _X
    assert live_sha != payload_sha
    loop = _loop(store, tmp_path, live_sha=live_sha)
    _insert_opened(
        store,
        did="d-open-feat",
        pr=_FEAT_PR,
        sha=payload_sha,
        ref=_dev_ref(),
        sender="huozhegrok",
    )
    loop.process_deferred_batch(limit=5)
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["state"] == "CODE_REVIEW"
    assert sess["feature_pr"] == _FEAT_PR
    assert sess["feature_pr_head"] == live_sha
    assert len(loop.dispatched) == 1  # type: ignore[attr-defined]
    assert loop.dispatched[0]["role"] == "architect"  # type: ignore[attr-defined]
    store.close()


def test_already_tracked_opened_dispatches_nothing(tmp_path: Path) -> None:
    """Acceptance (3). Tracked-column arm; both orders from (a) total one turn."""
    store = Store(tmp_path / "state.db")
    sk = _seed(store, state="PLANNING", silent_turns=2)
    loop = _loop(store, tmp_path, live_sha=_Y)
    _insert_opened(
        store,
        did="d-real",
        pr=_DESIGN_PR,
        sha=_X,
        ref=_arch_ref(),
        sender="huozheclaude",
        node_id="PR_227",
    )
    loop.process_deferred_batch(limit=5)
    assert len(loop.dispatched) == 1  # type: ignore[attr-defined]
    silent_after = int(store.get_session(sk)["silent_turns"] or 0)

    _insert_opened(
        store,
        did="d-second",
        pr=_DESIGN_PR,
        sha=_Y,
        ref=_arch_ref(),
        sender="huozheclaude",
        node_id="PR_227b",
    )
    loop.process_deferred_batch(limit=5)
    assert len(loop.dispatched) == 1  # type: ignore[attr-defined]
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["state"] == "DESIGN_REVIEW"
    assert int(sess["silent_turns"] or 0) == silent_after
    store.close()


def test_synth_then_real_opened_is_one_turn(tmp_path: Path) -> None:
    """Acceptance (3), synth-first order from (a)."""
    store = Store(tmp_path / "state.db")
    sk = _seed(store, state="PLANNING")
    calls: list[str] = []

    def fetch(_repo: str, head: str) -> list[dict[str, Any]]:
        calls.append(head)
        if head.endswith(_arch_ref()):
            return [
                _gh_pr(
                    number=_DESIGN_PR,
                    ref=_arch_ref(),
                    author="huozheclaude",
                    title="RFC",
                )
            ]
        return []

    report = _sweep(store, fetch_open_prs=fetch)
    assert report["synthesized"] == 1
    loop = _loop(store, tmp_path, live_sha=_Y)
    _drain_queued(loop, store)
    _insert_opened(
        store,
        did="d-real-late",
        pr=_DESIGN_PR,
        sha=_X,
        ref=_arch_ref(),
        sender="huozheclaude",
    )
    loop.process_deferred_batch(limit=5)
    assert len(loop.dispatched) == 1  # type: ignore[attr-defined]
    assert store.get_session(sk)["design_pr"] == _DESIGN_PR
    store.close()


def test_stale_synchronize_still_dropped(tmp_path: Path) -> None:
    """Acceptance (4). ADR-37 (4′) unchanged: payload X, watermark Y, live Y."""
    store = Store(tmp_path / "state.db")
    sk = _seed(store, state="DESIGN_REVIEW", design_pr=_DESIGN_PR)
    store.update_session_fields(sk, design_pr_head=_Y)
    loop = _loop(store, tmp_path, live_sha=_Y)
    _insert_sync(
        store,
        did="d-stale-sync",
        pr=_DESIGN_PR,
        sha=_X,
        ref=_arch_ref(),
        sender="huozheclaude",
    )
    loop.process_deferred_batch(limit=5)
    assert loop.dispatched == []  # type: ignore[attr-defined]
    assert store.get_session(sk)["state"] == "DESIGN_REVIEW"
    store.close()


def test_trailing_sync_for_stamped_live_head_is_duplicate(tmp_path: Path) -> None:
    """Acceptance (5). Stamp live Y; synchronize for Y dispatches nothing."""
    store = Store(tmp_path / "state.db")
    sk = _seed(store, state="PLANNING")
    loop = _loop(store, tmp_path, live_sha=_Y)
    _insert_opened(
        store,
        did="d-open-then-sync",
        pr=_DESIGN_PR,
        sha=_X,
        ref=_arch_ref(),
        sender="huozheclaude",
    )
    loop.process_deferred_batch(limit=5)
    assert store.get_session(sk)["design_pr_head"] == _Y
    assert len(loop.dispatched) == 1  # type: ignore[attr-defined]
    _insert_sync(
        store,
        did="d-trail-y",
        pr=_DESIGN_PR,
        sha=_Y,
        ref=_arch_ref(),
        sender="huozheclaude",
    )
    loop.process_deferred_batch(limit=5)
    assert len(loop.dispatched) == 1  # type: ignore[attr-defined]
    store.close()


def test_sweep_synths_untracked_feature_pr(tmp_path: Path) -> None:
    """Acceptance (6) + (11). #232: IMPLEMENTING, feature_pr NULL, design_pr set."""
    store = Store(tmp_path / "state.db")
    sk = _seed(store, state="IMPLEMENTING", design_pr=_DESIGN_PR)
    calls: list[str] = []

    def fetch(_repo: str, head: str) -> list[dict[str, Any]]:
        calls.append(head)
        if head.endswith(_dev_ref()):
            return [_gh_pr(number=_FEAT_PR, ref=_dev_ref(), sha=_Y)]
        return []

    report = _sweep(store, fetch_open_prs=fetch)
    assert report["synthesized"] == 1
    assert store.get_session(sk)["state"] == "IMPLEMENTING"
    queued = [dict(r) for r in store.list_queued()]
    assert len(queued) == 1
    row = queued[0]
    assert row["event"] == "pull_request"
    assert row["action"] == "opened"
    assert row["delivery_id"] == f"recon:open:{_FEAT_PR}:{_Y}"
    data = json.loads(decompress_payload(row["payload"]))
    assert data["pull_request"]["head"]["ref"] == _dev_ref()
    assert data["pull_request"]["head"]["sha"] == _Y
    assert data["pull_request"]["user"]["login"] == "huozhegrok"
    assert data["sender"]["login"] == "huozhegrok"
    assert data["pull_request"]["node_id"] == f"PR_{_FEAT_PR}"
    assert any(c.endswith(_dev_ref()) for c in calls)
    assert not any(c.endswith(_arch_ref()) for c in calls)
    store.close()


def test_second_sweep_inserts_zero(tmp_path: Path) -> None:
    """Acceptance (7). INSERT OR IGNORE on recon:open:<pr>:<sha>."""
    store = Store(tmp_path / "state.db")
    _seed(store, state="IMPLEMENTING", design_pr=_DESIGN_PR)

    def fetch(_repo: str, head: str) -> list[dict[str, Any]]:
        if head.endswith(_dev_ref()):
            return [_gh_pr(number=_FEAT_PR, ref=_dev_ref())]
        return []

    assert _sweep(store, fetch_open_prs=fetch)["synthesized"] == 1
    assert _sweep(store, fetch_open_prs=fetch)["synthesized"] == 0
    assert len(store.list_queued()) == 1
    store.close()


def test_after_drain_set_diff_does_not_reopen(tmp_path: Path) -> None:
    """Acceptance (8). Node row written; delete it and the set-diff opens again."""
    store = Store(tmp_path / "state.db")
    sk = _seed(store, state="IMPLEMENTING", design_pr=_DESIGN_PR)

    def fetch(_repo: str, head: str) -> list[dict[str, Any]]:
        if head.endswith(_dev_ref()):
            return [_gh_pr(number=_FEAT_PR, ref=_dev_ref())]
        return []

    def snapshot(sess: dict[str, Any]) -> dict[str, Any]:
        if sess.get("feature_pr"):
            return _snap_with_feature_node()
        return _empty_snap()

    assert _sweep(store, fetch_open_prs=fetch, fetch_snapshot=snapshot)["synthesized"] == 1
    loop = _loop(store, tmp_path, live_sha=_Y)
    _drain_queued(loop, store)
    assert store.get_session(sk)["feature_pr"] == _FEAT_PR
    assert store.has_delivery_node(f"PR_{_FEAT_PR}")
    assert _sweep(store, fetch_open_prs=fetch, fetch_snapshot=snapshot)["synthesized"] == 0

    with store._lock:
        store._conn.execute(
            "DELETE FROM delivery_nodes WHERE node_id = ?", (f"PR_{_FEAT_PR}",)
        )
        store._conn.commit()
    report = _sweep(store, fetch_open_prs=fetch, fetch_snapshot=snapshot)
    assert report["synthesized"] == 1
    with store._lock:
        opened = list(
            store._conn.execute(
                "SELECT delivery_id FROM deliveries WHERE action = 'opened'"
            )
        )
    assert len(opened) == 2
    store.close()


def test_paused_human_issues_no_get_then_resume_synths(tmp_path: Path) -> None:
    """Acceptance (9). Half must match resume_state, not the PLANNING fallback."""
    store = Store(tmp_path / "state.db")
    sk = _seed(
        store,
        state="PAUSED_HUMAN",
        design_pr=_DESIGN_PR,
        paused_reason="owner: wait",
    )
    calls: list[str] = []

    def fetch(_repo: str, head: str) -> list[dict[str, Any]]:
        calls.append(head)
        if head.endswith(_dev_ref()):
            return [_gh_pr(number=_FEAT_PR, ref=_dev_ref())]
        if head.endswith(_arch_ref()):
            return [
                _gh_pr(
                    number=_DESIGN_PR,
                    ref=_arch_ref(),
                    author="huozheclaude",
                    title="RFC",
                )
            ]
        return []

    assert _sweep(store, fetch_open_prs=fetch)["synthesized"] == 0
    assert calls == []

    store.update_session_fields(sk, state="IMPLEMENTING", paused_reason=None)
    report = _sweep(store, fetch_open_prs=fetch)
    assert report["synthesized"] == 1
    row = dict(store.list_queued()[0])
    data = json.loads(decompress_payload(row["payload"]))
    assert data["pull_request"]["head"]["ref"] == _dev_ref()

    store2 = Store(tmp_path / "state2.db")
    sk2 = _seed(store2, issue=160, state="PAUSED_HUMAN", paused_reason="owner: wait")
    calls2: list[str] = []

    def fetch_design(_repo: str, head: str) -> list[dict[str, Any]]:
        calls2.append(head)
        if head.endswith(_arch_ref(160)):
            return [
                _gh_pr(
                    number=300,
                    ref=_arch_ref(160),
                    author="huozheclaude",
                    title="RFC",
                )
            ]
        return []

    assert _sweep(store2, fetch_open_prs=fetch_design)["synthesized"] == 0
    assert calls2 == []
    store2.update_session_fields(sk2, state="PLANNING", paused_reason=None)
    report2 = _sweep(store2, fetch_open_prs=fetch_design)
    assert report2["synthesized"] == 1
    data2 = json.loads(decompress_payload(dict(store2.list_queued()[0])["payload"]))
    assert data2["pull_request"]["head"]["ref"] == _arch_ref(160)
    store.close()
    store2.close()


def test_parse_rejects_other_session_and_merged(tmp_path: Path) -> None:
    """Acceptance (10). Parse is the rule; the query filter is a convenience."""
    store = Store(tmp_path / "state.db")
    _seed(store, state="IMPLEMENTING", design_pr=_DESIGN_PR)

    def fetch(_repo: str, head: str) -> list[dict[str, Any]]:
        if head.endswith(_dev_ref()):
            return [
                _gh_pr(number=999, ref=_dev_ref(160)),
                _gh_pr(number=998, ref=_dev_ref(), merged=True, state="closed"),
            ]
        return []

    assert _sweep(store, fetch_open_prs=fetch)["synthesized"] == 0
    store.close()


def test_tracked_half_issues_no_architect_get(tmp_path: Path) -> None:
    """Acceptance (11). Per half, not per session. Item 6's row must GET developer."""
    store = Store(tmp_path / "state.db")
    _seed(
        store,
        state="IMPLEMENTING",
        design_pr=_DESIGN_PR,
        feature_pr=_FEAT_PR,
    )
    calls: list[str] = []

    def fetch(_repo: str, head: str) -> list[dict[str, Any]]:
        calls.append(head)
        return [_gh_pr(number=1, ref=_dev_ref())]

    assert _sweep(store, fetch_open_prs=fetch)["synthesized"] == 0
    assert calls == []
    store.close()
