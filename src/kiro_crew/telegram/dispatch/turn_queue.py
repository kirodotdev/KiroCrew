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
    """Collapse every message ONE SENDER queued during the just-finished turn
    into ONE combined turn (order preserved, blank-line joined) and answer them
    together, rather than replaying each as a separate turn.

    The dequeue + receipt flip run together under ``self._queue.lock`` so a
    concurrent mid-turn ``_enqueue_with_receipt`` (which takes the same lock)
    cannot interleave and leave an orphaned receipt. The combined turn itself
    runs OUTSIDE the lock -- messages that arrive during it open a fresh
    receipt and drain after the next turn. Only the queued text is replayed
    (matching what ``enqueue`` persists for DM channels: text only).

    One combined turn gets ONE envelope, so it may only combine messages that
    SHARE one -- same sender, same chat, same Topic. That is
    :attr:`_QueuedOrigin.sender_key`, and it is taken from the FIRST entry this
    iteration collapses, never from the turn that opened the queue: under
    ``dm_scope = "unified"`` one session key, and therefore one queue, is shared
    by every allow-listed person, so a queue holding two of them is reachable on
    the live path. Anything from a different sender or place defers itself and
    everything behind it, so FIFO stays exact and the outer loop drains it next
    as its own turn under its own envelope.

    Which is why this method is given the session key and nothing else: the
    opener's identity is not an input it could accidentally fall back to.

    An entry ANOTHER transport recorded shares this queue under the same scope and
    cannot be answered here at all. It is set aside, and because it has already
    been accepted and receipted, its owner's drain is woken once this pump is done
    -- outside ``self._queue.lock``, since that drain takes its own lock and runs a
    whole turn. See ``messaging/queue_drain.py`` for why the cascade terminates.
    """
    # Iterate rather than recurse: one burst can span multiple
    # attachment-capped turns, and a message deferred by the cap must drain
    # in THIS pump rather than waiting for unrelated future user input.
    # Mirrors the Discord drain.
    #
    # Channels whose entries this pump set aside, so they can be woken after it.
    # The sequence -- pump, then wake outside the queue lock but INSIDE this
    # channel's active marker, then pump again for any wake a peer could not
    # deliver back here -- lives in the shared module, because all four drains
    # need exactly it and getting the order wrong has no local symptom.
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
            # Something else holds this session's lease: a dashboard, cron or
            # auto-nudge turn on the same key, or another channel's turn under a
            # unified ``dm_scope``. Replaying now would only reach ``_handle_busy``,
            # which queues the same text again, and the next iteration would
            # dequeue it straight back -- flipping the receipt to "Now answering"
            # every lap without a turn ever starting. So nothing is
            # dequeued and nothing is flipped. The entries stay queued in order,
            # and ``drain_when_free`` runs this drain again once the lease is
            # free: that holder's turn may be one no channel drain follows, and
            # it may hand its permit back without a release hook ever running.
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
                    # ANOTHER transport recorded this entry, so it is not this
                    # dispatcher's to answer -- it holds no address this channel
                    # can reach. Set aside for its own channel's drain WITHOUT
                    # ``defer_rest``: order matters within one sender's messages,
                    # which ``sender_key`` already keeps exact, while blocking
                    # this channel's own queue behind a foreign entry would
                    # strand it whenever that transport sends nothing further.
                    # Remember WHOSE it is: the entry was already accepted and
                    # receipted, so its owner is woken once this pump is done.
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
                    texts.append(item[1])
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
            # How many of the set-aside entries belong to the sender this turn
            # answers. NOT ``len(remainder)``: that also counts entries from a
            # DIFFERENT sender and entries another TRANSPORT recorded, each of
            # which drains in its own turn in its own chat. Showing those to this
            # sender would promise them a follow-up for messages they never sent
            # -- and when their own burst fit in one turn, a "+N deferred" where
            # their true count is zero.
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
                    # Re-enqueued VERBATIM: its attachments, its privacy modifier
                    # (a deferred message drains in a LATER iteration of this pump,
                    # and dropping the request here would unprotect exactly the
                    # messages the collapse cap pushed back), and its origin -- an
                    # entry deferred because it came from SOMEONE ELSE would
                    # otherwise inherit the next first entry's identity, the bug
                    # one iteration later. Passing the payload through rather than
                    # rebuilding it is also what lets an entry another transport
                    # recorded survive this drain intact.
                    **rkw,
                )
            if texts and origin is not None:
                await self._receipt_flip_locked(
                    session_key,
                    int(origin.chat_id),
                    texts,
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
            # The one exception, and it is not a command being executed: a privacy
            # modifier was already parsed and stripped before this text was
            # queued, so it travels as state rather than as text. The STRICTEST of
            # the collapsed messages wins, because they answer as one turn under
            # one key -- honouring only the first would let a later `/incognito`
            # in the same burst be silently downgraded to whatever led it. It is
            # scoped to ONE sender's messages, so one person's modifier never
            # restricts a turn answering someone else.
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
    origin: _QueuedOrigin,
    person_origin: bool = False,
) -> bool:
    """Atomically enqueue a mid-turn message and create/grow its collapsing
    "⏳ Queued (N): …" receipt, under ``self._queue.lock``.

    Holding the lock across BOTH the enqueue and the receipt bookkeeping is
    what makes this race-free against the end-of-turn drain (which takes the
    same lock to dequeue + flip): the drain either sees this message queued
    WITH its receipt or sees neither yet -- never a half state that would
    orphan a bubble. Returns True if queued; False if the turn finished in
    the window (``enqueue`` is a no-op once the semaphore is free), so the
    caller runs the message as a fresh turn instead.

    *origin* is REQUIRED and keyword-only: it is who sent THIS message and where
    its reply goes, and the drain replays the entry under it. A default would be
    a way to enqueue an unattributed message, which under
    ``dm_scope = "unified"`` the drain could only answer under someone else's
    identity. *person_origin* is the message's own
    ``InboundMessage.person_origin``, which the drained replay's start priority
    is read from.
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

    ``answered`` is the subset actually answered by this turn (capped at
    ``_MAX_COLLAPSE``); the count reflects it -- not the full queued list --
    so a >cap burst doesn't overstate what this turn answers. ``deferred``
    (>0 only past the cap) is noted so the remainder isn't silently implied.

    ``owner`` is WHOSE messages those are, which the flip needs because one bubble
    can list several principals': a GROUP chat gives every member one chat address
    and one session key, so a drain answering one member must leave the others'
    lines -- and the entry that is their only handle -- alone. REQUIRED and
    keyword-only, the same way the registry transition it forwards to spells it:
    this wrapper has exactly one caller and that caller always can name the
    principal, so an omission is a type error rather than a silent return to
    retiring the whole bubble.

    ``chat_id`` is the chat the receipt BUBBLE lives in, which the drain takes
    from the queued entry's own origin rather than from the turn that opened the
    queue -- ``create_or_grow_locked`` posted that bubble into the chat of
    whoever queued first. The ``None`` thread is correct and not an omission:
    ``flip_answering_locked`` only ever EDITS, and ``edit_message`` addresses a
    message by its id, which already identifies it within its Topic.
    """
    assert self.client is not None
    await self._queue.flip_answering_locked(
        session_key, self._receipt_surface(chat_id, None), answered, deferred, owner=owner
    )
