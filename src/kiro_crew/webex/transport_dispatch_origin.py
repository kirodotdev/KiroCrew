"""Where a Webex message belongs and where its reply goes.

This module holds the channel's name in the shared queue-drain contract (``_CHANNEL``)
and the prefix its queue-entry fields take (``_ORIGIN_PREFIX``); the place one queued
message records (``_QueuedPlace``: room, thread root, sender and room type, with the
``sender_key`` two entries must share to collapse into one turn); the owner token an
entry carries (``_entry_owner``); the read that tells this channel's entries from
another transport's (``_queued_place``); the envelope a drained turn answers under
(``_reply_envelope``); and the route spellings: ``_route_of`` names the conversation a
message belongs to, ``_chat_type_of`` the session-key namespace that route takes, and
``_ROOT_THREAD`` the receipt address of a room root. The queue, its receipts and its
drain stay in ``transport_dispatch.py``, where the queue-drain and receipt guards read
them. This owner matches ``discord/transport_dispatch_origin.py``, the same seam on the
Discord dispatcher. New addressing work goes here.
"""

from __future__ import annotations

from dataclasses import replace
from typing import NamedTuple

from kiro_crew.messaging.link import CHAT_TYPE_DIRECT, CHAT_TYPE_FORUM
from kiro_crew.messaging.queue_drain import entry_channel, owner_token
from kiro_crew.webex.client import WebexInbound
from kiro_crew.webex.transport import ROOM_DIRECT

#: This channel's name in the shared queue-drain contract
#: (``messaging/queue_drain.py``). ONE constant, used both to tag the entries this
#: dispatcher produces and to register its drain, because a tag that does not match the
#: registration cannot be woken for its own entries. The neutral key those entries carry
#: it under is defined in that module, not here: a per-module copy of the string fails
#: silently, making this channel's entries unowned to every drain.
_CHANNEL = "webex"

#: Prefix the queued envelope fields take on a queue entry.
_ORIGIN_PREFIX = "webex_"


class _QueuedPlace(NamedTuple):
    """Where one queued Webex message's reply goes, and who sent it.

    Recorded per QUEUED MESSAGE and NOT inherited from the envelope that opened the
    finished turn: under ``messaging.dm_scope = "unified"`` every allow-listed person's
    direct room collapses onto one session key, so one queue holds messages from several
    people, and a turn replayed under the opener's envelope answers one person's text in
    another person's room, attributed to them.

    ``room_type`` rides along because it is what ``_route_of`` reads to decide whether a
    message routes to a person or to a space, and therefore which SESSION KEY the replay
    resolves to. A replay that inherited the wrong one would run against a different
    session than the one the message was queued on.
    """

    room_id: str
    parent_id: str
    person_email: str
    room_type: str

    @property
    def sender_key(self) -> tuple[str, str, str]:
        """WHO sent this and WHERE the reply goes: sender, room, and thread root.

        Two entries may be collapsed into one turn exactly when all three match, because
        one turn gets one envelope and one audited caller.

        The sender is in the key, not just the conversation. A GROUP SPACE routes as
        ``space:{room_id}``, and ``_session_key`` takes that route -- so every
        allow-listed member of one space shares one session key and one queue under ANY
        ``dm_scope``, not only under ``unified``. Keying on room and thread alone would
        collapse two members' mid-turn messages into one turn, and
        :func:`_reply_envelope` stamps the FIRST entry's email, so the second member's
        text would run and be SEL-audited as the first member. Delivery into the shared
        space would look right, which is what makes the misattribution the lasting half.

        Room and thread stay in the key beside the sender: a different room is reachable
        whenever a unified key puts two people on one queue, and a different thread
        whenever ``reply_in_thread`` is on, so mixing either would answer text into a
        conversation it did not come from.
        """
        return (self.person_email, self.room_id, self.parent_id)


def _entry_owner(inbound: "WebexInbound") -> str:
    """The neutral token naming the principal *inbound* came from.

    Built from the same three values ``_QueuedPlace.sender_key`` compares -- sender, room
    and thread root -- so "whose entry is this" and "may these collapse into one turn"
    can never answer differently. ``/stop`` compares it to drop one person's queued
    messages and leave everybody else's, which matters under ANY ``dm_scope`` here: a
    group space routes as ``space:{room_id}``, so every allow-listed member of one space
    already shares a session key and a queue.
    """
    return owner_token(_CHANNEL, (inbound.person_email, inbound.room_id, inbound.parent_id))


