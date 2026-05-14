from __future__ import annotations

import json
import queue
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from ai_mime.editor.server import TaskRunner, create_app
from ai_mime.agent_runner import AgentRunRequest, AgentRunResult, WorkspaceAgentChatService


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
        return wf

    def _recording(self, task_id: str) -> Path:
        rec = self.recordings / task_id
        rec.mkdir(parents=True)
        (rec / "manifest.jsonl").write_text("{}\n", encoding="utf-8")
        return rec

    def test_inventory_marks_ready_and_pending_tasks(self) -> None:
        self._ready_workflow("20260513T000000Z-ready")
        self._recording("20260513T000000Z-ready")
        self._recording("20260513T000100Z-pending")

        runner = TaskRunner(
            workflows_root=self.workflows,
            recordings_root=self.recordings,
            reflect_llm_cfg=None,
            replay_llm_cfg=None,
        )
        rows = {row["id"]: row for row in runner.list_tasks()}

        self.assertTrue(rows["20260513T000000Z-ready"]["can_replay"])
        self.assertTrue(rows["20260513T000000Z-ready"]["can_edit"])
        self.assertTrue(rows["20260513T000000Z-ready"]["can_reflect"])
        self.assertEqual(rows["20260513T000000Z-ready"]["status"], "ready")
        self.assertTrue(rows["20260513T000100Z-pending"]["can_reflect"])
        self.assertEqual(rows["20260513T000100Z-pending"]["status"], "pending_reflection")

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

    def test_reflect_and_replay_reject_missing_configs(self) -> None:
        self._ready_workflow("20260513T000000Z-ready")
        self._recording("20260513T000100Z-pending")
        app = create_app(workflows_root=self.workflows, recordings_root=self.recordings)
        client = TestClient(app)

        self.assertEqual(client.post("/api/tasks/20260513T000100Z-pending/reflect").status_code, 500)
        self.assertEqual(client.post("/api/tasks/20260513T000000Z-ready/replay").status_code, 500)

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
        self.assertFalse((self.workflows / ".agent" / "session_index.json").exists())

        chat = client.post("/api/agent/chat", json={"message": "hello", "session_id": None, "model": "opus"})
        self.assertEqual(chat.status_code, 200, chat.text)
        self.assertEqual(chat.json()["session_id"], "session-1")
        self.assertEqual(chat.json()["assistant_text"], "agent reply")
        self.assertEqual(chat.json()["model"], "opus")
        self.assertEqual(seen_models, ["opus"])

        messages = client.get("/api/agent/sessions/session-1/messages")
        self.assertEqual(messages.status_code, 200, messages.text)
        self.assertEqual(messages.json()["messages"][0]["message"], "hello")

    def test_agent_api_rejects_concurrent_turns(self) -> None:
        service = WorkspaceAgentChatService(
            workspace_dir=self.workflows,
            adapter=object(),
            session_lister=lambda _dir: [],
            message_loader=lambda _sid, _dir: [],
        )
        app = create_app(
            workflows_root=self.workflows,
            recordings_root=self.recordings,
            agent_chat_service=service,
        )
        client = TestClient(app)

        self.assertTrue(service._turn_lock.acquire(blocking=False))
        try:
            response = client.post("/api/agent/chat", json={"message": "hello", "session_id": None})
            self.assertEqual(response.status_code, 409, response.text)
        finally:
            service._turn_lock.release()

    def test_tasks_page_has_agent_mode_button(self) -> None:
        app = create_app(workflows_root=self.workflows, recordings_root=self.recordings)
        client = TestClient(app)

        response = client.get("/tasks")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("Agent Mode", response.text)


if __name__ == "__main__":
    unittest.main()
