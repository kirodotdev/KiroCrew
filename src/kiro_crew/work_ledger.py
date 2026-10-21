"""The shared work record between a conductor session and the workers it dispatched.

Three ledgers carry that name, and they are not interchangeable.
:mod:`kiro_crew.session_ledger` is ONE session's own durable state. Issue Radar's
``crew_store`` is a per-repository work ledger keyed by a forge issue number. This
module is the third: a record two parties write and neither owns, so that a
conductor learns what a worker did as DATA instead of reading its transcript.

This module is the STORAGE layer only. Its one importer is
``dashboard/handlers/work_ledger.py``, which serves the ``/api/work-ledger``
routes; the MCP tools in :mod:`kiro_crew.mcp_work` (``work_brief``,
``work_report``, ``work_ledger_read``, ``work_ledger_record``) reach it only
through those routes. Every write therefore passes the two entry points below (in
:mod:`kiro_crew.work_ledger_writes`), so the writer-ownership rule is enforced in one
place. The store's layers live in the sibling ``work_ledger_*`` owner modules; this
module keeps the projection rebuild, its cache-dirty flag and the write snapshots, and
binds every name the owners define, so ``kiro_crew.work_ledger.<name>`` reads, imports
and patches each of them.

WRITER OWNERSHIP is the whole design, and it is expressed as two entry points rather
than one update function with a field allowlist:

  * :func:`apply_conductor_action` writes ``title``, ``acceptance``, ``state``,
    ``verdict``, ``decision``, ``worker_session_key``, ``round`` and ``fails``.
  * :func:`apply_worker_report` writes ``status``, ``reason``, ``summary``,
    ``artifacts``, ``pr`` and ``last_report_at``.

The two field sets are disjoint. Phase 2 mounts one tool on each, so a worker cannot
reach a conductor field because the function it can call takes no parameter that
names one — an absent parameter outlives an allowlist that must be kept correct as
fields are added. Phase 1 performs NO identity resolution; that is Phase 2's job at
the tool layer, and the split shape here is what lets it be done by construction.

LOCK ORDER, for the two paths that hold more than one lock. ``create`` enforces the
two per-conductor bounds, which means reading the header's create counter and the
items directory and then writing both the header and the item, so it holds the
conductor lock across the whole transaction and takes the new item's lock from
inside that hold. ``bind`` holds the item lock and takes the worker's binding lock
from inside it, because "this worker holds no other open item" is a property of
the binding file, not of the item. So: **conductor -> item -> binding(worker)**.
No path anywhere takes any two of these in the other relative order, so the order
is total and two conductors cannot deadlock. Every other write takes exactly one
lock: the conductor lock for ``goal``, the item lock for ``decide``/``verdict``/
``close`` and for a worker report. File locks on fresh descriptors do NOT nest, so
each locked body has a ``_locked`` twin that a holder calls directly rather than
re-acquiring.

CAPS REFUSE, THEY DO NOT TRUNCATE. Every bound on a STORED field is validated
before the first write, so a refusal leaves every file byte-identical. A truncated
summary the worker believes landed whole is a silent loss the worker cannot detect.
The one bounded projection is the event line's ``text``: it is a 500-char excerpt of
a field the item record already holds in full, so clipping it loses nothing.

CORRUPTION READS AS ABSENT. A torn, truncated, non-UTF-8 or oversized record returns
``None`` rather than raising, matching :mod:`kiro_crew.session_ledger`'s treatment of
an oversized state file. A reader of a two-writer store must not be the thing that
crashes because the other writer was interrupted.
"""

from __future__ import annotations

import dataclasses
import json
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

from kiro_crew.atomic_write import atomic_write
from kiro_crew.work_vocab import WORK_FOLD_NAME

# --------------------------------------------------------------------------- #
# Projection. The crew log is the record; the files ``work_ledger_store`` lays out
# are its cache.
# --------------------------------------------------------------------------- #


