"""A slot's folds on the projection kernel, kept warm: the stream concatenated over a
slot's units, the per-cell memo with its locks and byte ceiling, and the resume from and
write of each cell's savepoint.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Final, NamedTuple

from kiro_crew.config.paths import data_home
from kiro_crew.crew_log.projection_budget import (
    SLOT_FOLD_CACHE_BYTES_ENV,
    slot_fold_cache_bytes,
    slot_fold_cell_bytes,
)
from kiro_crew.crew_log.projection_catalogue import KNOWN_TYPES, _Fold, logger
from kiro_crew.crew_log.projection_kernel import Checkpoint, require_name
from kiro_crew.crew_log.projection_registry import _FOLDS
from kiro_crew.crew_log.projection_session import _HeldCheckpoints
from kiro_crew.crew_log.projection_session_log import _log_identity, open_session_log
from kiro_crew.crew_log.schema import Entry
from kiro_crew.crew_log.store import CrewLog, ReadCursor, ResumePoint, log_exception_text
from kiro_crew.projection import ProjectionRegistry

if TYPE_CHECKING:
    # Type-only: the savepoint module imports the fold surface, so a runtime
    # import here would close the cycle its function-local imports avoid.
    from kiro_crew.crew_log.checkpoint import CutWitness, PrefixWitness


class _Ordinal(NamedTuple):
    """One entry of a slot's concatenated stream, at a seq that grows across units.

    A crew log's ``seq`` restarts at 1 in every unit file, while the kernel holds ONE
    watermark per cell and drops an event at or below it. So the units' own seqs cannot
    be handed over as they are: the second unit's entries all sit at or below the
    first's and would be dropped as a re-fold -- the collision :func:`advance` names,
    arriving for a legitimate reason. ``ordinal`` is what the kernel orders by instead:
    the entry's own seq plus the heights of the units already streamed, which grows
    across the whole concatenation.

    Two properties come out of that, and both are needed. It never goes backwards, so
    no entry is wrongly dropped. And one entry always maps to the SAME ordinal, so
    re-driving a range costs the overlap and nothing else -- which a plain running
    counter would lose, because a re-read entry would take a new number and be counted
    a second time.

    ``entry`` is the crew log entry untouched, so the fold sees exactly the record the
    file holds.
    """

    ordinal: int
    entry: Entry


def _ordinal_seq(event: _Ordinal) -> int:
    """The kernel's ordering number for one wrapped entry."""
    return event.ordinal


@dataclass(frozen=True)
class _UnitMark:
    """One unit's log identity, height and byte fingerprint, as the file reports them.

    ``origin`` is :func:`log_origin`, the same value the session folds and the on-disk
    savepoints compare with; ``None`` is an unknown identity and never matches, so a
    unit whose log cannot be read folds cold. It is in the reuse test because a seq
    alone cannot tell a log that GREW from one REMOVED AND RECREATED under the same id
    whose seq has already climbed back to or past the remembered one.

    ``size`` and ``mtime_ns`` are the stat-only fingerprint :func:`_log_identity`
    already computes for the session fold, over exactly the segment set a walk would
    read. They are here because identity and height together still describe a log only
    by how FAR it goes, never by what it says: an already-folded entry rewritten in
    place keeps its seq, so a mark without them compares equal to a log whose bytes
    have changed underneath a cell, and the stale cell is then served to
    :func:`rebuild_from_projection`, which writes it back over the record. A changed
    fingerprint folds cold instead. Both come from the same call that yields
    ``origin``, so carrying them costs no extra stat.

    The fingerprint settles the units a continuation does not re-read. It cannot settle
    the newest one, which a continuation exists to let GROW: an append moves size and
    mtime by itself, so a stat has nothing left to compare there.

    ``cuts`` is what settles that one. It is :meth:`CrewLog.cuts`, the number of times
    the store has cut this unit's history back, and a cut is the only thing that can
    change a record a fold already consumed -- so the same count twice means those
    records are the same bytes, read off a file a few bytes long rather than off the
    log. ``None`` is NOT PROVEN rather than zero, and a mark carrying it falls back to
    :class:`_PrefixSeen`, the decode-free digest of the records already folded, which is
    what a log created before the counter existed is settled by.
    """

    origin: str | None
    last_seq: int
    size: int | None = None
    mtime_ns: int | None = None
    cuts: int | None = None


def _unit_mark(unit_id: str) -> _UnitMark:
    """*unit_id*'s log identity, newest seq, fingerprint and cut count.

    ``origin`` is ``None`` for an unreadable or header-less log, and every caller
    treats that as unknown and folds cold -- which is also why the fingerprint needs no
    separate unknown test: :func:`_log_identity` returns an identity only when the same
    stat calls that produced the fingerprint succeeded.
    """
    try:
        handle = open_session_log(unit_id)
        if handle is None:
            return _UnitMark(None, 0)
        origin, size, mtime_ns = _log_identity(handle)
        # ``last_seq`` on a freshly opened handle is read off the file's tail, which
        # is what makes it usable as a growth signal for a reader that never appends.
        return _UnitMark(
            origin,
            int(getattr(handle, "last_seq", 0) or 0),
            size,
            mtime_ns,
            handle.cuts(),
        )
    except Exception:
        return _UnitMark(None, 0)


def _unit_marks(unit_ids: Sequence[str]) -> "tuple[_UnitMark, ...]":
    """Every unit's mark, in the order they are folded."""
    return tuple(_unit_mark(unit_id) for unit_id in unit_ids)


#: Stands for "every record in the file" when a digest wants no boundary.
#: :meth:`CrewLog.raw_prefix_digest` stops at end of file and reports the count it
#: reached, so a boundary above any real log yields the whole-file digest.
_ALL_RECORDS = 1 << 62


class _PrefixSeen(NamedTuple):
    """A digest of the newest unit's raw records, read at one known moment.

    ``records`` is a RAW record count, not an entry span: a blank or unparseable
    interior line is a record to the digest walk while the fold skips it, so the two
    numbers differ and only the walk's own count can bound it.
    """

    records: int
    sha: str


def _unit_prefix(unit_id: str, records: int = _ALL_RECORDS) -> "_PrefixSeen | None":
    """*unit_id*'s raw digest through *records*, or the whole file; ``None`` if unread.

    Decode-free: :meth:`CrewLog.raw_prefix_digest` frames and hashes raw records
    without parsing any of them, which is what makes this affordable on a read path.
    A short walk reports the count it reached, and the caller compares counts.
    """
    try:
        handle = open_session_log(unit_id)
        if handle is None:
            return None
        sha, hashed = handle.raw_prefix_digest(records)
        return _PrefixSeen(records=hashed, sha=sha)
    except Exception:
        return None


def _prefix_holds(unit_id: str, seen: _PrefixSeen) -> bool:
    """Whether *unit_id*'s first ``seen.records`` raw records still hash to *seen*.

    Growth above them is not a change, because the walk stops at the count *seen*
    names. This is the question a stat cannot answer: a size and an mtime say a file
    moved, never whether the bytes a fold already consumed are the same bytes.
    """
    again = _unit_prefix(unit_id, seen.records)
    return again is not None and again.records == seen.records and again.sha == seen.sha


