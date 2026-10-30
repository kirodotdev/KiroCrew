"""The remote-workspace stop on shutdown fits inside the graceful shutdown budget."""

from __future__ import annotations

from kiro_crew.coder import retention
from kiro_crew.gateway_shutdown_budget import GRACEFUL_SHUTDOWN_SECS, TOTAL_SHUTDOWN_BUDGET_SECS


def test_the_shutdown_stop_ends_inside_the_graceful_budget():
    # It runs beside the graceful shutdown; past it, the hard-exit cleanup and the
    # audit flush would be cut off by the service manager's SIGKILL.
    assert retention.SHUTDOWN_STOP_SECS < GRACEFUL_SHUTDOWN_SECS < TOTAL_SHUTDOWN_BUDGET_SECS
