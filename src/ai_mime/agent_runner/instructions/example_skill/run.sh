#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# ai-mime-env: load standalone env when not already provided by AI Mime
if [[ -z "${AI_MIME_PYTHON_PATH:-}" && -f "$HERE/.env" ]]; then
  set -a; . "$HERE/.env"; set +a
fi
INPUTS="${1:-$HERE/inputs/inputs.example.json}"

PYTHON="${AI_MIME_PYTHON_PATH:?AI_MIME_PYTHON_PATH is required}"

if [[ -x "$HERE/.venv/bin/python" ]]; then
  PYTHON="$HERE/.venv/bin/python"
elif [[ -x "$HERE/../../.venv/bin/python" ]]; then
  PYTHON="$HERE/../../.venv/bin/python"
fi

exec "$PYTHON" "$HERE/scripts/run.py" --inputs-json "$INPUTS"
