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

#: Headroom for signal delivery, event-loop wakeup, cleanup, and exit.
SIGNAL_MARGIN_SECS = 10

#: SIGTERM-to-SIGKILL deadline shared by systemd and launchd.
TOTAL_SHUTDOWN_BUDGET_SECS = GRACEFUL_SHUTDOWN_SECS + SIGNAL_MARGIN_SECS
