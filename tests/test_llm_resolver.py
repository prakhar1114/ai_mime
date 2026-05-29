from __future__ import annotations

import os
import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import BaseModel

from llm_resolver import (
    CONFIG_ENV_VAR,
    DEFAULT_USER_CONFIG,
    LiteLLMChatClient,
    ask_llm,
    get_llm_section,
    load_llm_config,
)


def _write_config(path: Path) -> None:
    path.write_text(
        "config_version: 1\n"
        "llm:\n"
        "  runtime:\n"
        '    model: "openai/test-runtime"\n'
        '    api_key_env: "TEST_LLM_KEY"\n'
        "    extra_kwargs: {}\n"
        "  reflect:\n"
        '    model: "openai/test-reflect"\n'
        '    api_key_env: "TEST_LLM_KEY"\n'
        "    extra_kwargs: {}\n"
        "    pass_a:\n"
        '      model: "openai/test-pass-a"\n'
        "      max_tokens: 11\n"
        "    pass_b:\n"
        '      model: "openai/test-pass-b"\n'
        "      max_tokens: 22\n"
        "    pass_c:\n"
        '      model: "openai/test-pass-c"\n'
        "      max_tokens: 33\n",
        encoding="utf-8",
    )


class LLMResolverConfigTests(unittest.TestCase):
    def test_requires_single_config_env_var(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, CONFIG_ENV_VAR):
                load_llm_config()

    def test_fails_when_config_path_missing(self) -> None:
        with patch.dict(os.environ, {CONFIG_ENV_VAR: "/tmp/does-not-exist-ai-mime.yml"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "missing config file"):
                load_llm_config()

    def test_loads_runtime_and_reflect_sections(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "user_config.yml"
            _write_config(path)
            with patch.dict(os.environ, {CONFIG_ENV_VAR: str(path)}, clear=True):
                cfg = load_llm_config()
                self.assertEqual(cfg.runtime.model, "openai/test-runtime")
                self.assertEqual(cfg.reflect.pass_a_model, "openai/test-pass-a")
                self.assertEqual(cfg.reflect.pass_b_max_tokens, 22)
                self.assertEqual(cfg.reflect.pass_c_model, "openai/test-pass-c")
                with self.assertRaisesRegex(RuntimeError, "Unknown LLM config section"):
                    get_llm_section("replay")

    def test_deep_merges_partial_user_config_with_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "user_config.yml"
            path.write_text(
                "llm:\n"
                "  runtime:\n"
                '    model: "openai/custom-runtime"\n'
                "  reflect:\n"
                "    pass_a:\n"
                "      max_tokens: 123\n",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {CONFIG_ENV_VAR: str(path)}, clear=True):
                cfg = load_llm_config()

            self.assertEqual(cfg.runtime.model, "openai/custom-runtime")
            self.assertEqual(cfg.runtime.api_key_env, "GEMINI_API_KEY")
            self.assertEqual(cfg.reflect.model, "gemini/gemini-3-pro-preview")
            self.assertEqual(cfg.reflect.pass_a_max_tokens, 123)
            self.assertIsNone(cfg.reflect.pass_a_model)
            self.assertEqual(cfg.reflect.pass_b_max_tokens, 7000)

    def test_default_user_config_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "user_config.yml"
            path.write_text(DEFAULT_USER_CONFIG, encoding="utf-8")

            with patch.dict(os.environ, {CONFIG_ENV_VAR: str(path)}, clear=True):
                cfg = load_llm_config()

            self.assertEqual(cfg.runtime.model, "gemini/gemini-3-flash-preview")
            self.assertEqual(cfg.reflect.pass_c_max_tokens, 7000)
            self.assertIsNone(cfg.reflect.pass_c_model)

    def test_ask_llm_uses_runtime_config(self) -> None:
        class FakeCompletions:
            def create(self, **kwargs):  # type: ignore[no-untyped-def]
                self.kwargs = kwargs
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))]
                )

        class FakeOpenAI:
            last_completions: FakeCompletions | None = None

            def __init__(self, **kwargs):  # type: ignore[no-untyped-def]
                self.kwargs = kwargs
                completions = FakeCompletions()
                FakeOpenAI.last_completions = completions
                self.chat = SimpleNamespace(completions=completions)

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "user_config.yml"
            _write_config(path)
            with patch.dict(
                os.environ,
                {CONFIG_ENV_VAR: str(path), "TEST_LLM_KEY": "secret"},
                clear=True,
            ), patch("llm_resolver.runtime._load_openai", return_value=FakeOpenAI):
                result = ask_llm(
                    "Return ok",
                    {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
                )

        self.assertEqual(result, {"ok": True})
        assert FakeOpenAI.last_completions is not None
        self.assertEqual(FakeOpenAI.last_completions.kwargs["model"], "test-runtime")

    def test_ask_llm_missing_configured_key_uses_claude_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "user_config.yml"
            _write_config(path)
            with patch.dict(os.environ, {CONFIG_ENV_VAR: str(path)}, clear=True), patch(
                "llm_resolver.runtime._run_claude_structured_fallback",
                return_value={"ok": True},
            ) as fallback:
                result = ask_llm(
                    "Return ok",
                    {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
                )

        self.assertEqual(result, {"ok": True})
        fallback.assert_called_once()
        self.assertEqual(fallback.call_args.kwargs["where"], "ask_llm")

    def test_ask_llm_rejects_model_argument(self) -> None:
        with self.assertRaises(TypeError):
            ask_llm(  # type: ignore[call-arg]
                "Return ok",
                {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
                model="openai/override",
            )

    def test_ask_llm_fallback_forwards_images_as_data_urls(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "user_config.yml"
            _write_config(cfg_path)
            image_path = Path(td) / "screen.png"
            image_path.write_bytes(b"png-bytes")

            captured: dict = {}

            def fake_fallback(**kwargs):  # type: ignore[no-untyped-def]
                captured.update(kwargs)
                return {"ok": True}

            with patch.dict(os.environ, {CONFIG_ENV_VAR: str(cfg_path)}, clear=True), patch(
                "llm_resolver.runtime._run_claude_structured_fallback",
                side_effect=fake_fallback,
            ):
                result = ask_llm(
                    "Inspect",
                    {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
                    images=[str(image_path)],
                )

        self.assertEqual(result, {"ok": True})
        content = captured["messages"][0]["content"]
        self.assertEqual(content[1]["type"], "image_url")
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_ask_llm_rejects_unsupported_image_extension_before_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "user_config.yml"
            _write_config(cfg_path)
            image_path = Path(td) / "screen.bmp"
            image_path.write_bytes(b"bmp")

            with patch.dict(os.environ, {CONFIG_ENV_VAR: str(cfg_path)}, clear=True), patch(
                "llm_resolver.runtime._run_claude_structured_fallback",
                return_value={"ok": True},
            ) as fallback:
                with self.assertRaisesRegex(RuntimeError, "ask_llm: unsupported image extension"):
                    ask_llm(
                        "Inspect",
                        {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
                        images=[str(image_path)],
                    )

        fallback.assert_not_called()

    def test_structured_client_missing_configured_key_uses_claude_fallback(self) -> None:
        class Answer(BaseModel):
            ok: bool

        with patch.dict(os.environ, {}, clear=True), patch(
            "llm_resolver.client._run_claude_structured_fallback",
            return_value={"ok": True},
        ) as fallback:
            client = LiteLLMChatClient(model="openai/test", api_base=None, api_key_env="TEST_LLM_KEY")
            result = client.create(response_model=Answer, messages=[{"role": "user", "content": "Return ok"}])

        self.assertEqual(result.ok, True)
        fallback.assert_called_once()

    def test_structured_client_absent_api_key_env_does_not_use_fallback(self) -> None:
        class Answer(BaseModel):
            ok: bool

        class FakeResp:
            output_parsed = Answer(ok=True)
            output_text = '{"ok": true}'

        class FakeResponses:
            def parse(self, **_kwargs):  # type: ignore[no-untyped-def]
                return FakeResp()

        class FakeOpenAI:
            def __init__(self, **_kwargs):  # type: ignore[no-untyped-def]
                self.responses = FakeResponses()

        with patch.dict(os.environ, {}, clear=True), patch(
            "llm_resolver.client._load_openai",
            return_value=FakeOpenAI,
        ), patch("llm_resolver.client._run_claude_structured_fallback") as fallback:
            client = LiteLLMChatClient(model="openai/test", api_base=None, api_key_env=None)
            result = client.create(response_model=Answer, messages=[{"role": "user", "content": "Return ok"}])

        self.assertEqual(result.ok, True)
        fallback.assert_not_called()

    def test_imports_are_sdk_lazy(self) -> None:
        module_names = [
            "llm_resolver",
            "llm_resolver.claude_fallback",
            "llm_resolver.client",
            "llm_resolver.runtime",
            "ai_mime.agent_runner.adapters.claude_sdk",
        ]
        saved = {name: sys.modules.get(name) for name in module_names}
        for name in module_names:
            sys.modules.pop(name, None)
        try:
            with patch.dict(sys.modules, {"openai": None, "litellm": None, "claude_agent_sdk": None}):
                imported = importlib.import_module("llm_resolver")
                self.assertNotIn("llm_resolver.claude_fallback", sys.modules)
                self.assertNotIn("ai_mime.agent_runner.adapters.claude_sdk", sys.modules)
                self.assertTrue(hasattr(imported, "ask_llm"))
        finally:
            for name in module_names:
                sys.modules.pop(name, None)
            for name, module in saved.items():
                if module is not None:
                    sys.modules[name] = module

    def test_claude_structured_helper_sends_inline_image_blocks(self) -> None:
        from claude_agent_sdk import ResultMessage

        from llm_resolver import claude_fallback

        captured: dict = {}

        async def fake_query(*, prompt, options):  # type: ignore[no-untyped-def]
            captured["options"] = options
            events = []
            async for event in prompt:
                events.append(event)
            captured["events"] = events
            yield ResultMessage(
                subtype="success",
                duration_ms=0,
                duration_api_ms=0,
                is_error=False,
                num_turns=1,
                session_id="session",
                result=None,
                structured_output={"ok": True},
            )

        with tempfile.TemporaryDirectory() as td:
            image_path = Path(td) / "screen.jpg"
            image_path.write_bytes(b"jpg")
            with patch.object(claude_fallback, "query", side_effect=fake_query), patch.object(
                claude_fallback,
                "_find_claude_exe",
                return_value=None,
            ):
                result = claude_fallback.run_claude_agent_sdk_structured(
                    prompt="Inspect",
                    response_schema={
                        "type": "object",
                        "properties": {"ok": {"type": "boolean"}},
                        "required": ["ok"],
                    },
                    images=[image_path],
                )

        self.assertEqual(result, {"ok": True})
        content = captured["events"][0]["message"]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "Inspect"})
        self.assertEqual(content[1]["type"], "image")
        self.assertEqual(content[1]["source"]["media_type"], "image/jpeg")
        self.assertEqual(content[1]["source"]["data"], "anBn")
        self.assertEqual(captured["options"].output_format["type"], "json_schema")


if __name__ == "__main__":
    unittest.main()
