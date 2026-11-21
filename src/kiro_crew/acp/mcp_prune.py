"""Leave an MCP server whose command cannot start out of a session's array.

One routine for both ACP drivers. A host in ``ACP_BACKENDS_PRUNE_UNSTARTABLE_MCP``
either loses such a server without a word (goose) or fails the whole ``session/new``
on it (deepseek), so the array is pruned before it is sent and each pruned server is
named in the log and recorded on the session's MCP report as failed. ``AcpClient``
records it when it begins the session's report; ``AcpRuntime`` when it opens it.

A server the spec writer already dropped for a command not found never reaches any
array, on any host; :func:`spec_dropped_servers` reads that drop record so
``AcpClient`` names it on the report the same way.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from kiro_crew.acp.mcp_session_report import BUCKET_CAP as MCP_REPORT_BUCKET_CAP
from kiro_crew.acp.mcp_session_report import ERROR_CAP as MCP_REPORT_ERROR_CAP
from kiro_crew.acp.mcp_session_report import NAME_CAP as MCP_REPORT_NAME_CAP
from kiro_crew.acp.mcp_session_report import McpSessionReport, sanitize_sink_text
from kiro_crew.acp.mcp_startable import PrunedServer, prune_unstartable
from kiro_crew.agent_files import AGENT_FILENAME, WORKER_AGENT_FILENAME
from kiro_crew.mcp_dropped_servers import load_dropped

# The client's logger name: these lines have always been filed under it.
logger = logging.getLogger("kiro_crew.acp.client")

__all__ = ["PruneResult", "prune_session_servers", "record_pruned", "spec_dropped_servers"]


@dataclass(frozen=True)
class PruneResult:
    """The array to send, and what was left out of it."""

    kept: list[Any]
    pruned: tuple[PrunedServer, ...]
    omitted: int


async def prune_session_servers(backend: str, servers: list[Any]) -> PruneResult:
    """*servers* without the stdio elements whose command certainly cannot start.

    The stats run off the loop. What is kept of each pruned server is bounded by the
    report's own caps: the count by the failed bucket's size, and each name and
    reason by its string cap. The reason quotes the configured command, which can
    hold a whole CLI line with a key in it, so it is cleaned before it is kept or
    logged.
    """
    kept, pruned = await asyncio.to_thread(prune_unstartable, servers)
    retained = tuple(
        PrunedServer(
            sanitize_sink_text(str(server.name or ""), MCP_REPORT_NAME_CAP),
            sanitize_sink_text(str(server.reason or ""), MCP_REPORT_ERROR_CAP),
        )
        for server in pruned[:MCP_REPORT_BUCKET_CAP]
    )
    omitted = len(pruned) - len(retained)
    for server in retained:
        logger.warning(
            "%s session MCP: leaving server %r out of this session -- %s. The "
            "session starts without it; fix its command to get its tools back",
            backend,
            server.name,
            server.reason,
        )
    if omitted:
        logger.warning(
            "%s session MCP: leaving %d more unstartable servers out of this "
            "session, not listed",
            backend,
            omitted,
        )
    return PruneResult(kept=kept, pruned=retained, omitted=omitted)


#: The agents whose spec carries the user's own MCP servers as the default agent's
#: rebuild wrote them: the default agent, and the worker, which mirrors its
#: ``mcpServers`` (``agent_materialization.worker_agent``). The rebuild's drop record
#: (``mcp_dropped_servers``) describes exactly that server set, so only a session on
#: one of these is missing what it lists.
_DEFAULT_SERVER_AGENTS = frozenset(
    name[: -len(".json")] for name in (AGENT_FILENAME, WORKER_AGENT_FILENAME)
)


def spec_dropped_servers(agent: str | None, spec: Any) -> tuple[PrunedServer, ...]:
    """The servers the spec writer left out of *agent*'s spec because their command was not found.

    The spec writer (``mcp_sources.resolve_mcp_servers``) drops such a server before
    any session sees it, so the array prune never meets it and the session would
    otherwise say nothing. Read from the rebuild's drop record, for an agent that
    carries the default agent's servers, minus any name *spec* (the session's own
    snapshot) still defines. Each name and reason is bounded like an array prune's
    entry; the caller caps the count with the pruned ones (:func:`record_pruned`).

    Blocking (one small file read) and non-raising: callers run it in the hop that
    already snapshots the spec, and an unreadable record reads as nothing dropped.
    """
    if agent not in _DEFAULT_SERVER_AGENTS:
        return ()
    try:
        dropped = load_dropped()
    except Exception:
        logger.debug("dropped-server record unreadable", exc_info=True)
        return ()
    servers = spec.get("mcpServers") if isinstance(spec, dict) else None
    defined = servers if isinstance(servers, dict) else {}
    return tuple(
        PrunedServer(
            sanitize_sink_text(name, MCP_REPORT_NAME_CAP),
            sanitize_sink_text(
                "command not found (install it, or fix the command on the MCP Servers "
                f"page): {rec['command']}",
                MCP_REPORT_ERROR_CAP,
            ),
        )
        for name, rec in sorted(dropped.items())
        if name not in defined
    )


def record_pruned(
    report: McpSessionReport, pruned: tuple[PrunedServer, ...], omitted: int
) -> tuple[str, ...]:
    """Record each pruned server on *report* as failed; the names it admitted.

    Called after the report's session began, since beginning it clears the failed
    bucket. A name the bucket refused gets no row anywhere.
    """
    admitted: list[str] = []
    for server in pruned:
        if report.record_pruned(server.name, server.reason) and server.name not in admitted:
            admitted.append(server.name)
    report.record_pruned_omitted(omitted)
    return tuple(admitted)
