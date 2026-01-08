import click
from pathlib import Path
import logging
import os
import json

from ai_mime.permissions import check_permissions
from ai_mime.reflect.workflow import reflect_session, compile_schema_for_workflow_dir
from ai_mime.record.app import run_app
from ai_mime.replay import list_replayable_workflows, resolve_workflow
from ai_mime.replay_engine import resolve_params, ReplayConfig, run_plan
from ai_mime.qwen_grounding import predict_computer_use_tool_call, tool_call_to_pixel_action
from ai_mime.os_executor import exec_computer_use_action
from ai_mime.record.screenshot import ScreenshotRecorder



@click.command()
def record():
    """Start the recording menubar app."""
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

@click.command()
@click.option(
    "--workflow",
    "workflow",
    default=None,
    help="Workflow folder name under workflows/ or a full path to a workflow directory.",
)
@click.option(
    "--workflows-dir",
    "workflows_dir",
    default="workflows",
    show_default=True,
    help="Base workflows directory.",
)
@click.option(
    "--model",
    "model",
    default="qwen3-vl-plus-2025-12-19",
    show_default=True,
    help="DashScope model name to use for replay (computer use).",
)
@click.option(
    "--base-url",
    "base_url",
    default="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    show_default=True,
    help="OpenAI-compatible base URL for DashScope.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print planned actions but do not execute mouse/keyboard.",
)
@click.option(
    "--param",
    "params",
    multiple=True,
    help="Override a task param. Repeatable: --param key=value",
)
def replay(workflow, workflows_dir, model, base_url, dry_run, params):
    """Replay a recorded workflow by executing schema.json.plan.steps using Qwen computer_use tool calls."""
    logging.basicConfig(level=logging.INFO)
    workflows_root = Path(workflows_dir)

    available = list_replayable_workflows(workflows_root)
    if not available:
        raise click.ClickException(f"No workflows with schema.json found under: {workflows_root}")

    if workflow:
        wf = resolve_workflow(workflows_root, workflow)
    else:
        # Default: "latest" by folder name (timestamps sort lexicographically)
        wf = max(available, key=lambda x: x.workflow_dir.name)
        click.echo(f"No --workflow provided; defaulting to: {wf.display_name} ({wf.workflow_dir.name})")

    overrides: dict[str, str] = {}
    for p in params:
        if "=" not in p:
            raise click.ClickException(f"Invalid --param '{p}'. Expected key=value")
        k, v = p.split("=", 1)
        overrides[k.strip()] = v

    schema = json.loads((wf.workflow_dir / "schema.json").read_text(encoding="utf-8"))
    resolved = resolve_params(schema, overrides=overrides)

    cfg = ReplayConfig(
        model=model,
        base_url=base_url,
        api_key=os.getenv("DASHSCOPE_API_KEY"),
        dry_run=dry_run,
    )

    screenshotter = ScreenshotRecorder()

    def _capture(dst: Path) -> Path:
        dst.parent.mkdir(parents=True, exist_ok=True)
        saved = screenshotter.capture(dst)
        if not saved:
            raise click.ClickException("Screenshot capture failed (check Screen Recording permission).")
        return Path(saved)

    run_plan(
        wf.workflow_dir,
        params=resolved,
        cfg=cfg,
        predict_tool_call=predict_computer_use_tool_call,
        tool_call_to_pixel_action=tool_call_to_pixel_action,
        capture_screenshot=_capture,
        exec_action=exec_computer_use_action,
        log=click.echo,
    )

    click.echo(f"Replay completed: {wf.display_name}")
