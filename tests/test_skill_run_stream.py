from __future__ import annotations

import json
import stat
import tempfile
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
