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


def test_version_mismatch_rebuilds(tmp_path: Path, monkeypatch) -> None:
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
    # Pretend this file was written by an older schema generation.
    with store._lock:
        store._conn.execute("PRAGMA user_version = 1")
        store._conn.commit()
    store.close()

    # Force a higher SCHEMA_VERSION so open rebuilds.
    import agentd.db as dbmod

    monkeypatch.setattr(dbmod, "SCHEMA_VERSION", 2)
    store2 = dbmod.Store(path)
    assert store2._schema_version() == 2
    assert store2.delivery_count() == 0  # rebuilt empty
    store2.close()
