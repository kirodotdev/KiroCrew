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
``tokens`` only on a provider-reported close, so a synthesized closer omits them
-- and a total that counted those turns as costing nothing would state a
measurement nobody made. Each total therefore rides beside the count of turns
that contributed to it, and a caller comparing the two learns what the total
covers.

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
than the five panel folds make and is why it is not one of them: the growth push
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
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Final

from kiro_crew.crew_log.entry_types import (
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
)
from kiro_crew.crew_log.errors import CODE_BAD_DATA, CrewLogError
from kiro_crew.crew_log.schema import KIND_SESSION, Entry
from kiro_crew.crew_log.store import (
    CrewLog,
    segment_paths,
    session_units_for_slot,
    unit_header_created_at,
)

if TYPE_CHECKING:
    # Type-only: the savepoint module imports this one, so a runtime import here
    # would close the cycle the function-local imports below exist to avoid.
    from kiro_crew.crew_log.checkpoint import PrefixWitness

# The ledger fold reads a record whose semantics -- which phases end a workstream,
# which event kinds exist, how much of each field is kept -- belong to the ledger
# subsystem. They are imported rather than restated so one owner sets them, the
# same direction ``store`` already takes for the store-name fold.
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

#: Projections keyed by a SLOT instead of by one crew log. A slot owns one ACP
#: session id at a time, so a fact that belongs to the slot for its whole life --
#: its work ledger -- is spread over a unit per id it ran under, and answering for
#: it means joining them (:func:`fold_slot`). Kept out of
#: :data:`PROJECTION_NAMES` for that reason: the growth push and the side panel
#: address a session, and pushing a slot-wide value under one session's id would
#: report a partial answer as the whole one.
SLOT_PROJECTION_NAMES: Final[tuple[str, ...]] = ("ledger", "radar")

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

#: Distinct tool names a ``tools`` projection details. Past it the totals stay
#: exact and ``names_omitted`` counts the names left out.
TOOL_NAME_LIMIT: Final[int] = 100

#: Distinct models a ``usage`` projection details per model. Past it the whole-
#: session totals stay exact and ``models_omitted`` counts the models left out,
#: the same posture as ``TOOL_NAME_LIMIT``. A turn carries a model string, so an
#: unbounded ``by_model`` would grow the checkpoint over a long session.
MODEL_LIMIT: Final[int] = 100

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

#: Entries folded per pass over a log. Five folds consume the same entries, so a
#: single generator would be exhausted by the first one and the span has to be
#: materialized -- but materializing the WHOLE span puts an entire cold-folded
#: log in memory at once, and a cold fold is the ordinary first read for any
#: session. Folding in chunks keeps one pass over the file while bounding what is
#: held to this many entries.
FOLD_CHUNK_ENTRIES: Final[int] = 1024

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
    """

    name: str
    seq: int
    value: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "seq": self.seq, "value": self.value}


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
    """One projection's three pure pieces."""

    name: str
    start: Callable[[], dict[str, Any]]
    step: Callable[[dict[str, Any], Entry], None]
    render: Callable[[dict[str, Any]], dict[str, Any]]


def require_name(name: str) -> str:
    """*name* if it is a projection this module folds, else raise ``bad_data``."""
    if name not in _FOLDS:
        raise CrewLogError(
            f"unknown projection {name!r}; expected one of {list(FOLD_NAMES)}",
            code=CODE_BAD_DATA,
            field="name",
        )
    return name


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


def projection_of(checkpoint: Checkpoint) -> Projection:
    """*checkpoint* rendered -- the value a reader is served, at its own seq."""
    fold_spec = _FOLDS[require_name(checkpoint.name)]
    return Projection(
        name=checkpoint.name,
        seq=checkpoint.last_seq,
        value=fold_spec.render(checkpoint.state),
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
    # ONE pass over the file, in bounded chunks. Five folds consume the same
    # entries, so a bare generator would be exhausted by the first of them and
    # some materialization is required -- but materializing the whole span holds
    # an entire cold-folded log in memory, and a cold fold (no reusable bundle,
    # so ``from_seq`` is 1) is the ordinary first read for any session. Chunking
    # keeps the single pass and bounds what is held to ``FOLD_CHUNK_ENTRIES``.
    # Folding a span in pieces is the same value as folding it whole: ``advance``
    # is seq-anchored and each chunk is strictly after the last, which is the
    # property the incremental-equals-from-scratch test pins at every split.
    grown = dict(base)
    chunk: list[Entry] = []
    for entry in handle.iter_from(from_seq, known=KNOWN_TYPES):
        chunk.append(entry)
        if len(chunk) >= FOLD_CHUNK_ENTRIES:
            grown = _advance_all(grown, chunk)
            chunk.clear()
    if chunk:
        grown = _advance_all(grown, chunk)
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


def _advance_all(
    checkpoints: dict[str, Checkpoint], chunk: Sequence[Entry]
) -> dict[str, Checkpoint]:
    """Every checkpoint advanced over the part of *chunk* it has not consumed.

    The per-checkpoint filter is what lets one chunk serve every fold that may sit
    at DIFFERENT seqs: a reused bundle can hold a status checkpoint further along
    than its tools one, and ``advance`` refuses an entry at or below the seq it
    already reached rather than silently double-counting it.
    """
    return {
        name: advance(cp, tuple(entry for entry in chunk if entry.seq > cp.last_seq))
        for name, cp in checkpoints.items()
    }


def read_projection(session_id: str, name: str) -> Projection:
    """One projection for *session_id*, folded from the start of its crew log."""
    bundle = fold_session(session_id, (require_name(name),))
    return bundle.projection(name)


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
        "credits_turns": 0,
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
        "compactions": 0,
        "freed_pct": 0.0,
        "steps": 0,
        "step_ms": 0,
    }


