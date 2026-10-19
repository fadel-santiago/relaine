from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from relaine.approval_cli import main as control_main
from relaine.approval_storage import proposal_fingerprint
from relaine.approval_workflow import ApprovalService
from relaine.execution_receipts import ExecutionStoreError
from relaine.schemas import (
    AuditEventType,
    BusinessToolErrorCode,
    CreateTaskParameters,
    CreateTaskProposal,
    ProposalStatus,
    TaskStatus,
    TaskUpdates,
    UpdateTaskParameters,
    UpdateTaskProposal,
    WorkflowResult,
    WorkflowStatus,
)


class StaticProposalProvider:
    def __init__(self, proposal) -> None:
        self.proposal = proposal

    def propose(self, request: str) -> WorkflowResult:
        return WorkflowResult(
            status=WorkflowStatus.PENDING_APPROVAL,
            request=request,
            proposal=self.proposal,
        )


def create_proposal(title: str = "Audited task") -> CreateTaskProposal:
    return CreateTaskProposal(
        action="create_task",
        parameters=CreateTaskParameters(title=title),
        rationale="The request explicitly asks to create this task.",
    )


def submit(service: ApprovalService, request: str = "Create the audited task"):
    submitted = service.submit(request)
    assert submitted.proposal is not None
    return submitted.proposal


def event_types(service: ApprovalService, proposal_id: str) -> list[AuditEventType]:
    history = service.history(proposal_id)
    assert history.error is None
    return [event.event_type for event in history.events]


def table_count(database: Path, table: str) -> int:
    assert table in {"tasks", "execution_receipts"}
    with sqlite3.connect(database) as connection:
        return connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_successful_approval_records_real_state_transitions(tmp_path: Path) -> None:
    database = tmp_path / "audit.sqlite3"
    service = ApprovalService(database, StaticProposalProvider(create_proposal()))
    pending = submit(service)

    approved = service.approve(pending.proposal_id, pending.fingerprint)

    assert approved.success is True
    assert approved.proposal is not None
    assert approved.proposal.status is ProposalStatus.COMPLETED
    assert table_count(database, "tasks") == 1
    assert table_count(database, "execution_receipts") == 1
    assert event_types(service, pending.proposal_id) == [
        AuditEventType.PROPOSAL_SUBMITTED,
        AuditEventType.PROPOSAL_APPROVED,
        AuditEventType.EXECUTION_CLAIMED,
        AuditEventType.EXECUTION_SUCCEEDED,
    ]
    for event in service.history(pending.proposal_id).events:
        assert not ({"request", "title", "description", "assignee", "rationale"} & event.metadata.keys())


def test_rejection_is_audited_without_business_mutation(tmp_path: Path) -> None:
    database = tmp_path / "audit.sqlite3"
    service = ApprovalService(database, StaticProposalProvider(create_proposal()))
    pending = submit(service)

    rejected = service.reject(pending.proposal_id, pending.fingerprint)

    assert rejected.success is True
    assert rejected.proposal is not None
    assert rejected.proposal.status is ProposalStatus.REJECTED
    assert table_count(database, "tasks") == 0
    assert event_types(service, pending.proposal_id) == [
        AuditEventType.PROPOSAL_SUBMITTED,
        AuditEventType.PROPOSAL_REJECTED,
    ]


def test_failed_tool_result_records_execution_failure(tmp_path: Path) -> None:
    database = tmp_path / "audit.sqlite3"
    proposal = UpdateTaskProposal(
        action="update_task",
        parameters=UpdateTaskParameters(
            task_id="TASK-DOES-NOT-EXIST",
            changes=TaskUpdates(status=TaskStatus.DONE),
        ),
        rationale="The task ID and requested status are explicit.",
    )
    service = ApprovalService(database, StaticProposalProvider(proposal))
    pending = submit(service, "Mark the missing task done")

    result = service.approve(pending.proposal_id, pending.fingerprint)

    assert result.success is True
    assert result.proposal is not None
    assert result.proposal.status is ProposalStatus.FAILED
    assert result.proposal.result is not None
    assert result.proposal.result.error is not None
    assert result.proposal.result.error.code is BusinessToolErrorCode.TASK_NOT_FOUND
    history = service.history(pending.proposal_id)
    assert [event.event_type for event in history.events] == [
        AuditEventType.PROPOSAL_SUBMITTED,
        AuditEventType.PROPOSAL_APPROVED,
        AuditEventType.EXECUTION_CLAIMED,
        AuditEventType.EXECUTION_FAILED,
    ]
    assert history.events[-1].metadata["error_code"] == "task_not_found"


def test_recovery_attempt_and_completion_are_persisted_in_order(tmp_path: Path) -> None:
    database = tmp_path / "audit.sqlite3"
    service = ApprovalService(database, StaticProposalProvider(create_proposal()))
    pending = submit(service)
    service.proposal_store.claim_for_execution(
        pending.proposal_id,
        pending.fingerprint,
    )

    recovered = ApprovalService(database).recover(
        pending.proposal_id,
        pending.fingerprint,
    )

    assert recovered.success is True
    assert recovered.proposal is not None
    assert recovered.proposal.status is ProposalStatus.COMPLETED
    assert table_count(database, "tasks") == 1
    assert event_types(service, pending.proposal_id) == [
        AuditEventType.PROPOSAL_SUBMITTED,
        AuditEventType.PROPOSAL_APPROVED,
        AuditEventType.EXECUTION_CLAIMED,
        AuditEventType.RECOVERY_ATTEMPTED,
        AuditEventType.EXECUTION_SUCCEEDED,
        AuditEventType.RECOVERY_COMPLETED,
    ]