#: What :func:`_folded_prefix_holds` concluded, and how.
#:
#: ``UNCUT`` and ``CUT`` are decided by the cut counter alone; ``UNPROVEN`` is the
#: counter declining to answer, and the digest decides instead. Three values rather
#: than a bool because the costs differ: two of them are a few bytes read, and the
#: third is a byte walk of the whole folded prefix.
_UNCUT = "uncut"
_CUT = "cut"
_UNPROVEN = "unproven"


def _cut_verdict(now: "int | None", held: "int | None") -> str:
    """Whether the cut counter proves this unit's folded records are still intact.

    The store raises the counter ONCE BEFORE each cut and once after it, and
    :meth:`CrewLog.cuts` answers ``None`` for the odd value in between. So a reading
    that comes back at all was taken with the bytes settled, and two equal such readings
    bracket a window in which no cut COMPLETED -- which is the question, because a cut
    is the only thing in this store that can change a record already on disk. Equal
    therefore means the folded records are the same bytes, and that is the whole proof.

    One raise would not do it. It brackets "a cut STARTED since you last looked", and a
    reader that sampled the raised value, folded the records that cut was about to
    remove, and sampled the same value again afterwards would compare equal across the
    rewrite.

    Unequal means a cut landed, which is not a slow case to re-check but a decided one:
    the records a fold consumed may be gone. Either reading being ``None`` proves nothing
    in either direction -- an absent, unreadable or mid-cut counter is not a count.
    """
    if now is None or held is None:
        return _UNPROVEN
    return _UNCUT if now == held else _CUT


def _folded_prefix_holds(unit_id: str, memo: "_SlotMemo", mark: _UnitMark) -> bool:
    """Whether the records *memo* already folded from *unit_id* are still those bytes.

    The cheap proof first. A unit whose cut counter reads the same as it did when the
    memo was built has not been cut since, and nothing else in the store can disturb a
    committed record -- so the question is settled by a few bytes, and the warm read
    costs the same on a log of ten entries and a log of a hundred thousand.

    The digest is the fallback, not the rule. It walks and hashes every record the fold
    consumed, so its price rises with the log: the records it re-reads are exactly the
    ones the reader has paid for once already. It is here for the readings the counter
    declines to make -- a log created before the counter existed carries none until its
    first cut, and a log whose cut is in flight right now has nothing settled to report
    -- and for those the digest is the only evidence there is.
    """
    verdict = _cut_verdict(mark.cuts, memo.marks[-1].cuts if memo.marks else None)
    if verdict == _UNCUT:
        return True
    if verdict == _CUT:
        return False
    return memo.prefix is not None and _prefix_holds(unit_id, memo.prefix)


class _SlotFold:
    """One SLOT-keyed crew-log fold as a :mod:`kiro_crew.projection` unit.

    :class:`_SessionFold` with the two differences a concatenation of units brings.
    The event arriving is an :class:`_Ordinal`, so ``apply`` unwraps before the fold
    sees it -- the kernel orders by the wrapper's number and the fold reads the entry.
    And the starting state is BOUND to the slot for a fold that declares ``bind_slot``,
    so a slot-keyed fold knows which board it answers for before the first entry
    instead of guessing it from whichever entry comes first.

    The same-reference rule is kept the same way: an entry the fold cannot be moved by
    returns the state untouched, and any other entry is stepped onto a copy.
    """

    def __init__(self, fold: _Fold, slot: str) -> None:
        self._fold = fold
        self._slot = slot
        self.key = fold.name
        self.state_version = fold.state_version

    def init(self) -> dict[str, Any]:
        state = self._fold.start()
        if self._slot and self._fold.bind_slot is not None:
            self._fold.bind_slot(state, self._slot)
        return state

    def apply(self, state: dict[str, Any], event: _Ordinal) -> dict[str, Any]:
        entry = event.entry
        if not self._fold.touched_by(entry):
            return state
        grown = self._fold.copied(state)
        self._fold.step(grown, entry)
        return grown

    def view(self, state: dict[str, Any]) -> dict[str, Any]:
        return self._fold.render(state)


class _SlotStream:
    """A slot's units as one stream the kernel can order, oldest unit first.

    Each unit's base is the total height of the units ALREADY STREAMED, counted as
    this pass actually saw them rather than taken from a sampled height -- so an
    append landing in an earlier unit while this runs still cannot push one of its
    entries onto an ordinal a later unit has already used. A unit with no log
    contributes nothing and is skipped, the same as the cold fold: a slot whose
    oldest unit was collected by retention still folds the ones it has.

    Iterating is a GENERATOR, so the kernel folds one entry at a time and a cold
    fold of a long slot holds no more than that.

    Two numbers the stream cannot return and its caller needs afterwards.
    :attr:`heights` is the last seq FOLDED from each unit that had a log, which is
    what lets the memo record what this pass really consumed instead of what was
    sampled before it. :attr:`reached` is that height for the newest such unit, the
    only figure a later read of the same slot can compare against and the one
    :func:`fold_slot_checkpoint` has always answered with; it starts at *since*, so a
    warm tail that turned out to be empty reports the position it resumed from rather
    than dropping to zero.

    *resume* is a :class:`~kiro_crew.crew_log.store.ResumePoint` from an earlier pass,
    and it turns the walk of ONE unit's log into a read of its tail: without it,
    reaching ``since + 1`` means decoding every record below it, so a poll that finds
    one new entry decodes the whole log. It names a byte offset in one file, so it is
    applied only when this stream reads exactly one unit -- the warm tail's shape --
    and a stream over several units ignores it rather than aiming a position at the
    wrong log.

    :attr:`points` is the third thing the caller needs afterwards: per unit, the
    resume point the walk reached, for the next pass to hand back. A unit that
    yielded nothing leaves no entry in it at all, which is how the caller tells
    "nothing arrived, keep the point you have" from "this is the new point".
    """

    def __init__(
        self,
        unit_ids: Sequence[str],
        *,
        base: int = 0,
        since: int = 0,
        resume: "ResumePoint | None" = None,
    ) -> None:
        self._unit_ids = tuple(unit_ids)
        self._base = base
        self._since = since
        self._resume = resume if len(self._unit_ids) == 1 else None
        self.reached = since
        self.heights: dict[str, int] = {}
        self.points: "dict[str, ResumePoint]" = {}

    def __iter__(self) -> "Iterator[_Ordinal]":
        base = self._base
        for unit_id in self._unit_ids:
            handle = open_session_log(unit_id)
            if handle is None:
                continue
            top = base
            reached = self._since
            # One cursor PER UNIT. A shared one would end the pass holding whichever
            # unit yielded last, and a later unit that yielded nothing would leave an
            # earlier unit's offset standing as this unit's position.
            cursor = ReadCursor()
            for entry in handle.iter_from(
                self._since + 1,
                known=KNOWN_TYPES,
                resume=self._resume,
                cursor=cursor,
            ):
                ordinal = base + entry.seq
                top = max(top, ordinal)
                reached = max(reached, entry.seq)
                yield _Ordinal(ordinal, entry)
            base = top
            self.heights[unit_id] = reached
            self.reached = reached
            if cursor.at is not None:
                self.points[unit_id] = cursor.at


