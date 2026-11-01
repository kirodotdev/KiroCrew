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
        PINNED_RUNTIME_NOTE,
        SPAWN_RUN_SCHEMA,
        DashboardState,
        KiroCrewConfig,
        ValidationError,
        _redact,
        _remote_run_operation_refusal,
        _spawn_scope_refusal,
        body_int,
        body_not_object_error,
        dashboard_slot_key,
        effective_session_key,
        effort_applied_note,
        effort_drop_reason,
        effort_pending_note,
        internal_memory_scope,
        parent_project_dirs,
        parent_template_allowlists,
        parent_work_supported,
        spawn_parent_claim,
        stopped_before_start,
        validate_tool_args,
        warm_project_agents_for_spawn,
    )


#: ``code`` for a start that was stopped while it waited, so it never ran (see
#: :func:`kiro_crew.subagent_wait_reasons.stopped_before_start`). A route that
#: tested only ``done and error`` would read that record as a started run.
STOPPED_BEFORE_START_CODE = "stopped_before_start"


def _stopped_before_start(info: Any, *, counted: bool = False) -> web.Response | None:
    """409 ``stopped_before_start`` when *info* is a start stopped while it waited.

    None for every other answer, so the caller goes on to its success body. A
    start that ended in an error is the caller's typed rejection, not this.
    ``counted`` adds ``counted: true`` for ``POST /api/spawn``, whose wave
    reconcile reads it.
    """
    if not stopped_before_start(info):
        return None
    error = "the run was stopped before it started; nothing ran"
    if counted:
        return web.json_response(
            {"error": error, "code": STOPPED_BEFORE_START_CODE, "counted": True}, status=409
        )
    return web.json_response({"error": error, "code": STOPPED_BEFORE_START_CODE}, status=409)


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


# Caller-chosen run ids between their freshness check and their registration
# with the manager (``_client_run_id_refusal``). The handler runs on the event
# loop, so a plain set checked and filled without an await in between is atomic.
# ``handlers/messaging.py`` imports this same object: ``compose`` rebinds this
# module's functions onto that module's globals, where the name must resolve.
_RESERVED_RUN_IDS: set[str] = set()


def _floored_caller_refusal(state: DashboardState, request: web.Request, parent: str) -> Any:
    """Refuse a start or continuation asked for by a run that carries the floor.

    The new run would take this gateway's approval posture, so a yolo crew
    would auto-approve every one of its tools and none would reach the hub.
    Both the claimed parent and the caller's own session are checked, the same
    pair the memory mode is read from.
    """
    from kiro_crew.subagent_manager.hub_approvals import floored_session

    caller = request.headers.get("X-Session-Key", "")
    subagents = getattr(state, "subagents", None)
    if not any(floored_session(subagents, key) for key in {parent, caller} if key):
        return None
    return web.json_response(
        {
            "error": "a run under an approval floor cannot start or continue another run",
            "code": "approval_floor_nested_spawn",
        },
        status=409,
    )


async def _client_run_id_refusal(state: DashboardState, run_id: object) -> web.Response | None:
    """The refusal for a caller-chosen ``run_id``, or ``None`` when it may be used.

    ``None`` also RESERVES the id in ``_RESERVED_RUN_IDS``; the caller must
    release it (``_RESERVED_RUN_IDS.discard``) once the run is registered with
    the manager or refused.

    The id has the exact shape this gateway mints itself (16 hex characters,
    ``SubagentManager._mint_agent_id``), so every reader of run ids accepts it.
    A remote hub names the run before it sends it, and records that name first,
    so a hub that dies between this answer and its own record still finds the run
    again under the name it wrote (``GET /api/spawn/{id}``). The id must be fresh
    here: one the manager knows, or one with a run folder on disk, is refused
    with ``409 run_id_taken`` rather than reused, so no caller can land on, or
    read through, another run's identity.
    """
    import re

    from kiro_crew.subagent_persistence import _agent_dir

    if not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{16}", run_id):
        return web.json_response(
            {"error": "run_id must be 16 lowercase hex characters", "code": "invalid_run_id"},
            status=400,
        )
    subagents = state.subagents
    known = (
        run_id in _RESERVED_RUN_IDS
        or subagents is not None
        and (
            subagents.get(run_id) is not None
            or bool(getattr(subagents, "is_queued", lambda _id: False)(run_id))
        )
    )
    taken = web.json_response(
        {"error": "run_id is already in use on this gateway", "code": "run_id_taken"},
        status=409,
    )
    if known:
        return taken
    # Reserved before the first await: two concurrent requests naming one fresh
    # id would otherwise both pass the disk check below and register under the
    # same id. The caller releases it once the run is registered or refused.
    _RESERVED_RUN_IDS.add(run_id)
    try:
        on_disk = await asyncio.to_thread(lambda: _agent_dir(run_id).exists())
    except BaseException:
        _RESERVED_RUN_IDS.discard(run_id)
        raise
    if on_disk:
        _RESERVED_RUN_IDS.discard(run_id)
        return taken
    return None


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


