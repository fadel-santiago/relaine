"""Atomic business mutation and durable execution receipt storage."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from relaine.approval_storage import proposal_fingerprint
from relaine.audit import append_audit_event, initialize_audit_schema
from relaine.schemas import (
    ActionType,
    AuditEventType,
    BusinessToolError,
    BusinessToolErrorCode,
    ExecutionProtocol,
    ExecutionReceipt,
    ProposalRecord,
    ProposalStatus,
    TaskToolResult,
)
from relaine.storage import SQLiteTaskStore, TaskNotFoundError, TaskStoreError


class ExecutionStoreError(RuntimeError):
    """A database or persisted-data failure at the receipt boundary."""


class ReceiptConflictError(RuntimeError):
    """A receipt exists but does not belong to the immutable proposal snapshot."""


class UnsafeExecutionError(RuntimeError):
    """The proposal state cannot prove that retrying is side-effect safe."""


class ExecutionReceiptStore:
    def __init__(self, path: str | Path, task_store: SQLiteTaskStore) -> None:
        self.path = Path(path)
        self.task_store = task_store
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
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS execution_receipts (
                        proposal_id TEXT PRIMARY KEY,
                        fingerprint TEXT NOT NULL CHECK (length(fingerprint) = 64),
                        result_json TEXT NOT NULL,
                        committed_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS execution_receipt_update_forbidden
                    BEFORE UPDATE ON execution_receipts
                    BEGIN
                        SELECT RAISE(ABORT, 'execution receipt is immutable');
                    END
                    """
                )
                connection.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS execution_receipt_delete_forbidden
                    BEFORE DELETE ON execution_receipts
                    BEGIN
                        SELECT RAISE(ABORT, 'execution receipt is immutable');
                    END
                    """
                )
        except sqlite3.Error as exc:
            raise ExecutionStoreError(
                f"could not initialize execution receipt database: {exc}"
            ) from exc

    @staticmethod
    def _serialize(result: TaskToolResult) -> str:
        return json.dumps(
            result.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _row_to_receipt(row: sqlite3.Row) -> ExecutionReceipt:
        return ExecutionReceipt(
            proposal_id=row["proposal_id"],
            fingerprint=row["fingerprint"],
            result=TaskToolResult.model_validate_json(row["result_json"]),
            committed_at=row["committed_at"],
        )

    def _select(
        self,
        connection: sqlite3.Connection,
        proposal_id: str,
    ) -> ExecutionReceipt | None:
        row = connection.execute(
            "SELECT * FROM execution_receipts WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()
        return None if row is None else self._row_to_receipt(row)

    def get(self, proposal_id: str) -> ExecutionReceipt | None:
        try:
            with closing(self._connect()) as connection:
                return self._select(connection, proposal_id)
        except (sqlite3.Error, ValidationError) as exc:
            raise ExecutionStoreError(f"could not retrieve execution receipt: {exc}") from exc

    @staticmethod
    def _assert_safe(record: ProposalRecord) -> None:
        if record.status is not ProposalStatus.EXECUTING:
            raise UnsafeExecutionError(
                f"proposal is {record.status.value}, not executing"
            )
        if record.execution_protocol is not ExecutionProtocol.RECEIPT_V1:
            raise UnsafeExecutionError(
                "executing proposal has no receipt protocol marker"
            )
        if proposal_fingerprint(record.proposal) != record.fingerprint:
            raise UnsafeExecutionError("proposal fingerprint validation failed")

    def execute(self, record: ProposalRecord) -> ExecutionReceipt:
        """Return an existing receipt or atomically commit effect plus receipt."""

        self._assert_safe(record)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._select(connection, record.proposal_id)
            if existing is not None:
                if existing.fingerprint != record.fingerprint:
                    raise ReceiptConflictError(
                        "receipt fingerprint does not match the proposal"
                    )
                connection.commit()
                return existing

            if record.proposal.action is ActionType.CREATE_TASK:
                task = self.task_store.insert_task_in_transaction(
                    connection,
                    record.proposal.parameters,
                )
                result = TaskToolResult(success=True, task=task)
            else:
                try:
                    task = self.task_store.update_task_status_in_transaction(
                        connection,
                        record.proposal.parameters.task_id,
                        record.proposal.parameters.changes.status,
                    )
                    result = TaskToolResult(success=True, task=task)
                except TaskNotFoundError:
                    result = TaskToolResult(
                        success=False,
                        error=BusinessToolError(
                            code=BusinessToolErrorCode.TASK_NOT_FOUND,
                            message=(
                                f"Task {record.proposal.parameters.task_id} "
                                "does not exist."
                            ),
                        ),
                    )

            committed_at = datetime.now(UTC).isoformat()
            connection.execute(
                """
                INSERT INTO execution_receipts (
                    proposal_id, fingerprint, result_json, committed_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    record.proposal_id,
                    record.fingerprint,
                    self._serialize(result),
                    committed_at,
                ),
            )
            if result.success:
                append_audit_event(
                    connection,
                    record.proposal_id,
                    AuditEventType.EXECUTION_SUCCEEDED,
                    {
                        "action": record.proposal.action.value,
                        "task_id": result.task.task_id,
                        "task_status": result.task.status.value,
                    },
                    timestamp=committed_at,
                )
            else:
                append_audit_event(
                    connection,
                    record.proposal_id,
                    AuditEventType.EXECUTION_FAILED,
                    {
                        "action": record.proposal.action.value,
                        "error_code": result.error.code.value,
                    },
                    timestamp=committed_at,
                )
            connection.commit()
            return ExecutionReceipt(
                proposal_id=record.proposal_id,
                fingerprint=record.fingerprint,
                result=result,
                committed_at=committed_at,
            )
        except (ReceiptConflictError, UnsafeExecutionError):
            connection.rollback()
            raise
        except (sqlite3.Error, ValidationError, TaskStoreError) as exc:
            connection.rollback()
            raise ExecutionStoreError(
                f"could not atomically execute proposal: {exc}"
            ) from exc
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
