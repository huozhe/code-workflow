"""Dispatcher drain + intake + breaker pause."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from agentd.config import Config
from agentd.db import Store
from agentd.dispatcher import Dispatcher


def _store(tmp_path: Path) -> Store:
    return Store(tmp_path / "state.db")


def _cfg(**intake) -> Config:
    return Config(raw={"intake": intake} if intake else {"intake": {}})


def test_ping_marked_done(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.insert_delivery(
        delivery_id="p1",
        event="ping",
        action=None,
        repo="",
        issue_num=None,
        sender="u",
        payload=b'{"zen":"x"}',
    )
    d = Dispatcher(store, _cfg(), threading.Event())
    assert d.drain_once() == 1
    assert store.queue_depth() == 0
    assert store.count_by_status().get("done") == 1
    store.close()


def test_intake_drop_and_route(tmp_path: Path) -> None:
    store = _store(tmp_path)
    drop_body = json.dumps(
        {
            "action": "opened",
            "issue": {
                "number": 1,
                "author_association": "NONE",
                "labels": [{"name": "agentd"}],
            },
        }
    ).encode()
    ok_body = json.dumps(
        {
            "action": "opened",
            "issue": {
                "number": 2,
                "author_association": "OWNER",
                "labels": [{"name": "agentd"}],
            },
        }
    ).encode()
    store.insert_delivery(
        delivery_id="d1",
        event="issues",
        action="opened",
        repo="o/r",
        issue_num=1,
        sender="outsider",
        payload=drop_body,
    )
    store.insert_delivery(
        delivery_id="d2",
        event="issues",
        action="opened",
        repo="o/r",
        issue_num=2,
        sender="huozhe",
        payload=ok_body,
    )
    d = Dispatcher(store, _cfg(), threading.Event())
    d.drain_once()
    by = store.count_by_status()
    assert by.get("dropped") == 1
    assert by.get("routed") == 1
    assert store.queue_depth() == 0
    store.close()


def test_dispatcher_pauses_when_breaker_open(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.set_disk_paused(True, reason="test")
    store.insert_delivery(
        delivery_id="p-break",
        event="ping",
        action=None,
        repo="",
        issue_num=None,
        sender="u",
        payload=b"{}",
    )
    d = Dispatcher(store, _cfg(), threading.Event())
    assert d.drain_once() == 0
    assert store.queue_depth() == 1
    store.close()


def test_schema_has_sessions_table(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with store._lock:
        row = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'"
        ).fetchone()
    assert row is not None
    assert store.list_sessions() == []
    store.close()
