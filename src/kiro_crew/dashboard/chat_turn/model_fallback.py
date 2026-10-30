"""The model a dashboard turn runs on: the session-start settle, the fallback chain, the
swap, the restore probe, the refusal hop, the canonical-model backfill, the pinned-model
verdict, and the except arm that falls back when the model refuses the account
(``_on_acp_model_access``)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from kiro_crew.agent_sdk.provider_identity import is_claude_code
from kiro_crew.dashboard.chat_utils import (
    _REFUSAL_FALLBACK_RESUME_MSG,
    _SYNTHETIC_RECOVERY_MSGS,
    TRANSIENT_RETRY_KIND,
    payload_for_replay,
)
from kiro_crew.dashboard.recovery_replays import replays_of
from kiro_crew.llm_helpers import (
    TURN_FALLBACK_ATTR,
    FallbackState,
    configured_fallback_chain,
    pick_epoch_host,
    provider_active_model,
    provider_advertised_ids,
    provider_raw_model,
    resolve_substitute_set_model,
)
from kiro_crew.security import redact

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        SYNTHETIC_RECOVERY_KIND,
        KiroCrewConfig,
        RefusalInfo,
        ReplayFamily,
        _ChatSlot,
        advance_fallback_candidate,
        advertised_model_ids,
        capabilities_of,
        effective_session_key,
        logger,
        model_is_unusable,
        model_registry,
        probe_fallback_restore,
        redact_credentials,
        redact_exfiltration_urls,
        resolve_pin_spelling,
        slot_switch_session_lock,
    )
    from kiro_crew.dashboard.chat_turn.recovery import (
        _session_stop_generation_for,
        _should_suppress_requeue,
        _terminal_error_meta,
    )
    from kiro_crew.dashboard.chat_turn.steer_queue import _has_user_queued_followup
    from kiro_crew.dashboard.chat_turn.turn_entry import _session_stop_generation, _stop_pressed
    from kiro_crew.dashboard.chat_turn.turn_ledgers import (
        _queue_recovery,
        _replay_owes_delivery,
        _requeue_owed_completion,
    )
    from kiro_crew.dashboard.chat_turn.turn_locals import _ChatTurn


def _app_allocation_kwargs(slot: "_ChatSlot") -> dict[str, str]:
    """``{"app_name": <app>}`` for a slot an app owns, else ``{}``.

    Handed to ``get_or_create`` so the session's own model resolution reads the
    app's ``apps.<name>.model`` when the agent pins none. Empty for every other
    slot, so their allocation call is unchanged.
    """
    app_name = getattr(slot, "_app", "")
    return {"app_name": app_name} if isinstance(app_name, str) and app_name else {}


async def _inherited_default_model(
    sessions: Any,
    slot: "_ChatSlot",
    session_key: str,
    *,
    agent: str | None,
    crew_agent: str,
) -> str:
    """The model the session layer would start a new session for *slot* on, or ``""``.

    Read for a slot that pins nothing (no slot pin, no crew pin): it says which
    default the slot inherits right now, so the turn can tell whether its live
    session still runs that default. It is NOT the model the turn requests: an
    inheriting slot asks ``get_or_create`` for no model, and the allocation
    resolves the ladder itself. The answer comes from that same resolver,
    ``session._session_model``, over the session manager's own config (kept
    current by the config watcher), with the inputs the allocation is given, so
    the two cannot disagree.

    ``""`` when every tier defers to the backend, when the session manager holds
    no config, or when the resolver fails (it logs and answers ``None``; it
    never raises, so nothing un-deliverable reaches the thread's Future).
    Off the loop: the resolver globs and reads installed agent JSON.
    """
    # circular import: the session module imports dashboard modules at load.
    from kiro_crew.session import _session_model

    cfg = getattr(sessions, "_cfg", None)
    if not isinstance(cfg, KiroCrewConfig):
        return ""
    app_name = getattr(slot, "_app", "")
    resolved = await asyncio.to_thread(
        _session_model,
        cfg,
        agent,
        crew_agent=crew_agent,
        app_name=app_name if isinstance(app_name, str) else "",
        session_key=session_key,
    )
    return resolved or ""


def _agent_fallback_chain() -> tuple[str, ...]:
    """The configured throttle-fallback chain (agent.fallback_model), or ``()``.

    Thin wrapper over :func:`llm_helpers.configured_fallback_chain`, kept as a
    module-level seam so tests can pin the chain without a config file. This
    only runs on the (rare) budget-exhausted error path, and ``cfg`` bound
    earlier in the turn is possibly-undefined when the config was malformed.
    ``()`` (unset or unreadable) disables the feature: the terminal error
    branch then behaves as though no fallback chain were configured.
    """
    return configured_fallback_chain()


def _sync_served_model(slot: Any, client: Any) -> None:
    """Re-read the live session's served model into the slot.

    The slot's ``served_model`` is a cache of a SESSION fact, and three paths
    change that fact without spawning a session: the explicit live pick
    (``api_chat_slot_model``), the fallback swap (``_fallback_swap_for_turn``)
    and the restore probe (``_probe_fallback_restore_for_slot``). Each of them
    calls this once its ``set_model`` has landed, so the composer chip names
    the model the next turn runs on rather than the one the session was
    spawned with. Read through the provider's PUBLIC ``served_model`` accessor
    -- the AcpProvider wrapper resolves both client shapes and filters the
    ``auto`` sentinel to ``""`` (chip shows "auto", not a stale concrete id).
    getattr-guarded on both sides for the minimal slot/client test doubles.
    """
    record = getattr(slot, "record_served_model", None)
    if record is None:
        return
    record(str(getattr(client, "served_model", "") or ""))


async def _fallback_swap_for_turn(slot: Any, client: Any) -> str | None:
    """Move the slot's live session onto the next usable fallback candidate.

    Called from the interactive error ladder once the same-model transient
    budget is exhausted. Thin slot-state adapter over the SHARED walk step
    (:func:`llm_helpers.advance_fallback_candidate` — the same body the
    unattended surfaces use, so skip rules and marker semantics cannot
    diverge): reconstructs a :class:`FallbackState` from the slot's per-cycle
    walk position, advances one step, and writes the position plus the sticky
    dashboard state back. Returns the candidate id, or ``None`` when the chain
    is exhausted / unconfigured / unusable — the caller then falls through to
    the terminal error branch exactly as today.
    """
    chain = _agent_fallback_chain()
    if not chain:
        return None
    # Same transaction lock as explicit picks: the
    # swap awaits set_model inside advance_fallback_candidate, and a pick
    # landing during that await could be overwritten by the swap — worse, the
    # activation snapshot below would then record the pick as fallback state.
    # Serialising here closes the LAST writer of the pick/fallback fields:
    # explicit pick (chat_handlers), bulk pick (chat_handlers), restore probe
    # (above), and this swap all hold slot._model_pick_lock. getattr-guarded
    # for minimal test stubs; the real _ChatSlot always carries the lock.
    _pick_lock = getattr(slot, "_model_pick_lock", None)
    if _pick_lock is None:
        _pick_lock = asyncio.Lock()
    # The per-slot pick lock alone is disjoint across aliases: a pick made
    # through a DIFFERENT alias of the same wire session holds a different
    # slot's lock, so it can land inside the set_model await below and be
    # absorbed into the epoch snapshot — then the later restore reads
    # not-stale and silently overwrites the user's choice. Hold the
    # session-scoped switch lock too, before the pick lock (the order the
    # switch handlers and the restore probe use, so no inversion), keyed on
    # the live session as the restore probe's lock is.
    _session_lock = slot_switch_session_lock(effective_session_key(slot))
    async with _session_lock, _pick_lock:
        fb_state = FallbackState(
            chain,
            pos=max(0, int(slot._fallback_candidate_idx or 0)),
            primary=slot._fallback_primary_model or "",
        )
        candidate = await advance_fallback_candidate(
            client, fb_state, surface="dashboard", log_suffix=f", slot={slot.key}"
        )
        slot._fallback_candidate_idx = fb_state.pos
        if candidate is None:
            return None
        # The swap moved the LIVE session onto `candidate`; the chip must
        # follow it, or an inheriting slot keeps naming the primary.
        _sync_served_model(slot, client)
        if not slot._fallback_primary_model:
            slot._fallback_primary_model = fb_state.primary
            # Snapshot slot.model and the explicit-pick generation at activation.
            # The generation is what tells a LATER genuine user pick (drop sticky
            # state, never override) apart from the automatic provider backfill
            # writing the served fallback into an unpinned slot (heal and
            # restore); the slot-model snapshot is what the heal restores.
            slot._fallback_slot_model = slot.model or ""
            slot._fallback_pick_gen = slot._model_pick_gen
            # The shared CLIENT pick epoch, same as the model-access path: the
            # restore probe compares it so an alias's explicit pick on the shared
            # wire session is honored. It MUST be snapshotted here too, or the
            # probe's epoch term reads a default 0 against a client epoch a prior
            # pick already bumped, making every throttle restore falsely stale and
            # stranding the session on the fallback.
            slot._fallback_client_pick_epoch = getattr(
                pick_epoch_host(client), "_explicit_pick_epoch", 0
            )
        slot._active_fallback_model = candidate
        slot._fallback_walked.append(candidate)
        return candidate


async def _probe_fallback_restore_for_slot(slot: Any, client: Any) -> None:
    """Start-of-turn restore probe: one ``set_model(primary)`` attempt.

    Fires only while a fallback is active (``slot._active_fallback_model``).
    Restores only when the session is still on the fallback this feature set —
    a user's explicit later pick or a session reset clears the sticky state
    without touching the model. Success is quiet in chat (log only): the
    primary's recovery is the expected state; degradation is the loud event.
    Never raises.
    """
    # The restore is a model transaction like an explicit pick: generation
    # check → set_model → heal → sticky-state clear must not interleave with
    # a pick in flight: an unlocked probe can check the generation, then
    # overwrite a pick that landed during its set_model await.
    # getattr-guarded for minimal test stubs; the real _ChatSlot always
    # carries the lock.
    _pick_lock = getattr(slot, "_model_pick_lock", None)
    if _pick_lock is None:
        _pick_lock = asyncio.Lock()
    # Hold the session-scoped switch lock too, before the pick lock (the same
    # order the switch handlers and the refusal restore use, so no inversion).
    # The pick lock alone is per-slot, so a pick made through a DIFFERENT alias
    # of the same wire session holds a disjoint lock: the staleness signal
    # (pick generation / shared epoch) is read once BEFORE the set_model await,
    # and a cross-alias pick landing inside that await would be applied first
    # and then silently overwritten when the restore's set_model completes last.
    # The session lock makes the two switches strictly ordered — the pick either
    # completes first (the staleness check then drops the record) or starts
    # after the restore finishes (the explicit pick wins by ordering). Keyed on
    # the live session, as the throttle swap's own lock is.
    _session_lock = slot_switch_session_lock(effective_session_key(slot))
    async with _session_lock, _pick_lock:
        await _probe_fallback_restore_for_slot_locked(slot, client)


async def _probe_fallback_restore_for_slot_locked(slot: Any, client: Any) -> None:
    """Body of the restore probe; caller holds ``slot._model_pick_lock``.

    Thin slot-state adapter over the SHARED probe body
    (:func:`llm_helpers.probe_fallback_restore` — the same
    probe/witness/clear sequencing the unattended surfaces use, so the two
    cannot diverge). Only the slot-specific pieces live here:

    - ``state``: the sticky fallback record is slot-held, not the provider
      marker.
    - ``stale``: an explicit user pick made AFTER the swap bumps the pick
      generation — including a pick of the fallback model itself, which
      neither the served model nor slot.model can distinguish from our own
      swap (the automatic provider backfill also writes the served fallback
      into an unpinned slot's model, so comparing slot.model VALUES would
      misread the backfill as a pick and permanently abandon restoration). An
      explicit pick must never be overridden by a restore.
    - ``clear``: slot fields and the provider marker drop as one logical
      record (:func:`_clear_fallback_sticky_state`).
    - ``on_restored``: heal slot.model if the automatic backfill wrote the
      fallback into an unpinned slot while the fallback was active —
      slot.model is re-sent as a set_model override on resume, so leaving the
      fallback id there would re-pin the fallback after the primary
      recovered. No explicit pick happened (``stale`` checked first), so the
      snapshot is the honest value.
    """
    candidate = slot._active_fallback_model
    if not candidate:
        return

    def _heal_backfilled_slot_model() -> None:
        if (slot.model or "") != slot._fallback_slot_model:
            slot.model = slot._fallback_slot_model
        # The restore moved the LIVE session back onto the primary; the chip
        # must follow it off the fallback id.
        _sync_served_model(slot, client)

    _live_pick_epoch = getattr(pick_epoch_host(client), "_explicit_pick_epoch", 0)
    _snap_pick_epoch = getattr(slot, "_fallback_client_pick_epoch", 0)
    # A moved shared epoch is a cross-alias explicit pick, but only when both
    # values are real integers: an epoch host that carries no integer epoch
    # gives no comparable cross-alias signal, so it must NOT force staleness
    # (matches production, where the epoch is always an int, and keeps a
    # non-int stub from reading as a spurious pick).
    _epoch_moved = (
        isinstance(_live_pick_epoch, int)
        and isinstance(_snap_pick_epoch, int)
        and _live_pick_epoch != _snap_pick_epoch
    )
    await probe_fallback_restore(
        client,
        surface="dashboard",
        state=(slot._fallback_primary_model, candidate),
        # Stale when THIS slot moved the pick (slot-local generation) OR when an
        # alias sharing the wire session moved it (the shared client epoch): the
        # slot generation is invisible across aliases, so without the epoch
        # comparison an alias's explicit re-pick of the substitute would be
        # silently overwritten by this restore. Mirrors the refusal path.
        stale=slot._model_pick_gen != slot._fallback_pick_gen or _epoch_moved,
        clear=lambda: _clear_fallback_sticky_state(slot, client),
        on_restored=_heal_backfilled_slot_model,
        log_suffix=f", slot={slot.key}",
    )


def _clear_fallback_sticky_state(slot: Any, client: Any) -> None:
    """Drop ALL sticky fallback state — slot fields AND the provider marker.

    The provider-side :data:`TURN_FALLBACK_ATTR` marker is cleared together
    with the slot fields, always: the two are one logical record, and a marker
    that outlives the slot state re-seeds a long-dead primary into a LATER,
    unrelated fallback walk (the marker-first primary seeding in
    ``advance_fallback_candidate`` would then "restore" a model the user
    explicitly moved away from).

    Marker FIRST, and a failed marker clear returns WITHOUT blanking the
    slot fields: blanking them around a surviving marker would orphan it
    with no dashboard path left to revisit it (only its stale-primary
    reseeding harm above would remain), so the record is retained
    DELIBERATELY — the next turn's probe re-attempts the clear, which
    succeeds for a transient failure and keeps re-failing for a permanently
    hostile attribute. In practice the branch is dead: neither the real ACP
    provider nor the client defines raising attribute hooks, so only exotic
    test doubles reach it; the ordering costs nothing.
    """
    try:
        if getattr(client, TURN_FALLBACK_ATTR, None) is not None:
            setattr(client, TURN_FALLBACK_ATTR, None)
    except Exception:
        logger.debug("clearing fallback marker failed; keeping slot state for retry", exc_info=True)
        return
    slot._active_fallback_model = ""
    slot._fallback_primary_model = ""
    slot._fallback_slot_model = ""


def _configured_refusal_fallback() -> str:
    """The configured refusal-fallback model (agent.refusal_fallback_model), or ``""``.

    Module-level seam (mirroring :func:`_agent_fallback_chain`) so tests can
    pin the value without a config file. ``""`` disables the feature: the
    refusal branches then surface the terminal card exactly as before.
    """
    try:
        return KiroCrewConfig.load().agent.refusal_fallback_model
    except Exception:
        return ""


def _resolve_refusal_fallback_target(refusal: "RefusalInfo | None") -> str:
    """The model one refusal retry should run on, or ``""`` (no retry).

    ``"auto"`` defers to the provider's own suggestion — the refusal
    envelope's ``recommended_model`` — and resolves to ``""`` when the
    envelope names none: with no configured id and no recommendation there
    is nothing sensible to retry on. A concrete configured id wins outright;
    the user chose it knowing their own refusal patterns.
    """
    cfg = _configured_refusal_fallback()
    if not cfg:
        return ""
    if cfg == "auto":
        return (refusal.recommended_model or "").strip() if refusal else ""
    return cfg


async def _refusal_fallback_swap(
    slot: Any, client: Any, candidate: str, session_key: str = ""
) -> str | None:
    """Move the slot's live session onto *candidate* for ONE refusal retry.

    Returns the primary (the model the session served before the swap) when
    the swap landed, or ``None`` when it could not — no ``set_model`` seam,
    the candidate IS the model that just refused (retrying the same filter
    is the pointless case this feature exists to avoid), or ``set_model``
    failed / silently no-oped. Unlike the throttle chain walk this is a
    single explicit hop: the config named one model, so there is nothing to
    advance through, and the sticky record is the slot's refusal fields —
    deliberately NOT :data:`TURN_FALLBACK_ATTR`, whose start-of-turn restore
    probe would move the session back to the primary BEFORE the retry ran.

    Same transaction locks as explicit picks and the restore: the
    session-scoped switch lock (acquired BEFORE the pick lock, the
    documented order) strictly orders this swap's ``set_model`` await and
    epoch snapshot against an alias pick on the shared wire session —
    without it a pick landing inside the await is folded into the snapshot
    and the restore's alias-pick guard cannot see it. The slot-local pick
    lock then orders same-slot picks. getattr-guarded for minimal test
    stubs; the real ``_ChatSlot`` always carries the lock.

    *session_key* is the TURN's binding, captured by the runner at turn
    start — the lock must key off the session the refused turn actually ran
    on, not a live re-derivation: a cron result can bind an unbound slot
    mid-turn, and deriving here would serialize against the newly bound
    session while ``set_model`` applies to the old client. The captured key
    is stamped on the slot so the restore and the drain's rebind check
    share the same domain.
    """
    _pick_lock = getattr(slot, "_model_pick_lock", None)
    if _pick_lock is None:
        _pick_lock = asyncio.Lock()
    _skey = session_key or effective_session_key(slot)
    _session_lock = slot_switch_session_lock(_skey)
    async with _session_lock, _pick_lock:
        primary = provider_active_model(client)
        if not primary:
            # The session is unpinned (the "auto" sentinel) or the model is
            # unknown. The restore leg would have to set_model("auto"), and
            # partitions that do not advertise the sentinel refuse it
            # (AcpModelUnavailable) — the restore then fails every turn and
            # the session stays stranded on the candidate. Swap only when
            # the return leg is provable: "auto" advertised as a target.
            _adv = provider_advertised_ids(client)
            if not _adv or model_is_unusable("auto", _adv):
                logger.info(
                    "refusal fallback: primary model unknown and 'auto' is not "
                    "a provable restore target; surfacing the refusal unswapped"
                )
                return None
            primary = "auto"
        if candidate.strip().lower() == primary.strip().lower():
            return None
        set_model_fn = resolve_substitute_set_model(client)
        if set_model_fn is None:
            return None
        _raw_before = provider_raw_model(client)
        try:
            await set_model_fn(candidate)
        except Exception:
            logger.warning(
                "refusal fallback: set_model(%r) failed; surfacing the refusal",
                candidate,
                exc_info=True,
            )
            return None
        # Witness the swap before announcing it (same rule as the throttle
        # walk): a non-raising set_model can be a silent no-op, and announcing
        # a retry that reruns the refusing model would burn a turn on the
        # same filter.
        _raw_after = provider_raw_model(client)
        if (
            _raw_before
            and _raw_after == _raw_before
            and _raw_after.strip().lower() != candidate.strip().lower()
        ):
            logger.warning(
                "refusal fallback: set_model(%r) was a silent no-op (model still %r); "
                "surfacing the refusal",
                candidate,
                _raw_after,
            )
            return None
        # Preserve an existing record across CHAINED swaps: when the prior
        # turn's restore failed (or silently no-oped) the record still names
        # the TRUE primary, and the "primary" read above is actually the
        # stale fallback the session is stranded on. Overwriting would lose
        # the user's real model permanently (restore would return to fallback
        # A, never to P). First swap: field is empty, records normally.
        _prior_primary = getattr(slot, "_refusal_fallback_primary", "")
        # Same hazard through the THROTTLE walk: while a throttle fallback is
        # actively serving, the wire model is the throttle candidate, not the
        # user's model — the walk's own record names the true primary. The
        # replay turn's throttle probe clears the walk's sticky state via its
        # moved-off branch (the refusal swap moved the wire model), so a
        # record naming the throttle candidate would be the only restore
        # target left, pinning the session to a model the user never chose.
        _throttle_primary = (
            getattr(slot, "_fallback_primary_model", "")
            if getattr(slot, "_active_fallback_model", "")
            else ""
        )
        slot._refusal_fallback_primary = _prior_primary or _throttle_primary or primary
        slot._refusal_fallback_candidate = candidate
        # The binding this swap ran under. The restore locks on THIS key and
        # the drain purges the replay when the live binding differs from it.
        slot._refusal_fallback_session_key = _skey
        # Snapshot the pick generation: an explicit pick landing between now
        # and the restore moves it, and the restore then drops its record
        # instead of overwriting the user's choice (throttle-path rule).
        slot._refusal_pick_gen = getattr(slot, "_model_pick_gen", 0)
        # And the CLIENT-scoped pick epoch: the slot generation is invisible
        # to a pick made through a session alias (a channel-born slot and its
        # dashboard twin share one wire session), but every alias holds this
        # same client object, so the live-switch path stamps it there. Both
        # ends resolve the host through pick_epoch_host — the pick handler
        # and this runner can hold different LAYERS of the same session.
        slot._refusal_client_pick_epoch = getattr(
            pick_epoch_host(client), "_explicit_pick_epoch", 0
        )
        _sync_served_model(slot, client)
        return primary


async def _restore_refusal_fallback(slot: Any, client: Any) -> None:
    """Move the session back to the primary after a single-message refusal retry.

    Runs at the start of the first turn that is NOT the retry replay. When
    the session has moved off the candidate this feature set (explicit
    pick, session reset), the record is stale — drop it and restore nothing,
    mirroring :func:`probe_fallback_restore`'s moved-off rule. One moved-off
    case is handed over instead of dropped: an active throttle walk whose
    recorded restore target IS the candidate advanced off it mid-retry, so
    the walk's target is rewritten to this record's primary and the walk's
    own restore returns the session there. An explicit
    user pick between the retry and this restore wins — even a pick of
    exactly the fallback id, which model equality alone cannot tell apart
    from our own swap: the pick-generation snapshot taken under the pick
    lock at swap time (``_refusal_pick_gen`` vs ``_model_pick_gen``) detects
    it, and the record is dropped without touching the model. A failed or
    unwitnessed restore keeps the record so the next genuine turn tries
    again. Never raises.

    Lock order: the session-scoped switch lock, then the slot's pick lock —
    the same relative order as the switch handlers (which take
    ``slot._lock`` first; this function never touches ``slot._lock``, so no
    inversion is possible). The session lock is what closes the alias race:
    without it, a pick on a DIFFERENT slot of the same session holds only
    disjoint locks, and the epoch snapshot below is read once BEFORE the
    ``set_model`` await — a pick landing inside that await would be applied
    first and then silently overwritten when the restore's ``set_model``
    completes last. Holding the session lock across the whole
    check-and-restore makes the two switches strictly ordered: a pick either
    completes first (the epoch check drops the record) or starts after the
    restore finishes (the pick wins by ordering, as an explicit choice
    should).
    """
    _pick_lock = getattr(slot, "_model_pick_lock", None)
    if _pick_lock is None:
        _pick_lock = asyncio.Lock()
    # Lock on the binding the SWAP recorded, not a live re-derivation: a
    # rebind between swap and restore (cron result binding an unbound slot)
    # would otherwise put the two seams in disjoint lock domains.
    _skey = getattr(slot, "_refusal_fallback_session_key", "") or effective_session_key(slot)
    _session_lock = slot_switch_session_lock(_skey)
    async with _session_lock, _pick_lock:
        primary = slot._refusal_fallback_primary
        candidate = slot._refusal_fallback_candidate
        if not primary:
            return
        # The record belongs to the session the SWAP ran under. After a rebind
        # (cron result binding an unbound slot), ``client`` serves the NEW
        # binding -- applying the record here would move the rebound session's
        # model to a primary it never chose, while the recorded session keeps
        # the candidate. Never apply a record across bindings: drop it. The
        # recorded session is unreachable through this slot, so there is no
        # provider to restore it through.
        _recorded_key = getattr(slot, "_refusal_fallback_session_key", "")
        _live_key = effective_session_key(slot)
        if _recorded_key and _live_key != _recorded_key:
            slot._refusal_fallback_primary = ""
            slot._refusal_fallback_candidate = ""
            logger.warning(
                "refusal fallback: slot %s rebound from %r to %r after the swap; "
                "dropping the restore record instead of moving the rebound "
                "session's model (recorded primary %r stays unrestored)",
                slot.key,
                _recorded_key,
                _live_key,
                primary,
            )
            return
        try:
            # An explicit pick after the swap wins — even a pick of the
            # candidate itself, which current-model equality alone cannot
            # tell apart from the automatic swap. Drop the record without
            # touching the model.
            if getattr(slot, "_model_pick_gen", 0) != getattr(slot, "_refusal_pick_gen", 0):
                slot._refusal_fallback_primary = ""
                slot._refusal_fallback_candidate = ""
                return
            # A pick through a session ALIAS moves the shared client's epoch,
            # not this slot's generation — same drop, same reason: an explicit
            # user pick outranks the automatic restore, whichever slot carried
            # it. (A pick that took the session-RESET path replaces the client
            # entirely; the moved-off check below catches it unless the pick
            # was exactly the candidate, a compound corner accepted as
            # residual.)
            if getattr(pick_epoch_host(client), "_explicit_pick_epoch", 0) != getattr(
                slot, "_refusal_client_pick_epoch", 0
            ):
                slot._refusal_fallback_primary = ""
                slot._refusal_fallback_candidate = ""
                return
            current = provider_active_model(client)
            if current and candidate and current.strip().lower() != candidate.strip().lower():
                _walk_from = (getattr(slot, "_fallback_primary_model", "") or "").strip().lower()
                if (
                    getattr(slot, "_active_fallback_model", "")
                    and _walk_from == candidate.strip().lower()
                ):
                    # The divergence is the throttle walk advancing OFF our
                    # candidate mid-retry: its restore target is the candidate,
                    # a model this session only reached through the refusal
                    # swap. Point the walk's restore at the true primary and
                    # hand this record's job to it — the walk's live choice is
                    # the one model currently known to serve, so moving the
                    # wire model here would fight it.
                    slot._fallback_primary_model = primary
                    slot._refusal_fallback_primary = ""
                    slot._refusal_fallback_candidate = ""
                    logger.info(
                        "refusal fallback: throttle walk advanced off candidate %r; "
                        "redirected its restore target to primary %r, slot=%s",
                        candidate,
                        primary,
                        slot.key,
                    )
                    return
                slot._refusal_fallback_primary = ""
                slot._refusal_fallback_candidate = ""
                return
            set_model_fn = resolve_substitute_set_model(client)
            if set_model_fn is None:
                slot._refusal_fallback_primary = ""
                slot._refusal_fallback_candidate = ""
                return
            _raw_before = provider_raw_model(client)
            await set_model_fn(primary)
            # Witness the restore exactly like the swap: a non-raising
            # set_model can silently no-op, and clearing the record on one
            # would leave the fallback active for the rest of the session
            # with nothing left to retry from. Keep the record instead — the
            # next turn's restore tries again.
            _raw_after = provider_raw_model(client)
            if (
                _raw_before
                and _raw_after == _raw_before
                and _raw_after.strip().lower() != primary.strip().lower()
            ):
                logger.warning(
                    "refusal fallback: restore set_model(%r) was a silent no-op "
                    "(model still %r); keeping the record for the next turn",
                    primary,
                    _raw_after,
                )
                return
        except Exception:
            logger.warning(
                "refusal fallback: restore to %r failed; keeping fallback for this turn",
                primary,
                exc_info=True,
            )
            return
        slot._refusal_fallback_primary = ""
        slot._refusal_fallback_candidate = ""
        _sync_served_model(slot, client)
        logger.info(
            "refusal fallback: restored primary %r after single-message retry on %r, slot=%s",
            primary,
            candidate,
            slot.key,
        )


async def _settle_session_model(
    slot: _ChatSlot,
    client: Any,
    provider_name: str,
    message: str,
    *,
    is_new: bool,
    resumed: bool,
    _is_refusal_retry_turn: bool,
    _synthetic_recovery_turn: bool,
    crew_model: str = "",
) -> bool:
    """Settle the model a turn's session runs on before anything model-dependent.

    Restores a refusal fallback the previous turn left, backfills an unpinned slot
    from the provider, records the pinned-model verdict a fresh or reloaded session
    gives and the model it serves. True when the session withheld the slot's pin.

    ``crew_model`` is the crew's own pin for this turn. Only a slot that pins
    nothing itself but has a crew pin is backfilled; a slot with neither pin
    inherits the default, whatever it resolves to, and is never written.
    """
    # ── Refusal-fallback restore (agent.refusal_fallback_model) ──
    # A refusal retry swapped the live session for ONE message; at the
    # start of any turn that is not that replay, move back to the primary.
    # Placed HERE — before the slot.model backfill and before anything
    # model-dependent (history compression, window_for_provider_client) —
    # so the genuine turn is assembled against the primary's context
    # window, not the fallback's. Synthetic recovery turns are excluded
    # like the throttle restore probe later in ``_run_chat``: an empty-response/compaction
    # continuation of the fallback replay must finish on the model that
    # produced it — including a kind-tagged requeue of the user's OWN
    # words, which the fixed-text membership check cannot recognize.
    # A failed restore keeps the record so the next turn
    # tries again.
    if (
        slot._refusal_fallback_primary
        and not _is_refusal_retry_turn
        and message not in _SYNTHETIC_RECOVERY_MSGS
        and not _synthetic_recovery_turn
    ):
        await _restore_refusal_fallback(slot, client)
    # Backfill slot.model from provider if user didn't explicitly set one.
    # AcpProvider stores the resolved model on client._model. For claude_code
    # that is a provider id; map it back to the canonical registry key so it
    # matches the canonical-keyed dropdown rows (else the active row won't
    # highlight and the header shows the raw provider id). Gated on the real
    # provider so a kiro/acp dotted id (which collides with a claude_code
    # alias spelling) is left as-is.
    withheld_pin = False
    # None = no verdict this spawn: either nothing is pinned (the backfill
    # branch below) or the pin is unjudgeable here (see
    # `_pinned_model_verdict`). Bound before the branch so the backfill path
    # cannot leave it undefined.
    verdict: bool | None = None
    if not slot.model and not slot._active_fallback_model and not slot._refusal_fallback_primary:
        # The fallback-active guard is load-bearing: while a throttle
        # fallback is serving this session, the provider's resolved model
        # IS the fallback candidate, and slot.model is PERSISTED — writing
        # the candidate here would outlive the in-memory sticky state
        # across a gateway restart and turn a temporary fallback into a
        # permanent pin. The refusal-fallback record guards the same
        # hazard on the replay turn (the restore above skips that turn by
        # design, so the provider still reports the refusal candidate
        # here). An unpinned slot simply stays unpinned for the
        # fallback's duration; the next non-fallback turn applies the
        # rule below.
        #
        # A slot that inherits (no crew pin either) is not written, whatever
        # the default resolves to, Auto included: storing the served model
        # would turn "inherit" into a pin, and the slot would stop following
        # a later change to the default. The chip reads the served model
        # through ``_sync_served_model`` instead.
        #
        # Nor while a deferred model pick is pending: a mid-turn Auto pick
        # commits ``slot.model = ""`` and marks ``_deferred_model_switch_key``
        # for the boundary. Backfilling the served (old) model here would
        # overwrite that "" and the boundary would then apply the old model,
        # silently undoing the pick — the same guard the completion backfill
        # carries (``turn_stats``).
        if crew_model and not slot._deferred_model_switch_key:
            slot.model = _backfill_canonical_model(client, provider_name) or slot.model
    elif is_new or resumed:
        # Record the verdict for BOTH answers, not only the withhold: it is
        # carried in the slots payload so the composer reads the
        # backend's own answer instead of inferring "usable?" from whether
        # /api/models happened to list the row.
        #
        # Recorded UNCONDITIONALLY, including the None (unknown) answer. This
        # session is a fresh or reloaded one, so whatever the slot was
        # carrying describes a session that has ended: a replacement
        # that advertises nothing (a dead provider, a backend that omits
        # `models`) must publish "not known" rather than inherit the previous
        # session's entitlement. `record_model_withheld(None)` stores exactly
        # that, and the frontend fails open on it.
        verdict = _pinned_model_verdict(client, slot.model, provider_name)
        slot.record_model_withheld(verdict)
    if is_new or resumed:
        # Record what this fresh/reloaded session actually RUNS on, for both
        # branches above: the inheriting slot (no pin — the first branch,
        # whose backfill leaves `slot.model` empty on purpose) is the one
        # whose chip has nothing else to name, and a withheld pin runs on
        # the same served default. `client` here is the AcpProvider
        # wrapper (see _sync_served_model for why its PUBLIC accessor is
        # the only readable source).
        _sync_served_model(slot, client)
    if verdict:
        withheld_pin = True
        # The session just advertised what this account can run, and the pin
        # is not on the list — the spawn withheld it, so this session runs on
        # the backend default.
        #
        # The pin is deliberately KEPT. Withholding (providers.acp) already
        # guarantees it is never sent and `displayModel` already guarantees
        # it is never shown as the running model, so a stale pin is inert —
        # while clearing it would be a one-way delete of an explicit user
        # setting, decided from ONE session's advertised list. Keeping it
        # means a plan re-upgrade (or a transiently short advertised list)
        # self-heals with no action from the user; clearing would force them
        # to notice and re-pick. Inert-and-recoverable beats tidy.
        #
        # Gated on a fresh/resumed session so this reports once per spawn —
        # the moment the withhold actually happens — rather than repeating on
        # every turn of a warm session.
        logger.warning(
            "Slot %s is pinned to %s, which this account cannot run; "
            "the session is on the backend default (pin kept for re-upgrade)",
            slot.key,
            slot.model,
        )
        # Say it in the transcript too, not only in the server log. Otherwise
        # the chip silently reads Auto, the picker stops listing the model,
        # and there is no way to learn the account lost access to it.
        #
        # A persisted "notice" card rather than a transient activity line: the
        # explanation has to survive a reload, because the state it explains
        # does (the pin stays, and the chip keeps reading Auto). Soft info
        # styling for the same reason the empty-response notices use it — a
        # plan change is not a crash. slot.append persists AND broadcasts one
        # chat_message, so it needs no companion broadcast_ws.
        slot.append(
            "notice",
            f"{slot.model} isn't offered right now — "
            f"this session is running on auto instead. Pick another model "
            f"from the composer, or leave it: your model choice is kept and "
            f"will be used automatically once it's offered again.",
            "msg msg-info",
        )
    return withheld_pin


def _is_bedrock_profile_id(model: str) -> bool:
    """True if *model* is a concrete Bedrock inference-profile id rather than a
    portable model alias.

    A region-routed inference profile (``global.anthropic.claude-opus-4-8[1m]``,
    ``us.anthropic.…``) pins one specific Bedrock model + region. kiro-cli
    resolves the picked alias to such an id internally and reports it on
    ``client._model``; the portable forms the picker sets (``claude-opus-4.7``,
    ``sonnet``, ``deepseek-3.2``) never carry the ``*.anthropic.*`` namespace or
    the ``[1m]`` capability suffix.
    """
    m = model.lower()
    return "anthropic." in m or "[1m]" in m


def _backfill_canonical_model(client: Any, provider: str) -> str:
    """Read the provider's resolved model (``client.client._model``) and map it
    to its canonical registry key for the dropdown, or ``""`` if unavailable.

    AcpProvider stores a provider id on ``_model``. ``canonicalize_for_provider``
    maps it back to the canonical key ONLY for ``claude_code`` (the canonical-
    keyed dropdown); for kiro/acp it is a no-op so a kiro dotted id that happens
    to be spelled like a claude_code alias (e.g. ``claude-sonnet-4.6``,
    ``claude-haiku-4.5``) is NOT rewritten to a claude_code canonical key.
    Skips the ``"auto"`` sentinel. Single home for the slot.model backfill so the
    early (pre-turn) and late (mid-turn init) sites agree.

    kiro-profile guard: on the kiro/acp path ``canonicalize_for_provider`` is a
    no-op, so a backfilled value is stored into ``slot.model`` verbatim and
    re-sent as a ``set_model`` override on every resume. kiro reports the
    RESOLVED Bedrock inference-profile id (e.g.
    ``global.anthropic.claude-opus-4-8[1m]``) — not the alias the user picked —
    so backfilling it pins the slot to one profile + region. A session that once
    resolved to the 1M Opus profile then stays nailed to it across resumes even
    when that profile is capacity-throttled, and the picker cannot
    dislodge the poisoned value (observed: every "model unavailable" throttle hit
    the profile-form id, never the dotted alias, which kiro routes with capacity
    awareness). So for non-``claude_code`` providers we DROP a profile-form id
    (return ``""``) to keep ``slot.model`` empty and let the next get_or_create
    re-resolve; a portable alias (what the picker actually sets) is still kept.
    claude_code is unaffected: its profile id canonicalizes to a dropdown key
    that is the model the user explicitly chose.
    """
    prov_model = getattr(getattr(client, "client", None), "_model", "") or ""
    if not (isinstance(prov_model, str) and prov_model and prov_model != "auto"):
        return ""
    if not is_claude_code(provider) and _is_bedrock_profile_id(prov_model):
        return ""
    return model_registry.canonicalize_for_provider(prov_model, provider)


def _pinned_model_verdict(client: Any, model: str, provider: str) -> bool | None:
    """Whether the live session can run the model this slot is pinned to.

    ``True`` withheld, ``False`` runnable, ``None`` **unknown**. The third state
    is the reason this exists as its own function: the verdict is carried in the
    slots payload so the composer stops re-deriving "usable?" from
    picker-list membership, and a consumer must be able to tell "the account
    cannot run this" from "nothing has told us yet". Every fail-open branch
    below is a genuine unknown, not a runnable answer:

    * no pin, or the ``auto`` sentinel -- nothing to judge;
    * ``claude_code`` / the claude backend -- ``slot.model`` holds a canonical
      key (or a prefixed provider id) against BARE advertised ids, and comparing
      those two namespaces would call every legitimate model unusable (see
      :func:`model_is_unusable`'s namespace note);
    * a provider with no ``available_models`` getter, or one that raised;
    * an empty advertised set -- no session yet, or a backend that omits
      ``models``. :func:`model_is_unusable` folds this into "allow the send",
      which is the right call for the WIRE; for a DISPLAYED verdict it has to
      stay distinguishable from an entitled answer, so it is caught here.

    ``providers.acp`` withholds an inherited/persisted model the account is not
    entitled to and leaves the session on the backend default, so the turn
    succeeds -- but nothing told the user, and the composer chip plus the picker
    went on reporting a model no turn would ever use (observed after a plan
    downgrade: the chip still read ``claude-opus-5`` while every turn ran on
    auto). This is the read side of that withhold, using the SAME predicate and
    the SAME namespace fold (:func:`resolve_pin_spelling`) the wire sites use,
    so the two cannot disagree about what "usable" means.

    A ``True`` verdict is REPORTED, never acted on: the caller does not clear the
    pin. The withhold already keeps the model off the wire, so a stale pin is
    inert and recovers by itself if entitlement returns.

    A pin can carry a stale ``<namespace>::<bare-id>`` qualifier from the
    catalog that advertised it when it was stored, while the session being
    judged advertises the BARE id -- the same class of namespace mismatch the
    advertised-list capability above acknowledges, except here the
    two spellings ARE comparable once the qualifier is peeled. So a literal
    miss is retried through :func:`resolve_pin_spelling` (full id first, then
    one peeled qualifier): the retry can only clear a false withhold, never
    create one, and a pin the backend genuinely does not serve still answers
    withheld under either spelling. The qualifier is deliberately NOT matched
    against ``agent.provider`` -- that config value is a fixed enum (``acp``)
    and never the vocabulary a catalog qualifies its ids with, so keying on it
    would leave the fold unreachable. And when this verdict clears, the wire
    sites clear the same way (they resolve and send the advertised spelling),
    so a ``False`` here still answers "will a turn use this pin?" truthfully.
    """
    if not model or model == "auto" or is_claude_code(provider):
        return None
    if capabilities_of(client).resolves_model_from_advertised_list:
        return None
    getter = getattr(client, "available_models", None)
    if not callable(getter):
        return None
    try:
        advertised = advertised_model_ids(getter())
    except Exception:
        return None
    if not advertised:
        return None
    verdict = model_is_unusable(model, advertised)
    if verdict and resolve_pin_spelling(model, advertised):
        verdict = False
    return verdict


def _record_refused_cycle(turn) -> None:
    """Mark this slot's last turn as a refused cycle of the loop that fired it.

    Only a loop's own delivered cycle counts (``_directive_self_wake``
    with the fired loop's id), so a refusal on a person's turn never stops
    a loop sharing the slot. The fire guard reads the pair before the next
    cycle and stops that loop with ``model_refused``: the refusal card is
    terminal for an unattended cycle, and the next one would send the same
    prompt to the same model.
    """
    _directive_loop_gen = turn._directive_loop_gen
    _directive_loop_id = turn._directive_loop_id
    _directive_self_wake = turn._directive_self_wake
    slot = turn.slot
    if not (_directive_self_wake and _directive_loop_id):
        return
    slot._last_turn_model_refused_loop_id = _directive_loop_id
    slot._last_turn_model_refused_loop_gen = _directive_loop_gen


async def _refusal_fallback_retry(turn) -> bool:
    """One single-message retry on the configured refusal fallback.

    ``True`` means the retry is queued (live session swapped, replay
    at queue index 0, retry notice appended) — the caller skips the
    terminal refusal card. ``False`` falls through to the card
    exactly as before the feature existed: disabled config, nested
    turn, Stop pressed, the one-per-user-message allowance already
    spent (this refusal IS the fallback's), or a swap that could not
    land. A refusal is deterministic FOR ONE MODEL — the whole point
    of the retry is that a different model family routinely accepts
    what another's filter declined.

    The replay takes one of two shapes. A turn refused before it
    dispatched any tool replays the user's message verbatim. A turn
    refused AFTER dispatching tools is instead continued: the fallback
    model is handed the same continuation a person gets from Continue
    (``_REFUSAL_FALLBACK_RESUME_MSG``) and carries on from the completed
    work, because replaying the message would run those tool calls a
    second time — a doubled read is waste, a doubled write is damage.
    """
    _attachment_meta = turn._attachment_meta
    _attachments = turn._attachments
    _crew_log_actor = turn._crew_log_actor
    _is_synthetic = turn._is_synthetic
    _prompt_depth = turn._prompt_depth
    session_key = turn.session_key
    slot = turn.slot
    state = turn.state
    if (
        _prompt_depth != 0
        or _crew_log_actor != "user"
        or slot._refusal_fallback_attempted
        or _should_suppress_requeue(slot)
        or _stop_pressed(turn)
        or _has_user_queued_followup(slot)
    ):
        # _stop_pressed: a Stop that pressed AND resolved back to idle
        # earlier in this turn is invisible to the state check above
        # but not to the monotonic counters — the user abandoned the
        # turn, so replaying it would run a stopped request.
        # _crew_log_actor: the retry is for ATTENDED turns. An unattended
        # producer (cron, autonudge, sub-agent) keeps the terminal
        # refusal behavior — nobody is watching the announced swap, and
        # an unattended replay doubles whatever the wake was about.
        # The replay turn itself carries the original turn's actor, so
        # a user turn's replay is still recognized here.
        # _has_user_queued_followup: a queued USER follow-up is the
        # user's NEXT intent — often a correction of the very message
        # that was refused. The
        # replay inserts at queue index 0, so it would run BEFORE that
        # correction and can dispatch side effects the correction
        # exists to prevent. Surface the refusal instead; the queued
        # message drains normally.
        return False
    _cand = _resolve_refusal_fallback_target(turn._turn_refusal)
    if not _cand:
        return False
    _primary = await _refusal_fallback_swap(slot, turn.client, _cand, session_key=session_key)
    if _primary is None:
        return False
    if _should_suppress_requeue(slot) or _stop_pressed(turn) or _has_user_queued_followup(slot):
        # A stop or a queued follow-up landed while set_model was in
        # flight: the user's intent changed under the swap. Unwind it
        # (witnessed, same helper the next-turn probe uses) and
        # surface the refusal — never requeue ahead of a correction.
        # _stop_pressed covers a Stop that COMPLETED (back to idle)
        # during the await: the state check alone misses it, and the
        # enqueue snapshots below would bake the moved counter in, so
        # the drain purge could never catch it either.
        await _restore_refusal_fallback(slot, turn.client)
        return False
    slot._refusal_fallback_attempted = True
    if turn._turn_tool_calls > 0:
        # The turn already dispatched tools before the filter declined
        # it. Replaying the user's message would run those side
        # effects a second time, so the retry is the continuation a
        # person gets from Continue: the session — now on the fallback
        # model — is asked to carry on from the completed work. The
        # nudge goes to the SAME harness session the refused turn ran
        # on (the retention every Continue relies on), and it tells the
        # model to stop rather than start over when the completed work
        # is not in view, so a session that did not keep the partial
        # turn fails safe instead of re-running writes. What Kiro Crew
        # itself guarantees is narrower: once a tool ran, the message
        # is never re-sent.
        # Runner text, so the payload says CONTINUATION regardless of
        # what the refused message was — a linked thread must not
        # mirror this nudge as user speech. No attachments ride along:
        # the original turn already delivered them into the session.
        _replay_body = _REFUSAL_FALLBACK_RESUME_MSG
        _replay_extra = None
        _replay_payload = payload_for_replay(True)
    else:
        _replay_body = turn.message
        _replay_payload = payload_for_replay(_is_synthetic)
        # The replay is the SAME turn again, so the original's attachment
        # lists ride the queue entry (the drain re-extracts them exactly as
        # it did for the user's row) — a refused message with files retries
        # with its files, not a text-only shadow of itself. The typed
        # mapping is preferred: it keeps ``dirs`` entries under ``dirs``,
        # so a folder attachment replays as a folder instead of being
        # retyped as a file. The flat fallback covers callers that supplied
        # only the untyped list, which by construction holds files.
        if _attachment_meta:
            _replay_extra = {key: list(paths) for key, paths in _attachment_meta.items()}
        elif _attachments:
            _replay_extra = {"files": list(_attachments)}
        else:
            _replay_extra = None
    _replay_qid = _queue_recovery(
        turn,
        0,
        _replay_body,
        kind=SYNTHETIC_RECOVERY_KIND,
        payload=_replay_payload,
        extra_meta=_replay_extra,
    )
    # Stop-generation snapshots (slot + session) at ENQUEUE: the drain
    # purges the replay when either counter moved (a Stop landed while
    # it waited) or user input queued behind it (superseded) — the
    # index-0 replay must never outrun the user's later intent. Its
    # binding is the one the swap just recorded.
    replays_of(slot).arm(
        ReplayFamily.CONTENT_FILTER,
        entry_id=_replay_qid,
        session_key=getattr(slot, "_refusal_fallback_session_key", ""),
        stop_gen=getattr(slot, "_stop_generation", 0),
        session_stop_gen=_session_stop_generation_for(
            getattr(state, "sessions", None), session_key
        ),
    )
    _cat = (turn._turn_refusal.category or "").lower() if turn._turn_refusal else ""
    # Model ids reach this surface from config (LLM-reachable via the
    # MCP config-write path) or from the provider's refusal envelope,
    # so they pass the same redaction as error text before display or
    # logging, and are length-bounded — a credential-shaped value must
    # not persist raw in the transcript or the logs.
    _safe_primary, _ = redact_exfiltration_urls(str(_primary))
    _safe_primary, _ = redact_credentials(_safe_primary)
    _safe_primary = _safe_primary[:120]
    _safe_cand, _ = redact_exfiltration_urls(str(_cand))
    _safe_cand, _ = redact_credentials(_safe_cand)
    _safe_cand = _safe_cand[:120]
    if turn._turn_tool_calls > 0:
        _n_tools = f"{turn._turn_tool_calls} tool call{'s' if turn._turn_tool_calls != 1 else ''}"
        _retry_notice = (
            f"⟳ Response declined by the model's content filter on '{_safe_primary}' "
            f"after {_n_tools} — continuing on '{_safe_cand}' in the same session…"
        )
    else:
        _retry_notice = (
            f"⟳ Response declined by the model's content filter on '{_safe_primary}' — "
            f"retrying once on '{_safe_cand}'…"
        )
    slot.append(
        "error",
        _retry_notice,
        "msg msg-err",
        meta={"kind": TRANSIENT_RETRY_KIND},
    )
    logger.warning(
        "Model refusal for slot %s (category=%s) — %s on "
        "refusal fallback %r (primary=%r, tool_calls=%d)",
        slot.key,
        _cat or "-",
        "continuing once" if turn._turn_tool_calls > 0 else "retrying once",
        _safe_cand,
        _safe_primary,
        turn._turn_tool_calls,
    )
    return True


async def _on_acp_model_access(turn: _ChatTurn, exc, _rejected_id, _access_fb_candidate) -> None:
    """The model refused the account: fall back to a model it may use."""
    _attachment_meta = turn._attachment_meta
    _attachments = turn._attachments
    _is_synthetic = turn._is_synthetic
    client = turn.client
    message = turn.message
    session_key = turn.session_key
    slot = turn.slot
    # ── Reactive model-access-denial fallback ──
    # A new conversation starts on the configured model (commonly the
    # "auto" sentinel), and this account is not ENTITLED to it — a
    # different failure from a throttle/capacity blip on an advertised
    # model. The raise-time classifier tags exactly this case: a named
    # model that is ABSENT from the session's advertised list
    # (``exc.rejected_model`` + ``model_is_unusable`` — the same
    # discriminator ``_model_is_unentitled`` uses to WORD the terminal
    # error, so the trigger and the prose cannot disagree). Because the
    # error is entitlement, not throttle, it is classified terminal and
    # the two throttle-gated fallback branches in _run_chat's except AcpError
    # chain (``acp_error_is_transient``) do not fire, so the first reply just
    # fails. This is the same reactive swap the unattended surfaces
    # run (``stream_and_collect`` Case 2.5 / ``run_bg_oneliner``),
    # on the interactive path.
    #
    # THREE properties the shared candidate selector already guarantees,
    # so this stays a fix and not a new hazard:
    #   - never the failed model: ``first_advertised_fallback`` skips
    #     ``exc.rejected_model`` AND the ``"auto"`` sentinel, so the
    #     default ``agent.fallback_model`` chain of ``("auto",)`` — whose
    #     only entry is the very model that just failed — can never be
    #     the target;
    #   - bounded: one attempt (``_model_access_fallback_used`` one-shot).
    #     An account entitled to NOTHING yields no candidate, _run_chat's elif
    #     goes false, and the terminal ``_on_acp_error_card`` arm surfaces the
    #     entitlement error whose prose already names the served list;
    #   - not a catch-all: _run_chat's elif guard fires ONLY on a named model absent
    #     from the advertised set. An unrelated provider error carries no
    #     ``rejected_model`` and stays terminal, so a real fault is never
    #     masked as a model switch.
    slot.purge_chunks()
    _rejected_safe = redact(str(_rejected_id))
    _cand_safe = redact(str(_access_fb_candidate))
    # Move the live session onto the accessible model through the shared
    # substitute set_model seam (the same one the throttle walk uses);
    # candidates are pre-filtered against the advertised list, so the
    # explicit-pick guard inside set_model does not fire for them.
    _set_model_fn = resolve_substitute_set_model(client)
    if _set_model_fn is None:
        # No set_model seam on this provider — nothing to swap onto.
        # Surface the entitlement error the same way ``_on_acp_error_card``
        # does (visible error card, structural meta so the frontend
        # offers the picker) and end the turn. A bare re-raise here would
        # escape _run_chat to a log-only callback and dead-end the turn
        # with no card, a silent failure.
        logger.info(
            "model access fallback: slot %s provider exposes no set_model; "
            "surfacing entitlement error for %r",
            slot.key,
            _rejected_id,
        )
        _entitle_text = redact(str(exc))
        slot.purge_chunks()
        slot.append(
            "error",
            f"❌ {_entitle_text}",
            "msg msg-err",
            meta=_terminal_error_meta(exc),
        )
    else:
        # Serialise the swap RPC AND the pick/fallback-state writes
        # under the SAME locks every other writer of those fields takes
        # (explicit pick + bulk pick in chat_handlers, throttle swap and
        # restore probe): the per-session switch lock plus the per-slot
        # pick lock. A forced bulk pick (skip_running=false) is a
        # documented concurrent API call that can land in the set_model
        # await below, commit its own model + tear the session down; the
        # session lock keeps this path's state writes ordered against it
        # so they cannot record a stale fallback record or diverge
        # slot.model from the live session.
        # No deadlock: _set_model_fn is the PROVIDER's set_model (see
        # resolve_substitute_set_model) — a provider/client coroutine
        # with no reference to this slot, so it cannot re-acquire the
        # slot-held _model_pick_lock. getattr-guarded for minimal test
        # stubs; the real _ChatSlot always carries the lock.
        _pick_lock = getattr(slot, "_model_pick_lock", None)
        if _pick_lock is None:
            _pick_lock = asyncio.Lock()
        # Take the per-SESSION switch lock outside the per-slot pick lock,
        # the exact order the restore probe and every switch handler use.
        # The per-slot _model_pick_lock is DISJOINT across session aliases
        # (a channel twin and the dashboard tab hold different slot
        # objects), so it alone does not serialize this swap against a
        # concurrent forced bulk pick on a twin slot that holds the
        # session lock. slot_switch_session_lock is keyed by session, so
        # both aliases contend on it and the swap RPC plus the
        # fallback-state writes below cannot interleave with a twin's
        # set_model, keeping persisted fallback state and slot.model
        # consistent with the live session.
        _fb_session_lock = slot_switch_session_lock(session_key)
        async with _fb_session_lock, _pick_lock:
            _swap_ok = False
            try:
                _raw_before = provider_raw_model(client)
                await _set_model_fn(_access_fb_candidate)
            except Exception:
                # The swap RPC itself failed. Same rule as the no-seam
                # corner: surface the ORIGINAL entitlement error through
                # the terminal card path and end the turn, never a bare
                # re-raise (which escapes to a log-only callback and shows
                # the user nothing).
                logger.debug(
                    "model access fallback: set_model(%r) failed; surfacing " "entitlement error",
                    _access_fb_candidate,
                    exc_info=True,
                )
                _entitle_text = redact(str(exc))
                slot.purge_chunks()
                slot.append(
                    "error",
                    f"❌ {_entitle_text}",
                    "msg msg-err",
                    meta=_terminal_error_meta(exc),
                )
            else:
                # Witness the swap before recording state or announcing it
                # (same rule as the restore probe and throttle walk): a
                # non-raising set_model can be a silent no-op that leaves
                # the original still-unentitled model active. Recording
                # fallback state and a "running on X instead" notice here
                # would be a false claim, and the replay would rerun the
                # rejected model. When the model is unchanged and is not
                # the candidate we asked for, treat it as a swap failure
                # and surface the original entitlement error instead.
                _raw_after = provider_raw_model(client)
                if (
                    _raw_before
                    and _raw_after == _raw_before
                    and _raw_after.strip().lower() != _access_fb_candidate.strip().lower()
                ):
                    logger.warning(
                        "model access fallback: set_model(%r) was a silent "
                        "no-op (model still %r); surfacing entitlement error",
                        _access_fb_candidate,
                        _raw_after,
                    )
                    _entitle_text = redact(str(exc))
                    slot.purge_chunks()
                    slot.append(
                        "error",
                        f"❌ {_entitle_text}",
                        "msg msg-err",
                        meta=_terminal_error_meta(exc),
                    )
                else:
                    slot._model_access_fallback_used = True
                    _sync_served_model(slot, client)
                    _swap_ok = True
                if _swap_ok:
                    # Register the SAME sticky fallback record the throttle
                    # walk writes (``advance_fallback_candidate``), for two
                    # reasons the one-shot flag alone does not cover:
                    #   - The spawn backfill (see the ``not slot.model and not
                    #     slot._active_fallback_model`` guard) writes the served
                    #     model into an empty ``slot.model`` whose crewmate
                    #     pins a model, on the replay turn. Without
                    #     ``_active_fallback_model`` set, that
                    #     backfill turns this TEMPORARY substitution into a
                    #     PERSISTENT pin that survives a reload — a durable
                    #     change to the user's slot caused by a transient
                    #     entitlement denial. Setting it makes the guard hold,
                    #     exactly as it does for throttle.
                    #   - ``_probe_fallback_restore_for_slot`` fires only while
                    #     ``_active_fallback_model`` is set; registering it arms
                    #     the start-of-turn probe to set_model back to the
                    #     primary and heal ``slot.model`` once the account can
                    #     use it again.
                    # ``_fallback_primary_model`` is the rejected (configured)
                    # model to restore TO; ``_fallback_slot_model`` snapshots
                    # the slot's pin to heal back (empty for an ``auto`` slot);
                    # ``_fallback_pick_gen`` snapshots the pick generation so a
                    # LATER genuine user pick (which bumps it) is told apart
                    # from the automatic backfill and clears the sticky state
                    # instead of being overridden by a restore.
                    if not slot._fallback_primary_model:
                        slot._fallback_primary_model = _rejected_id
                        slot._fallback_slot_model = slot.model or ""
                        slot._fallback_pick_gen = slot._model_pick_gen
                        # And the shared CLIENT pick epoch: the slot-local
                        # generation is invisible to a pick made through a
                        # session alias (a channel-born slot and its
                        # dashboard twin share one wire session and one
                        # client), so an alias re-picking the substitute
                        # would be undone by this slot's restore unless the
                        # probe also compares the shared epoch. Stamped here,
                        # compared in _probe_fallback_restore_for_slot_locked — mirroring
                        # _refusal_client_pick_epoch on the refusal path.
                        slot._fallback_client_pick_epoch = getattr(
                            pick_epoch_host(client), "_explicit_pick_epoch", 0
                        )
                    slot._active_fallback_model = _access_fb_candidate
                    # Persisted notice card (never silent: the account, not the
                    # user, forced the model change, so it must be said out
                    # loud and survive a reload the way the throttle notice
                    # does).
                    slot.append(
                        "notice",
                        f"⚠️ Your account cannot use model '{_rejected_safe}' — "
                        f"running on '{_cand_safe}' instead.",
                        "msg msg-info",
                    )
                    logger.warning(
                        "model access fallback: slot %s model %r not entitled; "
                        "re-prompting on %r",
                        slot.key,
                        _rejected_id,
                        _access_fb_candidate,
                    )
                    # Re-queue through _queue_recovery like every other retry:
                    # a direct queue_insert carries no admission stamp, so the
                    # drain's fail-closed re-check would destroy this retry in a
                    # channel-linked session.
                    #
                    # The set_model above is a provider RPC that yields the
                    # event loop, so a Stop (or a user follow-up) can land
                    # between _run_chat's elif entry guard and here. Re-check the same
                    # live-stop signals every sibling requeue site checks right
                    # before enqueue: a message the user has since stopped or
                    # replaced must not replay ahead of it. The model is already
                    # swapped and the notice already shown, which is harmless;
                    # we simply abandon the replay and let the turn end.
                    # ``_stop_pressed()`` is the SINGLE live-Stop predicate for
                    # this turn: it is True when a stop is in flight
                    # (``slot._stopping``) OR the monotonic ``slot._stop_generation``
                    # moved since turn entry. That one counter is sufficient for a
                    # channel-linked slot too: every Stop — dashboard or
                    # channel-born — enters through ``stop_slot_turn``, which sets
                    # ``slot._stop_state`` (whose setter bumps ``_stop_generation``
                    # on the idle→active edge) ON THE SLOT and only then routes the
                    # cancel to the linked session key; the link changes which
                    # session the cancel ADDRESSES, not which counter moves
                    # (asserted by test_stop_addresses_linked_session.py). So there
                    # is no session-scoped stop counter this guard could miss — the
                    # slot counter is the one every Stop advances.
                    if _replay_owes_delivery(turn):
                        # An owed completion is queued as the completion it
                        # is: no recovery record, so nothing cancels it
                        # before it runs (it runs on the swapped model).
                        _requeue_owed_completion(turn)
                    elif (
                        not _should_suppress_requeue(slot)
                        and not _stop_pressed(turn)
                        and not bool(getattr(slot, "_pending_steers", None))
                        and not _has_user_queued_followup(slot)
                    ):
                        # Snapshot the stop counter, so the drain can drop
                        # this replay at dequeue if a soft Stop or user
                        # follow-up lands while it waits (the pre-enqueue
                        # guard above closes only the pre-enqueue window).
                        # And the session-scoped one: a Stop issued on a
                        # linked channel surface moves ONLY that one.
                        _ma_stop_gen = getattr(slot, "_stop_generation", 0)
                        _ma_session_stop_gen = _session_stop_generation(turn)
                        # Forward the turn's attachment metadata into the
                        # replay, the same way the sibling refusal replay does:
                        # the replay is the SAME turn again, so a folder
                        # attachment must replay as a folder (keyed under its
                        # meta list) rather than being retyped as a file.
                        # Rebucketing the flat list under "files" would resolve
                        # an [attached_dir N] marker against the wrong list.
                        if _attachment_meta:
                            _ma_replay_extra = {
                                key: list(paths) for key, paths in _attachment_meta.items()
                            }
                        elif _attachments:
                            _ma_replay_extra = {"files": list(_attachments)}
                        else:
                            _ma_replay_extra = None
                        _ma_replay_qid = _queue_recovery(
                            turn,
                            0,
                            message,
                            kind=SYNTHETIC_RECOVERY_KIND,
                            # Verbatim replay, same rule as the
                            # transient/throttle retries.
                            payload=payload_for_replay(_is_synthetic),
                            extra_meta=_ma_replay_extra,
                        )
                        # Record THIS replay's queue id: it is the replay's
                        # identity. The drain claims it for the replay turn
                        # (the turn-start reset then preserves the one-shot
                        # for this replay alone), and its abort removes only
                        # this entry -- SYNTHETIC_RECOVERY_KIND is shared by
                        # many recovery paths, so a removal by kind would
                        # destroy co-queued unrelated recoveries. The binding
                        # is the one this replay's swap ran under: a cron
                        # result binding an unbound slot during the awaited
                        # set_model rebinds the session mid-episode, and the
                        # replay belongs to the OLD session.
                        replays_of(slot).arm(
                            ReplayFamily.MODEL_ACCESS,
                            entry_id=_ma_replay_qid or "",
                            session_key=session_key,
                            stop_gen=_ma_stop_gen,
                            session_stop_gen=_ma_session_stop_gen,
                        )
