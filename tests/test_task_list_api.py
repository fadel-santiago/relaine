from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from relaine.api import create_app
from relaine.schemas import (
    CreateTaskParameters,
    CreateTaskProposal,
    DecisionOutcome,
    ModelDecision,
    TaskStatus,
)
from relaine.storage import SQLiteTaskStore, TaskStoreError
from relaine.workflow import ProposalEngine


class MockModel:
    def with_structured_output(self, schema, *, method: str):
        assert schema is ModelDecision
        assert method == "json_schema"
        return self

    def invoke(self, messages: Any) -> ModelDecision:
        return ModelDecision(
            outcome=DecisionOutcome.PROPOSAL,
            proposal=CreateTaskProposal(
                action="create_task",
                parameters=CreateTaskParameters(title="Created through approval"),
                rationale="The request explicitly asks to create this task.",
            ),
        )


def api_client(database: Path) -> TestClient:
    return TestClient(create_app(database, ProposalEngine(MockModel())))


def test_empty_task_list_is_a_valid_read_only_response(tmp_path: Path) -> None:
    response = api_client(tmp_path / "tasks.sqlite3").get("/tasks")

    assert response.status_code == 200
    assert response.json() == {"tasks": [], "limit": 50, "error": None}


def test_task_list_returns_real_records_in_deterministic_bounded_order(
    tmp_path: Path,
) -> None:
    database = tmp_path / "tasks.sqlite3"
    store = SQLiteTaskStore(database)
    first = store.insert_task(CreateTaskParameters(title="First task"))
    second = store.insert_task(CreateTaskParameters(title="Second task"))
    third = store.insert_task(CreateTaskParameters(title="Third task"))
    store.update_task_status(second.task_id, TaskStatus.IN_PROGRESS)
    store.update_task_status(third.task_id, TaskStatus.DONE)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE tasks SET created_at = ? WHERE task_id = ?",
            ("2026-01-01T00:00:00+00:00", first.task_id),
        )
        connection.execute(
            "UPDATE tasks SET created_at = ? WHERE task_id = ?",
            ("2026-01-02T00:00:00+00:00", second.task_id),
        )
        connection.execute(
            "UPDATE tasks SET created_at = ? WHERE task_id = ?",
            ("2026-01-03T00:00:00+00:00", third.task_id),
        )

    response = api_client(database).get("/tasks?limit=2")

    assert response.status_code == 200
    payload = response.json()
    assert payload["limit"] == 2
    assert [task["task_id"] for task in payload["tasks"]] == [
        third.task_id,
        second.task_id,
    ]
    assert [task["title"] for task in payload["tasks"]] == [
        "Third task",
        "Second task",
    ]
    assert [task["status"] for task in payload["tasks"]] == [
        "done",
        "in_progress",
    ]


def test_task_list_persists_across_application_instances(tmp_path: Path) -> None:
    database = tmp_path / "tasks.sqlite3"
    task = SQLiteTaskStore(database).insert_task(
        CreateTaskParameters(title="Persistent dashboard task")
    )

    first = api_client(database).get("/tasks")
    second = api_client(database).get("/tasks")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["tasks"] == second.json()["tasks"]
    assert second.json()["tasks"][0]["task_id"] == task.task_id


def test_approved_existing_api_execution_appears_in_task_list(tmp_path: Path) -> None:
    database = tmp_path / "tasks.sqlite3"
    client = api_client(database)
    submitted = client.post(
        "/proposals",
        json={"request": "Create a task titled Created through approval"},
    )
    proposal = submitted.json()["proposal"]

    approved = client.post(
        f"/proposals/{proposal['proposal_id']}/approve",
        json={
            "proposal_id": proposal["proposal_id"],
            "fingerprint": proposal["fingerprint"],
        },
    )
    listed = client.get("/tasks")

    assert approved.status_code == 200
    assert approved.json()["proposal"]["status"] == "completed"
    assert listed.status_code == 200
    assert len(listed.json()["tasks"]) == 1
    assert listed.json()["tasks"][0]["task_id"] == approved.json()["proposal"][
        "result"
    ]["task"]["task_id"]


def test_task_list_validates_limit_and_surfaces_database_failures(
    tmp_path: Path,
) -> None:
    app = create_app(tmp_path / "tasks.sqlite3", ProposalEngine(MockModel()))
    client = TestClient(app)

    assert client.get("/tasks?limit=0").status_code == 422
    assert client.get("/tasks?limit=101").status_code == 422

    def fail_list(limit: int):
        raise TaskStoreError("simulated task list failure")

    app.state.approval_service.task_store.list_tasks = fail_list  # type: ignore[method-assign]
    failed = client.get("/tasks")

    assert failed.status_code == 500
    assert failed.json()["tasks"] == []
    assert failed.json()["error"]["code"] == "database_error"
