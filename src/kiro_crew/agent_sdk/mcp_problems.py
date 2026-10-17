"""The one line an operator reads when a session's MCP servers did not all start.

A headless turn (a webhook run, a task-runner step) runs in a session nobody is
watching, so a server that failed to start there is otherwise visible only as
missing tools. Each such surface names the unusable servers to its operator from
the session's own MCP report; this is the one reading they share.

It reaches the report through the provider contract
(``LLMProvider.mcp_session_report()`` -> ``SessionMcpReport.problem_summary``),
so it needs no ACP import.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: How long an unattended run (webhook, cron) holds its first prompt for the
#: agent spec's MCP servers to report. The session's own init drain gives up a
#: second after the first server reports, which a slow ``node`` server misses;
#: nobody can reconnect it by hand in these runs, so they wait longer. Bounded,
#: so a server that never answers costs a run this much and no more.
UNATTENDED_MCP_WAIT_SECS = 30.0


def session_mcp_problems(provider: Any) -> str:
    """One line naming the MCP servers this session could not use, or ``""``.

    The line is for the operator (a run list, a delivered result, a dashboard
    notification), never for a prompt, so the sanitized failure reasons are kept.
    Silent for a clean report, for a provider that keeps none, and for a report
    that cannot be read: a notice is never worth failing a run over.
    """
    if provider is None:
        return ""
    try:
        report = provider.mcp_session_report()
        return report.problem_summary() if report is not None else ""
    except Exception:
        logger.debug("MCP session report unreadable", exc_info=True)
        return ""


async def wait_for_declared_mcp(provider: Any, timeout: float) -> None:
    """Hold an unattended run's first prompt until the spec's servers report.

    Best-effort and bounded by *timeout*: a provider without the capability, or
    one whose wait fails, lets the run go ahead, and :func:`declared_mcp_missing`
    then says what it lacks.
    """
    if provider is None or timeout <= 0:
        return
    try:
        missing = await provider.wait_for_declared_mcp(timeout)
    except Exception:
        logger.debug("declared MCP wait failed", exc_info=True)
        return
    if missing:
        # The run prompts anyway; this line is when and how long it waited.
        logger.warning(
            "Unattended run prompting after a %gs wait without declared MCP server(s): %s",
            timeout,
            ", ".join(str(name) for name in missing),
        )


def _declared(provider: Any, reader: str) -> tuple[str, ...]:
    if provider is None:
        return ()
    try:
        report = provider.mcp_session_report()
        names = getattr(report, reader)() if report is not None else ()
        return tuple(name for name in names if isinstance(name, str) and name)
    except Exception:
        logger.debug("MCP session report unreadable", exc_info=True)
        return ()


def declared_mcp_missing(provider: Any) -> tuple[str, ...]:
    """The agent spec's servers this session cannot use, or ``()``.

    Read from the session's own report, so it covers a server that failed, one
    awaiting authorization and one that never reported: the run's outcome
    turns on it. Silent for a provider that keeps no report and for one that
    cannot be read.
    """
    return _declared(provider, "declared_not_ready")


def missing_mcp_notice(provider: Any) -> str:
    """The operator line naming declared servers that never reported, or ``""``.

    A failed or awaiting-authorization server is not repeated here:
    :func:`session_mcp_problems` already names it, with its reason.
    """
    names = _declared(provider, "declared_never_reported")
    if not names:
        return ""
    return "Declared MCP servers that never reported: " + ", ".join(names)


def declared_mcp_not_ready_note(provider: Any) -> str:
    """One line naming every declared server the run went without, or ``""``.

    For a surface that prints no :func:`session_mcp_problems` line beside it (a
    cron result), so a failed declared server is named here too.
    """
    names = declared_mcp_missing(provider)
    if not names:
        return ""
    return "MCP servers declared by the agent spec were not ready in this run: " + ", ".join(names)
