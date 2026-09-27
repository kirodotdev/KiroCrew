"""A target's model and effort: ``session_set_model`` and the deferred model pick the
target's next turn applies.
"""

from __future__ import annotations

import asyncio
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
            # "" (back to the model default) is refused too: on a kiro-family
            # backend the default only takes once the workspace effort overlay
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
    try:
        agent_cfg = (await asyncio.to_thread(KiroCrewConfig.load)).agent
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
    backend = select_provider_backend(effective_session_key(slot), member_backend, default_backend)
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
            effort_intent_gen=slot._session_effort_intent_gen,
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
    the level the caller saw. ``effort_intent_gen`` is the target slot's
    explicit-Default intent generation at call time. A later effort-picker
    choice on any slot driving the same session bumps every such slot when it
    changes the session's explicit-Default flag (a Default pick, or a level
    pick that clears a pending Default) and supersedes this pick's effort half.
    A level pick with no Default pending does not. ``pair_id_backend`` records
    that the target's backend spells effort into the model id
    (``gpt-6-astra[max]``), so a committed level must also fold a legacy suffix
    off the pin, as the effort dropdown route does.
    """

    model: str | None
    caller_session_key: str
    caller_tab_id: str
    caller_fenced: bool
    pick_gen: int
    reasoning_effort: str | None = None
    effort_gen: int = 0
    effort_intent_gen: int = 0
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


@dataclass(frozen=True)
class PendingModelPickCommitSnapshot:
    """The slot state a pending-pick commit can change."""

    pending_model_pick: PendingModelPick
    model: str
    jev_route: bool
    reasoning_effort: str
    model_pick_gen: int
    effort_pick_gen: int


def capture_pending_model_pick_commit(slot: "_ChatSlot") -> PendingModelPickCommitSnapshot:
    """Capture the exact slot state a pending-pick commit can change."""
    pick = slot._pending_model_pick
    assert pick is not None
    return PendingModelPickCommitSnapshot(
        pending_model_pick=pick,
        model=slot.model,
        jev_route=slot.jev_route,
        reasoning_effort=slot._reasoning_effort,
        model_pick_gen=slot._model_pick_gen,
        effort_pick_gen=slot._effort_pick_gen,
    )


def restore_pending_model_pick_commit(
    slot: "_ChatSlot", snapshot: PendingModelPickCommitSnapshot
) -> None:
    """Restore a deferred commit without advancing either pick generation."""
    slot._pending_model_pick = snapshot.pending_model_pick
    slot.model = snapshot.model
    slot.jev_route = snapshot.jev_route
    slot._reasoning_effort = snapshot.reasoning_effort
    slot._model_pick_gen = snapshot.model_pick_gen
    slot._effort_pick_gen = snapshot.effort_pick_gen
    _audit(
        caller_session_key=snapshot.pending_model_pick.caller_session_key,
        operation="set_model",
        slot_key=slot.key,
        outcome="deferred",
        detail={"code": "effort_default_clear_unsaved", "stage": "turn_start"},
    )


def apply_pending_model_pick(state: "DashboardState", slot: "_ChatSlot") -> bool:
    """Commit *slot*'s pending ``session_set_model`` pick, if it is still allowed.

    Called at the start of the slot's turn, before a session is acquired, after
    :func:`prewarm_enabled_check` so the fence re-read below is a cache hit.
    SYNCHRONOUS on purpose: the gate and the writes to ``slot.model`` and
    ``slot.reasoning_effort`` run with no suspension between them, so nothing
    can link or mirror the target after it was authorized and before either
    changed. An effort commit also clears the key's pending explicit Default in
    that same step. A failed save of that clear restores the pick, the slot's
    values and the flag, so this turn runs on its prior values and the next turn
    tries the pick again. A pick the gate now refuses is dropped and audited,
    and the turn runs on the model and effort it already had. So is each half
    of a pick the user has overtaken with a newer choice from that half's own
    control; for effort, a later effort-picker choice on any slot driving the
    same session is newer only when it changes the session's explicit-Default
    flag: a Default pick, or a level pick that clears a pending Default. A
    level pick with no Default pending does not supersede the call.

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
        or slot._session_effort_intent_gen != pick.effort_intent_gen
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
        state.sessions.set_explicit_effort_default(effective_session_key(slot), False)
    _audit(
        caller_session_key=pick.caller_session_key,
        operation="set_model",
        slot_key=slot.key,
        outcome="allowed",
        detail={**_pick_audit_fields(model, effort), "stage": "turn_start"},
    )
    return changed


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
