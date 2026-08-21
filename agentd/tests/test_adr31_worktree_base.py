"""ADR-31 acceptance: worktree base, the gateway fetch, and the local default.

Every fixture is offline. The promisor ones need all three of ``file://``,
``uploadpack.allowFilter=true`` and ``--no-checkout`` — each fails silently on
its own and leaves a clone with nothing missing, which tests nothing.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from agentd.gitops import (
    ensure_shared_clone,
    resolve_base_ref,
    shared_clone_path,
    worktree_add,
)

REPO = "local/testrepo"


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


def _source(tmp: Path, *, bare_for_filter: bool = False) -> Path:
    """A source repo with two commits. ``git init`` defaults to master here."""
    src = tmp / "src"
    src.mkdir()
    _git(src, "init")
    _git(src, "config", "user.email", "t@t")
    _git(src, "config", "user.name", "t")
    for n in ("one", "two"):
        (src / n).write_text(f"{n}\n", encoding="utf-8")
        _git(src, "add", ".")
        _git(src, "commit", "-m", n)
    if bare_for_filter:
        _git(src, "config", "uploadpack.allowFilter", "true")
    return src


def _advance(src: Path, name: str) -> None:
    (src / name).write_text(f"{name}\n", encoding="utf-8")
    _git(src, "add", ".")
    _git(src, "commit", "-m", name)


def _default(src: Path) -> str:
    return _git(src, "rev-parse", "--abbrev-ref", "HEAD")


def _missing(clone: Path) -> int:
    listed = subprocess.run(
        ["git", "rev-list", "--objects", "--all", "--missing=print"],
        cwd=clone,
        check=True,
        capture_output=True,
        text=True,
    )
    return sum(1 for line in listed.stdout.splitlines() if line.startswith("?"))


# ---------------------------------------------------------------- items 1, 2


def test_base_is_remote_ref_when_local_default_is_behind(tmp_path: Path) -> None:
    """(1) The fetch in the fixture is not optional.

    Without it the clone's ``origin/<default>`` stays at the clone-time commit,
    ``base_ref="HEAD"`` produces a branch at exactly that commit, and the count
    is 0 — the item passes against the bug it exists to catch.
    """
    src = _source(tmp_path)
    root = tmp_path / "root"
    clone = ensure_shared_clone(root, REPO, clone_url=str(src))
    _advance(src, "three")
    _git(clone, "fetch", "origin")

    default = _default(src)
    assert (
        _git(clone, "rev-list", "--count", f"refs/heads/{default}..origin/{default}")
        == "1"
    )

    worktree_add(clone, root / "wt", "sess", base_ref=resolve_base_ref(clone))
    assert _git(clone, "rev-list", "--count", f"sess..origin/{default}") == "0"


def test_base_is_remote_ref_when_local_default_is_forked(tmp_path: Path) -> None:
    """(2) The shape that actually occurred: no merge base at all."""
    src = _source(tmp_path)
    root = tmp_path / "root"
    clone = ensure_shared_clone(root, REPO, clone_url=str(src))
    default = _default(src)

    _git(clone, "checkout", "--detach", "--quiet", f"origin/{default}")
    _git(clone, "checkout", "--orphan", "forked")
    (clone / "unrelated").write_text("x\n", encoding="utf-8")
    _git(clone, "config", "user.email", "t@t")
    _git(clone, "config", "user.name", "t")
    _git(clone, "add", ".")
    _git(clone, "commit", "-m", "forked")
    _git(clone, "branch", "-f", default, "forked")
    _git(clone, "checkout", "--detach", "--quiet", "forked")

    merge_base = subprocess.run(
        ["git", "merge-base", default, f"origin/{default}"],
        cwd=clone,
        check=False,
        capture_output=True,
        text=True,
    )
    assert merge_base.returncode != 0, "fixture is not forked"

    worktree_add(clone, root / "wt", "sess", base_ref=resolve_base_ref(clone))
    assert _git(clone, "rev-list", "--count", f"sess..origin/{default}") == "0"


# ------------------------------------------------------------------- item 3


def test_ensure_shared_clone_fetches_an_existing_clone(tmp_path: Path) -> None:
    """(3) The ref moves with no worktree created."""
    src = _source(tmp_path)
    root = tmp_path / "root"
    clone = ensure_shared_clone(root, REPO, clone_url=str(src))
    default = _default(src)
    before = _git(clone, "rev-parse", f"origin/{default}")

    _advance(src, "three")
    ensure_shared_clone(root, REPO, clone_url=str(src))

    assert _git(clone, "rev-parse", f"origin/{default}") != before
    assert _git(clone, "worktree", "list").count("\n") == 0


# ------------------------------------------------------------------- item 4


def test_failed_fetch_does_not_refuse_the_session(tmp_path: Path) -> None:
    """(4) (a) has to hold without (b) — that is the whole argument for (b′)."""
    src = _source(tmp_path)
    root = tmp_path / "root"
    clone = ensure_shared_clone(root, REPO, clone_url=str(src))
    _git(clone, "remote", "set-url", "origin", str(tmp_path / "gone"))

    ensure_shared_clone(root, REPO, clone_url=str(src))

    base = resolve_base_ref(clone)
    assert _git(clone, "rev-parse", "--verify", base)
    worktree_add(clone, root / "wt", "sess", base_ref=base)
    assert (root / "wt").exists()


# ------------------------------------------------------------------- item 5


def test_default_branch_is_resolved_not_assumed(tmp_path: Path) -> None:
    """(5) ``git init`` gives ``master`` here, so this is the natural fixture."""
    src = _source(tmp_path)
    assert _default(src) == "master", "fixture no longer exercises a non-main default"
    root = tmp_path / "root"
    clone = ensure_shared_clone(root, REPO, clone_url=str(src))

    assert resolve_base_ref(clone) == "origin/master"


def test_dangling_origin_head_falls_back_to_head_and_logs(
    tmp_path: Path, caplog
) -> None:
    """(5) The dangling case is the one (b′) produces — not a deleted symref."""
    src = _source(tmp_path)
    root = tmp_path / "root"
    clone = ensure_shared_clone(root, REPO, clone_url=str(src))
    _git(clone, "update-ref", "-d", f"refs/remotes/origin/{_default(src)}")

    with caplog.at_level(logging.WARNING, logger="agentd.gitops"):
        base = resolve_base_ref(clone)

    assert base == "HEAD"
    assert "basing on HEAD" in caplog.text
    worktree_add(clone, root / "wt", "sess", base_ref=base)
    assert (root / "wt").exists()


# ------------------------------------------------------------------- item 6


def test_resumed_branch_keeps_commits_and_warns_on_the_live_path(
    tmp_path: Path, caplog
) -> None:
    """(6) The worktree is already present, so ``worktree_add`` returns early.

    A fixture with the branch but no worktree exercises only the create path and
    would pass green while the path live resumes take stays silent.
    """
    src = _source(tmp_path)
    root = tmp_path / "root"
    clone = ensure_shared_clone(root, REPO, clone_url=str(src))
    wt = root / "wt"
    worktree_add(clone, wt, "sess", base_ref=resolve_base_ref(clone))

    (wt / "agent-work").write_text("work\n", encoding="utf-8")
    _git(wt, "config", "user.email", "t@t")
    _git(wt, "config", "user.name", "t")
    _git(wt, "add", ".")
    _git(wt, "commit", "-m", "agent work")
    carried = _git(wt, "rev-parse", "HEAD")

    _advance(src, "three")
    _git(clone, "fetch", "origin")

    with caplog.at_level(logging.WARNING, logger="agentd.gitops"):
        worktree_add(clone, wt, "sess", base_ref=resolve_base_ref(clone))

    assert _git(wt, "rev-parse", "HEAD") == carried, "resume destroyed the agent's work"
    assert "commits behind" in caplog.text


def test_staleness_is_not_faked_when_the_base_fell_back(tmp_path: Path, caplog) -> None:
    """(6) ``<branch>..HEAD`` reads 0 — a count there reports "not stale"."""
    src = _source(tmp_path)
    root = tmp_path / "root"
    clone = ensure_shared_clone(root, REPO, clone_url=str(src))
    worktree_add(clone, root / "wt", "sess", base_ref=resolve_base_ref(clone))

    with caplog.at_level(logging.WARNING, logger="agentd.gitops"):
        worktree_add(clone, root / "wt", "sess", base_ref="HEAD")

    assert "base fell back to HEAD" in caplog.text
    assert "commits behind" not in caplog.text


# ------------------------------------------------------------------- item 7


def test_new_branch_has_no_upstream(tmp_path: Path) -> None:
    """(7) A regression test for the fix, not for the bug: ``HEAD`` set none."""
    src = _source(tmp_path)
    root = tmp_path / "root"
    clone = ensure_shared_clone(root, REPO, clone_url=str(src))
    worktree_add(clone, root / "wt", "sess", base_ref=resolve_base_ref(clone))

    upstream = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "sess@{upstream}"],
        cwd=clone,
        check=False,
        capture_output=True,
        text=True,
    )
    assert upstream.returncode != 0, f"branch tracks {upstream.stdout.strip()}"


# ------------------------------------------------------------------- item 8


def test_local_default_is_force_updated_across_a_fork(tmp_path: Path) -> None:
    """(8) Force, not fast-forward — a forked history has no merge base."""
    src = _source(tmp_path)
    root = tmp_path / "root"
    clone = ensure_shared_clone(root, REPO, clone_url=str(src))
    default = _default(src)

    _git(clone, "checkout", "--orphan", "forked")
    (clone / "unrelated").write_text("x\n", encoding="utf-8")
    _git(clone, "config", "user.email", "t@t")
    _git(clone, "config", "user.name", "t")
    _git(clone, "add", ".")
    _git(clone, "commit", "-m", "forked")
    _git(clone, "branch", "-f", default, "forked")
    _git(clone, "checkout", "--detach", "--quiet", "forked")

    _advance(src, "three")
    ensure_shared_clone(root, REPO, clone_url=str(src))

    assert _git(clone, "rev-parse", default) == _git(
        clone, "rev-parse", f"origin/{default}"
    )
    assert _git(clone, "rev-parse", "HEAD") == _git(
        clone, "rev-parse", f"origin/{default}"
    )


def test_re_point_spawns_no_fetch_on_a_promisor_clone(
    tmp_path: Path, monkeypatch
) -> None:
    """(8) The property that matters — ``checkout`` would fetch here, we do not.

    Lazy fetching is deliberately re-enabled for this test. ``GIT_NO_LAZY_FETCH``
    is defence in depth against a *future* working-tree touch; leaving it on here
    would test the guard instead of the binding, and the test would keep passing
    if the re-point were swapped back to ``checkout``.
    """
    from agentd import gitops

    def _permissive(*, lazy_fetch: bool = True) -> dict[str, str]:
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        return env

    monkeypatch.setattr(gitops, "_git_env", _permissive)

    src = _source(tmp_path, bare_for_filter=True)
    root = tmp_path / "root"
    clone = shared_clone_path(root, REPO)
    clone.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "git",
            "clone",
            "--filter=blob:none",
            "--no-checkout",
            f"file://{src}",
            str(clone),
        ],
        check=True,
        capture_output=True,
    )
    before = _missing(clone)
    assert before > 0, "fixture is not a promisor clone — nothing is being tested"

    _advance(src, "three")
    ensure_shared_clone(root, REPO, clone_url=str(src))

    default = _default(src)
    # The re-point must have happened, or "no blobs pulled" is true trivially.
    assert _git(clone, "rev-parse", "HEAD") == _git(
        clone, "rev-parse", f"origin/{default}"
    )
    assert _missing(clone) >= before, (
        "the re-point pulled blobs — checkout, not update-ref"
    )


def test_lazy_fetch_is_blocked_for_maintenance_but_not_for_worktrees() -> None:
    """(8) The guard is scoped, and the ADR's wording is not.

    ADR-31 says ``GIT_NO_LAZY_FETCH`` goes on "the gateway's git invocations".
    Applied to all of them it breaks ``worktree add`` on a promisor clone; see
    the promisor regression below for the failure it produces.
    """
    from agentd.gitops import _git_env

    assert _git_env()["GIT_TERMINAL_PROMPT"] == "0"
    assert "GIT_NO_LAZY_FETCH" not in _git_env()
    assert _git_env(lazy_fetch=False)["GIT_NO_LAZY_FETCH"] == "1"


def test_worktree_add_materialises_blobs_on_a_promisor_clone(tmp_path: Path) -> None:
    """(8) The agent worktree must be usable — it is where the agent edits.

    Found by running ``ensure_shared_clone`` against a copy of the live clone,
    not by a fixture: no local-path fixture is a promisor repo, which is the
    blind spot acceptance item 3 records.
    """
    src = _source(tmp_path, bare_for_filter=True)
    root = tmp_path / "root"
    clone = shared_clone_path(root, REPO)
    clone.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "git",
            "clone",
            "--filter=blob:none",
            "--no-checkout",
            f"file://{src}",
            str(clone),
        ],
        check=True,
        capture_output=True,
    )
    assert _missing(clone) > 0, "fixture is not a promisor clone"

    wt = root / "wt"
    worktree_add(clone, wt, "sess", base_ref=resolve_base_ref(clone))

    assert (wt / "one").read_text(encoding="utf-8") == "one\n"


def test_update_is_skipped_when_a_worktree_holds_the_default_branch(
    tmp_path: Path, caplog
) -> None:
    """(8) ``git branch -f`` is refused by *any* worktree, not only the root."""
    src = _source(tmp_path)
    root = tmp_path / "root"
    clone = ensure_shared_clone(root, REPO, clone_url=str(src))
    default = _default(src)
    _git(clone, "worktree", "add", str(root / "held"), default)

    _advance(src, "three")
    with caplog.at_level(logging.WARNING, logger="agentd.gitops"):
        ensure_shared_clone(root, REPO, clone_url=str(src))

    assert "stale" in caplog.text
    assert _git(clone, "rev-parse", f"origin/{default}") != _git(
        clone, "rev-parse", default
    )
    worktree_add(clone, root / "wt", "sess", base_ref=resolve_base_ref(clone))
    assert (root / "wt").exists(), "a refused branch update must not refuse the session"


# ------------------------------------------------------------------- item 9


def test_clone_root_index_is_emptied(tmp_path: Path) -> None:
    """(9) The fixture clones *with* a checkout — the existing-clone shape.

    A ``--no-checkout`` fixture starts with an empty index and passes vacuously.
    The defect this covers is invisible to every other assertion: skipping
    ``read-tree --empty`` still gets the refs right and still spawns no fetch.
    """
    src = _source(tmp_path)
    root = tmp_path / "root"
    clone = shared_clone_path(root, REPO)
    clone.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", str(src), str(clone)], check=True, capture_output=True
    )
    assert _git(clone, "ls-files"), "fixture has no index — nothing is being tested"

    ensure_shared_clone(root, REPO, clone_url=str(src))

    assert _git(clone, "ls-files") == ""