@dataclass
class _SlotMemo:
    """One slot fold kept warm: the kernel cell holding it, and what it folded over.

    The registry is per (slot, fold), not one shared instance, and that is a
    correctness requirement rather than a preference: ``prime`` folds and RESETS every
    definition registered in it, so a shared registry would make a cold refold of one
    fold discard another's warm cell -- and the three slot folds are read over
    different unit lists, so they go stale independently.

    ``marks`` is the per-unit watermark vector the reuse test compares, and the bases
    the ordinals are built from are derived from it: unit *i*'s base is the total
    height of the units before it. That derivation is what makes a warm continuation
    land on the same ordinals the earlier pass used, and it holds because a memo is
    only kept when every earlier unit's mark is unchanged.

    A memo is never edited once stored. A read that carries one forward stores a NEW
    memo over the SAME registry, so two reads racing on one slot cannot leave a pair of
    fields half updated: the registry's cell is the single truth and holds its own lock,
    and re-driving a range it has already folded is dropped on its watermark rather
    than counted twice.
    """

    registry: ProjectionRegistry
    store: str
    units: "tuple[str, ...]"
    marks: "tuple[_UnitMark, ...]"
    reached: int
    #: The NEWEST unit's raw digest as of the pass that built this cell, and the one
    #: thing a continuation must check that a mark cannot. Every earlier unit is held
    #: to whole-mark equality, so a rewrite there already folds cold; the newest unit is
    #: admitted precisely because it GREW, and growth moves its size and mtime by
    #: itself, which leaves a stat with nothing to say about the bytes underneath.
    #: ``None`` means no digest is vouched for, which has two causes now. The unit's cut
    #: counter settles the question on its own, so no digest was taken -- the ordinary
    #: case. Or one was wanted and could not be had: the file moved during the pass, or
    #: could not be read. The two are told apart by the mark's own ``cuts``, and a
    #: continuation with neither is refused rather than trusted.
    prefix: "_PrefixSeen | None" = None
    #: The kernel ORDINAL this cell is persisted through on disk
    #: (:func:`kiro_crew.crew_log.checkpoint.save_slot`), or 0 when nothing is. What
    #: :func:`_persist_slot_fold` measures a write against, carried on the cell so a
    #: continuation decides without reading the file to find out.
    saved: int = 0
    #: Where in the NEWEST unit's log this cell's fold stopped, as a byte offset the
    #: next read can open the file at. Without it a continuation still decodes every
    #: record below the seq it resumes from, so an idle board's poll costs the whole
    #: log however little arrived -- the second linear term in a warm read, beside the
    #: digest the cut counter replaced. ``None`` means no position is vouched for and
    #: the next read walks from the top: a pass that yielded nothing new, a unit list
    #: that changed, or a point the store itself declined to issue.
    resume: "ResumePoint | None" = None
    #: What ORDERS this cell against every other value of the same (slot, fold). Minted
    #: when the cell is built, so a cell CARRIED FORWARD unchanged keeps its number and a
    #: cell that folded anything new gets a higher one. See :func:`_next_slot_revision`.
    revision: int = 0
    #: What this cell is charged against the ceiling, counted ONCE when it is built
    #: (:func:`slot_fold_cell_bytes`). Stored rather than recomputed because the eviction
    #: loop sums every held cell, and re-counting there would make one store cost a walk
    #: of every warm cell's items.
    weight: int = 0


_slot_memos: "dict[tuple[str, str, str], _SlotMemo]" = {}
_slot_memo_guard = threading.Lock()

#: The last revision this process handed out, for any (slot, fold). Guarded by
#: :data:`_slot_memo_guard`.
#:
#: ONE COUNTER FOR EVERY KEY rather than one per key, which costs a shared number and
#: buys the property the push needs with nothing to evict: a per-key counter would have
#: to live somewhere, and the only table it could live in is the memo table -- which is
#: evicted, so an evicted key's next cell would restart at 1 and a client holding the
#: earlier number would discard every value after it. A process-wide counter is
#: strictly increasing for every key at once, so it is monotonic per key as a
#: consequence rather than as a thing to maintain.
#:
#: NOT comparable ACROSS PROCESSES, and nothing asks it to be: a frame carries the
#: revision of the process that folded it, and a client reconnecting to another gateway
#: re-reads rather than continues. In process, a restart folds cold and starts at 1
#: again, which a client that reconnected has already discarded its own state for.
_slot_revision = 0


def _next_slot_revision() -> int:
    """The next revision, strictly above every one already handed out.

    Caller holds :data:`_slot_memo_guard`. Never derived from a seq, a time or a file:
    every one of those can repeat or step backwards across a unit rollover, a unit
    recreated under the same id, or a rewritten prefix -- the three cases
    :func:`fold_slot_warm` names -- and the whole purpose of this number is to order two
    values that those cases leave looking identical.
    """
    global _slot_revision
    _slot_revision += 1
    return _slot_revision


@dataclass
class _FoldLock:
    """One key's fold lock, beside the number of passes holding or waiting for it.

    The count is what makes the table droppable: an entry at zero is one no pass can be
    inside, so replacing it with a fresh lock serializes exactly what it needs to.
    """

    lock: threading.Lock
    holders: int = 0


#: One lock per (data home, slot, fold), held across a whole pass of
#: :func:`fold_slot_warm`. Created under :data:`_slot_memo_guard`.
#:
#: WHY A PASS AND NOT A DICT ACCESS. Two warm passes on one key SHARE the cell: the
#: continuation drives ``memo.registry``, and :func:`_slot_checkpoint` then reads that
#: live cell and pairs it with the caller's OWN ``reached``. So a pass that read a
#: shorter tail could return the other pass's newer state labelled with its own older
#: seq -- and ``seq`` is what a reader truncates against, so the value is newer than the
#: number that describes it. The kernel's watermark repairs the CELL on a later read; it
#: cannot repair a value already returned.
#:
#: The pairing matters here because two readers on one slot is the NORMAL mode: the
#: append-driven wake folds the same board a dashboard poll is reading, which is what eager
#: folding is for. A lock is therefore the mechanism rather than a documented caveat.
#:
#: PER KEY, not one lock, because two slots have nothing to share. And held across the
#: pass rather than around the drive alone: the state and the seq are read at different
#: moments and it is their PAIRING that must be atomic. A caller that waits here waits
#: for a fold it would otherwise have duplicated, and finds the cell warm when it
#: arrives, so the lock costs no wall time it was not already paying.
#:
#: WHAT BOUNDS THIS TABLE. One entry per key with a pass holding or waiting for it, so
#: its size is the folds running right now rather than the slots this process has ever
#: folded. :func:`_release_fold_lock` drops an entry when its last holder leaves, which is
#: the only moment at which dropping one is safe: a lock handed out twice as two different
#: objects serializes nothing, so an entry with a waiter has to stay.
_slot_fold_locks: "dict[tuple[str, str, str], _FoldLock]" = {}


def _acquire_fold_lock(key: "tuple[str, str, str]") -> "_FoldLock":
    """Claim one (data home, slot, fold)'s lock entry, creating it on first use.

    Claiming is counting a holder, not taking the lock: the caller takes ``entry.lock``
    itself, outside :data:`_slot_memo_guard`, because a pass holds it while folding and
    the guard is taken inside that pass. Every claim owes a :func:`_release_fold_lock`.
    """
    with _slot_memo_guard:
        entry = _slot_fold_locks.get(key)
        if entry is None:
            entry = _FoldLock(lock=threading.Lock())
            _slot_fold_locks[key] = entry
        entry.holders += 1
        return entry


