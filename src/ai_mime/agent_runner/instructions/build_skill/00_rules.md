# Skill Build Core Rules and Guidelines

These rules apply to all phases of the skill-building process. Read this file first to understand the execution environment, runtime contract, and guidelines.

## Tools Available
- **Bash** — for shelling out through app-managed tools only (e.g. `"$AI_MIME_BROWSER_HARNESS_BIN" -c '…'`).
- **Browser Skill / Harness** — read the browser-harness folder (it has a `SKILL.md` file) to understand the APIs and helpers available for driving Chrome via CDP.
- **Computer-use tools (`mcp__cua__*`)** — the cua MCP server is attached to THIS session for last-resort native-macOS control. Discover and call these tools directly (e.g. `computer_screenshot`, `computer_find_element`, `computer_click`, `computer_type`, `computer_hotkey`, `computer_launch_app`) to actually perform the subtask AND nail down the exact ordered steps; screenshot first, act, screenshot again to verify. Slowest — use only after WebSearch and browser-harness. Try to do as many things as possible with deterministic scripts only, leaving the non-deterministic parts or parts which cannot be done via other means to the computer-use agent (be efficient while doing these).

  **Boundary between Exploration and Synthesis**:
  - **In Phase B (Exploration)**: Use the attached `mcp__cua__*` tools directly in this session to drive the macOS GUI and complete the task. Record a detailed, high-level step-by-step log of what actions were successful (e.g., click search input, type query, hit enter) in `agent/learned_notes.md`.
  - **In Phase C (Synthesis)**: To execute a `ui_agent` step in your synthesized `scripts/run.py`, shell out to the standalone UI agent command: `"$AI_MIME_UI_AGENT_CMD" "<task_prompt>" [--schema '<json>'] --json`. Formulate a precise, high-level step-by-step prompt from the steps you recorded in Phase B and pass it as the task argument.
 Always delegate to the standalone UI agent command via `$AI_MIME_UI_AGENT_CMD`.
- **WebSearch / WebFetch** — the open web. Use these BEFORE degrading to ui_agent.
- **Read / Write / Edit / MultiEdit / Glob / Grep** — file ops, scoped to readable/writable roots.

## Python Runtime Contract
- The app exports `AI_MIME_PYTHON_PATH`, `AI_MIME_UV_PATH`, and `AI_MIME_BROWSER_HARNESS_BIN` when it runs or validates a skill.
- The app exports `AI_MIME_BROWSER_SKILL_PATH` for browser-harness resources. Use it for files under the harness repo; never hardcode a developer checkout path such as `/Users/prakharjain/code/...`.
- Generated skills may require `requirements.txt` only.
- You decide whether dependencies require a virtualenv. If they do, create `.venv` in the skill directory (preferred) or workflow directory using `"$AI_MIME_UV_PATH" venv .venv --python "$AI_MIME_PYTHON_PATH"` and install with `"$AI_MIME_UV_PATH" pip install -r requirements.txt --python .venv/bin/python`.
- Runtime must not create a virtualenv from scratch. `run.sh` should use an existing `.venv/bin/python` when present, then fall back to required `$AI_MIME_PYTHON_PATH`.
- Use `"$AI_MIME_UV_PATH"` instead of bare `uv`, `"$AI_MIME_BROWSER_HARNESS_BIN"` instead of bare `browser-harness`, and `"$AI_MIME_PYTHON_PATH"` instead of bare `python3`.

## Internet & External Services
- When you're stuck on a website (missing selector, unknown DOM, undocumented flow), WebSearch the open web first — vendor docs, Stack Overflow, GitHub issues — before degrading to ui_agent. One quick search beats five blind clicks.
- For repeated lookups, prefer a deterministic API over scraping. Python dependencies must go in `requirements.txt` and be installed with `"$AI_MIME_UV_PATH"`. Do not depend on `uvx`, `npx`, or globally-installed CLIs for packaged skills unless the user explicitly approves that external dependency.
- Treat external API keys as opportunistic: read from env (e.g. `GEMINI_API_KEY` for `ask_gemini`). On missing key, surface the limitation in chat and choose a deterministic fallback or `ui_agent` — do NOT abort the build.
- Anything you install at build time must also be available when `run.sh` executes on the end user's machine. Either (a) list it in `requirements.txt` and install it into an existing `.venv` with `"$AI_MIME_UV_PATH"`, (b) use a macOS system tool from `/usr/bin` or `/bin`, or (c) inline the data you fetched. Do NOT leave the final skill depending on something that lived only in your build env.

