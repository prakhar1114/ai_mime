from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import queue as thread_queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import urllib.request
import uuid
import zipfile
from multiprocessing import Event, Process, Queue
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles


from ai_mime.reflect.runner import run_reflect_and_compile_schema
from ai_mime.screenshot import ScreenshotRecorder
from ai_mime.debug_log import log, log_server, open_server_log_file
from ai_mime.agent_runner import (
    AgentBusyError,
    WorkflowSkillBuildService,
    WorkspaceAgentChatService,
    validate_skill_package,
)
from ai_mime.app_data import is_frozen, workflow_runtime_env
from ai_mime.provider_settings import provider_settings_status, save_provider_settings

EDITOR_SERVER_PORT = 58838
DEFAULT_MARKETPLACE_MANIFEST_URL = "https://market.aimime.cc/manifest.json"
MARKETPLACE_MANIFEST_PATH_ENV = "AI_MIME_MARKETPLACE_MANIFEST_PATH"
_MAX_MARKETPLACE_MANIFEST_BYTES = 2 * 1024 * 1024
TASK_STATUSES: dict[str, dict[str, Any]] = {}


def _kill_processes_on_tcp_port(port: int) -> None:
    """Stop any process using this TCP port so a new editor server can bind (macOS/Linux: uses lsof)."""
    try:
        proc = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return
    raw = (proc.stdout or "").strip()
    if not raw:
        return
    ours = os.getpid()
    pids: list[int] = []
    for token in raw.replace("\n", " ").split():
        if token.isdigit():
            pid = int(token)
            if pid != ours:
                pids.append(pid)
    seen: set[int] = set()
    unique: list[int] = []
    for p in pids:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    if not unique:
        return
    print(
        f"[ai-mime] editor server port {port} in use; stopping PIDs {unique}",
        file=sys.stderr,
        flush=True,
    )
    for pid in unique:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                break
            except PermissionError:
                break
    time.sleep(0.15)


def _task_log(msg: str, *, exc_info: bool = False) -> None:
    print(f"[ai-mime dashboard] {msg}", file=sys.stderr, flush=True)
    log(f"Dashboard: {msg}", exc_info=exc_info)


def _workflows_root_from_env() -> Path:
    raw = (os.getenv("AI_MIME_WORKFLOWS_ROOT") or "").strip()
    if not raw:
        raise RuntimeError("Missing AI_MIME_WORKFLOWS_ROOT")
    p = Path(raw).expanduser()
    return p


def _recordings_root_from_env(workflows_root: Path) -> Path:
    raw = (os.getenv("AI_MIME_RECORDINGS_ROOT") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return workflows_root.parent / "recordings"


def _safe_task_id(task_id: str) -> str:
    if not task_id or "/" in task_id or "\\" in task_id or ".." in task_id:
        raise HTTPException(status_code=400, detail="Invalid task id")
    return task_id


def _safe_workflow_dir(workflows_root: Path, workflow_id: str) -> Path:
    # workflow_id is expected to be a folder name under workflows_root.
    if not workflow_id or "/" in workflow_id or "\\" in workflow_id or ".." in workflow_id:
        raise HTTPException(status_code=400, detail="Invalid workflow id")
    p = (workflows_root / workflow_id).resolve()
    root = workflows_root.resolve()
    if root not in p.parents and p != root:
        raise HTTPException(status_code=400, detail="Invalid workflow id")
    if not p.exists() or not p.is_dir():
        raise HTTPException(status_code=404, detail="Workflow not found")
    return p


def _safe_recording_dir(recordings_root: Path, task_id: str) -> Path:
    task_id = _safe_task_id(task_id)
    p = (recordings_root / task_id).resolve()
    root = recordings_root.resolve()
    if root not in p.parents and p != root:
        raise HTTPException(status_code=400, detail="Invalid task id")
    if not p.exists() or not p.is_dir():
        raise HTTPException(status_code=404, detail="Recording not found")
    return p


def _find_skill_dir(workflow_dir: Path) -> Path | None:
    """Return the per-task skill dir if a built skill is present.

    A skill is considered built when workflow_dir/skills/<slug>/run.sh exists
    and is executable.
    There is typically a single subdirectory; if there are multiple, prefer the
    most recently modified.
    """
    skills_root = workflow_dir / "skills"
    if not skills_root.is_dir():
        return None
    candidates: list[Path] = []
    for child in skills_root.iterdir():
        run_sh = child / "run.sh"
        if child.is_dir() and run_sh.is_file() and os.access(run_sh, os.X_OK):
            candidates.append(child)
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def _find_skill_dir_with_run_sh(workflow_dir: Path) -> Path | None:
    skills_root = workflow_dir / "skills"
    if not skills_root.is_dir():
        return None
    candidates: list[Path] = []
    for child in skills_root.iterdir():
        if child.is_dir() and (child / "run.sh").is_file():
            candidates.append(child)
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def _has_reflected_schema(workflow_dir: Path) -> bool:
    return (workflow_dir / "schema.json").exists()


def _has_optimized_plan(workflow_dir: Path) -> bool:
    return (workflow_dir / "optimized_plan.json").exists()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _sse_event(obj: dict[str, Any]) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def _parse_skill_progress_event(line: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(line)
    except Exception:
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("event"), str):
        return None
    return obj


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _direct_run_id() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8]


def _slugify_task_name(value: str, *, fallback: str = "direct-build") -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or fallback


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


_REQUIRED_IMPORT_SKILL_FILES = (
    "SKILL.md",
    "run.sh",
    "scripts/run.py",
    "inputs/inputs.example.json",
    "inputs/inputs.template.json",
    "references/fallback_plan.md",
)
_IMPORT_SKIP_DIRS = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".agent",
    "agent",
    "runs",
    "outputs",
}
_IMPORT_SKIP_FILES = {
    ".DS_Store",
    "step_cards.json",
    "plan_creation.json",
    "manifest.jsonl",
}
_IMPORT_SKIP_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".log",
    ".tmp",
    ".temp",
}
_IMPORT_WORKFLOW_SCREENSHOT_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
_MAX_IMPORT_FILE_BYTES = 50 * 1024 * 1024
_MAX_IMPORT_TOTAL_BYTES = 250 * 1024 * 1024
_MARKETPLACE_VENV_TIMEOUT_SECONDS = 600
_MARKETPLACE_VENV_LOG_TAIL_CHARS = 4000


