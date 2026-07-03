import json
import os
import subprocess
import shutil
import sys
from pathlib import Path
from ai_mime.debug_log import log
from ai_mime.app_data import is_frozen

def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    
    content = path.read_text(encoding="utf-8").strip()
    if not content:
        return {}
        
    try:
        data = json.loads(content)
        if not isinstance(data, dict):
            raise ValueError("Config file must be a JSON object")
        return data
    except Exception as e:
        log(f"Failed to parse JSON config at {path}: {e}")
        raise RuntimeError(f"Config file is corrupted or not a valid JSON object: {path}")

def _write_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    
    # Create a backup just in case
    if path.exists():
        backup_path = path.with_suffix(path.suffix + ".bak")
        try:
            shutil.copy2(path, backup_path)
        except Exception as e:
            log(f"Failed to create backup at {backup_path}: {e}")
            
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")

def _get_project_root() -> Path:
    return Path(__file__).parent.parent.parent.parent.absolute()

def _get_mcp_command_and_args() -> tuple[str, list[str]]:
    if is_frozen():
        return sys.executable, ["mcp-server"]
    
    uv_path = shutil.which("uv") or "uv"
    project_root = _get_project_root()
    return uv_path, ["--directory", str(project_root), "run", "ai-mime-mcp"]

def _patch_mcp_servers(config: dict) -> bool:
    """Returns True if the config was modified."""
    if "mcpServers" not in config:
        config["mcpServers"] = {}
    
    servers = config["mcpServers"]
    
    cmd, args = _get_mcp_command_and_args()
    
    servers["aimime"] = {
        "command": cmd,
        "args": args
    }
    return True

def _remove_mcp_server(config: dict) -> bool:
    """Returns True if the config was modified."""
    if "mcpServers" not in config:
        return False
    if "aimime" in config["mcpServers"]:
        del config["mcpServers"]["aimime"]
        if not config["mcpServers"]:
            del config["mcpServers"]
        return True
    return False

# Detectors
def _detect_claude_desktop() -> Path | None:
    path = Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
    if Path("/Applications/Claude.app").exists() or path.parent.exists() or path.exists():
        return path
    return None

def _detect_claude_code() -> bool:
    return shutil.which("claude") is not None

def _detect_antigravity() -> Path | None:
    path = Path.home() / ".gemini/config/mcp_config.json"
    return path if (Path.home() / ".gemini").exists() else None

def _detect_codex() -> bool:
    return shutil.which("codex") is not None

def get_available_clients() -> list[dict]:
    """Scans the system for supported agents and returns their availability."""
    clients = []
    
    if _detect_claude_desktop():
        clients.append({"id": "claude-desktop", "name": "Claude Desktop"})
        
    if _detect_claude_code():
        clients.append({"id": "claude-code", "name": "Claude Code (CLI)"})
        
    if _detect_antigravity():
        clients.append({"id": "antigravity", "name": "Antigravity"})
        
    if _detect_codex():
        clients.append({"id": "codex", "name": "Codex"})
        
    return clients

def install_mcp_to_clients(client_ids: list[str]):
    for cid in client_ids:
        try:
            if cid == "claude-desktop":
                path = _detect_claude_desktop()
                if path:
                    config = _read_json(path)
                    if _patch_mcp_servers(config):
                        _write_json(path, config)
                        log("Installed MCP to Claude Desktop")
            
            elif cid == "antigravity":
                path = _detect_antigravity()
                if path:
                    config = _read_json(path)
                    if _patch_mcp_servers(config):
                        _write_json(path, config)
                        log("Installed MCP to Antigravity")
                        
            elif cid == "codex":
                if _detect_codex():
                    cmd, args = _get_mcp_command_and_args()
                    subprocess.run(
                        ["codex", "mcp", "add", "aimime", "--", cmd] + args,
                        check=False,
                        capture_output=True
                    )
                    log("Installed MCP to Codex")
                        
            elif cid == "claude-code":
                if _detect_claude_code():
                    cmd, args = _get_mcp_command_and_args()
                    subprocess.run(
                        ["claude", "mcp", "add", "aimime", "--", cmd] + args,
                        check=False,
                        capture_output=True
                    )
                    log("Installed MCP to Claude Code")
        except Exception as e:
            log(f"Failed to install MCP to {cid}: {e}", exc_info=True)

def uninstall_mcp_from_clients(client_ids: list[str]):
    for cid in client_ids:
        try:
            if cid == "claude-desktop":
                path = _detect_claude_desktop()
                if path and path.exists():
                    config = _read_json(path)
                    if _remove_mcp_server(config):
                        _write_json(path, config)
                        log("Uninstalled MCP from Claude Desktop")
                        
            elif cid == "antigravity":
                path = _detect_antigravity()
                if path and path.exists():
                    config = _read_json(path)
                    if _remove_mcp_server(config):
                        _write_json(path, config)
                        log("Uninstalled MCP from Antigravity")
                        
            elif cid == "codex":
                if _detect_codex():
                    subprocess.run(
                        ["codex", "mcp", "remove", "aimime"],
                        check=False,
                        capture_output=True
                    )
                    log("Uninstalled MCP from Codex")
                        
            elif cid == "claude-code":
                if _detect_claude_code():
                    subprocess.run(
                        ["claude", "mcp", "remove", "aimime"],
                        check=False,
                        capture_output=True
                    )
                    log("Uninstalled MCP from Claude Code")
        except Exception as e:
            log(f"Failed to uninstall MCP from {cid}: {e}", exc_info=True)
