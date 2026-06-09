# Phase C — Synthesize and validate `scripts/run.py`

In this phase, you will write the final deterministic python execution script, run tests against it, and iterate until it runs clean.

## Instructions
1. Create or overwrite the skill script at `{skill_dir}/scripts/run.py` using details from `agent/learned_notes.md`. (Note: The skill directory `{skill_dir}` is located at `<workflow_dir>/skills/<skill_name>/` under the workflow directory. You can refer to the example script at `instructions/example_skill/scripts/run.py` to see a working reference for how to parse inputs and format progress logs).
2. For reflected workflows, per-step code shape must match its `executor` in the optimized plan. For direct builds, choose the simplest reliable execution shape from `agent/learned_notes.md` and the confirmed approach:
   - `script` → inline Python.
   - `browser_harness` → shell out to `"$AI_MIME_BROWSER_HARNESS_BIN" -c '…'` (or import helpers directly).
   - `ui_agent` → read the sibling UI-agent guide at `../ui_agent/00_ui_agent.md` under the shared instructions root, distill the matching UI Agent Recipe from `agent/learned_notes.md` into a compact task prompt, then shell out to `"$AI_MIME_UI_AGENT_CMD" "<task>" [--schema '<json>'] --json` and parse `result_json` from stdout. The prompt should include task-specific setup, action sequence, decision rules, gotchas, recovery, skip conditions, and final verification; do not paste the full guide or learned-notes file into the prompt.
     - **Python Invocation Example**:
       ```python
       import os, shlex, subprocess, json
       ui_agent_cmd = os.environ.get("AI_MIME_UI_AGENT_CMD")
       task_prompt = "Target: the open web browser. Goal: search for weather. If the search page is already open, skip navigation. Click the search input, type 'weather', press Enter, and verify results are visible. If focus is uncertain, re-focus the search input before typing."
       cmd = shlex.split(ui_agent_cmd) + [task_prompt, "--json"]
       proc = subprocess.run(cmd, stdout=subprocess.PIPE, text=True, check=True)
       result = json.loads(proc.stdout)
       # result is a dict containing {"status": "success"|"failed", "result_json": {...}, "summary": "..."}
       ```
3. Code Contract:
   - Invocation: `"$AI_MIME_PYTHON_PATH" scripts/run.py --inputs-json /path/to/inputs.json` or `./run.sh /path/to/inputs.json`. Read all inputs up front, do not prompt for inputs.
   - For irreducible judgment, call `ask_llm` with an explicit JSON schema. Pattern:
     ```python
     from llm_resolver import ask_llm
     pick = ask_llm(prompt, schema={"type":"object","properties":{"id":{"type":["string","null"]},"reason":{"type":"string"}},"required":["id","reason"]})
     ```
     Inside a browser-harness `-c` block ask_llm is preimported
     Branch deterministically on the returned dict. Document each call site in `SKILL.md`.
   - Emit progress logs continuously (step id + title) on stderr:
     - `{"event":"step_start","id":"<step_id>","title":"…"}`
     - `{"event":"step_done","id":"<step_id>","outputs":{…},"summary":"…"}`
     - `{"event":"step_failed","id":"<step_id>","error":"…","recoverable":true|false}`
     - `{"event":"workflow_done","outputs":{…}}`
     Free-form human logs may be interleaved. Exit non-zero on `step_failed`.
4. Clear any Phase-B side effects before testing. Print a one-line request to the user to confirm that `agent/side_effects.md` entries are cleared.
5. Run the assembled script end-to-end against `agent/confirmed_inputs.json` (e.g. via `"$AI_MIME_PYTHON_PATH" scripts/run.py --inputs-json agent/confirmed_inputs.json`). Verify that the same end state Phase B reached is achieved.
6. If it fails: diagnose, patch `scripts/run.py`, ask the user to clear new side effects, and re-run. Loop until it runs end-to-end cleanly.

## Success Criteria / Gating
Before moving to Phase D, you must verify that:
- `{skill_dir}/scripts/run.py` exists and is syntactically valid Python (stored in `<workflow_dir>/skills/<skill_name>/scripts/run.py`).
- The script has run end-to-end successfully against `agent/confirmed_inputs.json`.
- When the e2e script runs clean, do not ask for packaging approval. Send one short progress update such as "The full automation ran successfully. I'm turning it into a reusable skill now."

Once these criteria are met, proceed to Phase D by reading `04_phase_d_packaging.md`.
