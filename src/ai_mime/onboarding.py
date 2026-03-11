"""4-step AppKit onboarding wizard (first-launch only).

Runs a blocking ``NSApplication.run()`` loop.  Call ``run_onboarding()``
before ``rumps.App.run()``; the NSApplication singleton is shared and
``stop_()`` / ``run()`` can be called multiple times.
"""

from __future__ import annotations

import os
from pathlib import Path

import objc
import yaml
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
    NSPopUpButton,
    NSWindowStyleMaskTitled,
    NSWindowStyleMaskClosable,
    NSBackingStoreBuffered,
    NSNormalWindowLevel,
    NSButtonTypeMomentaryPushIn,
    NSControlTextDidChangeNotification,
    NSEventModifierFlagCommand,
)

from ai_mime.app_data import (
    get_bundled_resource,
    get_env_path,
    get_onboarding_done_path,
    get_user_config_path,
)

# ---------------------------------------------------------------------------
# Layout constants
# ---------------------------------------------------------------------------
_W = 560  # window width
_H = 480  # window height
_M = 40  # side margin
_CW = _W - 2 * _M  # content width
_STEPS = ("Welcome", "Permissions", "API Key", "Done")
_CENTER = 1  # NSTextAlignmentCenter

_PROVIDER_OPTIONS = [
    {
        "label": "Gemini",
        "api_key_env": "GEMINI_API_KEY",
        "api_base": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "default_model": "gemini/gemini-3-flash-preview",
    },
    {
        "label": "OpenAI",
        "api_key_env": "OPENAI_API_KEY",
        "api_base": None,
        "default_model": "openai/gpt-5.4",
    },
]

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


