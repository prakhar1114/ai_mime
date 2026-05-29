from __future__ import annotations

import json
import logging
import os
from typing import Any, cast

from pydantic import BaseModel

logger = logging.getLogger(__name__)


def _load_openai() -> Any:
    from openai import OpenAI  # type: ignore[import-not-found]

    return OpenAI


def _load_litellm_completion() -> Any:
    from litellm import completion  # type: ignore[import-not-found]

    return completion


def _missing_configured_api_key(api_key_env: str | None) -> bool:
    if not api_key_env:
        return False
    value = os.getenv(api_key_env)
    return value is None or not str(value).strip()


def _messages_to_claude_inputs(messages: list[dict[str, Any]]) -> tuple[str | None, str, list[str]]:
    system_parts: list[str] = []
    prompt_parts: list[str] = []
    images: list[str] = []

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
                    if isinstance(url, str) and url:
                        images.append(url)
        elif content is not None:
            text_parts.append(str(content))

        text = "\n".join(part for part in text_parts if part).strip()
        if not text:
            continue
        if role == "system":
            system_parts.append(text)
        else:
            prompt_parts.append(text if role == "user" else f"{role}: {text}")

    return (
        "\n\n".join(system_parts).strip() or None,
        "\n\n".join(prompt_parts).strip(),
        images,
    )


def _run_claude_structured_fallback(
    *,
    messages: list[dict[str, Any]],
    response_schema: dict[str, Any],
    where: str,
    model: str | None = None,
) -> Any:
    from .claude_fallback import run_claude_agent_sdk_structured

    system_prompt, prompt, images = _messages_to_claude_inputs(messages)
    prompt = prompt or "Return JSON matching the requested schema."
    try:
        return run_claude_agent_sdk_structured(
            prompt=prompt,
            response_schema=response_schema,
            images=images,
            system_prompt=system_prompt,
            model=model or "claude-sonnet-4-6",
            error_prefix=where,
        )
    except Exception as e:
        raise RuntimeError(f"{where}: Claude fallback failed: {e}") from e


