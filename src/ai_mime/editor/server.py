from __future__ import annotations

import json
import os
import re
import socket
import sys
from multiprocessing import Process
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from ai_mime.replay.catalog import list_replayable_workflows
from ai_mime.reflect.schema_utils import (
    available_upstream_extracts,
    reindex_schema,
    strip_details_in_schema,
    validate_schema,
)

_SINGLE_BRACE_PARAM_RE = re.compile(r"\{([a-zA-Z0-9_]+)\}")


def _workflows_root_from_env() -> Path:
    raw = (os.getenv("AI_MIME_WORKFLOWS_ROOT") or "").strip()
    if not raw:
        raise RuntimeError("Missing AI_MIME_WORKFLOWS_ROOT")
    p = Path(raw).expanduser()
    return p


def _safe_workflow_dir(workflows_root: Path, workflow_id: str) -> Path:
    # workflow_id is expected to be a folder name under workflows_root.
    if not workflow_id or "/" in workflow_id or "\\" in workflow_id or ".." in workflow_id:
        raise HTTPException(status_code=400, detail="Invalid workflow id")
    p = (workflows_root / workflow_id).resolve()
    root = workflows_root.resolve()
    if root not in p.parents and p != root:
        raise HTTPException(status_code=400, detail="Invalid workflow id")
    if not p.exists() or not p.is_dir():
        raise HTTPException(status_code=404, detail="Workflow not found")
    return p


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_json_atomic(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _apply_deleted_param_examples(*, schema: dict[str, Any], deleted_param_examples: dict[str, str]) -> None:
    """
    If a param is referenced as {param} but is not declared in schema.task_params,
    replace the reference with its last-known example value.

    This lets users delete parameters without leaving broken templates behind.
    """
    declared: set[str] = set()
    task_params = schema.get("task_params") or []
    if isinstance(task_params, list):
        for p in task_params:
            if isinstance(p, dict):
                name = p.get("name")
                if isinstance(name, str) and name.strip():
                    declared.add(name.strip())

    # Only consider substitutions for params that are not declared.
    subs: dict[str, str] = {
        k: str(v) for k, v in (deleted_param_examples or {}).items() if isinstance(k, str) and k.strip() and k not in declared
    }

    def _subst_in_str(s: str, *, ctx: str) -> str:
        needed = set(_SINGLE_BRACE_PARAM_RE.findall(s))
        missing = sorted([k for k in needed if k not in declared and k not in subs])
        if missing:
            raise ValueError(
                f"Template references missing params {missing} in {ctx}. "
                f"Either re-add them to task_params or delete/replace the {{param}} references."
            )

        def _repl(m: re.Match[str]) -> str:
            k = m.group(1)
            if k in subs:
                return subs[k]
            return m.group(0)

        return _SINGLE_BRACE_PARAM_RE.sub(_repl, s)

    # Apply to the common templated fields used by replay (and a few top-level strings).
    if isinstance(schema.get("task_name"), str):
        schema["task_name"] = _subst_in_str(schema["task_name"], ctx="task_name")
    if isinstance(schema.get("detailed_task_description"), str):
        schema["detailed_task_description"] = _subst_in_str(schema["detailed_task_description"], ctx="detailed_task_description")
    if isinstance(schema.get("success_criteria"), str):
        schema["success_criteria"] = _subst_in_str(schema["success_criteria"], ctx="success_criteria")

    plan = schema.get("plan") or {}
    subtasks = plan.get("subtasks") or []
    if isinstance(subtasks, list):
        for si, st in enumerate(subtasks):
            if not isinstance(st, dict):
                continue
            if isinstance(st.get("text"), str):
                st["text"] = _subst_in_str(st["text"], ctx=f"plan.subtasks[{si}].text")
            steps = st.get("steps") or []
            if not isinstance(steps, list):
                continue
            for li, step in enumerate(steps):
                if not isinstance(step, dict):
                    continue
                av = step.get("action_value")
                if isinstance(av, str):
                    step["action_value"] = _subst_in_str(av, ctx=f"plan.subtasks[{si}].steps[{li}].action_value")
                aa = step.get("additional_args")
                if isinstance(aa, dict) and isinstance(aa.get("extract_query"), str):
                    aa["extract_query"] = _subst_in_str(
                        aa["extract_query"], ctx=f"plan.subtasks[{si}].steps[{li}].additional_args.extract_query"
                    )


def create_app() -> FastAPI:
    workflows_root = _workflows_root_from_env()

    app = FastAPI(title="AI Mime Workflow Editor", docs_url=None, redoc_url=None)

    web_dir = Path(__file__).parent / "web"
    app.mount("/static", StaticFiles(directory=str(web_dir)), name="static")

    @app.get("/workflows/{workflow_id}", response_class=HTMLResponse)
    def workflow_editor(workflow_id: str):
        # Validate workflow id early so broken URLs fail fast (and log it).
        _safe_workflow_dir(workflows_root, workflow_id)
        # Serve a single-page app; workflow_id is passed via querystring for simplicity.
        index_path = web_dir / "index.html"
        if not index_path.exists():
            raise HTTPException(status_code=500, detail="Editor UI not found")
        html = index_path.read_text(encoding="utf-8")
        # Minimal inline config
        html = html.replace("__WORKFLOW_ID__", workflow_id)
        return HTMLResponse(content=html)

    @app.get("/api/workflows")
    def api_list_workflows():
        wfs = list_replayable_workflows(workflows_root)
        return {
            "workflows": [
                {"id": wf.workflow_dir.name, "display_name": wf.display_name, "dir": str(wf.workflow_dir)}
                for wf in sorted(wfs, key=lambda w: w.workflow_dir.name, reverse=True)
            ]
        }

    @app.get("/api/workflows/{workflow_id}")
    def api_get_workflow(workflow_id: str):
        wf_dir = _safe_workflow_dir(workflows_root, workflow_id)
        schema_path = wf_dir / "schema.json"
        if not schema_path.exists():
            raise HTTPException(status_code=404, detail="schema.json not found")
        schema = _read_json(schema_path)
        meta = _read_json(wf_dir / "metadata.json")
        return {"workflow_id": workflow_id, "schema": schema, "metadata": meta}

    @app.get("/api/workflows/{workflow_id}/upstream_extracts")
    def api_upstream_extracts(workflow_id: str, subtask_i: int):
        wf_dir = _safe_workflow_dir(workflows_root, workflow_id)
        schema_path = wf_dir / "schema.json"
        schema = _read_json(schema_path)
        if not isinstance(subtask_i, int) or subtask_i < 0:
            raise HTTPException(status_code=400, detail="subtask_i must be >= 0")
        return {"extracts": available_upstream_extracts(schema, subtask_i=subtask_i)}

    @app.post("/api/workflows/{workflow_id}")
    def api_save_workflow(workflow_id: str, payload: dict[str, Any] = Body(...)):
        wf_dir = _safe_workflow_dir(workflows_root, workflow_id)
        schema_path = wf_dir / "schema.json"
        schema = payload.get("schema")
        deleted_param_examples = payload.get("deleted_param_examples") or {}
        if not isinstance(schema, dict):
            raise HTTPException(status_code=400, detail="Body must be {schema: {...}}")
        if not isinstance(deleted_param_examples, dict):
            raise HTTPException(status_code=400, detail="deleted_param_examples must be an object (name -> example string)")
        try:
            _apply_deleted_param_examples(schema=schema, deleted_param_examples=deleted_param_examples)  # may raise ValueError
            reindex_schema(schema)
            strip_details_in_schema(schema)
            validate_schema(schema)
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))
        _write_json_atomic(schema_path, schema)
        return {"ok": True, "schema": schema}

    @app.get("/health")
    def health():
        return {"ok": True}

    return app


def _pick_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    _host, port = s.getsockname()
    s.close()
    return int(port)


def _run_uvicorn(host: str, port: int) -> None:
    # Import inside the subprocess so the caller doesn't require fastapi/uvicorn
    # unless the editor is actually used.
    import uvicorn  # type: ignore[import-not-found]

    app = create_app()
    # Ensure logs go to the parent terminal (stdout/stderr).
    uvicorn.run(app, host=host, port=port, log_level="info", access_log=True)


def start_editor_server(*, workflows_root: Path) -> tuple[Process, int]:
    """
    Start the editor server in a subprocess and return (process, port).
    The server binds to 127.0.0.1 only.
    """
    os.environ["AI_MIME_WORKFLOWS_ROOT"] = str(workflows_root)
    port = _pick_free_port()
    p = Process(target=_run_uvicorn, args=("127.0.0.1", port), daemon=True)
    p.start()
    print(f"[ai-mime] editor server starting on http://127.0.0.1:{port}", file=sys.stderr)
    return p, port
