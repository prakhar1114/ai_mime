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

# macOS UI (PyObjC / AppKit). Keep this simple and assume it's available.
import AppKit  # type: ignore[import-not-found]

NSAlert = AppKit.NSAlert  # type: ignore[attr-defined]
NSView = AppKit.NSView  # type: ignore[attr-defined]
NSTextField = AppKit.NSTextField  # type: ignore[attr-defined]
NSPopUpButton = AppKit.NSPopUpButton  # type: ignore[attr-defined]

from ai_mime.record.storage import SessionStorage
# We don't import EventRecorder here anymore to avoid loading pynput in the UI process
from ai_mime.record.recorder_process import run_recorder_process

from ai_mime.replay.catalog import list_replayable_workflows
from ai_mime.reflect.workflow import reflect_session, compile_schema_for_workflow_dir
from ai_mime.replay.engine import ReplayConfig, resolve_params, run_plan
from ai_mime.replay.grounding import predict_computer_use_tool_call, tool_call_to_pixel_action
from ai_mime.replay.os_executor import exec_computer_use_action
from ai_mime.screenshot import ScreenshotRecorder
from ai_mime.editor.server import start_editor_server


@observe(name="reflect_and_compile_schema")
def _run_reflect_and_compile_schema(session_dir: str, model: str = "gpt-5-mini") -> None:
    """
    Background task (runs in its own process):
    - reflect_session(session_dir) -> workflows/<session_name>/
    - compile schema.json inside that workflow dir
    """
    session_dir_p = Path(session_dir)
    recordings_dir = session_dir_p.parent
    workflows_root = recordings_dir.parent / "workflows"

    out_dir = reflect_session(session_dir_p, workflows_root)
    print(f"Reflect finished: {out_dir}")
    compile_schema_for_workflow_dir(out_dir, model=model)
    print(f"Schema compiled: {out_dir / 'schema.json'}")
    rumps.notification(
        title="Processing complete",
        subtitle=out_dir.name,
        message="Task available for running",
    )


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


def _run_replay_workflow_schema(workflow_dir: str, overrides: dict[str, str] | None = None) -> None:
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
            saved = screenshotter.capture(dst)
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
        )

        # Final notification + exit.
        rumps.notification(
            title="Task Complete",
            subtitle=wf_dir.name,
            message="Replay finished",
        )
        print("Task Complete")
    except Exception as e:
        try:
            rumps.notification(
                title="Replay failed",
                subtitle=str(Path(workflow_dir).name),
                message=str(e),
            )
        finally:
            print(f"Replay failed: {e}")


