"""Read-only tool diagnostics from the metadata event ledger.

The web UI runs in a different process from ACP. This report is historical,
not a live authorization source, and never claims that a tool is enabled merely
because it appeared in a past snapshot. Credentials and arguments are excluded.
"""
from __future__ import annotations
from typing import Any


def tool_center_report(native_tools: list[str], events: list[dict[str, Any]]) -> dict[str, Any]:
    latest: dict[str, dict[str, Any]] = {}
    blocked: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        kind = str(event.get("event") or "")
        data = event.get("data") or {}
        if not isinstance(data, dict):
            continue
        if kind == "crew.capabilities.changed":
            session = str(event.get("session_id") or "")
            if not session:
                continue
            names = data.get("tool_names") or []
            # Compatibility with historical events that only stored counts.
            valid = sorted(set(x for x in names if isinstance(x, str) and x.startswith("mcp__")))[:200]
            latest[session] = {
                "session": session,
                "timestamp": event.get("timestamp", 0),
                "tools": valid,
                "tool_count": int(data.get("tool_count") or 0),
                "verified_servers": list(data.get("verified_crew_servers") or []),
                "pending_servers": list(data.get("pending_crew_servers") or []),
                "failed_servers": list(data.get("failed_servers") or []),
                "truncated": bool(data.get("tool_names_truncated")),
            }
        if kind == "tool.dispatch.blocked" and data.get("kind") == "mcp":
            blocked.append({
                "session": str(event.get("session_id") or ""),
                "timestamp": event.get("timestamp", 0),
                "tool": str(data.get("tool") or "")[:160],
                "reason": str(data.get("reason") or "unknown")[:80],
            })
    snapshots = sorted(latest.values(), key=lambda x: float(x["timestamp"] or 0), reverse=True)[:12]
    return {
        "native_tools": sorted(native_tools),
        "session_snapshots": snapshots,
        "recent_blocks": blocked[-30:][::-1],
        "read_only": True,
        "live_permission_state": False,
        "note": "Recorded session snapshots, not live permission grants. Crew is the sole authorization authority.",
    }
