from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path
from typing import Any

import pytest


APP_JAVASCRIPT = Path(__file__).parents[1] / "src" / "relaine" / "static" / "app.js"
NODE = shutil.which("node")

HARNESS = textwrap.dedent(
    r"""
    const fs = require("fs");
    const source = fs.readFileSync(process.argv[1], "utf8");
    const config = JSON.parse(process.argv[2]);

    class FakeElement {
      constructor() {
        this.hidden = false;
        this.disabled = false;
        this.value = "";
        this.textContent = "";
        this.dataset = {};
        this.listeners = {};
        this.children = [];
        this.className = "";
        this.title = "";
        this.dateTime = "";
        this.attributes = {};
      }
      addEventListener(name, callback) {
        this.listeners[name] = callback;
      }
      replaceChildren(...children) {
        this.children = children;
      }
      append(...children) {
        this.children.push(...children);
      }
      setAttribute(name, value) {
        this.attributes[name] = value;
      }
    }

    const nodes = {};
    const element = (selector) => {
      if (!nodes[selector]) nodes[selector] = new FakeElement();
      return nodes[selector];
    };
    global.document = {
      querySelector: element,
      createElement() { return new FakeElement(); },
    };
    element("#proposal-content").hidden = true;
    element("#decision-actions").hidden = true;
    element("#outcome").hidden = true;
    element("#outcome-data").hidden = true;

    let storedValue = config.stored;
    global.sessionStorage = {
      getItem() { return storedValue; },
      setItem(key, value) { storedValue = value; },
      removeItem() { storedValue = null; },
    };

    const calls = [];
    const responses = [...config.responses];
    global.fetch = async (url, options = {}) => {
      calls.push({ url, options });
      let responseIndex = responses.findIndex((item) => item.url === url);
      if (responseIndex < 0 && url === "/tasks?limit=50") {
        return {
          ok: true,
          status: 200,
          json: async () => ({ tasks: [], limit: 50, error: null }),
        };
      }
      if (responseIndex < 0 && url.endsWith("/history")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            proposal_id: "PROP-RESTORE",
            events: [],
            total_events: 0,
            limit: 200,
            complete: true,
            error: null,
          }),
        };
      }
      if (responseIndex < 0) responseIndex = 0;
      const [planned] = responses.splice(responseIndex, 1);
      if (!planned || planned.throw) throw new Error("simulated API outage");
      return {
        ok: planned.ok,
        status: planned.status,
        json: async () => planned.payload,
      };
    };

    eval(source);

    (async () => {
      await new Promise((resolve) => setTimeout(resolve, 5));
      if (config.trigger === "submit") {
        element("#business-request").value = "Create the browser task";
        await element("#request-form").listeners.submit({ preventDefault() {} });
      } else if (config.trigger === "approve" || config.trigger === "reject") {
        const selector = config.trigger === "approve" ? "#approve-button" : "#reject-button";
        await element(selector).listeners.click();
      }
      await new Promise((resolve) => setTimeout(resolve, 5));
      process.stdout.write(JSON.stringify({
        stored: storedValue,
        calls,
        action: element("#proposal-action").textContent,
        parameters: element("#proposal-parameters").textContent,
        proposalId: element("#proposal-id").textContent,
        fingerprint: element("#proposal-fingerprint").textContent,
        status: element("#status-badge").textContent,
        statusState: element("#status-badge").dataset.state,
        decisionsHidden: element("#decision-actions").hidden,
        contentHidden: element("#proposal-content").hidden,
        outcomeHidden: element("#outcome").hidden,
        outcome: element("#outcome-message").textContent,
        tasksState: element("#tasks-state").textContent,
        tasksStateKind: element("#tasks-state").dataset.state,
        taskCount: element("#task-count").textContent,
        tasksHidden: element("#task-list").hidden,
        tasks: element("#task-list").children.map((item) => ({
          title: item.children[0].children[0].textContent,
          status: item.children[0].children[1].textContent,
          statusValue: item.children[0].children[1].dataset.status,
          taskId: item.children[1].textContent,
        })),
        historyState: element("#history-state").textContent,
        historyStateKind: element("#history-state").dataset.state,
        historySummary: element("#history-summary").textContent,
        historyHidden: element("#history-list").hidden,
        history: element("#history-list").children.map((item) => ({
          event: item.dataset.event,
          type: item.children[1].children[0].children[0].textContent,
          timestamp: item.children[1].children[0].children[1].dateTime,
          metadata: item.children[1].children[1]
            ? item.children[1].children[1].textContent
            : "",
        })),
      }));
    })();
    """
)


