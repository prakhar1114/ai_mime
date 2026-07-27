from __future__ import annotations

"""
PyWebView-based floating overlay HUD for Windows.

Renders frameless, transparent, always-on-top webview windows loading AI Mime HTML overlays
(e.g., recording controls toolbar or agent conversation HUD) on Windows platforms.
"""

import sys
import threading
from typing import Any

if sys.platform == "win32":
    import webview
else:
    webview = None


_shared_win_overlay: PyWebViewOverlayHUD | None = None


def get_shared_win_overlay() -> PyWebViewOverlayHUD | None:
    global _shared_win_overlay
    return _shared_win_overlay


def _get_browser_view(window: Any) -> Any:
    """Get the WinForms BrowserView for a pywebview window (non-blocking helper)."""
    if window is None:
        return None
    try:
        import webview.platforms.winforms as wf
        uid = getattr(window, "uid", None)
        bv = wf.BrowserView.instances.get(uid) if uid else None
        if bv is None and wf.BrowserView.instances:
            bv = list(wf.BrowserView.instances.values())[0]
        return bv
    except Exception as e:
        print(f"[win_overlay] _get_browser_view exception: {e}", flush=True)
    return None


def _get_webview2(bv: Any) -> Any:
    """Return the underlying WebView2 control for a BrowserView, or None.

    We talk to WebView2 directly (ExecuteScriptAsync / CoreWebView2.NavigateToString)
    instead of pywebview's Window.evaluate_js/load_html, which block up to 20s on
    events.shown/loaded -- events we never trip because we show the window via raw
    Win32 -- and then raise 'Main window failed to start'.
    """
    if bv is None:
        return None
    wv = getattr(bv, "webview", None)
    if wv is None:
        wv = getattr(getattr(bv, "browser", None), "webview", None)
    return wv


class PyWebViewApi:
    def __init__(self, overlay_hud: PyWebViewOverlayHUD):
        self._hud = overlay_hud

    def postMessage(self, msg: dict) -> None:
        if not isinstance(msg, dict):
            return
        msg_type = msg.get("type")
        if msg_type == "resize":
            height = msg.get("height")
            if isinstance(height, (int, float)):
                self._hud.resize(int(self._hud.width), int(height))
        elif msg_type == "minimize":
            self._hud.minimize()
        elif msg_type == "maximize":
            self._hud.restore()
        elif msg_type in ("close", "hide"):
            self._hud.hide()