def _queued_place(kwargs: dict) -> _QueuedPlace | None:
    """An owned entry's place, or None if ANOTHER channel recorded it.

    Ownership is decided on the NEUTRAL channel tag, never on whether a ``webex_`` key
    happens to be present. That distinction is the whole fix: a foreign entry carries no
    ``webex_room_id``, and reading it with a default produced ``""`` -- which then became
    THE room for the drained turn, so another transport's message was answered into an
    empty room under an empty identity, and was consumed off the queue while doing it.

    For an entry this channel DID record, the fields are REQUIRED: a missing key raises a
    ``KeyError`` naming it, and an empty ``room_id`` raises too, because that is the value
    that would address a reply at nothing. Both producers are in ``transport_dispatch.py``
    (``_enqueue_with_receipt``, and the drain's own re-enqueue which passes an entry's own
    kwargs straight back), so either is a producer bug here -- and a silent misdelivery is
    a worse outcome than a loud failure. The queue is an in-process list on a live
    session, so there is no entry persisted by an older build to be lenient towards.
    """
    if entry_channel(kwargs) != _CHANNEL:
        return None
    place = _QueuedPlace(
        room_id=str(kwargs[f"{_ORIGIN_PREFIX}room_id"] or ""),
        parent_id=str(kwargs[f"{_ORIGIN_PREFIX}parent_id"] or ""),
        person_email=str(kwargs[f"{_ORIGIN_PREFIX}person_email"] or ""),
        room_type=str(kwargs[f"{_ORIGIN_PREFIX}room_type"] or ""),
    )
    if not place.room_id:
        raise KeyError(f"{_ORIGIN_PREFIX}room_id")
    return place


def _reply_envelope(
    inbound: "WebexInbound | None", place: _QueuedPlace, *, person_origin: bool
) -> "WebexInbound":
    """The envelope a drained turn answers under: the QUEUED entry's, never the opener's.

    *person_origin* is the queued entries' own flag, never the opener's: the finished
    turn can have been opened by a gateway-built wake, and a wake can itself have been
    queued (kiro_crew.start_priority).

    Built on *inbound* when the finished turn had one, so a field this replay does not
    address (the spool's ``message_id``, card inputs) keeps whatever that turn carried.
    When a PEER channel woke this drain there is no such envelope, and a bare one is
    correct rather than a fallback: every field that addresses or attributes the reply
    comes from *place*, and deliberately stashing a "last seen" inbound would reintroduce
    the defect this function fixes, one channel further out.
    """
    base = inbound if inbound is not None else WebexInbound(person_email="", room_id="", text="")
    return replace(
        base,
        room_id=place.room_id,
        parent_id=place.parent_id,
        person_email=place.person_email,
        room_type=place.room_type,
        person_origin=person_origin,
    )


#: Scope-path segment marking a route that addresses a SPACE, not a person. A
#: Webex room id is an opaque base64 blob with no colon, so this prefix is what
#: tells the two route kinds apart everywhere one is read.
_SPACE_ROUTE_PREFIX = "space:"

#: The thread component of a receipt address when the bubble sits in the room
#: ROOT rather than under a thread -- either because ``reply_in_thread`` is off,
#: or because the message it receipts arrived outside any thread.
#:
#: A stand-in is required rather than an empty component:
#: :func:`~kiro_crew.messaging.queue_receipt.receipt_address_key` reads an empty
#: part as UNKNOWN and returns no address at all, which opens no bubble. A room
#: root IS a nameable conversation, so it gets a name. The colon keeps it out of
#: the value space it shares: a Webex message id is an opaque base64 blob with no
#: colon, so no real thread root can collide with this.
_ROOT_THREAD = "root:none"


def _route_of(inbound: "WebexInbound") -> str:
    """The conversation *inbound* belongs to.

    A direct room is the SENDER's conversation. A group space is the SPACE's,
    shared by everyone in it — the same semantics a Telegram group Topic gets,
    and the reason a space must never resolve to a participant's private DM key:
    that key carries their private history, is the target a mid-turn DM steers
    into, and is what their ``/new`` resets.

    Returned as ONE string that carries both the namespace and the identity, so
    the ``ConversationState`` bucket, the generation seed and the session key
    cannot disagree about which conversation a message routes to. A second
    parameter beside the id would be forgettable at exactly one call site, and
    the failure there is silent: the space quietly borrows a DM.
    """
    if inbound.room_type == ROOM_DIRECT:
        return inbound.person_email
    return f"{_SPACE_ROUTE_PREFIX}{inbound.room_id}"


def _chat_type_of(route: str) -> str:
    """The session-key namespace *route* belongs in.

    A space is ``forum``, which is what keeps it out of the ``unified`` DM bucket:
    ``build_dm_session_key`` collapses only ``direct`` routes, so a shared space
    can never merge into one person's cross-surface DM continuity.
    """
    return CHAT_TYPE_FORUM if route.startswith(_SPACE_ROUTE_PREFIX) else CHAT_TYPE_DIRECT
