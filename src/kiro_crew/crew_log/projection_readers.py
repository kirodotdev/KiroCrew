"""Reading a slot's folds and a tree's folds: which units a key spans, the cold fold over
them, and the public reads the routes serve.
"""

from __future__ import annotations

from collections.abc import Sequence

from kiro_crew import session_ledger
from kiro_crew.crew_log.entry_types import DASHBOARD_FOLD_NAME, PANEL_FOLD_NAME
from kiro_crew.crew_log.errors import CODE_BAD_DATA, CrewLogError
from kiro_crew.crew_log.projection_catalogue import (
    KNOWN_TYPES,
    TREE_PROJECTION_NAMES,
    WORKSTREAMS_FOLD_NAME,
    Projection,
)
from kiro_crew.crew_log.projection_kernel import Checkpoint, advance, projection_of, require_name
from kiro_crew.crew_log.projection_registry import _FOLDS
from kiro_crew.crew_log.projection_session_log import open_session_log
from kiro_crew.crew_log.projection_slot_memo import fold_slot_warm_revised
from kiro_crew.crew_log.projection_work import _work_units
from kiro_crew.crew_log.projection_workstreams import (
    _board_closure_units,
    _workstreams_item_workers,
    _workstreams_units,
)
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


def read_slot_projection(slot: str, name: str, *, also_slots: Sequence[str] = ()) -> Projection:
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
    """
    units = list(_slot_units_for_fold(slot, name))
    known = set(units)
    for extra in also_slots:
        for unit_id in _supplemental_units(extra, name):
            if unit_id not in known:
                known.add(unit_id)
                units.append(unit_id)
    checkpoint, revision = fold_slot_warm_revised(require_name(name), units, slot=slot)
    return projection_of(checkpoint, revision=revision)


def workstream_item_workers(slot: str, item_id: str) -> "list[str]":
    """The worker session key bound to *item_id* on each board *slot*'s workstreams page draws.

    The server-side half of opening a worker from its card: the page holds only an alias
    and an item id, so the key is looked up here, in the raw fold state the page's own read
    was rendered from (:func:`~kiro_crew.crew_log.projection_workstreams._workstreams_item_workers`
    has the shape of the answer). One warm fold read: the page's read just
    primed the same cell. Blocking file IO; callers run it off the event loop.
    """
    units = _slot_units_for_fold(slot, WORKSTREAMS_FOLD_NAME)
    checkpoint, _revision = fold_slot_warm_revised(WORKSTREAMS_FOLD_NAME, units, slot=slot)
    return _workstreams_item_workers(checkpoint.state, item_id)


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


def units_in_succession(slot: str) -> "tuple[str, ...]":
    """The slot's session units ordered by DURABLE succession rather than by the clock.

    Read by every fold whose answer depends on which unit is folded LAST: ``usage``,
    whose per-turn window trims from the front, and ``agentic``, which keeps the newest
    value per field and so lets a predecessor folded last overwrite a newer one.

    The header fallthrough orders units by ``createdAt``, a wall-clock stamp: a clock
    that steps backward between two units of one slot -- an NTP correction, a VM
    resume -- sorts the genuinely-newer unit BEFORE its predecessor. The usage fold
    appends each ``context/composed`` as a per-turn row and trims the OLDEST off the
    front at ``CONTEXT_TURNS_LIMIT``, so an inverted order lands the newest unit's rows
    at the front and the trim evicts them, keeping a retired session's rows as the
    "newest" the panel draws.

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
        return _work_units(slot, session_ledger.work_crew_log_units(slot))
    if name == WORKSTREAMS_FOLD_NAME:
        # The same units the ``work`` fold reads -- the conductor's, then every bound
        # worker's -- because one session appends its work records AND its turn closers
        # to one crew log, so the files carrying the boards are the files carrying the
        # spend. No wider: the fold answers for the boards THIS slot reaches, and
        # reading a slot nothing here binds would be a scan for data no reader asked
        # for.
        return _workstreams_units(slot)
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
    if name == DASHBOARD_FOLD_NAME:
        # Nor here, and for the reason the panel fold gives: this fold keeps the LATEST
        # value per field, so whichever unit is folded last wins each cell. A backward
        # clock step between two units of one slot sorts the retired unit last under the
        # header clock, and its stale value would then overwrite a value the live session
        # wrote and the write path accepted -- silently, because both are well-formed.
        return units_in_succession(slot)
    return session_units_for_slot(slot)


# --------------------------------------------------------------------------- #
# Reading a tree's folds
# --------------------------------------------------------------------------- #


def tree_fold_units(root: str, name: str) -> "tuple[tuple[str, ...], int]":
    """The units *name* is folded over for the tree rooted at *root*, and how many the
    selection refused.

    THE THIRD KEY KIND'S unit resolver, and the reason it is a third kind: this answer is
    not a property of the key. It walks what the record SAYS -- the root's own units,
    every worker those units recorded a bind for, and every board those workers conduct,
    to closure (:func:`_board_closure_units`) -- so a bind appended after the last read
    changes the population with nothing about *root* having changed. A slot fold's units
    come from the store's listing for one slot and cannot do that.
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
    return _board_closure_units(root)


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

    COLD every time, and that is the posture the key kind earns rather than an omission:
    a warm cell would be invalidated by growth in ANY member's log, including a member
    the cell did not know existed, and there is no publisher for that event. So this fold
    is lazy, has no bus scope, and belongs on a page load and on a refetch -- which the
    controller contract already covers, since a gap triggers a full refetch anyway.

    Its production caller is the dashboard controller:
    :meth:`kiro_crew.dashboard.handlers.member_dashboard_fields.LivePage._read_trees`,
    reached from ``GET /api/members/{slug}/dashboard`` when the page's model binds a
    tree fold, and again from that route's refetch. The controller calls it on arm and
    on refetch and never from the fold worker, which is what keeps this read off the
    thread that folds.

    BOTH routes that take a fold name -- the owner's per-session read and the internal
    per-unit one -- REFUSE a tree-keyed name rather than folding the one unit they
    address, so this is the only way in.
    """
    return projection_of(fold_tree_checkpoint(require_name(name), root))
