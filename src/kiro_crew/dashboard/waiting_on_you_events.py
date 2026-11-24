"""What follows a change to the waiting-on-you store, and what changes it besides a route.

Every change is told the same way once it is on disk: one display-only ``card`` row
(surface ``waiting``) in the slot's transcript, so the history shows when an ask came
and went, and the ordinary slots push, which carries the new ``waiting_on_you`` list.
There is no frame of its own: the slot's ``waiting_on_you`` goes out as a
``slot_patch``, and in every full ``slots`` list.

Besides the routes, four things change the store:

* a live row the OWNER typed (``meta.human``, authored by the owner). A card answer is
  one: the card sends its label or note through the ordinary chat send with a claim
  naming its ask (``meta.waitingOnYouAnswer``). When the row is written, a claim that
  names a live ask stamps the reserved ``meta.waitingOnYou`` on it and retires only
  that ask. Any other owner row retires every ask, because the server cannot tell
  which one it answers;
* an edit-resend, which is the owner writing, retires every ask;
* deleting a session removes its records (:func:`forget_deleted_session`, from the
  delete routes' shared teardown, so one delete and "Delete all" both reach it);
* the expiry sweep, every :data:`~kiro_crew.dashboard.waiting_on_you.SWEEP_INTERVAL_SECONDS`.

A rewind or regenerate leaves the asks pinned: the agent still sees them in its block
and clears any that has stopped applying. The agent hears of every change through
:func:`turn_block`, once per retired record.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from typing import Any, Awaitable, Callable, Coroutine

from aiohttp import web

from kiro_crew.dashboard import waiting_on_you as store_mod
from kiro_crew.waiting_on_you_contract import ASK_ID_RE, valid_key

logger = logging.getLogger(__name__)

#: Row-meta key of a row that answered an ask: ``{key, ask_id}``, checked against the
#: live asks when the row was written. Reserved (``chat_handlers.RESERVED_ROW_META_KEYS``),
#: so only :func:`on_live_user_row` writes it.
WAITING_ON_YOU_META_KEY = "waitingOnYou"
#: Send-meta key a card answer's claim rides in: ``{key, ask_id}``. Any caller may
#: write it, so it is only a claim until the row is written.
ANSWER_CLAIM_META_KEY = "waitingOnYouAnswer"
#: Queue-entry meta key: the ids of the asks pinned when the owner sent a message that
#: then waited in the queue. Its row is written at drain, after asks pinned since, and
#: retires only these. Reserved (``chat_handlers.RESERVED_ROW_META_KEYS``).
SHOWN_ASKS_META_KEY = "waitingOnYouShown"
#: Queue-entry meta key: the owner replaced this entry's words, so its row is the owner
#: writing whoever queued it first. Reserved (``chat_handlers.RESERVED_ROW_META_KEYS``).
OWNER_EDIT_META_KEY = "waitingOnYouOwnerEdit"
#: (slot key, ask id) of answers whose row is written but whose retirement is still
#: being saved. Set and read on the event loop with no await between, so a second
#: answer to the same ask sees the first as soon as its row lands.
_ANSWERING: set[tuple[str, str]] = set()
#: Message retirements whose write failed, by slot: each the ``(keep, only)`` it was
#: scheduled with, so the sweep retries exactly what the message retired and never
#: an ask pinned after it. ``only`` is ``None`` only when nothing knew what was shown.
_RETIRE_RETRIES: dict[str, list[tuple[frozenset[str], "Shown"]]] = {}
#: Deleted sessions whose records could not be removed yet; the sweep retries them.
_FORGET_RETRIES: set[str] = set()


def answer_claim(user_meta: Any) -> dict[str, Any]:
    """``{waitingOnYouAnswer: {key, ask_id}}`` from a send's meta when well formed, else ``{}``.

    The queue and the sub-agent hold carry it to the row the drain writes.
    """
    claim = user_meta.get(ANSWER_CLAIM_META_KEY) if isinstance(user_meta, dict) else None
    if not isinstance(claim, dict):
        return {}
    key, ask_id = claim.get("key"), claim.get("ask_id")
    if not valid_key(key) or not isinstance(ask_id, str) or not ASK_ID_RE.fullmatch(ask_id):
        return {}
    return {ANSWER_CLAIM_META_KEY: {"key": key, "ask_id": ask_id}}


#: The status a ``waiting`` card row records, by what happened.
ROW_PINNED = "pinned"
ROW_ANSWERED = "answered"
ROW_DISMISSED = "dismissed"
ROW_CLEARED = "cleared"
ROW_EXPIRED = "expired"
ROW_RETIRED = "retired"

_ROW_VERBS = {
    ROW_PINNED: "Pinned",
    ROW_ANSWERED: "Answered",
    ROW_DISMISSED: "Dismissed",
    ROW_CLEARED: "Cleared",
    ROW_EXPIRED: "Expired",
    ROW_RETIRED: "Closed",
}

_SWEEP_TASKS: set[asyncio.Task[None]] = set()
_RETIRE_TASKS: set[asyncio.Task[None]] = set()
#: The slot each scheduled retirement acts on; an entry leaves when its task ends.
_RETIRE_TASK_SLOTS: dict[asyncio.Task[None], str] = {}
#: How long a turn waits for its slot's scheduled retirements before composing.
RETIREMENT_WAIT_SECONDS = 5.0


async def publish(state: Any, slot_key: str, status: str, asks: list[dict[str, Any]]) -> None:
    """Record *asks* as one ``waiting`` row each and push the slots. Never raises."""
    if asks:
        try:
            from kiro_crew.dashboard.chat_cards import load_conversation_window, record_waiting

            await load_conversation_window(state, {"slot_key": slot_key})
            for ask in asks:
                record_waiting(state, slot_key, status, _ROW_VERBS[status], ask)
        except Exception:
            logger.warning("could not record a waiting-on-you row", exc_info=True)
    push(state, slot_key)


def push(state: Any, slot_key: str) -> None:
    """Tell every tab: a ``slot_patch`` of the slot's ``waiting_on_you`` and
    ``needs_full_chat`` (a pinned ask is one of the waits that flag reports), which also
    sends the full list to a tab that cannot apply a patch. Never raises."""
    try:
        patcher = getattr(state, "push_slot_patch", None)
        if callable(patcher):
            patcher(slot_key, ("waiting_on_you", "needs_full_chat"))
            return
        pusher = getattr(state, "push_slots_update", None)
        if callable(pusher):
            pusher()
    except Exception:
        logger.debug("slots push after a waiting-on-you change failed", exc_info=True)


def retires_asks(meta: dict[str, Any] | None) -> bool:
    """Whether a live user row with *meta* is the owner writing in the chat.

    Only a row a person typed (``meta.human``) whose server-stamped author is the
    owner. A teammate's message, a channel row with an unknown sender and every
    machine row (no ``meta.human``) retire nothing.
    """
    from kiro_crew.history_schema import HUMAN_TURN_META_KEY

    if not isinstance(meta, dict) or meta.get(HUMAN_TURN_META_KEY) is not True:
        return False
    from kiro_crew.shared_crew.attribution import message_author
    from kiro_crew.shared_crew.owner import is_owner_principal

    return is_owner_principal(message_author(meta))


def _live_claim(
    store: store_mod.WaitingStore, slot_key: str, claim: Any, state: Any = None
) -> dict[str, str]:
    """``{key, ask_id}`` when *claim* names an ask pinned on *slot_key* now, else ``{}``.

    An ask a written row already answered is not live, even when its retirement did
    not land: the row's stamp says so, and it outlives a restart that drops
    ``_ANSWERING``.
    """
    named = answer_claim({ANSWER_CLAIM_META_KEY: claim}).get(ANSWER_CLAIM_META_KEY)
    if not named or (slot_key, named["ask_id"]) in _ANSWERING:
        return {}
    if named["ask_id"] in _stamped_answers(state, slot_key):
        return {}
    for live in store.asks(slot_key):
        if live["key"] == named["key"] and live["ask_id"] == named["ask_id"]:
            return dict(named)
    return {}


def _stamped_answers(state: Any, slot_key: str) -> frozenset[str]:
    """Ask ids a written person row on *slot_key* answered (``meta.waitingOnYou``).

    The stamp is reserved and written only by :func:`on_live_user_row`, so it is the
    durable record that an answer was consumed. A ``gone`` stamp answered nothing.
    A restored tab whose window is still on disk is not read: reading it here would
    load the transcript on the event loop. The sweep loads it off the loop first.
    """
    slot = (getattr(state, "_slots", None) or {}).get(slot_key) if state is not None else None
    if getattr(slot, "window_pending", False) is True:
        return frozenset()
    ids: set[str] = set()
    for row in list(getattr(slot, "messages", None) or []):
        if not isinstance(row, dict) or row.get("role") != "user":
            continue
        stamp = (row.get("meta") or {}).get(WAITING_ON_YOU_META_KEY)
        if isinstance(stamp, dict) and isinstance(stamp.get("ask_id"), str):
            if stamp.get("gone") is not True:
                ids.add(stamp["ask_id"])
    return frozenset(ids)


def stale_answer_reason(state: Any, slot: Any, user_meta: Any) -> str:
    """Why a card answer send must be refused before it is sent or queued, or ''.

    A card answer whose ask is not pinned, or whose ask already has an answer
    waiting in the queue (another window answered first), would reach the agent as a
    bare "Proceed" or "Done" naming nothing, which the agent could act on twice. The
    check runs on the event loop before the send is admitted, so two sends cannot
    both pass it. The caller loads the store first (:func:`warm_store`); one still
    not loaded refuses the answer, since nothing can say which ask it names.
    """
    named = answer_claim(user_meta).get(ANSWER_CLAIM_META_KEY)
    if not named or state is None:
        return ""
    store = store_mod.store_for(state)
    slot_key = str(getattr(slot, "key", "") or "")
    if not store.loaded:
        return "the asks are still loading; try again"
    if not _live_claim(store, slot_key, named, state):
        return "this ask is no longer pinned"
    for item in list(getattr(slot, "_queue", None) or []):
        queued = answer_claim(item.get("meta") if isinstance(item, dict) else None)
        if queued.get(ANSWER_CLAIM_META_KEY, {}).get("ask_id") == named["ask_id"]:
            return "an answer to this ask is already queued"
    return ""


async def warm_store(state: Any) -> None:
    """Load the asks before a card answer is checked against them."""
    if state is not None:
        await store_mod.store_for(state).warm()


#: (slot key, ask id) of card answers a send request is carrying right now, from the
#: first check until the request returns. Taken with no await after the check, so of
#: two sends answering one ask only the first proceeds, whatever path each then takes
#: (sent now, queued, held). Once the request returns, the queue entry or
#: ``_ANSWERING`` (the row was written) carries the answer instead.
_IN_FLIGHT: set[tuple[str, str]] = set()
#: Where a send request keeps what :func:`reserve_answer` took, for its release.
RESERVATION_REQUEST_KEY = "waiting_on_you_reservation"


def reserve_answer(slot: Any, user_meta: Any) -> tuple[str, str] | None:
    """Take *user_meta*'s claimed ask for this request; None when another send holds it."""
    named = answer_claim(user_meta).get(ANSWER_CLAIM_META_KEY)
    if not named:
        return None
    reservation = (str(getattr(slot, "key", "") or ""), named["ask_id"])
    if reservation in _IN_FLIGHT:
        return None
    _IN_FLIGHT.add(reservation)
    return reservation


