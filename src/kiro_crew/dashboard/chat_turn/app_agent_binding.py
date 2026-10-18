"""Which agent an app-owned dashboard slot runs as: whether the app's binding applies, whether
the turn's backend may run the app's own agent, its start directories, and retiring and
recovering the app agent's session."""

from __future__ import annotations

import asyncio
import functools
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        DashboardState,
        KiroCrewConfig,
        ResolvedBindings,
        Routing,
        SessionCapabilities,
        _ChatSlot,
        _MemoryUnavailable,
        effective_session_key,
        logger,
        resolve_agent_bindings,
        resolve_session_agent_bindings,
        routing_for,
        select_provider_backend,
        subprocess_executor,
    )


def app_agent_binding_applies(slot: "_ChatSlot") -> bool:
    """Whether *slot*'s app-agent binding still names the agent it runs as.

    The binding is bound to the agent name (``<app>/<agent>``): a session
    switched to another agent keeps the field, and both the per-turn check in
    ``_run_chat`` and the eager-spawn opt-out apply only while the session runs
    as the bound agent. One predicate for both, so they cannot drift.
    """
    owner_app, _, owner_agent = (slot.app_agent_owner or "").partition("/")
    return bool(owner_app and owner_agent and slot.agent == owner_agent)


def _app_agent_start_dirs(
    state: "DashboardState", session_key: str, project: str | None
) -> tuple[str | None, ...]:
    """Every directory the provider for *session_key* can start in, project first.

    With a project, the provider starts there. Without one, session allocation
    falls back to the cwd the session map stored for a resumed session, the warm
    pool's cwd, or the factory's per-session work directory -- so an app-owned
    turn on a session whose project was cleared is checked against each of them,
    not against no project at all.
    """
    if project:
        return (project,)
    from kiro_crew.config.loader import _session_work_dir

    sessions = getattr(state, "sessions", None)
    session_map = getattr(sessions, "_session_map", None)
    stored = session_map.get_cwd(session_key) if session_map is not None else ""
    pool_cwd = getattr(sessions, "_pool_cwd", "") if sessions is not None else ""
    return (
        None,
        stored if isinstance(stored, str) else None,
        pool_cwd if isinstance(pool_cwd, str) else None,
        str(_session_work_dir(session_key)),
    )


async def _retire_app_agent_session(
    state: "DashboardState", slot: "_ChatSlot", session_key: str
) -> None:
    """Cancel *slot*'s pending eager start and remove any session it registered.

    Called when an app-owned turn is refused: whatever is registered under
    *session_key* was resolved without the ownership check and must not serve a
    later turn.
    """
    eager = getattr(slot, "_eager_spawn_task", None)
    if eager is not None and not eager.done():
        eager.cancel()
        await asyncio.gather(eager, return_exceptions=True)
    sessions = getattr(state, "sessions", None)
    if sessions is not None and sessions.has_session(session_key):
        await sessions.remove(session_key)


