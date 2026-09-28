"""Display titles for the tools of Kiro Crew's own MCP servers.

One table, ``data/mcp_tool_titles.json``, keyed by server then tool name. The
servers add each title to their ``tools/list`` descriptors as the standard MCP
``Tool.title``; the dashboard and the channel task label read the same file for
a tool-call row's title, so a row, replayed or live, never waits on the backend
relaying the field. Keyed by server so a third-party tool that shares a name
keeps its own humanised title.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Any

_TABLE_FILE = Path(__file__).resolve().parent / "data" / "mcp_tool_titles.json"


@functools.cache
def _table() -> dict[str, dict[str, str]]:
    return json.loads(_TABLE_FILE.read_text(encoding="utf-8"))


def declared_tool_title(server: str, tool: str) -> str:
    """The declared title of *tool* on *server*, or ``""`` when none is declared."""
    return _table().get(server, {}).get(tool, "")


def with_titles(server: str, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """*tools* with ``title`` filled from the table where one is declared.

    A descriptor that gains a title is copied, so a domain that hands out a
    shared descriptor keeps its own shape; any other is returned as is.
    """
    out: list[dict[str, Any]] = []
    for tool in tools:
        title = declared_tool_title(server, str(tool.get("name", "")))
        out.append({**tool, "title": title} if title and "title" not in tool else tool)
    return out
