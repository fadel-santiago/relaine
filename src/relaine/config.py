"""Runtime configuration for the local Ollama model."""

from __future__ import annotations

import os
from dataclasses import dataclass


DEFAULT_OLLAMA_MODEL = "gpt-oss:20b"
DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"


@dataclass(frozen=True, slots=True)
class OllamaConfig:
    """Configuration only; R1 never installs or pulls a model."""

    model: str = DEFAULT_OLLAMA_MODEL
    base_url: str = DEFAULT_OLLAMA_BASE_URL

    @classmethod
    def from_env(cls) -> "OllamaConfig":
        return cls(
            model=os.getenv("RELAINE_OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL).strip(),
            base_url=os.getenv(
                "RELAINE_OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL
            ).strip(),
        )
