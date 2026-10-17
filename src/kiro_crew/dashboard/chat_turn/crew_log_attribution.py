"""What a dashboard session's crew log records about it: the predecessor store, the served
model, lineage, the seeded tree, the inherited parent, the workspace and the memory/app/channel
class."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import CrewLogPrevious, crew_log_emit, logger


#: ACP tool statuses that END a call. `completed` is covered by `tool_final`; the
#: rest are the terminal answers the backend can give instead, and a durable record
#: has to take each of them as the call's outcome rather than leave the call open for
#: the turn-close sweep to guess at. A status NOT in here (`in_progress`, `pending`,
#: an unknown word) leaves the call open, which is the honest reading: the frame did
#: not say the call was over.
async def _slot_predecessor_store(sessions: Any, slot: Any, session_key: str) -> CrewLogPrevious:
    """The store *slot* was writing before, for a slot THIS process has not opened one for.

    The fallback pair behind the slot's own record, which the latch prefers and which
    this deliberately does not consult. That record is the only source that can name a
    store whose unit is not on disk yet: `on_session_opened` queues the create to the
    writer thread, so between the allocation and that job running there is nothing for
    any reader to find, and a slot's second allocation inside that window would
    otherwise answer the store before the first and orphan it.

    What the record cannot do is survive its process, which is where these two come in.
    The STORE decides between them: the units under this slot's key, ordered by the
    succession edges they recorded, are durable, so a gateway that restarts reads the
    same answer the process before it held. The slot-to-session mapping serves only
    where the store DEFINITELY has nothing -- a slot with no unit, a launch with the
    crew log off -- because it is a proxy for this question rather than an answer to
    it: an allocation whose history replay is pending holds the prior resumable id
    there deliberately, so for that window the mapping names a generation older than
    the store the slot is writing.

    Keyed by ``slot.key`` on the store side and by *session_key* on the mapping side,
    which are the keys each one actually holds: a unit's header records the SLOT, while
    a channel-born slot runs its turns on the channel's session key
    (``effective_session_key``), so reading the units under the session key would find
    none for exactly those slots.

    The store read hops a thread. ``mapped_sid`` is one dict lookup, no disk and no
    mutation, and it is the non-pruning accessor on purpose: ``resumable_sid`` stats
    the ACP transcript and PRUNES the entry when that file is gone, which erases the
    id exactly when the two stores disagree -- a crew log unit can outlive a
    truncated transcript, and that unit is the one whose tail most needs closing.

    An UNDECIDED store read gets no mapping. The two empties are different facts: no
    unit at all is a definite answer and the mapping is the only source left, while
    "the units could not be read, or do not say" means falling back would hand the
    edge to the source this read was preferred over -- a generation behind inside the
    replay window, and frozen into an append-only entry with no second chance. So
    that case names no store AND reports itself undecided, which the announce records
    as a chain BREAK: one citation lost while the fault lasts, and the log says so,
    rather than claiming to be the slot's first store and inviting a later fold to
    elect the store before it.

    The mapping's one bad state is handled where it is ANSWERABLE, not here. It is
    knowingly not ordering while an allocation holds the prior resumable id for an
    ACP provider that defers promotion, so a decided-empty store read plus a pending
    replay means neither source can say which store is current -- reachable on an
    upgraded slot whose stores all predate this edge, where the successor citing the
    mapping orphans the store between them, which is this defect. But the marker
    saying replay is owed belongs to a LIVE session, and this runs before this turn's
    session is allocated, so asking from here answers "no replay owed" both when none
    is owed and when there is no session to ask -- and the second is a cold start,
    which is the restart this read exists to survive. So the mapped id comes back
    flagged ``from_mapping`` and the slot downgrades it to a break as the edge is
    taken, after the session exists. A named id still proves a predecessor EXISTS,
    which is why that case records a break rather than nothing; an empty mapping
    names none, and claiming one would be a false statement of its own.
    """
    if getattr(slot, "_crew_log_opened_sid", "") or getattr(slot, "_crew_log_previous_sid", ""):
        # Tier 1 already answers, and the latch prefers it over anything returned here,
        # so the two reads below would be spent on a value that is then discarded. It
        # is not a cheap discard: the store read lists the session root and reads a
        # line pair per unit of the slot, so paying it on every warm turn grows with
        # the store rather than with this slot. Undecided is false because nothing was
        # read to be undecided ABOUT -- the record, not a refusal, is what answers.
        return CrewLogPrevious(sid="", undecided=False)
    derived, decided, complete = await asyncio.to_thread(
        crew_log_emit.slot_previous_store, slot.key
    )
    if derived:
        return CrewLogPrevious(sid=derived, undecided=False)
    if decided:
        # The store has no succession to read, so the mapping is the only source left
        # -- which is the whole migration path for an upgraded slot. It is handed back
        # PROVISIONAL rather than cited here, because the one state in which it is
        # knowingly not ordering cannot be read from this point: allocation holds the
        # prior resumable id for a provider that defers promotion, the marker saying so
        # is an attribute of a live session, and this runs before this turn's session
        # exists. Asking here returns "no replay owed" both when none is and when there
        # is nobody to ask, and the second is a cold start -- the case where the mapping
        # is most likely to be holding the older generation, and the restart this whole
        # read exists to survive. The decision is made as the edge is taken.
        #
        # ``complete`` decides whether an EMPTY mapping is a finding. When the store
        # holds no unit of this slot, nothing has a predecessor to hide and the pair
        # states the slot's first store. When it holds units it could not rank, the
        # mapping having nothing to give is not a fact about this slot, so nothing is
        # determined and the entry writes no key -- which is what makes it read as the
        # legacy silence it is instead of declaring itself a chain start.
        return CrewLogPrevious(
            sid=sessions.mapped_sid(session_key),
            undecided=False if complete else None,
            from_mapping=True,
        )
    return CrewLogPrevious(sid="", undecided=True)


def _crew_log_model(slot: Any, fallback: str = "") -> str:
    """The model the session RUNS on, for the crew log.

    ``slot.model`` is the CONFIGURED pin and can name a model the session never
    ran. A withheld pin is deliberately kept -- the composer chip still shows it --
    while the session runs on the backend default, and an unpinned slot's pin is
    backfilled only after the session opens. Writing the pin into an append-only
    entry therefore states a model that was not used, in a file nothing rewrites.

    ``served_model`` is the session fact, and it is empty when the backend serves
    its own default. The crew log records that emptiness rather than naming a model,
    for the same reason it omits an unmeasured token count: absent beats false.

    *fallback* covers only a slot shape that cannot report the fact at all -- the
    minimal test doubles -- because a real slot answering ``""`` means auto.
    """
    if hasattr(slot, "served_model"):
        return str(getattr(slot, "served_model", "") or "")
    return fallback


def _crew_log_lineage(slot: Any) -> tuple[str, str]:
    """The ``(parent_slot, parent_sid)`` a session's ``session/opened`` may cite.

    Both come from the slot, and both are used ONLY when ``_lineage_minted`` says
    THIS gateway process stamped them at ``session_create`` time. ``_created_by``
    is also restored from transcript metadata for the ownership boundary, and that
    file is editable by an agent's file tools, so promoting a restored value to
    gateway-authored lineage would let a metadata edit forge the one record the
    crew log's fence exists to protect. A slot with no witness -- a person's own
    tab, a fork, a restore after a restart -- yields two empty strings, and the
    emitter then writes no ``parent`` at all.
    """
    if not bool(getattr(slot, "_lineage_minted", False)):
        return "", ""
    return (
        str(getattr(slot, "_created_by", "") or ""),
        str(getattr(slot, "_created_by_sid", "") or ""),
    )


async def _crew_log_seed_tree(slot: Any) -> None:
    """Seed the crew log's session tree off the loop, for :func:`_crew_log_inherited_parent`.

    A restored slot's first turn after a restart is the ordinary moment the projection
    is still unseeded, and an unseeded read answers ``""``: the new log would then carry
    no ``parent``, and once retention removes the first log the edge is gone for good.
    ``ensure_seeded`` is idempotent, so every call after the first returns at once.

    Awaited BEFORE the turn takes its predecessor latch, never between the take and the
    emit: a cancellation here then leaves the latch as it was, while one after the take
    would lose the predecessor the slot's next log has to cite. Only for a slot that came
    back with a creator, the one case the read below can use. Never raises.
    """
    if not str(getattr(slot, "_created_by", "") or ""):
        return
    try:
        if not crew_log_emit.enabled():
            return
        from kiro_crew.crew_log.session_tree_projection import projection

        await asyncio.to_thread(projection().ensure_seeded)
    except asyncio.CancelledError:
        raise
    except Exception:
        return


def _crew_log_inherited_parent(slot: Any, head_sid: str) -> str:
    """The creator the crew log's own tree holds for *slot*, or ``""``. No I/O, no await.

    The fallback for a slot with no witness in this process (see
    :func:`_crew_log_lineage`): its new log re-cites the edge the tree already folded,
    so the edge survives the eviction and retention that remove the slot's FIRST log.
    Read from the fold of the crew log, never from restored metadata, so the fence
    above still holds. Never raises. Synchronous on purpose: it runs between the
    predecessor take and the emit, where nothing may yield (see
    :func:`_crew_log_seed_tree`, which seeds the tree before the take).

    Only for a slot that came back with a creator, and only when the record it reads
    names the SAME one. A slot key is reusable: a tab opened under the name of a closed
    worker has no creator of its own, and the tree still holds the old unit's edge under
    that key, so reading the tree alone would hand the new tab the old worker's lead. The
    restored ``_created_by`` alone is not evidence either (see above). Each check covers
    the other's gap: a metadata edit cannot name a creator the fold does not hold, and
    the fold cannot attach a parent to a slot nobody restored.

    Two sources, by whether the slot has a predecessor log. *head_sid* is the predecessor
    the new log cites: when it is named, the creator comes from the edge the tree already
    folded for the slot (:func:`inherited_parent`). When it is empty -- a child restored
    after a restart BEFORE its first turn, which has no predecessor -- the creator comes
    instead from the trusted ``session/spawned`` record the creator wrote at
    ``session_create`` (:func:`spawned_creator`); that is the record meant to carry the
    child across exactly this restart, so a child whose first turn runs after the restart
    still cites its creator rather than opening as a root. Both answer ``""`` when the
    fence is not met.
    """
    restored = str(getattr(slot, "_created_by", "") or "")
    if not restored:
        return ""
    key = str(getattr(slot, "key", "") or "")
    try:
        if not crew_log_emit.enabled():
            return ""
        from kiro_crew.crew_log.session_tree_projection import inherited_parent, spawned_creator

        if head_sid:
            inherited = inherited_parent(key, head_sid)
        else:
            # No predecessor log to inherit from -- the child restored after a restart
            # that happened before its first turn. The edge then lives only in the
            # creator's session/spawned record, written at session_create for exactly
            # this case. Reading it here lets the child's first session/opened cite the
            # creator, so the lineage survives in the child's own log.
            inherited = spawned_creator(key)
    except Exception:
        return ""
    return inherited if inherited == restored else ""


#: Slot attribute carrying a channel binding that committed BEFORE the session had a
#: crew log to record it in. The recorder sets it in that case instead of dropping the
#: fact, and :func:`_crew_log_class` folds it in, so the opening entry of a log created
#: later still states the restriction. Never cleared: the fold holds each member at the
#: most restrictive value the log ever recorded, and a session that was published once
#: holds that content for good.
PENDING_CHANNEL_ATTR = "_crew_log_pending_channel"


def _crew_log_workspace(slot: Any) -> str:
    """The workspace a session's crew log records, or ``""`` when the slot states none.

    Read alongside the class rather than folded into it, because a workspace is an
    IDENTITY and the class members are RESTRICTIONS -- there is no more-restrictive
    workspace for the class fold to keep, so it holds the first one stated and records a
    later different one as a move instead.

    A live slot always states one (it defaults to ``default``), so an empty answer means
    the object is not a slot -- a state double in a test. The arm that compares
    workspaces refuses on empty rather than treating it as a match, so an unstated
    workspace costs a cross-session read a refusal.
    """
    return str(getattr(slot, "workspace", "") or "")


def _crew_log_class(state: Any, slot: Any) -> tuple[str, str, bool]:
    """The ``(memory, app, channel)`` a session's ``session/opened`` records.

    The facts a reader needs to decide whether one session may read this one's
    crew log, taken from the live slot at the moment the log is opened -- which is
    the only moment they can be taken, because the reader that asks is usually
    asking about a session that has since closed.

    ``channel`` is true when this session's conversation is published to a
    messaging channel, by a channel-born link or by an outbound mirror recorded in
    the session store. A cron tab's link is not one: it names the job's own run and
    republishes to nobody, which is the exemption ``CRON_LINK_PREFIX`` carries
    wherever that boundary is enforced. The mirror probe is asked to fail CLOSED,
    so a store that cannot answer records a channel rather than silently recording
    a session as unpublished.

    A slot whose memory mode cannot be read yields an empty ``memory``, and the
    emitter then records no class at all rather than a partial one -- an absent
    record is refusable, a half-filled one reads as complete.
    """
    # circular import: session_control imports this package's modules at module level.
    from kiro_crew.dashboard.session_control import CRON_LINK_PREFIX, _has_channel_mirror

    link = str(getattr(slot, "linked_session_key", "") or "")
    channel = bool(link) and not link.startswith(CRON_LINK_PREFIX)
    if not channel:
        try:
            channel = _has_channel_mirror(state, slot)
        except Exception:
            # The probe already fails closed on a store it cannot read; this covers
            # the narrower case of a state double that has no store at all, and it
            # fails the same way. A log is being opened, not a boundary crossed, so
            # this must not raise -- and recording "published" on a state nobody can
            # read costs a dispatcher one refusal it can ask about.
            logger.debug("channel mirror probe failed; recording a channel", exc_info=True)
            channel = True
    if not channel and bool(getattr(slot, PENDING_CHANNEL_ATTR, False)):
        # A binding that committed while this slot had NO OPEN LOG could not be recorded
        # when it happened -- there was nothing to append to -- so the recorder left this
        # mark rather than dropping the fact. Reading it here is what makes a link that
        # was bound AND removed before the log existed still count: the turn that link
        # routed is in this log's content, so a class read from the live slot alone would
        # state never-published about a log holding channel-authored words. The mark is
        # never cleared, which matches the fold: each member is held at the most
        # restrictive value the log ever recorded, so this can only add a restriction.
        channel = True
    return (
        str(getattr(slot, "memory_mode", "") or ""),
        str(getattr(slot, "_app", "") or ""),
        channel,
    )
