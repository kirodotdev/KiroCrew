"""``POST /api/agents`` and ``GET /api/agents/resolved-model``, with the effort and memory-store field rules and the session-defaults refresh the crew writers share."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.agents import (
        ASSISTANT_MEMBER_RESERVED,
        ASSISTANT_NAME_TAKEN,
        DEFAULT_MEMORY_STORE,
        EFFORT_LEVELS,
        EFFORT_VALUES,
        TEMPLATE_NAME_RE,
        KiroCrewAgentConfig,
        KiroCrewConfig,
        MemberAlreadyExists,
        MemberNameError,
        UnknownMemoryStore,
        _assistant_name_taken_response,
        _assistant_reserved_response,
        _drained_to_thread,
        _foreign_private_copy_owner,
        _get_config_lock,
        _is_ghost_shaped,
        _model_pin_rejected,
        _name_would_be_masked,
        _pin_entitlement_backend,
        _require_owner,
        _revalidate_crew_pin,
        _safe_avatar,
        _safe_color,
        _sel,
        _UnverifiableLineage,
        discovery_executor,
        key_new_crew,
        list_agents,
        logger,
        memory_store_binding_defect,
        normalize_agent_model,
        persist_member_config,
        provision_member_memory,
        resolve_agent_identity,
        resolve_effective_model,
        retire_unpublished_allocation,
        teams_mod,
        validate_member_name,
    )


async def api_kirocrew_agent_resolved_model(request: web.Request) -> web.Response:
    """GET /api/agents/resolved-model?agent=NAME — the model a new session uses.

    Serves the one backend resolver so the dashboard's model chip does not have
    to re-derive the precedence client-side (and drift from it). ``agent`` is a
    Kiro Crew agent name; omitted falls back to the configured default agent.
    ``model`` is "" when every tier defers to the backend's own choice.
    """
    agent_name = request.query.get("agent", "").strip()
    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    # Globs ~/.kiro/agents and may read the installed agent file — keep the
    # filesystem work off the event loop.
    model = await asyncio.to_thread(resolve_effective_model, cfg, agent_name or None)
    alias, kiro_agent, model_pin = await asyncio.to_thread(
        resolve_agent_identity, cfg, agent_name or None
    )
    # Effort resolves through its own chain, served from the SAME resolver the
    # provider factory calls so the pane cannot disagree with what a session will
    # actually run at -- including the role-aware default, which a crew bound to a
    # background worker agent takes instead of the chat default. Keyed on what the
    # bindings resolved, which is what makes an omitted `agent` answer for the
    # configured default crew rather than for no crew at all.
    crew_effort = cfg.crew_pinned_effort(None, alias)
    session_effort = cfg.resolve_session_effort(kiro_agent, alias)
    return web.json_response(
        {
            "model": model,
            "agent": agent_name,
            "kiro_agent": kiro_agent,
            # Whether the agent itself pins the model, vs inheriting it.
            "pinned": bool(model_pin),
            # The effort a new session on this crew starts at, and whether the
            # crew pinned it or inherited a default. "" means no tier pins one
            # and the model's own default applies.
            "reasoning_effort": session_effort,
            "effort_pinned": bool(crew_effort),
        }
    )


async def _refresh_session_defaults(request: web.Request, crew: str) -> None:
    """Rebuild the provider factory so a crew's new effort pin reaches new sessions.

    The factory resolves the pin from the config it captured when it was built, so
    a write alone stays invisible until a restart. That is the same staleness
    ``api_kirocrew_config_patch`` already handles for ``agent.reasoning_effort``
    and ``agent.role_efforts.*``, and for the same reason it uses
    ``refresh_defaults()`` rather than ``reload_provider_factory()``: the factory
    is rebuilt and the warm pool drained WITHOUT touching live sessions, so an
    in-flight turn is not killed because a default changed. The pool must drain
    either way -- a pre-warmed child carries the old effort overlay, and the claim
    path never re-pushes effort.

    Best-effort: the write is already durable, so a crew save must not fail
    because the refresh did. A failure costs one gateway lifetime of staleness.
    """
    state = request.app.get("state")
    sessions = getattr(state, "sessions", None)
    if sessions is None:
        return
    try:
        await sessions.refresh_defaults()
    except Exception:
        logger.warning(
            "Could not refresh session defaults after saving crew %r; the pin "
            "applies from the next gateway start",
            crew,
            exc_info=True,
        )


def _crew_effort_rejected(raw: object) -> str | None:
    """Reason a crew's reasoning-effort pin is unusable, or ``None`` to allow it.

    Rejects rather than coercing, which is the opposite of the config-file load
    path (:func:`coerce_effort`). The difference is who is watching: a typo in a
    hand-edited file must not stop the gateway from booting, but a typo sent by
    the crew form has an author on the other end, and silently storing ``""``
    would read back as "inherits" and look like the save was lost.

    ``""`` is the inherit sentinel and always allowed -- it is how a pin is
    cleared.
    """
    if not isinstance(raw, str):
        return "reasoning_effort must be a string"
    val = raw.strip()
    if val in EFFORT_VALUES:
        return None
    return "reasoning_effort must be one of: " + ", ".join(("(empty)", *EFFORT_LEVELS))


def _crew_memory_store_rejected(raw: object) -> str | None:
    """Reason a crew's memory-store binding is unusable, or ``None`` to allow it.

    The rules themselves are ``memory_stores``' and are never restated here: a
    second copy of the shape rule is how the write boundary comes to accept a name
    the resolvers refuse to compose a path for, and that refusal would then surface
    at the crew's first memory write rather than on the form that authored it.

    Rejects rather than degrading, for the same reason as
    :func:`_crew_effort_rejected`: the value has an author on the other end, and a
    name quietly degraded onto another crew's silo reads back as a save that was
    lost while the crew files its memory somewhere it was never bound.

    This helper checks only shape. The create and update handlers separately
    enforce automatic allocation and immutable private ownership.
    """
    defect = memory_store_binding_defect(raw)
    if defect is None:
        return None
    return (
        f"memory_store {raw!r} is not a usable store name ({defect}); use lowercase "
        "letters, digits and hyphens, or '' for automatic provisioning on creation"
    )


async def _mark_first_greeting_owed(member_id: str, name: str) -> None:
    """Record that the crewmate just created opens its chat with its goal question.

    Best-effort: the crewmate exists either way, and a failed record only means
    its chat opens empty, so it is logged rather than turned into a failed
    create. Keyed by the immutable ``member_id``, which is the slug its thread
    and member directory use (``members.member_slug``).
    """
    from kiro_crew.dashboard.captain_greeting import mark_greeting_owed

    if not member_id:
        return
    try:
        await asyncio.to_thread(mark_greeting_owed, member_id)
    except Exception:
        logger.warning("could not record the first greeting owed to %r", name, exc_info=True)


async def api_kirocrew_agents_create(request: web.Request) -> web.Response:
    """POST /api/agents — create a new Kiro Crew agent.

    Wrapped in the UI-guide commit hook: when the owner's tab names a guide
    waiting on its ``crewmate.create`` step (``X-Guide-*`` headers), the guide is
    credited only from THIS handler's own success response and its immutable
    ``member_id``. Without the headers it is exactly the handler below.
    """
    from kiro_crew.dashboard.handlers.guide import run_guided_crewmate_create

    # The hook returns the inner handler's own response object unchanged.
    return cast(
        web.Response, await run_guided_crewmate_create(request, _api_kirocrew_agents_create)
    )


async def _api_kirocrew_agents_create(request: web.Request) -> web.Response:
    """The create itself — see :func:`api_kirocrew_agents_create`."""

    denied = await _require_owner(request, "agent.create")
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be an object", "code": "body_not_object"}, status=400
        )
    name = body.get("name", "")
    if not isinstance(name, str):
        return web.json_response(
            {"error": "Agent name must be text", "code": "invalid_member_name"}, status=400
        )
    if not name:
        return web.json_response({"error": "Agent name is required"}, status=400)
    # Keep the credential-specific response before the shared validator so the
    # rejected value is never echoed through the generic error path.
    if _name_would_be_masked(name):
        return web.json_response(
            {
                "error": (
                    "Agent name looks like a credential or a URL carrying one. "
                    "Pick a name that identifies the crew instead."
                ),
                "code": "credential_shaped_name",
            },
            status=400,
        )
    # The crew name is a display name: it must satisfy the same rule
    # ``GET /api/members`` applies when it lists the roster
    # (``members.validate_member_name``). Persisting a name that fails it -- a
    # tab, a line break, edge whitespace, a hidden character -- would create a
    # crew no roster surface can show or open; refused here, once, for every
    # client of this route. Same BOUNDARY as the credential rule above: names
    # already stored are not renamed.
    try:
        validate_member_name(name)
    except MemberNameError as exc:
        return web.json_response(
            {"error": f"Invalid Crew Member name: {exc}", "code": "invalid_member_name"},
            status=400,
        )
    # The template pointer must be EXPLICIT. Defaulting it to "kirocrew" would
    # make every crew created without naming a template an alias for the DEFAULT
    # agent: dispatch flattens an alias to its `kiro_agent`
    # (config.loader.resolve_agent_bindings), so the crew is offered in the chat
    # picker and then the default answers. "kirocrew" is a perfectly valid CHOICE
    # here (a crew booting the built-in agent against its own workspace/memory
    # store is the common case); only the silent default is refused.
    kiro_agent = str(body.get("kiro_agent") or "").strip()
    if not kiro_agent:
        return web.json_response(
            {
                "error": "kiro_agent is required — name the agent this crew boots "
                "from (pass 'kirocrew' for the built-in agent)",
                "code": "kiro_agent_required",
            },
            status=400,
        )
    # Validate the template identifier before storing or resolving it. This grammar
    # permits published dotted names but still rejects paths, spaces, and punctuation
    # at either edge.
    if not TEMPLATE_NAME_RE.fullmatch(kiro_agent):
        return web.json_response(
            {"error": "invalid kiro_agent name", "code": "invalid_kiro_agent_name"},
            status=400,
        )
    # Existence is resolved through `list_agents()`, which reads every spec via the
    # hardened reader: it resolves symlinks, refuses a spec whose REAL target is
    # sensitive, and goes through the same gate as every other dashboard file read.
    # A direct filename probe here called `Path.read_text()` itself, so a namespaced
    # agent file symlinked at a credentials path would have been read outside that
    # gate. `list_agents()` is also the broader and more accurate notion of
    # existence: it includes edition-provided rows that are ACP-resolvable with no
    # on-disk file, which is what "will this actually dispatch" means.
    # Off the loop: it scans and parses the agent directories.
    known_agents = await asyncio.get_running_loop().run_in_executor(
        discovery_executor(), lambda: {a.name for a in list_agents()}
    )
    template_missing = kiro_agent not in known_agents
    # Unknown-but-accepted: an edition may resolve a row this listing cannot see,
    # so refusing here would break a legitimate crew. WARN instead — the same
    # posture, and for the same reason, as the sync path's EXECUTABLE INVARIANT
    # check — so a crew that will fail at spawn leaves a trace rather than
    # failing silently later.
    if template_missing:
        logger.warning(
            "creating crew %r against template %r, which is not in the installed "
            "agent listing — if it is not ACP-resolvable the crew will fail at spawn",
            name,
            kiro_agent,
        )
    # Passed RAW, not str()-coerced: normalize_agent_model is total and maps a
    # non-string to "" (inherit). Wrapping in str() first would turn
    # {"model": 123} into the literal "123", which normalizes to a string the
    # backend then rejects as an unknown model id.
    model = normalize_agent_model(body.get("model"))
    _raw_color = body.get("session_color", "")
    session_color = _safe_color(_raw_color)
    if _raw_color not in ("", None) and not session_color:
        return web.json_response(
            {"error": "session_color must be #rrggbb or empty", "code": "invalid_color_hex"},
            status=400,
        )
    _raw_effort = body.get("reasoning_effort", "")
    effort_reason = _crew_effort_rejected(_raw_effort)
    if effort_reason:
        return web.json_response(
            {"error": effort_reason, "code": "invalid_reasoning_effort"}, status=400
        )
    reasoning_effort = _raw_effort.strip()
    # Same placement rule as the other pre-lock validations: refused before any
    # state is touched. Presentation only, but strictly a string — every roster
    # surface renders it verbatim in place of the name.
    _raw_display = body.get("display_name", "")
    if not isinstance(_raw_display, str):
        return web.json_response(
            {"error": "display_name must be a string", "code": "invalid_display_name"},
            status=400,
        )
    display_name = _raw_display.strip()
    # Same convention as session_color: a non-empty raw value that the coercer
    # collapses to "no override" is a caller mistake worth a 400, not a silent
    # fallback to the name-derived face. The one exception is a well-formed
    # ghost override whose traits all coerce to absent: that collapse is the
    # validator's own all-empty→reset rule, not caller junk, so it stores as
    # the canonical reset rather than being refused.
    _raw_avatar = body.get("avatar")
    avatar = _safe_avatar(_raw_avatar)
    if _raw_avatar not in (None, {}) and not avatar and not _is_ghost_shaped(_raw_avatar):
        return web.json_response(
            {
                "error": "avatar must be {'kind': 'ghost', 'traits'/'motions'/'sounds': {...}}, {'kind': 'image'}, {'kind': 'pack', 'id': ...}, or empty",
                "code": "invalid_avatar",
            },
            status=400,
        )
    if avatar.get("kind") == "image":
        # A crew that does not exist yet cannot have staged a picture (the
        # upload endpoint 404s for unknown names), so an image override on
        # create can never have a file to commit.
        return web.json_response(
            {
                "error": "upload the picture after creating the crew",
                "code": "avatar_file_missing",
            },
            status=400,
        )
    # Old clients still send default/empty on create. They now mean automatic
    # private allocation; no caller can choose or reuse another member's store.
    memory_store = body.get("memory_store", DEFAULT_MEMORY_STORE)
    memory_store_reason = _crew_memory_store_rejected(memory_store)
    if memory_store_reason:
        return web.json_response(
            {"error": memory_store_reason, "code": "invalid_memory_store"}, status=400
        )
    if memory_store not in ("", DEFAULT_MEMORY_STORE):
        return web.json_response(
            {
                "error": "A new Crew Member receives its own empty member memory automatically",
                "code": "member_memory_required",
            },
            status=400,
        )
    pending_reason = await _revalidate_crew_pin(model, request)
    if pending_reason:
        return web.json_response({"error": pending_reason, "code": "invalid_model"}, status=400)
    async with _get_config_lock():
        cfg = KiroCrewConfig.load()
        # The config key is an id and the name the user typed is its label
        # (`members.key_new_crew`, shared with `kirocrew agent create`).
        keyed = key_new_crew(name, display_name, cfg.agents)
        if keyed.code == ASSISTANT_MEMBER_RESERVED:
            return _assistant_reserved_response()
        if keyed.code == ASSISTANT_NAME_TAKEN:
            return _assistant_name_taken_response(keyed.taken)
        if keyed.taken:
            return web.json_response(
                {"error": f"Agent '{keyed.taken}' already exists", "code": "agent_exists"},
                status=409,
            )
        name, display_name = keyed.key, keyed.display_name
        model_reason = _model_pin_rejected(
            model, request, cfg.agent.provider, backend=_pin_entitlement_backend(cfg)
        )
        if model_reason:
            return web.json_response({"error": model_reason, "code": "invalid_model"}, status=400)
        # Checked INSIDE the config lock, immediately before the binding is
        # added: a fork recording lineage after a pre-lock validation must not
        # slip another crew's private copy into this new binding (GPT
        # round-34, same shape as the locked rebind's in-mutate check).
        try:
            owner = await asyncio.to_thread(_foreign_private_copy_owner, name, kiro_agent)
        except _UnverifiableLineage:
            return web.json_response(
                {
                    "error": f"Cannot verify whether '{kiro_agent}' is a private copy; retry.",
                    "code": "lineage_unverifiable",
                },
                status=409,
            )
        if owner:
            return web.json_response(
                {
                    "error": f"Template '{kiro_agent}' is crew '{owner}'s private copy; "
                    "it cannot be bound to another crew.",
                    "code": "foreign_private_copy",
                },
                status=409,
            )
        # A crew that carried this name before may still be listed on a team
        # (every removal path drops it best-effort). persist_member_config
        # purges that INSIDE the registry's locked mutation, right before the
        # record is published, on every create path: the in-process config
        # lock keeps this process's team writes out, and the cross-process
        # sidecar lock keeps `kirocrew agent create` out, so no writer can
        # create and team the same name between the purge and the registration.
        # A purge that cannot be made refuses the create (409 below).
        new_agent = KiroCrewAgentConfig(
            kiro_agent=kiro_agent,
            workspace=body.get("workspace", "default"),
            memory_store=memory_store,
            model=model,
            reasoning_effort=reasoning_effort,
            display_name=display_name,
            description=body.get("description", ""),
            triggers=body.get("triggers", ""),
            source=body.get("source", "kirocrew"),
            session_color=session_color,
            avatar=avatar,
        )
        # Provision against this snapshot; publish the agent and owned store
        # together through persist_member_config's flocked delta and create guard.
        # A fresh allocation that never reached config.json is removed on the way
        # out (retire_unpublished_allocation re-reads the disk under the config
        # lock first, so a publication that did land is kept).
        cfg.agents[name] = new_agent
        previous_store = new_agent.memory_store
        previous_member_id = new_agent.member_id
        try:
            try:
                await _drained_to_thread(provision_member_memory, cfg, name)
                await _drained_to_thread(lambda: persist_member_config(cfg, name, create=True))
            except BaseException:
                allocated = cfg.agents[name].memory_store
                if allocated != previous_store:
                    await _drained_to_thread(
                        lambda: retire_unpublished_allocation(
                            cfg,
                            name,
                            allocated,
                            previous_store=previous_store,
                            previous_member_id=previous_member_id,
                        )
                    )
                raise
        except MemberAlreadyExists:
            return web.json_response(
                {"error": f"Agent '{name}' already exists", "code": "agent_exists"}, status=409
            )
        except teams_mod.TeamsUnavailable as exc:
            return web.json_response(
                {
                    "error": f"A previous crew named '{name}' may still be on a team and "
                    f"the crew-teams store is unavailable ({exc}); retry once it is "
                    "readable and writable.",
                    "code": "teams_unavailable",
                },
                status=409,
            )
        except (OSError, UnknownMemoryStore) as exc:
            return web.json_response(
                {"error": str(exc), "code": "member_memory_unavailable"}, status=409
            )
    # A crew APPEARING changes what the effort chain resolves even with no pin of
    # its own: the factory's captured config does not know the crew, so it cannot
    # read the binding the role default keys on, and a scheduled or messaging
    # session naming that crew would take the chat default instead. Creation is
    # therefore always a change by `_effort_inputs` (None -> a tuple).
    await _refresh_session_defaults(request, name)
    if body.get("first_greeting") is True:
        await _mark_first_greeting_owed(cfg.agents[name].member_id, name)
    _sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="agent.create",
        outcome="success",
        source="dashboard",
        resources=name,
    )
    # `member_id` is the crew's IMMUTABLE identity (allocated with its member
    # memory; `member_config_for_id` resolves it and never a name or slug), so a
    # client that must bind something to the crew it just made -- the Meet
    # CrewMates flow's schedule -- can do so without going back through the
    # mutable display name.
    return web.json_response(
        {
            "ok": True,
            "name": name,
            "memory_store": cfg.agents[name].memory_store,
            "member_id": cfg.agents[name].member_id,
        }
    )
