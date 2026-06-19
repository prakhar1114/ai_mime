"""Expose built/installed skills to Claude Code and Codex via symlinks.

Each agent reads personal-scope skills from a well-known directory
(``~/.claude/skills`` / ``~/.codex/skills``). We surface an AI Mime skill by
symlinking its directory into whichever of those exist. Links are only ever
created/removed by this module, and only ones pointing back into the workflows
root are touched on cleanup — foreign skills and real directories are left
alone.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from ai_mime.app_data import get_workflows_dir
from ai_mime.debug_log import log as debug_log

# Personal-scope skills dirs, keyed by the base dir that signals the tool is
# present. We link into a target only when its base dir exists.
_SKILL_TARGETS: tuple[tuple[Path, Path], ...] = (
    (Path.home() / ".claude", Path.home() / ".claude" / "skills"),
    (Path.home() / ".codex", Path.home() / ".codex" / "skills"),
    (Path.home() / ".gemini", Path.home() / ".gemini" / "config" / "skills"),
)

_FRONTMATTER_NAME_RE = re.compile(r"^\s*name\s*:\s*(.+?)\s*$", re.MULTILINE)


def _log(message: str) -> None:
    debug_log(f"[skill-links] {message}")


def _skill_link_targets() -> list[Path]:
    """Skills dirs to link into — those whose tool base dir exists."""
    targets: list[Path] = []
    for base, skills_dir in _SKILL_TARGETS:
        if base.is_dir():
            skills_dir.mkdir(parents=True, exist_ok=True)
            targets.append(skills_dir)
    return targets


def _skill_name(skill_dir: Path) -> str:
    """Frontmatter ``name`` from SKILL.md, falling back to the directory name."""
    skill_md = skill_dir / "SKILL.md"
    if skill_md.is_file():
        m = _FRONTMATTER_NAME_RE.search(skill_md.read_text(encoding="utf-8"))
        if m:
            name = m.group(1).strip().strip("\"'").strip()
            if name:
                return name
    return skill_dir.name


def _points_into(link: Path, root: Path) -> bool:
    """True if ``link`` is a symlink whose target is inside ``root`` (uses the
    raw link target so broken/dangling links still match)."""
    if not link.is_symlink():
        return False
    try:
        target = Path(os.readlink(link))
        if not target.is_absolute():
            target = (link.parent / target).resolve(strict=False)
        return root in target.parents or target == root
    except OSError:
        return False


def link_skill(skill_dir: str | os.PathLike[str]) -> list[Path]:
    """Symlink ``skill_dir`` into every available target. Replaces an existing
    symlink with the same name; never clobbers a real directory. Returns the
    links created/updated."""
    skill_dir = Path(skill_dir).resolve()
    if not (skill_dir / "SKILL.md").is_file():
        return []
    name = _skill_name(skill_dir)
    created: list[Path] = []
    for skills_dir in _skill_link_targets():
        link = skills_dir / name
        if link.is_symlink():
            link.unlink()
        elif link.exists():
            # A real directory owns this name — don't clobber it.
            _log(f"Skipping {link}: a real directory already owns this name")
            continue
        try:
            link.symlink_to(skill_dir)
            created.append(link)
        except OSError as e:
            _log(f"Failed to link {link} -> {skill_dir}: {e}")
    return created


def unlink_skill_for_workflow(workflow_dir: str | os.PathLike[str]) -> list[Path]:
    """Remove every AI-Mime symlink that points into ``workflow_dir``."""
    workflow_dir = Path(workflow_dir).resolve()
    removed: list[Path] = []
    for skills_dir in _skill_link_targets():
        if not skills_dir.is_dir():
            continue
        for entry in skills_dir.iterdir():
            if _points_into(entry, workflow_dir):
                try:
                    entry.unlink()
                    removed.append(entry)
                except OSError as e:
                    _log(f"Failed to unlink {entry}: {e}")
    return removed


def sync_all_skill_links(enabled: bool) -> dict[str, int]:
    """Bulk-sync to match the toggle. When ``enabled``, link every skill under
    the workflows root into all targets; when disabled, remove every AI-Mime
    symlink (any link pointing into the workflows root, including stale ones)."""
    workflows_root = get_workflows_dir().resolve()
    if not enabled:
        removed = 0
        for skills_dir in _skill_link_targets():
            if not skills_dir.is_dir():
                continue
            for entry in list(skills_dir.iterdir()):
                if _points_into(entry, workflows_root):
                    try:
                        entry.unlink()
                        removed += 1
                    except OSError as e:
                        _log(f"Failed to unlink {entry}: {e}")
        return {"linked": 0, "removed": removed}

    linked = 0
    if workflows_root.is_dir():
        for skill_md in workflows_root.glob("*/skills/*/SKILL.md"):
            linked += len(link_skill(skill_md.parent))
    return {"linked": linked, "removed": 0}
