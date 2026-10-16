"""A crewmate's message in its DM thread, posted as a notification.

A crewmate that writes to the user in its DM thread otherwise reaches them only
through the Crewmates rail dot and the member row's unread marker, which a user
on another page or in another window does not see. So every ``assistant`` row
with text that lands live in a member DM slot publishes one note on the bus:

- channel ``system.member``, which the user can mute on its own in Settings ->
  Notifications; the note keeps the legacy ``kind`` ``agent``, like
  ``system.monitor``, so the feed badge, stat cards and per-kind sound are
  unchanged;
- ``member`` is the crewmate the slot's live DM binding names
  (:func:`kiro_crew.members.member_identity_for_session`), never anything the
  model wrote; a slot whose binding is gone publishes nothing, because a
  crewmate's message nobody can attribute is not a crewmate's message;
- title is the crewmate's name, body the message text (the delivery path
  redacts it, and the legacy adapter caps it);
- ``url`` opens that crewmate's thread on the Members page, ``slot`` names the
  thread so the banner can tell it is already on screen, and ``group_key`` is the
  slot key so a run of messages from one crewmate stacks;
- ``publisher_session`` is the slot, so a muted thread's note is delivered
  silenced, as every other note that session publishes is.

Only ``assistant`` rows: tool calls, tool results, injected and streamed rows
never notify. Which rows reach :func:`post_member_message` at all (live rows
only, never a replay) is decided by ``Slot.append`` before its ``_on_row`` hook.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import quote, urlencode

from kiro_crew.members import member_identity_for_session
from kiro_crew.notifications.bus import NotificationValidationError, payload_from_legacy

logger = logging.getLogger(__name__)

MEMBER_CHANNEL = "system.member"

# Strong references to in-flight publishes: the event loop keeps only weak ones,
# so an unreferenced task can be collected before it runs.
_pending: set[asyncio.Task[None]] = set()


def is_member_message(msg: dict[str, Any]) -> bool:
    """Whether *msg* is a row a crewmate wrote to the user: ``assistant`` with text."""
    content = msg.get("content")
    return msg.get("role") == "assistant" and isinstance(content, str) and bool(content.strip())


def _thread_url(name: str) -> str:
    """The Members page deep link to *name*'s thread (the page reads the exact name)."""
    return "/members?" + urlencode({"member": name}, quote_via=quote)


def _publish(state: Any, slot_key: str, text: str, member: dict[str, str]) -> None:
    try:
        payload = payload_from_legacy(
            "agent",
            member["name"],
            text,
            {"slot": slot_key},
            url=_thread_url(member["name"]),
            channel=MEMBER_CHANNEL,
            member=member,
            publisher_session=slot_key,
        )
        payload.group_key = slot_key
        state.notification_bus.push(payload)
    except NotificationValidationError:
        logger.warning("could not publish a crewmate message note for slug=%r", member.get("slug"))


async def _resolve_and_publish(state: Any, slot_key: str, text: str) -> None:
    try:
        # Blocking file IO (the slot's dm.json binding): off the event loop.
        member = await asyncio.to_thread(member_identity_for_session, slot_key)
        if member is not None:
            _publish(state, slot_key, text, member)
    except Exception:  # noqa: BLE001 -- a note must never surface into the turn
        logger.debug("crewmate message note failed", exc_info=True)


def post_member_message(state: Any, slot_key: str, msg: dict[str, Any]) -> None:
    """Publish the note for *msg*, a live row in member DM slot *slot_key*.

    Called from the slot's row hook, which runs on the event loop: the binding
    read is handed to a worker thread and the note is pushed back on the loop,
    where delivery fans out to the SSE queues. A caller with no running loop
    resolves and publishes inline.
    """
    if not is_member_message(msg):
        return
    text = msg["content"]
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        member = member_identity_for_session(slot_key)
        if member is not None:
            _publish(state, slot_key, text, member)
        return
    task = loop.create_task(_resolve_and_publish(state, slot_key, text))
    _pending.add(task)
    task.add_done_callback(_pending.discard)
