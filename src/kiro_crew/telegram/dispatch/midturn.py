"""A message that lands while a turn is running: steer it into that turn, or queue it.

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
    ):
        # Not queued, so re-run it now. The ORIGINAL msg, whose text still
        # carries the modifier, so command parsing re-derives the request rather
        # than this path having to re-thread it.
        await self.handle_message(msg)
