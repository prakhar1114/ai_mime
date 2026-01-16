from __future__ import annotations

import base64
import json
import logging
import os
import re
from lmnr import observe
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal, Optional

from pydantic import BaseModel, Field  # type: ignore[import-not-found]
from tqdm import tqdm  # type: ignore[import-not-found]

from ai_mime.user_config import ResolvedReflectConfig
from ai_mime.litellm_client import LiteLLMChatClient

logger = logging.getLogger(__name__)

MAX_RETRIES = 2  # max retries after the first attempt


PassAActionType = Literal[
    "CLICK",
    "DOUBLE_CLICK",
    "RIGHT_CLICK",
    "MIDDLE_CLICK",
    "TYPE",
    "SCROLL",
    "KEYPRESS",
    "DRAG",
    "EXTRACT",
]


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
Details (optional): {details_text}

You will be given PRE and POST screenshots for this step. The FIRST image is PRE, the SECOND image is POST.

Rules:
- Do NOT include coordinates.
- target.primary and fallback must reference visible text/icons and relative location (e.g., near top, left sidebar, inside popup).
- expected_current_state: describe the current screen/state where the action should be performed in GENERAL terms.
  It does not need to exactly match on-screen text. Focus on what has happened so far and what is true now (app/window, view/panel, focus) and general UI elements instead of the results.
- intent: 1 sentence.
- post_action: 1-2 short lines describing what changed from PRE to POST as a result of the action.
  It must be GENERIC and describe the UI outcome, not the specific parameter/value used (do not repeat typed text, emails, names, etc.).
- Do NOT parametrize values (do not use templates like "{{email}}"). Keep typed values literal.
- action_value:
  - TYPE: set to the literal typed value from action_details.text
  - KEYPRESS: set to the key from action_details.key if present (e.g. "ENTER", "CMD+SPACE")
  - EXTRACT: set to a refined extraction query describing what to capture from the current screenshot, slightly generalized and suitable for later parameterization
- For EXTRACT:
  - intent must include a short example of what was extracted using the recorded action_details.values, formatted as: "Example extracted content: <...>"
  - post_action should be a minimal-valid placeholder like "not relevant" because EXTRACT does not change the UI.
- If uncertain about labels, say "unlabeled button" or "icon button" rather than guessing.
- target.fallback is optional; use null if not needed."""


PASS_B_SYSTEM_PROMPT = """You are a workflow compiler. You take step cards (coordinate-free UI steps) and produce
a compact reusable workflow schema consisting of detailed task description, parameters, and success criteria for a computer-use vision agent.
"""

PASS_B_USER_TEMPLATE = """Task name: {task_name}
User task description: {task_description_user}

Step summaries (ordered JSON array):
{step_summaries_json}

### INSTRUCTIONS

**1. General Workflow Definition**
- **detailed_task_description**: Write 3-6 sentences describing the overall workflow high-level intent.
- **success_criteria**: Write a SINGLE generalized string describing how to verify the task succeeded (e.g., "Verify the requested song is playing").

**2. Grouping & Subtasks**
- Group the provided steps into logical **subtasks**.
- **Standard Granularity**: For standard navigation/typing, group approx. 4-6 steps per subtask.
- **CRITICAL EXCEPTION - EXTRACT Steps**:
  - `EXTRACT` steps are high-priority milestones. **Do not** bury an `EXTRACT` step inside a long navigation sequence.
  - Create a dedicated subtask for the extraction logic (e.g., "Locate the verification code on the screen and extract it").
  - If multiple extracts happen in sequence (e.g., copying Name, then Email), they can be grouped into one "Data Collection" subtask, provided the description covers all of them.
- **Format**: Each subtask string must:
  - Be descriptive and actionable.
  - Include placeholders for parameters (`{{song_name}}`) or upstream extracts (`{{extract_0}}`).
  - **Expected Outcome**: Must be explicit. For extraction subtasks, strictly state: "Expected outcome: The value for {{extract_X}} is captured."

