"""``worker_capabilities``: the MCP half of the before-dispatch worker lookup.

The gateway route ``GET /api/session-control/worker-capabilities``
(``dashboard/handlers/worker_capabilities.py``) resolves the worker the caller's
next ``session_create`` would open and reads its MCP servers; this body renders the
answer as one or two short lines. Read-only.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from kiro_crew.mcp_tools.dashboard_client import DashboardError
from kiro_crew.mcp_tools.table import Tool, ToolContext

NAME = "worker_capabilities"

ROUTE = "/api/session-control/worker-capabilities"


def _names(resp: dict[str, Any], key: str) -> str:
    names = [n for n in resp.get(key) or [] if isinstance(n, str)]
    more = resp.get(f"{key}_more")
    text = ", ".join(names) or "none"
    if isinstance(more, int) and not isinstance(more, bool) and more > 0:
        text += f", +{more} more"
    return text


def render(resp: dict[str, Any], query: str = "") -> str:
    """The lookup answer as text: the server names, or a yes/no with the matches."""
    agent = str(resp.get("agent") or "")
    who = f"Worker ({agent})" if agent else "Worker"
    if not query:
        return f"{who} MCP servers: {_names(resp, 'servers')}."
    if resp.get("worker_has"):
        line = f"{who} has {query!r}: yes ({_names(resp, 'matches')})."
    else:
        line = f"{who} has {query!r}: no."
    if resp.get("gateway_only"):
        line += f" This gateway has it, your worker lacks it: {_names(resp, 'gateway_only')}."
    return line


def run_worker_capabilities(args: dict[str, Any], ctx: ToolContext) -> str:
    params = {k: str(args[k]) for k in ("agent", "backend", "query") if args.get(k)}
    path = f"{ROUTE}?{urlencode(params)}" if params else ROUTE
    try:
        resp = ctx.client.get(path, session_key=ctx.caller_key)
    except DashboardError as refused:
        return f"Error: could not read your worker's tools: {refused.error}"
    return render(resp, str(args.get("query") or "").strip())


def worker_capabilities_tool() -> Tool:
    """The ``worker_capabilities`` row, a session-control read on the strict gate."""
    return Tool(
        name=NAME,
        description=(
            "Check, BEFORE you dispatch or tell the user something is unavailable, "
            "which MCP servers the worker your next session_create or work_dispatch "
            "would open has. No query: its server names. query (a server name or "
            "keyword, e.g. 'slack'): yes or no, the matching servers or tools, and "
            "any this gateway has that the worker lacks. Read-only."
        ),
        schema={
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "description": (
                        "The worker's agent, as you would pass it to session_create. "
                        "Omit for the one session_create would pick."
                    ),
                },
                "backend": {
                    "type": "string",
                    "description": "The worker's harness, as you would pass it to session_create.",
                },
                "query": {
                    "type": "string",
                    "description": "A server name or keyword to check for.",
                },
            },
        },
        run=run_worker_capabilities,
        identity="strict",
        routes=(f"GET {ROUTE}",),
    )
