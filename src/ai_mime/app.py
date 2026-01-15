import rumps
import multiprocessing
from pathlib import Path
import logging
import os
import json
import time
import urllib.request
import webbrowser
from lmnr import observe
from queue import Empty
import traceback
from typing import Any

from ai_mime.record.storage import SessionStorage
# We don't import EventRecorder here anymore to avoid loading pynput in the UI process
from ai_mime.record.recorder_process import run_recorder_process

from ai_mime.replay.catalog import list_replayable_workflows
from ai_mime.reflect.workflow import reflect_session, compile_schema_for_workflow_dir
from ai_mime.replay.engine import ReplayConfig, ReplayStopped, resolve_params, run_plan
from ai_mime.replay.grounding import predict_computer_use_tool_call, tool_call_to_pixel_action
from ai_mime.replay.os_executor import exec_computer_use_action
from ai_mime.screenshot import ScreenshotRecorder
from ai_mime.editor.server import start_editor_server
from ai_mime.replay.overlay_ui import ReplayOverlay
from ai_mime.record.overlay_ui import RecordingOverlay


@observe(name="reflect_and_compile_schema")
def _run_reflect_and_compile_schema(
    session_dir: str,
    model: str = "gpt-5-mini",
    *,
    clean_manifest_tail: bool = False,
    event_queue: Any | None = None,
) -> None:
    """
    Background task (runs in its own process):
    - reflect_session(session_dir) -> workflows/<session_name>/
    - compile schema.json inside that workflow dir
    """
    # Ensure INFO logs from schema compiler show up in this subprocess.
    try:
        logging.basicConfig(level=logging.INFO)
    except Exception:
        pass

    def _emit(obj: dict[str, Any]) -> None:
        if event_queue is None:
            return
        try:
            if hasattr(event_queue, "put_nowait"):
                event_queue.put_nowait(obj)
            else:
                event_queue.put(obj)
        except Exception:
            pass

    try:
        session_dir_p = Path(session_dir)
        recordings_dir = session_dir_p.parent
        workflows_root = recordings_dir.parent / "workflows"

        out_dir = reflect_session(session_dir_p, workflows_root, clean_manifest_tail=clean_manifest_tail)
        print(f"Reflect finished: {out_dir}")
        compile_schema_for_workflow_dir(out_dir, model=model)
        print(f"Schema compiled: {out_dir / 'schema.json'}")
        _emit({"type": "reflect_compile_done", "workflow_dir": str(out_dir)})
    except Exception as e:
        _emit({"type": "reflect_compile_failed", "error": str(e), "session_dir": str(session_dir)})
        raise


