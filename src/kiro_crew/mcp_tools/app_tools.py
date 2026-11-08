"""The tools first-party apps declare in their manifests, listed and forwarded, never run here.

``apps.app_agent_tools`` reads each first-party app's ``agentTools`` block. This domain
turns it into ``kirocrew-core`` tools without running any app code in this process,
which holds the gateway's internal secret:

- ``schemas()`` lists every tool a shipped first-party manifest declares, installed or
  not: it is the names build an app agent's deny universe reads, and that universe is
  fixed when the agent is materialized. ``advertised()`` lists the tools of the
  installed first-party apps that are not switched off (the same rule, and the same
  fail-open read, as ``apps.advertised``);
- ``HANDLERS`` answers each declared name with ONE core handler, :func:`app_agent_tool`,
  which posts the validated call to the gateway's ``/api/app-tools/call`` and returns the redacted answer cut at the tool's declared cap.

It is the LAST domain in ``DOMAIN_MODULES``, and it drops any declared name another
domain already has: a core tool always keeps its name, its handler and its schema.
"""

from __future__ import annotations

import functools
import importlib
import json
from collections.abc import Iterator, Mapping
from typing import Any

from kiro_crew import mcp_core
from kiro_crew.apps import app_agent_tools
from kiro_crew.apps.manager import app_enabled_state
from kiro_crew.platform import redact_via_context as redact

_DOMAIN = "app_tools"
#: The gateway route every forwarded call reaches: strict internal, so only a process
#: holding the internal secret may call it (``dashboard/handlers/app_agent_tools.py``).
_CALL_PATH = "/api/app-tools/call"


@functools.lru_cache(maxsize=1)
def _core_tool_names() -> frozenset[str]:
    """Every name the other domains declare. Names are static; descriptions are not read."""
    from kiro_crew.mcp_tools import DOMAIN_MODULES, _schemas_accepts_names_only

    names: set[str] = set()
    for domain in DOMAIN_MODULES:
        if domain == _DOMAIN:
            continue
        schemas = importlib.import_module(f"kiro_crew.mcp_tools.{domain}").schemas
        for spec in schemas(names_only=True) if _schemas_accepts_names_only(schemas) else schemas():
            names.add(str(spec.get("name", "")))
    return frozenset(names)


def _tools() -> list[app_agent_tools.DeclaredTool]:
    core = _core_tool_names()
    return [t for t in app_agent_tools.declared_tools() if t.name not in core]


def _universe() -> list[app_agent_tools.DeclaredTool]:
    core = _core_tool_names()
    return [t for t in app_agent_tools.shipped_tools() if t.name not in core]


def declared(name: str) -> app_agent_tools.DeclaredTool | None:
    """The app tool called *name*, or ``None`` when no app owns that name."""
    if name in _core_tool_names():
        return None
    return app_agent_tools.declared_tool(name)


def schemas() -> list[dict[str, Any]]:
    """Every tool a shipped first-party app declares, whether or not it is installed or enabled.

    An app agent's ``managedToolPolicy.exclude`` is built from these names once, when the
    agent is materialized, so a tool must be named before its app is installed.
    """
    return [t.decl.descriptor() for t in _universe()]


def advertised() -> list[dict[str, Any]]:
    """The installed first-party apps' tools, minus those of an app readably switched off.

    Hidden only on a readable ``False``, and listed on a read that fails, exactly as
    ``apps.advertised`` decides for the core's own app tools: the gateway refuses a
    call to a disabled app either way, while hiding on a transient fault would last
    the session's whole life.
    """
    hidden: set[str] = set()
    tools = _tools()
    for app in sorted({t.app for t in tools}):
        try:
            state = app_enabled_state(app)
        except Exception:
            continue
        if state is False:
            hidden.update(t.name for t in tools if t.app == app)
    return [t.decl.descriptor() for t in tools if t.name not in hidden]


def tool_schema(name: str) -> Any:
    """The validation schema of app tool *name*, or ``None`` when it is not one."""
    tool = declared(name)
    return app_agent_tools.build_tool_schema(tool) if tool is not None else None


def app_agent_tool(name: str, args: dict[str, Any]) -> str:
    """Forward one call of an app's declared tool to the gateway, which hands it to the app.

    *args* has already passed the declared schema (``mcp_core._validate_args`` reads
    :func:`tool_schema`). The gateway checks it again, resolves the app that declares
    *name* and posts ``{"tool", "arguments"}`` to that app's declared route. The answer
    echoes app state no inbound scrub touched, so it is redacted here, then cut at the
    tool's declared cap: redacting first means a cut can never split a credential.
    """
    tool = declared(name)
    if tool is None:
        return f"Error: no installed app declares the tool {name!r}."
    resp = mcp_core._post(_CALL_PATH, {"tool": name, "arguments": args})
    text = redact(json.dumps(resp, ensure_ascii=False, default=str))
    cap = tool.decl.max_result_chars
    if len(text) > cap:
        text = text[:cap] + f"\n… truncated ({len(text)} chars total)."
    return text


class _DeclaredHandlers(Mapping[str, Any]):
    """``HANDLERS`` for a tool set read from manifests: every name ``schemas()`` lists maps to one handler.

    A tool whose app is not installed has the handler too, which answers with an error
    and posts nothing.
    """

    def __getitem__(self, name: str) -> Any:
        if name not in self:
            raise KeyError(name)
        return app_agent_tool

    def __contains__(self, name: object) -> bool:
        return any(t.name == name for t in _universe())

    def __iter__(self) -> Iterator[str]:
        return iter([t.name for t in _universe()])

    def __len__(self) -> int:
        return len(_universe())


HANDLERS: Mapping[str, Any] = _DeclaredHandlers()
