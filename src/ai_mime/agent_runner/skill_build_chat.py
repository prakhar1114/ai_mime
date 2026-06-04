from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable

from ai_mime.agent_runner.base_chat import BaseAgentChatService, AgentBusyError, _read_json
from ai_mime.agent_runner.models import AgentRunRequest
from ai_mime.agent_runner.runner import (
    BUILD_SIGNAL_FILENAME,
    _skill_dir_for,
    build_agent_run_request,
    run_skill_e2e_test,
    validate_skill_package,
)
from ai_mime.debug_log import log as debug_log

logger = logging.getLogger(__name__)


def _log(message: str, *, exc_info: bool = False) -> None:
    logger.info(message)
    debug_log(f"[skill-build-chat] {message}", exc_info=exc_info)


class WorkflowSkillBuildService(BaseAgentChatService):
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
        self._custom_agent_dir = self.workflow_dir / "agent"
        self.signal_path = self._custom_agent_dir / BUILD_SIGNAL_FILENAME
        self._terminal_status: str | None = None
        super().__init__(
            agent_flow="skill_build",
            adapter=adapter,
            session_lister=session_lister,
            message_loader=message_loader,
            bash_requires_approval=bash_requires_approval,
        )

    @property
    def target_dir(self) -> Path:
        return self.workflow_dir

    @property
    def agent_dir(self) -> Path:
        return self._custom_agent_dir

    @property
    def active_session_filename(self) -> str:
        return "skill_build_active.json"

    def status(self) -> dict[str, Any]:
        base_status = super().status()
        active = self._read_active()
        active_session_id = active.get("session_id") if isinstance(active.get("session_id"), str) else None
        skill_dir = self._skill_dir_hint()
        return {
            "workflow_dir": str(self.workflow_dir),
            "active_session_id": active_session_id,
            **base_status,
            "terminal_status": self._terminal_status,
            "skill_dir": str(skill_dir),
            "has_skill": self._has_runnable_skill(skill_dir),
            "has_optimized_plan": (self.workflow_dir / "optimized_plan.json").exists(),
        }

    def _get_default_mode(self, session_id: str | None, existing: dict[str, Any]) -> str:
        return "Improve" if self._has_runnable_skill(self._skill_dir_hint()) else "Build"

    def _get_auto_allow_tools(self) -> list[str]:
        return ["Glob", "Grep", "Read", "Write", "Edit", "MultiEdit", "Skill", "WebFetch", "WebSearch"]
        
    def _get_stream_temp_dir_prefix(self) -> str:
        return "ai-mime-skill-build-"

    def _check_pre_stream(self) -> None:
        if self._terminal_status:
            raise AgentBusyError(f"Skill build already concluded ({self._terminal_status}); start a new workflow build to continue")

    def _post_stream_hook(self) -> dict[str, Any] | None:
        return self._consume_terminal_signal()

    def reset_terminal(self) -> None:
        """Allow a fresh attempt after a previous terminal signal."""
        self._terminal_status = None
        try:
            if self.signal_path.exists():
                self.signal_path.unlink()
        except Exception:
            pass

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
        if tool_name in {"WebFetch", "WebSearch"}:
            return {"behavior": "allow", "updated_input": input_data, "updated_permissions": None}
        return super()._authorize_tool(request, tool_name, input_data)

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

    def create_session(self) -> dict[str, Any]:
        return {"session_id": None, "summary": "New skill-build chat"}
