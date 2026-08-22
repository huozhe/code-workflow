"""M6-2 / ADR-23: GC reports leaks and deletes only the unambiguous."""

from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path

from agentd.config import Config
from agentd.db import Store
from agentd.gc import ARTIFACT_AGE_FLOOR_S, GarbageCollector
from agentd.gitops import (
    local_branch_gone,
    project_dir_name,
    project_path,
    shared_clone_path,
)


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


def test_git_worktree_list_reads_porcelain(tmp_path: Path) -> None:
    import subprocess

    from agentd.gc import _git_worktree_list

    clone = tmp_path / "repo"
    clone.mkdir()
    extra = tmp_path / "extra-wt"
    subprocess.run(["git", "init", str(clone)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(clone), "config", "user.email", "t@t"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(clone), "config", "user.name", "t"],
        check=True,
        capture_output=True,
    )
    (clone / "f").write_text("x", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(clone), "add", "f"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(clone), "commit", "-m", "i"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(clone), "worktree", "add", str(extra), "-b", "b"],
        check=True,
        capture_output=True,
    )
    trees = {p.resolve() for p in _git_worktree_list(clone)}
    assert clone.resolve() in trees
    assert extra.resolve() in trees


def test_worktree_on_live_session_without_ledger_is_reported(tmp_path: Path) -> None:
    """Crash between worktree add and register — live session, no ledger row."""
    import os

    store = Store(tmp_path / "state.db")
    _sess(store, issue=77, state="IMPLEMENTING")
    wt = (
        project_path(tmp_path, "huozhe/code-workflow")
        / "sessions"
        / "77"
        / "developer"
        / "worktrees"
        / "issue-77"
    )
    wt.mkdir(parents=True)
    old = time.time() - ARTIFACT_AGE_FLOOR_S - 10
    os.utime(wt, (old, old))
    clone = project_path(tmp_path, "huozhe/code-workflow") / "repo"
    clone.mkdir(parents=True)
    now = int(time.time())
    gc = GarbageCollector(
        store,
        _cfg(tmp_path),
        list_containers=list,
        list_worktrees=lambda p: [wt] if p == clone else [],
        git_gc=lambda _p: None,
        now_fn=lambda: now,
    )
    report = gc.collect_once()
    assert wt.is_dir()
    refs = [o["ref"] for o in report["orphans"]]
    assert any(str(wt) == r or "issue-77" in r for r in refs)
    store.close()


