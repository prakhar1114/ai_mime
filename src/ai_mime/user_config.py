from __future__ import annotations

import os
from pathlib import Path

from llm_resolver import (
    CONFIG_ENV_VAR,
    ResolvedLLMConfig,
    ResolvedReflectConfig,
    ResolvedUserConfig,
    load_user_config as _load_resolver_user_config,
)

from .app_data import get_user_config_path


def load_user_config(*, repo_root: Path | None = None) -> ResolvedUserConfig:
    """Load AI Mime's app-owned LLM config through llm-resolver."""
    path = repo_root / "user_config.yml" if repo_root else get_user_config_path()
    os.environ[CONFIG_ENV_VAR] = str(path)
    return _load_resolver_user_config()


__all__ = [
    "ResolvedLLMConfig",
    "ResolvedReflectConfig",
    "ResolvedUserConfig",
    "load_user_config",
]
