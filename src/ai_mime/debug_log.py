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


def log(msg: str, *, exc_info: bool = False) -> None:
    """Write debug message to global log file.

    Args:
        msg: Message to log
        exc_info: If True, append current exception traceback
    """
    try:
        log_path = Path(os.path.expanduser("~/Library/Application Support/AI Mime/debug.log"))
        log_path.parent.mkdir(parents=True, exist_ok=True)

        with open(log_path, "a", encoding="utf-8") as f:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            pid = os.getpid()
            f.write(f"[{ts}] [PID {pid}] {msg}\n")

            if exc_info:
                f.write(traceback.format_exc())

            f.flush()
    except Exception:
        # Fallback to /tmp if main log fails
        try:
            with open("/tmp/ai_mime_debug.log", "a", encoding="utf-8") as f:
                f.write(f"{msg}\n")
        except Exception:
            pass
