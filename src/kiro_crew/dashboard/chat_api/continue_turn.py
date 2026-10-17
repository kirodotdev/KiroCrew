"""Continue and end-wait: ``continue_slot_turn`` with its route, the session-start
failure streak that refuses a repeat, and ``POST /api/chat/slots/{slot}/end-wait``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_handlers import (
        _MANUAL_CONTINUE_MSG,
        _MANUAL_RESUME_MSG,
        _SESSION_START_REPEAT_REFUSAL_AT,
        _TURN_OPENING_INJECT_KINDS,
        SESSION_START_FAILED_KIND,
        SYNTHETIC_RECOVERY_KIND,
        DashboardState,
        _ChatSlot,
        _has_conversation,
        _history_key_for,
        _slot_not_found,
        _slot_replaced_while_queued,
        _start_next_queued_turn,
        _subagents_attached_response,
        deny_app_slot_access,
        effective_session_key,
        is_stop_event_row,
        is_system_notice,
        is_turn_interrupted,
        logger,
        read_bounded_json,
        relay_archive_refusal,
        sel,
    )


async def api_chat_slot_continue(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/continue — hand the thread back to the agent.

    Two callers, one mechanism: picking up a turn that was cut short, and asking
    a slot that finished cleanly to carry on. They are one endpoint because they
    are indistinguishable from the transcript — a force-quit runs no ``finally``,
    so a killed turn leaves no error row behind and reads exactly like a
    completed one. ``_has_conversation`` authorizes; ``_is_interrupted`` only
    chooses which of the two continuation bodies the model receives.

    Runs the same synthetic-continuation machinery the runner already uses for
    its own post-transient recovery: queue the continuation at the head, then let
    ``_start_next_queued_turn`` land it as an ``inject`` row and dispatch the
    turn. No bespoke dispatch path, and the row folds into the existing recovery
    card instead of printing machine prose as a user bubble.

    The frontend decides whether to OFFER this (it has the transcript, `running`
    and the queue locally, so it needs no server field for that). This endpoint
    re-checks under ``slot._lock`` because the client's view is a WS snapshot and
    therefore lagging: a press landing in the instant a turn starts, or a second
    browser tab acting on a stale cache, would otherwise dispatch a duplicate
    turn against one slot — real tokens, real tool calls, real repo writes. Every
    other dispatch route guards the same way (see ``api_chat_slot_regenerate``).

    NOT readiness-gated, and that is deliberate — see
    ``kiro_readiness.reject_if_kiro_unverified``. Continue is an ordinary send: it
    queues one synthetic message and lets the runner dispatch it, mutating nothing
    durable up front, so the ACP attempt is its authority and a signed-out install
    reports ``AcpAuthRequired`` in the transcript. Gating it instead put the
    button behind a latch that is refreshed by re-probing ``kiro-cli``, and a
    probe that merely TIMES OUT reads as signed-out: on a host where that probe is
    slow the press was refused with a 503 forever while typing the same request by
    hand worked. The unequal treatment of two paths that dispatch the same turn is
    the bug; the transcript's own error card is the report either way.

    The one refusal that reads the transcript's content is a different thing
    from a readiness gate: ``session_start_repeat`` fires only when the slot's
    OWN tail holds two ``session_start_failed`` error rows with nothing but
    recovery rows between them -- the same start, re-issued by Resume, failed
    twice. That is not a probe that can be wrong forever; it is the record of
    what this endpoint itself just did twice. A typed message stays allowed and
    resets the count, so the refusal never latches the slot.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return _slot_not_found()

    # App ownership check (App Kit §5.2): deny-by-default for app tokens, mirroring
    # api_chat. Without it an app token holding /api/chat could resume ANY
    # interrupted slot — including a dashboard user's — and that is not a read: it
    # dispatches an agent turn that runs tools and writes to the repo. Same
    # indistinguishable 404 as the send path, so the response cannot serve to
    # probe which foreign slots exist.
    request_app = request.get("app", "")
    denied = deny_app_slot_access(request_app, slot, slot.key, "chat_continue")
    if denied is not None:
        return denied

    # A relay archive never runs. AFTER the app-ownership 404 above: a foreign
    # app must not be able to tell an archive apart from a missing slot.
    refusal = relay_archive_refusal(slot)
    if refusal is not None:
        return refusal

    try:
        await continue_slot_turn(
            state,
            slot,
            directive_user_origin=not bool(request.get("app", "")),
            # Same-name registration can change while the lock or child probe waits.
            replaced=lambda: _slot_replaced_while_queued(
                state, slot, name, request, "chat.slot_continue"
            ),
        )
    except SlotContinueRefusal as exc:
        if exc.code == "slot_not_found":
            return _slot_not_found()
        return web.json_response({"error": exc.message, "code": exc.code}, status=exc.status)
    return web.json_response({"ok": True, "slot": slot.key})


class SlotContinueRefusal(Exception):
    """A Continue that was refused, carrying the 409 the endpoint renders.

    Raised by :func:`continue_slot_turn` so the Continue endpoint and
    session-control's ``retry_target`` map the same refusals the same way.
    ``code`` is the machine-readable contract; ``message`` is advisory prose.
    """

    def __init__(self, message: str, code: str, status: int = 409) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


async def continue_slot_turn(
    state: DashboardState,
    slot: _ChatSlot,
    *,
    directive_user_origin: bool,
    via: str = "",
    require_interrupted: bool = False,
    before_dispatch: Callable[[], None] | None = None,
    extra_meta: dict[str, Any] | None = None,
    replaced: Callable[[], bool] | None = None,
) -> str:
    """Queue the Resume/Continue body at the head of *slot* and dispatch it.

    The mechanism behind ``api_chat_slot_continue``, shared with
    ``session_control.retry_target`` and ``session_control.continue_target`` so
    the button and the ``session_retry`` / ``session_continue`` tools run one
    path and refuse the same states. Returns which body was queued:
    ``"resumed"`` for the interrupted pick-up, ``"continued"`` for the carry-on
    message a cleanly finished slot gets. Authorization is the
    caller's: this function checks only whether the slot can take a
    continuation right now, and raises :class:`SlotContinueRefusal` when it
    cannot.

    ``require_interrupted`` refuses a slot whose last turn did not fail
    (``turn_not_failed``); Continue leaves it off. ``before_dispatch`` runs
    synchronously under the slot lock immediately before the queue insert and
    may raise :class:`SlotContinueRefusal`. ``via`` is recorded on the
    ``dashboard_continue`` audit line when set. ``extra_meta`` is merged into
    the queue entry's ``meta`` (session control stamps the sending slot there,
    so a drain-time drop is reported back to it). ``replaced`` is checked after
    each await (the lock, the sub-agent probe) and refuses with a 404
    ``slot_not_found`` when it returns True: the Continue endpoint passes
    ``_slot_replaced_while_queued`` for its slot name.
    """
    async with slot._lock:
        # Same-name registration can change while the lock or child probe waits.
        if replaced is not None and replaced():
            raise SlotContinueRefusal("not found", "slot_not_found", status=404)
        if slot.running:
            raise SlotContinueRefusal("slot is running", "slot_running")
        if slot._stopping or slot._stop_state != "idle":
            raise SlotContinueRefusal("a stop is in progress", "slot_stopping")
        if slot.queue_depth:
            # The runner is about to pick the thread back up on its own; adding a
            # continuation would double-fire.
            raise SlotContinueRefusal("queued messages pending", "slot_queue_pending")
        if any(not f.done() for f in slot._approval_futures.values()):
            raise SlotContinueRefusal("approval pending", "slot_approval_pending")
        # Background sub-agents are still running (or waiting to start) for this
        # slot. `slot.running` is False here — the parent turn ENDS while its
        # children keep going — so nothing above catches this, and the widened
        # gate below makes it the common shape rather than the rare one (before
        # this endpoint accepted a settled transcript, a parent that finished
        # cleanly after `spawn_run` was refused only incidentally, by
        # `_is_interrupted`).
        #
        # It has to be refused HERE rather than left to the queue: a synthetic
        # recovery entry satisfies `is_system_injection_item`, so
        # `_dequeue_next_system_message` drains it straight through the
        # `hold_users` gate that exists to stop exactly this (chat_runner) — the
        # hold only holds plain USER messages. A parent turn would start and
        # interleave tool calls and repository writes with its own children's
        # completion injections. `api_chat` queues instead of dispatching for the
        # same reason; Continue has nowhere to queue to, so it refuses.
        #
        # Children guard — see _subagents_attached_response for the three
        # probes and why each is load-bearing. `effective_session_key`, never
        # `f"dashboard:{slot.key}"`: a channel-born slot's children register
        # under the channel key, and the dashboard-prefixed form silently
        # matches nothing — `_history_key_for`'s own docstring says as much.
        subagents_denied = await _subagents_attached_response(
            state, slot, effective_session_key(slot), "continue"
        )
        if replaced is not None and replaced():
            raise SlotContinueRefusal("not found", "slot_not_found", status=404)
        if subagents_denied is not None:
            raise SlotContinueRefusal("sub-agents are running", "slot_subagents_running")
        if not _has_conversation(slot):
            raise SlotContinueRefusal("nothing to continue", "slot_empty")
        # The same session start has already failed twice in a row with nothing
        # but Resume presses between the attempts. Continue would re-issue the
        # identical ``session/new`` a third time: the turn has no registered
        # session, so it starts one, and nothing about the request changed
        # since the last two walls. This is the one refusal that reads the
        # transcript's CONTENT rather than its shape, and it is not a
        # readiness gate (see the docstring): the evidence is the slot's own
        # two tagged rows, not a probe that can be wrong forever, and typing a
        # message is still allowed -- a user row ends the streak, so a typed
        # retry that fails once gets its Resume back. The first failure keeps
        # today's behaviour exactly: one Resume, same continuation, same words.
        failures = session_start_failure_streak(slot.messages)
        if failures >= _SESSION_START_REPEAT_REFUSAL_AT:
            raise SlotContinueRefusal(
                f"the agent session failed to start {failures} times in a "
                "row; Resume would run the same start again. Restart the "
                "gateway (kirocrew restart), then send your message again.",
                "session_start_repeat",
            )

        # _is_interrupted does not AUTHORIZE the continue — it only picks which
        # body to inject. Both are true statements about their own case, and
        # getting this wrong is not cosmetic: telling a model that finished
        # cleanly that it was "interrupted before it finished" sends it looking
        # for half-done work that does not exist.
        interrupted = _is_interrupted(slot)
        if require_interrupted and not interrupted:
            # session_retry's own refusal: a turn that finished, or that the user
            # ended with Stop, is not a failure to re-run. Continue would carry on
            # from it, but a retry verb must never become a way for another
            # session to make this one produce a fresh answer on top of a good one.
            raise SlotContinueRefusal(
                "the last turn did not fail; there is nothing to retry",
                "turn_not_failed",
            )
        resume = _MANUAL_RESUME_MSG if interrupted else _MANUAL_CONTINUE_MSG
        if before_dispatch is not None:
            # Synchronous, under the lock, with no await between it and the
            # insert below: a caller whose authority was decided before the lock
            # (session control) re-checks it at the last point where refusing
            # still changes nothing.
            before_dispatch()
        # circular import: session_control imports this package's modules at module level.
        from kiro_crew.dashboard.session_control import containment_meta

        # Admission stamp + provenance: recovery-kind entries are subject
        # to drain re-validation like any other externally admitted content, and
        # provenance follows the CALLER — the same request-identity split as
        # api_chat. An app hitting Continue on its own slot must not gain the
        # authenticated-human flag that gates session-mutating effects.
        slot.queue_insert(
            0,
            resume,
            kind=SYNTHETIC_RECOVERY_KIND,
            meta={**containment_meta(state, slot), **(extra_meta or {})},
            directive_user_origin=directive_user_origin,
        )

    # The queue entry is already committed; audit failure must not strand it.
    try:
        sel().log_tool_invocation(
            session_key=_history_key_for(slot.key),
            agent=getattr(slot, "agent", "") or "kirocrew",
            source="dashboard",
            tool_name="dashboard_continue",
            tool_kind="command",
            outcome="ok",
            metadata={"slot": slot.key, **({"via": via} if via else {})},
        )
    except Exception:
        logger.warning("continue: SEL audit failed (slot %s)", slot.key, exc_info=True)
    started = await _start_next_queued_turn(state, slot)
    if not started:
        # Lost a race for the queue entry (a concurrent dequeue consumed it).
        # The turn is running either way, so this is not an error for the caller.
        logger.info("continue: queue entry consumed by a concurrent dequeue (slot %s)", slot.key)
    state.push_slots_update()
    return "resumed" if interrupted else "continued"


def session_start_failure_streak(messages: list[dict]) -> int:
    """How many session starts in a row failed at the tail of *messages*.

    Walks back from the newest row counting ``error`` rows stamped with
    ``SESSION_START_FAILED_KIND`` (the structural tag ``chat_runner`` writes
    from the exception, never from the prose). Every row that is not the
    conversation's floor is walked past -- the ``inject`` row a Resume press
    lands as, tool rows, notices -- so two failures separated only by the user
    pressing Resume are consecutive. The walk stops at the first row that IS
    new information: a user or assistant row with content (a typed retry is a
    new attempt and starts the count over), an error row of any OTHER kind (a
    connection-lost row is a different failure, not a third start), or a row
    that OPENS a turn of its own -- a nudge, a sub-agent completion, or an
    ``inject`` whose kind begins new work (``_TURN_OPENING_INJECT_KINDS``,
    the mirror of ``INJECT_KIND_OPENS_TURN`` in ``RecoveryCard.tsx``) -- since
    a failure before such a row belongs to a different turn and must not cost
    this turn its first Resume. A ``recovery`` inject resumes the SAME turn and
    is walked past, which is what makes two Resume-separated failures
    consecutive.

    The transcript is the count because nothing else can hold it: a start
    that never answered registered no session, so the per-session
    ``consecutive_failures`` counter in ``SessionManager.record_failure`` is
    never reached for it. Mirrors ``sessionStartFailureStreak`` in
    ``website/src/pages/chat/ErrorCard.tsx``; the two must agree, or the card
    hides a Resume the server would have honoured (or offers one it refuses).
    """
    streak = 0
    for m in reversed(messages):
        role = m.get("role")
        meta = m.get("meta")
        if role == "error":
            kind = meta.get("kind") if isinstance(meta, dict) else None
            if kind == SESSION_START_FAILED_KIND:
                streak += 1
                continue
            break
        if is_stop_event_row(m):
            break
        if role in ("nudge", "subagent"):
            break
        if role == "inject" and (
            isinstance(meta, dict) and meta.get("injectKind") in _TURN_OPENING_INJECT_KINDS
        ):
            break
        if role in ("user", "assistant") and m.get("content") and not is_system_notice(role, meta):
            break
    return streak


def _is_stop_event(m: dict) -> bool:
    """True when *m* is the card recorded because the user pressed Stop.

    Thin alias over ``state.is_stop_event_row`` — the predicate lives there
    (next to ``parse_cls_meta``, its one dependency) so the slot-summary
    builder can share it without importing this handler module.
    """
    return is_stop_event_row(m)


def _is_interrupted(slot: _ChatSlot) -> bool:
    """True when the transcript shows a turn that ended without a reply.

    Thin adapter over ``state.is_turn_interrupted``, which owns the scan and
    its contract (see its docstring). Shared with the slot-summary builder so
    the Continue endpoint, the composer's Resume gate, and the sidebar's
    ``interrupted`` field can never disagree about what an interruption is.
    """
    return is_turn_interrupted(slot.messages)


async def api_chat_slot_end_wait(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/end-wait — ask the sleeping `wait` tool to
    return early. Body: ``{"wait_id": "..."}``.

    Cooperative, and deliberately NOT a cancel. The tool sleeps in a separate
    MCP subprocess that runs no listener, so there is nothing to signal: the
    request is parked on the slot and collected by the tool on its next
    keepalive poll (see WAIT_PING_SECS — bounded at 5s). The turn then continues
    with a normal tool result, which is the whole point of not routing this
    through /stop: /stop can only end a wait as collateral of killing the
    session, losing in-flight results and paying a respawn.

    ``wait_id`` is required and must match the sleep currently in flight. That
    rejects the two races a slot-scoped flag would have accepted: a click landing
    after the wait already elapsed, and a click from a stale tab still showing a
    previous wait's countdown.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return _slot_not_found()
    denied = deny_app_slot_access(request.get("app", ""), slot, name, "slot_end_wait")
    if denied is not None:
        return denied
    body, body_err = await read_bounded_json(request, allow_absent=True)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    wait_id = str(body.get("wait_id") or "").strip()
    if not wait_id:
        return web.json_response(
            {"error": "wait_id required", "code": "wait_id_required"}, status=400
        )
    current = slot._wait_state or {}
    if current.get("wait_id") != wait_id:
        return web.json_response(
            {"error": "no such wait in flight", "code": "wait_not_in_flight"}, status=409
        )
    slot._end_wait_request = wait_id
    # The button, not a session: clears any requester a session_end_wait left
    # behind, so the woken tool reports the user as the one who ended it.
    slot._end_wait_by = ""
    sel().log_tool_invocation(
        session_key=_history_key_for(name),
        agent=getattr(slot, "agent", "") or "kirocrew",
        source="dashboard",
        tool_name="dashboard_end_wait",
        tool_kind="command",
        outcome="success",
        metadata={"slot": name, "wait_id": wait_id},
    )
    return web.json_response({"ok": True})
