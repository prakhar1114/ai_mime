import click

@click.command()
def record():
    """Start the recording menubar app."""
    # We will import the app here to avoid importing dependencies (like rumps)
    # at the top level, which might fail if permissions aren't checked yet.
    from ai_mime.permissions import check_permissions

    if check_permissions():
        from ai_mime.record.app import run_app
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
def reflect(session, recordings_dir):
    """Convert recordings into useful assets."""
    from pathlib import Path

    from ai_mime.reflect.workflow import reflect_session

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

@click.command()
def replay():
    """Replay a recorded workflow."""
    click.echo("Replay: Not implemented yet.")
