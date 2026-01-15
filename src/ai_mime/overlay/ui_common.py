from __future__ import annotations

"""
Small shared "design system" for AI Mime overlays (macOS AppKit).

Goal: keep Replay + Recording overlays visually consistent and modern.
"""

# pyright: reportAttributeAccessIssue=false

from typing import Any

import AppKit  # type: ignore[import-not-found]


class KeyablePanel(AppKit.NSPanel):  # type: ignore[misc]
    """
    Borderless panels often refuse key status by default; override so NSTextField can edit.
    """

    def canBecomeKeyWindow(self) -> bool:  # noqa: N802 - ObjC override name
        return True

    def canBecomeMainWindow(self) -> bool:  # noqa: N802 - ObjC override name
        return False


def active_screen_visible_frame() -> tuple[float, float, float, float]:
    """
    Return (sx, sy, sw, sh) for the visible frame of the screen under the mouse cursor.
    Falls back to mainScreen/defaults.
    """
    try:
        mouse = AppKit.NSEvent.mouseLocation()
        mx = float(getattr(mouse, "x", 0.0))
        my = float(getattr(mouse, "y", 0.0))
        for s in (AppKit.NSScreen.screens() or []):
            fr = s.frame()
            (fx, fy), (fw, fh) = fr  # type: ignore[misc]
            if float(fx) <= mx <= float(fx) + float(fw) and float(fy) <= my <= float(fy) + float(fh):
                vf = s.visibleFrame()
                (sx, sy), (sw, sh) = vf  # type: ignore[misc]
                return float(sx), float(sy), float(sw), float(sh)
        s = AppKit.NSScreen.mainScreen()
        vf = s.visibleFrame() if s is not None else ((0, 0), (1440, 900))
        (sx, sy), (sw, sh) = vf  # type: ignore[misc]
        return float(sx), float(sy), float(sw), float(sh)
    except Exception:
        return 0.0, 0.0, 1440.0, 900.0


def sys_font(size: float, weight: float | None = None) -> Any:
    try:
        if weight is not None:
            return AppKit.NSFont.systemFontOfSize_weight_(float(size), float(weight))  # type: ignore[attr-defined]
    except Exception:
        pass
    return AppKit.NSFont.systemFontOfSize_(float(size))


def font_weights() -> tuple[float, float]:
    w_semibold = float(getattr(AppKit, "NSFontWeightSemibold", 0.6))  # type: ignore[attr-defined]
    w_medium = float(getattr(AppKit, "NSFontWeightMedium", 0.23))  # type: ignore[attr-defined]
    return w_semibold, w_medium


def theme_colors() -> dict[str, Any]:
    try:
        return {
            "title": AppKit.NSColor.labelColor(),
            "secondary": AppKit.NSColor.secondaryLabelColor(),
            "body": AppKit.NSColor.labelColor(),
        }
    except Exception:
        return {"title": None, "secondary": None, "body": None}


def style_small_button(btn: Any) -> Any:
    w_semibold, w_medium = font_weights()
    try:
        btn.setBezelStyle_(AppKit.NSBezelStyleRounded)  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        btn.setControlSize_(AppKit.NSControlSizeSmall)  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        btn.setFont_(sys_font(11.0, w_medium))
    except Exception:
        pass
    return btn


def make_hud_effect_view(width: float, height: float) -> Any:
    effect = AppKit.NSVisualEffectView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, float(width), float(height)))
    try:
        effect.setAutoresizingMask_(  # type: ignore[attr-defined]
            int(getattr(AppKit, "NSViewWidthSizable", 2)) | int(getattr(AppKit, "NSViewHeightSizable", 16))  # type: ignore[attr-defined]
        )
    except Exception:
        pass
    try:
        effect.setBlendingMode_(AppKit.NSVisualEffectBlendingModeBehindWindow)  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
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
    return effect


def make_overlay_panel(rect: Any, *, nonactivating: bool = True) -> Any:
    """
    Create a borderless overlay NSPanel.

    IMPORTANT: For reliable behavior across fullscreen Spaces, we prefer the
    NSWindowStyleMaskNonactivatingPanel style. If nonactivating is False, we still
    create a keyable panel (for editable text fields) but keep the non-activating style.
    """
    style = AppKit.NSWindowStyleMaskBorderless  # type: ignore[attr-defined]
    try:
        # Non-activating panels behave better across fullscreen Spaces.
        style = style | AppKit.NSWindowStyleMaskNonactivatingPanel  # type: ignore[attr-defined]
    except Exception:
        pass

    panel_cls = AppKit.NSPanel if nonactivating else KeyablePanel
    panel = panel_cls.alloc().initWithContentRect_styleMask_backing_defer_(
        rect,
        style,
        AppKit.NSBackingStoreBuffered,  # type: ignore[attr-defined]
        False,
    )
    panel.setOpaque_(False)
    panel.setHasShadow_(True)
    try:
        panel.setBackgroundColor_(AppKit.NSColor.clearColor())
    except Exception:
        pass

    try:
        panel.setLevel_(AppKit.NSScreenSaverWindowLevel)  # type: ignore[attr-defined]
    except Exception:
        try:
            panel.setLevel_(AppKit.NSStatusWindowLevel)  # type: ignore[attr-defined]
        except Exception:
            panel.setLevel_(AppKit.NSFloatingWindowLevel)  # type: ignore[attr-defined]
    panel.setHidesOnDeactivate_(False)
    try:
        panel.setCollectionBehavior_(
            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces  # type: ignore[attr-defined]
            | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary  # type: ignore[attr-defined]
        )
    except Exception:
        pass
    try:
        panel.setCollectionBehavior_(
            int(panel.collectionBehavior())
            | int(getattr(AppKit, "NSWindowCollectionBehaviorMoveToActiveSpace", 0))  # type: ignore[attr-defined]
        )
    except Exception:
        pass
    try:
        panel.setIgnoresMouseEvents_(False)
    except Exception:
        pass
    return panel


def title_label(text: str) -> Any:
    colors = theme_colors()
    w_semibold, _w_medium = font_weights()
    tf = AppKit.NSTextField.labelWithString_(str(text))
    try:
        tf.setFont_(sys_font(13.0, w_semibold))
    except Exception:
        pass
    try:
        if colors.get("title") is not None:
            tf.setTextColor_(colors["title"])
    except Exception:
        pass
    return tf
