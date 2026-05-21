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
    NSWindowStyleMaskTitled,
    NSWindowStyleMaskClosable,
    NSBackingStoreBuffered,
    NSNormalWindowLevel,
    NSButtonTypeMomentaryPushIn,
    NSControlTextDidChangeNotification,
    NSEventModifierFlagCommand,
    NSProgressIndicator,
    NSProgressIndicatorBarStyle,
)

from ai_mime.app_data import (
    get_bundled_browser_harness_dir,
    get_env_path,
    get_managed_browser_harness_path,
    get_managed_python_install_dir,
    get_onboarding_done_path,
    get_bundled_resource,
    get_python_path,
    get_tool_bin_dir,
    get_tool_dir,
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
_STEPS = ("Welcome", "Permissions", "Claude", "Skills", "Done")
_CENTER = 1       # NSTextAlignmentCenter
_ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"
_BROWSER_SKILL_NAME_ENV = "AI_MIME_BROWSER_SKILL_NAME"
_BROWSER_SKILL_PATH_ENV = "AI_MIME_BROWSER_SKILL_PATH"
_MACOS_CU_SKILL_NAME_ENV = "AI_MIME_MACOS_COMPUTER_USE_SKILL_NAME"
_MACOS_CU_SKILL_PATH_ENV = "AI_MIME_MACOS_COMPUTER_USE_SKILL_PATH"
_HERMES_SKILL_REL = "resources/claude-skills/macos-computer-use"
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


def _bundled_hermes_skill_dir() -> Path:
    return get_bundled_resource(_HERMES_SKILL_REL)


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
    macos_computer_use: _ClaudeSkillResolution,
) -> None:
    _merge_env_var(env_path, _BROWSER_SKILL_NAME_ENV, browser.skill_name)
    _merge_env_var(env_path, _BROWSER_SKILL_PATH_ENV, str(browser.path))
    _merge_env_var(env_path, _MACOS_CU_SKILL_NAME_ENV, macos_computer_use.skill_name)
    _merge_env_var(env_path, _MACOS_CU_SKILL_PATH_ENV, str(macos_computer_use.path))
    os.environ[_BROWSER_SKILL_NAME_ENV] = browser.skill_name
    os.environ[_BROWSER_SKILL_PATH_ENV] = str(browser.path)
    os.environ[_MACOS_CU_SKILL_NAME_ENV] = macos_computer_use.skill_name
    os.environ[_MACOS_CU_SKILL_PATH_ENV] = str(macos_computer_use.path)


def _detect_claude_skills(
    *,
    skills_dir: Path | None = None,
) -> tuple[_ClaudeSkillResolution | None, _ClaudeSkillResolution | None]:
    skills_root = skills_dir or _claude_skills_dir()
    browser = _find_existing_compatible_skill(
        skills_root,
        link_names=("browser", "browser-harness"),
        expected_name="browser",
    )
    macos_computer_use = _find_existing_compatible_skill(
        skills_root,
        link_names=("macos-computer-use",),
        expected_name="macos-computer-use",
    )
    return browser, macos_computer_use


