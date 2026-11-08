"""The ``resource_status`` block that reports the CALLER's own memory footprint.

Host headroom says memory is short; this says how much of it is the calling
session's. It reads ``GET /api/spawn/session-memory``, which answers the attested
caller about its own session, its sub-agents' shared runtime and the runs it
started, as counts and sizes only. Nothing about any other session is served, so
nothing about one can be printed here.

The caller is resolved through the strict identity gate: the lenient resolver's
``/proc`` walk can land on a PARENT slot, and this block would then describe the
parent's footprint as the caller's own.
"""

from __future__ import annotations

from collections.abc import Mapping

from kiro_crew import mcp_core

_MEMORY_HEADER = "Your session's memory"


def _memory_mb(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "unmeasured"
    return f"{float(value):.0f} MB"


def _memory_count(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, int):
        return "?"
    return str(value)


def _memory_runtime_line(label: str, row: Mapping[str, object]) -> str:
    if row.get("shared") is True:
        # The counts of a shared runtime are not the caller's own, so none are sent.
        return f"  {label}: {_memory_mb(row.get('rss_mb'))} (your share of a shared runtime)"
    return (
        f"  {label}: {_memory_mb(row.get('rss_mb'))}, {_memory_count(row.get('procs'))} processes, "
        f"{_memory_count(row.get('mcp'))} MCP servers"
    )


def render_session_memory(view: Mapping[str, object]) -> list[str]:
    """The block's lines for one ``/api/spawn/session-memory`` answer."""
    out = [f"{_MEMORY_HEADER} (upper bound; RSS counts shared pages in full):"]
    own = view.get("self")
    if isinstance(own, Mapping):
        out.append(_memory_runtime_line("This session", own))
    else:
        out.append("  This session: no live runtime of its own right now")
    companion = view.get("subagent_runtime")
    if isinstance(companion, Mapping):
        out.append(_memory_runtime_line("Sub-agent runtime", companion))
    runs = view.get("runs")
    if isinstance(runs, Mapping) and isinstance(runs.get("count"), int) and runs["count"]:
        line = (
            f"  Runs you started: {runs['count']} live, {_memory_mb(runs.get('rss_mb'))}, "
            f"{_memory_count(runs.get('procs'))} processes, {_memory_count(runs.get('mcp'))} MCP servers"
        )
        notes = []
        unsampled = runs.get("unsampled")
        if isinstance(unsampled, int) and unsampled:
            notes.append(f"{unsampled} not measured yet")
        shared = runs.get("shared")
        if isinstance(shared, int) and shared:
            notes.append(f"{shared} on a shared runtime: size only")
        if notes:
            line += f" ({'; '.join(notes)})"
        out.append(line)
    else:
        out.append("  Runs you started: none live")
    return out


def session_memory_lines() -> list[str]:
    """The caller's footprint block, or one line saying why it is missing.

    Never raises: ``resource_status`` must answer about the host even when this
    part cannot be read.
    """
    caller, _refusal = mcp_core.require_strict_session_key("")
    if not caller:
        return [f"\n{_MEMORY_HEADER}: unavailable (this caller's session could not be verified)"]
    try:
        payload = mcp_core._get("/api/spawn/session-memory", session_key=caller, timeout=8)
    except Exception:
        payload = None
    if not isinstance(payload, Mapping) or payload.get("error"):
        return [f"\n{_MEMORY_HEADER}: unavailable (the gateway did not answer)"]
    lines = render_session_memory(payload)
    lines[0] = "\n" + lines[0]
    return lines
