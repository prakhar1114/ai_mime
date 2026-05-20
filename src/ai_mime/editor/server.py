from __future__ import annotations

import json
import os
import queue as thread_queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from multiprocessing import Event, Process, Queue
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ai_mime.replay.catalog import list_replayable_workflows
from ai_mime.replay.engine import ReplayConfig, ReplayStopped, resolve_params, run_plan
from ai_mime.replay.grounding import predict_computer_use_tool_call, tool_call_to_pixel_action
from ai_mime.replay.os_executor import exec_computer_use_action
from ai_mime.reflect.runner import run_reflect_and_compile_schema
from ai_mime.screenshot import ScreenshotRecorder
from ai_mime.user_config import ResolvedLLMConfig, ResolvedReflectConfig
from ai_mime.debug_log import log
from ai_mime.agent_runner import AgentBusyError, WorkflowSkillBuildService, WorkspaceAgentChatService

EDITOR_SERVER_PORT = 58838


def _kill_processes_on_tcp_port(port: int) -> None:
    """Stop any process using this TCP port so a new editor server can bind (macOS/Linux: uses lsof)."""
    try:
        proc = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return
    raw = (proc.stdout or "").strip()
    if not raw:
        return
    ours = os.getpid()
    pids: list[int] = []
    for token in raw.replace("\n", " ").split():
        if token.isdigit():
            pid = int(token)
            if pid != ours:
                pids.append(pid)
    seen: set[int] = set()
    unique: list[int] = []
    for p in pids:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    if not unique:
        return
    print(
        f"[ai-mime] editor server port {port} in use; stopping PIDs {unique}",
        file=sys.stderr,
        flush=True,
    )
    for pid in unique:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                break
            except PermissionError:
                break
    time.sleep(0.15)


def _task_log(msg: str, *, exc_info: bool = False) -> None:
    print(f"[ai-mime dashboard] {msg}", file=sys.stderr, flush=True)
    log(f"Dashboard: {msg}", exc_info=exc_info)


def _workflows_root_from_env() -> Path:
    raw = (os.getenv("AI_MIME_WORKFLOWS_ROOT") or "").strip()
    if not raw:
        raise RuntimeError("Missing AI_MIME_WORKFLOWS_ROOT")
    p = Path(raw).expanduser()
    return p