async def admit_answer(
    request: web.Request, state: Any, slot: Any, user_meta: Any
) -> web.Response | None:
    """Admit a card answer for this send request, or the 409 that refuses it.

    The store is loaded, the claim checked against the pinned asks and the queue,
    and the ask reserved for the request with no await after the check, so of two
    sends answering one ask only the first proceeds, whichever path either takes.
    ``api_chat`` gives the reservation back when the request returns.
    """
    await warm_store(state)
    if stale := stale_answer_reason(state, slot, user_meta):
        return web.json_response({"error": stale, "code": "waiting_on_you_stale"}, status=409)
    reservation = reserve_answer(slot, user_meta)
    if reservation is None:
        return web.json_response(
            {
                "error": "an answer to this ask is already being sent",
                "code": "waiting_on_you_stale",
            },
            status=409,
        )
    request[RESERVATION_REQUEST_KEY] = reservation
    return None


def release_answer(reservation: tuple[str, str] | None) -> None:
    """Give back what :func:`reserve_answer` took, when its request returns."""
    if reservation is not None:
        _IN_FLIGHT.discard(reservation)


def on_live_user_row(state: Any, slot_key: str, meta: dict[str, Any] | None) -> Any:
    """The live-append hook: what the user row about to be written does to the asks.

    Returns the row's meta. The claim never stays on the row: an owner row whose claim
    names a live ask is stamped ``meta.waitingOnYou`` and retires that ask alone; an
    owner row whose well-formed claim names an ask already gone retires nothing; any
    other owner row retires every ask pinned when it was sent (``SHOWN_ASKS_META_KEY``
    for a row the queue held). A person's row whose queued words the owner replaced
    counts as the owner's. Rows by anyone else retire nothing. The check
    reads memory, so the stamp is on the row as it is written; the store change is
    scheduled, written, then told.
    """
    if not isinstance(meta, dict):
        return meta
    claim = shown_at_send = None
    owner_edited = False
    reserved = {
        ANSWER_CLAIM_META_KEY,
        WAITING_ON_YOU_META_KEY,
        SHOWN_ASKS_META_KEY,
        OWNER_EDIT_META_KEY,
    }
    if reserved & meta.keys():
        meta = dict(meta)
        claim = meta.pop(ANSWER_CLAIM_META_KEY, None)
        meta.pop(WAITING_ON_YOU_META_KEY, None)
        shown_at_send = _parse_shown(meta.pop(SHOWN_ASKS_META_KEY, None))
        owner_edited = meta.pop(OWNER_EDIT_META_KEY, None) is True
    if state is None or not (retires_asks(meta) or (owner_edited and _person_row(meta))):
        return meta
    store = store_mod.store_for(state)
    answered = _live_claim(store, slot_key, claim, state)
    if answered:
        meta[WAITING_ON_YOU_META_KEY] = answered
        _ANSWERING.add((slot_key, answered["ask_id"]))
        _schedule(slot_key, _retire_answered(state, slot_key, answered["ask_id"]))
    elif named := answer_claim({ANSWER_CLAIM_META_KEY: claim}).get(ANSWER_CLAIM_META_KEY):
        # A card answer that answers nothing now retires nothing. When its ask is gone
        # (replaced, cleared or closed while the answer waited in the queue) the row
        # still names the ask it was chosen for, so the turn can tell the agent which
        # earlier ask this answers. An ask still pinned is one another answer is
        # already retiring, and this row adds nothing to it.
        if not any(live["ask_id"] == named["ask_id"] for live in store.asks(slot_key)):
            meta[WAITING_ON_YOU_META_KEY] = {**named, "gone": True}
        return meta
    elif not store.loaded or store.asks(slot_key):
        _schedule(
            slot_key,
            _retire_for_message(
                state,
                slot_key,
                _answers_on_the_way(state, slot_key),
                _shown_now(store, slot_key) if shown_at_send is None else shown_at_send,
            ),
        )
    return meta


