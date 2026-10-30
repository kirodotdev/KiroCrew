"""A target's model and effort: ``session_set_model`` and the deferred model pick the
target's next turn applies.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from kiro_crew import model_registry
from kiro_crew.agent_sdk.backends import ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS
from kiro_crew.agent_sdk.capabilities import MODEL_NAMESPACE_ACP, capabilities_for
from kiro_crew.agent_sdk.provider_identity import is_claude_code
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.relay_archive import (
    RELAY_ARCHIVE_CODE,
    RELAY_ARCHIVE_ERROR,
    is_relay_archive,
)
from kiro_crew.dashboard.session_control_parts.caller_access import (
    SessionControlError,
    _audit,
    _audit_denials,
    _caller_is_ownership_fenced,
    caller_slot_key,
    prewarm_enabled_check,
)
from kiro_crew.dashboard.session_control_parts.target_authorization import authorize_target
from kiro_crew.effort import EFFORT_LEVELS, is_valid_effort
from kiro_crew.llm_helpers import slot_switch_session_lock
from kiro_crew.members import select_provider_backend
from kiro_crew.messaging.link import is_channel_session_key
from kiro_crew.security import redact
from kiro_crew.sel import sel

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

# The module's original logger name, which log filters and caplog assertions key on.
logger = logging.getLogger("kiro_crew.dashboard.session_control")


async def set_model_target(
    state: "DashboardState",
    *,
    caller_session_key: str,
    target: str,
    model: str | None = None,
    reasoning_effort: str | None = None,
    caller_fenced: bool | None = None,
) -> dict[str, Any]:
    """Record *model* and/or *reasoning_effort* as *target*'s pending pick.

    ``None`` for either means "keep what the target has"; at least one must be
    given. The pick is applied when the target's next turn starts.

    Nothing about the target changes now. The pick is committed by
    :func:`apply_pending_model_pick` at the start of the target's next turn,
    which re-runs this same gate and writes ``slot.model`` in one synchronous
    step. Committing here instead would need the live model switch, whose
    provider awaits sit after the last gate: a channel link or mirror landing
    in that window would let the change reach a session the caller may no
    longer touch.

    Only an IDLE session takes a pick. A target with a turn or attached
    sub-agents in flight is refused with ``target_busy``; a caller that wants
    to force it stops the target first (``session_stop``) and retries. A later
    pick replaces an earlier one that has not been applied yet.

    Two picks the picker allows are refused here. "Auto (Jev)" arms per-turn
    routing, which the model route keeps owner-only. A crew-bound (remote)
    target runs its turns on the peer, where this pick would never be applied.

    A reasoning effort must be one of the five canonical levels (``low``,
    ``medium``, ``high``, ``xhigh``, ``max``), which every backend accepts;
    any other level, including ``""``, is refused here and is set from the
    effort dropdown instead. It is committed with the model in the same step.
    It is NOT pushed live the way that route pushes it: the
    turn-start reset makes the cold start apply it through the same
    ``reasoning_effort_override`` the route's reset fallback relies on, with no
    provider await after the gate.

    ``caller_fenced`` has the meaning :func:`stop_target` documents.
    """
    # Deferred for the same import cycle `stop_target` documents.
    from kiro_crew.dashboard.chat_handlers import (
        _is_jev_route_pick,
        _model_rejected_reason,
        _normalize_model,
        _subagents_attached_response,
        _switch_target_busy,
    )
    from kiro_crew.dashboard.chat_runner import _JEV_ROUTE_AUTO_MODELS

    # Validated before any gate: a malformed pick needs no target lookup, and
    # refusing it first keeps a bad argument from reading as an access decision.
    if model is None and reasoning_effort is None:
        raise SessionControlError(
            "pass a model, a reasoning_effort, or both", code="bad_request", status=400
        )
    if reasoning_effort is not None:
        if not is_valid_effort(reasoning_effort):
            # Only the five standard levels: each folds onto every backend's
            # cold start, so a level checked here cannot go stale before the
            # target's next turn applies it. A level only one harness advertises
            # (Pi's ``minimal``, Claude's ``default``) is set from that session's
            # effort dropdown, which reads its live list.
            #
            # "" (back to the model default) is refused too: on kiro-cli
            # the default only takes once the workspace effort overlay
            # is cleared, which only the dropdown route's live clear_effort does.
            # Committed here, the cold start would recover the old level from
            # the overlay while every surface said "default".
            raise SessionControlError(
                f"reasoning_effort must be one of: {', '.join(EFFORT_LEVELS)}. "
                "Other levels, and returning a session to its model's default, are "
                "set from its effort dropdown.",
                code="effort_rejected",
                status=400,
            )
    model_name: str | None = None
    if model is not None:
        # Stripped once, so every check and the stored pick see the same spelling.
        model = model.strip()
        # "auto" is refused with the Jev sentinel: with the Jev preview on, a slot
        # on "auto" hands each turn's model choice to Jev routing, which only the
        # owner may arm. The picker's display label and any case of the sentinel
        # are refused the same way, so no spelling of the Jev entry is stored.
        # An empty name is the absence of a pick, not a model.
        folded = model.lower()
        if (
            _is_jev_route_pick(model)
            or folded in _JEV_ROUTE_AUTO_MODELS
            or folded in _JEV_ROUTE_SPELLINGS
        ):
            raise SessionControlError(
                "Auto and Auto (Jev) can only be picked by the owner from the model picker",
                code="model_owner_only",
                status=403,
            )
        if not model:
            raise SessionControlError("model is required", code="model_rejected", status=400)
        if redact(model) != model:
            # The pick is stored on the slot and broadcast to every dashboard, so a
            # credential-shaped argument is refused rather than persisted.
            raise SessionControlError(
                "model looks like it contains a credential; model not changed",
                code="model_rejected",
                status=400,
            )
        model_name = _normalize_model(model)
    loaded_cfg = None
    try:
        loaded_cfg = await asyncio.to_thread(KiroCrewConfig.load)
        agent_cfg = loaded_cfg.agent
        provider = agent_cfg.provider
        member_backend, default_backend = agent_cfg.member_acp_backend, agent_cfg.acp_backend
    except Exception:  # pragma: no cover - config load is resilient
        provider, member_backend, default_backend = "", "", ""

    # Same prewarm ordering and fence-verdict handling as `close_target`: the
    # verdict is stored with the pick so the turn-start re-check reads no config.
    try:
        await asyncio.to_thread(sel)
    except Exception:  # noqa: BLE001 - a prewarm failure must not fail the switch
        logger.warning("session-control SEL prewarm failed", exc_info=True)
    await prewarm_enabled_check()
    caller_key = caller_slot_key(state, caller_session_key)
    if caller_fenced is None:
        caller_fenced = bool(caller_key) and _caller_is_ownership_fenced(state, caller_key)

    slot = authorize_target(
        state,
        caller_session_key=caller_session_key,
        target=target,
        operation="set_model",
        precomputed_ownership_fenced=caller_fenced,
    )
    slot_key = slot.key
    # The model check and the alias correction both key on the TARGET's backend,
    # resolved through the same member-aware gate the provider factory uses: a
    # member DM routes to agent.member_acp_backend, which can differ from the
    # configured default, and a Claude backend takes canonical keys as wire ids.
    # A crewmate's own harness pin outranks both, and one that cannot start
    # refuses the pick (fail closed) rather than judging it for another harness.
    from kiro_crew.crewmate_harness import CrewmateHarnessUnavailable, crewmate_backend_pin

    target_key = effective_session_key(slot)
    try:
        backend = select_provider_backend(
            target_key,
            member_backend,
            default_backend,
            crew_backend=(
                crewmate_backend_pin(loaded_cfg, target_key) if loaded_cfg is not None else None
            ),
        )
    except CrewmateHarnessUnavailable as exc:
        raise SessionControlError(str(exc), code=exc.code, status=409) from None
    target_provider = (
        provider if is_claude_code(provider) else capabilities_for(backend).provider_seam
    )
    rejected = _model_rejected_reason(model_name, backend) if model_name is not None else ""
    if rejected:
        raise SessionControlError(rejected, code="model_rejected", status=400)
    if (
        model_name is not None
        and not is_claude_code(target_provider)
        and capabilities_for(backend).model_id_namespace == MODEL_NAMESPACE_ACP
    ):
        # On a kiro-cli backend an alias such as "sonnet" is not a wire id: stored
        # as-is it would be withheld at session start and the target would stay
        # on the default. Other backends keep their own id namespace untouched.
        model_name = model_registry.acp_id_correction(model_name) or model_name

    with _audit_denials(
        caller_session_key=caller_session_key, operation="set_model", slot_key=slot_key
    ):
        if is_relay_archive(slot):
            raise SessionControlError(RELAY_ARCHIVE_ERROR, code=RELAY_ARCHIVE_CODE, status=409)
        # No restore-allowlist marker is written for the level: the five
        # canonical levels are always restorable (`_REASONING_EFFORT_FALLBACK`),
        # so the marker the effort dropdown writes for other levels is a no-op.
        session_key = effective_session_key(slot)
        if _switch_target_busy(state, slot, session_key, state.sessions.get_provider(session_key)):
            raise _target_busy_error()
        children = await _subagents_attached_response(state, slot, session_key, "set_model")
        if children is not None:
            raise _target_busy_error()
        # The await above can let a turn start, or the target be replaced,
        # linked or mirrored; re-run the idle check and the gate synchronously
        # right before storing the pick.
        session_key = effective_session_key(slot)
        if _switch_target_busy(state, slot, session_key, state.sessions.get_provider(session_key)):
            raise _target_busy_error()
        # A model-picker switch in flight holds _model_pick_lock across its
        # provisional pick-generation bump and a possible rollback. Capturing
        # the generation inside that window would store a value the rollback
        # then invalidates, and the next turn would silently drop the pick.
        # Nothing awaits between here and the store below, so this check
        # holds until the pick is recorded.
        if slot._model_pick_lock.locked():
            raise _target_busy_error()
        # The effort dropdown holds the per-session switch lock across its
        # commit, its live push and any rollback. A level captured inside that
        # window would record a generation the rollback then bumps, and the
        # next turn would silently drop the pick, so refuse it the same way.
        if reasoning_effort is not None and slot_switch_session_lock(session_key).locked():
            raise _target_busy_error()
        live = authorize_target(
            state,
            caller_session_key=caller_session_key,
            target=slot_key,
            operation="set_model",
            skip_enabled_check=True,
            precomputed_ownership_fenced=caller_fenced,
        )
        if live is not slot:
            raise SessionControlError(
                "the target session was replaced; model not changed",
                code="target_replaced",
                status=409,
            )
        caller_tab_id = _caller_tab_id(state, caller_session_key)
        if not caller_tab_id:
            raise SessionControlError(
                "this session has no tab identity to hold a pending pick; model not changed",
                code="caller_unidentified",
                status=403,
            )
        slot._pending_model_pick = PendingModelPick(
            model=model_name,
            caller_session_key=caller_session_key,
            caller_tab_id=caller_tab_id,
            caller_fenced=caller_fenced,
            pick_gen=slot._model_pick_gen,
            reasoning_effort=reasoning_effort,
            effort_gen=slot._effort_pick_gen,
            pair_id_backend=backend in ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS,
        )

    requested = _pick_fields(model_name, reasoning_effort)
    _audit(
        caller_session_key=caller_session_key,
        operation="set_model",
        slot_key=slot_key,
        outcome="allowed",
        detail={**_pick_audit_fields(model_name, reasoning_effort), "stage": "pending"},
    )
    return {"ok": True, "target": slot_key, **requested, "pending": True}


def _pick_fields(model: str | None, reasoning_effort: str | None) -> dict[str, str]:
    """The pick's requested fields, omitting the ones the caller left alone."""
    fields: dict[str, str] = {}
    if model is not None:
        fields["model"] = model
    if reasoning_effort is not None:
        fields["reasoning_effort"] = reasoning_effort
    return fields


