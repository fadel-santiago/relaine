"""Minimal CLI for persisted R3 proposal approval and execution."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from relaine.approval_workflow import ApprovalService
from relaine.config import OllamaConfig
from relaine.schemas import ProposalStatus, WorkflowStatus
from relaine.storage import TaskStoreConfig
from relaine.workflow import build_default_engine


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be non-negative")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="relaine-control")
    parser.add_argument("--db-path", type=Path, help="shared SQLite database path")
    commands = parser.add_subparsers(dest="command", required=True)

    submit = commands.add_parser("submit", help="generate and persist a proposal")
    submit.add_argument("request", nargs="+")
    submit.add_argument("--model", help="already-installed Ollama model")
    submit.add_argument("--base-url", help="Ollama API base URL")

    inspect = commands.add_parser("inspect", help="inspect a persisted proposal")
    inspect.add_argument("proposal_id")

    history = commands.add_parser("history", help="show append-only proposal history")
    history.add_argument("proposal_id")

    approve = commands.add_parser("approve", help="approve the reviewed proposal")
    approve.add_argument("proposal_id")
    approve.add_argument("fingerprint")

    reject = commands.add_parser("reject", help="reject the reviewed proposal")
    reject.add_argument("proposal_id")
    reject.add_argument("fingerprint")

    recover = commands.add_parser("recover", help="reconcile an interrupted execution")
    recover.add_argument("proposal_id")
    recover.add_argument("fingerprint")

    recover_list = commands.add_parser(
        "recover-list",
        help="list unfinished executions for operator triage",
    )
    recover_list.add_argument(
        "--older-than-seconds",
        type=_non_negative_int,
        help="include records at least this old when timestamps are valid",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    database_path = args.db_path or TaskStoreConfig.from_env().path

    if args.command == "submit":
        defaults = OllamaConfig.from_env()
        engine = build_default_engine(
            OllamaConfig(
                model=args.model or defaults.model,
                base_url=args.base_url or defaults.base_url,
            )
        )
        result = ApprovalService(database_path, engine).submit(" ".join(args.request))
        print(result.model_dump_json(indent=2))
        return 1 if result.error or result.workflow_result.status is WorkflowStatus.FAILED else 0

    service = ApprovalService(database_path)
    if args.command == "recover-list":
        result = service.list_stalled_executions(args.older_than_seconds)
        print(result.model_dump_json(indent=2))
        return 1 if result.error else 0
    if args.command == "history":
        result = service.history(args.proposal_id)
        print(result.model_dump_json(indent=2))
        return 1 if result.error else 0
    if args.command == "inspect":
        result = service.inspect(args.proposal_id)
    elif args.command == "approve":
        result = service.approve(args.proposal_id, args.fingerprint)
    elif args.command == "reject":
        result = service.reject(args.proposal_id, args.fingerprint)
    else:
        result = service.recover(args.proposal_id, args.fingerprint)
    print(result.model_dump_json(indent=2))
    if not result.success:
        return 1
    return 1 if result.proposal.status is ProposalStatus.FAILED else 0