def _release_fold_lock(key: "tuple[str, str, str]", entry: "_FoldLock") -> None:
    """Give up a claim, and drop the entry once nobody holds or waits for it.

    The identity check keeps a release from deleting an entry some other pass created for
    the same key after this one was dropped.
    """
    with _slot_memo_guard:
        entry.holders -= 1
        if entry.holders <= 0 and _slot_fold_locks.get(key) is entry:
            del _slot_fold_locks[key]


def forget_slot_folds(slot: str = "", name: str = "") -> None:
    """Drop warm slot folds, so the next read folds cold.

    Everything when called with no argument; one slot's, or one fold of one slot's,
    when named. The memo is an optimization with no answer of its own, so dropping it
    costs time and never correctness -- which is what makes this safe as the read
    path's own response to doubt, and what a test isolating one slot's log from
    another's needs.
    """
    home = str(data_home())
    # The locks (``_slot_fold_locks``) are not touched here: an entry exists only while a
    # pass holds or waits for it, and dropping one out from under that pass would hand the
    # next caller a different lock object and reopen the race it exists to close.
    with _slot_memo_guard:
        if not slot and not name:
            _slot_memos.clear()
            _oversize_slot_cells.clear()
            return
        for table in (_slot_memos, _oversize_slot_cells):
            for key in [
                held
                for held in table
                if held[0] == home
                and (not slot or held[1] == slot)
                and (not name or held[2] == name)
            ]:
                del table[key]


def _slot_memo_bytes() -> int:
    """What every warm cell held right now is charged, together. Caller holds the guard.

    Read off each memo's own stored charge rather than recomputed. Re-counting would walk
    every held cell's items on every store, which turns one store into O(all cells) work.
    """
    return sum(memo.weight for memo in _slot_memos.values())


def _remember_slot_fold(key: "tuple[str, str, str]", memo: _SlotMemo) -> None:
    """Keep *memo* under *key*, capped by BYTES; least recently STORED goes first.

    BY BYTES RATHER THAN BY COUNT, which is a reversal of an earlier decision and worth
    stating as one. The count ceiling's case was that each cell is a bounded record, so
    the thing needing a limit is how many are retained -- and that holds exactly as long
    as the cells are comparable. They are not. A ``panel`` cell is a few hundred KiB and a
    ``radar`` cell at its declared caps is 2.4 GiB, so one ceiling of 64 cells priced a
    resident total across four orders of magnitude with no number in the code moving.
    Eager folding is what makes the high end reachable rather than theoretical: the worker
    stores a cell for every board this process WRITES, so which 64 cells are held stopped
    being a function of what a reader asked for.

    So the ceiling is a byte figure (:func:`slot_fold_cache_bytes`) and each cell is
    charged what it RETAINS (:func:`slot_fold_cell_bytes`). Small cells keep many slots
    warm and a huge one keeps few, which is the behaviour a count could not express.

    The earlier decision's second objection stands and is accepted: below the worst case
    the eviction order does mean "whoever has the biggest board loses". That is now the
    POINT -- one 2 GiB cell should lose to four hundred small ones -- rather than a side
    effect, because what is being bounded is resident bytes and not cell count.

    A CELL THAT CANNOT FIT THE WHOLE CEILING IS NOT STORED. ``radar`` at its caps is
    charged more than any sane ceiling, and the two alternatives are both worse: parking
    it would hold 2.4 GiB resident until the next store, and evicting everything else
    first would empty the table to make room for one cell that still does not fit. So it
    is refused, the read still answers correctly, and that slot folds cold next time --
    which costs time and never an answer. The first refusal of a cell is logged at
    WARNING and later ones at DEBUG; the operator's answer is a higher ceiling.

    Eviction order is least recently stored or advanced, which is not the same as least
    recently read: a read that finds the cell already at the file's position returns it
    without storing anything, so it does not move the cell towards the back. A cell can
    therefore be read often while sitting at the front, be evicted, and fold cold on its
    next read. That is the cost of not taking the guard on a read that had nothing to
    record, and it is the cheaper side of the trade.

    Insertion order tracks stores because a cell is never edited in place -- a read or an
    eager fold that carries one forward stores a NEW memo, which moves it to the end.
    """
    ceiling = slot_fold_cache_bytes()
    if memo.weight > ceiling:
        with _slot_memo_guard:
            # Dropped rather than left standing: an older cell of this key describes
            # bytes this pass has already folded past, so serving it later would be a
            # stale answer, where no cell is a cold fold.
            _slot_memos.pop(key, None)
            first = key not in _oversize_slot_cells
            _oversize_slot_cells[key] = memo.weight
            _oversize_slot_cells.move_to_end(key)
            while len(_oversize_slot_cells) > OVERSIZE_SLOT_CELL_LIMIT:
                _oversize_slot_cells.popitem(last=False)
        # WARNING once per cell, where an operator looks: from here on every read of this
        # board folds cold, and the answer is a higher ceiling.
        logger.log(
            logging.WARNING if first else logging.DEBUG,
            "crew log slot fold %s/%s retains %d charged bytes, above the %d-byte "
            "ceiling (%s), so it is not kept warm; every read of it folds cold",
            key[1],
            key[2],
            memo.weight,
            ceiling,
            SLOT_FOLD_CACHE_BYTES_ENV,
        )
        return
    with _slot_memo_guard:
        _slot_memos.pop(key, None)
        _oversize_slot_cells.pop(key, None)
        _slot_memos[key] = memo
        held = _slot_memo_bytes()
        while held > ceiling and len(_slot_memos) > 1:
            oldest, evicted = next(iter(_slot_memos.items()))
            _slot_memos.pop(oldest)
            held -= evicted.weight


#: Cells the last pass REFUSED to keep because their charge was above the whole ceiling,
#: with that charge. The eager worker reads this (:func:`slot_fold_over_ceiling`) and does
#: not advance such a cell on a wake: a pass it cannot store is a cold fold of every unit
#: the slot ran under, paid for a value nobody kept, once per wake. The read path is not
#: affected -- it folds the cell when asked, which is the lazy behaviour -- and a pass that
#: CAN store the cell (a raised ceiling, a state that shrank) clears the entry.
#:
#: BOUNDED, least recently refused first, at :data:`OVERSIZE_SLOT_CELL_LIMIT`. An entry
#: evicted here costs one wasted fold: the next wake for that cell tries it, is refused
#: again, and records it again.
_oversize_slot_cells: "OrderedDict[tuple[str, str, str], int]" = OrderedDict()

#: Most refused cells remembered at once. A refused cell is one at a fold's caps, so even a
#: large fleet holds few; past this the oldest is forgotten and costs one retry.
OVERSIZE_SLOT_CELL_LIMIT: Final[int] = 256


def slot_fold_over_ceiling(slot: str, name: str) -> bool:
    """Whether *slot*'s *name* cell was last refused as too big for the CURRENT ceiling.

    Compared against the ceiling now, not the one in force when the cell was refused, so
    an operator raising ``KIROCREW_SLOT_FOLD_CACHE_BYTES`` lets the next wake advance it.
    """
    key = (str(data_home()), slot, name)
    with _slot_memo_guard:
        weight = _oversize_slot_cells.get(key)
    return weight is not None and weight > slot_fold_cache_bytes()


