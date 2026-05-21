"""Central path resolution for AI Mime.

This is the only module that reads ``sys._MEIPASS``.  All other modules
import path helpers from here.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

_BROWSER_HARNESS_REL = "harness/browser-harness"
_FROZEN_SYSTEM_PATHS = (
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
)


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------


def is_frozen() -> bool:
    """True when running inside a PyInstaller bundle."""
    return getattr(sys, "frozen", False)


def _repo_root() -> Path:
    """Repo root when running in development (not frozen).

    app_data.py lives at <repo>/src/ai_mime/app_data.py → parents[2] == <repo>.
    """
    return Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# APP_DATA_DIR  — where mutable user data lives at runtime
# ---------------------------------------------------------------------------

if is_frozen():
    APP_DATA_DIR: Path = Path(os.path.expanduser("~/Library/Application Support/AI Mime"))
else:
    APP_DATA_DIR = _repo_root()


# ---------------------------------------------------------------------------
# Path getters
# ---------------------------------------------------------------------------


def get_env_path() -> Path:
    """Path to the .env file."""
    return APP_DATA_DIR / ".env"


def get_user_config_path() -> Path:
    """Path to user_config.yml."""
    return APP_DATA_DIR / "user_config.yml"


def get_recordings_dir() -> Path:
    """Path to the recordings/ directory."""
    return APP_DATA_DIR / "recordings"


def get_workflows_dir() -> Path:
    """Path to the workflows/ directory."""
    return APP_DATA_DIR / "workflows"


def get_onboarding_done_path() -> Path:
    """Sentinel file written when onboarding completes."""
    return APP_DATA_DIR / ".onboarding_done"


def get_bundled_resource(rel: str) -> Path:
    """Resolve a read-only bundled resource.

    frozen  → sys._MEIPASS / rel
    dev     → <repo root> / rel
    """
    if is_frozen():
        return Path(sys._MEIPASS) / rel  # type: ignore[attr-defined]
    return _repo_root() / rel


def get_uv_path() -> Path:
    """Resolve uv for app-managed Python/workflow environments.

    Frozen builds use the uv binary bundled by PyInstaller. Development uses
    the developer's PATH so local runs keep using the existing toolchain.
    """
    if is_frozen():
        return get_bundled_resource("bin/uv")
    found = shutil.which("uv")
    if found:
        return Path(found)
    return Path("uv")


def get_managed_python_install_dir() -> Path:
    """Directory where packaged onboarding installs uv-managed Python."""
    return APP_DATA_DIR / "python"


def get_tool_dir() -> Path:
    """Directory where app-owned uv tool environments live."""
    return APP_DATA_DIR / "tools"


def get_tool_bin_dir() -> Path:
    """Directory where app-owned uv tool executable shims live."""
    return APP_DATA_DIR / "bin"


def get_uv_cache_dir() -> Path:
    """Directory where app-owned uv cache data lives."""
    return APP_DATA_DIR / "uv-cache"


def get_managed_browser_harness_path() -> Path:
    """Path to the packaged browser-harness console script."""
    return get_tool_bin_dir() / "browser-harness"


def get_bundled_browser_harness_dir() -> Path:
    """Resolve the packaged browser-harness source/resources directory."""
    return get_bundled_resource(_BROWSER_HARNESS_REL)


def _find_managed_python(install_dir: Path | None = None) -> Path | None:
    root = install_dir or get_managed_python_install_dir()
    candidates: list[Path] = []
    for pattern in (
        "*/bin/python3.12",
        "*/bin/python3",
        "*/bin/python",
        "bin/python3.12",
        "bin/python3",
        "bin/python",
    ):
        candidates.extend(root.glob(pattern))
    for candidate in sorted(candidates):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def get_python_path(workflow_dir: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the Python executable for workflow scripts.

    Existing workflow/skill virtualenvs take precedence. In frozen builds,
    fall back to the app-managed Python installed during onboarding. In
    development, use the current/system interpreter instead of managed Python.
    """
    if workflow_dir is not None:
        venv_python = Path(workflow_dir) / ".venv" / "bin" / "python"
        if venv_python.is_file() and os.access(venv_python, os.X_OK):
            return venv_python

    if is_frozen():
        managed = _find_managed_python()
        if managed is not None:
            return managed
        return get_managed_python_install_dir() / "bin" / "python3.12"

    executable = Path(sys.executable)
    if executable.is_file():
        return executable
    found = shutil.which("python3") or shutil.which("python")
    if found:
        return Path(found)
    return Path("python3")


def workflow_runtime_env(workflow_dir: str | os.PathLike[str] | None = None) -> dict[str, str]:
    """Environment entries exported to generated workflow scripts."""
    env = {
        "AI_MIME_UV_PATH": str(get_uv_path()),
        "AI_MIME_PYTHON_PATH": str(get_python_path(workflow_dir)),
        "AI_MIME_BROWSER_HARNESS_BIN": str(get_managed_browser_harness_path()),
        "UV_PYTHON_INSTALL_DIR": str(get_managed_python_install_dir()),
    }
    if is_frozen():
        # uv isolation (tool/cache dirs, no user config) and the sanitized PATH only
        # apply in the packaged app. In dev, APP_DATA_DIR is the repo root, so
        # redirecting these would pollute the working tree and the dev's uv cache.
        env["UV_TOOL_DIR"] = str(get_tool_dir())
        env["UV_TOOL_BIN_DIR"] = str(get_tool_bin_dir())
        env["UV_CACHE_DIR"] = str(get_uv_cache_dir())
        env["UV_NO_CONFIG"] = "1"
        tool_bin = str(get_tool_bin_dir())
        bundled_bin = str(get_bundled_resource("bin"))
        env["PATH"] = os.pathsep.join([tool_bin, bundled_bin, *_FROZEN_SYSTEM_PATHS])
        env["AI_MIME_BROWSER_SKILL_NAME"] = "browser"
        env["AI_MIME_BROWSER_SKILL_PATH"] = str(get_bundled_browser_harness_dir())
    return env


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

_DEFAULT_USER_CONFIG = """\
reflect:
  model: "gemini/gemini-3-pro-preview"
  api_base: "https://generativelanguage.googleapis.com/v1beta/openai/"
  api_key_env: "GEMINI_API_KEY"
  extra_kwargs: {}
  pass_a:
    model: "gemini/gemini-3-pro-preview"
    max_tokens: 2000
  pass_b:
    model: "gemini/gemini-3-pro-preview"
    max_tokens: 7000

replay:
  model: "gemini/gemini-3-flash-preview"
  api_base: "https://generativelanguage.googleapis.com/v1beta/openai/"
  api_key_env: "GEMINI_API_KEY"
"""


def bootstrap_data_dir() -> None:
    """Idempotent: create APP_DATA_DIR layout and seed defaults.

    Only writes user_config.yml / .env when they do not already exist.
    """
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    get_recordings_dir().mkdir(exist_ok=True)
    get_workflows_dir().mkdir(exist_ok=True)

    cfg_path = get_user_config_path()
    if not cfg_path.exists():
        cfg_path.write_text(_DEFAULT_USER_CONFIG, encoding="utf-8")

    env_path = get_env_path()
    if not env_path.exists():
        env_path.write_text("", encoding="utf-8")
