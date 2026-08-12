"""The browser-snapshot compression helpers tools: what they advertise and what they do.

``schemas()`` returns the ADVERTISEMENT half of each tool -- its name, the
model-facing description, and the JSON Schema a call is validated against.
``HANDLERS`` maps each of those names to the function that runs it. Both halves
of a tool live here so its contract and its behavior are read together, and
``test_mcp_tool_registry`` fails if one arrives without the other.

Handlers reach this server's shared plumbing as attributes of ``mcp_core`` --
``mcp_core._post``, the identity resolvers, the governance vets. That is
deliberate rather than untidy: an attribute lookup resolves at CALL time, so a
test that rebinds one on the module still intercepts the handler. Importing
those names directly here would bind them at import time and silently escape
every existing patch site.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from kiro_crew import mcp_core
from kiro_crew.security import redact_credentials, redact_exfiltration_urls


def schemas() -> list[dict[str, Any]]:
    """Descriptors for the browse tools."""
    return [
        {
            "name": "browse_outline",
            "description": (
                "Compress a browser snapshot into a compact outline with element refs. "
                "Use AFTER calling browser_snapshot to reduce a large accessibility tree "
                "(50-100K tokens) into a navigable outline (~2-5K tokens). "
                "Returns interactive elements with refs for clicking, plus page structure."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "snapshot": {
                        "type": "string",
                        "description": "The raw browser_snapshot output text to compress",
                    },
                    "max_lines": {
                        "type": "integer",
                        "description": "Max output lines (default 100)",
                        "default": 100,
                    },
                },
                "required": ["snapshot"],
            },
        },
        {
            "name": "browse_search",
            "description": (
                "Search a browser snapshot for specific text or patterns. "
                "Returns matching lines with element refs. Use instead of reading "
                "the full snapshot when looking for specific content on a page."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "snapshot": {
                        "type": "string",
                        "description": "The raw browser_snapshot output text to search",
                    },
                    "query": {
                        "type": "string",
                        "description": "Text or regex pattern to search for",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Max matching lines to return (default 50)",
                        "default": 50,
                    },
                },
                "required": ["snapshot", "query"],
            },
        },
    ]


def browse_outline(name: str, args: dict[str, Any]) -> str:
    snapshot = args.get("snapshot", "")
    max_lines = args.get("max_lines", 100)
    result = mcp_core._compress_snapshot_to_outline(snapshot, max_lines)
    result, _ = redact_exfiltration_urls(result)
    result, _ = redact_credentials(result)
    mcp_core.sel().log_tool_invocation(
        session_key=mcp_core._resolve_session_key(),
        source="mcp",
        tool_name="browse_outline",
        outcome="success",
    )
    return result


def browse_search(name: str, args: dict[str, Any]) -> str:
    snapshot = args.get("snapshot", "")
    query = args.get("query", "")
    max_results = args.get("max_results", 50)
    result = mcp_core._search_snapshot(snapshot, query, max_results)
    result, _ = redact_exfiltration_urls(result)
    result, _ = redact_credentials(result)
    mcp_core.sel().log_tool_invocation(
        session_key=mcp_core._resolve_session_key(),
        source="mcp",
        tool_name="browse_search",
        outcome="success",
    )
    return result


HANDLERS: dict[str, Callable[[str, dict[str, Any]], str]] = {
    "browse_outline": browse_outline,
    "browse_search": browse_search,
}
