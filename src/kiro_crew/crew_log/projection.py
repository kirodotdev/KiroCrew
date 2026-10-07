"""Folds over ONE session's crew log -- the side panel's five views, and the ledger.

The panel reads five, and those five are what the growth push carries. ``class`` is a
sixth registered fold that is deliberately NOT advertised: its reader is
a session deciding whether it may read ANOTHER unit's log, which is why it is held at
the most restrictive value the log ever recorded rather than at the current one.

A projection folds one crew log and carries that log's ``seq`` as its version
(RFC FR-5), so a reader that holds a projection at seq N and reads the entries
after N reaches the same value a reader folding the whole file from scratch
does. That equality is the module's contract and the property its tests pin.

The INCREMENTAL form is the primitive here and the whole-file form wraps it. A
fold is three pure pieces -- a starting state, one step per entry, and a render
-- and :func:`fold` is those pieces run over every entry. A separate batch
implementation would be a second codepath that can disagree with the resumed
one about the same bytes, and nothing in the file would say which is right.

State is JSON-serializable and is the CHECKPOINT: a caller may store it, hand it
back later with the seq it was taken at, and continue. It is deliberately not
the rendered value. A fold keeps bookkeeping a reader has no use for (the open
tool calls it is matching by ``call_id``, the attempt an open turn is on), and
:func:`Checkpoint.state` holding exactly what the fold needs to continue is what
lets the render stay the surface the dashboard reads.
:mod:`kiro_crew.crew_log.checkpoint` writes that state beside the log, so a read
resumes where the last one stopped; this module owns no path and every failure
over there is answered by folding from seq 1 again.

Absent is never read as zero. ``turn/completed`` carries ``credits`` and
``tokens`` only when the provider reported them -- a synthesized closer omits
both, a measured one omits whichever went unreported -- and a total that counted
those turns as costing nothing would state a measurement nobody made. Each total
therefore rides beside the count of turns that contributed to it, and a caller
comparing the two learns what the total covers.

Nothing here synthesizes history. An interrupted turn and an unmatched tool call
are reported as OPEN, never closed with an invented outcome: closing them is the
store's ``repair=True``, which appends real deterministic closers under write
ownership, and a reader inventing the same fact in memory would make two readers
of one file disagree.

This module reads its own unit's file and nothing else (FR-4: no fold reads more
than its own crew log) -- with ONE stated exception, and it is stated because a
reader has to know which kind of fold it is holding. The ``ledger`` fold is keyed
by SLOT, and a slot owns one ACP session id at a time rather than for its whole
life, so the record it answers for is spread over a unit per id the slot ran
under. It therefore joins those units (:func:`fold_slot`), which is a wider read
than the six panel folds make and is why it is not one of them: the growth push
and the side panel address a session, and a slot-wide value pushed under one
session's id would report a partial answer as the whole one. The units it joins
are still exactly one slot's own, so nothing here reads across slots.

Resolving a ``ref`` is the PAGE path's work, in the routes that serve a person a
citation to follow.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import os
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Final, Literal, NamedTuple, cast

from kiro_crew.config.paths import data_home
from kiro_crew.context_blocks import PHASE_SESSION_START
from kiro_crew.crew_log.entry_types import (
    DASHBOARD_AGENTIC_ENTRY_TYPE,
    DASHBOARD_CODE_LIMIT,
    DASHBOARD_FIELD_LIMIT,
    DASHBOARD_FOLD_NAME,
    DASHBOARD_REASON_LIMIT,
    DASHBOARD_REFUSED_ENTRY_TYPE,
    DASHBOARD_TYPE_LIMIT,
    DASHBOARD_VALUE_BYTES,
    DASHBOARD_VALUE_LIMIT,
    MISTAKE_CORRECTED_CODE,
    MISTAKE_CORRECTION_LIMIT,
    MISTAKE_GROUP_LIMIT,
    MISTAKES_FOLD_NAME,
    PANEL_CREW_KEY_LIMIT,
    PANEL_ENTRY_TYPE,
    PANEL_FOLD_NAME,
    PANEL_HISTORY_LIMIT,
    PANEL_OWNER_LIMIT,
    PANEL_TEMPLATE_LIMIT,
    PANEL_TITLE_LIMIT,
    RADAR_CI_BOUNDS,
    RADAR_CI_KEYS,
    RADAR_CLEARABLE_FIELDS,
    RADAR_CREW_LEVEL_EVENT_KIND,
    RADAR_DEFAULT_SKIP_SCOPE,
    RADAR_EDITING_PHASES,
    RADAR_ENTRY_TYPE,
    RADAR_EVENT_KINDS,
    RADAR_LABELS_LIMIT,
    RADAR_NUMBER_BOUNDS,
    RADAR_PHASES,
    RADAR_SKIP_SCOPES,
    RADAR_TERMINAL_PHASES,
    SESSION_ENTRY_TYPES,
    WORK_ENTRY_TYPE,
)
from kiro_crew.crew_log.errors import CODE_BAD_DATA, CrewLogError
from kiro_crew.crew_log.schema import KIND_SESSION, MAX_ENTRY_BYTES, Entry
from kiro_crew.crew_log.session_tree import OpenedRecord, log_rank_of
from kiro_crew.crew_log.store import (
    CrewLog,
    log_exception_text,
    segment_paths,
    session_units_by_slot,
    session_units_for_slot,
    unit_header_created_at,
    unit_opened_previous,
)
from kiro_crew.projection import ProjectionRegistry, Savepoint, attribute_seq

if TYPE_CHECKING:
    # Type-only: the savepoint module imports this one, so a runtime import here
    # would close the cycle the function-local imports below exist to avoid.
    from kiro_crew.crew_log.checkpoint import PrefixWitness

# The ledger fold reads a record whose semantics -- which phases end a workstream,
# which event kinds exist, how much of each field is kept -- belong to the ledger
# subsystem. They are imported rather than restated so one owner sets them, the
# same direction ``store`` already takes for the store-name fold.
from kiro_crew import session_ledger
from kiro_crew.session_ledger import _FOLD_NAME as LEDGER_FOLD_NAME
from kiro_crew.session_ledger import _MAX_ARTIFACT_KEY as LEDGER_ARTIFACT_KEY_LIMIT
from kiro_crew.session_ledger import _MAX_ARTIFACTS as LEDGER_ARTIFACT_LIMIT
from kiro_crew.session_ledger import _MAX_EVENTS as LEDGER_EVENT_LIMIT
from kiro_crew.session_ledger import _MAX_PHASE as LEDGER_PHASE_LIMIT
from kiro_crew.session_ledger import _MAX_TEXT as LEDGER_TEXT_LIMIT
from kiro_crew.session_ledger import _MAX_TRIED as LEDGER_TRIED_LIMIT
from kiro_crew.session_ledger import EVENT_KINDS as LEDGER_EVENT_KINDS
from kiro_crew.session_ledger import LEDGER_ENTRY_TYPE
from kiro_crew.session_ledger import SCHEMA_VERSION as LEDGER_SCHEMA_VERSION
from kiro_crew.session_ledger import TERMINAL_PHASES as LEDGER_TERMINAL_PHASES
from kiro_crew.work_vocab import (
    WORK_CONDUCTOR_FIELDS,
    WORK_STORED_ITEM_LIMIT,
    WorkBoardItem,
    WorkBoardView,
)

logger = logging.getLogger(__name__)

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

#: Folds this module registers but does NOT advertise: no panel draws them and the
#: growth push does not carry them. ``class`` answers what kind of session a log
#: belongs to over the log's whole life, for a reader deciding whether another
#: session may read it, and its one caller asks for it by name
#: (``fold_session(("class",))``). It is registered here rather than kept private so
#: that one machinery folds it -- the same checkpoint, the same incremental reuse, the
#: same recreated-log guard -- while staying out of the advertised set, which would
#: otherwise name a projection with no reader.
INTERNAL_PROJECTION_NAMES: Final[tuple[str, ...]] = ("class",)

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

#: Every fold this module registers, in registry order.
FOLD_NAMES: Final[tuple[str, ...]] = (
    PROJECTION_NAMES + INTERNAL_PROJECTION_NAMES + SLOT_PROJECTION_NAMES
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
#: declared type this module ignores is a fact it chose not to use, while an
#: undeclared one is a fact it does not know exists, and only the second can
#: change what the entries after it mean.
KNOWN_TYPES: Final[frozenset[str]] = frozenset(SESSION_ENTRY_TYPES)

#: Newest moments a ``timeline`` keeps. A projection is pushed over a socket on
#: every growth, so its value is bounded by construction rather than by how long
#: the session ran; the count of moments dropped off the front is kept, so a
#: reader is told the list is a window rather than the whole history.
TIMELINE_LIMIT: Final[int] = 200

#: Newest per-turn context rows a ``usage`` projection keeps, the same posture and
#: the same value as :data:`TIMELINE_LIMIT`: this is the second window this module
#: holds, and a projection is pushed over a socket on every growth, so its size is
#: bounded by construction rather than by how long the session ran. No count of what
#: fell off the front rides beside it: every retained row carries its own ``ordinal``,
#: assigned before any truncation, so a reader reads the first row's true position and
#: knows exactly how much precedes it. A whole-session drop total could not answer
#: that for a reader bounded to a narrower window, which is what made it a defect.
#:
#: Measured at the WORST CASE rather than reasoned about, since this is the fold's
#: largest new state: 200 rows each holding :data:`CONTEXT_SOURCES_PER_TURN_LIMIT`
#: sources whose labels sit at :data:`TEXT_LIMIT` is 808,912 bytes of Python objects.
#: ``test_a_full_window_stays_under_the_slot_fold_cell_budget`` re-measures
#: that same worst case, so the figure fails rather than rots -- and it measures the
#: CAP, not a realistic row, because a realistic 40-source row is 511,192 and would
#: leave the real ceiling untested.
#:
#: It matters because this fold is read per SLOT, so its state lands in the slot-fold
#: cache, whose own ceiling is stated against its largest member -- ``radar`` at
#: 995,342 bytes. A full window plus a full ordinal memo (``context_turn_keys``, 120,042
#: bytes at its widest) fits under that with 66,388 bytes to spare.
#: The row shape is what buys that: the entry's own list of three-key dicts measures
#: 3,319,357 at the same worst case and would breach the ceiling by 2.3 MB. See
#: ``row_sources`` in ``_usage_step``.
CONTEXT_TURNS_LIMIT: Final[int] = 200

#: Sources one retained context row details. The label vocabulary
#: (``context_blocks.split_blocks``) is fixed and far under this, so the cap is what
#: keeps a row bounded against a NEWER writer's longer vocabulary rather than a limit
#: today's writer reaches. Past it the row's own ``chars`` total stays as recorded, so
#: a truncated source list is never mistaken for a smaller prompt, and the session-wide
#: ``by_source`` breakdown counts every source regardless -- it is keyed by label and so
#: is not subject to this per-row cap. Each source a row leaves out is counted in the
#: rendered ``context.sources_omitted``.
CONTEXT_SOURCES_PER_TURN_LIMIT: Final[int] = 64

#: Distinct tool names a ``tools`` projection details. Past it the totals stay
#: exact and ``names_omitted`` counts the names left out.
TOOL_NAME_LIMIT: Final[int] = 100

#: Distinct models a ``usage`` projection details per model. Past it the whole-
#: session totals stay exact and ``models_omitted`` counts the models left out,
#: the same posture as ``TOOL_NAME_LIMIT``. A turn carries a model string, so an
#: unbounded ``by_model`` would grow the checkpoint over a long session.
MODEL_LIMIT: Final[int] = 100

#: ``(unit, turn)`` keys ``_count_turn_ordinal`` remembers, so a rewound or edit-resent
#: turn reuses its ordinal rather than taking a new one. Sized to the REWINDABLE range:
#: ``chat_rewind`` can re-run any user message still in the slot's live list, and that
#: list holds up to ``dashboard.state._MAX_SLOT_MESSAGES`` (10,000) rows, so up to that
#: many distinct turns can recur. A smaller bound evicts a turn a user can still rewind
#: to, and the rerun then takes a NEW ordinal that nothing refolds. Pinned equal to that
#: cap by ``test_usage_the_ordinal_memo_covers_the_rewindable_range``. Bounded at all
#: because it is checkpointed fold state; the keys are held as ONE string (see
#: ``context_turn_keys``) so the bound costs ~100 KB rather than a dict of 10,000 entries.
#: Oldest-inserted keys evict first.
CONTEXT_ORDINAL_MEMO_LIMIT: Final[int] = 10_000

#: Open tool calls and pending approvals listed individually.
OPEN_LIST_LIMIT: Final[int] = 50

#: Open tool calls and pending approvals RETAINED in the fold state. The render
#: lists ``OPEN_LIST_LIMIT`` of them, but the state kept every id it had not yet
#: matched, so a session that leaked unmatched calls or approvals grew the
#: checkpoint without bound -- the one thing this module says it never does. Past
#: this cap a further distinct id is COUNTED as omitted and not retained, so the
#: state stays bounded and a later completion for a dropped id reads as unmatched
#: rather than reopening unbounded growth. Set above the render cap so the listed
#: window is always drawn from retained entries.
OPEN_RETAIN_LIMIT: Final[int] = 512

#: Distinct MCP servers a single tool row records. A tool called through many
#: servers would otherwise append every distinct name to its row without bound.
SERVERS_PER_TOOL_LIMIT: Final[int] = 32

#: Characters of any retained LABEL -- a tool or model name, a server, an
#: approval's tool or reason, a decision. Capping the COUNT of retained values
#: bounds nothing on its own: every one of these strings comes off the wire, and
#: a handful of near-64-KiB ones dwarf the budget they are counted against. A
#: label is a display value, so the honest bound is to keep its head and let the
#: tail go.
TEXT_LIMIT: Final[int] = 200

#: Characters of a retained IDENTITY -- a tool ``call_id``, an ``approval_id``.
#: These are NOT truncated like a label: two distinct ids sharing a 200-character
#: head would become one identity, and one completion would then close a
#: different call's frame. Past this length an id identifies NOTHING, exactly like
#: an absent one, and is counted and left unpaired.
ID_LIMIT: Final[int] = 200

#: The token dimensions ``turn/completed`` bills, in the order it declares them.
TOKEN_DIMENSIONS: Final[tuple[str, ...]] = ("input", "output", "cache_read", "cache_write")

#: Types a ``timeline`` records. Turn, lifecycle and cost boundaries -- the
#: moments a person scanning a session looks for. Message, step and tool entries
#: are deliberately absent: they are the bulk of a log, they are what the page
#: route and the ``tools`` projection already serve, and a timeline that included
#: them would be a second copy of the file rather than a summary of it.
TIMELINE_TYPES: Final[frozenset[str]] = frozenset(
    {
        "session/opened",
        "session/seeded",
        "session/closed",
        "turn/started",
        "turn/completed",
        "turn/refused",
        "compaction/applied",
        "model/selected",
        "write/dropped",
        "approval/requested",
        "approval/decided",
        "subagent/spawned",
        "subagent/completed",
        "subagent/failed",
    }
)


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
class Checkpoint:
    """A fold's resumable position: the seq it has consumed, and its state.

    The state is JSON-serializable so a caller may persist it. Writing it to disk
    is :mod:`kiro_crew.crew_log.checkpoint`, which records exactly this shape
    beside the log it was folded from; this module stays the folding and holds no
    path.
    """

    name: str
    last_seq: int
    state: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "last_seq": self.last_seq, "state": self.state}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Checkpoint:
        """A checkpoint from :meth:`to_dict`, or raise ``bad_data``."""
        name = raw.get("name")
        last_seq = raw.get("last_seq")
        state = raw.get("state")
        if not isinstance(name, str) or name not in _FOLDS:
            raise CrewLogError(f"unknown projection: {name!r}", code=CODE_BAD_DATA, field="name")
        if not isinstance(last_seq, int) or isinstance(last_seq, bool) or last_seq < 0:
            raise CrewLogError(
                f"checkpoint last_seq must be a non-negative int: {last_seq!r}",
                code=CODE_BAD_DATA,
                field="last_seq",
            )
        if not isinstance(state, dict):
            raise CrewLogError(
                "checkpoint state must be an object", code=CODE_BAD_DATA, field="state"
            )
        if not _state_matches_fold(name, state):
            raise CrewLogError(
                f"checkpoint state does not match the {name} fold",
                code=CODE_BAD_DATA,
                field="state",
            )
        return cls(name=name, last_seq=last_seq, state=state)


def _state_matches_fold(name: str, state: dict[str, Any]) -> bool:
    """Whether *state* has the registered fold's durable top-level shape."""
    expected = _FOLDS[name].start()
    if state.keys() != expected.keys():
        return False
    for key, initial_value in expected.items():
        value = state[key]
        if isinstance(initial_value, bool):
            valid = isinstance(value, bool)
        elif isinstance(initial_value, str):
            valid = isinstance(value, str)
        elif isinstance(initial_value, (int, float)):
            valid = isinstance(value, (int, float)) and not isinstance(value, bool)
        elif isinstance(initial_value, dict):
            valid = isinstance(value, dict)
        elif isinstance(initial_value, list):
            valid = isinstance(value, list)
        else:
            # ``None`` is a sentinel for fields that later hold different JSON
            # kinds, so the initial value cannot safely constrain their type.
            valid = True
        if not valid:
            return False
    return True


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


def require_name(name: str) -> str:
    """*name* if it is a projection this module folds, else raise ``bad_data``."""
    if name not in _FOLDS:
        raise CrewLogError(
            f"unknown projection {name!r}; expected one of {list(FOLD_NAMES)}",
            code=CODE_BAD_DATA,
            field="name",
        )
    return name


def fold_state_version(name: str) -> int:
    """The version of what the *name* fold STORES, which its savepoint is keyed to.

    Public because the savepoint module writes this number into each file and demands
    it back on resume, and the fold registry is this module's. One accessor rather than
    a second copy of the table, so a bump lands in one place.
    """
    return _FOLDS[require_name(name)].state_version


def initial(name: str) -> Checkpoint:
    """An empty checkpoint for *name*, at seq 0 -- before the first entry."""
    fold_spec = _FOLDS[require_name(name)]
    return Checkpoint(name=name, last_seq=0, state=fold_spec.start())


def advance(checkpoint: Checkpoint, entries: Iterable[Entry]) -> Checkpoint:
    """*checkpoint* continued over *entries*, which must come after it, in order.

    Every entry's seq must be strictly greater than the last one consumed. An
    entry at or below it is REFUSED rather than skipped, because the two
    plausible causes want opposite handling and this function cannot tell them
    apart: a caller that re-read a page it already folded would have its totals
    counted twice, and a caller holding a checkpoint for a unit that has been
    removed and recreated would have the whole new log swallowed as though it
    were already folded. Refusing names the collision, and rebuilding from
    ``initial`` is the answer to both -- which is what :func:`fold_session` does
    when it sees a log shorter than the checkpoint it holds.

    *checkpoint* is not touched. The state is COPIED before the first step, so a
    caller holding the older checkpoint still holds the value it was given: these
    are frozen records, and a returned one sharing a mutable dict with its input
    would leave that input claiming a seq its state has moved past. The copy is
    bounded work, since every fold's state is bounded by construction.
    """
    fold_spec = _FOLDS[require_name(checkpoint.name)]
    state = copy.deepcopy(checkpoint.state)
    last = checkpoint.last_seq
    for entry in entries:
        if entry.seq <= last:
            raise CrewLogError(
                f"entry {entry.seq} is at or below the {checkpoint.name} checkpoint's "
                f"seq {last}; fold from the start instead of re-folding entries",
                code=CODE_BAD_DATA,
                field="seq",
            )
        fold_spec.step(state, entry)
        last = entry.seq
    return Checkpoint(name=checkpoint.name, last_seq=last, state=state)


def projection_of(checkpoint: Checkpoint, *, revision: int = 0) -> Projection:
    """*checkpoint* rendered -- the value a reader is served, at its own seq.

    *revision* is passed by the two warm paths, which hold one: the slot memo and the
    session memo (``WarmSession.projection``). A cold fold has none and answers ``0``
    (:class:`Projection`).
    """
    fold_spec = _FOLDS[require_name(checkpoint.name)]
    return Projection(
        name=checkpoint.name,
        seq=checkpoint.last_seq,
        value=fold_spec.render(checkpoint.state),
        revision=revision,
    )


def fold(name: str, entries: Iterable[Entry]) -> dict[str, Any]:
    """*name* folded over *entries* from nothing -- the whole-file form.

    One line, and deliberately so: it is :func:`advance` from an empty
    checkpoint, so the resumed answer and the from-scratch answer come out of one
    implementation.
    """
    return projection_of(advance(initial(name), entries)).value


def fold_status(entries: Iterable[Entry]) -> dict[str, Any]:
    """The session's lifecycle and what it is doing now."""
    return fold("status", entries)


def fold_usage(entries: Iterable[Entry]) -> dict[str, Any]:
    """What the session spent: tokens, credits, injected context, compactions."""
    return fold("usage", entries)


def fold_timeline(entries: Iterable[Entry]) -> dict[str, Any]:
    """The newest turn, lifecycle and cost moments, oldest first."""
    return fold("timeline", entries)


def fold_tools(entries: Iterable[Entry]) -> dict[str, Any]:
    """Tool calls matched to their completions, per name and in total."""
    return fold("tools", entries)


def fold_approvals(entries: Iterable[Entry]) -> dict[str, Any]:
    """Approval requests matched to their decisions."""
    return fold("approvals", entries)


# --------------------------------------------------------------------------- #
# Reading a session's log
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SessionProjections:
    """Every projection for one session, all folded through the same seq.

    One pass over the file serves every requested fold, which is what makes pushing the whole
    side panel on each growth cost one read rather than one per fold.
    """

    session_id: str
    last_seq: int
    checkpoints: Mapping[str, Checkpoint] = field(default_factory=dict)
    #: The crew log file's creation identity when these checkpoints were folded,
    #: so a reuse (:func:`fold_session` ``since=``) can tell that the log it folds
    #: now is the SAME file. A log removed and recreated restarts its seqs, and if
    #: it grows past the cached seq before the next fold the seq guard alone
    #: passes -- stale state would then apply to a different file's bytes. ``None``
    #: when no log existed (the empty bundle) and never matches a real file.
    origin: str | None = None
    #: The seq every checkpoint in this bundle is PERSISTED through
    #: (:mod:`kiro_crew.crew_log.checkpoint`), which is not the seq it was folded
    #: through: a savepoint is allowed to lag, because resuming from an older one
    #: replays the tail and reaches the same value. Carried on the bundle so a
    #: caller reusing it across reads decides whether a write is owed from what it
    #: already holds, instead of reading the savepoint files to find out. 0 is
    #: "nothing on disk", which is what an unpersisted bundle must claim.
    saved_seq: int = 0

    #: A size fingerprint of the WHOLE segment set, captured beside ``origin``:
    #: the byte sum across every segment the walk would read, with the segment
    #: count folded in so a segment appearing or vanishing cannot cancel against
    #: another's growth. A reusable bundle may skip walking the log only while
    #: this still matches; ``None`` keeps bundles created before this field safe
    #: by forcing one validating walk before their next O(1) poll.
    size: int | None = None

    #: The newest modification time across the same segment set as ``size``. It
    #: detects an in-place same-size rewrite that size alone cannot distinguish;
    #: ``None`` keeps older bundles safe by forcing one validating walk.
    mtime_ns: int | None = None

    def projection(self, name: str) -> Projection:
        """One rendered projection, or raise ``bad_data`` for an unknown name."""
        return projection_of(self.checkpoints[require_name(name)])

    def rendered(self) -> dict[str, Projection]:
        """Every projection this bundle holds, rendered."""
        return {name: projection_of(cp) for name, cp in self.checkpoints.items()}


def empty_session(session_id: str, names: Iterable[str] = PROJECTION_NAMES) -> SessionProjections:
    """A bundle at seq 0 -- what a session with no crew log folds to.

    An absent log is not an error here. A session that ran with the emitter off
    has none, and its projections are the empty ones rather than a refusal, so a
    caller can render the panel without first asking whether the file exists.
    """
    return SessionProjections(
        session_id=session_id,
        last_seq=0,
        checkpoints={name: initial(name) for name in (require_name(n) for n in names)},
    )


def open_session_log(session_id: str) -> CrewLog | None:
    """This session's crew log opened for READING, or ``None`` when it has none.

    Never repairs. Repair appends closers and takes write ownership, which
    belongs to the gateway resuming the session, and a read path that claimed it
    would refuse whenever the live writer holds it -- turning "show me this
    session" into an error for exactly the sessions that are running.
    """
    if not CrewLog.exists(KIND_SESSION, session_id):
        return None
    return CrewLog.open(KIND_SESSION, session_id)


def _log_identity(handle: CrewLog) -> tuple[str | None, int | None, int | None]:
    """The crew log file's creation identity, size and mtime from stat calls alone.

    A reuse (:func:`fold_session` ``since=``) folds new bytes onto a cached
    checkpoint only when the file it folds now is the SAME one the checkpoint
    came from. The identity combines three signals so no single one has to be
    unique on its own: the header's ``created_at`` (stamped once at create, so a
    recreated log under the same id gets a fresh value), and the file's device
    and inode (which differ when a freed inode is NOT reused, and, combined with
    ``created_at``, make a same-millisecond recreation onto a recycled inode the
    only colliding case -- itself near-impossible). This catches what the seq
    guard cannot: a recreated log that has already grown PAST the cached seq.
    ``None`` is "unknown identity" and never matches, so a header without the
    field or a stat failure falls back to the safe full rebuild. Size and mtime
    together also prevent a same-size in-place rewrite from taking the unchanged
    fast path. A successful stat still returns both when the header lacks
    ``created_at``.

    BOTH identity signals are read from the file on disk on every call, and
    neither comes from *handle*'s own parsed header. That header was parsed when
    the handle was opened, so it keeps answering for the file that existed then --
    which would leave this comparing device and inode alone across exactly the
    recreation it exists to catch, and a just-freed inode is commonly handed
    straight back.

    The size and mtime cover EVERY segment the walk would read, not only the
    newest one that ``handle.path`` names: size is the sum and mtime the newest
    across the segment set, with the count folded into the sum so a whole
    segment appearing or vanishing (rotation, retention) can never cancel out
    against another's growth. The fingerprint must cover exactly what the walk
    consumes -- a fingerprint narrower than the walk is how each round of this
    guard's history got the same finding back in a new spelling -- and it stays
    stat-only, because reading file contents per poll is the cost the fast path
    exists to avoid. A segment vanishing between the listing and its stat is a
    file set in motion, and reads as "unknown": the fold then walks, which is
    the safe answer to a moving target.
    """
    created_at = unit_header_created_at(handle.kind, handle.id)
    try:
        stat = handle.path.stat()
        total_size = 0
        newest_mtime = 0
        segments = segment_paths(handle.kind, handle.id)
        for segment in segments:
            seg_stat = segment.stat()
            total_size += seg_stat.st_size
            newest_mtime = max(newest_mtime, seg_stat.st_mtime_ns)
        # The count rides in the size so "one segment of N bytes" and "two
        # segments of N bytes total" cannot fingerprint alike even at one stat's
        # granularity, and an empty listing stays distinct from an unstatable one.
        total_size = total_size + (len(segments) << 48)
    except OSError:
        return None, None, None
    if created_at is None:
        return None, total_size, newest_mtime
    return f"{created_at}:{stat.st_dev}:{stat.st_ino}", total_size, newest_mtime


def log_origin(handle: CrewLog) -> str | None:
    """The crew log file's creation identity for *handle*, or ``None``.

    The identity half of :func:`_log_identity` -- see there for what the value
    means and why it is read from disk on every call.

    Public because the on-disk savepoints (:mod:`kiro_crew.crew_log.checkpoint`)
    record this same value and must compare it the same way. Two spellings of "is
    this the same log" would be free to disagree, and the one that said yes too
    often would fold a retired file's state onto a live one's bytes.
    """
    return _log_identity(handle)[0]


def fold_session(
    session_id: str,
    names: Iterable[str] = PROJECTION_NAMES,
    *,
    since: SessionProjections | None = None,
    log: CrewLog | None = None,
) -> SessionProjections:
    """Every named projection for *session_id*, folded in one pass.

    *since* is a bundle from an earlier call and turns this into an incremental
    read: only the entries after its seq are consumed. It is discarded and the
    fold starts over in the two cases where continuing would be wrong -- the log
    is now SHORTER than the bundle (the unit was removed and recreated, so its
    seqs start again and the bundle describes different bytes), or the bundle is
    missing a name this call asks for.

    *log* is an already-open handle, so a caller that has just read
    ``last_seq`` folds against the same handle rather than opening the file
    twice.

    The on-disk savepoint (:mod:`kiro_crew.crew_log.checkpoint`) is not optional
    and has no switch: with no reusable *since* the fold resumes from what is
    beside the log instead of from seq 1, and the result is written back once it
    has moved far enough to earn a write. A read that DID reuse *since* serves from
    it but writes nothing, because it cannot vouch for the prefix that bundle was
    folded from -- so a hot incremental reader's savepoint is brought forward by the
    next read that folds the prefix itself rather than by every read. A flag would be
    a public surface with no production caller, and it is not needed to reach the
    from-scratch answer -- :func:`fold` and :func:`advance` ARE that answer, and a
    savepoint is never load-bearing, since every failure over there falls back to
    folding from seq 1.
    """
    wanted = tuple(require_name(name) for name in names)
    bundle, stable, handle, prefix = _fold_attempt(
        session_id, wanted, since=since, log=log, resume=True
    )
    if stable:
        return _persisted(bundle, handle=handle, prefix=prefix)
    # The file's identity changed WHILE it was being folded: the unit was removed
    # and recreated between the identity read and the pass, so the entries just
    # consumed may belong to a different file than the state they were folded onto.
    # One more attempt, from scratch -- no cached bundle, no savepoint, and a freshly
    # opened handle, since the one this call was given does not name the file it was
    # opened on.
    bundle, stable, handle, prefix = _fold_attempt(
        session_id, wanted, since=None, log=None, resume=False
    )
    if stable:
        return _persisted(bundle, handle=handle, prefix=prefix)
    # Twice in a row, so the unit is being recreated faster than it can be read.
    # The value is served, because the alternative is refusing to render a session
    # that exists, but its identity is reported as UNKNOWN: that is what stops a
    # caller from reusing it and stops it from being written to disk, both of which
    # compare against this field and neither of which accepts ``None``.
    logger.debug("crew log %s changed identity twice while folding it", session_id)
    return SessionProjections(
        session_id=session_id,
        last_seq=bundle.last_seq,
        checkpoints=bundle.checkpoints,
        origin=None,
        saved_seq=0,
    )


def _fold_attempt(
    session_id: str,
    wanted: Sequence[str],
    *,
    since: SessionProjections | None,
    log: CrewLog | None,
    resume: bool,
) -> tuple[SessionProjections, bool, CrewLog | None, PrefixWitness | None]:
    """One pass for :func:`fold_session`. ``(bundle, the file held still, handle, witness)``.

    The middle element is what makes the pass checkable. ``iter_from`` opens the
    log by NAME, so a unit removed and recreated mid-pass hands this function a
    different file's entries while it holds the first file's state -- and the seq
    numbers do not say so, because a recreated log starts its own again. So the
    identity is read before the pass and again after it, and a change makes the
    bundle untrustworthy rather than merely stale. The caller decides what to do
    about it; nothing is persisted from here, which is why the handle comes back
    too -- the caller writes the savepoint against the same handle rather than
    opening the file a second time.
    """
    handle = log if log is not None else open_session_log(session_id)
    if handle is None:
        return (empty_session(session_id, wanted), True, None, None)
    last_seq = handle.last_seq
    origin, size, mtime_ns = _log_identity(handle)
    # What the pass trusted about the file before reading it, so the same two things
    # can be asked again afterwards. The savepoint is held rather than a copy of its
    # digest: re-running the load is what re-checks it, and that keeps one routine
    # deciding whether a savepoint describes this file.
    resumed_from: SessionProjections | None = None
    prefix_seen: PrefixWitness | None = None
    # Whether this pass is standing on state an EARLIER call folded. That decides
    # whether it may write a savepoint at all, because a savepoint has to carry a
    # digest of the bytes its state came from. A pass that resumed from DISK carries
    # one transitively: the savepoint records the digest its own writer read before
    # folding, and ``resumed_prefix_still_verifies`` checks it again here, so the
    # custody survives the gap between the two calls. A cached bundle records no
    # digest -- there is no such field on it -- and the bytes below its seq were
    # consumed by a call that has already returned, so nothing this pass can read is
    # evidence about them. A digest read now would be honest about the file and wrong
    # about the state beside it, and the two would then agree with each other, so
    # every later resume would recompute those same bytes, match, and serve that state
    # for the life of the unit. So this pass takes no witness, and ``save`` writes
    # nothing without one. The savepoint is brought forward by the next read that
    # folds the prefix itself, which is a lag the module already allows for: resuming
    # from an older savepoint replays the tail and reaches the same value.
    reused_cached_state = False

    def held_still() -> bool:
        """Whether the file still matches everything this pass trusted about it.

        Three facts, and every one is read BEFORE the pass: the file's identity; on a
        read that resumed, the digest of the prefix the savepoint stood for; and, on a
        pass that will write a savepoint, the digest of the prefix it is about to
        consume. A pass is only trustworthy if none of them moved, so they are asked
        together here rather than at each return, where one would eventually be
        forgotten.

        The third is what lets the savepoint be written from a digest read before the
        pass instead of after it: proving that prefix held still is what makes the
        digest describe the bytes this fold actually consumed.
        """
        from kiro_crew.crew_log import checkpoint as savepoints

        if log_origin(handle) != origin:
            return False
        if prefix_seen is not None and not savepoints.prefix_unchanged(handle, prefix_seen):
            return False
        if resumed_from is None:
            return True
        return savepoints.resumed_prefix_still_verifies(handle, resumed_from)

    reusable = (
        since is not None
        and since.session_id == session_id
        # Same file: a recreated log gets a new inode, so a bundle folded from the
        # old one is refused even after the new file grows past its cached seq --
        # the case the seq guard below cannot catch on its own. ``origin is None``
        # (stat failed, or an old cached bundle predating this field) never
        # matches, so it falls back to the full rebuild.
        and origin is not None
        and since.origin == origin
        and since.last_seq <= last_seq
        and all(name in since.checkpoints for name in wanted)
    )
    if reusable and since is not None:
        base: dict[str, Checkpoint] = {name: since.checkpoints[name] for name in wanted}
        saved_seq = since.saved_seq
        reused_cached_state = True
    else:
        # No bundle in hand, so ask the disk before folding the file. A savepoint
        # covers the names it has and is silent about the rest, and each checkpoint
        # takes only the part of a chunk above its own seq, so a partial answer
        # costs the cold fold to the folds it did not cover rather than to all of
        # them.
        base = {name: initial(name) for name in wanted}
        saved_seq = 0
        # ``resume`` is false only on the retry a mid-pass identity change forces:
        # the savepoint on disk describes the file that just went away, so the
        # retry must not read it. It is private for that reason -- the one caller
        # that needs it is the retry, and a public switch would be a surface with
        # no production caller.
        resumed = _resume_from_disk(handle, wanted) if resume else None
        if resumed is not None:
            base.update(resumed.checkpoints)
            saved_seq = resumed.saved_seq
            resumed_from = resumed
    from kiro_crew.crew_log import checkpoint as savepoints

    # The digest a savepoint is written with has to be read BEFORE the pass consumes
    # the file, and it is read here rather than at the write because a digest taken
    # afterwards can cover bytes the pass never saw. Only a fold that owes a write
    # pays for it, and only one that can vouch for the whole prefix may write at all --
    # see ``reused_cached_state``. Its boundary is the seq read before the pass, so a
    # pass that ends somewhere else -- the file grew under it, or the handle's own seq
    # was stale -- matches no fold in the bundle and writes nothing, which costs the
    # savepoint rather than the read: what this fold SERVES is unaffected either way.
    if not reused_cached_state and savepoints.write_is_earned(last_seq, saved_seq):
        prefix_seen = savepoints.prefix_witness(handle, last_seq)
    from_seq = min((cp.last_seq for cp in base.values()), default=0) + 1
    if from_seq > last_seq and (
        not reused_cached_state
        or (
            since is not None
            and since.size is not None
            and since.size == size
            and since.mtime_ns is not None
            and since.mtime_ns == mtime_ns
        )
    ):
        # No entries were read, but the bundle still describes the identity and the
        # prefix seen before this check. Recheck both so a recreation or interior
        # damage during the call retries cold instead of serving retired state.
        #
        # A pass standing on an in-memory cached bundle carries no prefix digest
        # (see ``reused_cached_state``), so identity alone is all ``held_still``
        # can recheck for it -- and identity survives an in-place rewrite that
        # regresses the tail seq back to the bundle's position. The segment-set
        # size and mtime fingerprint read beside the identity closes that gap:
        # they may skip the validating walk only while both still match what the
        # bundle recorded, and ``None`` (a bundle from before these fields) never
        # matches, which costs one validating walk before that bundle's next O(1)
        # poll. A pass that did NOT reuse a cached bundle is covered by the
        # digest machinery instead, so it keeps the fast return unconditionally.
        return (
            SessionProjections(
                session_id=session_id,
                last_seq=last_seq,
                checkpoints=base,
                origin=origin,
                saved_seq=saved_seq,
                size=size,
                mtime_ns=mtime_ns,
            ),
            held_still(),
            handle,
            prefix_seen,
        )
    grown = _drive_session(_session_registry(wanted), session_id, base, handle)
    reached = max((cp.last_seq for cp in grown.values()), default=last_seq)
    return (
        SessionProjections(
            session_id=session_id,
            last_seq=reached,
            checkpoints=grown,
            origin=origin,
            saved_seq=saved_seq,
            size=size,
            mtime_ns=mtime_ns,
        ),
        held_still(),
        handle,
        prefix_seen,
    )