class LiteLLMChatClient:
    """Single generic LLM client for OpenAI-compatible and LiteLLM providers."""

    def __init__(
        self,
        *,
        model: str,
        api_base: str | None,
        api_key_env: str | None,
        extra_kwargs: dict[str, Any] | None = None,
        max_retries: int = 2,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise TypeError("LiteLLMChatClient requires a non-empty model at initialization.")
        self._model = model.strip()
        self._provider = self._model.split("/", 1)[0] if "/" in self._model else "openai"
        self._api_base = self._normalize_api_base(api_base)
        self._api_key_env = api_key_env.strip() if isinstance(api_key_env, str) and api_key_env.strip() else None
        self._api_key: str | None = None
        self._use_claude_fallback = _missing_configured_api_key(self._api_key_env)
        if self._api_key_env is not None:
            value = os.getenv(self._api_key_env)
            if value is not None and str(value).strip():
                self._api_key = str(value).strip()
        self._extra_kwargs = dict(extra_kwargs or {})
        self._max_retries = int(max_retries)
        self._reasoning: Any | None = self._extra_kwargs.pop("reasoning", None)

    @staticmethod
    def _normalize_api_base(api_base: str | None) -> str | None:
        if not api_base:
            return None
        text = str(api_base).strip()
        marker = "/chat/completions"
        if marker in text:
            text = text.split(marker, 1)[0]
        return text.rstrip("/")

    @staticmethod
    def _strip_provider_prefix(model: str) -> str:
        return model.split("/")[-1] if "/" in model else model

    @staticmethod
    def _messages_to_responses_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            content = message.get("content")
            items: list[dict[str, Any]] = []
            if isinstance(content, str):
                items.append({"type": "input_text", "text": content})
            elif isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    item_type = item.get("type")
                    if item_type == "text":
                        items.append({"type": "input_text", "text": str(item.get("text") or "")})
                    elif item_type == "image_url":
                        image_url = item.get("image_url")
                        url = image_url.get("url") if isinstance(image_url, dict) else None
                        if isinstance(url, str) and url:
                            items.append({"type": "input_image", "image_url": url})
            else:
                items.append({"type": "input_text", "text": "" if content is None else str(content)})
            out.append({"role": role, "content": items})
        return out

    @staticmethod
    def _response_format_for_model(response_model: type[BaseModel]) -> dict[str, Any]:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": response_model.__name__,
                "schema": response_model.model_json_schema(),
                "strict": True,
            },
        }

    @staticmethod
    def response_format_for_schema(schema: dict[str, Any], *, name: str = "ask_llm_response") -> dict[str, Any]:
        return {"type": "json_schema", "json_schema": {"name": name, "schema": schema}}

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

    @staticmethod
    def _output_text_preview(resp: Any, limit: int = 800) -> str:
        text = getattr(resp, "output_text", None)
        if text is None:
            return "<no output_text available>"
        value = str(text)
        return value if len(value) <= limit else (value[:limit] + "...")

    def _responses_parse_with_retries(
        self,
        *,
        where: str,
        client: OpenAI,
        model: str,
        input_payload: Any,
        text_format: type[BaseModel],
        max_output_tokens: int | None,
        reasoning: Any | None,
        extra_kwargs: dict[str, Any],
        repair_user_message: str,
        max_retries: int,
    ) -> Any:
        prev_resp: Any | None = None
        last_err: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                kwargs: dict[str, Any] = {
                    "model": model,
                    "input": input_payload,
                    "text_format": text_format,
                    "max_output_tokens": max_output_tokens,
                    **extra_kwargs,
                }
                if reasoning is not None:
                    kwargs["reasoning"] = reasoning
                if attempt > 0 and prev_resp is not None and getattr(prev_resp, "id", None):
                    kwargs["previous_response_id"] = prev_resp.id
                resp = client.responses.parse(**kwargs)
                prev_resp = resp
                if getattr(resp, "output_parsed", None) is None:
                    raise RuntimeError(f"{where}: output_parsed is None. output_text={self._output_text_preview(resp)}")
                return resp
            except TypeError:
                try:
                    kwargs2: dict[str, Any] = {
                        "model": model,
                        "input": input_payload,
                        "text_format": text_format,
                        "max_output_tokens": max_output_tokens,
                        **extra_kwargs,
                    }
                    if reasoning is not None:
                        kwargs2["reasoning"] = reasoning
                    resp = client.responses.parse(**kwargs2)
                    prev_resp = resp
                    if getattr(resp, "output_parsed", None) is None:
                        raise RuntimeError(
                            f"{where}: output_parsed is None. output_text={self._output_text_preview(resp)}"
                        )
                    return resp
                except Exception as e:
                    last_err = e
            except Exception as e:
                last_err = e
            if attempt < max_retries:
                prev_text = self._output_text_preview(prev_resp) if prev_resp is not None else "<no previous output>"
                logger.warning(
                    "%s attempt %d failed (%s). Retrying with repair prompt.",
                    where,
                    attempt + 1,
                    str(last_err),
                )
                input_payload = [
                    *list(input_payload),
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": (
                                    f"{repair_user_message}\n\n"
                                    f"Failure: {last_err}\n"
                                    f"Previous output_text: {prev_text}\n"
                                    "Fix your response and return ONLY schema-valid JSON."
                                ),
                            }
                        ],
                    },
                ]
        raise RuntimeError(f"{where}: failed after {max_retries + 1} attempts: {last_err}") from last_err

    def create(
        self,
        *,
        response_model: type[BaseModel] | None = None,
        messages: list[dict[str, Any]],
        max_tokens: int | None = None,
    ) -> Any:
        retries = self._max_retries
        model = self._model
        provider = self._provider
        base = self._api_base
        key = self._api_key
        merged_kwargs: dict[str, Any] = dict(self._extra_kwargs)
        reasoning = self._reasoning

        if response_model is None:
            if self._use_claude_fallback:
                raise RuntimeError(f"Missing API key env var {self._api_key_env!r} for model={model!r}.")
            if provider in {"openai", "gemini"}:
                OpenAI = _load_openai()
                client = OpenAI(base_url=base) if key is None else OpenAI(api_key=key, base_url=base)
                if reasoning is not None:
                    resp = client.responses.create(
                        model=self._strip_provider_prefix(model),
                        input=self._messages_to_responses_input(messages),  # type: ignore[arg-type]
                        reasoning=reasoning,
                        max_output_tokens=max_tokens,
                        **merged_kwargs,
                    )
                    out_text = getattr(resp, "output_text", None)
                    return "" if out_text is None else str(out_text).strip()
                resp = client.chat.completions.create(
                    model=self._strip_provider_prefix(model),
                    messages=messages,  # type: ignore[arg-type]
                    max_completion_tokens=max_tokens,
                    **merged_kwargs,
                )
                return (resp.choices[0].message.content or "").strip()

            completion = _load_litellm_completion()
            resp = completion(
                model=model,
                messages=messages,
                max_completion_tokens=max_tokens,
                api_base=base,
                api_key=key,
                drop_params=True,
                **merged_kwargs,
            )
            return self._extract_text_from_litellm_response(resp)

        response_model_t = cast(type[BaseModel], response_model)
        if self._use_claude_fallback:
            parsed = _run_claude_structured_fallback(
                messages=messages,
                response_schema=response_model_t.model_json_schema(),
                where=f"Structured parse {response_model_t.__name__}",
            )
            return response_model_t.model_validate(parsed)

        if provider == "openai":
            OpenAI = _load_openai()
            client = OpenAI(base_url=base) if key is None else OpenAI(api_key=key, base_url=base)
            resp = self._responses_parse_with_retries(
                where=f"Structured parse {response_model_t.__name__}",
                client=client,
                model=self._strip_provider_prefix(model),
                input_payload=self._messages_to_responses_input(messages),
                text_format=response_model_t,
                max_output_tokens=max_tokens,
                reasoning=reasoning,
                extra_kwargs=merged_kwargs,
                repair_user_message=(
                    f"Your previous {response_model_t.__name__} output did not parse/validate. "
                    "Re-output ONLY the schema-valid JSON."
                ),
                max_retries=retries,
            )
            parsed = getattr(resp, "output_parsed", None)
            if parsed is None:
                raise RuntimeError("Responses parse returned output_parsed=None")
            return parsed

        last_err: Exception | None = None
        for attempt in range(max(1, retries + 1)):
            try:
                completion = _load_litellm_completion()
                resp = completion(
                    model=model,
                    messages=messages,
                    max_completion_tokens=max_tokens,
                    response_format=self._response_format_for_model(response_model_t),
                    api_base=base,
                    api_key=key,
                    drop_params=True,
                    **merged_kwargs,
                )
                text = self._extract_text_from_litellm_response(resp)
                return self._parse_and_validate(text, response_model_t)
            except Exception as e:
                last_err = e
                logger.warning("LiteLLM structured call attempt %d/%d failed: %s", attempt + 1, retries + 1, e)
        raise RuntimeError(f"LiteLLM structured call failed after {retries + 1} retries: {last_err}") from last_err
