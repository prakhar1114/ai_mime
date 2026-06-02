from __future__ import annotations

import base64
import asyncio
import concurrent.futures
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

_DATA_URL_RE = re.compile(r"^data:(?P<mime>[^;]+);base64,(?P<data>.+)$", re.DOTALL)
_MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
_HOST_CLI_DIRS = (
    ".local/bin",
    "bin",
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
)


def _home_from_env(env: dict[str, str] | None = None) -> Path:
    raw = (env or os.environ).get("HOME")
    if raw:
        return Path(raw).expanduser()
    return Path.home()


def _candidate_dirs(home: Path) -> list[str]:
    dirs: list[str] = []
    for raw in _HOST_CLI_DIRS:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = home / candidate
        dirs.append(str(candidate))
    return dirs


def _merge_path(*groups: list[str]) -> str:
    seen: set[str] = set()
    merged: list[str] = []
    for group in groups:
        for item in group:
            if not item or item in seen:
                continue
            seen.add(item)
            merged.append(item)
    return os.pathsep.join(merged)


def _codex_env(base_env: dict[str, str] | None = None, *, codex_exe: str | None = None) -> dict[str, str]:
    env = dict(base_env or os.environ)
    home = _home_from_env(env)
    env.setdefault("HOME", str(home))
    codex_home = home / ".codex"
    if "CODEX_HOME" not in env and codex_home.exists():
        env["CODEX_HOME"] = str(codex_home)

    exe_dirs: list[str] = []
    if codex_exe:
        exe_dirs.append(str(Path(codex_exe).expanduser().parent))
    env["PATH"] = _merge_path(
        (env.get("PATH") or "").split(os.pathsep),
        exe_dirs,
        _candidate_dirs(home),
    )
    return env


def _codex_exe() -> str:
    home = _home_from_env()
    search_path = _merge_path(
        (os.environ.get("PATH") or "").split(os.pathsep),
        _candidate_dirs(home),
    )
    exe = shutil.which("codex", path=search_path)
    if not exe:
        raise RuntimeError("Codex CLI not found. Install `codex` and ensure it is on PATH.")
    return exe


def _load_codex_sdk() -> tuple[Any, Any, Any, Any, Any]:
    from openai_codex import AsyncCodex, CodexConfig, LocalImageInput, Sandbox, TextInput  # type: ignore[import-not-found]

    return AsyncCodex, CodexConfig, LocalImageInput, Sandbox, TextInput


def _messages_to_codex_prompt(messages: list[dict[str, Any]], tmp_dir: Path) -> tuple[str, list[Path]]:
    prompt_parts: list[str] = []
    images: list[Path] = []
    for message in messages:
        role = str(message.get("role") or "user")
        content = message.get("content")
        text_parts: list[str] = []
        if isinstance(content, str):
            text_parts.append(content)
        elif isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                item_type = item.get("type")
                if item_type == "text":
                    text_parts.append(str(item.get("text") or ""))
                elif item_type == "image_url":
                    image_url = item.get("image_url")
                    url = image_url.get("url") if isinstance(image_url, dict) else None
                    if not isinstance(url, str) or not url:
                        continue
                    match = _DATA_URL_RE.match(url)
                    if not match:
                        text_parts.append(f"[Image URL omitted from Codex run: {url[:120]}]")
                        continue
                    mime = match.group("mime")
                    ext = _MIME_EXT.get(mime)
                    if ext is None:
                        text_parts.append(f"[Unsupported image MIME omitted from Codex run: {mime}]")
                        continue
                    image_path = tmp_dir / f"image-{len(images)}{ext}"
                    image_path.write_bytes(base64.b64decode(match.group("data")))
                    images.append(image_path)
        elif content is not None:
            text_parts.append(str(content))

        text = "\n".join(part for part in text_parts if part).strip()
        if not text:
            continue
        prompt_parts.append(text if role == "user" else f"{role}: {text}")
    prompt = "\n\n".join(prompt_parts).strip() or "Return JSON matching the requested schema."
    return prompt, images


def _parse_json_output(text: str, *, where: str) -> Any:
    value = text.strip()
    if not value:
        raise RuntimeError(f"{where}: Codex produced no final output.")
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        start, end = value.find("{"), value.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(value[start : end + 1])
            except json.JSONDecodeError:
                pass
        raise RuntimeError(f"{where}: Codex output was not valid JSON: {value[:500]}")


