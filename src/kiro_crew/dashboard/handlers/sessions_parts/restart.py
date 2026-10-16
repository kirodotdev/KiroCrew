"""Restarting every session so it picks up a configuration change.

:func:`_reset_all_sessions` reloads the provider factory, drains every active session and the
warm pool, and shuts each provider down off the request with a bounded wait
(:data:`_SHUTDOWN_TIMEOUT_SECS`) and a force-kill fallback; ``POST /api/sessions/restart``
syncs discovered MCP servers first and then runs it. The computer-use and MCP-sync routes call
it too, through the facade. New restart steps go here.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import TYPE_CHECKING

from aiohttp import web

# circular import: handlers/__init__.py imports this family through the facade while
# the package is still initialising, so a `from ... import` of individual names would
# fail mid-cycle. `import ... as` binds via sys.modules and defers attribute access to
# call time, which also keeps tests' patches of ``handlers.<name>`` effective.
import kiro_crew.dashboard.handlers as _h
from kiro_crew.dashboard.handlers._shared import guard_owner_surface_routes
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.executors import subprocess_executor
from kiro_crew.mcp_discovery import sync_discovered_servers
from kiro_crew.runtime_ownership import release_session_lease

if TYPE_CHECKING:
    # The facade keeps this import: no owner may import the provider layer (the
    # agent-sdk boundary), so an annotation reads the name from there.
    from kiro_crew.dashboard.handlers.sessions import LLMProvider

# The family logs under the facade's name, which log filters and caplog assertions key on.
logger = logging.getLogger("kiro_crew.dashboard.handlers.sessions")


_SHUTDOWN_TIMEOUT_SECS = 10


async def _reset_all_sessions(request: web.Request) -> int:
    """Reset all active sessions so they pick up config changes.

    Reloads provider factory (handles provider switch ACP→CC or vice versa),
    shuts down all active sessions AND drains the warm pool (pre-spawned
    processes loaded the old MCP config at spawn time).
    New sessions cold-start on next message.
    Returns the number of sessions reset.
    """
    state: DashboardState = request.app["state"]
    sessions = state.sessions

    # Reload factory so provider switch takes effect immediately
    await sessions.reload_provider_factory()

    # Pop all active sessions
    providers: list[LLMProvider] = []
    count = sessions.count
    if count > 0:
        providers = await sessions.drain_all_providers()

    # Drain warm pool — pre-spawned processes have stale MCP config
    pool_providers = await sessions.drain_warm_pool()
    providers.extend(pool_providers)

    if count > 0 or pool_providers:
        logger.info(
            "Reset %d session(s) + %d pool process(es) after config change",
            count,
            len(pool_providers),
        )

    state.broadcast_ws("sessions_restarting", {"status": "restarting"})

    async def _background_restart() -> None:
        if providers:

            async def _safe_shutdown(p: LLMProvider) -> None:
                _timeout = _SHUTDOWN_TIMEOUT_SECS
                try:
                    await asyncio.wait_for(p.shutdown(), timeout=_timeout)
                except asyncio.TimeoutError:
                    logger.warning(
                        "Session shutdown hung past %.1fs; forcing kill",
                        _timeout,
                    )
                    # The timeout cancelled ``shutdown`` mid-flight, so it may not
                    # have reached its own release. Release here before the kill --
                    # idempotent, so a shutdown that did get that far costs
                    # nothing, and without it the gate refuses the very kill this
                    # arm exists to perform and the hung tree leaks.
                    await release_session_lease(p)
                    try:
                        # The kill signals the provider's whole process group and
                        # then waits out a bounded SIGTERM grace, so it blocks for
                        # as long as that grace -- never inline on the event loop
                        # (AUTOSDE: no-blocking-call-on-event-loop), which is what
                        # every other caller of it already avoids. Awaited so the
                        # tree is reaped before ``start_pool`` below spawns its
                        # replacements, and concurrent across the ``gather``, so N
                        # hung providers cost one grace rather than N in series.
                        await asyncio.get_running_loop().run_in_executor(
                            subprocess_executor(), _h._sync_kill_provider, p
                        )
                    except RuntimeError:
                        # The executor is already shut down -- a gateway teardown
                        # racing this restart. Run the kill on a plain daemon
                        # thread instead: still off the loop, and far better than
                        # skipping it, which is what leaks the tree.
                        threading.Thread(
                            target=_h._sync_kill_provider, args=(p,), daemon=True
                        ).start()
                    except Exception:
                        logger.exception("Force-kill fallback also failed for %r", p)
                except Exception:
                    pass

            await asyncio.gather(*[_safe_shutdown(p) for p in providers])

        sessions._pool_started = False
        await sessions.start_pool(blocking=False)
        logger.info("Background session restarted")
        state.push_refresh("agents")
        state.push_slots_update()
        state.broadcast_ws("sessions_restarting", {"status": "ready"})

    task = asyncio.create_task(_background_restart())
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)

    return count


async def api_sessions_restart(request: web.Request) -> web.Response:
    """POST /api/sessions/restart — reset all kiro-cli sessions.

    Forces fresh context injection on the next message. Use after editing
    memory, lessons, or skills to pick up changes immediately.

    Also syncs MCP servers from mcp.json → kirocrew.json so newly
    installed servers (e.g. via AIM) are picked up on restart.
    """
    # Sync MCP servers before restarting so new installs take effect.
    # Run in thread — the sync does blocking file I/O. Cap at 30s so a hung
    # rebuild doesn't stall the restart. sync_discovered_servers serializes
    # against the /api/mcp/sync handler's run of the same sequence.
    synced = 0
    sync_ok = True
    try:
        to_sync = await asyncio.wait_for(asyncio.to_thread(sync_discovered_servers), timeout=30)
        synced = len(to_sync)
    except Exception:
        # The restart still proceeds (it applies whatever IS on disk), but the
        # response says the reconcile failed rather than reporting a success
        # the on-disk config does not back.
        sync_ok = False
        logger.warning("MCP server sync failed before restart", exc_info=True)
    count = await _reset_all_sessions(request)
    return web.json_response(
        {"ok": True, "sessions_reset": count, "mcp_synced": synced, "mcp_sync_ok": sync_ok}
    )


# Every ``api_session*`` handler is an owner surface, so a private member's
# internal call is refused before it runs (audit label = handler name).
guard_owner_surface_routes(
    globals(),
    prefix="api_session",
    member_scoped=frozenset(),
)
