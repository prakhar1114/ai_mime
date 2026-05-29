from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from litellm import completion  # type: ignore[import-not-found]
from openai import OpenAI  # type: ignore[import-not-found]

from .client import LiteLLMChatClient
from .config import get_llm_section

_IMAGE_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def _data_url(path: str | Path) -> str:
    image_path = Path(path)
    ext = image_path.suffix.lower()
    mime = _IMAGE_MIME.get(ext)
    if not mime:
        raise RuntimeError(f"ask_llm: unsupported image extension {ext!r} for {image_path}")
    data = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


def _messages(prompt: str, images: list[str] | None) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for image in images or []:
        content.append({"type": "image_url", "image_url": {"url": _data_url(image)}})
    return [{"role": "user", "content": content}]


def _extract_json_text(resp: Any) -> Any:
    text = LiteLLMChatClient._extract_text_from_litellm_response(resp)
    if not text:
        raise RuntimeError(f"ask_llm: no text in LLM response: {str(resp)[:500]}")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"ask_llm: response not valid JSON: {text[:500]}") from e


def ask_llm(
    prompt: str,
    schema: dict[str, Any],
    images: list[str] | None = None,
    model: str | None = None,
    thinking: str | None = None,
    timeout: float = 30.0,
    section: str = "runtime",
) -> object:
    """Ask the configured runtime LLM for schema-constrained JSON."""
    cfg = get_llm_section(section)
    selected_model = (model or cfg.model).strip()
    provider = selected_model.split("/", 1)[0] if "/" in selected_model else "openai"
    extra_kwargs = dict(cfg.extra_kwargs)
    if thinking is not None:
        extra_kwargs.setdefault("reasoning", {"effort": thinking})
    response_format = LiteLLMChatClient.response_format_for_schema(schema)
    messages = _messages(prompt, images)
    key = None
    if cfg.api_key_env:
        import os

        value = os.getenv(cfg.api_key_env)
        if value is None or not value.strip():
            raise RuntimeError(f"Missing API key env var {cfg.api_key_env!r} for model={selected_model!r}.")
        key = value.strip()

    try:
        if provider in {"openai", "gemini"}:
            client = OpenAI(api_key=key, base_url=cfg.api_base, timeout=timeout)
            resp = client.chat.completions.create(
                model=LiteLLMChatClient._strip_provider_prefix(selected_model),
                messages=messages,  # type: ignore[arg-type]
                response_format=response_format,
                **extra_kwargs,
            )
            text = resp.choices[0].message.content or ""
            if not text.strip():
                raise RuntimeError(f"ask_llm: no text in LLM response: {resp}")
            return json.loads(text)

        resp = completion(
            model=selected_model,
            messages=messages,
            response_format=response_format,
            api_base=cfg.api_base,
            api_key=key,
            timeout=timeout,
            drop_params=True,
            **extra_kwargs,
        )
        return _extract_json_text(resp)
    except json.JSONDecodeError as e:
        raise RuntimeError("ask_llm: response not valid JSON") from e
    except Exception as e:
        raise RuntimeError(f"ask_llm: LLM request failed: {e}") from e
