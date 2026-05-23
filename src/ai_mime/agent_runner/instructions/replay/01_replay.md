# Replay Execution Process

## Instructions
1. **Read and learn**: Learn from the complete skill package before deciding how to recover or run: `SKILL.md`, `run.sh`, `scripts/run.py`, `inputs/inputs.example.json`, `inputs/inputs.template.json`, every file under `references/`, and especially `references/fallback_plan.md`.
2. **Validate and normalize**: Validate and normalize the user's inputs before running anything. If an input is ambiguous or unsafe to infer, ask a short clarifying question.
3. **Execution**: Prefer `./run.sh <inputs.json>` as the primary execution path. It is cheap, runs the task end-to-end, and emits rich stdout/stderr progress logs.
4. **Track Progress**: Use stdout, stderr, and JSON progress events (`step_start`, `step_done`, `step_failed`, `workflow_done`) to explain progress, results, and failures.
5. **Handle Variants**: For task variants, use the script and skill context to automate the new task directly. You may create temporary input JSON files or run helper commands, but keep durable outputs under allowed output paths.
6. **Triage Failures**: If `./run.sh` fails or cannot cover the remaining task, triage before editing: classify the failure as likely environment/user-state issue, input issue, transient UI issue, or skill defect. Closed tabs, missing windows, changed focus, logged-out browser state, interrupted app state, and one-off UI disruption are recovery work, not skill repair.
7. **Triage Options**: Decide from the logs, script, skill docs, and `references/fallback_plan.md` how to complete the task. You may continue manually, restore expected UI state, rerun only the remaining work, or complete the task directly from the fallback plan.
8. **UI-agent Fallback**: For UI-only parts, call the `mcp__cua__*` computer-use tools directly. Prefer script/browser approaches when they are clear, but do not stop just because the original script failed.
9. **Notes**: You may append durable domain findings to `agent/replay_notes.md` or `agent/domain_notes.md`. Keep these notes factual: selectors, URLs, payload shapes, input gotchas, and observed domain behavior.
10. **Triage Code Edits**: Targeted edits inside the skill directory are allowed only when there is clear evidence from `run.sh`, logs, `scripts/run.py`, or repeated deterministic failure that the skill package itself is stale, incomplete, or wrong. Only edit the skill when needed; do not rewrite `run.sh` or `scripts/run.py` just because the first run failed.
11. **Constraints**:
    - Do NOT edit `schema.json` or `optimized_plan.json`.
    - If completion is impossible with the available logs, skill, fallback plan, and UI-agent fallback, explain the concrete blocker and what user action is needed.
