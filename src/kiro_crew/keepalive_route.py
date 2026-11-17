"""Which session providers ``/api/session-keepalive`` can reach.

The route looks ``X-Session-Key`` up in the one session registry the gateway's
``DashboardState`` holds (``live_turn.api_session_keepalive``) and hands a Kiro Crew
tool's heartbeat to the provider it finds there. That heartbeat is how a call on Kiro
Crew's own servers proves it is alive, so a session handle times such a call only once
the registry the route reads has published its provider. ``DashboardState`` names that
registry (``serve_keepalive_route``), and the registry marks each provider as it
publishes it (``mark_published``, called where every publication and removal passes,
``SessionAllocationService.advance_ownership_generation``).

Every other provider stays unmarked, whatever built it: ``kirocrew chat`` and
``kirocrew run`` hold theirs in their own process, ``kirocrew eval`` builds a registry
of its own, a sub-agent run on a shared handle wraps that handle outside the registry,
and an app's own runner builds its provider from the factory. Their handles leave
those calls to the liveness oracle and the hard cap (``acp.session_handle_tool_silence``).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Set on the registry the keepalive route reads (a ``SessionManager``).
_SERVED = "keepalive_route_served"


def serve_keepalive_route(registry: Any) -> None:
    """*registry* is the one ``/api/session-keepalive`` reads: mark what it holds and publishes.

    Every provider it holds now is marked here, and each one it publishes from now
    on as it is published (``mark_published``).
    """
    try:
        setattr(registry, _SERVED, True)
        providers = [registry.get_provider(key) for key in registry.session_keys()]
    except Exception:
        logger.debug("keepalive route: registry not marked", exc_info=True)
        return
    for provider in providers:
        _mark(provider)


def mark_published(registry: object, session: object) -> None:
    """*registry* just published *session* (or removed it: None): mark its provider.

    Only the registry the keepalive route reads marks anything.
    """
    if session is None or getattr(registry, _SERVED, False) is not True:
        return
    _mark(getattr(session, "provider", None))


def _mark(provider: object) -> None:
    mark = getattr(provider, "mark_keepalive_routed", None)
    if not callable(mark):
        return
    try:
        mark()
    except Exception:
        logger.debug("keepalive route: provider not marked", exc_info=True)
