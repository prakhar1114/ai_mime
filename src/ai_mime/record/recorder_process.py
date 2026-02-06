import time
import multiprocessing
import os
import sys
import traceback
from pathlib import Path
from .storage import SessionStorage
from .capture import EventRecorder
from ..app_data import get_recordings_dir
from ..debug_log import log

def run_recorder_process(
    name,
    description,
    stop_event,
    session_dir_queue=None,
    refine_cmd_q=None,
    refine_resp_q=None,
    exclude_window_id=None,
    base_dir=None,
):
    """
    Entry point for the recording child process.
    """
    log(f"=== Recorder subprocess started: {name} ===")
    log(f"base_dir={base_dir}")
    log(f"sys.frozen={getattr(sys, 'frozen', False)}")

    print(f"Recorder process started for session: {name}")

    # Initialize storage and recorder in this process
    resolved_base = base_dir or str(get_recordings_dir())
    log(f"Resolved base_dir: {resolved_base}")

    storage = SessionStorage(base_dir=resolved_base)
    try:
        log("Calling start_session...")
        storage.start_session(name, description=description)
        log(f"Session started: {storage.session_dir}")
    except Exception as e:
        log(f"FAILED start_session: {e}")
        log(traceback.format_exc())
        print(f"Failed to start session: {e}")
        return

    # Let the UI process know where the session is being written.
    try:
        if session_dir_queue is not None and storage.session_dir is not None:
            session_dir_queue.put(str(storage.session_dir))
    except Exception:
        pass

    try:
        log("Creating EventRecorder...")
        recorder = EventRecorder(
            storage,
            refine_cmd_q=refine_cmd_q,
            refine_resp_q=refine_resp_q,
            exclude_window_id=exclude_window_id,
        )

        # Enable listeners (uncommenting the parts we disabled earlier)
        # The EventRecorder.start() method needs to be "clean" again for this usage.
        # We will need to restore the synchronous start since we are now in our own process.

        print("Starting recorder engine...")
        log("Starting recorder listeners...")
        recorder.start()
        log("Recorder started successfully")

        # Wait for stop signal
        while not stop_event.is_set():
            time.sleep(0.1)

        print("Stopping recorder engine...")
        log("Stopping recorder...")
        recorder.stop()
        log("Recorder stopped successfully")

        print("Recorder process finished.")
    except Exception as e:
        log(f"FATAL recorder error: {e}")
        log(traceback.format_exc())
