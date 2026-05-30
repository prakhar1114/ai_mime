import atexit
import click
import os
import signal
import subprocess
from pathlib import Path
import logging
import multiprocessing
from dotenv import load_dotenv
from lmnr import observe
from ai_mime.app_data import bootstrap_data_dir, get_env_path, get_onboarding_done_path, is_frozen
from ai_mime.permissions import check_permissions
from ai_mime.reflect.workflow import reflect_session, compile_schema_for_workflow_dir
from ai_mime.app import run_app
from ai_mime.onboarding import run_onboarding
from ai_mime.debug_log import log


COMPUTER_SERVER_PORT = 58840


def _free_port(port: int) -> None:
    """Kill whatever process is currently listening on ``port``."""
    try:
        pids = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}"],
            capture_output=True, text=True, check=False,
        ).stdout.split()
    except Exception as e:
        log(f"Computer server: failed to inspect port {port}: {e}", exc_info=True)
        return
    for pid in pids:
        try:
            os.kill(int(pid), signal.SIGKILL)
            log(f"Computer server: killed stale process {pid} on port {port}")
        except (ProcessLookupError, ValueError):
            pass


def _run_computer_server(port: int) -> None:
    """Child-process entrypoint: serve the cua computer server (with MCP)."""
    log(f"Computer server: child process started, binding port {port}")
    try:
        try:
            from ai_mime.computer_server_custom import install_custom_tools

            install_custom_tools()
        except Exception as e:
            log(
                f"Computer server: custom MCP tools unavailable; continuing without them: {e}",
                exc_info=True,
            )

        from computer_server import Server

        Server(host="0.0.0.0", port=port).start()
    except Exception as e:
        log(f"Computer server: crashed on port {port}: {e}", exc_info=True)
        raise


def _start_computer_server(port: int = COMPUTER_SERVER_PORT) -> None:
    """Launch the cua computer server on ``port`` as a child process.

    Runs in-process via multiprocessing (rather than shelling out to a separate
    interpreter) so the packaged app's server inherits the bundle's bundled
    computer_server and its macOS screen-recording/accessibility permissions.
    """
    _free_port(port)
    proc = multiprocessing.Process(
        target=_run_computer_server, args=(port,), daemon=True
    )
    proc.start()
    atexit.register(proc.terminate)
    log(f"Computer server: launched on port {port} (pid {proc.pid})")
    click.echo(f"Computer server starting on port {port} (pid {proc.pid}).")


@click.command()
def start_app():
    """Start the menubar app."""
    bootstrap_data_dir()

    # First-run onboarding — frozen builds only.
    # if is_frozen() and not get_onboarding_done_path().exists():
    if not get_onboarding_done_path().exists():
        log("start_app: running onboarding")
        run_onboarding()

    # Reload .env so that any key written by onboarding is live.
    load_dotenv(get_env_path(), override=True)

    _start_computer_server()

    if check_permissions():
        log("start_app: permissions OK, starting app")
        run_app()
    else:
        log("start_app: permissions missing, not starting app")
        click.echo("Permissions missing. Please enable them and restart.")

@click.command()
@click.option(
    "--session",
    "session",
    default=None,
    help="Session folder name under recordings/ (e.g. 20260107T130218Z-t31) or a full path to a session directory.",
)
@click.option(
    "--recordings-dir",
    "recordings_dir",
    default="recordings",
    show_default=True,
    help="Base recordings directory (used when --session is a folder name or omitted).",
)

@observe(name="reflect_session")
def reflect(session, recordings_dir):
    """Convert recordings into useful assets."""
    logging.basicConfig(level=logging.INFO)
    recordings_dir_p = Path(recordings_dir)
    if session:
        session_path = Path(session)
        if not session_path.exists():
            session_path = recordings_dir_p / session
    else:
        # Default: most recent session under recordings/
        if not recordings_dir_p.exists():
            raise click.ClickException(f"Recordings dir not found: {recordings_dir_p}")
        candidates = [p for p in recordings_dir_p.iterdir() if p.is_dir()]
        if not candidates:
            raise click.ClickException(f"No sessions found under: {recordings_dir_p}")
        session_path = max(candidates, key=lambda p: p.name)

    if not session_path.exists():
        raise click.ClickException(f"Session dir not found: {session_path}")

    workflows_root = recordings_dir_p.parent / "workflows"
    out_dir = reflect_session(session_path, workflows_root)
    click.echo(f"Workflow created: {out_dir}")

    try:
        compile_schema_for_workflow_dir(out_dir)
        click.echo(f"Schema compiled: {out_dir / 'schema.json'}")
    except Exception as e:
        raise click.ClickException(f"Schema compilation failed: {e}")


if __name__ == "__main__":
    # CRITICAL: freeze_support() must be called early for PyInstaller + multiprocessing
    multiprocessing.freeze_support()
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "computer-use":
        from ai_mime.agent_runner.computer_use import main as run_computer_use
        sys.exit(run_computer_use(sys.argv[2:]))
    start_app()
