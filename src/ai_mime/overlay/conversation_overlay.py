from __future__ import annotations

"""
Native macOS conversation overlay UI (AppKit NSPanel).
Used during active agent interactions in build_skill_chat or replay_execution modes.
"""

# pyright: reportAttributeAccessIssue=false

import AppKit  # type: ignore[import-not-found]
import objc
import Foundation
import threading
import urllib.request
import urllib.parse
import webbrowser

from ai_mime.overlay.ui_common import (
    active_screen_visible_frame,
    make_hud_effect_view,
    make_overlay_panel,
    style_small_button,
    title_label,
    sys_font,
)


class PulsingDotView(AppKit.NSView):  # type: ignore[misc]
    """
    Custom NSView that draws a green circle and pulses its opacity via an NSTimer.
    """

    def initWithFrame_(self, frame):
        self = objc.super(PulsingDotView, self).initWithFrame_(frame)
        if self:
            self._owner = None
            self._color = AppKit.NSColor.colorWithRed_green_blue_alpha_(0.15, 0.8, 0.25, 1.0)
            self._alpha = 1.0
            self._increasing = False
            self._timer = None
            try:
                self._timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                    0.08,
                    self,
                    "pulse:",
                    None,
                    True,
                )
            except Exception:
                pass
        return self

    def setColor_(self, color):
        self._color = color
        self.setNeedsDisplay_(True)

    def pulse_(self, sender):
        if self._increasing:
            self._alpha += 0.08
            if self._alpha >= 1.0:
                self._alpha = 1.0
                self._increasing = False
        else:
            self._alpha -= 0.08
            if self._alpha <= 0.3:
                self._alpha = 0.3
                self._increasing = True
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        AppKit.NSColor.clearColor().set()
        AppKit.NSRectFill(self.bounds())

        path = AppKit.NSBezierPath.bezierPathWithOvalInRect_(self.bounds())
        color_with_alpha = self._color.colorWithAlphaComponent_(self._alpha)
        color_with_alpha.set()
        path.fill()

    def mouseDown_(self, event):
        try:
            if hasattr(self, "_owner") and self._owner and self._owner.is_minimized:
                self._owner.maximize()
                return
        except Exception:
            pass
        objc.super(PulsingDotView, self).mouseDown_(event)

    def close(self):
        if self._timer:
            try:
                self._timer.invalidate()
            except Exception:
                pass
            self._timer = None


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


class ConversationOverlayActionHandler(AppKit.NSObject):  # type: ignore[misc]
    def interrupt_(self, sender):  # noqa: N802 - ObjC selector
        try:
            self._overlay._handle_interrupt()  # type: ignore[attr-defined]
        except Exception:
            pass

    def showChat_(self, sender):  # noqa: N802 - ObjC selector
        try:
            self._overlay._handle_show_chat()  # type: ignore[attr-defined]
        except Exception:
            pass

    def hide_(self, sender):  # noqa: N802 - ObjC selector
        try:
            self._overlay.minimize()  # type: ignore[attr-defined]
        except Exception:
            pass