def rebuild_from_projection(slot_key: str) -> dict[str, Any]:
    """Re-materialise *slot_key*'s ledger files from the crew log's ``work`` fold.

    Every write the routes accept is recorded as one ``work/recorded`` entry in
    the acting session's crew log, so the JSON under ``work-ledger/<conductor>/``
    is a cache of those entries: this rewrites the header, every item and every
    item's event log from the fold, and removes any item file the fold does not
    know, so the cache afterwards holds exactly the recorded board. Every writer
    is held off for the whole operation: the conductor lock (every conductor
    action takes it) and the lock of every item the cache holds (a report takes
    its item's lock and nothing else) are acquired first, then the crew-log writer
    is drained so an entry acknowledged to a caller but still queued is folded,
    then the fold is read and written back -- so no write accepted while the fold
    was read can be overwritten by it. An item the fold CREATES is locked as soon
    as the fold names it, which is after that read and still early enough: no
    writer can take the lock of an item whose record does not exist yet, so the
    window a writer could use opens only once this rebuild writes the file, by
    which time its lock is held. A slot whose fold holds no entry leaves the files
    untouched, so a caller can tell "no entries" from "rebuilt empty". A closed record
    the fold does not hold (it forgets closed items past
    ``work_vocab.WORK_CLOSED_WINDOW``) is kept as the cache holds it. The rebuilt header's
    :attr:`ConductorRecord.created_total` is the number of items the fold ever held,
    forgotten ones included, plus every legacy record kept, raised to a trusted cache
    header's count: the goal's spend count follows the record, but never drops below
    what the cache says was already spent.

    Returns the counts: ``{"slot_key", "items", "events", "removed", "legacy"}``.
    """
    # Imported here, not at module scope, on purpose: ``ledger_sweep`` imports this
    # module at boot, so a module-scope import would load the crew log's storage
    # subsystem on a flag-off launch. That is not a style preference -- it is pinned
    # by ``test_crew_log_emit_redaction_and_flag.py::test_a_flag_off_launch_does_not_load_the_storage_subsystem``,
    # which fails when these move up. The ``top-level-imports`` convention is
    # advisory; this boot-path invariant is enforced, so the invariant wins.
    from kiro_crew.crew_log import emit as crew_log_emit
    from kiro_crew.crew_log.projection import (
        WORK_ITEM_LIMIT,
        read_slot_projection,
        work_slots_naming_board,
    )
    from kiro_crew.crew_log.store import unprovable_session_units

    with ExitStack() as held:
        held.enter_context(conductor_lock(slot_key, create=True))
        present = _stored_item_ids(slot_key)
        for item_id in present:
            held.enter_context(item_lock(slot_key, item_id, create=True))
        crew_log_emit.flush(timeout=5.0)
        unprovable = unprovable_session_units()
        if unprovable:
            # A unit whose header cannot be proved might be this board's worker;
            # a fold without it would read as complete and erase what it held.
            raise WorkLedgerError(
                f"{unprovable} crew-log session unit(s) have an unreadable header, so the fold "
                "would be partial; repair or remove those units under the crew log root "
                "(any slot's), then rebuild",
                code=CODE_CREW_LOG_INCOMPLETE,
            )
        # Workers the record's own bind entries may not name (bound before the
        # board was recorded) join the fold from two more places: the cached binding
        # files, and every other slot whose log carries an entry naming this board
        # -- the one source that survives losing the cache and the bindings both.
        extra_slots = list(_bound_workers(slot_key))
        for other in work_slots_naming_board(slot_key):
            if other not in extra_slots:
                extra_slots.append(other)
        folded = read_slot_projection(slot_key, WORK_FOLD_NAME, also_slots=extra_slots).value
        header = folded.get("conductor") if isinstance(folded, dict) else None
        if not isinstance(header, dict) or not header.get("entries"):
            # No entry names this board: nothing recorded, so nothing to rebuild
            # from -- and never a reason to remove what the cache holds.
            return {"slot_key": slot_key, "items": 0, "events": 0, "removed": 0}
        # Every file this rebuild may touch is snapshotted first; a failure part
        # way through puts all of them back, so the cache is never left half
        # rewritten: it is the old board or the rebuilt one, nothing between.
        fold_items = [raw for raw in (folded.get("items") or ()) if isinstance(raw, dict)]
        omitted = folded.get("omitted")
        if len(fold_items) >= WORK_ITEM_LIMIT and (not isinstance(omitted, int) or omitted > 0):
            # The fold HOLDS at most ``WORK_ITEM_LIMIT`` items at once and counts
            # every create past that in ``omitted``. It forgets closed items past its
            # window, so a board the writer admits (at most 32 open) never fills it,
            # and this guard is defensive: it fires on a log something else wrote. A
            # full fold that reports omissions might be a PREFIX of the board --
            # rebuilding from it would write the oldest items back and, past the
            # dirty-cache shortcut below, which trusts the fold over the cache, unlink
            # every newer one, open items included -- so it is refused; the cache
            # stands. A full fold that omitted nothing rebuilds; a fold whose count is
            # unreadable is treated as full.
            raise WorkLedgerError(
                f"the crew log's fold holds {len(fold_items)} items for this board, its "
                f"ceiling of {WORK_ITEM_LIMIT}, and omitted {omitted!r}; a board that "
                "minted more than the fold retains cannot be rebuilt from the record -- "
                "keep the cache as it stands",
                code=CODE_CREW_LOG_INCOMPLETE,
            )
        # An item the fold CREATES needs its lock held here, not merely while its
        # own files are written. The write routes take an item's lock alone and
        # never the conductor lock, so a lock released as soon as that item was
        # written leaves a window where a second gateway commits a report against
        # it -- and a not-yet-present item's footprint snapshot is ``None``, so the
        # undo below unlinks that committed write without a trace. Held on
        # ``held``, every affected lock outlives both the rebuild and its undo.
        # Sorted, the order every other multi-item hold here uses, so this cannot
        # deadlock against one of those; deduplicated against ``present``, whose
        # locks are already held, because one process cannot hold a lock twice.
        for new_id in sorted(
            {
                raw["item_id"]
                for raw in fold_items
                if isinstance(raw.get("item_id"), str)
                and raw["item_id"] not in present
                and _ITEM_ID_RE.fullmatch(raw["item_id"])
            }
        ):
            held.enter_context(item_lock(slot_key, new_id, create=True))
        forgotten = _folded_forgotten(folded)
        _refuse_fold_behind_cache(slot_key, present, fold_items, header, forgotten)
        touched = _rebuild_footprint(slot_key, present, fold_items)
        try:
            counts = _rebuild_locked(slot_key, header, fold_items, present, forgotten)
        except BaseException:
            try:
                _restore_files(touched)  # the locks are already held here
            except Exception:
                # Neither the old board nor the rebuilt one: say so where every
                # reader looks, until a rebuild succeeds.
                mark_cache_dirty(slot_key, "a rebuild failed and its undo failed")
                logger.warning("work ledger rebuild undo failed for %s", slot_key, exc_info=True)
            raise
        clear_cache_dirty(slot_key)
        return counts


class _Forgotten(NamedTuple):
    """What a ``work`` fold view says about the closed items it let go."""

    #: Every item the fold has held, forgotten ones included.
    created: int
    #: How many closed items it let go.
    items: int
    #: Forgotten items the fold saw a baseline for, so ``created`` already counts them.
    baseline_ids: frozenset[str] = frozenset()


def _folded_forgotten(folded: Any) -> _Forgotten:
    """The fold's ``forgotten`` counts, zero for a view that carries none."""
    view = folded if isinstance(folded, dict) else {}
    counts = view.get("forgotten")
    counts = counts if isinstance(counts, dict) else {}
    raw_ids = counts.get("baseline_ids")
    return _Forgotten(
        created=max(0, _as_int(counts.get("created"), 0)),
        items=max(0, _as_int(counts.get("items"), 0)),
        baseline_ids=frozenset(
            entry
            for entry in (raw_ids if isinstance(raw_ids, list) else ())
            if isinstance(entry, str)
        ),
    )


def _may_be_forgotten(cached: "WorkItem", forgotten: "_Forgotten | None") -> bool:
    """Whether *cached* may be a closed item the fold let go, so its absence from the
    fold is no sign of a lost unit.

    The rebuild KEEPS such a record rather than judging it: the cache is the only
    place a forgotten item stays whole, and nothing in the store removes a record. So
    a closed record the fold lacks, on a board whose fold reports forgotten items, is
    neither refused nor removed -- whatever the reason it is absent, keeping it loses
    nothing. An open record the fold lacks is still a lost unit and still refused.
    """
    return forgotten is not None and forgotten.items > 0 and cached.is_terminal


