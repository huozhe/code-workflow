"""PRAGMA user_version migration path (M1-3)."""

from __future__ import annotations

from pathlib import Path

from agentd.db import SCHEMA_VERSION, Store


def test_fresh_db_stamps_user_version(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    assert store._schema_version() == SCHEMA_VERSION
    store.close()


def test_existing_version_matches_no_wipe(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    store = Store(path)
    store.insert_delivery(
        delivery_id="keep-me",
        event="ping",
        action=None,
        repo="",
        issue_num=None,
        sender="u",
        payload=b"{}",
    )
    store.close()

    store2 = Store(path)
    assert store2.delivery_count() == 1
    assert store2._schema_version() == SCHEMA_VERSION
    store2.close()


def test_v1_to_v2_preserves_deliveries(tmp_path: Path) -> None:
    """#21 R2: additive migration must keep the idempotency ledger."""
    import sqlite3

    path = tmp_path / "state.db"
    # Build a v1-shaped DB by hand (runners.session_key, no sessions.project_key).
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
        CREATE TABLE circuit_breaker (
          id INTEGER PRIMARY KEY CHECK (id = 1),
          disk_paused INTEGER NOT NULL DEFAULT 0,
          reason TEXT,
          updated_at INTEGER NOT NULL
        );
        INSERT INTO circuit_breaker(id, disk_paused, reason, updated_at)
        VALUES (1, 0, NULL, 0);
        CREATE TABLE sessions (
          session_key TEXT PRIMARY KEY,
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
          session_key TEXT PRIMARY KEY,
          container_id TEXT,
          endpoint TEXT,
          token TEXT,
          tier TEXT NOT NULL,
          last_seen_at INTEGER
        );
        INSERT INTO deliveries(
          delivery_id, event, action, repo, issue_num, sender,
          received_at, payload, status
        ) VALUES ('keep-me', 'issues', 'opened', 'o/r', 1, 'u', 1, X'7B7D', 'done');
        INSERT INTO sessions(
          session_key, repo, issue_num, state, architect, developer,
          created_at, updated_at
        ) VALUES ('o/r#1', 'o/r', 1, 'PLANNING', 'a', 'd', 1, 1);
        INSERT INTO runners(
          session_key, container_id, endpoint, token, tier, last_seen_at
        ) VALUES ('o/r#1', 'cid', '127.0.0.1:1', 'tok', 'hot', 1);
        PRAGMA user_version = 1;
        """
    )
    conn.commit()
    conn.close()

    store = Store(path)
    assert store._schema_version() == SCHEMA_VERSION
    assert store.delivery_count() == 1
    assert store.count_by_status().get("done") == 1
    sess = store.get_session("o/r#1")
    assert sess is not None
    assert sess["project_key"] == "o/r"
    runner = store.get_runner("o/r")
    assert runner is not None
    assert runner["container_id"] == "cid"
    store.close()


def test_unknown_version_gap_rebuilds_with_honest_log(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    path = tmp_path / "state.db"
    store = Store(path)
    store.insert_delivery(
        delivery_id="gone",
        event="ping",
        action=None,
        repo="",
        issue_num=None,
        sender="u",
        payload=b"{}",
    )
    with store._lock:
        # Stuck at current version with no path to a future SCHEMA_VERSION → rebuild.
        store._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        store._conn.commit()
    store.close()

    import agentd.db as dbmod

    monkeypatch.setattr(dbmod, "SCHEMA_VERSION", 99)
    with caplog.at_level("WARNING"):
        store2 = dbmod.Store(path)
    assert store2._schema_version() == 99
    assert store2.delivery_count() == 0
    assert any("Reconciler is not implemented" in r.message for r in caplog.records)
    store2.close()


def test_v7_to_v8_preserves_deliveries_and_backfills_nodes(tmp_path: Path) -> None:
    """ADR-21: v8 adds delivery_nodes and backfills; must not wipe the ledger."""
    import json
    import sqlite3

    from agentd.db import compress_payload

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
        CREATE TABLE circuit_breaker (
          id INTEGER PRIMARY KEY CHECK (id = 1),
          disk_paused INTEGER NOT NULL DEFAULT 0,
          reason TEXT,
          updated_at INTEGER NOT NULL
        );
        INSERT INTO circuit_breaker(id, disk_paused, reason, updated_at)
        VALUES (1, 0, NULL, 0);
        PRAGMA user_version = 7;
        """
    )
    payload = json.dumps(
        {
            "action": "created",
            "issue": {"node_id": "I_kw_issue32", "number": 32},
            "comment": {"node_id": "IC_kw_comment1", "user": {"login": "huozhe"}},
            "sender": {"login": "huozhe"},
        }
    ).encode()
    conn.execute(
        """
        INSERT INTO deliveries(
          delivery_id, event, action, repo, issue_num, sender,
          received_at, payload, status
        ) VALUES (?, 'issue_comment', 'created', 'huozhe/code-workflow', 32,
                  'huozhe', 100, ?, 'done')
        """,
        ("keep-ledger", compress_payload(payload)),
    )
    conn.commit()
    conn.close()

    store = Store(path)
    assert store._schema_version() == SCHEMA_VERSION
    assert store.delivery_count() == 1
    assert store.has_delivery_node("IC_kw_comment1")
    assert store.has_delivery_node("I_kw_issue32")
    store.close()


def test_insert_delivery_indexes_node_ids(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    store.insert_delivery(
        delivery_id="d1",
        event="issue_comment",
        action="created",
        repo="huozhe/code-workflow",
        issue_num=1,
        sender="huozhe",
        payload=b'{"comment":{"node_id":"IC_new"},"issue":{"node_id":"I_new"}}',
    )
    assert store.has_delivery_node("IC_new")
    assert store.has_delivery_node("I_new")
    store.close()
