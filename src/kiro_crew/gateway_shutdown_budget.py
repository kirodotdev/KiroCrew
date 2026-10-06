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
#: drain still grants a slow-but-progressing write once cancelled. It is the
#: only wait the gateway's ``_shutdown`` grants past ``GRACEFUL_SHUTDOWN_SECS``:
#: the teardown that follows an interrupted drain runs in an owned task waited
#: only until that same graceful deadline. At expiry the task is cancelled and
#: not awaited, so a step that swallows cancellation cannot extend the waiter.
#: The window therefore comes out of ``SIGNAL_MARGIN_SECS``, which must still
#: hold the exit path after it.
PERSISTENCE_DRAIN_GRACE_SECS = 2

#: SIGTERM-to-SIGKILL deadline shared by systemd and launchd.
TOTAL_SHUTDOWN_BUDGET_SECS = (
    GRACEFUL_SHUTDOWN_SECS + SIGNAL_MARGIN_SECS
)

#: How long an in-flight update installer has, after SIGTERM, to run its own
#: rollback before it is SIGKILLed. ``cli.sh`` moves the venv aside before it
#: rebuilds it and restores it from a TERM trap; SIGKILL skips that trap and
#: strands the install. Shutdown starts this stop first and waits for it
#: before its teardown, so it takes a bounded share of
#: ``GRACEFUL_SHUTDOWN_SECS`` and leaves the rest to that teardown.
UPDATE_INSTALLER_TERM_GRACE_SECS = GRACEFUL_SHUTDOWN_SECS * 0.4

#: Reap bound after the SIGKILL escalation. SIGKILL is not catchable, so this
#: only covers the kernel tearing the group down and the pipes draining.
UPDATE_INSTALLER_KILL_REAP_SECS = GRACEFUL_SHUTDOWN_SECS * 0.1

#: The most shutdown waits for the update coordinator to stop its installer.
UPDATE_INSTALLER_STOP_SECS = UPDATE_INSTALLER_TERM_GRACE_SECS + UPDATE_INSTALLER_KILL_REAP_SECS
