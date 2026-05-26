# Replay Execution Core Rules and Guidelines

These rules apply to all replay operations. Read this file first to understand the execution environment, runtime contract, and guidelines.

## Environment Details
- **Browser Skill / Harness** — read the browser-harness folder (it has a `SKILL.md` file) to understand the APIs and helpers available for driving Chrome via CDP.
- **Computer-use tools (`mcp__cua__*`)** — attached to THIS session for last-resort native-macOS control. Discover and call these tools directly (`computer_screenshot`, `computer_find_element`, `computer_click`, `computer_type`, `computer_hotkey`, …) to drive native apps and hostile DOMs; screenshot first, act, screenshot again to verify.
  - **Standalone UI Agent Delegation**:
    In any custom script execution or manual triage helper, hand native-UI actions to the standalone UI Agent via the `$AI_MIME_UI_AGENT_CMD` environment variable. Never search the codebase, write custom selenium/click loops in Python, or import internal modules directly.
    - **Usage Example in Python**:
      ```python
      import os, shlex, subprocess, json
      
      ui_agent_cmd = os.environ.get("AI_MIME_UI_AGENT_CMD")
      task_prompt = "In the Weather application: 1. Click search, 2. Type 'Paris', 3. Press Enter."
      schema = {
          "type": "object",
          "properties": {"temperature": {"type": "number"}},
          "required": ["temperature"]
      }
      
      cmd = shlex.split(ui_agent_cmd) + [task_prompt, "--schema", json.dumps(schema), "--json"]
      proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
      result = json.loads(proc.stdout)
      print("Paris Temperature:", result["result_json"]["temperature"])
      ``` 
- **Bash** — for shelling out through app-managed tools.
- **WebSearch / WebFetch** — the open web.

## Python Runtime Contract
- Use `$AI_MIME_PYTHON_PATH` instead of bare `python` / `python3`.
- Use `$AI_MIME_UV_PATH` instead of bare `uv`.
- Use `$AI_MIME_BROWSER_HARNESS_BIN` instead of bare `browser-harness`.
- The skill's `run.sh` will resolve and use the existing `.venv` if one exists.

## Skill Directory Path
- The skill directory `{skill_dir}` is located at `<workflow_dir>/skills/<skill_name>/` under the workflow directory.
- Any targeted edits allowed during triage must be made directly within this skill directory.

## Conversation Style
- Keep user-facing messages BRIEF.
- Respond in the Replay page chat. Help run the existing skill, validate inputs, and handle variants of the task using the skill context.

## Task Transition Rule
- Start with `01_replay.md` to begin execution.
