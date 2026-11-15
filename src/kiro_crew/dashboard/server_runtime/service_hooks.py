"""Hooks for the gateway-scoped services an entrypoint wires before ``runner.setup()``.

The pull-request status-delta sink, the shutdown of the Kiro prerequisite and KAS
login services, and the boot-time close of retired remote worker slots.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.server import (
        DashboardState,
        logger,
        register_status_delta_sink,
        unregister_status_delta_sink,
    )


def _wire_status_delta_sink(app: web.Application, state: DashboardState) -> None:
    """Register the PR status-delta sink and its shutdown cleanup on ``app``.

    Registered once at wiring time (rather than per WS connect) so the sink set
    holds exactly one entry per process; ``push_source_status`` no-ops while no
    owner socket is open. The matching ``on_cleanup`` hook is REQUIRED: the sink
    set is module-global and outlives any single ``DashboardState``, so without
    it, starting/stopping/restarting a dashboard in one process retains every old
    state's bound method — a slow leak plus duplicate dispatch to dead states on
    every later status change.
    """
    register_status_delta_sink(state.push_source_status)

    async def _status_sink_shutdown(_app: web.Application) -> None:
        unregister_status_delta_sink(state.push_source_status)

    app.on_cleanup.append(_status_sink_shutdown)


def _register_kiro_service_shutdown(app: web.Application) -> None:
    """Close the Kiro prerequisite service, and the KAS login service if a request built one.

    Shared by both entrypoints, and registered before ``runner.setup()`` freezes the
    signal lists.
    """

    async def _kiro_prerequisite_shutdown(app_: web.Application) -> None:
        try:
            await app_["kiro_prerequisite_service"].close()
        except Exception:  # noqa: BLE001 — one cleanup hook must not suppress later hooks
            logger.warning("Kiro prerequisite service close failed", exc_info=True)

    app.on_cleanup.append(_kiro_prerequisite_shutdown)

    async def _kas_login_shutdown(app_: web.Application) -> None:
        # Releases the service's aiohttp session IF a KAS request created it. It is
        # lazily built on first use (never at boot), so an app that never served a
        # KAS request has nothing to close.
        service = app_.get("kas_login_service")
        if service is not None:
            try:
                await service.close()
            except Exception:  # noqa: BLE001 — one cleanup hook must not suppress later hooks
                logger.warning("KAS login service close failed", exc_info=True)

    app.on_cleanup.append(_kas_login_shutdown)


async def _sweep_remote_closes(state: DashboardState) -> None:
    """Boot: close retired remote worker slots once, when remote dispatch is on.

    Called by the startup tunnel revive once it has finished
    (``instances_lifecycle._retake_hops_then_revive``), so the crews it brought
    back already read connected; the sweep reads the switch itself. Patrol ticks
    repeat it; nothing here runs on a timer. Never raises.
    """
    from kiro_crew.dashboard.handlers import remote_puller

    await remote_puller.sweep_closes(state)