def _refuse_fold_behind_cache(
    slot_key: str,
    present: "list[str]",
    fold_items: "list[dict[str, Any]]",
    header: "dict[str, Any] | None" = None,
    forgotten: "_Forgotten | None" = None,
) -> None:
    """Refuse the rebuild when the record holds LESS than the cache does.

    A unit retention pruned, or one that never made it to disk, leaves the fold
    short of what the cache saw land: an item created after this board's first
    recorded entry that no entry names any more, a cached report later than the
    fold's latest, or more event lines than the fold produced. Rebuilding from
    such a fold would erase work, so it refuses instead and names the item; the
    caller decides what the cache is worth. An item born before that projection
    epoch is compared on its reports only: its earlier history was never recorded,
    by design. The fold's ``first_entry_at`` and the item's own ``created_at`` are
    the epoch signal, matching the legacy-preservation rule in
    :func:`_rebuild_locked`.
    """
    cached_header = read_conductor(slot_key)
    # An ABANDONED write is the only reason the cache can be ahead here: the flag is set
    # solely when an unrecorded write could not be undone, so the cache's surplus is
    # explicitly not authoritative -- the record never saw it and the store already tried
    # to remove it. Refusing the rebuild to protect that surplus protected nothing, and it
    # closed the only exit: every route answers 409 cache_dirty naming a rebuild as the
    # cure, while the rebuild answered 409 crew_log_incomplete because the cache was ahead.
    # The board was locked with no way out. So when the flag stands, prefer the record.
    #
    # The generation guard below is deliberately NOT skipped: a generation mismatch means
    # the slot was reused and the fold answers with a PURGED board, which is not a
    # cache-ahead question and stays refusable however this flag reads.
    abandoned = cache_dirty(slot_key)
    if (
        cached_header is not None
        and not abandoned
        and cached_header.recorded_at
        and header is not None
        and cached_header.goal_version > _as_int(header.get("goal_version"), 0)
    ):
        # The header has the same two-step write as an item: a goal committed to
        # the cache whose entry never landed (the gateway died between the two)
        # leaves the cache one goal ahead of the record, and rebuilding from the
        # record would put the older goal and round back.
        raise WorkLedgerError(
            "the board's goal was written after the last goal the crew log holds; "
            "a unit was pruned or a goal write was never recorded, so the record "
            "cannot rebuild this board -- keep the cache as it stands, or record "
            "the goal again (work_ledger_record action=goal) and rebuild",
            code=CODE_CREW_LOG_INCOMPLETE,
        )
    folded_generation = _as_str((header or {}).get("generation"))
    if (
        cached_header is not None
        and cached_header.generation
        and folded_generation
        and cached_header.generation != folded_generation
    ):
        # A board is stamped with its own generation once, when it is created, so
        # two generations under one slot are two different boards: the slot was
        # reused after the earlier one was purged. A purge cannot take the earlier
        # board's ENTRIES back -- the crew log is append-only -- so the fold still
        # answers with the purged board while the cache holds the live one, and
        # rebuilding would replace the live board with the dead one.
        #
        # Not covered by the goal_version guard above, which is conditioned on
        # ``recorded_at``. That stamp lands only once a goal entry has, so a board
        # whose FIRST goal died between the cache commit and the append has an
        # unstamped header and skips that guard -- the very case it exists for.
        #
        # BOTH generations must be non-empty. A fold carrying none holds no entries
        # for a generation-stamped board, and a cache carrying none is a legacy
        # board from before the field; neither can be confused with a second board,
        # and :func:`_rebuild_locked` already adopts the cache's generation when the
        # record has none.
        raise WorkLedgerError(
            "the board in the cache is not the board the crew log holds: the two "
            "carry different generations, so this slot was reused after an earlier "
            "board was purged and the record still holds that earlier board's "
            "entries. Rebuilding would put the purged board back -- keep the cache "
            "as it stands, or remove this board's cached files and rebuild to "
            "reproduce the recorded board in its place",
            code=CODE_CREW_LOG_INCOMPLETE,
        )
    if abandoned:
        # Past the generation guard, every remaining check below asks whether the CACHE
        # holds more than the fold. With the board flagged, that surplus is the abandoned
        # write itself, so there is nothing left here worth refusing for -- and refusing is
        # what sealed the board. The rebuild proceeds and clears the flag, which is the
        # cure the dirty refusal has been naming all along.
        return
    by_id = {raw["item_id"]: raw for raw in fold_items if isinstance(raw.get("item_id"), str)}
    epoch = _parse_iso(_as_str((header or {}).get("first_entry_at")))
    for item_id in present:
        cached = read_work_item(slot_key, item_id)
        if cached is None:
            continue
        folded = by_id.get(item_id)
        if folded is None and _may_be_forgotten(cached, forgotten):
            # A closed item the fold may have let go once newer closed items filled
            # its window. The rebuild keeps its cached record (``_rebuild_locked``),
            # so there is nothing to protect by refusing.
            continue
        if folded is None:
            created = _parse_iso(cached.created_at)
            post_epoch = (
                epoch is not None
                and created is not None
                and (created.tzinfo is None) == (epoch.tzinfo is None)
                and created > epoch
            )
            if not cached.recorded_at and not post_epoch:
                # A confirmed pre-epoch item is a legacy baseline. An absent or
                # same-second stamp cannot prove the create followed the epoch;
                # the rebuild's existing rule keeps same-second items and removes
                # malformed cache rows, so this guard preserves both outcomes.
                continue
            raise WorkLedgerError(
                f"item {item_id} was created after the board's projection epoch but "
                "no crew-log entry names it now; a unit was pruned or its create "
                "was never recorded, so the record cannot rebuild this board -- keep "
                "the cache as it stands, or remove that item's cached files and "
                "rebuild to reproduce the board without it",
                code=CODE_CREW_LOG_INCOMPLETE,
            )
        if not cached.recorded_at:
            continue
        latest = folded.get("last_report_at")
        if cached.last_report_at and (
            not isinstance(latest, str) or latest < cached.last_report_at
        ):
            raise WorkLedgerError(
                f"item {item_id}'s latest report is not in the crew log; "
                "a unit was pruned, so the record cannot rebuild this board -- keep the "
                "cache as it stands, or remove that item's cached files and rebuild to "
                "reproduce it without the unrecorded report",
                code=CODE_CREW_LOG_INCOMPLETE,
            )
        folded_events = folded.get("events")
        folded_list = folded_events if isinstance(folded_events, list) else []
        cached_list = _read_events_unlocked(item_events_path(slot_key, item_id))
        cached_events = len(cached_list)
        if folded.get("baseline") is True:
            # Born from a baseline: the events before its first recorded mutation
            # were never recorded, by design, so only the tail from that mutation
            # on is compared. The first folded event carries the store's own id,
            # which finds it in the cache; a cached tail longer than the fold
            # means a later mutation's unit is gone.
            first = (
                folded_list[0].get("id")
                if folded_list and isinstance(folded_list[0], dict)
                else None
            )
            start = next((i for i, ev in enumerate(cached_list) if ev.id == first), None)
            if not isinstance(first, str) or start is None:
                continue
            cached_events = len(cached_list) - start
        folded_ids = {ev.get("id") for ev in folded_list if isinstance(ev, dict)}
        # Every comparable cached event is checked by identity, not just the newest.
        # The count below is uninformative once the cache holds its cap (the store
        # keeps the newest cap and the fold the same, so the comparison is 200 > 200),
        # and a pruned unit does not have to be the newest one: a middle event whose
        # unit is gone leaves the count and the latest id both matching, and only its
        # own absence from the fold shows it.
        absent_event: WorkEvent | None = None
        for cached_event in cached_list[len(cached_list) - cached_events :]:
            if cached_event.id not in folded_ids:
                absent_event = cached_event
                break
        if absent_event is not None:
            raise WorkLedgerError(
                f"item {item_id}'s cached event {absent_event.id} is not in the crew log; a "
                "unit was pruned, so the record cannot rebuild this board -- keep the "
                "cache as it stands, or remove that item's cached files and rebuild to "
                "reproduce it without the unrecorded events",
                code=CODE_CREW_LOG_INCOMPLETE,
            )
        if cached_events < MAX_EVENTS_PER_ITEM and cached_events > len(folded_list):
            raise WorkLedgerError(
                f"item {item_id} has {cached_events} cached events but the crew log "
                "folds fewer; a unit was pruned, so the record cannot rebuild this board "
                "-- keep the cache as it stands, or remove that item's cached files and "
                "rebuild to reproduce it without the unrecorded events",
                code=CODE_CREW_LOG_INCOMPLETE,
            )


