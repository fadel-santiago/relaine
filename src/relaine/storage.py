"""Persistent SQLite storage for RELAINE business tasks."""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from relaine.schemas import CreateTaskParameters, TaskRecord, TaskStatus


DEFAULT_TASK_DB_PATH = Path("relaine_tasks.sqlite3")
DEFAULT_TASK_LIST_LIMIT = 50
MAX_TASK_LIST_LIMIT = 100


class TaskStoreError(RuntimeError):
    """A SQLite or persisted-data failure at the storage boundary."""


class TaskNotFoundError(LookupError):
    """The requested task ID does not exist."""


@dataclass(frozen=True, slots=True)
class TaskStoreConfig:
    path: Path = DEFAULT_TASK_DB_PATH

    @classmethod
    def from_env(cls) -> "TaskStoreConfig":
        raw_path = os.getenv("RELAINE_DB_PATH", str(DEFAULT_TASK_DB_PATH)).strip()
        return cls(path=Path(raw_path or DEFAULT_TASK_DB_PATH))


class SQLiteTaskStore:
    """Small connection-per-operation store with automatic schema creation."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._initialize_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize_schema(self) -> None:
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS tasks (
                        task_id TEXT PRIMARY KEY,
                        title TEXT NOT NULL CHECK (length(trim(title)) > 0),
                        description TEXT,
                        assignee TEXT,
                        due_date TEXT,
                        status TEXT NOT NULL
                            CHECK (status IN ('todo', 'in_progress', 'done')),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
        except sqlite3.Error as exc:
            raise TaskStoreError(f"could not initialize task database: {exc}") from exc

    @staticmethod
    def _to_record(row: sqlite3.Row) -> TaskRecord:
        return TaskRecord.model_validate(dict(row))

    def insert_task_in_transaction(
        self,
        connection: sqlite3.Connection,
        parameters: CreateTaskParameters,
    ) -> TaskRecord:
        """Insert using a caller-owned transaction without committing it."""

        task_id = f"TASK-{uuid4().hex.upper()}"
        timestamp = datetime.now(UTC).isoformat()
        connection.execute(
            """
            INSERT INTO tasks (
                task_id, title, description, assignee, due_date,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                parameters.title,
                parameters.description,
                parameters.assignee,
                parameters.due_date,
                TaskStatus.TODO.value,
                timestamp,
                timestamp,
            ),
        )
        row = connection.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise TaskStoreError("created task could not be read back")
        return self._to_record(row)

    def update_task_status_in_transaction(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        status: TaskStatus,
    ) -> TaskRecord:
        """Update using a caller-owned transaction without committing it."""

        timestamp = datetime.now(UTC).isoformat()
        cursor = connection.execute(
            """
            UPDATE tasks
            SET status = ?, updated_at = ?
            WHERE task_id = ?
            """,
            (status.value, timestamp, task_id),
        )
        if cursor.rowcount == 0:
            raise TaskNotFoundError(task_id)
        row = connection.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise TaskStoreError("updated task could not be read back")
        return self._to_record(row)

    def insert_task(self, parameters: CreateTaskParameters) -> TaskRecord:
        try:
            with closing(self._connect()) as connection, connection:
                return self.insert_task_in_transaction(connection, parameters)
        except (sqlite3.Error, ValidationError) as exc:
            raise TaskStoreError(f"could not create task: {exc}") from exc

    def update_task_status(self, task_id: str, status: TaskStatus) -> TaskRecord:
        try:
            with closing(self._connect()) as connection, connection:
                return self.update_task_status_in_transaction(
                    connection,
                    task_id,
                    status,
                )
        except TaskNotFoundError:
            raise
        except (sqlite3.Error, ValidationError) as exc:
            raise TaskStoreError(f"could not update task: {exc}") from exc

    def fetch_task(self, task_id: str) -> TaskRecord | None:
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
                return None if row is None else self._to_record(row)
        except (sqlite3.Error, ValidationError) as exc:
            raise TaskStoreError(f"could not retrieve task: {exc}") from exc

    def list_tasks(self, limit: int = DEFAULT_TASK_LIST_LIMIT) -> list[TaskRecord]:
        """Return a bounded, deterministic snapshot without changing task data."""

        if isinstance(limit, bool) or not 1 <= limit <= MAX_TASK_LIST_LIMIT:
            raise ValueError(
                f"limit must be between 1 and {MAX_TASK_LIST_LIMIT}"
            )
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM tasks
                    ORDER BY created_at DESC, task_id ASC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                return [self._to_record(row) for row in rows]
        except (sqlite3.Error, ValidationError) as exc:
            raise TaskStoreError(f"could not list tasks: {exc}") from exc
