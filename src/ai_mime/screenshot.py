import mss
import threading
import os


class ScreenshotRecorder:
    def __init__(self):
        self.sct = mss.mss()
        self.lock = threading.Lock()

    def capture(self, filepath):
        """
        Capture the primary screen to the given filepath.
        Returns the filepath if successful, None otherwise.
        """
        try:
            with self.lock:
                # Capture monitor 1 (primary).
                # Note: sct.shot() saves to a file.
                self.sct.shot(mon=1, output=str(filepath))
                return str(filepath)
        except Exception as e:
            print(f"Screenshot failed: {e}")
            return None
