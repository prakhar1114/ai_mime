import time
import multiprocessing
from .storage import SessionStorage
from .capture import EventRecorder

def run_recorder_process(name, description, stop_event):
    """
    Entry point for the recording child process.
    """
    print(f"Recorder process started for session: {name}")

    # Initialize storage and recorder in this process
    storage = SessionStorage()
    try:
        storage.start_session(name, description=description)
    except Exception as e:
        print(f"Failed to start session: {e}")
        return

    recorder = EventRecorder(storage)

    # Enable listeners (uncommenting the parts we disabled earlier)
    # The EventRecorder.start() method needs to be "clean" again for this usage.
    # We will need to restore the synchronous start since we are now in our own process.

    print("Starting recorder engine...")
    recorder.start()

    # Wait for stop signal
    while not stop_event.is_set():
        time.sleep(0.1)

    print("Stopping recorder engine...")
    recorder.stop()
    print("Recorder process finished.")
