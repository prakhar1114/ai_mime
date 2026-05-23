# Replay Execution Core Rules and Guidelines

These rules apply to all replay operations. Read this file first to understand the execution environment, runtime contract, and guidelines.

## Environment Details
- **Browser Skill / Harness** — read the browser-harness folder (it has a `SKILL.md` file) to understand the APIs and helpers available for driving Chrome via CDP.
- **Computer-use tools (`mcp__cua__*`)** — attached to THIS session for last-resort native-macOS control.discover and call these tools directly (`computer_screenshot`, `computer_find_element`, `computer_click`, `computer_type`, `computer_hotkey`, …) to drive native apps and hostile DOMs; screenshot first, act, screenshot again to verify. (The skill's own `scripts/run.py` hands the same subtask to `run_computer_use_task` via `"$AI_MIME_UI_AGENT_CMD"` — an agent that drives the SAME cua MCP server — so it reproduces the steps you just performed.)
- **Bash** — for shelling out through app-managed tools.
- **WebSearch / WebFetch** — the open web.

## Python Runtime Contract
- Use `$AI_MIME_PYTHON_PATH` instead of bare `python` / `python3`.
- Use `$AI_MIME_UV_PATH` instead of bare `uv`.
- Use `$AI_MIME_BROWSER_HARNESS_BIN` instead of bare `browser-harness`.
- The skill's `run.sh` will resolve and use the existing `.venv` if one exists.

## Conversation Style
- Keep user-facing messages BRIEF.
- Respond in the Replay page chat. Help run the existing skill, validate inputs, and handle variants of the task using the skill context.

## Task Transition Rule
- Start with `01_replay.md` to begin execution.