#: Where a send request keeps :func:`shown_at_arrival`'s stamp, for a delivery helper
#: the handler hands only the request (``responsive_chat.accept``).
SHOWN_AT_SEND_REQUEST_KEY = "waiting_on_you_shown_at_send"


def shown_at_arrival(request: Any, state: Any, slot_key: str) -> dict[str, Any] | None:
    """The owner's send-time ask stamp for *slot_key*, kept on *request* as well.

    An empty *slot_key* (a send not by the owner, or one naming no slot yet) has
    none: the delivery helpers then read the asks pinned when they run.
    """
    shown = shown_asks_meta(state, slot_key) if slot_key else None
    request[SHOWN_AT_SEND_REQUEST_KEY] = shown
    return shown


def send_meta(user_meta: Any, shown_at_send: dict[str, Any] | None) -> dict[str, Any]:
    """What waiting-on-you puts on a send's meta before its row is written.

    A card answer's claim, or for the owner's other sends *shown_at_send*, the asks
    pinned when it arrived (``shown_asks_meta``; None for anyone else's send). The row
    may be written after an await or a wait in the queue, so the stamp, not the asks
    pinned at the write, decides what it retires.
    """
    return answer_claim(user_meta) or dict(shown_at_send or {})


def stamp_edited_entry(state: Any, slot_key: str, entry: Any, *, owner_edit: bool) -> None:
    """Re-stamp a queued entry whose words an edit replaced.

    The replacement words are the editor's, written against the asks pinned at the
    edit. The owner's edit retires those, a teammate's entry included, so it is
    marked as the owner's (``OWNER_EDIT_META_KEY``); anyone else's edit retires none,
    whatever the entry's original author. A card answer the edit kept as it was keeps
    its claim.
    """
    if not isinstance(entry, dict) or answer_claim(entry.get("meta")):
        return
    stamp: dict[str, Any]
    if owner_edit:
        stamp = {**shown_asks_meta(state, slot_key), OWNER_EDIT_META_KEY: True}
    elif _authored_by_owner(entry.get("meta")):
        stamp = {SHOWN_ASKS_META_KEY: []}
    else:
        stamp = {}
    if isinstance(entry.get("meta"), dict):
        entry["meta"].pop(SHOWN_ASKS_META_KEY, None)
        entry["meta"].pop(OWNER_EDIT_META_KEY, None)
    if stamp:
        if not isinstance(entry.get("meta"), dict):
            entry["meta"] = {}
        entry["meta"].update(stamp)


