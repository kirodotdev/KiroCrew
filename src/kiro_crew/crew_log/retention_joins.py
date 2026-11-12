"""Which ``superseded`` unit another slot's ledger read still folds, for retention to keep.

A ``superseded`` close proves the unit's OWN slot moved past it: that slot's newer unit
carries every fold the slot reads over it (``store_retention._TERMINAL_CLOSE_REASONS``).
The work ledger is also read from ANOTHER slot. A caller whose turns run in slot T under a
session key whose ledger spelling is S reads under S, and that read folds the units headed
S first, then T's own, applying T's carries and never S's
(``session_ledger.crew_log_units``). Until T holds a ledger carry of its own, that read
takes S's records from S's units themselves, so collecting one of them would drop its
records from T's read while S's own read, which applies S's carry, still shows them. From
T's first ledger carry on, the read replaces at that carry everything S's units folded
before it (they always fold first), so nothing in them reaches it.

:class:`LedgerJoins` is built once per sweep, on its first ``superseded`` candidate, and
reads the crew log tree alone, like the rest of the sweep. The slots that join S are the
ones whose units' openings state a session key with S as its ``ledger_key`` (line 2 of each
unit, and the newest ``session/opened`` of a unit whose line 2 states none, which is an older
build's unit resumed by this one: read from the tail window, and searched across the unit's
segments when the window holds none, only once a candidate's answer depends on it), and the
slots the gateway names
from a header S that is a raw caller key (``session_ledger._slots_named_for``). The slot's
``aliases`` control file is outside the tree and is not read: a ledger caller's spelling is
the ``ledger_key`` of the session key its request names, the one its unit's opening states.

A candidate is KEPT when its slot has such a joiner, it holds a ``ledger/recorded`` entry,
and some joiner holds no ledger ``session/carried`` for its own slot on disk. So is a unit
that a kept one is a predecessor of through the ``previous_sid`` links: collecting it would
make the unit that carries it a chain root, ordered by the header clock, and a backward
clock step would then fold the kept unit's older values after the carry. The same holds for
a unit between two that stay, whatever reads them: it is kept while the unit after it would,
once it is gone, sort before one that comes earlier in the chain by the header clock
(:meth:`LedgerJoins._orders_a_remaining_unit`). A unit with no
ledger record changes no ledger read, so it is collected whoever joins it, and so is a unit
the slot index does not list, since no spelling's listing names it. Anything else that
cannot be read keeps the unit. A slot that starts joining S after the sweep collected one of
S's units reads its records from that slot's first ledger carry on, which applies S's own
carry inside S's units (``projection_readers.fold_joined_checkpoint``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from kiro_crew.crew_log import store
from kiro_crew.crew_log.errors import CODE_BAD_HEADER, CrewLogError
from kiro_crew.crew_log.schema import KIND_SESSION, MAX_ENTRY_BYTES, Entry
from kiro_crew.jsonl_util import UnreadableRecord, strict_raw_records

_OPENED = frozenset({"session/opened"})
#: Byte markers a raw record must contain before it is parsed, so the scans below skip
#: every message body without a JSON parse, the way ``find_spawned_entries`` does.
_RECORD_NEEDLE = b'"ledger/recorded"'
_CARRY_NEEDLE = b'"session/carried"'
_OPENED_NEEDLE = b'"session/opened"'
#: The most ``previous_sid`` links one unit's chain is followed through to find a kept unit.
_MAX_CHAIN = 64


class LedgerJoins:
    """The ledger joins one retention pass reads, listed on first use and then reused."""

    def __init__(self) -> None:
        self._units: "dict[str, tuple[str, ...]] | None" = None
        self._slot_of: dict[str, str] = {}
        self._joiners: dict[str, set[str]] = {}
        self._carried: dict[str, bool] = {}
        #: Units whose line 2 states no key and whose tail window holds no opening: their
        #: newest opening is searched for across their segments, once, the first time a
        #: candidate's answer could depend on it (:meth:`_read_unsettled`).
        self._unsettled: list[str] = []

    def keeps(self, unit_id: str, directory: Path) -> bool:
        """Whether the ``superseded`` unit *unit_id* in *directory* must stay this pass.

        ``True`` when another slot's ledger read still folds a record of it (module
        docstring), and whenever any of that cannot be read.
        """
        try:
            reading = self._reading(unit_id, directory)
            if not reading:
                # The unit is also a link in the chain a kept predecessor is ordered by.
                reading = self._link_of_kept(unit_id)
            if not reading and self._orders_a_remaining_unit(unit_id):
                reading = [self._slot(unit_id) or unit_id]
        except (CrewLogError, OSError, ValueError):
            # Rendered to text: a kept traceback would keep every frame of the pass.
            store.log_exception_text(
                store.logger,
                logging.DEBUG,
                "crew log retention: kept %s, its ledger joins could not be read",
                unit_id,
            )
            return True
        if reading:
            store.logger.debug(
                "crew log retention: kept %s, slot %s's ledger read still folds it",
                unit_id,
                reading[0],
            )
        return bool(reading)

    def _reading(self, unit_id: str, directory: Path) -> "list[str]":
        """The slots whose ledger read still folds *unit_id*'s own records, if any."""
        slot = self._slot(unit_id)
        if not slot:
            # No slot index entry, so no spelling's listing names it and no ledger
            # read, joined or not, folds it.
            return []
        if not self._joiners.get(slot) and not self._unsettled:
            return []
        if _holds(directory, _RECORD_NEEDLE, _is_ledger_record) is None:
            return []
        reading = self._without_a_carry(slot)
        if not reading and self._unsettled:
            # Only now can a unit whose opening was not in its tail window decide this.
            self._read_unsettled()
            reading = self._without_a_carry(slot)
        return reading

    def _without_a_carry(self, slot: str) -> "list[str]":
        """The slots joining *slot* that hold no ledger carry of their own."""
        joiners = self._joiners.get(slot, set())
        return sorted(joiner for joiner in joiners if not self._holds_ledger_carry(joiner))

    def _read_unsettled(self) -> None:
        """Join the slots of the units whose newest opening lies past their tail window.

        Each such unit is read whole, segment by segment, newest first, until an opening
        is found (:func:`_newest_stated_keys`). Taken off the list as it is read, so a
        read that raises keeps this candidate and the next one asks the rest again.
        """
        from kiro_crew.session_ledger import ledger_key

        while self._unsettled:
            unit = self._unsettled[0]
            slot = self._slot_of.get(unit, "")
            for key in _newest_stated_keys(unit):
                spelling = ledger_key(key)
                if spelling and spelling != slot:
                    self._joiners.setdefault(spelling, set()).add(slot)
            self._unsettled.pop(0)

    def _link_of_kept(self, unit_id: str) -> "list[str]":
        """The slots that read a kept predecessor of *unit_id* through its succession chain.

        A kept unit is ordered before the unit that carries it by the ``previous_sid`` links
        between them. Collecting an intervening unit makes the carrying unit a chain root,
        and roots are ordered by the header clock, which a backward step inverts: the kept
        predecessor would then fold after the carry and lay its older values over it. So a
        unit stays while a predecessor reached through its links is kept, up to the chain's
        cap, and a chain that is not read in full keeps the unit.
        """
        current = unit_id
        seen = {unit_id}
        for _ in range(_MAX_CHAIN):
            previous = store.unit_opened_previous(KIND_SESSION, current)
            if not previous:
                return []
            if previous in seen:
                return [self._slot(previous) or previous]
            seen.add(previous)
            directory = store.unit_dir_for(KIND_SESSION, previous)
            if directory is None:
                return []
            try:
                reading = self._reading(previous, directory)
            except FileNotFoundError:
                # Collected earlier in this pass, so it holds nothing and keeps nothing.
                return []
            if reading:
                return reading
            current = previous
        return [self._slot(unit_id) or unit_id]

    def _orders_a_remaining_unit(self, unit_id: str) -> bool:
        """Whether collecting *unit_id* would let the header clock reorder two units that stay.

        A slot's units are ordered by their ``previous_sid`` links, and only a chain's roots
        by the header clock. A unit removed from the middle of a chain splits it: the unit
        after it becomes a root, and a root stamped before an earlier unit that remains
        (a backward clock step) would fold before it and lay that unit's older values over
        its own carry. So *unit_id* stays while some unit that remains before it in the
        chain sorts, by the store's own listing key ``(createdAt, id)``, after one that
        follows it. A chain not read in full, or a header not read, keeps it.
        """
        slot = self._slot(unit_id)
        if not slot:
            return False
        slot_units = (self._units or {}).get(slot, ())
        before: list[str] = []
        current = unit_id
        seen = {unit_id}
        for _ in range(_MAX_CHAIN):
            previous = store.unit_opened_previous(KIND_SESSION, current)
            if not previous or previous in seen or previous not in slot_units:
                break
            if store.unit_dir_for(KIND_SESSION, previous) is None:
                break
            seen.add(previous)
            before.append(previous)
            current = previous
        else:
            return True
        if not before:
            return False
        after: list[str] = []
        frontier = [unit_id]
        for _ in range(_MAX_CHAIN):
            frontier = [
                unit
                for unit in slot_units
                if unit not in seen and store.unit_opened_previous(KIND_SESSION, unit) in frontier
            ]
            if not frontier:
                break
            seen.update(frontier)
            after.extend(frontier)
        else:
            return True
        stamps = {unit: store.unit_header_created_at(KIND_SESSION, unit) for unit in before + after}
        if any(stamp is None for stamp in stamps.values()):
            return True
        return any(
            (stamps[later], later) < (stamps[earlier], earlier)
            for earlier in before
            for later in after
        )

    def _slot(self, unit_id: str) -> str:
        """*unit_id*'s slot, listing the slots and their joiners on the first call."""
        if self._units is None:
            from kiro_crew.session_ledger import _slots_named_for, ledger_key

            try:
                units = store.session_units_by_slot(strict=True)
            except CrewLogError as exc:
                if exc.code != CODE_BAD_HEADER:
                    raise
                # A unit whose header cannot be proved: no spelling's listing names it,
                # so no ledger read folds it, and the rest are listed as readers list them.
                units = store.session_units_by_slot()
            joiners: dict[str, set[str]] = {}
            for slot, unit_ids in units.items():
                for unit in unit_ids:
                    self._slot_of[unit] = slot
                    keys, settled = _stated_keys(unit)
                    if not settled:
                        self._unsettled.append(unit)
                    for key in keys:
                        spelling = ledger_key(key)
                        if spelling and spelling != slot:
                            joiners.setdefault(spelling, set()).add(slot)
                if ":" in slot:
                    for named in _slots_named_for(slot):
                        if named != slot and named in units:
                            joiners.setdefault(slot, set()).add(named)
            self._joiners = joiners
            self._units = units
        return self._slot_of.get(unit_id, "")

    def _holds_ledger_carry(self, slot: str) -> bool:
        """Whether a unit of *slot* on disk holds a ledger carry its own read applies."""
        known = self._carried.get(slot)
        if known is not None:
            return known
        from kiro_crew.crew_log.projection_kernel import carried_state
        from kiro_crew.session_ledger import _FOLD_NAME

        def _own_carry(entry: Entry) -> bool:
            return carried_state(entry, _FOLD_NAME, slot) is not None

        found = False
        for unit in reversed((self._units or {}).get(slot, ())):
            directory = store.unit_dir_for(KIND_SESSION, unit)
            if directory is None:
                continue
            try:
                found = _holds(directory, _CARRY_NEEDLE, _own_carry) is not None
            except FileNotFoundError:
                # Collected earlier in this pass, so it holds nothing.
                continue
            if found:
                break
        self._carried[slot] = found
        return found


