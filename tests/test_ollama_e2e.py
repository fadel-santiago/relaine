from __future__ import annotations

import json
import socket
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.error import URLError
from urllib.request import urlopen

import httpx
import pytest
import uvicorn

from relaine.api import create_app
from relaine.config import OllamaConfig


OLLAMA_MODEL = "gpt-oss:20b"


def _installed_models(base_url: str) -> set[str]:
    try:
        with urlopen(f"{base_url.rstrip('/')}/api/tags", timeout=2) as response:
            payload = json.load(response)
    except (OSError, URLError, ValueError) as exc:
        pytest.skip(f"Ollama is unavailable: {exc}")
    return {model["name"] for model in payload.get("models", [])}


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@contextmanager
def _live_api(
    database: Path,
    ollama_config: OllamaConfig,
) -> Iterator[httpx.Client]:
    """Run the real ASGI application through Uvicorn and a loopback TCP socket."""

    port = _free_loopback_port()
    app = create_app(database_path=database, ollama_config=ollama_config)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    client = httpx.Client(
        base_url=f"http://127.0.0.1:{port}",
        timeout=httpx.Timeout(180, connect=5),
    )

    deadline = time.monotonic() + 10
    try:
        while True:
            if not thread.is_alive():
                pytest.fail("Uvicorn stopped before the integration test connected")
            try:
                response = client.get("/tasks")
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() >= deadline:
                pytest.fail("Uvicorn did not become ready within 10 seconds")
            time.sleep(0.05)
        yield client
    finally:
        client.close()
        server.should_exit = True
        thread.join(timeout=10)
        if thread.is_alive():
            pytest.fail("Uvicorn did not stop within 10 seconds")


def _reference(proposal: dict[str, Any]) -> dict[str, str]:
    return {
        "proposal_id": proposal["proposal_id"],
        "fingerprint": proposal["fingerprint"],
    }


def _event_types(history: dict[str, Any]) -> list[str]:
    return [event["event_type"] for event in history["events"]]


def _sqlite_count(database: Path, table: str) -> int:
    if table not in {"tasks", "execution_receipts"}:
        raise ValueError("unexpected table")
    with sqlite3.connect(database) as connection:
        return int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])


