from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, AsyncIterator, Literal, Sequence

_STRUCTURED_FALLBACK_MODEL = "claude-sonnet-4-6"
_STRUCTURED_FALLBACK_EFFORT = "medium"
_IMAGE_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def query(*args: Any, **kwargs: Any) -> Any:
    from claude_agent_sdk import query as _query

    return _query(*args, **kwargs)


def _log_claude_sdk_stderr(data: str) -> None:
    text = str(data or "")
    if not text:
        return
    for line in text.splitlines() or [text]:
        print(f"[llm-resolver claude-sdk stderr] {line}", file=sys.stderr, flush=True)


def _find_claude_exe() -> str | None:
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


def _json_from_text(text: str) -> Any | None:
    if not text:
        return None
    candidates: list[str] = [text.strip()]
    fence = text.rsplit("```json", 1)
    if len(fence) == 2:
        candidates.append(fence[1].split("```", 1)[0].strip())
    parts = text.split("```")
    if len(parts) >= 3:
        candidates.append(parts[-2].strip())
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1].strip())
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except (TypeError, ValueError):
            continue
    return None


def _image_block(image: str | Path, *, error_prefix: str = "ask_llm") -> dict[str, Any]:
    text = str(image)
    if text.startswith("data:"):
        header, sep, data = text.partition(",")
        marker = ";base64"
        if not sep or marker not in header:
            raise RuntimeError(f"{error_prefix}: unsupported image data URL")
        media_type = header.removeprefix("data:").split(marker, 1)[0]
        if not media_type:
            raise RuntimeError(f"{error_prefix}: unsupported image data URL")
        return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}

    image_path = Path(image)
    ext = image_path.suffix.lower()
    mime = _IMAGE_MIME.get(ext)
    if not mime:
        raise RuntimeError(f"{error_prefix}: unsupported image extension {ext!r} for {image_path}")
    data = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}}


async def _run_claude_agent_sdk_structured_async(
    *,
    prompt: str,
    response_schema: dict[str, Any],
    images: Sequence[str | Path] | None = None,
    system_prompt: str | None = None,
    model: str = _STRUCTURED_FALLBACK_MODEL,
    effort: Literal["low", "medium", "high", "xhigh", "max"] = _STRUCTURED_FALLBACK_EFFORT,
    cwd: str | Path | None = None,
    error_prefix: str = "ask_llm",
) -> Any:
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock

    kwargs: dict[str, Any] = {
        "model": model,
        "tools": [],
        "allowed_tools": [],
        "output_format": {"type": "json_schema", "schema": response_schema},
        "effort": effort,
        "max_buffer_size": 20 * 1024 * 1024,
        "cwd": str(cwd or Path.cwd()),
        "stderr": _log_claude_sdk_stderr,
    }
    claude_path = _find_claude_exe()
    if claude_path:
        kwargs["cli_path"] = claude_path
    if system_prompt:
        kwargs["system_prompt"] = system_prompt
    options = ClaudeAgentOptions(**kwargs)

    image_items = [_image_block(img, error_prefix=error_prefix) for img in images or []]
    if image_items:
        content = [{"type": "text", "text": prompt}, *image_items]

        async def _prompt_stream():
            yield {"type": "user", "message": {"role": "user", "content": content}}

        prompt_arg: str | AsyncIterator[dict[str, Any]] = _prompt_stream()
    else:
        prompt_arg = prompt

    assistant_text: list[str] = []
    result_text: str | None = None
    async for message in query(prompt=prompt_arg, options=options):
        if isinstance(message, ResultMessage):
            if getattr(message, "is_error", False):
                err = getattr(message, "result", None) or getattr(message, "subtype", None) or "unknown error"
                raise RuntimeError(f"{error_prefix}: Claude Agent SDK request failed: {err}")
            structured = getattr(message, "structured_output", None)
            if structured is not None:
                return structured
            result = getattr(message, "result", None)
            if isinstance(result, str) and result.strip():
                result_text = result.strip()
                parsed = _json_from_text(result_text)
                if parsed is not None:
                    return parsed
        elif isinstance(message, AssistantMessage):
            for block in message.content or []:
                if isinstance(block, TextBlock):
                    text = str(getattr(block, "text", "") or "")
                    if text:
                        assistant_text.append(text)
                elif hasattr(block, "text"):
                    text = str(getattr(block, "text", "") or "")
                    if text:
                        assistant_text.append(text)

    text = "\n".join(assistant_text).strip() or (result_text or "")
    parsed = _json_from_text(text)
    if parsed is not None:
        return parsed
    if not text:
        raise RuntimeError(f"{error_prefix}: Claude Agent SDK response had no structured_output")
    raise RuntimeError(f"{error_prefix}: Claude Agent SDK response not valid JSON: {text[:500]}")


def _run_async_safely(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coro)).result()


def run_claude_agent_sdk_structured(
    *,
    prompt: str,
    response_schema: dict[str, Any],
    images: Sequence[str | Path] | None = None,
    system_prompt: str | None = None,
    model: str = _STRUCTURED_FALLBACK_MODEL,
    effort: Literal["low", "medium", "high", "xhigh", "max"] = _STRUCTURED_FALLBACK_EFFORT,
    cwd: str | Path | None = None,
    error_prefix: str = "ask_llm",
) -> Any:
    return _run_async_safely(
        _run_claude_agent_sdk_structured_async(
            prompt=prompt,
            response_schema=response_schema,
            images=images,
            system_prompt=system_prompt,
            model=model,
            effort=effort,
            cwd=cwd,
            error_prefix=error_prefix,
        )
    )
