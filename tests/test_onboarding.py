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
        ok, message = onboarding._detect_local_claude(
            which=lambda _name: None,
            is_file=lambda _path: False,
        )

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

    def test_detect_local_claude_checks_fallback_locations(self) -> None:
        calls: list[list[str]] = []
        home = Path("/Users/tester")
        fallback = home / ".local" / "bin" / "claude"

        def fake_run(args, **kwargs):
            calls.append(args)
            return SimpleNamespace(returncode=0, stdout="2.1.144 (Claude Code)\n", stderr="")

        ok, message = onboarding._detect_local_claude(
            which=lambda _name: None,
            run=fake_run,
            home=home,
            is_file=lambda path: Path(path) == fallback,
        )

        self.assertTrue(ok)
        self.assertEqual(calls, [[str(fallback), "--version"]])
        self.assertIn("2.1.144", message)

    def test_install_claude_skills_creates_idempotent_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skills_dir = root / "home" / ".claude" / "skills"
            env_path = root / ".env"
            browser_dir = root / "repo" / "harness" / "browser-harness"
            hermes_dir = root / "bundle" / "macos-computer-use"
            browser_dir.mkdir(parents=True)
            hermes_dir.mkdir(parents=True)
            (browser_dir / "SKILL.md").write_text("browser-harness skill\n", encoding="utf-8")
            (hermes_dir / "SKILL.md").write_text("---\nname: macos-computer-use\n---\n", encoding="utf-8")

            first = onboarding._install_claude_skills(
                skills_dir=skills_dir,
                browser_harness_skill_dir=browser_dir,
                hermes_skill_dir=hermes_dir,
                env_path=env_path,
            )
            second = onboarding._install_claude_skills(
                skills_dir=skills_dir,
                browser_harness_skill_dir=browser_dir,
                hermes_skill_dir=hermes_dir,
                env_path=env_path,
            )

            self.assertTrue((skills_dir / "browser").is_symlink())
            self.assertTrue((skills_dir / "macos-computer-use").is_symlink())
            self.assertEqual((skills_dir / "browser").resolve(), browser_dir.resolve())
            self.assertEqual((skills_dir / "macos-computer-use").resolve(), hermes_dir.resolve())
            self.assertEqual(first, second)
            self.assertIn(f"AI_MIME_BROWSER_SKILL_PATH={browser_dir.resolve()}", env_path.read_text(encoding="utf-8"))
            self.assertIn(
                f"AI_MIME_MACOS_COMPUTER_USE_SKILL_PATH={hermes_dir.resolve()}",
                env_path.read_text(encoding="utf-8"),
            )

    def test_install_claude_skills_accepts_existing_compatible_skills(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skills_dir = root / "home" / ".claude" / "skills"
            env_path = root / ".env"
            existing_browser = root / "existing" / "browser-harness"
            existing_macos = root / "existing" / "macos-computer-use"
            bundled_browser = root / "bundle" / "browser-harness"
            bundled_macos = root / "bundle" / "macos-computer-use"
            for path in (existing_browser, bundled_browser):
                path.mkdir(parents=True)
                (path / "SKILL.md").write_text("browser-harness skill\n", encoding="utf-8")
            for path in (existing_macos, bundled_macos):
                path.mkdir(parents=True)
                (path / "SKILL.md").write_text("---\nname: macos-computer-use\n---\n", encoding="utf-8")
            skills_dir.mkdir(parents=True)
            (skills_dir / "browser").symlink_to(existing_browser, target_is_directory=True)
            (skills_dir / "macos-computer-use").symlink_to(existing_macos, target_is_directory=True)

            result = onboarding._install_claude_skills(
                skills_dir=skills_dir,
                browser_harness_skill_dir=bundled_browser,
                hermes_skill_dir=bundled_macos,
                env_path=env_path,
            )

            self.assertEqual((skills_dir / "browser").resolve(), existing_browser.resolve())
            self.assertEqual((skills_dir / "macos-computer-use").resolve(), existing_macos.resolve())
            self.assertEqual(result["browser"], skills_dir / "browser")
            env_text = env_path.read_text(encoding="utf-8")
            self.assertIn(f"AI_MIME_BROWSER_SKILL_PATH={existing_browser.resolve()}", env_text)
            self.assertIn(f"AI_MIME_MACOS_COMPUTER_USE_SKILL_PATH={existing_macos.resolve()}", env_text)

    def test_detect_claude_skills_accepts_legacy_browser_harness_link(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skills_dir = root / "home" / ".claude" / "skills"
            browser_dir = root / "existing" / "browser-harness"
            macos_dir = root / "existing" / "macos-computer-use"
            browser_dir.mkdir(parents=True)
            macos_dir.mkdir(parents=True)
            (browser_dir / "SKILL.md").write_text("browser-harness skill\n", encoding="utf-8")
            (macos_dir / "SKILL.md").write_text("---\nname: macos-computer-use\n---\n", encoding="utf-8")
            skills_dir.mkdir(parents=True)
            (skills_dir / "browser-harness").symlink_to(browser_dir, target_is_directory=True)
            (skills_dir / "macos-computer-use").symlink_to(macos_dir, target_is_directory=True)

            browser, macos = onboarding._detect_claude_skills(skills_dir=skills_dir)

            self.assertIsNotNone(browser)
            self.assertIsNotNone(macos)
            assert browser is not None
            assert macos is not None
            self.assertEqual(browser.link_name, "browser-harness")
            self.assertEqual(browser.path, browser_dir.resolve())
            self.assertEqual(macos.path, macos_dir.resolve())

    def test_install_claude_skills_repairs_incompatible_browser_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skills_dir = root / "home" / ".claude" / "skills"
            env_path = root / ".env"
            wrong_browser = root / "existing" / "wrong-browser"
            correct_browser = root / "bundle" / "browser-harness"
            macos_dir = root / "existing" / "macos-computer-use"
            wrong_browser.mkdir(parents=True)
            (wrong_browser / "SKILL.md").write_text("not a browser skill\n", encoding="utf-8")
            correct_browser.mkdir(parents=True)
            (correct_browser / "SKILL.md").write_text("browser-harness skill\n", encoding="utf-8")
            macos_dir.mkdir(parents=True)
            (macos_dir / "SKILL.md").write_text("---\nname: macos-computer-use\n---\n", encoding="utf-8")
            skills_dir.mkdir(parents=True)
            (skills_dir / "browser").symlink_to(wrong_browser, target_is_directory=True)
            (skills_dir / "macos-computer-use").symlink_to(macos_dir, target_is_directory=True)

            onboarding._install_claude_skills(
                skills_dir=skills_dir,
                browser_harness_skill_dir=correct_browser,
                hermes_skill_dir=macos_dir,
                env_path=env_path,
            )

            self.assertEqual((skills_dir / "browser").resolve(), correct_browser.resolve())
            env_text = env_path.read_text(encoding="utf-8")
            self.assertIn(f"AI_MIME_BROWSER_SKILL_PATH={correct_browser.resolve()}", env_text)

    def test_install_claude_skills_rejects_conflicting_real_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skills_dir = root / "home" / ".claude" / "skills"
            env_path = root / ".env"
            browser_dir = root / "repo" / "harness" / "browser-harness"
            hermes_dir = root / "bundle" / "macos-computer-use"
            browser_dir.mkdir(parents=True)
            hermes_dir.mkdir(parents=True)
            (browser_dir / "SKILL.md").write_text("browser-harness skill\n", encoding="utf-8")
            (hermes_dir / "SKILL.md").write_text("---\nname: macos-computer-use\n---\n", encoding="utf-8")
            (skills_dir / "browser").mkdir(parents=True)

            with self.assertRaises(FileExistsError):
                onboarding._install_claude_skills(
                    skills_dir=skills_dir,
                    browser_harness_skill_dir=browser_dir,
                    hermes_skill_dir=hermes_dir,
                    env_path=env_path,
                )

    def test_install_managed_python_builds_uv_command(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            uv = root / "bin" / "uv"
            uv.parent.mkdir()
            uv.write_text("#!/bin/sh\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(args, **kwargs):
                calls.append(args)
                return SimpleNamespace(returncode=0, stdout="already installed\n", stderr="")

            ok, message = onboarding._install_managed_python(
                uv_path=uv,
                install_dir=root / "python",
                run=fake_run,
            )

            self.assertTrue(ok)
            self.assertIn("already installed", message)
            self.assertEqual(
                calls,
                [[str(uv), "python", "install", "3.12", "--install-dir", str(root / "python")]],
            )

    def test_install_managed_python_reports_missing_uv(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ok, message = onboarding._install_managed_python(
                uv_path=Path(td) / "missing-uv",
                install_dir=Path(td) / "python",
            )

            self.assertFalse(ok)
            self.assertIn("uv not found", message)

    def test_install_managed_python_reports_failed_install(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            uv = root / "uv"
            uv.write_text("#!/bin/sh\n", encoding="utf-8")

            def fake_run(args, **kwargs):
                return SimpleNamespace(returncode=2, stdout="", stderr="network unavailable\n")

            ok, message = onboarding._install_managed_python(
                uv_path=uv,
                install_dir=root / "python",
                run=fake_run,
            )

            self.assertFalse(ok)
            self.assertIn("network unavailable", message)


if __name__ == "__main__":
    unittest.main()