def _load_replay_config_from_env() -> ReplayConfig:
    """
    Replay grounding uses OpenAI-compatible Chat Completions.

    Configure via env:
      - REPLAY_PROVIDER: "openai" | "gemini" | "qwen"
      - REPLAY_MODEL: provider-specific model name
    """
    provider = (os.getenv("REPLAY_PROVIDER") or "").strip().lower()
    model = (os.getenv("REPLAY_MODEL") or "").strip()
    if not provider:
        raise RuntimeError('Missing REPLAY_PROVIDER. Use "openai", "gemini", or "qwen".')
    if not model:
        raise RuntimeError("Missing REPLAY_MODEL.")

    base_url_by_provider = {
        "openai": "https://api.openai.com/v1",
        "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "qwen": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    }
    api_key_env_by_provider = {
        "openai": "OPENAI_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "qwen": "DASHSCOPE_API_KEY",
    }
    if provider not in base_url_by_provider:
        raise RuntimeError(f"Unsupported REPLAY_PROVIDER={provider!r}. Use one of: openai, gemini, qwen.")

    api_key = os.getenv(api_key_env_by_provider[provider])
    return ReplayConfig(model=model, base_url=base_url_by_provider[provider], api_key=api_key)

def _resolve_menubar_icon_path() -> str | None:
    """
    Best-effort menubar icon resolution.

    Priority:
    1) AI_MIME_MENUBAR_ICON env var (absolute or relative path)
    2) repo docs/logo/icon60.png (best size for macOS menubar)
    """
    # When running from the repo, app.py lives at src/ai_mime/app.py.
    # Try to locate the repo root and use docs/logo assets.
    try:
        # app.py: <repo>/src/ai_mime/app.py -> parents[2] == <repo>
        repo_root = Path(__file__).resolve().parents[2]
    except Exception:
        repo_root = None
    if repo_root:
        candidate = repo_root / "docs" / "logo" / "icon60.png"
        if candidate.exists():
            return str(candidate)

    return None


def _run_replay_workflow_schema(
    workflow_dir: str,
    overrides: dict[str, str] | None = None,
    event_queue: Any | None = None,
    exclude_window_id: int | None = None,
    pause_event: Any | None = None,
    stop_event: Any | None = None,
) -> None:
    """
    Background task (runs in its own process): replay schema.json plan using Qwen tool calls.
    """
    try:
        wf_dir = Path(workflow_dir)
        schema = json.loads((wf_dir / "schema.json").read_text(encoding="utf-8"))
        params = resolve_params(schema, overrides=overrides or {})

        cfg = _load_replay_config_from_env()
        screenshotter = ScreenshotRecorder()

        def _capture(dst: Path) -> Path:
            dst.parent.mkdir(parents=True, exist_ok=True)
            saved = screenshotter.capture(dst, exclude_window_id=exclude_window_id)
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
            pause_event=pause_event,
            stop_event=stop_event,
        )

        # Final notification + exit.
        rumps.notification(
            title="Task Complete",
            subtitle=wf_dir.name,
            message="Replay finished",
        )
        print("Task Complete")
    except ReplayStopped:
        try:
            if event_queue is not None:
                try:
                    if hasattr(event_queue, "put_nowait"):
                        event_queue.put_nowait({"type": "replay_stopped"})
                    else:
                        event_queue.put({"type": "replay_stopped"})
                except Exception:
                    pass
            rumps.notification(
                title="Replay stopped",
                subtitle=str(Path(workflow_dir).name),
                message="Stopped by user",
            )
        finally:
            print("Replay stopped by user.")
    except Exception as e:
        try:
            if event_queue is not None:
                try:
                    if hasattr(event_queue, "put_nowait"):
                        event_queue.put_nowait({"type": "replay_failed", "error": str(e)})
                    else:
                        event_queue.put({"type": "replay_failed", "error": str(e)})
                except Exception:
                    pass
            rumps.notification(
                title="Replay failed",
                subtitle=str(Path(workflow_dir).name),
                message=str(e),
            )
        finally:
            print(f"Replay failed: {e}")


