"""A conductor's open work, in brief, for its sidebar row: the ``crew_work`` slot field.

A conductor between turns often has nothing running while its work still moves: a
worker reported ``progress`` and parked until the next check, or a check waits on CI.
The row reads only live slot state, so without this it cannot tell that from a
conductor holding for the person, or from a crew that has gone quiet. This is the
ledger half of that answer, kept small: how many items are open, how many wait on a
person (a ``blocked`` / ``question`` whose ``reason`` is ``approval`` or
``needs_human`` and that the conductor has not answered), and for every other open
item its worker and the moment it goes stale if that worker stays idle.

That moment is the ledger's own rule: :func:`kiro_crew.work_ledger.is_stale` with the
worker's closed and looping liveness read the way the side board reads them
(``ledger_wake.worker_closed`` / ``worker_looping``). Only "is its turn running" is
left to the dashboard, which holds the live rows (``website/src/lib/crewWork.ts``).

The items come from the ``work`` slot fold the crew log already publishes on its bus,
so no read here touches disk. Only live Crew Mode roots' boards are followed
(:func:`follow`, keyed subscriptions, released by :func:`retain`), so every other
board's fold stays on demand. A board not folded yet has no entry, and its row
carries no field: the row then behaves as it did before the field existed.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any, Final

logger = logging.getLogger(__name__)

#: The slot row field this module fills.
CREW_WORK_FIELD: Final[str] = "crew_work"

#: The slot fold the summary is read from.
WORK_FOLD: Final[str] = "work"

#: Boards followed at once. A root past it is still shown, on the old reading.
CREW_WORK_FOLLOW_LIMIT: Final[int] = 64

#: Open items one summary lists. More are still counted in ``open``.
CREW_WORK_ITEM_LIMIT: Final[int] = 32


#: "Never stale" for :func:`kiro_crew.work_ledger.is_stale`: far past any window.
_FAR: Final[timedelta] = timedelta(days=3650)

_lock = threading.Lock()
#: Open items per board slot key, as the fold rendered them.
_items: dict[str, list[dict[str, Any]]] = {}
_primed: set[str] = set()
#: The newest fold revision kept per board slot key: an older publish (a prime that
#: read before a newer warm fold landed) never replaces what a newer one kept.
_revisions: dict[str, int] = {}
#: Followed board slot key -> its keyed subscriptions' disposers.
_followed: dict[str, list[Callable[[], None]]] = {}
#: Followed fold key -> the dashboard slot key its board is shown under. A channel
#: conductor's fold names its board by session key (``discord:...``), which no
#: spelling rule turns back into the slot key, so the follow records it.
_slot_of_board: dict[str, str] = {}
_listener: Callable[[str], None] | None = None


def _open_items(value: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    raw_items = value.get("items")
    if not isinstance(raw_items, list):
        return None
    from kiro_crew.work_ledger import TERMINAL_ITEM_STATES

    return [
        raw
        for raw in raw_items
        if isinstance(raw, dict) and raw.get("state") not in TERMINAL_ITEM_STATES
    ]


def summarize(
    items: Iterable[Mapping[str, Any]],
    *,
    closed: Callable[[str], bool],
    looping: Callable[[str], bool],
    now: datetime | None = None,
) -> dict[str, Any]:
    """The ``crew_work`` field for a board's open *items*.

    *closed* and *looping* answer for a worker's session key. Per listed item:
    ``worker`` (the bound session's slot key, ``""`` for a remote or unbound worker)
    and ``stale_at_ms`` (epoch ms it is stale from while its worker's turn is not
    running; ``0`` for never). Both answers come from ``is_stale``.
    """
    from kiro_crew.dashboard.handlers.work_ledger_board import answered_since_report
    from kiro_crew.session_ledger_layout import ledger_key
    from kiro_crew.work_ledger import (
        DEFAULT_STALE_WINDOW_SECS,
        WAITING_STATUSES,
        WorkItem,
        is_stale,
        parse_stamp,
        quiet_since,
    )
    from kiro_crew.work_vocab import WORK_BLOCKED_REASONS

    moment = now or datetime.now().astimezone()
    open_count = 0
    person = 0
    listed: list[dict[str, Any]] = []
    for raw in items:
        item = WorkItem.from_dict(dict(raw))
        open_count += 1
        if (
            item.status in WAITING_STATUSES
            and item.reason in WORK_BLOCKED_REASONS
            and not answered_since_report(item, list(raw.get("events") or []))
        ):
            person += 1
            continue
        if len(listed) >= CREW_WORK_ITEM_LIMIT:
            continue
        remote = bool(item.worker_instance_id)
        key = item.worker_session_key or ""
        # A remote worker's liveness is a stored record read off the loop; here it
        # reads as neither closed nor looping, so only the window can flag it.
        gone = bool(key) and not remote and closed(key)
        loops = bool(key) and not remote and looping(key)

        def stale(
            at: datetime, item: WorkItem = item, gone: bool = gone, loops: bool = loops
        ) -> bool:
            return is_stale(
                item, worker_running=False, worker_closed=gone, worker_looping=loops, now=at
            )

        if remote:
            # A peer's worker reports its own liveness to the stored record, which
            # this frame does not read: never flag it, rather than call a running
            # peer turn stalled. Its board's own badge still can.
            stale_at = 0
        elif stale(moment):
            stale_at = int(moment.timestamp() * 1000)
        elif stale(moment + _FAR):
            since = parse_stamp(quiet_since(item))
            window = timedelta(seconds=DEFAULT_STALE_WINDOW_SECS)
            stale_at = int((since + window).timestamp() * 1000) if since is not None else 0
        else:
            stale_at = 0
        listed.append({"worker": "" if remote else ledger_key(key), "stale_at_ms": stale_at})
    return {"open": open_count, "person": person, "items": listed}


def summary_for_state(slot: str, state: Any) -> dict[str, Any] | None:
    """*slot*'s summary against *state*'s live slots, or ``None`` when not folded yet."""
    with _lock:
        items = _items.get(slot)
    if items is None:
        return None
    from kiro_crew import ledger_wake

    try:
        from kiro_crew.autonudge import get_instance

        nudge = get_instance()
    except Exception:  # pragma: no cover - no loop service reads as no loop
        nudge = None
    return summarize(
        items,
        closed=lambda key: ledger_wake.worker_closed(state, key),
        looping=lambda key: ledger_wake.worker_looping(nudge, key),
    )


def remember(board: str, value: Any, revision: int = 0) -> str | None:
    """Keep *board*'s open items from its folded *value*; its slot key when they moved.

    *board* is the fold's key. The slot key is the one :func:`follow` recorded for it,
    else its ledger spelling (``dashboard:chat-7`` -> ``chat-7``). ``None`` when the
    value is malformed or the items are the ones already kept, so an unchanged board
    pushes nothing. A *revision* below the newest one kept for the slot is dropped.
    """
    if not isinstance(value, Mapping):
        return None
    from kiro_crew.session_ledger_layout import ledger_key

    with _lock:
        slot = _slot_of_board.get(board) or ledger_key(board)
    items = _open_items(value)
    if not slot or items is None:
        return None
    with _lock:
        if revision and revision < _revisions.get(slot, 0):
            return None
        if revision:
            _revisions[slot] = revision
        if _items.get(slot) == items:
            return None
        _items[slot] = items
    return slot


def set_listener(listener: Callable[[str], None] | None) -> None:
    """Who hears a followed board's slot key when its open items move.

    The dashboard's crew-log publisher, which sends the row its ``slot_patch``.
    Called on the fold worker's thread, inside the bus publish.
    """
    global _listener
    _listener = listener


def _on_fold(event: Any) -> None:
    """One ``work`` fold advance for a followed board. On the fold worker. Never raises.

    A delta event carries no whole value; the publisher's recorded base for this
    revision is read instead, inside the publish, where it is exactly this one's.
    """
    try:
        key = str(getattr(event, "key", "") or "")
        revision = int(getattr(event, "revision", 0) or 0)
        value = getattr(event, "value", None)
        if not isinstance(value, dict) and key and revision > 0:
            from kiro_crew.crew_log import eager

            value = eager.published_value(key, WORK_FOLD, revision)
        moved = remember(key, value, revision)
        listener = _listener
        if moved is not None and listener is not None:
            listener(moved)
    except Exception:
        logger.debug("crew work summary failed", exc_info=True)


def follow(slot: str, fold_keys: Iterable[str]) -> None:
    """Keep *slot*'s board current: listen to its ``work`` fold, prime it once.

    For a live Crew Mode root's row. The subscriptions are KEYED to the board's fold
    keys (its slot key and session key, the two spellings a fold may name it by), so
    only these boards' ``work`` folds are kept eager. A root with no items kept yet
    gets one background fold, which publishes on the bus and lands here; so a restart
    does not leave its row on the old reading until its ledger next moves.
    Idempotent; bounded at :data:`CREW_WORK_FOLLOW_LIMIT` boards, and :func:`retain`
    gives a closed root's place back.
    """
    from kiro_crew.crew_log import bus

    keys = list(dict.fromkeys(k for k in fold_keys if k))
    with _lock:
        if slot in _followed or len(_followed) >= CREW_WORK_FOLLOW_LIMIT or not keys:
            return
        for key in keys:
            _slot_of_board[key] = slot
        _followed[slot] = [
            bus.subscribe(
                bus.FOLD_ADVANCED, _on_fold, scope=bus.SCOPE_SLOT, key=key, fold=WORK_FOLD
            )
            for key in keys
        ]
        if slot in _primed or slot in _items:
            return
        _primed.add(slot)
    try:
        from kiro_crew.crew_log import slot_prime

        # The first key is the board's own identity (the caller's session key), the
        # spelling its ledger entries are recorded under.
        slot_prime.schedule(keys[0], WORK_FOLD)
    except Exception:  # pragma: no cover - a prime must never cost the frame
        logger.debug("crew work prime failed for %s", slot, exc_info=True)


def worker_loop_moved(worker_slot: str) -> None:
    """A worker's own loop started, stopped or changed: refresh the boards naming it.

    Its loop is what ``is_stale`` reads as ``worker_looping``, and a loop change moves
    no ledger fold, so the boards holding an open item bound to *worker_slot* are
    handed to the listener as if their items had moved. No I/O.
    """
    from kiro_crew.session_ledger_layout import ledger_key

    worker = ledger_key(worker_slot)
    if not worker:
        return
    with _lock:
        boards = [
            slot
            for slot, items in _items.items()
            if any(ledger_key(str(i.get("worker_session_key") or "")) == worker for i in items)
        ]
    listener = _listener
    for slot in boards:
        if listener is not None:
            listener(slot)


def retain(live_roots: Iterable[str]) -> None:
    """Release every followed board that is not in *live_roots*, and what it kept."""
    keep = set(live_roots)
    with _lock:
        gone = [slot for slot in _followed if slot not in keep]
        disposers = [d for slot in gone for d in _followed.pop(slot)]
        for slot in gone:
            _items.pop(slot, None)
            _revisions.pop(slot, None)
            _primed.discard(slot)
        for board in [b for b, s in _slot_of_board.items() if s in gone]:
            del _slot_of_board[board]
    for dispose in disposers:
        dispose()


def unfollow_all() -> None:
    """Drop every board subscription. Idempotent."""
    retain(())


def forget_for_tests() -> None:
    """Drop every kept item list, subscription and prime mark. A test seam."""
    unfollow_all()
    set_listener(None)
    with _lock:
        _items.clear()
        _revisions.clear()
        _primed.clear()
        _slot_of_board.clear()