def _minted_revision() -> int:
    """One fresh revision, taking the guard. For a cell about to be built."""
    with _slot_memo_guard:
        return _next_slot_revision()


def _continuable(marks: "tuple[_UnitMark, ...]", held: "tuple[_UnitMark, ...]") -> bool:
    """Whether *marks* is *held* with the newest unit grown, and nothing else moved.

    The one shape a warm cell can be carried over, because its state was folded
    through every earlier unit already. Everything else describes different bytes and
    folds cold: a changed unit list, a unit whose identity changed or is unknown, an
    EARLIER unit that grew (a forced reset tears a session down while a turn is still
    appending through the handle it holds, so an earlier unit is not closed to writes),
    an earlier unit whose bytes changed without its seq moving, and any unit whose seq
    went backwards.

    "Nothing else moved" is decided by whole-mark equality, so it covers each earlier
    unit's stat fingerprint as well as its identity and height -- that is what makes an
    in-place rewrite of an already-folded entry a cold fold rather than an equal
    comparison. The newest unit is compared on identity and growth alone, because this
    function exists to admit exactly its growth and an append moves its fingerprint by
    itself.
    """
    return (
        bool(marks)
        and len(marks) == len(held)
        and all(mark.origin is not None for mark in marks)
        and marks[:-1] == held[:-1]
        and marks[-1].origin == held[-1].origin
        and marks[-1].last_seq > held[-1].last_seq
    )


def _note_slot_fold_origin(origin: str) -> None:
    """Stamp where this slot pass got its starting state, for the metrics side.

    Function-local import and swallowed failure, the policy
    :mod:`kiro_crew.crew_log.eager` already follows for this same package: a
    telemetry package that will not load costs a fold its metric and nothing
    else, and ``kiro_crew.metrics`` stays out of this family's module-scope
    imports so the projection facade need not re-export it.

    STAMPS, never records: the eager fold worker turns the mark into a point
    after the pass, OUTSIDE the per-cell lock this runs under -- a first
    ``get_recorder()`` builds the telemetry provider synchronously, and doing
    that while holding this lock would stall a reader waiting on the same cell.
    No ``exc_info``: this module's frames bind open ``CrewLog`` handles.
    """
    try:
        from kiro_crew.metrics.crew_log_health import SCOPE_SLOT, note_fold_origin

        note_fold_origin(SCOPE_SLOT, origin)
    except Exception:  # pragma: no cover - a broken install loses the metric only
        logger.debug("crew log fold origin metric unavailable")


def _note_slot_fold_idle() -> None:
    """Stamp that this slot pass advanced nothing. See :func:`_note_slot_fold_origin`."""
    try:
        from kiro_crew.metrics.crew_log_health import SCOPE_SLOT, note_fold_idle

        note_fold_idle(SCOPE_SLOT)
    except Exception:  # pragma: no cover - a broken install loses the metric only
        logger.debug("crew log fold idle metric unavailable")


def fold_slot_warm(name: str, unit_ids: Sequence[str], *, slot: str) -> Checkpoint:
    """*name* folded over *slot*'s units, CONTINUED from where the last read left it.

    :func:`fold_slot_checkpoint`'s answer, reached incrementally. Folding is O(the
    log), not O(the record): the reader walks every line of every unit to find the few
    that move this fold, and a slot's logs carry its message bodies. The readers that
    pay that are loops that wake on a timer, so the fold is kept in memory per slot and
    advanced over the entries that arrived since -- through the projection kernel,
    whose watermark drops an entry already folded, so the resumed answer and the
    from-scratch answer come out of one implementation.

    A warm read reads ONE file: the newest unit, from its remembered position. A cold
    read streams every unit. Both go through the kernel, so there is no second folding
    path that could disagree with the first about the same bytes.

    A continuation also PROVES that one file's already-folded records are still those
    bytes before trusting them (:func:`_folded_prefix_holds`). The store rewrites a
    committed prefix on two recovery paths -- an unreachable chunk group is truncated
    away and closers are appended with seqs continuing from the cut, and a failed append
    is truncated back after its bytes reached the disk -- and a reader holds no append
    lock while either runs. Both leave a file that has grown since the last read and
    reuses seqs the fold already consumed, which is a pure append to every stat and to
    every seq comparison.

    The proof is a COUNT, not a hash: the store raises the unit's cut counter before
    each of those truncations, so two equal readings of a few bytes bracket a window in
    which no committed record changed. That is what keeps a warm read the same price on
    a log of ten entries and a log of a hundred thousand. Hashing the already-folded
    records proves the same thing by re-walking exactly the bytes the reader has paid
    for once, which charges the whole folded prefix per read; it is the fallback for a
    log with no counter and nothing else.

    THE INVARIANT: the heights sampled before a pass reads are its READ PLAN, never the
    cell's claim about itself. What a memo remembers is what the pass actually FOLDED
    (:func:`_folded_marks`). Two things follow. An append landing mid-read is folded in
    and the next read starts above it, so it is neither dropped nor counted twice. And a
    slot that has not moved compares equal and is answered from the cell -- where a memo
    holding the sample would sit permanently one entry short of itself and send every
    later read back to the file for a tail it has already folded.

    PERSISTED, so a second process and a cell that was evicted resume instead of
    folding cold. The savepoint lives beside the newest unit's log and is admitted on
    exactly the shapes :func:`_continuable` admits -- see
    :mod:`kiro_crew.crew_log.checkpoint`'s slot section, which owns the file and every
    condition that retires one. It is an OPTIMIZATION with no answer of its own: a
    missing, stale or unreadable savepoint folds cold, which reaches the same value at
    more cost. What must never happen is a WRONG answer, so every condition
    :func:`_continuable` does not admit refolds from empty rather than carrying state
    that describes other bytes.
    """
    return fold_slot_warm_revised(name, unit_ids, slot=slot)[0]


def fold_slot_warm_revised(
    name: str, unit_ids: Sequence[str], *, slot: str
) -> "tuple[Checkpoint, int]":
    """:func:`fold_slot_warm`, and the REVISION of the cell it answered from.

    Two returns rather than a second lookup, because the pairing has to be atomic. A
    caller that read the checkpoint and then asked the memo table for its revision could
    be handed a number minted by a LATER pass -- and a frame carrying a newer revision
    beside an older value makes the client discard the newer value when it arrives, which
    is the one failure the revision exists to prevent. So the pass that folded reports
    both, inside its own lock.

    :func:`fold_slot_warm` stays the one-value call every existing caller makes: a
    savepoint, a writer continuing a checkpoint and a test have no use for a revision,
    and giving them a tuple to unpack would spread a push's contract over callers that
    do not push.
    """
    require_name(name)
    key = (str(data_home()), slot, name)
    # The whole pass, under this key's own lock. Everything below reads or advances one
    # shared cell and then pairs that cell's state with this pass's own seq, and it is
    # the PAIRING that a second pass would break -- see ``_slot_fold_locks``.
    entry = _acquire_fold_lock(key)
    try:
        with entry.lock:
            return _fold_slot_warm_locked(name, key, unit_ids, slot=slot)
    finally:
        _release_fold_lock(key, entry)


