"""Static classification of Bash commands for permission gating.

Shared by the Claude and Codex agent adapters so both runtimes apply the same
"is this command safe to auto-allow?" policy without depending on each other's
SDK. The classifier is deliberately conservative: only provably read-only
commands (plus a small set of user-accepted package managers) auto-allow;
anything unrecognized, privileged, networked, or writing via redirection is
treated as requiring approval.
"""
from __future__ import annotations

import os
import re
import shlex

_SHELL_SEPARATORS = {"&&", "||", ";", "|", "&"}
_SHELL_COMMAND_PREFIXES = {"command", "exec", "env", "time", "nohup"}
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")

# Commands that only read/print and cannot mutate the filesystem or run other
# code in normal usage.
_BASH_READONLY_COMMANDS = {
    "ls", "cat", "head", "tail", "pwd", "echo", "printf", "grep", "egrep",
    "fgrep", "rg", "wc", "which", "whoami", "id", "date", "env", "printenv",
    "file", "stat", "du", "df", "tree", "basename", "dirname", "realpath",
    "readlink", "true", "false", "uname", "hostname", "diff", "cmp", "jq",
    "column", "nl", "tac", "cut", "tr", "uniq", "comm", "xxd", "hexdump",
    "shasum", "sha256sum", "md5", "md5sum", "type", "ps",
}
# Commands that mutate the filesystem but only by *creating* paths, never
# overwriting or deleting existing data. Low-risk enough to auto-allow even
# though they are not strictly read-only.
_BASH_SAFE_WRITE_COMMANDS = {"mkdir"}
# Package managers explicitly accepted as safe (they run code, but are routine
# enough to auto-allow).
_BASH_ALLOWED_PACKAGE_MANAGERS = {"pip", "pip3", "npm", "uv"}
_GIT_GLOBAL_OPTS_WITH_ARG = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
_GIT_READONLY_SUBCOMMANDS = {
    "status", "diff", "log", "show", "rev-parse", "ls-files", "describe",
    "blame", "shortlog", "cat-file", "ls-tree", "reflog",
}
_FIND_DANGEROUS_FLAGS = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls"}
_REDIRECT_RE = re.compile(r"^(\d*)>>?(.*)$")

# Commands exported via workflow_runtime_env that are safe to auto-allow.
_BASH_ALLOWED_RUNTIME_ENV_COMMANDS = {
    "$AI_MIME_BROWSER_HARNESS_BIN",
    "$AI_MIME_UV_PATH",
    "$AI_MIME_PYTHON_PATH",
}


def _has_write_redirect(tokens: list[str]) -> bool:
    """True if any token redirects output to a file (e.g. `>out`, `>> log`).

    File-descriptor duplications (`2>&1`) and discards (`2>/dev/null`) are not
    treated as writes.
    """
    for token in tokens:
        if ">" not in token:
            continue
        match = _REDIRECT_RE.match(token)
        if match is None:
            return True
        target = match.group(2)
        if target.startswith("&"):  # fd duplication, e.g. 2>&1
            continue
        if target == "/dev/null":  # discard, harmless
            continue
        return True  # writes to a file (incl. standalone `>` with target next token)
    return False


def _git_subcommand(args: list[str]) -> str | None:
    i = 0
    while i < len(args):
        token = args[i]
        if token in _GIT_GLOBAL_OPTS_WITH_ARG:
            i += 2
            continue
        if token.startswith("-"):
            i += 1
            continue
        return token
    return None


def _segment_command_is_safe(base: str, args: list[str]) -> bool:
    if base == "sudo":
        return False
    if base in _BASH_ALLOWED_PACKAGE_MANAGERS:
        return True
    if base in _BASH_SAFE_WRITE_COMMANDS:
        return True
    if base == "git":
        return _git_subcommand(args) in _GIT_READONLY_SUBCOMMANDS
    if base == "find":
        return not any(a in _FIND_DANGEROUS_FLAGS for a in args)
    if base == "sort":
        return not any(a in ("-o", "--output") or a.startswith("--output=") for a in args)
    return base in _BASH_READONLY_COMMANDS


def bash_command_requires_approval(command: str) -> bool:
    """Return True when a Bash command should be sent to the user for approval.

    Conservative classifier: only provably read-only commands (plus the
    user-accepted package managers) auto-allow. Anything unrecognized,
    privileged, using command substitution, or writing via redirection asks.
    """
    text = (command or "").strip()
    if not text:
        return True
    if any(text.lstrip('"\'').startswith(cmd) for cmd in _BASH_ALLOWED_RUNTIME_ENV_COMMANDS):
        return False
    if "$(" in text or "`" in text:  # command substitution can hide commands
        return True
    try:
        tokens = shlex.split(text, posix=True)
    except ValueError:
        return True
    if not tokens or _has_write_redirect(tokens):
        return True

    expect_command = True
    i = 0
    n = len(tokens)
    while i < n:
        token = tokens[i]
        if token in _SHELL_SEPARATORS:
            expect_command = True
            i += 1
            continue
        if not expect_command:
            i += 1
            continue
        if _ENV_ASSIGNMENT_RE.match(token):
            i += 1
            continue
        if token in _SHELL_COMMAND_PREFIXES:
            i += 1  # the real command follows
            continue
        # `token` is in command position — collect its args up to the next separator.
        seg: list[str] = []
        j = i + 1
        while j < n and tokens[j] not in _SHELL_SEPARATORS:
            seg.append(tokens[j])
            j += 1
        if not _segment_command_is_safe(os.path.basename(token), seg):
            return True
        expect_command = False
        i = j
    return False
