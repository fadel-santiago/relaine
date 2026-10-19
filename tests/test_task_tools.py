from __future__ import annotations

from pathlib import Path

import pytest

from relaine.schemas import BusinessToolErrorCode, TaskStatus
from relaine.storage import SQLiteTaskStore, TaskStoreConfig, TaskStoreError
from relaine.tools import create_task, get_task, update_task


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "tasks.sqlite3"


@pytest.fixture
def store(db_path: Path) -> SQLiteTaskStore:
    return SQLiteTaskStore(db_path)


def test_database_path_is_configurable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured_path = tmp_path / "configured.sqlite3"
    monkeypatch.setenv("RELAINE_DB_PATH", str(configured_path))

    config = TaskStoreConfig.from_env()
    SQLiteTaskStore(config.path)

    assert config.path == configured_path
    assert configured_path.exists()


def test_create_task_persists_validated_state(store: SQLiteTaskStore) -> None:
    result = create_task(
        store,
        {
            "title": "Prepare quarterly report",
            "description": "Summarize revenue and costs",
            "assignee": "Finance",
            "due_date": "2026-10-31",
        },
    )

    assert result.success is True
    assert result.error is None
    assert result.task is not None
    assert result.task.task_id.startswith("TASK-")
    assert result.task.title == "Prepare quarterly report"
    assert result.task.status is TaskStatus.TODO
    assert store.fetch_task(result.task.task_id) == result.task


def test_update_task_status_is_persisted(store: SQLiteTaskStore) -> None:
    created = create_task(store, {"title": "Review budget"})
    assert created.task is not None

    updated = update_task(
        store,
        {
            "task_id": created.task.task_id,
            "changes": {"status": "in_progress"},
        },
    )

    assert updated.success is True
    assert updated.task is not None
    assert updated.task.status is TaskStatus.IN_PROGRESS
    persisted = store.fetch_task(created.task.task_id)
    assert persisted is not None
    assert persisted.status is TaskStatus.IN_PROGRESS
    assert persisted.updated_at >= persisted.created_at


def test_task_persists_across_store_instances(db_path: Path) -> None:
    first_store = SQLiteTaskStore(db_path)
    created = create_task(first_store, {"title": "Persistent task"})
    assert created.task is not None

    second_store = SQLiteTaskStore(db_path)
    retrieved = get_task(second_store, {"task_id": created.task.task_id})

    assert retrieved.success is True
    assert retrieved.task == created.task


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"title": ""},
        {"title": 123},
        {"title": "Valid", "unexpected": True},
        {"title": "Valid", "due_date": "31-10-2026"},
    ],
)
def test_create_task_rejects_invalid_input(
    store: SQLiteTaskStore,
    payload: dict[str, object],
) -> None:
    result = create_task(store, payload)

    assert result.success is False
    assert result.task is None
    assert result.error is not None
    assert result.error.code is BusinessToolErrorCode.INVALID_INPUT


@pytest.mark.parametrize("status", ["blocked", "complete", "", 1])
def test_update_task_rejects_invalid_status(
    store: SQLiteTaskStore,
    status: object,
) -> None:
    created = create_task(store, {"title": "Status validation"})
    assert created.task is not None

    result = update_task(
        store,
        {
            "task_id": created.task.task_id,
            "changes": {"status": status},
        },
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.code is BusinessToolErrorCode.INVALID_INPUT


def test_update_task_rejects_non_status_changes(store: SQLiteTaskStore) -> None:
    created = create_task(store, {"title": "Immutable title"})
    assert created.task is not None

    result = update_task(
        store,
        {
            "task_id": created.task.task_id,
            "changes": {"status": "done", "title": "Changed title"},
        },
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.code is BusinessToolErrorCode.INVALID_INPUT


def test_update_task_reports_nonexistent_id(store: SQLiteTaskStore) -> None:
    result = update_task(
        store,
        {
            "task_id": "TASK-DOES-NOT-EXIST",
            "changes": {"status": "done"},
        },
    )

    assert result.success is False
    assert result.task is None
    assert result.error is not None
    assert result.error.code is BusinessToolErrorCode.TASK_NOT_FOUND


def test_get_task_reports_nonexistent_id(store: SQLiteTaskStore) -> None:
    result = get_task(store, {"task_id": "TASK-DOES-NOT-EXIST"})

    assert result.success is False
    assert result.error is not None
    assert result.error.code is BusinessToolErrorCode.TASK_NOT_FOUND


def test_tool_returns_structured_database_error(
    store: SQLiteTaskStore,
    db_path: Path,
) -> None:
    db_path.unlink()
    db_path.mkdir()

    result = create_task(store, {"title": "Cannot be stored"})

    assert result.success is False
    assert result.error is not None
    assert result.error.code is BusinessToolErrorCode.DATABASE_ERROR


def test_schema_initialization_reports_corrupt_database(tmp_path: Path) -> None:
    corrupt_path = tmp_path / "corrupt.sqlite3"
    corrupt_path.write_text("not a sqlite database")

    with pytest.raises(TaskStoreError, match="could not initialize"):
        SQLiteTaskStore(corrupt_path)
