from __future__ import annotations

import json
import os
import queue
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from ai_mime.editor.server import TaskRunner, create_app
from ai_mime.agent_runner import AgentRunRequest, AgentRunResult, WorkspaceAgentChatService
from ai_mime.reflect.workflow import reflect_session


class FakeDashboardAdapter:
    id = "claude_code"
    label = "Claude Code"

    def run(self, request: AgentRunRequest, prompt: str) -> AgentRunResult:
        return AgentRunResult(status="success", session_id=request.session_id or "session-1", summary="agent reply")


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

    def _skill_upload_files(self, prefix: str = "weather-skill") -> list[tuple[str, tuple[str, bytes, str]]]:
        skill_md = (
            "---\n"
            "name: fetch-weather\n"
            "description: Fetch weather for a location.\n"
            "---\n\n"
            "# Fetch Weather\n"
        ).encode()
        run_py = (
            "import argparse, json\n"
            "p = argparse.ArgumentParser()\n"
            "p.add_argument('--inputs-json', required=True)\n"
            "a = p.parse_args()\n"
            "json.load(open(a.inputs_json))\n"
            "print('{\"event\":\"workflow_done\",\"outputs\":{\"ok\":true}}')\n"
        ).encode()
        run_sh = (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "HERE=\"$(cd \"$(dirname \"$0\")\" && pwd)\"\n"
            "INPUTS=\"${1:-$HERE/inputs/inputs.example.json}\"\n"
            "exec python3 \"$HERE/scripts/run.py\" --inputs-json \"$INPUTS\"\n"
        ).encode()
        return [
            ("files", (f"{prefix}/SKILL.md", skill_md, "text/markdown")),
            ("files", (f"{prefix}/run.sh", run_sh, "application/x-sh")),
            ("files", (f"{prefix}/scripts/run.py", run_py, "text/x-python")),
            ("files", (f"{prefix}/inputs/inputs.example.json", b'{"location":"San Francisco, CA"}', "application/json")),
            ("files", (f"{prefix}/inputs/inputs.template.json", b'{"location":"<FILL IN: location>"}', "application/json")),
            ("files", (f"{prefix}/references/fallback_plan.md", b"# Fallback\n\nSearch weather manually.\n", "text/markdown")),
            ("files", (f"{prefix}/runs/old/data.md", b"old run", "text/markdown")),
            ("files", (f"{prefix}/scripts/__pycache__/run.pyc", b"cached", "application/octet-stream")),
        ]

    def _workflow_upload_files(self, prefix: str = "invoice-workflow") -> list[tuple[str, tuple[str, bytes, str]]]:
        files = self._skill_upload_files(f"{prefix}/skills/fetch-weather")
        files.extend([
            ("files", (f"{prefix}/metadata.json", b'{"name":"Invoice Workflow"}', "application/json")),
            ("files", (f"{prefix}/schema.json", b"{}", "application/json")),
            ("files", (f"{prefix}/optimized_plan.json", b"{}", "application/json")),
            ("files", (f"{prefix}/outputs/result.json", b"{}", "application/json")),
            ("files", (f"{prefix}/agent/memory.md", b"old memory", "text/markdown")),
        ])
        return files

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

    def test_direct_build_create_workflow_and_dashboard_row(self) -> None:
        app = create_app(workflows_root=self.workflows, recordings_root=self.recordings)
        client = TestClient(app)

        response = client.post("/api/direct-build/workflows", json={"name": "Summarize invoices"})

        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        task_id = data["task_id"]
        workflow = Path(data["workflow_dir"])
        self.assertTrue(workflow.exists())
        self.assertEqual(json.loads((workflow / "metadata.json").read_text(encoding="utf-8"))["source"], "direct_build")
        self.assertEqual(json.loads((workflow / "metadata.json").read_text(encoding="utf-8"))["name"], "Summarize invoices")
        self.assertEqual(json.loads((workflow / "schema.json").read_text(encoding="utf-8")), {})
        self.assertEqual(json.loads((workflow / "optimized_plan.json").read_text(encoding="utf-8")), {})

        rows = {row["id"]: row for row in client.get("/api/tasks").json()["tasks"]}
        self.assertIn(task_id, rows)
        self.assertEqual(rows[task_id]["status"], "ready")
        self.assertEqual(rows[task_id]["display_name"], "Summarize invoices")
        self.assertTrue(rows[task_id]["has_schema"])
        self.assertTrue(rows[task_id]["has_optimized_plan"])
        self.assertFalse(rows[task_id]["has_skill"])

        page = client.get(f"/skill-build/{task_id}")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn("Build Skill", page.text)

    def test_import_preview_and_install_bare_skill(self) -> None:
        app = create_app(workflows_root=self.workflows, recordings_root=self.recordings)
        client = TestClient(app)

        preview = client.post("/api/import/preview", files=self._skill_upload_files())

        self.assertEqual(preview.status_code, 200, preview.text)
        preview_data = preview.json()
        self.assertEqual(preview_data["detected_type"], "skill")
        self.assertTrue(preview_data["valid"])
        self.assertIn("runs/old/data.md", preview_data["removed_preview"])
        self.assertIn("scripts/__pycache__/run.pyc", preview_data["removed_preview"])

        install = client.post("/api/import/install", json={"staging_id": preview_data["staging_id"]})

        self.assertEqual(install.status_code, 200, install.text)
        task_id = install.json()["task_id"]
        workflow = Path(install.json()["workflow_dir"])
        self.assertEqual(json.loads((workflow / "metadata.json").read_text(encoding="utf-8"))["source"], "imported_skill")
        self.assertEqual(json.loads((workflow / "schema.json").read_text(encoding="utf-8")), {})
        self.assertEqual(json.loads((workflow / "optimized_plan.json").read_text(encoding="utf-8")), {})
        skill_dir = workflow / "skills" / "fetch-weather"
        self.assertTrue((skill_dir / "run.sh").exists())
        self.assertTrue(os.access(skill_dir / "run.sh", os.X_OK))
        self.assertFalse((skill_dir / "runs").exists())
        self.assertFalse((skill_dir / "scripts" / "__pycache__").exists())

        row = client.get(f"/api/tasks/{task_id}/status")
        self.assertEqual(row.status_code, 200, row.text)
        self.assertTrue(row.json()["has_skill"])
        self.assertTrue(row.json()["can_replay"])

    def test_import_preview_and_install_workflow_directory(self) -> None:
        app = create_app(workflows_root=self.workflows, recordings_root=self.recordings)
        client = TestClient(app)

        preview = client.post("/api/import/preview", files=self._workflow_upload_files())

        self.assertEqual(preview.status_code, 200, preview.text)
        preview_data = preview.json()
        self.assertEqual(preview_data["detected_type"], "workflow")
        self.assertEqual(preview_data["display_name"], "Invoice Workflow")
        self.assertIn("outputs/result.json", preview_data["removed_preview"])
        self.assertIn("agent/memory.md", preview_data["removed_preview"])

        install = client.post("/api/import/install", json={"staging_id": preview_data["staging_id"]})

        self.assertEqual(install.status_code, 200, install.text)
        workflow = Path(install.json()["workflow_dir"])
        self.assertEqual(json.loads((workflow / "metadata.json").read_text(encoding="utf-8"))["name"], "Invoice Workflow")
        self.assertTrue((workflow / "skills" / "fetch-weather" / "run.sh").exists())
        self.assertFalse((workflow / "outputs").exists())
        self.assertFalse((workflow / "agent").exists())

        rows = {row["id"]: row for row in client.get("/api/tasks").json()["tasks"]}
        self.assertIn(install.json()["task_id"], rows)
        self.assertTrue(rows[install.json()["task_id"]]["has_skill"])
        self.assertTrue(rows[install.json()["task_id"]]["can_replay"])

    def test_import_preview_rejects_invalid_and_unsafe_folders(self) -> None:
        app = create_app(workflows_root=self.workflows, recordings_root=self.recordings)
        client = TestClient(app)

        invalid = client.post(
            "/api/import/preview",
            files=[("files", ("not-a-skill/readme.txt", b"hello", "text/plain"))],
        )
        self.assertEqual(invalid.status_code, 400, invalid.text)
        self.assertIn("not a valid AI Mime skill", invalid.text)

        unsafe = client.post(
            "/api/import/preview",
            files=[("files", ("bad/../SKILL.md", b"unsafe", "text/markdown"))],
        )
        self.assertEqual(unsafe.status_code, 400, unsafe.text)
        self.assertIn("Unsafe upload path", unsafe.text)

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
            id = "claude_code"

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
        self.assertEqual(models.json()["models"], [])

        created = client.post("/api/agent/sessions")
        self.assertEqual(created.status_code, 200, created.text)
        self.assertIsNone(created.json()["session_id"])
        self.assertFalse((self.workflows / ".agent" / "agent_sessions.json").exists())

        chat = client.post("/api/agent/chat", json={"message": "hello", "session_id": None, "model": None})
        self.assertEqual(chat.status_code, 200, chat.text)
        self.assertEqual(chat.json()["session_id"], "session-1")
        self.assertEqual(chat.json()["assistant_text"], "agent reply")
        self.assertIsNone(chat.json()["model"])
        self.assertEqual(seen_models, [None])

        messages = client.get("/api/agent/sessions/session-1/messages")
        self.assertEqual(messages.status_code, 200, messages.text)
        self.assertEqual(messages.json()["messages"][0]["message"], "hello")

    def test_agent_api_accepts_sequential_recovery_turns(self) -> None:
        seen_session_ids: list[str | None] = []

        class ChatAdapter:
            id = "claude_code"

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
        self.assertIn("Provider", response.text)
        self.assertIn("Direct build", response.text)
        self.assertIn("Upload skill", response.text)

    def test_direct_build_static_wiring_exists(self) -> None:
        tasks_js = Path("src/ai_mime/editor/web/tasks.js").read_text(encoding="utf-8")
        skill_build_js = Path("src/ai_mime/editor/web/skill_build.js").read_text(encoding="utf-8")

        self.assertIn("/api/direct-build/workflows", tasks_js)
        self.assertIn("action=direct-start", tasks_js)
        self.assertIn("/api/import/preview", tasks_js)
        self.assertIn("/api/import/install", tasks_js)
        self.assertIn("webkitdirectory", tasks_js)
        self.assertIn("direct-start", skill_build_js)
        self.assertIn("Start by asking me what task this skill should perform", skill_build_js)

    def test_provider_settings_api_reports_status(self) -> None:
        app = create_app(
            workflows_root=self.workflows,
            recordings_root=self.recordings,
            agent_chat_service=WorkspaceAgentChatService(
                workspace_dir=self.workflows,
                adapter=FakeDashboardAdapter(),
                session_lister=lambda _dir: [],
                message_loader=lambda _sid, _dir: [],
            ),
        )
        client = TestClient(app)

        with patch(
            "ai_mime.editor.server.provider_settings_status",
            return_value={
                "provider": "anthropic",
                "providers": {
                    "anthropic": {"available": True, "label": "Anthropic / Claude Code"},
                    "openai": {"available": False, "label": "OpenAI / Codex"},
                },
            },
        ):
            response = client.get("/api/settings/provider")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["provider"], "anthropic")

    def test_provider_settings_api_updates_provider_and_rebuilds_services(self) -> None:
        created_services: list[WorkspaceAgentChatService] = []

        class FakeService:
            def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                created_services.append(self)  # type: ignore[arg-type]

            def status(self):  # type: ignore[no-untyped-def]
                return {"active_session_id": None, "sessions": [], "models": []}

            def list_models(self):  # type: ignore[no-untyped-def]
                return {"models": []}

        app = create_app(
            workflows_root=self.workflows,
            recordings_root=self.recordings,
            agent_chat_service=WorkspaceAgentChatService(
                workspace_dir=self.workflows,
                adapter=FakeDashboardAdapter(),
                session_lister=lambda _dir: [],
                message_loader=lambda _sid, _dir: [],
            ),
        )
        client = TestClient(app)

        with patch(
            "ai_mime.editor.server.save_provider_settings",
            return_value={"provider": "openai", "providers": {}},
        ) as save, patch("ai_mime.editor.server.WorkspaceAgentChatService", FakeService):
            response = client.post("/api/settings/provider", json={"provider": "openai", "api_key": "sk-test"})

        self.assertEqual(response.status_code, 200, response.text)
        save.assert_called_once_with("openai", api_key="sk-test")
        self.assertEqual(response.json()["provider"], "openai")
        self.assertEqual(len(created_services), 1)

    def test_provider_settings_helper_writes_provider_and_api_key(self) -> None:
        from ai_mime import provider_settings

        config_path = self.root / "user_config.yml"
        env_path = self.root / ".env"
        with patch("ai_mime.provider_settings.get_user_config_path", return_value=config_path), patch(
            "ai_mime.provider_settings.get_env_path",
            return_value=env_path,
        ), patch.dict("os.environ", {}, clear=True), patch(
            "ai_mime.provider_settings._provider_runtime_status",
            return_value=(False, "runtime unavailable"),
        ):
            status = provider_settings.save_provider_settings("openai", api_key="sk-test")

        self.assertEqual(status["provider"], "openai")
        self.assertEqual(config_path.read_text(encoding="utf-8"), "config_version: 1\nprovider: openai\n")
        self.assertIn("OPENAI_API_KEY=sk-test", env_path.read_text(encoding="utf-8"))

    def test_provider_settings_helper_allows_runtime_login_without_key(self) -> None:
        from ai_mime import provider_settings

        config_path = self.root / "user_config.yml"
        env_path = self.root / ".env"
        with patch("ai_mime.provider_settings.get_user_config_path", return_value=config_path), patch(
            "ai_mime.provider_settings.get_env_path",
            return_value=env_path,
        ), patch.dict("os.environ", {}, clear=True), patch(
            "ai_mime.provider_settings._provider_runtime_status",
            return_value=(True, "Codex login detected"),
        ):
            status = provider_settings.save_provider_settings("openai")

        self.assertEqual(status["provider"], "openai")
        self.assertEqual(config_path.read_text(encoding="utf-8"), "config_version: 1\nprovider: openai\n")

    def test_codex_login_status_uses_app_aware_path(self) -> None:
        from ai_mime import provider_settings

        captured: dict[str, object] = {}

        def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env")
            return type("Proc", (), {"returncode": 0, "stdout": "Logged in using ChatGPT\n", "stderr": ""})()

        with (
            patch.dict("os.environ", {"HOME": str(self.root), "PATH": "/usr/bin:/bin"}, clear=True),
            patch(
                "ai_mime.provider_settings._find_codex_exe",
                return_value="/opt/homebrew/bin/codex",
            ),
            patch("ai_mime.provider_settings.subprocess.run", side_effect=fake_run),
        ):
            ok, message = provider_settings._provider_runtime_status("openai")

        self.assertTrue(ok)
        self.assertIn("Logged in", message)
        env = captured["env"]
        self.assertIsInstance(env, dict)
        path = env["PATH"].split(os.pathsep)
        self.assertIn("/opt/homebrew/bin", path)
        self.assertIn("/usr/local/bin", path)
        self.assertEqual(env["HOME"], str(self.root))

    def test_provider_settings_helper_rejects_unavailable_provider(self) -> None:
        from ai_mime import provider_settings

        config_path = self.root / "user_config.yml"
        env_path = self.root / ".env"
        with patch("ai_mime.provider_settings.get_user_config_path", return_value=config_path), patch(
            "ai_mime.provider_settings.get_env_path",
            return_value=env_path,
        ), patch.dict("os.environ", {}, clear=True), patch(
            "ai_mime.provider_settings._provider_runtime_status",
            return_value=(False, "Codex login check failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "Codex login check failed"):
                provider_settings.save_provider_settings("openai")
        self.assertFalse(config_path.exists())

    def test_provider_settings_helper_rejects_unknown_provider(self) -> None:
        from ai_mime import provider_settings

        with self.assertRaisesRegex(ValueError, "provider must be anthropic or openai"):
            provider_settings.save_provider_settings("custom")


if __name__ == "__main__":
    unittest.main()
