"""Folds over ONE session's crew log -- the session side panel's five views.

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
than its own crew log). Resolving a ``ref`` is the PAGE path's work, in the routes
that serve a person a citation to follow.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from kiro_crew.crew_log.entry_types import SESSION_ENTRY_TYPES
from kiro_crew.crew_log.errors import CODE_BAD_DATA, LedgerError
from kiro_crew.crew_log.schema import KIND_SESSION, Entry
from kiro_crew.crew_log.store import Ledger

#: The session side panel's projections, in the RFC section 5 order.
PROJECTION_NAMES: Final[tuple[str, ...]] = (
    "status",
    "usage",
    "timeline",
    "tools",
    "approvals",
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

#: Open tool calls and pending approvals listed individually.
OPEN_LIST_LIMIT: Final[int] = 50

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
        "remote/placed",
        "remote/lost",
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

    The state is JSON-serializable so a caller may persist it. Storing it on disk
    is not this module's business, and the shape is what a later checkpoint file
    would carry.
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
            raise LedgerError(f"unknown projection: {name!r}", code=CODE_BAD_DATA, field="name")
        if not isinstance(last_seq, int) or isinstance(last_seq, bool) or last_seq < 0:
            raise LedgerError(
                f"checkpoint last_seq must be a non-negative int: {last_seq!r}",
                code=CODE_BAD_DATA,
                field="last_seq",
            )
        if not isinstance(state, dict):
            raise LedgerError(
                "checkpoint state must be an object", code=CODE_BAD_DATA, field="state"
            )
        return cls(name=name, last_seq=last_seq, state=state)


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
        raise LedgerError(
            f"unknown projection {name!r}; expected one of {list(PROJECTION_NAMES)}",
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
            raise LedgerError(
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

    One pass over the file serves all five, which is what makes pushing the whole
    side panel on each growth cost one read rather than five.
    """

    session_id: str
    last_seq: int
    checkpoints: Mapping[str, Checkpoint] = field(default_factory=dict)

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


def open_session_log(session_id: str) -> Ledger | None:
    """This session's crew log opened for READING, or ``None`` when it has none.

    Never repairs. Repair appends closers and takes write ownership, which
    belongs to the gateway resuming the session, and a read path that claimed it
    would refuse whenever the live writer holds it -- turning "show me this
    session" into an error for exactly the sessions that are running.
    """
    if not Ledger.exists(KIND_SESSION, session_id):
        return None
    return Ledger.open(KIND_SESSION, session_id)


