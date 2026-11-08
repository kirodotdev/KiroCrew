"""The names KAS resolves a model's MCP tool call by.

KAS (``kiro-cli acp --agent-engine v3``) answers a model's tool call by looking
the called name up in the session's tool list, by the tool's ``id`` first and its
``displayName`` second, and by nothing else. For an MCP tool those are:

* ``id``: ``mcp_`` + ``<server>_<tool>`` with every whitespace and ``-`` written
  as ``_``, every other character outside ``[A-Za-z0-9_]`` dropped, lower-cased
  and cut to 64 characters (a collision gets a ``_<n>`` suffix inside those 64).
  This is the name the model's own tool list carries.
* ``displayName``: ``@<server>/<tool>``, lower-cased.

A bare tool name (``work_report``) is neither, so KAS answers it with
``Tool "work_report" is not available.`` without ever calling the server. Kiro
Crew's own instructions name its tools bare, and KAS's ``tool_search`` result does
too (``tool_name``, with "invoke the tool using ONLY the tool_name value"), which is
how a model on KAS comes to call a name KAS cannot resolve.

Read off the KAS bundle kiro-cli 2.21.1 ships
(``@kiro/agent/dist/server/acp-server.js``: the stream handler's
``r.find(L => L.id === name) ?? r.find(L => L.displayName === name)``, and the MCP
tool wrapper's ``sanitizedUniqueName`` / ``@${server}/${tool}`` builders).

Importing it costs only the stdlib, so the context builder and the dashboard can both
use it without loading the ACP package; the one ACP-layer read
(:func:`is_kas_provider_label`'s label constant) happens when that function is called.
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
