"""Reasoning effort: the selection-capabilities read,
``POST /api/chat/slots/{slot}/reasoning-effort`` and the on/off switches beside it
(``POST /api/chat/slots/{slot}/ultracode``, ``POST /api/chat/slots/{slot}/fast`` and
``POST /api/chat/slots/{slot}/approve-for-me``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from kiro_crew.dashboard.chat_handlers import (
        _TEARDOWN_INCOMPLETE_WARNING,
        ACP_BACKENDS_APPROVE_FOR_ME,
        ACP_BACKENDS_EFFORT_OVERLAY,
        ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS,
        APPROVE_FOR_ME_MODE,
        AcpProvider,
        DashboardState,
        KiroCrewConfig,
        LLMProvider,
        _app_cancel_denied,
        _append_session_switch_notice,
        _ChatSlot,
        _deny_approval_mode,
        _remember_reasoning_effort_for_restore,
        _reset_slot_session,
        _reset_slot_session_or_warn,
        _slot_replaced_while_queued,
        _slot_switch_session_lock,
        _subagents_attached_response,
        _switch_target_busy,
        apply_approval_mode,
        approve_for_me_engine_supported,
        approve_for_me_keys,
        approve_for_me_offered_on,
        approve_for_me_permitted,
        approve_for_me_profile_binds,
        approve_for_me_reads_on,
        approve_for_me_refresh_engine_support,
        canonical_key,
        cap_effort_capability_levels,
        deny_app_slot_access,
        deny_session_approval_caller,
        effective_session_key,
        get_reasoning_effort_ordered,
        get_reasoning_effort_values,
        harness_effort,
        logger,
        model_registry,
        own_models_backend,
        read_bounded_json,
        relay_archive_refusal,
        resolve_effective_model,
        sel,
        slot_not_found,
    )


async def _configured_backend_for_slot(
    slot: _ChatSlot, config: KiroCrewConfig | None = None
) -> str:
    """Resolve a cold slot through the same member-aware gate as the provider factory.

    *config* is the one to select from; without it, ``config.json`` is read.
    """
    if config is None:
        config = await asyncio.to_thread(KiroCrewConfig.load)
    return configured_backend_for_session(effective_session_key(slot), config)


def configured_backend_for_session(session_key: str | None, config: Any) -> str:
    """The backend a cold start on *session_key* selects under *config*.

    The session's own pick outranks a crewmate's harness pin, which outranks the
    member backend. A pin this gateway cannot start raises
    ``CrewmateHarnessUnavailable``.
    """
    from kiro_crew.crewmate_harness import crewmate_backend_pin
    from kiro_crew.members import select_provider_backend, session_backend_pick

    # A session created on a harness reads as that harness before it starts.
    pick = session_backend_pick(session_key)
    return select_provider_backend(
        session_key,
        config.agent.member_acp_backend,
        config.agent.acp_backend,
        backend_override=pick,
        crew_backend=crewmate_backend_pin(config, session_key) if pick is None else None,
    )


async def _own_models_field(backend: str, config: Any = None) -> dict[str, str]:
    """``models_backend`` for a slot on *backend*, when its pickers need that backend's list.

    *config* is the caller's already-loaded one, so the cold path reads it once.
    """
    config = config if config is not None else await asyncio.to_thread(KiroCrewConfig.load)
    own = own_models_backend(backend, config.agent.acp_backend)
    return {} if own is None else {"models_backend": own}


async def api_chat_slot_selection_capabilities(request: web.Request) -> web.Response:
    """Report the live ACP session's model and effort selection capabilities.

    An absent session is unknown, not unsupported. The composer can preserve its
    cold-start behavior until the first ACP session has advertised its options.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if slot is None:
        return slot_not_found()
    denied = deny_app_slot_access(request.get("app", ""), slot, name, "slot_selection_capabilities")
    if denied is not None:
        return denied
    provider = state.sessions.get_provider(effective_session_key(slot))
    if not isinstance(provider, AcpProvider):
        # The slot can exist before its ACP session. The configured harness
        # still knows whether model IDs encode effort, so the picker can render
        # the base rows while live effort options are pending.
        from kiro_crew.crewmate_harness import CrewmateHarnessUnavailable

        try:
            backend = await _configured_backend_for_slot(slot)
        except CrewmateHarnessUnavailable as exc:
            # The crewmate's pinned harness cannot start: say so, not a 500.
            return web.json_response({"error": str(exc), "code": exc.code}, status=409)
        config = await asyncio.to_thread(KiroCrewConfig.load)
        # An app-owned slot starts on that app's own model setting, as its session does.
        app_kw = {"app_name": slot._app} if slot._app else {}
        # Scoped to the harness this slot routes to, as the session factory scopes it.
        from kiro_crew import model_scope
        from kiro_crew.agent_sdk.capabilities import capabilities_for

        pin = slot.model and model_scope.scoped_pin(
            slot.model,
            capabilities_for(backend).model_id_namespace,
            source="slot",
            log_level=logging.DEBUG,
        )
        model = pin or await asyncio.to_thread(
            resolve_effective_model,
            config,
            slot.agent or None,
            selection_kind=slot.agent_kind,
            backend=backend,
            **app_kw,
        )
        return web.json_response(
            {
                "known": False,
                # The harness a new session would get: the Context panel names it.
                "backend": backend,
                "model_effort_pair_ids": backend in ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS,
                **await _own_models_field(backend, config),
                **_approve_for_me_field(state, slot, backend),
                **harness_effort(
                    state,
                    backend,
                    model,
                    slot.agent or "",
                    model_answers=backend not in ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS,
                ),
            }
        )
    backend = provider.capabilities.backend
    supported = provider.supports_effort()
    levels = (
        cap_effort_capability_levels(
            (
                level
                for level in provider.get_valid_effort_levels()
                if isinstance(level, str) and level in get_reasoning_effort_values()
            ),
            source="local live",
        )
        if supported
        else []
    )
    if supported and not levels:
        # Some model-scoped harnesses (notably kiro-cli) accept effort but do
        # not advertise an ACP config option. Keep the existing fallback menu.
        levels = cap_effort_capability_levels(
            (
                level
                for level in get_reasoning_effort_ordered()
                if level in get_reasoning_effort_values()
            ),
            source="local fallback",
        )
    ultracode = provider.ultracode_enabled()
    fast = provider.fast_mode_enabled()
    # Asked only of a switch that reads off: the harness names no blocker for one on.
    fast_unavailable = provider.fast_mode_unavailable() if fast is False else None
    return web.json_response(
        {
            "known": True,
            "backend": backend,
            "effort_supported": supported and bool(levels),
            "effort_levels": levels,
            "model_effort_pair_ids": backend in ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS,
            **await _own_models_field(backend),
            # Each present only while the session advertises that switch.
            **({} if ultracode is None else {"ultracode": ultracode}),
            **({} if fast is None else {"fast": fast}),
            # The harness's own words for why the Fast mode switch would not stay on.
            **({} if fast_unavailable is None else {"fast_unavailable": fast_unavailable}),
            # Present only on a harness that offers approve-for-me.
            **_approve_for_me_field(state, slot, backend),
        }
    )


