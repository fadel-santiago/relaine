from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from relaine.approval_storage import ProposalStateConflictError, ProposalStore
from relaine.approval_workflow import ApprovalService
from relaine.schemas import (
    ApprovalErrorCode,
    BusinessToolErrorCode,
    CreateTaskParameters,
    CreateTaskProposal,
    ErrorCode,
    ProposalStatus,
    TaskStatus,
    TaskUpdates,
    UpdateTaskParameters,
    UpdateTaskProposal,
    WorkflowError,
    WorkflowResult,
    WorkflowStatus,
)
from relaine.storage import SQLiteTaskStore
from relaine.tools import create_task, get_task


class StaticProposalProvider:
    def __init__(self, result: WorkflowResult) -> None:
        self.result = result
        self.calls = 0

    def propose(self, request: str) -> WorkflowResult:
        self.calls += 1
        return self.result.model_copy(update={"request": request})


def create_proposal_result(title: str = "Prepare launch plan") -> WorkflowResult:
    return WorkflowResult(
        status=WorkflowStatus.PENDING_APPROVAL,
        request=f"Create a task titled {title}.",
        proposal=CreateTaskProposal(
            action="create_task",
            parameters=CreateTaskParameters(title=title),
            rationale="The request explicitly asks to create this task.",
        ),
    )


def update_proposal_result(task_id: str, status: TaskStatus) -> WorkflowResult:
    return WorkflowResult(
        status=WorkflowStatus.PENDING_APPROVAL,
        request=f"Set {task_id} to {status.value}.",
        proposal=UpdateTaskProposal(
            action="update_task",
            parameters=UpdateTaskParameters(
                task_id=task_id,
                changes=TaskUpdates(status=status),
            ),
            rationale="The task ID and requested status are explicit.",
        ),
    )


def task_count(path: Path) -> int:
    with sqlite3.connect(path) as connection:
        return connection.execute("SELECT count(*) FROM tasks").fetchone()[0]


def test_approved_creation_executes_stored_proposal(tmp_path: Path) -> None:
    database = tmp_path / "approval.sqlite3"
    provider = StaticProposalProvider(create_proposal_result())
    service = ApprovalService(database, provider)

    submitted = service.submit("Create a task titled Prepare launch plan.")
    assert submitted.error is None
    assert submitted.proposal is not None
    assert submitted.proposal.status is ProposalStatus.PENDING

    approved = service.approve(
        submitted.proposal.proposal_id,
        submitted.proposal.fingerprint,
    )

    assert approved.success is True
    assert approved.proposal is not None
    assert approved.proposal.status is ProposalStatus.COMPLETED
    assert approved.proposal.result is not None
    assert approved.proposal.result.success is True
    assert approved.proposal.result.task is not None
    persisted = get_task(
        SQLiteTaskStore(database),
        {"task_id": approved.proposal.result.task.task_id},
    )
    assert persisted.task == approved.proposal.result.task


def test_approved_update_executes_status_change(tmp_path: Path) -> None:
    database = tmp_path / "approval.sqlite3"
    task_store = SQLiteTaskStore(database)
    created = create_task(task_store, {"title": "Ship release"})
    assert created.task is not None
    provider = StaticProposalProvider(
        update_proposal_result(created.task.task_id, TaskStatus.DONE)
    )
    service = ApprovalService(database, provider)
    submitted = service.submit("Mark the task done.")
    assert submitted.proposal is not None

    approved = service.approve(
        submitted.proposal.proposal_id,
        submitted.proposal.fingerprint,
    )

    assert approved.success is True
    assert approved.proposal is not None
    assert approved.proposal.status is ProposalStatus.COMPLETED
    persisted = task_store.fetch_task(created.task.task_id)
    assert persisted is not None
    assert persisted.status is TaskStatus.DONE


def test_rejection_terminates_without_business_write(tmp_path: Path) -> None:
    database = tmp_path / "approval.sqlite3"
    service = ApprovalService(
        database,
        StaticProposalProvider(create_proposal_result("Rejected task")),
    )
    submitted = service.submit("Create a task titled Rejected task.")
    assert submitted.proposal is not None

    rejected = service.reject(
        submitted.proposal.proposal_id,
        submitted.proposal.fingerprint,
    )

    assert rejected.success is True
    assert rejected.proposal is not None
    assert rejected.proposal.status is ProposalStatus.REJECTED
    assert rejected.proposal.result is None
    assert task_count(database) == 0


def test_mismatched_fingerprint_does_not_consume_pending_approval(
    tmp_path: Path,
) -> None:
    database = tmp_path / "approval.sqlite3"
    service = ApprovalService(database, StaticProposalProvider(create_proposal_result()))
    submitted = service.submit("Create a task titled Prepare launch plan.")
    assert submitted.proposal is not None

    mismatch = service.approve(submitted.proposal.proposal_id, "0" * 64)

    assert mismatch.success is False
    assert mismatch.error is not None
    assert mismatch.error.code is ApprovalErrorCode.FINGERPRINT_MISMATCH
    assert mismatch.proposal is not None
    assert mismatch.proposal.status is ProposalStatus.PENDING
    assert task_count(database) == 0

    approved = service.approve(
        submitted.proposal.proposal_id,
        submitted.proposal.fingerprint,
    )
    assert approved.success is True
    assert approved.proposal is not None
    assert approved.proposal.status is ProposalStatus.COMPLETED


