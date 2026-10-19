"""LangGraph implementation of the R1 proposal-only workflow."""

from __future__ import annotations

import json
from typing import Any, Protocol, TypedDict, cast

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_ollama import ChatOllama
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from relaine.config import OllamaConfig
from relaine.schemas import (
    DecisionOutcome,
    ErrorCode,
    ModelDecision,
    WorkflowError,
    WorkflowResult,
    WorkflowStatus,
)


SYSTEM_PROMPT = """You are RELAINE R1, a proposal-only workflow planner.

Classify the business request and return exactly the supplied JSON schema.
Never claim to execute work.

Supported actions (these MUST use outcome=proposal when their required parameters
are present):
- A request to create or add a task maps to action=create_task. It requires a
  concrete title. Optional fields are description, assignee, and due_date
  (YYYY-MM-DD). Text introduced by "titled" or "named" is the task title, even
  when that title begins with a verb.
- A request to change a task's status maps to action=update_task. It requires a
  task_id and one explicit status change. The only valid statuses are todo,
  in_progress, and done.

Classification rules:
- Do not invent a task ID, title, date, assignee, or change that the user omitted.
- Use outcome=missing_parameters when a supported action lacks required input.
- Use outcome=unsupported only for every other action, including deleting tasks,
  executing tools, reading data, approvals, messages, research, and integrations.
- When outcome=proposal, the top-level proposal field MUST be a non-null object
  containing action, parameters, and rationale. Put the brief explanation in
  proposal.rationale and set the top-level reason to null.
- A proposal rationale must be grounded only in the request.

Examples:
- "Create a task titled Verify the report" is a create_task proposal whose title
  is "Verify the report". Its shape is
  {"outcome":"proposal","proposal":{"action":"create_task","parameters":
  {"title":"Verify the report"},"rationale":"The request asks to create this
  task."},"reason":null,"missing_parameters":[]}.
- "Mark task TASK-42 as done" is an update_task proposal for TASK-42 with status
  done.
"""

MAX_REQUEST_LENGTH = 10_000


class StructuredModel(Protocol):
    def invoke(self, input: Any) -> Any: ...


class ChatModel(Protocol):
    def with_structured_output(
        self,
        schema: type[ModelDecision],
        *,
        method: str,
    ) -> StructuredModel: ...


class WorkflowState(TypedDict, total=False):
    request: str
    raw_decision: Any
    decision: ModelDecision
    failure: WorkflowError
    result: WorkflowResult


def _model_failure(exc: Exception) -> WorkflowError:
    text = str(exc).strip() or exc.__class__.__name__
    diagnostic = f"{exc.__class__.__name__}: {text}"
    unavailable_markers = (
        "connecterror",
        "connectionerror",
        "connection refused",
        "failed to connect",
        "model not found",
        "not found",
        "ollama endpoint",
    )
    code = (
        ErrorCode.MODEL_UNAVAILABLE
        if any(marker in diagnostic.lower() for marker in unavailable_markers)
        else ErrorCode.MODEL_ERROR
    )
    message = (
        "The configured Ollama service or model is unavailable."
        if code is ErrorCode.MODEL_UNAVAILABLE
        else "The Ollama model could not generate a proposal."
    )
    return WorkflowError(code=code, message=message, details=[diagnostic])


def _validation_details(exc: ValidationError) -> list[str]:
    return [
        f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
        for error in exc.errors(include_url=False)
    ]


