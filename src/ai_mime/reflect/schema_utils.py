from __future__ import annotations

import re


def validate_schema(schema: dict) -> None:
    """
    Best-effort validation of the workflow schema format used by this repo.
    Raises ValueError with a readable message on failure.
    """
    if not isinstance(schema, dict):
        raise ValueError("schema must be an object")
    plan = schema.get("plan")
    if not isinstance(plan, dict):
        raise ValueError("schema.plan must be an object")
    subtasks = plan.get("subtasks")
    if not isinstance(subtasks, list):
        raise ValueError("schema.plan.subtasks must be a list")

    allowed_action_types = {"CLICK", "TYPE", "SCROLL", "KEYPRESS", "DRAG", "EXTRACT"}
    extract_re = re.compile(r"^extract_[0-9]+$")

    for si, st in enumerate(subtasks):
        if not isinstance(st, dict):
            raise ValueError(f"subtask {si}: must be an object")
        if st.get("subtask_i") != si:
            raise ValueError(f"subtask {si}: subtask_i must equal its list index")
        if not isinstance(st.get("text"), str) or not st.get("text", "").strip():
            raise ValueError(f"subtask {si}: missing non-empty text")
        deps = st.get("dependencies")
        if deps is None:
            deps = []
            st["dependencies"] = deps
        if not isinstance(deps, list) or not all(isinstance(d, str) and d.strip() for d in deps):
            raise ValueError(f"subtask {si}: dependencies must be a list[str]")

        steps = st.get("steps")
        if not isinstance(steps, list):
            raise ValueError(f"subtask {si}: steps must be a list")

        for li, step in enumerate(steps):
            if not isinstance(step, dict):
                raise ValueError(f"subtask {si} step {li}: must be an object")
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
            if not isinstance(step.get("intent"), str) or not step.get("intent", "").strip():
                raise ValueError(f"subtask {si} step {li}: missing intent")
            if not isinstance(step.get("expected_current_state"), str) or not step.get(
                "expected_current_state", ""
            ).strip():
                raise ValueError(f"subtask {si} step {li}: missing expected_current_state")
            target = step.get("target")
            if not isinstance(target, dict) or not isinstance(target.get("primary"), str) or not target.get(
                "primary", ""
            ).strip():
                raise ValueError(f"subtask {si} step {li}: target.primary required")
            pa = step.get("post_action")
            if not isinstance(pa, list) or not pa or not all(isinstance(x, str) and x.strip() for x in pa):
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
