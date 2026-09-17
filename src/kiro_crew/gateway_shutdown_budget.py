"""Shared shutdown budgets for the Gateway and its service managers."""

from __future__ import annotations

#: Maximum time allowed for the Gateway's cooperative shutdown.
GRACEFUL_SHUTDOWN_SECS = 10

#: Headroom for signal delivery, event-loop wakeup, cleanup, and exit.
SIGNAL_MARGIN_SECS = 10

#: How long a service keeps waiting on its own in-flight durable writes AFTER
#: the cooperative shutdown has been cancelled. The gateway's bounded wait
#: delivers exactly one cancellation, at ``GRACEFUL_SHUTDOWN_SECS``; a drain
#: that absorbed it and kept waiting turned that bound into an unbounded one
#: whenever an executor write was wedged (a network mount, an AV-held rename),
#: so only the supervisor's SIGKILL ended the process. This window is what the
#: drain still grants a slow-but-progressing write once cancelled; it must fit
#: inside ``SIGNAL_MARGIN_SECS`` with room for the rest of teardown.
PERSISTENCE_DRAIN_GRACE_SECS = 2

#: SIGTERM-to-SIGKILL deadline shared by systemd and launchd.
TOTAL_SHUTDOWN_BUDGET_SECS = (
    GRACEFUL_SHUTDOWN_SECS + SIGNAL_MARGIN_SECS
)
