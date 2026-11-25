"""What a sub-agent run records when its recovery respawn could not start.

``CancellationCoordinator._schedule_cancel_recovery_impl`` respawns a run for one
of several reasons; when the respawn itself fails, the run ends with the error
and tombstone cause named here, so each reason says what it was trying to do.
"""

from __future__ import annotations

from typing import Any


def recovery_failed(reason: str, info: Any) -> tuple[str, str]:
    """``(info.error, tombstone cause)`` for a recovery respawn of *reason* that failed."""
    if reason == "context_overflow":
        return (
            "agent context exceeded the model window and the dedicated-session "
            "recovery could not start",
            "error",
        )
    if reason == "session_not_found":
        return (
            "the backend lost this run's session and the fresh-runtime re-run could not start",
            "error",
        )
    if reason == "backend_failover":
        # The refused attempt's own error leads: it is why the run failed. The
        # tail says the failover chain was tried and could not take the run.
        detail = getattr(info, "_failover_detail", "") or "the backend refused the first request"
        return (
            f"{detail} (agent.acp_failover could not start the run on another backend)",
            "error",
        )
    return "cancelled (recovery failed)", "cancelled"
