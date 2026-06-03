from __future__ import annotations

from typing import Any

# cua-computer-server mounts its MCP server (streamable HTTP) at /mcp on the API
# server started by cli.start_app; the trailing slash matters and the port must
# stay in sync with cli.COMPUTER_SERVER_PORT. Shared so any runtime adapter can
# attach the same computer-use tools.
CUA_MCP_SERVER_NAME = "cua"
CUA_MCP_URL = "http://127.0.0.1:58840/mcp/"


def cua_mcp_servers() -> dict[str, dict[str, Any]]:
    """MCP config for cua-computer-server's streamable-HTTP endpoint."""
    return {CUA_MCP_SERVER_NAME: {"type": "http", "url": CUA_MCP_URL}}
