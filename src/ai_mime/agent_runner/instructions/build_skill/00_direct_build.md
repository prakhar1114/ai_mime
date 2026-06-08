# Direct Build — Define the Skill

Use this phase for direct skill builds created from the user's task description.
There is no reflected recording. The source of truth is the user's chat, your
live exploration, `agent/confirmed_inputs.json`, and `agent/learned_notes.md`.

## Instructions
1. Ask the user for the task goal, required inputs, expected outputs, preferred approach, constraints, and side-effect concerns. Keep the questions compact and user-facing.
2. Confirm the final inputs, outputs, and high-level approach in chat before building. Do not proceed until the user explicitly confirms.
3. Write `agent/confirmed_inputs.json` as a JSON object containing runnable example values for every input. These values will be used for end-to-end validation.
4. Write `agent/learned_notes.md` with:
   - Task goal.
   - Confirmed inputs and outputs.
   - High-level approach.
   - Any permissions, files, websites, accounts, APIs, or native apps needed.
   - Any selectors, commands, URLs, payload shapes, UI-agent recipes, or edge cases discovered during exploration.
   - Side effects and how to clear them before reruns.
5. If you need to learn how to perform the task, explore using the tools from `00_rules.md`. Prefer deterministic script, CLI, API, file, and browser-harness approaches before native UI automation.
6. If the task cannot be made into a reliable skill, write the terminal signal with `skill_unbuildable` as described in `04_phase_d_packaging.md`.

## Success Criteria / Gating
Before moving to Phase C, verify that:
- The user has confirmed the task goal, inputs, outputs, and high-level approach.
- `agent/confirmed_inputs.json` exists and is a valid JSON object.
- `agent/learned_notes.md` exists and contains enough detail to implement the skill.
- You have printed one short progress update in chat that the task definition is confirmed.

Once these criteria are met, proceed to Phase C by reading `03_phase_c_synthesis.md`.
