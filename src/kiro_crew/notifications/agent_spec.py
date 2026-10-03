"""Operator notice while agent sessions are refused on an unprojected main spec.

When ``kirocrew.json`` cannot be read, or reads but its rebuild did not finish,
:mod:`kiro_crew.agent` records the main spec as unprojected and every session
start is refused (``AgentSpecUnprojected``), cron, channel and subagent
sessions included. The person whose start is refused sees the refusal, but an
unattended start has no one watching it, so without this producer the only
other trace is a log WARNING.

This producer reads the recorded cause on the event-loop heartbeat and turns
it into notes on the bus. An **episode** runs from the first reading with a
cause to the first reading without one:

* entering an episode pushes one critical note naming the cause and its
  remedy, re-pushed every :data:`REALERT_SECS` while it lasts (same
  ``group_key``, so the feed stacks it);
* a cause that changes inside the episode (the file now reads, but the rebuild
  did not finish) pushes the note again with the new cause;
* leaving the episode pushes one recovery note.

The read is one module global under its lock, with no file I/O, so it runs on
the loop. Muting ``system.agent`` in Settings -> Notifications silences it.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Callable

from kiro_crew.agent import main_spec_refusal_reason
from kiro_crew.notifications.bus import NotificationPayload

if TYPE_CHECKING:
    from kiro_crew.notifications.bus import NotificationBus

logger = logging.getLogger(__name__)

CHANNEL = "system.agent"
GROUP_KEY = "agent-spec-unprojected"

#: While sessions stay refused, push the note again on this cadence, so one
#: missed note is not the only notice of an outage covering every session.
REALERT_SECS = 3600.0


class AgentSpecRefusalNotifier:
    """Turns the recorded unprojected-spec cause into episode notes.

    Owned by ``DashboardState`` alongside the bus it publishes to and driven by
    :meth:`sample` from the event-loop heartbeat. ``reason_fn`` and ``clock``
    are injectable so tests drive the episode logic directly.
    """

    def __init__(
        self,
        bus: "NotificationBus",
        *,
        reason_fn: Callable[[], str | None] = main_spec_refusal_reason,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bus = bus
        self._reason = reason_fn
        self._clock = clock
        self._alerted_reason: str | None = None
        self._last_push = 0.0

    def sample(self) -> None:
        """Read the recorded cause and push what changed. Never raises."""
        try:
            self.observe(self._reason(), self._clock())
        except Exception:
            logger.debug("agent-spec refusal sample failed", exc_info=True)

    def observe(self, reason: str | None, now: float) -> None:
        """Advance the episode with one reading of the recorded cause."""
        if reason is None:
            if self._alerted_reason is not None:
                self._alerted_reason = None
                self._push_recovery()
            return
        if reason != self._alerted_reason:
            realert = self._alerted_reason is not None
            self._alerted_reason = reason
            self._last_push = now
            self._push_refused(reason, realert=realert)
        elif now - self._last_push >= REALERT_SECS:
            self._last_push = now
            self._push_refused(reason, realert=True)

    def _push_refused(self, reason: str, *, realert: bool) -> None:
        title = (
            "Agent sessions are still refused"
            if realert
            else "Agent sessions are refused: kirocrew.json is not re-projected"
        )
        self._bus.push(
            NotificationPayload(
                source="system",
                channel=CHANNEL,
                priority="critical",
                title=title,
                body=(
                    "No agent session can start, so chats, cron jobs, channel replies "
                    "and subagents fail until this clears. The main agent spec's "
                    "auto-approvals could not be re-projected against the current "
                    f"governance ceiling: {reason}. The next session start re-reads "
                    "the file and retries."
                ),
                group_key=GROUP_KEY,
                meta={"cause": reason},
            )
        )

    def _push_recovery(self) -> None:
        self._bus.push(
            NotificationPayload(
                source="system",
                channel=CHANNEL,
                title="Agent sessions can start again",
                body="kirocrew.json was re-projected, so agent sessions are admitted again.",
                group_key=GROUP_KEY,
            )
        )
