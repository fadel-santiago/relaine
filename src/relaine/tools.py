"""Explicit business tools; these are not registered with the R1 agent graph."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from relaine.schemas import (
    BusinessToolError,
    BusinessToolErrorCode,
    CreateTaskParameters,
    GetTaskParameters,
    TaskToolResult,
    UpdateTaskParameters,
)
from relaine.storage import SQLiteTaskStore, TaskNotFoundError, TaskStoreError


InputModel = TypeVar("InputModel", bound=BaseModel)
InputPayload = BaseModel | Mapping[str, Any]


def _validation_details(exc: ValidationError) -> list[str]:
    return [
        f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
        for error in exc.errors(include_url=False)
    ]


def _validate_input(model: type[InputModel], payload: InputPayload) -> InputModel:
    return model.model_validate(payload)


def _invalid_input(exc: ValidationError) -> TaskToolResult:
    return TaskToolResult(
        success=False,
        error=BusinessToolError(
            code=BusinessToolErrorCode.INVALID_INPUT,
            message="The business tool input is invalid.",
            details=_validation_details(exc),
        ),
    )


def _database_error(exc: TaskStoreError) -> TaskToolResult:
    return TaskToolResult(
        success=False,
        error=BusinessToolError(
            code=BusinessToolErrorCode.DATABASE_ERROR,
            message="The task database operation failed.",
            details=[str(exc)],
        ),
    )


def create_task(
    store: SQLiteTaskStore,
    parameters: CreateTaskParameters | Mapping[str, Any],
) -> TaskToolResult:
    """Validate and persist a task. This function performs a real write."""

    try:
        validated = _validate_input(CreateTaskParameters, parameters)
    except ValidationError as exc:
        return _invalid_input(exc)

    try:
        task = store.insert_task(validated)
    except TaskStoreError as exc:
        return _database_error(exc)
    return TaskToolResult(success=True, task=task)


def update_task(
    store: SQLiteTaskStore,
    parameters: UpdateTaskParameters | Mapping[str, Any],
) -> TaskToolResult:
    """Validate and persist one status update. This function performs a real write."""

    try:
        validated = _validate_input(UpdateTaskParameters, parameters)
    except ValidationError as exc:
        return _invalid_input(exc)

    try:
        task = store.update_task_status(
            validated.task_id,
            validated.changes.status,
        )
    except TaskNotFoundError:
        return TaskToolResult(
            success=False,
            error=BusinessToolError(
                code=BusinessToolErrorCode.TASK_NOT_FOUND,
                message=f"Task {validated.task_id} does not exist.",
            ),
        )
    except TaskStoreError as exc:
        return _database_error(exc)
    return TaskToolResult(success=True, task=task)


def get_task(
    store: SQLiteTaskStore,
    parameters: GetTaskParameters | Mapping[str, Any],
) -> TaskToolResult:
    """Read one persisted task without modifying it."""

    try:
        validated = _validate_input(GetTaskParameters, parameters)
    except ValidationError as exc:
        return _invalid_input(exc)

    try:
        task = store.fetch_task(validated.task_id)
    except TaskStoreError as exc:
        return _database_error(exc)
    if task is None:
        return TaskToolResult(
            success=False,
            error=BusinessToolError(
                code=BusinessToolErrorCode.TASK_NOT_FOUND,
                message=f"Task {validated.task_id} does not exist.",
            ),
        )
    return TaskToolResult(success=True, task=task)
