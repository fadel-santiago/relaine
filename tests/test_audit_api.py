from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from relaine.api import MAX_AUDIT_TIMELINE_EVENTS, create_app
from relaine.approval_storage import proposal_fingerprint
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
                parameters=CreateTaskParameters(title="Audited API task"),
                rationale="The request explicitly asks to create this task.",
            ),
        )


def audit_client(database: Path) -> TestClient:
    return TestClient(create_app(database, ProposalEngine(MockModel())))


def submit(client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/proposals",
        json={"request": "Create a task titled Audited API task"},
    )
    assert response.status_code == 201
    return response.json()["proposal"]


def test_history_endpoint_returns_validated_chronological_events(
    tmp_path: Path,
) -> None:
    database = tmp_path / "audit-api.sqlite3"
    client = audit_client(database)
    proposal = submit(client)
    approved = client.post(
        f"/proposals/{proposal['proposal_id']}/approve",
        json={
            "proposal_id": proposal["proposal_id"],
            "fingerprint": proposal["fingerprint"],
        },
    )
    assert approved.status_code == 200

    response = client.get(f"/proposals/{proposal['proposal_id']}/history")

    assert response.status_code == 200
    payload = response.json()
    assert payload["proposal_id"] == proposal["proposal_id"]
    assert payload["complete"] is True
    assert payload["total_events"] == 4
    assert payload["limit"] == MAX_AUDIT_TIMELINE_EVENTS
    assert [event["event_type"] for event in payload["events"]] == [
        "proposal_submitted",
        "proposal_approved",
        "execution_claimed",
        "execution_succeeded",
    ]
    assert [event["event_id"] for event in payload["events"]] == sorted(
        event["event_id"] for event in payload["events"]
    )
    assert all(event["timestamp"] for event in payload["events"])


def test_history_persists_across_api_instances_and_records_rejection(
    tmp_path: Path,
) -> None:
    database = tmp_path / "audit-api.sqlite3"
    first_client = audit_client(database)
    proposal = submit(first_client)
    rejected = first_client.post(
        f"/proposals/{proposal['proposal_id']}/reject",
        json={
            "proposal_id": proposal["proposal_id"],
            "fingerprint": proposal["fingerprint"],
        },
    )
    assert rejected.status_code == 200

    recovered_client = audit_client(database)
    response = recovered_client.get(
        f"/proposals/{proposal['proposal_id']}/history"
    )

    assert response.status_code == 200
    assert [event["event_type"] for event in response.json()["events"]] == [
        "proposal_submitted",
        "proposal_rejected",
    ]


def insert_legacy_proposal(database: Path, proposal_id: str = "PROP-LEGACY") -> None:
    proposal = CreateTaskProposal(
        action="create_task",
        parameters=CreateTaskParameters(title="Legacy task"),
        rationale="Legacy proposal fixture.",
    )
    fingerprint = proposal_fingerprint(proposal)
    timestamp = datetime.now(UTC).isoformat()
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO proposals (
                proposal_id, request, proposal_json, fingerprint, status,
                execution_protocol, result_json, failure_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'pending', NULL, NULL, NULL, ?, ?)
            """,
            (
                proposal_id,
                "Legacy request",
                proposal.model_dump_json(),
                fingerprint,
                timestamp,
                timestamp,
            ),
        )


def test_prefeature_proposal_returns_explicit_empty_history(tmp_path: Path) -> None:
    database = tmp_path / "audit-api.sqlite3"
    client = audit_client(database)
    insert_legacy_proposal(database)

    response = client.get("/proposals/PROP-LEGACY/history")

    assert response.status_code == 200
    assert response.json() == {
        "proposal_id": "PROP-LEGACY",
        "events": [],
        "total_events": 0,
        "limit": MAX_AUDIT_TIMELINE_EVENTS,
        "complete": True,
        "error": None,
    }


def test_history_bound_is_explicit_and_metadata_is_sanitized(tmp_path: Path) -> None:
    database = tmp_path / "audit-api.sqlite3"
    client = audit_client(database)
    insert_legacy_proposal(database, "PROP-LONG")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    event_count = MAX_AUDIT_TIMELINE_EVENTS + 5
    with sqlite3.connect(database) as connection:
        connection.executemany(
            """
            INSERT INTO audit_events (
                proposal_id, timestamp, event_type, metadata_json
            ) VALUES (?, ?, 'recovery_attempted', ?)
            """,
            [
                (
                    "PROP-LONG",
                    (start + timedelta(seconds=index)).isoformat(),
                    json.dumps(
                        {
                            "action": "create_task",
                            "receipt_present": False,
                            "secret": f"private-{index}",
                        }
                    ),
                )
                for index in range(event_count)
            ],
        )

    response = client.get("/proposals/PROP-LONG/history")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["events"]) == MAX_AUDIT_TIMELINE_EVENTS
    assert payload["total_events"] == event_count
    assert payload["complete"] is False
    assert [event["event_id"] for event in payload["events"]] == sorted(
        event["event_id"] for event in payload["events"]
    )
    assert all(
        event["metadata"]
        == {"action": "create_task", "receipt_present": False}
        for event in payload["events"]
    )


def test_history_for_missing_proposal_is_not_presented_as_empty(
    tmp_path: Path,
) -> None:
    response = audit_client(tmp_path / "audit-api.sqlite3").get(
        "/proposals/PROP-MISSING/history"
    )

    assert response.status_code == 404
    payload = response.json()
    assert payload["complete"] is False
    assert payload["events"] == []
    assert payload["error"]["code"] == "proposal_not_found"
