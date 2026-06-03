from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Iterable

from ai_mime.agent_runner.adapters.base import AgentRuntime, AgentRuntimeCapabilities, AgentStreamEvent
from ai_mime.agent_runner.models import (
    AgentRunRequest,
    AgentRunResult,
    resolved_browser_skill_name,
    resolved_browser_skill_path,
)
from ai_mime.app_data import workflow_runtime_env
from ai_mime.codex_support import codex_subprocess_env, find_codex_executable
from ai_mime.debug_log import log as debug_log


logger = logging.getLogger(__name__)
_CODEX_RESTRICTED_AGENT_MODES = {"general", "build_skill_chat", "replay_execution"}
_CODEX_RESTRICTED_FEATURES = (
    "computer_use",
    "apps",
    "plugins",
    "tool_search",
    "multi_agent",
    "browser_use",
    "browser_use_external",
)


def _log(message: str, *, exc_info: bool = False) -> None:
    logger.info(message)
    debug_log(f"[codex-sdk] {message}", exc_info=exc_info)


def _load_codex_sdk() -> tuple[Any, Any, Any, Any]:
    from openai_codex import AsyncCodex, Codex, CodexConfig, Sandbox  # type: ignore[import-not-found]

    return Codex, AsyncCodex, CodexConfig, Sandbox


def _is_missing_thread_error(error: BaseException) -> bool:
    text = str(error).lower()
    return "no rollout found for thread id" in text or "thread" in text and "not found" in text


class _AsyncCodexInterruptClient:
    def __init__(self, runtime: "CodexCliRuntime") -> None:
        self._runtime = runtime

    async def interrupt(self) -> None:
        turn = self._runtime._active_turn
        if turn is None:
            return
        self._runtime._interrupted = True
        result = turn.interrupt()
        if hasattr(result, "__await__"):
            await result


def _extract_text(value: Any, _seen: set[int] | None = None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if _seen is None:
        _seen = set()
    value_id = id(value)
    if value_id in _seen:
        return ""
    _seen.add(value_id)
    root = getattr(value, "root", None)
    if root is not None and root is not value:
        return _extract_text(root, _seen)
    if isinstance(value, (list, tuple)):
        return "\n".join(part for part in (_extract_text(item, _seen) for item in value) if part)
    if isinstance(value, dict):
        for key in ("text", "content", "message", "input_text", "output_text", "delta"):
            if key in value:
                text = _extract_text(value.get(key), _seen)
                if text:
                    return text
        return ""
    for key in ("text", "content", "message", "input_text", "output_text", "delta"):
        if hasattr(value, key):
            text = _extract_text(getattr(value, key), _seen)
            if text:
                return text
    data = _as_dict(value)
    if data and data is not value:
        return _extract_text(data, _seen)
    return str(value)


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json", by_alias=True)
        return dumped if isinstance(dumped, dict) else {}
    if hasattr(value, "__dict__"):
        return {key: item for key, item in vars(value).items() if not key.startswith("_")}
    return {}


def _jsonable(value: Any, _seen: set[int] | None = None) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if _seen is None:
        _seen = set()
    value_id = id(value)
    if value_id in _seen:
        return ""
    _seen.add(value_id)
    if isinstance(value, (list, tuple)):
        return [_jsonable(item, _seen) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item, _seen) for key, item in value.items()}
    data = _as_dict(value)
    if data and data is not value:
        return _jsonable(data, _seen)
    return str(value)


def _root(value: Any) -> Any:
    return getattr(value, "root", value)


def _field(value: Any, *names: str) -> Any:
    value = _root(value)
    if isinstance(value, dict):
        for name in names:
            if name in value:
                return value[name]
        return None
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    data = _as_dict(value)
    for name in names:
        if name in data:
            return data[name]
    return None


def _enum_value(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "")


def _is_tool_item_type(item_type: str) -> bool:
    normalized = item_type.replace("_", "").lower()
    return "tool" in normalized or "command" in normalized


def _codex_model(model: str | None) -> str | None:
    if not model:
        return None
    text = model.strip()
    if text.startswith("openai/"):
        return text.split("/", 1)[1].strip() or None
    return text or None