def _safe_upload_relpath(filename: str) -> Path:
    raw = (filename or "").replace("\\", "/").strip()
    if not raw:
        raise ValueError("Uploaded file is missing a relative path")
    if raw.startswith("/") or raw.startswith("~"):
        raise ValueError(f"Unsafe upload path: {filename}")
    parts = [part for part in raw.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise ValueError(f"Unsafe upload path: {filename}")
    return Path(*parts)


def _strip_common_upload_root(stage_dir: Path) -> Path:
    entries = [p for p in stage_dir.iterdir() if p.name != "__MACOSX"]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return stage_dir


def _is_skill_dir(path: Path) -> bool:
    return path.is_dir() and all((path / rel).is_file() for rel in _REQUIRED_IMPORT_SKILL_FILES)


def _find_workflow_skill_dir(workflow_dir: Path) -> Path | None:
    skills_root = workflow_dir / "skills"
    if not skills_root.is_dir():
        return None
    candidates = [p for p in sorted(skills_root.iterdir()) if _is_skill_dir(p)]
    return candidates[0] if candidates else None


def _parse_skill_frontmatter_fields(skill_dir: Path) -> dict[str, str]:
    skill_md = skill_dir / "SKILL.md"
    try:
        text = skill_md.read_text(encoding="utf-8")
    except Exception:
        return {}
    m = re.match(r"\A---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return {}
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip().strip("\"'")
    return out


def _parse_skill_preconditions(skill_dir: Path) -> list[str]:
    skill_md = skill_dir / "SKILL.md"
    try:
        text = skill_md.read_text(encoding="utf-8")
    except Exception:
        return []
    m = re.search(r"^##\s+Preconditions:?\s*$([\s\S]*?)(?=^##\s+|\Z)", text, re.MULTILINE)
    if not m:
        return []
    out: list[str] = []
    for raw_line in m.group(1).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = re.sub(r"^[-*]\s+", "", line)
        if line:
            out.append(line)
    return out


def _chmod_run_sh(skill_dir: Path) -> None:
    run_sh = skill_dir / "run.sh"
    if run_sh.is_file():
        run_sh.chmod(run_sh.stat().st_mode | 0o755)


def _should_skip_import_rel(rel: Path, *, is_workflow: bool, inside_skill: bool, keep_skill_venv: bool) -> bool:
    parts = rel.parts
    if any(part == ".venv" for part in parts):
        return not (inside_skill and keep_skill_venv)
    if any(part in _IMPORT_SKIP_DIRS for part in parts):
        return True
    name = rel.name
    if name in _IMPORT_SKIP_FILES:
        return True
    if name.startswith(".") and name not in {".env"}:
        return True
    suffix = rel.suffix.lower()
    if suffix in _IMPORT_SKIP_SUFFIXES:
        return True
    if is_workflow and len(parts) == 1 and suffix in _IMPORT_WORKFLOW_SCREENSHOT_SUFFIXES:
        return True
    return False


def _copy_import_clean(
    *,
    src: Path,
    dst: Path,
    is_workflow: bool,
    skill_dir: Path | None,
) -> list[str]:
    removed: list[str] = []
    skill_dir_resolved = skill_dir.resolve() if skill_dir is not None else None
    keep_skill_venv = bool(skill_dir is not None and (skill_dir / "requirements.txt").is_file())
    for path in src.rglob("*"):
        if path.is_symlink():
            removed.append(path.relative_to(src).as_posix())
            continue
        rel = path.relative_to(src)
        inside_skill = False
        if skill_dir_resolved is not None:
            try:
                path.resolve().relative_to(skill_dir_resolved)
                inside_skill = True
            except ValueError:
                inside_skill = False
        if _should_skip_import_rel(
            rel,
            is_workflow=is_workflow,
            inside_skill=inside_skill,
            keep_skill_venv=keep_skill_venv,
        ):
            removed.append(rel.as_posix())
            if path.is_dir():
                continue
            continue
        target = dst / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    return sorted(set(removed))


def _workflow_id_for_import(display_name: str) -> str:
    return f"{_direct_run_id()}-{_slugify_task_name(display_name, fallback='imported-skill')}"


def _existing_workflow_names(workflows_root: Path) -> set[str]:
    names: set[str] = set()
    if not workflows_root.exists():
        return names
    for child in workflows_root.iterdir():
        if not child.is_dir():
            continue
        meta = _read_json(child / "metadata.json")
        name = meta.get("name") if isinstance(meta, dict) else None
        if isinstance(name, str) and name.strip():
            names.add(name.strip())
    return names


def _unique_workflow_display_name(workflows_root: Path, display_name: str) -> str:
    base = display_name.strip() or "Imported Skill"
    existing = _existing_workflow_names(workflows_root)
    if base not in existing:
        return base
    for i in range(2, 10_000):
        candidate = f"{base} ({i})"
        if candidate not in existing:
            return candidate
    raise ValueError("Could not allocate a unique workflow name")


def _detect_import_root(root: Path) -> tuple[str, Path]:
    if _is_skill_dir(root):
        return "skill", root
    workflow_skill = _find_workflow_skill_dir(root)
    if workflow_skill is not None:
        return "workflow", workflow_skill
    raise ValueError("Uploaded folder is not a valid AI Mime skill or workflow directory")


def _validate_import_package(skill_dir: Path, schema: dict[str, Any], optimized_plan: dict[str, Any]) -> None:
    _chmod_run_sh(skill_dir)
    validate_skill_package(skill_dir, schema, optimized_plan)


def _remove_generated_import_artifacts(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_dir() and path.name in {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}:
            shutil.rmtree(path, ignore_errors=True)
        elif path.is_file() and path.suffix.lower() in {".pyc", ".pyo", ".log", ".tmp", ".temp"}:
            with contextlib.suppress(Exception):
                path.unlink()


def _create_import_preview(stage_dir: Path) -> dict[str, Any]:
    original_root = _strip_common_upload_root(stage_dir / "original")
    detected_type, original_skill_dir = _detect_import_root(original_root)
    clean_root = stage_dir / "clean"
    if clean_root.exists():
        shutil.rmtree(clean_root)

    if detected_type == "skill":
        removed = _copy_import_clean(
            src=original_root,
            dst=clean_root,
            is_workflow=False,
            skill_dir=original_skill_dir,
        )
        clean_skill_dir = clean_root
        schema: dict[str, Any] = {}
        optimized_plan: dict[str, Any] = {}
    else:
        removed = _copy_import_clean(
            src=original_root,
            dst=clean_root,
            is_workflow=True,
            skill_dir=original_skill_dir,
        )
        clean_skill_dir = _find_workflow_skill_dir(clean_root)
        if clean_skill_dir is None:
            raise ValueError("Cleaned workflow does not contain a valid skill package")
        schema = _read_json(clean_root / "schema.json")
        optimized_plan = _read_json(clean_root / "optimized_plan.json")

    _validate_import_package(clean_skill_dir, schema, optimized_plan)
    _remove_generated_import_artifacts(clean_root)
    fields = _parse_skill_frontmatter_fields(clean_skill_dir)
    skill_name = fields.get("name") or clean_skill_dir.name
    display_name = fields.get("name") or clean_skill_dir.name
    workflow_meta = _read_json(clean_root / "metadata.json") if detected_type == "workflow" else {}
    if isinstance(workflow_meta.get("name"), str) and workflow_meta["name"].strip():
        display_name = workflow_meta["name"].strip()

    return {
        "detected_type": detected_type,
        "display_name": display_name,
        "skill_name": skill_name,
        "skill_dir": str(clean_skill_dir),
        "removed_preview": removed[:200],
        "warnings": (
            ["More than 200 generated or irrelevant files were omitted from this preview."]
            if len(removed) > 200
            else []
        ),
        "valid": True,
    }


def _install_import_stage(stage_info: dict[str, Any], workflows_root: Path) -> dict[str, Any]:
    stage_dir = Path(str(stage_info.get("stage_dir") or ""))
    clean_root = stage_dir / "clean"
    detected_type = str(stage_info.get("detected_type") or "")
    display_name = str(stage_info.get("display_name") or "Imported Skill").strip() or "Imported Skill"
    display_name = _unique_workflow_display_name(workflows_root, display_name)
    task_id = _workflow_id_for_import(display_name)
    workflow_dir = (workflows_root / task_id).resolve()
    root = workflows_root.resolve()
    if root not in workflow_dir.parents and workflow_dir != root:
        raise ValueError("Invalid import destination")
    if workflow_dir.exists():
        raise FileExistsError(f"Workflow already exists: {workflow_dir}")

    try:
        if detected_type == "skill":
            skill_slug = _slugify_task_name(str(stage_info.get("skill_name") or display_name), fallback="imported-skill")
            workflow_dir.mkdir(parents=True, exist_ok=False)
            _write_json(
                workflow_dir / "metadata.json",
                {
                    "name": display_name,
                    "description": "",
                    "source": "imported_skill",
                    "created_at": _utc_timestamp(),
                },
            )
            _write_json(workflow_dir / "schema.json", {})
            _write_json(workflow_dir / "optimized_plan.json", {})
            skill_dst = workflow_dir / "skills" / skill_slug
            shutil.copytree(clean_root, skill_dst)
            _chmod_run_sh(skill_dst)
        elif detected_type == "workflow":
            shutil.copytree(clean_root, workflow_dir)
            meta = _read_json(workflow_dir / "metadata.json")
            if not meta:
                meta = {"name": display_name, "description": ""}
            meta["name"] = display_name
            meta.setdefault("description", "")
            meta["source"] = meta.get("source") or "imported_workflow"
            meta["imported_at"] = _utc_timestamp()
            _write_json(workflow_dir / "metadata.json", meta)
            if not (workflow_dir / "schema.json").exists():
                _write_json(workflow_dir / "schema.json", {})
            if not (workflow_dir / "optimized_plan.json").exists():
                _write_json(workflow_dir / "optimized_plan.json", {})
            skill_dir = _find_workflow_skill_dir(workflow_dir)
            if skill_dir is None:
                raise ValueError("Installed workflow does not contain a valid skill package")
            _chmod_run_sh(skill_dir)
        else:
            raise ValueError("Unknown staged import type")
    except Exception:
        if workflow_dir.exists():
            shutil.rmtree(workflow_dir, ignore_errors=True)
        raise

    return {"task_id": task_id, "workflow_dir": str(workflow_dir)}


def _resolve_marketplace_url(base_url: str, value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Marketplace item is missing {field}")
    resolved = urllib.parse.urljoin(base_url, value.strip())
    parsed = urllib.parse.urlparse(resolved)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"Marketplace item has an invalid {field}")
    return resolved


def _marketplace_manifest_dir_override() -> Path | None:
    raw = (os.getenv(MARKETPLACE_MANIFEST_PATH_ENV) or "").strip()
    if not raw:
        return None
    root = Path(raw).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"{MARKETPLACE_MANIFEST_PATH_ENV} must point to a marketplace directory")
    return root


def _resolve_marketplace_local_file(root: Path, value: Any, *, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Marketplace item is missing {field}")
    raw = value.strip().replace("\\", "/")
    if raw.startswith("/") or raw.startswith("~"):
        raise ValueError(f"Marketplace item has an unsafe {field}")
    parts = [part for part in raw.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise ValueError(f"Marketplace item has an unsafe {field}")
    path = (root / Path(*parts)).resolve()
    if root not in path.parents and path != root:
        raise ValueError(f"Marketplace item has an unsafe {field}")
    if not path.is_file():
        raise ValueError(f"Marketplace item {field} file not found")
    return path


def _fetch_url_bytes(url: str, *, max_bytes: int, label: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "AI-Mime/marketplace"})
    try:
        with urllib.request.urlopen(req, timeout=20) as response:  # nosec B310 - URL is configured or manifest-derived.
            out = bytearray()
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                out.extend(chunk)
                if len(out) > max_bytes:
                    raise ValueError(f"{label} is too large")
            return bytes(out)
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"Failed to fetch {label}: {e}") from e


def _read_file_bytes(path: Path, *, max_bytes: int, label: str) -> bytes:
    try:
        size = path.stat().st_size
        if size > max_bytes:
            raise ValueError(f"{label} is too large")
        return path.read_bytes()
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"Failed to read {label}: {e}") from e


def _fetch_marketplace_manifest() -> tuple[str, dict[str, Any], Path | None]:
    manifest_root = _marketplace_manifest_dir_override()
    if manifest_root is not None:
        manifest_ref = str((manifest_root / "manifest.json").resolve())
        raw = _read_file_bytes(manifest_root / "manifest.json", max_bytes=_MAX_MARKETPLACE_MANIFEST_BYTES, label="marketplace manifest")
    else:
        manifest_ref = DEFAULT_MARKETPLACE_MANIFEST_URL
        raw = _fetch_url_bytes(manifest_ref, max_bytes=_MAX_MARKETPLACE_MANIFEST_BYTES, label="marketplace manifest")
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise ValueError(f"Marketplace manifest is not valid JSON: {e}") from e
    if not isinstance(manifest, dict):
        raise ValueError("Marketplace manifest must be a JSON object")
    return manifest_ref, manifest, manifest_root


def _normalize_marketplace_item(item: Any, *, manifest_ref: str, manifest_root: Path | None) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError("Marketplace manifest item must be an object")
    item_id = item.get("id")
    name = item.get("name")
    if not isinstance(item_id, str) or not item_id.strip():
        raise ValueError("Marketplace item is missing id")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"Marketplace item {item_id!r} is missing name")
    package_path: str | None = None
    if manifest_root is not None:
        package_file = _resolve_marketplace_local_file(manifest_root, item.get("package_url"), field="package_url")
        package_url = item.get("package_url").strip()
        package_path = str(package_file)
    else:
        package_url = _resolve_marketplace_url(manifest_ref, item.get("package_url"), field="package_url")
    icon_url = None
    if isinstance(item.get("icon"), str) and item["icon"].strip():
        if manifest_root is None:
            icon_url = _resolve_marketplace_url(manifest_ref, item["icon"], field="icon")
        else:
            with contextlib.suppress(ValueError):
                _resolve_marketplace_local_file(manifest_root, item["icon"], field="icon")
    sha256 = item.get("sha256")
    if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", sha256.strip()):
        raise ValueError(f"Marketplace item {item_id!r} is missing a valid sha256")
    tags = item.get("tags") if isinstance(item.get("tags"), list) else []
    out = {
        "id": item_id.strip(),
        "name": name.strip(),
        "description": str(item.get("description") or ""),
        "type": str(item.get("type") or "workflow"),
        "version": str(item.get("version") or ""),
        "author": str(item.get("author") or ""),
        "tags": [str(v) for v in tags if isinstance(v, (str, int, float))],
        "icon_url": icon_url,
        "package_url": package_url,
        "_package_path": package_path,
        "sha256": sha256.strip().lower(),
        "size_bytes": item.get("size_bytes") if isinstance(item.get("size_bytes"), int) else None,
        "entrypoint": str(item.get("entrypoint") or ""),
        "skill_name": str(item.get("skill_name") or ""),
    }
    if out["size_bytes"] is not None and out["size_bytes"] > _MAX_IMPORT_TOTAL_BYTES:
        raise ValueError(f"Marketplace item {item_id!r} is too large")
    return out


def _normalized_marketplace_manifest() -> dict[str, Any]:
    manifest_ref, manifest, manifest_root = _fetch_marketplace_manifest()
    raw_items = manifest.get("items")
    if not isinstance(raw_items, list):
        raise ValueError("Marketplace manifest must contain an items list")
    items = [_normalize_marketplace_item(item, manifest_ref=manifest_ref, manifest_root=manifest_root) for item in raw_items]
    return {
        "version": manifest.get("version"),
        "name": str(manifest.get("name") or "AI Mime Skills Marketplace"),
        "homepage": str(manifest.get("homepage") or ""),
        "updated_at": str(manifest.get("updated_at") or ""),
        "manifest_url": manifest_ref,
        "source": "local" if manifest_root is not None else "remote",
        "items": items,
    }


def _public_marketplace_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    public = dict(manifest)
    public["items"] = [
        {key: value for key, value in item.items() if not key.startswith("_")}
        for item in manifest.get("items", [])
        if isinstance(item, dict)
    ]
    return public


