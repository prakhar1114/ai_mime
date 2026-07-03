"""5-step AppKit onboarding wizard (first-launch only).

Runs a blocking ``NSApplication.run()`` loop.  Call ``run_onboarding()``
before ``rumps.App.run()``; the NSApplication singleton is shared and
``stop_()`` / ``run()`` can be called multiple times.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

import objc
from Foundation import NSObject, NSTimer, NSMakeRect, NSNotificationCenter
from AppKit import (
    NSApplication,
    NSWindow,
    NSView,
    NSTextField,
    NSButton,
    NSColor,
    NSFont,
    NSScreen,
    NSImage,
    NSImageView,
    NSMenu,
    NSMenuItem,
    NSEvent,
    NSEventTypeApplicationDefined,
    NSWindowStyleMaskTitled,
    NSWindowStyleMaskClosable,
    NSBackingStoreBuffered,
    NSNormalWindowLevel,
    NSButtonTypeMomentaryPushIn,
    NSControlTextDidChangeNotification,
    NSEventModifierFlagCommand,
    NSProgressIndicator,
    NSProgressIndicatorBarStyle,
    NSProgressIndicatorSpinningStyle,
    NSImageSymbolConfiguration,
)

from ai_mime.app_data import (
    get_bundled_browser_harness_dir,
    get_bundled_llm_resolver_dir,
    get_env_path,
    get_managed_browser_harness_path,
    get_managed_python_install_dir,
    get_onboarding_done_path,
    get_bundled_resource,
    get_python_path,
    get_tool_bin_dir,
    get_tool_dir,
    get_uv_cache_dir,
    get_uv_path,
    is_frozen,
)

# ---------------------------------------------------------------------------
# Layout constants
# ---------------------------------------------------------------------------
_W = 560          # window width
_H = 480          # window height
_M = 40           # side margin
_CW = _W - 2 * _M  # content width
_STEPS = ("Permissions", "Provider", "Skills")
_FINISH_SPLASH_SECS = 1.2   # brief confirmation splash before hand-off (0 = none)
_CENTER = 1       # NSTextAlignmentCenter
_ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"
_BROWSER_SKILL_NAME_ENV = "AI_MIME_BROWSER_SKILL_NAME"
_BROWSER_SKILL_PATH_ENV = "AI_MIME_BROWSER_SKILL_PATH"
_PYTHON_VERSION = "3.12"
_CLAUDE_FALLBACK_DIRS = (
    ".local/bin",
    "bin",
    "/opt/homebrew/bin",
    "/usr/local/bin",
)


@dataclass(frozen=True)
class _ClaudeSkillResolution:
    link_name: str
    skill_name: str
    path: Path
    source: str

# ---------------------------------------------------------------------------
# Permission definitions
# ---------------------------------------------------------------------------
_PERMS = [
    {
        "key": "accessibility",
        "title": "Accessibility",
        "path": "Privacy & Security → Accessibility",
        "pane": "Privacy_Accessibility",
    },
    {
        "key": "screen_recording",
        "title": "Screen Recording",
        "path": "Privacy & Security → Screen & System Recording",
        "pane": "Privacy_ScreenCapture",
    },
]


def _merge_env_var(env_path: Path, key: str, value: str) -> None:
    """Set one dotenv-style key while preserving unrelated lines."""
    env_path.parent.mkdir(parents=True, exist_ok=True)
    value = value.strip()
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []

    prefix = f"{key}="
    out: list[str] = []
    replaced = False
    for line in lines:
        if not replaced and line.startswith(prefix):
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{key}={value}")

    env_path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


def _detect_local_claude(
    *,
    which=shutil.which,
    run=subprocess.run,
    timeout: float = 3.0,
    home: Path | None = None,
    is_file=os.path.isfile,
) -> tuple[bool, str]:
    """Return whether Claude Code is locally reachable and a display message."""
    exe = which("claude")
    if not exe:
        base_home = home or Path.home()
        for candidate_dir in _CLAUDE_FALLBACK_DIRS:
            candidate = Path(candidate_dir)
            if not candidate.is_absolute():
                candidate = base_home / candidate
            candidate = candidate / "claude"
            if is_file(candidate):
                exe = str(candidate)
                break
    if not exe:
        return False, "Claude Code not found on PATH."

    try:
        proc = run(
            [exe, "--version"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception as e:
        return False, f"Found claude, but version check failed: {e}"

    output = (proc.stdout or proc.stderr or "").strip().splitlines()
    detail = output[0] if output else str(exe)
    if proc.returncode == 0:
        return True, f"Local Claude Code detected: {detail}"
    return False, f"Found claude, but version check exited {proc.returncode}: {detail}"


def _claude_skills_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".claude" / "skills"


def _browser_harness_skill_dir() -> Path:
    return get_bundled_browser_harness_dir()


def _read_skill_name(skill_dir: Path) -> str | None:
    try:
        text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    except Exception:
        return None
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        stripped = line.strip()
        if stripped == "---":
            return None
        if stripped.startswith("name:"):
            value = stripped.split(":", 1)[1].strip()
            return value.strip("\"'")
    return None


def _skill_file_contains(skill_dir: Path, needle: str) -> bool:
    try:
        text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    except Exception:
        return False
    return needle in text


def _skill_is_compatible(skill_dir: Path, expected_name: str) -> bool:
    if expected_name == "browser":
        return _skill_file_contains(skill_dir, "browser-harness")
    return _read_skill_name(skill_dir) == expected_name


def _compatible_skill_at(link_path: Path, expected_name: str) -> _ClaudeSkillResolution | None:
    try:
        if not link_path.exists() or not link_path.is_dir():
            return None
        resolved = link_path.expanduser().resolve()
    except Exception:
        return None
    if not _skill_is_compatible(resolved, expected_name):
        return None
    return _ClaudeSkillResolution(
        link_name=link_path.name,
        skill_name=expected_name,
        path=resolved,
        source="accepted_existing",
    )


def _find_existing_compatible_skill(
    skills_root: Path,
    *,
    link_names: tuple[str, ...],
    expected_name: str,
    allow_incompatible_symlink: bool = False,
) -> _ClaudeSkillResolution | None:
    for link_name in link_names:
        link_path = skills_root / link_name
        existing = _compatible_skill_at(link_path, expected_name)
        if existing is not None:
            return existing
        if allow_incompatible_symlink and link_path.is_symlink():
            continue
        if link_path.exists() or link_path.is_symlink():
            raise FileExistsError(f"Existing Claude skill is incompatible: {link_path}")
    return None


def _ensure_symlink(
    link_path: Path,
    target_path: Path,
    *,
    expected_name: str,
    replace_incompatible_symlink: bool = False,
) -> None:
    target_path = target_path.expanduser().resolve()
    if not target_path.exists() or not target_path.is_dir():
        raise FileNotFoundError(f"Skill source not found: {target_path}")
    if not _skill_is_compatible(target_path, expected_name):
        raise FileNotFoundError(f"Skill source is not a compatible {expected_name} skill: {target_path}")

    if link_path.is_symlink():
        if link_path.resolve() == target_path:
            return
        if replace_incompatible_symlink:
            link_path.unlink()
            link_path.symlink_to(target_path, target_is_directory=True)
            return
        raise FileExistsError(f"Existing Claude skill is incompatible: {link_path}")
    elif link_path.exists():
        raise FileExistsError(f"Cannot replace existing non-symlink: {link_path}")

    link_path.symlink_to(target_path, target_is_directory=True)


def _persist_claude_skill_env(
    *,
    env_path: Path,
    browser: _ClaudeSkillResolution,
) -> None:
    _merge_env_var(env_path, _BROWSER_SKILL_NAME_ENV, browser.skill_name)
    _merge_env_var(env_path, _BROWSER_SKILL_PATH_ENV, str(browser.path))
    os.environ[_BROWSER_SKILL_NAME_ENV] = browser.skill_name
    os.environ[_BROWSER_SKILL_PATH_ENV] = str(browser.path)


def _detect_claude_skills(
    *,
    skills_dir: Path | None = None,
) -> _ClaudeSkillResolution | None:
    skills_root = skills_dir or _claude_skills_dir()
    return _find_existing_compatible_skill(
        skills_root,
        link_names=("browser", "browser-harness"),
        expected_name="browser",
    )


def _install_claude_skills(
    *,
    skills_dir: Path | None = None,
    browser_harness_skill_dir: Path | None = None,
    env_path: Path | None = None,
) -> dict[str, Path]:
    """Install AI Mime's Claude skills by linking bundled/repo sources."""
    skills_root = skills_dir or _claude_skills_dir()
    browser_source = browser_harness_skill_dir or _browser_harness_skill_dir()

    skills_root.mkdir(parents=True, exist_ok=True)

    _ensure_symlink(
        skills_root / "browser",
        browser_source,
        expected_name="browser",
        replace_incompatible_symlink=True,
    )
    browser = _ClaudeSkillResolution(
        link_name="browser",
        skill_name="browser",
        path=browser_source.expanduser().resolve(),
        source="installed_by_ai_mime",
    )

    _persist_claude_skill_env(
        env_path=env_path or get_env_path(),
        browser=browser,
    )

    return {
        "browser": skills_root / browser.link_name,
    }


