"""Resource Governor skeleton — §12.1 / §12.2 (NFR-2.2)."""

from __future__ import annotations

import logging
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

from agentd.db import Store
from agentd.disk import disk_free_gb

log = logging.getLogger("agentd.governor")

NotifyFn = Callable[[str, str], None]


def macos_notify(message: str, title: str = "agentd: storage circuit breaker") -> None:
    """Best-effort osascript notification (NFR-2.2).

    Always logs the *attempt* at INFO with return code. Exit 0 does not prove
    Notification Center showed a banner (TCC may suppress); the log correlates
    LaunchAgent trip → notify call site for host verification.
    """
    log.info("macos_notify attempt title=%r (launchd/osascript path)", title)
    try:
        proc = subprocess.run(
            [
                "osascript",
                "-e",
                (f'display notification "{_escape_applescript(message)}" '
                f'with title "{_escape_applescript(title)}" sound name "Basso"'),
            ],
            check=False,
            capture_output=True,
            timeout=5,
        )
        log.info(
            "macos_notify osascript rc=%s stderr=%r",
            proc.returncode,
            (proc.stderr or b"")[:200],
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("macos_notify failed: %s", exc)


def _escape_applescript(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


class ResourceGovernor:
    """Sample free disk every interval; trip below floor, reset above resume.

    Notification fires on TRIP only (not RESET). RESET is logged; the
    authoritative operator signal on trip is also the durable SQLite latch
    and (later) per-issue GitHub comments — macOS notify is best-effort.
    """

    def __init__(
        self,
        store: Store,
        root: Path,
        *,
        floor_gb: float = 15.0,
        resume_gb: float = 20.0,
        interval_s: float = 30.0,
        notify: NotifyFn | None = macos_notify,
        free_gb_fn: Callable[[Path], float | None] | None = None,
        on_trip: Callable[[], None] | None = None,
    ) -> None:
        self.store = store
        self.root = root
        self.floor_gb = floor_gb
        self.resume_gb = resume_gb
        self.interval_s = interval_s
        self.notify = notify
        self.on_trip = on_trip
        self._free_gb = free_gb_fn or disk_free_gb
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="agentd-governor", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.interval_s + 1)
            self._thread = None

    def sample_once(self) -> float | None:
        """One sample cycle. Returns free GB (or None if unreadable)."""
        free = self._free_gb(self.root)
        if free is None:
            log.warning("disk free space unreadable at %s", self.root)
            return None

        paused = self.store.is_disk_paused()
        if free < self.floor_gb and not paused:
            reason = f"disk {free:.1f} GB < floor {self.floor_gb:g} GB"
            self.store.set_disk_paused(True, reason=reason)
            log.warning("circuit breaker TRIP: %s", reason)
            if self.notify:
                self.notify(
                    f"Free disk {free:.1f} GB — agentd paused",
                    "agentd: storage circuit breaker",
                )
            if self.on_trip:
                self.on_trip()
        elif free > self.resume_gb and paused:
            reason = f"disk {free:.1f} GB > resume {self.resume_gb:g} GB"
            self.store.set_disk_paused(False, reason=reason)
            log.info("circuit breaker RESET: %s", reason)
        return free

    def _loop(self) -> None:
        # Sample immediately on start, then every interval.
        while not self._stop.is_set():
            try:
                self.sample_once()
            except Exception:
                log.exception("governor sample failed")
            self._stop.wait(self.interval_s)
