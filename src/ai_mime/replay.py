from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkflowRef:
    """A discovered workflow directory that is eligible for replay."""

    display_name: str
    workflow_dir: Path


def _safe_read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def iter_workflow_dirs_with_schema(workflows_root: Path) -> Iterable[Path]:
    """Yield workflow dirs under workflows_root that contain schema.json."""
    if not workflows_root.exists() or not workflows_root.is_dir():
        return
    for p in sorted(workflows_root.iterdir(), key=lambda x: x.name):
        if not p.is_dir():
            continue
        if (p / "schema.json").exists():
            yield p


def list_replayable_workflows(workflows_root: Path) -> list[WorkflowRef]:
    """List workflows (display name + path) that contain schema.json."""
    workflows: list[WorkflowRef] = []
    for d in iter_workflow_dirs_with_schema(workflows_root):
        meta = _safe_read_json(d / "metadata.json")
        # Prefer a human name, fall back to folder name.
        name = (meta.get("name") or "").strip() if isinstance(meta, dict) else ""
        display = name or d.name
        workflows.append(WorkflowRef(display_name=display, workflow_dir=d))

    # Ensure display_name uniqueness for UI menu keys.
    seen: dict[str, int] = {}
    uniqued: list[WorkflowRef] = []
    for wf in workflows:
        n = seen.get(wf.display_name, 0)
        seen[wf.display_name] = n + 1
        if n == 0:
            uniqued.append(wf)
        else:
            uniqued.append(
                WorkflowRef(
                    display_name=f"{wf.display_name} ({wf.workflow_dir.name})",
                    workflow_dir=wf.workflow_dir,
                )
            )
    return uniqued


def resolve_workflow(workflows_root: Path, workflow: str | Path) -> WorkflowRef:
    """
    Resolve a workflow reference given either:
    - a full path to a workflow directory
    - a folder name under workflows_root
    """
    workflow_p = Path(workflow)
    if workflow_p.exists():
        workflow_dir = workflow_p
    else:
        workflow_dir = workflows_root / str(workflow)

    if not workflow_dir.exists() or not workflow_dir.is_dir():
        raise FileNotFoundError(f"Workflow dir not found: {workflow_dir}")
    if not (workflow_dir / "schema.json").exists():
        raise FileNotFoundError(f"schema.json not found in workflow dir: {workflow_dir}")

    meta = _safe_read_json(workflow_dir / "metadata.json")
    name = (meta.get("name") or "").strip() if isinstance(meta, dict) else ""
    display = name or workflow_dir.name
    return WorkflowRef(display_name=display, workflow_dir=workflow_dir)


def replay_workflow_dummy(workflow: WorkflowRef | str | Path) -> None:
    """
    Dummy replay entrypoint.

    For now, this just logs which workflow would be replayed.
    """
    if isinstance(workflow, WorkflowRef):
        wf = workflow
    else:
        wf = WorkflowRef(display_name=str(workflow), workflow_dir=Path(workflow))

    logger.info("Replay triggered (dummy): %s (%s)", wf.display_name, wf.workflow_dir)
