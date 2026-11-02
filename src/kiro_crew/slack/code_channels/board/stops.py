"""Who stopped a worker from the Workers tab, until that worker's next turn starts.

:mod:`..worker_stop` notes a stop it carried out; the board's one read path
(:mod:`.source`) asks :func:`current` for each worker row, so the board and the
Workers tab both show "Stopped by @user · HH:MM" from the same snapshot, through
the same refresh. The note lives in this process only and holds while the
worker's slot is the SAME slot object, still on the turn generation that was
stopped: the next turn (a new generation) clears it, and so do the slot going
away and a new slot under the same key (a resumed worker, whose turns count
from 1 again). A note whose row is never read again is dropped at the cap.

Only a Slack user id (the clicker's, from the verified payload) and a clock time
are kept: never a session key, a command or any transcript text.
"""

from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass, field
from typing import Any, TypeVar

_S = TypeVar("_S")


def _is_user_id(value: str) -> bool:
    """A Slack user id, the only form that becomes a mention: ``U`` or ``W`` and
    2 to 30 ASCII capitals and digits, checked character by character (nothing
    trailing, not even a newline)."""
    rest = value[1:]
    return value[:1] in ("U", "W") and 2 <= len(rest) <= 30 and all(c in _ID_CHARS for c in rest)


_ID_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
_MAX_TRACKED = 4096


@dataclass(frozen=True)
class Stop:
    user: str
    #: ``HH:MM`` UTC, when the stop was carried out.
    at: str
    #: The worker slot's turn generation that was stopped.
    generation: int
    #: The slot object itself: the stop holds only while THIS slot is the one
    #: under the key. A resumed worker gets a new slot whose turns count from 1
    #: again, so a generation alone could match a turn nobody stopped. Held
    #: strongly (slots take no weak reference) until the note is read stale.
    slot: Any = field(default=None, compare=False, repr=False)


def _same_turn(stop: Stop, slot: Any, generation: int) -> bool:
    """Whether *stop* was noted for *slot* (the same object) and its turn *generation*."""
    return stop.slot is slot and stop.generation == generation


#: ``worker slot key -> Stop``.
_stops: dict[str, Stop] = {}
#: Worker slot keys whose stop has not been published yet: the next board and
#: Workers-tab publish skip their edit gap once, so it shows within seconds.
_urgent: set[str] = set()


def take_urgent(worker_keys: Any) -> bool:
    """Whether any of *worker_keys* has an unpublished stop; consumes those marks."""
    hit = {str(k) for k in worker_keys or () if k} & _urgent
    _urgent.difference_update(hit)
    return bool(hit)


def generation_of(slot: Any) -> int:
    """*slot*'s turn generation now: read it BEFORE awaiting the stop."""
    value = getattr(slot, "_turn_generation", 0)
    return value if isinstance(value, int) else 0


def note(slot: Any, user: str, *, generation: int) -> None:
    """Record that *user* stopped *slot*'s turn *generation* (a well-formed user id only).

    *generation* is the turn the stop was aimed at, read before any await: the
    slot may already be on its next (queued) turn by the time the stop returns,
    and that turn was stopped by nobody. A note for a generation the slot has
    moved past is not kept.
    """
    key = str(getattr(slot, "key", "") or "")
    if not key or not _is_user_id(user or ""):
        return
    if generation_of(slot) != generation:
        # Moved on: nothing to note. A note for a LATER turn (another Stop that
        # landed meanwhile) is that turn's and stays.
        stale = _stops.get(key)
        if stale is not None and _same_turn(stale, slot, generation):
            _forget(key)
        return
    if len(_stops) > _MAX_TRACKED:
        _stops.clear()
        _urgent.clear()
    _stops[key] = Stop(
        user=user,
        at=time.strftime("%H:%M", time.gmtime()),
        generation=generation,
        slot=slot,
    )
    _urgent.add(key)


def _forget(*keys: str) -> None:
    for key in keys:
        _stops.pop(key, None)
        _urgent.discard(key)  # an unpublished mark grants no edit-gap skip either


def current(state: Any, worker_key: str) -> Stop | None:
    """The stop still showing for *worker_key*, or ``None`` (and forgotten) once its
    slot started a new turn, is gone, or is another slot under the same key (a
    resumed worker)."""
    from kiro_crew.slack.code_channels.board.source import find_slot

    if not worker_key:
        return None
    slot = find_slot(state, worker_key)
    if slot is None:
        # The slot is gone (closed, or not open yet after a restart): its stop is
        # history, and a slot resumed under this key later was stopped by nobody.
        _forget(worker_key, f"dashboard_{worker_key}", worker_key.removeprefix("dashboard_"))
        return None
    key = str(getattr(slot, "key", "") or "")
    stop = _stops.get(key) if key else None
    if stop is None:
        _urgent.discard(key)  # no note: no edit-gap skip on its behalf either
        return None
    if not _same_turn(stop, slot, generation_of(slot)):
        _forget(key)  # a new turn, or a new slot: the stop is history
        return None
    return stop


def reconcile(snapshot: _S, state: Any = None) -> _S:
    """*snapshot* with every "Stopped by" overlay set to the stop showing NOW.

    A snapshot is read before the awaits of a publish, and in between a queued
    turn can start (stopped by nobody: its overlay is removed) or another Stop
    can land (its user and time replace the older ones, or are added to a row
    read before it). Synchronous: called right before each render (and before
    an urgent mark is taken), after the publish's awaits.
    """
    rows = tuple(getattr(snapshot, "rows", ()) or ())
    if not _stops and not any(getattr(r, "stopped_by", "") for r in rows):
        return snapshot
    if state is None:
        from kiro_crew.slack.code_channels import surface

        state = surface.dashboard_state()
    kept = []
    changed = False
    for row in rows:
        stop = current(state, row.session_key) if state is not None else None
        user, at = (stop.user, stop.at) if stop is not None else ("", "")
        if (row.stopped_by, row.stopped_at) != (user, at):
            row = dataclasses.replace(row, stopped_by=user, stopped_at=at)
            changed = True
        kept.append(row)
    if not changed:
        return snapshot
    return dataclasses.replace(snapshot, rows=tuple(kept))  # type: ignore[type-var]


def reset_for_tests() -> None:
    _stops.clear()
    _urgent.clear()
