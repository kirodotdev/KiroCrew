"""Slack option controls for a dashboard session, and whether its mirrors are paused.

Which slot and session keys own the options control posted in a Slack thread, the
per-session records of outstanding controls, minting a control's staleness token,
judging and expiring a superseded control, and whether a turn's Slack or other mirror
is paused.

Served through :mod:`kiro_crew.dashboard.chat_utils`, which callers and tests import and
patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from kiro_crew.dashboard.chat_utils_slots import effective_session_key
from kiro_crew.dashboard.state import DashboardState, _ChatSlot, _normalize_slot_key
from kiro_crew.history import transcript_sort_key
from kiro_crew.messaging.link import canonical_key
from kiro_crew.slack.outbound import (
    decode_options_token,
    encode_options_token,
    expire_options,
    mark_options_terminal,
    options_edit_lock,
)

if TYPE_CHECKING:
    from kiro_crew.slack.outbound import PostedOptions

logger = logging.getLogger("kiro_crew.dashboard.chat_utils")


def slack_options_slot(state: DashboardState, session_key: str) -> _ChatSlot | None:
    """The slot holding *session_key*'s Slack OPTIONS state, if one exists.

    Deliberately not routed through :func:`dashboard_slot_key`, which answers
    "is a tab open?". A slot can hold OPTIONS state with no tab currently open,
    and one lookup reaches both flavours of slot: a channel-born slot
    (``slack_<ts>``) and a dashboard slot mirroring out to Slack
    (``chat-<n>-<epoch>``) both live in the same registry.

    Returns None rather than raising for any state object that cannot answer the
    question. OPTIONS bookkeeping is best-effort cleanup and must never be able
    to abort the turn that triggered it.

    The key is required to be a real ``str``: ``_normalize_slot_key`` strips a
    repeated ``dashboard_`` prefix with an unbounded ``while``, which only
    terminates for a genuine string. Handing it anything whose ``startswith``
    is always truthy spins forever, allocating as it goes -- so a non-string
    key is refused here rather than normalized.
    """
    if not isinstance(session_key, str):
        return None
    getter = getattr(state, "get_slot", None)
    if not callable(getter):
        return None
    try:
        slot = getter(_normalize_slot_key(session_key))
        if slot is not None:
            return slot
        # The fold is FILENAME-shaped, so any slot whose name is not its session
        # key folded is unreachable through it. A cron slot is named
        # ``cron-<id>`` while its session key is ``cron:<id>``, which folds to
        # ``cron_<id>`` and matches nothing — so a persistent cron's OPTIONS
        # control was never tracked at all, and the follow-up turn had nothing
        # to expire, leaving it clickable into a superseded question.
        #
        # Such a slot still knows its own identity (``linked_session_key``), so
        # ask the slots rather than guessing at more spellings. Only on a miss,
        # so the common path stays a single dict lookup.
        for candidate in (getattr(state, "_slots", None) or {}).values():
            if effective_session_key(candidate) == session_key:
                return candidate
        return None
    except Exception:
        logger.debug("Slack OPTIONS slot lookup failed", exc_info=True)
        return None


def slack_options_linked_slot(state: DashboardState | None, thread_ts: str) -> _ChatSlot | None:
    """The dashboard slot that owns *thread_ts*, if a session mirrors into it.

    Prefers the thread -> slot reverse index, then falls back to scanning slots
    for a matching ``_slack_thread_ts``. The fallback exists because the index is
    written by one helper that a caller can forget: relying on it alone made this
    resolver silently return nothing for a freshly-linked thread.
    """
    if not thread_ts or state is None:
        return None
    linked = getattr(state, "get_linked_slot", None)
    if callable(linked):
        try:
            slot = linked(thread_ts)
        except Exception:
            slot = None
        if slot is not None:
            return slot
    slots = getattr(state, "_slots", None)
    if not isinstance(slots, dict):
        return None
    for slot in slots.values():
        if getattr(slot, "_slack_linked", False) and (
            getattr(slot, "_slack_thread_ts", "") == thread_ts
        ):
            return slot
    return None


def _persisted_thread_owner(state: DashboardState | None, thread_ts: str) -> str:
    """The session key the PERSISTED thread index maps *thread_ts* to.

    Distinct from :func:`slack_options_linked_slot`, which only knows the
    dashboard SLOT index. A cron thread is linked with ``cron:<id>`` and has no
    slot at all, so the slot index cannot see it — yet that is the key a control
    on such a thread is recorded under, because the record sites resolve the owner
    through this same index. Leaving it out of the ownership helpers made the
    record and the forget disagree: the control was filed under ``cron:<id>`` and
    then never cleared, so a later expiry found it and overwrote the selection.

    Returns "" when unknown. The ``isinstance`` check is deliberate: the index is
    typed ``str | None``, and anything else means the caller handed us a stub.
    """
    if state is None or not thread_ts:
        return ""
    sessions = getattr(state, "sessions", None)
    if sessions is None:
        return ""
    try:
        owner = sessions.get_session_for_thread(thread_ts)
    except Exception:
        logger.debug("Could not resolve the persisted owner of %s", thread_ts, exc_info=True)
        return ""
    return owner if isinstance(owner, str) and owner else ""


def slack_options_owner_key(state: DashboardState | None, thread_ts: str) -> str:
    """The single session key that owns the conversation living in *thread_ts*.

    Use this when RECORDING a control — it has to land on the one session whose
    next turn should spend it. Use :func:`slack_options_session_keys` when
    CLEARING, where covering every candidate is correct.

    The slot index is consulted first and the persisted thread index second. Where
    both know the thread they agree (``link_slack`` writes both), so the order only
    matters for a thread ONE of them can see — and a cron-linked thread is visible
    only to the persisted one.
    """
    slot = slack_options_linked_slot(state, thread_ts)
    if slot is not None:
        mirrored = effective_session_key(slot)
        if mirrored:
            return mirrored
    persisted = _persisted_thread_owner(state, thread_ts)
    if persisted:
        return persisted
    return canonical_key(thread_ts) if thread_ts else ""


def slack_options_session_keys(state: DashboardState | None, thread_ts: str) -> list[str]:
    """Every session key under which *thread_ts*'s OPTIONS control may be recorded.

    One Slack thread belongs to one conversation, but that conversation is
    addressed by several different keys depending on which side owns it: a
    Slack-born session is ``slack:<ts>``, a dashboard session mirroring out to the
    thread is ``dashboard:<slot>``, and a persistent cron is ``cron:<id>``. A
    caller holding only the thread timestamp cannot tell which, so return every
    candidate — they name the same conversation, so acting on all of them is
    correct rather than merely safe.

    Missing the cron spelling here is what let a selection leave its record
    behind: the forget cleared the keys it could guess, the ``cron:<id>`` record
    survived, and the next expiry edited over the user's answer.
    """
    if not thread_ts:
        return []
    keys = [canonical_key(thread_ts)]
    slot = slack_options_linked_slot(state, thread_ts)
    if slot is not None:
        mirrored = effective_session_key(slot)
        if mirrored and mirrored not in keys:
            keys.append(mirrored)
    persisted = _persisted_thread_owner(state, thread_ts)
    if persisted and persisted not in keys:
        keys.append(persisted)
    return keys


def options_records(state: DashboardState | None, session_key: str) -> tuple[PostedOptions, ...]:
    """Every OPTIONS control still outstanding for *session_key*.

    The store is keyed by SESSION KEY, on ``DashboardState``, not held on the
    slot. A plain Slack thread frequently has no dashboard slot, and a slot-held
    record is simply dropped for those sessions — nothing then tracks the control,
    no later turn can expire it, and the stale click this whole lifecycle exists
    to prevent stays possible. Keying by session key makes the slotless
    case ordinary instead of special, and it cannot go stale when a slot appears
    or disappears mid-conversation.
    """
    if state is None or not session_key:
        return ()
    store = getattr(state, "_slack_options_by_key", None)
    if not isinstance(store, dict):
        return ()
    return store.get(canonical_key(session_key), ())


def set_options_records(
    state: DashboardState | None, session_key: str, records: tuple[PostedOptions, ...]
) -> None:
    """Replace *session_key*'s outstanding controls, dropping the key when empty.

    Pruning on empty is the ONLY bound, and it is the right one: an entry exists
    exactly as long as a question is still unanswered, and it leaves the moment the
    lifecycle completes — the expiry settles it, a click forgets it, or an unlink
    clears it.

    Deliberately NOT capped with eviction. A cap sounds prudent and is actively
    harmful here: evicting a record for a control that is still clickable means no
    later turn can retire it, which is precisely the untracked control this whole
    lifecycle exists to eliminate — so a bound would reintroduce the defect at
    scale, silently, on the busiest instances. The footprint is small regardless:
    an entry exists only for a conversation with a live unanswered question, which
    is strictly fewer than the (itself unbounded) number of slots.
    """
    if state is None or not session_key:
        return
    store = getattr(state, "_slack_options_by_key", None)
    if not isinstance(store, dict):
        return
    key = canonical_key(session_key)
    if records:
        store[key] = records
    else:
        store.pop(key, None)


def remember_slack_options(
    state: DashboardState | None,
    session_key: str,
    posted: PostedOptions | None,
) -> None:
    """Record the live OPTIONS control just posted for *session_key*.

    APPENDS rather than replaces. A turn can post more than one OPTIONS message,
    and the same slot is reachable from several posting paths, so overwriting
    would leave the earlier control on screen with nothing tracking it — a click
    on it would then answer a question the conversation has already passed.
    Every outstanding record is kept so expiry can drain all of them.

    A no-op when there is no control or no dashboard state. Note there is NO
    slot requirement: the store is keyed by session key precisely so a plain
    Slack thread without a slot still gets its control tracked.
    """
    if posted is None or state is None or not session_key:
        return
    current = options_records(state, session_key)
    # Same message posted twice (a retry, or two paths recording one post)
    # must not queue two edits for one control.
    if posted not in current:
        set_options_records(state, session_key, (*current, posted))


def forget_slack_options(
    state: DashboardState | None, session_key: str, ts: str | None = None
) -> None:
    """Drop the recorded control for *session_key* without editing Slack.

    For when something else has already spent the control — a Send click
    re-renders the message with the user's selection, and striking every choice
    through afterwards would erase the choice they made.

    Pass *ts* to drop ONLY the control posted as that message. A click spends one
    control, not every control outstanding in the conversation: dropping them all
    would leave any other one on screen with nothing tracking it, so a later click
    on it would answer a superseded question. Omitting *ts* clears all of them,
    which is right when the whole conversation is going away (an unlink).
    """
    if state is None or not session_key:
        return
    if ts is None:
        set_options_records(state, session_key, ())
        return
    set_options_records(
        state,
        session_key,
        tuple(p for p in options_records(state, session_key) if p.ts != ts),
    )


def slack_options_owner_keys_snapshot(
    state: DashboardState | None, thread_ts: str
) -> tuple[str, ...]:
    """The keys *thread_ts*'s control could be recorded under, captured NOW.

    A caller that is about to await Slack has to take this BEFORE the await and
    forget against it afterwards. Recomputing after the fact reads the keys of
    whoever owns the thread THEN: a relink landing during a submit's edit moves the
    thread to another session, so the recomputed list names the new owner, the
    previous owner's record survives the click, and that session's next turn edits
    straight over the selection the user just made.
    """
    return tuple(slack_options_session_keys(state, thread_ts))


def mint_options_token(
    state: DashboardState | None,
    asker_key: str,
    row_ts: str | None = None,
) -> str | None:
    """The staleness token to post with a control asked by *asker_key*.

    Pairs the asking conversation with how far it had got when the question was
    asked.

    *asker_key* is supplied by the caller rather than resolved from the thread.
    The caller knows which session ran the turn; resolving the thread's owner here
    would name whoever owns it at MINT time, and a link landing between the turn
    starting and its footer going out would stamp the control with a conversation
    that never asked the question.

    *row_ts* likewise comes from the caller when it already holds the value. That
    keeps this free of I/O: reading the tail off disk takes the transcript's
    cross-process flock, so on a contended session it is not a bounded cost and
    has no business on the event loop. Passing the row the caller already has in
    memory is the same value -- a replayed row preserves its ``ts`` verbatim.

    Falls back to a disk read only when the caller has nothing, and that path is
    BLOCKING: run it in a thread. ``None`` means the control posts untokened,
    which the check reads as "cannot prove staleness" and honours.
    """
    try:
        if not asker_key:
            return None
        if not row_ts:
            log = getattr(state, "conversation_log", None)
            if log is None:
                return None
            row_ts = log.last_row_ts(asker_key)
        if not row_ts:
            return None
        return encode_options_token(asker_key, row_ts)
    except Exception:
        logger.debug("Could not mint an OPTIONS staleness token", exc_info=True)
        return None


async def options_control_is_stale(
    state: DashboardState | None, block_id: str | None, thread_ts: str
) -> bool:
    """Whether the control carrying *block_id* is answering a superseded question.

    The whole check: the token says which conversation asked and where that
    conversation stood at the time; the transcript on disk says where it stands
    now. A conversation that has moved on has superseded its own question.

    Nothing in gateway memory takes part, which is what makes this survive a
    restart -- the token is in the Slack message and the comparand is a persisted
    transcript row, so neither half is lost when the process dies. The counter a
    previous design compared against could NOT be used here: it is rebuilt from a
    windowed replay of the transcript on startup, so it climbs back through values
    it has already issued and reads a pre-restart token as current.

    ABSTAINS to False -- honour the click -- whenever staleness cannot be PROVEN:
    no token, a token this build cannot parse, an unreadable transcript, or an
    unparseable timestamp. Refusing a legitimate answer is worse than accepting a
    late one, and a control posted before this check existed carries no token at
    all.
    """
    token = decode_options_token(block_id)
    if token is None:
        return False
    asker_key, minted_ts = token
    try:
        # ONE comparison: has the conversation that ASKED moved on?
        #
        # Deliberately no ownership check. It would answer the wrong question now
        # that an accepted click carries its destination: the answer reaches the
        # conversation that asked it whatever the thread's ownership has since
        # done, so a thread changing hands does not make a still-pending question
        # unanswerable -- and refusing on that basis would reject a click the user
        # was legitimately shown. Handover is not supersession; only the asker's
        # own transcript advancing is.
        log = getattr(state, "conversation_log", None)
        if log is None:
            return False
        current_ts = await asyncio.to_thread(log.last_row_ts, asker_key)
        if not current_ts:
            return False
        return transcript_sort_key(current_ts) > transcript_sort_key(minted_ts)
    except Exception:
        logger.debug("Could not judge whether an OPTIONS control is stale", exc_info=True)
        return False


def forget_slack_options_for_thread(
    state: DashboardState | None,
    thread_ts: str,
    ts: str | None = None,
    keys: tuple[str, ...] | None = None,
) -> None:
    """Drop the recorded control for the conversation living in *thread_ts*.

    For callers that hold a Slack thread timestamp rather than a session key —
    the interaction handlers, which see a click on a message and not the session
    behind it. Clears every key the thread's conversation can be recorded under,
    so a control posted by the dashboard mirror is forgotten too.

    *ts* scopes it to the ONE control posted as that message, which is what a
    click spends. Without it every outstanding control in the conversation is
    dropped, leaving any other one clickable with nothing tracking it.

    Pass *keys* from :func:`slack_options_owner_keys_snapshot` when an await sits
    between reading ownership and clearing it — a relink during that window would
    otherwise leave the previous owner's record behind. Omitting *keys* resolves
    now, which is right only for a caller that has not awaited.
    """
    for key in keys if keys is not None else slack_options_session_keys(state, thread_ts):
        forget_slack_options(state, key, ts)


async def expire_slack_options(
    state: DashboardState | None, session_key: str, ts: str | None = None
) -> None:
    """Spend the OPTIONS control left from *session_key*'s previous turn.

    Called as a new turn begins, whichever surface it arrives on, so a control
    the conversation has moved past stops inviting a click that would answer a
    superseded question.

    Records stay TRACKED across the Slack edit and are only ever REMOVED
    afterwards, never re-added. A write-back that re-adds cannot tell "still
    outstanding" from "deliberately removed while I was awaiting": a click landing
    mid-await calls :func:`forget_slack_options`, and re-adding would resurrect
    the control it just answered, so every later turn would re-edit the message
    and overwrite the user's selected summary. Removing only what settled leaves
    a concurrent forget authoritative. A record whose edit failed *transiently* is
    simply never removed, so it is still retried later — dropping it would leave a
    live control on screen with nothing tracking it, the exact stale click this
    whole lifecycle exists to prevent. A failure that will never succeed (deleted
    message, a channel we are not in) counts as settled, so it cannot be retried
    on every later turn forever.

    Two concurrent expiries can therefore both edit the same control. That is
    deliberate: both write byte-identical spent blocks, so the cost is a wasted
    API call, whereas resurrecting an answered control corrupts what the user
    sees.

    Drains EVERY outstanding control, not just the newest: a turn can leave more
    than one on screen, and any one left untracked stays clickable into a
    superseded question.

    Pass *ts* to spend ONLY the control posted as that message. A caller that is
    cleaning up after ITSELF has to narrow this way: a concurrent turn can record
    its own fresh control in the same slot while this caller is still awaiting
    Slack, and a session-wide drain would strike that newer question through too
    — leaving the question the conversation is actually waiting on unanswerable.
    Omitting *ts* drains all of them, which is what a NEW turn wants (it
    supersedes everything before it) and what an unlink wants (the whole
    conversation is going away).
    """
    if state is None or not session_key:
        return
    outstanding = options_records(state, session_key)
    if not outstanding:
        return
    if ts is not None:
        outstanding = tuple(posted for posted in outstanding if posted.ts == ts)
        if not outstanding:
            # Already spent by whoever else tracked it; nothing of ours to edit.
            return
    slack = getattr(state, "slack_client", None)
    if slack is None:
        # Nothing was spent, so nothing may be dropped: with no client the
        # controls are still live on screen and must stay tracked.
        return
    settled: list[PostedOptions] = []
    for posted in outstanding:
        # Serialize against the Send handler's edit to this SAME message, and
        # re-read the record INSIDE the lock. A click that won the race has
        # already rewritten the message with the user's selection and dropped the
        # record, so finding it gone means "do not edit" -- without the re-read,
        # a late expiry would erase the answer the user just gave. The lock makes
        # that check trustworthy; the check is what makes the lock useful.
        async with options_edit_lock(posted.channel, posted.ts):
            if posted not in options_records(state, session_key):
                continue
            if await expire_options(slack, posted):
                # Retire the control for clicks too, not just for our records. A
                # Send click queued behind this expiry would otherwise find the
                # answer claim unheld, take it, and dispatch an answer to the
                # question we just struck through. Marked while we still hold the
                # lock, so the queued click cannot slip between the edit and this.
                mark_options_terminal(posted.channel, posted.ts)
                settled.append(posted)
    if settled:
        # Remove by identity against the CURRENT records, not by reassigning a
        # remembered tuple: a turn that finished while we awaited Slack may have
        # recorded its own control here, and it must survive.
        set_options_records(
            state,
            session_key,
            tuple(p for p in options_records(state, session_key) if p not in settled),
        )


def slack_mirror_is_paused(state: Any, session_key: str) -> bool:
    """True when a turn must NOT be mirrored to the session's linked thread.

    A pause retains the thread binding, so every inbound resolver still sees the
    link and a reply still reaches the session that owns the thread. This is the
    one predicate that tells outbound egress apart from that: consult it before
    SENDING, never before routing. Gating a routing decision on it would fork a
    new session out of a reply the user expected to continue this one.

    Scope is deliberately turn mirroring — the user echo, its tool stream, the
    assistant reply, an auth-required error and the linked approval prompt.
    Deliveries that merely reuse the thread as an ADDRESS (a cron result, a
    subagent completion, a requested file, an auto-nudge tick) are NOT gated: an
    absent link makes those fall back to the owner's DM, and one of them deletes
    the auto-nudge loop outright, so treating paused as no-link there would
    reroute messages the user asked for and destroy a live monitor.

    The channel compaction notice is also not gated, and belongs with the
    address-based deliveries above rather than with turn output: it reports that
    the session's own history was compacted, which stays true whether or not the
    conversation is currently connected. (An earlier version of this note claimed
    it "cannot reach a paused link" because an origin had no disconnect control.
    Origin rows now DO carry one, so that reasoning is void — the exclusion
    stands on the delivery's kind, not on the row's affordances.)

    Strict ``is True`` rather than truthiness, and fails OPEN: ``sessions`` is a
    bare ``MagicMock`` across much of the suite and returns a truthy child for
    any unstubbed accessor, so truthiness here would silence every linked thread
    in the test suite. Failing open leaves a muted thread noisy at worst; failing
    closed would make a live thread silently dead.
    """
    sessions = getattr(state, "sessions", None)
    if sessions is None:
        return False
    try:
        return sessions.is_slack_paused(session_key) is True
    except Exception:
        logger.debug("slack pause lookup failed for %s", session_key, exc_info=True)
        return False


def mirror_is_paused(state: Any, session_key: str, *, origin: bool = False) -> bool:
    """True when a turn must NOT be mirrored to one of the session's non-Slack deliveries.

    The channel-neutral twin of :func:`slack_mirror_is_paused`, and what a
    dashboard disconnect suppresses for a non-Slack channel.

    ``origin`` names WHICH delivery is being asked about, because a session can
    hold two at once — the conversation it was born in and an explicit mirror —
    and they mute independently. Callers that resolve a single outbound target
    pass the flag matching the row the user acted on; see
    :meth:`SessionMap.set_mirror_paused`.

    The scope is narrower than Slack's because the hazard Slack has does not
    exist here: no cron result, subagent completion, requested file or auto-nudge
    tick reads a mirror binding at all — those address a channel explicitly — so
    gating this cannot reroute a delivery to the owner's DM or destroy a monitor
    loop. It covers the two sites that carry turn output: the user echo and the
    assistant reply.

    Same ``is True`` / fail-open contract as the Slack gate, for the same
    MagicMock reason. A muted binding must stay visible to
    ``find_mirror_sessions``, to the resume-conflict check and to both clear
    paths, or in-channel ``!unlink`` and conflict detection break.
    """
    sessions = getattr(state, "sessions", None)
    if sessions is None:
        return False
    try:
        return sessions.is_mirror_paused(session_key, origin=origin) is True
    except Exception:
        logger.debug("mirror pause lookup failed for %s", session_key, exc_info=True)
        return False
