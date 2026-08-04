"""FastMCP compatibility shim.

The class was renamed to MCPServer in the official SDK v2 while keeping the
same `.tool()` / `.run(transport=...)` surface, and the standalone `fastmcp`
package exposes it under the original name. Resolve whichever is installed
rather than pinning the ecosystem to one of them.
"""

from __future__ import annotations

from typing import Any

_ERROR = (
    "No MCP server implementation found. Install one of: "
    "`mcp>=2` (MCPServer), `mcp<2` (FastMCP), or `fastmcp`."
)


def load_server_class() -> tuple[type, str]:
    """Return (ServerClass, flavour) for the installed SDK."""
    try:
        from mcp.server.fastmcp import FastMCP  # type: ignore[import-not-found]

        return FastMCP, "mcp.server.fastmcp.FastMCP"
    except ImportError:
        pass
    try:
        from fastmcp import FastMCP  # type: ignore[import-not-found]

        return FastMCP, "fastmcp.FastMCP"
    except ImportError:
        pass
    try:
        from mcp.server.mcpserver import MCPServer  # type: ignore[import-not-found]

        return MCPServer, "mcp.server.mcpserver.MCPServer"
    except ImportError as exc:  # pragma: no cover - environment without any SDK
        raise ImportError(_ERROR) from exc


def build_server(name: str, **kwargs: Any) -> tuple[Any, str]:
    server_class, flavour = load_server_class()
    return server_class(name, **kwargs), flavour
