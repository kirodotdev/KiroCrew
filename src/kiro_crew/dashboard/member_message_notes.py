"""A crewmate's message in its DM thread, posted as a notification.

A crewmate that writes to the user in its DM thread otherwise reaches them only
through the Crewmates rail dot and the member row's unread marker, which a user
on another page or in another window does not see. So every turn that lands in a
member DM slot publishes one note on the bus, for the turn's last reply row:

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

The note is published at turn end (:func:`post_turn_reply`, called by the chat
runner once the turn has landed), not per appended row, for two reasons:

- a DM slot also holds ``assistant`` rows the gateway wrote (a compaction
  notice, a cleared-conversation line, a slash command's reply, a workflow or
  cron result injected into the thread). Only a row the turn runner recorded as
  its own reply (``_note_reply_row``) is the crewmate speaking;
- one turn appends one reply row per model call (the text before each tool
  call is its own segment), so a per-row note would ping several times for one
  answer. The last reply row with text is the answer the user is waiting for.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Iterable
from urllib.parse import quote, urlencode

from kiro_crew.dashboard.system_notices import is_speech_row
from kiro_crew.members import member_identity_for_session
from kiro_crew.notifications.bus import NotificationValidationError, payload_from_legacy

logger = logging.getLogger(__name__)

MEMBER_CHANNEL = "system.member"

# Strong references to in-flight publishes: the event loop keeps only weak ones,
# so an unreferenced task can be collected before it runs.
_pending: set[asyncio.Task[None]] = set()


def is_member_message(msg: dict[str, Any]) -> bool:
    """Whether *msg* is a row a crewmate wrote to the user: ``assistant`` speech.

    Speech is what the crewmate's thread draws (``is_speech_row``), so a reply
    the thread hides never becomes a note: the bare U+200B a quiet check-in turn
    ends on, a system notice, or a completion envelope.
    """
    role = msg.get("role")
    return role == "assistant" and is_speech_row(role, msg.get("content"), msg.get("meta"))


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


def post_turn_reply(state: Any, slot: Any, reply_mids: Iterable[str]) -> None:
    """Publish the one note for a turn that landed in *slot*, if it is a member DM slot.

    *reply_mids* are the row ids the runner recorded as this turn's own replies
    (``slot._turn_reply_mids``); the newest of those rows that has text is the
    note's body. A turn with no such row (a command answered without a model
    call, a turn whose reply was empty) publishes nothing. Never raises: a note
    must not surface into the turn.
    """
    try:
        mids = set(reply_mids)
        if not mids:
            return
        from kiro_crew import eventlog_hooks
        from kiro_crew.dashboard.state import row_mid

        if eventlog_hooks.member_slug_for_slot(slot.key) is None:
            return
        for row in reversed(slot.messages):
            if row_mid(row) in mids and is_member_message(row):
                post_member_message(state, slot.key, row)
                return
    except Exception:  # noqa: BLE001 -- a note must never surface into the turn
        logger.debug("crewmate turn note failed", exc_info=True)


def post_member_message(state: Any, slot_key: str, msg: dict[str, Any]) -> None:
    """Publish the note for *msg*, a reply row in member DM slot *slot_key*.

    Called from :func:`post_turn_reply`, on the event loop: the binding
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
