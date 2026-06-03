from __future__ import annotations

import os
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

CONFIG_ENV_VAR = "AI_MIME_CONFIG_PATH"

ProviderName = Literal["anthropic", "openai", "custom"]

DEFAULT_ANTHROPIC_LLM_CONFIG: dict[str, Any] = {
    "runtime": {
        "model": "anthropic/claude-sonnet-4-6",
        "api_base": None,
        "api_key_env": "ANTHROPIC_API_KEY",
        "extra_kwargs": {},
    },
    "reflect": {
        "model": "anthropic/claude-sonnet-4-6",
        "api_base": None,
        "api_key_env": "ANTHROPIC_API_KEY",
        "extra_kwargs": {},
        "pass_a": {
            "model": None,
            "max_tokens": 2000,
        },
        "pass_b": {
            "model": None,
            "max_tokens": 7000,
        },
        "pass_c": {
            "model": "anthropic/claude-opus-4-8",
            "max_tokens": 7000,
        },
    },
    "agents": {
        "workspace_chat": {
            "model": "anthropic/claude-opus-4-8",
            "api_base": None,
            "api_key_env": "ANTHROPIC_API_KEY",
            "extra_kwargs": {},
        },
        "skill_build": {
            "model": "anthropic/claude-opus-4-8",
            "api_base": None,
            "api_key_env": "ANTHROPIC_API_KEY",
            "extra_kwargs": {},
        },
        "replay": {
            "model": "anthropic/claude-opus-4-8",
            "api_base": None,
            "api_key_env": "ANTHROPIC_API_KEY",
            "extra_kwargs": {},
        },
        "computer_use": {
            "model": "anthropic/claude-opus-4-8",
            "api_base": None,
            "api_key_env": "ANTHROPIC_API_KEY",
            "extra_kwargs": {},
        },
    },
}

DEFAULT_OPENAI_LLM_CONFIG: dict[str, Any] = {
    "runtime": {
        "model": "openai/gpt-5.4-mini",
        "api_base": None,
        "api_key_env": "OPENAI_API_KEY",
        "extra_kwargs": {},
    },
    "reflect": {
        "model": "openai/gpt-5.5",
        "api_base": None,
        "api_key_env": "OPENAI_API_KEY",
        "extra_kwargs": {},
        "pass_a": {
            "model": None,
            "max_tokens": 2000,
        },
        "pass_b": {
            "model": None,
            "max_tokens": 7000,
        },
        "pass_c": {
            "model": None,
            "max_tokens": 7000,
        },
    },
    "agents": {
        "workspace_chat": {
            "model": "openai/gpt-5.5",
            "api_base": None,
            "api_key_env": "OPENAI_API_KEY",
            "extra_kwargs": {},
        },
        "skill_build": {
            "model": "openai/gpt-5.5",
            "api_base": None,
            "api_key_env": "OPENAI_API_KEY",
            "extra_kwargs": {},
        },
        "replay": {
            "model": "openai/gpt-5.5",
            "api_base": None,
            "api_key_env": "OPENAI_API_KEY",
            "extra_kwargs": {},
        },
        "computer_use": {
            "model": "openai/gpt-5.5",
            "api_base": None,
            "api_key_env": "OPENAI_API_KEY",
            "extra_kwargs": {},
        },
    },
}

DEFAULT_LLM_CONFIG = DEFAULT_ANTHROPIC_LLM_CONFIG

