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
#: drain takes the smaller of this number and what is still spendable ahead of it.
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

#: Seconds held back from the shutdown budget when bounding ``SessionMap.close_all()``'s
#: cooperative turn drain, so its durability point (``SessionMap.aclose()``, which
#: persists the resumable session map) still runs inside the outer
#: ``GRACEFUL_SHUTDOWN_SECS`` deadline. ``close_all()`` is launched only after
#: ``cancel_all()`` returns, so a slow ``cancel_all()`` can leave little budget; without
#: this reserve the drain's own default could consume what remains and the outer
#: ``wait_for`` would cancel the close before ``aclose()`` persists -- losing resumable
#: state with no replay on the next start. State over turns: the drain gives up early.
_SESSION_CLOSE_PERSIST_RESERVE = 1.5

#: Ceiling on the ``close_all()`` turn drain, mirroring ``session``'s own
#: ``_DRAIN_ACTIVE_TURNS_TIMEOUT_SECS`` default (5.0) plus the ``+1.0`` grace
#: ``drain_active_turns`` adds to its ``wait_for``. The drain is capped at the
#: SMALLER of this ceiling and the reserve net of that grace, so even when
#: ``cancel_all()`` returns with the budget nearly full the drain cannot run
#: longer than its own default and still leaves ``aclose()`` room to persist
#: inside ``GRACEFUL_SHUTDOWN_SECS``. Without the ceiling a fast ``cancel_all()``
#: lets the drain spend budget_left - reserve (~8s), overrunning the ~6s default
#: and the outer deadline, so the resumable session map is lost on a wedged turn.
_SESSION_CLOSE_DRAIN_CEILING_SECS = 5.0
_SESSION_CLOSE_DRAIN_GRACE_SECS = 1.0

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
