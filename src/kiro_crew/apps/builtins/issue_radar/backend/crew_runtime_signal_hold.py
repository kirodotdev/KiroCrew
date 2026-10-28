"""An item signal stays raised until the crew records a step on that item.

The sweep fingerprints each due item and raises a signal (CI, review, reply, conflict,
merge, unblocked) when the fingerprint moved. A started turn is not proof the signal
was handled: the brief advances one item per turn, and a turn can fail partway. So a
signalled item's mark keeps its OLD fingerprint, with the new one beside it as
``pending``, and the new one is committed only once a turn carrying the signal has
started and the item's ledger shows a step newer than that start (its
``last_progress_at``, or any event recorded on it).
Until then the next sweep that reads the item raises the same signal again, at the
item's phase cadence (``RECHECK_SEC``), rather than waiting for the day-long idle
backstop.

The bound: a pending signal is carried to at most :data:`SIGNAL_TURNS` started turns
that record no step on its item. After that its fingerprint is committed and it
wakes nobody again, so a crew that looked and chose to do nothing is not paid for
again. A wake that started no turn (the crew was mid-turn) does not count, and
neither does a turn that recorded a step on another item it carried
(:func:`credit_turn`): that crew did not choose nothing. While the crew is still mid
the last charged turn and that turn carried other items, the bound waits for it
(:func:`resolve_mark`'s *busy*). A
new signal on the item restarts the count, because that is new information; a field
no signal reads moving under it does not.

A mark is ``{"fp": committed, "checked_at": t}`` or, while a signal is held,
``{"fp": committed, "checked_at": t, "pending": {"fp": newest, "at": t0, "turns": n}}``,
plus ``"last": {"at": t1, "with": [numbers]}`` after a turn that carried other items.
A pending entry of another shape reads as absent.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import crew_store

logger = logging.getLogger("kirocrew.app.issue-radar")

#: The most started turns a held signal is raised to without the crew recording a
#: step on its item.
SIGNAL_TURNS = 2

_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def _pending(entry: Any) -> dict[str, Any] | None:
    held = entry.get("pending") if isinstance(entry, Mapping) else None
    if not isinstance(held, Mapping) or not isinstance(held.get("fp"), dict):
        return None
    at, turns = held.get("at"), held.get("turns")
    if not isinstance(at, (int, float)) or isinstance(at, bool):
        return None
    if not isinstance(turns, int) or isinstance(turns, bool):
        return None
    out = {"fp": held["fp"], "at": float(at), "turns": turns}
    if isinstance(held.get("last"), Mapping):
        out["last"] = dict(held["last"])
    return out


def _committed(entry: Any) -> dict[str, Any] | None:
    fp = entry.get("fp") if isinstance(entry, Mapping) else None
    return fp if isinstance(fp, dict) else None


def is_held(entry: Any) -> bool:
    """Whether *entry* carries a signal the crew has not stepped on yet."""
    return _pending(entry) is not None


def fingerprint_basis(entry: Any) -> dict[str, Any] | None:
    """The fingerprint the next read builds on: the newest one observed.

    ``fingerprint_item`` carries values forward from it (a review verdict when the PR
    did not move, the blocker count when the graph is unknown), so it is the newest
    reading, not the committed one the signals are compared against.
    """
    held = _pending(entry)
    return held["fp"] if held is not None else _committed(entry)


def stamp_secs(text: Any) -> float | None:
    """A crew-log stamp (``2026-11-09T12:52:30.270000Z``) as epoch seconds."""
    if not isinstance(text, str) or not text:
        return None
    try:
        moment = datetime.strptime(text, _STAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return moment.timestamp()


def last_steps(ledger: Mapping[str, Any]) -> dict[int, float]:
    """Per issue number, the newest step the crew recorded: progress or any event."""
    out: dict[int, float] = {}

    def note(number: Any, stamp: Any) -> None:
        secs = stamp_secs(stamp)
        if isinstance(number, int) and not isinstance(number, bool) and secs is not None:
            out[number] = max(out.get(number, secs), secs)

    for item in ledger.get("items") or []:
        if isinstance(item, Mapping):
            note(item.get("number"), item.get("last_progress_at"))
    for event in ledger.get("events") or []:
        if isinstance(event, Mapping):
            note(event.get("number"), event.get("ts"))
    return out


def read_steps(owner: str, repo: str, crew_id: str, root: Path | None) -> dict[int, float]:
    """The crew's newest step per issue (:func:`last_steps`). An unreadable ledger is
    no step, which keeps a held signal raised; :data:`SIGNAL_TURNS` still bounds it."""
    try:
        return last_steps(crew_store.read_ledger(owner, repo, crew_id, root))
    except Exception:
        logger.debug("issue-radar: reading crew %s steps failed", crew_id, exc_info=True)
        return {}


def resolve_mark(
    entry: Any,
    fp: dict[str, Any],
    now: float,
    last_step: float | None,
    detect: Callable[[dict[str, Any] | None, dict[str, Any]], list[str]],
    busy: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    """The mark to store for an item read this sweep, and the signals to raise.

    *busy*: the crew's slot is mid-turn. The bound then waits while the last charged
    turn carried other items and has stepped on none yet: it may still be running,
    and a step it lands on one of them refunds this charge (:func:`credit_turn`).
    Nothing is raised meanwhile, since a wake would only be dropped."""
    prev = _committed(entry)
    held = _pending(entry)
    if held is not None and held["turns"] > 0 and last_step is not None and last_step > held["at"]:
        # A turn carrying the signal started at ``at``, and the crew recorded a step
        # on the item after it: the signal reached it.
        prev, held = held["fp"], None
    signals = detect(prev, fp)
    if not signals:
        return {"fp": fp, "checked_at": now}, []
    # Only a new SIGNAL restarts the count: a field no signal reads (a push, a bot
    # comment, the merge state) moving under a held signal is not new information.
    same = held is not None and not detect(held["fp"], fp)
    turns = held["turns"] if held is not None and same else 0
    at = held["at"] if held is not None and same else now
    pending: dict[str, Any] = {"fp": fp, "at": at, "turns": turns}
    if held is not None and same and "last" in held:
        pending["last"] = held["last"]
    if turns >= SIGNAL_TURNS:
        if not (busy and "last" in pending):
            return {"fp": fp, "checked_at": now}, []
        return {"fp": prev, "checked_at": now, "pending": pending}, []
    return {"fp": prev, "checked_at": now, "pending": pending}, signals


def charge_turns(
    stored: dict[str, Any],
    raised: Mapping[str, list[tuple[str, Any]]],
    started: Mapping[str, float],
) -> bool:
    """Charge each raised item for the turn its crew's wake started at ``started[crew]``,
    taken right before that crew's dispatch (an earlier stamp would let a step the
    previous turn wrote during the wake pass for handling this signal). Returns
    whether anything was charged."""
    charged = False
    for cid, keys in raised.items():
        if cid not in started:
            continue
        for ikey, _number in keys:
            count_turn(stored[ikey], started[cid], [n for k, n in keys if k != ikey])
            charged = True
    return charged


def count_turn(mark: Any, started_at: float, carried: Iterable[int] = ()) -> None:
    """Record that a turn carrying *mark*'s held signal started at *started_at*.

    Only a step newer than that counts as handling it, so a step the turn already
    running when the signal was seen wrote, unaware of it, does not. *carried* is the
    other items whose signals rode the same turn: a step on one of them refunds this
    charge (:func:`credit_turn`).
    """
    held = mark.get("pending") if isinstance(mark, dict) else None
    if isinstance(held, dict) and isinstance(held.get("turns"), int):
        if held["turns"] == 0:
            # The FIRST carrying turn's start: a slot runs one turn at a time, so any
            # turn running when the signal was seen has ended by then, and a later
            # carrying turn must not hide a step the first one wrote.
            held["at"] = started_at
        held["turns"] += 1
        others = sorted({n for n in carried if isinstance(n, int) and not isinstance(n, bool)})
        if others:
            held["last"] = {"at": started_at, "with": others}
        else:
            held.pop("last", None)


def credit_turn(entry: Any, steps: Mapping[int, float], number: Any) -> None:
    """Refund, in place, the last charge on *entry*'s held signal (item *number*)
    when that turn recorded a step on another item it carried.

    The brief advances one item per turn, so a turn that advanced a sibling did not
    choose to do nothing, and charging it would commit this signal unhandled after
    :data:`SIGNAL_TURNS` busy turns. A turn that stepped on none of its items stays
    charged, which is what still bounds a crew that looks and does nothing. The
    record of that turn is kept until it is refunded or a later turn replaces it,
    so a turn still running at the next sweep is credited once its step lands.
    """
    held = entry.get("pending") if isinstance(entry, dict) else None
    if not isinstance(held, dict):
        return
    last, turns, since = held.get("last"), held.get("turns"), held.get("at")
    if not isinstance(last, Mapping) or not isinstance(turns, int):
        return
    own = steps.get(number) if isinstance(number, int) else None
    if own is not None and isinstance(since, (int, float)) and own > since:
        # The item has its own step: resolve_mark takes it as handled, which a
        # refund to no turns would hide.
        return
    at, others = last.get("at"), last.get("with")
    if not isinstance(at, (int, float)) or isinstance(at, bool) or not isinstance(others, list):
        return
    if any(steps.get(n, float("-inf")) >= at for n in others if isinstance(n, int)):
        held["turns"] = max(0, turns - 1)
        held.pop("last", None)
