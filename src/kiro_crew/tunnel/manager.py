"""Tunnel lifecycle manager — stub.

In the open-source build there is no bundled tunnel provider, so the tunnel
feature is disabled. ``TunnelManager`` and its public surface are preserved so
that callers (dashboard server/state, handlers) keep importing and constructing
it without changes; all operations are no-ops and report
"not available in OSS".

To expose the dashboard remotely, run your own reverse proxy / tunnel (for
example ``ssh -R``, ``cloudflared``, ``ngrok``) in front of the dashboard port.
"""

from __future__ import annotations

import enum
import hashlib
import logging
import socket
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_NOT_AVAILABLE = "not available in OSS"


class TunnelState(enum.Enum):
    """Tunnel connection states."""

    DISABLED = "disabled"
    STARTING = "starting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    ERROR = "error"
    STOPPED = "stopped"


@dataclass
class TunnelStatus:
    """Snapshot of tunnel state for API consumers."""

    state: TunnelState = TunnelState.DISABLED
    url: str = ""
    error: str = ""
    started_at: float = 0.0
    connected_at: float = 0.0
    reconnect_attempt: int = 0


class TunnelManager:
    """No-op tunnel manager for the open-source build.

    The bundled tunnel provider is not available in OSS, so the manager never
    spawns a child process and ``start()`` leaves the tunnel disabled. All
    public methods and properties are preserved for import compatibility.
    """

    def __init__(
        self,
        port: int,
        *,
        name_mode: str = "username",
        name_override: str | None = None,
        on_connect: object | None = None,
        on_disconnect: object | None = None,
    ) -> None:
        self._port = port
        self._name_mode = name_mode
        self._name_override = name_override
        self._on_connect = on_connect  # callback(url: str)
        self._on_disconnect = on_disconnect  # callback()

        self._status = TunnelStatus()

    @property
    def status(self) -> TunnelStatus:
        return self._status

    @property
    def public_url(self) -> str:
        return self._status.url if self._status.state == TunnelState.CONNECTED else ""

    @property
    def state(self) -> TunnelState:
        return self._status.state

    def _tunnel_name(self) -> str:
        """Compute the tunnel name based on config."""
        if self._name_override:
            return self._name_override
        if self._name_mode == "hash":
            host_hash = hashlib.sha256(socket.gethostname().encode()).hexdigest()[:8]
            return f"kirocrew-{host_hash}"
        return "kirocrew"

    async def start(self) -> None:
        """No-op: the tunnel feature is not available in the OSS build."""
        logger.info(
            "Tunnel feature %s. The dashboard will not be exposed via a managed tunnel. "
            "Use your own reverse proxy / tunnel in front of port %d for remote access.",
            _NOT_AVAILABLE,
            self._port,
        )
        self._status.state = TunnelState.DISABLED
        self._status.error = _NOT_AVAILABLE
        self._status.started_at = time.time()

    async def stop(self) -> None:
        """No-op stop — nothing to tear down in the OSS build."""
        self._status.state = TunnelState.STOPPED
        self._status.url = ""
        logger.info("Tunnel manager stopped")
