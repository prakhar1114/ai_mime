from __future__ import annotations

import asyncio
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_mime.agent_runner import WorkflowSkillBuildService


def _schema() -> dict:
    return {
        "task_name": "record expenses in a sheet",
        "plan": {"subtasks": [{"subtask_i": 0, "text": "Extract receipt", "dependencies": [], "steps": []}]},
    }


def _optimized_plan() -> dict:
    return {
        "version": 1,
        "workflow_goal": "Record a receipt expense.",
        "user_filesystem_access": {"readable_roots": [], "writable_roots": []},
        "inputs": [
            {
                "name": "receipt_path",
                "description": "Path to the receipt.",
                "required": True,
                "default": "/tmp/receipt.pdf",
            }
        ],
        "steps": [
            {
                "id": "extract_receipt",
                "title": "Extract receipt",
                "source_subtask_ids": [0],
                "executor": "script",
                "goal": "Extract receipt details.",
                "inputs": ["receipt_path"],
                "outputs": ["receipt_expense"],
                "success_criteria": "Receipt expense is structured.",
            }
        ],
    }


def _write_workflow(workflow_dir: Path, schema: dict, plan: dict) -> Path:
    (workflow_dir / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
    (workflow_dir / "optimized_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    skill_dir = workflow_dir / "skills" / "record-expenses-in-a-sheet"
    (skill_dir / "scripts").mkdir(parents=True, exist_ok=True)
    (skill_dir / "inputs").mkdir(parents=True, exist_ok=True)
    (skill_dir / "references").mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: record-expenses-in-a-sheet\n"
        "description: Record a receipt expense. Use when the user asks to log an expense.\n"
        "---\n\n"
        "# Record Expenses Skill\n",
        encoding="utf-8",
    )
    example_inputs = {}
    for item in plan.get("inputs", []) or []:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            example_inputs[item["name"]] = item.get("default") or f"<FILL IN: {item.get('description','')}>"
    (skill_dir / "inputs" / "inputs.example.json").write_text(
        json.dumps(example_inputs, indent=2), encoding="utf-8"
    )
    (skill_dir / "inputs" / "inputs.template.json").write_text(
        json.dumps({k: f"<FILL IN: {k}>" for k in example_inputs.keys()}, indent=2),
        encoding="utf-8",
    )
    (skill_dir / "references" / "fallback_plan.md").write_text(
        "# Fallback plan\n\n## Subtask 0 — Extract receipt\nIntent: parse the receipt.\n",
        encoding="utf-8",
    )
    (skill_dir / "scripts" / "run.py").write_text(
        "import argparse, json\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--inputs-json', required=True)\n"
        "a = p.parse_args()\n"
        "json.load(open(a.inputs_json))\n"
        "print('ok')\n",
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
    return skill_dir


def _build_service(workflow_dir: Path) -> WorkflowSkillBuildService:
    return WorkflowSkillBuildService(
        workflow_dir=workflow_dir,
        adapter=object(),  # never invoked in these tests
        session_lister=lambda _dir: [],
        message_loader=lambda _sid, _dir: [],
    )


def _write_signal(workflow_dir: Path, payload: dict) -> None:
    agent_dir = workflow_dir / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "build_signal.json").write_text(json.dumps(payload), encoding="utf-8")


class WorkflowSkillBuildServiceTests(unittest.TestCase):
    def test_status_returns_active_session_and_skill_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workflow_dir = Path(td)
            _write_workflow(workflow_dir, _schema(), _optimized_plan())
            (workflow_dir / "agent").mkdir(exist_ok=True)
            (workflow_dir / "agent" / "skill_build_active.json").write_text(
                json.dumps({"session_id": "session-active", "model": "sonnet"}),
                encoding="utf-8",
            )
            service = _build_service(workflow_dir)

            status = service.status()

            self.assertEqual(status["active_session_id"], "session-active")
            self.assertTrue(status["has_optimized_plan"])
            self.assertTrue(status["has_skill"])

    def test_status_requires_executable_run_sh_for_built_skill(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workflow_dir = Path(td)
            (workflow_dir / "schema.json").write_text(json.dumps(_schema()), encoding="utf-8")
            (workflow_dir / "optimized_plan.json").write_text(json.dumps(_optimized_plan()), encoding="utf-8")
            skill_dir = workflow_dir / "skills" / "record-expenses-in-a-sheet"
            skill_dir.mkdir(parents=True)
            service = _build_service(workflow_dir)

            status_without_run_sh = service.status()

            self.assertEqual(status_without_run_sh["skill_dir"], str(skill_dir))
            self.assertFalse(status_without_run_sh["has_skill"])

            run_sh = skill_dir / "run.sh"
            run_sh.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
            run_sh.chmod(run_sh.stat().st_mode & ~(stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))

            status_with_non_executable_run_sh = service.status()

            self.assertFalse(status_with_non_executable_run_sh["has_skill"])

            run_sh.chmod(run_sh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

            status_with_executable_run_sh = service.status()

            self.assertTrue(status_with_executable_run_sh["has_skill"])

    def test_skill_ready_signal_runs_validate_and_e2e_and_emits_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workflow_dir = Path(td)
            schema = _schema()
            plan = _optimized_plan()
            skill_dir = _write_workflow(workflow_dir, schema, plan)
            service = _build_service(workflow_dir)
            _write_signal(workflow_dir, {"status": "skill_ready", "summary": "done"})

            event = service._consume_terminal_signal()

            self.assertIsNotNone(event)
            assert event is not None
            self.assertEqual(event["event"], "skill_build_done")
            self.assertEqual(event["status"], "skill_ready")
            self.assertEqual(event["skill_dir"], str(skill_dir))
            self.assertEqual(service._terminal_status, "skill_ready")

    def test_skill_unbuildable_signal_preserves_reason_and_suggested_changes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workflow_dir = Path(td)
            schema = _schema()
            plan = _optimized_plan()
            _write_workflow(workflow_dir, schema, plan)
            service = _build_service(workflow_dir)
            _write_signal(
                workflow_dir,
                {
                    "status": "skill_unbuildable",
                    "reason": "Auth wall blocks the script.",
                    "suggested_changes": ["Require API token input"],
                },
            )

            event = service._consume_terminal_signal()

            self.assertIsNotNone(event)
            assert event is not None
            self.assertEqual(event["event"], "skill_build_done")
            self.assertEqual(event["status"], "skill_unbuildable")
            self.assertEqual(event["reason"], "Auth wall blocks the script.")
            self.assertEqual(event["suggested_changes"], ["Require API token input"])
            self.assertEqual(service._terminal_status, "skill_unbuildable")

    def test_failed_validation_returns_check_failed_and_keeps_session_open(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workflow_dir = Path(td)
            schema = _schema()
            plan = _optimized_plan()
            skill_dir = _write_workflow(workflow_dir, schema, plan)
            # Corrupt SKILL.md (strip frontmatter) so validate_skill_package fails.
            (skill_dir / "SKILL.md").write_text("# no frontmatter here\n", encoding="utf-8")
            service = _build_service(workflow_dir)
            _write_signal(workflow_dir, {"status": "skill_ready", "summary": "done"})

            event = service._consume_terminal_signal()

            self.assertIsNotNone(event)
            assert event is not None
            self.assertEqual(event["event"], "skill_check_failed")
            self.assertIn("validate_skill_package", event["error"])
            self.assertIsNone(service._terminal_status)
            self.assertFalse((workflow_dir / "agent" / "build_signal.json").exists())

    def test_chat_stream_runs_without_turn_lock(self) -> None:
        async def fake_stream_chat(*_args, **_kwargs):
            yield {"event": "text", "text": "ok"}
            yield {"event": "done", "status": "success", "session_id": "skill-session-1", "summary": "done"}

        async def collect_events(service: WorkflowSkillBuildService) -> list[dict]:
            return [event async for event in service.chat_stream(message="continue build")]

        with tempfile.TemporaryDirectory() as td:
            workflow_dir = Path(td)
            _write_workflow(workflow_dir, _schema(), _optimized_plan())
            service = _build_service(workflow_dir)

            with patch("ai_mime.agent_runner.skill_build_chat.stream_chat", new=fake_stream_chat):
                events = asyncio.run(collect_events(service))

            self.assertEqual(events[-1]["event"], "done")
            self.assertEqual(events[-1]["session_id"], "skill-session-1")


class AuthorizeToolTests(unittest.TestCase):
    def _request(self, workflow_dir: Path, mcp_servers: dict | None = None):
        from ai_mime.agent_runner.models import AgentRunRequest, FilesystemAccess

        return AgentRunRequest(
            provider="claude",
            mode="build_skill_chat",
            workflow_dir=workflow_dir,
            workspace_dir=workflow_dir,
            readable_roots=[workflow_dir],
            writable_roots=[workflow_dir],
            user_filesystem_access=FilesystemAccess(),
            mcp_servers=mcp_servers,
        )

    def test_authorize_allows_web_tools_without_path_checks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workflow_dir = Path(td)
            _write_workflow(workflow_dir, _schema(), _optimized_plan())
            service = _build_service(workflow_dir)
            request = self._request(workflow_dir)
            for tool in ("WebFetch", "WebSearch"):
                decision = service._authorize_tool(request, tool, {"url": "https://example.com"})
                self.assertEqual(decision.get("behavior"), "allow", f"{tool} should be allowed")

    def test_authorize_allows_registered_mcp_server_tools(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workflow_dir = Path(td)
            _write_workflow(workflow_dir, _schema(), _optimized_plan())
            service = _build_service(workflow_dir)
            request = self._request(workflow_dir, mcp_servers={"hello": {"type": "stdio", "command": "echo"}})
            decision = service._authorize_tool(request, "mcp__hello__do_thing", {})
            self.assertEqual(decision.get("behavior"), "allow")

    def test_authorize_denies_unregistered_mcp_server_tools(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workflow_dir = Path(td)
            _write_workflow(workflow_dir, _schema(), _optimized_plan())
            service = _build_service(workflow_dir)
            request = self._request(workflow_dir, mcp_servers={"hello": {"type": "stdio", "command": "echo"}})
            decision = service._authorize_tool(request, "mcp__other__do_thing", {})
            self.assertEqual(decision.get("behavior"), "deny")

    def test_authorize_denies_mcp_when_none_registered(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workflow_dir = Path(td)
            _write_workflow(workflow_dir, _schema(), _optimized_plan())
            service = _build_service(workflow_dir)
            request = self._request(workflow_dir, mcp_servers=None)
            decision = service._authorize_tool(request, "mcp__hello__do_thing", {})
            self.assertEqual(decision.get("behavior"), "deny")


if __name__ == "__main__":
    unittest.main()
