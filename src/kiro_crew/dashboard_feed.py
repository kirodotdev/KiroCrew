"""The dynamic dashboard's data channel: subscribe once, read O(fields) forever.

A dashboard needs a handful of numbers out of a crew log that may hold a hundred
thousand entries. The obvious way to get them -- fold the log when the page asks --
is the way this module exists to avoid, and the contract says so twice: never use
``crew_log_projection``, and never write a read path that refolds a whole log.

So the shape is a SUBSCRIPTION, not a read:

1. One ``bus.subscribe(FOLD_ADVANCED, ..., baseline=True)`` per
   ``(scope, key, fold)`` the instance's manifest names. The baseline hands over
   the fold's current value at subscribe time and closes the join race itself:
   the subscription is registered before the read, and events arriving during it
   are replayed through the same revision floor.
2. The latest value per fold is KEPT here. A fold that has not moved costs
   nothing at all.
3. Reading the page's fields walks the manifest and looks up a cached value per
   field, so one read is O(fields) and never O(log).
4. A new value is pushed to the frame over the existing WS exporter, so the page
   re-fills without a reload and without asking.

The folds are the ones the MANIFEST names, which is the whole reason a template
declares its sources. A dashboard binding four fields of the ``work`` fold holds
ONE subscription, because the subscription is per fold and the fields are paths
into its value.

Revisions, not seqs
-------------------
``revision`` is what orders two values of one ``(key, fold)``; ``seq`` is not, and
the bus's own documentation says why -- a slot fold's seq is the newest unit's, so
a conductor-side change on a board with any worker bound leaves it unmoved, and
adding a unit can move the value while moving the seq DOWN. So the cache drops any
event whose revision is not higher than the one it holds, and reports ``seq``
onward only because that is what a reader truncates a crew-log read against.

An unresolved field is the CREWMATE'S news, not the reader's
------------------------------------------------------------
A fold field does not resolve for one of three reasons, and :attr:`FieldRead.unresolved`
names which: its fold has produced no value yet (:data:`FOLD_UNSEEN`), the fold has a
value with nothing at the field's path (:data:`PATH_MISSING`), or the fold HAS a value
and the field resolved at an earlier read but not at this one, so the page keeps that
older value (:data:`OLDER_VALUE`). A fold with no value on this read is always
:data:`FOLD_UNSEEN`, whatever resolved before: a cold cell (a unit rollover with no
savepoint, an evicted cell, a prime still running) is not a broken binding. None of them is something the person can act on -- the page and
its bindings are the crewmate's -- so the frame draws no band for them and
``dashboard_agentic.turn_block`` tells the crewmate instead. It is deliberately not a
clock: a crewmate that did no work for ten minutes has a dashboard that is CURRENT and
unchanged.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any, Callable, Final, Iterable, Mapping

from kiro_crew.crew_log import bus as crew_log_bus
from kiro_crew.crew_log.entry_types import PANEL_FOLD_NAME
from kiro_crew.crew_log.projection import SESSION_FOLD_NAMES, SLOT_PROJECTION_NAMES
from kiro_crew.dashboard_templates.manifest import TemplateManifest

#: The fold a page's OWN written values live in. No field reads a path into it -- its
#: value IS the table of written values -- so a caller asks for it by name through
#: ``subscribe(manifest, agentic=True)`` rather than through the manifest.
AGENTIC_FOLD: Final[str] = "agentic"

#: Longest a one-shot read waits for background primes of its cold slot folds, in total.
#: Past it the fields read as missing, which is what a fold with no value reads as.
PRIME_WAIT_SECONDS: Final[float] = 30.0

logger = logging.getLogger(__name__)

#: The sentinel for a field whose value is not available.
#:
#: A DISTINCT object rather than ``None``, because ``None`` is a value a fold can
#: legitimately hold: ``timeline.first_seq`` is ``None`` for a fold with no
#: moments, and rendering that as "unavailable" would report an empty timeline as
#: a broken binding. Only this object means "the path did not resolve".


class _Missing:
    """The type of :data:`MISSING`. One instance, compared by identity."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "MISSING"


