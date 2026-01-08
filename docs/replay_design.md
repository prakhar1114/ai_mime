# Replay Design

This document describes how **replay** works in AI Mime as implemented in this repo: how we discover workflows, interpret `schema.json`, ground actions using a computer-use vision model, and execute those actions on macOS.

## High-level flow

1. **Workflow selection**
   - Replay operates on a workflow directory under `workflows/` that contains a `schema.json`.
   - The menubar app builds its Replay menu by scanning `workflows/` and listing workflow dirs with `schema.json`.

2. **Schema-driven plan execution**
   - Replay loads `schema.json` and iterates `schema.plan.steps` in order.
   - Steps are constrained by the schema’s `action_type` (e.g. `KEYPRESS`, `TYPE`, `CLICK`) and step metadata such as `target.primary` and `screen_hint`.

3. **Live screenshot capture**
   - Before each step, replay captures a **current screenshot of the primary display**.
   - Screenshots are written into `<workflow_dir>/.replay/step_<i>_screen.png` for debugging and postmortems.

4. **Model grounding (computer use)**
   - For each step, replay sends the model:
     - the **current screenshot** (base64 data URL)
     - a text prompt containing task context + the current step details
   - The prompt includes:
     - `task_name`, `task_description_user`, `detailed_task_description`
     - `success_criteria`
     - resolved parameters (e.g. `{query}` → `"numb"`)
     - step index, intent, `target.primary`, `screen_hint`, `post_change`, `error_signals`
     - a **constraint** telling the model which `computer_use` action must be returned for this schema step

5. **Action execution**
   - The model returns exactly one `<tool_call>` for a `computer_use` action.
   - If the action includes `coordinate: [x, y]` in a **0..1000 reference frame**, replay maps it to screenshot pixels.
   - Replay executes the action on macOS using `pynput` for keyboard/mouse actions.

6. **Completion**
   - After the final schema step, replay logs `Task Complete`.
   - When replay is started from the menubar app, the replay subprocess also shows a macOS notification titled **Task Complete** and exits.

## Components and responsibilities

### `ai_mime.app`

- Provides the menubar app.
- Builds a **Replay** submenu by listing workflows with `schema.json`.
- Starts replay in a background process so the UI stays responsive.
- Shows notifications for replay start, replay failure, and replay completion.

### `ai_mime.replay.catalog`

- Workflow discovery utilities:
  - lists workflow directories under `workflows/` that contain `schema.json`
  - resolves a selected workflow directory

### `ai_mime.replay.engine`

- Core schema execution loop:
  - loads `schema.json`
  - resolves `task_params` using schema examples (and optional overrides where supported)
  - iterates `schema.plan.steps`
  - captures screenshots per step
  - builds the model prompt for each step
  - executes the model’s returned action

### `ai_mime.replay.grounding`

- Model client + parsing:
  - calls DashScope’s OpenAI-compatible endpoint with `DASHSCOPE_API_KEY`
  - provides a “computer_use” tool schema in the system prompt
  - parses `<tool_call>...</tool_call>` into a structured action
  - converts 0..1000 coordinates into pixel coordinates using the screenshot size

### `ai_mime.replay.os_executor`

- OS action executor (macOS):
  - executes `computer_use` actions like `key`, `type`, `mouse_move`, `left_click`, `double_click`, `scroll`, `wait`
  - implemented with `pynput`

### `ai_mime.screenshot`

- Provides `ScreenshotRecorder` used by both record and replay.
- Captures the primary monitor to a file via `mss`.

## Model integration details

- **API key**: read from environment variable `DASHSCOPE_API_KEY`.
- **Base URL**: `https://dashscope-intl.aliyuncs.com/compatible-mode/v1`
- **Model**: configured for replay as `qwen3-vl-plus-2025-12-19`
- **Tool-call format**: model is instructed to return:

```text
<tool_call>
{"name":"computer_use","arguments":{...}}
</tool_call>
```

## Files written by replay

- `<workflow_dir>/.replay/step_<i>_screen.png`
  - The screenshot used for grounding step `i`.
  - Useful for debugging why a particular action was predicted/executed.
