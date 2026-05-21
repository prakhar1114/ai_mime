from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_mime import app_data


class AppDataPathTests(unittest.TestCase):
    def test_dev_uv_path_uses_path_lookup(self) -> None:
        with patch.object(app_data, "is_frozen", return_value=False), patch.object(
            app_data.shutil, "which", return_value="/opt/homebrew/bin/uv"
        ):
            self.assertEqual(app_data.get_uv_path(), Path("/opt/homebrew/bin/uv"))

    def test_frozen_uv_path_uses_bundled_resource(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bundled_uv = root / "bin" / "uv"
            bundled_uv.parent.mkdir(parents=True)
            bundled_uv.write_text("#!/bin/sh\n", encoding="utf-8")
            bundled_uv.chmod(0o755)
            old_meipass = getattr(sys, "_MEIPASS", None)
            sys._MEIPASS = str(root)  # type: ignore[attr-defined]
            try:
                with patch.object(app_data, "is_frozen", return_value=True):
                    uv_path = app_data.get_uv_path()
                    self.assertEqual(uv_path, bundled_uv)
                    self.assertTrue(uv_path.is_file())
            finally:
                if old_meipass is None:
                    delattr(sys, "_MEIPASS")
                else:
                    sys._MEIPASS = old_meipass  # type: ignore[attr-defined]

    def test_python_path_prefers_workflow_venv(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workflow = Path(td)
            venv_python = workflow / ".venv" / "bin" / "python"
            venv_python.parent.mkdir(parents=True)
            venv_python.write_text("#!/bin/sh\n", encoding="utf-8")
            venv_python.chmod(0o755)

            self.assertEqual(app_data.get_python_path(workflow), venv_python)

    def test_dev_python_path_uses_current_interpreter(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.object(app_data, "is_frozen", return_value=False):
            workflow = Path(td)
            self.assertEqual(app_data.get_python_path(workflow), Path(sys.executable))

    def test_frozen_python_path_finds_managed_python(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            managed_python = root / "cpython-3.12.0-macos-aarch64-none" / "bin" / "python3.12"
            managed_python.parent.mkdir(parents=True)
            managed_python.write_text("#!/bin/sh\n", encoding="utf-8")
            managed_python.chmod(0o755)

            with patch.object(app_data, "APP_DATA_DIR", root.parent), patch.object(
                app_data, "get_managed_python_install_dir", return_value=root
            ), patch.object(app_data, "is_frozen", return_value=True):
                self.assertEqual(app_data.get_python_path(), managed_python)

    def test_workflow_runtime_env_exports_resolved_paths(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workflow = Path(td)
            venv_python = workflow / ".venv" / "bin" / "python"
            venv_python.parent.mkdir(parents=True)
            venv_python.write_text("#!/bin/sh\n", encoding="utf-8")
            venv_python.chmod(0o755)

            env = app_data.workflow_runtime_env(workflow)

            self.assertEqual(env["AI_MIME_PYTHON_PATH"], str(venv_python))
            self.assertIn("AI_MIME_UV_PATH", env)
            self.assertIn("UV_PYTHON_INSTALL_DIR", env)

    def test_frozen_workflow_runtime_env_prepends_tool_bin(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.dict("os.environ", {"PATH": "/usr/bin:/bin"}):
            root = Path(td)
            managed_python = root / "python" / "cpython-3.12.0-macos-aarch64-none" / "bin" / "python3.12"
            managed_python.parent.mkdir(parents=True)
            managed_python.write_text("#!/bin/sh\n", encoding="utf-8")
            managed_python.chmod(0o755)
            bundle = root / "bundle"
            browser_harness = bundle / "harness" / "browser-harness"
            browser_harness.mkdir(parents=True)

            old_meipass = getattr(sys, "_MEIPASS", None)
            sys._MEIPASS = str(bundle)  # type: ignore[attr-defined]
            try:
                with patch.object(app_data, "APP_DATA_DIR", root), patch.object(
                    app_data, "is_frozen", return_value=True
                ):
                    env = app_data.workflow_runtime_env()
            finally:
                if old_meipass is None:
                    delattr(sys, "_MEIPASS")
                else:
                    sys._MEIPASS = old_meipass  # type: ignore[attr-defined]

            self.assertEqual(env["PATH"], f"{root / 'bin'}:/usr/bin:/bin")
            self.assertEqual(env["AI_MIME_BROWSER_SKILL_NAME"], "browser")
            self.assertEqual(env["AI_MIME_BROWSER_SKILL_PATH"], str(browser_harness))
            self.assertNotIn("AI_MIME_BROWSER_HARNESS_BIN", env)


if __name__ == "__main__":
    unittest.main()
