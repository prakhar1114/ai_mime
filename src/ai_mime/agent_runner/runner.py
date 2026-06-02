from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Protocol

from ai_mime.agent_runner.mcp import cua_mcp_servers
from ai_mime.agent_runner.models import (
    AgentProvider,
    AgentRunMode,
    AgentRunRequest,
    AgentRunResult,
    FilesystemAccess,
    FilesystemAccessEntry,
    resolved_browser_skill_name,
    resolved_browser_skill_path,
)
from ai_mime.app_data import (
    get_managed_browser_harness_path,
    get_python_path,
    get_uv_path,
    get_workflows_dir,
    workflow_runtime_env,
)
from ai_mime.debug_log import log as debug_log


logger = logging.getLogger(__name__)


def _log(message: str, *, exc_info: bool = False) -> None:
    logger.info(message)
    debug_log(f"[agent-runner] {message}", exc_info=exc_info)


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
INSTRUCTIONS_ROOT = Path(__file__).parent / "instructions"



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


def _resolved_skill_context() -> str:
    return (
        f"Browser skill: `{resolved_browser_skill_name()}` at {resolved_browser_skill_path()}\n"
        "Computer-use: the cua MCP server is attached to this session — call the `mcp__cua__*` "
        "tools directly (`computer_screenshot`, `computer_find_element`, `computer_click`, "
        "`computer_type`, `computer_hotkey`, `computer_launch_app`, …) to drive native macOS "
        "apps while you work out the exact steps. To reproduce a native-UI subtask from a "
        "standalone script, hand those steps to `run_computer_use_task` via "
        "`$AI_MIME_UI_AGENT_CMD` — it runs the SAME cua MCP server through its own agent. "
        "Native-UI fallback; slowest."
    )


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
    mode: AgentRunMode,
    provider: AgentProvider = "claude",
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
            INSTRUCTIONS_ROOT,
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

    # Attach the cua computer-use MCP server so the build/replay chat agents can
    # call mcp__cua__computer_* directly for native-UI control (the same tools the
    # standalone UI agent uses via $AI_MIME_UI_AGENT_CMD).
    mcp_servers: dict | None = None
    if mode == "build_skill_chat":
        mcp_servers = {**_default_skill_builder_mcp_servers(), **cua_mcp_servers()}
    elif mode == "replay_execution":
        mcp_servers = dict(cua_mcp_servers())

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
    skill_context = _resolved_skill_context()
    if request.mode == "general":
        return f"""You are the AI Mime workspace debugging agent.

Mode: {request.mode}
Workspace directory: {request.workspace_dir}
Memory file: {memory_path}

You can inspect workflows, schemas, optimized plans, agent artifacts, and task outputs under the workspace.
Use the provided readable/writable roots as the permission boundary.
Keep file writes deliberate. Summarize durable findings in .agent/memory.md only when useful.

Resolved Claude skills:
{skill_context}

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
        browser_harness_bin = get_managed_browser_harness_path()
        return f"""You are the AI Mime iterative skill-builder agent for this workflow.
You are running inside a chat panel — the human user is on the other end of every message and is collaborating with you to produce a deterministic, reusable skill package for this workflow.

Workflow directory: {request.workflow_dir}
Schema: {request.schema_path}
Optimized plan: {request.optimized_plan_path}
Memory file: {memory_path}
Learned-notes file (append durable findings here): {learned_path}
Skill directory to create or refine: {skill_dir} (located under <workflow_dir>/skills/<skill_name>/ under your workspace; you must store the packaged skill and its run.sh directly inside this directory)
Terminal signal file: {signal_path}

To prevent task dilution and ensure consistent behavior, your instructions are broken down into sequential task files located in the instructions folder:
{INSTRUCTIONS_ROOT / "build_skill"}

Shared UI-agent guide, read only when a step uses `ui_agent`:
{INSTRUCTIONS_ROOT / "ui_agent" / "00_ui_agent.md"}

An example of a correctly structured and packaged skill is available for your reference at:
{INSTRUCTIONS_ROOT / "example_skill"}

You MUST execute these tasks step-by-step:
1. First, read and follow `00_rules.md` in the instructions directory to understand execution guidelines, Python path requirements, and tools.
2. Next, read and execute the instructions in `01_phase_a_confirm_inputs.md`.
3. Follow the instructions and transition gates at the end of each task file sequentially to move to the next file (e.g. `02_phase_b_execute_steps.md`, `03_phase_c_synthesis.md`, `04_phase_d_packaging.md`).

CRITICAL: Do NOT read all instruction files at once. Focus only on the active task file, complete its requirements, and only read the next file once the current file's success criteria are fully met.

Current environment and tools state:
- Default Python: `{default_python_path}`
- uv: `{uv_path}`
- browser-harness: `{browser_harness_bin}`
- Browser skill is `{resolved_browser_skill_name()}` at `{resolved_browser_skill_path()}`

Existing skill files:
{json.dumps(existing_skill_files, indent=2)}

Readable roots:
{json.dumps([str(p) for p in request.readable_roots], indent=2)}

Writable roots:
{json.dumps([str(p) for p in request.writable_roots], indent=2)}

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
Skill directory: {skill_dir} (located at <workflow_dir>/skills/<skill_name>/ under your workspace)
Memory file: {memory_path}
Replay notes file: {replay_notes_path}
Domain notes file: {domain_notes_path}

Your detailed instructions are located in the instructions folder:
{INSTRUCTIONS_ROOT / "replay"}

Shared UI-agent guide, read only for UI-only recovery:
{INSTRUCTIONS_ROOT / "ui_agent" / "00_ui_agent.md"}

You MUST execute the task following these instructions step-by-step:
1. First, read and follow `00_rules.md` in the instructions directory.
2. Next, read and execute `01_replay.md` to run/verify the skill.

CRITICAL: Do NOT read all instruction files at once. Focus only on the active task file, complete its requirements, and only read the next file once the current file's success criteria are fully met.

Existing skill files:
{json.dumps(existing_skill_files, indent=2)}

Readable roots:
{json.dumps([str(p) for p in request.readable_roots], indent=2)}

Writable roots:
{json.dumps([str(p) for p in request.writable_roots], indent=2)}

Existing memory:
{memory}
"""

    raise ValueError(f"Unsupported agent mode: {request.mode}")


def run_agent_task(request: AgentRunRequest, adapter: AgentAdapter, prompt: str | None = None) -> AgentRunResult:
    session_id = _load_or_create_session_id(request)
    request = request.model_copy(update={"session_id": session_id or None})
    _log(
        f"run_agent_task start provider={request.provider} mode={request.mode} workspace={request.workspace_dir} "
        f"workflow={request.workflow_dir} session_id={request.session_id or '<new>'} adapter={getattr(adapter, 'id', type(adapter).__name__)}"
    )

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
        # The app-managed runtime env is injected into the SDK run via
        # ClaudeAgentOptions.env (see _options_kwargs_for), so no global
        # os.environ mutation is needed here.
        run_prompt = prompt or _build_prompt(request)
        _log(f"run_agent_task invoking adapter prompt_chars={len(run_prompt)} temp_dir={td}")
        try:
            result = adapter.run(request, run_prompt)
        except Exception as e:
            _log(f"run_agent_task adapter raised: {e}", exc_info=True)
            raise

    final_session_id = result.session_id or session_id
    result = result.model_copy(update={"session_id": final_session_id})
    _log(
        f"run_agent_task complete status={result.status} session_id={final_session_id or '<none>'} "
        f"summary_chars={len(result.summary or '')} error={result.error or ''}"
    )
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
