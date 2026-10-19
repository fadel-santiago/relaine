"use strict";

const PROPOSAL_REFERENCE_KEY = "relaine.latestProposalReference";

const elements = {
  form: document.querySelector("#request-form"),
  request: document.querySelector("#business-request"),
  submit: document.querySelector("#submit-button"),
  empty: document.querySelector("#empty-state"),
  content: document.querySelector("#proposal-content"),
  action: document.querySelector("#proposal-action"),
  parameters: document.querySelector("#proposal-parameters"),
  rationale: document.querySelector("#proposal-rationale"),
  proposalId: document.querySelector("#proposal-id"),
  fingerprint: document.querySelector("#proposal-fingerprint"),
  status: document.querySelector("#status-badge"),
  decisions: document.querySelector("#decision-actions"),
  approve: document.querySelector("#approve-button"),
  reject: document.querySelector("#reject-button"),
  outcome: document.querySelector("#outcome"),
  outcomeMessage: document.querySelector("#outcome-message"),
  outcomeData: document.querySelector("#outcome-data"),
  live: document.querySelector("#live-status"),
  tasksRefresh: document.querySelector("#refresh-tasks-button"),
  tasksState: document.querySelector("#tasks-state"),
  tasksList: document.querySelector("#task-list"),
  taskCount: document.querySelector("#task-count"),
  historyState: document.querySelector("#history-state"),
  historyList: document.querySelector("#history-list"),
  historySummary: document.querySelector("#history-summary"),
};

let currentProposal = null;
let busy = false;
let tasksLoading = false;
let tasksRefreshQueued = false;
let historyLoading = false;
let queuedHistoryProposalId = null;

function clearProposalReference() {
  try {
    sessionStorage.removeItem(PROPOSAL_REFERENCE_KEY);
  } catch (error) {
    // Storage can be unavailable without affecting the persisted workflow.
  }
}

function saveProposalReference(record) {
  const reference = {
    proposal_id: record.proposal_id,
    fingerprint: record.fingerprint,
  };
  try {
    sessionStorage.setItem(PROPOSAL_REFERENCE_KEY, JSON.stringify(reference));
  } catch (error) {
    // The workflow remains usable when browser storage is disabled.
  }
}

function loadProposalReference() {
  let raw;
  try {
    raw = sessionStorage.getItem(PROPOSAL_REFERENCE_KEY);
  } catch (error) {
    return null;
  }
  if (!raw) return null;

  try {
    const parsed = JSON.parse(raw);
    const validId =
      parsed && typeof parsed.proposal_id === "string" && parsed.proposal_id.length > 0;
    const validFingerprint =
      parsed &&
      typeof parsed.fingerprint === "string" &&
      /^[0-9a-f]{64}$/.test(parsed.fingerprint);
    if (!validId || !validFingerprint) {
      clearProposalReference();
      return null;
    }
    return {
      proposal_id: parsed.proposal_id,
      fingerprint: parsed.fingerprint,
    };
  } catch (error) {
    clearProposalReference();
    return null;
  }
}

function setBusy(value, message) {
  busy = value;
  elements.submit.disabled = value;
  elements.approve.disabled = value;
  elements.reject.disabled = value;
  elements.request.disabled = value;
  if (message) {
    elements.live.textContent = message;
  }
}

function setStatus(value, label) {
  elements.status.dataset.state = value;
  elements.status.textContent = label || value.replaceAll("_", " ");
}

function resetOutcome() {
  elements.outcome.hidden = true;
  elements.outcomeMessage.textContent = "";
  elements.outcomeData.textContent = "";
  elements.outcomeData.hidden = true;
}

function showOutcome(message, data) {
  elements.outcome.hidden = false;
  elements.outcomeMessage.textContent = message;
  if (data) {
    elements.outcomeData.textContent = JSON.stringify(data, null, 2);
    elements.outcomeData.hidden = false;
  } else {
    elements.outcomeData.textContent = "";
    elements.outcomeData.hidden = true;
  }
}

