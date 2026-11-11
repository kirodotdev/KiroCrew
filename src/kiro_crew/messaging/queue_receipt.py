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
from typing import Any, Literal, Protocol

logger = logging.getLogger(__name__)

#: What one attempt to publish a debt settled. ``owed`` is the only one that leaves the
#: entry terminal; the other two both mean the key owes nothing and is free, which is why
#: the transitions may treat them alike. They are still distinguished, because a record
#: reaching a reader and a record given up are the same only to the REGISTRY.
RecordOutcome = Literal["published", "owed", "given_up"]

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

#: Publications refused in a row, with nothing landing, before a debt is GIVEN UP.
#: Counted in refusals, not time: each refusal already tried an edit AND a post, so a
#: run of them is evidence the conversation takes no writes. Only a landed body restarts
#: the count; a transition that retains a record is itself a refusal, so restarting
#: there would make the bound unreachable under ordinary mid-turn-then-drain traffic.
#:
#: Twice :data:`RECEIPT_MAX_OWED` because the two bounds race: a shorter allowance would
#: give the debt up before the size cap could bite. Giving up matters because one
#: unwritable chat otherwise holds the session key against every healthy sibling on it.
RECEIPT_MAX_PUBLISH_ATTEMPTS = 2 * RECEIPT_MAX_OWED