def _install_managed_python(
    *,
    uv_path: Path | None = None,
    install_dir: Path | None = None,
    run=subprocess.run,
    timeout: float = 900.0,
) -> tuple[bool, str]:
    """Install the packaged app's managed Python with bundled uv."""
    uv = uv_path or get_uv_path()
    target = install_dir or get_managed_python_install_dir()
    if not uv.exists():
        return False, f"uv not found at {uv}"

    target.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(uv),
        "python",
        "install",
        _PYTHON_VERSION,
        "--install-dir",
        str(target),
    ]
    try:
        proc = run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except Exception as e:
        return False, f"Python {_PYTHON_VERSION} install failed: {e}"

    output = (proc.stdout or proc.stderr or "").strip()
    if proc.returncode == 0:
        detail = output.splitlines()[-1] if output else f"Python {_PYTHON_VERSION} is installed."
        return True, detail
    detail = output.splitlines()[-1] if output else f"uv exited {proc.returncode}"
    return False, f"Python {_PYTHON_VERSION} install failed: {detail}"


def _install_browser_harness(
    *,
    uv_path: Path | None = None,
    python_path: Path | None = None,
    source_dir: Path | None = None,
    llm_resolver_dir: Path | None = None,
    run=subprocess.run,
    timeout: float = 900.0,
) -> tuple[bool, str]:
    """Install the bundled browser-harness command as an app-owned uv tool."""
    uv = uv_path or get_uv_path()
    python = python_path or get_python_path()
    source = source_dir or _browser_harness_skill_dir()
    llm_resolver = llm_resolver_dir or get_bundled_llm_resolver_dir()
    if not uv.exists():
        return False, f"uv not found at {uv}"
    if not python.exists():
        return False, f"managed Python not found at {python}"
    if not (source / "pyproject.toml").is_file():
        return False, f"browser-harness source not found at {source}"
    if not (llm_resolver / "pyproject.toml").is_file():
        return False, f"llm-resolver source not found at {llm_resolver}"

    tool_dir = get_tool_dir()
    tool_bin_dir = get_tool_bin_dir()
    tool_dir.mkdir(parents=True, exist_ok=True)
    tool_bin_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(uv),
        "tool",
        "install",
        "--force",
        "--python",
        str(python),
        "--with-editable",
        str(llm_resolver),
        str(source),
    ]
    try:
        proc = run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={
                **os.environ,
                "UV_TOOL_DIR": str(tool_dir),
                "UV_TOOL_BIN_DIR": str(tool_bin_dir),
                # Install into the same app-owned cache the runtime uses and ignore
                # the user's uv config, matching workflow_runtime_env() isolation.
                "UV_CACHE_DIR": str(get_uv_cache_dir()),
                "UV_NO_CONFIG": "1",
            },
        )
    except Exception as e:
        return False, f"browser-harness install failed: {e}"

    output = (proc.stdout or proc.stderr or "").strip()
    if proc.returncode != 0:
        detail = output.splitlines()[-1] if output else f"uv exited {proc.returncode}"
        return False, f"browser-harness install failed: {detail}"

    harness = get_managed_browser_harness_path()
    if not harness.is_file() or not os.access(harness, os.X_OK):
        return False, f"browser-harness executable not found at {harness}"
    detail = output.splitlines()[-1] if output else "browser-harness is installed."
    return True, detail


