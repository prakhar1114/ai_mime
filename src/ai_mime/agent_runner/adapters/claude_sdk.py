from __future__ import annotations
import os
import shutil
from pathlib import Path

import asyncio
import json
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Literal, Sequence

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    get_session_messages,
    list_sessions,
    query,
)

from ai_mime.agent_runner.adapters.base import AgentRuntime, AgentRuntimeCapabilities
from ai_mime.agent_runner.bash_safety import (
    _ENV_ASSIGNMENT_RE,
    _SHELL_COMMAND_PREFIXES,
    _SHELL_SEPARATORS,
    bash_command_requires_approval,  # re-exported for chat services
)
from ai_mime.agent_runner.models import AgentRunRequest, AgentRunResult
from ai_mime.app_data import is_frozen, workflow_runtime_env
from ai_mime.credentials_store import credentials_mode_for
from ai_mime.debug_log import log as debug_log

DEFAULT_ALLOWED_TOOLS = [
    "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "Bash", "Skill",
    "WebFetch", "WebSearch",
]
DEFAULT_ENABLED_SKILLS = (
    "browser",
    "browser-harness",
    "skill-creator:skill-creator",
)
DEFAULT_SETTING_SOURCES = ["user", "project", "local"]
AUTO_COMPACT_TOKEN_THRESHOLD = 175_000

CanUseToolCallback = Callable[[str, dict[str, Any], Any], Awaitable[Any]]


def to_permission_result(decision: dict[str, Any]) -> PermissionResultAllow | PermissionResultDeny:
    """Convert an internal authorize-dict into the SDK's typed PermissionResult.

    The chat services reason in plain dicts internally (with extra `ask` /
    `allow_always` control-flow behaviors), but the SDK's `can_use_tool` callback
    MUST return a `PermissionResultAllow` / `PermissionResultDeny` instance — a raw
    dict raises "Tool permission callback must return PermissionResult". Treat
    `allow`/`allow_always` as allow; everything else (deny, unknown) as deny.
    """
    if decision.get("behavior") in ("allow", "allow_always"):
        return PermissionResultAllow(
            updated_input=decision.get("updated_input"),
            updated_permissions=decision.get("updated_permissions"),
        )
    return PermissionResultDeny(
        message=str(decision.get("message") or "Tool call denied."),
        interrupt=bool(decision.get("interrupt", False)),
    )


_READ_FILE_TOOLS = {"Read", "NotebookRead", "Glob", "Grep"}
_WRITE_FILE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
_SANDBOX_MATCHER = "Read|NotebookRead|Glob|Grep|Write|Edit|MultiEdit|NotebookEdit"
_BASH_MATCHER = "Bash"
_CLAUDE_SDK_STDERR_PREFIX = "[ai-mime claude-sdk stderr]"
_PACKAGED_FORBIDDEN_BARE_COMMANDS = {
    "uv": "$AI_MIME_UV_PATH",
    "python": "$AI_MIME_PYTHON_PATH",
    "python3": "$AI_MIME_PYTHON_PATH",
    "browser-harness": "$AI_MIME_BROWSER_HARNESS_BIN",
    "uvx": "$AI_MIME_UV_PATH",
    "npx": "app-managed Python or a documented bundled tool",
}


def _log_claude_sdk_stderr(data: str) -> None:
    text = str(data or "")
    if not text:
        return
    for line in text.splitlines() or [text]:
        debug_log(f"{_CLAUDE_SDK_STDERR_PREFIX} {line}")


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


def _packaged_bash_block_reason(command: str) -> str | None:
    """Return a block reason for obvious host-tool Bash invocations.

    Only flags tokens in command position (the first word of the command or of a
    pipeline/list segment). A Homebrew/usr-local path that appears merely as a
    string literal or argument is not blocked.
    """
    if not command.strip():
        return None

    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = re.findall(r"[^\s]+", command)

    expect_command = True
    for token in tokens:
        if token in _SHELL_SEPARATORS:
            expect_command = True
            continue
        if not expect_command:
            continue
        if _ENV_ASSIGNMENT_RE.match(token):
            continue
        if token in _SHELL_COMMAND_PREFIXES:
            expect_command = True
            continue
        if token.startswith("/opt/homebrew/") or token.startswith("/usr/local/"):
            return f"Bash command uses host tool path {token}; use app-managed tool env vars instead."
        replacement = _PACKAGED_FORBIDDEN_BARE_COMMANDS.get(token)
        if replacement is not None:
            return f"Bash command uses bare `{token}` in packaged mode; use `{replacement}` instead."
        expect_command = False
    return None


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


def _build_packaged_bash_guard_hook():
    """PreToolUse hook that blocks obvious host tool usage in frozen builds."""
    if not is_frozen():
        return None

    async def _hook(input_data, _tool_use_id, _context):  # type: ignore[no-untyped-def]
        tool_name = input_data.get("tool_name") or ""
        if tool_name != "Bash":
            return {}
        tool_input = input_data.get("tool_input") or {}
        command = tool_input.get("command")
        if not isinstance(command, str):
            return {}
        reason = _packaged_bash_block_reason(command)
        if reason is None:
            return {}
        return {"decision": "block", "reason": reason}

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


def _is_valid_session_id(session_id: str | None, workspace_dir: Path) -> bool:
    if not session_id:
        return False
    try:
        sessions = list_sessions(directory=str(workspace_dir))
        for item in sessions or []:
            sid = getattr(item, "session_id", None)
            if sid == session_id:
                return True
    except Exception:
        pass
    return False


