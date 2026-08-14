"""M6-1a / ADR-20: local half of §11.2 — integrity is Store.__init__; this
inventories containers and nudges the dispatcher. No GitHub API."""

from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from agentd.db import Store

log = logging.getLogger("agentd.reconciler")

CONTAINER_AGE_FLOOR_S = 10 * 60
INFLIGHT_TURN_MAX_AGE_S = 900  # matches gateway turn_deadline_s default
RECONCILE_INTERVAL_S = 5 * 60

ListContainers = Callable[[], list[dict[str, Any]]]
RemoveContainer = Callable[[str], None]
NudgeFn = Callable[[], None]


def pragma_integrity_check(db_path: Path) -> str:
    """Read PRAGMA without Store() — Store raises on a bad file."""
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        return str(row[0]) if row else "failed"
    except sqlite3.Error as exc:
        return f"error: {exc}"
    finally:
        conn.close()


def _parse_started_at(raw: str) -> int:
    s = (raw or "").strip()
    if not s:
        return 0
    s = s.replace("Z", "+00:00")
    if "." in s:
        head, tail = s.split(".", 1)
        sign = "+" if "+" in tail else ("-" if "-" in tail[1:] else "")
        if sign:
            frac, _, tz = tail.partition(sign)
            tz = sign + tz
        else:
            frac, tz = tail, "+00:00"
        s = f"{head}.{(frac + '000000')[:6]}{tz}"
    try:
        return int(datetime.fromisoformat(s).timestamp())
    except ValueError:
        return 0


def _cid_match(a: str, b: str) -> bool:
    if not a or not b:
        return False
    return a == b or a.startswith(b) or b.startswith(a)


def list_managed_containers() -> list[dict[str, Any]]:
    r = subprocess.run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            "label=agentd.managed=true",
            "-q",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    ids = [x.strip() for x in (r.stdout or "").split() if x.strip()]
    if not ids or r.returncode != 0:
        if r.returncode != 0:
            raise RuntimeError(f"docker ps failed: {(r.stderr or '')[:200]}")
        return []
    r2 = subprocess.run(
        ["docker", "inspect", *ids],
        check=False,
        capture_output=True,
        text=True,
    )
    if r2.returncode != 0:
        raise RuntimeError(f"docker inspect failed: {(r2.stderr or '')[:200]}")
    data = json.loads(r2.stdout or "[]")
    out: list[dict[str, Any]] = []
    for obj in data:
        labels = (obj.get("Config") or {}).get("Labels") or {}
        out.append(
            {
                "id": str(obj.get("Id") or ""),
                "project": str(labels.get("agentd.project") or ""),
                "started_at": _parse_started_at(
                    str((obj.get("State") or {}).get("StartedAt") or "")
                ),
            }
        )
    return out


def _docker_rm(container_id: str) -> None:
    subprocess.run(
        ["docker", "rm", "-f", container_id],
        check=False,
        capture_output=True,
        text=True,
    )


