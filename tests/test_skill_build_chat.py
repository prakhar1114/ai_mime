from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

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
                "executor": "bash",
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
    (skill_dir / "references").mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text("# x\n", encoding="utf-8")
    (skill_dir / "references" / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
    (skill_dir / "references" / "optimized_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    (skill_dir / "references" / "learned_notes.md").write_text("ok\n", encoding="utf-8")
    (skill_dir / "scripts" / "run.py").write_text(
        "import argparse, json\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--inputs-json', required=True)\n"
        "a = p.parse_args()\n"
        "json.load(open(a.inputs_json))\n"
        "print('ok')\n",
        encoding="utf-8",
    )
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
            # Corrupt the references copy so validate_skill_package fails.
            (skill_dir / "references" / "schema.json").write_text("{}", encoding="utf-8")
            service = _build_service(workflow_dir)
            _write_signal(workflow_dir, {"status": "skill_ready", "summary": "done"})

            event = service._consume_terminal_signal()

            self.assertIsNotNone(event)
            assert event is not None
            self.assertEqual(event["event"], "skill_check_failed")
            self.assertIn("validate_skill_package", event["error"])
            self.assertIsNone(service._terminal_status)
            self.assertFalse((workflow_dir / "agent" / "build_signal.json").exists())


if __name__ == "__main__":
    unittest.main()