#: Terminal entries the registry retains at once. A debt is retried only by a later
#: transition on its own key, so a key that stops being addressed (a reset rotates its
#: ``:gen{N}`` suffix) is never visited again. Past this bound the LEAST RECENTLY
#: ATTEMPTED debt is released (each attempt moves its entry to the back), and the
#: release is COUNTED.
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

    Must be at least as fine as the session key: conversations the session key
    collapses onto one entry share one bubble, and only this key keeps them apart.
    So a Telegram forum route (Topic already in the session key) needs only
    ``chat_id``, while a Webex group space (``space:{room_id}``) needs the thread too.
    Every part comes from the provider's inbound payload, never from the agent.

    Returns ``""`` (UNKNOWN) when any part is missing: it matches no key, not even
    another empty one, and no bubble is opened for it. A legitimately absent part
    (a Webex room root, not a thread) gets a nameable stand-in instead.
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

    ``owner`` is the :func:`kiro_crew.messaging.queue_drain.owner_token` the queue
    entry carries too; empty makes the line nobody's to withdraw. ``address`` is the
    :func:`receipt_address_key` the line ARRIVED on: under a shared session key a
    line may be rendered only back into its own conversation, and empty shows it
    nowhere.
    """

    owner: str
    text: str
    address: str = ""


@dataclass
class QueueReceipt:
    """The single, in-place receipt bubble tracking messages queued mid-turn.

    ``msg_id`` is opaque (``Any``): Telegram ids are ints, Discord's strings, and
    the id is only ever handed back to the surface that produced it. ``None`` means the
    ``send_receipt`` was refused: there is no bubble, nothing is written to the entry,
    and it holds its lines only so the next message's bubble lists them.

    One entry serves a whole session key, which can span several conversations
    (``messaging.dm_scope = "unified"`` DMs, or a ``space:{room_id}`` group space),
    while ``msg_id`` names a message in exactly the ONE it was opened in. So, decided
    on the ADDRESS rather than on who is calling:

    * only the bubble's own address may write to it: the same ``msg_id`` in another
      chat is a stranger's post.
    * a body may quote only lines that arrived at that address
      (:meth:`texts_at_address`), so one person's text never shows in another's chat.

    A caller test cannot get both shared-key routes right: in a group space a second
    member must update the shared bubble, under unified DMs they must not.

    ``opened_on`` is the surface the bubble was opened on (safe to hold: it carries
    no per-request state); :attr:`address` is its key.
    """

    msg_id: Any
    #: The surface the bubble was OPENED on (or refused on, with no ``msg_id``) -- the
    #: one conversation ``msg_id`` is valid in, and the only surface an owed record is
    #: written or posted through.
    #: ``None`` only for an entry built without one, which is written to not at all
    #: rather than through some caller's surface.
    opened_on: ReceiptSurface | None = None
    lines: list[ReceiptLine] = field(default_factory=list)
    #: The FINAL records this bubble still owes, OLDEST first (the order a reader must
    #: see them), capped at :data:`RECEIPT_MAX_OWED`. Any makes the entry TERMINAL: not
    #: grown or flipped again, so answered text never goes back under "Queued". Each
    #: body is the one its transition computed; recomputing later would write the wrong
    #: record. A list because the channel that refused one record usually refuses the
    #: next, and a single slot would drop it.
    owed_bodies: list[str] = field(default_factory=list)
    #: Owed records released by the :data:`RECEIPT_MAX_OWED` cap, which leave no other
    #: trace. Said once on the first body that reaches the reader, cleared when it lands.
    omitted_records: int = 0
    #: Whether a record for this debt has reached the reader. The ONE bubble holds the
    #: OLDEST record, so after that every body is posted beneath it rather than edited
    #: over it.
    bubble_consumed: bool = False
    #: Refusals in a row of the oldest owed body, with nothing landing. Restarted only
    #: by a body that reaches the reader (see :data:`RECEIPT_MAX_PUBLISH_ATTEMPTS`).
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

        The whole address rule; every transition asks it instead of who is calling.
        True in a group space (one ``room_id``), false across chats that only share a
        session key. An unknown address on either side answers false.
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

        The one way to add to :attr:`owed_bodies`, so the :data:`RECEIPT_MAX_OWED` cap
        is applied at the point of retention, and what it releases is COUNTED in
        :attr:`omitted_records`. Each body is already bounded by :func:`receipt_text`.
        It leaves :meth:`note_refusal`'s allowance alone: restarting it here would make
        that bound unreachable.

        Appends rather than replaces: the earlier record describes earlier messages.
        ``lines`` is released, since a terminal entry is never rendered again.
        """
        self.owed_bodies.append(body)
        released = len(self.owed_bodies) - RECEIPT_MAX_OWED
        if released > 0:
            del self.owed_bodies[:released]
            self.omitted_records += released
        self.lines = []

    def note_refusal(self, *, progressed: bool) -> bool:
        """Count one refused publication. Returns whether retention is SPENT.

        The lifetime half of the bound :meth:`terminalize` sizes. *progressed* is
        whether a body landed during the attempt, the ONLY thing that restarts the
        allowance: a channel that publishes one record and refuses the next is still
        draining. A record JOINING the debt must not restart it, or the count never
        reaches the cap while traffic keeps arriving.

        Counts refusals of the OLDEST body only; newer ones share its fate, since it
        must go first for the reader to see them in order.
        """
        self.publish_failures = 0 if progressed else self.publish_failures + 1
        return self.publish_failures >= RECEIPT_MAX_PUBLISH_ATTEMPTS

    def abandon(self) -> int:
        """Give up everything owed. Returns how many records now reach nobody, ever.

        Empties the debt IN PLACE so the entry stops being terminal and releases its
        key to siblings. The count includes :attr:`omitted_records`, which are given up
        too.
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

    def drop_answered(self, owner: str, still_queued: int) -> None:
        """Drop *owner*'s answered lines here, keeping their *still_queued* NEWEST.

        Keeps the newest rather than dropping "as many as were answered": not every
        queued message keeps a line (one arriving while the entry owes a record, or a
        never-sent entry's line replaced by a message from another address), so a
        positional count would erase a still-queued message's only handle.
        The drain takes from the front and re-enqueues the rest, so what is still queued
        is the principal's LATEST; with fewer lines than *still_queued*, none is dropped
        (a line left listed is re-rendered as queued; a removed one is gone).

        Scoped to :attr:`address` too, since a drain answers ONE envelope. An empty
        *owner* drops nothing, matching :meth:`withdraw`.
        """
        if not owner:
            return
        mine = self.address
        theirs = [
            index
            for index, line in enumerate(self.lines)
            if line.owner == owner and line.address == mine
        ]
        # Slice guarded rather than written ``theirs[:-still_queued]``: -0 is 0, so an
        # unguarded slice would answer EMPTY for the full drain that keeps nothing, which
        # is the one case that must drop every one of their lines.
        answered = theirs[:-still_queued] if still_queued > 0 else theirs
        if not answered:
            return
        dropped = set(answered)
        self.lines = [line for index, line in enumerate(self.lines) if index not in dropped]

    def others_at_address(self, owner: str) -> list[str]:
        """What this bubble still lists for principals OTHER than *owner*, in order.

        Asked before a drain retires the bubble. Only another principal's line at this
        address may hold it: this entry is their only handle. Lines at another address
        are never rendered here, and *owner*'s own lines past the collapse cap are
        answered by their next drain (the flip body says ``+N deferred``). An empty
        *owner* returns every line at the address.
        """
        mine = self.address
        if not mine:
            return []
        return [line.text for line in self.lines if line.address == mine and line.owner != owner]


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

    @property
    def lock(self) -> asyncio.Lock:
        """The lock callers MUST hold across compound operations (see class doc)."""
        return self._lock

    def has_receipt(self, session_key: str) -> bool:
        """Whether a LIVE receipt exists for this session.

        An entry still owing a record is terminal, not live: it cannot be grown, and
        the next mid-turn message opens a fresh bubble rather than joining it. An entry
        whose ``send_receipt`` was refused has no bubble at all.
        """
        receipt = self._receipts.get(session_key)
        return receipt is not None and receipt.msg_id is not None and not receipt.owes_record

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
            # Terminal: growing it would put answered text back under "Queued". Write the
            # owed record first, through the bubble's OWN conversation (the arriving
            # message may be from another chat). Once the debt is published or given up,
            # the key is free and this message gets a bubble of its own.
            if await self._write_record(session_key, receipt) != "owed":
                self._receipts.pop(session_key, None)
                receipt = None
            else:
                # Still owed with attempts left: this message gets no bubble yet. Nothing
                # is lost, since the message is already enqueued and the drain renders
                # from what it dequeued. The line is NOT retained: no path reads a
                # terminal entry's lines, and keeping them would grow without bound.
                return
        if receipt is None or receipt.msg_id is None:
            if not surface.address_key:
                # No address, so no bubble: a guessed conversation for ``msg_id`` could
                # rewrite a stranger's message.
                logger.debug("%s: queue receipt skipped, surface has no address", surface.label)
                return
            # A refused send left its lines with no bubble: this bubble lists them too.
            held = receipt.lines if receipt is not None and receipt.addressed_by(surface) else []
            lines = [*held, line]
            msg_id = await surface.send_receipt(receipt_text([each.text for each in lines]))
            # Kept even when refused (no ``msg_id``, never written to), so the next
            # message's bubble still counts this one.
            self._receipts[session_key] = QueueReceipt(
                msg_id=msg_id, opened_on=surface, lines=lines
            )
            return
        receipt.lines.append(line)
        if not receipt.addressed_by(surface):
            # Another conversation under a shared key: ``msg_id`` is valid only in the
            # bubble's chat, so no edit is possible. The line is RECORDED so the registry
            # matches the queue, and shown nowhere; the message is still queued.
            return
        # A refused grow owes no record: the message is still QUEUED and the next edit
        # re-renders. Rendered from this address's own lines only.
        await self._edit(surface, receipt.msg_id, receipt_text(receipt.texts_at_address()))

    async def flip_answering_locked(
        self,
        session_key: str,
        surface: ReceiptSurface,
        answered: list[str],
        deferred: int = 0,
        *,
        owner: str,
    ) -> None:
        """Flip the receipt to a durable "▶️ Now answering" record.

        Drops the live entry so the next mid-turn burst opens a fresh receipt.
        ``answered`` is the subset this turn actually answers (the drain caps it),
        so a burst past the cap does not overstate the turn; ``deferred`` (>0 only
        past the cap) is noted so the remainder is not silently implied. Caller
        MUST hold :attr:`lock` across dequeue + this call.

        ``deferred`` also decides which of *owner*'s lines are dropped: it is the count
        the drain re-enqueued for THIS principal, the only number that names what is
        still queued (see :meth:`QueueReceipt.drop_answered`). Under-counting erases an
        acknowledgement; over-counting only leaves a line to re-render.

        One drained turn carries ONE envelope, so the address test below also tests the
        BODY: when it fails, ``answered`` is another chat's text and may not appear here.

        ``owner`` is whose messages this drain answered (the queue entries' token). In a
        group space a PARTIAL drain is ordinary, and when another principal's line is
        still listed the entry is KEPT and re-rendered as queued. Required and
        keyword-only: :func:`owner_token` never builds an empty token, so an omission is
        a type error rather than a silent retire of the whole bubble.
        """
        receipt = self._receipts.pop(session_key, None)
        if receipt is None:
            return
        body = receipt_text(answered, answering=True)
        if deferred:
            body += f" · +{deferred} deferred"
        if receipt.owes_record:
            # Already terminal: retry THAT record first and keep the entry until it lands.
            # From the debt's own chat, this record shares its fate (``also=1``), since
            # retaining it would re-arm the key the release freed. From a sibling chat,
            # the debt's refusals say nothing about it, so it is offered on its own
            # surface below.
            mine = receipt.addressed_by(surface)
            outcome = await self._write_record(session_key, receipt, also=1 if mine else 0)
            if outcome == "owed":
                # The edit AND post just failed, so this record JOINS the debt rather
                # than being dropped for good. Only this bubble's own chat's text may be
                # retained, because a retained body is later written to this bubble.
                if mine:
                    self._retain_owed(session_key, receipt, body)
                else:
                    self._receipts[session_key] = receipt
                return
            if outcome == "given_up":
                # The debt was given up and the key released. A record from that chat
                # went with it (counted above). A sibling's record is POSTED on its own,
                # untested surface, and counted lost only if that refuses too.
                if not mine and not await self._post_record(surface, body):
                    self._report_lost(surface.label, "sibling chat refuses the post too", 1)
                return
            # The debt published and spent the bubble, so this record is POSTED beside it,
            # and only when the caller addresses the bubble (else ``answered`` is another
            # chat's text).
            if mine:
                if not await self._post_record(receipt.opened_on, body):
                    # Reached nobody: retain it so a later transition still writes it.
                    self._retain_owed(session_key, receipt, body)
            return
        if not receipt.addressed_by(surface):
            # This turn answered ANOTHER conversation on the key, so nothing is written
            # here (its text may not appear in this chat). This bubble's own messages are
            # still QUEUED, so the entry goes back LIVE; dropping it would open a second
            # bubble for the same burst.
            self._receipts[session_key] = receipt
            return
        # ``deferred``, NOT ``len(answered)``: not every queued message opened a line.
        receipt.drop_answered(owner, deferred)
        if receipt.msg_id is None:
            # Its ``send_receipt`` was refused, so there is no bubble to write: only the
            # lines still queued stay, for the next bubble to list.
            if receipt.lines:
                self._receipts[session_key] = receipt
            return
        if receipt.others_at_address(owner):
            # A PARTIAL drain (ordinary in a group space): another principal's message is
            # still queued and this entry is its only handle. "Now answering" would say
            # it went, so the bubble is re-rendered to what is STILL queued and stays
            # LIVE; this turn's answer speaks for its own messages. A refused re-render
            # owes no record, since nothing here has left the queue.
            self._receipts[session_key] = receipt
            await self._edit(surface, receipt.msg_id, receipt_text(receipt.texts_at_address()))
            return
        if not await self._edit(surface, receipt.msg_id, body):
            # These messages LEFT the queue, so nothing will revisit the bubble: post the
            # record now, and keep the entry only if that fails too.
            await self._owe_record(session_key, receipt, body)

    async def finish_cancelled_locked(
        self, session_key: str, surface: ReceiptSurface, owner: str = ""
    ) -> None:
        """Finalize the receipt to a "🛑 Cancelled" record, if present.

        Caller MUST hold :attr:`lock` across clear_queue + this call.

        ``owner`` names the ONE principal whose lines are withdrawn. While any line at
        the bubble's address remains, nothing is written ("Cancelled" would say they
        went). Otherwise the bubble is finalized over the caller's OWN withdrawn lines,
        and only when the caller's surface addresses the bubble: in another chat the
        same ``msg_id`` is a different message.

        The entry is retained only when the finalizing write fails, as a terminal debt
        written later through :attr:`QueueReceipt.opened_on`. Omitted ``owner``
        finalizes the whole receipt, as whole-session callers mean.
        """
        receipt = self._receipts.get(session_key)
        if receipt is None:
            return
        if receipt.owes_record:
            # This bubble already owes a record from an earlier transition -- those
            # messages left the queue THEN, not in this clear. Writing "Cancelled" over
            # an owed "Now answering" would say the opposite of what happened, and
            # permanently. Retry what is owed, through the bubble's own conversation
            # rather than this caller's, and leave the entry until it lands. A debt the
            # bound gives up here releases its key too: the clear's own record is not
            # written either way, so nothing of this transition goes with it.
            if await self._write_record(session_key, receipt) != "owed":
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
            if receipt.msg_id is not None and receipt.addressed_by(surface):
                # Safe to render the withdrawn lines here: a line records the address it
                # ARRIVED at, this caller's lines arrived on this caller's surface, and
                # the test just established that surface is the bubble's own. A refused
                # ``send_receipt`` left no bubble, so there is nothing to finalize.
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
        if target is None or receipt.msg_id is None:
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

        For a transition whose edit was refused after its messages LEFT the queue.
        Posting now means the record does not wait on another message arriving.
        Retaining is the last resort when the post fails too. Never for a refused GROW:
        its message is still queued and owes no record.

        The refusal that creates the debt is COUNTED (an edit and a post both failed),
        so the conversation gets no free refusal; whether the allowance is spent is
        decided where a debt is published.
        """
        if await self._post_record(receipt.opened_on, body):
            return
        receipt.note_refusal(progressed=False)
        self._retain_owed(session_key, receipt, body)

    def _retain_owed(self, session_key: str, receipt: QueueReceipt, body: str) -> None:
        """Keep *receipt* terminal and owing *body*, so a later transition can write it.

        The only place an entry becomes terminal, which is what keeps the bound on what
        it retains in one place: :meth:`QueueReceipt.terminalize` drops the line list as
        it stores the body.
        """
        receipt.terminalize(body)
        self._receipts[session_key] = receipt
        self._touch(session_key)
        self._release_oldest_debts()

    def _touch(self, session_key: str) -> None:
        """Move *session_key*'s entry to the BACK of the registry, if it holds one.

        Re-inserting on every attempt orders terminal entries by last attempt, so
        :meth:`_release_oldest_debts` evicts a key nothing has come back to, not one
        still being retried.
        """
        receipt = self._receipts.pop(session_key, None)
        if receipt is not None:
            self._receipts[session_key] = receipt

    def _release_oldest_debts(self) -> None:
        """Hold the registry to :data:`RECEIPT_MAX_DEBTS` terminal entries, counting each.

        Live entries are not touched: a live entry's messages are still QUEUED, so
        dropping one strands its bubble on "⏳ Queued" and opens a second bubble beside
        it. Only terminal entries are candidates, and they are released least-recently-
        attempted first. The entry just retained is at the back, so the bound never
        reaches the record it was called about.
        """
        owing = [key for key, receipt in self._receipts.items() if receipt.owes_record]
        # Computed before it is used as a slice bound: a negative one reads as "all but
        # the last N", which releases debts while the registry is nowhere near full.
        excess = len(owing) - RECEIPT_MAX_DEBTS
        for key in owing[:excess] if excess > 0 else []:
            self._write_off(key, self._receipts[key], "registry full")

    def _write_off(
        self, session_key: str, receipt: QueueReceipt, reason: str, also: int = 0
    ) -> None:
        """Give up *receipt*'s debt, release its key, and COUNT what is lost.

        The one path out of retention, so every bound accounts the same way, and
        "given up" frees the key just like "published". *also* counts the caller's own
        record given up with the debt; retaining it would re-arm the key. Nothing goes
        to the reader (that conversation takes no writes); the operator warning is the
        only trace.
        """
        given_up = receipt.abandon() + also
        self._receipts.pop(session_key, None)
        label = receipt.opened_on.label if receipt.opened_on is not None else "?"
        self._report_lost(label, reason, given_up)

    def _report_lost(self, label: str, reason: str, count: int) -> None:
        """Account for *count* records that will reach no reader. The one such seam.

        A WARNING for the operator, not the reader: that chat would not take a write.
        """
        logger.warning(
            "%s: queue receipt debt given up (%s), %d record(s) reach nobody",
            label,
            reason,
            count,
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

    async def _write_record(
        self, session_key: str, receipt: QueueReceipt, *, also: int = 0
    ) -> RecordOutcome:
        """Put the record *receipt* owes onto its bubble. Returns whether it landed.

        Writes through ``receipt.opened_on``, NEVER a caller's surface: under a shared
        key the caller may be another chat, where ``msg_id`` is a different message and
        a post would land unannounced. Bodies were checked against the bubble's address
        where they were BUILT. No bound surface means no write.

        Edits first; on refusal POSTS instead (past Webex's per-message edit cap no edit
        will ever land), to the bubble's own send address so a forum Topic gets it.
        Bodies go OLDEST FIRST: only the oldest may take the bubble, the rest are posted
        beneath it. The first landed record spends the bubble, so later calls never edit
        over it. Records the cap released are named on the first body to land. Each body
        leaves the debt only once it LANDS.

        Returns ``published``, ``given_up`` (retention spent per
        :meth:`QueueReceipt.note_refusal`; key released either way) or ``owed`` (still
        terminal). *also* = 1 counts the caller's own record with a given-up debt, so
        one bound produces one accounted loss; ignored otherwise.
        """
        bodies = receipt.owed_bodies
        if not bodies:
            return "published"
        owed_before = len(bodies)
        surface = receipt.opened_on
        if surface is None:
            return self._note_refused(session_key, receipt, owed_before, also)
        while bodies:
            body = bodies[0]
            if receipt.omitted_records:
                body += f" · …and {receipt.omitted_records} earlier record(s) omitted"
            if receipt.bubble_consumed:
                if not await self._post_record(surface, body):
                    return self._note_refused(session_key, receipt, owed_before, also)
            elif not await self._edit(surface, receipt.msg_id, body):
                if not await self._post_record(surface, body):
                    return self._note_refused(session_key, receipt, owed_before, also)
            receipt.bubble_consumed = True
            receipt.omitted_records = 0
            del bodies[0]
        # The whole debt reached the reader, which is the clearest evidence a conversation
        # takes writes, so the allowance restarts. Carrying the refusals forward would
        # leave an entry that publishes everything one attempt from being given up.
        receipt.publish_failures = 0
        return "published"

    def _note_refused(
        self, session_key: str, receipt: QueueReceipt, owed_before: int, also: int = 0
    ) -> RecordOutcome:
        """Report one refused publication of *receipt*. Says whether it is still owed.

        *owed_before* is how many bodies the attempt started with, so what it published
        before being refused counts as the progress that keeps the debt alive. *also* is
        passed through to :meth:`_write_off` for a caller whose own record goes with the
        debt when the bound is reached.

        A surviving debt is TOUCHED so the population bound evicts orphans first.
        """
        progressed = len(receipt.owed_bodies) < owed_before
        if not receipt.note_refusal(progressed=progressed):
            self._touch(session_key)
            return "owed"
        self._write_off(session_key, receipt, "channel refuses every write", also)
        return "given_up"