DEFAULT_CONFIG: dict[str, Any] = {
    "config_version": 1,
    "provider": "anthropic",
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


class AgentModelsConfig(BaseModel):
    workspace_chat: LLMSectionConfig
    skill_build: LLMSectionConfig
    replay: LLMSectionConfig
    computer_use: LLMSectionConfig


class LLMConfigSections(BaseModel):
    runtime: LLMSectionConfig
    reflect: ReflectSectionConfig
    agents: AgentModelsConfig


class AgentSectionConfig(BaseModel):
    workspace_chat_runtime: str | None = None
    skill_build_runtime: str | None = None
    replay_runtime: str | None = None
    computer_use_runtime: str | None = None


class LLMResolverConfigFile(BaseModel):
    config_version: int = 1
    provider: ProviderName
    llm: LLMConfigSections | None = None
    agent: AgentSectionConfig | None = None


@dataclass(frozen=True)
class ResolvedLLMConfig:
    model: str
    api_base: str | None
    api_key_env: str | None
    extra_kwargs: dict[str, Any]
    provider: str
    agent_runtime: str


@dataclass(frozen=True)
class ResolvedReflectConfig(ResolvedLLMConfig):
    pass_a_model: str | None
    pass_b_model: str | None
    pass_c_model: str | None
    pass_a_max_tokens: int | None
    pass_b_max_tokens: int | None
    pass_c_max_tokens: int | None


@dataclass(frozen=True)
class ResolvedAgentConfigs:
    workspace_chat: ResolvedLLMConfig
    skill_build: ResolvedLLMConfig
    replay: ResolvedLLMConfig
    computer_use: ResolvedLLMConfig


@dataclass(frozen=True)
class ResolvedLLMConfigFile:
    provider: str
    agent_runtime: str
    runtime: ResolvedLLMConfig
    reflect: ResolvedReflectConfig
    agents: ResolvedAgentConfigs


@dataclass(frozen=True)
class ResolvedUserConfig:
    provider: str
    agent_runtime: str
    reflect: ResolvedReflectConfig
    runtime: ResolvedLLMConfig
    agents: ResolvedAgentConfigs


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


def _provider_default(provider: ProviderName) -> dict[str, Any]:
    if provider == "anthropic":
        return deepcopy(DEFAULT_ANTHROPIC_LLM_CONFIG)
    if provider == "openai":
        return deepcopy(DEFAULT_OPENAI_LLM_CONFIG)
    raise RuntimeError("custom provider does not have built-in LLM defaults.")


def runtime_for_model(provider: str, model: str, *, field_name: str = "model") -> str:
    model_text = model.strip()
    model_provider = model_text.split("/", 1)[0] if "/" in model_text else ""
    if model_provider == "anthropic" or model_text.startswith("claude-"):
        return "claude_code"
    if model_provider == "openai":
        return "codex_cli"
    if provider == "anthropic":
        return "claude_code"
    if provider == "openai":
        return "codex_cli"
    raise RuntimeError(f"provider=custom requires a provider-qualified {field_name} or an inferable model name.")


def runtime_model_name(model: str) -> str:
    provider, sep, name = model.partition("/")
    if sep and provider in {"anthropic", "openai"} and name:
        return name
    return model


def _validate_provider_shape(raw: dict[str, Any], cfg_file: LLMResolverConfigFile) -> None:
    if "provider" not in raw:
        raise RuntimeError("user_config.yml must set provider to one of: anthropic, openai, custom.")
    if cfg_file.config_version != 1:
        raise RuntimeError(f"Unsupported config_version={cfg_file.config_version}; expected 1.")
    if cfg_file.provider in ("anthropic", "openai"):
        if "llm" in raw:
            raise RuntimeError(f"provider={cfg_file.provider!r} uses built-in LLM defaults; remove the detailed llm block or set provider: custom.")
        if "agent" in raw:
            raise RuntimeError(f"provider={cfg_file.provider!r} uses a built-in agent runtime; remove the agent block or set provider: custom.")
    elif cfg_file.provider == "custom" and cfg_file.llm is None:
        raise RuntimeError("provider='custom' requires a detailed llm block with runtime and reflect sections.")


def _resolve_section(section: LLMSectionConfig, *, provider: str, agent_runtime: str) -> ResolvedLLMConfig:
    return ResolvedLLMConfig(
        model=section.model,
        api_base=_norm_opt_str(section.api_base),
        api_key_env=_norm_opt_str(section.api_key_env),
        extra_kwargs=dict(section.extra_kwargs or {}),
        provider=provider,
        agent_runtime=agent_runtime,
    )


def _resolve_reflect(section: ReflectSectionConfig, *, provider: str, agent_runtime: str) -> ResolvedReflectConfig:
    return ResolvedReflectConfig(
        model=section.model,
        api_base=_norm_opt_str(section.api_base),
        api_key_env=_norm_opt_str(section.api_key_env),
        extra_kwargs=dict(section.extra_kwargs or {}),
        provider=provider,
        agent_runtime=agent_runtime,
        pass_a_model=section.pass_a.model,
        pass_b_model=section.pass_b.model,
        pass_c_model=section.pass_c.model,
        pass_a_max_tokens=section.pass_a.max_tokens,
        pass_b_max_tokens=section.pass_b.max_tokens,
        pass_c_max_tokens=section.pass_c.max_tokens,
    )


def _resolve_agents(provider: ProviderName, cfg_file: LLMResolverConfigFile, agents: AgentModelsConfig) -> ResolvedAgentConfigs:
    overrides = cfg_file.agent or AgentSectionConfig()
    workspace_chat_runtime = _norm_opt_str(overrides.workspace_chat_runtime) or runtime_for_model(
        provider,
        agents.workspace_chat.model,
        field_name="llm.agents.workspace_chat.model",
    )
    skill_build_runtime = _norm_opt_str(overrides.skill_build_runtime) or runtime_for_model(
        provider,
        agents.skill_build.model,
        field_name="llm.agents.skill_build.model",
    )
    replay_runtime = _norm_opt_str(overrides.replay_runtime) or runtime_for_model(
        provider,
        agents.replay.model,
        field_name="llm.agents.replay.model",
    )
    computer_use_runtime = _norm_opt_str(overrides.computer_use_runtime) or runtime_for_model(
        provider,
        agents.computer_use.model,
        field_name="llm.agents.computer_use.model",
    )
    return ResolvedAgentConfigs(
        workspace_chat=_resolve_section(
            agents.workspace_chat,
            provider=provider,
            agent_runtime=workspace_chat_runtime,
        ),
        skill_build=_resolve_section(
            agents.skill_build,
            provider=provider,
            agent_runtime=skill_build_runtime,
        ),
        replay=_resolve_section(
            agents.replay,
            provider=provider,
            agent_runtime=replay_runtime,
        ),
        computer_use=_resolve_section(
            agents.computer_use,
            provider=provider,
            agent_runtime=computer_use_runtime,
        ),
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
    raw_provider = raw.get("provider")
    if raw_provider in ("anthropic", "openai") and "llm" in raw:
        raise RuntimeError(f"provider={raw_provider!r} uses built-in LLM defaults; remove the detailed llm block or set provider: custom.")
    if raw_provider in ("anthropic", "openai") and "agent" in raw:
        raise RuntimeError(f"provider={raw_provider!r} uses a built-in agent runtime; remove the agent block or set provider: custom.")
    merged = _deep_merge(DEFAULT_CONFIG, raw)
    try:
        cfg_file = LLMResolverConfigFile.model_validate(merged)
    except Exception as e:
        raise RuntimeError(f"Invalid LLM config at {path}: {e}") from e
    _validate_provider_shape(raw, cfg_file)
    if cfg_file.provider == "custom":
        if cfg_file.llm is None:
            raise RuntimeError("provider='custom' requires a detailed llm block with runtime and reflect sections.")
        llm = cfg_file.llm
    else:
        llm = LLMConfigSections.model_validate(_provider_default(cfg_file.provider))
    agents = _resolve_agents(cfg_file.provider, cfg_file, llm.agents)
    runtime_agent_runtime = runtime_for_model(cfg_file.provider, llm.runtime.model, field_name="llm.runtime.model")
    return ResolvedLLMConfigFile(
        provider=cfg_file.provider,
        agent_runtime=agents.workspace_chat.agent_runtime,
        runtime=_resolve_section(llm.runtime, provider=cfg_file.provider, agent_runtime=runtime_agent_runtime),
        reflect=_resolve_reflect(llm.reflect, provider=cfg_file.provider, agent_runtime=runtime_agent_runtime),
        agents=agents,
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


def get_computer_use_config() -> ResolvedLLMConfig:
    return load_llm_config().agents.computer_use


def get_agent_config(flow: str = "workspace_chat") -> ResolvedLLMConfig:
    cfg = load_llm_config()
    try:
        resolved = getattr(cfg.agents, flow)
    except AttributeError as e:
        raise RuntimeError(f"Unknown agent flow {flow!r}.") from e
    if not isinstance(resolved, ResolvedLLMConfig):
        raise RuntimeError(f"Agent flow {flow!r} is not a standard LLM section.")
    return resolved


def load_user_config() -> ResolvedUserConfig:
    cfg = load_llm_config()
    return ResolvedUserConfig(
        provider=cfg.provider,
        agent_runtime=cfg.agent_runtime,
        runtime=cfg.runtime,
        reflect=cfg.reflect,
        agents=cfg.agents,
    )