def _pick_audit_fields(model: str | None, reasoning_effort: str | None) -> dict[str, str]:
    """:func:`_pick_fields` spelled for the audit trail, where "" reads as a default."""
    fields: dict[str, str] = {}
    if model is not None:
        fields["model"] = model or "auto"
    if reasoning_effort is not None:
        fields["reasoning_effort"] = reasoning_effort or "default"
    return fields


@dataclass(frozen=True)
class PendingModelPick:
    """A ``session_set_model`` pick waiting for the target's next turn.

    Carries the caller's key AND tab identity, its ownership-fence verdict at
    call time, and the target's pick generation then, so a model the user picks
    in the meantime wins over this one. The tab identity is what ties the pick
    to the calling session: a slot key can be handed to a new occupant after
    the caller closes, and that occupant must not inherit the pick.

    ``model`` or ``reasoning_effort`` is ``None`` when the caller left that
    setting alone. ``effort_gen`` is the target's effort write generation at
    call time: any write since (the effort dropdown, a fork, a restore) means
    a newer choice landed, and it wins over this one even when it returned to
    the level the caller saw. ``pair_id_backend`` records that the target's
    backend spells effort into the model id (``gpt-6-astra[max]``), so a
    committed level must also fold a legacy suffix off the pin, as the effort
    dropdown route does.
    """

    model: str | None
    caller_session_key: str
    caller_tab_id: str
    caller_fenced: bool
    pick_gen: int
    reasoning_effort: str | None = None
    effort_gen: int = 0
    pair_id_backend: bool = False