class ConversationOverlay:
    """
    Floating, always-on-top, resizable HUD overlay indicating agent activity.
    """

    def __init__(self, port: int, task_id: str, mode: str) -> None:
        self.port = port
        self.task_id = task_id
        self.mode = mode

        self.width = 380.0
        self.height = 220.0
        self.is_minimized = False

        # Center vertically on the right edge of the active screen
        try:
            sx, sy, sw, sh = active_screen_visible_frame()
        except Exception:
            sx, sy, sw, sh = 0.0, 0.0, 1440.0, 900.0

        x = float(sx + sw - self.width)
        y = float(sy + (sh - self.height) / 2.0)
        rect = AppKit.NSMakeRect(x, y, self.width, self.height)

        # Make the panel borderless and always-on-top, non-activating
        self._panel = make_overlay_panel(rect, nonactivating=True)

        # Apply resizable style mask and limits
        try:
            style = AppKit.NSWindowStyleMaskBorderless | AppKit.NSWindowStyleMaskNonactivatingPanel | AppKit.NSWindowStyleMaskResizable
            self._panel.setStyleMask_(style)
            self._panel.setMinSize_(AppKit.NSMakeSize(380.0, 90.0))
            self._panel.setMaxSize_(AppKit.NSMakeSize(380.0, 280.0))
        except Exception:
            pass

        # Create content view using custom InteractiveHUDView class
        self._content = InteractiveHUDView.alloc().initWithFrame_(
            AppKit.NSMakeRect(0, 0, self.width, self.height)
        )
        try:
            self._content.setAutoresizingMask_(
                int(getattr(AppKit, "NSViewWidthSizable", 2)) | int(getattr(AppKit, "NSViewHeightSizable", 16))
            )
            self._content.setBlendingMode_(AppKit.NSVisualEffectBlendingModeBehindWindow)
            material = getattr(AppKit, "NSVisualEffectMaterialHUDWindow", None)
            if material is None:
                material = getattr(AppKit, "NSVisualEffectMaterialMenu", None)
            if material is not None:
                self._content.setMaterial_(material)
            self._content.setState_(AppKit.NSVisualEffectStateActive)
        except Exception:
            pass
        self._content._owner = self
        self._panel.setContentView_(self._content)

        # Vertical stack for organizing labels and buttons
        self._stack = AppKit.NSStackView.alloc().initWithFrame_(
            AppKit.NSMakeRect(0, 0, self.width, self.height)
        )
        self._stack.setOrientation_(AppKit.NSUserInterfaceLayoutOrientationVertical)  # type: ignore[attr-defined]
        self._stack.setAlignment_(AppKit.NSLayoutAttributeLeading)  # type: ignore[attr-defined]
        self._stack.setSpacing_(6.0)

        # Title Row: Pulsing Indicator + Mode Name
        self._header_row = AppKit.NSStackView.alloc().initWithFrame_(
            AppKit.NSMakeRect(0, 0, self.width, 24)
        )
        self._header_row.setOrientation_(AppKit.NSUserInterfaceLayoutOrientationHorizontal)  # type: ignore[attr-defined]
        self._header_row.setAlignment_(AppKit.NSLayoutAttributeCenterY)  # type: ignore[attr-defined]
        self._header_row.setSpacing_(8.0)

        # Bright Green Color for active agent indicator
        self._dot = PulsingDotView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 10, 10))
        self._dot._owner = self
        green_color = AppKit.NSColor.colorWithRed_green_blue_alpha_(0.15, 0.8, 0.25, 1.0)
        try:
            self._dot.setColor_(green_color)
        except Exception:
            pass

        title_text = "AI Agent"
        if mode == "build_skill_chat":
            title_text = "AI Mime: Skill Builder"
        elif mode == "replay_execution":
            title_text = "AI Mime: Replay Agent"
        self._title = title_label(title_text)

        self._header_row.addArrangedSubview_(self._dot)
        self._header_row.addArrangedSubview_(self._title)

        # Message Label showing text snippets
        self._message_label = AppKit.NSTextField.labelWithString_("Initializing conversation...")
        try:
            self._message_label.setFont_(sys_font(12.0))
            self._message_label.setTextColor_(AppKit.NSColor.labelColor())
            self._message_label.setMaximumNumberOfLines_(6)
            self._message_label.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)  # type: ignore[attr-defined]
            self._message_label.setTranslatesAutoresizingMaskIntoConstraints_(False)
        except Exception:
            pass

        # Tool Status Label
        self._tool_label = AppKit.NSTextField.labelWithString_("Thinking...")
        try:
            w_medium = float(getattr(AppKit, "NSFontWeightMedium", 0.23))  # type: ignore[attr-defined]
            self._tool_label.setFont_(sys_font(11.0, w_medium))
            self._tool_label.setTextColor_(AppKit.NSColor.secondaryLabelColor())
            self._tool_label.setMaximumNumberOfLines_(1)
            self._tool_label.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)  # type: ignore[attr-defined]
            self._tool_label.setTranslatesAutoresizingMaskIntoConstraints_(False)
        except Exception:
            pass

        # Controls Row
        self._controls_row = AppKit.NSStackView.alloc().initWithFrame_(
            AppKit.NSMakeRect(0, 0, self.width, 24)
        )
        self._controls_row.setOrientation_(AppKit.NSUserInterfaceLayoutOrientationHorizontal)  # type: ignore[attr-defined]
        self._controls_row.setAlignment_(AppKit.NSLayoutAttributeCenterY)  # type: ignore[attr-defined]
        self._controls_row.setSpacing_(8.0)

        # Spacer pushes buttons to the right
        spacer = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 1, 1))
        self._controls_row.addArrangedSubview_(spacer)
        try:
            spacer.setContentHuggingPriority_forOrientation_(
                1,
                AppKit.NSLayoutConstraintOrientationHorizontal,  # type: ignore[attr-defined]
            )
        except Exception:
            pass

        # Action Handler
        self._action_handler = ConversationOverlayActionHandler.alloc().init()
        self._action_handler._overlay = self  # type: ignore[attr-defined]

        # Hide Button
        self._hide_btn = AppKit.NSButton.buttonWithTitle_target_action_(
            "Hide",
            self._action_handler,
            "hide:",
        )
        style_small_button(self._hide_btn)
        self._controls_row.addArrangedSubview_(self._hide_btn)

        # Show Chat Button
        self._show_chat_btn = AppKit.NSButton.buttonWithTitle_target_action_(
            "Show Chat",
            self._action_handler,
            "showChat:",
        )
        style_small_button(self._show_chat_btn)
        self._controls_row.addArrangedSubview_(self._show_chat_btn)

        # Interrupt Button
        self._interrupt_btn = AppKit.NSButton.buttonWithTitle_target_action_(
            "Interrupt",
            self._action_handler,
            "interrupt:",
        )
        style_small_button(self._interrupt_btn)
        self._controls_row.addArrangedSubview_(self._interrupt_btn)

        # Layout stacking
        self._stack.addArrangedSubview_(self._header_row)
        self._stack.addArrangedSubview_(self._message_label)
        self._stack.addArrangedSubview_(self._tool_label)

        # Add vertical spacer to push controls to the bottom when window is taller
        self._v_spacer = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 1, 1))
        self._stack.addArrangedSubview_(self._v_spacer)
        try:
            self._v_spacer.setContentHuggingPriority_forOrientation_(
                1.0,
                AppKit.NSLayoutConstraintOrientationVertical,  # type: ignore[attr-defined]
            )
        except Exception:
            pass

        self._stack.addArrangedSubview_(self._controls_row)

        self._content.addSubview_(self._stack)

        # Constraints
        margin_layout = 10.0
        self._stack.setTranslatesAutoresizingMaskIntoConstraints_(False)
        self._dot.setTranslatesAutoresizingMaskIntoConstraints_(False)
        try:
            AppKit.NSLayoutConstraint.activateConstraints_(
                [
                    self._stack.leadingAnchor().constraintEqualToAnchor_constant_(
                        self._content.leadingAnchor(), margin_layout
                    ),
                    self._stack.trailingAnchor().constraintEqualToAnchor_constant_(
                        self._content.trailingAnchor(), -margin_layout
                    ),
                    self._stack.topAnchor().constraintEqualToAnchor_constant_(
                        self._content.topAnchor(), margin_layout
                    ),
                    self._stack.bottomAnchor().constraintEqualToAnchor_constant_(
                        self._content.bottomAnchor(), -margin_layout
                    ),
                    self._dot.widthAnchor().constraintEqualToConstant_(10.0),
                    self._dot.heightAnchor().constraintEqualToConstant_(10.0),
                    self._message_label.widthAnchor().constraintEqualToAnchor_(
                        self._stack.widthAnchor()
                    ),
                    self._tool_label.widthAnchor().constraintEqualToAnchor_(
                        self._stack.widthAnchor()
                    ),
                    self._header_row.widthAnchor().constraintEqualToAnchor_(
                        self._stack.widthAnchor()
                    ),
                    self._controls_row.widthAnchor().constraintEqualToAnchor_(
                        self._stack.widthAnchor()
                    ),
                ]
            )
        except Exception:
            pass

        try:
            self._message_label.setPreferredMaxLayoutWidth_(float(self.width - 20.0))
            self._tool_label.setPreferredMaxLayoutWidth_(float(self.width - 20.0))
        except Exception:
            pass

        self.hide()

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

    def minimize(self) -> None:
        try:
            if self.is_minimized:
                return
            self.is_minimized = True

            # Save the current frame size
            current_frame = self._panel.frame()
            self._expanded_size = current_frame.size

            # Hide standard UI views
            self._title.setHidden_(True)
            self._message_label.setHidden_(True)
            self._tool_label.setHidden_(True)
            if hasattr(self, "_v_spacer") and self._v_spacer:
                self._v_spacer.setHidden_(True)
            self._controls_row.setHidden_(True)

            # Temporarily clear min/max size limits before resizing down to minimize size
            self._panel.setMinSize_(AppKit.NSMakeSize(36.0, 36.0))
            self._panel.setMaxSize_(AppKit.NSMakeSize(36.0, 36.0))

            # Disable resizing while minimized
            style = AppKit.NSWindowStyleMaskBorderless | AppKit.NSWindowStyleMaskNonactivatingPanel
            self._panel.setStyleMask_(style)

            # Position on the right edge vertically centered
            sx, sy, sw, sh = active_screen_visible_frame()
            mini_w, mini_h = 36.0, 36.0
            x = float(sx + sw - mini_w)
            y = float(sy + (sh - mini_h) / 2.0)

            # Set new frame
            self._panel.setFrame_display_animate_(AppKit.NSMakeRect(x, y, mini_w, mini_h), True, True)
        except Exception as e:
            print(f"Error minimizing conversation overlay: {e}")

    def maximize(self) -> None:
        try:
            if not self.is_minimized:
                return
            self.is_minimized = False

            # Enable resizable style
            style = AppKit.NSWindowStyleMaskBorderless | AppKit.NSWindowStyleMaskNonactivatingPanel | AppKit.NSWindowStyleMaskResizable
            self._panel.setStyleMask_(style)

            # Set the limits back to expanded/max values
            self._panel.setMinSize_(AppKit.NSMakeSize(380.0, 90.0))
            self._panel.setMaxSize_(AppKit.NSMakeSize(380.0, 280.0))

            # Restore expanded size or default size
            w = 380.0
            h = float(self._expanded_size.height if hasattr(self, "_expanded_size") else self.height)

            # Position on the right edge vertically centered
            sx, sy, sw, sh = active_screen_visible_frame()
            x = float(sx + sw - w)
            y = float(sy + (sh - h) / 2.0)

            # Set new frame
            self._panel.setFrame_display_animate_(AppKit.NSMakeRect(x, y, w, h), True, True)

            # Show standard UI views again if they are not empty/missing
            self._title.setHidden_(False)

            message_val = self._message_label.stringValue()
            if message_val and str(message_val).strip():
                self._message_label.setHidden_(False)
            else:
                self._message_label.setHidden_(True)

            tool_val = self._tool_label.stringValue()
            if tool_val and str(tool_val).strip():
                self._tool_label.setHidden_(False)
            else:
                self._tool_label.setHidden_(True)

            if hasattr(self, "_v_spacer") and self._v_spacer:
                self._v_spacer.setHidden_(False)
            self._controls_row.setHidden_(False)

            self._update_window_height()
        except Exception as e:
            print(f"Error maximizing conversation overlay: {e}")

    def close(self) -> None:
        try:
            if hasattr(self, "_dot") and self._dot:
                self._dot.close()
        except Exception:
            pass
        try:
            self._panel.close()
        except Exception:
            pass

    def _update_window_height(self) -> None:
        try:
            if self.is_minimized:
                return

            # Force layout pass to ensure fitting size is up to date
            self._stack.layoutSubtreeIfNeeded()

            stack_height = float(self._stack.fittingSize().height)
            margin_layout = 10.0
            needed_height = stack_height + 2.0 * margin_layout

            # Bound the height between a minimum of 90.0 and maximum of 280.0
            new_height = max(90.0, min(280.0, needed_height))

            # Update frame
            sx, sy, sw, sh = active_screen_visible_frame()
            current_frame = self._panel.frame()
            w = 380.0  # Keep the width strictly fixed at 380.0
            x = float(sx + sw - w)
            y = float(sy + (sh - new_height) / 2.0)

            # Only update if the height is actually different
            if abs(current_frame.size.height - new_height) > 1.0:
                self._panel.setFrame_display_animate_(AppKit.NSMakeRect(x, y, w, new_height), True, False)
        except Exception as e:
            print(f"Error updating window height: {e}")

    def update_text(self, text: str) -> None:
        try:
            cleaned = text.strip() if text else ""
            if not cleaned:
                self._message_label.setHidden_(True)
                self._message_label.setStringValue_("")
            else:
                self._message_label.setHidden_(False)
                self._message_label.setStringValue_(cleaned)
            self._update_window_height()
        except Exception:
            pass

    def update_tool(self, tool_name: str) -> None:
        try:
            cleaned_tool = tool_name.strip() if tool_name else ""
            if not cleaned_tool:
                self._tool_label.setHidden_(True)
                self._tool_label.setStringValue_("")
            else:
                self._tool_label.setHidden_(False)
                if cleaned_tool.lower() == "thinking...":
                    self._tool_label.setStringValue_("Thinking...")
                else:
                    self._tool_label.setStringValue_(f"Running Tool: {cleaned_tool}")
            self._update_window_height()
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
            self._interrupt_btn.setEnabled_(False)
            self._tool_label.setStringValue_("Interrupting agent...")
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
