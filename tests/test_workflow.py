from __future__ import annotations

from typing import Any

import pytest

from relaine.config import OllamaConfig
from relaine.schemas import ActionType, ErrorCode, ModelDecision, WorkflowStatus
from relaine.workflow import ProposalEngine


class MockStructuredModel:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[Any] = []

    def invoke(self, input: Any) -> Any:
        self.calls.append(input)
        if self.error is not None:
            raise self.error
        return self.response


class MockChatModel:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.structured = MockStructuredModel(response, error)
        self.schema: type[ModelDecision] | None = None
        self.method: str | None = None

    def with_structured_output(
        self,
        schema: type[ModelDecision],
        *,
        method: str,
    ) -> MockStructuredModel:
        self.schema = schema
        self.method = method
        return self.structured


def test_default_ollama_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RELAINE_OLLAMA_MODEL", raising=False)
    monkeypatch.delenv("RELAINE_OLLAMA_BASE_URL", raising=False)

    config = OllamaConfig.from_env()

    assert config.model == "gpt-oss:20b"
    assert config.base_url == "http://127.0.0.1:11434"


def test_valid_create_task_routes_to_pending_approval() -> None:
    model = MockChatModel(
        {
            "outcome": "proposal",
            "proposal": {
                "action": "create_task",
                "parameters": {
                    "title": "Prepare Q4 report",
                    "description": "Summarize revenue and costs",
                    "assignee": None,
                    "due_date": "2026-10-31",
                },
                "rationale": "The request explicitly asks for a new task.",
            },
            "reason": None,
            "missing_parameters": [],
        }
    )

    result = ProposalEngine(model).propose(
        "Create a task titled Prepare Q4 report, due 2026-10-31."
    )

    assert result.status is WorkflowStatus.PENDING_APPROVAL
    assert result.proposal is not None
    assert result.proposal.action is ActionType.CREATE_TASK
    assert result.proposal.parameters.title == "Prepare Q4 report"
    assert result.error is None
    assert model.schema is ModelDecision
    assert model.method == "json_schema"


def test_valid_update_task_routes_to_pending_approval() -> None:
    model = MockChatModel(
        {
            "outcome": "proposal",
            "proposal": {
                "action": "update_task",
                "parameters": {
                    "task_id": "TASK-42",
                    "changes": {
                        "title": None,
                        "description": None,
                        "status": "done",
                        "assignee": None,
                        "due_date": None,
                    },
                },
                "rationale": "The task and requested status are explicit.",
            },
            "reason": None,
            "missing_parameters": [],
        }
    )

    result = ProposalEngine(model).propose("Mark task TASK-42 as done.")

    assert result.status is WorkflowStatus.PENDING_APPROVAL
    assert result.proposal is not None
    assert result.proposal.action is ActionType.UPDATE_TASK
    assert result.proposal.parameters.task_id == "TASK-42"
    assert result.proposal.parameters.changes.status == "done"


def test_unsupported_request_is_not_a_proposal() -> None:
    model = MockChatModel(
        {
            "outcome": "unsupported",
            "proposal": None,
            "reason": "Deleting tasks is outside R1.",
            "missing_parameters": [],
        }
    )

    result = ProposalEngine(model).propose("Delete task TASK-42.")

    assert result.status is WorkflowStatus.UNSUPPORTED
    assert result.proposal is None
    assert result.error is not None
    assert result.error.code is ErrorCode.UNSUPPORTED_REQUEST


def test_missing_required_parameters_fails_explicitly() -> None:
    model = MockChatModel(
        {
            "outcome": "missing_parameters",
            "proposal": None,
            "reason": "The task to update is not identified.",
            "missing_parameters": ["task_id"],
        }
    )

    result = ProposalEngine(model).propose("Mark the task as done.")

    assert result.status is WorkflowStatus.FAILED
    assert result.proposal is None
    assert result.error is not None
    assert result.error.code is ErrorCode.MISSING_PARAMETERS
    assert result.error.details == ["task_id"]


@pytest.mark.parametrize(
    "response",
    [
        {"outcome": "proposal", "proposal": None},
        {
            "outcome": "proposal",
            "proposal": {
                "action": "create_task",
                "parameters": {},
                "rationale": "Missing title must be rejected.",
            },
            "reason": None,
            "missing_parameters": [],
        },
        {
            "outcome": "proposal",
            "proposal": {
                "action": "delete_task",
                "parameters": {"task_id": "TASK-42"},
                "rationale": "This action is forbidden.",
            },
            "reason": None,
            "missing_parameters": [],
        },
    ],
)
def test_malformed_or_forbidden_model_response_fails(response: Any) -> None:
    result = ProposalEngine(MockChatModel(response)).propose("A request")

    assert result.status is WorkflowStatus.FAILED
    assert result.proposal is None
    assert result.error is not None
    assert result.error.code is ErrorCode.MALFORMED_MODEL_RESPONSE


def test_unavailable_ollama_fails_without_a_proposal() -> None:
    model = MockChatModel(error=ConnectionError("connection refused"))

    result = ProposalEngine(model).propose("Create a task titled Review budget.")

    assert result.status is WorkflowStatus.FAILED
    assert result.proposal is None
    assert result.error is not None
    assert result.error.code is ErrorCode.MODEL_UNAVAILABLE


def test_empty_request_fails_before_model_call() -> None:
    model = MockChatModel(response={})

    result = ProposalEngine(model).propose("   ")

    assert result.status is WorkflowStatus.FAILED
    assert result.error is not None
    assert result.error.code is ErrorCode.INVALID_REQUEST
    assert model.structured.calls == []