def _person_row(meta: dict[str, Any]) -> bool:
    from kiro_crew.history_schema import HUMAN_TURN_META_KEY

    return meta.get(HUMAN_TURN_META_KEY) is True


def owner_edited(entry: Any) -> bool:
    """Whether the owner replaced this queued entry's words (``OWNER_EDIT_META_KEY``)."""
    meta = entry.get("meta") if isinstance(entry, dict) else None
    return isinstance(meta, dict) and meta.get(OWNER_EDIT_META_KEY) is True


def merged_owner_edit(entry_metas: list[Any]) -> dict[str, bool]:
    """The owner-edit mark of a row merged from several entries: only when the owner
    edited every one of them, since the row is then the owner's words alone."""
    edited = bool(entry_metas) and all(
        isinstance(m, dict) and m.get(OWNER_EDIT_META_KEY) is True for m in entry_metas
    )
    return {OWNER_EDIT_META_KEY: True} if edited else {}


def _authored_by_owner(meta: Any) -> bool:
    from kiro_crew.shared_crew.attribution import message_author
    from kiro_crew.shared_crew.owner import is_owner_principal

    return isinstance(meta, dict) and is_owner_principal(message_author(meta))


#: What the owner could see when they wrote: the ids of the asks pinned then, and,
#: when the store was not loaded, the time they wrote, so every ask pinned by then
#: retires and none declared later does.
Shown = tuple[frozenset[str], "float | None"]


