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

1.  **Run `start_app` once**: The app will attempt to check permissions and may trigger a system prompt.
2.  **Open System Settings**: Go to **Privacy & Security**.
3.  **Grant Permissions**:
    *   **Accessibility**: Required to monitor global mouse/keyboard inputs. Add your Terminal app (e.g., iTerm, Terminal) AND the python binary from your venv if prompted.
    *   **Screen Recording**: Required to capture screenshots. Add your Terminal app / Python binary.
    *   **Microphone**: Required for voice notes.
Use Cmd+Shift+G when trying to add the python binary in settings.

*Note: If you see "Terminal" in the list but it still doesn't work, try removing it and re-adding it, or run the script from a dedicated terminal window.*

## Environment keys (.env)

Create a `.env` file (or export env vars) with:

```bash
OPENAI_API_KEY=
DASHSCOPE_API_KEY=
LMNR_PROJECT_API_KEY=
```

- `OPENAI_API_KEY`: used for schema compilation (reflect).
- `DASHSCOPE_API_KEY`: used for replay action grounding via DashScope’s OpenAI-compatible endpoint.
- `LMNR_PROJECT_API_KEY`: used for Laminar tracing/telemetry (if enabled in your environment).

## Usage

### Start app (record + replay UI)

Start the menubar app:

```bash
start_app
```

#### Start recording
- In the menubar app, click **Start Recording**.

#### During recording
- **Push-to-Talk**: hold **F9** to record voice notes (not implemented yet).
- **Captured inputs**: clicks, scrolls, typing bursts, and special keys (`Enter`, `Tab`, `Esc`, `Cmd+Space`).

#### Stop recording
- Click **Stop Recording**.
- The recorder stops immediately.
- **Reflect runs in the background**: it converts the raw recording into a workflow under `workflows/<session_id>/` and compiles `schema.json` (so you can replay). Do not terminate the process as Reflect runs in the background and takes some time to execute.

#### Replay a specific recording
- Open the menubar app → **Replay** → choose the workflow you want to run (workflows are discovered by scanning `workflows/` for folders that contain `schema.json`).

### Output

#### Record output
Recordings are saved in `recordings/<session_id>/`:
-   `manifest.jsonl`: Event log (Action + Screenshot + Voice).
-   `metadata.json`: Session info.
-   `screenshots/`: Image files (0.png, 1.png...).
-   `audio/`: Voice clips (0.wav, 1.wav...).

#### Reflect output
Workflows are saved in `workflows/<session_id>/`:
- `manifest.jsonl`: Cleaned manifest for replay/compilation.
- `metadata.json`: Copied session metadata.
- `screenshots/`: Screenshots copied into the workflow.
- `schema.json`: The final, replayable plan schema.

### CLI: Reflect

If you want to run reflect manually on an existing recording:

```bash
reflect --session <session_id>
```



## Glossary

- **Record**: capture a live session into `recordings/<session_id>/` (events + screenshots + audio).
- **Reflect**: transform a recording into a reusable workflow under `workflows/<session_id>/` and compile `schema.json`.
- **Replay**: execute `schema.json.plan.steps` on macOS; the model predicts the concrete GUI actions each step using the current screenshot.
