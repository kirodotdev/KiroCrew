"""Where a write of a carried fold or a tree decision lands once its slot may have moved on.

A slot's newest unit carries the slot's fold state (``session/carried``), and the fold
replaces whatever the older units gave it with that state. A write that names an older
unit AFTER such a carry would read wrongly either way: its entry sits in a unit the carry
already replaced, so it is lost, or that unit is published as the unit that recorded
last, so an unpinned read folds it AFTER the carry and lays its whole history over the
state the carry already holds. The same carry copies the slot's newest session-tree
decision into the newest unit (``tree_carry.tree_carries``), and the tree ranks a newer
log's decision first (``session_tree.edge_supersedes``), so a decision filed under the
older unit after the carry loses to the copy of the decision it replaced.

So every writer that appends such an entry into a unit it resolved before its job runs
decides the unit in that job: the ledger writers (``session_ledger.record_update`` and
the Issue Radar ledger, through ``emit_surfaces.on_ledger_recorded`` and
``on_radar_recorded``), the work board's (``emit_surfaces.on_work_recorded``, whose unit
the report route resolves off the writer), the board relay's (``work_relay._queue_run``,
whose unit its consumer thread resolves), the crew panel's
(``emit_surfaces.on_panel_published``), the dashboard's (``emit_surfaces._append_dashboard``,
whose unit the record and write routes resolve through ``_newest_unit`` and the crewmate
feed through its session) and the session tree's (``emit_session.on_session_adopted``,
``on_session_released`` and ``on_session_spawned``, whose unit the dashboard's verbs read
from the session map before they queue). The job asks :func:`hand_to_carrier`, appends,
and publishes the unit order (where the fold keeps one) for the unit the entry landed in.
It runs in the named unit's bucket, and the writer holds one slot's units to one lane
(``writer_groups``), which is where the successor's carry is committed too
(``emit_session._write_carries``, which calls :func:`note_carried` before it publishes
the carrier's order). So the carry lands wholly before the job or wholly after it.
Before, the job finds it and hands itself to the slot's newest unit, which asks again.
After, the carry reads the entry: the older unit's tail moved since the carry's fold read
it, so the carry folds again. A carry an earlier process wrote is read off the later
unit's log (:func:`carrier_after`), so a restart between the carry and the write changes
nothing. A unit the writer has no slot key for is claimed alone, so a job whose caller
could not note the key first notes it and queues itself again
(:func:`requeue_on_slot_lane`). Nothing compares a clock, and nothing guesses which entry
is whose: a ledger job hands its caller a :class:`Receipt` naming the unit and seq the
entry landed at.

Which entry types hand over, by the folds :func:`folds_fed_by` reads off the registry:

* Hand over, because each feeds a slot-keyed carried fold and its writer names a unit
  before the job runs: ``ledger/recorded`` and ``radar/recorded`` (``ledger``,
  ``radar``); ``work/recorded`` and the relay's ``work/charged`` copies (``work``,
  ``workstreams``, the work tree, the worker rows); ``panel/published`` (``panel``); and
  ``dashboard/agentic_value``, ``agentic_refused``, ``custom_value``, ``custom_failed``,
  ``goal_recorded`` and ``waiting_changed`` (``agentic``, ``mistakes``, ``custom``,
  ``goal``, ``waiting``). ``dashboard/stale_wake`` shares the dashboard's helper, so it
  follows the carry too, though it feeds ``status`` alone.
* Hand over under :data:`TREE_KEY`, because the carry copies them and their writer names
  a unit before the job runs: ``session/adopted`` and ``session/released``, whose copy in
  the newest unit outranks a later decision filed under an older one, and
  ``session/spawned``, which filed under an older unit already closed ``superseded``
  would go to retention with it while the child has no log of its own to cite its
  creator.
* Stay in their own unit by design: ``turn/*``, ``message/*``, ``tool/*``, the rest of
  ``session/*`` and the rest of a turn's record are the session's own transcript, written
  into its own unit by the live session, and a live turn of an older unit stands the
  carry down. ``card/*``, ``guide/*``, ``object/observed`` and
  ``dashboard/instance_changed`` feed only the carried ``status`` fold, and only its
  entry count and last time: the gone-row decision it is carried for (the lifecycle, an
  open turn, the last turn's stop reason, ``projection_side_panel._status_step``) never
  moves on them.
"""

