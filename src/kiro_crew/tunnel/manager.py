"""Tunnel lifecycle manager — thin wrapper over the ``TunnelProvider`` seam.

``TunnelManager`` owns the edition-neutral wiring the dashboard needs (name
computation, the connect/disconnect CORS reflection callbacks, the
``/api/tunnel/status`` snapshot) and delegates the actual lifecycle —
``start`` / ``stop`` / ``public_url`` — UNCONDITIONALLY to
``current_context().tunnel``.  There is deliberately NO edition/identity branch
here: the public core ships ``DefaultTunnelProvider`` (a no-op), so the
open-source build stays byte-identical (the tunnel is disabled and reports
"not available in OSS"); the Amazon companion composes a real
``TunnelProvider`` and the same manager drives it.

To expose the dashboard remotely on the open-source build, run your own reverse
proxy / tunnel (for example ``ssh -R``, ``cloudflared``, ``ngrok``) in front of
the dashboard port.
"""

from __future__ import annotations

import enum
import hashlib
import logging
import socket
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Optional

from kiro_crew.platform.context import (
    async_safe_context_call,
    current_context,
    safe_context_call,
)

if TYPE_CHECKING:
    from kiro_crew.platform.interfaces import TunnelProvider

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
    """Wraps the active ``TunnelProvider`` with the dashboard's edition-neutral glue.

    Lifecycle (``start`` / ``stop`` / ``public_url``) delegates UNCONDITIONALLY
    to ``current_context().tunnel`` — there is no ``isinstance``/identity check
    against the Default provider (that would be an edition branch by proxy).  The
    public core's ``DefaultTunnelProvider`` is a no-op, so on the open-source
    build ``start()`` leaves the tunnel disabled and reports "not available in
    OSS"; a companion's provider drives a real tunnel through the same calls.

    The connect/disconnect CORS-reflection callbacks and the
    ``/api/tunnel/status`` snapshot stay wrapped AROUND the provider: the manager
    registers the callbacks with the provider on ``start`` and, for status,
    prefers the provider's live snapshot but falls back to its own local
    ``TunnelStatus`` (which is what the Default provider yields → byte-identical
    standalone output).

    Precedence: an explicit local lifecycle write WINS over the provider
    snapshot.  ``stop()`` (STOPPED) and the OSS-disabled ``start()`` (DISABLED)
    pin the local status so a stale/lagging snapshot can no longer resurrect a
    "connected" state after the tunnel was torn down; a subsequent ``start()``
    that a live provider actually drives clears the pin so the snapshot flows
    again.
    """

    def __init__(
        self,
        port: int,
        *,
        name_mode: str = "username",
        name_override: str | None = None,
        on_connect: Optional[Callable[[str], Awaitable[None]]] = None,
        on_disconnect: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        self._port = port
        self._name_mode = name_mode
        self._name_override = name_override
        self._on_connect = on_connect  # callback(url: str)
        self._on_disconnect = on_disconnect  # callback()

        self._status = TunnelStatus()
        # When set, an explicit local lifecycle state (STOPPED / OSS-disabled
        # DISABLED) overrides the provider snapshot until the next ``start()``.
        self._lifecycle_pinned = False

    @staticmethod
    def _provider() -> "TunnelProvider":
        return current_context().tunnel

    @property
    def status(self) -> TunnelStatus:
        """Live status: the provider's snapshot if it has one, else the local one.

        The Default provider returns ``None`` from ``status_snapshot()``, so a
        standalone manager reports exactly its own ``TunnelStatus`` (unchanged);
        a companion provider returns a live snapshot that is projected onto the
        same ``TunnelStatus`` shape the API consumers expect.

        An explicit local lifecycle state (``stop()`` → STOPPED, OSS-disabled
        ``start()`` → DISABLED) takes precedence: while pinned, a stale or
        lagging snapshot is ignored so the tunnel cannot be reported "connected"
        after it was torn down.  The snapshot is projected onto a FRESH status
        each read, so a key a later snapshot omits (e.g. a cleared ``error`` or
        ``url``) resets to its default rather than persisting a stale value.
        """
        if self._lifecycle_pinned:
            return self._status
        snap = safe_context_call(
            lambda: self._provider().status_snapshot(),
            fallback=None,
            log_message="tunnel.status_snapshot() lookup failed; using local status",
        )
        if snap:
            return self._project_snapshot(snap)
        return self._status

    def _project_snapshot(self, snap: Dict[str, Any]) -> TunnelStatus:
        """Project an edition-neutral status dict onto a fresh ``TunnelStatus``.

        A new object is built each read (seeded only with the manager-owned
        ``started_at``) so fields absent from *snap* fall back to defaults
        instead of retaining values a previous snapshot set.
        """
        status = TunnelStatus(started_at=self._status.started_at)
        raw_state = snap.get("state")
        if raw_state is not None:
            try:
                status.state = TunnelState(raw_state)
            except ValueError:
                logger.debug("tunnel snapshot has unknown state %r; keeping prior", raw_state)
                status.state = self._status.state
        for field in ("url", "error", "started_at", "connected_at", "reconnect_attempt"):
            if field in snap:
                setattr(status, field, snap[field])
        return status

    @property
    def public_url(self) -> str:
        """The live public URL from the provider, but only while CONNECTED.

        Empty on the no-op Default (state DISABLED).  Guarding on CONNECTED
        mirrors the pre-seam stub (``url if state == CONNECTED else ""``) so a
        companion that keeps its last URL while RECONNECTING/ERROR is not
        reported as live.
        """
        if self.status.state != TunnelState.CONNECTED:
            return ""
        return safe_context_call(
            lambda: self._provider().public_url(),
            fallback="",
            log_message="tunnel.public_url() lookup failed",
        )

    @property
    def state(self) -> TunnelState:
        return self.status.state

    def _tunnel_name(self) -> str:
        """Compute the tunnel name based on config."""
        if self._name_override:
            return self._name_override
        if self._name_mode == "hash":
            host_hash = hashlib.sha256(socket.gethostname().encode()).hexdigest()[:8]
            return f"kirocrew-{host_hash}"
        return "kirocrew"

    async def start(self) -> None:
        """Start the tunnel via the active provider (no-op on the Default).

        Registers the connect/disconnect CORS-reflection callbacks with the
        provider, then delegates ``start()``.  The Default provider is a no-op,
        so on the open-source build this logs the "not available in OSS" notice
        and leaves the tunnel disabled — byte-identical to the previous stub.
        """
        # A fresh start clears any prior lifecycle pin (STOPPED/DISABLED) so a
        # live provider's snapshot flows through ``status`` again.
        self._lifecycle_pinned = False
        self._status.started_at = time.time()
        safe_context_call(
            lambda: self._provider().register_callbacks(
                on_connect=self._on_connect,
                on_disconnect=self._on_disconnect,
            ),
            fallback=None,
            log_message="tunnel.register_callbacks() failed",
        )
        await async_safe_context_call(
            lambda: self._provider().start(),
            fallback=None,
            log_message="tunnel.start() failed",
        )
        if self._provider_is_active():
            return
        # No managed provider drove a start → preserve the OSS-disabled notice.
        logger.info(
            "Tunnel feature %s. The dashboard will not be exposed via a managed tunnel. "
            "Use your own reverse proxy / tunnel in front of port %d for remote access.",
            _NOT_AVAILABLE,
            self._port,
        )
        self._status.state = TunnelState.DISABLED
        self._status.error = _NOT_AVAILABLE
        # Pin the disabled state: no provider is active, so a stale snapshot
        # must not override the OSS-disabled notice on the next status read.
        self._lifecycle_pinned = True

    async def stop(self) -> None:
        """Stop the tunnel via the active provider (no-op teardown on the Default).

        Only pins the local ``STOPPED`` state after the provider actually
        confirmed the stop. If ``provider().stop()`` raised (swallowed by
        ``async_safe_context_call`` → sentinel), the underlying tunnel may still
        be reachable, so we must NOT pin STOPPED and hide its URL — surface the
        error and let the next status read reflect the provider's real state.
        """
        _FAILED = object()
        outcome = await async_safe_context_call(
            lambda: self._provider().stop(),
            fallback=_FAILED,
            log_message="tunnel.stop() failed",
        )
        if outcome is _FAILED:
            # Provider stop failed — do NOT claim STOPPED (the tunnel may still
            # be up); leave state/URL to the provider's live snapshot so the
            # dashboard reflects the real, still-reachable state instead of
            # hiding it behind a STOPPED pin.
            logger.warning("Tunnel manager stop failed; not pinning STOPPED")
            return
        self._status.state = TunnelState.STOPPED
        self._status.url = ""
        self._status.error = ""
        self._status.connected_at = 0.0
        self._status.reconnect_attempt = 0
        # Pin STOPPED: a provider whose snapshot lags (or caches "connected")
        # must not resurrect a live tunnel after an explicit stop.
        self._lifecycle_pinned = True
        logger.info("Tunnel manager stopped")

    def _provider_is_active(self) -> bool:
        """Whether the active provider reports the tunnel enabled.

        Reads the edition-neutral ``enabled()`` — NOT an ``isinstance`` check
        against ``DefaultTunnelProvider`` — so standalone (Default ``enabled()``
        → ``False``) keeps the OSS-disabled notice while a companion whose
        provider is enabled skips it.
        """
        return safe_context_call(
            lambda: self._provider().enabled(),
            fallback=False,
            log_message="tunnel.enabled() lookup failed",
        )