def _codex_sdk_approval_handler(method: str, params: dict[str, Any] | None) -> dict[str, Any]:
    """Approve app-server requests that are already scoped by AI Mime.

    The Python SDK's default handler accepts command/file approvals, but returns
    an empty response for MCP elicitation requests. The CUA server uses MCP
    elicitation to confirm Computer Use access, and an empty response is treated
    as a denial before AI Mime's own tool authorization flow can help.
    """
    _log(f"approval request method={method} params={params or {}}")
    if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
        return {"decision": "accept"}
    if method == "mcpServer/elicitation/request":
        return {"action": "accept", "content": {}, "_meta": None}
    return {}


def _install_codex_sdk_approval_handler(codex: Any) -> None:
    client = getattr(codex, "_client", None)
    sync_client = getattr(client, "_sync", None) or client
    if sync_client is not None and hasattr(sync_client, "_approval_handler"):
        setattr(sync_client, "_approval_handler", _codex_sdk_approval_handler)


def _read_output_schema(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    with path.open("r", encoding="utf-8") as fh:
        parsed = json.load(fh)
    if not isinstance(parsed, dict):
        return None
    if parsed.get("type") != "object" or not isinstance(parsed.get("properties"), dict):
        return None
    return parsed


def _summary_from_items(items: Iterable[Any]) -> str:
    parts: list[str] = []
    for item in items:
        item = _root(item)
        item_type = str(_field(item, "type") or "").lower()
        role = str(_field(item, "role") or "").lower()
        normalized_item_type = item_type.replace("_", "")
        if item_type and "agentmessage" not in normalized_item_type and role != "assistant":
            continue
        text = _extract_text(_field(item, "text", "content", "message"))
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def _toml_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_literal(item) for item in value) + "]"
    if isinstance(value, dict):
        items = ", ".join(f"{key} = {_toml_literal(item)}" for key, item in value.items())
        return "{" + items + "}"
    if value is None:
        raise RuntimeError("Codex MCP config does not support null values.")
    return json.dumps(value)


def _codex_mcp_config_overrides(mcp_servers: dict[str, dict[str, Any]] | None) -> list[str]:
    if not mcp_servers:
        return []

    overrides: list[str] = []
    for name, server in mcp_servers.items():
        server_type = server.get("type")
        prefix = f"mcp_servers.{name}"
        required = bool(server.get("required", False))
        startup_timeout_sec = server.get("startup_timeout_sec", 30)
        tool_timeout_sec = server.get("tool_timeout_sec", 120)
        if server_type == "http":
            url = server.get("url")
            if not isinstance(url, str) or not url.strip():
                raise RuntimeError(f"Codex MCP server {name!r} requires a non-empty url.")
            overrides.extend([
                f"{prefix}.url={_toml_literal(url)}",
                f"{prefix}.required={_toml_literal(required)}",
                f"{prefix}.startup_timeout_sec={_toml_literal(startup_timeout_sec)}",
                f"{prefix}.tool_timeout_sec={_toml_literal(tool_timeout_sec)}",
                f"{prefix}.default_tools_approval_mode={_toml_literal('approve')}",
            ])
            continue

        if server_type == "stdio":
            command = server.get("command")
            if not isinstance(command, str) or not command.strip():
                raise RuntimeError(f"Codex MCP server {name!r} requires a non-empty command.")
            args = server.get("args", [])
            if args is None:
                args = []
            if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
                raise RuntimeError(f"Codex MCP server {name!r} args must be a list of strings.")
            overrides.extend([
                f"{prefix}.command={_toml_literal(command)}",
                f"{prefix}.args={_toml_literal(args)}",
                f"{prefix}.required={_toml_literal(required)}",
                f"{prefix}.startup_timeout_sec={_toml_literal(startup_timeout_sec)}",
                f"{prefix}.tool_timeout_sec={_toml_literal(tool_timeout_sec)}",
                f"{prefix}.default_tools_approval_mode={_toml_literal('approve')}",
            ])
            continue

        raise RuntimeError(f"Unsupported Codex MCP server {name!r} type: {server_type!r}.")
    return overrides


def _codex_capability_config_overrides(request: AgentRunRequest | None) -> list[str]:
    if request is None or request.mode not in _CODEX_RESTRICTED_AGENT_MODES:
        return []
    return [f"features.{name}=false" for name in _CODEX_RESTRICTED_FEATURES]


