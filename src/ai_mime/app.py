import rumps
import multiprocessing
from pathlib import Path
import time
import urllib.parse
import urllib.request
import webbrowser
from lmnr import observe
from queue import Empty
import traceback
from typing import Any

from ai_mime.record.storage import SessionStorage
# We don't import EventRecorder here anymore to avoid loading pynput in the UI process
from ai_mime.record.recorder_process import run_recorder_process
from ai_mime.app_data import get_bundled_resource, get_recordings_dir, get_workflows_dir

from ai_mime.user_config import ResolvedUserConfig, load_user_config
from ai_mime.reflect.runner import run_reflect_and_compile_schema
from ai_mime.editor.server import start_editor_server
from ai_mime.record.overlay_ui import RecordingOverlay
from ai_mime.debug_log import log


@observe(name="reflect_and_compile_schema")
def _run_reflect_and_compile_schema(
    session_dir: str,
    *,
    clean_manifest_tail: bool = False,
    event_queue: Any | None = None,
) -> None:
    """
    Background task (runs in its own process):
    - reflect_session(session_dir) -> workflows/<session_name>/
    - compile schema.json inside that workflow dir
    """
    run_reflect_and_compile_schema(
        session_dir,
        clean_manifest_tail=clean_manifest_tail,
        event_queue=event_queue,
    )


def _resolve_menubar_icon_path() -> str | None:
    """Best-effort menubar icon resolution (works frozen and in dev)."""
    # Use icon32.png for proper menubar sizing (macOS recommends 22-32px for menubar icons)
    candidate = get_bundled_resource("docs/logo/icon32.png")
    if candidate.exists():
        return str(candidate)
    # Fallback to icon60 if icon32 doesn't exist
    fallback = get_bundled_resource("docs/logo/icon60.png")
    if fallback.exists():
        return str(fallback)
    return None


