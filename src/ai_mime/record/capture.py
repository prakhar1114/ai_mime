from pynput import mouse, keyboard
import time
import threading
from .screenshot import ScreenshotRecorder
from .audio import AudioRecorder

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

    def start(self):
        """Start capturing events."""
        if self.recording: return
        self.recording = True

        print("Starting listeners...")
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

    def _get_screenshot(self):
        """Helper to capture screenshot and return relative path."""
        path = self.storage.get_screenshot_path()
        saved_path = self.screenshot_recorder.capture(path)
        return self.storage.get_relative_path(saved_path)

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

        # Use the most recent screenshot from the typing buffer (result of typing)
        # instead of taking a new one which might be "too late" (e.g. after Enter pressed but before flush processed)
        screenshot = self.type_screenshot
        self.type_screenshot = None

        # If no screenshot captured during typing (shouldn't happen), take one now
        if not screenshot:
             screenshot = self._get_screenshot()

        self.storage.write_event({
            "action_type": "type",
            "action_details": {"text": text},
            "screenshot": screenshot,
            "voice_clip": self._consume_audio(),
            "timestamp": time.time()
        })

    def on_click(self, x, y, button, pressed):
        if not self.recording: return
        if pressed:
            # 1. Flush any pending typing
            self.flush_typing()

            # 2. Capture screenshot (Pre-action)
            screenshot = self._get_screenshot()

            # 3. Write event
            self.storage.write_event({
                "action_type": "click",
                "action_details": {"button": str(button), "x": x, "y": y, "pressed": True},
                "screenshot": screenshot,
                "voice_clip": self._consume_audio(),
                "timestamp": time.time()
            })

    def on_scroll(self, x, y, dx, dy):
        if not self.recording: return
        now = time.time()
        if now - self.last_scroll_time < self.scroll_throttle:
            return

        self.last_scroll_time = now
        self.flush_typing()
        screenshot = self._get_screenshot()

        self.storage.write_event({
            "action_type": "scroll",
            "action_details": {"x": x, "y": y, "dx": dx, "dy": dy},
            "screenshot": screenshot,
            "voice_clip": self._consume_audio(),
            "timestamp": now
        })

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

            # For special keys, we want to capture the screen state *before* the key takes effect
            # (e.g. before the window closes on Esc, or before the form submits on Enter).
            # However, for Enter after typing, we just flushed the typing which used the "latest" screenshot.
            # So this new screenshot will capture the state right at the moment of pressing Enter.
            screenshot = self._get_screenshot()

            key_name = str(key).replace("Key.", "").upper()

            self.storage.write_event({
                "action_type": "key",
                "action_details": {"key": key_name},
                "screenshot": screenshot,
                "voice_clip": self._consume_audio(),
                "timestamp": time.time()
            })
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

        # Normal Typing

        # Normal Typing
        try:
            # Try to get char (letters, numbers)
            char = key.char
        except AttributeError:
            # Handle Cmd+Space (Spotlight/Search)
            if key == keyboard.Key.space and "cmd" in self.modifiers:
                self.flush_typing()
                screenshot = self._get_screenshot()
                self.storage.write_event({
                    "action_type": "key",
                    "action_details": {"key": "CMD+SPACE"}, # Explicitly log Search
                    "screenshot": screenshot,
                    "voice_clip": self._consume_audio(),
                    "timestamp": time.time()
                })
                return

            # Non-character keys (shift, ctrl, etc)
            # Log unknown keys to help debug F4/Search issues
            print(f"DEBUG: Unknown/Special key pressed: {key}")
            return

        if char:
            # Capture screenshot on every keypress and keep it.
            # This ensures we have the "latest" state of the screen as typing progresses.
            # When flush happens (e.g. on Enter), we use the LAST captured screenshot,
            # which shows the full text typed so far.
            self.type_screenshot = self._get_screenshot()

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
