from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ai_mime.reflect.schema_compiler import (
    cleanup_reflect_artifacts,
    compile_workflow_schema,
    create_optimized_plan,
    run_pass_a_step_cards,
    validate_optimized_plan,
)


def _schema(subtask_count: int = 5) -> dict:
    return {
        "task_name": "record expenses in a sheet",
        "plan": {
            "subtasks": [
                {"subtask_i": i, "text": f"Subtask {i}", "dependencies": [], "steps": []}
                for i in range(subtask_count)
            ]
        },
    }


def _valid_plan() -> dict:
    return {
        "version": 1,
        "workflow_goal": "Record a receipt expense into the Expenses Google Sheet.",
        "user_filesystem_access": {
            "readable_roots": [
                {
                    "path": "/Users/prakharjain/Desktop/expenses",
                    "reason": "Read receipt PDFs selected by the user.",
                }
            ],
            "writable_roots": [],
        },
        "inputs": [
            {
                "name": "receipt_path",
                "description": "Path to the receipt PDF to record.",
                "required": False,
                "default": "/Users/prakharjain/Desktop/expenses/Receipt - SFO-A Black Point Cafe.pdf",
            }
        ],
        "steps": [
            {
                "id": "extract_receipt",
                "title": "Find and extract receipt expense details",
                "source_subtask_ids": [0, 1],
                "executor": "script",
                "goal": (
                    "Use direct file access to locate the receipt PDF, render or read it as needed, "
                    "and use an LLM only if OCR or semantic extraction is required."
                ),
                "inputs": ["receipt_path"],
                "outputs": ["receipt_expense"],
                "success_criteria": "A structured receipt expense with description and amount is available.",
                "fallback": "ui_agent",
            },
            {
                "id": "append_expense_to_sheet",
                "title": "Append receipt expense to Google Sheets",
                "source_subtask_ids": [2, 3, 4],
                "executor": "browser_harness",
                "goal": (
                    "Use the existing Chrome session to find the Expenses Google Sheet "
                    "and append the extracted expense."
                ),
                "inputs": ["receipt_expense"],
                "outputs": [],
                "success_criteria": "The sheet contains a new row matching the extracted description and amount.",
                "fallback": "ui_agent",
            },
        ],
    }


