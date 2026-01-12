# Reflect Design (Record → Reflect → Compile)

`reflect` converts a raw recording session (`recordings/<session_id>/`) into a reusable workflow directory (`workflows/<session_id>/`) and (optionally) compiles a replayable `schema.json`.

## Inputs
- **Recording session** (`recordings/<session_id>/`)
  - `manifest.jsonl`: event log (each event references a pre-action screenshot path)
  - `metadata.json`: task name/description
  - `screenshots/`: `current_screenshot.png`, `pretyping_screenshot.png`, and frozen `N.png`

## Outputs
- **Workflow dir** (`workflows/<session_id>/`)
  - `metadata.json`: copied from the session
  - `manifest.jsonl`: cleaned/normalized
  - `screenshots/`: copied; screenshots referenced by click events may be annotated (dotted box)

## Current behavior (important)
- If `workflows/<session_id>/` already exists, `reflect_session()` currently **returns early** and does not refresh copied assets.

## Manifest cleanup rules (high level)
- Drops the final “Stop Recording” click.
- Converts the new last event into a “no-op” (null action) so the trace ends in a stable UI state.

## Compilation (workflow schema)
Compilation is handled by `compile_workflow_schema()` (two-pass pipeline) and writes:
- `step_cards.json`: Pass A output (resumable; missing steps are compiled)
- `schema.draft.json`: Pass B output merged with the plan structure
- `schema.json`: final schema used by replay

### Pass A (StepCards)
- Produces **coordinate-free** steps from events + screenshots.
- Uses **PRE = event screenshot**, **POST = next event screenshot** (for `EXTRACT`, PRE=POST to avoid inventing UI change).
- Supports action types: `CLICK`, `TYPE`, `SCROLL`, `KEYPRESS`, `DRAG`, `EXTRACT`.
- Carries through `details` (if recorded) to help later compilation.
- For `EXTRACT`, assigns `variable_name` programmatically (`extract_0`, `extract_1`, ...).

### Pass B (Task compiler)
- Produces `task_params`, `success_criteria`, and high-level subtasks with `{param}` placeholders.
- Parameterizes step `action_value` (e.g., `TYPE` values) using single-brace templates like `{query}`.
- Produces per-step subtask assignment and merges everything into a **v2 plan format**:
  - `schema.plan.subtasks[]` where each has `text`, optional `dependencies`, and `steps[]`.
  - `EXTRACT` steps preserve the refined query under `step.additional_args.extract_query`.