function errorMessage(error) {
  if (!error) {
    return "The request could not be completed.";
  }
  if (typeof error.message === "string") {
    return error.message;
  }
  if (Array.isArray(error)) {
    return error.map((item) => item.msg || "Invalid input").join("; ");
  }
  return "The request could not be completed.";
}

function renderProposal(record) {
  currentProposal = record;
  elements.empty.hidden = true;
  elements.content.hidden = false;
  elements.action.textContent = record.proposal.action;
  elements.parameters.textContent = JSON.stringify(record.proposal.parameters, null, 2);
  elements.rationale.textContent = record.proposal.rationale;
  elements.proposalId.textContent = record.proposal_id;
  elements.fingerprint.textContent = record.fingerprint;
  setStatus(record.status);
  elements.decisions.hidden = record.status !== "pending";
}

function renderSubmission(payload) {
  const workflow = payload.workflow_result;
  if (workflow && workflow.status === "pending_approval" && payload.proposal) {
    saveProposalReference(payload.proposal);
    renderProposal(payload.proposal);
    elements.live.textContent = "Proposal generated and ready for review.";
    return;
  }

  currentProposal = null;
  clearHistory("No audit history is available because no proposal was created.");
  elements.empty.hidden = false;
  elements.content.hidden = true;
  elements.decisions.hidden = true;
  const state = workflow ? workflow.status : "error";
  setStatus(state === "failed" ? "error" : state);
  const error = payload.error || (workflow && workflow.error);
  showOutcome(errorMessage(error), error && error.details);
  elements.live.textContent = errorMessage(error);
}

function renderOperation(payload) {
  if (payload.proposal) {
    renderProposal(payload.proposal);
  }

  if (payload.error) {
    setStatus(payload.proposal ? payload.proposal.status : "error");
    showOutcome(errorMessage(payload.error), payload.error.details);
    elements.live.textContent = errorMessage(payload.error);
    return;
  }

  const record = payload.proposal;
  if (!record) {
    setStatus("error");
    showOutcome("The server returned no proposal state.");
    return;
  }

  if (record.status === "rejected") {
    showOutcome("Proposal rejected. No business data was changed.");
  } else if (record.status === "completed" && record.result && record.result.task) {
    showOutcome("Execution completed and persisted.", record.result.task);
  } else if (record.status === "failed") {
    const resultError = record.result && record.result.error;
    const failure = record.failure;
    showOutcome(errorMessage(resultError || failure), resultError || failure);
  } else {
    showOutcome(`Proposal is ${record.status}.`);
  }
  elements.live.textContent = `Proposal ${record.status}.`;
}

async function requestJson(url, options) {
  const response = await fetch(url, options);
  const payload = await response.json();
  return { response, payload };
}

function showHistoryState(message, state = "empty") {
  elements.historyList.hidden = true;
  elements.historyState.hidden = false;
  elements.historyState.dataset.state = state;
  elements.historyState.textContent = message;
}

function clearHistory(message) {
  elements.historyList.replaceChildren();
  elements.historySummary.textContent = "No proposal";
  showHistoryState(message);
}

function formatHistoryTimestamp(value) {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString();
}

function renderHistory(payload) {
  elements.historyList.replaceChildren();
  if (payload.events.length === 0) {
    elements.historySummary.textContent = "0 events";
    showHistoryState("No audit events were recorded for this proposal.");
    return;
  }

  for (const event of payload.events) {
    const item = document.createElement("li");
    item.className = "history-event";
    item.dataset.event = event.event_type;

    const marker = document.createElement("span");
    marker.className = "history-marker";
    marker.setAttribute("aria-hidden", "true");

    const body = document.createElement("div");
    body.className = "history-event-body";

    const header = document.createElement("div");
    header.className = "history-event-header";

    const eventType = document.createElement("p");
    eventType.className = "history-event-type";
    eventType.textContent = event.event_type.replaceAll("_", " ");

    const timestamp = document.createElement("time");
    timestamp.className = "history-time";
    timestamp.dateTime = event.timestamp;
    timestamp.textContent = formatHistoryTimestamp(event.timestamp);

    header.append(eventType, timestamp);
    body.append(header);

    if (event.metadata && Object.keys(event.metadata).length > 0) {
      const metadata = document.createElement("pre");
      metadata.className = "history-metadata";
      metadata.textContent = JSON.stringify(event.metadata, null, 2);
      body.append(metadata);
    }

    item.append(marker, body);
    elements.historyList.append(item);
  }

  elements.historyState.hidden = true;
  elements.historyList.hidden = false;
  elements.historySummary.textContent = payload.complete
    ? `${payload.total_events} ${payload.total_events === 1 ? "event" : "events"}`
    : `Latest ${payload.events.length} of ${payload.total_events} events`;
}

