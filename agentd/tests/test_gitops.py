"""Shared clone + worktree timing (§6.4)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from agentd.gitops import ensure_shared_clone, local_branch_gone, worktree_add


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


def test_local_branch_gone_does_not_confirm_when_git_fails(tmp_path: Path) -> None:
    """#167: rc != 0 is "asked and could not read", not "gone".

    A failing git writes nothing to stdout, so a stdout-only decision reads a
    broken clone as confirmation that the branch is absent. Both broken shapes
    below pass the directory and `.git` existence guards.
    """
    clone = tmp_path / "repo"
    (clone / ".git").mkdir(parents=True)
    assert local_branch_gone(clone, "agentd/p/32/architect") is False

    real = tmp_path / "real"
    real.mkdir()
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *args], cwd=real, check=True, capture_output=True)
    (real / "f").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=real, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "i"], cwd=real, check=True, capture_output=True)
    subprocess.run(
        ["git", "branch", "agentd/p/32/architect"], cwd=real, check=True, capture_output=True
    )
    # the fixture distinguishes, so the assertions below mean something
    assert local_branch_gone(real, "agentd/p/32/architect") is False
    assert local_branch_gone(real, "agentd/p/32/developer") is True

    (real / ".git" / "HEAD").write_text("garbage\n", encoding="utf-8")
    assert local_branch_gone(real, "agentd/p/32/developer") is False
