from __future__ import annotations

import base64
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal

from openai import OpenAI  # type: ignore[import-not-found]
from pydantic import BaseModel, Field  # type: ignore[import-not-found]
from tqdm import tqdm  # type: ignore[import-not-found]

logger = logging.getLogger(__name__)

MAX_RETRIES = 2  # max retries after the first attempt


PassAActionType = Literal["CLICK", "TYPE", "SCROLL", "KEYPRESS", "DRAG"]


def _png_data_url(path: Path) -> str:
    raw = path.read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:image/png;base64,{b64}"

def _write_json_atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    tmp.replace(path)

def _read_json_if_exists(path: Path) -> Any | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


PASS_A_SYSTEM_PROMPT = """You convert a UI trace step (screenshots + action) into a reusable, coordinate-free step instruction
for a computer-use vision agent. You must be accurate and avoid inventing UI text.
"""

PASS_A_USER_TEMPLATE = """Task name: {task_name}
User task description: {task_description_user}

Action (ground truth): {action_json}
Param hint (optional): {param_hint_json}

You will be given PRE and POST screenshots for this step. The FIRST image is PRE, the SECOND image is POST.

Rules:
- Do NOT include coordinates.
- target.primary and fallback must reference visible text/icons and relative location (e.g., near top, left sidebar, inside popup).
- intent: 1 sentence.
- post_change: 1-2 short lines describing what changed from PRE to POST.
- If action_type is TYPE, convert the typed value into a parameter template if it seems variable, e.g. "{{email}}".
- If the typed value seems fixed (e.g. "OK", "yes"), keep it literal.
- action_value: only set for TYPE and KEYPRESS. For TYPE, set to the same value as input_template (template or literal). For KEYPRESS, set to the key from action_details.key if present (e.g. "ENTER", "CMD+SPACE").
- If uncertain about labels, say "unlabeled button" or "icon button" rather than guessing.
- error_signals: include obvious error/captcha texts if visible in POST, otherwise []."""


PASS_B_SYSTEM_PROMPT = """You are a workflow compiler. You take step cards (coordinate-free UI steps) and produce
a compact reusable workflow schema consisting of detailed task description, parameters, and success criteria for a computer-use vision agent.
"""

PASS_B_USER_TEMPLATE = """Task name: {task_name}
User task description: {task_description_user}

Step cards (ordered JSON array):
{step_cards_json}

Rules:
- detailed_task_description: 3-6 sentences describing the overall workflow.
- task_params: deduplicate templates like "{{email}}" across steps. Return a JSON array of param objects:
  {{ "name": "<param_name>", "type": "<string|number|date>", "description": "<what this param is>", "example": "...", "sensitive": true|false, "optional": true|false }}
  If optional is false, the workflow should be runnable using the example value when the caller doesn't supply a value.
- success_criteria: 1-3 simple checks, primarily "text_present" with key texts likely visible on the final screen.
"""


class StepTarget(BaseModel):
    """Coordinate-free selector description for the target UI element."""

    primary: str = Field(description="Primary target description using visible text/icons + relative location.")
    fallback: str = Field(description="Fallback target description if primary isn't found, still coordinate-free.")


class StepCardModel(BaseModel):
    """Reusable per-step instruction derived from PRE/POST screenshots + action."""

    i: int = Field(description="0-based step index within the workflow.")
    intent: str = Field(description="1 sentence describing the user intent for this step.")
    action_type: PassAActionType = Field(description="Action type enum for the vision agent.")
    target: StepTarget = Field(description="How to locate the target element, coordinate-free.")
    input_template: str | None = Field(description="Template string for TYPE steps (e.g. '{{email}}'), else null.")
    action_value: str | None = Field(
        description="Optional action value. For TYPE and KEYPRESS, include the value to type/press; otherwise null."
    )
    post_change: list[str] = Field(min_length=1, max_length=2, description="1-2 short lines describing the visible change.")
    screen_hint: str | None = Field(description="Optional hint of what should be visible after the step.")
    error_signals: list[str] = Field(description="List of visible error/captcha signals if present, else empty.")

    # Keep action_value behavior inside the schema so Structured Outputs can enforce it.
    # (Avoids additional manual validation code paths.)
    def model_post_init(self, __context: Any) -> None:  # type: ignore[override]
        if self.action_type in {"TYPE", "KEYPRESS"}:
            if not isinstance(self.action_value, str) or not self.action_value.strip():
                raise ValueError("action_value must be set for TYPE/KEYPRESS")
        else:
            if self.action_value is not None:
                raise ValueError("action_value must be null for non-TYPE/non-KEYPRESS")