def shown_asks_meta(state: Any, slot_key: str) -> dict[str, Any]:
    """The queue-entry stamp of a message the owner sends into the queue.

    Names the asks pinned now, an empty list included, so the row the drain writes
    later retires only those. While the store is loading it names the time instead.
    """
    if state is None:
        return {}
    return {SHOWN_ASKS_META_KEY: _shown_stamp(_shown_now(store_mod.store_for(state), slot_key))}


def merged_shown_asks(entry_metas: list[Any]) -> dict[str, Any]:
    """The stamp of a row the drain merges from several queued entries.

    Each entry retires what was pinned when it was sent, so the row retires the
    union. One entry without a stamp (restored from an older queue) makes the row
    fall back to the asks pinned at drain.
    """
    ids: set[str] = set()
    before: float | None = None
    for entry_meta in entry_metas:
        shown = _parse_shown(
            entry_meta.get(SHOWN_ASKS_META_KEY) if isinstance(entry_meta, dict) else None
        )
        if shown is None:
            return {}
        ids |= shown[0]
        if shown[1] is not None:
            before = shown[1] if before is None else max(before, shown[1])
    return {SHOWN_ASKS_META_KEY: _shown_stamp((frozenset(ids), before))} if entry_metas else {}


def _shown_stamp(shown: Shown) -> Any:
    ids, before = shown
    return sorted(ids) if before is None else {"ids": sorted(ids), "before": before}


