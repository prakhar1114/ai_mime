from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
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
from ai_mime.app_data import get_bundled_resource


def _default_browser_skill_root() -> Path:
    return get_bundled_resource("harness/browser-harness")


def _default_macos_skill_root() -> Path:
    return get_bundled_resource("resources/claude-skills/macos-computer-use")


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
        "## ask_gemini decision points\nNone.\n\n"
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
        'exec python3 "$HERE/scripts/run.py" --inputs-json "$INPUTS"\n',
        encoding="utf-8",
    )
    run_sh.chmod(run_sh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class FakeAdapter:
    def __init__(self) -> None:
        self.request: AgentRunRequest | None = None
        self.prompt: str | None = None
        self.runtime_env: dict[str, str | None] = {}

    def run(self, request: AgentRunRequest, prompt: str) -> AgentRunResult:
        self.request = request
        self.prompt = prompt
        self.runtime_env = {
            "AI_MIME_PYTHON_PATH": os.environ.get("AI_MIME_PYTHON_PATH"),
            "AI_MIME_UV_PATH": os.environ.get("AI_MIME_UV_PATH"),
        }
        return AgentRunResult(
            status="success",
            session_id=request.session_id or "",
            summary="Fake agent completed the optimized plan.",
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
        self.assertIn(_default_macos_skill_root(), request.readable_roots)
        self.assertNotIn(_default_browser_skill_root(), request.writable_roots)
        self.assertNotIn(_default_macos_skill_root(), request.writable_roots)

    def test_build_request_merges_user_read_hints_and_default_writes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workflow_dir = Path(td)
            (workflow_dir / "schema.json").write_text(json.dumps(_schema()), encoding="utf-8")
            (workflow_dir / "optimized_plan.json").write_text(json.dumps(_optimized_plan()), encoding="utf-8")

            request = build_agent_run_request(workflow_dir=workflow_dir, provider="claude")

        self.assertIn(Path("/Users/prakharjain/Desktop/expenses"), request.readable_roots)
        self.assertIn(Path("/tmp"), request.readable_roots)
        self.assertIn(Path("/tmp"), request.writable_roots)
        self.assertIn(_default_browser_skill_root(), request.readable_roots)
        self.assertIn(_default_macos_skill_root(), request.readable_roots)
        self.assertNotIn(_default_browser_skill_root(), request.writable_roots)
        self.assertNotIn(_default_macos_skill_root(), request.writable_roots)
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
        self.assertIn(_default_macos_skill_root(), request.readable_roots)
        self.assertNotIn(_default_browser_skill_root(), request.writable_roots)
        self.assertNotIn(_default_macos_skill_root(), request.writable_roots)

    def test_agent_request_uses_env_configured_skill_paths(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            browser_skill = root / "browser-harness"
            macos_skill = root / "macos-computer-use"
            browser_skill.mkdir()
            macos_skill.mkdir()
            env = {
                "AI_MIME_BROWSER_SKILL_NAME": "browser",
                "AI_MIME_BROWSER_SKILL_PATH": str(browser_skill),
                "AI_MIME_MACOS_COMPUTER_USE_SKILL_NAME": "macos-computer-use",
                "AI_MIME_MACOS_COMPUTER_USE_SKILL_PATH": str(macos_skill),
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
            self.assertIn(macos_skill.resolve(), request.readable_roots)
            self.assertNotIn(browser_skill.resolve(), request.writable_roots)
            self.assertNotIn(macos_skill.resolve(), request.writable_roots)
            prompt = adapter.prompt or ""
            self.assertIn(str(browser_skill.resolve()), prompt)
            self.assertIn(str(macos_skill.resolve()), prompt)

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
                build_agent_run_request(workflow_dir=Path(td), provider="claude")

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
            # Core protocol elements
            self.assertIn("build_signal.json", prompt)
            self.assertIn("skill_ready", prompt)
            self.assertIn("skill_unbuildable", prompt)
            self.assertIn("ask_gemini", prompt)
            # Executor taxonomy
            self.assertIn("script", prompt)
            self.assertIn("browser_harness", prompt)
            self.assertIn("ui_agent", prompt)
            # Four-phase protocol
            self.assertIn("Phase A", prompt)
            self.assertIn("Phase B", prompt)
            self.assertIn("Phase C", prompt)
            self.assertIn("Phase D", prompt)
            # Non-technical, autonomy-first chat behavior
            self.assertIn("The end user is not technical", prompt)
            self.assertIn("Ask only important questions", prompt)
            self.assertIn("Do NOT ask for confirmation before each step", prompt)
            self.assertIn("plain-language", prompt)
            self.assertIn("expected outputs", prompt)
            self.assertIn("very high-level", prompt)
            self.assertIn("do not ask for packaging approval", prompt)
            self.assertIn("Do not pause after successful individual steps", prompt)
            self.assertNotIn("Continue?", prompt)
            self.assertNotIn("Ready to package and create the skill", prompt)
            self.assertNotIn("advance only after explicit user OK", prompt)
            # Inputs editing
            self.assertIn("task_params", prompt)
            self.assertIn("inputs[]", prompt)
            # Side effect protocol
            self.assertIn("side_effects.md", prompt)
            # File contract
            self.assertIn("scripts/run.py", prompt)
            self.assertIn("run.sh", prompt)
            self.assertIn("inputs/inputs.example.json", prompt)
            self.assertIn("inputs/inputs.template.json", prompt)
            self.assertIn("references/fallback_plan.md", prompt)
            self.assertIn("skill-creator", prompt)
            # Internet & external services guidance
            self.assertIn("WebSearch", prompt)
            self.assertIn("WebFetch", prompt)
            self.assertIn("uvx", prompt)
            self.assertIn("npx", prompt)
            self.assertIn("no user setup", prompt)
            self.assertIn("AI_MIME_PYTHON_PATH", prompt)
            self.assertIn("AI_MIME_UV_PATH", prompt)
            self.assertIn("requirements.txt", prompt)
            self.assertIn(".venv/bin/python", prompt)
            self.assertIn("SKILL.md` `## Run` must document the Python runtime contract", prompt)
            self.assertIn("skill `.venv/bin/python`", prompt)
            self.assertIn("workflow `.venv/bin/python`", prompt)
            self.assertIn('"$AI_MIME_UV_PATH" venv .venv --python "$AI_MIME_PYTHON_PATH"', prompt)
            self.assertIn(
                '"$AI_MIME_UV_PATH" pip install -r requirements.txt --python .venv/bin/python',
                prompt,
            )
            self.assertIn("Runtime does not create or repair `.venv`", prompt)
            # Structured log contract
            self.assertIn("step_start", prompt)
            self.assertIn("step_done", prompt)
            self.assertIn("step_failed", prompt)
            self.assertIn("workflow_done", prompt)
            # Skill must not ship internal builder artifacts
            self.assertNotIn("references/schema.json", prompt)
            self.assertNotIn("references/optimized_plan.json", prompt)
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
                "PYTHON=\"${AI_MIME_PYTHON_PATH:-python3}\"\n"
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
            self.assertIn("replay execution agent", prompt)
            self.assertIn("SKILL.md", prompt)
            self.assertIn("run.sh", prompt)
            self.assertIn("scripts/run.py", prompt)
            self.assertIn("inputs/inputs.template.json", prompt)
            self.assertIn("references/fallback_plan.md", prompt)
            self.assertIn("Validate and normalize", prompt)
            self.assertIn("./run.sh <inputs.json>", prompt)
            self.assertIn("task variants", prompt)
            self.assertIn("complete the task", prompt)
            self.assertIn("macos-computer-use", prompt)
            self.assertIn("triage before editing", prompt)
            self.assertIn("Closed tabs", prompt)
            self.assertIn("missing windows", prompt)
            self.assertIn("one-off UI disruption", prompt)
            self.assertIn("replay_notes.md", prompt)
            self.assertIn("domain_notes.md", prompt)
            self.assertIn("Targeted edits", prompt)
            self.assertIn("Only edit the skill when needed", prompt)
            self.assertIn("repeated deterministic failure", prompt)
            self.assertNotIn("Do NOT switch to skill-build mode", prompt)
            self.assertNotIn("needs skill healing", prompt)
            self.assertNotIn("AI_MIME_REPLAY_HANDOFF_TO_SKILL_BUILD", prompt)

    def test_workspace_chat_service_can_use_replay_execution_mode(self) -> None:
        prompts: list[str] = []
        modes: list[str] = []
        system_prompts: list[str | None] = []

        class ChatAdapter:
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
            self.assertTrue((workflow_dir / "agent" / "replay" / "session_index.json").exists())

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
            out = _asyncio.get_event_loop().run_until_complete(
                _call("Read", {"file_path": str(workflow_dir / "schema.json")})
            )
            self.assertEqual(out, {})

            # Read outside readable root → block
            out = _asyncio.get_event_loop().run_until_complete(
                _call("Read", {"file_path": "/etc/passwd"})
            )
            self.assertEqual(out.get("decision"), "block")
            self.assertIn("sandbox", out.get("reason", ""))

            # Write to writable root → allowed
            out = _asyncio.get_event_loop().run_until_complete(
                _call("Write", {"file_path": str(allowed / "x.json")})
            )
            self.assertEqual(out, {})

            # Write outside writable root (still inside readable workflow_dir) → block
            out = _asyncio.get_event_loop().run_until_complete(
                _call("Write", {"file_path": str(workflow_dir / "outside.txt")})
            )
            self.assertEqual(out.get("decision"), "block")

            # Bash / unrelated tool → pass through
            out = _asyncio.get_event_loop().run_until_complete(
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

    def test_build_skill_chat_request_has_empty_mcp_servers_by_default(self) -> None:
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
            self.assertEqual(request.mcp_servers, {})

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
            self.assertEqual(request.mcp_servers, payload)

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
            self.assertEqual(request.mcp_servers, {})

    def test_workspace_chat_service_persists_returned_session_id(self) -> None:
        prompts: list[str] = []
        models: list[str | None] = []
        system_prompts: list[str | None] = []

        class ChatAdapter:
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
            self.assertIn("AI Mime workspace debugging agent", system_prompts[0] or "")
            index = json.loads((Path(td) / ".agent" / "session_index.json").read_text(encoding="utf-8"))
            self.assertIn("claude-session-1", index)
            self.assertEqual(index["claude-session-1"]["model"], "sonnet")

    def test_workspace_chat_service_sends_initial_context_only_for_new_session(self) -> None:
        prompts: list[str] = []
        system_prompts: list[str | None] = []

        class ChatAdapter:
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

    def test_workspace_chat_service_rejects_unknown_model(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            service = WorkspaceAgentChatService(
                workspace_dir=Path(td),
                adapter=FakeAdapter(),
                session_lister=lambda _dir: [],
                message_loader=lambda _sid, _dir: [],
            )
            with self.assertRaisesRegex(ValueError, "Unsupported Claude model"):
                service.chat(message="hello", model="not-a-model")

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

    def test_workspace_chat_service_rejects_concurrent_turns(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            service = WorkspaceAgentChatService(
                workspace_dir=Path(td),
                adapter=FakeAdapter(),
                session_lister=lambda _dir: [],
                message_loader=lambda _sid, _dir: [],
            )
            self.assertTrue(service._turn_lock.acquire(blocking=False))
            try:
                with self.assertRaisesRegex(RuntimeError, "already responding"):
                    service.chat(message="hello")
            finally:
                service._turn_lock.release()


if __name__ == "__main__":
    unittest.main()