class PyWebViewOverlayHUD:
    """Cross-platform / Windows floating overlay HUD using pywebview."""

    def __init__(
        self,
        html_content: str = "",
        title: str = "AI Mime HUD",
        width: int = 340,
        height: int = 180,
        x: int | None = None,
        y: int | None = None,
        js_api: Any = None,
    ):
        self.html_content = html_content
        self.title = title
        self.width = width
        self.height = height
        self.x = x
        self.y = y
        self.js_api = js_api or PyWebViewApi(self)
        self._window = None
        self._is_showing = False
        global _shared_win_overlay
        _shared_win_overlay = self

    def create_window(self) -> None:
        """Create window upfront on the main thread before webview.start()."""
        if webview is None or self._window is not None:
            return
        try:
            self._window = webview.create_window(
                title=self.title,
                html=self.html_content or "<html><body style='background:#222;'></body></html>",
                width=self.width,
                height=self.height,
                x=self.x,
                y=self.y,
                frameless=True,
                on_top=True,
                transparent=False,
                resizable=True,
                hidden=True,
                js_api=self.js_api,
            )
        except Exception as e:
            print(f"Error creating pywebview window: {e}")

    @property
    def is_visible(self) -> bool:
        return self._window is not None and self._is_showing

    def _invoke_on_ui(self, action: Any) -> None:
        """Schedule action on the WinForms UI thread using BeginInvoke (non-blocking).

        This avoids deadlocks that occur when calling pywebview's blocking
        window.restore()/show()/evaluate_js() from the webview main thread
        callback (the func passed to webview.start()).
        """
        bv = _get_browser_view(self._window)
        if bv is None:
            return
        try:
            if getattr(bv, "InvokeRequired", False):
                from System import Action as SysAction
                bv.BeginInvoke(SysAction(action))
            else:
                action()
        except Exception as e:
            print(f"[win_overlay] _invoke_on_ui error: {e}", flush=True)

    def _force_topmost_impl(self) -> None:
        """Direct Win32 calls to force the window visible, topmost, and positioned.

        Must be called on the WinForms UI thread.
        """
        bv = _get_browser_view(self._window)
        if bv is None:
            return
        try:
            import ctypes

            bv.ShowInTaskbar = False
            bv.TopMost = True

            import webview.platforms.winforms as wf
            bv.WindowState = wf.WinForms.FormWindowState.Normal

            hwnd = bv.Handle.ToInt32()
            user32 = ctypes.windll.user32

            GWL_EXSTYLE = -20
            WS_EX_TOPMOST = 0x00000008
            WS_EX_TOOLWINDOW = 0x00000080
            try:
                current_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                user32.SetWindowLongW(hwnd, GWL_EXSTYLE, current_style | WS_EX_TOPMOST | WS_EX_TOOLWINDOW)
            except Exception:
                pass

            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.ShowWindow(hwnd, 5)  # SW_SHOW

            scale = getattr(bv, "_scale", 1.0) or 1.0
            phys_w = int((self.width or 360) * scale)
            phys_h = int((self.height or 260) * scale)

            sw = float(user32.GetSystemMetrics(0))
            sh = float(user32.GetSystemMetrics(1))

            phys_x = int(self.x if self.x is not None else (sw - phys_w - 20))
            phys_y = int(self.y if self.y is not None else (sh - phys_h - 60))

            # SWP_SHOWWINDOW = 0x0040
            user32.SetWindowPos(hwnd, -1, phys_x, phys_y, phys_w, phys_h, 0x0040)
            bv.BringToFront()
            self._is_showing = True
            # We showed the window ourselves via Win32, so pywebview's own
            # events.shown never fires. Set it so any residual high-level pywebview
            # call doesn't block 20s waiting for a "shown" that never comes.
            try:
                self._window.events.shown.set()
            except Exception:
                pass
        except Exception as e:
            print(f"[win_overlay] _force_topmost_impl error: {e}", flush=True)

    def start(self, html_content: str | None = None) -> None:
        """Show the overlay window, optionally updating HTML."""
        if webview is None:
            return
        if self._window is None:
            self.create_window()
        if self._window is not None:
            try:
                if html_content and html_content != self.html_content:
                    self.html_content = html_content
                    # Navigate the WebView2 directly (non-blocking) rather than
                    # pywebview's shown-gated Window.load_html; then force topmost.
                    def _load_and_show():
                        try:
                            wv = _get_webview2(_get_browser_view(self._window))
                            if wv is not None and wv.CoreWebView2 is not None:
                                wv.CoreWebView2.NavigateToString(html_content)
                            elif wv is not None:
                                wv.EnsureCoreWebView2Async(None)
                        except Exception as e:
                            print(f"[win_overlay] navigate error: {e}", flush=True)
                        self._force_topmost_impl()
                    self._invoke_on_ui(_load_and_show)
                else:
                    self._invoke_on_ui(self._force_topmost_impl)
            except Exception as e:
                print(f"Error showing pywebview window: {e}")

    def restore(self) -> None:
        """Show and restore the overlay window (non-blocking, thread-safe)."""
        if self._window is not None:
            self._invoke_on_ui(self._force_topmost_impl)

    def resize(self, width: int, height: int) -> None:
        if self._window is not None:
            self.width = width
            self.height = height
            try:
                self._window.resize(width, height)
            except Exception:
                pass

    def minimize(self) -> None:
        if self._window is not None:
            try:
                self._window.minimize()
            except Exception:
                pass

    def load_html(self, html_content: str) -> None:
        """Load HTML into the WebView2 directly (non-blocking, thread-safe)."""
        if self._window is None:
            return
        self.html_content = html_content
        bv = _get_browser_view(self._window)
        if bv is None:
            return

        def _load():
            try:
                wv = _get_webview2(bv)
                if wv is None:
                    return
                if wv.CoreWebView2 is not None:
                    wv.CoreWebView2.NavigateToString(html_content)
                else:
                    wv.EnsureCoreWebView2Async(None)
            except Exception as e:
                print(f"[win_overlay] load_html error: {e}", flush=True)

        self._invoke_on_ui(_load)

    def evaluate_js(self, js_code: str) -> None:
        """Run JS via WebView2.ExecuteScriptAsync directly (non-blocking, thread-safe).

        Bypasses pywebview's Window.evaluate_js, which blocks on events.loaded and
        routes results through a JS return-value bridge that isn't wired up here.
        ExecuteScriptAsync is fire-and-forget -- exactly what updateOverlayState needs.
        """
        if self._window is None:
            return
        bv = _get_browser_view(self._window)
        if bv is None:
            return

        def _eval():
            try:
                wv = _get_webview2(bv)
                if wv is None or wv.CoreWebView2 is None:
                    return
                wv.ExecuteScriptAsync(js_code)
            except Exception as e:
                print(f"[win_overlay] evaluate_js error: {e}", flush=True)

        self._invoke_on_ui(_eval)

    def hide(self) -> None:
        """Hide the overlay window (non-blocking, thread-safe)."""
        if self._window is None:
            return
        bv = _get_browser_view(self._window)
        if bv is not None:
            def _do_hide():
                try:
                    bv.Hide()
                    self._is_showing = False
                except Exception:
                    pass
            self._invoke_on_ui(_do_hide)
        else:
            try:
                self._window.hide()
                self._is_showing = False
            except Exception:
                pass

    def close(self) -> None:
        self.hide()
