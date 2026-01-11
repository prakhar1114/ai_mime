# Replay Design

Replay runs a compiled workflow (`workflows/<session_id>/schema.json`) by iterating over high-level subtasks and using a vision model to decide the next UI action on each loop iteration.

## Inputs
- **Workflow directory** containing:
  - `schema.json` (compiled by reflect/compile)
- **Runtime config** (`ReplayConfig`):
  - model + OpenAI-compatible base URL + API key

## Schema shape (v2)
- `schema.plan.subtasks[]`: ordered subtasks
  - `text`: human-readable instruction with expected outcome (may include `{param}` templates)
  - `dependencies`: names of required extracts from earlier subtasks (optional)
  - `steps[]`: coordinate-free “reference steps” used as examples for the model (not executed deterministically)
- `schema.task_params[]`: parameter specs used to materialize `{param}` templates

## Execution loop (core idea)
For each subtask:
- Capture a **live screenshot** each iteration.
- Build a compact prompt including:
  - overall task + current subtask
  - dependency context (extract values from prior subtasks)
  - cross-subtask `task_memory`
  - per-subtask recent `history`
  - `reference_steps` derived from the schema’s step cards
- Ask the model for exactly one tool call:
  - `computer_use`: mouse/keyboard action
  - `extract`: request a value from the screenshot without UI interaction
  - `done`: mark current subtask complete
- Execute the action (for `computer_use`), record artifacts, and continue until `done`.

## Materialization (no LLM)
Before execution, replay:
- resolves `task_params` using schema examples + user overrides
- renders `{param}` templates into concrete strings for this run
- writes the rendered schema to `<workflow_dir>/.replay/<timestamp>/schema.rendered.json` for debugging

## Artifacts written per run
Under `<workflow_dir>/.replay/<timestamp>/`:
- `schema.rendered.json`
- `subtask_<subtask_idx>_iter_<iter>.png` (screenshots captured during replay)
- `events.jsonl` (append-only event log: tool calls, actions, errors, done)
- `task_memory.json` (latest cross-subtask memory)
- `extracts.json` (captured extract variables)

## Components
- `ai_mime.replay.engine`
  - run directory creation, schema load + materialization, subtask loop, artifact logging
- `ai_mime.replay.grounding`
  - OpenAI-compatible vision call + tool-call parsing/validation
  - maps 0..1000 coordinates into screenshot pixel coordinates
- `ai_mime.replay.os_executor`
  - executes normalized mouse/keyboard actions on macOS (pynput)
