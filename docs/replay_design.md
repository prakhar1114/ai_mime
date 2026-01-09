# Replay Design

This document describes how **replay** works in AI Mime as implemented in this repo: how we discover workflows, interpret `schema.json`, ground actions using a computer-use vision model, and execute those actions on macOS.

## High-level flow

1. **Workflow selection**
   - Replay operates on a workflow directory under `workflows/` that contains a `schema.json`.
   - The menubar app builds its Replay menu by scanning `workflows/` and listing workflow dirs with `schema.json`.

2. **Parameter entry (menubar UI)**
   - When the user clicks `Replay → <workflow>`, the app reads `schema.json.task_params`.
   - A single “form” window is shown with defaults, as multiline `key=default` (one per line).
   - If the user leaves a value blank (`key=`), the default example from the schema is used.
   - If the user cancels, replay does not start.

3. **Schema materialization (no LLM)**
   - Replay resolves parameters using schema defaults + user overrides.
   - Replay then **materializes** the schema by rendering `{param}` templates into concrete strings for this run:
     - `schema.subtasks[]`
     - `plan.steps[].action_value` (when present)
   - The materialized schema is written for debugging as `schema.rendered.json`.

4. **Per-run output directory**
   - Each replay run creates a unique directory:
     - `<workflow_dir>/.replay/<timestamp>/`
   - All run artifacts are stored inside it (screenshots, logs, rendered schema, memory).

5. **Subtask-driven execution loop**
   - Replay does **not** iterate exact `plan.steps` as a deterministic script.
   - Instead it loops over `schema.subtasks` (each subtask includes the expected outcome in its text).
   - For each subtask:
     - Replay captures a live screenshot each iteration.
     - Replay provides the model:
       - overall task + the current subtask (and expected outcome)
       - resolved params
       - cross-subtask `task_memory` (carried forward)
       - per-subtask `history` (recent actions + observations for this subtask only)
       - `reference_steps` examples derived from `schema.plan.steps` for that subtask
   - The model iterates until it calls `done` for the current subtask, then replay advances to the next subtask.

6. **Completion**
   - After the final subtask, replay logs `Task Complete`.
   - When replay is started from the menubar app, the replay subprocess also shows a macOS notification titled **Task Complete** and exits.

## Components and responsibilities

### `ai_mime.app`

- Provides the menubar app.
- Builds a **Replay** submenu by listing workflows with `schema.json`.
- Starts replay in a background process so the UI stays responsive.
- Shows notifications for replay start, replay failure, and replay completion.
- Prompts the user for parameters using a single multiline `key=default` form.

### `ai_mime.replay.catalog`

- Workflow discovery utilities:
  - lists workflow directories under `workflows/` that contain `schema.json`
  - resolves a selected workflow directory

### `ai_mime.replay.engine`

- Core replay loop:
  - loads `schema.json`
  - resolves `task_params` using schema examples + user overrides
  - materializes `{param}` templates into a run-specific `schema.rendered.json`
  - creates `<workflow_dir>/.replay/<timestamp>/` for all run artifacts
  - loops over `schema.subtasks`
  - within each subtask, runs a model-driven iteration loop until the model returns `done`
  - maintains `task_memory` (cross-subtask) and `history` (per-subtask)

### `ai_mime.replay.grounding`

- Model client + parsing:
  - calls DashScope’s OpenAI-compatible endpoint with `DASHSCOPE_API_KEY`
  - provides `computer_use` and `done` tool schemas in the system prompt
  - parses `<tool_call>...</tool_call>` into a structured action
  - converts 0..1000 coordinates into pixel coordinates using the screenshot size
- validates required fields (`observation` and `task_memory`) and retries on invalid tool calls

### `ai_mime.replay.os_executor`

- OS action executor (macOS):
  - executes `computer_use` actions like `key`, `type`, `mouse_move`, `left_click`, `double_click`, `scroll`, `wait`
  - implemented with `pynput`

### `ai_mime.screenshot`

- Provides `ScreenshotRecorder` used by both record and replay.
- Captures the primary monitor to a file via `mss`.

## Model integration details

- **Provider**: `REPLAY_PROVIDER` selects one of `openai`, `gemini`, `dashscope`.
- **API key**: selected by provider:
  - `openai` → `OPENAI_API_KEY`
  - `gemini` → `GEMINI_API_KEY` (docs: [Gemini OpenAI compatibility](https://ai.google.dev/gemini-api/docs/openai))
  - `dashscope` → `DASHSCOPE_API_KEY`
- **Base URL**: automatically set from the provider:
  - `openai`: `https://api.openai.com/v1`
  - `gemini`: `https://generativelanguage.googleapis.com/v1beta/openai/`
  - `dashscope`: `https://dashscope-intl.aliyuncs.com/compatible-mode/v1`
- **Model**: `REPLAY_MODEL` is required (provider-specific model name).
- **Tool-call format**: model is instructed to return:

```text
<tool_call>
{"name":"computer_use","arguments":{...}}
</tool_call>
```

## Files written by replay

- `<workflow_dir>/.replay/<timestamp>/schema.rendered.json`
  - Materialized schema for this run (after applying params).
- `<workflow_dir>/.replay/<timestamp>/subtask_<subtask_idx>_iter_<iter>.png`
  - Screenshot captured for each subtask iteration.
- `<workflow_dir>/.replay/<timestamp>/events.jsonl`
  - Append-only log of model tool calls and done results.
- `<workflow_dir>/.replay/<timestamp>/task_memory.json`
  - The latest cross-subtask memory string (updated each iteration).
