"""A message that lands while a turn is running: steer it into that turn, or queue it;
one into a busy RESUMED dashboard session goes to that slot's own machinery.

The steer arm is one transaction with any privacy modifier the message carries: the
mode is reserved before the steer, committed once the steer lands (committed as
unconfirmed when the steer raises or is cancelled), and released only when the
provider declines. A message that is not steered is queued with its receipt through
``TelegramDispatcher._enqueue_with_receipt``. That wrapper, the receipt flip and the
drain that replays queued messages stay in ``transport_dispatch.py``, where the
queue-drain and receipt ratchets read them.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import TYPE_CHECKING

from kiro_crew.messaging import privacy_mode
from kiro_crew.messaging.queue_receipt import STEER_ACK_EMOJI as _STEER_ACK_EMOJI
from kiro_crew.telegram.dispatch import origin as _origin

if TYPE_CHECKING:
    from kiro_crew.messaging.transport import InboundMessage
    from kiro_crew.telegram.transport_dispatch import TelegramDispatcher

#: The dispatcher's one logger, named for the facade module operators filter on.
logger = logging.getLogger("kiro_crew.telegram.transport_dispatch")

#: What a typed message into a BUSY resumed dashboard session is told when the
#: slot cannot take it; every other receipt is ``channel_handoff.resumed_busy_reply``'s.
#: The slot cannot take the message: no open tab, a closing or remote-bound
#: slot, or a lease held by something other than the dashboard turn loop
#: (Telegram's own turn on the resumed key). An incognito or temporary session is
#: taken like any other: those modes keep their transcript and queue.
_RESUMED_BUSY_REFUSAL = (
    "⏳ That session is busy with a turn started elsewhere. Send your "
    "message again once it finishes, or /unlink to return to your "
    "Telegram conversation."
)


async def _handle_busy(
    self: TelegramDispatcher,
    session_key: str,
    msg: InboundMessage,
    text: str,
    override_mode: str | None,
    *,
    thread: int | None = None,
    privacy_request: str = "",
    caller: str = "system",
) -> None:
    """A message arrived mid-turn: steer the running turn or queue for after
    it. ``text`` is the message with any ``/queue``|``/steer`` directive
    stripped; ``override_mode`` ('queue' | 'steer' | None) forces the path for
    THIS message, overriding the global ``queue_mode``.

    *privacy_request* is a modifier the caller stripped off *text*, and the two
    branches owe it different things because they run the request under different
    keys. A STEERED message folds into the turn already running on
    ``session_key``, so the mode is RESERVED on that key before the steer -- row
    on disk, mark and header (``privacy_mode.reserve``) -- or the running turn's
    transcript would be written before anything marks it; a refusal means no
    steer. A landed steer commits the reservation; a steer that did not land
    releases it (the message then takes the queue path, where the request is
    applied under the drained key), so a turn the user never asked to protect is
    not left restricted. A QUEUED message runs later under whatever key the
    drained turn resolves, so the request rides ALONG with it and is applied there.
    """
    assert self.client is not None
    chat_id = int(msg.conversation_id)
    mode = override_mode or str(self._live_cfg().messaging.queue_mode)
    # An attachment-bearing message can never take the steer path: ``steer``
    # forwards TEXT ONLY, so steering a photo/document message would deliver
    # its caption and silently drop every file. Such a message always goes to
    # the queue path below, which carries ``attachments`` through the drain.
    # Mirrors discord/transport_dispatch.py's identical gate -- Telegram was
    # missing it, and album buffering makes it far more reachable: a follow-up
    # typed during the debounce window starts a turn, so the album's own flush
    # arrives mid-turn and would have been steered as caption-only.
    if mode != "queue" and not msg.attachments:
        provider = self.sessions.get_provider(session_key)
        steer = getattr(provider, "steer", None)
        # Only steer when a turn is GENUINELY in flight. ``is_busy`` stays
        # True through post-turn bookkeeping (record_success / _persist_turn
        # / _maybe_notice / SEL audit -- all await points), so without this
        # guard a steer could reach kiro-cli for a prompt that already ended
        # -> silently swallowed (no fresh turn, no queue entry), and the
        # steer-ack reaction would land on a message whose turn already
        # finished. When no live turn, fall through to the queue/handle path
        # below (mirrors the queue path's ``force=False`` fallback), so the
        # message is re-run or queued instead of lost.
        has_active = getattr(provider, "has_active_turn", None)
        live = has_active is None or bool(has_active())
        can_steer = live and bool(getattr(provider, "supports_steer", False)) and steer is not None
        reservation: privacy_mode.Reservation | None = None
        # ONE producer for everything this path tells the user about the
        # mode: the refusal (at once), the confirmation (by commit, only once
        # the steer has put the message in the turn) or the failure notice.
        announce = lambda note: self._notify(chat_id, note, thread=thread)  # noqa: E731
        if privacy_request and can_steer:
            # RESERVE before the steer. A steer cannot be taken back once it
            # lands, and the turn it folds into runs on THIS key and writes
            # its transcript when it finishes -- so the row, the mark and the
            # header must exist before the message is in that turn, not after
            # (a persist that failed after the steer would leave the turn run
            # with no durable record; two modifiers racing for the last row
            # would both steer). reserve publishes only once the row is on
            # disk -- a row that cannot be taken or written is a refusal,
            # already audited and announced: no steer, nothing runs -- and
            # hands back what a failed steer must release. It does NOT confirm
            # the mode: the steer may still decline or fail, and a "mode ON"
            # for a message that then ran elsewhere or not at all is false.
            try:
                reservation = await privacy_mode.reserve(
                    privacy_request,
                    session_key,
                    source="telegram",
                    caller=caller,
                    sessions=self.sessions,
                    notify=announce,
                )
            except privacy_mode.PrivacyModeRefused:
                return
        try:
            steered = bool(can_steer and steer is not None and await steer(text))
        except asyncio.CancelledError:
            # Cancelled mid-steer: the outcome is unknown -- the steer's
            # bytes may already be with the backend -- so the mode STANDS
            # (fail-closed: taking it back would strip the protection from
            # a message that may be recorded). Committed silently; the
            # cancellation goes through.
            if reservation is not None:
                with suppress(Exception):
                    await privacy_mode.commit(reservation, unconfirmed=True)
            raise
        except BaseException:
            # The steer RAISED after the message may have reached the
            # backend (the write lands before the awaited flush that
            # fails), so nobody knows whether it is in the turn. Keep the
            # mode -- row, mark and header exactly as a landed steer leaves
            # them -- and tell the user the mode is on but the message
            # itself is unconfirmed; then let the failure propagate as
            # before. Only an explicit decline (``steer`` returning False)
            # releases: that message provably runs elsewhere. The notice
            # is best-effort, as in the arm above: ``commit`` records the
            # mode BEFORE it sends, so a sender that raises has changed
            # nothing else, and letting it through here would replace the
            # steer's own exception with the notice's.
            if reservation is not None:
                with suppress(Exception):
                    await privacy_mode.commit(reservation, unconfirmed=True)
            raise
        if steered:
            if reservation is not None:
                # The message is in the turn: the mode is the conversation's
                # for good, and THIS is when the user is told so.
                await privacy_mode.commit(reservation)
            # Record the user's OWN words on the running turn's renderer so
            # it can render an inline "↪️ steered: <text>" chip (never the
            # redacted backend echo). Best-effort: no active renderer -> skip.
            r = self._active_renderers.get(session_key)
            if r is not None:
                r.note_steer(text)
            # Instant, no-extra-bubble ack: react to the user's steer message
            # so a mid-turn steer isn't silent while it waits for the next
            # generation boundary. The steered reply lands at the end of the
            # turn's output (no pre/post split -- that retroactive slice of a
            # single stream leaked fragments across the cut). Best-effort --
            # reactions need Bot API 7.0+.
            steer_mid = getattr(msg, "message_id", 0)
            if steer_mid:
                try:
                    await self.client.set_message_reaction(chat_id, steer_mid, _STEER_ACK_EMOJI)
                except Exception:
                    logger.debug("telegram: steer ack reaction failed", exc_info=True)
            return
        if reservation is not None:
            # The steer did not land (the provider declined it): the message
            # falls through to the queue path below and runs at the drain,
            # where ``privacy_request`` is applied under the drained key. The
            # reservation is RELEASED -- marking this key for a message that
            # never ran here would restrict a turn the user never asked to
            # protect -- unless another modifier on this thread is riding it
            # or has landed, which release checks before loosening anything.
            await privacy_mode.release(
                reservation, sessions=self.sessions, source="telegram", caller=caller
            )
    # queue mode (or /queue override, or steer unavailable). Enqueue + receipt
    # happen atomically under ``self._queue.lock`` (see ``_enqueue_with_receipt``)
    # so the end-of-turn drain -- which takes the same lock to dequeue + flip
    # -- cannot interleave between the enqueue and the receipt and orphan a
    # bubble. If the turn finished in the window the message is not queued, so
    # we run it now (re-entering handle_message, which re-strips the directive
    # and runs it as a fresh turn) instead of stranding it.
    if not await self._enqueue_with_receipt(
        session_key,
        chat_id,
        text,
        thread=thread,
        attachments=list(msg.attachments) if msg.attachments else None,
        privacy_request=privacy_request,
        # The sender and their chat ride with the entry too, because the drain
        # replays it and the reply reaches whoever the replayed envelope names.
        # Under ``dm_scope = "unified"`` two allow-listed people share ONE
        # session key and therefore one queue, so without this a message queued
        # by one of them during the other's turn is answered into the other's
        # chat and attributed to them. Built from ``msg`` rather than from this
        # method's ``chat_id`` / ``thread``: ``thread`` here is the REPLY thread
        # the route resolved to, while the replay needs the message's own
        # ``thread_id`` so ``handle_message`` re-derives that route itself.
        origin=_origin._inbound_origin(msg),
        person_origin=msg.person_origin,
    ):
        # Not queued, so re-run it now. The ORIGINAL msg, whose text still
        # carries the modifier, so command parsing re-derives the request rather
        # than this path having to re-thread it.
        await self.handle_message(msg)


async def _handle_resumed_busy(
    self: TelegramDispatcher,
    session_key: str,
    msg: InboundMessage,
    text: str,
    override_mode: str | None,
    *,
    thread: int | None,
    route: tuple[str, str],
    interpret_commands: bool,
    drain: bool,
    principal: str = "",
) -> None:
    """A message arrived while the RESUMED dashboard session is mid-turn.

    Same mode ladder as :meth:`_handle_busy` (the per-message override, else
    ``messaging.queue_mode``), but the destination is the dashboard slot's own
    machinery (``dashboard.channel_handoff.hand_to_resumed_slot``), never this
    dispatcher's queue: that queue drains only at the tail of a TELEGRAM-driven
    turn and replays with resume routing off, so an entry made while the
    dashboard drives would run later in the NATIVE session. Every outcome is
    confirmed in the chat; a silent hand-off reads as a drop.

    *session_key* is the binding ``handle_message`` resolved ONCE for this message
    and every path below runs against that captured key: *route*,
    *interpret_commands* and *drain* are carried so a turn this arm starts goes
    straight to :meth:`_run_turn` instead of back through ``handle_message``,
    whose fresh binding resolution could route the message elsewhere.

    *principal* is the Telegram user admitted on inbound; it rides the entry's
    recipient stamp so a drop notice can be authorized against the roster.
    """
    chat_id = int(msg.conversation_id)
    # Deferred, like every dashboard import the dispatcher makes: it is on the
    # gateway boot path and the dashboard package is not.
    from kiro_crew.dashboard.channel_handoff import (
        REFUSED_IDLE,
        REFUSED_NO_SLOT,
        hand_to_resumed_slot,
        resumed_busy_reply,
    )

    link = self._session_resume.link_for(chat_id, thread)
    mode = override_mode or str(self._live_cfg().messaging.queue_mode)
    outcome = await hand_to_resumed_slot(
        getattr(self._session_resume, "dashboard_state", None),
        session_key,
        text,
        mode=mode,
        has_attachments=bool(msg.attachments),
        # Where a drop notice goes if the drain later refuses a queued entry,
        # and the principal the outbound recipient check needs: this user was
        # authorized against the allow-list on inbound, and a dashboard slot's
        # session key names no Telegram peer of its own.
        channel_type=link.channel_type,
        conversation_id=link.channel_id or "",
        principal=principal,
    )
    if outcome.refused:
        logger.info(
            "telegram: message into busy resumed session %s refused (%s)",
            session_key,
            outcome.reason,
        )
        if outcome.reason in (REFUSED_IDLE, REFUSED_NO_SLOT) and not self.sessions.is_busy(
            session_key
        ):
            # No dashboard turn is in progress to join (an idle slot, or no open
            # tab) and the turn that was running ended between this dispatcher's
            # busy check and the hand-off's own read: nothing holds the lease now,
            # so the message runs as a fresh turn instead of being refused. While the
            # lease IS still held (Telegram's own turn on the resumed key) the
            # refusal stands: this chat's queue replays natively.
            #
            # Straight to the turn, under the key resolved at admission -- NOT back
            # through ``handle_message``: updates run as concurrent tasks and
            # ``/unlink`` is exempt from the busy gate (the refusal even names it),
            # so a re-entry's fresh binding resolution could land this message in
            # the native session the user just left this one for.
            privacy_mode.hydrate(self.sessions, session_key)
            await self._run_turn(
                msg,
                text,
                session_key=session_key,
                resumed_key=session_key,
                route=route,
                user_id=int(msg.user_id),
                chat_id=chat_id,
                thread=getattr(msg, "thread_id", None),
                reply_thread=thread,
                interpret_commands=interpret_commands,
                drain=drain,
            )
            return
    await self._reply(
        chat_id, resumed_busy_reply(outcome, busy_refusal=_RESUMED_BUSY_REFUSAL), thread=thread
    )
