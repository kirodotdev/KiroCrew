"""Model selection: ``POST /api/chat/slots/{slot}/model``, the bulk
``POST /api/chat/slots/model``, and the live switch the per-slot route tries before
it relaunches the session.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from collections.abc import Iterable

    from kiro_crew.dashboard.chat_handlers import (
        _TEARDOWN_INCOMPLETE_WARNING,
        JEV_ROUTE_MODEL,
        MODEL_NAMESPACE_ACP,
        AcpModelUnavailable,
        AcpProvider,
        DashboardState,
        KiroCrewConfig,
        LLMProvider,
        _app_cancel_denied,
        _append_session_switch_notice,
        _ChatSlot,
        _context_usage_payload,
        _history_key_for,
        _normalize_model,
        _owner_denial_response,
        _reset_slot_session,
        _reset_slot_session_or_warn,
        _slot_replaced_while_queued,
        _slot_switch_session_lock,
        _subagents_attached_response,
        _switch_target_busy,
        _sync_served_model,
        capabilities_of,
        deny_app_slot_access,
        effective_session_key,
        is_owner_dashboard_request,
        logger,
        model_registry,
        pick_epoch_host,
        provider_model_pin_refused,
        read_bounded_json,
        relay_archive_refusal,
        sel,
        slot_not_found,
        subagents_attached_async,
    )


def _model_rejected_reason(
    model_name: str,
    backend: str | None = None,
    *,
    session_keys: Iterable[str | None] | None = None,
) -> str | None:
    """Reason to reject ``model_name`` for the harness that will run it, or None.

    The dashboard model dropdown falls back to canonical registry keys (e.g.
    ``fable-5-1m``) when /api/models is unavailable (gateway restart / kiro-cli
    cold-start timeout). On a harness whose model-id namespace has no id for
    such a key it is a DISPLAY identifier (kiro-cli answers -32603 "model not
    available"), and persisting one into ``slot.model`` breaks the next turn.
    This guard is defense-in-depth behind the frontend's auto-only fallback: a
    stale client, a direct API call, or the openai-compat path can never persist
    one. ``auto`` and ``""`` (provider default) always pass.

    Whether a canonical key is display-only is a property of the backend's
    MODEL-ID NAMESPACE (``capabilities_for(backend).model_id_namespace``), the
    same field :func:`_wire_model_id` reads to translate it: on a provider
    namespace whose registry row carries an id (``claude_code`` maps
    ``fable-5-1m`` to its Bedrock id) the key is translated there, so it passes
    here. ``agent.provider`` cannot answer this -- its only value is ``acp`` for
    every harness.

    *backend* lets a caller that already resolved the harness supply it, so this
    adds no config read of its own: ``KiroCrewConfig.load()`` deep-copies the
    validated dict even on a cache hit, and on a miss it reads and validates
    files. Omit it and the harness is resolved from config -- only for a
    canonical key, the one value whose answer depends on it. *session_keys*
    then names the sessions the pick lands on, each routed through the same
    member-aware gate the provider factory uses; the key must pass on every one
    of them. With neither, the configured default harness decides.
    """
    if not model_name or model_name == "auto":
        return None
    if not model_registry.is_canonical_key(model_name):
        return None
    if backend is not None:
        backends: list[str] = [backend]
    else:
        from kiro_crew.crewmate_harness import CrewmateHarnessUnavailable, crewmate_backend_pin
        from kiro_crew.members import select_provider_backend

        cfg = None
        try:
            cfg = KiroCrewConfig.load()
            default_backend = cfg.agent.acp_backend or ""
            member_backend = cfg.agent.member_acp_backend or ""
        except Exception:  # pragma: no cover - config load is resilient
            default_backend, member_backend = "", ""
        keys = list(session_keys or ())
        try:
            backends = (
                [
                    select_provider_backend(
                        k,
                        member_backend,
                        default_backend,
                        crew_backend=crewmate_backend_pin(cfg, k) if cfg is not None else None,
                    )
                    for k in keys
                ]
                if keys
                else [default_backend]
            )
        except CrewmateHarnessUnavailable as exc:
            # A crewmate whose pinned harness cannot start takes no model at all.
            return str(exc)
    from kiro_crew.agent_sdk.capabilities import capabilities_for
    from kiro_crew.crewmate_harness import harness_label

    entry = model_registry.get_entry(model_name) or {}
    for name in dict.fromkeys(backends):
        namespace = capabilities_for(name).model_id_namespace
        if namespace != MODEL_NAMESPACE_ACP and entry.get("providers", {}).get(namespace):
            continue
        # The harness by the name the dashboard shows it under, never the raw id.
        return (
            f"{model_name!r} is a display-only model identifier the "
            f"{harness_label(name or 'kiro')} harness does not accept; "
            f"select a listed model or 'auto'."
        )
    return None


def _is_jev_route_pick(raw: object) -> bool:
    """Whether a request body's ``model`` is the "Auto (Jev)" sentinel.

    Exact identity on a stripped string. A near-miss spelling is NOT this entry
    and falls through to the ordinary model path, where the guard refuses it --
    resolving it loosely would turn a typo into paid third-party egress.
    """
    return isinstance(raw, str) and raw.strip() == JEV_ROUTE_MODEL


def _wire_model_id(provider: AcpProvider, model_name: str) -> str:
    """Translate a canonical model key into the id THIS backend accepts.

    ``slot.model`` holds a canonical/wire value while ``session/set_model`` only
    accepts the backend's own ids — two namespaces. Mirrors the normalisation the
    warm-pool post-claim switch does in ``SessionManager``: a backend on the
    native ``acp`` namespace wants the bare dotted id via ``to_acp_id`` (which
    translates canonical keys and passes kiro's own ids through unchanged), while
    one on its own provider namespace wants that namespace's id (for
    claude-agent-acp, ``global.anthropic.*``).

    Which namespace is asked as a CAPABILITY, not read off the harness's name:
    ``SessionCapabilities.model_id_namespace``. The same field also answers
    whether "provider default" is expressible, because that is a property of the
    namespace — the native one carries the real id ``auto`` and a provider
    namespace has no id meaning "choose for me".

    Returns "" when the change cannot be expressed as a ``set_model`` on this
    backend, which tells the caller to fall back to a session reset.
    """
    # The dashboard sends "" for Auto, but the literal "auto" also passes the
    # guard (stale clients / direct API calls), so both mean "provider default".
    is_default = model_name in ("", "auto")
    namespace = capabilities_of(provider).model_id_namespace
    if namespace != MODEL_NAMESPACE_ACP:
        # No id on this namespace means "let the server choose", so returning to
        # default needs a reset.
        return "" if is_default else model_registry.to_provider_id(model_name, namespace)
    if is_default:
        # kiro DOES express Auto as a real model id — but only switch to it when
        # this session's backend actually advertised it.
        advertised = {m.get("modelId", "") for m in provider.available_models()}
        return "auto" if "auto" in advertised else ""
    return model_registry.to_acp_id(model_name)


async def _reapply_effort_after_live_switch(
    name: str, slot: _ChatSlot, provider: AcpProvider
) -> bool:
    """Re-apply the slot's reasoning effort to the model we just switched to.

    The kiro effort overlay is written before every (re)spawn, so a cold start
    picks the level up for free. An in-place switch never respawns, so without
    this the new model would run at its own default while the UI still reports
    the slot's level. Pushes it live through the same provider calls
    ``api_chat_slot_reasoning_effort`` uses.

    Returns False to ask the caller for a reset, which re-applies effort through
    the provider factory instead.
    """
    try:
        # Through the class: the shared body the provider's own set_model runs.
        return await AcpProvider.reapply_live_effort(provider, slot.reasoning_effort)
    except Exception as exc:
        logger.warning(
            "Effort re-apply after live model switch failed for slot %s: %s: %s"
            " — falling back to reset",
            name,
            type(exc).__name__,
            exc,
        )
        return False


async def _try_live_model_switch(
    name: str, slot: _ChatSlot, provider: LLMProvider | None, model_name: str
) -> bool:
    """Apply a model change to the LIVE session instead of tearing it down.

    ``session/set_model`` switches the model on a running kiro-cli session.
    Verified against kiro-cli 2.15.1: acked synchronously, carries the existing
    conversation across the switch (including across vendors), sticks over
    subsequent turns, and switches back. That makes a session reset
    unnecessary for an idle slot — and the reset is expensive twice over, since
    it kills the whole process tree now AND forces the next message to
    cold-start and replay a compressed transcript.

    Returns True when the live session owns *model_name*. False means the caller
    must fall back to a reset — including when there is no live session at all,
    where the reset is an O(1) no-op teardown but still routes through
    ``_reset_slot_session``'s pending-wait cleanup.
    """
    if not isinstance(provider, AcpProvider):
        return False
    if provider.has_active_turn():
        # Same hazard api_chat_slot_reasoning_effort documents: awaiting a
        # response mid-turn races the streaming prompt loop on stdout for the
        # non-multiplexed client. api_chat_slot_model defers the pick to the
        # turn boundary before reaching here (its check and this call share
        # one no-await window), so this is defense in depth for any future
        # caller — decline the live switch rather than race the stream.
        return False
    wire = _wire_model_id(provider, model_name)
    if not wire:
        return False
    try:
        await provider.client.set_model(wire)
    except AcpModelUnavailable:
        # NOT a "the call didn't land" failure, so the reset fallback below is
        # the wrong recovery: it would tear down the live conversation and then
        # cold-start on a DIFFERENT model while the caller reported success.
        # Propagate so the handler answers 4xx and the slot keeps its old model.
        raise
    except Exception as exc:
        logger.warning(
            "Live set_model(%s) failed for slot %s: %s: %s — falling back to reset",
            wire,
            name,
            type(exc).__name__,
            exc,
        )
        return False
    refused = provider.model_pin_refused if provider_model_pin_refused(provider) else ""
    if refused:
        # The shared-runtime handle applies a model through its config-option
        # ladder NON-strictly: an adapter that refuses every spelling leaves the
        # session on its default and records the refused id instead of raising
        # (the dedicated AcpClient raised above). The user named this exact
        # model, so answer it the way the dedicated runtime does: same error,
        # same 4xx, slot keeps its old model. The handle resets the record at
        # the start of every call, so this is THIS pick's verdict, not a stale
        # one. On a pair-id harness the advertised list is the entitlement, so
        # a bare id the pairs serve being refused is the adapter contradicting
        # its own list, which selects the adapter-mismatch wording.
        raise AcpModelUnavailable.for_recorded_refusal(
            refused, provider.available_models(), backend=provider.client.backend
        )
    # Client-scoped explicit-pick epoch. The pick GENERATION above is
    # slot-local, but two slots can drive one wire session (a channel-born
    # slot and its dashboard alias share `effective_session_key`), and the
    # refusal-fallback restore guard on another slot cannot see this slot's
    # generation. The shared CLIENT is the one object every alias holds, so
    # an explicit pick that lands on the live session stamps it here and the
    # restore compares against its swap-time snapshot. pick_epoch_host
    # resolves the SAME innermost object on both ends — this handler holds
    # the AcpProvider wrapper while the runner can hold the wrapped client.
    # Stamped IMMEDIATELY after set_model lands and BEFORE the effort
    # reapply: the wire session serves the picked model from this point, so
    # a sibling slot's refusal restore can already observe the pick live. If
    # the reapply below fails (reset fallback), a missing stamp would let
    # that restore treat the landed pick as its own swap and overwrite it.
    try:
        _host = pick_epoch_host(provider)
        _host._explicit_pick_epoch = getattr(_host, "_explicit_pick_epoch", 0) + 1
    except Exception:  # pragma: no cover - a frozen/slotted stub client
        pass
    if not await _reapply_effort_after_live_switch(name, slot, provider):
        return False
    logger.info("Slot %s model switched live to %r (session preserved)", name, wire)
    return True


def _broadcast_context_reset(state: "DashboardState", slot_key: str, provider: Any) -> None:
    """Push one ``context_usage`` event so the meter updates on a model switch.

    Without this the frontend keeps the previous model's stored ``{used,
    window}`` until the next turn emits an event. ``reset: true`` tells the
    ``sseContextUsage`` reducer it may REPLACE or DELETE the stored token entry
    (a frame WITHOUT ``reset`` never deletes, so the backend sets ``reset``
    whenever it has no real counts to send). With a live provider the payload
    carries the freshly rebased stats from ``set_model``; without one (the
    cold-reset path) it carries no tokens, so the reducer keeps the stored
    ``used`` and the UI derives the percentage against the new model's window.
    Best-effort: a broadcast failure must not fail the switch.

    When the new model's window is unknown, the frame carries ``used_tokens``
    (if the live rebase knows it) but neither ``pct`` nor ``window_tokens``.
    ``used`` is roughly model-independent and the transcript is unchanged across
    a switch, so the meter derives ``used / new-window`` against its own
    model-derived window — a correct reading, not the old model's percentage
    pinned beside a window that does not describe it. A rebase that DID
    resolve the new window ships the full ``pct``/``used``/``window`` triple and
    is not a reset. When ``used`` is genuinely unknown (a percentage-only
    backend) nothing is carried, and the reducer clears the reading rather than
    derive a wrong number.
    """
    try:
        if provider is not None:
            payload = _context_usage_payload(slot_key, provider)
        else:
            # No provider on the cold-reset path: no measurement to send, so the
            # reducer keeps its stored ``used`` and derives against the new
            # window.
            payload = {"slot": slot_key}
        payload["reset"] = True
        if not payload.get("window_tokens"):
            # New window unknown: a pct computed against the OLD window must not
            # pair with the NEW model's window downstream, so drop both the pct
            # and the (zero) window. Keep ``used_tokens`` when the provider has
            # it — the meter derives the percentage from it against the new
            # window. ``_context_usage_payload`` only sets ``used_tokens``
            # alongside ``window_tokens``, so surface it here from the provider.
            payload.pop("pct", None)
            payload.pop("window_tokens", None)
            payload.pop("used_tokens", None)
            used = 0
            if provider is not None and hasattr(provider, "context_used_tokens"):
                try:
                    used = int(provider.context_used_tokens() or 0)
                except Exception:
                    used = 0
            if used > 0:
                payload["used_tokens"] = used
        state.broadcast_context_usage(slot_key, payload)
    except Exception:
        logger.exception("Failed to broadcast context_usage reset for slot %s", slot_key)


async def api_chat_slot_model(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/model — set model for a chat slot.

    Prefers an in-place ``session/set_model`` on the running session and only
    resets when that is impossible (no ACP provider, an unrepresentable
    target, or the live call failing). A turn in flight takes neither path
    now: the pick is committed and answered ``deferred: true``, and the
    boundary of that turn applies it (``apply_deferred_model_switch``) — the
    reset fallback would otherwise tear down the streaming turn mid-stream.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    denied = deny_app_slot_access(request.get("app", ""), slot, name, "slot_model")
    if denied is not None:
        return denied
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    # The "Auto (Jev)" entry is resolved HERE and nowhere else: every reader below
    # -- the guard, the live switch, the reset, the session allocation, the
    # composer chip -- takes the id it produces, which is the plain ``auto`` the
    # session actually runs on until a tier is answered. The choice itself
    # survives as ``slot.jev_route`` alone.
    jev_route = _is_jev_route_pick(body.get("model", ""))
    if jev_route and not is_owner_dashboard_request(request):
        # Arming routing is an OWNER action: it hands the per-turn model choice to
        # the oracle, and a dear tier spends the owner's credential on a model the
        # caller never named. The cross-app gate above cannot carry that decision --
        # it admits an allow-listed non-owner whose ``app`` claim is empty -- so the
        # arm is gated on the same predicate the decision seam's consent route uses.
        # Only the arm is refused: a plain pick stays open to the same caller.
        sel().log_api_access(
            caller="non-owner",
            operation="chat_slot_model_jev_route",
            outcome="denied",
            source="owner_only",
            resources=f"slot={name}",
            error="non-owner identity rejected",
        )
        return _owner_denial_response(request, "arming Jev routing is owner-only")
    model_name = "auto" if jev_route else _normalize_model(body.get("model", ""))
    # Judged on THIS slot's harness: a member DM runs on agent.member_acp_backend.
    reason = _model_rejected_reason(model_name, session_keys=(effective_session_key(slot),))
    if reason:
        logger.warning("Slot %s model rejected: %s", name, reason)
        return web.json_response({"error": reason}, status=400)
    refusal = relay_archive_refusal(slot)
    if refusal is not None:
        return refusal
    # Three locks, always in this order (slot._lock, then the session lock,
    # then _model_pick_lock -- see _slot_switch_session_lock; the bulk handler
    # nests them the same way and nothing takes them in the opposite order).
    # An ExitStack because the session lock's KEY is only known after the
    # in-lock read below:
    # the session lock — two switches arriving through DIFFERENT alias slots
    # resolve onto ONE session but take DISJOINT per-slot locks, so without
    # it neither waits for the other and both reset this same session.
    #
    # slot._lock — same serialization as the agent, effort and workspace
    # switch handlers: the awaits below yield the event loop, and an
    # interleaved second switch could otherwise observe (or write)
    # intermediate state — two racing switches would each commit and reset
    # against the other's half-applied session. Holding it across the
    # provider RPC mirrors the effort handler holding it across change_effort.
    #
    # slot._model_pick_lock — one pick transaction at a time against the
    # model-fallback machinery: the fallback
    # swap and restore probe in chat_runner hold it across their own
    # set_model awaits, and a pick landing inside that window could be
    # overwritten by the swap (or roll back the swap's state). Serialising
    # the whole check → mutate → switch → rollback span makes each pick
    # atomic; the CAS rollback below stays as a backstop against any writer
    # outside both locks.
    #
    # There is deliberately NO unlocked no-op fast path: a serialized switch
    # holding the locks commits slot.model before its RPC and rolls it back
    # on AcpModelUnavailable, so an unlocked equality read could match that
    # transient value and report "already on X" for a model that is then
    # rolled back.
    async with contextlib.AsyncExitStack() as _stack:
        await _stack.enter_async_context(slot._lock)
        # Re-authorize after the await above (see _slot_replaced_while_queued):
        # ``name`` can be recreated for a different app while this request
        # queued, and every read of ``slot`` below would be of the stale one.
        if _slot_replaced_while_queued(state, slot, name, request, "chat.slot_model"):
            return slot_not_found()
        # The session the switch will probe and, on the reset path, tear
        # down. ``effective_session_key``, never ``_history_key_for`` (the
        # reload handler's rule): a channel- or cron-born slot runs its turns
        # under its linked key, and the dashboard-prefixed spelling names a
        # session that never existed — the busy probe would see nothing and
        # the reset would "succeed" against nothing while the live process
        # kept the old model. Resolved INSIDE the lock, not before it: the
        # binding can land while this request waits on the lock (a cron or
        # workflow slot is linked when its first result is injected), and a
        # key read before the wait would then name the wrong session.
        session_key = effective_session_key(slot)
        # Now serialize against every OTHER alias slot on this same session.
        # slot._lock is created per _ChatSlot and so is DISJOINT across
        # aliases. Keyed on the value resolved just above -- the same one the
        # probe and reset below use -- so the lock provably guards them even if
        # a binding landed while this request waited on slot._lock (see
        # _slot_switch_session_lock).
        await _stack.enter_async_context(_slot_switch_session_lock(session_key))
        await _stack.enter_async_context(slot._model_pick_lock)
        # Two more lock-acquisition awaits, one re-check: nothing reads
        # ``slot`` between them, so a check after the last one covers both.
        if _slot_replaced_while_queued(state, slot, name, request, "chat.slot_model"):
            return slot_not_found()
        # App isolation on the SESSION, not just the slot (the cancel
        # routes' policy): slot ownership does not imply ownership of a
        # linked channel session, so an app caller may not switch the model
        # a channel thread runs on. Denied as an indistinguishable 404.
        denied = _app_cancel_denied(request, slot, "chat.slot_model", session_key)
        if denied is not None:
            return denied
        # The routing flag is committed on each SUCCESS path and nowhere else -- see
        # the two writes below. Nothing is written here, because the busy check
        # between this line and the transaction answers 409 without a rollback: a
        # pick that was refused must not change what the next turn runs on.
        #
        # Checked INSIDE the locks only: a serialized predecessor targeting the
        # same model may have committed while this request waited, and acting
        # again would tear down the session that predecessor just set up.
        if (
            slot.model == model_name
            and not slot._active_fallback_model
            and not slot._refusal_fallback_primary
        ):
            # Same-value pick: nothing to switch, but the user's EXPLICIT
            # affirmation of this model must still be recorded — the fallback
            # restore probe reads the pick generation, and without the bump a user
            # who deliberately picks the very model the session fell back to (or
            # that the backfill wrote) would have their choice silently overridden
            # by the next restore probe.
            #
            # NOT taken while a fallback is actively serving the session: the pin
            # may equal the displayed primary while the wire model is the
            # fallback, so "nothing to switch" is false — the normal live-switch
            # path below must run so the pick actually moves the session (an
            # early return here strands the session on the fallback while usage
            # is attributed to the primary). The
            # pick-the-fallback-itself case also flows through the live path,
            # where the switch is a harmless same-model set and the pick-gen bump
            # still protects the choice from the restore probe.
            # The slot-local bump alone is invisible ACROSS aliases: two slots
            # can drive one wire session, and the other slot's refusal-fallback
            # restore compares the shared CLIENT's epoch, not this slot's
            # generation. A same-value pick is still an explicit pick, so stamp
            # the shared epoch exactly as the live-switch success path does —
            # otherwise an alias pinned to the fallback candidate re-picks it,
            # takes this shortcut, and the originating slot's restore silently
            # undoes the choice.
            _provider = state.sessions.get_provider(session_key)
            try:
                _host = pick_epoch_host(_provider)
                _host._explicit_pick_epoch = getattr(_host, "_explicit_pick_epoch", 0) + 1
            except Exception:  # pragma: no cover - a frozen/slotted stub client
                pass
            slot._model_pick_gen += 1
            # The one place the flag is recorded on this path, and the reason it is
            # not written before the branch: an unpinned slot picking "Auto (Jev)"
            # resolves to the ``auto`` it already holds and lands HERE, so a write
            # placed only in the transaction below would never run for the
            # commonest case. This shortcut is a success, so committing is correct.
            slot.jev_route = jev_route
            state.push_slots_update()
            return web.json_response({"ok": True, "model": model_name, "jev_route": jev_route})
        provider = state.sessions.get_provider(session_key)
        _busy = _switch_target_busy(state, slot, session_key, provider)
        if _busy:
            # Never tear down an in-flight turn: _try_live_model_switch
            # declines a mid-turn live switch (awaiting the backend's answer
            # would race the streaming turn's read loop on the same stdout),
            # so falling through would take the reset fallback and kill the
            # streaming turn. The pick is committed NOW and applied at the
            # turn boundary instead — the project chip's contract, and what
            # every backend's own mid-turn pick does (KAS accepts a mid-turn
            # model option, keeps the running turn on its model, and runs the
            # pick from the next turn). The turn in flight finishes on the
            # model it started on; ``apply_deferred_model_switch`` moves the
            # session when that turn ends (live where the harness allows, by
            # the deferred reset otherwise), and the next turn's admission is
            # its backstop. This holds for EVERY provider class with an active
            # turn, not only the ACP one that could go live: the boundary
            # decides the mechanism, so there is nothing a 409 would protect.
            # A pick that can only land through a session RESET -- the live
            # switch cannot express it (a non-ACP provider, or an id this
            # backend has no set_model value for, e.g. Auto on a
            # provider-namespace backend) -- cannot be deferred while a
            # sub-agent is attached. The boundary helper will not arm a reset
            # under attached children (it would tear down the runtime they run
            # on), so such a pick would never land: the turn would keep running
            # on the old model while the user was told it applies next. Refuse
            # it exactly as the idle path does below, committing nothing, rather
            # than report a deferral that cannot happen. A pick the live switch
            # CAN express is not blocked -- the boundary lands it live, with no
            # reset, so attached children do not stand in its way.
            _reset_only = not isinstance(provider, AcpProvider) or not _wire_model_id(
                provider, model_name
            )
            # A session whose turns a channel (Slack/Discord/Telegram/Teams) can
            # drive lands a deferred pick at a channel turn's end too, but only
            # LIVE (``land_model_pick_after_channel_turn``): a channel turn
            # cold-starts on the default model ladder, not on ``slot.model``, so
            # the reset fallback cannot land the pick there. A reset-only pick on
            # such a session keeps the pre-existing 409, the answer the
            # session_set_model pick gives a busy channel target, rather than
            # report a deferral the channel could keep serving the old model past.
            # Which sessions count is ``channel_drives_session``.
            # circular import: session_control imports dashboard modules at module level.
            from kiro_crew.dashboard.session_control import channel_drives_session

            if _reset_only and channel_drives_session(state, slot, session_key):
                return web.json_response(
                    {"error": "a turn is in flight", "code": "turn_in_flight"}, status=409
                )
            if _reset_only:
                children_409 = await _subagents_attached_response(
                    state, slot, session_key, "slot_model"
                )
                if children_409 is not None:
                    return children_409
            # Same commit as the idle transaction below (pin, routing flag,
            # pick generation), minus the live/reset step; the committed pin
            # is persisted by the next slot save, so a restart cold-starts on
            # it. The effort dropdown's defer-not-reset branch is the sibling.
            slot.model = model_name
            slot.jev_route = jev_route
            slot._model_pick_gen += 1
            # Stamp the shared client's explicit-pick epoch, as the same-value
            # shortcut and the live-switch success path do: the per-slot
            # generation above is invisible to a sibling alias, whose
            # refusal-fallback restore compares this epoch on the shared client.
            # Without it, before the boundary applies the pick, that restore
            # could treat the session as un-picked and move it off the pin.
            try:
                _host = pick_epoch_host(provider)
                _host._explicit_pick_epoch = getattr(_host, "_explicit_pick_epoch", 0) + 1
            except Exception:  # pragma: no cover - a frozen/slotted stub client
                pass
            slot._deferred_model_switch_key = session_key
            logger.info(
                "Slot %s model pick %r deferred: a turn is in flight on %s",
                name,
                model_name or "auto",
                session_key,
            )
            state.push_slots_update()
            return web.json_response(
                {"ok": True, "model": model_name, "jev_route": jev_route, "deferred": True}
            )
        prior_model = slot.model
        prior_pick_gen = slot._model_pick_gen
        prior_jev_route = slot.jev_route
        slot.model = model_name
        # Set on every pick, not only the Jev one: picking a concrete model is the
        # owner answering the very question the point asks, so it clears the flag
        # rather than leaving a routing that the next turn would apply over the
        # model they just chose. Inside the transaction, so ``_rollback_pick``
        # covers it.
        slot.jev_route = jev_route
        # Explicit user pick: bump the pick generation so the model-fallback
        # restore probe never overrides this choice (automatic backfill does NOT
        # bump it).
        slot._model_pick_gen += 1

        def _rollback_pick() -> None:
            """Undo this request's commit — model AND pick generation together.

            A refused or declined pick changed nothing, and leaving the bump in
            place would make the fallback restore probe read it as an explicit
            choice and silently abandon restoring the primary — the session
            would stay on the fallback with no card and no probe.

            COMPARE-AND-SWAP, not unconditional: both locks make an
            interleaved pick impossible, so this is a backstop against any
            writer outside them. Only restore when
            the state is still exactly ours (our bump, our model); the check
            and both writes are synchronous, so they are atomic on the event
            loop.
            """
            if slot._model_pick_gen == prior_pick_gen + 1 and slot.model == model_name:  # type: ignore[union-attr]
                slot.model = prior_model  # type: ignore[union-attr]
                slot._model_pick_gen = prior_pick_gen  # type: ignore[union-attr]
                # The routing choice is part of the same commit: a refused pick
                # changed nothing, and leaving the flag would route the next turn
                # for a request the caller was told had failed.
                slot.jev_route = prior_jev_route  # type: ignore[union-attr]

        def _live_serves_target(candidate: object) -> bool:
            """True when the live session's BACKEND-RESOLVED model already
            equals the requested wire id — the truth-based success exception.

            Not identity guessing: dispatch captures slot.model at its call
            site and registers the session only after provider.start(), so
            neither identity nor registration time proves anything; the
            served model does. A True here means slot.model is consistent
            with what actually runs — the partially-applied live switch
            (set_model landed, then the effort reapply failed) — so rollback
            would publish the OLD model over a live session running the NEW
            one, and a teardown would kill it. Defined once and consumed by
            BOTH the pre-reset busy re-check and the post-decline
            disambiguation, so the two spellings cannot diverge.
            """
            if not isinstance(candidate, AcpProvider):
                return False
            wire = _wire_model_id(candidate, model_name)
            if not wire:
                return False
            if candidate.served_model == wire:
                return True
            if wire == "auto":
                # AcpProvider.served_model collapses the "auto" sentinel to ""
                # on purpose (the fallback canary must never probe a model the
                # backend did not resolve), so a landed switch TO Auto is
                # invisible through it. The session client keeps the raw id —
                # after set_model("auto") lands the handle prefers that
                # explicit assignment — so read it unfiltered for this one
                # value (the same literal _wire_model_id hands out for Auto).
                # Any other non-match stays False (fail-closed).
                raw = getattr(candidate.client, "served_model", "")
                return str(raw or "").strip() == wire
            return False

        # Set on the reset path when the old session's teardown RAISED after
        # the pop: the switch is committed, the response carries an advisory
        # warning (agent-handler precedent via _reset_slot_session_or_warn).
        teardown_incomplete = False
        try:
            went_live = await _try_live_model_switch(name, slot, provider, model_name)
        except AcpModelUnavailable as exc:
            # The live session refused the pick as unavailable to this account.
            # Roll the slot back so the picker keeps showing what is actually
            # running, and answer 4xx — deliberately NOT the reset fallback
            # below, which would destroy the conversation and cold-start on a
            # DIFFERENT model while reporting success. Only the session that
            # owns the advertised list gets to make this call, so there is no
            # pre-emptive gate here to go stale. The rollback runs under the
            # locks, so no serialized successor can observe the transient value.
            _rollback_pick()
            logger.warning("Slot %s model rejected: %s", name, exc)
            return web.json_response({"error": str(exc), "code": "model_unavailable"}, status=400)
        if effective_session_key(slot) != session_key:
            # The slot was bound to a different session while the live switch
            # awaited its provider RPCs (a cron/workflow slot gets linked when
            # its first result is injected). Whatever set_model did landed on
            # a session the slot has stopped running on, and resetting the key this
            # request resolved would tear down (or "succeed" against) the
            # wrong session — either way committing would advertise the new
            # model over a session this handler never touched. Roll back and
            # answer 409; the retry resolves the current binding.
            _rollback_pick()
            return web.json_response(
                {"error": "slot session was rebound during the switch", "code": "session_rebound"},
                status=409,
            )
        if went_live:
            # The live session runs the pick; refresh the slot's served-model
            # cache from it so an inheriting chip ("auto") does not keep naming
            # the model the session was spawned with.
            _sync_served_model(slot, provider)
            _broadcast_context_reset(state, slot.key, provider)
        else:
            # LAST-INSTANT busy re-check — the invariant this handler rests on:
            # no destructive step may run while a turn can be live, so idleness
            # must be established within a NO-AWAIT window immediately before
            # the teardown, and the atomic skip_if_busy decline covers only that
            # microsecond residue. The
            # pre-check above is separated from this point by
            # _try_live_model_switch's provider RPCs (seconds on a slow
            # backend), so a send may have started — and even posted an
            # ask_question card — since it ran; _reset_slot_session clears
            # pending waits BEFORE its atomic decline (its docstring's safety
            # argument assumes a caller-side busy check microseconds old), so
            # entering it busy would falsely reject that turn's cards even
            # though the reset itself declines. Busy here → roll back and
            # answer the same 409 the pre-check gives.
            # Children guard, shared with reload/continue: the reset tears
            # down the runtime attached sub-agents run on, so a parent that is
            # idle but still has children (running, queued, or with a
            # completion event in flight) must refuse rather than discard
            # their work. Same probe block as api_chat_slot_reload; only the
            # rollback is added here because this handler committed first.
            children_409 = await _subagents_attached_response(
                state, slot, session_key, "slot_model"
            )
            if children_409 is not None:
                _rollback_pick()
                return children_409
            # Probed AFTER the children await, so this is the NO-AWAIT window
            # the reset needs; same predicate as the pre-check
            # (_switch_target_busy), so a sibling alias that began
            # cold-starting during either await is refused here too.
            recheck = state.sessions.get_provider(session_key)
            if _switch_target_busy(state, slot, session_key, recheck):
                if _live_serves_target(recheck):
                    # The turn that slipped in runs on a session that already
                    # serves the target (set_model landed before the effort
                    # reapply failed): slot.model is truthful, so report
                    # success without teardown — rolling back would publish
                    # the old model while the live turn streams under the new
                    # one.
                    logger.warning(
                        "Slot %s model switch: live session already serves the "
                        "target; skipping the reset under its in-flight turn",
                        name,
                    )
                    _sync_served_model(slot, recheck)
                    _broadcast_context_reset(state, slot.key, recheck)
                    state.push_slots_update()
                    return web.json_response(
                        {"ok": True, "model": model_name, "jev_route": jev_route}
                    )
                _rollback_pick()
                return web.json_response(
                    {"error": "a turn is in flight", "code": "turn_in_flight"}, status=409
                )
            logger.info(
                "Slot %s model switched to %r, resetting session", name, model_name or "auto"
            )
            # skip_if_busy: the has_active_turn() 409 above is a best-effort
            # fast path — message dispatch does not take slot._lock, so a turn
            # can start between that check and this reset. SessionManager.reset
            # evaluates busyness atomically with the session pop (the same
            # authoritative-backstop split api_chat_slot_reload documents), so
            # a turn that slipped into the window is declined here instead of
            # torn down mid-stream.
            reset_ok = await _reset_slot_session_or_warn(
                state, slot, session_key, switch_kind="model"
            )
            if reset_ok is None:
                # Teardown raised after the session pop: the switch is
                # COMMITTED (see _reset_slot_session_or_warn), so the answer
                # is the committed state with an advisory warning — never a
                # 500 that strands clients on the old value. Deliberately no
                # _rollback_pick(): rollback is only for the decline/409
                # paths, where nothing was torn down. NOT an early return:
                # the rebind guard below must still run, so a slot rebound
                # during the raising await answers the same rollback + 409 as
                # any other rebind.
                teardown_incomplete = True
            elif not reset_ok:
                # Disambiguate the decline FAIL-CLOSED — with one truth-based
                # exception checked first. Provider identity or registration
                # time cannot prove which model a live session runs: dispatch
                # captures slot.model at its call site but registers the
                # session only after a multi-second provider.start(), so a
                # session registered after the commit may still carry the old
                # model. A false 409 is retryable and costs nothing; a false
                # success strands a live session on the old model under the
                # new slot.model.
                busy_provider = state.sessions.get_provider(session_key)
                live_serves_target = _live_serves_target(busy_provider)
                if live_serves_target:
                    went_live = True
                    # See _live_serves_target: slot.model is consistent with
                    # what actually runs, so success without teardown is the
                    # truthful answer; the un-pushed effort override is
                    # already persisted on the slot and applies on the next
                    # cold start (same degradation the effort handler's defer
                    # branch accepts).
                    logger.warning(
                        "Slot %s model switch: live session already serves the "
                        "target; declined reset left it in place (effort "
                        "override, if any, applies on the next cold start)",
                        name,
                    )
                if not live_serves_target and isinstance(busy_provider, LLMProvider):
                    if busy_provider.has_active_turn():
                        # A turn slipped in: roll back the commit and answer
                        # the same 409 the fast path gives, leaving the turn
                        # running whichever model it captured.
                        _rollback_pick()
                        return web.json_response(
                            {"error": "a turn is in flight", "code": "turn_in_flight"},
                            status=409,
                        )
                    # A live IDLE session declined the reset (its turn ended
                    # before this re-read). Reporting success would leave that
                    # process alive on whatever model it captured. Tearing
                    # down an idle session is always safe — history lives on
                    # the slot, not in the process — so retry once
                    # (api_chat_slot_reload's template for this exact race); a
                    # second decline means another turn is genuinely racing,
                    # which is the turn-in-flight case again.
                    reset_ok = await _reset_slot_session_or_warn(
                        state, slot, session_key, switch_kind="model"
                    )
                    if reset_ok is None:
                        # Retry teardown raised: same committed-switch answer
                        # as the first attempt, and same fall-through to the
                        # rebind guard below.
                        teardown_incomplete = True
                    elif not reset_ok:
                        _rollback_pick()
                        return web.json_response(
                            {"error": "a turn is in flight", "code": "turn_in_flight"},
                            status=409,
                        )
                # No live provider: there was no registered session to tear
                # down — the next message cold-starts under the new model,
                # which is exactly what the reset would have arranged.
            if effective_session_key(slot) != session_key:
                # Same check after the reset await(s) as after the live
                # switch: the session this request tore down is not
                # the slot's, so the commit would advertise the new model
                # over a session that never saw the switch. The teardown
                # itself was harmless (that session was idle and already
                # unbound); roll back the commit and let the retry resolve the
                # current binding.
                _rollback_pick()
                return web.json_response(
                    {
                        "error": "slot session was rebound during the switch",
                        "code": "session_rebound",
                    },
                    status=409,
                )
            _broadcast_context_reset(state, slot.key, None)
            # The reset path is committed: mark the boundary in the transcript,
            # inside the locks the same-value check above ran under. A live
            # in-place switch resets nothing and appends nothing.
            if not went_live:
                await _append_session_switch_notice(slot, "model", model_name or "auto")
    state.push_slots_update()
    model_resp: dict = {"ok": True, "model": model_name, "jev_route": jev_route}
    if teardown_incomplete:
        # Advisory only — the switch itself succeeded and the response
        # carries the committed state (agent-handler precedent).
        model_resp["warning"] = _TEARDOWN_INCOMPLETE_WARNING
    return web.json_response(model_resp)


async def api_chat_slots_model(request: web.Request) -> web.Response:
    """POST /api/chat/slots/model — set the model for ALL chat slots (bulk).

    Body: {"model": "<name>" | "", "skip_running": bool (default True)}.
    "" selects the provider/auto default. Applies the model to every slot
    whose model differs, resetting each affected slot's session. Mid-turn
    policy deliberately differs from ``api_chat_slot_model``: the single-slot
    handler prefers a live in-place switch and answers 409 for a slot
    mid-turn, while this bulk endpoint always resets and skips mid-turn slots
    when ``skip_running`` is true (the default) — passing ``skip_running:
    false`` is an explicit opt-in that still tears down in-flight turns.
    Returns the slot keys that were switched / skipped / unchanged /
    failed; a per-slot reset failure is isolated (that slot is reported in
    ``failed`` and keeps its old model) rather than aborting the whole switch.
    """
    state: DashboardState = request.app["state"]
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    model_name = _normalize_model(body.get("model", ""))
    # One pick lands on every slot, so it must pass on every slot's harness.
    reason = _model_rejected_reason(
        model_name, session_keys=[effective_session_key(s) for s in list(state._slots.values())]
    )
    if reason:
        return web.json_response({"error": reason}, status=400)
    skip_running = body.get("skip_running", True)
    if not isinstance(skip_running, bool):
        return web.json_response({"error": "skip_running must be a boolean"}, status=400)
    # Deny-by-default (security-controls): the auth middleware always sets
    # request["app"] on every authenticated path (empty string for dashboard
    # users, app name for app tokens). An ABSENT key means the middleware did
    # not run -- refuse rather than fall through to all-slot access.
    if "app" not in request:
        return web.json_response({"error": "unauthorized"}, status=403)
    request_app = request["app"]
    # Dashboard users are identified by the middleware's EXPLICIT "" assignment.
    # Compare with == "" (not truthiness) so an unexpected falsy value (None, 0)
    # fails closed into the per-slot ownership check instead of bypassing it.
    is_dashboard_user = request_app == ""

    switched: list[str] = []
    skipped_running: list[str] = []
    unchanged: list[str] = []
    # Whether any slot's per-turn routing flag was cleared without its model
    # changing -- the one bulk outcome that is invisible in the three lists below.
    routing_cleared = False
    failed: list[str] = []
    # Snapshot the slot keys up front: sessions.reset awaits, so iterating the
    # live dict directly would risk a concurrent-modification surprise.
    for name, slot in list(state._slots.items()):
        # App Kit ownership isolation: app callers can only switch their own
        # slots (mirrors api_chat_slots_cleanup). Only an explicit dashboard
        # user bypasses the ownership check.
        if not is_dashboard_user and slot._app != request_app:
            continue
        # Same three locks, same order, as the single-slot pick (slot._lock
        # outer, _model_pick_lock inner). ALL classification happens inside
        # them, equality FIRST: a serialized switch commits slot.model before
        # its provider RPC and rolls it back on failure, so an unlocked
        # equality read could match that transient value and report a slot
        # "unchanged" for a model that is then rolled back — and an
        # unlocked running-check ahead of the equality
        # check would classify a running slot that already uses the requested
        # model as skipped_running instead of unchanged. Queuing on the locks
        # is cheap: turns do not hold slot._lock, so a running slot's lock
        # only contends with another switch handler.
        #
        # The session lock is entered AFTER slot._lock, once the key below is
        # known (see _slot_switch_session_lock): per-slot locks are disjoint
        # across aliases, so without it a single-slot switch through another
        # alias could reset this same session concurrently.
        async with contextlib.AsyncExitStack() as _stack:
            await _stack.enter_async_context(slot._lock)
            # Re-authorize after the await above (see
            # _slot_replaced_while_queued): the ownership check ran on the
            # snapshot's object, and ``name`` may now register a different
            # slot -- another app's, or a reconnect. Reported like the
            # rebound case below: skipped, so the caller retries against
            # whatever the name resolves to now, never switched or failed.
            if _slot_replaced_while_queued(state, slot, name, request, "chat.slots_model"):
                skipped_running.append(name)
                continue
            # The session this slot's turns run on — effective_session_key,
            # never _history_key_for (see api_chat_slot_model), resolved
            # INSIDE the lock so a binding that lands while this iteration
            # waits on it is what the reset addresses.
            session_key = effective_session_key(slot)
            # Now serialize against every OTHER alias slot on this session,
            # keyed on the value resolved just above (see
            # _slot_switch_session_lock): per-slot locks are disjoint across
            # aliases. Entered per iteration and released with the stack, so
            # two alias slots in ONE bulk request queue in turn rather than
            # re-entering the same lock.
            await _stack.enter_async_context(_slot_switch_session_lock(session_key))
            await _stack.enter_async_context(slot._model_pick_lock)
            # Two more lock-acquisition awaits, one re-check: nothing reads
            # ``slot`` between them, so a check after the last one covers both.
            if _slot_replaced_while_queued(state, slot, name, request, "chat.slots_model"):
                skipped_running.append(name)
                continue
            if not is_dashboard_user and session_key != _history_key_for(name):
                # Slot ownership does not imply ownership of a linked channel
                # session (the cancel routes' second condition): an app caller
                # does not get to switch the model a channel thread runs on.
                # Skipped silently, like every other slot the app does not own.
                continue
            if slot.model == model_name:
                # The MODEL is unchanged; the routing choice may not be. A slot
                # already on this model but routed per turn is a slot whose turns
                # would still be moved off it, so the flag is cleared here as well
                # -- otherwise the one case where the bulk switch reports "nothing
                # to do" is the one case where it silently did nothing at all.
                # Tracked so the push below fires for a slot whose only change is
                # this: the picker reads the flag, so without a broadcast the chip
                # keeps naming a routing this request has just stopped.
                if slot.jev_route:
                    slot.jev_route = False
                    routing_cleared = True
                unchanged.append(name)
                continue
            # Children guard (api_chat_slot_reload's): the reset tears down the
            # runtime attached sub-agents run on, so a parent with children
            # running, queued, or mid-delivery is skipped rather than have
            # their work discarded — regardless of skip_running, which speaks
            # to the parent's own turn, not to its children.
            if await subagents_attached_async(state, slot, session_key, "slots_model"):
                skipped_running.append(name)
                continue
            # Busy check on the EFFECTIVE session, same predicate as the
            # single-slot handler (_switch_target_busy): slot.running only
            # sees turns dispatched through this slot's task; a
            # channel-linked slot's turn runs under its linked key without
            # setting it, and a sibling alias cold-starting on the shared
            # session has set neither. Probed AFTER the children await: the
            # reset below is the next thing this iteration does, so this is
            # the no-await window. _reset_slot_session clears pending waits
            # BEFORE its atomic decline (its docstring's safety argument
            # assumes a caller-side busy check microseconds old), so entering
            # it against a live linked turn would reject that turn's cards
            # even though the reset itself declines. Gated on skip_running
            # like the atomic decline below: a caller that asked to switch
            # running slots gets the reset regardless.
            live_now = state.sessions.get_provider(session_key)
            if skip_running and _switch_target_busy(state, slot, session_key, live_now):
                skipped_running.append(name)
                continue
            # Reset before flipping the model and isolate per-slot failures: if
            # the reset raises, leave slot.model untouched so the slot is never
            # left on the new model with stale history (the model/history
            # inconsistency), and a single failure doesn't abort the whole bulk
            # switch. skip_if_busy mirrors skip_running: when the caller asked
            # to skip running slots, a turn that started AFTER the checks above
            # (message dispatch does not take slot._lock) is declined at the
            # authoritative point — SessionManager.reset evaluates busyness
            # atomically with the session pop — instead of being torn down;
            # skip_running=false keeps its documented force semantics.
            try:
                reset_ok = await _reset_slot_session(
                    state, slot, session_key, skip_if_busy=skip_running
                )
                if skip_running and not reset_ok:
                    if slot.running:
                        # A first send slipped into the reset await and is still
                        # inside its multi-second provider.start(): visible to
                        # slot.running (set at dispatch) but not yet to
                        # get_provider, so the provider ladder below would read
                        # "no live provider" and commit over a session that
                        # captured the OLD model (this handler commits AFTER the
                        # reset). Classify it as the in-lock pre-check would have.
                        skipped_running.append(name)
                        continue
                    busy_provider = state.sessions.get_provider(session_key)
                    if isinstance(busy_provider, LLMProvider):
                        if busy_provider.has_active_turn():
                            # A turn slipped into the check window: classify it
                            # the same as the pre-check would have, leaving the
                            # turn (and the slot's model) untouched.
                            skipped_running.append(name)
                            continue
                        # A live IDLE session declined the reset: the
                        # slipped-in turn already finished before the re-read.
                        # This handler commits AFTER the reset, so that session
                        # is still on the old model — committing over it would
                        # create the exact model/history inconsistency the
                        # ordering exists to prevent. Retry once
                        # (api_chat_slot_reload's template); a second decline
                        # means another turn is genuinely racing. The retry
                        # runs INSIDE this try so a teardown that raises keeps
                        # the per-slot failure isolation: the slot lands in
                        # failed with its model untouched instead of aborting
                        # the whole bulk switch with a 500.
                        reset_ok = await _reset_slot_session(
                            state, slot, session_key, skip_if_busy=True
                        )
                        if not reset_ok:
                            skipped_running.append(name)
                            continue
                    # No live provider: no session to tear down — the next
                    # message cold-starts under the new model.
            except Exception:
                logger.error("Bulk model switch: session reset failed for %s", name, exc_info=True)
                failed.append(name)
                continue
            if effective_session_key(slot) != session_key:
                # The slot was bound to a different session during the reset
                # await: the session torn down is not the slot's, so
                # committing would advertise the new model over one that
                # never saw the switch. Nothing to roll back (bulk commits
                # after the reset); report it as skipped so the caller retries.
                skipped_running.append(name)
                continue
            slot.model = model_name
            # Explicit pick (bulk): same generation bump as the single-slot pick.
            slot._model_pick_gen += 1
            # And the same clearing of the routing choice. This surface takes no
            # "Auto (Jev)" target -- it switches many sessions to one model, which
            # is the opposite of a per-turn tier -- but it is an explicit pick, so
            # leaving the flag set would route the next turn away from the model
            # the owner just chose for this slot.
            slot.jev_route = False
            _broadcast_context_reset(state, slot.key, None)
            # Same boundary row as the single-slot reset, per switched slot and
            # inside its locks; unchanged and skipped slots append nothing.
            await _append_session_switch_notice(slot, "model", model_name or "auto")
            switched.append(name)

    if switched:
        logger.info(
            "Bulk model switch to %r: %d switched, %d skipped-running, %d unchanged, %d failed",
            model_name or "auto",
            len(switched),
            len(skipped_running),
            len(unchanged),
            len(failed),
        )
        # Guard the push on real progress so partial switches still broadcast
        # even when a later slot's reset failed.
        state.push_slots_update()
    elif routing_cleared:
        # No model moved, but a routing flag did, and the picker renders that.
        state.push_slots_update()
    return web.json_response(
        {
            "ok": True,
            "model": model_name,
            "switched": switched,
            "skipped_running": skipped_running,
            "unchanged": unchanged,
            "failed": failed,
        }
    )
