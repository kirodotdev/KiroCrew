"""Turn verbs on a live target: stop, retry, continue, end a wait, reload and close."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

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
    _created_by_other,
    caller_slot_key,
    prewarm_enabled_check,
)
from kiro_crew.dashboard.session_control_parts.delivery_containment import send_origin_meta
from kiro_crew.dashboard.session_control_parts.target_authorization import (
    _deny_factory,
    authorize_target,
    load_target_window,
)
from kiro_crew.dashboard.stop_retry import allow_escalation
from kiro_crew.sel import sel

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

# The module's original logger name, which log filters and caplog assertions key on.
logger = logging.getLogger("kiro_crew.dashboard.session_control")


async def stop_target(
    state: "DashboardState",
    *,
    caller_session_key: str,
    target: str,
    caller_fenced: bool | None = None,
) -> dict[str, Any]:
    """Stop *target*'s in-flight turn, via the same path as the Stop button.

    A stop cancels cooperatively. The button escalates to a hard kill when a
    second press lands while the first is still pending; this verb deliberately
    does not do that for a repeat it cannot tell apart from a RETRY, because a
    client that got no response inside its request timeout re-sends the same
    request, and the kill path discards the target's queue and pending steers. So
    within ``stop_retry.WINDOW_SECS`` of this caller's first stop of this target, a
    repeat returns the existing "stop already in progress" no-op instead. A stop
    arriving after that window still escalates, so a genuine second decision keeps
    the capability — only a blind retry cannot reach it.

    Withholding the escalation never costs the caller the stop it asked for: a
    repeat that finds the target running again soft-stops it as a first call would.

    Still no force flag: escalation is decided by the target's own stop state and
    the window above, never by anything the caller can ask for, so advertising one
    would promise a hard kill a first call cannot deliver.

    ``caller_fenced`` is the ownership-fence verdict the HTTP gate carries for a
    caller it admitted as a crew member (``True``); ``None`` for every other
    caller. Forwarded to :func:`authorize_target` as
    ``precomputed_ownership_fenced`` — see there for why the verified admission
    travels with the request instead of being re-derived from config at the fence.
    The same parameter, with the same meaning, is on :func:`close_target`,
    :func:`send_to_target` and :func:`read_messages`.
    """
    # Prewarmed BEFORE `authorize_target`, and that ordering is load-bearing.
    # `stop_slot_turn`'s IDLE branch logs to the SEL with no await before it, so on
    # a fresh gateway a first `session_stop` against an idle slot would CONSTRUCT
    # the log on the loop -- trust-dir creation and key validation, blocking file
    # IO. Constructing it off-loop first makes that call a cheap cache hit.
    # Per-request, not a boot step: prewarming at startup is what
    # `no-new-work-on-gateway-boot-path` forbids, and a background task would only
    # narrow the race rather than close it.
    #
    # It must sit ABOVE the gate because `await` is a suspension point: between
    # `authorize_target` and `stop_slot_turn` the loop must not yield, or a user
    # action landing in that window (linking the target to a channel) makes the
    # decision stale and the `mirrored_target` refusal is bypassed -- the turn gets
    # cancelled on a session that became channel-backed after the check passed.
    # Nothing may suspend between this gate and the act it authorizes.
    #
    # Best-effort on purpose: construction can raise (a trust root too short to
    # sign the chain), and this is a latency guard, not an authorization one --
    # failing it must not turn a stop into a 500.
    try:
        await asyncio.to_thread(sel)
    except Exception:  # noqa: BLE001 - a prewarm failure must not fail the stop
        logger.warning("session-control SEL prewarm failed", exc_info=True)

    # The config warm goes HERE, not in the handler: the SEL prewarm above is an
    # `await`, and so is reading the request body, so a warm done before either of
    # them can be invalidated by a config edit landing in the gap -- leaving
    # `authorize_target`'s synchronous `session_control_enabled` to re-read and
    # validate the file on the loop, which is the whole thing the warm exists to
    # avoid. This is the last suspension before the gate.
    await prewarm_enabled_check()

    slot = authorize_target(
        state,
        caller_session_key=caller_session_key,
        target=target,
        operation="stop",
        precomputed_ownership_fenced=caller_fenced,
    )
    # Both calls below are SYNCHRONOUS, which is what lets them sit here at all:
    # the rule the comment above states is that nothing may SUSPEND between the
    # gate and the act, and neither of these does.
    #
    # `caller_slot_key` repeats the slot walk `authorize_target` just did rather
    # than changing what that function returns for all three verbs. The walk is
    # bounded by `MAX_LIVE_SLOTS` and touches no filesystem, and with no
    # suspension between them the two resolutions cannot disagree — a rebind
    # landing in that window is impossible, not merely unlikely.
    caller_key = caller_slot_key(state, caller_session_key)
    may_escalate = allow_escalation(caller_key, slot.key)
    # Deferred: ``chat_handlers`` imports ``dashboard.chat`` transitively, which
    # reaches back into the gateway at import time — a module-scope import here
    # closes that cycle through ``handlers.session_control`` -> ``server``.
    from kiro_crew.dashboard.chat_handlers import stop_slot_turn

    result = await stop_slot_turn(state, slot, source="session_control", escalate=may_escalate)
    _audit(
        caller_session_key=caller_session_key,
        operation="stop",
        slot_key=slot.key,
        outcome="allowed",
        detail={
            "result": result.get("info", "stopping"),
            # Recorded on the ALLOWED line, not only inside `stop_slot_turn`'s
            # own audit: this is the layer that made the retry judgement, so the
            # session-control trail has to show it was made.
            "escalation_withheld": not may_escalate,
        },
    )
    return {"ok": True, "target": slot.key, **result}


async def retry_target(
    state: "DashboardState",
    *,
    caller_session_key: str,
    target: str,
    caller_fenced: bool | None = None,
) -> dict[str, Any]:
    """Re-run *target*'s failed last turn, via the same path as the Resume button.

    Reuses ``chat_handlers.continue_slot_turn``, the mechanism behind
    ``POST /api/chat/slots/{slot}/continue``: the recovery continuation is queued
    at the head and dispatched, so the transcript gains the same recovery row a
    Resume press leaves and no second copy of the user's message. Every refusal
    Continue makes applies unchanged, including ``session_start_repeat`` after
    two identical failed starts.

    One refusal is this verb's own: ``turn_not_failed`` when
    ``is_turn_interrupted`` says the last turn finished or was ended with Stop.
    Continue carries on from a finished turn; a retry must not, or one session
    could make another produce a fresh answer on top of a good one.

    Not readiness-gated, for the reason ``api_chat_slot_continue`` gives: it
    mutates nothing durable before dispatch, so the turn's own start is the
    authority and a signed-out install reports in the transcript.

    ``caller_fenced`` has the meaning :func:`stop_target` documents.
    """
    slot_key, _body = await _continue_turn_of_target(
        state,
        caller_session_key=caller_session_key,
        target=target,
        caller_fenced=caller_fenced,
        operation="retry",
        require_interrupted=True,
    )
    return {"ok": True, "target": slot_key}


async def continue_target(
    state: "DashboardState",
    *,
    caller_session_key: str,
    target: str,
    caller_fenced: bool | None = None,
) -> dict[str, Any]:
    """Hand *target*'s thread back to its agent, as the Continue button does.

    The same call as :func:`retry_target` without ``require_interrupted``: a
    force-quit turn runs no ``finally`` and leaves no error row, so it reads as
    finished, and only this verb can pick it up. ``continue_slot_turn`` chooses
    the body exactly as it does for the button, and the result reports which one
    it queued in ``body``: ``"resumed"`` (the interrupted pick-up) or
    ``"continued"`` (carry on from a turn that ended cleanly). No prompt text is
    accepted; the body is always one of the two fixed continuation messages.

    Not readiness-gated, for the reason :func:`retry_target` gives.
    ``caller_fenced`` has the meaning :func:`stop_target` documents.
    """
    slot_key, body = await _continue_turn_of_target(
        state,
        caller_session_key=caller_session_key,
        target=target,
        caller_fenced=caller_fenced,
        operation="continue",
        require_interrupted=False,
    )
    return {"ok": True, "target": slot_key, "body": body}


async def _continue_turn_of_target(
    state: "DashboardState",
    *,
    caller_session_key: str,
    target: str,
    caller_fenced: bool | None,
    operation: str,
    require_interrupted: bool,
) -> tuple[str, str]:
    """Authorize *target* and queue a continuation on it; the core of retry and continue.

    Returns the target's slot key and the body ``continue_slot_turn`` queued
    (``"resumed"`` or ``"continued"``). *operation* names the verb on every audit
    line and on the gate's refusals.
    """
    # Both verbs re-read the target's last turn: a target restored as a
    # sidebar row loads its window first, ahead of the prewarm below.
    await load_target_window(state, target)
    # Same prewarm ordering as `close_target`: the SEL and config reads must be
    # warm before the synchronous gate, and the fence verdict is resolved once so
    # the re-check under the slot lock never loads config on the loop.
    try:
        await asyncio.to_thread(sel)
    except Exception:  # noqa: BLE001 - a prewarm failure must not fail the call
        logger.warning("session-control SEL prewarm failed", exc_info=True)
    await prewarm_enabled_check()

    caller_key = caller_slot_key(state, caller_session_key)
    if caller_fenced is None:
        caller_fenced = bool(caller_key) and _caller_is_ownership_fenced(state, caller_key)

    slot = authorize_target(
        state,
        caller_session_key=caller_session_key,
        target=target,
        operation=operation,
        precomputed_ownership_fenced=caller_fenced,
    )
    slot_key = slot.key
    # Deferred for the same import cycle `stop_target` documents.
    from kiro_crew.dashboard.chat_handlers import SlotContinueRefusal, continue_slot_turn

    def _reassert_reachable() -> None:
        # The slot lock `continue_slot_turn` takes, and its sub-agent probe, are
        # awaits: a target that was reachable at the gate above can become
        # channel-linked or mirrored, or be replaced under the same key, before
        # the continuation is queued. Re-run the same gate here, synchronously
        # and with no config read, as `close_target` does at its point of no
        # return.
        try:
            live = authorize_target(
                state,
                caller_session_key=caller_session_key,
                target=slot_key,
                operation=operation,
                skip_enabled_check=True,
                precomputed_ownership_fenced=caller_fenced,
            )
        except SessionControlError as exc:
            raise SlotContinueRefusal(exc.message, exc.code, status=exc.status) from exc
        if live is not slot:
            raise SlotContinueRefusal(
                f"the target session was replaced during the {operation}", "target_replaced"
            )

    with _audit_denials(
        caller_session_key=caller_session_key, operation=operation, slot_key=slot_key
    ):
        # The same predicate the Continue endpoint refuses on, so the button and
        # the verbs cannot drift: a relay archive would run the crew's old turn on
        # this machine.
        if is_relay_archive(slot):
            raise SessionControlError(RELAY_ARCHIVE_ERROR, code=RELAY_ARCHIVE_CODE, status=409)
        try:
            body = await continue_slot_turn(
                state,
                slot,
                # An agent asked for this, not the person at the keyboard: the
                # turn must not gain the authenticated-human flag.
                directive_user_origin=False,
                via="session_control",
                require_interrupted=require_interrupted,
                before_dispatch=_reassert_reachable,
                extra_meta=send_origin_meta(state, caller_key),
            )
        except SlotContinueRefusal as exc:
            raise SessionControlError(exc.message, status=exc.status, code=exc.code) from exc
    _audit(
        caller_session_key=caller_session_key,
        operation=operation,
        slot_key=slot_key,
        outcome="allowed",
        detail={"body": body},
    )
    return slot_key, body


async def end_wait_target(
    state: "DashboardState",
    *,
    caller_session_key: str,
    target: str,
    caller_fenced: bool | None = None,
) -> dict[str, Any]:
    """Wake *target* from the ``wait`` tool early, keeping its turn.

    The same mechanism as the dashboard's End-wait button
    (``api_chat_slot_end_wait``): the request is parked on the slot as
    ``_end_wait_request`` and the sleeping tool collects it on its next keepalive
    ping, then returns a normal tool result. Nothing is cancelled and no work is
    discarded, which is what separates this verb from ``session_stop``.

    The caller does not name a ``wait_id``. The button needs one because a stale
    tab can still show an old countdown; here the id is read from the slot at the
    moment of the request, so the request can only ever name the sleep that is in
    flight now. ``_end_wait_by`` records who asked, so the keepalive reply can tell
    the woken session that another session ended its wait rather than the user.

    A target that is not sleeping is not an error: the reply carries ``info``
    instead, so a caller does not retry something that has nothing to act on.
    """
    # Same ordering as `stop_target`: both prewarms are awaits, so they sit
    # above the gate and nothing suspends between the gate and the write.
    try:
        await asyncio.to_thread(sel)
    except Exception:  # noqa: BLE001 - a prewarm failure must not fail the call
        logger.warning("session-control SEL prewarm failed", exc_info=True)
    await prewarm_enabled_check()

    slot = authorize_target(
        state,
        caller_session_key=caller_session_key,
        target=target,
        operation="end_wait",
        precomputed_ownership_fenced=caller_fenced,
    )
    caller_key = caller_slot_key(state, caller_session_key)
    if _created_by_other(slot, caller_key):
        # Narrower than the other verbs on purpose: an owner session is not
        # creator-fenced by `authorize_target`, but waking a sleep moves a turn
        # forward on someone else's schedule, and the caller that armed the
        # worker's wait is the one that knows when it is safe to end it.
        raise _deny_factory(
            caller_session_key=caller_session_key, operation="end_wait", target=target
        )("session_end_wait reaches only sessions you created", "not_creator")
    if getattr(slot, "_wait_contested", False):
        # Two sleeps share this slot's session key (see _service_wait_ping's
        # ambiguous-identity guard). There is no way to aim at one of them, and
        # the button is hidden for the same reason.
        info = "two waits share this session, so neither can be ended early"
        result = {"ended": False, "info": info}
        audit_result = "contested"
    else:
        current = getattr(slot, "_wait_state", None) or {}
        wait_id = str(current.get("wait_id") or "")
        if not wait_id:
            result = {"ended": False, "info": "not sleeping in the wait tool"}
            audit_result = "not_waiting"
        else:
            slot._end_wait_request = wait_id
            slot._end_wait_by = caller_key
            result = {"ended": True, "wait_id": wait_id}
            audit_result = "requested"
    _audit(
        caller_session_key=caller_session_key,
        operation="end_wait",
        slot_key=slot.key,
        outcome="allowed",
        detail={"result": audit_result},
    )
    return {"ok": True, "target": slot.key, **result}


def _reload_busy_error() -> SessionControlError:
    """The refusal ``reload_target`` gives a target with work in flight or queued."""
    return SessionControlError(
        "session busy, not reloaded: it has a turn, queued messages or sub-agents in "
        "flight. Wait until it is idle and retry.",
        code="target_busy",
        status=409,
    )


def _reload_route_refusal(status: int, body: dict[str, Any]) -> SessionControlError:
    """Map a ``reload_slot_session`` refusal to what ``session_reload`` reports.

    The two busy codes collapse into ``target_busy`` so a caller sees one code for
    "not now"; ``slot_not_found`` means the slot was replaced while the request
    queued on a lock, which for this verb is a replaced target.
    """
    code = str(body.get("code") or "")
    if code in ("turn_in_flight", "slot_subagents_running"):
        return _reload_busy_error()
    if code == "slot_not_found":
        return SessionControlError(
            "the target session was replaced; not reloaded",
            code="target_replaced",
            status=409,
        )
    return SessionControlError(
        str(body.get("error") or "reload refused"), code=code or "reload_failed", status=status
    )


async def reload_target(
    state: "DashboardState",
    *,
    caller_session_key: str,
    target: str,
    caller_fenced: bool | None = None,
) -> dict[str, Any]:
    """Relaunch *target*'s agent process, as the tab menu's Reload session does.

    The teardown is ``chat_handlers.reload_slot_session``, the same code the
    dashboard route runs, so the lock order, the rebind and replacement
    re-checks and the ``skip_if_busy`` reset are shared rather than copied. The
    transcript is not rewritten: the session is reset and one reload notice is
    appended, naming the calling session.

    Narrower than the other verbs in three ways:

    * Only a session the caller CREATED, for every caller class. The dashboard
      owner's own sessions are reachable by ``session_stop`` and the rest, but
      a reload relaunches a process the person may be in the middle of using,
      so this verb stays on sessions the caller dispatched itself.
    * Never the caller itself (``authorize_target``'s default self refusal). A
      caller is mid-turn by definition, and the teardown refuses a session with
      a turn in flight, so a self-reload could only ever fail.
    * Only an IDLE target: ``_switch_target_busy`` (which also sees a turn that
      is still cold-starting), a non-empty queue, or attached sub-agents refuse
      it with ``target_busy`` before anything is torn down, and the same probe
      runs again inside the teardown's locks.

    ``caller_fenced`` has the meaning :func:`stop_target` documents.
    """
    # Deferred for the same import cycle `stop_target` documents.
    from kiro_crew.dashboard.chat_handlers import (
        _SESSION_RELOAD_NOTICE,
        _subagents_attached_response,
        _switch_target_busy,
        reload_slot_session,
    )

    # Same prewarm ordering as `stop_target`.
    try:
        await asyncio.to_thread(sel)
    except Exception:  # noqa: BLE001 - a prewarm failure must not fail the reload
        logger.warning("session-control SEL prewarm failed", exc_info=True)
    await prewarm_enabled_check()
    caller_key = caller_slot_key(state, caller_session_key)
    if caller_fenced is None:
        caller_fenced = bool(caller_key) and _caller_is_ownership_fenced(state, caller_key)

    def _gate(target_name: str, *, first: bool = False) -> "_ChatSlot":
        # Synchronous, so it can run inside the teardown's locks with no
        # suspension between the decision and the act it authorizes. Only the
        # first pass reads the enabled switch, as in `close_target`'s re-check.
        found = authorize_target(
            state,
            caller_session_key=caller_session_key,
            target=target_name,
            operation="reload",
            skip_enabled_check=not first,
            precomputed_ownership_fenced=caller_fenced,
        )
        with _audit_denials(
            caller_session_key=caller_session_key, operation="reload", slot_key=found.key
        ):
            if not caller_key or _created_by_other(found, caller_key):
                raise SessionControlError(
                    "a session can only reload sessions it created itself",
                    code="not_creator",
                    status=403,
                )
            if is_relay_archive(found):
                raise SessionControlError(RELAY_ARCHIVE_ERROR, code=RELAY_ARCHIVE_CODE, status=409)
        return found

    def _busy(target_slot: "_ChatSlot", session_key: str) -> bool:
        provider = state.sessions.get_provider(session_key)
        return bool(target_slot._queue) or _switch_target_busy(
            state, target_slot, session_key, provider
        )

    slot = _gate(target, first=True)
    slot_key = slot.key

    with _audit_denials(
        caller_session_key=caller_session_key, operation="reload", slot_key=slot_key
    ):
        # Refused up front, before any lock is taken, so a busy target costs the
        # caller one probe and nothing queues behind the target's own switches.
        session_key = effective_session_key(slot)
        if _busy(slot, session_key):
            raise _reload_busy_error()
        if await _subagents_attached_response(state, slot, session_key, "reload") is not None:
            raise _reload_busy_error()

    changed_after_reset: list[SessionControlError] = []

    def _still_ours(after_reset: bool) -> bool:
        # Re-runs the whole gate after every await inside the teardown: a
        # replaced slot, a new channel link or mirror, a moved workspace or a
        # changed creator raises its own refusal out of the lock stack.
        # ``after_reset`` comes from reload_slot_session itself, so the phase
        # is never inferred from which callback ran last.
        if state._slots.get(slot_key) is not slot:
            if after_reset:
                changed_after_reset.append(
                    SessionControlError(
                        "the target session was replaced", code="slot_replaced", status=409
                    )
                )
            return False
        try:
            return _gate(slot_key) is slot
        except SessionControlError as exc:
            if not after_reset:
                raise
            # The reset already ran. Letting the refusal escape would tell the
            # caller "nothing happened" about a process that is gone, and would
            # skip the audit below; record it and answer as a changed target.
            changed_after_reset.append(exc)
            return False

    notice = f"{_SESSION_RELOAD_NOTICE} Requested by session `{caller_key}`."
    try:
        response = await reload_slot_session(
            state,
            slot,
            slot_key,
            still_ours=_still_ours,
            denied=lambda _session_key: None,
            busy=lambda session_key: _busy(slot, session_key),
            notice=notice,
        )
    except SessionControlError:
        raise
    except Exception as exc:
        # The shared teardown absorbs a raise after the session pop (the
        # reload happened, degraded) and re-raises only one from before it,
        # when the old process is still the registered one. Report that as a
        # failed reload and audit it, rather than escaping as a bare 500.
        logger.exception("session_reload of %s failed before the reset", slot_key)
        _audit(
            caller_session_key=caller_session_key,
            operation="reload",
            slot_key=slot_key,
            outcome="denied",
            detail={"code": "reload_failed", "error": type(exc).__name__},
        )
        raise SessionControlError(
            "the reload failed before the target's agent process was reset; "
            "nothing was torn down",
            code="reload_failed",
            status=500,
        ) from exc
    if changed_after_reset:
        _audit(
            caller_session_key=caller_session_key,
            operation="reload",
            slot_key=slot_key,
            outcome="denied",
            detail={
                "code": "target_changed_during_reload",
                "gate_code": changed_after_reset[0].code,
            },
        )
        raise SessionControlError(
            "the target's agent process was reset, but the session changed during "
            f"the reload ({changed_after_reset[0].code}); no reload notice was added",
            code="target_changed_during_reload",
            status=409,
        )
    if response.status != 200:
        body = json.loads(response.text or "{}")
        error = _reload_route_refusal(response.status, body)
        _audit(
            caller_session_key=caller_session_key,
            operation="reload",
            slot_key=slot_key,
            outcome="denied",
            detail={"code": error.code},
        )
        raise error

    body = json.loads(response.text or "{}")
    warning = body.get("warning")
    detail: dict[str, Any] = {"reloaded_by": caller_key}
    if warning:
        detail["warning"] = warning
    _audit(
        caller_session_key=caller_session_key,
        operation="reload",
        slot_key=slot_key,
        outcome="allowed",
        detail=detail,
    )
    out: dict[str, Any] = {"ok": True, "target": slot_key}
    if warning:
        out["warning"] = warning
    return out


async def close_target(
    state: "DashboardState",
    *,
    caller_session_key: str,
    target: str,
    caller_fenced: bool | None = None,
) -> dict[str, Any]:
    """Close *target*, the same archival the tab ✕ performs.

    Non-destructive: the conversation is saved to history and can be reopened
    later — closing dismisses the LIVE tab, it does not delete the transcript.
    An in-flight turn is cancelled first (its work is discarded), so this is a
    strictly heavier act than :func:`stop_target`; the description tells the
    caller to read the session before closing it.

    Reuses the dashboard's own close path (:func:`chat_handlers.close_slot`), so
    a controlled close and a human ✕ share the identical nudge-retirement and
    app-notification ordering that keeps a dismissed tab from being resurrected.
    Its four failure modes surface as their own ``SessionControlError`` codes
    rather than a generic 500, so a caller can tell "the app refused the
    dismissal" from "history could not be saved".
    """
    # Same prewarm ordering as `stop_target`, for the same reasons: the SEL write
    # inside `authorize_target`'s deny path must be a cache hit, and the config
    # warm must be the LAST suspension before the synchronous gate.
    try:
        await asyncio.to_thread(sel)
    except Exception:  # noqa: BLE001 - a prewarm failure must not fail the close
        logger.warning("session-control SEL prewarm failed", exc_info=True)
    await prewarm_enabled_check()

    caller_key = caller_slot_key(state, caller_session_key)
    # Resolve the ownership-fence verdict ONCE, up front, while the config cache
    # is warm from ``prewarm_enabled_check`` above and we are not yet inside
    # ``close_slot``'s no-suspension window. BOTH the initial gate and the
    # synchronous re-check reuse it via ``precomputed_ownership_fenced`` rather
    # than recomputing — the fence can read config on a cache miss
    # (``_member_caller`` → ``_store_is_member_owned`` → ``KiroCrewConfig.load()``),
    # which is exactly the blocking-IO-on-the-event-loop the close critical
    # section must not do. A verdict the HTTP gate already carried (a caller it
    # admitted as a crew member) is honoured as-is; otherwise it is computed here.
    # An empty ``caller_key`` (unidentifiable caller) makes it ``False`` and the
    # gate below still raises ``caller_unidentified`` before the fence is consulted.
    if caller_fenced is None:
        caller_fenced = bool(caller_key) and _caller_is_ownership_fenced(state, caller_key)

    slot = authorize_target(
        state,
        caller_session_key=caller_session_key,
        target=target,
        operation="close",
        precomputed_ownership_fenced=caller_fenced,
        allow_self=True,
    )
    slot_key = slot.key
    # Deferred for the same import cycle `stop_target` documents.
    from kiro_crew.dashboard.chat_handlers import SlotCloseError, close_slot

    def _reassert_closeable() -> None:
        # Re-run the SAME target gate at close_slot's point of no return —
        # SYNCHRONOUSLY, so there is NO event-loop suspension between it and the
        # pop and nothing can change between the final authorization and the
        # archival. The initial gate above ran before close_slot's awaits
        # (nudge-loop retirement takes the AutoNudge lock; the app hook awaits
        # external work), and a target that was unmirrored/unlinked then can gain
        # a channel mirror or link in that window — archiving a now-channel-backed
        # session the caller was never allowed to reach.
        #
        # `skip_enabled_check=True` omits the ONE part of authorize_target that
        # can touch the disk (`session_control_enabled()`'s config read): the
        # feature was already confirmed enabled above, whether it was switched off
        # mid-close is not a containment boundary, and skipping it is what lets
        # this run with no await — an async prewarm-then-check would put an await
        # back before the pop and reopen the very window this closes. Every
        # containment and identity refusal still runs.
        #
        # `precomputed_ownership_fenced=caller_fenced` closes the SECOND disk
        # touch: the ownership fence's own config read (member-store lookup),
        # resolved once above and reused here so this callback never loads config
        # on the loop.
        try:
            live = authorize_target(
                state,
                caller_session_key=caller_session_key,
                target=slot_key,
                operation="close",
                skip_enabled_check=True,
                precomputed_ownership_fenced=caller_fenced,
                allow_self=True,
            )
        except SessionControlError as exc:
            # A stale-authorization refusal (mirrored/linked/workspace/caller-gone)
            # becomes a SlotCloseError carrying that same status, so it round-trips
            # to the caller as the specific 403 rather than a generic close failure.
            raise SlotCloseError(exc.message, code=exc.code, status=exc.status) from exc
        if live is not slot:
            # The key was re-minted onto a DIFFERENT session while close_slot
            # awaited (a concurrent close+reopen). authorize_target resolves by
            # key, so it would authorize the replacement — but close_slot pops
            # `name` and tears down / saves the ORIGINAL slot it holds. Comparing
            # identity (not mere presence) is the same guard `create_session` uses
            # for its re-minted-key window; abort so the replacement lives.
            raise SlotCloseError(
                "the target session was replaced during the close",
                code="target_replaced",
                status=409,
            )

    try:
        await close_slot(state, slot, slot_key, pre_pop_check=_reassert_closeable)
    except SlotCloseError as exc:
        # The close path already rolled back every partial step and logged the
        # cause; re-raise it as the surface's own error so the caller sees the
        # specific reason (write-in-flight/nudge/app/history) rather than a bare
        # failure. Audited as a denied operation so the trail shows the close was
        # attempted and did not take.
        _audit(
            caller_session_key=caller_session_key,
            operation="close",
            slot_key=slot_key,
            outcome="denied",
            detail={"code": exc.code},
        )
        raise SessionControlError(exc.message, status=exc.status, code=exc.code) from exc
    _audit(
        caller_session_key=caller_session_key,
        operation="close",
        slot_key=slot_key,
        outcome="allowed",
    )
    return {"ok": True, "target": slot_key}
