from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ai_mime.agent_runner import (
    AgentRunRequest,
    AgentRunResult,
    WorkspaceAgentChatService,
    build_agent_run_request,
    run_agent_task,
    run_skill_e2e_test,
    validate_skill_package,
)


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


def _write_valid_skill_package(skill_dir: Path, schema: dict, optimized_plan: dict) -> None:
    (skill_dir / "scripts").mkdir(parents=True, exist_ok=True)
    (skill_dir / "references").mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "# Record Expenses Skill\n\nRun with `python scripts/run.py --inputs-json inputs.json`.\n",
        encoding="utf-8",
    )
    (skill_dir / "references" / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
    (skill_dir / "references" / "optimized_plan.json").write_text(json.dumps(optimized_plan), encoding="utf-8")
    (skill_dir / "references" / "learned_notes.md").write_text("No learned notes yet.\n", encoding="utf-8")
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


class FakeAdapter:
    def __init__(self) -> None:
        self.request: AgentRunRequest | None = None
        self.prompt: str | None = None

    def run(self, request: AgentRunRequest, prompt: str) -> AgentRunResult:
        self.request = request
        self.prompt = prompt
        return AgentRunResult(
            status="success",
            session_id=request.session_id or "",
            summary="Fake agent completed the optimized plan.",
        )


class AgentRunnerTests(unittest.TestCase):
    def test_build_request_merges_user_read_hints_and_default_writes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workflow_dir = Path(td)
            (workflow_dir / "schema.json").write_text(json.dumps(_schema()), encoding="utf-8")
            (workflow_dir / "optimized_plan.json").write_text(json.dumps(_optimized_plan()), encoding="utf-8")

            request = build_agent_run_request(workflow_dir=workflow_dir, provider="claude")

        self.assertIn(Path("/Users/prakharjain/Desktop/expenses"), request.readable_roots)
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

    def test_general_mode_uses_workflows_workspace_and_allows_missing_schema(self) -> None:
        request = build_agent_run_request(workflow_dir=Path("/tmp/ignored"), provider="claude", mode="general")
        self.assertEqual(request.mode, "general")
        self.assertIsNone(request.schema_path)
        self.assertIsNone(request.optimized_plan_path)
        self.assertEqual(request.workflow_dir.name, "workflows")
        self.assertEqual(request.workspace_dir, request.workflow_dir)

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
            # Final approval gate before packaging
            self.assertIn("create the skill", prompt)
            # Inputs editing
            self.assertIn("task_params", prompt)
            self.assertIn("inputs[]", prompt)
            # Side effect protocol
            self.assertIn("side_effects.md", prompt)
            # File contract
            self.assertIn("scripts/run.py", prompt)
            self.assertIn("references/learned_notes.md", prompt)
            # schema/optimized_plan are writable for input edits
            writable = {str(p) for p in request.writable_roots}
            self.assertIn(str(workflow_dir / "schema.json"), writable)
            self.assertIn(str(workflow_dir / "optimized_plan.json"), writable)

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

            result = run_skill_e2e_test(skill_dir, plan)

            self.assertEqual(result.status, "failed")
            self.assertIn("required optimized_plan inputs have no default", result.error or "")

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