def build_workflow(model: ChatModel) -> Any:
    """Compile the R1 graph around a model that supports structured output."""

    structured_model = model.with_structured_output(
        ModelDecision,
        method="json_schema",
    )

    def validate_request(state: WorkflowState) -> dict[str, Any]:
        request = state.get("request", "").strip()
        if not request:
            return {
                "request": "(empty request)",
                "failure": WorkflowError(
                    code=ErrorCode.INVALID_REQUEST,
                    message="The business request cannot be empty.",
                ),
            }
        if len(request) > MAX_REQUEST_LENGTH:
            return {
                "request": request,
                "failure": WorkflowError(
                    code=ErrorCode.INVALID_REQUEST,
                    message=f"The business request exceeds {MAX_REQUEST_LENGTH} characters.",
                ),
            }
        return {"request": request}

    def after_request_validation(state: WorkflowState) -> str:
        return "failed" if "failure" in state else "generate_proposal"

    def generate_proposal(state: WorkflowState) -> dict[str, Any]:
        try:
            raw_decision = structured_model.invoke(
                [
                    SystemMessage(content=SYSTEM_PROMPT),
                    HumanMessage(content=state["request"]),
                ]
            )
        except Exception as exc:  # provider failures become explicit workflow results
            return {"failure": _model_failure(exc)}
        return {"raw_decision": raw_decision}

    def validate_proposal(state: WorkflowState) -> dict[str, Any]:
        if "failure" in state:
            return {}
        try:
            raw_decision = state.get("raw_decision")
            if isinstance(raw_decision, ModelDecision):
                decision = raw_decision
            elif isinstance(raw_decision, (str, bytes, bytearray)):
                decision = ModelDecision.model_validate_json(raw_decision)
            else:
                decision = ModelDecision.model_validate_json(json.dumps(raw_decision))
        except (TypeError, ValueError, ValidationError) as exc:
            details = (
                _validation_details(exc)
                if isinstance(exc, ValidationError)
                else [f"{exc.__class__.__name__}: {exc}"]
            )
            return {
                "failure": WorkflowError(
                    code=ErrorCode.MALFORMED_MODEL_RESPONSE,
                    message="The model response did not match the required proposal schema.",
                    details=details,
                )
            }

        if decision.outcome is DecisionOutcome.MISSING_PARAMETERS:
            return {
                "decision": decision,
                "failure": WorkflowError(
                    code=ErrorCode.MISSING_PARAMETERS,
                    message="The request is missing parameters required for a proposal.",
                    details=decision.missing_parameters,
                ),
            }
        return {"decision": decision}

    def route_result(state: WorkflowState) -> str:
        if "failure" in state:
            return "failed"
        if state["decision"].outcome is DecisionOutcome.UNSUPPORTED:
            return "unsupported"
        return "pending_approval"

    def pending_approval(state: WorkflowState) -> dict[str, Any]:
        decision = state["decision"]
        return {
            "result": WorkflowResult(
                status=WorkflowStatus.PENDING_APPROVAL,
                request=state["request"],
                proposal=decision.proposal,
            )
        }

    def unsupported(state: WorkflowState) -> dict[str, Any]:
        decision = state["decision"]
        return {
            "result": WorkflowResult(
                status=WorkflowStatus.UNSUPPORTED,
                request=state["request"],
                error=WorkflowError(
                    code=ErrorCode.UNSUPPORTED_REQUEST,
                    message=decision.reason or "The requested action is not supported in R1.",
                ),
            )
        }

    def failed(state: WorkflowState) -> dict[str, Any]:
        return {
            "result": WorkflowResult(
                status=WorkflowStatus.FAILED,
                request=state["request"],
                error=state["failure"],
            )
        }

    graph = StateGraph(WorkflowState)
    graph.add_node("validate_request", validate_request)
    graph.add_node("generate_proposal", generate_proposal)
    graph.add_node("validate_proposal", validate_proposal)
    graph.add_node("pending_approval", pending_approval)
    graph.add_node("unsupported", unsupported)
    graph.add_node("failed", failed)

    graph.add_edge(START, "validate_request")
    graph.add_conditional_edges(
        "validate_request",
        after_request_validation,
        {"generate_proposal": "generate_proposal", "failed": "failed"},
    )
    graph.add_edge("generate_proposal", "validate_proposal")
    graph.add_conditional_edges(
        "validate_proposal",
        route_result,
        {
            "pending_approval": "pending_approval",
            "unsupported": "unsupported",
            "failed": "failed",
        },
    )
    graph.add_edge("pending_approval", END)
    graph.add_edge("unsupported", END)
    graph.add_edge("failed", END)
    return graph.compile()


class ProposalEngine:
    def __init__(self, model: ChatModel) -> None:
        self._graph = build_workflow(model)

    def propose(self, request: str) -> WorkflowResult:
        state = self._graph.invoke({"request": request})
        return WorkflowResult.model_validate(state["result"])


def build_default_engine(config: OllamaConfig | None = None) -> ProposalEngine:
    resolved = config or OllamaConfig.from_env()
    model = ChatOllama(
        model=resolved.model,
        base_url=resolved.base_url,
        temperature=0,
    )
    return ProposalEngine(cast(ChatModel, model))
