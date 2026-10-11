"""``POST /api/spawn`` and ``POST /api/spawn/{id}/continue``: admitting a run.

The request's memory mode, the parent slot's stage-boundary owner, and the
hand-off to the subagent manager that keeps the task store off the event loop.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.messaging import (
        _SPAWN_REJECTED_CODE,
        DEFERRED_QUEUED_REASONS,
        NATIVE_CHILD_NOT_RESUMABLE,
        SPAWN_RUN_SCHEMA,
        DashboardState,
        KiroCrewConfig,
        ValidationError,
        _oversized_identity_refusal,
        _redact,
        _spawn_scope_refusal,
        dashboard_slot_key,
        effective_session_key,
        effort_applied_note,
        effort_drop_reason,
        internal_memory_scope,
        oversized_identity,
        parent_spawn_allowlists,
        parent_work_supported,
        validate_tool_args,
        warm_project_agents_for_spawn,
    )


async def _spawn_request_memory_mode(
    state: DashboardState, request: web.Request, parent: str
) -> str:
    from kiro_crew.dashboard.handlers._shared import resolve_session_memory_mode
    from kiro_crew.messaging.privacy_mode import strictest

    parent_mode = await resolve_session_memory_mode(state, parent)
    caller = request.headers.get("X-Session-Key", "")
    caller_mode = (
        parent_mode if caller == parent else await resolve_session_memory_mode(state, caller)
    )
    return strictest((parent_mode, caller_mode)) or "persistent"


def _slot_for_parent(state: DashboardState, parent: str) -> Any | None:
    """Return the slot a parent session key names, or one bound to it."""
    slots = getattr(state, "_slots", None)
    if not isinstance(slots, dict):
        return None
    slot_name = dashboard_slot_key(parent)
    if not slot_name and parent.startswith("dashboard:"):
        slot_name = parent.removeprefix("dashboard:")
    canonical = slots.get(slot_name) if slot_name else None
    same_parent = tuple(
        candidate
        for candidate in slots.values()
        if candidate is canonical or effective_session_key(candidate) == parent
    )
    return canonical or (same_parent[0] if same_parent else None)


async def api_spawn(request: web.Request) -> web.Response:
    """POST /api/spawn — spawn a subagent.

    Invariant: every error returned after ``state.subagents.spawn`` is called
    must include ``counted: true``. The manager counts submissions on entry;
    omitting the flag would make ``spawn_run`` reconcile the member again and
    could close a batch wave early.
    """
    # Owner identity is a property of a dashboard-user request: ``app == ""`` is
    # the class ``is_owner_dashboard_request`` can rule on at all. The other two
    # caller classes keep the control that already governs them -- an
    # ``X-Internal-Secret`` loopback process is admitted by the constant-time
    # secret match and reaches here with ``app`` ABSENT, and an app token is
    # confined to its manifest's declared paths by ``_enforce_app_scope``.
    if request.get("internal_auth") is not True and request.get("app") == "":
        # Body-scope import, like the sibling gates in this package
        # (``connections.py``, ``mcp_apps.py``, ``files.py``): ``source_providers``
        # reaches back into sibling handler modules, so importing the helper at
        # module scope from here would close a cycle.
        from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

        owner_denied = await require_owner_dashboard_request(request, "spawn.create")
        if owner_denied is not None:
            return owner_denied
    state: DashboardState = request.app["state"]
    if not state.subagents:
        return web.json_response({"error": "subagents not available"}, status=503)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    try:
        cleaned = validate_tool_args(
            {
                "task": body.get("task", ""),
                "agent": body.get("agent", ""),
                "max_turns": body.get("max_turns", 0),
                "cwd": body.get("cwd", ""),
                "model": body.get("model", ""),
                "reasoning_effort": body.get("reasoning_effort", ""),
                "include_memory": body.get("include_memory", True),
                "include_lessons": body.get("include_lessons", True),
                "include_project": body.get("include_project", True),
                # The dict is CLOSED -- validate_tool_args only sees what is
                # listed here -- so omitting a schema field silently disables it
                # rather than failing. That is what made the crew delegation
                # below unreachable: the block, its unknown_crew refusal and its
                # store resolution all ran off a value that was always None.
                "crew": body.get("crew", ""),
                "target_member": body.get("target_member", ""),
            },
            SPAWN_RUN_SCHEMA,
        )
    except ValidationError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    task = (cleaned.get("task") or "").strip()
    if not task:
        return web.json_response({"error": "task is required"}, status=400)
    parent_session = body.get("parent_session", "")
    if not isinstance(parent_session, str):
        return web.json_response(
            {"error": "parent_session must be a string", "code": "invalid_parent_session"},
            status=400,
        )
    inline_call = body.get("inline_call", "")
    if not isinstance(inline_call, str):
        return web.json_response(
            {"error": "inline_call must be a string", "code": "invalid_inline_call"},
            status=400,
        )
    if body.get("inline_collect") is True:
        # The inline-collection registry would keep the parent key and the call
        # id: each is refused whole, before anything is retained, never truncated.
        field = oversized_identity(parent_session, (), inline_call)
        if field:
            length = len(parent_session if field == "parent_session" else inline_call)
            return _oversized_identity_refusal(state, field, length)
    _, refusal = await internal_memory_scope(
        request, "spawn.create", claimed_session=parent_session
    )
    if refusal is not None:
        return refusal
    try:
        admitted_mode = await _spawn_request_memory_mode(state, request, parent_session)
    except (OSError, ValueError):
        return web.json_response(
            {
                "error": "The originating session's memory mode is unavailable.",
                "code": "memory_unavailable",
            },
            status=409,
        )
    # approval_mode and silent are HTTP API parameters passed by the SDK,
    # NOT MCP tool arguments from the LLM.  The LLM's spawn_run tool
    # (mcp_core.py) does not expose these params — they are added by the
    # SDK's spawn() method for app-level control.  Validated inline here
    # rather than in SPAWN_RUN_SCHEMA because they are transport-layer
    # params, not tool-schema params.
    #
    # Security: this endpoint requires X-Internal-Secret (internal_paths
    # in server.py), so only local MCP server processes can call it.
    approval_mode = body.get("approval_mode", "")
    if approval_mode not in ("", "auto"):
        return web.json_response({"error": "approval_mode must be '' or 'auto'"}, status=400)
    silent = body.get("silent", False)
    if not isinstance(silent, bool):
        silent = str(silent).lower() in ("true", "1", "yes")
    # keep=True marks the run's session as a continuable conversation
    # (spawn_continue can dispatch follow-up turns into it). Transport-layer
    # param like silent/approval_mode.
    keep = body.get("keep", False)
    if not isinstance(keep, bool):
        keep = str(keep).lower() in ("true", "1", "yes")
    agent = cleaned.get("agent") or ""
    from kiro_crew.dashboard.handlers._shared import member_request_scope
    from kiro_crew.execution_context import (
        ExecutionContext,
        MemoryStoreRef,
        derive_execution,
        read_session_execution,
    )

    crew = cleaned.get("target_member") or cleaned.get("crew") or ""
    if (
        cleaned.get("target_member")
        and cleaned.get("crew")
        and cleaned["target_member"] != cleaned["crew"]
    ):
        return web.json_response(
            {"error": "Conflicting target members", "code": "invalid_target_member"}, status=400
        )
    try:
        caller = await member_request_scope(request)
        parent_execution = caller.execution
        if parent_execution is None and parent_session:
            parent_execution = await asyncio.to_thread(read_session_execution, parent_session)
        # The parent agent spec's ``availableAgents`` declaration, read off-loop
        # from the template the record named, so the gate needs neither a second
        # record read nor a directory scan on the loop. A parentless request has
        # no declaration to honour: the synthesized context below carries the
        # CHILD's template, which must not be mistaken for a parent.
        parent_spawn_policy = (
            (
                parent_execution.template_id,
                await asyncio.to_thread(parent_spawn_allowlists, parent_execution.template_id),
            )
            if parent_execution is not None
            else ("", ())
        )
        if parent_execution is None:
            parent_execution = ExecutionContext(
                None, MemoryStoreRef("default"), "template", agent or "kirocrew"
            )
        config = await asyncio.to_thread(KiroCrewConfig.load) if crew else None
        if crew and config is not None and crew not in config.agents:
            return web.json_response(
                {"error": "The target member does not exist.", "code": "unknown_member"},
                status=404,
            )
        admitted_execution = derive_execution(
            parent_execution,
            target_member=crew or None,
            config=config,
            requested_mode=admitted_mode,
        )
        child_memory_store = admitted_execution.store.legacy_name
    except (OSError, ValueError) as exc:
        return web.json_response(
            {"error": str(exc), "code": "member_identity_unavailable"}, status=409
        )
    max_turns = cleaned.get("max_turns") or 0
    cwd = cleaned.get("cwd") or ""
    model = cleaned.get("model") or ""
    reasoning_effort = cleaned.get("reasoning_effort") or ""
    can_work = parent_work_supported(state, parent_session)
    # Batch/wave identity (transport-layer params from spawn_run MCP, like
    # approval_mode/silent above): validated inline, bounded, never LLM-schema.
    batch_id = str(body.get("batch_id", "") or "")[:32]
    if batch_id and not batch_id.isalnum():
        return web.json_response({"error": "batch_id must be alphanumeric"}, status=400)
    try:
        batch_total = max(0, min(int(body.get("batch_total", 0) or 0), 1000))
    except (TypeError, ValueError):
        batch_total = 0
    # The async moment preceding the synchronous spawn(): warm here so the
    # on-loop, cache-only agent validation inside spawn() is a hit.
    if agent:
        await warm_project_agents_for_spawn(state, cwd)
    # A member of a blocking spawn_sub_agents call: its id is minted and held
    # for that call BEFORE the run exists, so its completion can never reach the
    # parent whose turn is blocked in the call (subagent_inline_collection).
    inline_kwargs: dict[str, Any] = {}
    reserve = getattr(state.subagents, "reserve_inline_member", None)
    if body.get("inline_collect") is True and parent_session and callable(reserve):
        try:
            inline_wait = float(body.get("max_wait", 0) or 0)
        except (TypeError, ValueError):
            inline_wait = 0.0
        inline_id = reserve(parent_session, inline_wait, call=inline_call)
        if not inline_id:
            return web.json_response(
                {
                    "error": "too many sub-agents are waiting on this session's blocking calls",
                    "code": "inline_collection_full",
                },
                status=429,
            )
        inline_kwargs["_preassigned_id"] = inline_id
    try:
        info = await _spawn_on_loop(
            state,
            task,
            parent_session_key=parent_session,
            agent=agent,
            max_turns=max_turns,
            cwd=cwd,
            model=model or None,
            reasoning_effort=reasoning_effort,
            approval_mode=approval_mode or None,
            silent=silent,
            batch_id=batch_id,
            batch_total=batch_total,
            keep=keep,
            include_memory=cleaned.get("include_memory", True) is not False,
            include_lessons=cleaned.get("include_lessons", True) is not False,
            include_project=cleaned.get("include_project", True) is not False,
            memory_store=child_memory_store,
            crew=crew,
            _memory_mode=admitted_mode,
            _execution_context=admitted_execution.to_record(),
            _parent_spawn_policy=parent_spawn_policy,
            **inline_kwargs,
        )
    except BaseException:
        # The call never learns this id, so it can never collect it: release
        # it now rather than hold the parent's capacity until expiry.
        if inline_kwargs:
            state.subagents.inline_collections.discard(
                parent_session, inline_kwargs["_preassigned_id"]
            )
        raise
    if inline_kwargs and (not info or (info.done and info.error)):
        # Refused: nothing will run under the reserved id, so release it.
        state.subagents.inline_collections.discard(parent_session, inline_kwargs["_preassigned_id"])
    if not info:
        # Reached mgr.spawn (submission COUNTED at the top of spawn()) but
        # refused for capacity — tell the client so it does NOT reconcile
        # this member as a lost submission (double-count would close the
        # wave early).
        return web.json_response(
            {"error": f"capacity reached ({state.subagents.max_concurrent})", "counted": True},
            status=429,
        )
    if info.done and info.error:
        # Rejected INSIDE mgr.spawn: already counted as submitted and (for
        # batch members) announced through the completion consumer
        # (_announce_rejection). "counted" tells spawn_run's client-side
        # reconcile to skip this member.
        #
        # ``code`` is what the client switches on; ``error`` is advisory prose
        # (RFC 9457 3.1.3). Only the unknown-agent refusal mints its own
        # identifier today, because it is the only rejection a client treats
        # differently — spawn_run stops re-posting a name already refused. Every
        # other rejection reports the generic code, matching the sibling
        # /continue handler below.
        return web.json_response(
            {
                "error": info.error,
                "code": info.error_code or _SPAWN_REJECTED_CODE,
                "counted": True,
            },
            status=400,
        )
    resp: dict[str, object] = {
        "id": info.id,
        "task": task,
        "status": "spawned",
        "parent_work_supported": can_work,
    }
    # A row the gate DEFERRED or HELD (memory floor, critical posture, adaptive
    # cap at 0, macOS kernel memory pressure) is accepted and keyed like any
    # other -- same ``id``, counted in its wave -- but it is not running and may
    # not run for a long time: the pump re-checks it until the condition clears
    # (the pressure hold within its own bound). Saying ``spawned`` for it left
    # the caller waiting on a completion event that was not coming. ``queued``
    # names the wait; ``reason`` is the
    # kind, ``reason_detail`` the gate's own sentence. A row waiting only for a
    # slot or the stagger tick (``concurrency_limit``) keeps ``spawned``: that
    # wait is the ordinary wave shape and clears within seconds.
    queued_reason = str(getattr(info, "queued_reason", "") or "")
    if queued_reason in DEFERRED_QUEUED_REASONS:
        resp["status"] = "queued"
        resp["reason"] = queued_reason
        resp["reason_detail"] = _redact(str(getattr(info, "queued_reason_detail", "") or ""))
    # Server-side effort verdict: only this side knows the model the factory's
    # effort gate will see (explicit per-call value, else the subagent role
    # pin, else the session chain for the effective agent — a crew's pin, else
    # a non-sentinel global). Additive, optional key — reporting only, never
    # changes whether the spawn happened.
    if reasoning_effort:
        # Read the allocation-owned namespace on the loop. A reporting failure
        # cannot undo the submission or turn an unknown selection into "auto".
        selection: tuple[str, str] | None
        try:
            if agent:
                selection = ("template", agent)
            elif crew:
                selection = ("member", crew)
            elif parent_session:
                selection = state.sessions.get_agent_selection(parent_session)
            else:
                selection = ("template", "")
        except Exception:
            selection = None

        def _effort_verdict() -> tuple[str, str]:
            if (
                not isinstance(selection, tuple)
                or len(selection) != 2
                or selection[0] not in ("template", "member")
                or not isinstance(selection[1], str)
                or (selection[0] == "member" and not selection[1])
            ):
                return "", ""
            kind, verdict_agent = selection
            claim = verdict_agent if kind == "member" else ""
            d = effort_drop_reason(model, reasoning_effort, verdict_agent, crew_agent=claim)
            if d:
                return d, ""
            return "", effort_applied_note(model, reasoning_effort, verdict_agent, crew_agent=claim)

        # The resolvers read config and glob ~/.kiro/agents — file I/O that
        # must not run on the gateway event loop (the same reason
        # get_or_create runs _session_model in an executor).
        drop, applied = await asyncio.to_thread(_effort_verdict)
        if drop:
            resp["effort_dropped"] = drop
        elif applied:
            resp["effort_applied"] = applied
    if keep:
        # The conversation id is the FIRST run's id: spawn_continue targets it.
        resp["conversation"] = info.id
    return web.json_response(resp)


async def _spawn_on_loop(state: "DashboardState", task: str, **kwargs: Any) -> Any:
    """Spawn from an async handler WITHOUT blocking the loop on the task store.

    ``SubagentManager.spawn_async`` writes the durable row on the store's
    writer thread and only then starts the run (write-before-ack, off-loop).
    A manager without that entry -- a test double -- is spawned synchronously,
    which is the pre-queue behaviour those doubles model.
    """

    subagents = state.subagents
    assert subagents is not None  # every caller checked ``state.subagents`` first
    spawn_async = getattr(subagents, "spawn_async", None)
    if inspect.iscoroutinefunction(spawn_async):
        return await spawn_async(task, **kwargs)
    return subagents.spawn(task, **kwargs)


async def _continue_on_loop(state: "DashboardState", conv_id: str, task: str, **kwargs: Any) -> Any:
    """:func:`_spawn_on_loop` for continuations: ``continue_conversation_async``
    writes the durable row off-loop; a double without it continues synchronously."""

    subagents = state.subagents
    assert subagents is not None
    continue_async = getattr(subagents, "continue_conversation_async", None)
    if inspect.iscoroutinefunction(continue_async):
        return await continue_async(conv_id, task, **kwargs)
    return subagents.continue_conversation(conv_id, task, **kwargs)


async def api_spawn_continue(request: web.Request) -> web.Response:
    """POST /api/spawn/{agent_id}/continue — follow-up turn on a conversation.

    ``agent_id`` is the conversation id (the first keep=True run's id). Mints
    a NEW run on the same underlying session (resumed via session/load), so
    the follow-up executes with the conversation's accumulated context.
    """
    state: DashboardState = request.app["state"]
    if not state.subagents:
        return web.json_response(
            {"error": "subagents not available", "code": "subagents_unavailable"},
            status=503,
        )
    conv_id = request.match_info["agent_id"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    task = str(body.get("task", "") or "").strip()
    if not task:
        return web.json_response({"error": "task is required", "code": "task_required"}, status=400)
    parent_session = str(body.get("parent_session", "") or "")
    refusal = await _spawn_scope_refusal(request, claimed_session=parent_session)
    if refusal is not None:
        return refusal
    try:
        admitted_mode = await _spawn_request_memory_mode(state, request, parent_session)
    except (OSError, ValueError):
        return web.json_response(
            {
                "error": "The originating session's memory mode is unavailable.",
                "code": "memory_unavailable",
            },
            status=409,
        )
    agent = str(body.get("agent", "") or "")
    model = str(body.get("model", "") or "")
    try:
        max_turns = max(0, min(int(body.get("max_turns", 0) or 0), 1000))
    except (TypeError, ValueError):
        max_turns = 0
    # The run's own cwd, resolved OFF the event loop: a continuation has to run
    # where the run ran (a project-local agent does not resolve against the pool
    # project), but reading state.json and probing the path are blocking calls and
    # `continue_conversation` is synchronous. Doing it here keeps the gateway
    # responsive even when the recorded path lives on a stalled mount.
    resumed_cwd = await asyncio.to_thread(state.subagents.recorded_cwd, conv_id)
    info = await _continue_on_loop(
        state,
        conv_id,
        task,
        parent_session_key=parent_session,
        agent=agent,
        model=model or None,
        max_turns=max_turns,
        cwd=resumed_cwd,
        _memory_mode=admitted_mode,
    )
    if not info:
        return web.json_response(
            {
                "error": f"capacity reached ({state.subagents.max_concurrent})",
                "code": "capacity_reached",
            },
            status=429,
        )
    if info.done and info.error:
        if info.error.startswith("conversation_busy"):
            return web.json_response({"error": info.error, "code": "conversation_busy"}, status=409)
        if info.error.startswith(NATIVE_CHILD_NOT_RESUMABLE):
            # A harness-native child of a live session: the lever that exists
            # is the parent, so the refusal is a conflict, not a lookup miss.
            return web.json_response(
                {"error": info.error, "code": NATIVE_CHILD_NOT_RESUMABLE}, status=409
            )
        if info.error.startswith("conversation_gone"):
            return web.json_response({"error": info.error, "code": "conversation_gone"}, status=404)
        return web.json_response({"error": info.error, "code": _SPAWN_REJECTED_CODE}, status=400)
    return web.json_response({"id": info.id, "conversation": conv_id, "status": "spawned"})
