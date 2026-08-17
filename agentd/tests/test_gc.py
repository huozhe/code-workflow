"""M6-2 / ADR-23: GC reports leaks and deletes only the unambiguous."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from agentd.config import Config
from agentd.db import Store
from agentd.gc import ARTIFACT_AGE_FLOOR_S, GarbageCollector
from agentd.gitops import project_dir_name, project_path


def _cfg(tmp: Path, *, archive_days: int = 30) -> Config:
    return Config(raw={"retention": {"archive_days": archive_days}}, root=tmp)


def _sess(
    store: Store,
    *,
    issue: int = 32,
    state: str = "CLOSED",
    project: str = "huozhe/code-workflow",
    created_at: int = 1,
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
    )
    return sk


def _gc(
    store: Store,
    tmp: Path,
    *,
    now: int,
    containers: list | None = None,
    archive_days: int = 30,
    supervisor=None,
) -> GarbageCollector:
    return GarbageCollector(
        store,
        _cfg(tmp, archive_days=archive_days),
        supervisor=supervisor,
        list_containers=lambda: list(containers or []),
        list_images=lambda: [],
        remove_container=lambda _c: None,
        prune_image=lambda _i: None,
        git_gc=lambda _p: None,
        now_fn=lambda: now,
    )


def test_unmanaged_containers_absent_from_report(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    mixed = [
        {
            "id": "aaa",
            "name": "agentd-huozhe-code-workflow",
            "labels": {"agentd.managed": "true"},
            "running": False,
        },
        {
            "id": "f1",
            "name": "finanalysis-web-prod",
            "labels": {},
            "running": True,
        },
        {
            "id": "f2",
            "name": "finanalysis-app-prod",
            "labels": {},
            "running": True,
        },
        {
            "id": "f3",
            "name": "finanalysis-redis-prod",
            "labels": {},
            "running": True,
        },
    ]
    report = _gc(store, tmp_path, now=10_000, containers=mixed).collect_once(
        dry_run=True
    )
    names = [c["name"] for c in report["containers"]]
    assert "finanalysis-web-prod" not in names
    assert "finanalysis-app-prod" not in names
    assert "finanalysis-redis-prod" not in names
    assert "agentd-huozhe-code-workflow" in names
    store.close()


def test_gc_does_not_truncate_payloads(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    blob = json.dumps({"comment": {"body": "keep me"}}).encode()
    store.insert_delivery(
        delivery_id="old",
        event="issue_comment",
        action="created",
        repo="huozhe/code-workflow",
        issue_num=32,
        sender="huozhe",
        payload=blob,
        status="done",
    )
    store._conn.execute(
        "UPDATE deliveries SET received_at = 1 WHERE delivery_id = 'old'"
    )
    store._conn.commit()
    _gc(store, tmp_path, now=10_000_000).collect_once()
    row = store._conn.execute(
        "SELECT payload FROM deliveries WHERE delivery_id = 'old'"
    ).fetchone()
    assert row is not None
    from agentd.db import decompress_payload

    assert decompress_payload(row["payload"]) == blob
    store.close()


def test_orphan_session_dir_is_reported_not_deleted(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    _sess(store, issue=32, state="CLOSED")
    leaked = (
        project_path(tmp_path, "huozhe/code-workflow")
        / "sessions"
        / "32"
        / "developer"
        / "context"
    )
    leaked.mkdir(parents=True)
    (leaked / "digest-t-open.md").write_text("x", encoding="utf-8")
    old = time.time() - ARTIFACT_AGE_FLOOR_S - 10
    sess_dir = leaked.parents[1]  # sessions/<issue>
    import os

    os.utime(sess_dir, (old, old))
    now = int(time.time())
    report = _gc(store, tmp_path, now=now).collect_once()
    assert leaked.is_dir()
    refs = [o["ref"] for o in report["orphans"]]
    assert any("sessions/32" in r for r in refs)
    store.close()


def test_archive_residue_without_tarball_is_reported(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    residue = tmp_path / "archive" / "pr26-demo-residue-20260810162145" / "work"
    residue.mkdir(parents=True)
    (residue / "demo-tmp").write_text("x", encoding="utf-8")
    report = _gc(store, tmp_path, now=10_000).collect_once()
    assert residue.parent.is_dir()
    refs = [r["ref"] for r in report["residue"]]
    assert any("pr26-demo-residue-20260810162145" in x for x in refs)
    store.close()


def test_young_session_dir_is_not_an_orphan(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    _sess(store, issue=32, state="CLOSED")
    leaked = (
        project_path(tmp_path, "huozhe/code-workflow") / "sessions" / "32" / "developer"
    )
    leaked.mkdir(parents=True)
    now = int(time.time())
    report = _gc(store, tmp_path, now=now).collect_once()
    assert leaked.is_dir()
    assert report["orphans"] == []
    store.close()


def test_expired_archive_deleted_tmp_deleted_dry_run_keeps(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    proj = tmp_path / "archive" / project_dir_name("huozhe/code-workflow")
    proj.mkdir(parents=True)
    old_tar = proj / "47.tar.gz"
    old_tar.write_bytes(b"old")
    tmp = proj / "49.tar.gz.tmp"
    tmp.write_bytes(b"tmp")
    fresh_tar = proj / "32.tar.gz"
    fresh_tar.write_bytes(b"new")
    import os

    now = int(time.time())
    os.utime(old_tar, (now - 40 * 86400, now - 40 * 86400))
    os.utime(tmp, (now - 7200, now - 7200))
    os.utime(fresh_tar, (now - 100, now - 100))
    dry = _gc(store, tmp_path, now=now, archive_days=30).collect_once(dry_run=True)
    assert old_tar.is_file() and tmp.is_file() and fresh_tar.is_file()
    assert dry["archives_expired"] and dry["tmps_expired"]
    _gc(store, tmp_path, now=now, archive_days=30).collect_once()
    assert not old_tar.exists()
    assert not tmp.exists()
    assert fresh_tar.is_file()
    store.close()


def test_gc_pass_logs_counts(tmp_path: Path, caplog) -> None:
    store = Store(tmp_path / "state.db")
    with caplog.at_level(logging.INFO, logger="agentd.gc"):
        _gc(store, tmp_path, now=10_000).collect_once(dry_run=True)
    assert any("gc pass" in r.message for r in caplog.records)
    store.close()


def test_reconciler_does_not_run_gc() -> None:
    import inspect

    from agentd.reconciler import Reconciler

    src = inspect.getsource(Reconciler.reconcile_once)
    assert "collect_once" not in src
    assert "GarbageCollector" not in src
