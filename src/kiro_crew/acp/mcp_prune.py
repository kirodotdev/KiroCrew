"""Leave an MCP server whose command cannot start out of a session's array.

One routine for both ACP drivers. A host in ``ACP_BACKENDS_PRUNE_UNSTARTABLE_MCP``
either loses such a server without a word (goose) or fails the whole ``session/new``
on it (deepseek), so the array is pruned before it is sent and each pruned server is
named in the log and recorded on the session's MCP report as failed. ``AcpClient``
records it when it begins the session's report; ``AcpRuntime`` when it opens it.
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

# The client's logger name: these lines have always been filed under it.
logger = logging.getLogger("kiro_crew.acp.client")

__all__ = ["PruneResult", "prune_session_servers", "record_pruned"]


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
