from __future__ import annotations

import json
import queue
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from ai_mime.editor.server import TaskRunner, create_app
from ai_mime.agent_runner import AgentRunRequest, AgentRunResult, WorkspaceAgentChatService
from ai_mime.reflect.workflow import reflect_session


class TaskDashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workflows = self.root / "workflows"
        self.recordings = self.root / "recordings"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _ready_workflow(self, task_id: str, name: str = "Ready Task") -> Path:
        wf = self.workflows / task_id
        wf.mkdir(parents=True)
        (wf / "metadata.json").write_text(json.dumps({"name": name}), encoding="utf-8")
        (wf / "schema.json").write_text(json.dumps({"task_name": name, "plan": {"subtasks": []}}), encoding="utf-8")
        (wf / "optimized_plan.json").write_text(json.dumps({"version": 1, "steps": []}), encoding="utf-8")
        skill_dir = wf / "skills" / "ready-task"
        skill_dir.mkdir(parents=True)
        run_sh = skill_dir / "run.sh"
        run_sh.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        run_sh.chmod(run_sh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return wf

    def _optimized_workflow_without_skill(self, task_id: str, name: str = "Optimized Task") -> Path:
        wf = self.workflows / task_id
        wf.mkdir(parents=True)
        (wf / "metadata.json").write_text(json.dumps({"name": name}), encoding="utf-8")
        (wf / "schema.json").write_text(json.dumps({"task_name": name, "plan": {"subtasks": []}}), encoding="utf-8")
        (wf / "optimized_plan.json").write_text(json.dumps({"version": 1, "steps": []}), encoding="utf-8")
        return wf

    def _workflow_with_incomplete_skill_dir(self, task_id: str, *, run_sh: bool = False) -> Path:
        wf = self._optimized_workflow_without_skill(task_id)
        skill_dir = wf / "skills" / "incomplete-task"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# Incomplete\n", encoding="utf-8")
        if run_sh:
            (skill_dir / "run.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        return wf

    def _recording(self, task_id: str) -> Path:
        rec = self.recordings / task_id
        rec.mkdir(parents=True)
        (rec / "manifest.jsonl").write_text("{}\n", encoding="utf-8")
        return rec

    def _broken_workflow(self, task_id: str) -> Path:
        wf = self.workflows / task_id
        wf.mkdir(parents=True)
        (wf / "metadata.json").write_text(json.dumps({"name": "Broken Task"}), encoding="utf-8")
        return wf

    def test_inventory_marks_ready_and_pending_tasks(self) -> None:
        self._ready_workflow("20260513T000000Z-ready")
        self._ready_workflow("20260513T000050Z-workflow-only")
        self._optimized_workflow_without_skill("20260513T000075Z-optimized")
        self._recording("20260513T000000Z-ready")
        self._recording("20260513T000100Z-pending")
        self._broken_workflow("20260513T000200Z-broken")

        runner = TaskRunner(
            workflows_root=self.workflows,
            recordings_root=self.recordings,
            reflect_llm_cfg=None,
        )
        rows = {row["id"]: row for row in runner.list_tasks()}

        self.assertTrue(rows["20260513T000000Z-ready"]["can_replay"])
        self.assertTrue(rows["20260513T000000Z-ready"]["has_skill"])
        self.assertIsNotNone(rows["20260513T000000Z-ready"]["skill_dir"])
        # self.assertTrue(rows["20260513T000000Z-ready"]["can_edit"])
        self.assertTrue(rows["20260513T000000Z-ready"]["can_reflect"])
        self.assertEqual(rows["20260513T000000Z-ready"]["status"], "ready")
        self.assertTrue(rows["20260513T000050Z-workflow-only"]["can_reflect"])
        self.assertEqual(rows["20260513T000050Z-workflow-only"]["status"], "ready")
        self.assertEqual(rows["20260513T000075Z-optimized"]["status"], "ready")
        self.assertTrue(rows["20260513T000075Z-optimized"]["has_schema"])
        self.assertTrue(rows["20260513T000075Z-optimized"]["has_optimized_plan"])
        self.assertFalse(rows["20260513T000075Z-optimized"]["has_skill"])
        self.assertFalse(rows["20260513T000075Z-optimized"]["can_replay"])
        self.assertTrue(rows["20260513T000100Z-pending"]["can_reflect"])
        self.assertEqual(rows["20260513T000100Z-pending"]["status"], "pending_reflection")
        self.assertFalse(rows["20260513T000200Z-broken"]["can_reflect"])

    def test_api_lists_and_deletes_matching_task_folders(self) -> None:
        self._ready_workflow("20260513T000000Z-ready")
        rec = self._recording("20260513T000100Z-pending")
        app = create_app(workflows_root=self.workflows, recordings_root=self.recordings)
        client = TestClient(app)

        data = client.get("/api/tasks").json()["tasks"]
        self.assertEqual(len(data), 2)

        response = client.delete("/api/tasks/20260513T000100Z-pending")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(rec.exists())

    def test_reflect_reject_missing_configs(self) -> None:
        self._ready_workflow("20260513T000000Z-ready")
        self._recording("20260513T000100Z-pending")
        app = create_app(workflows_root=self.workflows, recordings_root=self.recordings)
        client = TestClient(app)

        self.assertEqual(client.post("/api/tasks/20260513T000100Z-pending/reflect").status_code, 500)

    def test_force_reflect_rewrites_existing_workflow_manifest(self) -> None:
        task_id = "20260513T000100Z-pending"
        recording = self._recording(task_id)
        (recording / "metadata.json").write_text(json.dumps({"name": "Updated"}), encoding="utf-8")
        (recording / "manifest.jsonl").write_text(json.dumps({"new": True}) + "\n", encoding="utf-8")
        workflow = self.workflows / task_id
        workflow.mkdir(parents=True)
        (workflow / "metadata.json").write_text(json.dumps({"name": "Old"}), encoding="utf-8")
        (workflow / "manifest.jsonl").write_text(json.dumps({"old": True}) + "\n", encoding="utf-8")
        (workflow / "schema.json").write_text(json.dumps({"plan": {"subtasks": []}}), encoding="utf-8")

        reflect_session(recording, self.workflows)
        self.assertEqual((workflow / "manifest.jsonl").read_text(encoding="utf-8"), json.dumps({"old": True}) + "\n")

        reflect_session(recording, self.workflows, force=True)
        self.assertEqual((workflow / "manifest.jsonl").read_text(encoding="utf-8"), json.dumps({"new": True}) + "\n")
        self.assertTrue((workflow / "schema.json").exists())

    def test_reflect_accepts_workflow_only_schema_task(self) -> None:
        task_id = "20260513T000000Z-ready"
        workflow = self._ready_workflow(task_id)

        class FakeProcess:
            instances: list["FakeProcess"] = []

            def __init__(self, *, target, args, kwargs, daemon):
                self.target = target
                self.args = args
                self.kwargs = kwargs
                self.daemon = daemon
                self.pid = 1234
                self.started = False
                FakeProcess.instances.append(self)

            def start(self) -> None:
                self.started = True

        app = create_app(
            workflows_root=self.workflows,
            recordings_root=self.recordings,
            reflect_llm_cfg=SimpleNamespace(model="test"),
        )
        client = TestClient(app)

        with patch("ai_mime.editor.server.Process", FakeProcess):
            response = client.post(f"/api/tasks/{task_id}/reflect")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(FakeProcess.instances[-1].args[0], str(workflow.resolve()))
        self.assertFalse(FakeProcess.instances[-1].kwargs["force"])
        self.assertTrue(FakeProcess.instances[-1].started)

    def test_reflect_rejects_task_without_recording_manifest_or_schema(self) -> None:
        task_id = "20260513T000200Z-broken"
        self._broken_workflow(task_id)
        app = create_app(
            workflows_root=self.workflows,
            recordings_root=self.recordings,
            reflect_llm_cfg=SimpleNamespace(model="test"),
        )
        client = TestClient(app)

        response = client.post(f"/api/tasks/{task_id}/reflect")

        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("Recording manifest.jsonl or workflow schema.json not found", response.text)

    def test_runner_skips_reflect_session_when_schema_exists(self) -> None:
        from ai_mime.reflect.runner import run_reflect_and_compile_schema

        task_id = "20260513T000000Z-ready"
        workflow = self._ready_workflow(task_id)
        compiled: list[Path] = []

        def fake_compile_schema_for_workflow_dir(out_dir, **_kwargs):
            compiled.append(Path(out_dir))
            return {"plan": {"subtasks": []}}

        with (
            patch("ai_mime.reflect.runner.reflect_session") as reflect_session_mock,
            patch(
                "ai_mime.reflect.runner.compile_schema_for_workflow_dir",
                side_effect=fake_compile_schema_for_workflow_dir,
            ),
        ):
            run_reflect_and_compile_schema(
                str(self.recordings / task_id),
                SimpleNamespace(model="test"),
                workflows_root=self.workflows,
            )

        reflect_session_mock.assert_not_called()
        self.assertEqual(compiled, [workflow])

    def test_recording_start_api_and_external_reflect_status(self) -> None:
        self._recording("20260513T000100Z-pending")
        command_q: queue.Queue = queue.Queue()
        app_state = {
            "recording": {"is_recording": False, "requested": False, "session_name": None},
            "reflecting": {"20260513T000100Z-pending": "reflecting"},
        }
        app = create_app(
            workflows_root=self.workflows,
            recordings_root=self.recordings,
            app_command_queue=command_q,
            app_state=app_state,
        )
        client = TestClient(app)

        row = client.get("/api/tasks").json()["tasks"][0]
        self.assertEqual(row["status"], "reflecting")
        self.assertFalse(row["can_reflect"])

        response = client.post("/api/recording/start")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(command_q.get_nowait()["type"], "start_recording")
        self.assertTrue(app_state["recording"]["requested"])

    def test_reflect_progress_events_update_status(self) -> None:
        self._recording("20260513T000100Z-pending")
        runner = TaskRunner(
            workflows_root=self.workflows,
            recordings_root=self.recordings,
            reflect_llm_cfg=None,
        )
        q: queue.Queue = queue.Queue()
        q.put({"type": "reflect_phase_started", "phase": "compiling", "label": "Compiling", "progress": 8})
        q.put({"type": "reflect_progress", "phase": "pass_a_complete", "label": "Pass A", "progress": 33})
        q.put({"type": "reflect_progress", "phase": "pass_b_complete", "label": "Pass B", "progress": 66})

        with runner._lock:
            runner._drain_reflect_events_locked("20260513T000100Z-pending", q)
            row = runner._task_row_locked("20260513T000100Z-pending")

        self.assertEqual(row["status"], "compiling")
        self.assertEqual(row["phase"], "pass_b_complete")
        self.assertEqual(row["progress"]["value"], 66)
        self.assertEqual(row["progress"]["label"], "Pass B")

    def test_reflect_page_status_and_task_agent_workspace(self) -> None:
        self._recording("20260513T000100Z-pending")
        self._ready_workflow("20260513T000000Z-ready")
        app = create_app(workflows_root=self.workflows, recordings_root=self.recordings)
        client = TestClient(app)

        page = client.get("/reflect/20260513T000100Z-pending")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertNotIn("__TASK_ID__", page.text)
        self.assertIn("20260513T000100Z-pending", page.text)

        missing = client.get("/reflect/not-found")
        self.assertEqual(missing.status_code, 404)

        invalid = client.get("/reflect/../bad")
        self.assertIn(invalid.status_code, {400, 404})

        status = client.get("/api/tasks/20260513T000100Z-pending/reflect/status")
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["progress"]["phase"], "pending_reflection")

        pending_agent = client.get("/api/tasks/20260513T000100Z-pending/agent/sessions")
        self.assertEqual(pending_agent.status_code, 200, pending_agent.text)
        self.assertEqual(pending_agent.json()["workspace_dir"], str(self.recordings / "20260513T000100Z-pending"))

        ready_agent = client.get("/api/tasks/20260513T000000Z-ready/agent/sessions")
        self.assertEqual(ready_agent.status_code, 200, ready_agent.text)
        self.assertEqual(ready_agent.json()["workspace_dir"], str(self.workflows / "20260513T000000Z-ready"))

    def test_skill_build_page_available_with_optimized_plan_without_skill(self) -> None:
        task_id = "20260513T000075Z-optimized"
        self._optimized_workflow_without_skill(task_id)
        app = create_app(workflows_root=self.workflows, recordings_root=self.recordings)
        client = TestClient(app)

        page = client.get(f"/skill-build/{task_id}")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn("Build Skill", page.text)
        self.assertNotIn("Type <code>begin</code> to start", page.text)

        status = client.get(f"/api/tasks/{task_id}/skill-build/sessions")
        self.assertEqual(status.status_code, 200, status.text)
        data = status.json()
        self.assertTrue(data["has_optimized_plan"])
        self.assertFalse(data["has_skill"])

    def test_incomplete_skill_folder_does_not_count_as_built_skill(self) -> None:
        for task_id, has_run_sh in (
            ("20260513T000080Z-bare-skill-dir", False),
            ("20260513T000081Z-non-executable-run-sh", True),
        ):
            with self.subTest(task_id=task_id):
                self._workflow_with_incomplete_skill_dir(task_id, run_sh=has_run_sh)
                app = create_app(workflows_root=self.workflows, recordings_root=self.recordings)
                client = TestClient(app)

                row = client.get(f"/api/tasks/{task_id}/reflect/status").json()
                self.assertTrue(row["has_optimized_plan"])
                self.assertFalse(row["has_skill"])
                self.assertIsNone(row["skill_dir"])
                self.assertFalse(row["can_replay"])

                status = client.get(f"/api/tasks/{task_id}/skill-build/sessions")
                self.assertEqual(status.status_code, 200, status.text)
                data = status.json()
                self.assertTrue(data["has_optimized_plan"])
                self.assertFalse(data["has_skill"])

    def test_reflect_js_does_not_force_rereflect(self) -> None:
        reflect_js = Path("src/ai_mime/editor/web/reflect.js").read_text(encoding="utf-8")
        tasks_js = Path("src/ai_mime/editor/web/tasks.js").read_text(encoding="utf-8")

        self.assertNotIn("force: true", reflect_js)
        self.assertNotIn("force: true", tasks_js)
        self.assertIn("force: false", reflect_js)
        self.assertIn("force: false", tasks_js)

    def test_agent_page_and_api_chat(self) -> None:
        seen_models: list[str | None] = []

        class ChatAdapter:
            def run(self, request: AgentRunRequest, prompt: str) -> AgentRunResult:
                seen_models.append(request.model)
                return AgentRunResult(status="success", session_id=request.session_id or "session-1", summary="agent reply")

        service = WorkspaceAgentChatService(
            workspace_dir=self.workflows,
            adapter=ChatAdapter(),
            session_lister=lambda _dir: [],
            message_loader=lambda sid, _dir: [{"type": "user", "session_id": sid, "message": "hello"}],
        )
        app = create_app(
            workflows_root=self.workflows,
            recordings_root=self.recordings,
            agent_chat_service=service,
        )
        client = TestClient(app)

        agent_html = client.get("/agent")
        self.assertEqual(agent_html.status_code, 200, agent_html.text)
        self.assertIn("Agent Mode", agent_html.text)

        sessions = client.get("/api/agent/sessions")
        self.assertEqual(sessions.status_code, 200, sessions.text)
        self.assertIn("models", sessions.json())

        models = client.get("/api/agent/models")
        self.assertEqual(models.status_code, 200, models.text)
        self.assertTrue(any(item["id"] == "sonnet" for item in models.json()["models"]))

        created = client.post("/api/agent/sessions")
        self.assertEqual(created.status_code, 200, created.text)
        self.assertIsNone(created.json()["session_id"])
        self.assertFalse((self.workflows / ".agent" / "agent_sessions.json").exists())

        chat = client.post("/api/agent/chat", json={"message": "hello", "session_id": None, "model": "opus"})
        self.assertEqual(chat.status_code, 200, chat.text)
        self.assertEqual(chat.json()["session_id"], "session-1")
        self.assertEqual(chat.json()["assistant_text"], "agent reply")
        self.assertEqual(chat.json()["model"], "opus")
        self.assertEqual(seen_models, ["opus"])

        messages = client.get("/api/agent/sessions/session-1/messages")
        self.assertEqual(messages.status_code, 200, messages.text)
        self.assertEqual(messages.json()["messages"][0]["message"], "hello")

    def test_agent_api_accepts_sequential_recovery_turns(self) -> None:
        seen_session_ids: list[str | None] = []

        class ChatAdapter:
            def run(self, request: AgentRunRequest, prompt: str) -> AgentRunResult:
                seen_session_ids.append(request.session_id)
                return AgentRunResult(
                    status="success",
                    session_id=request.session_id or "session-1",
                    summary="agent reply",
                )

        service = WorkspaceAgentChatService(
            workspace_dir=self.workflows,
            adapter=ChatAdapter(),
            session_lister=lambda _dir: [],
            message_loader=lambda _sid, _dir: [],
        )
        app = create_app(
            workflows_root=self.workflows,
            recordings_root=self.recordings,
            agent_chat_service=service,
        )
        client = TestClient(app)

        first = client.post("/api/agent/chat", json={"message": "hello", "session_id": None})
        self.assertEqual(first.status_code, 200, first.text)

        second = client.post("/api/agent/chat", json={"message": "hello again", "session_id": first.json()["session_id"]})
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["session_id"], "session-1")
        self.assertEqual(seen_session_ids, [None, "session-1"])

    def test_tasks_page_has_agent_mode_button(self) -> None:
        app = create_app(workflows_root=self.workflows, recordings_root=self.recordings)
        client = TestClient(app)

        response = client.get("/tasks")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("Agent Mode", response.text)


if __name__ == "__main__":
    unittest.main()
