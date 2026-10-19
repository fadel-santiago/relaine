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
)
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
                parameters=CreateTaskParameters(title="Browser-created task"),
                rationale="The request explicitly asks to create this task.",
            ),
        )


def browser_client(database: Path) -> TestClient:
    return TestClient(create_app(database, ProposalEngine(MockModel())))


def test_frontend_is_served_with_accessible_local_shell(tmp_path: Path) -> None:
    response = browser_client(tmp_path / "frontend.sqlite3").get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert "connect-src 'self'" in response.headers["content-security-policy"]
    assert "access-control-allow-origin" not in response.headers
    assert '<label for="business-request">Request</label>' in response.text
    assert 'id="approve-button"' in response.text
    assert 'id="reject-button"' in response.text
    assert 'id="task-list"' in response.text
    assert 'id="refresh-tasks-button"' in response.text
    assert 'id="history-list"' in response.text
    assert 'id="history-state"' in response.text
    assert "Loading persisted tasks" in response.text
    assert 'aria-live="polite"' in response.text
    assert 'src="/static/app.js"' in response.text
    assert 'href="/static/styles.css"' in response.text


def test_frontend_assets_are_buildless_safe_and_responsive(tmp_path: Path) -> None:
    client = browser_client(tmp_path / "frontend.sqlite3")

    javascript = client.get("/static/app.js")
    stylesheet = client.get("/static/styles.css")

    assert javascript.status_code == 200
    assert stylesheet.status_code == 200
    assert "textContent" in javascript.text
    assert "innerHTML" not in javascript.text
    assert 'fetch(url, options)' in javascript.text
    assert "proposal_id: currentProposal.proposal_id" in javascript.text
    assert "fingerprint: currentProposal.fingerprint" in javascript.text
    assert "JSON.stringify(reviewed)" in javascript.text
    assert 'requestJson("/tasks?limit=50"' in javascript.text
    assert "/history`" in javascript.text
    assert 'document.createElement("li")' in javascript.text
    assert "currentProposal.proposal.parameters" not in javascript.text
    assert "@media (max-width: 820px)" in stylesheet.text
    assert "@media (max-width: 560px)" in stylesheet.text
    assert '.task-status[data-status="todo"]' in stylesheet.text
    assert '.task-status[data-status="in_progress"]' in stylesheet.text
    assert '.task-status[data-status="done"]' in stylesheet.text
    assert '.history-event[data-event="execution_succeeded"]' in stylesheet.text
    assert "http://" not in javascript.text
    assert "https://" not in javascript.text


def test_existing_submit_inspect_and_approval_api_remains_unchanged(
    tmp_path: Path,
) -> None:
    database = tmp_path / "frontend.sqlite3"
    client = browser_client(database)

    submitted = client.post(
        "/proposals",
        json={"request": "Create a task titled Browser-created task"},
    )
    assert submitted.status_code == 201
    proposal = submitted.json()["proposal"]

    inspected = client.get(f"/proposals/{proposal['proposal_id']}")
    assert inspected.status_code == 200
    assert inspected.json()["proposal"] == proposal

    approved = client.post(
        f"/proposals/{proposal['proposal_id']}/approve",
        json={
            "proposal_id": proposal["proposal_id"],
            "fingerprint": proposal["fingerprint"],
        },
    )

    assert approved.status_code == 200
    assert approved.json()["proposal"]["status"] == "completed"
    assert approved.json()["proposal"]["result"]["task"]["title"] == "Browser-created task"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
