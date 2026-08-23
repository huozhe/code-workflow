"""Shared clone + worktree timing (§6.4)."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

from agentd.gitops import (
    ensure_shared_clone,
    local_branch_gone,
    scratch_dir_cleared,
    worktree_add,
)


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


def test_local_branch_gone_does_not_confirm_when_git_fails(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """#167: rc != 0 is "asked and could not read", not "gone".

    A failing git writes nothing to stdout, so a stdout-only decision reads a
    broken clone as confirmation that the branch is absent. Both broken shapes
    below pass the directory and `.git` existence guards.
    """
    clone = tmp_path / "repo"
    (clone / ".git").mkdir(parents=True)
    with caplog.at_level(logging.WARNING, logger="agentd.gitops"):
        assert local_branch_gone(clone, "agentd/p/32/architect") is False
    # The severity is the assertion, not just the text: a clone that stays
    # unreadable must be visible rather than silently inert, and an INFO line
    # is not. Same shape as #188's override log.
    assert any(
        r.levelno == logging.WARNING and "not confirming absence" in r.getMessage()
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]

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


def test_scratch_dir_cleared_counts_empty_as_gone(tmp_path: Path) -> None:
    """#167: shared by the teardown confirm path and GC's ledger sweep."""
    missing = tmp_path / "gone"
    assert scratch_dir_cleared(str(missing)) is True

    empty = tmp_path / "empty"
    empty.mkdir()
    assert scratch_dir_cleared(str(empty)) is True

    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "f").write_text("x\n", encoding="utf-8")
    assert scratch_dir_cleared(str(busy)) is False


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@example.com"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)
    (path / "f").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "i"], cwd=path, check=True, capture_output=True)


def test_nested_unusable_clone_is_answered_by_its_ancestor(tmp_path: Path) -> None:
    """#190: git discovery walks up, so rc=0 is not evidence about *this* clone.

    The ancestor must **not** carry the branch: that is what makes its answer an
    empty list, which is exactly what "the branch is gone" looks like. An earlier
    version of this fixture gave the ancestor the branch, so the ancestor's answer
    was non-empty, the wrong reading never occurred, and the test would have
    passed against the unfixed predicate.
    """
    outer = tmp_path / "outer"
    _init_repo(outer)
    branch = "agentd/p/32/developer"

    for name, make in (
        ("empty-git", lambda p: (p / ".git").mkdir(parents=True)),
        ("corrupt-head", _broken_head_clone),
    ):
        nested = outer / name
        make(nested)

        # Fixture proof, in two parts. Without these the assertion below passes
        # on a fixture that never reached the case (#190's own failure mode).
        listed = subprocess.run(
            ["git", "branch", "--list", branch],
            cwd=nested,
            capture_output=True,
            text=True,
            check=False,
        )
        assert listed.returncode == 0, (
            f"{name}: git must SUCCEED here, or #189's rc guard is what refuses "
            "and this test proves nothing new"
        )
        assert (listed.stdout or "").strip() == "", (
            f"{name}: the ancestor must answer with an empty list — that is what "
            "reads as confirmed absence"
        )
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=nested,
            capture_output=True,
            text=True,
            check=False,
        )
        assert Path(top.stdout.strip()).resolve() == outer.resolve(), (
            f"{name}: the answer must come from the ancestor, not the clone"
        )

        assert local_branch_gone(nested, branch) is False, (
            f"{name}: an answer about a different repository is not confirmation"
        )


def _broken_head_clone(path: Path) -> None:
    _init_repo(path)
    (path / ".git" / "HEAD").write_text("garbage\n", encoding="utf-8")


def test_healthy_clone_still_answers_both_ways_inside_a_repo(tmp_path: Path) -> None:
    """The guard must not refuse a clone that is genuinely readable (#190).

    Nested inside a repository on purpose: the guard compares resolved paths, and
    a clone under a symlinked root (macOS `/tmp` → `/private/tmp`) would fail a
    string compare while being perfectly healthy.
    """
    outer = tmp_path / "outer"
    _init_repo(outer)
    healthy = outer / "healthy"
    _init_repo(healthy)
    subprocess.run(
        ["git", "branch", "agentd/p/32/architect"],
        cwd=healthy,
        check=True,
        capture_output=True,
    )

    assert local_branch_gone(healthy, "agentd/p/32/architect") is False
    assert local_branch_gone(healthy, "agentd/p/32/developer") is True


def test_the_two_guards_are_independent(tmp_path: Path) -> None:
    """#189 refuses what git could not answer; #190 refuses answers from elsewhere.

    With no ancestor repository the discovery guard has nothing to catch, so an
    unusable clone must still be refused — by the `rc != 0` guard alone.
    """
    lone = tmp_path / "lone"
    (lone / ".git").mkdir(parents=True)
    listed = subprocess.run(
        ["git", "branch", "--list", "agentd/p/32/developer"],
        cwd=lone,
        capture_output=True,
        text=True,
        check=False,
    )
    assert listed.returncode != 0, (
        "fixture: with no ancestor, git must FAIL — that is #189's case, and if "
        "it succeeds this tmp dir is inside a checkout"
    )
    assert local_branch_gone(lone, "agentd/p/32/developer") is False
