"""Reading a slot's folds and a tree's folds: which units a key spans, the cold fold over
them, and the public reads the routes serve.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from typing import Any

from kiro_crew import session_ledger
from kiro_crew.config.paths import data_home
from kiro_crew.crew_log.entry_types import (
    CUSTOM_FOLD_NAME,
    DASHBOARD_FOLD_NAME,
    GOAL_FOLD_NAME,
    PANEL_FOLD_NAME,
    WAITING_FOLD_NAME,
)
from kiro_crew.crew_log.errors import CODE_BAD_DATA, CrewLogError
from kiro_crew.crew_log.projection_catalogue import (
    KNOWN_TYPES,
    TREE_PROJECTION_NAMES,
    WORKER_FOLD_NAME,
    WORKERS_FOLD_NAME,
    WORKSTREAMS_FOLD_NAME,
    Projection,
)
from kiro_crew.crew_log.projection_kernel import Checkpoint, advance, projection_of, require_name
from kiro_crew.crew_log.projection_registry import _FOLDS
from kiro_crew.crew_log.projection_session_log import open_session_log
from kiro_crew.crew_log.projection_slot_memo import fold_slot_warm_revised
from kiro_crew.crew_log.projection_workers import (
    merge_render,
    tree_seeded,
    worker_units,
    workers_children,
    workers_units,
)
from kiro_crew.crew_log.projection_workstreams import _own_board_units, _workstreams_units
from kiro_crew.crew_log.schema import KIND_SESSION
from kiro_crew.crew_log.session_tree import OpenedRecord, log_rank_of
from kiro_crew.crew_log.store import (
    session_units_for_slot,
    unit_header_created_at,
    unit_opened_previous,
)

# The ledger fold reads a record whose semantics -- which phases end a workstream,
# which event kinds exist, how much of each field is kept -- belong to the ledger
# subsystem. They are imported rather than restated so one owner sets them, the
# same direction ``store`` already takes for the store-name fold.
from kiro_crew.session_ledger import _FOLD_NAME as LEDGER_FOLD_NAME

# --------------------------------------------------------------------------- #
# Reading a slot's folds
# --------------------------------------------------------------------------- #


def fold_slot_checkpoint(name: str, unit_ids: Sequence[str], *, slot: str = "") -> Checkpoint:
    """*name* folded over every crew log of one slot, OLDEST UNIT FIRST.

    The slot-keyed read. ``unit_ids`` comes from
    :func:`~kiro_crew.crew_log.store.session_units_for_slot`, which orders them by
    creation, and a unit with no crew log is skipped rather than refused -- a slot
    whose oldest unit was collected by retention still folds the ones it has.

    A ``seq`` is comparable only WITHIN one file, so the guard :func:`advance`
    applies is RE-BASED per unit: the state carries forward across units while the
    seq restarts at each one. Without that, the second unit's entries would all sit
    at or below the first unit's seq and be refused as a re-fold -- the collision
    ``advance`` exists to name, arriving here for a legitimate reason.

    The returned ``last_seq`` is the last entry folded from the NEWEST unit, which
    is the only figure a later read of the same slot can compare against; it is 0
    when that unit contributed nothing. It is deliberately not a sum across files:
    that would be a number no file carries, and a reader could not truncate
    against it.

    A CHECKPOINT rather than a rendered value, because the writer needs one: it
    advances this state over the entry it is appending to answer with the record
    that entry produces, so the answer comes out of this same fold instead of a
    second implementation of the same update rules.

    *slot* is handed to a fold that declares ``bind_slot``, so a slot-keyed fold
    knows which slot it answers for before the first entry instead of guessing
    it from that entry.
    """
    fold_spec = _FOLDS[require_name(name)]
    state = fold_spec.start()
    if slot and fold_spec.bind_slot is not None:
        fold_spec.bind_slot(state, slot)
    reached = 0
    for unit_id in unit_ids:
        handle = open_session_log(unit_id)
        if handle is None:
            continue
        grown = advance(
            Checkpoint(name=name, last_seq=0, state=state),
            handle.iter_from(1, known=KNOWN_TYPES),
        )
        state = grown.state
        reached = grown.last_seq
    return Checkpoint(name=name, last_seq=reached, state=state)


def fold_slot(name: str, unit_ids: Sequence[str], *, slot: str = "") -> Projection:
    """:func:`fold_slot_checkpoint` rendered -- the value a slot-keyed reader is served."""
    return projection_of(fold_slot_checkpoint(name, unit_ids, slot=slot))


def read_slot_projection(
    slot: str, name: str, *, also_slots: Sequence[str] = (), cold: bool = True
) -> Projection:
    """One slot-keyed projection for *slot*, folded over every unit it ran under.

    The units come from the fold's OWNER, not from a raw store listing. For the ledger
    that owner drops the units a permanent delete excluded and puts the recorded order
    ahead of the header clock, and a raw listing here would serve a different answer
    from the one every other reader gets -- including a deleted conversation's goal and
    phase on a recycled slot key. A fold whose owner has no such rule falls through to
    the store listing, which is what it would have used anyway. *also_slots* names
    further slots whose units join the fold after those -- a caller that knows of a
    party the record itself does not name yet (a worker bound before the board was
    recorded) says so here.

    ``cold=False`` is the request-path form: where the slot has no warm cell and no
    usable savepoint it raises
    :class:`~kiro_crew.crew_log.projection_slot_memo.SlotFoldNotReady` instead of folding
    every unit (see :mod:`kiro_crew.crew_log.slot_prime`).
    """
    if name == WORKERS_FOLD_NAME:
        return _read_workers(slot, cold=cold)
    checkpoint, revision = read_slot_state(slot, name, also_slots=also_slots, cold=cold)
    return projection_of(checkpoint, revision=revision)


def read_slot_state(
    slot: str, name: str, *, also_slots: Sequence[str] = (), cold: bool = True
) -> "tuple[Checkpoint, int]":
    """The fold state :func:`read_slot_projection` renders, with its revision.

    The same warm in-memory cell, for a reader that needs more of the state than the
    rendered value carries (the usage fold's past turns). Never the ``workers`` fold,
    whose value merges several cells. ``cold`` is :func:`read_slot_projection`'s.
    """
    if name == WORKERS_FOLD_NAME:
        raise ValueError("the workers fold has no single state; read its projection")
    units = list(_slot_units_for_fold(slot, name))
    known = set(units)
    for extra in also_slots:
        for unit_id in _supplemental_units(extra, name):
            if unit_id not in known:
                known.add(unit_id)
                units.append(unit_id)
    return fold_slot_warm_revised(require_name(name), units, slot=slot, cold=cold)


#: Per ``(data home, crewmate slot)``, the part revisions the last ``workers`` read
#: merged and the revision it was served at. Bounded: the oldest crewmate goes first.
_WORKERS_REVISIONS: "dict[tuple[str, str], tuple[Any, int]]" = {}
_WORKERS_REVISIONS_MAX = 512
_WORKERS_REVISIONS_LOCK = threading.Lock()
#: Held across ONE crewmate's whole ``workers`` read, from the first part to the
#: revision, so a read that captured older parts cannot be handed a revision above one
#: that captured newer parts and finished first. Striped by crewmate, a fixed set.
_WORKERS_READ_LOCKS: "tuple[threading.Lock, ...]" = tuple(threading.Lock() for _ in range(64))


def _read_workers(slot: str, *, cold: bool = True) -> Projection:
    """``workers`` for crewmate *slot*: its own creates merged with each worker's row.

    Each part is a warm cell of its own (:func:`fold_slot_warm_revised`): ``workers``
    over the crewmate's units, and ``worker`` per child over that child's units
    (:func:`~kiro_crew.crew_log.projection_workers.worker_units`). A worker that wrote
    continues its own cell; every other part answers from the cell it holds.

    The REVISION moves whenever any part's does: the merged value is new then, and a
    consumer keeps the highest revision per (key, fold). A fresh one is minted when the
    parts changed, never the highest part's -- a part that leaves the set (a worker
    past the cap) would otherwise move the number backwards.
    """
    key = (str(data_home()), slot)
    lock = _WORKERS_READ_LOCKS[hash(key) % len(_WORKERS_READ_LOCKS)]
    if cold:
        with lock:
            return _read_workers_locked(slot, key, cold=cold)
    # The request-path form must not wait out a COLD fold another read is doing under
    # this stripe (a background prime of this crewmate, or of another one sharing the
    # stripe): it answers "not ready" instead, the rule the per-cell lock follows.
    from kiro_crew.crew_log.projection_slot_memo import (
        _PRIMING_POLL_SECONDS,
        SlotFoldNotReady,
        any_cold_fold_running,
    )

    while not lock.acquire(timeout=_PRIMING_POLL_SECONDS):
        if any_cold_fold_running(_WORKERS_PART_FOLDS):
            raise SlotFoldNotReady(slot, WORKERS_FOLD_NAME)
    try:
        return _read_workers_locked(slot, key, cold=cold)
    finally:
        lock.release()


#: The folds a ``workers`` read folds under its stripe lock.
_WORKERS_PART_FOLDS: "frozenset[str]" = frozenset({WORKERS_FOLD_NAME, WORKER_FOLD_NAME})


def _read_workers_locked(slot: str, key: "tuple[str, str]", *, cold: bool = True) -> Projection:
    from kiro_crew.crew_log.projection_slot_memo import _minted_revision

    seeded = tree_seeded()
    own, own_revision = fold_slot_warm_revised(
        WORKERS_FOLD_NAME, workers_units(slot), slot=slot, cold=cold
    )
    parts = [own_revision]
    workers: dict[str, Any] = {}
    for child in workers_children(slot):
        units = worker_units(child, units_in_succession(child))
        if not units:
            continue
        cell, revision = fold_slot_warm_revised(WORKER_FOLD_NAME, units, slot=child, cold=cold)
        workers[child] = _FOLDS[WORKER_FOLD_NAME].render(cell.state)
        parts.append(revision)
    value = merge_render(own.state, slot, workers)
    signature = (tuple(parts), tuple(workers))
    with _WORKERS_REVISIONS_LOCK:
        held = _WORKERS_REVISIONS.get(key)
        if not seeded:
            # The unseeded session tree names no workers, so this value is provisional
            # and vouches for no revision: the one read once it is seeded outranks it.
            revision = 0
        elif held is not None and held[0] == signature:
            revision = held[1]
        elif 0 in parts:
            # A part read outside a warm memo vouches for no revision, so neither does
            # the whole.
            revision = 0
        else:
            revision = _minted_revision()
            _WORKERS_REVISIONS.pop(key, None)
            _WORKERS_REVISIONS[key] = (signature, revision)
            while len(_WORKERS_REVISIONS) > _WORKERS_REVISIONS_MAX:
                del _WORKERS_REVISIONS[next(iter(_WORKERS_REVISIONS))]
    return Projection(name=WORKERS_FOLD_NAME, seq=own.last_seq, value=value, revision=revision)


def _supplemental_units(slot: str, name: str) -> "tuple[str, ...]":
    """One *also_slots* slot's units, in the same ORDER the fold uses for its own.

    A supplemental slot arrives by a different route from the fold's own units -- the
    caller names a party the record does not name yet -- but its units land in the same
    fold, so a later one still applies over an earlier one. Resolving it through the raw
    store listing would therefore order this one party by the header clock while every
    other unit in the same fold is ordered causally, and a clock that steps backward
    across that party's reset would fold its older unit last. The order log exists to
    prevent exactly that, so the supplemental path has to read it too.

    Only the work fold is redirected here. The ledger fold's supplemental units keep the
    store listing they already used; changing that is a separate question from this one.
    """
    if name == "work":
        return session_ledger.work_crew_log_units(slot)
    return session_units_for_slot(slot)


#: Each slot's newest unit, keyed by the slot's unit listing it was computed from. The
#: listing comes from the store's slot index, which re-checks a unit whose header was
#: still unpublished when it was listed, so a unit that becomes provable later changes
#: the key even though the root's stat does not move.
_NEWEST_UNITS: "dict[str, tuple[tuple[Any, ...], str]]" = {}
_NEWEST_UNITS_LIMIT = 1024
_newest_units_guard = threading.Lock()


def newest_unit_in_succession(slot: str) -> str:
    """The last of :func:`units_in_succession` for *slot*, or ``""``; cached on its units.

    A unit's place in the succession is written once (header, opening entry), so the
    answer can only change when the slot's set of units does.
    """
    if not slot:
        return ""
    identity: "tuple[Any, ...]" = (str(data_home()), session_units_for_slot(slot))
    with _newest_units_guard:
        held = _NEWEST_UNITS.get(slot)
    if held is not None and held[0] == identity:
        return held[1]
    units = units_in_succession(slot)
    unit = units[-1] if units else ""
    # Cached only when at most one unit lacks a readable predecessor (the chain start).
    # A unit whose opening entry has not landed yet reads as a second start and would
    # be ordered by the clock; caching that guess would outlive the opener's arrival.
    settled = sum(1 for u in units if unit_opened_previous(KIND_SESSION, u) is None) <= 1
    if unit and settled:
        with _newest_units_guard:
            _NEWEST_UNITS.pop(slot, None)
            _NEWEST_UNITS[slot] = (identity, unit)
            while len(_NEWEST_UNITS) > _NEWEST_UNITS_LIMIT:
                _NEWEST_UNITS.pop(next(iter(_NEWEST_UNITS)))
    return unit


def units_in_succession(slot: str) -> "tuple[str, ...]":
    """The slot's session units ordered by DURABLE succession rather than by the clock.

    Read by every fold whose answer depends on which unit is folded LAST: ``usage``,
    whose context part keeps the newest turn and a ring of the newest rows, and ``agentic``, which keeps the newest
    value per field and so lets a predecessor folded last overwrite a newer one.

    The header fallthrough orders units by ``createdAt``, a wall-clock stamp: a clock
    that steps backward between two units of one slot -- an NTP correction, a VM
    resume -- sorts the genuinely-newer unit BEFORE its predecessor. The usage fold
    keeps the newest composed turn and pushes each closed turn into a fixed ring that
    drops the OLDEST row (``TREND_TURNS``), so an inverted order makes a retired
    session's turn the "newest" the panel draws and can push the newest unit's rows
    out of the ring.

    So the units are ordered by the ``previous_sid`` succession chain the store itself
    wrote -- each ``session/opened`` names the unit it replaced -- via the same
    :func:`log_rank_of` the session tree ranks its logs with: depth in the chain leads.
    The predecessor link is read O(1) from each unit's opening entry, and there are as
    many reads as the slot has units, which is small. A unit whose predecessor cannot be
    read is a chain start (depth 0), the safe direction -- it starts its own chain rather
    than borrowing another's history.

    Depth alone is NOT the whole order, because a slot can hold two UNRELATED chains at
    once -- a session recreated after its predecessor's log was pruned, two logs whose
    link was never written -- and those chains do not relate. Ordering by ``(depth, ...)``
    globally would interleave them: chain A's depth-1 unit and chain B's depth-1 unit
    would sort adjacent, splitting each chain's ``previous_sid`` edges apart and landing
    a retired chain's rows between a live chain's. So the order is chain-CONTIGUOUS: each
    unit is grouped under its chain root (the deepest predecessor still in the
    population), the roots -- the only units the chain cannot relate -- are ordered by
    their ``createdAt`` header, and within a chain the ``previous_sid`` depth leads. That
    preserves every predecessor edge as a contiguous run and confines the wall clock to
    the one question the chain leaves open.
    """
    units = session_units_for_slot(slot)
    if len(units) < 2:
        # One unit (or none) has nothing to reorder, and the chain read is pure cost.
        return units
    records = []
    for unit_id in units:
        created = unit_header_created_at(KIND_SESSION, unit_id)
        previous = unit_opened_previous(KIND_SESSION, unit_id)
        records.append(
            OpenedRecord(
                sid=unit_id,
                slot=slot,
                created_at=created if isinstance(created, int) else 0,
                parent_slot=None,
                previous_sid=previous,
            )
        )
    by_sid = {record.sid: record for record in records}
    rank = log_rank_of(records)
    # The store's own listing order, which is the creation order it established and the
    # clock-independent chronology the OLD stable sort preserved for two same-ranked units.
    # It is the tiebreak for units the succession chain cannot separate -- two unrelated
    # roots, or (defensively) two units a damaged chain gives one depth -- so a root with
    # no readable ``createdAt`` keeps its store position instead of falling to an arbitrary
    # id order that would reorder a retired chain ahead of a live one.
    listed = {unit_id: index for index, unit_id in enumerate(units)}

    def _chain_root(unit_id: str) -> str:
        # Walk ``previous_sid`` to the deepest predecessor still in the population -- the
        # unit that shares no chain with any other root. A predecessor pruned from the
        # slot, absent, or belonging to another slot stops the walk (the same three stops
        # ``_chain_step`` makes), so the walk stays within THIS slot's history. A cycle
        # reached through damaged records is bounded by ``seen``: once a unit repeats, its
        # chain has no clean root and the first-seen unit anchors the group deterministically.
        cursor = unit_id
        seen = {cursor}
        while True:
            previous = by_sid[cursor].previous_sid
            if not previous:
                return cursor
            step = by_sid.get(previous)
            if step is None or step.slot != by_sid[cursor].slot or previous in seen:
                return cursor
            seen.add(previous)
            cursor = previous

    def _order_key(unit_id: str) -> "tuple[int, int, int, int]":
        root = _chain_root(unit_id)
        # Chains are grouped by their root so every ``previous_sid`` edge stays a
        # contiguous run. Roots -- the only units the chain cannot relate -- are ordered
        # by the header ``createdAt`` (the wall clock, consulted ONLY here), then by store
        # listing position so an unset or tied clock keeps creation order rather than an
        # arbitrary id one. Within a chain the succession depth leads (predecessor first);
        # the unit's own listing position is the final, defensive tiebreak. Ascending =
        # oldest first, the order the fold applies (a later unit's update wins).
        root_created = rank.get(root, (0, by_sid[root].created_at, root))[1]
        depth = rank.get(unit_id, (0, 0, unit_id))[0]
        return (root_created, listed.get(root, len(units)), depth, listed.get(unit_id, len(units)))

    return tuple(sorted(units, key=_order_key))


def _slot_units_for_fold(slot: str, name: str) -> "tuple[str, ...]":
    """The units *name* is folded over for *slot*, as that fold's owner defines them."""
    if name == LEDGER_FOLD_NAME:
        return session_ledger.crew_log_units(slot)
    if name == "work":
        # The board's OWN units and nothing else. A bound worker's report is written by
        # the gateway into the conductor's unit as well as the worker's
        # (``api_work_report``), so the board is complete without reading -- or walking
        # the binds to find -- any worker's log.
        return session_ledger.work_crew_log_units(slot)
    if name == WORKSTREAMS_FOLD_NAME:
        # The board's own units, like ``work``: a bound worker's charges and a nested
        # board's records are relayed into them (``work_relay``), so the project report
        # needs no worker's log.
        return _workstreams_units(slot)
    if name == WORKERS_FOLD_NAME:
        # The crewmate's own units: its creates. Each worker's own log is read by that
        # worker's ``worker`` cell, merged in by :func:`_read_workers`.
        return workers_units(slot)
    if name == WORKER_FOLD_NAME:
        return worker_units(slot, units_in_succession(slot))
    if name == PANEL_FOLD_NAME:
        # NOT the header fallthrough. A header's ``createdAt`` is stamped once and never
        # rewritten, so a backward clock step between two units of one slot would order
        # the retired unit last -- and this fold takes the newest entry WHOLE, so that
        # retired publish would become the current panel permanently, with the history
        # rows built against the wrong predecessor.
        return session_ledger.panel_crew_log_units(slot)
    if name == "usage":
        # NOT the header fallthrough either. The usage fold keeps a per-turn row window
        # and trims the oldest off the front, so a backward clock step that inverted two
        # units would evict the newest unit's rows and keep a retired session's. Order by
        # the durable succession chain instead of the header clock.
        return units_in_succession(slot)
    if name in (DASHBOARD_FOLD_NAME, CUSTOM_FOLD_NAME):
        # Nor here, and for the reason the panel fold gives: this fold keeps the LATEST
        # value per field, so whichever unit is folded last wins each cell. A backward
        # clock step between two units of one slot sorts the retired unit last under the
        # header clock, and its stale value would then overwrite a value the live session
        # wrote and the write path accepted -- silently, because both are well-formed.
        # The custom fold keeps the latest record per TYPE and is the same case.
        return units_in_succession(slot)
    if name in (GOAL_FOLD_NAME, WAITING_FOLD_NAME):
        # The same reason again: each keeps the NEWEST snapshot whole, so a retired
        # session sorted last by its header clock would bring back an old goal or a
        # question already answered.
        return units_in_succession(slot)
    return session_units_for_slot(slot)