# The savepoint module imports this one for the fold surface it persists, so the
# dependency runs one way and these two calls are function-local. A module-level
# import here would close the cycle, and the alternative -- moving the fold types
# into a third module to break it -- would split the surface a reader of either
# file has to hold in mind, for no gain at the one place they meet.


def _resume_from_disk(handle: CrewLog, wanted: Sequence[str]) -> SessionProjections | None:
    """The savepoints for *wanted* beside *handle*'s log, or ``None``."""
    from kiro_crew.crew_log import checkpoint as savepoints

    return savepoints.load(handle, wanted)


def _persisted(
    bundle: SessionProjections, *, handle: CrewLog | None, prefix: PrefixWitness | None
) -> SessionProjections:
    """*bundle*, with its savepoint on disk brought forward if a write is owed.

    Whether a write is owed is the savepoint module's decision, not this one's: how
    far a fold must have moved to earn one is a property of the files, and stating
    it here as well would give two places an answer that has to agree. A session
    with no log has nothing to write beside.

    *prefix* is the digest the pass read before consuming the file and rechecked
    after it, and it is what the savepoint is written with -- that write must not read
    the file again, or it could certify bytes no fold saw.
    """
    if handle is None:
        return bundle
    from kiro_crew.crew_log import checkpoint as savepoints

    return savepoints.save(handle, bundle, prefix=prefix)


# --------------------------------------------------------------------------- #
# The folds as projection-kernel units
# --------------------------------------------------------------------------- #


class _SessionFold:
    """One crew-log fold as a :mod:`kiro_crew.projection` unit.

    The kernel drives a pure ``apply`` and decides "something changed" by comparing
    the returned object's IDENTITY with the one it handed over. These folds are
    written the other way round -- ``step`` edits the dict it is given and returns
    nothing -- so this wrapper supplies the difference rather than the folds being
    rewritten: an entry the fold cannot be moved by returns the state UNTOUCHED, and
    any other entry is stepped onto a copy.

    Keeping ``step`` as it is, byte for byte, is the point. The folds are where this
    module's meaning lives, and a rewrite of every mutation site into a functional
    form would put a hundred chances to change a number between the old behaviour and
    the new one, in the one place that must not move.
    """

    def __init__(self, fold: _Fold) -> None:
        self._fold = fold
        self.key = fold.name
        self.state_version = fold.state_version

    def init(self) -> dict[str, Any]:
        return self._fold.start()

    def apply(self, state: dict[str, Any], entry: Entry) -> dict[str, Any]:
        if not self._fold.touched_by(entry):
            return state
        grown = self._fold.copied(state)
        self._fold.step(grown, entry)
        return grown

    def view(self, state: dict[str, Any]) -> dict[str, Any]:
        return self._fold.render(state)


class _HeldCheckpoints:
    """The checkpoints a read already holds, offered as the kernel's savepoint source.

    ``prime_checkpointed`` installs each unit's state at its watermark and then folds
    only the tail past the lowest of them, which is exactly what a read resuming from
    a cached bundle or from disk wants -- so both arrive through this one seam rather
    than through a second seeding path the tail fold would have to agree with.

    The identity is not checked here and no equality is compared, because the caller
    has already decided these checkpoints describe the log it is about to fold: a
    cached bundle passes the reuse test in :func:`_fold_attempt`, and a savepoint read
    from disk passed every admission condition in
    :mod:`kiro_crew.crew_log.checkpoint`. Re-deciding it on weaker evidence is how the
    two answers would get the chance to disagree.

    A name this holds nothing for answers ``None``, which drops the kernel's floor to
    the start and refolds the whole log -- the cold fold, which reaches the same value.
    """

    def __init__(self, held: Mapping[str, Checkpoint]) -> None:
        self._held = held

    def load(
        self,
        store: str,
        key: str,
        *,
        state_version: int,
        identity: Mapping[str, Any],
        admit: Any = None,
    ) -> Savepoint | None:
        checkpoint = self._held.get(key)
        if checkpoint is None:
            return None
        return Savepoint(
            key=key,
            state_version=state_version,
            watermark=checkpoint.last_seq,
            state=checkpoint.state,
            identity=identity,
        )

    def save(self, store: str, savepoint: Savepoint) -> bool:
        """Never written to. Persisting is :mod:`kiro_crew.crew_log.checkpoint`'s."""
        return False

    def discard(self, store: str, key: str) -> None:
        return None


def _session_registry(names: Sequence[str]) -> ProjectionRegistry:
    """A registry holding exactly *names*, for one read.

    Per READ, not one shared instance, and that is a behaviour requirement rather
    than a preference. A registry folds every unit registered in it, so a shared one
    would make ``fold_session(("status",))`` fold all six session folds and write all
    six savepoints -- and it would compute the persisted floor across folds the caller
    never asked for. What this module caches between reads is the bundle a caller
    hands back as ``since=``, so the registry's own cells have nothing to carry.
    """
    registry = ProjectionRegistry(seq_of=attribute_seq)
    for name in names:
        registry.register(_SessionFold(_FOLDS[name]))
    return registry


def _drive_session(
    registry: ProjectionRegistry,
    session_id: str,
    base: Mapping[str, Checkpoint],
    handle: CrewLog,
) -> dict[str, Checkpoint]:
    """*base* carried forward over every entry of *handle* above it.

    One pass over the file for every fold, and NOTHING is materialized: the kernel
    takes the tail as a stream and folds each entry through every unit as it arrives,
    so a cold fold of a long log holds one entry at a time. Each unit drops an entry
    at or below its own watermark inside the kernel's own fold step -- the same step a
    live event takes -- which is what lets one pass serve folds sitting at different
    seqs: a resumed bundle can hold ``status`` further along than ``tools``.

    A fold that RAISES over resumed state retires the state instead of the read. The
    shape checks a savepoint passes read a state's top level, so a value malformed
    below it -- a tool row holding a number where a list belongs -- is admitted and
    fails in the fold, and nothing above catches that: the routes answer only to
    ``CrewLogError``, so the read 500s and does so on every later read, because the
    file that caused it is still there. Any exception therefore discards the savepoints
    and folds the whole log from empty, which is the module's standing answer to doubt.
    A cold fold that raises is NOT swallowed -- that is a fold defect on real entries,
    and serving a value past it would hide it.
    """
    try:
        registry.prime_checkpointed(session_id, _HeldCheckpoints(base), {}, _tail_reader(handle))
        return _cells_as_checkpoints(registry, session_id)
    except Exception:
        log_exception_text(
            logger,
            logging.DEBUG,
            "crew log %s could not fold its resumed state; discarding and folding cold",
            session_id,
        )

    from kiro_crew.crew_log import checkpoint as savepoints

    wanted = tuple(base)
    savepoints.discard(handle, wanted)
    # A FRESH registry: the one above holds cells the failed pass half-folded, and
    # priming over them would carry that half into the cold answer. An empty source
    # drops the kernel's floor to the start, which is the cold fold.
    cold = _session_registry(wanted)
    cold.prime_checkpointed(session_id, _HeldCheckpoints({}), {}, _tail_reader(handle))
    return _cells_as_checkpoints(cold, session_id)


def _tail_reader(handle: CrewLog) -> Callable[[int], Iterable[Entry]]:
    """The kernel's tail source for *handle*: the entries strictly above a watermark."""

    def tail_from(watermark: int) -> Iterable[Entry]:
        # The kernel's empty watermark is -1 and a checkpoint's is 0; both mean
        # nothing consumed, and the log's own first seq is 1.
        return handle.iter_from(max(watermark, 0) + 1, known=KNOWN_TYPES)

    return tail_from


def _cells_as_checkpoints(registry: ProjectionRegistry, session_id: str) -> dict[str, Checkpoint]:
    """Every registered fold's cell as the checkpoint this module's callers carry."""
    return {
        name: Checkpoint(name=name, last_seq=max(watermark, 0), state=state)
        for name, (state, watermark) in registry.cells(session_id).items()
    }


def read_projection(session_id: str, name: str) -> Projection:
    """One projection for *session_id*, as current as its crew log.

    An EAGER session fold is answered from the warm memo (:func:`fold_session_warm`), so
    it carries the revision a pushed frame for it carries. Any other is folded from the
    savepoint beside the log, or from seq 1 when there is none.
    """
    if require_name(name) in EAGER_SESSION_FOLD_NAMES:
        return fold_session_warm(session_id).projection(name)
    bundle = fold_session(session_id, (name,))
    return bundle.projection(name)


# --------------------------------------------------------------------------- #
# Reading a slot's logs
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #


def _status_start() -> dict[str, Any]:
    return {
        "opened_at": None,
        "closed_at": None,
        "close_reason": None,
        "resumed": False,
        "previous": None,
        "seeded": False,
        "agent": "",
        "owner": "",
        "slot": "",
        "cwd": "",
        "model": "",
        "provider": "",
        "open_turn": None,
        "turns_completed": 0,
        "turns_refused": 0,
        "last_stop_reason": None,
        "last_error": None,
        "last_time": None,
        "entries": 0,
        "dropped_count": 0,
        "dropped_bytes": 0,
    }


def _status_step(state: dict[str, Any], entry: Entry) -> None:
    data = entry.data
    state["entries"] += 1
    state["last_time"] = entry.time
    kind = entry.type
    if kind == "session/opened":
        # A resume writes this type too, so the echo is refreshed rather than
        # kept from the first one: the agent or model a session re-attaches under
        # is the one it is serving on now. The opening TIME is the exception --
        # it is when this session began, which a re-attach does not change.
        if state["opened_at"] is None:
            state["opened_at"] = entry.time
        state["resumed"] = bool(data.get("resumed")) or state["resumed"]
        # The edge to the crew log this slot was writing BEFORE this one. Kept from
        # whichever entry carried it rather than refreshed from the newest, because
        # only the creating entry carries one: a re-attach echo has no ``previous``,
        # and letting it overwrite would drop the edge a chain walker needs.
        if state["previous"] is None:
            previous = data.get("previous")
            if isinstance(previous, dict):
                sid = previous.get("sid")
                if isinstance(sid, str) and sid:
                    state["previous"] = _as_str(sid)
        for key in ("agent", "owner", "slot", "cwd"):
            value = data.get(key)
            if isinstance(value, str):
                state[key] = _as_str(value)
        model = _as_str(data.get("model"))
        if model:
            state["model"] = model
        # A reopened session is serving again, so the close a reader would have
        # seen before it describes a life this entry has ended.
        state["closed_at"] = None
        state["close_reason"] = None
    elif kind == "session/seeded":
        state["seeded"] = True
    elif kind == "session/closed":
        state["closed_at"] = entry.time
        state["close_reason"] = _as_text_or_none(data.get("reason"))
        # The open turn is left ALONE. A close is not a turn ending: a session cut
        # off mid-turn has no ``turn/completed``, so clearing here would assert
        # that the turn finished when nothing recorded it doing so, and the one
        # fact a reader wants -- this session died with work in flight -- is
        # exactly what would be erased. A reader sees both ``closed_at`` and the
        # open turn and can tell what happened. Only ``turn/completed`` closes a
        # turn, which is the rule the render states.
    elif kind == "turn/started":
        attempt = data.get("attempt")
        state["open_turn"] = {
            "turn": _as_int(data.get("turn")),
            "attempt": attempt if isinstance(attempt, int) and not isinstance(attempt, bool) else 1,
            "actor": _as_str(data.get("actor")),
            "started_at": entry.time,
            "seq": entry.seq,
        }
    elif kind == "turn/completed":
        state["turns_completed"] += 1
        state["last_stop_reason"] = _as_text_or_none(data.get("stop_reason"))
        state["last_error"] = _as_text_or_none(data.get("error"))
        for key in ("model", "provider"):
            value = _as_str(data.get(key))
            if value:
                state[key] = value
        state["open_turn"] = None
    elif kind == "turn/refused":
        state["turns_refused"] += 1
    elif kind == "model/selected":
        model = _as_str(data.get("model"))
        if model:
            state["model"] = model
    elif kind == "request/configured":
        for key in ("model", "provider"):
            value = _as_str(data.get(key))
            if value:
                state[key] = value
    elif kind == "write/dropped":
        state["dropped_count"] += _as_int(data.get("dropped_count"))
        state["dropped_bytes"] += _as_int(data.get("dropped_bytes"))


def _status_render(state: dict[str, Any]) -> dict[str, Any]:
    if state["closed_at"] is not None:
        lifecycle = "closed"
    elif state["opened_at"] is not None:
        lifecycle = "open"
    else:
        # Reachable: retention can remove the segment that carried
        # ``session/opened``, and a fold over what survives has no opener to read.
        lifecycle = "unknown"
    return {
        "lifecycle": lifecycle,
        "opened_at": state["opened_at"],
        "closed_at": state["closed_at"],
        "close_reason": state["close_reason"],
        "resumed": state["resumed"],
        # The previous crew log of this SLOT, or null when this is its first one (or
        # when retention removed the segment that carried the edge). A reader
        # joining a slot's whole history follows this, one store at a time.
        "previous": state["previous"],
        "seeded": state["seeded"],
        "agent": state["agent"],
        "owner": state["owner"],
        "slot": state["slot"],
        "cwd": state["cwd"],
        "model": state["model"],
        "provider": state["provider"],
        # An open turn is REPORTED, never closed. The store's repair appends real
        # closers under write ownership; a reader that closed it here would make
        # two readers of one file disagree about the same turn.
        "turn": dict(state["open_turn"]) if state["open_turn"] else None,
        "turn_open": state["open_turn"] is not None,
        "turns_completed": state["turns_completed"],
        "turns_refused": state["turns_refused"],
        "last_stop_reason": state["last_stop_reason"],
        "last_error": state["last_error"],
        "last_time": state["last_time"],
        "entries": state["entries"],
        "dropped": {"count": state["dropped_count"], "bytes": state["dropped_bytes"]},
    }


# --------------------------------------------------------------------------- #
# usage
# --------------------------------------------------------------------------- #


def _usage_start() -> dict[str, Any]:
    return {
        "turns_completed": 0,
        "credits": 0.0,
        # The same charges the total above sums, kept apart by who spent them. Every
        # bucket is present from the start, so an absent source reads as "spent
        # nothing" rather than leaving the reader to guess whether the split is
        # partial. ``reported`` counts the entries that carried a measurement, which
        # is what says how much of the bucket's total is covered -- an absent
        # ``credits`` is an unmetered provider, never a zero charge.
        "credits_by_source": {source: {"credits": 0.0, "reported": 0} for source in CREDIT_SOURCES},
        "tokens": {dimension: 0 for dimension in TOKEN_DIMENSIONS},
        "tokens_turns": 0,
        "duration_ms": 0,
        "duration_turns": 0,
        "by_model": {},
        "models_omitted": 0,
        "models_omitted_saturated": False,
        "omitted_models": [],
        "context_tokens": 0,
        "context_chars": 0,
        "context_blocks": 0,
        "context_estimated": 0,
        "context_by_source": {},
        # Sources a retained row could not detail (see ``CONTEXT_SOURCES_PER_TURN_LIMIT``).
        "context_sources_omitted": 0,
        # The per-turn window the Context panel reads, newest LAST, bounded by
        # ``CONTEXT_TURNS_LIMIT``. The cumulative ``context_by_source`` above answers
        # "what does this session inject in total"; a panel drawing one bar per turn
        # needs the turns themselves, and cannot recover them from a sum.
        "context_turns": [],
        # Which UNIT each row came from, counted off ``session/opened``. This works WITH
        # the ``_closed`` seal, and neither covers the other's case: the seal tells two
        # CLOSED runs of one ordinal apart (an attempt that already finished, in this
        # unit or an earlier one), while the unit tells apart a run that was never closed
        # at all -- a unit cut off mid-turn leaves its rows unsealed, and the next unit's
        # same-ordinal turn walks straight onto them. Measured both ways: seal alone
        # stamps the earlier unit's open row, unit alone stamps an earlier attempt whose
        # own closer reported no reading. Internal to the fold; stripped at render.
        "context_unit": 0,
        # Which crew-log UNIT the ordinal key belongs to, counted off the ``session/opened``
        # that STARTS a unit (``resumed`` not true) and nothing else. Distinct from
        # ``context_unit`` above, which also moves on every re-attachment: a re-attach is
        # the same conversation picked up again, and its turn numbers continue the same
        # unit's, so a regenerate after a gateway restart must find the key its first run
        # left. Only a new unit -- a new session or a compaction, each its own crew log --
        # starts a new key space, because that is where turn numbers restart.
        "context_key_unit": 0,
        # A monotonic 1-based counter over the slot's TURN HISTORY, never reset by
        # truncation. Each composition row is stamped with its value as ``ordinal`` so a
        # retained row carries its TRUE position in the whole session history -- exact
        # regardless of how many older rows the window dropped or the day view excluded,
        # and a reader shows it directly instead of deriving a turn number from an array
        # index, which only counts the rows still present.
        #
        # It advances for turns WITHOUT a composition too, not just per composition row:
        # a turn can complete having composed nothing (a refusal, or a turn that reused
        # the prompt already in context), and if the counter only moved on compositions
        # a later row would carry an ordinal that under-counts the turns that actually
        # ran before it -- the panel would then show the wrong turn number and the wrong
        # "earlier turns not shown" count. So a composition advances it (below) AND a
        # ``turn/completed`` that closed a composition-less turn advances it once, so the
        # ordinal tracks the durable turn sequence, not the subset that composed. It is a
        # bare int that grows without bound over a session's life, the one running
        # counter a long session needs, and costs 8 bytes.
        "context_turns_seq": 0,
        # Every ``(unit, turn)`` the ordinal counter has already ACCOUNTED FOR, in the
        # order the counter gave them out. A turn is counted once -- the first event that
        # reaches it (a composition, the closer of a turn that composed nothing, or a
        # refusal) advances the counter and appends its key here; every later event naming
        # the same pair reads its ordinal back rather than advancing again, so a turn that
        # composes more than once, is retried, or is both composed and closed shares one
        # ordinal across its rows.
        #
        # Every seen pair rather than the single most-recent one because a turn number is
        # not monotonic within a unit: the panel recomputes it from the live row count
        # (``chat_runner``), and an edit-resend (``chat_regenerate``) or a rewind
        # (``chat_rewind``) truncates that list in place in the same crew-log session, so
        # a turn already counted can RECUR at a lower number later. A last-only check would
        # see the recurring turn as new and advance the counter again -- mislabelling the
        # rewound turn and inflating the dropped-turn count, with the wrong value persisted
        # in savepoint state and no refold that corrects it.
        #
        # ONE string, ``",{unit}:{turn},{unit}:{turn},"``, rather than a key -> ordinal
        # map: each new key takes the next ordinal, so a key's ordinal is its POSITION
        # (``context_turns_seq`` minus the keys after it) and needs no stored value. That
        # is what lets the memo cover the whole rewindable range
        # (``CONTEXT_ORDINAL_MEMO_LIMIT``) inside the slot-fold cell budget, and a str is
        # immutable, so a snapshot can share it without a copy. ``unit`` is the fold's own
        # integer counter and ``turn`` an int, so neither can contain a separator.
        "context_turn_keys": ",",
        # The newest NON-ZERO window ``request/configured`` stated. That entry is
        # written only when the configuration CHANGED, so the newest one still
        # describes every turn since, and a zero means the provider reported no
        # window rather than a window of nothing. This is what stamps a context ROW,
        # whose question is "what window was this prompt composed against".
        "context_window": 0,
        # No session-wide occupancy maximum is kept here. Occupancy is stamped onto the
        # ROWS instead (see the ``turn/completed`` branch in ``_usage_step``), because a
        # reader bounded to a time window has to be able to exclude a reading from
        # outside it -- and a scalar maximum cannot be narrowed to a window after the
        # fact. The reading and the window it was taken against stay together on the
        # row, so any window's peak is derivable from the rows inside it.
        # The model the newest ``request/configured`` named, stamped onto each context
        # row as it is appended. Truncated like every other retained label.
        "context_model": "",
        "compactions": 0,
        "freed_pct": 0.0,
        "steps": 0,
        "step_ms": 0,
    }