def _codex_config_overrides(request: AgentRunRequest | None) -> list[str]:
    return [
        "sandbox_workspace_write.network_access=true",
        *_codex_capability_config_overrides(request),
        *_codex_mcp_config_overrides(request.mcp_servers if request is not None else None),
    ]


def _should_attach_browser_skill(skills: Any) -> bool:
    if skills is None:
        return True
    if skills == "all":
        return True
    if isinstance(skills, (list, tuple, set, frozenset)):
        if not skills:
            return False
        names = {str(item) for item in skills}
        return bool(names & {"browser", "browser-harness"})
    return True


def _browser_skill_input() -> Any | None:
    path = resolved_browser_skill_path()
    if not path.is_dir() or not (path / "SKILL.md").is_file():
        return None
    from openai_codex import SkillInput  # type: ignore[import-not-found]

    return SkillInput(name=resolved_browser_skill_name(), path=str(path))


def _codex_turn_input(prompt: str, *, skills: Any = None) -> Any:
    if not _should_attach_browser_skill(skills):
        return prompt
    skill_input = _browser_skill_input()
    if skill_input is None:
        return prompt
    from openai_codex import TextInput  # type: ignore[import-not-found]

    return [skill_input, TextInput(prompt)]


def _tool_result_content(source: Any) -> Any:
    result = _field(source, "result")
    if result is not None:
        content = _field(result, "content", "structuredContent", "structured_content")
        return content if content is not None else result

    content = _field(
        source,
        "content",
        "output",
        "aggregatedOutput",
        "aggregated_output",
        "contentItems",
        "content_items",
        "structuredContent",
        "structured_content",
        "message",
    )
    if content is not None:
        return content

    error_obj = _field(source, "error")
    if error_obj is not None:
        return _extract_text(error_obj) or error_obj

    stdout = _field(source, "stdout")
    stderr = _field(source, "stderr")
    if stdout is not None or stderr is not None:
        return "\n".join(str(part) for part in (stdout, stderr) if part)

    status = _enum_value(_field(source, "status")).lower()
    if status == "declined":
        command = _field(source, "command")
        return f"Command blocked: {command}" if command else "Command blocked."

    return None


def _tool_result_is_error(source: Any) -> bool:
    explicit = _field(source, "isError", "is_error")
    if explicit is not None:
        return bool(explicit)
    if _field(source, "error") is not None:
        return True
    status = _enum_value(_field(source, "status")).lower()
    return status in {"declined", "failed", "errored", "error"}


def _tool_result_event(source: Any, *, tool_id: str | None = None) -> AgentStreamEvent | None:
    resolved_id = tool_id or str(_field(source, "toolUseId", "tool_use_id", "toolCallId", "tool_call_id", "id") or "")
    content = _jsonable(_tool_result_content(source))
    if not resolved_id and content is None:
        return None
    return {
        "event": "tool_result",
        "tool_use_id": resolved_id,
        "content": content,
        "is_error": _tool_result_is_error(source),
    }


def _is_completed_agent_message(notification: Any) -> bool:
    payload = _field(notification, "payload") or notification
    item = _root(_field(payload, "item") or _as_dict(payload).get("item"))
    item_type = str(_field(item, "type") or "").lower().replace("_", "")
    return "agentmessage" in item_type or str(_field(item, "role") or "").lower() == "assistant"


