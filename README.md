# AI Mime

**Record, Reflect, and Replay workflows on macOS.**

## Installation

1.  Clone the repository.
2.  Install dependencies:
    ```bash
    pip install -e .
    ```

## Permissions (Critical)

To function correctly, `ai-mime` requires macOS permissions. Because you are likely running this from a Python virtual environment, you must grant permissions to the **Python binary** inside your `.venv`.

1.  **Run `record` once**: The app will attempt to check permissions and may trigger a system prompt.
2.  **Open System Settings**: Go to **Privacy & Security**.
3.  **Grant Permissions**:
    *   **Accessibility**: Required to monitor global mouse/keyboard inputs. Add your Terminal app (e.g., iTerm, Terminal) AND the python binary from your venv if prompted.
    *   **Screen Recording**: Required to capture screenshots. Add your Terminal app / Python binary.
    *   **Microphone**: Required for voice notes.
Use Cmd+Shift+G when trying to add the python binary in settings.

*Note: If you see "Terminal" in the list but it still doesn't work, try removing it and re-adding it, or run the script from a dedicated terminal window.*

## Usage

### Record

Start the recording menubar app:

```bash
record
```

**Controls:**
-   **Menubar**: Click "Start Recording" to begin.
-   **Push-to-Talk**: Hold **F9** to record voice notes.
-   **Stop**: Click "Stop Recording" in the menubar to finish and save.
-   **Special Keys**: `Enter`, `Tab`, `Esc`, `F4`, and `Cmd+Space` trigger immediate events.

### Output

Recordings are saved in `recordings/YYYYMMDD.../`:
-   `manifest.jsonl`: Event log (Action + Screenshot + Voice).
-   `metadata.json`: Session info.
-   `screenshots/`: Image files (0.png, 1.png...).
-   `audio/`: Voice clips (0.wav, 1.wav...).

### Reflect & Replay

(Coming Soon)