# --------------------------------------------------------------------------- #
# Reading a tree's folds
# --------------------------------------------------------------------------- #


def tree_fold_units(root: str, name: str) -> "tuple[tuple[str, ...], int]":
    """The units *name* is folded over for the tree rooted at *root*, and how many the
    selection refused.

    The root board's OWN units only (:func:`_own_board_units`). A nested board's records
    are relayed up into its parent's log by the gateway (``work_relay``), so each board
    of the tree is already in the root's units and no bind is walked and no other
    board's log is opened to find them.
    """
    if require_name(name) not in TREE_PROJECTION_NAMES:
        # The same refusal shape ``require_name`` raises for a name the registry does not
        # hold at all, because the consequence is the same: answering here for a slot
        # fold would fold it over a whole tree's units and serve several boards' entries
        # as one board's, which is a complete-looking wrong answer.
        raise CrewLogError(
            f"{name!r} is not a tree-keyed projection; expected one of "
            f"{list(TREE_PROJECTION_NAMES)}",
            code=CODE_BAD_DATA,
            field="name",
        )
    return _own_board_units(root)


def fold_tree_checkpoint(name: str, root: str) -> Checkpoint:
    """*name* folded over every unit of the tree rooted at *root*, OLDEST UNIT FIRST.

    :func:`fold_slot_checkpoint`'s counterpart for the third key kind, and it re-bases
    the seq guard per unit for the same reason that one does: a ``seq`` is comparable only
    WITHIN one file, so handing the units' own seqs over unchanged would see the second
    unit's entries sit at or below the first's and refuse them as a re-fold.

    ``last_seq`` is the last entry folded from the NEWEST unit, which is the only figure
    a later read can compare against -- not a sum across files, which is a number no file
    carries.

    The refused-unit count is NOT returned here. It belongs to the rendered value (the
    fold's ``dropped``), and it is folded in as the state's own ``omitted`` so that a
    checkpoint resumed from a savepoint cannot read it as zero.
    """
    fold_spec = _FOLDS[require_name(name)]
    units, dropped = tree_fold_units(root, name)
    state = fold_spec.start()
    if fold_spec.bind_slot is not None:
        fold_spec.bind_slot(state, root)
    state["omitted"] = state.get("omitted", 0) + dropped
    reached = 0
    for unit_id in units:
        handle = open_session_log(unit_id)
        if handle is None:
            continue
        grown = advance(
            Checkpoint(name=name, last_seq=0, state=state),
            handle.iter_from(1, known=KNOWN_TYPES),
        )
        state = grown.state
        reached = grown.last_seq
    return Checkpoint(name=name, last_seq=reached, state=state)


