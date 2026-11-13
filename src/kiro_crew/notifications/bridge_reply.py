"""A chat reply that answers a bridged tool approval (RFC bridge actions, B4d, B4g).

The notification bridge posts a pending approval to the owner's DM on a reply
transport (``bridge_transport.REPLY_TRANSPORTS``) with what the call will run and
"reply approve or deny to this message", and links each posted message to the
approval's token by ``<conversation id>:<message id>``. Each transport's inbound
reader decides first whether a message is a reply to a message at all, and where
(Telegram: in a private chat; Discord: outside any thread), and hands only those
here; this decides and audits the answer the way a Slack press is decided:

* the live ``bridge_approvals`` preview must be on, else nothing is answered;
* the owner is the sender's own user id, matched against the transport's one
  configured owner DM (``bridge_transport.reply_owner``), and the conversation must
  be that owner's own DM (*in_owner_dm*), never a username or display name;
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

#: What the owner is told about a reply that answered nothing, by outcome value.
_STALE_NOTICE = "This request was already answered or has expired."
_WORD_NOTICE = "Reply approve or deny to answer this request."
_ADMITTED_NOTICE = {"approve": "Approved.", "deny": "Denied."}

#: The longest reply recorded as the audited answer; anything else is "other".
_MAX_RECORDED = 16


async def answer_bridged_reply(
    state: Any,
    *,
    transport: str,
    conversation_id: str,
    reply_to: str,
    sender: str,
    text: str,
    in_owner_dm: Callable[[str], bool],
    notify: Callable[[str], Awaitable[Any]],
) -> bool:
    """Answer the bridged approval a DM reply quotes. True when the reply was taken.

    *reply_to* is the id of the message the reply quotes in *conversation_id*.
    False, when no linked message matches, leaves the message to the ordinary turn
    path. *in_owner_dm* is the transport's own check that *conversation_id* is the
    given owner's DM; *notify* sends text to that conversation.
    """
    registry = getattr(state, "bridge_actions", None)
    if registry is None or not reply_to:
        return False
    token = registry.token_for_message(transport, f"{conversation_id}:{reply_to}")
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
    owner = reply_owner(state, transport)
    from_owner = bool(owner) and sender == owner and in_owner_dm(owner)
    word = text.strip().lower()
    choice = word if len(word) <= _MAX_RECORDED and word.isalnum() else "other"
    outcome = registry.resolve(token, choice, from_owner=from_owner)
    audit_action(
        registry,
        token,
        outcome,
        transport=transport,
        choice=choice,
        actor=sender,
        channel=conversation_id,
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
            logger.debug("%s: could not send a bridged-reply notice", transport, exc_info=True)
    return True
