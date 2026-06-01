from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ai_mime.agent_runner import (
    AgentRunRequest,
    AgentRunResult,
    WorkspaceAgentChatService,
    build_agent_run_request,
    run_agent_task,
    run_skill_e2e_test,
    validate_skill_package,
)
from ai_mime.agent_runner.mcp import cua_mcp_servers
from ai_mime.app_data import get_bundled_resource


def _default_browser_skill_root() -> Path:
    return get_bundled_resource("harness/browser-harness")


def _schema() -> dict:
    return {
        "task_name": "record expenses in a sheet",
        "plan": {"subtasks": [{"subtask_i": 0, "text": "Extract receipt", "dependencies": [], "steps": []}]},
    }


def _optimized_plan() -> dict:
    return {
        "version": 1,
        "workflow_goal": "Record a receipt expense.",
        "user_filesystem_access": {
            "readable_roots": [
                {
                    "path": "/Users/prakharjain/Desktop/expenses",
                    "reason": "Read receipt PDFs selected by the user.",
                }
            ],
            "writable_roots": [],
        },
        "inputs": [],
        "steps": [
            {
                "id": "extract_receipt",
                "title": "Extract receipt",
                "source_subtask_ids": [0],
                "executor": "script",
                "goal": "Extract receipt details using direct file access.",
                "inputs": [],
                "outputs": ["receipt_expense"],
                "success_criteria": "Receipt expense is structured.",
                "fallback": "ui_agent",
            }
        ],
    }


def _optimized_plan_with_default_input() -> dict:
    plan = _optimized_plan()
    plan["inputs"] = [
        {
            "name": "receipt_path",
            "description": "Path to the receipt.",
            "required": True,
            "default": "/tmp/receipt.pdf",
        }
    ]
    plan["steps"][0]["inputs"] = ["receipt_path"]
    return plan


def _example_inputs_from_plan(optimized_plan: dict) -> dict[str, object]:
    out: dict[str, object] = {}
    for item in optimized_plan.get("inputs", []) or []:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        out[name] = item.get("default") if "default" in item else f"<FILL IN: {item.get('description','')}>"
    return out


