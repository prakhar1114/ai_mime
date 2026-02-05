"""Central path resolution for AI Mime.

This is the only module that reads ``sys._MEIPASS``.  All other modules
import path helpers from here.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


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
