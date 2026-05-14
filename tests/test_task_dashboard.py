from __future__ import annotations

import json
import queue
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from ai_mime.editor.server import TaskRunner, create_app


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


if __name__ == "__main__":
    unittest.main()
