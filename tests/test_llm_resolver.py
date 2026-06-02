from __future__ import annotations

import json
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
    get_computer_use_config,
    get_llm_section,
    load_llm_config,
    runtime_for_model,
)


def _write_config(path: Path) -> None:
    path.write_text(
        "config_version: 1\n"
        "provider: custom\n"
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
        "      max_tokens: 33\n"
        "  agents:\n"
        "    workspace_chat:\n"
        '      model: "openai/test-workspace-chat"\n'
        '      api_key_env: "TEST_LLM_KEY"\n'
        "      extra_kwargs: {}\n"
        "    skill_build:\n"
        '      model: "openai/test-skill-build"\n'
        '      api_key_env: "TEST_LLM_KEY"\n'
        "      extra_kwargs: {}\n"
        "    replay:\n"
        '      model: "openai/test-replay"\n'
        '      api_key_env: "TEST_LLM_KEY"\n'
        "      extra_kwargs: {}\n"
        "    computer_use:\n"
        '      model: "openai/test-computer-use"\n'
        '      api_key_env: "TEST_LLM_KEY"\n'
        "      extra_kwargs: {}\n",
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
                self.assertEqual(cfg.provider, "custom")
                self.assertEqual(cfg.agent_runtime, "codex_cli")
                self.assertEqual(cfg.runtime.model, "openai/test-runtime")
                self.assertEqual(cfg.reflect.pass_a_model, "openai/test-pass-a")
                self.assertEqual(cfg.reflect.pass_b_max_tokens, 22)
                self.assertEqual(cfg.reflect.pass_c_model, "openai/test-pass-c")
                self.assertEqual(cfg.agents.workspace_chat.model, "openai/test-workspace-chat")
                self.assertEqual(cfg.agents.skill_build.model, "openai/test-skill-build")
                self.assertEqual(cfg.agents.replay.model, "openai/test-replay")
                self.assertEqual(cfg.agents.computer_use.model, "openai/test-computer-use")
                self.assertEqual(cfg.agents.computer_use.agent_runtime, "codex_cli")
                with self.assertRaisesRegex(RuntimeError, "Unknown LLM config section"):
                    get_llm_section("replay")

    def test_provider_anthropic_uses_builtin_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "user_config.yml"
            path.write_text(
                "config_version: 1\n"
                "provider: anthropic\n",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {CONFIG_ENV_VAR: str(path)}, clear=True):
                cfg = load_llm_config()

            self.assertEqual(cfg.provider, "anthropic")
            self.assertEqual(cfg.agent_runtime, "claude_code")
            self.assertEqual(cfg.runtime.model, "anthropic/claude-sonnet-4-6")
            self.assertEqual(cfg.runtime.api_key_env, "ANTHROPIC_API_KEY")
            self.assertEqual(cfg.reflect.model, "anthropic/claude-sonnet-4-6")
            self.assertEqual(cfg.reflect.pass_b_max_tokens, 7000)
            self.assertEqual(cfg.agents.workspace_chat.model, "anthropic/claude-sonnet-4-6")
            self.assertEqual(cfg.agents.skill_build.model, "anthropic/claude-sonnet-4-6")
            self.assertEqual(cfg.agents.replay.model, "anthropic/claude-sonnet-4-6")
            self.assertEqual(cfg.agents.computer_use.model, "anthropic/claude-opus-4-8")
            self.assertEqual(cfg.agents.computer_use.agent_runtime, "claude_code")

    def test_provider_openai_uses_builtin_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "user_config.yml"
            path.write_text(
                "config_version: 1\n"
                "provider: openai\n",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {CONFIG_ENV_VAR: str(path)}, clear=True):
                cfg = load_llm_config()

            self.assertEqual(cfg.provider, "openai")
            self.assertEqual(cfg.agent_runtime, "codex_cli")
            self.assertEqual(cfg.runtime.model, "openai/gpt-5.4-mini")
            self.assertEqual(cfg.runtime.api_key_env, "OPENAI_API_KEY")
            self.assertEqual(cfg.reflect.pass_c_model, "openai/gpt-5.5")
            self.assertEqual(cfg.agents.workspace_chat.model, "openai/gpt-5.5")
            self.assertEqual(cfg.agents.skill_build.model, "openai/gpt-5.5")
            self.assertEqual(cfg.agents.replay.model, "openai/gpt-5.5")
            self.assertEqual(cfg.agents.computer_use.model, "openai/gpt-5.5")
            self.assertEqual(cfg.agents.computer_use.api_key_env, "OPENAI_API_KEY")
            self.assertEqual(cfg.agents.computer_use.agent_runtime, "codex_cli")

    def test_provider_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "user_config.yml"
            path.write_text("config_version: 1\n", encoding="utf-8")
            with patch.dict(os.environ, {CONFIG_ENV_VAR: str(path)}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "must set provider"):
                    load_llm_config()

    def test_unknown_provider_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "user_config.yml"
            path.write_text("config_version: 1\nprovider: local\n", encoding="utf-8")
            with patch.dict(os.environ, {CONFIG_ENV_VAR: str(path)}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "Invalid LLM config"):
                    load_llm_config()

    def test_provider_rejects_detailed_llm_unless_custom(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "user_config.yml"
            path.write_text(
                "config_version: 1\n"
                "provider: anthropic\n"
                "llm:\n"
                "  runtime:\n"
                '    model: "anthropic/claude-sonnet-4-6"\n'
                "  reflect:\n"
                '    model: "anthropic/claude-sonnet-4-6"\n',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {CONFIG_ENV_VAR: str(path)}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "remove the detailed llm block"):
                    load_llm_config()

    def test_custom_provider_requires_detailed_llm(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "user_config.yml"
            path.write_text("config_version: 1\nprovider: custom\n", encoding="utf-8")
            with patch.dict(os.environ, {CONFIG_ENV_VAR: str(path)}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "requires a detailed llm block"):
                    load_llm_config()

    def test_custom_provider_agent_flow_runtime_override(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "user_config.yml"
            _write_config(path)
            text = path.read_text(encoding="utf-8")
            path.write_text(text + "agent:\n  replay_runtime: hermes_cli\n", encoding="utf-8")
            with patch.dict(os.environ, {CONFIG_ENV_VAR: str(path)}, clear=True):
                cfg = load_llm_config()
            self.assertEqual(cfg.provider, "custom")
            self.assertEqual(cfg.agent_runtime, "codex_cli")
            self.assertEqual(cfg.agents.replay.agent_runtime, "hermes_cli")

    def test_custom_provider_requires_agent_sections(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "user_config.yml"
            path.write_text(
                "config_version: 1\n"
                "provider: custom\n"
                "llm:\n"
                "  runtime:\n"
                '    model: "openai/test-runtime"\n'
                "  reflect:\n"
                '    model: "openai/test-reflect"\n',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {CONFIG_ENV_VAR: str(path)}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "agents"):
                    load_llm_config()

    def test_custom_provider_computer_use_runtime_infers_from_model(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "user_config.yml"
            _write_config(path)
            text = path.read_text(encoding="utf-8")
            path.write_text(text.replace('model: "openai/test-computer-use"', 'model: "anthropic/claude-opus-4-8"'), encoding="utf-8")
            with patch.dict(os.environ, {CONFIG_ENV_VAR: str(path)}, clear=True):
                cfg = load_llm_config()
                computer_cfg = get_computer_use_config()
            self.assertEqual(cfg.agents.computer_use.agent_runtime, "claude_code")
            self.assertEqual(computer_cfg.model, "anthropic/claude-opus-4-8")
            self.assertEqual(computer_cfg.agent_runtime, "claude_code")

    def test_runtime_for_model_uses_model_prefix_before_provider(self) -> None:
        self.assertEqual(runtime_for_model("anthropic", "openai/gpt-5.5"), "codex_cli")
        self.assertEqual(runtime_for_model("openai", "anthropic/claude-opus-4-8"), "claude_code")
        self.assertEqual(runtime_for_model("openai", "gpt-5.5"), "codex_cli")

    def test_default_user_config_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "user_config.yml"
            path.write_text(DEFAULT_USER_CONFIG, encoding="utf-8")

            with patch.dict(os.environ, {CONFIG_ENV_VAR: str(path)}, clear=True):
                cfg = load_llm_config()

            self.assertEqual(cfg.provider, "anthropic")
            self.assertEqual(cfg.agent_runtime, "claude_code")
            self.assertEqual(cfg.runtime.model, "anthropic/claude-sonnet-4-6")
            self.assertEqual(cfg.runtime.api_key_env, "ANTHROPIC_API_KEY")
            self.assertEqual(cfg.reflect.pass_c_max_tokens, 7000)
            self.assertEqual(cfg.reflect.pass_c_model, "anthropic/claude-opus-4-8")
            self.assertEqual(cfg.agents.computer_use.model, "anthropic/claude-opus-4-8")

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

    def test_ask_llm_missing_openai_key_uses_codex(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "user_config.yml"
            _write_config(path)
            with patch.dict(os.environ, {CONFIG_ENV_VAR: str(path)}, clear=True), patch(
                "llm_resolver.runtime._run_codex_structured",
                return_value={"ok": True},
            ) as fallback:
                result = ask_llm(
                    "Return ok",
                    {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
                )

        self.assertEqual(result, {"ok": True})
        fallback.assert_called_once()
        self.assertEqual(fallback.call_args.kwargs["where"], "ask_llm")
        self.assertEqual(fallback.call_args.kwargs["model"], "openai/test-runtime")

    def test_codex_structured_sends_prompt_on_stdin(self) -> None:
        from llm_resolver.codex import run_codex_structured

        captured: dict[str, object] = {}

        def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env")
            captured["input"] = kwargs.get("input")
            schema_path = Path(cmd[cmd.index("--output-schema") + 1])
            captured["schema"] = json.loads(schema_path.read_text(encoding="utf-8"))
            output_path = Path(cmd[cmd.index("-o") + 1])
            output_path.write_text('{"ok": true}', encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch("llm_resolver.codex._codex_exe", return_value="/bin/codex"), patch(
            "llm_resolver.codex.subprocess.run",
            side_effect=fake_run,
        ):
            result = run_codex_structured(
                messages=[{"role": "user", "content": "Return ok"}],
                response_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
                where="codex test",
                model="openai/gpt-5.5",
            )

        cmd = captured["cmd"]
        self.assertIsInstance(cmd, list)
        self.assertEqual(cmd[-1], "-")
        self.assertIn("--skip-git-repo-check", cmd)
        self.assertEqual(captured["input"], "Return ok")
        env = captured["env"]
        self.assertIsInstance(env, dict)
        self.assertIn("/usr/local/bin", env["PATH"].split(os.pathsep))
        self.assertEqual(captured["schema"]["additionalProperties"], False)
        self.assertEqual(result, {"ok": True})

    def test_codex_structured_parses_current_jsonl_fallback(self) -> None:
        from llm_resolver.codex import run_codex_structured

        stdout = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "codex-thread"}),
                json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": '{"ok": true}'}}),
                json.dumps({"type": "turn.completed"}),
            ]
        )

        with patch("llm_resolver.codex._codex_exe", return_value="/bin/codex"), patch(
            "llm_resolver.codex.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stdout=stdout, stderr=""),
        ):
            result = run_codex_structured(
                messages=[{"role": "user", "content": "Return ok"}],
                response_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
                where="codex test",
                model="openai/gpt-5.5",
            )

        self.assertEqual(result, {"ok": True})

    def test_ask_llm_missing_anthropic_key_passes_configured_model_to_claude_code(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "user_config.yml"
            path.write_text(
                "provider: custom\n"
                "llm:\n"
                "  runtime:\n"
                '    model: "anthropic/claude-opus-4-8"\n'
                '    api_key_env: "ANTHROPIC_API_KEY"\n'
                "    extra_kwargs: {}\n"
                "  reflect:\n"
                '    model: "anthropic/claude-opus-4-8"\n'
                '    api_key_env: "ANTHROPIC_API_KEY"\n'
                "    extra_kwargs: {}\n"
                "    pass_a:\n"
                "      max_tokens: 2000\n"
                "    pass_b:\n"
                "      max_tokens: 7000\n"
                "    pass_c:\n"
                '      model: "anthropic/claude-opus-4-8"\n'
                "      max_tokens: 7000\n"
                "  agents:\n"
                "    workspace_chat:\n"
                '      model: "anthropic/claude-opus-4-8"\n'
                '      api_key_env: "ANTHROPIC_API_KEY"\n'
                "      extra_kwargs: {}\n"
                "    skill_build:\n"
                '      model: "anthropic/claude-opus-4-8"\n'
                '      api_key_env: "ANTHROPIC_API_KEY"\n'
                "      extra_kwargs: {}\n"
                "    replay:\n"
                '      model: "anthropic/claude-opus-4-8"\n'
                '      api_key_env: "ANTHROPIC_API_KEY"\n'
                "      extra_kwargs: {}\n"
                "    computer_use:\n"
                '      model: "anthropic/claude-opus-4-8"\n'
                '      api_key_env: "ANTHROPIC_API_KEY"\n'
                "      extra_kwargs: {}\n",
                encoding="utf-8",
            )
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
        self.assertEqual(fallback.call_args.kwargs["model"], "claude-opus-4-8")

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
                "llm_resolver.runtime._run_codex_structured",
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
                "llm_resolver.runtime._run_codex_structured",
                return_value={"ok": True},
            ) as fallback:
                with self.assertRaisesRegex(RuntimeError, "ask_llm: unsupported image extension"):
                    ask_llm(
                        "Inspect",
                        {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
                        images=[str(image_path)],
                    )

        fallback.assert_not_called()

    def test_structured_client_missing_openai_key_uses_codex(self) -> None:
        class Answer(BaseModel):
            ok: bool

        with patch.dict(os.environ, {}, clear=True), patch(
            "llm_resolver.client._run_codex_structured",
            return_value={"ok": True},
        ) as fallback:
            client = LiteLLMChatClient(model="openai/test", api_base=None, api_key_env="TEST_LLM_KEY")
            result = client.create(response_model=Answer, messages=[{"role": "user", "content": "Return ok"}])

        self.assertEqual(result.ok, True)
        fallback.assert_called_once()
        self.assertEqual(fallback.call_args.kwargs["model"], "openai/test")

    def test_structured_client_missing_anthropic_key_passes_model_to_claude_code(self) -> None:
        class Answer(BaseModel):
            ok: bool

        with patch.dict(os.environ, {}, clear=True), patch(
            "llm_resolver.client._run_claude_structured_fallback",
            return_value={"ok": True},
        ) as fallback:
            client = LiteLLMChatClient(
                model="anthropic/claude-opus-4-8",
                api_base=None,
                api_key_env="ANTHROPIC_API_KEY",
            )
            result = client.create(response_model=Answer, messages=[{"role": "user", "content": "Return ok"}])

        self.assertEqual(result.ok, True)
        fallback.assert_called_once()
        self.assertEqual(fallback.call_args.kwargs["model"], "claude-opus-4-8")

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
