# AI Mime

**Record, Reflect, and Replay workflows on macOS.**

## Installation

1.  Clone the repository.
2.  Install dependencies:
    ```bash
    pip install -e .
    ```

## Usage

### Record

Start the recording menubar app:

```bash
record
```

**Permissions:**
On first run, you will be prompted to grant permissions for:
-   **Accessibility**: Required to monitor global mouse/keyboard inputs.
-   **Screen Recording**: Required to capture screenshots.

**Controls:**
-   **Menubar**: Click "Start Recording" to begin.
-   **Push-to-Talk**: Hold **F9** to record voice notes.
-   **Stop**: Click "Stop Recording" in the menubar to finish and save.
-   **Special Keys**: `Enter`, `Tab`, `Esc`, and **`F4` (Search)** trigger immediate events.

### Output

Recordings are saved in `recordings/YYYYMMDD.../`:
-   `manifest.jsonl`: Event log (Action + Screenshot + Voice).
-   `metadata.json`: Session info.
-   `screenshots/`: Image files.
-   `audio/`: Voice clips.

### Reflect & Replay

(Coming Soon)
