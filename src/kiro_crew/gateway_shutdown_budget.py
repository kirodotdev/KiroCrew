"""Shared shutdown budgets for the Gateway and its service managers."""

from __future__ import annotations

#: Maximum time allowed for the Gateway's cooperative shutdown.
GRACEFUL_SHUTDOWN_SECS = 10

#: The slice of GRACEFUL_SHUTDOWN_SECS held back for the notification-bridge
#: drain. One symbol owns BOTH the ceiling on the drain's own timeout and the
#: amount reserved out of the budget for it, because the two are only correct
#: while they are the same number: reserving less than the drain may spend lets
#: the drain overrun the budget, and reserving more starves the producers that
#: run before it. It is a ceiling rather than the timeout itself because the
#: steps ahead of the drain can consume the whole budget between them, and a
#: reservation cannot be honoured out of a budget that is already spent -- so the
#: drain takes the smaller of this number and what is still spendable ahead of
#: CLOSES_RESERVE_SECS.
BRIDGE_DRAIN_RESERVE_SECS = 2.0

#: The slice of GRACEFUL_SHUTDOWN_SECS held back for the closes that END the
#: shutdown: the cleanup gather, whose session-map close is where slot state
#: becomes durable, and the store and memory closes after it. Those steps are
#: the only ones whose work is lost rather than deferred when the caller's
#: ``wait_for`` expires, and they run LAST, so without a reservation every
#: second is legitimately spendable by the steps ahead of them -- each one
#: bounded, and between them able to consume the lot. Held back from those
#: steps rather than added to the total because the total is what systemd and
#: launchd already agreed to.
CLOSES_RESERVE_SECS = 2.0

#: Headroom for signal delivery, event-loop wakeup, cleanup, and exit.
SIGNAL_MARGIN_SECS = 10

#: SIGTERM-to-SIGKILL deadline shared by systemd and launchd.
TOTAL_SHUTDOWN_BUDGET_SECS = (
    GRACEFUL_SHUTDOWN_SECS + SIGNAL_MARGIN_SECS
)
