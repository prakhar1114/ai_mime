from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Protocol

from ai_mime.agent_runner.models import (
    AgentProvider,
    AgentRunMode,
    AgentRunRequest,
    AgentRunResult,
    FilesystemAccess,
    FilesystemAccessEntry,
)
from ai_mime.app_data import get_python_path, get_uv_path, get_workflows_dir, workflow_runtime_env


class AgentAdapter(Protocol):
    def run(self, request: AgentRunRequest, prompt: str) -> AgentRunResult:
        ...


SKILL_E2E_TIMEOUT_SECONDS = 1800
BUILD_SIGNAL_FILENAME = "build_signal.json"
REQUIRED_SKILL_FILES = (
    "SKILL.md",
    "scripts/run.py",
    "run.sh",
    "inputs/inputs.example.json",
    "inputs/inputs.template.json",
    "references/fallback_plan.md",
)


def _read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        obj = json.load(f)
    if not isinstance(obj, dict):
        raise ValueError(f"Expected JSON object at {path}")
    return obj


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _append_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(text)


def _unique_paths(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def _slugify(value: str, *, fallback: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or fallback


def _skill_name_for(schema: dict, workflow_dir: Path) -> str:
    task_name = schema.get("task_name")
    if isinstance(task_name, str) and task_name.strip():
        return _slugify(task_name, fallback=workflow_dir.name)
    return _slugify(workflow_dir.name, fallback="workflow-skill")


def _skill_dir_for(workflow_dir: Path, schema: dict) -> Path:
    return workflow_dir / "skills" / _skill_name_for(schema, workflow_dir)


def _skill_dir_for_request(request: AgentRunRequest) -> Path:
    schema: dict = {}
    if request.schema_path and request.schema_path.exists():
        try:
            schema = _read_json(request.schema_path)
        except Exception:
            schema = {}
    return _skill_dir_for(request.workflow_dir, schema)


def _default_skill_builder_mcp_servers() -> dict[str, dict]:
    """Always-on MCP servers for the build_skill_chat agent.

    Honors `AI_MIME_MCP_SERVERS_JSON` (a JSON object) so the macOS app
    installer can ship zero-config servers without code changes. Invalid
    JSON is ignored — never fails the build.

    TODO: bundle safe out-of-the-box defaults (e.g. a free web-fetch MCP)
    once we've picked them.
    """
    raw = os.getenv("AI_MIME_MCP_SERVERS_JSON")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {k: v for k, v in parsed.items() if isinstance(k, str) and isinstance(v, dict)}


def _filesystem_access_from_plan(optimized_plan: dict) -> FilesystemAccess:
    access = optimized_plan.get("user_filesystem_access") if isinstance(optimized_plan, dict) else {}
    if not isinstance(access, dict):
        access = {}

    def _entries(key: str) -> list[FilesystemAccessEntry]:
        raw = access.get(key)
        if not isinstance(raw, list):
            return []
        entries: list[FilesystemAccessEntry] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "").strip()
            reason = str(item.get("reason") or "").strip()
            if not path or not reason:
                continue
            entries.append(
                FilesystemAccessEntry(
                    path=Path(path).expanduser(),
                    reason=reason,
                    approval_required=bool(item.get("approval_required", False)),
                )
            )
        return entries

    return FilesystemAccess(
        readable_roots=_entries("readable_roots"),
        writable_roots=_entries("writable_roots"),
    )


def build_agent_run_request(
    *,
    workflow_dir: str | Path,
    provider: AgentProvider = "claude",
    mode: AgentRunMode = "execute_optimized_plan",
    model: str | None = None,
    session_id: str | None = None,
) -> AgentRunRequest:
    workflow_dir_p = Path(workflow_dir)
    if mode == "general":
        workspace_dir = get_workflows_dir()
        agent_dir = workspace_dir / ".agent"
        return AgentRunRequest(
            provider=provider,
            mode=mode,
            model=model,
            session_id=session_id,
            workflow_dir=workspace_dir,
            workspace_dir=workspace_dir,
            schema_path=None,
            optimized_plan_path=None,
            readable_roots=_unique_paths([workspace_dir]),
            writable_roots=_unique_paths([agent_dir]),
        )

    schema_path = workflow_dir_p / "schema.json"
    optimized_plan_path = workflow_dir_p / "optimized_plan.json"
    schema = _read_json(schema_path)
    optimized_plan = _read_json(optimized_plan_path)

    access = _filesystem_access_from_plan(optimized_plan)
    agent_dir = workflow_dir_p / "agent"
    outputs_dir = workflow_dir_p / "outputs"
    skills_dir = workflow_dir_p / "skills"

    readable_roots = _unique_paths(
        [
            workflow_dir_p,
            *[entry.path for entry in access.readable_roots],
        ]
    )
    if mode == "replay_execution":
        skill_dir = _skill_dir_for(workflow_dir_p, schema)
        writable_roots = _unique_paths(
            [
                agent_dir,
                outputs_dir,
                outputs_dir / "assets",
                skill_dir,
                *[entry.path for entry in access.writable_roots],
            ]
        )
    else:
        writable_roots = _unique_paths(
            [
                agent_dir,
                outputs_dir,
                outputs_dir / "assets",
                skills_dir,
                # build_skill_chat needs to edit schema.json / optimized_plan.json
                # when the user asks to tweak inputs. _within_roots matches file
                # paths exactly, so this does NOT grant write access to the rest
                # of workflow_dir.
                schema_path,
                optimized_plan_path,
            ]
        )

    mcp_servers: dict | None = None
    if mode == "build_skill_chat":
        mcp_servers = _default_skill_builder_mcp_servers()

    return AgentRunRequest(
        provider=provider,
        mode=mode,
        model=model,
        session_id=session_id,
        workflow_dir=workflow_dir_p,
        workspace_dir=workflow_dir_p,
        schema_path=schema_path,
        optimized_plan_path=optimized_plan_path,
        readable_roots=readable_roots,
        writable_roots=writable_roots,
        user_filesystem_access=access,
        mcp_servers=mcp_servers,
    )


def _load_or_create_session_id(request: AgentRunRequest) -> str:
    session_path = request.workflow_dir / "agent" / "session.json"
    if request.session_id:
        return request.session_id
    if session_path.exists():
        try:
            data = _read_json(session_path)
            sid = data.get("session_id")
            if isinstance(sid, str) and sid.strip():
                return sid
        except Exception:
            pass
    return ""


def _build_prompt(request: AgentRunRequest) -> str:
    memory_path = request.workflow_dir / "agent" / "memory.md"
    memory = memory_path.read_text(encoding="utf-8") if memory_path.exists() else ""
    if request.mode == "general":
        return f"""You are the AI Mime workspace debugging agent.

Mode: {request.mode}
Workspace directory: {request.workspace_dir}
Memory file: {memory_path}

You can inspect workflows, schemas, optimized plans, agent artifacts, and task outputs under the workspace.
Use the provided readable/writable roots as the permission boundary.
Keep file writes deliberate. Summarize durable findings in .agent/memory.md only when useful.

Readable roots:
{json.dumps([str(p) for p in request.readable_roots], indent=2)}

Writable roots:
{json.dumps([str(p) for p in request.writable_roots], indent=2)}

Existing memory:
{memory}
"""

    if request.mode == "build_skill_chat":
        skill_dir = _skill_dir_for_request(request)
        default_python_path = get_python_path(request.workflow_dir)
        uv_path = get_uv_path()
        existing_skill_files = sorted(
            str(path.relative_to(skill_dir))
            for path in skill_dir.rglob("*")
            if skill_dir.exists() and path.is_file()
        )
        signal_path = request.workflow_dir / "agent" / BUILD_SIGNAL_FILENAME
        learned_path = request.workflow_dir / "agent" / "learned_notes.md"
        return f"""You are the AI Mime iterative skill-builder agent for this workflow.
You are running inside a chat panel — the human user is on the other end of every message and is collaborating with you to produce a deterministic, reusable skill package for this workflow.

Workflow directory: {request.workflow_dir}
Schema: {request.schema_path}
Optimized plan: {request.optimized_plan_path}
Memory file: {memory_path}
Learned-notes file (append durable findings here): {learned_path}
Skill directory to create or refine: {skill_dir}
Terminal signal file: {signal_path}

Tools available to you in this environment:
- Bash — for shelling out (e.g. `browser-harness -c '…'` is on $PATH, plus any CLI you want to call).
- Browser Skill — invoke installed skills. The `browser` skill drives Chrome via CDP (see harness/browser-harness/).
- Cua-driver skill — drives native macOS apps via screenshot+click. Slowest; use sparingly.
- WebSearch / WebFetch — the open web. Use these BEFORE degrading to ui_agent.
- Read / Write / Edit / MultiEdit / Glob / Grep — file ops, scoped to readable/writable roots.

Python runtime contract:
- The app exports `AI_MIME_PYTHON_PATH` and `AI_MIME_UV_PATH` when it runs or validates a skill.
- Current resolved default Python: `{default_python_path}`.
- Current resolved uv: `{uv_path}`.
- Generated skills may require `requirements.txt` only.
- You decide whether dependencies require a virtualenv. If they do, create `.venv` in the skill directory (preferred) or workflow directory using `"$AI_MIME_UV_PATH" venv .venv --python "$AI_MIME_PYTHON_PATH"` and install with `"$AI_MIME_UV_PATH" pip install -r requirements.txt --python .venv/bin/python`.
- Runtime must not create a virtualenv from scratch. `run.sh` should use an existing `.venv/bin/python` when present, then fall back to `$AI_MIME_PYTHON_PATH`, then `python3`.

Internet & external services
- When you're stuck on a website (missing selector, unknown DOM, undocumented flow), WebSearch the open web first — vendor docs, Stack Overflow, GitHub issues — before degrading to ui_agent. One quick search beats five blind clicks.
- For repeated lookups, prefer a deterministic API over scraping. You may `npx`, `uvx`, or `pip install` an external CLI or MCP server **only if** it works with no user setup: no API key the end user must obtain, no account creation, no manual permissions. Free public APIs and zero-config MCP servers are fine; anything that prompts the user mid-run is not.
- Treat external API keys as opportunistic: read from env (e.g. `GEMINI_API_KEY` for `ask_gemini`). On missing key, surface the limitation in chat and choose a deterministic fallback or `ui_agent` — do NOT abort the build.
- Anything you install at build time must also be available when `run.sh` executes on the end user's machine. Either (a) `pip install` it as a script dep and document it in SKILL.md, (b) shell out to a globally-available tool (`curl`, `npx --yes`, `uvx`), or (c) inline the data you fetched. Do NOT leave the final skill depending on something that lived only in your build env.

You can ask the user clarifying questions in chat when the answer affects whether the automation can be built correctly. Do NOT use osascript / native dialogs — the user is in the browser.

Executor model — each step in `optimized_plan.steps[].executor` is one of:
- `script`: pure deterministic Python (file IO, HTTP, parsing, library calls, shelling out via subprocess). No UI. May call `ask_gemini` for stochastic JSON-schema decisions. **This is the preferred path.**
- `browser_harness`: composable Chrome CDP script via the `browser` skill / `browser-harness -c '…'`. May also call `ask_gemini` for in-page judgment.
- `ui_agent`: screenshot+click loop via `macos-computer-use`. Last resort for native UIs or hostile DOMs.

`ask_gemini` (`from browser_harness.helpers import ask_gemini`) is the stochasticity escape hatch for *both* `script` and `browser_harness` steps — do not push a step to `ui_agent` just because it has one fuzzy decision. Give `ask_gemini` an explicit JSON schema and branch deterministically on its output.

The `executor` field defines **what `scripts/run.py` should look like for that step in the final synthesized package**, not just what tool to use while exploring. During exploration, use whatever tool lets you learn fastest. During synthesis, the executor dictates the code shape.

If Pass C chose a smarter path different from the original recording, the `goal` field will say so (e.g. "reads PDF text directly via pdfplumber instead of opening Preview", "uses URL scheme to skip wizard", "calls X CLI instead of clicking through Settings"). When you encounter such a step, verify that shortcut actually works in the user's environment before committing to it. If it turns out to be blocked, missing credentials, or otherwise non-viable, surface that in chat as a simple user-facing limitation, choose the best fallback when one is safe, and update both `optimized_plan.json` and `schema.json` accordingly. Only ask the user to choose when the tradeoff changes the task's input, output, permissions, or reliability in a meaningful way.

Conversation style — keep user-facing messages BRIEF.
The end user is not technical and only has task context. Their roles in this chat are: (A) validate or edit the task inputs, (B) confirm the expected outputs, (C) understand the very high-level idea of how the automation will run, and (D) understand why automation cannot be built if you reach that conclusion. Ask only important questions that affect correctness, permissions, side effects, feasibility, or the final result. Do NOT ask for confirmation before each step or before moving to the next phase. Do NOT narrate selectors, DOM structure, screenshots, scripts, executor names, or tool calls unless the user asks. Expand only when the user asks for more detail. If the user proposes a different implementation approach, take their suggestion when it is compatible with a reliable automation — don't argue.

Progress updates
- Send short progress updates at meaningful milestones or blockers, in plain language a non-technical user can understand.
- Explain decisions as outcomes: what input is needed, what output will be produced, what the automation will do at a high level, or what is blocking it.
- If automation is blocked, explain the reason simply and offer concrete options or suggested changes. Avoid implementation jargon.

Strong preference for `browser_harness` over `ui_agent` whenever the target is in a browser. browser-harness is very powerful: compositor-level clicks pass through iframes/shadow DOM, raw CDP for anything helpers miss, parallel HTTP (see harness/browser-harness/SKILL.md). Only fall back to `ui_agent` if CDP genuinely cannot survive replay.

Protocol — four phases, work autonomously unless an important user decision is required:

Phase A — Confirm and finalize inputs and outputs
1. Read `{request.schema_path}` and `{request.optimized_plan_path}`. Show the inputs from `optimized_plan.inputs[]` and expected outputs from `optimized_plan.steps[].outputs` as a compact plain-language summary. Also include one very high-level sentence describing how the automation will run.
2. Ask for confirmation only if the inputs or outputs are missing, ambiguous, likely wrong, or require the user's values before validation can proceed. If they are clear enough to test with recorded defaults, say so briefly and continue.
3. Treat user-proposed *additional* inputs as first-class — don't push back unless they conflict with the recorded behavior.
4. For any change (edit, add, remove, rename), update BOTH files atomically:
   - `{request.optimized_plan_path}` — `inputs[]`.
   - `{request.schema_path}` — matching `task_params[]` entry (and any `{{placeholder}}` in `plan.subtasks[].text` if relevant).
   Read each file back; confirm in one line before continuing.
5. Do NOT modify `task_name` unless the user explicitly asks — the skill directory slug derives from it.
6. Persist final confirmed values to `agent/confirmed_inputs.json`. These are what `scripts/run.py --inputs-json` will receive at validation time.

Phase B — Execute optimized_plan steps one at a time
For each `optimized_plan.steps[i]` in order. Work autonomously, verify internally, and send only brief plain-language milestone updates or blocker messages.
1. Look up matching `schema.plan.subtasks[].steps[]` via `source_subtask_ids` for fine-grained intent. `step.goal` may describe a smarter path than the recording (API / CLI / file / URL-scheme); honor it.
2. Match `step.executor` to the execution shape:
   - `script` → Python via Bash (subprocess / requests / pdfplumber / file IO). `ask_gemini` for stochastic JSON decisions.
   - `browser_harness` → Chrome via the `browser` skill / `browser-harness -c '…'`. `ask_gemini` for in-page judgment.
   - `ui_agent` → `macos-computer-use` skill (screenshot + click).
   If you swap `ui_agent` → `browser_harness` because browser-harness can handle the target, update `optimized_plan.json` step's `executor` to match. Mention the user-visible effect only if it matters, e.g. "I found a more reliable way to do this in the browser."
3. Execute against the live environment using `agent/confirmed_inputs.json`. Verify success (screenshots / page_info / shell output) before declaring the step done.
4. Append durable findings to `agent/learned_notes.md` — selectors, URLs, payload shapes, traps. Map, not diary.
5. Side effects: after any non-idempotent change, append a one-liner to `agent/side_effects.md` (what was created, how to undo). Before retrying a step or re-running from earlier, stop and ask the user to clear the prior side effect — cite the specific ledger entry in plain language; wait for explicit "cleared".
6. If the step can't be made to work: first do ONE WebSearch for the failure mode / selector / API — many "hostile DOM" problems have a documented workaround. Only after that, surface it briefly in non-technical language, propose options (change inputs, change expected output, use a less reliable fallback, or declare unbuildable), and ask the user only when the choice affects their task.
7. Do not pause after successful individual steps. Continue to the next step on your own.

After the last step verifies, announce in one line: "All <N> steps complete end-to-end."

Phase C — Synthesize and validate `scripts/run.py`
1. Synthesize `{skill_dir}/scripts/run.py` from `agent/learned_notes.md`. Per-step code shape matches its `executor`: `script` → inline Python; `browser_harness` → shell out to `browser-harness -c '…'` (or import helpers directly); `ui_agent` → drive `macos-computer-use`.
2. Contract:
   - Invocation: `python scripts/run.py --inputs-json /path/to/inputs.json`. Read all inputs up front, no prompts.
   - For irreducible judgment, call `ask_gemini` with an explicit JSON schema. Pattern: `from browser_harness.helpers import ask_gemini; pick = ask_gemini(prompt, schema={{"type":"object","properties":{{"id":{{"type":["string","null"]}},"reason":{{"type":"string"}}}},"required":["id","reason"]}})`. Branch deterministically on the returned dict. Document each call site in `SKILL.md`.
   - Emit progress logs continuously (step id + title). Exit non-zero with diagnostic logs on failure.
3. Clear any Phase-B side effects before testing. One-line ask to confirm `agent/side_effects.md` entries are cleared.
4. Run the assembled script end-to-end against `agent/confirmed_inputs.json`. Verify the same end state Phase B reached.
5. If it fails: diagnose, patch `scripts/run.py`, ask the user to clear new side effects, re-run. Loop until clean.
6. When the e2e script runs clean, do not ask for packaging approval. Send one short progress update such as "The full automation ran successfully. I'm turning it into a reusable skill now." Then continue to Phase D. The user may ask to change implementation here — take the suggestion if it is compatible with a reliable automation.

Phase D — Package as a standard skill

1. Invoke the `skill-creator` skill to scaffold `SKILL.md`. It MUST have YAML frontmatter (non-empty `name`, `description`) and these sections (titles exact): `## Inputs`, `## Run`, `## Outputs`, `## Progress log format`, `## Fallback`, `## ask_gemini decision points`, `## References`.

   `SKILL.md` `## Run` must document the Python runtime contract:
   - `run.sh` uses the first available interpreter in this order: skill `.venv/bin/python`, workflow `.venv/bin/python`, `$AI_MIME_PYTHON_PATH`, then `python3`.
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
       INPUTS="${{1:-$HERE/inputs/inputs.example.json}}"
       PYTHON="${{AI_MIME_PYTHON_PATH:-python3}}"
       if [[ -x "$HERE/.venv/bin/python" ]]; then
         PYTHON="$HERE/.venv/bin/python"
       elif [[ -x "$HERE/../../.venv/bin/python" ]]; then
         PYTHON="$HERE/../../.venv/bin/python"
       fi
       exec "$PYTHON" "$HERE/scripts/run.py" --inputs-json "$INPUTS"
       ```
   - `inputs/inputs.example.json`     — copy of `agent/confirmed_inputs.json`. Re-runnable as-is.
   - `inputs/inputs.template.json`    — same keys as example, but each value is `"<FILL IN: <one-line description>>"` (or the input's recorded default).
   - `references/fallback_plan.md`    — REQUIRED. Synthesized from `schema.plan.subtasks[]` + matching `optimized_plan.steps[]`. Per subtask: heading, one-line `Intent:`, the recorded sub-steps as bullets, `Notes:` with selectors / URLs / traps learned in Phase B. A human or `macos-computer-use` agent must be able to finish the task from this file alone if `run.sh` fails.

3. Free-form `references/`. Beyond `fallback_plan.md`, write whatever notes help a future runner — domain notes, per-subtask notes, selectors, payload shapes. You decide based on what was actually useful in Phase B. Don't force everything into one `learned_notes.md`.

4. Do NOT copy `schema.json` or `optimized_plan.json` into the skill. They're builder-only artifacts.

   Reproducibility: any external tool / MCP server / API you relied on during the build must also be reachable when `run.sh` runs on the end user's machine. If Python packages are needed, list them in `requirements.txt`, create `.venv`, install them with uv during skill build, and document that `run.sh` will use the existing `.venv`. If you used `uvx some-cli`, list the exact invocation in SKILL.md `## Run` and call it the same way in `scripts/run.py` (e.g. `subprocess.run(["uvx", "some-cli", ...])`). Do NOT assume the end user has anything pre-installed beyond `bash`, `curl`, `npx`, `uvx`, `$AI_MIME_PYTHON_PATH`, and an already-created `.venv` when needed.

5. `scripts/run.py` MUST emit one JSON-line per step transition on stderr so a downstream agent (or human) can read partial progress on failure and resume from the right subtask:
   - `{{"event":"step_start","id":"<step_id>","title":"…"}}`
   - `{{"event":"step_done","id":"<step_id>","outputs":{{…}},"summary":"…"}}`
   - `{{"event":"step_failed","id":"<step_id>","error":"…","recoverable":true|false}}`
   - `{{"event":"workflow_done","outputs":{{…}}}}`
   Free-form human logs may be interleaved. Exit non-zero on `step_failed`. Document this contract in `SKILL.md` under `## Progress log format`.

6. Verify `./run.sh` runs clean against `inputs/inputs.example.json` (this is the published one-click command, so this is what gets tested — not just `python scripts/run.py`).

7. Write `{signal_path}` with `{{"status":"skill_ready","summary":"<one line>"}}` and stop. The service runs `validate_skill_package` + `run_skill_e2e_test` and surfaces the result in the UI.

8. Unbuildable escape (only reachable from Phase B or C after trying safe fallbacks and asking any necessary task-level questions): write `{signal_path}` with `{{"status":"skill_unbuildable","reason":"<plain-language concrete reason>","suggested_changes":["<plain-language suggested change>","..."]}}` and stop.

Existing skill files at {skill_dir}:
{json.dumps(existing_skill_files, indent=2)}

Readable roots:
{json.dumps([str(p) for p in request.readable_roots], indent=2)}

Writable roots:
{json.dumps([str(p) for p in request.writable_roots], indent=2)}


All the access that you need is present in the above directories. DO NOT try to read/write into files outside the above directories.
Do NOT bypass these limits using bash.

Existing memory:
{memory}
"""

    if request.mode == "replay_execution":
        skill_dir = _skill_dir_for_request(request)
        replay_notes_path = request.workflow_dir / "agent" / "replay_notes.md"
        domain_notes_path = request.workflow_dir / "agent" / "domain_notes.md"
        existing_skill_files = sorted(
            str(path.relative_to(skill_dir))
            for path in skill_dir.rglob("*")
            if skill_dir.exists() and path.is_file()
        )
        return f"""You are the AI Mime replay execution agent for this workflow.
You are running in the Replay page chat. Your job is to help run an existing skill, validate inputs, and handle variants of the task using the skill context.

Workflow directory: {request.workflow_dir}
Skill directory: {skill_dir}
Memory file: {memory_path}
Replay notes file: {replay_notes_path}
Domain notes file: {domain_notes_path}

Core behavior:
- Read and learn from the complete skill package before deciding how to recover or run: `SKILL.md`, `run.sh`, `scripts/run.py`, `inputs/inputs.example.json`, `inputs/inputs.template.json`, every file under `references/`, and especially `references/fallback_plan.md`.
- Validate and normalize the user's inputs before running anything. If an input is ambiguous or unsafe to infer, ask a short clarifying question.
- Prefer `./run.sh <inputs.json>` as the primary execution path. It is cheap, runs the task end-to-end, and emits rich stdout/stderr progress logs.
- Use stdout, stderr, and JSON progress events (`step_start`, `step_done`, `step_failed`, `workflow_done`) to explain progress, results, and failures.
- For task variants, use the script and skill context to automate the new task directly. You may create temporary input JSON files or run helper commands, but keep durable outputs under allowed output paths.
- If `./run.sh` fails or cannot cover the remaining task, triage before editing: classify the failure as likely environment/user-state issue, input issue, transient UI issue, or skill defect. Closed tabs, missing windows, changed focus, logged-out browser state, interrupted app state, and one-off UI disruption are recovery work, not skill repair.
- Decide from the logs, script, skill docs, and `references/fallback_plan.md` how to complete the task. You may continue manually, restore expected UI state, rerun only the remaining work, or complete the task directly from the fallback plan.
- Use the `macos-computer-use` skill as the UI-agent fallback for unknown UI-only parts. Prefer script/browser approaches when they are clear, but do not stop just because the original script failed.
- You may append durable domain findings to `{replay_notes_path}` or `{domain_notes_path}`. Keep these notes factual: selectors, URLs, payload shapes, input gotchas, and observed domain behavior.

Hard boundaries:
- Targeted edits inside `{skill_dir}` are allowed only when there is clear evidence from `run.sh`, logs, `scripts/run.py`, or repeated deterministic failure that the skill package itself is stale, incomplete, or wrong. Only edit the skill when needed; do not rewrite `run.sh` or `scripts/run.py` just because the first run failed.
- Do NOT edit `{request.schema_path}` or `{request.optimized_plan_path}`.
- If completion is impossible with the available logs, skill, fallback plan, and UI-agent fallback, explain the concrete blocker and what user action is needed.

Readable roots:
{json.dumps([str(p) for p in request.readable_roots], indent=2)}

Writable roots:
{json.dumps([str(p) for p in request.writable_roots], indent=2)}

All the access that you need is present in the above directories. DO NOT try to read/write into files outside the above directories.

Existing skill files:
{json.dumps(existing_skill_files, indent=2)}

Existing memory:
{memory}
"""

    return f"""You are the task agent for this AI Mime workflow.

Mode: {request.mode}
Workflow directory: {request.workflow_dir}
Schema: {request.schema_path}
Optimized plan: {request.optimized_plan_path}
Memory file: {memory_path}

Read only the schema, optimized plan, current memory, and existing skill files if present.
Use the provided readable/writable roots as the permission boundary.
Write the latest machine-readable result to outputs/result.json and a human-readable summary to outputs/README.md.
Do not create per-run result directories unless debug artifacts are explicitly requested.

Readable roots:
{json.dumps([str(p) for p in request.readable_roots], indent=2)}

Writable roots:
{json.dumps([str(p) for p in request.writable_roots], indent=2)}

Existing memory:
{memory}
"""


def run_agent_task(request: AgentRunRequest, adapter: AgentAdapter, prompt: str | None = None) -> AgentRunResult:
    session_id = _load_or_create_session_id(request)
    request = request.model_copy(update={"session_id": session_id or None})

    agent_dir = request.workflow_dir / (".agent" if request.mode == "general" else "agent")
    outputs_dir = request.workflow_dir / "outputs"
    assets_dir = outputs_dir / "assets"
    for path in [agent_dir, outputs_dir, assets_dir, *(request.writable_roots or [])]:
        # writable_roots may include specific files (e.g. schema.json) — skip
        # those; only ensure directories exist.
        if path.exists() and not path.is_dir():
            continue
        if path.suffix:
            continue
        path.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="ai-mime-agent-") as td:
        request = request.model_copy(update={"temp_dir": Path(td)})
        runtime_env = workflow_runtime_env(request.workflow_dir)
        old_env = {key: os.environ.get(key) for key in runtime_env}
        os.environ.update(runtime_env)
        try:
            result = adapter.run(request, prompt or _build_prompt(request))
        finally:
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    final_session_id = result.session_id or session_id
    result = result.model_copy(update={"session_id": final_session_id})
    _write_json(
        agent_dir / "session.json",
        {
            "provider": request.provider,
            "session_id": final_session_id,
            "last_status": result.status,
            "last_error": result.error,
            "mode": request.mode,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )
    _append_text(
        agent_dir / "memory.md",
        f"\n## {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n"
        f"- status: {result.status}\n"
        f"- summary: {result.summary.strip()}\n",
    )
    _write_json(outputs_dir / "result.json", result.model_dump(mode="json"))
    readme = f"# Agent Task Result\n\nStatus: {result.status}\n\n{result.summary.strip()}\n"
    if result.error:
        readme += f"\nError: {result.error}\n"
    (outputs_dir / "README.md").write_text(readme, encoding="utf-8")
    return result.model_copy(update={"outputs_path": outputs_dir / "result.json"})


_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _parse_skill_frontmatter(skill_md_path: Path) -> dict[str, str]:
    text = skill_md_path.read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise ValueError("SKILL.md is missing the YAML frontmatter (--- ... --- block at top).")
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        out[key.strip()] = val.strip().strip('"').strip("'")
    return out


def _required_input_keys(optimized_plan: dict) -> set[str]:
    keys: set[str] = set()
    raw = optimized_plan.get("inputs")
    if not isinstance(raw, list):
        return keys
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if isinstance(name, str) and name.strip() and item.get("required") is True:
            keys.add(name)
    return keys


def validate_skill_package(skill_dir: str | Path, schema: dict, optimized_plan: dict) -> None:
    skill_dir_p = Path(skill_dir)
    if not skill_dir_p.exists() or not skill_dir_p.is_dir():
        raise FileNotFoundError(f"Skill directory not found: {skill_dir_p}")

    for rel in REQUIRED_SKILL_FILES:
        path = skill_dir_p / rel
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"Skill package missing required file: {rel}")

    fm = _parse_skill_frontmatter(skill_dir_p / "SKILL.md")
    for required_key in ("name", "description"):
        if not fm.get(required_key):
            raise ValueError(f"SKILL.md frontmatter is missing required key: {required_key!r}")

    run_sh = skill_dir_p / "run.sh"
    if not os.access(run_sh, os.X_OK):
        raise ValueError("run.sh is not executable — `chmod +x run.sh` in the build agent before signalling.")

    example_path = skill_dir_p / "inputs" / "inputs.example.json"
    template_path = skill_dir_p / "inputs" / "inputs.template.json"
    try:
        example = _read_json(example_path)
    except Exception as e:
        raise ValueError(f"inputs/inputs.example.json must be a JSON object: {e}") from e
    try:
        template = _read_json(template_path)
    except Exception as e:
        raise ValueError(f"inputs/inputs.template.json must be a JSON object: {e}") from e

    required_keys = _required_input_keys(optimized_plan)
    missing_example = required_keys - set(example.keys())
    if missing_example:
        raise ValueError(
            "inputs/inputs.example.json missing required keys from optimized_plan.inputs[]: "
            + ", ".join(sorted(missing_example))
        )
    missing_template = required_keys - set(template.keys())
    if missing_template:
        raise ValueError(
            "inputs/inputs.template.json missing required keys from optimized_plan.inputs[]: "
            + ", ".join(sorted(missing_template))
        )

    for rel in ("scripts/run.py",):
        script = skill_dir_p / rel
        proc = subprocess.run(
            [sys.executable, "-m", "py_compile", str(script)],
            cwd=str(skill_dir_p),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"{rel} failed py_compile:\n{proc.stdout}")


