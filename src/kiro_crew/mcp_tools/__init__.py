"""Per-domain MCP tool descriptors for the ``kirocrew-core`` server.

``build_tool_list`` is what ``mcp_core._list_tools`` answers ``tools/list``
from. Domain modules are imported lazily inside it so this package stays a
leaf at import time: ``mcp_core`` reads ``_limits`` from here at module
level, and an eager import of the domain modules would close that loop.

Adding a tool means adding its descriptor to the domain module and its
handler to ``mcp_core``; nothing here needs to change. A domain whose tools
belong to something that can be switched off declares ALL of them in
``schemas()`` -- the half ``test_mcp_tool_registry`` holds against ``HANDLERS``
-- and narrows what ``tools/list`` emits with an ``advertised()`` function;
``apps`` is the one domain that does.
"""

from __future__ import annotations

import importlib
from typing import Any

# Descriptor modules, in the order their tools are advertised.
DOMAIN_MODULES: tuple[str, ...] = (
    "spawn",
    "learn",
    "ledger",
    "skills",
    "logs",
    "control",
    "messaging",
    "artifacts",
    "knowledge",
    "sessions",
    "workflows",
    "apps",
    "browser",
)


def build_tool_list() -> list[dict[str, Any]]:
    """Every ``kirocrew-core`` tool descriptor ``tools/list`` emits, concatenated by domain.

    Descriptors are rebuilt per call rather than cached: some carry a live
    value (the concurrent sub-agent cap), and a cache would pin the first
    reading for the life of the server process. A domain's ``advertised()``,
    when it defines one, is read in place of its ``schemas()`` -- that is where
    the ``apps`` domain drops the tools of an app that is switched off.
    """
    tools: list[dict[str, Any]] = []
    for name in DOMAIN_MODULES:
        # Imported here, not at module scope. Every domain module imports
        # ``mcp_core``, and ``mcp_core`` imports this package -- so hoisting these
        # to the top would close that loop and turn it into an import-time
        # failure on the gateway boot path. The laziness is load-bearing.
        module = importlib.import_module(f"{__name__}.{name}")
        advertised = getattr(module, "advertised", None)
        tools.extend(advertised() if advertised is not None else module.schemas())
    return tools


def dispatch(name: str, args: dict[str, Any]) -> str:
    """Run the handler for *name*, or report the tool as unknown.

    Domains are searched in the order they are advertised. A name is claimed by
    exactly one domain -- ``test_mcp_tool_registry`` fails on a collision -- so
    the order decides nothing beyond how soon the lookup stops.
    """
    for domain in DOMAIN_MODULES:
        module = importlib.import_module(f"{__name__}.{domain}")
        handler = module.HANDLERS.get(name)
        if handler is not None:
            return handler(name, args)
    return f"Unknown tool: {name}"
