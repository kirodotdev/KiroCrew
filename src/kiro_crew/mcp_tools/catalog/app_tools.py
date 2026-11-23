"""Tool declarations of the ``app_tools`` domain of ``kirocrew-core``.

The handlers are in :mod:`kiro_crew.mcp_tools.app_tools`, which reads every name here as
its own. This module imports none of them (see the package docstring).
"""

from __future__ import annotations

import functools
import importlib
from typing import Any

from kiro_crew.apps import app_agent_tools
from kiro_crew.apps.manager import app_enabled_state
from kiro_crew.mcp_tools.catalog import loaded_binding

_DOMAIN = "app_tools"


@functools.lru_cache(maxsize=1)
def _core_tool_names() -> frozenset[str]:
    """Every name the other domains declare. Names are static; descriptions are not read."""
    from kiro_crew.mcp_tools import DOMAIN_MODULES, _schemas_accepts_names_only

    names: set[str] = set()
    for domain in DOMAIN_MODULES:
        if domain == _DOMAIN:
            continue
        schemas = importlib.import_module(f"kiro_crew.mcp_tools.catalog.{domain}").schemas
        for spec in schemas(names_only=True) if _schemas_accepts_names_only(schemas) else schemas():
            names.add(str(spec.get("name", "")))
    return frozenset(names)


#: The gateway route that answers with the approved third-party tools (strict internal).
_LIST_PATH = "/api/app-tools/list"


def _tools() -> list[app_agent_tools.DeclaredTool]:
    """The first-party apps' tools, read in this process, minus any name a core tool has."""
    core = _core_tool_names()
    return [t for t in app_agent_tools.declared_tools() if t.name not in core]


def _third_party_tools() -> list[app_agent_tools.DeclaredTool]:
    """The approved third-party tools, as the gateway answers them, minus core names.

    The owner's approvals live in a store the session sandbox masks, which is where this
    server usually runs, so the gateway runs the gated reader and this server lists
    what it answers. Inside the gateway, use that reader without an HTTP round trip.
    Any failure lists nothing; the gateway checks each call again.
    """
    from kiro_crew.mcp_caller import current_caller

    caller = current_caller()
    try:
        # In-process callers have no wire payload; pooled stdio callers carry ``raw``
        # even though their gateway-injected identity also sets ``from_gateway``.
        if caller is not None and caller.from_gateway and not caller.raw:
            rows: Any = [app_agent_tools.tool_row(t) for t in app_agent_tools.third_party_tools()]
        else:
            # Import only the client, not the handlers, before the server's first list.
            from kiro_crew.mcp_core_parts import gateway  # noqa: PLC0415

            resp = gateway._get(_LIST_PATH)
            rows = resp.get("tools") if isinstance(resp, dict) else None
    except Exception:  # noqa: BLE001 - an unavailable reader lists no third-party tool
        return []
    core = _core_tool_names()
    first_party = {t.name for t in _tools()}
    found: list[app_agent_tools.DeclaredTool] = []
    for row in rows if isinstance(rows, list) else ():
        tool = app_agent_tools.tool_from_row(row)
        if tool is not None and tool.name not in core and tool.name not in first_party:
            found.append(tool)
    return found


def _universe() -> list[app_agent_tools.DeclaredTool]:
    core = _core_tool_names()
    return [t for t in app_agent_tools.shipped_tools() if t.name not in core]


def schemas() -> list[dict[str, Any]]:
    """Every tool a shipped first-party app declares, whether or not it is installed or enabled.

    An app agent's ``managedToolPolicy.exclude`` is built from these names once, when the
    agent is materialized, so a tool must be named before its app is installed.
    """
    return [t.decl.descriptor() for t in _universe()]


def advertised() -> list[dict[str, Any]]:
    """The installed first-party apps' tools and the approved third-party ones, minus
    those of an app readably switched off.

    Hidden only on a readable ``False``, and listed on a read that fails: the gateway
    refuses a call to a disabled app either way, while hiding on a transient fault
    would last the session's whole life.
    """
    hidden: set[str] = set()
    tools = [*_tools(), *_third_party_tools()]
    for app in sorted({t.app for t in tools}):
        try:
            state = loaded_binding(
                "kiro_crew.mcp_tools.app_tools", "app_enabled_state", app_enabled_state
            )(app)
        except Exception:
            continue
        if state is False:
            hidden.update(t.name for t in tools if t.app == app)
    return [t.decl.descriptor() for t in tools if t.name not in hidden]
