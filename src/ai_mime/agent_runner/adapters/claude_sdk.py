from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Literal

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    get_session_messages,
    list_sessions,
    query,
)

from ai_mime.agent_runner.models import AgentRunRequest, AgentRunResult

DEFAULT_ALLOWED_TOOLS = [
    "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "Bash", "Skill",
    "WebFetch", "WebSearch",
]
DEFAULT_SETTING_SOURCES = ["user", "project", "local"]
AUTO_COMPACT_TOKEN_THRESHOLD = 175_000

CanUseToolCallback = Callable[[str, dict[str, Any], Any], Awaitable[dict[str, Any]]]


_READ_FILE_TOOLS = {"Read", "NotebookRead", "Glob", "Grep"}
_WRITE_FILE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
_SANDBOX_MATCHER = "Read|NotebookRead|Glob|Grep|Write|Edit|MultiEdit|NotebookEdit"


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
        if target_path == root_resolved:
            return True
        try:
            target_path.relative_to(root_resolved)
            return True
        except ValueError:
            continue
    return False


def _extract_tool_paths(tool_input: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for key in ("file_path", "path", "notebook_path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            paths.append(value)
    return paths


def _build_filesystem_sandbox_hook(request: AgentRunRequest):
    """PreToolUse hook that denies file ops outside the request's roots.

    Hooks run BEFORE allow rules in the SDK's permission flow, so this is
    the authoritative sandbox even when Read/Write/Edit are auto-allowed.
    Returns None when no roots are configured (no enforcement).
    """
    readable = list(request.readable_roots or [])
    writable = list(request.writable_roots or [])
    if not readable and not writable:
        return None

    async def _hook(input_data, _tool_use_id, _context):  # type: ignore[no-untyped-def]
        tool_name = input_data.get("tool_name") or ""
        tool_input = input_data.get("tool_input") or {}
        if tool_name in _READ_FILE_TOOLS:
            roots = readable
        elif tool_name in _WRITE_FILE_TOOLS:
            roots = writable
        else:
            return {}
        paths = _extract_tool_paths(tool_input)
        if not paths:
            return {}
        denied = [p for p in paths if not _within_roots(p, roots)]
        if denied:
            return {
                "decision": "block",
                "reason": (
                    f"{tool_name} blocked by filesystem sandbox: "
                    f"path(s) outside allowed roots: {denied}"
                ),
            }
        return {}

    return _hook


def _text_from_message(message: Any) -> list[str]:
    if not isinstance(message, AssistantMessage):
        return []
    out: list[str] = []
    for block in message.content or []:
        if isinstance(block, TextBlock):
            out.append(str(block.text))
        elif hasattr(block, "text"):
            out.append(str(getattr(block, "text", "")))
    return [s for s in out if s]


def _result_summary(message: Any) -> str | None:
    if isinstance(message, ResultMessage):
        result = getattr(message, "result", None)
        if isinstance(result, str) and result.strip():
            return result.strip()
    return None


@dataclass(frozen=True)
class ClaudeAgentSdkAdapter:
    allowed_tools: list[str] | None = None

    def run(self, request: AgentRunRequest, prompt: str) -> AgentRunResult:
        return asyncio.run(self._run_async(request, prompt))

    async def _run_async(self, request: AgentRunRequest, prompt: str) -> AgentRunResult:
        try:
            options = ClaudeAgentOptions(**_options_kwargs_for(request, self.allowed_tools))
            assistant_parts: list[str] = []
            result_text: str | None = None
            session_id = request.session_id or ""
            status: Literal["success", "failed", "cancelled"] = "success"
            error: str | None = None
            async for message in query(prompt=prompt, options=options):
                assistant_parts.extend(_text_from_message(message))
                result_text = _result_summary(message) or result_text
                sid = getattr(message, "session_id", None)
                if isinstance(sid, str) and sid:
                    session_id = sid
                subtype = getattr(message, "subtype", None)
                if isinstance(subtype, str) and subtype.startswith("error"):
                    status = "failed"
                    error = subtype
        except Exception as e:
            return AgentRunResult(
                status="failed",
                session_id=request.session_id or "",
                summary="Claude Agent SDK request failed.",
                error=str(e),
            )

        summary = "\n".join(assistant_parts).strip() or result_text or "Claude completed the request."
        return AgentRunResult(status=status, session_id=session_id, summary=summary, error=error)


def list_claude_sessions(directory: Path) -> list[dict[str, Any]]:
    sessions = list_sessions(directory=str(directory))
    out: list[dict[str, Any]] = []
    for item in sessions or []:
        sid = getattr(item, "session_id", None)
        if not isinstance(sid, str) or not sid:
            continue
        out.append(
            {
                "session_id": sid,
                "summary": getattr(item, "summary", None) or getattr(item, "custom_title", None) or sid,
                "last_modified": getattr(item, "last_modified", None),
                "custom_title": getattr(item, "custom_title", None),
                "first_prompt": getattr(item, "first_prompt", None),
            }
        )
    return out


def _options_kwargs_for(
    request: AgentRunRequest,
    allowed_tools: list[str] | None,
    *,
    can_use_tool: CanUseToolCallback | None = None,
    auto_allow_tools: list[str] | None = None,
    skills: list[str] | Literal["all"] | None = "all",
    setting_sources: list[str] | None = None,
) -> dict[str, Any]:
    effective_allowed = allowed_tools if allowed_tools is not None else request.allowed_tools
    available = list(effective_allowed or DEFAULT_ALLOWED_TOOLS)
    kwargs: dict[str, Any] = {
        "cwd": str(request.workspace_dir),
        "tools": available,
        "allowed_tools": list(auto_allow_tools) if auto_allow_tools is not None else list(available),
        "settings": json.dumps(
            {
                "autoCompactEnabled": True,
                "autoCompactWindow": AUTO_COMPACT_TOKEN_THRESHOLD,
            }
        ),
    }
    if request.mcp_servers:
        kwargs["mcp_servers"] = dict(request.mcp_servers)
    if skills is not None:
        kwargs["skills"] = skills
    kwargs["setting_sources"] = list(setting_sources) if setting_sources is not None else list(DEFAULT_SETTING_SOURCES)
    if request.model:
        kwargs["model"] = request.model
    if request.session_id:
        kwargs["resume"] = request.session_id
    if request.system_prompt:
        kwargs["system_prompt"] = {
            "type": "preset",
            "preset": "claude_code",
            "append": request.system_prompt,
        }
    if can_use_tool is not None:
        kwargs["can_use_tool"] = can_use_tool
        kwargs["permission_mode"] = "default"
    sandbox_hook = _build_filesystem_sandbox_hook(request)
    if sandbox_hook is not None:
        existing_hooks = kwargs.get("hooks") or {}
        pre_tool_use = list(existing_hooks.get("PreToolUse") or [])
        pre_tool_use.append(HookMatcher(matcher=_SANDBOX_MATCHER, hooks=[sandbox_hook]))
        existing_hooks["PreToolUse"] = pre_tool_use
        kwargs["hooks"] = existing_hooks
    return kwargs


def _block_text(block: Any) -> str:
    text = getattr(block, "text", None)
    return str(text) if isinstance(text, str) else ""


async def stream_chat(
    request: AgentRunRequest,
    prompt: str,
    *,
    allowed_tools: list[str] | None = None,
    can_use_tool: CanUseToolCallback | None = None,
    auto_allow_tools: list[str] | None = None,
    skills: list[str] | Literal["all"] | None = "all",
    setting_sources: list[str] | None = None,
    on_client: Callable[[ClaudeSDKClient], None] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    options = ClaudeAgentOptions(
        **_options_kwargs_for(
            request,
            allowed_tools,
            can_use_tool=can_use_tool,
            auto_allow_tools=auto_allow_tools,
            skills=skills,
            setting_sources=setting_sources,
        )
    )

    session_id = request.session_id or ""
    status: Literal["success", "failed", "cancelled"] = "success"
    error: str | None = None
    summary_parts: list[str] = []

    client = ClaudeSDKClient(options=options)
    if on_client is not None:
        try:
            on_client(client)
        except Exception:
            pass

    try:
        await client.connect()
        await client.query(prompt)
        async for message in client.receive_response():
            sid = getattr(message, "session_id", None)
            if isinstance(sid, str) and sid:
                session_id = sid
            if isinstance(message, AssistantMessage):
                for block in message.content or []:
                    if isinstance(block, TextBlock):
                        text = _block_text(block)
                        if text:
                            summary_parts.append(text)
                            yield {"event": "text", "text": text}
                    elif isinstance(block, ToolUseBlock):
                        yield {
                            "event": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": block.input or {},
                        }
                    elif isinstance(block, ToolResultBlock):
                        yield {
                            "event": "tool_result",
                            "tool_use_id": block.tool_use_id,
                            "content": block.content,
                            "is_error": bool(block.is_error),
                        }
            elif isinstance(message, UserMessage):
                for block in message.content or []:
                    if isinstance(block, ToolResultBlock):
                        yield {
                            "event": "tool_result",
                            "tool_use_id": block.tool_use_id,
                            "content": block.content,
                            "is_error": bool(block.is_error),
                        }
            if isinstance(message, ResultMessage):
                subtype = getattr(message, "subtype", None)
                if isinstance(subtype, str) and subtype.startswith("error"):
                    status = "failed"
                    error = subtype
                result_text = getattr(message, "result", None)
                if isinstance(result_text, str) and result_text.strip() and not summary_parts:
                    summary_parts.append(result_text.strip())
    except asyncio.CancelledError:
        status = "cancelled"
        error = "interrupted"
        yield {"event": "interrupted"}
    except Exception as e:
        status = "failed"
        error = str(e)
        yield {"event": "error", "message": str(e)}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    yield {
        "event": "done",
        "session_id": session_id,
        "status": status,
        "error": error,
        "summary": "\n".join(summary_parts).strip(),
    }


def load_claude_session_messages(session_id: str, directory: Path) -> list[dict[str, Any]]:
    messages = get_session_messages(session_id, directory=str(directory))
    out: list[dict[str, Any]] = []
    for item in messages or []:
        out.append(
            {
                "type": getattr(item, "type", None) or "assistant",
                "uuid": getattr(item, "uuid", None),
                "session_id": getattr(item, "session_id", None) or session_id,
                "message": getattr(item, "message", None),
            }
        )
    return out
