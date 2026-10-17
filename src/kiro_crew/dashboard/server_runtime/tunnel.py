"""The tunnel's gateway lifecycle.

The teardown hook registered first among the cleanup hooks, and the tunnel's start
when the config or the active ``TunnelProvider`` enables it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.server import (
        _TUNNEL_STOP_TIMEOUT_SECS,
        DashboardState,
        KiroCrewConfig,
        current_context,
        logger,
        sel,
        setup_tunnel,
    )


def _wire_tunnel_shutdown(app: web.Application, state: DashboardState) -> None:
    """Register the tunnel teardown hook on ``app``'s shutdown path.

    Without this the tunnel is started (``tunnel/setup.py`` → ``TunnelManager.start()``)
    and then NEVER stopped: ``TunnelManager.stop()`` had no production caller, so
    whatever the active ``TunnelProvider`` brought up outlived the gateway — even
    on a clean Ctrl+C. A companion provider that supervises a child process
    leaked it (reparented to PID 1) and the next gateway start collided on the
    same tunnel name. The manager is edition-neutral, so stopping it here tears
    down EVERY provider (the public Default's ``stop()`` is a no-op).

    Registered like the other long-lived subsystems (``_watchdog_shutdown``,
    ``_register_instances_hooks``): the hook is appended BEFORE ``runner.setup()``
    freezes the app's signal lists, and reads ``state.tunnel_manager`` lazily —
    the manager is only assigned later, after ``setup_tunnel`` runs, and this
    hook fires at shutdown, long after that assignment. That lazy read is also
    what lets the REGISTRATION sit first in ``start_dashboard``: ``on_cleanup``
    handlers are dispatched in registration order under a hard shutdown
    deadline, so a tunnel hook queued behind the other subsystems can be starved
    (instances cleanup waiting on SSH children that ignore SIGTERM eats the
    deadline, the gateway force-exits, and the tunnel is never stopped).

    Two teardown paths, because a live tunnel does not imply a manager:
    ``setup_tunnel`` builds a ``TunnelManager`` and the hook stops that, but the
    on-demand link path (``slack.use_tunnel_url`` →
    ``current_context().tunnel.ensure_available()`` in ``slack/allowlist.py``)
    provisions and starts a tunnel straight on the provider and never constructs
    a manager. With ``state.tunnel_manager`` still None, bailing out left exactly
    the orphan this hook exists to prevent, so the no-manager path stops
    ``current_context().tunnel`` directly. Only one path runs per shutdown — the
    manager delegates to the same provider — so nothing is stopped twice.

    Failure containment: ``on_cleanup`` handlers run in sequence and a raise
    aborts the remaining ones, so a tunnel teardown must never propagate. BOTH
    paths go through ``_stop_bounded``: the stop is bounded by
    ``_TUNNEL_STOP_TIMEOUT_SECS`` and every exception is logged and swallowed, so
    neither a hanging nor a raising provider — nor a fail-closed
    ``current_context()`` — can block or crash the rest of gateway shutdown.
    ``TunnelManager.stop()`` is itself idempotent (it re-delegates and, on
    failure, simply declines to pin STOPPED) and a provider ``stop()`` is
    expected to be too, so a shutdown path that runs twice is harmless on either
    path.
    """

    async def _stop_bounded(stop: Callable[[], Awaitable[None]], what: str) -> None:
        """Await *stop* under the shared bound, logging and swallowing everything.

        *stop* is INVOKED inside the guard, so a synchronous raise — including a
        fail-closed ``current_context()`` lookup — is contained as well.
        """
        try:
            await asyncio.wait_for(stop(), timeout=_TUNNEL_STOP_TIMEOUT_SECS)
        except asyncio.TimeoutError:
            logger.warning(
                "%s did not finish within %.0fs — continuing shutdown",
                what,
                _TUNNEL_STOP_TIMEOUT_SECS,
            )
        except Exception:
            logger.warning("%s failed during shutdown", what, exc_info=True)

    async def _tunnel_shutdown(_app: web.Application) -> None:
        mgr = getattr(state, "tunnel_manager", None)
        if mgr is not None:
            await _stop_bounded(mgr.stop, "Tunnel stop")
            return
        # No manager, but the provider may still own a running tunnel (the
        # on-demand ``ensure_available()`` path never builds one).
        await _stop_bounded(lambda: current_context().tunnel.stop(), "Tunnel provider stop")

    app.on_cleanup.append(_tunnel_shutdown)


async def _start_aea_tunnel(
    app: web.Application, state: DashboardState, cfg: KiroCrewConfig, port: int
) -> None:
    """Start the tunnel when the config or the active ``TunnelProvider`` enables it."""
    _tunnel_enabled = cfg.tunnel.enabled
    # The enable gate is also routed through the active PlatformContext's
    # TunnelProvider.  The Default TunnelProvider.enabled() returns False, so
    # standalone is gated solely by ``cfg.tunnel.enabled`` exactly as before;
    # the companion can additionally enable the tunnel from its provider.
    try:
        _ctx_tunnel_enabled = current_context().tunnel.enabled()
    except Exception:
        logger.debug("tunnel.enabled() lookup failed; using cfg only", exc_info=True)
        _ctx_tunnel_enabled = False
    _tunnel_enabled = _tunnel_enabled or _ctx_tunnel_enabled
    logger.debug("Tunnel config: enabled=%s ctx.enabled=%s", _tunnel_enabled, _ctx_tunnel_enabled)
    if _tunnel_enabled:
        tunnel_mgr = await setup_tunnel(
            middlewares=list(app.middlewares),
            allowed_origins=app["allowed_origins"],
            tunnel_name_mode=cfg.tunnel.name_mode,
            tunnel_name_override=cfg.tunnel.name_override,
            port=port,
            log_api_access=sel().log_api_access,
        )
        if tunnel_mgr:
            state.tunnel_manager = tunnel_mgr