# An effort pick that is live and shown on the slot but whose explicit-Default
# intent could not be saved. The status reports the durable write; the slot and
# body report the value that is running.
_EFFORT_INTENT_NOT_SAVED = (
    "the effort choice is running but could not be saved; pick it again once the store is writable"
)
_EFFORT_INTENT_UNSAVED_CODE = "effort_intent_unsaved"
# A reset refusal whose rollback could not restore the earlier intent.
_EFFORT_INTENT_ROLLBACK_NOT_SAVED = "the earlier effort choice could not be saved again"


def _effort_intent_unsaved(
    slot: _ChatSlot,
    effort: str,
    *,
    normalized_model: str | None = None,
    rebind_warning: str | None = None,
) -> web.Response:
    """Report an intent write owed for a pick the live session already runs."""
    # Retry state belongs to the slot that received the pick.
    slot._effort_intent_owed = True
    return web.json_response(
        {
            "error": _EFFORT_INTENT_NOT_SAVED,
            "code": _EFFORT_INTENT_UNSAVED_CODE,
            "reasoning_effort": effort,
            "model": normalized_model,
            "warning": rebind_warning,
        },
        status=503,
    )


def _bump_session_effort_intent_slots(state: DashboardState, session_key: str) -> None:
    """Advance every open slot driving *session_key* after a saved intent change."""
    target = canonical_key(session_key)
    for current_slot in list(state._slots.values()):
        if canonical_key(effective_session_key(current_slot)) == target:
            current_slot._session_effort_intent_gen += 1