def mark_cache_dirty(slot_key: str, reason: str) -> None:
    """Flag *slot_key*'s cache as possibly holding what the record does not.

    Written when an undo failed: an unrecorded write that could not be put back,
    or a rebuild that failed part way and could not be restored. Every read and
    write of the board refuses ``cache_dirty`` while the flag stands; a rebuild
    that completes clears it, because the cache is then the record again.
    """
    directory = conductor_dir(slot_key)
    directory.mkdir(parents=True, exist_ok=True)
    atomic_write(directory / _DIRTY_FILE, reason.strip()[:200] + "\n", mode=0o600)


def cache_dirty(slot_key: str) -> "str | None":
    """The reason the cache is flagged dirty, or None when it is not."""
    path = conductor_dir(slot_key) / _DIRTY_FILE
    try:
        return path.read_text(encoding="utf-8").strip() or "unknown"
    except FileNotFoundError:
        return None
    except OSError:
        return "the dirty marker could not be read"


def clear_cache_dirty(slot_key: str) -> None:
    """Drop the flag: the cache was just rebuilt from the record."""
    try:
        (conductor_dir(slot_key) / _DIRTY_FILE).unlink()
    except FileNotFoundError:
        pass


def _bound_workers(slot_key: str) -> "tuple[str, ...]":
    """Every worker whose binding file points at *slot_key*'s board, oldest first."""
    root = bindings_dir()
    if not root.is_dir():
        return ()
    workers: list[str] = []
    for path in sorted(root.glob("*.json")):
        raw = _read_json_record(path, strict=False)
        if not isinstance(raw, dict) or _as_str(raw.get("conductor_slot_key")) != slot_key:
            continue
        worker = _as_str(raw.get("worker_slot_key"))
        if worker and worker not in workers:
            workers.append(worker)
    return tuple(workers)


def _rebuild_footprint(
    slot_key: str, present: "list[str]", fold_items: "list[dict[str, Any]]"
) -> dict[str, bytes | None]:
    """The bytes of every file :func:`rebuild_from_projection` may write or remove."""
    snapshot: dict[str, bytes | None] = {}
    paths: list[Path] = [
        conductor_dir(slot_key) / _CONDUCTOR_FILE,
        conductor_dir(slot_key) / _KEY_FILE,
    ]
    ids = set(present)
    workers: set[str] = set()
    for raw in fold_items:
        item_id = raw.get("item_id")
        if isinstance(item_id, str) and _ITEM_ID_RE.fullmatch(item_id):
            ids.add(item_id)
        worker = raw.get("worker_session_key")
        if isinstance(worker, str) and worker:
            workers.add(worker)
    for item_id in sorted(ids):
        paths += [item_path(slot_key, item_id), item_events_path(slot_key, item_id)]
    root = bindings_dir()
    if root.is_dir():
        for path in sorted(root.glob("*.json")):
            binding = _read_json_record(path, strict=False)
            if isinstance(binding, dict) and _as_str(binding.get("conductor_slot_key")) == slot_key:
                paths.append(path)
    for worker in sorted(workers):
        paths.append(binding_path(worker))
    for path in paths:
        try:
            snapshot[str(path)] = path.read_bytes()
        except FileNotFoundError:
            snapshot[str(path)] = None
    return snapshot