def _notification_to_agent_events(notification: Any) -> list[AgentStreamEvent]:
    method = str(_field(notification, "method") or "")
    payload = _field(notification, "payload") or notification
    data = _as_dict(payload)

    if method == "item/agentMessage/delta":
        text = _extract_text(_field(payload, "delta") or data.get("delta"))
        return [{"event": "text", "text": text}] if text else []

    if method == "item/commandExecution/outputDelta":
        tool_id = str(_field(payload, "itemId", "item_id") or "")
        text = _extract_text(_field(payload, "delta") or data.get("delta"))
        return [{
            "event": "tool_result",
            "tool_use_id": tool_id,
            "content": text,
            "is_error": False,
            "append": True,
        }] if tool_id or text else []

    if method == "item/mcpToolCall/progress":
        tool_id = str(_field(payload, "itemId", "item_id") or "")
        text = _extract_text(_field(payload, "message") or data.get("message"))
        return [{
            "event": "tool_result",
            "tool_use_id": tool_id,
            "content": text,
            "is_error": False,
            "append": True,
        }] if tool_id or text else []

    if method == "item/completed":
        item = _root(_field(payload, "item") or data.get("item"))
        item_type = str(_field(item, "type") or "").lower()
        text = _extract_text(_field(item, "text", "content", "message"))
        if text and ("agentmessage" in item_type.replace("_", "") or _field(item, "role") == "assistant"):
            return [{"event": "text", "text": text}]
        if _is_tool_item_type(item_type):
            event = _tool_result_event(item, tool_id=str(_field(item, "id") or ""))
            return [event] if event is not None else []
        return []

    if method in {"item/started", "item/updated"}:
        item = _root(_field(payload, "item") or data.get("item"))
        item_type = str(_field(item, "type") or "").lower()
        if not _is_tool_item_type(item_type):
            return []
        normalized_item_type = item_type.replace("_", "").lower()
        is_command = "command" in normalized_item_type
        name = "Bash" if is_command else str(
            _field(item, "name", "tool", "toolName", "tool_name")
            or item_type
            or "tool"
        )
        input_data = _field(item, "input", "arguments") or {}
        if not isinstance(input_data, dict):
            input_data = {"value": input_data}
        command = _field(item, "command")
        if command and "command" not in input_data:
            input_data["command"] = command
        cwd = _field(item, "cwd")
        if cwd and "cwd" not in input_data:
            input_data["cwd"] = _jsonable(cwd)
        server = _field(item, "server")
        if server and "server" not in input_data:
            input_data["server"] = server
        return [{
            "event": "tool_use",
            "id": str(_field(item, "id") or ""),
            "name": name,
            "input": input_data,
        }]

    if method in {"item/toolResult", "item/toolCallOutput"}:
        event = _tool_result_event(payload)
        return [event] if event is not None else []

    return []


def _thread_response_items(response: Any) -> list[Any]:
    for key in ("threads", "data", "items"):
        value = _field(response, key)
        if isinstance(value, list):
            return value
    return []


def _thread_updated_at(thread: Any) -> Any:
    return _field(thread, "updated_at", "updatedAt", "last_modified", "lastModified", "created_at", "createdAt")


def _thread_summary(thread: Any) -> str:
    return str(
        _field(thread, "thread_name", "threadName", "name", "title", "preview", "summary", "id")
        or ""
    )


def _collect_visible_messages(value: Any, session_id: str, out: list[dict[str, Any]]) -> None:
    value = _root(value)
    if isinstance(value, list):
        for item in value:
            _collect_visible_messages(item, session_id, out)
        return
    data = _as_dict(value)
    if not data and not hasattr(value, "__dict__"):
        return

    role = str(_field(value, "role") or "").lower()
    item_type = str(_field(value, "type") or "").lower()
    inferred_role = role
    if not inferred_role:
        if "usermessage" in item_type or item_type == "message" and _field(value, "role") == "user":
            inferred_role = "user"
        elif "agentmessage" in item_type:
            inferred_role = "assistant"

    if inferred_role in {"user", "assistant"}:
        text = _extract_text(_field(value, "text", "content", "message"))
        if text.strip():
            out.append({
                "type": inferred_role,
                "role": inferred_role,
                "uuid": _field(value, "id"),
                "session_id": session_id,
                "message": text,
            })
            return

    for key in ("thread", "turns", "items", "input", "output", "messages"):
        child = _field(value, key)
        if child is not None:
            _collect_visible_messages(child, session_id, out)


