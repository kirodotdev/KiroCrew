"""A Telegram reply that answers a bridged tool approval (RFC bridge actions, B4d).

It takes a message only when it is a reply, in a private chat, to a message the
registry still knows; ``notifications.bridge_reply`` decides and audits it. Anything
else is left to the ordinary turn path untouched, so a group reply, a reply to any
other message and a plain message all behave as before. The owner is the sender's
``from.id`` (the message's ``user_id``), and in a Telegram private chat the chat id
IS the peer's user id, so the chat id equal to the owner pins the reply to the
owner's own DM.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from kiro_crew.notifications.bridge_reply import answer_bridged_reply

#: The transport name the registry links and audits under.
TRANSPORT = "telegram"


async def consume_bridged_reply(
    state: Any, msg: Any, notify: Callable[[str], Awaitable[Any]]
) -> bool:
    """Answer the bridged approval *msg* replies to. True when *msg* was taken.

    False leaves *msg* to the ordinary turn path. *notify* sends text to the chat
    *msg* came from.
    """
    reply_to = getattr(msg, "reply_to_message_id", 0)
    if not reply_to or getattr(msg, "chat_type", "") != "private":
        return False
    chat_id = str(getattr(msg, "conversation_id", ""))
    return await answer_bridged_reply(
        state,
        transport=TRANSPORT,
        conversation_id=chat_id,
        reply_to=str(reply_to),
        sender=str(getattr(msg, "user_id", "")),
        text=str(getattr(msg, "text", "") or ""),
        in_owner_dm=lambda owner: chat_id == owner,
        notify=notify,
    )
