"""A Telegram reply that answers a bridged tool approval (RFC bridge actions, B4d).

The notification bridge posts a pending approval to the owner's Telegram DM with
what the call will run and "reply approve or deny to this message", and links each
posted message to the approval's token by ``<chat id>:<message id>``
(``notifications.bridge_transport``). This reads the owner's reply to one of them.

It takes a message only when it is a reply, in a private chat, to a message the
registry still knows. Anything else is left to the ordinary turn path untouched,
so a group reply, a reply to any other message and a plain message all behave as
before. A reply it takes never starts a turn, and every one is decided and
audited the way a Slack press is:

* the live ``bridge_approvals`` preview must be on, else nothing is answered;
* the owner is the sender's ``from.id`` (the message's ``user_id``), matched
  against the transport's one configured owner DM (``bridge_transport.reply_owner``)
  in that owner's own private chat, never a username or display name;
* only an exact ``approve`` or ``deny`` answers, and only this one request: the
  registry maps them to ``approved`` / ``rejected`` and refuses ``trust`` and every
  other word, so no reply reaches a session-wide grant;
* a stranger's, stale, expired or second reply, or one with another word, changes
  nothing and is audited; the owner alone is told why.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

#: The transport name the registry links and audits under.
TRANSPORT = "telegram"

#: What the owner is told about a reply that answered nothing, by outcome value.
_STALE_NOTICE = "This request was already answered or has expired."
_WORD_NOTICE = "Reply approve or deny to answer this request."
_ADMITTED_NOTICE = {"approve": "Approved.", "deny": "Denied."}

#: The longest reply recorded as the audited answer; anything else is "other".
_MAX_RECORDED = 16


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
    registry = getattr(state, "bridge_actions", None)
    if registry is None:
        return False
    chat_id = str(getattr(msg, "conversation_id", ""))
    token = registry.token_for_message(TRANSPORT, f"{chat_id}:{reply_to}")
    if not token:
        return False
    # From here the reply is about a bridged approval and never starts a turn.
    from kiro_crew.notifications.bridge_action_audit import audit_action
    from kiro_crew.notifications.bridge_actions import ActionOutcome, actions_enabled
    from kiro_crew.notifications.bridge_transport import reply_owner

    # Read live, off the loop, so turning the preview off stops a reply to a note
    # posted before it. Off answers nothing and, like a Slack press, audits nothing.
    if not await asyncio.to_thread(actions_enabled):
        return True
    sender = str(getattr(msg, "user_id", ""))
    owner = reply_owner(state, TRANSPORT)
    # In a Telegram private chat the chat id IS the peer's user id, so this also
    # pins the reply to the owner's own DM.
    from_owner = bool(owner) and sender == owner and chat_id == owner
    word = str(getattr(msg, "text", "") or "").strip().lower()
    choice = word if len(word) <= _MAX_RECORDED and word.isalnum() else "other"
    outcome = registry.resolve(token, choice, from_owner=from_owner)
    audit_action(
        registry, token, outcome, transport=TRANSPORT, choice=choice, actor=sender, channel=chat_id
    )
    if not from_owner:
        return True  # A stranger is told nothing about the owner's prompt.
    if outcome is ActionOutcome.ADMITTED:
        notice = _ADMITTED_NOTICE.get(choice, "")
    elif outcome is ActionOutcome.INVALID_CHOICE:
        notice = _WORD_NOTICE
    else:
        notice = _STALE_NOTICE
    if notice:
        try:
            await notify(notice)
        except Exception:
            logger.debug("telegram: could not send a bridged-reply notice", exc_info=True)
    return True
