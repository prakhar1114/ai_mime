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

        # DECISION: For better visibility in "Reflect", we use the current screen state
        # (what was typed so far) rather than the screen state at the START of typing.
        # The 'type_screenshot' captured at start is often during an animation (e.g. Spotlight opening).
        # We discard the stale start-of-batch screenshot in favor of a fresh one.
        self.type_screenshot = None
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

        # PTT Logic
        if key == self.ptt_key:
            if not self.ptt_active:
                self.ptt_active = True
                path = self.storage.get_audio_path()
                self.audio_recorder.start(str(path))
            return # Don't record PTT key itself

        # Special Keys: Flush typing, then record separately
        if key in [keyboard.Key.enter, keyboard.Key.tab, keyboard.Key.esc, keyboard.Key.f4]:
            self.flush_typing()

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

        # Normal Typing
        try:
            # Try to get char (letters, numbers)
            char = key.char
        except AttributeError:
            # Non-character keys (shift, ctrl, etc) - ignore for now in typing batch
            return

        if char:
            if not self.type_buf:
                # First key of batch: Capture Screenshot!
                self.type_screenshot = self._get_screenshot()

            self.type_buf.append(char)

    def on_release(self, key):
        if key == self.ptt_key:
            if self.ptt_active:
                self.ptt_active = False
                saved_file = self.audio_recorder.stop()
                if saved_file:
                    self.pending_audio_clip = saved_file