def _rebuild_locked(
    slot_key: str,
    header: dict[str, Any],
    fold_items: "list[dict[str, Any]]",
    present: "list[str]",
    forgotten: "_Forgotten | None" = None,
) -> dict[str, Any]:
    """The rebuild's writes, run under the locks :func:`rebuild_from_projection` holds."""
    epoch = _parse_iso(_as_str(header.get("first_entry_at")))
    record = ConductorRecord.from_dict(
        {
            k: v
            for k, v in dict(header, slot_key=slot_key).items()
            if k not in ("entries", "first_entry_at")
        }
    )
    existing = read_conductor(slot_key)
    trusted_base = False
    if existing is not None:
        # Header fields the record never saw -- a goal set before the board's
        # first entry, the true creation stamp -- are the cache's to keep.
        if not header.get("goal"):
            record = dataclasses.replace(
                record,
                goal=existing.goal,
                round=max(record.round, existing.round),
                depth=existing.depth,
                parent_item=existing.parent_item,
            )
        if _predates(existing.created_at, _parse_iso(record.created_at)):
            record = dataclasses.replace(record, created_at=existing.created_at)
        if not record.generation and existing.generation:
            record = dataclasses.replace(record, generation=existing.generation)
        if existing.recorded_at:
            record = dataclasses.replace(record, recorded_at=existing.recorded_at)
        # The goal's two caps and where its count starts ride no crew-log entry,
        # so they are the cache's to keep -- but only while the cache holds no
        # goal write the record lacks. A dirty cache, or one a goal write ahead
        # of the record, may carry a refused write's caps and count start; those
        # are dropped for the defaults below, as for a board with no cache header.
        if not cache_dirty(slot_key) and existing.goal_version == record.goal_version:
            record = dataclasses.replace(
                record,
                item_cap=existing.item_cap,
                concurrency_cap=existing.concurrency_cap,
                goal_items_base=existing.goal_items_base,
            )
            # A trusted header from before the per-goal count keeps its legacy
            # status, settled below against the rebuilt total.
            trusted_base = True
    if not record.recorded_at and record.goal_version:
        # The fold holds a goal entry, which is what the stamp asserts; a rebuild
        # that left it empty would exempt the header from the next check.
        record = dataclasses.replace(record, recorded_at=_now_iso())

    written_items = 0
    written_events = 0
    kept: set[str] = set()
    for raw in fold_items:
        item = WorkItem.from_dict({key: value for key, value in raw.items() if key != "events"})
        if not item.item_id:
            continue
        # The fold produced this item from entries the log holds whole, which is
        # exactly what ``recorded_at`` asserts. Entries never carry the stamp, so
        # a rebuild that copied the fold verbatim would clear it -- and the next
        # completeness check would skip the item, letting a later pruned unit
        # erase its reports. Keep the cache's stamp when it has one; otherwise
        # this rebuild is the moment the store confirmed the log holds it.
        cached_item = read_work_item(slot_key, item.item_id) if item.item_id in present else None
        if cached_item is not None and cached_item.recorded_at:
            item.recorded_at = cached_item.recorded_at
        else:
            item.recorded_at = _now_iso()
        parsed = (WorkEvent.from_dict(entry) for entry in raw.get("events") or ())
        events = [event for event in parsed if event is not None][-MAX_EVENTS_PER_ITEM:]
        # Every lock these writes need is held by the caller for the whole rebuild
        # AND its undo -- this item's included, whether the cache already held it
        # or the fold creates it -- so nothing is acquired or released here. A
        # second acquire of a lock this process already holds would wait on itself.
        events_path = item_events_path(slot_key, item.item_id)
        lines = (json.dumps(event.to_dict(), ensure_ascii=False) + "\n" for event in events)
        events_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(events_path, "".join(lines), mode=0o600, restrict_to_owner=True, newline="\n")
        _write_record(item_path(slot_key, item.item_id), item.to_dict())
        kept.add(item.item_id)
        written_items += 1
        written_events += len(events)

    removed = 0
    forgotten_kept: set[str] = set()
    legacy: set[str] = set()
    for item_id in present:
        if item_id in kept:
            continue
        # STRICT so a present-but-momentarily-unreadable record -- a Windows rename
        # race, a permission or disk blip -- RAISES rather than reading as absent.
        # The non-strict read read such a file as ``None`` and the loop then unlinked
        # it, turning a transient read error into real data loss. The raise unwinds
        # into ``rebuild_from_projection``'s undo, which restores every file and leaves
        # the record on disk for the next rebuild to read once the blip clears. Torn or
        # non-UTF-8 CONTENT still reads as ``None`` here (corruption reads as absent):
        # such a record is dropped below, except on a board whose fold has forgotten
        # items, where it is left in place -- only an I/O error is treated as
        # "present, read later".
        cached = read_work_item(slot_key, item_id, strict=True)
        # Legacy means the log never held the record's create: an UNRECORDED record
        # from before the board's first recorded entry. Stamp order alone cannot
        # decide it. A closed legacy record is terminal too, so the forgotten test
        # would claim it and the total would lose it; and the board's first item
        # shares its second with the epoch, so the stamp test would call that
        # RECORDED item legacy and the total would count it twice (once inside the
        # fold's creates, once as legacy). ``recorded_at`` tells the two apart.
        if cached is not None and not cached.recorded_at and _predates(cached.created_at, epoch):
            if forgotten is not None and item_id in forgotten.baseline_ids:
                # The fold folded its baseline and then let it go: its stamp failed
                # after the baseline landed, so the record still reads unrecorded. The
                # fold's total already counts it, so it is kept and counted once.
                forgotten_kept.add(item_id)
                continue
            # Created before this board's first recorded entry and never recorded:
            # the log never saw it, so the log cannot judge it. It stays as the
            # cache holds it and is ADDED to the total below.
            legacy.add(item_id)
            continue
        if cached is not None and _predates(cached.created_at, epoch):
            # RECORDED but older than the board's first surviving entry: retention
            # pruned the unit holding its create. The log cannot judge it, so it is
            # kept as the cache holds it -- never unlinked -- and counted like a
            # forgotten record, once, in the fold's total.
            forgotten_kept.add(item_id)
            continue
        if cached is not None and _may_be_forgotten(cached, forgotten):
            # A closed record the fold does not hold: the cache is where it stays
            # whole, so it is kept as it is, and counted in the fold's total below.
            forgotten_kept.add(item_id)
            continue
        if cached is None and forgotten is not None and forgotten.items > 0:
            # A record whose content does not parse, on a board whose fold has let
            # closed items go. It may be one of those forgotten items, whose only
            # whole copy -- record and events -- is the cache, so the rebuild leaves
            # both files where they are rather than judge it. Every reader already
            # reads it as absent, so keeping it changes nothing anyone sees, and it
            # is not counted: the fold's total covers its create if the log held one.
            continue
        item_path(slot_key, item_id).unlink(missing_ok=True)
        item_events_path(slot_key, item_id).unlink(missing_ok=True)
        removed += 1
    # The create counter is set from the record, not carried over from the cache's
    # header: every item the fold held -- still held or forgotten once closed -- is a
    # create the log holds, and every legacy record kept is one the log may yet hold
    # whole (its first recorded mutation carries it as a baseline). Nothing else the
    # fold reports counts: a non-zero ``omitted`` counts entries that are not this
    # board's creates (stragglers of a purged board, parked lines).
    # Defaults for caps the cache could not vouch for: 6 open, a line at 20,
    # counted from the board's first create -- the side that tells the user
    # sooner, never later. A
    # kept count start past the new total is clamped so the count cannot go negative.
    # The fold counts every item it held, forgotten closed ones included, so the
    # board's create count comes from it rather than from what it still holds.
    # A kept forgotten record is one of the fold's own creates, so it is counted once,
    # in the fold's total; a legacy record never reached the fold and is added.
    held = len(kept) + len(forgotten_kept)
    total = max(held, forgotten.created if forgotten else 0) + len(legacy)
    if trusted_base and existing is not None:
        # The store only ever adds to the counter, and the pieces above can overlap
        # without one containing the other (a pruned unit's create, a torn forgotten
        # record), so they can come out short. A trusted header's count is a floor:
        # the rebuild may raise the spend count, never lower it below what was spent.
        total = max(total, existing.created_total)
    if not trusted_base:
        base = 0
    elif record.goal_items_base == LEGACY_GOAL_BASE:
        base = total
    else:
        base = min(record.goal_items_base, total)
    record = dataclasses.replace(record, created_total=total, goal_items_base=base)
    _write_record(conductor_dir(slot_key) / _CONDUCTOR_FILE, record.to_dict())
    # The identity breadcrumb beside the record, as the bootstrap writes it, so a
    # cache rebuilt into an empty directory carries the same two files a new one does.
    atomic_write(conductor_dir(slot_key) / _KEY_FILE, slot_key + "\n", mode=0o600)
    # A kept forgotten record is an item the fold does not hold, so like a legacy one
    # its binding is kept where it points.
    _reconcile_bindings(slot_key, fold_items, kept, legacy | forgotten_kept)
    return {
        "slot_key": slot_key,
        "items": written_items,
        "events": written_events,
        "removed": removed,
        "legacy": len(legacy),
    }


