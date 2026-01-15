from __future__ import annotations

"""
Lightweight macOS replay overlay UI.

Runs in the existing rumps/AppKit UI process (NOT in the replay worker process).

The overlay is intended to be excluded from agent screenshots by capturing the
screen content *below* this window id using Quartz (see ScreenshotRecorder).
"""

# pyright: reportAttributeAccessIssue=false

from dataclasses import dataclass
from typing import Callable
from pathlib import Path

# macOS UI (PyObjC / AppKit). Assume it's available in this app context.
import AppKit  # type: ignore[import-not-found]

from ai_mime.overlay.ui_common import active_screen_visible_frame


def _label(
    text: str = "",
    *,
    font: AppKit.NSFont | None = None,
    color: AppKit.NSColor | None = None,
) -> AppKit.NSTextField:
    tf = AppKit.NSTextField.labelWithString_(text)
    # Allow wrapping / multi-line where supported.
    try:
        tf.setMaximumNumberOfLines_(0)
    except Exception:
        pass
    try:
        tf.setLineBreakMode_(AppKit.NSLineBreakByWordWrapping)  # type: ignore[attr-defined]
    except Exception:
        pass
    # Ensure multi-line labels behave like labels: wrap, and (if supported) truncate the last
    # visible line instead of expanding the overlay for long/unbroken strings.
    try:
        tf.setUsesSingleLineMode_(False)  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        cell = tf.cell()
        try:
            cell.setWraps_(True)
        except Exception:
            pass
        try:
            cell.setTruncatesLastVisibleLine_(True)
        except Exception:
            pass
    except Exception:
        pass
    if font is not None:
        try:
            tf.setFont_(font)
        except Exception:
            pass
    if color is not None:
        try:
            tf.setTextColor_(color)
        except Exception:
            pass
    return tf


def _rotated_image_90_steps(img: AppKit.NSImage, quarter_turns: int) -> AppKit.NSImage:
    """
    Return a new NSImage rotated by 90° * quarter_turns about its center.
    quarter_turns should be in {0,1,2,3}.
    """
    qt = int(quarter_turns) % 4
    if qt == 0:
        return img

    size = img.size()
    w = float(getattr(size, "width", 0.0))
    h = float(getattr(size, "height", 0.0))
    out = AppKit.NSImage.alloc().initWithSize_(size)

    out.lockFocus()
    try:
        # Rotate around center: translate to center, rotate, translate back.
        t = AppKit.NSAffineTransform.transform()
        t.translateXBy_yBy_(w / 2.0, h / 2.0)
        t.rotateByDegrees_(90.0 * float(qt))
        t.translateXBy_yBy_(-w / 2.0, -h / 2.0)
        t.concat()

        img.drawInRect_fromRect_operation_fraction_(
            AppKit.NSMakeRect(0, 0, w, h),
            AppKit.NSMakeRect(0, 0, w, h),
            AppKit.NSCompositingOperationSourceOver,  # type: ignore[attr-defined]
            1.0,
        )
    finally:
        out.unlockFocus()
    return out


@dataclass
class ReplayOverlayState:
    subtask_idx: int | None = None
    subtask_total: int | None = None
    subtask_text: str = ""
    predicted_action: str = ""


class ReplayOverlayActionHandler(AppKit.NSObject):  # type: ignore[misc]
    def pause_(self, sender):  # noqa: N802 - ObjC selector
        try:
            self._overlay._handle_pause_clicked()  # type: ignore[attr-defined]
        except Exception:
            pass

    def stop_(self, sender):  # noqa: N802 - ObjC selector
        try:
            self._overlay._handle_stop_clicked()  # type: ignore[attr-defined]
        except Exception:
            pass

    def tick_(self, sender):  # noqa: N802 - ObjC selector
        # 90-degree step rotation animation.
        try:
            self._overlay._advance_icon_rotation()  # type: ignore[attr-defined]
        except Exception:
            pass