def _stated_keys(unit_id: str) -> "tuple[tuple[str, ...], bool]":
    """The session keys *unit_id*'s openings state its turns run under, and whether that
    is settled.

    Line 2, the opening that created the unit, is this build's record of it. A unit whose
    line 2 states none is an older build's, or a keyless opener's, so its NEWEST opening is
    read too: a resume by this build states the key there. The newest segment's tail window
    answers the usual case. When it holds no opening, the unit grew past that window since
    its last opening, and the answer is NOT settled: :func:`_newest_stated_keys` searches the
    segments, which :class:`LedgerJoins` asks only once a candidate's answer depends on it.
    A line 2 that has not landed yet names no key: that unit's slot carries over S's own
    carry when it does.
    """
    created = store.unit_opened_session_key(KIND_SESSION, unit_id)
    if created:
        return (created,), True
    if created is None:
        return (), True
    directory = store.unit_dir_for(KIND_SESSION, unit_id)
    if directory is None:
        return (), True
    try:
        segments = [
            (first, child)
            for child in directory.iterdir()
            if (first := store._segment_first_seq(child)) is not None
        ]
        if not segments:
            return (), True
        tail = store._scan_tail(max(segments, key=lambda pair: pair[0])[1])
    except FileNotFoundError:
        # Removed since the slots were listed, by this pass or another remover.
        return (), True
    opened = None if tail.empty else store._last_entry_of_types(tail, _OPENED)
    if opened is None:
        return (), False
    return _session_key_of(opened), True


