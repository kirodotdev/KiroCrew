"""Run controls on a spawned run, and the fence that keeps them with their session.

``_spawn_scope_refusal`` admits an internal caller only to the runs its own
session started; the queued-spawn lookups answer for a run the gate accepted but
has not started; and the routes steer, release, report lost, mark collected,
retry, delete and stop-all.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING, Any, cast

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.messaging import (
        _COLLECTED_ID_MAX_LEN,
        _COLLECTED_IDS_CAP,
        _IDENTITY_LESS_RUN_CONTROL,
        DISMISSAL_FAILED,
        DISMISSAL_LOG_ABSENT,
        DISMISSAL_LOG_COMMITTED,
        DISMISSAL_LOG_FAILED,
        DISMISSAL_NO_FOLDER,
        NATIVE_CHILD_NOT_RESUMABLE,
        SUBAGENT_COMPLETION_KIND,
        SUBAGENT_COMPLETION_META_KEY,
        SUCCESSOR_UNKNOWN,
        DashboardState,
        QueuedReadUnavailable,
        QueuedRun,
        QueuedRunListing,
        _redact,
        _run_belongs_to_caller,
        _sel,
        _spawn_on_loop,
        dashboard_slot_key,
        effective_session_key,
        internal_memory_scope,
        logger,
        read_state,
        record_panel_dismissal_outcome,
        subagent_event_slot,
        warm_project_agents_for_spawn,
    )


async def _queued_run(state: DashboardState, run_id: str) -> QueuedRun | None:
    """The accepted spawn *run_id* when it has no run yet, else None.

    A spawn the gate deferred, or one waiting for a slot, exists only as a queue
    entry or a task-store row, so the registry and the run folders cannot name
    it. Without this read a caller that was just told "queued" is then told the
    run does not exist. A read that fails answers None, which leaves the caller
    where it was before this lookup existed.
    """
    if state.subagents is None:
        return None
    try:
        return await state.subagents.queued_run_async(run_id)
    except QueuedReadUnavailable:
        raise  # "not queued" is unknowable: the caller answers 503, never 404
    except Exception:
        logger.debug("Queued-run lookup failed for %s", run_id, exc_info=True)
        return None


def _queue_unreadable() -> web.Response:
    """503 for a lookup the task store could not answer: transient, retry it."""
    return web.json_response(
        {"error": "the task queue is unreadable; retry shortly", "code": "taskq_unavailable"},
        status=503,
        headers={"Retry-After": "2"},
    )


async def _queued_runs(
    state: DashboardState, parent: str | None, *, app: str | None = None
) -> QueuedRunListing:
    """:func:`_queued_run` for a listing: *parent*'s queued spawns (None = all).

    A failed read is a PARTIAL listing, never an empty one: an empty answer
    reads as "nothing queued", the reading that gets accepted work dispatched
    twice.
    """
    if state.subagents is None:
        return QueuedRunListing(())
    try:
        return await state.subagents.queued_runs_async(parent, app=app)
    except Exception:
        logger.debug("Queued-run listing failed", exc_info=True)
        return QueuedRunListing((), partial=True)


async def _queued_lookup(
    request: web.Request, state: DashboardState, run_id: str
) -> QueuedRun | None:
    """The guard's queued lookup for this request, or a fresh one."""
    if "spawn_queued_lookup" in request:
        return cast("QueuedRun | None", request["spawn_queued_lookup"])
    return await _queued_run(state, run_id)  # QueuedReadUnavailable: the caller's 503


def _queued_not_started() -> web.Response:
    """409 for a control that needs a run, aimed at a spawn still queued.

    The same id answers ``queued`` on ``GET /api/spawn/{id}``, so "not found"
    here would contradict it.
    """
    return web.json_response(
        {"error": "queued — not started", "code": "queued_not_started"}, status=409
    )


