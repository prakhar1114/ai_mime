from __future__ import annotations

import unittest
from pathlib import Path


class PackagingTests(unittest.TestCase):
    def test_pyinstaller_spec_bundles_uv_from_env(self) -> None:
        spec = (Path(__file__).resolve().parents[1] / "scripts" / "pyinstaller.spec").read_text(
            encoding="utf-8"
        )

        self.assertIn("UV_BINARY_PATH", spec)
        self.assertIn('(_uv_binary, "bin")', spec)
        self.assertIn("Run scripts/build.sh", spec)

    def test_pyinstaller_spec_bundles_full_browser_harness_tree(self) -> None:
        spec = (Path(__file__).resolve().parents[1] / "scripts" / "pyinstaller.spec").read_text(
            encoding="utf-8"
        )

        self.assertIn('os.path.join(_repo, "harness", "browser-harness")', spec)
        self.assertNotIn('"browser-harness", "SKILL.md"', spec)

    def test_build_script_exports_uv_binary_path(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "scripts" / "build.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("UV_BINARY_PATH=\"$(command -v uv || true)\"", script)
        self.assertIn("export UV_BINARY_PATH", script)
        self.assertIn("uv binary not found", script)


if __name__ == "__main__":
    unittest.main()
