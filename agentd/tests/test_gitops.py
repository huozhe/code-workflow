"""Shared clone + worktree timing (§6.4)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from agentd.gitops import ensure_shared_clone, worktree_add


def _init_bare_source(tmp: Path) -> Path:
    src = tmp / "src"
    src.mkdir()
    subprocess.run(["git", "init"], cwd=src, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"],
        cwd=src,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "t"],
        cwd=src,
        check=True,
        capture_output=True,
    )
    (src / "README").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=src, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=src,
        check=True,
        capture_output=True,
    )
    return src


def test_worktree_add_under_two_seconds_warm(tmp_path: Path) -> None:
    """Warm worktree timing.

    Uses a tiny fixture repo so CI stays offline. This does **not** stress the
    §21 budget against a large working tree (node_modules / language-server
    indexes). Architect-measured ~30 ms on this repo (48 files) — still not a
    scale proof. Note on #10 if/when a large checkout is available.
    """
    src = _init_bare_source(tmp_path)
    root = tmp_path / "agentd-root"
    clone = ensure_shared_clone(
        root,
        "local/testrepo",
        clone_url=str(src),
    )
    wt1 = root / "sessions" / "a" / "worktrees" / "w1"
    elapsed1 = worktree_add(clone, wt1, "branch-w1")
    wt2 = root / "sessions" / "a" / "worktrees" / "w2"
    elapsed2 = worktree_add(clone, wt2, "branch-w2")
    assert elapsed2 < 2.0, f"warm worktree add took {elapsed2:.3f}s (limit 2s)"
    assert (wt2 / "README").exists()
    assert elapsed1 < 5.0


def test_worktree_add_keeps_uncommitted_when_already_on_branch(tmp_path: Path) -> None:
    """#78 B1: layout refresh must not wipe a live worktree."""
    src = _init_bare_source(tmp_path)
    root = tmp_path / "agentd-root"
    clone = ensure_shared_clone(root, "local/testrepo", clone_url=str(src))
    wt = root / "sessions" / "a" / "worktrees" / "w1"
    worktree_add(clone, wt, "branch-w1")
    dirty = wt / "uncommitted.py"
    dirty.write_text("keep\n", encoding="utf-8")
    worktree_add(clone, wt, "branch-w1")
    assert dirty.read_text(encoding="utf-8") == "keep\n"
