"""``GET /api/chat/slots/{slot}/steering``: the steering files one chat receives.

Read-only. The rows come from :func:`kiro_crew.steering_plan.chat_steering_plan`,
fed by the Steering tab's own scan (``list_steering_blocking``) for the chat's
project and by the folder collection for the chat's folder roots, so the list
names exactly the files those readers admit. Paths are display paths through the
Steering tab's redaction; no body leaves this route.
"""

from __future__ import annotations

from typing import Any

from aiohttp import web

from kiro_crew.dashboard.state import DashboardState


def _harness_of(provider: Any, backend: str) -> tuple[str, bool]:
    """``(provider_type, native_steering)`` for a live provider, else for *backend*.

    The cold answer resolves the label the way ``provider_label`` does, and
    ``native_steering`` the way ``AcpProvider`` reports it (KAS owns conditional
    steering).
    """
    if provider is not None:
        return str(provider.context_provider_type), bool(provider.native_steering)
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
    from kiro_crew.agent_sdk.drivers.acp import provider_label_for_backend

    return provider_label_for_backend(backend), backend == ACP_BACKEND_KAS


def _resolved_agent(slot: Any, session_key: str, project: str | None) -> str | None:
    """The template this chat's turns run on, resolved as turn admission resolves it.

    ``turn_admission`` passes ``bindings.kiro_agent`` to the context builder, so the
    steering load's default-agent gate and the kiro-cli resources read judge that name,
    not the slot's raw pick (an alias, or empty for the configured default).
    """
    from kiro_crew.config.loader import KiroCrewConfig, resolve_agent_bindings
    from kiro_crew.session_agent_selection import resolve_session_agent_bindings

    bindings = resolve_session_agent_bindings(
        resolve_agent_bindings,
        KiroCrewConfig.load(),
        session_key,
        slot.agent or None,
        project,
        agent_kind=slot.agent_kind,
    )
    return bindings.kiro_agent or slot.agent or None


#: Default of ``_plan_blocking``'s *binding_project*: the same as *project*.
_SAME_PROJECT: Any = object()


def _plan_blocking(
    slot: Any,
    session_key: str,
    project: str | None,
    steering_dirs: tuple[str, ...],
    provider_type: str,
    native_steering: bool,
    native_launch_sources: tuple[str, ...] = (),
    binding_project: Any = _SAME_PROJECT,
) -> list[dict[str, str]]:
    """The rows for one chat. *binding_project* is the project turn admission
    resolves the agent with when it differs from the one context is built for."""
    from pathlib import Path

    from kiro_crew import context as ctx
    from kiro_crew.context_assembly.agent_spec import steering_resource_documents
    from kiro_crew.context_assembly.inclusion import _folder_steering_delivery_snapshot
    from kiro_crew.dashboard.handlers.steering import (
        _display_path,
        _redact_meta,
        list_steering_blocking,
    )
    from kiro_crew.steering_plan import chat_steering_plan

    # The member identity the context builder receives (``prompt_assembly``'s
    # ``member=``): a member chat's envelope decides its steering.
    is_member = slot.mode == "member" and bool(slot.agent)
    if binding_project is _SAME_PROJECT:
        binding_project = project
    agent = None if is_member else _resolved_agent(slot, session_key, binding_project)
    listed = list_steering_blocking(Path(project) if project else None)["files"]
    folder: list[str] = []
    folder_cut: list[str] = []
    if steering_dirs:
        # The same skip the turn takes: the trees a harness delivers, and the
        # documents the serving process's launch record names.
        skip_roots, delivered = _folder_steering_delivery_snapshot(
            provider_type=provider_type,
            native_steering=native_steering,
            project=project,
            native_launch_sources=native_launch_sources,
            agent=agent,
        )
        collection = ctx.collect_folder_steering(
            steering_dirs,
            project=project,
            skip_delivered_roots=skip_roots,
            delivered_sources=delivered,
        )
        # The bounded section the turn renders, at the same cap (``caps.steering``
        # does not scale with the window): a document is in it when its header is.
        # A member chat's essentials envelope decides its folder guides instead, so
        # it renders nothing here and the plan marks those rows ``briefing``.
        scrub = ctx._neutralize_structural_markers
        section = (
            ""
            if is_member
            else ctx.render_folder_steering(
                collection, max_chars=ctx._resolve_caps(None).steering, scrub=scrub
            )
        )
        for path, _body in collection.documents:
            kept = is_member or f"# {scrub(path)}\n" in section
            (folder if kept else folder_cut).append(_redact_meta(_display_path(path)))
    loader = [
        (_redact_meta(_display_path(path)), resolved, body is not None)
        for path, resolved, body in (
            steering_resource_documents(
                project,
                harness_reads_agents_md=ctx.acp_driver.reads_project_agents_md(provider_type),
            )
            if ctx.acp_driver.crew_loads_spec_steering(provider_type)
            else ()
        )
    ]
    return chat_steering_plan(
        listed=listed,
        folder_documents=folder,
        folder_cut=folder_cut,
        loader_documents=loader,
        provider_type=provider_type,
        native_steering=native_steering,
        project=project,
        agent=agent,
        is_member=is_member,
    )