def _predates(created_at: str, epoch: "datetime | None") -> bool:
    """Whether an item stamped *created_at* was made before the board's first entry."""
    if epoch is None:
        return False
    created = _parse_iso(created_at)
    if created is None or (created.tzinfo is None) != (epoch.tzinfo is None):
        return False
    # Stamps are second-resolution, so an item in the SAME second as the first
    # entry cannot be told from one made just before it: it is kept, not judged.
    return created <= epoch


def _reconcile_bindings(
    slot_key: str, folded_items: Any, kept: "set[str]", legacy: "set[str]"
) -> None:
    """Bindings under ``work-ledger/bindings/`` match the recorded board.

    Every recorded OPEN item bound to a worker has that worker's binding pointing
    at it, unless another board holds the worker now; a binding that points at
    this board's item which the record does not bind to that worker is removed.
    A terminal item still names the worker that finished it but does not route
    it (the store lets another board bind a worker whose item is terminal), so its
    binding is kept where it points and never claimed back. Items in *legacy* --
    legacy records and kept forgotten ones, which the fold does not hold -- keep
    theirs.
    """
    wanted: dict[str, str] = {}
    live: set[str] = set()
    for raw in folded_items:
        if not isinstance(raw, dict):
            continue
        worker = raw.get("worker_session_key")
        item_id = raw.get("item_id")
        if isinstance(worker, str) and worker and isinstance(item_id, str) and item_id in kept:
            wanted[worker] = item_id
            if raw.get("state") not in TERMINAL_ITEM_STATES:
                live.add(worker)
    for worker, item_id in wanted.items():
        if worker not in live:
            # A terminal item names the worker that finished it, but it does not
            # route that worker: once the item is terminal the store lets another
            # board bind the worker. A binding still pointing here is kept (below);
            # one that has moved on is not claimed back.
            continue
        with binding_lock(worker):
            current = read_binding(worker)
            if current is not None and current[0] != slot_key:
                # Another board holds this worker now. The store let it bind because
                # this board's item was terminal or unreadable at the time; writing
                # over it would route that board's worker to this one's item.
                continue
            if current != (slot_key, item_id):
                _write_binding(worker, slot_key, item_id)
    root = bindings_dir()
    if not root.is_dir():
        return
    for path in sorted(root.glob("*.json")):
        raw = _read_json_record(path, strict=False)
        if not isinstance(raw, dict):
            continue
        worker = _as_str(raw.get("worker_slot_key"))
        bound_conductor = _as_str(raw.get("conductor_slot_key"))
        bound_item = _as_str(raw.get("item_id"))
        if not worker or bound_conductor != slot_key:
            continue
        if bound_item in legacy or wanted.get(worker) == bound_item:
            continue
        with binding_lock(worker):
            # Re-read under the lock: another board may have rebound this worker
            # since the scan, and its current binding is not ours to remove.
            if read_binding(worker) == (slot_key, bound_item):
                binding_path(worker).unlink(missing_ok=True)


def snapshot_for_write(
    slot_key: str, *, item_id: str | None = None, worker_session_key: str | None = None
) -> dict[str, bytes | None]:
    """The bytes of every ledger file one write can touch, keyed by path.

    Taken by a route BEFORE it asks the store to write, under the board's lock,
    so that a write whose crew-log entry is then not confirmed can be undone
    exactly: the conductor record, the named item's record and event log, and
    the named worker's binding. ``None`` marks a file that did not exist, so the
    restore removes it. A write can create only one item and touch only the
    files named here, so the snapshot is complete for every action.
    """
    paths = [conductor_dir(slot_key) / _CONDUCTOR_FILE, conductor_dir(slot_key) / _KEY_FILE]
    if item_id:
        paths += [item_path(slot_key, item_id), item_events_path(slot_key, item_id)]
    if isinstance(worker_session_key, str) and worker_session_key:
        # The same spelling `bind` stores under: `_require_text` keeps the key as
        # given, so the path is derived from the request's key unchanged.
        paths.append(binding_path(worker_session_key))
    snapshot: dict[str, bytes | None] = {}
    for path in paths:
        try:
            snapshot[str(path)] = path.read_bytes()
        except FileNotFoundError:
            snapshot[str(path)] = None
    return snapshot


def current_bytes(snapshot: dict[str, bytes | None]) -> dict[str, bytes | None]:
    """What the files *snapshot* names hold NOW, keyed the way it keys them.

    Read by a route between its store commit and its undo, so the undo can tell
    the bytes its own write left behind from bytes a second gateway on the same
    store has since committed. ``None`` marks a file that is absent.
    """
    return {raw: _read_or_none(Path(raw)) for raw in snapshot}


def _read_or_none(path: Path) -> bytes | None:
    """The file's bytes, or None when it is absent or cannot be read.

    Total on purpose: an unreadable file reads as None on both sides of the
    comparison below, which leaves the plain restore in place rather than
    abandoning an undo over a file the process cannot open.
    """
    try:
        return path.read_bytes()
    except OSError:
        return None