def _pending_pick_view(pick: PendingModelPick | None) -> dict[str, str]:
    """The ``pending_*`` fields ``session_read_message`` reports for *pick*.

    Redacted: the owner's picker stores whatever string it is given.
    """
    if pick is None:
        return {}
    view: dict[str, str] = {}
    if pick.model is not None:
        view["pending_model"] = redact(pick.model)
    if pick.reasoning_effort is not None:
        view["pending_reasoning_effort"] = pick.reasoning_effort
    return view


def _caller_tab_id(state: "DashboardState", caller_session_key: str) -> str:
    """The calling slot's ``_tab_id``, or ``""`` when it has none or is gone."""
    caller_key = caller_slot_key(state, caller_session_key)
    caller = state._slots.get(caller_key) if caller_key else None
    return str(getattr(caller, "_tab_id", "") or "") if caller is not None else ""


def _another_alias_is_mid_turn(state: "DashboardState", slot: "_ChatSlot") -> bool:
    """Whether a slot OTHER than *slot* runs a turn on *slot*'s session.

    The alias half of ``_switch_target_busy``: *slot*'s own turn is the one
    starting, so its ``running`` flag is not a signal here. A registered
    provider with an active turn, or another running slot whose turn key
    names the same session, means the session is not *slot*'s to reset now.
    """
    # circular import: chat_handlers imports this module at module level.
    from kiro_crew.dashboard.chat_handlers import _cancel_target
    from kiro_crew.messaging.link import canonical_key

    session_key = effective_session_key(slot)
    provider = state.sessions.get_provider(session_key)
    has_active_turn = getattr(provider, "has_active_turn", None)
    if callable(has_active_turn) and has_active_turn():
        return True
    target = canonical_key(session_key)
    return any(
        other is not slot and other.running and canonical_key(_cancel_target(other)) == target
        for other in list(state._slots.values())
    )


