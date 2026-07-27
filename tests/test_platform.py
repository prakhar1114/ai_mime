from pathlib import Path
import sys
import unittest

from ai_mime.platform import (
    executable_name,
    get_default_app_data_dir,
    get_system_paths,
    get_venv_python_relpath,
    is_windows,
    is_mac,
    is_link,
)


class TestPlatform(unittest.TestCase):
    def test_app_data_dir(self):
        app_data = get_default_app_data_dir()
        self.assertIsInstance(app_data, Path)
        self.assertTrue(str(app_data).endswith("AI Mime") or str(app_data).endswith("ai_mime"))

    def test_executable_name(self):
        if is_windows():
            self.assertEqual(executable_name("uv"), "uv.exe")
            self.assertEqual(executable_name("uv.exe"), "uv.exe")
        else:
            self.assertEqual(executable_name("uv"), "uv")

    def test_venv_python_relpath(self):
        rel = get_venv_python_relpath()
        if is_windows():
            self.assertEqual(rel, Path("Scripts") / "python.exe")
        else:
            self.assertEqual(rel, Path("bin") / "python")

    def test_system_paths(self):
        paths = get_system_paths()
        self.assertIsInstance(paths, list)
        self.assertGreater(len(paths), 0)


if __name__ == "__main__":
    unittest.main()
