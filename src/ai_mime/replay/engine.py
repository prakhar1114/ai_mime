from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


class ReplayError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReplayConfig:
    model: str
    base_url: str
    api_key: str | None
    delay_s: float = 0.35
    click_delay_s: float = 0.2
    after_step_delay_s: float = 0.4
    dry_run: bool = False
    ground_clicks: bool = True
    predict_all_actions: bool = True


_SINGLE_BRACE_PARAM_RE = re.compile(r"\{([a-zA-Z0-9_]+)\}")


def load_schema(workflow_dir: str | os.PathLike[str]) -> dict[str, Any]:
    workflow_dir_p = Path(workflow_dir)
    schema_path = workflow_dir_p / "schema.json"
    if not schema_path.exists():
        raise ReplayError(f"schema.json not found: {schema_path}")
    try:
        return json.loads(schema_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise ReplayError(f"Failed to read schema.json: {e}") from e


def resolve_params(schema: dict[str, Any], overrides: dict[str, str] | None = None) -> dict[str, str]:
    overrides = overrides or {}
    params: dict[str, str] = {}

    task_params = schema.get("task_params") or []
    if not isinstance(task_params, list):
        raise ReplayError("schema.json.task_params must be a list")

    for p in task_params:
        if not isinstance(p, dict):
            continue
        name = p.get("name")
        if not isinstance(name, str) or not name:
            continue
        if name in overrides:
            params[name] = str(overrides[name])
            continue
        example = p.get("example")
        if example is None:
            raise ReplayError(f"Missing required param '{name}' and no example provided in schema.json")
        params[name] = str(example)

    # Include any extra overrides not declared in task_params (best-effort).
    for k, v in overrides.items():
        if k not in params:
            params[k] = str(v)

    return params


def _render_template(s: str | None, params: dict[str, str]) -> str | None:
    if s is None:
        return None
    if not isinstance(s, str):
        return str(s)
    # Schema uses single-brace templates like "{query}".
    needed = set(_SINGLE_BRACE_PARAM_RE.findall(s))
    missing = [k for k in sorted(needed) if k not in params]
    if missing:
        raise ReplayError(f"Missing params for template {missing} in string: {s}")
    try:
        return s.format(**params)
    except Exception as e:
        raise ReplayError(f"Failed to render template '{s}': {e}") from e


def iter_plan_steps(schema: dict[str, Any]) -> list[dict[str, Any]]:
    plan = schema.get("plan", {})
    steps = plan.get("steps", [])
    out: list[dict[str, Any]] = []
    for s in steps:
        if isinstance(s, dict):
            out.append(s)
    return out


def run_plan(
    workflow_dir: str | os.PathLike[str],
    *,
    params: dict[str, str],
    cfg: ReplayConfig,
    predict_tool_call: Callable[[Path, str, ReplayConfig], dict[str, Any]],
    tool_call_to_pixel_action: Callable[[Path, dict[str, Any]], dict[str, Any]],
    capture_screenshot: Callable[[Path], Path],
    exec_action: Callable[[dict[str, Any], ReplayConfig], None],
    log: Callable[[str], None] = print,
) -> None:
    schema = load_schema(workflow_dir)
    steps = iter_plan_steps(schema)
    workflow_dir_p = Path(workflow_dir)
    replay_dir = workflow_dir_p / ".replay"
    replay_dir.mkdir(parents=True, exist_ok=True)

    task_name = schema.get("task_name") or ""
    task_description_user = schema.get("task_description_user") or ""
    detailed_task_description = schema.get("detailed_task_description") or ""
    success_criteria = schema.get("success_criteria") or []

    def _sleep(dt: float) -> None:
        if dt and dt > 0:
            time.sleep(dt)

    for step in steps:
        i = step.get("i")
        action_type = step.get("action_type")
        intent = step.get("intent") or ""
        target = step.get("target") or {}
        target_primary = target.get("primary") if isinstance(target, dict) else None
        screen_hint = step.get("screen_hint") or ""

        # Values
        action_value = _render_template(step.get("action_value"), params)
        input_template = _render_template(step.get("input_template"), params)

        header = f"Step {i}: {action_type} — {intent}".strip()
        log(header)
        if target_primary:
            log(f"  target: {target_primary}")
        if screen_hint:
            log(f"  hint: {screen_hint}")

        _sleep(cfg.delay_s)

        # Always capture current screenshot so the model can ground actions.
        img_path = replay_dir / f"step_{i}_screen.png"
        img_path = capture_screenshot(img_path)

        # Build the user_query with full schema context each step (as requested).
        # We also constrain the model to produce an action compatible with the schema step.
        expected_action_value = action_value or input_template
        if action_type == "KEYPRESS" and not expected_action_value:
            raise ReplayError(f"Step {i}: KEYPRESS requires action_value")
        if action_type == "TYPE" and not expected_action_value:
            raise ReplayError(f"Step {i}: TYPE requires action_value or input_template")

        constraint = ""
        if action_type == "KEYPRESS":
            constraint = (
                f"Constraint: This step MUST be a key action. Output action=key with keys representing: {expected_action_value}."
            )
        elif action_type == "TYPE":
            constraint = (
                f"Constraint: This step MUST be a type action. Output action=type with text exactly: {expected_action_value}."
            )
        elif action_type == "CLICK":
            constraint = (
                "Constraint: This step MUST be a left click. Output action=left_click with coordinate [x,y] in 0..1000."
            )
        else:
            constraint = f"Constraint: Follow the schema action_type exactly: {action_type}."

        user_query = (
            f"Task name: {task_name}\n"
            f"Task description (user): {task_description_user}\n"
            f"Detailed task description: {detailed_task_description}\n"
            f"Success criteria: {success_criteria}\n"
            f"Resolved params: {params}\n\n"
            f"Current step index: {i}\n"
            f"Step intent: {intent}\n"
            f"Schema action_type: {action_type}\n"
            f"Schema target.primary: {target_primary}\n"
            f"Schema screen_hint: {screen_hint}\n"
            f"Schema post_change: {step.get('post_change')}\n"
            f"Schema error_signals: {step.get('error_signals')}\n"
            f"{constraint}\n\n"
            f"Return exactly one tool call."
        )

        tool_call = predict_tool_call(img_path, user_query, cfg)
        pixel_action = tool_call_to_pixel_action(img_path, tool_call)
        log(f"  model_action: {pixel_action}")

        _sleep(cfg.click_delay_s)
        if cfg.dry_run:
            log("  DRYRUN: not executing")
        else:
            exec_action(pixel_action, cfg)

        _sleep(cfg.after_step_delay_s)

    log("Task Complete")
