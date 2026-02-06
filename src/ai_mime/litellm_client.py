from __future__ import annotations

import json
import logging
import os
from typing import Any, cast

from openai import OpenAI  # type: ignore[import-not-found]
from litellm import completion  # type: ignore[import-not-found]
from lmnr import observe
from pydantic import BaseModel


logger = logging.getLogger(__name__)


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
        if self._api_key_env is not None:
            v = os.getenv(self._api_key_env)
            if v is None or not str(v).strip():
                raise RuntimeError(f"Missing API key env var {self._api_key_env!r} for model={self._model!r}.")
            self._api_key = str(v).strip()

        self._extra_kwargs = dict(extra_kwargs or {})
        self._max_retries = int(max_retries)

        # OpenAI reasoning controls are a Responses API feature (not Chat Completions).
        # Never forward these into chat.completions.create().
        self._reasoning: Any | None = self._extra_kwargs.pop("reasoning", None)

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

    @staticmethod
    def _output_text_preview(resp: Any, limit: int = 800) -> str:
        t = getattr(resp, "output_text", None)
        if t is None:
            return "<no output_text available>"
        s = str(t)
        return s if len(s) <= limit else (s[:limit] + "…")

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
        """
        Execute OpenAI-compatible Responses API structured parse with retries.

        On failure, retries append a repair message including:
        - failure reason
        - previous output_text (if any)
        - a directive to return only schema-valid JSON
        """
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

                event = getattr(resp, "output_parsed", None)
                if event is None:
                    raise RuntimeError(f"{where}: output_parsed is None. output_text={self._output_text_preview(resp)}")
                return resp
            except TypeError:
                # Some SDK versions may not accept previous_response_id; retry without it.
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
                    event = getattr(resp, "output_parsed", None)
                    if event is None:
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
                # Append the exception context into the same messages array.
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

    @observe(name="llm")
    def create(
        self,
        *,
        response_model: type[BaseModel] | None = None,
        messages: list[dict[str, Any]],
        max_tokens: int | None = None,
    ) -> Any:
        retries = self._max_retries

        m = self._model
        provider = self._provider
        base = self._api_base
        key = self._api_key
        merged_kwargs: dict[str, Any] = dict(self._extra_kwargs)
        reasoning = self._reasoning

        # Plain-text completion path (used by replay grounding/extract).
        if response_model is None:
            if provider in {"openai", "gemini"}:
                client = OpenAI(base_url=base) if key is None else OpenAI(api_key=key, base_url=base)
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
                drop_params=True,  # Skip unsupported params like token counting
                **merged_kwargs,
            )
            return self._extract_text_from_litellm_response(resp)

        # Structured output path (used by reflect Pass A/B).
        response_model_t = cast(type[BaseModel], response_model)

        if provider == "openai":
            client = OpenAI(base_url=base) if key is None else OpenAI(api_key=key, base_url=base)
            input_payload: Any = self._messages_to_responses_input(messages)
            resp = self._responses_parse_with_retries(
                where=f"Structured parse {response_model_t.__name__}",
                client=client,
                model=self._strip_provider_prefix(m),
                input_payload=input_payload,
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

        # Non-openai provider: use LiteLLM directly with JSON schema response_format.
        last_err: Exception | None = None
        for attempt in range(max(1, retries + 1)):
            try:
                # Disable litellm's token counting to avoid tiktoken encoding errors
                resp = completion(
                    model=m,
                    messages=messages,
                    max_completion_tokens=max_tokens,
                    response_format=self._response_format_for_model(response_model_t),
                    api_base=base,
                    api_key=key,
                    drop_params=True,  # Skip unsupported params like token counting
                    **merged_kwargs,
                )
                text = self._extract_text_from_litellm_response(resp)
                return self._parse_and_validate(text, response_model_t)
            except Exception as e:
                last_err = e
                logger.warning(f"LiteLLM structured call attempt {attempt + 1}/{retries + 1} failed: {e}")

        raise RuntimeError(f"LiteLLM structured call failed after {retries + 1} retries: {last_err}") from last_err
