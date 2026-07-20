"""Tunnel startup logic — extracted for testability without heavy dashboard imports."""

from __future__ import annotations

import logging
from typing import Callable, Set

from kiro_crew.tunnel import TunnelManager, set_tunnel_url

logger = logging.getLogger(__name__)


async def setup_tunnel(
    *,
    middlewares: list,
    allowed_origins: Set[str],
    tunnel_name_mode: str,
    tunnel_name_override: str,
    port: int,
    log_api_access: Callable[..., None],
) -> TunnelManager | None:
    """Set up the dashboard tunnel if token auth is active.

    Returns the TunnelManager if started, None if denied. Lifecycle
    (``start`` / ``stop`` / ``public_url``) routes through the active
    ``TunnelProvider`` seam inside ``TunnelManager``; the public core's Default
    provider is a no-op so the OSS build stays disabled, while a companion drives
    a real tunnel. The connect/disconnect callbacks below (CORS + ``set_tunnel_url``)
    and ``/api/tunnel/status`` stay wrapped AROUND the provider.

    The token-auth deny gate is evaluated BEFORE the manager is constructed or
    ``start()`` reached, so a companion tunnel can never start without dashboard
    token auth. All dependencies are passed explicitly — no implicit imports from
    dashboard.
    """
    # Security gate: refuse to start tunnel without token auth (deny-by-default).
    # MUST stay ahead of TunnelManager construction / provider.start().
    _has_token_auth = any(getattr(mw, "_is_token_auth", False) for mw in middlewares)
    logger.debug("Tunnel security gate: _has_token_auth=%s", _has_token_auth)

    if not _has_token_auth:
        log_api_access(
            caller="tunnel_manager",
            operation="tunnel.start_denied",
            outcome="denied",
            resources="token_auth_middleware_missing",
        )
        logger.error(
            "tunnel.enabled=true but token auth middleware is not active. "
            "Refusing to start tunnel — remote access requires auth. "
            "Enable Slack or set dashboard.url to activate token auth."
        )
        return None

    log_api_access(
        caller="tunnel_manager",
        operation="tunnel.start_allowed",
        outcome="ok",
        resources=f"port={port}",
    )

    _tunnel_url: str = ""

    async def _on_tunnel_connect(url: str) -> None:
        """Add tunnel URL to CORS origins when connected."""
        nonlocal _tunnel_url
        _tunnel_url = url
        allowed_origins.add(url)
        set_tunnel_url(url)
        log_api_access(
            caller="tunnel_manager",
            operation="tunnel.connected",
            outcome="ok",
            resources=url,
        )
        logger.info("Tunnel connected — added %s to allowed origins", url)

    async def _on_tunnel_disconnect() -> None:
        """Remove tunnel URL from CORS origins on disconnect."""
        nonlocal _tunnel_url
        set_tunnel_url("")
        if _tunnel_url:
            allowed_origins.discard(_tunnel_url)
            _tunnel_url = ""
        log_api_access(
            caller="tunnel_manager",
            operation="tunnel.disconnected",
            outcome="ok",
        )

    tunnel_mgr = TunnelManager(
        port,
        name_mode=tunnel_name_mode,
        name_override=tunnel_name_override or None,
        on_connect=_on_tunnel_connect,
        on_disconnect=_on_tunnel_disconnect,
    )
    await tunnel_mgr.start()
    return tunnel_mgr
