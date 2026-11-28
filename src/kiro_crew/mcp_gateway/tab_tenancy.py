"""A browser tab per caller on one shared browser MCP server.

A browser server (playwright-mcp) acts on ONE "current" tab per process, so on a
shared backend chat B's navigate moved chat A's page. With every call already
serialized (``call_gate``), the gateway gives each caller its own tab in its own
isolated context and makes it current before the caller's call runs:

* engaged by tool SHAPE: a tabs tool whose ``action`` enum holds
  list/new/close/select and whose schema has ``isolatedContext``
  (:func:`find_tabs_tool`); its sibling ``<prefix>close`` closes the browser;
* a tab's owner is read from the server's own listing, which labels every tab
  of a named context ``[isolatedContext: <name>]``; the name is derived from the
  caller's attested session key (:func:`context_name`), so a popup opened from
  a caller's tab is that caller's too;
* a caller's tab indices are its own (0..n-1) and are mapped to the server's;
  a listing in a reply shows only the caller's tabs, renumbered;
* closing the browser closes only the caller's tabs.

Chats seeing each other's pages is allowed (one local user); this is about the
right page answering the right chat, not isolation.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Optional

_TAB_LINE = re.compile(
    r"^- (?P<index>\d+): (?P<current>\(current\) )?\[.*\]\(.*\)"
    r"(?: \[isolatedContext: (?P<ctx>[^\]]+)\])?\s*$"
)
_ACTIONS = frozenset({"list", "new", "close", "select"})


def context_name(session_key: str) -> str:
    """The isolated context a caller's tabs live in: stable, short, not the key itself."""
    return "kc-" + hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:12]


def find_tabs_tool(tools: Any) -> Optional[tuple[str, str]]:
    """``(tabs tool, close tool)`` names when the listing has the tab shape, else ``None``."""
    # Server-authored JSON: only string names and enum members are compared, so
    # an object where a string belongs is a non-tab shape, never a TypeError.
    names = (
        {t["name"] for t in tools if isinstance(t, dict) and isinstance(t.get("name"), str)}
        if isinstance(tools, list)
        else set()
    )
    for tool in tools if isinstance(tools, list) else ():
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            continue
        schema = tool.get("inputSchema")
        props = schema.get("properties") if isinstance(schema, dict) else None
        if not isinstance(props, dict) or "isolatedContext" not in props or "index" not in props:
            continue
        action = props.get("action")
        enum = action.get("enum") if isinstance(action, dict) else None
        if not isinstance(enum, list) or not _ACTIONS <= {e for e in enum if isinstance(e, str)}:
            continue
        name = tool["name"]
        prefix = name.rsplit("_", 1)[0] + "_" if "_" in name else ""
        close = prefix + "close"
        return name, (close if close in names else "")
    return None


def single_page_browser(tools: Any) -> bool:
    """A browser server with one page and no caller-ownable tab (puppeteer-style).

    Read from the listing: a tool whose name ends in ``navigate`` and that takes
    a ``url``, on a server with no tabs tool of the :func:`find_tabs_tool` shape.
    """
    if not isinstance(tools, list) or find_tabs_tool(tools) is not None:
        return False
    for tool in tools:
        if not isinstance(tool, dict) or not str(tool.get("name", "")).endswith("navigate"):
            continue
        schema = tool.get("inputSchema")
        props = schema.get("properties") if isinstance(schema, dict) else None
        if isinstance(props, dict) and "url" in props:
            return True
    return False


@dataclass(frozen=True)
class Tab:
    index: int
    current: bool
    context: str


def parse_tabs(text: str) -> list[Tab]:
    """Every tab line in a listing or an ``### Open tabs`` section."""
    tabs = []
    for line in text.splitlines():
        m = _TAB_LINE.match(line)
        if m:
            tabs.append(Tab(int(m["index"]), bool(m["current"]), m["ctx"] or ""))
    return tabs


def own_tabs(tabs: list[Tab], ctx: str) -> list[Tab]:
    return [t for t in tabs if t.context == ctx]


def rewrite_listing(text: str, ctx: str, *, whole: bool = False) -> str:
    """*text* with every tab line not in *ctx* removed and the rest renumbered 0..n-1.

    Only lines under an ``### Open tabs`` heading are tab lines, or every line
    when *whole* (the tabs tool's own reply); page content that happens to look
    like one is left alone.
    """
    out, n = [], 0
    in_tabs = whole
    for line in text.split("\n"):
        if line.startswith("### "):
            in_tabs = whole or line.strip() == "### Open tabs"
        m = _TAB_LINE.match(line) if in_tabs else None
        if m is None:
            out.append(line)
            continue
        if (m["ctx"] or "") != ctx:
            continue
        out.append(f"- {n}: " + line[m.end("index") + 2 :])
        n += 1
    return "\n".join(out)


def result_text(msg: Any) -> str:
    """The joined text of a ``tools/call`` result, or ``""``."""
    result = msg.get("result") if isinstance(msg, dict) else None
    content = result.get("content") if isinstance(result, dict) else None
    if not isinstance(content, list):
        return ""
    return "\n".join(
        c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"
    )


def rewrite_reply(msg: dict[str, Any], ctx: str, *, whole: bool = False) -> dict[str, Any]:
    """*msg* with every text block's tab lines limited to *ctx*."""
    result = msg.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("content"), list):
        return msg
    content = result["content"]
    new_content = [
        (
            {**c, "text": rewrite_listing(c["text"], ctx, whole=whole)}
            if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str)
            else c
        )
        for c in content
    ]
    return {**msg, "result": {**result, "content": new_content}}


def map_index(local: Any, mine: list[Tab]) -> Optional[int]:
    """The server's index for the caller's *local* index, or ``None`` when out of range."""
    if isinstance(local, bool) or not isinstance(local, (int, float)) or int(local) != local:
        return None
    i = int(local)
    return mine[i].index if 0 <= i < len(mine) else None