def _credit_charge(value: Any) -> float | None:
    """*value* as a credit charge this module can arithmetic on, else ``None``.

    THE module's one screen for a charge coming off the wire, shared by every fold that
    reads a ``credits`` field -- ``usage`` through :func:`_bill_credits`, and ``subagents``
    per row. Spelled once because it is one question, and two spellings of it drift: the
    second would be the one that forgets a case.

    Two shapes get through a naive check. ``bool`` is an ``int`` in Python, so ``True``
    would bill as one credit. And a JSON integer is UNBOUNDED while a ``float`` is not, so
    ``float(10 ** 400)`` raises ``OverflowError`` rather than returning a wrong number --
    and nothing between here and :func:`fold_session` catches it, so an escaping raise
    would cost the whole projection over one line that stays on disk for good. A line this
    module cannot interpret costs that line and nothing else.

    Deliberately does NOT screen the VALUE for sign, NaN or infinity. That is the caller's
    business and belongs on the RESULT: a charge has many unusable shapes and an
    accumulated total has one invariant (stay finite, never go down), so checking the total
    needs one clause where checking the charge needs a clause per shape.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (OverflowError, ValueError):
        return None


def _bill_credits(state: dict[str, Any], entry: Entry) -> float | None:
    """Add this entry's charge to the session total and to its own source bucket.

    One function for all three spenders, so the total and the split cannot drift
    apart: a bucket that is incremented somewhere the total is not would make the
    two disagree, and a reader has no way to tell which half is wrong.

    Returns the charge, so ``turn/completed`` can also attribute it per model, and
    ``None`` when the entry carried no ``credits`` at all. Absent credits are NOT
    zero: a provider that does not bill in them writes no key, and folding that in
    as 0.0 would state a measurement nobody made. ``reported`` beside each bucket is
    what tells a reader how many charges the bucket's total covers.
    """
    source = _CREDIT_SOURCE_OF.get(entry.type)
    if source is None:
        return None
    billed = _credit_charge(entry.data.get("credits"))
    if billed is None:
        return None
    bucket = state["credits_by_source"][source]
    total = state["credits"] + billed
    grown = bucket["credits"] + billed
    # ONE invariant, and it is about the RESULT rather than the charge: a spend
    # total stays finite, and it never goes down.
    #
    # Checking the charge instead needs a new clause per shape, and the shapes
    # outnumber the clauses -- NaN, each infinity, a negative, and a pair of finite
    # values that overflow on the way up are four different inputs with one
    # consequence. That consequence is what is worth stating, because it is also
    # what cannot be undone: ``round`` keeps a non-finite total non-finite, the
    # savepoint persists it, and a cold refold reads the same entry again, so the
    # fold serves a broken total for the life of the unit.
    #
    # Zero passes deliberately. It does not move the total, and a provider reporting
    # 0.0 measured zero -- a different fact from a closer that reported nothing,
    # which is what ``reported`` beside each bucket exists to tell apart.
    #
    # ``by_model`` needs no check of its own: every billed charge is non-negative, so
    # a model's row is a sub-sum of ``state["credits"]`` and cannot exceed a total
    # this guard has already proved finite.
    if not math.isfinite(total) or not math.isfinite(grown):
        return None
    if total < state["credits"] or grown < bucket["credits"]:
        return None
    state["credits"] = total
    bucket["credits"] = grown
    bucket["reported"] += 1
    return billed


def _count_turn_ordinal(state: dict[str, Any], unit_no: int, turn_no: int) -> int:
    """Advance the session-global ordinal counter ONCE per distinct ``(unit, turn)``.

    *unit_no* is ``context_key_unit``, the crew-log unit, so the key is stable across a
    retry, a rewind, a regenerate and a re-attachment within one unit; every caller
    passes that and nothing else.

    The ordinal is a context row's position in the session's turn history -- what the
    panel renders as the turn's number and what it counts the dropped-off turns from.
    Every durable turn boundary passes through here: a composition, the closer of a
    turn that composed nothing, and the refusal of a turn a dispatch gate never let run.
    Each is ONE turn, so each consumes AT MOST one ordinal.

    ``context_turn_keys`` lists every ``(unit, turn)`` already accounted for, in the
    order the counter numbered them. If this call names a listed pair, its ordinal is
    read back from its position and the counter is left where it is -- so a turn that
    composes more than once, is retried, or is both composed and closed takes a single
    ordinal shared across its rows. A pair not yet seen advances the counter and is
    appended, which is what makes position and ordinal agree.

    Every pair rather than the single most-recent one because a turn number is NOT monotonic
    within a unit: an edit-resend or a rewind truncates the live row list the panel
    numbers from, so a turn already counted can recur at a lower number later. Reusing
    its original ordinal keeps the rewound turn's label stable; a last-only check would
    read the recurrence as new and advance the counter again, mislabelling it and
    inflating the dropped-turn count with no refold that corrects it.

    The list is BOUNDED at ``CONTEXT_ORDINAL_MEMO_LIMIT`` keys, the rewindable range, and
    evicts its oldest key. The counter is a bare int that keeps advancing, so evicting a
    key never disturbs a later turn's ordinal, and position still reads true because the
    keys after a match are counted from the END.
    """
    keys = state["context_turn_keys"]
    needle = f",{unit_no}:{turn_no},"
    found = keys.find(needle)
    if found >= 0:
        # Each key after this one ends in exactly one comma, and the newest key holds
        # the counter's current value.
        return int(state["context_turns_seq"] - keys.count(",", found + len(needle)))
    state["context_turns_seq"] += 1
    keys = keys + needle[1:]
    if state["context_turns_seq"] > CONTEXT_ORDINAL_MEMO_LIMIT:
        # One key in, one key out: every advance appends exactly one key, so past the
        # bound the list is full and drops its oldest.
        keys = "," + keys[keys.index(",", 1) + 1 :]
    state["context_turn_keys"] = keys
    return int(state["context_turns_seq"])


def _usage_step(state: dict[str, Any], entry: Entry) -> None:
    data = entry.data
    billed = _bill_credits(state, entry)
    if entry.type == "session/opened":
        # A new unit begins, and the rows appended from here belong to it. The
        # occupancy stamp is scoped to this counter because ordinals restart per unit.
        #
        # An opener is also written per RE-ATTACHMENT, so this can advance inside one
        # unit. Deliberately not special-cased: it splits a unit into two scopes, and
        # the effect is conservative -- a completion after a re-attachment leaves rows
        # composed before it unstamped, so occupancy reads absent. Under-reporting a
        # reading is the safe direction; carrying an earlier run's reading is not.
        state["context_unit"] += 1
        if data.get("resumed") is not True:
            state["context_key_unit"] = int(state.get("context_key_unit", 0)) + 1
        # The model/window stamps are scoped to the unit that was told them, so a new
        # unit must start without them. The successor restamps from its own
        # request/configured; until it does, a row reads absent rather than inheriting
        # the predecessor's values. request/configured only accepts a NON-ZERO window
        # and a NON-EMPTY model (an auto/backend-default session reports model="" and a
        # provider that has not reported a window writes 0), so without this reset such
        # a successor would silently keep the previous unit's stamp -- carrying one
        # unit's reading onto another's rows, with no self-correcting re-fold.
        state["context_window"] = 0
        state["context_model"] = ""
    elif entry.type == "turn/completed":
        state["turns_completed"] += 1
        model = _as_str(data.get("model"))
        by_model = state["by_model"]
        per_model = None
        if _keyable(model):
            per_model = by_model.get(model)
            if per_model is None and len(by_model) < MODEL_LIMIT:
                per_model = {"turns": 0, "credits": 0.0, "credits_turns": 0, "tokens": 0}
                by_model[model] = per_model
        if per_model is None:
            # Past the model budget, or too long to key safely, the whole-session
            # totals below still count this turn exactly; only the per-model
            # detail is dropped. ``models_omitted`` counts the DISTINCT models
            # left out -- once each, not once per turn they ran -- which keeps the
            # retained ``by_model`` bounded over a long session without inventing
            # models that do not exist.
            _note_omitted(
                state,
                model,
                seen_key="omitted_models",
                count_key="models_omitted",
                saturated_key="models_omitted_saturated",
                budget=MODEL_LIMIT,
            )
        if per_model is not None:
            per_model["turns"] += 1
        # The session total and the turn bucket were already billed above. Only the
        # per-model row is left, and it stays turn-scoped: a child's closer names no
        # model, so attributing its spend to the parent turn's model would charge one
        # model for work another did.
        if billed is not None and per_model is not None:
            per_model["credits"] += billed
            per_model["credits_turns"] += 1
        tokens = data.get("tokens")
        if isinstance(tokens, dict):
            # A block counts as a REPORT only when some dimension is above zero. Logs
            # on disk carry four-zero blocks on closers whose provider sent no
            # counts; a completed turn cannot have cost zero tokens, so a block of
            # zeros is an absence written as a measurement, and counting it would
            # tell a reader beside a real bill that the turn measured 0. The sums
            # below are unaffected either way -- adding zeros moves nothing.
            reported = False
            for dimension in TOKEN_DIMENSIONS:
                measured = _as_int(tokens.get(dimension))
                state["tokens"][dimension] += measured
                if per_model is not None:
                    per_model["tokens"] += measured
                if measured > 0:
                    reported = True
            if reported:
                state["tokens_turns"] += 1
        # The fullest this window got, as the PROVIDER measured it, taken as one pair
        # so the reading and its window always come from the same turn.
        #
        # Deliberately NOT derived from ``tokens.input`` above: that total is summed
        # over every model call the turn made, so on a tool-using turn it exceeds the
        # window it would be divided by, and the ratio a reader computes from it is
        # wrong in the direction that looks alarming. Billing and occupancy are two
        # quantities, and only the second answers "how full did this get".
        #
        # A closer SEALS the rows it closes, whether or not it carried a reading. The
        # window merges every UNIT of a slot and turn ordinals restart per unit, and a
        # regenerate or rewind reruns an ordinal as a fresh ATTEMPT -- so a matching
        # turn number is NOT on its own proof a row belongs to the turn now closing.
        # Two things could otherwise be conflated: a later unit's turn 1 walking back
        # into a previous unit's turn 1, and this turn's attempt 2 walking back into
        # its own attempt 1 (whose rows are the SAME ordinal). The seal is what tells
        # them apart: each attempt's closer marks its own tail run closed, and the next
        # closer stops at the first already-closed row. Sealing happens even for a
        # closer with no reading, because otherwise an attempt that reported no
        # occupancy would leave its rows unsealed and the next attempt's reading would
        # stamp them. ``_closed`` is private bookkeeping the panel reader never sees;
        # the reading itself is ``used`` / ``used_window``, present only when measured.
        occupancy = data.get("context")
        used: int | None = None
        window = 0
        if isinstance(occupancy, dict):
            used = _as_int(occupancy.get("used"))
            window = _as_int(occupancy.get("window"))
        turn_no = _as_int(data.get("turn"))
        unit_no = state["context_unit"]
        rows = state["context_turns"]
        for index in range(len(rows) - 1, -1, -1):
            # The UNIT, beside the ordinal: ordinals restart in each unit of a slot, so
            # an earlier unit's turn N carries this turn's number without being this
            # turn. The seal below cannot catch that when the earlier turn was never
            # CLOSED -- a unit cut off mid-turn by a gateway restart leaves its rows
            # unsealed, and nothing else here tells them from an unstamped attempt.
            if rows[index]["turn"] != turn_no or rows[index]["unit"] != unit_no:
                break
            # The boundary between this closer's rows and an earlier attempt's (or an
            # earlier unit's same-ordinal turn's): that run was sealed by its own
            # closer, so its reading -- or its deliberate absence of one -- must stand.
            if rows[index].get("_closed"):
                break
            # REPLACED, never edited in place. ``_usage_copy`` shares these row dicts
            # between a snapshot and the state that keeps growing -- that sharing is
            # what makes the copy O(window) instead of O(window x sources) -- so writing
            # into a row would reach a projection already handed to a reader. Assigning
            # an element touches only this state's own freshly-copied list.
            sealed = {**rows[index], "_closed": True}
            if used is not None:
                sealed["used"] = used
                # Travels WITH the reading, including as 0: a turn that reported a used
                # count but no window is a reading whose window is unknown, and
                # borrowing another turn's size would pair the number with something it
                # was never measured against.
                sealed["used_window"] = window
            rows[index] = sealed
        duration = data.get("duration_ms")
        if isinstance(duration, int) and not isinstance(duration, bool):
            state["duration_ms"] += duration
            state["duration_turns"] += 1
        # A turn that closed having composed nothing still ran, so it occupies a
        # position in the turn history the ordinal tracks: advance the counter once for
        # it so the NEXT composition's ordinal counts it among the turns before it,
        # rather than the counter only ever moving on the turns that happened to compose
        # (which would make the panel show the wrong turn number). This turn is not yet
        # counted when the mark names some OTHER turn -- an earlier one, or none at all.
        #
        # ONCE, however many attempts it takes: the mark is stamped here as well as at a
        # composition, so a retried composition-less turn -- closed once per attempt,
        # which is why the seal above is per-attempt -- consumes one ordinal rather than
        # one per attempt. Without the stamp the second attempt reads the same unstamped
        # state as the first and advances again, and every later row carries a turn
        # number too high.
        _count_turn_ordinal(state, state["context_key_unit"], turn_no)
    elif entry.type == "turn/refused":
        # A refused turn never reached the model, so it carries no cost and composes
        # nothing -- but it still OCCUPIED a position in the turn history (a dispatch
        # gate declining a turn is ordinary operation), so it consumes an ordinal just
        # as a composition-less close does, and through the same helper so the rule is
        # one rule. ``turn/refused`` is a turn's only durable entry when it fires: no
        # ``turn/completed`` follows, so without this the counter never accounts for the
        # turn and every later composition in the unit reads an ordinal one low.
        _count_turn_ordinal(state, state["context_key_unit"], _as_int(data.get("turn")))
    elif entry.type == "context/composed":
        state["context_tokens"] += _as_int(data.get("tokens"))
        state["context_chars"] += _as_int(data.get("chars"))
        if data.get("tokens_estimated") is True:
            state["context_estimated"] += 1
        # Label to CHARACTERS for this one turn, which is the shape its reader asks
        # for. Deliberately NOT the entry's own ``[{kind, chars, tokens}]`` list:
        # per-turn per-source tokens have no reader, and they are not an independent
        # measurement -- the writer derives every one of them from that source's
        # ``chars`` at one fixed ratio, and the cumulative ``context_by_source``
        # below keeps the summed version for a reader who wants them.
        #
        # Measured at the worst case rather than argued, and it is what decides the
        # shape: a full window is 808,912 bytes this way against 3,319,357 as the
        # entry's list of three-key dicts. This fold is read per SLOT, so the cell
        # lands in the slot-fold cache whose largest budgeted member is 995,342 --
        # so the list shape does not merely cost more, it does not fit.
        row_sources: dict[str, int] = {}
        sources = data.get("sources")
        if isinstance(sources, list):
            for source in sources:
                if not isinstance(source, dict):
                    continue
                label = source.get("kind")
                if not isinstance(label, str) or not label:
                    continue
                per_source = state["context_by_source"].setdefault(
                    label, {"blocks": 0, "tokens": 0, "chars": 0}
                )
                per_source["blocks"] += 1
                per_source["tokens"] += _as_int(source.get("tokens"))
                per_source["chars"] += _as_int(source.get("chars"))
                state["context_blocks"] += 1
                # The retained ROW is bounded where the cumulative tally above is
                # not: that one keys by label and so holds one entry per distinct
                # label whatever the turn count, while a row is kept per turn and
                # would multiply any per-row growth by the window. A label at
                # ``TEXT_LIMIT`` is not keyed, the same reason ``_keyable`` gives
                # everywhere else: it cannot be told apart from one that was cut, so
                # two sources would pool into one number.
                # A source left out is COUNTED (``sources_omitted`` on the rendered
                # ``context``), so a reader can tell a truncated breakdown from a
                # complete one. A label the row already holds still adds to it at the
                # cap: only a NEW label is refused, so a kept label is never cut short.
                keyed = _as_str(label)
                if _keyable(keyed) and (
                    keyed in row_sources or len(row_sources) < CONTEXT_SOURCES_PER_TURN_LIMIT
                ):
                    row_sources[keyed] = row_sources.get(keyed, 0) + _as_int(source.get("chars"))
                else:
                    state["context_sources_omitted"] += 1
        row: dict[str, Any] = {
            "turn": _as_int(data.get("turn")),
            # The entry's own writer-assigned stamp (epoch ms). The entry data
            # carries no time of its own, and this is the one the log recorded.
            "ts": entry.time,
            "chars": _as_int(data.get("chars")),
            # NO per-row ``tokens`` / ``tokens_estimated``: the only reader of these rows
            # shapes them to the ``ContextTrace`` interface, which asks for neither, and a
            # row's token count is not an independent measurement -- the writer derives it
            # from the same ``chars`` at a fixed ratio. The session-wide counts those two
            # feed (``tokens``, ``estimated_turns``) are accumulated above and unaffected.
            "sources": row_sources,
            # Which unit of the slot this composition came from. Internal to the fold
            # and stripped at render: it keeps one unit's reading off another unit's
            # same-ordinal rows, and no reader asks for it.
            "unit": state["context_unit"],
            # Which population this row belongs to, as the COMPOSER stated it, or ""
            # when it did not. Never derived here: a reader separating the one-off
            # session-start injection from the per-turn ones needs the composer's own
            # answer, and the guess available to a fold -- the first row of a unit --
            # is wrong for the rebuild a mid-session replay triggers.
            "phase": _as_str(data.get("phase")),
            # Stamped from the newest configuration rather than looked up later, so a
            # row records the model and window its prompt was actually measured
            # against even after either moves.
            "model": state["context_model"],
            "window": state["context_window"],
        }
        # The row's TRUE position in the whole session history, assigned before any
        # truncation and never reused. Dropped rows take their ordinal with them, so the
        # first retained row's ordinal is exactly how many rows came before it -- what a
        # reader needs to show a turn's real number without counting an array that holds
        # only the survivors.
        #
        # ONCE per distinct (unit, turn), through the shared helper: a single turn can
        # compose more than once (a rebuild re-emits the session-start injection, a
        # prompt recomposed within one turn), and every such row is the SAME turn, so it
        # carries that turn's one ordinal. The helper reuses the current counter value
        # when this turn is already the one it last accounted for, so a second
        # composition does not advance past the real turn sequence.
        row["ordinal"] = _count_turn_ordinal(state, state["context_key_unit"], row["turn"])
        turns_window = state["context_turns"]
        turns_window.append(row)
        if len(turns_window) > CONTEXT_TURNS_LIMIT:
            over = len(turns_window) - CONTEXT_TURNS_LIMIT
            # Drop the OLDEST per-turn rows first, and keep the session-start rows they
            # sit among. A session-start composition is the one-off injection a unit
            # opens with -- many times the size of a per-turn one and the anchor the
            # Context panel draws its history against. Trimming it like an ordinary row
            # once a session passes CONTEXT_TURNS_LIMIT turns would silently corrupt the
            # panel's totals (the reader sums the RETAINED rows) and lose the largest
            # bar. So the trim skips session-start rows: only per-turn rows count toward
            # ``over`` and are removed, oldest first.
            #
            # Session-start rows are one per unit start/rebuild, so the count preserved
            # is bounded by the units folded into a slot, not by how long the session
            # ran. The final clamp below is the backstop for the pathological case where
            # session-start rows alone would exceed the cap: it drops oldest-overall so
            # the byte budget this limit exists to hold is never breached.
            removed = 0
            index = 0
            while removed < over and index < len(turns_window):
                if turns_window[index].get("phase") == PHASE_SESSION_START:
                    index += 1
                    continue
                del turns_window[index]
                removed += 1
            if len(turns_window) > CONTEXT_TURNS_LIMIT:
                del turns_window[: len(turns_window) - CONTEXT_TURNS_LIMIT]
    elif entry.type == "request/configured":
        # Newest NON-ZERO wins. This entry is written only when the configuration
        # changed, and a provider that reports no window writes 0 -- taking that as
        # the current window would erase a size the session was told earlier and is
        # still running under.
        window = _as_int(data.get("context_window"))
        if window > 0:
            state["context_window"] = window
        configured_model = _as_str(data.get("model"))
        if configured_model:
            state["context_model"] = configured_model
    elif entry.type == "compaction/applied":
        state["compactions"] += 1
        freed = data.get("freed_pct")
        if isinstance(freed, (int, float)) and not isinstance(freed, bool):
            state["freed_pct"] += float(freed)
    elif entry.type == "step/completed":
        state["steps"] += 1
        state["step_ms"] += _as_int(data.get("ms"))


def _usage_render(state: dict[str, Any]) -> dict[str, Any]:
    tokens = dict(state["tokens"])
    return {
        "turns": {
            "completed": state["turns_completed"],
            # Turn-scoped on purpose, and it stays that way now the total covers
            # three sources: this number answers how many of the session's TURNS
            # reported a cost, which a whole-session count could not.
            "credits_reported": state["credits_by_source"]["turn"]["reported"],
            "tokens_reported": state["tokens_turns"],
            "duration_reported": state["duration_turns"],
        },
        "credits": round(state["credits"], 6),
        # The total above, split by who spent it. Rounded per bucket so the parts
        # are each readable; a reader comparing them against the total is comparing
        # two roundings of the same sum, not two different sums.
        "credits_by_source": {
            source: {
                "credits": round(row["credits"], 6),
                "reported": row["reported"],
            }
            for source, row in state["credits_by_source"].items()
        },
        "tokens": {**tokens, "total": sum(tokens.values())},
        "duration_ms": state["duration_ms"],
        "by_model": {
            name: {
                "turns": row["turns"],
                "credits": round(row["credits"], 6),
                "credits_reported": row["credits_turns"],
                "tokens": row["tokens"],
            }
            for name, row in sorted(state["by_model"].items())
        },
        "models_omitted": state["models_omitted"],
        # True once the dedup budget is spent: ``models_omitted`` is then a floor,
        # not a total, the same posture ``names_omitted`` takes.
        "models_omitted_saturated": state["models_omitted_saturated"],
        "context": {
            "tokens": state["context_tokens"],
            "chars": state["context_chars"],
            "blocks": state["context_blocks"],
            "estimated_turns": state["context_estimated"],
            "by_source": {
                name: dict(row) for name, row in sorted(state["context_by_source"].items())
            },
            # How many sources the retained ``turns`` rows leave out, across the session.
            # 0 means every row's ``sources`` is its whole breakdown.
            "sources_omitted": state["context_sources_omitted"],
            # The per-turn window, OLDEST FIRST. No count of what fell off the front
            # rides beside it: each row carries its own ``ordinal``, assigned before
            # any truncation, so the first row's number is what tells a reader this is
            # a window rather than the whole history -- and it says how much precedes
            # it, which a whole-session total could not for a narrower window.
            # ``_closed`` is the fold's private seal (which closer already stamped this
            # row) and ``unit`` (which run of the slot appended it) are both dropped
            # here: together they keep one run's reading off another's same-ordinal
            # rows, and no reader asks for either.
            "turns": [
                {
                    **{k: v for k, v in row.items() if k not in ("_closed", "unit")},
                    "sources": dict(row["sources"]),
                }
                for row in state["context_turns"]
            ],
            # NO session-wide occupancy pair here. The reading and its window ride on
            # each ROW (``used`` / ``used_window``), because the reader is bounded to a
            # TIME WINDOW and a maximum computed here could not be narrowed to it: the
            # fullest turn of a long session is frequently older than every row the
            # caller asked for. The reader takes the peak over the rows it keeps and
            # gets its window from the same row.
            #
            # ``window`` remains: it is the newest size ``request/configured`` stated,
            # which is what a reader with NO occupancy reading in its window can still
            # be told the session runs under. A reader with a reading uses that row's
            # own ``used_window`` instead.
            "window": state["context_window"],
        },
        "compactions": {
            "count": state["compactions"],
            "freed_pct": round(state["freed_pct"], 4),
        },
        "steps": {"completed": state["steps"], "ms": state["step_ms"]},
    }


# --------------------------------------------------------------------------- #
# timeline
# --------------------------------------------------------------------------- #


def _timeline_start() -> dict[str, Any]:
    # ``last_seq`` is the seq of the newest moment folded so far, or 0 before the
    # first. A crew log's seq is strictly increasing within one succession unit and
    # restarts at 1 in the next, so a moment at or below it is the mark that a SECOND
    # unit has begun -- which this fold refuses below rather than render as one
    # scrambled timeline.
    return {"moments": [], "dropped": 0, "last_seq": 0}


def _timeline_step(state: dict[str, Any], entry: Entry) -> None:
    if entry.type not in TIMELINE_TYPES:
        return
    if entry.seq <= state["last_seq"]:
        # A slot-wide read folds the units a slot ran under back to back, and each
        # unit's seq restarts at 1. One session holds exactly one unit, so the only
        # reader today never gets here; the day a slot-keyed reader folds this over a
        # slot, two units interleaved by wall clock carry a clock-rollback inversion.
        # Fail loudly at that boundary so the timeline is ordered by succession before
        # anyone orders it by bare timestamp.
        raise CrewLogError(
            f"timeline moment at seq {entry.seq} is at or below the fold's seq "
            f"{state['last_seq']}; the timeline fold spans one succession unit, not a "
            f"slot of them -- order the units by succession before folding",
            code=CODE_BAD_DATA,
            field="seq",
        )
    state["last_seq"] = entry.seq
    moment: dict[str, Any] = {"seq": entry.seq, "time": entry.time, "type": entry.type}
    data = entry.data
    for key in (
        "turn",
        "attempt",
        "actor",
        "stop_reason",
        "reason",
        "model",
        "source",
        "duration_ms",
        # Both spellings of a measured duration: ``duration_ms`` is the turn entry's,
        # ``ms`` is what the subagent closers and steps call theirs. Copying only one
        # of the two keeps that half of the log's durations out of the timeline.
        "ms",
        "credits",
        "freed_pct",
        "dropped_count",
        "agent_id",
        "agent",
        "resumed",
        "count",
        "approval_id",
        "decision",
        "by",
        "tool",
    ):
        value = data.get(key)
        if isinstance(value, str):
            # Retained in a bounded window, so its SIZE is part of that bound.
            value = _as_str(value)
        if isinstance(value, (str, int, float, bool)) and value != "":
            moment[key] = value
    moments: list[dict[str, Any]] = state["moments"]
    moments.append(moment)
    if len(moments) > TIMELINE_LIMIT:
        # A window, and it says so: the count of moments cut off the front rides
        # in the value, so a reader is never shown a partial list that looks whole.
        state["dropped"] += len(moments) - TIMELINE_LIMIT
        del moments[: len(moments) - TIMELINE_LIMIT]


def _timeline_render(state: dict[str, Any]) -> dict[str, Any]:
    moments: list[dict[str, Any]] = state["moments"]
    return {
        "moments": [dict(moment) for moment in moments],
        "dropped": state["dropped"],
        "limit": TIMELINE_LIMIT,
        "first_seq": moments[0]["seq"] if moments else None,
        "last_seq": moments[-1]["seq"] if moments else None,
    }


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #


def _tools_start() -> dict[str, Any]:
    return {
        "calls": 0,
        "completed": 0,
        "errors": 0,
        "unidentified_calls": 0,
        "unmatched_completions": 0,
        "elapsed_ms": 0,
        "by_name": {},
        "open": {},
        "open_omitted": 0,
        "names_omitted": 0,
        "names_omitted_saturated": False,
        "omitted_names": [],
    }


def _tool_row(state: dict[str, Any], name: str) -> dict[str, Any] | None:
    """The per-name row for *name*, or ``None`` when it gets no detail row.

    A name gets no row for either of two reasons -- the name budget is spent, or
    the name is too long to key safely -- and both take the same path: COUNTED in
    the totals and left out of the detail, so the aggregate a caller sums stays
    exact while the value stays bounded.
    """
    by_name: dict[str, Any] = state["by_name"]
    if _keyable(name):
        row = by_name.get(name)
        if row is not None:
            return row
        if len(by_name) < TOOL_NAME_LIMIT:
            row = {
                "calls": 0,
                "completed": 0,
                "errors": 0,
                "elapsed_ms": 0,
                "last_status": None,
                "last_time": None,
                "servers": [],
                "servers_over": [],
                "servers_omitted": 0,
                "servers_saturated": False,
            }
            by_name[name] = row
            return row
    _note_omitted(
        state,
        name,
        seen_key="omitted_names",
        count_key="names_omitted",
        saturated_key="names_omitted_saturated",
        budget=TOOL_NAME_LIMIT,
    )
    return None


def _tools_step(state: dict[str, Any], entry: Entry) -> None:
    data = entry.data
    if entry.type == "tool/called":
        state["calls"] += 1
        name = _as_str(data.get("name"))
        row = _tool_row(state, name)
        if row is not None:
            row["calls"] += 1
            row["last_time"] = entry.time
            server = _as_str(data.get("server"))
            if server:
                if server in row["servers"]:
                    pass  # already detailed for this tool
                elif _keyable(server) and len(row["servers"]) < SERVERS_PER_TOOL_LIMIT:
                    row["servers"].append(server)
                else:
                    # Count DISTINCT omitted servers, not repeated calls through
                    # one of them, and say so when the dedup list that makes the
                    # count exact is spent. A server at the cut length comes here
                    # too: it cannot be told apart from a cut one, so listing it
                    # would let it stand for a server it is not.
                    _note_omitted(
                        row,
                        server,
                        seen_key="servers_over",
                        count_key="servers_omitted",
                        saturated_key="servers_saturated",
                        budget=SERVERS_PER_TOOL_LIMIT,
                    )
        call_id = _as_id(data.get("call_id"))
        # An empty call_id is what the declaration allows when the frame carried
        # none, and it identifies NOTHING: keying the open-call map by it would
        # make every such call the same call, so one completion would close a
        # different call's frame. An id past ``ID_LIMIT`` is treated the same way,
        # because it is retained here and its size is part of the bound. Those are
        # counted and left unpaired.
        if call_id:
            if call_id in state["open"] or len(state["open"]) < OPEN_RETAIN_LIMIT:
                state["open"][call_id] = {
                    "call_id": call_id,
                    "name": name,
                    "turn": _as_int(data.get("turn")),
                    "time": entry.time,
                    "seq": entry.seq,
                }
            else:
                # The retained open-call map is full of never-matched ids. A
                # further distinct one is counted and dropped rather than kept,
                # so the checkpoint stays bounded; its later completion reads as
                # unmatched, which it effectively is.
                state["open_omitted"] += 1
        else:
            state["unidentified_calls"] += 1
    elif entry.type == "tool/completed":
        state["completed"] += 1
        name = _as_str(data.get("name"))
        status = _as_str(data.get("status"))
        # Two independent signals, and either one is an error: ``status`` is the
        # frame's own outcome, while ``is_error`` is tri-state and absent when the
        # caller asserted nothing -- so an absent one is not a claim that the call
        # worked.
        failed = status in {"refused", "error", "failed"} or data.get("is_error") is True
        if failed:
            state["errors"] += 1
        elapsed = _as_int(data.get("elapsed_ms"))
        state["elapsed_ms"] += elapsed
        row = _tool_row(state, name)
        if row is not None:
            row["completed"] += 1
            row["elapsed_ms"] += elapsed
            row["last_status"] = status
            row["last_time"] = entry.time
            if failed:
                row["errors"] += 1
        # The SAME coercion as the open side, so the two agree on what an identity
        # is. If a completion paired on a raw id while the call retained a coerced
        # one, an over-long id would look unmatched here and unbounded there.
        call_id = _as_id(data.get("call_id"))
        if call_id:
            if state["open"].pop(call_id, None) is None:
                state["unmatched_completions"] += 1


def _tools_render(state: dict[str, Any]) -> dict[str, Any]:
    open_calls = sorted(state["open"].values(), key=lambda call: call["seq"])
    return {
        "calls": state["calls"],
        "completed": state["completed"],
        "errors": state["errors"],
        # An unmatched call is reported OPEN, not completed with a guessed status.
        "open": len(open_calls),
        "open_calls": [dict(call) for call in open_calls[:OPEN_LIST_LIMIT]],
        "open_calls_omitted": max(0, len(open_calls) - OPEN_LIST_LIMIT),
        "open_dropped": state["open_omitted"],
        "unidentified_calls": state["unidentified_calls"],
        "unmatched_completions": state["unmatched_completions"],
        "elapsed_ms": state["elapsed_ms"],
        "by_name": {
            name: {
                "calls": row["calls"],
                "completed": row["completed"],
                "errors": row["errors"],
                "elapsed_ms": row["elapsed_ms"],
                "last_status": row["last_status"],
                "last_time": row["last_time"],
                "servers": list(row["servers"]),
                "servers_omitted": row["servers_omitted"],
                "servers_omitted_saturated": row["servers_saturated"],
            }
            for name, row in sorted(state["by_name"].items())
        },
        "names_omitted": state["names_omitted"],
        # True once the dedup budget is spent: ``names_omitted`` is then a floor,
        # not a total. Without this a reader cannot tell an exact count from a
        # stalled one.
        "names_omitted_saturated": state["names_omitted_saturated"],
    }


# --------------------------------------------------------------------------- #
# approvals
# --------------------------------------------------------------------------- #


def _approvals_start() -> dict[str, Any]:
    return {
        "requested": 0,
        "decided": 0,
        "unidentified_requests": 0,
        "unmatched_decisions": 0,
        "by_decision": {},
        "pending": {},
        "pending_omitted": 0,
        "last": None,
    }


def _approvals_step(state: dict[str, Any], entry: Entry) -> None:
    data = entry.data
    approval_id = _as_id(data.get("approval_id"))
    identified = bool(approval_id)
    if entry.type == "approval/requested":
        state["requested"] += 1
        if identified:
            if approval_id in state["pending"] or len(state["pending"]) < OPEN_RETAIN_LIMIT:
                state["pending"][approval_id] = {
                    "approval_id": approval_id,
                    "tool": _as_str(data.get("tool")),
                    "reason": _as_str(data.get("reason")),
                    "turn": _as_int(data.get("turn")),
                    "time": entry.time,
                    "seq": entry.seq,
                }
            else:
                # Retained pending map full of never-decided requests: count and
                # drop the further one so the checkpoint stays bounded. A later
                # decision for a dropped id reads as unmatched.
                state["pending_omitted"] += 1
        else:
            # Same rule as an empty tool call_id: an unidentified request cannot
            # be paired with a decision without pairing it with the wrong one.
            state["unidentified_requests"] += 1
    elif entry.type == "approval/decided":
        state["decided"] += 1
        decision = _as_str(data.get("decision"))
        state["by_decision"][decision] = state["by_decision"].get(decision, 0) + 1
        request = state["pending"].pop(approval_id, None) if identified else None
        if identified and request is None:
            state["unmatched_decisions"] += 1
        state["last"] = {
            "approval_id": approval_id if identified else "",
            "decision": decision,
            "by": _as_str(data.get("by")),
            "cause": _as_str(data.get("cause")),
            "tool": (request or {}).get("tool", ""),
            "turn": _as_int(data.get("turn")),
            "time": entry.time,
            "seq": entry.seq,
        }


def _approvals_render(state: dict[str, Any]) -> dict[str, Any]:
    pending = sorted(state["pending"].values(), key=lambda item: item["seq"])
    return {
        "requested": state["requested"],
        "decided": state["decided"],
        "pending": len(pending),
        "pending_requests": [dict(item) for item in pending[:OPEN_LIST_LIMIT]],
        "pending_omitted": max(0, len(pending) - OPEN_LIST_LIMIT),
        "pending_dropped": state["pending_omitted"],
        "unidentified_requests": state["unidentified_requests"],
        "unmatched_decisions": state["unmatched_decisions"],
        "by_decision": dict(sorted(state["by_decision"].items())),
        "last": dict(state["last"]) if state["last"] else None,
    }


# --------------------------------------------------------------------------- #
# subagents
# --------------------------------------------------------------------------- #
#
# WHAT THIS FOLD IS FOR. "Which children did this session dispatch, and for each one
# what happened, how long it ran and what it cost" -- answered from one savepoint read
# rather than from a 200-entry ``timeline`` window that happens to have swallowed the
# four ``subagent/*`` types along with everything else.
#
# The same number lives in five places besides this one -- the runtime accumulator, the
# terminal tombstone, the persistence record, the WS frame and its replay, and the
# browser store -- and keeping those five agreeing is where a reader's numbers go wrong.
# This fold is the one that is DERIVED from the log, so it cannot disagree with the
# record without the record itself being wrong.


#: Outcomes this fold counts under their own name. ``subagent/failed`` carries an OPEN
#: enum, deliberately: the value is the subagent runtime's own, and enforcing the set
#: would turn "the upstream vocabulary grew" into a lost record. So a closer whose
#: outcome is not one of these lands in ``unknown``, which is what the declaration
#: already means by it -- the row keeps the literal string, so the fact is not lost at
#: the level that can hold it.
_SUBAGENT_OUTCOMES: Final[frozenset[str]] = frozenset({"completed", "failed", "stopped", "unknown"})

#: Entry types ``subagents`` reads: one opener, two closers, and the dismissal. A
#: dismissal is neither an opener nor a closer -- it is the user clearing a card -- but
#: this fold is what the panel draws, so it is the fold that has to stop drawing the
#: row. ``subagent/steered`` is DECLARED in the session vocabulary and deliberately not
#: read here -- a steer is an event about a child rather than a state of one, and
#: nothing this fold answers for is a count of them. Leaving it out of ``affects`` is
#: safe in the direction that matters: the set may be wider than the truth but never
#: narrower, and a type the step ignores would cost a copy per entry for a value that
#: never changes.
SUBAGENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "subagent/spawned",
        "subagent/completed",
        "subagent/failed",
        "subagent/dismissed",
    }
)


def _subagents_start() -> dict[str, Any]:
    return {
        "by_id": {},
        # Deliberately NO ``open`` list. ``_subagents_render`` answers which children are
        # still going from the rows themselves (an ``outcome`` of ``None``) and from the
        # totals, so a second list here would be state nothing reads, kept in step by hand
        # on every closer.
        # Dispatches this fold counted but did NOT retain, for any of THREE reasons: an
        # id that identifies nothing (empty or over-long), an id a retained row already
        # holds, or a full ``by_id``. The three reasons differ; what they share is the
        # only thing this counter is for, which is keeping
        # ``spawned == len(by_id) + omitted`` true.
        "omitted": 0,
        "totals": {
            # EVERY dispatch, including the ones ``omitted`` counts. See
            # ``_subagents_render`` for why the two are allowed to disagree.
            "spawned": 0,
            "completed": 0,
            "failed": 0,
            "stopped": 0,
            "unknown": 0,
            # Closers that found no row. Without this, a session whose totals exceed
            # what its rows account for reads as an arithmetic bug.
            "closed_unmatched": 0,
            # DELIBERATELY no cost or duration aggregate here. ``usage`` folds the credits
            # one from these same closers -- ``CREDIT_SOURCES`` carries ``"subagent"``, and
            # ``credits_by_source["subagent"]`` is ``{"credits", "reported"}`` over exactly
            # this population -- so a second spelling would be two things to keep in step.
            # A summed duration had no such owner and no reader either, and the rule this
            # module holds is the one stated for a row: a field kept against a reader that
            # does not exist is state paid for on every copy and every savepoint write.
            # Both figures still ride per child on the row that closed, which answers the
            # question a surface actually asks -- whose, not how much altogether. What that
            # gives up is the duration of a child whose dispatch left no row: its outcome
            # and ``closed_unmatched`` still record that it ran and how it ended, but how
            # long it took is not kept anywhere.
        },
    }


def _subagent_credits(value: Any) -> float | None:
    """*value* as a credit charge on ONE row, or ``None`` when none was reported.

    Absent is NOT zero: a child that was never billed and a child that cost nothing
    are different facts, and only one of them is a measurement.

    The SHAPE screen is :func:`_credit_charge`, the module's single one. What this adds is
    the part that screen deliberately leaves to its caller -- sign and finiteness -- stated
    per VALUE rather than against a running total, because this fold keeps none: ``usage``
    owns the aggregate over these same closers, so the only credit figure published here is
    the one on a row, and a row has nothing to accumulate into.

    Both clauses are about what the row would PUBLISH. A non-finite value survives
    ``round``, the savepoint persists it, and a cold refold reads the same entry again, so
    one unusable charge would sit in the served projection for the life of the unit. A
    negative one is not a charge: the module's spend invariant is that a total never goes
    down, and a row is a term in someone else's.
    """
    charge = _credit_charge(value)
    if charge is None or not math.isfinite(charge) or charge < 0:
        return None
    return charge


def _subagents_step(state: dict[str, Any], entry: Entry) -> None:
    data = entry.data
    agent_id = _as_id(data.get("agent_id"))
    rows: dict[str, Any] = state["by_id"]
    totals: dict[str, Any] = state["totals"]

    if entry.type == "subagent/spawned":
        # Counted BEFORE the cap is consulted: this is how many children the session
        # dispatched, which is a fact about the session rather than about how many rows
        # this fold chose to keep.
        totals["spawned"] += 1
        # An empty or over-long id identifies nothing, so keying a row by it would make
        # every such child the same child and let one closer close another's row. It is
        # counted above and left unretained, the same path a dispatch past the cap takes.
        if not agent_id or agent_id in rows:
            # Both of these dispatches were counted in ``spawned`` and neither gets a row of
            # its own, so both belong in ``omitted``: that is what keeps
            # ``spawned == len(by_id) + omitted`` true, which the spec advertises as
            # checkable. A dispatch that vanished from both sides of the identity would read
            # to an auditing reader as arithmetic this fold got wrong.
            state["omitted"] += 1
            return
        if len(rows) >= OPEN_RETAIN_LIMIT:
            # The bound this module holds everywhere: a session that dispatched more
            # children than the cap keeps its totals exact and stops growing the state.
            state["omitted"] += 1
            return
        rows[agent_id] = {
            "agent_id": agent_id,
            # Retained because ``render`` orders the rows by it. Deliberately the SEQ and
            # not the time: two children dispatched in one millisecond tie on a clock, and
            # a seq is what the log guarantees is ordered.
            "seq_spawned": entry.seq,
            # The envelope's own stamp, NOT a field the entry carries -- the log already
            # records when each line was written, so a ``started`` in ``data`` would be a
            # second clock a writer could disagree with. It is kept because a reader
            # assembling one list from SEVERAL sessions' folds has no common order
            # otherwise: ``seq_spawned`` orders rows inside one log and says nothing
            # across two. It also dates a row, which is what an age window needs.
            "started_ms": entry.time,
            "agent": _as_str(data.get("agent")),
            "model": _as_str(data.get("model")),
            # What the child was asked to do. ``""`` when the entry carried none, which
            # includes every log written before the field existed -- a surface draws no
            # task line for it rather than an empty one.
            "task": _as_str(data.get("task")),
            "outcome": None,
            # ``None``, not 0, for the same reason ``credits`` is: a closer writes ``ms``
            # only when it measured a duration above zero, and crash-repair's closer writes
            # none at all, so a 0 would present an absent measurement as a measured instant.
            "ms": None,
            "credits": None,
            "reason": "",
            # Whether the user has cleared this child's card. Latches true and never
            # back: a dismissal is an act, and the log does not un-record one.
            "dismissed": False,
        }
        return

    if entry.type == "subagent/dismissed":
        # The user clearing a card, which is neither an opener nor a closer: the row
        # keeps whatever outcome its own closer recorded, and a dismissal may arrive
        # before any closer for a child cleared while it was still running. Only the
        # ROW moves -- nothing in ``totals`` does, because what the session dispatched
        # and what it spent are not changed by the user hiding a card, and a spend
        # figure that fell when someone tidied the panel would be wrong.
        row = rows.get(agent_id) if agent_id else None
        if row is None:
            # A dismissal naming no retained row: the dispatch was omitted past the
            # cap, or its opener never reached the file. Nothing is drawn for that
            # child either way, so there is nothing to stop drawing and no counter
            # for it -- a count nothing renders is state paid for on every copy.
            return
        row["dismissed"] = True
        return

    # A closer, and the type is matched EXPLICITLY rather than reached by falling through
    # the two branches above. ``affects`` spares this fold the entries it does not read,
    # but only on the kernel's path (:class:`_SessionFold`): :func:`advance` and
    # :func:`fold` call ``step`` for every entry in the file, so a fall-through ``else``
    # here would bill every ``turn/completed`` in the log as a child that closed.
    if entry.type not in ("subagent/completed", "subagent/failed"):
        return
    if entry.type == "subagent/completed":
        outcome = "completed"
    else:
        outcome = _as_str(data.get("outcome")) or "unknown"
    counted = outcome if outcome in _SUBAGENT_OUTCOMES else "unknown"
    raw_ms = data.get("ms")
    ms = raw_ms if isinstance(raw_ms, int) and not isinstance(raw_ms, bool) else None
    # Screened as the value the ROW would publish. There is no total here to hang the
    # invariant on: ``usage`` owns the aggregate over these closers.
    credits = _subagent_credits(data.get("credits"))

    # The totals move for EVERY closer, row or no row. crash-repair closes a child
    # across the whole file, so a closer routinely names a dispatch this fold omitted --
    # and dropping it would under-report what the session actually ran and spent, which
    # is the one number a reader comes here for.
    totals[counted] += 1

    row = rows.get(agent_id) if agent_id else None
    if row is None:
        totals["closed_unmatched"] += 1
        return
    row["outcome"] = outcome
    row["ms"] = ms
    row["credits"] = credits
    row["reason"] = _as_str(data.get("reason"))


def _subagents_render(state: dict[str, Any]) -> dict[str, Any]:
    """The children this session dispatched, in dispatch order.

    ``totals["spawned"]`` and ``by_id`` are allowed to DISAGREE, and two counts
    reconcile them: ``spawned == len(by_id) + omitted + dismissed``. A reader that wants
    to know what the session did reads the total; one that wants per-child detail reads
    the rows and is told, by a non-zero ``omitted``, that it is holding a window rather
    than the whole list, and by a non-zero ``dismissed`` that the user cleared some of
    the cards. Reporting only the retained count would silently shrink a long session's
    history to the cap and look exact.

    A DISMISSED row is kept in the state and left out of ``by_id``. The row has to stay,
    because a dismissal is an act the log does not un-record and a later closer still
    lands on it; it has to leave ``by_id``, because this fold is what the panel draws and
    the user cleared that card. ``totals`` never moves for a dismissal: what the session
    dispatched and what it spent are facts about the session, so a spend figure that fell
    when someone tidied the panel would be wrong.

    ``credits`` on a row is ``None`` when that child reported no charge, never ``0``.
    A surface drawing this has three states to draw, not two: a number, "no charge was
    reported", and a child that has not closed yet.

    ``task`` is ``""`` when the dispatch recorded none, which is also what every log
    written before the field reads as. A surface draws no task line for it rather than
    an empty one: the log does not say what the child was asked, which is not the same
    claim as its having been asked for nothing.

    ``started_ms`` is the ``subagent/spawned`` entry's own envelope stamp. It is here
    for a reader assembling ONE list out of several sessions' folds, which
    ``seq_spawned`` cannot order -- seqs are per log -- and for dating a row against an
    age window.

    ``running`` is the count still open, and it is derived from TWO floors because neither
    alone is right.

    The totals give ``spawned - closed``, which retention never touches -- so it stays exact
    once the cap has dropped a dispatch, where counting the retained rows without a closer
    would report a session with 600 children in flight as having 512 or fewer.

    But that difference can fall BELOW what this fold can still point at. A closer whose
    ``subagent/spawned`` never reached the readable file -- an append dropped once its
    attempt budget was spent, or a damaged record ``_iter_segments`` skips -- bills into
    ``closed`` having never bumped ``spawned``. With one other child still in flight that
    arithmetic answers 0 while a retained row is drawn wearing a running pill, and a floor
    at 0 hides it rather than fixing it. So the retained open rows are the other floor: the
    result is at least the number of children this fold is still showing as open. In the
    retain-cap case the omitted dispatch WAS counted in ``spawned``, so the totals figure is
    the larger of the two and stays exact.
    """
    rows = sorted(state["by_id"].values(), key=lambda row: row["seq_spawned"])
    # The rows a surface draws: a dismissed card is one the user cleared, and the whole
    # point of recording the dismissal here is that this fold stops offering the row.
    # The dismissed ones stay in ``totals`` -- what the session dispatched and what it
    # spent are facts about the session, and a spend figure that fell when someone
    # tidied the panel would be wrong.
    drawn = [row for row in rows if not row["dismissed"]]
    dismissed = len(rows) - len(drawn)
    totals = dict(state["totals"])
    closed = sum(totals[outcome] for outcome in sorted(_SUBAGENT_OUTCOMES))
    # Counted, not emitted. An id list and the cap were both in this value with no reader:
    # the panel derives the open rows by filtering `by_id` for an absent outcome, which is
    # the same filter, so the list was a second spelling of something already there.
    #
    # Over the DRAWN rows, so a dismissed child still in flight puts no running pill on
    # a panel that does not show it. The totals floor below still counts it, which is
    # right: it is running whether or not anyone is looking.
    still_open = sum(1 for row in drawn if row["outcome"] is None)
    return {
        "by_id": {
            row["agent_id"]: {
                "agent_id": row["agent_id"],
                "seq_spawned": row["seq_spawned"],
                "started_ms": row["started_ms"],
                "agent": row["agent"],
                "model": row["model"],
                "task": row["task"],
                "outcome": row["outcome"],
                "ms": row["ms"],
                "credits": row["credits"],
                "reason": row["reason"],
            }
            for row in drawn
        },
        "running": max(still_open, totals["spawned"] - closed),
        # Whether that number is the answer or a FLOOR under it. It is a floor exactly when
        # both truncations are in play at once: a dispatch this fold omitted, and a closer
        # that matched no row. Each unmatched closer is subtracted from the count, but the
        # fold cannot tell whether it closed one of the omitted dispatches or a child whose
        # ``subagent/spawned`` never reached the file -- and only the first of those two
        # should reduce the running count. So a session at 513 dispatches with one orphan
        # closer reports 512 while 513 are in flight. A surface stating the count says "at
        # least" when this is false, because a number that can be short by an unknown amount
        # and is drawn as exact is the same class of lie as a truncated list drawn as whole.
        "running_exact": not (state["omitted"] > 0 and totals["closed_unmatched"] > 0),
        "omitted": state["omitted"],
        # Retained rows this render is NOT offering, because the user cleared them. It
        # is published rather than left implicit because it is the third term in the
        # identity above: without it a reader checking
        # ``spawned == len(by_id) + omitted`` sees it fail the moment anyone dismisses
        # a card, and cannot tell a tidied panel from arithmetic the fold got wrong.
        "dismissed": dismissed,
        "totals": totals,
    }


# --------------------------------------------------------------------------- #
# ledger
# --------------------------------------------------------------------------- #


def _ledger_iso(stamp_ms: int) -> str:
    """An entry's epoch-millisecond ``time`` as the record's local ISO spelling.

    The envelope already carries when each update happened, so the record's
    timestamps are DERIVED from it rather than written into the entry -- one clock,
    and no way for an entry to claim a time the log disagrees with.

    A value outside the range a ``datetime`` can hold answers ``""``, the same thing
    an absent stamp answers. ``fromtimestamp`` raises ``OverflowError`` or ``OSError``
    on one, and this reads bytes a reader does not control: a damaged or planted
    ``time`` would otherwise turn every read of that slot into a crash, permanently,
    since the line stays on disk and nothing rewrites it. Losing one stamp costs a
    reader a display value; raising costs it the whole record.
    """
    try:
        return datetime.fromtimestamp(stamp_ms / 1000).astimezone().isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return ""


def _ledger_field(value: Any, limit: int = LEDGER_TEXT_LIMIT) -> str:
    """*value* as a clamped string, or ``""``. The fold's own shape gate.

    The writer clamps too, but these bytes come off a file a reader does not
    control, so the length is re-applied here: a planted or damaged line is exactly
    the input that ignores the writer's rule, and every field below is RETAINED in
    a state a nudge turn carries.
    """
    if not isinstance(value, str):
        return ""
    return value[:limit]


def _ledger_start() -> dict[str, Any]:
    return {
        "goal": "",
        "phase": "",
        "next": "",
        "tried": [],
        "artifacts": {},
        "events": [],
        "created_at": "",
        "last_progress_at": "",
        "finished_at": "",
    }


def _ledger_step(state: dict[str, Any], entry: Entry) -> None:
    if entry.type != LEDGER_ENTRY_TYPE:
        return
    data = entry.data
    stamp = _ledger_iso(entry.time)
    if not state["created_at"]:
        state["created_at"] = stamp
    # An ABSENT field means unchanged, which is what lets a partial update be one
    # entry; only a present one is applied. ``isinstance`` rather than truthiness,
    # so a caller clearing a field to "" is applied rather than ignored.
    if isinstance(data.get("goal"), str):
        state["goal"] = _ledger_field(data["goal"])
    if isinstance(data.get("phase"), str):
        state["phase"] = _ledger_field(data["phase"], LEDGER_PHASE_LIMIT)
        # Re-derived on every phase write rather than latched: a workstream that
        # leaves a terminal phase is in flight again, and a stale ``finished_at``
        # would keep the snapshot suppressed for a session that resumed.
        state["finished_at"] = stamp if state["phase"] in LEDGER_TERMINAL_PHASES else ""
    if isinstance(data.get("next"), str):
        state["next"] = _ledger_field(data["next"])
    tried = data.get("tried")
    if isinstance(tried, Mapping) and isinstance(tried.get("approach"), str):
        rows: list[dict[str, str]] = state["tried"]
        rows.append(
            {
                "approach": _ledger_field(tried["approach"]),
                "rejected_because": _ledger_field(tried.get("rejected_because")),
                "at": stamp,
            }
        )
        # Bounded like every other fold state here: the oldest rejected approach
        # ages out so a long workstream cannot grow the record without limit.
        if len(rows) > LEDGER_TRIED_LIMIT:
            del rows[: len(rows) - LEDGER_TRIED_LIMIT]
    artifacts = data.get("artifacts")
    if isinstance(artifacts, Mapping):
        merged: dict[str, str] = state["artifacts"]
        for key, value in artifacts.items():
            if not isinstance(key, str) or not isinstance(value, str):
                continue
            folded = _ledger_field(key, LEDGER_ARTIFACT_KEY_LIMIT)
            # Popped before reassigning: a plain update keeps the key's ORIGINAL
            # insertion position, so updating the oldest pointer on a full map would
            # leave it first in line for the age-out below -- dropping the very
            # artifact this entry just set.
            merged.pop(folded, None)
            merged[folded] = _ledger_field(value)
        while len(merged) > LEDGER_ARTIFACT_LIMIT:
            merged.pop(next(iter(merged)))
    event = data.get("event")
    if isinstance(event, str) and event.strip():
        kind = data.get("event_kind")
        # The kind is a FILTER over the text, not the fact, so an unrecognized one
        # degrades to ``note`` rather than discarding the event. A phase that moved
        # without a recognized kind cannot reach the file at all: the writer refuses
        # it, so nothing here has to reconstruct that rule.
        if not (isinstance(kind, str) and kind in LEDGER_EVENT_KINDS):
            kind = "note"
        events: list[dict[str, str]] = state["events"]
        events.append({"ts": stamp, "kind": kind, "text": _ledger_field(event.strip())})
        if len(events) > LEDGER_EVENT_LIMIT:
            del events[: len(events) - LEDGER_EVENT_LIMIT]
    state["last_progress_at"] = stamp


def _ledger_render(state: dict[str, Any]) -> dict[str, Any]:
    """The state RECORD, in the shape every reader of the ledger already expects.

    Deliberately the same ten keys the ledger's document carried when it was a file
    of its own, so the MCP tool, the route and the injected snapshot did not have to
    learn a new shape to stop being a second copy of the truth. ``schema`` describes
    the RECORD, which is unchanged; where the record lives is not something a
    consumer of it branches on.
    """
    return {
        "schema": LEDGER_SCHEMA_VERSION,
        "goal": state["goal"],
        "phase": state["phase"],
        "next": state["next"],
        "tried": [dict(row) for row in state["tried"]],
        "artifacts": dict(state["artifacts"]),
        "events": [dict(row) for row in state["events"]],
        "created_at": state["created_at"],
        "last_progress_at": state["last_progress_at"],
        "finished_at": state["finished_at"],
    }


# radar -- the Issue Radar crew ledger
# --------------------------------------------------------------------------- #

#: Ceilings on the state a crew's fold RETAINS. The crew page reads at most
#: ``_MAX_EVENTS`` lines (500), so the event tail keeps that many; the oldest go
#: first, which is the order every reader already drops them in. Phase lines are
#: kept PER ITEM so a long-parked lane's entry line cannot be pushed out by other
#: items' chatter -- the lane that has sat longest is the one the pipeline view
#: exists to show. Text is re-clamped on the way in because these bytes come off a
#: file a reader does not control.
RADAR_EVENT_LIMIT: Final[int] = 500
RADAR_PHASE_LINE_LIMIT: Final[int] = 200
#: Rejected approaches kept PER ITEM, newest last; a crew that rejects more than
#: this on one issue has stopped learning from the list, and the oldest rows are
#: the ones a resume can do without.
RADAR_TRIED_LIMIT: Final[int] = 100
#: Work items and passes kept PER CREW. Past the bound the fold EVICTS -- an item:
#: a finished one first, oldest finish first, then the open one longest without
#: progress; a pass: the earliest decided -- and COUNTS what it evicted in
#: ``counts``, so a bounded record is told from a complete one. A crew that has
#: touched more distinct issues than this has a history, not a working set, and the
#: working set is what a resume needs; an evicted pass is one the repository may
#: investigate again, which the spec accepts as this index's bound.
RADAR_ITEM_LIMIT: Final[int] = 500
RADAR_SKIP_LIMIT: Final[int] = 5000
#: Clamp for every free-text field the fold reads. It is a SHAPE gate on bytes a
#: reader does not control, not an input cap, so it must equal the largest value the
#: record tool would have accepted -- ``validation.MAX_MEDIUM_STRING``, the cap on
#: ``next``, ``decision``, ``why``, ``tried_approach`` and ``tried_rejected_because``.
#: Lower than that and an ordinary accepted field is truncated on every read, which
#: is silent state corruption rather than a bound. Not imported from ``validation``
#: on purpose -- the fold stays free of the MCP layer -- so
#: ``test_issue_radar_crew_store`` pins the two constants equal instead.
RADAR_TEXT_LIMIT: Final[int] = 5000
RADAR_SCHEMA_VERSION: Final[int] = 1


def _radar_iso(stamp_ms: int) -> str:
    """An entry's epoch-millisecond ``time`` as the UTC ``Z`` spelling the record keeps.

    Derived from the envelope rather than written into the entry: one clock, and no
    way for an entry to claim a time the log disagrees with. A stamp outside the
    range a ``datetime`` holds answers ``""`` rather than raising, because this reads
    bytes a reader does not control and one damaged line must cost a display value,
    not every read of the crew forever.
    """
    try:
        moment = datetime.fromtimestamp(stamp_ms / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return ""
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _radar_text(value: Any, limit: int = RADAR_TEXT_LIMIT) -> str:
    """*value* as a clamped string, or ``""``. The fold's own shape gate."""
    if not isinstance(value, str):
        return ""
    return value[:limit]