def _codex_model(model: str | None) -> str | None:
    if not model:
        return None
    text = model.strip()
    if text.startswith("openai/"):
        return text.split("/", 1)[1].strip() or None
    return text or None


def _codex_output_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Codex/OpenAI structured outputs require strict object schemas."""
    normalized = json.loads(json.dumps(schema))

    def resolve_ref(ref: str) -> dict[str, Any] | None:
        if not ref.startswith("#/"):
            return None
        current: Any = normalized
        for raw_part in ref[2:].split("/"):
            part = raw_part.replace("~1", "/").replace("~0", "~")
            if not isinstance(current, dict) or part not in current:
                return None
            current = current[part]
        return json.loads(json.dumps(current)) if isinstance(current, dict) else None

    def visit(node: Any) -> None:
        if not isinstance(node, dict):
            return
        ref = node.get("$ref")
        if isinstance(ref, str) and len(node) > 1:
            resolved = resolve_ref(ref)
            if resolved is not None:
                merged = {**resolved, **node}
                merged.pop("$ref", None)
                node.clear()
                node.update(merged)
        if node.get("default") is None:
            node.pop("default", None)
        node_type = node.get("type")
        if node_type == "object" or "properties" in node:
            node.setdefault("additionalProperties", False)
            properties = node.get("properties")
            if isinstance(properties, dict):
                node["required"] = list(properties.keys())
        for child in node.get("properties", {}).values():
            visit(child)
        if "items" in node:
            visit(node["items"])
        for key in ("anyOf", "oneOf", "allOf"):
            for child in node.get(key, []):
                visit(child)
        for key in ("$defs", "definitions"):
            for child in node.get(key, {}).values():
                visit(child)

    visit(normalized)
    return normalized


async def _run_codex_structured_async(
    *,
    messages: list[dict[str, Any]],
    response_schema: dict[str, Any],
    where: str,
    model: str | None,
    timeout: float,
) -> Any:
    with tempfile.TemporaryDirectory(prefix="llm-resolver-codex-") as td:
        tmp_dir = Path(td)
        prompt, images = _messages_to_codex_prompt(messages, tmp_dir)
        AsyncCodex, CodexConfig, LocalImageInput, Sandbox, TextInput = _load_codex_sdk()
        codex_exe = _codex_exe()
        config = CodexConfig(
            codex_bin=codex_exe,
            cwd=str(tmp_dir),
            env=_codex_env(codex_exe=codex_exe),
        )
        run_input: list[Any] = [TextInput(prompt)]
        run_input.extend(LocalImageInput(str(image)) for image in images)
        model_name = _codex_model(model)

        async with AsyncCodex(config=config) as codex:
            thread_kwargs: dict[str, Any] = {
                "cwd": str(tmp_dir),
                "sandbox": Sandbox.read_only,
            }
            if model_name:
                thread_kwargs["model"] = model_name
            thread = await codex.thread_start(**thread_kwargs)
            result = await asyncio.wait_for(
                thread.run(
                    run_input,
                    sandbox=Sandbox.read_only,
                    output_schema=_codex_output_schema(response_schema),
                    model=model_name,
                ),
                timeout=timeout,
            )
        final_response = getattr(result, "final_response", None)
        return _parse_json_output(str(final_response or ""), where=where)


def _run_async_from_sync(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def run_codex_structured(
    *,
    messages: list[dict[str, Any]],
    response_schema: dict[str, Any],
    where: str,
    model: str | None = None,
    timeout: float = 300.0,
) -> Any:
    """Run Codex SDK for schema-constrained JSON.

    This intentionally does not require OPENAI_API_KEY. Codex may be authenticated
    through its own login flow; auth failures are surfaced from the SDK runtime.
    """
    try:
        return _run_async_from_sync(
            _run_codex_structured_async(
                messages=messages,
                response_schema=response_schema,
                where=where,
                model=model,
                timeout=timeout,
            )
        )
    except asyncio.TimeoutError as e:
        raise RuntimeError(f"{where}: Codex timed out after {timeout:g}s.") from e
