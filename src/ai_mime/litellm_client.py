from __future__ import annotations

import json
from typing import Any, cast

from openai import OpenAI  # type: ignore[import-not-found]
from litellm import completion  # type: ignore[import-not-found]
from pydantic import BaseModel


class LiteLLMChatClient:
    """
    Single LLM client used across the repo.

    Routing rules:
    - If model provider prefix is "openai/" -> use OpenAI Python SDK (supports OpenAI and OpenAI-compatible endpoints via api_base).
    - Otherwise -> use LiteLLM directly.

    No Instructor is used here.
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        api_base: str | None = None,
        api_key: str | None = None,
        extra_kwargs: dict[str, Any] | None = None,
        max_retries: int = 2,
    ) -> None:
        self._model = model
        self._provider = (model.split("/", 1)[0] if isinstance(model, str) and "/" in model else "openai") if model else None
        self._api_base_raw = api_base
        self._api_base = self._normalize_api_base(api_base)
        self._api_key = api_key
        self._extra_kwargs = dict(extra_kwargs or {})
        self._max_retries = int(max_retries)

        # OpenAI reasoning controls are a Responses API feature (not Chat Completions).
        # Never forward these into chat.completions.create().
        self._reasoning: Any | None = self._extra_kwargs.pop("reasoning", None)

    def _resolve(
        self,
        *,
        model: str | None = None,
        api_base: str | None = None,
        api_key: str | None = None,
        extra_kwargs: dict[str, Any] | None = None,
    ) -> tuple[str, str, str | None, str | None, dict[str, Any], Any | None]:
        m = model or self._model
        if not isinstance(m, str) or not m.strip():
            raise TypeError("LiteLLMChatClient requires model (either at __init__ or per-call).")
        m = m.strip()
        provider = m.split("/", 1)[0] if "/" in m else "openai"
        base = self._normalize_api_base(api_base if api_base is not None else self._api_base_raw)
        key = api_key if api_key is not None else self._api_key

        merged: dict[str, Any] = dict(self._extra_kwargs)
        if extra_kwargs:
            merged.update(extra_kwargs)
        reasoning = merged.pop("reasoning", None) if "reasoning" in merged else self._reasoning
        return m, provider, base, key, merged, reasoning

    @staticmethod
    def _normalize_api_base(api_base: str | None) -> str | None:
        """
        Some deployments pass an endpoint URL like ".../chat/completions" instead of a base URL.
        Normalize it to the base URL so SDKs work reliably.
        """
        if not api_base:
            return None
        s = str(api_base).strip()
        marker = "/chat/completions"
        if marker in s:
            s = s.split(marker, 1)[0]
        return s.rstrip("/")

    @staticmethod
    def _strip_provider_prefix(model: str) -> str:
        # Common convention: "openai/gpt-5.2" -> "gpt-5.2"
        # Keep other provider strings best-effort: take the last segment.
        return model.split("/")[-1] if "/" in model else model

    @staticmethod
    def _use_openai_client(provider: str) -> bool:
        # Do it purely based on provider prefix. If you point api_base at an OpenAI-compatible
        # endpoint (vLLM/Ollama/etc) but still use the openai/ prefix, we will use OpenAI SDK.
        return provider in {"openai", "gemini", "qwen"}

    @staticmethod
    def _messages_to_responses_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Convert OpenAI-chat-style messages (text + image_url) into OpenAI Responses API input payload.
        """
        out: list[dict[str, Any]] = []
        for m in messages:
            role = m.get("role")
            content = m.get("content")

            items: list[dict[str, Any]] = []
            if isinstance(content, str):
                items.append({"type": "input_text", "text": content})
            elif isinstance(content, list):
                for it in content:
                    if not isinstance(it, dict):
                        continue
                    t = it.get("type")
                    if t == "text":
                        items.append({"type": "input_text", "text": str(it.get("text") or "")})
                    elif t == "image_url":
                        iu = it.get("image_url")
                        url = iu.get("url") if isinstance(iu, dict) else None
                        if isinstance(url, str) and url:
                            items.append({"type": "input_image", "image_url": url})
            else:
                # Unknown; best-effort cast to string.
                items.append({"type": "input_text", "text": "" if content is None else str(content)})

            out.append({"role": role, "content": items})
        return out

    @staticmethod
    def _response_format_for_model(response_model: type[BaseModel]) -> dict[str, Any]:
        # OpenAI-style JSON schema structured output.
        return {
            "type": "json_schema",
            "json_schema": {
                "name": response_model.__name__,
                "schema": response_model.model_json_schema(),
                "strict": True,
            },
        }

    @staticmethod
    def _parse_and_validate(content: str, response_model: type[BaseModel]) -> BaseModel:
        obj = json.loads(content)
        return response_model.model_validate(obj)

    @staticmethod
    def _extract_text_from_litellm_response(resp: Any) -> str:
        choice0 = resp["choices"][0] if isinstance(resp, dict) else resp.choices[0]  # type: ignore[attr-defined]
        msg = choice0["message"] if isinstance(choice0, dict) else choice0.message  # type: ignore[attr-defined]
        content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
        return "" if content is None else str(content).strip()

    def create(
        self,
        *,
        # If response_model is provided -> structured output (validated BaseModel).
        # If response_model is None -> plain text completion (str).
        response_model: type[BaseModel] | None = None,
        messages: list[dict[str, Any]],
        # Optional per-call overrides (backward compatible with older replay code)
        model: str | None = None,
        api_base: str | None = None,
        api_key: str | None = None,
        extra_kwargs: dict[str, Any] | None = None,
        max_tokens: int | None = None,
        max_retries: int | None = None,
    ) -> Any:
        retries = self._max_retries if max_retries is None else int(max_retries)

        m, provider, base, key, merged_kwargs, reasoning = self._resolve(
            model=model, api_base=api_base, api_key=api_key, extra_kwargs=extra_kwargs
        )

        # Plain-text completion path (used by replay grounding/extract).
        if response_model is None:
            if self._use_openai_client(provider):
                client = OpenAI(api_key=key, base_url=base)
                # Reasoning is only supported on Responses API; if configured, use responses.create.
                if reasoning is not None:
                    resp = client.responses.create(
                        model=self._strip_provider_prefix(m),
                        input=self._messages_to_responses_input(messages),  # type: ignore[arg-type]
                        reasoning=reasoning,
                        max_output_tokens=max_tokens,
                        **merged_kwargs,
                    )
                    out_text = getattr(resp, "output_text", None)
                    return "" if out_text is None else str(out_text).strip()

                resp = client.chat.completions.create(
                    model=self._strip_provider_prefix(m),
                    messages=messages,  # type: ignore[arg-type]
                    max_completion_tokens=max_tokens,
                    **merged_kwargs,
                )
                return (resp.choices[0].message.content or "").strip()

            resp = completion(
                model=m,
                messages=messages,
                max_completion_tokens=max_tokens,
                api_base=base,
                api_key=key,
                **merged_kwargs,
            )
            return self._extract_text_from_litellm_response(resp)

        # Structured output path (used by reflect Pass A/B).
        response_model_t = cast(type[BaseModel], response_model)

        if self._use_openai_client(provider):
            client = OpenAI(api_key=key, base_url=base)

            # Prefer Responses API when reasoning is requested (only supported there).
            if reasoning is not None:
                resp = client.responses.parse(
                    model=self._strip_provider_prefix(m),
                    input=self._messages_to_responses_input(messages),  # type: ignore[arg-type]
                    text_format=response_model_t,
                    max_output_tokens=max_tokens,
                    reasoning=reasoning,
                    **merged_kwargs,
                )
                parsed = getattr(resp, "output_parsed", None)
                if parsed is None:
                    raise RuntimeError("Responses parse returned output_parsed=None")
                return parsed

            # Otherwise use Chat Completions with JSON schema response_format.
            resp = client.chat.completions.create(
                model=self._strip_provider_prefix(m),
                messages=messages,  # type: ignore[arg-type]
                max_completion_tokens=max_tokens,
                response_format=self._response_format_for_model(response_model_t),  # type: ignore[arg-type]
                **merged_kwargs,
            )
            content = (resp.choices[0].message.content or "").strip()
            return self._parse_and_validate(content, response_model_t)

        # Non-openai provider: use LiteLLM directly with JSON schema response_format.
        last_err: Exception | None = None
        for _ in range(max(1, retries + 1)):
            try:
                resp = completion(
                    model=m,
                    messages=messages,
                    max_completion_tokens=max_tokens,
                    response_format=self._response_format_for_model(response_model_t),
                    api_base=base,
                    api_key=key,
                    **merged_kwargs,
                )
                text = self._extract_text_from_litellm_response(resp)
                return self._parse_and_validate(text, response_model_t)
            except Exception as e:
                last_err = e
        raise RuntimeError(f"LiteLLM structured call failed after retries: {last_err}") from last_err
