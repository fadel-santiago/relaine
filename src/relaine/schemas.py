"""Strict schemas shared by RELAINE proposal and business-tool layers."""

from __future__ import annotations

from enum import StrEnum
from datetime import datetime
from typing import Any, Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


NonEmptyString = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, strict=True),
]
DateString = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    ),
]


class StrictModel(BaseModel):
    # Enum values must be accepted from JSON strings, while fields themselves
    # remain strictly constrained and unknown fields are rejected.
    model_config = ConfigDict(extra="forbid", validate_default=True)


class ActionType(StrEnum):
    CREATE_TASK = "create_task"
    UPDATE_TASK = "update_task"


class TaskStatus(StrEnum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    DONE = "done"


class CreateTaskParameters(StrictModel):
    title: NonEmptyString
    description: NonEmptyString | None = None
    assignee: NonEmptyString | None = None
    due_date: DateString | None = None


class TaskUpdates(StrictModel):
    # Preserve R1's existing nullable wire shape while R2 permits only status writes.
    title: None = None
    description: None = None
    status: TaskStatus
    assignee: None = None
    due_date: None = None


class UpdateTaskParameters(StrictModel):
    task_id: NonEmptyString
    changes: TaskUpdates


class CreateTaskProposal(StrictModel):
    action: Literal[ActionType.CREATE_TASK]
    parameters: CreateTaskParameters
    rationale: NonEmptyString


class UpdateTaskProposal(StrictModel):
    action: Literal[ActionType.UPDATE_TASK]
    parameters: UpdateTaskParameters
    rationale: NonEmptyString


Proposal: TypeAlias = Annotated[
    CreateTaskProposal | UpdateTaskProposal,
    Field(discriminator="action"),
]


class DecisionOutcome(StrEnum):
    PROPOSAL = "proposal"
    UNSUPPORTED = "unsupported"
    MISSING_PARAMETERS = "missing_parameters"


class ModelDecision(StrictModel):
    """The only structured response accepted from the model."""

    outcome: DecisionOutcome
    proposal: Proposal | None = None
    reason: NonEmptyString | None = None
    missing_parameters: list[NonEmptyString] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_outcome_payload(self) -> "ModelDecision":
        if self.outcome is DecisionOutcome.PROPOSAL:
            if self.proposal is None:
                raise ValueError("proposal outcome requires a proposal")
            if self.missing_parameters:
                raise ValueError("proposal outcome cannot contain missing parameters")
        elif self.outcome is DecisionOutcome.UNSUPPORTED:
            if self.proposal is not None or self.reason is None:
                raise ValueError("unsupported outcome requires only a reason")
            if self.missing_parameters:
                raise ValueError("unsupported outcome cannot contain missing parameters")
        elif self.outcome is DecisionOutcome.MISSING_PARAMETERS:
            if self.proposal is not None or not self.missing_parameters:
                raise ValueError(
                    "missing_parameters outcome requires one or more missing parameters"
                )
        return self


class WorkflowStatus(StrEnum):
    PENDING_APPROVAL = "pending_approval"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


class ErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    UNSUPPORTED_REQUEST = "unsupported_request"
    MISSING_PARAMETERS = "missing_parameters"
    MALFORMED_MODEL_RESPONSE = "malformed_model_response"
    MODEL_UNAVAILABLE = "model_unavailable"
    MODEL_ERROR = "model_error"


class WorkflowError(StrictModel):
    code: ErrorCode
    message: NonEmptyString
    details: list[NonEmptyString] = Field(default_factory=list)


class WorkflowResult(StrictModel):
    status: WorkflowStatus
    request: NonEmptyString
    proposal: Proposal | None = None
    error: WorkflowError | None = None

    @model_validator(mode="after")
    def validate_status_payload(self) -> "WorkflowResult":
        if self.status is WorkflowStatus.PENDING_APPROVAL:
            if self.proposal is None or self.error is not None:
                raise ValueError("pending approval requires only a valid proposal")
        elif self.proposal is not None or self.error is None:
            raise ValueError("unsupported and failed results require only an error")
        return self


class GetTaskParameters(StrictModel):
    task_id: NonEmptyString


class TaskRecord(StrictModel):
    task_id: NonEmptyString
    title: NonEmptyString
    description: NonEmptyString | None = None
    assignee: NonEmptyString | None = None
    due_date: DateString | None = None
    status: TaskStatus
    created_at: datetime
    updated_at: datetime


class BusinessToolErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    TASK_NOT_FOUND = "task_not_found"
    DATABASE_ERROR = "database_error"


class BusinessToolError(StrictModel):
    code: BusinessToolErrorCode
    message: NonEmptyString
    details: list[NonEmptyString] = Field(default_factory=list)


class TaskListResult(StrictModel):
    tasks: list[TaskRecord] = Field(default_factory=list)
    limit: int = Field(ge=1, le=100)
    error: BusinessToolError | None = None


class TaskToolResult(StrictModel):
    success: bool
    task: TaskRecord | None = None
    error: BusinessToolError | None = None

    @model_validator(mode="after")
    def validate_result_payload(self) -> "TaskToolResult":
        if self.success:
            if self.task is None or self.error is not None:
                raise ValueError("successful tool results require only a task")
        elif self.task is not None or self.error is None:
            raise ValueError("failed tool results require only an error")
        return self


Fingerprint = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$"),
]


