"""Session archive write + purge (M5-3 / ADR-12 / §10.5 step 3).

Retention sweep is §12.3 / M6 — this module only writes.
"""

from __future__ import annotations

import json
import os
import shutil
import tarfile
import time
from pathlib import Path
from typing import Any

from agentd.gitops import issue_session_rel, project_dir_name, project_path


def archive_tarball_path(root: Path, repo: str, issue_num: int) -> Path:
    """ADR-12: archive/<owner>__<repo>/<issue_num>.tar.gz."""
    return root / "archive" / project_dir_name(repo) / f"{int(issue_num)}.tar.gz"


def session_dir_path(root: Path, repo: str, issue_num: int) -> Path:
    return project_path(root, repo) / issue_session_rel(issue_num)


def build_manifest(
    *,
    session_key: str,
    project_key: str,
    terminal_state: str,
    design_pr: int | None,
    feature_pr: int | None,
    turn_count: int,
    closed_at: int,
) -> dict[str, Any]:
    return {
        "session_key": session_key,
        "project_key": project_key,
        "terminal_state": terminal_state,
        "design_pr": design_pr,
        "feature_pr": feature_pr,
        "turn_count": int(turn_count),
        "closed_at": int(closed_at),
    }


def write_manifest(session_dir: Path, manifest: dict[str, Any]) -> Path:
    """Write manifest.json into the session dir *before* tarring (ADR-12)."""
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return path


def write_tarball(session_dir: Path, dest: Path) -> Path:
    """Write dest.tmp, fsync, rename. Never leave a partial final name."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    with tarfile.open(tmp, "w:gz") as tf:
        tf.add(session_dir, arcname=session_dir.name)
    with open(tmp, "rb") as fh:
        os.fsync(fh.fileno())
    os.replace(tmp, dest)
    return dest


def purge_session_dir(session_dir: Path) -> None:
    if session_dir.is_dir():
        shutil.rmtree(session_dir)


def archive_and_purge(
    *,
    root: Path,
    repo: str,
    issue_num: int,
    session_key: str,
    project_key: str,
    terminal_state: str,
    design_pr: int | None,
    feature_pr: int | None,
    turn_count: int,
    closed_at: int | None = None,
) -> Path | None:
    """If the session dir exists: manifest, tar, purge. Return dest or None.

    Caller flips CLOSED after this returns. A missing dir is the
    purge-then-crash recovery case — no rewrite, no error.
    """
    session_dir = session_dir_path(root, repo, issue_num)
    dest = archive_tarball_path(root, repo, issue_num)
    if not session_dir.is_dir():
        return dest if dest.is_file() else None
    ts = int(closed_at if closed_at is not None else time.time())
    write_manifest(
        session_dir,
        build_manifest(
            session_key=session_key,
            project_key=project_key,
            terminal_state=terminal_state,
            design_pr=design_pr,
            feature_pr=feature_pr,
            turn_count=turn_count,
            closed_at=ts,
        ),
    )
    write_tarball(session_dir, dest)
    purge_session_dir(session_dir)
    return dest


def format_completion_summary(
    *,
    session_key: str,
    classification: str,
    design_pr: int | None = None,
    feature_pr: int | None = None,
    turn_count: int = 0,
) -> str:
    """§10.3: VERIFIED close gets a completion summary; ABANDONED does not."""
    parts = [f"Session `{session_key}` closed as **{classification}**."]
    if design_pr:
        parts.append(f"Design PR: #{int(design_pr)}.")
    if feature_pr:
        parts.append(f"Feature PR: #{int(feature_pr)}.")
    parts.append(f"Turns: {int(turn_count)}.")
    return " ".join(parts)
