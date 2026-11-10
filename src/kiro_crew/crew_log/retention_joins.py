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
unit, and the newest ``session/opened`` in the tail window of a unit whose line 2 states
none, which is an older build's unit resumed by this one), and the slots the gateway names
from a header S that is a raw caller key (``session_ledger._slots_named_for``). The slot's
``aliases`` control file is outside the tree and is not read: a ledger caller's spelling is
the ``ledger_key`` of the session key its request names, the one its unit's opening states.

A candidate is KEPT when its slot has such a joiner, it holds a ``ledger/recorded`` entry,
and some joiner holds no ledger ``session/carried`` for its own slot on disk. A unit with no
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


class LedgerJoins:
    """The ledger joins one retention pass reads, listed on first use and then reused."""

    def __init__(self) -> None:
        self._units: "dict[str, tuple[str, ...]] | None" = None
        self._slot_of: dict[str, str] = {}
        self._joiners: dict[str, set[str]] = {}
        self._carried: dict[str, bool] = {}

    def keeps(self, unit_id: str, directory: Path) -> bool:
        """Whether the ``superseded`` unit *unit_id* in *directory* must stay this pass.

        ``True`` when another slot's ledger read still folds a record of it (module
        docstring), and whenever any of that cannot be read.
        """
        try:
            slot = self._slot(unit_id)
            if not slot:
                # No slot index entry, so no spelling's listing names it and no ledger
                # read, joined or not, folds it.
                return False
            joiners = self._joiners.get(slot)
            if not joiners:
                return False
            if not _holds(directory, _RECORD_NEEDLE, _is_ledger_record):
                return False
            reading = sorted(joiner for joiner in joiners if not self._holds_ledger_carry(joiner))
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
                    for key in _stated_keys(unit):
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
                found = _holds(directory, _CARRY_NEEDLE, _own_carry)
            except FileNotFoundError:
                # Collected earlier in this pass, so it holds nothing.
                continue
            if found:
                break
        self._carried[slot] = found
        return found


def _stated_keys(unit_id: str) -> "tuple[str, ...]":
    """The session keys *unit_id*'s openings state its turns run under, as far as is known.

    Line 2, the opening that created the unit, is this build's record of it. A unit whose
    line 2 states none is an older build's, or a keyless opener's, so the newest opening in
    its tail window is read too: a resume by this build states the key there. A line 2 that
    has not landed yet names no key: that unit's slot carries over S's own carry when it does.
    """
    created = store.unit_opened_session_key(KIND_SESSION, unit_id)
    if created:
        return (created,)
    if created is None:
        return ()
    directory = store.unit_dir_for(KIND_SESSION, unit_id)
    if directory is None:
        return ()
    try:
        segments = [
            (first, child)
            for child in directory.iterdir()
            if (first := store._segment_first_seq(child)) is not None
        ]
        if not segments:
            return ()
        tail = store._scan_tail(max(segments, key=lambda pair: pair[0])[1])
    except FileNotFoundError:
        # Removed since the slots were listed, by this pass or another remover.
        return ()
    if tail.empty:
        return ()
    opened = store._last_entry_of_types(tail, _OPENED)
    data = opened.data if opened is not None else None
    key = data.get("session_key") if isinstance(data, dict) else None
    return (key,) if isinstance(key, str) and key else ()


def _is_ledger_record(entry: Entry) -> bool:
    from kiro_crew.session_ledger_layout import LEDGER_ENTRY_TYPE

    return entry.type == LEDGER_ENTRY_TYPE


def _holds(directory: Path, needle: bytes, wanted: "Callable[[Entry], bool]") -> bool:
    """Whether any segment of the unit in *directory* holds an entry *wanted* accepts.

    The whole log, every segment, because these entries can sit anywhere in it. A raw
    record without *needle* is skipped unparsed. Raises what the reads raise, as
    ``OSError`` or ``ValueError``, so a caller tells "holds none" from "was not read".
    """
    for child in sorted(directory.iterdir()):
        if store._segment_first_seq(child) is None or child.stat().st_size == 0:
            continue
        with open(child, "rb") as handle:
            try:
                for raw in strict_raw_records(handle, child, cap=MAX_ENTRY_BYTES):
                    if needle not in raw:
                        continue
                    parsed = store._parses_to_object(raw.strip())
                    entry = Entry.from_dict(parsed) if parsed is not None else None
                    if entry is not None and wanted(entry):
                        return True
            except UnreadableRecord as exc:
                raise ValueError(
                    f"crew log segment {child.name} holds an unreadable record"
                ) from exc
    return False