class ProposalStatus(StrEnum):
    PENDING = "pending"
    EXECUTING = "executing"
    COMPLETED = "completed"
    REJECTED = "rejected"
    FAILED = "failed"


class ExecutionProtocol(StrEnum):
    RECEIPT_V1 = "receipt_v1"


class ApprovalDecisionType(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class ApprovalDecision(StrictModel):
    proposal_id: NonEmptyString
    fingerprint: Fingerprint
    decision: ApprovalDecisionType


class BusinessRequest(StrictModel):
    request: NonEmptyString


class ProposalReference(StrictModel):
    proposal_id: NonEmptyString
    fingerprint: Fingerprint


class ApprovalErrorCode(StrEnum):
    PROPOSAL_NOT_FOUND = "proposal_not_found"
    FINGERPRINT_MISMATCH = "fingerprint_mismatch"
    PROPOSAL_MODIFIED = "proposal_modified"
    INVALID_STATE = "invalid_state"
    CHECKPOINT_ERROR = "checkpoint_error"
    DATABASE_ERROR = "database_error"
    EXECUTION_ERROR = "execution_error"
    RECOVERY_UNSAFE = "recovery_unsafe"


class ApprovalError(StrictModel):
    code: ApprovalErrorCode
    message: NonEmptyString
    details: list[NonEmptyString] = Field(default_factory=list)


class ProposalRecord(StrictModel):
    proposal_id: NonEmptyString
    request: NonEmptyString
    proposal: Proposal
    fingerprint: Fingerprint
    status: ProposalStatus
    execution_protocol: ExecutionProtocol | None = None
    result: TaskToolResult | None = None
    failure: ApprovalError | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_lifecycle_payload(self) -> "ProposalRecord":
        if self.status is ProposalStatus.COMPLETED:
            if self.result is None or not self.result.success or self.failure is not None:
                raise ValueError("completed proposals require a successful tool result")
        elif self.status is ProposalStatus.FAILED:
            failed_result = self.result is not None and not self.result.success
            if not failed_result and self.failure is None:
                raise ValueError("failed proposals require a failed result or failure")
        elif self.result is not None or self.failure is not None:
            raise ValueError("unfinished or rejected proposals cannot contain results")
        return self


class ExecutionReceipt(StrictModel):
    proposal_id: NonEmptyString
    fingerprint: Fingerprint
    result: TaskToolResult
    committed_at: datetime


class AuditEventType(StrEnum):
    PROPOSAL_SUBMITTED = "proposal_submitted"
    PROPOSAL_APPROVED = "proposal_approved"
    PROPOSAL_REJECTED = "proposal_rejected"
    EXECUTION_CLAIMED = "execution_claimed"
    EXECUTION_SUCCEEDED = "execution_succeeded"
    EXECUTION_FAILED = "execution_failed"
    RECOVERY_ATTEMPTED = "recovery_attempted"
    RECOVERY_COMPLETED = "recovery_completed"


class AuditEvent(StrictModel):
    event_id: int = Field(gt=0)
    proposal_id: NonEmptyString
    timestamp: datetime
    event_type: AuditEventType
    metadata: dict[str, Any]


class AuditHistory(StrictModel):
    proposal_id: NonEmptyString
    events: list[AuditEvent] = Field(default_factory=list)
    error: ApprovalError | None = None


class AuditTimelineResult(StrictModel):
    proposal_id: NonEmptyString
    events: list[AuditEvent] = Field(default_factory=list)
    total_events: int = Field(ge=0)
    limit: int = Field(ge=1, le=200)
    complete: bool
    error: ApprovalError | None = None


class RecoveryClassification(StrEnum):
    RECOVERABLE = "recoverable"
    COMMITTED_AWAITING_FINALIZATION = "committed_awaiting_finalization"
    MANUAL_INVESTIGATION = "manual_investigation"


class StalledExecution(StrictModel):
    proposal_id: NonEmptyString
    status: ProposalStatus
    fingerprint: Fingerprint | None = None
    last_activity_at: datetime | None = None
    receipt_present: bool
    classification: RecoveryClassification
    details: list[NonEmptyString] = Field(default_factory=list)


class StalledExecutionList(StrictModel):
    items: list[StalledExecution] = Field(default_factory=list)
    generated_at: datetime
    minimum_age_seconds: int | None = Field(default=None, ge=0)
    error: ApprovalError | None = None


class ProposalSubmissionResult(StrictModel):
    workflow_result: WorkflowResult
    proposal: ProposalRecord | None = None
    error: ApprovalError | None = None


class ProposalOperationResult(StrictModel):
    success: bool
    proposal: ProposalRecord | None = None
    error: ApprovalError | None = None

    @model_validator(mode="after")
    def validate_operation_payload(self) -> "ProposalOperationResult":
        if self.success:
            if self.proposal is None or self.error is not None:
                raise ValueError("successful operations require only a proposal")
        elif self.error is None:
            raise ValueError("failed operations require an error")
        return self
