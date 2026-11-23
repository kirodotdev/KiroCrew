"""Pin Crew's own stdio MCP servers to legacy version negotiation on KAS.

They also get kiro-cli's 120 s connect timeout there, and a pooled broker stub
gets its own server's. Split out of :mod:`kiro_crew.acp.kas_agents` for its line
cap; that module keeps the import path (``kas_agents._pin_legacy_negotiation``).
"""

from __future__ import annotations

from typing import Any

from kiro_crew.acp.kas_settings_mcp import KIRO_CLI_MCP_TIMEOUT_MS, _with_timeout
from kiro_crew.acp.mcp_mount_check import stub_server
from kiro_crew.mcp_cleanup import KIROCREW_BIN_MCP_SERVERS

_MANAGED_MCP_SERVER_NAMES = frozenset(KIROCREW_BIN_MCP_SERVERS)


def _pin_legacy_negotiation(
    servers: list[dict[str, Any]], stub_timeout_ms: int
) -> list[dict[str, Any]]:
    """*servers* with Crew's stdio servers on legacy negotiation and each stub's timeout set.

    Crew's servers speak the classic ``initialize`` handshake only, and under the
    host's default negotiation each one is cold-started twice per session.
    ``legacy`` starts it once. Each also gets ``timeout``: KAS gives a
    session-level server 60 s to connect unless its ``_meta.kiro`` names one, and
    a Crew server waiting in the gateway's spawn queue after a restart can take
    longer. The engine reads it from 0.66.111 on; 0.66.102 and older ignore it.

    A pooled broker stub of any other server gets only ``timeout``, the budget
    :func:`~kiro_crew.acp.kas_settings_mcp.finish_settings_mcp` gives that server
    in the agent block: the ``timeout`` (or ``timeoutMs``) the stub carries from
    its declaration, which KAS does not read on a session-level element, else
    *stub_timeout_ms*. Any other server keeps the host's defaults. Copies, never
    mutates: the caller's entries are shared.
    """
    out: list[dict[str, Any]] = []
    for element in servers:
        pin: dict[str, Any] = {}
        if (
            isinstance(element, dict)
            and element.get("name") in _MANAGED_MCP_SERVER_NAMES
            and element.get("command")
        ):
            pin = {"versionNegotiation": "legacy", "timeout": KIRO_CLI_MCP_TIMEOUT_MS}
        elif isinstance(element, dict) and stub_server(element):
            pin = {"timeout": _with_timeout(element, stub_timeout_ms)["timeout"]}
        if pin:
            meta = element.get("_meta")
            meta = meta if isinstance(meta, dict) else {}
            kiro = meta.get("kiro")
            kiro = {**(kiro if isinstance(kiro, dict) else {}), **pin}
            element = {**element, "_meta": {**meta, "kiro": kiro}}
        out.append(element)
    return out