def _queued_run_payload(queued: QueuedRun) -> dict[str, object]:
    """The wire shape of a queued spawn, shared by the status and list routes.

    ``done: false`` with ``queued: true`` and no transcript: the run has not
    started, so there are no turns and no partial text. ``reason`` and
    ``reason_detail`` are present only when known, the same fields the accept
    answer (``POST /api/spawn``) carries for a deferred spawn.
    """
    data: dict[str, object] = {
        "id": queued.id,
        "task": _redact(queued.task),
        "done": False,
        # Both spellings: ``status`` matches the accept answer
        # (``POST /api/spawn``), ``queued`` is what the poll loops read.
        "status": "queued",
        "queued": True,
        "agent": _redact(queued.agent),
    }
    if queued.accepted_at > 0:
        data["started"] = queued.accepted_at
        data["elapsed"] = max(0, round(time.time() - queued.accepted_at))
    if queued.reason:
        data["reason"] = queued.reason
    if queued.reason_detail:
        data["reason_detail"] = _redact(queued.reason_detail)
    if queued.resuming:
        # It STARTED and waits to go on (after a restart, or to retry): queued,
        # but never "not started".
        data["resuming"] = True
        data["resuming_reason"] = queued.resuming
    return data


async def _spawn_scope_refusal(
    request: web.Request, *, claimed_session: str | None = None
) -> web.Response | None:
    """Keep run controls with their originating session, regardless of target member.

    Every INTERNAL caller (kiro-cli's MCP servers, the CLI) takes the ownership
    check, whatever memory store its identity resolved to and whether it resolved
    one at all: a verified Global-memory session is still only the owner of its
    own runs, and a caller that presented no ``X-Session-Key`` owns no run a
    session started. Only the dashboard owner (cookie auth, no ``internal_auth``)
    is admitted without it, because that surface IS the owner. Refusals answer
    404 ``task_scope_denied`` so a run id is never confirmed to a caller that may
    not see it; the identity-less refusal says why, since a wrong run id and a
    missing identity are indistinguishable from the caller's side otherwise.
    """
    scope, refusal = await internal_memory_scope(
        request, "spawn.access", claimed_session=claimed_session
    )
    if refusal is not None:
        return refusal
    if request.get("internal_auth") is not True:
        return None  # the dashboard owner's own surface
    caller = request.headers.get("X-Session-Key", "")
    state = request.app["state"]
    run_id = request.match_info["agent_id"]
    info = state.subagents.get(run_id) if state.subagents else None
    queued: QueuedRun | None = None
    if info is None and state.subagents:
        # Kept on the request, None included, so the handler does not ask the
        # store again.
        try:
            queued = request["spawn_queued_lookup"] = await _queued_run(state, run_id)
        except QueuedReadUnavailable:
            return _queue_unreadable()
    parent: object
    # One order, stated once: the live run, then a spawn the gate is still
    # holding, then the persisted record, then a harness-native card.
    if info is not None:
        parent = info.parent_session_key
    elif queued is not None:
        # A spawn the gate is still holding has no run folder yet; its row's
        # session key is the originating session, the field the live branch
        # reads from ``info``.
        parent = queued.parent_session_key
    elif (record := await asyncio.to_thread(read_state, run_id)) is not None:
        # The persisted record spells the field ``parent_session``
        # (``subagent_persistence.write_state``). A record that lacks it is an
        # unknown owner, not a parentless run: ``None`` stays ``None``.
        parent = record.get("parent_session")
    else:
        # A harness-native child has no managed run and no persisted record; its
        # ownership is the dashboard slot that tracks its card. Anything else
        # unknown stays ``None`` and is refused.
        card = (getattr(state, "_native_cards", None) or {}).get(run_id)
        # The card stores the bare slot key (``_register_native_card``); the
        # caller's identity is that slot's session key, ``dashboard:<slot>``.
        slot = card.get("slot") if isinstance(card, dict) else None
        parent = f"dashboard:{slot}" if isinstance(slot, str) and slot else None
    if _run_belongs_to_caller(caller, run_id, parent):
        return None
    _sel().log_api_access(
        caller=caller or "internal",
        operation="spawn.access",
        outcome="denied",
        source="subagent",
        error=(
            "The run belongs to another originating session."
            if caller
            else "The caller presented no session identity."
        ),
        resources=f"run={run_id} scope={'private' if scope else 'global'}",
    )
    return web.json_response(
        {
            "error": "not found" if caller else _IDENTITY_LESS_RUN_CONTROL,
            "code": "task_scope_denied",
        },
        status=404,
    )


