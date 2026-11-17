"""One answer to "may an update restart this gateway now?".

``is_quiescent(orchestrator)`` is the single predicate every update path asks
before it cuts the process: the scheduler's voluntary apply, the dashboard's
"Update now" and the restart behind it. The edition imports the same function,
so there is one definition of "nothing a restart would interrupt" and no second
census to drift from it.

Quiescent means ALL of:

* the in-flight census is empty: turns, slot tasks, workflows, callbacks,
  channel handlers, sub-agents, scripts and the TaskRunner
  (``GatewayOrchestrator._in_flight_work_counts``);
* no channel message waits in a session queue and no dashboard slot has queued
  sends;
* no approval card waits for a person;
* nobody typed into a chat (dashboard or channel) for ``QUIET_WINDOW_SECS``: an
  open conversation between turns reads as idle to the census, and a restart
  would end it under the person who is reading the answer.

Hysteresis: a census that was busy when last read keeps the answer "not yet"
for ``SETTLE_SECS`` after it clears, so a turn ending for a moment between two
tool calls of a longer job cannot open the gate.

CLI chat (``kirocrew chat``) runs its own provider process and registers no
session with this gateway, so it is not reachable from here. Callers that must
cover terminal sessions (the edition's update agent) check them themselves.

The ledger lives on the ``SessionManager`` (``_user_input_ledger``), not in this
module, so two gateways in one process, or two tests, never share a clock.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: How long after the last typed message in any chat the gateway still counts
#: as in use. Named so a deployment or a test reads one value.
QUIET_WINDOW_SECS = 15 * 60.0

#: How long a census that was last seen busy must stay clear before the gateway
#: counts as quiescent (the hysteresis).
SETTLE_SECS = 60.0


class ActivityLedger:
    """When a person last typed, and when work was last seen running."""

    __slots__ = ("last_busy_at", "last_input_at")

    def __init__(self) -> None:
        self.last_input_at: float | None = None
        self.last_busy_at: float | None = None


def _ledger(sessions: Any, *, create: bool) -> ActivityLedger | None:
    """The ledger carried by *sessions*, created on first use when *create*."""
    if sessions is None:
        return None
    ledger = getattr(sessions, "_user_input_ledger", None)
    if isinstance(ledger, ActivityLedger):
        return ledger
    if not create:
        return None
    ledger = ActivityLedger()
    try:
        sessions._user_input_ledger = ledger
    except Exception:
        logger.debug("quiescence: session manager cannot carry a ledger", exc_info=True)
        return None
    return ledger


def note_user_input(sessions: Any, *, now: float | None = None) -> None:
    """Record that a person just typed into a chat.

    Called at the two places input enters: the dashboard chat send and the
    channel inbound admission. Never raises: a ledger that cannot be written
    must not refuse the message it was recording.
    """
    try:
        ledger = _ledger(sessions, create=True)
        if ledger is not None:
            ledger.last_input_at = time.monotonic() if now is None else now
    except Exception:
        logger.debug("quiescence: could not record user input", exc_info=True)


@dataclass(frozen=True)
class Quiescence:
    """What an update would interrupt, and what it still has to wait out."""

    turns: int = 0
    background: int = 0
    queued_messages: int = 0
    pending_approvals: int = 0
    #: Seconds since a person last typed, or ``None`` if nobody has since boot.
    since_input_secs: float | None = None
    quiet_window_secs: float = QUIET_WINDOW_SECS
    #: Seconds of hysteresis left after the census last read busy.
    settle_remaining_secs: float = 0.0

    @property
    def busy(self) -> int:
        """Work items an immediate restart would cut."""
        return self.turns + self.background + self.queued_messages + self.pending_approvals

    @property
    def quiet_remaining_secs(self) -> float:
        """Seconds of typing-free time still owed, 0 when the window has passed."""
        if self.since_input_secs is None:
            return 0.0
        return max(0.0, self.quiet_window_secs - self.since_input_secs)

    @property
    def quiescent(self) -> bool:
        return self.busy == 0 and self.quiet_remaining_secs <= 0 and self.settle_remaining_secs <= 0

    def payload(self) -> dict[str, object]:
        """The counts a client shows, whole numbers only."""
        return {
            "turns": self.turns,
            "background": self.background,
            "queued_messages": self.queued_messages,
            "pending_approvals": self.pending_approvals,
            "busy": self.busy,
            "quiet_remaining_secs": int(self.quiet_remaining_secs + 0.999),
            "settle_remaining_secs": int(self.settle_remaining_secs + 0.999),
        }


def _slot_values(state: Any) -> list[Any]:
    slots = getattr(state, "_slots", None)
    return list(slots.values()) if isinstance(slots, dict) else []


def queued_message_count(orchestrator: Any) -> int:
    """Messages waiting behind a turn: channel session queues and dashboard slot queues.

    A source that cannot answer counts as one waiting message, so the answer
    fails toward "not quiescent".
    """
    total = 0
    sessions = getattr(orchestrator, "sessions", None)
    # The channel queues live on the allocation service's session table.
    boundary = getattr(sessions, "_allocation_boundary", None)
    if callable(boundary):
        try:
            total += sum(len(s.queue) for s in list(boundary()._sessions.values()))
        except Exception:
            logger.warning("quiescence: channel queue count failed", exc_info=True)
            total += 1
    for slot in _slot_values(getattr(orchestrator, "dashboard_state", None)):
        try:
            total += len(getattr(slot, "_queue", None) or ())
        except Exception:
            logger.warning("quiescence: slot queue count failed", exc_info=True)
            total += 1
    return total


def pending_approval_count(orchestrator: Any) -> int:
    """Approval cards waiting for a person: dashboard slots and typed-reply prompts."""
    total = 0
    state = getattr(orchestrator, "dashboard_state", None)
    if state is not None:
        # circular import: the dashboard package imports the gateway at load.
        from kiro_crew.dashboard.slot_retention import has_pending_approval

        for slot in _slot_values(state):
            try:
                if has_pending_approval(state, slot):
                    total += 1
            except Exception:
                logger.warning("quiescence: slot approval probe failed", exc_info=True)
                total += 1
    from kiro_crew.messaging import approval

    try:
        total += approval.pending_count()
    except Exception:
        logger.warning("quiescence: typed approval probe failed", exc_info=True)
        total += 1
    return total


def assess(
    orchestrator: Any,
    *,
    quiet_window_secs: float = QUIET_WINDOW_SECS,
    now: float | None = None,
) -> Quiescence:
    """Read every source once and apply the hysteresis.

    ``quiet_window_secs=0`` drops the typing window for a caller that IS the
    person asking (an explicit restart), and keeps everything else.
    """
    moment = time.monotonic() if now is None else now
    turns, background = orchestrator._in_flight_work_counts()
    queued = queued_message_count(orchestrator)
    approvals = pending_approval_count(orchestrator)
    ledger = _ledger(getattr(orchestrator, "sessions", None), create=True)
    since_input: float | None = None
    settle_left = 0.0
    if ledger is not None:
        if ledger.last_input_at is not None:
            since_input = max(0.0, moment - ledger.last_input_at)
        if turns + background + queued + approvals > 0:
            ledger.last_busy_at = moment
        elif ledger.last_busy_at is not None:
            settle_left = max(0.0, SETTLE_SECS - (moment - ledger.last_busy_at))
    return Quiescence(
        turns=turns,
        background=background,
        queued_messages=queued,
        pending_approvals=approvals,
        since_input_secs=since_input,
        quiet_window_secs=max(0.0, quiet_window_secs),
        settle_remaining_secs=settle_left,
    )


def is_quiescent(
    orchestrator: Any,
    *,
    quiet_window_secs: float = QUIET_WINDOW_SECS,
    now: float | None = None,
) -> bool:
    """Whether an update may restart *orchestrator* now. The one shared predicate."""
    return assess(orchestrator, quiet_window_secs=quiet_window_secs, now=now).quiescent