def _safe_zip_relpath(filename: str) -> Path:
    raw = (filename or "").replace("\\", "/").strip()
    if not raw:
        raise ValueError("Zip entry is missing a path")
    if raw.startswith("/") or raw.startswith("~"):
        raise ValueError(f"Unsafe zip path: {filename}")
    parts = [part for part in raw.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise ValueError(f"Unsafe zip path: {filename}")
    return Path(*parts)


def _extract_marketplace_zip(raw_zip: bytes, stage_dir: Path) -> None:
    original_dir = stage_dir / "original"
    original_dir.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    total_bytes = 0
    try:
        with zipfile.ZipFile(io.BytesIO(raw_zip)) as zf:
            for info in zf.infolist():
                rel = _safe_zip_relpath(info.filename)
                rel_key = rel.as_posix()
                mode = (info.external_attr >> 16) & 0o170000
                if mode == 0o120000:
                    raise ValueError(f"Zip entry is a symlink: {rel_key}")
                if rel_key in seen:
                    raise ValueError(f"Duplicate zip path: {rel_key}")
                seen.add(rel_key)
                if info.is_dir():
                    (original_dir / rel).mkdir(parents=True, exist_ok=True)
                    continue
                if info.file_size > _MAX_IMPORT_FILE_BYTES:
                    raise ValueError(f"Marketplace package file is too large: {rel_key}")
                total_bytes += info.file_size
                if total_bytes > _MAX_IMPORT_TOTAL_BYTES:
                    raise ValueError("Marketplace package is too large")
                dst = original_dir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info, "r") as src, dst.open("wb") as out:
                    shutil.copyfileobj(src, out)
    except zipfile.BadZipFile as e:
        raise ValueError("Marketplace package is not a valid zip file") from e
    if not any(original_dir.rglob("*")):
        raise ValueError("Marketplace package is empty")


def _create_marketplace_import_stage(item: dict[str, Any]) -> dict[str, Any]:
    package_path = item.get("_package_path")
    if isinstance(package_path, str) and package_path:
        raw_zip = _read_file_bytes(Path(package_path), max_bytes=_MAX_IMPORT_TOTAL_BYTES, label="marketplace package")
    else:
        raw_zip = _fetch_url_bytes(item["package_url"], max_bytes=_MAX_IMPORT_TOTAL_BYTES, label="marketplace package")
    expected_size = item.get("size_bytes")
    if isinstance(expected_size, int) and expected_size >= 0 and len(raw_zip) != expected_size:
        raise ValueError("Marketplace package size did not match manifest")
    actual_sha = hashlib.sha256(raw_zip).hexdigest()
    if actual_sha != item["sha256"]:
        raise ValueError("Marketplace package checksum did not match manifest")

    stage_dir = Path(tempfile.mkdtemp(prefix="ai-mime-marketplace-import-"))
    try:
        _extract_marketplace_zip(raw_zip, stage_dir)
        preview = _create_import_preview(stage_dir)
        return {
            "stage_dir": str(stage_dir),
            **preview,
            "created_at": time.monotonic(),
            "marketplace_item_id": item["id"],
        }
    except Exception:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise


def _tail_process_output(value: str | None) -> str:
    if not value:
        return ""
    value = value.strip()
    if len(value) <= _MARKETPLACE_VENV_LOG_TAIL_CHARS:
        return value
    return value[-_MARKETPLACE_VENV_LOG_TAIL_CHARS:]


def _run_marketplace_venv_command(cmd: list[str], *, cwd: Path, env: dict[str, str], label: str) -> None:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=_MARKETPLACE_VENV_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        output = _tail_process_output(e.stdout if isinstance(e.stdout, str) else "")
        detail = f"Marketplace dependency setup timed out during {label}."
        if output:
            detail += f"\n\n{output}"
        raise ValueError(detail) from e
    except Exception as e:
        raise ValueError(f"Marketplace dependency setup failed to start during {label}: {e}") from e

    if proc.returncode != 0:
        output = _tail_process_output(proc.stdout)
        detail = f"Marketplace dependency setup failed during {label} with exit code {proc.returncode}."
        if output:
            detail += f"\n\n{output}"
        raise ValueError(detail)


def _setup_marketplace_skill_venv(workflow_dir: Path, skill_dir: Path) -> None:
    requirements = skill_dir / "requirements.txt"
    if not requirements.is_file():
        return

    stale_venv = skill_dir / ".venv"
    if stale_venv.exists():
        shutil.rmtree(stale_venv, ignore_errors=True)

    runtime_env = workflow_runtime_env(workflow_dir)
    uv_path = runtime_env.get("AI_MIME_UV_PATH")
    python_path = runtime_env.get("AI_MIME_PYTHON_PATH")
    if not uv_path or not python_path:
        raise ValueError("Marketplace dependency setup requires AI_MIME_UV_PATH and AI_MIME_PYTHON_PATH")
    env = {**os.environ, **runtime_env}

    _run_marketplace_venv_command(
        [uv_path, "venv", ".venv", "--python", python_path],
        cwd=skill_dir,
        env=env,
        label="virtualenv creation",
    )
    _run_marketplace_venv_command(
        [uv_path, "pip", "install", "-r", "requirements.txt", "--python", ".venv/bin/python"],
        cwd=skill_dir,
        env=env,
        label="dependency installation",
    )

    venv_python = skill_dir / ".venv" / "bin" / "python"
    if not venv_python.is_file() or not os.access(venv_python, os.X_OK):
        raise ValueError("Marketplace dependency setup did not create an executable .venv/bin/python")


def _snapshot_asset_files(assets_dir: Path) -> dict[str, tuple[int, int]]:
    if not assets_dir.exists() or not assets_dir.is_dir():
        return {}
    out: dict[str, tuple[int, int]] = {}
    for path in assets_dir.rglob("*"):
        if not path.is_file():
            continue
        try:
            stat_result = path.stat()
            rel = path.relative_to(assets_dir).as_posix()
        except OSError:
            continue
        out[rel] = (stat_result.st_size, stat_result.st_mtime_ns)
    return out


def _changed_assets(before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]]) -> list[str]:
    return sorted(rel for rel, marker in after.items() if before.get(rel) != marker)


def _copy_run_assets(assets_dir: Path, run_dir: Path, changed: list[str]) -> list[str]:
    copied: list[str] = []
    if not changed:
        return copied
    run_assets_dir = run_dir / "assets"
    for rel in changed:
        src = assets_dir / rel
        if not src.is_file():
            continue
        dst = run_assets_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(rel)
    return copied


def _markdown_json(value: Any) -> str:
    return "```json\n" + json.dumps(value, indent=2, ensure_ascii=False) + "\n```"


def _markdown_asset_link(rel: str) -> str:
    href = "assets/" + rel.replace("\\", "/")
    escaped_href = href.replace(" ", "%20").replace(")", "%29")
    label = Path(rel).name or rel
    return f"- [{label}]({escaped_href})"


def _write_direct_run_markdown(
    *,
    data_path: Path,
    run_id: str,
    status: str,
    started_at: str,
    duration_ms: int | None,
    exit_code: int | None,
    params: dict[str, Any],
    outputs: dict[str, Any],
    asset_rels: list[str],
    error: str | None = None,
    log_lines: list[str] | None = None,
    cmd: list[str] | None = None,
) -> None:
    lines = [
        f"# Run {run_id}",
        "",
        f"- Status: {status}",
        f"- Started: {started_at}",
    ]
    if duration_ms is not None:
        lines.append(f"- Duration: {duration_ms} ms")
    if exit_code is not None:
        lines.append(f"- Exit code: {exit_code}")
    if cmd:
        lines.extend(["", "## Command Executed", "", "```bash", " ".join(cmd), "```"])
    lines.extend(["", "## Input", "", _markdown_json(params), "", "## Output", "", _markdown_json(outputs)])
    if asset_rels:
        lines.extend(["", "## Assets", ""])
        lines.extend(_markdown_asset_link(rel) for rel in asset_rels)
    if error:
        lines.extend(["", "## Error", "", error])
    if log_lines:
        lines.extend(["", "## Logs", "", "```", "\n".join(log_lines), "```"])
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _emit(queue: Any | None, obj: dict[str, Any]) -> None:
    if queue is None:
        return
    try:
        if hasattr(queue, "put_nowait"):
            queue.put_nowait(obj)
        else:
            queue.put(obj)
    except Exception:
        pass


def _run_reflect_task(
    session_dir: str,
    workflows_root: str,
    *,
    force: bool = False,
    event_queue: Any | None = None,
) -> None:
    run_reflect_and_compile_schema(
        session_dir,
        workflows_root=workflows_root,
        clean_manifest_tail=False,
        force=force,
        event_queue=event_queue,
        log_fn=lambda msg: _task_log(msg),
    )





