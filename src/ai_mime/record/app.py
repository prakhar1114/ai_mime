import rumps
import multiprocessing
from .storage import SessionStorage
# We don't import EventRecorder here anymore to avoid loading pynput in the UI process
from .recorder_process import run_recorder_process

class RecorderApp(rumps.App):
    def __init__(self):
        super(RecorderApp, self).__init__("AI Mime", icon=None)
        # We only need storage here to read last session or show info,
        # but the active storage instance will live in the subprocess.
        self.storage = SessionStorage()

        self.recorder_process = None
        self.stop_event = None
        self.is_recording = False

        # Menu Items
        self.start_button = rumps.MenuItem("Start Recording", callback=self.toggle_recording)
        self.menu = [
            self.start_button,
            None, # Separator
        ]

    def toggle_recording(self, sender):
        if not self.is_recording:
            self.start_recording()
        else:
            self.stop_recording()

    def start_recording(self):
        # Prompt for session name
        window = rumps.Window(
            message="Enter a name for this session:",
            title="Start Recording",
            default_text="",
            ok="Start",
            cancel="Cancel"
        )
        response = window.run()

        if not response.clicked:
            return

        name = response.text.strip()
        if not name:
            rumps.alert("Name required!")
            return

        # Optional Description (2nd prompt)
        window_desc = rumps.Window(
            message="Enter description (optional):",
            title="Session Description",
            default_text="",
            ok="Go",
            cancel="Skip"
        )
        response_desc = window_desc.run()
        description = response_desc.text.strip() if response_desc.clicked else ""

        try:
            self.stop_event = multiprocessing.Event()
            self.recorder_process = multiprocessing.Process(
                target=run_recorder_process,
                args=(name, description, self.stop_event)
            )
            self.recorder_process.start()

            self.is_recording = True
            self.title = "🔴 Rec"
            self.start_button.title = "Stop Recording"
        except Exception as e:
            rumps.alert(f"Error starting: {e}")

    def stop_recording(self):
        try:
            if self.stop_event:
                self.stop_event.set()

            if self.recorder_process:
                self.recorder_process.join(timeout=5)
                if self.recorder_process.is_alive():
                    self.recorder_process.terminate()
                self.recorder_process = None

        except Exception as e:
            rumps.alert(f"Error stopping: {e}")

        self.is_recording = False
        self.title = "AI Mime"
        self.start_button.title = "Start Recording"

        rumps.notification(
            title="Recording Saved",
            subtitle="Session capture finished",
            message="The background recording process has stopped."
        )

def run_app():
    # Support for freezing (PyInstaller) if needed later
    multiprocessing.freeze_support()
    app = RecorderApp()
    app.run()
