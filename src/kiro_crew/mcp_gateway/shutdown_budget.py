"""Shutdown timing budgets shared by the gateway daemon and its supervisor.

These constants exist in a dependency-free leaf module for one reason: the
supervisor's SIGTERM→SIGKILL grace period MUST cover the daemon's own graceful
shutdown budget. As independent literals in two files they can drift into an
*inverted* pair (grace < drain) — the supervisor then SIGKILLs the daemon while
it is still inside its own drain window, so ``pool.shutdown_all()`` never runs
and every restart with attached stubs ends in a hard kill.

Deriving the grace period from the daemon's budget here makes that inversion
unrepresentable: raising the drain window automatically raises the grace.

``gatewayd`` cannot own these values because ``manager`` must not import it
(``gatewayd`` already imports ``manager``, so the dependency only runs one way).
"""

from __future__ import annotations

#: How long ``gatewayd`` lets IN-FLIGHT tool calls finish after ``stop_event``
#: fires, before cancelling whatever is left. Bounded because a wedged backend
#: must never block a restart indefinitely.
DRAIN_SECS = 10.0

#: Deadline handed to ``BackendPool.shutdown_all()`` — the per-backend
#: SIGTERM→SIGKILL budget, applied to every pooled backend concurrently.
POOL_SHUTDOWN_SECS = 5.0

#: Headroom on top of the two phases above: signal delivery, event-loop
#: wakeups, the hot-key flush, socket unlink and flock release.
SIGNAL_MARGIN_SECS = 5.0

#: Total wall-clock a clean ``gatewayd`` shutdown may take. The supervisor
#: (:mod:`kiro_crew.mcp_gateway.manager`) waits at least this long after
#: SIGTERM before escalating to SIGKILL, so a daemon that is shutting down
#: correctly is never killed mid-drain.
TOTAL_SHUTDOWN_BUDGET_SECS = DRAIN_SECS + POOL_SHUTDOWN_SECS + SIGNAL_MARGIN_SECS