def test_audit_outcome_failure_rolls_back_mutation_and_receipt(tmp_path: Path) -> None:
    database = tmp_path / "audit.sqlite3"
    service = ApprovalService(database, StaticProposalProvider(create_proposal()))
    pending = submit(service)
    executing = service.proposal_store.claim_for_execution(
        pending.proposal_id,
        pending.fingerprint,
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TRIGGER abort_execution_audit
            BEFORE INSERT ON audit_events
            WHEN NEW.event_type = 'execution_succeeded'
            BEGIN
                SELECT RAISE(ABORT, 'simulated audit failure');
            END
            """
        )

    with pytest.raises(ExecutionStoreError, match="simulated audit failure"):
        service.execution_store.execute(executing)

    assert table_count(database, "tasks") == 0
    assert table_count(database, "execution_receipts") == 0
    assert service.proposal_store.get(pending.proposal_id).status is ProposalStatus.EXECUTING


def test_audit_events_cannot_be_updated_or_deleted(tmp_path: Path) -> None:
    database = tmp_path / "audit.sqlite3"
    service = ApprovalService(database, StaticProposalProvider(create_proposal()))
    pending = submit(service)

    with sqlite3.connect(database) as connection, pytest.raises(
        sqlite3.IntegrityError,
        match="audit event is immutable",
    ):
        connection.execute(
            "UPDATE audit_events SET event_type = 'proposal_rejected' WHERE proposal_id = ?",
            (pending.proposal_id,),
        )
    with sqlite3.connect(database) as connection, pytest.raises(
        sqlite3.IntegrityError,
        match="audit event is immutable",
    ):
        connection.execute(
            "DELETE FROM audit_events WHERE proposal_id = ?",
            (pending.proposal_id,),
        )


def test_history_persists_and_duplicate_approval_adds_no_events(tmp_path: Path) -> None:
    database = tmp_path / "audit.sqlite3"
    service = ApprovalService(database, StaticProposalProvider(create_proposal()))
    pending = submit(service)
    first = service.approve(pending.proposal_id, pending.fingerprint)
    assert first.success is True
    original = ApprovalService(database).history(pending.proposal_id).events

    duplicate = ApprovalService(database).approve(
        pending.proposal_id,
        pending.fingerprint,
    )
    persisted = ApprovalService(database).history(pending.proposal_id).events

    assert duplicate.success is False
    assert persisted == original


def test_duplicate_rejection_and_recovery_add_no_false_events(tmp_path: Path) -> None:
    database = tmp_path / "audit.sqlite3"
    service = ApprovalService(database, StaticProposalProvider(create_proposal()))

    rejected_pending = submit(service)
    assert service.reject(
        rejected_pending.proposal_id,
        rejected_pending.fingerprint,
    ).success
    rejected_history = service.history(rejected_pending.proposal_id).events
    duplicate_rejection = service.reject(
        rejected_pending.proposal_id,
        rejected_pending.fingerprint,
    )

    recovery_pending = submit(service)
    service.proposal_store.claim_for_execution(
        recovery_pending.proposal_id,
        recovery_pending.fingerprint,
    )
    assert service.recover(
        recovery_pending.proposal_id,
        recovery_pending.fingerprint,
    ).success
    recovered_history = service.history(recovery_pending.proposal_id).events
    duplicate_recovery = service.recover(
        recovery_pending.proposal_id,
        recovery_pending.fingerprint,
    )

    assert duplicate_rejection.success is False
    assert service.history(rejected_pending.proposal_id).events == rejected_history
    assert duplicate_recovery.success is False
    assert service.history(recovery_pending.proposal_id).events == recovered_history


def test_prefeature_records_do_not_receive_fabricated_history(tmp_path: Path) -> None:
    database = tmp_path / "audit.sqlite3"
    service = ApprovalService(database)
    proposal = create_proposal("Legacy task")
    fingerprint = proposal_fingerprint(proposal)
    timestamp = datetime.now(UTC).isoformat()
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO proposals (
                proposal_id, request, proposal_json, fingerprint, status,
                execution_protocol, result_json, failure_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'pending', NULL, NULL, NULL, ?, ?)
            """,
            (
                "PROP-LEGACY",
                "Legacy request",
                proposal.model_dump_json(),
                fingerprint,
                timestamp,
                timestamp,
            ),
        )

    history = service.history("PROP-LEGACY")

    assert history.error is None
    assert history.events == []


def test_history_cli_prints_chronological_json(tmp_path: Path, capsys) -> None:
    database = tmp_path / "audit.sqlite3"
    service = ApprovalService(database, StaticProposalProvider(create_proposal()))
    pending = submit(service)
    service.reject(pending.proposal_id, pending.fingerprint)

    exit_code = control_main(
        ["--db-path", str(database), "history", pending.proposal_id]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["proposal_id"] == pending.proposal_id
    assert [event["event_type"] for event in output["events"]] == [
        "proposal_submitted",
        "proposal_rejected",
    ]
