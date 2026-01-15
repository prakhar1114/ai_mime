from __future__ import annotations

"""
Lightweight macOS recording overlay UI (AppKit NSPanel).

Runs in the existing rumps/AppKit UI process.

This overlay is intended to be excluded from screenshots by capturing screen
content *below* its window id (see ScreenshotRecorder.capture(exclude_window_id=...)).
"""

# pyright: reportAttributeAccessIssue=false

import time
from dataclasses import dataclass
from typing import Any

import AppKit  # type: ignore[import-not-found]

from ai_mime.overlay.ui_common import (
    active_screen_visible_frame,
    make_hud_effect_view,
    make_overlay_panel,
    style_small_button,
    title_label,
)


@dataclass
class RecordingOverlayState:
    mode: str = "collapsed"  # collapsed | details | extract
    req_id: float | None = None


class RecordingOverlayActionHandler(AppKit.NSObject):  # type: ignore[misc]
    # ObjC selector methods; naming follows PyObjC conventions.
    def addDetails_(self, sender):  # noqa: N802
        self._overlay._begin_refine("details")  # type: ignore[attr-defined]

    def extractData_(self, sender):  # noqa: N802
        self._overlay._begin_refine("extract")  # type: ignore[attr-defined]

    def cancelRecording_(self, sender):  # noqa: N802
        self._overlay._cancel_recording()  # type: ignore[attr-defined]

    def submit_(self, sender):  # noqa: N802
        self._overlay._submit_form()  # type: ignore[attr-defined]

    def cancel_(self, sender):  # noqa: N802
        self._overlay._cancel_form()  # type: ignore[attr-defined]

    def sync_(self, sender):  # noqa: N802
        # Periodic reposition to active screen.
        try:
            self._overlay._sync_position()  # type: ignore[attr-defined]
        except Exception:
            pass


