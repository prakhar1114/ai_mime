import atexit
import click
import contextlib
import os
import signal
import subprocess
import sys
import threading
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
from ai_mime.debug_log import log, log_server, open_server_log_file


from ai_mime.platform import free_port as platform_free_port

COMPUTER_SERVER_PORT = 58840


def _free_port(port: int) -> None:
    """Kill whatever process is currently listening on ``port``."""
    try:
        platform_free_port(port)
    except Exception as e:
        log(f"Computer server: failed to inspect port {port}: {e}", exc_info=True)


def _run_computer_server(port: int) -> None:
    """Child-process entrypoint: serve the cua computer server (with MCP)."""
    log(f"Computer server: child process started, binding port {port}")
    if is_frozen():
        log_server(f"Computer server: child process started, binding port {port}")
    try:
        if is_frozen():
            with open_server_log_file() as server_log_file:
                with contextlib.redirect_stdout(server_log_file), contextlib.redirect_stderr(server_log_file):
                    _serve_computer_server(port)
        else:
            _serve_computer_server(port)
    except Exception as e:
        log(f"Computer server: crashed on port {port}: {e}", exc_info=True)
        if is_frozen():
            log_server(f"Computer server: crashed on port {port}: {e}", exc_info=True)
        raise


def _serve_computer_server(port: int) -> None:
    print(
        f"[ai-mime] computer server starting on 0.0.0.0:{port}",
        file=sys.stderr,
        flush=True,
    )
    try:
        from ai_mime.computer_server_custom import install_custom_tools

        install_custom_tools()
    except Exception as e:
        log(
            f"Computer server: custom MCP tools unavailable; continuing without them: {e}",
            exc_info=True,
        )
        if is_frozen():
            log_server(
                f"Computer server: custom MCP tools unavailable; continuing without them: {e}",
                exc_info=True,
            )

    from computer_server import Server

    Server(host="0.0.0.0", port=port).start()


def _mcp_initialize_response_looks_valid(body: bytes) -> bool:
    """Check if an HTTP/SSE response body contains a valid MCP initialize JSON-RPC result."""
    try:
        text = body.decode("utf-8", errors="replace")
        for line in text.splitlines():
            if line.startswith("data: "):
                line = line[6:]
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                if isinstance(data, dict) and "result" in data:
                    res = data["result"]
                    if isinstance(res, dict) and ("protocolVersion" in res or "capabilities" in res):
                        return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _start_computer_server(port: int = COMPUTER_SERVER_PORT) -> None:
    """Launch the cua computer server on ``port``."""
    _free_port(port)
    if sys.platform == "win32":
        thread = threading.Thread(
            target=_run_computer_server, args=(port,), daemon=True
        )
        thread.start()
        log(f"Computer server: launched on port {port} (thread)")
        click.echo(f"Computer server starting on port {port}.")
    else:
        proc = multiprocessing.Process(
            target=_run_computer_server, args=(port,), daemon=True
        )
        proc.start()
        atexit.register(proc.terminate)
        log(f"Computer server: launched on port {port} (pid {proc.pid})")
        click.echo(f"Computer server starting on port {port} (pid {proc.pid}).")


def _start_editor_server() -> None:
    """Launch the editor/dashboard server (FastAPI) on port 58838."""
    from ai_mime.editor.server import start_editor_server
    from ai_mime.app_data import get_recordings_dir, get_workflows_dir
    workflows_root = get_workflows_dir()
    recordings_root = get_recordings_dir()
    workflows_root.mkdir(parents=True, exist_ok=True)
    recordings_root.mkdir(parents=True, exist_ok=True)
    proc, port = start_editor_server(
        workflows_root=workflows_root,
        recordings_root=recordings_root,
    )
    atexit.register(proc.terminate)
    log(f"Editor server: launched on port {port} (pid {proc.pid})")
    click.echo(f"Editor server starting on port {port} (pid {proc.pid}).")


@click.command()
def start_app():
    """Start the menubar app."""
    bootstrap_data_dir()

    # First-run onboarding — frozen builds only.
    # if is_frozen() and not get_onboarding_done_path().exists():
    if not get_onboarding_done_path().exists():
        log("start_app: running onboarding")
        run_onboarding()
        if not get_onboarding_done_path().exists():
            log("start_app: onboarding was not completed. Exiting.")
            import sys
            sys.exit(0)

    # Reload .env so that any key written by onboarding is live.
    load_dotenv(get_env_path(), override=True)

    _start_computer_server()

    if check_permissions():
        log("start_app: permissions OK, starting app")
        run_app()
    else:
        log("start_app: permissions missing, starting editor server in headless mode")
        _start_editor_server()
        click.echo("Permissions missing. Running editor server in headless mode.")

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
    elif len(sys.argv) > 1 and sys.argv[1] == "mcp-server":
        from ai_mime.mcp_server import cli_main
        sys.exit(cli_main())
    start_app()
