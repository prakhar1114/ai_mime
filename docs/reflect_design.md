# Reflect Design (Record → Reflect → Reuse)

### Purpose
`reflect` converts a raw recording session (`recordings/<session_id>/`) into a reusable, coordinate-free workflow schema in `workflows/<session_id>/`, suitable for rerunning with a vision agent.

---

### Inputs
- **Recording session folder**: `recordings/<session_id>/`
  - `manifest.jsonl`: event log (action + pre-action screenshot path + metadata)
  - `metadata.json`: `name`, `description`, etc.
  - `screenshots/`: numbered screenshots (plus helper frames during recording)

---

### Output folder layout
`workflows/<session_id>/` contains:
- **Reflected assets (regenerated each reflect run)**:
  - `manifest.jsonl` (cleaned)
  - `metadata.json` (copied)
  - `screenshots/` (copied; click frames may be annotated)
- **Compiled artifacts (preserved across reflect runs)**:
  - `step_cards.json` (Pass A checkpoint)
  - `schema.draft.json` (Pass B checkpoint)
  - `schema.json` (final schema used for reruns)

Important: `reflect_session()` refreshes `manifest.jsonl`, `metadata.json`, and `screenshots/` but **does not delete** `step_cards.json` / `schema*.json`, enabling resumable compilation.

---

### Step pairing model (PRE/POST)
The recorder stores **pre-action** screenshots by design:
- For an actionable event at index `k`:
  - `PRE = events[k].screenshot`
  - `POST = events[k+1].screenshot` (if present)

This means we infer post-action visual change from the next recorded pre-action frame.

---

### Pass A — StepCards (per-step, vision + action)
**Goal**: Convert each recorded UI event into a reusable, coordinate-free instruction step.

**Input per step**:
- Task name + user description (from `metadata.json`)
- Action ground truth (from `manifest.jsonl`):
  - `action_type` mapped into: `CLICK | TYPE | SCROLL | KEYPRESS | DRAG`
  - `action_details` (e.g., typed text, key name, etc.)
- Screenshots: PRE + POST (2 images; derived as above)

**Output**: `step_cards.json` (ordered list of StepCards)
- Each StepCard describes:
  - `expected_current_state`: a generalized description of the current UI state where the action should be performed (app/window/view/focus), not exact on-screen text.
  - `intent`: 1 sentence describing why the user performs the step.
  - `target`: coordinate-free selector (`primary`, optional `fallback`).
  - `action_type` and (when applicable) `action_value` for `TYPE` / `KEYPRESS`.
  - `post_action`: 1–2 lines describing what changed after the action (generic outcome, not the specific parameter/value used).
- Pass A **does not parameterize** values. Typed values are kept literal in `action_value`.

**Reliability + performance**
- Runs **up to 5 concurrent** OpenAI requests at a time.
- Uses Structured Outputs (`responses.parse(..., text_format=StepCardModel)`) to enforce JSON shape.
- **Retries**: up to 2 retries per step on failure; retries append the failure + previous output into the same message thread to self-correct.

**Checkpointing**
- Writes partial `step_cards.json` as steps complete.
- On rerun, loads existing `step_cards.json` and only compiles missing step indices.
- Persistence is merge-safe: it merges with on-disk results before writing to avoid shrinking/overwriting.

---

### Pass B — Task schema (text-only compile)
**Goal**: Produce a compact reusable workflow schema from StepCards.

**Input**
- Task name + user description
- Step summaries derived from StepCards (ordered JSON array of `{i, intent, action_type, action_value}`)

**Output**
- Pass B produces task-level fields:
  - `detailed_task_description` (string)
  - `task_params` (parameter specs with clear descriptions + examples)
  - `subtasks` (list of higher-level units that group multiple steps; each subtask includes `{param}` placeholders where relevant and an explicit “Expected outcome: …”)
  - `success_criteria` (single generalized string)
- Pass B also outputs per-step updates (internal to compilation) that:
  - parameterize step `action_value` using single-brace templates like `{song_name}` where appropriate
  - assign each step to a high-level `subtask` (typically grouping ~4–6 steps in one window/app context)
- Final `schema.json` is written as:
  - `task_name`, `task_description_user`
  - Pass B output fields
  - `plan.steps`: based on Pass A StepCards (authoritative executable plan), augmented with:
    - parameterized `action_value` (when applicable)
    - `subtask` for each step

**Checkpointing**
- Writes `schema.draft.json` after Pass B succeeds.
- On rerun, if `schema.json` already has `plan.steps`, compilation is skipped.
- If only `schema.draft.json` exists, Pass B is skipped and we proceed to final write (with `plan.steps` attached).

---

### CLI flow
`reflect --session <session_id>`:
1. `reflect_session()` refreshes reflected assets in `workflows/<session_id>/`
2. `compile_workflow_schema()`:
   - Pass A (resumable) → `step_cards.json`
   - Pass B → `schema.draft.json`
   - Final write → `schema.json`

Logging is enabled at INFO level for visibility into pass progress and failures.

---

### Rerun behavior (what makes it fast)
On rerun, the compiler reuses:
- Completed StepCards from `step_cards.json`
- Completed task compilation from `schema.draft.json`
- Final schema from `schema.json` (if present and already contains `plan.steps`)

This minimizes repeated LLM calls and makes “fix-and-rerun” practical.
