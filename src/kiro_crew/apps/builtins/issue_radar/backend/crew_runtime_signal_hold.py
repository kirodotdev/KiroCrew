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

A first look at an item in a waiting phase (no mark yet, or a mark taken while the
item was in an acting phase) is compared with :func:`first_look_basis` rather than
seeded silently, so an outcome that settled before the watcher first read the item,
or while the crew was still acting on it (CI finished, a verdict, a conflict, a
merge), is raised like any other.

A started turn is charged when it is dispatched, but it still waits for a
background-turn permit, and a permit timeout, a cancel or a restart can end it before
the model reads the prompt. So the sweep carries each item-signal reason in
``crew_wake_inflight`` beside asks and pickups. A turn that hands its reason back is
refunded (:func:`return_turns`), and while a turn still carries the reason the signal
is neither raised again nor committed (:func:`resolve_mark`'s *waiting*).

A mark is ``{"fp": committed, "checked_at": t}`` or, while a signal is held,
``{"fp": committed, "checked_at": t, "pending": {"fp": newest, "at": t0, "turns": n}}``,
plus ``"last": {"at": t1, "with": [numbers]}`` after a turn that carried other items.
A pending entry of another shape reads as absent.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import crew_model, crew_store

logger = logging.getLogger("kirocrew.app.issue-radar")

#: The most started turns a held signal is raised to without the crew recording a
#: step on its item.
SIGNAL_TURNS = 2

_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"

#: How often an item in each phase is re-read. The forge is the mover for some
#: phases and the crew is the mover for others, and paying a CI-grade cadence for
#: a phase waiting on a human is pure rate limit. ``selected`` is pre-claim and
#: purely local — there is nothing public to watch yet.
RECHECK_SEC = {
    "awaiting-ci": 60,
    "addressing-review": 60,
    "awaiting-merge": 120,
    "claimed": 300,
    "investigating": 300,
    "implementing": 300,
    "awaiting-reply": 300,
}
_DEFAULT_RECHECK_SEC = 300


def _is_due(item: dict[str, Any], stored: dict[str, Any] | None, now: float) -> bool:
    """Whether this item's phase is due for a re-read on this tick."""
    phase = str(item.get("phase") or "")
    if phase == "selected":
        return False  # pre-claim, local only — nothing public to watch
    if not stored:
        return True
    interval = RECHECK_SEC.get(phase, _DEFAULT_RECHECK_SEC)
    last = stored.get("checked_at")
    if not isinstance(last, (int, float)):
        return True
    # A mark in the FUTURE is due immediately. The mark is wall-clock, so a clock
    # correction (or a machine that resumed with a bad clock) can leave one ahead
    # of now — and an unconditional ``elapsed >= interval`` would then park that
    # item forever, which is the exact silent stall this sweep exists to prevent.
    if now < float(last):
        return True
    return (now - float(last)) >= interval


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


def first_look_basis(fp: Mapping[str, Any]) -> dict[str, Any]:
    """The fingerprint *fp* is compared with on a first look at a waiting item: the
    same reading with every settled outcome taken back to "not yet". CI that finished,
    a review verdict, a conflict and a merge then raise their signal, while running
    CI, comment counts and blockers seed as they are: nothing says they are news.
    """
    basis = dict(fp)
    if fp.get("checks") in ("success", "failure"):  # an outcome, not work still running
        basis["checks"], basis["check_counts"] = "running", None
    basis["review_decision"] = ""
    basis["conflicted"] = False
    basis["merged"] = False
    return basis


def resolve_mark(
    entry: Any,
    fp: dict[str, Any],
    now: float,
    last_step: float | None,
    detect: Callable[[dict[str, Any] | None, dict[str, Any]], list[str]],
    busy: bool = False,
    waiting: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    """The mark to store for an item read this sweep, and the signals to raise.

    *busy*: the crew's slot is mid-turn. The bound then waits while the last charged
    turn carried other items and has stepped on none yet: it may still be running,
    and a step it lands on one of them refunds this charge (:func:`credit_turn`).
    Nothing is raised meanwhile, since a wake would only be dropped.

    *waiting*: a turn carrying this item's signal has not reached the model yet, nor
    handed it back. The signal is kept as it is and raised to nobody: that turn
    carries it, and :func:`return_turns` refunds it if the turn never runs."""
    prev = _committed(entry)
    held = _pending(entry)
    acting = crew_model.ACTING_PHASES
    if fp.get("phase") not in acting and (prev is None or prev.get("phase") in acting):
        # A first look at a waiting item, or the first since the crew parked it: compare
        # it with "nothing has happened yet" rather than seeding silently, so an outcome
        # that settled before then (CI that went green while it was implementing) still
        # reaches the crew. Its loop gap is the day-long backstop and a full crew gets
        # no pickup, so nothing else would.
        prev = first_look_basis(fp if prev is None else prev)
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
    if waiting and held is not None and same:
        return {"fp": prev, "checked_at": now, "pending": {**held, "fp": fp}}, []
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


def item_reason_number(reason: str, signals: Collection[str]) -> int | None:
    """The item a wake reason raises a held signal for (``#<number> ci-changed,
    review-verdict``, every signal in *signals*), or None for any other reason."""
    head, _, tail = reason.partition(" ")
    if not head.startswith("#") or not head[1:].isdigit() or not tail:
        return None
    return int(head[1:]) if all(s in signals for s in tail.split(", ")) else None


def return_turn(entry: Any) -> None:
    """Refund, in place, one charge on *entry*'s held signal: the turn it was charged
    for ended before the model read its prompt."""
    held = entry.get("pending") if isinstance(entry, dict) else None
    if isinstance(held, dict) and isinstance(held.get("turns"), int):
        held["turns"] = max(0, held["turns"] - 1)
        held.pop("last", None)


def return_turns(
    owner: str,
    repo: str,
    root: Path | None,
    stored: dict[str, Any],
    signals: Collection[str],
    item_key: Callable[[str, int], str],
) -> tuple[dict[str, list[str]], dict[str, set[int]]]:
    """Refund in *stored* every charge whose turn handed its item-signal reason back.

    Returns ``(returned, waiting)``: per crew, the reasons refunded, which the caller
    removes from the in-flight file once *stored* is saved (:func:`settle_returned`),
    and the items a turn still carries (:func:`resolve_mark`'s *waiting*). An
    unreadable in-flight file refunds nothing and holds nothing.
    """
    # Lazy import: a module-level one would be mirrored through the facade.
    from . import crew_wake_inflight

    try:
        view = crew_wake_inflight.turns(owner, repo, root)
    except OSError:
        logger.warning("issue-radar: reading carried item signals failed", exc_info=True)
        return {}, {}
    returned: dict[str, list[str]] = {}
    waiting: dict[str, set[int]] = {}
    for cid, (back, live) in view.items():
        for reason in back:
            if (number := item_reason_number(reason, signals)) is not None:
                return_turn(stored.get(item_key(cid, number)))
                returned.setdefault(cid, []).append(reason)
        numbers = {n for r in live if (n := item_reason_number(r, signals)) is not None}
        if numbers:
            waiting[cid] = numbers
    return returned, waiting


def settle_returned(owner: str, repo: str, root: Path | None, returned: Mapping[str, Any]) -> None:
    """Forget the item-signal reasons :func:`return_turns` refunded, once saved."""
    from . import crew_wake_inflight

    for cid, reasons in returned.items():
        try:
            crew_wake_inflight.settle(owner, repo, root, cid, list(reasons))
        except OSError:
            # Left in place, the next sweep refunds it again: one more wake, never a lost one.
            logger.warning("issue-radar: could not forget returned item signals", exc_info=True)
