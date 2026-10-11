"""Which sub-agent results a blocking ``spawn_sub_agents`` call returns inline.

``spawn_sub_agents`` spawns its members, polls them, and hands their results back
as the tool call's own return value. Each member's ``[Subagent completion event]``
must therefore never reach the parent: the parent's turn is still blocked inside
that tool call, and an injected prompt either cancels the turn (the prompt-busy
retry interrupts it) or plays later as a redundant turn about a result the model
already read.

This registry is the ONE owner of that delivery, per PARENT session key, so every
parent kind (dashboard tab, cron run, channel thread, task runner) is held to the
same rule. Each member is a record whose state moves one way::

    collecting --hold--> held --close, not returned / expiry--> delivering
         |                 |                                       |
         \\--close, returned--+--> claimed --response dropped / expiry--/
                                    |
                                    \\--response written--> settled (completion seen)
                                                          \\-> returned --completion--> consumed

* **collecting**: ``reserve`` records the member when ``/api/spawn`` mints its run
  id, before the run can start, so a member that finishes at once, or one that
  waits behind the concurrency cap, is covered. From here on the run is kept
  out of completed-run eviction (``pins``), so a member that finishes and is
  followed by many newer completions before its terminal report reaches
  ``hold`` is still there to be read.
* **held**: the member completed while collecting. Only its id is kept here.
  The completion stays on the live run (``SubagentManager.get``), undelivered
  (``_delivery_queued``, so no ``delivered`` tombstone starts its result.txt
  retention clock), still pinned. It is never injected, and a release reads it
  back from the run.
* **claimed**: the call's close (``finish``) named the member in its result, but
  that result has not yet reached the parent: the dispatcher writes the tool's
  response only after the call returns, and a cancel can still drop it. Nothing
  is settled. ``commit`` settles the claim once the response is written, and
  releases it for ordinary delivery when the response was dropped or never
  reported (expiry). So an unsettled result is always one that can still be
  delivered. A written commit settles a collecting or held member it names
  too: its claim was lost or has not landed yet, and a late claim then
  changes nothing, so a result the response carried is never redelivered.
* **returned**: the response carried the member before its completion was seen
  (the poll can read ``done`` before the completion callback runs). Its
  completion is consumed, never injected.
* **delivering**: a released held or claimed completion. ``_deliver`` is the one
  function that takes it to a terminal outcome, whichever path released it.

Every record counts against the parent's one capacity until it leaves the
registry, whatever its state, so admission never lets a member in that a later
state could not keep.

The retired-parent fence lives here too. A parent's teardown calls ``retire``
synchronously, which marks every record of that parent, so a fenced result is
recognised even after completed-run retention evicted its run record. ``_deliver``
reads the mark at the point of delivery, after every wait.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: Added to the call's ``max_wait`` before a collection is treated as abandoned.
#: Covers ``_hold_for_parent_resume`` (bounded by the same deadline) and the
#: final result reads that follow the wait.
COLLECTION_GRACE_SECS = 300.0
#: Upper bound on one collection's lifetime, whatever ``max_wait`` claims:
#: the tool clamps its own wait to two hours.
MAX_COLLECTION_TTL_SECS = 7200.0 + COLLECTION_GRACE_SECS
#: Per-parent cap on the ids each store keeps. The ONE bound for this
#: population: the dashboard slot's collected set uses it too, and a member that
#: would exceed it is refused at spawn rather than silently left unheld.
MAX_IDS_PER_PARENT = 1000
#: Longest parent session key a record keeps. The caller names the parent, so
#: the record cap alone bounds how MANY records there are, not how big each is.
#: Sized from the real key formats: the longest is a dashboard thread,
#: ``thread:<slot>:<mid>`` (7 + a 249-character slot key + 1 + an 18-character
#: mid = 275), then ``dashboard:<slot>`` (259); cron, hook, subagent and channel
#: keys are a prefix and one or two provider ids. A longer key is REFUSED before
#: anything is retained, never truncated: a cut key could merge two parents.
MAX_PARENT_KEY_CHARS = 512
#: Longest agent id a record keeps. Run ids are 16 hex characters
#: (``SubagentManager._mint_agent_id``); the margin admits older id shapes.
MAX_AGENT_ID_CHARS = 128
#: Longest call id a record keeps. The tool names its call with a 32-character
#: hex uuid; the bound is the agent id's, for the same margin.
MAX_CALL_ID_CHARS = MAX_AGENT_ID_CHARS
#: How long a returned id waits for its completion before it is forgotten. A
#: completion normally follows within seconds of the run being read as done.
COLLECTED_TTL_SECS = 3600.0
#: How long a claim waits for the dispatcher's word on the call's response
#: before it is released for ordinary delivery. The tool reports it right
#: after the response is written; a lost report costs a duplicate, never a
#: lost result.
CLAIM_TTL_SECS = 300.0
#: The longest a parent's expiry timer waits: the shortest deadline any record
#: is given (a claim's, and the reservation grace), so a pending timer always
#: fires by a newly given deadline.
_EXPIRY_SWEEP_SECS = min(CLAIM_TTL_SECS, COLLECTION_GRACE_SECS)
#: Added to the injection timeout to bound how long a released result waits
#: for the run's own terminal report to return before it is delivered anyway.
#: The report's longest legitimate wait is a held cron parent's idle reset,
#: which the injection timeout bounds.
TERMINAL_REPORT_GRACE_SECS = 60.0
#: How often a released result re-checks that its parent's turn ended.
_ORPHAN_IDLE_POLL_SECS = 0.5

COLLECTING = "collecting"
HELD = "held"
CLAIMED = "claimed"
RETURNED = "returned"
DELIVERING = "delivering"
#: Terminal outcomes of ``_deliver``. The record leaves the registry with one.
DELIVERED = "delivered"
UNDELIVERED = "undelivered"
#: The parent's route parked the announce on its slot queue, whose drain
#: settles it, so delivering it is the queue's job, not this registry's.
HANDED_OFF = "handed_off"


@dataclass(eq=False)
class _Record:
    aid: str
    parent: str
    state: str
    deadline: float
    #: When the member was reserved: no extension moves its deadline past this
    #: plus ``MAX_COLLECTION_TTL_SECS``.
    first_reserved: float = 0.0
    #: The member's completion arrived while it was collected or claimed. Only
    #: this flag is kept: result, error and status are read from the run.
    held: bool = False
    retired: bool = False
    #: The fence has dropped this result (once, whichever path saw it first).
    fenced: bool = False
    #: The delivery of an unreturned result, cancelled when its parent retires.
    task: asyncio.Task[str] | None = None
    #: The call that reserved it, so ending that call ends this reservation
    #: even when the call never learned the id (its ``/api/spawn`` reply was
    #: lost). ``""`` is the parent's one anonymous call (no call id given).
    call: str = ""


class InlineCollections:
    """Per-parent owner of the members a blocking call collects inline."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        # parent -> {agent id: record}
        self._records: dict[str, dict[str, _Record]] = {}
        # Strong references to the deliveries in flight.
        self._tasks: set[asyncio.Task[str]] = set()
        # The manager whose route, sessions, terminal reports and store a
        # delivery uses. Bound by ``SubagentManager`` at construction.
        self._manager: Any = None
        # The parents with an expiry timer pending, with the loop it is on: ONE
        # per parent (``_schedule_expiry``). A parent whose records are all gone
        # keeps its timer until it fires and finds nothing, so a
        # reserve-and-discard cycle reuses it, and timers never outnumber the
        # parents active in the last ``_EXPIRY_SWEEP_SECS``. A timer on a loop
        # that has since closed never fires, so it is replaced.
        self._timers: dict[str, asyncio.AbstractEventLoop] = {}

    def bind(self, manager: Any) -> None:
        self._manager = manager

    @staticmethod
    def refuse_oversized(field: str, length: int) -> None:
        """Name, once in the log, an identity refused past its bound."""
        logger.warning(
            "inline collection refused a %s of %d characters (bound %d); nothing recorded",
            field,
            length,
            _BOUND_OF[field],
        )

    # ── spawn side ──

    def reserve(self, parent: str, aid: str, max_wait: float, *, call: str = "") -> bool:
        """Record that live call *call* collects *aid* for *parent*; False when full."""
        if not parent or not aid:
            return False
        field = oversized_identity(parent, (aid,), call)
        if field:
            self.refuse_oversized(field, _length_of(field, parent, (aid,), call))
            return False
        ttl = min(max(float(max_wait), 0.0) + COLLECTION_GRACE_SECS, MAX_COLLECTION_TTL_SECS)
        now = self._clock()
        deadline = now + ttl
        records = self._records.get(parent, {})
        existing = records.get(aid)
        accepted = (
            existing.state == COLLECTING and not existing.retired
            if existing is not None
            else len(records) < MAX_IDS_PER_PARENT
        )
        if accepted:
            # Before expiring: an accepted reserve is the call still alive, so
            # nothing it collects may expire under it. A refused one extends
            # nothing.
            self._extend_call(parent, call, deadline)
        self._expire(parent)
        records = self._records.setdefault(parent, {})
        existing = records.get(aid)
        if existing is not None:
            if existing.state == COLLECTING and not existing.retired:
                existing.deadline = max(
                    existing.deadline,
                    min(deadline, existing.first_reserved + MAX_COLLECTION_TTL_SECS),
                )
                return True
            self._prune(parent)
            return False
        # Every retained record holds its place, whatever its state: a member
        # admitted here must still fit when its reservation becomes a claim.
        if len(records) >= MAX_IDS_PER_PARENT:
            self._prune(parent)
            return False
        records[aid] = _Record(aid, parent, COLLECTING, deadline, first_reserved=now, call=call)
        self._extend_call(parent, call, deadline)
        self._schedule_expiry(parent, ttl)
        return True

    def _extend_call(self, parent: str, call: str, deadline: float) -> None:
        """Move every member *call* still collects for *parent* to *deadline*, if later.

        The call starts its ``max_wait`` poll only once its last spawn
        returned, so a collection is due from the call's latest reservation,
        never from each member's own: a member reserved early stays held while
        its call is still spawning or polling. No member is moved past its own
        reservation plus ``MAX_COLLECTION_TTL_SECS``, however many reserves
        follow.
        """
        for rec in self._records.get(parent, {}).values():
            if rec.call != call or rec.state not in (COLLECTING, HELD):
                continue
            rec.deadline = max(
                rec.deadline, min(deadline, rec.first_reserved + MAX_COLLECTION_TTL_SECS)
            )

    def _of_call(self, parent: str, call: str | None) -> set[str]:
        """The ids *call* reserved for *parent* that are still collecting or held."""
        if call is None:
            return set()
        return {
            aid
            for aid, rec in self._records.get(parent, {}).items()
            if rec.call == call and rec.state in (COLLECTING, HELD)
        }

    def finish(
        self,
        parent: str,
        released: Iterable[str],
        returned: Iterable[str],
        *,
        call: str | None = "",
    ) -> None:
        """End collection of *released*; *returned* is named in the call's result.

        Synchronous. A member the result names is CLAIMED, in place, so it keeps
        the capacity its reservation took: nothing is settled until ``commit``
        hears the response reached the parent. A held member the result does not
        name is delivered as an ordinary completion in the background, since
        that waits for the parent's turn, which is blocked on the caller. Every
        member *call* reserved is released too, including one the call never
        learned; ``None`` names no call.
        """
        returned_set = set(returned)
        records = self._records.get(parent, {})
        for aid in set(released) | returned_set | self._of_call(parent, call):
            rec = records.get(aid)
            if rec is None or rec.state not in (COLLECTING, HELD):
                continue
            if aid in returned_set:
                self._claim(rec)
            else:
                self._end_collection(rec)
        self._prune(parent)

    def commit(
        self,
        parent: str,
        ids: Iterable[str],
        delivered: bool,
        *,
        released: Iterable[str] = (),
        call: str | None = "",
    ) -> list[asyncio.Task[str]]:
        """Settle or release *ids*: the call's response was written or dropped.

        Synchronous up to the returned tasks, which settle the results whose
        completion is in hand; the caller awaits them. A written response is
        the stronger fact, so it settles *ids* whether or not their claim
        landed first: a claim that is late, or lost, then finds them settled
        and changes nothing. A result whose completion has not arrived becomes
        ``returned`` (written), so that completion is consumed, or leaves the
        registry (dropped), so it takes the ordinary route. A dropped result
        whose completion is in hand is delivered as an ordinary completion.
        *released* ends the collection of the call's other members, as the
        claim would have, and so does *call* for every member it reserved.
        """
        records = self._records.get(parent, {})
        named = set(ids)
        settles: list[asyncio.Task[str]] = []
        for aid in (set(released) | self._of_call(parent, call)) - named:
            rec = records.get(aid)
            if rec is not None and rec.state in (COLLECTING, HELD):
                self._end_collection(rec)
        for aid in named:
            rec = records.get(aid)
            if rec is None or rec.state not in (COLLECTING, HELD, CLAIMED):
                continue
            if rec.retired:
                # The parent ended before the response was confirmed, so
                # nothing is settled and restart recovery keeps it. If the
                # response was in fact written before the teardown, that
                # recovery delivers it again: a duplicate, never a loss.
                # One whose completion has not arrived stays until its deadline,
                # so ``hold`` still fences that completion.
                if rec.held:
                    self._parent_retired(rec)
                    self._remove(rec)
            elif not delivered and rec.state != CLAIMED:
                self._end_collection(rec)  # its claim never landed: as a close
            elif not rec.held:
                if delivered:
                    rec.state = RETURNED
                    rec.deadline = self._clock() + COLLECTED_TTL_SECS
                    # Its own timer: a completion that never reaches
                    # ``consume_collected`` (a fenced one, say) must not leave
                    # the marker for a parent key nothing touches again.
                    self._schedule_expiry(parent, COLLECTED_TTL_SECS)
                else:
                    self._remove(rec)
            else:
                task = self._release(rec, returned=delivered)
                if task is not None and delivered:
                    settles.append(task)
        self._prune(parent)
        return settles

    def _end_collection(self, rec: _Record) -> None:
        """The call is over without returning *rec*: forget it, or deliver what it held."""
        if rec.state == COLLECTING:
            self._remove(rec)
        else:
            self._release(rec, returned=False)

    def discard(self, parent: str, aid: str) -> None:
        """Forget *aid*: its spawn was refused, so nothing ran under the id."""
        rec = self._records.get(parent, {}).get(aid)
        if rec is not None and rec.state in (COLLECTING, HELD):
            self._remove(rec)

    # ── gateway side ──

    def hold(self, parent: str, aid: str) -> bool:
        """Keep *aid*'s completion undelivered if a live call collects it.

        Only marks the record: the caller marks the run ``_delivery_queued``,
        and the pin ``reserve`` took keeps the run out of completed-run
        eviction until collection ownership ends.
        """
        self._expire(parent)
        rec = self._records.get(parent, {}).get(aid)
        if rec is None or rec.state not in (COLLECTING, CLAIMED) or rec.held:
            return False
        rec.held = True
        if rec.retired:
            # Its parent ended while it ran: owned, and dropped by the fence.
            self._parent_retired(rec)
            self._remove(rec)
            return True
        if rec.state == COLLECTING:
            rec.state = HELD
        return True

    def pins(self, info: Any) -> bool:
        """True while a collection owns *info*'s run, so retention keeps it.

        ``evict_completed_agents`` asks it. The pin is taken at ``reserve``,
        before the run can finish, and kept through hold, claim and delivery:
        the registry keeps no copy, so the live run is what a release reads.
        Ownership ends, and the pin with it, when the record leaves the
        registry (commit, drop, discard, expiry, delivery done or cancelled at
        shutdown), when a written commit leaves only the ``returned`` marker,
        or when the parent is retired. Bounded by ``MAX_IDS_PER_PARENT`` and
        each collection's expiry.
        """
        records = self._records.get(getattr(info, "parent_session_key", ""), {})
        rec = records.get(getattr(info, "id", ""))
        return rec is not None and _owns_run(rec)

    def has_collected(self, parent: str) -> bool:
        """True while any claimed or returned id for *parent* still awaits its completion."""
        now = self._clock()
        return any(
            r.state in (CLAIMED, RETURNED) and not r.held and r.deadline > now
            for r in self._records.get(parent, {}).values()
        )

    def consume_collected(self, parent: str, aid: str) -> bool:
        """True (and forget it) when a call already returned *aid* inline."""
        rec = self._records.get(parent, {}).get(aid)
        if rec is None or rec.state != RETURNED:
            return False
        self._remove(rec)
        return rec.deadline > self._clock()

    # ── teardown ──

    def retire(self, parent: str) -> None:
        """Fence every record of *parent*: its conversation has ended.

        Synchronous, so it is in place before the teardown's first await. A held
        completion is dropped now. The delivery of one the call did not return is
        cancelled now, wherever it is suspended, including inside the parent's
        route after the fence was read, so it can never resume into the
        conversation's allocation or injection. A collecting or claimed member's
        late completion is dropped by ``hold``, and a claim's ``commit`` settles
        nothing and leaves the record in place until its deadline, so that
        completion is still fenced. A result whose response was written already reached the parent,
        so its settle is left to finish.
        """
        records = self._records.get(parent)
        if not records:
            return
        for rec in list(records.values()):
            rec.retired = True
            held = rec.state in (HELD, CLAIMED) and rec.held
            if held or rec.task is not None:
                # Dropped here, not by the cancelled task: a task cancelled
                # before its first step never runs its body.
                self._parent_retired(rec)
                self._remove(rec)
            if rec.task is not None:
                rec.task.cancel()
                rec.task = None

    # ── the one delivery ──

    def _claim(self, rec: _Record) -> None:
        rec.state = CLAIMED
        rec.deadline = self._clock() + CLAIM_TTL_SECS
        self._schedule_expiry(rec.parent, CLAIM_TTL_SECS)

    def _release(self, rec: _Record, *, returned: bool) -> asyncio.Task[str] | None:
        if rec.retired:
            self._parent_retired(rec)
            self._remove(rec)
            return None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("inline result %s released with no running loop; kept held", rec.aid)
            return None
        rec.state = DELIVERING
        task = loop.create_task(self._deliver(rec, returned=returned))
        if not returned:
            rec.task = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _deliver(self, rec: _Record, *, returned: bool) -> str:
        """Take a released held completion to its terminal outcome.

        The ONE delivery for every path that releases a held completion: the
        call's close, the response's commit (written or dropped) and expiry. A
        result whose response was written is settled at once, even if its parent
        is retired before the settle runs: the response already reached it. One it did not
        return waits for the run's own terminal report (the hold was taken inside
        it) and for the parent's turn to end, then takes the ordinary route on a separate
        ticket, so the original record's ``_delivery_queued`` is never touched
        and its report never writes a mark this delivery owns. The fence is read
        after every wait, at the point of delivery, and a teardown that lands
        later, inside the route, cancels the delivery (``retire``).
        """
        outcome = UNDELIVERED
        try:
            outcome = await self._deliver_once(rec, returned=returned)
        except asyncio.CancelledError:
            if not rec.retired:
                raise  # shutdown: the folder stays for restart recovery
            # ``retire`` cancelled it, and has already dropped it.
        except Exception:
            logger.exception("Subagent %s: delivering a held inline result failed", rec.aid)
        finally:
            rec.task = None
            self._remove(rec)
        return outcome

    async def _deliver_once(self, rec: _Record, *, returned: bool) -> str:
        mgr = self._manager
        if returned:
            # The written response carried it to the parent before any later
            # teardown, so it is delivered whatever happens to the parent now.
            info = self._live(rec)
            if info is None:
                logger.warning(
                    "Subagent %s: its run record is gone, so its delivered mark is "
                    "left to restart recovery",
                    rec.aid,
                )
                return UNDELIVERED
            await self._settle([info])
            return DELIVERED
        await self._await_terminal_report(rec.aid)
        await self._await_parent_idle(rec.parent)
        info = self._live(rec)
        ticket = _ticket(info) if info is not None else await self._gone_ticket(rec)
        if self._parent_retired(rec):
            return UNDELIVERED
        on_done = mgr._on_done if mgr is not None else None
        if on_done is None:
            return UNDELIVERED  # restart recovery delivers the folder
        logger.info(
            "Subagent %s: spawn_sub_agents ended without returning it; delivering it to %s",
            rec.aid,
            rec.parent,
        )
        try:
            await on_done(ticket)
        except Exception:
            logger.exception("Subagent %s: delivering a released inline result failed", rec.aid)
            return UNDELIVERED
        if rec.retired:
            # Retired inside the route, which swallowed the cancel: ``retire``
            # already dropped it, and what the route did reached no live parent.
            return UNDELIVERED
        # The route has returned: what it did reached the parent before any
        # teardown, so a later ``retire`` must not cancel the settle below.
        rec.task = None
        if ticket._report_undelivered:
            # The route gave up (e.g. the parent's allocation failed): the
            # folder stays un-tombstoned and an owed report owed, for restart.
            return UNDELIVERED
        if ticket._delivery_queued:
            return HANDED_OFF
        await self._settle([ticket])
        return DELIVERED

    def _completion_keep_mode(self) -> str:
        mode = getattr(self._manager, "_completion_keep", "head")
        return mode if mode in ("head", "tail", "both") else "head"

    def _live(self, rec: _Record) -> Any | None:
        """The held member's run record, read from the manager, or None when gone.

        Result, error and status all come from it, so a failed member is never
        announced as a success, and nothing the registry kept can be stale.
        """
        get = getattr(self._manager, "get", None)
        if not callable(get):
            return None
        try:
            live = get(rec.aid)
        except Exception:
            logger.debug("Could not read the live run %s", rec.aid, exc_info=True)
            return None
        if getattr(live, "id", None) != rec.aid or not hasattr(live, "_delivery_queued"):
            return None
        return live

    async def _gone_ticket(self, rec: _Record) -> Any:
        """An explicit error for a held member whose run record is gone at release.

        Never an empty success: the outcome is an error naming what happened,
        and the result file when it is still readable.
        """
        from kiro_crew.subagent import SubagentInfo
        from kiro_crew.subagent_persistence import agent_dir_for_display

        path = ""
        try:
            path = str(agent_dir_for_display(rec.aid) / "result.txt")
        except Exception:
            logger.debug("Could not name the result file of %s", rec.aid, exc_info=True)
        on_disk = await asyncio.to_thread(_transcript_on_disk, path)
        note = "[run record gone before its held result was released"
        note += f"; full result: {path}]" if on_disk else "; result not retained]"
        logger.warning("Subagent %s: %s", rec.aid, note)
        return _ticket(
            SubagentInfo(
                id=rec.aid,
                task="",
                done=True,
                parent_session_key=rec.parent,
                error=note,
                result_path=path if on_disk else "",
            )
        )

    def _parent_retired(self, rec: _Record) -> bool:
        """True, with the result's delivery dropped, when its parent was retired.

        The terminal report's own statement: no injection, which would recreate
        the retired conversation or reach its replacement, and no delivered mark,
        so restart reconciliation still finds the folder. An owed memory-wait
        report is owed only to the parent that ended, so it is cleared.
        """
        if not rec.retired:
            return False
        if rec.fenced:
            return True
        rec.fenced = True
        info = self._live(rec)
        if info is not None and getattr(info, "_report_owed", False) is True:
            try:
                self._manager._admission.taskq_clear_owed_reports([rec.aid])
            except Exception:
                logger.debug("Could not clear the owed report of %s", rec.aid, exc_info=True)
        logger.info("Subagent %s: held inline result dropped -- its parent ended", rec.aid)
        return True

    async def _settle(self, completions: list[Any]) -> None:
        deliveries = settle_deliveries(completions)
        if not deliveries or self._manager is None:
            return
        try:
            await self._manager.settle_queued_delivery(deliveries)
        except Exception:
            logger.debug("Could not settle held inline deliveries", exc_info=True)

    async def _await_terminal_report(self, aid: str) -> None:
        # The hold was taken inside the run's terminal report, which may still be
        # finishing (a held cron parent's idle reset awaits). Take over only once
        # it has returned, so the two never decide the same delivery.
        mgr = self._manager
        if mgr is None:
            return
        from kiro_crew.subagent import INJECTION_TIMEOUT

        # The registry keeps only the id, so the report is found by it.
        pending = [
            task for task, owner in mgr._report_owners.items() if getattr(owner, "id", None) == aid
        ]
        if not pending:
            return
        bound = INJECTION_TIMEOUT + TERMINAL_REPORT_GRACE_SECS
        try:
            await asyncio.wait_for(
                asyncio.gather(*(asyncio.shield(t) for t in pending), return_exceptions=True),
                bound,
            )
        except asyncio.TimeoutError:
            # A hung report must not keep the result, and its capacity, here
            # for good. The hold parked the report's own delivery, so it never
            # decides this one: the result goes out on its separate ticket.
            logger.warning(
                "Subagent %s: its terminal report is still running after %.0fs; "
                "delivering the released result without waiting further",
                aid,
                bound,
            )

    async def _await_parent_idle(self, parent: str) -> None:
        """Wait, bounded, for *parent*'s turn to end before a released result plays.

        A cancelled call closes its collection while the turn that ran it is
        still ending, so the result plays as a new turn after it, never as a
        prompt inside it. TEMPORARY: the registry's one busy-parent wait, with
        exactly one call site (``_deliver_once``; a test pins both). The general
        busy-parent fence on the completion route owns this wait once it lands
        (docs/system-specs/modules/subagent.md names its issue), and this method
        is deleted then; the wire contract stays.
        """
        from kiro_crew.subagent import INJECTION_TIMEOUT

        sessions = getattr(self._manager, "_sessions", None)
        if sessions is None:
            return
        deadline = time.monotonic() + INJECTION_TIMEOUT
        while sessions.is_busy(parent) and time.monotonic() < deadline:
            await asyncio.sleep(_ORPHAN_IDLE_POLL_SECS)

    # ── internals ──

    def _expire(self, parent: str) -> None:
        records = self._records.get(parent)
        if not records:
            return
        now = self._clock()
        expired = [
            r
            for r in records.values()
            if r.state in (COLLECTING, HELD, CLAIMED) and r.deadline <= now
        ]
        if expired:
            held = [r for r in expired if r.held and not r.retired]
            if held:
                logger.warning(
                    "spawn_sub_agents collection for %s never reported back; "
                    "delivering %d held result(s) as ordinary completions",
                    parent,
                    len(held),
                )
            calls = {r.call for r in expired if r.state == CLAIMED}
            for rec in expired:
                # A claim nobody confirmed is released like a dropped response:
                # a duplicate turn is recoverable, a lost result is not.
                if rec.held:
                    self._release(rec, returned=False)
                else:
                    del records[rec.aid]
            # Its call is over: end every member it reserved, as its drop would.
            for call in calls:
                for aid in self._of_call(parent, call):
                    self._end_collection(records[aid])
        self._drop_expired_returns(parent, now)
        self._prune(parent)

    def _drop_expired_returns(self, parent: str, now: float) -> None:
        records = self._records.get(parent, {})
        for aid in [a for a, r in records.items() if r.state == RETURNED and r.deadline <= now]:
            del records[aid]
        self._prune(parent)

    def _schedule_expiry(self, parent: str, ttl: float) -> None:
        """Have *parent*'s one expiry timer fire by *ttl* (plus a second) from now.

        A parent has at most one timer, and it never waits longer than
        ``_EXPIRY_SWEEP_SECS``. Every deadline a record is given is at least
        that far away, so a timer already pending fires no later than any new
        deadline and nothing is ever rescheduled or cancelled. When it fires it
        re-arms for the parent's earliest remaining deadline. A pending timer
        on a loop that has closed never fires, so it is replaced.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (sync caller): the next reserve/hold call expires it
        pending = self._timers.get(parent)
        if pending is not None and not pending.is_closed():
            return
        loop.call_later(min(max(ttl, 0.0), _EXPIRY_SWEEP_SECS) + 1.0, self._on_timer, parent, loop)
        self._timers[parent] = loop

    def _on_timer(self, parent: str, loop: asyncio.AbstractEventLoop) -> None:
        if self._timers.get(parent) is loop:
            del self._timers[parent]
        self._expire(parent)
        # Re-arm for the earliest deadline still pending.
        records = self._records.get(parent)
        if not records:
            return
        due = [
            r.deadline for r in records.values() if r.state in (COLLECTING, HELD, CLAIMED, RETURNED)
        ]
        if due:
            self._schedule_expiry(parent, min(due) - self._clock())

    def _remove(self, rec: _Record) -> None:
        records = self._records.get(rec.parent)
        if records is not None and records.get(rec.aid) is rec:
            del records[rec.aid]
        self._prune(rec.parent)

    def _prune(self, parent: str) -> None:
        if parent in self._records and not self._records[parent]:
            del self._records[parent]


#: Each caller-supplied identity a record keeps, with its bound.
_BOUND_OF = {
    "parent_session": MAX_PARENT_KEY_CHARS,
    "agent_id": MAX_AGENT_ID_CHARS,
    "call_id": MAX_CALL_ID_CHARS,
}


def oversized_identity(parent: str, ids: Iterable[str] = (), call: str = "") -> str:
    """The field past its bound, ``parent_session``, ``agent_id`` or ``call_id``, else ``""``.

    The entry check for every caller-supplied string a record keeps: its
    parent key, agent id and call id. Its state, deadline and flags are set
    here, and its result, error and status are read from the live run, never
    stored.
    """
    if len(parent) > MAX_PARENT_KEY_CHARS:
        return "parent_session"
    if any(len(aid) > MAX_AGENT_ID_CHARS for aid in ids):
        return "agent_id"
    if len(call) > MAX_CALL_ID_CHARS:
        return "call_id"
    return ""


def _length_of(field: str, parent: str, ids: Iterable[str], call: str) -> int:
    """The length of the *field* ``oversized_identity`` named, for its refusal."""
    if field == "parent_session":
        return len(parent)
    if field == "call_id":
        return len(call)
    return max((len(aid) for aid in ids), default=0)


def _owns_run(rec: _Record) -> bool:
    """Whether *rec* still owns its run, which is then pinned against eviction.

    A ``returned`` marker owns nothing: the written response carried the
    result. A retired parent's records are fenced and dropped, except a written
    response's settle, which is still delivering.
    """
    if rec.state == RETURNED:
        return False
    return not rec.retired or rec.state == DELIVERING


def _transcript_on_disk(path: Any) -> bool:
    """Whether *path* names a readable ``result.txt`` a released result can point at.

    One ``stat`` and one access check, run off the loop at release, so the
    note never points at a file that is already gone.
    """
    if not isinstance(path, str) or not path:
        return False
    try:
        return os.path.isfile(path) and os.access(path, os.R_OK)
    except (OSError, ValueError):
        return False


def _ticket(info: Any) -> Any:
    """A separate record for one redelivery of *info*'s completion.

    *info* is the live run record (or the gone-run error record). The route
    writes its routing flags on what it is handed. Those decide this delivery's
    outcome, so they are written on a copy that lives for that one delivery:
    the held record's flags stay what its own terminal report left them.
    """
    ticket = copy.copy(info)
    ticket._delivery_queued = False
    ticket._report_undelivered = False
    return ticket


def settle_deliveries(completions: Iterable[Any]) -> list[Any]:
    """The deliveries to settle for held *completions* that have now reached the parent.

    A completed run settles its ``delivered`` mark. A memory-wait expiry has no
    folder: what it owes is the store's report, which was not cleared when its
    report task returned because the hold had parked it (``_delivery_queued``),
    so it is settled as an owed report instead.
    """
    from kiro_crew.subagent import SubagentDelivery

    out: list[Any] = []
    for info in completions:
        if info.outcome == "completed":
            out.append(SubagentDelivery(info.id, info.elapsed, info.credits))
        elif getattr(info, "_report_owed", False) is True:
            out.append(SubagentDelivery(info.id, info.elapsed, info.credits, report_owed=True))
    return out
