from __future__ import annotations

import os
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

CONFIG_ENV_VAR = "AI_MIME_CONFIG_PATH"

DEFAULT_LLM_CONFIG: dict[str, Any] = {
    "runtime": {
        "model": "gemini/gemini-3-flash-preview",
        "api_base": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "api_key_env": "GEMINI_API_KEY",
        "extra_kwargs": {},
    },
    "reflect": {
        "model": "gemini/gemini-3-pro-preview",
        "api_base": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "api_key_env": "GEMINI_API_KEY",
        "extra_kwargs": {},
        "pass_a": {
            "model": "gemini/gemini-3-pro-preview",
            "max_tokens": 2000,
        },
        "pass_b": {
            "model": "gemini/gemini-3-pro-preview",
            "max_tokens": 7000,
        },
        "pass_c": {
            "model": "gemini/gemini-3-pro-preview",
            "max_tokens": 7000,
        },
    },
}

DEFAULT_CONFIG: dict[str, Any] = {
    "config_version": 1,
    "llm": DEFAULT_LLM_CONFIG,
}

DEFAULT_USER_CONFIG = yaml.safe_dump(DEFAULT_CONFIG, sort_keys=False)


class PassTokenConfig(BaseModel):
    model: str | None = Field(default=None, description="Optional per-pass model override.")
    max_tokens: int | None = Field(default=None, description="Optional max_tokens override.")


class LLMSectionConfig(BaseModel):
    model: str
    api_base: str | None = None
    api_key_env: str | None = None
    extra_kwargs: dict[str, Any] = Field(default_factory=dict)


class ReflectSectionConfig(LLMSectionConfig):
    pass_a: PassTokenConfig = Field(default_factory=PassTokenConfig)
    pass_b: PassTokenConfig = Field(default_factory=PassTokenConfig)
    pass_c: PassTokenConfig = Field(default_factory=PassTokenConfig)


class LLMConfigSections(BaseModel):
    runtime: LLMSectionConfig
    reflect: ReflectSectionConfig


class LLMResolverConfigFile(BaseModel):
    config_version: int = 1
    llm: LLMConfigSections


@dataclass(frozen=True)
class ResolvedLLMConfig:
    model: str
    api_base: str | None
    api_key_env: str | None
    extra_kwargs: dict[str, Any]


@dataclass(frozen=True)
class ResolvedReflectConfig(ResolvedLLMConfig):
    pass_a_model: str | None
    pass_b_model: str | None
    pass_c_model: str | None
    pass_a_max_tokens: int | None
    pass_b_max_tokens: int | None
    pass_c_max_tokens: int | None


@dataclass(frozen=True)
class ResolvedLLMConfigFile:
    runtime: ResolvedLLMConfig
    reflect: ResolvedReflectConfig


@dataclass(frozen=True)
class ResolvedUserConfig:
    reflect: ResolvedReflectConfig
    runtime: ResolvedLLMConfig


def _config_path_from_env() -> Path:
    raw = os.getenv(CONFIG_ENV_VAR)
    if raw is None or not raw.strip():
        raise RuntimeError(f"{CONFIG_ENV_VAR} is not set; cannot load LLM config.")
    path = Path(raw).expanduser()
    if not path.is_file():
        raise RuntimeError(f"{CONFIG_ENV_VAR} points to a missing config file: {path}")
    return path


def _norm_opt_str(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = deepcopy(value)
    return out


def _resolve_section(section: LLMSectionConfig) -> ResolvedLLMConfig:
    return ResolvedLLMConfig(
        model=section.model,
        api_base=_norm_opt_str(section.api_base),
        api_key_env=_norm_opt_str(section.api_key_env),
        extra_kwargs=dict(section.extra_kwargs or {}),
    )


def _resolve_reflect(section: ReflectSectionConfig) -> ResolvedReflectConfig:
    return ResolvedReflectConfig(
        model=section.model,
        api_base=_norm_opt_str(section.api_base),
        api_key_env=_norm_opt_str(section.api_key_env),
        extra_kwargs=dict(section.extra_kwargs or {}),
        pass_a_model=section.pass_a.model,
        pass_b_model=section.pass_b.model,
        pass_c_model=section.pass_c.model,
        pass_a_max_tokens=section.pass_a.max_tokens,
        pass_b_max_tokens=section.pass_b.max_tokens,
        pass_c_max_tokens=section.pass_c.max_tokens,
    )


def load_llm_config() -> ResolvedLLMConfigFile:
    path = _config_path_from_env()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise RuntimeError(f"Failed to parse LLM config at {path}: {e}") from e
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise RuntimeError(f"LLM config at {path} must contain a YAML mapping at the top level.")
    merged = _deep_merge(DEFAULT_CONFIG, raw)
    try:
        cfg_file = LLMResolverConfigFile.model_validate(merged)
    except Exception as e:
        raise RuntimeError(f"Invalid LLM config at {path}: {e}") from e
    if cfg_file.config_version != 1:
        raise RuntimeError(f"Unsupported LLM config_version={cfg_file.config_version}; expected 1.")
    return ResolvedLLMConfigFile(
        runtime=_resolve_section(cfg_file.llm.runtime),
        reflect=_resolve_reflect(cfg_file.llm.reflect),
    )


def get_llm_section(section: str = "runtime") -> ResolvedLLMConfig:
    cfg = load_llm_config()
    try:
        resolved = getattr(cfg, section)
    except AttributeError as e:
        raise RuntimeError(f"Unknown LLM config section {section!r}.") from e
    if not isinstance(resolved, ResolvedLLMConfig):
        raise RuntimeError(f"LLM config section {section!r} is not a standard LLM section.")
    return resolved


def get_reflect_config() -> ResolvedReflectConfig:
    return load_llm_config().reflect


def load_user_config() -> ResolvedUserConfig:
    cfg = load_llm_config()
    return ResolvedUserConfig(runtime=cfg.runtime, reflect=cfg.reflect)


