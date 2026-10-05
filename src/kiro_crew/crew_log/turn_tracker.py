"""What each running turn owns while it is in flight, and the memos keyed to it.

The emitter (:mod:`kiro_crew.crew_log.emit`) numbers a turn's model calls and tool calls,
settles each tool call exactly once, counts reruns of one turn ordinal, and defers a
superseded log's tail repair while a turn of that log is still running. All of that is
state CORRELATED TO a live turn, and one rule governs it: an entry is released by an
event of its OWN turn -- the turn's terminal, or its session closing -- never by pressure
from other sessions. :class:`TurnTracker` is that state and that rule, in one place.

Interface -- what a caller must know:

* Lifecycle: :meth:`~TurnTracker.begin`, :meth:`~TurnTracker.closing` (returns the
  release the turn's terminal owes), :meth:`~TurnTracker.release_unclosed`,
  :meth:`~TurnTracker.live_turn`.
* Ordinals: :meth:`~TurnTracker.next_step`, :meth:`~TurnTracker.current_step`,
  :meth:`~TurnTracker.next_call_index`, :meth:`~TurnTracker.next_attempt`,
  :meth:`~TurnTracker.seed_attempts`, :meth:`~TurnTracker.knows_attempts`.
* Tool calls: :meth:`~TurnTracker.call_opened`, :meth:`~TurnTracker.call_settled`,
  :meth:`~TurnTracker.sweep_open_calls`.
* Supersede repair debt: :meth:`~TurnTracker.stand_down`,
  :meth:`~TurnTracker.repair_debt`, :meth:`~TurnTracker.take_repair_debt`.
* Teardown and bounds: :meth:`~TurnTracker.forget_session`, :meth:`~TurnTracker.trim`,
  :meth:`~TurnTracker.pop_unless_live`.

Invariants: a live turn's record is never evicted, so its ordinals never restart; a
tool call settles once; the repair debt is recorded in the same lock hold as the
liveness check that decided it; a session's debt is paid only once it has no live turn.

Ordering constraint: the tracker's lock is held only inside these methods, so a caller
holding a lock of its own may call in (:meth:`trim` and :meth:`pop_unless_live` exist
for exactly that) -- but nothing may call into such a caller while holding this lock.
Three things do leave the tracker under it, and each must keep to that rule: log
records (the leak alarm and the trims report while the lock is held), the caller's
``session_of`` callback in :meth:`trim`, and the finalizers of values popped from a
caller's store (a ``CrewLog`` handle's lease release). In-process only: no I/O, no
storage.

Imported lazily by the emitter, for the boot-path reason that module's ``_crew_log``
gives.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, NamedTuple

_logger = logging.getLogger(__name__)

#: A last-resort ceiling on live-turn records, set far above any plausible number of
#: concurrent turns. Reaching it does not mean the gateway is busy; it means turns are
#: ending without their terminal event landing, so nothing releases their state. At that
#: point the records whose terminal is already queued are shed and the leak is reported at
#: error level; a live turn is never evicted, because restarting its numbering corrupts
#: the log, so a ceiling reached with every record still live is accepted as an overage
#: rather than acted on.
MAX_LIVE_TURNS: Final[int] = 4096

#: Bound on open tool calls and on settled-call markers.
MAX_OPEN_CALLS: Final[int] = 512

#: Bound on the per-session attempt map.
MAX_SESSIONS: Final[int] = 128


@dataclass
class _LiveTurn:
    """What a turn in flight owns. Created at its start, dropped at its end.

    Two counters, and the difference between them is the point.

    ``step`` numbers the turn's MODEL CALLS. One turn is several: the model speaks, calls
    tools, and is called again with their results. It is minted by
    :meth:`TurnTracker.next_step` and every entry produced inside that call carries it, so
    a reader can ask what one model call cost without inferring boundaries.

    ``call_index`` numbers the turn's TOOL CALLS. It answers a different question -- the
    order the runner issued the calls in -- and a step can issue several tools at once,
    so the step alone cannot order them.

    One record rather than a map per counter: two structures can be evicted separately,
    and losing either counter mid-turn restarts its numbering so two entries claim one
    ordinal. One record cannot be half-dropped.
    """

    step: int = 0
    call_index: int = 0
    #: True once this turn's terminal event has been HANDED to the writer. The entry is
    #: queued rather than written, so the pin is owed to the write job that releases it --
    #: and a claim must not take it back in the meantime. Without this a leaked record and
    #: one whose closer is in flight are indistinguishable, and the claim treats both as
    #: stale.
    closer_owed: bool = False


class SettledCall(NamedTuple):
    """What a tool call's closer carries from its opener.

    ``elapsed_ms`` is -1 when no opener was recorded -- a call whose ``tool/called`` this
    tracker never saw still gets its closer, with empty name and server and no elapsed.
    """

    elapsed_ms: int
    name: str
    server: str
    call_index: int
    step: int


class SweptCall(NamedTuple):
    """One open call :meth:`TurnTracker.sweep_open_calls` closed."""

    call_id: str
    elapsed_ms: int
    name: str
    server: str
    call_index: int
    step: int


class TurnTracker:
    """The live-turn records and every memo keyed to them. Thread-safe.

    ``max_live_turns`` is the leak alarm (:data:`MAX_LIVE_TURNS`), ``max_open_calls``
    bounds the open-call registry and the settled markers, ``max_sessions`` bounds the
    attempt map. ``clock`` times a tool call from its opener to its closer, and ``logger``
    receives the leak and overage reports.
    """

    def __init__(
        self,
        *,
        max_live_turns: int = MAX_LIVE_TURNS,
        max_open_calls: int = MAX_OPEN_CALLS,
        max_sessions: int = MAX_SESSIONS,
        clock: Callable[[], float] = time.monotonic,
        logger: logging.Logger | None = None,
    ) -> None:
        self._max_live_turns = max_live_turns
        self._max_open_calls = max_open_calls
        self._session_limit = max_sessions
        self._clock = clock
        self._log = logger if logger is not None else _logger
        self._lock = threading.Lock()
        #: (session, turn) -> the state that turn OWNS while it is in flight, created at
        #: ``turn/started`` and dropped by that turn's own end. The step counter lives
        #: HERE, in the same record as the pin, rather than in a map of its own: two
        #: structures can be evicted separately, and losing the counter while the turn
        #: still runs restarts the numbering so two of its entries claim one ordinal.
        self._live: "OrderedDict[tuple[str, int], _LiveTurn]" = OrderedDict()
        #: session -> how many of its turns are live. Derived from ``_live`` and
        #: maintained with it under the same lock, so the two cannot drift. Read by the
        #: eviction rule, which needs the question answered per SESSION: a handle is per
        #: session, while a step ordinal is per turn.
        self._pinned: "dict[str, int]" = {}
        #: (session, call_id) -> (began, name, server, call_index, step, turn). The
        #: completion frame repeats none of this, so the call frame's identity, its
        #: position among the turn's calls and the model call it belonged to are all
        #: remembered here and filled in when it closes. The TURN is part of the value
        #: because a closer must name the turn the call was OPENED in: a call left open by
        #: one turn and closed under the next one's ordinal is a false statement about a
        #: turn that never used the tool.
        self._open_calls: "OrderedDict[tuple[str, str], tuple[float, str, str, int, int, int]]" = (
            OrderedDict()
        )
        #: (session, call_id) -> the turn the call was settled in. A tool call settles
        #: exactly ONCE: the first terminal frame writes its closer, and a later frame for
        #: the same id must add nothing. Both update parsers can emit a status-only result
        #: for one terminal frame, so two closers for the same call is a live hazard rather
        #: than a corner case. Popping the open-call record alone cannot tell "already
        #: settled by us" from "never opened" -- both find no started record -- so this map
        #: records the calls this tracker has closed. Pruned on the SAME lifecycle as the
        #: open calls: per turn when its record is released, for a closed session's gone
        #: turns in :meth:`forget_session`. Its CAP is its own (:meth:`_bound_settled`),
        #: because a marker accumulates per completed call where an open-call record is
        #: popped by one, so the shared never-evict-a-live-turn rule would leave this map
        #: growing for a whole turn.
        self._settled: "OrderedDict[tuple[str, str], int]" = OrderedDict()
        #: session -> {turn ordinal -> the highest attempt opened at it}. A regenerate or a
        #: rewind reruns a turn the ordinal already names, so without this two starts at one
        #: ordinal are indistinguishable and a fold cannot tell a retry from a duplicate
        #: write. Seeded from the file on resume, so a restart between two retries does not
        #: reset the count and claim attempt 1 twice.
        self._attempts: "OrderedDict[str, dict[int, int]]" = OrderedDict()
        #: Superseded logs whose tail repair STOOD DOWN for a turn still running, mapped to
        #: the slot that owes it. The stand-down is correct -- that turn ends in its own real
        #: ``turn/completed`` -- but it is correct only while that terminal is still coming,
        #: and a terminal that spends its attempt budget is dropped instead. This is what
        #: lets the drop re-queue the repair: without it the terminal's drop and the waiting
        #: repair are two facts no site holds together. Keyed by the PREDECESSOR's id,
        #: because that is the session whose terminal resolves the question and the session
        #: the repair would be written into.
        self._repair_owed: "dict[str, str]" = {}
        #: True once the live-turn cap overage has been reported, so a genuinely busy
        #: gateway names the condition once rather than on every event while over the cap.
        #: Cleared when the map falls back to the cap, so a later recurrence is named again.
        self._overage_reported = False

    # -- lifecycle -------------------------------------------------------------------

    def live_turn(self, session_id: str) -> int:
        """The ordinal of *session_id*'s running turn, or 0 when none is running.

        The HIGHEST live ordinal, because a session can hold more than one: a nested turn
        pins its own record while its parent's is still open, and the newest is the one
        currently producing entries. 0 is not a turn, so a caller must treat it as "no
        running turn" rather than stamp it on an entry.
        """
        if not session_id:
            return 0
        with self._lock:
            return self._live_turn_locked(session_id)

    def begin(self, session_id: str, turn: int) -> None:
        """Open the live-turn record for *turn*.

        Idempotent on purpose: a second start for the same turn keeps the record it
        already has, because resetting the step counter is the corruption this state
        exists to prevent.
        """
        if not session_id:
            return
        with self._lock:
            self._live_state_locked(session_id, turn)

    def release_unclosed(self, session_id: str) -> None:
        """Close every live-turn record of one session that no terminal is coming for.

        Used by a fresh claim. A record whose terminal event is already queued is LEFT
        ALONE. Its pin is owed to the write job that will release it, and taking it back
        here lets capacity eviction drop the handle -- and the lease with it -- while the
        file still shows that turn open, so a successor process repairs a turn whose real
        completion is still on its way to disk. That is the two-outcomes-for-one-turn
        hazard, one process removed. A record with no terminal handed over is what this
        release is for: nothing else will ever close it.
        """
        if not session_id:
            return
        with self._lock:
            for key in [k for k in self._live if k[0] == session_id]:
                state = self._live.get(key)
                if state is not None and state.closer_owed:
                    continue
                self._release_locked(*key)

    def closing(self, session_id: str, turn: int) -> Callable[[], None]:
        """Mark *turn*'s pin as owed to the writer, and return the release for it.

        Called where a terminal event is HANDED OVER, which is not where it lands: the
        entry is queued, and the pin it releases has to outlive the handover so eviction
        cannot take the handle before the closer is on disk. The mark is set synchronously
        here, ahead of the queueing, because a claim arriving in that window reads the live
        map and has no other way to tell this turn from one that leaked.

        The release runs when the terminal RESOLVES, written or dropped alike, which is
        also when a repair that stood down for this turn stops being owed anything. A drop
        has already re-queued it and taken the debt by then -- its hook runs ahead of the
        release -- so what the release clears is the LANDED case, where the tail closed
        truthfully and the debt is simply paid. But the debt is keyed by SESSION, and a
        session can hold several live turns at once: the stand-down stays warranted while
        ANY of them remains, so the debt is paid only once the session has no live turn
        left. Clearing it on the first terminal to land would let a LATER live turn's own
        terminal drop with nothing left to re-queue the repair.
        """
        with self._lock:
            state = self._live.get((session_id, int(turn)))
            if state is not None:
                state.closer_owed = True

        def _release() -> None:
            with self._lock:
                self._release_locked(session_id, turn)
                if not any(sid == session_id for (sid, _) in self._live):
                    self._repair_owed.pop(session_id, None)

        return _release

    # -- ordinals --------------------------------------------------------------------

    def next_step(self, session_id: str, turn: int) -> int:
        """The next MODEL-CALL ordinal inside *turn*."""
        with self._lock:
            state = self._live_state_locked(session_id, turn)
            state.step += 1
            return state.step

    def current_step(self, session_id: str, turn: int) -> int:
        """Which model call *turn* is inside right now, or 0 before the first.

        Reads without minting: every entry produced during a model call names that call,
        and only :meth:`next_step` may advance it. Zero means no step has been announced,
        and the entry then omits ``step`` rather than claiming to belong to a model call
        nobody observed.
        """
        if not session_id:
            return 0
        with self._lock:
            state = self._live.get((session_id, int(turn)))
            return 0 if state is None else state.step

    def next_call_index(self, session_id: str, turn: int) -> int:
        """The next TOOL-CALL ordinal inside *turn*.

        Numbers the tool calls of one turn in the order the runner made them, so a reader
        can order them without comparing seq -- and can tell two calls of the same tool
        apart when their ids are opaque. The counter is the turn's own state, so no
        pressure from other sessions can reset it and hand two calls the same ordinal.
        """
        with self._lock:
            state = self._live_state_locked(session_id, turn)
            state.call_index += 1
            return state.call_index

    def next_attempt(self, session_id: str, turn: int) -> int:
        """Which try this is at *turn*, counting from 1, and record it.

        A regenerate or a rewind reruns a turn the ordinal already names. Without a
        discriminator the two starts are identical lines, so a fold cannot tell a retry
        from a duplicate write, and ``(turn, attempt)`` is the identity it groups on.
        """
        with self._lock:
            seen = self._attempts.setdefault(session_id, {})
            attempt = seen.get(int(turn), 0) + 1
            seen[int(turn)] = attempt
            self._attempts.move_to_end(session_id)
            self._trim_locked(self._attempts, self._session_limit, lambda k: k, "turn attempts")
            return attempt

    def seed_attempts(self, session_id: str, highest: Mapping[int, int]) -> None:
        """Raise *session_id*'s attempt counts to at least *highest*, per ordinal.

        For a resume, whose counts belong to a process that is gone, and a session whose
        map the bound evicted: the caller reads the file's ``turn/started`` entries and
        hands their highest attempt per ordinal here. A count already above the file's is
        kept.
        """
        if not highest:
            return
        with self._lock:
            seen = self._attempts.setdefault(session_id, {})
            for turn, attempt in highest.items():
                if attempt > seen.get(turn, 0):
                    seen[turn] = attempt
            self._attempts.move_to_end(session_id)
            self._trim_locked(self._attempts, self._session_limit, lambda k: k, "turn attempts")

    def knows_attempts(self, session_id: str) -> bool:
        """Whether memory already holds *session_id*'s attempt counts."""
        with self._lock:
            return session_id in self._attempts

    # -- tool calls ------------------------------------------------------------------

    def call_opened(
        self,
        session_id: str,
        call_id: str,
        *,
        name: str,
        server: str,
        call_index: int,
        step: int,
        turn: int,
    ) -> None:
        """Remember an open call's identity, position and start time until it closes."""
        with self._lock:
            self._open_calls[(session_id, call_id)] = (
                self._clock(),
                name,
                server,
                call_index,
                step,
                int(turn),
            )
            self._trim_locked(
                self._open_calls, self._max_open_calls, lambda k: k[0], "pending tool calls"
            )

    def call_settled(self, session_id: str, call_id: str, turn: int) -> SettledCall | None:
        """Settle a call. ``None`` when it was already settled, so no closer is owed.

        A call settles exactly ONCE. Both update parsers can produce a status-only terminal
        frame for the same id, so a second frame arriving after the first closed the call
        must add NOTHING. A frame whose id was never opened still settles and still gets
        its closer, which is the :class:`SettledCall` with ``elapsed_ms`` -1.
        """
        with self._lock:
            if (session_id, call_id) in self._settled:
                return None
            started = self._open_calls.pop((session_id, call_id), None)
            self._settled[(session_id, call_id)] = int(turn)
            self._settled.move_to_end((session_id, call_id))
            self._bound_settled_locked()
        if started is None:
            return SettledCall(-1, "", "", 0, 0)
        began, name, server, call_index, step, _ = started
        return SettledCall(
            max(0, int((self._clock() - began) * 1000)), name, server, call_index, step
        )

    def sweep_open_calls(self, session_id: str, turn: int) -> list[SweptCall]:
        """Close and settle every call of *turn* still open, and return them.

        Filtered on the turn as well as the session. A transient can leave a call open and
        the same session then runs another turn, so selecting every open call would close
        the earlier turn's call under THIS turn's ordinal -- a tool attributed to a turn
        that never used it. A call still open past its own turn's closer is the
        interrupted-turn repair's job, which works from the file rather than from memory.

        The sweep IS each call's closer, so each one is marked settled: a terminal frame
        arriving after the sweep must not write a second closer.
        """
        with self._lock:
            stale = [
                (call_id, self._open_calls.pop((sid, call_id)))
                for (sid, call_id), started in list(self._open_calls.items())
                if sid == session_id and started[5] == int(turn)
            ]
            for call_id, _ in stale:
                self._settled[(session_id, call_id)] = int(turn)
                self._settled.move_to_end((session_id, call_id))
            if stale:
                self._bound_settled_locked()
        swept: list[SweptCall] = []
        for call_id, (began, name, server, call_index, step, _) in stale:
            elapsed_ms = max(0, int((self._clock() - began) * 1000))
            swept.append(SweptCall(call_id, elapsed_ms, name, server, call_index, step))
        return swept

    # -- the supersede repair's debt -------------------------------------------------

    def stand_down(self, previous_sid: str, slot: str) -> int:
        """The live turn that stands *previous_sid*'s tail repair down, or 0.

        When one is running, the debt is recorded against *previous_sid* in the SAME lock
        hold as the liveness read. A separate read followed by a second acquisition to
        record the debt leaves a window between them: a ceiling rejection running in that
        gap reads the debt while it is still empty, releases the terminal's pin, and finds
        nothing to re-queue -- then the debt is written with no consumer left, and the
        predecessor's tail stays open for the life of the file.

        The live record is read AS IT STANDS, never released first. ``closer_owed`` is set
        where a terminal is HANDED OVER, so a turn still running is indistinguishable from
        a leaked record by that field alone, and releasing on it would drop the record of a
        turn a forced reset tore down mid-flight -- the one case whose closer arrives
        later, from its own ``finally``.
        """
        with self._lock:
            running = self._live_turn_locked(previous_sid)
            if running:
                self._repair_owed[previous_sid] = slot
            return running

    def repair_debt(self, session_id: str) -> str:
        """The slot owed a repair of *session_id*'s tail, or ``""``. Reads only."""
        with self._lock:
            return self._repair_owed.get(session_id, "")

    def take_repair_debt(self, session_id: str) -> str:
        """Consume *session_id*'s repair debt: the owed slot, or ``""``."""
        with self._lock:
            return self._repair_owed.pop(session_id, "")

    # -- teardown and bounds ---------------------------------------------------------

    def forget_session(self, session_id: str) -> None:
        """Drop what a closed session leaves behind, except what a live turn still needs.

        Its attempt counts go: a later session reusing this id starts its own, and a resume
        reseeds them from the file instead. Its open calls and settled markers go only for
        turns already gone: a live turn's open calls are its own to close, and a
        mid-teardown turn still suppresses its own duplicates.
        """
        with self._lock:
            self._attempts.pop(session_id, None)
            for key in [
                k
                for k, rec in self._open_calls.items()
                if k[0] == session_id and (session_id, rec[5]) not in self._live
            ]:
                self._open_calls.pop(key, None)
            for key in [
                k
                for k, t in self._settled.items()
                if k[0] == session_id and (session_id, t) not in self._live
            ]:
                self._settled.pop(key, None)

    def trim(
        self,
        store: "OrderedDict[Any, Any]",
        limit: int,
        session_of: Callable[[Any], str],
        what: str,
    ) -> None:
        """Trim the caller's *store* to *limit*, never dropping an entry of a live turn.

        For a session-keyed cache the caller owns (an open handle, a last-written
        configuration). Every such map holds state CORRELATED TO a turn, and the rule is
        the same for all of them: an entry is released by an event of its OWN turn, never
        by pressure from other sessions. Evicting mid-turn is what turns a bounded cache
        into a correctness bug rather than a performance one.

        Call it with the store's own lock held: the trim then runs under both locks, so a
        turn starting cannot slip between the liveness read and the eviction. When every
        entry belongs to a live turn the store is allowed to exceed *limit*: each entry is
        released by its own turn's event, so the overshoot is bounded by concurrent live
        turns.
        """
        with self._lock:
            self._trim_locked(store, limit, session_of, what)

    def pop_unless_live(self, store: "dict[str, Any]", session_id: str) -> None:
        """Drop *session_id*'s entry from the caller's *store* unless it has a live turn.

        Same locking contract as :meth:`trim`.
        """
        with self._lock:
            if session_id not in self._pinned:
                store.pop(session_id, None)

    # -- internals -------------------------------------------------------------------

    def _live_turn_locked(self, session_id: str) -> int:
        return max((turn for (sid, turn) in self._live if sid == session_id), default=0)

    def _live_state_locked(self, session_id: str, turn: int) -> _LiveTurn:
        """The live record for one turn, created on first use. ``_lock`` held.

        Creating on first use covers the turn whose start was never recorded -- the flag
        turned on mid-turn -- so its tool calls still number consistently. The record is
        released by that turn's end or its session closing, exactly like one opened by a
        start.
        """
        key = (session_id, int(turn))
        state = self._live.get(key)
        if state is None:
            state = _LiveTurn()
            self._live[key] = state
            self._pinned[session_id] = self._pinned.get(session_id, 0) + 1
            # Not trimmed by pressure from other turns: every record here belongs to a turn
            # that is still running, and dropping one corrupts that turn's numbering. The
            # ceiling below sheds only records whose terminal is already queued, and
            # accepts an overage of genuinely live turns rather than evicting one -- a leak
            # alarm, not a cache policy.
            if len(self._live) > self._max_live_turns:
                self._drop_oldest_live_locked()
        return state

    def _drop_oldest_live_locked(self) -> None:
        """Shed non-live records once the ceiling is hit; never a live turn. ``_lock`` held.

        A record whose terminal event has already been handed to the writer
        (``closer_owed``) is not live: its turn is over, no further event mints into it,
        and its numbering cannot restart. Those are the records shed here, oldest first,
        so the cap reclaims the residue a leak would otherwise pile up.

        A live turn is NEVER evicted. Evicting one drops its step/call_index counters, and
        its next event re-creates a fresh record at step 0/call_index 0 -- two entries then
        claim one ordinal, which is corruption a fold reads as fact in a file that is never
        rewritten. A bounded structure over its cap is recoverable; a duplicated ordinal is
        not. So when nothing non-live is left to shed, the overage is ACCEPTED and reported
        once rather than acted on.
        """
        shed = 0
        for key in [k for k, state in self._live.items() if state.closer_owed]:
            if len(self._live) <= self._max_live_turns:
                break
            old_session, _old_turn = key
            self._live.pop(key, None)
            remaining = self._pinned.get(old_session, 0) - 1
            if remaining > 0:
                self._pinned[old_session] = remaining
            else:
                self._pinned.pop(old_session, None)
            shed += 1
        if shed:
            self._log.error(
                "session log is tracking more than %d turns in flight and shed %d "
                "closed-but-undrained record(s): turns are ending without their "
                "terminal event landing",
                self._max_live_turns,
                shed,
            )
        if len(self._live) > self._max_live_turns:
            if not self._overage_reported:
                self._overage_reported = True
                self._log.error(
                    "session log is tracking %d turns in flight, over the %d "
                    "ceiling, and every record is a turn still running -- accepting "
                    "the overage rather than evicting a live turn and restarting its "
                    "numbering",
                    len(self._live),
                    self._max_live_turns,
                )

    def _release_locked(self, session_id: str, turn: int) -> None:
        """Drop one turn's live record. ``_lock`` held.

        The turn is gone, so its settled-tool markers can go too: a later frame for one of
        its calls can only be a duplicate of a closer already written, and the turn's own
        state is what a duplicate would have keyed on.
        """
        if self._live.pop((session_id, int(turn)), None) is None:
            return
        for key in [k for k, t in self._settled.items() if k[0] == session_id and t == int(turn)]:
            self._settled.pop(key, None)
        remaining = self._pinned.get(session_id, 0) - 1
        if remaining > 0:
            self._pinned[session_id] = remaining
        else:
            self._pinned.pop(session_id, None)
        if self._overage_reported and len(self._live) <= self._max_live_turns:
            self._overage_reported = False

    def _trim_locked(
        self,
        store: "OrderedDict[Any, Any]",
        limit: int,
        session_of: Callable[[Any], str],
        what: str,
    ) -> None:
        if len(store) <= limit:
            return
        for key in list(store):
            if len(store) <= limit:
                break
            if session_of(key) not in self._pinned:
                store.pop(key, None)
        if len(store) > limit:
            self._log.debug(
                "session log %s holds %d entries, over the %d cap: every one "
                "belongs to a turn in flight",
                what,
                len(store),
                limit,
            )

    def _bound_settled_locked(self) -> None:
        """Trim the settled markers to their cap, OLDEST first. ``_lock`` held.

        The one exception to the never-evict-a-live-turn rule, and what a marker MEANS is
        why. Every other map holds state a later event of the same turn has to read back --
        a handle, a step ordinal, an open call's start time -- so evicting one mid-turn
        corrupts that turn's own record. A settle marker carries only "a closer for this id
        is already written", and the frames it suppresses come from two parsers reading the
        SAME terminal frame, so a duplicate arrives beside its original. The youngest
        markers are therefore the ones doing the work and the oldest are the ones worth
        spending. Under the shared rule the map would instead grow for the whole of any turn
        that makes more calls than the cap. The cost of a dropped marker is bounded and
        visible: at worst a second ``tool/completed`` for a call whose duplicate frame
        arrives after the cap's worth of later calls have settled.
        """
        dropped = 0
        while len(self._settled) > self._max_open_calls:
            self._settled.popitem(last=False)
            dropped += 1
        if dropped:
            self._log.debug(
                "session log dropped %d settled tool marker(s) at the %d cap: a "
                "duplicate terminal frame for one of them would write a second closer",
                dropped,
                self._max_open_calls,
            )


__all__ = [
    "MAX_LIVE_TURNS",
    "MAX_OPEN_CALLS",
    "MAX_SESSIONS",
    "SettledCall",
    "SweptCall",
    "TurnTracker",
]