from __future__ import annotations

import functools
import logging
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable
from typing import Final

logger = logging.getLogger(__name__)

#: The key the session tree is noted under beside the carried folds' names. The carry
#: copies the slot's newest ``session/adopted`` or ``session/released`` and its
#: ``session/spawned`` entries into the newest unit (``tree_carry.tree_carries``), so a
#: tree entry asks for this key where a fold's entry asks for its folds. No fold is named
#: this, so no fold's carry is ever read as the tree's.
TREE_KEY: Final[str] = "session-tree"

#: Most ``(data home, slot, unit)`` records remembered, the oldest forgotten first: the
#: bound ``writer_groups.UNIT_SLOTS_LIMIT`` puts on the units the writer keys by slot.
CARRIERS_LIMIT: Final[int] = 4096

#: ``(data home, slot, unit)`` -> what *unit* is known to have carried for *slot* (fold
#: names, and :data:`TREE_KEY`), and whether its log was read for a carry an earlier
#: process wrote (:func:`_read_carries`). A carry this process commits is noted on the
#: slot's lane (:func:`note_carried`); a read is merged in, so neither undoes the other.
#: A read unit is not read again: whatever it carries after the read, this process wrote
#: and noted.
_CARRIES: "OrderedDict[tuple[str, str, str], tuple[frozenset[str], bool]]" = OrderedDict()
_UNKNOWN: "Final[tuple[frozenset[str], bool]]" = (frozenset(), False)
_guard = threading.Lock()

#: The bytes a raw record must hold before :func:`_read_carries` parses it: a fold's
#: carry, or a tree entry the carry copied, which it marks ``carried``. A line is written
#: compactly (``schema.serialize``), so each marker is exactly these bytes.
_CARRY_NEEDLES: Final[tuple[bytes, ...]] = (b'"session/carried"', b'"carried":true')
#: The tree entries the carry copies (``tree_carry.tree_carries``).
_TREE_ENTRY_TYPES: Final[frozenset[str]] = frozenset(
    {"session/adopted", "session/released", "session/spawned"}
)


class Receipt:
    """Where one ledger write landed, settled once by the writer job that made it.

    ``unit`` is the unit the entry is in (the slot's newest one when the job redirected
    it), ``seq`` its position there, and ``landed`` whether the append returned. Until it
    is settled, ``landed`` is False: an entry still queued or dropped is not in the file.
    Settled on the writer thread, read by the caller once :meth:`wait` (or a writer
    flush) returns.
    """

    __slots__ = ("unit", "seq", "landed", "_done", "_lock")

    def __init__(self, unit: str) -> None:
        self.unit = unit
        self.seq = 0
        self.landed = False
        self._done = threading.Event()
        self._lock = threading.Lock()

    def settle(self, unit: str, seq: int, *, landed: bool) -> None:
        """Record the outcome. Only the first call counts."""
        with self._lock:
            if self._done.is_set():
                return
            self.unit = unit
            self.seq = seq
            self.landed = landed
            self._done.set()

    @property
    def settled(self) -> bool:
        """Whether the job has said what became of the entry."""
        return self._done.is_set()

    def wait(self, timeout: "float | None") -> bool:
        """Wait at most *timeout* seconds for the outcome; True once it is settled."""
        return self._done.wait(timeout)


