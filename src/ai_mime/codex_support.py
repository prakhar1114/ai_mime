from __future__ import annotations

import os
import shutil
from pathlib import Path

import sys

_HOST_CLI_DIRS = (
    ".local/bin",
    ".codex/bin",
    ".claude/bin",
    "AppData/Roaming/npm",
    "AppData/Local/Programs/Codex",
    "AppData/Local/Programs/Claude",
    "bin",
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
)


def _home_from_env(env: dict[str, str] | None = None) -> Path:
    raw = (env or os.environ).get("HOME") or (env or os.environ).get("USERPROFILE")
    if raw:
        return Path(raw).expanduser()
    return Path.home()


def _candidate_dirs(home: Path) -> list[str]:
    dirs: list[str] = []
    appdata = os.environ.get("APPDATA")
    localappdata = os.environ.get("LOCALAPPDATA")
    if appdata:
        dirs.append(os.path.join(appdata, "npm"))
    if localappdata:
        dirs.append(os.path.join(localappdata, "Programs", "Codex"))

    for raw in _HOST_CLI_DIRS:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = home / candidate
        dirs.append(str(candidate))
    return dirs


def _merge_path(*groups: list[str]) -> str:
    seen: set[str] = set()
    merged: list[str] = []
    for group in groups:
        for item in group:
            if not item or item in seen:
                continue
            seen.add(item)
            merged.append(item)
    return os.pathsep.join(merged)


def find_codex_executable(*, env: dict[str, str] | None = None) -> str | None:
    """Find Codex from terminal, Windows or macOS app launch environments."""
    base_env = env or os.environ
    home = _home_from_env(base_env)
    search_path = _merge_path(
        (base_env.get("PATH") or "").split(os.pathsep),
        _candidate_dirs(home),
    )
    exe = shutil.which("codex", path=search_path)
    if exe:
        return exe

    if sys.platform == "win32":
        for dir_path in _candidate_dirs(home):
            base = Path(dir_path) / "codex"
            for ext in (".cmd", ".exe", ".bat", ".ps1"):
                p = base.with_suffix(ext)
                if p.is_file():
                    return str(p)
    return None


def codex_subprocess_env(
    base_env: dict[str, str] | None = None,
    *,
    codex_exe: str | os.PathLike[str] | None = None,
) -> dict[str, str]:
    """Environment for invoking Codex from a GUI-launched app.

    The npm-installed Codex entrypoint is a ``#!/usr/bin/env node`` wrapper.
    A macOS app often lacks the shell PATH entries where ``node`` and Codex
    live, so preserve the app's env and append common host CLI locations.
    """
    env = dict(base_env or os.environ)
    home = _home_from_env(env)
    env.setdefault("HOME", str(home))
    codex_home = home / ".codex"
    if "CODEX_HOME" not in env and codex_home.exists():
        env["CODEX_HOME"] = str(codex_home)

    exe_dirs: list[str] = []
    if codex_exe is not None:
        try:
            exe_dirs.append(str(Path(codex_exe).expanduser().resolve().parent))
        except Exception:
            exe_dirs.append(str(Path(codex_exe).expanduser().parent))

    env["PATH"] = _merge_path(
        (env.get("PATH") or "").split(os.pathsep),
        exe_dirs,
        _candidate_dirs(home),
    )
    return env