def _usage_step(state: dict[str, Any], entry: Entry) -> None:
    data = entry.data
    if entry.type == "turn/completed":
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
        credits = data.get("credits")
        # Absent credits are NOT zero: a synthesized closer reports no cost
        # because none was measured, and folding that in as 0.0 would state a
        # measurement nobody made. The count beside the total is what tells a
        # reader how many turns the total covers.
        if isinstance(credits, (int, float)) and not isinstance(credits, bool):
            state["credits"] += float(credits)
            state["credits_turns"] += 1
            if per_model is not None:
                per_model["credits"] += float(credits)
                per_model["credits_turns"] += 1
        tokens = data.get("tokens")
        if isinstance(tokens, dict):
            state["tokens_turns"] += 1
            for dimension in TOKEN_DIMENSIONS:
                measured = _as_int(tokens.get(dimension))
                state["tokens"][dimension] += measured
                if per_model is not None:
                    per_model["tokens"] += measured
        duration = data.get("duration_ms")
        if isinstance(duration, int) and not isinstance(duration, bool):
            state["duration_ms"] += duration
            state["duration_turns"] += 1
    elif entry.type == "context/composed":
        state["context_tokens"] += _as_int(data.get("tokens"))
        state["context_chars"] += _as_int(data.get("chars"))
        if data.get("tokens_estimated") is True:
            state["context_estimated"] += 1
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
            "credits_reported": state["credits_turns"],
            "tokens_reported": state["tokens_turns"],
            "duration_reported": state["duration_turns"],
        },
        "credits": round(state["credits"], 6),
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
    return {"moments": [], "dropped": 0}


def _timeline_step(state: dict[str, Any], entry: Entry) -> None:
    if entry.type not in TIMELINE_TYPES:
        return
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


def fold_slot_checkpoint(name: str, unit_ids: Sequence[str]) -> Checkpoint:
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
    """
    fold_spec = _FOLDS[require_name(name)]
    state = fold_spec.start()
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


def fold_slot(name: str, unit_ids: Sequence[str]) -> Projection:
    """:func:`fold_slot_checkpoint` rendered -- the value a slot-keyed reader is served."""
    return projection_of(fold_slot_checkpoint(name, unit_ids))


def read_slot_projection(slot: str, name: str) -> Projection:
    """One slot-keyed projection for *slot*, folded over every unit it ran under.

    The units come from the fold's OWNER, not from a raw store listing. For the ledger
    that owner drops the units a permanent delete excluded and puts the recorded order
    ahead of the header clock, and a raw listing here would serve a different answer
    from the one every other reader gets -- including a deleted conversation's goal and
    phase on a recycled slot key. A fold whose owner has no such rule falls through to
    the store listing, which is what it would have used anyway.
    """
    return fold_slot(require_name(name), _slot_units_for_fold(slot, name))


def _slot_units_for_fold(slot: str, name: str) -> "tuple[str, ...]":
    """The units *name* is folded over for *slot*, as that fold's owner defines them."""
    if name == LEDGER_FOLD_NAME:
        from kiro_crew import session_ledger

        return session_ledger.crew_log_units(slot)
    return session_units_for_slot(slot)


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


_FOLDS: Final[dict[str, _Fold]] = {
    "status": _Fold("status", _status_start, _status_step, _status_render),
    "usage": _Fold("usage", _usage_start, _usage_step, _usage_render),
    "timeline": _Fold("timeline", _timeline_start, _timeline_step, _timeline_render),
    "tools": _Fold("tools", _tools_start, _tools_step, _tools_render),
    "approvals": _Fold("approvals", _approvals_start, _approvals_step, _approvals_render),
    "class": _Fold("class", _class_start, _class_step, _class_render),
    "ledger": _Fold("ledger", _ledger_start, _ledger_step, _ledger_render),
    "radar": _Fold("radar", _radar_start, _radar_step, _radar_render),
}

if tuple(_FOLDS) != FOLD_NAMES:  # pragma: no cover - import-time consistency
    raise RuntimeError(
        "the fold registry and FOLD_NAMES disagree: " f"{tuple(_FOLDS)} against {FOLD_NAMES}"
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
