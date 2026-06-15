from __future__ import annotations

"""
Native macOS conversation overlay UI (AppKit NSPanel).
Used during active agent interactions in build_skill_chat or replay_execution modes.
"""

# pyright: reportAttributeAccessIssue=false

import AppKit  # type: ignore[import-not-found]
import WebKit
import json
import objc
import Foundation
import threading
import urllib.request
import urllib.parse
import webbrowser

from ai_mime.overlay.overlay_html import OVERLAY_HTML
from ai_mime.overlay.ui_common import (
    active_screen_visible_frame,
    make_hud_effect_view,
    make_overlay_panel,
    style_small_button,
    title_label,
    sys_font,
)


_EXPANDED_WIDTH = 380.0
_MIN_EXPANDED_WIDTH = 280.0
_MIN_EXPANDED_HEIGHT = 90.0
_MAX_EXPANDED_HEIGHT = 280.0
_SCREEN_MARGIN = 12.0
_RIGHT_EDGE_MARGIN = 6.0
_CONTENT_MARGIN = 10.0
_MESSAGE_MAX_LINES = 4
_TOOL_MAX_LINES = 2


class InteractiveHUDView(AppKit.NSVisualEffectView):  # type: ignore[misc]
    def initWithFrame_(self, frame):
        self = objc.super(InteractiveHUDView, self).initWithFrame_(frame)
        if self:
            self._owner = None
        return self

    def mouseDown_(self, event):
        try:
            if self._owner and self._owner.is_minimized:
                self._owner.maximize()
                return
        except Exception:
            pass
        objc.super(InteractiveHUDView, self).mouseDown_(event)


class WebOverlayMessageHandler(AppKit.NSObject):  # type: ignore[misc]
    def userContentController_didReceiveScriptMessage_(self, userContentController, message):
        try:
            body = message.body()
            if hasattr(body, "get"):
                action = body.get("type")
                if action == "hide":
                    self._overlay.minimize()
                elif action == "show_chat":
                    self._overlay._handle_show_chat()
                elif action == "interrupt":
                    self._overlay._handle_interrupt()
                elif action == "resize":
                    height = body.get("height", 0)
                    self._overlay._clamp_expanded_frame(float(height))
                elif action == "maximize":
                    self._overlay.maximize()
        except Exception as e:
            print(f"Error handling JS message: {e}")

