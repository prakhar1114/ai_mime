from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any, Callable

from ai_mime.agent_runner.adapters.registry import get_agent_runtime
from ai_mime.agent_runner.base_chat import BaseAgentChatService, AgentBusyError, _build_prompt, agent_config_for_flow, configured_agent_runtime, model_options_from_config, _runtime_id_from_session_meta
from ai_mime.agent_runner.models import AgentRunRequest, AgentRunResult
from ai_mime.agent_runner.runner import build_agent_run_request
from ai_mime.app_data import get_workflows_dir
from ai_mime.debug_log import log as debug_log

logger = logging.getLogger(__name__)


def _log(message: str, *, exc_info: bool = False) -> None:
    logger.info(message)
    debug_log(f"[agent-chat] {message}", exc_info=exc_info)

# Expose definitions that were previously here for backwards compatibility
__all__ = [
    "AgentBusyError",
    "_flow_for_mode",
    "_runtime_id_from_session_meta",
    "agent_config_for_flow",
    "configured_agent_runtime",
    "model_options_from_config",
    "WorkspaceAgentChatService",
]

def _flow_for_mode(mode: str) -> str:
    return "replay" if mode == "replay_execution" else "workspace_chat"


class WorkspaceAgentChatService(BaseAgentChatService):
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
        self._custom_agent_dir = agent_dir
        super().__init__(
            agent_flow=_flow_for_mode(mode),
            adapter=adapter,
            session_lister=session_lister,
            message_loader=message_loader,
            bash_requires_approval=bash_requires_approval,
        )

    @property
    def target_dir(self) -> Path:
        return self.workspace_dir

    @property
    def agent_dir(self) -> Path:
        return self._custom_agent_dir or (self.workspace_dir / ".agent")

    @property
    def active_session_filename(self) -> str:
        return "active_session.json"

    def status(self) -> dict[str, Any]:
        base_status = super().status()
        active = self._read_active()
        return {
            "workspace_dir": str(self.workspace_dir),
            "active_session_id": active.get("session_id"),
            **base_status,
        }

    def _get_default_mode(self, session_id: str | None, existing: dict[str, Any]) -> str:
        return "Run" if self.mode == "replay_execution" else "Chat"

    def _get_auto_allow_tools(self) -> list[str]:
        return ["Glob", "Grep", "Read", "Write", "Edit", "MultiEdit", "Skill"]

    def _build_request(self, *, session_id: str | None, model: str | None, runtime_id: str | None = None) -> AgentRunRequest:
        rt_id = runtime_id or self.runtime_id
        if self.mode != "general":
            return build_agent_run_request(
                workflow_dir=self.workspace_dir,
                provider=rt_id if rt_id in {"claude", "claude_code", "codex_cli"} else "claude",
                mode=self.mode,  # type: ignore[arg-type]
                model=model,
                session_id=session_id,
            )
        agent_dir = self.agent_dir
        return AgentRunRequest(
            provider=rt_id if rt_id in {"claude", "claude_code", "codex_cli"} else "claude",
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

    def _agent_sessions_path(self) -> Path:
        agent_dir = self.workspace_dir / "agent"
        if agent_dir.exists() or (self.workspace_dir / "schema.json").exists():
            return agent_dir / "agent_sessions.json"
        return self.agent_dir / "agent_sessions.json"

    def chat(self, *, message: str, session_id: str | None = None, model: str | None = None) -> dict[str, Any]:
        text = message.strip()
        if not text:
            raise ValueError("message must be non-empty")
        selected_model = self._model_for_request(model)
        resume_id = session_id if session_id and not session_id.startswith("draft-") else None
        resume_runtime_id = self._session_runtime_id(resume_id)
        if resume_id and resume_runtime_id and resume_runtime_id != self.adapter.id:
            raise ValueError(self._runtime_mismatch_error(resume_runtime_id))
        adapter = self._adapter_for_session(resume_id)
        request = self._build_request(session_id=resume_id, model=selected_model, runtime_id=adapter.id)
        if resume_id is None:
            system_prompt = _build_prompt(request)
            request = request.model_copy(update={"system_prompt": system_prompt})
        prompt = text
        with tempfile.TemporaryDirectory(prefix="ai-mime-agent-") as td:
            request = request.model_copy(update={"temp_dir": Path(td)})
            result: AgentRunResult = adapter.run(request, prompt)
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
            runtime_id=adapter.id,
        )
        return {
            "session_id": sid,
            "assistant_text": result.summary,
            "status": result.status,
            "error": result.error,
            "model": selected_model,
        }
