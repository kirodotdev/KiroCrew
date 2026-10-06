"""Shared shutdown budgets for the Gateway and its service managers."""

from __future__ import annotations

#: Maximum time allowed for the Gateway's cooperative shutdown.
GRACEFUL_SHUTDOWN_SECS = 10

#: How long shutdown waits for in-flight notification-bridge fanout before the
#: transports close. A note is already on the dashboard before the bridge sees it,
#: so a leg cut short costs one chat DM the owner reads there instead.
BRIDGE_DRAIN_RESERVE_SECS = 2.0

#: Max time the orphan-recovery bell waits on its own durable persist future
#: before falling through to the Slack DM fallback. The bell is credited as
#: delivered only once that write lands, but awaiting it unbounded lets a stalled
#: write (disk full/slow -- exactly when a ``system.resources`` orphan bell fires)
#: block the independent Slack attempt below it, so the orphan is skipped after
#: restart and its notification is lost. Bounded here, a timed-out persist is a
#: not-yet-delivered bell: the Slack fallback still runs and the held orphan is
#: kept (not tombstoned) when neither path lands.
_ORPHAN_BELL_PERSIST_TIMEOUT = 2.0

#: Headroom for signal delivery, event-loop wakeup, cleanup, and exit.
SIGNAL_MARGIN_SECS = 10

#: SIGTERM-to-SIGKILL deadline shared by systemd and launchd.
TOTAL_SHUTDOWN_BUDGET_SECS = GRACEFUL_SHUTDOWN_SECS + SIGNAL_MARGIN_SECS

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
