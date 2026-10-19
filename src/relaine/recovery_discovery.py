"""Read-only integrity classification for unfinished executions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError

from relaine.approval_storage import PROPOSAL_ADAPTER, ProposalStore, proposal_fingerprint
from relaine.schemas import (
    ExecutionProtocol,
    ExecutionReceipt,
    ProposalStatus,
    RecoveryClassification,
    StalledExecution,
    StalledExecutionList,
    TaskToolResult,
)


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _latest(*timestamps: datetime | None) -> datetime | None:
    present = [timestamp for timestamp in timestamps if timestamp is not None]
    return max(present) if present else None


class RecoveryDiscovery:
    def __init__(self, proposal_store: ProposalStore) -> None:
        self.proposal_store = proposal_store

    @staticmethod
    def _classify(row: dict[str, Any]) -> StalledExecution:
        details: list[str] = []
        proposal_id = str(row["proposal_id"])
        proposal_updated_at = _timestamp(row.get("proposal_updated_at"))
        verified_fingerprint: str | None = None
        proposal_valid = False

        try:
            proposal = PROPOSAL_ADAPTER.validate_json(row["proposal_json"])
            computed_fingerprint = proposal_fingerprint(proposal)
            if computed_fingerprint == row.get("fingerprint"):
                proposal_valid = True
                verified_fingerprint = computed_fingerprint
            else:
                details.append("Stored proposal does not match its fingerprint.")
        except (ValidationError, TypeError, ValueError):
            details.append("Stored proposal snapshot is malformed.")

        if row.get("proposal_result_json") is not None:
            details.append("Executing proposal unexpectedly contains a final result.")
        if row.get("proposal_failure_json") is not None:
            details.append("Executing proposal unexpectedly contains a failure record.")

        receipt_present = row.get("receipt_proposal_id") is not None
        receipt_valid = False
        receipt_committed_at: datetime | None = None
        if receipt_present:
            try:
                result = TaskToolResult.model_validate_json(row["receipt_result_json"])
                receipt_committed_at = _timestamp(row["receipt_committed_at"])
                if receipt_committed_at is None:
                    raise ValueError("receipt timestamp is invalid")
                receipt = ExecutionReceipt(
                    proposal_id=row["receipt_proposal_id"],
                    fingerprint=row["receipt_fingerprint"],
                    result=result,
                    committed_at=receipt_committed_at,
                )
                if receipt.proposal_id != proposal_id:
                    details.append("Receipt proposal ID does not match the proposal.")
                elif not proposal_valid or receipt.fingerprint != verified_fingerprint:
                    details.append("Receipt fingerprint does not match the proposal.")
                else:
                    receipt_valid = True
            except (ValidationError, TypeError, ValueError):
                details.append("Execution receipt is malformed.")

        protocol_valid = row.get("execution_protocol") == ExecutionProtocol.RECEIPT_V1.value
        if not protocol_valid:
            details.append("Execution has no verifiable receipt_v1 protocol marker.")

        unexpected_payload = (
            row.get("proposal_result_json") is not None
            or row.get("proposal_failure_json") is not None
        )
        if not proposal_valid or not protocol_valid or unexpected_payload:
            classification = RecoveryClassification.MANUAL_INVESTIGATION
        elif receipt_present and receipt_valid:
            classification = RecoveryClassification.COMMITTED_AWAITING_FINALIZATION
            details.append(
                "A valid committed receipt can finalize this proposal without another write."
            )
        elif receipt_present:
            classification = RecoveryClassification.MANUAL_INVESTIGATION
        else:
            classification = RecoveryClassification.RECOVERABLE
            details.append(
                "No receipt exists; receipt_v1 proves an atomic retry has no committed effect."
            )

        return StalledExecution(
            proposal_id=proposal_id,
            status=ProposalStatus.EXECUTING,
            fingerprint=verified_fingerprint,
            last_activity_at=_latest(proposal_updated_at, receipt_committed_at),
            receipt_present=receipt_present,
            classification=classification,
            details=details,
        )

    def list(
        self,
        minimum_age_seconds: int | None = None,
        *,
        now: datetime | None = None,
    ) -> StalledExecutionList:
        if minimum_age_seconds is not None and minimum_age_seconds < 0:
            raise ValueError("minimum age must be non-negative")
        generated_at = now or datetime.now(UTC)
        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=UTC)
        cutoff = (
            None
            if minimum_age_seconds is None
            else generated_at - timedelta(seconds=minimum_age_seconds)
        )

        items: list[StalledExecution] = []
        for row in self.proposal_store.list_executing_rows():
            item = self._classify(row)
            if (
                cutoff is not None
                and item.last_activity_at is not None
                and item.last_activity_at > cutoff
            ):
                continue
            if cutoff is not None and item.last_activity_at is None:
                item = item.model_copy(
                    update={
                        "details": [
                            *item.details,
                            "Age filter was not applied because no valid timestamp exists.",
                        ]
                    }
                )
            items.append(item)

        return StalledExecutionList(
            items=items,
            generated_at=generated_at,
            minimum_age_seconds=minimum_age_seconds,
        )
