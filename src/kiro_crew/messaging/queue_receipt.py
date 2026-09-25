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

    Pass the very values the channel's own edit call addresses a message with --
    Telegram's ``chat_id``, Discord's ``channel_id``, Teams' ``service_url`` plus
    ``conversation_id``, Webex's ``room_id`` -- and nothing else. A value the edit
    call does not use does not belong here: Telegram's forum ``thread`` routes a
    SEND, while ``edit_message`` addresses a message by id within its chat, so two
    surfaces differing only by Topic address the same message space and must
    produce the same key.

    Every part comes from the provider's own inbound payload, so the key names a
    conversation the provider assigned and nothing the agent can choose.

    Returns ``""`` when any part is missing, which reads as UNKNOWN rather than as
    an address: :meth:`QueueReceipt.addressed_by` matches no key against it, not
    even another empty one, and :meth:`ReceiptQueue.create_or_grow_locked` opens no
    bubble on a surface that cannot name its address. Fail closed -- without an
    address there is no way to tell the bubble's own conversation from any other.
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
    #: The FINAL record this bubble still owes, set when that edit did not land.
    #:
    #: An entry carrying one is TERMINAL: its messages have already left the queue,
    #: so it is not grown and not flipped again -- the next mid-turn message opens a
    #: fresh bubble rather than putting answered text back under "Queued". The body
    #: travels WITH the entry because the record owed is the one that transition
    #: computed; recomputing it later would write whatever the later transition
    #: happened to be instead.
    final_body: str | None = None

    @property
    def owes_record(self) -> bool:
        """Whether this entry is terminal, still owing a record it could not write."""
        return self.final_body is not None

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
        """Make this entry terminal, owing *body*, and release the lines it does not need.

        The one way to set :attr:`final_body`, so the bound on what a terminal entry
        retains is applied HERE -- at the point of retention -- rather than at each of
        the transitions that terminalize, where the next one added would forget it.
        ``body`` is already bounded: :func:`receipt_text` lists at most
        :data:`RECEIPT_MAX_ITEMS` items and :func:`short` truncates each, so the
        retained string cannot grow with the burst that produced it, while ``lines``
        holds every message verbatim and is of no further use -- a terminal entry is
        never grown, never flipped, and never rendered again.
        """
        self.final_body = body
        self.lines = []

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

    @property
    def lock(self) -> asyncio.Lock:
        """The lock callers MUST hold across compound operations (see class doc)."""
        return self._lock

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
            # owes is written first and the key released only once that lands -- this
            # entry is that bubble's only handle. The write goes to the bubble's OWN
            # conversation, not this caller's: under a shared key the arriving message
            # may be from another chat, and the record quotes that chat's text.
            if await self._write_record(receipt):
                del self._receipts[session_key]
                receipt = None
            else:
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
                self._receipts[session_key] = receipt
                return
            # It landed, so the bubble now carries the older record and this
            # transition's own record has no bubble left to edit: it is POSTED beside
            # it, at the bubble's own address. Retiring the key and returning here
            # instead would lose the drained burst's receipt for good -- a retired key
            # is revisited by nothing, and these messages have already left the queue.
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
        """
        receipt.terminalize(body)
        self._receipts[session_key] = receipt

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
        """
        body = receipt.final_body
        if body is None:
            return True
        surface = receipt.opened_on
        if surface is None:
            return False
        if await self._edit(surface, receipt.msg_id, body):
            return True
        return await self._post_record(surface, body)