class TaskRunner:
    def __init__(
        self,
        *,
        workflows_root: Path,
        recordings_root: Path,
        app_state: Any | None = None,
        app_command_queue: Any | None = None,
    ) -> None:
        self.workflows_root = workflows_root
        self.recordings_root = recordings_root
        self.app_state = app_state
        self.app_command_queue = app_command_queue
        self._lock = threading.Lock()
        self._states: dict[str, dict[str, Any]] = {}
        self._reflect_processes: dict[str, tuple[Process, Queue]] = {}

    def list_tasks(self) -> list[dict[str, Any]]:
        with self._lock:
            self._refresh_locked()
            task_ids = self._discover_task_ids_locked()
            return [self._task_row_locked(task_id) for task_id in sorted(task_ids, reverse=True)]

    def get_status(self, task_id: str) -> dict[str, Any]:
        task_id = _safe_task_id(task_id)
        with self._lock:
            self._refresh_locked()
            if task_id not in self._discover_task_ids_locked() and task_id not in self._states:
                raise HTTPException(status_code=404, detail="Task not found")
            return self._task_row_locked(task_id)

    def start_reflect(self, task_id: str, *, force: bool = False) -> dict[str, Any]:
        task_id = _safe_task_id(task_id)
        _task_log(f"reflect requested: task_id={task_id} force={force}")
        with self._lock:
            self._refresh_locked()
            if task_id in self._reflect_processes:
                _task_log(f"reflect requested while already running: task_id={task_id}")
                return self._task_row_locked(task_id)

            recording_dir = (self.recordings_root / task_id).resolve()
            workflow_dir = (self.workflows_root / task_id).resolve()
            self._assert_under_root(recording_dir, self.recordings_root)
            self._assert_under_root(workflow_dir, self.workflows_root)
            has_recording_manifest = (recording_dir / "manifest.jsonl").exists()
            has_workflow_schema = (workflow_dir / "schema.json").exists()
            if not has_recording_manifest and not has_workflow_schema:
                _task_log(
                    f"reflect rejected: task_id={task_id} reason=missing_reflect_input "
                    f"recording_dir={recording_dir} workflow_dir={workflow_dir}"
                )
                raise HTTPException(status_code=400, detail="Recording manifest.jsonl or workflow schema.json not found")
            reflect_input_dir = workflow_dir if has_workflow_schema else recording_dir
            q: Queue = Queue()
            p = Process(
                target=_run_reflect_task,
                args=(str(reflect_input_dir), str(self.workflows_root)),
                kwargs={"force": force, "event_queue": q},
                daemon=True,
            )
            self._states[task_id] = {
                "status": "reflecting",
                "phase": "reflecting",
                "error": None,
                "progress": {"value": 5, "label": "Reflecting", "phase": "reflecting"},
            }
            self._reflect_processes[task_id] = (p, q)
            p.start()
            _task_log(f"reflect process started: task_id={task_id} pid={p.pid} input_dir={reflect_input_dir}")
            return self._task_row_locked(task_id)



    def delete_task(self, task_id: str) -> dict[str, Any]:
        task_id = _safe_task_id(task_id)
        with self._lock:
            self._refresh_locked()
            if task_id in self._reflect_processes:
                raise HTTPException(status_code=409, detail="Cannot delete while reflection is running")

            workflow_dir = (self.workflows_root / task_id).resolve()
            recording_dir = (self.recordings_root / task_id).resolve()
            self._assert_under_root(workflow_dir, self.workflows_root)
            self._assert_under_root(recording_dir, self.recordings_root)
            existed = False
            self._states[task_id] = {"status": "deleting", "phase": "deleting", "error": None}
            for path in (workflow_dir, recording_dir):
                if path.exists():
                    existed = True
                    if not path.is_dir():
                        raise HTTPException(status_code=400, detail=f"Refusing to delete non-directory: {path}")
                    shutil.rmtree(path)
            self._states.pop(task_id, None)
            if not existed:
                raise HTTPException(status_code=404, detail="Task not found")
            return {"ok": True, "task_id": task_id}

    def _discover_task_ids_locked(self) -> set[str]:
        task_ids: set[str] = set()
        for root in (self.workflows_root, self.recordings_root):
            if not root.exists() or not root.is_dir():
                continue
            for p in root.iterdir():
                if p.is_dir() and p.name != ".agent":
                    task_ids.add(p.name)
        task_ids.update(self._states.keys())
        for task_id in self._external_reflecting_locked():
            task_ids.add(task_id)
        return task_ids

    def _task_row_locked(self, task_id: str) -> dict[str, Any]:
        workflow_dir = self.workflows_root / task_id
        recording_dir = self.recordings_root / task_id
        has_workflow = workflow_dir.exists() and workflow_dir.is_dir()
        has_recording = recording_dir.exists() and recording_dir.is_dir()
        has_recording_manifest = has_recording and (recording_dir / "manifest.jsonl").exists()
        has_schema = has_workflow and _has_reflected_schema(workflow_dir)
        has_optimized_plan = has_workflow and _has_optimized_plan(workflow_dir)
        meta = _read_json(workflow_dir / "metadata.json") if has_workflow else _read_json(recording_dir / "metadata.json")
        display_name = str(meta.get("name") or task_id).strip() if isinstance(meta, dict) else task_id
        state = dict(self._states.get(task_id) or {})
        external_reflecting = self._external_reflecting_locked()
        if task_id in external_reflecting and task_id not in self._reflect_processes:
            phase = str(external_reflecting.get(task_id) or "reflecting")
            status = "reflecting" if phase == "reflecting" else "compiling"
            state = {
                "status": status,
                "phase": phase,
                "error": None,
                "progress": self._progress_from_phase(phase),
            }
        status = str(state.get("status") or "")
        if not status or status in {"ready", "pending_reflection"}:
            status = "ready" if (has_schema or has_optimized_plan) else "pending_reflection"
        if status == "reflecting" and state.get("phase") == "compiling":
            status = "compiling"
        active = status in {"reflecting", "compiling", "deleting"}
        skill_dir = _find_skill_dir(workflow_dir) if has_workflow else None
        has_skill = skill_dir is not None
        can_reflect = bool((has_recording_manifest or has_schema) and not active)
        can_replay = bool(has_skill and not active)
        return {
            "id": task_id,
            "display_name": display_name,
            "status": status,
            "phase": state.get("phase") or status,
            "error": state.get("error"),
            "progress": state.get("progress") or self._progress_from_status(status, state.get("phase")),
            "has_recording": has_recording,
            "has_workflow": has_workflow,
            "has_schema": has_schema,
            "has_optimized_plan": has_optimized_plan,
            "has_skill": has_skill,
            "skill_dir": str(skill_dir) if skill_dir else None,
            "can_reflect": can_reflect,
            "can_replay": can_replay,
            "can_delete": bool((has_recording or has_workflow) and not active),
            "workflow_dir": str(workflow_dir) if has_workflow else None,
            "recording_dir": str(recording_dir) if has_recording else None,
        }

    def app_status(self) -> dict[str, Any]:
        state = self._read_app_state()
        recording = state.get("recording") if isinstance(state.get("recording"), dict) else {}
        return {
            "is_recording": bool(recording.get("is_recording")),
            "recording_session": recording.get("session_name"),
            "recording_requested": bool(recording.get("requested")),
            "reflecting": self._external_reflecting_locked(),
        }

    def _read_app_state(self) -> dict[str, Any]:
        if self.app_state is None:
            return {}
        try:
            return dict(self.app_state)
        except Exception:
            return {}

    def _external_reflecting_locked(self) -> dict[str, str]:
        state = self._read_app_state()
        reflecting = state.get("reflecting")
        if not isinstance(reflecting, dict):
            return {}
        out: dict[str, str] = {}
        for key, value in reflecting.items():
            if isinstance(key, str) and key:
                out[key] = str(value or "reflecting")
        return out

    def _refresh_locked(self) -> None:
        for task_id, (proc, queue) in list(self._reflect_processes.items()):
            self._drain_reflect_events_locked(task_id, queue)
            if not proc.is_alive():
                self._drain_reflect_events_locked(task_id, queue)
                _proc, _queue = self._reflect_processes.pop(task_id)
                _proc.join(timeout=0.1)
                state = self._states.get(task_id) or {}
                if state.get("status") in {"reflecting", "compiling"}:
                    if proc.exitcode == 0 and (self.workflows_root / task_id / "schema.json").exists():
                        self._states[task_id] = {"status": "ready", "phase": "ready", "error": None}
                        _task_log(f"reflect process complete: task_id={task_id} pid={proc.pid}")
                    else:
                        self._states[task_id] = {
                            "status": "failed_reflection",
                            "phase": "failed_reflection",
                            "error": state.get("error") or f"Reflection exited with code {proc.exitcode}",
                        }
                        _task_log(f"reflect process failed: task_id={task_id} pid={proc.pid} exitcode={proc.exitcode} error={self._states[task_id].get('error')}")



    def _drain_reflect_events_locked(self, task_id: str, queue: Queue) -> None:
        while True:
            try:
                evt = queue.get_nowait()
            except Exception:
                break
            if not isinstance(evt, dict):
                continue
            et = evt.get("type")
            if et == "reflect_phase_started":
                phase = str(evt.get("phase") or "reflecting")
                self._states[task_id] = {
                    "status": phase,
                    "phase": phase,
                    "error": None,
                    "progress": self._progress_from_event(evt, fallback_phase=phase),
                }
                _task_log(f"reflect event: task_id={task_id} phase={phase}")
            elif et == "reflect_progress":
                phase = str(evt.get("phase") or "compiling")
                status = "reflecting" if phase == "reflecting" else "compiling"
                self._states[task_id] = {
                    "status": status,
                    "phase": phase,
                    "error": None,
                    "progress": self._progress_from_event(evt, fallback_phase=phase),
                }
                
                label = str(evt.get("label") or "")
                if label:
                    if self.app_command_queue is not None:
                        self.app_command_queue.put({
                            "type": "update_agent_status",
                            "status": label,
                            "needs_input": False,
                            "task_id": task_id,
                        })
                    TASK_STATUSES[task_id] = {"status": label, "needs_input": False}
                _task_log(f"reflect progress: task_id={task_id} phase={phase} progress={self._states[task_id]['progress'].get('value')}")
            elif et == "reflect_compile_done":
                self._states[task_id] = {
                    "status": "ready",
                    "phase": "optimized_plan_complete",
                    "error": None,
                    "progress": {"value": 100, "label": "Optimized plan", "phase": "optimized_plan_complete"},
                }
                _task_log(f"reflect event: task_id={task_id} done")
            elif et == "reflect_compile_failed":
                existing = self._states.get(task_id) or {}
                self._states[task_id] = {
                    "status": "failed_reflection",
                    "phase": "failed_reflection",
                    "error": str(evt.get("error") or "Reflection failed"),
                    "progress": existing.get("progress") or self._progress_from_phase("failed_reflection"),
                }
                _task_log(f"reflect event: task_id={task_id} failed error={self._states[task_id].get('error')}")



    @staticmethod
    def _assert_under_root(path: Path, root: Path) -> None:
        root_r = root.resolve()
        if root_r not in path.parents and path != root_r:
            raise HTTPException(status_code=400, detail="Invalid task path")

    @staticmethod
    def _progress_from_event(evt: dict[str, Any], *, fallback_phase: str) -> dict[str, Any]:
        value = evt.get("progress")
        try:
            value_i = int(value)
        except Exception:
            value_i = TaskRunner._progress_from_phase(fallback_phase)["value"]
        value_i = max(0, min(100, value_i))
        label = evt.get("label")
        if not isinstance(label, str) or not label.strip():
            label = TaskRunner._progress_from_phase(fallback_phase)["label"]
        phase = evt.get("phase")
        return {"value": value_i, "label": label, "phase": str(phase or fallback_phase)}

    @staticmethod
    def _progress_from_phase(phase: str) -> dict[str, Any]:
        mapping: dict[str, tuple[int, str]] = {
            "reflecting": (15, "Reflecting (this may take a minute)"),
            "compiling": (18, "Compiling"),
            "pass_a_started": (20, "Pass A"),
            "pass_a_complete": (33, "Pass A"),
            "pass_b_started": (45, "Pass B"),
            "pass_b_complete": (66, "Pass B"),
            "optimized_plan_started": (82, "Optimized plan"),
            "optimized_plan_complete": (100, "Optimized plan"),
            "ready": (100, "Optimized plan"),
            "failed_reflection": (0, "Reflection failed"),
        }
        value, label = mapping.get(phase, (0, str(phase or "Pending")))
        return {"value": value, "label": label, "phase": phase}

    @staticmethod
    def _progress_from_status(status: str, phase: Any) -> dict[str, Any]:
        if isinstance(phase, str) and phase:
            return TaskRunner._progress_from_phase(phase)
        return TaskRunner._progress_from_phase(status)