def read_tree_projection(root: str, name: str) -> Projection:
    """One tree-keyed projection for the tree rooted at *root*. The read a block gets.

    Folded over the root's own units (:func:`tree_fold_units`) in the root's warm cell,
    the same cell a slot fold uses: a read with nothing new appended reads no entry, and
    after a restart the fold resumes from its savepoint.

    Its production caller is the dashboard controller:
    :meth:`kiro_crew.dashboard.handlers.member_dashboard_fields.LivePage._read_trees`,
    reached from ``GET /api/members/{slug}/dashboard`` when the page's model binds a
    tree fold, and again from that route's refetch. It is never called from the fold
    worker.

    BOTH routes that take a fold name -- the owner's per-session read and the internal
    per-unit one -- REFUSE a tree-keyed name rather than folding the one unit they
    address, so this is the only way in.
    """
    fold_name = require_name(name)
    units, dropped = tree_fold_units(root, fold_name)
    checkpoint, revision = fold_slot_warm_revised(fold_name, list(units), slot=root)
    projected = projection_of(checkpoint, revision=revision)
    if dropped and isinstance(projected.value, dict):
        # The refused count belongs to this read's selection, not to the cell's state.
        value = {**projected.value, "dropped": int(projected.value.get("dropped", 0)) + dropped}
        projected = Projection(
            name=projected.name, seq=projected.seq, value=value, revision=projected.revision
        )
    return projected