def _write_valid_skill_package(skill_dir: Path, schema: dict, optimized_plan: dict) -> None:
    (skill_dir / "scripts").mkdir(parents=True, exist_ok=True)
    (skill_dir / "inputs").mkdir(parents=True, exist_ok=True)
    (skill_dir / "references").mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        'name: record-expenses-in-a-sheet\n'
        'description: Record a receipt expense. Use when the user asks to log an expense.\n'
        "---\n\n"
        "# Record Expenses Skill\n\n"
        "## Inputs\n- receipt_path (required, string) — path to receipt.\n\n"
        "## Run\n`./run.sh`\n\n"
        "## Outputs\nA structured expense record.\n\n"
        "## Progress log format\nstep_start / step_done / step_failed / workflow_done JSON-line events.\n\n"
        "## Fallback\nSee references/fallback_plan.md.\n\n"
        "## ask_llm decision points\nNone.\n\n"
        "## References\n- fallback_plan.md\n",
        encoding="utf-8",
    )
    inputs_example = _example_inputs_from_plan(optimized_plan)
    (skill_dir / "inputs" / "inputs.example.json").write_text(
        json.dumps(inputs_example, indent=2), encoding="utf-8"
    )
    (skill_dir / "inputs" / "inputs.template.json").write_text(
        json.dumps({k: f"<FILL IN: {k}>" for k in inputs_example.keys()}, indent=2),
        encoding="utf-8",
    )
    (skill_dir / "references" / "fallback_plan.md").write_text(
        "# Fallback plan\n\n## Subtask 0 — Extract receipt\nIntent: parse the receipt.\n",
        encoding="utf-8",
    )
    (skill_dir / "scripts" / "run.py").write_text(
        "import argparse\n"
        "import json\n"
        "parser = argparse.ArgumentParser()\n"
        "parser.add_argument('--inputs-json', required=True)\n"
        "args = parser.parse_args()\n"
        "with open(args.inputs_json, 'r', encoding='utf-8') as f:\n"
        "    inputs = json.load(f)\n"
        "print('step extract_receipt: starting')\n"
        "print('inputs=' + json.dumps(inputs, sort_keys=True))\n"
        "print('step extract_receipt: done')\n",
        encoding="utf-8",
    )
    run_sh = skill_dir / "run.sh"
    run_sh.write_text(
        '#!/usr/bin/env bash\n'
        'set -euo pipefail\n'
        'HERE="$(cd "$(dirname "$0")" && pwd)"\n'
        'INPUTS="${1:-$HERE/inputs/inputs.example.json}"\n'
        'PYTHON="${AI_MIME_PYTHON_PATH:?AI_MIME_PYTHON_PATH is required}"\n'
        'if [[ -x "$HERE/.venv/bin/python" ]]; then\n'
        '  PYTHON="$HERE/.venv/bin/python"\n'
        'elif [[ -x "$HERE/../../.venv/bin/python" ]]; then\n'
        '  PYTHON="$HERE/../../.venv/bin/python"\n'
        'fi\n'
        'exec "$PYTHON" "$HERE/scripts/run.py" --inputs-json "$INPUTS"\n',
        encoding="utf-8",
    )
    run_sh.chmod(run_sh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class FakeAdapter:
    id = "claude_code"

    def __init__(self) -> None:
        self.request: AgentRunRequest | None = None
        self.prompt: str | None = None
        self.runtime_env: dict[str, str | None] = {}

    def run(self, request: AgentRunRequest, prompt: str) -> AgentRunResult:
        self.request = request
        self.prompt = prompt
        # The real SDK adapter derives the runtime env from the request's
        # workflow_dir and passes it via ClaudeAgentOptions.env (see
        # _options_kwargs_for); mirror that here so the test reflects the
        # current injection path rather than a global os.environ mutation.
        from ai_mime.app_data import workflow_runtime_env

        self.runtime_env = dict(workflow_runtime_env(request.workflow_dir))
        return AgentRunResult(
            status="success",
            session_id=request.session_id or "",
            summary="Fake agent completed the optimized plan.",
        )


def _agents_config(
    *,
    workspace_chat_model: str = "anthropic/claude-sonnet-4-6",
    workspace_chat_runtime: str = "claude_code",
    skill_build_model: str = "anthropic/claude-sonnet-4-6",
    skill_build_runtime: str = "claude_code",
    replay_model: str = "anthropic/claude-sonnet-4-6",
    replay_runtime: str = "claude_code",
    computer_use_model: str = "anthropic/claude-opus-4-8",
    computer_use_runtime: str = "claude_code",
) -> SimpleNamespace:
    return SimpleNamespace(
        workspace_chat=SimpleNamespace(model=workspace_chat_model, agent_runtime=workspace_chat_runtime),
        skill_build=SimpleNamespace(model=skill_build_model, agent_runtime=skill_build_runtime),
        replay=SimpleNamespace(model=replay_model, agent_runtime=replay_runtime),
        computer_use=SimpleNamespace(model=computer_use_model, agent_runtime=computer_use_runtime),
    )


class AgentRunnerTests(unittest.TestCase):
    def test_agent_run_request_defaults_include_runtime_read_write_roots(self) -> None:
        request = AgentRunRequest(
            provider="claude",
            mode="general",
            workflow_dir=Path("/workflows"),
            workspace_dir=Path("/workflows"),
        )

        self.assertIn(Path("/tmp"), request.readable_roots)
        self.assertIn(Path("/tmp"), request.writable_roots)
        self.assertIn(_default_browser_skill_root(), request.readable_roots)
        self.assertNotIn(_default_browser_skill_root(), request.writable_roots)

    def test_build_request_merges_user_read_hints_and_default_writes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workflow_dir = Path(td)
            (workflow_dir / "schema.json").write_text(json.dumps(_schema()), encoding="utf-8")
            (workflow_dir / "optimized_plan.json").write_text(json.dumps(_optimized_plan()), encoding="utf-8")

            request = build_agent_run_request(workflow_dir=workflow_dir, provider="claude", mode="build_skill_chat")

        self.assertIn(Path("/Users/prakharjain/Desktop/expenses"), request.readable_roots)
        self.assertIn(Path("/tmp"), request.readable_roots)
        self.assertIn(Path("/tmp"), request.writable_roots)
        self.assertIn(_default_browser_skill_root(), request.readable_roots)
        self.assertNotIn(_default_browser_skill_root(), request.writable_roots)
        self.assertIn(workflow_dir / "outputs", request.writable_roots)
        self.assertIn(workflow_dir / "agent", request.writable_roots)
        self.assertIn(workflow_dir / "skills", request.writable_roots)

    def test_run_agent_task_resumes_session_and_writes_latest_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workflow_dir = Path(td)
            (workflow_dir / "schema.json").write_text(json.dumps(_schema()), encoding="utf-8")
            (workflow_dir / "optimized_plan.json").write_text(json.dumps(_optimized_plan()), encoding="utf-8")
            request = build_agent_run_request(
                workflow_dir=workflow_dir,
                provider="claude",
                mode="build_skill_chat",
                session_id="existing-session",
            )
            adapter = FakeAdapter()

            result = run_agent_task(request, adapter)

            self.assertEqual(result.status, "success")
            self.assertEqual(result.session_id, "existing-session")
            self.assertTrue((workflow_dir / "agent" / "session.json").exists())
            self.assertTrue((workflow_dir / "agent" / "memory.md").exists())
            self.assertTrue((workflow_dir / "outputs" / "result.json").exists())
            self.assertTrue((workflow_dir / "outputs" / "README.md").exists())
            self.assertIsNotNone(adapter.request)
            assert adapter.request is not None
            self.assertEqual(adapter.request.session_id, "existing-session")
            self.assertIsNotNone(adapter.request.temp_dir)
            self.assertIn("optimized_plan.json", adapter.prompt or "")
            self.assertIsNotNone(adapter.runtime_env["AI_MIME_PYTHON_PATH"])
            self.assertIsNotNone(adapter.runtime_env["AI_MIME_UV_PATH"])

    def test_general_mode_uses_workflows_workspace_and_allows_missing_schema(self) -> None:
        request = build_agent_run_request(workflow_dir=Path("/tmp/ignored"), provider="claude", mode="general")
        self.assertEqual(request.mode, "general")
        self.assertIsNone(request.schema_path)
        self.assertIsNone(request.optimized_plan_path)
        self.assertEqual(request.workflow_dir.name, "workflows")
        self.assertEqual(request.workspace_dir, request.workflow_dir)
        self.assertIn(Path("/tmp"), request.readable_roots)
        self.assertIn(Path("/tmp"), request.writable_roots)
        self.assertIn(_default_browser_skill_root(), request.readable_roots)
        self.assertNotIn(_default_browser_skill_root(), request.writable_roots)

    def test_agent_request_uses_env_configured_skill_paths(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            browser_skill = root / "browser-harness"
            browser_skill.mkdir()
            env = {
                "AI_MIME_BROWSER_SKILL_NAME": "browser",
                "AI_MIME_BROWSER_SKILL_PATH": str(browser_skill),
            }
            with patch.dict(os.environ, env, clear=False):
                request = AgentRunRequest(
                    provider="claude",
                    mode="general",
                    workflow_dir=root / "workflows",
                    workspace_dir=root / "workflows",
                )
                adapter = FakeAdapter()
                run_agent_task(request, adapter)

            self.assertIn(browser_skill.resolve(), request.readable_roots)
            self.assertNotIn(browser_skill.resolve(), request.writable_roots)
            prompt = adapter.prompt or ""
            self.assertIn(str(browser_skill.resolve()), prompt)

    def test_workspace_chat_service_general_request_allows_agent_dir_read_write(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_td, tempfile.TemporaryDirectory() as agent_td:
            service = WorkspaceAgentChatService(
                workspace_dir=Path(workspace_td),
                agent_dir=Path(agent_td),
                adapter=FakeAdapter(),
                session_lister=lambda _dir: [],
                message_loader=lambda _sid, _dir: [],
            )

            request = service._build_request(session_id=None, model="sonnet")

        self.assertIn(Path(agent_td), request.readable_roots)
        self.assertIn(Path(agent_td), request.writable_roots)

    def test_workflow_mode_rejects_missing_schema(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(FileNotFoundError):
                build_agent_run_request(workflow_dir=Path(td), provider="claude", mode="build_skill_chat")

    def test_build_skill_chat_prompt_contains_iteration_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workflow_dir = Path(td)
            schema = _schema()
            plan = _optimized_plan()
            (workflow_dir / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
            (workflow_dir / "optimized_plan.json").write_text(json.dumps(plan), encoding="utf-8")
            request = build_agent_run_request(workflow_dir=workflow_dir, provider="claude", mode="build_skill_chat")
            adapter = FakeAdapter()

            run_agent_task(request, adapter)

            prompt = adapter.prompt or ""
            # Verify system prompt refers to instructions directory and sequence files
            self.assertIn("instructions/build_skill", prompt)
            self.assertIn("instructions/ui_agent/00_ui_agent.md", prompt)
            self.assertIn("00_rules.md", prompt)
            self.assertIn("01_phase_a_confirm_inputs.md", prompt)
            self.assertIn("CRITICAL: Do NOT read all instruction files at once", prompt)
            self.assertIn(
                Path(__file__).parent.parent / "src" / "ai_mime" / "agent_runner" / "instructions",
                request.readable_roots,
            )

            # Read the files in the instructions folder and verify they contain the detailed protocols
            instructions_dir = Path(__file__).parent.parent / "src" / "ai_mime" / "agent_runner" / "instructions" / "build_skill"
            files_content = ""
            for p in instructions_dir.glob("*.md"):
                files_content += p.read_text(encoding="utf-8") + "\n"

            # Core protocol elements
            self.assertIn("build_signal.json", files_content)
            self.assertIn("skill_ready", files_content)
            self.assertIn("skill_unbuildable", files_content)
            self.assertIn("ask_llm", files_content)
            # Executor taxonomy
            self.assertIn("script", files_content)
            self.assertIn("browser_harness", files_content)
            self.assertIn("ui_agent", files_content)
            # Four-phase protocol
            self.assertIn("Phase A", files_content)
            self.assertIn("Phase B", files_content)
            self.assertIn("Phase C", files_content)
            self.assertIn("Phase D", files_content)
            # Non-technical, autonomy-first chat behavior
            self.assertIn("The end user is not technical", files_content)
            self.assertIn("Ask only important questions", files_content)
            self.assertIn("Do NOT ask for confirmation before each step", files_content)
            self.assertIn("plain-language", files_content)
            self.assertIn("expected outputs", files_content)
            self.assertIn("very high-level", files_content)
            self.assertIn("do not ask for packaging approval", files_content)
            self.assertIn("Do not pause after successful individual steps", files_content)
            self.assertNotIn("Continue?", files_content)
            self.assertNotIn("Ready to package and create the skill", files_content)
            self.assertNotIn("advance only after explicit user OK", files_content)
            # Inputs editing
            self.assertIn("inputs[]", files_content)
            # Side effect protocol
            self.assertIn("side_effects.md", files_content)
            # File contract
            self.assertIn("scripts/run.py", files_content)
            self.assertIn("run.sh", files_content)
            self.assertIn("inputs/inputs.example.json", files_content)
            self.assertIn("inputs/inputs.template.json", files_content)
            self.assertIn("references/fallback_plan.md", files_content)
            self.assertIn("skill-creator", files_content)
            # Internet & external services guidance
            self.assertIn("WebSearch", files_content)
            self.assertIn("WebFetch", files_content)
            self.assertIn("Do not depend on `uvx`, `npx`", files_content)
            self.assertNotIn("npx --yes", files_content)
            self.assertIn("AI_MIME_PYTHON_PATH", files_content)
            self.assertIn("AI_MIME_UV_PATH", files_content)
            self.assertIn("AI_MIME_BROWSER_HARNESS_BIN", files_content)
            self.assertIn('"$AI_MIME_BROWSER_HARNESS_BIN" -c', files_content)
            self.assertIn("requirements.txt", files_content)
            self.assertIn(".venv/bin/python", files_content)
            self.assertIn("SKILL.md` `## Run` must document the Python runtime contract", files_content)
            self.assertIn("skill `.venv/bin/python`", files_content)
            self.assertIn("workflow `.venv/bin/python`", files_content)
            self.assertIn("then required `$AI_MIME_PYTHON_PATH`", files_content)
            self.assertIn('"$AI_MIME_UV_PATH" venv .venv --python "$AI_MIME_PYTHON_PATH"', files_content)
            self.assertIn(
                '"$AI_MIME_UV_PATH" pip install -r requirements.txt --python .venv/bin/python',
                files_content,
            )
            self.assertIn('PYTHON="${AI_MIME_PYTHON_PATH:?AI_MIME_PYTHON_PATH is required}"', files_content)
            self.assertNotIn('PYTHON="${AI_MIME_PYTHON_PATH:-python3}"', files_content)
            self.assertIn("Runtime does not create or repair `.venv`", files_content)
            # Structured log contract
            self.assertIn("step_start", files_content)
            self.assertIn("step_done", files_content)
            self.assertIn("step_failed", files_content)
            self.assertIn("workflow_done", files_content)
            # Skill must not ship internal builder artifacts
            self.assertNotIn("references/schema.json", files_content)
            self.assertNotIn("references/optimized_plan.json", files_content)
            # schema/optimized_plan are writable for input edits
            writable = {str(p) for p in request.writable_roots}
            self.assertIn(str(workflow_dir / "schema.json"), writable)
            self.assertIn(str(workflow_dir / "optimized_plan.json"), writable)

    def test_run_skill_e2e_exports_runtime_env_and_run_sh_prefers_venv(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            skill_dir = Path(td) / "skill"
            schema = _schema()
            plan = _optimized_plan()
            _write_valid_skill_package(skill_dir, schema, plan)
            venv_python = skill_dir / ".venv" / "bin" / "python"
            venv_python.parent.mkdir(parents=True)
            venv_python.write_text(
                "#!/usr/bin/env bash\n"
                "echo \"venv-python:$1\"\n"
                "echo \"env-python:${AI_MIME_PYTHON_PATH:-}\"\n"
                "exit 0\n",
                encoding="utf-8",
            )
            venv_python.chmod(0o755)
            (skill_dir / "run.sh").write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "HERE=\"$(cd \"$(dirname \"$0\")\" && pwd)\"\n"
                "INPUTS=\"${1:-$HERE/inputs/inputs.example.json}\"\n"
                "PYTHON=\"${AI_MIME_PYTHON_PATH:?AI_MIME_PYTHON_PATH is required}\"\n"
                "if [[ -x \"$HERE/.venv/bin/python\" ]]; then\n"
                "  PYTHON=\"$HERE/.venv/bin/python\"\n"
                "fi\n"
                "exec \"$PYTHON\" \"$HERE/scripts/run.py\" --inputs-json \"$INPUTS\"\n",
                encoding="utf-8",
            )
            (skill_dir / "run.sh").chmod(0o755)

            result = run_skill_e2e_test(skill_dir, plan)

            self.assertEqual(result.status, "success")
            self.assertIn("venv-python:", result.summary)
            self.assertIn(f"env-python:{venv_python}", result.summary)

    def test_replay_execution_prompt_and_access_are_narrow(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workflow_dir = Path(td)
            schema = _schema()
            user_writable = workflow_dir / "expense-output"
            plan = _optimized_plan()
            plan["user_filesystem_access"]["writable_roots"] = [
                {
                    "path": str(user_writable),
                    "reason": "Write the completed expense export.",
                }
            ]
            (workflow_dir / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
            (workflow_dir / "optimized_plan.json").write_text(json.dumps(plan), encoding="utf-8")
            skill_dir = workflow_dir / "skills" / "record-expenses-in-a-sheet"
            _write_valid_skill_package(skill_dir, schema, plan)
            request = build_agent_run_request(workflow_dir=workflow_dir, provider="claude", mode="replay_execution")
            adapter = FakeAdapter()

            run_agent_task(request, adapter)

            self.assertEqual(request.mode, "replay_execution")
            writable = {str(p) for p in request.writable_roots}
            self.assertIn(str(workflow_dir / "agent"), writable)
            self.assertIn(str(workflow_dir / "outputs"), writable)
            self.assertIn(str(workflow_dir / "outputs" / "assets"), writable)
            self.assertIn(str(skill_dir), writable)
            self.assertIn(str(user_writable), writable)
            self.assertNotIn(str(workflow_dir / "skills"), writable)
            self.assertNotIn(str(workflow_dir / "schema.json"), writable)
            self.assertNotIn(str(workflow_dir / "optimized_plan.json"), writable)

            prompt = adapter.prompt or ""
            # Verify system prompt refers to instructions directory and sequence files
            self.assertIn("instructions/replay", prompt)
            self.assertIn("instructions/ui_agent/00_ui_agent.md", prompt)
            self.assertIn("00_rules.md", prompt)
            self.assertIn("01_replay.md", prompt)
            self.assertIn("CRITICAL: Do NOT read all instruction files at once", prompt)
            self.assertIn(
                Path(__file__).parent.parent / "src" / "ai_mime" / "agent_runner" / "instructions",
                request.readable_roots,
            )

            # Read the files in the instructions folder and verify they contain the detailed protocols
            instructions_dir = Path(__file__).parent.parent / "src" / "ai_mime" / "agent_runner" / "instructions" / "replay"
            files_content = ""
            for p in instructions_dir.glob("*.md"):
                files_content += p.read_text(encoding="utf-8") + "\n"

            self.assertIn("Validate and normalize", files_content)
            self.assertIn("./run.sh <inputs.json>", files_content)
            self.assertIn("task variants", files_content)
            self.assertIn("complete the task", files_content)
            self.assertIn("$AI_MIME_UI_AGENT_CMD", files_content)
            self.assertIn("triage before editing", files_content)
            self.assertIn("Closed tabs", files_content)
            self.assertIn("missing windows", files_content)
            self.assertIn("one-off UI disruption", files_content)
            self.assertIn("replay_notes.md", files_content)
            self.assertIn("domain_notes.md", files_content)
            self.assertIn("Targeted edits", files_content)
            self.assertIn("Only edit the skill when needed", files_content)
            self.assertIn("repeated deterministic failure", files_content)
            self.assertNotIn("Do NOT switch to skill-build mode", files_content)
            self.assertNotIn("needs skill healing", files_content)
            self.assertNotIn("AI_MIME_REPLAY_HANDOFF_TO_SKILL_BUILD", files_content)

    def test_workspace_chat_service_can_use_replay_execution_mode(self) -> None:
        prompts: list[str] = []
        modes: list[str] = []
        system_prompts: list[str | None] = []

        class ChatAdapter:
            id = "claude_code"

            def run(self, request: AgentRunRequest, prompt: str) -> AgentRunResult:
                prompts.append(prompt)
                modes.append(request.mode)
                system_prompts.append(request.system_prompt)
                return AgentRunResult(status="success", session_id="replay-session-1", summary="ok")

        with tempfile.TemporaryDirectory() as td:
            workflow_dir = Path(td)
            schema = _schema()
            plan = _optimized_plan()
            (workflow_dir / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
            (workflow_dir / "optimized_plan.json").write_text(json.dumps(plan), encoding="utf-8")
            _write_valid_skill_package(workflow_dir / "skills" / "record-expenses-in-a-sheet", schema, plan)
            service = WorkspaceAgentChatService(
                workspace_dir=workflow_dir,
                mode="replay_execution",
                agent_dir=workflow_dir / "agent" / "replay",
                adapter=ChatAdapter(),
                session_lister=lambda _dir: [],
                message_loader=lambda _sid, _dir: [],
            )

            result = service.chat(message="run it")

            self.assertEqual(result["session_id"], "replay-session-1")
            self.assertEqual(modes, ["replay_execution"])
            self.assertEqual(prompts, ["run it"])
            self.assertIn("replay execution agent", system_prompts[0] or "")
            self.assertTrue((workflow_dir / "agent" / "agent_sessions.json").exists())

    def test_replay_execution_chat_accepts_sequential_recovery_turns(self) -> None:
        session_ids: list[str | None] = []

        class ChatAdapter:
            id = "claude_code"

            def run(self, request: AgentRunRequest, prompt: str) -> AgentRunResult:
                session_ids.append(request.session_id)
                return AgentRunResult(status="success", session_id=request.session_id or "replay-session-1", summary="ok")

        with tempfile.TemporaryDirectory() as td:
            workflow_dir = Path(td)
            schema = _schema()
            plan = _optimized_plan()
            (workflow_dir / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
            (workflow_dir / "optimized_plan.json").write_text(json.dumps(plan), encoding="utf-8")
            _write_valid_skill_package(workflow_dir / "skills" / "record-expenses-in-a-sheet", schema, plan)
            service = WorkspaceAgentChatService(
                workspace_dir=workflow_dir,
                mode="replay_execution",
                agent_dir=workflow_dir / "agent" / "replay",
                adapter=ChatAdapter(),
                session_lister=lambda _dir: [],
                message_loader=lambda _sid, _dir: [],
            )

            first = service.chat(message="continue replay")
            result = service.chat(message="continue again", session_id=first["session_id"])

            self.assertEqual(result["session_id"], "replay-session-1")
            self.assertEqual(session_ids, [None, "replay-session-1"])

    def test_replay_execution_stream_runs_without_turn_lock(self) -> None:
        class StreamAdapter:
            id = "claude_code"

            async def stream_chat(self, *_args, **_kwargs):
                yield {"event": "text", "text": "ok"}
                yield {"event": "done", "status": "success", "session_id": "replay-session-1", "summary": "ok"}

        async def collect_events(service: WorkspaceAgentChatService) -> list[dict]:
            return [event async for event in service.chat_stream(message="continue replay")]

        with tempfile.TemporaryDirectory() as td:
            workflow_dir = Path(td)
            schema = _schema()
            plan = _optimized_plan()
            (workflow_dir / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
            (workflow_dir / "optimized_plan.json").write_text(json.dumps(plan), encoding="utf-8")
            _write_valid_skill_package(workflow_dir / "skills" / "record-expenses-in-a-sheet", schema, plan)
            service = WorkspaceAgentChatService(
                workspace_dir=workflow_dir,
                mode="replay_execution",
                agent_dir=workflow_dir / "agent" / "replay",
                adapter=StreamAdapter(),
                session_lister=lambda _dir: [],
                message_loader=lambda _sid, _dir: [],
            )

            events = asyncio.run(collect_events(service))

            self.assertEqual(events[-1]["event"], "done")
            self.assertEqual(events[-1]["session_id"], "replay-session-1")

    def test_validate_skill_package_accepts_valid_package(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            skill_dir = Path(td) / "skills" / "record-expenses-in-a-sheet"
            schema = _schema()
            plan = _optimized_plan_with_default_input()
            _write_valid_skill_package(skill_dir, schema, plan)

            validate_skill_package(skill_dir, schema, plan)

    def test_validate_skill_package_rejects_missing_required_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            skill_dir = Path(td) / "skills" / "record-expenses-in-a-sheet"
            schema = _schema()
            plan = _optimized_plan_with_default_input()
            _write_valid_skill_package(skill_dir, schema, plan)
            (skill_dir / "scripts" / "run.py").unlink()

            with self.assertRaisesRegex(FileNotFoundError, "scripts/run.py"):
                validate_skill_package(skill_dir, schema, plan)

    def test_run_skill_e2e_test_rejects_missing_required_default(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            skill_dir = Path(td) / "skills" / "record-expenses-in-a-sheet"
            schema = _schema()
            plan = _optimized_plan()
            plan["inputs"] = [{"name": "receipt_path", "description": "Path", "required": True}]
            _write_valid_skill_package(skill_dir, schema, plan)
            # Force the resolver past confirmed_inputs and inputs.example.json so
            # it must synthesize from optimized_plan and discover the missing default.
            (skill_dir / "inputs" / "inputs.example.json").unlink()

            result = run_skill_e2e_test(skill_dir, plan)

            self.assertEqual(result.status, "failed")
            self.assertIn("required optimized_plan inputs have no default", result.error or "")

    def test_validate_skill_package_rejects_missing_frontmatter(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            skill_dir = Path(td) / "skills" / "record-expenses-in-a-sheet"
            schema = _schema()
            plan = _optimized_plan_with_default_input()
            _write_valid_skill_package(skill_dir, schema, plan)
            (skill_dir / "SKILL.md").write_text("# No frontmatter here\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "frontmatter"):
                validate_skill_package(skill_dir, schema, plan)

    def test_validate_skill_package_rejects_frontmatter_missing_name(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            skill_dir = Path(td) / "skills" / "record-expenses-in-a-sheet"
            schema = _schema()
            plan = _optimized_plan_with_default_input()
            _write_valid_skill_package(skill_dir, schema, plan)
            (skill_dir / "SKILL.md").write_text(
                "---\ndescription: only description\n---\n\n# Body\n", encoding="utf-8"
            )

            with self.assertRaisesRegex(ValueError, "'name'"):
                validate_skill_package(skill_dir, schema, plan)

    def test_validate_skill_package_rejects_non_executable_run_sh(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            skill_dir = Path(td) / "skills" / "record-expenses-in-a-sheet"
            schema = _schema()
            plan = _optimized_plan_with_default_input()
            _write_valid_skill_package(skill_dir, schema, plan)
            run_sh = skill_dir / "run.sh"
            run_sh.chmod(run_sh.stat().st_mode & ~(stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))

            with self.assertRaisesRegex(ValueError, "run.sh is not executable"):
                validate_skill_package(skill_dir, schema, plan)

    def test_validate_skill_package_accepts_free_form_references(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            skill_dir = Path(td) / "skills" / "record-expenses-in-a-sheet"
            schema = _schema()
            plan = _optimized_plan_with_default_input()
            _write_valid_skill_package(skill_dir, schema, plan)
            (skill_dir / "references" / "domain_notes.md").write_text("Domain.\n", encoding="utf-8")
            (skill_dir / "references" / "subtask_0.md").write_text("Subtask 0 notes.\n", encoding="utf-8")

            validate_skill_package(skill_dir, schema, plan)

    def test_validate_skill_package_rejects_example_missing_required_key(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            skill_dir = Path(td) / "skills" / "record-expenses-in-a-sheet"
            schema = _schema()
            plan = _optimized_plan_with_default_input()
            _write_valid_skill_package(skill_dir, schema, plan)
            (skill_dir / "inputs" / "inputs.example.json").write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "inputs.example.json missing required keys"):
                validate_skill_package(skill_dir, schema, plan)

    def test_filesystem_sandbox_hook_blocks_paths_outside_roots(self) -> None:
        import asyncio as _asyncio

        from ai_mime.agent_runner.adapters.claude_sdk import _build_filesystem_sandbox_hook
        from ai_mime.agent_runner.models import FilesystemAccess

        with tempfile.TemporaryDirectory() as td:
            workflow_dir = Path(td)
            allowed = workflow_dir / "agent"
            allowed.mkdir()
            request = AgentRunRequest(
                provider="claude",
                mode="build_skill_chat",
                workflow_dir=workflow_dir,
                workspace_dir=workflow_dir,
                readable_roots=[workflow_dir],
                writable_roots=[allowed],
                user_filesystem_access=FilesystemAccess(),
            )
            hook = _build_filesystem_sandbox_hook(request)
            assert hook is not None

            async def _call(tool_name: str, tool_input: dict) -> dict:
                return await hook({"tool_name": tool_name, "tool_input": tool_input}, "tid", None)

            # Read inside readable root → allowed (empty dict)
            out = _asyncio.run(
                _call("Read", {"file_path": str(workflow_dir / "schema.json")})
            )
            self.assertEqual(out, {})

            # Read outside readable root → block
            out = _asyncio.run(
                _call("Read", {"file_path": "/etc/passwd"})
            )
            self.assertEqual(out.get("decision"), "block")
            self.assertIn("sandbox", out.get("reason", ""))

            # Write to writable root → allowed
            out = _asyncio.run(
                _call("Write", {"file_path": str(allowed / "x.json")})
            )
            self.assertEqual(out, {})

            # Write outside writable root (still inside readable workflow_dir) → block
            out = _asyncio.run(
                _call("Write", {"file_path": str(workflow_dir / "outside.txt")})
            )
            self.assertEqual(out.get("decision"), "block")

            # Bash / unrelated tool → pass through
            out = _asyncio.run(
                _call("Bash", {"command": "echo hi"})
            )
            self.assertEqual(out, {})

    def test_options_kwargs_installs_sandbox_pretooluse_hook(self) -> None:
        from ai_mime.agent_runner.adapters.claude_sdk import _options_kwargs_for
        from ai_mime.agent_runner.models import FilesystemAccess

        with tempfile.TemporaryDirectory() as td:
            workflow_dir = Path(td)
            request = AgentRunRequest(
                provider="claude",
                mode="build_skill_chat",
                workflow_dir=workflow_dir,
                workspace_dir=workflow_dir,
                readable_roots=[workflow_dir],
                writable_roots=[workflow_dir],
                user_filesystem_access=FilesystemAccess(),
            )
            kwargs = _options_kwargs_for(request, None)
            hooks = kwargs.get("hooks") or {}
            pre = hooks.get("PreToolUse") or []
            self.assertEqual(len(pre), 1)

    def test_packaged_bash_guard_blocks_bare_host_tools(self) -> None:
        import asyncio as _asyncio

        from ai_mime.agent_runner.adapters import claude_sdk

        with patch.object(claude_sdk, "is_frozen", return_value=True):
            hook = claude_sdk._build_packaged_bash_guard_hook()
        assert hook is not None

        async def _call(command: str) -> dict:
            return await hook({"tool_name": "Bash", "tool_input": {"command": command}}, "tid", None)

        for command in (
            "uv --version",
            "python3 scripts/run.py",
            "browser-harness -c 'print(1)'",
            "uvx some-tool",
            "npx some-tool",
            "/opt/homebrew/bin/uv --version",
            "/usr/local/bin/python3 --version",
        ):
            out = _asyncio.run(_call(command))
            self.assertEqual(out.get("decision"), "block", command)
            self.assertIn("packaged mode", out.get("reason", "") + " packaged mode")

    def test_packaged_bash_guard_allows_explicit_app_tools_and_venv(self) -> None:
        import asyncio as _asyncio

        from ai_mime.agent_runner.adapters import claude_sdk

        with patch.object(claude_sdk, "is_frozen", return_value=True):
            hook = claude_sdk._build_packaged_bash_guard_hook()
        assert hook is not None

        async def _call(command: str) -> dict:
            return await hook({"tool_name": "Bash", "tool_input": {"command": command}}, "tid", None)

        for command in (
            '"$AI_MIME_UV_PATH" --version',
            '"$AI_MIME_BROWSER_HARNESS_BIN" -c "print(1)"',
            '"$AI_MIME_PYTHON_PATH" scripts/run.py',
            './.venv/bin/python scripts/run.py',
            'cd /tmp && "$AI_MIME_UV_PATH" --version',
            # Host paths mentioned only inside a string arg must not be blocked.
            '"$AI_MIME_PYTHON_PATH" -c "print(\'/usr/local/lib\')"',
            'echo "see /opt/homebrew/bin" && "$AI_MIME_UV_PATH" --version',
        ):
            out = _asyncio.run(_call(command))
            self.assertEqual(out, {}, command)

    def test_options_kwargs_installs_packaged_bash_guard_when_frozen(self) -> None:
        from ai_mime.agent_runner.adapters import claude_sdk
        from ai_mime.agent_runner.adapters.claude_sdk import _options_kwargs_for
        from ai_mime.agent_runner.models import FilesystemAccess

        with tempfile.TemporaryDirectory() as td, patch.object(claude_sdk, "is_frozen", return_value=True):
            workflow_dir = Path(td)
            request = AgentRunRequest(
                provider="claude",
                mode="build_skill_chat",
                workflow_dir=workflow_dir,
                workspace_dir=workflow_dir,
                readable_roots=[workflow_dir],
                writable_roots=[workflow_dir],
                user_filesystem_access=FilesystemAccess(),
            )
            kwargs = _options_kwargs_for(request, None)
            hooks = kwargs.get("hooks") or {}
            pre = hooks.get("PreToolUse") or []
            self.assertEqual(len(pre), 2)
            # The app-managed runtime env must be wired onto the SDK options so the
            # Bash tool resolves $AI_MIME_* in both run and stream_chat paths.
            self.assertIn("AI_MIME_PYTHON_PATH", kwargs.get("env") or {})
            self.assertIn("AI_MIME_UV_PATH", kwargs.get("env") or {})

    def test_options_kwargs_enables_claude_auto_compaction(self) -> None:
        from claude_agent_sdk import ClaudeAgentOptions

        from ai_mime.agent_runner.adapters.claude_sdk import (
            AUTO_COMPACT_TOKEN_THRESHOLD,
            _options_kwargs_for,
        )

        with tempfile.TemporaryDirectory() as td:
            workflow_dir = Path(td)
            request = AgentRunRequest(
                provider="claude",
                mode="build_skill_chat",
                workflow_dir=workflow_dir,
                workspace_dir=workflow_dir,
            )
            kwargs = _options_kwargs_for(request, None)
            settings = json.loads(kwargs["settings"])

            self.assertIs(settings["autoCompactEnabled"], True)
            self.assertEqual(settings["autoCompactWindow"], AUTO_COMPACT_TOKEN_THRESHOLD)
            ClaudeAgentOptions(**kwargs)

    def test_build_skill_chat_request_attaches_cua_mcp_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workflow_dir = Path(td)
            (workflow_dir / "schema.json").write_text(json.dumps(_schema()), encoding="utf-8")
            (workflow_dir / "optimized_plan.json").write_text(json.dumps(_optimized_plan()), encoding="utf-8")
            prev = os.environ.pop("AI_MIME_MCP_SERVERS_JSON", None)
            try:
                request = build_agent_run_request(
                    workflow_dir=workflow_dir, provider="claude", mode="build_skill_chat"
                )
            finally:
                if prev is not None:
                    os.environ["AI_MIME_MCP_SERVERS_JSON"] = prev
            self.assertEqual(request.mcp_servers, cua_mcp_servers())

    def test_build_skill_chat_request_reads_mcp_servers_from_env(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workflow_dir = Path(td)
            (workflow_dir / "schema.json").write_text(json.dumps(_schema()), encoding="utf-8")
            (workflow_dir / "optimized_plan.json").write_text(json.dumps(_optimized_plan()), encoding="utf-8")
            payload = {"hello": {"type": "stdio", "command": "echo", "args": ["mcp"]}}
            prev = os.environ.get("AI_MIME_MCP_SERVERS_JSON")
            os.environ["AI_MIME_MCP_SERVERS_JSON"] = json.dumps(payload)
            try:
                request = build_agent_run_request(
                    workflow_dir=workflow_dir, provider="claude", mode="build_skill_chat"
                )
            finally:
                if prev is None:
                    os.environ.pop("AI_MIME_MCP_SERVERS_JSON", None)
                else:
                    os.environ["AI_MIME_MCP_SERVERS_JSON"] = prev
            self.assertEqual(request.mcp_servers, {**payload, **cua_mcp_servers()})

    def test_build_skill_chat_request_ignores_invalid_mcp_env(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workflow_dir = Path(td)
            (workflow_dir / "schema.json").write_text(json.dumps(_schema()), encoding="utf-8")
            (workflow_dir / "optimized_plan.json").write_text(json.dumps(_optimized_plan()), encoding="utf-8")
            prev = os.environ.get("AI_MIME_MCP_SERVERS_JSON")
            os.environ["AI_MIME_MCP_SERVERS_JSON"] = "not-json{"
            try:
                request = build_agent_run_request(
                    workflow_dir=workflow_dir, provider="claude", mode="build_skill_chat"
                )
            finally:
                if prev is None:
                    os.environ.pop("AI_MIME_MCP_SERVERS_JSON", None)
                else:
                    os.environ["AI_MIME_MCP_SERVERS_JSON"] = prev
            self.assertEqual(request.mcp_servers, cua_mcp_servers())

    def test_workspace_chat_service_persists_returned_session_id(self) -> None:
        prompts: list[str] = []
        models: list[str | None] = []
        system_prompts: list[str | None] = []

        class ChatAdapter:
            id = "claude_code"

            def run(self, request: AgentRunRequest, prompt: str) -> AgentRunResult:
                prompts.append(prompt)
                models.append(request.model)
                system_prompts.append(request.system_prompt)
                return AgentRunResult(status="success", session_id="claude-session-1", summary="hello")

        with tempfile.TemporaryDirectory() as td:
            service = WorkspaceAgentChatService(
                workspace_dir=Path(td),
                adapter=ChatAdapter(),
                session_lister=lambda _dir: [],
                message_loader=lambda _sid, _dir: [],
            )
            result = service.chat(message="hello", model="sonnet")

            self.assertEqual(result["session_id"], "claude-session-1")
            self.assertEqual(result["model"], "sonnet")
            self.assertEqual(models, ["sonnet"])
            self.assertEqual(prompts[0], "hello")
            index = json.loads((Path(td) / ".agent" / "agent_sessions.json").read_text(encoding="utf-8"))
            self.assertIn("claude-session-1", index)
            self.assertEqual(index["claude-session-1"]["model"], "sonnet")

    def test_workspace_chat_service_sends_initial_context_only_for_new_session(self) -> None:
        prompts: list[str] = []
        system_prompts: list[str | None] = []

        class ChatAdapter:
            id = "claude_code"

            def run(self, request: AgentRunRequest, prompt: str) -> AgentRunResult:
                prompts.append(prompt)
                system_prompts.append(request.system_prompt)
                return AgentRunResult(status="success", session_id=request.session_id or "new-session", summary="ok")

        with tempfile.TemporaryDirectory() as td:
            service = WorkspaceAgentChatService(
                workspace_dir=Path(td),
                adapter=ChatAdapter(),
                session_lister=lambda _dir: [],
                message_loader=lambda _sid, _dir: [],
            )
            service.chat(message="first")
            service.chat(message="second", session_id="new-session")

            self.assertEqual(prompts[0], "first")
            self.assertIn("AI Mime workspace debugging agent", system_prompts[0] or "")
            self.assertEqual(prompts[1], "second")
            self.assertIsNone(system_prompts[1])

    def test_workspace_chat_service_uses_config_model_even_when_request_model_is_sent(self) -> None:
        captured: dict[str, AgentRunRequest] = {}

        class ChatAdapter:
            id = "claude_code"

            def run(self, request: AgentRunRequest, prompt: str) -> AgentRunResult:
                captured["request"] = request
                return AgentRunResult(status="success", session_id="configured-session", summary="ok")

        with tempfile.TemporaryDirectory() as td, patch(
            "ai_mime.agent_runner.chat.load_user_config",
            return_value=SimpleNamespace(
                agent_runtime="claude_code",
                agents=_agents_config(workspace_chat_model="anthropic/claude-sonnet-4-6"),
            ),
        ), patch("ai_mime.agent_runner.chat.get_agent_runtime", return_value=ChatAdapter()):
            service = WorkspaceAgentChatService(
                workspace_dir=Path(td),
                session_lister=lambda _dir: [],
                message_loader=lambda _sid, _dir: [],
            )
            result = service.chat(message="hello", model="not-a-model")

        self.assertEqual(result["status"], "success")
        self.assertEqual(captured["request"].model, "claude-sonnet-4-6")

    def test_workspace_chat_service_lists_and_loads_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            service = WorkspaceAgentChatService(
                workspace_dir=Path(td),
                adapter=FakeAdapter(),
                session_lister=lambda _dir: [{"session_id": "old", "summary": "Older chat"}],
                message_loader=lambda sid, _dir: [{"type": "user", "session_id": sid, "message": "hi"}],
            )

            self.assertEqual(service.list_sessions()[0]["session_id"], "old")
            self.assertEqual(service.load_messages("old")[0]["message"], "hi")

    def test_workspace_chat_service_accepts_sequential_recovery_turns(self) -> None:
        session_ids: list[str | None] = []

        class ChatAdapter:
            id = "claude_code"

            def run(self, request: AgentRunRequest, prompt: str) -> AgentRunResult:
                session_ids.append(request.session_id)
                return AgentRunResult(status="success", session_id=request.session_id or "workspace-session-1", summary="ok")

        with tempfile.TemporaryDirectory() as td:
            service = WorkspaceAgentChatService(
                workspace_dir=Path(td),
                adapter=ChatAdapter(),
                session_lister=lambda _dir: [],
                message_loader=lambda _sid, _dir: [],
            )
            first = service.chat(message="hello")
            second = service.chat(message="hello again", session_id=first["session_id"])

            self.assertEqual(second["session_id"], "workspace-session-1")
            self.assertEqual(session_ids, [None, "workspace-session-1"])

    def test_workspace_chat_stream_runs_without_turn_lock(self) -> None:
        class StreamAdapter:
            id = "claude_code"

            async def stream_chat(self, *_args, **_kwargs):
                yield {"event": "text", "text": "ok"}
                yield {"event": "done", "status": "success", "session_id": "workspace-session-1", "summary": "ok"}

        async def collect_events(service: WorkspaceAgentChatService) -> list[dict]:
            return [event async for event in service.chat_stream(message="continue workspace")]

        with tempfile.TemporaryDirectory() as td:
            service = WorkspaceAgentChatService(
                workspace_dir=Path(td),
                adapter=StreamAdapter(),
                session_lister=lambda _dir: [],
                message_loader=lambda _sid, _dir: [],
            )

            events = asyncio.run(collect_events(service))

            self.assertEqual(events[-1]["event"], "done")
            self.assertEqual(events[-1]["session_id"], "workspace-session-1")

    @patch("ai_mime.agent_runner.adapters.claude_sdk.list_sessions", return_value=[])
    @patch("ai_mime.agent_runner.adapters.claude_sdk.query")
    def test_claude_adapter_clears_invalid_session_id(self, mock_query, mock_list_sessions) -> None:
        from ai_mime.agent_runner.adapters.claude_sdk import ClaudeAgentSdkAdapter
        
        async def mock_query_gen(*args, **kwargs):
            from claude_agent_sdk import ResultMessage
            msg = ResultMessage(
                subtype="success",
                duration_ms=0,
                duration_api_ms=0,
                is_error=False,
                num_turns=1,
                session_id="new-generated-session-id",
                result="success",
            )
            yield msg
            
        mock_query.return_value = mock_query_gen()
        
        with tempfile.TemporaryDirectory() as td:
            request = AgentRunRequest(
                provider="claude",
                mode="general",
                workflow_dir=Path(td),
                workspace_dir=Path(td),
                session_id="stale-session-id",
            )
            adapter = ClaudeAgentSdkAdapter()
            result = adapter.run(request, "hello")
            self.assertEqual(result.status, "success")
            mock_list_sessions.assert_called_once_with(directory=str(Path(td)))
            called_options = mock_query.call_args[1]["options"]
            self.assertIsNone(called_options.resume)

    def test_agent_runtime_registry_resolves_claude_and_codex(self) -> None:
        from ai_mime.agent_runner.adapters.registry import available_agent_runtimes, get_agent_runtime

        runtime_ids = {item.id for item in available_agent_runtimes()}

        self.assertIn("claude_code", runtime_ids)
        self.assertIn("codex_cli", runtime_ids)
        self.assertEqual(get_agent_runtime("claude_code").id, "claude_code")
        self.assertEqual(get_agent_runtime("codex_cli").id, "codex_cli")
        with self.assertRaisesRegex(ValueError, "Unknown agent runtime"):
            get_agent_runtime("not-real")

    def test_workspace_chat_service_defaults_to_configured_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch(
            "ai_mime.agent_runner.chat.load_user_config",
            return_value=SimpleNamespace(
                agent_runtime="codex_cli",
                agents=_agents_config(
                    workspace_chat_model="openai/gpt-config-agent",
                    workspace_chat_runtime="codex_cli",
                ),
            ),
        ):
            service = WorkspaceAgentChatService(workspace_dir=Path(td))

        self.assertEqual(service.runtime_id, "codex_cli")
        self.assertEqual(service.adapter.id, "codex_cli")
        self.assertEqual(service.model_options, [
            {"id": "openai/gpt-config-agent", "label": "openai/gpt-config-agent", "description": "Configured in user_config.yml."}
        ])

    def test_workspace_chat_uses_workspace_chat_model_and_strips_provider_prefix(self) -> None:
        captured: dict[str, AgentRunRequest] = {}

        class Runtime:
            id = "codex_cli"

            def run(self, request: AgentRunRequest, prompt: str) -> AgentRunResult:
                captured["request"] = request
                return AgentRunResult(status="success", session_id="workspace-session", summary="ok")

        with tempfile.TemporaryDirectory() as td, patch(
            "ai_mime.agent_runner.chat.load_user_config",
            return_value=SimpleNamespace(
                provider="openai",
                agent_runtime="codex_cli",
                agents=_agents_config(
                    workspace_chat_model="openai/gpt-workspace",
                    workspace_chat_runtime="codex_cli",
                ),
            ),
        ), patch("ai_mime.agent_runner.chat.get_agent_runtime", return_value=Runtime()):
            service = WorkspaceAgentChatService(
                workspace_dir=Path(td),
                session_lister=lambda _dir: [],
                message_loader=lambda _sid, _dir: [],
            )
            result = service.chat(message="hello")

        self.assertEqual(result["status"], "success")
        self.assertEqual(captured["request"].model, "gpt-workspace")

    def test_replay_chat_uses_replay_model(self) -> None:
        captured: dict[str, AgentRunRequest] = {}

        class Runtime:
            id = "claude_code"

            def run(self, request: AgentRunRequest, prompt: str) -> AgentRunResult:
                captured["request"] = request
                return AgentRunResult(status="success", session_id="replay-session", summary="ok")

        with tempfile.TemporaryDirectory() as td:
            workflow_dir = Path(td)
            (workflow_dir / "schema.json").write_text(json.dumps(_schema()), encoding="utf-8")
            (workflow_dir / "optimized_plan.json").write_text(json.dumps(_optimized_plan()), encoding="utf-8")
            with patch(
                "ai_mime.agent_runner.chat.load_user_config",
                return_value=SimpleNamespace(
                    provider="anthropic",
                    agent_runtime="claude_code",
                    agents=_agents_config(
                        replay_model="anthropic/claude-replay",
                        replay_runtime="claude_code",
                    ),
                ),
            ), patch("ai_mime.agent_runner.chat.get_agent_runtime", return_value=Runtime()):
                service = WorkspaceAgentChatService(
                    workspace_dir=workflow_dir,
                    mode="replay_execution",
                    session_lister=lambda _dir: [],
                    message_loader=lambda _sid, _dir: [],
                )
                service.chat(message="rerun")

        self.assertEqual(captured["request"].mode, "replay_execution")
        self.assertEqual(captured["request"].model, "claude-replay")

    def test_skill_build_uses_skill_build_model(self) -> None:
        from ai_mime.agent_runner.skill_build_chat import WorkflowSkillBuildService

        captured: dict[str, AgentRunRequest] = {}

        class Runtime:
            id = "codex_cli"

            def stream_chat(self, request: AgentRunRequest, prompt: str, **_kwargs):  # type: ignore[no-untyped-def]
                async def events():
                    captured["request"] = request
                    yield {"event": "done", "status": "success", "session_id": "skill-session", "summary": "ok"}

                return events()

        async def collect(service: WorkflowSkillBuildService) -> list[dict[str, object]]:
            return [event async for event in service.chat_stream(message="build")]

        with tempfile.TemporaryDirectory() as td:
            workflow_dir = Path(td)
            (workflow_dir / "schema.json").write_text(json.dumps(_schema()), encoding="utf-8")
            (workflow_dir / "optimized_plan.json").write_text(json.dumps(_optimized_plan()), encoding="utf-8")
            with patch(
                "ai_mime.agent_runner.skill_build_chat.load_user_config",
                return_value=SimpleNamespace(
                    provider="openai",
                    agent_runtime="codex_cli",
                    agents=_agents_config(
                        skill_build_model="openai/gpt-skill",
                        skill_build_runtime="codex_cli",
                    ),
                ),
            ), patch("ai_mime.agent_runner.chat.get_agent_runtime", return_value=Runtime()):
                service = WorkflowSkillBuildService(
                    workflow_dir=workflow_dir,
                    session_lister=lambda _dir: [],
                    message_loader=lambda _sid, _dir: [],
                )
                asyncio.run(collect(service))

        self.assertEqual(captured["request"].mode, "build_skill_chat")
        self.assertEqual(captured["request"].model, "gpt-skill")

    def test_codex_cli_builds_new_and_resume_commands(self) -> None:
        from ai_mime.agent_runner.adapters.codex_cli import CodexCliRuntime

        runtime = CodexCliRuntime(codex_path="/bin/codex")
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            request = AgentRunRequest(
                provider="codex_cli",
                mode="general",
                model="gpt-test",
                workflow_dir=workspace,
                workspace_dir=workspace,
                mcp_servers={"cua": {"type": "http", "url": "http://127.0.0.1:58840/mcp/"}},
            )
            schema_path = workspace / "schema.json"
            output_path = workspace / "last.txt"

            cmd = runtime.build_command(
                request,
                "hello",
                output_schema_path=schema_path,
                output_last_message_path=output_path,
            )

            self.assertEqual(cmd[:2], ["/bin/codex", "exec"])
            self.assertIn("--json", cmd)
            self.assertIn("--cd", cmd)
            self.assertIn(str(workspace), cmd)
            self.assertIn("--sandbox", cmd)
            self.assertIn("workspace-write", cmd)
            self.assertIn("--skip-git-repo-check", cmd)
            self.assertIn("-c", cmd)
            self.assertIn('mcp_servers.cua.url="http://127.0.0.1:58840/mcp/"', cmd)
            self.assertIn("mcp_servers.cua.required=true", cmd)
            self.assertIn('mcp_servers.cua.default_tools_approval_mode="auto"', cmd)
            self.assertIn("-m", cmd)
            self.assertIn("gpt-test", cmd)
            self.assertIn("--output-schema", cmd)
            self.assertIn(str(schema_path), cmd)
            self.assertIn("-o", cmd)
            self.assertIn(str(output_path), cmd)
            self.assertEqual(cmd[-1], "-")

            resume_request = request.model_copy(update={"session_id": "codex-session"})
            resume_cmd = runtime.build_command(resume_request, "continue")
            self.assertEqual(resume_cmd[:4], ["/bin/codex", "exec", "resume", "codex-session"])
            self.assertIn("--json", resume_cmd)
            self.assertIn("-m", resume_cmd)
            self.assertEqual(resume_cmd[-1], "-")

    def test_codex_cli_builds_stdio_mcp_config(self) -> None:
        from ai_mime.agent_runner.adapters.codex_cli import CodexCliRuntime

        runtime = CodexCliRuntime(codex_path="/bin/codex")
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            request = AgentRunRequest(
                provider="codex_cli",
                mode="general",
                workflow_dir=workspace,
                workspace_dir=workspace,
                mcp_servers={"hello": {"type": "stdio", "command": "echo", "args": ["one", "two"]}},
            )

            cmd = runtime.build_command(request, "hello")

        self.assertIn('mcp_servers.hello.command="echo"', cmd)
        self.assertIn('mcp_servers.hello.args=["one", "two"]', cmd)
        self.assertIn("mcp_servers.hello.required=true", cmd)

    def test_codex_cli_rejects_unsupported_mcp_config(self) -> None:
        from ai_mime.agent_runner.adapters.codex_cli import CodexCliRuntime

        runtime = CodexCliRuntime(codex_path="/bin/codex")
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            request = AgentRunRequest(
                provider="codex_cli",
                mode="general",
                workflow_dir=workspace,
                workspace_dir=workspace,
                mcp_servers={"bad": {"type": "sse", "url": "http://example.com"}},
            )

            with self.assertRaisesRegex(RuntimeError, "Unsupported Codex MCP server"):
                runtime.build_command(request, "hello")

    def test_codex_jsonl_maps_to_agent_events(self) -> None:
        from ai_mime.agent_runner.adapters.codex_cli import parse_codex_jsonl

        events = parse_codex_jsonl(
            [
                json.dumps({"msg": {"type": "text", "content": "hello"}}),
                json.dumps({"msg": {"type": "tool_use", "id": "t1", "name": "shell", "input": {"command": "ls"}}}),
                json.dumps({"msg": {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}}),
                json.dumps({"msg": {"type": "done", "session_id": "s1", "summary": "finished"}}),
                "not-json",
            ]
        )

        self.assertEqual(events[0], {"event": "text", "text": "hello"})
        self.assertEqual(events[1]["event"], "tool_use")
        self.assertEqual(events[1]["name"], "shell")
        self.assertEqual(events[2]["event"], "tool_result")
        self.assertEqual(events[3]["event"], "done")
        self.assertEqual(events[3]["session_id"], "s1")

    def test_codex_runtime_does_not_require_openai_api_key(self) -> None:
        from ai_mime.agent_runner.adapters.codex_cli import CodexCliRuntime

        class FakePopen:
            returncode = 0

            def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                self.args = args
                self.kwargs = kwargs
                self.received_input = None

            def communicate(self, input=None):  # type: ignore[no-untyped-def]
                self.received_input = input
                return ('{"msg":{"type":"done","session_id":"codex-session","summary":"ok"}}\n', "")

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            request = AgentRunRequest(
                provider="codex_cli",
                mode="general",
                workflow_dir=workspace,
                workspace_dir=workspace,
            )
            runtime = CodexCliRuntime(codex_path="/bin/codex")
            with patch.dict(os.environ, {}, clear=True), patch(
                "ai_mime.agent_runner.adapters.codex_cli.subprocess.Popen",
                side_effect=FakePopen,
            ) as popen:
                result = runtime.run(request, "hello")

        self.assertEqual(result.status, "success")
        self.assertEqual(result.session_id, "codex-session")
        popen.assert_called_once()
        self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.PIPE)
        self.assertEqual(popen.call_args.args[0][-1], "-")

    def test_codex_runtime_interrupts_active_process(self) -> None:
        from ai_mime.agent_runner.adapters.codex_cli import CodexCliRuntime

        class FakeProcess:
            returncode = None

            def __init__(self) -> None:
                self.signals: list[int] = []

            def send_signal(self, sig: int) -> None:
                self.signals.append(sig)

        runtime = CodexCliRuntime(codex_path="/bin/codex")
        process = FakeProcess()
        runtime._active_process = process  # type: ignore[assignment]

        self.assertFalse(CodexCliRuntime(codex_path="/bin/codex").interrupt())
        self.assertTrue(runtime.interrupt())
        self.assertEqual(process.signals, [2])

    def test_codex_runtime_lists_sessions_from_codex_home(self) -> None:
        from ai_mime.agent_runner.adapters.codex_cli import CodexCliRuntime

        with tempfile.TemporaryDirectory() as td:
            codex_home = Path(td) / "codex-home"
            workspace = Path(td) / "workspace"
            other_workspace = Path(td) / "other"
            sessions_dir = codex_home / "sessions" / "2026" / "06" / "01"
            sessions_dir.mkdir(parents=True)
            workspace.mkdir()
            other_workspace.mkdir()
            (codex_home / "session_index.jsonl").write_text(
                "\n".join([
                    json.dumps({"id": "old-session", "thread_name": "Old", "updated_at": "2026-06-01T01:00:00Z"}),
                    "not-json",
                    json.dumps({"id": "new-session", "thread_name": "New", "updated_at": "2026-06-01T02:00:00Z"}),
                    json.dumps({"id": "other-session", "thread_name": "Other", "updated_at": "2026-06-01T03:00:00Z"}),
                ]),
                encoding="utf-8",
            )
            (sessions_dir / "rollout-new-session.jsonl").write_text(
                json.dumps({"type": "session_meta", "payload": {"id": "new-session", "cwd": str(workspace)}}) + "\n",
                encoding="utf-8",
            )
            (sessions_dir / "rollout-old-session.jsonl").write_text(
                json.dumps({"type": "session_meta", "payload": {"id": "old-session", "cwd": str(workspace)}}) + "\n",
                encoding="utf-8",
            )
            (sessions_dir / "rollout-other-session.jsonl").write_text(
                json.dumps({"type": "session_meta", "payload": {"id": "other-session", "cwd": str(other_workspace)}}) + "\n",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                sessions = CodexCliRuntime(codex_path="/bin/codex").list_sessions(workspace)

        self.assertEqual([item["session_id"] for item in sessions], ["new-session", "old-session"])
        self.assertEqual(sessions[0]["summary"], "New")
        self.assertEqual(sessions[0]["source"], "codex")
        self.assertEqual(sessions[0]["last_modified"], "2026-06-01T02:00:00Z")

    def test_codex_runtime_loads_visible_messages_from_transcript(self) -> None:
        from ai_mime.agent_runner.adapters.codex_cli import CodexCliRuntime

        with tempfile.TemporaryDirectory() as td:
            codex_home = Path(td) / "codex-home"
            workspace = Path(td) / "workspace"
            session_file = codex_home / "sessions" / "2026" / "06" / "01" / "rollout-test-session.jsonl"
            session_file.parent.mkdir(parents=True)
            workspace.mkdir()
            session_file.write_text(
                "\n".join([
                    json.dumps({"type": "session_meta", "payload": {"id": "test-session", "cwd": str(workspace)}}),
                    json.dumps({"type": "turn_context", "payload": {"cwd": str(workspace)}}),
                    json.dumps({"type": "response_item", "payload": {"type": "message", "role": "developer", "content": "hidden"}}),
                    json.dumps({
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "id": "u1",
                            "content": [{"type": "input_text", "text": "hello"}],
                        },
                    }),
                    json.dumps({"type": "response_item", "payload": {"type": "reasoning", "summary": []}}),
                    json.dumps({
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "id": "a1",
                            "content": [{"type": "output_text", "text": "hi there"}],
                        },
                    }),
                    json.dumps({"type": "event_msg", "payload": {"msg": {"type": "tool_call"}}}),
                    "not-json",
                ]),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                messages = CodexCliRuntime(codex_path="/bin/codex").load_messages("test-session", workspace)

        self.assertEqual(
            messages,
            [
                {"type": "user", "role": "user", "uuid": "u1", "session_id": "test-session", "message": "hello"},
                {"type": "assistant", "role": "assistant", "uuid": "a1", "session_id": "test-session", "message": "hi there"},
            ],
        )

    def test_codex_runtime_missing_session_files_return_empty_lists(self) -> None:
        from ai_mime.agent_runner.adapters.codex_cli import CodexCliRuntime

        with tempfile.TemporaryDirectory() as td:
            codex_home = Path(td) / "missing-codex-home"
            workspace = Path(td) / "workspace"
            workspace.mkdir()
            with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                runtime = CodexCliRuntime(codex_path="/bin/codex")
                self.assertEqual(runtime.list_sessions(workspace), [])
                self.assertEqual(runtime.load_messages("missing-session", workspace), [])

    def test_codex_runtime_reports_missing_binary(self) -> None:
        from ai_mime.agent_runner.adapters.codex_cli import CodexCliRuntime

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            request = AgentRunRequest(
                provider="codex_cli",
                mode="general",
                workflow_dir=workspace,
                workspace_dir=workspace,
            )
            runtime = CodexCliRuntime()
            with patch.dict(os.environ, {"OPENAI_API_KEY": "secret"}, clear=True), patch(
                "ai_mime.agent_runner.adapters.codex_cli.shutil.which",
                return_value=None,
            ):
                result = runtime.run(request, "hello")

        self.assertEqual(result.status, "failed")
        self.assertIn("Codex CLI not found", result.error or "")

    def test_computer_use_uses_configured_claude_model(self) -> None:
        from ai_mime.agent_runner.computer_use import run_computer_use_task

        captured: dict[str, object] = {}

        async def fake_runtime(task: str, *, runtime_id: str, model: str, response_schema=None):  # type: ignore[no-untyped-def]
            captured.update({
                "task": task,
                "runtime_id": runtime_id,
                "model": model,
                "response_schema": response_schema,
            })
            return AgentRunResult(status="success", session_id="cua-session", summary="done")

        cfg = SimpleNamespace(
            provider="anthropic",
            agents=_agents_config(computer_use_model="anthropic/claude-opus-4-8", computer_use_runtime="claude_code"),
        )
        with patch("ai_mime.agent_runner.computer_use.load_user_config", return_value=cfg), patch(
            "ai_mime.agent_runner.computer_use._run_agent_runtime_computer_use_task_async",
            side_effect=fake_runtime,
        ):
            result = run_computer_use_task("open Safari")

        self.assertEqual(result.status, "success")
        self.assertEqual(captured["task"], "open Safari")
        self.assertEqual(captured["runtime_id"], "claude_code")
        self.assertEqual(captured["model"], "anthropic/claude-opus-4-8")

    def test_computer_use_openai_config_routes_to_codex(self) -> None:
        from ai_mime.agent_runner.computer_use import run_computer_use_task

        captured: dict[str, object] = {}

        async def fake_runtime(task: str, *, runtime_id: str, model: str, response_schema=None):  # type: ignore[no-untyped-def]
            captured.update({
                "task": task,
                "runtime_id": runtime_id,
                "model": model,
                "response_schema": response_schema,
            })
            return AgentRunResult(status="success", session_id="codex-cua-session", summary='{"ok": true}')

        cfg = SimpleNamespace(
            provider="openai",
            agents=_agents_config(computer_use_model="openai/gpt-5.5", computer_use_runtime="codex_cli"),
        )
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
        with patch("ai_mime.agent_runner.computer_use.load_user_config", return_value=cfg), patch(
            "ai_mime.agent_runner.computer_use._run_agent_runtime_computer_use_task_async",
            side_effect=fake_runtime,
        ):
            result = run_computer_use_task("inspect", response_schema=schema)

        self.assertEqual(result.status, "success")
        self.assertEqual(captured["runtime_id"], "codex_cli")
        self.assertEqual(captured["model"], "openai/gpt-5.5")
        self.assertEqual(captured["response_schema"], schema)

    def test_computer_use_runtime_request_strips_provider_prefix_and_attaches_mcp(self) -> None:
        from ai_mime.agent_runner.computer_use import _run_agent_runtime_computer_use_task_async

        captured: dict[str, object] = {}

        class FakeRuntime:
            async def stream_chat(self, request: AgentRunRequest, prompt: str, **kwargs):  # type: ignore[no-untyped-def]
                captured["request"] = request
                captured["prompt"] = prompt
                captured["kwargs"] = kwargs
                yield {"event": "text", "text": '{"ok": true}'}
                yield {"event": "tool_use", "id": "tool-1", "name": "computer_get_window_state", "input": {}}
                yield {"event": "tool_result", "tool_use_id": "tool-1", "content": "ok", "is_error": False}
                yield {"event": "done", "session_id": "codex-cua-session", "status": "success", "summary": '{"ok": true}'}

        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
        with patch("ai_mime.agent_runner.computer_use.get_agent_runtime", return_value=FakeRuntime()):
            result = asyncio.run(
                _run_agent_runtime_computer_use_task_async(
                    "inspect",
                    runtime_id="codex_cli",
                    model="openai/gpt-5.5",
                    response_schema=schema,
                )
            )

        request = captured["request"]
        self.assertIsInstance(request, AgentRunRequest)
        assert isinstance(request, AgentRunRequest)
        self.assertEqual(request.provider, "codex_cli")
        self.assertEqual(request.model, "gpt-5.5")
        self.assertEqual(request.mcp_servers, cua_mcp_servers())
        self.assertIn("You drive this macOS computer", str(captured["prompt"]))
        self.assertIn("can_use_tool", captured["kwargs"])
        self.assertTrue(any("tool_use: computer_get_window_state" in line for line in result.logs))
        self.assertFalse(any("tool_result:" in line for line in result.logs))
        self.assertEqual(result.result_json, {"ok": True})


if __name__ == "__main__":
    unittest.main()
