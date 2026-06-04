from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from ai_mime.agent_runner.adapters.claude_sdk import (
    bash_command_requires_approval,
    to_permission_result,
)
from ai_mime.agent_runner.adapters.registry import get_agent_runtime
from ai_mime.agent_runner.chat import (
    AgentBusyError,
    _runtime_id_from_session_meta,
    agent_config_for_flow,
    configured_agent_runtime,
    model_options_from_config,
)
from ai_mime.agent_runner.models import AgentRunRequest
from ai_mime.agent_runner.runner import (
    BUILD_SIGNAL_FILENAME,
    _build_prompt,
    _read_json,
    _skill_dir_for,
    _write_json,
    build_agent_run_request,
    run_skill_e2e_test,
    validate_skill_package,
)
from ai_mime.debug_log import log as debug_log
from ai_mime.provider_settings import read_bash_requires_approval, write_bash_requires_approval
from ai_mime.user_config import load_user_config
from llm_resolver import runtime_model_name


logger = logging.getLogger(__name__)


def _log(message: str, *, exc_info: bool = False) -> None:
    logger.info(message)
    debug_log(f"[skill-build-chat] {message}", exc_info=exc_info)


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class WorkflowSkillBuildService:
    """Per-workflow chat service that iteratively builds a skill package.

    Mirrors WorkspaceAgentChatService but is scoped to a single workflow_dir
    and watches `agent/build_signal.json` for the terminal signal the model
    writes when the package is ready (or declared unbuildable)."""

    def __init__(
        self,
        *,
        workflow_dir: Path,
        adapter: Any | None = None,
        session_lister: Callable[[Path], list[dict[str, Any]]] | None = None,
        message_loader: Callable[[str, Path], list[dict[str, Any]]] | None = None,
        bash_requires_approval: bool | None = None,
    ) -> None:
        self.workflow_dir = Path(workflow_dir)
        self.agent_dir = self.workflow_dir / "agent"
        self.signal_path = self.agent_dir / BUILD_SIGNAL_FILENAME
        self.agent_flow = "skill_build"
        self.config = None if adapter is not None else load_user_config()
        self.agent_config = None if self.config is None else agent_config_for_flow(self.config, self.agent_flow)
        self.adapter = adapter or configured_agent_runtime(self.config, flow=self.agent_flow)
        self.runtime_id = self.adapter.id
        self.session_lister = session_lister or self.adapter.list_sessions
        self.message_loader = message_loader or self.adapter.load_messages
        if bash_requires_approval is None:
            bash_requires_approval = read_bash_requires_approval()
        self.bash_requires_approval = bash_requires_approval
        self._active_client: Any | None = None
        self._active_loop: asyncio.AbstractEventLoop | None = None
        self._pending_permissions: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._session_bash_allow_all: bool = False
        self.model_options = self._load_model_options()
        self._terminal_status: str | None = None

    def status(self) -> dict[str, Any]:
        active = self._read_active()
        active_session_id = active.get("session_id") if isinstance(active.get("session_id"), str) else None
        skill_dir = self._skill_dir_hint()
        return {
            "workflow_dir": str(self.workflow_dir),
            "active_session_id": active_session_id,
            "sessions": self.list_sessions(),
            "models": self.model_options,
            "bash_requires_approval": self.bash_requires_approval,
            "bash_requires_approval_supported": self._bash_approval_supported(),
            "terminal_status": self._terminal_status,
            "skill_dir": str(skill_dir),
            "has_skill": self._has_runnable_skill(skill_dir),
            "has_optimized_plan": (self.workflow_dir / "optimized_plan.json").exists(),
            "active_runtime": self.runtime_id,
        }

    def _bash_approval_supported(self) -> bool:
        """Whether the active runtime honours the Bash-approval gate (Claude only)."""
        return bool(getattr(self.adapter.capabilities, "permissions", False))

    def bash_approval_setting(self) -> dict[str, Any]:
        return {
            "bash_requires_approval": self.bash_requires_approval,
            "bash_requires_approval_supported": self._bash_approval_supported(),
            "active_runtime": self.runtime_id,
        }

    def set_bash_requires_approval(self, value: bool) -> bool:
        self.bash_requires_approval = bool(value)
        if self.bash_requires_approval:
            self._session_bash_allow_all = False
        write_bash_requires_approval(self.bash_requires_approval)
        return self.bash_requires_approval

    def list_models(self) -> dict[str, Any]:
        return {"models": self.model_options}

    def _session_runtime_id(self, session_id: str | None) -> str | None:
        if not session_id or session_id.startswith("draft-"):
            return None
        meta = self._read_index().get(session_id)
        if isinstance(meta, dict):
            return _runtime_id_from_session_meta(meta)
        return None

    def _runtime_mismatch_error(self, runtime_id: str) -> str:
        return (
            f"Cannot resume a {runtime_id} session while the active agent is set to {self.adapter.id}. "
            "Please switch back your agent setting to continue this chat."
        )

    def _adapter_for_session(self, session_id: str | None) -> Any:
        # Resuming a different recorded runtime is blocked by chat_stream.
        # Same-runtime resumes must keep the injected/current adapter.
        return self.adapter

    def list_sessions(self) -> list[dict[str, Any]]:
        index = self._read_index()
        out_by_id: dict[str, dict[str, Any]] = {}
        for sid, meta in index.items():
            if not isinstance(sid, str) or not isinstance(meta, dict):
                continue
            runtime_id = _runtime_id_from_session_meta(meta)
            out_by_id[sid] = {
                "session_id": sid,
                "summary": meta.get("summary") or sid,
                "created_at": meta.get("created_at"),
                "updated_at": meta.get("updated_at"),
                "mode": meta.get("mode") or "build_skill_chat",
                "model": meta.get("model"),
                "runtime_id": runtime_id,
                "source": runtime_id or "ai_mime",
            }

        try:
            current_provider_sessions = self.session_lister(self.workflow_dir)
        except Exception as e:
            if not out_by_id:
                return [{"session_id": "", "summary": f"Failed to list {self.adapter.label} sessions: {e}", "error": str(e)}]
            current_provider_sessions = []

        changed = False
        now = _utc_now()
        for item in current_provider_sessions:
            sid = item.get("session_id") if isinstance(item, dict) else None
            if not isinstance(sid, str) or not sid:
                continue
            existing = index.get(sid) if isinstance(index.get(sid), dict) else {}
            updated_at = item.get("updated_at") or item.get("last_modified") or existing.get("updated_at") or now
            merged = {
                **existing,
                "summary": existing.get("summary") or item.get("summary") or item.get("custom_title") or item.get("first_prompt") or sid,
                "created_at": existing.get("created_at") or updated_at,
                "updated_at": updated_at,
                "mode": existing.get("mode") or ("Improve" if self._has_runnable_skill(self._skill_dir_hint()) else "Build"),
                "model": existing.get("model") or item.get("model"),
                "last_modified": item.get("last_modified") or existing.get("last_modified") or updated_at,
                "custom_title": item.get("custom_title") or existing.get("custom_title"),
                "first_prompt": item.get("first_prompt") or existing.get("first_prompt"),
                "runtime_id": self.runtime_id,
            }
            if index.get(sid) != merged:
                index[sid] = merged
                changed = True
            out_by_id[sid] = {
                "session_id": sid,
                "summary": merged.get("summary") or sid,
                "created_at": merged.get("created_at"),
                "updated_at": merged.get("updated_at"),
                "mode": merged.get("mode") or "build_skill_chat",
                "model": merged.get("model"),
                "last_modified": merged.get("last_modified"),
                "custom_title": merged.get("custom_title"),
                "first_prompt": merged.get("first_prompt"),
                "runtime_id": merged.get("runtime_id"),
                "source": merged.get("runtime_id") or "ai_mime",
            }
        if changed:
            self._write_index(index)

        return sorted(
            out_by_id.values(),
            key=lambda x: str(x.get("updated_at") or x.get("last_modified") or ""),
            reverse=True,
        )

    def create_session(self) -> dict[str, Any]:
        return {"session_id": None, "summary": "New skill-build chat"}

    def load_messages(self, session_id: str) -> list[dict[str, Any]]:
        if not session_id or session_id.startswith("draft-"):
            return []
        runtime_id = self._session_runtime_id(session_id)
        if runtime_id and runtime_id != self.runtime_id:
            return get_agent_runtime(runtime_id).load_messages(session_id, self.workflow_dir)
        return self.message_loader(session_id, self.workflow_dir)

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
        if self._terminal_status:
            raise AgentBusyError(f"Skill build already concluded ({self._terminal_status}); start a new workflow build to continue")
        selected_model = self._model_for_request(model)

        resume_id = session_id if session_id and not session_id.startswith("draft-") else None
        resume_runtime_id = self._session_runtime_id(resume_id)
        if resume_id and resume_runtime_id and resume_runtime_id != self.adapter.id:
            yield {
                "event": "done",
                "session_id": resume_id,
                "status": "failed",
                "error": self._runtime_mismatch_error(resume_runtime_id),
                "summary": "",
            }
            return
        adapter = self._adapter_for_session(resume_id)
        request = self._build_request(session_id=resume_id, model=selected_model, runtime_id=adapter.id)
        if resume_id is None:
            request = request.model_copy(update={"system_prompt": _build_prompt(request)})
        _log(
            f"chat_stream start runtime={adapter.id} workflow={self.workflow_dir} "
            f"session_id={resume_id or '<new>'} model={selected_model or '<default>'} message_chars={len(text)}"
        )

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
        recorded_started_session = False

        auto_allow = [
            "Glob", "Grep", "Read", "Write", "Edit", "MultiEdit", "Skill",
            "WebFetch", "WebSearch",
        ]
        if not self.bash_requires_approval:
            auto_allow.append("Bash")

        async def _wrapped_pump_stream() -> None:
            try:
                with tempfile.TemporaryDirectory(prefix="ai-mime-skill-build-") as td:
                    local_request = request.model_copy(update={"temp_dir": Path(td)})
                    _log(f"chat_stream pump start temp_dir={td} runtime={adapter.id}")
                    async for event in adapter.stream_chat(
                        local_request,
                        text,
                        can_use_tool=can_use_tool,
                        auto_allow_tools=auto_allow,
                        on_client=_store_client,
                    ):
                        if event.get("event") in {"error", "done", "tool_use", "interrupted", "session_started"}:
                            _log(f"chat_stream event {event}")
                        await event_queue.put(event)
            except Exception as e:
                _log(f"chat_stream pump failed: {e}", exc_info=True)
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
                event_type = event.get("event")
                if event_type == "session_started":
                    final_session_id = str(event.get("session_id") or final_session_id)
                    if final_session_id and not recorded_started_session:
                        self._record_session(
                            session_id=final_session_id,
                            previous_session_id=session_id,
                            summary=(text[:80] or final_session_id),
                            status="running",
                            error=None,
                            model=selected_model,
                            runtime_id=adapter.id,
                        )
                        recorded_started_session = True
                elif event_type == "error":
                    final_status = "failed"
                    final_error = str(event.get("message") or "Skill-build runtime error.")
                elif event_type == "interrupted":
                    final_status = "cancelled"
                    final_error = "interrupted"
                elif event_type == "done":
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

        if final_session_id:
            _log(
                f"chat_stream complete runtime={adapter.id} session_id={final_session_id} "
                f"status={final_status} summary_chars={len(final_summary)} error={final_error or ''}"
            )
            self._record_session(
                session_id=final_session_id,
                previous_session_id=session_id,
                summary=(text[:80] or final_session_id),
                status=final_status,
                error=final_error,
                model=selected_model,
                runtime_id=adapter.id,
            )

        terminal_event = self._consume_terminal_signal()
        if terminal_event is not None:
            yield terminal_event

    def interrupt(self) -> bool:
        client = self._active_client
        loop = self._active_loop
        if client is None or loop is None:
            return self.adapter.interrupt()
        try:
            fut = asyncio.run_coroutine_threadsafe(client.interrupt(), loop)
            fut.result(timeout=5)
            return True
        except Exception:
            return False

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

    def reset_terminal(self) -> None:
        """Allow a fresh attempt after a previous terminal signal."""
        self._terminal_status = None
        try:
            if self.signal_path.exists():
                self.signal_path.unlink()
        except Exception:
            pass

    @staticmethod
    def _set_future_result(future: asyncio.Future, result: dict[str, Any]) -> None:
        if not future.done():
            future.set_result(result)

    def _consume_terminal_signal(self) -> dict[str, Any] | None:
        if not self.signal_path.exists():
            return None
        try:
            signal = _read_json(self.signal_path)
        except Exception as e:
            return {"event": "skill_check_failed", "error": f"build_signal.json unreadable: {e}"}

        raw_status = str(signal.get("status") or "").strip()
        if raw_status == "skill_unbuildable":
            reason = str(signal.get("reason") or "Workflow cannot be made deterministic.")
            self._terminal_status = "skill_unbuildable"
            return {
                "event": "skill_build_done",
                "status": "skill_unbuildable",
                "reason": reason,
                "suggested_changes": signal.get("suggested_changes") or [],
                "summary": signal.get("summary") or reason,
            }

        if raw_status != "skill_ready":
            # Unknown status — surface and clear so the model can try again.
            try:
                self.signal_path.unlink()
            except Exception:
                pass
            return {"event": "skill_check_failed", "error": f"Unknown build_signal.status: {raw_status!r}"}

        # skill_ready → run validate + e2e
        schema_path = self.workflow_dir / "schema.json"
        plan_path = self.workflow_dir / "optimized_plan.json"
        try:
            schema = _read_json(schema_path)
            plan = _read_json(plan_path)
        except Exception as e:
            try:
                self.signal_path.unlink()
            except Exception:
                pass
            return {"event": "skill_check_failed", "error": f"Could not load schema/plan: {e}"}

        skill_dir = _skill_dir_for(self.workflow_dir, schema)
        try:
            validate_skill_package(skill_dir, schema, plan)
        except Exception as e:
            try:
                self.signal_path.unlink()
            except Exception:
                pass
            return {
                "event": "skill_check_failed",
                "error": f"validate_skill_package failed: {e}",
                "skill_dir": str(skill_dir),
            }

        e2e = run_skill_e2e_test(
            skill_dir,
            plan,
            confirmed_inputs_path=self.workflow_dir / "agent" / "confirmed_inputs.json",
        )
        if e2e.status != "success":
            try:
                self.signal_path.unlink()
            except Exception:
                pass
            return {
                "event": "skill_check_failed",
                "error": e2e.error or "scripts/run.py e2e test failed",
                "logs": e2e.summary,
                "skill_dir": str(skill_dir),
            }

        self._terminal_status = "skill_ready"
        return {
            "event": "skill_build_done",
            "status": "skill_ready",
            "summary": signal.get("summary") or "Skill package built and verified.",
            "skill_dir": str(skill_dir),
            "e2e_logs": e2e.summary,
        }

    def _authorize_tool(
        self, request: AgentRunRequest, tool_name: str, input_data: dict[str, Any]
    ) -> dict[str, Any]:
        # Read/Glob/Grep and Write/Edit/MultiEdit are auto-allowed at the SDK
        # level, so they never reach this callback; their filesystem scope is
        # enforced by the PreToolUse sandbox hook in claude_sdk.py instead.
        if tool_name == "Skill":
            return {"behavior": "allow", "updated_input": input_data, "updated_permissions": None}
        if tool_name in {"WebFetch", "WebSearch"}:
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
            command = input_data.get("command")
            if isinstance(command, str) and not bash_command_requires_approval(command):
                return {"behavior": "allow", "updated_input": input_data, "updated_permissions": None}
            return {
                "behavior": "ask",
                "reason": "Bash command requires your approval.",
            }
        return {
            "behavior": "deny",
            "message": f"{tool_name} is not enabled for the skill builder.",
            "interrupt": False,
        }

    def _build_request(self, *, session_id: str | None, model: str | None, runtime_id: str | None = None) -> AgentRunRequest:
        rt_id = runtime_id or self.runtime_id
        base = build_agent_run_request(
            workflow_dir=self.workflow_dir,
            mode="build_skill_chat",
            provider=rt_id if rt_id in {"claude", "claude_code", "codex_cli"} else "claude",
            model=model,
            session_id=session_id,
        )
        return base

    def _skill_dir_hint(self) -> Path:
        schema_path = self.workflow_dir / "schema.json"
        try:
            schema = _read_json(schema_path) if schema_path.exists() else {}
        except Exception:
            schema = {}
        return _skill_dir_for(self.workflow_dir, schema)

    @staticmethod
    def _has_runnable_skill(skill_dir: Path) -> bool:
        run_sh = skill_dir / "run.sh"
        return run_sh.is_file() and os.access(run_sh, os.X_OK)

    def _agent_sessions_path(self) -> Path:
        return self.agent_dir / "agent_sessions.json"

    def _record_session(
        self,
        *,
        session_id: str,
        previous_session_id: str | None,
        summary: str,
        status: str,
        error: str | None,
        model: str | None,
        runtime_id: str | None = None,
    ) -> None:
        now = _utc_now()
        index = self._read_index()
        if previous_session_id and previous_session_id.startswith("draft-") and previous_session_id in index:
            index.pop(previous_session_id, None)
        existing = index.get(session_id) if isinstance(index.get(session_id), dict) else {}
        
        skill_dir = self._skill_dir_hint()
        has_skill = self._has_runnable_skill(skill_dir)
        mode = "Improve" if has_skill else "Build"

        index[session_id] = {
            "summary": existing.get("summary") or summary,
            "created_at": existing.get("created_at") or now,
            "updated_at": now,
            "mode": mode,
            "model": model,
            "last_status": status,
            "last_error": error,
            "runtime_id": runtime_id or existing.get("runtime_id") or self.runtime_id,
        }
        self._write_index(index)
        self._write_active({"session_id": session_id, "model": model, "updated_at": now})

    def _model_for_request(self, model: str | None) -> str | None:
        if self.config is None:
            value = (model or "").strip()
            return value or None
        assert self.agent_config is not None
        configured_model = str(getattr(self.agent_config, "model", "") or "").strip()
        return runtime_model_name(configured_model) or None

    def _load_model_options(self) -> list[dict[str, str]]:
        if self.config is None:
            return []
        return model_options_from_config(self.config, flow=self.agent_flow)

    def _read_index(self) -> dict[str, Any]:
        path = self._agent_sessions_path()
        if not path.exists():
            return {}
        try:
            index = _read_json(path)
        except Exception:
            return {}
        return index

    def _write_index(self, index: dict[str, Any]) -> None:
        path = self._agent_sessions_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(path, index)

    def _read_active(self) -> dict[str, Any]:
        path = self.agent_dir / "skill_build_active.json"
        if not path.exists():
            return {}
        return _read_json(path)

    def _write_active(self, active: dict[str, Any]) -> None:
        _write_json(self.agent_dir / "skill_build_active.json", active)
