import rumps
import multiprocessing
from pathlib import Path
import logging
import os
import json
from lmnr import observe

from ai_mime.record.storage import SessionStorage
# We don't import EventRecorder here anymore to avoid loading pynput in the UI process
from ai_mime.record.recorder_process import run_recorder_process

from ai_mime.replay.catalog import list_replayable_workflows
from ai_mime.reflect.workflow import reflect_session, compile_schema_for_workflow_dir
from ai_mime.replay.engine import ReplayConfig, resolve_params, run_plan
from ai_mime.replay.grounding import predict_computer_use_tool_call, tool_call_to_pixel_action
from ai_mime.replay.os_executor import exec_computer_use_action
from ai_mime.screenshot import ScreenshotRecorder


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


def _run_replay_workflow_schema(workflow_dir: str) -> None:
    """
    Background task (runs in its own process): replay schema.json plan using Qwen tool calls.
    """
    try:
        wf_dir = Path(workflow_dir)
        schema = json.loads((wf_dir / "schema.json").read_text(encoding="utf-8"))
        params = resolve_params(schema, overrides={})

        cfg = ReplayConfig(
            model="qwen3-vl-plus-2025-12-19",
            base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            api_key=os.getenv("DASHSCOPE_API_KEY"),
            dry_run=False,
        )
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

        # Menu Items
        self.start_button = rumps.MenuItem("Start Recording", callback=self.toggle_recording)
        # Repopulate on demand when user clicks "Replay" (no polling).
        self.replay_menu = rumps.MenuItem("Replay", callback=self._on_replay_menu_clicked)
        self._populate_replay_menu()
        self.menu = [
            self.start_button,
            None,  # Separator
            self.replay_menu,
        ]

    def _on_replay_menu_clicked(self, sender):
        # Refresh available workflows right before showing the submenu.
        self._populate_replay_menu()

    def _populate_replay_menu(self):
        # Clear existing submenu items
        try:
            self.replay_menu.clear()
        except Exception:
            # Best-effort: if clear isn't available for some reason, recreate the submenu.
            self.replay_menu = rumps.MenuItem("Replay")

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
                    self.replay_process = multiprocessing.Process(
                        target=_run_replay_workflow_schema,
                        args=(str(wf.workflow_dir),),
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
            self.session_dir = None
            self.recorder_process = multiprocessing.Process(
                target=run_recorder_process,
                args=(name, description, self.stop_event, self.session_dir_queue),
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

        except Exception as e:
            rumps.alert(f"Error stopping: {e}")

        self.is_recording = False
        self.title = "AI Mime"
        self.start_button.title = "Start Recording"

        # Kick off reflect+schema compilation in the background (do not block UI).
        if self.session_dir:
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

        rumps.notification(
            title="Recording Saved",
            subtitle="Session capture finished",
            message="The background recording process has stopped.",
        )


def run_app():
    multiprocessing.freeze_support()
    app = RecorderApp()
    app.run()
