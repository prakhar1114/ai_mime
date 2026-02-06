#!/usr/bin/env bash
# Create AppIcon.icns from docs/logo/*.png.
# Uses Pillow (already a project dep) to resize + write the ICNS binary directly,
# avoiding iconutil which is broken on macOS 26+.
#
# Usage:  bash scripts/create_icns.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

python3 "$SCRIPT_DIR/_create_icns.py" "$REPO_ROOT"
