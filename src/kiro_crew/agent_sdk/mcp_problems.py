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
