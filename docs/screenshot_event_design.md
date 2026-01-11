# Screenshot ↔ Event Pairing (Recording)

Recording produces a `manifest.jsonl` where each event references a **stable, pre-action screenshot**. This lets reflect/replay reason about “what the user saw right before acting”.

## Files written during a recording session
Under `recordings/<session_id>/screenshots/`:
- `current_screenshot.png`: continuously refreshed snapshot of the primary display (overwritten)
- `pretyping_screenshot.png`: snapshot captured at the start of a typing burst (overwritten per burst)
- `{N}.png`: frozen screenshots referenced by manifest events

## Core rules
- **Pre-action frame**: every recorded event points at a screenshot frozen *right before* the action.
- **Post-action frame**: the *next event’s* pre-action screenshot is typically the best approximation of the prior action’s visible result.
- **Atomicity**: `current_screenshot.png` is updated via “capture to temp + `os.replace`” and freezing/copying is guarded by a shared lock to avoid partial reads.

## Event flow (high level)
- **Click / Scroll / Special keys**:
  - flush pending typing (if any)
  - freeze `current_screenshot.png` to `{N}.png`
  - write event with `screenshot: "screenshots/{N}.png"`
  - force-refresh `current_screenshot.png` so the next event sees updates sooner

- **Typing**:
  - on first character: copy `current_screenshot.png` → `pretyping_screenshot.png` (captures the empty/untyped state)
  - buffer characters
  - on flush: freeze `pretyping_screenshot.png` to `{N}.png` and write a single `type` event
  - then force-refresh `current_screenshot.png` so subsequent actions see typed text

## Refinement / extraction during recording
Recording supports an interactive “refine” flow (triggered by **Ctrl+I**) that can:
- write an `extract` event (with a frozen pre-action screenshot + user-provided query/values), or
- attach freeform `details` text to the *next* recorded event.
