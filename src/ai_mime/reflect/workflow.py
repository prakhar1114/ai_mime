from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from ai_mime.reflect.schema_compiler import compile_workflow_schema


@dataclass(frozen=True)
class ClickAnnotationStyle:
    box_size_px: int = 36
    color: str = "#ff3b30"
    stroke_width_px: int = 3
    dash_px: int = 6
    gap_px: int = 4


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            events.append(json.loads(line))
    return events


def _write_jsonl(path: Path, events: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def _clamp_int(v: float | int, lo: int, hi: int) -> int:
    try:
        vv = int(round(float(v)))
    except Exception:
        vv = int(v)  # best-effort
    return max(lo, min(hi, vv))


def _draw_dotted_rect(
    draw: ImageDraw.ImageDraw,
    left: int,
    top: int,
    right: int,
    bottom: int,
    *,
    color: str,
    width: int,
    dash: int,
    gap: int,
) -> None:
    # Top/bottom edges (horizontal)
    x = left
    while x < right:
        x2 = min(x + dash, right)
        draw.line([(x, top), (x2, top)], fill=color, width=width)
        draw.line([(x, bottom), (x2, bottom)], fill=color, width=width)
        x += dash + gap

    # Left/right edges (vertical)
    y = top
    while y < bottom:
        y2 = min(y + dash, bottom)
        draw.line([(left, y), (left, y2)], fill=color, width=width)
        draw.line([(right, y), (right, y2)], fill=color, width=width)
        y += dash + gap


def _annotate_clicks_on_image(
    src_path: Path,
    dst_path: Path,
    clicks: list[tuple[float, float]],
    style: ClickAnnotationStyle,
) -> None:
    dst_path.parent.mkdir(parents=True, exist_ok=True)

    with Image.open(src_path) as im:
        im = im.convert("RGBA")
        draw = ImageDraw.Draw(im)

        w, h = im.size
        half = style.box_size_px // 2
        for x, y in clicks:
            cx = _clamp_int(x, 0, w - 1)
            cy = _clamp_int(y, 0, h - 1)
            left = max(0, cx - half)
            right = min(w - 1, cx + half)
            top = max(0, cy - half)
            bottom = min(h - 1, cy + half)

            # Ensure a non-empty rectangle even near edges / tiny images.
            if right <= left:
                right = min(w - 1, left + 1)
            if bottom <= top:
                bottom = min(h - 1, top + 1)

            _draw_dotted_rect(
                draw,
                left,
                top,
                right,
                bottom,
                color=style.color,
                width=style.stroke_width_px,
                dash=style.dash_px,
                gap=style.gap_px,
            )

        # Preserve PNG output; flatten to RGB to avoid unexpected alpha handling in viewers.
        out = im.convert("RGB")
        out.save(dst_path)


def _copy_or_annotate_screenshots(
    *,
    source_session_dir: Path,
    workflow_dir: Path,
    events: list[dict[str, Any]],
    style: ClickAnnotationStyle,
) -> None:
    # Group click coordinates by screenshot path so we can annotate multiple clicks on the same image.
    click_coords_by_screenshot: dict[str, list[tuple[float, float]]] = {}
    screenshot_paths: set[str] = set()

    for e in events:
        screenshot = e.get("screenshot")
        if not screenshot:
            continue
        screenshot_paths.add(screenshot)
        if e.get("action_type") in {"click", "double_click", "right_click", "middle_click"}:
            details = e.get("action_details") or {}
            x = details.get("x")
            y = details.get("y")
            if x is None or y is None:
                continue
            click_coords_by_screenshot.setdefault(screenshot, []).append((x, y))

    for rel in sorted(screenshot_paths):
        src = source_session_dir / rel
        dst = workflow_dir / rel
        if rel in click_coords_by_screenshot:
            _annotate_clicks_on_image(src, dst, click_coords_by_screenshot[rel], style)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


def reflect_session(
    session_dir: str | os.PathLike[str],
    workflows_root: str | os.PathLike[str],
    *,
    style: ClickAnnotationStyle | None = None,
    clean_manifest_tail: bool = False,
) -> Path:
    """
    Convert a recording session dir into a reusable workflow folder.

    Creates: <workflows_root>/<session_dir.name>/
      - manifest.jsonl (cleaned)
      - metadata.json (copied)
      - screenshots/ (copied; click screenshots annotated)
    """
    style = style or ClickAnnotationStyle()

    session_dir_p = Path(session_dir)
    workflows_root_p = Path(workflows_root)

    manifest_src = session_dir_p / "manifest.jsonl"
    metadata_src = session_dir_p / "metadata.json"
    if not manifest_src.exists():
        raise FileNotFoundError(f"manifest.jsonl not found: {manifest_src}")
    if not metadata_src.exists():
        raise FileNotFoundError(f"metadata.json not found: {metadata_src}")

    workflows_root_p.mkdir(parents=True, exist_ok=True)
    workflow_dir = workflows_root_p / session_dir_p.name

    # Refresh reflect outputs for this session without deleting compiled artifacts
    # like step_cards.json / schema*.json that enable resumable compilation.
    if workflow_dir.exists():
        return workflow_dir

    workflow_dir.mkdir(parents=True, exist_ok=True)

    # Copy metadata.json verbatim
    shutil.copy2(metadata_src, workflow_dir / "metadata.json")

    # Load + transform manifest
    events = _read_jsonl(manifest_src)
    if clean_manifest_tail:
        if events:
            # Remove last line (click on Stop Recording)
            events = events[:-1]
        if events:
            # Remove action from new last line (click on AI Mime): represent as null action.
            events[-1]["action_type"] = None
            events[-1]["action_details"] = {}

    # Copy screenshots referenced by the *updated* manifest (annotate clicks)
    _copy_or_annotate_screenshots(
        source_session_dir=session_dir_p,
        workflow_dir=workflow_dir,
        events=events,
        style=style,
    )

    # Write updated manifest into workflow
    _write_jsonl(workflow_dir / "manifest.jsonl", events)
    return workflow_dir


def compile_schema_for_workflow_dir(
    workflow_dir: str | os.PathLike[str],
    model: str = "gpt-5-mini",
) -> dict[str, Any]:
    """
    Compile a parametrizable, coordinate-free schema into <workflow_dir>/schema.json.
    """
    return compile_workflow_schema(workflow_dir=workflow_dir, model=model)
