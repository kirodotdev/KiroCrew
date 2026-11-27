"""Hold a queued message while its card's editor is open.

Without the hold, a turn that ends while the person is still typing in a
queued card's editor drains the entry as it was, and the edit lands on an
entry that is already gone: the agent gets the old words. Opening the editor
marks the entry; the drain treats a marked entry like the sub-agent hold
treats user messages (``chat_turn.queue_drain``): user entries wait, system
injections still flow. Saving the edit (the PATCH), closing the editor, or the
deadline releases it, and a release on an idle slot starts the drain the hold
deferred.

Each open editor holds under its own holder id (one per chat page or pane
showing the queue), so closing the editor in one tab leaves the entry held
while another tab still has it open.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.handlers._shared import read_bounded_json
from kiro_crew.dashboard.slot_ownership import deny_app_slot_access, slot_not_found

logger = logging.getLogger(__name__)

#: In-memory only: underscore keys are not in the durable queue keys, so a
#: restart never restores a hold nobody is still editing under. The value maps
#: each editor's holder id to its deadline.
EDIT_HOLD_KEY = "_edit_holds"
# The open editor renews the hold every 30 s (``QueueStack``), so this bounds
# how long a tab closed or killed mid-edit keeps the queue waiting.
EDIT_HOLD_SECONDS = 90.0
# The hold's clock; tests replace it.
_clock = time.monotonic
#: A released hold drains only after this pause, so a hold the next card sends
#: just after this release (a move the press hold does not order) lands first.
RELEASE_GRACE_SECONDS = 2.0
#: A holder id is one `secureRandomId()` UUID (36 characters). The entry keeps the
#: id the request names, so the length is bounded where it is retained.
MAX_HOLDER_LEN = 64
#: Editors of one entry. Only a caller minting ids reaches it; the earliest
#: deadline then goes, with a warning, rather than an editor being refused.
MAX_EDIT_HOLDERS = 32


def _holds(item: dict[str, Any]) -> dict[str, float]:
    return item.get(EDIT_HOLD_KEY) or {}


def _live(until: Any) -> bool:
    return isinstance(until, float) and until > _clock()


def edit_held(item: dict[str, Any]) -> bool:
    """Whether any of *item*'s editors is open and its deadline has not passed."""
    return any(_live(until) for until in _holds(item).values())


def queue_edit_held(slot: Any) -> bool:
    """Whether any entry of *slot*'s queue is held for an open editor."""
    return any(edit_held(item) for item in getattr(slot, "_queue", ()))


def edit_hold_blocks_user_turn(slot: Any, *, run_now: bool = False) -> bool:
    """Whether an open editor keeps a person's turn on *slot* from starting.

    The one check every path that starts a user turn asks: the queue drain
    (a turn's end, a queued arrival, a responsive send, a restored card) and the
    composer's send on an idle slot. Only a card's Run now (*run_now*) passes,
    because the person picked that entry to go first.
    """
    return not run_now and queue_edit_held(slot)


def release_edit_hold(item: dict[str, Any]) -> bool:
    """Drop *item*'s hold; True when it had one."""
    return item.pop(EDIT_HOLD_KEY, None) is not None


def place_edit_hold(state: Any, slot: Any, item: dict[str, Any], holder: str) -> None:
    """Hold *item* for *holder*'s open editor until the deadline, or renew it.

    Past ``MAX_EDIT_HOLDERS`` holders the earliest deadline goes, so the entry
    holds a bounded set.
    """
    until = _clock() + EDIT_HOLD_SECONDS
    holds = item.setdefault(EDIT_HOLD_KEY, {})
    if holder not in holds and len(holds) >= MAX_EDIT_HOLDERS:
        del holds[min(holds, key=lambda key: holds[key])]
        logger.warning(
            "queue entry %s: over %d editors, dropped the earliest hold",
            item.get("id"),
            MAX_EDIT_HOLDERS,
        )
    holds[holder] = until
    _arm_expiry(state, slot, item, holder, until)


