"""Session loop: deferred → session row + route without docker turns."""

from __future__ import annotations

import json
from pathlib import Path

from agentd.config import Config
from agentd.db import Store
from agentd.intake import evaluate_intake
from agentd.session_loop import SessionLoop


def test_deferred_issue_creates_planning_without_supervisor(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    cfg = Config(
        raw={
            "host": {"owner": "huozhe"},
            "agents": {
                "claude": {"login": "huozheclaude"},
                "grok": {"login": "huozhegrok"},
            },
            "intake": {"mode": "all", "actors": "collaborators"},
        },
        root=tmp_path,
    )
    body = json.dumps(
        {
            "action": "opened",
            "issue": {
                "number": 9,
                "author_association": "OWNER",
                "title": "Build the thing",
                "labels": [],
            },
            "repository": {"full_name": "huozhe/code-workflow"},
            "sender": {"login": "huozhe"},
        }
    ).encode()
    # intake would accept in mode=all
    assert evaluate_intake(
        event="issues", action="opened", payload=body, config=cfg
    ).accepted
    store.insert_delivery(
        delivery_id="d-issue-1",
        event="issues",
        action="opened",
        repo="huozhe/code-workflow",
        issue_num=9,
        sender="huozhe",
        payload=body,
        status="deferred",
    )
    loop = SessionLoop(store, cfg, supervisor=None, dispatch_turns=False)
    # without supervisor, process leaves deferred (cannot create session)
    loop.process_deferred_batch()
    assert store.count_by_status().get("deferred") == 1
    store.close()


def test_routing_self_echo_marks_done_no_turn(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    cfg = Config(
        raw={
            "host": {"owner": "huozhe"},
            "agents": {
                "claude": {"login": "huozheclaude"},
                "grok": {"login": "huozhegrok"},
            },
        },
        root=tmp_path,
    )
    now = 1_700_000_000
    store.upsert_session(
        session_key="huozhe/code-workflow#9",
        repo="huozhe/code-workflow",
        issue_num=9,
        state="PLANNING",
        architect="huozheclaude",
        developer="huozhegrok",
        created_at=now,
        updated_at=now,
    )
    # issue_opened routes to architect; sender=architect ⇒ self-echo (no turn).
    # M3-D: still terminal "done" after FSM, not stuck deferred.
    body = json.dumps(
        {
            "action": "opened",
            "issue": {
                "number": 9,
                "author_association": "OWNER",
                "title": "x",
                "labels": [{"name": "agentd"}],
            },
            "repository": {"full_name": "huozhe/code-workflow"},
            "sender": {"login": "huozheclaude"},
        }
    ).encode()
    store.insert_delivery(
        delivery_id="d-echo",
        event="issues",
        action="opened",
        repo="huozhe/code-workflow",
        issue_num=9,
        sender="huozheclaude",
        payload=body,
        status="deferred",
    )
    loop = SessionLoop(store, cfg, supervisor=None, dispatch_turns=False)
    loop.process_deferred_batch()
    assert store.count_by_status().get("done") == 1
    store.close()
