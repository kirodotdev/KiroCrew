"""Whether ``mcp_entry`` started this process as a Kiro Crew MCP stdio server.

A process-role flag, read by ``sel.sel``: an enforcing harness withholds the SEL
trust root from every child it starts, and only such a server degrades to a log
that writes nothing. Set once at the entry and never cleared. Stdlib only, so the
slim MCP entry stays light.
"""

from __future__ import annotations

_mcp_stdio_server = False


def note_mcp_stdio_server() -> None:
    """Remember that ``mcp_entry`` started this process as an MCP stdio server."""
    global _mcp_stdio_server
    _mcp_stdio_server = True


def is_mcp_stdio_server() -> bool:
    """Whether :func:`note_mcp_stdio_server` ran in this process."""
    return _mcp_stdio_server
