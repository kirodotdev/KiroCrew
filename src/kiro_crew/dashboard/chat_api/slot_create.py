"""Slot create: ``POST /api/chat/slots``, the app-agent owner record it persists, and the
default-template stamp an agent-less slot gets, which the agent switch shares.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from types import TracebackType

    from kiro_crew.dashboard.chat_handlers import (
        _CREATABLE_MODES,
        _DEFERRED_PLAIN_CREATE_KNOWN_KEYS,
        ARTIFACT_SLUG_RE,
        DashboardState,
        KiroCrewConfig,
        MemoryStartupUnavailable,
        SessionFolderError,
        _app_acquisition_conflict,
        _app_claim_refused,
        _app_slot_acquisition_denial,
        _app_slot_acquisition_recheck,
        _bump_slot_tags_revision,
        _ChatSlot,
        _coerce_requested_mode,
        _history_key_for,
        _normalize_slot_key,
        _rebase_rehydrated_refresh_mark,
        _record_explicit_agent_selection,
        _rehydrate_slot_title,
        _slot_switch_session_lock,
        _sync_dashboard_slots,
        _unhide_folder,
        _workspace_name_for_dir,
        create_session_folder,
        cron_creator_admission,
        cron_slot_creator,
        default_project_dir,
        default_template,
        deny_app_slot_session_access,
        dispatch_kiro_agent,
        drained_to_thread,
        effective_session_key,
        folder_principal,
        is_owner_dashboard_request,
        is_sensitive_path,
        logger,
        members_mod,
        owner_start_priority,
        pin_private_agent_store,
        read_bounded_json,
        redact_credentials,
        redact_exfiltration_urls,
        request_slot_origin,
        resolve_agent_bindings,
        resolve_default_session_folder,
        resolve_folder_project_dir_off_loop,
        restore_agent_selection,
        save_slot_off_loop,
        schedule_eager_spawn,
        slot_history_key,
        slot_not_found,
        tags_write_lock,
        validate_folder_tag_ids,
        voice_runtime_workspace_conflict,
        wait_for_memory_preparation,
    )
    from kiro_crew.remote_dispatch_mailbox import MailboxRequest
    from kiro_crew.session_agent_selection import SelectionChange


def _default_template_stamp(cfg: KiroCrewConfig) -> str:
    """The name an agent-less slot is stamped with: the default custom agent (a
    template), as kiro-cli DECLARES it.

    Runs OFF the event loop (``asyncio.to_thread``) on purpose: ``agent.default_agent``
    may spell a spec's file stem, and :func:`dispatch_kiro_agent` can only warm a
    cold materialized snapshot when called off-loop -- on the loop it returns its
    input unchanged, which would stamp the stem during the boot window and strand
    the slot once the default moves on. The stamp outlives a later change of the
    default, so it has to be the declared name from the first write.
    """
    return dispatch_kiro_agent(default_template(cfg))


def _created_slot_row(state: Any, request: web.Request, slot: Any) -> dict[str, Any]:
    """*slot*'s row for the create route's answer, rank-free for a scoped caller.

    A create can answer with an existing, already-pinned session (an
    idempotent retry or a raced key). Its ``pin_rank`` is a place in the
    person's whole pinned order, which an app or member caller gets none of.
    """
    row = state.serialize_slot(slot)
    if folder_principal(state, request):
        from kiro_crew.dashboard.pinned_session_order import PIN_ORDER_FIELDS

        for field in PIN_ORDER_FIELDS:
            row.pop(field, None)
    return row


def _retract_failed_newborn(state: DashboardState, slot: _ChatSlot) -> bool:
    """Withdraw a slot this create minted and is answering with a failure.

    The mint runs inside the create's ``suspend_slots_push`` block, so a newborn
    still in ``state._slots`` when a failure returns goes out in the block's owed
    frame: every client lists a session the caller was told did not get created,
    and each retry adds another empty row. Removed synchronously, before the
    block exits, so that frame never lists it, together with the restricted and
    ephemeral keys the mint registered. Retracted only while it is provably the
    newborn: a slot replaced under its key, or one a turn started on during the
    create's awaits, stays (liveness over tidiness). Returns whether it was
    retracted, so the caller undoes the selection it recorded for the key only
    when no slot holds that key any more.
    """
    if slot.running or slot.messages or state._slots.get(slot.key) is not slot:
        return False
    state._slots.pop(slot.key, None)
    state._restricted_keys.discard(f"dashboard:{slot.key}")
    state._ephemeral_keys.discard(f"dashboard:{slot.key}")
    _sync_dashboard_slots(state)
    return True


async def _birth_default_project(cfg: KiroCrewConfig | None, workspace: str) -> str:
    """The project a new slot gets when neither it nor its folder names one."""
    cfg_proj = cfg.dashboard.default_project if cfg else ""

    def resolve() -> str:
        if isinstance(cfg_proj, str) and cfg_proj:
            resolved = os.path.realpath(os.path.expanduser(cfg_proj))
            # A configured default that overlaps the data home would be refused
            # at spawn anyway, so it is skipped here like a sensitive path.
            if (
                os.path.isdir(resolved)
                and not is_sensitive_path(resolved)
                and voice_runtime_workspace_conflict(resolved) is None
            ):
                return resolved
        return default_project_dir(workspace)

    # Off the loop: every branch, the workspace fallback included, touches the
    # disk or reloads config.
    return await asyncio.to_thread(resolve)


async def _check_new_slot_assignment(
    cfg: KiroCrewConfig, agent: str, agent_kind: str, project: str, memory_mode: str
) -> None:
    """Raise where the owner's member assignment of a new slot would refuse it.

    The same questions the assignment asks after the mint, with no write: the
    member's private store must still be the member's own (the
    ``member_store_ownership_holds`` check the pin makes), the agent must
    resolve, and the member identity check ``_record_explicit_agent_selection``
    makes must pass. A refusal here means the create mints, builds and files
    nothing.
    """
    from kiro_crew.dashboard.chat_persistence import (
        _member_private_selection,
        member_store_ownership_holds,
    )
    from kiro_crew.execution_context import resolve_member_execution
    from kiro_crew.memory_stores import UnknownMemoryStore

    if agent_kind != "template":
        selected, store = _member_private_selection(agent, cfg)
        if store and not await asyncio.to_thread(
            member_store_ownership_holds, cfg, selected, store
        ):
            raise UnknownMemoryStore(
                f"Crew Member {selected!r} does not own its private memory store; "
                "no session was created"
            )

    chosen = await asyncio.to_thread(
        resolve_agent_bindings,
        cfg,
        agent,
        project or None,
        validate_memory_files=False,
        selection_kind=agent_kind,
    )
    if chosen.selection_kind == "member":
        await asyncio.to_thread(
            resolve_member_execution,
            cfg,
            agent or chosen.resolved_alias,
            memory_mode=memory_mode,
            app="",
            validate_memory_files=False,
        )


async def api_chat_slot_create(request: web.Request) -> web.Response:
    """POST /api/chat/slots — create a new chat slot."""
    state: DashboardState = request.app["state"]
    body, body_err = await read_bounded_json(request, allow_absent=True)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    # A session on a connected crew is created ON that crew and opened here
    # through its window; this hub never mints or adopts a relay binding.
    # Refused before anything is read or written, so a stale client cannot stamp
    # a remote executor onto a new local slot. The answer is the same for every
    # caller and every state, so it discloses nothing.
    from kiro_crew.shared_crew import admission_gate

    _service = await admission_gate.current_service()
    if body.get("instance_id") or body.get("adopt_remote_slot"):
        return web.json_response(
            {
                "error": "sessions on a connected crew are created on that crew, not here",
                "code": "remote_create_retired",
            },
            status=400,
        )
    # Remote-dispatch preview (RFC conductor-remote-dispatch §2b): an owner's
    # work-item create on a crew short of memory, CPU or disk is refused before
    # anything is read or minted, and every owner create reply carries the
    # snapshot. Preview off, or any other caller: (None, None).
    from kiro_crew.dashboard.remote_dispatch_headroom import create_admission

    busy, headroom = await create_admission(request, body)
    if busy is not None:
        return busy
    # Remote dispatch (Feature Preview): a hub's work item this create binds, or
    # None, which is also the answer with the preview off. Imported only for a body
    # that names one, so a plain create and the boot path never load it.
    work_item: MailboxRequest | None = None
    if body.get("work_item") is not None:
        from kiro_crew.dashboard.handlers.remote_work_item import work_item_for_create

        work_item, work_item_refusal = await work_item_for_create(request, body)
        if work_item_refusal is not None:
            return work_item_refusal
    name = body.get("name")
    if name is not None and not isinstance(name, str):
        # `get_or_create_slot` normalizes the key with string operations, so a
        # non-string name would surface there as an unhandled 500.
        name = str(name)
    agent = body.get("agent", "")
    # The selection NAMESPACE, when the caller states one. "member" names a
    # configured crew, "template" a shared provider template; an omitted kind
    # keeps the legacy name-only resolution. The kind is selection input, never
    # authority: every owner, app and private-memory gate below still applies.
    agent_kind = body.get("agent_kind", "")
    if agent_kind not in ("", "member", "template"):
        return web.json_response(
            {"error": "invalid agent kind", "code": "invalid_agent_kind"}, status=400
        )
    # A palette command naming its app's own agent stamps the owning app here, so
    # every turn can refuse to run when the bare name resolves to any OTHER spec
    # (see _ChatSlot.app_agent_owner). Only meaningful beside an explicit template
    # pick of that agent; anything else is refused rather than half-applied.
    app_agent_owner_raw = body.get("app_agent_owner", "")
    app_agent_owner = ""
    if app_agent_owner_raw:
        from kiro_crew.apps.manifest import app_name_error

        if (
            not isinstance(app_agent_owner_raw, str)
            or app_name_error(app_agent_owner_raw)
            or not isinstance(agent, str)
            or not agent
            or agent_kind != "template"
        ):
            return web.json_response(
                {"error": "invalid app agent owner", "code": "invalid_app_agent_owner"},
                status=400,
            )
        app_agent_owner = f"{app_agent_owner_raw}/{agent}"
    model = body.get("model", "")
    # Folder membership at BIRTH. Assigning it afterwards (client PATCH) is
    # visibly too late: get_or_create_slot broadcasts the new slot before this
    # handler returns, so the dashboard renders it at the top level for a frame
    # or two and it then jumps into the folder. Validated exactly as
    # PATCH /api/chat/slots/{slot}/folder validates it.
    folder_id = str(body.get("folder_id") or "")
    if folder_id and not any(f["id"] == folder_id for f in state._folders):
        return web.json_response(
            {"error": "folder not found", "code": "folder_not_found"}, status=400
        )
    existing_slot = state._slots.get(_normalize_slot_key(str(name))) if name else None
    request_app = request.get("app", "")
    # The person's default folder, for a dashboard New chat that named none.
    # Asked for explicitly (`default_folder: true`) so apps, crons, channels and
    # session_create keep the placement they have today, and applied only to a
    # fresh unnamed create, since a named one can reopen a session that already
    # has a place. A default that does not name one of the person's folders
    # leaves the chat unfiled; see `resolve_default_session_folder`.
    folder_from_default = False
    requested_agent = agent
    if (
        body.get("default_folder") is True
        and not folder_id
        and not name
        and not request_app
        and not body.get("ephemeral")
        and not body.get("artifact")
        and is_owner_dashboard_request(request)
    ):
        try:
            default_cfg = await asyncio.to_thread(KiroCrewConfig.load)
            configured = getattr(default_cfg.dashboard, "default_session_folder", "")
        except Exception:
            logger.warning("Failed to load config for the default folder", exc_info=True)
            configured = ""
        if isinstance(configured, str) and configured:
            folder_id, folder_agent = await state.read_folders(
                lambda folders: resolve_default_session_folder(folders, configured)
            )
            folder_from_default = bool(folder_id)
            # The folder's agent pin applies as it does for New chat in that
            # folder; an agent the caller picked still wins.
            if folder_from_default and folder_agent and not agent:
                agent = folder_agent
    # Ownership of a NAMED slot for an app caller, before any refusal below can
    # answer about it (get_or_create_slot's memory-mode 409 among them) and
    # before anything is written. The post-create check stays authoritative.
    acquisition_judged: Any = None
    if request_app and name:
        denied, acquisition_judged = await _app_slot_acquisition_denial(
            request, state, request_app, str(name), "chat_slot_create", session_grant_route=False
        )
        if denied is not None:
            return denied
        existing_slot = state._slots.get(_normalize_slot_key(str(name)))
    memory_mode = body.get("memory_mode", "persistent")
    if memory_mode not in ("persistent", "incognito", "temporary"):
        return web.json_response({"error": "invalid memory_mode"}, status=400)
    _mode = _coerce_requested_mode(body.get("mode", ""))
    if _mode not in _CREATABLE_MODES:
        return web.json_response({"error": "invalid mode", "code": "invalid_mode"}, status=400)
    # A member-* name is RESERVED for DM threads (born only through the member
    # thread endpoint); `get_or_create_slot` below rejects it with a ValueError
    # that becomes a 409; refused here so the error carries its code. Checked on the
    # normalized key, the form the slot store is built from.
    if name and _normalize_slot_key(str(name)).casefold().startswith(
        members_mod.DM_SLOT_KEY_PREFIX
    ):
        return web.json_response(
            {
                "error": "member thread slots are created only via the member thread endpoint",
                "code": "member_slot_reserved",
            },
            status=409,
        )
    folder_project = ""
    if folder_id and (existing_slot is None or not existing_slot.project):
        folder_snapshot = await state.read_folders(
            lambda folders: [dict(folder) for folder in folders]
        )
        # The chain walk runs on the loop; only a declared project's ``stat``
        # hops to a worker thread (see `resolve_folder_project_dir_off_loop`).
        folder_project, folder_project_error = await resolve_folder_project_dir_off_loop(
            folder_snapshot, folder_id
        )
        if folder_project_error and folder_from_default:
            # The person asked for no folder, so a default folder whose project
            # directory has gone missing must not refuse the chat. Unfiled is the
            # safe answer: filing it there with a different project would run the
            # chat somewhere that folder says it should not.
            logger.warning(
                "Default folder %s has an unusable project (%s); new chat left unfiled",
                folder_id,
                folder_project_error,
            )
            folder_id = ""
            folder_project = ""
            folder_from_default = False
            agent = requested_agent
        elif folder_project_error:
            return web.json_response(
                {
                    "error": f"invalid folder project: {folder_project_error}",
                    "code": "folder_project_invalid",
                },
                status=400,
            )
    # Binding resolution checks memory readiness. A fresh conversation gets
    # the same bounded recovery grace as its first turn, before any slot or
    # protected assignment is published.
    if existing_slot is None and is_owner_dashboard_request(request):
        try:
            await wait_for_memory_preparation(getattr(state, "memory_startup_task", None))
        except MemoryStartupUnavailable as exc:
            return web.json_response({"error": str(exc), "code": "store_unavailable"}, status=503)

    # Resolve workspace from agent bindings
    workspace = "default"
    cfg = None
    try:
        cfg = KiroCrewConfig.load()
    except Exception:
        # Infra failure loading config must not block slot creation outright, so
        # validation below is skipped rather than failing closed.
        logger.warning("Failed to load config for slot create", exc_info=True)
    # An agent-less create means "the default TEMPLATE": stamp it into the slot
    # in the template namespace instead of storing "", so the slot's metadata
    # records what will actually answer — otherwise the dashboard footer chip
    # renders its literal 'default' fallback while dispatch quietly resolves
    # the real default. The default template, never the `default` crewmate
    # alias: a plain session is a template on the default folder and
    # memory store, and stamping the alias made every plain chat that
    # crewmate's — its workspace, its model pin, its roster recency. Placed
    # BEFORE the normalization below so the stamped template also gets its
    # workspace resolved by the existing binding path.
    # Stamped as the name kiro-cli DECLARES (`_default_template_stamp`), not a
    # file stem `agent.default_agent` may spell.
    if cfg is not None and not agent:
        agent = await asyncio.to_thread(_default_template_stamp, cfg)
        agent_kind = "template"
    # Normalize an agent nothing will dispatch to the one that WILL answer.
    # Otherwise the name is stored verbatim and resolve_agent_bindings silently
    # falls back to the default agent: the sidebar advertises the requested agent
    # while a different one answers, with none of its tools. Storing the real
    # agent keeps the slot honest, and a caller that requires a specific binding
    # (an app panel verifying the returned agent) can see the mismatch instead of
    # discovering it turns later.
    if cfg is not None and agent:
        resolving_key = _normalize_slot_key(str(name)) if name else ""
        resolving_slot = state._slots.get(resolving_key) if resolving_key else None
        resolving_fields = (
            (
                resolving_slot.agent,
                resolving_slot.project,
                resolving_slot.workspace,
                resolving_slot.memory_store,
                resolving_slot._app,
                effective_session_key(resolving_slot),
            )
            if resolving_slot is not None
            else None
        )
        try:
            # Resolved in the STATED namespace, so a template pick takes the
            # template's workspace rather than a same-name member's. No project
            # scope here: a create carries no slot yet, and the catalog offers a
            # slot-less chat global rows only, so this is the whole choice set.
            bindings = await asyncio.to_thread(
                resolve_agent_bindings,
                cfg,
                agent,
                validate_memory_files=False,
                selection_kind=agent_kind,
            )
            workspace = _workspace_name_for_dir(cfg, bindings.workspace_dir)
            if agent_kind and not bindings.requested_resolved:
                # A stated namespace never falls back to whoever answers by
                # default -- and it is refused HERE, before get_or_create_slot,
                # so a refused create registers no slot. The legacy name-only
                # path below keeps its store-verbatim-and-log behaviour.
                return web.json_response(
                    {
                        "error": "the selected agent choice is not available",
                        "code": "agent_choice_unavailable",
                    },
                    status=409,
                )
            if not bindings.requested_resolved:
                # Log only — the requested binding is the user's intent and is
                # stored VERBATIM. Rewriting it to whatever currently answers was
                # destructive: the resolution behind that decision can be
                # momentarily stale while the overwrite is permanent, so a valid
                # binding could be silently rebound to the default forever, where a
                # verbatim name recovers as soon as it resolves. Surfacing the
                # effective agent to the UI is a separate, non-destructive change.
                logger.info(
                    "Slot %s requested agent %r, which currently resolves to %r",
                    name,
                    agent,
                    bindings.resolved_alias or "(default)",
                )
        except Exception:
            logger.warning("Failed to resolve bindings for slot create", exc_info=True)
        if resolving_key and (
            state._slots.get(resolving_key) is not resolving_slot
            or (
                resolving_slot is not None
                and resolving_fields
                != (
                    resolving_slot.agent,
                    resolving_slot.project,
                    resolving_slot.workspace,
                    resolving_slot.memory_store,
                    resolving_slot._app,
                    effective_session_key(resolving_slot),
                )
            )
        ):
            return web.json_response(
                {"error": "slot changed during agent resolution", "code": "session_rebound"},
                status=409,
            )
    # The owner's member assignment below can refuse a new slot's agent. Ask the
    # same question here, before the mint, the session folder or the folder
    # un-hide, so the common refusal builds nothing. The assignment still runs
    # after the mint, and a refusal there is retracted as before.
    preflight_key = _normalize_slot_key(str(name)) if name else ""
    if (
        cfg is not None
        and is_owner_dashboard_request(request)
        and (not preflight_key or preflight_key not in state._slots)
    ):
        preflight_project = folder_project or await _birth_default_project(cfg, workspace)
        try:
            await _check_new_slot_assignment(cfg, agent, agent_kind, preflight_project, memory_mode)
        except Exception as exc:
            from kiro_crew.dashboard.handlers.memory import _store_unavailable_response

            return _store_unavailable_response("", exc)
    # Resolved before the mint decision below: nothing may await between that
    # read and `get_or_create_slot`, and these are the two awaits this path adds.
    cron_creator = await cron_slot_creator(request)
    persisted_cron_slot = False
    persisted_cron_meta: dict[str, Any] = {}
    if cron_creator and name:
        refusal, persisted_cron_slot, persisted_cron_meta = await cron_creator_admission(
            request, state, str(name), cron_creator
        )
        if refusal is not None:
            return refusal

    # Whether this request will MINT a new live slot, decided before
    # get_or_create_slot runs. `name` can address an already-open slot, which
    # returns unchanged. A persisted key with no live slot still follows the
    # normal create path. Its saved title is handled separately below.
    # Computed on the normalized key, which is the key the slot store is built
    # from; an omitted or degenerate name is always a mint. The creator fence
    # already read any saved cron state before this decision.
    _requested_key = _normalize_slot_key(str(name)) if name else ""
    reopening_persisted_cron = bool(
        cron_creator
        and _requested_key
        and persisted_cron_slot
        and _requested_key not in state._slots
    )
    is_new_slot = not _requested_key or _requested_key not in state._slots
    # A named NEW app slot must not land on a transcript the app does not own:
    # the slot's first save would stamp the app onto that metadata line, which is
    # what /api/sessions trusts.
    if (
        is_new_slot
        and _requested_key
        and await _app_claim_refused(
            state, request_app, "chat_slot_create", (_history_key_for(_requested_key),)
        )
    ):
        return slot_not_found()

    # Coalesce every push inside into ONE broadcast at exit, so the first frame
    # any client sees already carries the folder, title, artifact binding and
    # project. Otherwise each of those is a separate post-create correction the
    # UI renders as a jump.
    #
    # A successful ordinary local New Chat is different: the POST response itself
    # carries the complete slot and the client inserts it before activating the
    # tab. Serializing every open slot before returning only makes that first paint
    # wait. The state-owned 10 ms handoff moves its coalesced full-list frame past
    # the response. Only known request keys may qualify; metadata-bearing,
    # app-owned, mode-specific and nonpersistent creates remain
    # synchronous. Errors before a successful handoff retain request-visible
    # publication semantics; delayed publication failures are logged and retried
    # once by the guarded callback.
    defer_plain_create_broadcast = bool(
        set(body).issubset(_DEFERRED_PLAIN_CREATE_KNOWN_KEYS)
        and is_new_slot
        and not request_app
        and not folder_id
        and not _mode
        and not body.get("title")
        and not body.get("artifact")
        and memory_mode == "persistent"
        and not body.get("ephemeral")
    )
    # Crew Mode new-chat counters, decided at the mint and emitted only after the
    # creation block exits without a refusal or a retraction.
    count_new_chat = False
    born_crew = False
    crew_repeat = False
    async with contextlib.AsyncExitStack() as creation_stack:
        request_deferred_flush = creation_stack.enter_context(state.suspend_slots_push())
        if request_app and _requested_key:
            denied = await _app_slot_acquisition_recheck(
                state, request_app, _requested_key, acquisition_judged, "chat_slot_create"
            )
            if denied is not None:
                return denied
        is_new_slot = not _requested_key or _requested_key not in state._slots
        # The binding is stamped only on the owner's own fresh local create below;
        # any request that would skip that branch is refused, never left unbound.
        if app_agent_owner and not (
            is_new_slot and cfg is not None and is_owner_dashboard_request(request)
        ):
            return web.json_response(
                {"error": "invalid app agent owner", "code": "invalid_app_agent_owner"},
                status=400,
            )
        # Charge only a new allocation, with no await between the check and mint.
        if is_new_slot:
            _rate = _service.check_session_create(admission_gate.actor_budget(request))
            if _rate is not None:
                return _rate.refusal_response()
        try:
            slot = state.get_or_create_slot(
                name,
                agent=agent,
                workspace=workspace,
                model=model,
                mode=_mode,
                memory_mode=memory_mode,
                ephemeral=body.get("ephemeral"),
                app=request.get("app", ""),
                origin=request_slot_origin(request.get("app", ""), cron_creator=cron_creator),
                # Human request-layer path: the dashboard new-chat tab. The
                # origin conjunct in state_parts/slot_directory.py still excludes app-token callers.
                # A slot an attested cron opens is not a person's and is not
                # counted.
                count_user_session=not cron_creator,
            )
        except ValueError as exc:
            if request_app:
                return _app_acquisition_conflict(
                    state,
                    request_app,
                    _normalize_slot_key(str(name)) if name else "",
                    acquisition_judged,
                    exc,
                )
            return web.json_response({"error": str(exc)}, status=409)
        # Set by the member assignment below once it records a pick, so a
        # failure after that point can undo the pick with the slot.
        assignment_key = ""
        selection_change: SelectionChange | None = None
        if is_new_slot:

            async def _retract_newborn_on_raise(
                exc_type: type[BaseException] | None,
                exc: BaseException | None,
                tb: TracebackType | None,
            ) -> bool:
                # The failure returns below retract the newborn themselves. This
                # covers everything else that can leave the block after the
                # mint: an unexpected exception at any await, and a
                # cancellation, which no `except Exception` catches. Registered
                # after `suspend_slots_push`, so it unwinds first and the frame
                # that block flushes on the way out never lists the newborn.
                if exc_type is not None and _retract_failed_newborn(state, slot):
                    await drained_to_thread(
                        restore_agent_selection, assignment_key, selection_change
                    )
                return False

            creation_stack.push_async_exit(_retract_newborn_on_raise)
        if is_new_slot and agent_kind and not slot.agent_kind:
            # The namespace the name was stated (or stamped) in travels with the
            # slot from birth, for every caller: the owner branch below re-records
            # it from the committed resolution, and a non-owner create would
            # otherwise leave a template-stamped slot reading as a bare name.
            slot.agent_kind = agent_kind
        if reopening_persisted_cron:
            # The creator fence already loaded and authorized this metadata
            # snapshot. Reuse it so a closed-session reopen restores the user's
            # saved title provenance without a second store read.
            persisted_title = persisted_cron_meta.get("title")
            if isinstance(persisted_title, str) and persisted_title:
                _rehydrate_slot_title(
                    slot,
                    persisted_title,
                    titled=True,
                    metadata=persisted_cron_meta,
                )
                _rebase_rehydrated_refresh_mark(slot)
        if cron_creator and is_new_slot:
            # Same attribution as the send auto-create: a cron-opened slot
            # carries its creator and never reads as a person's own tab.
            slot._created_by = cron_creator
        if is_new_slot:
            # Shared Crew creator: the actor the auth middleware published,
            # write-once. Function-local: this module runs on the facade globals.
            from kiro_crew.shared_crew.attribution import stamp_creator
            from kiro_crew.shared_crew.audit import schedule_action_audit
            from kiro_crew.shared_crew.contracts import ActionOutcome, ActionType
            from kiro_crew.shared_crew.identity import actor_of

            _sc_actor = actor_of(request)
            stamp_creator(slot, _sc_actor)
            schedule_action_audit(
                ActionType.SESSION_CREATE,
                session_key=slot.key,
                initiator=_sc_actor,
                outcome=ActionOutcome.APPLIED,
                target=slot.key,
            )
        if is_new_slot and cfg is not None:
            if is_owner_dashboard_request(request):
                # Take the slot lock before the newborn's first await: a later
                # same-name member pick changes its namespace without changing
                # the agent/project strings checked after publication.
                await creation_stack.enter_async_context(slot._lock)
        if slot.is_restricted:
            logger.info("Slot %s created with memory_mode=%s", slot.key, slot.memory_mode)
        # App ownership check (App Kit §5.2), same deny-by-default rule as
        # api_chat_send. It matters HERE because `name` can address an
        # ALREADY-EXISTING slot: get_or_create_slot returns that slot without
        # consulting ownership, and everything below mutates it (folder, title,
        # artifact binding). Without this an app token could refile or retitle
        # another app's — or the dashboard's — session. A slot this request just
        # created carries `_app == request_app`, so it passes unless
        # get_or_create_slot linked it to another conversation's session (the
        # session half, same rule as every per-slot route); a dashboard caller
        # (empty app) keeps full access.
        # `request_app` is read once at the top of the handler, because the remote
        # binding gate up there needs the same value before the peer is touched.
        # One body for every reason on purpose: a distinct code per reason would
        # turn this 404 into an existence oracle for slots the caller may not
        # know about. The reason stays in the audit row.
        denied = deny_app_slot_session_access(request_app, slot, slot.key, "chat_slot_create")
        if denied is not None:
            if is_new_slot:
                _retract_failed_newborn(state, slot)
            return denied
        # Pin title if explicitly provided (prevents auto-title from overwriting).
        # A cron caller uses this route as both create and reopen. Its title names
        # the new session, but a later reopen must not undo a user's rename.
        title = (body.get("title") or "").strip()[:200] if isinstance(body, dict) else ""
        if cron_creator and (not is_new_slot or reopening_persisted_cron):
            title = ""
        if title:
            title, _ = redact_exfiltration_urls(title)
            title, _ = redact_credentials(title)
            slot.title = title
        if title:
            # A pinned title is caller-explicit: record origin "user" so the
            # background title refresh never rewrites it (this endpoint can
            # address an ALREADY-auto-titled slot whose origin would otherwise
            # stay "auto"), and bump the epoch so an in-flight background
            # attempt stands down instead of clobbering the pin.
            slot._titled = True
            slot._title_origin = "user"
            slot._title_epoch += 1
        # Bind to an artifact if provided (companion chat). Validate
        # against the artifact slug grammar so an injection-shaped value can never
        # land on the slot; anything invalid is silently dropped. Uniqueness (≤1
        # active bound session per slug) is a frontend-flow convention, not
        # enforced here.
        artifact_slug = body.get("artifact") if isinstance(body, dict) else None
        if isinstance(artifact_slug, str) and ARTIFACT_SLUG_RE.match(artifact_slug):
            slot._artifact = artifact_slug
        # File the slot before the coalesced broadcast, so its first appearance
        # in every client is already inside the folder.
        folder_applied = False
        if folder_id:
            # Mirror PATCH /api/chat/slots/{slot}/folder: a CHANGED folder must
            # re-inject the [FOLDER] breadcrumb on the next turn. `is_new` alone
            # is not enough — `name` can address an already-used slot, whose
            # turn is `is_new=False`, so moving it would otherwise leave the
            # model believing the session is still in its old folder.
            # Harmless on the new-slot path: that turn is `is_new`, so the
            # breadcrumb fires regardless and the flag is consumed there.
            previous_folder = slot.folder_id
            previous_changed = slot._folder_changed
            if folder_id != slot.folder_id:
                slot._folder_changed = True
            slot.folder_id = folder_id
            # Existence is only reliable inside the store lock. If the folder
            # went away, abandon THIS assignment and leave the slot as it was —
            # `name` can address an already-used slot, so clearing outright would
            # unfile a conversation that was sitting in a perfectly good folder
            # of its own. This is a chat turn, so declining the move beats
            # failing the turn. The folder's un-hide and claim are writes, so
            # they run once nothing later can refuse the create (below);
            # this only reads whether the folder is there.
            claim_folder = (
                folder_id != previous_folder and request.headers.get("X-Internal-Secret") is None
            )
            if not await state.read_folders(
                lambda folders: any(f["id"] == folder_id for f in folders)
            ):
                slot.folder_id = previous_folder
                slot._folder_changed = previous_changed
            else:
                folder_applied = True
                if is_new_slot:
                    # Folder-tag inheritance, creation-only. A brand-new
                    # chat filed into a folder copies that folder's tags by value onto
                    # its own tag list — the folder's tags are an organizational
                    # default for chats started inside it. Follows chat_fork.py's
                    # copy-by-value style.
                    #
                    # Gated on is_new_slot so re-opening an existing session inside
                    # the folder never re-stamps tags, and confirmed only after
                    # _unhide_folder reported the folder EXISTS (its read is under the
                    # store lock, the only race-free place to look it up). Direct
                    # folder only: no ancestor/subfolder transitivity. Ids are
                    # re-validated against the live vocabulary and appended only when
                    # not already present, so a stale id on the folder is dropped
                    # rather than written onto the slot.
                    def _read_folder_tags(folders: list[dict[str, Any]]) -> list[str]:
                        f = next((x for x in folders if x["id"] == folder_id), None)
                        tags = f.get("tags") if f else None
                        return list(tags) if isinstance(tags, list) else []

                    # One shared definition of "an inheritable folder tag id"
                    # (string, in the live vocabulary) — see validate_folder_tag_ids
                    # for why each guard exists. The READ, the intersection AND the
                    # apply all sit under tags_write_lock (the invariant every
                    # consumer follows, matching the channel-filing path): a folder
                    # PATCH or tag deletion committing after an earlier read would
                    # otherwise stamp a stale tag set or resurrect a deleted id onto
                    # the new slot. Lock ordering (tags_write_lock → folder-store
                    # lock) matches the folder create/PATCH paths.
                    async with tags_write_lock(state):
                        inherited = await state.read_folders(_read_folder_tags)
                        appended = False
                        for tid in validate_folder_tag_ids(inherited, state):
                            if tid not in slot.tags:
                                slot.tags.append(tid)
                                appended = True
                        # "tags changed => revision changed": the awaited folder
                        # read above is a window in which a concurrent slots GET
                        # can snapshot the empty newborn under its birth revision;
                        # the inherited list must not ship under that same one.
                        if appended:
                            _bump_slot_tags_revision(slot)
        # A slot with no project filed into a project-linked folder inherits
        # from the nearest configured ancestor before its first broadcast. The
        # server owns this fallback because the client folder cache can be
        # temporarily stale; existing named slots with an explicit project keep
        # it and continue to use the project endpoint for scope changes.
        if folder_project and folder_applied and not slot.project:
            slot.project = folder_project
        # A fresh folder per new chat, when the owner turned it on. Only for a
        # brand-new unnamed dashboard chat: a named create can reopen a saved
        # session, and apps, crons and ephemeral helpers keep their own rules. A
        # project-linked folder (applied above) wins, an artifact companion chat
        # keeps the default, and a dashboard caller opts out with
        # `no_session_folder` when it sets the project itself right after the
        # create or opens a purpose-built chat (an app, an intake, a hand-off).
        # A refused root or a failed create falls through to the default below
        # and is reported in the response, so the chat still opens and the user is
        # told why it has no folder of its own.
        session_folder_notice = ""
        session_folder_root = getattr(cfg.dashboard, "session_folder_root", "") if cfg else ""
        if (
            isinstance(session_folder_root, str)
            and session_folder_root
            and not slot.project
            and is_new_slot
            and not _requested_key
            and body.get("no_session_folder") is not True
            and not body.get("artifact")
            and not request_app
            and not cron_creator
            and not body.get("ephemeral")
            and is_owner_dashboard_request(request)
        ):
            try:
                slot.project = await asyncio.to_thread(create_session_folder, session_folder_root)
            except SessionFolderError as exc:
                logger.warning("Slot %s: no session folder (%s)", slot.key, exc)
                session_folder_notice = "session_folder_unavailable"
        # Default project to workspace directory so file search works out of the box
        if not slot.project:
            slot.project = await _birth_default_project(cfg, workspace)
        if is_new_slot and cfg is not None:
            if is_owner_dashboard_request(request):
                assignment_key = effective_session_key(slot)
                assignment_agent = slot.agent
                assignment_project = slot.project
                await creation_stack.enter_async_context(_slot_switch_session_lock(assignment_key))
                try:
                    # An explicit template choice is the shared template even when
                    # a member carries the same name: it never pins member memory.
                    assigned_store = (
                        ""
                        if agent_kind == "template"
                        else await pin_private_agent_store(
                            state, assignment_key, agent, cfg, memory_mode=slot.memory_mode
                        )
                    )
                    chosen = await asyncio.to_thread(
                        resolve_agent_bindings,
                        cfg,
                        assignment_agent,
                        assignment_project or None,
                        validate_memory_files=False,
                        selection_kind=agent_kind,
                    )
                    # Availability was settled before the mint; this records
                    # the namespace the pick was committed in.
                    slot.agent_kind = chosen.selection_kind
                    if app_agent_owner:
                        slot.app_agent_owner = app_agent_owner
                    selection_change = await _record_explicit_agent_selection(
                        assignment_key,
                        assignment_agent,
                        chosen,
                        config=cfg,
                        memory_mode=slot.memory_mode,
                        app=slot._app or "",
                    )
                except Exception as exc:
                    from kiro_crew.dashboard.handlers.memory import _store_unavailable_response

                    # The selection record returns its change only on success,
                    # so this path has no recorded pick to restore.
                    _retract_failed_newborn(state, slot)
                    return _store_unavailable_response(slot.memory_store, exc)
                if (
                    state._slots.get(slot.key) is not slot
                    or effective_session_key(slot) != assignment_key
                    or slot.agent != assignment_agent
                    or slot.project != assignment_project
                ):
                    _retract_failed_newborn(state, slot)
                    await drained_to_thread(
                        restore_agent_selection, assignment_key, selection_change
                    )
                    return web.json_response(
                        {
                            "error": "Could not save the member assignment. Try again.",
                            "code": "session_rebound",
                        },
                        status=409,
                    )
                if assigned_store:
                    slot.memory_store = assigned_store
        if is_new_slot and not getattr(slot, "crew_mode_agent", ""):
            # A chat born on the Crew Mode conductor (the conductor as the default
            # template, or a create naming it) is stamped the way the agent switch
            # stamps one, so it stays in Crew Mode after `agent.crew_mode_agent`
            # changes. The stamp counts only while the chat is still on that agent.
            # Stamped after the owner branch records the namespace the pick was
            # committed in, and before the slot's first save and frame; the counters
            # are emitted once the create has committed, below.
            from kiro_crew import crew_mode

            slot.crew_mode_agent = crew_mode.stamp_for_switch(slot.agent, slot.agent_kind)
            # Only an unnamed create is certainly a new conversation: a named one
            # can reopen a saved session, the same reason the session counter in
            # `get_or_create_slot` and the session folder below skip it.
            if not _requested_key:
                count_new_chat = True
                born_crew = bool(slot.crew_mode_agent)
                crew_repeat = born_crew and bool(crew_mode.known_roots() - {slot.key})
        _sync_dashboard_slots(state)
        # Persist INSIDE the suspension, ahead of the coalesced broadcast, the
        # same ordering `session_control_parts/session_create.py`'s create span uses ("the whole
        # allocation-to-persist span runs under `suspend_slots_push`", so "a
        # slot whose birth write fails is never broadcast at all"). Two paths
        # mint a slot through this context manager; leaving the durable write
        # outside it is what makes the failure below reachable:
        #
        # `suspend_slots_push`'s `__exit__` flushes the owed push, and on the
        # coalescing window's LEADING edge that flush broadcasts synchronously
        # (`state.push_slots_update`). An exception there — a non-serializable
        # value reaching `json.dumps` is the evidenced shape —
        # escapes `__exit__`, so with the write out here it skipped a metadata
        # mutation the request had already acknowledged: this `force=True` save
        # is the ONLY durable record of a recreate's folder filing or pinned
        # title (see `_save_slot_to_history`'s message-less merge), and the slot
        # itself survives in memory, so nothing later reconciles the two. Every
        # client repairs a dropped FRAME on its next read; none of them repairs
        # a write that never happened.
        #
        # The cost this ordering accepts, named by the comment it replaces: the
        # suspension is process-wide, so other clients' slot updates coalesce
        # (they defer — no caller blocks) until this off-loop write completes,
        # and a contended history lock takes the patient acquire. Accepted for
        # the same reason the twin accepts it, which awaits a cross-process
        # metadata write inside its own suspension; this span already suspends
        # on the workspace-conflict probe above, so it was never await-free.
        #
        # A pinned title must persist too (not just a folder move): without the
        # write, a restart rehydrates the previous title with a refreshable
        # "auto" origin and the background refresh may rewrite the pin.
        if app_agent_owner:
            # The app-agent binding is what makes every turn re-check the agent,
            # and nothing else records it: the selection record omits it, and a
            # message-less newborn has no metadata line, so the save path's
            # empty-window merge writes nothing and a restart would restore the
            # slot unbound. The birth line is written here, awaited and checked,
            # and the create fails rather than hand the palette a session that
            # would lose its binding. The newborn is retracted on that failure,
            # the same retraction `session_control_parts/session_create.py`'s create uses: left in
            # `state._slots` it would still go out in the coalesced broadcast
            # this suspension owes, so every client would show a session the
            # caller was told did not get created, a retry would mint a second
            # one, and -- message-less, so the dirty re-save writes no line -- a
            # restart would bring it back without its binding. Retracted only
            # while it is still provably the newborn: a turn that started on it
            # during the write keeps the slot (liveness over tidiness).
            # A shutdown mid-write is the same unbound newborn, retracted on the
            # way out by the unwind callback registered at the mint.
            persisted = await _persist_app_agent_owner(state, slot)
            if not persisted:
                if _retract_failed_newborn(state, slot):
                    # The pick recorded above names a key no slot holds now;
                    # undo it with the slot, as the `session_rebound` branch does.
                    await drained_to_thread(
                        restore_agent_selection, assignment_key, selection_change
                    )
                else:
                    slot._dirty = True
                return web.json_response(
                    {
                        "error": "Could not save the new session. Try again.",
                        "code": "app_agent_owner_unpersisted",
                    },
                    status=503,
                )
        if work_item is not None and not is_new_slot:
            # A work item binds only a slot this create minted, never an open one.
            return web.json_response(
                {
                    "error": "a create with a work_item makes a new session",
                    "code": "work_item_needs_new_slot",
                },
                status=400,
            )
        if work_item is not None:
            # The last step that can refuse the create, so nothing below (the
            # folder claim, the metadata save) runs for a refused one. Bound before
            # the first frame, so no client sees the worker unbound.
            from kiro_crew.dashboard.handlers.remote_work_item import (
                bind_work_item,
                discard_work_item,
            )

            bound_item = work_item

            async def _unbind_retracted_worker(
                exc_type: type[BaseException] | None,
                exc: BaseException | None,
                tb: TracebackType | None,
            ) -> bool:
                # Registered before the bind and run before the mint's own unwind:
                # a newborn retracted on the way out, a cancellation included, takes
                # its mailbox item and binding with it. The discard touches only a
                # binding this slot holds, so a refused or unfinished bind is a no-op.
                if exc_type is not None and _retract_failed_newborn(state, slot):
                    # One drained step: the discard, then the selection restore.
                    await discard_work_item(
                        slot.key,
                        bound_item,
                        then=lambda: drained_to_thread(
                            restore_agent_selection, assignment_key, selection_change
                        ),
                    )
                return False

            creation_stack.push_async_exit(_unbind_retracted_worker)
            refusal = await bind_work_item(request, slot.key, work_item)
            if refusal is not None:
                if _retract_failed_newborn(state, slot):
                    await drained_to_thread(
                        restore_agent_selection, assignment_key, selection_change
                    )
                return refusal
            # A bound worker must survive a restart before its first message, or its
            # item would outlive it; the birth line is what brings a slot back.
            if not await _persist_birth_line(state, slot):
                if _retract_failed_newborn(state, slot):
                    await discard_work_item(
                        slot.key,
                        work_item,
                        then=lambda: drained_to_thread(
                            restore_agent_selection, assignment_key, selection_change
                        ),
                    )
                else:
                    slot._dirty = True
                return web.json_response(
                    {
                        "error": "Could not save the new session. Try again.",
                        "code": "worker_unpersisted",
                    },
                    status=503,
                )
        if folder_applied:
            # Past the last step that can refuse the create, so the folder's
            # un-hide and claim are written only for a create that stands. A
            # person opening a chat in the folder (no internal secret: the
            # browser) claims it, so an agent's chat_folder_delete refuses it
            # from then on. A folder deleted since the read above declines the
            # move, as it would have at that read.
            if not await _unhide_folder(state, folder_id, claim_for_person=claim_folder):
                slot.folder_id = previous_folder
                slot._folder_changed = previous_changed
                folder_applied = False
        if folder_id or title:
            # The create/recreate request has been authorized against this
            # transcript.  Do not let a rebind while the off-loop write waits on
            # the history lock redirect its newly supplied metadata to another
            # session.
            #
            # No slot retraction on failure, unlike the twin: there the write is
            # the newborn's only record, so an unpersisted slot would vanish on
            # restart and retracting is the lesser evil. Here the slot already
            # has a metadata line and `best_effort` (default) logs the failure
            # and marks the slot dirty so the periodic flush retries it, which
            # is the retry the metadata mutation routes rely on.
            await save_slot_off_loop(
                state,
                slot,
                force=True,
                expected_history_key=slot_history_key(slot),
            )
        # Guarantee a frame. get_or_create_slot pushes for a NEW slot, but
        # returns an existing named slot without pushing — and this handler is
        # now the only thing that files a slot (the client sends no follow-up
        # PATCH to supply that push). Without this, re-creating an
        # existing slot name with a different folder_id would move it for the
        # requester while every other connected client kept the stale
        # placement. Inside the suspension this only marks a push owed, so the
        # new-slot path still emits exactly ONE coalesced frame.
        state.push_slots_update()
        if defer_plain_create_broadcast:
            request_deferred_flush(f"create slot {slot.key!r}")
    # The create is committed here: every refusal and retraction above returns
    # from inside the block, and an exception leaving it retracts the newborn.
    if count_new_chat:
        from kiro_crew.metrics import crew_mode as crew_mode_metrics

        crew_mode_metrics.record_new_chat(crew_mode=born_crew)
        if born_crew:
            crew_mode_metrics.record_turned_on("create", repeat=crew_repeat)
    # Speculative session creation: overlap the ACP handshake with the user's
    # think-time before their first message. No-op unless session.eager_spawn.
    #
    # Skipped for a relay archive: it never runs, so a spawned kiro-cli would
    # idle until it timed out.
    #
    # A NEW owner chat starts on the default session environment when one is set,
    # unprewarmed: a local spawn would run on the wrong machine, a remote one would
    # start billed compute for a chat that may never send a message.
    bound_remote = False
    if slot.executor != "remote" and existing_slot is None and is_owner_dashboard_request(request):
        from kiro_crew.dashboard.handlers.session_environments import (
            apply_default_environment,
        )

        bound_remote = await apply_default_environment(
            slot,
            getattr(state, "sessions", None),
            getattr(state, "_terminal_sessions", None),
        )
    if slot.executor != "remote" and not bound_remote:
        schedule_eager_spawn(state, slot, start_priority=owner_start_priority(request))
    created = _created_slot_row(state, request, slot)
    if session_folder_notice:
        created["session_folder_notice"] = session_folder_notice
    if headroom is not None:
        created["headroom"] = headroom
    if work_item is not None:
        created["work_item_id"] = work_item.item_id
    return web.json_response(created)


async def _persist_app_agent_owner(state: DashboardState, slot: _ChatSlot) -> bool:
    """Durably record *slot*'s app-agent binding before its create is answered.

    The birth line carries the binding; see :func:`_persist_birth_line`.
    """
    return await _persist_birth_line(state, slot)


async def _persist_birth_line(state: DashboardState, slot: _ChatSlot) -> bool:
    """Durably record *slot* before its create is answered, so a restart keeps it.

    A message-less newborn otherwise has no metadata line, and a restart does not
    bring it back. A line-less newborn gets the full metadata line a first save
    would write; a slot that already has a line takes the force save. False when
    neither write committed.
    """
    log = state.conversation_log
    if log is None:
        return False
    history_key = slot_history_key(slot)
    from kiro_crew.dashboard.slot_persistence import metadata_codec

    line = {
        "_type": "metadata",
        **metadata_codec.encode(
            slot,
            folds=metadata_codec.SaveFolds(
                memory_mode=getattr(slot, "memory_mode", "persistent"),
                created_at=slot.created_at,
                tab_id=getattr(slot, "_tab_id", None),
            ),
        ),
    }
    try:
        if await drained_to_thread(
            log.update_metadata_if, history_key, line, lambda meta: not meta
        ):
            return True
        return await save_slot_off_loop(
            state,
            slot,
            force=True,
            best_effort=False,
            expected_history_key=history_key,
        )
    except Exception:
        logger.warning(
            "chat_slot_create: could not persist the birth line for slot=%s",
            slot.key,
            exc_info=True,
        )
        return False