async def _requested_backend(raw: object) -> tuple[str | None, web.Response | None]:
    """The backend a spawn request names, as a backend id, or its refusal.

    ``(None, None)`` when the request names none: the run starts on the gateway
    default exactly as before. The request spells backends the way a governance
    rule does (``"kiro"``, not the empty id), so an agent can name every backend
    and a falsy value is never mistaken for one.

    A backend is available when governance allows it AND this machine has it
    installed (``probe_backend``; a probe that could not decide does not block).
    Refused rather than degraded: the factory's gate would fall back to Kiro, and
    a sub-agent the orchestrator sent to another harness silently running on Kiro
    -- or failing at cold start on a harness that is not there -- is the failure
    this parameter exists to rule out. The refusal lists only what can start, so
    an agent's retry picks a backend that works.
    """
    from kiro_crew.acp_backends import POLICY_ID_BY_BACKEND, selectable_backend_values
    from kiro_crew.agent_sdk.backend_install import MISSING, probe_backend

    if raw is None or raw == "":
        return None, None
    by_name = {name: backend for backend, name in POLICY_ID_BY_BACKEND.items()}
    selectable = selectable_backend_values()
    # Off the loop: a probe shells out and walks the filesystem (cached briefly).
    states = await asyncio.to_thread(lambda: {b: probe_backend(b) for b in selectable})
    # Installed after the gateway started (``restart_required``): the gateway
    # still holds the cached absence, so a start now would fail like a missing one.
    available = [
        b for b in selectable if states[b].installed != MISSING and not states[b].restart_required
    ]
    names = sorted(POLICY_ID_BY_BACKEND.get(b, b) for b in available)
    backend = by_name.get(raw) if isinstance(raw, str) else None
    if backend is not None and backend in available:
        return backend, None
    if backend not in states:
        return None, web.json_response(
            {
                "error": f"backend {raw!r} is not available; choose one of {names}",
                "code": "unknown_backend",
                "backends": names,
            },
            status=400,
        )
    state = states[backend]
    if state.restart_required:
        return None, web.json_response(
            {
                "error": (
                    f"backend {raw!r} was installed after the gateway started;"
                    f" restart the gateway to use it. Choose one of {names}"
                ),
                "code": "backend_restart_required",
                "backends": names,
            },
            status=400,
        )
    # Allowed here but not installed on this machine: say how to install it.
    missing = ", ".join(state.missing_components) or "unknown"
    error = (
        f"backend {raw!r} is not installed on this machine (missing: {missing});"
        f" choose one of {names}"
    )
    return None, web.json_response(
        {
            "error": error,
            "code": "backend_not_installed",
            "backends": names,
            # "" when the probe names no installer for this backend.
            "install_command": state.install_command,
        },
        status=400,
    )


