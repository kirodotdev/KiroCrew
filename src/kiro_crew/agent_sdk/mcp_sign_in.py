"""One KAS engine process that runs a single MCP server's sign-in for the owner.

The Settings MCP sign-in on KAS (``kiro_crew.connections.mint_kas``) needs a
KAS engine of its own: spawned for a session key the caller has bound to a
principal, so the handshake declares ``secretStorage`` and the tokens the engine
stores land in that principal's grant vault (``acp.kas_secret_storage``). This
module is that process, behind the agent-SDK boundary, so the connections layer
never imports the ACP layer.

Nothing here reads or writes a vault entry. The engine does that itself over the
request path the grant store already guards.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["KasSignInProcess", "kas_sign_in_kept", "sign_in_persistence_enabled"]

#: How long one read of the session's frames waits before the caller checks its
#: deadline and the process again.
_DRAIN_SLICE_SECONDS = 2.0


def sign_in_persistence_enabled() -> bool:
    """``agent.kas_persist_mcp_sign_in``; any failure to read it is off. Blocking."""
    from kiro_crew.acp import kas_secret_storage

    return kas_secret_storage.persistence_enabled()


def kas_sign_in_kept() -> bool:
    """Whether a KAS sign-in started now would be kept. Blocking.

    True only when owner dashboard chats run on KAS (``agent.acp_backend`` after
    config normalization) and ``agent.kas_persist_mcp_sign_in`` is on. With
    persistence off the grant would die with the engine process. An unreadable
    config is False.
    """
    try:
        from kiro_crew.acp_backends import ACP_BACKEND_KAS
        from kiro_crew.config.loader import KiroCrewConfig

        on_kas = KiroCrewConfig.load().agent.acp_backend == ACP_BACKEND_KAS
        return on_kas and sign_in_persistence_enabled()
    except Exception as exc:  # noqa: BLE001
        logger.warning("MCP sign-in backend unreadable (%s)", type(exc).__name__)
        return False


def _runtime_factory() -> Any:
    """The runtime class the process is. A seam, so tests run no engine."""
    from kiro_crew.acp.runtime import AcpRuntime

    return AcpRuntime


class KasSignInProcess:
    """A KAS engine spawned for one sign-in, and its one promptless session.

    ``shutdown`` and ``is_process_alive`` are the client shape the connections
    mint table calls on every row it holds.
    """

    def __init__(self, *, work_dir: Path, agent: str, grant_key: str) -> None:
        from kiro_crew.acp_backends import ACP_BACKEND_KAS

        self._key = grant_key
        self._work_dir = work_dir
        self.runtime = _runtime_factory()(
            work_dir=work_dir,
            agent=agent,
            sandbox_mode="auto",
            acp_backend=ACP_BACKEND_KAS,
            mcp_grant_key=grant_key,
        )
        self.session: Any = None

    async def start(self) -> None:
        """Spawn the engine and open one promptless session on it."""
        await self.runtime.spawn()
        self.session = await self.runtime.create_session(
            cwd=self._work_dir, agent=None, session_key=self._key
        )

    def is_process_alive(self) -> bool:  # pid-owner-ok: this module spawned it
        try:
            return bool(self.runtime.is_alive())
        except Exception:  # noqa: BLE001 -- liveness must never raise into a serve
            logger.debug("KAS sign-in liveness check failed", exc_info=True)
            return True

    async def shutdown(self) -> None:
        await self.runtime.kill(expected=True, reason="connections mint teardown")

    def keeps_grants(self) -> bool:
        """Whether this process may still store into its principal's vault.

        Read off the server its handshake installed: none means the handshake
        declared no storage, and a server whose binding lapsed has latched off
        (``SecretStorageServer.still_bound``).
        """
        server = getattr(self.runtime, "_mcp_secret_server", None)
        if server is None:
            return False
        try:
            return bool(server.still_bound())
        except Exception:  # noqa: BLE001 -- fail closed
            return False

    def begin(self, server: str) -> bool:
        """Ask the engine to start *server*'s OAuth sign-in. False: refused."""
        return bool(self.runtime.begin_mcp_sign_in(self.session.session_id, server))

    def consent_url(self, server: str) -> str | None:
        """The consent URL the engine sent for *server* since the last read."""
        for request in self.session.pop_pending_oauth_requests():
            if request.get("serverName") == server and request.get("oauthUrl"):
                return str(request["oauthUrl"])
        return None

    def buckets(self) -> dict[str, list[str]]:
        """The session's MCP report buckets: ready, failed, awaiting_auth."""
        payload = self.session.mcp_session_report().payload() if self.session else None
        payload = payload if isinstance(payload, dict) else {}
        return {
            name: [s for s in payload.get(name) or [] if isinstance(s, str)]
            for name in ("ready", "failed", "awaiting_auth")
        }

    async def read_frames(self) -> None:
        """Fold the session's pending frames in, waiting up to one slice."""
        started = time.monotonic()
        await self.session.drain_init(
            duration=_DRAIN_SLICE_SECONDS,
            idle_exit=_DRAIN_SLICE_SECONDS,
            no_report_ceiling=_DRAIN_SLICE_SECONDS,
        )
        # A dead runtime poisons the queue and the drain returns at once; the
        # rest of the slice is slept so a caller's loop cannot spin.
        spent = time.monotonic() - started
        if spent < _DRAIN_SLICE_SECONDS:
            await asyncio.sleep(_DRAIN_SLICE_SECONDS - spent)
