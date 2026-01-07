import os
import json
import time
from pathlib import Path

class SessionStorage:
    def __init__(self, base_dir="recordings"):
        self.base_dir = Path(base_dir)
        self.session_dir = None
        self.screenshots_dir = None
        self.audio_dir = None
        self.manifest_path = None
        self.metadata_path = None
        self.screenshot_counter = 0
        self.audio_counter = 0

    def start_session(self, name, description="", config=None):
        """Initialize a new session directory and metadata."""
        timestamp = time.strftime("%Y%m%dT%H%M%SZ")
        safe_name = "".join(c for c in name if c.isalnum() or c in (' ', '-', '_')).strip().replace(' ', '_')
        session_folder_name = f"{timestamp}-{safe_name}"

        self.session_dir = self.base_dir / session_folder_name
        self.screenshots_dir = self.session_dir / "screenshots"
        self.audio_dir = self.session_dir / "audio"
        self.manifest_path = self.session_dir / "manifest.jsonl"
        self.metadata_path = self.session_dir / "metadata.json"

        # Create directories
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        self.audio_dir.mkdir(parents=True, exist_ok=True)

        # Write metadata
        metadata = {
            "session_id": session_folder_name,
            "name": name,
            "description": description,
            "created_at": timestamp,
            "platform": "macos",
            "config": config or {}
        }
        with open(self.metadata_path, "w") as f:
            json.dump(metadata, f, indent=2)

        print(f"Started session: {self.session_dir}")

    def write_event(self, event_data):
        """Append an event to the manifest."""
        if not self.manifest_path:
            raise RuntimeError("Session not started")

        # Ensure timestamp is present
        if "timestamp" not in event_data:
            event_data["timestamp"] = time.time()

        with open(self.manifest_path, "a") as f:
            f.write(json.dumps(event_data) + "\n")

    def get_screenshot_path(self, filename=None):
        """Get path for a new screenshot. If no filename, generates one based on counter."""
        if not filename:
            name = f"{self.screenshot_counter}.png"
            self.screenshot_counter += 1
        else:
            name = filename
        return self.screenshots_dir / name

    def get_audio_path(self, filename=None):
        """Get path for a new audio clip."""
        if not filename:
            name = f"{self.audio_counter}.wav"
            self.audio_counter += 1
        else:
            name = filename
        return self.audio_dir / name

    def get_relative_path(self, absolute_path):
        """Convert absolute path to relative path from session dir for manifest."""
        if absolute_path is None:
            return None
        return os.path.relpath(absolute_path, self.session_dir)