def _parse_shown(raw: Any) -> Shown | None:
    before: float | None = None
    if isinstance(raw, dict):
        stamp_time = raw.get("before")
        if isinstance(stamp_time, bool) or not isinstance(stamp_time, (int, float)):
            return None
        before, raw = float(stamp_time), raw.get("ids", [])
    if not isinstance(raw, list) or not all(
        isinstance(i, str) and ASK_ID_RE.fullmatch(i) for i in raw
    ):
        return None
    return frozenset(raw), before


def _shown_now(store: store_mod.WaitingStore, slot_key: str) -> Shown:
    """What the owner could see when they wrote, read with no await.

    The retirement waits for the store, and a declaration saving meanwhile lands
    first; only what this names retires, so that newer ask stays pinned. While the
    store is still loading nothing can say which asks were shown, so it names the
    time instead: every ask pinned by then retires.
    """
    if not store.loaded:
        return frozenset(), store.now()
    return frozenset(a["ask_id"] for a in store.asks(slot_key)), None


def _answers_on_the_way(state: Any, slot_key: str) -> frozenset[str]:
    """Ask ids with an answer queued, being sent, or written but not yet retired."""
    ids = {ask_id for key, ask_id in _IN_FLIGHT | _ANSWERING if key == slot_key}
    slot = (getattr(state, "_slots", None) or {}).get(slot_key)
    for item in list(getattr(slot, "_queue", None) or []):
        queued = answer_claim(item.get("meta") if isinstance(item, dict) else None)
        if queued:
            ids.add(queued[ANSWER_CLAIM_META_KEY]["ask_id"])
    return frozenset(ids)


def _schedule(slot_key: str, work: Coroutine[Any, Any, None]) -> None:
    try:
        task = asyncio.get_running_loop().create_task(work)
    except RuntimeError:
        work.close()
        return
    _RETIRE_TASKS.add(task)
    _RETIRE_TASK_SLOTS[task] = slot_key
    task.add_done_callback(_forget_retire_task)


def _forget_retire_task(task: asyncio.Task[None]) -> None:
    _RETIRE_TASKS.discard(task)
    _RETIRE_TASK_SLOTS.pop(task, None)


async def _settle_slot_retirements(slot_key: str) -> None:
    """Wait, bounded, for the retirements the rows of *slot_key* have scheduled.

    The row that opens a turn schedules its retirement as it is written, so the turn
    would otherwise read the asks that row is still closing.
    """
    pending = [task for task, key in list(_RETIRE_TASK_SLOTS.items()) if key == slot_key]
    if pending:
        await asyncio.wait(pending, timeout=RETIREMENT_WAIT_SECONDS)


async def _retire_answered(state: Any, slot_key: str, ask_id: str) -> None:
    """Retire the one ask a row answered. Never raises; the turn it opens retries."""
    try:
        gone = await store_mod.store_for(state).retire(slot_key, ask_id, store_mod.REASON_ANSWERED)
    except Exception:
        # Still marked answered: the ask stays pinned on disk, but a second answer
        # must not reach it before the turn's own retry lands.
        logger.warning("could not retire the answered ask of %s", slot_key, exc_info=True)
        return
    _ANSWERING.discard((slot_key, ask_id))
    if gone:
        await publish(state, slot_key, ROW_ANSWERED, [gone])


async def _retire_for_message(
    state: Any, slot_key: str, keep: frozenset[str], shown: Shown
) -> None:
    store = store_mod.store_for(state)
    only, before = shown
    try:
        gone = await store.retire_all(
            slot_key, store_mod.REASON_MESSAGE, keep=keep, only=only, before=before
        )
    except Exception:
        logger.warning("could not retire the asks of %s after a message", slot_key, exc_info=True)
        _RETIRE_RETRIES.setdefault(slot_key, []).append((keep, shown))
        return
    if gone:
        await publish(state, slot_key, ROW_RETIRED, gone)


