"""M6-2 / ADR-23: hourly GC — report leaks, delete only the unambiguous."""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agentd.config import Config
from agentd.db import Store
from agentd.gitops import project_dir_name, shared_clone_path

log = logging.getLogger("agentd.gc")

GC_INTERVAL_S = 60 * 60
ARTIFACT_AGE_FLOOR_S = 10 * 60  # ADR-20
TMP_MAX_AGE_S = 60 * 60

ListContainers = Callable[[], list[dict[str, Any]]]
ListImages = Callable[[], list[dict[str, Any]]]
RemoveContainer = Callable[[str], None]
PruneImage = Callable[[str], None]
GitGc = Callable[[Path], None]


def _is_managed(c: dict[str, Any]) -> bool:
    labels = c.get("labels") or {}
    return str(labels.get("agentd.managed") or "") == "true"


def list_managed_and_foreign_containers() -> list[dict[str, Any]]:
    """docker ps -a (unfiltered). GC keeps only agentd.managed=true."""
    r = subprocess.run(
        ["docker", "ps", "-a", "--format", "{{.ID}}\t{{.Names}}\t{{.State}}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"docker ps failed: {(r.stderr or '')[:200]}")
    ids = []
    meta: dict[str, tuple[str, str]] = {}
    for line in (r.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        cid, name, state = parts[0].strip(), parts[1].strip(), parts[2].strip()
        if cid:
            ids.append(cid)
            meta[cid] = (name, state)
    if not ids:
        return []
    r2 = subprocess.run(
        ["docker", "inspect", *ids],
        check=False,
        capture_output=True,
        text=True,
    )
    if r2.returncode != 0:
        raise RuntimeError(f"docker inspect failed: {(r2.stderr or '')[:200]}")
    import json

    data = json.loads(r2.stdout or "[]")
    out: list[dict[str, Any]] = []
    for obj in data:
        cid = str(obj.get("Id") or "")[:12]
        labels = (obj.get("Config") or {}).get("Labels") or {}
        name, state = meta.get(cid, ("", ""))
        if not name:
            names = obj.get("Name") or ""
            name = str(names).lstrip("/")
        running = bool((obj.get("State") or {}).get("Running"))
        out.append(
            {
                "id": str(obj.get("Id") or cid),
                "name": name,
                "labels": dict(labels),
                "running": running,
            }
        )
    return out


class GarbageCollector:
    """Own hourly timer. Never runs on the reconciler thread."""

    def __init__(
        self,
        store: Store,
        config: Config,
        supervisor: Any | None = None,
        *,
        list_containers: ListContainers | None = None,
        list_images: ListImages | None = None,
        remove_container: RemoveContainer | None = None,
        prune_image: PruneImage | None = None,
        git_gc: GitGc | None = None,
        now_fn: Callable[[], int] | None = None,
        interval_s: float = GC_INTERVAL_S,
    ) -> None:
        self.store = store
        self.config = config
        self.supervisor = supervisor
        self.list_containers = list_containers or list_managed_and_foreign_containers
        self.list_images = list_images or (lambda: [])
        self.remove_container = remove_container or _docker_rm
        self.prune_image = prune_image or _docker_rmi
        self.git_gc = git_gc or _run_git_gc
        self._now = now_fn or (lambda: int(time.time()))
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._nudge = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="agentd-gc", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._nudge.set()
        if self._thread:
            self._thread.join(timeout=min(self.interval_s, 5) + 1)
            self._thread = None

    def signal(self) -> None:
        """Breaker trip: run a pass on the GC thread, not inline."""
        self._nudge.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.collect_once()
            except Exception:
                log.exception("gc collect_once failed")
            self._nudge.clear()
            self._nudge.wait(self.interval_s)
            if self._stop.is_set():
                return

    def collect_once(self, *, dry_run: bool = False) -> dict[str, Any]:
        t0 = time.monotonic()
        now = int(self._now())
        report: dict[str, Any] = {
            "archives_expired": [],
            "tmps_expired": [],
            "residue": [],
            "orphans": [],
            "ledger_stale": [],
            "containers": [],
            "images": [],
            "git_gc": [],
        }
        self._sweep_archive(report, now=now, dry_run=dry_run)
        self._sweep_session_dirs(report, now=now)
        self._sweep_ledger(report, now=now, dry_run=dry_run)
        self._sweep_containers(report, dry_run=dry_run)
        self._maybe_git_gc(report, dry_run=dry_run)
        ms = int((time.monotonic() - t0) * 1000)
        log.info(
            "gc pass residue=%d orphans=%d archives=%d tmps=%d "
            "containers=%d images=%d ledger_stale=%d git_gc=%d dry_run=%s in %dms",
            len(report["residue"]),
            len(report["orphans"]),
            len(report["archives_expired"]),
            len(report["tmps_expired"]),
            len(report["containers"]),
            len(report["images"]),
            len(report["ledger_stale"]),
            len(report["git_gc"]),
            dry_run,
            ms,
        )
        return report

    def _sweep_archive(
        self, report: dict[str, Any], *, now: int, dry_run: bool
    ) -> None:
        root = Path(self.config.root) / "archive"
        if not root.is_dir():
            return
        retain_s = int(self.config.archive_days) * 86400
        for entry in root.iterdir():
            if entry.is_file():
                self._consider_archive_file(
                    entry, report, now=now, retain_s=retain_s, dry_run=dry_run
                )
                continue
            if not entry.is_dir():
                continue
            tars = list(entry.glob("*.tar.gz"))
            tmps = list(entry.glob("*.tar.gz.tmp"))
            for f in tars + tmps:
                self._consider_archive_file(
                    f, report, now=now, retain_s=retain_s, dry_run=dry_run
                )
            if not tars and not tmps:
                report["residue"].append({"ref": str(entry)})
                log.warning("gc archive residue path=%s", entry)

    def _consider_archive_file(
        self,
        path: Path,
        report: dict[str, Any],
        *,
        now: int,
        retain_s: int,
        dry_run: bool,
    ) -> None:
        try:
            mtime = int(path.stat().st_mtime)
        except OSError:
            return
        age = now - mtime
        name = path.name
        if name.endswith(".tar.gz.tmp"):
            if age >= TMP_MAX_AGE_S:
                report["tmps_expired"].append({"ref": str(path)})
                if not dry_run:
                    path.unlink(missing_ok=True)
            return
        if name.endswith(".tar.gz") and age >= retain_s:
            report["archives_expired"].append({"ref": str(path)})
            if not dry_run:
                path.unlink(missing_ok=True)

    def _sweep_session_dirs(self, report: dict[str, Any], *, now: int) -> None:
        projects = Path(self.config.root) / "projects"
        if not projects.is_dir():
            return
        sessions_by_issue: dict[tuple[str, int], dict[str, Any]] = {}
        for sess in self.store.list_sessions():
            repo = str(sess.get("repo") or "")
            try:
                issue = int(sess.get("issue_num") or 0)
            except (TypeError, ValueError):
                continue
            if repo and issue:
                sessions_by_issue[(project_dir_name(repo), issue)] = sess
        for proj in projects.iterdir():
            if not proj.is_dir():
                continue
            sess_root = proj / "sessions"
            if not sess_root.is_dir():
                continue
            for child in sess_root.iterdir():
                if not child.is_dir() or not child.name.isdigit():
                    continue
                issue = int(child.name)
                key = (proj.name, issue)
                sess = sessions_by_issue.get(key)
                state = str((sess or {}).get("state") or "")
                if sess is not None and state != "CLOSED":
                    continue
                try:
                    mtime = int(child.stat().st_mtime)
                except OSError:
                    continue
                if now - mtime < ARTIFACT_AGE_FLOOR_S:
                    continue
                report["orphans"].append({"ref": str(child), "state": state or "absent"})
                log.warning(
                    "gc orphan session_dir path=%s state=%s",
                    child,
                    state or "absent",
                )

    def _sweep_ledger(
        self, report: dict[str, Any], *, now: int, dry_run: bool
    ) -> None:
        for sess in self.store.list_sessions():
            sk = str(sess.get("session_key") or "")
            if not sk:
                continue
            for art in self.store.list_artifacts(sk, open_only=True):
                ref = str(art.get("ref") or "")
                kind = str(art.get("kind") or "")
                if kind not in ("worktree", "scratch"):
                    continue
                p = Path(ref)
                if p.exists():
                    continue
                created = int(art.get("created_at") or 0)
                if created <= 0 or now - created < ARTIFACT_AGE_FLOOR_S:
                    continue
                report["ledger_stale"].append({"ref": ref, "kind": kind})
                if not dry_run:
                    self.store.mark_artifact_removed(
                        session_key=sk, ref=ref, kind=kind, removed_at=now
                    )

    def _sweep_containers(self, report: dict[str, Any], *, dry_run: bool) -> None:
        try:
            raw = self.list_containers()
        except Exception:
            log.exception("gc container inventory failed")
            return
        for c in raw:
            if not _is_managed(c):
                continue
            name = str(c.get("name") or "")
            report["containers"].append(
                {"name": name, "id": str(c.get("id") or ""), "running": bool(c.get("running"))}
            )
            if not c.get("running") and not dry_run:
                cid = str(c.get("id") or name)
                if cid:
                    try:
                        self.remove_container(cid)
                    except Exception:
                        log.exception("gc remove container failed id=%s", cid)

    def _maybe_git_gc(self, report: dict[str, Any], *, dry_run: bool) -> None:
        live = self.store.live_project_keys()
        seen: set[str] = set()
        for sess in self.store.list_sessions():
            repo = str(sess.get("repo") or "")
            if not repo or repo in seen:
                continue
            seen.add(repo)
            pk = str(sess.get("project_key") or repo)
            clone = shared_clone_path(self.config.root, repo)
            if not (clone / ".git").exists() and not clone.is_dir():
                continue
            lock = getattr(self.supervisor, "admit_lock", None) if self.supervisor else None
            if lock is None:
                if pk in live:
                    continue
                report["git_gc"].append({"repo": repo, "skipped": False})
                if not dry_run:
                    self.git_gc(clone)
                continue
            with lock:
                live = self.store.live_project_keys()
                if pk in live:
                    continue
                report["git_gc"].append({"repo": repo, "skipped": False})
                if not dry_run:
                    self.git_gc(clone)


def _docker_rm(cid: str) -> None:
    subprocess.run(["docker", "rm", "-f", cid], check=False, capture_output=True)


def _docker_rmi(image: str) -> None:
    subprocess.run(["docker", "rmi", image], check=False, capture_output=True)


def _run_git_gc(clone: Path) -> None:
    subprocess.run(
        ["git", "-C", str(clone), "gc", "--auto"],
        check=False,
        capture_output=True,
    )