def record(
    status: str = "pending",
    *,
    result: dict[str, Any] | None = None,
    failure: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "proposal_id": "PROP-RESTORE",
        "fingerprint": "a" * 64,
        "status": status,
        "proposal": {
            "action": "create_task",
            "parameters": {"title": "Authoritative server task"},
            "rationale": "Persisted server explanation.",
        },
        "result": result,
        "failure": failure,
    }


def operation_response(proposal: dict[str, Any]) -> dict[str, Any]:
    return {"success": True, "proposal": proposal, "error": None}


def run_browser(config: dict[str, Any]) -> dict[str, Any]:
    if NODE is None:
        pytest.skip("Node.js is unavailable for the dependency-free browser harness")
    completed = subprocess.run(
        [NODE, "-e", HARNESS, str(APP_JAVASCRIPT), json.dumps(config)],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return json.loads(completed.stdout)


def test_successful_submission_stores_only_id_and_fingerprint() -> None:
    proposal = record()
    result = run_browser(
        {
            "stored": None,
            "trigger": "submit",
            "responses": [
                {
                    "ok": True,
                    "status": 201,
                    "payload": {
                        "workflow_result": {"status": "pending_approval"},
                        "proposal": proposal,
                        "error": None,
                    },
                }
            ],
        }
    )

    assert json.loads(result["stored"]) == {
        "proposal_id": proposal["proposal_id"],
        "fingerprint": proposal["fingerprint"],
    }
    assert result["decisionsHidden"] is False


def test_restore_uses_authoritative_server_record_for_approval() -> None:
    pending = record()
    completed = record(
        "completed",
        result={
            "success": True,
            "task": {"task_id": "TASK-1", "title": "Authoritative server task"},
            "error": None,
        },
    )
    stored = {
        "proposal_id": pending["proposal_id"],
        "fingerprint": pending["fingerprint"],
        "parameters": {"title": "Untrusted browser replacement"},
    }

    result = run_browser(
        {
            "stored": json.dumps(stored),
            "trigger": "approve",
            "responses": [
                {"ok": True, "status": 200, "payload": operation_response(pending)},
                {"ok": True, "status": 200, "payload": operation_response(completed)},
            ],
        }
    )

    restore_call = next(
        call for call in result["calls"] if call["url"] == "/proposals/PROP-RESTORE"
    )
    assert restore_call == {
        "url": "/proposals/PROP-RESTORE",
        "options": {"method": "GET"},
    }
    approval = next(
        call
        for call in result["calls"]
        if call["url"] == "/proposals/PROP-RESTORE/approve"
    )
    assert approval["url"] == "/proposals/PROP-RESTORE/approve"
    assert json.loads(approval["options"]["body"]) == {
        "proposal_id": "PROP-RESTORE",
        "fingerprint": "a" * 64,
    }
    assert "Authoritative server task" in result["parameters"]
    assert "Untrusted browser replacement" not in result["parameters"]
    assert json.loads(result["stored"]) == {
        "proposal_id": "PROP-RESTORE",
        "fingerprint": "a" * 64,
    }
    assert result["statusState"] == "completed"
    assert result["decisionsHidden"] is True


def test_missing_server_proposal_clears_stale_reference() -> None:
    result = run_browser(
        {
            "stored": json.dumps(
                {"proposal_id": "PROP-MISSING", "fingerprint": "b" * 64}
            ),
            "responses": [
                {
                    "ok": False,
                    "status": 404,
                    "payload": {
                        "success": False,
                        "proposal": None,
                        "error": {
                            "code": "proposal_not_found",
                            "message": "Proposal does not exist.",
                            "details": [],
                        },
                    },
                }
            ],
        }
    )

    assert result["stored"] is None
    assert result["decisionsHidden"] is True
    assert result["outcome"] == "Proposal does not exist."


def test_invalid_browser_reference_is_cleared_without_api_call() -> None:
    result = run_browser(
        {
            "stored": json.dumps(
                {"proposal_id": "PROP-BAD", "fingerprint": "not-a-fingerprint"}
            ),
            "responses": [],
        }
    )

    assert result["stored"] is None
    assert all(call["url"] == "/tasks?limit=50" for call in result["calls"])
    assert result["decisionsHidden"] is True


@pytest.mark.parametrize(
    ("proposal", "expected_outcome"),
    [
        (record("rejected"), "Proposal rejected. No business data was changed."),
        (
            record(
                "completed",
                result={
                    "success": True,
                    "task": {"task_id": "TASK-1", "status": "todo"},
                    "error": None,
                },
            ),
            "Execution completed and persisted.",
        ),
        (
            record(
                "failed",
                result={
                    "success": False,
                    "task": None,
                    "error": {
                        "code": "task_not_found",
                        "message": "Task does not exist.",
                        "details": [],
                    },
                },
            ),
            "Task does not exist.",
        ),
    ],
)
def test_terminal_state_restores_without_decision_controls(
    proposal: dict[str, Any],
    expected_outcome: str,
) -> None:
    result = run_browser(
        {
            "stored": json.dumps(
                {
                    "proposal_id": proposal["proposal_id"],
                    "fingerprint": proposal["fingerprint"],
                }
            ),
            "responses": [
                {"ok": True, "status": 200, "payload": operation_response(proposal)}
            ],
        }
    )

    assert result["statusState"] == proposal["status"]
    assert result["decisionsHidden"] is True
    assert result["outcome"] == expected_outcome


def test_unavailable_api_keeps_reference_but_offers_no_decision() -> None:
    stored = {"proposal_id": "PROP-RESTORE", "fingerprint": "a" * 64}
    result = run_browser(
        {
            "stored": json.dumps(stored),
            "responses": [
                {
                    "ok": False,
                    "status": 500,
                    "payload": {
                        "success": False,
                        "proposal": None,
                        "error": {
                            "code": "database_error",
                            "message": "The proposal database operation failed.",
                            "details": [],
                        },
                    },
                }
            ],
        }
    )

    assert json.loads(result["stored"]) == stored
    proposal_calls = [
        call for call in result["calls"] if call["url"].startswith("/proposals/")
    ]
    assert len(proposal_calls) == 1
    assert proposal_calls[0]["options"] == {"method": "GET"}
    assert result["decisionsHidden"] is True
    assert result["statusState"] == "error"
    assert result["outcome"] == "The proposal database operation failed."


def test_task_dashboard_renders_loading_result_and_status_distinctions() -> None:
    tasks = [
        {
            "task_id": "TASK-3",
            "title": "Finished <img src=x onerror=alert(1)>",
            "status": "done",
        },
        {"task_id": "TASK-2", "title": "Active work", "status": "in_progress"},
        {"task_id": "TASK-1", "title": "Queued work", "status": "todo"},
    ]
    result = run_browser(
        {
            "stored": None,
            "responses": [
                {
                    "url": "/tasks?limit=50",
                    "ok": True,
                    "status": 200,
                    "payload": {"tasks": tasks, "limit": 50, "error": None},
                }
            ],
        }
    )

    assert result["tasksHidden"] is False
    assert result["taskCount"] == "3 tasks"
    assert result["tasks"] == [
        {
            "title": "Finished <img src=x onerror=alert(1)>",
            "status": "done",
            "statusValue": "done",
            "taskId": "TASK-3",
        },
        {
            "title": "Active work",
            "status": "in progress",
            "statusValue": "in_progress",
            "taskId": "TASK-2",
        },
        {
            "title": "Queued work",
            "status": "todo",
            "statusValue": "todo",
            "taskId": "TASK-1",
        },
    ]


def test_task_dashboard_has_empty_and_error_states() -> None:
    empty = run_browser({"stored": None, "responses": []})
    failed = run_browser(
        {
            "stored": None,
            "responses": [
                {
                    "url": "/tasks?limit=50",
                    "ok": False,
                    "status": 500,
                    "payload": {
                        "tasks": [],
                        "limit": 50,
                        "error": {
                            "code": "database_error",
                            "message": "The task database operation failed.",
                            "details": [],
                        },
                    },
                }
            ],
        }
    )

    assert empty["tasksHidden"] is True
    assert empty["taskCount"] == "0 tasks"
    assert empty["tasksStateKind"] == "empty"
    assert "No persisted tasks yet" in empty["tasksState"]
    assert failed["tasksHidden"] is True
    assert failed["taskCount"] == "Unavailable"
    assert failed["tasksStateKind"] == "error"
    assert failed["tasksState"] == "The task database operation failed."


def test_successful_approval_refreshes_tasks_without_page_reload() -> None:
    pending = record()
    completed = record(
        "completed",
        result={
            "success": True,
            "task": {
                "task_id": "TASK-NEW",
                "title": "Authoritative server task",
                "status": "todo",
            },
            "error": None,
        },
    )
    result = run_browser(
        {
            "stored": json.dumps(
                {
                    "proposal_id": pending["proposal_id"],
                    "fingerprint": pending["fingerprint"],
                }
            ),
            "trigger": "approve",
            "responses": [
                {
                    "url": "/proposals/PROP-RESTORE",
                    "ok": True,
                    "status": 200,
                    "payload": operation_response(pending),
                },
                {
                    "url": "/tasks?limit=50",
                    "ok": True,
                    "status": 200,
                    "payload": {"tasks": [], "limit": 50, "error": None},
                },
                {
                    "url": "/proposals/PROP-RESTORE/approve",
                    "ok": True,
                    "status": 200,
                    "payload": operation_response(completed),
                },
                {
                    "url": "/tasks?limit=50",
                    "ok": True,
                    "status": 200,
                    "payload": {
                        "tasks": [completed["result"]["task"]],
                        "limit": 50,
                        "error": None,
                    },
                },
            ],
        }
    )

    task_requests = [
        call for call in result["calls"] if call["url"] == "/tasks?limit=50"
    ]
    assert len(task_requests) == 2
    assert all(call["options"] == {"method": "GET"} for call in task_requests)
    assert result["tasks"] == [
        {
            "title": "Authoritative server task",
            "status": "todo",
            "statusValue": "todo",
            "taskId": "TASK-NEW",
        }
    ]


def audit_event(
    event_id: int,
    event_type: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "proposal_id": "PROP-RESTORE",
        "timestamp": f"2026-01-01T00:00:0{event_id}+00:00",
        "event_type": event_type,
        "metadata": metadata or {},
    }


def history_response(events: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "proposal_id": "PROP-RESTORE",
        "events": events,
        "total_events": len(events),
        "limit": 200,
        "complete": True,
        "error": None,
    }


def test_restored_proposal_loads_real_history_with_safe_text_metadata() -> None:
    pending = record()
    events = [
        audit_event(
            1,
            "proposal_submitted",
            {"action": "<img src=x onerror=alert(1)>"},
        ),
        audit_event(2, "proposal_approved", {"action": "create_task"}),
    ]
    result = run_browser(
        {
            "stored": json.dumps(
                {
                    "proposal_id": pending["proposal_id"],
                    "fingerprint": pending["fingerprint"],
                }
            ),
            "responses": [
                {
                    "url": "/proposals/PROP-RESTORE",
                    "ok": True,
                    "status": 200,
                    "payload": operation_response(pending),
                },
                {
                    "url": "/proposals/PROP-RESTORE/history",
                    "ok": True,
                    "status": 200,
                    "payload": history_response(events),
                },
            ],
        }
    )

    assert result["historyHidden"] is False
    assert result["historySummary"] == "2 events"
    assert [event["event"] for event in result["history"]] == [
        "proposal_submitted",
        "proposal_approved",
    ]
    assert result["history"][0]["timestamp"] == events[0]["timestamp"]
    assert "<img src=x onerror=alert(1)>" in result["history"][0]["metadata"]


def test_history_dashboard_has_empty_and_error_states() -> None:
    pending = record()
    stored = json.dumps(
        {
            "proposal_id": pending["proposal_id"],
            "fingerprint": pending["fingerprint"],
        }
    )
    inspect_response = {
        "url": "/proposals/PROP-RESTORE",
        "ok": True,
        "status": 200,
        "payload": operation_response(pending),
    }
    empty = run_browser(
        {
            "stored": stored,
            "responses": [
                inspect_response,
                {
                    "url": "/proposals/PROP-RESTORE/history",
                    "ok": True,
                    "status": 200,
                    "payload": history_response([]),
                },
            ],
        }
    )
    failed = run_browser(
        {
            "stored": stored,
            "responses": [
                inspect_response,
                {
                    "url": "/proposals/PROP-RESTORE/history",
                    "ok": False,
                    "status": 500,
                    "payload": {
                        "proposal_id": "PROP-RESTORE",
                        "events": [],
                        "total_events": 0,
                        "limit": 200,
                        "complete": False,
                        "error": {
                            "code": "database_error",
                            "message": "History is unavailable.",
                            "details": [],
                        },
                    },
                },
            ],
        }
    )

    assert empty["historyHidden"] is True
    assert empty["historySummary"] == "0 events"
    assert empty["historyState"] == "No audit events were recorded for this proposal."
    assert failed["historyHidden"] is True
    assert failed["historySummary"] == "Unavailable"
    assert failed["historyStateKind"] == "error"
    assert failed["historyState"] == "History is unavailable."


@pytest.mark.parametrize(
    ("decision", "terminal_status", "terminal_event"),
    [
        ("approve", "completed", "execution_succeeded"),
        ("reject", "rejected", "proposal_rejected"),
    ],
)
def test_decision_refreshes_history_without_reload(
    decision: str,
    terminal_status: str,
    terminal_event: str,
) -> None:
    pending = record()
    terminal = record(
        terminal_status,
        result=(
            {
                "success": True,
                "task": {"task_id": "TASK-1", "title": "Task", "status": "todo"},
                "error": None,
            }
            if terminal_status == "completed"
            else None
        ),
    )
    initial_events = [audit_event(1, "proposal_submitted")]
    terminal_events = [*initial_events, audit_event(2, terminal_event)]
    result = run_browser(
        {
            "stored": json.dumps(
                {
                    "proposal_id": pending["proposal_id"],
                    "fingerprint": pending["fingerprint"],
                }
            ),
            "trigger": decision,
            "responses": [
                {
                    "url": "/proposals/PROP-RESTORE",
                    "ok": True,
                    "status": 200,
                    "payload": operation_response(pending),
                },
                {
                    "url": "/proposals/PROP-RESTORE/history",
                    "ok": True,
                    "status": 200,
                    "payload": history_response(initial_events),
                },
                {
                    "url": f"/proposals/PROP-RESTORE/{decision}",
                    "ok": True,
                    "status": 200,
                    "payload": operation_response(terminal),
                },
                {
                    "url": "/proposals/PROP-RESTORE/history",
                    "ok": True,
                    "status": 200,
                    "payload": history_response(terminal_events),
                },
            ],
        }
    )

    history_requests = [
        call
        for call in result["calls"]
        if call["url"] == "/proposals/PROP-RESTORE/history"
    ]
    assert len(history_requests) == 2
    assert [event["event"] for event in result["history"]] == [
        "proposal_submitted",
        terminal_event,
    ]
