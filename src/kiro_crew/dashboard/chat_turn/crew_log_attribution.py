"""What a dashboard session's crew log records about it: the predecessor store, the served
model, lineage, the seeded tree, the inherited parent, the workspace and the memory/app/channel
class, and the close of a turn's crew log and its streak's activity when the turn ends
(``_turn_close_crew_log``)."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from kiro_crew.dashboard.state import CrewLogPrevious
from kiro_crew.llm_helpers import _billing_stats

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        crew_log_emit,
        logger,
    )
    from kiro_crew.dashboard.chat_turn.decisions_hooks import _jev_route_turn_ok, _settle_jev_route
    from kiro_crew.dashboard.chat_turn.turn_locals import _ChatTurn


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


def _crew_log_tree_depth(slot: Any, parent_slot: str) -> "int | None":
    """The ``depth`` this session's ``session/opened`` may state, or ``None``. No I/O.

    How many creators stand above this session: ``0`` when nobody created it, and the
    creator's own depth plus one when one did. *parent_slot* is the creator this entry
    is about to cite, already resolved by the two functions above, so this reads the
    SAME edge the entry records rather than a second opinion on it.

    ``None`` is "not established", and it is the answer in three states that look alike
    from the entry and are not. A cited creator whose depth the tree cannot give --
    unseeded, incomplete, a citation with no log, a cycle (see
    :func:`~kiro_crew.crew_log.session_tree_projection.slot_depth`) -- is unknown. So
    is a slot that came back from a restart STATING a creator this process may not
    cite: the creator exists, the entry carries no ``parent`` for the fence's sake
    (see :func:`_crew_log_lineage`), and the honest depth is therefore unknown rather
    than zero. And so is a slot that states no creator while the tree itself cannot be
    read, which is the third: a root's zero needs no walk, but writing it against a
    tree too incomplete to place any child would leave the distribution holding roots
    and nothing else (see
    :func:`~kiro_crew.crew_log.session_tree_projection.root_depth`). A slot is a root
    here only when it states no creator AND the tree can say so.

    That distinction is the whole reason the caller resolves this instead of the
    emitter inferring it from an empty ``parent_slot``: the two cases are one value
    there, and the reader this feeds counts a tree per root.

    Never raises. A depth that cannot be read costs one session its place in a
    distribution, and nothing else.
    """
    try:
        if not crew_log_emit.enabled():
            return None
        from kiro_crew.crew_log.session_tree_projection import root_depth, slot_depth

        if parent_slot:
            above = slot_depth(parent_slot)
            return None if above is None else above + 1
        if str(getattr(slot, "_created_by", "") or ""):
            # A creator was restored and could not be promoted to a citation. Unknown,
            # not a root: this session hangs under something, and the one number that
            # must not be written here is the one that says it does not.
            return None
        # A root -- and it still asks the tree, through the SAME readable-tree check a
        # child's walk goes through. A zero written while an unreadable tree withholds
        # every child's depth is the one way this field can bias a distribution on its
        # own, so the two answers go absent together.
        return root_depth()
    except Exception:
        return None


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


def _take_engine_profile(turn: _ChatTurn) -> "dict[str, Any] | None":
    """This turn's engine measurement (``AcpPromptStats.kas_profile``), taken once.

    ``None`` when the per-turn stats object is still the one the turn started with
    (``_turn_stats0``): the runtime installs a fresh one only when the prompt is sent,
    so an unchanged object means this turn never sent one and anything on it belongs
    to an earlier turn. Cleared after reading, so no later reader takes it again.
    """
    stats = _billing_stats(turn.client)
    if stats is None or stats is turn._turn_stats0:
        return None
    profile = getattr(stats, "kas_profile", None)
    if not isinstance(profile, dict) or not profile:
        return None
    stats.kas_profile = None
    return profile


async def _turn_close_crew_log(turn: _ChatTurn) -> None:
    """Close the turn's crew log, in the one order a reader can trust, and the streak's activity."""
    _crew_log_terminal: "dict | None"
    _crew_log_sid = turn._crew_log_sid
    _crew_log_step = turn._crew_log_step
    _crew_log_step_t0 = turn._crew_log_step_t0
    _crew_log_turn_no = turn._crew_log_turn_no
    _crew_log_turn_open = turn._crew_log_turn_open
    _escalation = turn._escalation
    _prompt_depth = turn._prompt_depth
    _turn_t0 = turn._turn_t0
    _turn_thought = turn._turn_thought
    session_key = turn.session_key
    # The turn's crew log closers, in the one order a reader can trust: every
    # `message/sent` for this turn has now been flushed, so the tool closer,
    # the last step's completion and the turn's own completion land after the
    # text they close over. Emitted HERE rather than at the terminal event
    # because several recovery paths return before that event is reached, and
    # a stash that never runs leaves a turn open in the file forever.
    # Derived-tier escalation hears how a routed turn ended. A no-op for a
    # session with no routed turn in flight, which is every session while
    # ``decisions.model_route_derive`` is off.
    _settle_jev_route(session_key, _jev_route_turn_ok(turn._crew_log_terminal))
    if turn._crew_log_terminal is not None or _crew_log_turn_open:
        # Ahead of the turn's closers, so the ``usage`` fold finds this turn's
        # composition row still at the tail and not yet sealed.
        crew_log_emit.on_context_measured(
            _crew_log_sid, _crew_log_turn_no, profile=_take_engine_profile(turn)
        )
    if turn._crew_log_terminal is not None:
        # Any tool still open produced no result frame of its own. Closed
        # before the turn completes so a turn never completes with an open
        # call inside it -- the shape the interrupted-turn repair exists to
        # fix, which a live turn must not produce in the first place.
        crew_log_emit.close_open_tool_calls(_crew_log_sid, _crew_log_turn_no)
        # The turn's LAST model call has no tool group after it to close it,
        # so the turn's end does.
        crew_log_emit.on_step_completed(
            _crew_log_sid,
            _crew_log_turn_no,
            _crew_log_step,
            ms=int((time.monotonic() - _crew_log_step_t0) * 1000),
        )
        crew_log_emit.on_turn_completed(_crew_log_sid, _crew_log_turn_no, **turn._crew_log_terminal)
        _crew_log_terminal = None
        turn._crew_log_terminal = _crew_log_terminal
        _crew_log_turn_open = False
        turn._crew_log_turn_open = _crew_log_turn_open
    elif _crew_log_turn_open:
        # The turn RAN and ended without its terminal event: the stream raised,
        # or a recovery path (stale turn, tool stall, pipe death) returned
        # before it. Closed here rather than left open, because this process
        # OBSERVED the end -- and an open `turn/started` says the opposite, that
        # the writer died mid-turn. Nothing in this process would ever correct
        # it: the interrupted-turn repair is opt-in and only a resume asks for
        # it, so a later resume would close it as an interruption that never
        # happened, stamped at the last real entry's time.
        #
        # Same closer order as the measured path, for the same reason. Only
        # locals bound BEFORE the stream are read: `_provider_name` and
        # `_record_model` are bound only inside `_on_complete`, the terminal-event arm, so
        # naming them here is a global lookup that raises NameError inside a `finally`
        # and masks the very exception that brought us here.
        crew_log_emit.close_open_tool_calls(_crew_log_sid, _crew_log_turn_no)
        crew_log_emit.on_step_completed(
            _crew_log_sid,
            _crew_log_turn_no,
            _crew_log_step,
            ms=int((time.monotonic() - _crew_log_step_t0) * 1000),
        )
        crew_log_emit.on_turn_failed(
            _crew_log_sid,
            _crew_log_turn_no,
            error=turn._crew_log_error,
            duration_ms=int((time.monotonic() - _turn_t0) * 1000),
            model=turn._turn_model,
            provider=turn.provider_name,
            depth=_prompt_depth,
        )
        _crew_log_turn_open = False
        turn._crew_log_turn_open = _crew_log_turn_open
    # Poisoned-conversation streak break — run from _run_chat's FINALLY on purpose (fork
    # GPT review): several recovery paths (stale-turn, tool-stall,
    # pipe-death) `return` before the main completion block in _turn_persist_and_settle, and a turn
    # with model activity that exits through them must STILL break the
    # exhaustion streak — the backend demonstrably served this
    # conversation. Safe against _on_acp_error_card's streak
    # increment: incrementing requires ZERO activity, so the two are
    # mutually exclusive by construction and this can never clobber a
    # legitimate increment. One-shot re-arm stays landed-turn-only.
    _escalation.record_activity(emitted=turn._turn_emitted, thought=_turn_thought)