def legacy_effort_base(model: str | None, *, pair_id_backend: bool) -> str:
    """The base id a pin folds to when an effort level is committed, or ``""``.

    On a backend that spells effort into the model id (codex:
    ``gpt-6-astra[max]``), a pin still carrying a ``[level]`` suffix would keep
    claiming the old level after a new one is committed, so the commit folds the
    pin to its base id. Any other backend, or a pin with no suffix, folds nothing.

    The effort dropdown route and :func:`apply_pending_model_pick` both commit a
    level, and both ask this, so a change to the fold reaches both paths.
    """
    if not pair_id_backend:
        return ""
    base, level = model_registry.split_effort_suffix(model or "")
    return base if level else ""


def effort_commit_needs_reset(provider: object) -> bool:
    """Whether committing a new effort level must reset the live session.

    A live ACP session whose model takes no effort level keeps the level as a
    stored value only: it applies once the user picks a capable model, and a
    reset would restart the session for nothing. Any other live session, or no
    live session, needs the reset (or the live push the dropdown tries first)
    for the level to take.

    Shared by the effort dropdown route and :func:`apply_pending_model_pick`
    for the same reason as :func:`legacy_effort_base`.
    """
    # Duck-typed so this module stays above the agent-SDK boundary. Every
    # LLMProvider has ``supports_effort``, and the base one returns a placeholder
    # False, so only a provider that sets ``reports_effort_support`` (the ACP
    # provider) can skip the reset. Read off the class, as isinstance would, so
    # any other provider, an unspecced test double or no session keeps it.
    if getattr(provider.__class__, "reports_effort_support", False) is not True:
        return True
    return provider.supports_effort() is not False  # type: ignore[attr-defined]


def model_switch_deferred(state: "DashboardState", slot: "_ChatSlot") -> str:
    """Why a between-turns model switch on *slot* must wait, or ``""`` if it may land.

    Shared by every writer that changes ``slot.model`` between turns and arms the
    deferred reset: a queued ``session_set_model`` pick and an Issue Radar crew's
    triage switch. Two reasons, both about a switch the next turn would not see:

    * a model-picker switch is mid-transaction -- its pick generation is
      provisional and may still roll back, so the next turn start decides against
      the settled generation;
    * another slot drives the same session and has a turn in flight (or is
      cold-starting it) -- a commit now would reset nothing that turn can see, since
      get_or_create attaches to that session on its old model.
    """
    if slot._model_pick_lock.locked():
        return "picker in flight"
    if _another_alias_is_mid_turn(state, slot):
        return "alias mid-turn"
    return ""


def arm_model_switch_reset(state: "DashboardState", slot: "_ChatSlot") -> None:
    """Make the next turn start on ``slot.model`` instead of the live session's.

    Armed only when a session can still be on the old model: one already
    registered, or an eager spawn in flight that may register one before
    get_or_create. With neither, get_or_create cold-starts on the new model, and
    an armed reset would tear that session down at turn end for nothing.
    """
    key = effective_session_key(slot)
    eager = slot._eager_spawn_task
    if state.sessions.get_provider(key) is not None or (eager is not None and not eager.done()):
        slot._pending_reset_history_key = slot._pending_reset_history_key or key


