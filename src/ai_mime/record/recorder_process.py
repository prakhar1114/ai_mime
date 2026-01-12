import time
import multiprocessing
from .storage import SessionStorage
from .capture import EventRecorder

def run_recorder_process(
    name,
    description,
    stop_event,
    session_dir_queue=None,
    refine_req_q=None,
    refine_resp_q=None,
):
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

    # Let the UI process know where the session is being written.
    try:
        if session_dir_queue is not None and storage.session_dir is not None:
            session_dir_queue.put(str(storage.session_dir))
    except Exception:
        pass

    recorder = EventRecorder(storage, refine_req_q=refine_req_q, refine_resp_q=refine_resp_q)

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
