"""Who sent a queued Telegram message, and where its reply goes.

A queue entry carries its sender's origin -- user, chat, Topic, chat type and
``@handle`` -- because the drain replays it under that envelope rather than under
the turn that opened the queue. This module is that record and its spelling on a
queue entry: the sender key that decides which entries may share one turn, the
owner token ``/stop`` and the receipt flip compare, and the channel tag that tells
this channel's entries from another transport's on a shared queue. Every dispatcher
path that queues, drains, flips or clears reads these helpers; the facade re-exports
each name.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from kiro_crew.messaging.queue_drain import entry_channel, owner_token, tag_entry

if TYPE_CHECKING:
    from kiro_crew.messaging.transport import InboundMessage

#: Prefix the queued origin's fields take on a queue entry, so they can never
#: collide with the entry's other payload (``attachments``, ``privacy_request``).
_ORIGIN_PREFIX = "telegram_"


#: This channel's name in the shared queue-drain contract
#: (``messaging/queue_drain.py``). ONE constant, used both to tag the entries this
#: dispatcher produces and to register its drain, because a tag that does not match the
#: registration cannot be woken for its own entries. The neutral key those entries carry
#: it under is defined in that module, not here: a per-module copy of the string fails
#: silently, making this channel's entries unowned to every drain.
_CHANNEL = "telegram"


#: Origin fields that are NOT part of "who sent this, and where does the reply go",
#: so they are excluded from :attr:`_QueuedOrigin.sender_key`. Only ``username``
#: qualifies: it is a MUTABLE label for the sender ``user_id`` already pins, and a
#: handle changed between two messages would make one person's own burst compare
#: unequal and stop the collapse the drain exists for.
#:
#: The exclusion is a DENY list, so a field added to :class:`_QueuedOrigin` later
#: joins the key by default. That direction is deliberate: a missing WHO field lets
#: two people's messages collapse into one turn under one identity, while a surplus
#: field only costs a collapse, and answering the wrong person is the worse failure.
_NOT_A_SENDER = frozenset({"username"})


class _QueuedOrigin(NamedTuple):
    """Who sent one queued message and where its reply goes.

    Recorded per QUEUED MESSAGE when it arrives, and NOT inherited from the envelope
    that opened the finished turn: under ``messaging.dm_scope = "unified"`` every
    allow-listed person's direct chat collapses into one session key
    (``build_dm_session_key`` reduces the bucket to ``unified:{agent}``, dropping
    both channel and user), so one queue holds messages from several people. A
    drained turn that ran under the opener's envelope would post one person's answer
    into another person's chat, and would name the opener as the author of text they
    did not write everywhere the turn is attributed -- its audit caller, its
    persisted transcript row, and its principal-scoped context all resolve from this
    envelope.

    These are exactly the fields the replayed ``TelegramInboundMessage`` carries, so
    ``handle_message`` re-derives the route, the session key and the reply address
    from the QUEUED message's own envelope rather than from the opener's. No
    per-message id is recorded, because the drain constructs a fresh message rather
    than copying the opener's: a drained turn is a reply to a burst, not to any one
    message. That is why the collapse trap -- grouping on a per-message identifier,
    which makes one person's burst compare unequal -- is avoided structurally here
    rather than by exclusion.
    """

    user_id: str
    chat_id: str
    thread_id: str
    chat_type: str
    username: str

    @property
    def sender_key(self) -> tuple[str, ...]:
        """Who sent this and where the reply goes, with the mutable handle dropped.

        Two entries may be collapsed into one turn exactly when these match, because
        one turn gets one envelope. Derived from ``_fields`` minus
        :data:`_NOT_A_SENDER` rather than listed by hand, so a new field cannot be
        silently left out of the comparison that keeps two people's messages apart.
        """
        return tuple(getattr(self, name) for name in self._fields if name not in _NOT_A_SENDER)


def _inbound_origin(msg: InboundMessage) -> _QueuedOrigin:
    """This message's own origin, for recording on its queue entry.

    ``thread_id`` / ``chat_type`` / ``username`` are read through ``getattr`` for the
    same reason every other consumer does: the neutral :class:`InboundMessage` stays
    channel-agnostic and only ``TelegramInboundMessage`` declares them.
    """
    return _QueuedOrigin(
        user_id=str(msg.user_id),
        chat_id=str(msg.conversation_id),
        thread_id=str(getattr(msg, "thread_id", None) or ""),
        chat_type=str(getattr(msg, "chat_type", "private")),
        username=str(getattr(msg, "username", "")),
    )


def _entry_owner(origin: _QueuedOrigin) -> str:
    """The neutral token naming the principal *origin* came from.

    Built from ``sender_key``, the same value that decides whether two queued messages
    may share one turn, so "whose entry is this" and "may these collapse together" can
    never answer differently. ``/stop`` compares it to drop one person's queued messages
    and leave everybody else's.
    """
    return owner_token(_CHANNEL, origin.sender_key)


def _origin_kwargs(origin: _QueuedOrigin) -> dict[str, str]:
    """An origin as prefixed queue-entry keyword arguments, plus the neutral channel.

    The channel rides with them because a drain must be able to tell an entry it owns
    from one another transport recorded BEFORE it reads any channel-specific field,
    and because the value names which peer drain to wake for a foreign entry. The owner
    rides with them for the mirror reason on the clear side: ``/stop`` must tell one
    person's entries from another's across every transport on the queue, and the
    prefixed fields below are unreadable to it on a foreign entry.
    """
    recorded = {f"{_ORIGIN_PREFIX}{name}": value for name, value in origin._asdict().items()}
    return tag_entry(recorded, _CHANNEL, _entry_owner(origin))


def _queued_origin(kwargs: dict) -> _QueuedOrigin | None:
    """The origin recorded on a queue entry, or None if ANOTHER channel recorded it.

    One queue can hold entries from more than one transport. Every DM dispatcher is
    constructed with the orchestrator's single ``SessionManager``
    (``telegram/gateway.py``, ``discord/gateway.py``, ``teams/transport_dispatch.py``),
    and under ``messaging.dm_scope = "unified"`` ``build_dm_session_key`` reduces a
    direct chat's bucket to ``unified:{agent}`` -- dropping the CHANNEL as well as the
    user -- so a Telegram DM and a Discord DM to the same agent resolve to the same
    session key, and therefore the same queue.

    Such an entry is not this dispatcher's to replay: it carries no field this channel
    can address, and answering it here would post one transport's reply into another
    transport's conversation. So None means DEFER, never raise and never guess. The
    drain re-enqueues it untouched and wakes the channel that owns it. Raising here
    instead would be worse than the bug this module prevents: the entry is already
    dequeued when this runs, so an exception would discard every message dequeued in
    that iteration, and the remainder is re-enqueued only after the loop.

    Ownership is decided on the NEUTRAL channel field, not on the presence of a
    prefixed one, so an entry that names this channel but is missing a field raises a
    ``KeyError`` naming it. That case is a producer bug in this channel's dispatcher --
    both producers are ``TelegramDispatcher``'s own, ``_enqueue_with_receipt`` and the
    drain's re-enqueue --
    and defaulting to empty strings would address the reply to an empty chat id,
    which is a silent misdelivery.
    """
    if entry_channel(kwargs) != _CHANNEL:
        return None
    return _QueuedOrigin(
        *(str(kwargs[f"{_ORIGIN_PREFIX}{name}"] or "") for name in _QueuedOrigin._fields)
    )
