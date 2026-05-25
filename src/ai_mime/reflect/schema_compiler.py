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
from typing import Any, Callable, Iterable, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field  # type: ignore[import-not-found]
from tqdm import tqdm  # type: ignore[import-not-found]

from ai_mime.user_config import ResolvedReflectConfig
from ai_mime.litellm_client import LiteLLMChatClient
from ai_mime.debug_log import log as debug_log

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

OptimizedExecutor = Literal["script", "browser_harness", "ui_agent"]


class PassCFilesystemAccessPath(BaseModel):
    """A user filesystem path Pass C believes the optimized execution needs."""
    path: str = Field(description="Absolute user filesystem path or directory needed by the task.")
    reason: str = Field(description="Why the optimized executor needs access to this path.")
    approval_required: bool = Field(
        default=False,
        description="Whether this access must be approved before the runner grants it.",
    )


class PassCUserFilesystemAccess(BaseModel):
    """User filesystem access hints. Workflow/temp/output permissions are runner-owned."""
    readable_roots: list[PassCFilesystemAccessPath] = Field(
        default_factory=list,
        description="User filesystem paths the task may need to read.",
    )
    writable_roots: list[PassCFilesystemAccessPath] = Field(
        default_factory=list,
        description="User filesystem paths the task may need to write outside the workflow workspace.",
    )


class PassCInputSpec(BaseModel):
    """Top-level optimized-plan input variable."""
    name: str = Field(description="Stable snake_case variable name.")
    description: str = Field(description="Human-readable description of this input.")
    required: bool = Field(description="Whether the caller must provide this input before execution.")
    default: str | None = Field(default=None, description="Optional inferred/default value.")


class PassCStep(BaseModel):
    """One executor-owned optimized step, potentially covering multiple schema subtasks."""
    id: str = Field(description="Unique snake_case step id.")
    title: str = Field(description="Short human-readable step title.")
    source_subtask_ids: list[int] = Field(
        min_length=1,
        description="One or more source schema plan.subtasks indexes covered by this optimized step.",
    )
    executor: OptimizedExecutor = Field(description="Executor that should own this optimized step.")
    goal: str = Field(description="Concrete execution goal. Mention any LLM/OCR needs here if relevant.")
    inputs: list[str] = Field(default_factory=list, description="Variable names this step consumes.")
    outputs: list[str] = Field(default_factory=list, description="Variable names this step produces.")
    success_criteria: str = Field(description="How to know this optimized step succeeded.")
    fallback: OptimizedExecutor = Field(description="Fallback executor (usually ui_agent).")


class PassCOutput(BaseModel):
    """Optimized execution strategy produced by Pass C."""
    version: Literal[1] = Field(description="Optimized plan schema version. Must be 1.")
    workflow_goal: str = Field(description="Concise goal of the workflow.")
    user_filesystem_access: PassCUserFilesystemAccess = Field(
        default_factory=PassCUserFilesystemAccess,
        description="User filesystem read/write access hints only.",
    )
    inputs: list[PassCInputSpec] = Field(default_factory=list, description="Top-level workflow input variables.")
    steps: list[PassCStep] = Field(min_length=1, description="Optimized executor-owned steps.")


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


def _should_use_fallback(llm_cfg: ResolvedReflectConfig) -> bool:
    if not llm_cfg.api_key_env:
        return True
    val = os.getenv(llm_cfg.api_key_env)
    return val is None or not val.strip()


def _extract_json_from_text(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    candidates: list[str] = [text.strip()]
    fence = text.rsplit("```json", 1)
    if len(fence) == 2:
        candidates.append(fence[1].split("```", 1)[0].strip())
    fence2 = text.rsplit("```", 1)
    if len(fence2) == 2:
        parts = text.split("```")
        if len(parts) >= 3:
            candidates.append(parts[1].strip())
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1].strip())
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _claude_sdk_structured_fallback(
    workflow_dir: Path,
    system_prompt: str,
    prompt_content: str,
    response_model: type[BaseModel],
) -> dict[str, Any]:
    from ai_mime.agent_runner.adapters.claude_sdk import run_claude_sdk_structured

    raw_output = run_claude_sdk_structured(
        workflow_dir=workflow_dir,
        system_prompt=system_prompt,
        prompt_content=prompt_content,
        response_schema=response_model.model_json_schema(),
    )

    parsed_json = _extract_json_from_text(raw_output)
    if parsed_json is None:
        raise ValueError(f"Failed to extract valid JSON matching {response_model.__name__} from Claude response: {raw_output}")

    validated = response_model.model_validate(parsed_json)
    return validated.model_dump()




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


