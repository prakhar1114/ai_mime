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
            self.assertIn("AI_MIME_BROWSER_HARNESS_BIN", env)
            self.assertIn("UV_PYTHON_INSTALL_DIR", env)
            # uv isolation vars and PATH sanitization are frozen-only — in dev
            # (APP_DATA_DIR == repo root) they would pollute the working tree.
            self.assertNotIn("UV_TOOL_DIR", env)
            self.assertNotIn("UV_TOOL_BIN_DIR", env)
            self.assertNotIn("UV_CACHE_DIR", env)
            self.assertNotIn("UV_NO_CONFIG", env)
            self.assertNotIn("PATH", env)

    def test_frozen_workflow_runtime_env_sanitizes_path_and_exports_app_tools(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.dict(
            "os.environ", {"PATH": "/opt/homebrew/bin:/usr/local/bin:/Users/tester/.local/bin:/usr/bin:/bin"}
        ):
            root = Path(td)
            managed_python = root / "python" / "cpython-3.12.0-macos-aarch64-none" / "bin" / "python3.12"
            managed_python.parent.mkdir(parents=True)
            managed_python.write_text("#!/bin/sh\n", encoding="utf-8")
            managed_python.chmod(0o755)
            bundle = root / "bundle"
            bundled_bin = bundle / "bin"
            bundled_bin.mkdir(parents=True)
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

            self.assertEqual(env["PATH"], f"{root / 'bin'}:{bundled_bin}:/usr/bin:/bin:/usr/sbin:/sbin")
            self.assertNotIn("/opt/homebrew/bin", env["PATH"])
            self.assertNotIn("/usr/local/bin", env["PATH"])
            self.assertNotIn("/Users/tester/.local/bin", env["PATH"])
            self.assertEqual(env["AI_MIME_BROWSER_SKILL_NAME"], "browser")
            self.assertEqual(env["AI_MIME_BROWSER_SKILL_PATH"], str(browser_harness))
            self.assertEqual(env["AI_MIME_BROWSER_HARNESS_BIN"], str(root / "bin" / "browser-harness"))
            self.assertEqual(env["UV_TOOL_DIR"], str(root / "tools"))
            self.assertEqual(env["UV_TOOL_BIN_DIR"], str(root / "bin"))
            self.assertEqual(env["UV_CACHE_DIR"], str(root / "uv-cache"))
            self.assertEqual(env["UV_NO_CONFIG"], "1")

    def test_get_managed_browser_harness_path_frozen(self) -> None:
        with patch.object(app_data, "is_frozen", return_value=True), patch.object(
            app_data, "get_tool_bin_dir", return_value=Path("/fake/app_data/bin")
        ):
            self.assertEqual(app_data.get_managed_browser_harness_path(), Path("/fake/app_data/bin/browser-harness"))

    def test_get_managed_browser_harness_path_dev_in_sys_exe_parent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fake_python = Path(td) / "python"
            fake_harness = Path(td) / "browser-harness"
            fake_harness.write_text("#!/bin/sh\n", encoding="utf-8")
            fake_harness.chmod(0o755)

            with patch.object(app_data, "is_frozen", return_value=False), patch.object(
                sys, "executable", str(fake_python)
            ):
                self.assertEqual(app_data.get_managed_browser_harness_path(), fake_harness)

    def test_get_managed_browser_harness_path_dev_in_sub_repo_venv(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo_root = Path(td)
            sub_venv_harness = repo_root / "harness" / "browser-harness" / ".venv" / "bin" / "browser-harness"
            sub_venv_harness.parent.mkdir(parents=True)
            sub_venv_harness.write_text("#!/bin/sh\n", encoding="utf-8")
            sub_venv_harness.chmod(0o755)

            with patch.object(app_data, "is_frozen", return_value=False), patch.object(
                app_data, "_repo_root", return_value=repo_root
            ), patch.object(sys, "executable", "/fake/python"):
                self.assertEqual(app_data.get_managed_browser_harness_path(), sub_venv_harness)

    def test_get_managed_browser_harness_path_dev_on_path(self) -> None:
        with patch.object(app_data, "is_frozen", return_value=False), patch.object(
            app_data, "_repo_root", return_value=Path("/fake/repo")
        ), patch.object(sys, "executable", "/fake/python"), patch.object(
            app_data.shutil, "which", return_value="/usr/local/bin/browser-harness"
        ):
            self.assertEqual(app_data.get_managed_browser_harness_path(), Path("/usr/local/bin/browser-harness"))

    def test_get_managed_browser_harness_path_dev_fallback(self) -> None:
        with patch.object(app_data, "is_frozen", return_value=False), patch.object(
            app_data, "_repo_root", return_value=Path("/fake/repo")
        ), patch.object(sys, "executable", "/fake/python"), patch.object(
            app_data.shutil, "which", return_value=None
        ), patch.object(
            app_data, "get_tool_bin_dir", return_value=Path("/fake/app_data/bin")
        ):
            self.assertEqual(app_data.get_managed_browser_harness_path(), Path("/fake/app_data/bin/browser-harness"))

    def test_frozen_workflow_runtime_env_exports_pythonpath(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            bundle = Path(td) / "bundle"
            bundle.mkdir()
            old_meipass = getattr(sys, "_MEIPASS", None)
            sys._MEIPASS = str(bundle)  # type: ignore[attr-defined]
            try:
                with patch.object(app_data, "is_frozen", return_value=True):
                    env = app_data.workflow_runtime_env()
                    self.assertEqual(env["PYTHONPATH"], str(bundle))
            finally:
                if old_meipass is None:
                    delattr(sys, "_MEIPASS")
                else:
                    sys._MEIPASS = old_meipass  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