async def drain_if_idle(state: Any, slot: Any) -> None:
    """Start the drain a hold deferred, if the slot is idle and nothing is held."""
    if not slot._queue or edit_hold_blocks_user_turn(slot) or slot.executor == "remote":
        return
    # circular import: chat_runner imports the dashboard package at module level.
    from kiro_crew.dashboard.chat_runner import _start_next_queued_turn, idle_slot_admits_drain

    started = False
    try:
        async with slot._lock:
            # The same admission every idle-slot drain reads, sign-in hold
            # included, so a release never runs a turn into a signed-out CLI.
            if (
                not slot._queue
                or not idle_slot_admits_drain(state, slot)
                or edit_hold_blocks_user_turn(slot)
            ):
                return
            slot._turn_admission_reserved = True
            try:
                started = await _start_next_queued_turn(state, slot)
            finally:
                slot._turn_admission_reserved = False
    except Exception:
        logger.warning("Could not drain slot %s after a queue edit hold", slot.key, exc_info=True)
        return
    if started:
        state.push_slots_update()


def _spawn_drain(state: Any, slot: Any) -> None:
    task = asyncio.get_running_loop().create_task(drain_if_idle(state, slot))
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)


def _arm_expiry(
    state: Any,
    slot: Any,
    item: dict[str, Any],
    holder: str,
    until: float,
    delay: float = EDIT_HOLD_SECONDS,
) -> None:
    """Drop *holder*'s hold at its deadline, unless it was renewed or shortened
    first; drain once no other editor still holds the entry."""

    def _expire() -> None:
        holds = _holds(item)
        if holds.get(holder) != until:
            return
        del holds[holder]
        if not holds:
            item.pop(EDIT_HOLD_KEY, None)
        if not edit_held(item):
            _spawn_drain(state, slot)

    asyncio.get_running_loop().call_later(delay, _expire)


def release_and_drain(state: Any, slot: Any, item: dict[str, Any] | None, holder: str = "") -> None:
    """Shorten *holder*'s hold on *item* to the release grace; its expiry starts
    the drain when no other editor holds the entry. On an entry still queued, a
    holder it does not carry changes nothing; a cancelled entry drops every hold.

    The hold stays for ``RELEASE_GRACE_SECONDS`` rather than being dropped, so
    every drain, a turn's own end included, still sees it, and a hold another
    card sends just after this release lands before anything runs.
    """
    if item is None or not edit_held(item):
        return
    if not any(entry is item for entry in slot._queue):
        # A cancelled entry has left the queue: nothing can reopen it, so the
        # entries it held may run now.
        item.pop(EDIT_HOLD_KEY, None)
        _spawn_drain(state, slot)
        return
    holds = _holds(item)
    if not _live(holds.get(holder)):
        return
    until = _clock() + RELEASE_GRACE_SECONDS
    holds[holder] = until
    _arm_expiry(state, slot, item, holder, until, RELEASE_GRACE_SECONDS)


def holder_of(body: dict[str, Any]) -> tuple[str, web.Response | None]:
    """The editor's ``holder`` id from a request body: absent is the shared
    ``""`` (a tab loaded before per-editor holds), a non-string or one past
    ``MAX_HOLDER_LEN`` is a 400, since a hold keeps the id it is given."""
    holder = body.get("holder", "")
    if not isinstance(holder, str):
        return "", web.json_response(
            {"error": "holder must be a string", "code": "invalid_field_type"}, status=400
        )
    if len(holder) > MAX_HOLDER_LEN:
        return "", web.json_response(
            {
                "error": f"holder must be at most {MAX_HOLDER_LEN} characters",
                "code": "holder_too_long",
            },
            status=400,
        )
    return holder, None


async def api_chat_slot_queue_editing(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/queue/{queue_id}/editing — open or close a hold.

    ``{"editing": true}`` holds the entry while its card's editor is open;
    ``{"editing": false}`` releases it without an edit. Saving through the
    PATCH releases it too. An optional ``holder`` string names the editor, and
    a release frees only that editor's hold. 404 when the entry has already
    left the queue.
    """
    state = request.app["state"]
    name = request.match_info["slot"]
    queue_id = request.match_info["queue_id"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    denied = deny_app_slot_access(request.get("app", ""), slot, name, "slot_queue_edit")
    if denied is not None:
        return denied
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    editing = body.get("editing")
    if not isinstance(editing, bool):
        return web.json_response(
            {"error": "editing must be a boolean", "code": "invalid_field_type"}, status=400
        )
    holder, holder_err = holder_of(body)
    if holder_err is not None:
        return holder_err
    item = next((i for i in slot._queue if i.get("id") == queue_id), None)
    if item is None:
        return web.json_response(
            {"error": "queue item not found", "code": "queue_entry_not_found"}, status=404
        )
    if editing:
        place_edit_hold(state, slot, item, holder)
    else:
        release_and_drain(state, slot, item, holder)
    return web.json_response({"ok": True})
