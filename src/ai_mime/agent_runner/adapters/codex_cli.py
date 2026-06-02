from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Iterable

from ai_mime.agent_runner.adapters.base import AgentRuntime, AgentRuntimeCapabilities, AgentStreamEvent
from ai_mime.agent_runner.models import AgentRunRequest, AgentRunResult
from ai_mime.app_data import workflow_runtime_env
from ai_mime.codex_support import codex_subprocess_env, find_codex_executable

_CODEX_STDOUT_LIMIT = 128 * 1024 * 1024


def _extract_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    if isinstance(value, dict):
        text = value.get("text") or value.get("content") or value.get("message")
        return text if isinstance(text, str) else ""
    return str(value)


def codex_json_event_to_agent_event(obj: dict[str, Any]) -> AgentStreamEvent | None:
    """Best-effort normalization for Codex CLI JSONL events.

    Codex CLI has changed event envelopes over time. Keep this adapter tolerant:
    inspect both the top-level object and a nested `msg` object, then map common
    text/tool/done/error shapes into the existing AI Mime frontend events.
    """
    msg = obj.get("msg") if isinstance(obj.get("msg"), dict) else obj
    event_type = str(msg.get("type") or obj.get("type") or "").strip()
    event_type_l = event_type.lower()

    if event_type_l == "thread.started":
        session_id = msg.get("thread_id") or obj.get("thread_id")
        return {"event": "session_started", "session_id": str(session_id or "")}

    if event_type_l in {"error", "turn_error", "exec_error"} or msg.get("error"):
        message = _extract_text(msg.get("message") or msg.get("error") or msg.get("content"))
        return {"event": "error", "message": message or "Codex CLI reported an error."}

    if event_type_l in {"item.completed", "item.started"} and isinstance(msg.get("item"), dict):
        item = msg["item"]
        item_type = str(item.get("type") or "").lower()
        if item_type == "agent_message":
            text = _extract_text(item.get("text") or item.get("content"))
            return {"event": "text", "text": text} if text else None
        if "tool" in item_type or "command" in item_type:
            if event_type_l == "item.completed":
                return None
            name = str(item.get("name") or item.get("tool") or item.get("tool_name") or item.get("command") or item_type or "tool")
            input_data = item.get("input") or item.get("arguments") or {}
            if not isinstance(input_data, dict):
                input_data = {"value": input_data}
            if item.get("command") and "command" not in input_data:
                input_data["command"] = item.get("command")
            if item.get("server") and "server" not in input_data:
                input_data["server"] = item.get("server")
            return {
                "event": "tool_use",
                "id": str(item.get("id") or ""),
                "name": name,
                "input": input_data,
            }

    if event_type_l in {"text", "agent_message", "assistant_message", "response.output_text.delta"}:
        text = _extract_text(msg.get("text") or msg.get("content") or msg.get("message") or msg.get("delta"))
        return {"event": "text", "text": text} if text else None

    if event_type_l in {"tool_use", "tool_call", "function_call", "exec_command_begin", "exec_command"}:
        name = str(msg.get("name") or msg.get("tool_name") or msg.get("command") or "tool")
        input_data = msg.get("input") or msg.get("arguments") or {}
        if not isinstance(input_data, dict):
            input_data = {"value": input_data}
        if msg.get("command") and "command" not in input_data:
            input_data["command"] = msg.get("command")
        return {
            "event": "tool_use",
            "id": str(msg.get("id") or msg.get("tool_call_id") or ""),
            "name": name,
            "input": input_data,
        }

    if event_type_l in {"tool_result", "function_call_output", "exec_command_end", "exec_result"}:
        return {
            "event": "tool_result",
            "tool_use_id": str(msg.get("tool_use_id") or msg.get("tool_call_id") or msg.get("id") or ""),
            "content": msg.get("content") if "content" in msg else msg.get("output"),
            "is_error": bool(msg.get("is_error") or msg.get("error")),
        }

    if event_type_l == "turn.completed":
        return None

    if event_type_l in {"done", "turn_complete", "task_complete", "completed", "result"}:
        status = "failed" if msg.get("is_error") or msg.get("error") else "success"
        summary = _extract_text(msg.get("summary") or msg.get("result") or msg.get("final_response") or msg.get("content"))
        return {
            "event": "done",
            "session_id": str(msg.get("session_id") or msg.get("conversation_id") or msg.get("thread_id") or ""),
            "status": status,
            "error": _extract_text(msg.get("error")) or None,
            "summary": summary,
        }

    # Some JSONL records are plain assistant message envelopes.
    role = msg.get("role")
    if role == "assistant":
        text = _extract_text(msg.get("content") or msg.get("message"))
        return {"event": "text", "text": text} if text else None

    return None


def parse_codex_jsonl(lines: Iterable[str]) -> list[AgentStreamEvent]:
    events: list[AgentStreamEvent] = []
    for line in lines:
        text = line.strip()
        if not text:
            continue
        if '"type":"item.completed"' in text[:80] and '"mcp_tool_call"' in text[:256]:
            continue
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        event = codex_json_event_to_agent_event(obj)
        if event is not None:
            events.append(event)
    return events


