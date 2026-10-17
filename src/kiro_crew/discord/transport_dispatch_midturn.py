"""A message that lands while a turn is running: steer it into that turn, or queue it; one
into a busy RESUMED dashboard session goes to that slot's own machinery.

``_handle_busy`` steers a text message into the running turn when the provider takes a
steer, and queues it through ``DiscordDispatcher._enqueue_with_receipt`` otherwise. That
wrapper, the receipt flip and the drain that replays queued messages stay in
``transport_dispatch.py``, where the queue-drain and receipt guards read them. This owner
matches ``telegram/dispatch/midturn.py``, the same seam on the Telegram dispatcher.

Each function here whose first parameter is ``self`` is a ``DiscordDispatcher``
method: the class binds it by name, so it reads and writes the dispatcher's own
state on the instance, and a patch of the method on the class or on an instance
reaches every caller.

Instance fields read (a call on one included): ``_active_renderers``,
``_session_resume``, ``client``, ``sessions``.

Instance fields assigned, or whose mapping is changed: none.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kiro_crew.discord.transport_dispatch_origin import _CHANNEL, _inbound_origin
from kiro_crew.messaging.queue_receipt import STEER_ACK_EMOJI as _STEER_ACK_EMOJI
from kiro_crew.messaging.transport import InboundMessage

if TYPE_CHECKING:
    from kiro_crew.discord.transport_dispatch import DiscordDispatcher

#: The dispatcher's one logger, named for the facade module operators filter on.
logger = logging.getLogger("kiro_crew.discord.transport_dispatch")


#: The slot cannot take the message: no open tab, a closing or remote-bound
#: slot, or a lease held by something other than the dashboard turn loop
#: (Discord's own turn on the resumed key). An incognito or temporary session is
#: taken like any other: those modes keep their transcript and queue.
_RESUMED_BUSY_REFUSAL = (
    "⏳ That session is busy with a turn started elsewhere. "
    "Send it again once it finishes, or `!unlink` to go back to "
    "your own conversation."
)


async def _handle_resumed_busy(
    self: DiscordDispatcher,
    session_key: str,
    msg: InboundMessage,
    text: str,
    override_mode: str | None,
) -> None:
    """A message arrived while the RESUMED dashboard session is mid-turn.

    The same mode ladder as :meth:`_handle_busy` -- the per-message override,
    else ``messaging.queue_mode`` -- but the destination is the dashboard slot's
    own machinery (``dashboard.channel_handoff.hand_to_resumed_slot``), never this
    dispatcher's queue: that queue is drained at the tail of a DISCORD-driven
    turn and replayed with resume routing off, so an entry made while the
    dashboard drives would run later in the native session. The slot's queue is
    drained by the dashboard turn loop, so its ordering is the dashboard's.

    Every outcome is confirmed in the DM. A dashboard-driven turn's reply reaches
    this conversation, if at all, through the dashboard's own cross-surface leg
    and never through this dispatcher, so a silent hand-off would read as a drop
    and the user would resend into the same turn.
    """
    assert self.client is not None
    # Deferred, like every dashboard import in this module: the dispatcher is on
    # the gateway boot path and the dashboard package is not.
    from kiro_crew.dashboard.channel_handoff import hand_to_resumed_slot, resumed_busy_reply

    mode = override_mode or str(self._live_cfg().messaging.queue_mode)
    outcome = await hand_to_resumed_slot(
        getattr(self._session_resume, "dashboard_state", None),
        session_key,
        text,
        mode=mode,
        has_attachments=bool(msg.attachments),
        # Where a drop notice goes if the drain later refuses a queued entry,
        # and the principal the outbound recipient check needs. Only a DM route
        # supplies one: this user was authorized against ``allowed_user_ids``
        # on inbound, and a dashboard slot's session key names no Discord peer
        # of its own. A THREAD route supplies none -- the same convention the
        # mirror's recipient check follows -- because ``may_send_to`` answers a
        # thread from the thread roster and falls through to the DM arm only
        # when the thread is not on it: a principal there would let a thread the
        # operator has since removed from ``allowed_thread_ids`` receive the
        # notice, excerpt included, on the strength of a user roster it was
        # never checked against.
        channel_type=_CHANNEL,
        conversation_id=msg.conversation_id,
        principal=(msg.user_id if not msg.thread_id else ""),
    )
    if outcome.refused:
        logger.info(
            "discord: message into busy resumed session %s refused (%s)",
            session_key,
            outcome.reason,
        )
    await self.client.send_message(
        msg.conversation_id, resumed_busy_reply(outcome, busy_refusal=_RESUMED_BUSY_REFUSAL)
    )


async def _handle_busy(
    self: DiscordDispatcher,
    session_key: str,
    msg: InboundMessage,
    text: str,
    override_mode: str | None,
) -> None:
    """A message arrived mid-turn: steer the running turn or queue it."""
    assert self.client is not None
    channel_id = msg.conversation_id
    mode = override_mode or str(self._live_cfg().messaging.queue_mode)
    if mode != "queue" and not msg.attachments:
        provider = self.sessions.get_provider(session_key)
        steer = getattr(provider, "steer", None)
        # Only steer when a turn is GENUINELY in flight (see the Telegram
        # dispatcher for the post-turn-bookkeeping race rationale).
        has_active = getattr(provider, "has_active_turn", None)
        live = has_active is None or bool(has_active())
        steered = bool(
            live
            and getattr(provider, "supports_steer", False)
            and steer is not None
            and await steer(text)
        )
        if steered:
            r = self._active_renderers.get(session_key)
            if r is not None:
                r.note_steer(text)
            # Instant, no-extra-bubble ack: react to the user's steer
            # message. Best-effort.
            steer_mid = getattr(msg, "message_id", "")
            if steer_mid:
                try:
                    await self.client.add_reaction(channel_id, steer_mid, _STEER_ACK_EMOJI)
                except Exception:
                    logger.debug("discord: steer ack reaction failed", exc_info=True)
            return
    # queue mode (or !queue override, or steer unavailable). Atomic
    # enqueue + receipt under self._queue.lock — see the Telegram dispatcher.
    if not await self._enqueue_with_receipt(
        session_key,
        channel_id,
        text,
        attachments=msg.attachments,
        # The sender and their channel ride with the entry too, because the drain
        # replays it and the reply reaches whoever the replayed envelope names.
        # Under ``dm_scope = "unified"`` two allow-listed people share ONE
        # session key and therefore one queue, so without this a message queued
        # by one of them during the other's turn is answered into the other's
        # channel and attributed to them.
        origin=_inbound_origin(msg),
        person_origin=msg.person_origin,
    ):
        await self.handle_message(msg)