async def apply_deferred_model_switch(
    state: "DashboardState",
    slot: "_ChatSlot",
    *,
    session_lock_held: bool = False,
    live_only: bool = False,
) -> bool:
    """Land a model pick the picker took while a turn was in flight.

    ``api_chat_slot_model`` commits ``slot.model`` at once for such a pick and
    records the session it could not touch in ``slot._deferred_model_switch_key``
    (the picker's answer carries ``deferred: true``). The turn it would have cut
    finishes on the model it started on, as every backend's own mid-turn pick
    does (measured on KAS: ``session/set_config_option`` is accepted mid-turn,
    the running turn keeps its model, the next turn runs the pick). This is
    what runs at the boundary, from two callers: the end of the turn the pick
    arrived in (``_end_turn_tail``, before its deferred-reset consume) and the
    start of the slot's next turn (``turn_admission``, the backstop for a turn
    that ended without its tail, and for a sibling alias's turn this slot's tail
    never sees).

    The session is moved exactly the way an idle pick moves it, so every
    harness takes the path it already takes for the picker:
    ``_try_live_model_switch`` first (``session/set_model`` on kiro-cli, the
    model config option on KAS and claude, each harness's own refusal
    handling), and the deferred reset (``arm_model_switch_reset``, consumed by
    the caller's ``_consume_pending_reset``) when the live switch is not
    available — no ACP provider, an unrepresentable target, a failed call, or a
    model the account cannot run, where the cold start's withheld-pin handling
    is the surface that tells the user. A session still busy — another alias
    mid-turn, or a picker transaction holding ``_model_pick_lock`` — keeps the
    pick deferred for a later boundary; the committed ``slot.model`` is what
    any cold start reads meanwhile, so a restart loses nothing.

    ``session_lock_held`` is passed by ``turn_admission``, which has ALREADY
    taken ``slot_switch_session_lock(session_key)`` for its cold start: that
    lock is not reentrant, so the helper must then take only
    ``_model_pick_lock`` or the turn self-deadlocks at session creation. Every
    other caller (``_end_turn_tail``, a future channel-completion hook) holds
    neither lock and leaves it False, so the helper takes the session lock
    itself. That lock is load-bearing on the tail path: ``_model_pick_lock`` is
    per-slot, so it does NOT serialise two aliases sharing one session, and a
    sibling alias's next turn runs ``_restore_refusal_fallback`` inside its own
    held session lock; without taking the same session-keyed lock here, this
    tail ``set_model`` would race that restore on the shared client and the
    restore could overwrite the pick.

    ``live_only`` is passed by the channel-turn boundary
    (:func:`land_model_pick_after_channel_turn`): when the live switch does not
    land, the pick stays deferred instead of arming the reset. A channel turn
    cold-starts on the default model ladder, not on ``slot.model``, so a reset
    there would move the session off the old model without putting it on the
    pick. The next dashboard boundary lands it, as before.

    Returns True when the deferred reset was armed, so the caller's consume
    knows a teardown may follow.
    """
    key = slot._deferred_model_switch_key
    if not key:
        return False
    # circular import: chat_handlers imports this module at module level.
    from kiro_crew.dashboard.chat_handlers import (
        AcpModelUnavailable,
        _broadcast_context_reset,
        _sync_served_model,
        _try_live_model_switch,
        subagents_attached_async,
    )

    if slot._model_pick_lock.locked() or _another_alias_is_mid_turn(state, slot):
        logger.info("Slot %s deferred model switch waits: session still busy", slot.key)
        return False
    # The marker is NOT cleared here. Everything below can be cancelled at the
    # turn deadline while it awaits -- the live switch, or the off-loop sub-agent
    # read -- and a marker cleared before those awaits would lose the pick with
    # no reset armed and nothing to retry. Keep it set until the live switch has
    # SUCCEEDED or a reset is armed: the terminal branches below clear it then,
    # and a cancellation in between leaves the pick retryable at a later boundary.
    session_key = effective_session_key(slot)
    if session_key != key:
        # The slot moved to another session while the pick waited. The old one
        # is not this slot's to move; the current binding is what the next
        # turn runs on, so that is the session the pick lands on.
        logger.info(
            "Slot %s deferred model switch: session rebound %s -> %s", slot.key, key, session_key
        )
    went_live = False
    # Two locks, in the switch handlers' global order (session lock, then pick
    # lock). The session lock is session-keyed and SHARED across aliases, so it
    # is the only thing that serialises this tail set_model against a sibling
    # alias's refusal-fallback restore, which runs inside that alias's own held
    # session lock; the pick lock is per-slot and cannot. The pick lock still
    # serialises against this slot's own model-fallback swap/restore. When
    # admission already holds the session lock (session_lock_held) the helper
    # takes only the pick lock: re-acquiring the non-reentrant session lock on
    # that task would hang the turn at session creation.
    async with contextlib.AsyncExitStack() as _locks:
        if not session_lock_held:
            await _locks.enter_async_context(slot_switch_session_lock(session_key))
        await _locks.enter_async_context(slot._model_pick_lock)
        if not slot._deferred_model_switch_key:
            # Another boundary landed the pick (or armed its reset) while this
            # one waited for the locks: two boundaries can race for one pick
            # once a channel turn's end is one of them. Applying it again would
            # arm a second reset against the session the first one set up.
            return False
        # Read the pick UNDER the locks, not before them. A newer /model request
        # commits through the busy branch WITHOUT taking the session lock, so
        # slot.model and the deferred key can both change while this helper waits
        # for the lock. Reading here applies what is current once serialised,
        # never a model captured before the wait while a newer pick queued in.
        key = slot._deferred_model_switch_key
        model = slot.model or "auto"
        provider = state.sessions.get_provider(session_key)
        try:
            went_live = await _try_live_model_switch(slot.key, slot, provider, model)
        except AcpModelUnavailable as exc:
            # The reset below cold-starts on the committed pin; the account
            # cannot run it, so the cold start withholds it and the slot's
            # withheld-pin card says so — the same surface a restored pin gets.
            logger.warning(
                "Slot %s deferred model switch: live session refused %r (%s); resetting",
                slot.key,
                model,
                exc,
            )
        except Exception:
            logger.warning(
                "Slot %s deferred model switch: live set_model(%r) failed; resetting",
                slot.key,
                model,
                exc_info=True,
            )
    if went_live:
        # The tab can rebind to a different session WHILE the set_model RPC
        # above awaits: a cron tab links to its live runtime through
        # ``ensure_cron_slot`` -> ``_bind_cron_slot``, which takes none of the
        # locks held here. When that happens the switch reached the old
        # session's provider, not the one the slot now runs on. Publishing the
        # old provider's served model and leaving the pick cleared would strand
        # the current binding on the old model with no reset armed to retry.
        # Re-read the key before publishing; on a rebind keep the pick deferred
        # for the current binding, the same way the sub-agent branch below keeps
        # it when a reset cannot be armed. A later boundary applies it against
        # whatever session the slot holds then.
        post_rpc_key = effective_session_key(slot)
        current_model = slot.model or "auto"
        if post_rpc_key != session_key or current_model != model:
            # The binding rebound to another session (a cron tab's
            # ``ensure_cron_slot`` -> ``_bind_cron_slot`` takes none of these
            # locks), or a newer pick changed the model, WHILE the set_model RPC
            # awaited. The switch reached the old session or the old model, so
            # clearing the marker would strand the current binding with nothing
            # to retry. Keep the pick deferred for what is current now; a later
            # boundary lands it on the right session and model.
            slot._deferred_model_switch_key = post_rpc_key or key
            logger.info(
                "Slot %s deferred model switch: binding/model changed to %s/%r during the live "
                "switch; pick kept deferred for the current binding",
                slot.key,
                post_rpc_key,
                current_model,
            )
            return False
        # The live switch succeeded on the current binding and model: now it is
        # safe to clear the marker, past the last await where a cancellation or
        # a newer pick could otherwise be lost.
        slot._deferred_model_switch_key = ""
        _sync_served_model(slot, provider)
        _broadcast_context_reset(state, slot.key, provider)
        state.push_slots_update()
        logger.info("Slot %s deferred model switch landed live on %r", slot.key, model)
        return False
    if live_only:
        # The marker was never cleared above, so the pick stays deferred for the
        # next dashboard boundary (see ``live_only`` in the docstring).
        logger.info(
            "Slot %s deferred model switch held at a channel boundary: live switch did not land",
            slot.key,
        )
        return False
    # The live switch did not land, so the pin applies only via a cold-start
    # reset. ``_consume_pending_reset`` leaves an armed reset UNSPENT while
    # sub-agents share the runtime, so arming one here would leave later turns
    # on the old model with no card while slot.model names the pick. Keep the
    # pick deferred instead: the key stays set, this turn's completion still
    # bills the served model (turn_stats reads the key), and a later boundary
    # retries once the children detach. Same policy the idle path enforces as
    # ``children_409``, applied here as "wait" rather than a dead reset.
    if await subagents_attached_async(state, slot, session_key, "deferred_model_switch"):
        # The marker was never cleared above, so it already names the pick; the
        # explicit set keeps this branch readable and pins it to the current key.
        slot._deferred_model_switch_key = key
        logger.info(
            "Slot %s deferred model switch held: sub-agents attached, reset not armed", slot.key
        )
        return False
    # A reset is the pending mechanism from here, so the marker is cleared: the
    # two cannot both be live or a later boundary would apply the pick twice.
    slot._deferred_model_switch_key = ""
    arm_model_switch_reset(state, slot)
    # Clear the composer's cached context window, as the idle reset path does
    # with a None provider: the pick moves the session to a model whose window
    # and length limit differ, so the readout and the next-prompt length check
    # drop the cached count and wait for the next turn's telemetry. Emitted only
    # here, where the reset is armed -- the sub-agents-attached branch above
    # returns first, so a pick still waiting keeps its measurements until it
    # lands.
    _broadcast_context_reset(state, slot.key, None)
    logger.info("Slot %s deferred model switch: reset armed for %r", slot.key, model)
    return True