def test_proposal_snapshot_is_database_immutable(tmp_path: Path) -> None:
    database = tmp_path / "approval.sqlite3"
    service = ApprovalService(database, StaticProposalProvider(create_proposal_result()))
    submitted = service.submit("Create a task titled Prepare launch plan.")
    assert submitted.proposal is not None

    with sqlite3.connect(database) as connection, pytest.raises(
        sqlite3.IntegrityError,
        match="proposal snapshot is immutable",
    ):
        connection.execute(
            "UPDATE proposals SET proposal_json = ? WHERE proposal_id = ?",
            ("{}", submitted.proposal.proposal_id),
        )


def test_fingerprint_detects_modified_snapshot(tmp_path: Path) -> None:
    database = tmp_path / "approval.sqlite3"
    service = ApprovalService(database, StaticProposalProvider(create_proposal_result()))
    submitted = service.submit("Create a task titled Prepare launch plan.")
    assert submitted.proposal is not None

    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER proposal_snapshot_immutable")
        raw = connection.execute(
            "SELECT proposal_json FROM proposals WHERE proposal_id = ?",
            (submitted.proposal.proposal_id,),
        ).fetchone()[0]
        modified = json.loads(raw)
        modified["parameters"]["title"] = "Tampered title"
        connection.execute(
            "UPDATE proposals SET proposal_json = ? WHERE proposal_id = ?",
            (json.dumps(modified), submitted.proposal.proposal_id),
        )

    result = service.approve(
        submitted.proposal.proposal_id,
        submitted.proposal.fingerprint,
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.code is ApprovalErrorCode.PROPOSAL_MODIFIED
    assert task_count(database) == 0


def test_duplicate_approval_cannot_execute_twice(tmp_path: Path) -> None:
    database = tmp_path / "approval.sqlite3"
    service = ApprovalService(database, StaticProposalProvider(create_proposal_result()))
    submitted = service.submit("Create a task titled Prepare launch plan.")
    assert submitted.proposal is not None

    first = service.approve(
        submitted.proposal.proposal_id,
        submitted.proposal.fingerprint,
    )
    second = service.approve(
        submitted.proposal.proposal_id,
        submitted.proposal.fingerprint,
    )

    assert first.success is True
    assert second.success is False
    assert second.error is not None
    assert second.error.code is ApprovalErrorCode.INVALID_STATE
    assert task_count(database) == 1


def test_atomic_claim_allows_only_one_executor(tmp_path: Path) -> None:
    database = tmp_path / "approval.sqlite3"
    service = ApprovalService(database, StaticProposalProvider(create_proposal_result()))
    submitted = service.submit("Create a task titled Prepare launch plan.")
    assert submitted.proposal is not None
    first_store = ProposalStore(database)
    second_store = ProposalStore(database)

    claimed = first_store.claim_for_execution(
        submitted.proposal.proposal_id,
        submitted.proposal.fingerprint,
    )

    assert claimed.status is ProposalStatus.EXECUTING
    with pytest.raises(ProposalStateConflictError):
        second_store.claim_for_execution(
            submitted.proposal.proposal_id,
            submitted.proposal.fingerprint,
        )


def test_pending_proposal_and_checkpoint_recover_across_services(
    tmp_path: Path,
) -> None:
    database = tmp_path / "approval.sqlite3"
    first_service = ApprovalService(
        database,
        StaticProposalProvider(create_proposal_result("Recovered task")),
    )
    submitted = first_service.submit("Create a task titled Recovered task.")
    assert submitted.proposal is not None

    with sqlite3.connect(database) as connection:
        checkpoint_count = connection.execute(
            "SELECT count(*) FROM checkpoints"
        ).fetchone()[0]
    assert checkpoint_count > 0

    recovered_service = ApprovalService(database)
    inspected = recovered_service.inspect(submitted.proposal.proposal_id)
    approved = recovered_service.approve(
        submitted.proposal.proposal_id,
        submitted.proposal.fingerprint,
    )

    assert inspected.success is True
    assert inspected.proposal is not None
    assert inspected.proposal.status is ProposalStatus.PENDING
    assert approved.success is True
    assert approved.proposal is not None
    assert approved.proposal.status is ProposalStatus.COMPLETED


def test_tool_failure_is_persisted_and_visible(tmp_path: Path) -> None:
    database = tmp_path / "approval.sqlite3"
    service = ApprovalService(
        database,
        StaticProposalProvider(
            update_proposal_result("TASK-DOES-NOT-EXIST", TaskStatus.DONE)
        ),
    )
    submitted = service.submit("Mark missing task done.")
    assert submitted.proposal is not None

    approved = service.approve(
        submitted.proposal.proposal_id,
        submitted.proposal.fingerprint,
    )

    assert approved.success is True
    assert approved.proposal is not None
    assert approved.proposal.status is ProposalStatus.FAILED
    assert approved.proposal.result is not None
    assert approved.proposal.result.error is not None
    assert approved.proposal.result.error.code is BusinessToolErrorCode.TASK_NOT_FOUND


def test_unsupported_request_is_not_persisted_or_checkpointed(
    tmp_path: Path,
) -> None:
    database = tmp_path / "approval.sqlite3"
    unsupported = WorkflowResult(
        status=WorkflowStatus.UNSUPPORTED,
        request="Delete every task.",
        error=WorkflowError(
            code=ErrorCode.UNSUPPORTED_REQUEST,
            message="Deleting tasks is outside R1.",
        ),
    )
    provider = StaticProposalProvider(unsupported)
    service = ApprovalService(database, provider)

    result = service.submit("Delete every task.")

    assert result.workflow_result.status is WorkflowStatus.UNSUPPORTED
    assert result.proposal is None
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM proposals").fetchone()[0] == 0
        checkpoint_table = connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE name = 'checkpoints'"
        ).fetchone()[0]
    assert checkpoint_table == 0