class ConversationOverlay:
    """
    Floating, always-on-top, resizable HUD overlay indicating agent activity, powered by WebKit.
    """

    def __init__(self, port: int, task_id: str, mode: str) -> None:
        self.port = port
        self.task_id = task_id
        self.mode = mode

        self.width = self._expanded_width()
        self.height = 220.0
        self.is_minimized = False

        rect = self._expanded_frame(self.height)
        self._panel = make_overlay_panel(rect, nonactivating=True)
        self._panel.setIgnoresMouseEvents_(False)

        try:
            style = AppKit.NSWindowStyleMaskBorderless | AppKit.NSWindowStyleMaskNonactivatingPanel | AppKit.NSWindowStyleMaskResizable
            self._panel.setStyleMask_(style)
            self._apply_expanded_size_limits()
            self._panel.setOpaque_(False)
            self._panel.setBackgroundColor_(AppKit.NSColor.clearColor())
        except Exception:
            pass

        # Set up WebKit
        config = WebKit.WKWebViewConfiguration.alloc().init()
        try:
            config.setValue_forKey_(False, "drawsBackground")
        except Exception:
            pass
        uc = WebKit.WKUserContentController.alloc().init()
        
        self._action_handler = WebOverlayMessageHandler.alloc().init()
        self._action_handler._overlay = self
        
        uc.addScriptMessageHandler_name_(self._action_handler, "overlay")
        config.setUserContentController_(uc)
        
        self._webview = WebKit.WKWebView.alloc().initWithFrame_configuration_(
            AppKit.NSMakeRect(0, 0, self.width, self.height), config
        )
        
        try:
            self._webview.setAutoresizingMask_(
                int(getattr(AppKit, "NSViewWidthSizable", 2)) | int(getattr(AppKit, "NSViewHeightSizable", 16))
            )
            self._webview.setOpaque_(False)
            self._webview.setBackgroundColor_(AppKit.NSColor.clearColor())
            if self._webview.respondsToSelector_("setUnderPageBackgroundColor:"):
                self._webview.setUnderPageBackgroundColor_(AppKit.NSColor.clearColor())
        except Exception:
            pass

        self._webview.loadHTMLString_baseURL_(OVERLAY_HTML, None)
        self._panel.setContentView_(self._webview)

        self._clamp_expanded_frame()
        self.hide()

        # Init state
        title_text = "AI Agent"
        if mode == "build_skill_chat":
            title_text = "AI Mime: Skill Builder"
        elif mode == "replay_execution":
            title_text = "AI Mime: Replay Agent"
        
        self._push_state({"title": title_text, "mode": "maximized"})

    def _expanded_width(self) -> float:
        try:
            _sx, _sy, sw, _sh = active_screen_visible_frame()
        except Exception:
            sw = 1440.0
        available = max(220.0, float(sw) - 2.0 * _SCREEN_MARGIN)
        return max(min(_MIN_EXPANDED_WIDTH, available), min(_EXPANDED_WIDTH, available))

    def _expanded_frame(self, height: float | None = None) -> object:
        try:
            sx, sy, sw, sh = active_screen_visible_frame()
        except Exception:
            sx, sy, sw, sh = 0.0, 0.0, 1440.0, 900.0
        width = self._expanded_width()
        available_height = max(_MIN_EXPANDED_HEIGHT, float(sh) - 2.0 * _SCREEN_MARGIN)
        h = float(height if height is not None else self.height)
        h = max(_MIN_EXPANDED_HEIGHT, min(_MAX_EXPANDED_HEIGHT, h, available_height))
        x = max(float(sx) + _SCREEN_MARGIN, float(sx) + float(sw) - width - _RIGHT_EDGE_MARGIN)
        centered_y = float(sy) + (float(sh) - h) / 2.0
        y = max(float(sy) + _SCREEN_MARGIN, min(float(sy) + float(sh) - h - _SCREEN_MARGIN, centered_y))
        return AppKit.NSMakeRect(x, y, width, h)

    def _apply_expanded_size_limits(self) -> None:
        width = self._expanded_width()
        min_width = min(width, _MIN_EXPANDED_WIDTH)
        self._panel.setMinSize_(AppKit.NSMakeSize(min_width, _MIN_EXPANDED_HEIGHT))
        self._panel.setMaxSize_(AppKit.NSMakeSize(width, _MAX_EXPANDED_HEIGHT))

    def _clamp_expanded_frame(self, height: float | None = None, *, animate: bool = False) -> None:
        if self.is_minimized:
            return
        try:
            self.width = self._expanded_width()
            self._apply_expanded_size_limits()
            frame = self._expanded_frame(height)
            self._webview.setFrameSize_(frame.size)
            self._panel.setFrame_display_animate_(frame, True, animate)
        except Exception as e:
            print(f"Error in _clamp_expanded_frame: {e}")

    def _push_state(self, state_dict: dict) -> None:
        try:
            state_json = json.dumps(state_dict)
            script = f"updateOverlayState({json.dumps(state_json)});"
            self._webview.evaluateJavaScript_completionHandler_(script, None)
        except Exception as e:
            print(f"Error evaluating JS: {e}")

    def show(self) -> None:
        try:
            self._clamp_expanded_frame()
            self._panel.orderFrontRegardless()
        except Exception:
            try:
                self._panel.makeKeyAndOrderFront_(None)
            except Exception:
                pass

    def hide(self) -> None:
        try:
            self._panel.orderOut_(None)
        except Exception:
            pass

    def minimize(self) -> None:
        try:
            if self.is_minimized:
                return
            self.is_minimized = True

            current_frame = self._panel.frame()
            self._expanded_size = current_frame.size

            self._push_state({"mode": "minimized"})

            self._panel.setMinSize_(AppKit.NSMakeSize(32.0, 64.0))
            self._panel.setMaxSize_(AppKit.NSMakeSize(32.0, 64.0))

            style = AppKit.NSWindowStyleMaskBorderless | AppKit.NSWindowStyleMaskNonactivatingPanel
            self._panel.setStyleMask_(style)

            sx, sy, sw, sh = active_screen_visible_frame()
            mini_w, mini_h = 32.0, 64.0
            x = float(sx + sw - mini_w - _RIGHT_EDGE_MARGIN)
            y = float(sy + (sh - mini_h) / 2.0)

            self._panel.setFrame_display_animate_(AppKit.NSMakeRect(x, y, mini_w, mini_h), True, True)
        except Exception as e:
            print(f"Error minimizing conversation overlay: {e}")

    def maximize(self) -> None:
        try:
            if not self.is_minimized:
                return
            self.is_minimized = False

            style = AppKit.NSWindowStyleMaskBorderless | AppKit.NSWindowStyleMaskNonactivatingPanel | AppKit.NSWindowStyleMaskResizable
            self._panel.setStyleMask_(style)

            self._apply_expanded_size_limits()

            h = float(self._expanded_size.height if hasattr(self, "_expanded_size") else self.height)

            self._panel.setFrame_display_animate_(self._expanded_frame(h), True, True)

            self._push_state({"mode": "maximized"})
        except Exception as e:
            print(f"Error maximizing conversation overlay: {e}")

    def close(self) -> None:
        try:
            self._panel.close()
        except Exception:
            pass

    def update_text(self, text: str) -> None:
        try:
            cleaned = text.strip() if text else ""
            self._push_state({"message": cleaned})
        except Exception:
            pass

    def update_tool(self, tool_name: str) -> None:
        try:
            cleaned_tool = tool_name.strip() if tool_name else ""
            self._push_state({"tool": cleaned_tool})
        except Exception:
            pass

    def _handle_show_chat(self) -> None:
        try:
            if self.mode == "build_skill_chat":
                path_suffix = f"/skill-build/{urllib.parse.quote(self.task_id)}"
            elif self.mode == "replay_execution":
                path_suffix = f"/replay/{urllib.parse.quote(self.task_id)}"
            else:
                path_suffix = f"/agent"
            focus_browser_tab(self.port, path_suffix)
        except Exception as e:
            print(f"Error focusing chat in browser: {e}")

    def _handle_interrupt(self) -> None:
        try:
            self._push_state({"tool": "Interrupting agent...", "interrupt_disabled": True})
        except Exception:
            pass

        def _post_interrupt():
            try:
                if self.mode == "build_skill_chat":
                    path = f"/api/tasks/{urllib.parse.quote(self.task_id)}/skill-build/interrupt"
                elif self.mode == "replay_execution":
                    path = f"/api/tasks/{urllib.parse.quote(self.task_id)}/replay-agent/interrupt"
                else:
                    path = f"/api/tasks/{urllib.parse.quote(self.task_id)}/agent/interrupt"

                url = f"http://127.0.0.1:{self.port}{path}"
                req = urllib.request.Request(url, method="POST")
                with urllib.request.urlopen(req, timeout=3.0) as resp:
                    resp.read()
            except Exception as e:
                print(f"Error interrupting agent: {e}")

        threading.Thread(target=_post_interrupt, daemon=True).start()