MISSING: Final[_Missing] = _Missing()

#: Why a fold field did not resolve, as :attr:`FieldRead.unresolved` names it.
#: The fold has produced no value this feed has seen.
FOLD_UNSEEN: Final[str] = "fold_unseen"
#: The fold has a value, and nothing sits at the field's path in it.
PATH_MISSING: Final[str] = "path_missing"
#: The fold HAS a value on this read and the field is not in it, but the field resolved
#: at an earlier read on this slot, so a page that drew it keeps the older value. Never
#: given for a fold with no value on this read: that is :data:`FOLD_UNSEEN`.
OLDER_VALUE: Final[str] = "older_value"

#: Which fold fields resolved at an earlier read, by slot, for :data:`OLDER_VALUE`.
#:
#: PROCESS-WIDE, because the reads that matter are not one feed's: a Dashboard tab is
#: refilled from a fresh feed per request, and the turn block reads through another, so
#: only a record shared by all of them can say a value was on screen before. Bounded by
#: slot count and forgotten on restart, which only turns an older value into one of the
#: other two reasons -- the crewmate is still told the field did not resolve.
_RESOLVED_BEFORE: "OrderedDict[str, set[str]]" = OrderedDict()
_RESOLVED_SLOTS_MAX: Final[int] = 256
#: The most field names one slot keeps. A page's fields are bounded by its manifest,
#: and a read with *declared* keeps only the names the page declares, so this
#: only matters for a caller that passes none; past it the set is replaced.
_RESOLVED_NAMES_MAX: Final[int] = 256
_RESOLVED_LOCK = threading.Lock()


def remember_resolved(
    slot: str, names: Iterable[str], declared: Iterable[str] | None = None
) -> None:
    """Record that *names* resolved on *slot*, so a later miss reads as older.

    *declared* is every fold field the page in force declares. Names outside it are
    forgotten, so a page edited onto other fields does not keep its old ones forever.
    """
    names = set(names)
    if not slot:
        return
    with _RESOLVED_LOCK:
        known = _RESOLVED_BEFORE.pop(slot, set())
        if declared is not None:
            known &= set(declared)
        known |= names
        if len(known) > _RESOLVED_NAMES_MAX:
            known = set(sorted(names)[:_RESOLVED_NAMES_MAX])
        if not known:
            return
        _RESOLVED_BEFORE[slot] = known
        while len(_RESOLVED_BEFORE) > _RESOLVED_SLOTS_MAX:
            _RESOLVED_BEFORE.popitem(last=False)


def resolved_before(slot: str) -> frozenset[str]:
    """The fold fields that resolved at some earlier read on *slot*."""
    with _RESOLVED_LOCK:
        return frozenset(_RESOLVED_BEFORE.get(slot, ()))


def forget_resolved() -> None:
    """Drop every slot's record. For a test that needs a process with no history."""
    with _RESOLVED_LOCK:
        _RESOLVED_BEFORE.clear()


def page_panel(record: Mapping[str, Any] | None) -> dict[str, Any]:
    """The reader's own panel *record* as a page sees it, or an empty one.

    The record is chosen by the caller, by the panel read route's rule. An absent
    one reads as an empty ``data`` object, never as another crew's. ``crew_key``
    is left out, because the read route never hands the digest to a client.
    """
    if not isinstance(record, Mapping):
        return {"template": "", "title": "", "data": {}, "published_at": ""}
    return {key: item for key, item in record.items() if key != "crew_key"}


def resolve_path(value: Any, path: str) -> Any:
    """Walk a dotted *path* into a folded *value*; :data:`MISSING` if it does not.

    MAPPINGS ONLY, and list indices are deliberately not supported: the manifest's
    own path pattern is dotted identifiers, so ``moments.0.seq`` is not a path a
    template can declare. A field that wants a series declares type ``array`` and
    takes the whole list, which a chart's JS then walks -- that is what "html MAY
    run JS" buys, and it keeps the host out of the business of indexing into data
    whose length it does not control.
    """
    current = value
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return MISSING
        current = current[part]
    return current


