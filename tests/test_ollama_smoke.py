from __future__ import annotations

import json
from urllib.error import URLError
from urllib.request import urlopen

import pytest

from relaine.config import OllamaConfig
from relaine.schemas import ActionType, WorkflowStatus
from relaine.workflow import build_default_engine


def _installed_models(base_url: str) -> set[str]:
    try:
        with urlopen(f"{base_url.rstrip('/')}/api/tags", timeout=2) as response:
            payload = json.load(response)
    except (OSError, URLError, ValueError) as exc:
        pytest.skip(f"Ollama is unavailable: {exc}")
    return {model["name"] for model in payload.get("models", [])}


@pytest.mark.ollama
def test_real_ollama_create_task_smoke() -> None:
    config = OllamaConfig.from_env()
    if config.model not in _installed_models(config.base_url):
        pytest.skip(f"configured model is not installed: {config.model}")

    result = build_default_engine(config).propose(
        "Create a task titled Verify RELAINE R1 smoke test."
    )

    assert result.status is WorkflowStatus.PENDING_APPROVAL, result.model_dump()
    assert result.proposal is not None
    assert result.proposal.action is ActionType.CREATE_TASK
    assert result.proposal.parameters.title == "Verify RELAINE R1 smoke test"
