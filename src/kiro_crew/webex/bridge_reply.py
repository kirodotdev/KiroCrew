"""A Webex reply that answers a bridged tool approval (RFC bridge actions, B4g).

Webex has no quoted reply: a reply is a message in the thread the note started, so
``parentId`` names the note. Webex threads are FLAT, so any reply in that thread
names it, never a later message, and the thread stays open after the answer. So this
takes a message only when it answers the linked decision (``approve`` or ``deny``
for an approval, an option number for a question) in such a thread, in a direct
room, under a message the registry still knows; ``notifications.bridge_reply``
decides and audits it. Anything else, a follow-up or a command typed in the thread
included, is left to the ordinary turn path untouched, as are a space reply, a reply
to any other message and a plain message.

The owner is the sender's ``personEmail``, read from Webex's own message record and
lower-cased, against the one configured ``user:<email>`` owner DM: the identity the
email allow-list already authorizes on. The bridge addresses that DM by the email
itself (``WebexTransport.resolve_conversation``; Webex opens the 1:1 space from
``toPersonEmail``), so each posted note is linked under ``<email>:<message id>``,
and a reply is looked up under its sender's email. A direct room holds the bot and
one person, so a reply in a direct room under the owner's email is the owner's own.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from kiro_crew.notifications.bridge_approval_note import DECISION_VALUES
from kiro_crew.notifications.bridge_reply import answer_bridged_reply
from kiro_crew.webex.transport import ROOM_DIRECT

#: The transport name the registry links and audits under.
TRANSPORT = "webex"


async def consume_bridged_reply(
    state: Any, inbound: Any, notify: Callable[[str], Awaitable[Any]]
) -> bool:
    """Answer the bridged approval *inbound* replies to. True when it was taken.

    False leaves *inbound* to the ordinary turn path. *notify* sends text to the
    room *inbound* came from.
    """
    parent = str(getattr(inbound, "parent_id", "") or "")
    if not parent or getattr(inbound, "room_type", "") != ROOM_DIRECT:
        return False
    text = str(getattr(inbound, "text", "") or "")
    # Unlike a quoted reply, a thread stays open: the owner keeps talking in it
    # after answering. Only what answers the linked decision is an answer here, an
    # option number for a question and an answer word for an approval; every other
    # message in the thread is the ordinary conversation it would have been without
    # a note.
    sender = str(getattr(inbound, "person_email", "") or "").lower()
    registry = getattr(state, "bridge_actions", None)
    token = registry.token_for_message(TRANSPORT, f"{sender}:{parent}") if registry else ""
    context = registry.context_for(token) if registry is not None and token else None
    word = text.strip().lower()
    if context is not None and context.kind == "question":
        if not (len(word) <= 2 and word.isascii() and word.isdigit()):
            return False
    elif word not in DECISION_VALUES:
        return False
    return await answer_bridged_reply(
        state,
        transport=TRANSPORT,
        conversation_id=sender,
        reply_to=parent,
        sender=sender,
        text=text,
        # The conversation is the sender's own direct room, checked above.
        in_owner_dm=lambda owner: True,
        notify=notify,
    )
