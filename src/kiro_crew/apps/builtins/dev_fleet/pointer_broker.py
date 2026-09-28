"""The sandboxed Dev Fleet backend's client for the gateway's pointer-state broker.

The backend cannot read ``live_target.json`` — the file is bind-masked in its
namespace, and that mask is deliberate (see ``gateway_routes.py``). What it needs
from the pointer is three facts for the fleet view and the removal guards: which
checkout is live, which is staged, and whether a staged cutover can be cancelled.
The gateway answers those at ``GET /api/apps/dev-fleet/live-target``; this module
is the client, installed as :func:`live.install_pointer_provider` by ``server.main``.

Authentication is the App Kit exchange every app backend already has the material
for: ``KIROCREW_PROXY_SECRET`` (the same ``.app_secret`` the gateway signs proxied
requests with) is traded at ``POST /api/apps/dev-fleet/token`` for an app-scoped
token, which the token middleware stamps as ``request["app"] == "dev-fleet"``. That
principal is allowed to READ this one route and nothing that writes the pointer.

Failure is loud: :class:`live.PointerUnavailable`, never ``None``. ``None`` means
"nothing is live", and a removal that believed that during a broker outage would
delete the checkout a cutover is staged on. Callers choose how to degrade.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp

from kiro_crew.apps.builtins.dev_fleet import live

logger = logging.getLogger("kirocrew.app.dev-fleet.broker")

#: Same display TTL the gateway-local resolver uses, so the badge is never staler
#: through the broker than it was when the backend read the file itself.
_CACHE_TTL_SECS = live._LIVE_TTL
_REQUEST_TIMEOUT_SECS = 5.0


class GatewayPointerBroker:
    """Async callable ``(fresh) -> PointerState`` backed by the gateway route."""

    def __init__(self, *, port: int, app_secret: str, app_name: str = "dev-fleet") -> None:
        if port <= 0:
            raise ValueError("gateway port must be positive")
        self._base = f"http://127.0.0.1:{port}"
        self._secret = app_secret
        self._app = app_name
        self._token: str | None = None
        self._cached: live.PointerState | None = None
        self._cached_at = 0.0
        self._lock = asyncio.Lock()
        self._session: aiohttp.ClientSession | None = None

    # -- lifecycle -----------------------------------------------------------
    async def aclose(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    def _client(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_SECS)
            )
        return self._session

    # -- the provider --------------------------------------------------------
    async def __call__(self, fresh: bool) -> live.PointerState:
        now = time.monotonic()
        if not fresh and self._cached is not None and (now - self._cached_at) < _CACHE_TTL_SECS:
            return self._cached
        # One in-flight fetch at a time: the fleet refresher and a request handler
        # asking together should share one round trip, not race two.
        async with self._lock:
            now = time.monotonic()
            if not fresh and self._cached is not None and (now - self._cached_at) < _CACHE_TTL_SECS:
                return self._cached
            state = await self._fetch(fresh)
            self._cached = state
            self._cached_at = time.monotonic()
            return state

    async def _fetch(self, fresh: bool) -> live.PointerState:
        try:
            token = await self._ensure_token()
            payload = await self._get_state(token, fresh)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            raise live.PointerUnavailable(f"gateway live-target route unreachable: {exc}") from exc
        if payload is None:
            # The token was refused: it may have expired or the gateway may have
            # restarted with a new signing key. Re-exchange the secret exactly once.
            self._token = None
            try:
                token = await self._ensure_token()
                payload = await self._get_state(token, fresh)
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                raise live.PointerUnavailable(
                    f"gateway live-target route unreachable: {exc}"
                ) from exc
            if payload is None:
                raise live.PointerUnavailable("gateway refused the dev-fleet app token")
        return _parse_state(payload)

    async def _ensure_token(self) -> str:
        if self._token:
            return self._token
        async with self._client().post(
            f"{self._base}/api/apps/{self._app}/token",
            headers={"X-App-Secret": self._secret},
        ) as resp:
            if resp.status != 200:
                raise live.PointerUnavailable(f"app token exchange failed with HTTP {resp.status}")
            data = await resp.json(content_type=None)
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise live.PointerUnavailable("app token exchange returned no token")
        self._token = token
        return token

    async def _get_state(self, token: str, fresh: bool) -> dict[str, Any] | None:
        """``None`` when the token was refused (401/403); raises on transport errors."""
        params = {"token": token}
        if fresh:
            params["fresh"] = "1"
        async with self._client().get(
            f"{self._base}/api/apps/{self._app}/live-target", params=params
        ) as resp:
            if resp.status in (401, 403):
                return None
            if resp.status != 200:
                raise live.PointerUnavailable(
                    f"gateway live-target route answered HTTP {resp.status}"
                )
            data = await resp.json(content_type=None)
        if not isinstance(data, dict):
            raise live.PointerUnavailable("gateway live-target route returned a non-object")
        return data

    # -- removal leases ------------------------------------------------------
    async def acquire_removal_lease(self, path: str) -> str | None:
        """Ask the gateway to lease *path* for a removal; returns the lease CAPABILITY.

        ``None`` means a cutover is in flight (refused, not an error). Raises
        :class:`live.PointerUnavailable` when the gateway cannot be reached — the caller
        treats that as "not granted", because it cannot prove no cutover is staging the
        path it is about to delete.
        """
        data = await self._lease_call("POST", {"path": path})
        token = data.get("token")
        if not data.get("granted") or not isinstance(token, str) or not token:
            return None
        return token

    async def renew_removal_lease(self, token: str) -> bool:
        """Heartbeat: ``False`` means the lease is gone and must be treated as lost."""
        data = await self._lease_call("PUT", {"token": token})
        return bool(data.get("renewed"))

    async def release_removal_lease(self, token: str) -> None:
        await self._lease_call("DELETE", {"token": token})

    async def _lease_call(self, method: str, body: dict[str, str]) -> dict[str, Any]:
        for attempt in (0, 1):
            try:
                app_token = await self._ensure_token()
                async with self._client().request(
                    method,
                    f"{self._base}/api/apps/{self._app}/live-target/removal-lease",
                    params={"token": app_token},
                    json=body,
                ) as resp:
                    if resp.status in (401, 403) and attempt == 0:
                        self._token = None
                        continue
                    if resp.status != 200:
                        raise live.PointerUnavailable(
                            f"gateway removal-lease route answered HTTP {resp.status}"
                        )
                    data = await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                raise live.PointerUnavailable(
                    f"gateway removal-lease route unreachable: {exc}"
                ) from exc
            if not isinstance(data, dict):
                raise live.PointerUnavailable("gateway removal-lease route returned a non-object")
            return data
        raise live.PointerUnavailable("gateway refused the dev-fleet app token")


def _parse_state(payload: dict[str, Any]) -> live.PointerState:
    live_path = payload.get("live")
    staged = payload.get("staged")
    cancel = payload.get("staged_cancel_available")
    previous = payload.get("previous")
    if live_path is not None and not isinstance(live_path, str):
        raise live.PointerUnavailable("gateway live-target route: 'live' is not a string")
    if staged is not None and not isinstance(staged, str):
        raise live.PointerUnavailable("gateway live-target route: 'staged' is not a string")
    if previous is not None and not isinstance(previous, str):
        raise live.PointerUnavailable("gateway live-target route: 'previous' is not a string")
    return live.PointerState(
        live=live_path or None,
        staged=staged or None,
        staged_cancel_available=bool(cancel),
        previous=previous or None,
    )


def unconfigured_provider(reason: str) -> live.PointerProvider:
    """A provider that reports *reason* as an outage on every read.

    Installed when the backend has no gateway port to call. It exists so the
    backend's identity ("I am the sandboxed process; I do not read the pointer
    myself") is established even when the broker cannot be built — leaving the
    provider unset would make ``live`` fall back to reading the masked file
    locally, which answers the empty mask stub as "nothing is live".
    """

    async def _provider(fresh: bool) -> live.PointerState:
        raise live.PointerUnavailable(reason)

    return _provider


def unconfigured_lease_client(reason: str) -> live.RemovalLeaseClient:
    """The lease counterpart of :func:`unconfigured_provider`: every lease is refused
    (as an outage, which ``live.removal_lease`` reads as "not granted")."""

    async def _acquire(path: str) -> str | None:
        raise live.PointerUnavailable(reason)

    async def _renew(token: str) -> bool:
        raise live.PointerUnavailable(reason)

    async def _release(token: str) -> None:
        raise live.PointerUnavailable(reason)

    return (_acquire, _renew, _release)


__all__ = ("GatewayPointerBroker", "unconfigured_lease_client", "unconfigured_provider")
