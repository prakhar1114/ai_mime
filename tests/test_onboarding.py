from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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
            browser_dir.mkdir(parents=True)
            (browser_dir / "SKILL.md").write_text("browser-harness skill\n", encoding="utf-8")

            first = onboarding._install_claude_skills(
                skills_dir=skills_dir,
                browser_harness_skill_dir=browser_dir,
                env_path=env_path,
            )
            second = onboarding._install_claude_skills(
                skills_dir=skills_dir,
                browser_harness_skill_dir=browser_dir,
                env_path=env_path,
            )

            self.assertTrue((skills_dir / "browser").is_symlink())
            self.assertEqual((skills_dir / "browser").resolve(), browser_dir.resolve())
            self.assertEqual(first, second)
            self.assertIn(f"AI_MIME_BROWSER_SKILL_PATH={browser_dir.resolve()}", env_path.read_text(encoding="utf-8"))

    def test_install_claude_skills_replaces_existing_browser_with_bundled_source(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skills_dir = root / "home" / ".claude" / "skills"
            env_path = root / ".env"
            existing_browser = root / "existing" / "browser-harness"
            bundled_browser = root / "bundle" / "browser-harness"
            for path in (existing_browser, bundled_browser):
                path.mkdir(parents=True)
                (path / "SKILL.md").write_text("browser-harness skill\n", encoding="utf-8")
            skills_dir.mkdir(parents=True)
            (skills_dir / "browser").symlink_to(existing_browser, target_is_directory=True)

            result = onboarding._install_claude_skills(
                skills_dir=skills_dir,
                browser_harness_skill_dir=bundled_browser,
                env_path=env_path,
            )

            self.assertEqual((skills_dir / "browser").resolve(), bundled_browser.resolve())
            self.assertEqual(result["browser"], skills_dir / "browser")
            env_text = env_path.read_text(encoding="utf-8")
            self.assertIn(f"AI_MIME_BROWSER_SKILL_PATH={bundled_browser.resolve()}", env_text)

    def test_frozen_install_claude_skills_prefers_bundled_browser_harness(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skills_dir = root / "home" / ".claude" / "skills"
            env_path = root / ".env"
            existing_browser = root / "existing" / "browser-harness"
            bundled_browser = root / "bundle" / "browser-harness"
            for path in (existing_browser, bundled_browser):
                path.mkdir(parents=True)
                (path / "SKILL.md").write_text("browser-harness skill\n", encoding="utf-8")
            skills_dir.mkdir(parents=True)
            (skills_dir / "browser").symlink_to(existing_browser, target_is_directory=True)

            with patch.object(onboarding, "is_frozen", return_value=True):
                onboarding._install_claude_skills(
                    skills_dir=skills_dir,
                    browser_harness_skill_dir=bundled_browser,
                    env_path=env_path,
                )

            self.assertEqual((skills_dir / "browser").resolve(), bundled_browser.resolve())
            self.assertIn(f"AI_MIME_BROWSER_SKILL_PATH={bundled_browser.resolve()}", env_path.read_text(encoding="utf-8"))

    def test_detect_claude_skills_accepts_legacy_browser_harness_link(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skills_dir = root / "home" / ".claude" / "skills"
            browser_dir = root / "existing" / "browser-harness"
            browser_dir.mkdir(parents=True)
            (browser_dir / "SKILL.md").write_text("browser-harness skill\n", encoding="utf-8")
            skills_dir.mkdir(parents=True)
            (skills_dir / "browser-harness").symlink_to(browser_dir, target_is_directory=True)

            browser = onboarding._detect_claude_skills(skills_dir=skills_dir)

            self.assertIsNotNone(browser)
            assert browser is not None
            self.assertEqual(browser.link_name, "browser-harness")
            self.assertEqual(browser.path, browser_dir.resolve())

    def test_install_claude_skills_repairs_incompatible_browser_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skills_dir = root / "home" / ".claude" / "skills"
            env_path = root / ".env"
            wrong_browser = root / "existing" / "wrong-browser"
            correct_browser = root / "bundle" / "browser-harness"
            wrong_browser.mkdir(parents=True)
            (wrong_browser / "SKILL.md").write_text("not a browser skill\n", encoding="utf-8")
            correct_browser.mkdir(parents=True)
            (correct_browser / "SKILL.md").write_text("browser-harness skill\n", encoding="utf-8")
            skills_dir.mkdir(parents=True)
            (skills_dir / "browser").symlink_to(wrong_browser, target_is_directory=True)

            onboarding._install_claude_skills(
                skills_dir=skills_dir,
                browser_harness_skill_dir=correct_browser,
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
            browser_dir.mkdir(parents=True)
            (browser_dir / "SKILL.md").write_text("browser-harness skill\n", encoding="utf-8")
            (skills_dir / "browser").mkdir(parents=True)

            with self.assertRaises(FileExistsError):
                onboarding._install_claude_skills(
                    skills_dir=skills_dir,
                    browser_harness_skill_dir=browser_dir,
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

    def test_install_browser_harness_installs_bundled_source_with_managed_python(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            uv = root / "bin" / "uv"
            python = root / "python" / "bin" / "python3.12"
            source = root / "bundle" / "harness" / "browser-harness"
            llm_resolver = root / "bundle" / "packages" / "llm-resolver"
            tool_dir = root / "tools"
            tool_bin_dir = root / "bin-tools"
            uv.parent.mkdir(parents=True)
            python.parent.mkdir(parents=True)
            source.mkdir(parents=True)
            llm_resolver.mkdir(parents=True)
            uv.write_text("#!/bin/sh\n", encoding="utf-8")
            python.write_text("#!/bin/sh\n", encoding="utf-8")
            (source / "pyproject.toml").write_text("[project]\nname='browser-harness'\n", encoding="utf-8")
            (llm_resolver / "pyproject.toml").write_text("[project]\nname='llm-resolver'\n", encoding="utf-8")
            calls: list[list[str]] = []
            envs: list[dict[str, str]] = []

            def fake_run(args, **kwargs):
                calls.append(args)
                envs.append(kwargs["env"])
                harness = tool_bin_dir / "browser-harness"
                harness.write_text("#!/bin/sh\n", encoding="utf-8")
                harness.chmod(0o755)
                return SimpleNamespace(returncode=0, stdout="installed browser-harness\n", stderr="")

            with patch.object(onboarding, "get_tool_dir", return_value=tool_dir), patch.object(
                onboarding, "get_tool_bin_dir", return_value=tool_bin_dir
            ), patch.object(onboarding, "get_managed_browser_harness_path", return_value=tool_bin_dir / "browser-harness"):
                ok, message = onboarding._install_browser_harness(
                    uv_path=uv,
                    python_path=python,
                    source_dir=source,
                    llm_resolver_dir=llm_resolver,
                    run=fake_run,
                )

            self.assertTrue(ok)
            self.assertIn("installed browser-harness", message)
            self.assertEqual(
                calls,
                [[
                    str(uv),
                    "tool",
                    "install",
                    "--force",
                    "--python",
                    str(python),
                    "--with-editable",
                    str(llm_resolver),
                    str(source),
                ]],
            )
            self.assertEqual(envs[0]["UV_TOOL_DIR"], str(tool_dir))
            self.assertEqual(envs[0]["UV_TOOL_BIN_DIR"], str(tool_bin_dir))

    def test_install_browser_harness_reports_missing_source(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            uv = root / "uv"
            python = root / "python"
            uv.write_text("#!/bin/sh\n", encoding="utf-8")
            python.write_text("#!/bin/sh\n", encoding="utf-8")

            ok, message = onboarding._install_browser_harness(
                uv_path=uv,
                python_path=python,
                source_dir=root / "missing-browser-harness",
            )

            self.assertFalse(ok)
            self.assertIn("browser-harness source not found", message)


if __name__ == "__main__":
    unittest.main()