class PassBParamSpec(BaseModel):
    """Workflow parameter specification."""
    name: str = Field(description="Parameter name (deduplicated), e.g. 'email'.")
    type: str = Field(description="Parameter primitive type as a string (e.g. 'string', 'number', 'date').")
    description: str = Field(description="Short human-readable description of what this parameter represents.")
    example: str = Field(description="Example value for the parameter.")
    sensitive: bool = Field(description="Whether this parameter is sensitive (e.g., password, token).")
    optional: bool = Field(default=False, description="Whether the parameter is optional for running the workflow.")


# class PassBPlan(BaseModel):
#     """Task plan consisting of StepCards (kept compact)."""
#     steps: list[dict[str, Any]] = Field(description="Ordered array of step objects (StepCards).")


class PassBOutput(BaseModel):
    """Task-level compiled schema produced from StepCards."""
    detailed_task_description: str = Field(description="3-6 sentence description of the overall workflow.")
    task_params: list[PassBParamSpec] = Field(description="Deduplicated parameters referenced by step templates.")
    success_criteria: list[str] = Field(description="1-3 simple checks to verify success.")


def _output_text_preview(resp: Any, limit: int = 800) -> str:
    t = getattr(resp, "output_text", None)
    if t is None:
        return "<no output_text available>"
    s = str(t)
    return s if len(s) <= limit else (s[:limit] + "…")


def _call_parse_with_retries(
    *,
    where: str,
    client: OpenAI,
    model: str,
    input_payload: Any,
    text_format: Any,
    max_output_tokens: int | None,
    repair_user_message: str,
) -> Any:
    """
    Execute a Structured Outputs parse call with retries.

    On failure, retries send a follow-up message including:
    - the failure reason
    - the previous output_text (if any)
    - a directive to return only schema-valid JSON

    Best-effort threads retries via previous_response_id (when supported by SDK).
    """
    prev_resp: Any | None = None
    last_err: Exception | None = None

    for attempt in range(MAX_RETRIES + 1):
        try:
            kwargs: dict[str, Any] = {
                "model": model,
                "input": input_payload,
                "text_format": text_format,
                "max_output_tokens": max_output_tokens,
            }
            if attempt > 0 and prev_resp is not None and getattr(prev_resp, "id", None):
                kwargs["previous_response_id"] = prev_resp.id

            resp = client.responses.parse(**kwargs)
            prev_resp = resp

            # Treat "parsed is None" as a retryable failure.
            event = getattr(resp, "output_parsed", None)
            if event is None:
                raise RuntimeError(
                    f"{where}: output_parsed is None. output_text={_output_text_preview(resp)}"
                )
            return resp
        except TypeError:
            # Some SDK versions may not accept previous_response_id; retry without it.
            try:
                resp = client.responses.parse(
                    model=model,
                    input=input_payload,
                    text_format=text_format,
                    max_output_tokens=max_output_tokens,
                )
                prev_resp = resp
                event = getattr(resp, "output_parsed", None)
                if event is None:
                    raise RuntimeError(
                        f"{where}: output_parsed is None. output_text={_output_text_preview(resp)}"
                    )
                return resp
            except Exception as e:
                last_err = e
        except Exception as e:
            last_err = e

        # Prepare retry prompt by appending a repair message.
        if attempt < MAX_RETRIES:
            prev_text = _output_text_preview(prev_resp) if prev_resp is not None else "<no previous output>"
            logger.warning(
                "%s attempt %d failed (%s). Retrying with repair prompt.",
                where,
                attempt + 1,
                str(last_err),
            )
            # Append the exception context into the same messages array.
            input_payload = [
                *list(input_payload),
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                f"{repair_user_message}\n\n"
                                f"Failure: {last_err}\n"
                                f"Previous output_text: {prev_text}\n"
                                "Fix your response and return ONLY schema-valid JSON."
                            ),
                        }
                    ],
                },
            ]

    raise RuntimeError(f"{where}: failed after {MAX_RETRIES + 1} attempts: {last_err}") from last_err


@dataclass(frozen=True)
class StepInput:
    i: int
    event_idx: int
    action_json: dict[str, Any]
    param_hint_json: dict[str, Any]
    pre_screenshot: Path | None
    post_screenshot: Path | None


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out


