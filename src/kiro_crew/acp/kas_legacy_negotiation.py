"""Pin Crew's own stdio MCP servers to legacy version negotiation on KAS.

Split out of :mod:`kiro_crew.acp.kas_agents` for its line cap; that module keeps
the import path (``kas_agents._pin_legacy_negotiation``).
"""

from __future__ import annotations

from typing import Any

from kiro_crew.mcp_cleanup import KIROCREW_BIN_MCP_SERVERS

_MANAGED_MCP_SERVER_NAMES = frozenset(KIROCREW_BIN_MCP_SERVERS)


def _pin_legacy_negotiation(servers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """*servers* with Crew's own stdio servers pinned to legacy version negotiation.

    Crew's servers speak the classic ``initialize`` handshake only, and under the
    host's default negotiation each one is cold-started twice per session.
    ``legacy`` starts it once. A server Crew does not own keeps the host's
    default. Copies, never mutates: the caller's entries are shared.
    """
    out: list[dict[str, Any]] = []
    for element in servers:
        if (
            isinstance(element, dict)
            and element.get("name") in _MANAGED_MCP_SERVER_NAMES
            and element.get("command")
        ):
            meta = element.get("_meta")
            meta = meta if isinstance(meta, dict) else {}
            kiro = meta.get("kiro")
            kiro = {**(kiro if isinstance(kiro, dict) else {}), "versionNegotiation": "legacy"}
            element = {**element, "_meta": {**meta, "kiro": kiro}}
        out.append(element)
    return out
