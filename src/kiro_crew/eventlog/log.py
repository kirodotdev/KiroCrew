"""One member's append-only log, stored as a ``member``-kind crew log.

This module is an ADAPTER, not a log implementation. The bytes, the locking, the
durability and the repair all belong to :mod:`kiro_crew.crew_log.store`, which
already owns them for the ``crew`` and ``session`` kinds::

    <data home>/crew-log/members/<store name>/log.jsonl

A member's log is a third KIND there rather than a second mechanism beside it.
That is the whole point of this file being thin: the ``crew-log`` root is masked
from the sandbox and fenced against the agent file tools (``security/paths.py``,
``sandbox.py``), named at the root so every kind inherits it. A per-member log
stored anywhere else would need its own fence entry, and the next log added would
miss it the same way -- an append-only record the agent can rewrite is not an
append-only record, and that property has to hold by WHERE THE FILE LIVES rather
than by someone remembering to list it.

One translation lives here and nowhere else, so nothing above this layer changes:

**Contributed event types.** A crew log keeps exactly one guest TYPE namespace,
``app:<name>/<action>``, and grants it to the ``member`` kind. The contribution
protocol spells the same thing ``<app>/<action>``. The stored form takes the
``app:`` prefix so the log's own ownership rule decides the write, and the read
gives the protocol spelling back, so an app's declared ``contributions.events``
and every frame carrying them are untouched.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kiro_crew.crew_log.checkpoint import (
    PrefixWitness,
    prefix_admit,
    prefix_unchanged,
    prefix_witness,
)
from kiro_crew.crew_log.errors import (
    CODE_ALREADY_EXISTS,
    CODE_ALREADY_OWNED,
    CODE_BAD_DATA,
    CODE_NO_LEDGER,
    CrewLogError,
)
from kiro_crew.crew_log.schema import APP_SOURCE_PREFIX, KIND_MEMBER
from kiro_crew.crew_log.store import CrewLog, crew_log_path, segment_first_seqs
from kiro_crew.eventlog.types import (
    Event,
    is_contributed_event_type,
    is_known_event_type,
)

if TYPE_CHECKING:
    from kiro_crew.projection import Admit

#: The fixed emitter for every built-in event. These facts are observed BY the
#: gateway about the member, never written by the member itself, which is why the
#: ``member`` kind takes no ``crew:`` guest at all.
_BUILTIN_SRC = "gateway"

#: How long an append waits out another PROCESS's write lease before giving up,
#: and how the wait is spaced. Short because the holder is short: the lease is
#: released within the append that took it, so the wait is for one append to
#: finish, never for a process to exit. Bounded because the caller above is a
#: best-effort hook on a serving path -- a wedged peer must cost this event, not
#: the queue behind it.
APPEND_CONTENTION_SECONDS = 2.0
APPEND_CONTENTION_FIRST_DELAY = 0.01
APPEND_CONTENTION_MAX_DELAY = 0.2

#: How many of the NEWEST events one instance keeps in memory. A member log has no
#: compaction and no rotation, so its length is a function of how long the member
#: has existed, and a cold roster read loads one per member -- so what is retained
#: has to be bounded independently of the file. Sized for the reads served off the
#: tail (a history page, the newest seq) rather than for the whole history, which
#: the store pages and streams instead.
MAX_RETAINED_EVENTS = 5000


class LogCorrupt(Exception):
    """A committed region of a member log is unreadable.

    Kept as this module's own exception because its callers catch it by name. It
    now wraps the refusal the crew log store raises rather than detecting
    corruption here: a gap or an unparseable committed line is that layer's
    judgement to make, and making it twice is how two answers drift apart.
    """

    def __init__(self, path: Path, line_no: int, detail: str) -> None:
        self.path = path
        self.line_no = line_no
        super().__init__(f"{path}: line {line_no}: {detail}")


#: Cumulative ceiling on ONE unit's log. The per-append size check bounds a single
#: event and the per-app daily quota bounds a day's appends, but a quota renews, so
#: neither bounds the total a contributor can accumulate in one log. This surface
#: folds the whole ledger into memory on a cold load, so the total is the figure
#: that decides what that fold costs.
MAX_UNIT_LOG_BYTES = 64 * 1024 * 1024

#: Headroom inside that ceiling which only the gateway's OWN events may use.
#:
#: A member's log has two kinds of writer and one ceiling. Without a reserve, an
#: authorized contributor that fills the log to the cap does not merely stop
#: contributing -- it stops the gateway from recording that member's activity and
#: config changes at all, permanently, because those appends meet the same ceiling.
#: A contributor is refused this much earlier so the gateway's own record of the
#: member cannot be crowded out by an app the user installed.
GATEWAY_RESERVE_BYTES = 8 * 1024 * 1024


class UnitLogFull(Exception):
    """One unit's log has reached its cumulative ceiling.

    Its own type rather than a ValueError: a caller has to tell "this append is
    malformed" from "this unit has no room left", because only the second is
    answered by pruning or archiving rather than by fixing the request.
    """

    def __init__(self, path: Path, size: int, limit: int) -> None:
        self.path = path
        self.size = size
        self.limit = limit
        super().__init__(f"{path}: {size} bytes reaches the {limit}-byte ceiling for one unit")


def _stored_type(type_: str) -> str:
    """The spelling the crew log stores for *type_*.

    A contributed ``<app>/<action>`` becomes ``app:<app>/<action>``; a built-in
    type is already a domain the ``member`` kind owns and is returned unchanged.
    """
    if is_contributed_event_type(type_):
        return APP_SOURCE_PREFIX + type_
    return type_


def _wire_type(stored: str) -> str:
    """The protocol spelling for a *stored* type -- the inverse of :func:`_stored_type`."""
    if stored.startswith(APP_SOURCE_PREFIX):
        return stored[len(APP_SOURCE_PREFIX) :]
    return stored


def _src_for(stored_type: str) -> str:
    """The emitter to record for *stored_type*.

    A guest type carries its app's identity in the type itself, and the crew log
    requires the two to agree -- an ``app:<name>`` emitter may write only under its
    own ``app:<name>/`` prefix -- so the emitter is DERIVED here instead of being
    passed in. A caller that could name a different app than the type it is writing
    would be a caller that can attribute an entry to somebody else.
    """
    if stored_type.startswith(APP_SOURCE_PREFIX):
        domain = stored_type.split("/", 1)[0]
        return domain
    return _BUILTIN_SRC


def _as_event(entry: Any) -> Event:
    """One crew log entry as this surface's :class:`Event`."""
    return {
        "type": _wire_type(entry.type),
        "seq": entry.seq,
        "time": entry.time,
        "data": entry.data,
    }