async def api_chat_slot_steering(request: web.Request) -> web.Response:
    """List the steering files this chat receives, where each comes from, and why."""
    from kiro_crew.crewmate_harness import CrewmateHarnessUnavailable
    from kiro_crew.dashboard.chat_handlers import _configured_backend_for_slot
    from kiro_crew.dashboard.chat_utils_slots import effective_session_key
    from kiro_crew.dashboard.handlers._shared import _slot_project, session_folder_steering_dirs
    from kiro_crew.dashboard.handlers.steering import _offload
    from kiro_crew.dashboard.slot_ownership import deny_app_slot_access, slot_not_found

    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if slot is None:
        return slot_not_found()
    denied = deny_app_slot_access(request.get("app", ""), slot, name, "slot_steering")
    if denied is not None:
        return denied
    session_key = effective_session_key(slot)
    provider = state.sessions.get_provider(session_key)
    if provider is not None and not hasattr(provider, "native_steering"):
        provider = None
    backend = ""
    if provider is None:
        try:
            backend = await _configured_backend_for_slot(slot)
        except CrewmateHarnessUnavailable as exc:
            return web.json_response({"error": str(exc), "code": exc.code}, status=409)
    provider_type, native_steering = _harness_of(provider, backend)
    # This chat's own project, read from the slot itself: the turn builds its
    # context from ``slot.project`` (``turn_admission._current_binding``), and a
    # linked session key (``slack:<ts>``) names no slot to look it up by.
    project_dir = _slot_project(slot)
    steering_dirs = await session_folder_steering_dirs(state, session_key)
    rows = await _offload(
        _plan_for_provider,
        provider,
        slot,
        session_key,
        str(project_dir) if project_dir else None,
        steering_dirs,
        provider_type,
        native_steering,
    )
    return web.json_response({"provider": provider_type, "files": rows})


def _plan_for_provider(
    provider: Any,
    slot: Any,
    session_key: str,
    project: str | None,
    steering_dirs: tuple[str, ...],
    provider_type: str,
    native_steering: bool,
) -> list[dict[str, str]]:
    """:func:`_plan_blocking` with what the live provider adds, read off the loop.

    The launch record's documents (``native_context_documents`` stats the files it
    names), and the provider's working directory for a chat that names no project,
    which the turn takes as its project (``build_message``).
    """
    from kiro_crew.agent_sdk import context_provider_of

    context_provider = context_provider_of(provider) if provider is not None else None
    launched: tuple[str, ...] = ()
    context_project = project
    if context_provider is not None:
        launched = tuple(context_provider.native_context_documents)
        if project is None and not session_key.startswith("subagent:"):
            context_project = context_provider.cwd or None
    return _plan_blocking(
        slot,
        session_key,
        context_project,
        steering_dirs,
        provider_type,
        native_steering,
        launched,
        binding_project=project,
    )
