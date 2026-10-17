"""The ``work`` slot fold: the conductor work board (:mod:`kiro_crew.work_vocab`), keyed by
the conductor's slot, and which units that board spans.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final, cast

# Reads the work-unit listing (``work_crew_log_units``) from ``session_ledger``.
from kiro_crew import session_ledger
from kiro_crew.crew_log.entry_types import WORK_ENTRY_TYPE
from kiro_crew.crew_log.projection_catalogue import KNOWN_TYPES
from kiro_crew.crew_log.projection_ledger import _ledger_iso
from kiro_crew.crew_log.projection_session_log import open_session_log
from kiro_crew.crew_log.projection_values import TEXT_LIMIT, _as_id, _as_int, _as_str
from kiro_crew.crew_log.schema import Entry
from kiro_crew.crew_log.store import session_units_by_slot
from kiro_crew.work_vocab import (
    WORK_BLOCKED_REASONS,
    WORK_CLOSED_WINDOW,
    WORK_CONDUCTOR_FIELDS,
    WORK_STORED_ITEM_LIMIT,
    WORK_TOMBSTONE_LIMIT,
    WorkBoardForgotten,
    WorkBoardItem,
    WorkBoardView,
)

# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# work -- the conductor work board, keyed by the conductor's slot
# --------------------------------------------------------------------------- #


def _work_units(
    slot: str,
    conductor_units: Sequence[str],
    *,
    limit: int | None = None,
    on_drop: "Callable[[int], None] | None" = None,
) -> "tuple[str, ...]":
    """The conductor's units followed by every bound worker's, oldest first.

    *limit* caps what is RETAINED and stops the bind scan once reached, for a caller
    whose own bound covers this list. It is optional because the two callers differ:
    the ``work`` fold reads one board and is bounded by that, while ``workstreams``
    follows nested boards and so states its own ceiling. Without it a bounded caller
    still pays an unbounded peak here -- the whole listing lands in a list before the
    caller can measure it, which is the bound describing a state the list already left.

    A worker's report is appended to the WORKER's log, and that log's header names
    the worker's own slot, so the header index alone never reaches it. The
    conductor's ``bind`` entries carry ``worker_session_key``: each names a slot whose
    units join the fold after the conductor's. A worker bound to several boards
    carries the conductor's ``slot`` on every entry, and the fold keeps only the
    entries naming this board, so the extra units add nothing that is not this
    board's.
    """
    if limit is not None and limit <= 0:
        return ()
    # Bounded BEFORE the logs are opened: the bind scan below reads every entry of
    # every conductor unit, so a cap applied to its output would still pay to read
    # units it then discards.
    #
    # THE NEWEST ARE KEPT. These are oldest-first, so taking the head keeps a
    # long-lived crewmate's oldest work and drops what it is doing now -- a dashboard
    # showing finished boards and hiding the live one. The tail is what a reader came
    # for, and the fold order below is unchanged: this selects which units, not the
    # order they are folded in.
    if limit is not None:
        listed = list(conductor_units)
        if on_drop is not None and len(listed) > limit:
            on_drop(len(listed) - limit)
        conductor_units = listed[-limit:]
    workers: list[str] = []
    seen: set[str] = set()
    for unit_id in conductor_units:
        handle = open_session_log(unit_id)
        if handle is None:
            continue
        for entry in handle.iter_from(1, known=KNOWN_TYPES):
            if entry.type != WORK_ENTRY_TYPE:
                continue
            data = entry.data
            if _as_str(data.get("slot")) != slot:
                continue
            # A BASELINE names its worker too, and it is the only record of that
            # binding once the ``bind`` entry's unit is pruned: a board rebuilt from
            # baselines has the worker in every item and no `bind` action anywhere, so
            # a scan for the action alone collects none of their units and every task
            # on that board reports a cost nobody can be charged for.
            if data.get("action") != "bind" and data.get("baseline") is not True:
                continue
            worker = data.get("worker_session_key")
            if isinstance(worker, str) and worker and worker not in seen:
                seen.add(worker)
                workers.append(worker)
    units = list(conductor_units)
    known = set(units)
    for worker in workers:
        for unit_id in session_ledger.work_crew_log_units(worker):
            if unit_id in known:
                continue
            if limit is not None and len(units) >= limit:
                # Counted, not just stopped: a worker's units refused here are boards
                # and spend the snapshot will not carry, and a reader told nothing
                # reads the short answer as the whole one.
                if on_drop is not None:
                    on_drop(1)
                continue
            known.add(unit_id)
            units.append(unit_id)
    return tuple(units)


def work_slots_naming_board(slot: str) -> "tuple[str, ...]":
    """Every OTHER slot whose session units carry a ``work/recorded`` entry for *slot*.

    The third way a board's workers are found, and the one that needs nothing but
    the record. The fold reaches a worker through the conductor's recorded ``bind``,
    and a rebuild also reaches it through the cached binding file; a worker bound
    before the board was recorded has neither once the cache is lost, yet its own
    log holds the baseline report that names the board. A rebuild that searched
    only the first two would rebuild the board without that worker's item.

    A whole-log walk, so it is for a rebuild (an operator's request), not for the
    per-read fold: every other slot's units are opened, and each is left as soon as
    one entry names the board. Ordered by slot name so the result is stable.
    """
    if not slot:
        return ()
    found: list[str] = []
    for other, unit_ids in sorted(session_units_by_slot().items()):
        if other == slot:
            continue
        for unit_id in unit_ids:
            handle = open_session_log(unit_id)
            if handle is None:
                continue
            names_board = False
            for entry in handle.iter_from(1, known=KNOWN_TYPES):
                if entry.type == WORK_ENTRY_TYPE and _as_str(entry.data.get("slot")) == slot:
                    names_board = True
                    break
            if names_board:
                found.append(other)
                break
    return tuple(found)


#: Item records the fold may hold per board at once -- the fold's own memory bound.
#: The fold FORGETS a closed item once :data:`WORK_CLOSED_WINDOW` newer closed items
#: sit behind it (:func:`_work_settle`), so during a replay it holds the items open at
#: that point in the conductor's log (the writer caps those at
#: ``work_ledger.MAX_ITEMS_PER_CONDUCTOR``, 32) plus that window, never the board's
#: lifetime total. Creates and closes interleave in the conductor's own units, which
#: fold first and in order, so the peak is bounded by open fan-out, not by history.
#: This ceiling therefore guards only a log the writer did not produce: a create past
#: it is counted in ``omitted``, and ``work_ledger.rebuild_from_projection`` refuses a
#: full fold that reports omissions, because only such a fold may be a prefix of the
#: board.
WORK_ITEM_LIMIT: Final[int] = WORK_STORED_ITEM_LIMIT

#: The states an item closes in; the writer's ``TERMINAL_ITEM_STATES``.
_WORK_CLOSED_STATES: Final[tuple[str, ...]] = ("accepted", "rejected", "abandoned")

#: Newest event lines kept per item, the same tail the stored ledger kept.
WORK_EVENT_LIMIT: Final[int] = 200

#: Entries parked per item while its ``create`` is still in a unit not yet folded.
WORK_PARKED_LIMIT: Final[int] = 64

#: Longest event text a line carries, the store's own excerpt bound.
WORK_EVENT_TEXT_LIMIT: Final[int] = 500

#: Fields a conductor entry may set on an item, by action; a worker's are fixed.
#: One table, shared with the write route through ``kiro_crew.work_vocab``.
_WORK_CONDUCTOR_FIELDS: Final[dict[str, tuple[str, ...]]] = WORK_CONDUCTOR_FIELDS
_WORK_WORKER_FIELDS: Final[tuple[str, ...]] = ("status", "summary", "artifacts", "pr")
#: Every item field a baseline entry may carry: the conductor's and the worker's.
_WORK_BASELINE_FIELDS: Final[tuple[str, ...]] = (
    "title",
    "acceptance",
    "state",
    "verdict",
    "decision",
    "worker_session_key",
    "round",
    "fails",
    "status",
    "reason",
    "summary",
    "artifacts",
    "pr",
)


def _work_iso(stamp_ms: int) -> str:
    """An entry's epoch-millisecond ``time`` in the ledger's timestamp spelling.

    Work and session ledgers read the same untrusted envelope field, so they use
    one conversion policy: local time with offset and seconds precision, or the
    empty string when the platform cannot represent the value.
    """
    return _ledger_iso(stamp_ms)


def _work_stamp(committed: Any, stamp_ms: int) -> str:
    """The committed stamp an entry carries, else the entry's own append time."""
    if isinstance(committed, str) and committed:
        return committed[:TEXT_LIMIT]
    return _work_iso(stamp_ms)


def _work_start() -> dict[str, Any]:
    return {
        "slot": "",
        "goal": "",
        "round": 0,
        "goal_version": 0,
        "depth": 0,
        "parent_item": None,
        "created_at": "",
        "items": {},
        "order": [],
        "parked": {},
        "omitted": 0,
        "entries": 0,
        "first_entry_at": "",
        "last_entry_at": "",
        # The epoch behind ``last_entry_at``, kept so the greatest stamp can be chosen
        # by TIME rather than by spelling. ``_work_iso`` renders local time with an
        # offset, so a plain string comparison is only accidentally ordered: at an
        # autumn DST change the offset shrinks and a later entry spells an earlier
        # string, which is exactly the inversion this field exists to refuse. Not
        # rendered -- ``_work_render`` serves ``last_entry_at`` -- so it costs a reader
        # nothing. A checkpoint written before this key existed differs from this shape
        # and ``_state_matches_fold`` discards it, so no resumed state reads it absent.
        "last_entry_ms": 0,
        "generation": "",
        # Closed items still held, oldest close first; the oldest past
        # ``WORK_CLOSED_WINDOW`` is forgotten (``_work_settle``).
        "closed": [],
        # Ids of forgotten items, oldest first, as a dict for O(1) membership; capped
        # at ``WORK_TOMBSTONE_LIMIT``. An entry naming one is dropped, never parked.
        "tombstones": {},
        "forgotten": {
            "created": 0,
            "items": 0,
            "accepted": 0,
            "rejected": 0,
            "abandoned": 0,
            # Forgotten items created in the board's current round, so the board's
            # "added this round" count still covers them. Reset when the round moves.
            "in_round": 0,
        },
    }


def _work_new_item(item_id: str, stamp_ms: int) -> dict[str, Any]:
    return {
        "item_id": item_id,
        "title": "",
        "acceptance": {},
        "state": "open",
        "verdict": None,
        "decision": "",
        "worker_session_key": None,
        "round": 0,
        "fails": 0,
        "status": None,
        "reason": None,
        "summary": "",
        "artifacts": {},
        "pr": None,
        "last_report_at": None,
        "created_at": _work_iso(stamp_ms),
        "closed_at": None,
        "decided_at": None,
        "pending_since": None,
        "events": [],
    }


def _work_reset_board(state: dict[str, Any]) -> None:
    """A new board generation under the same slot: the earlier board's items,
    header, parked entries and accumulators are dropped; the slot binding is kept.

    ``entries`` and ``first_entry_at`` belong to the BOARD, not to the fold: they are
    rendered as that board's metadata, and ``first_entry_at`` is the epoch that tells
    an item created before the board's first recorded entry from one created after.
    Carrying them across a reset gives the new board the old one's entry count and
    the old one's epoch, which classifies its own items as predating it.
    ``generation`` is excluded because the caller assigns the new one immediately.
    """
    fresh = _work_start()
    for key, value in fresh.items():
        if key not in ("slot", "generation"):
            state[key] = value


def _work_bind_slot(state: dict[str, Any], slot: str) -> None:
    """The board this fold is of, as the reader names it, before the first entry."""
    state["slot"] = slot


def _work_step(state: dict[str, Any], entry: Entry) -> None:
    if entry.type != WORK_ENTRY_TYPE:
        return
    data = entry.data
    if not state["slot"]:
        # Unbound (a caller that folded units without naming the board): only a
        # conductor's OWN action names its board. A report this session filed
        # as a worker names its parent's board and must not pick the fold's.
        if data.get("actor") != "conductor":
            return
        state["slot"] = _as_str(data.get("slot"))
    elif _as_str(data.get("slot")) != state["slot"]:
        # A bound worker's log, or a nested conductor's, carries another
        # board's entries; only this board's fold here.
        return
    generation = _as_str(data.get("generation"))
    if not generation and state["generation"]:
        # A stamped board is live, so an entry carrying NO generation cannot be its:
        # the stamp is minted with the board and every entry of a stamped board
        # carries it. It is a straggler from an earlier board under this slot --
        # purged, or dropped by the reset below -- and the log is append-only, so it
        # outlives that board forever. Applied, it silently resurrects that board's
        # items on every fold, and the rebuild cannot refuse it either: that guard
        # compares two generations and needs both non-empty. The mismatch branch
        # below cannot catch this one, being reached only when the entry HAS a
        # generation to disagree with. A board from before the stamp existed keeps
        # working: it never adopts a generation, so `state["generation"]` stays
        # empty and its own generationless entries still apply.
        state["omitted"] += 1
        return
    if generation and generation != state["generation"]:
        # Generations are opaque ids minted when a board's record is created, so
        # they transition in LOG order, never by comparing them: the conductor's
        # units fold first and in sequence, so a conductor entry with a new id is
        # the next board under this slot and the earlier board is dropped; a
        # worker entry whose id is not the current board's is a straggler from
        # a purged board and is omitted.
        if data.get("actor") != "conductor":
            if state["generation"] or state["entries"]:
                state["omitted"] += 1
                return
            # Nothing folded yet and the first entry is a worker's: a board from
            # before the projection whose first recorded write is a report. Its
            # generation is the board's; adopt it.
        elif state["entries"]:
            # Whatever came before -- a generation-stamped board or one from
            # before the stamp existed -- was the earlier board; it is dropped.
            _work_reset_board(state)
        state["generation"] = generation
    state["entries"] += 1
    if not state["first_entry_at"]:
        state["first_entry_at"] = _work_iso(entry.time)
    # HERE, where an entry is accepted, rather than beside any one action: this is the
    # answer to "how old is this board's information", and a reader asking that must
    # not get a different answer depending on which kind of entry came last. Taken from
    # an item's own stamps instead, a conductor-only round -- a decision, a verdict, an
    # acceptance, a bind -- moves nothing, so a board that just changed keeps ageing and
    # eventually reads as stale while it is in fact current.
    #
    # The GREATEST accepted stamp, not the last one written, because the fold order is
    # by UNIT and never by time: ``_work_units`` yields the conductor's units first and
    # then each bound worker's, so a worker report appended before the conductor's
    # latest round is folded after it. An unconditional write hands the board that older
    # stamp, the age inflates to the gap between the two, and a current board reads as
    # stale -- reproducibly, since the log order never changes. Scoped to the current
    # board generation for free: the reset above runs first and clears both keys, so a
    # purged board's newest stamp cannot pin a board born after it.
    if entry.time >= state["last_entry_ms"]:
        state["last_entry_ms"] = entry.time
        state["last_entry_at"] = _work_iso(entry.time)
    if not state["created_at"] and data.get("actor") == "conductor":
        state["created_at"] = _work_iso(entry.time)
    action = _as_str(data.get("action"))
    if action == "goal":
        _work_apply_header(state, data)
        return
    item_id = _as_id(data.get("item_id"))
    if not item_id:
        state["omitted"] += 1
        return
    if action == "create":
        if item_id in state["items"]:
            state["omitted"] += 1
            return
        # The store mints an id only while no record holds it and keeps every record,
        # so it never re-mints a forgotten id; the log holds each entry once, so this is
        # not a replay either. Should a log from elsewhere carry one, it starts a fresh
        # item and the id stops being a tombstone.
        state["tombstones"].pop(item_id, None)
        if len(state["items"]) >= WORK_ITEM_LIMIT:
            state["omitted"] += 1
            return
        item = _work_new_item(item_id, entry.time)
        item["round"] = state["round"]
        item["created_at"] = _work_stamp(data.get("created_at"), entry.time)
        state["items"][item_id] = item
        state["order"].append(item_id)
        _work_apply_header(state, data)
        _work_apply(item, data, entry.time)
        # Entries seen before the create belong to a unit folded earlier than the
        # conductor's; they were kept aside and are applied now, in time order.
        for parked in sorted(state["parked"].pop(item_id, ()), key=lambda p: p[0]):
            _work_apply(item, parked[1], parked[0])
        state["forgotten"]["created"] += 1
        _work_settle(state, item)
        return
    item = state["items"].get(item_id)
    if item is None and item_id in state["tombstones"]:
        # A closed item this fold already let go. The writer refuses every write to a
        # closed item, so this entry was written BEFORE the close and is only folded
        # after it, from a unit read later (a worker's). Parking it would wait for a
        # create that already went by, so it is dropped: it could only change fields of
        # an item the fold does not render, and its history stays in the crew log.
        return
    if item is None and data.get("baseline") is True:
        # The entry carries the whole committed item (one the record never held
        # whole before): materialise it here, then apply the action as usual.
        if len(state["items"]) >= WORK_ITEM_LIMIT:
            state["omitted"] += 1
            return
        item = _work_new_item(item_id, entry.time)
        item["created_at"] = _work_stamp(data.get("created_at"), entry.time)
        for name in _WORK_BASELINE_FIELDS:
            if name in data:
                item[name] = _work_field(name, data[name])
        # The committed stamps ride along: an item with a report or a close from
        # before the projection keeps them across a rebuild.
        for name in ("last_report_at", "closed_at", "decided_at", "pending_since"):
            if isinstance(data.get(name), str) and data[name]:
                item[name] = _work_stamp(data[name], entry.time)
        # The baseline carries the board's lineage and goal too, whoever wrote it.
        _work_apply_header(state, data)
        # Rendered, so a reader knows this item's history before the baseline is
        # not in the record (its event tail starts at the baseline).
        item["baseline"] = True
        state["items"][item_id] = item
        state["order"].append(item_id)
        for parked_entry in sorted(state["parked"].pop(item_id, ()), key=lambda p: p[0]):
            _work_apply(item, parked_entry[1], parked_entry[0])
        state["forgotten"]["created"] += 1
    if item is None:
        parked = state["parked"].get(item_id)
        if parked is None:
            # A new key is retained only within the item bound: the parked map
            # can hold no more distinct items than the board itself may.
            if len(state["parked"]) >= WORK_ITEM_LIMIT:
                state["omitted"] += 1
                return
            parked = state["parked"][item_id] = []
        if len(parked) >= WORK_PARKED_LIMIT:
            state["omitted"] += 1
            return
        parked.append([entry.time, dict(data)])
        return
    if data.get("baseline") is True:
        # A baseline on an item the fold already holds: the store had not stamped
        # it recorded (a failed stamp after its create), so a LATER entry -- a
        # worker's, folded after the conductor's units -- may carry a baseline too.
        # Internal, never rendered: it only protects the item's tombstone.
        item["_unrecorded"] = True
    _work_apply(item, data, entry.time)
    _work_settle(state, item)


def _work_settle(state: dict[str, Any], item: dict[str, Any]) -> None:
    """Track *item*'s closed status and forget the oldest closed item past the window.

    A closed item does not need to live in the fold: its whole history stays in the
    crew log. Keeping every one would make the fold grow with the board's LIFETIME
    creates. So the fold keeps the newest
    :data:`WORK_CLOSED_WINDOW` closed items whole, for the surfaces that draw recent
    history, and lets older ones go: their id joins ``tombstones`` (so a late entry
    for one is dropped, not parked) and their closing state is counted in
    ``forgotten``. The writer never reopens an item; should a fold see one leave a
    closed state anyway, it simply leaves the window again.
    """
    item_id = item["item_id"]
    closed: list[str] = state["closed"]
    if item["state"] not in _WORK_CLOSED_STATES:
        if item_id in closed:
            closed.remove(item_id)
        return
    if item_id in closed:
        return
    closed.append(item_id)
    while len(closed) > WORK_CLOSED_WINDOW:
        _work_forget(state, closed.pop(0))


def _work_forget(state: dict[str, Any], item_id: str) -> None:
    """Drop *item_id* from the fold and remember only its id and closing state."""
    item = state["items"].pop(item_id, None)
    if item is None:
        return
    if item_id in state["order"]:
        state["order"].remove(item_id)
    forgotten = state["forgotten"]
    forgotten["items"] += 1
    forgotten[item["state"]] += 1
    if item["round"] >= state["round"]:
        forgotten["in_round"] += 1
    # The value says whether a later entry for the item may carry a BASELINE: one born
    # from a baseline, or one the fold saw a baseline for (its recorded stamp failed).
    # Such an entry -- a worker's, folded after the conductor's units -- would
    # materialise the item again once its id left the set. A worker writes only after
    # the conductor's bind, so a failed stamp after a create always shows on a
    # conductor entry first. Past the cap, the oldest other id goes first, and a
    # protected id goes only when every id the set holds is one.
    tombstones: dict[str, bool] = state["tombstones"]
    tombstones[item_id] = item.get("baseline") is True or item.get("_unrecorded") is True
    while len(tombstones) > WORK_TOMBSTONE_LIMIT:
        evict = next((key for key, born in tombstones.items() if born is not True), None)
        del tombstones[evict if evict is not None else next(iter(tombstones))]


def _work_set_round(state: dict[str, Any], new_round: int) -> None:
    """Set the board's round; a round that moves zeroes ``forgotten.in_round``.

    Every item forgotten so far was created in an earlier round than a new one, so
    none of them is "added this round" any more.
    """
    if new_round != state["round"]:
        state["forgotten"]["in_round"] = 0
    state["round"] = new_round


def _work_apply_header(state: dict[str, Any], data: Mapping[str, Any]) -> None:
    """The board-level fields a conductor entry may carry.

    A conductor ``goal`` entry is the authoritative source for the goal text and round,
    and it is version-stamped. A worker BASELINE carries the same fields for a board the
    record never saw set, and it is not version-stamped -- so applying it last-writer-wins
    let a stale baseline folded after a conductor goal regress the header, while
    ``goal_version`` beside it was protected by ``max()``. The fold is deterministic, so a
    regressed header is not a transient glitch: it re-derives identically on every later
    rebuild and the wrong goal is what a resume reads.

    Hence ``_from_baseline``: a baseline supplies header fields only while the record holds
    no goal write for this board at all (``goal_version`` still 0). That is what "no newer
    conductor goal has been folded" means here, and it keeps the baseline doing the one job
    it exists for -- describing a board whose header was never recorded -- without letting
    it speak over a board whose header was.
    """
    from_baseline = _as_str(data.get("action")) != "goal"
    baseline_may_set = state["goal_version"] == 0
    if isinstance(data.get("goal"), str) and (not from_baseline or baseline_may_set):
        state["goal"] = _work_text("goal", data["goal"])
    if "round" in data and data.get("action") == "goal":
        _work_set_round(state, _as_int(data.get("round")))
    if "goal_version" in data and data.get("action") == "goal":
        state["goal_version"] = max(state["goal_version"], _as_int(data.get("goal_version")))
    if "depth" in data and not state["depth"]:
        state["depth"] = _as_int(data.get("depth"))
    if isinstance(data.get("parent_item"), str) and state["parent_item"] is None:
        state["parent_item"] = _as_id(data["parent_item"])
    # A baseline carries the board's own committed round and creation stamp under
    # names of their own (``round`` on an item entry is the item's), so a board
    # whose header the record never saw set rebuilds with them rather than with a
    # zero round and the first entry's time.
    if "board_round" in data and from_baseline and baseline_may_set:
        _work_set_round(state, _as_int(data.get("board_round")))
    board_created = data.get("board_created_at")
    if isinstance(board_created, str) and board_created:
        stamp = _work_stamp(board_created, 0)
        if not state["created_at"] or stamp < state["created_at"]:
            state["created_at"] = stamp


def _work_apply(item: dict[str, Any], data: Mapping[str, Any], stamp_ms: int) -> None:
    """One entry's delta onto *item*: its fields, then its event line.

    A conductor's and a worker's fields are disjoint, so applying the two parties'
    entries in either relative order yields the same fields -- which is what lets
    one board be folded unit by unit when the units interleave in time. The event
    tail is the one place order shows, and it is kept in time order on insert.
    """
    action = _as_str(data.get("action"))
    actor = _as_str(data.get("actor"))
    if actor == "worker":
        if action != "report":
            return
        for name in _WORK_WORKER_FIELDS:
            if name in data:
                item[name] = _work_field(name, data[name])
        # Every report REPLACES the reason, so an absent one is a cleared one rather
        # than "unchanged" -- the one worker field read that way.
        item["reason"] = _work_reason(data.get("reason"))
        item["last_report_at"] = _work_stamp(data.get("last_report_at"), stamp_ms)
    else:
        allowed = _WORK_CONDUCTOR_FIELDS.get(action)
        if allowed is None:
            return
        for name in allowed:
            if name in data:
                item[name] = _work_field(name, data[name])
        if action == "close":
            item["closed_at"] = _work_stamp(data.get("closed_at"), stamp_ms)
        elif action == "decide":
            item["decided_at"] = _work_stamp(data.get("decided_at"), stamp_ms)
        elif action == "verdict":
            # A pending verdict carries the stamp its run began at; any other verdict
            # ends the run, so an absent field reads as cleared, not unchanged.
            carried = data.get("pending_since")
            running = item["verdict"] == "pending" and isinstance(carried, str) and bool(carried)
            item["pending_since"] = _work_stamp(carried, stamp_ms) if running else None
        elif action == "accept":
            # A pending run belongs to the bar it measured; the new bar starts its own.
            item["pending_since"] = None
    kind = _as_str(data.get("event_kind"))
    if not kind:
        return
    status = data.get("status") if action == "report" else None
    text = _as_str(data.get("event"))[:WORK_EVENT_TEXT_LIMIT]
    # The store's own stamp and id when the entry carries them (every entry the
    # routes write does); the append time and the content address otherwise.
    ts = _work_stamp(data.get("event_ts"), stamp_ms)
    event_id = data.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        from kiro_crew.work_vocab import work_event_id

        event_id = work_event_id(ts, item["item_id"], kind, text, status=status)
    _work_add_event(
        item,
        {
            "id": event_id[:TEXT_LIMIT],
            "ts": ts,
            "item_id": item["item_id"],
            "kind": kind,
            "status": status if isinstance(status, str) else None,
            "text": text,
            "_t": stamp_ms,
        },
    )


def _work_reason(value: Any) -> str | None:
    """A report's ``reason`` when it is one of the closed vocabulary, else unset."""
    return value if isinstance(value, str) and value in WORK_BLOCKED_REASONS else None


def _work_field(name: str, value: Any) -> Any:
    """*value* in the shape the record holds for *name*; the fold's own shape gate."""
    if name in ("acceptance", "artifacts"):
        if not isinstance(value, dict):
            return {}
        if name == "artifacts":
            return {str(k): v for k, v in value.items() if isinstance(v, str)}
        return dict(value)
    if name in ("round", "fails", "pr"):
        return _as_int(value) if value is not None else None
    if name == "reason":
        return _work_reason(value)
    if name in ("verdict", "worker_session_key", "status"):
        return _work_text(name, value) if value is not None else None
    return _work_text(name, value)


#: The store's own character caps for the work board's text fields. The shared
#: ``TEXT_LIMIT`` (200) is a title's width; a decision or a goal the store took
#: at 2000 must come back whole, so the fold cuts each field at the store's bound.
_WORK_TEXT_LIMITS: Final[dict[str, int]] = {
    "goal": 2000,
    "decision": 2000,
    "summary": 500,
    "title": 200,
}


def _work_text(name: str, value: Any) -> str:
    """*value* when it is a string, cut at the store's cap for *name*, else empty."""
    if not isinstance(value, str):
        return ""
    return value[: _WORK_TEXT_LIMITS.get(name, TEXT_LIMIT)]


def _work_add_event(item: dict[str, Any], event: dict[str, Any]) -> None:
    """Insert *event* into the item's tail in time order and apply the store's rules:
    two consecutive ``progress`` reports collapse to the newer, and the tail keeps
    its newest :data:`WORK_EVENT_LIMIT` lines."""
    events: list[dict[str, Any]] = item["events"]
    at = len(events)
    while at > 0 and events[at - 1]["_t"] > event["_t"]:
        at -= 1
    events.insert(at, event)
    if _work_is_progress(event):
        if at > 0 and _work_is_progress(events[at - 1]):
            del events[at - 1]
            at -= 1
        if at + 1 < len(events) and _work_is_progress(events[at + 1]):
            del events[at]
    if len(events) > WORK_EVENT_LIMIT:
        del events[: len(events) - WORK_EVENT_LIMIT]


def _work_is_progress(event: Mapping[str, Any]) -> bool:
    return event.get("kind") == "report" and event.get("status") == "progress"


def _work_render(state: dict[str, Any]) -> WorkBoardView:
    """The board in the shape its readers already consume: the conductor header and
    every item in creation order, each with its event tail.

    The return is NARROWED to :class:`~kiro_crew.work_vocab.WorkBoardView` rather than
    left as ``dict``: a reader mapping this onto a dashboard's own contract type then
    has both ends checked by mypy, and a field renamed here is an error at every such
    reader instead of a key that silently reads as missing. The projection kernel in
    ``kiro_crew.projection`` is unchanged -- its Protocol asks for ``-> dict`` and a
    return type is covariant.
    """
    items: list[WorkBoardItem] = []
    for item_id in state["order"]:
        item = state["items"].get(item_id)
        if item is None:
            continue
        rendered = {
            key: value for key, value in item.items() if key != "events" and key != "_unrecorded"
        }
        rendered["schema"] = 1
        rendered["events"] = [
            {key: value for key, value in event.items() if key != "_t"} for event in item["events"]
        ]
        items.append(cast("WorkBoardItem", rendered))
    return {
        "conductor": {
            "schema": 1,
            "slot_key": state["slot"],
            "goal": state["goal"],
            "round": state["round"],
            "goal_version": state["goal_version"],
            "depth": state["depth"],
            "parent_item": state["parent_item"],
            "created_at": state["created_at"],
            "entries": state["entries"],
            "first_entry_at": state["first_entry_at"],
            # Read directly, like its sibling keys. ``_work_start`` declares this key
            # in the fold's durable top-level shape, and ``_state_matches_fold``
            # refuses any checkpoint whose top-level keys differ from that shape, so a
            # payload missing it is discarded and cold-folded rather than resumed. The
            # key is present on every state that reaches here.
            "last_entry_at": state["last_entry_at"],
            "generation": state["generation"],
        },
        "items": items,
        "omitted": state["omitted"] + sum(len(p) for p in state["parked"].values()),
        "forgotten": cast(
            "WorkBoardForgotten",
            dict(
                state["forgotten"],
                # The protected tombstones: a rebuild reads these to count a forgotten
                # item whose record still reads unrecorded once, not twice. Bounded by
                # the tombstone cap.
                baseline_ids=[key for key, born in state["tombstones"].items() if born is True],
            ),
        ),
    }