class Reconciler:
    """Inventory managed containers; nudge the dispatcher. No drain."""

    def __init__(
        self,
        store: Store,
        *,
        list_containers: ListContainers | None = None,
        remove_container: RemoveContainer | None = None,
        nudge: NudgeFn | None = None,
        interval_s: float = RECONCILE_INTERVAL_S,
        now_fn: Callable[[], int] | None = None,
    ) -> None:
        self.store = store
        self.list_containers = list_containers or list_managed_containers
        self.remove_container = remove_container or _docker_rm
        self.nudge = nudge
        self.interval_s = interval_s
        self._now = now_fn or (lambda: int(time.time()))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="agentd-reconciler", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.interval_s + 1)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.reconcile_once()
            except Exception:
                log.exception("reconcile_once failed")
            self._stop.wait(self.interval_s)

    def _log_pass(self, report: dict[str, Any], *, containers: int, kept: int, started: float) -> None:
        ms = int((time.monotonic() - started) * 1000)
        log.info(
            "reconcile pass containers=%d kept=%d spared=%d removed=%d cleared=%d "
            "open_turns=%d closed_live=%d in %dms",
            containers,
            kept,
            len(report["spared"]),
            len(report["removed"]),
            len(report["cleared_rows"]),
            len(report["open_turns"]),
            len(report["closed_live"]),
            ms,
        )

    def reconcile_once(self, *, dry_run: bool = False) -> dict[str, Any]:
        t0 = time.monotonic()
        report: dict[str, Any] = {
            "orphans": [],
            "spared": [],
            "removed": [],
            "cleared_rows": [],
            "missing_runners": [],
            "stale_runners": [],
            "open_turns": self.store.list_open_turns(),
            "closed_live": self.store.list_live_sessions_with_closed_delivery(),
        }
        for row in report["closed_live"]:
            sk = str(row.get("session_key") or "")
            if sk:
                row["open_artifacts"] = self.store.count_open_artifacts(sk)

        try:
            containers = self.list_containers()
        except Exception:
            log.exception("container inventory failed — no removals this pass")
            if not dry_run and self.nudge:
                self.nudge()
            report["inventory_error"] = True
            self._log_pass(report, containers=0, kept=0, started=t0)
            return report

        now = int(self._now())
        live = self.store.live_project_keys()
        inflight = self.store.projects_with_open_turns(
            now=now, max_age_s=INFLIGHT_TURN_MAX_AGE_S
        )
        runners = {str(r["project_key"]): r for r in self.store.list_runners()}
        seen_ids = [str(c.get("id") or "") for c in containers]
        n_kept = 0

        for c in containers:
            cid = str(c.get("id") or "")
            pk = str(c.get("project") or "")
            started = int(c.get("started_at") or 0)
            age = now - started
            decision = self._decide(
                project=pk,
                container_id=cid,
                age_s=age,
                started_at=started,
                live=pk in live if pk else False,
                inflight=pk in inflight if pk else False,
                runner=runners.get(pk) if pk else None,
            )
            entry = {
                "id": cid,
                "project": pk,
                "age_s": age,
                "action": decision["action"],
                "reason": decision["reason"],
            }
            if decision["action"] == "keep":
                n_kept += 1
            elif decision["action"] == "spare":
                report["spared"].append(entry)
            elif decision["action"] == "remove":
                report["orphans"].append(entry)
                if not dry_run:
                    self.remove_container(cid)
                    report["removed"].append(entry)
                    log.warning(
                        "reconcile removed container=%s project=%s reason=%s",
                        cid,
                        pk,
                        decision["reason"],
                    )
            elif decision["action"] == "missing_runner":
                report["missing_runners"].append(entry)
                if not dry_run:
                    self.remove_container(cid)
                    report["removed"].append(entry)
                    log.warning(
                        "reconcile removed tokenless container=%s project=%s",
                        cid,
                        pk,
                    )

        for pk, row in runners.items():
            rid = str(row.get("container_id") or "")
            if rid and not any(_cid_match(rid, sid) for sid in seen_ids if sid):
                stale = {"project": pk, "container_id": rid}
                report["stale_runners"].append(stale)
                if not dry_run:
                    self.store.delete_runner(pk)
                    report["cleared_rows"].append(stale)
                    log.info("reconcile cleared stale runner project=%s", pk)

        if not dry_run and self.nudge:
            self.nudge()
        self._log_pass(report, containers=len(containers), kept=n_kept, started=t0)
        return report

    def _decide(
        self,
        *,
        project: str,
        container_id: str,
        age_s: int,
        started_at: int,
        live: bool,
        inflight: bool,
        runner: dict[str, Any] | None,
    ) -> dict[str, str]:
        young = started_at <= 0 or age_s < CONTAINER_AGE_FLOOR_S
        if live and runner and _cid_match(str(runner.get("container_id") or ""), container_id):
            return {"action": "keep", "reason": "live runner"}
        if young:
            return {"action": "spare", "reason": "age < 10m"}
        if inflight:
            return {"action": "spare", "reason": "open turn"}
        if live and not runner:
            return {"action": "missing_runner", "reason": "no runners row"}
        if live and runner:
            return {"action": "remove", "reason": "extra container"}
        return {"action": "remove", "reason": "no live session"}