def apply_pending_model_pick(state: "DashboardState", slot: "_ChatSlot") -> bool:
    """Commit *slot*'s pending ``session_set_model`` pick, if it is still allowed.

    Called at the start of the slot's turn, before a session is acquired, after
    :func:`prewarm_enabled_check` so the fence re-read below is a cache hit.
    SYNCHRONOUS on purpose: the gate and the writes to ``slot.model`` and
    ``slot.reasoning_effort`` run with no suspension between them, so nothing
    can link or mirror the target after it was authorized and before either
    changed. A pick the gate now refuses is dropped and audited, and the turn
    runs on the model and effort it already had. So is each half of a pick the
    user has overtaken with a newer choice from that half's own control.

    The ownership fence only tightens: a caller fenced at call time stays
    fenced, and one that was not is re-checked now, since it may have become a
    fenced crew member while the pick waited.

    Returns True when the live session must be reset before this turn uses it:
    ``slot.model`` changed (or a fallback serves the session), a codex pin's
    legacy ``[level]`` suffix was folded away, or ``slot.reasoning_effort``
    changed.
    """
    pick = slot._pending_model_pick
    if pick is None:
        return False
    deferred = model_switch_deferred(state, slot)
    if deferred:
        # The pick stays pending and a later turn start decides it (see
        # model_switch_deferred). Nothing is decided yet, so nothing is audited.
        logger.info("session-control set_model: pick on %s deferred, %s", slot.key, deferred)
        return False
    slot._pending_model_pick = None
    # Each half of the pick yields separately to a newer choice the user made
    # from its own control, so a dropdown effort pick does not throw away the
    # caller's model, nor a picker model pick the caller's effort.
    model = pick.model
    effort = pick.reasoning_effort
    if model is not None and slot._model_pick_gen != pick.pick_gen:
        model = None
        _audit(
            caller_session_key=pick.caller_session_key,
            operation="set_model",
            slot_key=slot.key,
            outcome="denied",
            detail={"code": "superseded_by_newer_pick", "stage": "turn_start"},
        )
    # On a pair-id backend (codex) the model picker IS the effort control: a
    # user picking ``gpt-6-astra[max]`` bumps only the model generation, so a
    # newer model pick supersedes the effort half as well.
    if effort is not None and (
        slot._effort_pick_gen != pick.effort_gen
        or (pick.pair_id_backend and slot._model_pick_gen != pick.pick_gen)
    ):
        effort = None
        _audit(
            caller_session_key=pick.caller_session_key,
            operation="set_model",
            slot_key=slot.key,
            outcome="denied",
            detail={"code": "superseded_by_newer_effort", "stage": "turn_start"},
        )
    if model is None and effort is None:
        return False
    if _caller_tab_id(state, pick.caller_session_key) != pick.caller_tab_id:
        _audit(
            caller_session_key=pick.caller_session_key,
            operation="set_model",
            slot_key=slot.key,
            outcome="denied",
            detail={"code": "caller_replaced", "stage": "turn_start"},
        )
        return False
    caller_key = caller_slot_key(state, pick.caller_session_key)
    fenced = pick.caller_fenced or (
        bool(caller_key) and _caller_is_ownership_fenced(state, caller_key)
    )
    # The enabled check runs here too: an operator who disables session control
    # after a pick was queued must stop it from applying. `_run_chat` calls
    # `prewarm_enabled_check` immediately before this, with nothing suspending
    # in between, so the check reads the warmed config and does no file IO.
    try:
        live = authorize_target(
            state,
            caller_session_key=pick.caller_session_key,
            target=slot.key,
            operation="set_model",
            precomputed_ownership_fenced=fenced,
        )
    except SessionControlError as exc:
        _audit(
            caller_session_key=pick.caller_session_key,
            operation="set_model",
            slot_key=slot.key,
            outcome="denied",
            detail={"code": exc.code, "stage": "turn_start"},
        )
        return False
    if live is not slot:
        _audit(
            caller_session_key=pick.caller_session_key,
            operation="set_model",
            slot_key=slot.key,
            outcome="denied",
            detail={"code": "target_replaced", "stage": "turn_start"},
        )
        return False
    changed = False
    if model is not None:
        # A fallback serving the session means the wire model differs from the
        # pin, so an equal pin still needs the session reset, as the picker
        # treats it.
        changed = (
            (slot.model or "") != model
            or bool(slot._active_fallback_model)
            or bool(slot._refusal_fallback_primary)
        )
        slot.model = model
        # A concrete pick answers the routing question, as it does from the picker.
        slot.jev_route = False
        # Recorded as an explicit pick so the model-fallback restore never undoes it.
        slot._model_pick_gen += 1
    if effort is not None:
        base = legacy_effort_base(slot.model, pair_id_backend=pick.pair_id_backend)
        if base:
            slot.model = base
            changed = True
        if (slot.reasoning_effort or "") != effort and effort_commit_needs_reset(
            state.sessions.get_provider(effective_session_key(slot))
        ):
            # A live session keeps the level it started with; the reset the
            # caller arms on True makes the cold start read this one. A model
            # that takes no level keeps it stored only, as the dropdown does.
            changed = True
        slot.reasoning_effort = effort
    _audit(
        caller_session_key=pick.caller_session_key,
        operation="set_model",
        slot_key=slot.key,
        outcome="allowed",
        detail={**_pick_audit_fields(model, effort), "stage": "turn_start"},
    )
    return changed


