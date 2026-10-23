"""The decisions hooks a dashboard turn runs: the stale-decision drop, the strip hand-off, the
tool-card risk annotation, and the Jev model routing that may pick the turn's model."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Callable

from kiro_crew.llm_helpers import (
    ROUTE_BASELINE_ATTR,
    TURN_FALLBACK_ATTR,
    latch_route_baseline,
    pick_epoch_host,
    provider_advertised_ids,
    provider_fallback_active,
    resolve_substitute_set_model,
    route_baseline_latch,
)

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        DashboardState,
        LLMEvent,
        _ChatSlot,
        effective_session_key,
        logger,
        slot_switch_session_lock,
    )
    from kiro_crew.dashboard.chat_turn.crew_log_attribution import _crew_log_model
    from kiro_crew.dashboard.chat_turn.model_fallback import _sync_served_model
    from kiro_crew.dashboard.chat_turn.tool_approval import _auto_approve_reason, _slot_is_trusted
    from kiro_crew.dashboard.chat_turn.turn_stats import _turn_outcome


def _discard_stale_decision(slot: _ChatSlot) -> None:
    """Drop an outcome left pending by an earlier turn of this session. Never raises.

    The claim side (:func:`_decisions_strip_meta`) keys on the session, so without
    this a turn that published and then died before any assistant row -- an
    interrupt, a provider failure before the first token -- would leave its
    outcome for the NEXT reply to wear. The registry's TTL does not cover it: a
    later turn that reaches no decision publishes nothing to replace the entry.

    Imported inside the function for the same reason the claim side is: an
    ordinary turn with the seam off must not pull the decisions package onto the
    turn path, and this costs one dict lookup when there is nothing to drop.
    """
    try:
        from kiro_crew.decisions.outcomes import discard

        if discard(effective_session_key(slot)):
            logger.debug("decisions: dropped an outcome left by an earlier turn")
    except Exception:  # pragma: no cover - an observation may not cost a turn
        logger.debug("decisions: could not drop a stale outcome", exc_info=True)


def _decisions_strip_meta(slot: _ChatSlot) -> dict | None:
    """This session's pending decision outcomes as row ``meta``, or ``None``.

    The decisions that shaped this reply were made during the turn -- prompt
    assembly for ``skills.select``, just before the prompt is sent for
    ``model.route`` -- before any message existed to carry them
    (:mod:`kiro_crew.decisions.outcomes`). This is the other end of that hand-off,
    and it is read at the moment the assistant row is APPENDED rather than after:
    ``slot.append`` broadcasts the live ``chat_message`` frame from inside the
    call, so a field written onto the row afterwards would persist but be missing
    from the frame the open tab renders -- one door out of two.

    A LIST, always, even for the one-decision turn that is the common case. Two
    points can decide the same reply, so a shape that held one would have to drop
    the second or change shape between turns -- and a reader that must branch on
    "object or array" is a reader two producers can disagree about. The frontend
    still accepts a bare object, so a row written by an earlier release reads
    unchanged.

    Carried under ``meta`` rather than as a top-level key because ``meta`` is the
    part of a row that already travels every door: ``_build_message_entry_uncached``
    persists it, the restore path reads it back, ``chat_message_frame`` puts it on
    the WS push, and ``append``'s own ``mid`` minting merges into it rather than
    replacing it. A new top-level key would be dropped by all of them.

    ``None`` for a session with no decision -- which is every session while the
    seam is off, and every unsampled session while it is on -- and ``append``
    already writes no ``meta`` for ``None``, so the whole ride-along costs one
    dict lookup on the reply path of an ordinary turn. Nothing here raises:
    ``consume`` does not, by contract, because this runs where an exception would
    cost the user their reply.
    """
    from kiro_crew.decisions.outcomes import consume

    strips = consume(effective_session_key(slot))
    return {"decisions_strip": strips} if strips else None


def _route_history_source(state: DashboardState, session_key: str) -> "Callable[[], list[dict]]":
    """A callable serving this session's recent user/assistant rows, or nothing.

    A CALLABLE and not a list, for the reason ``skills.select`` passes one: the
    point invokes it only after the history budget is known to be above 0, so at
    the shipped default of 0 no transcript is read at all.

    Roles are restricted at the READ, so tool output is never projected rather
    than filtered afterwards.
    """
    from kiro_crew.decisions.points import HISTORY_ROLES, MAX_HISTORY_MESSAGES

    log = getattr(state, "conversation_log", None)
    if log is None or not session_key:
        return lambda: []

    def _rows() -> list[dict]:
        return log.recent(
            session_key,
            max_messages=MAX_HISTORY_MESSAGES,
            roles=HISTORY_ROLES,
        )

    return _rows


#: The one spelling of "the owner chose to name no model": the picker's Auto row.
#: Compared against a stripped, lower-cased model so a hand-written ``"Auto"``
#: reads the same as the picker's own value.
#:
#: ``""`` is deliberately NOT here. It is the opposite fact -- a slot nobody has
#: picked for yet -- and routing is an OWNER action, so the absence of a choice
#: does not arm it. Admitting ``""`` was also uneven in practice: on a seam whose
#: first session backfills a resolved model into ``slot.model``
#: (``_backfill_canonical_model``) before this gate is read, the slot was already
#: pinned and nothing routed, while on a seam that backfills ``""`` (a Bedrock
#: profile-form id, or a model not yet reported) the same ``""`` slot DID route --
#: one state, two behaviours, and a composer chip that could not tell which. This
#: drops that arm on purpose; ``auto`` (the owner's inherit pick) is the one way in.
_JEV_ROUTE_AUTO_MODELS = ("auto",)


def _jev_route_armed(slot: Any) -> bool:
    """Whether this slot's turns ask ``model.route`` which model to run on.

    Two ways in, and both are the OWNER saying "do not pin this session, let Jev
    name a model per turn":

    * the owner picked the ``Auto (Jev)`` row, which survives as ``slot.jev_route``;
    * the slot's model is ``auto`` -- the picker's inherit row, which the sentinel
      pick also resolves to -- while the Jev preview is on, which is the state the
      composer chip renders as ``Auto (Jev)``.

    A slot that names NO model (``""``) is not armed. That value is not a choice,
    it is the absence of one, and the runtime replaces it with the resolved model
    before this gate is ever read (see :data:`_JEV_ROUTE_AUTO_MODELS`).

    In-memory only, so the turn gate can read it on the event loop and the switch
    can re-read it inside the model locks. Whether the PREVIEW is on is a keystone
    read and is deliberately NOT asked here: :func:`_route_model_for_turn` asks it
    once, off the loop, which is also what keeps the shipped default -- consent
    off, every slot on ``auto`` -- from paying a filesystem read on the loop for
    every turn of every session.
    """
    if getattr(slot, "jev_route", False):
        return True
    return str(getattr(slot, "model", "") or "").strip().lower() in _JEV_ROUTE_AUTO_MODELS


def _jev_preview_on(session_key: str) -> bool:
    """Whether the Jev preview's two switches are both on for *session_key*. Blocking.

    The OWNER's keystone and the FLEET's ``capabilities.decisions`` ceiling, plus the
    sampling bucket -- i.e. every refusal ``decide`` re-runs on its first line, asked
    through the helper the gate exposes for exactly this ("a hook whose state is
    expensive to build"). It grants nothing: ``decide`` checks all of it again, so a
    config change racing this read costs one wasted question and never a turn.

    Filesystem IO, so every caller runs it off the event loop. It exists to keep the
    implicit arm cheap on the overwhelmingly common install: consent off, every slot
    on ``auto``, and one small keystone read per turn instead of the history budget,
    tier map and provider round trip below it.
    """
    try:
        from kiro_crew.decisions import gate as _gate
        from kiro_crew.decisions.points import model_route as _model_route

        return _gate.is_enabled(_model_route.POINT, session_key=session_key)
    except Exception:  # pragma: no cover - a build without the point
        logger.debug("model.route: preview probe failed; not routing", exc_info=True)
        return False


_JEV_ROUTE_BASELINE_ATTR = ROUTE_BASELINE_ATTR


#: Stop reasons that say the MODEL ran out of room or steps before it finished --
#: the length limit and the step limit. The only endings derived-tier escalation
#: reads as evidence a turn's tier was too low.
_JEV_ROUTE_CAPABILITY_STOPS = frozenset({"max_tokens", "max_turn_requests"})


def _jev_route_turn_ok(terminal: "dict[str, Any] | None") -> bool | None:
    """How a turn ended, for derived-tier escalation: ok, too hard, or neither.

    ``True`` for a turn whose terminal event reads ``ok``. ``False`` only when the
    model itself stopped at its length or step limit
    (:data:`_JEV_ROUTE_CAPABILITY_STOPS`). ``None`` for everything else: a turn
    with no terminal event (a cancel, a dead process, a transport error), an
    error or timeout the provider reported, a refusal, a recovered stall. None of
    those says a dearer model would have done better, so none may raise the
    session's tier.
    """
    if terminal is None:
        return None
    reason = str(terminal.get("stop_reason") or "")
    if _turn_outcome(reason) == "ok":
        return True
    return False if reason in _JEV_ROUTE_CAPABILITY_STOPS else None


def _settle_jev_route(session_key: str, ok: bool | None) -> None:
    """Hand a turn's ending to ``model_route.settle``. Never raises; in-memory only."""
    try:
        from kiro_crew.decisions.points import model_route

        model_route.settle(session_key, ok)
    except Exception:  # pragma: no cover - a build without the point
        logger.debug("model.route: could not settle the routed turn", exc_info=True)


def _active_fallback(slot: Any, client: Any) -> tuple[bool, str]:
    """``(a substitution serves this session, the model it substituted for)``.

    Refusal record first -- it already folds a throttle primary in, so it names the
    owner's model even under both -- then the SHARED throttle marker, which is the
    only half a sibling alias can see, then that alias's own sticky copy.
    """
    refusal_primary = str(getattr(slot, "_refusal_fallback_primary", "") or "")
    if refusal_primary:
        return True, refusal_primary
    if provider_fallback_active(client):
        marker = getattr(client, TURN_FALLBACK_ATTR, None)
        primary = str((marker or ("",))[0] or "")
        return True, primary or str(getattr(slot, "_fallback_primary_model", "") or "")
    if getattr(slot, "_active_fallback_model", ""):
        return True, str(getattr(slot, "_fallback_primary_model", "") or "")
    return False, ""


def _jev_route_baseline(slot: Any, client: Any) -> tuple[str, str, int]:
    """``(live model, pre-route baseline, pick epoch)`` for this turn's routing.

    The BASELINE is the model the session runs with routing off, latched on the
    epoch HOST (the wire session, which several slot aliases share) because the live
    value is the previous turn's tier from the second routed turn on.

    The record is ``(session id, model, pick epoch)`` and one equality invalidates
    it: a moved epoch is an explicit pick (read with the restore probe's own default
    of ``0``, so a first pick counts), a different id is a pooled host serving a new
    session, no record is the first turn. While a substitution serves the session
    the latch takes what it substituted FOR (:func:`_active_fallback`), and takes
    nothing at all when that is unnameable. ``""`` is a real latch: the backend
    named no model, so there is nothing to go back to.
    """
    host = pick_epoch_host(client)
    epoch = getattr(host, "_explicit_pick_epoch", 0)
    epoch = epoch if isinstance(epoch, int) else 0
    _sync_served_model(slot, client)
    live_model = _crew_log_model(slot)
    # A degraded model is not what this session runs when nothing has moved it.
    _fallback_on, _fallback_primary = _active_fallback(slot, client)
    latched = route_baseline_latch(host)
    if latched is not None:
        return live_model, latched, epoch
    baseline = _fallback_primary or live_model
    if _fallback_on and not _fallback_primary:
        # A fallback serves the session and nothing names what it left, so there is
        # no record to take: latching the substitute is the defect this branch
        # exists to avoid, and the next turn latches once the ladder is done.
        return live_model, baseline, epoch
    latch_route_baseline(host, baseline)
    return live_model, baseline, epoch


def _jev_route_premise_moved(slot: Any, client: Any, *, live_model: str, epoch: int) -> bool:
    """Whether a pick has moved the premise this turn's routed answer was computed on.

    Three signals, and any one of them drops the answer: the flag a manual pick
    clears, the model the turn STARTED on, and the shared pick epoch a pick through
    ANOTHER alias moves while leaving this slot's own cache untouched. The model is
    the live value rather than the receipt's pre-route baseline -- those differ from
    the second routed turn on, and comparing the baseline would drop every turn after
    the first as a phantom re-pick.

    ONE predicate, because every exit that ACTS on the answer asks the same question,
    and recording that the answer was refused is such an exit: those rows are durable
    and never rewritten, so one written for an answer a pick has already superseded
    describes a decision nothing acted on and nothing can clear.
    """
    epoch_now = getattr(pick_epoch_host(client), "_explicit_pick_epoch", 0)
    return (
        not _jev_route_armed(slot)
        or _crew_log_model(slot) != live_model
        or (isinstance(epoch_now, int) and epoch_now != epoch)
    )


def _jev_current_window(client: Any, model_route: Any, live_model: str) -> int | None:
    """The window the session is SERVED on, else the registry's reading of *live_model*.

    ONE reading, for both window rules AND for the landing a refused tier is sent
    to: the served value is the denominator the meter's own percentage is taken
    against, so a registry entry lagging it hides a shrink the meter can already
    see -- and a landing sized by the other reading can be handed exactly the window
    the rules just refused.
    """
    try:
        served = int(client.context_window_tokens() or 0)
    except Exception:
        # A provider with no window accessor is the registry case, not a raised turn.
        logger.debug("model.route: no served window reading", exc_info=True)
        served = 0
    return served or model_route.known_window(live_model)


def _jev_downgrade_refused(
    state: DashboardState,
    client: Any,
    session_key: str,
    model_route: Any,
    *,
    current_window: int | None,
    target: str,
    p: Any,
    prompt: str,
    rollback_target: str,
    target_window: int | None = None,
) -> bool:
    """Whether routing must NOT put this turn on *target*'s smaller context window.

    Two rules, each only for a move to a SMALLER window, because the harm is
    one-directional: a window at least as large changes nothing about what fits,
    and an unknown one on either side is not evidence of a shrink.

    The ROLLBACK rule comes first because it is a question about this seam rather
    than about the turn: a shrink is only ever applied because it can be taken back
    if what actually serves turns out smaller still, and the model to go back to is
    the one the session was on. A session the backend never named a model for reads
    that as ``""`` -- there is no id to ask for -- so a smaller window is refused on
    the same grounds an unknown one is: nothing here confirms the turn could be put
    back. A window at least as large needs no way back and is unaffected.

    The CONFIDENCE rule is the point's own floor. The FIT rule asks what the
    session's context reading becomes once the window is the smaller one: the meter
    is a percentage of the SERVED window, so the same transcript that sits well
    inside a large one can land at or above this session's compaction threshold in a
    small one, and that turn ends by handing the backend a history it replaces with
    a summary no later switch back recovers.

    The FIT rule PERMITS only on a reading that confirms the target holds this turn.
    Anything unmeasurable refuses instead: a meter the provider itself calls unknown
    (the post-compaction and resumed state, where the history is real and unread), a
    meter that cannot be reached, and a threshold that cannot be read. Refusing there
    costs the turn one gear and nothing else, because a window that does not shrink
    never compacts -- and it is not a veto on ROUTING, only on shrinking this turn:
    the next turn asks again with a meter that has since reported. A reading of 0 the
    provider vouches for is a KNOWN small history, not an unmeasurable one, so a
    fresh session downgrades as it always did.

    *current_window* is the caller's single reading of the window the session is on
    (:func:`_jev_current_window`), passed rather than taken here so the landing a
    refusal is sent to is sized by the same number these rules refused on.

    *target_window* overrides the registry lookup of *target*. The post-switch caller
    passes the window the session is NOW SERVED on, because that is the only reading a
    substituted model appears in: its id can still read as the one that was asked for
    while the meter has already been rebased to whatever is actually serving.

    *prompt* is the ASSEMBLED text this turn sends -- the person's words under
    whatever request prefix, replayed history and hook context the turn carries -- and
    not the excerpt the tier was classified from. The meter answers for the history
    the session already holds, and the turn being decided for adds all of this on top,
    so the fit reading is the pair rather than the meter alone.

    *model_route* is the point module the caller already holds, passed rather than
    imported here: the whole ``decisions`` package is reached lazily from this turn
    path, and a second import statement would be a second place that decision holds.
    """
    if target_window is None:
        target_window = model_route.known_window(target)
    if not model_route.permits_smaller_window(p, current=current_window, target=target_window):
        return True
    if not model_route.is_smaller_window(current=current_window, target=target_window):
        return False
    if not rollback_target:
        return True
    try:
        unknown = bool(client.context_usage_unknown())
        used = int(client.context_used_tokens() or 0)
        limit = float(state.sessions.effective_autocompact_pct(session_key))
    except Exception:
        # Nothing here CONFIRMS the target holds this turn, and a window that does not
        # shrink never compacts, so the unreadable answer is the refusing one.
        logger.debug("model.route: no context reading for the fit rule", exc_info=True)
        return True
    if unknown:
        # The provider's own word that its 0 means "not measured" rather than "empty".
        return True
    # A vouched-for 0 is a KNOWN small history, and the prompt is in hand either way,
    # so it is still weighed: one that alone crosses the threshold in the smaller
    # window compacts that turn, and skipping it would exempt the largest prompts.
    return (max(used, 0) + model_route.prompt_tokens(prompt)) / target_window * 100.0 >= limit


async def _route_model_for_turn(
    state: DashboardState,
    slot: _ChatSlot,
    client: Any,
    message: str,
    session_key: str,
    *,
    prompt: str,
    retried: bool = False,
) -> None:
    """Ask ``model.route`` which model this turn should run on, and switch to it.

    Called for a NORMAL dashboard chat turn of a slot whose owner picked
    ``Auto (Jev)`` in the model picker, after the fallback restore probe and before
    the prompt is sent. Every refusal -- the seam off, the session unsampled, an
    answer outside the three tiers, a pinned id this account cannot run, a
    timeout, a provider failure, no ``set_model`` seam -- leaves the session on
    the model it was already on, which is exactly what an unconsented install
    does. Nothing here raises except ``CancelledError``.

    An UNPINNED tier is not a refusal and is the shipped state: every tier of
    ``decisions.model_route`` is ``""`` until an owner pins one, because no model id
    may be hardcoded as a default. The answer is then recorded and published --
    the strip reads "complex -> (unpinned)" -- and the tier applies nothing.

    What it does NOT do is leave the session wherever an earlier turn's tier put
    it, nor wherever a model substitution left it -- a throttle or refusal fallback
    is undone by its own restore, so :func:`_active_fallback` decides what the
    baseline names and whether a restore is attempted at all. A tier map is PARTIALLY pinned on the way to a full one -- every install
    starts with all three unpinned -- so leaving the model alone lets the first
    answered pinned tier capture the session for the rest of its life: a one-line
    rename answered ``simple`` moves it to the cheap pin, and a redesign answered
    ``complex`` is unpinned, so the owner's own model would never be reached again.
    So an unpinned tier switches BACK to the model the session ran on before
    routing first moved it (:func:`_jev_route_baseline`), which is what this turn
    would have run on with routing off. The receipt still says the tier applied
    nothing, because it did: ``model_chosen`` stays ``UNPINNED`` and the restore is
    not Jev's pick. ``applied`` and ``model_used`` describe the switch the turn
    ASKED FOR, so on such a row they describe the restore -- a restore that does not
    take leaves the turn on the previous tier's model, and a row that omitted them
    would be read as a turn that needed no switch.

    The BASELINE recorded on the row is that same pre-route model, for every turn
    of the session rather than only the first, and for every slot alias driving that
    session rather than each alias for itself: "what would this turn have used" is a
    question about the owner's configuration, and answering it with the previous
    turn's tier puts the cheapest model on the dearest turn's receipt as its own
    default. It is what a refusal keeps.

    The switch is taken under the SAME two locks the fallback swap and the restore
    probe hold (session lock, then pick lock, in that order), because it is the
    same kind of model transaction: without them a pick arriving through another
    alias of this session could land inside the ``set_model`` await and then be
    silently overwritten. The ``decide`` call is deliberately OUTSIDE them: it is
    a network round trip, and holding a session-scoped lock across it would stall
    every sibling alias for the provider budget.
    """
    try:
        from kiro_crew.decisions.points import model_route
    except Exception:  # pragma: no cover - a build without the point
        return
    if not getattr(slot, "jev_route", False):
        # The IMPLICIT arm: the owner never picked the sentinel, so the preview's
        # own two switches are what authorises spending on a routed turn. Asked
        # here rather than in the turn gate because it reads the keystone, and
        # asked BEFORE the work below because the shipped default is consent off
        # with every slot on ``auto`` -- which must keep costing one small read
        # rather than a history budget, a tier map and a provider round trip.
        # Not asked for the explicit arm: that request already proved owner
        # identity at the picker, and ``decide`` re-checks both switches anyway.
        if not await asyncio.to_thread(_jev_preview_on, session_key):
            return
    # Read BEFORE the round trip and kept apart: ``_live_model`` is the session's
    # model right now, which is what the re-pick guard below compares against, and
    # ``_baseline`` is the model it ran on before routing, which is what the receipt
    # records and what an unpinned tier goes back to. They are the same value on the
    # first routed turn of a session and diverge from the second one on. Both come
    # from one call because a sibling alias driving the same session makes this
    # slot's own cache the older fact, and then neither value may be read from it.
    # ``_epoch`` is that call's pick-epoch reading, re-read inside the locks below.
    try:
        _live_model, _baseline, _epoch = _jev_route_baseline(slot, client)
        # Read once: the same list answers what the tier may route to and where a
        # refused downgrade may land.
        _advertised = provider_advertised_ids(client)
        routed = await model_route.routed_model(
            message,
            session_key=session_key,
            current_model=_baseline,
            advertised=_advertised,
            history_source=_route_history_source(state, session_key),
            retried=retried,
            rate_of=model_route.rate_lookup(client),
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # pragma: no cover - the point guards itself
        logger.debug("model.route: keeping the session's own model", exc_info=True)
        return
    if not routed:
        return
    _chosen = str(routed.get("model_chosen") or "")
    # The model this turn switches to: the tier's pin, or -- for an unpinned tier --
    # the pre-route baseline the session has since drifted off. ``""`` is nothing to
    # do: the tier is unpinned and the session is already on its own baseline, or
    # the backend never named that baseline so there is no id to ask for. The
    # decision is still the owner's evidence for what to pin, so it is recorded and
    # published on the same terms as an applied one either way.
    _restore_to = _baseline if _baseline and _baseline != _live_model else ""
    if _active_fallback(slot, client)[0]:
        # The model-fallback ladder holds this session, and the baseline is the
        # primary it walked away from: switching onto it here would put the turn
        # back on the model that had just failed. Putting the primary back is the
        # restore probe's decision, taken when it has evidence the primary serves
        # again. A PIN still applies -- an answered tier is the owner's instruction
        # either way, which is what a routed turn over an active fallback already
        # does.
        _restore_to = ""
    _target = _chosen or _restore_to
    if not _target:
        # Nothing to switch: the tier keeps the model the turn already runs on,
        # the one exit beside a taken switch where the answer shapes the turn.
        model_route.note_applied(session_key, routed)
        await asyncio.to_thread(model_route.record_outcome, session_key, routed)
        return

    async def _record_refused() -> None:
        """The refusal's own row: the suppressed tier and the probability it came
        with, in the shape every other dropped tier takes. Written at each exit that
        DISCARDS the tier rather than once up front, because the locked re-pick guard
        below drops the whole answer, and a row already appended for it would outlive
        a decision nothing acted on. Off the loop, and never published -- the answer a
        receipt would show is the one that was refused."""
        await asyncio.to_thread(
            model_route.record_error,
            session_key,
            turn_id=str(routed.get("turn_id") or ""),
            tier=str(routed.get("tier") or ""),
            latency_ms=int(routed.get("latency_ms") or 0),
            error=model_route.ERROR_WINDOW_REFUSED,
            p=routed.get("p"),
        )

    # The window rules apply to a TIER's model only. An unpinned tier's restore goes
    # to the model the session ran on with routing off, which is the owner's own
    # choice rather than a shrink this seam chose.
    _current_window = _jev_current_window(client, model_route, _live_model)
    _refused = False
    if bool(_chosen) and _jev_downgrade_refused(
        state,
        client,
        session_key,
        model_route,
        current_window=_current_window,
        target=_target,
        p=routed.get("p"),
        prompt=prompt,
        rollback_target=_live_model,
    ):
        # A refused tier STAYS PUT. The turn keeps the window it already has, which
        # is a window these rules read and accepted, and nothing is asked of the
        # provider -- so there is no switch to undo, nothing to land on, and no turn
        # to stop. The row says which tier was suppressed and at what probability.
        # The premise is still asked first: the locks below are never reached, and a
        # durable row must not outlive an answer a pick has already superseded.
        if not _jev_route_premise_moved(slot, client, live_model=_live_model, epoch=_epoch):
            await _record_refused()
        return
    set_model_fn = resolve_substitute_set_model(client)
    if set_model_fn is None:
        if not _chosen:
            # Nothing was asked of the provider anyway: the tier applied nothing, and
            # a restore that cannot be expressed is the same no-op a refusal is. So
            # this is the unpinned outcome row above, not a finding about a dropped
            # tier -- there was no tier model to drop.
            await asyncio.to_thread(model_route.record_outcome, session_key, routed)
            return
        # No seam to switch through: the tier was answered and cannot be applied,
        # which is a finding rather than a quiet no-op -- the owner picked
        # Auto (Jev) and every turn would keep the same model with nothing said.
        await asyncio.to_thread(
            model_route.record_error,
            session_key,
            turn_id=str(routed.get("turn_id") or ""),
            tier=str(routed.get("tier") or ""),
            latency_ms=int(routed.get("latency_ms") or 0),
            error=model_route.ERROR_NO_SWITCH,
        )
        return
    _pick_lock = getattr(slot, "_model_pick_lock", None)
    if _pick_lock is None:  # pragma: no cover - minimal slot doubles
        _pick_lock = asyncio.Lock()
    try:
        async with slot_switch_session_lock(effective_session_key(slot)), _pick_lock:
            # The answer was computed against a premise that a pick landing during
            # the network round trip invalidates, and a pick made by hand is the
            # newer instruction. So both halves of the premise are re-read HERE,
            # inside the locks, where an in-flight pick has already completed: the
            # flag any manual pick clears, the model the turn STARTED on, and the
            # shared pick epoch. The model is the live value rather than the
            # receipt's pre-route baseline -- those differ from the second routed
            # turn on, and comparing the baseline would drop every turn after the
            # first as a phantom re-pick. The epoch is what a pick through ANOTHER
            # alias moves: it leaves this slot's own cache untouched, so without it
            # a cross-alias pick landing inside the await is overwritten here. Any
            # of the three having moved drops the answer -- re-asking would spend
            # again on a turn whose model the owner just chose.
            if _jev_route_premise_moved(slot, client, live_model=_live_model, epoch=_epoch):
                logger.debug(
                    "model.route: dropping the routed model for slot %s, the slot was "
                    "re-picked during the await",
                    slot.key,
                )
                return
            await set_model_fn(_target)
            _sync_served_model(slot, client)
            # A returning ``set_model`` is not proof of a switch: on a backend that
            # judges the model VALUE, the candidate ladder can be exhausted and the
            # call returns having stayed on the backend default -- no exception. The
            # row appended below is durable and never rewritten, so it is read here
            # rather than assumed, and it records the model the turn RAN on.
            _used = _crew_log_model(slot)
            # The rules authorised the window of the id that was ASKED for. A backend
            # answering a tier-policy substitution leaves the turn live on a different
            # model, and only the meter says so: it is rebased to what serves while the
            # id can still read as the one requested. So the FIT rule is asked once
            # more, against the served window, before any prompt is streamed.
            # A refused tier never reaches this point -- it stayed put -- so every
            # answer here is about a model the backend SUBSTITUTED for the one the
            # rules sized.
            if _chosen and _jev_downgrade_refused(
                state,
                client,
                session_key,
                model_route,
                current_window=_current_window,
                target=_used or _target,
                target_window=_jev_current_window(client, model_route, _used or _target),
                p=routed.get("p"),
                prompt=prompt,
                rollback_target=_live_model,
            ):
                logger.warning(
                    "model.route: the model serving slot %s after set_model(%r) has a "
                    "smaller window than the rules allowed; putting the turn back",
                    slot.key,
                    _target,
                )
                # Marked BEFORE the switch back, so a restore that raises is still
                # reported as the refusal it is rather than as a failed apply.
                _refused = True
                # The ROLLBACK rule made this expressible for a shrink the rules
                # authorised: the id to go back to is the one the session was on. A
                # substitution under an UPGRADE can still land here with nothing
                # named to go back to, and then the turn keeps the model it is on --
                # the same residual a switch that cannot be undone has always had.
                if _live_model:
                    await set_model_fn(_live_model)
                    _sync_served_model(slot, client)
            if not _refused:
                routed["model_used"] = _used
                # Two ways a switch counts. The session reports the id that was asked
                # for, or it reports something other than where the turn started -- the
                # ladder's own fallback spelling, which is the ask under a name this
                # build serves. Only staying put, on a model that was not the ask, is a
                # failed switch. Read against ``_target``, the model THIS turn asked
                # for, so the pair describes a restore on the same terms as a pin: a
                # restore that does not take leaves the turn on the previous tier's
                # model, and a reader takes an absent ``applied`` as applied, so a row
                # silent about it would report that turn as healthy. Not written for a
                # REFUSED tier: the row's error category is what describes that turn.
                routed["applied"] = _used == _target or _used != _live_model
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if _refused:
            # The switch TO the tier model landed; it is the switch BACK that raised.
            # So this is still the refusal the flag above marked, not a failed apply,
            # and its row is the refusal row with ``p``.
            logger.warning(
                "model.route: restoring %r failed for slot %s (%s); the turn stays on "
                "the model it switched to",
                _live_model,
                slot.key,
                type(exc).__name__,
            )
            await _record_refused()
            return
        logger.warning(
            "model.route: set_model(%r) failed for slot %s (%s); keeping the current model",
            _target,
            slot.key,
            type(exc).__name__,
        )
        if not _chosen:
            # A failed RESTORE, not a failed apply: the tier is unpinned, so an error
            # row would name a tier that was never switched to and the strip would
            # report a dropped answer. It is still a switch that did not take, and
            # the row says which model the turn ran on instead -- an absent
            # ``applied`` reads as applied, so staying silent here would publish a
            # healthy receipt for a turn left on the previous tier's model.
            routed["model_used"] = _crew_log_model(slot)
            routed["applied"] = False
            await asyncio.to_thread(model_route.record_outcome, session_key, routed)
            return
        await asyncio.to_thread(
            model_route.record_error,
            session_key,
            turn_id=str(routed.get("turn_id") or ""),
            tier=str(routed.get("tier") or ""),
            latency_ms=int(routed.get("latency_ms") or 0),
            error=model_route.ERROR_SWITCH_FAILED,
        )
        return
    if _refused:
        await _record_refused()
        return
    # Written and published only once the switch has landed, so the strip and the
    # log describe the model the turn actually ran on. Off the loop: the row is a
    # filesystem append.
    model_route.note_applied(session_key, routed)
    await asyncio.to_thread(model_route.record_outcome, session_key, routed)
    return


def _session_auto_approves(state: DashboardState, slot: _ChatSlot) -> bool:
    """Whether this session answers its own permission requests (trust or YOLO).

    Read through the SAME two helpers the permission branch decides by, so the
    annotation cannot claim a mode the approval path is not in. It asks a question
    and authorises nothing: no caller of this function approves, rejects or delays
    a tool call.
    """
    return _slot_is_trusted(slot) or state.is_yolo_active()


async def _tool_risk_meta(
    state: DashboardState,
    slot: _ChatSlot,
    event: "LLMEvent",
    *,
    session_key: str,
    message: str,
    calls_this_turn: int,
) -> dict | None:
    """One ``decisions_tool_risk`` record for this tool card, or ``None``.

    An ANNOTATION on the row the transcript already appends for this call. It reads
    the permission mode and changes no DECISION about it: it does not approve,
    reject or re-order a permission request, and it is called from the ``tool_call``
    branch, which contains no approval code at all. That is why "the approval is
    byte-identical with the point on and off" is a property a test asserts rather
    than a claim to trust (``test_decisions_tool_risk_card.py``).

    It does cost TIMING, and that is stated rather than implied: the ``tool_call``
    frame arrives BEFORE the permission request, so awaiting here delays when this
    turn's next event -- often that request -- is read, by up to the point's own
    wait budget. Bounded, sampled, and paid only by a session the seam is on for;
    the alternative was an await beside ``approve_tool``, where the same cost would
    buy a far weaker claim about the decision.

    Only for a session that AUTO-APPROVES. A session that prompts puts the call in
    front of the human, who is the annotation; adding a badge to a card they are
    already judging would sit a second opinion beside a control. A trusting
    session shows nobody anything, which is where a badge is the only signal.

    ``None`` on every other path -- the seam off, the session unsampled, the turn
    cap, a scrub, a timeout, a ``safe`` verdict -- so an ordinary tool row is
    exactly the row this build appends today. Nothing here raises: the call is
    already approved and an observation must not cost it.

    The decisions package is imported INSIDE the function for the reason every
    other caller does it: an ordinary turn with the seam off must not pull that
    graph onto the tool-call path.
    """
    try:
        if not _session_auto_approves(state, slot):
            return None
        from kiro_crew.decisions.points import tool_risk

        record = await tool_risk.risk_record(
            tool=event.tool_name or event.title or "",
            arguments=event.tool_input or "",
            message=message,
            policy=_auto_approve_reason(slot, state.is_yolo_active()),
            session_key=session_key,
            calls_this_turn=calls_this_turn,
            tool_kind=event.tool_kind or "",
            diff_path=event.diff_path or "",
        )
        return {"decisions_tool_risk": record} if record else None
    except Exception:  # pragma: no cover - an observation may not cost a call
        logger.debug("decisions: could not annotate the tool card", exc_info=True)
        return None