def load_task_metadata(workflow_dir: str | os.PathLike[str]) -> tuple[str, str]:
    """
    Returns (task_name, task_description_user) from workflow metadata.json.
    """
    workflow_dir_p = Path(workflow_dir)
    meta = _read_json(workflow_dir_p / "metadata.json")
    return str(meta.get("name", "")), str(meta.get("description", ""))


def load_events(workflow_dir: str | os.PathLike[str]) -> list[dict[str, Any]]:
    workflow_dir_p = Path(workflow_dir)
    return _read_jsonl(workflow_dir_p / "manifest.jsonl")


def _map_action_type(action_type: str | None) -> PassAActionType | None:
    if action_type is None:
        return None
    t = str(action_type).lower()
    if t == "click":
        return "CLICK"
    if t == "type":
        return "TYPE"
    if t == "scroll":
        return "SCROLL"
    if t == "key":
        return "KEYPRESS"
    if t == "drag":
        return "DRAG"
    if t == "end":
        return None
    return None


def derive_step_inputs(workflow_dir: str | os.PathLike[str], events: list[dict[str, Any]]) -> list[StepInput]:
    """
    Derives per-step inputs needed for Pass A.

    PRE screenshot is the event's screenshot, POST screenshot is the next event's screenshot (if any).
    """
    workflow_dir_p = Path(workflow_dir)
    steps: list[StepInput] = []

    step_i = 0
    for event_idx, e in enumerate(events):
        mapped = _map_action_type(e.get("action_type"))
        if mapped is None:
            continue

        pre_rel = e.get("screenshot")
        post_rel = None
        if event_idx + 1 < len(events):
            post_rel = events[event_idx + 1].get("screenshot")

        pre_path = workflow_dir_p / pre_rel if pre_rel else None
        post_path = workflow_dir_p / post_rel if post_rel else None

        # Param hints: MVP only includes typed text.
        param_hint: dict[str, Any] = {}
        if mapped == "TYPE":
            details = e.get("action_details") or {}
            if isinstance(details, dict) and "text" in details:
                param_hint["typed_text"] = details.get("text")

        steps.append(
            StepInput(
                i=step_i,
                event_idx=event_idx,
                action_json={
                    "action_type": mapped,
                    "action_details": e.get("action_details") or {},
                },
                param_hint_json=param_hint,
                pre_screenshot=pre_path,
                post_screenshot=post_path,
            )
        )
        step_i += 1

    return steps