def channel_drives_session(state: "DashboardState", slot: "_ChatSlot", session_key: str) -> bool:
    """Whether *session_key*'s turns can run on a channel path (Slack, Discord, ...).

    The three signals ``turn_admission`` reads to tell a channel-driven turn from a
    direct dashboard one: a channel session key; a mirror link that accepts inbound
    resume (a ``!sessions`` resume keeps its ``dashboard:`` key yet the channel drives
    it); or a Slack channel bound on the slot. Such a session can go a whole
    conversation without a dashboard turn boundary.
    """
    return (
        is_channel_session_key(session_key)
        or bool(state.sessions.mirror_accepts_inbound(session_key))
        or bool(getattr(slot, "_slack_channel", ""))
    )


#: Landings in flight, held so the loop keeps a reference until each one ends.
_CHANNEL_PICK_LANDINGS: set[asyncio.Task[None]] = set()


def _log_landing_failure(task: asyncio.Task[None]) -> None:
    _CHANNEL_PICK_LANDINGS.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.warning("Model pick landing after a channel turn failed", exc_info=task.exception())


def schedule_channel_pick_landing(state: "DashboardState", slot: "_ChatSlot") -> None:
    """Run :func:`land_model_pick_after_channel_turn` for *slot* on the running loop."""
    task = asyncio.ensure_future(land_model_pick_after_channel_turn(state, slot))
    _CHANNEL_PICK_LANDINGS.add(task)
    task.add_done_callback(_log_landing_failure)


