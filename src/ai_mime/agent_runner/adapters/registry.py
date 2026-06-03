from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ai_mime.agent_runner.adapters.base import AgentRuntime
from ai_mime.agent_runner.adapters.claude_sdk import ClaudeCodeRuntime
from ai_mime.agent_runner.adapters.codex_cli import CodexCliRuntime

RuntimeFactory = Callable[[], AgentRuntime]


@dataclass(frozen=True)
class AgentRuntimeDefinition:
    id: str
    label: str
    factory: RuntimeFactory


_RUNTIMES: dict[str, AgentRuntimeDefinition] = {
    "claude": AgentRuntimeDefinition("claude", "Claude Code", ClaudeCodeRuntime),
    "claude_code": AgentRuntimeDefinition("claude_code", "Claude Code", ClaudeCodeRuntime),
    "codex_cli": AgentRuntimeDefinition("codex_cli", "Codex CLI", CodexCliRuntime),
}


def available_agent_runtimes() -> list[AgentRuntimeDefinition]:
    return list(_RUNTIMES.values())


def get_agent_runtime(runtime_id: str) -> AgentRuntime:
    try:
        definition = _RUNTIMES[runtime_id]
    except KeyError as e:
        known = ", ".join(sorted(_RUNTIMES))
        raise ValueError(f"Unknown agent runtime {runtime_id!r}. Known runtimes: {known}") from e
    return definition.factory()
