import sounddevice as sd
import soundfile as sf
import threading
import queue
import numpy as np

class AudioRecorder:
    def __init__(self):
        print("Initializing AudioRecorder...")
        self.recording = False
        self.q = queue.Queue()
        self.stream = None
        self.thread = None
        self.filename = None
        print("AudioRecorder initialized.")

    def start(self, filename):
        """Start recording audio to the specified filename."""
        if self.recording:
            return
        self.recording = True
        self.filename = filename
        self.q = queue.Queue()

        def callback(indata, frames, time, status):
            if status:
                print(f"Audio status: {status}")
            self.q.put(indata.copy())

        # Start stream (default device)
        try:
            self.stream = sd.InputStream(samplerate=44100, channels=1, callback=callback)
            self.stream.start()
        except Exception as e:
            print(f"Failed to start audio stream: {e}")
            self.recording = False
            return

        # Start writer thread
        self.thread = threading.Thread(target=self._write_file)
        self.thread.start()

    def stop(self):
        """Stop recording and ensure file is written. Returns filename."""
        if not self.recording:
            return None

        self.recording = False
        if self.stream:
            self.stream.stop()
            self.stream.close()

        if self.thread:
            self.thread.join()

        return self.filename

    def _write_file(self):
        """Worker to write audio data to file."""
        if not self.filename:
            return

        try:
            with sf.SoundFile(self.filename, mode='w', samplerate=44100, channels=1) as file:
                while True:
                    try:
                        # If not recording and queue empty, we are done
                        if not self.recording and self.q.empty():
                            break

                        data = self.q.get(timeout=0.1)
                        file.write(data)
                    except queue.Empty:
                        continue
        except Exception as e:
            print(f"Error writing audio file: {e}")