class _OnboardingWizard(NSObject):
    """Window controller + view builder for the 4-step wizard."""

    # ------------------------------------------------------------------
    # Init
    # ------------------------------------------------------------------
    def init(self):
        self = objc.super(_OnboardingWizard, self).init()
        self._step = 0
        self._window = None
        self._content = None
        self._continue_btn = None
        self._api_key_field = None
        self._provider_popup = None
        self._model_field = None
        self._perm_timer = None
        self._perm_rows = {}  # key → {"indicator": NSView, "check": NSTextField, "granted": bool}
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
        self._api_key_field = None
        self._provider_popup = None
        self._model_field = None
        self._perm_rows = {}

    def _render(self):
        self._clear()
        self._add_step_dots()
        (
            self._render_welcome,
            self._render_permissions,
            self._render_api_key,
            self._render_done,
        )[self._step]()

    def _add_step_dots(self):
        n = len(_STEPS)
        dot = 8
        gap = 12
        total = n * dot + (n - 1) * gap
        sx = (_W - total) / 2
        y = _H - 26  # near the top
        for i in range(n):
            v = NSView.alloc().initWithFrame_(
                NSMakeRect(sx + i * (dot + gap), y, dot, dot)
            )
            v.setWantsLayer_(True)
            layer = v.layer()
            layer.setCornerRadius_(dot / 2)
            color = (
                NSColor.systemBlueColor()
                if i <= self._step
                else NSColor.colorWithWhite_alpha_(0.75, 1.0)
            )
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
        self._add_label(
            "AI Mime", x=0, y=_H - 230, w=_W, h=40, size=32, bold=True, align=_CENTER
        )

        # Tagline
        self._add_label(
            "Record your screen actions, then use AI\n"
            "to build replayable workflow automations.",
            x=0,
            y=_H - 310,
            w=_W,
            h=60,
            size=15,
            align=_CENTER,
            color=NSColor.secondaryLabelColor(),
        )

        # Get Started – prominent centered button
        self._add_primary_button("Get Started")

    # ------------------------------------------------------------------
    # Step 1 – Permissions
    # ------------------------------------------------------------------
    def _render_permissions(self):
        self._add_label(
            "Permissions", x=0, y=_H - 86, w=_W, h=34, size=24, bold=True, align=_CENTER
        )
        self._add_label(
            "AI Mime needs two permissions to function.\n"
            "Click buttons below to open settings, then enable AI Mime.",
            x=0,
            y=_H - 155,
            w=_W,
            h=50,
            size=14,
            align=_CENTER,
            color=NSColor.secondaryLabelColor(),
        )

        # Per-permission rows with individual status indicators and buttons
        row_h = 70  # Increased height to fit buttons
        gap = 16
        # stack from top: first perm at _H-250, second below
        y = _H - 250
        for perm in _PERMS:
            self._add_perm_row(perm, y, row_h)
            y -= row_h + gap

        self._add_continue("Continue", enabled=False)

        # Poll for permission state every 0.5 s
        self._perm_timer = (
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                0.5, self, "pollPerms:", None, True
            )
        )

    def _add_perm_row(self, perm, y, h):
        key, title, path, pane = perm["key"], perm["title"], perm["path"], perm["pane"]

        # ── status indicator circle (left) ──
        ind_d = 24
        ind_y = y + h - 30  # Position near top of row

        indicator = NSView.alloc().initWithFrame_(NSMakeRect(_M, ind_y, ind_d, ind_d))
        indicator.setWantsLayer_(True)
        indicator.layer().setCornerRadius_(ind_d / 2)
        indicator.layer().setBackgroundColor_(
            NSColor.colorWithWhite_alpha_(0.78, 1.0).CGColor
        )
        self._content.addSubview_(indicator)

        # Checkmark inside indicator (hidden until granted)
        check = NSTextField.alloc().initWithFrame_(
            NSMakeRect(_M, ind_y + 3, ind_d, ind_d - 8)
        )
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

        title_lbl = NSTextField.alloc().initWithFrame_(
            NSMakeRect(text_x, y + h - 24, text_w, 20)
        )
        title_lbl.setStringValue_(title)
        title_lbl.setBezeled_(False)
        title_lbl.setDrawsBackground_(False)
        title_lbl.setEditable_(False)
        title_lbl.setSelectable_(False)
        title_lbl.setFont_(NSFont.boldSystemFontOfSize_(15))
        self._content.addSubview_(title_lbl)

        # ── path hint ──
        path_lbl = NSTextField.alloc().initWithFrame_(
            NSMakeRect(text_x, y + h - 42, text_w, 16)
        )
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
        open_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(text_x, y + 6, open_btn_w, open_btn_h)
        )
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
        refresh_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(text_x + open_btn_w + 8, y + 6, refresh_btn_w, refresh_btn_h)
        )
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

        subprocess.Popen(
            ["open", "x-apple.systempreferences:com.apple.preference.security"]
        )

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
            self._continue_btn.setEnabled_(
                all(r["granted"] for r in self._perm_rows.values())
            )

    def _update_perm(self, key, granted):
        row = self._perm_rows.get(key)
        if row is None or row["granted"] == granted:
            return
        row["granted"] = granted
        if granted:
            row["indicator"].layer().setBackgroundColor_(
                NSColor.systemGreenColor().CGColor
            )
            row["check"].setHidden_(False)
        else:
            row["indicator"].layer().setBackgroundColor_(
                NSColor.colorWithWhite_alpha_(0.78, 1.0).CGColor
            )
            row["check"].setHidden_(True)

    # ------------------------------------------------------------------
    # Step 2 – API Key
    # ------------------------------------------------------------------
    def _render_api_key(self):
        if self._perm_timer is not None:
            self._perm_timer.invalidate()
            self._perm_timer = None

        self._add_label(
            "LLM Setup", x=0, y=_H - 86, w=_W, h=34, size=24, bold=True, align=_CENTER
        )
        self._add_label(
            "Choose provider/model and paste the API key.\n"
            "This writes .env and user_config.yml for you.",
            x=0,
            y=_H - 154,
            w=_W,
            h=50,
            size=15,
            align=_CENTER,
            color=NSColor.secondaryLabelColor(),
        )

        self._add_label("Provider", x=_M, y=_H - 210, w=120, h=20, size=13, bold=True)
        self._provider_popup = NSPopUpButton.alloc().initWithFrame_(
            NSMakeRect(_M, _H - 242, _CW, 28)
        )
        self._provider_popup.addItemsWithTitles_(
            [p["label"] for p in _PROVIDER_OPTIONS]
        )
        self._provider_popup.setTarget_(self)
        self._provider_popup.setAction_("providerChanged:")
        self._content.addSubview_(self._provider_popup)

        self._add_label("Model", x=_M, y=_H - 282, w=120, h=20, size=13, bold=True)
        self._model_field = NSTextField.alloc().initWithFrame_(
            NSMakeRect(_M, _H - 314, _CW, 32)
        )
        self._model_field.setPlaceholderString_("gemini/gemini-3-flash-preview")
        self._model_field.setFont_(NSFont.systemFontOfSize_(14))
        self._content.addSubview_(self._model_field)

        # Input field
        self._add_label("API key", x=_M, y=_H - 354, w=120, h=20, size=13, bold=True)
        self._api_key_field = NSTextField.alloc().initWithFrame_(
            NSMakeRect(_M, _H - 386, _CW, 32)
        )
        self._api_key_field.setPlaceholderString_("Enter provider API key")
        self._api_key_field.setFont_(NSFont.systemFontOfSize_(14))
        self._content.addSubview_(self._api_key_field)
        self._window.makeFirstResponder_(self._api_key_field)

        self.providerChanged_(None)

        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            self,
            "apiKeyChanged:",
            NSControlTextDidChangeNotification,
            self._api_key_field,
        )
        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            self,
            "modelChanged:",
            NSControlTextDidChangeNotification,
            self._model_field,
        )

        self._add_continue("Continue", enabled=False)

    # Cocoa selector  apiKeyChanged:
    def apiKeyChanged_(self, notification):
        val = (self._api_key_field.stringValue() or "").strip()
        model = (
            (self._model_field.stringValue() or "").strip()
            if self._model_field is not None
            else ""
        )
        if self._continue_btn is not None:
            self._continue_btn.setEnabled_(len(val) > 0 and len(model) > 0)

    def modelChanged_(self, notification):
        self.apiKeyChanged_(notification)

    def providerChanged_(self, sender):
        if self._provider_popup is None or self._model_field is None:
            return
        idx = int(self._provider_popup.indexOfSelectedItem())
        if idx < 0 or idx >= len(_PROVIDER_OPTIONS):
            idx = 0
        default_model = str(_PROVIDER_OPTIONS[idx]["default_model"])
        self._model_field.setStringValue_(default_model)
        self.apiKeyChanged_(None)

    def _upsert_env_var(self, env_path: Path, key: str, value: str) -> None:
        lines: list[str] = []
        if env_path.exists():
            try:
                lines = env_path.read_text(encoding="utf-8").splitlines()
            except Exception:
                lines = []

        out: list[str] = []
        seen = False
        prefix = f"{key}="
        for line in lines:
            if line.startswith(prefix):
                out.append(f"{key}={value}")
                seen = True
            else:
                out.append(line)
        if not seen:
            out.append(f"{key}={value}")
        env_path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")

    def _write_model_selection_to_user_config(
        self, *, model: str, api_base: str | None, api_key_env: str
    ) -> None:
        cfg_path = get_user_config_path()
        cfg: dict = {}
        if cfg_path.exists():
            try:
                loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    cfg = loaded
            except Exception:
                cfg = {}

        reflect = cfg.get("reflect")
        if not isinstance(reflect, dict):
            reflect = {}
            cfg["reflect"] = reflect

        replay = cfg.get("replay")
        if not isinstance(replay, dict):
            replay = {}
            cfg["replay"] = replay

        reflect["model"] = model
        reflect["api_key_env"] = api_key_env
        if api_base:
            reflect["api_base"] = api_base
        else:
            reflect.pop("api_base", None)

        pass_a = reflect.get("pass_a")
        if not isinstance(pass_a, dict):
            pass_a = {}
            reflect["pass_a"] = pass_a
        pass_a["model"] = model

        pass_b = reflect.get("pass_b")
        if not isinstance(pass_b, dict):
            pass_b = {}
            reflect["pass_b"] = pass_b
        pass_b["model"] = model

        replay["model"] = model
        replay["api_key_env"] = api_key_env
        if api_base:
            replay["api_base"] = api_base
        else:
            replay.pop("api_base", None)

        cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")

    # ------------------------------------------------------------------
    # Step 3 – Done
    # ------------------------------------------------------------------
    def _render_done(self):
        # Smaller logo
        logo_path = str(get_bundled_resource("AppIcon.appiconset/icon_128_1x.png"))
        logo_img = NSImage.alloc().initWithContentsOfFile_(logo_path)
        logo_size = 72
        logo_view = NSImageView.alloc().initWithFrame_(
            NSMakeRect((_W - logo_size) / 2, _H - 142, logo_size, logo_size)
        )
        if logo_img is not None:
            logo_view.setImage_(logo_img)
        self._content.addSubview_(logo_view)

        self._add_label(
            "You\u2019re all set!",
            x=0,
            y=_H - 198,
            w=_W,
            h=36,
            size=24,
            bold=True,
            align=_CENTER,
        )
        self._add_label(
            "AI Mime is ready.\nLook for the icon in your macOS menu bar.",
            x=0,
            y=_H - 268,
            w=_W,
            h=52,
            size=15,
            align=_CENTER,
            color=NSColor.secondaryLabelColor(),
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
        lbl.setFont_(
            NSFont.boldSystemFontOfSize_(size)
            if bold
            else NSFont.systemFontOfSize_(size)
        )
        if align:
            lbl.setAlignment_(align)
        if color is not None:
            lbl.setTextColor_(color)
        self._content.addSubview_(lbl)
        return lbl

    def _add_continue(self, title, *, enabled):
        """Standard centred continue button at the bottom."""
        btn_w, btn_h = 140, 40
        btn = NSButton.alloc().initWithFrame_(
            NSMakeRect((_W - btn_w) / 2, 48, btn_w, btn_h)
        )
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
        btn = NSButton.alloc().initWithFrame_(
            NSMakeRect((_W - btn_w) / 2, 48, btn_w, btn_h)
        )
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
        # --- Persist API key before advancing past step 2 ---
        if self._step == 2:
            key = (self._api_key_field.stringValue() or "").strip()
            model = (
                (self._model_field.stringValue() or "").strip()
                if self._model_field is not None
                else ""
            )
            idx = (
                int(self._provider_popup.indexOfSelectedItem())
                if self._provider_popup is not None
                else 0
            )
            if idx < 0 or idx >= len(_PROVIDER_OPTIONS):
                idx = 0
            provider = _PROVIDER_OPTIONS[idx]
            api_key_env = str(provider["api_key_env"])
            api_base = provider["api_base"]

            if key and model:
                self._upsert_env_var(get_env_path(), api_key_env, key)
                os.environ[api_key_env] = key
                self._write_model_selection_to_user_config(
                    model=model,
                    api_base=api_base if isinstance(api_base, str) else None,
                    api_key_env=api_key_env,
                )

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
        ("Undo", "undo:", "z"),
        ("Redo", "redo:", "y"),
        (None, None, None),
        ("Cut", "cut:", "x"),
        ("Copy", "copy:", "c"),
        ("Paste", "paste:", "v"),
        (None, None, None),
        ("Select All", "selectAll:", "a"),
    ):
        if title is None:
            edit.addItem_(NSMenuItem.separatorItem())
            continue
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            title, action, key
        )
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
