"""Local-only development server entry point for RELAINE R5a."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from relaine.api import create_app
from relaine.config import OllamaConfig
from relaine.storage import TaskStoreConfig


LOCAL_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8000


def _port(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="relaine-api")
    parser.add_argument("--db-path", type=Path, help="shared SQLite database path")
    parser.add_argument("--model", help="already-installed Ollama model")
    parser.add_argument("--base-url", help="Ollama API base URL")
    parser.add_argument("--port", type=_port, default=DEFAULT_API_PORT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    defaults = OllamaConfig.from_env()
    application = create_app(
        database_path=args.db_path or TaskStoreConfig.from_env().path,
        ollama_config=OllamaConfig(
            model=args.model or defaults.model,
            base_url=args.base_url or defaults.base_url,
        ),
    )
    uvicorn.run(
        application,
        host=LOCAL_API_HOST,
        port=args.port,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
