# Screenshot ↔ Event Pairing Design

This document describes how `ai_mime` records screenshots and pairs them with user actions in `manifest.jsonl`.

## Goals

- Each recorded event references the **most recent screenshot captured _before_ the action** (a “pre-action” frame).
- The visual **result** of an action is expected to appear in the **next** screenshot (used by the next event).
- When idle (no actions), we still keep the “current” screenshot fresh by overwriting it on a fixed interval.
- Typing is handled so the `type` event references the **pre-typing** state (e.g. empty search bar), not the post-typing state.

## Files on disk

Within a session directory:

- `screenshots/current_screenshot.png`
  - Continuously overwritten every **500ms** by a background updater.
  - Written atomically (capture to temp + `os.replace`) to avoid partial reads.
- `screenshots/pretyping_screenshot.png`
  - Overwritten once per typing burst.
  - Captured from `current_screenshot.png` at the moment typing begins.
- `screenshots/{N}.png`
  - Frozen per-event screenshot files referenced by events in `manifest.jsonl`.
  - Created by **copying** `current_screenshot.png` or `pretyping_screenshot.png` (never renaming the source).

## Core rule: “freeze right before the event”

For non-typing actions (`click`, `scroll`, special `key`):

1. Ensure any pending typing is flushed.
2. **Freeze** the latest `current_screenshot.png` into the next numbered file (copy under a lock).
3. Write the event referencing that numbered screenshot path.
4. Optionally force-refresh `current_screenshot.png` immediately after the action, so the “result” becomes available sooner for the next event.

### “Start screenshot” (no explicit start event)

There is **no explicit** `start` line in the manifest.

Instead, the **first event** freezes the current pre-action frame and that becomes `screenshots/0.png` automatically.

## Typing behavior

Typing is buffered into a burst and recorded as a single `type` event when the buffer is flushed.

- On first typed character of a burst:
  - Copy `current_screenshot.png` → `pretyping_screenshot.png` (this is the pre-typing frame).
- On `flush_typing()`:
  - Freeze `pretyping_screenshot.png` → `screenshots/{N}.png`
  - Write a `type` event referencing `screenshots/{N}.png`
  - Force-refresh `current_screenshot.png` so the post-typing state (text visible) is available for the next action.

Special handling:

- Space is treated as a normal character (when Cmd is not held).
- Backspace/delete edits the typing buffer (does not emit a separate key event).

## Concurrency safety

- The updater’s write (`os.replace`) and all reads/copies used to freeze screenshots share the same lock.
- `SessionStorage.copy_file()` also includes a small retry loop to handle edge cases around startup/stop timing.
