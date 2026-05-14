from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

AgentProvider = Literal["claude"]
AgentRunMode = Literal["general", "execute_optimized_plan", "build_skill"]
AgentRunStatus = Literal["success", "failed", "cancelled"]


class AgentModelOption(BaseModel):
    id: str
    label: str
    description: str = ""


class FilesystemAccessEntry(BaseModel):
    path: Path
    reason: str
    approval_required: bool = False


class FilesystemAccess(BaseModel):
    readable_roots: list[FilesystemAccessEntry] = Field(default_factory=list)
    writable_roots: list[FilesystemAccessEntry] = Field(default_factory=list)


class BrowserHarnessConfig(BaseModel):
    enabled: bool = False
    harness_root: Path | None = None
    agent_workspace: Path | None = None
    domain_skills_dir: Path | None = None
    shared_domain_skills_dir: Path | None = None


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
    browser_harness: BrowserHarnessConfig = Field(default_factory=BrowserHarnessConfig)
    temp_dir: Path | None = None
    system_prompt: str | None = None


class AgentRunResult(BaseModel):
    status: AgentRunStatus
    session_id: str
    summary: str
    outputs_path: Path | None = None
    error: str | None = None
