"""No core MCP tool schema may carry a shape Gemini refuses.

Gemini answers HTTP 400 for the WHOLE request, every tool included, when one
tool's ``inputSchema`` holds either:

- a ``type`` array beside object keywords (``properties``, ``required``,
  ``additionalProperties``, ``items``) -- spell it ``anyOf`` instead; or
- an ``enum`` listing the empty string -- name the cleared state instead.

A bare union with no object keyword beside it (``["string", "null"]``) is
accepted, so it is not flagged.
"""

from __future__ import annotations

import importlib
from typing import Any, Callable, Iterator

import pytest

from kiro_crew.mcp_tools import DOMAIN_MODULES

_OBJECT_KEYWORDS = frozenset({"properties", "required", "additionalProperties", "items"})


def _core_domain(name: str) -> Callable[[], list[dict[str, Any]]]:
    return lambda: importlib.import_module(f"kiro_crew.mcp_tools.{name}").schemas()


def _attr(module: str, name: str, *args: Any) -> Callable[[], list[dict[str, Any]]]:
    return lambda: getattr(importlib.import_module(module), name)(*args)


_SOURCES: dict[str, Callable[[], list[dict[str, Any]]]] = {
    **{f"kirocrew-core:{d}": _core_domain(d) for d in DOMAIN_MODULES},
    "kirocrew-core": _attr("kiro_crew.mcp_core", "_list_tools"),
    "kirocrew-cron": _attr("kiro_crew.mcp_cron_tool_list", "_list_tools"),
    "kirocrew-dashboard": _attr("kiro_crew.mcp_dashboard", "_list_tools"),
    "secrets": _attr("kiro_crew.mcp_secrets", "_list_tools"),
    "guide": _attr("kiro_crew.mcp_guide", "_tool_definitions"),
    "debug": _attr("kiro_crew.mcp_debug", "_tool_definitions"),
    "computer": _attr("kiro_crew.mcp_computer", "_tool_definitions"),
    "panel": _attr("kiro_crew.mcp_panel", "_tool_definitions"),
    "crew-log": _attr("kiro_crew.mcp_crew_log", "_tool_definitions"),
    "mate": _attr("kiro_crew.mcp_mate", "_tool_definitions"),
    "work": _attr("kiro_crew.mcp_work", "_tool_definitions", True),
    "panel-snapshot": lambda: [
        importlib.import_module("kiro_crew.mcp_panel_snapshot").TOOL_DEFINITION
    ],
    "kirocrew-dashboard+crew": lambda: importlib.import_module(
        "kiro_crew.mcp_session_crew"
    ).with_crew_schema(importlib.import_module("kiro_crew.mcp_dashboard")._list_tools(), True),
    "remote-workspace": _attr("kiro_crew.remote_session.workspace_tools", "tool_specs", "."),
}


def _offences(node: Any, path: str) -> Iterator[str]:
    if isinstance(node, dict):
        kind = node.get("type")
        if isinstance(kind, list) and len(kind) > 1 and _OBJECT_KEYWORDS & node.keys():
            yield f"{path}: type union {kind} beside {sorted(_OBJECT_KEYWORDS & node.keys())}"
        enum = node.get("enum")
        if isinstance(enum, list) and "" in enum:
            yield f"{path}: enum {enum} lists the empty string"
        for key, value in node.items():
            yield from _offences(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _offences(value, f"{path}[{i}]")


@pytest.mark.parametrize("source", sorted(_SOURCES))
def test_no_tool_schema_carries_a_shape_gemini_refuses(source: str) -> None:
    tools = _SOURCES[source]()
    assert tools, f"{source} listed no tools; this test is reading the wrong place"
    found = [
        f"{tool['name']}{offence}"
        for tool in tools
        for offence in _offences(tool.get("inputSchema") or {}, "")
    ]
    assert not found, "\n".join(found)


@pytest.mark.parametrize(
    "schema",
    [
        {"type": ["string", "object"], "properties": {"task": {"type": "string"}}},
        {"type": "object", "properties": {"mode": {"type": "string", "enum": ["", "auto"]}}},
    ],
)
def test_the_walker_flags_both_shapes(schema: dict[str, Any]) -> None:
    assert list(_offences(schema, ""))


def test_the_walker_leaves_a_bare_union_alone() -> None:
    assert not list(_offences({"type": ["string", "null"]}, ""))
