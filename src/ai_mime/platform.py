"""Platform abstraction layer for AI Mime.

Centralizes all OS-specific paths, commands, process handling, and filesystem
operations for macOS, Windows, and Linux.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Any

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


def is_windows() -> bool:
    return IS_WINDOWS


def is_mac() -> bool:
    return IS_MAC


def get_default_app_data_dir() -> Path:
    """Resolve standard OS user application data directory for AI Mime."""
    if IS_WINDOWS:
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "AI Mime"
        return Path.home() / "AppData" / "Roaming" / "AI Mime"
    elif IS_MAC:
        return Path.home() / "Library" / "Application Support" / "AI Mime"
    else:
        # Linux / XDG standard
        xdg_config = os.environ.get("XDG_CONFIG_HOME")
        if xdg_config:
            return Path(xdg_config) / "ai_mime"
        return Path.home() / ".config" / "ai_mime"


def get_system_paths() -> list[str]:
    """Get system binary path entries for execution isolation."""
    if IS_WINDOWS:
        system_root = os.environ.get("SystemRoot", "C:\\Windows")
        return [
            os.path.join(system_root, "System32"),
            system_root,
            os.path.join(system_root, "System32", "Wbem"),
            os.path.join(system_root, "System32", "WindowsPowerShell", "v1.0"),
        ]
    return [
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]


def executable_name(base: str) -> str:
    """Append .exe to executable name on Windows if not already present."""
    if IS_WINDOWS and not base.lower().endswith(".exe"):
        return f"{base}.exe"
    return base


def get_venv_python_relpath() -> Path:
    """Relative path from a venv root to its Python interpreter."""
    if IS_WINDOWS:
        return Path("Scripts") / "python.exe"
    return Path("bin") / "python"


def get_venv_bin_subdir() -> str:
    """Subdirectory name containing venv executables ('Scripts' on Windows, 'bin' on Unix)."""
    return "Scripts" if IS_WINDOWS else "bin"


def open_directory(path: str | os.PathLike[str]) -> None:
    """Open a folder in the operating system's native file manager."""
    p = Path(path).resolve()
    if not p.exists():
        raise FileNotFoundError(f"Directory does not exist: {p}")

    if IS_WINDOWS:
        os.startfile(str(p))  # type: ignore[attr-defined]
    elif IS_MAC:
        subprocess.run(["open", str(p)], check=True)
    else:
        subprocess.run(["xdg-open", str(p)], check=True)


def open_url(url: str) -> None:
    """Open a URL in the user's default browser."""
    if IS_WINDOWS:
        os.startfile(url)  # type: ignore[attr-defined]
    else:
        webbrowser.open(url)


def free_port(port: int) -> None:
    """Kill any process currently listening on the specified TCP port."""
    try:
        import psutil
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                for conn in proc.connections(kind="tcp"):
                    if conn.laddr and conn.laddr.port == port:
                        proc.kill()
                        break
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass
    except ImportError:
        # Fallback if psutil is not available
        if IS_MAC or IS_LINUX:
            try:
                pids = subprocess.run(
                    ["lsof", "-ti", f"tcp:{port}"],
                    capture_output=True,
                    text=True,
                    check=False,
                ).stdout.split()
                for pid in pids:
                    try:
                        os.kill(int(pid), 9)
                    except (ProcessLookupError, ValueError):
                        pass
            except Exception:
                pass
        elif IS_WINDOWS:
            try:
                out = subprocess.run(
                    ["netstat", "-ano", "-p", "tcp"],
                    capture_output=True,
                    text=True,
                    check=False,
                ).stdout
                for line in out.splitlines():
                    if f":{port} " in line and "LISTENING" in line:
                        parts = line.strip().split()
                        pid = parts[-1]
                        subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, check=False)
            except Exception:
                pass


def create_directory_link(link_path: Path, target_path: Path) -> None:
    """Create a directory link (Symlink on macOS/Linux, NTFS Junction on Windows).

    On Windows, NTFS Junction Points do not require Administrator privileges or Developer Mode.
    """
    target_path = target_path.expanduser().resolve()
    if not target_path.exists() or not target_path.is_dir():
        raise FileNotFoundError(f"Target directory does not exist: {target_path}")

    if IS_WINDOWS:
        # Windows Junction Points using _winapi if available
        try:
            import _winapi
            _winapi.CreateJunction(str(target_path), str(link_path))
            return
        except Exception:
            # Fallback to mklink /J command
            cmd = f'cmd /c mklink /J "{link_path}" "{target_path}"'
            proc = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if proc.returncode != 0:
                # Ultimate fallback: try standard symlink or copy
                try:
                    link_path.symlink_to(target_path, target_is_directory=True)
                except Exception as e:
                    raise OSError(f"Failed to create directory link on Windows: {proc.stderr or e}")
    else:
        link_path.symlink_to(target_path, target_is_directory=True)


def is_link(path: Path) -> bool:
    """True if path is a symlink or Windows Junction point."""
    if path.is_symlink():
        return True
    if IS_WINDOWS and path.exists():
        # Check Windows junction attribute (FILE_ATTRIBUTE_REPARSE_POINT = 0x400)
        try:
            import stat
            st = path.lstat()
            if hasattr(st, "st_file_attributes") and (st.st_file_attributes & 0x400):
                return True
        except Exception:
            pass
    return False
