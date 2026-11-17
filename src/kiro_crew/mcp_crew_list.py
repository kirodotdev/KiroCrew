"""``crew_list``: the MCP half of the remote-dispatch crew roster (audit D1).

The gateway route ``GET /api/session-control/crews``
(``dashboard/handlers/remote_crew_list.py``) gates the call and reads every crew;
this body only renders its rows. The tool is advertised only while
``work_ledger.remote_dispatch`` is on (:func:`with_crew_list`).
"""

from __future__ import annotations

from typing import Any

from kiro_crew.mcp_tools.dashboard_client import DashboardError
from kiro_crew.mcp_tools.table import Tool, ToolContext
from kiro_crew.platform import redact_via_context as redact

NAME = "crew_list"

ROUTE = "/api/session-control/crews"

#: Past the route's slowest per-crew read (``ROSTER_TIMEOUT_S``, 22 s, in
#: ``dashboard/handlers/remote_crew_list.py``); the client's default is 10 s.
GET_TIMEOUT_S = 30.0


def _number(value: object, unit: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "?"
    if isinstance(value, int):
        return f"{value}{unit}"
    return f"{value:g}{unit}"


def _line(row: dict[str, Any]) -> str:
    name, cid = row.get("name") or "", row.get("id") or ""
    head = f"- `{name}` (crew=`{cid}`): {row.get('crew_state') or 'unknown'}"
    # The verdict comes from the version gate, the text from a separate read:
    # either can be missing without the other.
    flag = row.get("version_match")
    if flag is not None or row.get("version"):
        match = "matches" if flag is True else "MISMATCH" if flag is False else "unknown"
        text = f"v{row['version']} " if row.get("version") else ""
        head += f", version {text}({match})"
    room = row.get("headroom")
    if isinstance(room, dict):
        head += (
            f"; mem {_number(room.get('mem_free_gb'), ' GB')} free, "
            f"disk {_number(room.get('disk_free_gb'), ' GB')} free, "
            f"load/cpu {_number(room.get('load_per_cpu'), '')}, "
            f"posture {room.get('posture') or 'unknown'}"
        )
    head += (
        f"; open_workers {_number(row.get('open_workers'), '')}, "
        f"bound_here {_number(row.get('bound_here'), '')}"
    )
    parts = [head]
    agents = [a for a in row.get("agents") or [] if isinstance(a, str)]
    if agents:
        default = row.get("default_agent") or ""
        parts.append(
            f"  agents: {', '.join(agents)}" + (f" (default {default})" if default else "")
        )
    models = [m for m in row.get("models") or [] if isinstance(m, str)]
    if models:
        parts.append(f"  models: {', '.join(models)}")
    missing = [m for m in row.get("unavailable") or [] if isinstance(m, str)]
    if missing:
        parts.append(f"  unread: {', '.join(missing)}")
    return "\n".join(parts)


def run_crew_list(args: dict[str, Any], ctx: ToolContext) -> str:
    try:
        resp = ctx.client.get(ROUTE, session_key=ctx.caller_key, timeout=GET_TIMEOUT_S)
    except DashboardError as refused:
        return redact(f"Error: could not list crews: {refused.error}")
    rows = [r for r in resp.get("crews") or [] if isinstance(r, dict)]
    omitted = resp.get("omitted")
    omitted = omitted if isinstance(omitted, int) and not isinstance(omitted, bool) else 0
    if not rows and not omitted:
        return "No remote crews are registered on this hub."
    lines = [f"\U0001f5a5\ufe0f {len(rows) + omitted} crew(s) registered on this hub:"]
    lines += [_line(r) for r in rows]
    if omitted:
        lines.append(f"{omitted} more crew(s) are registered and not listed (the list is capped).")
    lines.append(
        "Pass a crew's id as crew= on session_create, or omit crew with work_item= and "
        "the hub picks by headroom. Headroom is a snapshot; the crew's own gate decides."
    )
    return redact("\n".join(lines))


def crew_list_tool() -> Tool:
    """The ``crew_list`` row, a session-control read on the strict gate."""
    return Tool(
        name=NAME,
        description=(
            "Preview. List the remote crews registered on this hub: id, name, connection "
            "state, version match, a headroom snapshot (free memory and disk, load, "
            "open hub workers), and the crew's agent and model names. Read-only. "
            "Before your first crew or work_item call, read "
            "references/remote-dispatch.md in the goal-conductor skill's directory "
            "and follow it."
        ),
        schema={"type": "object", "properties": {}},
        run=run_crew_list,
        identity="strict",
        routes=(f"GET {ROUTE}",),
    )


def with_crew_list(tools: list[dict[str, Any]], enabled: bool) -> list[dict[str, Any]]:
    """*tools* without ``crew_list`` while remote dispatch is off."""
    return tools if enabled else [t for t in tools if t.get("name") != NAME]
