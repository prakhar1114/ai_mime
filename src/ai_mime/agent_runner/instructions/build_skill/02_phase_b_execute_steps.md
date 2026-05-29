# Phase B — Execute optimized_plan steps one at a time

In this phase, you will execute each step of the optimized plan to discover selectors, URLs, CLI commands, APIs, and gotchas, and record them.

## Strong Preference & Creative Upgrades (Bash/CLI/AppleScript > Browser-Harness > UI Agent)
To build extremely fast, robust, and elegant automations, you must be highly creative and prioritize execution mechanisms in this exact order:
1. **`script` (Bash, macOS CLI commands, AppleScript/JXA, direct file reads/writes, back-end APIs, SQLite, plist edits)**:
   - **This is your absolute first priority.**
   - Whenever you see any step mapped to `ui_agent` or `browser_harness`, ask yourself: **Can we achieve this end state deterministically without opening any UI/browser?**
   - **MacOS CLI Examples**: Use `open -a "AppName"` to launch apps, `open "URL"` for deep-linking, `defaults write/read` to update system or application plists, or SQL queries directly into the app's SQLite DBs.
   - **AppleScript (`osascript`) Examples**: Use AppleScript to control system volume, check window states, trigger menu-bar items, select options, copy/paste, or send keystrokes to specific applications (e.g., `osascript -e 'tell application "Spotify" to play track "..."'`).
   - **Task Decomposition**: Split complex hybrid steps. If you have a sequence of actions, perform the setup, data loading, or launch steps deterministically via `bash`/`osascript` and only use the UI agent/CDP for the strictly irreducible parts.
2. **`browser_harness` (CDP Chrome scripting)**:
   - Use this if the action requires in-browser web pages but does not need native OS GUI. It is much faster and more reliable than `ui_agent`.
3. **`ui_agent` (Computer-use vision-GUI agent)**:
   - Use only as an absolute last resort when no CLI, AppleScript, API, file, or browser-harness method is viable.

### Web Search for Discovery
If you are ever unsure about how to do a specific task deterministically (e.g., "how to open Spotify playlist via CLI", "AppleScript to play Spotify", "how to change terminal settings via defaults write", "SQLite DB path for Apple Notes"), you **must** do a WebSearch to explore deterministic options, CLI utilities, hidden system paths, or AppleScript commands. Do not default to `ui_agent` without searching first.

### Swapping Executors Dynamically
If you discover a deterministic CLI, AppleScript, or API shortcut for a step that was originally mapped to `ui_agent` or `browser_harness`:
- Execute it successfully via Bash.
- Update the step's `executor` in `optimized_plan.json` to `script` (or `browser_harness` if upgrading from `ui_agent`).
- Document the deterministic CLI command or AppleScript in `agent/learned_notes.md`.

## Instructions
For each `optimized_plan.steps[i]` in order, work autonomously, verify internally, and send only brief plain-language milestone updates or blocker messages.

1. Look up the matching `schema.plan.subtasks[].steps[]` via `source_subtask_ids` for fine-grained intent. `step.goal` may describe a smarter path than the recording (API / CLI / file / URL-scheme); honor it. Active exploration via AppleScript, bash commands, system plists, SQLite DBs, and APIs is highly encouraged.
2. Match `step.executor` to the execution shape:
   - `script` → Python via Bash (subprocess / requests / pdfplumber / file IO / `osascript` AppleScript / direct system commands). `ask_llm` for stochastic JSON decisions.
   - `browser_harness` → Chrome via the `browser` skill / `"$AI_MIME_BROWSER_HARNESS_BIN" -c '…'`. `ask_llm` for in-page judgment.
   - `ui_agent` → call the `mcp__cua__*` computer-use tools directly (screenshot → find_element → click/type, re-screenshot to verify) to perform the step live.
   If you swap `ui_agent` → `browser_harness` or swap either to `script` because you found a smarter deterministic or CDP-based way to handle the target, update the `optimized_plan.json` step's `executor` to match. Mention the user-visible effect only if it matters, e.g. "I found a more reliable way to do this in the browser." or "I optimized this step to run deterministically via command-line shell script."
3. Execute against the live environment using `agent/confirmed_inputs.json`. Verify success (screenshots / page_info / shell output) before declaring the step done.
4. Append durable findings to `agent/learned_notes.md` — selectors, URLs, payload shapes, traps. Make this a clear technical map of how to reproduce the step, not a diary. For any `ui_agent` steps, record a highly precise, ordered list of high-level step-by-step actions (e.g., "1. Find and click search input, 2. Type 'weather', 3. Press key Enter"). This list of actions will be used directly as the task prompt when calling the UI agent during synthesis in Phase C.
5. Side effects: after any non-idempotent change, append a one-liner to `agent/side_effects.md` (what was created, how to undo). Before retrying a step or re-running from earlier, stop and ask the user to clear the prior side effect — cite the specific ledger entry in plain language; wait for explicit "cleared".
6. If the step can't be made to work: first do ONE WebSearch for the failure mode / selector / API — many "hostile DOM" problems have a documented workaround. Only after that, surface it briefly in non-technical language, propose options (change inputs, change expected output, use a less reliable fallback, or declare unbuildable), and ask the user only when the choice affects their task.
7. Do not pause after successful individual steps. Continue to the next step on your own.

## Success Criteria / Gating
Before moving to Phase C, you must verify that:
- You have executed all steps of the optimized plan sequentially.
- `agent/learned_notes.md` exists and contains the necessary details (selectors, URLs, paths, or code snippets) for every step.
- You have printed: "All <N> steps complete end-to-end" in chat.

Once these criteria are met, proceed to Phase C by reading `03_phase_c_synthesis.md`.