class RecorderApp(rumps.App):
    def __init__(self, *, user_cfg: ResolvedUserConfig):
        icon_path = _resolve_menubar_icon_path()
        # Keep a non-empty title: rumps can fail to attach/open the menu reliably with an empty title.
        # (We still show the icon; macOS will render both.)
        super(RecorderApp, self).__init__("AI Mime", icon=icon_path, quit_button=None)
        self._user_cfg = user_cfg
        # We only need storage here to read last session or show info,
        # but the active storage instance will live in the subprocess.
        self.storage = SessionStorage(base_dir=str(get_recordings_dir()))

        self.recorder_process = None
        self.stop_event = None
        self.is_recording = False
        self.session_dir_queue = None
        self.session_dir = None
        self.reflect_process = None
        self.reflect_event_q: multiprocessing.Queue | None = None
        self.refine_cmd_q: multiprocessing.Queue | None = None
        self.refine_resp_q: multiprocessing.Queue | None = None
        self._recording_overlay: RecordingOverlay | None = None
        self._conversation_overlay: Any | None = None
        self._skip_reflect_once = False
        self.dummy_recording = False

        # Track workflows currently being processed (session_name -> status)
        self._processing_workflows: dict[str, str] = {}  # session_name -> "reflecting" | "compiling"

        # Browser dashboard -> rumps app commands and app -> dashboard status.
        self.dashboard_command_q: multiprocessing.Queue | None = multiprocessing.Queue()
        self._dashboard_manager = multiprocessing.Manager()
        self.dashboard_state = self._dashboard_manager.dict()

        # Local task dashboard (FastAPI) subprocess.
        self.dashboard_process: multiprocessing.Process | None = None
        self.dashboard_port: int | None = None

        # Poll reflect/schema compilation completion from the reflect worker.
        self._reflect_timer = rumps.Timer(self._poll_reflect_events, 0.2)
        self._reflect_timer.start()

        self._dashboard_command_timer = rumps.Timer(self._poll_dashboard_commands, 0.25)
        self._dashboard_command_timer.start()
        self._publish_dashboard_state()

        # Menu Items
        self.start_button = rumps.MenuItem("Start Recording", callback=self.toggle_recording)
        self.tasks_button = rumps.MenuItem("Open Dashboard", callback=self._open_tasks_dashboard)
        self.workflows_button = rumps.MenuItem("Open Workflows Directory", callback=self._open_workflows_directory)

        # Options submenu (placed at the bottom, right above the default Quit item).
        self.options_menu = rumps.MenuItem("Options")
        self.dummy_toggle = rumps.MenuItem("Test Recording", callback=self._toggle_dummy_recording)
        self.options_menu["Test Recording"] = self.dummy_toggle
        self.custom_quit_button = rumps.MenuItem("Quit", callback=self.quit_app)

        # Build the menu using rumps.Menu APIs (more robust across rumps versions than assigning a raw list).
        self._build_menu()
        self.port = self._ensure_dashboard_server()
        self._open_tasks_dashboard()

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
                self.menu.add(self.tasks_button)
                self.menu.add(self.workflows_button)
                self.menu.add(None)
                self.menu.add(self.options_menu)
                self.menu.add(None)
                self.menu.add(self.custom_quit_button)
                return
            except Exception:
                # Fallback path: assign list-style menu (works across rumps versions).
                self.menu = [
                    self.start_button,
                    self.tasks_button,
                    self.workflows_button,
                    None,
                    self.options_menu,
                    None,
                    self.custom_quit_button,
                ]
                return
        except Exception as e:
            detail = f"Menu build failed: {e}\n\n{traceback.format_exc()}"
            self._log_to_tmp(detail)
            try:
                rumps.alert("AI Mime menu failed to initialize. See /tmp/ai_mime_app.log")
            except Exception:
                pass

    def _publish_dashboard_state(self, *, recording_requested: bool | None = None) -> None:
        try:
            existing = dict(self.dashboard_state.get("recording") or {})
            if recording_requested is None:
                requested = bool(existing.get("requested")) and not self.is_recording
            else:
                requested = bool(recording_requested)
            session_name = None
            if self.session_dir:
                try:
                    session_name = Path(str(self.session_dir)).name
                except Exception:
                    session_name = str(self.session_dir)
            self.dashboard_state["recording"] = {
                "is_recording": bool(self.is_recording),
                "session_name": session_name,
                "requested": requested,
            }
            self.dashboard_state["reflecting"] = dict(self._processing_workflows)
        except Exception as e:
            log(f"Error publishing dashboard state: {e}", exc_info=True)

    def _poll_dashboard_commands(self, _):
        q = self.dashboard_command_q
        if q is None:
            return
        handled = False
        while True:
            try:
                cmd = q.get_nowait()
            except Empty:
                break
            except Exception as e:
                log(f"Error polling command queue: {e}", exc_info=True)
                break
            try:
                if not isinstance(cmd, dict):
                    continue
                if cmd.get("type") == "start_recording":
                    handled = True
                    if self.is_recording:
                        self._publish_dashboard_state(recording_requested=False)
                        continue
                    try:
                        self.start_recording()
                    finally:
                        self._publish_dashboard_state(recording_requested=False)
                elif cmd.get("type") == "show_conversation_overlay":
                    handled = True
                    mode = cmd.get("mode") or "general"
                    task_id = cmd.get("task_id") or ""
                    if self._conversation_overlay is not None:
                        try:
                            self._conversation_overlay.close()
                        except Exception:
                            pass
                    try:
                        from ai_mime.overlay.conversation_overlay import ConversationOverlay
                        self._conversation_overlay = ConversationOverlay(port=self.port, task_id=task_id, mode=mode)
                        self._conversation_overlay.show()
                    except Exception as e:
                        log(f"Failed to create ConversationOverlay: {e}", exc_info=True)
                elif cmd.get("type") == "update_conversation_overlay":
                    handled = True
                    if self._conversation_overlay is not None:
                        try:
                            if "text" in cmd:
                                self._conversation_overlay.update_text(cmd["text"])
                            if "tool" in cmd:
                                self._conversation_overlay.update_tool(cmd["tool"])
                        except Exception as e:
                            log(f"Failed to update ConversationOverlay: {e}", exc_info=True)
                elif cmd.get("type") == "hide_conversation_overlay":
                    handled = True
                    if self._conversation_overlay is not None:
                        try:
                            self._conversation_overlay.close()
                        except Exception:
                            pass
                        self._conversation_overlay = None
                elif cmd.get("type") == "toggle_conversation_overlay":
                    handled = True
                    self._toggle_conversation_overlay(None)
                elif cmd.get("type") == "show_automation_overlay":
                    handled = True
                    task_id = cmd.get("task_id") or ""
                    if self._conversation_overlay is not None:
                        try:
                            self._conversation_overlay.close()
                        except Exception:
                            pass
                        self._conversation_overlay = None
                    try:
                        from ai_mime.overlay.conversation_overlay import AutomationOverlay
                        self._conversation_overlay = AutomationOverlay(port=self.port, task_id=task_id)
                    except Exception as e:
                        log(f"Failed to create AutomationOverlay: {e}", exc_info=True)
                elif cmd.get("type") == "update_automation_overlay":
                    handled = True
                    status = cmd.get("status") or "running"
                    if self._conversation_overlay is not None and hasattr(self._conversation_overlay, "update_status"):
                        try:
                            self._conversation_overlay.update_status(status)
                        except Exception as e:
                            log(f"Failed to update AutomationOverlay: {e}", exc_info=True)
                elif cmd.get("type") == "quit_app":
                    handled = True
                    self.quit_app()
                elif cmd.get("type") == "open_workflows_directory":
                    handled = True
                    self._open_workflows_directory()
                elif cmd.get("type") == "open_directory":
                    handled = True
                    self._open_directory(cmd.get("path") or "")
            except Exception as e:
                log(f"Error handling dashboard command {cmd}: {e}", exc_info=True)
        if handled:
            self._publish_dashboard_state()

    def _toggle_dummy_recording(self, _sender):
        # rumps supports a checkmark state via .state (0/1) on macOS.
        self.dummy_recording = not self.dummy_recording
        try:
            self.dummy_toggle.state = int(self.dummy_recording)
        except Exception:
            pass

    def _toggle_conversation_overlay(self, _sender=None):
        if self._conversation_overlay is not None:
            try:
                if self._conversation_overlay.is_minimized:
                    self._conversation_overlay.maximize()
                else:
                    self._conversation_overlay.minimize()
            except Exception as e:
                log(f"Failed to toggle ConversationOverlay: {e}", exc_info=True)
        else:
            try:
                rumps.notification(
                    title="AI Mime",
                    subtitle="No active overlay",
                    message="Overlay is only active during agent conversations.",
                )
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

            if et == "reflect_phase_started":
                # Update processing status to show current phase
                session_name = evt.get("session_name")
                phase = evt.get("phase")
                if session_name and phase:
                    self._processing_workflows[session_name] = phase
                    self._publish_dashboard_state()
                    log(f"Updated {session_name} status to {phase}")
                continue

            if et == "reflect_compile_done":
                try:
                    wf_dir = Path(str(evt.get("workflow_dir") or ""))
                    name = wf_dir.name if wf_dir.name else "Workflow"
                except Exception:
                    name = "Workflow"

                # Remove from processing state
                if name in self._processing_workflows:
                    del self._processing_workflows[name]
                self._publish_dashboard_state()

                rumps.notification(
                    title="Processing complete",
                    subtitle=name,
                    message="Task updated in dashboard",
                )
                # Cleanup queue after completion.
                try:
                    self.reflect_event_q = None
                except Exception:
                    pass
                return
            if et == "reflect_compile_failed":
                msg = str(evt.get("error") or "Unknown error")
                session_dir = str(evt.get("session_dir") or "")
                if session_dir:
                    session_name = Path(session_dir).name
                    if session_name in self._processing_workflows:
                        del self._processing_workflows[session_name]
                self._publish_dashboard_state()

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

    def _workflows_root(self) -> Path:
        return Path(self.storage.base_dir).parent / "workflows"

    def _ensure_dashboard_server(self) -> int:
        if self.dashboard_process is not None and self.dashboard_process.is_alive() and self.dashboard_port is not None:
            return self.dashboard_port

        proc, port = start_editor_server(
            workflows_root=self._workflows_root(),
            recordings_root=get_recordings_dir(),
            app_command_queue=self.dashboard_command_q,
            app_state=self.dashboard_state,
        )
        self.dashboard_process = proc
        self.dashboard_port = port

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

    def _open_tasks_dashboard(self, _sender=None) -> None:
        try:
            url = f"http://127.0.0.1:{self.port}/tasks"
            ok = webbrowser.open(url, new=1)
            if not ok:
                raise RuntimeError(f"Failed to open browser for: {url}")
        except Exception as e:
            rumps.alert(f"Open Tasks failed: {e}")

    def _open_workflows_directory(self, _sender=None) -> None:
        try:
            import subprocess
            path = get_workflows_dir()
            path.mkdir(parents=True, exist_ok=True)
            subprocess.run(["open", str(path)], check=True)
        except Exception as e:
            rumps.alert(f"Open Workflows Directory failed: {e}")

    def _open_directory(self, path_raw: str) -> None:
        try:
            import subprocess
            path = Path(str(path_raw)).expanduser()
            if not path.is_dir():
                raise RuntimeError(f"Directory not found: {path}")
            subprocess.run(["open", str(path)], check=True)
        except Exception as e:
            rumps.alert(f"Open Directory failed: {e}")

    def _open_skill_build_for_task(self, task_id: str) -> None:
        try:
            encoded = urllib.parse.quote(task_id, safe="")
            url = f"http://127.0.0.1:{self.port}/skill-build/{encoded}"
            ok = webbrowser.open(url, new=1)
            if not ok:
                raise RuntimeError(f"Failed to open browser for: {url}")
        except Exception as e:
            log(f"Open skill build failed: {e}", exc_info=True)
            rumps.alert(f"Open Skill Build failed: {e}")

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
                kwargs={"base_dir": str(get_recordings_dir())},
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
            self._publish_dashboard_state(recording_requested=False)
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
            self._publish_dashboard_state(recording_requested=False)
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
        self._publish_dashboard_state(recording_requested=False)

        # Kick off reflect+schema compilation in the background (do not block UI).
        if self.session_dir and not self.dummy_recording and not self._skip_reflect_once and not cancelled:
            try:
                log(f"Starting reflect subprocess for {self.session_dir}")
                session_name = Path(self.session_dir).name
                # Mark as processing
                self._processing_workflows[session_name] = "reflecting"
                self._publish_dashboard_state()

                # Queue used to notify completion back to the UI process (so notifications reliably show).
                self.reflect_event_q = multiprocessing.Queue()
                self.reflect_process = multiprocessing.Process(
                    target=_run_reflect_and_compile_schema,
                    args=(self.session_dir,),
                    kwargs={
                        "clean_manifest_tail": bool(clean_manifest_tail),
                        "event_queue": self.reflect_event_q,
                    },
                )
                self.reflect_process.start()
                self._open_skill_build_for_task(session_name)
                log("Reflect subprocess started, showing notification")
                rumps.notification(
                    title="Reflect started",
                    subtitle="Building workflow + schema in background",
                    message=session_name,
                )
                log("Notification shown")
            except Exception as e:
                log(f"Error starting reflect: {e}", exc_info=True)
                if session_name in self._processing_workflows:
                    del self._processing_workflows[session_name]
                self._publish_dashboard_state()
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
        self._publish_dashboard_state()

        rumps.notification(
            title="Recording Saved" if not cancelled else "Recording stopped",
            subtitle="Session capture finished" if not cancelled else "Cancelled by user",
            message="The background recording process has stopped.",
        )

    def quit_app(self, _sender=None):
        # Terminate dashboard process if active
        if self.dashboard_process and self.dashboard_process.is_alive():
            try:
                self.dashboard_process.terminate()
                self.dashboard_process.join(timeout=1.0)
            except Exception:
                pass
        
        # Stop recording if active
        if self.is_recording:
            try:
                self.stop_recording(join_timeout=1.0, cancelled=True)
            except Exception:
                pass

        # Terminate active reflect process
        if self.reflect_process and self.reflect_process.is_alive():
            try:
                self.reflect_process.terminate()
                self.reflect_process.join(timeout=1.0)
            except Exception:
                pass

        # Kill all processes containing "mime"
        self._kill_mime_processes()
        
        # Quit the rumps application
        rumps.quit_application()

    def _kill_mime_processes(self):
        import os
        import signal
        import subprocess

        current_pid = os.getpid()
        
        # 1. Gather all descendants of the current process
        descendants = set()
        try:
            output = subprocess.check_output(["ps", "-ax", "-o", "pid,ppid"], text=True)
            children_map = {}
            for line in output.strip().splitlines()[1:]:
                parts = line.strip().split()
                if len(parts) >= 2:
                    try:
                        pid = int(parts[0])
                        ppid = int(parts[1])
                        children_map.setdefault(ppid, []).append(pid)
                    except ValueError:
                        continue
            def gather(p):
                for child in children_map.get(p, []):
                    if child not in descendants:
                        descendants.add(child)
                        gather(child)
            gather(current_pid)
        except Exception:
            pass

        # 2. Gather processes matching specific keywords
        keyword_pids = set()
        try:
            output = subprocess.check_output(["ps", "-ax", "-o", "pid,command"], text=True)
            for line in output.strip().splitlines()[1:]:
                parts = line.strip().split(None, 1)
                if len(parts) < 2:
                    continue
                try:
                    pid = int(parts[0])
                except ValueError:
                    continue
                
                cmd = parts[1]
                cmd_lower = cmd.lower()
                
                # Exclude IDEs and helpers
                if any(x in cmd_lower for x in ["cursor", "vscode", "helper", "extension-host", "grep"]):
                    continue

                if ("mime" in cmd_lower or
                    "run_computer_use" in cmd_lower or
                    "computer_server" in cmd_lower or
                    ("run.sh" in cmd_lower and ("ai_mime" in cmd_lower or "workflows" in cmd_lower)) or
                    ("run.py" in cmd_lower and ("ai_mime" in cmd_lower or "workflows" in cmd_lower))):
                    keyword_pids.add(pid)
        except Exception:
            pass

        all_to_kill = descendants.union(keyword_pids)
        if current_pid in all_to_kill:
            all_to_kill.remove(current_pid)

        for pid in all_to_kill:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except Exception:
                pass


def run_app():
    user_cfg = load_user_config()
    app = RecorderApp(user_cfg=user_cfg)
    app.run()
