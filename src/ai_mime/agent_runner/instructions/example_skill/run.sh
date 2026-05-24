#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
INPUTS="${1:-$HERE/inputs/inputs.example.json}"

PYTHON="${AI_MIME_PYTHON_PATH:?AI_MIME_PYTHON_PATH is required}"

if [[ -x "$HERE/.venv/bin/python" ]]; then
  PYTHON="$HERE/.venv/bin/python"
elif [[ -x "$HERE/../../.venv/bin/python" ]]; then
  PYTHON="$HERE/../../.venv/bin/python"
fi

exec "$PYTHON" "$HERE/scripts/run.py" --inputs-json "$INPUTS"
