"""The pure fold API over the registry: a fold's resumable position (:class:`Checkpoint`),
starting one (``initial``), continuing it over entries (``advance``), rendering it
(``projection_of``) and the whole-file form (``fold``).
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from kiro_crew.crew_log.errors import CODE_BAD_DATA, CrewLogError
from kiro_crew.crew_log.projection_catalogue import FOLD_NAMES, Projection
from kiro_crew.crew_log.projection_registry import _FOLDS
from kiro_crew.crew_log.schema import Entry


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
    it back on resume, and the fold registry beside it (``projection_registry``) holds
    the number. One accessor rather than a second copy of the table, so a bump lands in
    one place.
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


def state_is_serializable(checkpoint: Checkpoint) -> bool:
    """Whether *checkpoint*'s state survives a JSON round trip unchanged.

    A checkpoint is only resumable if it can be written down, so this is the
    property a caller storing one checks rather than assumes.
    """
    try:
        return json.loads(json.dumps(checkpoint.state)) == checkpoint.state
    except (TypeError, ValueError):
        return False
