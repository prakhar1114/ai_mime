# Phase A — Confirm and finalize inputs and outputs

In this phase, you will confirm the schema inputs, plan steps, and approach with the user, update the plan schema if necessary, and write the verified inputs.

## Instructions
1. Read the schema (`schema.json`) and the optimized plan (`optimized_plan.json`). Show the inputs from `optimized_plan.inputs[]` and expected outputs from `optimized_plan.steps[].outputs` as a compact plain-language summary in chat. Also include one very high-level sentence describing the approach of how the automation will run.
2. ALWAYS ask the user to explicitly confirm these inputs, outputs, and the approach. Do NOT proceed to Phase B or execute any plan steps until the user has explicitly replied confirming they are correct.
3. Treat user-proposed *additional* inputs as first-class — don't push back unless they conflict with the recorded behavior.
4. For any change (edit, add, remove, rename), update BOTH files atomically:
   - `optimized_plan.json` — `inputs[]`.
   - `schema.json` — matching `task_params[]` entry (and any `{{placeholder}}` in `plan.subtasks[].text` if relevant).
   Read each file back to verify, and confirm the changes in one line in chat before continuing.
5. Do NOT modify `task_name` unless the user explicitly asks — the skill directory slug derives from it.
6. Persist final confirmed values to `agent/confirmed_inputs.json`. These are what `scripts/run.py --inputs-json` will receive at validation time.

## Success Criteria / Gating
Before moving to Phase B, you must verify that:
- You have presented the inputs, outputs, and approach summary to the user.
- The user has explicitly responded and confirmed that the inputs, outputs, and approach are correct.
- `agent/confirmed_inputs.json` exists and contains a valid JSON object matching the keys and default/user-supplied values from the inputs.

Once these criteria are met, proceed to Phase B by reading `02_phase_b_execute_steps.md`.