def _fold_slot_warm_locked(
    name: str, key: "tuple[str, str, str]", unit_ids: Sequence[str], *, slot: str
) -> "tuple[Checkpoint, int]":
    """:func:`fold_slot_warm`'s body, with this key's lock already held.

    Split out so the lock's extent is one ``with`` statement rather than an indentation
    a later edit could fall out of: every return below is inside it by construction.
    """
    with _slot_memo_guard:
        memo = _slot_memos.get(key)
    marks = _unit_marks(unit_ids)
    units = tuple(unit_ids)
    known = all(mark.origin is not None for mark in marks)
    if memo is not None and memo.units == units:
        if known and memo.marks == marks:
            # NOTHING FOLDED, so the cell keeps the revision it was built with. Minting
            # one here would move the number on every poll of an idle board, and a client
            # would re-seed from a frame that carries the value it already holds.
            #
            # Counted as a WARM start all the same: the pass started and the cell
            # answered it, which is what the origin reports. ``fold.apply`` is
            # what separates a pass that moved a value from one that did not --
            # which is why this branch, the ONLY one that advances nothing, is also
            # the only one that marks the pass idle.
            _note_slot_fold_origin("warm")
            _note_slot_fold_idle()
            return _slot_checkpoint(name, memo)
        if _continuable(marks, memo.marks) and _folded_prefix_holds(units[-1], memo, marks[-1]):
            _note_slot_fold_origin("warm")
            before = _digest_to_carry(units, marks)
            tail = _SlotStream(
                units[-1:],
                base=sum(mark.last_seq for mark in marks[:-1]),
                since=memo.reached,
                resume=_resume_from(memo),
            )
            for event in tail:
                memo.registry.drive(memo.store, event)
            grown = _SlotMemo(
                registry=memo.registry,
                store=memo.store,
                units=units,
                marks=_folded_marks(units, marks, tail, carried=memo.marks),
                reached=tail.reached,
                prefix=_vouched(units[-1], before),
                saved=memo.saved,
                resume=tail.points.get(units[-1], memo.resume),
                revision=_minted_revision(),
                weight=_cell_weight(name, memo.registry, memo.store),
            )
            grown = _persist_slot_fold(name, grown, slot=slot, sampled=marks[-1])
            _remember_slot_fold(key, grown)
            return _slot_checkpoint(name, grown)
    # Nothing warm to carry forward. Ask the DISK before folding every unit: a savepoint
    # beside the newest unit's log reaches the same value over the entries appended since
    # it was written, where the cold fold below streams every unit from its first record.
    if known and units:
        resumed = _resume_slot_fold(name, units, marks, slot=slot)
        if resumed is not None:
            _note_slot_fold_origin("savepoint")
            resumed = _persist_slot_fold(name, resumed, slot=slot, sampled=marks[-1])
            _remember_slot_fold(key, resumed)
            return _slot_checkpoint(name, resumed)
    # Nothing warm and no savepoint: every unit of the slot is streamed from its
    # first record. The counter exists to make that share visible.
    _note_slot_fold_origin("cold")
    cold = _SlotStream(units)
    registry = ProjectionRegistry(seq_of=_ordinal_seq)
    registry.register(_SlotFold(_FOLDS[name], slot))
    before = _digest_to_carry(units, marks)
    registry.prime(slot, cold)
    fresh = _SlotMemo(
        registry=registry,
        store=slot,
        units=units,
        marks=_folded_marks(units, marks, cold),
        reached=cold.reached,
        prefix=_vouched(units[-1], before) if units else None,
        # Nothing on disk is standing behind this value: a cold fold is what happens when
        # no savepoint could be used, so the next write is owed for the whole position.
        saved=0,
        resume=cold.points.get(units[-1]) if units else None,
        # A cold fold is where the three rollover cases land -- a new unit, a unit
        # recreated under the same id, a rewritten prefix -- and every one of them can
        # leave ``reached`` unmoved or lower. The mint is what makes the value after them
        # orderable at all.
        revision=_minted_revision(),
        weight=_cell_weight(name, registry, slot),
    )
    if known:
        fresh = _persist_slot_fold(name, fresh, slot=slot, sampled=marks[-1] if units else None)
        _remember_slot_fold(key, fresh)
    return _slot_checkpoint(name, fresh)


def _slot_cell(name: str, memo: _SlotMemo) -> "tuple[Any, int] | None":
    """*memo*'s kernel cell for *name* as ``(state, watermark)``, or ``None``.

    The watermark is the ORDINAL the cell stands at, which is the number a savepoint
    resumes it at. It is read off the live cell rather than derived from ``reached``,
    because the cell is what ``prime_checkpointed`` installs and a second derivation
    could disagree with it about the same pass.
    """
    try:
        return memo.registry.cells(memo.store)[name]
    except Exception:  # pragma: no cover - a cell the registry did not build
        return None


def _identity_marks(marks: "Sequence[_UnitMark]") -> "list[list[Any]]":
    """*marks* as the JSON a slot savepoint's identity block compares verbatim.

    EVERY field of the mark, including ``cuts``, because the block's job is to be
    admitted on exactly the shapes :func:`_continuable` admits and that function compares
    an earlier unit's mark whole. An earlier unit that was cut therefore retires the
    savepoint here as it folds cold there, rather than the two disagreeing about the same
    unit vector.
    """
    return [[mark.origin, mark.last_seq, mark.size, mark.mtime_ns, mark.cuts] for mark in marks]