def focus_browser_tab(port: int, path_suffix: str) -> None:
    try:
        url = f"http://127.0.0.1:{port}{path_suffix}"
        parsed = urllib.parse.urlparse(url)
        target_path = parsed.path

        found = False
        for browser in ["Google Chrome", "Brave Browser", "Safari"]:
            # Check if browser app is running first, to avoid launching it if closed
            check_running = f'tell application "System Events" to return (count of (every process whose name is "{browser}")) > 0'
            script_check = Foundation.NSAppleScript.alloc().initWithSource_(check_running)
            success_check, _ = script_check.executeAndReturnError_(None) if script_check else (None, None)
            is_running = bool(success_check.booleanValue()) if success_check else False
            if not is_running:
                continue

            if browser in ("Google Chrome", "Brave Browser"):
                script_text = f'''
                tell application "{browser}"
                    set found to false
                    repeat with w in windows
                        set tabIndex to 1
                        repeat with t in tabs of w
                            if URL of t contains "{target_path}" then
                                set active tab index of w to tabIndex
                                set index of w to 1
                                activate
                                set found to true
                                exit repeat
                            end if
                            set tabIndex to tabIndex + 1
                        end repeat
                        if found then exit repeat
                    end repeat
                    return found
                end tell
                '''
            else:  # Safari
                script_text = f'''
                tell application "Safari"
                    set found to false
                    repeat with w in windows
                        set tabIndex to 1
                        repeat with t in tabs of w
                            if URL of t contains "{target_path}" then
                                set current tab of w to t
                                set index of w to 1
                                activate
                                set found to true
                                exit repeat
                            end if
                            set tabIndex to tabIndex + 1
                        end repeat
                        if found then exit repeat
                    end repeat
                    return found
                end tell
                '''

            script = Foundation.NSAppleScript.alloc().initWithSource_(script_text)
            success, _ = script.executeAndReturnError_(None) if script else (None, None)
            if success and success.booleanValue():
                found = True
                break

        if not found:
            webbrowser.open(url)
    except Exception as e:
        print(f"Error focusing browser tab: {e}")