def _native_child_refusal(state: "DashboardState", conversation_id: str) -> str | None:
    """Typed reason when *conversation_id* is a harness-native child of a live
    session (kiro-cli ``use_subagent`` / KAS subtask), else None."""
    probe = getattr(state.subagents, "native_child_resume_refusal", None)
    if not callable(probe):
        return None
    try:
        reason = probe(conversation_id)
    except Exception:  # noqa: BLE001 - advisory lookup
        return None
    # A typed refusal is a str with the known prefix; anything else (a test
    # double's attribute, a stray object) is not a refusal.
    if isinstance(reason, str) and reason.startswith(NATIVE_CHILD_NOT_RESUMABLE):
        return reason
    return None


async def api_spawn_steer(request: web.Request) -> web.Response:
    """POST /api/spawn/{agent_id}/steer — inject into a RUNNING run's turn.

    Body: ``{message, mode?}``. ``mode="interrupt"`` (default) injects into
    the running turn; ``mode="follow_up"`` queues the message for delivery as
    a continuation AFTER the run's current turn completes (never interrupts).
    """
    state: DashboardState = request.app["state"]
    if not state.subagents:
        return web.json_response(
            {"error": "subagents not available", "code": "subagents_unavailable"},
            status=503,
        )
    agent_id = request.match_info["agent_id"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    message = str(body.get("message", "") or "").strip()
    if not message:
        return web.json_response(
            {"error": "message is required", "code": "message_required"}, status=400
        )
    mode = str(body.get("mode", "") or "interrupt").strip()
    if mode not in ("interrupt", "follow_up"):
        return web.json_response(
            {"error": "mode must be 'interrupt' or 'follow_up'", "code": "invalid_mode"},
            status=400,
        )
    if mode == "follow_up":
        ok, detail = await state.subagents.follow_up_run(agent_id, message)
    else:
        ok, detail = await state.subagents.steer_run(agent_id, message)
    if not ok:
        if detail == "not_found":
            native = _native_child_refusal(state, agent_id)
            if native is not None:
                return web.json_response(
                    {"error": native, "code": NATIVE_CHILD_NOT_RESUMABLE}, status=409
                )
            try:
                if await _queued_lookup(request, state, agent_id) is not None:
                    return _queued_not_started()
            except QueuedReadUnavailable:
                return _queue_unreadable()
            return web.json_response({"error": detail, "code": "not_found"}, status=404)
        if detail.startswith("not_running"):
            return web.json_response({"error": detail, "code": "not_running"}, status=409)
        if detail.startswith("session_starting"):
            # Transient: the run is alive but its session has not registered
            # yet. 503 + Retry-After tells clients to retry, unlike
            # the terminal 502 steer_failed.
            return web.json_response(
                {"error": detail, "code": "session_starting"},
                status=503,
                headers={"Retry-After": "5"},
            )
        return web.json_response({"error": detail, "code": "steer_failed"}, status=502)
    return web.json_response(
        {"id": agent_id, "status": "follow_up_queued" if mode == "follow_up" else "steered"}
    )


async def api_spawn_release(request: web.Request) -> web.Response:
    """POST /api/spawn/{agent_id}/release — end a continuable conversation.

    Deletes the persisted session mapping and the on-disk session files.
    Refuses while a run is in flight on the conversation.
    """
    state: DashboardState = request.app["state"]
    if not state.subagents:
        return web.json_response(
            {"error": "subagents not available", "code": "subagents_unavailable"},
            status=503,
        )
    conv_id = request.match_info["agent_id"]
    ok, detail = await state.subagents.release_conversation_async(conv_id)
    if not ok:
        if detail.startswith("conversation_busy"):
            return web.json_response({"error": detail, "code": "conversation_busy"}, status=409)
        return web.json_response({"error": detail, "code": "conversation_gone"}, status=404)
    return web.json_response({"conversation": conv_id, "status": "released"})


async def api_spawn_lost(request: web.Request) -> web.Response:
    """POST /api/spawn/lost — reconcile a batch member whose spawn POST failed.

    Called by ``spawn_run`` (mcp_core) when a member was explicitly rejected
    BEFORE ``mgr.spawn`` ran (validation 400 / 503), so the response carried
    no ``counted`` flag. Every sibling's ``batch_total`` already counts the
    lost member, so without this reconcile the wave's ``submitted < expected``
    forever and held digest results strand until restart (Opus MEDIUM + Design
    Review CONCERN 1).

    Transport failures are excluded because the gateway may have accepted the
    member before its response failed; reconciling that member as lost could
    close the wave early. The stuck-wave sweep safely handles truly lost
    transport submissions after its grace period.
    """
    state: DashboardState = request.app["state"]
    if not state.subagents:
        return web.json_response({"error": "subagents not available"}, status=503)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    batch_id = str(body.get("batch_id", "") or "")[:32]
    if not batch_id or not batch_id.isalnum():
        return web.json_response({"error": "valid batch_id required"}, status=400)
    try:
        batch_total = max(0, min(int(body.get("batch_total", 0) or 0), 1000))
    except (TypeError, ValueError):
        batch_total = 0
    reason = str(body.get("reason", "") or "spawn submission failed")[:300]
    parent_session = str(body.get("parent_session", "") or "")
    _, refusal = await internal_memory_scope(request, "spawn.batch", claimed_session=parent_session)
    if refusal is not None:
        return refusal
    state.subagents.record_lost_submission(
        batch_id, batch_total, reason, parent_session_key=parent_session
    )
    return web.json_response({"status": "reconciled", "batch_id": batch_id})


async def api_spawn_mark_collected(request: web.Request) -> web.Response:
    """POST /api/spawn/mark-collected — suppress injection for blocking tool.

    Called by the spawn_sub_agents MCP tool after it has polled and collected
    results inline.  Records the agent IDs on the parent slot so that the
    subsequent _subagent_done callback skips the _run_chat injection (the model
    already processed these results as a tool-call return value).  Without this,
    each completion event triggers a redundant LLM turn whose response shadows
    any [OPTIONS:] buttons the synthesis message rendered.
    """
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    ids = body.get("ids")
    if not ids or not isinstance(ids, list):
        return web.json_response(
            {"error": "'ids' array required", "code": "ids_required"}, status=400
        )
    parent_session = str(body.get("parent_session", "") or "")
    _, refusal = await internal_memory_scope(request, "spawn.batch", claimed_session=parent_session)
    if refusal is not None:
        return refusal
    slot_name = dashboard_slot_key(parent_session)
    if not slot_name:
        return web.json_response({"status": "no_slot"})
    slot = state.get_slot(slot_name)
    if not slot:
        return web.json_response({"status": "no_slot"})
    # Record the IDs (bounded to 200 to prevent unbounded growth). A member whose
    # completion is already QUEUED on the slot (its delivery timed out waiting on
    # this tool's turn) is settled here instead: the queued announce is removed
    # so it never plays as a redundant turn, and its owed delivery marks are
    # written now, since the tool's return value IS the consumption. Its id is
    # kept out of the set, where nothing would ever discard it again.
    wanted = {
        aid
        for aid in ids[:200]
        if isinstance(aid, str) and 0 < len(aid) <= _COLLECTED_ID_MAX_LEN
        # Only ids this gateway knows: nothing else will ever discard them.
        and (state.subagents is None or state.subagents.get(aid) is not None)
    }
    owed: list[Any] = []
    for item in list(slot._queue):
        meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
        card = meta.get(SUBAGENT_COMPLETION_META_KEY) if meta else None
        aid = card.get("agentId") if isinstance(card, dict) and card.get("kind") == "single" else ""
        if item.get("kind") != SUBAGENT_COMPLETION_KIND or aid not in wanted:
            continue
        if slot.queue_remove_by_id(str(item.get("id") or "")) is None:
            continue
        wanted.discard(aid)
        try:
            owed.extend(slot.take_pending_subagent_deliveries([str(item.get("content") or "")]))
        except Exception:
            logger.debug("Could not claim delivery marks for %s", aid, exc_info=True)
    room = max(0, _COLLECTED_IDS_CAP - len(slot._subagents_inline_collected))
    slot._subagents_inline_collected.update(sorted(wanted)[:room])
    if owed and state.subagents is not None:
        try:
            await state.subagents.settle_queued_delivery(owed)
        except Exception:
            logger.debug("Could not settle inline-collected deliveries", exc_info=True)
    state.push_slots_update()
    return web.json_response({"status": "ok", "marked": len(ids)})


async def api_spawn_retry(request: web.Request) -> web.Response:
    """POST /api/spawn/{agent_id}/retry — re-spawn a FAILED subagent's task.

    Backs the chip's "Retry failed (N)" batch control. Only terminal failed
    agents are retryable (never running ones — that would double the work —
    and never user-stopped ones — the user killed that work on purpose).
    Spawns a fresh agent with the original task/agent/parent (new id; the old
    terminal card stays for history). Batch identity is NOT carried over: the
    retry is a standalone spawn, so a wave's digest accounting (already
    completed) is never reopened.
    """
    state: DashboardState = request.app["state"]
    if not state.subagents:
        return web.json_response({"error": "subagents not available"}, status=503)
    agent_id = request.match_info["agent_id"]
    if agent_id.startswith("native:"):
        return web.json_response(
            {"error": "native subagents run inside the parent turn and cannot be retried"},
            status=400,
        )
    old = state.subagents.get(agent_id)
    if not old:
        try:
            if await _queued_lookup(request, state, agent_id) is not None:
                return _queued_not_started()
        except QueuedReadUnavailable:
            return _queue_unreadable()
        return web.json_response({"error": "not found"}, status=404)
    if not old.done:
        return web.json_response({"error": "agent is still running"}, status=409)
    if old.outcome != "failed":
        return web.json_response(
            {"error": f"only failed agents can be retried (outcome={old.outcome})"},
            status=409,
        )
    # Claimed before the first await, on the manager that also guards the
    # continuation side: two retries arriving together (two tabs, a double
    # click) and a retry racing a spawn_continue cannot both start work.
    successor = state.subagents.claim_retry(old)
    if isinstance(successor, str) and successor:
        return web.json_response(
            {
                "error": (
                    f"run {agent_id} was already picked up by run {successor}; "
                    "retrying it would run its task a second time"
                ),
                "code": "retry_superseded",
            },
            status=409,
        )
    try:
        return await _retry_failed_run(state, agent_id, old)
    finally:
        # Releases a claim still pending: every path that started nothing. A
        # landed start was settled with its id, and a spawn that raised with
        # SUCCESSOR_UNKNOWN; this call leaves both alone.
        state.subagents.settle_retry(old, None)


async def _retry_failed_run(state: "DashboardState", agent_id: str, old: Any) -> web.Response:
    """Start the replacement run for a failed *old*; the checks are the caller's."""
    assert state.subagents is not None
    execution = old.execution_context
    if execution is None:
        from kiro_crew.subagent_persistence import read_run_execution

        try:
            execution = await asyncio.to_thread(read_run_execution, old.id)
        except (OSError, ValueError) as exc:
            return web.json_response(
                {"error": f"memory_unavailable: {exc}", "code": "memory_unavailable"}, status=400
            )
    # Same validated warm as the primary spawn handler. old.cwd was validated
    # at the ORIGINAL spawn, but the allowlist may have changed since (and a
    # gateway restart leaves the cache cold), so it is re-checked against the
    # current config before any discovery read.
    if old.agent:
        await warm_project_agents_for_spawn(state, old.cwd or "")
    start = _spawn_on_loop(
        state,
        old._raw_task or old.task,
        parent_session_key=old.parent_session_key,
        agent=old.agent,
        max_turns=old.max_turns,
        cwd=old.cwd,
        model=old.model or None,
        # Like model and the context groups: a retry must run at the SAME
        # effort as the run it replaces, or it is a different experiment.
        reasoning_effort=old.reasoning_effort,
        # And on the same backend: a retry on the default harness would be a
        # different experiment, and a quiet one.
        backend=getattr(old, "backend", None),
        approval_mode=old.approval_mode or None,
        silent=old.silent,
        # A retry must see the SAME context scope as the run it replaces —
        # otherwise the retried agent is a different experiment.
        delegation=dict(old.delegation),
        include_memory=old.include_memory,
        include_lessons=old.include_lessons,
        include_project=old.include_project,
        # Same scope the original ran under. Omitting it makes a retry widen to
        # the global store, so the failure mode is "retrying a delegation leaks
        # it" -- and a retry is exactly when nobody re-reads the scope.
        memory_store=old.memory_store,
        crew=old.crew,
        app=execution.app,
        _memory_mode=execution.memory_mode,
        _execution_context=execution.to_record(),
    )
    try:
        info = await start
    except BaseException:
        # The spawn may have accepted its durable row before it raised (a
        # cancelled request included), so whether a successor exists is unknown:
        # the claim stays taken rather than letting a second retry run the task.
        state.subagents.settle_retry(old, SUCCESSOR_UNKNOWN)
        raise
    if not info:
        return web.json_response(
            {"error": f"capacity reached ({state.subagents.max_concurrent})"}, status=429
        )
    if info.done and info.error:
        return web.json_response({"error": info.error}, status=400)
    state.subagents.settle_retry(old, info.id)
    logger.info("Subagent %s retried as %s (POST /api/spawn/{id}/retry)", agent_id, info.id)
    return web.json_response({"id": info.id, "retried_from": agent_id, "status": "spawned"})


async def _log_panel_dismissal(state: DashboardState, agent_id: str) -> str:
    """Record a panel dismissal in the owning session's crew log.

    Answers one of :data:`DISMISSAL_LOG_ABSENT`,
    :data:`DISMISSAL_LOG_COMMITTED` or :data:`DISMISSAL_LOG_FAILED`. The last two
    are the distinction a boolean could not carry: both mean a unit holds this
    child, so the run exists and an unknown-id answer would be wrong, while only
    one of them means the card is actually cleared.

    This is the record the PANEL reads. Its durable half is a fold of the session's
    crew log, so the dismissal belongs there: kept anywhere else it is a second
    record of a fact about the session, reclaimed on its own schedule. The folder
    registry below this is the case in point -- it is keyed on the run's folder at
    both ends, so it forgets a dismissal when that folder is pruned, while the fold
    still draws the child the log kept.

    The owning UNIT is resolved in two steps, cheapest first.
    :func:`crew_log.emit.dismiss_child` answers from the emitter's own spawn pin,
    which is the unit the child's ``subagent/spawned`` was written to; the terminal
    report releases that pin, so it answers only for a child still running, and a
    gateway restart clears it entirely. Then the LOG is searched, over the units
    each live slot has run under -- which is the same question the panel's own read
    answers, and the only one that cannot name the wrong unit: a slot owns one ACP
    session id at a time, so a child dispatched before a reset sits in a retired
    unit that the slot's current id does not name.

    ``ABSENT`` when no unit holds the child, which is not a failure: there is then
    no folded card to clear, and the caller's folder record still answers for a
    pre-existing one. ``ABSENT`` also when the crew log is switched off, where an
    install that opted out of the record has no record to write to.

    Each append is waited on until it COMMITS, because the caller publishes a
    dismissal to the user on the strength of this answer, and once the run's folder
    has been reclaimed this entry is the only record of it.
    """
    try:
        from kiro_crew.crew_log import emit as crew_log_emit
        from kiro_crew.crew_log.resolve import UnitSearchFailed, unit_holding_child

        if not crew_log_emit.enabled():
            return DISMISSAL_LOG_ABSENT

        loop_wait = crew_log_emit.awaiting_commit

        async def _committed(emit_one) -> bool:
            return await loop_wait(emit_one, what=f"the panel dismissal for {agent_id}")

        pinned = False

        def _via_pin(on_settled) -> None:
            nonlocal pinned
            pinned = bool(crew_log_emit.dismiss_child(agent_id, on_settled=on_settled))
            if not pinned:
                # No pin, so nothing was queued and nothing will settle. Resolved
                # here rather than left to the timeout, which would spend the whole
                # bound before the unit search that is the real answer.
                on_settled(False)

        if await _committed(_via_pin):
            return DISMISSAL_LOG_COMMITTED
        if pinned:
            # The pin named the unit, so the run exists, and its append did not
            # commit. Searching the units would only queue a second entry into the
            # same wedged writer; the caller's retry is what should decide.
            return DISMISSAL_LOG_FAILED

        slots = [
            subagent_event_slot(effective_session_key(slot))
            for slot in list(getattr(state, "_slots", {}).values())
        ]
        for slot_key in slots:
            try:
                unit = await asyncio.to_thread(unit_holding_child, slot_key, agent_id)
            except UnitSearchFailed:
                # The store would not say whether this slot holds the child. Reading
                # that as "this slot does not" would let the loop finish and answer
                # ABSENT, which the caller turns into a 404 or a success -- an
                # obligation reported discharged that was never looked for.
                logger.debug(
                    "crew log: the unit search for %s under %s failed",
                    agent_id,
                    slot_key,
                    exc_info=True,
                )
                return DISMISSAL_LOG_FAILED
            if unit:
                landed = await _committed(
                    lambda on_settled, _unit=unit: crew_log_emit.on_subagent_dismissed(
                        _unit, agent_id=agent_id, on_settled=on_settled
                    )
                )
                return DISMISSAL_LOG_COMMITTED if landed else DISMISSAL_LOG_FAILED
        return DISMISSAL_LOG_ABSENT
    except Exception:
        # FAILED, not ABSENT. Everything that reaches here is a store or emitter
        # fault, and the caller turns ABSENT into a 404 or a plain success -- an
        # obligation reported discharged that was never looked for. The one case
        # that genuinely owes no record, a switched-off emitter, returns above
        # before anything here can fail.
        logger.debug("crew log: recording a panel dismissal failed", exc_info=True)
        return DISMISSAL_LOG_FAILED


async def api_spawn_delete(request: web.Request) -> web.Response:
    """DELETE /api/spawn/{agent_id} — cancel a running subagent or remove a finished one."""
    state: DashboardState = request.app["state"]
    agent_id = request.match_info["agent_id"]
    # Handle native kiro-cli subagents (native:* IDs not in SubagentManager)
    if agent_id.startswith("native:") and hasattr(state, "_native_cards"):
        card_info = getattr(state, "_native_cards", {}).get(agent_id)
        if card_info:
            # Can't actually kill the kiro-cli internal sub-agent, but we can
            # close the Activity card so it stops showing "Starting..."
            state._native_cards.pop(agent_id, None)
            # Persist the stop on the slot-owned tracker record so WS replay
            # (native_subagent_snapshots) reconstructs this card as STOPPED for
            # reconnecting clients — not as still-running or completed.
            try:
                _slot = state.get_slot(card_info["slot"])
                _rec = (
                    _slot._native_subagent_tracker.get(card_info.get("session_id", ""))
                    if _slot is not None
                    else None
                )
                if _rec is not None and not _rec.get("done"):
                    _rec["done"] = True
                    _rec["done_at"] = time.time()
                    _rec["elapsed"] = time.time() - card_info.get("started", time.time())
                    _rec["error"] = None
                    _rec["stopped"] = True
                    _rec["outcome"] = "stopped"
                    _rec["result"] = "(cancelled)"
            except Exception:
                logger.debug("native cancel: tracker update failed for %s", agent_id, exc_info=True)
            # User-initiated cancellation is an auditable action (parity with
            # the managed path, which audits inside SubagentManager.cancel()).
            try:
                _sel().log_tool_invocation(
                    session_key=card_info["slot"],
                    source="subagent",
                    tool_name="cancel_native_subagent",
                    outcome="cancelled_by_user",
                    metadata={"card_id": agent_id},
                )
            except Exception:
                logger.debug("SEL audit failed for native cancel %s", agent_id, exc_info=True)
            state.broadcast_ws(
                "subagent_done",
                {
                    "id": agent_id,
                    "slot": card_info["slot"],
                    "elapsed": time.time() - card_info.get("started", time.time()),
                    "error": None,
                    "stopped": True,
                    "task": "",
                    "agent": "",
                    "result": "(cancelled)",
                },
            )
            return web.json_response({"ok": True, "cancelled": True})
        return web.json_response({"error": "not found"}, status=404)
    manager = state.subagents
    info = manager.get(agent_id) if manager is not None else None
    if manager is None or info is None:
        # A card the durable replay rebuilt has no manager entry -- after a
        # gateway restart that is every finished run -- so a flat 404 made those
        # cards undismissable: the delete failed, the banner showed, and the next
        # reconnect sent the card again. The dismissal is recorded against the
        # folder instead, which is the same record the live path writes.
        #
        # Dashboard owner only. That is exactly as wide as what the owner can
        # already see -- their sockets short-circuit visibility to every live slot
        # -- so no caller gains reach over a run it could not list. An app token
        # is refused rather than handed a route into another app's runs, and an
        # absent claim means the request never passed the auth middleware.
        request_app = request.get("app", "")
        if "app" not in request or request_app:
            _sel().log_api_access(
                caller=request_app or "unknown",
                operation="spawn.dismiss",
                outcome="denied",
                source="app_isolation",
                resources="dashboard-only dismissal of a persisted run",
                error="app tokens cannot dismiss persisted subagent runs",
            )
            return web.json_response(
                {"error": "app token not allowed", "code": "app_token_forbidden"}, status=403
            )
        outcome = await asyncio.to_thread(record_panel_dismissal_outcome, agent_id)
        # The dismissal the PANEL reads: an entry in the owning session's crew log.
        # The panel's durable half is a fold of that log, so this is the record that
        # keeps the card cleared; the folder registry above is kept because
        # ``GET /api/spawn`` still reads the folders, and because a dismissal made
        # before this entry type existed lives only there.
        logged = await _log_panel_dismissal(state, agent_id)
        if outcome == DISMISSAL_NO_FOLDER and logged == DISMISSAL_LOG_ABSENT:
            # Nothing durable can rebuild this card -- no folder, and no log that
            # records the child -- so there is no run here to speak of. Same answer
            # as before for a truly unknown id.
            return web.json_response({"error": "not found"}, status=404)
        if outcome == DISMISSAL_FAILED or logged == DISMISSAL_LOG_FAILED:
            # One of the two records was owed and did not land, so the dismissal is
            # at best partial and the card comes back on a reader that still holds
            # its own record. The folder half feeds ``GET /api/spawn``; the log half
            # feeds the panel, and once the run's folder is reclaimed it is the only
            # record there is. Answering ok would claim a dismissal a reader goes on
            # contradicting, with nothing anywhere saying which half failed, so the
            # caller cannot retry the one that did not land.
            _sel().log_api_access(
                caller="internal",
                operation="spawn.dismiss",
                outcome="denied",
                source="subagent",
                resources=f"persisted run {agent_id}",
                error="the dismissal record could not be written",
            )
            return web.json_response(
                {"error": "dismissal not recorded", "code": "dismissal_unwritable"}, status=503
            )
        _sel().log_api_access(
            caller="internal",
            operation="spawn.dismiss",
            outcome="allowed",
            source="subagent",
            resources=f"persisted run {agent_id}",
        )
        return web.json_response({"ok": True, "cancelled": False, "dismissed": True})
    cancelled = await manager.cancel(agent_id)
    if not cancelled:
        settlement = await manager.settle_before_delete(agent_id)
        if settlement == "pending":
            return web.json_response(
                {
                    "error": "completion delivery is still pending",
                    "code": "completion_delivery_pending",
                },
                status=409,
            )
    return web.json_response({"ok": True, "cancelled": cancelled})


async def api_spawn_stop_all(request: web.Request) -> web.Response:
    """POST /api/spawn/stop-all — stop one chat's running and queued subagents."""
    state: DashboardState = request.app["state"]
    request_app = request.get("app", "")
    if "app" not in request or request_app:
        _sel().log_api_access(
            caller=request_app or "unknown",
            operation="spawn.stop_all",
            outcome="denied",
            source="app_isolation",
            resources="dashboard-only bulk cancellation",
            error="app tokens cannot stop dashboard subagent waves",
        )
        return web.json_response(
            {"error": "app token not allowed", "code": "app_token_forbidden"}, status=403
        )
    if not state.subagents:
        return web.json_response(
            {"error": "subagents not available", "code": "subagents_unavailable"},
            status=503,
        )
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    slot_name = body.get("slot") if isinstance(body, dict) else None
    if not isinstance(slot_name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,256}", slot_name):
        return web.json_response(
            {"error": "valid slot is required", "code": "invalid_slot"}, status=400
        )
    slot = state.get_slot(slot_name)
    if slot is None:
        return web.json_response({"error": "slot not found", "code": "slot_not_found"}, status=404)
    _, refusal = await internal_memory_scope(
        request, "spawn.stop_all", claimed_session=effective_session_key(slot)
    )
    if refusal is not None:
        return refusal
    running, queued = await state.subagents.cancel_for_parent(effective_session_key(slot))
    return web.json_response(
        {"ok": True, "stopped": running + queued, "running": running, "queued": queued}
    )
