from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from llm_resolver import (
    CONFIG_ENV_VAR,
    DEFAULT_USER_CONFIG,
    ask_llm,
    get_llm_section,
    load_llm_config,
    migrate_config_file,
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
            self.assertEqual(cfg.reflect.pass_a_model, "gemini/gemini-3-pro-preview")
            self.assertEqual(cfg.reflect.pass_b_max_tokens, 7000)

    def test_default_user_config_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "user_config.yml"
            path.write_text(DEFAULT_USER_CONFIG, encoding="utf-8")

            with patch.dict(os.environ, {CONFIG_ENV_VAR: str(path)}, clear=True):
                cfg = load_llm_config()

            self.assertEqual(cfg.runtime.model, "gemini/gemini-3-flash-preview")
            self.assertEqual(cfg.reflect.pass_c_max_tokens, 7000)

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
            ), patch("llm_resolver.runtime.OpenAI", FakeOpenAI):
                result = ask_llm(
                    "Return ok",
                    {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
                )

        self.assertEqual(result, {"ok": True})
        assert FakeOpenAI.last_completions is not None
        self.assertEqual(FakeOpenAI.last_completions.kwargs["model"], "test-runtime")

    def test_migrates_old_top_level_config_shape(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "user_config.yml"
            path.write_text(
                "reflect:\n"
                '  model: "openai/reflect"\n'
                '  api_key_env: "OPENAI_API_KEY"\n'
                "  pass_a:\n"
                "    max_tokens: 1\n"
                "  pass_b:\n"
                "    max_tokens: 2\n"
                "replay:\n"
                '  model: "gemini/replay"\n'
                '  api_key_env: "GEMINI_API_KEY"\n',
                encoding="utf-8",
            )

            self.assertTrue(migrate_config_file(path))

            with patch.dict(os.environ, {CONFIG_ENV_VAR: str(path)}, clear=True):
                cfg = load_llm_config()
            self.assertEqual(cfg.reflect.model, "openai/reflect")
            self.assertEqual(cfg.runtime.model, "gemini/replay")


if __name__ == "__main__":
    unittest.main()