def _default_inputs_from_plan(optimized_plan: dict) -> dict[str, object]:
    inputs: dict[str, object] = {}
    missing_required: list[str] = []
    raw_inputs = optimized_plan.get("inputs")
    if not isinstance(raw_inputs, list):
        return inputs
    for item in raw_inputs:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        if "default" in item:
            inputs[name] = item.get("default")
        elif item.get("required") is True:
            missing_required.append(name)
    if missing_required:
        raise ValueError(
            "Cannot run skill e2e test because required optimized_plan inputs have no default: "
            + ", ".join(sorted(missing_required))
        )
    return inputs


def _resolve_e2e_inputs(
    skill_dir_p: Path,
    optimized_plan: dict,
    confirmed_inputs_path: str | Path | None,
) -> tuple[Path | None, dict[str, object] | None, AgentRunResult | None]:
    """Returns (inputs_path_to_use, inputs_dict_or_none, early_failure_or_none).

    Priority:
      1. confirmed_inputs_path if supplied + exists — use its path directly.
      2. {skill_dir}/inputs/inputs.example.json — use its path directly.
      3. Synthesize from optimized_plan defaults — caller must write to temp.
    """
    if confirmed_inputs_path is not None:
        cp = Path(confirmed_inputs_path)
        if cp.exists():
            return cp, None, None

    example = skill_dir_p / "inputs" / "inputs.example.json"
    if example.exists():
        return example, None, None

    try:
        synthesized = _default_inputs_from_plan(optimized_plan)
    except Exception as e:
        return None, None, AgentRunResult(
            status="failed",
            session_id="",
            summary="Skill e2e input validation failed.",
            error=str(e),
        )
    return None, synthesized, None