def _install_claude_skills(
    *,
    skills_dir: Path | None = None,
    browser_harness_skill_dir: Path | None = None,
    hermes_skill_dir: Path | None = None,
    env_path: Path | None = None,
) -> dict[str, Path]:
    """Install AI Mime's Claude skills by linking bundled/repo sources."""
    skills_root = skills_dir or _claude_skills_dir()
    browser_source = browser_harness_skill_dir or _browser_harness_skill_dir()
    hermes_source = hermes_skill_dir or _bundled_hermes_skill_dir()

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

    macos_computer_use = _find_existing_compatible_skill(
        skills_root,
        link_names=("macos-computer-use",),
        expected_name="macos-computer-use",
    )
    if macos_computer_use is None:
        _ensure_symlink(
            skills_root / "macos-computer-use",
            hermes_source,
            expected_name="macos-computer-use",
        )
        macos_computer_use = _ClaudeSkillResolution(
            link_name="macos-computer-use",
            skill_name="macos-computer-use",
            path=hermes_source.expanduser().resolve(),
            source="installed_by_ai_mime",
        )

    _persist_claude_skill_env(
        env_path=env_path or get_env_path(),
        browser=browser,
        macos_computer_use=macos_computer_use,
    )

    return {
        "browser": skills_root / browser.link_name,
        "macos-computer-use": skills_root / "macos-computer-use",
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
    run=subprocess.run,
    timeout: float = 900.0,
) -> tuple[bool, str]:
    """Install the bundled browser-harness command as an app-owned uv tool."""
    uv = uv_path or get_uv_path()
    python = python_path or get_python_path()
    source = source_dir or _browser_harness_skill_dir()
    if not uv.exists():
        return False, f"uv not found at {uv}"
    if not python.exists():
        return False, f"managed Python not found at {python}"
    if not (source / "pyproject.toml").is_file():
        return False, f"browser-harness source not found at {source}"

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
        self._claude_key_field = None
        self._claude_detected = False
        self._claude_status_label = None
        self._install_btn = None
        self._installing = False
        self._install_progress = None
        self._install_progress_label = None
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
        NSApplication.sharedApplication().stop_(None)

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
        self._claude_key_field = None
        self._claude_status_label = None
        self._install_btn = None
        self._install_progress = None
        self._install_progress_label = None
        self._skill_rows = {}
        self._skills_error_label = None
        self._perm_rows = {}

    def _render(self):
        self._clear()
        self._add_step_dots()
        (
            self._render_welcome,
            self._render_permissions,
            self._render_claude_setup,
            self._render_skills_setup,
            self._render_done,
        )[self._step]()

    def _add_step_dots(self):
        n = len(_STEPS)
        dot = 8
        gap = 12
        total = n * dot + (n - 1) * gap
        sx = (_W - total) / 2
        y = _H - 26          # near the top
        for i in range(n):
            v = NSView.alloc().initWithFrame_(NSMakeRect(sx + i * (dot + gap), y, dot, dot))
            v.setWantsLayer_(True)
            layer = v.layer()
            layer.setCornerRadius_(dot / 2)
            color = NSColor.systemBlueColor() if i <= self._step else NSColor.colorWithWhite_alpha_(0.75, 1.0)
            layer.setBackgroundColor_(color.CGColor)
            self._content.addSubview_(v)

    # ------------------------------------------------------------------
    # Step 0 – Welcome
    # ------------------------------------------------------------------
    def _render_welcome(self):
        # Logo
        logo_path = str(get_bundled_resource("AppIcon.appiconset/icon_128_1x.png"))
        logo_img = NSImage.alloc().initWithContentsOfFile_(logo_path)
        logo_size = 100
        logo_view = NSImageView.alloc().initWithFrame_(
            NSMakeRect((_W - logo_size) / 2, _H - 178, logo_size, logo_size)
        )
        if logo_img is not None:
            logo_view.setImage_(logo_img)
        self._content.addSubview_(logo_view)

        # App name
        self._add_label("AI Mime",
                        x=0, y=_H - 230, w=_W, h=40,
                        size=32, bold=True, align=_CENTER)

        # Tagline
        self._add_label(
            "Record your screen actions, then use AI\n"
            "to build replayable workflow automations.",
            x=0, y=_H - 310, w=_W, h=60,
            size=15, align=_CENTER, color=NSColor.secondaryLabelColor(),
        )

        # Get Started – prominent centered button
        self._add_primary_button("Get Started")

    # ------------------------------------------------------------------
    # Step 1 – Permissions
    # ------------------------------------------------------------------
    def _render_permissions(self):
        self._add_label("Permissions",
                        x=0, y=_H - 86, w=_W, h=34,
                        size=24, bold=True, align=_CENTER)
        self._add_label(
            "AI Mime needs two permissions to function.\n"
            "Click buttons below to open settings, then enable AI Mime.",
            x=0, y=_H - 155, w=_W, h=50,
            size=14, align=_CENTER, color=NSColor.secondaryLabelColor(),
        )

        # Per-permission rows with individual status indicators and buttons
        row_h = 70  # Increased height to fit buttons
        gap   = 16
        # stack from top: first perm at _H-250, second below
        y = _H - 250
        for perm in _PERMS:
            self._add_perm_row(perm, y, row_h)
            y -= (row_h + gap)

        self._add_continue("Continue", enabled=False)

        # Poll for permission state every 0.5 s
        self._perm_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.5, self, "pollPerms:", None, True
        )

    def _add_perm_row(self, perm, y, h):
        key, title, path, pane = perm["key"], perm["title"], perm["path"], perm["pane"]

        # ── status indicator circle (left) ──
        ind_d  = 24
        ind_y  = y + h - 30  # Position near top of row

        indicator = NSView.alloc().initWithFrame_(NSMakeRect(_M, ind_y, ind_d, ind_d))
        indicator.setWantsLayer_(True)
        indicator.layer().setCornerRadius_(ind_d / 2)
        indicator.layer().setBackgroundColor_(NSColor.colorWithWhite_alpha_(0.78, 1.0).CGColor)
        self._content.addSubview_(indicator)

        # Checkmark inside indicator (hidden until granted)
        check = NSTextField.alloc().initWithFrame_(NSMakeRect(_M, ind_y + 3, ind_d, ind_d - 8))
        check.setStringValue_("\u2713")
        check.setBezeled_(False)
        check.setDrawsBackground_(False)
        check.setEditable_(False)
        check.setSelectable_(False)
        check.setFont_(NSFont.boldSystemFontOfSize_(14))
        check.setAlignment_(_CENTER)
        check.setTextColor_(NSColor.whiteColor())
        check.setHidden_(True)
        self._content.addSubview_(check)

        # ── title ──
        text_x = _M + ind_d + 12
        text_w = _CW - ind_d - 12

        title_lbl = NSTextField.alloc().initWithFrame_(NSMakeRect(text_x, y + h - 24, text_w, 20))
        title_lbl.setStringValue_(title)
        title_lbl.setBezeled_(False)
        title_lbl.setDrawsBackground_(False)
        title_lbl.setEditable_(False)
        title_lbl.setSelectable_(False)
        title_lbl.setFont_(NSFont.boldSystemFontOfSize_(15))
        self._content.addSubview_(title_lbl)

        # ── path hint ──
        path_lbl = NSTextField.alloc().initWithFrame_(NSMakeRect(text_x, y + h - 42, text_w, 16))
        path_lbl.setStringValue_(path)
        path_lbl.setBezeled_(False)
        path_lbl.setDrawsBackground_(False)
        path_lbl.setEditable_(False)
        path_lbl.setSelectable_(False)
        path_lbl.setFont_(NSFont.systemFontOfSize_(11))
        path_lbl.setTextColor_(NSColor.secondaryLabelColor())
        self._content.addSubview_(path_lbl)

        # ── "Open Settings" button ──
        open_btn_w, open_btn_h = 120, 28
        open_btn = NSButton.alloc().initWithFrame_(NSMakeRect(text_x, y + 6, open_btn_w, open_btn_h))
        open_btn.setTitle_("Open Settings")
        open_btn.setButtonType_(NSButtonTypeMomentaryPushIn)
        open_btn.setTarget_(self)
        open_btn.setAction_("openSpecificSettings:")
        open_btn.setTag_(hash(pane))  # Store pane identifier in tag
        open_btn.setBezelStyle_(1)  # Rounded bezel
        open_btn.setFont_(NSFont.systemFontOfSize_(12))
        self._content.addSubview_(open_btn)

        # ── "Refresh" button ──
        refresh_btn_w, refresh_btn_h = 80, 28
        refresh_btn = NSButton.alloc().initWithFrame_(NSMakeRect(text_x + open_btn_w + 8, y + 6, refresh_btn_w, refresh_btn_h))
        refresh_btn.setTitle_("Refresh")
        refresh_btn.setButtonType_(NSButtonTypeMomentaryPushIn)
        refresh_btn.setTarget_(self)
        refresh_btn.setAction_("pollPerms:")
        refresh_btn.setBezelStyle_(1)
        refresh_btn.setFont_(NSFont.systemFontOfSize_(12))
        self._content.addSubview_(refresh_btn)

        self._perm_rows[key] = {
            "indicator": indicator,
            "check": check,
            "granted": False,
            "pane": pane,
        }

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
            row["indicator"].layer().setBackgroundColor_(NSColor.systemGreenColor().CGColor)
            row["check"].setHidden_(False)
        else:
            row["indicator"].layer().setBackgroundColor_(NSColor.colorWithWhite_alpha_(0.78, 1.0).CGColor)
            row["check"].setHidden_(True)

    # ------------------------------------------------------------------
    # Step 2 – Claude Setup
    # ------------------------------------------------------------------
    def _render_claude_setup(self):
        if self._perm_timer is not None:
            self._perm_timer.invalidate()
            self._perm_timer = None

        self._claude_detected, claude_msg = _detect_local_claude()

        self._add_label("Claude Setup",
                        x=0, y=_H - 86, w=_W, h=34,
                        size=24, bold=True, align=_CENTER)
        self._add_label(
            "Connect AI Mime with Claude. Use your local Claude Code\n"
            "installation, or paste an Anthropic API key below.",
            x=0, y=_H - 150, w=_W, h=48,
            size=15, align=_CENTER, color=NSColor.secondaryLabelColor(),
        )

        self._claude_status_label = self._add_label(
            claude_msg,
            x=_M, y=_H - 200, w=_CW, h=24,
            size=13, align=_CENTER,
            color=NSColor.systemGreenColor() if self._claude_detected else NSColor.secondaryLabelColor(),
        )

        refresh_btn_w, refresh_btn_h = 150, 30
        refresh_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect((_W - refresh_btn_w) / 2, _H - 244, refresh_btn_w, refresh_btn_h)
        )
        refresh_btn.setTitle_("Check Claude Code")
        refresh_btn.setButtonType_(NSButtonTypeMomentaryPushIn)
        refresh_btn.setTarget_(self)
        refresh_btn.setAction_("checkClaudeCode:")
        refresh_btn.setBezelStyle_(1)
        refresh_btn.setFont_(NSFont.systemFontOfSize_(12))
        self._content.addSubview_(refresh_btn)

        self._add_label(
            "Anthropic API key",
            x=_M, y=_H - 295, w=_CW, h=18,
            size=12, bold=True, color=NSColor.secondaryLabelColor(),
        )
        self._claude_key_field = NSTextField.alloc().initWithFrame_(NSMakeRect(_M, _H - 335, _CW, 36))
        self._claude_key_field.setPlaceholderString_("sk-ant-...")
        self._claude_key_field.setFont_(NSFont.systemFontOfSize_(15))
        self._content.addSubview_(self._claude_key_field)
        if not self._claude_detected:
            self._window.makeFirstResponder_(self._claude_key_field)

        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            self,
            "claudeKeyChanged:",
            NSControlTextDidChangeNotification,
            self._claude_key_field,
        )

        self._add_continue("Continue", enabled=self._claude_detected)

    # Cocoa selector  checkClaudeCode:
    def checkClaudeCode_(self, sender):
        self._claude_detected, msg = _detect_local_claude()
        if self._claude_status_label is not None:
            self._claude_status_label.setStringValue_(msg)
            self._claude_status_label.setTextColor_(
                NSColor.systemGreenColor() if self._claude_detected else NSColor.secondaryLabelColor()
            )
        self._update_claude_continue()

    # Cocoa selector  claudeKeyChanged:
    def claudeKeyChanged_(self, notification):
        self._update_claude_continue()

    def _update_claude_continue(self):
        val = (self._claude_key_field.stringValue() or "").strip() if self._claude_key_field is not None else ""
        if self._continue_btn is not None:
            self._continue_btn.setEnabled_(self._claude_detected or len(val) > 0)

    # ------------------------------------------------------------------
    # Step 3 – Skills Setup
    # ------------------------------------------------------------------
    def _render_skills_setup(self):
        self._add_label("Install Claude Skills",
                        x=0, y=_H - 86, w=_W, h=34,
                        size=24, bold=True, align=_CENTER)
        self._add_label(
            "AI Mime links automation skills into Claude Code and prepares\n"
            "Python for workflow scripts when running the packaged app.",
            x=0, y=_H - 150, w=_W, h=48,
            size=15, align=_CENTER, color=NSColor.secondaryLabelColor(),
        )

        self._add_skill_row("browser-harness", "Repo browser-harness skill", _H - 230)
        self._add_skill_row("macos-computer-use", "Bundled Hermes macOS computer-use skill", _H - 300)
        if is_frozen():
            self._add_skill_row("python-3.12", "Managed Python for workflow virtualenvs", _H - 350)

        install_btn_w, install_btn_h = 150, 34
        self._install_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect((_W - install_btn_w) / 2, _H - 398, install_btn_w, install_btn_h)
        )
        self._install_btn.setTitle_("Install")
        self._install_btn.setButtonType_(NSButtonTypeMomentaryPushIn)
        self._install_btn.setTarget_(self)
        self._install_btn.setAction_("installSkills:")
        self._install_btn.setBezelStyle_(1)
        self._install_btn.setFont_(NSFont.systemFontOfSize_(13))
        self._content.addSubview_(self._install_btn)

        progress_w, progress_h = 260, 12
        self._install_progress = NSProgressIndicator.alloc().initWithFrame_(
            NSMakeRect((_W - progress_w) / 2, _H - 428, progress_w, progress_h)
        )
        self._install_progress.setStyle_(NSProgressIndicatorBarStyle)
        self._install_progress.setIndeterminate_(False)
        self._install_progress.setMinValue_(0.0)
        self._install_progress.setMaxValue_(100.0)
        self._install_progress.setDoubleValue_(0.0)
        self._install_progress.setHidden_(True)
        self._content.addSubview_(self._install_progress)

        self._install_progress_label = self._add_label(
            "",
            x=_M, y=_H - 456, w=_CW, h=18,
            size=11, align=_CENTER, color=NSColor.secondaryLabelColor(),
        )

        self._skills_error_label = self._add_label(
            "",
            x=_M, y=8, w=_CW, h=36,
            size=12, align=_CENTER, color=NSColor.systemRedColor(),
        )
        self._add_continue("Continue", enabled=False)
        self._refresh_skill_status()

    def _add_skill_row(self, name, detail, y):
        ind_d = 22
        indicator = NSView.alloc().initWithFrame_(NSMakeRect(_M, y, ind_d, ind_d))
        indicator.setWantsLayer_(True)
        indicator.layer().setCornerRadius_(ind_d / 2)
        indicator.layer().setBackgroundColor_(NSColor.colorWithWhite_alpha_(0.78, 1.0).CGColor)
        self._content.addSubview_(indicator)

        check = NSTextField.alloc().initWithFrame_(NSMakeRect(_M, y + 2, ind_d, ind_d - 6))
        check.setStringValue_("\u2713")
        check.setBezeled_(False)
        check.setDrawsBackground_(False)
        check.setEditable_(False)
        check.setSelectable_(False)
        check.setFont_(NSFont.boldSystemFontOfSize_(13))
        check.setAlignment_(_CENTER)
        check.setTextColor_(NSColor.whiteColor())
        check.setHidden_(True)
        self._content.addSubview_(check)

        text_x = _M + ind_d + 12
        self._add_label(name, x=text_x, y=y + 6, w=210, h=18, size=14, bold=True)
        self._add_label(
            detail,
            x=text_x, y=y - 14, w=300, h=16,
            size=11, color=NSColor.secondaryLabelColor(),
        )
        status = self._add_label(
            "Not installed",
            x=_W - _M - 140, y=y + 2, w=140, h=18,
            size=12, color=NSColor.secondaryLabelColor(),
        )
        self._skill_rows[name] = {"indicator": indicator, "check": check, "status": status}

    def _set_skill_status(self, name, installed, status_text):
        row = self._skill_rows.get(name)
        if row is None:
            return
        row["status"].setStringValue_(status_text)
        if installed:
            row["indicator"].layer().setBackgroundColor_(NSColor.systemGreenColor().CGColor)
            row["check"].setHidden_(False)
            row["status"].setTextColor_(NSColor.systemGreenColor())
        else:
            row["indicator"].layer().setBackgroundColor_(NSColor.colorWithWhite_alpha_(0.78, 1.0).CGColor)
            row["check"].setHidden_(True)
            row["status"].setTextColor_(NSColor.secondaryLabelColor())

    def _refresh_skill_status(self):
        skills_root = _claude_skills_dir()
        try:
            browser_skill, hermes_skill = _detect_claude_skills(skills_dir=skills_root)
        except Exception as e:
            browser_skill = None
            hermes_skill = None
            if self._skills_error_label is not None:
                self._skills_error_label.setStringValue_(str(e))
        browser_ok = browser_skill is not None
        hermes_ok = hermes_skill is not None
        python_ok = (not is_frozen()) or get_python_path().exists()
        self._set_skill_status("browser-harness", browser_ok, "Installed" if browser_ok else "Not installed")
        self._set_skill_status("macos-computer-use", hermes_ok, "Installed" if hermes_ok else "Not installed")
        if is_frozen():
            self._set_skill_status("python-3.12", python_ok, "Installed" if python_ok else "Not installed")
        if browser_skill is not None and hermes_skill is not None:
            _persist_claude_skill_env(
                env_path=get_env_path(),
                browser=browser_skill,
                macos_computer_use=hermes_skill,
            )
        if self._continue_btn is not None:
            self._continue_btn.setEnabled_(browser_ok and hermes_ok and python_ok)

    # Cocoa selector  installSkills:
    def installSkills_(self, sender):
        if self._installing:
            return
        self._installing = True
        if self._skills_error_label is not None:
            self._skills_error_label.setStringValue_("")
        self._set_install_progress("Preparing install...", 5)
        if self._install_btn is not None:
            self._install_btn.setTitle_("Installing...")
            self._install_btn.setEnabled_(False)
        if self._continue_btn is not None:
            self._continue_btn.setEnabled_(False)
            self._continue_btn.setHidden_(True)

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
        if self._install_btn is not None:
            self._install_btn.setTitle_("Install")
            self._install_btn.setEnabled_(True)
        if self._install_progress is not None:
            self._install_progress.setHidden_(True)
        if self._install_progress_label is not None:
            self._install_progress_label.setStringValue_("")
        if self._continue_btn is not None:
            self._continue_btn.setHidden_(False)
        self._refresh_skill_status()

    # ------------------------------------------------------------------
    # Step 4 – Done
    # ------------------------------------------------------------------
    def _render_done(self):
        # Smaller logo
        logo_path = str(get_bundled_resource("AppIcon.appiconset/icon_128_1x.png"))
        logo_img  = NSImage.alloc().initWithContentsOfFile_(logo_path)
        logo_size = 72
        logo_view = NSImageView.alloc().initWithFrame_(
            NSMakeRect((_W - logo_size) / 2, _H - 142, logo_size, logo_size)
        )
        if logo_img is not None:
            logo_view.setImage_(logo_img)
        self._content.addSubview_(logo_view)

        self._add_label("You\u2019re all set!",
                        x=0, y=_H - 198, w=_W, h=36,
                        size=24, bold=True, align=_CENTER)
        self._add_label(
            "AI Mime is ready.\n"
            "Look for the icon in your macOS menu bar.",
            x=0, y=_H - 268, w=_W, h=52,
            size=15, align=_CENTER, color=NSColor.secondaryLabelColor(),
        )

        self._add_primary_button("Start")

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

    def _add_continue(self, title, *, enabled):
        """Standard centred continue button at the bottom."""
        btn_w, btn_h = 140, 40
        btn = NSButton.alloc().initWithFrame_(NSMakeRect((_W - btn_w) / 2, 48, btn_w, btn_h))
        btn.setTitle_(title)
        btn.setButtonType_(NSButtonTypeMomentaryPushIn)
        btn.setTarget_(self)
        btn.setAction_("onContinue:")
        btn.setEnabled_(enabled)
        self._content.addSubview_(btn)
        self._continue_btn = btn
        return btn

    def _add_primary_button(self, title):
        """Larger, blue-tinted button used on Welcome and Done pages."""
        btn_w, btn_h = 180, 44
        btn = NSButton.alloc().initWithFrame_(NSMakeRect((_W - btn_w) / 2, 48, btn_w, btn_h))
        btn.setTitle_(title)
        btn.setButtonType_(NSButtonTypeMomentaryPushIn)
        btn.setTarget_(self)
        btn.setAction_("onContinue:")
        btn.setEnabled_(True)
        try:
            btn.setContentTintColor_(NSColor.systemBlueColor())
        except Exception:
            pass
        self._content.addSubview_(btn)
        self._continue_btn = btn
        return btn

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    # Cocoa selector  onContinue:
    def onContinue_(self, sender):
        # --- Persist Anthropic API key before advancing past step 2 ---
        if self._step == 2:
            key = (self._claude_key_field.stringValue() or "").strip() if self._claude_key_field is not None else ""
            if key:
                _merge_env_var(get_env_path(), _ANTHROPIC_API_KEY_ENV, key)
                os.environ[_ANTHROPIC_API_KEY_ENV] = key

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
