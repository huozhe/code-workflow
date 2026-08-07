"""SQLite state store — §15.1 (M1 full schema; M0 subset still primary)."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
import zlib
from pathlib import Path
from typing import Any

log = logging.getLogger("agentd.db")

# Bump when DDL changes require a rebuild. SQLite is a derived cache (ADR-2);
# mismatch ⇒ wipe + recreate. GitHub remains source of truth (P1).
SCHEMA_VERSION = 1

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

CREATE TABLE IF NOT EXISTS sessions (
  session_key TEXT PRIMARY KEY,
  repo TEXT NOT NULL,
  issue_num INTEGER NOT NULL,
  state TEXT NOT NULL,
  paused_reason TEXT,
  architect TEXT NOT NULL,
  developer TEXT NOT NULL,
  roles_locked INTEGER NOT NULL DEFAULT 0,
  design_pr INTEGER,
  feature_pr INTEGER,
  turn_count INTEGER NOT NULL DEFAULT 0,
  consec_agent_turns INTEGER NOT NULL DEFAULT 0,
  review_rounds INTEGER NOT NULL DEFAULT 0,
  progress_fp TEXT,
  progress_repeat INTEGER NOT NULL DEFAULT 0,
  zero_thread_rounds INTEGER NOT NULL DEFAULT 0,
  gh_watermark INTEGER,
  verified_at INTEGER,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS runners (
  session_key TEXT PRIMARY KEY,
  container_id TEXT,
  endpoint TEXT,
  token TEXT,
  tier TEXT NOT NULL,
  last_seen_at INTEGER,
  FOREIGN KEY (session_key) REFERENCES sessions(session_key) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS turns (
  turn_id TEXT PRIMARY KEY,
  session_key TEXT NOT NULL,
  role TEXT NOT NULL,
  delivery_id TEXT,
  started_at INTEGER NOT NULL,
  ended_at INTEGER,
  status TEXT,
  summary TEXT,
  FOREIGN KEY (session_key) REFERENCES sessions(session_key) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS artifacts (
  id INTEGER PRIMARY KEY,
  session_key TEXT NOT NULL,
  role TEXT NOT NULL,
  kind TEXT NOT NULL,
  ref TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  removed_at INTEGER,
  FOREIGN KEY (session_key) REFERENCES sessions(session_key) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS escalations (
  id INTEGER PRIMARY KEY,
  session_key TEXT NOT NULL,
  role TEXT,
  reason TEXT NOT NULL,
  comment_id INTEGER,
  opened_at INTEGER NOT NULL,
  resolved_at INTEGER
);
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
        self._apply_schema()
        row = self._conn.execute("PRAGMA integrity_check").fetchone()
        if row is None or row[0] != "ok":
            raise RuntimeError(f"SQLite integrity_check failed: {row}")
        fk = self._conn.execute("PRAGMA foreign_keys").fetchone()
        if not fk or int(fk[0]) != 1:
            raise RuntimeError("PRAGMA foreign_keys is not enabled")

    def _schema_version(self) -> int:
        row = self._conn.execute("PRAGMA user_version").fetchone()
        return int(row[0]) if row else 0

    def _apply_schema(self) -> None:
        """Create/migrate schema. ADR-2: mismatch ⇒ destructive rebuild."""
        ver = self._schema_version()
        if ver == SCHEMA_VERSION:
            # Still run IF NOT EXISTS so a partially-created file heals.
            self._conn.executescript(SCHEMA)
            return
        if ver == 0:
            # Fresh DB or pre-version M0 file: CREATE IF NOT EXISTS keeps rows.
            self._conn.executescript(SCHEMA)
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            log.info("schema stamped user_version=%s", SCHEMA_VERSION)
            return
        if ver > SCHEMA_VERSION:
            raise RuntimeError(
                f"state.db user_version={ver} is newer than agentd "
                f"SCHEMA_VERSION={SCHEMA_VERSION}; upgrade the binary"
            )
        # ver < SCHEMA_VERSION and ver != 0: destructive rebuild (ADR-2).
        log.warning(
            "schema user_version=%s < SCHEMA_VERSION=%s; rebuilding derived "
            "cache (ADR-2). Deliveries re-sync from GitHub via Reconciler.",
            ver,
            SCHEMA_VERSION,
        )
        self._rebuild_schema()

    def _rebuild_schema(self) -> None:
        tables = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        for t in tables:
            self._conn.execute(f'DROP TABLE IF EXISTS "{t[0]}"')
        self._conn.executescript(SCHEMA)
        self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._conn.commit()

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

    def list_queued(self, limit: int = 100) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._conn.execute(
                    """
                    SELECT delivery_id, event, action, repo, issue_num, sender,
                           received_at, payload, status
                    FROM deliveries
                    WHERE status = 'queued'
                    ORDER BY received_at ASC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            )

    def set_delivery_status(self, delivery_id: str, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE deliveries SET status = ? WHERE delivery_id = ?",
                (status, delivery_id),
            )
            self._conn.commit()

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

    def count_by_status(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM deliveries GROUP BY status"
            ).fetchall()
            return {str(r["status"]): int(r["n"]) for r in rows}

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

    def breaker_state(self) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT disk_paused, reason, updated_at FROM circuit_breaker WHERE id = 1"
            ).fetchone()
            if not row:
                return {"disk_paused": False, "reason": None, "updated_at": 0}
            return {
                "disk_paused": bool(row["disk_paused"]),
                "reason": row["reason"],
                "updated_at": int(row["updated_at"] or 0),
            }

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT session_key, repo, issue_num, state, paused_reason,
                       architect, developer, updated_at
                FROM sessions
                ORDER BY updated_at DESC
                """
            ).fetchall()
            return [dict(r) for r in rows]

    def get_session(self, session_key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT s.*, r.container_id, r.endpoint, r.token AS runner_token, r.tier
                FROM sessions s
                LEFT JOIN runners r ON r.session_key = s.session_key
                WHERE s.session_key = ?
                """,
                (session_key,),
            ).fetchone()
            return dict(row) if row else None

    def upsert_session(
        self,
        *,
        session_key: str,
        repo: str,
        issue_num: int,
        state: str,
        architect: str,
        developer: str,
        created_at: int,
        updated_at: int,
        paused_reason: str | None = None,
        roles_locked: int | None = None,
        design_pr: int | None = None,
        turn_count: int | None = None,
        consec_agent_turns: int | None = None,
        review_rounds: int | None = None,
        progress_fp: str | None = None,
        progress_repeat: int | None = None,
        zero_thread_rounds: int | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO sessions(
                  session_key, repo, issue_num, state, paused_reason,
                  architect, developer, roles_locked, design_pr,
                  turn_count, consec_agent_turns, review_rounds,
                  progress_fp, progress_repeat, zero_thread_rounds,
                  created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_key) DO UPDATE SET
                  state = excluded.state,
                  paused_reason = COALESCE(excluded.paused_reason, sessions.paused_reason),
                  roles_locked = COALESCE(excluded.roles_locked, sessions.roles_locked),
                  design_pr = COALESCE(excluded.design_pr, sessions.design_pr),
                  turn_count = COALESCE(excluded.turn_count, sessions.turn_count),
                  consec_agent_turns = COALESCE(excluded.consec_agent_turns, sessions.consec_agent_turns),
                  review_rounds = COALESCE(excluded.review_rounds, sessions.review_rounds),
                  progress_fp = COALESCE(excluded.progress_fp, sessions.progress_fp),
                  progress_repeat = COALESCE(excluded.progress_repeat, sessions.progress_repeat),
                  zero_thread_rounds = COALESCE(excluded.zero_thread_rounds, sessions.zero_thread_rounds),
                  updated_at = excluded.updated_at
                """,
                (
                    session_key,
                    repo,
                    issue_num,
                    state,
                    paused_reason,
                    architect,
                    developer,
                    0 if roles_locked is None else roles_locked,
                    design_pr,
                    0 if turn_count is None else turn_count,
                    0 if consec_agent_turns is None else consec_agent_turns,
                    0 if review_rounds is None else review_rounds,
                    progress_fp,
                    0 if progress_repeat is None else progress_repeat,
                    0 if zero_thread_rounds is None else zero_thread_rounds,
                    created_at,
                    updated_at,
                ),
            )
            self._conn.commit()

    def update_session_fields(self, session_key: str, **fields: Any) -> None:
        if not fields:
            return
        allowed = {
            "state",
            "paused_reason",
            "roles_locked",
            "design_pr",
            "feature_pr",
            "turn_count",
            "consec_agent_turns",
            "review_rounds",
            "progress_fp",
            "progress_repeat",
            "zero_thread_rounds",
            "updated_at",
        }
        cols = []
        vals: list[Any] = []
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(f"disallowed session field {k}")
            cols.append(f"{k} = ?")
            vals.append(v)
        if "updated_at" not in fields:
            cols.append("updated_at = ?")
            vals.append(int(time.time()))
        vals.append(session_key)
        with self._lock:
            self._conn.execute(
                f"UPDATE sessions SET {', '.join(cols)} WHERE session_key = ?",
                vals,
            )
            self._conn.commit()

    def list_deferred(self, limit: int = 100) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._conn.execute(
                    """
                    SELECT delivery_id, event, action, repo, issue_num, sender,
                           received_at, payload, status
                    FROM deliveries
                    WHERE status = 'deferred'
                    ORDER BY received_at ASC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            )

    def quarantine_deferred(
        self,
        *,
        before_received_at: int | None = None,
        reason: str = "quarantine",
    ) -> int:
        """Move deferred rows to terminal ``dropped`` (one-shot backlog purge).

        Returns number of rows updated. ``before_received_at`` if set only
        touches deliveries received strictly before that unix timestamp.
        """
        with self._lock:
            if before_received_at is not None:
                cur = self._conn.execute(
                    """
                    UPDATE deliveries
                    SET status = 'dropped'
                    WHERE status = 'deferred' AND received_at < ?
                    """,
                    (before_received_at,),
                )
            else:
                cur = self._conn.execute(
                    """
                    UPDATE deliveries
                    SET status = 'dropped'
                    WHERE status = 'deferred'
                    """
                )
            self._conn.commit()
            n = int(cur.rowcount or 0)
        return n

    def count_hot_sessions(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM runners WHERE tier = 'hot'"
            ).fetchone()
            return int(row["n"]) if row else 0

    def insert_turn(
        self,
        *,
        turn_id: str,
        session_key: str,
        role: str,
        delivery_id: str | None,
        started_at: int,
        ended_at: int | None,
        status: str | None,
        summary: str | None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO turns(
                  turn_id, session_key, role, delivery_id,
                  started_at, ended_at, status, summary
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    turn_id,
                    session_key,
                    role,
                    delivery_id,
                    started_at,
                    ended_at,
                    status,
                    summary,
                ),
            )
            self._conn.commit()

    def register_artifact(
        self,
        *,
        session_key: str,
        role: str,
        kind: str,
        ref: str,
        created_at: int | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO artifacts(session_key, role, kind, ref, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_key, role, kind, ref, created_at or int(time.time())),
            )
            self._conn.commit()

    def open_escalation(
        self,
        *,
        session_key: str,
        role: str | None,
        reason: str,
        comment_id: int | None = None,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO escalations(session_key, role, reason, comment_id, opened_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_key, role, reason, comment_id, int(time.time())),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def get_runner(self, session_key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM runners WHERE session_key = ?",
                (session_key,),
            ).fetchone()
            return dict(row) if row else None

    def upsert_runner(
        self,
        session_key: str,
        *,
        container_id: str,
        endpoint: str,
        token: str,
        tier: str,
    ) -> None:
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO runners(
                  session_key, container_id, endpoint, token, tier, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_key) DO UPDATE SET
                  container_id = excluded.container_id,
                  endpoint = excluded.endpoint,
                  token = excluded.token,
                  tier = excluded.tier,
                  last_seen_at = excluded.last_seen_at
                """,
                (session_key, container_id, endpoint, token, tier, now),
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
            by_status = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM deliveries GROUP BY status"
            ).fetchall()
            br = self._conn.execute(
                "SELECT disk_paused, reason, updated_at FROM circuit_breaker WHERE id = 1"
            ).fetchone()
            sessions_n = self._conn.execute(
                "SELECT COUNT(*) AS n FROM sessions"
            ).fetchone()
            return {
                "db": str(self.path),
                "deliveries_total": int(total["n"]) if total else 0,
                "queue_depth": int(queued["n"]) if queued else 0,
                "deliveries_by_status": {
                    str(r["status"]): int(r["n"]) for r in by_status
                },
                "disk_paused": bool(br and br["disk_paused"]),
                "breaker_reason": br["reason"] if br else None,
                "breaker_updated_at": int(br["updated_at"] or 0) if br else 0,
                "sessions": int(sessions_n["n"]) if sessions_n else 0,
            }
