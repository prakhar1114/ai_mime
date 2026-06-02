"""Global debug logging utility for AI Mime.

Usage:
    from ai_mime.debug_log import log

    log("Something happened")
    log(f"Variable: {value}")
    log("Error occurred", exc_info=True)  # Includes traceback
"""
import os
import sys
import traceback
from pathlib import Path
from datetime import datetime


def _app_support_dir() -> Path:
    return Path(os.path.expanduser("~/Library/Application Support/AI Mime"))


def get_debug_log_path() -> Path:
    """Path to the global debug log file."""
    return _app_support_dir() / "debug.log"


def get_server_log_path() -> Path:
    """Path to the append-only server log file."""
    return _app_support_dir() / "server.log"


def open_server_log_file():
    """Open an append-only stream for server stdout/stderr."""
    for log_path in (get_server_log_path(), Path("/tmp/ai_mime_server.log")):
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            return open(log_path, "a", encoding="utf-8", buffering=1)
        except Exception:
            pass
    return open(os.devnull, "w", encoding="utf-8")


def _write_log(log_path: Path, fallback_path: str, msg: str, *, exc_info: bool) -> None:
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)

        with open(log_path, "a", encoding="utf-8") as f:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            pid = os.getpid()
            f.write(f"[{ts}] [PID {pid}] {msg}\n")

            if exc_info:
                f.write(traceback.format_exc())

            f.flush()
    except Exception:
        # Fallback to /tmp if main log fails.
        try:
            with open(fallback_path, "a", encoding="utf-8") as f:
                f.write(f"{msg}\n")
                if exc_info:
                    f.write(traceback.format_exc())
        except Exception:
            pass


def log(msg: str, *, exc_info: bool = False) -> None:
    """Write debug message to global debug log file.

    Args:
        msg: Message to log
        exc_info: If True, append current exception traceback
    """
    _write_log(get_debug_log_path(), "/tmp/ai_mime_debug.log", msg, exc_info=exc_info)


def log_server(msg: str, *, exc_info: bool = False) -> None:
    """Write server message to the append-only server log file."""
    _write_log(get_server_log_path(), "/tmp/ai_mime_server.log", msg, exc_info=exc_info)
