"""Mid-turn queue receipts — the single collapsing "⏳ Queued (N): …" bubble.

A message that arrives while a turn is running is either folded into that turn
as a steer or queued for after it. Queued messages get ONE receipt bubble that
is edited in place as the burst grows, then flipped to a durable record when the
turn drains it ("▶️ Now answering") or cancelled ("🛑 Cancelled"). Two channels
grew the same subsystem independently -- Telegram and Discord, ~560 duplicated
lines -- and this module is the half of it that is genuinely channel-neutral.

What lives here and what does NOT:

* HERE -- the receipt registry, its lock, and the three lifecycle transitions
  (create/grow, flip-to-answering, finalize-cancelled). These are pure
  bookkeeping over an opaque message id, and every line of them was identical
  across the two channels apart from the address type and the send call.
* NOT here -- ``_handle_busy`` and ``_drain_queue``. They re-enter the channel's
  own ``handle_message`` (whose signature differs per channel: route/chat_id/
  thread vs user_id/channel_id/thread_id) and they own the ``_active_renderers``
  registry. Sharing them would need a ``run_turn`` callback that buys nothing
  and couples this module to turn execution.

Channels reach the transitions through :class:`ReceiptSurface`, whose address is
bound at CONSTRUCTION -- so nothing below ever sees a ``chat_id``, a ``thread``
or a ``channel_id``, which is what let the five address-shaped divergences
between the two copies collapse to zero.

Dependency direction is ``<channel> -> messaging`` (never the reverse), matching
``messaging/dispatch.py``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)

#: Verbatim items shown in a receipt before "…and N more". A large mid-turn
#: burst would otherwise grow the rendered receipt past a channel's message
#: limit; the count prefix still reflects the true total.
RECEIPT_MAX_ITEMS = 5

#: Records one bubble may owe at once. Each is already bounded -- a terminal entry
#: stores what :func:`receipt_text` rendered, never the burst behind it -- so this
#: bounds the LIST, which grows only while a channel refuses every write in a row.
#: On overflow the OLDEST is released: the newest record is the one that corrects
#: what the reader can currently see, so losing it would leave the visible state
#: wrong, while losing the oldest loses one earlier burst's record alone. What is
#: released is counted and named on the next record that reaches the reader, so a
#: shortened list cannot pass for a burst that produced no such records.
RECEIPT_MAX_OWED = 4

#: Refused publications in a row, with nothing landing, before a debt is GIVEN UP.
#: :data:`RECEIPT_MAX_OWED` bounds how much one debt holds; this and
#: :data:`RECEIPT_MAX_DEBTS` bound how LONG it is held and how MANY are held, which is
#: the rest of the same policy: retain a debt while it is plausibly publishable, and
#: while the registry has room for it.
#:
#: The count is of refusals, not of elapsed time, because each refusal is fresh evidence
#: that this conversation takes no writes while a clock only says nobody spoke. Each
#: refusal has already tried an edit AND a post, so this many of them running is strong
#: evidence rather than a rate-limit window. It restarts on any body that LANDS and on
#: any new record joining the debt, which together give the bound its guarantee: a record
#: is given up only after being offered, and refused, this many times itself.
#:
#: What keeps arriving in the case this exists for is mid-turn messages meeting a debt
#: that cannot publish, each turned away with no bubble of its own -- so the count is
#: also how many acknowledgements one debt may cost before it stops being worth keeping.
#:
#: Giving up matters because the entry is the registry's only entry for its session key,
#: and a key can span several conversations: one permanently unwritable chat holds the
#: key against every healthy sibling on it, so none of them gets a receipt either. The
#: cost of giving up is the bubble in the dead conversation keeping its "⏳ Queued" text,
#: which no write could have corrected anyway.
RECEIPT_MAX_PUBLISH_ATTEMPTS = 5

#: Terminal entries the registry retains at once. A debt is retried only by a later
#: transition ON ITS OWN KEY, so a key that stops being addressed -- a session key
#: rotates its ``:gen{N}`` suffix on reset -- leaves a debt nothing will ever visit
#: again, and per-debt attempts cannot expire what is never attempted. This is the bound
#: that reaches those: past it the LEAST RECENTLY retained debt is released, which is
#: the one whose key has been silent longest and so the one most likely to be orphaned.
#: What it releases is COUNTED, for the same reason the body cap counts what it drops.
RECEIPT_MAX_DEBTS = 64

#: Instant, no-extra-bubble acknowledgement that a mid-turn steer was accepted
#: and folded into the running turn (not merely "seen" — 👀 reads as passive).
STEER_ACK_EMOJI = "🫡"

#: Upper bound on how many queued messages collapse into a single combined turn.
#: A single human will not realistically burst past this mid-turn; anything beyond
#: stays queued and drains after the next turn. Lives here rather than in each
#: dispatcher because it bounds the same collapse in every channel that carries
#: the queue, and three copies had already drifted apart by comment alone.
MAX_COLLAPSE = 50

#: What a receipt SHOWS for a message whose only content was an upload. An
#: attachment-only message has no text, and a blank line in the bubble reads as a
#: message the queue lost. Lives here because every channel that ingests files
#: needs the same substitution in both receipt transitions.
ATTACHMENT_PLACEHOLDER = "[attachment]"


#: Joins an address key's parts. Cannot occur inside a provider id or a service
#: URL, so two different addresses can never join to the same key -- a plain ":"
#: would let ("https://a", "b:c") and ("https://a:b", "c") collide.
_ADDRESS_SEP = "\x00"


def receipt_address_key(label: str, *parts: object) -> str:
    """The stable id of the ONE conversation a surface's message ids are valid in.

    Pass the values that name the conversation the bubble is SEEN in, and it must
    be at least as fine as the session key: two conversations the session key
    collapses onto one entry share one bubble, and this key is then the only thing
    that can keep one of them out of the other's. Two conversations the session key
    already separates never meet in an entry, so a key that cannot tell them apart
    costs nothing there.

    That is why two channels with the same shape answer differently. A Telegram
    forum route carries the Topic in the session key, so two Topics hold separate
    entries and ``chat_id`` alone is enough. A Webex group space routes as
    ``space:{room_id}``, so two THREADS of one room share an entry and the thread
    belongs in the key beside the room.

    Every part comes from the provider's own inbound payload, so the key names a
    conversation the provider assigned and nothing the agent can choose.

    Returns ``""`` when any part is missing, which reads as UNKNOWN rather than as
    an address: :meth:`QueueReceipt.addressed_by` matches no key against it, not
    even another empty one, and :meth:`ReceiptQueue.create_or_grow_locked` opens no
    bubble on a surface that cannot name its address. Fail closed -- without an
    address there is no way to tell the bubble's own conversation from any other.
    A caller whose component is legitimately absent -- a Webex receipt in the room
    root rather than under a thread -- passes a nameable stand-in for it, because
    "the room root" is a conversation and "unknown" is not.
    """
    values = [str(part) for part in parts]
    if not label or not all(values):
        return ""
    return _ADDRESS_SEP.join([label, *values])


def short(text: str, limit: int = 40) -> str:
    """Collapse whitespace and truncate for compact receipt display."""
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def receipt_text(
    texts: list[str],
    *,
    answering: bool = False,
    cancelled: bool = False,
) -> str:
    """Render the single collapsing receipt for ``texts`` (order preserved).

    Only the first :data:`RECEIPT_MAX_ITEMS` are listed verbatim; the count
    prefix still reflects the true total.
    """
    count = len(texts)
    items = " · ".join(f"“{short(t)}”" for t in texts[:RECEIPT_MAX_ITEMS])
    if count > RECEIPT_MAX_ITEMS:
        items += f" · …and {count - RECEIPT_MAX_ITEMS} more"
    if cancelled:
        return f"🛑 Cancelled ({count}): {items}"
    if answering:
        return f"▶️ Now answering ({count}): {items}"
    return f"⏳ Queued ({count}): {items}"


@dataclass(frozen=True)
class ReceiptLine:
    """One queued message as the receipt lists it: whose it is, and what it shows.

    The owner is the token :func:`kiro_crew.messaging.queue_drain.owner_token` builds,
    the same value the queue entry itself carries, so the bubble and the queue agree
    about who queued what. Empty for a producer that cannot name its principal, which
    makes the line nobody's to withdraw.

    The address is :func:`receipt_address_key` for the surface this line ARRIVED on,
    and it is what decides where the line may be shown. Under a shared session key
    one bubble can list lines from several conversations, and a line may only be
    rendered back into the conversation it came from -- so every body written to the
    bubble is built from :meth:`QueueReceipt.texts_at_address`, never from the whole
    list. Empty when the surface could not name its address, which shows the line
    nowhere.
    """

    owner: str
    text: str
    address: str = ""


@dataclass
class QueueReceipt:
    """The single, in-place receipt bubble tracking messages queued mid-turn.

    ``msg_id`` is deliberately opaque (``Any``): Telegram message ids are ints
    and Discord's are strings, the two are never interleaved in one process, and
    a generic parameter would add ceremony without catching a real mixup -- the
    id is only ever handed straight back to the surface that produced it.

    One registry entry serves a whole session key, and that key can span several
    conversations: under ``messaging.dm_scope = "unified"`` every allow-listed
    person's direct chat collapses into one key, and a group space routes as
    ``space:{room_id}`` under ANY scope. So the lines on one bubble may come from
    several conversations while ``msg_id`` names a message in exactly ONE of them --
    the one the bubble was opened in. Everything below follows from that single
    pairing, and all of it is decided on the ADDRESS rather than on who is calling:

    * only the bubble's own address may write to the bubble. ``msg_id`` is a
      per-conversation number, so the same integer in another chat is an unrelated
      message and editing it there would rewrite a stranger's post.
    * a body written to the bubble may quote only the lines that arrived at that
      same address (:meth:`texts_at_address`). One person's text must not appear in
      another person's chat, and the bubble's chat is somebody's chat.

    Deciding on the address and not on the principal is what makes both rules true
    on the two shared-key routes at once. In a group space every member's surface
    binds the same ``room_id``, so a second member's message both may and must
    update the shared bubble; under unified DM scope two members' surfaces bind
    different chats, so neither may write to the other's. A test on who is calling
    cannot tell those apart -- it is the same "somebody else" in both.

    ``opened_on`` is the surface the bubble was opened on, and :attr:`address` is
    its key. Holding the surface for the entry's life is something a surface
    supports: it closes over its channel's long-lived client plus the bound address
    and carries no per-request state. An entry always has one, because a bubble is
    not opened on a surface that cannot name its address.
    """

    msg_id: Any
    #: Who opened the bubble, as the queue's own owner token. Recorded so the queue
    #: side can say whose bubble this is; no write rule tests it, because a principal
    #: cannot tell a shared conversation from a shared session key -- :attr:`address`
    #: is what the rules compare.
    opened_by: str = ""
    #: The surface the bubble was OPENED on -- the one conversation ``msg_id`` is
    #: valid in, and the only surface an owed record is written or posted through.
    #: ``None`` only for an entry built without one, which is written to not at all
    #: rather than through some caller's surface.
    opened_on: ReceiptSurface | None = None
    lines: list[ReceiptLine] = field(default_factory=list)
    #: The FINAL records this bubble still owes, OLDEST first, each set when its own
    #: write did not land.
    #:
    #: An entry carrying any is TERMINAL: those messages have already left the queue,
    #: so it is not grown and not flipped again -- the next mid-turn message opens a
    #: fresh bubble rather than putting answered text back under "Queued". A body
    #: travels WITH the entry because the record owed is the one that transition
    #: computed; recomputing it later would write whatever the later transition
    #: happened to be instead.
    #:
    #: It is a LIST because a transition can meet an entry that already owes one: the
    #: channel that refused the earlier record is usually still refusing, and a single
    #: slot would mean the later transition's record is dropped on the floor with
    #: nothing left to publish it. They are kept in the order they happened, which is
    #: the order a reader must see them in, and capped at :data:`RECEIPT_MAX_OWED`.
    owed_bodies: list[str] = field(default_factory=list)
    #: How many owed records were released to keep :attr:`owed_bodies` inside
    #: :data:`RECEIPT_MAX_OWED`. Counted at the same seam that releases them, because a
    #: released record has no other trace: a shortened list reads exactly like a burst
    #: that never produced those records. The count is said out loud once, on the body
    #: that reaches the reader first, and cleared when that body lands.
    omitted_records: int = 0
    #: Whether a record for this debt has already reached the reader. There is ONE
    #: bubble and it holds the OLDEST record, so once anything has been published the
    #: bubble is spent: every later body is posted beneath it. Without this a retry
    #: would edit the next body over the record already sitting in the bubble.
    bubble_consumed: bool = False
    #: Publications refused in a row with nothing landing. The evidence the retention
    #: bound is measured against: restarted by any body that reaches the reader and by any
    #: new record joining the debt, so it counts refusals of the debt's NEWEST record
    #: rather than the age of the entry holding it.
    publish_failures: int = 0

    @property
    def owes_record(self) -> bool:
        """Whether this entry is terminal, still owing a record it could not write."""
        return bool(self.owed_bodies)

    @property
    def texts(self) -> list[str]:
        """What the bubble shows, in order. What :func:`receipt_text` renders."""
        return [line.text for line in self.lines]

    @property
    def address(self) -> str:
        """The ONE conversation this bubble lives in, as :func:`receipt_address_key`.

        Empty only for an entry with no bound surface, which is then written to
        nowhere -- an empty key matches nothing, including another empty one.
        """
        return self.opened_on.address_key if self.opened_on is not None else ""

    def addressed_by(self, surface: ReceiptSurface) -> bool:
        """Whether *surface* writes to the same conversation this bubble lives in.

        This is the whole address rule as one question, and every transition asks it
        instead of asking who is calling. True on a shared-conversation route (a group
        space, where every member's surface binds the one ``room_id``) and false across
        two chats that merely share a session key (unified DM scope). An unknown
        address on either side answers false: without a key there is nothing to
        compare, and guessing is what puts a body in the wrong chat.
        """
        mine = self.address
        return bool(mine) and mine == surface.address_key

    def texts_at_address(self) -> list[str]:
        """What the bubble may SHOW, in order: the lines that arrived at its address.

        The entry can hold lines from other conversations -- a shared session key puts
        them there -- and those may not be rendered into this one, so every body
        written to the bubble is built from this and never from :attr:`texts`.
        """
        mine = self.address
        if not mine:
            return []
        return [line.text for line in self.lines if line.address == mine]

    def terminalize(self, body: str) -> None:
        """Make this entry terminal, owing *body* after anything it already owes.

        The one way to add to :attr:`owed_bodies`, so the bound on what a terminal entry
        retains is applied HERE -- at the point of retention -- rather than at each of
        the transitions that terminalize, where the next one added would forget it. Three
        bounds meet at this seam, one per way a debt can grow without end. Each ``body``
        is already bounded: :func:`receipt_text` lists at most :data:`RECEIPT_MAX_ITEMS`
        items and :func:`short` truncates each, so one retained string cannot grow with
        the burst that produced it. The LIST is bounded here, to
        :data:`RECEIPT_MAX_OWED`, because it grows by one every time a transition meets a
        channel that is still refusing. How LONG the debt is held is bounded by
        :meth:`note_refusal`, whose allowance this restarts for the new record; how MANY
        debts the registry holds is bounded where this is called from.

        ``lines`` is released: it holds every message verbatim and is of no further use
        -- a terminal entry is never grown, never flipped, and never rendered again.

        Appending rather than replacing is the whole point. The earlier record describes
        messages that left the queue earlier, and *body* describes this transition's own;
        writing one over the other would say the wrong thing happened, permanently.

        What the cap releases is COUNTED here, at that same seam. A released record has
        no other trace of itself: the list simply becomes shorter, which reads exactly
        like a burst that never produced those records at all. The count travels with the
        entry until a record reaches the reader carrying it.
        """
        self.owed_bodies.append(body)
        released = len(self.owed_bodies) - RECEIPT_MAX_OWED
        if released > 0:
            del self.owed_bodies[:released]
            self.omitted_records += released
        self.lines = []
        # The debt has a new newest record, which no channel has refused even once, so the
        # lifetime bound starts over on it. That is what makes the bound's guarantee the
        # strong one: a record is never given up until it has itself been offered
        # RECEIPT_MAX_PUBLISH_ATTEMPTS times. Counting straight through would let a record
        # that arrived on the last attempt be discarded without ever being written.
        self.publish_failures = 0

    def note_refusal(self, *, progressed: bool) -> bool:
        """Count one refused publication. Returns whether retention is SPENT.

        The lifetime half of the bound that :meth:`terminalize` states the size half of,
        kept beside it so what a terminal entry is allowed to hold and how long it is
        allowed to hold it are one decision in one place. Only the observation is
        elsewhere: a refusal is seen where a body is published, and the publisher reports
        it here rather than judging it there.

        *progressed* is whether anything left the debt during that attempt. It resets the
        count, because a channel that published one record and refused the next is
        working -- the debt is draining, and expiring it would throw away records that
        are on their way to a reader. Only an attempt that moves nothing counts.

        So what the count really measures is how many times the debt's NEWEST record has
        been offered and refused with nothing moving, since :meth:`terminalize` restarts
        it for each new record. Spending it takes that many transitions on this key that
        publish nothing at all, and the transitions that keep arriving in exactly the case
        this bound exists for are the mid-turn messages being turned away with no bubble:
        each one is a reader's acknowledgement this debt is costing, and the count is the
        number of them it may cost.
        """
        self.publish_failures = 0 if progressed else self.publish_failures + 1
        return self.publish_failures >= RECEIPT_MAX_PUBLISH_ATTEMPTS

    def abandon(self) -> int:
        """Give up everything owed. Returns how many records now reach nobody, ever.

        The debt is emptied IN PLACE, so this entry stops being terminal: it owes
        nothing, every transition treats it as settled, and the key it sat on is released
        by the same line that releases a debt which published. That is the whole point --
        a key held by a conversation that takes no writes is a key no sibling on it can
        have.

        The return value includes :attr:`omitted_records` as well as the bodies still
        held, because those records are given up here too. Counting only the bodies would
        lose a count that was itself the record of a loss, which is the same silence one
        level up.
        """
        given_up = len(self.owed_bodies) + self.omitted_records
        self.owed_bodies.clear()
        self.omitted_records = 0
        return given_up

    def withdraw(self, owner: str) -> list[str]:
        """Drop *owner*'s lines and return what they showed, in order.

        An empty *owner* drops nothing, matching the queue-side predicate: a caller that
        cannot name its principal withdraws nothing rather than everybody's lines.
        """
        if not owner:
            return []
        taken = [line.text for line in self.lines if line.owner == owner]
        if taken:
            self.lines = [line for line in self.lines if line.owner != owner]
        return taken


class ReceiptSurface(Protocol):
    """One conversation's receipt bubble, with its address already bound.

    Implementations close over whatever addresses their channel needs (Telegram
    binds ``chat_id`` AND the forum ``thread``; Discord binds ``channel_id``), so
    forum routing and channel addressing stay entirely channel-local.
    """

    #: Channel name for log lines only ("telegram" / "discord").
    label: str

    #: The ONE conversation this surface's message ids are valid in, built by
    #: :func:`receipt_address_key` from the addresses the channel's own edit call
    #: uses. It is what :meth:`QueueReceipt.addressed_by` compares, so two surfaces
    #: built for the same conversation MUST produce the same string and two built
    #: for different conversations must not. Empty means the surface cannot name its
    #: address, and then it opens no bubble and writes to none.
    address_key: str

    async def send_receipt(self, body: str) -> Any | None:
        """Post a new receipt bubble. Returns an opaque message id, or None."""

    async def edit_receipt(self, msg_id: Any, body: str) -> bool:
        """Rewrite the receipt in place. Returns whether the edit LANDED.

        A channel client answers a refusal with ``False`` rather than an exception:
        a rate-limited chat, or a bubble past the per-message edit cap Webex
        documents, is an ordinary non-2xx answer. Returning it is what lets the
        registry tell "the bubble now shows this" from "the bubble still shows the
        old text", which is the difference between a durable record and a bubble
        stranded reading "⏳ Queued".

        An implementation that cannot tell may return ``None``; that is silence,
        not a reported failure, and :meth:`ReceiptQueue._edit` treats it as landed.
        May also raise, which IS a reported failure.
        """


class ReceiptQueue:
    """Owns the per-session receipt registry, its lock, and the transitions.

    The lock is deliberately CALLER-HELD and exposed as :attr:`lock` rather than
    taken inside each method. Holding it across BOTH the enqueue and the receipt
    bookkeeping is what makes the subsystem race-free against the end-of-turn
    drain, which takes the same lock across dequeue + flip: the drain either sees
    a message queued WITH its receipt or sees neither yet -- never a half state
    that would orphan a bubble. ``/stop`` holds it across clear_queue + finalize
    for the same reason. Hiding the lock inside these methods would silently
    reintroduce that race, which is why the ``_locked`` suffixes stay in the
    public names: ugly, and load-bearing.
    """

    def __init__(self) -> None:
        self._receipts: dict[str, QueueReceipt] = {}
        self._lock = asyncio.Lock()
        self._abandoned_records = 0
        self._abandoned_debts = 0

    @property
    def lock(self) -> asyncio.Lock:
        """The lock callers MUST hold across compound operations (see class doc)."""
        return self._lock

    @property
    def abandoned_records(self) -> int:
        """Records given up by the retention bound, so no reader ever sees them.

        Both halves of the bound land here: a debt whose channel refuses it
        :data:`RECEIPT_MAX_PUBLISH_ATTEMPTS` times running, and a debt released to keep
        the registry inside :data:`RECEIPT_MAX_DEBTS`. Kept for the same reason
        :attr:`QueueReceipt.omitted_records` is kept -- a released record leaves no other
        trace of itself, so a registry that quietly got smaller would read exactly like
        one that never owed anything.

        Says nothing to the reader, deliberately: the conversation these records belonged
        to is the one that would not take a write.
        """
        return self._abandoned_records

    @property
    def abandoned_debts(self) -> int:
        """How many terminal entries the retention bound released."""
        return self._abandoned_debts

    def has_receipt(self, session_key: str) -> bool:
        """Whether a LIVE receipt exists for this session.

        An entry still owing a record is terminal, not live: it cannot be grown, and
        the next mid-turn message opens a fresh bubble rather than joining it.
        """
        receipt = self._receipts.get(session_key)
        return receipt is not None and not receipt.owes_record

    async def create_or_grow_locked(
        self,
        session_key: str,
        surface: ReceiptSurface,
        display_text: str,
        owner: str = "",
    ) -> None:
        """Create the receipt, or append to it and edit in place.

        ``display_text`` is what the receipt SHOWS, which is not always the raw
        message: a file-capable channel substitutes :data:`ATTACHMENT_PLACEHOLDER`
        for an attachment-only message so the bubble is not blank. Caller MUST hold
        :attr:`lock`, and MUST have already enqueued the message under that same
        hold.

        ``owner`` is who queued this one line -- the same token the queue entry carries
        -- so a later ``/stop`` for one principal can withdraw that person's lines and
        leave the rest alone. Pass the value the entry was tagged with; empty means the
        line is nobody's to withdraw.
        """
        receipt = self._receipts.get(session_key)
        line = ReceiptLine(owner=owner, text=display_text, address=surface.address_key)
        if receipt is not None and receipt.owes_record:
            # Terminal: those messages already left the queue. Growing it would put
            # answered text back under "Queued" beside this new one, so the record it
            # owes is written first and the key released only once it owes nothing --
            # this entry is that bubble's only handle. The write goes to the bubble's OWN
            # conversation, not this caller's: under a shared key the arriving message
            # may be from another chat, and the record quotes that chat's text.
            #
            # Owing nothing covers the debt being PUBLISHED and the debt being GIVEN UP
            # on a conversation that has refused every write for long enough. Both free
            # the key, and this is the burst on which THIS message gets a bubble of its
            # own: under a shared session key the arriving surface is often a healthy
            # sibling of the one that went silent, and the key was the only thing
            # standing between it and its own receipt.
            if await self._write_record(receipt):
                del self._receipts[session_key]
                receipt = None
            else:
                # Still no channel, and the debt has attempts left, so this entry stays
                # terminal and this message gets no bubble yet. The residual is one
                # missing "⏳ Queued" acknowledgement per message that arrives while the
                # debt is held, and the count above is what bounds how many of them one
                # debt may cost; nothing is lost either way, because the caller enqueued
                # before calling and the drain renders its answering record from what it
                # dequeued rather than from here. The line is deliberately NOT retained on
                # a terminal entry. No path reads those lines -- every transition returns
                # above on ``owes_record`` -- so keeping them would change nothing a reader
                # sees while growing a verbatim burst for as long as the channel refuses,
                # which is the retention the bound at ``terminalize`` exists to release.
                return
        if receipt is None:
            if not surface.address_key:
                # No address, so no bubble. Every later write would have to guess which
                # conversation this entry's ``msg_id`` belongs to, and a wrong guess
                # rewrites a stranger's message. A channel whose inbound omitted its
                # conversation id degrades to no receipt, which is the same outcome as
                # a refused ``send_receipt`` and is already handled everywhere.
                logger.debug("%s: queue receipt skipped, surface has no address", surface.label)
                return
            msg_id = await surface.send_receipt(receipt_text([display_text]))
            if msg_id is not None:
                self._receipts[session_key] = QueueReceipt(
                    msg_id=msg_id, opened_by=owner, opened_on=surface, lines=[line]
                )
            return
        receipt.lines.append(line)
        if not receipt.addressed_by(surface):
            # Another conversation under a shared key. There is no address this edit
            # could use: ``msg_id`` names a message in the bubble's chat and nowhere
            # else, while THIS caller's chat holds no bubble, so editing through the
            # caller would rewrite whatever unrelated message happens to hold that
            # number there. The line is RECORDED so the registry and the queue agree
            # about what is still queued, and it is shown on no bubble at all: this one
            # renders only its own address, and this chat gets its own bubble once this
            # entry is gone. Safe because a grow owes no record -- the message is still
            # QUEUED and will be answered. Which conversation a shared bubble belongs to
            # is the registry key's own question, not this one's.
            return
        # A refused grow needs no record and no terminal state: this message is still
        # QUEUED, which is exactly what ``lines`` tracks, so the registry and the queue
        # still agree and the next message's edit re-renders the list. Only a transition
        # whose messages have already LEFT the queue can strand a bubble. Rendered from
        # this address's own lines: the entry may also hold another chat's, and they may
        # not be shown here even though the bubble is the same object.
        await self._edit(surface, receipt.msg_id, receipt_text(receipt.texts_at_address()))

    async def flip_answering_locked(
        self,
        session_key: str,
        surface: ReceiptSurface,
        answered: list[str],
        deferred: int = 0,
    ) -> None:
        """Flip the receipt to a durable "▶️ Now answering" record.

        Drops the live entry so the next mid-turn burst opens a fresh receipt.
        ``answered`` is the subset this turn actually answers (the drain caps it),
        so a burst past the cap does not overstate the turn; ``deferred`` (>0 only
        past the cap) is noted so the remainder is not silently implied. Caller
        MUST hold :attr:`lock` across dequeue + this call.

        One drained turn carries ONE envelope, so every text in ``answered`` came from
        the same conversation, and *surface* is built from that same origin. So the
        address test below is also a test on the BODY: when it passes, ``answered`` is
        this bubble's own chat's text; when it fails, ``answered`` belongs to a
        different chat and none of it may appear here.
        """
        receipt = self._receipts.pop(session_key, None)
        if receipt is None:
            return
        body = receipt_text(answered, answering=True)
        if deferred:
            body += f" · +{deferred} deferred"
        if receipt.owes_record:
            # Already terminal from an earlier refused transition. Retry THAT record
            # first -- writing this transition's words over what actually happened would
            # say the opposite, permanently -- and keep the entry until it lands.
            if not await self._write_record(receipt):
                # The debt has no channel: that call just tried an edit AND a post and
                # both failed, so THIS record has none either and posting it now would
                # fail the same way. It JOINS the debt behind the older one instead of
                # being dropped -- these messages have already left the queue and a
                # retired key is revisited by nothing, so dropping it is permanent
                # silence over answered messages. Only this bubble's own chat's text may
                # be retained here: otherwise ``answered`` belongs to another
                # conversation, and a retained body is written to this bubble later.
                if receipt.addressed_by(surface):
                    self._retain_owed(session_key, receipt, body)
                else:
                    self._receipts[session_key] = receipt
                return
            # It owes nothing now, so this transition's own record has no bubble left to
            # edit: it is POSTED beside it, at the bubble's own address. Retiring the key
            # and returning here instead would lose the drained burst's receipt for good
            # -- a retired key is revisited by nothing, and these messages have already
            # left the queue. That holds whether the older record published or was given
            # up: a spent debt frees the key, and this record is a fresh one that has not
            # been refused even once.
            # Only when the caller addresses the bubble: otherwise ``answered`` is
            # another chat's text, which may not appear here at all.
            if receipt.addressed_by(surface):
                if not await self._post_record(receipt.opened_on, body):
                    # The post failed too, so this record has reached nobody. Retain it:
                    # a later transition then edits the bubble to it, which replaces a
                    # record the reader has already been shown rather than losing this
                    # one entirely.
                    self._retain_owed(session_key, receipt, body)
            return
        if not receipt.addressed_by(surface):
            # This turn answered a DIFFERENT conversation that shares the session key,
            # so nothing of it may be written here -- not the edit, whose body would
            # quote that chat's text into this one, and not a retained record, which
            # would post the same body later. Nothing is stranded by staying silent:
            # the drain answers one envelope at a time and defers the rest, so this
            # bubble's own messages are still QUEUED. Putting the entry back LIVE is
            # what keeps that true -- dropping it would leave a bubble reading
            # "⏳ Queued" for messages that really are still queued, and open a second
            # bubble beside it for the same burst.
            self._receipts[session_key] = receipt
            return
        if not await self._edit(surface, receipt.msg_id, body):
            # These messages have LEFT the queue, so nothing else will ever revisit this
            # bubble on its own: dropped now it reads "⏳ Queued" for good. The record is
            # published immediately instead, and the entry kept only if that fails too.
            await self._owe_record(session_key, receipt, body)

    async def finish_cancelled_locked(
        self, session_key: str, surface: ReceiptSurface, owner: str = ""
    ) -> None:
        """Finalize the receipt to a "🛑 Cancelled" record, if present.

        Caller MUST hold :attr:`lock` across clear_queue + this call.

        ``owner`` names the ONE principal whose messages were cleared, and then only
        that person's lines are withdrawn from the record. Whether anything is written
        depends on the ADDRESS, not on who is calling:

        * the caller's surface addresses the bubble -- it finalizes as cancelled over
          the caller's OWN withdrawn lines, which arrived at that same address. Not
          over what remains: those lines may have arrived from another chat that
          merely shares this session key.
        * it does not -- nothing is written, because ``msg_id`` names a message in the
          bubble's chat and in the caller's the same number is another message
          entirely. The lines are withdrawn from the record either way.

        Both cases are preceded by a condition that has nothing to do with addressing:
        while ANY line remains after the withdraw, nothing is written at all. Those
        messages are still queued, so "Cancelled" would say they went and a re-render
        would show their text.

        The entry is dropped once it owes nothing. It is RETAINED in exactly one case:
        the finalizing edit did not land, so the entry carries the record it owes and
        is terminal. That is safe against a later drain for the same reason the
        addressing rule holds -- an owed record is written through
        :attr:`QueueReceipt.opened_on`, the surface the bubble was opened on, so no
        transition is ever handed an id minted in a different chat. A retained entry is
        not live and is never grown, so a later burst opens a fresh bubble rather than
        joining this one; which conversation a shared bubble belongs to is the registry
        key's own question.

        Omitted, the whole receipt is finalized, which is what the whole-session callers
        mean: the queue they cleared was all of it.
        """
        receipt = self._receipts.get(session_key)
        if receipt is None:
            return
        if receipt.owes_record:
            # This bubble already owes a record from an earlier transition -- those
            # messages left the queue THEN, not in this clear. Writing "Cancelled" over
            # an owed "Now answering" would say the opposite of what happened, and
            # permanently. Retry what is owed, through the bubble's own conversation
            # rather than this caller's, and leave the entry until it lands.
            if await self._write_record(receipt):
                self._receipts.pop(session_key, None)
            return
        if owner:
            withdrawn = receipt.withdraw(owner)
            if not withdrawn:
                return
            if receipt.texts_at_address():
                # The bubble's OWN chat still has queued messages listed on it, and this
                # entry is their only handle. Nothing is written -- "Cancelled" would say
                # they went -- and the entry stays LIVE, because retiring the key here
                # strands the bubble on "⏳ Queued" for good: the later drain finds no
                # entry to flip, and the next burst opens a second bubble beside the
                # stale one. The caller learns their own stop worked from the stop reply.
                # Lines from another chat do not hold the bubble: they were never
                # rendered on it, so it owes them nothing.
                return
            self._receipts.pop(session_key, None)
            if receipt.addressed_by(surface):
                # Safe to render the withdrawn lines here: a line records the address it
                # ARRIVED at, this caller's lines arrived on this caller's surface, and
                # the test just established that surface is the bubble's own.
                body = receipt_text(withdrawn, cancelled=True)
                if not await self._edit(surface, receipt.msg_id, body):
                    await self._owe_record(session_key, receipt, body)
            return
        self._receipts.pop(session_key, None)
        # Addressed to the bubble, never to this caller: a whole-session clear names no
        # principal (a caller that means "the queue was all of it"), so under a shared
        # key it can arrive from a different chat, and falling back to that chat is how
        # a body reaches a reader it was never for. Unlike a grow this record is TERMINAL
        # -- those messages have left the queue, nothing will revisit the bubble, and a
        # bubble left reading "⏳ Queued" for cleared messages is wrong for good -- so it
        # is written rather than skipped. Rendered from this address's own lines: the
        # clear spans every conversation on the key, and the bubble is only one of them.
        target = receipt.opened_on
        if target is None:
            return
        body = receipt_text(receipt.texts_at_address(), cancelled=True)
        if not await self._edit(target, receipt.msg_id, body):
            await self._owe_record(session_key, receipt, body)

    async def _edit(self, surface: ReceiptSurface, msg_id: Any, body: str) -> bool:
        """Rewrite the bubble to *body*. Returns whether the write LANDED.

        A raise and a reported ``False`` are the same answer here: the bubble still
        shows its old text. Only an explicit ``False`` counts as reported failure --
        a surface that answers ``None`` has not reported one, and reading silence as
        failure would keep every receipt in the registry for good.
        """
        try:
            return await surface.edit_receipt(msg_id, body) is not False
        except Exception:
            logger.debug("%s: queue receipt edit failed", surface.label, exc_info=True)
            return False

    async def _owe_record(self, session_key: str, receipt: QueueReceipt, body: str) -> None:
        """Publish *body* as this bubble's record NOW; keep the entry only if that fails.

        Reached when a transition's own edit was refused and its messages have already
        LEFT the queue. Posting here is what gives the record a path that does not
        depend on another message ever arriving: a retained body is otherwise written
        only by a LATER transition, so a burst that ends at this refusal would leave the
        bubble reading "⏳ Queued" over answered messages for good.

        The stale bubble plus a posted record are together true. Retaining is the last
        resort, for when the post fails too -- then a later transition still has a body
        to write, and the entry carries the bound on what it holds.

        The one transition that must NOT come here is a refused GROW: its message is
        still queued, so it owes no record at all and a post would announce something
        that has not happened.
        """
        if await self._post_record(receipt.opened_on, body):
            return
        self._retain_owed(session_key, receipt, body)

    def _retain_owed(self, session_key: str, receipt: QueueReceipt, body: str) -> None:
        """Keep *receipt* terminal and owing *body*, so a later transition can write it.

        The only place an entry becomes terminal, which is what keeps the bound on what
        it retains in one place: :meth:`QueueReceipt.terminalize` drops the line list as
        it stores the body.

        The entry is re-inserted at the BACK of the registry, which is what makes the
        population bound below pick the right victim. Registry order is insertion order,
        so moving an entry on every retention orders the terminal ones by how recently
        each was retained, and the front of that order is the debt whose key has been
        silent longest -- the one most likely to have been orphaned by a key that no
        longer arrives, rather than one still being retried.
        """
        receipt.terminalize(body)
        self._receipts.pop(session_key, None)
        self._receipts[session_key] = receipt
        self._release_oldest_debts()

    def _release_oldest_debts(self) -> None:
        """Hold the registry to :data:`RECEIPT_MAX_DEBTS` terminal entries, counting each.

        Live entries are not touched: a live entry's messages are still QUEUED, so
        dropping one strands its bubble on "⏳ Queued" and opens a second bubble beside
        it. Only terminal entries are candidates, and they are released oldest-retained
        first. The entry just retained is at the back, so the bound never reaches the
        record it was called about.
        """
        owing = [key for key, receipt in self._receipts.items() if receipt.owes_record]
        # Computed before it is used as a slice bound: a negative one reads as "all but
        # the last N", which releases debts while the registry is nowhere near full.
        excess = len(owing) - RECEIPT_MAX_DEBTS
        for key in owing[:excess] if excess > 0 else []:
            self._write_off(self._receipts.pop(key), "registry full")

    def _write_off(self, receipt: QueueReceipt, reason: str) -> None:
        """Give up *receipt*'s debt and COUNT it. The one path out of retention.

        Both halves of the bound end here, so a released record is counted the same way
        whichever one released it, and neither can become the silent drop that a bound
        without an accounting seam would be.
        """
        given_up = receipt.abandon()
        self._abandoned_records += given_up
        self._abandoned_debts += 1
        label = receipt.opened_on.label if receipt.opened_on is not None else "?"
        logger.warning(
            "%s: queue receipt debt given up (%s), %d record(s) reach nobody",
            label,
            reason,
            given_up,
        )

    async def _post_record(self, surface: ReceiptSurface | None, body: str) -> bool:
        """POST *body* as a fresh message at *surface*. Returns whether it landed.

        The path for a record that has no bubble left to edit -- the bubble is spent
        (past a per-message edit cap) or already carries an earlier record. A stale
        bubble plus a posted record are together true; a silent bubble alone is not.
        ``None`` writes nothing: an entry with no bound surface has no address, and the
        only other one available is the wrong one.
        """
        if surface is None:
            return False
        try:
            return await surface.send_receipt(body) is not None
        except Exception:
            logger.debug("%s: queue receipt record post failed", surface.label, exc_info=True)
            return False

    async def _write_record(self, receipt: QueueReceipt) -> bool:
        """Put the record *receipt* owes onto its bubble. Returns whether it landed.

        Writes through ``receipt.opened_on`` and NEVER through a caller's surface.
        Under a shared session key the surface handed to a transition belongs to
        whichever conversation spoke last, and both writes here are addressed to the
        bubble's own: the edit targets ``msg_id``, which is another message entirely
        in any other chat, and the fallback POSTS the body as a fresh notified
        message, so a body built for one chat would arrive in a reader's chat
        unannounced. That the body is safe to show there is established where it was
        BUILT -- every transition renders from
        :meth:`QueueReceipt.texts_at_address` or from lines it has just checked
        against the bubble's address -- because here there is nothing left to check
        it against. An entry with no bound surface is not written at all: there is no
        address to fall back to, only the wrong one.

        Editing first is what keeps the record in the bubble the reader is already
        looking at. When the bubble refuses edits the record is POSTED instead: past
        Webex's documented per-message edit cap no edit of that id will ever land, so
        retrying alone would leave the record owed for the life of the process. The
        stale bubble still reads "⏳ Queued" and the posted record says what happened,
        which together are true; a silent bubble alone is not. The post goes to the
        bubble's own send address, so on a channel with forum Topics the record lands
        in the Topic the bubble is in rather than the parent chat.

        Several records can be owed, and they are published OLDEST FIRST: the oldest
        goes into the bubble, and each later one is POSTED beneath it, which is the
        order they happened in and so the order a reader must read them in. Only the
        oldest can take the bubble -- there is one bubble and the rest arrived after it.

        The bubble is spent by the FIRST record that reaches the reader, whether it got
        there by edit or by post, and that is remembered on the entry. A later call must
        not edit again: the bubble sits above everything posted beneath it, so editing a
        later body into it would both erase the record already shown there and put the
        two records in the wrong order for whoever reads them.

        Records released by the cap are named once, on the first body to reach the
        reader, and the count is cleared when that body lands. A shortened list is
        otherwise indistinguishable from a burst that produced no such records.

        Each body is dropped from the debt only once it LANDS, one at a time, so a
        failure partway through keeps exactly what has not reached anybody.

        A refusal is reported to :meth:`QueueReceipt.note_refusal`, and a debt whose
        retention that spends is GIVEN UP here: emptied and counted, which is what lets
        every caller below treat "published" and "given up" alike. Both mean the same
        thing to them -- this key owes nothing now -- and that is the point, because a key
        released is a key a healthy sibling conversation can open its own bubble on.

        Returns whether the debt is now empty.
        """
        bodies = receipt.owed_bodies
        if not bodies:
            return True
        owed_before = len(bodies)
        surface = receipt.opened_on
        if surface is None:
            return self._note_refused(receipt, owed_before)
        while bodies:
            body = bodies[0]
            if receipt.omitted_records:
                body += f" · …and {receipt.omitted_records} earlier record(s) omitted"
            if receipt.bubble_consumed:
                if not await self._post_record(surface, body):
                    return self._note_refused(receipt, owed_before)
            elif not await self._edit(surface, receipt.msg_id, body):
                if not await self._post_record(surface, body):
                    return self._note_refused(receipt, owed_before)
            receipt.bubble_consumed = True
            receipt.omitted_records = 0
            del bodies[0]
        return True

    def _note_refused(self, receipt: QueueReceipt, owed_before: int) -> bool:
        """Report one refused publication of *receipt*. Returns whether it owes nothing.

        *owed_before* is how many bodies the attempt started with, so what it published
        before being refused counts as the progress that keeps the debt alive.
        """
        progressed = len(receipt.owed_bodies) < owed_before
        if not receipt.note_refusal(progressed=progressed):
            return False
        self._write_off(receipt, "channel refuses every write")
        return True