def note_carried(slot: str, unit: str, folds: "Iterable[str]") -> None:
    """Remember that *unit*'s carry of each of *folds* for *slot* has landed.

    Called by the carry on the slot's lane, right after its group commit and before the
    carrier's order is published, so a writer job on the same lane that runs after it
    finds it. :data:`TREE_KEY` among *folds* says the session tree's entries hand over to
    *unit* too.
    """
    if not slot or not unit:
        return
    _remember(slot, unit, frozenset(folds), read=False)


def clear() -> None:
    """Forget every carrier. For ``emit.reset_caches``, which stands in for a restart."""
    with _guard:
        _CARRIES.clear()


def carrier_after(slot: str, unit_id: str, *folds: str) -> str:
    """The slot's newest unit when *slot* moved past *unit_id* for any of *folds*, else ``""``.

    *folds* are carried fold names, or :data:`TREE_KEY` for a tree entry. Each branch, in
    the order asked, the cheapest first:

    * ``""`` when *unit_id* is the slot's newest unit, the ordinary writer: the
      succession is cached and no log is opened. ``""`` too when it is not in the slot's
      succession (another spelling's unit).
    * The newest unit when a unit AFTER *unit_id* in the succession is known to have
      carried one of *folds*: its carry was committed in this process
      (:func:`note_carried`), or its log was read here before. No log is opened.
    * The newest unit when *unit_id* is already closed ``superseded``: its successor
      carried every fold it held, and retention may collect it. A tail read.
    * The newest unit when a later unit's log, read once per process
      (:func:`_read_carries`), holds a carry an earlier process wrote: a
      ``session/carried`` for *slot* naming one of *folds*, or, for :data:`TREE_KEY`, any
      ``session/carried`` for *slot* or a tree entry marked ``carried``. A restart
      forgets this process's notes, and a unit a session-map key still maps is never
      closed, so this read is what finds such a carry then. What it finds is noted, so
      the next write hits the branch above.
    * ``""`` when no later unit carried yet (a carry still to land reads the unit's
      entries when it does), when a read failed (that unit proves nothing and is read
      again next time), or when anything else cannot be read: the write then goes where
      it always went.

    Exact on the slot's lane, where the writer job asks it: the read there sees all of
    the carry's one group commit or none of it. Asked off the lane it is a first guess
    the job asks again.
    """
    if not slot or not unit_id:
        return ""
    try:
        from kiro_crew.crew_log import projection

        units = projection.slot_succession(slot)
        if unit_id not in units or units[-1] == unit_id:
            return ""
        later = units[units.index(unit_id) + 1 :]
        wanted = frozenset(folds)
        if (
            _known_carrier(slot, later, wanted)
            or _closed_superseded(unit_id)
            or _read_carrier(slot, later, wanted)
        ):
            return units[-1]
    except Exception:
        # Rendered to text: a kept traceback keeps this frame, and a crew log handle bound in it.
        from kiro_crew.crew_log.store import log_exception_text

        log_exception_text(
            logger,
            logging.DEBUG,
            "crew log %s: could not tell whether slot %s moved on",
            unit_id,
            slot,
        )
    return ""


@functools.lru_cache(maxsize=None)
def folds_fed_by(entry_type: str) -> "tuple[str, ...]":
    """Every carried fold an *entry_type* entry can move, by the fold registry's own sets.

    A ``work/recorded`` entry moves the board's ``work`` and ``workstreams`` folds, the
    work tree, the worker rows and ``status``; ``work/charged`` moves ``workstreams`` and
    ``status``. Read off each fold's ``affects`` (``_Fold.touched_by_type``) rather than
    listed here, so a fold that starts reading the type is asked about the day it does.
    Cached: the registry is built at import and never changes.
    """
    from kiro_crew.crew_log import projection

    return tuple(
        name
        for name in sorted(projection.CARRIED_FOLD_NAMES)
        if projection._FOLDS[name].touched_by_type(entry_type)
    )