def _qualified_model_refusal(
    request: web.Request, model: str, backend: str | None, raw_backend: object
) -> web.Response | None:
    """Refuse a ``provider/model`` id that the backend this request names cannot take.

    Bare and bracketed names, and a request that names no backend, keep the shape
    check alone. Only a backend that resolves its model from an advertised list takes
    such ids; once it has advertised one, the id must be on it, read as the models
    picker reads it. An empty list is trusted.
    """
    from kiro_crew.agent_sdk.capabilities import capabilities_for
    from kiro_crew.dashboard.handlers.agents import _advertised_backend_models

    if backend is None or "/" not in model:
        return None
    if not capabilities_for(backend).resolves_model_from_advertised_list:
        return web.json_response(
            {
                "error": f"backend {raw_backend!r} does not take provider-qualified model ids",
                "code": "model_unavailable",
            },
            status=400,
        )
    advertised = {row["model_name"] for row in _advertised_backend_models(request, backend)}
    if advertised - {"auto"} and model not in advertised:
        return web.json_response(
            {
                "error": f"model {model!r} is not among the models backend {raw_backend!r} advertises",
                "code": "model_unavailable",
            },
            status=400,
        )
    return None


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
    not_object = body_not_object_error(body)
    if not_object is not None:
        return not_object
    # The parent's project scope, read as soon as the request names its parent
    # and before any later await (the backend probe, the memory-scope and
    # record reads), so a parent session that ends meanwhile cannot shrink the
    # scope the gate checks now and the queued row records. Dict reads only;
    # a non-string parent is refused below and reads no scope here.
    asked_parent = body.get("parent_session", "")
    parent_dirs = list(parent_project_dirs(asked_parent)) if isinstance(asked_parent, str) else []
    # A caller-named run (a remote hub forwarding a spawn) is reserved before
    # the first admission await, and held until this request ends. A hub whose
    # connection dropped withdraws the run with DELETE; while admission is still
    # awaiting (the member scope, the parent's records) that DELETE must answer
    # ``409 spawn_admission_pending``, not ``404``. A 404 tells the hub the run
    # never started, so it forgets it, and this handler would then start a run
    # nobody monitors or can stop.
    run_id = body.get("run_id", "") if isinstance(body, dict) else ""
    if run_id:
        refusal = await _client_run_id_refusal(state, run_id)
        if refusal is not None:
            return refusal
    try:
        return await _admit_spawn(request, state, body, run_id)
    finally:
        if run_id:
            _RESERVED_RUN_IDS.discard(run_id)