def _radar_int(value: Any) -> int | None:
    """*value* as an int, or ``None``. Bools are refused: JSON ``true`` is not a number."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _radar_number(value: Any, field: str) -> int | None:
    """*value* as an int inside the record tool's range for *field*, or ``None``.

    The magnitude half of the same rule :func:`_radar_text` applies to a string and
    :func:`radar_ci_state` to a counter: these bytes come off a file a reader does not
    control, so a number outside the range the tool would have accepted is dropped
    rather than retained. Dropping is the existing answer for a number of the wrong
    type, and an item or skip keyed on such a number was never one the tool wrote.
    """
    number = _radar_int(value)
    if number is None:
        return None
    low, high = RADAR_NUMBER_BOUNDS[field]
    return number if low <= number <= high else None


def radar_ci_state(value: Mapping[str, Any]) -> dict[str, Any]:
    """The members of a CI reading the record tool would have accepted, bounded.

    Only :data:`RADAR_CI_KEYS`, each with the tool's own type and ceiling from
    :data:`RADAR_CI_BOUNDS`: a string verdict clipped to its length, a counter kept
    only when it is an int within the tool's range. Any other member, and any member
    of the wrong shape, is dropped. The fold applies this to the bytes it reads and
    the crew store's carry to a pre-projection file, so a reading that reached the
    log by any path is retained within the same bounds.
    """
    kept: dict[str, Any] = {}
    for key in RADAR_CI_KEYS:
        if key not in value:
            continue
        kind, bound = RADAR_CI_BOUNDS[key]
        member = value[key]
        if kind is str:
            if isinstance(member, str) and member:
                kept[key] = member[:bound]
            continue
        number = _radar_int(member)
        if number is not None and 0 <= number <= bound:
            kept[key] = number
    return kept


def _radar_event_id(ts: str, crew_id: str, number: int | None, kind: str, text: str) -> str:
    """The content-addressed line id the crew ledger has always given a progress line.

    Kept byte-identical to the pre-projection formula so a reader keyed on ids sees
    the same id for the same line. ``number`` renders as the empty string on a
    crew-level line, which cannot collide with a real number.
    """
    shown = "" if number is None else int(number)
    raw = f"{ts}|{crew_id}|{shown}|{kind}|{text}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def _radar_start() -> dict[str, Any]:
    return {
        "crew_id": "",
        "owner": "",
        "repo": "",
        "items": {},
        "events": [],
        "skips": {},
        "phase_lines": {},
        # ``[line id, payload digest]`` pairs for the newest entries folded: the
        # collapse of a REPEATED entry keys on the whole update, not on the line's
        # display identity, so two same-millisecond calls that differ only in the
        # fields they patch both fold.
        "last_update": {},
        # How many items and passes the bounds above evicted from this fold, so a
        # reader can tell a bounded record from a complete one.
        "evicted_items": 0,
        "evicted_skips": 0,
    }


def _radar_payload_digest(data: Mapping[str, Any]) -> str:
    """A digest of the whole update, so a repeat is told from a same-looking one."""
    try:
        raw = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        raw = repr(sorted(data.items(), key=lambda kv: str(kv[0])))
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16]


def _radar_new_item(crew_id: str, owner: str, repo: str, number: int) -> dict[str, Any]:
    """A work item before its first update, in the key order the record has always had."""
    return {
        "schema": RADAR_SCHEMA_VERSION,
        "crew_id": crew_id,
        "owner": owner,
        "repo": repo,
        "number": number,
        "phase": "selected",
        "outcome": None,
        "decision": "",
        "why": "",
        "next": "",
        "tried": [],
        "worktree": "",
        "branch": "",
        "base_sha": "",
        "pr_number": None,
        "ci_state": {},
        "claim_comment_id": None,
        "labels_applied": [],
        "claimed_at": None,
        "last_progress_at": None,
        "finished_at": None,
    }


def _radar_record_skip(
    state: dict[str, Any], key: str, number: int, skip: Mapping[str, Any], crew_id: str, ts: str
) -> None:
    """Record one pass in this crew's contribution to the repository's skip index.

    FIRST decision wins, as the shared index always did: the first crew's reason is
    the audit trail a human reads, a later identical pass adds nothing, and a
    different conclusion is a disagreement to surface on the later crew's own item
    rather than a silent edit of someone else's record. ``crew_id`` and
    ``decided_at`` on the row default to the entry's own and are overridden only by
    a carried row, which re-states a decision made elsewhere and earlier.
    """
    if not isinstance(skip.get("reason"), str):
        return
    skips: dict[str, dict[str, Any]] = state["skips"]
    if key in skips:
        return
    scope = skip.get("scope")
    skips[key] = {
        "number": number,
        "reason": _radar_text(skip["reason"]),
        "scope": (
            scope
            if isinstance(scope, str) and scope in RADAR_SKIP_SCOPES
            else RADAR_DEFAULT_SKIP_SCOPE
        ),
        "crew_id": _radar_text(skip.get("crew_id"), 64) or crew_id,
        "decided_at": _radar_text(skip.get("decided_at"), 64) or ts,
        # The writer saw another crew's decision standing when it recorded this pass;
        # the union never lets such a row stand over the one it saw. The writer's
        # observation orders the two, so a clock stepped backward cannot re-order them.
        "deferred": skip.get("deferred") is True,
    }
    while len(skips) > RADAR_SKIP_LIMIT:
        # The EARLIEST decided pass goes first: the shared index keeps a number's
        # first decision, and of this crew's rows the oldest is the one most likely
        # already re-decided by the issue itself (closed, or reopened and worked).
        oldest = min(skips, key=lambda k: (str(skips[k].get("decided_at") or ""), k))
        del skips[oldest]
        state["evicted_skips"] += 1


def _radar_bound_items(state: dict[str, Any], keep: str) -> None:
    """Evict work items past :data:`RADAR_ITEM_LIMIT`, never the one just written.

    A FINISHED item goes before any open one -- an open item is work the crew still
    owes, and the record exists so it can resume that work -- oldest finish first;
    only when every other item is open does the one longest without progress go. An
    evicted item takes its phase history with it, so the two stay bounded together,
    and is counted.

    An item in an EDITING phase is never a victim, whatever the pressure. The
    one-editor rule is decided by scanning the items this record still holds, so
    evicting the item that holds the edit makes the rule answer "no editor" and admit
    a second one -- and an append-only log cannot retract the conflicting lines the two
    then write. The bound stays meaningful because the rule admits ONE editing item at
    a time, so this withholds at most one candidate out of the limit; if it somehow
    withholds them all, the bound is exceeded instead, which is what this function
    already does when the only item left is the one just written.
    """
    items: dict[str, dict[str, Any]] = state["items"]
    while len(items) > RADAR_ITEM_LIMIT:
        candidates = [
            key for key in items if key != keep and items[key]["phase"] not in RADAR_EDITING_PHASES
        ]
        if not candidates:
            return
        finished = [key for key in candidates if items[key]["phase"] in RADAR_TERMINAL_PHASES]
        pool = finished or candidates
        victim = min(
            pool,
            key=lambda k: (
                str(items[k].get("finished_at") or items[k].get("last_progress_at") or ""),
                k,
            ),
        )
        del items[victim]
        state["phase_lines"].pop(victim, None)
        state["evicted_items"] += 1


def _radar_step(state: dict[str, Any], entry: Entry) -> None:
    if entry.type != RADAR_ENTRY_TYPE:
        return
    data = entry.data
    crew_id = _radar_text(data.get("crew_id"), 64)
    if not crew_id:
        return
    if not state["crew_id"]:
        # The first entry names the crew; every unit a crew's slot ran under belongs
        # to that one crew, so a later entry naming another is a planted or damaged
        # line and is left out rather than folded into a record it does not own.
        state["crew_id"] = crew_id
        state["owner"] = _radar_text(data.get("owner"), 256)
        state["repo"] = _radar_text(data.get("repo"), 256)
    elif crew_id != state["crew_id"]:
        return
    ts = _radar_iso(entry.time)
    kind = data.get("event_kind")
    if not (isinstance(kind, str) and kind in RADAR_EVENT_KINDS):
        return
    text = _radar_text(data.get("event"))
    number = _radar_number(data.get("number"), "number")
    carried = data.get("carried") is True
    events: list[dict[str, Any]] = state["events"]

    if number is None:
        # A crew-level line -- the queue sweep that took nothing. Consecutive sweeps
        # COALESCE: "checked, took nothing" is a recurring latest-value fact, and a
        # crew is nudged on a timer, so one line per idle cycle would push the crew's
        # real work history out of its own bounded tail. The first sweep after real
        # work stands; a sweep landing on a sweep adds nothing, and its timestamp is
        # deliberately the older one -- when the idle stretch BEGAN is the reading a
        # human opening a quiet crew wants.
        if kind != RADAR_CREW_LEVEL_EVENT_KIND:
            return
        if events and events[-1].get("kind") == RADAR_CREW_LEVEL_EVENT_KIND:
            return
        events.append(
            {
                "id": _radar_event_id(ts, crew_id, None, kind, text),
                "ts": ts,
                "crew_id": crew_id,
                "kind": kind,
                "text": text,
            }
        )
        del events[: max(0, len(events) - RADAR_EVENT_LIMIT)]
        return
    if kind == RADAR_CREW_LEVEL_EVENT_KIND:
        # The pairing the writer enforces, re-applied to the bytes: a crew-level kind
        # with a number would file a queue sweep under an issue it never touched.
        return

    key = str(number)
    line_id = _radar_event_id(ts, crew_id, number, kind, text)
    digest = _radar_payload_digest(data)
    last_update: dict[str, str] = state["last_update"]
    skip = data.get("skip")
    skip_row = skip if isinstance(skip, Mapping) else None
    carried_pass = carried and skip_row is not None and "phase" not in data
    # WHAT THIS UPDATE WROTE, and therefore what has to still be here for a retry of
    # it to be redundant. Every update writes an item except a carried pass, which
    # deliberately writes a skip and no item; an update carrying a `skip` writes one
    # too. Items and skips are bounded on SEPARATE rules and evict independently, so
    # asking whether EITHER survived would let a surviving skip vouch for an item the
    # item bound has since evicted -- and then a crash retry of the same update, which
    # is the one thing that would put the item back, returns here instead and leaves it
    # missing until some later DISTINCT update happens to rewrite it.
    still_present = (carried_pass or key in state["items"]) and (
        skip_row is None or key in state["skips"]
    )
    if last_update.get(key) == digest and still_present:
        # The same UPDATE twice IN A ROW for this item -- an append retried after a
        # crash or after a refused read-back, or one call landing twice. Only the
        # item's LAST applied update is compared, never a window of history: a retry
        # is by construction the next update for its item (the crew's writes are
        # serialized and the crew is waiting on the answer), while an item that
        # legitimately returns to an earlier state with identical fields after
        # intervening updates is a new update and applies. The digest covers the
        # whole update -- crew, number, kind, text and every field -- and not the
        # timestamped line id, which a retry re-stamps.
        #
        # Gated on the RECORD still existing, because this map and the two bounded
        # collections evict on different rules and different key sets: the digest of
        # an evicted item (or pass) can outlive it, and a retry matching that stale
        # digest would return here and leave the record GONE -- a row the crew was
        # told landed, absent. While the record is present the dedup is doing its
        # job; once it is not, the retry is what puts it back.
        return
    last_update.pop(key, None)
    last_update[key] = digest
    while len(last_update) > RADAR_EVENT_LIMIT:
        last_update.pop(next(iter(last_update)))
    if carried_pass and skip_row is not None:
        # A carried PASS on an issue this crew never worked -- the pre-projection
        # index was repository-wide, so the crew that carries it forward is usually
        # not the crew that decided it. It records the row and the line and NO work
        # item: an item would put an issue the crew never touched on its own page.
        _radar_record_skip(state, key, number, skip_row, crew_id, ts)
        events.append(
            {
                "id": line_id,
                "ts": ts,
                "crew_id": crew_id,
                "number": number,
                "kind": kind,
                "text": text,
            }
        )
        del events[: max(0, len(events) - RADAR_EVENT_LIMIT)]
        return
    items: dict[str, dict[str, Any]] = state["items"]
    existing = items.get(key)
    item = (
        existing
        if existing is not None
        else _radar_new_item(crew_id, state["owner"], state["repo"], number)
    )
    prev_phase = item["phase"] if existing is not None else None
    progressed = existing is None

    cleared = data.get("clear")
    if isinstance(cleared, list):
        # An explicit null in the update is carried as a CLEAR, named by field, since
        # a typed null is not a value the entry type admits. Applied before the set
        # fields, so a call that clears and sets the same field keeps the set value.
        for name in cleared:
            if not isinstance(name, str) or name not in RADAR_CLEARABLE_FIELDS:
                continue
            if name in ("pr_number", "claim_comment_id", "outcome"):
                item[name] = None
            elif name == "ci_state":
                item[name] = {}
            elif name == "labels_applied":
                item[name] = []
            else:
                item[name] = ""
            if name in ("pr_number", "ci_state", "next"):
                progressed = True

    phase = data.get("phase")
    if isinstance(phase, str) and phase in RADAR_PHASES:
        if phase != item["phase"]:
            progressed = True
        item["phase"] = phase
    for field_name in ("decision", "why", "worktree", "branch", "base_sha"):
        if isinstance(data.get(field_name), str):
            item[field_name] = _radar_text(data[field_name])
    if isinstance(data.get("next"), str):
        new_next = _radar_text(data["next"])
        if new_next != item["next"]:
            progressed = True
        item["next"] = new_next
    if "pr_number" in data:
        item["pr_number"] = _radar_number(data.get("pr_number"), "pr_number")
        progressed = True
    if "claim_comment_id" in data:
        item["claim_comment_id"] = _radar_number(data.get("claim_comment_id"), "claim_comment_id")
    ci_state = data.get("ci_state")
    if isinstance(ci_state, Mapping):
        # Merged KEY BY KEY, only the declared members, each re-bounded to the record
        # tool's own type and ceiling: a reading that named any other key would
        # otherwise grow the item by key with no bound, and one that carried an
        # oversized member would make every retained item hold it -- the entry type
        # admits an object here, and these are bytes read off a file.
        merged_ci: dict[str, Any] = radar_ci_state(item["ci_state"])
        merged_ci.update(radar_ci_state(ci_state))
        item["ci_state"] = merged_ci
        progressed = True
    labels = data.get("labels_applied")
    if isinstance(labels, list):
        item["labels_applied"] = [_radar_text(x, 256) for x in labels if isinstance(x, str)][
            :RADAR_LABELS_LIMIT
        ]
    if isinstance(data.get("outcome"), str):
        item["outcome"] = _radar_text(data["outcome"]).strip() or None
    tried = data.get("tried")
    if (
        isinstance(tried, Mapping)
        and isinstance(tried.get("approach"), str)
        and tried["approach"].strip()
    ):
        row = {
            "approach": _radar_text(tried["approach"]).strip(),
            "rejected_because": _radar_text(tried.get("rejected_because")),
        }
        # A carried entry RE-STATES a record, and a carry that did not fully land is
        # run again, so the same rejected approach can arrive twice; a live entry
        # is one call and appends as it always did.
        already = carried and any(
            r.get("approach") == row["approach"]
            and r.get("rejected_because") == row["rejected_because"]
            for r in item["tried"]
        )
        if not already:
            item["tried"].append({**row, "at": ts})
            del item["tried"][: max(0, len(item["tried"]) - RADAR_TRIED_LIMIT)]
            progressed = True

    # Stamps come off the entry's own clock. ``claimed_at`` is stamped once, the
    # first time the item is in any phase past ``selected``; ``last_progress_at``
    # moves ONLY on real progress, because the claim TTL is measured from it and a
    # bare read-back must not renew a claim. A CARRIED entry brings its own stamps:
    # it re-states a record that already had them, and re-stamping would make every
    # carried claim look freshly made.
    if carried:
        for stamp in ("claimed_at", "last_progress_at", "finished_at"):
            if stamp in data:
                item[stamp] = _radar_text(data.get(stamp), 64) or None
        if item["last_progress_at"] is None:
            item["last_progress_at"] = ts
    else:
        if item["claimed_at"] is None and item["phase"] != "selected":
            item["claimed_at"] = ts
        if item["last_progress_at"] is None or progressed:
            item["last_progress_at"] = ts
        if item["phase"] in RADAR_TERMINAL_PHASES:
            if not item["finished_at"]:
                item["finished_at"] = ts
        else:
            # Reopened, or never finished: a resolved issue can come back and be
            # handled again by the same crew, which reuses this very item, so EVERY
            # field that describes a finished result is dropped together.
            item["finished_at"] = None
            item["outcome"] = None
    items[key] = item
    _radar_bound_items(state, keep=key)

    if skip_row is not None:
        _radar_record_skip(state, key, number, skip_row, crew_id, ts)

    # The line carries ``phase`` ONLY when this entry created the item or moved it,
    # so a reader can treat "a line carrying a phase" as "an ENTRY into that phase":
    # a CI reading that leaves the item in ``awaiting-ci`` must not reset the lane's
    # dwell clock, or the item polled most often is the one whose stall is hidden.
    moved = existing is None or prev_phase != item["phase"]
    line: dict[str, Any] = {
        "id": line_id,
        "ts": ts,
        "crew_id": crew_id,
        "number": number,
        "kind": kind,
        "text": text,
    }
    if moved:
        line["phase"] = item["phase"]
        phase_lines: dict[str, list[dict[str, str]]] = state["phase_lines"]
        rows = phase_lines.setdefault(key, [])
        rows.append({"phase": item["phase"], "at": item["last_progress_at"] if carried else ts})
        del rows[: max(0, len(rows) - RADAR_PHASE_LINE_LIMIT)]
    events.append(line)
    del events[: max(0, len(events) - RADAR_EVENT_LIMIT)]


def _radar_render(state: dict[str, Any]) -> dict[str, Any]:
    """The crew's ledger, in the shapes its readers already expect.

    ``items`` newest progress first and ``events`` newest first, the orders the crew
    page has always listed them in. ``skips`` is THIS crew's contribution to the
    repository's shared index -- the index itself is the union over every crew of
    the repository, folded by the app. ``phase_lines`` is the per-item history of
    phase entries the pipeline view draws lanes from.
    """
    items = sorted(
        (
            dict(
                record,
                tried=[dict(row) for row in record["tried"]],
                ci_state=dict(record["ci_state"]),
                labels_applied=list(record["labels_applied"]),
            )
            for record in state["items"].values()
        ),
        key=lambda record: record.get("last_progress_at") or "",
        reverse=True,
    )
    return {
        "schema": RADAR_SCHEMA_VERSION,
        "crew_id": state["crew_id"],
        "owner": state["owner"],
        "repo": state["repo"],
        "items": items,
        "events": [dict(line) for line in reversed(state["events"])],
        "skips": {key: dict(row) for key, row in state["skips"].items()},
        "phase_lines": {
            key: [dict(row) for row in rows] for key, rows in state["phase_lines"].items()
        },
        "counts": {
            "open": sum(
                1
                for record in state["items"].values()
                if record["phase"] not in RADAR_TERMINAL_PHASES
            ),
            "evicted_items": state["evicted_items"],
            "evicted_skips": state["evicted_skips"],
        },
    }


# --------------------------------------------------------------------------- #
# class -- what kind of session this log belongs to, over its whole life
# --------------------------------------------------------------------------- #


def _class_start() -> dict[str, Any]:
    return {
        # Whether the FIRST opener this fold saw stated a class. ``saw_opener`` is
        # what makes it the first one rather than any one: a log carries an opening
        # entry per re-attachment, so a log whose original opener predates the field
        # gains a later one that does state a class, and letting that set ``opened``
        # would date the log by an entry written long after the part whose class is
        # unknown.
        "opened": False,
        "saw_opener": False,
        "stated": 0,
        "memory": "",
        "app": "",
        "channel": False,
        # The workspace the FIRST stated class named, and whether a later one named a
        # different one. Not folded most-restrictively like the members above, because a
        # workspace is an identity rather than a restriction -- there is no "more
        # restrictive" workspace to keep. What a reader needs is whether ONE workspace
        # owns this log's whole content, so the first is kept and any move is recorded as
        # a fact of its own. A log that moved belongs to no single workspace, and a
        # cross-session read of it is refused whichever workspace asks.
        "workspace": "",
        "workspace_moved": False,
        # The last seq this fold RECEIVED, and whether the history it saw has a hole
        # in it. Separate from ``complete``, which is about the log's beginning: a log
        # can begin properly and still be missing a record in the middle.
        "last_seq": 0,
        "damaged": False,
    }


def _class_read(data: Mapping[str, Any]) -> dict[str, Any] | None:
    """The class members of *data*, or ``None`` when it states no class.

    ``memory`` is required, so its absence is what says a class was not stated. A
    line carrying the other members without it is a fragment, and a fragment reads
    as nothing stated rather than as a class with an unknown memory mode -- the
    same rule the reader applies to a missing object.

    ``workspace`` is NOT required, and that is deliberate: whether a class was stated
    and which workspace stated it are two questions, and a line that names a memory
    mode did state a class. An absent workspace reads as the empty string, which no
    live slot can produce (a slot's workspace defaults to ``default``), so the arm that
    compares workspaces refuses on it rather than treating it as a match.
    """
    memory = data.get("memory")
    if not isinstance(memory, str) or not memory:
        return None
    app = data.get("app")
    workspace = data.get("workspace")
    return {
        "memory": _as_str(memory),
        "app": _as_str(app) if isinstance(app, str) else "",
        "channel": data.get("channel") is True,
        "workspace": _as_str(workspace) if isinstance(workspace, str) else "",
    }


def _class_absorb(state: dict[str, Any], stated: dict[str, Any]) -> None:
    """Fold *stated* into *state*, keeping the most restrictive value ever held.

    Restrictive, not latest, and that is the whole semantics of this fold. The
    question a reader asks is whether this log could hold content that must not
    cross a boundary, and content is durable: a session published to a channel for
    one turn holds that turn's words for good, so a later turn reporting no channel
    does not make the log readable again. The same reasoning covers an app that
    owned the session and a memory mode that was ever not persistent.

    So each member only ever moves AWAY from the permissive value: ``channel``
    latches true, ``app`` keeps the first owner it ever had, and ``memory`` keeps
    the first non-persistent mode. A member that has never been restrictive tracks
    what was last stated, which is what makes an ordinary session's fold read as
    the ordinary class rather than as an empty one.
    """
    state["stated"] += 1
    if state["memory"] == "" or state["memory"] == "persistent":
        state["memory"] = stated["memory"]
    if not state["app"] and stated["app"]:
        state["app"] = stated["app"]
    state["channel"] = state["channel"] or stated["channel"]
    # Workspace is the exception to the paragraph above: it is an identity, so there is
    # no more-restrictive value to keep. The first one stated is kept, and a later one
    # that differs sets ``workspace_moved`` -- which is itself the restrictive fact,
    # since a log whose content spans two workspaces is owned by neither.
    if not state["workspace"]:
        state["workspace"] = stated["workspace"]
    elif stated["workspace"] and stated["workspace"] != state["workspace"]:
        state["workspace_moved"] = True


def _class_step(state: dict[str, Any], entry: Entry) -> None:
    # Seq CONTIGUITY, and for this fold only. The store skips an unparseable interior
    # line deliberately -- its own words: one unreadable record must not make the rest
    # of the file unreadable -- and that is right for a fold accumulating totals, where
    # a lost entry costs a count. It is wrong for this one: the skipped line may be the
    # SOLE record of a restriction, and dropping it turns a restricted log into a
    # permissive answer, which is an authorization ceiling raised by byte damage. So a
    # gap in the seqs this fold receives marks the history damaged and the reader
    # refuses on it, while every other fold keeps the store's tolerance.
    #
    # The FIRST entry seen is accepted at whatever seq it carries: a log whose front
    # retention took does not begin at 1, and that is the reader's own check to make,
    # from the segment names, rather than a hole reported from the middle.
    previous = state["last_seq"]
    state["last_seq"] = entry.seq
    if previous and entry.seq != previous + 1:
        state["damaged"] = True
    if entry.type == "write/dropped":
        # The log itself saying an append was permanently lost. For a fold whose
        # answer is an authorization ceiling that is a hole: the lost append may have
        # been the class move that restricted this session, and nothing else records
        # it. This is what lets the RECORDER be best-effort at the call site -- a
        # class move that never reaches the file cannot leave the log readable.
        state["damaged"] = True
        return
    if entry.type == "session/opened":
        first = not state["saw_opener"]
        state["saw_opener"] = True
        recorded = entry.data.get("class")
        if not isinstance(recorded, Mapping):
            # A log opened before the field existed. Nothing is absorbed and
            # ``opened`` stays false, so the render reports a history with no
            # beginning rather than an unrestricted session.
            return
        stated = _class_read(recorded)
        if stated is None:
            # The object is THERE and cannot be read, which is damage rather than
            # age: a writer that records the field records it whole, and the
            # declaration refuses a fragment at append. Absence is a date; an
            # unreadable presence is a hole.
            state["damaged"] = True
            return
        if first:
            # Only the log's own beginning can date it. A later opener is written
            # when a new gateway process re-attaches to the same session, so on a log
            # whose first opener predates the field it would otherwise supply a
            # beginning for a stretch of the log it was not present for.
            state["opened"] = True
        _class_absorb(state, stated)
    elif entry.type == "session/class":
        stated = _class_read(entry.data)
        if stated is None:
            # A move was recorded and cannot be read. What it moved TO is the whole
            # content of this entry, so skipping it discards a transition this fold
            # exists to carry -- and the direction it discards is always toward
            # permissive, since only a restriction is worth recording a move for.
            state["damaged"] = True
            return
        _class_absorb(state, stated)


def _class_render(state: dict[str, Any]) -> dict[str, Any]:
    return {
        # Whether any class was stated at all. False for a log written before the
        # class was recorded, and for one whose class-bearing entries retention has
        # taken.
        "recorded": state["stated"] > 0,
        # Whether the history has a BEGINNING -- the log's FIRST opening entry
        # stated a class. A fold that saw only transitions knows the class moved and
        # not what it moved from, so it cannot report the earliest class the log
        # held, and a reader deciding an authorization question must treat that as
        # unknown. A LATER opener does not supply that beginning: one is written per
        # re-attachment, so on a log whose first opener predates the field it would
        # date a stretch of the log it was not present for.
        # This is also what dates the log: the opening ``class`` object and
        # ``session/class`` were declared together, so a log stating the first was
        # written by a build that records the second, and its absence of transitions
        # is therefore a real account of a class that never moved rather than the
        # silence of a writer that could not say.
        "complete": state["opened"],
        # Whether the history this fold saw has a HOLE: a seq the store skipped
        # because the line was unreadable, or a class record present and unreadable.
        # Distinct from ``complete`` at the other end of the same question -- that one
        # is about the log's beginning, this one about its middle -- and a reader
        # deciding an authorization question refuses on either, because the record a
        # hole swallows is more likely to be a restriction than a relaxation: only a
        # restriction is worth writing a move for.
        "damaged": state["damaged"],
        "memory": state["memory"],
        "app": state["app"],
        "channel": state["channel"],
        # Which workspace owns this log's content, and whether more than one ever did.
        # A cross-session read compares the first against the caller's own workspace and
        # refuses on the second, so an empty ``workspace`` (no live slot can state one)
        # and a moved one both refuse rather than matching.
        "workspace": state["workspace"],
        "workspace_moved": state["workspace_moved"],
    }


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
# A slot's folds on the projection kernel
# --------------------------------------------------------------------------- #

#: What ONE RETAINED ROW of each slot fold costs, measured rather than reasoned about:
#: the marginal bytes of the WIDEST row kind that fold keeps.
#:
#: PER ROW AND NOT PER CELL, which is the whole shape of this budget and not a detail.
#: A per-cell figure measured at the caps cannot bound these states, because three of
#: the four nest a capped list INSIDE each capped item -- so their cap-state is the
#: PRODUCT of two caps rather than the sum:
#:
#:   ``radar``   :data:`RADAR_ITEM_LIMIT` items x :data:`RADAR_TRIED_LIMIT` tried rows,
#:               and one ``phase_lines`` key per item x :data:`RADAR_PHASE_LINE_LIMIT`
#:   ``work``    :data:`WORK_ITEM_LIMIT` items x :data:`WORK_EVENT_LIMIT` events
#:   ``panel``   :data:`PANEL_OWNER_LIMIT` owners x :data:`PANEL_HISTORY_LIMIT` rows
#:   ``ledger``  flat
#:
#: So ``radar``'s cap-state is 156,500 rows, which at the figure below is 24,402 MiB --
#: not the 26 MiB a cell driven to its items and skips alone measures, and not the
#: 995,342 bytes the spec recorded before that. A static per-cell charge is therefore not
#: an upper bound at all, and a budget built on one is not a bound. The four cap-state
#: charges are ``radar`` 24,402 MiB, ``work`` 165.6 MiB, ``panel`` 10.1 MiB, ``ledger``
#: 2.8 MiB.
#:
#: HOW MEASURED. Each row kind is driven with its free-text fields at the clamp the FOLD
#: applies, in 4-byte UTF-8 characters, because the clamps count CHARACTERS and an
#: ASCII row is a quarter of the bytes the same clamp admits. The cost is
#: ``(bytes at N rows - bytes at 1 row) / (N - 1)`` of
#: ``len(json.dumps(state, ensure_ascii=False).encode("utf-8"))``, except ``radar``,
#: whose figure is the widest item weighed alone (each item brings narrow companion
#: rows that pull an average below it), and ``panel``, driven in ASCII: its row is one
#: entry's document kept whole, so the entry's byte cap binds, and ASCII fills it widest.
#: ``test_every_slot_folds_row_cost_is_derived_from_its_own_rows`` re-derives all four,
#: so a clamp raised without re-measuring fails CI rather than quietly invalidating this.
#:
#: ONE FIGURE PER FOLD, the widest of its own kinds, so a ``radar`` skip, tried or
#: progress row is charged as a whole item. That over-charges every cell but one made of
#: full items, which is the safe direction: the budget then holds fewer cells than its
#: bytes allow, never more.
_SLOT_FOLD_ROW_BYTES: Final[dict[str, int]] = {
    # a published document per owner, filling one entry (51,655 measured)
    "panel": 52_000,
    # a work item with every field it keeps at its clamp (163,464 measured)
    "radar": 163_500,
    # a tried row: two fields at LEDGER_TEXT_LIMIT (16,065 measured)
    "ledger": 16_100,
    # an item with title, summary and decision at their clamps; its acceptance and
    # artifacts have no clamp and are charged per container (``_work_opaque``)
    "work": 2_705,
    # one agentic field: its value at DASHBOARD_VALUE_BYTES, plus the field name,
    # the declared type, the stamp and the ownership digest
    "agentic": 8_400,
    # one mistake group: code, field and reason at their clamps, the ownership
    # digest, two stamps and the bounded correction list (772 measured)
    "mistakes": 1_100,
    # a workstreams task with title and summary at their clamps, beside the hourly
    # buckets a board and a spender each keep (measured below, see the test)
    "workstreams": 1_150,
}

#: Default ceiling on the bytes EVERY warm slot cell may hold together, across every
#: data home, slot and fold. 256 MiB.
#:
#: Chosen so that one fold at its declared caps still FITS and the one that cannot is the
#: one that must not: ``work`` at 256 items x 200 events is charged 165.6 MiB (133.6
#: for its rows, 32 for each item's ``acceptance`` and ``artifacts`` at the entry bound,
#: :func:`_work_opaque`), ``panel``
#: 10.1 MiB, ``ledger`` 2.8 MiB -- while ``radar`` at its caps is 24,402 MiB and is refused
#: rather than fitted (:func:`_remember_slot_fold`). A smaller default would start
#: refusing the biggest work board, which is the one a cold fold costs most.
#:
#: It is a ceiling on CHARGES, not on bytes allocated, and the charge runs about 4x over a
#: real cell's serialized size (one row cost covers the fold's widest row kind). So a full
#: table at this ceiling is nearer 60 MiB resident than 256.
#:
#: The ceiling it replaces admitted 64 cells of any size, so its own worst case was
#: 64 x 24,402 MiB, which no number in the code had to move to reach.
DEFAULT_SLOT_FOLD_CACHE_BYTES: Final[int] = 256 * 1024 * 1024

#: The variable an operator lowers the ceiling with, in BYTES. Read per call, so a test
#: or a small deployment can set it without a restart; an unparseable or non-positive
#: value leaves the default standing rather than switching the cache off, because a
#: typo'd ceiling must not silently cost every read a cold fold.
SLOT_FOLD_CACHE_BYTES_ENV: Final[str] = "KIROCREW_SLOT_FOLD_CACHE_BYTES"

#: Charged per row to a fold that declares no row cost, so an unmeasured fold cannot make
#: the budget unbounded by costing nothing. 64 KiB is :data:`MAX_ENTRY_BYTES`: no row can
#: hold more than the entry that carried it.
_UNMEASURED_ROW_BYTES: Final[int] = 64 * 1024


def slot_fold_cache_bytes() -> int:
    """The live ceiling on all warm slot cells together, in bytes.

    One accessor rather than a constant, because the variable is read per call and the
    eviction loop and every test that reports the budget must agree on the same number.
    """
    raw = os.environ.get(SLOT_FOLD_CACHE_BYTES_ENV, "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value > 0:
            return value
    return DEFAULT_SLOT_FOLD_CACHE_BYTES


def slot_fold_row_bytes(name: str) -> int:
    """What one retained row of the *name* fold is charged. See :data:`_SLOT_FOLD_ROW_BYTES`."""
    return _SLOT_FOLD_ROW_BYTES.get(name, _UNMEASURED_ROW_BYTES)


def slot_fold_cell_bytes(name: str, state: "Mapping[str, Any] | None" = None) -> int:
    """An upper bound on what *state* retains, charged against the ceiling.

    ``rows + 1`` rather than ``rows``: the extra row covers the fold's flat header fields
    -- a ``goal``, a ``next``, a crew id -- which belong to no row and which an empty cell
    holds anyway. It also means a cell is never charged zero.

    COUNTING, not serializing. The count is a handful of ``len`` calls plus one pass over
    the items (bounded by the fold's own item cap), where weighing the state itself would
    mean serializing up to 2.4 GiB on every store -- more than the fold that produced it.
    And counting is still an UPPER BOUND, because each row's own text is clamped by the
    fold, and a value the fold keeps whole is charged the entry that carried it
    (``count_opaque``): that is what a static per-cell figure could not be.

    *state* absent answers the one-row floor, which is what a caller asking about a fold
    rather than about a cell wants.
    """
    fold = _FOLDS.get(name)
    rows = 0
    opaque = 0
    if state is not None and fold is not None:
        if fold.count_rows is not None:
            rows = max(0, fold.count_rows(state))
        if fold.count_opaque is not None:
            opaque = max(0, fold.count_opaque(state))
    return (rows + 1) * slot_fold_row_bytes(name) + opaque * MAX_ENTRY_BYTES


class _Ordinal(NamedTuple):
    """One entry of a slot's concatenated stream, at a seq that grows across units.

    A crew log's ``seq`` restarts at 1 in every unit file, while the kernel holds ONE
    watermark per cell and drops an event at or below it. So the units' own seqs cannot
    be handed over as they are: the second unit's entries all sit at or below the
    first's and would be dropped as a re-fold -- the collision :func:`advance` names,
    arriving for a legitimate reason. ``ordinal`` is what the kernel orders by instead:
    the entry's own seq plus the heights of the units already streamed, which grows
    across the whole concatenation.

    Two properties come out of that, and both are needed. It never goes backwards, so
    no entry is wrongly dropped. And one entry always maps to the SAME ordinal, so
    re-driving a range costs the overlap and nothing else -- which a plain running
    counter would lose, because a re-read entry would take a new number and be counted
    a second time.

    ``entry`` is the crew log entry untouched, so the fold sees exactly the record the
    file holds.
    """

    ordinal: int
    entry: Entry


def _ordinal_seq(event: _Ordinal) -> int:
    """The kernel's ordering number for one wrapped entry."""
    return event.ordinal


@dataclass(frozen=True)
class _UnitMark:
    """One unit's log identity, height and byte fingerprint, as the file reports them.

    ``origin`` is :func:`log_origin`, the same value the session folds and the on-disk
    savepoints compare with; ``None`` is an unknown identity and never matches, so a
    unit whose log cannot be read folds cold. It is in the reuse test because a seq
    alone cannot tell a log that GREW from one REMOVED AND RECREATED under the same id
    whose seq has already climbed back to or past the remembered one.

    ``size`` and ``mtime_ns`` are the stat-only fingerprint :func:`_log_identity`
    already computes for the session fold, over exactly the segment set a walk would
    read. They are here because identity and height together still describe a log only
    by how FAR it goes, never by what it says: an already-folded entry rewritten in
    place keeps its seq, so a mark without them compares equal to a log whose bytes
    have changed underneath a cell, and the stale cell is then served to
    :func:`rebuild_from_projection`, which writes it back over the record. A changed
    fingerprint folds cold instead. Both come from the same call that yields
    ``origin``, so carrying them costs no extra stat.

    The fingerprint settles the units a continuation does not re-read. It cannot settle
    the newest one, which a continuation exists to let GROW: an append moves size and
    mtime by itself, so a stat has nothing left to compare there. That unit is settled
    instead by :class:`_PrefixSeen`, a decode-free digest of the records already folded,
    which is the only evidence that distinguishes a file that grew from one truncated
    and regrown to the same reading.
    """

    origin: str | None
    last_seq: int
    size: int | None = None
    mtime_ns: int | None = None


def _unit_mark(unit_id: str) -> _UnitMark:
    """*unit_id*'s log identity, newest seq and fingerprint, or an unknown mark.

    ``origin`` is ``None`` for an unreadable or header-less log, and every caller
    treats that as unknown and folds cold -- which is also why the fingerprint needs no
    separate unknown test: :func:`_log_identity` returns an identity only when the same
    stat calls that produced the fingerprint succeeded.
    """
    try:
        handle = open_session_log(unit_id)
        if handle is None:
            return _UnitMark(None, 0)
        origin, size, mtime_ns = _log_identity(handle)
        # ``last_seq`` on a freshly opened handle is read off the file's tail, which
        # is what makes it usable as a growth signal for a reader that never appends.
        return _UnitMark(origin, int(getattr(handle, "last_seq", 0) or 0), size, mtime_ns)
    except Exception:
        return _UnitMark(None, 0)


def _unit_marks(unit_ids: Sequence[str]) -> "tuple[_UnitMark, ...]":
    """Every unit's mark, in the order they are folded."""
    return tuple(_unit_mark(unit_id) for unit_id in unit_ids)


#: Stands for "every record in the file" when a digest wants no boundary.
#: :meth:`CrewLog.raw_prefix_digest` stops at end of file and reports the count it
#: reached, so a boundary above any real log yields the whole-file digest.
_ALL_RECORDS = 1 << 62


class _PrefixSeen(NamedTuple):
    """A digest of the newest unit's raw records, read at one known moment.

    ``records`` is a RAW record count, not an entry span: a blank or unparseable
    interior line is a record to the digest walk while the fold skips it, so the two
    numbers differ and only the walk's own count can bound it.
    """

    records: int
    sha: str


def _unit_prefix(unit_id: str, records: int = _ALL_RECORDS) -> "_PrefixSeen | None":
    """*unit_id*'s raw digest through *records*, or the whole file; ``None`` if unread.

    Decode-free: :meth:`CrewLog.raw_prefix_digest` frames and hashes raw records
    without parsing any of them, which is what makes this affordable on a read path.
    A short walk reports the count it reached, and the caller compares counts.
    """
    try:
        handle = open_session_log(unit_id)
        if handle is None:
            return None
        sha, hashed = handle.raw_prefix_digest(records)
        return _PrefixSeen(records=hashed, sha=sha)
    except Exception:
        return None


def _prefix_holds(unit_id: str, seen: _PrefixSeen) -> bool:
    """Whether *unit_id*'s first ``seen.records`` raw records still hash to *seen*.

    Growth above them is not a change, because the walk stops at the count *seen*
    names. This is the question a stat cannot answer: a size and an mtime say a file
    moved, never whether the bytes a fold already consumed are the same bytes.
    """
    again = _unit_prefix(unit_id, seen.records)
    return again is not None and again.records == seen.records and again.sha == seen.sha


class _SlotFold:
    """One SLOT-keyed crew-log fold as a :mod:`kiro_crew.projection` unit.

    :class:`_SessionFold` with the two differences a concatenation of units brings.
    The event arriving is an :class:`_Ordinal`, so ``apply`` unwraps before the fold
    sees it -- the kernel orders by the wrapper's number and the fold reads the entry.
    And the starting state is BOUND to the slot for a fold that declares ``bind_slot``,
    so a slot-keyed fold knows which board it answers for before the first entry
    instead of guessing it from whichever entry comes first.

    The same-reference rule is kept the same way: an entry the fold cannot be moved by
    returns the state untouched, and any other entry is stepped onto a copy.
    """

    def __init__(self, fold: _Fold, slot: str) -> None:
        self._fold = fold
        self._slot = slot
        self.key = fold.name
        self.state_version = fold.state_version

    def init(self) -> dict[str, Any]:
        state = self._fold.start()
        if self._slot and self._fold.bind_slot is not None:
            self._fold.bind_slot(state, self._slot)
        return state

    def apply(self, state: dict[str, Any], event: _Ordinal) -> dict[str, Any]:
        entry = event.entry
        if not self._fold.touched_by(entry):
            return state
        grown = self._fold.copied(state)
        self._fold.step(grown, entry)
        return grown

    def view(self, state: dict[str, Any]) -> dict[str, Any]:
        return self._fold.render(state)


class _SlotStream:
    """A slot's units as one stream the kernel can order, oldest unit first.

    Each unit's base is the total height of the units ALREADY STREAMED, counted as
    this pass actually saw them rather than taken from a sampled height -- so an
    append landing in an earlier unit while this runs still cannot push one of its
    entries onto an ordinal a later unit has already used. A unit with no log
    contributes nothing and is skipped, the same as the cold fold: a slot whose
    oldest unit was collected by retention still folds the ones it has.

    Iterating is a GENERATOR, so the kernel folds one entry at a time and a cold
    fold of a long slot holds no more than that.

    Two numbers the stream cannot return and its caller needs afterwards.
    :attr:`heights` is the last seq FOLDED from each unit that had a log, which is
    what lets the memo record what this pass really consumed instead of what was
    sampled before it. :attr:`reached` is that height for the newest such unit, the
    only figure a later read of the same slot can compare against and the one
    :func:`fold_slot_checkpoint` has always answered with; it starts at *since*, so a
    warm tail that turned out to be empty reports the position it resumed from rather
    than dropping to zero.
    """

    def __init__(self, unit_ids: Sequence[str], *, base: int = 0, since: int = 0) -> None:
        self._unit_ids = tuple(unit_ids)
        self._base = base
        self._since = since
        self.reached = since
        self.heights: dict[str, int] = {}

    def __iter__(self) -> "Iterator[_Ordinal]":
        base = self._base
        for unit_id in self._unit_ids:
            handle = open_session_log(unit_id)
            if handle is None:
                continue
            top = base
            reached = self._since
            for entry in handle.iter_from(self._since + 1, known=KNOWN_TYPES):
                ordinal = base + entry.seq
                top = max(top, ordinal)
                reached = max(reached, entry.seq)
                yield _Ordinal(ordinal, entry)
            base = top
            self.heights[unit_id] = reached
            self.reached = reached


@dataclass
class _SlotMemo:
    """One slot fold kept warm: the kernel cell holding it, and what it folded over.

    The registry is per (slot, fold), not one shared instance, and that is a
    correctness requirement rather than a preference: ``prime`` folds and RESETS every
    definition registered in it, so a shared registry would make a cold refold of one
    fold discard another's warm cell -- and the three slot folds are read over
    different unit lists, so they go stale independently.

    ``marks`` is the per-unit watermark vector the reuse test compares, and the bases
    the ordinals are built from are derived from it: unit *i*'s base is the total
    height of the units before it. That derivation is what makes a warm continuation
    land on the same ordinals the earlier pass used, and it holds because a memo is
    only kept when every earlier unit's mark is unchanged.

    A memo is never edited once stored. A read that carries one forward stores a NEW
    memo over the SAME registry, so two reads racing on one slot cannot leave a pair of
    fields half updated: the registry's cell is the single truth and holds its own lock,
    and re-driving a range it has already folded is dropped on its watermark rather
    than counted twice.
    """

    registry: ProjectionRegistry
    store: str
    units: "tuple[str, ...]"
    marks: "tuple[_UnitMark, ...]"
    reached: int
    #: The NEWEST unit's raw digest as of the pass that built this cell, and the one
    #: thing a continuation must check that a mark cannot. Every earlier unit is held
    #: to whole-mark equality, so a rewrite there already folds cold; the newest unit is
    #: admitted precisely because it GREW, and growth moves its size and mtime by
    #: itself, which leaves a stat with nothing to say about the bytes underneath.
    #: ``None`` means no digest is vouched for -- the file moved during the pass, or
    #: could not be read -- and a continuation is refused rather than trusted.
    prefix: "_PrefixSeen | None" = None
    #: The kernel ORDINAL this cell is persisted through on disk
    #: (:func:`kiro_crew.crew_log.checkpoint.save_slot`), or 0 when nothing is. What
    #: :func:`_persist_slot_fold` measures a write against, carried on the cell so a
    #: continuation decides without reading the file to find out.
    saved: int = 0
    #: What ORDERS this cell against every other value of the same (slot, fold). Minted
    #: when the cell is built, so a cell CARRIED FORWARD unchanged keeps its number and a
    #: cell that folded anything new gets a higher one. See :func:`_next_slot_revision`.
    revision: int = 0
    #: What this cell is charged against the ceiling, counted ONCE when it is built
    #: (:func:`slot_fold_cell_bytes`). Stored rather than recomputed because the eviction
    #: loop sums every held cell, and re-counting there would make one store cost a walk
    #: of every warm cell's items.
    weight: int = 0


_slot_memos: "dict[tuple[str, str, str], _SlotMemo]" = {}
_slot_memo_guard = threading.Lock()

#: The last revision this process handed out, for any (slot, fold). Guarded by
#: :data:`_slot_memo_guard`.
#:
#: ONE COUNTER FOR EVERY KEY rather than one per key, which costs a shared number and
#: buys the property the push needs with nothing to evict: a per-key counter would have
#: to live somewhere, and the only table it could live in is the memo table -- which is
#: evicted, so an evicted key's next cell would restart at 1 and a client holding the
#: earlier number would discard every value after it. A process-wide counter is
#: strictly increasing for every key at once, so it is monotonic per key as a
#: consequence rather than as a thing to maintain.
#:
#: NOT comparable ACROSS PROCESSES, and nothing asks it to be: a frame carries the
#: revision of the process that folded it, and a client reconnecting to another gateway
#: re-reads rather than continues. In process, a restart folds cold and starts at 1
#: again, which a client that reconnected has already discarded its own state for.
_slot_revision = 0


def _next_slot_revision() -> int:
    """The next revision, strictly above every one already handed out.

    Caller holds :data:`_slot_memo_guard`. Never derived from a seq, a time or a file:
    every one of those can repeat or step backwards across a unit rollover, a unit
    recreated under the same id, or a rewritten prefix -- the three cases
    :func:`fold_slot_warm` names -- and the whole purpose of this number is to order two
    values that those cases leave looking identical.
    """
    global _slot_revision
    _slot_revision += 1
    return _slot_revision


@dataclass
class _FoldLock:
    """One key's fold lock, beside the number of passes holding or waiting for it.

    The count is what makes the table droppable: an entry at zero is one no pass can be
    inside, so replacing it with a fresh lock serializes exactly what it needs to.
    """

    lock: threading.Lock
    holders: int = 0


#: One lock per (data home, slot, fold), held across a whole pass of
#: :func:`fold_slot_warm`. Created under :data:`_slot_memo_guard`.
#:
#: WHY A PASS AND NOT A DICT ACCESS. Two warm passes on one key SHARE the cell: the
#: continuation drives ``memo.registry``, and :func:`_slot_checkpoint` then reads that
#: live cell and pairs it with the caller's OWN ``reached``. So a pass that read a
#: shorter tail could return the other pass's newer state labelled with its own older
#: seq -- and ``seq`` is what a reader truncates against, so the value is newer than the
#: number that describes it. The kernel's watermark repairs the CELL on a later read; it
#: cannot repair a value already returned.
#:
#: The pairing matters here because two readers on one slot is the NORMAL mode: the
#: append-driven wake folds the same board a dashboard poll is reading, which is what eager
#: folding is for. A lock is therefore the mechanism rather than a documented caveat.
#:
#: PER KEY, not one lock, because two slots have nothing to share. And held across the
#: pass rather than around the drive alone: the state and the seq are read at different
#: moments and it is their PAIRING that must be atomic. A caller that waits here waits
#: for a fold it would otherwise have duplicated, and finds the cell warm when it
#: arrives, so the lock costs no wall time it was not already paying.
#:
#: WHAT BOUNDS THIS TABLE. One entry per key with a pass holding or waiting for it, so
#: its size is the folds running right now rather than the slots this process has ever
#: folded. :func:`_release_fold_lock` drops an entry when its last holder leaves, which is
#: the only moment at which dropping one is safe: a lock handed out twice as two different
#: objects serializes nothing, so an entry with a waiter has to stay.
_slot_fold_locks: "dict[tuple[str, str, str], _FoldLock]" = {}


def _acquire_fold_lock(key: "tuple[str, str, str]") -> "_FoldLock":
    """Claim one (data home, slot, fold)'s lock entry, creating it on first use.

    Claiming is counting a holder, not taking the lock: the caller takes ``entry.lock``
    itself, outside :data:`_slot_memo_guard`, because a pass holds it while folding and
    the guard is taken inside that pass. Every claim owes a :func:`_release_fold_lock`.
    """
    with _slot_memo_guard:
        entry = _slot_fold_locks.get(key)
        if entry is None:
            entry = _FoldLock(lock=threading.Lock())
            _slot_fold_locks[key] = entry
        entry.holders += 1
        return entry


def _release_fold_lock(key: "tuple[str, str, str]", entry: "_FoldLock") -> None:
    """Give up a claim, and drop the entry once nobody holds or waits for it.

    The identity check keeps a release from deleting an entry some other pass created for
    the same key after this one was dropped.
    """
    with _slot_memo_guard:
        entry.holders -= 1
        if entry.holders <= 0 and _slot_fold_locks.get(key) is entry:
            del _slot_fold_locks[key]


def forget_slot_folds(slot: str = "", name: str = "") -> None:
    """Drop warm slot folds, so the next read folds cold.

    Everything when called with no argument; one slot's, or one fold of one slot's,
    when named. The memo is an optimization with no answer of its own, so dropping it
    costs time and never correctness -- which is what makes this safe as the read
    path's own response to doubt, and what a test isolating one slot's log from
    another's needs.
    """
    home = str(data_home())
    # The locks (``_slot_fold_locks``) are not touched here: an entry exists only while a
    # pass holds or waits for it, and dropping one out from under that pass would hand the
    # next caller a different lock object and reopen the race it exists to close.
    with _slot_memo_guard:
        if not slot and not name:
            _slot_memos.clear()
            _oversize_slot_cells.clear()
            return
        for table in (_slot_memos, _oversize_slot_cells):
            for key in [
                held
                for held in table
                if held[0] == home
                and (not slot or held[1] == slot)
                and (not name or held[2] == name)
            ]:
                del table[key]


def _slot_memo_bytes() -> int:
    """What every warm cell held right now is charged, together. Caller holds the guard.

    Read off each memo's own stored charge rather than recomputed. Re-counting would walk
    every held cell's items on every store, which turns one store into O(all cells) work.
    """
    return sum(memo.weight for memo in _slot_memos.values())


def _remember_slot_fold(key: "tuple[str, str, str]", memo: _SlotMemo) -> None:
    """Keep *memo* under *key*, capped by BYTES; least recently STORED goes first.

    BY BYTES RATHER THAN BY COUNT, which is a reversal of an earlier decision and worth
    stating as one. The count ceiling's case was that each cell is a bounded record, so
    the thing needing a limit is how many are retained -- and that holds exactly as long
    as the cells are comparable. They are not. A ``panel`` cell is a few hundred KiB and a
    ``radar`` cell at its declared caps is 2.4 GiB, so one ceiling of 64 cells priced a
    resident total across four orders of magnitude with no number in the code moving.
    Eager folding is what makes the high end reachable rather than theoretical: the worker
    stores a cell for every board this process WRITES, so which 64 cells are held stopped
    being a function of what a reader asked for.

    So the ceiling is a byte figure (:func:`slot_fold_cache_bytes`) and each cell is
    charged what it RETAINS (:func:`slot_fold_cell_bytes`). Small cells keep many slots
    warm and a huge one keeps few, which is the behaviour a count could not express.

    The earlier decision's second objection stands and is accepted: below the worst case
    the eviction order does mean "whoever has the biggest board loses". That is now the
    POINT -- one 2 GiB cell should lose to four hundred small ones -- rather than a side
    effect, because what is being bounded is resident bytes and not cell count.

    A CELL THAT CANNOT FIT THE WHOLE CEILING IS NOT STORED. ``radar`` at its caps is
    charged more than any sane ceiling, and the two alternatives are both worse: parking
    it would hold 2.4 GiB resident until the next store, and evicting everything else
    first would empty the table to make room for one cell that still does not fit. So it
    is refused, the read still answers correctly, and that slot folds cold next time --
    which costs time and never an answer. The first refusal of a cell is logged at
    WARNING and later ones at DEBUG; the operator's answer is a higher ceiling.

    Eviction order is least recently stored or advanced, which is not the same as least
    recently read: a read that finds the cell already at the file's position returns it
    without storing anything, so it does not move the cell towards the back. A cell can
    therefore be read often while sitting at the front, be evicted, and fold cold on its
    next read. That is the cost of not taking the guard on a read that had nothing to
    record, and it is the cheaper side of the trade.

    Insertion order tracks stores because a cell is never edited in place -- a read or an
    eager fold that carries one forward stores a NEW memo, which moves it to the end.
    """
    ceiling = slot_fold_cache_bytes()
    if memo.weight > ceiling:
        with _slot_memo_guard:
            # Dropped rather than left standing: an older cell of this key describes
            # bytes this pass has already folded past, so serving it later would be a
            # stale answer, where no cell is a cold fold.
            _slot_memos.pop(key, None)
            first = key not in _oversize_slot_cells
            _oversize_slot_cells[key] = memo.weight
            _oversize_slot_cells.move_to_end(key)
            while len(_oversize_slot_cells) > OVERSIZE_SLOT_CELL_LIMIT:
                _oversize_slot_cells.popitem(last=False)
        # WARNING once per cell, where an operator looks: from here on every read of this
        # board folds cold, and the answer is a higher ceiling.
        logger.log(
            logging.WARNING if first else logging.DEBUG,
            "crew log slot fold %s/%s retains %d charged bytes, above the %d-byte "
            "ceiling (%s), so it is not kept warm; every read of it folds cold",
            key[1],
            key[2],
            memo.weight,
            ceiling,
            SLOT_FOLD_CACHE_BYTES_ENV,
        )
        return
    with _slot_memo_guard:
        _slot_memos.pop(key, None)
        _oversize_slot_cells.pop(key, None)
        _slot_memos[key] = memo
        held = _slot_memo_bytes()
        while held > ceiling and len(_slot_memos) > 1:
            oldest, evicted = next(iter(_slot_memos.items()))
            _slot_memos.pop(oldest)
            held -= evicted.weight


#: Cells the last pass REFUSED to keep because their charge was above the whole ceiling,
#: with that charge. The eager worker reads this (:func:`slot_fold_over_ceiling`) and does
#: not advance such a cell on a wake: a pass it cannot store is a cold fold of every unit
#: the slot ran under, paid for a value nobody kept, once per wake. The read path is not
#: affected -- it folds the cell when asked, which is the lazy behaviour -- and a pass that
#: CAN store the cell (a raised ceiling, a state that shrank) clears the entry.
#:
#: BOUNDED, least recently refused first, at :data:`OVERSIZE_SLOT_CELL_LIMIT`. An entry
#: evicted here costs one wasted fold: the next wake for that cell tries it, is refused
#: again, and records it again.
_oversize_slot_cells: "OrderedDict[tuple[str, str, str], int]" = OrderedDict()

#: Most refused cells remembered at once. A refused cell is one at a fold's caps, so even a
#: large fleet holds few; past this the oldest is forgotten and costs one retry.
OVERSIZE_SLOT_CELL_LIMIT: Final[int] = 256


def slot_fold_over_ceiling(slot: str, name: str) -> bool:
    """Whether *slot*'s *name* cell was last refused as too big for the CURRENT ceiling.

    Compared against the ceiling now, not the one in force when the cell was refused, so
    an operator raising ``KIROCREW_SLOT_FOLD_CACHE_BYTES`` lets the next wake advance it.
    """
    key = (str(data_home()), slot, name)
    with _slot_memo_guard:
        weight = _oversize_slot_cells.get(key)
    return weight is not None and weight > slot_fold_cache_bytes()


def _minted_revision() -> int:
    """One fresh revision, taking the guard. For a cell about to be built."""
    with _slot_memo_guard:
        return _next_slot_revision()


def _continuable(marks: "tuple[_UnitMark, ...]", held: "tuple[_UnitMark, ...]") -> bool:
    """Whether *marks* is *held* with the newest unit grown, and nothing else moved.

    The one shape a warm cell can be carried over, because its state was folded
    through every earlier unit already. Everything else describes different bytes and
    folds cold: a changed unit list, a unit whose identity changed or is unknown, an
    EARLIER unit that grew (a forced reset tears a session down while a turn is still
    appending through the handle it holds, so an earlier unit is not closed to writes),
    an earlier unit whose bytes changed without its seq moving, and any unit whose seq
    went backwards.

    "Nothing else moved" is decided by whole-mark equality, so it covers each earlier
    unit's stat fingerprint as well as its identity and height -- that is what makes an
    in-place rewrite of an already-folded entry a cold fold rather than an equal
    comparison. The newest unit is compared on identity and growth alone, because this
    function exists to admit exactly its growth and an append moves its fingerprint by
    itself.
    """
    return (
        bool(marks)
        and len(marks) == len(held)
        and all(mark.origin is not None for mark in marks)
        and marks[:-1] == held[:-1]
        and marks[-1].origin == held[-1].origin
        and marks[-1].last_seq > held[-1].last_seq
    )


def fold_slot_warm(name: str, unit_ids: Sequence[str], *, slot: str) -> Checkpoint:
    """*name* folded over *slot*'s units, CONTINUED from where the last read left it.

    :func:`fold_slot_checkpoint`'s answer, reached incrementally. Folding is O(the
    log), not O(the record): the reader walks every line of every unit to find the few
    that move this fold, and a slot's logs carry its message bodies. The readers that
    pay that are loops that wake on a timer, so the fold is kept in memory per slot and
    advanced over the entries that arrived since -- through the projection kernel,
    whose watermark drops an entry already folded, so the resumed answer and the
    from-scratch answer come out of one implementation.

    A warm read reads ONE file: the newest unit, from its remembered position. A cold
    read streams every unit. Both go through the kernel, so there is no second folding
    path that could disagree with the first about the same bytes.

    A continuation also HASHES that one file's already-folded records before trusting
    them (:func:`_prefix_holds`). The store rewrites a committed prefix on two recovery
    paths -- an unreachable chunk group is truncated away and closers are appended with
    seqs continuing from the cut, and a failed append is truncated back after its bytes
    reached the disk -- and a reader holds no append lock while either runs. Both leave
    a file that has grown since the last read and reuses seqs the fold already consumed,
    which is a pure append to every stat and to every seq comparison. The digest is
    decode-free, so it costs a byte walk of one unit against the parse-and-fold walk of
    every unit that a cold read would pay.

    THE INVARIANT: the heights sampled before a pass reads are its READ PLAN, never the
    cell's claim about itself. What a memo remembers is what the pass actually FOLDED
    (:func:`_folded_marks`). Two things follow. An append landing mid-read is folded in
    and the next read starts above it, so it is neither dropped nor counted twice. And a
    slot that has not moved compares equal and is answered from the cell -- where a memo
    holding the sample would sit permanently one entry short of itself and send every
    later read back to the file for a tail it has already folded.

    PERSISTED, so a second process and a cell that was evicted resume instead of
    folding cold. The savepoint lives beside the newest unit's log and is admitted on
    exactly the shapes :func:`_continuable` admits -- see
    :mod:`kiro_crew.crew_log.checkpoint`'s slot section, which owns the file and every
    condition that retires one. It is an OPTIMIZATION with no answer of its own: a
    missing, stale or unreadable savepoint folds cold, which reaches the same value at
    more cost. What must never happen is a WRONG answer, so every condition
    :func:`_continuable` does not admit refolds from empty rather than carrying state
    that describes other bytes.
    """
    return fold_slot_warm_revised(name, unit_ids, slot=slot)[0]


def fold_slot_warm_revised(
    name: str, unit_ids: Sequence[str], *, slot: str
) -> "tuple[Checkpoint, int]":
    """:func:`fold_slot_warm`, and the REVISION of the cell it answered from.

    Two returns rather than a second lookup, because the pairing has to be atomic. A
    caller that read the checkpoint and then asked the memo table for its revision could
    be handed a number minted by a LATER pass -- and a frame carrying a newer revision
    beside an older value makes the client discard the newer value when it arrives, which
    is the one failure the revision exists to prevent. So the pass that folded reports
    both, inside its own lock.

    :func:`fold_slot_warm` stays the one-value call every existing caller makes: a
    savepoint, a writer continuing a checkpoint and a test have no use for a revision,
    and giving them a tuple to unpack would spread a push's contract over callers that
    do not push.
    """
    require_name(name)
    key = (str(data_home()), slot, name)
    # The whole pass, under this key's own lock. Everything below reads or advances one
    # shared cell and then pairs that cell's state with this pass's own seq, and it is
    # the PAIRING that a second pass would break -- see ``_slot_fold_locks``.
    entry = _acquire_fold_lock(key)
    try:
        with entry.lock:
            return _fold_slot_warm_locked(name, key, unit_ids, slot=slot)
    finally:
        _release_fold_lock(key, entry)


def _fold_slot_warm_locked(
    name: str, key: "tuple[str, str, str]", unit_ids: Sequence[str], *, slot: str
) -> "tuple[Checkpoint, int]":
    """:func:`fold_slot_warm`'s body, with this key's lock already held.

    Split out so the lock's extent is one ``with`` statement rather than an indentation
    a later edit could fall out of: every return below is inside it by construction.
    """
    with _slot_memo_guard:
        memo = _slot_memos.get(key)
    marks = _unit_marks(unit_ids)
    units = tuple(unit_ids)
    known = all(mark.origin is not None for mark in marks)
    if memo is not None and memo.units == units:
        if known and memo.marks == marks:
            # NOTHING FOLDED, so the cell keeps the revision it was built with. Minting
            # one here would move the number on every poll of an idle board, and a client
            # would re-seed from a frame that carries the value it already holds.
            return _slot_checkpoint(name, memo)
        if (
            _continuable(marks, memo.marks)
            and memo.prefix is not None
            and _prefix_holds(units[-1], memo.prefix)
        ):
            before = _unit_prefix(units[-1])
            tail = _SlotStream(
                units[-1:],
                base=sum(mark.last_seq for mark in marks[:-1]),
                since=memo.reached,
            )
            for event in tail:
                memo.registry.drive(memo.store, event)
            grown = _SlotMemo(
                registry=memo.registry,
                store=memo.store,
                units=units,
                marks=_folded_marks(units, marks, tail, carried=memo.marks),
                reached=tail.reached,
                prefix=_vouched(units[-1], before),
                saved=memo.saved,
                revision=_minted_revision(),
                weight=_cell_weight(name, memo.registry, memo.store),
            )
            grown = _persist_slot_fold(name, grown, slot=slot, sampled=marks[-1])
            _remember_slot_fold(key, grown)
            return _slot_checkpoint(name, grown)
    # Nothing warm to carry forward. Ask the DISK before folding every unit: a savepoint
    # beside the newest unit's log reaches the same value over the entries appended since
    # it was written, where the cold fold below streams every unit from its first record.
    if known and units:
        resumed = _resume_slot_fold(name, units, marks, slot=slot)
        if resumed is not None:
            resumed = _persist_slot_fold(name, resumed, slot=slot, sampled=marks[-1])
            _remember_slot_fold(key, resumed)
            return _slot_checkpoint(name, resumed)
    cold = _SlotStream(units)
    registry = ProjectionRegistry(seq_of=_ordinal_seq)
    registry.register(_SlotFold(_FOLDS[name], slot))
    before = _unit_prefix(units[-1]) if units else None
    registry.prime(slot, cold)
    fresh = _SlotMemo(
        registry=registry,
        store=slot,
        units=units,
        marks=_folded_marks(units, marks, cold),
        reached=cold.reached,
        prefix=_vouched(units[-1], before) if units else None,
        # Nothing on disk is standing behind this value: a cold fold is what happens when
        # no savepoint could be used, so the next write is owed for the whole position.
        saved=0,
        # A cold fold is where the three rollover cases land -- a new unit, a unit
        # recreated under the same id, a rewritten prefix -- and every one of them can
        # leave ``reached`` unmoved or lower. The mint is what makes the value after them
        # orderable at all.
        revision=_minted_revision(),
        weight=_cell_weight(name, registry, slot),
    )
    if known:
        fresh = _persist_slot_fold(name, fresh, slot=slot, sampled=marks[-1] if units else None)
        _remember_slot_fold(key, fresh)
    return _slot_checkpoint(name, fresh)


def _slot_cell(name: str, memo: _SlotMemo) -> "tuple[Any, int] | None":
    """*memo*'s kernel cell for *name* as ``(state, watermark)``, or ``None``.

    The watermark is the ORDINAL the cell stands at, which is the number a savepoint
    resumes it at. It is read off the live cell rather than derived from ``reached``,
    because the cell is what ``prime_checkpointed`` installs and a second derivation
    could disagree with it about the same pass.
    """
    try:
        return memo.registry.cells(memo.store)[name]
    except Exception:  # pragma: no cover - a cell the registry did not build
        return None


def _identity_marks(marks: "Sequence[_UnitMark]") -> "list[list[Any]]":
    """*marks* as the JSON a slot savepoint's identity block compares verbatim."""
    return [[mark.origin, mark.last_seq, mark.size, mark.mtime_ns] for mark in marks]


def _resume_slot_fold(
    name: str, units: "tuple[str, ...]", marks: "tuple[_UnitMark, ...]", *, slot: str
) -> "_SlotMemo | None":
    """A warm cell for *slot*'s *name*, resumed from the DISK savepoint, or ``None``.

    The same three steps a warm continuation takes, standing on state a previous PROCESS
    folded instead of on a cell in this one: the savepoint is admitted only if it
    describes this unit vector and the newest unit's prefix still hashes to what its
    writer read, the tail above it is folded through the kernel, and the result is a cell
    exactly like the one a continuation builds.

    The tail goes through ``prime_checkpointed`` rather than a seeding path of its own, so
    a resumed fold and a cold fold run one implementation over the same events -- the
    property that makes the resumed answer and the cold answer the same answer.

    ``None`` for no savepoint and for every refusal, which is the cold fold.
    """
    from kiro_crew.crew_log import checkpoint as savepoints

    handle = open_session_log(units[-1])
    if handle is None:
        return None
    # Unit *i*'s ordinal base is the total height of the units before it, which is the
    # derivation the warm continuation uses and the one the savepoint's own position is
    # checked against. It is reproducible here only because every earlier mark is in the
    # identity block and compared verbatim.
    base = sum(mark.last_seq for mark in marks[:-1])
    saved = savepoints.load_slot(
        handle,
        name,
        slot=slot,
        units=units,
        marks=_identity_marks(marks[:-1]),
        ordinal_base=base,
    )
    if saved is None:
        return None
    # A FULL digest of the newest unit, read before this pass consumes it -- the same
    # thing the cold and warm paths read, and for the same reason. The savepoint's own
    # witness cannot stand in for it: that digest ends at the seq the savepoint was
    # written at, and this pass is about to fold the TAIL above it, so a cell carrying
    # it would vouch for bytes it did not read. Two things go wrong when it does, and
    # both are silent. A continuation then asks :func:`_prefix_holds` about the saved
    # boundary only, so a tail rewritten in place -- the store's own recovery truncates
    # an unreachable chunk group and re-appends with the seqs continuing from the cut --
    # compares equal, and the cell keeps state folded from bytes the file has replaced.
    # And :func:`_persist_slot_fold` would write a witness whose ``seq`` is above its own
    # ``prefix_records``, which :func:`prefix_admit` refuses on the next read, so every
    # other restart pays a cold fold.
    before = _unit_prefix(units[-1])
    registry = ProjectionRegistry(seq_of=_ordinal_seq)
    registry.register(_SlotFold(_FOLDS[name], slot))
    tail = _SlotStream(units[-1:], base=base, since=saved.reached)
    try:
        registry.prime_checkpointed(
            slot,
            _HeldCheckpoints(
                {name: Checkpoint(name=name, last_seq=saved.watermark, state=saved.state)}
            ),
            {},
            lambda _floor: tail,
        )
        # RENDERED here, and the value thrown away. It is the admission condition the
        # shape checks cannot be: they read a state's top level, so a value malformed
        # BELOW it -- an event row holding a number where a mapping belongs -- is
        # admitted, survives a tail that touches nothing, and raises in the render the
        # caller performs AFTER this function has returned. The ledger's reader answers
        # that by logging and serving the empty record, so a payload like this would
        # empty a live slot's ledger on every read for the life of the unit. A state no
        # reader can be served is not a state to resume from, and asking costs one render
        # of a bounded value on a read that resumed.
        _FOLDS[name].render(registry.cells(slot)[name][0])
    except Exception:
        # A state that passed every admission condition and then could not be FOLDED or
        # rendered. The file is retired and the caller folds cold, which is the answer
        # ``_drive_session`` gives for a session savepoint and for the same reason --
        # refusing it on the way in would take a per-fold walk of every nested container.
        log_exception_text(
            logger,
            logging.DEBUG,
            "crew log slot %s could not fold its resumed state; discarding and folding cold",
            slot,
        )
        savepoints.discard_slot(handle, (name,))
        return None
    # The savepoint was admitted BEFORE this pass, and that window is not empty. The
    # store's supersede repair replaces orphan chunk records at seqs the savepoint
    # already consumed; the tail starts above them so this fold never reads the
    # replacement, and ``before`` was captured after the repair so it matches the
    # repaired file -- every check on this side passes while the state came from bytes
    # the repair removed. So the savepoint is re-admitted against the file as it stands
    # now, and one that fails to describe it costs a cold fold.
    if not savepoints.slot_resume_still_verifies(
        handle,
        name,
        slot=slot,
        units=units,
        marks=_identity_marks(marks[:-1]),
        ordinal_base=base,
        resumed=saved,
    ):
        logger.debug(
            "crew log slot %s moved under its own savepoint while folding; folding cold", slot
        )
        return None
    return _SlotMemo(
        registry=registry,
        store=slot,
        units=units,
        # ``carried=marks``: this pass re-stat'ed every unit, and the savepoint's identity
        # block confirmed the earlier ones are the vector its state was folded over, so
        # only the newest unit's height comes from the stream.
        marks=_folded_marks(units, marks, tail, carried=marks),
        reached=tail.reached,
        prefix=_vouched(units[-1], before),
        saved=saved.watermark,
        revision=_minted_revision(),
        weight=_cell_weight(name, registry, slot),
    )


def _persist_slot_fold(
    name: str, memo: _SlotMemo, *, slot: str, sampled: "_UnitMark | None"
) -> _SlotMemo:
    """*memo*, with its savepoint on disk brought forward when a write is owed.

    How far a fold must have moved to earn a write is the savepoint module's decision
    (``checkpoint.write_is_earned``), not this one's: it is a property of the files, and
    stating it here as well would give two places an answer that has to agree.

    A WARM CONTINUATION WRITES TOO, and that is the one place this departs from the
    session half, where a pass standing on a cached bundle writes nothing. The reason the
    session half refuses is that a cached bundle records no digest, so nothing a later
    pass can read is evidence about the bytes below its seq. A slot cell is different: it
    CARRIES its digest (``_SlotMemo.prefix``) and :func:`_continuable` re-checks it
    through :func:`_prefix_holds` before carrying the cell forward, so custody of the
    already-folded prefix survives from the pass that folded it to this one. Without
    this, a gateway that stays up leaves the savepoint wherever the process first folded,
    and the restart it exists for replays the whole gap.

    *sampled* is the newest unit's mark as read BEFORE the pass, and two of its fields do
    the work. Its ``origin`` is the identity the pass folded under, which the write
    refuses to differ from. Its ``last_seq`` is the boundary the digest in ``memo.prefix``
    was taken at, so a pass that ended somewhere ELSE -- the file grew under it -- matches
    nothing and writes nothing: the digest would then cover fewer bytes than the state was
    folded from, which is the one savepoint no later read recovers from. That costs the
    savepoint and never the read, which is already served.

    THE PREFIX IS CONFIRMED HERE, at the write, and that is what makes one rule cover
    every pass that reaches this function. A digest taken before a pass and never asked
    again would certify bytes a concurrent truncation changed afterwards, and because
    every later resume recomputes the same changed bytes it would match, and the state
    would be served for the life of the unit while disagreeing with a cold fold. The cold
    and warm passes also vouch for their own digest (:func:`_vouched`), which they need
    for their cell; asking again here costs one decode-free walk on a pass that has just
    parsed and folded the same file, and it buys the property that NOTHING writes a
    savepoint without re-reading the bytes its state came from.
    """
    if not memo.units or sampled is None or sampled.origin is None or memo.prefix is None:
        return memo
    if memo.reached != sampled.last_seq:
        return memo
    if memo.weight > slot_fold_cache_bytes():
        # A cell too big to keep WARM is one whose savepoint the store would refuse too:
        # its cap is :data:`~kiro_crew.crew_log.checkpoint.MAX_CHECKPOINT_BYTES`, two
        # orders of magnitude under this ceiling. The refusal is not the reason to skip --
        # the store swallows it and the read is unaffected -- the MEMORY is: the store
        # decides by serializing first, so attempting a ``radar`` cell at its declared
        # caps would build a 2.4 GiB string to measure it and throw it away. And such a
        # cell is not kept warm (:func:`_remember_slot_fold`), so every read of it would
        # arrive here again. The charge is an upper bound that overestimates by a small
        # factor, so this cannot skip a cell whose payload would really have fit.
        return memo
    cell = _slot_cell(name, memo)
    if cell is None:
        return memo
    _state, watermark = cell
    from kiro_crew.crew_log import checkpoint as savepoints

    if not savepoints.write_is_earned(watermark, memo.saved):
        return memo
    if not _prefix_holds(memo.units[-1], memo.prefix):
        return memo
    handle = open_session_log(memo.units[-1])
    if handle is None:
        return memo
    written = savepoints.save_slot(
        handle,
        name,
        slot=slot,
        units=memo.units,
        marks=_identity_marks(memo.marks[:-1]),
        ordinal_base=sum(mark.last_seq for mark in memo.marks[:-1]),
        state=_state,
        watermark=watermark,
        # The digest read before the pass and re-confirmed just above, re-labelled with
        # the boundary it was taken at. It covers at least every record the fold
        # consumed -- a whole-file walk reaches past the newest entry, never short of it
        # -- so it is a witness through ``reached`` and a stricter one than the seq span
        # alone would be.
        prefix=savepoints.PrefixWitness(
            seq=memo.reached, records=memo.prefix.records, sha=memo.prefix.sha
        ),
        expect_origin=sampled.origin,
    )
    if not written:
        return memo
    return replace(memo, saved=watermark)


def _cell_weight(name: str, registry: ProjectionRegistry, store: str) -> int:
    """What the cell *registry* holds for *name* is charged, read off the live cell.

    Counted from the state the kernel actually holds rather than from the checkpoint,
    because the charge has to describe the object that stays resident -- and that is the
    cell, which the checkpoint only borrows a reference to.
    """
    try:
        state, _watermark = registry.cells(store)[name]
    except Exception:  # pragma: no cover - a cell the registry did not build
        return slot_fold_cell_bytes(name)
    return slot_fold_cell_bytes(name, state)


def _vouched(unit_id: str, before: "_PrefixSeen | None") -> "_PrefixSeen | None":
    """*before*, but only if *unit_id* is byte-identical now to what it was then.

    A digest read only AFTER a pass would certify bytes the pass never read: an entry
    rewritten while the fold was running would be hashed together with state folded
    from its earlier value, every later continuation would recompute that same digest,
    match, and keep serving the state. So the digest is taken before the pass and
    confirmed after it, and a file that moved in between vouches for nothing -- which
    costs the next read a cold fold and never a wrong answer.
    """
    if before is None:
        return None
    return before if _unit_prefix(unit_id) == before else None


def _folded_marks(
    units: "tuple[str, ...]",
    sampled: "tuple[_UnitMark, ...]",
    stream: _SlotStream,
    *,
    carried: "tuple[_UnitMark, ...] | None" = None,
) -> "tuple[_UnitMark, ...]":
    """*sampled*, with each streamed unit's height replaced by what was FOLDED.

    This is where :func:`fold_slot_warm`'s invariant is applied: the sampled heights are
    that read's plan, and what a memo remembers is what its pass consumed.

    The FINGERPRINT is carried across as sampled, and deliberately not re-stat'ed after
    the pass. A fingerprint taken afterwards would cover bytes this pass may not have
    folded -- an entry appended between the last read and that stat -- and the next read
    would then compare equal and serve a cell that is missing it. Sampled, the error can
    only fall the other way: a slot whose bytes moved mid-pass fails the comparison, and
    the next read folds cold, which costs time and not correctness.

    *carried* is a warm continuation's earlier marks, which this pass did not read and
    has already found unchanged; only the newest unit is re-stated from the stream.
    """
    base = list(carried if carried is not None else sampled)
    return tuple(
        _UnitMark(
            sampled[index].origin,
            stream.heights.get(unit, base[index].last_seq),
            sampled[index].size,
            sampled[index].mtime_ns,
        )
        for index, unit in enumerate(units)
    )


def _slot_checkpoint(name: str, memo: _SlotMemo) -> "tuple[Checkpoint, int]":
    """*memo*'s kernel cell as the checkpoint this module's callers carry, and its revision.

    ``last_seq`` is the newest unit's OWN seq, not the kernel's ordinal, and that is
    the contract rather than an implementation detail: a writer advances this
    checkpoint over the entry it is appending to answer with the record that entry
    produces, and that entry's seq comes from its file. An ordinal here would sit far
    above it and :func:`advance` would refuse the entry as already folded.

    The state comes back AS THE CELL HOLDS IT, not copied, which is what the folds'
    own discipline makes safe: ``apply`` returns a new object rather than editing this
    one, so a later read replaces the cell's state instead of moving what a caller
    kept, and the callers that continue this checkpoint go through :func:`advance`,
    which copies before its first step.
    """
    state, _watermark = memo.registry.cells(memo.store)[name]
    return Checkpoint(name=name, last_seq=max(memo.reached, 0), state=state), memo.revision


# --------------------------------------------------------------------------- #
# A session's folds, kept warm
# --------------------------------------------------------------------------- #
#
# The session-keyed half of eager folding. The eager folder (:mod:`.eager`) wakes on
# every committed entry, and for the entry's own unit it calls :func:`fold_session_warm`,
# which continues that unit's bundle from where the last call left it. The route that
# serves the panel calls the same function, so a read after a wake is a memo lookup.
#
# THE SAME DISCIPLINE AS THE SLOT MEMO, in each of its three parts. A cell is continued
# only from a bundle :func:`fold_session` itself accepts as ``since=`` (same file, same
# origin, not ahead of the log), so the memo cannot hold a value a cold fold would not
# reach. A revision is minted from the one process-wide counter the slot memo uses
# (:func:`_minted_revision`), so it is monotonic per (session, fold) across a unit
# recreated under the same id and across a resume from disk. And the table is bounded in
# BYTES, least recently advanced first.
#
# THE SAVEPOINT IS STILL BROUGHT FORWARD. ``fold_session`` writes nothing on a read that
# reused ``since=``, because it cannot vouch for the prefix that bundle came from. A memo
# that always passed ``since=`` would therefore leave the disk savepoint where the first
# fold put it for the life of the process, and a restart would replay the whole gap. So a
# pass whose bundle has moved far enough past its savepoint to earn a write
# (``checkpoint.write_is_earned``) folds from the DISK savepoint instead, which replays at
# most that many entries and writes the savepoint back.

#: Default ceiling on the bytes every warm SESSION cell may hold together. 64 MiB.
#:
#: Charged by each fold's SERIALIZED size, unlike the slot table, and that difference is
#: affordable here for a reason the slot table does not have: a session fold's state is
#: flat and capped near a megabyte (see the measured figures in
#: ``docs/system-specs/modules/crew-log-projection.md``), and the fold already COPIES that
#: state for every entry that moves it (``copy_state``). Weighing a CHANGED fold once per
#: pass is the same order of work as one of those copies, so it buys an exact charge for no
#: new cost class. A slot state can be 2.4 GiB, where the same step would cost more than
#: the fold.
DEFAULT_SESSION_FOLD_CACHE_BYTES: Final[int] = 64 * 1024 * 1024

#: The variable an operator lowers the session ceiling with, in BYTES; same rules as
#: :data:`SLOT_FOLD_CACHE_BYTES_ENV`.
SESSION_FOLD_CACHE_BYTES_ENV: Final[str] = "KIROCREW_SESSION_FOLD_CACHE_BYTES"


def session_fold_cache_bytes() -> int:
    """The live ceiling on all warm session cells together, in bytes."""
    raw = os.environ.get(SESSION_FOLD_CACHE_BYTES_ENV, "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value > 0:
            return value
    return DEFAULT_SESSION_FOLD_CACHE_BYTES


def session_fold_state_bytes(state: Mapping[str, Any]) -> int:
    """What one session fold's *state* weighs: its UTF-8 JSON length.

    The same measure every cell figure in this module is quoted in, so the ceiling and
    the numbers beside it are in one unit. A state that will not serialize is charged
    :data:`_UNMEASURED_ROW_BYTES` rather than raising: the pass storing it must not fail
    over a charge.
    """
    try:
        return len(json.dumps(state, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):  # pragma: no cover - every fold state is JSON
        return _UNMEASURED_ROW_BYTES


class _SessionMemo(NamedTuple):
    """One session's warm bundle, its revision per fold, and what each fold weighs."""

    bundle: SessionProjections
    revisions: Mapping[str, int]
    weights: Mapping[str, int]

    @property
    def weight(self) -> int:
        return sum(self.weights.values())


class WarmSession(NamedTuple):
    """What :func:`fold_session_warm` answers.

    ``changed`` names the folds whose revision this pass MINTED, in registry order --
    the ones whose value differs from the last one this process handed out. It is what
    the eager folder publishes, and why an entry that moved only ``status`` costs one
    event rather than seven.
    """

    bundle: SessionProjections
    revisions: Mapping[str, int]
    changed: tuple[str, ...]

    def projection(self, name: str) -> Projection:
        """One fold of the bundle, rendered, carrying its revision."""
        return projection_of(
            self.bundle.checkpoints[require_name(name)], revision=self.revisions.get(name, 0)
        )


_session_memos: "dict[tuple[str, str], _SessionMemo]" = {}
_session_memo_guard = threading.Lock()

#: The fold-lock family a session memo is serialized under. A NUL cannot appear in a
#: registered fold name, so a session key can never share a lock with a slot key.
_SESSION_LOCK_TAG: Final[str] = "\0session"


def fold_session_warm(session_id: str) -> WarmSession:
    """Every eager SESSION fold for *session_id*, continued from this process's memo.

    One call folds them all, because they share one pass over one file -- the property
    :func:`fold_session` exists for -- and the route that serves the panel needs them
    together from the same moment.

    Serialized per session under the fold-lock table the slot memo uses, so the eager
    worker and a route cannot interleave a read of the memo with another pass's store.
    """
    home = str(data_home())
    key = (home, session_id)
    lock_key = (home, session_id, _SESSION_LOCK_TAG)
    entry = _acquire_fold_lock(lock_key)
    try:
        with entry.lock:
            return _fold_session_warm_locked(session_id, key, EAGER_SESSION_FOLD_NAMES)
    finally:
        _release_fold_lock(lock_key, entry)


def _fold_session_warm_locked(
    session_id: str, key: "tuple[str, str]", names: "tuple[str, ...]"
) -> WarmSession:
    """:func:`fold_session_warm`'s body, with this session's lock already held."""
    from kiro_crew.crew_log import checkpoint as savepoints

    with _session_memo_guard:
        held = _session_memos.get(key)
    since = held.bundle if held is not None else None
    if since is not None and savepoints.write_is_earned(since.last_seq, since.saved_seq):
        # Far enough past the disk savepoint to owe it a write, and only a pass that
        # folds the prefix itself may write one. See this section's header.
        since = None
    bundle = fold_session(session_id, names, since=since)
    # A different file, or one whose identity could not be held still: nothing the memo
    # vouched for describes it, so every non-empty fold is new.
    rebuilt = held is None or bundle.origin is None or held.bundle.origin != bundle.origin
    revisions: dict[str, int] = {}
    weights: dict[str, int] = {}
    changed: list[str] = []
    for name in names:
        checkpoint = bundle.checkpoints[name]
        before = None if rebuilt or held is None else held.bundle.checkpoints.get(name)
        previous = 0 if rebuilt or held is None else held.revisions.get(name, 0)
        # IDENTITY FIRST, equality only when it fails. A fold the pass did not move hands
        # back the very object it was given, which is the common case and costs nothing;
        # a pass that resumed from disk hands back an equal object that is not the same
        # one, and minting for it would push a value nothing changed.
        same = before is not None and (
            before.state is checkpoint.state or before.state == checkpoint.state
        )
        if same and previous > 0 and held is not None:
            revisions[name] = previous
            weights[name] = held.weights.get(name, 0)
            continue
        weights[name] = session_fold_state_bytes(checkpoint.state)
        if checkpoint.last_seq <= 0:
            # Nothing folded: an empty value is not worth an order, and 0 tells a
            # consumer there is none.
            revisions[name] = 0
            continue
        revisions[name] = _minted_revision()
        changed.append(name)
    memo = _SessionMemo(bundle=bundle, revisions=revisions, weights=weights)
    if bundle.origin is not None:
        _remember_session_fold(key, memo)
    else:
        # Served, never kept: an identity that could not be held still is one a later
        # pass must not continue from.
        with _session_memo_guard:
            _session_memos.pop(key, None)
    return WarmSession(bundle=bundle, revisions=revisions, changed=tuple(changed))


def _remember_session_fold(key: "tuple[str, str]", memo: _SessionMemo) -> None:
    """Store *memo*, evicting least recently advanced cells until the table fits.

    The rules of :func:`_remember_slot_fold`, for the same reasons: a cell above the
    whole ceiling is not stored (a cold fold costs time, never an answer), and insertion
    order is advance order because a cell is never edited in place.
    """
    ceiling = session_fold_cache_bytes()
    with _session_memo_guard:
        _session_memos.pop(key, None)
        if memo.weight > ceiling:
            logger.debug(
                "crew log session %s weighs %d bytes, above the %d-byte ceiling (%s), so "
                "it is not kept warm",
                key[1],
                memo.weight,
                ceiling,
                SESSION_FOLD_CACHE_BYTES_ENV,
            )
            return
        _session_memos[key] = memo
        held = sum(cell.weight for cell in _session_memos.values())
        while held > ceiling and len(_session_memos) > 1:
            oldest, evicted = next(iter(_session_memos.items()))
            _session_memos.pop(oldest)
            held -= evicted.weight


def forget_session_folds(session_id: str = "") -> None:
    """Drop *session_id*'s warm cell in every home, or every cell when it is empty.

    What a ``session/closed`` does once the closing entry has been folded and pushed:
    the unit will not append again, its savepoint holds the state, and a warm cell would
    be bytes held for a reader that already has the value.
    """
    with _session_memo_guard:
        if not session_id:
            _session_memos.clear()
            return
        for key in [key for key in _session_memos if key[1] == session_id]:
            _session_memos.pop(key, None)


def session_fold_memo_bytes() -> int:
    """What every warm session cell held right now weighs, together."""
    with _session_memo_guard:
        return sum(cell.weight for cell in _session_memos.values())


def slot_of_session(session_id: str) -> str:
    """The slot *session_id*'s crew log belongs to, or ``""`` when unprovable.

    The bridge a SESSION-addressed caller needs to reach a slot-keyed fold. The
    header is the answer rather than a session mapping: it is written once inside
    the fenced tree and never rewritten, so it cannot be made to name another
    conversation's slot by anything that can write the mapping file.
    """
    from kiro_crew.crew_log.store import unit_header_slot

    return unit_header_slot(KIND_SESSION, session_id) or ""


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


#: Item records the fold retains per board -- the fold's own memory bound, so a
#: log that carries more still folds to a value of bounded size. Two bounds meet
#: here. The WRITER caps a board's OPEN items at
#: ``work_ledger.MAX_ITEMS_PER_CONDUCTOR`` (32, live fan-out) and the CREATES it
#: admits over the board's life, open and closed together, at
#: ``work_ledger.MAX_STORED_ITEMS_PER_CONDUCTOR`` -- which is THIS number, both read
#: off ``work_vocab.WORK_STORED_ITEM_LIMIT``. The writer counts those creates in a
#: monotonic counter in the board's header, the way this fold counts them in an
#: append-only log, so a record removed from the writer's cache reopens nothing:
#: every create the writer admits is one recorded create, a board the writer admits
#: cannot overflow the fold, the fold holds the whole board and ``omitted`` stays 0.
#: The fold still holds at most ``WORK_ITEM_LIMIT`` items and counts every create
#: past that in ``omitted``, and ``work_ledger.rebuild_from_projection`` still
#: refuses a full fold that reports omissions -- only such a fold is necessarily a
#: prefix of the board rather than the board -- but with the two bounds equal that
#: guard is defensive, not the working path.
WORK_ITEM_LIMIT: Final[int] = WORK_STORED_ITEM_LIMIT

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
        "summary": "",
        "artifacts": {},
        "pr": None,
        "last_report_at": None,
        "created_at": _work_iso(stamp_ms),
        "closed_at": None,
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
        return
    item = state["items"].get(item_id)
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
        for name in ("last_report_at", "closed_at"):
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
    _work_apply(item, data, entry.time)


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
        state["round"] = _as_int(data.get("round"))
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
        state["round"] = _as_int(data.get("board_round"))
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
        rendered = {key: value for key, value in item.items() if key != "events"}
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
    }


# workstreams -- every board this slot reaches, each task costed by its own worker
# --------------------------------------------------------------------------- #
#
# WHY A SECOND FOLD OVER THE SAME ENTRIES. ``work`` answers for ONE board: it binds
# to the slot, drops every entry naming another, and resets itself when a new
# generation appears under that slot. A project report asks a wider question -- what
# is this crewmate running, across every board its own units and its workers' units
# reach -- and it asks what each task COST, which no work entry carries. Neither half
# can come from ``work``: the board it drops is a workstream the report must show, and
# the spend lives in the ``usage`` entries of the bound worker's own log.
#
# So this fold groups by BOARD, keyed by ``(slot, generation)``, and joins each task
# to the spend of the worker session bound to it. It reuses what is already here
# rather than restating it: the same work entry type and the same conductor/worker
# field tables as ``work``, and the same ``_bill_credits`` screen (via
# :data:`_CREDIT_SOURCE_OF` and :func:`_credit_charge`) the ``usage`` fold bills with.
#
# HOW A CREDIT ENTRY FINDS ITS WORKER. An ``Entry`` carries no slot, so the fold reads
# the one entry that marks where a unit begins and names its session's slot --
# ``session/opened`` -- and attributes every charge after it to that slot. That is the
# same marker ``usage`` keeps in :data:`USAGE_TYPES` for the same reason. A charge in a
# unit whose opener this fold never saw is attributed to nothing and counted in
# ``spend_unattributed``, never silently to the board: an unattributable charge is a
# gap in the report, and reporting it as a task's cost would be an invention.
#
# COST IS NEVER ZERO BY DEFAULT. A task whose worker reported no charge renders
# ``credits: 0.0`` WITH ``credits_reported: false``, which is the ``usage`` fold's own
# posture (``reported`` beside each bucket): an unmetered provider writes no
# ``credits`` key, and folding that in as a measurement nobody made is the one error
# a spend report cannot be corrected out of.

#: Boards one fold retains, newest activity first. A crewmate reads its own
#: workstreams, and a page that lists more than this is not read -- so the cap is the
#: page's, and every board past it is counted in ``omitted`` rather than dropped
#: silently.
WORKSTREAMS_BOARD_LIMIT: Final[int] = 12

#: Tasks retained per board. Counted per board in ``tasks_omitted``, so a board that
#: ran more says so instead of reading as complete at the cap.
WORKSTREAMS_TASK_LIMIT: Final[int] = 40

#: Hourly buckets kept per spender and per board: two days, which is the window the
#: report's bar chart draws. The NEWEST are kept; older hours fall off the front.
WORKSTREAMS_SERIES_LIMIT: Final[int] = 48

#: Spender slots one fold retains. A board's workers are bound by its conductor, so
#: this is bounded in practice by the boards x their items; the cap is what keeps a
#: slot whose units name an unbounded number of sessions from growing the state.
WORKSTREAMS_SPENDER_LIMIT: Final[int] = WORKSTREAMS_BOARD_LIMIT * WORKSTREAMS_TASK_LIMIT

#: Units one read of this fold may collect, across every round of the bind walk. The
#: ceiling that makes the walk a bounded read rather than one that grows with the bind
#: graph, and the walk's ONLY bound: the fold shows 12 boards of 40 tasks, so a slot
#: reaching more units than this has more history than the page can draw either way.
WORKSTREAMS_UNIT_LIMIT: Final[int] = 512

#: Work events one TASK's drawer shows, newest first. The drawer answers "what is
#: this, and is the claim true" with the item's own narrative, and twenty lines is
#: where a reader stops scrolling and starts asking the crewmate instead.
WORKSTREAMS_EVENT_LIMIT: Final[int] = 20

#: Events the WHOLE fold carries, across every board and task.
#:
#: The per-task cap alone does not bound this: 12 boards of 40 tasks at 20 events
#: each is 9,600 lines, which is neither a state worth checkpointing nor a page a
#: frame will mint. So the fold keeps ONE newest-first ring across every task and
#: spends it globally -- a busy board's recent work crowds out an idle board's older
#: work, which is the right way round for a reader opening a drawer.
#:
#: A task whose lines fell out of the ring is not silently empty: it carries
#: ``events_seen``, so the drawer says "20 of 34" or "no events recorded" and never
#: implies a task did nothing when the truth is that the fold stopped carrying it.
WORKSTREAMS_EVENT_BUDGET: Final[int] = 400

#: Characters of the conductor's ``decision`` a task row carries.
#:
#: The store takes 2000 and the ``work`` fold returns all of it. This fold cuts it,
#: because here it is one of 480 possible rows rather than one board's own field, and
#: the drawer shows the ruling's substance in its first lines. A reader who needs the
#: whole text has the conductor's own board for it.
WORKSTREAMS_DECISION_LIMIT: Final[int] = 400

# The entry types this fold reads are :data:`WORKSTREAMS_TYPES`, declared beside the
# ``usage`` fold's billing table because they are spelled FROM it.


def _workstreams_hour(stamp_ms: int) -> str:
    """An entry's epoch-millisecond ``time`` as the UTC hour it fell in.

    UTC, and not the local spelling ``_work_iso`` renders: an hourly bucket is a KEY,
    two buckets are compared by it, and a local offset that shrinks at an autumn DST
    change spells a later hour as an earlier string -- so the chart's bars would swap
    places for one hour a year. The report draws a bar per hour and labels it from
    this key, so the key has to be ordered by the time it names.
    """
    try:
        moment = datetime.fromtimestamp(stamp_ms / 1000, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return ""
    return moment.strftime("%Y-%m-%dT%H:00Z")


def _workstreams_bump(series: dict[str, Any], hour: str, key: str, amount: float) -> None:
    """Add *amount* to *hour*'s *key* bucket, keeping the newest hours only.

    The trim is by KEY order, which :func:`_workstreams_hour` makes a time order, so a
    late-arriving entry for an hour already evicted does not reopen it at the cost of
    the newest one.
    """
    if not hour:
        return
    bucket = series.get(hour)
    if bucket is None:
        if len(series) >= WORKSTREAMS_SERIES_LIMIT and hour < min(series):
            # Older than every hour kept and the window is full: the chart does not
            # draw it, and admitting it would evict an hour the chart does draw.
            return
        bucket = series[hour] = {"credits": 0.0, "accepted": 0}
        for stale in sorted(series)[: max(0, len(series) - WORKSTREAMS_SERIES_LIMIT)]:
            del series[stale]
        bucket = series.setdefault(hour, {"credits": 0.0, "accepted": 0})
    grown = bucket[key] + amount
    if isinstance(grown, float) and not math.isfinite(grown):
        return
    bucket[key] = grown


def _workstreams_start() -> dict[str, Any]:
    return {
        # The board this fold was asked about, which is the slot whose units lead the
        # fold. Kept so a render can say which of the boards is the crewmate's own.
        "slot": "",
        # The session slot of the unit being folded, taken off its ``session/opened``.
        # This is what a charge is attributed to; empty means no opener has been seen
        # in this unit yet, and a charge then lands in ``spend_unattributed``.
        "cursor": "",
        "boards": {},
        "order": [],
        "omitted": 0,
        # Session units this fold could not read, because the ceiling on what it
        # retains was reached. Seeded by the bind hook from the selection that chose
        # them; 0 means the record's whole reachable history is in this answer.
        "units_omitted": 0,
        # Per spender: what that session spent, how many charges the total covers, how
        # long its turns ran, and its own hourly buckets. Joined to a task at RENDER by
        # the task's ``worker_session_key`` rather than at step time, because a bind
        # can be folded after the charge it explains (the conductor's units lead, but a
        # board recorded from a worker's own baseline has no conductor unit at all).
        "spend": {},
        "spenders_omitted": 0,
        # Charges in a unit whose opener was never folded. A gap the report states
        # rather than a cost it assigns to a task that did not incur it.
        "spend_unattributed": 0.0,
        "spend_unattributed_reported": 0,
        # THE NEWEST ENTRY THIS FOLD APPLIED, which is how the page tells "nothing is
        # happening" from "nothing is reaching me". A gateway that died mid-run leaves
        # every number on the page intact and correct as of a time the page cannot
        # otherwise name, so without this the report reads as current forever.
        #
        # Across every entry kind the fold steps, not only work records: a charge
        # arriving with no work entry beside it is still the log advancing, and
        # treating that as silence would raise the band while spend was still moving.
        "last_entry_ms": 0,
        "last_entry_at": "",
        # ONE newest-last ring of work events across every board and task, which is
        # what a task drawer reads its own narrative from. Global rather than per
        # task because the per-task cap alone does not bound the fold -- see
        # `WORKSTREAMS_EVENT_BUDGET` for why, and for what a task whose lines fell
        # out of it says instead of nothing.
        "events": [],
    }


def _workstreams_bind_slot(state: dict[str, Any], slot: str) -> None:
    """The board this fold was asked about, as the reader names it.

    The unit-overflow count is collected here too, because this is the one hook that
    runs between choosing the units and rendering them. A fold cannot count what it
    never saw: a unit refused for want of room contributes no entry, so without this
    the snapshot would report a short answer as a whole one.
    """
    state["slot"] = slot
    state["units_omitted"] = _workstreams_units_dropped(slot)


def _workstreams_new_board(board_id: str, slot: str, generation: str) -> dict[str, Any]:
    return {
        "id": board_id,
        "slot": slot,
        "generation": generation,
        "goal": "",
        "round": 0,
        "items": {},
        "order": [],
        "tasks_omitted": 0,
        "last_activity_ms": 0,
        "last_activity_at": "",
        "series": {},
    }


def _workstreams_new_item(item_id: str) -> dict[str, Any]:
    """One task row's state, and THE declaration of what this fold keeps per item.

    The step copies a field only when the item already has a key for it, so this dict
    is the filter: the event tail, the acceptance, the artifacts map and the
    conductor's ``decision`` are absent because the report draws one line per task and
    the ``work`` fold already serves the board in full. ``decision`` in particular is
    2000 clamped characters per item that nothing on this page renders.
    """
    return {
        "item_id": item_id,
        "title": "",
        "state": "open",
        "verdict": None,
        "worker_session_key": None,
        "round": 0,
        "status": None,
        "summary": "",
        "pr": None,
        # WHEN THIS TASK LAST PRODUCED ANYTHING, so a reader can tell a worker that is
        # thinking from one that is dead -- the question the row's duration cannot
        # answer, because duration is work time and reads the same either way.
        #
        # Written from the entry's own time on the actions where the WORKER is the one
        # speaking, and deliberately NOT on a conductor action: a decide or a verdict
        # is the conductor moving, and counting it would reset the silence of exactly
        # the worker whose silence is being asked about.
        #
        # The millisecond stamp is kept beside its ISO rendering for the reason the
        # board's pair is: the comparison is numeric, and an ISO string is empty for a
        # stamp no ``datetime`` can hold.
        "last_report_ms": 0,
        "last_report_at": "",
        # WHEN THIS TASK OPENED AND WHEN IT CLOSED -- the two ends of its span, which
        # `last_report_at` and `duration_ms` together cannot give. Duration is the
        # WORKER SESSION's measured work time, so a session bound to three tasks
        # reports one figure for all three and nothing that places any of them on a
        # clock; `last_report_at` is one instant. A per-worker timeline needs a start
        # and an end per task, which is what these two are -- and they are spelled
        # exactly as the ``work`` fold spells its own, so a page that reads one fold's
        # span reads the other's with the same helper rather than a second vocabulary.
        #
        # ISO, not the millisecond pair ``last_report_at`` keeps beside it, because
        # nothing here COMPARES them: the opener is written once and the closer is
        # written from the item's own state, so neither needs the numeric guard a
        # monotonic watermark does. ``_workstreams_item_span`` is the only writer.
        "created_at": "",
        "closed_at": "",
        # The conductor's latest ruling on this item, clamped -- see
        # `WORKSTREAMS_DECISION_LIMIT`. The drawer shows it because "what did the
        # conductor decide" is the question a reader opens one task to ask.
        "decision": "",
        # How many events this task has produced, ALL of them, whether or not the
        # fold is still carrying their text. The count and the carried tail are
        # separate facts: a drawer reading only the tail cannot tell a task that did
        # nothing from one whose lines fell out of the global ring, and saying "no
        # events recorded" about the second is a lie about the work.
        "events_seen": 0,
        # The hour this item was ACCEPTED, so the chart's accepted line is drawn from
        # the item's own transition rather than recomputed from a board total. Kept on
        # the item (not added straight into the series) because a later close can move
        # an item out of ``accepted`` again, and a counter already added cannot be
        # moved back -- see ``_workstreams_item_state``.
        "accepted_hour": "",
    }


def _workstreams_board_of(state: dict[str, Any], entry: Entry) -> dict[str, Any] | None:
    """The board *entry* belongs to, minting it on first sight, or ``None`` when the
    fold is already at its board cap.

    Keyed by ``(slot, generation)``, because that pair is what names ONE board: a slot
    reused after an earlier board was purged carries both boards' entries forever (the
    log is append-only), and the generation is the id minted with each board's record.
    A board from before the stamp existed has no generation and keys on its slot alone,
    which is the one board such a slot can have.
    """
    data = entry.data
    slot = _as_str(data.get("slot"))
    if not slot:
        return None
    generation = _as_str(data.get("generation"))
    board_id = f"{slot}#{generation}" if generation else slot
    board = state["boards"].get(board_id)
    if board is None:
        if len(state["boards"]) >= WORKSTREAMS_BOARD_LIMIT:
            state["omitted"] += 1
            return None
        board = state["boards"][board_id] = _workstreams_new_board(board_id, slot, generation)
        state["order"].append(board_id)
    if entry.time >= board["last_activity_ms"]:
        board["last_activity_ms"] = entry.time
        board["last_activity_at"] = _work_iso(entry.time)
    return board


def _workstreams_event(
    state: dict[str, Any], board_id: str, item: dict[str, Any], entry: Entry
) -> None:
    """Record one work event into the fold's global ring, if it is one.

    An entry with no ``event_kind`` is a field write and not a line of narrative, so
    it is counted nowhere: the work vocabulary makes the kind the thing that says
    "this is an event", and the ``work`` fold reads it the same way.

    The count on the item moves whether or not the text is carried, which is what
    lets a drawer distinguish a task that did nothing from one the ring has moved
    past. Oldest lines fall off the front, because the question a drawer asks is
    about now.
    """
    kind = _as_str(entry.data.get("event_kind"))
    if not kind:
        return
    item["events_seen"] = _as_int(item.get("events_seen")) + 1
    events: list[dict[str, Any]] = state["events"]
    status = entry.data.get("status")
    events.append(
        {
            "board": board_id,
            "item_id": item["item_id"],
            "at": _work_iso(entry.time),
            "kind": kind[:TEXT_LIMIT],
            "status": _as_str(status)[:TEXT_LIMIT] if isinstance(status, str) else None,
            # Cut to `TEXT_LIMIT` by `_as_str` itself, which is the cut this field
            # wants and the reason there is no second limit beside it: a drawer line
            # is read at a glance, and the full text is in the `work` fold for
            # anyone who needs it. A constant here would add a number that never
            # fires, because `_as_str` has already cut below it.
            "text": _as_str(entry.data.get("event")),
        }
    )
    if len(events) > WORKSTREAMS_EVENT_BUDGET:
        del events[: len(events) - WORKSTREAMS_EVENT_BUDGET]


def _workstreams_item_state(
    board: dict[str, Any], item: dict[str, Any], new: str, hour: str
) -> None:
    """Move *item* to state *new*, keeping the board's accepted-per-hour line honest.

    An item can be accepted and then closed again under another state, so the line is
    maintained as a DIFFERENCE: the hour an item was accepted in is remembered on the
    item, and a move out of ``accepted`` takes its count back off that same hour. A
    counter incremented on the way in and never on the way out would leave the chart
    claiming an acceptance the board does not hold.
    """
    was = item["state"]
    item["state"] = new
    if was == new:
        return
    if new == "accepted":
        item["accepted_hour"] = hour
        _workstreams_bump(board["series"], hour, "accepted", 1)
    elif was == "accepted":
        _workstreams_bump(board["series"], item["accepted_hour"], "accepted", -1)
        item["accepted_hour"] = ""


def _workstreams_span_opened(item: dict[str, Any], data: Mapping[str, Any], stamp_ms: int) -> None:
    """Stamp *item*'s ``created_at`` if it has none yet. Idempotent after that.

    Written ONCE, by whichever entry reaches the item first -- which is not always a
    ``create``. This fold mints an item from any action that names it (see
    :func:`_workstreams_work`), so a board rebuilt from a baseline, or a worker report
    folded ahead of its conductor's unit, would otherwise have no opener at all and
    its row could not be placed on a clock.

    A committed stamp the entry carries wins over the append time, the way
    :func:`_work_stamp` already has it, so a span survives a rebuild unchanged.
    """
    if not item["created_at"]:
        item["created_at"] = _work_stamp(data.get("created_at"), stamp_ms)


def _workstreams_span_closed(item: dict[str, Any], data: Mapping[str, Any], stamp_ms: int) -> None:
    """Keep *item*'s ``closed_at`` in step with the state it now holds.

    Read off the item's own ``state`` AFTER the entry has been applied, rather than
    from the ``close`` action, and that is what makes it ordering-tolerant: the state
    is already guarded by :func:`_workstreams_item_state`, so this follows it instead
    of keeping a second opinion about which action closes an item.

    AND IT IS CLEARED WHEN AN ITEM REOPENS. An item can be closed and then moved back
    to ``open`` -- the same transition ``_workstreams_item_state`` takes an accepted
    count back off an hour for -- and a close stamp left standing would draw a bar
    that ended, for a task that is still running.

    Called on the CONDUCTOR path only, because only a conductor entry can be the one
    that closed an item. A terminal item this fold never saw a conductor entry for
    keeps an empty ``closed_at``, which is the fold's own posture everywhere else:
    stamping it with whatever entry happened to arrive next would state a close time
    the record never claimed, and a page reading it would draw a bar ending at a
    moment nothing happened.
    """
    if item["state"] == "open":
        item["closed_at"] = ""
    elif not item["closed_at"]:
        item["closed_at"] = _work_stamp(data.get("closed_at"), stamp_ms)


def _workstreams_work(state: dict[str, Any], entry: Entry) -> None:
    """Apply one work record to the board it names."""
    data = entry.data
    board = _workstreams_board_of(state, entry)
    if board is None:
        return
    action = _as_str(data.get("action"))
    hour = _workstreams_hour(entry.time)
    if action == "goal":
        board["goal"] = _work_text("goal", data.get("goal"))
        board["round"] = max(board["round"], _as_int(data.get("round")))
        return
    item_id = _as_id(data.get("item_id"))
    if not item_id:
        state["omitted"] += 1
        return
    item = board["items"].get(item_id)
    if item is None:
        if len(board["items"]) >= WORKSTREAMS_TASK_LIMIT:
            board["tasks_omitted"] += 1
            return
        # Minted by whichever action reaches the item first, not only by ``create``.
        # The units are folded conductor-first, but a worker's report can still be the
        # first record of an item this fold sees -- a board rebuilt from a baseline, or
        # a conductor unit pruned -- and parking the entry the way ``work`` does would
        # buy a report ordering this report-per-task row does not need.
        item = board["items"][item_id] = _workstreams_new_item(item_id)
        board["order"].append(item_id)
        if data.get("baseline") is True:
            # A BASELINE CARRIES THE WHOLE COMMITTED ITEM, so it seeds every field
            # this fold keeps before the action delta below narrows to that action's
            # own. Without this a task first seen through its close arrives with the
            # close fields and nothing else: no title, no verdict, no worker -- a row
            # the reader sees as an untitled task that was never worked, when the
            # entry it was built from named all three.
            for name in _WORK_BASELINE_FIELDS:
                if name in data and name in item:
                    item[name] = _work_field(name, data[name])
            # The worker's own stamp rides along for the same reason the fields do:
            # a baselined task that reported before the rebuild is not silent.
            stamp = data.get("last_report_at")
            if isinstance(stamp, str) and stamp:
                item["last_report_at"] = _work_stamp(stamp, entry.time)
    _workstreams_event(state, board["id"], item, entry)
    # The span's open end, before either party's field copy: it depends on no field
    # and must be set by whichever entry got here first, whoever wrote it.
    _workstreams_span_opened(item, data, entry.time)
    if data.get("actor") == "worker" or action == "report":
        # THE WORKER SPOKE, so this is the stamp the row's silence is measured from.
        # Set before the field copy and unconditionally: a report that carries no
        # field this fold keeps is still the worker producing something, and reading
        # it as silence would call a working worker dead.
        if entry.time >= item["last_report_ms"]:
            item["last_report_ms"] = entry.time
            item["last_report_at"] = _work_iso(entry.time)
        for name in _WORK_WORKER_FIELDS:
            if name in data and name in item:
                item[name] = _work_field(name, data[name])
        return
    fields = _WORK_CONDUCTOR_FIELDS.get(action, ())
    for name in fields:
        if name not in data or name not in item:
            continue
        if name == "state":
            _workstreams_item_state(board, item, _work_field("state", data[name]), hour)
            continue
        if name == "decision":
            # CUT HERE, not at the store's 2000: `_work_field` returns a decision
            # whole because the `work` fold serves one board, and this one serves up
            # to 480 rows inside a page. See `WORKSTREAMS_DECISION_LIMIT`.
            item[name] = _work_text(name, data[name])[:WORKSTREAMS_DECISION_LIMIT]
            continue
        item[name] = _work_field(name, data[name])
    # AFTER the field copy, so the state this reads is the one this entry set.
    _workstreams_span_closed(item, data, entry.time)
    if "round" in data:
        board["round"] = max(board["round"], _as_int(data.get("round")))


def _workstreams_spend(state: dict[str, Any], entry: Entry) -> None:
    """Attribute one closer's charge and duration to the session that incurred it."""
    data = entry.data
    billed = _credit_charge(data.get("credits"))
    # ``duration_ms`` is the turn closer's spelling and ``ms`` the subagent and
    # background closers'; reading only one keeps half the log's durations out.
    measured = _as_int(data.get("duration_ms")) or _as_int(data.get("ms"))
    if billed is None and not measured:
        return
    slot = state["cursor"]
    if not slot:
        # No opener folded in this unit, so there is nothing this charge can be
        # attributed to. Counted, never assigned.
        if billed is not None:
            grown = state["spend_unattributed"] + billed
            if math.isfinite(grown) and grown >= state["spend_unattributed"]:
                state["spend_unattributed"] = grown
                state["spend_unattributed_reported"] += 1
        return
    row = state["spend"].get(slot)
    if row is None:
        if len(state["spend"]) >= WORKSTREAMS_SPENDER_LIMIT:
            state["spenders_omitted"] += 1
            return
        row = state["spend"][slot] = {
            "credits": 0.0,
            "reported": 0,
            "duration_ms": 0,
            "series": {},
        }
    if measured:
        row["duration_ms"] += measured
    if billed is None:
        return
    # The same invariant ``_bill_credits`` keeps, for the same reason: a spend total
    # stays finite and never goes down, and a broken one survives in the savepoint.
    grown = row["credits"] + billed
    if not math.isfinite(grown) or grown < row["credits"]:
        return
    row["credits"] = grown
    row["reported"] += 1
    _workstreams_bump(row["series"], _workstreams_hour(entry.time), "credits", billed)


def _workstreams_advanced(state: dict[str, Any], entry: Entry) -> None:
    """Remember that the fold applied *entry*, for the page's own staleness.

    Guarded on ``>=`` like the board's stamp: a unit folded out of order must not
    pull the watermark backwards and make a current page claim it is old.
    """
    if entry.time >= state["last_entry_ms"]:
        state["last_entry_ms"] = entry.time
        state["last_entry_at"] = _work_iso(entry.time)


def _workstreams_step(state: dict[str, Any], entry: Entry) -> None:
    if entry.type == "session/opened":
        # Where a unit begins, and the only entry that names its session's slot. Every
        # charge after this line is that session's until the next opener.
        state["cursor"] = _as_str(entry.data.get("slot"))[:TEXT_LIMIT]
        return
    if entry.type == WORK_ENTRY_TYPE:
        _workstreams_advanced(state, entry)
        _workstreams_work(state, entry)
        return
    if entry.type in _CREDIT_SOURCE_OF:
        _workstreams_advanced(state, entry)
        _workstreams_spend(state, entry)


def _workstreams_copy(state: dict[str, Any]) -> dict[str, Any]:
    """A copy of every container ``step`` can reach, and no deeper.

    The kernel requires a new object on a real change and the same one on none, so the
    step edits a copy. Spelled out rather than left to the deep-copy fallback because
    this state nests three levels (board -> item, board -> series, spender -> series)
    and a deep copy of all of it is paid on every entry the fold reads -- which, with
    three closers in ``affects``, is most of a busy worker's log.
    """
    boards = {}
    for board_id, board in state["boards"].items():
        boards[board_id] = {
            **board,
            "items": {item_id: dict(item) for item_id, item in board["items"].items()},
            "order": list(board["order"]),
            "series": {hour: dict(bucket) for hour, bucket in board["series"].items()},
        }
    return {
        **state,
        "boards": boards,
        "order": list(state["order"]),
        # The ring itself is copied; its rows are not, because nothing mutates a row
        # after it is appended -- an event is a line of record, written once.
        "events": list(state["events"]),
        "spend": {
            slot: {
                **row,
                "series": {hour: dict(bucket) for hour, bucket in row["series"].items()},
            }
            for slot, row in state["spend"].items()
        },
    }


def _workstreams_series(series: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One board's hourly buckets as a list, OLDEST FIRST, at the series cap.

    Oldest first because the chart draws left to right in time; the cap is applied
    here as well as in the step so a merge of several spenders' windows cannot hand a
    reader more hours than the contract states.
    """
    hours = sorted(series)[-WORKSTREAMS_SERIES_LIMIT:]
    return [
        {
            "hour": hour,
            "credits": round(float(series[hour]["credits"]), 6),
            "accepted": int(series[hour]["accepted"]),
        }
        for hour in hours
    ]


def _workstreams_merge(into: dict[str, Any], series: Mapping[str, Any]) -> None:
    """Add every bucket of *series* into *into*, keeping the newest hours."""
    for hour, bucket in series.items():
        _workstreams_bump(into, hour, "credits", float(bucket["credits"]))
        _workstreams_bump(into, hour, "accepted", int(bucket["accepted"]))


def _workstreams_bound_tasks(state: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Which task each worker session was bound to, keyed by that session's slot.

    THE NESTING LINK. A bound worker can be a conductor of its own board, and that
    board's slot IS the worker session key its parent bound -- the same identity
    :func:`_workstreams_units` follows to reach the sub-board's units. So the task a
    nested board hangs under is already in this state, and the link is read here
    rather than recorded in the step: a bind can be folded after the sub-board's own
    entries, and a link written at step time would then be missing for exactly the
    boards that have one.

    First bind wins, in board order then item order, so a worker bound to two tasks
    names one parent and names the same one on every read. Bounded by the board and
    task caps already applied, so this map adds no limit of its own.
    """
    bound: dict[str, dict[str, Any]] = {}
    for board_id in state["order"]:
        board = state["boards"].get(board_id)
        if board is None:
            continue
        for item_id in board["order"][:WORKSTREAMS_TASK_LIMIT]:
            item = board["items"].get(item_id)
            if item is None:
                continue
            worker = item["worker_session_key"]
            if not isinstance(worker, str) or not worker or worker in bound:
                continue
            bound[worker] = {
                "board": board_id,
                "item_id": item_id,
                "title": item["title"],
            }
    return bound


def _workstreams_tails(state: Mapping[str, Any]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """The fold's global event ring, grouped into one newest-first tail per task.

    Grouped at RENDER rather than kept per task at step time, because the budget is
    global: a per-task list would have to be trimmed against a total it cannot see,
    and the trimming would have to walk every other task to know whose turn it is.
    One pass over a bounded ring answers it instead.

    Keyed by (board, item) and not by item alone: two boards can carry the same item
    id -- a board rebuilt under a new generation keeps its predecessor's ids -- and
    merging their lines would put one board's narrative in the other's drawer.
    """
    tails: dict[tuple[str, str], list[dict[str, Any]]] = {}
    events = state.get("events")
    if not isinstance(events, list):
        return tails
    # REVERSED, so the newest line is first and the per-task cut keeps the newest.
    for row in reversed(events):
        if not isinstance(row, Mapping):
            continue
        key = (_as_str(row.get("board")), _as_str(row.get("item_id")))
        tail = tails.setdefault(key, [])
        if len(tail) >= WORKSTREAMS_EVENT_LIMIT:
            continue
        tail.append({name: row[name] for name in ("at", "kind", "status", "text") if name in row})
    return tails


def _workstreams_render(state: dict[str, Any]) -> dict[str, Any]:
    """The report's own shape: one row per board, each task costed by its worker.

    THE JOIN HAPPENS HERE, not in the step, so it cannot depend on whether a bind was
    folded before or after the charge it explains.

    A board's ``credits`` sums its DISTINCT workers' spend. A worker session bound to
    two items of one board spent its credits once, so each task row reports that
    session's spend and the board counts it a single time -- a board total built by
    adding up its task rows would multiply one session's bill by the number of items it
    served. ``credits_reported`` is false exactly when no charge was folded for the
    sessions concerned, which is what tells "spent nothing" from "nobody measured".

    Each task row also carries the ``spender`` it was costed through, and a
    nested board carries the ``parent`` task it hangs under
    (:func:`_workstreams_bound_tasks`). Those two together make this flat board list a
    TREE a reader can roll up: the parent link says where a sub-board belongs, and the
    spender is what lets a roll-up bill one worker once however many tasks of that
    subtree it served -- the rule this render already keeps for a board total, and the
    one a reader adding up task rows cannot otherwise reproduce.

    ``spender`` is an OPAQUE per-render alias, never the worker's session key. This
    payload is embedded in a page any dashboard caller can read, and the conductor
    ledger's rule is that no reader but the conductor sees a session key. An alias
    keeps the only property a reader needs -- two rows billed to the same worker
    carry the same token, two different workers do not -- while naming nobody. It is
    minted per render, so the same worker is a different token in the next read and
    the tokens cannot be accumulated across reads into a worker's history.
    """
    spend: Mapping[str, Any] = state["spend"]
    bound = _workstreams_bound_tasks(state)
    tails = _workstreams_tails(state)
    boards: list[dict[str, Any]] = []
    everywhere: dict[str, Any] = {}
    counted: set[str] = set()
    #: session key -> alias, for THIS render only. Ordinal rather than a hash: a hash
    #: of a session key is still derived from it, so a caller holding a candidate key
    #: could confirm a guess by hashing it the same way.
    aliases: dict[str, str] = {}

    def spender_of(worker: Any) -> str | None:
        """The alias for *worker*, minting one on first sight. None when unbound."""
        if not isinstance(worker, str) or not worker:
            return None
        alias = aliases.get(worker)
        if alias is None:
            #: A bare ordinal: each template supplies its own label around it, so a
            #: token carrying the word "worker" would render as "worker worker 2".
            alias = str(len(aliases) + 1)
            aliases[worker] = alias
        return alias

    #: board id -> alias, for THIS render only. A board's own id is its conductor's
    #: slot key (plus a generation), so the id is a THIRD party's session key and
    #: leaks the same way a worker's would. The alias keeps what a page needs from an
    #: id -- telling two boards apart, and a sub-board finding its parent -- and the
    #: goal beside it is what a reader actually reads.
    board_aliases: dict[str, str] = {}

    def board_alias_of(board_id: str) -> str:
        alias = board_aliases.get(board_id)
        if alias is None:
            alias = f"board-{len(board_aliases) + 1}"
            board_aliases[board_id] = alias
        return alias

    for board_id in state["order"]:
        board = state["boards"].get(board_id)
        if board is None:
            continue
        tasks: list[dict[str, Any]] = []
        total = 0.0
        charges = 0
        board_series: dict[str, Any] = {}
        _workstreams_merge(board_series, board["series"])
        seen: set[str] = set()
        needs_you = 0
        accepted = 0
        for item_id in board["order"][:WORKSTREAMS_TASK_LIMIT]:
            item = board["items"].get(item_id)
            if item is None:
                continue
            worker = item["worker_session_key"]
            row = spend.get(worker) if isinstance(worker, str) and worker else None
            if item["state"] == "accepted":
                accepted += 1
            if item["status"] in ("question", "blocked"):
                needs_you += 1
            tasks.append(
                {
                    "item_id": item_id,
                    "title": item["title"],
                    "state": item["state"],
                    "status": item["status"],
                    "summary": item["summary"],
                    "verdict": item["verdict"],
                    "pr": item["pr"],
                    "round": item["round"],
                    # The spender the cost came through, so a roll-up over a subtree
                    # can bill it once. A task with no bound worker carries None,
                    # which is also why it has no cost of its own.
                    "spender": spender_of(worker),
                    "credits": round(float(row["credits"]), 6) if row else 0.0,
                    # FALSE, never a zero cost: a worker whose provider does not bill
                    # in credits wrote no charge, and a task with no bound worker at
                    # all has nobody to have billed.
                    "credits_reported": bool(row and row["reported"]),
                    "duration_ms": int(row["duration_ms"]) if row else 0,
                    # WHEN THIS TASK LAST SPOKE, which `duration_ms` cannot answer:
                    # duration is work time, so a worker thinking for twenty minutes
                    # and a worker that died twenty minutes ago read identically.
                    # Empty for a task no worker has reported on, which is absence of
                    # silence rather than a long one.
                    "last_report_at": item["last_report_at"],
                    # THE TWO ENDS OF THIS TASK'S SPAN, so a page can place the row on
                    # a clock beside every other row and show what ran at the same
                    # time. `duration_ms` above cannot: it is the worker SESSION's
                    # total work time, one figure however many tasks that session
                    # served, and it names no instant. Either may be empty -- a task
                    # whose opener this fold never saw, or one still open -- and a
                    # page draws an unplaceable or an unfinished end rather than
                    # inventing a bound. See `_workstreams_span_opened` and
                    # `_workstreams_span_closed`.
                    "created_at": item["created_at"],
                    "closed_at": item["closed_at"],
                    # THE DRAWER'S OWN FIELDS. A reader opening one task asks what
                    # the conductor decided and what the item's own record says, and
                    # the page cannot fetch either: the frame it is minted into has
                    # `connect-src 'none'`, so whatever a drawer shows has to be in
                    # the document already.
                    "decision": item["decision"],
                    "events": tails.get((board_id, item_id), []),
                    # The count beside the tail, so "20 of 34" and "no events
                    # recorded" are different sentences. See the budget's own note.
                    "events_seen": _as_int(item.get("events_seen")),
                }
            )
            if row is None or not isinstance(worker, str) or worker in seen:
                continue
            seen.add(worker)
            total += float(row["credits"])
            charges += int(row["reported"])
            _workstreams_merge(board_series, row["series"])
            if worker not in counted:
                counted.add(worker)
                _workstreams_merge(everywhere, row["series"])
        _workstreams_merge(everywhere, board["series"])
        # A nested board: its own slot is a worker session key some task bound. Absent
        # for a board nobody bound, and for a board whose bind is its own (a conductor
        # bound as the worker of one of its items is not its own subtask).
        parent = bound.get(board["slot"])
        if parent is not None and parent["board"] == board_id:
            parent = None
        if parent is not None:
            # The parent link names a BOARD, so it goes through the same table: a raw
            # id here would carry the very key the board's own `id` withholds, and the
            # two must agree or the tree cannot find its parent.
            parent = dict(parent)
            parent["board"] = board_alias_of(parent["board"])
        boards.append(
            {
                "id": board_alias_of(board_id),
                "goal": board["goal"],
                "round": board["round"],
                "parent": parent,
                "total": len(board["order"]),
                "accepted": accepted,
                "open": sum(
                    1
                    for item_id in board["order"]
                    if board["items"].get(item_id, {}).get("state") == "open"
                ),
                "needs_you": needs_you,
                "credits": round(total, 6),
                "credits_reported": charges > 0,
                "last_activity_at": board["last_activity_at"],
                "tasks": tasks,
                "tasks_omitted": board["tasks_omitted"]
                + max(0, len(board["order"]) - WORKSTREAMS_TASK_LIMIT),
                "series": _workstreams_series(board_series),
            }
        )
        #: Whether this is the SUBJECT's own board, decided from the board's slot
        #: before the id is aliased. The alias carries no slot, by design, so the
        #: ordering question has to be answered while the slot is still in hand.
        #: Popped before the row is returned: it is an ordering fact, not a field the
        #: page is told about, and the rendered keys are pinned as an exact set.
        boards[-1]["_own"] = board["slot"] == state["slot"]
        #: The MILLISECOND stamp, carried for the sort and popped with ``_own``.
        #: ``last_activity_at`` is rendered by ``_work_iso`` and carries a LOCAL
        #: offset, so comparing two of those as strings orders them by wall-clock
        #: text: across an autumn DST hour or a timezone change a later board reads
        #: as older, and at ``WORKSTREAMS_BOARD_LIMIT`` the board moved into
        #: ``omitted`` can be the newest one. The epoch value has no such ambiguity
        #: and is already kept beside it.
        boards[-1]["_ms"] = board["last_activity_ms"]
    # Newest activity first, which is the order the page lists them in. The board's own
    # slot leads a tie so the crewmate's own workstream is not shuffled below a
    # sub-board that happens to share its last stamp.
    boards.sort(key=lambda row: (row["_ms"], row["_own"]), reverse=True)
    for row in boards:
        del row["_own"]
        del row["_ms"]
    return {
        "schema": 1,
        "slot": state["slot"],
        "items": boards[:WORKSTREAMS_BOARD_LIMIT],
        "omitted": state["omitted"] + max(0, len(boards) - WORKSTREAMS_BOARD_LIMIT),
        "series": _workstreams_series(everywhere),
        # The charges no session could be named for, stated rather than folded into a
        # board. 0.0 with a false flag is "nothing was unattributable".
        "unattributed": {
            "credits": round(float(state["spend_unattributed"]), 6),
            "credits_reported": state["spend_unattributed_reported"] > 0,
        },
        "spenders_omitted": state["spenders_omitted"],
        # SESSION UNITS the ceiling refused, stated once for the whole answer. A page
        # drawing boards from a truncated read looks complete, so this is what lets it
        # say "and older history this does not reach" instead of implying there is
        # none. It counts units rather than boards because a unit is what was refused;
        # how many boards were in it is unknowable without reading it.
        "units_omitted": _as_int(state.get("units_omitted")),
        # WHEN THE LOG LAST REACHED THIS FOLD. The page's own staleness question is
        # not "did a field fail to resolve" -- the frame's band already answers that
        # -- but "are these numbers still being produced". A gateway that died leaves
        # every value here intact and correct as of a time nothing else on the page
        # names, so this is what lets the report say so instead of reading as current.
        # Empty when the fold has applied nothing at all, which is a page with no
        # numbers rather than old ones.
        "last_entry_at": state["last_entry_at"],
    }


def _workstreams_rows(state: Mapping[str, Any]) -> int:
    """``workstreams``: each board keeps its tasks and its hourly series, and each
    spender keeps its own series."""
    boards = state.get("boards")
    rows = _rows_in(boards) + _rows_in(state.get("order")) + _rows_in(state.get("events"))
    if isinstance(boards, dict):
        for board in boards.values():
            if isinstance(board, Mapping):
                rows += (
                    _rows_in(board.get("items"))
                    + _rows_in(board.get("order"))
                    + _rows_in(board.get("series"))
                )
    spend = state.get("spend")
    rows += _rows_in(spend)
    if isinstance(spend, dict):
        for row in spend.values():
            if isinstance(row, Mapping):
                rows += _rows_in(row.get("series"))
    return rows


def _workstreams_binds_in(unit_ids: Iterable[str]) -> "tuple[str, ...]":
    """Every ``worker_session_key`` bound in *unit_ids*, for ANY board, in order.

    Not filtered to one board, which is the difference from :func:`_work_units`: this
    fold shows every workstream the slot reaches, including a bound worker that is
    itself a conductor, so the boards it must cost include that worker's own. The
    binds for those are in the sub-conductor's log, which this fold already reads.
    """
    workers: list[str] = []
    seen: set[str] = set()
    for unit_id in unit_ids:
        handle = open_session_log(unit_id)
        if handle is None:
            continue
        for entry in handle.iter_from(1, known=KNOWN_TYPES):
            if entry.type != WORK_ENTRY_TYPE or entry.data.get("action") != "bind":
                continue
            worker = entry.data.get("worker_session_key")
            if isinstance(worker, str) and worker and worker not in seen:
                seen.add(worker)
                workers.append(worker)
    return tuple(workers)


def _workstreams_units(slot: str) -> "tuple[str, ...]":
    """The slot's own units, then every bound worker's, then the nested boards', oldest first.

    The conductor's units lead, which is what :func:`_work_units` establishes and what
    makes a bind known before the worker's own entries are read; the workers follow in
    the order their binds were recorded. A worker's units are the ones carrying its
    ``usage`` entries -- the same files: one session appends its work records and its
    turn closers to one crew log, so ``work_crew_log_units`` lists exactly the units
    this fold needs for both halves.

    THEN the nested boards. A bound worker can be a conductor of its own board, and
    this fold shows that board (its entries are in a log already being read), so it
    must be able to COST it: the sub-board's own workers are reached by following the
    binds recorded in the logs already collected. A board the fold shows with no
    reachable spend reports ``credits_reported: false`` honestly, but it reports
    nothing a reader can act on, and the bind that would fix it is already in hand.

    The walk runs to closure, NOT a fixed number of rounds, because a round count
    cannot express the invariant: a board becomes VISIBLE when its conductor's units are
    read, and becomes COSTABLE one round later, when that conductor's own workers are
    read. Any fixed depth therefore has a last round whose boards it shows and cannot
    cost -- the exact state this walk exists to prevent. :data:`WORKSTREAMS_UNIT_LIMIT`
    is the bound instead, and it is the real one: a cycle is excluded by the dedupe, and
    a round that adds no unit ends the walk, so the rounds cannot outnumber the units.

    THE BOUND IS APPLIED AT ADMISSION, on every unit this function retains, including
    the conductor's own and its direct workers'. A limit tested only between rounds
    bounds the WALK and not the population: the first round is a whole slot's listing
    plus every bound worker's, so a conductor with ten thousand units had ten thousand
    in the list before the first test ran, and the bound it was given described a state
    the list had already left.
    """
    units: list[str] = []
    known: set[str] = set()
    dropped = 0

    def admit(candidate: "Sequence[str]") -> list[str]:
        """Take units until the bound is reached, and COUNT the rest.

        A unit refused for want of room is counted, because a snapshot that silently
        shows fewer boards than the record holds reads as a complete answer. A unit
        refused as a duplicate is not: it is already in.
        """
        nonlocal dropped
        taken: list[str] = []
        for unit_id in candidate:
            if unit_id in known:
                continue
            if len(units) >= WORKSTREAMS_UNIT_LIMIT:
                dropped += 1
                continue
            known.add(unit_id)
            units.append(unit_id)
            taken.append(unit_id)
        return taken

    # The conductor's own units and its direct workers', through the same gate as the
    # nested boards below rather than as a list built first and measured after.
    def refused(count: int) -> None:
        nonlocal dropped
        dropped += count

    frontier = tuple(
        admit(
            _work_units(
                slot,
                session_ledger.work_crew_log_units(slot),
                limit=WORKSTREAMS_UNIT_LIMIT,
                on_drop=refused,
            )
        )
    )
    # The ceiling is NOT the loop's exit condition. Stopping on it leaves the round
    # that would have been refused unoffered, and a unit never offered is a unit never
    # counted -- the snapshot would then call a truncated read complete. `admit`
    # refuses once full, so a full walk runs one more round, counts what it cannot
    # take, and ends on that round adding nothing.
    while frontier:
        added = admit(
            [
                unit_id
                for worker in _workstreams_binds_in(frontier)
                for unit_id in session_ledger.work_crew_log_units(worker)
            ]
        )
        if not added:
            break
        frontier = tuple(added)
    _WORKSTREAMS_UNITS_DROPPED[slot] = dropped
    if len(_WORKSTREAMS_UNITS_DROPPED) > _WORKSTREAMS_DROPPED_SLOTS:
        # Bounded like everything else here. The map is a handoff and not a cache, so
        # the oldest pending entry is the one nobody came back for.
        _WORKSTREAMS_UNITS_DROPPED.pop(next(iter(_WORKSTREAMS_UNITS_DROPPED)), None)
    return tuple(units)


#: How many slots may have an unread unit-overflow count pending at once. One read
#: selects units and then folds them, so an entry normally lives for one call; this
#: only bounds the leak when a selection is never followed by its fold.
_WORKSTREAMS_DROPPED_SLOTS: Final[int] = 64

#: Units this fold refused for want of room, by slot, written by
#: :func:`_workstreams_units` and read ONCE by :func:`_workstreams_bind_slot`.
#:
#: A handoff rather than shared state: the count is known only where the units are
#: chosen, which is outside the fold, and it has to reach the render so a snapshot can
#: say it is short. Keyed by slot because two reads for different slots can interleave,
#: and read-and-cleared so a later fold of the same slot cannot inherit this number.
#: Absent means zero, which is also what a warm fold that skipped selection reports.
_WORKSTREAMS_UNITS_DROPPED: Final[dict[str, int]] = {}


def _workstreams_units_dropped(slot: str) -> int:
    """How many units the last selection for *slot* refused. Clears on read."""
    return _WORKSTREAMS_UNITS_DROPPED.pop(slot, 0)


# panel -- a crew's own webview, keyed by the publishing member's slot
# --------------------------------------------------------------------------- #

#: Describes the panel RECORD, not where it lives. Equal to the store's own version
#: so the shape a drawer consumes is unchanged by the record moving into this log; a
#: consumer of a panel does not branch on which file held it.
PANEL_SCHEMA_VERSION: Final[int] = 1


def _panel_iso(stamp_ms: int) -> str:
    """An entry's epoch-millisecond ``time`` as the record's UTC ``+00:00`` spelling.

    Derived from the envelope rather than written into the entry: one clock, and no
    way for an entry to claim a publish time the log disagrees with.

    Offset-CARRYING, because this value leaves the host. The drawer hands
    ``published_at`` to ``new Date()``, which reads an offset-free string as the
    BROWSER's local time -- invisible on the loopback dashboard, where the same clock
    wrote it, and skewed by the whole offset from a remote browser in another zone.

    A stamp outside the range a ``datetime`` can hold answers ``""``, the same thing
    an absent one answers: these are bytes a reader does not control, so a damaged or
    planted ``time`` would otherwise turn every read of that slot into a crash,
    permanently, since the line stays on disk and nothing rewrites it. Losing one
    stamp costs a reader a display value; raising costs it the whole panel.
    """
    try:
        return datetime.fromtimestamp(stamp_ms / 1000, tz=timezone.utc).isoformat(
            timespec="seconds"
        )
    except (OverflowError, OSError, ValueError):
        return ""


def _panel_text(value: Any, limit: int) -> str:
    """*value* as a clamped string, or ``""``. The fold's own shape gate."""
    if not isinstance(value, str):
        return ""
    return value[:limit]


def _panel_owner_start() -> dict[str, Any]:
    """One owner's panel state: the record it last published, and its past titles."""
    return {
        "template": "",
        "title": "",
        "crew": "",
        "crew_key": "",
        "data": {},
        "published_at": "",
        "history": [],
        "publishes": 0,
        # Overflow is COUNTED, not silently dropped: a trimmed tail otherwise reads
        # exactly like a crew that never published those cycles, so a reader cannot
        # tell a bounded history from a complete one.
        "history_omitted": 0,
        # Fold order, not a timestamp: this decides which owner is evicted, and
        # ``published_at`` is empty for an entry whose ``time`` no ``datetime`` can
        # hold, so a stamp key would rank a damaged record against real ones.
        "seq": 0,
    }


def _panel_start() -> dict[str, Any]:
    # Keyed by OWNERSHIP DIGEST rather than holding one record, because one slot can
    # carry two crews: the slot is the member slug's, and a crew whose persisted
    # ``member_id`` is another crew's name-derived slug lands on the same one. With a
    # single record the later publisher's panel would be the only one the fold could
    # answer with, so the other crew's own drawer showed nothing while its entries sat
    # in this very log. ``newest`` names the owner that published last, which is what
    # a reader with no digest of its own is answered with.
    return {"owners": {}, "newest": "", "owners_omitted": 0, "seq": 0}


def _panel_step(state: dict[str, Any], entry: Entry) -> None:
    if entry.type != PANEL_ENTRY_TYPE:
        return
    data = entry.data
    # WHOLE-DOCUMENT REPLACEMENT, which is where this fold parts company with the
    # ledger's. A publish replaces the panel, so a present ``template`` and ``data``
    # are what make an entry a publish at all; an entry missing either is a damaged
    # or planted line, and applying its half over a good panel would splice two
    # cycles' state together -- exactly the mixed record whole-replacement exists to
    # prevent. Skipped rather than partially applied.
    template = _panel_text(data.get("template"), PANEL_TEMPLATE_LIMIT)
    payload = data.get("data")
    if not template or not isinstance(payload, Mapping):
        return
    stamp = _panel_iso(entry.time)
    key = _panel_text(data.get("crew_key"), PANEL_CREW_KEY_LIMIT)
    owners: dict[str, Any] = state["owners"]
    own = owners.get(key)
    if own is None:
        # Bounded before the insert, and the LEAST RECENTLY PUBLISHED owner is what
        # goes: a slot that somehow sees many owners keeps the ones publishing now,
        # and a reader whose record aged out is answered the same way a reader with no
        # record is -- an empty panel, never another crew's.
        #
        # Ranked on the fold-order ``seq`` rather than on ``published_at``, because a
        # stamp is empty for an entry whose ``time`` no ``datetime`` can hold: a
        # string key would sort every such record first and evict a live crew's panel
        # on the strength of one planted line.
        if len(owners) >= PANEL_OWNER_LIMIT:
            oldest = min(owners, key=lambda k: _as_int(owners[k].get("seq")))
            del owners[oldest]
            # Said out loud, not silently dropped: without it the slot reports no
            # truncation at all. It counts evictions and names none of them -- the
            # deletion above removes the only thing that could identify one -- so it
            # cannot tell an evicted crew from one that never published here.
            state["owners_omitted"] = _as_int(state.get("owners_omitted")) + 1
        own = owners[key] = _panel_owner_start()
    # The SUPERSEDED panel becomes a history row, before the new one overwrites it,
    # so the row describes the panel that is being replaced rather than the one
    # replacing it. Nothing is appended for the first publish: there is no earlier
    # panel to record, and a row describing the empty start state would read as a
    # publish that never happened. Per owner, because a crew's history is its own.
    if own["template"]:
        rows: list[dict[str, str]] = own["history"]
        rows.append(
            {
                "at": own["published_at"],
                "title": own["title"],
                "template": own["template"],
            }
        )
        # Bounded like every other fold state here: the oldest superseded panel ages
        # out so a crew publishing every cycle cannot grow the record without limit.
        # What ages out is COUNTED, for the same reason the owner eviction is.
        if len(rows) > PANEL_HISTORY_LIMIT:
            dropped = len(rows) - PANEL_HISTORY_LIMIT
            del rows[:dropped]
            own["history_omitted"] = _as_int(own.get("history_omitted")) + dropped
    own["template"] = template
    own["data"] = dict(payload)
    own["title"] = _panel_text(data.get("title"), PANEL_TITLE_LIMIT)
    own["crew"] = _panel_text(data.get("crew"), PANEL_TITLE_LIMIT)
    own["crew_key"] = key
    own["published_at"] = stamp
    own["publishes"] += 1
    state["seq"] = _as_int(state.get("seq")) + 1
    own["seq"] = state["seq"]
    state["newest"] = key


def _panel_owner_record(own: Mapping[str, Any]) -> dict[str, Any]:
    """One owner's record, in the shape the drawer already consumes.

    ``history_omitted`` is the bound speaking: it is how a reader tells a history
    trimmed at its cap from one that holds every cycle the crew ever published. It
    belongs here because it is the OWNER's own bound. The slot's eviction count is
    not: eviction deletes an owner's entry, so the crew that count is about has no
    record here to read it from, and a copy on each surviving owner's record would
    answer a question none of them is asking.
    """
    return {
        "schema": PANEL_SCHEMA_VERSION,
        "template": own["template"],
        "title": own["title"],
        "crew": own["crew"],
        "crew_key": own["crew_key"],
        "data": dict(own["data"]),
        "published_at": own["published_at"],
        "history": [dict(row) for row in own["history"]],
        "publishes": own["publishes"],
        "history_omitted": _as_int(own.get("history_omitted")),
    }


def _panel_render(state: dict[str, Any]) -> dict[str, Any]:
    """The panel RECORD, in the shape the drawer already consumes.

    Deliberately the same keys the store's document carried when it was a file of
    its own, so the read route, the composer and the drawer did not have to learn a
    new shape to stop being a second copy of the truth. ``history`` and ``publishes``
    are what the file could not hold: one overwritable document has no past, which is
    the whole reason the record moved into this log.

    The top level is the NEWEST publish on this slot, and ``owners`` carries one
    record per publishing crew keyed by its ownership digest. A reader that knows
    which crew it is asking about reads its own entry there; the top level is for a
    reader that does not, and on an uncontested slot the two are the same record.

    An empty ``template`` is how a reader tells "this crew has published nothing"
    from "this crew published an empty panel": the store refuses a publish that names
    no template, so no real record has one.

    ``owners_omitted`` is the owner bound speaking, and it sits on THIS record alone
    rather than on each owner's, because the crew it is about has none: eviction
    deletes the owner's entry. So it speaks only about the fold. It rises on the
    eviction of any owner with no reference to any particular key, so a non-zero value
    says the slot truncated and cannot say whom -- a crew that never published on a
    slot four others filled reads exactly what a genuinely evicted crew reads. ``0``
    says this fold recorded no eviction, which is NOT the same as "this crew never
    published": the append is best-effort, so a publish whose entry never landed
    leaves no record here while the store's file still holds that crew's panel.
    """
    owners: dict[str, Any] = state["owners"]
    newest = owners.get(state["newest"])
    record = _panel_owner_record(newest if newest is not None else _panel_owner_start())
    record["owners"] = {key: _panel_owner_record(own) for key, own in owners.items()}
    record["owners_omitted"] = _as_int(state.get("owners_omitted"))
    return record


# -------------------------------------------------------------------------- #
# the dynamic dashboard: agentic values, and the mistake book
# -------------------------------------------------------------------------- #


def _dashboard_value_fits(value: Any) -> bool:
    """Whether one agentic value is small enough to retain.

    RE-APPLIED here rather than trusted from the writer, like every other ceiling
    in this file: this runs over bytes off a file the reader does not control, so a
    planted or damaged line is exactly the input that ignores the writer's clamp.

    Measured on the serialized form because that is what the ceiling is about, and
    an unserializable value is refused by the same test -- a value the fold cannot
    encode is one no reader can be handed.
    """
    try:
        blob = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return False
    return len(blob.encode("utf-8")) <= DASHBOARD_VALUE_BYTES


def _dashboard_type_holds(declared: str, value: Any) -> bool:
    """Whether *value* is of the manifest type *declared*.

    THE SAME FIVE TYPES the manifest declares (``manifest.FIELD_TYPES``), checked
    the same way the write path checks them. Two copies of one rule, and that is
    deliberate rather than an oversight: this module must not import
    ``dashboard_templates`` -- the manifest imports THIS one, for the fold names --
    so the shared helper cannot live there without closing the cycle. A test pins
    the two against one table of cases.

    A bool is NOT a number, which is the one case worth stating: Python says
    ``isinstance(True, int)``, so without the exclusion a crewmate writing ``true``
    into a ``number`` field would pass the check and the page would render ``True``
    where a count belongs.
    """
    if declared == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if declared == "string":
        return isinstance(value, str)
    if declared == "boolean":
        return isinstance(value, bool)
    if declared == "array":
        return isinstance(value, list)
    if declared == "object":
        return isinstance(value, dict)
    return False


def _agentic_start() -> dict[str, Any]:
    """One slot's agentic values: the latest per FIELD, and what was evicted.

    Keyed by field rather than holding one document, because the fields are
    independent cells: a crewmate that learns one number writes that one, and a
    whole-document fold would make it restate the other twenty-three to report it.
    That is the one place this fold parts company with ``panel``, whose entry
    replaces a whole published panel.
    """
    return {"fields": {}, "fields_omitted": 0, "wrote": 0}


def _agentic_step(state: dict[str, Any], entry: Entry) -> None:
    if entry.type != DASHBOARD_AGENTIC_ENTRY_TYPE:
        return
    data = entry.data
    name = _panel_text(data.get("field"), DASHBOARD_FIELD_LIMIT)
    declared = _panel_text(data.get("type"), DASHBOARD_TYPE_LIMIT)
    if not name or not declared:
        # A line naming no field or no type cannot be applied to a cell: a damaged
        # or planted entry, skipped rather than half-applied, for the reason
        # ``_panel_step`` skips one missing its template.
        return
    wrapper = data.get("value")
    # THROUGH THE WRAPPER's single key. The entry declares ``value`` as an object
    # because this registry has no any-type (see the entry type's own note); the
    # real value is ``v``. A line with no wrapper, or one carrying no ``v``, names
    # no value at all and is skipped like one naming no field.
    if not isinstance(wrapper, Mapping) or "v" not in wrapper:
        return
    value = wrapper["v"]
    # THE TYPE IS RE-CHECKED, not taken on the writer's word. The write path checked
    # it against the live manifest, but the entry is on disk and the reader is a
    # different process at a different time: a line whose declared type and value
    # disagree is exactly the corruption this re-check exists to drop, and applying
    # it would put a string in a cell a chart reads as a number.
    if not _dashboard_type_holds(declared, value) or not _dashboard_value_fits(value):
        return
    fields: dict[str, Any] = state["fields"]
    if name not in fields and len(fields) >= DASHBOARD_VALUE_LIMIT:
        # Bounded before the insert, and the LEAST RECENTLY WRITTEN field is what
        # goes: a slot that has seen many field sets keeps the ones being written
        # now. Ranked on the fold-order counter rather than on a stamp, because a
        # stamp is empty for an entry whose ``time`` no ``datetime`` can hold --
        # the same reason ``_panel_step`` ranks its owners that way.
        oldest = min(fields, key=lambda key: _as_int(fields[key].get("order")))
        del fields[oldest]
        state["fields_omitted"] = _as_int(state.get("fields_omitted")) + 1
    state["wrote"] = _as_int(state.get("wrote")) + 1
    fields[name] = {
        # UNWRAPPED for the reader. The wrapper is an artefact of what the entry
        # registry can declare, and a reader of this fold should not have to know
        # about it: a template's field is a value, not a value in a box.
        "value": value,
        "type": declared,
        "at": _panel_iso(entry.time),
        "instance_version": _as_int(data.get("instance_version")),
        "crew_key": _panel_text(data.get("crew_key"), PANEL_CREW_KEY_LIMIT),
        "order": state["wrote"],
    }


def _agentic_render(state: dict[str, Any]) -> dict[str, Any]:
    """The agentic values, one row per field.

    ``order`` is dropped: it is the fold's own eviction ranking and means nothing
    to a reader, who gets ``at`` for the same question. ``wrote`` counts ACCEPTED
    writes over the slot's whole life, which is what tells a reader "this crewmate
    is writing and the cell is still empty" from "this crewmate has never written".
    """
    fields: dict[str, Any] = state["fields"]
    return {
        "fields": {
            name: {key: row[key] for key in row if key != "order"} for name, row in fields.items()
        },
        "fields_omitted": _as_int(state.get("fields_omitted")),
        "wrote": _as_int(state.get("wrote")),
    }


def _agentic_rows(state: Mapping[str, Any]) -> int:
    """``agentic``: one row per field, each holding one capped value."""
    return _rows_in(state.get("fields"))


def _mistake_key(code: str, field: str) -> str:
    """The group key, ``(code, field)`` flattened.

    Flattened because fold state is JSON on disk and a tuple key is not, and
    separated by a character neither half can contain: a refusal code is a bare
    identifier and a field name matches ``manifest._FIELD``, so neither carries a
    space. Without that the two halves could be split wrongly on read-back and two
    different mistakes would land on one group.
    """
    return f"{code} {field}"


def _mistakes_start() -> dict[str, Any]:
    """The mistake book: one group per ``(code, field)``, plus what was evicted."""
    return {"groups": {}, "groups_omitted": 0, "refused": 0, "seen": 0}


def _mistakes_step(state: dict[str, Any], entry: Entry) -> None:
    if entry.type != DASHBOARD_REFUSED_ENTRY_TYPE:
        return
    data = entry.data
    code = _panel_text(data.get("code"), DASHBOARD_CODE_LIMIT)
    if not code:
        # A line that cannot say what it is cannot be applied to a group. The append
        # validator refuses one too (``code`` is required on the type), so reaching
        # here means a damaged or planted line -- skipped rather than counted, for
        # the reason ``_panel_step`` skips one missing its template.
        return
    field = _panel_text(data.get("field"), DASHBOARD_FIELD_LIMIT)
    stamp = _panel_iso(entry.time)
    corrects = data.get("corrects")
    # A CORRECTION, which an ACCEPTED write appends and a refusal never does: it is
    # not a refusal, it is the answer to one. It names the fields an earlier refusal
    # reached wrongly, and the fix is the field the accepted write actually used.
    #
    # Decided on the CODE, which is the one key always present, rather than on the
    # presence of ``corrects``: an earlier draft branched on the list and gave a
    # correction no code at all, which the append validator refused outright -- so
    # every correction was dropped at the door and the book stayed a list of errors
    # with no answers in it.
    if code == MISTAKE_CORRECTED_CODE:
        if not isinstance(corrects, list):
            return
        for raw in corrects:
            wrong = _panel_text(raw, DASHBOARD_FIELD_LIMIT)
            if not wrong:
                continue
            for key, group in state["groups"].items():
                if key.endswith(f" {wrong}"):
                    fixes: list[str] = group.setdefault("corrected_to", [])
                    if field and field not in fixes:
                        fixes.append(field)
                        # NEWEST KEPT, oldest dropped: the useful answer is the
                        # latest one, and a long list would make the reader choose
                        # -- which is the problem this fold exists to remove.
                        del fixes[:-MISTAKE_CORRECTION_LIMIT]
                    group["corrected_at"] = stamp
        return
    state["refused"] = _as_int(state.get("refused")) + 1
    key = _mistake_key(code, field)
    groups: dict[str, Any] = state["groups"]
    group = groups.get(key)
    if group is None:
        if len(groups) >= MISTAKE_GROUP_LIMIT:
            # Least recently SEEN goes, so the groups kept are the mistakes still
            # being made rather than the first ones ever made.
            oldest = min(groups, key=lambda other: _as_int(groups[other].get("order")))
            del groups[oldest]
            state["groups_omitted"] = _as_int(state.get("groups_omitted")) + 1
        group = {
            "code": code,
            "field": field,
            "count": 0,
            "first_seen": stamp,
            "reason": "",
            "corrected_to": [],
            "corrected_at": "",
        }
        groups[key] = group
    state["seen"] = _as_int(state.get("seen")) + 1
    group["count"] = _as_int(group.get("count")) + 1
    group["last_seen"] = stamp
    group["order"] = state["seen"]
    # The LATEST wording wins. The sentence is what the agent was told, so an
    # updated refusal message should be what it reads back -- handing it the first
    # wording would teach a correction the gateway has since improved.
    group["reason"] = _panel_text(data.get("reason"), DASHBOARD_REASON_LIMIT)
    group["crew_key"] = _panel_text(data.get("crew_key"), PANEL_CREW_KEY_LIMIT)


def _mistakes_render(state: dict[str, Any]) -> dict[str, Any]:
    """The mistake book, worst group first.

    Ordered by COUNT rather than by recency, because the reader is an agent with
    one short list to read before it writes: a mistake made five times is the one
    most worth not repeating, and recency is already on each row as ``last_seen``.
    """
    groups: list[dict[str, Any]] = [
        {key: value for key, value in group.items() if key != "order"}
        for group in state["groups"].values()
    ]
    groups.sort(key=lambda group: (-_as_int(group.get("count")), str(group.get("field"))))
    return {
        "groups": groups,
        "groups_omitted": _as_int(state.get("groups_omitted")),
        "refused": _as_int(state.get("refused")),
    }


def _mistakes_rows(state: Mapping[str, Any]) -> int:
    """``mistakes``: one row per group, each a small fixed record."""
    return _rows_in(state.get("groups"))


def _as_int(value: Any) -> int:
    """*value* when it is a real int, else 0 -- a bool is not a count."""
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _keyable(label: str) -> bool:
    """Whether *label* is safe to use as a per-thing KEY.

    ``_as_str`` cuts a retained label to ``TEXT_LIMIT``, and a label sitting
    exactly at that length cannot be told apart from one that was cut -- so two
    different things sharing a head would land on one key and report each other's
    totals, which is a wrong answer rather than a big one. A label at the limit is
    therefore not keyed. It goes where a label past the COUNT budget goes: counted
    in the whole-session totals, which stay exact, and reported as omitted detail.
    """
    return len(label) < TEXT_LIMIT


def _note_omitted(
    state: dict[str, Any],
    label: str,
    *,
    seen_key: str,
    count_key: str,
    saturated_key: str,
    budget: int,
) -> None:
    """Record *label* as detail this fold left out, counting each one once.

    The count is of DISTINCT labels, so it is deduplicated against a list -- and
    that list is itself retained, so it is capped like everything else here. With
    the cap reached, a label cannot be recognised as one already counted, and
    counting it again would count one label once per APPEARANCE rather than once:
    a tool name reaches this path from its call and again from its completion, and
    a model reaches it once per turn. The count therefore stops at the budget and
    *saturated_key* says it has become a floor rather than a total.
    """
    seen: list[str] = state[seen_key]
    if label in seen:
        return
    if len(seen) < budget:
        seen.append(label)
        state[count_key] += 1
    else:
        state[saturated_key] = True


def _as_text_or_none(value: Any) -> str | None:
    """*value* cut to ``TEXT_LIMIT`` when it is a string, else ``None``.

    The nullable sibling of :func:`_as_str`, for a retained field whose ABSENCE is
    meaningful -- a close reason, a stop reason, an error -- where the empty string
    would assert a reason of no characters instead of no reason at all. The size
    bound is the same, because the field is retained either way.
    """
    if not isinstance(value, str):
        return None
    return value[:TEXT_LIMIT]


def _as_str(value: Any) -> str:
    """*value* when it is a string, cut to ``TEXT_LIMIT``, else the empty one.

    Every ``data`` field these folds read comes off bytes a reader does not
    control, so the shape is checked here rather than trusted from the type
    declaration: a declaration binds the WRITER, and a damaged or planted line is
    exactly the input that ignores it.

    The LENGTH is part of that shape. Every caller here retains what it gets --
    as a ``by_name`` key, a server in a row, an approval's reason -- and a count
    cap bounds how MANY are kept, never how big each one is, so one coercion
    point is where the size bound belongs rather than at each of the dozen
    retention sites that would each have to remember it.
    """
    if not isinstance(value, str):
        return ""
    return value[:TEXT_LIMIT]


def _as_id(value: Any) -> str:
    """*value* when it is a string short enough to PAIR on, else the empty one.

    An identity is not a label and is deliberately not truncated: two distinct
    ids sharing a ``TEXT_LIMIT``-character head would collapse into one identity,
    and a completion would then close a different call's frame -- turning a
    bounded-memory fix into a wrong answer. Past ``ID_LIMIT`` an id identifies
    nothing, which is the same thing an absent one does, so it takes the same
    path: counted, and left unpaired.
    """
    if not isinstance(value, str) or not value or len(value) > ID_LIMIT:
        return ""
    return value


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

#: Where a credit charge came from, which is the split ``usage`` keeps beside its
#: total. Fixed rather than discovered: these are the three writers that carry a
#: ``credits`` field, so the buckets are a closed set and a reader is never shown a
#: partial split. A fourth spender would add a bucket here and move ``usage``'s own
#: :attr:`_Fold.state_version`, which is the version its savepoints carry.
CREDIT_SOURCES: Final[tuple[str, ...]] = ("turn", "subagent", "background")

#: Which bucket each billing entry type lands in.
_CREDIT_SOURCE_OF: Final[dict[str, str]] = {
    "turn/completed": "turn",
    "subagent/completed": "subagent",
    "subagent/failed": "subagent",
    "background/completed": "background",
}

#: Entry types ``tools`` pairs: a call and the completion that closes it.
TOOL_TYPES: Final[frozenset[str]] = frozenset({"tool/called", "tool/completed"})

#: Every entry type the ``workstreams`` fold reads: the work record, the opener that
#: names a unit's slot, and the three closers that carry a charge.
#:
#: HERE rather than beside that fold's own constants because it is spelled FROM the
#: billing table just above -- a fourth spender added to :data:`_CREDIT_SOURCE_OF` is
#: then read by the report fold without a second edit, which is the whole point of
#: deriving it. Declared after that table because a module-level expression cannot
#: read a name defined further down the file.
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


def _rows_in(value: Any) -> int:
    """How many rows *value* holds, for a list or a dict; 0 for anything else.

    Tolerant on purpose. These run over a state that may have come off disk from an
    older build, and a charge that raised would take down the read that was storing the
    cell -- where answering 0 only under-counts a container the fold does not use.
    """
    return len(value) if isinstance(value, (list, dict)) else 0


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
    return rows + _rows_in(state.get("parked")) + _rows_in(state.get("order"))


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
        state_version=_FOLD_STATE_VERSION_BASE + 10,
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


def state_is_serializable(checkpoint: Checkpoint) -> bool:
    """Whether *checkpoint*'s state survives a JSON round trip unchanged.

    A checkpoint is only resumable if it can be written down, so this is the
    property a caller storing one checks rather than assumes.
    """
    try:
        return json.loads(json.dumps(checkpoint.state)) == checkpoint.state
    except (TypeError, ValueError):
        return False
