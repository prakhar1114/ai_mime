from __future__ import annotations

import re


def validate_schema(schema: dict) -> None:
    """
    Best-effort validation of the workflow schema format used by this repo.
    Raises ValueError with a readable message on failure.
    """
    # Assume schema shape is correct (as produced by our compiler/editor).
    # Still raise ValueError with readable messages for real workflow issues.
    try:
        plan = schema["plan"]
        subtasks = plan["subtasks"]
    except Exception as e:
        raise ValueError(f"schema missing required plan/subtasks structure: {e}") from e

    allowed_action_types = {"CLICK", "TYPE", "SCROLL", "KEYPRESS", "DRAG", "EXTRACT"}
    extract_re = re.compile(r"^extract_[0-9]+$")
    extract_placeholder_re = re.compile(r"\{(extract_[0-9]+)\}")

    # Map extract variable_name -> producing subtask_i (used to validate dependencies are upstream).
    produced_in: dict[str, int] = {}
    for si, st in enumerate(subtasks):
        for step in st.get("steps") or []:
            if step.get("action_type") == "EXTRACT":
                vn = step.get("variable_name")
                if extract_re.fullmatch(vn):
                    produced_in[vn] = si

    for si, st in enumerate(subtasks):
        if st.get("subtask_i") != si:
            raise ValueError(f"subtask {si}: subtask_i must equal its list index")
        if not str(st.get("text") or "").strip():
            raise ValueError(f"subtask {si}: missing non-empty text")
        deps = st.get("dependencies")
        if deps is None:
            deps = []
            st["dependencies"] = deps
        if not isinstance(deps, list):
            raise ValueError(f"subtask {si}: dependencies must be a list[str]")

        steps = st.get("steps")
        if not isinstance(steps, list):
            raise ValueError(f"subtask {si}: steps must be a list")

        # Validate that any `{extract_i}` references in this subtask are declared in dependencies,
        # and that those dependencies are produced upstream.
        def _extract_refs_from_string(s: object) -> set[str]:
            return set(extract_placeholder_re.findall(s)) if isinstance(s, str) else set()

        refs: set[str] = set()
        refs |= _extract_refs_from_string(st.get("text"))
        for step in steps:
            refs |= _extract_refs_from_string(step.get("action_value"))
        for r in sorted(refs):
            if r not in deps:
                raise ValueError(f"subtask {si}: references {{{r}}} but dependencies is missing '{r}'")
            prod_si = produced_in.get(r)
            if prod_si is None:
                raise ValueError(f"subtask {si}: dependency '{r}' is not produced by any EXTRACT step upstream")
            if prod_si >= si:
                raise ValueError(f"subtask {si}: dependency '{r}' must come from an earlier subtask (found at subtask {prod_si})")

        # Also validate any extract-looking dependencies are upstream-produced.
        for dep in deps:
            if extract_re.fullmatch(dep):
                prod_si = produced_in.get(dep)
                if prod_si is None:
                    raise ValueError(f"subtask {si}: dependency '{dep}' is not produced by any EXTRACT step")
                if prod_si >= si:
                    raise ValueError(
                        f"subtask {si}: dependency '{dep}' must come from an earlier subtask (found at subtask {prod_si})"
                    )

        for li, step in enumerate(steps):
            if step.get("i") != li:
                raise ValueError(f"subtask {si} step {li}: i must equal its index within the subtask")
            at = step.get("action_type")
            if at not in allowed_action_types:
                raise ValueError(f"subtask {si} step {li}: invalid action_type={at!r}")

            av = step.get("action_value")
            vn = step.get("variable_name")
            aa = step.get("additional_args")
            if not isinstance(aa, dict):
                aa = {}
            # Backward-compat: accept legacy top-level extract_query, but treat it as additional_args.extract_query.
            if (
                isinstance(step.get("extract_query"), str)
                and step.get("extract_query", "").strip()
                and "extract_query" not in aa
            ):
                aa["extract_query"] = step.get("extract_query")

            if at in {"TYPE", "KEYPRESS"}:
                # Allow null action_value for TYPE/KEYPRESS (user may want model to infer).
                # If provided, it must be a string (can be empty).
                if av is not None and not isinstance(av, str):
                    raise ValueError(f"subtask {si} step {li}: {at} action_value must be a string or null")
            elif at == "EXTRACT":
                if not isinstance(vn, str) or not extract_re.fullmatch(vn):
                    raise ValueError(f"subtask {si} step {li}: EXTRACT requires variable_name like extract_0")
                if av != vn:
                    raise ValueError(f"subtask {si} step {li}: EXTRACT requires action_value == variable_name")
                eq = aa.get("extract_query")
                if not isinstance(eq, str) or not eq.strip():
                    raise ValueError(f"subtask {si} step {li}: EXTRACT requires non-empty additional_args.extract_query")
                step["additional_args"] = aa
                step.pop("extract_query", None)
            else:
                if av is not None:
                    raise ValueError(f"subtask {si} step {li}: action_value must be null for {at}")
                if "extract_query" in aa:
                    raise ValueError(f"subtask {si} step {li}: additional_args.extract_query must not be set for {at}")
                step["additional_args"] = aa
                step.pop("extract_query", None)
                if vn is not None and at != "EXTRACT":
                    raise ValueError(f"subtask {si} step {li}: variable_name must be null for {at}")

            # Minimal required fields for replay robustness
            if not str(step.get("intent") or "").strip():
                raise ValueError(f"subtask {si} step {li}: missing intent")
            if not str(step.get("expected_current_state") or "").strip():
                raise ValueError(f"subtask {si} step {li}: missing expected_current_state")
            target = step.get("target")
            if not isinstance(target, dict) or not str(target.get("primary") or "").strip():
                raise ValueError(f"subtask {si} step {li}: target.primary required")
            pa = step.get("post_action")
            if not isinstance(pa, list) or not pa:
                raise ValueError(f"subtask {si} step {li}: post_action must be non-empty list[str]")


