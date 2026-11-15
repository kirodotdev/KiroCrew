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


def _tools() -> list[app_agent_tools.DeclaredTool]:
    core = _core_tool_names()
    return [t for t in app_agent_tools.declared_tools() if t.name not in core]


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
    """The installed first-party apps' tools, minus those of an app readably switched off.

    Hidden only on a readable ``False``, and listed on a read that fails: the gateway
    refuses a call to a disabled app either way, while hiding on a transient fault
    would last the session's whole life.
    """
    hidden: set[str] = set()
    tools = _tools()
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
