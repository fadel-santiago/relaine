from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Callable

import pytest

from relaine.approval_workflow import ApprovalService
from relaine.execution_receipts import ExecutionStoreError
from relaine.schemas import (
    ApprovalErrorCode,
    CreateTaskParameters,
    CreateTaskProposal,
    ProposalStatus,
    WorkflowResult,
    WorkflowStatus,
)


class StaticProposalProvider:
    def propose(self, request: str) -> WorkflowResult:
        return WorkflowResult(
            status=WorkflowStatus.PENDING_APPROVAL,
            request=request,
            proposal=CreateTaskProposal(
                action="create_task",
                parameters=CreateTaskParameters(title="Crash-safe task"),
                rationale="The request explicitly asks to create this task.",
            ),
        )


def task_count(path: Path) -> int:
    with sqlite3.connect(path) as connection:
        return connection.execute("SELECT count(*) FROM tasks").fetchone()[0]


def receipt_count(path: Path) -> int:
    with sqlite3.connect(path) as connection:
        return connection.execute(
            "SELECT count(*) FROM execution_receipts"
        ).fetchone()[0]


def submit(service: ApprovalService):
    submitted = service.submit("Create a task titled Crash-safe task.")
    assert submitted.proposal is not None
    return submitted.proposal


def simulate_crash_before_mutation(service: ApprovalService, proposal_id: str, fingerprint: str):
    return service.proposal_store.claim_for_execution(proposal_id, fingerprint)


def test_recovery_retries_when_crash_preceded_atomic_mutation(tmp_path: Path) -> None:
    database = tmp_path / "recovery.sqlite3"
    first_service = ApprovalService(database, StaticProposalProvider())
    pending = submit(first_service)
    executing = simulate_crash_before_mutation(
        first_service,
        pending.proposal_id,
        pending.fingerprint,
    )
    assert executing.status is ProposalStatus.EXECUTING
    assert first_service.execution_store.get(pending.proposal_id) is None
    assert task_count(database) == 0

    recovered = ApprovalService(database).recover(
        pending.proposal_id,
        pending.fingerprint,
    )

    assert recovered.success is True
    assert recovered.proposal is not None
    assert recovered.proposal.status is ProposalStatus.COMPLETED
    assert task_count(database) == 1
    assert receipt_count(database) == 1


def test_recovery_uses_receipt_after_commit_before_completion_recording(
    tmp_path: Path,
) -> None:
    database = tmp_path / "recovery.sqlite3"
    first_service = ApprovalService(database, StaticProposalProvider())
    pending = submit(first_service)
    executing = simulate_crash_before_mutation(
        first_service,
        pending.proposal_id,
        pending.fingerprint,
    )
    committed = first_service.execution_store.execute(executing)
    assert task_count(database) == 1
    assert first_service.proposal_store.get(pending.proposal_id).status is ProposalStatus.EXECUTING

    recovered = ApprovalService(database).recover(
        pending.proposal_id,
        pending.fingerprint,
    )

    assert recovered.success is True
    assert recovered.proposal is not None
    assert recovered.proposal.status is ProposalStatus.COMPLETED
    assert recovered.proposal.result == committed.result
    assert task_count(database) == 1
    assert receipt_count(database) == 1


def test_duplicate_receipt_execution_returns_original_result(tmp_path: Path) -> None:
    database = tmp_path / "recovery.sqlite3"
    service = ApprovalService(database, StaticProposalProvider())
    pending = submit(service)
    executing = simulate_crash_before_mutation(
        service,
        pending.proposal_id,
        pending.fingerprint,
    )

    first = service.execution_store.execute(executing)
    duplicate = service.execution_store.execute(executing)

    assert duplicate == first
    assert task_count(database) == 1
    assert receipt_count(database) == 1


def test_concurrent_recovery_across_services_commits_one_effect(tmp_path: Path) -> None:
    database = tmp_path / "recovery.sqlite3"
    submitting_service = ApprovalService(database, StaticProposalProvider())
    pending = submit(submitting_service)
    simulate_crash_before_mutation(
        submitting_service,
        pending.proposal_id,
        pending.fingerprint,
    )

    first_service = ApprovalService(database)
    second_service = ApprovalService(database)
    barrier = Barrier(2)
    first_execute = first_service.execution_store.execute
    second_execute = second_service.execution_store.execute

    def synchronized(execute: Callable):
        def run(record):
            barrier.wait(timeout=5)
            return execute(record)

        return run

    first_service.execution_store.execute = synchronized(first_execute)  # type: ignore[method-assign]
    second_service.execution_store.execute = synchronized(second_execute)  # type: ignore[method-assign]

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                service.recover,
                pending.proposal_id,
                pending.fingerprint,
            )
            for service in (first_service, second_service)
        ]
        results = [future.result(timeout=10) for future in futures]

    assert all(result.success for result in results)
    assert task_count(database) == 1
    assert receipt_count(database) == 1
    task_ids = {
        result.proposal.result.task.task_id
        for result in results
        if result.proposal is not None and result.proposal.result is not None
    }
    assert len(task_ids) == 1