def _recordings_root_from_env(workflows_root: Path) -> Path:
    raw = (os.getenv("AI_MIME_RECORDINGS_ROOT") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return workflows_root.parent / "recordings"


def _safe_task_id(task_id: str) -> str:
    if not task_id or "/" in task_id or "\\" in task_id or ".." in task_id:
        raise HTTPException(status_code=400, detail="Invalid task id")
    return task_id


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


def _safe_recording_dir(recordings_root: Path, task_id: str) -> Path:
    task_id = _safe_task_id(task_id)
    p = (recordings_root / task_id).resolve()
    root = recordings_root.resolve()
    if root not in p.parents and p != root:
        raise HTTPException(status_code=400, detail="Invalid task id")
    if not p.exists() or not p.is_dir():
        raise HTTPException(status_code=404, detail="Recording not found")
    return p


def _find_skill_dir(workflow_dir: Path) -> Path | None:
    """Return the per-task skill dir if a built skill is present.

    A skill is considered built when workflow_dir/skills/<slug>/run.sh exists
    and is executable.
    There is typically a single subdirectory; if there are multiple, prefer the
    most recently modified.
    """
    skills_root = workflow_dir / "skills"
    if not skills_root.is_dir():
        return None
    candidates: list[Path] = []
    for child in skills_root.iterdir():
        run_sh = child / "run.sh"
        if child.is_dir() and run_sh.is_file() and os.access(run_sh, os.X_OK):
            candidates.append(child)
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def _has_reflected_schema(workflow_dir: Path) -> bool:
    return (workflow_dir / "schema.json").exists()


def _has_optimized_plan(workflow_dir: Path) -> bool:
    return (workflow_dir / "optimized_plan.json").exists()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _sse_event(obj: dict[str, Any]) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def _parse_skill_progress_event(line: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(line)
    except Exception:
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("event"), str):
        return None
    return obj


def _emit(queue: Any | None, obj: dict[str, Any]) -> None:
    if queue is None:
        return
    try:
        if hasattr(queue, "put_nowait"):
            queue.put_nowait(obj)
        else:
            queue.put(obj)
    except Exception:
        pass


def _run_reflect_task(
    session_dir: str,
    workflows_root: str,
    reflect_llm_cfg: ResolvedReflectConfig,
    *,
    force: bool = False,
    event_queue: Any | None = None,
) -> None:
    run_reflect_and_compile_schema(
        session_dir,
        reflect_llm_cfg,
        workflows_root=workflows_root,
        clean_manifest_tail=False,
        force=force,
        event_queue=event_queue,
        log_fn=lambda msg: _task_log(msg),
    )


def _run_replay_task(
    workflow_dir: str,
    replay_llm_cfg: ResolvedLLMConfig,
    *,
    overrides: dict[str, str] | None = None,
    event_queue: Any | None = None,
    stop_event: Any | None = None,
) -> None:
    wf_dir = Path(workflow_dir)
    try:
        schema = json.loads((wf_dir / "schema.json").read_text(encoding="utf-8"))
        params = resolve_params(schema, overrides=overrides or {})
        cfg = ReplayConfig(
            model=replay_llm_cfg.model,
            base_url=replay_llm_cfg.api_base,
            api_key_env=replay_llm_cfg.api_key_env,
            llm_extra_kwargs=dict(replay_llm_cfg.extra_kwargs or {}),
        )
        screenshotter = ScreenshotRecorder()

        def _capture(dst: Path) -> Path:
            dst.parent.mkdir(parents=True, exist_ok=True)
            saved = screenshotter.capture(dst, exclude_window_id=None)
            if not saved:
                raise RuntimeError("Screenshot capture failed (check Screen Recording permission).")
            return Path(saved)

        run_plan(
            wf_dir,
            params=params,
            cfg=cfg,
            predict_tool_call=predict_computer_use_tool_call,
            tool_call_to_pixel_action=tool_call_to_pixel_action,
            capture_screenshot=_capture,
            exec_action=exec_computer_use_action,
            log=print,
            event_queue=event_queue,
            pause_event=None,
            stop_event=stop_event,
        )
    except ReplayStopped:
        _emit(event_queue, {"type": "replay_stopped"})
    except Exception as e:
        _emit(event_queue, {"type": "replay_failed", "error": str(e), "traceback": traceback.format_exc()})
        raise


class TaskRunner:
    def __init__(
        self,
        *,
        workflows_root: Path,
        recordings_root: Path,
        reflect_llm_cfg: ResolvedReflectConfig | None,
        replay_llm_cfg: ResolvedLLMConfig | None,
        app_state: Any | None = None,
    ) -> None:
        self.workflows_root = workflows_root
        self.recordings_root = recordings_root
        self.reflect_llm_cfg = reflect_llm_cfg
        self.replay_llm_cfg = replay_llm_cfg
        self.app_state = app_state
        self._lock = threading.Lock()
        self._states: dict[str, dict[str, Any]] = {}
        self._reflect_processes: dict[str, tuple[Process, Queue]] = {}
        self._replay_processes: dict[str, tuple[Process, Queue, Event]] = {}

    def list_tasks(self) -> list[dict[str, Any]]:
        with self._lock:
            self._refresh_locked()
            task_ids = self._discover_task_ids_locked()
            return [self._task_row_locked(task_id) for task_id in sorted(task_ids, reverse=True)]

    def get_status(self, task_id: str) -> dict[str, Any]:
        task_id = _safe_task_id(task_id)
        with self._lock:
            self._refresh_locked()
            if task_id not in self._discover_task_ids_locked() and task_id not in self._states:
                raise HTTPException(status_code=404, detail="Task not found")
            return self._task_row_locked(task_id)

    def start_reflect(self, task_id: str, *, force: bool = False) -> dict[str, Any]:
        task_id = _safe_task_id(task_id)
        _task_log(f"reflect requested: task_id={task_id} force={force}")
        if self.reflect_llm_cfg is None:
            _task_log(f"reflect rejected: task_id={task_id} reason=missing_config")
            raise HTTPException(status_code=500, detail="Reflect config is unavailable")
        with self._lock:
            self._refresh_locked()
            if task_id in self._reflect_processes:
                _task_log(f"reflect rejected: task_id={task_id} reason=already_running")
                raise HTTPException(status_code=409, detail="Reflection already running")
            if task_id in self._replay_processes:
                _task_log(f"reflect rejected: task_id={task_id} reason=replay_running")
                raise HTTPException(status_code=409, detail="Replay already running")
            recording_dir = (self.recordings_root / task_id).resolve()
            workflow_dir = (self.workflows_root / task_id).resolve()
            self._assert_under_root(recording_dir, self.recordings_root)
            self._assert_under_root(workflow_dir, self.workflows_root)
            has_recording_manifest = (recording_dir / "manifest.jsonl").exists()
            has_workflow_schema = (workflow_dir / "schema.json").exists()
            if not has_recording_manifest and not has_workflow_schema:
                _task_log(
                    f"reflect rejected: task_id={task_id} reason=missing_reflect_input "
                    f"recording_dir={recording_dir} workflow_dir={workflow_dir}"
                )
                raise HTTPException(status_code=400, detail="Recording manifest.jsonl or workflow schema.json not found")
            reflect_input_dir = workflow_dir if has_workflow_schema else recording_dir
            q: Queue = Queue()
            p = Process(
                target=_run_reflect_task,
                args=(str(reflect_input_dir), str(self.workflows_root), self.reflect_llm_cfg),
                kwargs={"force": force, "event_queue": q},
                daemon=True,
            )
            self._states[task_id] = {
                "status": "reflecting",
                "phase": "reflecting",
                "error": None,
                "progress": {"value": 5, "label": "Reflecting", "phase": "reflecting"},
            }
            self._reflect_processes[task_id] = (p, q)
            p.start()
            _task_log(f"reflect process started: task_id={task_id} pid={p.pid} input_dir={reflect_input_dir}")
            return self._task_row_locked(task_id)

    def start_replay(self, task_id: str) -> dict[str, Any]:
        task_id = _safe_task_id(task_id)
        if self.replay_llm_cfg is None:
            raise HTTPException(status_code=500, detail="Replay config is unavailable")
        with self._lock:
            self._refresh_locked()
            if self._replay_processes:
                raise HTTPException(status_code=409, detail="Replay already running")
            if task_id in self._reflect_processes:
                raise HTTPException(status_code=409, detail="Reflection is running")
            workflow_dir = _safe_workflow_dir(self.workflows_root, task_id)
            if not (workflow_dir / "schema.json").exists():
                raise HTTPException(status_code=400, detail="schema.json not found")
            q: Queue = Queue()
            stop_event: Event = Event()
            p = Process(
                target=_run_replay_task,
                args=(str(workflow_dir), self.replay_llm_cfg),
                kwargs={"overrides": {}, "event_queue": q, "stop_event": stop_event},
                daemon=True,
            )
            self._states[task_id] = {"status": "replaying", "phase": "replaying", "error": None}
            self._replay_processes[task_id] = (p, q, stop_event)
            p.start()
            return self._task_row_locked(task_id)

    def delete_task(self, task_id: str) -> dict[str, Any]:
        task_id = _safe_task_id(task_id)
        with self._lock:
            self._refresh_locked()
            if task_id in self._reflect_processes:
                raise HTTPException(status_code=409, detail="Cannot delete while reflection is running")
            if task_id in self._replay_processes:
                raise HTTPException(status_code=409, detail="Cannot delete while replay is running")
            workflow_dir = (self.workflows_root / task_id).resolve()
            recording_dir = (self.recordings_root / task_id).resolve()
            self._assert_under_root(workflow_dir, self.workflows_root)
            self._assert_under_root(recording_dir, self.recordings_root)
            existed = False
            self._states[task_id] = {"status": "deleting", "phase": "deleting", "error": None}
            for path in (workflow_dir, recording_dir):
                if path.exists():
                    existed = True
                    if not path.is_dir():
                        raise HTTPException(status_code=400, detail=f"Refusing to delete non-directory: {path}")
                    shutil.rmtree(path)
            self._states.pop(task_id, None)
            if not existed:
                raise HTTPException(status_code=404, detail="Task not found")
            return {"ok": True, "task_id": task_id}

    def _discover_task_ids_locked(self) -> set[str]:
        task_ids: set[str] = set()
        for root in (self.workflows_root, self.recordings_root):
            if not root.exists() or not root.is_dir():
                continue
            for p in root.iterdir():
                if p.is_dir() and p.name != ".agent":
                    task_ids.add(p.name)
        task_ids.update(self._states.keys())
        for task_id in self._external_reflecting_locked():
            task_ids.add(task_id)
        return task_ids

    def _task_row_locked(self, task_id: str) -> dict[str, Any]:
        workflow_dir = self.workflows_root / task_id
        recording_dir = self.recordings_root / task_id
        has_workflow = workflow_dir.exists() and workflow_dir.is_dir()
        has_recording = recording_dir.exists() and recording_dir.is_dir()
        has_recording_manifest = has_recording and (recording_dir / "manifest.jsonl").exists()
        has_schema = has_workflow and _has_reflected_schema(workflow_dir)
        has_optimized_plan = has_workflow and _has_optimized_plan(workflow_dir)
        meta = _read_json(workflow_dir / "metadata.json") if has_workflow else _read_json(recording_dir / "metadata.json")
        display_name = str(meta.get("name") or task_id).strip() if isinstance(meta, dict) else task_id
        state = dict(self._states.get(task_id) or {})
        external_reflecting = self._external_reflecting_locked()
        if task_id in external_reflecting and task_id not in self._reflect_processes:
            phase = str(external_reflecting.get(task_id) or "reflecting")
            status = "reflecting" if phase == "reflecting" else "compiling"
            state = {
                "status": status,
                "phase": phase,
                "error": None,
                "progress": self._progress_from_phase(phase),
            }
        status = str(state.get("status") or "")
        if not status or status in {"ready", "pending_reflection"}:
            status = "ready" if (has_schema or has_optimized_plan) else "pending_reflection"
        if status == "reflecting" and state.get("phase") == "compiling":
            status = "compiling"
        active = status in {"reflecting", "compiling", "replaying", "deleting"}
        skill_dir = _find_skill_dir(workflow_dir) if has_workflow else None
        has_skill = skill_dir is not None
        can_reflect = bool((has_recording_manifest or has_schema) and not active)
        can_replay = bool(has_skill and not active and not self._replay_processes)
        return {
            "id": task_id,
            "display_name": display_name,
            "status": status,
            "phase": state.get("phase") or status,
            "error": state.get("error"),
            "progress": state.get("progress") or self._progress_from_status(status, state.get("phase")),
            "has_recording": has_recording,
            "has_workflow": has_workflow,
            "has_schema": has_schema,
            "has_optimized_plan": has_optimized_plan,
            "has_skill": has_skill,
            "skill_dir": str(skill_dir) if skill_dir else None,
            "can_reflect": can_reflect,
            "can_replay": can_replay,
            "can_delete": bool((has_recording or has_workflow) and not active),
            "workflow_dir": str(workflow_dir) if has_workflow else None,
            "recording_dir": str(recording_dir) if has_recording else None,
        }

    def app_status(self) -> dict[str, Any]:
        state = self._read_app_state()
        recording = state.get("recording") if isinstance(state.get("recording"), dict) else {}
        return {
            "is_recording": bool(recording.get("is_recording")),
            "recording_session": recording.get("session_name"),
            "recording_requested": bool(recording.get("requested")),
            "reflecting": self._external_reflecting_locked(),
        }

    def _read_app_state(self) -> dict[str, Any]:
        if self.app_state is None:
            return {}
        try:
            return dict(self.app_state)
        except Exception:
            return {}

    def _external_reflecting_locked(self) -> dict[str, str]:
        state = self._read_app_state()
        reflecting = state.get("reflecting")
        if not isinstance(reflecting, dict):
            return {}
        out: dict[str, str] = {}
        for key, value in reflecting.items():
            if isinstance(key, str) and key:
                out[key] = str(value or "reflecting")
        return out

    def _refresh_locked(self) -> None:
        for task_id, (proc, queue) in list(self._reflect_processes.items()):
            self._drain_reflect_events_locked(task_id, queue)
            if not proc.is_alive():
                self._drain_reflect_events_locked(task_id, queue)
                _proc, _queue = self._reflect_processes.pop(task_id)
                _proc.join(timeout=0.1)
                state = self._states.get(task_id) or {}
                if state.get("status") in {"reflecting", "compiling"}:
                    if proc.exitcode == 0 and (self.workflows_root / task_id / "schema.json").exists():
                        self._states[task_id] = {"status": "ready", "phase": "ready", "error": None}
                        _task_log(f"reflect process complete: task_id={task_id} pid={proc.pid}")
                    else:
                        self._states[task_id] = {
                            "status": "failed_reflection",
                            "phase": "failed_reflection",
                            "error": state.get("error") or f"Reflection exited with code {proc.exitcode}",
                        }
                        _task_log(f"reflect process failed: task_id={task_id} pid={proc.pid} exitcode={proc.exitcode} error={self._states[task_id].get('error')}")

        for task_id, (proc, queue, _stop_event) in list(self._replay_processes.items()):
            self._drain_replay_events_locked(task_id, queue)
            if not proc.is_alive():
                self._drain_replay_events_locked(task_id, queue)
                _proc, _queue, _event = self._replay_processes.pop(task_id)
                _proc.join(timeout=0.1)
                state = self._states.get(task_id) or {}
                if state.get("status") == "replaying":
                    if proc.exitcode == 0:
                        self._states[task_id] = {"status": "ready", "phase": "ready", "error": None}
                    else:
                        self._states[task_id] = {
                            "status": "replay_failed",
                            "phase": "replay_failed",
                            "error": state.get("error") or f"Replay exited with code {proc.exitcode}",
                        }

    def _drain_reflect_events_locked(self, task_id: str, queue: Queue) -> None:
        while True:
            try:
                evt = queue.get_nowait()
            except Exception:
                break
            if not isinstance(evt, dict):
                continue
            et = evt.get("type")
            if et == "reflect_phase_started":
                phase = str(evt.get("phase") or "reflecting")
                self._states[task_id] = {
                    "status": phase,
                    "phase": phase,
                    "error": None,
                    "progress": self._progress_from_event(evt, fallback_phase=phase),
                }
                _task_log(f"reflect event: task_id={task_id} phase={phase}")
            elif et == "reflect_progress":
                phase = str(evt.get("phase") or "compiling")
                status = "reflecting" if phase == "reflecting" else "compiling"
                self._states[task_id] = {
                    "status": status,
                    "phase": phase,
                    "error": None,
                    "progress": self._progress_from_event(evt, fallback_phase=phase),
                }
                _task_log(f"reflect progress: task_id={task_id} phase={phase} progress={self._states[task_id]['progress'].get('value')}")
            elif et == "reflect_compile_done":
                self._states[task_id] = {
                    "status": "ready",
                    "phase": "optimized_plan_complete",
                    "error": None,
                    "progress": {"value": 100, "label": "Optimized plan", "phase": "optimized_plan_complete"},
                }
                _task_log(f"reflect event: task_id={task_id} done")
            elif et == "reflect_compile_failed":
                existing = self._states.get(task_id) or {}
                self._states[task_id] = {
                    "status": "failed_reflection",
                    "phase": "failed_reflection",
                    "error": str(evt.get("error") or "Reflection failed"),
                    "progress": existing.get("progress") or self._progress_from_phase("failed_reflection"),
                }
                _task_log(f"reflect event: task_id={task_id} failed error={self._states[task_id].get('error')}")

    def _drain_replay_events_locked(self, task_id: str, queue: Queue) -> None:
        while True:
            try:
                evt = queue.get_nowait()
            except Exception:
                break
            if not isinstance(evt, dict):
                continue
            et = evt.get("type")
            if et in {"replay_started", "subtask_started", "predicted_tool_call", "pixel_action", "extract_result", "done"}:
                state = self._states.get(task_id) or {}
                state.update({"status": "replaying", "phase": "replaying", "error": None, "last_event": evt})
                self._states[task_id] = state
            elif et == "replay_finished":
                self._states[task_id] = {"status": "ready", "phase": "ready", "error": None}
            elif et == "replay_stopped":
                self._states[task_id] = {"status": "ready", "phase": "stopped", "error": None}
            elif et == "replay_failed":
                self._states[task_id] = {
                    "status": "replay_failed",
                    "phase": "replay_failed",
                    "error": str(evt.get("error") or "Replay failed"),
                }

    @staticmethod
    def _assert_under_root(path: Path, root: Path) -> None:
        root_r = root.resolve()
        if root_r not in path.parents and path != root_r:
            raise HTTPException(status_code=400, detail="Invalid task path")

    @staticmethod
    def _progress_from_event(evt: dict[str, Any], *, fallback_phase: str) -> dict[str, Any]:
        value = evt.get("progress")
        try:
            value_i = int(value)
        except Exception:
            value_i = TaskRunner._progress_from_phase(fallback_phase)["value"]
        value_i = max(0, min(100, value_i))
        label = evt.get("label")
        if not isinstance(label, str) or not label.strip():
            label = TaskRunner._progress_from_phase(fallback_phase)["label"]
        phase = evt.get("phase")
        return {"value": value_i, "label": label, "phase": str(phase or fallback_phase)}

    @staticmethod
    def _progress_from_phase(phase: str) -> dict[str, Any]:
        mapping: dict[str, tuple[int, str]] = {
            "reflecting": (5, "Reflecting"),
            "compiling": (8, "Compiling"),
            "pass_a_started": (10, "Pass A"),
            "pass_a_complete": (33, "Pass A"),
            "pass_b_started": (45, "Pass B"),
            "pass_b_complete": (66, "Pass B"),
            "optimized_plan_started": (82, "Optimized plan"),
            "optimized_plan_complete": (100, "Optimized plan"),
            "ready": (100, "Optimized plan"),
            "failed_reflection": (0, "Reflection failed"),
        }
        value, label = mapping.get(phase, (0, str(phase or "Pending")))
        return {"value": value, "label": label, "phase": phase}

    @staticmethod
    def _progress_from_status(status: str, phase: Any) -> dict[str, Any]:
        if isinstance(phase, str) and phase:
            return TaskRunner._progress_from_phase(phase)
        return TaskRunner._progress_from_phase(status)


def create_app(
    *,
    workflows_root: Path | None = None,
    recordings_root: Path | None = None,
    reflect_llm_cfg: ResolvedReflectConfig | None = None,
    replay_llm_cfg: ResolvedLLMConfig | None = None,
    app_command_queue: Any | None = None,
    app_state: Any | None = None,
    agent_chat_service: WorkspaceAgentChatService | None = None,
) -> FastAPI:
    workflows_root = workflows_root or _workflows_root_from_env()
    recordings_root = recordings_root or _recordings_root_from_env(workflows_root)
    task_runner = TaskRunner(
        workflows_root=workflows_root,
        recordings_root=recordings_root,
        reflect_llm_cfg=reflect_llm_cfg,
        replay_llm_cfg=replay_llm_cfg,
        app_state=app_state,
    )
    agent_service = agent_chat_service or WorkspaceAgentChatService()
    task_agent_services: dict[str, WorkspaceAgentChatService] = {}
    replay_agent_services: dict[str, WorkspaceAgentChatService] = {}
    skill_build_services: dict[str, WorkflowSkillBuildService] = {}

    app = FastAPI(title="AI Mime Task Dashboard", docs_url=None, redoc_url=None)

    web_dir = Path(__file__).parent / "web"
    app.mount("/static", StaticFiles(directory=str(web_dir)), name="static")

    @app.get("/", response_class=HTMLResponse)
    def root():
        return tasks_dashboard()

    @app.get("/tasks", response_class=HTMLResponse)
    def tasks_dashboard():
        index_path = web_dir / "tasks.html"
        if not index_path.exists():
            raise HTTPException(status_code=500, detail="Task dashboard UI not found")
        return HTMLResponse(content=index_path.read_text(encoding="utf-8"))

    @app.get("/agent", response_class=HTMLResponse)
    def agent_dashboard():
        index_path = web_dir / "agent.html"
        if not index_path.exists():
            raise HTTPException(status_code=500, detail="Agent UI not found")
        return HTMLResponse(content=index_path.read_text(encoding="utf-8"))

    @app.get("/reflect/{task_id}", response_class=HTMLResponse)
    def reflect_dashboard(task_id: str):
        _safe_task_id(task_id)
        task_runner.get_status(task_id)
        index_path = web_dir / "reflect.html"
        if not index_path.exists():
            raise HTTPException(status_code=500, detail="Reflect UI not found")
        html = index_path.read_text(encoding="utf-8").replace("__TASK_ID__", task_id)
        return HTMLResponse(content=html)

    def _validate_agent_session_id(session_id: str) -> None:
        if not session_id or "/" in session_id or "\\" in session_id or ".." in session_id:
            raise HTTPException(status_code=400, detail="Invalid session id")

    def _task_agent_service(task_id: str) -> WorkspaceAgentChatService:
        row = task_runner.get_status(task_id)
        workspace_raw = row.get("workflow_dir") or row.get("recording_dir")
        if not isinstance(workspace_raw, str) or not workspace_raw:
            raise HTTPException(status_code=404, detail="Task workspace not found")
        workspace = Path(workspace_raw)
        existing = task_agent_services.get(task_id)
        if existing is not None and existing.workspace_dir == workspace:
            return existing
        service = WorkspaceAgentChatService(workspace_dir=workspace)
        task_agent_services[task_id] = service
        return service

    def _replay_agent_service(task_id: str) -> WorkspaceAgentChatService:
        row = task_runner.get_status(task_id)
        workspace_raw = row.get("workflow_dir")
        skill_dir_raw = row.get("skill_dir")
        if not isinstance(workspace_raw, str) or not workspace_raw:
            raise HTTPException(status_code=404, detail="Workflow directory not found for task")
        if not isinstance(skill_dir_raw, str) or not skill_dir_raw:
            raise HTTPException(status_code=404, detail="Skill is not built for this task yet")
        workspace = Path(workspace_raw)
        existing = replay_agent_services.get(task_id)
        if existing is not None and existing.workspace_dir == workspace:
            return existing
        service = WorkspaceAgentChatService(
            workspace_dir=workspace,
            mode="replay_execution",
            agent_dir=workspace / "agent" / "replay",
        )
        replay_agent_services[task_id] = service
        return service

    async def _agent_chat_stream_response(
        service: WorkspaceAgentChatService,
        payload: dict[str, Any],
    ) -> StreamingResponse:
        message = payload.get("message")
        session_id = payload.get("session_id")
        model = payload.get("model")
        if not isinstance(message, str) or not message.strip():
            raise HTTPException(status_code=400, detail="message must be non-empty")
        if session_id is not None and not isinstance(session_id, str):
            raise HTTPException(status_code=400, detail="session_id must be a string or null")
        if model is not None and not isinstance(model, str):
            raise HTTPException(status_code=400, detail="model must be a string or null")

        try:
            event_iter = service.chat_stream(message=message, session_id=session_id, model=model)
        except AgentBusyError as e:
            raise HTTPException(status_code=409, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        async def _sse():
            try:
                async for event in event_iter:
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'event': 'error', 'message': str(e)})}\n\n"

        return StreamingResponse(
            _sse(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    def _agent_chat_response(service: WorkspaceAgentChatService, payload: dict[str, Any]) -> dict[str, Any]:
        message = payload.get("message")
        session_id = payload.get("session_id")
        if not isinstance(message, str) or not message.strip():
            raise HTTPException(status_code=400, detail="message must be non-empty")
        if session_id is not None and not isinstance(session_id, str):
            raise HTTPException(status_code=400, detail="session_id must be a string or null")
        model = payload.get("model")
        if model is not None and not isinstance(model, str):
            raise HTTPException(status_code=400, detail="model must be a string or null")
        try:
            return service.chat(message=message, session_id=session_id, model=model)
        except AgentBusyError as e:
            raise HTTPException(status_code=409, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/tasks")
    def api_list_tasks():
        return {"tasks": task_runner.list_tasks(), "app": task_runner.app_status()}

    @app.get("/api/app/status")
    def api_app_status():
        return task_runner.app_status()

    @app.get("/api/agent/sessions")
    def api_agent_sessions():
        return agent_service.status()

    @app.get("/api/agent/models")
    def api_agent_models():
        return agent_service.list_models()

    @app.post("/api/agent/sessions")
    def api_agent_create_session():
        return agent_service.create_session()

    @app.get("/api/agent/sessions/{session_id}/messages")
    def api_agent_session_messages(session_id: str):
        _validate_agent_session_id(session_id)
        try:
            return {"session_id": session_id, "messages": agent_service.load_messages(session_id)}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/agent/chat/stream")
    async def api_agent_chat_stream(payload: dict[str, Any] = Body(...)):
        return await _agent_chat_stream_response(agent_service, payload)

    @app.post("/api/agent/interrupt")
    def api_agent_interrupt():
        return {"interrupted": agent_service.interrupt()}

    @app.post("/api/agent/permission")
    def api_agent_permission(payload: dict[str, Any] = Body(...)):
        request_id = payload.get("request_id")
        decision = payload.get("decision")
        if not isinstance(request_id, str) or not request_id:
            raise HTTPException(status_code=400, detail="request_id must be a non-empty string")
        if decision not in ("allow", "allow_always", "deny"):
            raise HTTPException(status_code=400, detail="decision must be allow, allow_always, or deny")
        return {"resolved": agent_service.resolve_permission(request_id, decision)}

    @app.post("/api/agent/settings/bash_requires_approval")
    def api_agent_set_bash_requires_approval(payload: dict[str, Any] = Body(...)):
        value = payload.get("value")
        if not isinstance(value, bool):
            raise HTTPException(status_code=400, detail="value must be boolean")
        return {"bash_requires_approval": agent_service.set_bash_requires_approval(value)}

    @app.post("/api/agent/chat")
    def api_agent_chat(payload: dict[str, Any] = Body(...)):
        return _agent_chat_response(agent_service, payload)

    @app.post("/api/recording/start")
    def api_start_recording():
        if app_command_queue is None:
            raise HTTPException(status_code=503, detail="Recording control is unavailable")
        status = task_runner.app_status()
        if status.get("is_recording") or status.get("recording_requested"):
            return {"ok": True, "queued": False, "message": "Recording already active or queued"}
        try:
            if task_runner.app_state is not None:
                state = task_runner._read_app_state()
                recording = state.get("recording") if isinstance(state.get("recording"), dict) else {}
                recording = dict(recording)
                recording["requested"] = True
                task_runner.app_state["recording"] = recording
            app_command_queue.put({"type": "start_recording"})
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to queue recording start: {e}")
        return {"ok": True, "queued": True}

    @app.get("/api/tasks/{task_id}/status")
    def api_task_status(task_id: str):
        return task_runner.get_status(task_id)

    @app.get("/api/tasks/{task_id}/reflect/status")
    def api_task_reflect_status(task_id: str):
        return task_runner.get_status(task_id)

    @app.get("/api/tasks/{task_id}/agent/sessions")
    def api_task_agent_sessions(task_id: str):
        return _task_agent_service(task_id).status()

    @app.get("/api/tasks/{task_id}/agent/models")
    def api_task_agent_models(task_id: str):
        return _task_agent_service(task_id).list_models()

    @app.post("/api/tasks/{task_id}/agent/sessions")
    def api_task_agent_create_session(task_id: str):
        return _task_agent_service(task_id).create_session()

    @app.get("/api/tasks/{task_id}/agent/sessions/{session_id}/messages")
    def api_task_agent_session_messages(task_id: str, session_id: str):
        _validate_agent_session_id(session_id)
        try:
            return {"session_id": session_id, "messages": _task_agent_service(task_id).load_messages(session_id)}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/tasks/{task_id}/agent/chat/stream")
    async def api_task_agent_chat_stream(task_id: str, payload: dict[str, Any] = Body(...)):
        return await _agent_chat_stream_response(_task_agent_service(task_id), payload)

    @app.post("/api/tasks/{task_id}/agent/interrupt")
    def api_task_agent_interrupt(task_id: str):
        return {"interrupted": _task_agent_service(task_id).interrupt()}

    @app.post("/api/tasks/{task_id}/agent/permission")
    def api_task_agent_permission(task_id: str, payload: dict[str, Any] = Body(...)):
        request_id = payload.get("request_id")
        decision = payload.get("decision")
        if not isinstance(request_id, str) or not request_id:
            raise HTTPException(status_code=400, detail="request_id must be a non-empty string")
        if decision not in ("allow", "allow_always", "deny"):
            raise HTTPException(status_code=400, detail="decision must be allow, allow_always, or deny")
        return {"resolved": _task_agent_service(task_id).resolve_permission(request_id, decision)}

    @app.post("/api/tasks/{task_id}/agent/settings/bash_requires_approval")
    def api_task_agent_set_bash_requires_approval(task_id: str, payload: dict[str, Any] = Body(...)):
        value = payload.get("value")
        if not isinstance(value, bool):
            raise HTTPException(status_code=400, detail="value must be boolean")
        return {"bash_requires_approval": _task_agent_service(task_id).set_bash_requires_approval(value)}

    @app.post("/api/tasks/{task_id}/agent/chat")
    def api_task_agent_chat(task_id: str, payload: dict[str, Any] = Body(...)):
        return _agent_chat_response(_task_agent_service(task_id), payload)

    @app.get("/api/tasks/{task_id}/replay-agent/sessions")
    def api_replay_agent_sessions(task_id: str):
        return _replay_agent_service(task_id).status()

    @app.get("/api/tasks/{task_id}/replay-agent/models")
    def api_replay_agent_models(task_id: str):
        return _replay_agent_service(task_id).list_models()

    @app.post("/api/tasks/{task_id}/replay-agent/sessions")
    def api_replay_agent_create_session(task_id: str):
        return _replay_agent_service(task_id).create_session()

    @app.get("/api/tasks/{task_id}/replay-agent/sessions/{session_id}/messages")
    def api_replay_agent_session_messages(task_id: str, session_id: str):
        _validate_agent_session_id(session_id)
        try:
            return {"session_id": session_id, "messages": _replay_agent_service(task_id).load_messages(session_id)}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/tasks/{task_id}/replay-agent/chat/stream")
    async def api_replay_agent_chat_stream(task_id: str, payload: dict[str, Any] = Body(...)):
        return await _agent_chat_stream_response(_replay_agent_service(task_id), payload)

    @app.post("/api/tasks/{task_id}/replay-agent/interrupt")
    def api_replay_agent_interrupt(task_id: str):
        return {"interrupted": _replay_agent_service(task_id).interrupt()}

    @app.post("/api/tasks/{task_id}/replay-agent/permission")
    def api_replay_agent_permission(task_id: str, payload: dict[str, Any] = Body(...)):
        request_id = payload.get("request_id")
        decision = payload.get("decision")
        if not isinstance(request_id, str) or not request_id:
            raise HTTPException(status_code=400, detail="request_id must be a non-empty string")
        if decision not in ("allow", "allow_always", "deny"):
            raise HTTPException(status_code=400, detail="decision must be allow, allow_always, or deny")
        return {"resolved": _replay_agent_service(task_id).resolve_permission(request_id, decision)}

    @app.post("/api/tasks/{task_id}/replay-agent/settings/bash_requires_approval")
    def api_replay_agent_set_bash_requires_approval(task_id: str, payload: dict[str, Any] = Body(...)):
        value = payload.get("value")
        if not isinstance(value, bool):
            raise HTTPException(status_code=400, detail="value must be boolean")
        return {"bash_requires_approval": _replay_agent_service(task_id).set_bash_requires_approval(value)}

    def _skill_build_service(task_id: str) -> WorkflowSkillBuildService:
        row = task_runner.get_status(task_id)
        workspace_raw = row.get("workflow_dir")
        if not isinstance(workspace_raw, str) or not workspace_raw:
            raise HTTPException(status_code=404, detail="Workflow directory not found for task")
        workflow_dir = Path(workspace_raw)
        if not (workflow_dir / "optimized_plan.json").exists():
            raise HTTPException(
                status_code=409,
                detail="optimized_plan.json not present yet; finish reflect first",
            )
        existing = skill_build_services.get(task_id)
        if existing is not None and existing.workflow_dir == workflow_dir:
            return existing
        service = WorkflowSkillBuildService(workflow_dir=workflow_dir)
        skill_build_services[task_id] = service
        return service

    async def _skill_build_stream_response(
        service: WorkflowSkillBuildService,
        payload: dict[str, Any],
    ) -> StreamingResponse:
        message = payload.get("message")
        session_id = payload.get("session_id")
        model = payload.get("model")
        if not isinstance(message, str) or not message.strip():
            raise HTTPException(status_code=400, detail="message must be non-empty")
        if session_id is not None and not isinstance(session_id, str):
            raise HTTPException(status_code=400, detail="session_id must be a string or null")
        if model is not None and not isinstance(model, str):
            raise HTTPException(status_code=400, detail="model must be a string or null")
        try:
            event_iter = service.chat_stream(message=message, session_id=session_id, model=model)
        except AgentBusyError as e:
            raise HTTPException(status_code=409, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        async def _sse():
            try:
                async for event in event_iter:
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'event': 'error', 'message': str(e)})}\n\n"

        return StreamingResponse(
            _sse(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/skill-build/{task_id}", response_class=HTMLResponse)
    def skill_build_page(task_id: str):
        _safe_task_id(task_id)
        row = task_runner.get_status(task_id)
        workflow_dir_raw = row.get("workflow_dir")
        workflow_dir = Path(workflow_dir_raw) if isinstance(workflow_dir_raw, str) and workflow_dir_raw else None
        if workflow_dir is None or not _has_optimized_plan(workflow_dir):
            index_path = web_dir / "reflect.html"
            if not index_path.exists():
                raise HTTPException(status_code=500, detail="Reflect UI not found")
            html = index_path.read_text(encoding="utf-8").replace("__TASK_ID__", task_id)
            return HTMLResponse(content=html)
        index_path = web_dir / "skill_build.html"
        if not index_path.exists():
            raise HTTPException(status_code=500, detail="Skill build UI not found")
        html = index_path.read_text(encoding="utf-8").replace("__TASK_ID__", task_id)
        return HTMLResponse(content=html)

    @app.get("/api/tasks/{task_id}/skill/inputs-template")
    def api_skill_inputs_template(task_id: str):
        _safe_task_id(task_id)
        row = task_runner.get_status(task_id)
        skill_dir_raw = row.get("skill_dir")
        if not isinstance(skill_dir_raw, str) or not skill_dir_raw:
            raise HTTPException(status_code=404, detail="Skill is not built for this task yet")
        skill_dir = Path(skill_dir_raw)
        template_path = skill_dir / "inputs" / "inputs.template.json"
        if not template_path.exists():
            raise HTTPException(status_code=404, detail=f"inputs.template.json not found at {template_path}")
        try:
            raw = template_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to parse inputs.template.json: {e}")
        if not isinstance(data, dict):
            raise HTTPException(status_code=500, detail="inputs.template.json must be a JSON object")
        return {
            "skill_dir": str(skill_dir),
            "template_path": str(template_path),
            "template": data,
        }

    @app.post("/api/tasks/{task_id}/skill/run/stream")
    def api_skill_run_stream(task_id: str, payload: dict[str, Any] | None = Body(default=None)):
        _safe_task_id(task_id)
        row = task_runner.get_status(task_id)
        workflow_dir_raw = row.get("workflow_dir")
        skill_dir_raw = row.get("skill_dir")
        if not isinstance(workflow_dir_raw, str) or not workflow_dir_raw:
            raise HTTPException(status_code=404, detail="Workflow directory not found for task")
        if not isinstance(skill_dir_raw, str) or not skill_dir_raw:
            raise HTTPException(status_code=404, detail="Skill is not built for this task yet")

        workflow_dir = Path(workflow_dir_raw).resolve()
        skill_dir = Path(skill_dir_raw).resolve()
        _safe_workflow_dir(task_runner.workflows_root, task_id)
        if workflow_dir not in skill_dir.parents:
            raise HTTPException(status_code=400, detail="Invalid skill directory")
        run_sh = skill_dir / "run.sh"
        if not run_sh.exists():
            raise HTTPException(status_code=404, detail=f"run.sh not found at {run_sh}")
        if not os.access(run_sh, os.X_OK):
            raise HTTPException(status_code=400, detail=f"run.sh is not executable: {run_sh}")

        params = payload.get("params") if isinstance(payload, dict) else None
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise HTTPException(status_code=400, detail="params must be a JSON object")

        def _stream():
            started = time.monotonic()
            stdout_lines: list[str] = []
            stderr_lines: list[str] = []
            final_outputs: dict[str, Any] = {}
            q: thread_queue.Queue[tuple[str, str | int | None]] = thread_queue.Queue()
            proc: subprocess.Popen[str] | None = None

            def _reader(pipe: Any, source: str) -> None:
                try:
                    for raw in iter(pipe.readline, ""):
                        q.put((source, raw.rstrip("\n")))
                finally:
                    try:
                        pipe.close()
                    except Exception:
                        pass
                    q.put((f"{source}_done", None))

            with tempfile.TemporaryDirectory(prefix="ai-mime-skill-run-") as td:
                inputs_path = Path(td) / "inputs.json"
                inputs_path.write_text(json.dumps(params, indent=2, ensure_ascii=False), encoding="utf-8")
                cmd = [str(run_sh), str(inputs_path)]
                yield _sse_event({
                    "event": "started",
                    "skill_dir": str(skill_dir),
                    "inputs_path": str(inputs_path),
                    "command": "./run.sh",
                })
                try:
                    proc = subprocess.Popen(
                        cmd,
                        cwd=str(skill_dir),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        bufsize=1,
                    )
                except Exception as e:
                    yield _sse_event({"event": "error", "message": f"Failed to start run.sh: {e}"})
                    return

                assert proc.stdout is not None
                assert proc.stderr is not None
                threading.Thread(target=_reader, args=(proc.stdout, "stdout"), daemon=True).start()
                threading.Thread(target=_reader, args=(proc.stderr, "stderr"), daemon=True).start()

                done_streams: set[str] = set()
                try:
                    while len(done_streams) < 2:
                        try:
                            source, value = q.get(timeout=0.1)
                        except thread_queue.Empty:
                            if proc.poll() is not None and len(done_streams) >= 2:
                                break
                            continue
                        if source.endswith("_done"):
                            done_streams.add(source.removesuffix("_done"))
                            continue
                        line = "" if value is None else str(value)
                        if source == "stdout":
                            stdout_lines.append(line)
                        else:
                            stderr_lines.append(line)
                        yield _sse_event({"event": source, "line": line})

                        progress = _parse_skill_progress_event(line)
                        if progress is None:
                            continue
                        outputs = progress.get("outputs")
                        if isinstance(outputs, dict):
                            source_event = str(progress.get("event") or "output")
                            if source_event == "workflow_done":
                                final_outputs = dict(outputs)
                                key = "workflow_done"
                            else:
                                key = str(progress.get("id") or source_event)
                            yield _sse_event({
                                "event": "output",
                                "key": key,
                                "value": outputs,
                                "source_event": source_event,
                            })
                    exit_code = proc.wait()
                except GeneratorExit:
                    if proc is not None and proc.poll() is None:
                        proc.terminate()
                    raise
                finally:
                    if proc is not None and proc.poll() is None:
                        proc.terminate()

                duration_ms = int((time.monotonic() - started) * 1000)
                yield _sse_event({
                    "event": "done",
                    "success": exit_code == 0,
                    "exit_code": exit_code,
                    "duration_ms": duration_ms,
                    "outputs": final_outputs,
                    "stdout_log": "\n".join(stdout_lines),
                    "stderr_log": "\n".join(stderr_lines),
                })

        return StreamingResponse(
            _stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/replay/{task_id}", response_class=HTMLResponse)
    def replay_page(task_id: str):
        _safe_task_id(task_id)
        row = task_runner.get_status(task_id)
        if not row.get("has_skill"):
            raise HTTPException(status_code=409, detail="Skill is not built for this task yet")
        index_path = web_dir / "replay.html"
        if not index_path.exists():
            raise HTTPException(status_code=500, detail="Replay UI not found")
        html = index_path.read_text(encoding="utf-8").replace("__TASK_ID__", task_id)
        return HTMLResponse(content=html)

    @app.get("/api/tasks/{task_id}/skill-build/sessions")
    def api_skill_build_sessions(task_id: str):
        return _skill_build_service(task_id).status()

    @app.get("/api/tasks/{task_id}/skill-build/models")
    def api_skill_build_models(task_id: str):
        return _skill_build_service(task_id).list_models()

    @app.post("/api/tasks/{task_id}/skill-build/sessions")
    def api_skill_build_create_session(task_id: str):
        return _skill_build_service(task_id).create_session()

    @app.get("/api/tasks/{task_id}/skill-build/sessions/{session_id}/messages")
    def api_skill_build_session_messages(task_id: str, session_id: str):
        _validate_agent_session_id(session_id)
        try:
            return {
                "session_id": session_id,
                "messages": _skill_build_service(task_id).load_messages(session_id),
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/tasks/{task_id}/skill-build/chat/stream")
    async def api_skill_build_chat_stream(task_id: str, payload: dict[str, Any] = Body(...)):
        return await _skill_build_stream_response(_skill_build_service(task_id), payload)

    @app.post("/api/tasks/{task_id}/skill-build/interrupt")
    def api_skill_build_interrupt(task_id: str):
        return {"interrupted": _skill_build_service(task_id).interrupt()}

    @app.post("/api/tasks/{task_id}/skill-build/permission")
    def api_skill_build_permission(task_id: str, payload: dict[str, Any] = Body(...)):
        request_id = payload.get("request_id")
        decision = payload.get("decision")
        if not isinstance(request_id, str) or not request_id:
            raise HTTPException(status_code=400, detail="request_id must be a non-empty string")
        if decision not in ("allow", "allow_always", "deny"):
            raise HTTPException(status_code=400, detail="decision must be allow, allow_always, or deny")
        return {"resolved": _skill_build_service(task_id).resolve_permission(request_id, decision)}

    @app.post("/api/tasks/{task_id}/skill-build/settings/bash_requires_approval")
    def api_skill_build_bash_approval(task_id: str, payload: dict[str, Any] = Body(...)):
        value = payload.get("value")
        if not isinstance(value, bool):
            raise HTTPException(status_code=400, detail="value must be boolean")
        return {"bash_requires_approval": _skill_build_service(task_id).set_bash_requires_approval(value)}

    @app.post("/api/tasks/{task_id}/skill-build/reset")
    def api_skill_build_reset(task_id: str):
        _skill_build_service(task_id).reset_terminal()
        return {"ok": True}

    @app.post("/api/tasks/{task_id}/reflect")
    def api_reflect_task(task_id: str, payload: dict[str, Any] | None = Body(default=None)):
        force = bool(payload.get("force")) if isinstance(payload, dict) else False
        return task_runner.start_reflect(task_id, force=force)

    @app.post("/api/tasks/{task_id}/replay")
    def api_replay_task(task_id: str):
        return task_runner.start_replay(task_id)

    @app.delete("/api/tasks/{task_id}")
    def api_delete_task(task_id: str):
        return task_runner.delete_task(task_id)

    @app.get("/health")
    def health():
        return {"ok": True}

    return app


# Fixed loopback port for a stable URL; re-enable if collisions with other local services matter.



# def _pick_free_port() -> int:
#     s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
#     s.bind(("127.0.0.1", 0))
#     _host, port = s.getsockname()
#     s.close()
#     return int(port)


def _run_uvicorn(
    host: str,
    port: int,
    workflows_root: str,
    recordings_root: str,
    reflect_llm_cfg: ResolvedReflectConfig | None,
    replay_llm_cfg: ResolvedLLMConfig | None,
    app_command_queue: Any | None,
    app_state: Any | None,
) -> None:
    # Import inside the subprocess so the caller doesn't require fastapi/uvicorn
    # unless the editor is actually used.
    import uvicorn  # type: ignore[import-not-found]

    app = create_app(
        workflows_root=Path(workflows_root),
        recordings_root=Path(recordings_root),
        reflect_llm_cfg=reflect_llm_cfg,
        replay_llm_cfg=replay_llm_cfg,
        app_command_queue=app_command_queue,
        app_state=app_state,
    )
    # Ensure logs go to the parent terminal (stdout/stderr).
    uvicorn.run(app, host=host, port=port, log_level="info", access_log=True)


def start_editor_server(
    *,
    workflows_root: Path,
    recordings_root: Path | None = None,
    reflect_llm_cfg: ResolvedReflectConfig | None = None,
    replay_llm_cfg: ResolvedLLMConfig | None = None,
    app_command_queue: Any | None = None,
    app_state: Any | None = None,
) -> tuple[Process, int]:
    """
    Start the editor server in a subprocess and return (process, port).
    The server binds to 127.0.0.1 only.
    """
    os.environ["AI_MIME_WORKFLOWS_ROOT"] = str(workflows_root)
    recordings_root = recordings_root or workflows_root.parent / "recordings"
    os.environ["AI_MIME_RECORDINGS_ROOT"] = str(recordings_root)
    port = EDITOR_SERVER_PORT
    _kill_processes_on_tcp_port(port)
    p = Process(
        target=_run_uvicorn,
        args=(
            "127.0.0.1",
            port,
            str(workflows_root),
            str(recordings_root),
            reflect_llm_cfg,
            replay_llm_cfg,
            app_command_queue,
            app_state,
        ),
        daemon=False,
    )
    p.start()
    print(f"[ai-mime] editor server starting on http://127.0.0.1:{port}", file=sys.stderr)
    return p, port
