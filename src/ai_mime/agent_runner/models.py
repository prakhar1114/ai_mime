from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from ai_mime.app_data import get_bundled_resource

AgentProvider = Literal["claude"]
AgentRunMode = Literal["general", "execute_optimized_plan", "build_skill_chat", "replay_execution"]
AgentRunStatus = Literal["success", "failed", "cancelled", "skill_ready", "skill_unbuildable"]


class AgentModelOption(BaseModel):
    id: str
    label: str
    description: str = ""


class FilesystemAccessEntry(BaseModel):
    path: Path
    reason: str
    approval_required: bool = False


_BROWSER_HARNESS_SKILL_REL = "harness/browser-harness"
_MACOS_COMPUTER_USE_SKILL_REL = "resources/claude-skills/macos-computer-use"
_BROWSER_SKILL_NAME_ENV = "AI_MIME_BROWSER_SKILL_NAME"
_BROWSER_SKILL_PATH_ENV = "AI_MIME_BROWSER_SKILL_PATH"
_MACOS_CU_SKILL_NAME_ENV = "AI_MIME_MACOS_COMPUTER_USE_SKILL_NAME"
_MACOS_CU_SKILL_PATH_ENV = "AI_MIME_MACOS_COMPUTER_USE_SKILL_PATH"


def _skill_path_from_env(key: str, fallback: Path) -> Path:
    raw = (os.environ.get(key) or "").strip()
    if raw:
        candidate = Path(raw).expanduser()
        if candidate.exists() and candidate.is_dir():
            return candidate.resolve()
    return fallback


def resolved_browser_skill_name() -> str:
    return (os.environ.get(_BROWSER_SKILL_NAME_ENV) or "browser").strip() or "browser"


def resolved_browser_skill_path() -> Path:
    return _skill_path_from_env(_BROWSER_SKILL_PATH_ENV, get_bundled_resource(_BROWSER_HARNESS_SKILL_REL))


def resolved_macos_computer_use_skill_name() -> str:
    return (os.environ.get(_MACOS_CU_SKILL_NAME_ENV) or "macos-computer-use").strip() or "macos-computer-use"


def resolved_macos_computer_use_skill_path() -> Path:
    return _skill_path_from_env(
        _MACOS_CU_SKILL_PATH_ENV,
        get_bundled_resource(_MACOS_COMPUTER_USE_SKILL_REL),
    )


def _default_readable_roots() -> tuple[Path, ...]:
    return (
        Path("/tmp"),
        resolved_browser_skill_path(),
        resolved_macos_computer_use_skill_path(),
    )


def _default_writable_roots() -> tuple[Path, ...]:
    return (Path("/tmp"),)


DEFAULT_RUNTIME_ROOTS = _default_readable_roots()


class FilesystemAccess(BaseModel):
    readable_roots: list[FilesystemAccessEntry] = Field(default_factory=list)
    writable_roots: list[FilesystemAccessEntry] = Field(default_factory=list)


class AgentRunRequest(BaseModel):
    provider: AgentProvider
    mode: AgentRunMode
    model: str | None = None
    session_id: str | None = None
    workflow_dir: Path
    workspace_dir: Path
    schema_path: Path | None = None
    optimized_plan_path: Path | None = None
    readable_roots: list[Path] = Field(default_factory=list)
    writable_roots: list[Path] = Field(default_factory=list)
    user_filesystem_access: FilesystemAccess = Field(default_factory=FilesystemAccess)
    temp_dir: Path | None = None
    system_prompt: str | None = None
    allowed_tools: list[str] | None = None
    mcp_servers: dict[str, dict[str, Any]] | None = None

    @model_validator(mode="after")
    def _ensure_default_runtime_roots(self) -> "AgentRunRequest":
        for root in _default_readable_roots():
            if root not in self.readable_roots:
                self.readable_roots.append(root)
        for root in _default_writable_roots():
            if root not in self.writable_roots:
                self.writable_roots.append(root)
        return self


class AgentRunResult(BaseModel):
    status: AgentRunStatus
    session_id: str
    summary: str
    outputs_path: Path | None = None
    error: str | None = None