def fold_session(
    session_id: str,
    names: Iterable[str] = PROJECTION_NAMES,
    *,
    since: SessionProjections | None = None,
    log: Ledger | None = None,
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
    """
    wanted = tuple(require_name(name) for name in names)
    handle = log if log is not None else open_session_log(session_id)
    if handle is None:
        return empty_session(session_id, wanted)
    last_seq = handle.last_seq
    reusable = (
        since is not None
        and since.session_id == session_id
        and since.last_seq <= last_seq
        and all(name in since.checkpoints for name in wanted)
    )
    base: dict[str, Checkpoint] = (
        {name: since.checkpoints[name] for name in wanted}
        if reusable and since is not None
        else {name: initial(name) for name in wanted}
    )
    from_seq = min((cp.last_seq for cp in base.values()), default=0) + 1
    if from_seq > last_seq:
        return SessionProjections(session_id=session_id, last_seq=last_seq, checkpoints=base)
    # ONE pass, materialized, because five folds consume the same entries and a
    # generator would be exhausted by the first. The span is bounded by the
    # caller: an incremental refresh reads what one batch appended.
    fresh = tuple(handle.iter_from(from_seq, known=KNOWN_TYPES))
    grown = {
        name: advance(cp, tuple(entry for entry in fresh if entry.seq > cp.last_seq))
        for name, cp in base.items()
    }
    reached = max((cp.last_seq for cp in grown.values()), default=last_seq)
    return SessionProjections(session_id=session_id, last_seq=reached, checkpoints=grown)


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
        "remote": None,
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
        for key in ("agent", "owner", "slot", "cwd"):
            value = data.get(key)
            if isinstance(value, str):
                state[key] = value
        model = data.get("model")
        if isinstance(model, str) and model:
            state["model"] = model
        # A reopened session is serving again, so the close a reader would have
        # seen before it describes a life this entry has ended.
        state["closed_at"] = None
        state["close_reason"] = None
    elif kind == "session/seeded":
        state["seeded"] = True
    elif kind == "session/closed":
        state["closed_at"] = entry.time
        reason = data.get("reason")
        state["close_reason"] = reason if isinstance(reason, str) else None
        state["open_turn"] = None
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
        reason = data.get("stop_reason")
        state["last_stop_reason"] = reason if isinstance(reason, str) else None
        error = data.get("error")
        state["last_error"] = error if isinstance(error, str) else None
        for key in ("model", "provider"):
            value = data.get(key)
            if isinstance(value, str) and value:
                state[key] = value
        state["open_turn"] = None
    elif kind == "turn/refused":
        state["turns_refused"] += 1
    elif kind == "model/selected":
        model = data.get("model")
        if isinstance(model, str) and model:
            state["model"] = model
    elif kind == "request/configured":
        for key in ("model", "provider"):
            value = data.get(key)
            if isinstance(value, str) and value:
                state[key] = value
    elif kind == "write/dropped":
        state["dropped_count"] += _as_int(data.get("dropped_count"))
        state["dropped_bytes"] += _as_int(data.get("dropped_bytes"))
    elif kind == "remote/placed":
        provider = data.get("provider")
        remote_id = data.get("id")
        state["remote"] = {
            "provider": provider if isinstance(provider, str) else "",
            "id": remote_id if isinstance(remote_id, str) else "",
            "lost_reason": None,
        }
    elif kind == "remote/lost":
        reason = data.get("reason")
        placed = state["remote"] if isinstance(state.get("remote"), dict) else {}
        state["remote"] = {
            "provider": placed.get("provider", ""),
            "id": placed.get("id", ""),
            "lost_reason": reason if isinstance(reason, str) else "",
        }


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
        "remote": dict(state["remote"]) if state["remote"] else None,
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
        per_model = state["by_model"].setdefault(
            model, {"turns": 0, "credits": 0.0, "credits_turns": 0, "tokens": 0}
        )
        per_model["turns"] += 1
        credits = data.get("credits")
        # Absent credits are NOT zero: a synthesized closer reports no cost
        # because none was measured, and folding that in as 0.0 would state a
        # measurement nobody made. The count beside the total is what tells a
        # reader how many turns the total covers.
        if isinstance(credits, (int, float)) and not isinstance(credits, bool):
            state["credits"] += float(credits)
            state["credits_turns"] += 1
            per_model["credits"] += float(credits)
            per_model["credits_turns"] += 1
        tokens = data.get("tokens")
        if isinstance(tokens, dict):
            state["tokens_turns"] += 1
            for dimension in TOKEN_DIMENSIONS:
                measured = _as_int(tokens.get(dimension))
                state["tokens"][dimension] += measured
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
        "names_omitted": 0,
        "omitted_names": [],
    }


def _tool_row(state: dict[str, Any], name: str) -> dict[str, Any] | None:
    """The per-name row for *name*, or ``None`` once the name budget is spent.

    A name past the budget is COUNTED in the totals and left out of the detail,
    so the aggregate a caller sums stays exact while the value stays bounded.
    """
    by_name: dict[str, Any] = state["by_name"]
    row = by_name.get(name)
    if row is not None:
        return row
    if len(by_name) >= TOOL_NAME_LIMIT:
        omitted: list[str] = state["omitted_names"]
        if name not in omitted:
            state["names_omitted"] += 1
            if len(omitted) < TOOL_NAME_LIMIT:
                omitted.append(name)
        return None
    row = {
        "calls": 0,
        "completed": 0,
        "errors": 0,
        "elapsed_ms": 0,
        "last_status": None,
        "last_time": None,
        "servers": [],
    }
    by_name[name] = row
    return row


def _tools_step(state: dict[str, Any], entry: Entry) -> None:
    data = entry.data
    if entry.type == "tool/called":
        state["calls"] += 1
        name = _as_str(data.get("name"))
        row = _tool_row(state, name)
        if row is not None:
            row["calls"] += 1
            row["last_time"] = entry.time
            server = data.get("server")
            if isinstance(server, str) and server and server not in row["servers"]:
                row["servers"].append(server)
        call_id = data.get("call_id")
        # An empty call_id is what the declaration allows when the frame carried
        # none, and it identifies NOTHING: keying the open-call map by it would
        # make every such call the same call, so one completion would close a
        # different call's frame. Those are counted and left unpaired.
        if isinstance(call_id, str) and call_id:
            state["open"][call_id] = {
                "call_id": call_id,
                "name": name,
                "turn": _as_int(data.get("turn")),
                "time": entry.time,
                "seq": entry.seq,
            }
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
        call_id = data.get("call_id")
        if isinstance(call_id, str) and call_id:
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
            }
            for name, row in sorted(state["by_name"].items())
        },
        "names_omitted": state["names_omitted"],
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
        "last": None,
    }


def _approvals_step(state: dict[str, Any], entry: Entry) -> None:
    data = entry.data
    approval_id = data.get("approval_id")
    identified = isinstance(approval_id, str) and bool(approval_id)
    if entry.type == "approval/requested":
        state["requested"] += 1
        if identified:
            state["pending"][approval_id] = {
                "approval_id": approval_id,
                "tool": _as_str(data.get("tool")),
                "reason": _as_str(data.get("reason")),
                "turn": _as_int(data.get("turn")),
                "time": entry.time,
                "seq": entry.seq,
            }
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
        "unidentified_requests": state["unidentified_requests"],
        "unmatched_decisions": state["unmatched_decisions"],
        "by_decision": dict(sorted(state["by_decision"].items())),
        "last": dict(state["last"]) if state["last"] else None,
    }


# --------------------------------------------------------------------------- #


def _as_int(value: Any) -> int:
    """*value* when it is a real int, else 0 -- a bool is not a count."""
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _as_str(value: Any) -> str:
    """*value* when it is a string, else the empty one.

    Every ``data`` field these folds read comes off bytes a reader does not
    control, so the shape is checked here rather than trusted from the type
    declaration: a declaration binds the WRITER, and a damaged or planted line is
    exactly the input that ignores it.
    """
    return value if isinstance(value, str) else ""


_FOLDS: Final[dict[str, _Fold]] = {
    "status": _Fold("status", _status_start, _status_step, _status_render),
    "usage": _Fold("usage", _usage_start, _usage_step, _usage_render),
    "timeline": _Fold("timeline", _timeline_start, _timeline_step, _timeline_render),
    "tools": _Fold("tools", _tools_start, _tools_step, _tools_render),
    "approvals": _Fold("approvals", _approvals_start, _approvals_step, _approvals_render),
}

if tuple(_FOLDS) != PROJECTION_NAMES:  # pragma: no cover - import-time consistency
    raise RuntimeError(
        "the fold registry and PROJECTION_NAMES disagree: "
        f"{tuple(_FOLDS)} against {PROJECTION_NAMES}"
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
