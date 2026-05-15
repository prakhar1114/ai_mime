from __future__ import annotations

import logging
import traceback
from pathlib import Path
from typing import Any, Callable

from ai_mime.debug_log import log
from ai_mime.reflect.workflow import compile_schema_for_workflow_dir, reflect_session
from ai_mime.user_config import ResolvedReflectConfig


def emit_reflect_event(event_queue: Any | None, obj: dict[str, Any]) -> None:
    if event_queue is None:
        return
    try:
        if hasattr(event_queue, "put_nowait"):
            event_queue.put_nowait(obj)
        else:
            event_queue.put(obj)
    except Exception:
        pass


def run_reflect_and_compile_schema(
    session_dir: str,
    reflect_llm_cfg: ResolvedReflectConfig,
    *,
    workflows_root: str | Path | None = None,
    clean_manifest_tail: bool = False,
    force: bool = False,
    event_queue: Any | None = None,
    log_fn: Callable[[str], None] | None = None,
) -> None:
    """
    Shared reflection worker used by the menubar app and editor dashboard.
    Emits progress events suitable for both coarse status and reflect.html.
    """
    session_dir_p = Path(session_dir)
    session_name = session_dir_p.name
    workflows_root_p = Path(workflows_root) if workflows_root is not None else session_dir_p.parent.parent / "workflows"

    def _log(msg: str, *, exc_info: bool = False) -> None:
        if log_fn is not None:
            try:
                log_fn(msg)
            except Exception:
                pass
        log(msg, exc_info=exc_info)

    def _emit(obj: dict[str, Any]) -> None:
        obj.setdefault("session_name", session_name)
        emit_reflect_event(event_queue, obj)

    try:
        logging.basicConfig(level=logging.INFO)
        _log(f"reflect subprocess started: session={session_name} session_dir={session_dir_p} workflows_root={workflows_root_p}")
        _log(f"reflect_llm_cfg.model={reflect_llm_cfg.model}")
        _log(f"clean_manifest_tail={clean_manifest_tail}")
        _log(f"force={force}")

        _emit({
            "type": "reflect_phase_started",
            "phase": "reflecting",
            "label": "Reflecting",
            "progress": 5,
        })
        workflow_dir = workflows_root_p / session_name
        if (workflow_dir / "schema.json").exists():
            out_dir = workflow_dir
            _log(f"Using existing reflected workflow with schema: {out_dir}")
            print(f"Using existing reflected workflow with schema: {out_dir}")
        else:
            out_dir = reflect_session(session_dir_p, workflows_root_p, clean_manifest_tail=clean_manifest_tail, force=force)
            _log(f"Reflect finished: {out_dir}")
            print(f"Reflect finished: {out_dir}")

        _emit({
            "type": "reflect_phase_started",
            "phase": "compiling",
            "label": "Compiling",
            "progress": 8,
            "workflow_dir": str(out_dir),
        })

        def _progress(event: dict[str, Any]) -> None:
            event.setdefault("type", "reflect_progress")
            event.setdefault("workflow_dir", str(out_dir))
            _emit(event)

        _log("Starting compile_schema_for_workflow_dir...")
        compile_schema_for_workflow_dir(out_dir, llm_cfg=reflect_llm_cfg, progress_callback=_progress)
        _log(f"Schema compiled: {out_dir / 'schema.json'}")
        print(f"Schema compiled: {out_dir / 'schema.json'}")
        _emit({
            "type": "reflect_compile_done",
            "phase": "optimized_plan_complete",
            "label": "Optimized plan",
            "progress": 100,
            "workflow_dir": str(out_dir),
        })
    except Exception as e:
        _log(f"FAILED reflect/compile: {e}", exc_info=True)
        _emit({
            "type": "reflect_compile_failed",
            "error": str(e),
            "traceback": traceback.format_exc(),
            "session_dir": str(session_dir_p),
        })
        raise
