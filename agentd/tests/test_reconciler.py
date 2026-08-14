"""M6-1a / ADR-20: local container inventory and dry-run report."""

from __future__ import annotations

import json
from pathlib import Path

from agentd.db import Store
from agentd.reconciler import (
    CONTAINER_AGE_FLOOR_S,
    Reconciler,
    pragma_integrity_check,
)


def _sess(
    store: Store,
    *,
    issue: int,
    state: str,
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
        created_at=1,
        updated_at=1,
    )
    return sk


def _rec(
    store: Store,
    containers: list[dict],
    *,
    now: int = 1_000_000,
    removed: list | None = None,
    nudges: list | None = None,
    dry_run: bool = False,
) -> dict:
    gone: list = removed if removed is not None else []
    nlist: list = nudges if nudges is not None else []
    rec = Reconciler(
        store,
        list_containers=lambda: list(containers),
        remove_container=lambda cid: gone.append(cid),
        nudge=lambda: nlist.append(1),
        now_fn=lambda: now,
    )
    return rec.reconcile_once(dry_run=dry_run)


def test_teardown_session_is_live_not_orphan(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    pk = "huozhe/code-workflow"
    _sess(store, issue=1, state="TEARDOWN")
    store.upsert_runner(pk, container_id="abc123", endpoint="127.0.0.1:1", token="t", tier="hot")
    removed: list = []
    _rec(
        store,
        [{"id": "abc123", "project": pk, "started_at": 1}],
        now=1 + CONTAINER_AGE_FLOOR_S + 60,
        removed=removed,
    )
    assert removed == []
    assert store.get_runner(pk) is not None
    store.close()


def test_closed_only_project_is_orphan(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    pk = "huozhe/code-workflow"
    _sess(store, issue=1, state="CLOSED")
    store.upsert_runner(pk, container_id="dead1", endpoint="127.0.0.1:1", token="t", tier="hot")
    removed: list = []
    _rec(
        store,
        [{"id": "dead1", "project": pk, "started_at": 1}],
        now=1 + CONTAINER_AGE_FLOOR_S + 60,
        removed=removed,
    )
    assert removed == ["dead1"]
    store.close()


def test_age_floor_spares_orphan(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    pk = "huozhe/code-workflow"
    _sess(store, issue=1, state="CLOSED")
    removed: list = []
    report = _rec(
        store,
        [{"id": "young", "project": pk, "started_at": 900_000}],
        now=900_000 + 60,
        removed=removed,
    )
    assert removed == []
    assert any(s["reason"] == "age < 10m" for s in report["spared"])
    store.close()


def test_inflight_turn_spares_orphan(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    pk = "huozhe/code-workflow"
    sk = _sess(store, issue=1, state="CLOSED")
    store.insert_turn(
        turn_id="t-open",
        session_key=sk,
        role="developer",
        delivery_id=None,
        started_at=1,
        ended_at=None,
        status=None,
        summary=None,
    )
    removed: list = []
    report = _rec(
        store,
        [{"id": "hold", "project": pk, "started_at": 1}],
        now=1 + CONTAINER_AGE_FLOOR_S + 60,
        removed=removed,
    )
    assert removed == []
    assert any(s["reason"] == "open turn" for s in report["spared"])
    store.close()


def test_live_no_runner_row_removed(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    pk = "huozhe/code-workflow"
    _sess(store, issue=1, state="IMPLEMENTING")
    removed: list = []
    report = _rec(
        store,
        [{"id": "orphan-live", "project": pk, "started_at": 1}],
        now=1 + CONTAINER_AGE_FLOOR_S + 60,
        removed=removed,
    )
    assert removed == ["orphan-live"]
    assert any(m["reason"] == "no runners row" for m in report["missing_runners"])
    store.close()


def test_age_floor_spares_live_no_runner(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    pk = "huozhe/code-workflow"
    _sess(store, issue=1, state="IMPLEMENTING")
    removed: list = []
    _rec(
        store,
        [{"id": "new", "project": pk, "started_at": 990_000}],
        now=990_000 + 30,
        removed=removed,
    )
    assert removed == []
    store.close()


def test_stale_runner_row_cleared(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    pk = "huozhe/code-workflow"
    _sess(store, issue=1, state="IMPLEMENTING")
    store.upsert_runner(pk, container_id="gone", endpoint="127.0.0.1:1", token="t", tier="hot")
    _rec(store, [], now=1_000_000)
    assert store.get_runner(pk) is None
    store.close()


def test_healthy_left_alone(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    pk = "huozhe/code-workflow"
    _sess(store, issue=1, state="IMPLEMENTING")
    store.upsert_runner(pk, container_id="abc123def", endpoint="127.0.0.1:1", token="t", tier="hot")
    removed: list = []
    _rec(
        store,
        [{"id": "abc123def456", "project": pk, "started_at": 1}],
        now=1 + CONTAINER_AGE_FLOOR_S + 60,
        removed=removed,
    )
    assert removed == []
    assert store.get_runner(pk) is not None
    store.close()


def test_nudge_after_pass(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    nudges: list = []
    _rec(store, [], now=1, nudges=nudges)
    assert nudges == [1]
    store.close()


def test_dry_run_does_not_remove_or_clear(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    pk = "huozhe/code-workflow"
    _sess(store, issue=1, state="CLOSED")
    store.upsert_runner(pk, container_id="dead1", endpoint="127.0.0.1:1", token="t", tier="hot")
    removed: list = []
    report = _rec(
        store,
        [{"id": "dead1", "project": pk, "started_at": 1}],
        now=1 + CONTAINER_AGE_FLOOR_S + 60,
        removed=removed,
        dry_run=True,
    )
    assert removed == []
    assert store.get_runner(pk) is not None
    assert report["orphans"]
    store.close()


def test_dry_run_reports_open_turn_and_closed_live(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    pk = "huozhe/code-workflow"
    sk = _sess(store, issue=32, state="IMPLEMENTING")
    store.insert_turn(
        turn_id="t-bf292cec8404",
        session_key=sk,
        role="developer",
        delivery_id=None,
        started_at=1,
        ended_at=None,
        status=None,
        summary=None,
    )
    store.insert_delivery(
        delivery_id="32dbdbb0-9514-11f1-9c48-1b2c0e0c4789",
        event="issues",
        action="closed",
        repo=pk,
        issue_num=32,
        sender="huozheclaude",
        payload=b"{}",
        status="routed",
    )
    store.register_artifact(session_key=sk, role="developer", kind="scratch", ref="/tmp/x")
    report = _rec(store, [], now=1, dry_run=True)
    assert any(t["turn_id"] == "t-bf292cec8404" for t in report["open_turns"])
    assert any(
        r["session_key"] == sk and r["delivery_id"].startswith("32dbdbb0")
        for r in report["closed_live"]
    )
    assert report["closed_live"][0]["open_artifacts"] == 1
    store.close()


def test_pragma_integrity_ok(tmp_path: Path) -> None:
    p = tmp_path / "ok.db"
    Store(p).close()
    assert pragma_integrity_check(p) == "ok"


def test_pragma_integrity_corrupt_file(tmp_path: Path) -> None:
    p = tmp_path / "bad.db"
    p.write_bytes(b"this is not a sqlite database at all")
    result = pragma_integrity_check(p)
    assert result != "ok"
    assert "error" in result or "not a database" in result.lower() or result != "ok"


def test_inventory_error_does_not_clear_runners(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    pk = "huozhe/code-workflow"
    _sess(store, issue=1, state="IMPLEMENTING")
    store.upsert_runner(pk, container_id="abc", endpoint="127.0.0.1:1", token="t", tier="hot")

    def boom() -> list:
        raise RuntimeError("no docker")

    rec = Reconciler(store, list_containers=boom, remove_container=lambda _c: None)
    report = rec.reconcile_once()
    assert report.get("inventory_error") is True
    assert store.get_runner(pk) is not None
    store.close()


def test_agentctl_dry_run_corrupt_exits_before_store(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    import pytest

    from agentctl.__main__ import main

    p = tmp_path / "bad.db"
    p.write_bytes(b"not sqlite")

    class _Cfg:
        state_db = p

    monkeypatch.setattr("agentctl.__main__.load_config", lambda: _Cfg())
    with pytest.raises(SystemExit) as ei:
        main(["reconcile", "--dry-run"])
    assert ei.value.code == 1
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["integrity"] != "ok"