def _resume_slot_fold(
    name: str, units: "tuple[str, ...]", marks: "tuple[_UnitMark, ...]", *, slot: str
) -> "_SlotMemo | None":
    """A warm cell for *slot*'s *name*, resumed from the DISK savepoint, or ``None``.

    The same three steps a warm continuation takes, standing on state a previous PROCESS
    folded instead of on a cell in this one: the savepoint is admitted only if it
    describes this unit vector and the newest unit's prefix still hashes to what its
    writer read, the tail above it is folded through the kernel, and the result is a cell
    exactly like the one a continuation builds.

    The tail goes through ``prime_checkpointed`` rather than a seeding path of its own, so
    a resumed fold and a cold fold run one implementation over the same events -- the
    property that makes the resumed answer and the cold answer the same answer.

    ``None`` for no savepoint and for every refusal, which is the cold fold.
    """
    from kiro_crew.crew_log import checkpoint as savepoints

    handle = open_session_log(units[-1])
    if handle is None:
        return None
    # Unit *i*'s ordinal base is the total height of the units before it, which is the
    # derivation the warm continuation uses and the one the savepoint's own position is
    # checked against. It is reproducible here only because every earlier mark is in the
    # identity block and compared verbatim.
    base = sum(mark.last_seq for mark in marks[:-1])
    saved = savepoints.load_slot(
        handle,
        name,
        slot=slot,
        units=units,
        marks=_identity_marks(marks[:-1]),
        ordinal_base=base,
    )
    if saved is None:
        return None
    # The newest unit's own evidence, read before this pass consumes it -- the same thing
    # the cold and warm paths read, and for the same reason. On a unit with a cut counter
    # that is the counter, already in ``marks[-1]``, and nothing is hashed. On one without
    # it is a FULL digest, and the savepoint's own witness cannot stand in for that
    # digest: the witness ends at the seq the savepoint was written at, and this pass is
    # about to fold the TAIL above it, so a cell carrying it would vouch for bytes it did
    # not read. Two things go wrong when it does, and both are silent. A continuation then
    # asks :func:`_prefix_holds` about the saved boundary only, so a tail rewritten in
    # place -- the store's own recovery truncates an unreachable chunk group and
    # re-appends with the seqs continuing from the cut -- compares equal, and the cell
    # keeps state folded from bytes the file has replaced. And :func:`_persist_slot_fold`
    # would write a witness whose ``seq`` is above its own ``prefix_records``, which
    # :func:`prefix_admit` refuses on the next read, so every other restart pays a cold
    # fold.
    before = _digest_to_carry(units, marks)
    registry = ProjectionRegistry(seq_of=_ordinal_seq)
    registry.register(_SlotFold(_FOLDS[name], slot))
    tail = _SlotStream(units[-1:], base=base, since=saved.reached)
    try:
        registry.prime_checkpointed(
            slot,
            _HeldCheckpoints(
                {name: Checkpoint(name=name, last_seq=saved.watermark, state=saved.state)}
            ),
            {},
            lambda _floor: tail,
        )
        # RENDERED here, and the value thrown away. It is the admission condition the
        # shape checks cannot be: they read a state's top level, so a value malformed
        # BELOW it -- an event row holding a number where a mapping belongs -- is
        # admitted, survives a tail that touches nothing, and raises in the render the
        # caller performs AFTER this function has returned. The ledger's reader answers
        # that by logging and serving the empty record, so a payload like this would
        # empty a live slot's ledger on every read for the life of the unit. A state no
        # reader can be served is not a state to resume from, and asking costs one render
        # of a bounded value on a read that resumed.
        _FOLDS[name].render(registry.cells(slot)[name][0])
    except Exception:
        # A state that passed every admission condition and then could not be FOLDED or
        # rendered. The file is retired and the caller folds cold, which is the answer
        # ``_drive_session`` gives for a session savepoint and for the same reason --
        # refusing it on the way in would take a per-fold walk of every nested container.
        log_exception_text(
            logger,
            logging.DEBUG,
            "crew log slot %s could not fold its resumed state; discarding and folding cold",
            slot,
        )
        savepoints.discard_slot(handle, (name,))
        return None
    # The savepoint was admitted BEFORE this pass, and that window is not empty. The
    # store's supersede repair replaces orphan chunk records at seqs the savepoint
    # already consumed; the tail starts above them so this fold never reads the
    # replacement, and ``before`` was captured after the repair so it matches the
    # repaired file -- every check on this side passes while the state came from bytes
    # the repair removed. So the savepoint is re-admitted against the file as it stands
    # now, and one that fails to describe it costs a cold fold.
    if not savepoints.slot_resume_still_verifies(
        handle,
        name,
        slot=slot,
        units=units,
        marks=_identity_marks(marks[:-1]),
        ordinal_base=base,
        resumed=saved,
    ):
        logger.debug(
            "crew log slot %s moved under its own savepoint while folding; folding cold", slot
        )
        return None
    return _SlotMemo(
        registry=registry,
        store=slot,
        units=units,
        # ``carried=marks``: this pass re-stat'ed every unit, and the savepoint's identity
        # block confirmed the earlier ones are the vector its state was folded over, so
        # only the newest unit's height comes from the stream.
        marks=_folded_marks(units, marks, tail, carried=marks),
        reached=tail.reached,
        prefix=_vouched(units[-1], before),
        saved=saved.watermark,
        # No resume point, so the first continuation of a cell that stood on a savepoint
        # walks the newest unit once and reports a point of its own. The tail folded here
        # could issue one, and it would be as sound as any other: the cut count is what
        # vouches for an unread prefix, and this pass holds it. It is left unset because
        # a point is an optimisation and this is the one pass that cannot be checked
        # against the loop that wrote it -- the state it continues came from another
        # process, so ``reached`` here is the savepoint's position advanced by this tail
        # rather than a height this cell folded end to end.
        revision=_minted_revision(),
        weight=_cell_weight(name, registry, slot),
    )


def _persist_slot_fold(
    name: str, memo: _SlotMemo, *, slot: str, sampled: "_UnitMark | None"
) -> _SlotMemo:
    """*memo*, with its savepoint on disk brought forward when a write is owed.

    How far a fold must have moved to earn a write is the savepoint module's decision
    (``checkpoint.write_is_earned``), not this one's: it is a property of the files, and
    stating it here as well would give two places an answer that has to agree.

    A WARM CONTINUATION WRITES TOO, and that is the one place this departs from the
    session half, where a pass standing on a cached bundle writes nothing. The reason the
    session half refuses is that a cached bundle records no evidence, so nothing a later
    pass can read says anything about the bytes below its seq. A slot cell is different:
    it CARRIES its evidence -- the newest unit's cut count in ``_SlotMemo.marks``, or its
    digest in ``_SlotMemo.prefix`` on a unit that states no count -- and
    :func:`_folded_prefix_holds` re-checks it before carrying the cell forward, so custody
    of the already-folded prefix survives from the pass that folded it to this one.
    Without this, a gateway that stays up leaves the savepoint wherever the process first
    folded, and the restart it exists for replays the whole gap.

    *sampled* is the newest unit's mark as read BEFORE the pass, and three of its fields
    do the work. Its ``origin`` is the identity the pass folded under, which the write
    refuses to differ from. Its ``last_seq`` is the boundary the evidence was taken at, so
    a pass that ended somewhere ELSE -- the file grew under it -- matches nothing and
    writes nothing: the evidence would then cover fewer bytes than the state was folded
    from, which is the one savepoint no later read recovers from. That costs the savepoint
    and never the read, which is already served. And its ``cuts`` is which of the two
    witnesses this unit gets.

    THE EVIDENCE IS CONFIRMED HERE, at the write, and that is what makes one rule cover
    every pass that reaches this function. A digest taken before a pass and never asked
    again would certify bytes a concurrent truncation changed afterwards, and because
    every later resume recomputes the same changed bytes it would match, and the state
    would be served for the life of the unit while disagreeing with a cold fold. The cold
    and warm passes also vouch for their own digest (:func:`_vouched`), which they need
    for their cell; asking again here costs one decode-free walk on a pass that has just
    parsed and folded the same file, and it buys the property that NOTHING writes a
    savepoint without re-reading the evidence its state came from. The counter is
    re-confirmed for the same reason and at a fraction of the price -- and it is the
    stronger of the two here, because a cut during the pass RAISES it rather than being
    hashed into agreement with itself.
    """
    if not memo.units or sampled is None or sampled.origin is None:
        return memo
    if sampled.cuts is None and memo.prefix is None:
        return memo
    if memo.reached != sampled.last_seq:
        return memo
    if memo.weight > slot_fold_cache_bytes():
        # A cell too big to keep WARM is one whose savepoint the store would refuse too:
        # its cap is :data:`~kiro_crew.crew_log.checkpoint.MAX_CHECKPOINT_BYTES`, two
        # orders of magnitude under this ceiling. The refusal is not the reason to skip --
        # the store swallows it and the read is unaffected -- the MEMORY is: the store
        # decides by serializing first, so attempting a ``radar`` cell at its declared
        # caps would build a 2.4 GiB string to measure it and throw it away. And such a
        # cell is not kept warm (:func:`_remember_slot_fold`), so every read of it would
        # arrive here again. The charge is an upper bound that overestimates by a small
        # factor, so this cannot skip a cell whose payload would really have fit.
        return memo
    cell = _slot_cell(name, memo)
    if cell is None:
        return memo
    _state, watermark = cell
    from kiro_crew.crew_log import checkpoint as savepoints

    if not savepoints.write_is_earned(watermark, memo.saved):
        return memo
    handle = open_session_log(memo.units[-1])
    if handle is None:
        return memo
    witness = _slot_witness(handle, memo, sampled)
    if witness is None:
        return memo
    written = savepoints.save_slot(
        handle,
        name,
        slot=slot,
        units=memo.units,
        marks=_identity_marks(memo.marks[:-1]),
        ordinal_base=sum(mark.last_seq for mark in memo.marks[:-1]),
        state=_state,
        watermark=watermark,
        prefix=witness,
        expect_origin=sampled.origin,
    )
    if not written:
        return memo
    return replace(memo, saved=watermark)