PASS_C_SYSTEM_PROMPT = """You are an optimized workflow strategy compiler.
You convert a completed UI replay schema into a compact executor-oriented plan.

Executors (pick exactly one per step):
- script: pure deterministic Python — file IO, HTTP, parsing, shell-outs, library
  calls. No UI of any kind. May call ask_gemini() for irreducible judgment that
  returns a JSON-schema-constrained answer. This is the preferred executor.
- browser_harness: composable browser-harness CDP script (new_tab, js,
  click_at_xy, wait_for_*, etc.) when the workflow genuinely needs a tab. May
  also call ask_gemini() for in-page judgment.
- ui_agent: live screenshot + click loop. Slowest and least reliable; last
  resort, for native macOS apps or web UIs whose DOM is too hostile for CDP.

ask_gemini is the stochasticity escape hatch: if a step has one fuzzy decision
(e.g. "pick the row that matches the user's query"), keep the step as script or
browser_harness and let ask_gemini handle the decision — do NOT downgrade to
ui_agent just because one sub-decision is judgment-based.

Prefer the smarter deterministic path over the recorded UI path.
The recording shows what the user *did*; you should produce what they *meant*,
by whatever route survives replay most reliably. Whenever a chunk of UI steps
can be replaced by a smarter path that reaches the same end state, prefer that
path and collapse the source subtasks into a single script (or browser_harness)
step. Examples of "smarter paths" — non-exhaustive, pick whichever fits:
- A direct API or CLI call instead of clicking through the UI.
- A deep-link / URL-scheme that jumps past a navigation flow.
- Reading or writing the underlying file directly (PDF parsing, CSV/JSON edit,
  plist/SQLite for a native app) instead of opening the app.
- A keyboard shortcut, AppleScript/osascript, or built-in OS command that
  replaces a multi-click path.
- A query parameter or hidden route that skips a loader/wizard.
- Any other shortcut the recording missed because the user did it manually.

When you swap in a smarter path, state it in `goal` (e.g. "reads PDF text
directly via pdfplumber instead of opening Preview", "uses URL scheme to open
file in-place", "calls hosts CLI instead of System Settings UI"). The skill
builder will verify the chosen path actually works.

Rules:
- Do not name tools. Describe OCR / extraction / model judgement needs inside
  `goal`; the runner picks the implementation.
- Group adjacent source subtasks when one executor can do them faster.
- Prefer `script` whenever the work does not require a browser tab or native UI.
- Prefer `browser_harness` over `ui_agent` for any web work; only fall back to
  `ui_agent` when CDP cannot survive replay.
- Every step must include a `fallback` executor (usually `ui_agent`).
- Use semantic variable names for data passed between steps.
- `user_filesystem_access` is only for user files/directories needed by the task.
- Do not include workflow_dir, outputs, agent state, skills, or temp directories
  in `user_filesystem_access`.
- Mark user filesystem writes `approval_required=true` unless they are narrow
  task-specific files.
- When you replace a UI chunk with a smarter path, `source_subtask_ids` should
  still list every collapsed source subtask — the mapping back to the recording
  stays intact even when the execution path diverges.
"""