def _newest_stated_keys(unit_id: str) -> "tuple[str, ...]":
    """The session key *unit_id*'s newest opening states, searched across its segments.

    Every segment, newest first, a raw record without the type's bytes skipped unparsed
    (:func:`_holds`). Raises what the reads raise, so the caller keeps its candidate.
    """
    directory = store.unit_dir_for(KIND_SESSION, unit_id)
    if directory is None:
        return ()
    try:
        opened = _holds(directory, _OPENED_NEEDLE, _is_opening, newest=True)
    except FileNotFoundError:
        # Removed since the slots were listed, by this pass or another remover.
        return ()
    return _session_key_of(opened) if opened is not None else ()


def _session_key_of(opened: Entry) -> "tuple[str, ...]":
    data = opened.data
    key = data.get("session_key") if isinstance(data, dict) else None
    return (key,) if isinstance(key, str) and key else ()


def _is_ledger_record(entry: Entry) -> bool:
    from kiro_crew.session_ledger_layout import LEDGER_ENTRY_TYPE

    return entry.type == LEDGER_ENTRY_TYPE


def _is_opening(entry: Entry) -> bool:
    return entry.type in _OPENED


def _holds(
    directory: Path, needle: bytes, wanted: "Callable[[Entry], bool]", *, newest: bool = False
) -> "Entry | None":
    """An entry of the unit in *directory* that *wanted* accepts, or ``None`` for none.

    The whole log, every segment, because these entries can sit anywhere in it. A raw
    record without *needle* is skipped unparsed. The first match answers, or with
    *newest* the newest one: the segments are then read newest first, and the first
    segment holding a match answers with its last. Raises what the reads raise, as
    ``OSError`` or ``ValueError``, so a caller tells "holds none" from "was not read".
    """
    segments = sorted(
        (
            (first, child)
            for child in directory.iterdir()
            if (first := store._segment_first_seq(child)) is not None
        ),
        key=lambda pair: pair[0],
        reverse=newest,
    )
    for _first, child in segments:
        if child.stat().st_size == 0:
            continue
        found: "Entry | None" = None
        with open(child, "rb") as handle:
            try:
                for raw in strict_raw_records(handle, child, cap=MAX_ENTRY_BYTES):
                    if needle not in raw:
                        continue
                    parsed = store._parses_to_object(raw.strip())
                    entry = Entry.from_dict(parsed) if parsed is not None else None
                    if entry is not None and wanted(entry):
                        if not newest:
                            return entry
                        found = entry
            except UnreadableRecord as exc:
                raise ValueError(
                    f"crew log segment {child.name} holds an unreadable record"
                ) from exc
        if found is not None:
            return found
    return None