async def api_chat_slot_reasoning_effort(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/reasoning-effort — set reasoning effort.

    Body: {"reasoning_effort": "" | "low" | "medium" | "high" | "xhigh" | "max"}.
    "" = provider default (e.g. CC falls back to its opus heuristic, kiro to
    the model's default).

    Works for both ACP backends (claude-agent-acp and kiro-cli) via the
    provider's ``change_effort`` — which pushes the level live to the running
    session (claude: session/set_config_option, kiro: /effort + cli.json
    overlay). Effort is Opus/Sonnet-only; on a non-capable model this is a
    persisted no-op (no live apply, no session reset).
    """
    # circular import: session_control imports this package's modules at module level.
    from kiro_crew.dashboard.session_control import (
        effort_commit_needs_reset,
        legacy_effort_base,
    )

    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    active_slot: _ChatSlot = slot
    denied = deny_app_slot_access(request.get("app", ""), slot, name, "slot_reasoning_effort")
    if denied is not None:
        return denied
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    effort = body.get("reasoning_effort", "")
    valid_efforts = get_reasoning_effort_values()
    if not isinstance(effort, str) or effort not in valid_efforts:
        return web.json_response(
            {
                "error": f"reasoning_effort must be one of: {', '.join(sorted(valid_efforts - {''}))}"
            },
            status=400,
        )
    refusal = relay_archive_refusal(slot)
    if refusal is not None:
        return refusal
    # Same serialization + transactional ordering as the agent switch: the
    # awaits below yield the event loop, so the section runs under the slot's
    # lock, and the slot is mutated only AFTER the switch actually took
    # effect (live update, deferral, or reset) — a failed request provably
    # changed nothing.
    # Two locks, in the order documented at _slot_switch_session_lock:
    # slot._lock, then the session lock. An ExitStack because the session
    # lock's KEY is only known after the in-lock read below, and locking on
    # any earlier read could leave this holding the wrong session lock.
    async with contextlib.AsyncExitStack() as _stack:
        await _stack.enter_async_context(slot._lock)
        # Re-authorize after the await above (see _slot_replaced_while_queued):
        # ``name`` can be recreated for a different app while this request
        # queued, and every read of ``slot`` below would be of the stale one.
        if _slot_replaced_while_queued(state, slot, name, request, "chat.slot_reasoning_effort"):
            return slot_not_found()
        # The session the switch will probe and, on the fallback path, reset —
        # ``effective_session_key``, never ``_history_key_for`` (see
        # api_chat_slot_model): a channel- or cron-born slot runs its turns
        # under its linked key, and the dashboard-prefixed spelling names a
        # session that never existed — the live-effort probe would see
        # nothing and the reset would "succeed" against nothing while the
        # live process kept the old effort. Resolved INSIDE the lock: the
        # binding can land while this request waits on it.
        session_key = effective_session_key(slot)
        # Now serialize against every OTHER alias slot on this same session.
        # slot._lock is created per _ChatSlot and so is DISJOINT across
        # aliases. Keyed on the value resolved just above -- the same one the
        # probe and reset below use -- so the lock provably guards them even if
        # a binding landed while this request waited on slot._lock (see
        # _slot_switch_session_lock).
        await _stack.enter_async_context(_slot_switch_session_lock(session_key))
        # Second lock-acquisition await, second re-check: a same-name
        # recreate lands during this wait just as easily as during the first.
        if _slot_replaced_while_queued(state, slot, name, request, "chat.slot_reasoning_effort"):
            return slot_not_found()
        # App isolation on the SESSION, not just the slot (the cancel routes'
        # policy), BEFORE the same-value fast path so the denial is
        # indistinguishable from a missing slot for every request shape.
        denied = _app_cancel_denied(request, slot, "chat.slot_reasoning_effort", session_key)
        if denied is not None:
            return denied
        _live = state.sessions.get_provider(session_key)
        if effort and isinstance(_live, AcpProvider) and not _live.accepts_effort_level(effort):
            # A level the live session's own vocabulary hides: refused here, so
            # it neither persists nor falls through to a session reset.
            return web.json_response(
                {
                    "error": f"reasoning_effort {effort!r} is not offered by this session",
                    "code": "effort_level_not_offered",
                },
                status=400,
            )
        try:
            await asyncio.to_thread(_remember_reasoning_effort_for_restore, effort)
        except (OSError, ValueError) as exc:
            logger.warning("Cannot retain effort selection: %s", exc)
            return web.json_response(
                {
                    "error": "reasoning effort persistence unavailable",
                    "code": "effort_marker_unavailable",
                },
                status=503,
            )
        provider = state.sessions.get_provider(session_key)
        legacy_model = slot.model
        legacy_base = ""
        if model_registry.split_effort_suffix(legacy_model)[1]:
            # The backend is only worth resolving when the pin carries a suffix.
            from kiro_crew.crewmate_harness import CrewmateHarnessUnavailable

            try:
                backend = (
                    provider.capabilities.backend
                    if isinstance(provider, AcpProvider)
                    else await _configured_backend_for_slot(slot)
                )
            except CrewmateHarnessUnavailable as exc:
                return web.json_response({"error": str(exc), "code": exc.code}, status=409)
            legacy_base = legacy_effort_base(
                legacy_model, pair_id_backend=backend in ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS
            )

        def normalize_legacy_model() -> dict[str, str]:
            if not legacy_base or active_slot.model != legacy_model:
                return {}
            active_slot.model = legacy_base
            active_slot._dirty = True
            return {"model": legacy_base}

        if slot.reasoning_effort == effort:
            # Re-picking the level already shown is still an explicit user
            # choice: bump the effort generation so a pending session_set_model
            # effort yields to it, as the model route bumps its own generation
            # on a same-value pick. Bumped here rather than on the write below,
            # which the setter skips as a same-value write, so a codex pin still
            # carrying a legacy ``[level]`` suffix gets the bump too.
            slot._effort_pick_gen += 1
            if not legacy_base and not slot._effort_intent_owed:
                return web.json_response({"ok": True, "reasoning_effort": effort})
        logger.info("Slot %s reasoning_effort switched to %r", name, effort or "default")

        # With no live session, or with a turn running, the pick is saved here and
        # written to cli.json by the next spawn or turn, where a refusal is only
        # logged. kiro-cli reads effort only from that file, so a pick it refuses on
        # every retry is answered here, as change_effort answers a live one.
        cold = provider is None
        deferred = (
            isinstance(provider, AcpProvider)
            and provider.supports_effort()
            and provider.has_active_turn()
        )
        if effort and (cold or deferred):
            from kiro_crew.session_allocation_parts.cold_start_work_dir import (
                cold_start_work_dir,
            )

            overlay_refusal: Exception | None = None
            try:
                if isinstance(provider, AcpProvider):
                    backend = provider.capabilities.backend
                    work_dir = provider._client._work_dir
                else:
                    backend = await _configured_backend_for_slot(slot)
                    work_dir = await asyncio.to_thread(
                        cold_start_work_dir,
                        state.sessions._session_map,
                        session_key,
                        slot.project or None,
                    )
                if backend in ACP_BACKENDS_EFFORT_OVERLAY:
                    overlay_refusal = await asyncio.to_thread(
                        AcpProvider.effort_overlay_refusal, work_dir
                    )
            except Exception:
                # Nothing could be told about the file: the pick is saved as before.
                logger.debug("Slot %s effort overlay probe failed", name, exc_info=True)
            if overlay_refusal is not None:
                logger.warning(
                    "Slot %s effort change refused by the workspace overlay: %s",
                    name,
                    overlay_refusal,
                )
                return web.json_response(
                    {"error": str(overlay_refusal), "code": "effort_overlay_refused"}, status=400
                )

        _updated_live: bool | None = False
        # True once this request's own explicit clear has rewritten the overlay.
        # A Default committed any other way (no live provider, the deferred push,
        # a clear that raised, a model without effort) is carried to the key's
        # next cold start, the first point that can remove an entry Kiro Crew
        # did not write.
        overlay_cleared = False
        # Set once this request has saved the session key's intent and must not
        # write it again: by the reset path before its reset await (a cold start
        # during that await can spend it, and writing it again at the final
        # commit would arm it a second time), or by a level's clear of a pending
        # Default before its live push.
        intent_recorded = False

        async def record_default_intent(key: str, pending: bool) -> bool | None:
            """Save *key*'s explicit-Default intent.

            True once saved; False when it could not be saved. None when the
            write is refused because a cold start under *key* is in flight: that
            start read the key's effort basis once and publishes a session built
            on that read, so a basis written meanwhile is one it cannot have
            seen, and would leave the slot showing a value the session does not
            run. Nothing is written and the caller answers a retryable 409.
            Checked with no await before the write below, so no start can begin
            between the check and the save. The write is counted
            (``effort_intent_write``) from the in-memory flag through the save
            and its put-back, so a start that takes the key's reservation during
            the save waits for it to settle before reading the basis.

            While a cold start is in flight, a level pick on a key whose flag is
            already clear has nothing to record: it is True at once, with nothing
            written, counted or refused, because the start reads the same basis
            either way. With no start in flight that pick saves the clear flag
            like any other write, so a failed save is reported. While a cold
            start is in flight, a Default pick on a key whose Default is already
            pending stays refused: the start in flight may be on a level its
            caller chose, which leaves that flag pending and brings the session
            up on the level, so the pick has to reach that session once it is
            registered, and the refusal is what makes the retry do that. A
            caller waiting to claim the key's live session, which can wait a
            whole turn, is not a cold start and refuses nothing here.
            """
            if state.sessions.effort_basis_locked(key):
                if not pending and not state.sessions.explicit_effort_default_pending(key):
                    return True
                return None
            with state.sessions.effort_intent_write(key):
                prior = state.sessions.explicit_effort_default_pending(key)
                if prior and not pending:
                    # This request can still put the flag back (a failed save
                    # below, the reset path's rollback), so its row stays counted
                    # until the request ends and no other key's pick can take it
                    # meanwhile.
                    _stack.enter_context(state.sessions.hold_explicit_effort_default(key))
                if not state.sessions.set_explicit_effort_default(key, pending):
                    return False
                try:
                    await state.sessions.aflush()
                except asyncio.CancelledError:
                    # A cancelled pick committed nothing, so its value must not
                    # reach the file with a later save.
                    state.sessions.set_explicit_effort_default(key, prior)
                    with contextlib.suppress(Exception):
                        await state.sessions.aflush()
                    raise
                except Exception:
                    # Memory keeps what the file still holds.
                    state.sessions.set_explicit_effort_default(key, prior)
                    logger.warning("Could not save explicit effort Default intent", exc_info=True)
                    return False
                if prior != pending:
                    _bump_session_effort_intent_slots(state, key)
            return True

        def intent_unavailable() -> web.Response:
            # Answered before the slot's value changed or anything reached the
            # session it is bound to, so a retry starts over.
            return web.json_response(
                {
                    "error": "the effort choice could not be saved",
                    "code": "effort_intent_unavailable",
                },
                status=503,
            )

        def start_in_flight() -> web.Response:
            # A cold start under the key is in flight and has read, or will read,
            # the effort basis this pick would change (``record_default_intent``
            # returned None; a pick that would leave it as it is passes). Nothing
            # changed, so a retry once it has registered lands. The same code a
            # live turn earns: the picker's failure copy for it already says to
            # try again when the session is free.
            return web.json_response(
                {"error": "the session is starting", "code": "turn_in_flight"}, status=409
            )

        def intent_refused(recorded: bool | None) -> web.Response:
            """The refusal for an intent write that did not land before anything changed."""
            return start_in_flight() if recorded is None else intent_unavailable()

        async def put_back_default_unless_committed() -> None:
            # Runs as this request exits: a pick that did not leave the slot on
            # its level has not replaced the Default it cleared before the push.
            # Written even while a cold start under the key is in flight: it
            # restores a Default this same request cleared, so a start that read
            # the cleared value arms nothing and the NEXT cold start applies the
            # flag. The intent survives, one start late, and the hold taken at
            # the clear is what keeps its row.
            if active_slot.reasoning_effort == effort:
                return
            state.sessions.set_explicit_effort_default(session_key, True)
            try:
                await state.sessions.aflush()
            except Exception:
                # Memory keeps the Default, and the map's next save writes it.
                logger.warning("Could not save the explicit effort Default again", exc_info=True)

        async def undo_live_push() -> bool:
            """Put the live session this pick reached back on the slot's value.

            True once that session runs the value again. An automatic clear puts
            back a Default, so an entry Kiro Crew did not write stays.
            """
            if not isinstance(provider, AcpProvider):
                return False
            try:
                if active_slot.reasoning_effort:
                    return bool(await provider.change_effort(active_slot.reasoning_effort))
                return bool(await provider.clear_effort())
            except Exception:
                logger.warning("Could not put the live session back on its effort", exc_info=True)
                return False

        # True once this request pushed its pick to the live session, which cannot
        # be unwound: from then on a failed save keeps the pick and answers 503.
        pushed_live = False

        if isinstance(provider, AcpProvider) and provider.supports_effort():
            # Guard against racing the in-flight prompt read loop: a live
            # change_effort issues session/set_config_option and its response wait
            # would call stdout.readline() concurrently with the streaming
            # _prompt_loop → dropped/misrouted frame or a stuck turn. The override
            # is already persisted on the slot, so defer the live push to the next
            # turn instead of pushing now or resetting (effort is a cheap knob).
            if provider.has_active_turn():
                logger.info("Slot %s deferred live effort push: turn active", name)
                # This path's success point: the override is recorded on the
                # slot now and pushed to the live session next turn.
                recorded = await record_default_intent(session_key, not effort)
                if not recorded:
                    return intent_refused(recorded)
                slot.reasoning_effort = effort
                normalized = normalize_legacy_model()
                slot._effort_intent_owed = False
                state.push_slots_update()
                return web.json_response(
                    {"ok": True, "reasoning_effort": effort, "deferred": True, **normalized}
                )
            # change_effort handles both backends and persists the per-model
            # override + overlay. "" clears the slot's level → fall back to the
            # model default (kiro: /effort with the workspace default; claude: the
            # adapter's ``default`` live), else False asks for the reset below.
            # This is the one explicit operator clear, so it alone may remove an
            # overlay entry Kiro Crew did not write; every automatic clear (a
            # live model switch on a Default slot) keeps the ownership guard.
            #
            # A level replaces a pending Default for good, so that clear is saved
            # before the push: a push the file could not follow would leave the
            # flag for a later cold start that takes no level, and that start
            # would run Default under the level the slot shows.
            if effort and state.sessions.explicit_effort_default_pending(session_key):
                recorded = await record_default_intent(session_key, False)
                if not recorded:
                    return intent_refused(recorded)
                intent_recorded = True
                _stack.push_async_callback(put_back_default_unless_committed)
            try:
                if effort:
                    _updated_live = await provider.change_effort(effort)
                else:
                    _updated_live = await provider.clear_effort(
                        every_model=True,
                        owned_only=False,
                        fence_rewrite=state.sessions.fence_effort_overlay_rewrite,
                    )
                    # A bool means this clear left nothing for a cold start to
                    # remove (False only asks for the reset below); None changed
                    # nothing and is refused below.
                    overlay_cleared = _updated_live is not None
                pushed_live = bool(_updated_live)
            except Exception as exc:
                if getattr(exc, "overlay_refused", False) is True:
                    # The workspace cli.json refuses every write, pick or clear,
                    # and kiro-cli reads effort only from it: a reset would start
                    # on the file's level while the slot showed this one.
                    logger.warning(
                        "Slot %s effort change refused by the workspace overlay: %s", name, exc
                    )
                    return web.json_response(
                        {"error": str(exc), "code": "effort_overlay_refused"}, status=400
                    )
                if provider.switches_by_relaunch is True or (
                    effort and getattr(exc, "command_refused", False) is True
                ):
                    # The relaunch refused the level and put the prior one back,
                    # or kiro-cli refused it and change_effort rolled back.
                    # Committing it, or resetting to cold-start on it, would only
                    # fail the same way on every later message.
                    logger.warning(
                        "change_effort(%s) refused for slot %s: %s: %s",
                        effort,
                        name,
                        type(exc).__name__,
                        exc,
                    )
                    # A fixed sentence: the exception text can carry agent-authored
                    # words, a path or a host. The log line above keeps the detail.
                    return web.json_response(
                        {
                            "error": "The agent did not accept the reasoning effort change.",
                            "code": "effort_refused",
                        },
                        status=400,
                    )
                logger.warning(
                    "change_effort(%s) failed for slot %s: %s: %s — falling back to reset",
                    effort,
                    name,
                    type(exc).__name__,
                    exc,
                )
        elif not effort_commit_needs_reset(provider):
            # Model does not support effort: persist the slot value for when the
            # user switches to a capable model, but do not touch the live session.
            _updated_live = True
            logger.info("Slot %s effort persisted (model not effort-capable)", name)

        if _updated_live is None:
            # clear_effort's third outcome: NOTHING changed -- not the workspace
            # overlay, not the provider's map. A bool cannot carry that here,
            # because both of its values commit the new slot value below (the
            # reset branch at the `slot.reasoning_effort = effort` before its
            # teardown, and the success path at the one before the final 200),
            # so either would show "default" while the overlay still holds the
            # old level and a respawn re-applies it. Commit nothing, reset
            # nothing, and report that the overlay is locked, a link or in a
            # folder outside the work dir or on a sensitive path, unreadable, non-object
            # or holding a non-object setting for this model, or past its read ceiling
            # as read or once rewritten.
            return web.json_response(
                {
                    "error": (
                        "the workspace effort overlay is locked or fenced by a spawning session, a link or "
                        "in a folder outside the work dir or on a sensitive path, unreadable, "
                        "not a JSON object, holds a non-object setting for this model, or exceeds "
                        "its 1 MiB size limit or would exceed it once rewritten"
                    ),
                    "code": "effort_overlay_busy",
                },
                status=409,
            )
        replaced_override: str | None = None
        if _updated_live:
            # The live pick replaces the allocation's recorded effort override, so
            # a restart or reset successor starts on the level this session runs.
            replaced_override = state.sessions.record_effort_override(session_key, effort)

        def refuse_restoring_override(recorded: bool | None) -> web.Response:
            # A refused pick leaves the session on its earlier level, so a
            # successor starts on that level, not on the one refused here.
            if replaced_override is not None:
                state.sessions.record_effort_override(session_key, replaced_override)
            return intent_refused(recorded)

        if effective_session_key(slot) != session_key and _updated_live:
            # The slot was bound to a different session while change_effort /
            # clear_effort awaited its provider RPC. The push landed on the
            # session the slot WAS bound to, and change_effort already
            # persisted the per-model override + overlay — that cannot be
            # unwound, so a 409 here would claim a rollback that did not
            # happen and leave the slot value contradicting the persisted
            # override. Commit the slot value (it is what the new binding's
            # next cold start reads) and report the rebind as a warning.
            #
            # This request never touched the new binding's overlay, so a Default
            # is carried to that key's next cold start, and a level clears any
            # Default pending there. That is saved before the slot shows the
            # pick: otherwise the slot would show a pick that key's next cold
            # start does not run, and a retry of a Default would stop at the
            # same-value check. A cold start in flight under the new binding
            # refuses the write the same way a failed save does: it already read
            # the basis this pick would change.
            saved_new = await record_default_intent(effective_session_key(slot), not effort)
            if not saved_new and (not pushed_live or await undo_live_push()):
                # Nothing was pushed, or the session the push reached runs the
                # slot's value again, so the refusal describes a pick that
                # changed nothing. When it cannot be put back, the push stands
                # and the pick is kept, answered 503 below.
                return refuse_restoring_override(saved_new)
            slot.reasoning_effort = effort
            # The push landed on the session the slot WAS bound to, so that key
            # follows the ordinary rule, unless a level already cleared its
            # Default before the push.
            saved_old = intent_recorded or await record_default_intent(
                session_key, not effort and not overlay_cleared
            )
            normalized = normalize_legacy_model()
            state.push_slots_update()
            warning = (
                "slot session was rebound during the switch; "
                "the new binding applies on its next cold start"
            )
            if not (saved_old and saved_new):
                return _effort_intent_unsaved(
                    slot,
                    effort,
                    normalized_model=normalized.get("model"),
                    rebind_warning=warning,
                )
            slot._effort_intent_owed = False
            return web.json_response(
                {
                    "ok": True,
                    "reasoning_effort": effort,
                    **normalized,
                    "warning": warning,
                }
            )

        if not _updated_live:
            if effective_session_key(slot) != session_key:
                # Rebound while a FAILED live push awaited: the reset fallback
                # below would tear down the wrong session, and nothing is
                # committed yet on this path, so there is genuinely nothing
                # to roll back — answer the same 409 the model/workspace
                # handlers use so the retry resolves the current binding.
                return web.json_response(
                    {
                        "error": "slot session was rebound during the switch",
                        "code": "session_rebound",
                    },
                    status=409,
                )
            # Never tear down an in-flight turn (the model handler's policy,
            # and the _cancel_target subtlety: a RUNNING turn owns a captured
            # identity, so the key resolved above may not be the turn's).
            # Children guard, shared with reload/model: the reset tears down
            # the runtime attached sub-agents run on. Nothing is committed
            # yet, so a refusal here changes nothing.
            children_409 = await _subagents_attached_response(
                state, slot, session_key, "slot_reasoning_effort"
            )
            if children_409 is not None:
                return children_409
            # Probed AFTER the change_effort and children awaits above, in
            # the NO-AWAIT window before the commit and reset below; the
            # signals and the sibling-alias window are on _switch_target_busy.
            # The effort-capable live provider's active turn never reaches
            # this — the defer branch above already returned for it. A 409 is
            # retryable once the turn completes; nothing is committed yet.
            recheck = state.sessions.get_provider(session_key)
            if _switch_target_busy(state, slot, session_key, recheck):
                return web.json_response(
                    {"error": "a turn is in flight", "code": "turn_in_flight"}, status=409
                )
            # No live session (or live update failed): reset so the next cold
            # start picks up the new effort via the provider factory/overlay.
            # The effort is committed BEFORE the reset: a message send landing
            # while the reset await is in flight cold-starts a session from
            # the slot's CURRENT value, so the new effort must already be
            # visible. A failing teardown does NOT undo the switch (the reset
            # pops the session first, so every replacement runs the new
            # value) and is reported as a success with a warning — a 500
            # would make the acting tab keep the OLD store value for a
            # switch that actually happened.
            prior_effort = slot.reasoning_effort
            prior_explicit_default = state.sessions.explicit_effort_default_pending(session_key)
            recorded_default = not effort and not overlay_cleared
            recorded = await record_default_intent(session_key, recorded_default)
            if not recorded:
                return intent_refused(recorded)
            intent_recorded = True
            slot.reasoning_effort = effort

            async def restore_default_intent() -> bool:
                # Undo only this request's own write: a cold start during the
                # reset await may already have applied and spent it. One still
                # in flight has read this request's write, so the undo is
                # refused and reported as a rollback not saved.
                current = state.sessions.explicit_effort_default_pending(session_key)
                if current != recorded_default:
                    return True
                return bool(await record_default_intent(session_key, prior_explicit_default))

            teardown_incomplete = False
            reset_ok = True
            try:
                # A POST-POP teardown raise does NOT undo the switch (the
                # reset pops the session first, so every replacement runs the
                # new value) and is answered as a success with a warning —
                # but only once the helper has verified the raise came AFTER
                # the session pop. A PRE-POP raise means the old session
                # survives on the old effort, so the helper propagates it and
                # this request restores the prior effort and answers 500.
                # The helper resets with skip_if_busy=True, which keeps this
                # handler's decline ladder below:
                # SessionManager.reset evaluates busyness atomically with the
                # session pop, so a turn that slipped into the residue (or
                # holds the semaphore before its prompt is in flight, which
                # has_active_turn cannot see) is declined here instead of torn
                # down mid-stream. The helper's None verdict marks the
                # degraded post-pop teardown; a bool verdict is the reset
                # outcome the ladder reads.
                reset_verdict = await _reset_slot_session_or_warn(
                    state, slot, session_key, switch_kind="reasoning_effort"
                )
            except Exception:
                # The identity probe proved the pop never happened: the old
                # session is still alive on the old effort, so the committed
                # value describes a switch that did not take and the acting
                # tab keeps its OLD store value on the 500. Restore the prior
                # effort (only if this request's write still stands) and
                # re-push so clients and persisted state land on the truth,
                # then let the raise escape as a 500.
                if slot.reasoning_effort == effort:
                    slot.reasoning_effort = prior_effort
                    if not await restore_default_intent():
                        logger.warning(
                            "Could not restore explicit effort Default intent for %s", session_key
                        )
                slot._dirty = True
                state.push_slots_update()
                raise
            if reset_verdict is None:
                teardown_incomplete = True
            else:
                reset_ok = reset_verdict
            if not reset_ok and not teardown_incomplete:
                # Disambiguate the decline FAIL-CLOSED (the workspace
                # handler's template): live provider mid-turn → roll back and
                # 409; live IDLE provider → retry once; no live provider →
                # nothing to tear down, the next message cold-starts under
                # the new effort.
                busy_provider = state.sessions.get_provider(session_key)
                if isinstance(busy_provider, LLMProvider):
                    if busy_provider.has_active_turn():
                        slot.reasoning_effort = prior_effort
                        if not await restore_default_intent():
                            return web.json_response(
                                {
                                    "error": "a turn is in flight",
                                    "code": "turn_in_flight",
                                    "warning": _EFFORT_INTENT_ROLLBACK_NOT_SAVED,
                                },
                                status=409,
                            )
                        return web.json_response(
                            {"error": "a turn is in flight", "code": "turn_in_flight"},
                            status=409,
                        )
                    # Retry through the same probing helper: a pre-pop raise on
                    # the retry propagates (restore + 500) exactly as the first
                    # attempt, and a post-pop raise becomes the committed 200 +
                    # warning below.
                    try:
                        reset_verdict = await _reset_slot_session_or_warn(
                            state,
                            slot,
                            session_key,
                            switch_kind="reasoning_effort",
                        )
                    except Exception:
                        if slot.reasoning_effort == effort:
                            slot.reasoning_effort = prior_effort
                            if not await restore_default_intent():
                                logger.warning(
                                    "Could not restore explicit effort Default intent for %s",
                                    session_key,
                                )
                        slot._dirty = True
                        state.push_slots_update()
                        raise
                    if reset_verdict is None:
                        teardown_incomplete = True
                    else:
                        reset_ok = reset_verdict
                    if (
                        not reset_ok
                        and not teardown_incomplete
                        and state.sessions.get_provider(session_key) is not None
                    ):
                        slot.reasoning_effort = prior_effort
                        if not await restore_default_intent():
                            return web.json_response(
                                {
                                    "error": "a turn is in flight",
                                    "code": "turn_in_flight",
                                    "warning": _EFFORT_INTENT_ROLLBACK_NOT_SAVED,
                                },
                                status=409,
                            )
                        return web.json_response(
                            {"error": "a turn is in flight", "code": "turn_in_flight"},
                            status=409,
                        )
            if effective_session_key(slot) != session_key:
                # Same check after the reset await as after the live push:
                # the session torn down is not the slot's, so the
                # commit would advertise an effort a session that never saw
                # the switch does not run. The teardown itself was harmless
                # (that session was idle and already unbound); roll back and
                # let the retry resolve the current binding.
                slot.reasoning_effort = prior_effort
                if not await restore_default_intent():
                    return web.json_response(
                        {
                            "error": "slot session was rebound during the switch",
                            "code": "session_rebound",
                            "warning": _EFFORT_INTENT_ROLLBACK_NOT_SAVED,
                        },
                        status=409,
                    )
                return web.json_response(
                    {
                        "error": "slot session was rebound during the switch",
                        "code": "session_rebound",
                    },
                    status=409,
                )
            # The reset path is committed: mark the boundary in the
            # transcript, inside both locks. The live-push, deferral and
            # not-effort-capable paths reset nothing and append nothing, and so
            # does a same-level re-pick that only normalizes a legacy pin.
            if prior_effort != effort:
                await _append_session_switch_notice(slot, "reasoning_effort", effort or "default")
            if teardown_incomplete:
                normalized = normalize_legacy_model()
                slot._effort_intent_owed = False
                state.push_slots_update()
                return web.json_response(
                    {
                        "ok": True,
                        "reasoning_effort": effort,
                        **normalized,
                        "warning": _TEARDOWN_INCOMPLETE_WARNING,
                    }
                )
        # Live-update and deferral paths commit here (the reset path already
        # committed before its reset, and assigning again is a no-op).
        saved: bool | None = True
        if not intent_recorded:
            saved = await record_default_intent(session_key, not effort and not overlay_cleared)
            if not saved and not pushed_live:
                return refuse_restoring_override(saved)
        slot.reasoning_effort = effort
        normalized = normalize_legacy_model()
        if not saved:
            # The live session already runs the pick, so the slot keeps that value
            # while the response reports the durable intent still owed.
            state.push_slots_update()
            return _effort_intent_unsaved(slot, effort, normalized_model=normalized.get("model"))
    slot._effort_intent_owed = False
    state.push_slots_update()
    return web.json_response({"ok": True, "reasoning_effort": effort, **normalized})


async def api_chat_slot_ultracode(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/ultracode — turn the session's ultracode switch on or off.

    Body: ``{"enabled": true | false}``; answers ``{"ok": true, "ultracode": <read-back>}``.
    The refusals are :func:`_write_session_switch`'s, with the ``ultracode_`` codes.
    """
    return await _write_session_switch(
        request,
        "ultracode",
        "ultracode",
        read=lambda provider: provider.ultracode_enabled(),
        write=lambda provider, enabled: provider.set_ultracode(enabled),
    )


async def api_chat_slot_fast_mode(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/fast — turn the session's Fast mode switch on or off.

    Body: ``{"enabled": true | false}``; answers ``{"ok": true, "fast": <read-back>}``.
    The refusals are :func:`_write_session_switch`'s, with the ``fast_`` codes.
    """
    return await _write_session_switch(
        request,
        "fast",
        "Fast mode",
        read=lambda provider: provider.fast_mode_enabled(),
        write=lambda provider, enabled: provider.set_fast_mode(enabled),
    )


def _approve_for_me_field(state: DashboardState, slot: _ChatSlot, backend: str) -> dict[str, bool]:
    """``{"approve_for_me": <on?>}`` for a slot on a harness that offers it, else ``{}``.

    Read from the dashboard's own record, not the session: the engine lists the
    option only after an opted-in restart, and the slot has no session in between.
    """
    if (
        backend not in ACP_BACKENDS_APPROVE_FOR_ME
        or not approve_for_me_offered_on(slot)
        or not approve_for_me_engine_supported()
    ):
        # An engine not known to send its verdict to Crew gets no row at all.
        return {}
    return {"approve_for_me": approve_for_me_reads_on(state, effective_session_key(slot))}


async def _restart_for_approve_for_me(
    state: DashboardState, slot: _ChatSlot, session_key: str
) -> bool:
    """End the slot's idle session so its next start picks up the engine switch.

    False when a turn holds the session, or waits on a person (a permission card,
    a question): nothing is torn down and nothing pending is answered for them.
    The record is already changed, so the turn's next settle holds the option off,
    every verdict the engine sends meanwhile takes the card (the owner check now says
    no), and the restart waits for an idle session.
    """
    provider = state.sessions.get_provider(session_key)
    if provider is None:
        return True
    if _switch_target_busy(state, slot, session_key, provider):
        return False
    return bool(await _reset_slot_session(state, slot, session_key, skip_if_busy=True))


async def end_approve_for_me(state: DashboardState, slots: list[_ChatSlot]) -> None:
    """Drop the person's approve-for-me on *slots* after another mode was picked.

    The key leaves the record at once. An idle session is restarted, so its next
    start runs with the engine switch off. A busy one is held off by the turn's
    owner check: its next call KAS approves cancels the turn, and the next turn
    writes the option off.
    """
    keys = approve_for_me_keys(state)
    for slot in slots:
        session_key = effective_session_key(slot)
        if session_key not in keys:
            continue
        keys.discard(session_key)
        try:
            await _restart_for_approve_for_me(state, slot, session_key)
        except Exception as exc:
            logger.warning(
                "approve-for-me restart failed for slot %s: %s: %s",
                slot.key,
                type(exc).__name__,
                exc,
            )


async def api_chat_slot_approve_for_me(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/approve-for-me — let KAS approve calls on its own.

    Body: ``{"enabled": true | false}``; answers ``{"ok": true, "approve_for_me":
    <bool>}``. Dashboard owner only. Only a slot on a harness in
    ``ACP_BACKENDS_APPROVE_FOR_ME`` takes it (409 ``approve_for_me_not_offered``), and
    never one a person does not drive from the dashboard: app-owned, unattended,
    cron, or tied to a messaging channel (409 ``approve_for_me_not_owners``).
    Turning it on is checked against the ``approval_modes`` policy (403
    ``mode_disabled_by_policy``) and first puts the slot back to Normal. Either way
    the slot's idle session is restarted so the next message starts its engine with
    the switch set; a running turn refuses with 409 ``turn_in_flight`` and changes
    nothing.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if slot is None:
        return slot_not_found()
    denied = await deny_session_approval_caller(request, "slot_approve_for_me")
    if denied is not None:
        return denied
    if request.get("app") or request.get("internal_auth") is True:
        return web.json_response(
            {
                "error": "approve for me is set from the dashboard only",
                "code": "approve_for_me_owner_only",
            },
            status=403,
        )
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        return web.json_response(
            {"error": "enabled must be true or false", "code": "invalid_value"}, status=400
        )
    mode = APPROVE_FOR_ME_MODE
    async with contextlib.AsyncExitStack() as _stack:
        await _stack.enter_async_context(slot._lock)
        if _slot_replaced_while_queued(state, slot, name, request, "chat.slot_approve_for_me"):
            return slot_not_found()
        session_key = effective_session_key(slot)
        await _stack.enter_async_context(_slot_switch_session_lock(session_key))
        if _slot_replaced_while_queued(state, slot, name, request, "chat.slot_approve_for_me"):
            return slot_not_found()
        provider = state.sessions.get_provider(session_key)
        if isinstance(provider, AcpProvider):
            backend = provider.capabilities.backend
        else:
            backend = await _configured_backend_for_slot(slot)
        if enabled and not approve_for_me_offered_on(slot):
            # Turns no person at the dashboard sent keep Crew's own checks.
            return web.json_response(
                {
                    "error": "approve for me is offered only on a chat you drive here",
                    "code": "approve_for_me_not_owners",
                },
                status=409,
            )
        if backend not in ACP_BACKENDS_APPROVE_FOR_ME or (
            enabled and not await asyncio.to_thread(approve_for_me_refresh_engine_support)
        ):
            # Only an engine known to send its verdict to Crew may hold it on.
            return web.json_response(
                {
                    "error": "this session offers no approve-for-me",
                    "code": "approve_for_me_not_offered",
                },
                status=409,
            )
        if enabled and (
            not approve_for_me_permitted()
            or approve_for_me_profile_binds(session_key, getattr(slot, "agent", "") or "")
        ):
            # The policy does not list the mode, or a governance profile binds the
            # session and may narrow it: the owner check would hold it off anyway.
            return _deny_approval_mode(
                caller="dashboard:approve_for_me",
                operation=f"mode_change:{mode}",
                mode=mode,
                resource=slot.key,
            )
        keys = approve_for_me_keys(state)
        was_on = session_key in keys
        if was_on == enabled:
            # Already so: nothing changes, so a running turn is no reason to refuse.
            return web.json_response({"ok": True, "approve_for_me": enabled})
        if _switch_target_busy(state, slot, session_key, provider):
            return web.json_response(
                {"error": "a turn is in flight", "code": "turn_in_flight"}, status=409
            )
        if enabled:
            answer = await apply_approval_mode(
                state,
                mode="normal",
                slot=slot,
                slot_key=slot.key,
                request_app="",
                cron_creator="",
                audit_caller=lambda label: label,
            )
            if answer.status != 200:
                return answer
            keys.add(session_key)
        else:
            keys.discard(session_key)
        if not await _restart_for_approve_for_me(state, slot, session_key):
            # A turn started after the busy check: put the record back.
            if was_on:
                keys.add(session_key)
            else:
                keys.discard(session_key)
            return web.json_response(
                {"error": "a turn is in flight", "code": "turn_in_flight"}, status=409
            )
    try:
        sel().log_api_access(
            caller="dashboard:approve_for_me",
            operation=f"mode_change:{mode}",
            outcome="enabled" if enabled else "disabled",
            resources=slot.key,
        )
    except Exception:
        logger.warning("SEL audit failed for approve-for-me change", exc_info=True)
    state.push_slots_update()
    logger.info("Slot %s approve_for_me switched to %s", name, enabled)
    return web.json_response({"ok": True, "approve_for_me": enabled})


async def _write_session_switch(
    request: web.Request,
    key: str,
    switch_name: str,
    *,
    read: Callable[[LLMProvider], bool | None],
    write: Callable[[LLMProvider, bool], Awaitable[bool | None]],
) -> web.Response:
    """Write one of the session's on/off switches and answer the value it reads back.

    *key* is the switch's ``selection-capabilities`` key, the key of the answer and
    the prefix of its error codes; *switch_name* is how an error names it. *read*
    and *write* reach the provider's reader and writer. Only a live session that
    advertises the switch takes it (409 ``<key>_not_offered``), and never while a
    turn runs (409 ``turn_in_flight``): the write's response wait would read the
    harness's output beside the streaming turn, the hazard the effort route defers
    its live push for. A write the adapter rejects answers 502 ``<key>_refused``.
    Nothing is persisted here: the switch is the adapter's own per-session state.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if slot is None:
        return slot_not_found()
    denied = deny_app_slot_access(request.get("app", ""), slot, name, f"slot_{key}")
    if denied is not None:
        return denied
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        return web.json_response(
            {"error": "enabled must be true or false", "code": "invalid_value"}, status=400
        )
    # The lock order and re-checks of the effort route: a model switch on the same
    # session must not run its own config write beside this one.
    async with contextlib.AsyncExitStack() as _stack:
        await _stack.enter_async_context(slot._lock)
        if _slot_replaced_while_queued(state, slot, name, request, f"chat.slot_{key}"):
            return slot_not_found()
        session_key = effective_session_key(slot)
        await _stack.enter_async_context(_slot_switch_session_lock(session_key))
        if _slot_replaced_while_queued(state, slot, name, request, f"chat.slot_{key}"):
            return slot_not_found()
        denied = _app_cancel_denied(request, slot, f"chat.slot_{key}", session_key)
        if denied is not None:
            return denied
        provider = state.sessions.get_provider(session_key)
        if not isinstance(provider, LLMProvider) or read(provider) is None:
            return web.json_response(
                {
                    "error": f"this session offers no {switch_name} switch",
                    "code": f"{key}_not_offered",
                },
                status=409,
            )
        # The write also holds the session's turn lease, taken without waiting: a
        # channel turn holds it before its prompt is in flight, which the busy probe
        # cannot see, and that prompt would read the stdout this write's reply wait reads.
        lease_key = state.sessions._fold_key(session_key)
        if _switch_target_busy(state, slot, session_key, provider) or not (
            await state.sessions.try_acquire(lease_key)
        ):
            return web.json_response(
                {"error": "a turn is in flight", "code": "turn_in_flight"}, status=409
            )
        try:
            value = await write(provider, enabled)
        except Exception as exc:
            logger.warning(
                "%s write (%s) failed for slot %s: %s: %s",
                key,
                enabled,
                name,
                type(exc).__name__,
                exc,
            )
            # A fixed sentence: the exception text can carry agent-authored words,
            # a path or a host. The log line above keeps the detail for the owner.
            return web.json_response(
                {
                    "error": f"The agent did not accept the {switch_name} change.",
                    "code": f"{key}_refused",
                },
                status=502,
            )
        finally:
            state.sessions.release(lease_key, wake_queue=True)
    logger.info("Slot %s %s switched to %s", name, key, value)
    return web.json_response({"ok": True, key: value})
