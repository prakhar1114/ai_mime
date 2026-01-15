from __future__ import annotations

import sys
import time
from typing import Iterable

from pynput import keyboard, mouse

from .engine import ReplayConfig, ReplayError


_K = keyboard.Key


def _normalize_key_token(t: str) -> str:
    return t.strip().lower().replace(" ", "").replace("-", "_")


def _token_to_key(token: str) -> keyboard.Key | str:
    """
    Map common tokens to pynput Key or literal character for Controller.type/press.
    """
    tok = _normalize_key_token(token)
    # Modifiers
    if tok in ("cmd", "command", "meta"):
        return _K.cmd
    if tok in ("ctrl", "control"):
        return _K.ctrl
    if tok in ("alt", "option"):
        return _K.alt
    if tok == "shift":
        return _K.shift

    # Special keys
    if tok in ("space",):
        return _K.space
    if tok in ("enter", "return"):
        return _K.enter
    if tok in ("tab",):
        return _K.tab
    if tok in ("esc", "escape"):
        return _K.esc
    if tok in ("backspace", "delete"):
        return _K.backspace

    # Function keys
    if tok.startswith("f") and tok[1:].isdigit():
        n = int(tok[1:])
        try:
            return getattr(_K, f"f{n}")
        except Exception:
            pass

    # Single character
    if len(tok) == 1:
        return tok
    # Fallback: treat as literal string (may not work for all keys)
    return tok


def exec_keypress_from_schema_value(action_value: str, cfg: ReplayConfig) -> None:
    """
    Execute a schema KEYPRESS action_value like "CMD+SPACE".
    """
    if not isinstance(action_value, str) or not action_value:
        raise ReplayError("KEYPRESS action_value must be a non-empty string")
    tokens = [t for t in action_value.split("+") if t.strip()]
    exec_keypress_tokens(tokens, cfg)


def exec_keypress_tokens(keys: Iterable[str], cfg: ReplayConfig) -> None:
    ctrl = keyboard.Controller()
    seq = [_token_to_key(k) for k in keys]
    # Press in order, release in reverse.
    pressed: list[keyboard.Key | str] = []
    try:
        for k in seq:
            ctrl.press(k)  # type: ignore[arg-type]
            pressed.append(k)
        # tiny tap
        time.sleep(0.02)
    finally:
        for k in reversed(pressed):
            try:
                ctrl.release(k)  # type: ignore[arg-type]
            except Exception:
                pass


def exec_type(text: str, cfg: ReplayConfig) -> None:
    if not isinstance(text, str):
        text = str(text)
    keyboard.Controller().type(text)


def exec_mouse_move(x: int, y: int, cfg: ReplayConfig) -> None:
    m = mouse.Controller()
    m.position = (int(x), int(y))


