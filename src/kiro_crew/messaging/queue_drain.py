"""The ownership contract for a mid-turn queue shared by several transports.

Why a shared module exists at all, when ``_handle_busy`` and ``_drain_queue``
deliberately stay per-channel: one mid-turn queue can hold entries from more than one
TRANSPORT. Every DM dispatcher is constructed with the orchestrator's single
``SessionManager``, and under ``messaging.dm_scope = "unified"``
``build_dm_session_key`` reduces a direct chat's bucket to ``unified:{agent}``,
dropping the CHANNEL as well as the user. So a Telegram DM and a Discord DM to the
same agent resolve to one session key, and therefore one queue.

Three things therefore cannot live in any one channel's module.

The first is the ownership predicate. A drain can only answer the entries its OWN
channel recorded, because an entry from another transport carries no address it can
reach: the origin fields are channel-specific, so :data:`QUEUED_CHANNEL_KEY` is the
one field every drain can read on every entry. Four channels drain this queue
(telegram, discord, teams, webex) and a fifth is a plausible addition, so the key and
its reader are defined ONCE here rather than restated per module. A restated literal
is a silent failure: one typo makes that channel's entries "not mine" to every drain,
including its own, so they are unowned and stranded with nothing raising.

The second is WHOSE each entry is. ``/stop`` cancels one person's turn, so it may drop
only that person's queued messages; under a unified scope the rest of the queue belongs
to other people who are still waiting for an answer. Naming the principal takes the same
neutral treatment as naming the channel and for a stronger reason: the caller has to
compare every entry on the queue, including entries another transport recorded whose
channel-specific fields it cannot read at all. So :data:`QUEUED_OWNER_KEY` and
:func:`owner_token` live here too.

The third is the wake. An entry set aside has already been accepted and receipted, so
something must come back for it. Nothing did: a drain runs only from the tail of its
own channel's turn, so a cross-transport entry waited for that transport to finish some
unrelated turn, and waited forever if it went quiet.

The registry holds channels, not addresses. It maps a channel type to "drain this
session key", which is all a waker needs to know about a peer; every conversation id,
chat id, room id and thread stays inside the channel that owns it, the same boundary
``messaging/queue_receipt.py`` keeps for the receipt.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from contextlib import contextmanager
from typing import Any, Iterator

logger = logging.getLogger(__name__)

#: Neutral key naming WHICH CHANNEL recorded a queue entry, written by every producer
#: that enqueues onto a shared session key. Neutral because the channel-specific origin
#: fields cannot be read until ownership is known: this is the one field every drain can
#: read on every entry. It says whose entry this is, and therefore which drain to wake.
#:
#: Defined here and imported, never restated: see the module docstring for why a
#: per-module copy of this string fails silently.
QUEUED_CHANNEL_KEY = "queued_channel"

#: Neutral key naming WHICH PRINCIPAL queued an entry -- one sender in one place --
#: written by every producer beside :data:`QUEUED_CHANNEL_KEY`. The channel tag answers
#: "which drain replies to this"; this answers "whose message is this", which is the
#: question a partial clear asks. Neutral for the same reason: a caller clearing only
#: its own entries must compare them all, including entries another transport recorded,
#: and it cannot read a channel-specific origin field to do it.
#:
#: The value is opaque and built only by :func:`owner_token`, so the one place that
#: decides what "the same principal" means is that function.
QUEUED_OWNER_KEY = "queued_owner"

#: Separates the parts inside an owner token. A unit separator, because every part is an
#: id, an email or a room name from a transport, and none of them can contain one -- so
#: two different principals cannot collide by spelling their parts with the separator.
_OWNER_SEP = "\x1f"

#: Neutral key naming the RESUMED session an entry was accepted for, written only when
#: the busy session was one the conversation resumed rather than its own native
#: session. A drain replays with command interpretation off, and that flag also
#: skips resume routing (a native entry must keep native affinity even if a binding
#: appeared after it was queued), so without this field a replay could only re-derive
#: the native key -- and an entry accepted for a resumed dashboard session would run
#: in the wrong conversation. The drain reads it and pins the replay to that key
#: instead. Absent means the entry was accepted for the native session.
QUEUED_RESUMED_KEY = "queued_resumed_key"

#: "Drain everything you own on this session key." The only capability a peer
#: channel is given, and the reason no address crosses this seam.
DrainCallable = Callable[[str], Awaitable[None]]

#: channel type -> that channel's drain. One dispatcher per channel per process, so a
#: later construction REPLACES an earlier one rather than accumulating: a stale
#: dispatcher holds a dead client, and waking it would post nothing while masking the
#: live one.
_DRAINS: dict[str, DrainCallable] = {}

#: session key -> the channel types whose drains are on the stack for it right now.
#: A woken drain sets aside the waker's own new entries and would wake it straight
#: back; refusing a target that is already draining is what makes the cascade
#: terminate.
_ACTIVE: dict[str, set[str]] = {}

#: session key -> channels a peer tried to wake while they were already draining.
#:
#: Refusing that wake is what bounds the cascade, but DROPPING it strands a message.
#: A drain's pump returns as soon as an iteration finds nothing of its own to answer,
#: while its active marker is still held across its own :func:`wake_other_drains`
#: await -- so an entry arriving in that window is set aside by the peer running under
#: that await, and the refused wake was the only thing that would have come back for
#: it. Its own channel's next turn tail is not a recovery: the message is already
#: accepted and receipted, and a channel that goes quiet never has a next turn. So the
#: request is RETAINED here and the refusing drain re-pumps before releasing its
#: marker.
_PENDING: dict[str, set[str]] = {}

#: How many times one drain will re-pump for a retained wake before leaving the rest to
#: the next drain of this session. A bound is required because each re-pump can itself
#: queue work for a peer that queues work back; under continuous two-channel traffic
#: that is real work, not a spin, but it must not be unbounded.
#:
#: Hitting the cap leaves the request OWED rather than consumed, so the record survives
#: for the next drain to act on: see the last round in :func:`drain_until_quiet`.
_MAX_WAKE_ROUNDS = 8


def owner_token(channel_type: str, sender_key: Iterable[object]) -> str:
    """The opaque token naming the principal *sender_key* identifies on *channel_type*.

    Pass the channel's own ``sender_key`` -- the tuple each transport already derives for
    "who sent this and where the reply goes", the same comparison that decides whether
    two queued messages may be collapsed into one turn. Ownership for a partial clear is
    that identical question, so it reads the identical value rather than a second notion
    of identity that could drift from it.

    The channel name leads, so two transports that happen to spell a user id the same way
    are still two principals. Built here rather than per channel so a producer and a
    clear cannot disagree about the spelling.
    """
    return _OWNER_SEP.join((str(channel_type), *(str(part) for part in sender_key)))


def tag_entry(kwargs: dict[str, str], channel_type: str, owner: str) -> dict[str, str]:
    """Record *channel_type* and *owner* as this queue entry's producer, and return *kwargs*.

    Every producer enqueueing onto a shared session key calls this, including a drain
    re-enqueueing an entry it set aside -- that path passes the entry's own kwargs
    straight back, so the tags ride along already and re-tagging them would be the one
    way a set-aside entry could change hands.

    *owner* comes from :func:`owner_token` and is what lets ``/stop`` drop one person's
    queued messages and leave everybody else's. It is REQUIRED, with no default: a
    producer that cannot name its principal passes ``""`` and says so at its own call
    site, because an empty token matches no caller and the entry it makes is one its
    own sender can never withdraw. Defaulting it would make that the silent outcome of
    forgetting the argument rather than a decision.

    Returns the same dict it was given so a producer can build and tag in one
    expression; the mutation is what matters.
    """
    kwargs[QUEUED_CHANNEL_KEY] = str(channel_type)
    kwargs[QUEUED_OWNER_KEY] = str(owner)
    return kwargs


def entry_owner(kwargs: dict) -> str:
    """Which principal queued this entry, or "" if it did not say.

    Absent means a producer that does not record it. Such an entry is nobody's to drop:
    it is left queued by every partial clear rather than guessed at, the same way an
    untagged channel makes it nobody's to answer.
    """
    return str(kwargs.get(QUEUED_OWNER_KEY) or "")


def entries_queued_by(owner: str) -> Callable[[dict], bool]:
    """The predicate selecting exactly the entries *owner* queued.

    Handed to ``SessionManager.clear_queue`` so the session layer decides nothing about
    ownership and learns no field name: it holds the entries, the predicate reads them.

    An empty *owner* selects NOTHING, deliberately: a caller that cannot name its
    principal must clear nothing rather than everything, because "everything" here is
    other people's unanswered messages. Whole-session teardown asks for that by passing
    no predicate at all, which is a different request and reads as one at the call site.
    """
    if not owner:
        return lambda _kwargs: False
    return lambda kwargs: entry_owner(kwargs) == owner


def entry_channel(kwargs: dict) -> str:
    """Which channel recorded this queue entry, or "" if it did not say.

    Absent means a producer that does not record it, which every drain handles as "not
    mine": the entry is set aside untouched rather than answered under an address this
    channel guessed. Nothing can be woken for it either, because nothing names who owns
    it -- so it waits for its own channel's next turn. Defaulting the other way would
    let any drain claim an untagged entry and reply to it in the wrong conversation.
    """
    return str(kwargs.get(QUEUED_CHANNEL_KEY) or "")


def tag_resumed_entry(kwargs: dict[str, Any], resumed_key: str | None) -> dict[str, Any]:
    """Record the RESUMED session *resumed_key* this entry was accepted for, if any.

    A ``None`` (the busy session was the conversation's own native one) writes
    nothing, so a native entry keeps the shape it always had and the drain keeps
    re-deriving the native key for it. Returns *kwargs* like :func:`tag_entry`.
    """
    if resumed_key:
        kwargs[QUEUED_RESUMED_KEY] = str(resumed_key)
    return kwargs


def entry_resumed_key(kwargs: dict) -> str:
    """The resumed session this entry was accepted for, or "" for a native entry."""
    return str(kwargs.get(QUEUED_RESUMED_KEY) or "")


def register_drain(channel_type: str, drain: DrainCallable) -> None:
    """Publish this channel's drain so a peer sharing its queue can wake it.

    Pass the SAME constant the channel tags its entries with. A registration name that
    does not match the tag cannot be woken for its own entries, which is why each
    dispatcher holds one ``_CHANNEL`` constant used for both.
    """
    _DRAINS[str(channel_type)] = drain


def unregister_drain(channel_type: str, drain: DrainCallable | None = None) -> None:
    """Drop *channel_type*'s drain (idempotent), so no peer wakes a dispatcher that is
    going away.

    Waking a dead dispatcher does not merely fail: its pump dequeues the entries it owns
    under its own lock and replays them AFTER releasing it, so a closed client raising in
    the replay loses messages it has already taken off the queue. A registration that
    outlives its client therefore turns every later cross-transport wake into a loss.

    *drain* is the registration the caller believes it owns, making this a
    COMPARE-AND-DROP. Two dispatcher lifetimes can overlap: a restart registers the
    replacement before the OLD client's close path runs, and an unconditional pop there
    would remove the LIVE one -- leaving the channel unwakeable until the next restart,
    which is the very stranding this module exists to prevent. Equality, not identity,
    because the registered drain is a bound method: each ``self._drain_queue`` access
    mints a new object, and two compare equal exactly when they name the same method on
    the same dispatcher.

    Omitted, the drop is unconditional, for a caller holding no handle on its own
    registration.
    """
    if drain is not None and _DRAINS.get(str(channel_type)) != drain:
        return
    _DRAINS.pop(str(channel_type), None)


def reset_drains() -> None:
    """Forget every registration. For tests, which build dispatchers freely."""
    _DRAINS.clear()
    _ACTIVE.clear()
    _PENDING.clear()


def _take_pending(channel_type: str, session_key: str) -> bool:
    """Whether a peer asked to wake *channel_type* while it was already draining.

    Consuming: the request is cleared as it is read, so one retained wake causes one
    re-pump and a quiet round ends the loop.
    """
    key = str(session_key)
    pending = _PENDING.get(key)
    if pending is None:
        return False
    asked = str(channel_type) in pending
    pending.discard(str(channel_type))
    if not pending:
        _PENDING.pop(key, None)
    return asked


def _peek_pending(channel_type: str, session_key: str) -> bool:
    """Whether a wake is owed to *channel_type*, WITHOUT consuming the request.

    Used on the last round, which cannot serve one: consuming it there would leave the
    entry queued AND lose the record that anything is owed for it, so not even a later
    peer drain's wake would know to look. Left in place, the next
    :func:`drain_until_quiet` on this session finds it and re-pumps.
    """
    return str(channel_type) in _PENDING.get(str(session_key), ())


async def drain_until_quiet(
    *,
    channel: str,
    session_key: str,
    pump: Callable[[set[str]], Awaitable[None]],
) -> None:
    """Run *channel*'s pump on *session_key*, wake the peers it set entries aside for,
    and pump again for any wake a peer could not deliver back to this drain.

    The active marker is held for the WHOLE of this, including across the wake: a woken
    peer would otherwise re-enter this drain and, with messages still arriving, nothing
    would bound the hops. The re-pump is what makes that bound safe to keep. A peer
    running under this drain's wake sets aside anything this channel queued meanwhile
    and asks to wake it; the marker refuses, :func:`wake_other_drains` retains the
    request, and this loop answers it here instead -- so the entry is handled in this
    drain rather than waiting for a next turn that a quiet channel never has.

    ``pump`` is given the set to record the channels whose entries it set aside; a fresh
    set per round, because an earlier round's peers have already been woken.

    Defined here rather than in each dispatcher because all four need exactly this
    sequence, and a channel that got the order wrong -- waking under its own queue lock,
    or outside its own marker -- would be a defect with no local symptom.
    """
    with draining(channel, session_key):
        for round_no in range(1, _MAX_WAKE_ROUNDS + 1):
            foreign: set[str] = set()
            await pump(foreign)
            await wake_other_drains(waker=channel, session_key=session_key, channels=foreign)
            if round_no < _MAX_WAKE_ROUNDS:
                if not _take_pending(channel, session_key):
                    return
                continue
            # The last round cannot serve another one, so it must not CONSUME the
            # request either: taking it here would leave the entry queued and lose the
            # only record that a wake is owed for it. Peeked and left in place, the next
            # drain on this session -- this channel's own turn tail, or a peer's wake --
            # finds it and re-pumps. Calling this channel's drain again after the marker
            # releases is the other half of what the finding suggests, and is not done:
            # that is re-entry, and under continuous two-channel traffic nothing would
            # bound it, which is what the cap exists for.
            if _peek_pending(channel, session_key):
                logger.warning(
                    "%s: still receiving cross-transport wake requests after %d drain "
                    "round(s) for session=%s; the wake stays owed and the remaining "
                    "queued messages drain on the next drain of this session",
                    channel,
                    _MAX_WAKE_ROUNDS,
                    session_key,
                )
            return


@contextmanager
def draining(channel_type: str, session_key: str) -> Iterator[None]:
    """Mark this channel as draining *session_key* for as long as the block runs.

    Read by :func:`wake_other_drains` to refuse a target already on the stack, which
    is what bounds the cascade. Reference-counted per channel is unnecessary: a
    channel's drain is not re-entered on one session (its own pump loops instead).
    """
    key = str(session_key)
    active = _ACTIVE.setdefault(key, set())
    active.add(str(channel_type))
    try:
        yield
    finally:
        active.discard(str(channel_type))
        if not active:
            _ACTIVE.pop(key, None)


async def wake_other_drains(*, waker: str, session_key: str, channels: Iterable[str]) -> None:
    """Drain *channels* on *session_key*.

    MUST be called with the caller's queue lock RELEASED. A woken drain takes its own
    channel's lock and re-enters its own ``handle_message``, so waking under the
    waker's lock would hold it across another channel's whole turn.

    Skipped, in every case without raising: the waker itself, a channel with no
    registered drain (its dispatcher is not running in this process), and a channel
    already draining this session key. A drain that raises is logged and the rest
    still run -- one channel whose client is not connected must not strand the
    entries of the others, and the waker has already finished its own work.

    A target skipped because it is ALREADY DRAINING is retained in :data:`_PENDING`
    rather than dropped: that drain's pump may already have returned while its marker
    is still held, in which case nothing else would ever come back for the entry.
    :func:`drain_until_quiet` is what consumes the retention.
    """
    already: frozenset[str] | set[str] = _ACTIVE.get(str(session_key)) or frozenset()
    candidates = [
        name
        for name in dict.fromkeys(str(c) for c in channels if c)
        if name != str(waker) and name in _DRAINS
    ]
    retained = [name for name in candidates if name in already]
    if retained:
        _PENDING.setdefault(str(session_key), set()).update(retained)
    targets = [name for name in candidates if name not in already]
    for name in targets:
        drain = _DRAINS[name]
        try:
            await drain(str(session_key))
        except Exception:
            # Retire it. A drain that raises has proven its dispatcher cannot serve this
            # queue, and waking it again would dequeue more entries only to lose them in
            # the same replay. Compare-and-drop against the callable just invoked, so a
            # dispatcher that RESTARTED during this wake keeps its live registration.
            # After this the channel is simply absent, which is the same path as a
            # channel whose dispatcher never ran here: its entries are set aside and
            # nothing is woken for them, so they wait rather than being consumed.
            #
            # This covers a client that dies without an orderly shutdown, which a
            # close-time unregister cannot. It does not replace one: the first wake after
            # the client dies still reaches it once.
            unregister_drain(name, drain)
            logger.warning(
                "%s: waking the %s queue drain failed for session=%s; that drain is "
                "retired and its queued messages wait for a live dispatcher",
                waker,
                name,
                session_key,
                exc_info=True,
            )
