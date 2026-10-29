"""``crew=`` on the five session tools: the MCP half (rfc-conductor-remote-dispatch.md, wave 1).

The gateway decides everything (switch, caller, crew, target fence, redaction) in
``dashboard/handlers/remote_dispatch_routes.py``; the bodies here only carry the
call on the same ``/api/session-control/*`` route the local verb uses and render
the answer. :func:`with_crew_schema` adds the ``crew`` argument to the advertised
schemas while ``work_ledger.remote_dispatch`` is on, and leaves them untouched
while it is off.
"""

from __future__ import annotations

import copy
from typing import Any
from urllib.parse import quote

from kiro_crew.mcp_tools.dashboard_client import DashboardError
from kiro_crew.mcp_tools.table import ToolContext
from kiro_crew.platform import redact_via_context as redact

#: The tools that take ``crew``.
CREW_TOOLS = (
    "session_create",
    "session_send",
    "session_read_message",
    "session_stop",
    "session_close",
)

_CREW_PROPERTY: dict[str, Any] = {
    "type": "string",
    "description": (
        "Preview. Run this on a connected remote crew instead of this machine: the "
        "crew's instance id or exact name. The session lives on that crew and shows "
        "in its sidebar group. With crew, session_create takes only title, agent and "
        "model (omit agent to get the crew's own default), and send, read, stop and "
        "close reach only a crew session YOU opened with session_create(crew=) on "
        "that same crew; pass the key it returned as target."
    ),
}


def with_crew_schema(tools: list[dict[str, Any]], enabled: bool) -> list[dict[str, Any]]:
    """*tools* with ``crew`` added to the five schemas when *enabled*, else unchanged."""
    if not enabled:
        return tools
    out = []
    for tool in tools:
        if tool.get("name") in CREW_TOOLS:
            tool = copy.deepcopy(tool)
            props = tool.setdefault("inputSchema", {}).setdefault("properties", {})
            props["crew"] = dict(_CREW_PROPERTY)
        out.append(tool)
    return out


def _on(resp: dict[str, Any]) -> str:
    name = resp.get("crew_name") or resp.get("crew") or ""
    return f"on crew `{name}`"


def run_crew_create(args: dict[str, Any], ctx: ToolContext) -> str:
    payload = {k: args[k] for k in ("crew", "title", "agent", "model") if k in args}
    extra = sorted(k for k in args if k not in payload and args.get(k) not in (None, ""))
    if extra:
        return f"Error: session_create with crew does not take {', '.join(extra)}."
    try:
        resp = ctx.client.post("/api/session-control/create", payload, session_key=ctx.caller_key)
    except DashboardError as refused:
        return redact(f"Error: could not create a session on that crew: {refused.error}")
    title = f" ({resp['title']})" if resp.get("title") else ""
    agent = f" bound to `{resp['agent']}`" if resp.get("agent") else ""
    note = f" The title was not set: {resp['title_error']}." if resp.get("title_error") else ""
    return redact(
        f"\U0001f195 Opened `{resp.get('target')}`{title} {_on(resp)}{agent}. It is empty "
        f"and waiting in that crew's sidebar group.{note} Pass crew and this key as "
        "target to session_send, session_read_message, session_stop and session_close."
    )


def run_crew_send(args: dict[str, Any], ctx: ToolContext) -> str:
    payload: dict[str, Any] = {
        "crew": args["crew"],
        "target": args["target"],
        "message": args["message"],
    }
    if args.get("steer"):
        payload["steer"] = True
    try:
        resp = ctx.client.post("/api/session-control/send", payload, session_key=ctx.caller_key)
    except DashboardError as refused:
        return redact(f"Error: could not send to that crew session: {refused.error}")
    if resp.get("steered") is True:
        how = "steered into its running turn"
    elif args.get("steer"):
        how = "queued (the crew could not steer its running turn)"
    else:
        how = "delivered (queued if busy)"
    return redact(f"\U0001f4e8 Message {how} to `{resp.get('target')}` {_on(resp)}.")


def run_crew_read(args: dict[str, Any], ctx: ToolContext) -> str:
    if args.get("since") is not None:
        return "Error: session_read_message with crew does not take since."
    query = (
        f"target={quote(str(args['target']))}&crew={quote(str(args['crew']))}"
        f"&limit={int(args.get('limit', 20))}"
    )
    try:
        resp = ctx.client.get(f"/api/session-control/read?{query}", session_key=ctx.caller_key)
    except DashboardError as refused:
        return redact(f"Error: could not read that crew session: {refused.error}")
    state = "still working" if resp.get("running") else "idle"
    lines = [
        f"\U0001f4d6 `{resp.get('target', '')}` {_on(resp)} — {resp.get('title', '')} ({state})"
    ]
    rows = [r for r in resp.get("messages") or [] if isinstance(r, dict)]
    if not rows:
        lines.append("No messages yet.")
    for row in rows:
        lines.append(f"[{row.get('role') or '?'}] {row.get('content') or ''}")
    return redact("\n".join(lines))


def run_crew_stop(args: dict[str, Any], ctx: ToolContext) -> str:
    try:
        resp = ctx.client.post(
            "/api/session-control/stop",
            {"crew": args["crew"], "target": args["target"]},
            session_key=ctx.caller_key,
        )
    except DashboardError as refused:
        return redact(f"Error: could not stop that crew session: {refused.error}")
    if resp.get("compacting") is True:
        return redact(
            f"\u2139\ufe0f `{resp.get('target')}` {_on(resp)} is compacting its context, so "
            "the crew did not stop it; send session_stop again to force the stop."
        )
    return redact(f"\U0001f6d1 Stop sent to `{resp.get('target')}` {_on(resp)}.")


def run_crew_close(args: dict[str, Any], ctx: ToolContext) -> str:
    try:
        resp = ctx.client.post(
            "/api/session-control/close",
            {"crew": args["crew"], "target": args["target"]},
            session_key=ctx.caller_key,
        )
    except DashboardError as refused:
        return redact(f"Error: could not close that crew session: {refused.error}")
    return redact(
        f"\U0001f5d1\ufe0f Closed `{resp.get('target')}` {_on(resp)}; that crew archives it "
        "to its own history."
    )