def reindex_schema(schema: dict) -> None:
    plan = schema.get("plan") or {}
    subtasks = plan.get("subtasks") or []
    if not isinstance(plan, dict) or not isinstance(subtasks, list):
        return
    for si, st in enumerate(subtasks):
        if not isinstance(st, dict):
            continue
        st["subtask_i"] = si
        steps = st.get("steps") or []
        if not isinstance(steps, list):
            st["steps"] = []
            continue
        for li, step in enumerate(steps):
            if isinstance(step, dict):
                step["i"] = li


def strip_details_in_schema(schema: dict) -> None:
    plan = schema.get("plan") or {}
    subtasks = plan.get("subtasks") or []
    if not isinstance(plan, dict) or not isinstance(subtasks, list):
        return
    for st in subtasks:
        if not isinstance(st, dict):
            continue
        steps = st.get("steps") or []
        if not isinstance(steps, list):
            continue
        for step in steps:
            if isinstance(step, dict):
                step.pop("details", None)


def available_upstream_extracts(schema: dict, *, subtask_i: int) -> list[str]:
    plan = schema.get("plan") or {}
    subtasks = plan.get("subtasks") or []
    if not isinstance(plan, dict) or not isinstance(subtasks, list):
        return []
    found: list[str] = []
    seen: set[str] = set()
    for st in subtasks:
        if not isinstance(st, dict):
            continue
        si = st.get("subtask_i")
        if not isinstance(si, int) or si >= subtask_i:
            continue
        steps = st.get("steps") or []
        if not isinstance(steps, list):
            continue
        for step in steps:
            if not isinstance(step, dict):
                continue
            if step.get("action_type") != "EXTRACT":
                continue
            vn = step.get("variable_name")
            if isinstance(vn, str) and vn.strip() and vn not in seen:
                seen.add(vn)
                found.append(vn)
    return found
