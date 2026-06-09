from __future__ import annotations

import json
import queue
import stat
import tempfile
import threading
import time
import textwrap
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from ai_mime.editor.server import create_app


def _events_from_sse(text: str) -> list[dict]:
    events: list[dict] = []
    for block in text.split("\n\n"):
        for line in block.splitlines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload:
                events.append(json.loads(payload))
    return events


class SkillRunStreamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workflows = self.root / "workflows"
        self.recordings = self.root / "recordings"
        self.task_id = "20260516T000000Z-test"
        self.workflow = self.workflows / self.task_id
        self.skill = self.workflow / "skills" / "test-skill"
        self.skill.mkdir(parents=True)
        (self.workflow / "schema.json").write_text(json.dumps({"plan": {"subtasks": []}}), encoding="utf-8")
        (self.workflow / "metadata.json").write_text(json.dumps({"name": "Test Skill"}), encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_run_sh(self, body: str, *, executable: bool = True) -> None:
        run_sh = self.skill / "run.sh"
        run_sh.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            + textwrap.dedent(body).lstrip(),
            encoding="utf-8",
        )
        if executable:
            run_sh.chmod(run_sh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def _client(self) -> TestClient:
        app = create_app(workflows_root=self.workflows, recordings_root=self.recordings)
        return TestClient(app)

    def _run_dirs(self) -> list[Path]:
        runs = self.workflow / "runs"
        if not runs.exists():
            return []
        return sorted(path for path in runs.iterdir() if path.is_dir())

    def _write_replay_metadata_files(self, *, preconditions: str = "") -> None:
        self._write_run_sh("echo ok\n")
        skill_md = (
            "---\n"
            "name: test-skill\n"
            "description: Run a test skill from replay.\n"
            "---\n\n"
            "# Test Skill\n\n"
        )
        if preconditions:
            skill_md += f"## Preconditions\n{preconditions}\n\n"
        skill_md += "## Inputs\n- `name` (required, string): Person name.\n"
        (self.skill / "SKILL.md").write_text(skill_md, encoding="utf-8")
        (self.skill / "inputs").mkdir(exist_ok=True)
        (self.skill / "inputs" / "inputs.template.json").write_text(
            json.dumps({"name": "<FILL IN: person name>", "count": "<OPTIONAL: repeat count>"}),
            encoding="utf-8",
        )
        (self.skill / "inputs" / "inputs.example.json").write_text(
            json.dumps({"name": "Ada", "count": 2}),
            encoding="utf-8",
        )

    def test_skill_run_streams_logs_and_parses_workflow_outputs(self) -> None:
        self._write_run_sh(
            r'''
            python3 - "$1" <<'PY'
            import json, sys
            inputs = json.load(open(sys.argv[1]))
            print("stdout:" + inputs["name"], flush=True)
            print("plain stderr", file=sys.stderr, flush=True)
            print(json.dumps({"event":"workflow_done","outputs":{"greeting":"hello " + inputs["name"]}}), file=sys.stderr, flush=True)
            PY
            '''
        )
        client = self._client()

        response = client.post(
            f"/api/tasks/{self.task_id}/skill/run/stream",
            json={"params": {"name": "Ada"}},
        )

        self.assertEqual(response.status_code, 200, response.text)
        events = _events_from_sse(response.text)
        self.assertEqual(events[0]["event"], "started")
        self.assertIn({"event": "stdout", "line": "stdout:Ada"}, events)
        self.assertIn({"event": "stderr", "line": "plain stderr"}, events)
        output_events = [event for event in events if event.get("event") == "output"]
        self.assertEqual(output_events[-1]["key"], "workflow_done")
        self.assertEqual(output_events[-1]["value"], {"greeting": "hello Ada"})
        done = events[-1]
        self.assertEqual(done["event"], "done")
        self.assertTrue(done["success"])
        self.assertEqual(done["exit_code"], 0)
        self.assertIn("stdout:Ada", done["stdout_log"])
        self.assertIn("plain stderr", done["stderr_log"])
        self.assertEqual(done["outputs"], {"greeting": "hello Ada"})
        self.assertIsInstance(done.get("run_id"), str)
        self.assertIsInstance(done.get("run_dir"), str)

        run_dirs = self._run_dirs()
        self.assertEqual(len(run_dirs), 1)
        self.assertEqual(Path(done["run_dir"]).resolve(), run_dirs[0].resolve())
        data = (run_dirs[0] / "data.md").read_text(encoding="utf-8")
        self.assertIn("## Input", data)
        self.assertIn('"name": "Ada"', data)
        self.assertIn("## Output", data)
        self.assertIn('"greeting": "hello Ada"', data)
        self.assertNotIn("## Error", data)
        self.assertFalse((run_dirs[0] / "assets").exists())

    def test_skill_run_reports_nonzero_exit_with_separate_logs(self) -> None:
        self._write_run_sh(
            r'''
            echo "before failure"
            echo "bad selector" >&2
            exit 7
            '''
        )
        client = self._client()

        response = client.post(f"/api/tasks/{self.task_id}/skill/run/stream", json={"params": {}})

        self.assertEqual(response.status_code, 200, response.text)
        events = _events_from_sse(response.text)
        done = events[-1]
        self.assertEqual(done["event"], "done")
        self.assertFalse(done["success"])
        self.assertEqual(done["exit_code"], 7)
        self.assertIn("before failure", done["stdout_log"])
        self.assertIn("bad selector", done["stderr_log"])

        run_dirs = self._run_dirs()
        self.assertEqual(len(run_dirs), 1)
        data = (run_dirs[0] / "data.md").read_text(encoding="utf-8")
        self.assertIn("## Input", data)
        self.assertIn("## Output", data)
        self.assertIn("## Error", data)
        self.assertIn("run.sh exited with code 7", data)

    def test_skill_run_copies_changed_assets_into_run_folder(self) -> None:
        self._write_run_sh(
            r'''
            mkdir -p ../../outputs/assets/reports
            echo "asset body" > ../../outputs/assets/reports/result.txt
            python3 - <<'PY'
            import json, sys
            print(json.dumps({"event":"workflow_done","outputs":{"asset":"reports/result.txt"}}), file=sys.stderr, flush=True)
            PY
            '''
        )
        client = self._client()

        response = client.post(f"/api/tasks/{self.task_id}/skill/run/stream", json={"params": {"name": "Ada"}})

        self.assertEqual(response.status_code, 200, response.text)
        run_dirs = self._run_dirs()
        self.assertEqual(len(run_dirs), 1)
        copied = run_dirs[0] / "assets" / "reports" / "result.txt"
        self.assertEqual(copied.read_text(encoding="utf-8").strip(), "asset body")
        data = (run_dirs[0] / "data.md").read_text(encoding="utf-8")
        self.assertIn("## Assets", data)
        self.assertIn("[result.txt](assets/reports/result.txt)", data)

    def test_skill_run_rejects_non_executable_run_sh(self) -> None:
        self._write_run_sh("echo nope\n", executable=False)
        client = self._client()

        response = client.post(f"/api/tasks/{self.task_id}/skill/run/stream", json={"params": {}})

        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("run.sh is not executable", response.text)

    def test_skill_kill_terminates_process_group_children(self) -> None:
        child_started = self.workflow / "child-started.txt"
        child_survived = self.workflow / "child-survived.txt"
        self._write_run_sh(
            f'''
            python3 - <<'PY' &
            import pathlib, time
            pathlib.Path({str(child_started)!r}).write_text("started", encoding="utf-8")
            time.sleep(2)
            pathlib.Path({str(child_survived)!r}).write_text("survived", encoding="utf-8")
            PY
            child=$!
            echo "child:$child"
            wait "$child"
            '''
        )
        client = self._client()
        seen: queue.Queue[dict | Exception] = queue.Queue()

        def _consume_stream() -> None:
            try:
                with client.stream(
                    "POST",
                    f"/api/tasks/{self.task_id}/skill/run/stream",
                    json={"params": {}},
                ) as response:
                    self.assertEqual(response.status_code, 200)
                    for line in response.iter_lines():
                        if not line.startswith("data:"):
                            continue
                        seen.put(json.loads(line[5:].strip()))
            except Exception as e:
                seen.put(e)

        thread = threading.Thread(target=_consume_stream, daemon=True)
        thread.start()

        deadline = time.monotonic() + 3
        while not child_started.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(child_started.exists(), "child process did not start")

        kill_response = client.post(f"/api/tasks/{self.task_id}/skill/kill")

        self.assertEqual(kill_response.status_code, 200, kill_response.text)
        self.assertEqual(kill_response.json()["ok"], True)
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive(), "run stream did not finish after kill")
        time.sleep(2.2)
        self.assertFalse(child_survived.exists(), "child process survived skill kill")

        events: list[dict] = []
        while not seen.empty():
            item = seen.get()
            if isinstance(item, Exception):
                raise item
            events.append(item)
        done = [event for event in events if event.get("event") == "done"][-1]
        self.assertFalse(done["success"])
        self.assertNotEqual(done["exit_code"], 0)

    def test_inputs_template_includes_examples_and_skill_metadata(self) -> None:
        self._write_replay_metadata_files(
            preconditions="- User is signed in.\n- Network access is available."
        )
        client = self._client()

        response = client.get(f"/api/tasks/{self.task_id}/skill/inputs-template")

        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["template"]["name"], "<FILL IN: person name>")
        self.assertEqual(data["examples"], {"name": "Ada", "count": 2})
        self.assertEqual(data["skill"]["name"], "test-skill")
        self.assertEqual(data["skill"]["description"], "Run a test skill from replay.")
        self.assertEqual(data["skill"]["preconditions"], ["User is signed in.", "Network access is available."])

    def test_inputs_template_omits_absent_preconditions(self) -> None:
        self._write_replay_metadata_files()
        client = self._client()

        response = client.get(f"/api/tasks/{self.task_id}/skill/inputs-template")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["skill"]["preconditions"], [])

    def test_open_skill_folder_queues_validated_directory(self) -> None:
        self._write_replay_metadata_files()
        command_q: queue.Queue = queue.Queue()
        app = create_app(
            workflows_root=self.workflows,
            recordings_root=self.recordings,
            app_command_queue=command_q,
        )
        client = TestClient(app)

        response = client.post(f"/api/tasks/{self.task_id}/skill/open-folder")

        self.assertEqual(response.status_code, 200, response.text)
        command = command_q.get_nowait()
        self.assertEqual(command["type"], "open_directory")
        self.assertEqual(Path(command["path"]).resolve(), self.skill.resolve())

    def test_replay_agent_sessions_endpoint_is_task_scoped(self) -> None:
        (self.workflow / "optimized_plan.json").write_text(
            json.dumps({"user_filesystem_access": {"readable_roots": [], "writable_roots": []}}),
            encoding="utf-8",
        )
        (self.skill / "run.sh").write_text("#!/usr/bin/env bash\necho ok\n", encoding="utf-8")
        (self.skill / "run.sh").chmod(
            (self.skill / "run.sh").stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        )
        client = self._client()

        response = client.get(f"/api/tasks/{self.task_id}/replay-agent/sessions")

        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["workspace_dir"], str(self.workflow))
        self.assertIn("models", data)
        self.assertIn("sessions", data)


if __name__ == "__main__":
    unittest.main()
