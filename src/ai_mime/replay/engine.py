from __future__ import annotations

import base64
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from lmnr import observe

from openai import OpenAI  # type: ignore[import-not-found]


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

    for p in task_params:
        name = p.get("name")
        if not name:
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

    # Render plan.subtasks[].text + step action_values + additional_args.
    plan = out.get("plan") or {}
    subtasks = plan.get("subtasks") or []
    for st in subtasks:
        st["text"] = _render_template(st.get("text"), params)
        steps = st.get("steps") or []
        for s in steps:
            s["action_value"] = _render_template(s.get("action_value"), params)
            aa = s.get("additional_args") or {}
            # Backward-compat: if an older v2 schema has extract_query at the top-level, fold it in.
            if s.get("extract_query") and "extract_query" not in aa:
                aa["extract_query"] = s.get("extract_query")
            if aa.get("extract_query") is not None:
                aa["extract_query"] = _render_template(aa.get("extract_query"), params)
            s["additional_args"] = aa
            s.pop("extract_query", None)
    out["plan"] = {**plan, "subtasks": subtasks}

    return out


def _encode_image_data_url(image_path: Path) -> str:
    suffix = image_path.suffix.lower()
    if suffix == ".png":
        mime = "image/png"
    elif suffix in (".jpg", ".jpeg"):
        mime = "image/jpeg"
    elif suffix == ".webp":
        mime = "image/webp"
    else:
        mime = "image/png"
    b64 = base64.b64encode(image_path.read_bytes()).decode("utf-8")
    return f"data:{mime};base64,{b64}"


def _run_vision_extract(*, image_path: Path, query: str, cfg: ReplayConfig) -> str:
    """
    Host-driven extraction: ask the model to extract information from the screenshot given a refined query.
    Returns best-effort extracted text (may be empty if not found).
    """
    if not cfg.api_key:
        raise ReplayError("Missing replay API key for extraction (cfg.api_key).")
    if not query.strip():
        return ""
    data_url = _encode_image_data_url(image_path)
    client = OpenAI(api_key=cfg.api_key, base_url=cfg.base_url)
    messages: Any = [
        {"role": "system", "content": "You extract requested information from screenshots. Be concise and do not hallucinate."},
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": data_url}},
                {"type": "text", "text": f"Extraction query: {query}\n\nReturn ONLY the extracted value as plain text. If not present, return an empty string."},
            ],
        },
    ]
    try:
        completion = client.chat.completions.create(model=cfg.model, messages=messages)
        content = (completion.choices[0].message.content or "").strip()
        return content
    except Exception as e:
        raise ReplayError(f"Extraction call failed: {e}") from e


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

    extracts = {}

    plan = schema_rendered.get("plan") or {}
    subtasks_any = plan.get("subtasks") or []
    subtasks: list[dict[str, Any]] = subtasks_any

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

    def _persist_extracts() -> None:
        (run_dir / "extracts.json").write_text(json.dumps({"extracts": extracts}, indent=2), encoding="utf-8")

    # Cross-subtask memory carried forward (updated by model each iteration).
    task_memory = ""
    _persist_task_memory(task_memory)

    max_iters_per_subtask = 30

    # Map extract variable_name -> {query, subtask_i}.
    extract_meta: dict[str, dict[str, Any]] = {
        s["variable_name"]: {"query": s["additional_args"]["extract_query"], "subtask_i": st.get("subtask_i")}
        for st in subtasks
        for s in (st.get("steps") or [])
        if s.get("action_type") == "EXTRACT"
    }

    # Main loop: iterate subtasks, run until the model calls done(), then move to next.
    for subtask_idx, st in enumerate(subtasks):
        subtask_text = st.get("text") or ""
        deps = st.get("dependencies") or []

        steps_any = st.get("steps") or []
        if not isinstance(steps_any, list):
            # Be robust to malformed schemas; treat non-list as empty list.
            steps_any = []
            st["steps"] = []
        reference_steps: list[dict[str, Any]] = [
            {
                "i": i,
                "intent": s.get("intent"),
                "action_type": s.get("action_type"),
                "action_value": s.get("action_value"),
                "variable_name": s.get("variable_name"),
                "extract_query": (s.get("additional_args") or {}).get("extract_query"),
                "target_primary": (s.get("target") or {}).get("primary"),
                "expected_current_state": s.get("expected_current_state"),
                "post_action": s.get("post_action"),
            }
            for i, s in enumerate(steps_any)
        ]

        # Build Additional context from dependency extracts (query + value).
        additional_context: list[dict[str, Any]] = [
            {
                "name": dep,
                "query": (extract_meta.get(dep) or {}).get("query") or "",
                "value": extracts.get(dep),
            }
            for dep in deps
        ]

        log(f"Subtask {subtask_idx + 1}/{len(subtasks)}: {subtask_text}")
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
                f"Additional context: {additional_context}\n"
                f"Params: {params}\n"
                f"Task memory: {task_memory}\n"
                f"History (this subtask): {history[-5:]}\n\n"
                f"Reference steps (examples) from previous runs: {reference_steps}\n\n"
                "Extraction: If the reference steps include an EXTRACT step and you are at the corresponding screen/state, you MUST call extract with that step's variable_name and extract_query. Do not use computer_use for extraction.\n"
                "Decide ONE next action to progress the current subtask, or call done if the expected outcome is met.\n"
                "If you call computer_use, include a current-step specific observation and an updated task_memory.\n"
                "If you call done, include result (what was achieved / info to pass) and updated task_memory.\n"
                "If you seem stuck (observations repeating / screen not changing), try an alternate strategy (e.g., back, close popups, refocus, scroll, open the right app/tab, or retry the entry path).\n"
                "Return exactly one tool call."
            )

            tool_call = predict_tool_call(img_path, user_query, cfg)
            name = tool_call.get("name")
            args = tool_call.get("arguments") or {}

            if name == "extract":
                vn = args.get("variable_name")
                q = args.get("query") or ""
                val = _run_vision_extract(image_path=img_path, query=q, cfg=cfg)
                extracts[vn] = val
                _persist_extracts()
                task_memory = str(args.get("task_memory") or task_memory)
                _persist_task_memory(task_memory)
                _append_event(
                    {
                        "type": "extract",
                        "subtask_idx": subtask_idx,
                        "iter": it,
                        "variable_name": vn,
                        "query": q,
                        "value": val,
                        "task_memory": task_memory,
                    }
                )
                for item in additional_context:
                    if item.get("name") == vn:
                        item["value"] = val
                log(f"  extract: {vn}={val}")
                continue

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
            try:
                exec_action(pixel_action, cfg)
            except ReplayError as e:
                # Do not abort the whole replay on a single execution failure.
                # Record the failure and continue to the next iteration (model can recover).
                msg = str(e)
                _append_event(
                    {
                        "type": "exec_error",
                        "subtask_idx": subtask_idx,
                        "iter": it,
                        "error": msg,
                        "pixel_action": pixel_action,
                        "task_memory": task_memory,
                    }
                )
                log(f"  exec_error: {msg}")
                continue
            _sleep(cfg.after_step_delay_s)
        else:
            raise ReplayError(f"Subtask {subtask_idx} exceeded max iterations ({max_iters_per_subtask})")

    log("Task Complete")
