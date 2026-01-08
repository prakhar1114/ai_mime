from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from lmnr import observe


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


def _utc_timestamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _ensure_run_dir(workflow_dir: Path) -> Path:
    """
    Create a per-run directory under workflow_dir/.replay/<timestamp>/.
    """
    base = workflow_dir / ".replay" / _utc_timestamp()
    base.mkdir(parents=True, exist_ok=True)
    return base


def materialize_schema(schema: dict[str, Any], params: dict[str, str]) -> dict[str, Any]:
    """
    Render (materialize) any templated strings in schema.json using provided params.
    No LLM is used here.
    """
    out: dict[str, Any] = dict(schema)

    # Render subtasks
    subtasks_any = out.get("subtasks") or []
    if isinstance(subtasks_any, list):
        rendered: list[str] = []
        for s in subtasks_any:
            if s is None:
                continue
            rendered_s = _render_template(str(s), params)
            rendered.append(str(rendered_s) if rendered_s is not None else "")
        out["subtasks"] = rendered

    # Render plan step action_values
    plan = out.get("plan") or {}
    steps = plan.get("steps") or []
    if isinstance(plan, dict) and isinstance(steps, list):
        new_steps: list[dict[str, Any]] = []
        for s in steps:
            if not isinstance(s, dict):
                continue
            s2 = dict(s)
            s2["action_value"] = _render_template(s2.get("action_value"), params)
            # For debugging: also render subtask label if present, without overwriting the template.
            if isinstance(s2.get("subtask"), str):
                s2["subtask_rendered"] = _render_template(s2.get("subtask"), params)
            new_steps.append(s2)
        out["plan"] = {**plan, "steps": new_steps}

    return out


def iter_plan_steps(schema: dict[str, Any]) -> list[dict[str, Any]]:
    plan = schema.get("plan", {})
    steps = plan.get("steps", [])
    out: list[dict[str, Any]] = []
    for s in steps:
        if isinstance(s, dict):
            out.append(s)
    return out


@observe(name="replay_task")
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
    workflow_dir_p = Path(workflow_dir)
    run_dir = _ensure_run_dir(workflow_dir_p)

    # Load schema and materialize any {param} templates for this run (no LLM).
    schema = load_schema(workflow_dir)
    schema_rendered = materialize_schema(schema, params)
    (run_dir / "schema.rendered.json").write_text(json.dumps(schema_rendered, indent=2), encoding="utf-8")

    # Top-level task metadata (kept mostly for logging / prompt context).
    task_name = schema.get("task_name") or ""
    task_description_user = schema.get("task_description_user") or ""
    _ = schema.get("detailed_task_description") or ""
    _ = schema.get("success_criteria") or ""

    # From here on, rely only on the rendered schema (it contains all run-relevant strings).
    steps = iter_plan_steps(schema_rendered)
    subtasks_rendered: list[str] = schema_rendered.get("subtasks") or []

    def _sleep(dt: float) -> None:
        if dt and dt > 0:
            time.sleep(dt)

    # Per-run debug artifacts.
    events_path = run_dir / "events.jsonl"
    task_memory_path = run_dir / "task_memory.json"

    def _append_event(obj: dict[str, Any]) -> None:
        events_path.parent.mkdir(parents=True, exist_ok=True)
        with events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    def _persist_task_memory(mem: str) -> None:
        task_memory_path.write_text(json.dumps({"task_memory": mem}, indent=2), encoding="utf-8")

    # Cross-subtask memory carried forward (updated by model each iteration).
    task_memory = ""
    _persist_task_memory(task_memory)

    max_iters_per_subtask = 30

    def _step_subtask_key(step: dict[str, Any]) -> str | None:
        st = step.get("subtask_rendered") or step.get("subtask")
        return st if isinstance(st, str) and st.strip() else None

    def _build_reference_steps_by_subtask() -> dict[str, list[dict[str, Any]]]:
        """
        Build a mapping: rendered_subtask -> list of reference step summaries.
        """
        out: dict[str, list[dict[str, Any]]] = {s: [] for s in subtasks_rendered}
        for s in steps:
            if not isinstance(s, dict):
                continue
            st = _step_subtask_key(s)
            if not st or st not in out:
                continue
            target = s.get("target") or {}
            target_primary = target.get("primary") if isinstance(target, dict) else None
            out[st].append(
                {
                    "i": s.get("i"),
                    "intent": s.get("intent"),
                    "action_type": s.get("action_type"),
                    "action_value": s.get("action_value"),
                    "target_primary": target_primary,
                    "expected_current_state": s.get("expected_current_state"),
                    "post_action": s.get("post_action"),
                }
            )
        return out

    reference_steps_by_subtask = _build_reference_steps_by_subtask()

    # Main loop: iterate subtasks, run until the model calls done(), then move to next.
    for subtask_idx, subtask_text in enumerate(subtasks_rendered):
        reference_steps = reference_steps_by_subtask.get(subtask_text, [])

        log(f"Subtask {subtask_idx + 1}/{len(subtasks_rendered)}: {subtask_text}")
        if task_description_user:
            log(f"  task: {task_description_user}")

        history: list[dict[str, Any]] = []

        for it in range(max_iters_per_subtask):
            _sleep(cfg.delay_s)

            img_path = run_dir / f"subtask_{subtask_idx:02d}_iter_{it:03d}.png"
            img_path = capture_screenshot(img_path)

            # Keep prompt brief and action-oriented.
            user_query = (
                f"Overall Task: {task_name}\n"
                f"Current subtask and expected outcome: {subtask_text}\n"
                f"Params: {params}\n"
                f"Task memory: {task_memory}\n"
                f"History (this subtask): {history[-8:]}\n"
                f"Reference steps (examples) from previous runs: {reference_steps}\n\n"
                "Decide ONE next action to progress the current subtask, or call done if the expected outcome is met.\n"
                "If you call computer_use, include a current-step specific observation and an updated task_memory.\n"
                "If you call done, include result (what was achieved / info to pass) and updated task_memory.\n"
                "Return exactly one tool call."
            )

            tool_call = predict_tool_call(img_path, user_query, cfg)
            name = tool_call.get("name")
            args = tool_call.get("arguments") or {}

            if name == "done":
                result = args.get("result")
                task_memory = str(args.get("task_memory") or "")
                _append_event(
                    {
                        "type": "done",
                        "subtask_idx": subtask_idx,
                        "iter": it,
                        "result": result,
                        "task_memory": task_memory,
                    }
                )
                _persist_task_memory(task_memory)
                log(f"  done: {result}")
                break

            pixel_action = tool_call_to_pixel_action(img_path, tool_call)
            observation = pixel_action.get("observation")
            task_memory = str(pixel_action.get("task_memory") or "")

            history.append({"action": pixel_action.get("action"), "observation": observation})
            _append_event(
                {
                    "type": "computer_use",
                    "subtask_idx": subtask_idx,
                    "iter": it,
                    "tool_call": tool_call,
                    "pixel_action": pixel_action,
                    "task_memory": task_memory,
                }
            )
            _persist_task_memory(task_memory)

            log(f"  action: {pixel_action.get('action')} | obs: {observation}")
            _sleep(cfg.click_delay_s)
            if cfg.dry_run:
                log("  DRYRUN: not executing")
            else:
                exec_action(pixel_action, cfg)
            _sleep(cfg.after_step_delay_s)
        else:
            raise ReplayError(f"Subtask {subtask_idx} exceeded max iterations ({max_iters_per_subtask})")

    log("Task Complete")
