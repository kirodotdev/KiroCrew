"""Qualified names for the direct MCP invocation path.

These helpers describe direct calls through the session's tool list:

* ``id``: ``mcp_`` + ``<server>_<tool>`` with whitespace and ``-`` written as
  ``_``, other characters outside ``[A-Za-z0-9_]`` dropped, lower-cased and cut
  to 64 characters. The helper does not allocate collision suffixes; the exact
  active tool definition takes precedence when names collide.
* ``displayName``: ``@<server>/<tool>``, lower-cased.

A bare name such as ``work_report`` is neither direct-call spelling. A legacy
loader that asks for direct invocation using only a bare ``tool_name`` still
needs the qualified-name fallback in the session context.

This is not a naming contract for every discovery flow. When a loader specifies
an executor, use its returned ID with that executor unchanged. Neither these
helpers nor the direct-call fallback translate catalogue or executor IDs.

Importing this module costs only the stdlib. The provider-label constant is read
inside :func:`is_kas_provider_label`, behind the agent-SDK boundary.
"""

from __future__ import annotations

import re

__all__ = [
    "KAS_MCP_TOOL_ID_MAX_LEN",
    "KAS_TOOL_UNAVAILABLE_RE",
    "is_kas_provider_label",
    "kas_mcp_display_name",
    "kas_mcp_tool_id",
]

#: KAS's cap on an MCP tool id, the ``mcp_`` prefix included.
KAS_MCP_TOOL_ID_MAX_LEN = 64

#: The text KAS answers a tool call it could not resolve with, as the failed
#: tool call's output. ``name`` is the name the model called, verbatim.
KAS_TOOL_UNAVAILABLE_RE = re.compile(r'Tool "(?P<name>[^"\r\n]{1,256})" is not available\.')

_SEPARATORS = re.compile(r"[\s-]")
_DROPPED = re.compile(r"[^a-zA-Z0-9_]")


def kas_mcp_tool_id(server: str, tool: str) -> str:
    """The id KAS gives MCP tool *tool* on server *server*, before any collision suffix."""
    fused = _DROPPED.sub("", _SEPARATORS.sub("_", f"{server}_{tool}"))
    return f"mcp_{fused.lower()}"[:KAS_MCP_TOOL_ID_MAX_LEN]


def kas_mcp_display_name(server: str, tool: str) -> str:
    """The ``displayName`` KAS gives MCP tool *tool* on server *server*."""
    return f"@{server.lower()}/{tool.lower()}"


def is_kas_provider_label(provider_type: str | None) -> bool:
    """Whether *provider_type* (a session's provider label) names KAS.

    A positive comparison against KAS's own label (harness-parity H5). The label
    table lives in the ACP layer; it is read here, behind the agent-SDK boundary,
    and only when asked, so importing this module stays stdlib-only.
    """
    if not provider_type:
        return False
    from kiro_crew.acp.types import PROVIDER_LABEL_KAS

    return provider_type == PROVIDER_LABEL_KAS