def _written_by_another(raw: str, expected: dict[str, bytes | None] | None) -> bool:
    """True when *raw* holds neither the bytes the undoing write left nor nothing.

    The store's per-file locks serialise writers, so they cannot tear a record,
    but they do not order them: a second gateway that commits between this
    write's commit and its undo holds a record these older bytes have no claim
    on. Comparing against what this write left is what tells the two apart. A
    path *expected* does not name counts as this write's own, which keeps a
    caller that passes nothing on the plain restore.
    """
    if expected is None or raw not in expected:
        return False
    return _read_or_none(Path(raw)) != expected[raw]


def restore_snapshot(
    slot_key: str,
    snapshot: dict[str, bytes | None],
    *,
    created_item: str | None = None,
    item_id: str | None = None,
    worker_session_key: str | None = None,
    expected: dict[str, bytes | None] | None = None,
) -> None:
    """Put every file in *snapshot* back as it was, and remove a created item.

    The undo for a write whose crew-log entry was not confirmed: the cache must
    not keep a mutation the record never saw, whatever the board's history --
    a board from before the projection is undone the same way as any other,
    because this needs no fold, only the bytes the write replaced.

    Holds every lock the writers of these files take, in the module's lock
    order: the conductor lock, then each named item's lock, then the worker's
    binding lock. Pass the SAME ``item_id`` and ``worker_session_key`` the
    snapshot was taken with. The restore rewrites an existing item's record and
    event log and a worker's binding, and the route that took the snapshot has
    released those locks by the time this runs, so without them a writer
    admitted in the gap has its record replaced mid-write -- a torn record on
    POSIX, a refused open on Windows. The dashboard's board lock does not close
    that gap: it serialises one process, and these file locks are what a second
    gateway on the same store obeys.

    Locks bound the tearing, not the ordering: a writer that COMPLETES in that
    gap holds bytes this undo has no claim on. Pass *expected* -- what this
    write left in each of these files, read by the caller between its commit and
    this undo -- and any file holding something else is left alone and the cache
    flagged dirty, so the rebuild that reconciles the two is triggered rather
    than merely available. Called without *expected*, the older bytes win, which
    leaves the cache behind the record rather than ahead of it -- the direction a
    rebuild converges.

    The conductor lock is taken NON-CREATING, and a missing one ends the undo
    having written nothing: a store purged between the write and here holds no
    mutation to take back, and creating its directory to hold a lock would
    rebuild the board an operator deleted. Under that hold the store provably
    exists -- the purge takes the same lock -- so the item locks below may
    create their files, the reasoning ``rebuild_from_crew_log`` uses.
    """
    with ExitStack() as held:
        try:
            held.enter_context(conductor_lock(slot_key, create=False))
        except FileNotFoundError:
            return
        created = created_item if created_item and _ITEM_ID_RE.fullmatch(created_item) else None
        named = {created} if created else set()
        if item_id and _ITEM_ID_RE.fullmatch(item_id):
            named.add(item_id)
        for locked_item in sorted(named):
            # Sorted, the order every other multi-item hold here uses, so this
            # cannot deadlock against one of those. Deduplicated because a
            # create names the same id twice, and two acquires of one lock file
            # from one process wait on each other forever.
            held.enter_context(item_lock(slot_key, locked_item, create=True))
        if isinstance(worker_session_key, str) and worker_session_key:
            held.enter_context(binding_lock(worker_session_key))
        passed_over: list[str] = []
        if created:
            for path in (item_path(slot_key, created), item_events_path(slot_key, created)):
                # A create becomes visible to the other gateway the moment it
                # commits, so its files can carry that gateway's later write.
                if _written_by_another(str(path), expected):
                    passed_over.append(str(path))
                    continue
                path.unlink(missing_ok=True)
        passed_over += _restore_files(snapshot, expected)
        if passed_over:
            mark_cache_dirty(
                slot_key,
                "an unrecorded write could not be undone whole: another writer holds "
                f"{len(passed_over)} of its files, so a rebuild reconciles the two",
            )


def _restore_files(
    snapshot: dict[str, bytes | None], expected: dict[str, bytes | None] | None = None
) -> list[str]:
    """Write every snapshotted file back, removing those that did not exist.

    A file whose directory has gone is SKIPPED, not recreated: every snapshotted
    file that held bytes had its directory at snapshot time too, so a missing one
    means the store was removed afterwards, and rebuilding the tree around it
    would resurrect a purged board instead of undoing a write.

    A file holding bytes *expected* does not account for is skipped as well, and
    its path returned: another writer committed after the write being undone, and
    replacing its record with these older bytes would drop an acknowledged write.
    """
    passed_over: list[str] = []
    for raw, data in snapshot.items():
        path = Path(raw)
        if _written_by_another(raw, expected):
            passed_over.append(raw)
            continue
        if data is None:
            path.unlink(missing_ok=True)
            continue
        if not path.parent.is_dir():
            continue
        atomic_write(path, data, mode=0o600, restrict_to_owner=True)
    return passed_over


def mark_goal_recorded(slot_key: str) -> None:
    """Stamp the header as held by the crew log, so a rebuild checks its goal writes.

    Called by the write route once a ``goal`` entry has landed. A failure here is
    harmless: the header is checked from the first stamp that does land.
    """
    with _existing_conductor_lock(slot_key):
        record = read_conductor(slot_key)
        if record is None or record.recorded_at:
            return
        record.recorded_at = _now_iso()
        _write_record(conductor_dir(slot_key) / _CONDUCTOR_FILE, record.to_dict())


def mark_item_recorded(slot_key: str, item_id: str) -> None:
    """Stamp *item_id* as held whole by the crew log, so later entries carry deltas only.

    Called by a write route once the entry that carried the whole item (a create,
    or a baseline) has landed. A failure here is harmless: the next mutation
    simply carries the whole item again.
    """
    checked_id = _require_item_id(item_id)
    with item_lock(slot_key, checked_id, create=False):
        item = read_work_item(slot_key, checked_id)
        if item is None or item.recorded_at:
            return
        item.recorded_at = _now_iso()
        _write_record(item_path(slot_key, checked_id), item.to_dict())


# The owners hold the store's layers below the projection rebuild above. Every name they
# define is bound here too, so the code above reads them as globals of this module, and
# ``kiro_crew.work_ledger.<name>`` imports and patches each one:
# :mod:`kiro_crew._facade_mirror` carries a patch through this module to every owner that
# binds the name, and serves the imports only the owners read.
import importlib as _importlib  # noqa: E402
import sys as _sys  # noqa: E402
from typing import TYPE_CHECKING  # noqa: E402