class RecorderApp(rumps.App):
    def __init__(self):
        icon_path = _resolve_menubar_icon_path()
        # Keep a non-empty title: rumps can fail to attach/open the menu reliably with an empty title.
        # (We still show the icon; macOS will render both.)
        super(RecorderApp, self).__init__("AI Mime", icon=icon_path)
        # We only need storage here to read last session or show info,
        # but the active storage instance will live in the subprocess.
        self.storage = SessionStorage()

        self.recorder_process = None
        self.stop_event = None
        self.is_recording = False
        self.session_dir_queue = None
        self.session_dir = None
        self.reflect_process = None
        self.reflect_event_q: multiprocessing.Queue | None = None
        self.replay_process = None
        self.replay_event_q: multiprocessing.Queue | None = None
        self._replay_overlay: ReplayOverlay | None = None
        self._replay_state: dict[str, Any] = {}
        # multiprocessing.Event isn't typed cleanly across platforms; keep this as Any.
        self._replay_pause_event: Any | None = None
        self._replay_stop_event: Any | None = None
        self.refine_cmd_q: multiprocessing.Queue | None = None
        self.refine_resp_q: multiprocessing.Queue | None = None
        self._recording_overlay: RecordingOverlay | None = None
        self._skip_reflect_once = False
        self.dummy_recording = False

        # Local workflow editor (FastAPI) subprocess
        self.editor_process: multiprocessing.Process | None = None
        self.editor_port: int | None = None

        # Poll replay progress events from the replay worker.
        self._replay_timer = rumps.Timer(self._poll_replay_events, 0.1)
        self._replay_timer.start()

        # Poll reflect/schema compilation completion from the reflect worker.
        self._reflect_timer = rumps.Timer(self._poll_reflect_events, 0.2)
        self._reflect_timer.start()

        # Menu Items
        self.start_button = rumps.MenuItem("Start Recording", callback=self.toggle_recording)
        # Repopulate on demand when user clicks "Replay" (no polling).
        self.replay_menu = rumps.MenuItem("Replay", callback=self._on_replay_menu_clicked)
        self._populate_replay_menu()

        # Workflow review / edit
        self.edit_menu = rumps.MenuItem("Edit Workflow", callback=self._on_edit_menu_clicked)
        self._populate_edit_menu()

        # Options submenu (placed at the bottom, right above the default Quit item).
        self.options_menu = rumps.MenuItem("Options")
        self.dummy_toggle = rumps.MenuItem("Test Recording", callback=self._toggle_dummy_recording)
        self.options_menu["Test Recording"] = self.dummy_toggle

        # Build the menu using rumps.Menu APIs (more robust across rumps versions than assigning a raw list).
        self._build_menu()

    def _log_to_tmp(self, msg: str) -> None:
        try:
            Path("/tmp/ai_mime_app.log").write_text(str(msg) + "\n", encoding="utf-8")
        except Exception:
            pass

    def _build_menu(self) -> None:
        """
        Construct the status bar dropdown menu.

        If menu construction fails, write details to /tmp/ai_mime_app.log and show a visible alert.
        """
        try:
            # Preferred path: mutate the existing rumps menu in-place.
            try:
                self.menu.clear()

                self.menu.add(self.start_button)
                self.menu.add(None)
                self.menu.add(self.replay_menu)
                self.menu.add(None)
                self.menu.add(self.edit_menu)
                self.menu.add(None)
                self.menu.add(self.options_menu)
                return
            except Exception:
                # Fallback path: assign list-style menu (works across rumps versions).
                self.menu = [
                    self.start_button,
                    None,
                    self.replay_menu,
                    None,
                    self.edit_menu,
                    None,
                    self.options_menu,
                ]
                return
        except Exception as e:
            detail = f"Menu build failed: {e}\n\n{traceback.format_exc()}"
            self._log_to_tmp(detail)
            try:
                rumps.alert("AI Mime menu failed to initialize. See /tmp/ai_mime_app.log")
            except Exception:
                pass


    def _toggle_dummy_recording(self, _sender):
        # rumps supports a checkmark state via .state (0/1) on macOS.
        self.dummy_recording = not self.dummy_recording
        try:
            self.dummy_toggle.state = int(self.dummy_recording)
        except Exception:
            pass

    def _ensure_replay_overlay(self) -> ReplayOverlay:
        if self._replay_overlay is not None:
            return self._replay_overlay

        def _toggle_pause(paused: bool) -> None:
            ev = self._replay_pause_event
            if ev is None:
                return
            try:
                if paused:
                    ev.clear()
                else:
                    ev.set()
            except Exception:
                pass

        def _stop() -> None:
            se = self._replay_stop_event
            pe = self._replay_pause_event
            try:
                if se is not None:
                    se.set()
            finally:
                # Ensure we're not stuck paused while trying to stop.
                try:
                    if pe is not None:
                        pe.set()
                except Exception:
                    pass

        self._replay_overlay = ReplayOverlay(on_toggle_pause=_toggle_pause, on_stop=_stop)
        self._replay_overlay.show()
        return self._replay_overlay

    def _close_replay_overlay(self) -> None:
        if self._replay_overlay is None:
            return
        try:
            self._replay_overlay.close()
        finally:
            self._replay_overlay = None
            self.replay_event_q = None
            self._replay_state = {}
            self._replay_pause_event = None
            self._replay_stop_event = None

    def _fmt_tool_call(self, name: Any, args: Any) -> str:
        n = "" if name is None else str(name)
        if not isinstance(args, dict):
            return n

        def _truncate(s: Any, nmax: int = 140) -> str:
            t = "" if s is None else str(s)
            t = t.replace("\n", " ").strip()
            return (t[: nmax - 1] + "…") if len(t) > nmax else t

        if n == "computer_use":
            action = args.get("action")
            coord = args.get("coordinate")
            keys = args.get("keys")
            text = args.get("text")
            parts = [f"computer_use: {action}"]
            if coord is not None:
                parts.append(f"coord={coord}")
            if keys is not None:
                parts.append(f"keys={keys}")
            if text:
                parts.append(f"text={_truncate(text)}")
            return " | ".join(parts)
        if n == "extract":
            vn = args.get("variable_name")
            q = args.get("query")
            return f"extract: {vn} | query={_truncate(q)}"
        if n == "done":
            return f"done: {_truncate(args.get('result'))}"
        return f"{n}: {_truncate(args)}"

    def _poll_replay_events(self, _):
        # If the worker died unexpectedly, close overlay.
        if self.replay_process is not None and hasattr(self.replay_process, "is_alive"):
            try:
                alive = bool(self.replay_process.is_alive())
                if not alive and self._replay_overlay is not None:
                    self._close_replay_overlay()
            except Exception:
                pass

        if self.replay_event_q is None:
            return

        # Drain queue quickly; keep only latest values to reduce UI churn.
        updated = False
        while True:
            try:
                evt = self.replay_event_q.get_nowait()
            except Empty:
                break
            except Exception:
                break

            if not isinstance(evt, dict):
                continue
            et = evt.get("type")

            if et == "replay_started":
                updated = True
            elif et == "subtask_started":
                self._replay_state["subtask_idx"] = evt.get("subtask_idx")
                self._replay_state["subtask_total"] = evt.get("subtask_total")
                self._replay_state["subtask_text"] = evt.get("subtask_text") or ""
                updated = True
            elif et == "predicted_tool_call":
                self._replay_state["predicted_action"] = self._fmt_tool_call(evt.get("name"), evt.get("arguments"))
                updated = True
            elif et == "pixel_action":
                updated = True
            elif et == "extract_result":
                # Keep memory updated; surface extraction in predicted_action if no better signal.
                self._replay_state["predicted_action"] = f"extract: {evt.get('variable_name')} | query={evt.get('query')}"
                updated = True
            elif et == "done":
                # IMPORTANT: "done" in engine events means the *current subtask* finished,
                # not the whole replay. Keep the overlay running until replay_finished.
                try:
                    res = evt.get("result")
                    if res:
                        self._replay_state["predicted_action"] = f"done: {str(res)}"
                except Exception:
                    pass
                updated = True
            elif et == "replay_finished":
                # Close overlay at the end of the entire replay run.
                self._close_replay_overlay()
                return
            elif et == "replay_stopped":
                self._close_replay_overlay()
                return
            elif et == "replay_failed":
                # Close overlay on failure; show error via menubar notification already.
                self._close_replay_overlay()
                return

        if updated and self._replay_overlay is not None:
            try:
                self._replay_overlay.update(**self._replay_state)
            except Exception:
                pass

    def _poll_reflect_events(self, _):
        # Drain reflect queue quickly; emit a single notification from the UI process.
        q = self.reflect_event_q
        if q is None:
            return
        while True:
            try:
                evt = q.get_nowait()
            except Empty:
                break
            except Exception:
                break
            if not isinstance(evt, dict):
                continue
            et = evt.get("type")
            if et == "reflect_compile_done":
                try:
                    wf_dir = Path(str(evt.get("workflow_dir") or ""))
                    name = wf_dir.name if wf_dir.name else "Workflow"
                except Exception:
                    name = "Workflow"
                rumps.notification(
                    title="Processing complete",
                    subtitle=name,
                    message="Task available for running",
                )
                # Cleanup queue after completion.
                try:
                    self.reflect_event_q = None
                except Exception:
                    pass
                return
            if et == "reflect_compile_failed":
                msg = str(evt.get("error") or "Unknown error")
                rumps.notification(
                    title="Processing failed",
                    subtitle="Reflect/compile error",
                    message=msg,
                )
                try:
                    self.reflect_event_q = None
                except Exception:
                    pass
                return

    def _on_replay_menu_clicked(self, sender):
        # Refresh available workflows right before showing the submenu.
        self._populate_replay_menu()

    def _on_edit_menu_clicked(self, sender):
        # Refresh available workflows right before showing the submenu.
        self._populate_edit_menu()

    def _workflows_root(self) -> Path:
        return Path(self.storage.base_dir).parent / "workflows"

    def _read_json(self, path: Path) -> dict:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write_json_atomic(self, path: Path, obj: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def _populate_edit_menu(self):

        try:
            self.edit_menu.clear()
        except Exception:
            pass

        workflows = list_replayable_workflows(self._workflows_root())
        workflows = sorted(workflows, key=lambda w: w.workflow_dir.name, reverse=True)
        if not workflows:
            empty = rumps.MenuItem("No workflows found", callback=None)
            empty.set_callback(None)
            self.edit_menu["No workflows found"] = empty
            return

        for wf in workflows:
            def _cb(sender, wf=wf):
                try:
                    self._open_workflow_editor(wf.workflow_dir, wf.display_name)
                except Exception as e:
                    rumps.alert(f"Edit failed: {e}")

            self.edit_menu[wf.display_name] = rumps.MenuItem(wf.display_name, callback=_cb)

    def _ensure_editor_server(self) -> int:
        if self.editor_process is not None and self.editor_process.is_alive() and self.editor_port is not None:
            return self.editor_port

        proc, port = start_editor_server(workflows_root=self._workflows_root())
        self.editor_process = proc
        self.editor_port = port

        # Best-effort: wait briefly for /health so the browser doesn't immediately 404/connection-refuse.
        health_url = f"http://127.0.0.1:{port}/health"
        for _ in range(40):
            try:
                with urllib.request.urlopen(health_url, timeout=0.1) as resp:
                    if getattr(resp, "status", 200) == 200:
                        break
            except Exception:
                time.sleep(0.05)

        return port

    def _open_workflow_editor(self, workflow_dir: Path, display_name: str) -> None:
        port = self._ensure_editor_server()
        url = f"http://127.0.0.1:{port}/workflows/{workflow_dir.name}"
        ok = webbrowser.open(url, new=1)
        if not ok:
            raise RuntimeError(f"Failed to open browser for: {url}")

    def _populate_replay_menu(self):
        # Clear existing submenu items
        try:
            self.replay_menu.clear()
        except Exception:
            pass


        workflows_root = Path(self.storage.base_dir).parent / "workflows"
        workflows = list_replayable_workflows(workflows_root)
        # Newest-first so newly reflected sessions show at the top.
        workflows = sorted(workflows, key=lambda w: w.workflow_dir.name, reverse=True)

        if not workflows:
            empty = rumps.MenuItem("No workflows found", callback=None)
            empty.set_callback(None)
            self.replay_menu["No workflows found"] = empty
            return

        for wf in workflows:
            def _cb(sender, wf=wf):
                logging.basicConfig(level=logging.INFO)
                # Start replay in background so UI stays responsive.
                try:
                    if self.replay_process is not None and getattr(self.replay_process, "is_alive", lambda: False)():
                        rumps.alert("Replay already running. Please wait for it to finish.")
                        return

                    schema_path = wf.workflow_dir / "schema.json"
                    schema = json.loads(schema_path.read_text(encoding="utf-8"))
                    task_params = schema.get("task_params") or []
                    task_name = str(schema.get("task_name") or wf.display_name or "").strip()

                    overrides: dict[str, str] = {}
                    if isinstance(task_params, list) and task_params:
                        # One-window form: multiline key=default (defaults from schema examples).
                        lines: list[str] = []
                        for p in task_params:
                            name = p.get("name")
                            if not isinstance(name, str) or not name.strip():
                                continue
                            default = p.get("example")
                            default_s = "" if default is None else str(default)
                            lines.append(f"{name}={default_s}")

                        window = rumps.Window(
                            message="Edit parameters (format: key=value, one per line). Leave empty to use default.",
                            title=f"Replay Params — {wf.display_name}",
                            default_text="\n".join(lines),
                            ok="Run",
                            cancel="Cancel",
                        )
                        response = window.run()
                        if not response.clicked:
                            return

                        raw = (response.text or "").strip()
                        if raw:
                            # Parse key=value lines; blank values mean use default (skip override).
                            for line in raw.splitlines():
                                line = line.strip()
                                if not line or line.startswith("#"):
                                    continue
                                if "=" not in line:
                                    continue
                                k, v = line.split("=", 1)
                                k = k.strip()
                                v = v.strip()
                                if not k:
                                    continue
                                if v:
                                    overrides[k] = v

                    # Create overlay in the UI process and pass its window id to the worker so
                    # screenshots can be captured *below* it (overlay never appears in agent images).
                    pause_ev = multiprocessing.Event()
                    pause_ev.set()  # running by default
                    stop_ev = multiprocessing.Event()
                    self._replay_pause_event = pause_ev
                    self._replay_stop_event = stop_ev
                    overlay = self._ensure_replay_overlay()
                    overlay_id = overlay.window_id()
                    if overlay_id <= 0:
                        # Strong guarantee: if we can't get a window id, we can't safely exclude.
                        raise RuntimeError("Failed to initialize replay overlay window id.")

                    # Set up replay event queue for overlay updates.
                    self.replay_event_q = multiprocessing.Queue()
                    self._replay_state = {}

                    self.replay_process = multiprocessing.Process(
                        target=_run_replay_workflow_schema,
                        args=(
                            str(wf.workflow_dir),
                            overrides,
                            self.replay_event_q,
                            overlay_id,
                            pause_ev,
                            stop_ev,
                        ),
                    )
                    self.replay_process.start()
                    rumps.notification(
                        title="Replay started",
                        subtitle=wf.display_name,
                        message="Replaying schema plan in background",
                    )
                except Exception as e:
                    self._close_replay_overlay()
                    rumps.alert(f"Replay failed to start: {e}")

            self.replay_menu[wf.display_name] = rumps.MenuItem(wf.display_name, callback=_cb)

    def toggle_recording(self, sender):
        if not self.is_recording:
            self.start_recording()
        else:
            # Menubar stop can still record the final click, so enable legacy tail cleanup.
            self.stop_recording(clean_manifest_tail=True)

    def start_recording(self):
        # Prompt for session name
        window = rumps.Window(
            message="Enter a name for this session:",
            title="Start Recording",
            default_text="",
            ok="Start",
            cancel="Cancel",
        )
        response = window.run()

        if not response.clicked:
            return

        name = response.text.strip()
        if not name:
            rumps.alert("Name required!")
            return

        # Optional Description (2nd prompt)
        window_desc = rumps.Window(
            message="Enter description (optional):",
            title="Session Description",
            default_text="",
            ok="Go",
            cancel="Skip",
        )
        response_desc = window_desc.run()
        description = response_desc.text.strip() if response_desc.clicked else ""

        try:
            self.stop_event = multiprocessing.Event()
            self.session_dir_queue = multiprocessing.Queue()
            self.refine_cmd_q = multiprocessing.Queue()
            self.refine_resp_q = multiprocessing.Queue()
            self.session_dir = None

            # Create recording overlay in the UI process and pass its window id to the recorder
            # so screenshots can be captured *below* it (overlay never appears in images).
            self._recording_overlay = RecordingOverlay(
                refine_cmd_q=self.refine_cmd_q,
                refine_resp_q=self.refine_resp_q,
                on_cancel_recording=self.cancel_recording,
                on_finish_recording=self.finish_recording,
            )
            self._recording_overlay.show()
            overlay_id = self._recording_overlay.window_id()
            if overlay_id <= 0:
                raise RuntimeError("Failed to initialize recording overlay window id.")

            self.recorder_process = multiprocessing.Process(
                target=run_recorder_process,
                args=(
                    name,
                    description,
                    self.stop_event,
                    self.session_dir_queue,
                    self.refine_cmd_q,
                    self.refine_resp_q,
                    overlay_id,
                ),
            )
            self.recorder_process.start()

            # Best-effort: capture session dir path from the recorder subprocess.
            try:
                if self.session_dir_queue is not None:
                    self.session_dir = self.session_dir_queue.get(timeout=2.0)
            except Exception:
                self.session_dir = None

            self.is_recording = True
            self.title = "🔴 Rec"
            self.start_button.title = "Stop Recording"
        except Exception as e:
            # Best-effort cleanup if overlay/queues were partially created.
            try:
                if self._recording_overlay is not None:
                    self._recording_overlay.close()
            except Exception:
                pass
            self._recording_overlay = None
            self.refine_cmd_q = None
            self.refine_resp_q = None
            rumps.alert(f"Error starting: {e}")

    def cancel_recording(self):
        # Stop immediately and skip reflect/schema compilation.
        self._skip_reflect_once = True
        self.stop_recording(join_timeout=1.0, cancelled=True)

    def finish_recording(self):
        # Stop normally. Since this is from the overlay, we don't need tail cleanup.
        self.stop_recording(clean_manifest_tail=False, cancelled=False)

    def stop_recording(
        self,
        *,
        join_timeout: float = 10.0,
        cancelled: bool = False,
        clean_manifest_tail: bool = False,
    ):
        try:
            if self.stop_event:
                self.stop_event.set()

            if self.recorder_process:
                # Recorder should stop quickly now that reflect/schema compilation is offloaded.
                self.recorder_process.join(timeout=join_timeout)
                if self.recorder_process.is_alive():
                    self.recorder_process.terminate()
                self.recorder_process = None
                self.refine_cmd_q = None
                self.refine_resp_q = None

        except Exception as e:
            rumps.alert(f"Error stopping: {e}")

        finally:
            if self._recording_overlay is not None:
                try:
                    self._recording_overlay.close()
                finally:
                    self._recording_overlay = None

        self.is_recording = False
        self.title = "AI Mime"
        self.start_button.title = "Start Recording"

        # Kick off reflect+schema compilation in the background (do not block UI).
        if self.session_dir and not self.dummy_recording and not self._skip_reflect_once and not cancelled:
            try:
                # Queue used to notify completion back to the UI process (so notifications reliably show).
                self.reflect_event_q = multiprocessing.Queue()
                self.reflect_process = multiprocessing.Process(
                    target=_run_reflect_and_compile_schema,
                    args=(self.session_dir, "gpt-5-mini"),
                    kwargs={
                        "clean_manifest_tail": bool(clean_manifest_tail),
                        "event_queue": self.reflect_event_q,
                    },
                )
                self.reflect_process.start()
                rumps.notification(
                    title="Reflect started",
                    subtitle="Building workflow + schema in background",
                    message=Path(self.session_dir).name,
                )
            except Exception as e:
                rumps.alert(f"Error starting reflect: {e}")
        elif self.session_dir and self.dummy_recording:
            # Explicit notification so it's clear why the workflow doesn't appear.
            rumps.notification(
                title="Dummy recording saved",
                subtitle="Reflect skipped",
                message=Path(self.session_dir).name,
            )
        elif self.session_dir and (self._skip_reflect_once or cancelled):
            rumps.notification(
                title="Recording cancelled",
                subtitle="Reflect skipped",
                message=Path(self.session_dir).name,
            )

        # Reset one-shot flag.
        self._skip_reflect_once = False

        rumps.notification(
            title="Recording Saved" if not cancelled else "Recording stopped",
            subtitle="Session capture finished" if not cancelled else "Cancelled by user",
            message="The background recording process has stopped.",
        )


def run_app():
    multiprocessing.freeze_support()
    app = RecorderApp()
    app.run()
