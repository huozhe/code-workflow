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
    """huozhe/code-workflow#42 → huozhe__code-workflow__42 (legacy flat layout)."""
    owner_repo, _, num = session_key.partition("#")
    owner, _, repo = owner_repo.partition("/")
    return f"{owner}__{repo}__{num}"


def project_key_from_repo(repo_full: str) -> str:
    """owner/repo → project key (also used as runners PK)."""
    return repo_full.strip()


def project_key_from_session(session_key: str) -> str:
    owner_repo, _, _ = session_key.partition("#")
    return project_key_from_repo(owner_repo)


def project_dir_name(repo_full: str) -> str:
    """owner/repo → owner__repo (§6.2 project tree)."""
    owner, _, repo = repo_full.partition("/")
    return f"{owner}__{repo}"


def role_branch_name(repo_full: str, issue_num: int, role: str) -> str:
    """Supervisor-minted branch: agentd/<owner__repo>/<issue>/<role> (§6.2 / M3-D)."""
    return f"agentd/{project_dir_name(repo_full)}/{int(issue_num)}/{role}"


def parse_role_branch(head_ref: str | None, repo_full: str) -> tuple[int, str] | None:
    """Parse ``agentd/<owner__repo>/<issue>/<role>`` → (issue_num, role) or None."""
    if not head_ref or not repo_full:
        return None
    prefix = f"agentd/{project_dir_name(repo_full)}/"
    if not head_ref.startswith(prefix):
        return None
    rest = head_ref[len(prefix) :]
    num_s, sep, role = rest.partition("/")
    if not sep or not role or "/" in role:
        return None
    try:
        return int(num_s), role
    except ValueError:
        return None


def is_design_head_ref(head_ref: str | None, repo_full: str) -> bool:
    """True when head is the Architect worktree branch (mechanical Design PR signal)."""
    parsed = parse_role_branch(head_ref, repo_full)
    return parsed is not None and parsed[1] == "architect"


def is_feature_head_ref(head_ref: str | None, repo_full: str) -> bool:
    """True when head is the Developer worktree branch (mechanical Feature PR signal)."""
    parsed = parse_role_branch(head_ref, repo_full)
    return parsed is not None and parsed[1] == "developer"


def project_path(root: Path, repo_full: str) -> Path:
    return root / "projects" / project_dir_name(repo_full)


def issue_session_rel(issue_num: int) -> str:
    """Relative path under project for one issue's session dirs."""
    return f"sessions/{int(issue_num)}"


def shared_clone_path(root: Path, repo_full: str) -> Path:
    """Shared clone lives under the project tree (§6.2)."""
    return project_path(root, repo_full) / "repo"


def legacy_shared_clone_path(root: Path, repo_full: str) -> Path:
    owner, _, repo = repo_full.partition("/")
    return root / "repos" / owner / repo


def ensure_shared_clone(
    root: Path,
    repo_full: str,
    *,
    clone_url: str | None = None,
) -> Path:
    """Ensure project clone exists with gc.auto=0 + relative worktrees.

    Migrates a legacy ``repos/<owner>/<repo>`` clone into the project tree
    when present and the project clone is missing.
    """
    path = shared_clone_path(root, repo_full)
    legacy = legacy_shared_clone_path(root, repo_full)
    if not (path / ".git").exists() and not (path / "HEAD").exists():
        if (legacy / ".git").exists() or (legacy / "HEAD").exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                log.info("migrating legacy clone %s → %s", legacy, path)
                legacy.rename(path)

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


def _worktree_already_on_branch(worktree_path: Path, branch: str) -> bool:
    """True when path is a git worktree whose HEAD is *branch* (#78 B1)."""
    if not worktree_path.exists():
        return False
    head = subprocess.run(
        ["git", "-C", str(worktree_path), "rev-parse", "--abbrev-ref", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    return head.returncode == 0 and head.stdout.strip() == branch


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
        if _worktree_already_on_branch(worktree_path, branch):
            return 0.0
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


def local_branch_gone(clone: Path, branch: str) -> bool:
    """True only after we listed the clone and the branch is absent.

    A missing clone is *not* confirmation — do not mark the ledger.
    """
    if not branch:
        return False
    if not clone.exists():
        return False
    if not (clone / ".git").exists() and not (clone / "HEAD").exists():
        return False
    listed = _git(clone, "branch", "--list", branch, check=False)
    return not bool((listed.stdout or "").strip())


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=check,
        capture_output=True,
        text=True,
    )