**3. Parameterization Logic (Task Params)**
- Identify values in step `action_value` that should be dynamic (user inputs).
- **The "Semantic Meaning" Rule**: Only parameterize values that represent **User Intent**—things the user would care to change between runs (e.g., "Song Name", "Price", "City").
- **What to EXCLUDE (Keep Action Value NULL)**:
  - **Structural Data**: Do not parameterize values that are just housekeeping or formatting logic.
    - *Example:* If the workflow adds a row to a sheet and types an index number ("1", "2"...), this is NOT a parameter.
    - *Example:* If the workflow types a fixed header like "Date" or "Total".
  - **Infrastructure**: App names, URLs, "Chrome", "Spotify".
  - **Dependencies**: Values that come from upstream `EXTRACT` steps.
- **Output**: Define legitimate user parameters in `task_params` with clear types, descriptions, and examples.

**4. Handling Data Dependencies (Extracts)**
- **Identification**: Use the provided `variable_name` from the step summary (e.g., "extract_0", "extract_1").
- **Downstream Usage Rules**:
  - If a downstream `TYPE` or `KEYPRESS` step uses data extracted earlier:
    1.  **Action Value**: Set the step's `action_value` to `null` in `plan_step_updates`.
    2.  **Subtask Text**: Ensure the `subtask` string explicitly references the source variable (e.g., "Type the code from {{extract_0}} into the box").

**5. Output Mapping (plan_step_updates)**
- You must generate an update object for **every** step index provided in the input.
- **action_value**:
  - For `EXTRACT` steps: Return the variable name (e.g., "extract_0").
  - For `TYPE`/`KEYPRESS` steps:
    - Return `{{param_name}}` if it is a user input.
    - Return `null` if the value is structural (e.g., an index number) or sourced from an extract.
  - For others: `null`.
- **subtask**: Must be an EXACT string match to one of the strings defined in your `subtasks` list.
  - *Note:* If you set `action_value` to `null` for a structural step (like typing an index), your subtask text MUST describe what to type (e.g., "Enter the next sequential index number").