@dataclass
class _Cell:
    """One fold's latest value, and the revision that orders it."""

    value: Any = None
    revision: int = 0
    seq: int = 0
    seen: bool = False


@dataclass
class FieldRead:
    """One read of a dashboard's fields: the values, and what did not resolve."""

    fields: dict[str, Any] = dc_field(default_factory=dict)
    #: Fields whose source did not resolve. NAMED rather than counted: the page
    #: marks them, and a count could not say which cell to mark.
    missing: list[str] = dc_field(default_factory=list)
    #: The highest ``seq`` across the folds read, which is what a reader truncates
    #: a crew-log read against. Per-fold seqs genuinely differ, so this is a
    #: watermark and not a claim that every fold is at it.
    seq: int = 0
    #: The FOLD fields that did not resolve, by name, with the reason
    #: (:data:`FOLD_UNSEEN`, :data:`PATH_MISSING` or :data:`OLDER_VALUE`). A subset of
    #: ``missing``: an agentic field never written is absent, not unresolved, and has
    #: its own upkeep text. Read by the crewmate's turn block, never shown to the reader.
    unresolved: dict[str, str] = dc_field(default_factory=dict)
    #: When each agentic field was WRITTEN, by field name, from the agentic fold's own
    #: row rather than from anything the writer put in its value.
    #:
    #: A page comparing a judgment against the record needs this and cannot get it any
    #: other way: a verdict is only current if nothing has happened since it was
    #: written, and a timestamp inside the value would be the writer's claim about its
    #: own freshness -- which is exactly the claim a dead lead keeps making.
    #:
    #: Absent for a field never written, which is the same reading as its absence from
    #: ``fields``.
    written_at: dict[str, str] = dc_field(default_factory=dict)


def scope_for(fold: str) -> str:
    """Which bus scope a fold is keyed by, or ``""`` for one no scope keys.

    Read off the projection kernel's own key-kind sets rather than from a list here, so
    a fold that moves family -- or a new one -- is keyed correctly without an edit.

    ``""`` means this channel cannot subscribe to the fold, which is better than
    guessing a scope and silently never receiving an event. TWO different folds answer
    it, and a caller must not read them as the same thing:

    * A name in NO key-kind set is not a registered fold at all.
    * A TREE-keyed fold IS registered and IS bindable, and deliberately has no scope:
      it joins the logs of many slots, so its value is stale when any member's log
      grows and the bus has no key that covers that. Its value belongs to a page load
      and a refetch (:func:`~kiro_crew.crew_log.projection.read_tree_projection`),
      which the controller's gap rule already covers -- so ``subscribe`` reports a tree
      binding unavailable and
      :meth:`~kiro_crew.dashboard.handlers.member_dashboard_fields.LivePage._read_trees`
      supplies the value instead. A CALLER MUST NOT read that report as "this fold is
      unreachable": it means "not by subscription". Stated here rather than left to
      fall out of three failed membership tests, because an absence that means "by
      design" and an absence that means "unknown name" would otherwise look identical.
    """
    if fold in SLOT_PROJECTION_NAMES:
        return crew_log_bus.SCOPE_SLOT
    if fold in SESSION_FOLD_NAMES:
        return crew_log_bus.SCOPE_SESSION
    return ""


