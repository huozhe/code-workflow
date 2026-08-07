"""Shared clones + worktrees — §6.3 / §6.4. Only agentd mutates the shared clone."""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger("agentd.gitops")

_repo_locks: dict[str, threading.Lock] = {}
_repo_locks_guard = threading.Lock()


def _lock_for(repo_path: Path) -> threading.Lock:
    key = str(repo_path.resolve())
    with _repo_locks_guard:
        if key not in _repo_locks:
            _repo_locks[key] = threading.Lock()
        return _repo_locks[key]


def session_dir_name(session_key: str) -> str:
    """huozhe/code-workflow#42 → huozhe__code-workflow__42"""
    owner_repo, _, num = session_key.partition("#")
    owner, _, repo = owner_repo.partition("/")
    return f"{owner}__{repo}__{num}"


def shared_clone_path(root: Path, repo_full: str) -> Path:
    owner, _, repo = repo_full.partition("/")
    return root / "repos" / owner / repo


def ensure_shared_clone(
    root: Path,
    repo_full: str,
    *,
    clone_url: str | None = None,
) -> Path:
    """Ensure ~/.agentd/repos/<owner>/<repo> exists with gc.auto=0 + relative worktrees."""
    path = shared_clone_path(root, repo_full)
    with _lock_for(path):
        if not (path / ".git").exists() and not (path / "HEAD").exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            url = clone_url or f"https://github.com/{repo_full}.git"
            log.info("cloning %s → %s", url, path)
            subprocess.run(
                ["git", "clone", "--filter=blob:none", url, str(path)],
                check=True,
                capture_output=True,
                text=True,
            )
        _git(path, "config", "gc.auto", "0")
        _git(path, "config", "worktree.useRelativePaths", "true")
    return path


def worktree_add(
    clone: Path,
    worktree_path: Path,
    branch: str,
    *,
    base_ref: str = "HEAD",
) -> float:
    """``git worktree add`` under the per-repo mutex. Returns elapsed seconds."""
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    with _lock_for(clone):
        t0 = time.perf_counter()
        # Remove stale worktree dir if present
        if worktree_path.exists():
            try:
                _git(clone, "worktree", "remove", "--force", str(worktree_path))
            except subprocess.CalledProcessError:
                pass
        # Create branch from base if needed, then add worktree
        existing = _git(clone, "branch", "--list", branch, check=False)
        if existing.stdout.strip():
            _git(clone, "worktree", "add", str(worktree_path), branch)
        else:
            _git(
                clone,
                "worktree",
                "add",
                "-b",
                branch,
                str(worktree_path),
                base_ref,
            )
        elapsed = time.perf_counter() - t0
        log.info(
            "worktree add %s branch=%s elapsed=%.3fs",
            worktree_path,
            branch,
            elapsed,
        )
        return elapsed


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=check,
        capture_output=True,
        text=True,
    )
