"""Shared clones + worktrees — §6.3 / §6.4. Only agentd mutates the shared clone."""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger("agentd.gitops")

# ADR-31 (b): a fetch that hangs under admit_lock blocks every project's drain.
_FETCH_TIMEOUT_S = 120.0
# The clone is a different job with a different failure: it runs once, moves the
# whole history, and has no stale state to fall back on — so it gets its own
# budget and, unlike the fetch, a timeout there refuses the session.
_CLONE_TIMEOUT_S = 600.0


def _git_env(*, lazy_fetch: bool = True) -> dict[str, str]:
    """Never prompt; block lazy fetching only where it is a hazard (ADR-31).

    The ADR asks for ``GIT_NO_LAZY_FETCH`` on "the gateway's git invocations".
    Read literally that breaks ``worktree add``, which on a promisor clone must
    materialise the blobs the agent is about to edit — verified against the live
    clone: *fatal: could not fetch … from promisor remote*. The guard belongs on
    the clone-root maintenance calls, which is where a working-tree touch would
    silently re-open the hazard (d) closes.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    if not lazy_fetch:
        env["GIT_NO_LAZY_FETCH"] = "1"
    return env


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
    if (
        not (path / ".git").exists()
        and not (path / "HEAD").exists()
        and ((legacy / ".git").exists() or (legacy / "HEAD").exists())
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            log.info("migrating legacy clone %s → %s", legacy, path)
            legacy.rename(path)

    with _lock_for(path):
        if not (path / ".git").exists() and not (path / "HEAD").exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            url = clone_url or f"https://github.com/{repo_full}.git"
            log.info("cloning %s → %s (timeout %.0fs)", url, path, _CLONE_TIMEOUT_S)
            subprocess.run(
                # --no-checkout: the root working tree has no consumer, and
                # creating it is what makes its index pin objects (ADR-31 (d)).
                ["git", "clone", "--filter=blob:none", "--no-checkout", url, str(path)],
                check=True,
                capture_output=True,
                text=True,
                timeout=_CLONE_TIMEOUT_S,
                env=_git_env(),
            )
        _git(path, "config", "gc.auto", "0")
        _git(path, "config", "worktree.useRelativePaths", "true")
        if _fetch(path):
            _sync_default_branch(path)
    return path


def _fetch(clone: Path) -> bool:
    """ADR-31 (b)/(b′): refresh the clone; a failure warns and proceeds.

    Safe to proceed because (a) verifies the base ref exists before use, so a
    fetch that could not run leaves the base at worst as stale as the last
    successful one.

    Returns False when *either* call fails, including a ``set-head`` failure
    after a fetch that did succeed. That skips (d) for a clone which did in fact
    refresh — deliberate, not an oversight: (d) force-moves a shared ref, and
    doing that off a default branch we could not confirm is the worse trade.
    (d) is therefore best-effort on a flaky network, exactly as (b′) is.
    """
    for args in (
        ("fetch", "origin", "--prune"),
        ("remote", "set-head", "origin", "--auto"),
    ):
        try:
            done = _git(clone, *args, check=False, timeout=_FETCH_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            log.warning("git %s timed out in %s — proceeding stale", args[0], clone)
            return False
        if done.returncode != 0:
            log.warning(
                "git %s failed in %s (%s) — proceeding stale",
                args[0],
                clone,
                (done.stderr or "").strip()[:200],
            )
            return False
    return True


def _sync_default_branch(clone: Path) -> None:
    """ADR-31 (d): the local default branch is a working ref — agents read it.

    Order matters: re-pointing HEAD first detaches it, which is what lets
    ``git branch -f`` touch the branch the clone root would otherwise hold.
    """
    base = resolve_base_ref(clone)
    if base == "HEAD":
        return
    head = _git(clone, "rev-parse", base, check=False)
    if head.returncode != 0:
        return
    sha = head.stdout.strip()
    # HEAD by update-ref, never checkout: on a promisor clone checkout spawns a
    # lazy fetch as its own child, which no timeout of ours can reach.
    _git(clone, "update-ref", "--no-deref", "HEAD", sha, check=False, lazy_fetch=False)
    # A stale index is a gc reachability root and pins the trees it names.
    _git(clone, "read-tree", "--empty", check=False, lazy_fetch=False)
    default = base.split("/", 1)[1]
    forced = _git(clone, "branch", "-f", default, base, check=False, lazy_fetch=False)
    if forced.returncode != 0:
        log.warning(
            "left %s in %s stale: %s",
            default,
            clone,
            (forced.stderr or "").strip()[:200],
        )


def resolve_base_ref(clone: Path) -> str:
    """ADR-31 (a): ``origin/<default>``, or ``HEAD`` when that cannot be verified.

    The fallback is deliberately the local branch this ADR exists to stop
    reading: it converges on the first successful fetch and claims nothing
    before it. It is logged so the degraded state is visible, not inferred.
    """
    named = _git(
        clone, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False
    )
    base = named.stdout.strip()
    if (
        base
        and _git(clone, "rev-parse", "--verify", "-q", base, check=False).returncode
        == 0
    ):
        return base
    log.warning(
        "cannot resolve origin/HEAD in %s (got %r) — basing on HEAD",
        clone,
        base,
    )
    return "HEAD"


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
        # Above the early return: host worktrees outlive ensure_session, so a
        # live stale resume takes that path and would otherwise log nothing.
        _log_staleness(clone, branch, base_ref)
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
                # A remote-tracking start point would otherwise set an upstream
                # that base_ref="HEAD" never set (ADR-31 (a)).
                "--no-track",
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


def _log_staleness(clone: Path, branch: str, base_ref: str) -> None:
    """ADR-31 (c): an existing branch is never re-based — it carries the work.

    Measuring against ``HEAD`` would compare the branch with the ref it was
    created from and always read 0, so the fallback logs itself instead.
    """
    if base_ref == "HEAD":
        log.warning(
            "staleness unknown for %s in %s: base fell back to HEAD", branch, clone
        )
        return
    if not _git(clone, "branch", "--list", branch, check=False).stdout.strip():
        return
    counted = _git(clone, "rev-list", "--count", f"{branch}..{base_ref}", check=False)
    if counted.returncode != 0:
        return
    behind = counted.stdout.strip()
    if behind and behind != "0":
        log.warning("%s in %s is %s commits behind %s", branch, clone, behind, base_ref)


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


def _git(
    cwd: Path,
    *args: str,
    check: bool = True,
    timeout: float | None = None,
    lazy_fetch: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=check,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_git_env(lazy_fetch=lazy_fetch),
    )
