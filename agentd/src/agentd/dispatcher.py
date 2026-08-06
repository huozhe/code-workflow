"""Dispatcher skeleton — drain queued deliveries (M1: intake + status only)."""

from __future__ import annotations

import logging
import threading

from agentd.config import Config
from agentd.db import Store, decompress_payload
from agentd.intake import evaluate_intake

log = logging.getLogger("agentd.dispatcher")


class Dispatcher:
    """Scan ``status='queued'``; pause when the disk breaker is open.

    Unhandled events become ``deferred`` (not ``routed``). ``routed`` is
    reserved for hand-off to a real session (M2+). See §15.1 M1 amendment.
    """

    def __init__(
        self,
        store: Store,
        config: Config,
        nudge: threading.Event,
        *,
        idle_wait_s: float = 5.0,
    ) -> None:
        self.store = store
        self.config = config
        self.nudge = nudge
        self.idle_wait_s = idle_wait_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="agentd-dispatcher", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.nudge.set()  # wake waiters
        if self._thread:
            self._thread.join(timeout=self.idle_wait_s + 1)
            self._thread = None

    def drain_once(self) -> int:
        """Process up to one batch. Returns number of rows transitioned."""
        if self.store.is_disk_paused():
            log.debug("dispatcher paused (disk breaker open)")
            return 0
        n = 0
        for row in self.store.list_queued(limit=100):
            if self.store.is_disk_paused() or self._stop.is_set():
                break
            self._handle(row)
            n += 1
        return n

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.drain_once()
            except Exception:
                log.exception("dispatcher drain failed")
            self.nudge.wait(timeout=self.idle_wait_s)
            self.nudge.clear()

    def _handle(self, row) -> None:
        delivery_id = str(row["delivery_id"])
        event = str(row["event"])
        action = row["action"]
        action_s = str(action) if action is not None else None
        payload = decompress_payload(row["payload"])

        if event == "ping":
            self.store.set_delivery_status(delivery_id, "done")
            log.info("delivery done id=%s event=ping", delivery_id)
            return

        decision = evaluate_intake(
            event=event,
            action=action_s,
            payload=payload,
            config=self.config,
        )
        if decision is not None:
            if decision.accepted:
                # Intake passed but session create is M2 — park as deferred.
                # Do NOT use routed: that means handed to a sessions row.
                self.store.set_delivery_status(delivery_id, "deferred")
                log.info(
                    "delivery deferred id=%s event=%s action=%s (%s); session create is M2",
                    delivery_id,
                    event,
                    action_s,
                    decision.reason,
                )
            else:
                self.store.set_delivery_status(delivery_id, "dropped")
                log.info(
                    "delivery dropped id=%s event=%s action=%s: %s",
                    delivery_id,
                    event,
                    action_s,
                    decision.reason,
                )
            return

        # Non-intake events (comments, PRs, push, …): no M1 handler.
        # deferred keeps them out of the hot scan without pretending they were routed.
        self.store.set_delivery_status(delivery_id, "deferred")
        log.info(
            "delivery deferred id=%s event=%s action=%s (no M1 handler; M2+ resumes)",
            delivery_id,
            event,
            action_s,
        )