async def _stamped_but_pinned(state: Any) -> set[tuple[str, str]]:
    """Pinned asks a written row already answered: a retirement lost to a restart.

    A restored tab's window is loaded off the event loop before its rows are read.
    """
    from kiro_crew.dashboard.state import load_window_off_loop

    store = store_mod.store_for(state)
    live = getattr(state, "_slots", None) or {}
    found: set[tuple[str, str]] = set()
    for slot_key in store.slots_with_asks():
        slot = live.get(slot_key)
        if getattr(slot, "window_pending", False) is True:
            try:
                await load_window_off_loop(slot)
            except Exception:
                logger.warning("could not load the window of %s", slot_key, exc_info=True)
                continue
        stamped = _stamped_answers(state, slot_key)
        found |= {(slot_key, a["ask_id"]) for a in store.asks(slot_key) if a["ask_id"] in stamped}
    return found


async def _retry_failed_retirements(state: Any) -> None:
    """Retire again what a message retired when its write failed, each with its own
    targets. A retirement that fails again stays queued for the next sweep."""
    for slot_key in sorted(_RETIRE_RETRIES):
        pending = _RETIRE_RETRIES.pop(slot_key)
        for keep, shown in pending:
            await _retire_for_message(state, slot_key, keep, shown)


async def forget_slot(state: Any, slot_key: str) -> None:
    """Remove the records of a deleted session. Never raises."""
    try:
        if await store_mod.store_for(state).forget(slot_key):
            push(state, slot_key)
    except Exception:
        logger.warning("could not remove the asks of deleted %s", slot_key, exc_info=True)
        _FORGET_RETRIES.add(slot_key)
        return
    _FORGET_RETRIES.discard(slot_key)


async def forget_deleted_session(state: Any, slot_keys: set[str]) -> None:
    """Remove the asks of a deleted transcript, named by each slot-key spelling it had.

    The delete routes' shared teardown calls this after the unlink. A slot key is
    recycled, so a key a live slot holds keeps its record: that is a successor's.
    """
    live = getattr(state, "_slots", None) or {}
    for slot_key in sorted(slot_keys - {""}):
        if slot_key not in live:
            await forget_slot(state, slot_key)


Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def retiring_on_success(handler: Handler) -> Handler:
    """*handler* (the edit-resend route) that retires the slot's asks when it succeeds.

    An edit-resend is the owner writing, as a new message is, but its row is written
    on a scratch slot with no author, so the live-append hook never sees it.
    """

    @functools.wraps(handler)
    async def _route(request: web.Request) -> web.StreamResponse:
        from kiro_crew.dashboard.handlers.source_providers import (
            is_host_owner_dashboard_request,
        )

        state = request.app["state"]
        slot_key = request.match_info.get("slot", "")
        shown = _shown_now(store_mod.store_for(state), slot_key)
        resp = await handler(request)
        if 200 <= resp.status < 300 and slot_key and is_host_owner_dashboard_request(request):
            await _retire_for_message(state, slot_key, frozenset(), shown)
        return resp

    return _route


# ── the expiry sweep ──


async def sweep_once(state: Any) -> None:
    """Retry answered asks, message retirements and deleted-session removals whose
    write did not land, expire what is due, then tell each slot. Never raises.

    An answer's mark stays until its ask is retired, so an ask whose write failed
    both on its row and on its turn's retry is retried here. After a restart the
    mark is gone, so a pinned ask that a written row's stamp answered is retried
    too. Retiring is idempotent: a retry racing the row's own is harmless.
    """
    for slot_key, ask_id in sorted(_ANSWERING | await _stamped_but_pinned(state)):
        await _retire_answered(state, slot_key, ask_id)
    await _retry_failed_retirements(state)
    live = getattr(state, "_slots", None) or {}
    for slot_key in sorted(_FORGET_RETRIES):
        if slot_key in live:
            # The key was recycled for a new session: its record is the successor's.
            _FORGET_RETRIES.discard(slot_key)
        else:
            await forget_slot(state, slot_key)
    try:
        expired = await store_mod.store_for(state).sweep()
    except Exception:
        logger.warning("waiting-on-you sweep failed", exc_info=True)
        return
    for slot_key, asks in expired.items():
        await publish(state, slot_key, ROW_EXPIRED, asks)


async def _sweep_loop(state: Any) -> None:
    from kiro_crew import shutdown_event

    store = store_mod.store_for(state)
    await store.warm()
    for slot_key in store.slots_with_asks():
        push(state, slot_key)
    while not shutdown_event.is_set():
        await sweep_once(state)
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=store_mod.SWEEP_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            continue