class ReplayOverlay:
    """
    A small always-on-top overlay panel with a spinner and the key fields:
    - Subtask (wraps to 2 lines)
    - Predicted action
    """

    def __init__(
        self,
        *,
        on_toggle_pause: Callable[[bool], None] | None = None,
        on_stop: Callable[[], None] | None = None,
    ) -> None:
        self._state = ReplayOverlayState()
        self._paused = False
        self._on_toggle_pause = on_toggle_pause
        self._on_stop = on_stop
        self._action_handler = ReplayOverlayActionHandler.alloc().init()
        try:
            self._action_handler._overlay = self  # type: ignore[attr-defined]
        except Exception:
            pass

        self._rotation_deg = 0.0
        self._rotation_timer = None
        self._icon_frames: list[AppKit.NSImage] = []
        self._icon_frame_idx = 0

        # Resolve bundled icon asset (use 32px for crisp downscaling in the small overlay).
        repo_root = Path(__file__).resolve().parents[3]
        self._icon_path = repo_root / "docs" / "logo" / "icon32.png"

        # Window sizing / placement
        width = 420.0
        height = 142.0

        # Place at the middle of the right edge of the active screen with some margin.
        try:
            sx, sy, sw, sh = active_screen_visible_frame()
        except Exception:
            sx, sy, sw, sh = 0.0, 0.0, 1440.0, 900.0

        margin = 16.0
        x = float(sx + sw - width - margin)
        y = float(sy + (sh - height) / 2.0)

        rect = AppKit.NSMakeRect(x, y, width, height)

        style = AppKit.NSWindowStyleMaskBorderless  # type: ignore[attr-defined]
        # In some macOS setups, a non-activating panel behaves better across fullscreen Spaces.
        try:
            style = style | AppKit.NSWindowStyleMaskNonactivatingPanel  # type: ignore[attr-defined]
        except Exception:
            pass
        self._panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect,
            style,
            AppKit.NSBackingStoreBuffered,  # type: ignore[attr-defined]
            False,
        )

        # Appearance: use macOS-native vibrancy/HUD material (adapts to light/dark).
        self._panel.setOpaque_(False)
        self._panel.setHasShadow_(True)
        try:
            self._panel.setBackgroundColor_(AppKit.NSColor.clearColor())
        except Exception:
            pass

        # Always on top; do not steal focus; show on all spaces/fullscreen.
        try:
            # Screen-saver level tends to survive fullscreen Spaces and stay above app windows.
            self._panel.setLevel_(AppKit.NSScreenSaverWindowLevel)  # type: ignore[attr-defined]
        except Exception:
            try:
                self._panel.setLevel_(AppKit.NSStatusWindowLevel)  # type: ignore[attr-defined]
            except Exception:
                self._panel.setLevel_(AppKit.NSFloatingWindowLevel)  # type: ignore[attr-defined]
        self._panel.setHidesOnDeactivate_(False)
        try:
            self._panel.setCollectionBehavior_(
                AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces  # type: ignore[attr-defined]
                | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary  # type: ignore[attr-defined]
            )
        except Exception:
            pass
        # Additional hint: move with the active Space when the user switches fullscreen Spaces.
        try:
            self._panel.setCollectionBehavior_(
                int(self._panel.collectionBehavior())
                | int(getattr(AppKit, "NSWindowCollectionBehaviorMoveToActiveSpace", 0))  # type: ignore[attr-defined]
            )
        except Exception:
            pass
        # Interactive overlay: allow clicks on Pause/Stop.
        try:
            self._panel.setIgnoresMouseEvents_(False)
        except Exception:
            pass

        # Backing view: NSVisualEffectView for consistent macOS theme.
        effect = AppKit.NSVisualEffectView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, width, height))
        try:
            effect.setBlendingMode_(AppKit.NSVisualEffectBlendingModeBehindWindow)  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            # Prefer HUD-like material if available.
            material = getattr(AppKit, "NSVisualEffectMaterialHUDWindow", None)  # type: ignore[attr-defined]
            if material is None:
                material = getattr(AppKit, "NSVisualEffectMaterialMenu", None)  # type: ignore[attr-defined]
            if material is not None:
                effect.setMaterial_(material)
        except Exception:
            pass
        try:
            effect.setState_(AppKit.NSVisualEffectStateActive)  # type: ignore[attr-defined]
        except Exception:
            pass
        self._panel.setContentView_(effect)
        content = effect

        # Fonts (aim for a friendly macOS-native feel; avoid "README"/fixed-pitch look)
        def _sys_font(size: float, weight: float | None = None) -> AppKit.NSFont:
            try:
                if weight is not None:
                    return AppKit.NSFont.systemFontOfSize_weight_(size, weight)  # type: ignore[attr-defined]
            except Exception:
                pass
            try:
                return AppKit.NSFont.systemFontOfSize_(size)
            except Exception:
                return AppKit.NSFont.systemFontOfSize_(size)

        # Weight constants can vary; use getattr fallback.
        w_semibold = float(getattr(AppKit, "NSFontWeightSemibold", 0.6))  # type: ignore[attr-defined]
        w_medium = float(getattr(AppKit, "NSFontWeightMedium", 0.23))  # type: ignore[attr-defined]

        title_font = _sys_font(13.0, w_semibold)
        header_font = _sys_font(11.0, w_semibold)
        body_font = _sys_font(10.75, None)
        pred_font = _sys_font(10.75, w_medium)

        # Theme colors (adaptive light/dark)
        try:
            c_title = AppKit.NSColor.labelColor()
            c_header = AppKit.NSColor.secondaryLabelColor()
            c_body = AppKit.NSColor.labelColor()
            c_mono = AppKit.NSColor.labelColor()
        except Exception:
            c_title = None
            c_header = None
            c_body = None
            c_mono = None

        # Rotating app icon + title row
        self._icon_view = AppKit.NSImageView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 16, 16))
        img = AppKit.NSImage.alloc().initWithContentsOfFile_(str(self._icon_path))
        if img is None:
            img = AppKit.NSApplication.sharedApplication().applicationIconImage()
        # Precompute 4 frames at 0/90/180/270 degrees and cycle.
        self._icon_frames = [_rotated_image_90_steps(img, k) for k in range(4)]
        self._icon_frame_idx = 0
        self._icon_view.setImage_(self._icon_frames[0])
        self._icon_view.setImageScaling_(AppKit.NSImageScaleProportionallyUpOrDown)  # type: ignore[attr-defined]

        self._title = _label("Replay running", font=title_font, color=c_title)

        # Field labels
        self._subtask = _label("", font=body_font, color=c_body)
        # Make action slightly secondary so it reads friendlier / less “loggy”.
        self._pred = _label("", font=pred_font, color=c_header)

        # Constrain wrapping/lines for a compact UI.
        try:
            self._subtask.setMaximumNumberOfLines_(2)
        except Exception:
            pass
        try:
            self._pred.setMaximumNumberOfLines_(2)
        except Exception:
            pass
        # Force wrapping even for long/unbroken strings; truncate beyond the max lines.
        for v in (self._subtask, self._pred):
            try:
                v.setLineBreakMode_(AppKit.NSLineBreakByCharWrapping)  # type: ignore[attr-defined]
            except Exception:
                pass
            try:
                cell = v.cell()
                try:
                    cell.setWraps_(True)
                except Exception:
                    pass
                try:
                    cell.setTruncatesLastVisibleLine_(True)
                except Exception:
                    pass
                # Some macOS versions respect truncation better when the cell is set too.
                try:
                    cell.setLineBreakMode_(AppKit.NSLineBreakByCharWrapping)  # type: ignore[attr-defined]
                except Exception:
                    pass
            except Exception:
                pass

        # Stack layout
        stack = AppKit.NSStackView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, width, height))
        stack.setOrientation_(AppKit.NSUserInterfaceLayoutOrientationVertical)  # type: ignore[attr-defined]
        stack.setAlignment_(AppKit.NSLayoutAttributeLeading)  # type: ignore[attr-defined]
        stack.setSpacing_(4.0)
        # We control padding via outer constraints (below). Avoid double-padding from stack edgeInsets.
        try:
            insets = AppKit.NSMakeEdgeInsets(0, 0, 0, 0)  # type: ignore[attr-defined]
        except Exception:
            try:
                insets = AppKit.NSEdgeInsetsMake(0, 0, 0, 0)  # type: ignore[attr-defined]
            except Exception:
                insets = (0, 0, 0, 0)
        try:
            stack.setEdgeInsets_(insets)
        except Exception:
            pass

        # Row: spinner + title
        row = AppKit.NSStackView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, width, 20))
        row.setOrientation_(AppKit.NSUserInterfaceLayoutOrientationHorizontal)  # type: ignore[attr-defined]
        row.setAlignment_(AppKit.NSLayoutAttributeCenterY)  # type: ignore[attr-defined]
        row.setSpacing_(6.0)
        row.addArrangedSubview_(self._icon_view)
        row.addArrangedSubview_(self._title)
        # Avoid clipping the title in compact widths.
        try:
            self._title.setLineBreakMode_(AppKit.NSLineBreakByClipping)  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            self._title.setContentCompressionResistancePriority_forOrientation_(751, AppKit.NSLayoutConstraintOrientationHorizontal)  # type: ignore[attr-defined]
        except Exception:
            pass
        stack.addArrangedSubview_(row)

        stack.addArrangedSubview_(self._subtask)

        stack.addArrangedSubview_(self._pred)

        # Controls row
        controls = AppKit.NSStackView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, width, 22))
        controls.setOrientation_(AppKit.NSUserInterfaceLayoutOrientationHorizontal)  # type: ignore[attr-defined]
        controls.setAlignment_(AppKit.NSLayoutAttributeCenterY)  # type: ignore[attr-defined]
        controls.setSpacing_(8.0)

        # Spacer to push buttons right.
        try:
            spacer = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 1, 1))
            controls.addArrangedSubview_(spacer)
            # Let spacer expand.
            spacer.setContentHuggingPriority_forOrientation_(1, AppKit.NSLayoutConstraintOrientationHorizontal)  # type: ignore[attr-defined]
        except Exception:
            pass

        self._pause_btn = AppKit.NSButton.buttonWithTitle_target_action_(  # type: ignore[attr-defined]
            "Pause",
            self._action_handler,
            "pause:",
        )
        self._stop_btn = AppKit.NSButton.buttonWithTitle_target_action_(  # type: ignore[attr-defined]
            "Stop",
            self._action_handler,
            "stop:",
        )
        for b in (self._pause_btn, self._stop_btn):
            try:
                b.setBezelStyle_(AppKit.NSBezelStyleRounded)  # type: ignore[attr-defined]
            except Exception:
                pass
            try:
                b.setControlSize_(AppKit.NSControlSizeSmall)  # type: ignore[attr-defined]
            except Exception:
                pass
            try:
                b.setFont_(_sys_font(11.0, w_medium))
            except Exception:
                pass
        controls.addArrangedSubview_(self._pause_btn)
        controls.addArrangedSubview_(self._stop_btn)
        stack.addArrangedSubview_(controls)

        content.addSubview_(stack)

        # Auto-layout: pin stack to content.
        try:
            # Minimal padding inside the overlay box.
            m = 6.0
            stack.setTranslatesAutoresizingMaskIntoConstraints_(False)
            AppKit.NSLayoutConstraint.activateConstraints_(
                [
                    stack.leadingAnchor().constraintEqualToAnchor_constant_(content.leadingAnchor(), m),
                    stack.trailingAnchor().constraintEqualToAnchor_constant_(content.trailingAnchor(), -m),
                    stack.topAnchor().constraintEqualToAnchor_constant_(content.topAnchor(), m),
                    stack.bottomAnchor().constraintEqualToAnchor_constant_(content.bottomAnchor(), -m),
                ]
            )
        except Exception:
            pass

        # Ensure labels wrap within the overlay width (NSStackView doesn't force arranged subviews
        # to match its width unless we constrain it).
        try:
            for v in (self._subtask, self._pred, self._title):
                v.setTranslatesAutoresizingMaskIntoConstraints_(False)
            AppKit.NSLayoutConstraint.activateConstraints_(
                [
                    self._subtask.widthAnchor().constraintEqualToAnchor_(stack.widthAnchor()),
                    self._pred.widthAnchor().constraintEqualToAnchor_(stack.widthAnchor()),
                ]
            )
        except Exception:
            pass

        # Hint preferred wrapping width (helps NSTextField compute multi-line intrinsic size).
        try:
            self._subtask.setPreferredMaxLayoutWidth_(float(width - 26.0))
        except Exception:
            pass
        try:
            self._pred.setPreferredMaxLayoutWidth_(float(width - 26.0))
        except Exception:
            pass

        # Start hidden until explicitly shown.
        self.hide()

    def show(self) -> None:
        try:
            self._panel.orderFrontRegardless()
        except Exception:
            try:
                self._panel.makeKeyAndOrderFront_(None)
            except Exception:
                pass
        # Start / resume rotation timer.
        if self._rotation_timer is None:
            self._rotation_timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                0.28,
                self._action_handler,
                "tick:",
                None,
                True,
            )

    def hide(self) -> None:
        try:
            self._panel.orderOut_(None)
        except Exception:
            pass

    def close(self) -> None:
        if self._rotation_timer is not None:
            try:
                self._rotation_timer.invalidate()
            except Exception:
                pass
            self._rotation_timer = None
        try:
            self._panel.close()
        except Exception:
            pass

    def _handle_pause_clicked(self) -> None:
        self._paused = not self._paused
        try:
            self._pause_btn.setTitle_("Resume" if self._paused else "Pause")
        except Exception:
            pass
        try:
            self._title.setStringValue_("Replay paused" if self._paused else "Replay running")
        except Exception:
            pass
        cb = self._on_toggle_pause
        if cb is not None:
            cb(self._paused)

    def _handle_stop_clicked(self) -> None:
        # Disable buttons immediately to avoid double-submits.
        try:
            self._pause_btn.setEnabled_(False)
        except Exception:
            pass
        try:
            self._stop_btn.setEnabled_(False)
        except Exception:
            pass
        try:
            self._title.setStringValue_("Stopping…")
        except Exception:
            pass
        cb = self._on_stop
        if cb is not None:
            cb()

    def _advance_icon_rotation(self) -> None:
        # Rotate by 90 degrees each tick around center via precomputed frames.
        if not self._icon_frames:
            return
        self._icon_frame_idx = (self._icon_frame_idx + 1) % 4
        self._icon_view.setImage_(self._icon_frames[self._icon_frame_idx])

    def window_id(self) -> int:
        """
        Return the global window number (CGWindowID-compatible) for Quartz capture.
        """
        try:
            return int(self._panel.windowNumber())
        except Exception:
            return 0

    def debug_snapshot(self) -> dict:
        """
        Best-effort window diagnostics for debugging Spaces/fullscreen behavior.
        """
        try:
            frame = self._panel.frame()
            (fx, fy), (fw, fh) = frame  # type: ignore[misc]
        except Exception:
            fx = fy = fw = fh = None
        try:
            scr = self._panel.screen()
            sframe = scr.frame() if scr is not None else None
        except Exception:
            sframe = None
        try:
            occl = int(self._panel.occlusionState())  # type: ignore[attr-defined]
        except Exception:
            occl = None
        try:
            key = bool(self._panel.isKeyWindow())
        except Exception:
            key = None
        try:
            main = bool(self._panel.isMainWindow())
        except Exception:
            main = None
        try:
            ignores = bool(self._panel.ignoresMouseEvents())
        except Exception:
            ignores = None
        try:
            visible = bool(self._panel.isVisible())
        except Exception:
            visible = None
        try:
            wn = int(self._panel.windowNumber())
        except Exception:
            wn = None
        try:
            lvl = int(self._panel.level())
        except Exception:
            lvl = None
        try:
            cb = int(self._panel.collectionBehavior())
        except Exception:
            cb = None
        return {
            "windowNumber": wn,
            "isVisible": visible,
            "occlusionState": occl,
            "isKeyWindow": key,
            "isMainWindow": main,
            "ignoresMouseEvents": ignores,
            "level": lvl,
            "collectionBehavior": cb,
            "frame": [fx, fy, fw, fh],
            "screenFrame": str(sframe) if sframe is not None else None,
        }

    def update(self, **kwargs) -> None:
        """
        Update overlay state fields. Accepts keys from ReplayOverlayState.
        """
        for k, v in kwargs.items():
            if hasattr(self._state, k):
                setattr(self._state, k, v)

        if self._state.subtask_idx is not None and self._state.subtask_total is not None:
            subtask_prefix = f"{self._state.subtask_idx + 1}/{self._state.subtask_total} "
        elif self._state.subtask_idx is not None:
            subtask_prefix = f"{self._state.subtask_idx + 1} "
        else:
            subtask_prefix = ""

        st = (subtask_prefix + (self._state.subtask_text or "")).strip() or "(waiting)"
        pa = (self._state.predicted_action or "").strip() or "(waiting)"
        self._subtask.setStringValue_(f"Subtask: {st}")
        self._pred.setStringValue_(f"Action: {pa}")
