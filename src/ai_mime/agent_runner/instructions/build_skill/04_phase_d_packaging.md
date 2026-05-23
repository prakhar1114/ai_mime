# Phase D — Package as a standard skill

In this phase, you will write the markdown documentation, wrap execution in `run.sh`, create example/template input files, and signal that the skill is ready.

## Instructions
1. Invoke the `skill-creator` skill to scaffold `SKILL.md`. It MUST have YAML frontmatter (non-empty `name`, `description`) and these sections (titles exact): `## Inputs`, `## Run`, `## Outputs`, `## Progress log format`, `## Fallback`, `## ask_gemini decision points`, `## References`.

   `SKILL.md` `## Run` must document the Python runtime contract:
   - `run.sh` uses the first available interpreter in this order: skill `.venv/bin/python`, workflow `.venv/bin/python`, then required `$AI_MIME_PYTHON_PATH`.
   - If `requirements.txt` exists, include these exact build/repair commands:
       ```bash
       "$AI_MIME_UV_PATH" venv .venv --python "$AI_MIME_PYTHON_PATH"
       "$AI_MIME_UV_PATH" pip install -r requirements.txt --python .venv/bin/python
       ```
   - State clearly that the install commands are for skill build or manual repair. Runtime does not create or repair `.venv`.

2. Write the final layout at `{skill_dir}/`. Required files (validated by `validate_skill_package`):
   - `SKILL.md`                       — skill-creator format, sections above.
   - `scripts/run.py`                 — from Phase C.
   - `requirements.txt`               — optional, only when `scripts/run.py` needs third-party Python packages. If present, you must create `.venv` and install it during skill build before signalling.
   - `run.sh`                         — one-click wrapper, `chmod +x`. Body:
       ```bash
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
       ```
   - `inputs/inputs.example.json`     — copy of `agent/confirmed_inputs.json`. Re-runnable as-is.
   - `inputs/inputs.template.json`    — same keys as example, but each value is `"<FILL IN: <one-line description>>"` (or the input's recorded default).
   - `references/fallback_plan.md`    — REQUIRED. Synthesized from `schema.plan.subtasks[]` + matching `optimized_plan.steps[]`. Per subtask: heading, one-line `Intent:`, the recorded sub-steps as bullets, `Notes:` with selectors / URLs / traps learned in Phase B. A human or the UI agent must be able to finish the task from this file alone if `run.sh` fails.

3. Free-form `references/`. Beyond `fallback_plan.md`, write whatever notes help a future runner — domain notes, per-subtask notes, selectors, payload shapes. You decide based on what was actually useful in Phase B. Don't force everything into one `learned_notes.md`.

4. Do NOT copy `schema.json` or `optimized_plan.json` into the skill. They're builder-only artifacts.

   Reproducibility: any external tool / MCP server / API you relied on during the build must also be reachable when `run.sh` runs on the end user's machine. Browser-harness is available in the AI Mime workflow runtime as `$AI_MIME_BROWSER_HARNESS_BIN`; use `$AI_MIME_BROWSER_SKILL_PATH` for harness resource files. If Python packages are needed, list them in `requirements.txt`, create `.venv`, install them with `"$AI_MIME_UV_PATH"` during skill build, and document that `run.sh` will use the existing `.venv`. Do NOT assume the end user has anything pre-installed beyond `bash`, macOS system tools, `$AI_MIME_UV_PATH`, `$AI_MIME_BROWSER_HARNESS_BIN`, `$AI_MIME_PYTHON_PATH`, and an already-created `.venv` when needed.

5. Verify `./run.sh` runs clean against `inputs/inputs.example.json`.

6. Write `{signal_path}` (terminal signal file `agent/build_signal.json`) with `{"status":"skill_ready","summary":"<one line>"}` and stop. The service runs `validate_skill_package` + `run_skill_e2e_test` and surfaces the result in the UI.

7. Unbuildable escape (only reachable from Phase B or C after trying safe fallbacks and asking any necessary task-level questions): write `{signal_path}` with `{"status":"skill_unbuildable","reason":"<plain-language concrete reason>","suggested_changes":["<plain-language suggested change>","..."]}` and stop.
