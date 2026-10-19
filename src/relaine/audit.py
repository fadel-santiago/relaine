"""Append-only SQLite audit history for proposal lifecycle events."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from relaine.schemas import AuditEvent, AuditEventType


class AuditStoreError(RuntimeError):
    """A database or persisted-data failure in the audit ledger."""


def initialize_audit_schema(connection: sqlite3.Connection) -> None:
    """Create the audit ledger and database-enforced append-only guards."""

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            proposal_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            event_type TEXT NOT NULL CHECK (
                event_type IN (
                    'proposal_submitted', 'proposal_approved',
                    'proposal_rejected', 'execution_claimed',
                    'execution_succeeded', 'execution_failed',
                    'recovery_attempted', 'recovery_completed'
                )
            ),
            metadata_json TEXT NOT NULL CHECK (
                json_valid(metadata_json)
                AND json_type(metadata_json) = 'object'
            )
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS audit_events_proposal_order
        ON audit_events (proposal_id, event_id)
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS audit_event_update_forbidden
        BEFORE UPDATE ON audit_events
        BEGIN
            SELECT RAISE(ABORT, 'audit event is immutable');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS audit_event_delete_forbidden
        BEFORE DELETE ON audit_events
        BEGIN
            SELECT RAISE(ABORT, 'audit event is immutable');
        END
        """
    )


def append_audit_event(
    connection: sqlite3.Connection,
    proposal_id: str,
    event_type: AuditEventType,
    metadata: dict[str, Any],
    *,
    timestamp: str | None = None,
) -> int:
    """Append an event in the caller's transaction without committing it."""

    serialized = json.dumps(
        metadata,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    cursor = connection.execute(
        """
        INSERT INTO audit_events (
            proposal_id, timestamp, event_type, metadata_json
        ) VALUES (?, ?, ?, ?)
        """,
        (
            proposal_id,
            timestamp or datetime.now(UTC).isoformat(),
            event_type.value,
            serialized,
        ),
    )
    return int(cursor.lastrowid)


class AuditStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._initialize_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize_schema(self) -> None:
        try:
            with closing(self._connect()) as connection, connection:
                initialize_audit_schema(connection)
        except sqlite3.Error as exc:
            raise AuditStoreError(f"could not initialize audit database: {exc}") from exc

    def record_recovery_attempt(
        self,
        proposal_id: str,
        action: str,
        receipt_present: bool,
    ) -> None:
        try:
            with closing(self._connect()) as connection, connection:
                append_audit_event(
                    connection,
                    proposal_id,
                    AuditEventType.RECOVERY_ATTEMPTED,
                    {
                        "action": action,
                        "receipt_present": receipt_present,
                    },
                )
        except sqlite3.Error as exc:
            raise AuditStoreError(f"could not append recovery audit event: {exc}") from exc

    def history(self, proposal_id: str) -> list[AuditEvent]:
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    """
                    SELECT event_id, proposal_id, timestamp, event_type, metadata_json
                    FROM audit_events
                    WHERE proposal_id = ?
                    ORDER BY event_id
                    """,
                    (proposal_id,),
                ).fetchall()
            return [
                AuditEvent(
                    event_id=row["event_id"],
                    proposal_id=row["proposal_id"],
                    timestamp=row["timestamp"],
                    event_type=row["event_type"],
                    metadata=json.loads(row["metadata_json"]),
                )
                for row in rows
            ]
        except (sqlite3.Error, json.JSONDecodeError, ValidationError) as exc:
            raise AuditStoreError(f"could not retrieve audit history: {exc}") from exc