"""


class StepTarget(BaseModel):
    """Coordinate-free selector description for the target UI element."""

    primary: str = Field(description="Primary target description using visible text/icons + relative location.")
    fallback: Optional[str] = Field(description="Fallback target description if primary isn't found, still coordinate-free.")


class StepCardModel(BaseModel):
    """Reusable per-step instruction derived from PRE/POST screenshots + action."""

    i: int = Field(description="0-based step index within the workflow.")
    expected_current_state: str = Field(
        description=(
            "General description of the current screen/state where this action should be performed. It does not need to "
            "quote exact UI text; focus on app/window, view/panel, and focus/selection."
        )
    )
    intent: str = Field(description="1 sentence describing the user intent for this step.")
    action_type: PassAActionType = Field(description="Action type enum for the vision agent.")
    action_value: str | None = Field(
        description="Optional action value. For TYPE and KEYPRESS, include the value to type/press. For EXTRACT, include the refined extract query. Otherwise null."
    )
    target: StepTarget = Field(description="How to locate the target element, coordinate-free.")
    post_action: list[str] = Field(
        min_length=1,
        max_length=2,
        description=(
            "1-2 short lines describing the visible change after the action. Keep it generic; do not repeat specific "
            "typed values/parameters."
        ),
    )

    # Keep action_value behavior inside the schema so Structured Outputs can enforce it.
    # (Avoids additional manual validation code paths.)
    def model_post_init(self, __context: Any) -> None:  # type: ignore[override]
        if self.action_type in {"TYPE", "KEYPRESS", "EXTRACT"}:
            if not isinstance(self.action_value, str) or not self.action_value.strip():
                raise ValueError("action_value must be set for TYPE/KEYPRESS/EXTRACT")
        else:
            if self.action_value is not None:
                raise ValueError("action_value must be null for non-TYPE/non-KEYPRESS/non-EXTRACT")


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


class PassBStepUpdate(BaseModel):
    """Per-step plan augmentation produced by Pass B."""

    i: int = Field(description="0-based step index within the workflow plan.")
    action_value: str | None = Field(
        description="Possibly parameterized action value for this step. Null for non-TYPE/non-KEYPRESS steps."
    )
    subtask: str = Field(description="The exact subtask string from PassBOutput.subtasks that this step belongs to.")


class PassBOutput(BaseModel):
    """Task-level compiled schema produced from Pass A steps (summarized) + Pass B augmentation."""

    detailed_task_description: str = Field(description="3-6 sentence description of the overall workflow.")
    subtasks: list[str] = Field(
        min_length=1,
        description=(
            "High-level subtasks as detailed strings. Each must include parameter placeholders like '{param}' where "
            "relevant and include an explicit expected outcome."
        ),
    )
    task_params: list[PassBParamSpec] = Field(description="Deduplicated parameters referenced by templates in steps/subtasks.")
    success_criteria: str = Field(description="A single generalized string describing how to verify success.")
    plan_step_updates: list[PassBStepUpdate] = Field(
        description="Per-step updates including parameterized action_value and subtask assignment for every step index."
    )


@dataclass(frozen=True)
class StepInput:
    i: int
    event_idx: int
    action_json: dict[str, Any]
    param_hint_json: dict[str, Any]
    details: str | None
    extract_var_name: str | None
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
    if t == "double_click":
        return "DOUBLE_CLICK"
    if t == "right_click":
        return "RIGHT_CLICK"
    if t == "middle_click":
        return "MIDDLE_CLICK"
    if t == "type":
        return "TYPE"
    if t == "scroll":
        return "SCROLL"
    if t == "key":
        return "KEYPRESS"
    if t == "drag":
        return "DRAG"
    if t == "extract":
        return "EXTRACT"
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
    extract_i = 0
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

        # For EXTRACT, we want a stable snapshot of the current screen. Treat PRE and POST as the same image
        # so the model does not invent UI changes.
        extract_var_name: str | None = None
        if mapped == "EXTRACT":
            post_path = pre_path
            extract_var_name = f"extract_{extract_i}"
            extract_i += 1

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
                details=(str(e.get("details")) if e.get("details") is not None else None),
                extract_var_name=extract_var_name,
                pre_screenshot=pre_path,
                post_screenshot=post_path,
            )
        )
        step_i += 1

    return steps



def run_pass_a_step_cards(
    *,
    workflow_dir: str | os.PathLike[str],
    llm_cfg: ResolvedReflectConfig,
) -> list[dict[str, Any]]:
    """
    Pass A: compile each actionable manifest step into a StepCard.
    Does one LLM call per step.
    """
    workflow_dir_p = Path(workflow_dir)
    task_name, task_description_user = load_task_metadata(workflow_dir_p)
    events = load_events(workflow_dir_p)
    steps = derive_step_inputs(workflow_dir_p, events)

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

    pass_a_model = llm_cfg.pass_a_model or llm_cfg.model
    logger.info(
        "Pass A: total_steps=%d existing=%d (model=%s) with up to 5 in-flight requests",
        len(steps),
        len(existing_by_i),
        pass_a_model,
    )

    def _compile_one(s: StepInput) -> dict[str, Any]:

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
            details_text=json.dumps(s.details, ensure_ascii=False),
        )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": PASS_A_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [{"type": "text", "text": user}]
                + [{"type": "image_url", "image_url": {"url": _png_data_url(p)}} for p in img_paths],
            },
        ]

        client = LiteLLMChatClient(
            model=pass_a_model,
            api_base=llm_cfg.api_base,
            api_key=llm_cfg.api_key,
            extra_kwargs=llm_cfg.extra_kwargs,
            max_retries=MAX_RETRIES,
        )
        event = client.create(
            response_model=StepCardModel,
            messages=messages,
            max_tokens=llm_cfg.pass_a_max_tokens,
            max_retries=MAX_RETRIES,
        )
        card = event.model_dump()
        card["i"] = s.i
        # Ensure we always carry forward the input details (null or string) even if the model omits it.
        card["details"] = s.details
        # Set variable_name programmatically (do not ask the model to produce extract_0/extract_1/...).
        vn = s.extract_var_name
        if vn is not None:
            if not re.fullmatch(r"extract_[0-9]+", vn):
                raise RuntimeError(f"Pass A step {s.i}: invalid extract_var_name={vn!r}")
        card["variable_name"] = vn
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
        with ThreadPoolExecutor(max_workers=10) as ex:
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
    llm_cfg: ResolvedReflectConfig,
) -> dict[str, Any]:
    """
    Pass B: compile task-level schema from step cards.
    """
    workflow_dir_p = Path(workflow_dir)
    task_name, task_description_user = load_task_metadata(workflow_dir_p)

    pass_b_model = llm_cfg.pass_b_model or llm_cfg.model
    logger.info("Pass B: compiling task schema (model=%s)", pass_b_model)
    # Pass B operates on summarized steps (include details to improve intent + parametrization).
    step_summaries: list[dict[str, Any]] = []
    for s in step_cards:
        if not isinstance(s, dict):
            continue
        step_summaries.append(
            {
                "i": s.get("i"),
                "intent": s.get("intent"),
                "action_type": s.get("action_type"),
                "action_value": s.get("action_value"),
                "details": s.get("details"),
                "variable_name": s.get("variable_name"),
            }
        )
    user = PASS_B_USER_TEMPLATE.format(
        task_name=task_name,
        task_description_user=task_description_user,
        step_summaries_json=json.dumps(step_summaries, ensure_ascii=False),
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": PASS_B_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]
    client = LiteLLMChatClient(
        model=pass_b_model,
        api_base=llm_cfg.api_base,
        api_key=llm_cfg.api_key,
        extra_kwargs=llm_cfg.extra_kwargs,
        max_retries=MAX_RETRIES,
    )
    event = client.create(
        response_model=PassBOutput,
        messages=messages,
        max_tokens=llm_cfg.pass_b_max_tokens,
        max_retries=MAX_RETRIES,
    )
    return event.model_dump()


def write_plan_creation(workflow_dir: str | os.PathLike[str], plan_creation: dict[str, Any]) -> Path:
    """
    Checkpoint for Pass B raw output (task-level planning), kept separate from final schema.json.
    Expected to include: detailed_task_description, task_params, success_criteria, subtasks, plan_step_updates.
    """
    workflow_dir_p = Path(workflow_dir)
    path = workflow_dir_p / "plan_creation.json"
    _write_json_atomic(path, plan_creation)
    return path

def write_schema(workflow_dir: str | os.PathLike[str], schema: dict[str, Any]) -> Path:
    workflow_dir_p = Path(workflow_dir)
    path = workflow_dir_p / "schema.json"
    _write_json_atomic(path, schema)
    return path


_TEMPLATE_RE = re.compile(r"\{\{([a-zA-Z0-9_]+)\}\}")

_EXTRACT_PLACEHOLDER_RE = re.compile(r"\{(extract_[0-9]+)\}")
_EXTRACT_NAME_RE = re.compile(r"^extract_[0-9]+$")


def sanitize_subtask_text_extract_placeholders(schema: dict[str, Any]) -> None:
    """
    Pass B may include "{extract_i}" in the *producing* subtask's text. That is not a real
    runtime dependency (the extract doesn't exist yet) and it breaks validation.

    For any subtask that produces extract variables (via EXTRACT steps), replace occurrences of
    "{extract_i}" with "extract_i" in that same subtask's text.
    """
    plan = schema.get("plan") or {}
    subtasks = plan.get("subtasks") or []

    for st in subtasks:
        si = int(st.get("subtask_i"))
        steps = st.get("steps") or []
        produced_here: set[str] = set()
        for s in steps:
            if s.get("action_type") == "EXTRACT":
                vn = s.get("variable_name")
                if isinstance(vn, str) and _EXTRACT_NAME_RE.fullmatch(vn):
                    produced_here.add(vn)
        if not produced_here:
            continue
        text = st.get("text")
        if not isinstance(text, str) or not text:
            continue
        # Only de-template placeholders for extracts produced in this same subtask.
        for vn in sorted(produced_here, key=lambda x: int(x.split("_")[1])):
            text = text.replace("{" + vn + "}", vn)
        st["text"] = text


def update_dependencies(schema: dict[str, Any]) -> None:
    """
    Infer and populate plan.subtasks[].dependencies based on `{extract_i}` placeholders.

    - Extracts are defined by EXTRACT steps' variable_name (e.g. "extract_0") and are produced
      within a specific subtask_i.
    - Any subtask that *references* `{extract_i}` in its text or step action_value should list
      "extract_i" (without braces) in dependencies, but only if the extract is produced in an
      earlier subtask (upstream-only).
    """
    # Assume schema shape is correct (as produced by our compiler/editor).
    plan = schema.get("plan") or {}
    subtasks = plan.get("subtasks") or []

    # Map extract name -> producing subtask_i.
    produced_in: dict[str, int] = {}
    for st in subtasks:
        si = int(st.get("subtask_i"))
        steps = st.get("steps") or []
        for s in steps:
            if s.get("action_type") != "EXTRACT":
                continue
            vn = s.get("variable_name")
            if isinstance(vn, str) and _EXTRACT_NAME_RE.fullmatch(vn):
                produced_in[vn] = si

    def _find_refs(s: Any) -> set[str]:
        return set(_EXTRACT_PLACEHOLDER_RE.findall(s)) if isinstance(s, str) else set()

    for st in subtasks:
        si = int(st.get("subtask_i"))

        existing_deps = [d for d in (st.get("dependencies") or []) if isinstance(d, str) and d.strip()]

        refs: set[str] = set()
        refs |= _find_refs(st.get("text"))
        steps = st.get("steps") or []
        for s in steps:
            refs |= _find_refs(s.get("action_value"))

        # Only add deps for extracts produced upstream.
        inferred = [
            r for r in sorted(refs, key=lambda x: int(x.split("_")[1])) if (produced_in.get(r, 10**9) < si)
        ]

        # Preserve existing deps, ensure inferred extract deps present, dedupe stable.
        combined: list[str] = []
        seen: set[str] = set()
        for d in existing_deps + inferred:
            if d not in seen:
                seen.add(d)
                combined.append(d)
        st["dependencies"] = combined


def extract_param_templates(step_cards: Iterable[dict[str, Any]]) -> set[str]:
    """
    Utility: extract parameter template names like {{email}} from StepCards.

    Note: Pass A no longer parameterizes step values, so this is primarily useful
    for legacy data / experiments where templates may still appear (e.g., in action_value).
    """
    found: set[str] = set()
    for c in step_cards:
        s = c.get("action_value")
        if isinstance(s, str):
            for m in _TEMPLATE_RE.finditer(s):
                found.add(m.group(1))
    return found


@observe()
def compile_workflow_schema(
    *,
    workflow_dir: str | os.PathLike[str],
    llm_cfg: ResolvedReflectConfig,
) -> dict[str, Any]:
    """
    End-to-end compile:
      - Pass A -> step_cards.json
      - Pass B -> plan_creation.json (raw Pass B output incl. plan_step_updates)
      - Finalize -> schema.json (merged v2 plan.subtasks)
    Returns the final schema object written to schema.json.
    """
    workflow_dir_p = Path(workflow_dir)
    task_name, task_description_user = load_task_metadata(workflow_dir_p)

    logger.info("Schema compile start: workflow_dir=%s task_name=%s model=%s", workflow_dir_p, task_name, llm_cfg.model)

    # If final output exists, reuse it and avoid re-running any passes.
    schema_path = workflow_dir_p / "schema.json"
    existing_schema = _read_json_if_exists(schema_path)
    if (
        isinstance(existing_schema, dict)
        and isinstance(existing_schema.get("plan"), dict)
        and (
            isinstance(existing_schema.get("plan", {}).get("subtasks"), list)
            or isinstance(existing_schema.get("plan", {}).get("steps"), list)
        )
    ):
        logger.info("Schema compile: found existing schema.json; skipping all passes.")
        return existing_schema

    # Pass A: must be complete before Pass B.
    # Note: run_pass_a_step_cards() handles partial persistence + resume via step_cards.json.
    step_cards_path = workflow_dir_p / "step_cards.json"
    existing_step_cards = _read_json_if_exists(step_cards_path)
    if isinstance(existing_step_cards, list) and existing_step_cards:
        step_cards = existing_step_cards
        logger.info("Pass A: found existing step_cards.json; skipping.")
    else:
        step_cards = run_pass_a_step_cards(workflow_dir=workflow_dir_p, llm_cfg=llm_cfg)
        write_step_cards(workflow_dir_p, step_cards)
        logger.info("Pass A complete: step_cards.json (%d steps)", len(step_cards))

    # Pass B checkpoint: plan_creation.json contains RAW Pass B output (incl. plan_step_updates).
    plan_creation_path = workflow_dir_p / "plan_creation.json"
    existing_plan_creation = _read_json_if_exists(plan_creation_path)

    if isinstance(existing_plan_creation, dict) and isinstance(existing_plan_creation.get("plan_step_updates"), list):
        logger.info("Pass B: found existing plan_creation checkpoint; skipping.")
        plan_creation: dict[str, Any] = dict(existing_plan_creation)
    else:
        task_compiler_out = run_pass_b_task_compiler(workflow_dir=workflow_dir_p, step_cards=step_cards, llm_cfg=llm_cfg)

        plan_creation = dict(task_compiler_out)
        write_plan_creation(workflow_dir_p, plan_creation)
        logger.info("Pass B complete: wrote plan_creation.json")

    # Build final schema by merging plan_creation + step_cards into a v2 plan.subtasks format.
    final_schema: dict[str, Any] = {
        "task_name": task_name,
        "task_description_user": task_description_user,
        **plan_creation,
    }

    # Merge Pass B per-step updates back into the full Pass A step cards.
    updates_any = final_schema.get("plan_step_updates")
    subtasks_any = final_schema.get("subtasks")
    if not isinstance(updates_any, list):
        raise RuntimeError("Pass B output missing plan_step_updates (expected list).")
    if not isinstance(subtasks_any, list) or not all(isinstance(s, str) and s for s in subtasks_any):
        raise RuntimeError("Pass B output missing subtasks (expected list[str]).")

    updates_by_i: dict[int, dict[str, Any]] = {}
    for u in updates_any:
        if not isinstance(u, dict):
            continue
        ii = u.get("i")
        if isinstance(ii, int):
            updates_by_i[ii] = u
        elif isinstance(ii, str) and ii.isdigit():
            updates_by_i[int(ii)] = u

    missing_updates = [i for i in range(len(step_cards)) if i not in updates_by_i]
    if missing_updates:
        raise RuntimeError(f"Pass B output incomplete: missing plan_step_updates for step indices: {missing_updates}")

    # Preserve refined extract queries from Pass A before Pass B overwrites action_value for EXTRACT steps.
    # Store under additional_args so future per-step arguments can be extended without new top-level fields.
    for s in step_cards:
        if not isinstance(s, dict):
            continue
        aa = s.get("additional_args")
        if not isinstance(aa, dict):
            aa = {}
        if s.get("action_type") == "EXTRACT":
            av0 = s.get("action_value")
            if isinstance(av0, str) and av0.strip():
                aa["extract_query"] = av0.strip()
            else:
                aa.pop("extract_query", None)
        else:
            aa.pop("extract_query", None)
        s["additional_args"] = aa
        # Backward-compat cleanup: do not carry old field forward.
        s.pop("extract_query", None)

    subtask_set = set(subtasks_any)
    for i, s in enumerate(step_cards):
        u = updates_by_i[i]
        subtask = u.get("subtask")
        if not isinstance(subtask, str) or subtask not in subtask_set:
            raise RuntimeError(f"Pass B step {i}: subtask must be exactly one of subtasks[]")
        s["subtask"] = subtask

        # Only overwrite action_value if Pass B provides it (including explicit null).
        if "action_value" in u:
            s["action_value"] = u.get("action_value")

        # Safety: enforce action_value nullability by action_type after merge.
        at = s.get("action_type")
        av = s.get("action_value")
        if at in {"TYPE", "KEYPRESS"}:
            # Allow null when value should be sourced from upstream extracts (Pass B prompt directs this).
            if av is not None and not isinstance(av, str):
                raise RuntimeError(f"Step {i}: action_type={at} requires action_value to be a string or null after Pass B merge.")
        elif at == "EXTRACT":
            if not isinstance(av, str) or not av.strip():
                raise RuntimeError(f"Step {i}: action_type={at} requires non-empty action_value after Pass B merge.")
        else:
            if av is not None:
                raise RuntimeError(f"Step {i}: action_type={at} requires action_value=null after Pass B merge.")

    # Ensure EXTRACT steps have a non-empty additional_args.extract_query after merge.
    for i, s in enumerate(step_cards):
        if s.get("action_type") != "EXTRACT":
            continue
        aa = s.get("additional_args") if isinstance(s.get("additional_args"), dict) else {}
        eq = aa.get("extract_query") if isinstance(aa, dict) else None
        if not isinstance(eq, str) or not eq.strip():
            raise RuntimeError(
                f"Step {i}: EXTRACT step missing additional_args.extract_query (refined query) from Pass A."
            )

    # Remove plan_step_updates from top-level schema (we've merged it into plan.subtasks[].steps).
    final_schema.pop("plan_step_updates", None)

    # Build new v2 plan format: plan.subtasks[] where each subtask contains its steps.
    # Do NOT keep top-level subtasks[] in the final schema; subtask text lives in plan.subtasks[].text.
    steps_by_subtask: dict[str, list[dict[str, Any]]] = {st: [] for st in subtasks_any}
    for s in step_cards:
        st = s.get("subtask")
        if not isinstance(st, str) or st not in steps_by_subtask:
            raise RuntimeError("Internal error: step missing valid subtask assignment after merge.")
        steps_by_subtask[st].append(s)

    plan_subtasks: list[dict[str, Any]] = []
    for subtask_i, st_text in enumerate(subtasks_any):
        steps_out: list[dict[str, Any]] = []
        for local_i, s in enumerate(steps_by_subtask.get(st_text, [])):
            # Make step indices subtask-local only.
            s2 = dict(s)
            s2["i"] = local_i
            # Remove redundant global subtask string from each step (lives on parent).
            s2.pop("subtask", None)
            # Do not keep details in final schema (they should be encoded into intent/etc by Pass A/B).
            s2.pop("details", None)
            # Backward-compat cleanup: remove legacy extract_query field if present.
            s2.pop("extract_query", None)
            steps_out.append(s2)
        plan_subtasks.append(
            {
                "subtask_i": subtask_i,
                "text": st_text,
                "dependencies": [],
                "steps": steps_out,
            }
        )

    final_schema.pop("subtasks", None)
    final_schema["plan"] = {"subtasks": plan_subtasks}

    # Pass B sometimes writes "{extract_i}" into the producing subtask text; de-template those.
    sanitize_subtask_text_extract_placeholders(final_schema)

    # Populate dependencies based on any `{extract_i}` references in subtask text/action_value.
    update_dependencies(final_schema)

    write_schema(workflow_dir_p, final_schema)
    logger.info("Wrote schema.json")
    return final_schema
