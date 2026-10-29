"""Shared Crew gateway wiring: the team-config gate and the startup/cleanup hooks.

:func:`team_config_middleware` runs inner to token auth, after the request's
actor is published. While the tailnet login allowlist could not be read
(``access.tailnet_identity_config_unreadable``), a person reaching the Crew
through a tailnet path is refused ``team_config_invalid``: an unusable team
setup never falls back to admitting them as if the Crew were personal. The
local operator, Crew automation and apps are not refused.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from aiohttp import web

from kiro_crew.shared_crew.access import tailnet_identity_config_unreadable
from kiro_crew.shared_crew.contracts import (
    ACTOR_REQUEST_KEY,
    ActorContext,
    PrincipalKind,
    Provenance,
)
from kiro_crew.shared_crew.crew_mode import (
    ERROR_TEAM_CONFIG_INVALID,
    REASON_NETWORK_IDENTITY_CONFIG_UNREADABLE,
)

logger = logging.getLogger(__name__)

#: Provenance that marks a person reaching the Crew through a tailnet path.
SHARED_PROVENANCE = frozenset(
    {Provenance.TAILSCALE_SERVE, Provenance.TAILSCALE_DIRECT, Provenance.TAILSCALE_LISTENER}
)

SHUTDOWN_CLEANUP_TIMEOUT_SECONDS = 10


@web.middleware
async def team_config_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    actor = request.get(ACTOR_REQUEST_KEY)
    principal = actor.principal if isinstance(actor, ActorContext) else None
    if (
        principal is not None
        and principal.kind is PrincipalKind.HUMAN
        and principal.provenance in SHARED_PROVENANCE
        and tailnet_identity_config_unreadable()
    ):
        return web.json_response(
            {
                "error": ERROR_TEAM_CONFIG_INVALID,
                "code": ERROR_TEAM_CONFIG_INVALID,
                "reason": REASON_NETWORK_IDENTITY_CONFIG_UNREADABLE,
            },
            status=403,
        )
    return await handler(request)


def install(app: web.Application) -> None:
    """Append the team-config gate, inner to token auth and the slot checkpoint."""
    app.middlewares.append(team_config_middleware)


async def hold_until_startup_check() -> None:
    """Before the bind: with hard limits asked for, refuse runtime starts until checked.

    Reads config only. The admission service, its durable store and the
    containment probe are built by ``admission_gate.startup_check`` after the
    bind (or by the first use, as without this hook); until it publishes its
    report, hard-limit mode refuses as an unproven probe.
    """
    from kiro_crew.shared_crew import admission_gate
    from kiro_crew.shared_crew import containment as _containment

    if await asyncio.to_thread(admission_gate._configured_hard_limits):
        _containment.publish_report(
            _containment.failed_report(hard_limits=True, detail="startup containment check pending")
        )


async def check_after_bind(startup_check: Callable[[], Any]) -> None:
    """Run the containment check after the bind; clear the hold when it refuses nothing.

    ``startup_check`` answers None when neither admission nor hard limits are
    on, and publishes no report then, so the pre-bind hold is cleared here.
    """
    from kiro_crew.shared_crew import containment as _containment

    if await startup_check() is None:
        _containment.publish_report(None)


def install_lifecycle(app: web.Application) -> Callable[[], None]:
    """Wire the hooks before traffic; return the post-bind maintenance starter.

    Building the admission service (its durable store's integrity check), the
    containment probe, reconciliation and orphan cleanup all run after the bind,
    so none of them holds up aiohttp's pre-bind startup hooks. Before the bind,
    hard-limit mode only places a hold, read from config, that refuses runtime
    starts until the probe has reported. Admission and capacity reads still await
    ``ensure_ready`` themselves, sharing its reconciliation lock with maintenance.
    """
    maintenance: asyncio.Task[None] | None = None
    probes: asyncio.Task[None] | None = None

    async def _shared_crew_startup(app_: web.Application) -> None:
        # Defer admission imports until startup; registering hooks performs no I/O.
        from kiro_crew.shared_crew import wiring

        wiring.install(app_["state"])
        await hold_until_startup_check()

    def start_maintenance() -> None:
        nonlocal maintenance, probes
        from kiro_crew.shared_crew import host_power
        from kiro_crew.shared_crew.admission_gate import installed_service, startup_check

        if probes is None:
            # Informational host probes, after the bind and off the boot path.
            probes = asyncio.create_task(host_power.probe_after_start())
        if maintenance is not None:
            return

        async def maintain() -> None:
            await check_after_bind(startup_check)
            service = installed_service()
            if service is None or not service.enabled:
                return
            try:
                await service.ensure_ready()
                released = await service.release_stale_slices()
                if released:
                    logger.info("shared crew: released %d idle principal slice(s)", released)
            except Exception:  # noqa: BLE001 -- admission keeps its own readiness fence
                logger.warning("shared crew: startup maintenance failed", exc_info=True)

        maintenance = asyncio.create_task(maintain())

    async def _shared_crew_cleanup(_app: web.Application) -> None:
        # Cleanup is optional until startup has built an admission service.
        from kiro_crew.shared_crew.admission_gate import shutdown_cleanup

        for task in (maintenance, probes):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        try:
            await asyncio.wait_for(shutdown_cleanup(), timeout=SHUTDOWN_CLEANUP_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 -- cleanup never blocks the stop
            logger.debug("shared crew: slice cleanup at stop failed", exc_info=True)

    app.on_startup.append(_shared_crew_startup)
    app.on_cleanup.append(_shared_crew_cleanup)
    return start_maintenance


__all__ = [
    "check_after_bind",
    "hold_until_startup_check",
    "install",
    "install_lifecycle",
    "team_config_middleware",
]
