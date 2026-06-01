from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Literal, TypedDict

from ai_mime.agent_runner.models import AgentRunRequest, AgentRunResult

AgentRuntimeId = Literal["claude", "claude_code", "codex_cli"]
AgentStreamEventName = Literal[
    "text",
    "tool_use",
    "tool_result",
    "permission_request",
    "done",
    "error",
    "interrupted",
]


class AgentStreamEvent(TypedDict, total=False):
    event: AgentStreamEventName
    text: str
    id: str
    name: str
    input: dict[str, Any]
    tool_use_id: str
    content: Any
    is_error: bool
    session_id: str
    status: str
    error: str | None
    summary: str
    message: str


@dataclass(frozen=True)
class AgentRuntimeCapabilities:
    streaming: bool = False
    sessions: bool = False
    permissions: bool = False
    mcp: bool = False
    structured_output: bool = False
    interrupt: bool = False


class AgentRuntime(ABC):
    id: str
    label: str
    capabilities: AgentRuntimeCapabilities

    @abstractmethod
    def run(self, request: AgentRunRequest, prompt: str) -> AgentRunResult:
        ...

    @abstractmethod
    async def stream_chat(
        self,
        request: AgentRunRequest,
        prompt: str,
        **kwargs: Any,
    ) -> AsyncIterator[AgentStreamEvent]:
        ...

    @abstractmethod
    def list_sessions(self, directory: Path) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    def load_messages(self, session_id: str, directory: Path) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    def interrupt(self) -> bool:
        ...