def test_young_worktree_without_ledger_is_not_an_orphan(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    _sess(store, issue=77, state="IMPLEMENTING")
    wt = (
        project_path(tmp_path, "huozhe/code-workflow")
        / "sessions"
        / "77"
        / "developer"
        / "worktrees"
        / "issue-77"
    )
    wt.mkdir(parents=True)
    clone = project_path(tmp_path, "huozhe/code-workflow") / "repo"
    clone.mkdir(parents=True)
    now = int(time.time())
    gc = GarbageCollector(
        store,
        _cfg(tmp_path),
        list_containers=list,
        list_worktrees=lambda p: [wt] if p == clone else [],
        git_gc=lambda _p: None,
        now_fn=lambda: now,
    )
    report = gc.collect_once()
    refs = [o["ref"] for o in report["orphans"]]
    assert not any("issue-77" in r for r in refs)
    store.close()


def test_aged_shared_clone_is_not_an_orphan_worktree(tmp_path: Path) -> None:
    """git lists the main clone first. Age it or the floor hides a missing skip."""
    import os

    store = Store(tmp_path / "state.db")
    _sess(store, issue=77, state="IMPLEMENTING")
    clone = project_path(tmp_path, "huozhe/code-workflow") / "repo"
    clone.mkdir(parents=True)
    old = time.time() - ARTIFACT_AGE_FLOOR_S - 10
    os.utime(clone, (old, old))
    now = int(time.time())
    gc = GarbageCollector(
        store,
        _cfg(tmp_path),
        list_containers=list,
        list_worktrees=lambda p: [clone] if p == clone else [],
        git_gc=lambda _p: None,
        now_fn=lambda: now,
    )
    report = gc.collect_once()
    refs = [o["ref"] for o in report["orphans"]]
    assert str(clone) not in refs
    assert str(clone.resolve()) not in refs
    store.close()


def test_registered_worktree_matches_git_resolved_path(tmp_path: Path) -> None:
    """Ledger stores the raw ref; porcelain returns the resolved path."""
    import os

    store = Store(tmp_path / "state.db")
    sk = _sess(store, issue=77, state="IMPLEMENTING")
    actual = (
        project_path(tmp_path, "huozhe/code-workflow")
        / "sessions"
        / "77"
        / "developer"
        / "worktrees"
        / "issue-77"
    )
    actual.mkdir(parents=True)
    raw = tmp_path / "wt-link"
    raw.symlink_to(actual)
    old = time.time() - ARTIFACT_AGE_FLOOR_S - 10
    os.utime(actual, (old, old))
    store.register_artifact(
        session_key=sk, role="developer", kind="worktree", ref=str(raw)
    )
    clone = project_path(tmp_path, "huozhe/code-workflow") / "repo"
    clone.mkdir(parents=True)
    now = int(time.time())
    gc = GarbageCollector(
        store,
        _cfg(tmp_path),
        list_containers=list,
        list_worktrees=lambda p: [raw.resolve()] if p == clone else [],
        git_gc=lambda _p: None,
        now_fn=lambda: now,
    )
    report = gc.collect_once()
    wts = [o["ref"] for o in report["orphans"] if o.get("kind") == "worktree"]
    assert wts == []
    store.close()


def test_gc_does_not_remove_cold_container_for_live_session(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    _sess(store, issue=77, state="IMPLEMENTING")
    store.upsert_runner(
        "huozhe/code-workflow",
        container_id="cold123",
        endpoint="127.0.0.1:9",
        token="t",
        tier="cold",
    )
    containers = [
        {
            "id": "cold123",
            "name": "agentd-huozhe-code-workflow",
            "labels": {"agentd.managed": "true"},
            "running": False,
        }
    ]
    gc = GarbageCollector(
        store,
        _cfg(tmp_path),
        list_containers=lambda: containers,
        git_gc=lambda _p: None,
        now_fn=lambda: 10_000,
    )
    report = gc.collect_once()
    names = [c["name"] for c in report["containers"]]
    assert "agentd-huozhe-code-workflow" in names
    src = __import__("inspect").getsource(GarbageCollector._sweep_containers)
    assert "remove_container" not in src
    assert "docker" not in src
    store.close()


def test_git_gc_skipped_without_supervisor(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    _sess(store, issue=32, state="CLOSED")
    clone = project_path(tmp_path, "huozhe/code-workflow") / "repo"
    clone.mkdir(parents=True)
    (clone / ".git").mkdir()
    called: list[Path] = []
    gc = GarbageCollector(
        store,
        _cfg(tmp_path),
        supervisor=None,
        list_containers=list,
        git_gc=lambda p: called.append(p),
        now_fn=lambda: 10_000,
    )
    report = gc.collect_once()
    assert called == []
    assert report["git_gc"]
    assert all(g.get("skipped") for g in report["git_gc"])
    store.close()


def test_reconciler_does_not_run_gc() -> None:
    import inspect

    from agentd.reconciler import Reconciler

    src = inspect.getsource(Reconciler.reconcile_once)
    assert "collect_once" not in src
    assert "GarbageCollector" not in src


# --- #167: branch rows are reclaimable, and not by Path.exists() -------------


def _clone_with_branches(tmp: Path, *branches: str, project: str) -> Path:
    """A real shared clone, because `local_branch_gone` shells out to git.

    A fake directory would make `local_branch_gone` return False for every ref —
    the safe answer — so every one of these tests would pass for the wrong
    reason. The fixture is asserted in `test_branch_fixture_reproduces_the_property`.
    """
    clone = shared_clone_path(tmp, project)
    clone.mkdir(parents=True, exist_ok=True)

    def run(*a: str) -> None:
        subprocess.run(a, cwd=clone, check=True, capture_output=True)
    run("git", "init", "-q")
    run("git", "config", "user.email", "t@t")
    run("git", "config", "user.name", "t")
    (clone / "f").write_text("x\n", encoding="utf-8")
    run("git", "add", ".")
    run("git", "commit", "-qm", "i")
    for b in branches:
        run("git", "branch", b)
    return clone


_ARCH = "agentd/huozhe__code-workflow/32/architect"
_DEV = "agentd/huozhe__code-workflow/32/developer"


def test_branch_fixture_reproduces_the_property(tmp_path: Path) -> None:
    """Fixture check first: the clone must actually distinguish the two cases.

    `local_branch_gone` returns False for a missing or non-git clone, so a bad
    fixture is indistinguishable from "the branch is still there" — which is the
    answer every other test here expects in its negative case.
    """
    clone = _clone_with_branches(tmp_path, _ARCH, project="huozhe/code-workflow")
    assert local_branch_gone(clone, _DEV) is True
    assert local_branch_gone(clone, _ARCH) is False


def test_stale_branch_row_is_swept(tmp_path: Path) -> None:
    """Exit condition, positive half: branch absent from the clone → removed."""
    store = Store(tmp_path / "state.db")
    sk = _sess(store)
    _clone_with_branches(tmp_path, _ARCH, project="huozhe/code-workflow")
    now = 10_000_000
    store.register_artifact(
        session_key=sk,
        role="developer",
        kind="branch",
        ref=_DEV,
        created_at=now - ARTIFACT_AGE_FLOOR_S - 1,
    )
    report = _gc(store, tmp_path, now=now).collect_once()

    assert {"ref": _DEV, "kind": "branch"} in report["ledger_stale"]
    assert store.list_artifacts(sk, open_only=True) == []
    store.close()


def test_live_branch_row_is_left_open(tmp_path: Path) -> None:
    """The assertion that matters.

    Widening the kind tuple alone passes the positive test above while clearing
    every live branch row too, because `Path(<branch name>).exists()` is False
    for a branch that is present.
    """
    store = Store(tmp_path / "state.db")
    sk = _sess(store)
    _clone_with_branches(tmp_path, _ARCH, project="huozhe/code-workflow")
    now = 10_000_000
    store.register_artifact(
        session_key=sk,
        role="architect",
        kind="branch",
        ref=_ARCH,
        created_at=now - ARTIFACT_AGE_FLOOR_S - 1,
    )
    report = _gc(store, tmp_path, now=now).collect_once()

    assert report["ledger_stale"] == []
    assert [a["ref"] for a in store.list_artifacts(sk, open_only=True)] == [_ARCH]
    store.close()


def test_young_stale_branch_row_survives_the_age_floor(tmp_path: Path) -> None:
    """ADR-20's floor still applies on the branch path."""
    store = Store(tmp_path / "state.db")
    sk = _sess(store)
    _clone_with_branches(tmp_path, _ARCH, project="huozhe/code-workflow")
    now = 10_000_000
    store.register_artifact(
        session_key=sk,
        role="developer",
        kind="branch",
        ref=_DEV,
        created_at=now - 5,
    )
    report = _gc(store, tmp_path, now=now).collect_once()

    assert report["ledger_stale"] == []
    assert len(store.list_artifacts(sk, open_only=True)) == 1
    store.close()


def test_branch_row_survives_an_unreadable_clone(tmp_path: Path) -> None:
    """No clone is not confirmation (gitops: "a missing clone is *not* confirmation").

    Without this the first GC pass after a clone is moved or not yet created
    would clear every branch row on no evidence at all.
    """
    store = Store(tmp_path / "state.db")
    sk = _sess(store)  # no clone created
    now = 10_000_000
    store.register_artifact(
        session_key=sk,
        role="developer",
        kind="branch",
        ref=_DEV,
        created_at=now - ARTIFACT_AGE_FLOOR_S - 1,
    )
    report = _gc(store, tmp_path, now=now).collect_once()

    assert report["ledger_stale"] == []
    assert len(store.list_artifacts(sk, open_only=True)) == 1
    store.close()


def test_dry_run_reports_the_branch_row_without_clearing_it(tmp_path: Path) -> None:
    """Checklist item: a dry run shows what would be cleared, and clears nothing."""
    store = Store(tmp_path / "state.db")
    sk = _sess(store)
    _clone_with_branches(tmp_path, _ARCH, project="huozhe/code-workflow")
    now = 10_000_000
    store.register_artifact(
        session_key=sk,
        role="developer",
        kind="branch",
        ref=_DEV,
        created_at=now - ARTIFACT_AGE_FLOOR_S - 1,
    )
    report = _gc(store, tmp_path, now=now).collect_once(dry_run=True)

    assert {"ref": _DEV, "kind": "branch"} in report["ledger_stale"]
    assert len(store.list_artifacts(sk, open_only=True)) == 1
    store.close()