async def _recover_app_agent_binding(
    cfg: "KiroCrewConfig", slot: "_ChatSlot", *, project: str | None
) -> ResolvedBindings:
    """Re-register an app-owned slot's resources from source, then re-resolve.

    The last recovery rung for an app slot whose agent stayed unresolved after
    the snapshot rescan: the spec was never materialized even though the source
    is intact (a plain gateway restart re-materializes every ENABLED app via
    ``reconcile_enabled_app_resources``, so this is what avoids that restart and
    heals mid-turn). Uses ``register_app`` — not the narrower
    ``refresh_app_agents`` — so the app's MCP servers are registered BEFORE its
    agents; re-materializing only the agent would inline an empty server map and
    recreate an agent whose own tool refs dangle (dispatches, tools never mount).
    Gated on ``is_app_enabled`` held under ``app_lifecycle_lock`` so a concurrent
    disable/uninstall cannot race recovery into reactivating a deregistered
    agent; a disabled app is left to fail loud. A recovery failure only logs —
    the re-resolve below then simply returns the still-cold bindings. Returns the
    freshly resolved bindings; the caller reassigns its own locals from them.
    """
    # Local imports mirror server.py's reconcile import to avoid a top-level
    # apps<->dashboard cycle.
    from kiro_crew.apps.bridges import register_app
    from kiro_crew.apps.manager import app_lifecycle_lock, is_app_enabled

    try:
        loop = asyncio.get_running_loop()
        async with app_lifecycle_lock(slot._app):
            if await loop.run_in_executor(subprocess_executor(), is_app_enabled, slot._app):
                # register_app runs in an executor thread that cannot be
                # cancelled. Shield the await so cancelling THIS coroutine (e.g.
                # an eager-spawn task being cancelled) does NOT release the
                # lifecycle lock while that thread is still writing — a concurrent
                # disable could otherwise acquire the lock, deregister the app,
                # and have the still-running thread republish a now-disabled
                # agent. On cancel, wait for the thread to finish before letting
                # the lock release, then propagate the cancellation.
                fut = loop.run_in_executor(subprocess_executor(), register_app, slot._app)
                try:
                    await asyncio.shield(fut)
                except asyncio.CancelledError:
                    await fut
                    raise
    except Exception:  # noqa: BLE001 — a recovery failure only costs the fail-loud
        logger.warning(
            "Failed to re-register app resources from source for app slot %s",
            slot.key,
            exc_info=True,
        )
    selected_agent = slot.agent
    bindings = await asyncio.to_thread(
        functools.partial(resolve_session_agent_bindings, agent_kind=slot.agent_kind),
        resolve_agent_bindings,
        cfg,
        effective_session_key(slot),
        selected_agent or None,
        project,
    )
    if slot.agent != selected_agent:
        raise _MemoryUnavailable(
            "memory_unavailable: agent changed during recovery; retry the turn"
        )
    return bindings


def _provider_backend(provider: Any) -> str | None:
    """The backend id *provider*'s own capabilities name, or None when it names none.

    A provider that carries no real :class:`SessionCapabilities` -- a wrapper, a
    test double, a future adapter -- names no backend, and every caller treats
    that as refused rather than as the default id.
    """
    caps = getattr(provider, "capabilities", None)
    if not isinstance(caps, SessionCapabilities):
        return None
    return caps.backend


def _app_agent_host_allowed(provider: Any) -> bool:
    """Whether *provider*'s backend launches its agent from the agent spec.

    An app's own agent runs only where the spec is what the harness mounts
    (``Routing.AGENT_SPEC``); any other backend would run the app-authored
    prompt without the app's spec. A provider naming no backend is refused.
    """
    backend = _provider_backend(provider)
    return backend is not None and routing_for(backend) is Routing.AGENT_SPEC


def _launched_copy_names(provider: Any) -> set[str]:
    """Every launch-copy name the layers of *provider* say kiro-cli was asked for.

    The allocation stamps it on the provider (``app_launch_agent``) and the Kiro
    runtime keeps it (``_launch_agent``); the agent's own name stays on ``_agent``.
    """
    from kiro_crew.dashboard.handlers.usage import _wrapper_chain

    names: set[str] = set()
    for node in _wrapper_chain(provider):
        for attr in ("app_launch_agent", "_launch_agent"):
            name = getattr(node, attr, None)
            if isinstance(name, str) and name:
                names.add(name)
    return names


def _runs_app_agent_launch(provider: Any, launch_name: str) -> bool:
    """Whether kiro-cli behind *provider* was asked for *launch_name* and nothing else.

    Fail closed: a provider that names no launch copy cannot show it runs the
    copy, so it does not.
    """
    return bool(launch_name) and _launched_copy_names(provider) == {launch_name}


async def _app_agent_turn_backend(
    state: "DashboardState", session_key: str, cfg: "KiroCrewConfig | None"
) -> str | None:
    """The backend the turn about to be claimed for *session_key* would run on.

    A live provider registered under the key is the one the claim reuses, so its
    own identity answers (None when it names none). Otherwise the claim allocates
    through the same member-aware selector this reads. The claimed provider is
    checked again after the claim; this read only spares a refused turn its cold
    start and decides whether the turn launches the app's copy.
    """
    sessions = state.sessions
    provider = sessions.get_provider(session_key)
    if provider is not None and await sessions.is_provider_alive(session_key) is True:
        return _provider_backend(provider)
    if cfg is None:
        cfg = await asyncio.to_thread(KiroCrewConfig.load)
    return select_provider_backend(session_key, cfg.agent.member_acp_backend, cfg.agent.acp_backend)