class RecorderApp(rumps.App):
    def __init__(self):
        super(RecorderApp, self).__init__("AI Mime", icon=None)
        # We only need storage here to read last session or show info,
        # but the active storage instance will live in the subprocess.
        self.storage = SessionStorage()

        self.recorder_process = None
        self.stop_event = None
        self.is_recording = False
        self.session_dir_queue = None
        self.session_dir = None
        self.reflect_process = None
        self.replay_process = None
        self.refine_req_q: multiprocessing.Queue | None = None
        self.refine_resp_q: multiprocessing.Queue | None = None
        self.dummy_recording = False

        # Local workflow editor (FastAPI) subprocess
        self.editor_process: multiprocessing.Process | None = None
        self.editor_port: int | None = None

        # Poll refinement requests from the recorder subprocess.
        # This stays idle unless a Ctrl+I request arrives.
        self._refine_timer = rumps.Timer(self._poll_refine_requests, 0.2)
        self._refine_timer.start()

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

        self.menu = [
            self.start_button,
            None,  # Separator
            self.replay_menu,
            None,  # Separator
            self.edit_menu,
            None,  # Separator
            self.options_menu,
        ]

    def _toggle_dummy_recording(self, _sender):
        # rumps supports a checkmark state via .state (0/1) on macOS.
        self.dummy_recording = not self.dummy_recording
        try:
            self.dummy_toggle.state = int(self.dummy_recording)
        except Exception:
            pass

    def _poll_refine_requests(self, _):
        """
        Recorder subprocess sends a refinement request over a queue.
        We show a small separate modal popup and send response back.
        """
        if not self.refine_req_q or not self.refine_resp_q:
            return
        try:
            req = self.refine_req_q.get_nowait()
        except Empty:
            return

        resp = self._run_refine_popup(req)
        self.refine_resp_q.put(resp)

    def _run_refine_popup(self, req: dict) -> dict:
        """
        Show separate modal popups:
        - Step 1: choose Extract/Add details (dropdown only)
        - Step 2: show only the relevant fields for the chosen action
        Returns structured dict to send back to recorder process.
        """
        # Step 1: choose action kind
        choose_alert = NSAlert.alloc().init()
        choose_alert.setMessageText_("Action refinement")
        choose_alert.setInformativeText_("Choose Extract or Add details. Recording is paused until you submit/cancel.")
        choose_alert.addButtonWithTitle_("Next")
        choose_alert.addButtonWithTitle_("Cancel")

        choose_view = NSView.alloc().initWithFrame_(((0, 0), (320, 40)))
        dropdown = NSPopUpButton.alloc().initWithFrame_pullsDown_(((0, 8), (220, 26)), False)
        dropdown.addItemsWithTitles_(["Extract", "Add details"])
        choose_view.addSubview_(dropdown)
        choose_alert.setAccessoryView_(choose_view)

        rc = choose_alert.runModal()
        if int(rc) != 1000:
            return {"kind": "cancel", "req_id": req.get("req_id")}

        kind = str(dropdown.titleOfSelectedItem() or "")

        # Step 2: show only relevant fields
        if kind == "Extract":
            alert = NSAlert.alloc().init()
            alert.setMessageText_("Extract")
            alert.setInformativeText_("Fill query + extracted values.")
            alert.addButtonWithTitle_("Submit")
            alert.addButtonWithTitle_("Cancel")

            view = NSView.alloc().initWithFrame_(((0, 0), (460, 90)))
            query = NSTextField.alloc().initWithFrame_(((0, 50), (460, 24)))
            query.setPlaceholderString_("Query (what to extract from the page)")
            view.addSubview_(query)

            values = NSTextField.alloc().initWithFrame_(((0, 12), (460, 24)))
            values.setPlaceholderString_("Values (what you extracted)")
            view.addSubview_(values)

            alert.setAccessoryView_(view)
            rc2 = alert.runModal()
            if int(rc2) != 1000:
                return {"kind": "cancel", "req_id": req.get("req_id")}
            return {
                "kind": "extract",
                "query": str(query.stringValue() or "").strip(),
                "values": str(values.stringValue() or "").strip(),
                "req_id": req.get("req_id"),
            }

        # Add details
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Add details")
        alert.setInformativeText_("These details will be attached to the next recorded event.")
        alert.addButtonWithTitle_("Submit")
        alert.addButtonWithTitle_("Cancel")

        view = NSView.alloc().initWithFrame_(((0, 0), (460, 50)))
        details = NSTextField.alloc().initWithFrame_(((0, 12), (460, 24)))
        details.setPlaceholderString_("Details (natural language)")
        view.addSubview_(details)
        alert.setAccessoryView_(view)

        rc2 = alert.runModal()
        if int(rc2) != 1000:
            return {"kind": "cancel", "req_id": req.get("req_id")}
        return {
            "kind": "details",
            "text": str(details.stringValue() or "").strip(),
            "req_id": req.get("req_id"),
        }

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
                    schema_path = wf.workflow_dir / "schema.json"
                    schema = json.loads(schema_path.read_text(encoding="utf-8"))
                    task_params = schema.get("task_params") or []

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

                    self.replay_process = multiprocessing.Process(
                        target=_run_replay_workflow_schema,
                        args=(str(wf.workflow_dir), overrides),
                    )
                    self.replay_process.start()
                    rumps.notification(
                        title="Replay started",
                        subtitle=wf.display_name,
                        message="Replaying schema plan in background",
                    )
                except Exception as e:
                    rumps.alert(f"Replay failed to start: {e}")

            self.replay_menu[wf.display_name] = rumps.MenuItem(wf.display_name, callback=_cb)

    def toggle_recording(self, sender):
        if not self.is_recording:
            self.start_recording()
        else:
            self.stop_recording()

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
            self.refine_req_q = multiprocessing.Queue()
            self.refine_resp_q = multiprocessing.Queue()
            self.session_dir = None
            self.recorder_process = multiprocessing.Process(
                target=run_recorder_process,
                args=(
                    name,
                    description,
                    self.stop_event,
                    self.session_dir_queue,
                    self.refine_req_q,
                    self.refine_resp_q,
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
            rumps.alert(f"Error starting: {e}")

    def stop_recording(self):
        try:
            if self.stop_event:
                self.stop_event.set()

            if self.recorder_process:
                # Recorder should stop quickly now that reflect/schema compilation is offloaded.
                self.recorder_process.join(timeout=10)
                if self.recorder_process.is_alive():
                    self.recorder_process.terminate()
                self.recorder_process = None
                self.refine_req_q = None
                self.refine_resp_q = None

        except Exception as e:
            rumps.alert(f"Error stopping: {e}")

        self.is_recording = False
        self.title = "AI Mime"
        self.start_button.title = "Start Recording"

        # Kick off reflect+schema compilation in the background (do not block UI).
        if self.session_dir and not self.dummy_recording:
            try:
                self.reflect_process = multiprocessing.Process(
                    target=_run_reflect_and_compile_schema,
                    args=(self.session_dir, "gpt-5-mini"),
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

        rumps.notification(
            title="Recording Saved",
            subtitle="Session capture finished",
            message="The background recording process has stopped.",
        )


def run_app():
    multiprocessing.freeze_support()
    app = RecorderApp()
    app.run()