def test_legacy_executing_state_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "recovery.sqlite3"
    service = ApprovalService(database, StaticProposalProvider())
    pending = submit(service)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            UPDATE proposals
            SET status = 'executing'
            WHERE proposal_id = ?
            """,
            (pending.proposal_id,),
        )

    recovered = ApprovalService(database).recover(
        pending.proposal_id,
        pending.fingerprint,
    )

    assert recovered.success is False
    assert recovered.error is not None
    assert recovered.error.code is ApprovalErrorCode.RECOVERY_UNSAFE
    assert task_count(database) == 0
    assert receipt_count(database) == 0


def test_recovery_database_error_leaves_execution_unresolved(tmp_path: Path) -> None:
    database = tmp_path / "recovery.sqlite3"
    service = ApprovalService(database, StaticProposalProvider())
    pending = submit(service)
    simulate_crash_before_mutation(service, pending.proposal_id, pending.fingerprint)
    recovering_service = ApprovalService(database)

    def database_failure(record):
        raise ExecutionStoreError("simulated receipt database failure")

    recovering_service.execution_store.execute = database_failure  # type: ignore[method-assign]
    recovered = recovering_service.recover(
        pending.proposal_id,
        pending.fingerprint,
    )

    assert recovered.success is False
    assert recovered.error is not None
    assert recovered.error.code is ApprovalErrorCode.DATABASE_ERROR
    assert recovering_service.proposal_store.get(pending.proposal_id).status is ProposalStatus.EXECUTING
    assert task_count(database) == 0


def test_recovery_refuses_tampered_executing_proposal(tmp_path: Path) -> None:
    database = tmp_path / "recovery.sqlite3"
    service = ApprovalService(database, StaticProposalProvider())
    pending = submit(service)
    simulate_crash_before_mutation(service, pending.proposal_id, pending.fingerprint)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER proposal_snapshot_immutable")
        raw = connection.execute(
            "SELECT proposal_json FROM proposals WHERE proposal_id = ?",
            (pending.proposal_id,),
        ).fetchone()[0]
        modified = raw.replace("Crash-safe task", "Tampered task")
        connection.execute(
            "UPDATE proposals SET proposal_json = ? WHERE proposal_id = ?",
            (modified, pending.proposal_id),
        )

    recovered = ApprovalService(database).recover(
        pending.proposal_id,
        pending.fingerprint,
    )

    assert recovered.success is False
    assert recovered.error is not None
    assert recovered.error.code is ApprovalErrorCode.PROPOSAL_MODIFIED
    assert task_count(database) == 0
    assert receipt_count(database) == 0


def test_committed_receipt_is_database_immutable(tmp_path: Path) -> None:
    database = tmp_path / "recovery.sqlite3"
    service = ApprovalService(database, StaticProposalProvider())
    pending = submit(service)
    executing = simulate_crash_before_mutation(
        service,
        pending.proposal_id,
        pending.fingerprint,
    )
    service.execution_store.execute(executing)

    with sqlite3.connect(database) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="execution receipt is immutable"):
            connection.execute(
                "UPDATE execution_receipts SET result_json = '{}' WHERE proposal_id = ?",
                (pending.proposal_id,),
            )


def test_recovery_never_executes_unapproved_rejected_or_completed(
    tmp_path: Path,
) -> None:
    database = tmp_path / "recovery.sqlite3"
    service = ApprovalService(database, StaticProposalProvider())

    pending = submit(service)
    unapproved = service.recover(pending.proposal_id, pending.fingerprint)
    assert unapproved.success is False
    assert unapproved.error is not None
    assert unapproved.error.code is ApprovalErrorCode.INVALID_STATE

    rejected_pending = submit(service)
    rejected = service.reject(
        rejected_pending.proposal_id,
        rejected_pending.fingerprint,
    )
    assert rejected.success is True
    rejected_recovery = service.recover(
        rejected_pending.proposal_id,
        rejected_pending.fingerprint,
    )
    assert rejected_recovery.success is False
    assert rejected_recovery.error is not None
    assert rejected_recovery.error.code is ApprovalErrorCode.INVALID_STATE

    approved_pending = submit(service)
    approved = service.approve(
        approved_pending.proposal_id,
        approved_pending.fingerprint,
    )
    assert approved.success is True
    completed_recovery = service.recover(
        approved_pending.proposal_id,
        approved_pending.fingerprint,
    )
    assert completed_recovery.success is False
    assert completed_recovery.error is not None
    assert completed_recovery.error.code is ApprovalErrorCode.INVALID_STATE
    assert task_count(database) == 1
