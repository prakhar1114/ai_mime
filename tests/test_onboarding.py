from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ai_mime import onboarding


class OnboardingHelperTests(unittest.TestCase):
    def test_merge_env_var_preserves_unrelated_keys(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env_path = Path(td) / ".env"
            env_path.write_text(
                "GEMINI_API_KEY=old-gemini\n"
                "OPENAI_API_KEY=old-openai\n"
                "ANTHROPIC_API_KEY=old-anthropic\n",
                encoding="utf-8",
            )

            onboarding._merge_env_var(env_path, "ANTHROPIC_API_KEY", "new-anthropic")

            self.assertEqual(
                env_path.read_text(encoding="utf-8"),
                "GEMINI_API_KEY=old-gemini\n"
                "OPENAI_API_KEY=old-openai\n"
                "ANTHROPIC_API_KEY=new-anthropic\n",
            )

    def test_merge_env_var_appends_missing_key(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env_path = Path(td) / ".env"
            env_path.write_text("GEMINI_API_KEY=old-gemini\n", encoding="utf-8")

            onboarding._merge_env_var(env_path, "ANTHROPIC_API_KEY", "new-anthropic")

            self.assertEqual(
                env_path.read_text(encoding="utf-8"),
                "GEMINI_API_KEY=old-gemini\nANTHROPIC_API_KEY=new-anthropic\n",
            )

    def test_detect_local_claude_reports_missing_cli(self) -> None:
        ok, message = onboarding._detect_local_claude(which=lambda _name: None)

        self.assertFalse(ok)
        self.assertIn("not found", message)

    def test_detect_local_claude_runs_version_check(self) -> None:
        calls: list[list[str]] = []

        def fake_run(args, **kwargs):
            calls.append(args)
            return SimpleNamespace(returncode=0, stdout="claude 2.0.0\n", stderr="")

        ok, message = onboarding._detect_local_claude(
            which=lambda _name: "/usr/local/bin/claude",
            run=fake_run,
        )

        self.assertTrue(ok)
        self.assertEqual(calls, [["/usr/local/bin/claude", "--version"]])
        self.assertIn("claude 2.0.0", message)

    def test_install_claude_skills_creates_idempotent_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skills_dir = root / "home" / ".claude" / "skills"
            browser_dir = root / "repo" / "harness" / "browser-harness"
            hermes_dir = root / "bundle" / "macos-computer-use"
            browser_dir.mkdir(parents=True)
            hermes_dir.mkdir(parents=True)
            (browser_dir / "SKILL.md").write_text("---\nname: browser-harness\n---\n", encoding="utf-8")
            (hermes_dir / "SKILL.md").write_text("---\nname: macos-computer-use\n---\n", encoding="utf-8")

            first = onboarding._install_claude_skills(
                skills_dir=skills_dir,
                browser_harness_skill_dir=browser_dir,
                hermes_skill_dir=hermes_dir,
            )
            second = onboarding._install_claude_skills(
                skills_dir=skills_dir,
                browser_harness_skill_dir=browser_dir,
                hermes_skill_dir=hermes_dir,
            )

            self.assertTrue((skills_dir / "browser-harness").is_symlink())
            self.assertTrue((skills_dir / "macos-computer-use").is_symlink())
            self.assertEqual((skills_dir / "browser-harness").resolve(), browser_dir.resolve())
            self.assertEqual((skills_dir / "macos-computer-use").resolve(), hermes_dir.resolve())
            self.assertEqual(first, second)

    def test_install_claude_skills_rejects_conflicting_real_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skills_dir = root / "home" / ".claude" / "skills"
            browser_dir = root / "repo" / "harness" / "browser-harness"
            hermes_dir = root / "bundle" / "macos-computer-use"
            browser_dir.mkdir(parents=True)
            hermes_dir.mkdir(parents=True)
            (browser_dir / "SKILL.md").write_text("---\nname: browser-harness\n---\n", encoding="utf-8")
            (hermes_dir / "SKILL.md").write_text("---\nname: macos-computer-use\n---\n", encoding="utf-8")
            (skills_dir / "browser-harness").mkdir(parents=True)

            with self.assertRaises(FileExistsError):
                onboarding._install_claude_skills(
                    skills_dir=skills_dir,
                    browser_harness_skill_dir=browser_dir,
                    hermes_skill_dir=hermes_dir,
                )


if __name__ == "__main__":
    unittest.main()
