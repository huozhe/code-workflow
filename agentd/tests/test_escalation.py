"""M3-A: escalation round trip (§8.5) — post comment, record id, owner unpauses."""

from __future__ import annotations

import json
from pathlib import Path

from agentd.config import Config
from agentd.db import SCHEMA_VERSION, Store
from agentd.design_loop import DesignLoop
from agentd.github_write import format_escalation_comment


def _cfg(tmp_path: Path) -> Config:
    return Config(
        raw={
            "host": {"owner": "huozhe"},
            "agents": {
                "claude": {"login": "huozheclaude"},
                "grok": {"login": "huozhegrok"},
            },
        },
        root=tmp_path,
    )


def test_format_escalation_comment_tags_owner_and_states_answers() -> None:
    body = format_escalation_comment(
        owner="huozhe",
        session_key="o/r#1",
        state="DESIGN_REVIEW",
        role="system",
        reason="stall: fingerprint",
    )
    assert "@huozhe" in body
    assert "DESIGN_REVIEW" in body
    assert "stall: fingerprint" in body
    assert "Any reply" in body or "any reply" in body.lower()
    assert "PAUSED_HUMAN" in body


def test_escalate_posts_comment_and_records_id(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    now = 1_700_000_000
    store.upsert_session(
        session_key="huozhe/code-workflow#42",
        repo="huozhe/code-workflow",
        issue_num=42,
        state="DESIGN_REVIEW",
        architect="huozheclaude",
        developer="huozhegrok",
        created_at=now,
        updated_at=now,
    )
    posts: list[dict] = []

    def fake_post(*, repo, issue_num, body, token):  # noqa: ANN001
        posts.append(
            {"repo": repo, "issue_num": issue_num, "body": body, "token": token}
        )
        return 9_001

    loop = DesignLoop(
        store,
        cfg,
        supervisor=None,
        dispatch_turns=False,
        post_comment=fake_post,
        gateway_token="tok-gw",
    )
    loop._escalate("huozhe/code-workflow#42", "system", "budget: consec_agent_turns")

    sess = store.get_session("huozhe/code-workflow#42")
    assert sess is not None
    assert sess["state"] == "PAUSED_HUMAN"
    assert "budget" in str(sess["paused_reason"])
    assert sess.get("resume_state") == "DESIGN_REVIEW"

    esc = store.get_open_escalation("huozhe/code-workflow#42")
    assert esc is not None
    assert esc["comment_id"] == 9001
    assert esc["resolved_at"] is None
    assert len(posts) == 1
    assert posts[0]["repo"] == "huozhe/code-workflow"
    assert posts[0]["issue_num"] == 42
    assert "@huozhe" in posts[0]["body"]
    assert posts[0]["token"] == "tok-gw"
    store.close()


def test_owner_reply_resumes_pre_pause_state(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    now = 1_700_000_000
    sk = "huozhe/code-workflow#42"
    store.upsert_session(
        session_key=sk,
        repo="huozhe/code-workflow",
        issue_num=42,
        state="DESIGN_REVIEW",
        architect="huozheclaude",
        developer="huozhegrok",
        created_at=now,
        updated_at=now,
    )
    loop = DesignLoop(
        store,
        cfg,
        supervisor=None,
        dispatch_turns=False,
        post_comment=lambda **k: 55,  # noqa: ARG005
        gateway_token="t",
    )
    loop._escalate(sk, "architect", "needs human on API shape")

    body = json.dumps(
        {
            "action": "created",
            "issue": {"number": 42},
            "comment": {"id": 1, "body": "Use REST not GraphQL for v1."},
            "repository": {"full_name": "huozhe/code-workflow"},
            "sender": {"login": "huozhe"},
        }
    ).encode()
    store.insert_delivery(
        delivery_id="d-owner-1",
        event="issue_comment",
        action="created",
        repo="huozhe/code-workflow",
        issue_num=42,
        sender="huozhe",
        payload=body,
        status="deferred",
    )
    loop.process_deferred_batch()

    sess = store.get_session(sk)
    assert sess is not None
    assert sess["state"] == "DESIGN_REVIEW"
    assert sess.get("paused_reason") is None
    assert sess.get("resume_state") is None
    esc = store.get_open_escalation(sk)
    assert esc is None
    # escalation row closed
    with store._lock:
        row = store._conn.execute(
            "SELECT resolved_at, comment_id FROM escalations WHERE session_key=?",
            (sk,),
        ).fetchone()
    assert row is not None and row["resolved_at"] is not None
    assert row["comment_id"] == 55
    assert store.count_by_status().get("routed") == 1
    store.close()


def test_non_owner_while_paused_defers(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    now = 1_700_000_000
    sk = "huozhe/code-workflow#7"
    store.upsert_session(
        session_key=sk,
        repo="huozhe/code-workflow",
        issue_num=7,
        state="PAUSED_HUMAN",
        architect="huozheclaude",
        developer="huozhegrok",
        created_at=now,
        updated_at=now,
        paused_reason="x",
    )
    store.update_session_fields(sk, resume_state="PLANNING")
    body = json.dumps(
        {
            "action": "created",
            "issue": {"number": 7},
            "comment": {"body": "bot noise"},
            "repository": {"full_name": "huozhe/code-workflow"},
            "sender": {"login": "huozhegrok"},
        }
    ).encode()
    store.insert_delivery(
        delivery_id="d-bot",
        event="issue_comment",
        action="created",
        repo="huozhe/code-workflow",
        issue_num=7,
        sender="huozhegrok",
        payload=body,
        status="deferred",
    )
    loop = DesignLoop(store, cfg, supervisor=None, dispatch_turns=False)
    loop.process_deferred_batch()
    # still deferred (route defer)
    assert store.count_by_status().get("deferred") == 1
    assert store.get_session(sk)["state"] == "PAUSED_HUMAN"
    store.close()


def test_schema_v3_resume_state_column(tmp_path: Path) -> None:
    store = Store(tmp_path / "s.db")
    assert store._schema_version() == SCHEMA_VERSION
    cols = store._table_columns("sessions")
    assert "resume_state" in cols
    store.close()


def test_v2_to_v3_preserves_deliveries(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "state.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE deliveries (
          delivery_id TEXT PRIMARY KEY,
          event TEXT NOT NULL,
          action TEXT,
          repo TEXT NOT NULL DEFAULT '',
          issue_num INTEGER,
          sender TEXT NOT NULL DEFAULT '',
          received_at INTEGER NOT NULL,
          payload BLOB NOT NULL,
          status TEXT NOT NULL DEFAULT 'queued'
        );
        INSERT INTO deliveries(
          delivery_id, event, action, repo, issue_num, sender,
          received_at, payload, status
        ) VALUES ('keep', 'ping', null, '', null, 'u', 1, X'7B7D', 'done');
        CREATE TABLE sessions (
          session_key TEXT PRIMARY KEY,
          project_key TEXT NOT NULL DEFAULT '',
          repo TEXT NOT NULL,
          issue_num INTEGER NOT NULL,
          state TEXT NOT NULL,
          paused_reason TEXT,
          architect TEXT NOT NULL,
          developer TEXT NOT NULL,
          roles_locked INTEGER NOT NULL DEFAULT 0,
          design_pr INTEGER,
          feature_pr INTEGER,
          turn_count INTEGER NOT NULL DEFAULT 0,
          consec_agent_turns INTEGER NOT NULL DEFAULT 0,
          review_rounds INTEGER NOT NULL DEFAULT 0,
          progress_fp TEXT,
          progress_repeat INTEGER NOT NULL DEFAULT 0,
          zero_thread_rounds INTEGER NOT NULL DEFAULT 0,
          gh_watermark INTEGER,
          verified_at INTEGER,
          created_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL
        );
        CREATE TABLE runners (
          project_key TEXT PRIMARY KEY,
          container_id TEXT,
          endpoint TEXT,
          token TEXT,
          tier TEXT NOT NULL,
          last_seen_at INTEGER
        );
        CREATE TABLE circuit_breaker (
          id INTEGER PRIMARY KEY CHECK (id = 1),
          disk_paused INTEGER NOT NULL DEFAULT 0,
          reason TEXT,
          updated_at INTEGER NOT NULL
        );
        INSERT INTO circuit_breaker(id, disk_paused, reason, updated_at)
        VALUES (1, 0, NULL, 0);
        PRAGMA user_version = 2;
        """
    )
    conn.commit()
    conn.close()

    store = Store(path)
    assert store._schema_version() == 3
    assert store.delivery_count() == 1
    assert "resume_state" in store._table_columns("sessions")
    store.close()