async function refreshHistory(proposalId) {
  if (!proposalId) {
    clearHistory("Submit or restore a proposal to view its recorded lifecycle.");
    return;
  }
  if (historyLoading) {
    queuedHistoryProposalId = proposalId;
    return;
  }

  historyLoading = true;
  elements.historySummary.textContent = "Loading";
  showHistoryState("Loading recorded proposal events…", "loading");

  try {
    const { response, payload } = await requestJson(
      `/proposals/${encodeURIComponent(proposalId)}/history`,
      { method: "GET" },
    );
    if (!response.ok || payload.error || !Array.isArray(payload.events)) {
      elements.historySummary.textContent = "Unavailable";
      showHistoryState(errorMessage(payload.error), "error");
      return;
    }
    renderHistory(payload);
  } catch (error) {
    elements.historySummary.textContent = "Unavailable";
    showHistoryState("Execution history could not be loaded from the local server.", "error");
  } finally {
    historyLoading = false;
    if (queuedHistoryProposalId) {
      const nextProposalId = queuedHistoryProposalId;
      queuedHistoryProposalId = null;
      await refreshHistory(nextProposalId);
    }
  }
}

function showTasksState(message, state = "loading") {
  elements.tasksState.hidden = false;
  elements.tasksState.dataset.state = state;
  elements.tasksState.textContent = message;
}

function renderTasks(tasks) {
  elements.tasksList.replaceChildren();
  elements.taskCount.textContent = `${tasks.length} ${tasks.length === 1 ? "task" : "tasks"}`;
  if (tasks.length === 0) {
    elements.tasksList.hidden = true;
    showTasksState("No persisted tasks yet. Approved task creations will appear here.", "empty");
    return;
  }

  for (const task of tasks) {
    const item = document.createElement("li");
    item.className = "task-card";

    const header = document.createElement("div");
    header.className = "task-card-header";

    const title = document.createElement("p");
    title.className = "task-title";
    title.textContent = task.title;

    const taskStatus = document.createElement("span");
    taskStatus.className = "task-status";
    taskStatus.dataset.status = task.status;
    taskStatus.textContent = task.status.replaceAll("_", " ");

    const taskId = document.createElement("span");
    taskId.className = "task-id";
    taskId.textContent = task.task_id;
    taskId.title = task.task_id;

    header.append(title, taskStatus);
    item.append(header, taskId);
    elements.tasksList.append(item);
  }

  elements.tasksState.hidden = true;
  elements.tasksList.hidden = false;
}

async function refreshTasks() {
  if (tasksLoading) {
    tasksRefreshQueued = true;
    return;
  }
  tasksLoading = true;
  elements.tasksRefresh.disabled = true;
  showTasksState("Loading persisted tasks…");

  try {
    const { response, payload } = await requestJson("/tasks?limit=50", {
      method: "GET",
    });
    if (!response.ok || payload.error || !Array.isArray(payload.tasks)) {
      elements.tasksList.hidden = true;
      elements.taskCount.textContent = "Unavailable";
      showTasksState(errorMessage(payload.error), "error");
      return;
    }
    renderTasks(payload.tasks);
  } catch (error) {
    elements.tasksList.hidden = true;
    elements.taskCount.textContent = "Unavailable";
    showTasksState("Tasks could not be loaded from the local server.", "error");
  } finally {
    tasksLoading = false;
    elements.tasksRefresh.disabled = false;
    if (tasksRefreshQueued) {
      tasksRefreshQueued = false;
      await refreshTasks();
    }
  }
}

