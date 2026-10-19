# RELAINE Workflow Agent — R1

R1 is a proposal-only workflow. It accepts a business request, asks a configured
local Ollama model for structured output, validates it, and returns one of:
`pending_approval`, `unsupported`, or `failed`. It never executes a proposed
action or modifies business data.

## Setup and CLI

Python 3.12 and an already-installed Ollama model are required. The default model
is `gpt-oss:20b`; select a different installed model without downloading it:

```console
export RELAINE_OLLAMA_MODEL=localai-fast:latest
uv sync
uv run relaine "Create a task titled Prepare the quarterly report"
```

The Ollama endpoint defaults to `http://127.0.0.1:11434` and can be changed with
`RELAINE_OLLAMA_BASE_URL`.

Mocked tests run by default. The smoke test performs real local inference and is
explicitly selected:

```console
uv run pytest
uv run pytest -m ollama tests/test_ollama_smoke.py
```

## Reproducible release verification

R5c-2 was verified with CPython 3.12.15 and uv 0.12.23. The locked runtime can
be installed and the wheel checked in a disposable environment with:

```console
uv lock --check
uv export --frozen --no-dev --no-emit-project --format requirements-txt \
  --output-file /tmp/relaine-requirements.txt
uv venv --python 3.12 /tmp/relaine-release-env
uv pip sync --require-hashes --python /tmp/relaine-release-env/bin/python \
  /tmp/relaine-requirements.txt
uv build --wheel --out-dir /tmp/relaine-dist
uv pip install --python /tmp/relaine-release-env/bin/python --no-deps \
  /tmp/relaine-dist/relaine_workflow_agent-0.1.0-py3-none-any.whl
/tmp/relaine-release-env/bin/relaine --help
/tmp/relaine-release-env/bin/relaine-control --help
/tmp/relaine-release-env/bin/relaine-api --help
/tmp/relaine-release-env/bin/relaine-api \
  --db-path /tmp/relaine-release.sqlite3 --port 8000
```

The API command is a loopback-only development server without authentication;
do not expose it through a public proxy. Proposal generation still requires a
separately running, already-installed Ollama model, and the default test run
intentionally excludes tests marked `ollama`.
