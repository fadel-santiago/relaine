from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from relaine.approval_cli import main as control_main
from relaine.approval_workflow import ApprovalService
from relaine.schemas import (
    CreateTaskParameters,
    CreateTaskProposal,
    ProposalStatus,
    RecoveryClassification,
    WorkflowResult,
    WorkflowStatus,
)


class StaticProposalProvider:
    def __init__(self, title: str = "Discover stalled execution") -> None:
        self.title = title

    def propose(self, request: str) -> WorkflowResult:
        return WorkflowResult(
            status=WorkflowStatus.PENDING_APPROVAL,
            request=request,
            proposal=CreateTaskProposal(
                action="create_task",
                parameters=CreateTaskParameters(title=self.title),
                rationale="The request explicitly asks to create this task.",
            ),
        )


def stalled_service(database: Path, title: str = "Discover stalled execution"):
    service = ApprovalService(database, StaticProposalProvider(title))
    submitted = service.submit(f"Create a task titled {title}.")
    assert submitted.proposal is not None
    executing = service.proposal_store.claim_for_execution(
        submitted.proposal.proposal_id,
        submitted.proposal.fingerprint,
    )
    return service, executing


def test_recover_list_is_empty_without_executing_proposals(tmp_path: Path) -> None:
    result = ApprovalService(tmp_path / "discovery.sqlite3").list_stalled_executions()

    assert result.error is None
    assert result.items == []


def test_uncommitted_receipt_v1_execution_is_recoverable(tmp_path: Path) -> None:
    database = tmp_path / "discovery.sqlite3"
    _, executing = stalled_service(database)

    result = ApprovalService(database).list_stalled_executions()

    assert result.error is None
    assert len(result.items) == 1
    item = result.items[0]
    assert item.proposal_id == executing.proposal_id
    assert item.status is ProposalStatus.EXECUTING
    assert item.fingerprint == executing.fingerprint
    assert item.receipt_present is False
    assert item.classification is RecoveryClassification.RECOVERABLE
    assert item.last_activity_at is not None


def test_committed_receipt_awaits_finalization(tmp_path: Path) -> None:
    database = tmp_path / "discovery.sqlite3"
    service, executing = stalled_service(database)
    receipt = service.execution_store.execute(executing)

    result = ApprovalService(database).list_stalled_executions()

    assert len(result.items) == 1
    item = result.items[0]
    assert item.receipt_present is True
    assert (
        item.classification
        is RecoveryClassification.COMMITTED_AWAITING_FINALIZATION
    )
    assert item.last_activity_at == receipt.committed_at


def test_legacy_execution_requires_manual_investigation(tmp_path: Path) -> None:
    database = tmp_path / "discovery.sqlite3"
    service = ApprovalService(database, StaticProposalProvider())
    submitted = service.submit("Create a task titled Discover stalled execution.")
    assert submitted.proposal is not None
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE proposals SET status = 'executing' WHERE proposal_id = ?",
            (submitted.proposal.proposal_id,),
        )

    item = ApprovalService(database).list_stalled_executions().items[0]

    assert item.receipt_present is False
    assert item.classification is RecoveryClassification.MANUAL_INVESTIGATION
    assert any("no verifiable receipt_v1" in detail for detail in item.details)


def test_tampered_proposal_requires_manual_investigation(tmp_path: Path) -> None:
    database = tmp_path / "discovery.sqlite3"
    _, executing = stalled_service(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER proposal_snapshot_immutable")
        raw = connection.execute(
            "SELECT proposal_json FROM proposals WHERE proposal_id = ?",
            (executing.proposal_id,),
        ).fetchone()[0]
        connection.execute(
            "UPDATE proposals SET proposal_json = ? WHERE proposal_id = ?",
            (raw.replace("Discover stalled execution", "Tampered"), executing.proposal_id),
        )

    item = ApprovalService(database).list_stalled_executions().items[0]

    assert item.classification is RecoveryClassification.MANUAL_INVESTIGATION
    assert item.fingerprint is None
    assert any("does not match its fingerprint" in detail for detail in item.details)


def test_tampered_receipt_requires_manual_investigation(tmp_path: Path) -> None:
    database = tmp_path / "discovery.sqlite3"
    service, executing = stalled_service(database)
    service.execution_store.execute(executing)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER execution_receipt_update_forbidden")
        connection.execute(
            "UPDATE execution_receipts SET result_json = '{}' WHERE proposal_id = ?",
            (executing.proposal_id,),
        )

    item = ApprovalService(database).list_stalled_executions().items[0]

    assert item.receipt_present is True
    assert item.classification is RecoveryClassification.MANUAL_INVESTIGATION
    assert any("receipt is malformed" in detail for detail in item.details)


def test_discovery_persists_across_service_instances(tmp_path: Path) -> None:
    database = tmp_path / "discovery.sqlite3"
    _, executing = stalled_service(database)

    recovered_process = ApprovalService(database)
    result = recovered_process.list_stalled_executions()

    assert [item.proposal_id for item in result.items] == [executing.proposal_id]
    assert result.items[0].classification is RecoveryClassification.RECOVERABLE


def test_age_filter_uses_timestamp_without_changing_safety_classification(
    tmp_path: Path,
) -> None:
    database = tmp_path / "discovery.sqlite3"
    _, old = stalled_service(database, "Old execution")
    _, fresh = stalled_service(database, "Fresh execution")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE proposals SET updated_at = ? WHERE proposal_id = ?",
            ((datetime.now(UTC) - timedelta(hours=2)).isoformat(), old.proposal_id),
        )

    result = ApprovalService(database).list_stalled_executions(3600)

    assert [item.proposal_id for item in result.items] == [old.proposal_id]
    assert result.items[0].classification is RecoveryClassification.RECOVERABLE
    assert fresh.proposal_id not in {item.proposal_id for item in result.items}


def test_recover_list_cli_prints_readable_json(
    tmp_path: Path,
    capsys,
) -> None:
    database = tmp_path / "discovery.sqlite3"
    _, executing = stalled_service(database)

    exit_code = control_main(
        ["--db-path", str(database), "recover-list", "--older-than-seconds", "0"]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["error"] is None
    assert output["minimum_age_seconds"] == 0
    assert output["items"][0]["proposal_id"] == executing.proposal_id
    assert output["items"][0]["classification"] == "recoverable"
