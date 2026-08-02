from __future__ import annotations

import sys
from typing import Any

from ai_mime.memory import delete_memory, get_memory, search_memory, store_memory

# cua-computer-server mounts its MCP server (streamable HTTP) at /mcp on the API
# server started by cli.start_app; the trailing slash matters and the port must
# stay in sync with cli.COMPUTER_SERVER_PORT. Shared so any runtime adapter can
# attach the same computer-use tools.
CUA_MCP_SERVER_NAME = "cua"
CUA_MCP_URL = "http://127.0.0.1:58840/mcp/"

AI_MIME_MCP_SERVER_NAME = "ai-mime-mcp"


def cua_mcp_servers() -> dict[str, dict[str, Any]]:
    """MCP config for cua-computer-server's streamable-HTTP endpoint."""
    return {CUA_MCP_SERVER_NAME: {"type": "http", "url": CUA_MCP_URL}}


def ai_mime_mcp_servers() -> dict[str, dict[str, Any]]:
    """MCP config for AI Mime's stdio MCP server (exposing skill tools and memory tools)."""
    return {
        AI_MIME_MCP_SERVER_NAME: {
            "command": sys.executable,
            "args": ["-m", "ai_mime.mcp_server"],
        }
    }


def fetch_relevant_memories(
    prompt: str,
    limit: int = 10,
    threshold: float | None = None,
) -> str:
    """Perform semantic search for prompt and return formatted markdown string of memories."""
    memories = search_memory(prompt, limit=limit, threshold=threshold)
    if not memories:
        return ""

    lines = ["## Relevant User Memories:"]
    for mem in memories:
        score_pct = int(mem.get("score", 0) * 100)
        date_str = mem.get("date") or mem.get("created_at") or ""
        date_label = f" [{date_str[:10]}]" if date_str else ""
        lines.append(f"- [{score_pct}% match]{date_label} {mem.get('content', '')}")
    return "\n".join(lines)


__all__ = [
    "CUA_MCP_SERVER_NAME",
    "CUA_MCP_URL",
    "AI_MIME_MCP_SERVER_NAME",
    "cua_mcp_servers",
    "ai_mime_mcp_servers",
    "store_memory",
    "get_memory",
    "search_memory",
    "delete_memory",
    "fetch_relevant_memories",
]
