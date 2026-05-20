from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

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


DOMAIN_SKILLS_ROOT_FROM_REPO = Path("harness/browser-harness")


def _domain_skills_root() -> Path:
    current_file = Path(__file__).resolve()
    for parent in current_file.parents:
        candidate = parent / DOMAIN_SKILLS_ROOT_FROM_REPO
        if candidate.exists():
            return candidate
    return current_file.parents[3] / DOMAIN_SKILLS_ROOT_FROM_REPO


DEFAULT_RUNTIME_ROOTS = (Path("/tmp"), _domain_skills_root())


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
        for root in DEFAULT_RUNTIME_ROOTS:
            if root not in self.readable_roots:
                self.readable_roots.append(root)
            if root not in self.writable_roots:
                self.writable_roots.append(root)
        return self


class AgentRunResult(BaseModel):
    status: AgentRunStatus
    session_id: str
    summary: str
    outputs_path: Path | None = None
    error: str | None = None
