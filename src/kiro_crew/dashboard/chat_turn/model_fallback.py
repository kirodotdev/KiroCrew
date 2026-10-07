"""The model a dashboard turn runs on: the session-start settle, the fallback chain, the
swap, the restore probe and the refusal hop."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        _SYNTHETIC_RECOVERY_MSGS,
        TURN_FALLBACK_ATTR,
        FallbackState,
        KiroCrewConfig,
        RefusalInfo,
        _backfill_canonical_model,
        _ChatSlot,
        _pinned_model_verdict,
        advance_fallback_candidate,
        configured_fallback_chain,
        effective_session_key,
        logger,
        model_is_unusable,
        pick_epoch_host,
        probe_fallback_restore,
        provider_active_model,
        provider_advertised_ids,
        provider_raw_model,
        resolve_effective_model,
        resolve_substitute_set_model,
        session_agent_selection_kind,
        slot_switch_session_lock,
    )


def _default_session_model(
    cfg: "KiroCrewConfig | None", slot: "_ChatSlot", agent_model: str
) -> str:
    """The model a slot that pins nothing STARTS a session on, or ``""``.

    Resolved through :func:`resolve_effective_model` — the one resolver the
    dashboard's model chip already reads (``/api/agents/resolved-model``) — so
    the model an auto-created slot runs on is the model the chip says it will.
    Before this the chat-send path only consulted the crew's own pin
    (``bindings.model``) and left everything below it to ``get_or_create``,
    which resolves from the session manager's config SNAPSHOT — refreshed only
    by the settings handlers that call ``refresh_defaults`` — while this turn and
    the chip both read a fresh load. A global ``agent.model`` written any other
    way (``kirocrew config set``, a hand edit) was therefore shown by the chip
    but not run by the first turn of a fresh slot until the gateway restarted.

    The value is deliberately a LOCAL for the ``get_or_create`` call and is
    NEVER written to ``slot.model``. That field is persisted slot state and is
    re-sent as a ``set_model`` override on every resume, so an empty value means
    "inherit": the slot follows a later change to ``agent.model`` or to the
    agent's pin, the chip renders the inherited value live, and the explicit
    slot-create endpoint stores ``""`` when nothing is picked. Persisting the
    resolved default here would silently turn every inheriting slot into a
    permanent pin on its first message, and would route an inherited default
    the account cannot run through the "isn't offered right now" pin flow.

    Returns ``""`` when the slot or crew already pins a model (nothing to
    resolve), when the config could not be loaded, or when every tier defers
    to the backend — ``get_or_create`` then resolves on its own.

    Blocking: ``resolve_effective_model`` reaches ``_resolve_named_agent_model``
    (a glob + per-file read of the installed agent JSON) and
    ``_resolve_agent_model`` (another file read), so callers run this through
    ``asyncio.to_thread`` and never inline on the event loop. That makes the
    ``except`` below load-bearing beyond logging: ``resolve_agent_bindings``
    inside the resolver can raise ``StopIteration`` on a malformed config, and a
    ``StopIteration`` cannot be delivered through a Future (3.12+ substitutes a
    ``RuntimeError``; older interpreters leave the future PENDING and the await
    hangs), so awaiting the thread would fail with the wrong error, or not
    return at all, instead of surfacing the resolver's. ``StopIteration`` is an
    ``Exception`` subclass, so it is converted to ``""`` HERE, in the worker,
    before it can reach the Future boundary. Keep the clause at ``Exception``
    or wider; narrowing it re-opens that hole.
    """
    if slot.model or agent_model or cfg is None:
        return ""
    try:
        kind = session_agent_selection_kind(
            effective_session_key(slot), slot.agent or cfg.default_agent
        )
        if kind == "template":
            return resolve_effective_model(cfg, slot.agent or None, selection_kind=kind)
        return resolve_effective_model(cfg, slot.agent or None)
    except Exception:  # noqa: BLE001 — includes StopIteration; see docstring
        logger.warning("Failed to resolve the default model for slot %s", slot.key, exc_info=True)
        return ""


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

    The session's other reported facts (the model its harness said served the
    last turn, its backend, the crew it runs as, the effort in force) are read
    in the same pass, so every site that refreshes the model refreshes them
    too: a model switch rebuilds the effort option, for one.
    """
    record = getattr(slot, "record_served_model", None)
    if record is None:
        return
    record(str(getattr(client, "served_model", "") or ""))
    record_facts = getattr(slot, "record_session_facts", None)
    if record_facts is None:
        return
    turn_model = getattr(client, "turn_model", "")
    backend = getattr(getattr(client, "capabilities", None), "backend", None)
    agent = getattr(client, "session_agent", None)
    effort = getattr(client, "applied_effort", None)
    record_facts(
        turn_model=turn_model if isinstance(turn_model, str) else "",
        backend=backend if isinstance(backend, str) else None,
        agent=agent if isinstance(agent, str) and agent else None,
        effort=effort if isinstance(effort, str) else None,
    )


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
) -> bool:
    """Settle the model a turn's session runs on before anything model-dependent.

    Restores a refusal fallback the previous turn left, backfills an unpinned slot
    from the provider, records the pinned-model verdict a fresh or reloaded session
    gives and the model it serves. True when the session withheld the slot's pin.
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
        # fallback's duration; the next non-fallback turn backfills as
        # before.
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
