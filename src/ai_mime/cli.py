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
def reflect():
    """Convert recordings into useful assets."""
    click.echo("Reflect: Not implemented yet.")

@click.command()
def replay():
    """Replay a recorded workflow."""
    click.echo("Replay: Not implemented yet.")