def _codex_home() -> Path:
    raw = os.environ.get("CODEX_HOME")
    if raw and raw.strip():
        return Path(raw).expanduser()
    return Path.home() / ".codex"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                text = line.strip()
                if not text:
                    continue
                try:
                    obj = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except OSError:
        return []
    return out


def _find_codex_session_file(session_id: str) -> Path | None:
    if not session_id:
        return None
    sessions_dir = _codex_home() / "sessions"
    if not sessions_dir.is_dir():
        return None
    try:
        for path in sessions_dir.rglob("*.jsonl"):
            if session_id in path.name:
                return path
    except OSError:
        return None

    try:
        for path in sessions_dir.rglob("*.jsonl"):
            for obj in _read_jsonl(path):
                if obj.get("type") != "session_meta":
                    continue
                payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
                if payload.get("id") == session_id:
                    return path
    except OSError:
        return None
    return None


def _codex_session_cwd(session_id: str) -> str | None:
    path = _find_codex_session_file(session_id)
    if path is None:
        return None
    for obj in _read_jsonl(path):
        if obj.get("type") != "session_meta":
            continue
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        cwd = payload.get("cwd")
        return cwd if isinstance(cwd, str) and cwd else None
    return None


def _codex_content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("input_text") or item.get("output_text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(part for part in parts if part)
    if isinstance(content, dict):
        text = content.get("text") or content.get("input_text") or content.get("output_text") or content.get("content")
        return text if isinstance(text, str) else ""
    return ""


def _codex_message_text(payload: dict[str, Any]) -> str:
    return _codex_content_text(payload.get("content") or payload.get("message"))


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
        if server_type == "http":
            url = server.get("url")
            if not isinstance(url, str) or not url.strip():
                raise RuntimeError(f"Codex MCP server {name!r} requires a non-empty url.")
            overrides.extend([
                f"{prefix}.url={_toml_literal(url)}",
                f"{prefix}.required=true",
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
                f"{prefix}.required=true",
                f"{prefix}.default_tools_approval_mode={_toml_literal('approve')}",
            ])
            continue

        raise RuntimeError(f"Unsupported Codex MCP server {name!r} type: {server_type!r}.")
    return overrides


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
    sandbox: str = "workspace-write"
    _active_process: subprocess.Popen[str] | asyncio.subprocess.Process | None = field(default=None, init=False, repr=False)
    _interrupted: bool = field(default=False, init=False, repr=False)

    def _codex_executable(self) -> str:
        if self.codex_path:
            return self.codex_path
        exe = find_codex_executable()
        if not exe:
            raise RuntimeError("Codex CLI not found. Install `codex` and ensure it is on PATH.")
        return exe

    def _env_for(self, request: AgentRunRequest) -> dict[str, str]:
        env = dict(os.environ)
        env.update(workflow_runtime_env(request.workflow_dir))
        return codex_subprocess_env(env, codex_exe=self._codex_executable())

    def build_command(
        self,
        request: AgentRunRequest,
        _prompt: str,
        *,
        output_schema_path: Path | None = None,
        output_last_message_path: Path | None = None,
    ) -> list[str]:
        exe = self._codex_executable()
        if request.session_id:
            cmd = [exe, "exec", "resume", request.session_id, "--json"]
        else:
            cmd = [
                exe,
                "exec",
                "--json",
                "--cd",
                str(request.workspace_dir),
                "--sandbox",
                self.sandbox,
                "--skip-git-repo-check",
            ]
        for override in _codex_mcp_config_overrides(request.mcp_servers):
            cmd.extend(["-c", override])
        if request.model:
            cmd.extend(["-m", request.model])
        if output_schema_path is not None:
            cmd.extend(["--output-schema", str(output_schema_path)])
        if output_last_message_path is not None:
            cmd.extend(["-o", str(output_last_message_path)])
        cmd.append("-")
        return cmd

    def run(self, request: AgentRunRequest, prompt: str) -> AgentRunResult:
        try:
            env = self._env_for(request)
            with tempfile.TemporaryDirectory(prefix="ai-mime-codex-") as td:
                output_path = Path(td) / "last_message.txt"
                cmd = self.build_command(request, prompt, output_last_message_path=output_path)
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(request.workspace_dir),
                    env=env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                self._active_process = proc
                self._interrupted = False
                stdout, stderr = proc.communicate(prompt)
                events = parse_codex_jsonl(stdout.splitlines())
                text_parts = [str(e.get("text") or "") for e in events if e.get("event") == "text"]
                session_events = [e for e in events if e.get("event") == "session_started"]
                done_events = [e for e in events if e.get("event") == "done"]
                last_done = done_events[-1] if done_events else {}
                last_session = session_events[-1] if session_events else {}
                summary = "\n".join(part for part in text_parts if part).strip()
                if output_path.exists():
                    summary = output_path.read_text(encoding="utf-8").strip() or summary
                summary = summary or str(last_done.get("summary") or "").strip() or "Codex completed the request."
                status = "success" if proc.returncode == 0 and not any(e.get("event") == "error" for e in events) else "failed"
                if self._interrupted:
                    status = "cancelled"
                error = None if status == "success" else (stderr.strip() or str(last_done.get("error") or "Codex CLI request failed."))
                session_id = str(last_done.get("session_id") or last_session.get("session_id") or request.session_id or "")
                return AgentRunResult(
                    status=status,  # type: ignore[arg-type]
                    session_id=session_id,
                    summary=summary,
                    logs=[json.dumps(e, ensure_ascii=False) for e in events],
                    error=error,
                )
        except Exception as e:
            return AgentRunResult(
                status="failed",
                session_id=request.session_id or "",
                summary="Codex CLI request failed.",
                error=str(e),
            )
        finally:
            self._active_process = None

    async def stream_chat(
        self,
        request: AgentRunRequest,
        prompt: str,
        **_kwargs: Any,
    ) -> AsyncIterator[AgentStreamEvent]:
        env = self._env_for(request)
        cmd = self.build_command(request, prompt)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(request.workspace_dir),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_CODEX_STDOUT_LIMIT,
        )
        self._active_process = proc
        self._interrupted = False
        assert proc.stdin is not None
        proc.stdin.write(prompt.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()
        assert proc.stdout is not None
        text_parts: list[str] = []
        final_session_id = request.session_id or ""
        try:
            async for raw in proc.stdout:
                try:
                    line = raw.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                events = parse_codex_jsonl([line])
                for event in events:
                    if event.get("event") == "text":
                        text_parts.append(str(event.get("text") or ""))
                    if event.get("event") == "session_started" and event.get("session_id"):
                        final_session_id = str(event.get("session_id") or final_session_id)
                    if event.get("event") == "done" and event.get("session_id"):
                        final_session_id = str(event.get("session_id") or final_session_id)
                    yield event
            stderr = ""
            if proc.stderr is not None:
                stderr_bytes = await proc.stderr.read()
                stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
            rc = await proc.wait()
            if self._interrupted:
                yield {"event": "interrupted"}
                yield {
                    "event": "done",
                    "session_id": final_session_id,
                    "status": "cancelled",
                    "error": "interrupted",
                    "summary": "\n".join(text_parts).strip(),
                }
            elif rc != 0:
                yield {"event": "error", "message": stderr or f"Codex CLI exited {rc}."}
                yield {
                    "event": "done",
                    "session_id": final_session_id,
                    "status": "failed",
                    "error": stderr or f"Codex CLI exited {rc}.",
                    "summary": "\n".join(text_parts).strip(),
                }
            else:
                yield {
                    "event": "done",
                    "session_id": final_session_id,
                    "status": "success",
                    "error": None,
                    "summary": "\n".join(text_parts).strip(),
                }
        finally:
            self._active_process = None

    def list_sessions(self, _directory: Path) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        directory = _directory.resolve()
        for obj in _read_jsonl(_codex_home() / "session_index.jsonl"):
            session_id = obj.get("id")
            if not isinstance(session_id, str) or not session_id:
                continue
            cwd = _codex_session_cwd(session_id)
            if cwd is not None:
                try:
                    if Path(cwd).expanduser().resolve() != directory:
                        continue
                except OSError:
                    continue
            summary = obj.get("thread_name") if isinstance(obj.get("thread_name"), str) else None
            updated_at = obj.get("updated_at") if isinstance(obj.get("updated_at"), str) else None
            rows.append({
                "session_id": session_id,
                "summary": summary or session_id,
                "updated_at": updated_at,
                "last_modified": updated_at,
                "source": "codex",
            })
        return sorted(rows, key=lambda item: str(item.get("updated_at") or item.get("last_modified") or ""), reverse=True)

    def load_messages(self, _session_id: str, _directory: Path) -> list[dict[str, Any]]:
        path = _find_codex_session_file(_session_id)
        if path is None:
            return []
        messages: list[dict[str, Any]] = []
        for obj in _read_jsonl(path):
            if obj.get("type") != "response_item":
                continue
            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
            if payload.get("type") != "message":
                continue
            role = payload.get("role")
            if role not in {"user", "assistant"}:
                continue
            text = _codex_message_text(payload)
            if not text.strip():
                continue
            messages.append({
                "type": role,
                "role": role,
                "uuid": payload.get("id") if isinstance(payload.get("id"), str) else None,
                "session_id": _session_id,
                "message": text,
            })
        return messages

    def interrupt(self) -> bool:
        proc = self._active_process
        if proc is None or proc.returncode is not None:
            return False
        self._interrupted = True
        try:
            proc.send_signal(signal.SIGINT)
        except ProcessLookupError:
            return False
        except Exception:
            try:
                proc.terminate()
            except Exception:
                return False
        return True
