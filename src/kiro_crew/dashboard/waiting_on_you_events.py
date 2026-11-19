"""What follows a change to the waiting-on-you store, and what changes it besides a route.

Every change is told the same way once it is on disk: one display-only ``card`` row
(surface ``waiting``) in the slot's transcript, so the history shows when an ask came
and went, and the ordinary slots push, which carries the new ``waiting_on_you`` list.
There is no frame of its own: the slot's ``waiting_on_you`` goes out as a
``slot_patch``, and in every full ``slots`` list.

Besides the routes, four things change the store:

* a live row the OWNER typed (``meta.human``, authored by the owner) retires every
  ask, because the server cannot tell which ask it answers. A card answer's own row
  carries ``meta.waitingOnYou`` and is skipped: its route settles that one ask;
* a rewind or regenerate clears the slot's asks;
* deleting a session removes its records;
* the expiry sweep, every :data:`~kiro_crew.dashboard.waiting_on_you.SWEEP_INTERVAL_SECONDS`.

And the agent hears of them through :func:`turn_block`, once per retired record.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from typing import Any, Awaitable, Callable

from aiohttp import web

from kiro_crew.dashboard import waiting_on_you as store_mod

logger = logging.getLogger(__name__)

#: Row-meta key a card answer's user row carries: ``{key, ask_id, answer_id}``.
#: Reserved (``chat_handlers.RESERVED_ROW_META_KEYS``), so no caller can write it.
WAITING_ON_YOU_META_KEY = "waitingOnYou"
#: Request key the answer route sets on the chat send it makes. Only server code can
#: set a request key, so this is how that send's row gets its reserved meta.
ANSWER_REQUEST_KEY = "kirocrew_waiting_on_you_answer"


def answer_meta(request: Any) -> dict[str, Any]:
    """``{waitingOnYou: {...}}`` for the chat send an answer route makes, else ``{}``."""
    try:
        claim = request.get(ANSWER_REQUEST_KEY)
    except Exception:
        return {}
    return {WAITING_ON_YOU_META_KEY: dict(claim)} if isinstance(claim, dict) else {}


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
    """Tell every tab: a ``slot_patch`` of the slot's ``waiting_on_you``, which also
    sends the full list to a tab that cannot apply a patch. Never raises."""
    try:
        patcher = getattr(state, "push_slot_patch", None)
        if callable(patcher):
            patcher(slot_key, ("waiting_on_you",))
            return
        pusher = getattr(state, "push_slots_update", None)
        if callable(pusher):
            pusher()
    except Exception:
        logger.debug("slots push after a waiting-on-you change failed", exc_info=True)


def retires_asks(meta: dict[str, Any] | None) -> bool:
    """Whether a live user row with *meta* is the owner writing in the chat.

    Only a row a person typed (``meta.human``) whose server-stamped author is the
    owner. A card answer's row is not: it answers one ask, settled by its route. A
    teammate's message, a channel row with an unknown sender and every machine row
    (no ``meta.human``) retire nothing.
    """
    from kiro_crew.history_schema import HUMAN_TURN_META_KEY

    if not isinstance(meta, dict) or meta.get(HUMAN_TURN_META_KEY) is not True:
        return False
    if WAITING_ON_YOU_META_KEY in meta:
        return False
    from kiro_crew.shared_crew.attribution import message_author
    from kiro_crew.shared_crew.owner import is_owner_principal

    return is_owner_principal(message_author(meta))


def on_live_user_row(state: Any, slot_key: str, meta: dict[str, Any] | None) -> None:
    """The live-append hook: the owner's message retires every pinned ask.

    Runs on the append, so the store change is scheduled: it is written, then told.
    """
    if state is None or not retires_asks(meta):
        return
    store = store_mod.store_for(state)
    if store.loaded and not store.asks(slot_key):
        return
    try:
        task = asyncio.get_running_loop().create_task(_retire_for_message(state, slot_key))
    except RuntimeError:
        return
    _RETIRE_TASKS.add(task)
    task.add_done_callback(_RETIRE_TASKS.discard)


async def _retire_for_message(state: Any, slot_key: str) -> None:
    store = store_mod.store_for(state)
    try:
        gone = await store.retire_all(slot_key, store_mod.REASON_MESSAGE)
    except Exception:
        logger.warning("could not retire the asks of %s after a message", slot_key, exc_info=True)
        return
    if gone:
        await publish(state, slot_key, ROW_RETIRED, gone)


async def clear_slot(state: Any, slot_key: str) -> None:
    """Clear every ask of *slot_key* after a rewind or regenerate. Never raises."""
    store = store_mod.store_for(state)
    try:
        gone = await store.clear(slot_key, None, reason=store_mod.REASON_REWOUND)
    except Exception:
        logger.warning("could not clear the asks of %s", slot_key, exc_info=True)
        return
    if gone:
        await publish(state, slot_key, ROW_CLEARED, gone)


async def forget_slot(state: Any, slot_key: str) -> None:
    """Remove the records of a deleted session. Never raises."""
    try:
        if await store_mod.store_for(state).forget(slot_key):
            push(state, slot_key)
    except Exception:
        logger.warning("could not remove the asks of deleted %s", slot_key, exc_info=True)


Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def clearing_on_success(handler: Handler) -> Handler:
    """*handler* (a rewind or regenerate route) that clears the slot's asks when it succeeds.

    Each ask was declared from the conversation as it stood. Matching an ask to the
    turn that made it would mean reading authority back from rows, so every ask goes
    and the agent re-declares what still stands.
    """

    @functools.wraps(handler)
    async def _route(request: web.Request) -> web.StreamResponse:
        resp = await handler(request)
        if 200 <= resp.status < 300:
            slot_key = request.match_info.get("slot", "")
            if slot_key:
                await clear_slot(request.app["state"], slot_key)
        return resp

    return _route


def forgetting_on_success(handler: Handler) -> Handler:
    """*handler* (``DELETE /api/sessions/{key}``) that removes the session's asks.

    The slot key is read before the delete, while the session's tab still exists to
    name it.
    """

    @functools.wraps(handler)
    async def _route(request: web.Request) -> web.StreamResponse:
        from kiro_crew.dashboard.chat_utils import dashboard_slot_key

        session_key = request.match_info.get("key", "")
        candidates = {dashboard_slot_key(session_key), session_key.removeprefix("dashboard:")}
        resp = await handler(request)
        if 200 <= resp.status < 300:
            for slot_key in candidates - {""}:
                await forget_slot(request.app["state"], slot_key)
        return resp

    return _route


# ── the expiry sweep ──


async def sweep_once(state: Any) -> None:
    """Expire what is due, then tell each slot. Never raises."""
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


def _answered_key(slot: Any, tombstones: list[dict[str, Any]]) -> str:
    """The key of the ask the row that opens this turn answers, checked against the store.

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
            return ""
        claim = (row.get("meta") or {}).get(WAITING_ON_YOU_META_KEY)
        if not isinstance(claim, dict):
            return ""
        for stone in tombstones:
            if stone.get("answer_id") == claim.get("answer_id"):
                return str(stone.get("key") or "")
        return ""
    return ""


async def turn_block(state: Any, slot: Any, *, now: float | None = None) -> str:
    """The ``[WAITING ON YOU]`` block for the turn starting on *slot*, or ``""``.

    Present while an ask is pinned or a retired record is unheard. The retired
    records are handed over here, once.
    """
    from kiro_crew.context_blocks import render_waiting_on_you

    store = store_mod.store_for(state)
    await store.warm()
    slot_key = str(getattr(slot, "key", "") or "")
    if not slot_key or not store.has_any(slot_key):
        return ""
    snapshot = store.snapshot_for_turn(slot_key)
    pinned = store.asks(slot_key)
    retired = await store.take_retired(slot_key) if snapshot["retired"] else []
    answered = _answered_key(slot, snapshot["tombstones"])
    return render_waiting_on_you(
        pinned, retired, answered=answered, now=time.time() if now is None else now
    )