def hand_to_carrier(
    slot: str, unit: str, folds: "Iterable[str]", land: "Callable[[str], None]", what: str
) -> bool:
    """Hand ``land(newer)`` to *slot*'s newest unit when it carried one of *folds* past *unit*.

    *folds* are the carried folds the entry feeds, or ``(TREE_KEY,)`` for a tree entry.
    From the writer job that was about to append into *unit*, on the slot's lane (the
    writer thread, or the caller's under the writer's inline claim; the module docstring
    says why that lane decides). True when the job was handed on: the caller then appends
    nothing, and the follow-on job, in the newer unit's own bucket, runs *land* there,
    which asks again. False when the entry belongs in *unit* (:func:`carrier_after`
    answered ``""``, or *slot* is empty). The newer unit's slot is noted for the writer
    first, so its bucket is held to the same lane.
    """
    newer = carrier_after(slot, unit, *folds)
    if not newer or newer == unit:
        return False
    from kiro_crew.crew_log import writer_groups
    from kiro_crew.crew_log.emit_runtime import _hand_over
    from kiro_crew.crew_log.writer import WriteJob

    if not writer_groups.known(newer):
        writer_groups.note_unit_slot(newer, slot)
    _hand_over(newer, WriteJob.follow_on(functools.partial(land, newer), what))
    return True


def requeue_on_slot_lane(unit: str, land: "Callable[[str], None]", what: str) -> bool:
    """Queue ``land(unit)`` again once *unit*'s slot is noted, when the writer had no key for it.

    For a writer job whose caller could not read *unit*'s header before submitting it,
    because that caller may be on the serving loop (``emit_surfaces._append_dashboard``
    for the crewmate feed). A unit with no key (closed, which drops its key, or never
    opened by this process) was claimed alone, off its slot's lane, where
    :func:`hand_to_carrier` cannot answer exactly. So the header is read here, on the
    writer, the slot noted, and *land* queued behind it as a follow-on, whose claim is
    held to the slot's lane and asks again. False when *unit* already has a key, or its
    header names no slot: the job then decides where it is, as it always did.
    """
    from kiro_crew import session_ledger
    from kiro_crew.crew_log import writer_groups

    if writer_groups.known(unit):
        return False
    writer_groups.note_unit_slot(unit, session_ledger.canonical_slot("", unit))
    if not writer_groups.known(unit):
        return False
    from kiro_crew.crew_log.emit_runtime import _hand_over
    from kiro_crew.crew_log.writer import WriteJob

    _hand_over(unit, WriteJob.follow_on(functools.partial(land, unit), what))
    return True


def _remember(slot: str, unit: str, carried: "frozenset[str]", *, read: bool) -> None:
    """Merge *carried* into what *unit* is known to have carried for *slot*; mark it *read*."""
    key = (_home(), slot, unit)
    with _guard:
        held, was_read = _CARRIES.get(key, _UNKNOWN)
        _CARRIES[key] = (held | carried, was_read or read)
        _CARRIES.move_to_end(key)
        while len(_CARRIES) > CARRIERS_LIMIT:
            _CARRIES.popitem(last=False)


def _known_carrier(slot: str, later: "tuple[str, ...]", wanted: "frozenset[str]") -> bool:
    """Whether one of *later* is known here to have carried one of *wanted*. Opens no log."""
    home = _home()
    with _guard:
        return any(_CARRIES.get((home, slot, unit), _UNKNOWN)[0] & wanted for unit in later)


def _read_carrier(slot: str, later: "tuple[str, ...]", wanted: "frozenset[str]") -> bool:
    """Whether one of *later* holds a carry of one of *wanted* on disk, reading each unread one.

    Newest first, and a unit is read once per process (:data:`_CARRIES`): the first read
    after a restart pays for it, and every write after that hits :func:`_known_carrier`.
    A unit whose read failed is left unread, so it is read again next time.
    """
    home = _home()
    for unit in reversed(later):
        with _guard:
            read = _CARRIES.get((home, slot, unit), _UNKNOWN)[1]
        if read:
            continue
        carried = _read_carries(slot, unit)
        if carried is None:
            continue
        _remember(slot, unit, carried, read=True)
        if carried & wanted:
            return True
    return False