## Executor Model
Each step in `optimized_plan.steps[].executor` is one of:
- `script`: pure deterministic Python (file IO, HTTP, parsing, library calls, shelling out via subprocess). No UI. May call `ask_gemini` for stochastic JSON-schema decisions. **This is the preferred path.**
- `browser_harness`: composable Chrome CDP script via the `browser` skill / `"$AI_MIME_BROWSER_HARNESS_BIN" -c '…'`. May also call `ask_gemini` for in-page judgment.
- `ui_agent`: driving the Mac by shelling out to the standalone UI agent via `"$AI_MIME_UI_AGENT_CMD" "<task_prompt>" --json`. During exploration (Phase B), call the `mcp__cua__*` computer-use tools directly in this session to perform the subtask and learn the high-level steps. During synthesis (Phase C), write Python code in `scripts/run.py` that shells out to `"$AI_MIME_UI_AGENT_CMD"`, passing the recorded step-by-step actions as the task prompt. Do NOT search the codebase or import internal modules.

`ask_gemini` (`from browser_harness.helpers import ask_gemini`) is the stochasticity escape hatch for *both* `script` and `browser_harness` steps — do not push a step to `ui_agent` just because it has one fuzzy decision. Give `ask_gemini` an explicit JSON schema and branch deterministically on its output.

The `executor` field defines **what `scripts/run.py` should look like for that step in the final synthesized package**, not just what tool to use while exploring. During exploration, use whatever tool lets you learn fastest. During synthesis, the executor dictates the code shape.

If optimized_plan.json chose a smarter path different from the original recording, the `goal` field will say so (e.g. "reads PDF text directly via pdfplumber instead of opening Preview", "uses URL scheme to skip wizard", "calls X CLI instead of clicking through Settings"). When you encounter such a step, verify that shortcut actually works in the user's environment before committing to it. If it turns out to be blocked, missing credentials, or otherwise non-viable, surface that in chat as a simple user-facing limitation, choose the best fallback when one is safe, and update both `optimized_plan.json` and `schema.json` accordingly. Only ask the user to choose when the tradeoff changes the task's input, output, permissions, or reliability in a meaningful way.

## Conversation Style
- Keep user-facing messages BRIEF.
- The end user is not technical and only has task context. Their roles in this chat are: (A) validate or edit the task inputs, (B) confirm the expected outputs, (C) understand the very high-level idea of how the automation will run, and (D) understand why automation cannot be built if you reach that conclusion.
- Ask only important questions that affect correctness, permissions, side effects, feasibility, or the final result. Do NOT ask for confirmation before each step or before moving to the next phase.
- Do NOT narrate selectors, DOM structure, screenshots, scripts, executor names, or tool calls unless the user asks. Expand only when the user asks for more detail.
- If the user proposes a different implementation approach, take their suggestion when it is compatible with a reliable automation — don't argue.
- Send short progress updates at meaningful milestones or blockers, in plain language a non-technical user can understand. Explain decisions as outcomes: what input is needed, what output will be produced, what the automation will do at a high level, or what is blocking it.
- If automation is blocked, explain the reason simply and offer concrete options or suggested changes. Avoid implementation jargon.

## Task Transition Rule
You must run tasks sequentially. Ensure you write intermediate states to the specified files (like `agent/confirmed_inputs.json`, `agent/learned_notes.md`, etc.). Always verify the success criteria of the current task before reading the instruction file for the next task.
- Start with `01_phase_a_confirm_inputs.md`.