def exec_click(x: int, y: int, cfg: ReplayConfig, *, button: mouse.Button = mouse.Button.left, clicks: int = 1) -> None:
    x_i = int(x)
    y_i = int(y)
    n = max(1, int(clicks))

    # Prefer native Quartz events on macOS; they tend to be far more reliable for double-click
    # semantics across apps than rapid synthetic Controller.click loops.
    if sys.platform == "darwin":
        try:
            import Quartz  # type: ignore[import-not-found]

            if button == mouse.Button.left:
                down = Quartz.kCGEventLeftMouseDown  # type: ignore[attr-defined]
                up = Quartz.kCGEventLeftMouseUp  # type: ignore[attr-defined]
                btn = Quartz.kCGMouseButtonLeft  # type: ignore[attr-defined]
            elif button == mouse.Button.right:
                down = Quartz.kCGEventRightMouseDown  # type: ignore[attr-defined]
                up = Quartz.kCGEventRightMouseUp  # type: ignore[attr-defined]
                btn = Quartz.kCGMouseButtonRight  # type: ignore[attr-defined]
            else:
                down = Quartz.kCGEventOtherMouseDown  # type: ignore[attr-defined]
                up = Quartz.kCGEventOtherMouseUp  # type: ignore[attr-defined]
                btn = Quartz.kCGMouseButtonCenter  # type: ignore[attr-defined]

            # Use a realistic double-click interval.
            inter_click_delay_s = 0.12 if n > 1 else 0.03

            for i in range(n):
                pt = (float(x_i), float(y_i))
                ev_down = Quartz.CGEventCreateMouseEvent(None, down, pt, btn)  # type: ignore[attr-defined]
                ev_up = Quartz.CGEventCreateMouseEvent(None, up, pt, btn)  # type: ignore[attr-defined]

                # clickState is 1 for single click, 2 for second click, etc.
                Quartz.CGEventSetIntegerValueField(ev_down, Quartz.kCGMouseEventClickState, i + 1)  # type: ignore[attr-defined]
                Quartz.CGEventSetIntegerValueField(ev_up, Quartz.kCGMouseEventClickState, i + 1)  # type: ignore[attr-defined]

                Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev_down)  # type: ignore[attr-defined]
                time.sleep(0.01)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev_up)  # type: ignore[attr-defined]
                if i < n - 1:
                    time.sleep(inter_click_delay_s)
            return
        except Exception:
            # Fall back to pynput if Quartz isn't available or posting fails.
            pass

    m = mouse.Controller()
    m.position = (x_i, y_i)
    time.sleep(0.03)
    # Use Controller.click(count=n) so the library can generate multi-click semantics itself.
    try:
        m.click(button, count=n)
    except TypeError:
        # Older pynput signature fallback.
        for _ in range(n):
            m.click(button)
            time.sleep(0.12 if n > 1 else 0.03)


def exec_scroll(pixels: float, cfg: ReplayConfig) -> None:
    m = mouse.Controller()
    # pynput uses steps; best-effort map pixels to 1 step per ~120px.
    dy = int(round(float(pixels) / 120.0))
    if dy == 0:
        dy = 1 if pixels > 0 else -1
    m.scroll(0, dy)


def exec_wait(seconds: float, cfg: ReplayConfig) -> None:
    time.sleep(max(0.0, float(seconds)))


def exec_computer_use_action(action: dict, cfg: ReplayConfig) -> None:
    """
    Execute the normalized action dict produced by grounding.tool_call_to_pixel_action().
    """
    a = action.get("action")
    if not isinstance(a, str) or not a:
        raise ReplayError(f"Invalid action: {action}")

    if a == "key":
        keys = action.get("keys")
        if not isinstance(keys, list) or not keys:
            raise ReplayError(f"action=key requires keys[]: {action}")
        exec_keypress_tokens([str(k) for k in keys], cfg)
        return

    if a == "type":
        text = action.get("text")
        if text is None:
            raise ReplayError(f"action=type requires text: {action}")
        exec_type(str(text), cfg)
        return

    if a == "mouse_move":
        exec_mouse_move(int(action["x_px"]), int(action["y_px"]), cfg)
        return

    if a in ("left_click", "right_click", "middle_click", "double_click", "triple_click"):
        btn = mouse.Button.left
        if a == "right_click":
            btn = mouse.Button.right
        if a == "middle_click":
            btn = mouse.Button.middle
        clicks = 1
        if a == "double_click":
            clicks = 2
        if a == "triple_click":
            clicks = 3
        exec_click(int(action["x_px"]), int(action["y_px"]), cfg, button=btn, clicks=clicks)
        return

    if a in ("scroll", "hscroll"):
        pixels = action.get("pixels")
        if pixels is None:
            raise ReplayError(f"action=scroll requires pixels: {action}")
        exec_scroll(float(pixels), cfg)
        return

    if a == "wait":
        t = action.get("time")
        if t is None:
            raise ReplayError(f"action=wait requires time: {action}")
        exec_wait(float(t), cfg)
        return

    if a in ("terminate", "answer"):
        # These are model-side actions; treat terminate as stop condition, answer as no-op.
        raise ReplayError(f"Model returned terminal action '{a}': {action}")

    raise ReplayError(f"Unsupported computer_use action: {a}")
