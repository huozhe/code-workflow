"""SQLite state store — §15.1 (M1 full schema; M0 subset still primary)."""

from __future__ import annotations

import json
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
SCHEMA_VERSION = 9

# Schema DDL only — connection pragmas are set separately (see Store.__init__).
# v2 (#20): runners keyed by project (N sessions : 1 runner); sessions.project_key.
# v3 (M3-A): sessions.resume_state for PAUSED_HUMAN → pre-pause restore (§8.5).
# v4 (M3-B): sessions.stall_open_threads JSON for zero-thread delta (§9.3).
# v5 (#35): project_blocks for structural ensure_session refusal (per project).
# v6 (#39): turns.public_actions JSON; sessions.silent_turns for dead-end stall.
# v7 (M5-2): sessions.classification — VERIFIED/ABANDONED at issues.closed.
# v8 (M6-1b / ADR-21): delivery_nodes + sessions.closed_issue_escalated_at.
# v9 (M6-1c / ADR-22): turns.resume_attempts.
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

CREATE TABLE IF NOT EXISTS delivery_nodes (
  node_id TEXT PRIMARY KEY,
  delivery_id TEXT NOT NULL,
  seen_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS circuit_breaker (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  disk_paused INTEGER NOT NULL DEFAULT 0,
  reason TEXT,
  updated_at INTEGER NOT NULL
);

INSERT OR IGNORE INTO circuit_breaker(id, disk_paused, reason, updated_at)
VALUES (1, 0, NULL, 0);

CREATE TABLE IF NOT EXISTS runners (
  project_key TEXT PRIMARY KEY,
  container_id TEXT,
  endpoint TEXT,
  token TEXT,
  tier TEXT NOT NULL,
  last_seen_at INTEGER
);

CREATE TABLE IF NOT EXISTS sessions (
  session_key TEXT PRIMARY KEY,
  project_key TEXT NOT NULL,
  repo TEXT NOT NULL,
  issue_num INTEGER NOT NULL,
  state TEXT NOT NULL,
  paused_reason TEXT,
  resume_state TEXT,
  stall_open_threads TEXT,
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
  silent_turns INTEGER NOT NULL DEFAULT 0,
  gh_watermark INTEGER,
  verified_at INTEGER,
  classification TEXT,
  closed_issue_escalated_at INTEGER,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sessions_project
  ON sessions(project_key);


CREATE TABLE IF NOT EXISTS turns (
  turn_id TEXT PRIMARY KEY,
  session_key TEXT NOT NULL,
  role TEXT NOT NULL,
  delivery_id TEXT,
  started_at INTEGER NOT NULL,
  ended_at INTEGER,
  status TEXT,
  summary TEXT,
  public_actions TEXT,
  resume_attempts INTEGER NOT NULL DEFAULT 0,
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

CREATE TABLE IF NOT EXISTS project_blocks (
  project_key TEXT PRIMARY KEY,
  reason TEXT NOT NULL,
  session_key TEXT,
  issue_num INTEGER,
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


def node_ids_from_payload(data: dict[str, Any]) -> list[str]:
    """Stable GitHub object IDs carried on webhook envelopes (ADR-21)."""
    out: list[str] = []
    for key in ("comment", "review", "pull_request", "issue"):
        nid = (data.get(key) or {}).get("node_id")
        if nid:
            out.append(str(nid))
    return out


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
        """Create/migrate schema.

        Prefer incremental migrations that preserve the deliveries ledger
        (§12.3 idempotency). Destructive rebuild only when no path exists
        (ADR-2 worst case) — and the log must not claim a Reconciler that
        is not implemented yet (M6).
        """
        ver = self._schema_version()
        if ver == SCHEMA_VERSION:
            self._conn.executescript(SCHEMA)
            return
        if ver == 0:
            self._conn.executescript(SCHEMA)
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            log.info("schema stamped user_version=%s", SCHEMA_VERSION)
            return
        if ver > SCHEMA_VERSION:
            raise RuntimeError(
                f"state.db user_version={ver} is newer than agentd "
                f"SCHEMA_VERSION={SCHEMA_VERSION}; upgrade the binary"
            )
        # Incremental chain: preserve deliveries ledger.
        if ver == 1:
            self._migrate_v1_to_v2()
            ver = 2
        if ver == 2:
            self._migrate_v2_to_v3()
            ver = 3
        if ver == 3:
            self._migrate_v3_to_v4()
            ver = 4
        if ver == 4:
            self._migrate_v4_to_v5()
            ver = 5
        if ver == 5:
            self._migrate_v5_to_v6()
            ver = 6
        if ver == 6:
            self._migrate_v6_to_v7()
            ver = 7
        if ver == 7:
            self._migrate_v7_to_v8()
            ver = 8
        if ver == 8:
            self._migrate_v8_to_v9()
            ver = 9
        if ver == SCHEMA_VERSION:
            return
        log.warning(
            "schema user_version=%s < SCHEMA_VERSION=%s; no incremental "
            "migration path — rebuilding state tables (ADR-2 worst case). "
            "Deliveries/idempotency ledger will be wiped; re-ingest is "
            "manual (Reconciler is not implemented until M6).",
            ver,
            SCHEMA_VERSION,
        )
        self._rebuild_schema()

    def _table_columns(self, table: str) -> list[str]:
        rows = self._conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        return [str(r[1]) for r in rows]

    def _migrate_v1_to_v2(self) -> None:
        """Additive #20 migration: sessions.project_key; runners keyed by project.

        Preserves deliveries (and other tables). Runners rows are rewritten;
        multiple session-level runners for the same project collapse to one
        (last-write wins).
        """
        log.info("migrating schema v1 → v2 (preserve deliveries ledger)")
        # sessions.project_key
        sess_cols = self._table_columns("sessions")
        if sess_cols and "project_key" not in sess_cols:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN project_key TEXT NOT NULL DEFAULT ''"
            )
        if sess_cols or "sessions" in {
            r[0]
            for r in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }:
            self._conn.execute(
                """
                UPDATE sessions
                SET project_key = repo
                WHERE project_key = '' OR project_key IS NULL
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_sessions_project ON sessions(project_key)"
            )

        # runners: session_key PK → project_key PK
        runner_cols = self._table_columns("runners")
        if "session_key" in runner_cols and "project_key" not in runner_cols:
            self._conn.execute("ALTER TABLE runners RENAME TO runners_v1")
            self._conn.execute(
                """
                CREATE TABLE runners (
                  project_key TEXT PRIMARY KEY,
                  container_id TEXT,
                  endpoint TEXT,
                  token TEXT,
                  tier TEXT NOT NULL,
                  last_seen_at INTEGER
                )
                """
            )
            rows = self._conn.execute(
                """
                SELECT r.session_key, r.container_id, r.endpoint, r.token,
                       r.tier, r.last_seen_at, s.repo
                FROM runners_v1 r
                LEFT JOIN sessions s ON s.session_key = r.session_key
                """
            ).fetchall()
            for row in rows:
                sk = str(row[0] or "")
                repo = str(row[6] or "")
                if repo:
                    pk = repo
                elif "#" in sk:
                    pk = sk.partition("#")[0]
                else:
                    pk = sk
                if not pk:
                    continue
                self._conn.execute(
                    """
                    INSERT INTO runners(
                      project_key, container_id, endpoint, token, tier, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(project_key) DO UPDATE SET
                      container_id = excluded.container_id,
                      endpoint = excluded.endpoint,
                      token = excluded.token,
                      tier = excluded.tier,
                      last_seen_at = excluded.last_seen_at
                    """,
                    (
                        pk,
                        row[1],
                        row[2],
                        row[3],
                        row[4] or "cold",
                        row[5],
                    ),
                )
            self._conn.execute("DROP TABLE runners_v1")

        # Ensure full schema objects exist (IF NOT EXISTS).
        self._conn.executescript(SCHEMA)
        self._conn.execute("PRAGMA user_version = 2")
        self._conn.commit()
        log.info("schema migration v1 → v2 complete; user_version=2")

    def _migrate_v2_to_v3(self) -> None:
        """M3-A: sessions.resume_state for PAUSED_HUMAN restore (§8.5)."""
        log.info("migrating schema v2 → v3 (sessions.resume_state)")
        cols = self._table_columns("sessions")
        if cols and "resume_state" not in cols:
            self._conn.execute("ALTER TABLE sessions ADD COLUMN resume_state TEXT")
        self._conn.executescript(SCHEMA)
        self._conn.execute("PRAGMA user_version = 3")
        self._conn.commit()
        log.info("schema migration v2 → v3 complete; user_version=3")

    def _migrate_v3_to_v4(self) -> None:
        """M3-B: persist prior open review-thread ids for zero-thread delta (§9.3)."""
        log.info("migrating schema v3 → v4 (sessions.stall_open_threads)")
        cols = self._table_columns("sessions")
        if cols and "stall_open_threads" not in cols:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN stall_open_threads TEXT"
            )
        self._conn.executescript(SCHEMA)
        self._conn.execute("PRAGMA user_version = 4")
        self._conn.commit()
        log.info("schema migration v3 → v4 complete; user_version=4")

    def _migrate_v4_to_v5(self) -> None:
        """#35: project_blocks for structural ensure_session refusal."""
        log.info("migrating schema v4 → v5 (project_blocks)")
        self._conn.executescript(SCHEMA)
        self._conn.execute("PRAGMA user_version = 5")
        self._conn.commit()
        log.info("schema migration v4 → v5 complete; user_version=5")

    def _migrate_v5_to_v6(self) -> None:
        """#39: turns.public_actions + sessions.silent_turns."""
        log.info("migrating schema v5 → v6 (public_actions / silent_turns)")
        scols = self._table_columns("sessions")
        if scols and "silent_turns" not in scols:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN silent_turns INTEGER NOT NULL DEFAULT 0"
            )
        tcols = self._table_columns("turns")
        if tcols and "public_actions" not in tcols:
            self._conn.execute("ALTER TABLE turns ADD COLUMN public_actions TEXT")
        self._conn.executescript(SCHEMA)
        self._conn.execute("PRAGMA user_version = 6")
        self._conn.commit()
        log.info("schema migration v5 → v6 complete; user_version=6")

    def _migrate_v6_to_v7(self) -> None:
        """M5-2: sessions.classification (VERIFIED/ABANDONED at close)."""
        log.info("migrating schema v6 → v7 (sessions.classification)")
        cols = self._table_columns("sessions")
        if cols and "classification" not in cols:
            self._conn.execute("ALTER TABLE sessions ADD COLUMN classification TEXT")
        self._conn.executescript(SCHEMA)
        self._conn.execute("PRAGMA user_version = 7")
        self._conn.commit()
        log.info("schema migration v6 → v7 complete; user_version=7")

    def _migrate_v7_to_v8(self) -> None:
        """ADR-21: node-ID index. Preserve deliveries — backfill reads them."""
        log.info("migrating schema v7 → v8 (delivery_nodes + closed_issue_escalated_at)")
        self._conn.executescript(SCHEMA)
        scols = self._table_columns("sessions")
        if scols and "closed_issue_escalated_at" not in scols:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN closed_issue_escalated_at INTEGER"
            )
        rows = self._conn.execute(
            "SELECT delivery_id, payload, received_at FROM deliveries"
        ).fetchall()
        for row in rows:
            try:
                data = json.loads(decompress_payload(row["payload"]))
            except (zlib.error, json.JSONDecodeError, TypeError, ValueError):
                continue
            self._index_nodes_locked(
                str(row["delivery_id"]), data, int(row["received_at"] or 0)
            )
        self._conn.execute("PRAGMA user_version = 8")
        self._conn.commit()
        log.info("schema migration v7 → v8 complete; user_version=8")

    def _migrate_v8_to_v9(self) -> None:
        """ADR-22: resume_attempts on turns. Preserve deliveries."""
        log.info("migrating schema v8 → v9 (turns.resume_attempts)")
        self._conn.executescript(SCHEMA)
        tcols = self._table_columns("turns")
        if tcols and "resume_attempts" not in tcols:
            self._conn.execute(
                "ALTER TABLE turns ADD COLUMN resume_attempts INTEGER NOT NULL DEFAULT 0"
            )
        self._conn.execute("PRAGMA user_version = 9")
        self._conn.commit()
        log.info("schema migration v8 → v9 complete; user_version=9")

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
        now = int(time.time())
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
                    now,
                    blob,
                    status,
                ),
            )
            inserted = cur.rowcount == 1
            if inserted:
                try:
                    data = json.loads(payload)
                except (json.JSONDecodeError, TypeError, ValueError):
                    data = {}
                if isinstance(data, dict):
                    self._index_nodes_locked(delivery_id, data, now)
            self._conn.commit()
            return inserted

    def _index_nodes_locked(
        self, delivery_id: str, data: dict[str, Any], seen_at: int
    ) -> None:
        for nid in node_ids_from_payload(data):
            self._conn.execute(
                """
                INSERT OR IGNORE INTO delivery_nodes(node_id, delivery_id, seen_at)
                VALUES (?, ?, ?)
                """,
                (nid, delivery_id, seen_at),
            )

    def has_delivery_node(self, node_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM delivery_nodes WHERE node_id = ?",
                (node_id,),
            ).fetchone()
            return row is not None

    def list_nonterminal_sessions(self) -> list[dict[str, Any]]:
        """Sessions the sweep visits — not CLOSED, not TEARDOWN (§11.2)."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT session_key, project_key, repo, issue_num, state,
                       paused_reason, architect, developer, design_pr,
                       feature_pr, verified_at, closed_issue_escalated_at, created_at
                FROM sessions
                WHERE state NOT IN ('CLOSED', 'TEARDOWN')
                """
            ).fetchall()
            return [dict(r) for r in rows]

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
                LEFT JOIN runners r ON r.project_key = s.project_key
                WHERE s.session_key = ?
                """,
                (session_key,),
            ).fetchone()
            return dict(row) if row else None

    def get_session_by_design_pr(
        self, repo: str, pr_number: int
    ) -> dict[str, Any] | None:
        """Resolve session whose Design PR number is ``pr_number`` (M3-D NB).

        PR-keyed webhooks (issue_comment on a PR, review comments) store the PR
        number where issue_num is expected; look up by ``sessions.design_pr``.
        """
        with self._lock:
            row = self._conn.execute(
                """
                SELECT s.*, r.container_id, r.endpoint, r.token AS runner_token, r.tier
                FROM sessions s
                LEFT JOIN runners r ON r.project_key = s.project_key
                WHERE s.repo = ? AND s.design_pr = ?
                ORDER BY s.updated_at DESC
                LIMIT 1
                """,
                (repo, int(pr_number)),
            ).fetchone()
            return dict(row) if row else None

    def get_session_by_feature_pr(
        self, repo: str, pr_number: int
    ) -> dict[str, Any] | None:
        """Resolve session whose Feature PR number is ``pr_number`` (M4-1)."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT s.*, r.container_id, r.endpoint, r.token AS runner_token, r.tier
                FROM sessions s
                LEFT JOIN runners r ON r.project_key = s.project_key
                WHERE s.repo = ? AND s.feature_pr = ?
                ORDER BY s.updated_at DESC
                LIMIT 1
                """,
                (repo, int(pr_number)),
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
        project_key: str | None = None,
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
        """Insert a session row. On conflict, do not write state, roles,
        roles_locked, or loop-safety counters (#72). INSERT still binds 0
        for those so NOT NULL DEFAULT columns work; UPDATE must not see
        those 0s."""
        pk = project_key or repo
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO sessions(
                  session_key, project_key, repo, issue_num, state, paused_reason,
                  architect, developer, roles_locked, design_pr,
                  turn_count, consec_agent_turns, review_rounds,
                  progress_fp, progress_repeat, zero_thread_rounds,
                  created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_key) DO UPDATE SET
                  project_key = excluded.project_key,
                  paused_reason = COALESCE(excluded.paused_reason, sessions.paused_reason),
                  design_pr = COALESCE(excluded.design_pr, sessions.design_pr),
                  progress_fp = COALESCE(excluded.progress_fp, sessions.progress_fp),
                  updated_at = excluded.updated_at
                """,
                (
                    session_key,
                    pk,
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
            "resume_state",
            "roles_locked",
            "design_pr",
            "feature_pr",
            "turn_count",
            "consec_agent_turns",
            "review_rounds",
            "progress_fp",
            "progress_repeat",
            "zero_thread_rounds",
            "silent_turns",
            "stall_open_threads",
            "verified_at",  # §10.2 checkbox record (M5-1)
            "classification",  # §10.3 VERIFIED/ABANDONED (M5-2)
            "closed_issue_escalated_at",  # ADR-21: one escalate per closed-issue episode
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

    def bump_turn_counters(
        self,
        session_key: str,
        *,
        silent: str,
        restart_consec: bool = False,
    ) -> dict[str, int]:
        """ADR-32 (b): move all three turn counters in **one** SQL statement.

        The old shape read ``sessions`` before dispatch and wrote the counters
        back after, a read-modify-write spanning the whole turn. Returns the
        post-update values, which are the only ones a caller may act on.

        ``silent`` is ``"inc"`` / ``"reset"`` / ``"keep"`` (see
        :func:`agentd.loop_safety.silent_turn_mode`); ``restart_consec`` is the
        owner-unpause path, where the counter restarts at this turn.
        """
        if silent not in ("inc", "reset", "keep"):
            raise ValueError(f"bad silent mode {silent!r}")
        with self._lock:
            row = self._conn.execute(
                """
                UPDATE sessions SET
                  turn_count = turn_count + 1,
                  consec_agent_turns =
                    CASE WHEN ? THEN 1 ELSE consec_agent_turns + 1 END,
                  silent_turns = CASE ?
                    WHEN 'reset' THEN 0
                    WHEN 'inc' THEN silent_turns + 1
                    ELSE silent_turns END,
                  updated_at = ?
                WHERE session_key = ?
                RETURNING turn_count, consec_agent_turns, silent_turns
                """,
                (1 if restart_consec else 0, silent, int(time.time()), session_key),
            ).fetchone()
            self._conn.commit()
        if row is None:
            raise KeyError(session_key)
        return {
            "turn_count": int(row["turn_count"]),
            "consec_agent_turns": int(row["consec_agent_turns"]),
            "silent_turns": int(row["silent_turns"]),
        }

    def deliveries_in_window(
        self,
        *,
        repo: str,
        after: int,
        through: int,
    ) -> list[dict[str, Any]]:
        """Deliveries received inside a turn's window (ADR-32 (a)).

        Bounds are **strict below, inclusive above**: ``received_at`` and
        ``turns.started_at`` are both whole seconds and collide in practice, so
        a ``>=`` here would let the triggering delivery observe its own turn.
        """
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT delivery_id, event, action, repo, issue_num, sender,
                       received_at, payload
                FROM deliveries
                WHERE repo = ? AND received_at > ? AND received_at <= ?
                ORDER BY received_at ASC
                """,
                (repo, int(after), int(through)),
            ).fetchall()
            return [dict(r) for r in rows]

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

    def count_deferred(self, *, before_received_at: int | None = None) -> int:
        """Count deferred deliveries (optional upper bound on received_at)."""
        with self._lock:
            if before_received_at is not None:
                row = self._conn.execute(
                    """
                    SELECT COUNT(*) AS n FROM deliveries
                    WHERE status = 'deferred' AND received_at < ?
                    """,
                    (before_received_at,),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM deliveries WHERE status = 'deferred'"
                ).fetchone()
            return int(row["n"]) if row else 0

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
        """Count HOT project runners (§6.6 — admission unit is the project)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM runners WHERE tier = 'hot'"
            ).fetchone()
            return int(row["n"]) if row else 0

    def count_project_sessions(self, project_key: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM sessions WHERE project_key = ?",
                (project_key,),
            ).fetchone()
            return int(row["n"]) if row else 0

    def count_turns(self, session_key: str) -> int:
        """All turn rows for this session, including teardown.

        Distinct from ``sessions.turn_count``, which §9.2 increments only
        for budgeted (non-teardown) turns. The archive manifest uses this.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM turns WHERE session_key=?",
                (session_key,),
            ).fetchone()
        return int(row["n"] if row else 0)

    def list_deliveries_for(
        self, repo: str, issue_nums: list[int]
    ) -> list[dict[str, Any]]:
        """Deliveries whose issue_num is in ``issue_nums`` (ADR-14 membership).

        ``issue_nums`` must already omit NULL — SQLite ``IN`` does not match
        NULL list members. Empty list → no rows (do not emit ``IN ()``).
        """
        nums = [int(n) for n in issue_nums]
        if not nums:
            return []
        placeholders = ",".join("?" * len(nums))
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT delivery_id, event, action, repo, issue_num, sender,
                       received_at, payload, status
                FROM deliveries
                WHERE repo = ? AND issue_num IN ({placeholders})
                ORDER BY received_at ASC
                """,
                (repo, *nums),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_turns(self, session_key: str) -> list[dict[str, Any]]:
        """All turn rows for ``session_key``, oldest first (ADR-14 totals)."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT turn_id, session_key, role, delivery_id,
                       started_at, ended_at, status, summary, public_actions
                FROM turns
                WHERE session_key = ?
                ORDER BY started_at ASC
                """,
                (session_key,),
            ).fetchall()
            return [dict(r) for r in rows]

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

    def get_turn(self, turn_id: str) -> dict[str, Any] | None:
        """One turn row, or None. ADR-32 (a) reads its ``[started_at, ended_at]``."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT turn_id, session_key, role, delivery_id,
                       started_at, ended_at, status, summary, public_actions
                FROM turns WHERE turn_id = ?
                """,
                (turn_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def finish_turn(
        self,
        turn_id: str,
        *,
        ended_at: int,
        status: str,
        summary: str | None,
        public_actions: str | None = None,
    ) -> None:
        """Mark a turn row complete (success, failed, or gateway_timeout)."""
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE turns
                SET ended_at=?, status=?, summary=?, public_actions=COALESCE(?, public_actions)
                WHERE turn_id=? AND ended_at IS NULL
                """,
                (ended_at, status, summary, public_actions, turn_id),
            )
            if cur.rowcount == 0:
                log.warning(
                    "finish_turn ignored turn=%s status=%s (already ended)",
                    turn_id,
                    status,
                )
            self._conn.commit()

    def mark_turn_resuming(self, turn_id: str) -> int:
        """Set status=resuming and increment resume_attempts. Returns the new count."""
        with self._lock:
            self._conn.execute(
                """
                UPDATE turns
                SET status = 'resuming',
                    resume_attempts = COALESCE(resume_attempts, 0) + 1
                WHERE turn_id = ? AND ended_at IS NULL
                """,
                (turn_id,),
            )
            row = self._conn.execute(
                "SELECT resume_attempts FROM turns WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
            self._conn.commit()
            return int(row["resume_attempts"] if row else 0)

    def clear_turn_resuming(self, turn_id: str) -> None:
        """Clear in-flight mark after a failed resume so the next pass can retry."""
        with self._lock:
            self._conn.execute(
                """
                UPDATE turns SET status = NULL
                WHERE turn_id = ? AND ended_at IS NULL AND status = 'resuming'
                """,
                (turn_id,),
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
    ) -> int:
        """Record a cleanup-ledger row (FR-4.3). Idempotent for open rows.

        Returns artifact id (existing open row or newly inserted).
        """
        kind_s = str(kind or "scratch")
        ref_s = str(ref or "").strip()
        if not session_key or not ref_s:
            raise ValueError("session_key and ref required for artifact.register")
        role_s = str(role or "system")
        with self._lock:
            existing = self._conn.execute(
                """
                SELECT id FROM artifacts
                WHERE session_key = ? AND kind = ? AND ref = ? AND removed_at IS NULL
                LIMIT 1
                """,
                (session_key, kind_s, ref_s),
            ).fetchone()
            if existing:
                return int(existing["id"])
            cur = self._conn.execute(
                """
                INSERT INTO artifacts(session_key, role, kind, ref, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_key, role_s, kind_s, ref_s, created_at or int(time.time())),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def list_artifacts(
        self,
        session_key: str,
        *,
        open_only: bool = True,
    ) -> list[dict[str, Any]]:
        with self._lock:
            if open_only:
                rows = self._conn.execute(
                    """
                    SELECT id, session_key, role, kind, ref, created_at, removed_at
                    FROM artifacts
                    WHERE session_key = ? AND removed_at IS NULL
                    ORDER BY created_at ASC, id ASC
                    """,
                    (session_key,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT id, session_key, role, kind, ref, created_at, removed_at
                    FROM artifacts
                    WHERE session_key = ?
                    ORDER BY created_at ASC, id ASC
                    """,
                    (session_key,),
                ).fetchall()
            return [dict(r) for r in rows]

    def mark_artifact_removed(
        self,
        *,
        session_key: str,
        ref: str,
        kind: str | None = None,
        removed_at: int | None = None,
    ) -> int:
        """Set removed_at on matching open rows. Returns rows updated."""
        ts = removed_at if removed_at is not None else int(time.time())
        with self._lock:
            if kind:
                cur = self._conn.execute(
                    """
                    UPDATE artifacts SET removed_at = ?
                    WHERE session_key = ? AND ref = ? AND kind = ?
                      AND removed_at IS NULL
                    """,
                    (ts, session_key, ref, kind),
                )
            else:
                cur = self._conn.execute(
                    """
                    UPDATE artifacts SET removed_at = ?
                    WHERE session_key = ? AND ref = ? AND removed_at IS NULL
                    """,
                    (ts, session_key, ref),
                )
            self._conn.commit()
            return int(cur.rowcount or 0)

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

    def get_open_escalation(self, session_key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT id, session_key, role, reason, comment_id, opened_at, resolved_at
                FROM escalations
                WHERE session_key = ? AND resolved_at IS NULL
                ORDER BY opened_at DESC
                LIMIT 1
                """,
                (session_key,),
            ).fetchone()
            return dict(row) if row else None

    def close_escalation(
        self,
        session_key: str,
        *,
        escalation_id: int | None = None,
        resolved_at: int | None = None,
    ) -> int:
        """Mark open escalation(s) resolved. Returns rows updated."""
        ts = resolved_at if resolved_at is not None else int(time.time())
        with self._lock:
            if escalation_id is not None:
                cur = self._conn.execute(
                    """
                    UPDATE escalations SET resolved_at = ?
                    WHERE id = ? AND resolved_at IS NULL
                    """,
                    (ts, escalation_id),
                )
            else:
                cur = self._conn.execute(
                    """
                    UPDATE escalations SET resolved_at = ?
                    WHERE session_key = ? AND resolved_at IS NULL
                    """,
                    (ts, session_key),
                )
            self._conn.commit()
            return int(cur.rowcount or 0)

    def list_runners(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM runners").fetchall()
            return [dict(r) for r in rows]

    def delete_runner(self, project_key: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM runners WHERE project_key = ?",
                (project_key,),
            )
            self._conn.commit()
            return int(cur.rowcount or 0)

    def live_project_keys(self) -> set[str]:
        """Projects with at least one session not CLOSED. TEARDOWN is live."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT project_key FROM sessions WHERE state != 'CLOSED'"
            ).fetchall()
            return {str(r["project_key"]) for r in rows if r["project_key"]}

    def projects_with_open_turns(self, *, now: int, max_age_s: int) -> set[str]:
        """Projects with a turn still inside the turn-timeout window."""
        cutoff = now - max_age_s
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT DISTINCT s.project_key
                FROM turns t
                JOIN sessions s ON s.session_key = t.session_key
                WHERE t.ended_at IS NULL
                  AND t.started_at IS NOT NULL
                  AND t.started_at > ?
                """,
                (cutoff,),
            ).fetchall()
            return {str(r["project_key"]) for r in rows if r["project_key"]}

    def list_open_turns(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT t.turn_id, t.session_key, t.role, t.status, t.started_at,
                       t.resume_attempts, t.delivery_id, s.state AS session_state
                FROM turns t
                LEFT JOIN sessions s ON s.session_key = t.session_key
                WHERE t.ended_at IS NULL
                ORDER BY t.started_at ASC
                """
            ).fetchall()
            return [dict(r) for r in rows]

    def list_resuming_turns(self) -> list[dict[str, Any]]:
        """Open turns handed to the drain thread (status=resuming)."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT t.turn_id, t.session_key, t.role, t.status, t.started_at,
                       t.resume_attempts, t.delivery_id, s.state AS session_state
                FROM turns t
                LEFT JOIN sessions s ON s.session_key = t.session_key
                WHERE t.ended_at IS NULL AND t.status = 'resuming'
                ORDER BY t.started_at ASC
                """
            ).fetchall()
            return [dict(r) for r in rows]

    def list_live_sessions_with_closed_delivery(self) -> list[dict[str, Any]]:
        """Live sessions whose ledger has a routed issues.closed (local record)."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT s.session_key, s.state, s.issue_num, d.delivery_id
                FROM sessions s
                JOIN deliveries d
                  ON d.repo = s.repo AND d.issue_num = s.issue_num
                 AND d.event = 'issues' AND d.action = 'closed'
                 AND d.status = 'routed'
                WHERE s.state != 'CLOSED'
                ORDER BY s.session_key
                """
            ).fetchall()
            return [dict(r) for r in rows]

    def count_open_artifacts(self, session_key: str) -> int:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS n FROM artifacts
                WHERE session_key = ? AND removed_at IS NULL
                """,
                (session_key,),
            ).fetchone()
            return int(row["n"]) if row else 0

    def get_runner(self, project_key: str) -> dict[str, Any] | None:
        """Look up the project runner. ``project_key`` is ``owner/repo``."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM runners WHERE project_key = ?",
                (project_key,),
            ).fetchone()
            return dict(row) if row else None

    def get_runner_for_session(self, session_key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT r.* FROM runners r
                JOIN sessions s ON s.project_key = r.project_key
                WHERE s.session_key = ?
                """,
                (session_key,),
            ).fetchone()
            return dict(row) if row else None

    def upsert_runner(
        self,
        project_key: str,
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
                  project_key, container_id, endpoint, token, tier, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_key) DO UPDATE SET
                  container_id = excluded.container_id,
                  endpoint = excluded.endpoint,
                  token = excluded.token,
                  tier = excluded.tier,
                  last_seen_at = excluded.last_seen_at
                """,
                (project_key, container_id, endpoint, token, tier, now),
            )
            self._conn.commit()

    def touch_runner_seen(self, project_key: str, *, now: int | None = None) -> int:
        """Stamp `last_seen_at` alone (ADR-34 step 7).

        Deliberately not `upsert_runner`: that rewrites `tier`, `endpoint` and
        `token` from whatever the caller happens to hold, and the reconciler
        holds a row it read at the top of the pass. A probe must never be able
        to move `tier`.
        """
        ts = int(time.time()) if now is None else int(now)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE runners SET last_seen_at = ? WHERE project_key = ?",
                (ts, project_key),
            )
            self._conn.commit()
            return int(cur.rowcount or 0)

    def open_project_block(
        self,
        *,
        project_key: str,
        reason: str,
        session_key: str | None,
        issue_num: int | None,
        comment_id: int | None,
    ) -> None:
        """Record a structural project wedge (#35). Idempotent while open."""
        now = int(time.time())
        with self._lock:
            existing = self._conn.execute(
                """
                SELECT project_key FROM project_blocks
                WHERE project_key = ? AND resolved_at IS NULL
                """,
                (project_key,),
            ).fetchone()
            if existing:
                return
            self._conn.execute(
                """
                INSERT INTO project_blocks(
                  project_key, reason, session_key, issue_num, comment_id, opened_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_key) DO UPDATE SET
                  reason = excluded.reason,
                  session_key = excluded.session_key,
                  issue_num = excluded.issue_num,
                  comment_id = excluded.comment_id,
                  opened_at = excluded.opened_at,
                  resolved_at = NULL
                """,
                (
                    project_key,
                    reason,
                    session_key,
                    issue_num,
                    comment_id,
                    now,
                ),
            )
            self._conn.commit()

    def get_open_project_block(self, project_key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM project_blocks
                WHERE project_key = ? AND resolved_at IS NULL
                """,
                (project_key,),
            ).fetchone()
            return dict(row) if row else None

    def close_project_block(self, project_key: str) -> int:
        """Resolve open project block. Returns rows updated."""
        now = int(time.time())
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE project_blocks SET resolved_at = ?
                WHERE project_key = ? AND resolved_at IS NULL
                """,
                (now, project_key),
            )
            self._conn.commit()
            return int(cur.rowcount or 0)

    def list_open_project_blocks(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM project_blocks
                WHERE resolved_at IS NULL
                ORDER BY opened_at ASC
                """
            ).fetchall()
            return [dict(r) for r in rows]

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
            blocks = self._conn.execute(
                """
                SELECT project_key, reason, session_key, issue_num, comment_id, opened_at
                FROM project_blocks WHERE resolved_at IS NULL
                ORDER BY opened_at ASC
                """
            ).fetchall()
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
                "project_blocks": [dict(r) for r in blocks],
            }