class _OnboardingWizard(NSObject):
    """Window controller + view builder for the 5-step wizard."""

    # ------------------------------------------------------------------
    # Init
    # ------------------------------------------------------------------
    def init(self):
        self = objc.super(_OnboardingWizard, self).init()
        self._step = 0
        self._window = None
        self._content = None
        self._continue_btn = None
        self._back_btn = None
        self._provider_popup = None
        self._provider_choice = 0
        self._provider_cards = {}
        self._provider_status_rows = {}
        self._provider_instructions_label = None
        self._test_status_btn = None
        self._testing_provider = False
        self._provider_progress = None
        self._provider_progress_label = None
        self._installing = False
        self._install_progress = None
        self._install_progress_label = None
        self._starting = False
        self._start_progress = None
        self._start_status_label = None
        self._start_timer = None
        self._skills_autostart_timer = None
        self._skill_rows = {}
        self._skills_error_label = None
        self._perm_timer = None
        self._perm_rows = {}   # key → {"indicator": NSView, "check": NSTextField, "granted": bool}
        self._stopped = False
        return self

    # ------------------------------------------------------------------
    # Show / tear-down
    # ------------------------------------------------------------------
    def show(self):
        screen = NSScreen.mainScreen()
        fr = screen.frame()
        x = fr.origin.x + (fr.size.width - _W) / 2
        y = fr.origin.y + (fr.size.height - _H) / 2

        self._window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, _W, _H),
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
            NSBackingStoreBuffered,
            False,
        )
        self._window.setTitle_("AI Mime Setup")
        self._window.setDelegate_(self)
        self._window.setLevel_(NSNormalWindowLevel)

        self._content = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, _W, _H))
        self._window.setContentView_(self._content)

        self._render()
        self._window.makeKeyAndOrderFront_(self)

    def _stop_loop(self):
        if self._stopped:
            return
        self._stopped = True
        if self._perm_timer is not None:
            self._perm_timer.invalidate()
            self._perm_timer = None
        if self._start_timer is not None:
            self._start_timer.invalidate()
            self._start_timer = None
        if self._skills_autostart_timer is not None:
            self._skills_autostart_timer.invalidate()
            self._skills_autostart_timer = None

        app = NSApplication.sharedApplication()
        app.stop_(None)

        # Post a dummy event to break the event loop immediately.
        try:
            event = NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
                NSEventTypeApplicationDefined,
                (0, 0),
                0,
                0,
                0,
                None,
                0,
                0,
                0
            )
            app.postEvent_atStart_(event, True)
        except Exception:
            pass

    # ---------------------------------------------------------------
    # NSWindowDelegate
    # ---------------------------------------------------------------
    def windowWillClose_(self, notification):
        self._stop_loop()

    # ------------------------------------------------------------------
    # Rendering core
    # ------------------------------------------------------------------
    def _clear(self):
        for v in list(self._content.subviews() or []):
            v.removeFromSuperview()
        self._continue_btn = None
        self._back_btn = None
        self._provider_popup = None
        self._provider_progress = None
        self._provider_progress_label = None
        self._provider_status_rows = {}
        self._provider_cards = {}
        self._provider_instructions_label = None
        self._test_status_btn = None
        self._install_progress = None
        self._install_progress_label = None
        self._starting = False
        self._start_progress = None
        self._start_status_label = None
        self._skill_rows = {}
        self._skills_error_label = None
        self._perm_rows = {}

    def _render(self):
        self._clear()
        (
            self._render_permissions,
            self._render_provider_setup,
            self._render_skills_setup,
        )[self._step]()

    # ------------------------------------------------------------------
    # Step 1 – Permissions
    # ------------------------------------------------------------------

    def _render_permissions(self):
        self._rounded_icon(54, _H - 116)
        self._add_label("Enable permissions", x=0, y=_H - 152, w=_W, h=30,
                        size=22, bold=True, align=_CENTER)
        self._add_label(
            "AI Mime needs two macOS permissions to\nwatch and replay your workflows.",
            x=0, y=_H - 196, w=_W, h=36, size=13, align=_CENTER,
            color=NSColor.secondaryLabelColor())

        card_h = 124
        card_y = 150
        self._card(_M, card_y, _CW, card_h)
        row_h = card_h / 2
        visuals = {
            "accessibility": (NSColor.systemBlueColor(), "accessibility",
                              "Control the screen to replay your actions"),
            "screen_recording": (NSColor.systemRedColor(), "video.fill",
                                 "See the screen while recording a task"),
        }
        self._hairline(_M + 62, card_y + row_h, _CW - 62 - 16)
        order = list(_PERMS)
        self._add_perm_row(order[0], visuals[order[0]["key"]], card_y + row_h, row_h)
        self._add_perm_row(order[1], visuals[order[1]["key"]], card_y, row_h)

        self._primary_button("Continue", "onContinue:", enabled=False)

        self._perm_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.5, self, "pollPerms:", None, True)


    def _add_perm_row(self, perm, visual, y, h):
        key = perm["key"]; pane = perm["pane"]; title = perm["title"]
        color, symbol, subtitle = visual
        cy = y + h / 2
        self._tile(_M + 16, cy - 15, color, symbol, size=30)
        tx = _M + 16 + 30 + 13
        self._add_label(title, x=tx, y=cy + 1, w=250, h=18, size=14, bold=True)
        self._add_label(subtitle, x=tx, y=cy - 17, w=300, h=15, size=12,
                        color=NSColor.secondaryLabelColor())

        right = _M + _CW - 16
        open_btn = NSButton.alloc().initWithFrame_(NSMakeRect(right - 66, cy - 13, 66, 26))
        open_btn.setTitle_("Open")
        open_btn.setButtonType_(NSButtonTypeMomentaryPushIn)
        open_btn.setBezelStyle_(1)
        open_btn.setFont_(NSFont.systemFontOfSize_(12))
        open_btn.setTarget_(self)
        open_btn.setAction_("openSpecificSettings:")
        open_btn.setTag_(hash(pane))
        self._content.addSubview_(open_btn)

        enabled_lbl = self._add_label("Enabled", x=right - 84, y=cy - 8, w=84, h=16,
                                      size=12.5, bold=True, align=2,
                                      color=NSColor.systemGreenColor())
        enabled_lbl.setHidden_(True)

        status_iv = self._symbol_view("exclamationmark.circle.fill",
                                      NSColor.systemOrangeColor(),
                                      right - 66 - 14 - 9, cy, pt=17)

        self._perm_rows[key] = {"granted": False, "pane": pane,
                                "status_iv": status_iv, "open_btn": open_btn,
                                "enabled_lbl": enabled_lbl}

    # Cocoa selector  openSettings:
    def openSettings_(self, sender):
        """Open the main Privacy & Security settings."""
        import subprocess
        subprocess.Popen(["open", "x-apple.systempreferences:com.apple.preference.security"])

    # Cocoa selector  openSpecificSettings:
    def openSpecificSettings_(self, sender):
        """Open a specific privacy settings pane based on the button's tag."""
        import subprocess
        # Find which pane to open based on tag
        tag = sender.tag()
        for row in self._perm_rows.values():
            if hash(row["pane"]) == tag:
                pane = row["pane"]
                # Try modern macOS URL first (macOS 13+)
                url = f"x-apple.systempreferences:com.apple.preference.security?{pane}"
                subprocess.Popen(["open", url])
                break

    # Cocoa selector  pollPerms:
    def pollPerms_(self, timer):
        try:
            import ApplicationServices
            opts = {ApplicationServices.kAXTrustedCheckOptionPrompt: False}
            acc_ok = bool(ApplicationServices.AXIsProcessTrustedWithOptions(opts))
        except Exception:
            acc_ok = False

        try:
            import mss
            with mss.mss() as sct:
                sct.grab({"top": 0, "left": 0, "width": 1, "height": 1})
            rec_ok = True
        except Exception:
            rec_ok = False

        self._update_perm("accessibility", acc_ok)
        self._update_perm("screen_recording", rec_ok)

        # Enable Continue only when every permission is granted
        if self._continue_btn is not None:
            self._continue_btn.setEnabled_(all(r["granted"] for r in self._perm_rows.values()))


    def _update_perm(self, key, granted):
        row = self._perm_rows.get(key)
        if row is None or row["granted"] == granted:
            return
        row["granted"] = granted
        if granted:
            img = self._sf("checkmark.circle.fill", 17)
            if img is not None:
                row["status_iv"].setImage_(img)
            try:
                row["status_iv"].setContentTintColor_(NSColor.systemGreenColor())
            except Exception:
                pass
            row["open_btn"].setHidden_(True)
            row["enabled_lbl"].setHidden_(False)
        else:
            img = self._sf("exclamationmark.circle.fill", 17)
            if img is not None:
                row["status_iv"].setImage_(img)
            try:
                row["status_iv"].setContentTintColor_(NSColor.systemOrangeColor())
            except Exception:
                pass
            row["open_btn"].setHidden_(False)
            row["enabled_lbl"].setHidden_(True)

    # ------------------------------------------------------------------
    # Step 2 – Claude Setup
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Step 2 – AI Provider Setup
    # ------------------------------------------------------------------

    def _render_provider_setup(self):
        if self._perm_timer is not None:
            self._perm_timer.invalidate()
            self._perm_timer = None

        self._add_label("Choose your AI provider", x=0, y=_H - 100, w=_W, h=30,
                        size=22, bold=True, align=_CENTER)
        self._add_label(
            "AI Mime runs on a coding agent CLI. We’ll\ninstall and verify it for you.",
            x=0, y=_H - 144, w=_W, h=36, size=13, align=_CENTER,
            color=NSColor.secondaryLabelColor())

        from ai_mime.provider_settings import _read_provider
        self._provider_choice = 1 if _read_provider() == "openai" else 0
        gap = 14
        cw = (_CW - gap) / 2
        ch = 62
        cy = _H - 234
        self._add_provider_card(0, "Claude Code", "Anthropic", _M, cy, cw, ch)
        self._add_provider_card(1, "Codex", "OpenAI", _M + cw + gap, cy, cw, ch)
        self._style_provider_cards()

        try:
            gray = NSColor.systemGrayColor()
        except Exception:
            gray = NSColor.grayColor()
        scard_h = 88
        scard_y = 150
        self._card(_M, scard_y, _CW, scard_h)
        self._hairline(_M + 62, scard_y + scard_h / 2, _CW - 62 - 16)
        self._add_provider_status_row("binary", gray, "terminal.fill",
                                      "CLI binary", "Command-line tool",
                                      scard_y + scard_h / 2, scard_h / 2)
        self._add_provider_status_row("login", NSColor.systemGreenColor(), "key.fill",
                                      "Authentication", "Provider login status",
                                      scard_y, scard_h / 2)
        # Store the subtitle label for dynamic updates
        self._provider_login_subtitle = self._provider_status_rows["login"].get("subtitle")

        self._provider_instructions_label = self._add_label(
            "", x=_M, y=118, w=_CW, h=28, size=12, align=_CENTER,
            color=NSColor.secondaryLabelColor())

        progress_w = 260
        self._provider_progress = NSProgressIndicator.alloc().initWithFrame_(
            NSMakeRect((_W - progress_w) / 2, 100, progress_w, 12))
        self._provider_progress.setStyle_(NSProgressIndicatorBarStyle)
        self._provider_progress.setIndeterminate_(False)
        self._provider_progress.setMinValue_(0.0)
        self._provider_progress.setMaxValue_(100.0)
        self._provider_progress.setDoubleValue_(0.0)
        self._provider_progress.setHidden_(True)
        self._content.addSubview_(self._provider_progress)

        self._provider_progress_label = self._add_label(
            "", x=_M, y=82, w=_CW, h=16, size=11, align=_CENTER,
            color=NSColor.secondaryLabelColor())

        self._back_button()
        self._test_status_btn = self._secondary_button("Test", "testProvider:", 300, 29, 92, 30)
        self._primary_button("Continue", "smartAction:", enabled=False)

        self._update_provider_status_ui()


    def _add_provider_status_row(self, key, tile_color, symbol, title, subtitle, y, h):
        cy = y + h / 2
        self._tile(_M + 16, cy - 14, tile_color, symbol, size=28)
        tx = _M + 16 + 28 + 12
        self._add_label(title, x=tx, y=cy + 1, w=240, h=18, size=13.5, bold=True)
        sub_lbl = self._add_label(subtitle, x=tx, y=cy - 16, w=260, h=14, size=11.5,
                        color=NSColor.secondaryLabelColor())
        lbl = self._add_label("Checking…", x=_W - _M - 176, y=cy - 8, w=160, h=16,
                              size=13, bold=True, align=2,
                              color=NSColor.secondaryLabelColor())
        self._provider_status_rows[key] = {"status": lbl, "subtitle": sub_lbl, "is_ok": False}


    def _set_status_row(self, key, ok, text):
        row = self._provider_status_rows.get(key)
        if row is None:
            return
        row["is_ok"] = ok
        lbl = row["status"]
        lbl.setStringValue_(text)
        if ok:
            lbl.setTextColor_(NSColor.systemGreenColor())
        elif text in ("Checking…", "Not Verified"):
            lbl.setTextColor_(NSColor.secondaryLabelColor())
        else:
            lbl.setTextColor_(NSColor.systemRedColor())

    def _update_provider_status_ui(self):
        idx = self._selected_provider_index()
        provider = "openai" if idx == 1 else "anthropic"
        label_prefix = "Claude Code" if provider == "anthropic" else "Codex"

        from ai_mime.provider_settings import is_provider_installed, _provider_runtime_status

        installed = is_provider_installed(provider)
        if installed:
            self._set_status_row("binary", True, "Installed")
            logged_in, msg = _provider_runtime_status(provider)
            if logged_in:
                self._set_status_row("login", True, "Installed")
                login_row = self._provider_status_rows.get("login")
                if login_row and login_row.get("subtitle"):
                    login_row["subtitle"].setStringValue_(f"Signed in to {label_prefix}")
                if self._provider_instructions_label is not None:
                    self._provider_instructions_label.setStringValue_(f"{label_prefix} found and logged in.")
                    self._provider_instructions_label.setTextColor_(NSColor.systemGreenColor())
                if self._continue_btn is not None:
                    self._continue_btn.setTitle_("Continue")
                    self._continue_btn.setEnabled_(True)
            else:
                self._set_status_row("login", False, "Not Logged In")
                login_row = self._provider_status_rows.get("login")
                if login_row and login_row.get("subtitle"):
                    login_row["subtitle"].setStringValue_("Provider login status")
                if self._provider_instructions_label is not None:
                    self._provider_instructions_label.setTextColor_(NSColor.secondaryLabelColor())
                    self._provider_instructions_label.setStringValue_(
                        f"{label_prefix} found. Please complete login in Terminal, then click 'Test' to verify."
                    )
                if self._continue_btn is not None:
                    self._continue_btn.setTitle_("Login")
                    self._continue_btn.setEnabled_(True)
        else:
            self._set_status_row("binary", False, "Not Installed")
            self._set_status_row("login", False, "Not Verified")
            if self._provider_instructions_label is not None:
                self._provider_instructions_label.setTextColor_(NSColor.secondaryLabelColor())
                self._provider_instructions_label.setStringValue_(
                    f"CLI tool is not installed. Click 'Install' to automatically install and login."
                )
            if self._continue_btn is not None:
                self._continue_btn.setTitle_("Install")
                self._continue_btn.setEnabled_(True)

    def _launch_terminal_login(self, provider):
        import subprocess
        cmd = "claude auth login" if provider == "anthropic" else "codex login"
        applescript = f'''
        tell application "Terminal"
            activate
            do script "{cmd}"
        end tell
        '''
        try:
            subprocess.run(["osascript", "-e", applescript])
        except Exception:
            pass

    # Cocoa selector providerChanged:
    def providerChanged_(self, sender):
        if self._continue_btn is not None:
            self._continue_btn.setEnabled_(False)
        self._update_provider_status_ui()

    def _selected_provider_index(self):
        """Index of the selected provider card (0 = Anthropic, 1 = OpenAI)."""
        return getattr(self, "_provider_choice", 0)


    def _add_provider_card(self, index, title, company, x, y, w, h):
        """A selectable, icon-less provider tile: product title + company."""
        from AppKit import NSBox
        card = NSBox.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
        card.setBoxType_(4)           # NSBoxCustom
        card.setTitlePosition_(0)     # NSNoTitle
        card.setCornerRadius_(13)
        card.setBorderWidth_(1.5)
        card.setBorderColor_(NSColor.separatorColor())
        card.setFillColor_(NSColor.controlBackgroundColor())
        self._content.addSubview_(card)

        self._add_label(title, x=x + 18, y=y + h / 2 - 2, w=w - 44, h=20, size=16, bold=True)
        self._add_label(company, x=x + 18, y=y + h / 2 - 22, w=w - 44, h=16, size=13,
                        color=NSColor.secondaryLabelColor())

        check = self._symbol_view("checkmark.circle.fill", self._accent(),
                                  x + w - 20, y + h - 20, pt=17)
        check.setHidden_(True)

        hit = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
        hit.setTitle_("")
        hit.setButtonType_(NSButtonTypeMomentaryPushIn)
        try:
            hit.setTransparent_(True)
        except Exception:
            hit.setBordered_(False)
        hit.setTarget_(self)
        hit.setAction_("providerCardClicked:")
        hit.setTag_(index)
        self._content.addSubview_(hit)

        self._provider_cards[index] = {"card": card, "check": check}


    def _style_provider_cards(self):
        """Reflect the current selection across both cards."""
        accent = self._accent()
        for i, row in self._provider_cards.items():
            selected = (i == self._provider_choice)
            card = row["card"]
            if selected:
                card.setBorderColor_(accent)
                card.setBorderWidth_(2.0)
                try:
                    card.setFillColor_(accent.colorWithAlphaComponent_(0.08))
                except Exception:
                    pass
            else:
                card.setBorderColor_(NSColor.separatorColor())
                card.setBorderWidth_(1.5)
                card.setFillColor_(NSColor.controlBackgroundColor())
            if row["check"] is not None:
                row["check"].setHidden_(not selected)

    # Cocoa selector  providerCardClicked:
    def providerCardClicked_(self, sender):
        self._provider_choice = int(sender.tag())
        self._style_provider_cards()
        self.providerChanged_(sender)

    # Cocoa selector testProvider:
    def testProvider_(self, sender):
        # Test button only tests status and updates UI
        self._update_provider_status_ui()

    # Cocoa selector smartAction:
    def smartAction_(self, sender):
        if self._testing_provider:
            return

        idx = self._selected_provider_index()
        provider = "openai" if idx == 1 else "anthropic"

        from ai_mime.provider_settings import is_provider_installed, _provider_runtime_status

        installed = is_provider_installed(provider)
        if not installed:
            self._testing_provider = True
            if self._test_status_btn is not None:
                self._test_status_btn.setEnabled_(False)
            if self._continue_btn is not None:
                self._continue_btn.setEnabled_(False)
                self._continue_btn.setTitle_("Installing...")
            if self._provider_progress is not None:
                self._provider_progress.setHidden_(False)
                self._provider_progress.setDoubleValue_(10.0)
            if self._provider_progress_label is not None:
                self._provider_progress_label.setStringValue_("Starting installation...")

            thread = threading.Thread(target=self._install_provider_worker, daemon=True)
            thread.start()
        else:
            logged_in, msg = _provider_runtime_status(provider)
            if not logged_in:
                self._launch_terminal_login(provider)
                self._update_provider_status_ui()
            else:
                self.onContinue_(sender)

    def _install_provider_worker(self):
        idx = self._selected_provider_index()
        provider = "openai" if idx == 1 else "anthropic"

        def bump_progress():
            import time
            val = 10.0
            while getattr(self, "_testing_provider", False) and val <= 75.0:
                self.performSelectorOnMainThread_withObject_waitUntilDone_(
                    "setProviderProgressFromWorker:",
                    ("Downloading and running installer...", val),
                    False,
                )
                time.sleep(1)
                val += 0.5

        threading.Thread(target=bump_progress, daemon=True).start()

        from ai_mime.provider_settings import install_provider_cli
        ok, msg = install_provider_cli(provider)

        payload = (ok, msg)
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "finishProviderInstallFromWorker:",
            payload,
            False,
        )

    # Cocoa selector setProviderProgressFromWorker:
    def setProviderProgressFromWorker_(self, payload):
        label, value = payload
        if self._provider_progress is not None:
            self._provider_progress.setDoubleValue_(float(value))
        if self._provider_progress_label is not None:
            self._provider_progress_label.setStringValue_(label)

    # Cocoa selector finishProviderInstallFromWorker:
    def finishProviderInstallFromWorker_(self, payload):
        ok, msg = payload
        self._testing_provider = False
        if self._test_status_btn is not None:
            self._test_status_btn.setEnabled_(True)
        if self._continue_btn is not None:
            self._continue_btn.setEnabled_(True)
        if self._provider_progress is not None:
            self._provider_progress.setHidden_(True)
        if self._provider_progress_label is not None:
            self._provider_progress_label.setStringValue_("")

        if not ok:
            if self._provider_instructions_label is not None:
                self._provider_instructions_label.setStringValue_(f"Installation failed: {msg}")
                self._provider_instructions_label.setTextColor_(NSColor.systemRedColor())
            self._update_provider_status_ui()
        else:
            idx = self._selected_provider_index()
            provider = "openai" if idx == 1 else "anthropic"
            from ai_mime.provider_settings import _provider_runtime_status
            logged_in, status_msg = _provider_runtime_status(provider)
            if not logged_in:
                self._launch_terminal_login(provider)
            self._update_provider_status_ui()

    # ------------------------------------------------------------------
    # Step 3 – Skills Setup
    # ------------------------------------------------------------------

    def _render_skills_setup(self):
        self._add_label("Installing skills", x=0, y=_H - 100, w=_W, h=30,
                        size=22, bold=True, align=_CENTER)
        self._add_label(
            "Linking AI Mime’s automation skills into your\nagent. This only happens once.",
            x=0, y=_H - 144, w=_W, h=36, size=13, align=_CENTER,
            color=NSColor.secondaryLabelColor())

        try:
            indigo = NSColor.systemIndigoColor()
        except Exception:
            indigo = NSColor.systemPurpleColor()
        rows = [("browser-harness", indigo, "globe", "Browser harness", "Web automation skill for Claude")]
        if is_frozen():
            rows.append(("python-3.12", NSColor.systemOrangeColor(),
                         "chevron.left.forwardslash.chevron.right",
                         "Managed Python 3.12", "Runtime for workflow scripts"))
        n = len(rows)
        row_h = 58
        card_h = n * row_h
        card_y = 296 - card_h
        self._card(_M, card_y, _CW, card_h)
        for i, (name, color, symbol, title, subtitle) in enumerate(rows):
            ry = card_y + (n - 1 - i) * row_h
            if i > 0:
                self._hairline(_M + 62, ry + row_h, _CW - 62 - 16)
            self._add_skill_row(name, color, symbol, title, subtitle, ry, row_h)

        progress_w = 280
        self._install_progress = NSProgressIndicator.alloc().initWithFrame_(
            NSMakeRect((_W - progress_w) / 2, 150, progress_w, 12))
        self._install_progress.setStyle_(NSProgressIndicatorBarStyle)
        self._install_progress.setIndeterminate_(False)
        self._install_progress.setMinValue_(0.0)
        self._install_progress.setMaxValue_(100.0)
        self._install_progress.setDoubleValue_(0.0)
        self._install_progress.setHidden_(True)
        self._content.addSubview_(self._install_progress)

        self._install_progress_label = self._add_label(
            "", x=_M, y=130, w=_CW, h=16, size=11, align=_CENTER,
            color=NSColor.secondaryLabelColor())
        self._skills_error_label = self._add_label(
            "", x=_M, y=96, w=_CW, h=30, size=12, align=_CENTER,
            color=NSColor.systemRedColor())

        self._back_button()
        self._primary_button("Retry", "installSkills:", enabled=False)
        self._continue_btn.setHidden_(True)
        self._refresh_skill_status()

        self._skills_autostart_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.3, self, "autostartSkills:", None, False)


    def _add_skill_row(self, name, color, symbol, title, subtitle, y, h):
        cy = y + h / 2
        self._tile(_M + 16, cy - 14, color, symbol, size=28)
        tx = _M + 16 + 28 + 12
        self._add_label(title, x=tx, y=cy + 1, w=260, h=18, size=13.5, bold=True)
        self._add_label(subtitle, x=tx, y=cy - 16, w=280, h=14, size=11.5,
                        color=NSColor.secondaryLabelColor())
        lbl = self._add_label("Pending", x=_W - _M - 156, y=cy - 8, w=140, h=16,
                              size=13, bold=True, align=2,
                              color=NSColor.secondaryLabelColor())
        self._skill_rows[name] = {"status": lbl}


    def _set_skill_status(self, name, installed, status_text):
        row = self._skill_rows.get(name)
        if row is None:
            return
        row["status"].setStringValue_(status_text)
        row["status"].setTextColor_(
            NSColor.systemGreenColor() if installed else NSColor.secondaryLabelColor())

    def _refresh_skill_status(self):
        skills_root = _claude_skills_dir()
        try:
            browser_skill = _detect_claude_skills(skills_dir=skills_root)
        except Exception as e:
            browser_skill = None
            if self._skills_error_label is not None:
                self._skills_error_label.setStringValue_(str(e))
        browser_ok = browser_skill is not None
        python_ok = (not is_frozen()) or get_python_path().exists()
        self._set_skill_status("browser-harness", browser_ok, "Installed" if browser_ok else "Not installed")
        if is_frozen():
            self._set_skill_status("python-3.12", python_ok, "Installed" if python_ok else "Not installed")
        if browser_skill is not None:
            _persist_claude_skill_env(
                env_path=get_env_path(),
                browser=browser_skill,
            )

    # Cocoa selector  autostartSkills:
    def autostartSkills_(self, timer):
        """Auto-run the skills/harness install (or skip if already present)."""
        self._skills_autostart_timer = None
        if not self._skill_rows or self._installing:
            return  # navigated away, or an install is already running
        skills_root = _claude_skills_dir()
        try:
            browser_ok = _detect_claude_skills(skills_dir=skills_root) is not None
        except Exception:
            browser_ok = False
        python_ok = (not is_frozen()) or get_python_path().exists()
        if browser_ok and python_ok:
            self.onContinue_(None)   # nothing to install — advance straight through
        else:
            self.installSkills_(None)

    # Cocoa selector  installSkills:
    def installSkills_(self, sender):
        if self._installing:
            return
        self._installing = True
        if self._skills_error_label is not None:
            self._skills_error_label.setStringValue_("")
        self._set_install_progress("Preparing install...", 5)
        if self._continue_btn is not None:
            self._continue_btn.setEnabled_(False)
            self._continue_btn.setTitle_("Installing...")
        if self._back_btn is not None:
            self._back_btn.setEnabled_(False)

        thread = threading.Thread(target=self._install_skills_worker, daemon=True)
        thread.start()

    def _install_skills_worker(self):
        error = None
        try:
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                "setInstallProgressFromWorker:",
                ("Linking Claude skills...", 20),
                False,
            )
            _install_claude_skills()
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                "setInstallProgressFromWorker:",
                ("Claude skills linked.", 55 if is_frozen() else 90),
                False,
            )
            if is_frozen():
                self.performSelectorOnMainThread_withObject_waitUntilDone_(
                    "setInstallProgressFromWorker:",
                    (f"Installing Python {_PYTHON_VERSION}...", 70),
                    False,
                )
                ok, msg = _install_managed_python()
                if not ok:
                    raise RuntimeError(msg)
                self.performSelectorOnMainThread_withObject_waitUntilDone_(
                    "setInstallProgressFromWorker:",
                    (msg, 82),
                    False,
                )
                self.performSelectorOnMainThread_withObject_waitUntilDone_(
                    "setInstallProgressFromWorker:",
                    ("Installing browser-harness...", 88),
                    False,
                )
                ok, msg = _install_browser_harness()
                if not ok:
                    raise RuntimeError(msg)
                self.performSelectorOnMainThread_withObject_waitUntilDone_(
                    "setInstallProgressFromWorker:",
                    (msg, 92),
                    False,
                )
        except Exception as e:
            error = str(e)

        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "finishInstallFromWorker:",
            error,
            False,
        )

    # Cocoa selector  setInstallProgressFromWorker:
    def setInstallProgressFromWorker_(self, payload):
        label, value = payload
        self._set_install_progress(label, value)

    def _set_install_progress(self, label, value):
        if self._install_progress is not None:
            self._install_progress.setHidden_(False)
            self._install_progress.setDoubleValue_(float(value))
        if self._install_progress_label is not None:
            self._install_progress_label.setStringValue_(label)

    # Cocoa selector  finishInstallFromWorker:
    def finishInstallFromWorker_(self, error):
        if error:
            if self._skills_error_label is not None:
                self._skills_error_label.setStringValue_(error)
            self._set_install_progress("Install failed.", 100)
        else:
            self._set_install_progress("Install complete.", 100)
        self._installing = False
        if self._install_progress is not None:
            self._install_progress.setHidden_(True)
        if self._install_progress_label is not None:
            self._install_progress_label.setStringValue_("")
        if self._back_btn is not None:
            self._back_btn.setEnabled_(True)
        self._refresh_skill_status()

        if error:
            # Surface a Retry button so the user isn't stuck on a failed install.
            if self._continue_btn is not None:
                self._continue_btn.setEnabled_(True)
                self._continue_btn.setTitle_("Retry")
                self._continue_btn.setHidden_(False)
        else:
            # Success — advance automatically (no click needed).
            self.onContinue_(None)

    # ------------------------------------------------------------------
    # (The interactive "Done" page was removed - onboarding now auto-finishes
    #  after the Skills step via _finish_onboarding below.)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Shared widget helpers
    # ------------------------------------------------------------------
    def _add_label(self, text, *, x, y, w, h, size, bold=False, align=0, color=None):
        """Add a text label to self._content."""
        lbl = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
        lbl.setStringValue_(text)
        lbl.setBezeled_(False)
        lbl.setDrawsBackground_(False)
        lbl.setEditable_(False)
        lbl.setSelectable_(False)
        lbl.setFont_(NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size))
        if align:
            lbl.setAlignment_(align)
        if color is not None:
            lbl.setTextColor_(color)
        self._content.addSubview_(lbl)
        return lbl

    # ------------------------------------------------------------------
    # Premium macOS design helpers
    # ------------------------------------------------------------------
    @objc.python_method
    def _accent(self):
        try:
            return NSColor.controlAccentColor()
        except Exception:
            return NSColor.systemBlueColor()

    @objc.python_method
    def _sf(self, name, pt=15):
        """Return an SF Symbol NSImage at a point size, or None."""
        try:
            img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
        except Exception:
            return None
        if img is None:
            return None
        try:
            cfg = NSImageSymbolConfiguration.configurationWithPointSize_weight_scale_(pt, 0.0, 2)
            img2 = img.imageWithSymbolConfiguration_(cfg)
            if img2 is not None:
                img = img2
        except Exception:
            pass
        return img

    @objc.python_method
    def _symbol_view(self, name, tint, cx, cy, pt=15):
        box = pt + 10
        iv = NSImageView.alloc().initWithFrame_(NSMakeRect(cx - box / 2, cy - box / 2, box, box))
        img = self._sf(name, pt)
        if img is not None:
            iv.setImage_(img)
            try:
                iv.setImageScaling_(3)  # proportionally up/down
            except Exception:
                pass
        try:
            iv.setContentTintColor_(tint)
        except Exception:
            pass
        self._content.addSubview_(iv)
        return iv

    @objc.python_method
    def _rounded_icon(self, size, y):
        logo_path = str(get_bundled_resource("AppIcon.appiconset/icon_256_1x.png"))
        img = NSImage.alloc().initWithContentsOfFile_(logo_path)
        iv = NSImageView.alloc().initWithFrame_(NSMakeRect((_W - size) / 2, y, size, size))
        if img is not None:
            iv.setImage_(img)
        iv.setWantsLayer_(True)
        iv.layer().setCornerRadius_(size * 0.2237)
        iv.layer().setMasksToBounds_(True)
        self._content.addSubview_(iv)
        return iv

    @objc.python_method
    def _card(self, x, y, w, h):
        from AppKit import NSBox
        v = NSBox.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
        v.setBoxType_(4)           # NSBoxCustom
        v.setTitlePosition_(0)     # NSNoTitle
        v.setCornerRadius_(12)
        v.setFillColor_(NSColor.controlBackgroundColor())
        v.setBorderWidth_(0.5)
        v.setBorderColor_(NSColor.separatorColor())
        self._content.addSubview_(v)
        return v

    @objc.python_method
    def _cg(self, color):
        """Resolve an NSColor to a CGColorRef safe for CALayer use.

        macOS dynamic / catalog colours (systemBlueColor, etc.) cannot be
        converted to CGColor directly — they need to go through a concrete
        colour space first.
        """
        # Strategy 1: convert to calibrated RGB then get CGColor
        try:
            rgb = color.colorUsingColorSpaceName_("NSCalibratedRGBColorSpace")
            if rgb is not None:
                return rgb.CGColor
        except Exception:
            pass
        # Strategy 2: direct .CGColor (works for some colour types)
        try:
            cg = color.CGColor
            if cg is not None:
                return cg
        except Exception:
            pass
        # Strategy 3: extract components and build via Quartz
        try:
            rgb = color.colorUsingColorSpaceName_("NSCalibratedRGBColorSpace")
            if rgb is None:
                rgb = color
            from Quartz import CGColorCreateGenericRGB
            return CGColorCreateGenericRGB(
                rgb.redComponent(), rgb.greenComponent(),
                rgb.blueComponent(), rgb.alphaComponent())
        except Exception:
            pass
        return None

    @objc.python_method
    def _tile(self, x, y, color, symbol, size=30):
        from AppKit import NSBox
        t = NSBox.alloc().initWithFrame_(NSMakeRect(x, y, size, size))
        t.setBoxType_(4)           # NSBoxCustom
        t.setTitlePosition_(0)     # NSNoTitle
        t.setBorderWidth_(0)
        t.setCornerRadius_(7)
        t.setFillColor_(color)
        self._content.addSubview_(t)
        self._symbol_view(symbol, NSColor.whiteColor(), x + size / 2, y + size / 2, pt=15)
        return t

    @objc.python_method
    def _hairline(self, x, y, w):
        from AppKit import NSBox
        v = NSBox.alloc().initWithFrame_(NSMakeRect(x, y, w, 1))
        v.setBoxType_(4)           # NSBoxCustom
        v.setTitlePosition_(0)     # NSNoTitle
        v.setBorderWidth_(0)
        v.setFillColor_(NSColor.separatorColor())
        self._content.addSubview_(v)

    @objc.python_method
    def _primary_button(self, title, action, enabled=True, w=116, h=32, x=None, y=28):
        if x is None:
            x = _W - _M - w
        b = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
        b.setTitle_(title)
        b.setButtonType_(NSButtonTypeMomentaryPushIn)
        b.setBezelStyle_(1)
        try:
            b.setControlSize_(3)  # large
        except Exception:
            pass
        b.setKeyEquivalent_("\r")  # blue default button + Return
        b.setTarget_(self)
        b.setAction_(action)
        b.setEnabled_(enabled)
        self._content.addSubview_(b)
        self._continue_btn = b
        return b

    @objc.python_method
    def _secondary_button(self, title, action, x, y, w=96, h=30, font=13):
        b = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
        b.setTitle_(title)
        b.setButtonType_(NSButtonTypeMomentaryPushIn)
        b.setBezelStyle_(1)
        b.setFont_(NSFont.systemFontOfSize_(font))
        b.setTarget_(self)
        b.setAction_(action)
        self._content.addSubview_(b)
        return b

    @objc.python_method
    def _back_button(self):
        b = NSButton.alloc().initWithFrame_(NSMakeRect(_M, 30, 72, 30))
        b.setTitle_("Back")
        b.setButtonType_(NSButtonTypeMomentaryPushIn)
        b.setBezelStyle_(1)
        b.setFont_(NSFont.systemFontOfSize_(13))
        b.setTarget_(self)
        b.setAction_("onBack:")
        self._content.addSubview_(b)
        self._back_btn = b
        return b

    # Cocoa selector  onBack:
    def onBack_(self, sender):
        if self._step > 0:
            self._step -= 1
            self._render()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    # Cocoa selector  onContinue:
    def onContinue_(self, sender):
        if self._step == len(_STEPS) - 1:
            self._finish_onboarding()
            return

        # --- Persist provider settings before advancing past the Provider step ---
        if self._step == 1:
            idx = self._selected_provider_index()
            provider = "openai" if idx == 1 else "anthropic"

            from ai_mime.provider_settings import save_provider_settings
            try:
                save_provider_settings(provider, api_key=None)
            except Exception as e:
                if self._provider_instructions_label is not None:
                    self._provider_instructions_label.setStringValue_(f"Error: {e}")
                    self._provider_instructions_label.setTextColor_(NSColor.systemRedColor())
                return

        self._step += 1

        if self._step >= len(_STEPS):
            # All steps done — write sentinel and exit run loop.
            get_onboarding_done_path().touch()
            if self._window is not None:
                self._window.close()  # triggers windowWillClose_ → _stop_loop
            else:
                self._stop_loop()
            return

        self._render()


    def _finish_onboarding(self):
        """Brief, non-interactive confirmation, then write the sentinel and hand
        off to the menu-bar app (replaces the old interactive Done page)."""
        if self._starting:
            return
        self._starting = True

        if _FINISH_SPLASH_SECS <= 0:
            self.finishOnboardingStart_(None)
            return

        self._clear()
        self._rounded_icon(84, _H - 176)
        self._add_label("You’re all set", x=0, y=_H - 232, w=_W, h=34,
                        size=25, bold=True, align=_CENTER)
        self._add_label("AI Mime is now running in your menu bar.",
                        x=_M, y=_H - 272, w=_CW, h=22, size=14, align=_CENTER,
                        color=NSColor.secondaryLabelColor())

        spinner = NSProgressIndicator.alloc().initWithFrame_(
            NSMakeRect((_W - 20) / 2, _H - 330, 20, 20))
        spinner.setStyle_(NSProgressIndicatorSpinningStyle)
        spinner.setIndeterminate_(True)
        spinner.setDisplayedWhenStopped_(False)
        self._content.addSubview_(spinner)
        spinner.startAnimation_(self)

        self._add_label("Starting AI Mime…", x=_M, y=_H - 366, w=_CW, h=16,
                        size=12, align=_CENTER, color=NSColor.secondaryLabelColor())

        self._start_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            _FINISH_SPLASH_SECS, self, "finishOnboardingStart:", None, False)

    # Cocoa selector  finishOnboardingStart:
    def finishOnboardingStart_(self, timer):
        self._start_timer = None
        # All steps done — write sentinel and exit run loop.
        get_onboarding_done_path().touch()
        if self._window is not None:
            self._window.close()  # triggers windowWillClose_ → _stop_loop
        else:
            self._stop_loop()