async def _admit_spawn(
    request: web.Request, state: DashboardState, body: Any, run_id: str
) -> web.Response:
    """The admission and placement half of ``api_spawn``; *run_id* is already reserved."""
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
                "executor": body.get("executor", "local"),
                "instance_id": body.get("instance_id", ""),
                # The dict is CLOSED -- validate_tool_args only sees what is
                # listed here -- so omitting a schema field silently disables it
                # rather than failing. That is what made the crew delegation
                # below unreachable: the block, its unknown_crew refusal and its
                # store resolution all ran off a value that was always None.
                "crew": body.get("crew", ""),
                "target_member": body.get("target_member", ""),
                "backend": body.get("backend", ""),
                "label": body.get("label", ""),
            },
            SPAWN_RUN_SCHEMA,
        )
    except ValidationError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    task = (cleaned.get("task") or "").strip()
    if not task:
        return web.json_response({"error": "task is required"}, status=400)
    backend, backend_refusal = await _requested_backend(cleaned.get("backend"))
    if backend_refusal is not None:
        return backend_refusal
    model_refusal = _qualified_model_refusal(
        request, cleaned.get("model") or "", backend, cleaned.get("backend")
    )
    if model_refusal is not None:
        return model_refusal
    parent_session = body.get("parent_session", "")
    if not isinstance(parent_session, str):
        return web.json_response(
            {"error": "parent_session must be a string", "code": "invalid_parent_session"},
            status=400,
        )
    executor = str(cleaned.get("executor") or "local")
    instance_id = str(cleaned.get("instance_id") or "")
    if executor == "local" and instance_id:
        return web.json_response(
            {
                "error": "instance_id requires executor='remote'",
                "code": "remote_instance_without_executor",
            },
            status=400,
        )
    if executor == "remote" and cleaned.get("backend"):
        # The peer runs the child on its own harness and is not sent a backend,
        # so accepting one here would report a placement that ignored it.
        return web.json_response(
            {
                "error": "backend cannot be chosen for a remote run; the crew uses its own",
                "code": "remote_backend_unsupported",
            },
            status=400,
        )
    _, refusal = await internal_memory_scope(
        request, "spawn.create", claimed_session=spawn_parent_claim(request, parent_session)
    )
    if refusal is not None:
        return refusal
    nested_refusal = _floored_caller_refusal(state, request, parent_session)
    if nested_refusal is not None:
        return nested_refusal
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
    requested_memory_mode = body.get("memory_mode", "")
    if requested_memory_mode:
        if requested_memory_mode not in ("persistent", "incognito", "temporary"):
            return web.json_response(
                {"error": "invalid memory_mode", "code": "invalid_memory_mode"}, status=400
            )
        from kiro_crew.messaging.privacy_mode import strictest

        admitted_mode = strictest((admitted_mode, requested_memory_mode)) or "persistent"
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
    # A remote hub forwards its parent's approval posture as a floor: with
    # "interactive" this run's tools are never auto-approved here, whatever
    # this gateway's own yolo or config fallbacks say. Same transport-layer
    # rule as approval_mode: not an LLM tool argument.
    approval_floor = body.get("approval_floor", "")
    if approval_floor not in ("", "interactive"):
        return web.json_response(
            {
                "error": "approval_floor must be '' or 'interactive'",
                "code": "invalid_approval_floor",
            },
            status=400,
        )
    if approval_floor and approval_mode:
        return web.json_response(
            {
                "error": "approval_floor and approval_mode=auto contradict",
                "code": "approval_floor_conflict",
            },
            status=400,
        )
    if approval_floor:
        from kiro_crew.subagent_manager.hub_approvals import floor_enforceable

        if not await asyncio.to_thread(floor_enforceable, backend):
            # This harness pre-approves through a surface the floor cannot
            # reach, so accepting the run would drop the floor silently. The
            # run's own backend is checked, not only the gateway default.
            return web.json_response(
                {
                    "error": "this gateway's agent backend cannot enforce an approval floor",
                    "code": "approval_floor_unenforceable",
                },
                status=409,
            )
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
                await asyncio.to_thread(
                    parent_template_allowlists,
                    parent_session,
                    parent_execution.template_id,
                    parent_dirs,
                ),
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
        batch_total = max(0, min(body_int(body.get("batch_total", 0) or 0), 1000))
    except (TypeError, ValueError):
        batch_total = 0
    # Fair admission (Shared Crew): a child of whatever runs in the parent
    # session, charged to that work's initiator. Refused before the manager
    # counts the submission, so the refusal carries no ``counted``.
    from kiro_crew.shared_crew.admission_entry import admit_subagent

    child_admission = await admit_subagent(request, state, parent_session)
    if child_admission.rejected:
        return child_admission.refusal_response()
    from kiro_crew.shared_crew.admission_gate import Admission
    from kiro_crew.shared_crew.admission_link import link_spawn

    # A caller-reserved ``run_id`` (a hub placing this run) is the id linked;
    # otherwise the admission mints one. ``""`` from the link keeps the caller's.
    linked_id = await link_spawn(child_admission, state.subagents, preferred=run_id)
    if linked_id is None:
        await child_admission.abandon()
        return Admission(None, "rejected", store_unavailable=True).refusal_response()
    run_id = linked_id or run_id
    if executor == "remote":
        from kiro_crew.sel import sel

        if request.get("app", ""):
            sel().log_tool_invocation(
                session_key=parent_session or "",
                source="app_isolation",
                tool_name="spawn_run",
                outcome="denied",
                error="app tokens cannot spend a remote crew",
                metadata={"executor": "remote"},
            )
            return web.json_response(
                {"error": "app tokens cannot spend a remote crew", "code": "app_token_forbidden"},
                status=403,
            )
        # The admitted execution, not only an explicit ``crew``: a parent that is
        # itself member-bound (or on a named store) passes that binding on, and
        # the peer, which receives no store identity, would build a default-store
        # execution and read its own Global memory instead.
        if (
            crew
            or admitted_execution.member_id is not None
            or admitted_execution.store.store_id != "default"
        ):
            return web.json_response(
                {
                    "error": "remote runs cannot inherit a local Crew Member memory binding",
                    "code": "remote_crew_binding_unsupported",
                },
                status=400,
            )
        if keep:
            return web.json_response(
                {
                    "error": "remote continuable conversations are not supported yet",
                    "code": "remote_keep_unsupported",
                },
                status=400,
            )
        # Remote placement must not loosen this gateway's ceiling. Two gates:
        # 1. the operator opts in (instances.remote_subagents): the peer applies
        #    its OWN tool-approval policy, which this gateway cannot verify, so
        #    the model alone may not choose to leave the local approval ceiling;
        # 2. the same spawn governance (capabilities.spawn and its agent scope)
        #    and parent-spec availableAgents allowlist the local path enforces in
        #    admission, checked against the effective child template.
        from kiro_crew.subagent import (
            _validate_app_agent_ownership,
            _vet_parent_available_agents,
            _vet_remote_placement_governance,
            _vet_spawn_governance,
        )

        remote_cfg = await asyncio.to_thread(KiroCrewConfig.load)
        if not getattr(remote_cfg.instances, "remote_subagents", False):
            sel().log_tool_invocation(
                session_key=parent_session or "",
                source="subagent",
                tool_name="spawn_run",
                outcome="denied",
                error="remote subagent placement is turned off on this gateway",
                metadata={"executor": "remote"},
            )
            return web.json_response(
                {
                    "error": "remote subagent placement is turned off on this gateway "
                    "(instances.remote_subagents)",
                    "code": "remote_subagents_disabled",
                },
                status=403,
            )
        # The child's template: the explicit agent, else the one it inherits.
        # Checked by both gates and sent to the peer, so a parent cannot pass
        # the allowlist on its own name and then run a different agent remotely.
        effective_agent = agent or admitted_execution.template_id
        # An app-owned member session reaches here over the loopback MCP secret,
        # not an app token, so its app identity comes from the execution record.
        # Without it the PROFILE half of POLICY ∩ PROFILE is skipped, as the
        # local admission gate (``app = execution.app``) does not do.
        child_app = admitted_execution.app
        # The agent the PARENT runs as, not the requested child: a profile bound
        # to it narrows the parent's own tool gate, so it narrows placement and
        # the project snapshot too. It comes from the parent's execution record,
        # which a channel or cron parent has as well as a dashboard tab. A named
        # parent whose agent cannot be read is refused: with no agent, the
        # profile bound to it would be skipped.
        parent_agent = parent_spawn_policy[0]
        if parent_session and not parent_agent:
            sel().log_tool_invocation(
                session_key=parent_session,
                source="subagent",
                tool_name="spawn_run",
                outcome="denied",
                error="the parent session's agent could not be resolved",
                metadata={"executor": "remote"},
            )
            return web.json_response(
                {
                    "error": "spawn refused: the parent session's agent could not be resolved",
                    "code": "remote_parent_agent_unresolved",
                },
                status=403,
            )
        denial = await asyncio.to_thread(
            _vet_remote_placement_governance, parent_session, child_app, agent=parent_agent
        )
        if not denial:
            denial = await asyncio.to_thread(
                _vet_spawn_governance,
                parent_session,
                effective_agent,
                child_app,
                parent_agent=parent_agent,
            )
        if not denial:
            denial = _vet_parent_available_agents(
                parent_spawn_policy, effective_agent, app=child_app
            )
        if not denial and agent and child_app:
            # As the local admission gate: an app may only name its OWN agents,
            # or remote placement would run the host default or another app's.
            denial = await asyncio.to_thread(_validate_app_agent_ownership, agent, child_app)
        if denial:
            sel().log_tool_invocation(
                session_key=parent_session or "",
                source="subagent",
                tool_name="spawn_run",
                outcome="denied",
                error="remote spawn denied by governance",
                metadata={"executor": "remote"},
            )
            return web.json_response(
                {"error": f"spawn refused: {denial}", "code": "remote_spawn_denied"},
                status=403,
            )
        from kiro_crew.dashboard.remote_subagents import (
            RemoteSubagentError,
            auto_approve_sources,
            get_remote_subagent_service,
        )

        try:
            remote_info = await get_remote_subagent_service(state).spawn(
                task=task,
                parent_session=parent_session,
                agent=effective_agent,
                max_turns=max_turns,
                cwd=cwd,
                model=model,
                reasoning_effort=reasoning_effort,
                include_memory=cleaned.get("include_memory", True) is not False,
                include_lessons=cleaned.get("include_lessons", True) is not False,
                include_project=cleaned.get("include_project", True) is not False,
                memory_mode=admitted_mode,
                batch_id=batch_id,
                batch_total=batch_total,
                instance_id=instance_id,
                approval_mode=approval_mode,
                auto_approve_sources=auto_approve_sources(remote_cfg),
                app=child_app,
                parent_agent=parent_agent,
                silent=bool(silent),
            )
        except RemoteSubagentError as exc:
            return web.json_response({"error": str(exc), "code": exc.code}, status=exc.status)
        return web.json_response(
            {
                "id": remote_info.id,
                # The placement's own copy: it already carries both redaction passes.
                "task": remote_info.task,
                "status": "spawned",
                "parent_work_supported": can_work,
                "executor": "remote",
                "instance_id": remote_info.instance_id,
                "remote_id": remote_info.remote_id,
            }
        )
    # The async moment preceding the synchronous spawn(): warm here so the
    # on-loop, cache-only agent validation inside spawn() is a hit. A
    # caller-named ``run_id`` was reserved by ``api_spawn`` before admission.
    try:
        if cwd:
            from kiro_crew.dashboard.remote_workspaces import claim_workspace

            if not await asyncio.to_thread(claim_workspace, cwd):
                await child_admission.abandon()
                return web.json_response(
                    {
                        "error": "the run's workspace snapshot is gone; upload it again",
                        "code": "remote_workspace_gone",
                    },
                    status=409,
                )
        if agent:
            await warm_project_agents_for_spawn(state, cwd)
        from kiro_crew.shared_crew.admission_entry import admitted_scope

        with admitted_scope(child_admission):
            info = await _spawn_on_loop(
                state,
                task,
                parent_session_key=parent_session,
                agent=agent,
                max_turns=max_turns,
                cwd=cwd,
                model=model or None,
                reasoning_effort=reasoning_effort,
                backend=backend,
                label=str(cleaned.get("label") or ""),
                approval_mode=approval_mode or None,
                approval_floor=approval_floor,
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
                _parent_project_dirs=parent_dirs,
                **({"_preassigned_id": run_id} if run_id else {}),
            )
    finally:
        # Registered with the manager (or refused): the manager's own record
        # now answers the freshness check, so the reservation is released.
        if run_id:
            _RESERVED_RUN_IDS.discard(run_id)
    if not info or (info.done and info.error):
        await child_admission.abandon()
    else:
        await child_admission.attach_child(info.id, session_key=f"subagent:{info.id}")
    if not info:
        # Reached mgr.spawn (submission COUNTED at the top of spawn()) but
        # refused for capacity — tell the client so it does NOT reconcile
        # this member as a lost submission (double-count would close the
        # wave early).
        cap = getattr(state.subagents, "max_concurrent", "?")
        return web.json_response(
            {"error": f"capacity reached ({cap})", "counted": True},
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
    # Accepted (and counted) by mgr.spawn, then stopped before its claim: no run
    # and no card will follow, so "spawned" would leave the caller waiting.
    stopped = _stopped_before_start(info, counted=True)
    if stopped is not None:
        return stopped
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
            """The receipt field for this request's effort and its text, or ``("", "")``."""
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
            # Judged on the backend the run starts on, not the gateway default.
            # At most one of the three is non-empty for a requested effort.
            for field, verdict in (
                ("effort_dropped", effort_drop_reason),
                ("effort_applied", effort_applied_note),
                ("effort_pending", effort_pending_note),
            ):
                text = verdict(
                    model, reasoning_effort, verdict_agent, crew_agent=claim, backend=backend
                )
                if text:
                    return field, text
            return "", ""

        # The resolvers read config and glob ~/.kiro/agents — file I/O that
        # must not run on the gateway event loop (the same reason
        # get_or_create runs _session_model in an executor).
        field, text = await asyncio.to_thread(_effort_verdict)
        if field:
            resp[field] = text

    # Reporting only, like the effort verdict. Read from the run's own sharing
    # plan, the one decision the start and admission take, so the note can
    # never follow a copy of the pin rule. The plan reads config (file I/O),
    # so off the loop; a crew spawn already loaded it above.
    def _runtime_note() -> str:
        manager = state.subagents
        if manager is None:
            return ""
        import copy

        from kiro_crew.subagent import _selection_kind

        try:
            # Only a template run can ever take the shared arm.
            if _selection_kind(info) != "template":
                return ""
            plan = manager._sharing_plan(info, cfg=config)
            pins = (getattr(plan, "eff_model", ""), getattr(plan, "eff_effort", ""))
            if not any(isinstance(p, str) and p for p in pins):
                return ""
            if getattr(plan, "shared", None) is not False or getattr(info, "keep", False):
                return ""
            # Name the pin only when it is what took the share away: the same
            # run without its own model would take the shared arm. A backend
            # or setting that never shares gets no note.
            unpinned = copy.copy(info)
            unpinned.model = ""
            unpinned.routed_model = ""
            if manager._should_use_session_sharing(unpinned) is not True:
                return ""
        except Exception:
            return ""
        return PINNED_RUNTIME_NOTE

    runtime_note = await asyncio.to_thread(_runtime_note)
    if runtime_note:
        resp["runtime_note"] = runtime_note
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
    not_object = body_not_object_error(body)
    if not_object is not None:
        return not_object
    task = str(body.get("task", "") or "").strip()
    if not task:
        return web.json_response({"error": "task is required", "code": "task_required"}, status=400)
    parent_session = str(body.get("parent_session", "") or "")
    refusal = await _spawn_scope_refusal(
        request, claimed_session=spawn_parent_claim(request, parent_session)
    )
    if refusal is not None:
        return refusal
    nested_refusal = _floored_caller_refusal(state, request, parent_session)
    if nested_refusal is not None:
        return nested_refusal
    remote_refusal = await _remote_run_operation_refusal(state, conv_id, "continuation")
    if remote_refusal is not None:
        return remote_refusal
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
        max_turns = max(0, min(body_int(body.get("max_turns", 0) or 0), 1000))
    except (TypeError, ValueError):
        max_turns = 0
    # The run's own cwd, resolved OFF the event loop: a continuation has to run
    # where the run ran (a project-local agent does not resolve against the pool
    # project), but reading state.json and probing the path are blocking calls and
    # `continue_conversation` is synchronous. Doing it here keeps the gateway
    # responsive even when the recorded path lives on a stalled mount.
    resumed_cwd = await asyncio.to_thread(state.subagents.recorded_cwd, conv_id)
    from kiro_crew.shared_crew.admission_entry import admit_subagent

    child_admission = await admit_subagent(request, state, parent_session)
    if child_admission.rejected:
        return child_admission.refusal_response()
    from kiro_crew.shared_crew.admission_entry import admitted_scope
    from kiro_crew.shared_crew.admission_gate import Admission
    from kiro_crew.shared_crew.admission_link import link_spawn

    run_id = await link_spawn(child_admission, state.subagents)
    if run_id is None:
        await child_admission.abandon()
        return Admission(None, "rejected", store_unavailable=True).refusal_response()
    with admitted_scope(child_admission):
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
            **({"_preassigned_id": run_id} if run_id else {}),
        )
    if not info or (info.done and info.error):
        await child_admission.abandon()
    else:
        # A continuation runs on its conversation's session key
        # (``subagent:<original-id>``), so that is the key its nested spawns name.
        await child_admission.attach_child(
            info.id,
            session_key=getattr(info, "conversation_key", "") or f"subagent:{info.id}",
            also=(f"subagent:{info.id}",),
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
    stopped = _stopped_before_start(info)
    if stopped is not None:
        return stopped
    return web.json_response({"id": info.id, "conversation": conv_id, "status": "spawned"})
