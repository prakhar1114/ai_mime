from pynput import mouse, keyboard
import time
import threading
import os
from pathlib import Path
from .screenshot import ScreenshotRecorder
from .audio import AudioRecorder

class CurrentScreenshotUpdater:
    """
    Continuously captures the primary display to screenshots/current_screenshot.png.
    Writes are made atomic by capturing to a temp file and os.replace()ing into place.
    """
    def __init__(self, screenshot_recorder: ScreenshotRecorder, storage, interval_s: float = 0.5):
        self.screenshot_recorder = screenshot_recorder
        self.storage = storage
        self.interval_s = interval_s
        self._stop_event = threading.Event()
        self._thread = None
        self._capture_lock = threading.Lock()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def force_refresh(self):
        """Capture immediately (atomic replace)."""
        self._capture_once()

    def freeze_current(self, filename: str | None = None):
        """
        Freeze the most recent current screenshot into the numbered screenshots dir.
        Uses the same lock as writer so read/copy never races with os.replace().
        """
        with self._capture_lock:
            dest_path = Path(self.storage.get_current_screenshot_path())
            if not dest_path.exists():
                self._capture_once()
            return self.storage.freeze_screenshot(dest_path, filename=filename)

    def copy_current_to(self, dst_path):
        """
        Copy the most recent current screenshot to dst_path safely under the writer lock.
        """
        with self._capture_lock:
            src_path = Path(self.storage.get_current_screenshot_path())
            if not src_path.exists():
                self._capture_once()
            return self.storage.copy_file(src_path, dst_path)

    def _run(self):
        # Capture immediately so we have a current frame available ASAP.
        self._capture_once()
        while not self._stop_event.is_set():
            # Sleep first to avoid a tight loop if capture fails quickly.
            self._stop_event.wait(self.interval_s)
            if self._stop_event.is_set():
                break
            self._capture_once()

    def _capture_once(self):
        # Ensure captures + replaces aren't overlapped (also used by force_refresh).
        with self._capture_lock:
            dest_path = Path(self.storage.get_current_screenshot_path())
            # Keep a .png suffix so mss reliably writes PNG.
            tmp_path = dest_path.with_suffix(".tmp.png")
            try:
                # Capture to temp path first.
                saved_tmp = self.screenshot_recorder.capture(tmp_path)
                if not saved_tmp:
                    return None
                # Atomic swap into place.
                os.replace(str(tmp_path), str(dest_path))
                return str(dest_path)
            except Exception as e:
                print(f"Current screenshot update failed: {e}")
                try:
                    if tmp_path.exists():
                        tmp_path.unlink()
                except Exception:
                    pass
                return None