class MemberLog:
    """Append-only log for one member, backed by a ``member``-kind crew log."""

    def __init__(self, slug: str) -> None:
        self.slug = str(slug)
        self.header: dict | None = None
        self.events: list[Event] = []
        #: The oldest seq :attr:`events` still holds, or 0 when it holds every event
        #: the log has. Non-zero means the tail is a WINDOW, so a reader wanting
        #: anything below it must go to the store rather than to this list.
        self.retained_from = 0
        self._crew_log: CrewLog | None = None
        self._loaded = False
        #: The (size, mtime_ns) the loaded events were read at, or None when
        #: nothing is loaded. Compared by :meth:`refresh_if_changed`.
        self._loaded_stat: tuple[int, int] | None = None

    @property
    def path(self) -> Path:
        """The file the crew log store keeps this member's entries in."""
        return crew_log_path(KIND_MEMBER, self.slug)

    @property
    def committed_bytes(self) -> int:
        """Bytes durably committed, i.e. the size of the stored log."""
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    # ---- lifecycle --------------------------------------------------------
    def create(self, name: str) -> None:
        """Materialise the log with its header, or do nothing if it exists.

        Atomicity, the private directory mode and the directory fsync are the
        store's, which does all three for every kind. ``name`` is written as the
        header's optional display name so a cold reader has one before it has
        folded anything; a later rename arrives as a ``member/config`` fact that a
        fold applies over it.
        """
        if CrewLog.exists(KIND_MEMBER, self.slug):
            return
        try:
            CrewLog.create(KIND_MEMBER, self.slug, name=name)
        except CrewLogError as exc:
            # Another writer won the race between the check and the create. The
            # store's own refusal is the authority on that, and the log it refused
            # to overwrite is the one we wanted, so this is a no-op and not a fault.
            if getattr(exc, "code", "") != CODE_ALREADY_EXISTS:
                raise
        self._loaded = False

    def load(self) -> None:
        self._loaded = False
        self._ensure_loaded()

    def _stat(self) -> tuple[int, int] | None:
        """``(size, mtime_ns)`` of the log file, or None when it is absent."""
        try:
            st = self.path.stat()
        except OSError:
            return None
        return (st.st_size, st.st_mtime_ns)

    def refresh_if_changed(self) -> bool:
        """Reload when the file changed under us; answer whether it did.

        This process is not the log's only writer -- ``kirocrew-core`` runs as its
        own stdio subprocess and records member activity through the same service
        -- so a held instance can be arbitrarily behind the file. Reads went
        through :meth:`_ensure_loaded`, which returns the cached events as soon as
        ``_loaded`` is set, so another process's commits stayed invisible until a
        local append or a restart.

        The stat is the cheap part: an unchanged file costs one ``stat`` and no
        parse, which matters because a roster read asks this once per member. A
        changed file is reloaded whole, because the store owns the parse and a
        partial tail read would have to duplicate its framing rules.
        """
        if not self._loaded:
            return False
        if self._stat() == self._loaded_stat:
            return False
        self.load()
        return True

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        # Sampled BEFORE the read and compared against a fresh sample after it.
        # Stamping a stat taken only AFTER the parse records the state of a file
        # this instance has not fully read: the log has two ordinary writers, so a
        # commit landing between the iterator's EOF and that sample gets its size
        # and mtime stamped onto the events from before it, and
        # `refresh_if_changed` then finds no change and keeps them -- stale for the
        # life of the process, until a local append or a restart. Comparing the two
        # samples turns that into a miss that heals on the very next read, because
        # the stamp is withheld (left None, exactly the state a fresh instance has)
        # whenever the file moved under the read.
        pre_stat = self._stat()
        try:
            crew_log = CrewLog.open(KIND_MEMBER, self.slug)
        except CrewLogError as exc:
            # Absent is an answer, not a fault: the callers above treat "no log for
            # this slug" as an empty read. Any OTHER refusal is the store judging
            # the committed region unreadable, which is precisely LogCorrupt.
            if getattr(exc, "code", "") == CODE_NO_LEDGER:
                self._crew_log = None
                self.header = None
                self.events = []
                # Cleared with the events: a reload that finds the log gone must not
                # leave a window floor from the previous load standing, or every
                # later read would be sent to a store handle that is now None.
                self.retained_from = 0
                self._loaded = True
                # A log CREATED between the refused open and this point is the same
                # hazard as an append during a read: stamping the new file's state
                # against zero events would hide it. Withheld when they disagree.
                self._loaded_stat = pre_stat if self._stat() == pre_stat else None
                return
            raise LogCorrupt(self.path, 0, str(exc)) from exc
        self._crew_log = crew_log
        self.header = crew_log.header.to_dict()
        try:
            # A BOUNDED tail, not the whole history. A member log has no compaction
            # and no rotation, so its length grows for the member's life, and a cold
            # roster read asks every member for one -- so retaining the complete
            # history makes a single read's cost a function of how long the crew has
            # existed. The deque drops the oldest as it fills, so the peak is the cap
            # rather than the file: bounded ON APPEND, not by slicing a list that was
            # already built. The readers that need more than this window go to the
            # store: `history` pages through it, and `iter_events` streams it.
            kept: deque[Event] = deque(maxlen=MAX_RETAINED_EVENTS)
            seen = 0
            for entry in crew_log.iter_from(1):
                kept.append(_as_event(entry))
                seen += 1
        except CrewLogError as exc:
            raise LogCorrupt(self.path, 0, str(exc)) from exc
        self.events = list(kept)
        # The oldest seq this instance still holds, or 0 when it holds everything.
        # Consulted rather than inferred from the list's length: a log whose length
        # happens to equal the cap is NOT truncated, and treating it as truncated
        # would send every read to the store for no reason.
        self.retained_from = self.events[0]["seq"] if seen > MAX_RETAINED_EVENTS else 0
        self._loaded = True
        # Assigned only on a SUCCESSFUL parse -- a raised parse leaves the previous
        # stamp alone rather than recording a state the events do not match -- and
        # assigned the PRE-read sample, so the stamp describes the file this
        # instance actually read. Withheld when the samples disagree, per the note
        # at the top of this method.
        self._loaded_stat = pre_stat if self._stat() == pre_stat else None

    # ---- write ------------------------------------------------------------
    def append(self, type: str, data: dict) -> Event:
        """Append one event and return it.

        The type check stays here because it is this surface's vocabulary: the
        crew log owns the four domains but not the built-in action names, so a
        typo'd built-in would otherwise be written as a fact nothing folds.
        """
        if not is_known_event_type(type):
            raise ValueError(f"unknown event type {type!r}")
        self._ensure_loaded()
        if self._crew_log is None:
            raise LogCorrupt(self.path, 0, "cannot append to a log with no header")
        # A cumulative ceiling per unit, on top of the per-append size check and
        # the per-app daily quota. Neither of those bounds the TOTAL: a quota is
        # renewable, so a contributor appending within it every day grows one log
        # without limit, and this surface folds the whole ledger into memory on
        # every cold load. The ceiling is what makes that fold's cost finite.
        #
        # A CONTRIBUTED append meets it earlier, by the gateway's reserve. One log
        # has two writers and one ceiling, so a contributor allowed all the way to
        # the cap would silently stop the gateway from recording that member's own
        # activity and config -- a user's record of their crew member lost to an app
        # they installed. The reserve is refused to the contributor and kept for the
        # writer that cannot be asked to prune.
        ceiling = MAX_UNIT_LOG_BYTES
        if is_contributed_event_type(type):
            ceiling -= GATEWAY_RESERVE_BYTES
        if self.committed_bytes > ceiling:
            raise UnitLogFull(self.path, self.committed_bytes, ceiling)
        stored = _stored_type(type)
        entry = self._append_through_contention(stored, data)
        event = _as_event(entry)
        # Refresh from disk rather than appending to the cached list: the store
        # re-reads under its own lock on every write, so another PROCESS appending
        # between our last load and this one is already committed ahead of us. A
        # cache that only grew by our own entry would hold a hole at those seqs
        # and answer reads from it.
        #
        # This reload is where an append's cost sits, and it cannot be moved by
        # dropping it: the check above needs the header, so an invalidate-only
        # version simply makes the NEXT append's load pay it. Measured on this class
        # at 3.0 ms into an empty log against 52.0 ms at 8,000 events, of which the
        # parse alone is 50.1 ms, and identical either way. Making an append cheap
        # needs the header read separated from the event parse, which is a change to
        # what this class promises rather than to where it reloads -- tracked
        # separately rather than folded in here.
        self._loaded = False
        self._ensure_loaded()
        return event

    # ---- read -------------------------------------------------------------
    def _append_through_contention(self, stored: str, data: dict):
        """Append, waiting out a CONTENTION refusal instead of losing the event.

        ``crew_log.lease`` takes write ownership non-blocking, so two processes
        appending to one member at the same instant do not serialize behind the
        per-append lock: the second is refused ``already_owned`` and writes nothing.
        The member log has two ordinary writers -- the gateway, and ``kirocrew-core``
        recording activity from its own process, which
        :meth:`refresh_if_changed` already names on the read side -- so the
        collision is routine and the lost event is a transition missing from history
        for good.

        The lease module tells a refused caller to report the loss rather than retry
        it, on the stated ground that it will not own the log by asking again in a
        moment. That ground holds for a LONG-LIVED holder such as the session
        emitter's cached handle. It does not hold here, and that is measured rather
        than assumed: this class releases within the append that took it, because the
        reload below replaces the handle the claim was bound to, and a test asserts
        this process holds no lease after ensure, after two appends or after a read.
        A holder that momentary is precisely one worth waiting for, so waiting is
        what this does -- for a bounded time, and only for that one code.

        Retrying is safe because the store refuses BEFORE it writes: its own append
        contract says a rejected append leaves the file identical, and the lease
        says a refused caller has written nothing. So no attempt can double-write.
        Exhausting the budget re-raises, which leaves the callers' existing
        reporting in place rather than swallowing the loss quietly.
        """
        assert self._crew_log is not None
        deadline = time.monotonic() + APPEND_CONTENTION_SECONDS
        delay = APPEND_CONTENTION_FIRST_DELAY
        while True:
            try:
                return self._crew_log.append(stored, data, src=_src_for(stored))
            except CrewLogError as exc:
                code = getattr(exc, "code", "")
                # A refused PAYLOAD keeps this surface's ValueError, the same type
                # the unknown-type check above raises: a caller telling a client's
                # bad append apart from a server fault reads the exception type, and
                # splitting one bad-input answer across two types is how the caller
                # starts reporting half of them as a fault. The store still makes
                # the judgement -- this only carries its verdict in the shape
                # callers already handle.
                if code == CODE_BAD_DATA:
                    raise ValueError(str(exc)) from exc
                if code != CODE_ALREADY_OWNED or time.monotonic() >= deadline:
                    raise
                time.sleep(delay)
                # Backed off rather than spun: a tight retry loop against a lock
                # held by another process burns a core to no purpose. Capped so a
                # long wait still makes several attempts.
                delay = min(delay * 2, APPEND_CONTENTION_MAX_DELAY)

    def events_after(self, after: int, limit: int) -> list[Event]:
        """Oldest-first page of events with ``seq > after``, at most *limit*.

        The catch-up read of the contribution protocol: a consumer that folded up
        to ``after`` asks for what came next, in the order it must fold it.
        Distinct from :meth:`history`, which pages BACKWARDS for a timeline view
        -- folding a newest-first page would apply a later event before an
        earlier one.

        Reads from the STORE whenever the in-memory tail has a floor, the same way
        :meth:`history` and :meth:`iter_events` do. The tail keeps only the newest
        ``MAX_RETAINED_EVENTS``, so answering a cold cursor from it would return
        the newest events and silently omit every durable one below the floor --
        and a fold is exactly the reader that cannot survive a gap, because it
        would apply later state over earlier state it never saw. An incomplete
        answer here is worse than a slower one.
        """
        self._ensure_loaded()
        if self.retained_from:
            if self._crew_log is None:
                return []
            start = after + 1 if after >= 1 else 1
            out: list[Event] = []
            for entry in self._crew_log.iter_from(start):
                if entry.seq <= after:
                    continue
                out.append(_as_event(entry))
                if limit is not None and limit >= 0 and len(out) >= limit:
                    break
            return out
        out = [e for e in self.events if e["seq"] > after]
        if limit is not None and limit >= 0:
            return out[:limit]
        return out

    def history(self, before: int | None, limit: int | None) -> list[Event]:
        """Newest-first page of events with ``seq < before`` (or all).

        Answered from the retained tail only while that tail holds the whole
        window. Once the log has outgrown ``MAX_RETAINED_EVENTS`` the tail is a
        window, not the history, so a page reaching below it is read from the store
        -- whose own ``page`` is bounded by ``limit``, so the cost is the page's
        size rather than the log's length.

        A ``limit`` of None still means every event, and that is the caller's bound
        to set, not this one's: the only caller passing it reads one member's whole
        history deliberately. Streamed rather than served from the tail so the
        answer stays correct, since the tail would silently drop the oldest events.
        """
        self._ensure_loaded()
        if self._crew_log is None:
            return []
        if self.retained_from:
            if limit is not None and limit >= 0:
                page = self._crew_log.page(before, limit)
                return [_as_event(e) for e in page.entries]
            streamed = [
                _as_event(e)
                for e in self._crew_log.iter_from(1)
                if before is None or e.seq < before
            ]
            return list(reversed(streamed))
        evs = self.events
        if before is not None:
            evs = [e for e in evs if e["seq"] < before]
        newest_first = list(reversed(evs))
        if limit is not None and limit >= 0:
            return newest_first[:limit]
        return newest_first

    def iter_events(self) -> Iterator[Event]:
        """Every event, oldest first, WITHOUT retaining them.

        What a fold wants, and what a whole-list read cannot offer: a fold
        reduces, so it needs to see each event once and never needs the list. The
        retained tail cannot serve a fold either, because a fold that started above
        the tail's floor would silently skip the events below it.
        """
        self._ensure_loaded()
        if self._crew_log is None:
            return iter(())
        return (_as_event(entry) for entry in self._crew_log.iter_from(1))

    def last_seq(self) -> int:
        """The newest event's seq, or 0 for a log with no events.

        Read off the last EVENT rather than counted from the list length: a
        damaged committed line is skipped on load (that is the store's rule, so a
        reader loses that line and not the file), and a counted cursor would then
        sit one below the real newest seq and hand a subscriber a catch-up
        position that re-delivers an event it already folded.

        0 is below the first real seq, which a crew log numbers 1, so it reads as
        "nothing yet" wherever a cursor is compared.
        """
        self._ensure_loaded()
        return self.events[-1]["seq"] if self.events else 0

    def exists(self) -> bool:
        return CrewLog.exists(KIND_MEMBER, self.slug)

    def checkpoint_identity(self) -> dict[str, Any] | None:
        """The facts that say WHICH log a savepoint belongs to, or None when absent.

        Two facts, and they are the same two the crew log's own savepoints compare,
        read through that module's own public helpers so two spellings of "is this
        the same log" cannot drift apart -- the one that said yes too often would
        fold a retired file's state onto a live one's bytes:

        * ``origin`` -- the file's creation identity. A member removed and recreated
          under the same slug restarts its seqs, so once the new log has grown past a
          stored watermark a seq check ALONE would pass, and the fold would resume
          state derived from an unrelated log.
        * ``first_seq`` -- the oldest surviving segment's first seq. Retention deletes
          whole segments off the front, so a cold fold folds a window while a
          savepoint still counts entries the file does not hold. The two answers
          differ, and the savepoint's is the one no reader can reproduce.

        Returned as a plain mapping because the projection kernel stores it VERBATIM
        and never interprets a key: adding a third fact here retires this log's
        existing savepoints and needs no change in the kernel.

        ``None`` means "do not take the shortcut" -- no log, or an identity the store
        could not answer for. A caller folds from the start, which costs more and is
        never wrong.
        """
        self._ensure_loaded()
        handle = self._crew_log
        if handle is None:
            return None
        # Function-local: crew_log.projection imports the store and the session
        # ledger, so a module-level import here would widen this module's import
        # graph for one accessor -- the same reason that module keeps its own
        # checkpoint import local.
        from kiro_crew.crew_log.projection import log_origin

        origin = log_origin(handle)
        if origin is None:
            return None
        firsts = segment_first_seqs(KIND_MEMBER, self.slug)
        if not firsts:
            return None
        return {"origin": origin, "first_seq": firsts[0]}

    def checkpoint_admit(self, first_seq: int) -> Admit | None:
        """The live-log condition a savepoint of this log must satisfy, or None.

        :meth:`checkpoint_identity` covers what is fixed once the fold is done, and
        equality is all it can do. This covers the one fact equality cannot hold: the
        bytes a savepoint's state was folded from are still the bytes in the file.
        :meth:`last_seq` records why this log needs it -- a damaged committed line is
        skipped on load, so a reader loses that line and not the file. A cold fold
        then omits what that line contributed while a savepoint written before the
        damage keeps it, and because a resumed fold never revisits the region below
        its watermark, the two reads disagree for the life of the member rather than
        for one load. A savepoint may LAG; it may not hold a value no later read
        reproduces.

        The predicate is the crew log's own, not a second copy: it reads the digest
        helpers that live on ``CrewLog`` precisely so one mechanism serves both
        clients, and two spellings of this question would drift.

        *first_seq* is the value :meth:`checkpoint_identity` reported, which bounds how
        few raw records a prefix can hold.

        ``None`` means "do not take the shortcut", the same answer and for the same
        reason as an absent identity.
        """
        self._ensure_loaded()
        handle = self._crew_log
        if handle is None:
            return None
        return prefix_admit(handle, first_seq)

    def checkpoint_witness(self, seq: int) -> PrefixWitness | None:
        """The digest of this log's raw records through *seq*, or None.

        Read this BEFORE the fold that consumes the file, and confirm it with
        :meth:`checkpoint_prefix_unchanged` after the pass. A digest read only
        afterwards can certify bytes the pass never saw: a consumed record that
        changed in between is hashed together with state folded from its earlier
        value, and every later resume recomputes the digest from those same changed
        bytes, so the comparison passes and the state is served for the life of the
        member while disagreeing with a cold fold.

        ``None`` when *seq* is not a boundary this file resolves, which costs a
        savepoint rather than recording one nothing can check.
        """
        self._ensure_loaded()
        handle = self._crew_log
        if handle is None:
            return None
        return prefix_witness(handle, seq)

    def checkpoint_prefix_unchanged(self, witness: PrefixWitness) -> bool:
        """Whether the records *witness* covers still hash to what it recorded.

        Decode-free, and growth above the boundary is not a change: the walk stops at
        the record count the witness names. This is what a stat cannot answer -- a
        size and an mtime say the file moved, never whether the bytes already
        consumed are the same bytes.
        """
        self._ensure_loaded()
        handle = self._crew_log
        if handle is None:
            return False
        return prefix_unchanged(handle, witness)