@pytest.mark.ollama
def test_real_ollama_fastapi_approval_rejection_and_restart(tmp_path: Path) -> None:
    """Exercise two real model calls through live HTTP and isolated SQLite."""

    database = tmp_path / "relaine-r5c1.sqlite3"
    config = OllamaConfig(model=OLLAMA_MODEL)
    if config.model not in _installed_models(config.base_url):
        pytest.skip(f"required model is not installed: {config.model}")

    with _live_api(database, config) as client:
        created = client.post(
            "/proposals",
            json={"request": "Create a task titled R5c-1 live verification task."},
        )
        assert created.status_code == 201, created.text
        create_body = created.json()
        assert create_body["workflow_result"]["status"] == "pending_approval"
        create_proposal = create_body["proposal"]
        assert create_proposal["status"] == "pending"
        assert create_proposal["proposal"]["action"] == "create_task"
        assert (
            create_proposal["proposal"]["parameters"]["title"]
            == "R5c-1 live verification task"
        )

        inspected_pending = client.get(
            f"/proposals/{create_proposal['proposal_id']}"
        )
        assert inspected_pending.status_code == 200
        assert inspected_pending.json()["proposal"]["status"] == "pending"
        assert client.get("/tasks").json()["tasks"] == []
        assert _sqlite_count(database, "tasks") == 0

        wrong_reference = _reference(create_proposal)
        wrong_reference["fingerprint"] = "0" * 64
        denied = client.post(
            f"/proposals/{create_proposal['proposal_id']}/approve",
            json=wrong_reference,
        )
        assert denied.status_code == 409
        assert denied.json()["success"] is False
        assert denied.json()["error"]["code"] == "fingerprint_mismatch"
        assert _sqlite_count(database, "tasks") == 0

        approved = client.post(
            f"/proposals/{create_proposal['proposal_id']}/approve",
            json=_reference(create_proposal),
        )
        assert approved.status_code == 200, approved.text
        approved_body = approved.json()
        assert approved_body["success"] is True
        assert approved_body["proposal"]["status"] == "completed"
        task = approved_body["proposal"]["result"]["task"]
        assert task["title"] == "R5c-1 live verification task"
        assert task["status"] == "todo"
        assert _sqlite_count(database, "tasks") == 1
        assert _sqlite_count(database, "execution_receipts") == 1

        create_history_response = client.get(
            f"/proposals/{create_proposal['proposal_id']}/history"
        )
        assert create_history_response.status_code == 200
        create_history = create_history_response.json()
        assert create_history["complete"] is True
        assert _event_types(create_history) == [
            "proposal_submitted",
            "proposal_approved",
            "execution_claimed",
            "execution_succeeded",
        ]

        duplicate = client.post(
            f"/proposals/{create_proposal['proposal_id']}/approve",
            json=_reference(create_proposal),
        )
        assert duplicate.status_code == 409
        duplicate_body = duplicate.json()
        assert duplicate_body["success"] is False
        assert duplicate_body["error"]["code"] == "invalid_state"
        assert duplicate_body["proposal"]["status"] == "completed"
        assert _sqlite_count(database, "tasks") == 1
        assert _sqlite_count(database, "execution_receipts") == 1

        update = client.post(
            "/proposals",
            json={
                "request": f"Update task {task['task_id']} status to done.",
            },
        )
        assert update.status_code == 201, update.text
        update_body = update.json()
        assert update_body["workflow_result"]["status"] == "pending_approval"
        update_proposal = update_body["proposal"]
        assert update_proposal["status"] == "pending"
        assert update_proposal["proposal"]["action"] == "update_task"
        assert update_proposal["proposal"]["parameters"]["task_id"] == task["task_id"]
        assert (
            update_proposal["proposal"]["parameters"]["changes"]["status"]
            == "done"
        )

        rejected = client.post(
            f"/proposals/{update_proposal['proposal_id']}/reject",
            json=_reference(update_proposal),
        )
        assert rejected.status_code == 200, rejected.text
        assert rejected.json()["proposal"]["status"] == "rejected"
        tasks_after_rejection = client.get("/tasks").json()["tasks"]
        assert len(tasks_after_rejection) == 1
        assert tasks_after_rejection[0]["task_id"] == task["task_id"]
        assert tasks_after_rejection[0]["status"] == "todo"

        update_history_response = client.get(
            f"/proposals/{update_proposal['proposal_id']}/history"
        )
        assert update_history_response.status_code == 200
        update_history = update_history_response.json()
        assert _event_types(update_history) == [
            "proposal_submitted",
            "proposal_rejected",
        ]

    # A new app and service graph must recover authoritative state without inference.
    with _live_api(database, config) as restarted:
        persisted_tasks = restarted.get("/tasks")
        assert persisted_tasks.status_code == 200
        assert persisted_tasks.json()["tasks"] == tasks_after_rejection

        persisted_create = restarted.get(
            f"/proposals/{create_proposal['proposal_id']}"
        )
        persisted_update = restarted.get(
            f"/proposals/{update_proposal['proposal_id']}"
        )
        assert persisted_create.json()["proposal"]["status"] == "completed"
        assert persisted_update.json()["proposal"]["status"] == "rejected"

        restarted_create_history = restarted.get(
            f"/proposals/{create_proposal['proposal_id']}/history"
        )
        restarted_update_history = restarted.get(
            f"/proposals/{update_proposal['proposal_id']}/history"
        )
        assert restarted_create_history.json() == create_history
        assert restarted_update_history.json() == update_history

    print(
        json.dumps(
            {
                "transport": "live uvicorn over loopback HTTP",
                "model": config.model,
                "model_submissions": 2,
                "create_proposal_id": create_proposal["proposal_id"],
                "create_fingerprint": create_proposal["fingerprint"],
                "created_task": task,
                "create_history": _event_types(create_history),
                "duplicate_approval": {
                    "http_status": duplicate.status_code,
                    "error_code": duplicate_body["error"]["code"],
                },
                "update_proposal_id": update_proposal["proposal_id"],
                "update_fingerprint": update_proposal["fingerprint"],
                "update_status": rejected.json()["proposal"]["status"],
                "update_history": _event_types(update_history),
                "restart_verified": True,
            },
            indent=2,
            sort_keys=True,
        )
    )