def create_app(
    *,
    workflows_root: Path | None = None,
    recordings_root: Path | None = None,
    app_command_queue: Any | None = None,
    app_state: Any | None = None,
    agent_chat_service: WorkspaceAgentChatService | None = None,
) -> FastAPI:
    workflows_root = workflows_root or _workflows_root_from_env()
    recordings_root = recordings_root or _recordings_root_from_env(workflows_root)
    task_runner = TaskRunner(
        workflows_root=workflows_root,
        recordings_root=recordings_root,
        app_state=app_state,
        app_command_queue=app_command_queue,
    )
    agent_service = agent_chat_service or WorkspaceAgentChatService()
    task_agent_services: dict[str, WorkspaceAgentChatService] = {}
    replay_agent_services: dict[str, WorkspaceAgentChatService] = {}
    skill_build_services: dict[str, WorkflowSkillBuildService] = {}
    import_staging: dict[str, dict[str, Any]] = {}

    _running_automations: dict[str, subprocess.Popen[str]] = {}

    app = FastAPI(title="AI Mime Task Dashboard", docs_url=None, redoc_url=None)

    web_dir = Path(__file__).parent / "web"
    app.mount("/static", StaticFiles(directory=str(web_dir)), name="static")

    @app.get("/", response_class=HTMLResponse)
    def root():
        return tasks_dashboard()

    @app.get("/tasks", response_class=HTMLResponse)
    def tasks_dashboard():
        index_path = web_dir / "tasks.html"
        if not index_path.exists():
            raise HTTPException(status_code=500, detail="Task dashboard UI not found")
        return HTMLResponse(content=index_path.read_text(encoding="utf-8"))

    @app.get("/agent", response_class=HTMLResponse)
    def agent_dashboard():
        index_path = web_dir / "agent.html"
        if not index_path.exists():
            raise HTTPException(status_code=500, detail="Agent UI not found")
        return HTMLResponse(content=index_path.read_text(encoding="utf-8"))

    @app.get("/marketplace", response_class=HTMLResponse)
    def marketplace_dashboard():
        index_path = web_dir / "marketplace.html"
        if not index_path.exists():
            raise HTTPException(status_code=500, detail="Marketplace UI not found")
        return HTMLResponse(content=index_path.read_text(encoding="utf-8"))

    @app.get("/reflect/{task_id}", response_class=HTMLResponse)
    def reflect_dashboard(task_id: str):
        _safe_task_id(task_id)
        task_runner.get_status(task_id)
        index_path = web_dir / "reflect.html"
        if not index_path.exists():
            raise HTTPException(status_code=500, detail="Reflect UI not found")
        html = index_path.read_text(encoding="utf-8").replace("__TASK_ID__", task_id)
        return HTMLResponse(content=html)

    def _validate_agent_session_id(session_id: str) -> None:
        if not session_id or "/" in session_id or "\\" in session_id or ".." in session_id:
            raise HTTPException(status_code=400, detail="Invalid session id")

    def _task_agent_service(task_id: str) -> WorkspaceAgentChatService:
        row = task_runner.get_status(task_id)
        workspace_raw = row.get("workflow_dir") or row.get("recording_dir")
        if not isinstance(workspace_raw, str) or not workspace_raw:
            raise HTTPException(status_code=404, detail="Task workspace not found")
        workspace = Path(workspace_raw)
        existing = task_agent_services.get(task_id)
        if existing is not None and existing.workspace_dir == workspace:
            return existing
        service = WorkspaceAgentChatService(workspace_dir=workspace)
        task_agent_services[task_id] = service
        return service

    def _replay_agent_service(task_id: str) -> WorkspaceAgentChatService:
        row = task_runner.get_status(task_id)
        workspace_raw = row.get("workflow_dir")
        skill_dir_raw = row.get("skill_dir")
        if not isinstance(workspace_raw, str) or not workspace_raw:
            raise HTTPException(status_code=404, detail="Workflow directory not found for task")
        if not isinstance(skill_dir_raw, str) or not skill_dir_raw:
            raise HTTPException(status_code=404, detail="Skill is not built for this task yet")
        workspace = Path(workspace_raw)
        existing = replay_agent_services.get(task_id)
        if existing is not None and existing.workspace_dir == workspace:
            return existing
        service = WorkspaceAgentChatService(
            workspace_dir=workspace,
            mode="replay_execution",
            agent_dir=workspace / "agent" / "replay",
        )
        replay_agent_services[task_id] = service
        return service

    async def _agent_chat_stream_response(
        service: WorkspaceAgentChatService,
        payload: dict[str, Any],
        task_id: str | None = None,
    ) -> StreamingResponse:
        message = payload.get("message")
        session_id = payload.get("session_id")
        model = payload.get("model")
        if not isinstance(message, str) or not message.strip():
            raise HTTPException(status_code=400, detail="message must be non-empty")
        if session_id is not None and not isinstance(session_id, str):
            raise HTTPException(status_code=400, detail="session_id must be a string or null")
        if model is not None and not isinstance(model, str):
            raise HTTPException(status_code=400, detail="model must be a string or null")

        try:
            event_iter = service.chat_stream(message=message, session_id=session_id, model=model)
        except AgentBusyError as e:
            raise HTTPException(status_code=409, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        async def _sse():
            if app_command_queue is not None:
                app_command_queue.put({
                    "type": "show_conversation_overlay",
                    "mode": service.mode,
                    "task_id": task_id or "",
                })
                default_status = "Starting workflow..."
                app_command_queue.put({
                    "type": "update_agent_status",
                    "status": default_status,
                    "needs_input": False,
                    "task_id": task_id or "",
                })
                if task_id:
                    TASK_STATUSES[task_id] = {"status": default_status, "needs_input": False}
                
            # Yield default status immediately
            yield f"data: {json.dumps({'event': 'agent_status', 'status': default_status, 'needs_input': False})}\n\n"

            message_accum = ""
            try:
                async for event in event_iter:
                    if app_command_queue is not None:
                        if event.get("event") == "text":
                            message_accum += event.get("text") or ""
                            snippet = message_accum
                            if len(snippet) > 500:
                                snippet = "..." + snippet[-497:]
                            app_command_queue.put({
                                "type": "update_conversation_overlay",
                                "text": snippet,
                            })
                        elif event.get("event") == "tool_use":
                            tool_name = event.get("name") or ""
                            if tool_name == "set_status":
                                tool_input = event.get("input") or {}
                                status_str = tool_input.get("status", "")
                                needs_input = tool_input.get("needs_input", False)
                                app_command_queue.put({
                                    "type": "update_agent_status",
                                    "status": status_str,
                                    "needs_input": needs_input,
                                    "task_id": task_id or "",
                                })
                                if task_id:
                                    TASK_STATUSES[task_id] = {"status": status_str, "needs_input": needs_input}
                                yield f"data: {json.dumps({'event': 'agent_status', 'status': status_str, 'needs_input': needs_input})}\n\n"
                            app_command_queue.put({
                                "type": "update_conversation_overlay",
                                "tool": tool_name,
                            })
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'event': 'error', 'message': str(e)})}\n\n"
            finally:
                if app_command_queue is not None:
                    app_command_queue.put({"type": "hide_conversation_overlay"})

        return StreamingResponse(
            _sse(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    def _agent_chat_response(service: WorkspaceAgentChatService, payload: dict[str, Any]) -> dict[str, Any]:
        message = payload.get("message")
        session_id = payload.get("session_id")
        if not isinstance(message, str) or not message.strip():
            raise HTTPException(status_code=400, detail="message must be non-empty")
        if session_id is not None and not isinstance(session_id, str):
            raise HTTPException(status_code=400, detail="session_id must be a string or null")
        model = payload.get("model")
        if model is not None and not isinstance(model, str):
            raise HTTPException(status_code=400, detail="model must be a string or null")
        try:
            return service.chat(message=message, session_id=session_id, model=model)
        except AgentBusyError as e:
            raise HTTPException(status_code=409, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/tasks")
    def api_list_tasks():
        return {"tasks": task_runner.list_tasks(), "app": task_runner.app_status()}

    @app.get("/api/app/status")
    def api_app_status():
        return task_runner.app_status()

    @app.post("/api/overlay/toggle")
    def api_overlay_toggle():
        if app_command_queue is not None:
            app_command_queue.put({"type": "toggle_conversation_overlay"})
        return {"ok": True}

    @app.get("/api/settings/provider")
    def api_provider_settings():
        return provider_settings_status()

    @app.post("/api/settings/provider")
    def api_update_provider_settings(payload: dict[str, Any] = Body(...)):
        nonlocal agent_service
        provider = payload.get("provider")
        if not isinstance(provider, str):
            raise HTTPException(status_code=400, detail="provider must be anthropic or openai")
        api_key = payload.get("api_key")
        if api_key is not None and not isinstance(api_key, str):
            raise HTTPException(status_code=400, detail="api_key must be a string or null")
        try:
            status = save_provider_settings(provider, api_key=api_key)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except RuntimeError as e:
            raise HTTPException(status_code=400, detail=str(e))

        agent_service = WorkspaceAgentChatService()
        task_agent_services.clear()
        replay_agent_services.clear()
        skill_build_services.clear()
        return status

    @app.get("/api/agent/sessions")
    def api_agent_sessions():
        return agent_service.status()

    @app.get("/api/agent/models")
    def api_agent_models():
        return agent_service.list_models()

    @app.post("/api/agent/sessions")
    def api_agent_create_session():
        return agent_service.create_session()

    @app.get("/api/agent/sessions/{session_id}/messages")
    def api_agent_session_messages(session_id: str):
        _validate_agent_session_id(session_id)
        try:
            return {"session_id": session_id, "messages": agent_service.load_messages(session_id)}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/agent/chat/stream")
    async def api_agent_chat_stream(payload: dict[str, Any] = Body(...)):
        return await _agent_chat_stream_response(agent_service, payload)

    @app.post("/api/agent/interrupt")
    def api_agent_interrupt():
        return {"interrupted": agent_service.interrupt()}

    @app.post("/api/agent/permission")
    def api_agent_permission(payload: dict[str, Any] = Body(...)):
        request_id = payload.get("request_id")
        decision = payload.get("decision")
        if not isinstance(request_id, str) or not request_id:
            raise HTTPException(status_code=400, detail="request_id must be a non-empty string")
        if decision not in ("allow", "allow_always", "deny"):
            raise HTTPException(status_code=400, detail="decision must be allow, allow_always, or deny")
        return {"resolved": agent_service.resolve_permission(request_id, decision)}

    @app.get("/api/agent/settings/bash_requires_approval")
    def api_agent_get_bash_requires_approval():
        return agent_service.bash_approval_setting()

    @app.post("/api/agent/settings/bash_requires_approval")
    def api_agent_set_bash_requires_approval(payload: dict[str, Any] = Body(...)):
        value = payload.get("value")
        if not isinstance(value, bool):
            raise HTTPException(status_code=400, detail="value must be boolean")
        agent_service.set_bash_requires_approval(value)
        return agent_service.bash_approval_setting()

    @app.post("/api/agent/chat")
    def api_agent_chat(payload: dict[str, Any] = Body(...)):
        return _agent_chat_response(agent_service, payload)

    @app.post("/api/recording/start")
    def api_start_recording():
        if app_command_queue is None:
            raise HTTPException(status_code=503, detail="Recording control is unavailable")
        status = task_runner.app_status()
        if status.get("is_recording") or status.get("recording_requested"):
            return {"ok": True, "queued": False, "message": "Recording already active or queued"}
        try:
            if task_runner.app_state is not None:
                state = task_runner._read_app_state()
                recording = state.get("recording") if isinstance(state.get("recording"), dict) else {}
                recording = dict(recording)
                recording["requested"] = True
                task_runner.app_state["recording"] = recording
            app_command_queue.put({"type": "start_recording"})
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to queue recording start: {e}")
        return {"ok": True, "queued": True}

    @app.post("/api/direct-build/workflows")
    def api_create_direct_build_workflow(payload: dict[str, Any] = Body(...)):
        name = payload.get("name")
        if not isinstance(name, str) or not name.strip():
            raise HTTPException(status_code=400, detail="name must be a non-empty string")
        display_name = name.strip()
        slug = _slugify_task_name(display_name)
        for _attempt in range(20):
            task_id = f"{_direct_run_id()}-{slug}"
            workflow_dir = (task_runner.workflows_root / task_id).resolve()
            task_runner._assert_under_root(workflow_dir, task_runner.workflows_root)
            if not workflow_dir.exists():
                break
        else:
            raise HTTPException(status_code=500, detail="Failed to allocate a unique workflow id")

        created_at = _utc_timestamp()
        try:
            workflow_dir.mkdir(parents=True, exist_ok=False)
            _write_json(
                workflow_dir / "metadata.json",
                {
                    "name": display_name,
                    "description": "",
                    "source": "direct_build",
                    "created_at": created_at,
                },
            )
            _write_json(workflow_dir / "schema.json", {})
            _write_json(workflow_dir / "optimized_plan.json", {})
        except Exception as e:
            with contextlib.suppress(Exception):
                if workflow_dir.exists():
                    shutil.rmtree(workflow_dir)
            raise HTTPException(status_code=500, detail=f"Failed to create direct build workflow: {e}")

        return {
            "task_id": task_id,
            "workflow_dir": str(workflow_dir),
            "created_at": created_at,
        }

    @app.post("/api/import/preview")
    async def api_import_preview(files: list[UploadFile] = File(...)):
        if not files:
            raise HTTPException(status_code=400, detail="Upload a skill or workflow folder")
        staging_id = uuid.uuid4().hex
        stage_dir = Path(tempfile.mkdtemp(prefix="ai-mime-import-"))
        original_dir = stage_dir / "original"
        original_dir.mkdir(parents=True, exist_ok=True)
        seen: set[str] = set()
        total_bytes = 0
        try:
            for upload in files:
                rel = _safe_upload_relpath(upload.filename or "")
                rel_key = rel.as_posix()
                if rel_key in seen:
                    raise ValueError(f"Duplicate uploaded path: {rel_key}")
                seen.add(rel_key)
                dst = original_dir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                file_bytes = 0
                with dst.open("wb") as f:
                    while True:
                        chunk = await upload.read(1024 * 1024)
                        if not chunk:
                            break
                        file_bytes += len(chunk)
                        total_bytes += len(chunk)
                        if file_bytes > _MAX_IMPORT_FILE_BYTES:
                            raise ValueError(f"Uploaded file is too large: {rel_key}")
                        if total_bytes > _MAX_IMPORT_TOTAL_BYTES:
                            raise ValueError("Uploaded folder is too large")
                        f.write(chunk)
            if not seen:
                raise ValueError("Uploaded folder is empty")

            preview = _create_import_preview(stage_dir)
            preview["staging_id"] = staging_id
            import_staging[staging_id] = {
                "stage_dir": str(stage_dir),
                **preview,
                "created_at": time.monotonic(),
            }
            return preview
        except ValueError as e:
            shutil.rmtree(stage_dir, ignore_errors=True)
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            shutil.rmtree(stage_dir, ignore_errors=True)
            raise HTTPException(status_code=500, detail=f"Failed to preview import: {e}")
        finally:
            for upload in files:
                with contextlib.suppress(Exception):
                    await upload.close()

    @app.post("/api/import/install")
    def api_import_install(payload: dict[str, Any] = Body(...)):
        staging_id = payload.get("staging_id")
        if not isinstance(staging_id, str) or not staging_id:
            raise HTTPException(status_code=400, detail="staging_id must be a non-empty string")
        stage_info = import_staging.get(staging_id)
        if not stage_info:
            raise HTTPException(status_code=404, detail="Import preview not found or expired")
        try:
            result = _install_import_stage(stage_info, task_runner.workflows_root)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to install import: {e}")
        finally:
            removed = import_staging.pop(staging_id, None)
            if removed is not None:
                shutil.rmtree(Path(str(removed.get("stage_dir") or "")), ignore_errors=True)
        return result

    @app.get("/api/marketplace/manifest")
    def api_marketplace_manifest():
        try:
            return _public_marketplace_manifest(_normalized_marketplace_manifest())
        except ValueError as e:
            raise HTTPException(status_code=502, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Failed to load marketplace manifest: {e}")

    @app.post("/api/marketplace/install")
    def api_marketplace_install(payload: dict[str, Any] = Body(...)):
        item_id = payload.get("item_id")
        if not isinstance(item_id, str) or not item_id.strip():
            raise HTTPException(status_code=400, detail="item_id must be a non-empty string")
        try:
            manifest = _normalized_marketplace_manifest()
            item = next((row for row in manifest["items"] if row["id"] == item_id.strip()), None)
            if item is None:
                raise HTTPException(status_code=404, detail="Marketplace item not found")
            stage_info = _create_marketplace_import_stage(item)
            try:
                result = _install_import_stage(stage_info, task_runner.workflows_root)
                workflow_dir = Path(str(result.get("workflow_dir") or ""))
                skill_dir = _find_workflow_skill_dir(workflow_dir)
                if skill_dir is None:
                    raise ValueError("Installed marketplace workflow does not contain a valid skill package")
                try:
                    _setup_marketplace_skill_venv(workflow_dir, skill_dir)
                except Exception:
                    if workflow_dir.exists():
                        shutil.rmtree(workflow_dir, ignore_errors=True)
                    raise
                return result
            finally:
                shutil.rmtree(Path(str(stage_info.get("stage_dir") or "")), ignore_errors=True)
        except HTTPException:
            raise
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to install marketplace item: {e}")

    @app.post("/api/app/quit")
    def api_quit_app():
        if app_command_queue is None:
            raise HTTPException(status_code=503, detail="App control is unavailable")
        for tid, proc in list(_running_automations.items()):
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                pass
            _running_automations.pop(tid, None)
        try:
            app_command_queue.put({"type": "quit_app"})
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to queue quit command: {e}")
        return {"ok": True}

    @app.post("/api/app/open-workflows")
    def api_open_workflows():
        if app_command_queue is None:
            raise HTTPException(status_code=503, detail="App control is unavailable")
        try:
            app_command_queue.put({"type": "open_workflows_directory"})
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to queue open workflows command: {e}")
        return {"ok": True}


    @app.get("/api/tasks/{task_id}/runs")
    def api_task_runs(task_id: str):
        _safe_task_id(task_id)
        row = task_runner.get_status(task_id)
        workflow_dir_raw = row.get("workflow_dir")
        if not isinstance(workflow_dir_raw, str) or not workflow_dir_raw:
            raise HTTPException(status_code=404, detail="Workflow directory not found for task")
        workflow_dir = Path(workflow_dir_raw)
        runs_dir = workflow_dir / "runs"
        if not runs_dir.exists() or not runs_dir.is_dir():
            return {"runs": []}
        
        runs = []
        for run_dir in sorted(runs_dir.iterdir(), key=lambda x: x.name, reverse=True):
            if not run_dir.is_dir():
                continue
            data_path = run_dir / "data.md"
            if not data_path.exists():
                continue
            
            run_id = run_dir.name
            status = "unknown"
            started = ""
            
            try:
                content = data_path.read_text(encoding="utf-8")
                # Parse Status
                status_match = re.search(r"-\s*Status:\s*([^\n]+)", content, re.IGNORECASE)
                if status_match:
                    status = status_match.group(1).strip()
                # Parse Started
                started_match = re.search(r"-\s*Started:\s*([^\n]+)", content, re.IGNORECASE)
                if started_match:
                    started = started_match.group(1).strip()
            except Exception:
                pass
                
            runs.append({
                "run_id": run_id,
                "status": status,
                "started": started
            })
        return {"runs": runs}

    @app.get("/api/tasks/{task_id}/runs/{run_id}")
    def api_task_run_detail(task_id: str, run_id: str):
        _safe_task_id(task_id)
        if ".." in run_id or "/" in run_id or "\\" in run_id:
            raise HTTPException(status_code=400, detail="Invalid run_id")
        row = task_runner.get_status(task_id)
        workflow_dir_raw = row.get("workflow_dir")
        if not isinstance(workflow_dir_raw, str) or not workflow_dir_raw:
            raise HTTPException(status_code=404, detail="Workflow directory not found for task")
        workflow_dir = Path(workflow_dir_raw)
        run_dir = workflow_dir / "runs" / run_id
        data_path = run_dir / "data.md"
        if not data_path.exists():
            raise HTTPException(status_code=404, detail="Run data not found")
        try:
            data_md = data_path.read_text(encoding="utf-8")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to read run data: {e}")
        return {
            "run_id": run_id,
            "data_md": data_md
        }

    @app.get("/api/tasks/{task_id}/status")
    def api_task_status(task_id: str):
        status = task_runner.get_status(task_id)
        if task_id in TASK_STATUSES:
            status["agent_status"] = TASK_STATUSES[task_id]
        return status

    @app.get("/api/tasks/{task_id}/reflect/status")
    def api_task_reflect_status(task_id: str):
        return task_runner.get_status(task_id)

    @app.get("/api/tasks/{task_id}/agent/sessions")
    def api_task_agent_sessions(task_id: str):
        return _task_agent_service(task_id).status()

    @app.get("/api/tasks/{task_id}/agent/models")
    def api_task_agent_models(task_id: str):
        return _task_agent_service(task_id).list_models()

    @app.post("/api/tasks/{task_id}/agent/sessions")
    def api_task_agent_create_session(task_id: str):
        return _task_agent_service(task_id).create_session()

    @app.get("/api/tasks/{task_id}/agent/sessions/{session_id}/messages")
    def api_task_agent_session_messages(task_id: str, session_id: str):
        _validate_agent_session_id(session_id)
        try:
            return {"session_id": session_id, "messages": _task_agent_service(task_id).load_messages(session_id)}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/tasks/{task_id}/agent/chat/stream")
    async def api_task_agent_chat_stream(task_id: str, payload: dict[str, Any] = Body(...)):
        return await _agent_chat_stream_response(_task_agent_service(task_id), payload, task_id=task_id)

    @app.post("/api/tasks/{task_id}/agent/interrupt")
    def api_task_agent_interrupt(task_id: str):
        return {"interrupted": _task_agent_service(task_id).interrupt()}

    @app.post("/api/tasks/{task_id}/agent/permission")
    def api_task_agent_permission(task_id: str, payload: dict[str, Any] = Body(...)):
        request_id = payload.get("request_id")
        decision = payload.get("decision")
        if not isinstance(request_id, str) or not request_id:
            raise HTTPException(status_code=400, detail="request_id must be a non-empty string")
        if decision not in ("allow", "allow_always", "deny"):
            raise HTTPException(status_code=400, detail="decision must be allow, allow_always, or deny")
        return {"resolved": _task_agent_service(task_id).resolve_permission(request_id, decision)}

    @app.get("/api/tasks/{task_id}/agent/settings/bash_requires_approval")
    def api_task_agent_get_bash_requires_approval(task_id: str):
        return _task_agent_service(task_id).bash_approval_setting()

    @app.post("/api/tasks/{task_id}/agent/settings/bash_requires_approval")
    def api_task_agent_set_bash_requires_approval(task_id: str, payload: dict[str, Any] = Body(...)):
        value = payload.get("value")
        if not isinstance(value, bool):
            raise HTTPException(status_code=400, detail="value must be boolean")
        service = _task_agent_service(task_id)
        service.set_bash_requires_approval(value)
        return service.bash_approval_setting()

    @app.post("/api/tasks/{task_id}/agent/chat")
    def api_task_agent_chat(task_id: str, payload: dict[str, Any] = Body(...)):
        return _agent_chat_response(_task_agent_service(task_id), payload)

    @app.get("/api/tasks/{task_id}/replay-agent/sessions")
    def api_replay_agent_sessions(task_id: str):
        return _replay_agent_service(task_id).status()

    @app.get("/api/tasks/{task_id}/replay-agent/models")
    def api_replay_agent_models(task_id: str):
        return _replay_agent_service(task_id).list_models()

    @app.post("/api/tasks/{task_id}/replay-agent/sessions")
    def api_replay_agent_create_session(task_id: str):
        return _replay_agent_service(task_id).create_session()

    @app.get("/api/tasks/{task_id}/replay-agent/sessions/{session_id}/messages")
    def api_replay_agent_session_messages(task_id: str, session_id: str):
        _validate_agent_session_id(session_id)
        try:
            return {"session_id": session_id, "messages": _replay_agent_service(task_id).load_messages(session_id)}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/tasks/{task_id}/replay-agent/chat/stream")
    async def api_replay_agent_chat_stream(task_id: str, payload: dict[str, Any] = Body(...)):
        return await _agent_chat_stream_response(_replay_agent_service(task_id), payload, task_id=task_id)

    @app.post("/api/tasks/{task_id}/replay-agent/interrupt")
    def api_replay_agent_interrupt(task_id: str):
        return {"interrupted": _replay_agent_service(task_id).interrupt()}

    @app.post("/api/tasks/{task_id}/replay-agent/permission")
    def api_replay_agent_permission(task_id: str, payload: dict[str, Any] = Body(...)):
        request_id = payload.get("request_id")
        decision = payload.get("decision")
        if not isinstance(request_id, str) or not request_id:
            raise HTTPException(status_code=400, detail="request_id must be a non-empty string")
        if decision not in ("allow", "allow_always", "deny"):
            raise HTTPException(status_code=400, detail="decision must be allow, allow_always, or deny")
        return {"resolved": _replay_agent_service(task_id).resolve_permission(request_id, decision)}

    @app.get("/api/tasks/{task_id}/replay-agent/settings/bash_requires_approval")
    def api_replay_agent_get_bash_requires_approval(task_id: str):
        return _replay_agent_service(task_id).bash_approval_setting()

    @app.post("/api/tasks/{task_id}/replay-agent/settings/bash_requires_approval")
    def api_replay_agent_set_bash_requires_approval(task_id: str, payload: dict[str, Any] = Body(...)):
        value = payload.get("value")
        if not isinstance(value, bool):
            raise HTTPException(status_code=400, detail="value must be boolean")
        service = _replay_agent_service(task_id)
        service.set_bash_requires_approval(value)
        return service.bash_approval_setting()

    def _skill_build_service(task_id: str) -> WorkflowSkillBuildService:
        row = task_runner.get_status(task_id)
        workspace_raw = row.get("workflow_dir")
        if not isinstance(workspace_raw, str) or not workspace_raw:
            raise HTTPException(status_code=404, detail="Workflow directory not found for task")
        workflow_dir = Path(workspace_raw)
        if not (workflow_dir / "optimized_plan.json").exists():
            raise HTTPException(
                status_code=409,
                detail="optimized_plan.json not present yet; finish reflect first",
            )
        existing = skill_build_services.get(task_id)
        if existing is not None and existing.workflow_dir == workflow_dir:
            return existing
        service = WorkflowSkillBuildService(workflow_dir=workflow_dir)
        skill_build_services[task_id] = service
        return service

    async def _skill_build_stream_response(
        service: WorkflowSkillBuildService,
        payload: dict[str, Any],
        task_id: str | None = None,
    ) -> StreamingResponse:
        message = payload.get("message")
        session_id = payload.get("session_id")
        model = payload.get("model")
        if not isinstance(message, str) or not message.strip():
            raise HTTPException(status_code=400, detail="message must be non-empty")
        if session_id is not None and not isinstance(session_id, str):
            raise HTTPException(status_code=400, detail="session_id must be a string or null")
        if model is not None and not isinstance(model, str):
            raise HTTPException(status_code=400, detail="model must be a string or null")
        try:
            event_iter = service.chat_stream(message=message, session_id=session_id, model=model)
        except AgentBusyError as e:
            raise HTTPException(status_code=409, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        async def _sse():
            if app_command_queue is not None:
                app_command_queue.put({
                    "type": "show_conversation_overlay",
                    "mode": "build_skill_chat",
                    "task_id": task_id or "",
                })
            message_accum = ""
            try:
                async for event in event_iter:
                    if app_command_queue is not None:
                        if event.get("event") == "text":
                            message_accum += event.get("text") or ""
                            snippet = message_accum
                            if len(snippet) > 500:
                                snippet = "..." + snippet[-497:]
                            app_command_queue.put({
                                "type": "update_conversation_overlay",
                                "text": snippet,
                            })
                        elif event.get("event") == "tool_use":
                            tool_name = event.get("name") or ""
                            app_command_queue.put({
                                "type": "update_conversation_overlay",
                                "tool": tool_name,
                            })
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'event': 'error', 'message': str(e)})}\n\n"
            finally:
                if app_command_queue is not None:
                    app_command_queue.put({"type": "hide_conversation_overlay"})

        return StreamingResponse(
            _sse(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/skill-build/{task_id}", response_class=HTMLResponse)
    def skill_build_page(task_id: str):
        _safe_task_id(task_id)
        row = task_runner.get_status(task_id)
        workflow_dir_raw = row.get("workflow_dir")
        workflow_dir = Path(workflow_dir_raw) if isinstance(workflow_dir_raw, str) and workflow_dir_raw else None
        if workflow_dir is None or not _has_optimized_plan(workflow_dir):
            index_path = web_dir / "reflect.html"
            if not index_path.exists():
                raise HTTPException(status_code=500, detail="Reflect UI not found")
            html = index_path.read_text(encoding="utf-8").replace("__TASK_ID__", task_id)
            return HTMLResponse(content=html)
        index_path = web_dir / "skill_build.html"
        if not index_path.exists():
            raise HTTPException(status_code=500, detail="Skill build UI not found")
        html = index_path.read_text(encoding="utf-8").replace("__TASK_ID__", task_id)
        return HTMLResponse(content=html)

    @app.get("/api/tasks/{task_id}/skill/inputs-template")
    def api_skill_inputs_template(task_id: str):
        _safe_task_id(task_id)
        row = task_runner.get_status(task_id)
        skill_dir_raw = row.get("skill_dir")
        if not isinstance(skill_dir_raw, str) or not skill_dir_raw:
            raise HTTPException(status_code=404, detail="Skill is not built for this task yet")
        skill_dir = Path(skill_dir_raw)
        template_path = skill_dir / "inputs" / "inputs.template.json"
        if not template_path.exists():
            raise HTTPException(status_code=404, detail=f"inputs.template.json not found at {template_path}")
        try:
            raw = template_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to parse inputs.template.json: {e}")
        if not isinstance(data, dict):
            raise HTTPException(status_code=500, detail="inputs.template.json must be a JSON object")
        example_path = skill_dir / "inputs" / "inputs.example.json"
        examples = _read_json(example_path)
        if not isinstance(examples, dict):
            examples = {}
        fields = _parse_skill_frontmatter_fields(skill_dir)
        return {
            "skill_dir": str(skill_dir),
            "template_path": str(template_path),
            "template": data,
            "examples": examples,
            "skill": {
                "name": fields.get("name") or skill_dir.name,
                "description": fields.get("description") or "",
                "preconditions": _parse_skill_preconditions(skill_dir),
            },
        }

    @app.post("/api/tasks/{task_id}/skill/open-folder")
    def api_open_skill_folder(task_id: str):
        _safe_task_id(task_id)
        if app_command_queue is None:
            raise HTTPException(status_code=503, detail="App control is unavailable")
        row = task_runner.get_status(task_id)
        workflow_dir_raw = row.get("workflow_dir")
        skill_dir_raw = row.get("skill_dir")
        if not isinstance(workflow_dir_raw, str) or not workflow_dir_raw:
            raise HTTPException(status_code=404, detail="Workflow directory not found for task")
        if not isinstance(skill_dir_raw, str) or not skill_dir_raw:
            raise HTTPException(status_code=404, detail="Skill is not built for this task yet")
        workflow_dir = Path(workflow_dir_raw).resolve()
        skill_dir = Path(skill_dir_raw).resolve()
        _safe_workflow_dir(task_runner.workflows_root, task_id)
        if workflow_dir not in skill_dir.parents:
            raise HTTPException(status_code=400, detail="Invalid skill directory")
        if not skill_dir.exists() or not skill_dir.is_dir():
            raise HTTPException(status_code=404, detail="Skill folder not found")
        try:
            app_command_queue.put({"type": "open_directory", "path": str(skill_dir)})
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to queue open skill folder command: {e}")
        return {"ok": True, "path": str(skill_dir)}

    @app.post("/api/tasks/{task_id}/skill/run/stream")
    def api_skill_run_stream(task_id: str, payload: dict[str, Any] | None = Body(default=None)):
        _safe_task_id(task_id)
        row = task_runner.get_status(task_id)
        workflow_dir_raw = row.get("workflow_dir")
        skill_dir_raw = row.get("skill_dir")
        if not isinstance(workflow_dir_raw, str) or not workflow_dir_raw:
            raise HTTPException(status_code=404, detail="Workflow directory not found for task")

        workflow_dir = Path(workflow_dir_raw).resolve()
        if isinstance(skill_dir_raw, str) and skill_dir_raw:
            skill_dir = Path(skill_dir_raw).resolve()
        else:
            fallback_skill_dir = _find_skill_dir_with_run_sh(workflow_dir)
            if fallback_skill_dir is None:
                raise HTTPException(status_code=404, detail="Skill is not built for this task yet")
            skill_dir = fallback_skill_dir.resolve()
        _safe_workflow_dir(task_runner.workflows_root, task_id)
        if workflow_dir not in skill_dir.parents:
            raise HTTPException(status_code=400, detail="Invalid skill directory")
        run_sh = skill_dir / "run.sh"
        if not run_sh.exists():
            raise HTTPException(status_code=404, detail=f"run.sh not found at {run_sh}")
        if not os.access(run_sh, os.X_OK):
            raise HTTPException(status_code=400, detail=f"run.sh is not executable: {run_sh}")

        params = payload.get("params") if isinstance(payload, dict) else None
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise HTTPException(status_code=400, detail="params must be a JSON object")

        def _stream():
            started = time.monotonic()
            started_at = _utc_timestamp()
            run_id = _direct_run_id()
            run_dir = workflow_dir / "runs" / run_id
            data_path = run_dir / "data.md"
            assets_dir = workflow_dir / "outputs" / "assets"
            run_dir.mkdir(parents=True, exist_ok=True)
            assets_before = _snapshot_asset_files(assets_dir)
            stdout_lines: list[str] = []
            stderr_lines: list[str] = []
            log_lines: list[str] = []
            final_outputs: dict[str, Any] = {}
            q: thread_queue.Queue[tuple[str, str | int | None]] = thread_queue.Queue()
            proc: subprocess.Popen[str] | None = None
            overlay_completed = False
            cmd: list[str] = []

            def _finish_run_log(
                *,
                status: str,
                exit_code: int | None,
                error: str | None = None,
            ) -> list[str]:
                duration_ms = int((time.monotonic() - started) * 1000)
                assets_after = _snapshot_asset_files(assets_dir)
                copied_assets = _copy_run_assets(
                    assets_dir,
                    run_dir,
                    _changed_assets(assets_before, assets_after),
                )
                _write_direct_run_markdown(
                    data_path=data_path,
                    run_id=run_id,
                    status=status,
                    started_at=started_at,
                    duration_ms=duration_ms,
                    exit_code=exit_code,
                    params=params,
                    outputs=final_outputs,
                    asset_rels=copied_assets,
                    error=error,
                    log_lines=log_lines,
                    cmd=cmd,
                )
                return copied_assets

            def _reader(pipe: Any, source: str) -> None:
                try:
                    for raw in iter(pipe.readline, ""):
                        q.put((source, raw.rstrip("\n")))
                finally:
                    try:
                        pipe.close()
                    except Exception:
                        pass
                    q.put((f"{source}_done", None))

            with tempfile.TemporaryDirectory(prefix="ai-mime-skill-run-") as td:
                inputs_path = Path(td) / "inputs.json"
                inputs_path.write_text(json.dumps(params, indent=2, ensure_ascii=False), encoding="utf-8")
                cmd = [str(run_sh), str(inputs_path)]
                if app_command_queue is not None:
                    app_command_queue.put({
                        "type": "show_automation_overlay",
                        "task_id": task_id,
                    })
                yield _sse_event({
                    "event": "started",
                    "skill_dir": str(skill_dir),
                    "inputs_path": str(inputs_path),
                    "command": "./run.sh",
                    "run_id": run_id,
                    "run_dir": str(run_dir),
                })
                try:
                    proc = subprocess.Popen(
                        cmd,
                        cwd=str(skill_dir),
                        env={**os.environ, **workflow_runtime_env(workflow_dir)},
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        bufsize=1,
                        start_new_session=True,
                    )
                    _running_automations[task_id] = proc
                except Exception as e:
                    message = f"Failed to start run.sh: {e}"
                    _finish_run_log(status="failed", exit_code=None, error=message)
                    if app_command_queue is not None:
                        app_command_queue.put({
                            "type": "update_automation_overlay",
                            "status": "failed",
                        })
                        overlay_completed = True
                    yield _sse_event({
                        "event": "error",
                        "message": message,
                        "run_id": run_id,
                        "run_dir": str(run_dir),
                    })
                    return

                assert proc.stdout is not None
                assert proc.stderr is not None
                threading.Thread(target=_reader, args=(proc.stdout, "stdout"), daemon=True).start()
                threading.Thread(target=_reader, args=(proc.stderr, "stderr"), daemon=True).start()

                done_streams: set[str] = set()
                try:
                    while len(done_streams) < 2:
                        try:
                            source, value = q.get(timeout=0.1)
                        except thread_queue.Empty:
                            if proc.poll() is not None and len(done_streams) >= 2:
                                break
                            continue
                        if source.endswith("_done"):
                            done_streams.add(source.removesuffix("_done"))
                            continue
                        line = "" if value is None else str(value)
                        if source == "stdout":
                            stdout_lines.append(line)
                            log_lines.append(line)
                        else:
                            stderr_lines.append(line)
                            log_lines.append(f"[stderr] {line}")
                        yield _sse_event({"event": source, "line": line})

                        progress = _parse_skill_progress_event(line)
                        if progress is None:
                            continue
                        outputs = progress.get("outputs")
                        if isinstance(outputs, dict):
                            source_event = str(progress.get("event") or "output")
                            if source_event == "workflow_done":
                                final_outputs = dict(outputs)
                                key = "workflow_done"
                            else:
                                key = str(progress.get("id") or source_event)
                            yield _sse_event({
                                "event": "output",
                                "key": key,
                                "value": outputs,
                                "source_event": source_event,
                            })
                    exit_code = proc.wait()
                    duration_ms = int((time.monotonic() - started) * 1000)
                    success = exit_code == 0
                    _finish_run_log(
                        status="success" if success else "failed",
                        exit_code=exit_code,
                        error=None if success else f"run.sh exited with code {exit_code}",
                    )
                    if app_command_queue is not None:
                        app_command_queue.put({
                            "type": "update_automation_overlay",
                            "status": "success" if success else "failed",
                        })
                        overlay_completed = True
                    yield _sse_event({
                        "event": "done",
                        "success": success,
                        "exit_code": exit_code,
                        "duration_ms": duration_ms,
                        "outputs": final_outputs,
                        "stdout_log": "\n".join(stdout_lines),
                        "stderr_log": "\n".join(stderr_lines),
                        "combined_log": "\n".join(log_lines),
                        "run_id": run_id,
                        "run_dir": str(run_dir),
                    })
                except GeneratorExit:
                    if proc is not None and proc.poll() is None:
                        proc.terminate()
                    raise
                except Exception as e:
                    message = f"Run stream failed: {e}"
                    _finish_run_log(status="failed", exit_code=None, error=message)
                    if app_command_queue is not None:
                        app_command_queue.put({
                            "type": "update_automation_overlay",
                            "status": "failed",
                        })
                        overlay_completed = True
                    yield _sse_event({
                        "event": "error",
                        "message": message,
                        "run_id": run_id,
                        "run_dir": str(run_dir),
                    })
                    return
                finally:
                    if proc is not None and proc.poll() is None:
                        try:
                            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                        except Exception:
                            pass
                        proc.terminate()
                    _running_automations.pop(task_id, None)
                    if not overlay_completed and app_command_queue is not None:
                        app_command_queue.put({"type": "hide_conversation_overlay"})

        return StreamingResponse(
            _stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/tasks/{task_id}/skill/kill")
    def api_kill_skill_run(task_id: str):
        _safe_task_id(task_id)
        proc = _running_automations.get(task_id)
        if not proc:
            return {"ok": False, "message": "No active skill run for this task"}
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to kill process group: {e}")
        return {"ok": True, "message": "Skill run terminated"}

    @app.get("/replay/{task_id}", response_class=HTMLResponse)
    def replay_page(task_id: str):
        _safe_task_id(task_id)
        row = task_runner.get_status(task_id)
        if not row.get("has_skill"):
            raise HTTPException(status_code=409, detail="Skill is not built for this task yet")
        index_path = web_dir / "replay.html"
        if not index_path.exists():
            raise HTTPException(status_code=500, detail="Replay UI not found")
        html = index_path.read_text(encoding="utf-8").replace("__TASK_ID__", task_id)
        return HTMLResponse(content=html)

    @app.get("/api/tasks/{task_id}/skill-build/sessions")
    def api_skill_build_sessions(task_id: str):
        return _skill_build_service(task_id).status()

    @app.get("/api/tasks/{task_id}/skill-build/models")
    def api_skill_build_models(task_id: str):
        return _skill_build_service(task_id).list_models()

    @app.post("/api/tasks/{task_id}/skill-build/sessions")
    def api_skill_build_create_session(task_id: str):
        return _skill_build_service(task_id).create_session()

    @app.get("/api/tasks/{task_id}/skill-build/sessions/{session_id}/messages")
    def api_skill_build_session_messages(task_id: str, session_id: str):
        _validate_agent_session_id(session_id)
        try:
            return {
                "session_id": session_id,
                "messages": _skill_build_service(task_id).load_messages(session_id),
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/tasks/{task_id}/skill-build/chat/stream")
    async def api_skill_build_chat_stream(task_id: str, payload: dict[str, Any] = Body(...)):
        return await _skill_build_stream_response(_skill_build_service(task_id), payload, task_id=task_id)

    @app.post("/api/tasks/{task_id}/skill-build/interrupt")
    def api_skill_build_interrupt(task_id: str):
        return {"interrupted": _skill_build_service(task_id).interrupt()}

    @app.post("/api/tasks/{task_id}/skill-build/permission")
    def api_skill_build_permission(task_id: str, payload: dict[str, Any] = Body(...)):
        request_id = payload.get("request_id")
        decision = payload.get("decision")
        if not isinstance(request_id, str) or not request_id:
            raise HTTPException(status_code=400, detail="request_id must be a non-empty string")
        if decision not in ("allow", "allow_always", "deny"):
            raise HTTPException(status_code=400, detail="decision must be allow, allow_always, or deny")
        return {"resolved": _skill_build_service(task_id).resolve_permission(request_id, decision)}

    @app.get("/api/tasks/{task_id}/skill-build/settings/bash_requires_approval")
    def api_skill_build_get_bash_approval(task_id: str):
        return _skill_build_service(task_id).bash_approval_setting()

    @app.post("/api/tasks/{task_id}/skill-build/settings/bash_requires_approval")
    def api_skill_build_bash_approval(task_id: str, payload: dict[str, Any] = Body(...)):
        value = payload.get("value")
        if not isinstance(value, bool):
            raise HTTPException(status_code=400, detail="value must be boolean")
        service = _skill_build_service(task_id)
        service.set_bash_requires_approval(value)
        return service.bash_approval_setting()

    @app.post("/api/tasks/{task_id}/skill-build/reset")
    def api_skill_build_reset(task_id: str):
        _skill_build_service(task_id).reset_terminal()
        return {"ok": True}

    @app.post("/api/tasks/{task_id}/reflect")
    def api_reflect_task(task_id: str, payload: dict[str, Any] | None = Body(default=None)):
        force = bool(payload.get("force")) if isinstance(payload, dict) else False
        return task_runner.start_reflect(task_id, force=force)



    @app.delete("/api/tasks/{task_id}")
    def api_delete_task(task_id: str):
        return task_runner.delete_task(task_id)

    @app.get("/health")
    def health():
        return {"ok": True}

    return app


# Fixed loopback port for a stable URL; re-enable if collisions with other local services matter.



# def _pick_free_port() -> int:
#     s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
#     s.bind(("127.0.0.1", 0))
#     _host, port = s.getsockname()
#     s.close()
#     return int(port)


def _run_uvicorn(
    host: str,
    port: int,
    workflows_root: str,
    recordings_root: str,
    app_command_queue: Any | None,
    app_state: Any | None,
) -> None:
    # Import inside the subprocess so the caller doesn't require fastapi/uvicorn
    # unless the editor is actually used.
    import uvicorn  # type: ignore[import-not-found]

    if is_frozen():
        log_server(f"Editor server: child process started, binding {host}:{port}")
    try:
        if is_frozen():
            with open_server_log_file() as server_log_file:
                with contextlib.redirect_stdout(server_log_file), contextlib.redirect_stderr(server_log_file):
                    _serve_uvicorn(
                        uvicorn,
                        host=host,
                        port=port,
                        workflows_root=workflows_root,
                        recordings_root=recordings_root,
                        app_command_queue=app_command_queue,
                        app_state=app_state,
                    )
        else:
            _serve_uvicorn(
                uvicorn,
                host=host,
                port=port,
                workflows_root=workflows_root,
                recordings_root=recordings_root,
                app_command_queue=app_command_queue,
                app_state=app_state,
            )
    except Exception as e:
        if is_frozen():
            log_server(f"Editor server: crashed on {host}:{port}: {e}", exc_info=True)
        raise


def _serve_uvicorn(
    uvicorn: Any,
    *,
    host: str,
    port: int,
    workflows_root: str,
    recordings_root: str,
    app_command_queue: Any | None,
    app_state: Any | None,
) -> None:
    print(
        f"[ai-mime] editor server starting on http://{host}:{port}",
        file=sys.stderr,
        flush=True,
    )
    app = create_app(
        workflows_root=Path(workflows_root),
        recordings_root=Path(recordings_root),
        app_command_queue=app_command_queue,
        app_state=app_state,
    )
    uvicorn.run(app, host=host, port=port, log_level="info", access_log=True)


def start_editor_server(
    *,
    workflows_root: Path,
    recordings_root: Path | None = None,
    app_command_queue: Any | None = None,
    app_state: Any | None = None,
) -> tuple[Process, int]:
    """
    Start the editor server in a subprocess and return (process, port).
    The server binds to 127.0.0.1 only.
    """
    os.environ["AI_MIME_WORKFLOWS_ROOT"] = str(workflows_root)
    recordings_root = recordings_root or workflows_root.parent / "recordings"
    os.environ["AI_MIME_RECORDINGS_ROOT"] = str(recordings_root)
    port = EDITOR_SERVER_PORT
    _kill_processes_on_tcp_port(port)
    p = Process(
        target=_run_uvicorn,
        args=(
            "127.0.0.1",
            port,
            str(workflows_root),
            str(recordings_root),
            app_command_queue,
            app_state,
        ),
        daemon=False,
    )
    p.start()
    if is_frozen():
        log_server(f"Editor server: launched on port {port} (pid {p.pid})")
    print(f"[ai-mime] editor server starting on http://127.0.0.1:{port}", file=sys.stderr)
    return p, port