async def startup(app: web.Application) -> None:
    """The gateway's startup hook: load the store and start the sweep, then return."""
    task = asyncio.ensure_future(_sweep_loop(app["state"]))
    _SWEEP_TASKS.add(task)
    task.add_done_callback(_SWEEP_TASKS.discard)


async def cleanup(_app: web.Application) -> None:
    tasks = list(_SWEEP_TASKS)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


# ── what the agent hears ──


def _answered(slot: Any) -> dict[str, str]:
    """``{key, ask_id}`` of the ask the person row that opens this turn answered, if any.

    Only display-only ``card`` rows are passed over. Any other row that is not the
    person's (a reply, a nudge, a sub-agent result, an inject) means no answer
    started this turn, so an older answer is not attached to it.
    """
    for row in reversed(getattr(slot, "messages", None) or []):
        if not isinstance(row, dict):
            continue
        role = row.get("role")
        if role == "card":
            continue
        if role != "user":
            return {}
        stamp = (row.get("meta") or {}).get(WAITING_ON_YOU_META_KEY)
        if isinstance(stamp, dict) and isinstance(stamp.get("ask_id"), str):
            answered = {"key": str(stamp.get("key") or ""), "ask_id": stamp["ask_id"]}
            return {**answered, "gone": "1"} if stamp.get("gone") is True else answered
        return {}
    return {}


#: What one composed turn showed the agent: its slot, the retired records and the
#: dropped count, removed from the store once the model answers that turn.
WaitingShown = tuple[str, list[dict[str, Any]], int]


async def turn_block(state: Any, slot: Any, *, now: float | None = None) -> str:
    """The ``[WAITING ON YOU]`` block :func:`turn_context` composes, alone."""
    block, _shown = await turn_context(state, slot, now=now)
    return block


async def turn_context(
    state: Any, slot: Any, *, now: float | None = None
) -> tuple[str, WaitingShown | None]:
    """The ``[WAITING ON YOU]`` block for the turn starting on *slot* (or ``""``), and
    the closures it names, for :func:`acknowledge_shown` (``None`` when it names none).

    Present while an ask is pinned, a retired record is unheard, or the row that opens
    the turn answered an ask. That ask is retired here too, which does nothing when
    the row's own retirement landed and retries it when it did not. The retired
    records are read with the pinned asks in one step after the slot's scheduled
    retirements land, so no ask is both pinned and closed. They stay in the store
    until the model answers the turn that carries them, so a turn stopped or refused
    before its prompt is sent leaves them for the next one.
    """
    from kiro_crew.context_blocks import render_waiting_on_you

    store = store_mod.store_for(state)
    await store.warm()
    slot_key = str(getattr(slot, "key", "") or "")
    if not slot_key:
        return "", None
    answered = _answered(slot)
    await _settle_slot_retirements(slot_key)
    if answered and not answered.get("gone"):
        await _retire_answered(state, slot_key, answered["ask_id"])
    pinned, retired, dropped = await store.turn_view(slot_key)
    shown: WaitingShown | None = (slot_key, retired, dropped) if retired or dropped else None
    if not pinned and not retired and not answered:
        return "", None
    block = render_waiting_on_you(
        pinned,
        retired,
        dropped=dropped,
        answered=answered.get("key", ""),
        answered_gone=bool(answered.get("gone")),
        now=time.time() if now is None else now,
    )
    return block, shown


def acknowledge_shown(state: Any, shown: WaitingShown | None) -> None:
    """The model answered the prompt that carried *shown*: those retired records are
    heard, so the store drops them. Awaits nothing."""
    if shown is None:
        return
    _schedule(shown[0], _acknowledge(state, *shown))


async def _acknowledge(
    state: Any, slot_key: str, shown: list[dict[str, Any]], dropped: int
) -> None:
    try:
        await store_mod.store_for(state).acknowledge_retired(slot_key, shown, dropped)
    except Exception:
        # They stay unheard, so the next turn tells the agent again.
        logger.warning("could not mark the closed asks of %s heard", slot_key, exc_info=True)
