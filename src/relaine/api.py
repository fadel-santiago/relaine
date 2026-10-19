"""Minimal local FastAPI surface over the controlled approval service."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Query, Response, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from relaine.approval_workflow import ApprovalService, ProposalProvider
from relaine.config import OllamaConfig
from relaine.schemas import (
    ApprovalError,
    ApprovalErrorCode,
    AuditEvent,
    AuditTimelineResult,
    BusinessToolError,
    BusinessToolErrorCode,
    BusinessRequest,
    ErrorCode,
    ProposalOperationResult,
    ProposalReference,
    ProposalStatus,
    ProposalSubmissionResult,
    TaskListResult,
    WorkflowStatus,
)
from relaine.storage import (
    DEFAULT_TASK_LIST_LIMIT,
    MAX_TASK_LIST_LIMIT,
    TaskStoreConfig,
    TaskStoreError,
)
from relaine.workflow import build_default_engine


MAX_AUDIT_TIMELINE_EVENTS = 200
SAFE_AUDIT_METADATA_KEYS = frozenset(
    {
        "action",
        "error_code",
        "execution_protocol",
        "fingerprint",
        "outcome",
        "receipt_present",
        "task_id",
        "task_status",
    }
)


def _submission_status(result: ProposalSubmissionResult) -> int:
    if result.error is not None:
        return status.HTTP_500_INTERNAL_SERVER_ERROR
    workflow = result.workflow_result
    if workflow.status is WorkflowStatus.PENDING_APPROVAL:
        return status.HTTP_201_CREATED
    if workflow.error is None:
        return status.HTTP_500_INTERNAL_SERVER_ERROR
    if workflow.error.code is ErrorCode.MODEL_UNAVAILABLE:
        return status.HTTP_503_SERVICE_UNAVAILABLE
    if workflow.error.code in {
        ErrorCode.MODEL_ERROR,
        ErrorCode.MALFORMED_MODEL_RESPONSE,
    }:
        return status.HTTP_502_BAD_GATEWAY
    return status.HTTP_422_UNPROCESSABLE_CONTENT


def _operation_status(result: ProposalOperationResult) -> int:
    if result.success:
        if result.proposal is not None and result.proposal.status is ProposalStatus.FAILED:
            return status.HTTP_422_UNPROCESSABLE_CONTENT
        return status.HTTP_200_OK
    if result.error is None:
        return status.HTTP_500_INTERNAL_SERVER_ERROR
    if result.error.code is ApprovalErrorCode.PROPOSAL_NOT_FOUND:
        return status.HTTP_404_NOT_FOUND
    if result.error.code in {
        ApprovalErrorCode.FINGERPRINT_MISMATCH,
        ApprovalErrorCode.PROPOSAL_MODIFIED,
        ApprovalErrorCode.INVALID_STATE,
        ApprovalErrorCode.RECOVERY_UNSAFE,
    }:
        return status.HTTP_409_CONFLICT
    return status.HTTP_500_INTERNAL_SERVER_ERROR


def _reference_mismatch() -> ProposalOperationResult:
    return ProposalOperationResult(
        success=False,
        error=ApprovalError(
            code=ApprovalErrorCode.INVALID_STATE,
            message="The request proposal ID does not match the route proposal ID.",
        ),
    )


def _safe_audit_event(event: AuditEvent) -> AuditEvent:
    return event.model_copy(
        update={
            "metadata": {
                key: value
                for key, value in event.metadata.items()
                if key in SAFE_AUDIT_METADATA_KEYS
            }
        }
    )


def create_app(
    database_path: str | Path | None = None,
    proposal_provider: ProposalProvider | None = None,
    ollama_config: OllamaConfig | None = None,
) -> FastAPI:
    """Build the API with an injectable provider for model-isolated tests."""

    path = Path(database_path) if database_path is not None else TaskStoreConfig.from_env().path
    provider = proposal_provider or build_default_engine(ollama_config)
    service = ApprovalService(path, provider)
    frontend_directory = Path(__file__).with_name("static")
    app = FastAPI(
        title="RELAINE Workflow API",
        version="5a",
        docs_url=None,
        redoc_url=None,
    )
    app.state.approval_service = service
    app.mount(
        "/static",
        StaticFiles(directory=frontend_directory),
        name="static",
    )

    @app.get("/", include_in_schema=False)
    def frontend() -> FileResponse:
        return FileResponse(
            frontend_directory / "index.html",
            headers={
                "Content-Security-Policy": (
                    "default-src 'self'; script-src 'self'; style-src 'self'; "
                    "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
                    "frame-ancestors 'none'; form-action 'self'"
                ),
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post(
        "/proposals",
        response_model=ProposalSubmissionResult,
        status_code=status.HTTP_201_CREATED,
    )
    def submit_proposal(payload: BusinessRequest, response: Response) -> ProposalSubmissionResult:
        result = service.submit(payload.request)
        response.status_code = _submission_status(result)
        return result

    @app.get(
        "/tasks",
        response_model=TaskListResult,
    )
    def list_tasks(
        response: Response,
        limit: int = Query(
            default=DEFAULT_TASK_LIST_LIMIT,
            ge=1,
            le=MAX_TASK_LIST_LIMIT,
        ),
    ) -> TaskListResult:
        try:
            tasks = service.task_store.list_tasks(limit)
        except TaskStoreError as exc:
            response.status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
            return TaskListResult(
                limit=limit,
                error=BusinessToolError(
                    code=BusinessToolErrorCode.DATABASE_ERROR,
                    message="The task database operation failed.",
                    details=[str(exc)],
                ),
            )
        return TaskListResult(tasks=tasks, limit=limit)

    @app.get(
        "/proposals/{proposal_id}/history",
        response_model=AuditTimelineResult,
    )
    def proposal_history(
        proposal_id: str,
        response: Response,
    ) -> AuditTimelineResult:
        inspected = service.inspect(proposal_id)
        if not inspected.success:
            response.status_code = _operation_status(inspected)
            return AuditTimelineResult(
                proposal_id=proposal_id,
                total_events=0,
                limit=MAX_AUDIT_TIMELINE_EVENTS,
                complete=False,
                error=inspected.error,
            )

        history = service.history(proposal_id)
        if history.error is not None:
            response.status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
            return AuditTimelineResult(
                proposal_id=proposal_id,
                total_events=0,
                limit=MAX_AUDIT_TIMELINE_EVENTS,
                complete=False,
                error=history.error,
            )

        ordered = sorted(history.events, key=lambda event: event.event_id)
        total_events = len(ordered)
        visible = ordered[-MAX_AUDIT_TIMELINE_EVENTS:]
        return AuditTimelineResult(
            proposal_id=proposal_id,
            events=[_safe_audit_event(event) for event in visible],
            total_events=total_events,
            limit=MAX_AUDIT_TIMELINE_EVENTS,
            complete=total_events <= MAX_AUDIT_TIMELINE_EVENTS,
        )

    @app.get(
        "/proposals/{proposal_id}",
        response_model=ProposalOperationResult,
    )
    def inspect_proposal(
        proposal_id: str,
        response: Response,
    ) -> ProposalOperationResult:
        result = service.inspect(proposal_id)
        response.status_code = _operation_status(result)
        return result

    def decide(
        proposal_id: str,
        reference: ProposalReference,
        response: Response,
        *,
        approve: bool,
    ) -> ProposalOperationResult:
        if reference.proposal_id != proposal_id:
            result = _reference_mismatch()
        elif approve:
            result = service.approve(proposal_id, reference.fingerprint)
        else:
            result = service.reject(proposal_id, reference.fingerprint)
        response.status_code = _operation_status(result)
        return result

    @app.post(
        "/proposals/{proposal_id}/approve",
        response_model=ProposalOperationResult,
    )
    def approve_proposal(
        proposal_id: str,
        reference: ProposalReference,
        response: Response,
    ) -> ProposalOperationResult:
        return decide(proposal_id, reference, response, approve=True)

    @app.post(
        "/proposals/{proposal_id}/reject",
        response_model=ProposalOperationResult,
    )
    def reject_proposal(
        proposal_id: str,
        reference: ProposalReference,
        response: Response,
    ) -> ProposalOperationResult:
        return decide(proposal_id, reference, response, approve=False)

    return app
