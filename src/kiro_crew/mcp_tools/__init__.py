"""Per-domain MCP tool descriptors for the ``kirocrew-core`` server.

``build_tool_list`` is what ``mcp_core._list_tools`` answers ``tools/list``
from. Domain modules are imported lazily inside it so this package stays a
leaf at import time: ``mcp_core`` reads ``_limits`` from here at module
level, and an eager import of the domain modules would close that loop.

Adding a tool means adding its descriptor to the domain module's
``schemas()`` and its handler to that module's ``HANDLERS``; nothing here needs
to change. A domain whose tools belong to something that can be switched off
declares ALL of them in ``schemas()`` -- the half ``test_mcp_tool_registry``
holds against ``HANDLERS`` -- and narrows what the full ``tools/list`` emits with
an ``advertised()`` function; ``apps`` is the one domain that does. The
names-only build (``build_tool_names``) reads ``schemas()``, never
``advertised()``, so it performs no enablement read.

Two modules in this package are not domains, and nothing here imports them:
``table`` (:class:`~kiro_crew.mcp_tools.table.ToolTable`, the one-row-per-tool
server shape ``kirocrew-dashboard`` is built on) and ``dashboard_client`` (the
``DashboardClient`` port a table's tools reach the gateway through).
"""

from __future__ import annotations

import importlib
import inspect
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


def build_tool_list(*, names_only: bool = False) -> list[dict[str, Any]]:
    """Every ``kirocrew-core`` tool descriptor ``tools/list`` emits, concatenated by domain.

    Descriptors are rebuilt per call rather than cached: some carry a live
    value (the concurrent sub-agent cap), and a cache would pin the first
    reading for the life of the server process. A domain's ``advertised()``,
    when it defines one, is read in place of its ``schemas()`` on the full build
    -- that is where the ``apps`` domain drops the tools of an app that is
    switched off.

    ``names_only`` is the read path for a caller that keeps only tool NAMES and
    discards every description (``build_tool_names`` / in-process discovery). The
    two builders that reach for a live value to fill a description --
    ``spawn.schemas`` (a user-level agents directory scan) and ``control.schemas``
    (a config read) -- are told not to, so a names-only read never performs that
    work. The names and their order are identical to a full build with every app
    enabled; only the descriptions differ (empty under ``names_only``), and the
    caller discards those anyway. This is what lets those builders carry NO
    ``get_running_loop`` skip of their own: asking for descriptors does not pull
    in the live reads. The same rule covers ``advertised()``: the names-only path
    never consults it, so no app's enablement record is read there either -- it
    lists every name a domain DECLARES, including the tools of a disabled app.
    """
    tools: list[dict[str, Any]] = []
    for name in DOMAIN_MODULES:
        # Imported here, not at module scope. Every domain module imports
        # ``mcp_core``, and ``mcp_core`` imports this package -- so hoisting these
        # to the top would close that loop and turn it into an import-time
        # failure on the gateway boot path. The laziness is load-bearing.
        module = importlib.import_module(f"{__name__}.{name}")
        # Only the two builders with a live-valued description accept the flag;
        # the rest build static descriptors cheaply and are called unchanged.
        schemas = module.schemas
        if names_only:
            tools.extend(
                schemas(names_only=True) if _schemas_accepts_names_only(schemas) else schemas()
            )
            continue
        advertised = getattr(module, "advertised", None)
        tools.extend(advertised() if advertised is not None else schemas())
    return tools


def build_tool_names() -> list[str]:
    """Ordered ``kirocrew-core`` tool names, WITHOUT assembling descriptions.

    Returns exactly the names ``build_tool_list`` would, in the same order, but
    takes the ``names_only`` path so a names-only caller never triggers the
    live-value reads some descriptions carry. ``mcp_discovery`` reads this when it
    wants only names, which is every time it reaches the in-process path.
    """
    return [
        name
        for tool in build_tool_list(names_only=True)
        if isinstance(tool, dict) and (name := tool.get("name"))
    ]


def _schemas_accepts_names_only(schemas: Any) -> bool:
    """True when a domain's ``schemas`` takes a ``names_only`` keyword.

    Only the builders with a live-valued description grow the parameter; the
    rest keep their zero-argument signature. Detected rather than assumed so a
    new live-valued builder opts in by adding the keyword, with no registry here
    to keep in sync.
    """
    try:
        return "names_only" in inspect.signature(schemas).parameters
    except (TypeError, ValueError):
        return False


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
