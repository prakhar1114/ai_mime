# Phase B — Execute optimized_plan steps one at a time

In this phase, you will execute each step of the optimized plan to discover selectors, URLs, CLI commands, APIs, and gotchas, and record them.

## Strong Preference
Strong preference for `browser_harness` over `ui_agent` whenever the target is in a browser. browser-harness is very powerful: compositor-level clicks pass through iframes/shadow DOM, raw CDP for anything helpers miss, parallel HTTP. Only fall back to `ui_agent` if CDP genuinely cannot survive replay.

## Instructions
For each `optimized_plan.steps[i]` in order, work autonomously, verify internally, and send only brief plain-language milestone updates or blocker messages.

1. Look up the matching `schema.plan.subtasks[].steps[]` via `source_subtask_ids` for fine-grained intent. `step.goal` may describe a smarter path than the recording (API / CLI / file / URL-scheme); honor it.
2. Match `step.executor` to the execution shape:
   - `script` → Python via Bash (subprocess / requests / pdfplumber / file IO). `ask_gemini` for stochastic JSON decisions.
   - `browser_harness` → Chrome via the `browser` skill / `"$AI_MIME_BROWSER_HARNESS_BIN" -c '…'`. `ask_gemini` for in-page judgment.
   - `ui_agent` → call the `mcp__cua__*` computer-use tools directly (screenshot → find_element → click/type, re-screenshot to verify) to perform the step live.
   If you swap `ui_agent` → `browser_harness` because browser-harness can handle the target, update the `optimized_plan.json` step's `executor` to match. Mention the user-visible effect only if it matters, e.g. "I found a more reliable way to do this in the browser."
3. Execute against the live environment using `agent/confirmed_inputs.json`. Verify success (screenshots / page_info / shell output) before declaring the step done.
4. Append durable findings to `agent/learned_notes.md` — selectors, URLs, payload shapes, traps. Make this a clear technical map of how to reproduce the step, not a diary.
5. Side effects: after any non-idempotent change, append a one-liner to `agent/side_effects.md` (what was created, how to undo). Before retrying a step or re-running from earlier, stop and ask the user to clear the prior side effect — cite the specific ledger entry in plain language; wait for explicit "cleared".
6. If the step can't be made to work: first do ONE WebSearch for the failure mode / selector / API — many "hostile DOM" problems have a documented workaround. Only after that, surface it briefly in non-technical language, propose options (change inputs, change expected output, use a less reliable fallback, or declare unbuildable), and ask the user only when the choice affects their task.
7. Do not pause after successful individual steps. Continue to the next step on your own.

## Success Criteria / Gating
Before moving to Phase C, you must verify that:
- You have executed all steps of the optimized plan sequentially.
- `agent/learned_notes.md` exists and contains the necessary details (selectors, URLs, paths, or code snippets) for every step.
- You have printed: "All <N> steps complete end-to-end" in chat.

Once these criteria are met, proceed to Phase C by reading `03_phase_c_synthesis.md`.