class EventRecorder:
    def __init__(self, storage):
        self.storage = storage
        self.screenshot_recorder = ScreenshotRecorder()
        self.audio_recorder = AudioRecorder()

        # Audio State
        self.pending_audio_clip = None
        self.ptt_key = keyboard.Key.f9  # Default PTT
        self.ptt_active = False

        # Typing State
        self.type_buf = []
        self.type_screenshot = None

        # Throttling
        self.last_scroll_time = 0
        self.scroll_throttle = 0.5

        self.recording = False
        self.mouse_listener = None
        self.keyboard_listener = None
        self.modifiers = set() # Track active modifiers

        # Current screenshot updater (overwrites current_screenshot.png every 500ms)
        self.current_updater = CurrentScreenshotUpdater(
            screenshot_recorder=self.screenshot_recorder,
            storage=self.storage,
            interval_s=0.5,
        )

    def start(self):
        """Start capturing events."""
        if self.recording: return
        self.recording = True

        print("Starting listeners...")
        # Start current screenshot updater first so we always have a recent pre-action frame.
        self.current_updater.start()
        # Best-effort ensure at least one current frame exists before any first event.
        self.current_updater.force_refresh()

        # Blocking=False (default).
        self.mouse_listener = mouse.Listener(
            on_click=self.on_click,
            on_scroll=self.on_scroll
        )
        self.keyboard_listener = keyboard.Listener(
            on_press=self.on_press,
            on_release=self.on_release
        )
        self.mouse_listener.start()
        self.keyboard_listener.start()

    def stop(self):
        """Stop capturing and cleanup."""
        if not self.recording: return
        self.recording = False

        if self.mouse_listener: self.mouse_listener.stop()
        if self.keyboard_listener: self.keyboard_listener.stop()

        self.flush_typing()

        # Stop current screenshot updater
        self.current_updater.stop()

        # Stop audio if running
        if self.ptt_active:
            saved = self.audio_recorder.stop()
            if saved: self.pending_audio_clip = saved

        # Handle leftover audio
        if self.pending_audio_clip:
             self.storage.write_event({
                "action_type": "end",
                "voice_clip": self.storage.get_relative_path(self.pending_audio_clip),
                "timestamp": time.time(),
                "screenshot": None,
                "action_details": {}
             })

    def _freeze_current_screenshot(self):
        """Freeze the most recent pre-action current screenshot into the numbered screenshots/ dir."""
        return self.current_updater.freeze_current()

    def _capture_pretyping_screenshot(self):
        """Copy current screenshot into a stable pretyping file (overwritten per typing burst)."""
        pretyping_path = self.storage.get_pretyping_screenshot_path()
        self.current_updater.copy_current_to(pretyping_path)
        self.type_screenshot = pretyping_path

    def _consume_audio(self):
        """Return pending audio clip path (relative) and clear it."""
        if self.pending_audio_clip:
            rel = self.storage.get_relative_path(self.pending_audio_clip)
            self.pending_audio_clip = None
            return rel
        return None

    def flush_typing(self):
        """Flush buffered typing events."""
        if not self.type_buf:
            return

        text = "".join(self.type_buf)
        self.type_buf = []

        # For typing, the screenshot must represent the pre-typing state.
        # We store a "pretyping_screenshot.png" at typing burst start and freeze from that.
        pretyping_path = self.type_screenshot
        self.type_screenshot = None
        if pretyping_path:
            screenshot = self.storage.freeze_screenshot(pretyping_path)
        else:
            # Fallback: freeze current (best-effort) if we missed typing-burst start.
            screenshot = self._freeze_current_screenshot()

        self.storage.write_event({
            "action_type": "type",
            "action_details": {"text": text},
            "screenshot": screenshot,
            "voice_clip": self._consume_audio(),
            "timestamp": time.time()
        })

        # After typing, refresh current screenshot so subsequent actions see the typed result quickly.
        self.current_updater.force_refresh()

    def on_click(self, x, y, button, pressed):
        if not self.recording: return
        if pressed:
            # 1. Flush any pending typing
            self.flush_typing()

            # 2. Freeze latest pre-action screenshot
            screenshot = self._freeze_current_screenshot()

            # 3. Write event
            self.storage.write_event({
                "action_type": "click",
                "action_details": {"button": str(button), "x": x, "y": y, "pressed": True},
                "screenshot": screenshot,
                "voice_clip": self._consume_audio(),
                "timestamp": time.time()
            })
            # 4. Refresh current screenshot to capture result sooner
            self.current_updater.force_refresh()

    def on_scroll(self, x, y, dx, dy):
        if not self.recording: return
        now = time.time()
        if now - self.last_scroll_time < self.scroll_throttle:
            return

        self.last_scroll_time = now
        self.flush_typing()
        screenshot = self._freeze_current_screenshot()

        self.storage.write_event({
            "action_type": "scroll",
            "action_details": {"x": x, "y": y, "dx": dx, "dy": dy},
            "screenshot": screenshot,
            "voice_clip": self._consume_audio(),
            "timestamp": now
        })
        self.current_updater.force_refresh()

    def on_press(self, key):
        if not self.recording: return

        # Track Modifiers
        if key in [keyboard.Key.cmd, keyboard.Key.cmd_l, keyboard.Key.cmd_r]:
            self.modifiers.add("cmd")
        if key in [keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r]:
            self.modifiers.add("ctrl")
        if key in [keyboard.Key.alt, keyboard.Key.alt_l, keyboard.Key.alt_r]:
            self.modifiers.add("alt")
        if key in [keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r]:
            self.modifiers.add("shift")

        # PTT Logic
        if key == self.ptt_key:
            if not self.ptt_active:
                self.ptt_active = True
                path = self.storage.get_audio_path()
                self.audio_recorder.start(str(path))
            return # Don't record PTT key itself

        # Special Keys: Flush typing, then record separately
        # Note: on macOS laptops, F4 is often mapped to Launchpad.
        # To record the raw F4 key, use Fn+F4 or check System Settings > Keyboard > Shortcuts.
        if key in [keyboard.Key.enter, keyboard.Key.tab, keyboard.Key.esc, keyboard.Key.f4]:
            self.flush_typing()

            # Freeze latest pre-action screenshot (after flush_typing() which refreshes current to include typed text).
            screenshot = self._freeze_current_screenshot()

            key_name = str(key).replace("Key.", "").upper()

            self.storage.write_event({
                "action_type": "key",
                "action_details": {"key": key_name},
                "screenshot": screenshot,
                "voice_clip": self._consume_audio(),
                "timestamp": time.time()
            })
            self.current_updater.force_refresh()
            return

        # Handle Cmd+Space (Spotlight/Search) specifically
        if key == keyboard.Key.space:
             # Check if Cmd is currently held down.
             # pynput Listener doesn't give us modifier state easily in on_press event args,
             # but we can track it manually or use a helper.
             # However, for now, let's just treat Cmd+Space as a special "Search" event if we can detect it.
             # Since we don't have global state for modifiers in this simplified class yet,
             # we will implement a basic modifier tracker.
             pass

        try:
            # Try to get char (letters, numbers)
            # pynput keys can be Key or KeyCode; only KeyCode has .char
            char = key.char  # type: ignore[attr-defined]
        except AttributeError:
            # Handle Cmd+Space (Spotlight/Search)
            if key == keyboard.Key.space and "cmd" in self.modifiers:
                self.flush_typing()
                screenshot = self._freeze_current_screenshot()
                self.storage.write_event({
                    "action_type": "key",
                    "action_details": {"key": "CMD+SPACE"}, # Explicitly log Search
                    "screenshot": screenshot,
                    "voice_clip": self._consume_audio(),
                    "timestamp": time.time()
                })
                self.current_updater.force_refresh()
                return

            # Treat space as normal typing if Cmd isn't held (pynput represents it as a Key, not a char)
            if key == keyboard.Key.space and "cmd" not in self.modifiers:
                if not self.type_buf:
                    self._capture_pretyping_screenshot()
                self.type_buf.append(" ")
                return

            # Backspace: mutate the typing buffer (don’t emit a separate key event)
            if key in [keyboard.Key.backspace, keyboard.Key.delete]:
                if self.type_buf:
                    self.type_buf.pop()
                return

            # Non-character keys (shift, ctrl, etc)
            # Log unknown keys to help debug F4/Search issues
            print(f"DEBUG: Unknown/Special key pressed: {key}")
            return

        if char:
            # Start of typing burst: capture pretyping frame once.
            if not self.type_buf:
                self._capture_pretyping_screenshot()

            self.type_buf.append(char)

    def on_release(self, key):
        # Update Modifiers
        if key in [keyboard.Key.cmd, keyboard.Key.cmd_l, keyboard.Key.cmd_r]:
            self.modifiers.discard("cmd")
        if key in [keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r]:
            self.modifiers.discard("ctrl")
        if key in [keyboard.Key.alt, keyboard.Key.alt_l, keyboard.Key.alt_r]:
            self.modifiers.discard("alt")
        if key in [keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r]:
            self.modifiers.discard("shift")

        if key == self.ptt_key:
            if self.ptt_active:
                self.ptt_active = False
                saved_file = self.audio_recorder.stop()
                if saved_file:
                    self.pending_audio_clip = saved_file
