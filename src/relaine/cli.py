"""Minimal JSON CLI for RELAINE R1."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from relaine.config import OllamaConfig
from relaine.schemas import WorkflowStatus
from relaine.workflow import build_default_engine


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="relaine",
        description="Generate a validated R1 task proposal using local Ollama.",
    )
    parser.add_argument("request", nargs="+", help="business request to evaluate")
    parser.add_argument(
        "--model",
        help="name of an already-installed Ollama model",
    )
    parser.add_argument("--base-url", help="Ollama API base URL")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    defaults = OllamaConfig.from_env()
    config = OllamaConfig(
        model=args.model or defaults.model,
        base_url=args.base_url or defaults.base_url,
    )
    result = build_default_engine(config).propose(" ".join(args.request))
    print(result.model_dump_json(indent=2))
    return 1 if result.status is WorkflowStatus.FAILED else 0