@dataclass
class CodexCliRuntime(AgentRuntime):
    id: str = field(default="codex_cli", init=False)
    label: str = field(default="Codex CLI", init=False)
    capabilities: AgentRuntimeCapabilities = field(
        default=AgentRuntimeCapabilities(
            streaming=True,
            sessions=True,
            permissions=False,
            mcp=True,
            structured_output=True,
            interrupt=True,
        ),
        init=False,
    )
    codex_path: str | None = None
    sandbox: str = "danger-full-access"
    _active_turn: Any | None = field(default=None, init=False, repr=False)
    _active_loop: asyncio.AbstractEventLoop | None = field(default=None, init=False, repr=False)
    _interrupted: bool = field(default=False, init=False, repr=False)

    def _codex_executable(self) -> str:
        if self.codex_path:
            return self.codex_path
        exe = find_codex_executable()
        if not exe:
            raise RuntimeError("Codex CLI not found. Install `codex` and ensure it is on PATH.")
        return exe

    def _env_for(self, request: AgentRunRequest | None = None) -> dict[str, str]:
        env = dict(os.environ)
        if request is not None:
            env.update(workflow_runtime_env(request.workflow_dir))
        env = codex_subprocess_env(env, codex_exe=self._codex_executable())
        no_proxy = env.get("NO_PROXY") or env.get("no_proxy") or ""
        required_no_proxy = ["127.0.0.1", "localhost", "::1"]
        existing = {part.strip() for part in no_proxy.split(",") if part.strip()}
        merged = [part for part in no_proxy.split(",") if part.strip()]
        for item in required_no_proxy:
            if item not in existing:
                merged.append(item)
        env["NO_PROXY"] = ",".join(merged)
        env["no_proxy"] = env["NO_PROXY"]
        return env

    def _config_for(self, request: AgentRunRequest | None = None, *, cwd: Path | None = None) -> Any:
        _Codex, _AsyncCodex, CodexConfig, _Sandbox = _load_codex_sdk()
        return CodexConfig(
            codex_bin=self._codex_executable(),
            cwd=str(cwd or (request.workspace_dir if request is not None else Path.cwd())),
            env=self._env_for(request),
            config_overrides=tuple(_codex_config_overrides(request)),
        )

    def _turn_input(self, prompt: str, *, skills: Any = None) -> Any:
        return _codex_turn_input(prompt, skills=skills)

    def _sandbox_value(self) -> Any:
        _Codex, _AsyncCodex, _CodexConfig, Sandbox = _load_codex_sdk()
        if self.sandbox == "read-only":
            return Sandbox.read_only
        if self.sandbox in {"danger-full-access", "full-access"}:
            return Sandbox.full_access
        return Sandbox.workspace_write

    def _thread_kwargs(self, request: AgentRunRequest) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "cwd": str(request.workspace_dir),
            "sandbox": self._sandbox_value(),
        }
        model = _codex_model(request.model)
        if model:
            kwargs["model"] = model
        return kwargs

    def _thread_start_kwargs(self, request: AgentRunRequest) -> dict[str, Any]:
        kwargs = self._thread_kwargs(request)
        if request.system_prompt and request.system_prompt.strip():
            kwargs["developer_instructions"] = request.system_prompt
        return kwargs

    def _start_or_resume_thread(self, codex: Any, request: AgentRunRequest) -> tuple[Any, bool]:
        if request.session_id:
            try:
                _log(
                    "resuming thread "
                    f"session_id={request.session_id} mode={request.mode} workspace={request.workspace_dir} model={_codex_model(request.model)}"
                )
                return codex.thread_resume(request.session_id, **self._thread_kwargs(request)), True
            except Exception as e:
                if _is_missing_thread_error(e):
                    _log(
                        "resume target missing; starting new thread "
                        f"session_id={request.session_id} mode={request.mode} workspace={request.workspace_dir}: {e}"
                    )
                else:
                    _log(
                        "resume failed; starting new thread "
                        f"session_id={request.session_id} mode={request.mode} workspace={request.workspace_dir}",
                        exc_info=True,
                    )
        _log(f"starting new thread mode={request.mode} workspace={request.workspace_dir} model={_codex_model(request.model)}")
        return codex.thread_start(**self._thread_start_kwargs(request)), False

    async def _start_or_resume_thread_async(self, codex: Any, request: AgentRunRequest) -> tuple[Any, bool]:
        if request.session_id:
            try:
                _log(
                    "resuming async thread "
                    f"session_id={request.session_id} mode={request.mode} workspace={request.workspace_dir} model={_codex_model(request.model)}"
                )
                return await codex.thread_resume(request.session_id, **self._thread_kwargs(request)), True
            except Exception as e:
                if _is_missing_thread_error(e):
                    _log(
                        "async resume target missing; starting new thread "
                        f"session_id={request.session_id} mode={request.mode} workspace={request.workspace_dir}: {e}"
                    )
                else:
                    _log(
                        "async resume failed; starting new thread "
                        f"session_id={request.session_id} mode={request.mode} workspace={request.workspace_dir}",
                        exc_info=True,
                    )
        _log(f"starting new async thread mode={request.mode} workspace={request.workspace_dir} model={_codex_model(request.model)}")
        return await codex.thread_start(**self._thread_start_kwargs(request)), False

    def run(self, request: AgentRunRequest, prompt: str) -> AgentRunResult:
        try:
            Codex, _AsyncCodex, _CodexConfig, _Sandbox = _load_codex_sdk()
            with Codex(config=self._config_for(request)) as codex:
                _install_codex_sdk_approval_handler(codex)
                _log(f"run start mode={request.mode} workspace={request.workspace_dir} session_id={request.session_id or '<new>'}")
                thread, resumed = self._start_or_resume_thread(codex, request)
                output_schema = _read_output_schema(request.schema_path)
                turn_kwargs = self._thread_kwargs(request)
                if output_schema is not None:
                    turn_kwargs["output_schema"] = output_schema
                _log(
                    f"turn start thread_id={_field(thread, 'id') or '<unknown>'} resumed={resumed} "
                    f"schema={'yes' if output_schema is not None else 'no'} prompt_chars={len(prompt)}"
                )
                turn = thread.turn(self._turn_input(prompt), **turn_kwargs)
                self._active_turn = turn
                self._interrupted = False
                result = turn.run()
                status = _enum_value(_field(result, "status")) or "success"
                if status == "completed":
                    status = "success"
                if self._interrupted:
                    status = "cancelled"
                error_obj = _field(result, "error")
                error = _extract_text(error_obj) or None
                summary = (
                    _extract_text(_field(result, "final_response", "finalResponse"))
                    or _summary_from_items(_field(result, "items") or [])
                    or "Codex completed the request."
                )
                usage = _as_dict(_field(result, "usage"))
                logs = [
                    json.dumps(
                        {
                            "event": "codex_turn",
                            "thread_id": _field(thread, "id") or request.session_id or "",
                            "turn_id": _field(result, "id") or "",
                            "status": status,
                            "duration_ms": _field(result, "duration_ms", "durationMs"),
                            "usage": usage or None,
                            "error": error,
                        },
                        ensure_ascii=False,
                    )
                ]
                _log(
                    f"turn complete thread_id={_field(thread, 'id') or '<unknown>'} status={status} "
                    f"summary_chars={len(summary)} error={error or ''}"
                )
                return AgentRunResult(
                    status=status if status in {"success", "failed", "cancelled"} else "success",  # type: ignore[arg-type]
                    session_id=str(_field(thread, "id") or request.session_id or ""),
                    summary=summary,
                    logs=logs,
                    error=error if status != "success" else None,
                )
        except Exception as e:
            _log(f"run failed mode={request.mode} workspace={request.workspace_dir}: {e}", exc_info=True)
            return AgentRunResult(
                status="cancelled" if self._interrupted else "failed",
                session_id=request.session_id or "",
                summary="Codex request failed.",
                error=str(e),
            )
        finally:
            self._active_turn = None
            self._active_loop = None

    async def stream_chat(
        self,
        request: AgentRunRequest,
        prompt: str,
        **kwargs: Any,
    ) -> AsyncIterator[AgentStreamEvent]:
        on_client = kwargs.get("on_client")
        skills = kwargs.get("skills")
        _Codex, AsyncCodex, _CodexConfig, _Sandbox = _load_codex_sdk()
        text_parts: list[str] = []
        tool_result_parts: dict[str, list[str]] = {}
        final_session_id = request.session_id or ""
        saw_text_delta = False
        try:
            async with AsyncCodex(config=self._config_for(request)) as codex:
                _install_codex_sdk_approval_handler(codex)
                _log(f"stream start mode={request.mode} workspace={request.workspace_dir} session_id={request.session_id or '<new>'}")
                thread, resumed = await self._start_or_resume_thread_async(codex, request)
                final_session_id = str(_field(thread, "id") or final_session_id)
                yield {"event": "session_started", "session_id": final_session_id}
                output_schema = _read_output_schema(request.schema_path)
                turn_kwargs = self._thread_kwargs(request)
                if output_schema is not None:
                    turn_kwargs["output_schema"] = output_schema
                _log(
                    f"stream turn start thread_id={final_session_id or '<unknown>'} resumed={resumed} "
                    f"schema={'yes' if output_schema is not None else 'no'} prompt_chars={len(prompt)}"
                )
                turn = await thread.turn(self._turn_input(prompt, skills=skills), **turn_kwargs)
                self._active_turn = turn
                self._active_loop = asyncio.get_running_loop()
                self._interrupted = False
                if callable(on_client):
                    try:
                        on_client(_AsyncCodexInterruptClient(self))
                    except Exception:
                        pass
                async for notification in turn.stream():
                    method = str(_field(notification, "method") or "")
                    if method == "item/agentMessage/delta":
                        saw_text_delta = True
                    if saw_text_delta and method == "item/completed" and _is_completed_agent_message(notification):
                        continue
                    for event in _notification_to_agent_events(notification):
                        if event.get("event") == "text":
                            text_parts.append(str(event.get("text") or ""))
                        elif event.get("event") == "tool_result" and event.get("append"):
                            tool_id = str(event.get("tool_use_id") or "")
                            if tool_id:
                                parts = tool_result_parts.setdefault(tool_id, [])
                                parts.append(str(event.get("content") or ""))
                                event = {
                                    key: value for key, value in event.items()
                                    if key != "append"
                                }
                                event["content"] = "".join(parts)
                        yield event
                _log(
                    f"stream complete thread_id={final_session_id or '<unknown>'} "
                    f"status={'cancelled' if self._interrupted else 'success'} summary_chars={len(chr(10).join(text_parts).strip())}"
                )
                yield {
                    "event": "done",
                    "session_id": final_session_id,
                    "status": "cancelled" if self._interrupted else "success",
                    "error": "interrupted" if self._interrupted else None,
                    "summary": "\n".join(text_parts).strip(),
                }
        except Exception as e:
            status = "cancelled" if self._interrupted else "failed"
            if self._interrupted:
                yield {"event": "interrupted"}
            else:
                yield {"event": "error", "message": str(e)}
            _log(f"stream failed mode={request.mode} workspace={request.workspace_dir}: {e}", exc_info=True)
            yield {
                "event": "done",
                "session_id": final_session_id,
                "status": status,
                "error": "interrupted" if self._interrupted else str(e),
                "summary": "\n".join(text_parts).strip(),
            }
        finally:
            self._active_turn = None
            self._active_loop = None

    def list_sessions(self, directory: Path) -> list[dict[str, Any]]:
        try:
            Codex, _AsyncCodex, _CodexConfig, _Sandbox = _load_codex_sdk()
            with Codex(config=self._config_for(cwd=directory)) as codex:
                response = codex.thread_list(cwd=str(directory.resolve()))
        except Exception:
            return []
        rows: list[dict[str, Any]] = []
        for thread in _thread_response_items(response):
            session_id = _field(thread, "id")
            if not session_id:
                continue
            updated_at = _thread_updated_at(thread)
            rows.append({
                "session_id": str(session_id),
                "summary": _thread_summary(thread) or str(session_id),
                "updated_at": updated_at,
                "last_modified": updated_at,
                "source": "codex",
            })
        return sorted(rows, key=lambda item: str(item.get("updated_at") or item.get("last_modified") or ""), reverse=True)

    def load_messages(self, session_id: str, directory: Path) -> list[dict[str, Any]]:
        try:
            Codex, _AsyncCodex, _CodexConfig, _Sandbox = _load_codex_sdk()
            with Codex(config=self._config_for(cwd=directory)) as codex:
                thread = codex.thread_resume(session_id, cwd=str(directory), sandbox=self._sandbox_value())
                response = thread.read(include_turns=True)
        except Exception:
            return []
        messages: list[dict[str, Any]] = []
        _collect_visible_messages(response, session_id, messages)
        return messages

    def interrupt(self) -> bool:
        turn = self._active_turn
        if turn is None:
            return False
        self._interrupted = True
        try:
            result = turn.interrupt()
            if hasattr(result, "__await__"):
                loop = self._active_loop
                if loop is None or not loop.is_running():
                    return False
                asyncio.run_coroutine_threadsafe(result, loop)
            return True
        except Exception:
            return False
