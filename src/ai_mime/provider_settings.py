from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Literal

import yaml

from ai_mime.app_data import get_env_path, get_user_config_path
from ai_mime.codex_support import codex_subprocess_env, find_codex_executable

Provider = Literal["anthropic", "openai"]

_PROVIDER_LABELS: dict[Provider, str] = {
    "anthropic": "Anthropic / Claude Code",
    "openai": "OpenAI / Codex",
}
_PROVIDER_KEY_ENVS: dict[Provider, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}
_CLAUDE_FALLBACK_DIRS = (
    ".local/bin",
    "bin",
    "/opt/homebrew/bin",
    "/usr/local/bin",
)


def _read_user_config(config_path: Path | None = None) -> dict[str, Any]:
    path = config_path or get_user_config_path()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_user_config(data: dict[str, Any], config_path: Path | None = None) -> None:
    path = config_path or get_user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data.setdefault("config_version", 1)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _read_provider(config_path: Path | None = None) -> str:
    provider = _read_user_config(config_path).get("provider")
    return provider if isinstance(provider, str) and provider else "anthropic"


def _write_provider(provider: Provider, config_path: Path | None = None) -> None:
    # Merge into the existing config so other keys (e.g. bash_requires_approval)
    # are preserved instead of being clobbered.
    data = _read_user_config(config_path)
    data["provider"] = provider
    _write_user_config(data, config_path)


def read_bash_requires_approval(config_path: Path | None = None) -> bool:
    """Whether Bash commands require user approval (persisted in user_config.yml).

    Defaults to True (require approval); only an explicit off-value disables it.
    """
    value = _read_user_config(config_path).get("bash_requires_approval")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "no", "off")
    return True


def write_bash_requires_approval(value: bool, config_path: Path | None = None) -> bool:
    data = _read_user_config(config_path)
    data["bash_requires_approval"] = bool(value)
    _write_user_config(data, config_path)
    return bool(value)


def read_autoinstall_skills(config_path: Path | None = None) -> bool:
    """Whether built/installed skills are auto-linked into Claude Code / Codex
    (persisted in user_config.yml). Defaults to True."""
    value = _read_user_config(config_path).get("autoinstall_skills")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "no", "off")
    return True


def write_autoinstall_skills(value: bool, config_path: Path | None = None) -> bool:
    data = _read_user_config(config_path)
    data["autoinstall_skills"] = bool(value)
    _write_user_config(data, config_path)
    return bool(value)


def _read_dotenv_value(key: str, env_path: Path | None = None) -> str | None:
    path = env_path or get_env_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    prefix = f"{key}="
    for line in lines:
        if not line.startswith(prefix):
            continue
        value = line[len(prefix) :].strip()
        return value.strip("\"'")
    return None


def _env_value(key: str) -> str | None:
    value = os.environ.get(key)
    if value is not None and value.strip():
        return value.strip()
    value = _read_dotenv_value(key)
    return value.strip() if value is not None and value.strip() else None


def _merge_env_var(env_path: Path, key: str, value: str) -> None:
    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    prefix = f"{key}="
    out: list[str] = []
    replaced = False
    for line in lines:
        if not replaced and line.startswith(prefix):
            out.append(f"{key}={value.strip()}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{key}={value.strip()}")
    env_path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    os.environ[key] = value.strip()


def _find_binary_exe(name: str) -> str | None:
    exe = shutil.which(name)
    if exe:
        return exe
    home = Path.home()
    for candidate_dir in _CLAUDE_FALLBACK_DIRS:
        candidate = Path(candidate_dir)
        if not candidate.is_absolute():
            candidate = home / candidate
        candidate = candidate / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _find_claude_exe() -> str | None:
    return _find_binary_exe("claude")


def _find_codex_exe() -> str | None:
    return find_codex_executable()


def _command_status(
    cmd: list[str],
    *,
    timeout: float = 3.0,
    env: dict[str, str] | None = None,
) -> tuple[bool, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False, env=env)
    except FileNotFoundError:
        return False, f"{cmd[0]} not found."
    except Exception as e:
        return False, str(e)
    output = (proc.stdout or proc.stderr or "").strip()
    detail = output.splitlines()[0] if output else f"{cmd[0]} exited {proc.returncode}."
    return proc.returncode == 0, detail


def _provider_runtime_status(provider: Provider) -> tuple[bool, str]:
    if provider == "anthropic":
        exe = _find_claude_exe()
        if not exe:
            return False, "Claude Code not found."
        ok, detail = _command_status([exe, "--version"])
        return ok, f"Claude Code detected: {detail}" if ok else f"Claude Code check failed: {detail}"

    exe = _find_codex_exe()
    if not exe:
        return False, "Codex CLI not found."
    ok, detail = _command_status([exe, "login", "status"], env=codex_subprocess_env(codex_exe=exe))
    return ok, f"Codex login detected: {detail}" if ok else f"Codex login check failed: {detail}"


def provider_status(provider: Provider) -> dict[str, Any]:
    key_env = _PROVIDER_KEY_ENVS[provider]
    has_api_key = _env_value(key_env) is not None
    runtime_available, runtime_status = _provider_runtime_status(provider)
    available = has_api_key or runtime_available
    if has_api_key:
        status = f"{key_env} is configured."
    else:
        status = runtime_status
    return {
        "label": _PROVIDER_LABELS[provider],
        "api_key_env": key_env,
        "has_api_key": has_api_key,
        "runtime_available": runtime_available,
        "available": available,
        "status": status,
    }


def provider_settings_status() -> dict[str, Any]:
    current = _read_provider()
    return {
        "provider": current if current in _PROVIDER_LABELS else "custom",
        "providers": {
            "anthropic": provider_status("anthropic"),
            "openai": provider_status("openai"),
        },
    }


def save_provider_settings(provider: str, *, api_key: str | None = None) -> dict[str, Any]:
    if provider not in _PROVIDER_LABELS:
        raise ValueError("provider must be anthropic or openai")
    selected = provider  # type: ignore[assignment]
    if api_key is not None and api_key.strip():
        _merge_env_var(get_env_path(), _PROVIDER_KEY_ENVS[selected], api_key.strip())

    status = provider_status(selected)
    if not status["available"]:
        raise RuntimeError(str(status["status"]))

    _write_provider(selected)
    return provider_settings_status()
