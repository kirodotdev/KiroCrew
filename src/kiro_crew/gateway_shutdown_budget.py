"""Shared shutdown budgets for the Gateway and its service managers."""

from __future__ import annotations

#: Maximum time allowed for the Gateway's cooperative shutdown.
GRACEFUL_SHUTDOWN_SECS = 10

#: Headroom for signal delivery, event-loop wakeup, cleanup, and exit.
SIGNAL_MARGIN_SECS = 10

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
