"""A Discord reply that answers a bridged tool approval (RFC bridge actions, B4g).

It takes a message only when it is a reply, outside any thread, to a message the
registry still knows; ``notifications.bridge_reply`` decides and audits it. Anything
else is left to the ordinary turn path untouched, so a thread reply, a reply to any
other message and a plain message all behave as before.

The owner is the sender's user id from the gateway's own ``author.id``. A Discord DM
channel id is unrelated to the user id, so the reply is pinned to the owner's own
DM through the transport's DM pairing (``DiscordTransport.direct_peer_of``), the
record only DM-gated writers set and that session control's owner-DM check reads.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from kiro_crew.notifications.bridge_reply import answer_bridged_reply

#: The transport name the registry links and audits under.
TRANSPORT = "discord"


async def consume_bridged_reply(
    state: Any, msg: Any, notify: Callable[[str], Awaitable[Any]]
) -> bool:
    """Answer the bridged approval *msg* replies to. True when *msg* was taken.

    False leaves *msg* to the ordinary turn path. *notify* sends text to the
    channel *msg* came from.
    """
    reply_to = str(getattr(msg, "reply_to_message_id", "") or "")
    if not reply_to or getattr(msg, "thread_id", None):
        return False
    channel_id = str(getattr(msg, "conversation_id", ""))

    def in_owner_dm(owner: str) -> bool:
        live = state.get_channel_transport(TRANSPORT)
        return live is not None and live.direct_peer_of(channel_id) == owner

    return await answer_bridged_reply(
        state,
        transport=TRANSPORT,
        conversation_id=channel_id,
        reply_to=reply_to,
        sender=str(getattr(msg, "user_id", "")),
        text=str(getattr(msg, "text", "") or ""),
        in_owner_dm=in_owner_dm,
        notify=notify,
    )