class RecordingOverlay:
    """
    Minimal always-on-top overlay:
      - Collapsed: two buttons (Add more details / Extract Data)
      - Expanded: inline form + Submit/Cancel
    """

    def __init__(self, *, refine_cmd_q: Any, refine_resp_q: Any, on_cancel_recording: Any) -> None:
        self._cmd_q = refine_cmd_q
        self._resp_q = refine_resp_q
        self._on_cancel_recording = on_cancel_recording
        self._state = RecordingOverlayState()
        self._action_handler = RecordingOverlayActionHandler.alloc().init()
        self._action_handler._overlay = self  # type: ignore[attr-defined]
        self._sync_timer = None

        # Window sizing / placement
        # Width is dynamic: collapsed is compact; expanded grows up to a max.
        self._max_width = 560.0
        self._collapsed_width = 360.0
        self._height_collapsed = 86.0
        self._height_details = 140.0
        self._height_extract = 190.0

        margin = 16.0
        self._margin = float(margin)
        # Initial placement; will be kept in sync via a timer while visible.
        sx, sy, sw, sh = active_screen_visible_frame()
        x = float(sx + sw - self._collapsed_width - self._margin)
        y = float(sy + (sh - self._height_collapsed) / 2.0)
        rect = AppKit.NSMakeRect(x, y, self._collapsed_width, self._height_collapsed)

        # Recording overlay needs to be able to become key so text inputs are editable,
        # but we keep non-activating panel style for better fullscreen Spaces behavior.
        self._panel = make_overlay_panel(rect, nonactivating=False)
        # Create content at max width; it will autoresize with the panel frame.
        self._content = make_hud_effect_view(self._max_width, self._height_extract)
        self._panel.setContentView_(self._content)

        # Main stack (we rebuild contents per mode for simplicity)
        self._stack = AppKit.NSStackView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, self._collapsed_width, self._height_collapsed))
        self._stack.setOrientation_(AppKit.NSUserInterfaceLayoutOrientationVertical)  # type: ignore[attr-defined]
        self._stack.setAlignment_(AppKit.NSLayoutAttributeLeading)  # type: ignore[attr-defined]
        self._stack.setSpacing_(6.0)
        try:
            insets = AppKit.NSMakeEdgeInsets(0, 0, 0, 0)  # type: ignore[attr-defined]
            self._stack.setEdgeInsets_(insets)
        except Exception:
            pass

        self._content.addSubview_(self._stack)
        try:
            m = 6.0
            self._stack.setTranslatesAutoresizingMaskIntoConstraints_(False)
            AppKit.NSLayoutConstraint.activateConstraints_(
                [
                    self._stack.leadingAnchor().constraintEqualToAnchor_constant_(self._content.leadingAnchor(), m),
                    self._stack.trailingAnchor().constraintEqualToAnchor_constant_(self._content.trailingAnchor(), -m),
                    self._stack.topAnchor().constraintEqualToAnchor_constant_(self._content.topAnchor(), m),
                    self._stack.bottomAnchor().constraintEqualToAnchor_constant_(self._content.bottomAnchor(), -m),
                ]
            )
        except Exception:
            pass

        # Inputs (created once; inserted/removed based on mode)
        self._details_field = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 240, 24))
        self._details_field.setPlaceholderString_("Details (natural language)")
        try:
            self._details_field.setEditable_(True)
            self._details_field.setSelectable_(True)
            self._details_field.setBezeled_(True)
        except Exception:
            pass

        self._query_field = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 240, 24))
        self._query_field.setPlaceholderString_("Query (what to extract from the page)")
        try:
            self._query_field.setEditable_(True)
            self._query_field.setSelectable_(True)
            self._query_field.setBezeled_(True)
        except Exception:
            pass

        self._values_field = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 240, 24))
        self._values_field.setPlaceholderString_("Values (what you extracted)")
        try:
            self._values_field.setEditable_(True)
            self._values_field.setSelectable_(True)
            self._values_field.setBezeled_(True)
        except Exception:
            pass

        self._render_collapsed()
        self.hide()

    def window_id(self) -> int:
        try:
            return int(self._panel.windowNumber())
        except Exception:
            return 0

    def show(self) -> None:
        # Keep pinned to the active screen while visible.
        if self._sync_timer is None:
            try:
                self._sync_timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                    0.5,
                    self._action_handler,
                    "sync:",
                    None,
                    True,
                )
            except Exception:
                self._sync_timer = None
        self._sync_position()
        try:
            self._panel.orderFrontRegardless()
        except Exception:
            try:
                self._panel.makeKeyAndOrderFront_(None)
            except Exception:
                pass

    def hide(self) -> None:
        if self._sync_timer is not None:
            try:
                self._sync_timer.invalidate()
            except Exception:
                pass
            self._sync_timer = None
        try:
            self._panel.orderOut_(None)
        except Exception:
            pass

    def close(self) -> None:
        if self._sync_timer is not None:
            try:
                self._sync_timer.invalidate()
            except Exception:
                pass
            self._sync_timer = None
        try:
            self._panel.close()
        except Exception:
            pass

    def _sync_position(self) -> None:
        """
        Reposition to the active screen (by mouse location) while keeping current size.
        """
        try:
            fr = self._panel.frame()
            (_x, _y), (w, h) = fr  # type: ignore[misc]
            self._place_on_active_screen(float(w), float(h))
        except Exception:
            pass

    def _place_on_active_screen(self, w: float, h: float) -> None:
        try:
            sx, sy, sw, sh = active_screen_visible_frame()
        except Exception:
            sx, sy, sw, sh = 0.0, 0.0, 1440.0, 900.0
        x = float(sx + sw - float(w) - self._margin)
        y = float(sy + (sh - float(h)) / 2.0)
        try:
            self._panel.setFrame_display_(AppKit.NSMakeRect(x, y, float(w), float(h)), True)
        except Exception:
            pass

    def _clear_stack(self) -> None:
        try:
            arranged = list(self._stack.arrangedSubviews())
        except Exception:
            arranged = []
        for v in arranged:
            try:
                self._stack.removeArrangedSubview_(v)
            except Exception:
                pass
            try:
                v.removeFromSuperview()
            except Exception:
                pass

    def _set_size(self, w: float, h: float) -> None:
        try:
            # Keep it pinned to the active screen right edge and vertically centered.
            self._place_on_active_screen(float(w), float(h))
        except Exception:
            pass

    def _title_label(self, text: str) -> Any:
        return title_label(text)

    def _button(self, title: str, action: str) -> Any:
        b = AppKit.NSButton.buttonWithTitle_target_action_(title, self._action_handler, action)  # type: ignore[attr-defined]
        return style_small_button(b)

    def _render_collapsed(self) -> None:
        self._state.mode = "collapsed"
        self._state.req_id = None
        self._clear_stack()

        self._set_size(self._collapsed_width, self._height_collapsed)

        title_row = AppKit.NSStackView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, self._collapsed_width, 22))
        title_row.setOrientation_(AppKit.NSUserInterfaceLayoutOrientationHorizontal)  # type: ignore[attr-defined]
        title_row.setAlignment_(AppKit.NSLayoutAttributeCenterY)  # type: ignore[attr-defined]
        title_row.setSpacing_(8.0)
        title_row.addArrangedSubview_(self._title_label("Recording"))
        spacer = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 1, 1))
        title_row.addArrangedSubview_(spacer)
        try:
            spacer.setContentHuggingPriority_forOrientation_(1, AppKit.NSLayoutConstraintOrientationHorizontal)  # type: ignore[attr-defined]
        except Exception:
            pass
        title_row.addArrangedSubview_(self._button("Cancel recording", "cancelRecording:"))
        self._stack.addArrangedSubview_(title_row)

        row = AppKit.NSStackView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, self._collapsed_width, 28))
        row.setOrientation_(AppKit.NSUserInterfaceLayoutOrientationHorizontal)  # type: ignore[attr-defined]
        row.setAlignment_(AppKit.NSLayoutAttributeCenterY)  # type: ignore[attr-defined]
        row.setSpacing_(10.0)

        row.addArrangedSubview_(self._button("Add more details", "addDetails:"))
        row.addArrangedSubview_(self._button("Extract Data", "extractData:"))

        self._stack.addArrangedSubview_(row)

    def _begin_refine(self, kind: str) -> None:
        if self._state.mode != "collapsed":
            return

        req_id = float(time.time())
        self._state.req_id = req_id
        try:
            self._cmd_q.put({"type": "begin_refine", "kind": str(kind), "req_id": req_id})
        except Exception:
            # If we can't talk to recorder, just stay collapsed.
            self._state.req_id = None
            return

        if kind == "details":
            self._render_details(req_id)
        else:
            self._render_extract(req_id)

        # Make the panel key so the text field can accept input.
        try:
            AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            self._panel.makeKeyAndOrderFront_(None)
        except Exception:
            pass

    def _render_details(self, req_id: float) -> None:
        self._state.mode = "details"
        self._clear_stack()
        self._set_size(self._max_width, self._height_details)

        self._stack.addArrangedSubview_(self._header_with_cancel("Add more details"))
        self._details_field.setStringValue_("")
        self._stack.addArrangedSubview_(self._details_field)
        self._stack.addArrangedSubview_(self._submit_cancel_row())

        try:
            self._panel.makeFirstResponder_(self._details_field)
        except Exception:
            pass

    def _render_extract(self, req_id: float) -> None:
        self._state.mode = "extract"
        self._clear_stack()
        self._set_size(self._max_width, self._height_extract)

        self._stack.addArrangedSubview_(self._header_with_cancel("Extract Data"))
        self._query_field.setStringValue_("")
        self._values_field.setStringValue_("")
        self._stack.addArrangedSubview_(self._query_field)
        self._stack.addArrangedSubview_(self._values_field)
        self._stack.addArrangedSubview_(self._submit_cancel_row())

        try:
            self._panel.makeFirstResponder_(self._query_field)
        except Exception:
            pass

    def _submit_cancel_row(self) -> Any:
        row = AppKit.NSStackView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, self._max_width, 28))
        row.setOrientation_(AppKit.NSUserInterfaceLayoutOrientationHorizontal)  # type: ignore[attr-defined]
        row.setAlignment_(AppKit.NSLayoutAttributeCenterY)  # type: ignore[attr-defined]
        row.setSpacing_(10.0)

        # Spacer to push buttons right.
        spacer = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 1, 1))
        row.addArrangedSubview_(spacer)
        try:
            spacer.setContentHuggingPriority_forOrientation_(1, AppKit.NSLayoutConstraintOrientationHorizontal)  # type: ignore[attr-defined]
        except Exception:
            pass

        row.addArrangedSubview_(self._button("Cancel", "cancel:"))
        row.addArrangedSubview_(self._button("Submit", "submit:"))
        return row

    def _header_with_cancel(self, title: str) -> Any:
        row = AppKit.NSStackView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, self._max_width, 22))
        row.setOrientation_(AppKit.NSUserInterfaceLayoutOrientationHorizontal)  # type: ignore[attr-defined]
        row.setAlignment_(AppKit.NSLayoutAttributeCenterY)  # type: ignore[attr-defined]
        row.setSpacing_(8.0)
        row.addArrangedSubview_(self._title_label(title))
        spacer = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 1, 1))
        row.addArrangedSubview_(spacer)
        try:
            spacer.setContentHuggingPriority_forOrientation_(1, AppKit.NSLayoutConstraintOrientationHorizontal)  # type: ignore[attr-defined]
        except Exception:
            pass
        row.addArrangedSubview_(self._button("Cancel recording", "cancelRecording:"))
        return row

    def _submit_form(self) -> None:
        req_id = self._state.req_id
        if req_id is None:
            self._render_collapsed()
            return

        if self._state.mode == "details":
            text = str(self._details_field.stringValue() or "").strip()
            resp = {"kind": "details", "text": text, "req_id": req_id}
        elif self._state.mode == "extract":
            query = str(self._query_field.stringValue() or "").strip()
            values = str(self._values_field.stringValue() or "").strip()
            resp = {"kind": "extract", "query": query, "values": values, "req_id": req_id}
        else:
            resp = {"kind": "cancel", "req_id": req_id}

        try:
            self._resp_q.put(resp)
        except Exception:
            pass
        self._render_collapsed()

    def _cancel_form(self) -> None:
        req_id = self._state.req_id
        if req_id is not None:
            try:
                self._resp_q.put({"kind": "cancel", "req_id": req_id})
            except Exception:
                pass
        self._render_collapsed()

    def _cancel_recording(self) -> None:
        try:
            cb = self._on_cancel_recording
            if cb is not None:
                cb()
        except Exception:
            pass
