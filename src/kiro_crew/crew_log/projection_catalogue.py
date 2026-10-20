"""The fold catalogue: every registered fold name by the key it is folded by, the
``_Fold`` record a fold is registered as, and the :class:`Projection` value a reader is
served. The lowest layer of the fold owners: it imports no other one.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

from kiro_crew.crew_log.entry_types import (
    DASHBOARD_FOLD_NAME,
    MISTAKES_FOLD_NAME,
    SESSION_ENTRY_TYPES,
)
from kiro_crew.crew_log.schema import Entry

#: The crew-log folds' one logger, still named for the facade module so a log filter
#: or capture keyed on that name keeps matching; a module that logs imports this one.
logger = logging.getLogger("kiro_crew.crew_log.projection")

#: The session side panel's projections, in the RFC section 5 order. These fold ONE
#: session's crew log and are the set the growth push sends, which is why ``class``
#: is NOT among them: nothing on a client draws it, so pushing it would ship a frame
#: per log growth to every owner socket for no reader.
PROJECTION_NAMES: Final[tuple[str, ...]] = (
    "status",
    "usage",
    "timeline",
    "tools",
    "approvals",
    "subagents",
)

#: Folds the registry holds but does NOT advertise: no panel draws them and the
#: growth push does not carry them. ``class`` answers what kind of session a log
#: belongs to over the log's whole life, for a reader deciding whether another
#: session may read it, and its one caller asks for it by name
#: (``fold_session(("class",))``). It is registered here rather than kept private so
#: that one machinery folds it -- the same checkpoint, the same incremental reuse, the
#: same recreated-log guard -- while staying out of the advertised set, which would
#: otherwise name a projection with no reader.
#: ``outline`` joins it for the same reason stated one way further: it is keyed by one
#: SESSION, because a turn ordinal and a seq both restart in the next succession unit
#: (the ``timeline`` fold raises rather than render two units as one list), so the fold
#: that answers "what was this conversation" answers it per log. But no side panel draws
#: it -- its reader is a dashboard block that asks for it by name, the way ``class``'s
#: one caller does -- and an outline moves on the three entries of every turn, so
#: advertising it would push a frame to every owner socket on each of them for a
#: projection the panel set has no reader for.
#: ``otel_usage`` joins them for the same reason: its one reader is the OTel export
#: subscriber (:mod:`kiro_crew.metrics.crew_log_export`), which asks for the fold by
#: name through the crew-log bus rather than through a side panel, so advertising it
#: would push a frame to every owner socket on every turn for a reader that is not a
#: browser.
#: ``otel_session_tree`` joins them for the reason ``otel_usage`` does: its one reader
#: is the OTel export subscriber (:mod:`kiro_crew.metrics.crew_log_export`), which asks
#: for the fold by name through the crew-log bus, so advertising it would push a frame
#: to every owner socket for a reader that is not a browser.
INTERNAL_PROJECTION_NAMES: Final[tuple[str, ...]] = (
    "class",
    "outline",
    "otel_usage",
    "otel_session_tree",
)

#: Folds keyed by one SESSION, which are the ones the projection kernel drives
#: (:func:`_session_registry`). The advertised panel set plus the internal ``class``:
#: all of them fold a single crew log, so one pass over one file serves them and a
#: savepoint beside that file resumes them. The slot-keyed folds are the complement
#: and are driven by :func:`fold_slot_checkpoint`, which joins several files.
SESSION_FOLD_NAMES: Final[tuple[str, ...]] = PROJECTION_NAMES + INTERNAL_PROJECTION_NAMES

#: The fold's registry name, spelled HERE rather than beside the rest of its
#: constants because :data:`SLOT_PROJECTION_NAMES` below names it and that table is
#: built at import, before the fold's own section is reached.
WORKSTREAMS_FOLD_NAME: Final[str] = "workstreams"

#: Projections keyed by a SLOT instead of by one crew log. A slot owns one ACP
#: session id at a time, so a fact that belongs to the slot for its whole life --
#: its work ledger -- is spread over a unit per id it ran under, and answering for
#: it means joining them (:func:`fold_slot`). Kept out of
#: :data:`PROJECTION_NAMES` for that reason: the growth push and the side panel
#: address a session, and pushing a slot-wide value under one session's id would
#: report a partial answer as the whole one.
#:
#: The two dashboard folds are slot-keyed for exactly that reason, and for them it
#: is the point rather than an implementation detail. A crewmate's own dashboard
#: values and its record of refused writes are facts about the CREWMATE, so a fold
#: that reset every time its DM session was recreated would blank the dashboard and
#: forget the lesson precisely when a fresh context most needs both.
SLOT_PROJECTION_NAMES: Final[tuple[str, ...]] = (
    "ledger",
    "radar",
    "work",
    "panel",
    DASHBOARD_FOLD_NAME,
    MISTAKES_FOLD_NAME,
    WORKSTREAMS_FOLD_NAME,
)

#: The slot-keyed fold served by its OWNER and by no generic route. This fold's owner
#: (the Issue Radar crew store) orders a crew's units by the order the crew recorded
#: into them and pins the live unit last; a generic read has neither fact, and a
#: per-unit read would serve a part of the record as the whole. The dashboard's
#: projection routes refuse this name the way they refuse an unregistered one.
OWNER_SERVED_SLOT_PROJECTION: Final[str] = "radar"

#: The fold's registry name, spelled HERE for :data:`TREE_PROJECTION_NAMES`' sake, the
#: way :data:`WORKSTREAMS_FOLD_NAME` is spelled above its own table.
WORKTREE_FOLD_NAME: Final[str] = "worktree"

#: Projections keyed by a TREE ROOT -- the THIRD key kind, and the one that lets a
#: dashboard block bind to a tree instead of to a list.
#:
#: WHY A THIRD KIND AND NOT A WIDE SLOT FOLD. Three facts separate it, and each one on
#: its own would be enough:
#:
#: 1. THE POPULATION. A session fold reads ONE unit; a slot fold reads the units of ONE
#:    slot (:func:`~kiro_crew.crew_log.store.session_units_for_slot`) and joins them
#:    (:func:`fold_slot`). A tree fold reads the units of MANY SLOTS -- a root board's,
#:    every worker it bound, and every board those workers conduct, to closure. No
#:    listing of the key's own slot names them: they are reached by following what the
#:    record says, not by asking the store what one slot ran under.
#: 2. THE KEY DOES NOT DETERMINE THE POPULATION. A slot's units are a property OF THE
#:    SLOT, so a cell may be keyed by it. A tree's membership is a property of the
#:    RECORD: a bind appended today adds a slot that was not in yesterday's closure, and
#:    nothing about the key changed. So the population is re-resolved per read by the
#:    tree's own unit resolver and cannot be cached against the key the way a slot's is.
#: 3. INVALIDATION. A slot cell goes stale when THAT slot's log grows, which is the event
#:    the eager folder already wakes on. A tree cell goes stale when ANY member's log
#:    grows -- including a member the cell did not know existed when it was built -- and
#:    that is a different wake rule with no publisher. It is why the tree fold is LAZY
#:    (:attr:`_Fold.lazy_reason`) and has no bus scope
#:    (:func:`~kiro_crew.dashboard_feed.scope_for`): it is served on a page load and on a
#:    refetch, never pushed.
#:
#: And a third kind needs its OWN measured row cost, which is the other half of why it
#: is named here rather than folded into the slot set: :data:`_SLOT_FOLD_ROW_BYTES`'
#: ceiling is stated against its largest member, so a key kind admitted with no measured
#: cost would charge the fallback and leave that budget unbounded in practice. See
#: :data:`_TREE_FOLD_ROW_BYTES`, which states its method the way the slot table states
#: its own, and the import-time guard (:data:`_UNMEASURED_TREE_FOLDS`) that refuses
#: a tree fold without one.
TREE_PROJECTION_NAMES: Final[tuple[str, ...]] = (WORKTREE_FOLD_NAME,)

#: Every fold the registry (``_FOLDS``) holds, in registry order.
FOLD_NAMES: Final[tuple[str, ...]] = (
    PROJECTION_NAMES + INTERNAL_PROJECTION_NAMES + SLOT_PROJECTION_NAMES + TREE_PROJECTION_NAMES
)

#: The SHAPE of what these folds store, which is what a savepoint holds. It lives
#: here because it describes ``start`` and ``step``, and those are here: the
#: projection kernel reads it off each definition
#: (:class:`~kiro_crew.projection.ProjectionDefinition`) and refuses a payload
#: written under another number, so a stored state cannot resume onto logic that
#: keeps different bookkeeping.
#:
#: THE RULE: any change to what a fold's ``start`` or ``step`` STORES moves that
#: FOLD's version, including one that keeps the same keys. Shape is all this number
#: and ``_state_matches_fold`` can check, so a counting fix that leaves the keys
#: alone would resume the old build's state onto the new logic -- and the long
#: sessions a savepoint speeds up are the ones that then serve pre-fix numbers for the
#: life of the unit. Moving it retires that fold's savepoints to a cold fold, which
#: costs one refold each and is the only in-product way to retire them, since the tree
#: is fenced from the agent.
#: ``test_changing_what_a_fold_stores_forces_the_savepoint_version_to_move`` pins each
#: fold's stored state against its own version, so forgetting the move fails CI rather
#: than shipping.
#:
#: THE NUMBER IS PER FOLD (:attr:`_Fold.state_version`), and this is the value a fold
#: that has never moved still stands at. A savepoint file records the version of the
#: fold it holds, so a bump retires THAT fold's files and leaves every other fold's
#: standing -- where one shared number retired all six for a change to one of them,
#: and the sessions paying for it were the long ones the savepoints exist for.
_FOLD_STATE_VERSION_BASE: Final[int] = 4

#: The types these folds can interpret, handed to ``iter_from(known=...)`` so an
#: entry from a newer writer stops the fold instead of skewing it. The set is the
#: DECLARED session vocabulary rather than the types these folds branch on: a
#: declared type these folds ignore is a fact they chose not to use, while an
#: undeclared one is a fact they do not know exists, and only the second can
#: change what the entries after it mean.
KNOWN_TYPES: Final[frozenset[str]] = frozenset(SESSION_ENTRY_TYPES)


# --------------------------------------------------------------------------- #
# The fold surface
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Projection:
    """One fold's rendered value at a stated version.

    ``seq`` is the crew log's seq the value was folded through, which is what
    makes two projections comparable and what a reconnecting client truncates
    against (FR-5).

    ``revision`` is what ORDERS two values of the same (key, fold), and it exists because
    ``seq`` cannot. A slot fold's ``seq`` is the newest unit's own
    (:func:`_slot_checkpoint`), and a slot's units are folded in a fixed order -- so a
    conductor-side change on a board with any worker bound leaves that number unmoved,
    and a client ordering by it would discard the changed value. A session fold's ``seq``
    restarts when its unit is recreated under the same id. The revision is minted by the
    process that folded (:func:`_next_slot_revision`, one counter for both families),
    never read off a file, so a new unit, a recreated unit and a rewritten prefix all
    move it forward. ``0`` means no revision is vouched for: a fold read outside a warm
    memo, or one that has folded nothing.
    """

    name: str
    seq: int
    value: dict[str, Any]
    revision: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "seq": self.seq,
            "value": self.value,
            "revision": self.revision,
        }


@dataclass(frozen=True)
class _Fold:
    """One projection's three pure pieces.

    ``bind_slot`` is the fourth piece a SLOT-keyed fold has: the reader knows
    which slot it is folding and says so before the first entry, so the fold
    never has to infer its board from whichever entry happens to come first.

    ``affects`` and ``copy_state`` are what let a MUTATING ``step`` serve as the
    projection kernel's pure ``apply`` (:class:`_SessionFold`). ``step`` edits the
    dict it is handed, and the kernel requires a new object on a real change and the
    SAME object on none -- so ``apply`` copies first and steps the copy, and skips
    both when the entry cannot touch this fold.

    ``affects`` is the set of entry types whose ``step`` can change this fold's
    state, or ``None`` for a fold every entry moves. It may be WIDER than the truth
    and must never be narrower: a type wrongly included costs a copy and, for a
    client watching the change feed, one frame for an entry that changed nothing,
    while a type wrongly left out drops a real change and serves a stale value with
    nothing raised.

    ``copy_state`` must copy every container ``step`` can reach, transitively --
    ``None`` falls back to a deep copy, which is always correct and pays for the
    whole state. A shallower copy is what makes the per-entry cost bounded, and
    ``test_a_fold_never_reaches_into_the_state_it_was_handed`` is what keeps it
    honest: a nested container left shared shows up there as the prior state moving.

    ``state_version`` is the version of what THIS fold stores, and it is the number
    its savepoint files carry. It is per fold so that retiring one fold's stored
    meaning costs a cold fold to that fold alone -- see
    :data:`_FOLD_STATE_VERSION_BASE` for the rule that moves it.

    ``mode`` decides WHEN the fold runs, and ``"eager"`` IS THE DEFAULT. An eager fold
    is advanced off the append path, so a reader is served a value that was already
    current (:mod:`kiro_crew.crew_log.eager`); a lazy one is folded when a reader asks,
    which is the posture every fold had before and the one a reader pays a walk of the
    whole log for. The default is eager because that is the posture a dashboard wants
    and because leaving it implicit is what let the other folds stay lazy by nobody
    having decided -- so ``"lazy"`` now costs a ``lazy_reason``, and a fold with no
    reason to be lazy is eager without anyone writing the word.

    ``lazy_reason`` is that one line, REQUIRED of a lazy fold and refused on an eager
    one. Required because "lazy" is the exception and an exception with no stated ground
    is indistinguishable from an oversight; refused on an eager fold because a reason
    sitting on one describes a posture this registry does not hold.

    An eager fold must also declare ``affects``, spelled out even when it is every type:
    the worker skips the copy and the step for an entry a fold does not name, and a
    ``None`` there would leave a reader unable to tell "all of them" from "not decided".
    Both are checked here, at import, because a registry the process cannot honour is
    not a thing to discover under load.

    ``count_rows`` is how much this fold's state RETAINS, as a count of rows rather than
    a count of bytes, and it is what the warm memo's byte budget charges a cell by
    (:func:`slot_fold_cell_bytes`). It must count every container the state keeps,
    INCLUDING a list nested inside each item -- that nesting is why no single
    measured-at-caps figure can bound these states. It must stay O(the item cap): a few
    ``len`` calls and one pass over the items, never a serialization.

    ``count_opaque`` counts what ``count_rows`` cannot charge by a row cost: a retained
    value the fold keeps WHOLE and does not clamp, so the only bound on it is the entry
    that carried it. Each one is charged :data:`MAX_ENTRY_BYTES`. Same O(the item cap)
    rule, no serialization. ``work`` has three: an item's ``acceptance`` (the one field the
    store does not cap), its ``artifacts`` map (values unclamped), and each parked entry.

    ``None`` means the fold declares no row count, and a cell of it is charged the
    one-row floor. That is right for a SESSION-keyed fold, which has no warm SLOT cell at
    all -- its own memo weighs it by serialized size (:func:`session_fold_state_bytes`);
    a slot-keyed fold without one is refused at import, because the budget would
    then be charging it a constant while its state grows.
    """

    name: str
    start: Callable[[], dict[str, Any]]
    step: Callable[[dict[str, Any], Entry], None]
    render: Callable[[dict[str, Any]], dict[str, Any]]
    bind_slot: Callable[[dict[str, Any], str], None] | None = None
    affects: frozenset[str] | None = None
    copy_state: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    state_version: int = _FOLD_STATE_VERSION_BASE
    mode: Literal["eager", "lazy"] = "eager"
    lazy_reason: str = ""
    count_rows: Callable[[Mapping[str, Any]], int] | None = None
    count_opaque: Callable[[Mapping[str, Any]], int] | None = None

    def __post_init__(self) -> None:
        if self.mode == "eager" and self.affects is None:
            raise ValueError(
                f"the {self.name} fold is eager with no affects set: an eager fold is "
                "woken by entry type, so one that every entry moves would fold on every "
                "entry in the log off the append path"
            )
        if self.mode == "eager" and self.lazy_reason:
            raise ValueError(
                f"the {self.name} fold is eager and carries a lazy_reason: a reason left "
                "behind a flip describes a posture this registry no longer holds"
            )
        if self.mode == "lazy" and not self.lazy_reason:
            raise ValueError(
                f"the {self.name} fold is lazy with no lazy_reason: eager is the default, "
                "so staying lazy is an exception and an exception with no stated ground "
                "cannot be told apart from an oversight"
            )

    def touched_by(self, entry: Entry) -> bool:
        """Whether *entry* can move this fold, so a copy is worth making."""
        return self.affects is None or entry.type in self.affects

    def touched_by_type(self, entry_type: str) -> bool:
        """:meth:`touched_by` for a caller holding only the TYPE, not the entry.

        The eager path decides which folds a committed entry wakes before it has read
        the entry back, so it has the type and nothing else. Same answer as
        :meth:`touched_by`, from the same set, rather than a second membership test that
        could drift from it.
        """
        return self.affects is None or entry_type in self.affects

    def copied(self, state: dict[str, Any]) -> dict[str, Any]:
        """*state* copied deeply enough that :attr:`step` cannot reach the original."""
        if self.copy_state is None:
            return copy.deepcopy(state)
        return self.copy_state(state)