class DashboardFeed:
    """One crewmate's dashboard data: subscriptions in, cached values out.

    Thread-safe by one lock around the cache, which is the whole of the shared
    state. Bus callbacks run on the crew log's FOLD WORKER thread, synchronously
    inside ``bus.publish``, and a reader runs on whatever thread asked -- so the
    callback does the least possible work (compare a revision, store a value, hand
    a notification onward) and never I/O, never a fold, never a socket write.

    ``on_change`` is called AFTER the lock is released, with the fold name. It is
    how part 5's push happens: the caller gives a function that hands the frame to
    the loop owning the sockets. Called on the fold worker, so a caller that does
    anything but a ``call_soon_threadsafe`` is blocking the folder.
    """

    def __init__(
        self,
        slot: str,
        session_unit: str = "",
        on_change: Callable[[str], None] | None = None,
        panel_record: Mapping[str, Any] | None = None,
    ) -> None:
        #: The crewmate's DM slot, which keys every slot fold.
        self.slot = slot
        #: The reading crewmate's OWN panel record, chosen by the panel read
        #: route's rule. A ``panel`` binding resolves against this record and never
        #: against the fold's top level; ``None`` reads as "never published".
        self.panel_record = panel_record
        #: The unit id its session folds are keyed by, or ``""`` when the thread is
        #: not running. A dashboard binding only slot folds needs none, which is
        #: why this is optional rather than required: a crewmate whose DM has never
        #: run still has a work ledger and a mistake book.
        self.session_unit = session_unit
        self._on_change = on_change
        self._lock = threading.Lock()
        self._cells: dict[str, _Cell] = {}
        self._disposers: list[Callable[[], None]] = []
        self._folds: frozenset[str] = frozenset()

    # -- subscription ------------------------------------------------------- #

    def subscribe(self, manifest: TemplateManifest, agentic: bool = False) -> list[str]:
        """Subscribe to the folds *manifest* names; return the ones that could not be.

        *agentic* adds the ``agentic`` fold, which no field reads a path into: a page's
        own written values live in it, and reading them off disk per change would put
        file I/O on the fold worker's thread -- the one thread this class may not block.
        Subscribed through the same baseline, so a value written before the page opened
        is there on first read. Named rather than taken as a set of folds because the
        manifest already says which folds a page reads, and this is the single one it
        cannot say.

        REPLACES any earlier subscription set, so an instance edited onto a template
        with different sources stops receiving the folds it does not bind. Disposed
        before subscribing, so this object is never called twice for one event.

        A fold is unsubscribable for two reasons and both are reported rather than
        raised: a name in neither fold family (which the manifest's own validation
        makes unreachable, so it would be a kernel change), and a SESSION fold on a
        crewmate whose DM thread has no unit yet. The second is ordinary -- a
        crewmate that has never run a turn -- and the fields it feeds read as
        missing and unresolved rather than as an error.

        THE BASELINE READ IS FILE I/O ON THIS THREAD, which the bus documents. A
        caller on an event loop runs this through ``asyncio.to_thread``; a caller on
        the fold worker must not call it at all.
        """
        self.unsubscribe()
        unavailable: list[str] = []
        extra = frozenset({AGENTIC_FOLD}) if agentic else frozenset()
        folds = sorted(frozenset(manifest.folds) | extra)
        for fold in folds:
            scope = scope_for(fold)
            key = self.slot if scope == crew_log_bus.SCOPE_SLOT else self.session_unit
            if not scope or not key:
                unavailable.append(fold)
                continue
            with self._lock:
                self._cells.setdefault(fold, _Cell())
            try:
                self._disposers.append(
                    crew_log_bus.subscribe(
                        crew_log_bus.FOLD_ADVANCED,
                        self._make_callback(fold),
                        scope=scope,
                        key=key,
                        fold=fold,
                        # THE WHOLE POINT. Without it this object holds a
                        # subscription that is correct from now on and empty until
                        # the crewmate happens to do something -- so a dashboard
                        # opened on a quiet session would render blank cells over a
                        # log that has the numbers in it.
                        baseline=True,
                    )
                )
            except Exception:
                # The baseline read raised, so the bus already removed the
                # subscription. Reported as unavailable rather than propagated: one
                # unreadable fold must not deny the page the other five, and the
                # fields it feeds read as missing, which is the honest rendering.
                logger.warning(
                    "the %s fold could not be subscribed for slot %s",
                    fold,
                    self.slot,
                    exc_info=True,
                )
                unavailable.append(fold)
        self._folds = frozenset(folds)
        return unavailable

    def unsubscribe(self) -> None:
        """Drop every subscription and forget every cached value. Idempotent."""
        disposers, self._disposers = self._disposers, []
        for dispose in disposers:
            dispose()
        with self._lock:
            self._cells.clear()
        self._folds = frozenset()

    def wait_for_primes(self, timeout: float = PRIME_WAIT_SECONDS) -> None:
        """Wait, up to *timeout* s in total, for the slot folds that had no baseline.

        For a ONE-SHOT read (subscribe, read, unsubscribe). A slot fold with no warm
        cell hands no baseline and is folded by one background prime
        (:mod:`kiro_crew.crew_log.slot_prime`); a long-lived subscriber receives that
        value as its next event, but a one-shot reader would unsubscribe first and
        report fields that exist as missing. So it waits here for the prime, which has
        already published to this subscription when it settles. The fold still runs
        off this thread and once per cell; waiting costs no CPU.
        """
        from kiro_crew.crew_log import slot_prime

        deadline = time.monotonic() + max(timeout, 0.0)
        for fold in sorted(self._folds):
            if scope_for(fold) != crew_log_bus.SCOPE_SLOT:
                continue
            with self._lock:
                cell = self._cells.get(fold)
                seen = cell is not None and cell.seen
            if seen:
                continue
            slot_prime.wait(self.slot, fold, max(deadline - time.monotonic(), 0.0))

    def _make_callback(self, fold: str) -> Callable[[Any], None]:
        """The per-fold callback, closed over the fold NAME rather than reading it.

        The bus filters on fold already, so the event's own name adds nothing -- and
        taking the name from the closure means a malformed event cannot write into
        the wrong cell, which is the one thing a cache must not let an untrusted
        publisher do.
        """

        def _apply(event: Any) -> None:
            self._accept(fold, event)

        return _apply

    def _accept(self, fold: str, event: Any) -> None:
        """Store one fold value if it is newer. Runs on the FOLD WORKER thread.

        The event is read DEFENSIVELY, as the WS exporter reads it and for the same
        reason: a bus carries whatever a publisher sends, and a malformed one must
        cost this update rather than the fan-out to the next subscriber.
        """
        revision = int(getattr(event, "revision", 0) or 0)
        seq = int(getattr(event, "seq", 0) or 0)
        value = getattr(event, "value", None)
        if revision <= 0 or not isinstance(value, Mapping):
            return
        with self._lock:
            cell = self._cells.get(fold)
            if cell is None:
                return
            if cell.seen and revision <= cell.revision:
                # ORDERED BY REVISION, never by arrival. A baseline and a held event
                # can reach here in either order, and seq cannot order them -- see
                # the module docstring.
                return
            cell.value = value
            cell.revision = revision
            cell.seq = seq
            cell.seen = True
        # OUTSIDE THE LOCK. The callback hands the frame to the socket loop, and
        # holding this lock across that would put the reader's lock on the folder's
        # critical path for every open dashboard.
        if self._on_change is not None:
            try:
                self._on_change(fold)
            except Exception:  # pragma: no cover - a push must not cost the cache
                logger.debug("dashboard push for %s/%s failed", self.slot, fold, exc_info=True)

    # -- reading ------------------------------------------------------------ #

    def read(
        self, manifest: TemplateManifest, agentic: Mapping[str, Any] | None = None
    ) -> FieldRead:
        """The page's field values, from the cache. O(fields), never O(log).

        *agentic* is the ``agentic`` fold's rendered value, whose ``fields`` member
        holds the crewmate's own writes. It is passed in rather than read here
        because it IS one of the subscribed folds when the manifest names it -- but a
        template may declare agentic fields without binding the fold, so the caller
        supplies it from wherever it has it. An agentic field with no written value
        reads as missing, which is correct: an empty cell the crewmate has not filled
        is not a zero.

        The per-field ``type`` is NOT re-checked here. The write path checked it
        against this manifest and the fold re-checked it off disk, so a third check
        on the read path would be a third place for the rule to drift -- and would
        have nothing to do about a failure except blank a cell the agent was told was
        accepted.
        """
        read = FieldRead()
        #: Fold-sourced fields that did not resolve, and why. An agentic field the
        #: crewmate has never written is ABSENT rather than unresolved: it goes in
        #: `missing` only, and its upkeep text already tells the crewmate to write it.
        unresolved = read.unresolved
        resolved_fold: list[str] = []
        written = agentic.get("fields") if isinstance(agentic, Mapping) else None
        with self._lock:
            cells = {name: (cell.value, cell.seq, cell.seen) for name, cell in self._cells.items()}
        for name, spec in manifest.fields.items():
            if spec.agentic:
                row = written.get(name) if isinstance(written, Mapping) else None
                if isinstance(row, Mapping) and "value" in row:
                    read.fields[name] = row["value"]
                    # The fold's own stamp for this cell. Taken only when the value was
                    # taken, so the two cannot disagree about which write they describe.
                    at = row.get("at")
                    if isinstance(at, str) and at:
                        read.written_at[name] = at
                else:
                    read.missing.append(name)
                continue
            fold = spec.fold or ""
            cached = cells.get(fold)
            if fold == PANEL_FOLD_NAME:
                # The record comes from the caller, so it resolves even when the
                # fold has nothing: a publish lives in its file as well as the log.
                resolved = resolve_path(page_panel(self.panel_record), spec.path or "")
                if resolved is MISSING:
                    read.missing.append(name)
                    # NEVER PUBLISHED is the panel's "no value yet": the empty record
                    # has none of a published record's paths, and that is not a bad
                    # binding.
                    published = isinstance(self.panel_record, Mapping) and bool(
                        self.panel_record.get("published_at")
                    )
                    unresolved[name] = PATH_MISSING if published else FOLD_UNSEEN
                    continue
                read.fields[name] = resolved
                resolved_fold.append(name)
                if cached is not None and cached[2]:
                    read.seq = max(read.seq, cached[1])
                continue
            if cached is None or not cached[2]:
                read.missing.append(name)
                unresolved[name] = FOLD_UNSEEN
                continue
            value, seq, _seen = cached
            resolved = resolve_path(value, spec.path or "")
            if resolved is MISSING:
                read.missing.append(name)
                unresolved[name] = PATH_MISSING
                continue
            read.fields[name] = resolved
            resolved_fold.append(name)
            read.seq = max(read.seq, seq)
        # A path miss in a fold that HAS a value, on a field that resolved before, is an
        # OLDER value on a page that drew it. A fold with no value on this read stays
        # unseen: the cell may only be cold, and that is not the crewmate's to fix.
        before = resolved_before(self.slot)
        for name, reason in unresolved.items():
            if reason == PATH_MISSING and name in before:
                unresolved[name] = OLDER_VALUE
        remember_resolved(
            self.slot,
            resolved_fold,
            declared=[n for n, spec in manifest.fields.items() if not spec.agentic],
        )
        return read

    def folds(self) -> frozenset[str]:
        """The folds this feed is subscribed to. For a test and a log line."""
        return self._folds

    def fold_value(self, fold: str) -> Mapping[str, Any]:
        """One subscribed fold's latest cached value, or an empty mapping.

        The read :meth:`read` cannot do for a caller: it resolves a PATH into a fold,
        and the ``agentic`` fold is consumed whole -- its ``fields`` member is the table
        of written values, keyed by field name. Empty for a fold this feed does not hold
        and for one that has not been seen, which read the same way: no value yet.
        """
        with self._lock:
            cell = self._cells.get(fold)
            if cell is None or not cell.seen or not isinstance(cell.value, Mapping):
                return {}
            return cell.value
