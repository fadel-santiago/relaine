"""Human approval graph connecting immutable proposals to R2 tools."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from relaine.audit import AuditStore, AuditStoreError
from relaine.approval_storage import (
    ApprovalStoreError,
    FingerprintMismatchError,
    ProposalModifiedError,
    ProposalNotFoundError,
    ProposalStateConflictError,
    ProposalStore,
)
from relaine.execution_receipts import (
    ExecutionReceiptStore,
    ExecutionStoreError,
    ReceiptConflictError,
    UnsafeExecutionError,
)
from relaine.recovery_discovery import RecoveryDiscovery
from relaine.schemas import (
    ApprovalDecision,
    ApprovalDecisionType,
    ApprovalError,
    ApprovalErrorCode,
    AuditHistory,
    ExecutionProtocol,
    ProposalOperationResult,
    ProposalStatus,
    ProposalSubmissionResult,
    StalledExecutionList,
    WorkflowResult,
    WorkflowStatus,
)
from relaine.storage import SQLiteTaskStore


class ProposalProvider(Protocol):
    def propose(self, request: str) -> WorkflowResult: ...


class ApprovalState(TypedDict, total=False):
    proposal_id: str
    fingerprint: str
    decision: dict[str, Any]
    operation_error: dict[str, Any]


def build_approval_graph(
    proposal_store: ProposalStore,
    execution_store: ExecutionReceiptStore,
    checkpointer: SqliteSaver,
) -> Any:
    def await_approval(state: ApprovalState) -> dict[str, Any]:
        response = interrupt(
            {
                "proposal_id": state["proposal_id"],
                "fingerprint": state["fingerprint"],
                "required_decisions": ["approve", "reject"],
            },
            response_schema=ApprovalDecision,
        )
        decision = ApprovalDecision.model_validate(response)
        if (
            decision.proposal_id != state["proposal_id"]
            or decision.fingerprint != state["fingerprint"]
        ):
            raise ValueError("approval reference does not match interrupted proposal")
        return {"decision": decision.model_dump(mode="json")}

    def route_decision(state: ApprovalState) -> str:
        decision = ApprovalDecision.model_validate(state["decision"])
        return (
            "execute"
            if decision.decision is ApprovalDecisionType.APPROVE
            else "reject"
        )

    def execute(state: ApprovalState) -> dict[str, Any]:
        proposal_id = state["proposal_id"]
        fingerprint = state["fingerprint"]
        claimed = False
        receipt_committed = False
        try:
            record = proposal_store.claim_for_execution(proposal_id, fingerprint)
            claimed = True
            receipt = execution_store.execute(record)
            receipt_committed = True
            proposal_store.finish_execution(proposal_id, receipt.result)
            return {}
        except Exception as exc:
            failure = ApprovalError(
                code=ApprovalErrorCode.EXECUTION_ERROR,
                message="Controlled proposal execution failed.",
                details=[f"{exc.__class__.__name__}: {exc}"],
            )
            if claimed and not receipt_committed:
                try:
                    proposal_store.fail_execution(proposal_id, failure)
                except (ApprovalStoreError, ProposalStateConflictError):
                    pass
            return {"operation_error": failure.model_dump(mode="json")}

    def reject(state: ApprovalState) -> dict[str, Any]:
        proposal_store.reject(state["proposal_id"], state["fingerprint"])
        return {}

    graph = StateGraph(ApprovalState)
    graph.add_node("await_approval", await_approval)
    graph.add_node("execute", execute)
    graph.add_node("reject", reject)
    graph.add_edge(START, "await_approval")
    graph.add_conditional_edges(
        "await_approval",
        route_decision,
        {"execute": "execute", "reject": "reject"},
    )
    graph.add_edge("execute", END)
    graph.add_edge("reject", END)
    return graph.compile(checkpointer=checkpointer)


class ApprovalService:
    def __init__(
        self,
        database_path: str | Path,
        proposal_provider: ProposalProvider | None = None,
    ) -> None:
        self.database_path = Path(database_path)
        self.proposal_provider = proposal_provider
        self.proposal_store = ProposalStore(self.database_path)
        self.task_store = SQLiteTaskStore(self.database_path)
        self.execution_store = ExecutionReceiptStore(
            self.database_path,
            self.task_store,
        )
        self.audit_store = AuditStore(self.database_path)
        self.recovery_discovery = RecoveryDiscovery(self.proposal_store)

    @staticmethod
    def _config(proposal_id: str) -> dict[str, dict[str, str]]:
        return {"configurable": {"thread_id": proposal_id}}

    @staticmethod
    def _error_result(
        error: ApprovalError,
        proposal: Any = None,
    ) -> ProposalOperationResult:
        return ProposalOperationResult(
            success=False,
            proposal=proposal,
            error=error,
        )

    def submit(self, request: str) -> ProposalSubmissionResult:
        if self.proposal_provider is None:
            raise RuntimeError("submitting a request requires a proposal provider")
        workflow_result = self.proposal_provider.propose(request)
        if workflow_result.status is not WorkflowStatus.PENDING_APPROVAL:
            return ProposalSubmissionResult(workflow_result=workflow_result)
        if workflow_result.proposal is None:  # protected by WorkflowResult validation
            raise RuntimeError("pending workflow result has no proposal")

        try:
            record = self.proposal_store.save_pending(
                workflow_result.request,
                workflow_result.proposal,
            )
            with SqliteSaver.from_conn_string(str(self.database_path)) as checkpointer:
                graph = build_approval_graph(
                    self.proposal_store,
                    self.execution_store,
                    checkpointer,
                )
                graph.invoke(
                    {
                        "proposal_id": record.proposal_id,
                        "fingerprint": record.fingerprint,
                    },
                    config=self._config(record.proposal_id),
                )
        except Exception as exc:
            error = ApprovalError(
                code=ApprovalErrorCode.CHECKPOINT_ERROR,
                message="The proposal could not be checkpointed for approval.",
                details=[f"{exc.__class__.__name__}: {exc}"],
            )
            try:
                record = self.proposal_store.fail_execution(record.proposal_id, error)
            except Exception:
                record = None
            return ProposalSubmissionResult(
                workflow_result=workflow_result,
                proposal=record,
                error=error,
            )
        return ProposalSubmissionResult(
            workflow_result=workflow_result,
            proposal=self.proposal_store.get(record.proposal_id),
        )

    def inspect(self, proposal_id: str) -> ProposalOperationResult:
        try:
            record = self.proposal_store.get(proposal_id)
        except ProposalNotFoundError:
            return self._error_result(
                ApprovalError(
                    code=ApprovalErrorCode.PROPOSAL_NOT_FOUND,
                    message=f"Proposal {proposal_id} does not exist.",
                )
            )
        except ProposalModifiedError as exc:
            return self._error_result(
                ApprovalError(
                    code=ApprovalErrorCode.PROPOSAL_MODIFIED,
                    message="The stored proposal snapshot failed integrity validation.",
                    details=[str(exc)],
                )
            )
        except ApprovalStoreError as exc:
            return self._error_result(
                ApprovalError(
                    code=ApprovalErrorCode.DATABASE_ERROR,
                    message="The proposal database operation failed.",
                    details=[str(exc)],
                )
            )
        return ProposalOperationResult(success=True, proposal=record)

    def _preflight(
        self,
        proposal_id: str,
        fingerprint: str,
    ) -> ProposalOperationResult | None:
        try:
            self.proposal_store.validate_pending_reference(proposal_id, fingerprint)
        except ProposalNotFoundError:
            return self._error_result(
                ApprovalError(
                    code=ApprovalErrorCode.PROPOSAL_NOT_FOUND,
                    message=f"Proposal {proposal_id} does not exist.",
                )
            )
        except FingerprintMismatchError:
            current = self.inspect(proposal_id).proposal
            return self._error_result(
                ApprovalError(
                    code=ApprovalErrorCode.FINGERPRINT_MISMATCH,
                    message="The supplied fingerprint does not match the reviewed proposal.",
                ),
                current,
            )
        except ProposalModifiedError as exc:
            return self._error_result(
                ApprovalError(
                    code=ApprovalErrorCode.PROPOSAL_MODIFIED,
                    message="The stored proposal snapshot failed integrity validation.",
                    details=[str(exc)],
                )
            )
        except ProposalStateConflictError as exc:
            current = self.inspect(proposal_id).proposal
            return self._error_result(
                ApprovalError(
                    code=ApprovalErrorCode.INVALID_STATE,
                    message=f"Proposal cannot be decided while {exc.status.value}.",
                ),
                current,
            )
        except ApprovalStoreError as exc:
            return self._error_result(
                ApprovalError(
                    code=ApprovalErrorCode.DATABASE_ERROR,
                    message="The proposal database operation failed.",
                    details=[str(exc)],
                )
            )
        return None

    def decide(
        self,
        proposal_id: str,
        fingerprint: str,
        decision_type: ApprovalDecisionType,
    ) -> ProposalOperationResult:
        preflight = self._preflight(proposal_id, fingerprint)
        if preflight is not None:
            return preflight

        decision = ApprovalDecision(
            proposal_id=proposal_id,
            fingerprint=fingerprint,
            decision=decision_type,
        )
        try:
            with SqliteSaver.from_conn_string(str(self.database_path)) as checkpointer:
                graph = build_approval_graph(
                    self.proposal_store,
                    self.execution_store,
                    checkpointer,
                )
                graph_state = graph.invoke(
                    Command(resume=decision.model_dump(mode="json")),
                    config=self._config(proposal_id),
                )
            record = self.proposal_store.get(proposal_id)
        except Exception as exc:
            return self._error_result(
                ApprovalError(
                    code=ApprovalErrorCode.CHECKPOINT_ERROR,
                    message="The persisted approval workflow could not be resumed.",
                    details=[f"{exc.__class__.__name__}: {exc}"],
                )
            )
        if "operation_error" in graph_state:
            return self._error_result(
                ApprovalError.model_validate(graph_state["operation_error"]),
                record,
            )
        return ProposalOperationResult(success=True, proposal=record)

    def approve(self, proposal_id: str, fingerprint: str) -> ProposalOperationResult:
        return self.decide(
            proposal_id,
            fingerprint,
            ApprovalDecisionType.APPROVE,
        )

    def reject(self, proposal_id: str, fingerprint: str) -> ProposalOperationResult:
        return self.decide(
            proposal_id,
            fingerprint,
            ApprovalDecisionType.REJECT,
        )

    def recover(self, proposal_id: str, fingerprint: str) -> ProposalOperationResult:
        """Safely reconcile one interrupted receipt-based execution."""

        try:
            record = self.proposal_store.get(proposal_id)
        except ProposalNotFoundError:
            return self._error_result(
                ApprovalError(
                    code=ApprovalErrorCode.PROPOSAL_NOT_FOUND,
                    message=f"Proposal {proposal_id} does not exist.",
                )
            )
        except ProposalModifiedError as exc:
            return self._error_result(
                ApprovalError(
                    code=ApprovalErrorCode.PROPOSAL_MODIFIED,
                    message="The stored proposal snapshot failed integrity validation.",
                    details=[str(exc)],
                )
            )
        except ApprovalStoreError as exc:
            return self._error_result(
                ApprovalError(
                    code=ApprovalErrorCode.DATABASE_ERROR,
                    message="The proposal database operation failed.",
                    details=[str(exc)],
                )
            )

        if record.fingerprint != fingerprint:
            return self._error_result(
                ApprovalError(
                    code=ApprovalErrorCode.FINGERPRINT_MISMATCH,
                    message="The supplied fingerprint does not match the reviewed proposal.",
                ),
                record,
            )
        if record.status is not ProposalStatus.EXECUTING:
            return self._error_result(
                ApprovalError(
                    code=ApprovalErrorCode.INVALID_STATE,
                    message=(
                        "Only an executing proposal can be reconciled; "
                        f"this proposal is {record.status.value}."
                    ),
                ),
                record,
            )
        if record.execution_protocol is not ExecutionProtocol.RECEIPT_V1:
            return self._error_result(
                ApprovalError(
                    code=ApprovalErrorCode.RECOVERY_UNSAFE,
                    message=(
                        "Execution predates durable receipts; retry safety "
                        "cannot be established."
                    ),
                ),
                record,
            )

        try:
            receipt_present = self.execution_store.get(proposal_id) is not None
            self.audit_store.record_recovery_attempt(
                proposal_id,
                record.proposal.action.value,
                receipt_present,
            )
            receipt = self.execution_store.execute(record)
        except (ReceiptConflictError, UnsafeExecutionError) as exc:
            return self._error_result(
                ApprovalError(
                    code=ApprovalErrorCode.RECOVERY_UNSAFE,
                    message="The execution receipt cannot prove a safe recovery.",
                    details=[str(exc)],
                ),
                record,
            )
        except ExecutionStoreError as exc:
            return self._error_result(
                ApprovalError(
                    code=ApprovalErrorCode.DATABASE_ERROR,
                    message="The execution receipt database operation failed.",
                    details=[str(exc)],
                ),
                record,
            )
        except AuditStoreError as exc:
            return self._error_result(
                ApprovalError(
                    code=ApprovalErrorCode.DATABASE_ERROR,
                    message="The recovery attempt could not be audited.",
                    details=[str(exc)],
                ),
                record,
            )

        try:
            completed = self.proposal_store.finish_execution(
                proposal_id,
                receipt.result,
                recovery=True,
            )
        except ProposalStateConflictError:
            try:
                current = self.proposal_store.get(proposal_id)
            except (ApprovalStoreError, ProposalNotFoundError, ProposalModifiedError) as exc:
                return self._error_result(
                    ApprovalError(
                        code=ApprovalErrorCode.DATABASE_ERROR,
                        message="The reconciled proposal could not be read.",
                        details=[str(exc)],
                    )
                )
            if (
                current.status in {ProposalStatus.COMPLETED, ProposalStatus.FAILED}
                and current.result == receipt.result
            ):
                return ProposalOperationResult(success=True, proposal=current)
            return self._error_result(
                ApprovalError(
                    code=ApprovalErrorCode.RECOVERY_UNSAFE,
                    message="Concurrent reconciliation produced conflicting state.",
                ),
                current,
            )
        except ApprovalStoreError as exc:
            return self._error_result(
                ApprovalError(
                    code=ApprovalErrorCode.DATABASE_ERROR,
                    message="The recovered execution result could not be persisted.",
                    details=[str(exc)],
                ),
                record,
            )
        return ProposalOperationResult(success=True, proposal=completed)

    def history(self, proposal_id: str) -> AuditHistory:
        """Return the append-only lifecycle events for one proposal."""

        try:
            return AuditHistory(
                proposal_id=proposal_id,
                events=self.audit_store.history(proposal_id),
            )
        except AuditStoreError as exc:
            return AuditHistory(
                proposal_id=proposal_id,
                error=ApprovalError(
                    code=ApprovalErrorCode.DATABASE_ERROR,
                    message="The proposal audit history could not be retrieved.",
                    details=[str(exc)],
                ),
            )

    def list_stalled_executions(
        self,
        minimum_age_seconds: int | None = None,
    ) -> StalledExecutionList:
        """List and classify unfinished executions without changing state."""

        try:
            return self.recovery_discovery.list(minimum_age_seconds)
        except (ApprovalStoreError, ValueError) as exc:
            return StalledExecutionList(
                generated_at=datetime.now(UTC),
                minimum_age_seconds=(
                    minimum_age_seconds
                    if minimum_age_seconds is None or minimum_age_seconds >= 0
                    else None
                ),
                error=ApprovalError(
                    code=ApprovalErrorCode.DATABASE_ERROR,
                    message="Stalled execution discovery failed.",
                    details=[str(exc)],
                ),
            )
