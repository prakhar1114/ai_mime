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
import os
import subprocess
from ai_mime.app_data import get_managed_browser_harness_path, workflow_runtime_env


_EXPANDED_WIDTH = 380.0
_MIN_EXPANDED_WIDTH = 280.0
_MIN_EXPANDED_HEIGHT = 90.0
_MAX_EXPANDED_HEIGHT = 280.0
_SCREEN_MARGIN = 12.0
_RIGHT_EDGE_MARGIN = 0.0
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
                elif action == "close":
                    self._overlay.close()
                elif action == "show_chat":
                    self._overlay._handle_show_chat()
                elif action == "interrupt":
                    self._overlay._handle_interrupt()
                elif action == "resize":
                    height = body.get("height", 0)
                    self._overlay._clamp_expanded_frame(float(height))
                elif action == "maximize":
                    self._overlay.maximize()
                elif action == "permission_decision":
                    request_id = body.get("request_id")
                    decision = body.get("decision")
                    if request_id and decision:
                        self._overlay._handle_permission_decision(request_id, decision)
        except Exception as e:
            print(f"Error handling JS message: {e}")

class ConversationOverlay:
    """
    Floating, always-on-top, resizable HUD overlay indicating agent activity, powered by WebKit.
    """

    def __init__(self, port: int, task_id: str, mode: str, status: str = None, needs_input: bool = False) -> None:
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
            self._panel.setHasShadow_(False)
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

        title_text = "AI Agent"
        if mode == "build_skill_chat":
            title_text = "AI Mime: Skill Builder"
        elif mode == "replay_execution":
            title_text = "AI Mime: Replay Agent"

        status_text = status if status is not None else "Initializing..."

        state_dict = {"title": title_text, "mode": "maximized", "status": status_text, "needs_input": needs_input}
        state_json = json.dumps(json.dumps(state_dict))
        injected_html = OVERLAY_HTML.replace("</body>", f"<script>updateOverlayState({state_json});</script></body>")

        self._webview.loadHTMLString_baseURL_(injected_html, None)
        self._panel.setContentView_(self._webview)

        self._clamp_expanded_frame()
        self.hide()

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

            self._panel.setMinSize_(AppKit.NSMakeSize(32.0, 32.0))
            self._panel.setMaxSize_(AppKit.NSMakeSize(32.0, 32.0))

            style = AppKit.NSWindowStyleMaskBorderless | AppKit.NSWindowStyleMaskNonactivatingPanel
            self._panel.setStyleMask_(style)

            sx, sy, sw, sh = active_screen_visible_frame()
            mini_w, mini_h = 32.0, 32.0
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

    def update_tool(self, tool_name: str, tool_input: dict = None) -> None:
        try:
            cleaned_tool = tool_name.strip() if tool_name else ""
            self._push_state({"tool": cleaned_tool, "tool_input": tool_input or {}})
        except Exception:
            pass

    def update_status(self, status: str, needs_input: bool) -> None:
        try:
            self._push_state({"status": status, "needs_input": needs_input})
        except Exception:
            pass

    def update_permission(self, perm_req: dict) -> None:
        try:
            self._push_state({"permission_request": perm_req})
            if perm_req and self.is_minimized:
                self.maximize()
        except Exception:
            pass

    def _handle_permission_decision(self, request_id: str, decision: str) -> None:
        def _post_decision():
            try:
                if self.mode == "build_skill_chat":
                    path = f"/api/tasks/{urllib.parse.quote(self.task_id)}/skill-build/permission"
                elif self.mode == "replay_execution":
                    path = f"/api/tasks/{urllib.parse.quote(self.task_id)}/replay-agent/permission"
                else:
                    path = f"/api/tasks/{urllib.parse.quote(self.task_id)}/agent/permission" if self.task_id else "/api/agent/permission"

                url = f"http://127.0.0.1:{self.port}{path}"
                data = json.dumps({"request_id": request_id, "decision": decision}).encode("utf-8")
                req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=3.0) as resp:
                    resp.read()
            except Exception as e:
                print(f"Error submitting permission decision: {e}")

        # Clear the prompt locally right away to feel snappy
        self._push_state({"permission_request": None})
        threading.Thread(target=_post_decision, daemon=True).start()

    def _handle_show_chat(self) -> None:
        try:
            if self.mode == "build_skill_chat":
                path_suffix = f"/skill-build/{urllib.parse.quote(self.task_id)}"
            elif self.mode == "replay_execution":
                path_suffix = f"/replay/{urllib.parse.quote(self.task_id)}"
            else:
                path_suffix = f"/agent"
            focus_browser_tab(self.port, path_suffix)
            self.close()
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

        bh_path = get_managed_browser_harness_path()
        env = os.environ.copy()
        env.update(workflow_runtime_env())

        script = f"""
import sys
for t in list_tabs():
    if {repr(target_path)} in t.get("url", ""):
        cdp("Target.activateTarget", targetId=t["targetId"])
        sys.exit(0)
sys.exit(1)
"""
        found = False
        try:
            result = subprocess.run(
                [str(bh_path), "-c", script],
                env=env,
                capture_output=True,
                text=True,
                timeout=3.0,
            )
            found = (result.returncode == 0)
        except subprocess.TimeoutExpired:
            print("browser-harness timed out")
        except Exception as e:
            print(f"browser-harness error: {e}")

        if found:
            activate_script = '''
            tell application "System Events"
                if exists (process "Google Chrome") then
                    set frontmost of process "Google Chrome" to true
                end if
                if exists (process "Brave Browser") then
                    set frontmost of process "Brave Browser" to true
                end if
            end tell
            '''
            script_obj = Foundation.NSAppleScript.alloc().initWithSource_(activate_script)
            if script_obj:
                script_obj.executeAndReturnError_(None)
        else:
            webbrowser.open(url)
    except Exception as e:
        print(f"Error focusing browser tab: {e}")


