"""Messages queued behind a running turn: their receipt, and the drain that answers them.

``_enqueue_with_receipt`` queues a mid-turn message (the queue path of
``dispatch/midturn.py``) together with its collapsing "⏳ Queued (N): …" receipt, under
``self._queue.lock``. When the turn ends, ``_drain_queue`` runs the pump through
``messaging.queue_drain.drain_until_quiet``: ``_pump_queue`` collapses one sender's
messages into one replayed turn, re-enqueues the rest in order, flips the receipt with
``_receipt_flip_locked`` and leaves a peer channel's entries for that channel's drain.
``_receipt_surface`` binds this conversation's address to the receipt, and ``/stop``
in ``transport_dispatch.py`` finalizes the same receipt through it.

The dispatcher state these read is ``sessions`` (the queue itself), ``client`` and
``_queue`` (the receipt registry and its lock); ``handle_message`` and ``_reply`` are
reached through ``self``. The queue-drain and receipt ratchets read the drain and its
flip calls in this file.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, cast

from kiro_crew.messaging import privacy_mode
from kiro_crew.messaging.attachments import IngestLimits
from kiro_crew.messaging.queue_drain import (
    drain_until_quiet,
    drain_when_free,
    entry_channel,
    entry_person_origin,
    person_tag,
)
from kiro_crew.messaging.queue_receipt import MAX_COLLAPSE as _MAX_COLLAPSE
from kiro_crew.messaging.queue_receipt import ReceiptSurface, receipt_address_key
from kiro_crew.telegram.client import frame_reply_context
from kiro_crew.telegram.dispatch import origin as _origin
from kiro_crew.telegram.transport import TelegramInboundMessage

if TYPE_CHECKING:
    from kiro_crew.telegram.client import TelegramClient
    from kiro_crew.telegram.dispatch.origin import _QueuedOrigin
    from kiro_crew.telegram.transport_dispatch import TelegramDispatcher

#: The dispatcher's one logger, named for the facade module operators filter on.
logger = logging.getLogger("kiro_crew.telegram.transport_dispatch")

# Keep queue collapse within the shared ingestion layer's per-turn file cap.
# Without this, two queued 10-photo albums would concatenate to 20 attachments
# in one turn and ingest_attachments would silently process only the first 10,
# losing the second album entirely. Mirrors discord/transport_dispatch.py.
_MAX_COLLAPSED_ATTACHMENTS = IngestLimits().max_attachments


async def _drain_queue(self: TelegramDispatcher, session_key: str) -> None:
    """Collapse the messages ONE SENDER queued during the finished turn into ONE
    combined turn (order preserved, blank-line joined).

    Dequeue and receipt flip share ``self._queue.lock`` with
    ``_enqueue_with_receipt``, so no receipt is orphaned; the combined turn runs
    outside the lock. One turn gets one envelope, so it only combines entries with
    the same :attr:`_QueuedOrigin.sender_key`, taken from the FIRST entry, never
    the opener: under ``dm_scope = "unified"`` one queue is shared by every
    allow-listed person. That is why this takes only the session key. Another
    sender's entry defers itself and everything behind it (FIFO stays exact).

    An entry ANOTHER transport recorded is set aside and its owner's drain is woken
    after this pump, outside the lock; ``messaging/queue_drain.py`` shows why the
    cascade terminates.
    """
    # Iterate rather than recurse: a message deferred by the attachment cap must
    # drain in THIS pump, not wait for unrelated user input. The pump/wake/re-pump
    # order lives in the shared module because all four drains need exactly it.
    await drain_until_quiet(
        channel=_origin._CHANNEL,
        session_key=session_key,
        pump=lambda foreign: self._pump_queue(session_key, foreign),
    )


async def _pump_queue(
    self: TelegramDispatcher, session_key: str, foreign_channels: set[str]
) -> None:
    """The collapse-and-answer loop itself. See :meth:`_drain_queue`.

    Split out so the wake has one exit point to run after: the loop returns from
    several places, and a wake that some of them skipped is the defect it exists
    to close.
    """
    while True:
        texts: list[str] = []
        # The user's OWN words, parallel to ``texts`` but WITHOUT the reply-
        # context framing. ``texts`` (framed) builds the replayed turn the agent
        # sees; ``raw_texts`` is what the "Now answering" receipt shows the chat,
        # matching the "⏳ Queued" bubble, so internal framing is never surfaced.
        raw_texts: list[str] = []
        all_attachments: list[Any] = []
        remainder: list[tuple[str, str, dict]] = []
        privacy_requests: list[str] = []
        defer_rest = False
        # The origin this iteration answers, taken from the FIRST entry it
        # collapses. None until that entry is read.
        origin: _QueuedOrigin | None = None
        # Whether a person sent any entry this turn collapses.
        person = False
        async with self._queue.lock:
            # Another turn holds the lease (dashboard, cron, nudge, or a peer
            # channel). Replaying would re-queue the text via ``_handle_busy`` and
            # flip the receipt every lap with no turn starting, so dequeue nothing;
            # ``drain_when_free`` reruns this drain once the lease frees, since the
            # holder may release without any hook.
            if self.sessions.is_busy(session_key):
                drain_when_free(
                    channel=_origin._CHANNEL,
                    session_key=session_key,
                    is_busy=self.sessions.is_busy,
                )
                logger.debug(
                    "telegram: session %s is held by another turn; its queue drains "
                    "once the lease is free",
                    session_key,
                )
                return
            # Drain the ENTIRE queue under the lock, then split: the first
            # _MAX_COLLAPSE messages FROM ONE SENDER collapse into this turn;
            # the rest are re-enqueued IN ORIGINAL ORDER (the queue is now
            # empty, so re-adding preserves FIFO) to drain after the next turn.
            # This bounds the combined prompt without dropping or reordering
            # surplus.
            while True:
                item = self.sessions.dequeue(session_key)
                if item is None:
                    break
                item_attachments = list(item[2].get("attachments") or [])
                item_origin = _origin._queued_origin(item[2])
                if item_origin is None:
                    # ANOTHER transport's entry: no address here can reach it. Set
                    # aside WITHOUT ``defer_rest`` (blocking behind it would strand
                    # this queue) and record its channel so its owner is woken.
                    remainder.append(item)
                    foreign_channels.add(entry_channel(item[2]))
                    continue
                if origin is None:
                    origin = item_origin
                # Never collapse past the shared ingestion cap: the extra files
                # would be dropped inside ingest_attachments with the user given
                # no indication, so defer instead. Mirrors the Discord drain.
                exceeds_attachment_cap = bool(
                    texts
                    and item_attachments
                    and len(all_attachments) + len(item_attachments) > _MAX_COLLAPSED_ATTACHMENTS
                )
                fits = (
                    not defer_rest
                    and len(texts) < _MAX_COLLAPSE
                    and not exceeds_attachment_cap
                    # sender_key, NOT the whole origin: the origin also carries
                    # the sender's @handle, which they can change between two
                    # messages, so comparing all of it would make one person's
                    # own burst compare unequal and drain as N turns.
                    and item_origin.sender_key == origin.sender_key
                )
                if fits:
                    # Frame each entry's own reply context onto its own text, so
                    # every queued reply carries exactly its own parent. The
                    # combined turn replays with no reply_context, so _run_turn
                    # does not frame it twice.
                    entry_reply_context = item[2].get("reply_context") or ""
                    entry_text = item[1]
                    if isinstance(entry_reply_context, str) and entry_reply_context:
                        entry_text = frame_reply_context(
                            entry_text,
                            entry_reply_context,
                            chat_id=str(item_origin.chat_id),
                            session_key=session_key,
                        )
                    texts.append(entry_text)
                    # The receipt shows the user's own words, never the framing.
                    raw_texts.append(item[1])
                    all_attachments.extend(item_attachments)
                    person = person or entry_person_origin(item[2])
                    requested = item[2].get("privacy_request") or ""
                    if isinstance(requested, str) and requested:
                        privacy_requests.append(requested)
                else:
                    # Once one message does not fit, defer it AND everything
                    # behind it, so queue order stays exact.
                    defer_rest = True
                    remainder.append(item)
            # Count only THIS sender's set-aside entries, not ``len(remainder)``:
            # other senders' and transports' entries drain in their own chats, and
            # counting them would show this sender a "+N deferred" they never sent.
            own_deferred = 0
            for _ts, rtext, rkw in remainder:
                r_origin = _origin._queued_origin(rkw)
                if (
                    origin is not None
                    and r_origin is not None
                    and r_origin.sender_key == origin.sender_key
                ):
                    own_deferred += 1
                self.sessions.enqueue(
                    session_key,
                    str(time.time()),
                    rtext,
                    force=True,
                    # Re-enqueued VERBATIM: attachments, privacy modifier and origin
                    # all survive, so a deferred entry stays protected, keeps its
                    # own identity, and another transport's entry stays intact.
                    **rkw,
                )
            if texts and origin is not None:
                await self._receipt_flip_locked(
                    session_key,
                    int(origin.chat_id),
                    raw_texts,
                    own_deferred,
                    owner=_origin._entry_owner(origin),
                )
        if not texts or origin is None:
            return
        if remainder:
            logger.debug(
                "telegram: drain set aside %d message(s) for %s, %d of them this "
                "sender's own (collapse cap %d / attachment cap %d); the rest "
                "belong to another sender or another transport. All drain in "
                "order, this sender's in the next iteration of this pump",
                len(remainder),
                session_key,
                own_deferred,
                _MAX_COLLAPSE,
                _MAX_COLLAPSED_ATTACHMENTS,
            )
        combined = "\n\n".join(texts)
        await self.handle_message(
            TelegramInboundMessage(
                channel_type="telegram",
                # Every addressing and attribution field comes from the queued
                # entry's own origin, so the turn runs in the sender's chat under
                # the sender's identity even when someone else opened the queue.
                user_id=origin.user_id,
                conversation_id=origin.chat_id,
                text=combined,
                # Carry the QUEUED message's ORIGINAL route so the drained turn
                # resolves to the SAME forum session key -- a plain DM-shaped
                # InboundMessage would drain a queued forum message under the DM
                # key instead.
                thread_id=origin.thread_id or None,
                chat_type=origin.chat_type,
                username=origin.username,
                attachments=all_attachments,
                # The queued entries' own flag: a gateway-built wake can have been
                # queued too (kiro_crew.start_priority).
                person_origin=person,
            ),
            drain=False,
            # Drained payloads are pure turn content: a queued "/new" must reach
            # the model as literal text, not execute as a command on drain.
            interpret_commands=False,
            # The privacy modifier was stripped before queueing, so it travels as
            # state. The STRICTEST in the burst wins, so a later `/incognito` is
            # never downgraded; the burst is one sender's, so it binds only them.
            privacy_request=privacy_mode.strictest(privacy_requests),
        )


# ── Mid-turn queue receipt (single, in-place, persistent record) ───────


def _receipt_surface(self: TelegramDispatcher, chat_id: int, thread: int | None) -> ReceiptSurface:
    """A receipt surface with this conversation's address already bound.

    Binding ``chat_id`` AND the forum ``thread`` here is what keeps forum
    routing out of the shared queue module: it never sees an address at all.
    """
    # cast, not assert: mypy does not carry an assert-narrowed local
    # into the nested class body below, so the closure would still see
    # ``TelegramClient | None``. The caller path always has a live client.
    client = cast("TelegramClient", self.client)
    reply = self._reply

    class _Surface:
        label = "telegram"
        # ``chat_id`` alone, deliberately: a forum route's ``comp`` is
        # ``"{chat_id}:{thread}"``, so ``_session_key`` already gives each Topic its
        # own entry and no two Topics ever share a bubble for this key to keep
        # apart. ``thread`` routes a send, which the bubble's own retained surface
        # already carries.
        address_key = receipt_address_key("telegram", chat_id)

        async def send_receipt(self, body: str) -> Any | None:
            return await reply(chat_id, body, thread=thread)

        async def edit_receipt(self, msg_id: Any, body: str) -> bool:
            return await client.edit_message(chat_id, msg_id, body)

    return _Surface()


async def _enqueue_with_receipt(
    self: TelegramDispatcher,
    session_key: str,
    chat_id: int,
    text: str,
    *,
    thread: int | None = None,
    attachments: list[Any] | None = None,
    privacy_request: str = "",
    reply_context: str = "",
    origin: _QueuedOrigin,
    person_origin: bool = False,
) -> bool:
    """Atomically enqueue a mid-turn message and create/grow its collapsing
    "⏳ Queued (N): …" receipt, under ``self._queue.lock``.

    The drain takes the same lock, so it sees this message with its receipt or
    neither, never an orphaned bubble. Returns False if the turn finished in the
    window (``enqueue`` is a no-op once the semaphore is free); the caller then
    runs it as a fresh turn.

    *origin* is REQUIRED: the drain replays the entry under it, and an
    unattributed entry under ``dm_scope = "unified"`` could only be answered under
    someone else's identity. *person_origin* sets the replay's start priority.
    """
    assert self.client is not None
    async with self._queue.lock:
        if not self.sessions.enqueue(
            session_key,
            str(time.time()),
            text,
            force=False,
            attachments=list(attachments or []),
            **person_tag(person_origin),
            # Rides WITH the message, for the same reason its attachments do:
            # the drain re-enters with `interpret_commands=False` on text the
            # modifier was already stripped from, so a request left behind here
            # is one no later parse can recover.
            privacy_request=privacy_request,
            # The reply's quoted context rides with its own entry so the drain
            # can frame it onto THIS entry's text before collapsing — kept out
            # of ``text`` so it never reached the activation gate.
            reply_context=reply_context,
            **_origin._origin_kwargs(origin),
        ):
            return False
        await self._queue.create_or_grow_locked(
            session_key, self._receipt_surface(chat_id, thread), text, _origin._entry_owner(origin)
        )
        return True


async def _receipt_flip_locked(
    self: TelegramDispatcher,
    session_key: str,
    chat_id: int,
    answered: list[str],
    deferred: int = 0,
    *,
    owner: str,
) -> None:
    """Flip the receipt to a durable "▶️ Now answering" record and drop the
    live entry so the next mid-turn burst opens a fresh receipt. Caller MUST
    hold ``self._queue.lock`` (the drain holds it across dequeue + flip).

    ``answered`` is what this turn answers (capped at ``_MAX_COLLAPSE``), so a
    >cap burst is not overstated; ``deferred`` notes the remainder.

    ``owner`` is required: in a GROUP chat one bubble lists several members'
    lines, and a drain answering one member must leave the others' alone; an
    omission is a type error, not a silent retire of the whole bubble.

    ``chat_id`` is the bubble's chat, from the queued entry's origin. The ``None``
    thread is correct: the flip only EDITS, and a message id already locates it.
    """
    assert self.client is not None
    await self._queue.flip_answering_locked(
        session_key, self._receipt_surface(chat_id, None), answered, deferred, owner=owner
    )