def _read_carries(slot: str, unit: str) -> "frozenset[str] | None":
    """What *unit*'s log holds of a carry for *slot*, read off the file; ``None`` when unread.

    The folds its ``session/carried`` entries for *slot* name, and :data:`TREE_KEY` when
    it holds one of those or a tree entry marked ``carried``. A unit holds at most one
    carry (the slot carry runs for a unit when it is created), and
    ``emit_session._write_carries`` appends it in ONE group commit, tree entries first. So
    the segments are read oldest first and the read stops at the first record after that
    run; a unit holding none is read through once. A raw record without either marker is
    skipped unparsed, so a message body costs no parse. ``None`` for a unit with no
    directory or a read that failed: either proves nothing.
    """
    from kiro_crew.crew_log import store
    from kiro_crew.crew_log.schema import KIND_SESSION, MAX_ENTRY_BYTES
    from kiro_crew.jsonl_util import UnreadableRecord, strict_raw_records

    directory = store.unit_dir_for(KIND_SESSION, unit)
    if directory is None:
        return None
    found: set[str] = set()
    in_run = False
    try:
        segments = sorted(
            (
                (first, child)
                for child in directory.iterdir()
                if (first := store._segment_first_seq(child)) is not None
            ),
            key=lambda pair: pair[0],
        )
        for _first, child in segments:
            with open(child, "rb") as handle:
                for raw in strict_raw_records(handle, child, cap=MAX_ENTRY_BYTES):
                    keys = _carry_keys(raw, slot)
                    if keys is None:
                        if in_run:
                            return frozenset(found)
                        continue
                    in_run = True
                    found |= keys
    except (OSError, ValueError, UnreadableRecord):
        store.log_exception_text(
            logger,
            logging.DEBUG,
            "crew log %s: its carry for slot %s could not be read",
            unit,
            slot,
        )
        return None
    return frozenset(found)


def _carry_keys(raw: bytes, slot: str) -> "frozenset[str] | None":
    """The keys one raw record adds when it is part of a carry, ``None`` when it is not."""
    if not any(needle in raw for needle in _CARRY_NEEDLES):
        return None
    from kiro_crew.crew_log import store
    from kiro_crew.crew_log.entry_types import SLOT_CARRY_ENTRY_TYPE
    from kiro_crew.crew_log.schema import Entry

    parsed = store._parses_to_object(raw.strip())
    entry = Entry.from_dict(parsed) if parsed is not None else None
    if entry is None:
        return None
    data = entry.data
    if entry.type == SLOT_CARRY_ENTRY_TYPE:
        fold = data.get("fold")
        if data.get("slot") == slot and isinstance(fold, str) and fold:
            return frozenset((fold, TREE_KEY))
        return frozenset()
    if entry.type in _TREE_ENTRY_TYPES and data.get("carried") is True:
        return frozenset((TREE_KEY,))
    return None


def _closed_superseded(unit_id: str) -> bool:
    """Whether *unit_id*'s newest lifecycle entry is a ``superseded`` close. A tail read."""
    from kiro_crew.crew_log.entry_types import SUPERSEDED_CLOSE_REASON
    from kiro_crew.crew_log.schema import KIND_SESSION
    from kiro_crew.crew_log.store import _newest_close, unit_dir_for

    directory = unit_dir_for(KIND_SESSION, unit_id)
    if directory is None:
        return False
    closed = _newest_close(directory)
    return closed is not None and closed.data.get("reason") == SUPERSEDED_CLOSE_REASON


def _home() -> str:
    from kiro_crew.config.paths import data_home

    return str(data_home())