from kiro_crew import _facade_mirror  # noqa: E402
from kiro_crew.work_ledger_purge import (  # noqa: E402, F401
    _IDENTITY_FILES,
    ItemCensus,
    _hold_every_item_lock,
    _newest_activity,
    _purge_conductor_locked,
    _remove_contents_locked,
    _remove_lock_shell,
    _unlink_item_locks_in_hold,
    census_items,
    header_damage,
    purge_conductor,
)
from kiro_crew.work_ledger_records import (  # noqa: E402, F401
    ACCEPTANCE_KINDS,
    ACCEPTANCE_PLACEHOLDERS,
    ACCEPTANCE_READ_FIELDS,
    BLOCKED_REASONS,
    CODE_ALREADY_BOUND,
    CODE_CACHE_DIRTY,
    CODE_CREW_LOG_INCOMPLETE,
    CODE_DEPTH_EXCEEDED,
    CODE_FIELD_TOO_LONG,
    CODE_GOAL_CONCURRENCY_CAP_REACHED,
    CODE_INVALID_ACTION,
    CODE_INVALID_STATUS,
    CODE_INVALID_VALUE,
    CODE_ITEM_CAP_EXCEEDED,
    CODE_ITEM_CLOSED,
    CODE_LEDGER_NOT_FINISHED,
    CODE_NO_LEDGER,
    CODE_UNKNOWN_ITEM,
    CONDUCTOR_ACTIONS,
    DEFAULT_GOAL_CONCURRENCY_CAP,
    DEFAULT_GOAL_ITEM_CAP,
    DEFAULT_STALE_WINDOW_SECS,
    EVENT_KINDS,
    ITEM_STATES,
    LEGACY_GOAL_BASE,
    MAX_ARTIFACT_KEY_CHARS,
    MAX_ARTIFACT_KEYS,
    MAX_ARTIFACT_VALUE_CHARS,
    MAX_DECISION_CHARS,
    MAX_DEPTH,
    MAX_EVENT_TEXT_CHARS,
    MAX_EVENTS_PER_ITEM,
    MAX_GOAL_CHARS,
    MAX_GOAL_CONCURRENCY_CAP,
    MAX_GOAL_ITEM_CAP,
    MAX_ITEMS_PER_CONDUCTOR,
    MAX_PR,
    MAX_RECORD_BYTES,
    MAX_SUMMARY_CHARS,
    MAX_TITLE_CHARS,
    MIN_PR,
    PENDING_BUDGET_SECS,
    SCHEMA_VERSION,
    STALE_ELIGIBLE_STATUSES,
    TERMINAL_ITEM_STATES,
    VERDICTS,
    WAITING_STATUSES,
    WORKER_STATUSES,
    ConductorRecord,
    WorkEvent,
    WorkItem,
    WorkLedgerError,
    _as_int,
    _as_opt_str,
    _as_str,
    _finite_int,
    _now_iso,
    _settle_goal_base,
    _stored_concurrency_cap,
    _stored_item_cap,
    logger,
)
from kiro_crew.work_ledger_rules import (  # noqa: E402, F401
    _decided_after_report,
    _is_placeholder,
    _parse_iso,
    _require_acceptance,
    _require_artifacts,
    _require_choice,
    _require_count,
    _require_pr,
    _require_text,
    _worker_owns_next_move,
    child_depth,
    depth_cap_hint,
    is_acceptance_concrete,
    is_orphaned,
    is_pending_overdue,
    is_stale,
    max_depth,
    may_conduct,
    parse_stamp,
)
from kiro_crew.work_ledger_store import (  # noqa: E402, F401
    _BINDINGS_DIR,
    _CONDUCTOR_FILE,
    _DIRTY_FILE,
    _ITEM_ID_RE,
    _ITEMS_DIR,
    _KEY_FILE,
    _LOCK_FILE,
    DIRTY_FILE,
    _append_event_locked,
    _coalesce_progress,
    _commit_item_locked,
    _existing_conductor_lock,
    _item_lock_path,
    _open_lock,
    _read_events_unlocked,
    _read_json_record,
    _read_text_or_none,
    _recreate_item_lock_file,
    _require_item_id,
    _require_record_fits,
    _restore_text,
    _serialize,
    _slot_key_is_shaped,
    _stored_item_ids,
    _work_ledger_root,
    _write_record,
    binding_lock,
    binding_path,
    bindings_dir,
    conductor_dir,
    conductor_lock,
    event_id,
    item_events_path,
    item_lock,
    item_path,
    items_dir,
    list_work_items,
    mint_item_id,
    read_conductor,
    read_events,
    read_work_item,
)
from kiro_crew.work_ledger_writes import (  # noqa: E402, F401
    _create_item,
    _header_under_lock,
    _read_binding_text,
    _refuse_if_worker_holds_open_item,
    _restore_binding_text,
    _write_binding,
    _write_goal,
    _write_item_action,
    accept_batch,
    apply_acceptance_update,
    apply_conductor_action,
    apply_worker_report,
    ensure_conductor,
    read_binding,
    read_work_brief,
)

#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.work_ledger_records",
    "kiro_crew.work_ledger_store",
    "kiro_crew.work_ledger_rules",
    "kiro_crew.work_ledger_writes",
    "kiro_crew.work_ledger_purge",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name this module serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: the public names of the one module, served ones included.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))

if TYPE_CHECKING:  # the project names this module imported that only its owners read
    from kiro_crew.atomic_write import read_bytes_with_retry  # noqa: F401
    from kiro_crew.config.paths import data_home  # noqa: F401
    from kiro_crew.platform_compat import (  # noqa: F401
        file_lock,
        open_lock_file,
        release_lock,
        try_acquire_lock,
    )
    from kiro_crew.session_ledger import (  # noqa: F401
        _store_name,
        is_link,
        require_lock_inode,
        resolved_within,
        unlink_lock_in_hold,
    )
    from kiro_crew.work_vocab import (  # noqa: F401
        WORK_BLOCKED_REASONS,
        WORK_ITEM_STATES,
        WORK_MAX_DEPTH_DEFAULT,
        WORK_MAX_DEPTH_MAX,
        WORK_MAX_DEPTH_MIN,
        WORK_MAX_DEPTH_SETTING,
        WORK_REASON_STATUSES,
        WORK_STORED_ITEM_LIMIT,
        WORK_VERDICTS,
        WORK_WORKER_STATUSES,
        work_event_id,
    )
