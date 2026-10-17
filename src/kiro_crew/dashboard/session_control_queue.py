"""``session_queue``: list, cancel or move what a sender queued on a session it created.

Split out of :mod:`kiro_crew.dashboard.session_control`, whose line count is capped.
The gates (``authorize_target``, the creator fence, the audit row) stay there and
are reached through the module, so a test that patches one on
``session_control`` patches it for this verb too.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_delivery import (
    queue_entry_is_user_origin,
    queued_text_for_display,
    sanitize_outbound,
)
from kiro_crew.dashboard.chat_utils import _remove_queued_by_id, _reorder_queued_rows
from kiro_crew.validation import QUEUE_ENTRY_ID_MAX_CHARS

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)

# Bounds on a ``queue_target`` listing: how many entries it shows and how much of
# each entry's (redacted) text.
MAX_QUEUE_LIST_ENTRIES = 50
MAX_QUEUE_EXCERPT_CHARS = 200

QUEUE_ACTIONS = ("list", "cancel", "move")

# Who queued an entry, as ``queue_target`` reports it.
QUEUE_FROM_CALLER = "yours"
QUEUE_FROM_PERSON = "person"
QUEUE_FROM_OTHER = "other"


def _queue_entry_from(entry: dict[str, Any], caller_key: str, caller_tab: str) -> str:
    """Who queued *entry*, as ``queue_target`` reports it.

    ``yours`` needs the sender stamp ``send_to_target`` writes
    (:func:`send_origin_meta`) to name the caller's slot key AND its current tab
    identity. The key alone is not an identity: a closed slot's key can be handed
    to the next occupant, and that occupant must not inherit the old one's
    entries. A caller with no tab identity owns nothing, for the same reason
    :func:`send_origin_meta` stamps nothing for one.

    A person's own typed message is checked first so that no stamp, however it
    got there, can make one read as the caller's.

    Only a PLAIN entry can be the caller's: one with no producer ``kind``, no
    consumption callback and no steer delivery. ``session_send`` queues exactly
    that shape. A plan-stage delivery can inherit the sender stamp through its
    meta, yet it carries a kind the drain acts on and callbacks a waiter settles
    on, so removing or reordering it here would strand that waiter. A steer that
    ``_requeue_unconsumed_steers`` put back because its turn ended first carries
    the send's stamp too, with no kind and no callback, but its persisted steer
    row already says it will run as its own turn, and that row is not a
    ``queued`` row a cancel reaches; ``steer_delivery_id``, which
    ``steer_into_running_turn`` mints for every steer, marks it. All of these
    read as ``other``.
    """
    if queue_entry_is_user_origin(entry):
        return QUEUE_FROM_PERSON
    if entry.get("kind") or "_on_consumed" in entry or "_on_irreversibly_consumed" in entry:
        return QUEUE_FROM_OTHER
    meta = entry.get("meta")
    if isinstance(meta, dict) and meta.get("steer_delivery_id"):
        return QUEUE_FROM_OTHER
    if (
        caller_tab
        and sc.send_origin_slot(meta) == caller_key
        and sc.send_origin_tab(meta) == caller_tab
    ):
        return QUEUE_FROM_CALLER
    return QUEUE_FROM_OTHER


def _queue_excerpt(content: Any) -> str:
    """One entry's text for the listing: display-redacted, then cut.

    Redacted with ``user_origin=False`` whoever wrote it: the reader is an agent,
    not the session's own human, so a person's queued words get the redaction
    every non-human surface applies.
    """
    text = queued_text_for_display(content if isinstance(content, str) else "", user_origin=False)
    if len(text) <= MAX_QUEUE_EXCERPT_CHARS:
        return text
    return text[:MAX_QUEUE_EXCERPT_CHARS] + " …[truncated]"


def _queue_entry_id(item: dict[str, Any]) -> str:
    """An entry's id for the listing, bounded like every other listed field.

    Ids this process mints are 12 hex characters, but an entry restored from disk
    keeps whatever id its line carried (``sanitize_restored_queue`` bounds only the
    whole entry), and that file can be edited outside the gateway. So the id
    passes the same ``sanitize_outbound`` chain as every other outbound field
    before it is cut. An id that redaction or the cut changed could never be
    named back to cancel or move anyway, so nothing usable is lost.
    """
    raw = sanitize_outbound(str(item.get("id") or ""))
    if len(raw) <= QUEUE_ENTRY_ID_MAX_CHARS:
        return raw
    return raw[: QUEUE_ENTRY_ID_MAX_CHARS - 1] + "…"


async def queue_target(
    state: "DashboardState",
    *,
    caller_session_key: str,
    target: str,
    action: str = "list",
    entry: str = "",
    position: int | None = None,
    caller_fenced: bool | None = None,
) -> dict[str, Any]:
    """List, cancel or move entries in the queue of a session the caller created.

    ``session_send`` to a busy target queues the message and answers
    ``started: False``; this is the sender's handle on it afterwards.

    Two gates, both before anything is read or changed:

    * The target passes :func:`authorize_target` and, on top of it, must be a
      session the caller CREATED (``_created_by``), whatever class of caller this
      is. The queue holds text the target's human typed, and the owner-level
      reach the other verbs allow an unfenced caller would put a person's own
      tabs' queued words in front of an agent. Archived sessions are not open, so
      they resolve to ``target_not_found`` like every other verb's.
    * ``cancel`` and ``move`` act only on an entry the caller itself queued
      (:func:`_queue_entry_from`). A move to an EARLIER position may pass only
      the caller's own entries, so an agent never jumps ahead of a person's
      message or another session's; a move later only lets others run sooner,
      and is always allowed.

    Synchronous from the gate to the reply: nothing here suspends, so the entry
    that was checked is the entry that changes, and the listing is read from the
    queue the gates approved. The handler warms the config read before calling
    in, as ``read_summary``'s does.

    A cancel or move takes the queue card's own path (``DELETE .../queue/{id}``
    and ``PUT .../queue/order``): it changes memory, which is the queue's
    authority, broadcasts the same frame, and leaves the save to the periodic
    flush. It makes the same durability promise as the card, no more.
    """
    deny = sc._deny_factory(caller_session_key=caller_session_key, operation="queue", target=target)
    if action not in QUEUE_ACTIONS:
        raise deny(f"action must be one of {', '.join(QUEUE_ACTIONS)}", "bad_action", status=400)
    slot = sc.authorize_target(
        state,
        caller_session_key=caller_session_key,
        target=target,
        operation="queue",
        precomputed_ownership_fenced=caller_fenced,
    )
    caller_key = sc.caller_slot_key(state, caller_session_key)
    if sc._created_by_other(slot, caller_key):
        raise deny("session_queue reaches only sessions this session created", "not_creator")
    caller_tab = str(getattr(state._slots.get(caller_key), "_tab_id", "") or "")

    queue: list[dict[str, Any]] = slot._queue
    detail: dict[str, Any] = {"action": action}
    result: dict[str, Any] = {}

    if action in ("cancel", "move"):
        if not entry:
            raise deny(f"entry is required for action={action}", "entry_required", status=400)
        index = next((i for i, item in enumerate(queue) if item.get("id") == entry), -1)
        if index < 0:
            raise deny(f"no queued entry {entry!r} on that session", "entry_not_found", status=404)
        if _queue_entry_from(queue[index], caller_key, caller_tab) != QUEUE_FROM_CALLER:
            raise deny(
                "that entry was not queued by this session; only the entries you "
                "queued with session_send can be cancelled or moved",
                "not_your_entry",
            )
        detail["entry"] = entry
        frame: tuple[str, dict[str, Any]] | None = None

        if action == "cancel":
            slot.queue_remove_by_id(entry)
            _remove_queued_by_id(slot.messages, entry)
            slot.invalidate_source_links()
            # The frame the queue card's own cancel sends, so every open tab drops
            # the card. Content is withheld: the client restores a cancelled
            # entry's text into the composer only on the tab that pressed cancel,
            # and nobody pressed it here.
            frame = ("queue_cancel", {"slot": slot.key, "queue_id": entry, "content": ""})
            result["cancelled"] = entry
        else:
            if position is None:
                raise deny("position is required for action=move", "position_required", status=400)
            if position < 0:
                raise deny("position must be 0 or more", "bad_position", status=400)
            new_index = min(position, len(queue) - 1)
            if new_index < index:
                passed = queue[new_index:index]
                if any(
                    _queue_entry_from(item, caller_key, caller_tab) != QUEUE_FROM_CALLER
                    for item in passed
                ):
                    raise deny(
                        "a move to an earlier position may pass only entries this "
                        "session queued; a person's message or another session's "
                        "stays ahead of yours",
                        "move_blocked",
                    )
            if new_index != index:
                queue.insert(new_index, queue.pop(index))
                order = [item["id"] for item in queue]
                _reorder_queued_rows(slot.messages, order)
                slot.invalidate_source_links()
                frame = ("queue_reorder", {"slot": slot.key, "order": order})
            detail["from"] = index
            detail["to"] = new_index
            result["moved"] = entry
            result["position"] = new_index

        if frame is not None:
            state.broadcast_ws(*frame)
            try:
                state.push_slots_update()
            except Exception:  # pragma: no cover - sidebar refresh is best-effort
                logger.debug("session_queue: push_slots_update failed", exc_info=True)

    entries: list[dict[str, Any]] = []
    for pos, item in enumerate(queue[:MAX_QUEUE_LIST_ENTRIES]):
        entries.append(
            {
                "id": _queue_entry_id(item),
                "position": pos,
                "from": _queue_entry_from(item, caller_key, caller_tab),
                "excerpt": _queue_excerpt(item.get("content")),
            }
        )
    detail["queued"] = len(queue)
    sc._audit(
        caller_session_key=caller_session_key,
        operation="queue",
        slot_key=slot.key,
        outcome="allowed",
        detail=detail,
    )
    return {
        "ok": True,
        "target": slot.key,
        "title": sc._bounded_status_title(slot.display_title),
        "running": bool(slot.running or getattr(slot, "_in_stage_execution", False)),
        "action": action,
        "count": len(queue),
        "omitted": max(0, len(queue) - MAX_QUEUE_LIST_ENTRIES),
        "entries": entries,
        **result,
    }