@dataclass(frozen=True)
class ClaudeCodeRuntime(AgentRuntime):
    id: str = field(default="claude_code", init=False)
    label: str = field(default="Claude Code", init=False)
    capabilities: AgentRuntimeCapabilities = field(
        default=AgentRuntimeCapabilities(
            streaming=True,
            sessions=True,
            permissions=True,
            mcp=True,
            structured_output=False,
            interrupt=True,
        ),
        init=False,
    )
    allowed_tools: list[str] | None = None

    def run(self, request: AgentRunRequest, prompt: str) -> AgentRunResult:
        return asyncio.run(self._run_async(request, prompt))

    async def stream_chat(self, request: AgentRunRequest, prompt: str, **kwargs: Any):
        async for event in stream_chat(request, prompt, **kwargs):
            yield event

    def list_sessions(self, directory: Path) -> list[dict[str, Any]]:
        return list_claude_sessions(directory)

    def load_messages(self, session_id: str, directory: Path) -> list[dict[str, Any]]:
        return load_claude_session_messages(session_id, directory)

    def interrupt(self) -> bool:
        return False

    async def _run_async(self, request: AgentRunRequest, prompt: str) -> AgentRunResult:
        if request.session_id and not _is_valid_session_id(request.session_id, request.workspace_dir):
            debug_log(f"Session {request.session_id} not found in {request.workspace_dir}. Starting a new session.")
            request = request.model_copy(update={"session_id": None})

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


ClaudeAgentSdkAdapter = ClaudeCodeRuntime


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


def _find_claude_exe() -> str | None:
    """Find the local Claude Code executable path, checking common fallback directories.

    This matches the detection logic in onboarding.py to ensure the path is consistent
    across both onboarding and agent execution, even if the parent terminal's PATH
    is stripped or not inherited.
    """

    exe = shutil.which("claude")
    if exe:
        return exe

    fallback_dirs = (
        ".local/bin",
        "bin",
        "/opt/homebrew/bin",
        "/usr/local/bin",
    )
    home = Path.home()
    for candidate_dir in fallback_dirs:
        candidate = Path(candidate_dir)
        if not candidate.is_absolute():
            candidate = home / candidate
        candidate = candidate / "claude"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)

    return None


def _options_kwargs_for(
    request: AgentRunRequest,
    allowed_tools: list[str] | None,
    *,
    can_use_tool: CanUseToolCallback | None = None,
    auto_allow_tools: list[str] | None = None,
    skills: Sequence[str] | Literal["all"] | None = DEFAULT_ENABLED_SKILLS,
    setting_sources: list[str] | None = None,
) -> dict[str, Any]:
    effective_allowed = allowed_tools if allowed_tools is not None else request.allowed_tools
    available = list(effective_allowed or DEFAULT_ALLOWED_TOOLS)
    kwargs: dict[str, Any] = {
        "cwd": str(request.workspace_dir),
        "tools": available,
        "allowed_tools": list(auto_allow_tools) if auto_allow_tools is not None else list(available),
        "stderr": _log_claude_sdk_stderr,
        "max_buffer_size": 20 * 1024 * 1024,
        "settings": json.dumps(
            {
                "autoCompactEnabled": True,
                "autoCompactWindow": AUTO_COMPACT_TOKEN_THRESHOLD,
            }
        ),
    }
    claude_path = _find_claude_exe()
    if claude_path:
        kwargs["cli_path"] = claude_path
    if request.mcp_servers:
        kwargs["mcp_servers"] = dict(request.mcp_servers)
    if skills is not None:
        kwargs["skills"] = skills if skills == "all" else list(skills)
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
    # Export the app-managed runtime env (app Python/uv/browser-harness paths and,
    # when frozen, the sanitized PATH) to the CLI and its Bash subprocess. The SDK
    # merges this over the inherited os.environ, so these win. This is what makes
    # the bash guard's `$AI_MIME_*` replacements actually resolve — for both
    # ClaudeAgentSdkAdapter.run and the interactive stream_chat paths.
    kwargs["env"] = dict(
        workflow_runtime_env(
            request.workflow_dir,
            credentials_mode=credentials_mode_for(request.mode),
        )
    )

    sandbox_hook = _build_filesystem_sandbox_hook(request)
    bash_guard_hook = _build_packaged_bash_guard_hook()
    _add_pre_tool_use_hook(kwargs, _SANDBOX_MATCHER, sandbox_hook)
    _add_pre_tool_use_hook(kwargs, _BASH_MATCHER, bash_guard_hook)
    return kwargs


def _add_pre_tool_use_hook(kwargs: dict[str, Any], matcher: str, hook) -> None:
    """Append a PreToolUse HookMatcher to the options kwargs, no-op if hook is None."""
    if hook is None:
        return
    existing_hooks = kwargs.get("hooks") or {}
    pre_tool_use = list(existing_hooks.get("PreToolUse") or [])
    pre_tool_use.append(HookMatcher(matcher=matcher, hooks=[hook]))
    existing_hooks["PreToolUse"] = pre_tool_use
    kwargs["hooks"] = existing_hooks


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
    skills: Sequence[str] | Literal["all"] | None = DEFAULT_ENABLED_SKILLS,
    setting_sources: list[str] | None = None,
    on_client: Callable[[ClaudeSDKClient], None] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    if request.session_id and not _is_valid_session_id(request.session_id, request.workspace_dir):
        debug_log(f"Session {request.session_id} not found in {request.workspace_dir}. Starting a new session.")
        request = request.model_copy(update={"session_id": None})

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
