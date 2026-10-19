from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from relaine.api import create_app
from relaine.api_server import main as api_main
from relaine.schemas import (
    CreateTaskParameters,
    CreateTaskProposal,
    DecisionOutcome,
    ModelDecision,
    TaskStatus,
    TaskUpdates,
    UpdateTaskParameters,
    UpdateTaskProposal,
)
from relaine.workflow import ProposalEngine


class MockModel:
    def __init__(self, decision: ModelDecision) -> None:
        self.decision = decision
        self.calls = 0

    def with_structured_output(self, schema, *, method: str):
        assert schema is ModelDecision
        assert method == "json_schema"
        return self

    def invoke(self, messages: Any) -> ModelDecision:
        self.calls += 1
        return self.decision


def create_decision(title: str = "API task") -> ModelDecision:
    return ModelDecision(
        outcome=DecisionOutcome.PROPOSAL,
        proposal=CreateTaskProposal(
            action="create_task",
            parameters=CreateTaskParameters(title=title),
            rationale="The request explicitly asks to create this task.",
        ),
    )


def client_for(database: Path, decision: ModelDecision):
    model = MockModel(decision)
    app = create_app(database, ProposalEngine(model))
    return TestClient(app), model


def submit(client: TestClient, request: str = "Create an API task") -> dict[str, Any]:
    response = client.post("/proposals", json={"request": request})
    assert response.status_code == 201
    payload = response.json()
    assert payload["workflow_result"]["status"] == "pending_approval"
    assert payload["proposal"]["status"] == "pending"
    return payload["proposal"]


def reference(proposal: dict[str, Any]) -> dict[str, str]:
    return {
        "proposal_id": proposal["proposal_id"],
        "fingerprint": proposal["fingerprint"],
    }


def task_count(database: Path) -> int:
    with sqlite3.connect(database) as connection:
        return connection.execute("SELECT count(*) FROM tasks").fetchone()[0]


def test_submit_and_inspect_persisted_proposal(tmp_path: Path) -> None:
    database = tmp_path / "api.sqlite3"
    client, model = client_for(database, create_decision())

    proposal = submit(client)
    inspected = client.get(f"/proposals/{proposal['proposal_id']}")

    assert inspected.status_code == 200
    assert inspected.json()["success"] is True
    assert inspected.json()["proposal"] == proposal
    assert model.calls == 1


def test_approval_executes_exact_stored_proposal(tmp_path: Path) -> None:
    database = tmp_path / "api.sqlite3"
    client, _ = client_for(database, create_decision("Execute through API"))
    proposal = submit(client)

    approved = client.post(
        f"/proposals/{proposal['proposal_id']}/approve",
        json=reference(proposal),
    )

    assert approved.status_code == 200
    body = approved.json()
    assert body["success"] is True
    assert body["proposal"]["status"] == "completed"
    assert body["proposal"]["result"]["task"]["title"] == "Execute through API"
    assert task_count(database) == 1


def test_rejection_changes_state_without_creating_task(tmp_path: Path) -> None:
    database = tmp_path / "api.sqlite3"
    client, _ = client_for(database, create_decision("Reject through API"))
    proposal = submit(client)

    rejected = client.post(
        f"/proposals/{proposal['proposal_id']}/reject",
        json=reference(proposal),
    )

    assert rejected.status_code == 200
    assert rejected.json()["proposal"]["status"] == "rejected"
    assert task_count(database) == 0


def test_invalid_fingerprint_is_conflict_and_remains_pending(tmp_path: Path) -> None:
    database = tmp_path / "api.sqlite3"
    client, _ = client_for(database, create_decision())
    proposal = submit(client)
    invalid_reference = reference(proposal)
    invalid_reference["fingerprint"] = "0" * 64

    denied = client.post(
        f"/proposals/{proposal['proposal_id']}/approve",
        json=invalid_reference,
    )
    inspected = client.get(f"/proposals/{proposal['proposal_id']}")

    assert denied.status_code == 409
    assert denied.json()["error"]["code"] == "fingerprint_mismatch"
    assert inspected.json()["proposal"]["status"] == "pending"
    assert task_count(database) == 0


def test_duplicate_approval_request_cannot_execute_twice(tmp_path: Path) -> None:
    database = tmp_path / "api.sqlite3"
    client, _ = client_for(database, create_decision())
    proposal = submit(client)
    url = f"/proposals/{proposal['proposal_id']}/approve"

    first = client.post(url, json=reference(proposal))
    duplicate = client.post(url, json=reference(proposal))

    assert first.status_code == 200
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "invalid_state"
    assert duplicate.json()["proposal"]["status"] == "completed"
    assert task_count(database) == 1


def test_api_rejects_replacement_arguments_and_tampered_storage(tmp_path: Path) -> None:
    database = tmp_path / "api.sqlite3"
    client, _ = client_for(database, create_decision())
    proposal = submit(client)
    replacement = reference(proposal)
    replacement["parameters"] = {"title": "Replacement"}  # type: ignore[assignment]

    invalid_body = client.post(
        f"/proposals/{proposal['proposal_id']}/approve",
        json=replacement,
    )
    assert invalid_body.status_code == 422

    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER proposal_snapshot_immutable")
        raw = connection.execute(
            "SELECT proposal_json FROM proposals WHERE proposal_id = ?",
            (proposal["proposal_id"],),
        ).fetchone()[0]
        modified = json.loads(raw)
        modified["parameters"]["title"] = "Tampered title"
        connection.execute(
            "UPDATE proposals SET proposal_json = ? WHERE proposal_id = ?",
            (json.dumps(modified), proposal["proposal_id"]),
        )

    tampered = client.post(
        f"/proposals/{proposal['proposal_id']}/approve",
        json=reference(proposal),
    )
    assert tampered.status_code == 409
    assert tampered.json()["error"]["code"] == "proposal_modified"
    assert task_count(database) == 0


def test_execution_failure_is_visible_in_http_status_and_body(tmp_path: Path) -> None:
    database = tmp_path / "api.sqlite3"
    decision = ModelDecision(
        outcome=DecisionOutcome.PROPOSAL,
        proposal=UpdateTaskProposal(
            action="update_task",
            parameters=UpdateTaskParameters(
                task_id="TASK-DOES-NOT-EXIST",
                changes=TaskUpdates(status=TaskStatus.DONE),
            ),
            rationale="The task ID and status are explicit.",
        ),
    )
    client, _ = client_for(database, decision)
    proposal = submit(client, "Mark TASK-DOES-NOT-EXIST done")

    failed = client.post(
        f"/proposals/{proposal['proposal_id']}/approve",
        json=reference(proposal),
    )

    assert failed.status_code == 422
    assert failed.json()["proposal"]["status"] == "failed"
    assert failed.json()["proposal"]["result"]["error"]["code"] == "task_not_found"


def test_server_entrypoint_is_fixed_to_loopback(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, Any] = {}
    sentinel_app = object()

    monkeypatch.setattr("relaine.api_server.create_app", lambda **kwargs: sentinel_app)

    def capture_run(application, **kwargs):
        captured["application"] = application
        captured.update(kwargs)

    monkeypatch.setattr("relaine.api_server.uvicorn.run", capture_run)

    exit_code = api_main(
        ["--db-path", str(tmp_path / "api.sqlite3"), "--port", "8765"]
    )

    assert exit_code == 0
    assert captured["application"] is sentinel_app
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8765
