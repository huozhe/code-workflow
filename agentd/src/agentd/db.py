"""SQLite state store — M0 subset of §15.1 (deliveries + circuit breaker)."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS deliveries (
  delivery_id TEXT PRIMARY KEY,
  event TEXT NOT NULL,
  action TEXT,
  repo TEXT,
  issue_num INTEGER,
  sender TEXT,
  received_at INTEGER NOT NULL,
  payload BLOB NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued'
);
CREATE INDEX IF NOT EXISTS ix_deliveries_pending
  ON deliveries(status, received_at);

CREATE TABLE IF NOT EXISTS circuit_breaker (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  disk_paused INTEGER NOT NULL DEFAULT 0,
  reason TEXT,
  updated_at INTEGER NOT NULL
);

INSERT OR IGNORE INTO circuit_breaker(id, disk_paused, reason, updated_at)
VALUES (1, 0, NULL, 0);
"""


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        row = self._conn.execute("PRAGMA integrity_check").fetchone()
        if row is None or row[0] != "ok":
            raise RuntimeError(f"SQLite integrity_check failed: {row}")

    def close(self) -> None:
        self._conn.close()

    def insert_delivery(
        self,
        *,
        delivery_id: str,
        event: str,
        action: str | None,
        repo: str | None,
        issue_num: int | None,
        sender: str | None,
        payload: bytes,
        status: str = "queued",
    ) -> bool:
        """INSERT OR IGNORE. Returns True if a new row was inserted."""
        cur = self._conn.execute(
            """
            INSERT OR IGNORE INTO deliveries(
              delivery_id, event, action, repo, issue_num, sender,
              received_at, payload, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                delivery_id,
                event,
                action,
                repo,
                issue_num,
                sender,
                int(time.time()),
                payload,
                status,
            ),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def queue_depth(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM deliveries WHERE status = 'queued'"
        ).fetchone()
        return int(row["n"]) if row else 0

    def delivery_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM deliveries").fetchone()
        return int(row["n"]) if row else 0

    def is_disk_paused(self) -> bool:
        row = self._conn.execute(
            "SELECT disk_paused FROM circuit_breaker WHERE id = 1"
        ).fetchone()
        return bool(row and row["disk_paused"])

    def set_disk_paused(self, paused: bool, reason: str | None = None) -> None:
        self._conn.execute(
            """
            UPDATE circuit_breaker
            SET disk_paused = ?, reason = ?, updated_at = ?
            WHERE id = 1
            """,
            (1 if paused else 0, reason, int(time.time())),
        )
        self._conn.commit()

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "db": str(self.path),
            "deliveries_total": self.delivery_count(),
            "queue_depth": self.queue_depth(),
            "disk_paused": self.is_disk_paused(),
        }