class AutomationOverlayTimerHandler(AppKit.NSObject):  # type: ignore[misc]
    def autoClose_(self, timer):  # noqa: N802 - ObjC selector
        try:
            self._overlay.close()  # type: ignore[attr-defined]
        except Exception:
            pass

class AutomationOverlayMessageHandler(AppKit.NSObject):  # type: ignore[misc]
    def setOverlay_(self, overlay):
        self._overlay = overlay

    def userContentController_didReceiveScriptMessage_(self, controller, message):
        try:
            body = message.body()
            if hasattr(body, "get"):
                msg_type = body.get("type")
                if msg_type == "hide":
                    self._overlay.hide()
                elif msg_type == "minimize":
                    self._overlay.minimize()
                elif msg_type == "maximize":
                    self._overlay.maximize()
                elif msg_type == "interrupt":
                    self._overlay._handle_interrupt()
                elif msg_type == "show_chat":
                    self._overlay._handle_show_chat()
                elif msg_type == "resize":
                    height = body.get("height")
                    if isinstance(height, (int, float)):
                        self._overlay._handle_resize(height)
        except Exception as e:
            print(f"Error handling JS message: {e}")

class AutomationOverlay:
    def __init__(self, port: int, task_id: str, app_command_queue: Any = None):
        self.port = port
        self.task_id = task_id
        self.is_minimized = False
        self._last_state: dict[str, Any] = {"status": "running", "mode": "maximized"}
        self._timer_handler = AutomationOverlayTimerHandler.alloc().init()
        self._timer_handler._overlay = self

        # We need to create the panel on the main thread, or we can just initialize it here
        # since it's already instantiated in the main thread (poll_dashboard_command_queue is on main thread)
        self.initUI()

    def initUI(self) -> None:
        try:
            from .automation_overlay_html import AUTOMATION_OVERLAY_HTML
        except ImportError:
            AUTOMATION_OVERLAY_HTML = "<html><body>Error loading HTML</body></html>"

        sx, sy, sw, sh = active_screen_visible_frame()
        self.width = 320.0
        self.height = 160.0
        self.base_x = float(sx + sw - self.width - 20.0)
        self.base_y = float(sy + (sh - self.height) / 2.0)
        rect = AppKit.NSMakeRect(self.base_x, self.base_y, self.width, self.height)

        self._panel = make_overlay_panel(rect, nonactivating=True)
        style = AppKit.NSWindowStyleMaskBorderless | AppKit.NSWindowStyleMaskNonactivatingPanel
        self._panel.setStyleMask_(style)
        self._panel.setLevel_(int(getattr(AppKit, "NSMainMenuWindowLevel", 24)) + 2)
        self._panel.setCollectionBehavior_(
            int(getattr(AppKit, "NSWindowCollectionBehaviorCanJoinAllSpaces", 1)) |
            int(getattr(AppKit, "NSWindowCollectionBehaviorFullScreenAuxiliary", 256))
        )
        self._panel.setOpaque_(False)
        self._panel.setBackgroundColor_(AppKit.NSColor.clearColor())
        self._panel.setHasShadow_(False)
        self._panel.setMovableByWindowBackground_(True)

        config = WebKit.WKWebViewConfiguration.alloc().init()
        config.preferences().setValue_forKey_(True, "developerExtrasEnabled")

        user_content = WebKit.WKUserContentController.alloc().init()
        self._message_handler = AutomationOverlayMessageHandler.alloc().init()
        self._message_handler.setOverlay_(self)
        user_content.addScriptMessageHandler_name_(self._message_handler, "overlay")
        config.setUserContentController_(user_content)

        self._webview = WebKit.WKWebView.alloc().initWithFrame_configuration_(
            self._panel.contentView().bounds(), config
        )
        self._webview.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
        self._webview.setValue_forKey_(False, "drawsBackground")
        self._panel.contentView().addSubview_(self._webview)

        state_json = json.dumps(json.dumps(self._last_state))
        injected_html = AUTOMATION_OVERLAY_HTML.replace("</body>", f"<script>updateOverlayState({state_json});</script></body>")

        self._webview.loadHTMLString_baseURL_(injected_html, None)
        self.show()

    def _push_state(self, state_dict: dict) -> None:
        try:
            self._last_state.update(state_dict)
            state_json = json.dumps(self._last_state)
            script = f"updateOverlayState({json.dumps(state_json)});"
            self._webview.evaluateJavaScript_completionHandler_(script, None)
        except Exception as e:
            print(f"Error evaluating JS: {e}")

    def show(self) -> None:
        try:
            self._panel.orderFrontRegardless()
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

    def minimize(self) -> None:
        if self.is_minimized:
            return
        self.is_minimized = True
        self._push_state({"mode": "minimized"})

        current_frame = self._panel.frame()
        self._expanded_size = current_frame.size

        sx, sy, sw, sh = active_screen_visible_frame()
        mini_w, mini_h = 32.0, 32.0
        x = float(sx + sw - mini_w - 20.0)
        y = float(sy + (sh - mini_h) / 2.0)

        self._panel.setFrame_display_animate_(AppKit.NSMakeRect(x, y, mini_w, mini_h), True, True)

    def maximize(self) -> None:
        if not self.is_minimized:
            return
        self.is_minimized = False
        self._push_state({"mode": "maximized"})

        sx, sy, sw, sh = active_screen_visible_frame()
        h = float(self._expanded_size.height if hasattr(self, "_expanded_size") else self.height)
        w = float(self._expanded_size.width if hasattr(self, "_expanded_size") else self.width)

        x = float(sx + sw - w - 20.0)
        y = float(sy + (sh - h) / 2.0)

        self._panel.setFrame_display_animate_(AppKit.NSMakeRect(x, y, w, h), True, True)

    def update_status(self, status: str) -> None:
        try:
            self._push_state({"status": status})
            if status in ("success", "failed"):
                AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                    3.0,
                    self._timer_handler,
                    "autoClose:",
                    None,
                    False,
                )
                if status == "failed":
                    self._handle_show_chat()
        except Exception as e:
            print(f"Error updating automation status: {e}")

    def update_text(self, text: str) -> None:
        try:
            cleaned = text.strip() if text else ""
            if not cleaned:
                return

            try:
                data = json.loads(cleaned)
                if isinstance(data, dict) and "message" in data:
                    cleaned = str(data["message"]).strip()
            except Exception:
                pass

            if cleaned:
                self._push_state({"message": cleaned})
        except Exception:
            pass

    def _handle_interrupt(self) -> None:
        try:
            self._push_state({"message": "Stopping automation...", "stop_disabled": True})
        except Exception:
            pass

        def _post_kill():
            try:
                path = f"/api/tasks/{urllib.parse.quote(self.task_id)}/skill/kill"
                url = f"http://127.0.0.1:{self.port}{path}"
                req = urllib.request.Request(url, method="POST")
                with urllib.request.urlopen(req, timeout=3.0) as resp:
                    resp.read()
            except Exception as e:
                print(f"Error killing automation: {e}")
            try:
                path_suffix = f"/replay/{urllib.parse.quote(self.task_id)}"
                focus_browser_tab(self.port, path_suffix)
            except Exception as e:
                pass
            try:
                self.close()
            except Exception:
                pass

        threading.Thread(target=_post_kill, daemon=True).start()

    def _handle_show_chat(self) -> None:
        try:
            path_suffix = f"/replay/{urllib.parse.quote(self.task_id)}"
            focus_browser_tab(self.port, path_suffix)
            self.close()
        except Exception as e:
            print(f"Error focusing chat in browser: {e}")

    def _handle_resize(self, height: float) -> None:
        if self.is_minimized:
            return
        try:
            frame = self._panel.frame()
            current_w = frame.size.width
            current_h = frame.size.height
            new_h = max(32.0, min(height, 400.0))
            if abs(current_h - new_h) > 1.0:
                new_y = frame.origin.y + (current_h - new_h) / 2.0
                new_rect = AppKit.NSMakeRect(frame.origin.x, new_y, current_w, new_h)
                self._panel.setFrame_display_animate_(new_rect, True, False)
        except Exception as e:
            print(f"Error resizing AutomationOverlay: {e}")