# ---------------------------------------------------------------------------
# Main-menu bootstrap  (so Cmd+V / Cmd+A work in text fields)
# ---------------------------------------------------------------------------

def _ensure_main_menu(app):
    """Install a minimal menu bar so Cmd+V/C/X/A work in text fields.

    macOS routes these shortcuts through the main menu; without it they
    are silently dropped even when a text field is the first responder.
    """
    if app.mainMenu() is not None:
        return

    main = NSMenu.alloc().init()
    edit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Edit", None, "")
    edit = NSMenu.alloc().initWithTitle_("Edit")

    for title, action, key in (
        ("Undo",       "undo:",      "z"),
        ("Redo",       "redo:",      "y"),
        (None,         None,         None),
        ("Cut",        "cut:",       "x"),
        ("Copy",       "copy:",      "c"),
        ("Paste",      "paste:",     "v"),
        (None,         None,         None),
        ("Select All", "selectAll:", "a"),
    ):
        if title is None:
            edit.addItem_(NSMenuItem.separatorItem())
            continue
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
        item.setKeyEquivalentModifierMask_(NSEventModifierFlagCommand)
        edit.addItem_(item)

    edit_item.setSubmenu_(edit)
    main.addItem_(edit_item)
    app.setMainMenu_(main)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_onboarding() -> None:
    """Block until the user finishes (or closes) the onboarding wizard.

    Safe to call before ``rumps.App.run()``; they share one NSApplication
    singleton.  ``stop_()`` merely unwinds the *current* ``run()`` call;
    ``run()`` can be entered again by rumps afterward.
    """
    app = NSApplication.sharedApplication()
    _ensure_main_menu(app)
    app.activateIgnoringOtherApps_(True)
    wizard = _OnboardingWizard.alloc().init()
    wizard.show()
    app.run()
