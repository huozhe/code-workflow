"""M6-1b / ADR-21: node-ID set-diff, forward adopt, closed-live escalate."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from agentd.db import Store, decompress_payload
from agentd.design_loop import CLOSE_RECONCILE_PREFIX
from agentd.reconciler import Reconciler


def _sess(
    store: Store,
    *,
    issue: int = 32,
    state: str = "IMPLEMENTING",
    created_at: int = 100,
    paused_reason: str | None = None,
    project: str = "huozhe/code-workflow",
) -> str:
    sk = f"{project}#{issue}"
    store.upsert_session(
        session_key=sk,
        project_key=project,
        repo=project,
        issue_num=issue,
        state=state,
        architect="huozheclaude",
        developer="huozhegrok",
        created_at=created_at,
        updated_at=created_at,
        paused_reason=paused_reason,
    )
    return sk


def _sweep_rec(
    store: Store,
    snapshot: dict,
    *,
    dry_run: bool = False,
    escalations: list | None = None,
) -> tuple[Reconciler, dict, list]:
    esc: list = escalations if escalations is not None else []

    def fetch(sess: dict) -> dict:
        return dict(snapshot)

    rec = Reconciler(
        store,
        list_containers=list,
        remove_container=lambda _c: None,
        fetch_snapshot=fetch,
        escalate=lambda sk, reason, **kw: esc.append((sk, reason, kw)),
    )
    return rec, rec.reconcile_once(dry_run=dry_run), esc


def test_backfill_makes_first_sweep_synthesize_zero(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    _sess(store)
    store.insert_delivery(
        delivery_id="already",
        event="issue_comment",
        action="created",
        repo="huozhe/code-workflow",
        issue_num=32,
        sender="huozhe",
        payload=b'{"comment":{"node_id":"IC_known"},"issue":{"node_id":"I_32"}}',
        status="done",
    )
    snap = {
        "issue_state": "open",
        "issue_body": "",
        "nodes": [
            {"id": "IC_known", "kind": "comment", "created_at": 200, "author": "huozhe", "body": "hi"},
        ],
    }
    _, report, _ = _sweep_rec(store, snap)
    assert report["synthesized"] == 0
    assert not any(
        str(d["delivery_id"]).startswith("recon:")
        for d in store.list_queued()
    )
    store.close()


def test_new_comments_synthesize_once(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    _sess(store)
    snap = {
        "issue_state": "open",
        "issue_body": "",
        "issue_title": "M6-1",
        "issue_html_url": "https://github.com/huozhe/code-workflow/issues/32",
        "issue_labels": [{"name": "agentd"}],
        "nodes": [
            {
                "id": "IC_a",
                "kind": "comment",
                "created_at": 200,
                "author": "alice",
                "body": "one",
            },
            {
                "id": "IC_b",
                "kind": "comment",
                "created_at": 201,
                "author": "bob",
                "body": "two",
            },
        ],
    }
    _, report, _ = _sweep_rec(store, snap)
    assert report["synthesized"] == 2
    queued = [dict(r) for r in store.list_queued()]
    ids = {r["delivery_id"] for r in queued}
    assert ids == {"recon:IC_a", "recon:IC_b"}
    a = next(r for r in queued if r["delivery_id"] == "recon:IC_a")
    assert a["event"] == "issue_comment"
    assert a["action"] == "created"
    assert a["sender"] == "alice"
    payload = json.loads(decompress_payload(a["payload"]))
    assert payload["comment"]["node_id"] == "IC_a"
    assert payload["sender"]["login"] == "alice"
    assert payload["issue"]["title"] == "M6-1"
    assert payload["issue"]["html_url"].endswith("/issues/32")
    assert payload["issue"]["labels"] == [{"name": "agentd"}]
    assert store.has_delivery_node("IC_a")
    _, report2, _ = _sweep_rec(store, snap)
    assert report2["synthesized"] == 0
    assert len(store.list_queued()) == 2
    store.close()


def test_created_at_bound_skips_pre_session_nodes(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    _sess(store, created_at=500)
    snap = {
        "issue_state": "open",
        "issue_body": "",
        "nodes": [
            {"id": "IC_old", "kind": "comment", "created_at": 100, "author": "x", "body": "old"},
            {"id": "IC_new", "kind": "comment", "created_at": 600, "author": "y", "body": "new"},
        ],
    }
    _, report, _ = _sweep_rec(store, snap)
    assert report["synthesized"] == 1
    assert store.has_delivery_node("IC_new")
    assert not store.has_delivery_node("IC_old")
    store.close()


def test_never_synthesize_issue_edited_or_closed(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    _sess(store)
    snap = {
        "issue_state": "closed",
        "issue_body": "",
        "nodes": [
            {"id": "I_32", "kind": "issue", "created_at": 200, "author": "huozheclaude", "body": ""},
        ],
    }
    _, report, _ = _sweep_rec(store, snap)
    assert report["synthesized"] == 0
    queued = [dict(r) for r in store.list_queued()]
    assert not any(r["event"] == "issues" for r in queued)
    store.close()


def test_never_writes_verified_at(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _sess(store)
    snap = {
        "issue_state": "open",
        "issue_body": "- [x] Human Verification Complete",
        "nodes": [],
    }
    _sweep_rec(store, snap)
    sess = store.get_session(sk)
    assert sess is not None
    assert sess.get("verified_at") in (None, 0)
    store.close()


def test_closed_issue_escalates_leaves_session_live(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _sess(store, state="IMPLEMENTING")
    snap = {"issue_state": "closed", "issue_body": "", "nodes": []}
    _, report, esc = _sweep_rec(store, snap)
    assert report["escalated"] == 1
    assert len(esc) == 1
    assert esc[0][0] == sk
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["state"] == "IMPLEMENTING"
    assert sess.get("classification") in (None, "")
    store.close()


def test_closed_issue_escalates_only_once(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _sess(store, state="IMPLEMENTING")
    snap = {"issue_state": "closed", "issue_body": "", "nodes": []}
    esc: list = []

    def escalate(session_key: str, reason: str, **kw) -> None:
        esc.append((session_key, reason, kw))
        store.update_session_fields(
            session_key,
            state="PAUSED_HUMAN",
            paused_reason=f"{CLOSE_RECONCILE_PREFIX}{reason}",
            resume_state="IMPLEMENTING",
        )

    rec = Reconciler(
        store,
        list_containers=list,
        remove_container=lambda _c: None,
        fetch_snapshot=lambda _s: dict(snap),
        escalate=escalate,
    )
    rec.reconcile_once()
    rec.reconcile_once()
    rec.reconcile_once()
    assert len(esc) == 1
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["state"] == "PAUSED_HUMAN"
    store.close()


def test_closed_issue_escalate_survives_resume(tmp_path: Path) -> None:
    """§8.5 resume clears paused_reason; the closed-episode flag must stay."""
    store = Store(tmp_path / "state.db")
    sk = _sess(store, state="IMPLEMENTING")
    snap_closed = {"issue_state": "closed", "issue_body": "", "nodes": []}
    esc: list = []

    def escalate(session_key: str, reason: str, **kw) -> None:
        esc.append((session_key, reason, kw))
        store.update_session_fields(
            session_key,
            state="PAUSED_HUMAN",
            paused_reason=f"{CLOSE_RECONCILE_PREFIX}{reason}",
            resume_state="IMPLEMENTING",
        )

    rec = Reconciler(
        store,
        list_containers=list,
        remove_container=lambda _c: None,
        fetch_snapshot=lambda _s: dict(snap_closed),
        escalate=escalate,
    )
    rec.reconcile_once()
    assert len(esc) == 1
    # Owner reply: §8.5 clears the pause; issue stays closed.
    store.update_session_fields(
        sk, state="IMPLEMENTING", paused_reason=None, resume_state=None
    )
    rec.reconcile_once()
    rec.reconcile_once()
    assert len(esc) == 1
    # Reopen re-arms; a later close escalates once more.
    rec.fetch_snapshot = lambda _s: {"issue_state": "open", "issue_body": "", "nodes": []}
    rec.reconcile_once()
    rec.fetch_snapshot = lambda _s: dict(snap_closed)
    rec.reconcile_once()
    assert len(esc) == 2
    store.close()


def test_merged_feature_pr_adopts_forward(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _sess(store, state="MERGING")
    store.update_session_fields(sk, feature_pr=111)
    snap = {
        "issue_state": "open",
        "issue_body": "",
        "nodes": [],
        "feature_merged": True,
    }
    _, report, _ = _sweep_rec(store, snap)
    assert report["adopted"] == 1
    assert store.get_session(sk)["state"] == "AWAITING_VERIFICATION"
    store.close()


def test_close_reconcile_hold_lifts_when_issue_open(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _sess(
        store,
        state="PAUSED_HUMAN",
        paused_reason=f"{CLOSE_RECONCILE_PREFIX}ticked body, no verified_at",
    )
    snap = {"issue_state": "open", "issue_body": "", "nodes": []}
    _, report, esc = _sweep_rec(store, snap)
    assert report.get("holds_lifted", 0) == 1
    assert esc == []
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["state"] == "PAUSED_HUMAN"
    assert not str(sess.get("paused_reason") or "").startswith(CLOSE_RECONCILE_PREFIX)
    store.close()


def test_synthesis_cap_50_warns(tmp_path: Path, caplog) -> None:
    store = Store(tmp_path / "state.db")
    _sess(store)
    nodes = [
        {"id": f"IC_{i}", "kind": "comment", "created_at": 200 + i, "author": "a", "body": "x"}
        for i in range(60)
    ]
    snap = {"issue_state": "open", "issue_body": "", "nodes": nodes}
    with caplog.at_level(logging.WARNING, logger="agentd.reconciler"):
        _, report, _ = _sweep_rec(store, snap)
    assert report["synthesized"] == 50
    assert report["capped"] == 10
    assert any("cap" in r.getMessage().lower() for r in caplog.records)
    store.close()


def test_dry_run_sweep_does_not_write(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _sess(store, state="MERGING")
    store.update_session_fields(sk, feature_pr=1)
    snap = {
        "issue_state": "closed",
        "issue_body": "",
        "nodes": [
            {"id": "IC_x", "kind": "comment", "created_at": 200, "author": "a", "body": "z"},
        ],
        "feature_merged": True,
    }
    _, report, esc = _sweep_rec(store, snap, dry_run=True)
    assert report["synthesized"] == 1
    assert report["escalated"] == 1
    assert report["adopted"] == 1
    assert esc == []
    assert store.list_queued() == []
    assert not store.has_delivery_node("IC_x")
    assert store.get_session(sk)["state"] == "MERGING"
    store.close()


def test_fetch_snapshot_includes_reviews() -> None:
    from agentd.github_fetch import fetch_session_snapshot

    def fake_get(url: str, *, token: str):
        if url.endswith("/issues/32") and "/comments" not in url:
            return {
                "state": "open",
                "node_id": "I_1",
                "body": "",
                "title": "t",
                "html_url": "https://example/issues/32",
                "labels": [{"name": "agentd"}],
            }
        if "/issues/32/comments" in url:
            return []
        if url.endswith("/pulls/111"):
            return {
                "node_id": "PR_1",
                "merged": False,
                "title": "feat: x",
                "user": {"login": "dev"},
                "created_at": "2026-08-14T00:00:00Z",
                "head": {"ref": "agentd/huozhe__code-workflow/32/developer"},
            }
        if "/pulls/111/reviews" in url:
            return [
                {
                    "node_id": "PRR_1",
                    "state": "APPROVED",
                    "user": {"login": "arch"},
                    "submitted_at": "2026-08-14T01:00:00Z",
                },
                {
                    "node_id": "PRR_pending",
                    "state": "PENDING",
                    "user": {"login": "arch"},
                    "submitted_at": None,
                },
            ]
        raise AssertionError(url)

    snap = fetch_session_snapshot(
        repo="huozhe/code-workflow",
        issue_num=32,
        feature_pr=111,
        design_pr=None,
        token="t",
        http_get=fake_get,
    )
    assert snap is not None
    kinds = [n["kind"] for n in snap["nodes"]]
    assert "review" in kinds
    assert "pull_request" in kinds
    review = next(n for n in snap["nodes"] if n["kind"] == "review")
    assert review["id"] == "PRR_1"
    assert review["state"] == "APPROVED"
    assert review["number"] == 111
    assert review.get("head_ref")
    assert not any(n["id"] == "PRR_pending" for n in snap["nodes"])


def test_synthesized_review_classifies_as_feature_approved(tmp_path: Path) -> None:
    from agentd.config import Config
    from agentd.design_loop import DesignLoop
    from agentd.gitops import role_branch_name

    store = Store(tmp_path / "state.db")
    sk = _sess(store, issue=32, state="CODE_REVIEW")
    store.update_session_fields(sk, feature_pr=111)
    head = role_branch_name("huozhe/code-workflow", 32, "developer")
    snap = {
        "issue_state": "open",
        "issue_body": "",
        "nodes": [
            {
                "id": "PRR_appr",
                "kind": "review",
                "created_at": 200,
                "author": "huozheclaude",
                "state": "APPROVED",
                "number": 111,
                "head_ref": head,
                "title": "feat: x",
            }
        ],
    }
    _sweep_rec(store, snap)
    queued = [dict(r) for r in store.list_queued()]
    row = next(r for r in queued if r["delivery_id"] == "recon:PRR_appr")
    payload = json.loads(decompress_payload(row["payload"]))
    loop = DesignLoop(store, Config(), dispatch_turns=False, gateway_token="")
    kind = loop._event_kind(
        row["event"],
        row["action"],
        payload,
        str(row["sender"]),
        repo="huozhe/code-workflow",
    )
    assert kind == "feature_approved_unverified"
    store.close()
