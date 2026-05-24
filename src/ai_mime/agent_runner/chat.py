from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from ai_mime.agent_runner.adapters.claude_sdk import (
    ClaudeAgentSdkAdapter,
    list_claude_sessions,
    load_claude_session_messages,
    stream_chat,
    to_permission_result,
)
from ai_mime.agent_runner.models import AgentRunRequest, AgentRunResult
from ai_mime.agent_runner.runner import build_agent_run_request, _build_prompt, _read_json, _write_json
from ai_mime.app_data import get_workflows_dir


class AgentBusyError(RuntimeError):
    pass


DEFAULT_CLAUDE_MODEL_OPTIONS: list[dict[str, str]] = [
    {
        "id": "opus",
        "label": "Default",
        "description": "Claude Code chooses the recommended model for the account.",
    },
    {
        "id": "sonnet",
        "label": "Sonnet",
        "description": "Balanced default for coding and agentic work.",
    },
    {
        "id": "opus",
        "label": "Opus",
        "description": "Best for deeper reasoning and architecture.",
    },
    {
        "id": "haiku",
        "label": "Haiku",
        "description": "Fastest option for lighter debugging.",
    },
    {
        "id": "sonnet[1m]",
        "label": "Sonnet 1M",
        "description": "Long-context Sonnet alias when available for the account.",
    },
    {
        "id": "opusplan",
        "label": "Opus Plan",
        "description": "Uses Opus for planning and Sonnet for execution.",
    },
]


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class WorkspaceAgentChatService:
    def __init__(
        self,
        *,
        workspace_dir: Path | None = None,
        mode: str = "general",
        agent_dir: Path | None = None,
        adapter: Any | None = None,
        session_lister: Callable[[Path], list[dict[str, Any]]] | None = None,
        message_loader: Callable[[str, Path], list[dict[str, Any]]] | None = None,
        bash_requires_approval: bool | None = None,
    ) -> None:
        self.workspace_dir = workspace_dir or get_workflows_dir()
        self.mode = mode
        self.agent_dir = agent_dir or (self.workspace_dir / ".agent")
        self.adapter = adapter or ClaudeAgentSdkAdapter()
        self.session_lister = session_lister or list_claude_sessions
        self.message_loader = message_loader or load_claude_session_messages
        if bash_requires_approval is None:
            env_val = (os.getenv("AI_MIME_BASH_REQUIRES_APPROVAL") or "").strip().lower()
            bash_requires_approval = env_val in ("1", "true", "yes", "on")
        self.bash_requires_approval = bash_requires_approval
        self._turn_lock = threading.Lock()
        self._active_client: Any | None = None
        self._active_loop: asyncio.AbstractEventLoop | None = None
        self._pending_permissions: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._session_bash_allow_all: bool = False
        self.model_options = self._load_model_options()
        self.default_model = self.model_options[0]["id"] if self.model_options else "default"

    def status(self) -> dict[str, Any]:
        active = self._read_active()
        return {
            "workspace_dir": str(self.workspace_dir),
            "active_session_id": active.get("session_id"),
            "sessions": self.list_sessions(),
            "models": self.model_options,
            "default_model": self.default_model,
            "bash_requires_approval": self.bash_requires_approval,
        }

    def set_bash_requires_approval(self, value: bool) -> bool:
        self.bash_requires_approval = bool(value)
        if self.bash_requires_approval:
            self._session_bash_allow_all = False
        return self.bash_requires_approval

    def list_models(self) -> dict[str, Any]:
        return {"models": self.model_options, "default_model": self.default_model}

    def list_sessions(self) -> list[dict[str, Any]]:
        index = self._read_index()
        out_by_id: dict[str, dict[str, Any]] = {}
        for sid, meta in index.items():
            if not isinstance(sid, str) or not isinstance(meta, dict):
                continue
            out_by_id[sid] = {
                "session_id": sid,
                "summary": meta.get("summary") or sid,
                "created_at": meta.get("created_at"),
                "updated_at": meta.get("updated_at"),
                "mode": meta.get("mode") or "general",
                "model": meta.get("model"),
                "source": "ai_mime",
            }
        try:
            for item in self.session_lister(self.workspace_dir):
                sid = item.get("session_id") if isinstance(item, dict) else None
                if not isinstance(sid, str) or not sid:
                    continue
                current = out_by_id.get(sid, {})
                out_by_id[sid] = {
                    **current,
                    "session_id": sid,
                    "summary": current.get("summary") or item.get("summary") or sid,
                    "last_modified": item.get("last_modified"),
                    "custom_title": item.get("custom_title"),
                    "first_prompt": item.get("first_prompt"),
                    "source": "claude",
                }
        except Exception as e:
            return [{"session_id": "", "summary": f"Failed to list Claude sessions: {e}", "error": str(e)}]
        return sorted(
            out_by_id.values(),
            key=lambda x: str(x.get("updated_at") or x.get("last_modified") or ""),
            reverse=True,
        )

    def create_session(self) -> dict[str, Any]:
        return {"session_id": None, "summary": "New chat"}

    def load_messages(self, session_id: str) -> list[dict[str, Any]]:
        if not session_id or session_id.startswith("draft-"):
            return []
        return self.message_loader(session_id, self.workspace_dir)

    def chat(self, *, message: str, session_id: str | None = None, model: str | None = None) -> dict[str, Any]:
        text = message.strip()
        if not text:
            raise ValueError("message must be non-empty")
        selected_model = self._validate_model(model)
        if not self._turn_lock.acquire(blocking=False):
            raise AgentBusyError("Workspace agent is already responding")
        try:
            resume_id = session_id if session_id and not session_id.startswith("draft-") else None
            request = self._build_request(session_id=resume_id, model=selected_model)
            if resume_id is None:
                system_prompt = _build_prompt(request)
                request = request.model_copy(update={"system_prompt": system_prompt})
            prompt = text
            with tempfile.TemporaryDirectory(prefix="ai-mime-agent-") as td:
                request = request.model_copy(update={"temp_dir": Path(td)})
                result: AgentRunResult = self.adapter.run(request, prompt)
            sid = result.session_id or resume_id
            if not sid:
                raise RuntimeError("Claude did not return a session_id")
            self._record_session(
                session_id=sid,
                previous_session_id=session_id,
                summary=(text[:80] or sid),
                status=result.status,
                error=result.error,
                model=selected_model,
            )
            return {
                "session_id": sid,
                "assistant_text": result.summary,
                "status": result.status,
                "error": result.error,
                "model": selected_model,
            }
        finally:
            self._turn_lock.release()

    async def chat_stream(
        self,
        *,
        message: str,
        session_id: str | None = None,
        model: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        text = message.strip()
        if not text:
            raise ValueError("message must be non-empty")
        selected_model = self._validate_model(model)
        if not self._turn_lock.acquire(blocking=False):
            raise AgentBusyError("Workspace agent is already responding")

        resume_id = session_id if session_id and not session_id.startswith("draft-") else None
        request = self._build_request(session_id=resume_id, model=selected_model)
        if resume_id is None:
            request = request.model_copy(update={"system_prompt": _build_prompt(request)})

        event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        async def can_use_tool(tool_name: str, input_data: dict[str, Any], _ctx: Any) -> Any:
            decision = self._authorize_tool(request, tool_name, input_data)
            if decision.get("behavior") == "ask":
                tool_use_id = decision.get("tool_use_id") or f"perm-{uuid.uuid4().hex[:12]}"
                loop = asyncio.get_running_loop()
                future: asyncio.Future[dict[str, Any]] = loop.create_future()
                self._pending_permissions[tool_use_id] = future
                await event_queue.put({
                    "event": "permission_request",
                    "request_id": tool_use_id,
                    "tool_name": tool_name,
                    "input": input_data,
                    "reason": decision.get("reason") or "Tool requires approval.",
                })
                try:
                    resolved = await future
                finally:
                    self._pending_permissions.pop(tool_use_id, None)
                if resolved.get("behavior") == "allow_always" and tool_name == "Bash":
                    self._session_bash_allow_all = True
                    return to_permission_result({"behavior": "allow", "updated_input": input_data})
                return to_permission_result(resolved)
            return to_permission_result(decision)

        def _store_client(c: Any) -> None:
            self._active_client = c
            self._active_loop = asyncio.get_running_loop()

        final_session_id = resume_id or ""
        final_status = "success"
        final_error: str | None = None
        final_summary = ""
        stream_done = asyncio.Event()

        # Tools that should pass through can_use_tool: gated ones.
        # Bash is gated when approval is required; otherwise auto-allowed.
        auto_allow = ["Glob", "Grep", "Read", "Write", "Edit", "MultiEdit", "Skill"]
        if not self.bash_requires_approval:
            auto_allow.append("Bash")

        async def _wrapped_pump_stream() -> None:
            try:
                with tempfile.TemporaryDirectory(prefix="ai-mime-agent-") as td:
                    local_request = request.model_copy(update={"temp_dir": Path(td)})
                    async for event in stream_chat(
                        local_request,
                        text,
                        can_use_tool=can_use_tool,
                        auto_allow_tools=auto_allow,
                        on_client=_store_client,
                    ):
                        await event_queue.put(event)
            except Exception as e:
                await event_queue.put({"event": "error", "message": str(e)})
            finally:
                stream_done.set()
                await event_queue.put({"__sentinel__": True})

        pump_task = asyncio.create_task(_wrapped_pump_stream())
        try:
            while True:
                event = await event_queue.get()
                if event.get("__sentinel__"):
                    break
                if event.get("event") == "done":
                    final_session_id = str(event.get("session_id") or final_session_id)
                    final_status = str(event.get("status") or "success")
                    final_error = event.get("error")
                    final_summary = str(event.get("summary") or "")
                yield event
        finally:
            for fut in list(self._pending_permissions.values()):
                if not fut.done():
                    fut.set_result({
                        "behavior": "deny",
                        "message": "Permission request cancelled.",
                        "interrupt": False,
                    })
            self._pending_permissions.clear()
            try:
                await pump_task
            except Exception:
                pass
            self._active_client = None
            self._active_loop = None
            self._turn_lock.release()

        if final_session_id:
            self._record_session(
                session_id=final_session_id,
                previous_session_id=session_id,
                summary=(text[:80] or final_session_id),
                status=final_status,
                error=final_error,
                model=selected_model,
            )

    def interrupt(self) -> bool:
        client = self._active_client
        loop = self._active_loop
        if client is None or loop is None:
            return False
        try:
            fut = asyncio.run_coroutine_threadsafe(client.interrupt(), loop)
            fut.result(timeout=5)
            return True
        except Exception:
            return False

    def _authorize_tool(
        self, request: AgentRunRequest, tool_name: str, input_data: dict[str, Any]
    ) -> dict[str, Any]:
        read_only = {"Read", "Glob", "Grep"}
        write_tools = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
        if tool_name in read_only:
            paths = self._extract_paths(input_data)
            denied = [p for p in paths if not self._within_roots(p, request.readable_roots)]
            if denied:
                return {
                    "behavior": "deny",
                    "message": f"{tool_name} blocked: path(s) outside readable scope: {denied}",
                    "interrupt": False,
                }
            return {"behavior": "allow", "updated_input": input_data, "updated_permissions": None}
        if tool_name in write_tools:
            paths = self._extract_paths(input_data)
            denied = [p for p in paths if not self._within_roots(p, request.writable_roots)]
            if denied:
                return {
                    "behavior": "deny",
                    "message": f"{tool_name} blocked: path(s) outside writable scope: {denied}",
                    "interrupt": False,
                }
            return {"behavior": "allow", "updated_input": input_data, "updated_permissions": None}
        if tool_name == "Skill":
            return {"behavior": "allow", "updated_input": input_data, "updated_permissions": None}
        if tool_name.startswith("mcp__"):
            servers = request.mcp_servers or {}
            parts = tool_name.split("__", 2)
            server_name = parts[1] if len(parts) >= 2 else ""
            if server_name and server_name in servers:
                return {"behavior": "allow", "updated_input": input_data, "updated_permissions": None}
            return {
                "behavior": "deny",
                "message": f"{tool_name} blocked: MCP server {server_name!r} is not registered for this workflow.",
                "interrupt": False,
            }
        if tool_name == "Bash":
            if not self.bash_requires_approval or self._session_bash_allow_all:
                return {"behavior": "allow", "updated_input": input_data, "updated_permissions": None}
            return {
                "behavior": "ask",
                "reason": "Bash command requires your approval.",
            }
        return {
            "behavior": "deny",
            "message": f"{tool_name} is not enabled for the workspace agent.",
            "interrupt": False,
        }

    def resolve_permission(self, request_id: str, decision: str) -> bool:
        future = self._pending_permissions.get(request_id)
        loop = self._active_loop
        if future is None or loop is None or future.done():
            return False
        if decision == "allow":
            resolved: dict[str, Any] = {"behavior": "allow", "updated_input": None, "updated_permissions": None}
        elif decision == "allow_always":
            resolved = {"behavior": "allow_always"}
        else:
            resolved = {
                "behavior": "deny",
                "message": "User denied this tool call.",
                "interrupt": False,
            }
        loop.call_soon_threadsafe(self._set_future_result, future, resolved)
        return True

    @staticmethod
    def _set_future_result(future: asyncio.Future, result: dict[str, Any]) -> None:
        if not future.done():
            future.set_result(result)

    @staticmethod
    def _extract_paths(input_data: dict[str, Any]) -> list[str]:
        candidates: list[str] = []
        for key in ("file_path", "path", "notebook_path"):
            value = input_data.get(key)
            if isinstance(value, str) and value:
                candidates.append(value)
        return candidates

    @staticmethod
    def _within_roots(target: str, roots: list[Path]) -> bool:
        try:
            target_path = Path(target).expanduser().resolve()
        except Exception:
            return False
        for root in roots:
            try:
                root_resolved = Path(root).expanduser().resolve()
            except Exception:
                continue
            try:
                target_path.relative_to(root_resolved)
                return True
            except ValueError:
                continue
        return False

    def _build_request(self, *, session_id: str | None, model: str | None) -> AgentRunRequest:
        if self.mode != "general":
            return build_agent_run_request(
                workflow_dir=self.workspace_dir,
                provider="claude",
                mode=self.mode,  # type: ignore[arg-type]
                model=model,
                session_id=session_id,
            )
        agent_dir = self.agent_dir
        return AgentRunRequest(
            provider="claude",
            mode="general",
            model=model,
            session_id=session_id,
            workflow_dir=self.workspace_dir,
            workspace_dir=self.workspace_dir,
            schema_path=None,
            optimized_plan_path=None,
            readable_roots=[self.workspace_dir, agent_dir],
            writable_roots=[agent_dir],
        )

    def _record_session(
        self,
        *,
        session_id: str,
        previous_session_id: str | None,
        summary: str,
        status: str,
        error: str | None,
        model: str | None,
    ) -> None:
        now = _utc_now()
        index = self._read_index()
        if previous_session_id and previous_session_id.startswith("draft-") and previous_session_id in index:
            index.pop(previous_session_id, None)
        existing = index.get(session_id) if isinstance(index.get(session_id), dict) else {}
        index[session_id] = {
            "summary": existing.get("summary") or summary,
            "created_at": existing.get("created_at") or now,
            "updated_at": now,
            "mode": self.mode,
            "model": model,
            "last_status": status,
            "last_error": error,
        }
        self._write_index(index)
        self._write_active({"session_id": session_id, "model": model, "updated_at": now})

    def _validate_model(self, model: str | None) -> str:
        value = (model or self.default_model).strip()
        allowed = {item["id"] for item in self.model_options}
        if value not in allowed:
            raise ValueError(f"Unsupported Claude model: {value}")
        return value

    def _load_model_options(self) -> list[dict[str, str]]:
        raw = (os.getenv("AI_MIME_CLAUDE_MODELS") or "").strip()
        if not raw:
            return list(DEFAULT_CLAUDE_MODEL_OPTIONS)
        options: list[dict[str, str]] = []
        for item in raw.split(","):
            model_id = item.strip()
            if model_id:
                options.append({"id": model_id, "label": model_id, "description": "Configured by AI_MIME_CLAUDE_MODELS."})
        return options or list(DEFAULT_CLAUDE_MODEL_OPTIONS)

    def _read_index(self) -> dict[str, Any]:
        path = self.agent_dir / "session_index.json"
        if not path.exists():
            return {}
        return _read_json(path)

    def _write_index(self, index: dict[str, Any]) -> None:
        _write_json(self.agent_dir / "session_index.json", index)

    def _read_active(self) -> dict[str, Any]:
        path = self.agent_dir / "active_session.json"
        if not path.exists():
            return {}
        return _read_json(path)

    def _write_active(self, active: dict[str, Any]) -> None:
        _write_json(self.agent_dir / "active_session.json", active)