def run_pass_a_step_cards(
    *,
    workflow_dir: str | os.PathLike[str],
    model: str = "gpt-5-mini",
    max_output_tokens: int | None = 2000,
) -> list[dict[str, Any]]:
    """
    Pass A: compile each actionable manifest step into a StepCard.
    Does one LLM call per step.
    """
    workflow_dir_p = Path(workflow_dir)
    task_name, task_description_user = load_task_metadata(workflow_dir_p)
    events = load_events(workflow_dir_p)
    steps = derive_step_inputs(workflow_dir_p, events)

    client = OpenAI()

    # Load any existing StepCards so reruns only attempt missing steps.
    step_cards_path = workflow_dir_p / "step_cards.json"
    def _load_existing_by_i() -> dict[int, dict[str, Any]]:
        existing_any = _read_json_if_exists(step_cards_path)
        existing_by_i: dict[int, dict[str, Any]] = {}
        if isinstance(existing_any, list):
            for item in existing_any:
                if not isinstance(item, dict):
                    continue
                ii = item.get("i")
                if isinstance(ii, int):
                    existing_by_i[ii] = item
                elif isinstance(ii, str) and ii.isdigit():
                    existing_by_i[int(ii)] = item
        return existing_by_i

    existing_by_i = _load_existing_by_i()

    logger.info(
        "Pass A: total_steps=%d existing=%d (model=%s) with up to 5 in-flight requests",
        len(steps),
        len(existing_by_i),
        model,
    )

    def _compile_one(s: StepInput) -> dict[str, Any]:
        logger.info(
            "Pass A: step_i=%d event_idx=%d action_type=%s",
            s.i,
            s.event_idx,
            s.action_json.get("action_type"),
        )

        # Ensure screenshots exist if paths are set.
        img_paths: list[Path] = []
        if s.pre_screenshot is not None:
            if not s.pre_screenshot.exists():
                raise FileNotFoundError(f"PRE screenshot missing for step {s.i}: {s.pre_screenshot}")
            img_paths.append(s.pre_screenshot)
        if s.post_screenshot is not None:
            if not s.post_screenshot.exists():
                raise FileNotFoundError(f"POST screenshot missing for step {s.i}: {s.post_screenshot}")
            img_paths.append(s.post_screenshot)

        user = PASS_A_USER_TEMPLATE.format(
            task_name=task_name,
            task_description_user=task_description_user,
            action_json=json.dumps({"i": s.i, **s.action_json}, ensure_ascii=False),
            param_hint_json=json.dumps(s.param_hint_json or {}, ensure_ascii=False),
        )

        input_payload: Any = [
            {"role": "system", "content": PASS_A_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [{"type": "input_text", "text": user}]
                + [{"type": "input_image", "image_url": _png_data_url(p)} for p in img_paths],
            },
        ]

        resp = _call_parse_with_retries(
            where=f"Pass A step {s.i}",
            client=client,
            model=model,
            input_payload=input_payload,
            text_format=StepCardModel,
            max_output_tokens=max_output_tokens,
            repair_user_message="Your previous StepCard output did not parse/validate. Re-output ONLY the StepCard JSON.",
        )
        try:
            event: StepCardModel | None = resp.output_parsed  # type: ignore[attr-defined]
            if event is None:
                raise RuntimeError(f"output_parsed is None. output_text={_output_text_preview(resp)}")
            card = event.model_dump()
        except Exception as e:
            raise RuntimeError(f"Pass A step {s.i}: failed to read output_parsed/model_dump: {e}") from e
        card["i"] = s.i
        return card

    # Determine which step indices are missing.
    missing = [s for s in steps if s.i not in existing_by_i]
    if not missing:
        logger.info("Pass A: all steps already present; skipping.")
        return [existing_by_i[i] for i in range(len(steps))]

    logger.info("Pass A: compiling missing_steps=%d", len(missing))

    # Start from existing; add new results as they complete.
    results: dict[int, dict[str, Any]] = dict(existing_by_i)

    def _persist_partial() -> None:
        # Re-read any on-disk results and merge before writing to avoid accidental shrink/overwrite
        # (e.g., if multiple runs overlap or a previous run wrote more than this process has in memory).
        results.update(_load_existing_by_i())

        # Persist only the known cards (sorted by i). This format is stable and resume-friendly.
        ordered = [results[i] for i in sorted(results.keys())]
        _write_json_atomic(step_cards_path, ordered)

    # Always persist whatever we already have before starting new calls.
    _persist_partial()

    try:
        with ThreadPoolExecutor(max_workers=5) as ex:
            futures = {ex.submit(_compile_one, s): s for s in missing}
            with tqdm(total=len(futures), desc="Pass A", unit="step") as pbar:
                for fut in as_completed(futures):
                    s = futures[fut]
                    try:
                        results[s.i] = fut.result()
                        _persist_partial()
                    except Exception as e:
                        # Persist completed work then exit so rerun only retries failures.
                        _persist_partial()
                        raise RuntimeError(f"Pass A failed on step {s.i}: {e}") from e
                    finally:
                        pbar.update(1)
    except Exception:
        # Ensure partials are saved even on unexpected executor errors.
        _persist_partial()
        raise

    # Ensure completeness before returning.
    missing_after = [s.i for s in steps if s.i not in results]
    if missing_after:
        _persist_partial()
        raise RuntimeError(f"Pass A incomplete after run; missing step indices: {missing_after}")

    ordered_final = [results[i] for i in range(len(steps))]
    _write_json_atomic(step_cards_path, ordered_final)
    return ordered_final


def write_step_cards(workflow_dir: str | os.PathLike[str], step_cards: list[dict[str, Any]]) -> Path:
    workflow_dir_p = Path(workflow_dir)
    path = workflow_dir_p / "step_cards.json"
    _write_json_atomic(path, step_cards)
    return path