class PassCOptimizerTests(unittest.TestCase):
    def test_valid_plan_allows_multiple_source_subtasks_and_data_flow(self) -> None:
        validate_optimized_plan(_valid_plan(), _schema())

    def test_duplicate_user_filesystem_path_is_rejected(self) -> None:
        plan = _valid_plan()
        plan["user_filesystem_access"]["readable_roots"].append(
            {
                "path": "/Users/prakharjain/Desktop/expenses",
                "reason": "Duplicate path.",
            }
        )
        with self.assertRaisesRegex(ValueError, "path duplicated"):
            validate_optimized_plan(plan, _schema())

    def test_empty_user_filesystem_reason_is_rejected(self) -> None:
        plan = _valid_plan()
        plan["user_filesystem_access"]["readable_roots"][0]["reason"] = ""
        with self.assertRaisesRegex(ValueError, "reason must be non-empty"):
            validate_optimized_plan(plan, _schema())

    def test_broad_user_filesystem_write_requires_approval(self) -> None:
        plan = _valid_plan()
        plan["user_filesystem_access"]["writable_roots"] = [
            {
                "path": str(Path.home() / "Desktop"),
                "reason": "Write task files to Desktop.",
                "approval_required": False,
            }
        ]
        with self.assertRaisesRegex(ValueError, "approval_required=true"):
            validate_optimized_plan(plan, _schema())

    def test_invalid_executor_is_rejected(self) -> None:
        plan = _valid_plan()
        plan["steps"][0]["executor"] = "ask_llm"
        with self.assertRaisesRegex(ValueError, "schema validation"):
            validate_optimized_plan(plan, _schema())

    def test_unknown_step_input_is_rejected(self) -> None:
        plan = _valid_plan()
        plan["steps"][1]["inputs"] = ["missing_variable"]
        with self.assertRaisesRegex(ValueError, "unknown input variable"):
            validate_optimized_plan(plan, _schema())

    def test_duplicate_step_output_is_rejected(self) -> None:
        plan = _valid_plan()
        plan["steps"][1]["outputs"] = ["receipt_expense"]
        with self.assertRaisesRegex(ValueError, "output duplicated"):
            validate_optimized_plan(plan, _schema())

    def test_invalid_source_subtask_id_is_rejected(self) -> None:
        plan = _valid_plan()
        plan["steps"][1]["source_subtask_ids"] = [2, 99]
        with self.assertRaisesRegex(ValueError, "invalid source_subtask_id"):
            validate_optimized_plan(plan, _schema())

    def test_valid_existing_optimized_plan_skips_pass_c(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workflow_dir = Path(td)
            plan = _valid_plan()
            (workflow_dir / "schema.json").write_text(json.dumps(_schema()), encoding="utf-8")
            (workflow_dir / "optimized_plan.json").write_text(json.dumps(plan), encoding="utf-8")

            loaded = create_optimized_plan(
                workflow_dir=workflow_dir,
                schema=_schema(),
                llm_cfg=None,  # type: ignore[arg-type]
            )

        self.assertEqual(loaded, plan)

    def test_cleanup_preserves_durable_files_and_removes_reflect_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workflow_dir = Path(td)
            schema = _schema()
            plan = _valid_plan()
            (workflow_dir / "metadata.json").write_text(json.dumps({"name": "Expense"}), encoding="utf-8")
            (workflow_dir / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
            (workflow_dir / "optimized_plan.json").write_text(json.dumps(plan), encoding="utf-8")
            (workflow_dir / "step_cards.json").write_text("[]", encoding="utf-8")
            (workflow_dir / "plan_creation.json").write_text("{}", encoding="utf-8")
            (workflow_dir / "0.png").write_bytes(b"png")
            (workflow_dir / "manifest.jsonl").write_text(
                json.dumps({"screenshot": "0.png"}) + "\n",
                encoding="utf-8",
            )

            cleanup_reflect_artifacts(workflow_dir, schema=schema, optimized_plan=plan)

            self.assertTrue((workflow_dir / "metadata.json").exists())
            self.assertTrue((workflow_dir / "schema.json").exists())
            self.assertTrue((workflow_dir / "optimized_plan.json").exists())
            self.assertTrue((workflow_dir / "manifest.jsonl").exists())
            self.assertFalse((workflow_dir / "step_cards.json").exists())
            self.assertFalse((workflow_dir / "plan_creation.json").exists())
            self.assertFalse((workflow_dir / "0.png").exists())

    def test_cleanup_does_not_run_with_invalid_optimized_plan(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workflow_dir = Path(td)
            schema = _schema()
            plan = _valid_plan()
            plan["steps"][0]["executor"] = "invalid"
            (workflow_dir / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
            (workflow_dir / "optimized_plan.json").write_text(json.dumps(plan), encoding="utf-8")
            (workflow_dir / "step_cards.json").write_text("[]", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "schema validation"):
                cleanup_reflect_artifacts(workflow_dir, schema=schema, optimized_plan=plan)

            self.assertTrue((workflow_dir / "step_cards.json").exists())

    def test_compile_completes_without_calling_into_agent_runner(self) -> None:
        # Skill build is now a user-initiated chat flow (WorkflowSkillBuildService);
        # the reflect pipeline no longer auto-invokes the agent runner.
        with tempfile.TemporaryDirectory() as td:
            workflow_dir = Path(td)
            schema = _schema()
            plan = _valid_plan()
            (workflow_dir / "metadata.json").write_text(json.dumps({"name": "Expense", "description": ""}), encoding="utf-8")
            (workflow_dir / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
            (workflow_dir / "optimized_plan.json").write_text(json.dumps(plan), encoding="utf-8")
            llm_cfg = SimpleNamespace(model="test-model")

            with patch("ai_mime.reflect.schema_compiler.create_optimized_plan", return_value=plan) as ensure_plan:
                result = compile_workflow_schema(workflow_dir=workflow_dir, llm_cfg=llm_cfg)  # type: ignore[arg-type]

            self.assertEqual(result, schema)
            ensure_plan.assert_called_once()

    def test_compile_workflow_schema_skips_pass_a_only_if_complete(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workflow_dir = Path(td)
            (workflow_dir / "metadata.json").write_text(
                json.dumps({"name": "Expense", "description": ""}), encoding="utf-8"
            )
            # Create a manifest with 2 events mapping to 2 actionable steps
            (workflow_dir / "manifest.jsonl").write_text(
                json.dumps({"action_type": "click", "screenshot": "0.png"}) + "\n" +
                json.dumps({"action_type": "type", "action_details": {"text": "hello"}, "screenshot": "1.png"}) + "\n",
                encoding="utf-8"
            )
            # Scenario 1: step_cards.json exists but is incomplete (only has index 0, missing index 1)
            (workflow_dir / "step_cards.json").write_text(
                json.dumps([{"i": 0, "intent": "click something"}]), encoding="utf-8"
            )

            llm_cfg = SimpleNamespace(
                model="test-model",
                pass_a_model=None,
                pass_b_model=None,
                api_base=None,
                api_key_env=None,
                extra_kwargs=None,
                pass_b_max_tokens=None,
            )
            
            with patch("ai_mime.reflect.schema_compiler.run_pass_a_step_cards") as mock_run_a, \
                 patch("ai_mime.reflect.schema_compiler.run_pass_b_task_compiler") as mock_run_b, \
                 patch("ai_mime.reflect.schema_compiler.create_optimized_plan") as mock_create_plan:
                
                # Mock screenshots exist checks inside any other functions if any, but since we mock run_pass_a_step_cards, it doesn't do filesystem checks
                mock_run_a.return_value = [
                    {"i": 0, "action_type": "CLICK", "action_value": None, "target": {"primary": "x"}},
                    {"i": 1, "action_type": "TYPE", "action_value": "hello", "target": {"primary": "y"}}
                ]
                mock_run_b.return_value = {
                    "detailed_task_description": "desc",
                    "subtasks": ["subtask 1"],
                    "task_params": [],
                    "success_criteria": "success",
                    "plan_step_updates": [
                        {"i": 0, "subtask": "subtask 1", "action_value": None},
                        {"i": 1, "subtask": "subtask 1", "action_value": "hello"}
                    ]
                }
                mock_create_plan.return_value = {}
                
                compile_workflow_schema(workflow_dir=workflow_dir, llm_cfg=llm_cfg) # type: ignore[arg-type]
                
                # Should not have skipped Pass A because cards were incomplete
                mock_run_a.assert_called_once()

    def test_pass_a_client_receives_screenshot_image_blocks(self) -> None:
        captured: dict = {}

        class FakeClient:
            def __init__(self, **_kwargs):  # type: ignore[no-untyped-def]
                pass

            def create(self, **kwargs):  # type: ignore[no-untyped-def]
                captured.update(kwargs)
                return SimpleNamespace(
                    model_dump=lambda: {
                        "i": 0,
                        "expected_current_state": "screen",
                        "intent": "click",
                        "action_type": "CLICK",
                        "action_value": None,
                        "target": {"primary": "button", "fallback": None},
                        "post_action": ["changed"],
                    }
                )

        with tempfile.TemporaryDirectory() as td:
            workflow_dir = Path(td)
            (workflow_dir / "metadata.json").write_text(
                json.dumps({"name": "Task", "description": "Do it"}), encoding="utf-8"
            )
            (workflow_dir / "pre.png").write_bytes(b"pre")
            (workflow_dir / "post.png").write_bytes(b"post")
            (workflow_dir / "manifest.jsonl").write_text(
                json.dumps({"action_type": "click", "screenshot": "pre.png"}) + "\n"
                + json.dumps({"action_type": "end", "screenshot": "post.png"}) + "\n",
                encoding="utf-8",
            )
            llm_cfg = SimpleNamespace(
                model="openai/test",
                pass_a_model=None,
                api_base=None,
                api_key_env="MISSING_KEY",
                extra_kwargs={},
                pass_a_max_tokens=123,
            )

            with patch("ai_mime.reflect.schema_compiler.LiteLLMChatClient", FakeClient):
                cards = run_pass_a_step_cards(workflow_dir=workflow_dir, llm_cfg=llm_cfg)  # type: ignore[arg-type]

        self.assertEqual(cards[0]["i"], 0)
        content = captured["messages"][1]["content"]
        image_urls = [item["image_url"]["url"] for item in content if item.get("type") == "image_url"]
        self.assertEqual(len(image_urls), 2)
        self.assertTrue(all(url.startswith("data:image/png;base64,") for url in image_urls))



if __name__ == "__main__":
    unittest.main()
