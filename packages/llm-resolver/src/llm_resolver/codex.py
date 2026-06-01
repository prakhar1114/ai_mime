from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
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


def _codex_exe() -> str:
    exe = shutil.which("codex")
    if not exe:
        raise RuntimeError("Codex CLI not found. Install `codex` and ensure it is on PATH.")
    return exe


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


def run_codex_structured(
    *,
    messages: list[dict[str, Any]],
    response_schema: dict[str, Any],
    where: str,
    model: str | None = None,
    timeout: float = 300.0,
) -> Any:
    """Run Codex CLI for schema-constrained JSON.

    This intentionally does not require OPENAI_API_KEY. Codex may be authenticated
    through its own CLI login flow; auth failures are surfaced from stderr.
    """
    with tempfile.TemporaryDirectory(prefix="llm-resolver-codex-") as td:
        tmp_dir = Path(td)
        schema_path = tmp_dir / "schema.json"
        output_path = tmp_dir / "last-message.json"
        schema_path.write_text(json.dumps(response_schema), encoding="utf-8")
        prompt, images = _messages_to_codex_prompt(messages, tmp_dir)

        cmd = [
            _codex_exe(),
            "exec",
            "--json",
            "--cd",
            str(tmp_dir),
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--output-schema",
            str(schema_path),
            "-o",
            str(output_path),
        ]
        if model:
            cmd.extend(["-m", model.split("/", 1)[1] if model.startswith("openai/") else model])
        for image in images:
            cmd.extend(["-i", str(image)])
        cmd.append(prompt)

        proc = subprocess.run(
            cmd,
            cwd=str(tmp_dir),
            env=dict(os.environ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"{where}: Codex failed with exit code {proc.returncode}: {detail}")
        if output_path.exists():
            return _parse_json_output(output_path.read_text(encoding="utf-8"), where=where)
        # Fallback for older Codex versions where -o may not write despite success.
        for line in reversed(proc.stdout.splitlines()):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                msg = obj.get("msg") if isinstance(obj.get("msg"), dict) else obj
                for key in ("result", "summary", "content", "message", "final_response"):
                    if isinstance(msg.get(key), str):
                        return _parse_json_output(str(msg[key]), where=where)
        return _parse_json_output(proc.stdout, where=where)