def channel_pick_waits_on(state: "DashboardState", slot: "_ChatSlot", released_key: str) -> bool:
    """Whether *slot* holds a pick that the end of a turn on *released_key* may land.

    *released_key* is the session manager's folded key, so the slot's keys are
    folded the same way before comparing.
    """
    fold = state.sessions._fold_key
    if slot._deferred_model_switch_key:
        if fold(slot._deferred_model_switch_key) == released_key:
            return True
    pick = slot._pending_model_pick
    if pick is None or pick.reasoning_effort is not None:
        return False
    return fold(effective_session_key(slot)) == released_key


async def land_model_pick_after_channel_turn(state: "DashboardState", slot: "_ChatSlot") -> None:
    """Land *slot*'s waiting model pick at a boundary no dashboard turn passes.

    Scheduled by the session manager's release observer when a turn lease on the
    slot's session ends with no dashboard turn of the slot's own reserved or in
    flight (a Slack, Discord, Telegram or Teams turn, a cron run). Two kinds of
    pick wait here:

    * the picker's deferred pick (``_deferred_model_switch_key``);
    * a ``session_set_model`` pick (``_pending_model_pick``) that changes only the
      model. It is committed through :func:`apply_pending_model_pick`, the same
      gate a dashboard turn start runs, and handed to the deferred switch.

    LIVE ONLY. The session moves through the harness's in-place switch or not at
    all: a channel turn cold-starts on the default model ladder rather than on
    ``slot.model``, so the reset a dashboard boundary would arm cannot land the
    pick here. A pick the live switch cannot take, and a pick that sets an effort
    (a live session keeps the level it started with), keep waiting for the next
    dashboard turn, exactly as before this boundary existed.

    The session is between turns when this runs, but a new channel turn may
    already hold it: the live switch declines under an active turn, the pick stays
    deferred, and that turn's own release retries.
    """
    if slot.running:
        # The slot's own dashboard turn owns this boundary, from the moment a
        # send reserves it: its admission and its tail land the pick, reset
        # path included.
        return
    pick = slot._pending_model_pick
    if pick is not None and pick.reasoning_effort is None:
        # Warms the config the pick's fence re-check reads, so the synchronous
        # gate below does no file IO on the loop.
        await prewarm_enabled_check()
        if slot.running or slot._pending_model_pick is not pick:
            return
        if apply_pending_model_pick(state, slot):
            # circular import: chat_handlers imports this module at module level.
            from kiro_crew.dashboard.chat_handlers import save_slot_off_loop
            from kiro_crew.dashboard.chat_utils import slot_history_key

            # slot.model now names the pick and the live session does not run
            # it yet: that is exactly the deferred marker's state.
            slot._deferred_model_switch_key = effective_session_key(slot)
            state.push_slots_update()
            if not await save_slot_off_loop(
                state, slot, force=True, expected_history_key=slot_history_key(slot)
            ):
                slot._dirty = True
    if not slot._deferred_model_switch_key or slot.running:
        return
    # Hold the session's own turn lease across the live switch. The dashboard
    # session lock does not exclude a channel turn, and a channel prompt that
    # starts while the switch awaits its reply would read the same stdout.
    # Taken without waiting: a turn that already holds the lease lands the pick
    # at its own release.
    lease_key = state.sessions._fold_key(effective_session_key(slot))
    if slot_switch_session_lock(effective_session_key(slot)).locked():
        # A dashboard switch or turn start holds the session lock and may be
        # waiting for this lease; taking the lease first would deadlock it.
        return
    if not await state.sessions.try_acquire(lease_key):
        return
    _LANDING_LEASES.add(lease_key)
    try:
        if slot._deferred_model_switch_key and not slot.running:
            await apply_deferred_model_switch(state, slot, live_only=True)
    finally:
        # wake_queue: a channel message that arrived while this landing held
        # the lease was queued behind it, and no channel turn ends to drain it.
        state.sessions.release(lease_key, wake_queue=True)
        _LANDING_LEASES.discard(lease_key)


#: Session keys whose lease a landing holds right now. The release observer
#: skips their release, or a pick the live switch refused would land again,
#: release again, and loop.
_LANDING_LEASES: set[str] = set()


def landing_holds_lease(key: str) -> bool:
    """Whether a model-pick landing is releasing the lease on *key* itself."""
    return key in _LANDING_LEASES


#: Lowercased spellings of the picker's Jev entry that ``session_set_model``
#: refuses alongside the exact sentinel: its display label and any case of the
#: sentinel id. Owner-only, like the sentinel itself.
_JEV_ROUTE_SPELLINGS = frozenset({"auto (jev)", "auto:jev"})


def _target_busy_error() -> SessionControlError:
    """The refusal ``set_model_target`` gives a target with work in flight."""
    return SessionControlError(
        "session busy, model not changed: it has a turn or sub-agents in flight. "
        "Stop it with session_stop and retry once it is idle.",
        code="target_busy",
        status=409,
    )