PASS_C_USER_TEMPLATE = """Task name: {task_name}
User task description: {task_description_user}

Current final schema summary:
{schema_summary_json}

Manifest action summaries:
{manifest_summary_json}

Discovered local file hints:
{file_hints_json}

Generate optimized_plan.json version 1.

Output requirements:
- user_filesystem_access: include only user filesystem paths/directories needed by the optimized task, with reasons.
- Do not include workflow/output/temp directories in user_filesystem_access; the runner grants those separately.
- inputs: top-level variables needed before execution or inferred from recording.
- steps: optimized executor steps. source_subtask_ids may contain multiple schema subtask indexes.
- step inputs must reference top-level inputs or earlier step outputs.
- step outputs must be unique.
- executor and fallback must be one of: script, browser_harness, ui_agent.
- Prefer `script` for any work that does not require a browser tab or native UI.
- Before emitting any `browser_harness` or `ui_agent` step, ask: is there a smarter, more deterministic way to reach the same end state that the recording happened to miss (direct file read/write, CLI, URL-scheme, shortcut, library call, etc.)? If yes, emit a single `script` step using that path and state the chosen shortcut in `goal`. Only keep UI-level steps when no shortcut is viable.
- If a receipt/PDF/bill is opened only for extraction, collapse file opening + extraction into a single `script` step that parses the file directly; mention ask_gemini in `goal` if semantic OCR is needed.
- If Chrome/browser/Google Sheets/web tabs are unavoidable, collapse the browser work into a single `browser_harness` step.
"""


class StepTarget(BaseModel):
    """Coordinate-free selector description for the target UI element."""
    primary: str = Field(description="Primary target description using visible text/icons + relative location.")
    fallback: str | None = Field(
        default=None,
        description="Fallback target description if primary isn't found, still coordinate-free.",
    )


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
    debug_log(f"derive_step_inputs: processing {len(events)} events")
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

    debug_log(f"derive_step_inputs: found {len(steps)} actionable steps")
    if not steps:
        debug_log("WARNING: No actionable steps found in manifest! Schema will be empty.")

    return steps


