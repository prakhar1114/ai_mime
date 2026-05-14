from __future__ import annotations

import json
import tempfile
import time
import uuid
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
from ai_mime.app_data import get_workflows_dir


class AgentAdapter(Protocol):
    def run(self, request: AgentRunRequest, prompt: str) -> AgentRunResult:
        ...


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
    writable_roots = _unique_paths(
        [
            agent_dir,
            outputs_dir,
            outputs_dir / "assets",
            skills_dir,
        ]
    )

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
    return f"{request.workflow_dir.name}-{uuid.uuid4().hex[:12]}"


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


def run_agent_task(request: AgentRunRequest, adapter: AgentAdapter) -> AgentRunResult:
    session_id = _load_or_create_session_id(request)
    request = request.model_copy(update={"session_id": session_id})

    agent_dir = request.workflow_dir / (".agent" if request.mode == "general" else "agent")
    outputs_dir = request.workflow_dir / "outputs"
    assets_dir = outputs_dir / "assets"
    for path in [agent_dir, outputs_dir, assets_dir, *(request.writable_roots or [])]:
        path.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="ai-mime-agent-") as td:
        request = request.model_copy(update={"temp_dir": Path(td)})
        prompt = _build_prompt(request)
        result = adapter.run(request, prompt)

    result = result.model_copy(update={"session_id": session_id})
    _write_json(
        agent_dir / "session.json",
        {
            "provider": request.provider,
            "session_id": session_id,
            "last_status": result.status,
            "last_error": result.error,
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
