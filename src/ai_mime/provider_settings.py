from __future__ import annotations

import os
import shutil
import subprocess
import sys
import urllib.request
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


def read_autoinstall_mcp(config_path: Path | None = None) -> bool:
    """Whether MCP servers are auto-installed (persisted in user_config.yml). Defaults to False."""
    value = _read_user_config(config_path).get("autoinstall_mcp")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return False


def write_autoinstall_mcp(value: bool, config_path: Path | None = None) -> bool:
    data = _read_user_config(config_path)
    data["autoinstall_mcp"] = bool(value)
    _write_user_config(data, config_path)
    return bool(value)


def read_autoinstall_mcp_clients(config_path: Path | None = None) -> list[str]:
    """Which MCP clients are auto-installed (persisted in user_config.yml)."""
    value = _read_user_config(config_path).get("autoinstall_mcp_clients")
    if isinstance(value, list):
        return [str(v) for v in value]
    return []


def write_autoinstall_mcp_clients(clients: list[str], config_path: Path | None = None) -> list[str]:
    data = _read_user_config(config_path)
    data["autoinstall_mcp_clients"] = list(clients)
    _write_user_config(data, config_path)
    return clients


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
    fallback_dirs = list(_CLAUDE_FALLBACK_DIRS)
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        localappdata = os.environ.get("LOCALAPPDATA")
        if appdata:
            fallback_dirs.append(os.path.join(appdata, "npm"))
        if localappdata:
            fallback_dirs.append(os.path.join(localappdata, "Programs", name))
            fallback_dirs.append(os.path.join(localappdata, "Programs", "Claude"))
            fallback_dirs.append(os.path.join(localappdata, "Programs", "Codex"))
        fallback_dirs.append(str(home / ".claude" / "bin"))
        fallback_dirs.append(str(home / ".codex" / "bin"))
    for candidate_dir in fallback_dirs:
        candidate = Path(candidate_dir)
        if not candidate.is_absolute():
            candidate = home / candidate
        target = candidate / name
        for ext in ("", ".cmd", ".exe", ".bat", ".ps1"):
            p = target.with_name(target.name + ext) if ext else target
            if p.is_file():
                return str(p)
    return None


def _find_claude_exe() -> str | None:
    return _find_binary_exe("claude")


def _find_codex_exe() -> str | None:
    return find_codex_executable() or _find_binary_exe("codex")


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
        ok, detail = _command_status([exe, "auth", "status"])
        return ok, f"Claude Code logged in: {detail}" if ok else f"Claude Code login check failed: {detail}"

    exe = _find_codex_exe()
    if not exe:
        return False, "Codex CLI not found."
    ok, detail = _command_status([exe, "login", "status"], env=codex_subprocess_env(codex_exe=exe))
    return ok, f"Codex login detected: {detail}" if ok else f"Codex login check failed: {detail}"


def is_provider_installed(provider: Provider) -> bool:
    if provider == "anthropic":
        return _find_claude_exe() is not None
    return _find_codex_exe() is not None


def launch_terminal_login(provider: str) -> None:
    """Launch interactive login in a new terminal window for the specified provider."""
    if provider == "anthropic":
        exe = _find_claude_exe()
        subcmd = "auth login"
        fallback_name = "claude"
    else:
        exe = _find_codex_exe()
        subcmd = "login"
        fallback_name = "codex"

    cmd_exec = f'"{exe}" {subcmd}' if exe else f"{fallback_name} {subcmd}"

    if sys.platform == "win32":
        home = Path.home()
        appdata = os.environ.get("APPDATA", "")
        localappdata = os.environ.get("LOCALAPPDATA", "")
        paths_to_add = [
            str(home / ".local" / "bin"),
            str(home / ".claude" / "bin"),
            str(home / ".codex" / "bin"),
            os.path.join(appdata, "npm") if appdata else "",
            os.path.join(localappdata, "Programs", "Codex") if localappdata else "",
            os.path.join(localappdata, "Programs", "Claude") if localappdata else "",
        ]
        if exe:
            paths_to_add.insert(0, str(Path(exe).parent))

        extra_path = ";".join([p for p in paths_to_add if p and os.path.exists(p)])
        env = dict(os.environ)
        if extra_path:
            env["PATH"] = extra_path + ";" + env.get("PATH", "")
            full_cmd = f'set "PATH={extra_path};%PATH%" && {cmd_exec}'
        else:
            full_cmd = cmd_exec

        subprocess.Popen(["cmd.exe", "/c", "start", "cmd.exe", "/k", full_cmd], env=env)
    elif sys.platform == "darwin":
        applescript = f'''
        tell application "Terminal"
            activate
            do script "{cmd_exec}"
        end tell
        '''
        try:
            subprocess.run(["osascript", "-e", applescript])
        except Exception:
            pass
    else:
        for term in ("x-terminal-emulator", "gnome-terminal", "konsole", "xfce4-terminal", "xterm"):
            if shutil.which(term):
                subprocess.Popen([term, "-e", cmd_exec])
                break


def launch_terminal_install(provider: str) -> None:
    """Launch CLI installer in an interactive terminal window so user prompts and progress are displayed."""
    if sys.platform == "win32":
        if provider == "anthropic":
            if shutil.which("npm"):
                cmd_str = 'npm install -g @anthropic-ai/claude-code || powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://claude.ai/install.ps1 | iex"'
            else:
                cmd_str = 'powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://claude.ai/install.ps1 | iex"'
        else:  # openai
            if shutil.which("npm"):
                cmd_str = 'npm install -g @openai/codex || powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://chatgpt.com/codex/install.ps1 | iex"'
            else:
                cmd_str = 'powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://chatgpt.com/codex/install.ps1 | iex"'

        subprocess.Popen(["cmd.exe", "/c", "start", "cmd.exe", "/k", cmd_str])
    elif sys.platform == "darwin":
        cmd_str = "curl -fsSL https://claude.ai/install.sh | bash" if provider == "anthropic" else "curl -fsSL https://chatgpt.com/codex/install.sh | sh"
        applescript = f'''
        tell application "Terminal"
            activate
            do script "{cmd_str}"
        end tell
        '''
        try:
            subprocess.run(["osascript", "-e", applescript])
        except Exception:
            pass
    else:
        cmd_str = "curl -fsSL https://claude.ai/install.sh | bash" if provider == "anthropic" else "curl -fsSL https://chatgpt.com/codex/install.sh | sh"
        for term in ("x-terminal-emulator", "gnome-terminal", "konsole", "xfce4-terminal", "xterm"):
            if shutil.which(term):
                subprocess.Popen([term, "-e", cmd_str])
                break


def install_provider_cli(provider: Provider) -> tuple[bool, str]:
    if is_provider_installed(provider):
        return True, "Already installed."

    if provider == "anthropic":
        if sys.platform == "win32":
            cmd = 'powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://claude.ai/install.ps1 | iex"'
        else:
            cmd = "curl -fsSL https://claude.ai/install.sh | bash"
    else:  # openai
        if shutil.which("npm"):
            cmd = "npm install -g @openai/codex"
        elif sys.platform == "win32":
            cmd = 'powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://chatgpt.com/codex/install.ps1 | iex"'
        else:
            cmd = "curl -fsSL https://chatgpt.com/codex/install.sh | sh"

    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=600)
    except Exception as e:
        return False, f"Installation error: {e}"

    if is_provider_installed(provider):
        return True, "Installation complete."

    err = (proc.stderr or proc.stdout or "").strip()
    return False, f"Installation failed: {err}"


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
