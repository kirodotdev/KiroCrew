"""``/api/chat/slots/{slot}/steering``: the steering files one chat receives.

``GET`` lists them; ``PUT`` switches one off or back on for this chat (owner only,
through :mod:`kiro_crew.steering_off`). The rows come from :func:`kiro_crew.steering_plan.chat_steering_plan`,
fed by the Steering tab's own scan (``list_steering_blocking``) for the chat's
project and by the folder collection for the chat's folder roots, so the list
names exactly the files those readers admit. Paths are display paths through the
Steering tab's redaction; no body leaves this route.
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)


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
    from kiro_crew.context_assembly.inclusion import (
        _folder_steering_delivery_snapshot,
    )
    from kiro_crew.dashboard.handlers.steering import (
        _display_path,
        _redact_meta,
        list_steering_blocking,
    )
    from kiro_crew.steering_off import steering_off_for
    from kiro_crew.steering_plan import chat_steering_plan

    # The member identity the context builder receives (``prompt_assembly``'s
    # ``member=``): a member chat's envelope decides its steering.
    is_member = slot.mode == "member" and bool(slot.agent)
    if binding_project is _SAME_PROJECT:
        binding_project = project
    agent = None if is_member else _resolved_agent(slot, session_key, binding_project)
    listed = list_steering_blocking(Path(project) if project else None)["files"]
    steering_off = steering_off_for(session_key)
    folder: list[str] = []
    folder_cut: list[str] = []
    folder_resolved: dict[str, str] = {}
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
                collection,
                max_chars=ctx._resolve_caps(None).steering,
                scrub=scrub,
            )
        )
        for path, _body in collection.documents:
            display = _redact_meta(_display_path(path))
            folder_resolved[display] = _resolved_str(path)
            kept = is_member or f"# {scrub(path)}\n" in section
            (folder if kept else folder_cut).append(display)
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
        folder_resolved=folder_resolved,
        steering_off=steering_off,
        provider_type=provider_type,
        native_steering=native_steering,
        project=project,
        agent=agent,
        is_member=is_member,
    )


def _resolved_str(path: str) -> str:
    """*path* resolved, the spelling :mod:`kiro_crew.steering_off` stores."""
    from pathlib import Path

    try:
        return str(Path(path).resolve())
    except (OSError, RuntimeError):
        return path


def _public(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """*rows* without the resolved paths the switch route matches on."""
    return [{k: v for k, v in row.items() if k != "resolved"} for row in rows]


async def api_chat_slot_steering(request: web.Request) -> web.Response:
    """List the steering files this chat receives, where each comes from, and why."""
    found = await _chat_rows(request, "slot_steering")
    if isinstance(found, web.Response):
        return found
    provider_type, _session_key, rows = found
    return web.json_response({"provider": provider_type, "files": _public(rows)})


async def api_chat_slot_steering_switch(request: web.Request) -> web.Response:
    """Switch one steering file off (or back on) for this chat.

    Body ``{"path": <a row's display path>, "off": bool}``. Only a row whose text Crew
    writes can switch (``switch`` is ``on`` or ``off``); any other answers 409
    ``not_switchable``. Stores the row's resolved path, never a body, and answers
    the list as it now reads.
    """
    import asyncio

    from kiro_crew.dashboard.chat_utils_slots import effective_session_key
    from kiro_crew.dashboard.handlers._shared import (
        read_bounded_json,
        require_owner_dashboard_request,
    )
    from kiro_crew.dashboard.slot_ownership import slot_not_found
    from kiro_crew.steering_off import SteeringOffStoreUnreadable, set_steering_off
    from kiro_crew.steering_plan import SWITCH_OFF, SWITCH_ON

    # Owner only: an agent's own internal-API credential must not reach the record
    # the sandbox seals against it.
    denied = await require_owner_dashboard_request(request, "slot_steering_switch")
    if denied is not None:
        return denied
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    path, off = body.get("path"), body.get("off")
    if not isinstance(path, str) or not path or not isinstance(off, bool):
        return web.json_response(
            {"error": "path must be a non-empty string and off a boolean", "code": "invalid_body"},
            status=400,
        )
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    authorized = state._slots.get(name)
    found = await _chat_rows(request, "slot_steering_switch", session_gate=True)
    if isinstance(found, web.Response):
        return found
    provider_type, session_key, rows = found
    row = next(
        (r for r in rows if r["path"] == path and r["switch"] in (SWITCH_ON, SWITCH_OFF)), None
    )
    if row is None:
        return web.json_response(
            {"error": "this file cannot be switched off for this chat", "code": "not_switchable"},
            status=409,
        )
    # The slot the gate authorized, still the one under this name: a close and
    # re-create while the rows were built would otherwise take this write.
    if authorized is None or state._slots.get(name) is not authorized:
        return slot_not_found()
    # Still bound to the session the rows were built for: a channel rebind while
    # they were built would store the switch under a key no turn reads.
    if effective_session_key(authorized) != session_key:
        return web.json_response(
            {
                "error": "this chat moved to another session while saving; try again",
                "code": "session_changed",
            },
            status=409,
        )
    try:
        await asyncio.to_thread(set_steering_off, session_key, row["resolved"], off)
    except SteeringOffStoreUnreadable as exc:
        return web.json_response(
            {
                "error": f"the switch-off record cannot be read, so it was not changed: {exc}",
                "code": "store_unreadable",
            },
            status=409,
        )
    except OSError:
        # A full disk, a refused link or a permission error. The outcome is unknown:
        # the writer (``atomic_write.rewrite_in_place``) puts the old record back on
        # an error, but that put-back can fail too. So the reply claims neither, and
        # the client reads again.
        logger.error("could not write the steering switch-off record", exc_info=True)
        return web.json_response(
            {
                "error": "the switch-off record may not have been saved; read the list again",
                "code": "write_failed",
            },
            status=500,
        )
    found = await _chat_rows(request, "slot_steering_switch")
    if isinstance(found, web.Response):
        return found
    provider_type, _session_key, rows = found
    return web.json_response({"provider": provider_type, "files": _public(rows)})


async def _chat_rows(
    request: web.Request, operation: str, *, session_gate: bool = False
) -> web.Response | tuple[str, str, list[dict[str, str]]]:
    """``(provider_type, session_key, rows)`` for the request's chat, or a refusal.

    *session_gate* is the write's gate: an app must own the session the slot is
    bound to as well as the slot (``deny_app_slot_session_access``), as the other
    per-slot writes require.
    """
    from kiro_crew.crewmate_harness import CrewmateHarnessUnavailable
    from kiro_crew.dashboard.chat_handlers import _configured_backend_for_slot
    from kiro_crew.dashboard.chat_utils_slots import effective_session_key
    from kiro_crew.dashboard.handlers._shared import _slot_project, session_folder_steering_dirs
    from kiro_crew.dashboard.handlers.steering import _offload
    from kiro_crew.dashboard.slot_ownership import (
        deny_app_slot_access,
        deny_app_slot_session_access,
        slot_not_found,
    )

    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if slot is None:
        return slot_not_found()
    gate = deny_app_slot_session_access if session_gate else deny_app_slot_access
    denied = gate(request.get("app", ""), slot, name, operation)
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
    return provider_type, session_key, rows


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