def run_pass_b_task_compiler(
    *,
    workflow_dir: str | os.PathLike[str],
    step_cards: list[dict[str, Any]],
    model: str = "gpt-5-mini",
    max_output_tokens: int | None = 1200,
) -> dict[str, Any]:
    """
    Pass B: compile task-level schema from step cards.
    """
    workflow_dir_p = Path(workflow_dir)
    task_name, task_description_user = load_task_metadata(workflow_dir_p)
    client = OpenAI()

    logger.info("Pass B: compiling task schema (model=%s)", model)
    user = PASS_B_USER_TEMPLATE.format(
        task_name=task_name,
        task_description_user=task_description_user,
        step_cards_json=json.dumps(step_cards, ensure_ascii=False),
    )
    input_payload: Any = [
        {"role": "system", "content": PASS_B_SYSTEM_PROMPT},
        {"role": "user", "content": [{"type": "input_text", "text": user}]},
    ]
    resp = _call_parse_with_retries(
        where="Pass B",
        client=client,
        model=model,
        input_payload=input_payload,
        text_format=PassBOutput,
        max_output_tokens=max_output_tokens,
        repair_user_message="Your previous task compiler output did not parse/validate.",
    )
    try:
        event: PassBOutput | None = resp.output_parsed  # type: ignore[attr-defined]
        if event is None:
            raise RuntimeError(f"output_parsed is None. output_text={_output_text_preview(resp)}")
        return event.model_dump()
    except Exception as e:
        raise RuntimeError(f"Pass B: failed to read output_parsed/model_dump: {e}") from e


def write_schema_draft(workflow_dir: str | os.PathLike[str], schema_draft: dict[str, Any]) -> Path:
    workflow_dir_p = Path(workflow_dir)
    path = workflow_dir_p / "schema.draft.json"
    _write_json_atomic(path, schema_draft)
    return path

def write_schema(workflow_dir: str | os.PathLike[str], schema: dict[str, Any]) -> Path:
    workflow_dir_p = Path(workflow_dir)
    path = workflow_dir_p / "schema.json"
    _write_json_atomic(path, schema)
    return path


_TEMPLATE_RE = re.compile(r"\{\{([a-zA-Z0-9_]+)\}\}")


def extract_param_templates(step_cards: Iterable[dict[str, Any]]) -> set[str]:
    """
    Utility: extract parameter template names like {{email}} from StepCards.
    Intended for tests / sanity checks (Pass B is still the source of truth).
    """
    found: set[str] = set()
    for c in step_cards:
        s = c.get("input_template")
        if isinstance(s, str):
            for m in _TEMPLATE_RE.finditer(s):
                found.add(m.group(1))
    return found


def compile_workflow_schema(
    *,
    workflow_dir: str | os.PathLike[str],
    model: str = "gpt-5-mini",
) -> dict[str, Any]:
    """
    End-to-end compile:
      - Pass A -> step_cards.json
      - Pass B -> schema.draft.json
      - Write schema.json from Pass B
    Returns the final schema object written to schema.json.
    """
    workflow_dir_p = Path(workflow_dir)
    task_name, task_description_user = load_task_metadata(workflow_dir_p)

    logger.info("Schema compile start: workflow_dir=%s task_name=%s model=%s", workflow_dir_p, task_name, model)

    # If final output exists, reuse it and avoid re-running any passes.
    schema_path = workflow_dir_p / "schema.json"
    existing_schema = _read_json_if_exists(schema_path)
    if isinstance(existing_schema, dict) and isinstance(existing_schema.get("plan"), dict) and isinstance(
        existing_schema.get("plan", {}).get("steps"), list
    ):
        logger.info("Schema compile: found existing schema.json; skipping all passes.")
        return existing_schema

    # Pass A: must be complete before Pass B.
    # Note: run_pass_a_step_cards() handles partial persistence + resume via step_cards.json.
    step_cards = run_pass_a_step_cards(workflow_dir=workflow_dir_p, model=model)
    write_step_cards(workflow_dir_p, step_cards)
    logger.info("Pass A complete: step_cards.json (%d steps)", len(step_cards))

    # Pass B: must be complete before writing schema.json.
    draft_path = workflow_dir_p / "schema.draft.json"
    existing_draft = _read_json_if_exists(draft_path)
    if isinstance(existing_draft, dict) and "task_params" in existing_draft and "detailed_task_description" in existing_draft:
        logger.info("Pass B: found existing schema.draft.json; skipping.")
        final_schema: dict[str, Any] = dict(existing_draft)
    else:
        task_compiler_out = run_pass_b_task_compiler(
            workflow_dir=workflow_dir_p,
            step_cards=step_cards,
            model=model,
        )

        final_schema = {
            "task_name": task_name,
            "task_description_user": task_description_user,
            **task_compiler_out,
        }

        # Do not write draft until we attach the plan below (so reruns can skip Pass B safely).

    # Always attach plan.steps from Pass A in the final schema (and draft).
    final_schema["plan"] = {"steps": step_cards}

    write_schema_draft(workflow_dir_p, final_schema)
    logger.info("Pass B complete: wrote schema.draft.json")

    write_schema(workflow_dir_p, final_schema)
    logger.info("Wrote schema.json")
    return final_schema
