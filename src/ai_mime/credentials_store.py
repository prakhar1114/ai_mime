"""Credential storage and scoping for AI Mime skills.

This module is the single chokepoint for reading and writing user credentials
(API keys, tokens, account emails) that generated skills need at runtime.

Design:
- A global per-user store (``credentials.json`` in the app data dir) is the
  source of truth. Keys are namespaced by service, e.g.
  ``{"jira": {"email": "...", "api_token": "...", "domain": "..."}}``.
- A skill ships only a *manifest* (``credentials.template.json`` in the skill
  root) that declares which service/keys it needs, with ``<FILL IN: ...>``
  placeholder values. Real values never travel with the skill.
- Writes to the global store go ONLY through :func:`scoped_merge`, which merges
  just the manifest-declared keys and leaves every other service untouched. The
  build agent never writes the global store directly — it only writes its own
  workflow-scoped values file, and trusted code merges it.
- At run time, ``AI_MIME_CREDENTIALS_PATH`` points at a scoped, read-only
  *projection* containing only the declared keys (see
  :func:`resolve_credentials_path`). Generated scripts read that file. Because
  the projection is produced by trusted code, a future password/encryption layer
  can live entirely here without changing any generated script.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from ai_mime import app_data

# Manifest shipped inside the skill (declares required keys, no real values).
MANIFEST_FILENAME = "credentials.template.json"
# Build-time values file the agent writes into the workflow's agent/ dir.
LOCAL_VALUES_FILENAME = "credentials.local.json"
# Stable, regenerated projection for installed-skill runs.
RUNTIME_PROJECTION_FILENAME = ".credentials.runtime.json"

_PLACEHOLDER_RE = re.compile(r"^\s*<.*>\s*$")


# ---------------------------------------------------------------------------
# Global store
# ---------------------------------------------------------------------------

def global_store_path() -> Path:
    # Resolved lazily (module attribute, not an imported name) so app_data can
    # import this module at top level without a circular-import failure.
    return app_data.APP_DATA_DIR / "credentials.json"


def read_global() -> dict[str, dict[str, Any]]:
    path = global_store_path()
    if not path.is_file():
        return {}
    # Let a corrupt store raise rather than silently returning {} — a silent
    # empty read would let the next write wipe real credentials.
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {}
    # Keep only well-formed service buckets.
    return {k: dict(v) for k, v in data.items() if isinstance(v, dict)}


def _write_global(data: dict[str, dict[str, Any]]) -> None:
    path = global_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Manifest parsing
# ---------------------------------------------------------------------------

def manifest_path(skill_dir: str | os.PathLike[str]) -> Path:
    return Path(skill_dir) / MANIFEST_FILENAME


def has_manifest(skill_dir: str | os.PathLike[str]) -> bool:
    return manifest_path(skill_dir).is_file()


def parse_manifest(skill_dir: str | os.PathLike[str]) -> dict[str, dict[str, str]]:
    """Returns ``{service: {key: description}}`` from the skill manifest.

    Raises ValueError if the manifest exists but is malformed.
    """
    path = manifest_path(skill_dir)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise ValueError(f"{MANIFEST_FILENAME} is not valid JSON: {e}") from e
    if not isinstance(raw, dict):
        raise ValueError(f"{MANIFEST_FILENAME} must be a JSON object of {{service: {{key: description}}}}")
    out: dict[str, dict[str, str]] = {}
    for service, keys in raw.items():
        if not isinstance(service, str) or not service.strip():
            raise ValueError(f"{MANIFEST_FILENAME} has an invalid service name: {service!r}")
        if not isinstance(keys, dict) or not keys:
            raise ValueError(f"{MANIFEST_FILENAME} service {service!r} must map to a non-empty object of keys")
        bucket: dict[str, str] = {}
        for key, descr in keys.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError(f"{MANIFEST_FILENAME} service {service!r} has an invalid key: {key!r}")
            bucket[key] = descr if isinstance(descr, str) else ""
        out[service] = bucket
    return out


def is_placeholder(value: Any) -> bool:
    """True for empty values or ``<FILL IN: ...>``-style placeholders."""
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    text = value.strip()
    return text == "" or bool(_PLACEHOLDER_RE.match(text))


def manifest_real_values(skill_dir: str | os.PathLike[str]) -> list[str]:
    """Returns ``service.key`` entries whose manifest value is a *real* (non
    placeholder) value — i.e. a credential leak that must never ship."""
    path = manifest_path(skill_dir)
    if not path.is_file():
        return []
    # A malformed manifest should fail packaging loudly, not pass the leak guard.
    raw = json.loads(path.read_text(encoding="utf-8"))
    leaks: list[str] = []
    if not isinstance(raw, dict):
        return leaks
    for service, keys in raw.items():
        if not isinstance(keys, dict):
            continue
        for key, value in keys.items():
            if not is_placeholder(value):
                leaks.append(f"{service}.{key}")
    return leaks


# ---------------------------------------------------------------------------
# Install-time fields and scoped writes
# ---------------------------------------------------------------------------

def install_fields(skill_dir: str | os.PathLike[str]) -> list[dict[str, str]]:
    """Flat list of credential fields for the install UI, prefilled from the
    global store where a value already exists."""
    manifest = parse_manifest(skill_dir)
    store = read_global()
    fields: list[dict[str, str]] = []
    for service, keys in manifest.items():
        existing = store.get(service) or {}
        for key, descr in keys.items():
            value = existing.get(key)
            fields.append(
                {
                    "service": service,
                    "key": key,
                    "description": descr or f"{service} {key}",
                    "value": value if isinstance(value, str) else "",
                }
            )
    return fields


def missing_required(skill_dir: str | os.PathLike[str]) -> list[str]:
    """``service.key`` entries declared by the manifest that still have no real
    value in the global store. Call after :func:`scoped_merge` to enforce that
    install collected every required credential."""
    manifest = parse_manifest(skill_dir)
    store = read_global()
    missing: list[str] = []
    for service, keys in manifest.items():
        existing = store.get(service) or {}
        for key in keys:
            if is_placeholder(existing.get(key)):
                missing.append(f"{service}.{key}")
    return missing


def scoped_merge(
    values: dict[str, dict[str, Any]],
    manifest: dict[str, dict[str, str]],
) -> None:
    """Merge ``values`` into the global store, restricted to the keys declared
    in ``manifest``. Services and keys outside the manifest are never touched.
    Empty / placeholder values are ignored so they cannot clobber real ones."""
    if not manifest:
        return
    store = read_global()
    changed = False
    for service, keys in manifest.items():
        incoming = values.get(service)
        if not isinstance(incoming, dict):
            continue
        bucket = dict(store.get(service) or {})
        for key in keys:
            if key not in incoming:
                continue
            value = incoming[key]
            if is_placeholder(value):
                continue
            if bucket.get(key) != value:
                bucket[key] = value
                changed = True
        if bucket:
            store[service] = bucket
    if changed:
        _write_global(store)


def merge_local_values_file(
    local_values_path: str | os.PathLike[str],
    skill_dir: str | os.PathLike[str],
) -> None:
    """Read a workflow-local values file (the agent's build-time creds) and
    scoped-merge it into the global store using the skill's manifest."""
    path = Path(local_values_path)
    if not path.is_file():
        return
    values = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(values, dict):
        return
    manifest = parse_manifest(skill_dir)
    scoped_merge(values, manifest)


# ---------------------------------------------------------------------------
# Runtime projection
# ---------------------------------------------------------------------------

def project(
    manifest: dict[str, dict[str, str]],
    dest: str | os.PathLike[str] | None = None,
) -> Path:
    """Write a scoped, read-only file containing only the manifest-declared keys
    that exist in the global store. Returns its path. When ``dest`` is None a
    private temp file is created (caller owns cleanup)."""
    store = read_global()
    scoped: dict[str, dict[str, Any]] = {}
    for service, keys in manifest.items():
        existing = store.get(service) or {}
        bucket = {key: existing[key] for key in keys if key in existing}
        scoped[service] = bucket
    if dest is None:
        fd, tmp_name = tempfile.mkstemp(prefix="ai-mime-creds-", suffix=".json")
        os.close(fd)
        dest_path = Path(tmp_name)
    else:
        dest_path = Path(dest)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_text(json.dumps(scoped, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(dest_path, 0o600)
    except OSError:
        pass
    return dest_path


def _find_skill_and_workflow(path: Path) -> tuple[Path | None, Path | None]:
    """Given a skill dir, a workflow dir, or a skill runtime root, return
    ``(skill_dir, workflow_dir)``. Either may be None if not found.

    Two layouts are recognized:
      - ``path`` is the skill dir itself (has SKILL.md / a credentials manifest).
        Its workflow is two levels up (``<workflow>/skills/<name>``).
      - ``path`` is a workflow dir containing ``skills/<name>/``.
    """
    looks_like_skill = (path / MANIFEST_FILENAME).is_file() or (path / "SKILL.md").is_file()
    if looks_like_skill:
        return path, path.parent.parent

    skills_root = path / "skills"
    if skills_root.is_dir():
        for child in sorted(skills_root.iterdir()):
            if (child / MANIFEST_FILENAME).is_file():
                return child, path

    return None, (path if path.exists() else None)


def credentials_mode_for(agent_mode: str | None) -> str | None:
    """Map an ``AgentRunRequest.mode`` to a credential-resolution mode for
    :func:`resolve_credentials_path` / ``workflow_runtime_env``."""
    if agent_mode == "build_skill_chat":
        return "build"
    if agent_mode == "replay_execution":
        return "run"
    return None


def resolve_credentials_path(
    workflow_dir: str | os.PathLike[str] | None,
    mode: str | None = None,
) -> str | None:
    """Resolve the file path to expose as ``AI_MIME_CREDENTIALS_PATH`` for a run.

    Returns None when there is nothing to inject (no target, or the skill
    declares no credentials). Otherwise the path depends on ``mode``:

    - ``"build"`` — the build agent's own values file
      (``agent/credentials.local.json``). Returned even if it does not exist
      yet, because the session env is fixed before the agent writes it.
    - ``"run"`` — an installed-skill run. Project the skill's declared keys from
      the global store into a fresh read-only file and return that.
    - ``None`` (auto) — reuse the build values file if it already exists, else
      fall back to a projection. (Note: we deliberately do NOT treat the
      presence of an ``agent/`` dir as a "build" signal — replay creates one too.)
    """
    if workflow_dir is None:
        return None

    skill_dir, workflow = _find_skill_and_workflow(Path(workflow_dir))
    if skill_dir is None or not has_manifest(skill_dir):
        return None

    # Directory that holds the build agent's values file and the run projection.
    base_dir = workflow if workflow is not None else skill_dir
    build_values_file = base_dir / "agent" / LOCAL_VALUES_FILENAME

    if mode == "build":
        return str(build_values_file)
    if mode is None and build_values_file.is_file():
        return str(build_values_file)

    # mode == "run", or auto with no build values file: project from the global store.
    # A malformed manifest raises here (already validated at packaging time).
    manifest = parse_manifest(skill_dir)
    return str(project(manifest, base_dir / RUNTIME_PROJECTION_FILENAME))
