"""SQLite state store — M0 subset of §15.1 (deliveries + circuit breaker)."""

from __future__ import annotations

import sqlite3
import threading
import time
import zlib
from pathlib import Path
from typing import Any


# Schema DDL only — connection pragmas are set separately (see Store.__init__).
SCHEMA = """
CREATE TABLE IF NOT EXISTS deliveries (
  delivery_id TEXT PRIMARY KEY,
  event TEXT NOT NULL,
  action TEXT,
  repo TEXT NOT NULL DEFAULT '',
  issue_num INTEGER,
  sender TEXT NOT NULL DEFAULT '',
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


def compress_payload(raw: bytes) -> bytes:
    """zlib (RFC 1950) compress — §15.1 payload storage."""
    return zlib.compress(raw, level=6)


def decompress_payload(blob: bytes) -> bytes:
    return zlib.decompress(blob)


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # foreign_keys cannot be set inside executescript's implicit transaction.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        row = self._conn.execute("PRAGMA integrity_check").fetchone()
        if row is None or row[0] != "ok":
            raise RuntimeError(f"SQLite integrity_check failed: {row}")
        fk = self._conn.execute("PRAGMA foreign_keys").fetchone()
        if not fk or int(fk[0]) != 1:
            raise RuntimeError("PRAGMA foreign_keys is not enabled")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def foreign_keys_enabled(self) -> bool:
        with self._lock:
            row = self._conn.execute("PRAGMA foreign_keys").fetchone()
            return bool(row and int(row[0]) == 1)

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
        blob = compress_payload(payload)
        with self._lock:
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
                    repo if repo is not None else "",
                    issue_num,
                    sender if sender is not None else "",
                    int(time.time()),
                    blob,
                    status,
                ),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def queue_depth(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM deliveries WHERE status = 'queued'"
            ).fetchone()
            return int(row["n"]) if row else 0

    def delivery_count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM deliveries").fetchone()
            return int(row["n"]) if row else 0

    def is_disk_paused(self) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT disk_paused FROM circuit_breaker WHERE id = 1"
            ).fetchone()
            return bool(row and row["disk_paused"])

    def set_disk_paused(self, paused: bool, reason: str | None = None) -> None:
        with self._lock:
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
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) AS n FROM deliveries"
            ).fetchone()
            queued = self._conn.execute(
                "SELECT COUNT(*) AS n FROM deliveries WHERE status = 'queued'"
            ).fetchone()
            br = self._conn.execute(
                "SELECT disk_paused FROM circuit_breaker WHERE id = 1"
            ).fetchone()
            return {
                "db": str(self.path),
                "deliveries_total": int(total["n"]) if total else 0,
                "queue_depth": int(queued["n"]) if queued else 0,
                "disk_paused": bool(br and br["disk_paused"]),
            }