@observe()
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
    debug_log(f"Pass A: total_steps={len(steps)} existing={len(existing_by_i)} model={pass_a_model}")
    logger.info(
        "Pass A: total_steps=%d existing=%d (model=%s) with up to 5 in-flight requests",
        len(steps),
        len(existing_by_i),
        pass_a_model,
    )

    # Resolve + cache LLM config once; reused across all Pass A steps (thread-safe usage).
    pass_a_client = None
    if not _should_use_fallback(llm_cfg):
        pass_a_client = LiteLLMChatClient(
            model=pass_a_model,
            api_base=llm_cfg.api_base,
            api_key_env=llm_cfg.api_key_env,
            extra_kwargs=llm_cfg.extra_kwargs,
            max_retries=MAX_RETRIES,
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

        if pass_a_client is None:
            card = _claude_sdk_structured_fallback(
                workflow_dir=workflow_dir_p,
                system_prompt=PASS_A_SYSTEM_PROMPT,
                prompt_content=(
                    f"Task name: {task_name}\n"
                    f"User task description: {task_description_user}\n\n"
                    f"Action: {json.dumps(s.action_json)}\n"
                    f"Param Hint: {json.dumps(s.param_hint_json)}\n"
                    f"Details: {s.details}\n"
                    f"PRE screenshot is located at: {s.pre_screenshot}\n"
                    f"POST screenshot is located at: {s.post_screenshot}\n\n"
                    f"Instructions: {user}\n\n"
                ),
                response_model=StepCardModel,
            )
        else:
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": PASS_A_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [{"type": "text", "text": user}]
                    + [{"type": "image_url", "image_url": {"url": _png_data_url(p)}} for p in img_paths],
                },
            ]

            event = pass_a_client.create(
                response_model=StepCardModel,
                messages=messages,
                max_tokens=llm_cfg.pass_a_max_tokens,
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
        debug_log("Pass A: all steps already present; skipping.")
        logger.info("Pass A: all steps already present; skipping.")
        return [existing_by_i[i] for i in range(len(steps))]

    debug_log(f"Pass A: compiling {len(missing)} missing steps (will make LLM calls)")
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


@observe()
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

    if _should_use_fallback(llm_cfg):
        return _claude_sdk_structured_fallback(
            workflow_dir=workflow_dir_p,
            system_prompt=PASS_B_SYSTEM_PROMPT,
            prompt_content=user,
            response_model=PassBOutput,
        )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": PASS_B_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]
    client = LiteLLMChatClient(
        model=pass_b_model,
        api_base=llm_cfg.api_base,
        api_key_env=llm_cfg.api_key_env,
        extra_kwargs=llm_cfg.extra_kwargs,
        max_retries=MAX_RETRIES,
    )
    event = client.create(
        response_model=PassBOutput,
        messages=messages,
        max_tokens=llm_cfg.pass_b_max_tokens,
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


def write_optimized_plan(workflow_dir: str | os.PathLike[str], optimized_plan: dict[str, Any]) -> Path:
    workflow_dir_p = Path(workflow_dir)
    path = workflow_dir_p / "optimized_plan.json"
    _write_json_atomic(path, optimized_plan)
    return path


def _schema_subtask_count(schema: dict[str, Any]) -> int:
    plan = schema.get("plan") if isinstance(schema.get("plan"), dict) else {}
    subtasks = plan.get("subtasks") if isinstance(plan, dict) else []
    return len(subtasks) if isinstance(subtasks, list) else 0


def _expanded_user_home_paths() -> set[str]:
    home = Path.home()
    return {
        "/",
        "$HOME",
        "~",
        str(home),
        "~/Desktop",
        str(home / "Desktop"),
        "~/Documents",
        str(home / "Documents"),
        "~/Downloads",
        str(home / "Downloads"),
    }


def _validate_user_filesystem_access(plan: PassCOutput) -> None:
    access = plan.user_filesystem_access
    for field_name, entries in (
        ("readable_roots", access.readable_roots),
        ("writable_roots", access.writable_roots),
    ):
        seen: set[str] = set()
        for entry in entries:
            path = entry.path.strip()
            if not path:
                raise ValueError(f"optimized_plan.user_filesystem_access.{field_name}[].path must be non-empty")
            if path in seen:
                raise ValueError(f"optimized_plan.user_filesystem_access.{field_name} path duplicated: {path}")
            seen.add(path)
            if not entry.reason.strip():
                raise ValueError(
                    f"optimized_plan.user_filesystem_access.{field_name} path {path!r}: reason must be non-empty"
                )
            if field_name == "writable_roots" and path in _expanded_user_home_paths() and not entry.approval_required:
                raise ValueError(
                    "optimized_plan.user_filesystem_access.writable_roots broad path "
                    f"{path!r} must set approval_required=true"
                )


def validate_optimized_plan(optimized_plan: dict[str, Any], schema: dict[str, Any]) -> None:
    """
    Validate the minimal Pass C optimized_plan.json contract.
    Raises ValueError with a readable message when the plan is invalid.
    """
    try:
        plan = PassCOutput.model_validate(optimized_plan)
    except Exception as e:
        raise ValueError(f"optimized_plan failed schema validation: {e}") from e

    if plan.version != 1:
        raise ValueError("optimized_plan.version must be 1")
    if not plan.workflow_goal.strip():
        raise ValueError("optimized_plan.workflow_goal must be non-empty")
    _validate_user_filesystem_access(plan)

    input_names: set[str] = set()
    for inp in plan.inputs:
        name = inp.name.strip()
        if not name:
            raise ValueError("optimized_plan.inputs[].name must be non-empty")
        if name in input_names:
            raise ValueError(f"optimized_plan input name duplicated: {name}")
        input_names.add(name)
        if not inp.description.strip():
            raise ValueError(f"optimized_plan input {name}: description must be non-empty")

    if not plan.steps:
        raise ValueError("optimized_plan.steps must be non-empty")

    subtask_count = _schema_subtask_count(schema)
    if subtask_count <= 0:
        raise ValueError("schema has no plan.subtasks for optimized_plan source_subtask_ids validation")

    step_ids: set[str] = set()
    available_vars: set[str] = set(input_names)
    produced_vars: set[str] = set()
    for step in plan.steps:
        sid = step.id.strip()
        if not sid:
            raise ValueError("optimized_plan.steps[].id must be non-empty")
        if sid in step_ids:
            raise ValueError(f"optimized_plan step id duplicated: {sid}")
        step_ids.add(sid)

        if not step.title.strip():
            raise ValueError(f"optimized_plan step {sid}: title must be non-empty")
        if not step.goal.strip():
            raise ValueError(f"optimized_plan step {sid}: goal must be non-empty")
        if not step.success_criteria.strip():
            raise ValueError(f"optimized_plan step {sid}: success_criteria must be non-empty")

        if not step.source_subtask_ids:
            raise ValueError(f"optimized_plan step {sid}: source_subtask_ids must be non-empty")
        for si in step.source_subtask_ids:
            if not isinstance(si, int) or si < 0 or si >= subtask_count:
                raise ValueError(
                    f"optimized_plan step {sid}: invalid source_subtask_id={si!r}; schema has {subtask_count} subtasks"
                )

        for name in step.inputs:
            if name not in available_vars:
                raise ValueError(f"optimized_plan step {sid}: unknown input variable {name!r}")

        for name in step.outputs:
            name_s = str(name).strip()
            if not name_s:
                raise ValueError(f"optimized_plan step {sid}: outputs must be non-empty strings")
            if name_s in input_names:
                raise ValueError(f"optimized_plan step {sid}: output {name_s!r} duplicates a top-level input")
            if name_s in produced_vars:
                raise ValueError(f"optimized_plan output duplicated across steps: {name_s}")
            produced_vars.add(name_s)
            available_vars.add(name_s)


def _iter_strings(obj: Any) -> Iterable[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_strings(v)


def _summarize_schema_for_pass_c(schema: dict[str, Any]) -> dict[str, Any]:
    plan = schema.get("plan") if isinstance(schema.get("plan"), dict) else {}
    subtasks_any = plan.get("subtasks") if isinstance(plan, dict) else []
    subtasks_out: list[dict[str, Any]] = []
    if isinstance(subtasks_any, list):
        for st in subtasks_any:
            if not isinstance(st, dict):
                continue
            steps_out: list[dict[str, Any]] = []
            for s in st.get("steps") or []:
                if not isinstance(s, dict):
                    continue
                steps_out.append(
                    {
                        "i": s.get("i"),
                        "intent": s.get("intent"),
                        "action_type": s.get("action_type"),
                        "action_value": s.get("action_value"),
                        "variable_name": s.get("variable_name"),
                        "target_primary": (
                            (s.get("target") or {}).get("primary") if isinstance(s.get("target"), dict) else None
                        ),
                        "expected_current_state": s.get("expected_current_state"),
                    }
                )
            subtasks_out.append(
                {
                    "subtask_i": st.get("subtask_i"),
                    "text": st.get("text"),
                    "dependencies": st.get("dependencies") or [],
                    "steps": steps_out,
                }
            )
    return {
        "task_name": schema.get("task_name"),
        "task_description_user": schema.get("task_description_user"),
        "detailed_task_description": schema.get("detailed_task_description"),
        "task_params": schema.get("task_params") or [],
        "success_criteria": schema.get("success_criteria"),
        "subtasks": subtasks_out,
    }


def _summarize_manifest_for_pass_c(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, e in enumerate(events):
        if not isinstance(e, dict):
            continue
        details = e.get("action_details") if isinstance(e.get("action_details"), dict) else {}
        slim_details: dict[str, Any] = {}
        if e.get("action_type") == "type":
            slim_details["text"] = details.get("text")
        elif e.get("action_type") == "key":
            slim_details["key"] = details.get("key")
        elif e.get("action_type") == "extract":
            slim_details["query"] = details.get("query")
            slim_details["values"] = details.get("values")
        elif e.get("action_type") in {"click", "double_click", "right_click", "middle_click", "scroll", "drag"}:
            for k in ("button", "dx", "dy"):
                if k in details:
                    slim_details[k] = details.get(k)
        out.append(
            {
                "event_i": i,
                "action_type": e.get("action_type"),
                "action_details": slim_details,
                "details": e.get("details"),
                "screenshot": e.get("screenshot"),
            }
        )
    return out


_FILE_NAME_RE = re.compile(r"[^/\\\n\r\t\"']+\.(?:pdf|csv|tsv|xlsx|xls|docx|png|jpe?g|webp)", re.IGNORECASE)


def _discover_file_hints_for_pass_c(workflow_dir: Path, schema: dict[str, Any], events: list[dict[str, Any]]) -> list[str]:
    """
    Best-effort local file hints for Pass C. This is intentionally conservative:
    only search exact filenames mentioned in schema/manifest under the workflow dir and ~/Desktop.
    """
    text_blob = "\n".join(_iter_strings(schema)) + "\n" + "\n".join(_iter_strings(events))
    names: list[str] = []
    seen_names: set[str] = set()
    for m in _FILE_NAME_RE.finditer(text_blob):
        name = m.group(0).strip()
        if name and name not in seen_names:
            seen_names.add(name)
            names.append(name)
    if not names:
        return []

    roots = [workflow_dir, Path.home() / "Desktop"]
    found: list[str] = []
    seen_paths: set[str] = set()

    def _walk_limited(root: Path, max_depth: int = 4) -> Iterable[Path]:
        if not root.exists():
            return
        root = root.resolve()
        for dirpath, dirnames, filenames in os.walk(root):
            cur = Path(dirpath)
            try:
                depth = len(cur.relative_to(root).parts)
            except Exception:
                depth = 0
            if depth >= max_depth:
                dirnames[:] = []
            for fn in filenames:
                yield cur / fn

    wanted = set(names)
    for root in roots:
        for p in _walk_limited(root):
            if p.name not in wanted:
                continue
            ps = str(p)
            if ps not in seen_paths:
                seen_paths.add(ps)
                found.append(ps)
    return found[:20]


@observe()
def run_pass_c_optimizer(
    *,
    workflow_dir: str | os.PathLike[str],
    schema: dict[str, Any],
    llm_cfg: ResolvedReflectConfig,
) -> dict[str, Any]:
    """
    Pass C: compile a completed schema into a compact optimized execution strategy.
    """
    workflow_dir_p = Path(workflow_dir)
    task_name = str(schema.get("task_name") or "")
    task_description_user = str(schema.get("task_description_user") or "")
    events = load_events(workflow_dir_p)

    pass_c_model = llm_cfg.pass_b_model or llm_cfg.model
    logger.info("Pass C: compiling optimized plan (model=%s)", pass_c_model)
    debug_log(f"Pass C: starting optimized plan generation model={pass_c_model}")

    user = PASS_C_USER_TEMPLATE.format(
        task_name=task_name,
        task_description_user=task_description_user,
        schema_summary_json=json.dumps(_summarize_schema_for_pass_c(schema), ensure_ascii=False),
        manifest_summary_json=json.dumps(_summarize_manifest_for_pass_c(events), ensure_ascii=False),
        file_hints_json=json.dumps(_discover_file_hints_for_pass_c(workflow_dir_p, schema, events), ensure_ascii=False),
    )

    if _should_use_fallback(llm_cfg):
        optimized_plan = _claude_sdk_structured_fallback(
            workflow_dir=workflow_dir_p,
            system_prompt=PASS_C_SYSTEM_PROMPT,
            prompt_content=user,
            response_model=PassCOutput,
        )
        validate_optimized_plan(optimized_plan, schema)
        return optimized_plan

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": PASS_C_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]
    client = LiteLLMChatClient(
        model=pass_c_model,
        api_base=llm_cfg.api_base,
        api_key_env=llm_cfg.api_key_env,
        extra_kwargs=llm_cfg.extra_kwargs,
        max_retries=MAX_RETRIES,
    )
    event = client.create(
        response_model=PassCOutput,
        messages=messages,
        max_tokens=llm_cfg.pass_b_max_tokens,
    )
    optimized_plan = event.model_dump()
    validate_optimized_plan(optimized_plan, schema)
    return optimized_plan


def create_optimized_plan(
    *,
    workflow_dir: str | os.PathLike[str],
    schema: dict[str, Any],
    llm_cfg: ResolvedReflectConfig,
) -> dict[str, Any]:
    workflow_dir_p = Path(workflow_dir)
    path = workflow_dir_p / "optimized_plan.json"
    existing = _read_json_if_exists(path)
    if isinstance(existing, dict):
        try:
            validate_optimized_plan(existing, schema)
            debug_log("Pass C: found valid optimized_plan.json; skipping.")
            logger.info("Pass C: found valid optimized_plan.json; skipping.")
            cleanup_reflect_artifacts(workflow_dir_p, schema=schema, optimized_plan=existing)
            return existing
        except Exception as e:
            debug_log(f"Pass C: existing optimized_plan.json invalid; regenerating: {e}")
            logger.info("Pass C: existing optimized_plan.json invalid; regenerating: %s", e)

    optimized_plan = run_pass_c_optimizer(workflow_dir=workflow_dir_p, schema=schema, llm_cfg=llm_cfg)
    write_optimized_plan(workflow_dir_p, optimized_plan)
    cleanup_reflect_artifacts(workflow_dir_p, schema=schema, optimized_plan=optimized_plan)
    debug_log(f"Pass C complete: {path}")
    logger.info("Pass C complete: wrote optimized_plan.json")
    return optimized_plan


def cleanup_reflect_artifacts(
    workflow_dir: str | os.PathLike[str],
    *,
    schema: dict[str, Any],
    optimized_plan: dict[str, Any],
) -> list[Path]:
    """
    Remove reflect-only workflow artifacts once schema.json and optimized_plan.json are valid.

    The original recording directory is not touched; this only cleans files copied/generated
    inside the workflow directory.
    """
    workflow_dir_p = Path(workflow_dir)
    if not (workflow_dir_p / "schema.json").exists():
        raise RuntimeError(f"Cannot cleanup reflect artifacts before schema.json exists: {workflow_dir_p}")
    if not (workflow_dir_p / "optimized_plan.json").exists():
        raise RuntimeError(f"Cannot cleanup reflect artifacts before optimized_plan.json exists: {workflow_dir_p}")
    validate_optimized_plan(optimized_plan, schema)

    removed: list[Path] = []

    manifest_path = workflow_dir_p / "manifest.jsonl"
    screenshot_paths: set[Path] = set()
    if manifest_path.exists():
        try:
            for event in _read_jsonl(manifest_path):
                rel = event.get("screenshot") if isinstance(event, dict) else None
                if not isinstance(rel, str) or not rel.strip():
                    continue
                candidate = (workflow_dir_p / rel).resolve()
                try:
                    candidate.relative_to(workflow_dir_p.resolve())
                except ValueError:
                    continue
                screenshot_paths.add(candidate)
        except Exception as e:
            logger.info("Reflect cleanup: could not parse manifest screenshots: %s", e)

    for path in sorted(screenshot_paths):
        if path.exists() and path.is_file():
            path.unlink()
            removed.append(path)

    for name in (
        # "manifest.jsonl",
        "step_cards.json",
        "plan_creation.json",
        "schema.draft.json",
        "schema.draft.v1.json",
        "schema.v1.json",
    ):
        path = workflow_dir_p / name
        if path.exists() and path.is_file():
            path.unlink()
            removed.append(path)

    if removed:
        logger.info("Reflect cleanup removed %d artifacts from %s", len(removed), workflow_dir_p)
        debug_log(f"Reflect cleanup removed {len(removed)} artifacts from {workflow_dir_p}")
    return removed


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
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
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

    def _progress(event_type: str, *, phase: str, label: str, progress: int) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback({
                "type": event_type,
                "phase": phase,
                "label": label,
                "progress": progress,
                "workflow_dir": str(workflow_dir_p),
            })
        except Exception:
            pass

    debug_log(f"Schema compile start: workflow_dir={workflow_dir_p} task={task_name} model={llm_cfg.model}")
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
        logger.info("Schema compile: found existing schema.json; skipping Pass A/B/finalization.")
        _progress("reflect_progress", phase="pass_a_complete", label="Pass A", progress=33)
        _progress("reflect_progress", phase="pass_b_complete", label="Pass B", progress=66)
        _progress("reflect_progress", phase="optimized_plan_started", label="Optimized plan", progress=82)
        optimized_plan = create_optimized_plan(workflow_dir=workflow_dir_p, schema=existing_schema, llm_cfg=llm_cfg)
        _progress("reflect_progress", phase="optimized_plan_complete", label="Optimized plan", progress=100)
        return existing_schema

    # Pass A: must be complete before Pass B.
    # Note: run_pass_a_step_cards() handles partial persistence + resume via step_cards.json.
    step_cards_path = workflow_dir_p / "step_cards.json"
    existing_step_cards = _read_json_if_exists(step_cards_path)
    if isinstance(existing_step_cards, list) and existing_step_cards:
        step_cards = existing_step_cards
        debug_log(f"Pass A: found existing step_cards.json with {len(step_cards)} steps; skipping.")
        logger.info("Pass A: found existing step_cards.json; skipping.")
        _progress("reflect_progress", phase="pass_a_complete", label="Pass A", progress=33)
    else:
        _progress("reflect_progress", phase="pass_a_started", label="Pass A", progress=10)
        debug_log("Pass A: starting step card generation...")
        step_cards = run_pass_a_step_cards(workflow_dir=workflow_dir_p, llm_cfg=llm_cfg)
        write_step_cards(workflow_dir_p, step_cards)
        debug_log(f"Pass A complete: {len(step_cards)} steps")
        logger.info("Pass A complete: step_cards.json (%d steps)", len(step_cards))
        _progress("reflect_progress", phase="pass_a_complete", label="Pass A", progress=33)

    # Validate that we have actionable steps before proceeding
    if not step_cards:
        error_msg = (
            f"No actionable steps found in {workflow_dir_p.name}. "
            "The recording may have no meaningful actions (clicks, typing, etc.). "
            "Please record a new session with actual UI interactions."
        )
        debug_log(f"ERROR: {error_msg}")
        raise RuntimeError(error_msg)

    # Pass B checkpoint: plan_creation.json contains RAW Pass B output (incl. plan_step_updates).
    plan_creation_path = workflow_dir_p / "plan_creation.json"
    existing_plan_creation = _read_json_if_exists(plan_creation_path)

    if isinstance(existing_plan_creation, dict) and isinstance(existing_plan_creation.get("plan_step_updates"), list):
        debug_log("Pass B: found existing plan_creation; skipping.")
        logger.info("Pass B: found existing plan_creation checkpoint; skipping.")
        plan_creation: dict[str, Any] = dict(existing_plan_creation)
        _progress("reflect_progress", phase="pass_b_complete", label="Pass B", progress=66)
    else:
        _progress("reflect_progress", phase="pass_b_started", label="Pass B", progress=45)
        debug_log("Pass B: starting task compilation...")
        task_compiler_out = run_pass_b_task_compiler(workflow_dir=workflow_dir_p, step_cards=step_cards, llm_cfg=llm_cfg)

        plan_creation = dict(task_compiler_out)
        write_plan_creation(workflow_dir_p, plan_creation)
        debug_log("Pass B complete")
        logger.info("Pass B complete: wrote plan_creation.json")
        _progress("reflect_progress", phase="pass_b_complete", label="Pass B", progress=66)

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
    _progress("reflect_progress", phase="optimized_plan_started", label="Optimized plan", progress=82)
    optimized_plan = create_optimized_plan(workflow_dir=workflow_dir_p, schema=final_schema, llm_cfg=llm_cfg)
    _progress("reflect_progress", phase="optimized_plan_complete", label="Optimized plan", progress=100)

    debug_log(f"Schema compilation complete: {workflow_dir_p / 'schema.json'}")
    logger.info("Wrote schema.json")
    return final_schema
