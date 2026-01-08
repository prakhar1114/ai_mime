import click
from pathlib import Path
import logging

from ai_mime.permissions import check_permissions
from ai_mime.reflect.workflow import reflect_session, compile_schema_for_workflow_dir
from ai_mime.app import run_app



@click.command()
def start_app():
    """Start the menubar app."""
    # We will import the app here to avoid importing dependencies (like rumps)
    # at the top level, which might fail if permissions aren't checked yet.

    if check_permissions():
        run_app()
    else:
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
@click.option(
    "--model",
    "model",
    default="gpt-5-mini",
    show_default=True,
    help="OpenAI model name to use for reflect compilation.",
)

def reflect(session, recordings_dir, model):
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
        compile_schema_for_workflow_dir(out_dir, model=model)
        click.echo(f"Schema compiled: {out_dir / 'schema.json'}")
    except Exception as e:
        raise click.ClickException(f"Schema compilation failed: {e}")