def run_skill_e2e_test(
    skill_dir: str | Path,
    optimized_plan: dict,
    confirmed_inputs_path: str | Path | None = None,
) -> AgentRunResult:
    skill_dir_p = Path(skill_dir)
    run_sh = skill_dir_p / "run.sh"
    run_script = skill_dir_p / "scripts" / "run.py"
    runtime_root = skill_dir_p.parent.parent if skill_dir_p.parent.name == "skills" else skill_dir_p

    inputs_path, synthesized_inputs, early = _resolve_e2e_inputs(
        skill_dir_p, optimized_plan, confirmed_inputs_path
    )
    if early is not None:
        return early

    def _invoke(inputs_json_path: Path) -> AgentRunResult:
        if run_sh.exists() and os.access(run_sh, os.X_OK):
            cmd = [str(run_sh), str(inputs_json_path)]
        else:
            cmd = [sys.executable, str(run_script), "--inputs-json", str(inputs_json_path)]
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(skill_dir_p),
                env={**os.environ, **workflow_runtime_env(runtime_root)},
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=SKILL_E2E_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as e:
            output = e.stdout if isinstance(e.stdout, str) else ""
            return AgentRunResult(
                status="failed",
                session_id="",
                summary=f"Skill e2e test timed out after {SKILL_E2E_TIMEOUT_SECONDS} seconds.\n\n{output}",
                error="timeout",
            )
        except Exception as e:
            return AgentRunResult(
                status="failed", session_id="", summary="Skill e2e test failed to start.", error=str(e)
            )

        summary = proc.stdout.strip() or "Skill e2e test completed without output."
        status = "success" if proc.returncode == 0 else "failed"
        error = None if proc.returncode == 0 else f"run.sh exited with code {proc.returncode}"
        return AgentRunResult(status=status, session_id="", summary=summary, error=error)

    if inputs_path is not None:
        return _invoke(inputs_path)

    assert synthesized_inputs is not None
    with tempfile.TemporaryDirectory(prefix="ai-mime-skill-inputs-") as td:
        tmp_inputs = Path(td) / "inputs.json"
        tmp_inputs.write_text(
            json.dumps(synthesized_inputs, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return _invoke(tmp_inputs)
