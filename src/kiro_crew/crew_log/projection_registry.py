"""The fold registry, ``_FOLDS``: one :class:`~kiro_crew.crew_log.projection._Fold` per
registered name, what each one's ``step`` touches, how its state is copied and how many
rows it retains, and the eager or lazy posture that follows. The checks at the bottom run
at import, because a registry the process cannot honour is not a thing to discover
under load.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Final, cast

from kiro_crew.crew_log.entry_types import (
    DASHBOARD_AGENTIC_ENTRY_TYPE,
    DASHBOARD_FOLD_NAME,
    DASHBOARD_REFUSED_ENTRY_TYPE,
    GOAL_ENTRY_TYPE,
    GOAL_FOLD_NAME,
    MISTAKES_FOLD_NAME,
    PANEL_ENTRY_TYPE,
    PANEL_FOLD_NAME,
    RADAR_ENTRY_TYPE,
    WAITING_ENTRY_TYPE,
    WAITING_FOLD_NAME,
    WORK_ENTRY_TYPE,
)
from kiro_crew.crew_log.projection_catalogue import (
    _FOLD_STATE_VERSION_BASE,
    FOLD_NAMES,
    KNOWN_TYPES,
    SESSION_FOLD_NAMES,
    SLOT_PROJECTION_NAMES,
    TREE_PROJECTION_NAMES,
    WORKSTREAMS_FOLD_NAME,
    WORKTREE_FOLD_NAME,
    _Fold,
)
from kiro_crew.crew_log.projection_crewmate import (
    _goal_render,
    _goal_rows,
    _goal_start,
    _goal_step,
    _waiting_render,
    _waiting_rows,
    _waiting_start,
    _waiting_step,
)
from kiro_crew.crew_log.projection_dashboard import (
    _agentic_render,
    _agentic_rows,
    _agentic_start,
    _agentic_step,
    _mistakes_render,
    _mistakes_rows,
    _mistakes_start,
    _mistakes_step,
    _panel_render,
    _panel_start,
    _panel_step,
)
from kiro_crew.crew_log.projection_internal import (
    OUTLINE_TYPES,
    _class_render,
    _class_start,
    _class_step,
    _outline_copy,
    _outline_render,
    _outline_start,
    _outline_step,
)
from kiro_crew.crew_log.projection_ledger import _ledger_render, _ledger_start, _ledger_step
from kiro_crew.crew_log.projection_radar import _radar_render, _radar_start, _radar_step
from kiro_crew.crew_log.projection_side_panel import (
    TIMELINE_TYPES,
    _approvals_render,
    _approvals_start,
    _approvals_step,
    _status_render,
    _status_start,
    _status_step,
    _timeline_render,
    _timeline_start,
    _timeline_step,
    _tools_render,
    _tools_start,
    _tools_step,
)
from kiro_crew.crew_log.projection_subagents import (
    SUBAGENT_TYPES,
    _subagents_render,
    _subagents_start,
    _subagents_step,
)
from kiro_crew.crew_log.projection_usage import (
    _CREDIT_SOURCE_OF,
    _usage_render,
    _usage_start,
    _usage_step,
)
from kiro_crew.crew_log.projection_values import _rows_in
from kiro_crew.crew_log.projection_work import (
    _work_bind_slot,
    _work_render,
    _work_start,
    _work_step,
)
from kiro_crew.crew_log.projection_workstreams import (
    _workstreams_bind_slot,
    _workstreams_copy,
    _workstreams_render,
    _workstreams_rows,
    _workstreams_start,
    _workstreams_step,
)
from kiro_crew.crew_log.projection_worktree import (
    _worktree_bind_slot,
    _worktree_copy,
    _worktree_render,
    _worktree_rows,
    _worktree_start,
    _worktree_step,
)

# The ledger fold reads a record whose semantics -- which phases end a workstream,
# which event kinds exist, how much of each field is kept -- belong to the ledger
# subsystem. They are imported rather than restated so one owner sets them, the
# same direction ``store`` already takes for the store-name fold.
from kiro_crew.session_ledger import LEDGER_ENTRY_TYPE

# --------------------------------------------------------------------------- #
# What each fold touches, and what its copy has to cover
# --------------------------------------------------------------------------- #
#
# A session fold's ``step`` mutates the dict it is handed, and the projection kernel
# drives a pure ``apply`` (:class:`_SessionFold`), so each fold declares two things
# the wrapper needs. They are declared TOGETHER rather than beside their own folds
# because they are read against each other: the question a reviewer asks is whether
# one fold's copier covers every container its step reaches, and the answer is easier
# to see with the six side by side than scattered through two thousand lines.
#
# A copier covers the containers a step MUTATES, not every container in the state. A
# value the step only ever REPLACES whole -- ``status``'s ``open_turn``, ``approvals``'
# ``last``, a frame put into ``open`` or ``pending`` -- needs no copy of its own: the
# old object is dropped rather than edited, so the state it came from keeps it intact.


#: Entry types ``usage`` bills. A turn's cost, the context composed for it,
#: compaction, step time -- and the two other things that spend this session's
#: budget without being one of its turns: the children it dispatched and the
#: background helpers the gateway ran on its behalf. Billing turns alone reads a
#: session that spent most of its budget on a wave of subagents as cheap.
USAGE_TYPES: Final[frozenset[str]] = frozenset(
    {
        # Not a cost entry either, and it is here for the per-turn OCCUPANCY stamp:
        # it is the only entry that marks where a unit begins, and turn ordinals
        # restart in each unit of a slot. Without it the stamp's reach-back cannot
        # tell an earlier unit's same-ordinal turn from an earlier attempt of the
        # turn now closing.
        "session/opened",
        "turn/completed",
        # A refused turn never ran, so it carries no cost and no occupancy -- it is
        # here only so the ordinal counter sees it. A dispatch gate refusing a turn
        # (an oversized or blocked ``@prompt``, a superseded replay) is ordinary
        # operation, and the refusal is the turn's ONLY durable entry: it emits no
        # ``turn/completed`` and composes nothing. Without it the counter never
        # accounts for that turn, so every later composition in the unit reads an
        # ordinal one low per refusal -- the panel's turn number and its "earlier
        # turns not shown" count both drift down and never self-correct.
        "turn/refused",
        "context/composed",
        "compaction/applied",
        "step/completed",
        "subagent/completed",
        "subagent/failed",
        "background/completed",
        # Not a cost entry, and it is here for the OCCUPANCY pair: the window size
        # a prompt was measured against lives on this entry and on no other, and a
        # per-turn context row without it reports a numerator with no denominator.
        # It is also the only entry written BEFORE the prompt that names the model
        # the prompt was composed for -- ``turn/completed`` names it afterwards, so
        # stamping a context row from that one would date each row by the next
        # turn's configuration.
        "request/configured",
    }
)

#: Entry types ``tools`` pairs: a call and the completion that closes it.
TOOL_TYPES: Final[frozenset[str]] = frozenset({"tool/called", "tool/completed"})

#: Every entry type the ``workstreams`` fold reads: the work record, the opener that
#: names a unit's slot, and the three closers that carry a charge.
#:
#: HERE, beside what every other fold touches, rather than beside that fold's own
#: constants, because it is spelled FROM the ``usage`` fold's billing table -- a
#: fourth spender added to :data:`_CREDIT_SOURCE_OF` is then read by the report fold
#: without a second edit, which is the whole point of deriving it.
WORKSTREAMS_TYPES: Final[frozenset[str]] = frozenset(
    {WORK_ENTRY_TYPE, "session/opened"} | set(_CREDIT_SOURCE_OF)
)

#: Entry types ``approvals`` pairs: a request and the decision that answers it.
APPROVAL_TYPES: Final[frozenset[str]] = frozenset({"approval/requested", "approval/decided"})


def _flat_copy(state: dict[str, Any]) -> dict[str, Any]:
    """A copy for a fold whose step writes only top-level keys.

    ``status`` and ``class``. Every nested value either is a scalar or is replaced
    whole, so nothing under the top level is ever edited in place.
    """
    return dict(state)


def _timeline_copy(state: dict[str, Any]) -> dict[str, Any]:
    """``moments`` is appended to and trimmed from the front; a moment is never edited."""
    grown = dict(state)
    grown["moments"] = list(state["moments"])
    return grown


def _usage_copy(state: dict[str, Any]) -> dict[str, Any]:
    """Per-dimension, per-model and per-source rows are all incremented in place."""
    grown = dict(state)
    grown["tokens"] = dict(state["tokens"])
    grown["credits_by_source"] = {
        source: dict(row) for source, row in state["credits_by_source"].items()
    }
    grown["by_model"] = {model: dict(row) for model, row in state["by_model"].items()}
    grown["context_by_source"] = {
        source: dict(row) for source, row in state["context_by_source"].items()
    }
    # Appended to, trimmed from the front, and a row is REPLACED when its turn's
    # occupancy arrives -- so only the LIST is rebuilt. The row dicts are shared with
    # the snapshot rather than copied, which is what keeps this O(window) instead of
    # O(window x sources), and that sharing is exactly why the occupancy stamp in
    # ``_usage_step`` assigns ``rows[index] = {**row, ...}`` instead of writing into a
    # row: an in-place write would reach a projection already handed to a reader.
    grown["context_turns"] = list(state["context_turns"])
    grown["omitted_models"] = list(state["omitted_models"])
    # ``context_turn_keys`` needs no copy: it is a str, which ``_count_turn_ordinal``
    # REPLACES rather than mutates, so the snapshot keeps the value it was handed.
    return grown


def _tools_copy(state: dict[str, Any]) -> dict[str, Any]:
    """A per-name row is incremented, and its two server lists are appended to.

    The deepest copy of the six, and still bounded: ``TOOL_NAME_LIMIT`` rows each
    holding at most ``SERVERS_PER_TOOL_LIMIT`` names, plus ``OPEN_RETAIN_LIMIT``
    frames whose dict is rebuilt but whose frames are only ever added and removed.
    """
    grown = dict(state)
    grown["by_name"] = {
        name: {
            **row,
            "servers": list(row["servers"]),
            "servers_over": list(row["servers_over"]),
        }
        for name, row in state["by_name"].items()
    }
    grown["open"] = dict(state["open"])
    grown["omitted_names"] = list(state["omitted_names"])
    return grown


def _approvals_copy(state: dict[str, Any]) -> dict[str, Any]:
    """``pending`` gains and loses frames, and ``by_decision`` counts per decision."""
    grown = dict(state)
    grown["pending"] = dict(state["pending"])
    grown["by_decision"] = dict(state["by_decision"])
    return grown


def _subagents_copy(state: dict[str, Any]) -> dict[str, Any]:
    """A per-child row is filled in by its closer, and ``totals`` by every branch.

    Those are the only two containers the step reaches: the row dict for the child a
    closer names, and the ``totals`` dict every branch increments. Every value inside a
    row is a scalar, so the row's shallow copy covers it. Bounded by
    ``OPEN_RETAIN_LIMIT`` rows of fixed width.
    """
    grown = dict(state)
    grown["by_id"] = {agent_id: dict(row) for agent_id, row in state["by_id"].items()}
    grown["totals"] = dict(state["totals"])
    return grown


# --------------------------------------------------------------------------- #
# What each slot fold RETAINS, counted in rows
# --------------------------------------------------------------------------- #
#
# One counter per slot-keyed fold, for the warm memo's byte budget
# (:func:`slot_fold_cell_bytes`). Each counts every container its fold keeps, and the
# NESTED ones are the whole reason these exist: a per-cell figure measured at the caps
# would miss them and would not be an upper bound.
#
# Each is O(the fold's own item cap) and allocates nothing.


def _ledger_rows(state: Mapping[str, Any]) -> int:
    """``ledger``: flat. Rejected approaches, events, artifact pointers."""
    return (
        _rows_in(state.get("tried"))
        + _rows_in(state.get("events"))
        + _rows_in(state.get("artifacts"))
    )


def _radar_rows(state: Mapping[str, Any]) -> int:
    """``radar``: the widest, and the one with TWO nested containers.

    Each item keeps its own ``tried`` list (:data:`RADAR_TRIED_LIMIT`) and ``phase_lines``
    holds one list per item (:data:`RADAR_PHASE_LINE_LIMIT`), so the cap-state is
    500 x 100 plus 500 x 200 rows -- about 156,500 in total, where the items and skips
    alone are 5,500. Counting only the top level would under-charge a full cell by 28x.
    """
    items = state.get("items")
    rows = _rows_in(items)
    if isinstance(items, dict):
        for item in items.values():
            if isinstance(item, Mapping):
                rows += _rows_in(item.get("tried"))
    phase_lines = state.get("phase_lines")
    if isinstance(phase_lines, dict):
        for lines in phase_lines.values():
            rows += _rows_in(lines)
    return (
        rows
        + _rows_in(state.get("skips"))
        + _rows_in(state.get("events"))
        + _rows_in(state.get("last_update"))
    )


def _work_rows(state: Mapping[str, Any]) -> int:
    """``work``: each item keeps its own event tail (:data:`WORK_EVENT_LIMIT`).

    Only the CLAMPED part. ``acceptance``, ``artifacts`` and the parked entries are kept
    whole, so a row cost cannot bound them; :func:`_work_opaque` charges those.
    """
    items = state.get("items")
    rows = _rows_in(items)
    if isinstance(items, dict):
        for item in items.values():
            if isinstance(item, Mapping):
                rows += _rows_in(item.get("events"))
    return (
        rows
        + _rows_in(state.get("parked"))
        + _rows_in(state.get("order"))
        + _rows_in(state.get("closed"))
        + _rows_in(state.get("tombstones"))
    )


def _work_opaque(state: Mapping[str, Any]) -> int:
    """``work``: the values it keeps whole, each bounded only by the entry that set it.

    An item's ``acceptance`` is the one field the store does not cap, and its
    ``artifacts`` map keeps every value as given; a report or an ``accept`` replaces each
    WHOLE, so each is at most one entry. A parked entry is a whole entry's data. An empty
    container is not counted: it holds nothing the header row does not already cover.
    """
    count = 0
    items = state.get("items")
    if isinstance(items, dict):
        for item in items.values():
            if isinstance(item, Mapping):
                count += bool(item.get("acceptance")) + bool(item.get("artifacts"))
    parked = state.get("parked")
    if isinstance(parked, dict):
        for held in parked.values():
            count += _rows_in(held)
    return count


def _panel_rows(state: Mapping[str, Any]) -> int:
    """``panel``: each owner keeps one whole published document and its history tail.

    The document is bounded by :data:`~kiro_crew.crew_log.schema.MAX_ENTRY_BYTES` rather
    than by a fold cap, and it is what makes an OWNER the widest row this fold has -- so
    an owner counts as one row and is charged that width.
    """
    owners = state.get("owners")
    rows = _rows_in(owners)
    if isinstance(owners, dict):
        for owner in owners.values():
            if isinstance(owner, Mapping):
                rows += _rows_in(owner.get("history"))
    return rows


_FOLDS: Final[dict[str, _Fold]] = {
    # ``affects`` is SPELLED OUT for both of these even though it is every type this
    # module knows, because a fold that genuinely consumes the whole vocabulary should
    # say so rather than leave a reader to read ``None`` as either "all of them" or "not
    # decided yet". ``status`` counts entries and keeps the newest time; ``class``
    # records the seq it saw so a gap in the history reads as damage. The wide set is
    # also why EVERY committed entry wakes the eager folder (``eager.note_commit``).
    "status": _Fold(
        "status",
        _status_start,
        _status_step,
        _status_render,
        affects=KNOWN_TYPES,
        copy_state=_flat_copy,
    ),
    "usage": _Fold(
        "usage",
        _usage_start,
        _usage_step,
        _usage_render,
        affects=USAGE_TYPES,
        copy_state=_usage_copy,
        # Moved off the base for the per-turn context window and the occupancy pair,
        # and moved again for the monotonic ordinal counter this fold now keeps. That
        # counter advances over the TURN HISTORY -- a turn that composed nothing still
        # consumes an ordinal (``context_turns_seq`` plus the memo
        # that records which turns were counted), and a REFUSED turn does too now
        # that the fold sees ``turn/refused`` -- so a savepoint from a build whose
        # counter only moved on compositions, or on completions but not refusals, would
        # resume with a differently-numbered sequence; the bump retires THIS fold's
        # files to a cold fold and leaves the others' standing. Moved again because a new
        # unit now CLEARS the model/window stamps on ``session/opened``: a savepoint from
        # a build that carried a prior unit's stamp into the next unit would resume still
        # carrying it. Moved once more because the counter now REMEMBERS every counted
        # ``(unit, turn)`` in a map rather than only the most recent, so a rewound or
        # edit-resent turn reuses its ordinal instead of taking a new one; a savepoint
        # written with the old scalar companion carries no such map and must refold cold.
        # Moved again because the key's unit is now ``context_key_unit``, which a
        # re-attachment does not advance; a savepoint keyed by the per-attachment counter
        # would hand a regenerated turn a second ordinal.
        # Moved again for ``context_sources_omitted``: a savepoint without the counter
        # would resume short of it, and the count it renders would be wrong for good.
        # Moved once more because the memo is now ``context_turn_keys``, one string
        # covering the whole rewindable range; a savepoint holding the old bounded map
        # carries neither the new field nor the turns that map had already evicted.
        # Moved again for a COUNTING fix that keeps every key: ``tokens_turns`` counts
        # a block of four zeros as unreported, and a savepoint from a build that
        # counted it as a report would resume onto this logic still carrying the
        # inflated count for the life of the unit.
        # Moved again for ``context_started`` and the per-row ``first_start``: a
        # savepoint without them would mark a later rebuild as the session's start.
        state_version=_FOLD_STATE_VERSION_BASE + 11,
    ),
    # The panel's feed section reads this one, so it is pushed like the rest; its value
    # is bounded by ``TIMELINE_LIMIT`` whatever the session's length.
    "timeline": _Fold(
        "timeline",
        _timeline_start,
        _timeline_step,
        _timeline_render,
        affects=TIMELINE_TYPES,
        copy_state=_timeline_copy,
        # The state carries ``last_seq`` now, so a savepoint from a build that stored
        # only ``moments`` describes an older shape and resumes onto this logic blind
        # to the unit boundary -- retire it.
        state_version=_FOLD_STATE_VERSION_BASE + 1,
    ),
    "tools": _Fold(
        "tools",
        _tools_start,
        _tools_step,
        _tools_render,
        affects=TOOL_TYPES,
        copy_state=_tools_copy,
    ),
    "approvals": _Fold(
        "approvals",
        _approvals_start,
        _approvals_step,
        _approvals_render,
        affects=APPROVAL_TYPES,
        copy_state=_approvals_copy,
    ),
    "subagents": _Fold(
        "subagents",
        _subagents_start,
        _subagents_step,
        _subagents_render,
        affects=SUBAGENT_TYPES,
        copy_state=_subagents_copy,
        # Three past the base, one step per change to what this fold STORES: it dropped
        # its own credits aggregate once ``usage`` was found to fold the same number from
        # the same closers, then its duration aggregate once no reader could be named for
        # it, then it gained everything the panel needs to rebuild a child's card from
        # this fold after the dispatching process is gone -- ``task`` and ``started_ms``
        # per row, and a ``dismissed`` flag per row so a card the user cleared stays
        # cleared. Those arrived together and are ONE step: the number
        # retires the savepoints written under the shape before it, and a shape that
        # changed once needs retiring once. Every sibling stays at the base, because a
        # bump here costs a cold fold to this fold alone -- see
        # :data:`_FOLD_STATE_VERSION_BASE`.
        state_version=_FOLD_STATE_VERSION_BASE + 3,
    ),
    "class": _Fold(
        "class",
        _class_start,
        _class_step,
        _class_render,
        affects=KNOWN_TYPES,
        copy_state=_flat_copy,
    ),
    # Keyed by one SESSION and NOT advertised -- the two choices are argued where
    # :data:`INTERNAL_PROJECTION_NAMES` names it. ``affects`` is
    # :data:`OUTLINE_TYPES`, whose ABSENCES are the fold's contract: no
    # ``context/composed`` and no ``tool/*``, so no row can carry an injected block or
    # tool output. ``copy_state`` is spelled out because this is the one internal fold
    # woken by the message entries that are a log's bulk, and the deep-copy fallback
    # would charge each of them a copy of the whole window.
    "outline": _Fold(
        "outline",
        _outline_start,
        _outline_step,
        _outline_render,
        affects=OUTLINE_TYPES,
        copy_state=_outline_copy,
    ),
    # The SLOT-keyed folds each answer to exactly ONE entry type -- their ``step``
    # returns on its first line for anything else -- so ``affects`` names that type and
    # the kernel skips both the copy and the step for every other entry. A slot's log
    # is mostly message bodies and tool rows, so that is nearly all of it. They declare
    # no ``copy_state`` and fall back to the deep copy, which every fold state here is
    # bounded by construction for.
    #
    # ALL FOUR ARE EAGER, which is the default and so is written nowhere below. Each is
    # read by a loop that wakes on a timer and each wakes the worker for one entry type
    # in a log that is otherwise message bodies, so the read it serves is a memo lookup
    # rather than a walk of every unit the slot ran under. What their four warm cells
    # cost resident is :data:`SLOT_FOLD_CACHE_BYTES`' problem, not this table's.
    "ledger": _Fold(
        "ledger",
        _ledger_start,
        _ledger_step,
        _ledger_render,
        affects=frozenset({LEDGER_ENTRY_TYPE}),
        count_rows=_ledger_rows,
    ),
    "radar": _Fold(
        "radar",
        _radar_start,
        _radar_step,
        _radar_render,
        affects=frozenset({RADAR_ENTRY_TYPE}),
        count_rows=_radar_rows,
    ),
    "work": _Fold(
        "work",
        _work_start,
        _work_step,
        # ONE cast, here, because ``_work_render`` promises a TypedDict while this
        # registry field asks for ``dict[str, Any]``. mypy refuses that assignment even
        # though it holds at runtime: a TypedDict is assignable to a read-only mapping
        # but not to a mutable ``dict[str, V]``, which is invariant in V. Widening the
        # field to ``Mapping`` instead pushes the same refusal onto ``Projection.value``
        # and two ``view`` methods, so it would cost three shared types rather than one
        # line. Safe in the direction that matters: the registry only CALLS this, and a
        # caller wanting the checked shape reads ``_work_render``'s own annotation.
        cast("Callable[[dict[str, Any]], dict[str, Any]]", _work_render),
        bind_slot=_work_bind_slot,
        affects=frozenset({WORK_ENTRY_TYPE}),
        count_rows=_work_rows,
        count_opaque=_work_opaque,
        # Two past the base. The state carries ``closed``, ``tombstones`` and
        # ``forgotten``, and a closed item past the window leaves ``items``: a
        # savepoint of the shape that holds every item would resume holding them.
        # Each item carries ``pending_since``, which a savepoint without it would
        # resume short of.
        state_version=_FOLD_STATE_VERSION_BASE + 2,
    ),
    PANEL_FOLD_NAME: _Fold(
        PANEL_FOLD_NAME,
        _panel_start,
        _panel_step,
        _panel_render,
        affects=frozenset({PANEL_ENTRY_TYPE}),
        count_rows=_panel_rows,
    ),
    # The two dashboard folds, EAGER like their four siblings and for the same
    # reason: each answers one entry type in a log that is otherwise message bodies,
    # so the read they serve is a memo lookup rather than a walk of every unit the
    # slot ran under. That matters more here than anywhere else in this table --
    # part 3 of the dynamic dashboard subscribes to these through the bus with a
    # baseline, and its whole cost contract is O(fields) per read and never O(log).
    DASHBOARD_FOLD_NAME: _Fold(
        DASHBOARD_FOLD_NAME,
        _agentic_start,
        _agentic_step,
        _agentic_render,
        affects=frozenset({DASHBOARD_AGENTIC_ENTRY_TYPE}),
        count_rows=_agentic_rows,
    ),
    MISTAKES_FOLD_NAME: _Fold(
        MISTAKES_FOLD_NAME,
        _mistakes_start,
        _mistakes_step,
        _mistakes_render,
        affects=frozenset({DASHBOARD_REFUSED_ENTRY_TYPE}),
        count_rows=_mistakes_rows,
    ),
    # The project report's fold, EAGER like every slot-keyed sibling. It is the widest
    # ``affects`` of the slot folds -- a work record, the unit opener and the three
    # closers that carry a charge -- which is the cost of answering "what did each task
    # cost" at all: the spend is in the worker's ``usage`` entries and nowhere else.
    # Still bounded per entry: the step is a dict write and the copy is spelled out
    # (:func:`_workstreams_copy`), so no read walks the log and no entry pays a deep
    # copy of three nested levels.
    WORKSTREAMS_FOLD_NAME: _Fold(
        WORKSTREAMS_FOLD_NAME,
        _workstreams_start,
        _workstreams_step,
        _workstreams_render,
        bind_slot=_workstreams_bind_slot,
        affects=WORKSTREAMS_TYPES,
        copy_state=_workstreams_copy,
        count_rows=_workstreams_rows,
    ),
    # A crewmate's own record, the two folds its Dashboard page binds for the goal and
    # for what waits on the person. Each entry is a whole snapshot the gateway appends
    # when the fact changes, so the step replaces top-level keys only and a flat copy
    # covers it.
    GOAL_FOLD_NAME: _Fold(
        GOAL_FOLD_NAME,
        _goal_start,
        _goal_step,
        _goal_render,
        affects=frozenset({GOAL_ENTRY_TYPE}),
        copy_state=_flat_copy,
        count_rows=_goal_rows,
    ),
    WAITING_FOLD_NAME: _Fold(
        WAITING_FOLD_NAME,
        _waiting_start,
        _waiting_step,
        _waiting_render,
        affects=frozenset({WAITING_ENTRY_TYPE}),
        copy_state=_flat_copy,
        count_rows=_waiting_rows,
    ),
    # The TREE-keyed fold, the third key kind, argued where
    # :data:`TREE_PROJECTION_NAMES` names it. ``affects`` is the one work entry type:
    # the nodes are boards and the edges are items, and both are recorded there.
    #
    # LAZY, and it is the only lazy fold in this registry -- which is why
    # :attr:`_Fold.lazy_reason` is required rather than optional. Eager would mean a
    # warm cell, and a tree cell is invalidated by growth in ANY member's log,
    # including a member the cell did not know existed when it was built; no publisher
    # raises that event, so an eager tree fold would be woken for the root's own log
    # and then serve a tree missing every change made in a child's.
    #
    # ``count_rows`` is declared even though a lazy fold holds no cell, because the
    # tree guard (:data:`_UNMEASURED_TREE_FOLDS`) requires it of a tree fold for the
    # reason the slot guard requires it of a slot fold: a charge that cannot see the
    # row count is a constant, and the figure a composing agent reads out of the
    # catalog has to be true now rather than when somebody later gives this kind a cell.
    WORKTREE_FOLD_NAME: _Fold(
        WORKTREE_FOLD_NAME,
        _worktree_start,
        _worktree_step,
        _worktree_render,
        bind_slot=_worktree_bind_slot,
        affects=frozenset({WORK_ENTRY_TYPE}),
        copy_state=_worktree_copy,
        count_rows=_worktree_rows,
        mode="lazy",
        lazy_reason=(
            "a tree joins the logs of many slots, so its value is stale when ANY "
            "member's log grows -- including a member not known when the value was "
            "built -- and no publisher raises that event. Read on a page load and on a "
            "refetch instead, which the controller's gap rule already covers."
        ),
    ),
}

if tuple(_FOLDS) != FOLD_NAMES:  # pragma: no cover - import-time consistency
    raise RuntimeError(
        "the fold registry and FOLD_NAMES disagree: " f"{tuple(_FOLDS)} against {FOLD_NAMES}"
    )

#: The eager folds, resolved once at import. Eager is the default, so this is every fold
#: that did not write down why it is not.
EAGER_FOLD_NAMES: Final[tuple[str, ...]] = tuple(
    name for name, fold in _FOLDS.items() if fold.mode == "eager"
)

#: The eager folds split by WHAT THEY ARE KEYED BY, because each half has its own warm
#: path and the eager folder drives the two differently. A SLOT fold joins every unit the
#: slot ran under and is continued in the slot memo (:func:`fold_slot_warm_revised`); a
#: SESSION fold reads one unit's file and is continued in the session memo
#: (:func:`fold_session_warm`). Both mint their revision from the same counter, so the
#: consumer rule "keep the highest revision per (key, fold)" is one rule.
EAGER_SLOT_FOLD_NAMES: Final[tuple[str, ...]] = tuple(
    name for name in EAGER_FOLD_NAMES if name in SLOT_PROJECTION_NAMES
)
EAGER_SESSION_FOLD_NAMES: Final[tuple[str, ...]] = tuple(
    name for name in EAGER_FOLD_NAMES if name in SESSION_FOLD_NAMES
)

#: The exceptions, each beside the one line that earns it. A caller auditing the posture
#: reads this rather than inverting :data:`EAGER_FOLD_NAMES`, because the absence of a
#: name says nothing about why. EMPTY today: every fold has a reader that wants it
#: current, and none has a cost that outweighs that.
LAZY_FOLD_REASONS: Final[dict[str, str]] = {
    name: fold.lazy_reason for name, fold in _FOLDS.items() if fold.mode == "lazy"
}

#: Every eager fold has a warm path to be advanced along. A fold in neither family would
#: be woken for and then have nothing to continue, which is a silent lazy fold.
_ORPHAN_EAGER: Final[tuple[str, ...]] = tuple(
    name
    for name in EAGER_FOLD_NAMES
    if name not in EAGER_SLOT_FOLD_NAMES and name not in EAGER_SESSION_FOLD_NAMES
)
if _ORPHAN_EAGER:  # pragma: no cover - import-time
    raise RuntimeError(f"an eager fold has no warm path to advance: {list(_ORPHAN_EAGER)}")

#: A slot-keyed fold is the only kind that gets a warm cell, so it is the only kind the
#: byte budget charges -- and a charge that cannot see the state's row count is a
#: CONSTANT, which is what this change exists to stop being. Checked at import because a
#: fold added without a counter would be silently under-charged forever.
_UNCOUNTED_SLOT_FOLDS: Final[tuple[str, ...]] = tuple(
    name for name in SLOT_PROJECTION_NAMES if _FOLDS[name].count_rows is None
)
if _UNCOUNTED_SLOT_FOLDS:  # pragma: no cover - import-time
    raise RuntimeError(
        "a slot-keyed fold must declare count_rows, or the warm memo's byte budget "
        f"charges it a constant while its state grows: {list(_UNCOUNTED_SLOT_FOLDS)}"
    )

#: A fold must belong to EXACTLY ONE key kind. The three sets are what every consumer
#: routes on -- the manifest's binding gate, the bus scope, the catalog's ``keyed_by``,
#: the fold readers themselves -- and each of them tests them in its own order, so
#: a name in two sets would be routed one way by one consumer and another way by the
#: next, while a name in none is registered and unreachable.
_MISKEYED_FOLDS: Final[tuple[str, ...]] = tuple(
    name
    for name in FOLD_NAMES
    if sum(
        name in family
        for family in (SESSION_FOLD_NAMES, SLOT_PROJECTION_NAMES, TREE_PROJECTION_NAMES)
    )
    != 1
)
if _MISKEYED_FOLDS:  # pragma: no cover - import-time
    raise RuntimeError(
        "every registered fold must be in exactly one of SESSION_FOLD_NAMES, "
        f"SLOT_PROJECTION_NAMES or TREE_PROJECTION_NAMES: {list(_MISKEYED_FOLDS)}"
    )
