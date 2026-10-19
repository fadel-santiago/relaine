"""Persistent immutable proposal ledger for controlled execution."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from relaine.audit import append_audit_event, initialize_audit_schema
from relaine.schemas import (
    AuditEventType,
    ApprovalError,
    CreateTaskProposal,
    ExecutionProtocol,
    Proposal,
    ProposalRecord,
    ProposalStatus,
    TaskToolResult,
    UpdateTaskProposal,
)


PROPOSAL_ADAPTER = TypeAdapter(Proposal)


class ApprovalStoreError(RuntimeError):
    """A database or persisted-data failure in the proposal ledger."""


class ProposalNotFoundError(LookupError):
    pass


class FingerprintMismatchError(ValueError):
    pass


class ProposalModifiedError(ValueError):
    pass


class ProposalStateConflictError(RuntimeError):
    def __init__(self, status: ProposalStatus) -> None:
        self.status = status
        super().__init__(f"proposal is already {status.value}")


def proposal_fingerprint(proposal: CreateTaskProposal | UpdateTaskProposal) -> str:
    snapshot = {
        "action": proposal.action.value,
        "parameters": proposal.parameters.model_dump(mode="json"),
    }
    canonical = json.dumps(
        snapshot,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class ProposalStore:
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
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS proposals (
                        proposal_id TEXT PRIMARY KEY,
                        request TEXT NOT NULL,
                        proposal_json TEXT NOT NULL,
                        fingerprint TEXT NOT NULL,
                        status TEXT NOT NULL CHECK (
                            status IN (
                                'pending', 'executing', 'completed',
                                'rejected', 'failed'
                            )
                        ),
                        execution_protocol TEXT CHECK (
                            execution_protocol IS NULL
                            OR execution_protocol = 'receipt_v1'
                        ),
                        result_json TEXT,
                        failure_json TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                initialize_audit_schema(connection)
                columns = {
                    row["name"]
                    for row in connection.execute("PRAGMA table_info(proposals)")
                }
                if "execution_protocol" not in columns:
                    connection.execute(
                        """
                        ALTER TABLE proposals
                        ADD COLUMN execution_protocol TEXT CHECK (
                            execution_protocol IS NULL
                            OR execution_protocol = 'receipt_v1'
                        )
                        """
                    )
                connection.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS proposal_snapshot_immutable
                    BEFORE UPDATE OF proposal_id, proposal_json, fingerprint
                    ON proposals
                    BEGIN
                        SELECT RAISE(ABORT, 'proposal snapshot is immutable');
                    END
                    """
                )
                connection.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS execution_protocol_transition_guard
                    BEFORE UPDATE OF execution_protocol ON proposals
                    WHEN NOT (
                        OLD.status = 'pending'
                        AND OLD.execution_protocol IS NULL
                        AND NEW.status = 'executing'
                        AND NEW.execution_protocol = 'receipt_v1'
                    )
                    BEGIN
                        SELECT RAISE(ABORT, 'invalid execution protocol transition');
                    END
                    """
                )
        except sqlite3.Error as exc:
            raise ApprovalStoreError(
                f"could not initialize proposal database: {exc}"
            ) from exc

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _serialize(model: object) -> str:
        if hasattr(model, "model_dump"):
            value = model.model_dump(mode="json")  # type: ignore[union-attr]
        else:
            value = model
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> ProposalRecord:
        proposal = PROPOSAL_ADAPTER.validate_json(row["proposal_json"])
        result = (
            None
            if row["result_json"] is None
            else TaskToolResult.model_validate_json(row["result_json"])
        )
        failure = (
            None
            if row["failure_json"] is None
            else ApprovalError.model_validate_json(row["failure_json"])
        )
        return ProposalRecord(
            proposal_id=row["proposal_id"],
            request=row["request"],
            proposal=proposal,
            fingerprint=row["fingerprint"],
            status=row["status"],
            execution_protocol=row["execution_protocol"],
            result=result,
            failure=failure,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _verify_record(record: ProposalRecord) -> ProposalRecord:
        if proposal_fingerprint(record.proposal) != record.fingerprint:
            raise ProposalModifiedError(
                f"proposal {record.proposal_id} no longer matches its fingerprint"
            )
        return record

    def save_pending(
        self,
        request: str,
        proposal: CreateTaskProposal | UpdateTaskProposal,
    ) -> ProposalRecord:
        proposal_id = f"PROP-{uuid4().hex.upper()}"
        fingerprint = proposal_fingerprint(proposal)
        timestamp = self._timestamp()
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    """
                    INSERT INTO proposals (
                        proposal_id, request, proposal_json, fingerprint, status,
                        execution_protocol, result_json, failure_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)
                    """,
                    (
                        proposal_id,
                        request,
                        self._serialize(proposal),
                        fingerprint,
                        ProposalStatus.PENDING.value,
                        timestamp,
                        timestamp,
                    ),
                )
                append_audit_event(
                    connection,
                    proposal_id,
                    AuditEventType.PROPOSAL_SUBMITTED,
                    {
                        "action": proposal.action.value,
                        "fingerprint": fingerprint,
                    },
                    timestamp=timestamp,
                )
        except sqlite3.Error as exc:
            raise ApprovalStoreError(f"could not save proposal: {exc}") from exc
        return self.get(proposal_id)

    def _select(self, connection: sqlite3.Connection, proposal_id: str) -> ProposalRecord:
        row = connection.execute(
            "SELECT * FROM proposals WHERE proposal_id = ?", (proposal_id,)
        ).fetchone()
        if row is None:
            raise ProposalNotFoundError(proposal_id)
        try:
            return self._verify_record(self._row_to_record(row))
        except (ValidationError, ValueError) as exc:
            if isinstance(exc, ProposalModifiedError):
                raise
            raise ApprovalStoreError(f"invalid persisted proposal: {exc}") from exc

    def get(self, proposal_id: str) -> ProposalRecord:
        try:
            with closing(self._connect()) as connection:
                return self._select(connection, proposal_id)
        except (ProposalNotFoundError, ProposalModifiedError):
            raise
        except sqlite3.Error as exc:
            raise ApprovalStoreError(f"could not retrieve proposal: {exc}") from exc

    def list_executing_rows(self) -> list[dict[str, object]]:
        """Return raw executing rows for read-only integrity classification."""

        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    """
                    SELECT
                        p.proposal_id,
                        p.proposal_json,
                        p.fingerprint,
                        p.status,
                        p.execution_protocol,
                        p.result_json AS proposal_result_json,
                        p.failure_json AS proposal_failure_json,
                        p.updated_at AS proposal_updated_at,
                        r.proposal_id AS receipt_proposal_id,
                        r.fingerprint AS receipt_fingerprint,
                        r.result_json AS receipt_result_json,
                        r.committed_at AS receipt_committed_at
                    FROM proposals AS p
                    LEFT JOIN execution_receipts AS r
                        ON r.proposal_id = p.proposal_id
                    WHERE p.status = ?
                    ORDER BY p.updated_at, p.proposal_id
                    """,
                    (ProposalStatus.EXECUTING.value,),
                ).fetchall()
                return [dict(row) for row in rows]
        except sqlite3.Error as exc:
            raise ApprovalStoreError(
                f"could not list executing proposals: {exc}"
            ) from exc

    @staticmethod
    def _validate_reference(
        record: ProposalRecord,
        fingerprint: str,
    ) -> None:
        if record.fingerprint != fingerprint:
            raise FingerprintMismatchError(record.proposal_id)
        if record.status is not ProposalStatus.PENDING:
            raise ProposalStateConflictError(record.status)

    def validate_pending_reference(
        self,
        proposal_id: str,
        fingerprint: str,
    ) -> ProposalRecord:
        record = self.get(proposal_id)
        self._validate_reference(record, fingerprint)
        return record

    def _begin_immediate(self, connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")

    def claim_for_execution(
        self,
        proposal_id: str,
        fingerprint: str,
    ) -> ProposalRecord:
        connection = self._connect()
        try:
            self._begin_immediate(connection)
            record = self._select(connection, proposal_id)
            self._validate_reference(record, fingerprint)
            timestamp = self._timestamp()
            cursor = connection.execute(
                """
                UPDATE proposals
                SET status = ?, execution_protocol = ?, updated_at = ?
                WHERE proposal_id = ? AND fingerprint = ? AND status = ?
                """,
                (
                    ProposalStatus.EXECUTING.value,
                    ExecutionProtocol.RECEIPT_V1.value,
                    timestamp,
                    proposal_id,
                    fingerprint,
                    ProposalStatus.PENDING.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ProposalStateConflictError(record.status)
            append_audit_event(
                connection,
                proposal_id,
                AuditEventType.PROPOSAL_APPROVED,
                {
                    "action": record.proposal.action.value,
                    "fingerprint": record.fingerprint,
                },
                timestamp=timestamp,
            )
            append_audit_event(
                connection,
                proposal_id,
                AuditEventType.EXECUTION_CLAIMED,
                {
                    "action": record.proposal.action.value,
                    "execution_protocol": ExecutionProtocol.RECEIPT_V1.value,
                },
                timestamp=timestamp,
            )
            connection.commit()
            return self.get(proposal_id)
        except (
            ProposalNotFoundError,
            ProposalModifiedError,
            FingerprintMismatchError,
            ProposalStateConflictError,
        ):
            connection.rollback()
            raise
        except sqlite3.Error as exc:
            connection.rollback()
            raise ApprovalStoreError(f"could not claim proposal: {exc}") from exc
        finally:
            connection.close()

    def reject(self, proposal_id: str, fingerprint: str) -> ProposalRecord:
        connection = self._connect()
        try:
            self._begin_immediate(connection)
            record = self._select(connection, proposal_id)
            self._validate_reference(record, fingerprint)
            cursor = connection.execute(
                """
                UPDATE proposals
                SET status = ?, updated_at = ?
                WHERE proposal_id = ? AND fingerprint = ? AND status = ?
                """,
                (
                    ProposalStatus.REJECTED.value,
                    self._timestamp(),
                    proposal_id,
                    fingerprint,
                    ProposalStatus.PENDING.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ProposalStateConflictError(record.status)
            append_audit_event(
                connection,
                proposal_id,
                AuditEventType.PROPOSAL_REJECTED,
                {
                    "action": record.proposal.action.value,
                    "fingerprint": record.fingerprint,
                },
            )
            connection.commit()
            return self.get(proposal_id)
        except (
            ProposalNotFoundError,
            ProposalModifiedError,
            FingerprintMismatchError,
            ProposalStateConflictError,
        ):
            connection.rollback()
            raise
        except sqlite3.Error as exc:
            connection.rollback()
            raise ApprovalStoreError(f"could not reject proposal: {exc}") from exc
        finally:
            connection.close()

    def finish_execution(
        self,
        proposal_id: str,
        result: TaskToolResult,
        *,
        recovery: bool = False,
    ) -> ProposalRecord:
        status = ProposalStatus.COMPLETED if result.success else ProposalStatus.FAILED
        try:
            with closing(self._connect()) as connection, connection:
                record = self._select(connection, proposal_id)
                cursor = connection.execute(
                    """
                    UPDATE proposals
                    SET status = ?, result_json = ?, updated_at = ?
                    WHERE proposal_id = ? AND status = ?
                    """,
                    (
                        status.value,
                        self._serialize(result),
                        self._timestamp(),
                        proposal_id,
                        ProposalStatus.EXECUTING.value,
                    ),
                )
                if cursor.rowcount != 1:
                    current = self.get(proposal_id)
                    raise ProposalStateConflictError(current.status)
                if recovery:
                    append_audit_event(
                        connection,
                        proposal_id,
                        AuditEventType.RECOVERY_COMPLETED,
                        {
                            "action": record.proposal.action.value,
                            "outcome": (
                                "succeeded" if result.success else "failed"
                            ),
                        },
                    )
        except ProposalStateConflictError:
            raise
        except sqlite3.Error as exc:
            raise ApprovalStoreError(f"could not store execution result: {exc}") from exc
        return self.get(proposal_id)

    def fail_execution(
        self,
        proposal_id: str,
        failure: ApprovalError,
    ) -> ProposalRecord:
        try:
            with closing(self._connect()) as connection, connection:
                record = self._select(connection, proposal_id)
                cursor = connection.execute(
                    """
                    UPDATE proposals
                    SET status = ?, failure_json = ?, updated_at = ?
                    WHERE proposal_id = ? AND status IN (?, ?)
                    """,
                    (
                        ProposalStatus.FAILED.value,
                        self._serialize(failure),
                        self._timestamp(),
                        proposal_id,
                        ProposalStatus.PENDING.value,
                        ProposalStatus.EXECUTING.value,
                    ),
                )
                if cursor.rowcount != 1:
                    current = self.get(proposal_id)
                    raise ProposalStateConflictError(current.status)
                if record.status is ProposalStatus.EXECUTING:
                    append_audit_event(
                        connection,
                        proposal_id,
                        AuditEventType.EXECUTION_FAILED,
                        {
                            "action": record.proposal.action.value,
                            "error_code": failure.code.value,
                        },
                    )
        except ProposalStateConflictError:
            raise
        except sqlite3.Error as exc:
            raise ApprovalStoreError(f"could not store execution failure: {exc}") from exc
        return self.get(proposal_id)