def _slot_witness(
    handle: CrewLog, memo: _SlotMemo, sampled: _UnitMark
) -> "PrefixWitness | CutWitness | None":
    """The evidence *memo*'s savepoint records, re-confirmed now, or ``None``.

    The cut count where the unit states one: *sampled*'s reading, taken before the pass,
    checked against the live file here. A cut in between raises the live count, so this
    answers ``None`` and the savepoint is skipped rather than written with a witness its
    own resume could never admit.

    The digest otherwise, re-labelled with the boundary it was taken at. It covers at
    least every record the fold consumed -- a whole-file walk reaches past the newest
    entry, never short of it -- so it is a witness through ``reached`` and a stricter one
    than the seq span alone would be.
    """
    from kiro_crew.crew_log import checkpoint as savepoints

    if sampled.cuts is not None:
        held = savepoints.CutWitness(seq=memo.reached, cuts=sampled.cuts)
        return held if savepoints.cuts_unchanged(handle, held) else None
    if memo.prefix is None or not _prefix_holds(memo.units[-1], memo.prefix):
        return None
    return savepoints.PrefixWitness(
        seq=memo.reached, records=memo.prefix.records, sha=memo.prefix.sha
    )


def _cell_weight(name: str, registry: ProjectionRegistry, store: str) -> int:
    """What the cell *registry* holds for *name* is charged, read off the live cell.

    Counted from the state the kernel actually holds rather than from the checkpoint,
    because the charge has to describe the object that stays resident -- and that is the
    cell, which the checkpoint only borrows a reference to.
    """
    try:
        state, _watermark = registry.cells(store)[name]
    except Exception:  # pragma: no cover - a cell the registry did not build
        return slot_fold_cell_bytes(name)
    return slot_fold_cell_bytes(name, state)


def _digest_to_carry(
    units: "tuple[str, ...]", marks: "tuple[_UnitMark, ...]"
) -> "_PrefixSeen | None":
    """The newest unit's whole-file digest, for a memo that will NEED one.

    A memo needs one only when the next read of this unit cannot be settled by its cut
    counter, and the counter's presence is what says which case this is. So a unit that
    HAS a counter skips the hash on the write side of the memo as well as the read side
    -- which is where most of the saving is, because this digest covers the whole file
    rather than the records a fold consumed.

    A unit with no counter gets the digest, and the next read is settled by it exactly
    as before.
    """
    if not units or not marks:
        return None
    return None if marks[-1].cuts is not None else _unit_prefix(units[-1])


def _resume_from(memo: "_SlotMemo") -> "ResumePoint | None":
    """*memo*'s resume point, but only where it agrees with what *memo* folded.

    The point and ``reached`` are two records of the same fact -- the last entry this
    cell consumed from its newest unit -- written by the same loop, so they agree by
    construction today. This asserts it anyway, because the two failures are not the
    same size. A point BELOW ``reached`` costs a few extra records on the next read.
    A point ABOVE it would make the next read open the file past entries the cell
    never folded and never yield them, which is state silently missing from a fold
    that reports itself complete.

    So anything other than equality drops back to the full walk: the store's own
    checks cannot catch this one, since an offset that is a perfectly good record
    boundary for the seq it names is exactly what this would hand them.
    """
    point = memo.resume
    if point is None or point.seq != memo.reached:
        return None
    return point


def _vouched(unit_id: str, before: "_PrefixSeen | None") -> "_PrefixSeen | None":
    """*before*, but only if *unit_id* is byte-identical now to what it was then.

    A digest read only AFTER a pass would certify bytes the pass never read: an entry
    rewritten while the fold was running would be hashed together with state folded
    from its earlier value, every later continuation would recompute that same digest,
    match, and keep serving the state. So the digest is taken before the pass and
    confirmed after it, and a file that moved in between vouches for nothing -- which
    costs the next read a cold fold and never a wrong answer.
    """
    if before is None:
        return None
    return before if _unit_prefix(unit_id) == before else None


def _folded_marks(
    units: "tuple[str, ...]",
    sampled: "tuple[_UnitMark, ...]",
    stream: _SlotStream,
    *,
    carried: "tuple[_UnitMark, ...] | None" = None,
) -> "tuple[_UnitMark, ...]":
    """*sampled*, with each streamed unit's height replaced by what was FOLDED.

    This is where :func:`fold_slot_warm`'s invariant is applied: the sampled heights are
    that read's plan, and what a memo remembers is what its pass consumed.

    The FINGERPRINT is carried across as sampled, and deliberately not re-stat'ed after
    the pass. A fingerprint taken afterwards would cover bytes this pass may not have
    folded -- an entry appended between the last read and that stat -- and the next read
    would then compare equal and serve a cell that is missing it. Sampled, the error can
    only fall the other way: a slot whose bytes moved mid-pass fails the comparison, and
    the next read folds cold, which costs time and not correctness.

    *carried* is a warm continuation's earlier marks, which this pass did not read and
    has already found unchanged; only the newest unit is re-stated from the stream.

    Built by REPLACING one field of the sampled mark rather than by naming the fields to
    keep. A mark is compared whole, so a field this function forgot to carry would read
    as changed on the very next pass and send every warm read of every slot back to a
    cold fold -- a mark rebuilt field by field makes that the default outcome of adding
    one.
    """
    base = list(carried if carried is not None else sampled)
    return tuple(
        replace(sampled[index], last_seq=stream.heights.get(unit, base[index].last_seq))
        for index, unit in enumerate(units)
    )


def _slot_checkpoint(name: str, memo: _SlotMemo) -> "tuple[Checkpoint, int]":
    """*memo*'s kernel cell as the checkpoint this module's callers carry, and its revision.

    ``last_seq`` is the newest unit's OWN seq, not the kernel's ordinal, and that is
    the contract rather than an implementation detail: a writer advances this
    checkpoint over the entry it is appending to answer with the record that entry
    produces, and that entry's seq comes from its file. An ordinal here would sit far
    above it and :func:`advance` would refuse the entry as already folded.

    The state comes back AS THE CELL HOLDS IT, not copied, which is what the folds'
    own discipline makes safe: ``apply`` returns a new object rather than editing this
    one, so a later read replaces the cell's state instead of moving what a caller
    kept, and the callers that continue this checkpoint go through :func:`advance`,
    which copies before its first step.
    """
    state, _watermark = memo.registry.cells(memo.store)[name]
    return Checkpoint(name=name, last_seq=max(memo.reached, 0), state=state), memo.revision
