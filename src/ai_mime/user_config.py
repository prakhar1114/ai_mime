from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class PassTokenConfig(BaseModel):
    model: str | None = Field(default=None, description="Optional per-pass LiteLLM model override.")
    max_tokens: int | None = Field(default=None, description="Optional max_tokens override for this pass.")


class LLMSectionConfig(BaseModel):
    """
    OpenAI-compatible chat model config (routed via LiteLLM).
    """

    model: str = Field(description="LiteLLM model string, e.g. 'openai/gpt-5-mini' or 'ollama/llama3.1'.")
    api_base: str | None = Field(
        default=None,
        description="Optional OpenAI-compatible base URL (e.g. http://127.0.0.1:8000/v1 for vLLM).",
    )
    api_key_env: str | None = Field(
        default=None,
        description="Optional env var name that contains the API key to use for this section.",
    )
    api_key: str | None = Field(
        default=None,
        description="Optional literal API key (discouraged). If set, overrides api_key_env.",
    )
    extra_kwargs: dict[str, Any] = Field(
        default_factory=dict,
        description="Provider/model specific kwargs forwarded to LiteLLM completion call.",
    )

    def resolve_api_key(self) -> str | None:
        if isinstance(self.api_key, str) and self.api_key.strip():
            return self.api_key.strip()
        if isinstance(self.api_key_env, str) and self.api_key_env.strip():
            v = os.getenv(self.api_key_env.strip())
            if v is None or not str(v).strip():
                raise RuntimeError(
                    f"Missing API key env var {self.api_key_env!r} for model={self.model!r}. "
                    "Set it in your environment or remove api_key_env for local providers."
                )
            return str(v).strip()
        return None


class ReflectSectionConfig(LLMSectionConfig):
    pass_a: PassTokenConfig = Field(default_factory=PassTokenConfig)
    pass_b: PassTokenConfig = Field(default_factory=PassTokenConfig)


class UserConfigFile(BaseModel):
    reflect: ReflectSectionConfig
    replay: LLMSectionConfig


@dataclass(frozen=True)
class ResolvedLLMConfig:
    model: str
    api_base: str | None
    api_key: str | None
    extra_kwargs: dict[str, Any]


@dataclass(frozen=True)
class ResolvedReflectConfig(ResolvedLLMConfig):
    pass_a_model: str | None
    pass_b_model: str | None
    pass_a_max_tokens: int | None
    pass_b_max_tokens: int | None


@dataclass(frozen=True)
class ResolvedUserConfig:
    reflect: ResolvedReflectConfig
    replay: ResolvedLLMConfig


def _repo_root_from_this_file() -> Path:
    # user_config.py: <repo>/src/ai_mime/user_config.py -> parents[2] == <repo>
    return Path(__file__).resolve().parents[2]


def load_user_config(*, repo_root: Path | None = None) -> ResolvedUserConfig:
    """
    Load and validate repo-root user_config.yml.

    Location policy: repo root only.
    """
    root = repo_root or _repo_root_from_this_file()
    path = root / "user_config.yml"
    if not path.exists():
        raise RuntimeError(
            f"user_config.yml not found at repo root: {path}\n"
            "Create one to configure reflect + replay models."
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise RuntimeError(f"Failed to parse user_config.yml: {e}") from e
    if not isinstance(raw, dict):
        raise RuntimeError("user_config.yml must contain a YAML mapping at the top level.")

    cfg_file = UserConfigFile.model_validate(raw)

    reflect_api_key = cfg_file.reflect.resolve_api_key()
    replay_api_key = cfg_file.replay.resolve_api_key()

    reflect = ResolvedReflectConfig(
        model=cfg_file.reflect.model,
        api_base=cfg_file.reflect.api_base,
        api_key=reflect_api_key,
        extra_kwargs=dict(cfg_file.reflect.extra_kwargs or {}),
        pass_a_model=cfg_file.reflect.pass_a.model,
        pass_b_model=cfg_file.reflect.pass_b.model,
        pass_a_max_tokens=cfg_file.reflect.pass_a.max_tokens,
        pass_b_max_tokens=cfg_file.reflect.pass_b.max_tokens,
    )
    replay = ResolvedLLMConfig(
        model=cfg_file.replay.model,
        api_base=cfg_file.replay.api_base,
        api_key=replay_api_key,
        extra_kwargs=dict(cfg_file.replay.extra_kwargs or {}),
    )
    return ResolvedUserConfig(reflect=reflect, replay=replay)
