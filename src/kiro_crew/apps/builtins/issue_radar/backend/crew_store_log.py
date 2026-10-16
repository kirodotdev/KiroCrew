"""The crew log a crew's ledger is recorded in.

The ledger's entry type and fold, the per-crew write lock and the receipts of
appends not yet drained, the check that an append landed, the ledger's error
classes, the crew's units in recorded order and the fold checkpoint a read resumes
from. Callers reach every name here through
:mod:`kiro_crew.apps.builtins.issue_radar.backend.crew_store`.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from kiro_crew.config.paths import data_home
from kiro_crew.crew_log.entry_types import RADAR_ENTRY_TYPE
from kiro_crew.crew_log.store import log_exception_text

from .crew_store_records import CrewStoreError, logger, slot_key_for
from .crew_store_unit_order import _recorded_unit_order

# ── the ledger: work items, progress lines and passes ───────────────────────
#
# A crew's work items, its progress lines and its passes are not files of their
# own. Each ``issue_radar_crew_record`` call appends ONE ``radar/recorded`` entry to
# the crew's own crew log -- the session log of the slot the crew runs on -- and
# the whole ledger (this module, ``crew_store_carry`` and ``crew_store_ledger``) is a
# FOLD of those entries: the ``radar`` projection in
# ``kiro_crew.crew_log.projection``. One entry carries the item's delta, the event
# that explains it and, when the phase is ``skipped``, the skip row, so the three
# writes a three-file store would make all-or-nothing under three locks are one append that a
# crash cannot separate.
#
# A crew's slot owns one ACP session id at a time, so its record is spread over a
# unit per id it ran under and the read joins them (:func:`crew_log_units`). The
# repository's shared skip index is the UNION of every crew's folded passes
# (:func:`read_skips`); that is the one read made across crews, and it is a fold
# rather than a second mutable file.
#
# The pre-projection files (``crews/<crew_id>/<n>.json``, ``events.jsonl``,
# ``skipped.json``) are read once more, to be CARRIED into the log on a crew's
# first write after the upgrade (:func:`_carry_legacy_forward`), and never written.

LEDGER_ENTRY_TYPE = RADAR_ENTRY_TYPE
FOLD_NAME = "radar"
_ENTRY_SRC = "gateway"

#: How long a write waits for the crew log writer to drain before answering. The
#: route runs the write on a worker thread, so this costs no event-loop time.
_APPEND_FLUSH_SECONDS = 5.0

#: One lock per (data home, crew) held across a WRITE's fold, refusals, append and
#: answer. The refusals are checked against the folded record, so two requests for
#: one crew validating against the same fold could both pass the one-editor rule and
#: both append; the route runs writes on worker threads, so the serialization is a
#: thread lock, taken for the whole write including the drain. Reads take nothing.
_crew_write_locks: dict[tuple[str, str], threading.Lock] = {}
_crew_write_locks_guard = threading.Lock()

#: Crews whose last append had not drained to the log when the write answered, each
#: mapped to the RECEIPT for that append: the unit whose log it belongs in, the seq it
#: followed, and the entry itself. The crew's next write drains again BEFORE it folds,
#: so it does not validate against a record missing that entry -- two requests could
#: otherwise both pass the one-editor rule. The receipt is what makes the clearing
#: honest: a drained writer is not a landed entry (a dropped write empties the buffer
#: too), so the mark is released only once the entry is found in that unit's log BY
#: CONTENT. Touched only under the crew's write lock.
_undrained: dict[tuple[str, str], tuple[str, int, dict[str, Any]]] = {}


def _landed_since(
    projection: Any, session_id: str, after_seq: int, data: dict[str, Any]
) -> tuple[tuple[Any, ...] | None, bool]:
    """The entries after *after_seq* in *session_id*'s log, and whether *data* is among
    them BY CONTENT.

    Content rather than seq arithmetic, because the crew's own turn appends to the same
    log while it records, so the span holds more than the entry being looked for. Tried
    twice: a read that fails once (a transient I/O error) must not turn a landed entry
    into a refusal, and a second failure is the same answer as a missing entry -- the
    writer is done and nothing shows the entry in the file. A log that is gone answers
    ``(None, False)``: nothing landed and nothing will.

    Every miss is logged at WARNING with the seq, the unit and the file it read:
    the callers ask only after the writer reported the entry written, so a miss is
    the "written but not readable" state, and the log line is the only place its
    cause shows.
    """
    path: Any = None
    for attempt in (1, 2):
        try:
            handle = projection.open_session_log(session_id)
            if handle is None:
                logger.warning(
                    "crew ledger: entry after seq %d not readable: unit %s has no log",
                    after_seq,
                    session_id,
                )
                return None, False
            path = getattr(handle, "path", None)
            since = tuple(handle.iter_from(after_seq + 1, known=projection.KNOWN_TYPES))
            landed = any(e.type == LEDGER_ENTRY_TYPE and e.data == data for e in since)
            if not landed:
                logger.warning(
                    "crew ledger: entry after seq %d not found in unit %s log %s "
                    "(%d entries read)",
                    after_seq,
                    session_id,
                    path,
                    len(since),
                )
            return since, landed
        except Exception:
            log_exception_text(
                logger,
                logging.WARNING,
                "crew ledger: could not read the entry after seq %d back from unit "
                "%s log %s (attempt %d)",
                after_seq,
                session_id,
                path,
                attempt,
            )
    return None, False


def _crew_write_lock(crew_id: str) -> threading.Lock:
    key = (str(data_home()), crew_id)
    with _crew_write_locks_guard:
        lock = _crew_write_locks.get(key)
        if lock is None:
            lock = _crew_write_locks[key] = threading.Lock()
        return lock


class CrewLedgerUnavailable(CrewStoreError):
    """The crew's session has no crew log to record into.

    The crew log is switched off, or the session has not run its first turn yet.
    A :class:`CrewStoreError` so the crew routes' conflict mapping still answers it,
    and its own class so the write route can name the condition (409
    ``crew_log_unavailable``) instead of a generic conflict.
    """


CrewLedgerUnavailable.__module__ = "kiro_crew.apps.builtins.issue_radar.backend.crew_store"


class CrewLedgerEntryTooLarge(CrewStoreError):
    """The update does not fit one crew log entry, so it can never land."""


CrewLedgerEntryTooLarge.__module__ = "kiro_crew.apps.builtins.issue_radar.backend.crew_store"


class CrewLedgerNotRecorded(CrewStoreError):
    """The crew log writer drained without landing the update: it was refused.

    The session's log was deleted under the write, or the writer gave up on it.
    Nothing was recorded, so the caller is told so (503 ``ledger_not_recorded``)
    rather than handed a record that exists nowhere; the same body may be retried.
    """


CrewLedgerNotRecorded.__module__ = "kiro_crew.apps.builtins.issue_radar.backend.crew_store"


def _projection() -> Any:
    """The fold package, imported where it is used so a crew record read never pulls it."""
    from kiro_crew.crew_log import projection

    return projection


def crew_log_units(
    owner: str,
    repo: str,
    crew_id: str,
    root: Path | None = None,
    *,
    live_session_id: str = "",
    strict: bool = False,
) -> tuple[str, ...]:
    """Every crew log holding *crew_id*'s ledger entries, oldest unit first.

    Units are listed by the wall clock their headers carry, then re-ordered by the
    order this crew RECORDED into them (:data:`_UNIT_ORDER_SUFFIX`): a unit nothing
    recorded into contributes no entries, so its place among them is immaterial and
    header order is kept for it, and every unit absent from the order log is older
    than everything in it -- the log keeps the newest ids -- so those apply FIRST.
    The LIVE unit applies LAST whatever either says: a writer knows which unit it is
    in, and the fold cannot be wrong about it.

    ``()`` when the crew has none -- a crew that never recorded, or a gateway whose
    crew log is off -- and, unless *strict*, on every failure to LIST them, because
    this runs on the read path of a crew's every cycle and a listing that cannot be
    made must not raise into one; a read then answers the empty record. A WRITE lists
    strictly: a listing that failed, and one that is INCOMPLETE because a unit already
    holding entries cannot be proved, are both raised rather than folded, because a
    write that validated against a record missing a unit could admit a second editor.
    """
    try:
        from kiro_crew.crew_log.store import session_units_for_slot

        units = session_units_for_slot(slot_key_for(crew_id), strict=strict)
        recorded = _recorded_unit_order(owner, repo, crew_id, root)
        if recorded:
            known = [u for u in recorded if u in units]
            rest = [u for u in units if u not in recorded]
            units = tuple(rest + known)
        if live_session_id and live_session_id in units:
            units = tuple(u for u in units if u != live_session_id) + (live_session_id,)
        return units
    except Exception:
        logger.warning(
            "crew ledger: could not list the crew logs for crew %s", crew_id, exc_info=True
        )
        if strict:
            raise
        return ()


def _unit_last_seq(unit_id: str) -> int:
    """The newest seq in *unit_id*'s log as the file itself reports it, or 0."""
    try:
        handle = _projection().open_session_log(unit_id)
    except Exception:
        return 0
    if handle is None:
        return 0
    return int(getattr(handle, "last_seq", 0) or 0)


def _fold_checkpoint(crew_id: str, units: tuple[str, ...]) -> Any:
    """This crew's ledger fold, continued from where the last read left it.

    Folding is O(the log), not O(the record): the reader walks every line of every unit
    to find the ``radar/recorded`` ones, and a crew's session log carries its message
    bodies. A crew is read on every cycle it wakes, so re-walking its whole history each
    time is the cost this avoids.

    The incremental fold belongs to the crew log and is shared with every other
    slot-keyed reader (:func:`kiro_crew.crew_log.projection.fold_slot_warm`): it keeps
    this fold's cell in memory per crew, continues it over the entries that arrived
    since, and refolds cold for every shape that cannot be carried -- a different unit
    list, a unit whose log was removed and recreated under the same id or whose identity
    cannot be read, an earlier unit that grew (a forced reset tears a session down while
    a turn is still appending through the handle it holds), and a seq that went
    backwards. ONE implementation rather than one per consumer, because each of these
    readers has to enforce the same rules and a rule missing from one of them is a wrong
    record rather than a slow one.
    """
    return _projection().fold_slot_warm(FOLD_NAME, units, slot=slot_key_for(crew_id))
