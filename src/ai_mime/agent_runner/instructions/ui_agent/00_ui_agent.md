# UI Agent Recipe Guide

Use this guide only when a workflow step genuinely needs the UI agent. It is an
authoring guide for producing compact task-specific prompts for
`$AI_MIME_UI_AGENT_CMD`; do not paste this whole guide into the prompt.

## Goal
The UI agent should receive a distilled execution recipe, not a generic recording
or a transcript of exploration. A good recipe tells it what the desired end state
is, what is already known about the app, where it should be careful, and how to
verify completion.

## What to Discover
During exploration, record durable facts that help the next UI-agent run avoid
repeating the same mistakes:

- Target app identity: app name, bundle id if known, and whether web/native
  variants must be avoided.
- Deterministic setup: commands, deep links, menus, or app state that can be
  prepared before visual work starts.
- Expected starting state: what screen, pane, selected item, or visible landmark
  indicates the task is ready to continue.
- Stable landmarks: labels, placeholders, headings, icons, relative positions,
  and visible text that help locate controls.
- Decision points: where the agent must choose among results, tabs, contacts,
  files, or dialogs, and the exact matching rule.
- Pitfalls and gotchas: focus changes, stale windows, slow rendering, ambiguous
  duplicate labels, shortcuts that do not work, or controls that only appear
  after typing.
- Skip conditions: parts of the flow that can be skipped when the desired state
  is already visible.
- Recovery path: what to do if the wrong app is foreground, a result is missing,
  focus is uncertain, or an expected panel is not visible.
- Final verification: the exact visible evidence that proves success.

## Learned Notes Format
For each `ui_agent` step, write learned notes as a technical recipe:

```md
### UI Agent Recipe
- Target: <native app / browser / dialog and any app identity constraints>
- Intent: <user-visible outcome>
- Setup: <deterministic preparation before UI work>
- Start state: <what should be visible before acting>
- Actions:
  1. <step with stable landmark or matching rule>
  2. <step>
- Decisions: <selection rules or "none">
- Pitfalls: <what failed, what not to repeat, why>
- Skip if already true: <state checks that allow jumping ahead>
- Recovery: <short fallback steps>
- Verify: <exact final success evidence>
```

Keep this factual. Do not write a diary of attempts. Include failures only when
they change the future recipe.

## Prompt Shape for `$AI_MIME_UI_AGENT_CMD`
The generated UI-agent prompt should be concise and operational. Include the
recipe facts the agent needs, not the full learned notes file.

Recommended prompt sections:

1. **Target and constraints**: name the app/surface and any variants to avoid.
2. **Goal**: state the final user-visible outcome.
3. **Known setup**: include deterministic preparation or expected foreground
   state if known.
4. **Optimized action sequence**: list the shortest reliable path, including
   skip conditions when the target state is already visible.
5. **Selection rules**: explain how to pick among ambiguous options.
6. **Gotchas**: include known "do not do this" behavior and focus risks.
7. **Recovery**: say how to recover if the app/focus/state is wrong.
8. **Verification**: describe the final visible proof required before reporting
   success.

## What Not to Do
- Do not pass a bare recording such as "click search, type text, press enter"
  when exploration found focus or activation traps.
- Do not tell the UI agent to redo setup that is already known to be complete;
  include a skip condition instead.
- Do not let it type when focus is uncertain. Require it to inspect enough state
  to confirm the target input is active or re-focus the target surface first.
- Do not repeat exploratory checks after the app state is clear. Tell the agent
  which verification points matter.
- Do not copy tool catalogs or MCP function descriptions into the recipe. The UI
  agent can discover available tools from the MCP server. Mention a specific
  method only when exploration proved it is necessary for this task.

## Quality Bar
A future run should be able to complete the UI portion from the generated prompt
without re-learning the same app behavior. If a previous run failed, the next
recipe must explicitly encode the lesson: what went wrong, how to avoid it, and
what evidence confirms the fix worked.