async function restoreProposal() {
  const stored = loadProposalReference();
  if (!stored) return;

  currentProposal = null;
  elements.decisions.hidden = true;
  setStatus("executing", "Restoring");
  setBusy(true, "Restoring the latest persisted proposal.");

  try {
    const { response, payload } = await requestJson(
      `/proposals/${encodeURIComponent(stored.proposal_id)}`,
      { method: "GET" },
    );

    if (!response.ok || !payload.success || !payload.proposal) {
      if (response.status === 404 || response.status === 409) {
        clearProposalReference();
      }
      setStatus("error");
      showOutcome(
        errorMessage(payload.error),
        payload.error && payload.error.details,
      );
      elements.live.textContent = errorMessage(payload.error);
      return;
    }

    const authoritative = payload.proposal;
    if (
      authoritative.proposal_id !== stored.proposal_id ||
      authoritative.fingerprint !== stored.fingerprint
    ) {
      clearProposalReference();
      setStatus("error");
      showOutcome("The saved proposal reference no longer matches the persisted proposal.");
      elements.live.textContent = "The saved proposal reference is obsolete.";
      return;
    }

    saveProposalReference(authoritative);
    renderProposal(authoritative);
    if (authoritative.status === "pending") {
      resetOutcome();
      elements.live.textContent = "Pending proposal restored and ready for review.";
    } else {
      renderOperation({ success: true, proposal: authoritative, error: null });
      elements.live.textContent = `Restored proposal ${authoritative.status}.`;
    }
    await refreshHistory(authoritative.proposal_id);
  } catch (error) {
    setStatus("error");
    showOutcome("The local server is unavailable. No business data was changed.");
    elements.live.textContent = "The saved proposal could not be restored.";
  } finally {
    setBusy(false);
  }
}

elements.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy) return;

  resetOutcome();
  currentProposal = null;
  elements.decisions.hidden = true;
  setStatus("executing", "Generating");
  setBusy(true, "Generating a structured proposal.");

  try {
    const { payload } = await requestJson("/proposals", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request: elements.request.value }),
    });
    renderSubmission(payload);
    if (payload.proposal) {
      await refreshHistory(payload.proposal.proposal_id);
    }
  } catch (error) {
    setStatus("error");
    showOutcome("The local server could not be reached.");
    elements.live.textContent = "The local server could not be reached.";
  } finally {
    setBusy(false);
  }
});

async function decide(decision) {
  if (busy || !currentProposal || currentProposal.status !== "pending") return;

  const reviewed = {
    proposal_id: currentProposal.proposal_id,
    fingerprint: currentProposal.fingerprint,
  };
  setBusy(true, `${decision === "approve" ? "Approving" : "Rejecting"} proposal.`);
  setStatus("executing", decision === "approve" ? "Executing" : "Rejecting");
  elements.decisions.hidden = true;
  resetOutcome();

  try {
    const { payload } = await requestJson(
      `/proposals/${encodeURIComponent(reviewed.proposal_id)}/${decision}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(reviewed),
      },
    );
    renderOperation(payload);
    const refreshes = [refreshHistory(reviewed.proposal_id)];
    if (
      decision === "approve" &&
      payload.proposal &&
      payload.proposal.status === "completed" &&
      payload.proposal.result &&
      payload.proposal.result.success === true
    ) {
      refreshes.push(refreshTasks());
    }
    await Promise.all(refreshes);
  } catch (error) {
    setStatus("error");
    showOutcome("The local server could not be reached. Inspect the proposal before retrying.");
    elements.live.textContent = "The local server could not be reached.";
  } finally {
    setBusy(false);
  }
}

elements.approve.addEventListener("click", () => decide("approve"));
elements.reject.addEventListener("click", () => decide("reject"));
elements.tasksRefresh.addEventListener("click", () => refreshTasks());
void restoreProposal();
void refreshTasks();
