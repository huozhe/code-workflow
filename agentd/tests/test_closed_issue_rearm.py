"""ADR-21 / #118: reopen event clears closed_issue_escalated_at."""

from __future__ import annotations

import json
from pathlib import Path

from agentd.config import Config
from agentd.db import Store
from agentd.design_loop import CLOSE_RECONCILE_PREFIX, DesignLoop
from agentd.reconciler import Reconciler


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


def _sess(
    store: Store,
    *,
    state: str = "IMPLEMENTING",
    paused_reason: str | None = None,
) -> str:
    sk = "huozhe/code-workflow#118"
    store.upsert_session(
        session_key=sk,
        project_key="huozhe/code-workflow",
        repo="huozhe/code-workflow",
        issue_num=118,
        state=state,
        architect="huozheclaude",
        developer="huozhegrok",
        created_at=1,
        updated_at=1,
        paused_reason=paused_reason,
    )
    return sk


def _insert_reopen(store: Store, *, did: str, sender: str) -> None:
    payload = json.dumps(
        {
            "action": "reopened",
            "issue": {
                "number": 118,
                "state": "open",
                "title": "session",
                "author_association": "OWNER",
                "labels": [{"name": "agentd"}],
            },
            "repository": {"full_name": "huozhe/code-workflow"},
            "sender": {"login": sender},
        }
    ).encode()
    store.insert_delivery(
        delivery_id=did,
        event="issues",
        action="reopened",
        repo="huozhe/code-workflow",
        issue_num=118,
        sender=sender,
        payload=payload,
        status="deferred",
    )


def _loop(store: Store, tmp: Path) -> DesignLoop:
    return DesignLoop(
        store,
        _cfg(tmp),
        dispatch_turns=False,
        gateway_token="gw",
        post_comment=lambda **k: 1,
        get_issue_fn=lambda **k: {"body": "", "state": "open"},
        get_issue_body_fn=lambda **k: "",
    )


def _rec(store: Store, snap: dict, esc: list) -> Reconciler:
    def escalate(session_key: str, reason: str, **kw) -> None:
        esc.append((session_key, reason, kw))
        store.update_session_fields(
            session_key,
            state="PAUSED_HUMAN",
            paused_reason=f"{CLOSE_RECONCILE_PREFIX}{reason}",
            resume_state="IMPLEMENTING",
        )

    return Reconciler(
        store,
        list_containers=list,
        remove_container=lambda _c: None,
        fetch_snapshot=lambda _s: dict(snap),
        escalate=escalate,
    )


def test_reopen_event_clears_marker_then_later_close_escalates_again(
    tmp_path: Path,
) -> None:
    """Sweep sets the marker; reopen event clears it with no pass in between."""
    store = Store(tmp_path / "state.db")
    sk = _sess(store)
    snap_closed = {"issue_state": "closed", "issue_body": "", "nodes": []}
    esc: list = []
    rec = _rec(store, snap_closed, esc)
    rec.reconcile_once()
    assert len(esc) == 1
    assert store.get_session(sk)["closed_issue_escalated_at"]

    _insert_reopen(store, did="d-reopen", sender="huozhe")
    _loop(store, tmp_path).process_deferred_batch()
    assert store.get_session(sk)["closed_issue_escalated_at"] is None

    rec.reconcile_once()
    assert len(esc) == 2
    store.close()


def test_reopen_clears_marker_after_section_8_5_resume(tmp_path: Path) -> None:
    """Hold is already gone; marker is not. Nesting the clear in the lift fails this."""
    store = Store(tmp_path / "state.db")
    sk = _sess(store, state="IMPLEMENTING", paused_reason=None)
    store.update_session_fields(sk, closed_issue_escalated_at=1_000)
    _insert_reopen(store, did="d-reopen", sender="huozhe")
    _loop(store, tmp_path).process_deferred_batch()
    assert store.get_session(sk)["closed_issue_escalated_at"] is None
    store.close()


def test_agent_reopen_clears_marker(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _sess(store, state="IMPLEMENTING", paused_reason=None)
    store.update_session_fields(sk, closed_issue_escalated_at=1_000)
    _insert_reopen(store, did="d-agent", sender="huozhegrok")
    _loop(store, tmp_path).process_deferred_batch()
    assert store.get_session(sk)["closed_issue_escalated_at"] is None
    store.close()
