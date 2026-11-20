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

from kiro_crew import work_holders
from kiro_crew.work_holders import Holder

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
    #: What the counts above are made of, one per named thing, longest running
    #: first (``work_holders.summarize``). Empty when nothing is counted. Read
    #: through :meth:`named_holders` only.
    holders: tuple[Holder, ...] = ()

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

    def named_holders(self) -> tuple[list[dict[str, object]], int]:
        """The first holders as JSON entries, and how many more there are.

        HOST ONLY. A name identifies another session's or job's work, so it goes
        to a caller entitled to see all of them (the restart idle check, which
        takes the local secret on loopback) and never into :meth:`payload`, a
        broadcast or a log line, which reach every dashboard client and app.
        """
        return work_holders.payload(self.holders)

    def payload(self) -> dict[str, object]:
        """The counts a client shows, whole numbers only. Never a holder's name."""
        return {
            "turns": self.turns,
            "background": self.background,
            "queued_messages": self.queued_messages,
            "pending_approvals": self.pending_approvals,
            "busy": self.busy,
            "quiet_remaining_secs": int(self.quiet_remaining_secs + 0.999),
            "settle_remaining_secs": int(self.settle_remaining_secs + 0.999),
        }


def _slot_items(state: Any) -> list[tuple[Any, Any]]:
    slots = getattr(state, "_slots", None)
    return list(slots.items()) if isinstance(slots, dict) else []


def queued_message_holders(orchestrator: Any) -> list[Holder]:
    """Messages waiting behind a turn, by the session or slot they wait in.

    A source that cannot answer is one ``unreadable`` holder, so the answer
    fails toward "not quiescent".
    """
    held: list[Holder] = []
    sessions = getattr(orchestrator, "sessions", None)
    state = getattr(orchestrator, "dashboard_state", None)
    slot_names = work_holders.slot_names(state)
    # The channel queues live on the allocation service's session table.
    boundary = getattr(sessions, "_allocation_boundary", None)
    if callable(boundary):
        try:
            held.extend(
                Holder(
                    work_holders.QUEUED,
                    work_holders.session_name(key, slot_names),
                    count=len(s.queue),
                )
                for key, s in list(boundary()._sessions.items())
                if len(s.queue) > 0
            )
        except Exception:
            logger.warning("quiescence: channel queue count failed", exc_info=True)
            held = [Holder(work_holders.UNREADABLE, "channel queue")]
    for key, slot in _slot_items(state):
        try:
            waiting = len(getattr(slot, "_queue", None) or ())
            if waiting:
                held.append(Holder(work_holders.QUEUED, str(key), count=waiting))
        except Exception:
            logger.warning("quiescence: slot queue count failed", exc_info=True)
            held.append(Holder(work_holders.UNREADABLE, "slot queue"))
    return held


def queued_message_count(orchestrator: Any) -> int:
    """Messages waiting behind a turn: channel session queues and dashboard slot queues."""
    return work_holders.total(queued_message_holders(orchestrator))


def pending_approval_holders(orchestrator: Any) -> list[Holder]:
    """Approval cards waiting for a person, by slot; typed-reply prompts as a count."""
    held: list[Holder] = []
    state = getattr(orchestrator, "dashboard_state", None)
    if state is not None:
        # circular import: the dashboard package imports the gateway at load.
        from kiro_crew.dashboard.slot_retention import has_pending_approval

        for key, slot in _slot_items(state):
            try:
                if has_pending_approval(state, slot):
                    held.append(Holder(work_holders.APPROVAL, str(key)))
            except Exception:
                logger.warning("quiescence: slot approval probe failed", exc_info=True)
                held.append(Holder(work_holders.UNREADABLE, "slot approval"))
    from kiro_crew.messaging import approval

    try:
        typed = max(0, int(approval.pending_count()))
        if typed:
            held.append(Holder(work_holders.APPROVAL, count=typed))
    except Exception:
        logger.warning("quiescence: typed approval probe failed", exc_info=True)
        held.append(Holder(work_holders.UNREADABLE, "typed approval"))
    return held


def pending_approval_count(orchestrator: Any) -> int:
    """Approval cards waiting for a person: dashboard slots and typed-reply prompts."""
    return work_holders.total(pending_approval_holders(orchestrator))


def _work_holders(orchestrator: Any) -> list[Holder]:
    """The in-flight census by name, or ``[]`` where the orchestrator has none to give."""
    census = getattr(orchestrator, "_in_flight_work_census", None)
    if not callable(census):
        return []
    try:
        held = census()
    except Exception:
        logger.warning("quiescence: holder census failed", exc_info=True)
        return []
    return [h for h in held if isinstance(h, Holder)] if isinstance(held, list) else []


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
    waiting = queued_message_holders(orchestrator)
    cards = pending_approval_holders(orchestrator)
    queued = work_holders.total(waiting)
    approvals = work_holders.total(cards)
    # Named only when something is counted: the counts above decide, the
    # names describe them.
    busy = turns + background + queued + approvals > 0
    holders = (
        work_holders.summarize([*_work_holders(orchestrator), *waiting, *cards]) if busy else ()
    )
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
        holders=holders,
    )


def is_quiescent(
    orchestrator: Any,
    *,
    quiet_window_secs: float = QUIET_WINDOW_SECS,
    now: float | None = None,
) -> bool:
    """Whether an update may restart *orchestrator* now. The one shared predicate."""
    return assess(orchestrator, quiet_window_secs=quiet_window_secs, now=now).quiescent