class AutomationIndicatorView(AppKit.NSView):  # type: ignore[misc]
    def initWithFrame_(self, frame):
        self = objc.super(AutomationIndicatorView, self).initWithFrame_(frame)
        if self:
            self._owner = None
            self._state = "running"  # "running", "success", "failed"

            # Large Spinning Progress Indicator (64x64, centered in 72x72)
            self._spinner = AppKit.NSProgressIndicator.alloc().initWithFrame_(
                AppKit.NSMakeRect(4.0, 4.0, 64.0, 64.0)
            )
            self._spinner.setStyle_(AppKit.NSProgressIndicatorStyleSpinning)
            try:
                self._spinner.setControlSize_(AppKit.NSControlSizeLarge)
            except Exception:
                pass
            self._spinner.startAnimation_(None)
            self.addSubview_(self._spinner)
        return self

    def setStatus_(self, status: str):
        self._state = status
        if status in ("success", "failed"):
            self._spinner.stopAnimation_(None)
            self._spinner.setHidden_(True)
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        AppKit.NSColor.clearColor().set()
        AppKit.NSRectFill(self.bounds())

        if self._state == "success":
            # Draw a green checkmark/tick centered in 72x72
            AppKit.NSColor.colorWithRed_green_blue_alpha_(0.15, 0.8, 0.25, 1.0).set()
            path = AppKit.NSBezierPath.bezierPath()
            path.setLineWidth_(6.0)
            path.setLineCapStyle_(AppKit.NSLineCapStyleRound)
            path.moveToPoint_(AppKit.NSMakePoint(18.0, 32.0))
            path.lineToPoint_(AppKit.NSMakePoint(30.0, 20.0))
            path.lineToPoint_(AppKit.NSMakePoint(54.0, 52.0))
            path.stroke()
        elif self._state == "failed":
            # Draw a red cross/error indicator centered in 72x72
            AppKit.NSColor.colorWithRed_green_blue_alpha_(0.9, 0.25, 0.2, 1.0).set()
            path = AppKit.NSBezierPath.bezierPath()
            path.setLineWidth_(6.0)
            path.setLineCapStyle_(AppKit.NSLineCapStyleRound)
            # Line 1
            path.moveToPoint_(AppKit.NSMakePoint(20.0, 20.0))
            path.lineToPoint_(AppKit.NSMakePoint(52.0, 52.0))
            # Line 2
            path.moveToPoint_(AppKit.NSMakePoint(52.0, 20.0))
            path.lineToPoint_(AppKit.NSMakePoint(20.0, 52.0))
            path.stroke()

    def mouseDown_(self, event):
        try:
            if self._owner:
                self._owner.on_clicked()
        except Exception:
            pass
        objc.super(AutomationIndicatorView, self).mouseDown_(event)


class AutomationOverlayActionHandler(AppKit.NSObject):  # type: ignore[misc]
    def autoClose_(self, timer):  # noqa: N802 - ObjC selector
        try:
            self._overlay.close()  # type: ignore[attr-defined]
        except Exception:
            pass


class AutomationOverlay:
    """
    Compact, non-resizable circular HUD indicating automation/replay execution is running.
    Clicking it opens/focuses the replay task page in the browser.
    """

    def __init__(self, port: int, task_id: str) -> None:
        self.port = port
        self.task_id = task_id
        self.is_minimized = True  # Mock minimized so background view acts as click trigger

        sx, sy, sw, sh = active_screen_visible_frame()
        self.width = 72.0
        self.height = 72.0
        x = float(sx + sw - self.width)
        y = float(sy + (sh - self.height) / 2.0)
        rect = AppKit.NSMakeRect(x, y, self.width, self.height)

        self._panel = make_overlay_panel(rect, nonactivating=True)
        style = AppKit.NSWindowStyleMaskBorderless | AppKit.NSWindowStyleMaskNonactivatingPanel
        self._panel.setStyleMask_(style)

        # Content View
        self._content = InteractiveHUDView.alloc().initWithFrame_(
            AppKit.NSMakeRect(0, 0, self.width, self.height)
        )
        self._content._owner = self
        self._panel.setContentView_(self._content)

        # Indicator View
        self._indicator = AutomationIndicatorView.alloc().initWithFrame_(
            AppKit.NSMakeRect(0, 0, self.width, self.height)
        )
        self._indicator._owner = self
        self._content.addSubview_(self._indicator)

        self._action_handler = AutomationOverlayActionHandler.alloc().init()
        self._action_handler._overlay = self

        self.show()

    def show(self) -> None:
        try:
            self._panel.orderFrontRegardless()
        except Exception:
            try:
                self._panel.makeKeyAndOrderFront_(None)
            except Exception:
                pass

    def hide(self) -> None:
        try:
            self._panel.orderOut_(None)
        except Exception:
            pass

    def close(self) -> None:
        try:
            self._panel.close()
        except Exception:
            pass

    def update_status(self, status: str) -> None:
        try:
            self._indicator.setStatus_(status)
            if status in ("success", "failed"):
                AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                    2.0,
                    self._action_handler,
                    "autoClose:",
                    None,
                    False,
                )
        except Exception as e:
            print(f"Error updating automation status: {e}")

    def on_clicked(self) -> None:
        try:
            path_suffix = f"/replay/{urllib.parse.quote(self.task_id)}"
            focus_browser_tab(self.port, path_suffix)
            # Dismiss overlay once clicked
            self.close()
        except Exception as e:
            print(f"Error in automation click handler: {e}")
