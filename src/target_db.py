"""
DuckDB interface: schema initialization, event insertion, and audit helpers.
"""
import json
import logging
import uuid
from pathlib import Path
from typing import Any

import duckdb

from config.settings import settings

logger = logging.getLogger(__name__)

_DDL_EVENTS = """
CREATE TABLE IF NOT EXISTS events (
    event_id    VARCHAR   PRIMARY KEY,
    user_id     INTEGER   NOT NULL,
    amount      DOUBLE    NOT NULL,
    timestamp   TIMESTAMP NOT NULL,
    event_type  VARCHAR   NOT NULL,
    ingested_at TIMESTAMP DEFAULT current_timestamp
);
"""

_DDL_FAILED = """
CREATE TABLE IF NOT EXISTS failed_messages (
    id          VARCHAR   PRIMARY KEY,
    raw_payload JSON      NOT NULL,
    received_at TIMESTAMP DEFAULT current_timestamp,
    status      VARCHAR   DEFAULT 'pending'
);
"""

_DDL_AUDIT = """
CREATE TABLE IF NOT EXISTS healing_audit (
    id                  VARCHAR   PRIMARY KEY,
    failed_message_id   VARCHAR   NOT NULL,
    event_id            VARCHAR,
    original_payload    JSON,
    healed_payload      JSON,
    transformation_code TEXT,
    healed_at           TIMESTAMP DEFAULT current_timestamp,
    status              VARCHAR
);
"""


class DatabaseManager:
    def __init__(self, db_path: str | None = None, read_only: bool = False) -> None:
        self._path = db_path or settings.duckdb_path
        self._read_only = read_only
        self._conn: duckdb.DuckDBPyConnection | None = None

    def initialize(self) -> None:
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = duckdb.connect(self._path, read_only=self._read_only)
        if not self._read_only:
            self._conn.execute(_DDL_EVENTS)
            self._conn.execute(_DDL_FAILED)
            self._conn.execute(_DDL_AUDIT)
        logger.info("DuckDB initialized at %s (read_only=%s)", self._path, self._read_only)

    @property
    def conn(self) -> duckdb.DuckDBPyConnection:
        assert self._conn is not None, "DatabaseManager.initialize() must be called first."
        return self._conn

    # ── Event insertion ───────────────────────────────────────────────────────

    def insert_event(self, payload: dict[str, Any]) -> None:
        """Insert a schema-compliant event record. Raises on type mismatch."""
        self.conn.execute(
            """
            INSERT INTO events (event_id, user_id, amount, timestamp, event_type)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                str(payload["event_id"]),
                int(payload["user_id"]),          # raises ValueError for non-int strings
                float(payload["amount"]),          # raises for missing/non-numeric
                str(payload["timestamp"]),
                str(payload["event_type"]),
            ],
        )

    # ── Failed message store ──────────────────────────────────────────────────

    def store_failed_message(self, payload: dict[str, Any]) -> str:
        msg_id = str(uuid.uuid4())
        self.conn.execute(
            "INSERT INTO failed_messages (id, raw_payload) VALUES (?, ?)",
            [msg_id, json.dumps(payload)],
        )
        # Write a JSON sidecar so the MCP subprocess can read the payload
        # without opening DuckDB (avoids cross-process write-lock conflict).
        sidecar_dir = Path(self._path).parent / "failed_payloads"
        sidecar_dir.mkdir(parents=True, exist_ok=True)
        (sidecar_dir / f"{msg_id}.json").write_text(json.dumps(payload))
        logger.info("Stored failed message id=%s", msg_id)
        return msg_id

    def get_failed_message(self, message_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT raw_payload FROM failed_messages WHERE id = ?", [message_id]
        ).fetchone()
        return json.loads(row[0]) if row else None

    def mark_failed_message_healed(self, message_id: str) -> None:
        self.conn.execute(
            "UPDATE failed_messages SET status = 'healed' WHERE id = ?", [message_id]
        )

    # ── Schema introspection ──────────────────────────────────────────────────

    def get_table_schema(self, table_name: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(f"PRAGMA table_info('{table_name}')").fetchall()
        return [
            {
                "cid": r[0], "name": r[1], "type": r[2],
                "notnull": bool(r[3]), "default": r[4], "pk": bool(r[5]),
            }
            for r in rows
        ]

    # ── Audit log ─────────────────────────────────────────────────────────────

    def write_audit(
        self,
        failed_message_id: str,
        event_id: str,
        original: dict[str, Any],
        healed: dict[str, Any],
        code: str,
        status: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO healing_audit
              (id, failed_message_id, event_id, original_payload,
               healed_payload, transformation_code, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                str(uuid.uuid4()),
                failed_message_id,
                event_id,
                json.dumps(original),
                json.dumps(healed),
                code,
                status,
            ],
        )

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            logger.info("DuckDB connection closed.")
