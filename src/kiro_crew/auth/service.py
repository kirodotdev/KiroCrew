"""KasLoginService — orchestrates interactive KAS-mode login for the dashboard API.

Ties the token store, install-shape detection, and the device-code login flow into
the small state machine the HTTP handlers expose: status -> begin -> poll -> (token
saved) / logout. The service owns the pending-login table because the device flow is
inherently two requests apart (begin returns the user code, poll observes approval),
and the DeviceAuthorization codes must never leave the gateway process — the browser
only ever sees the verification URI.

Polling is single-shot by design: the dashboard drives the cadence, so a slow or
abandoned login never pins a server-side task. Expiry is enforced locally from the
authorization's own deadline, which also bounds how long an abandoned entry lives.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiohttp

from kiro_crew.auth.login import device
from kiro_crew.auth.login.endpoints import USER_AGENT, social_service_url
from kiro_crew.auth.shape import select_transport
from kiro_crew.auth.store import SocialProvider, TokenStore, TokenStoreError

logger = logging.getLogger(__name__)

_HEADERS = {"Content-Type": "application/json", "User-Agent": USER_AGENT}


class UnknownLoginError(Exception):
    """The login_id does not match any pending device authorization."""


@dataclass
class _PendingLogin:
    """A device authorization awaiting user approval, keyed by opaque login_id."""

    auth: device.DeviceAuthorization
    provider: SocialProvider


def _parse_provider(provider_str: str) -> SocialProvider:
    """Map a caller-supplied provider name to the wire enum (case-insensitive)."""
    normalized = (provider_str or "").strip().lower()
    for member in SocialProvider:
        if member.value.lower() == normalized or member.name.lower() == normalized:
            return member
    raise ValueError(f"unknown social provider: {provider_str!r}")


class KasLoginService:
    """Login orchestration for the KAS-mode auth subsystem.

    Handlers stay stateless; all mutable login state (the pending-authorization
    table and the shared HTTP session) lives here, guarded by one asyncio.Lock so
    concurrent dashboard tabs cannot corrupt the table.
    """

    def __init__(
        self,
        store: TokenStore,
        *,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._store = store
        self._session = session
        self._pending: dict[str, _PendingLogin] = {}
        self._lock = asyncio.Lock()

    async def _http(self) -> aiohttp.ClientSession:
        # Lazy: constructing the session at gateway boot would bind it to a loop the
        # service may never run on; first use always happens on the serving loop.
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def status(self) -> dict[str, Any]:
        """Current auth state + which login transport this install shape should use."""
        # File reads happen off-loop: the store is tiny but sits on whatever disk the
        # data home lives on, and status is polled by the dashboard.
        token = await asyncio.to_thread(self._store.resolve)
        transport = select_transport()
        return {
            "authenticated": token is not None,
            "provider": token.provider if token else "",
            "identity": token.identity if token else "",
            "transport": transport.value,
        }

    async def begin_device(self, provider_str: str) -> dict[str, Any]:
        """Start a device-code login; returns what the user needs to approve it.

        Raises ValueError for an unrecognized provider and DeviceAuthError when the
        auth service rejects the authorization request.
        """
        provider = _parse_provider(provider_str)
        session = await self._http()
        auth = await device.initiate_device_authorization(provider, session=session)
        # Opaque handle so the deviceCode (the secret half of the flow) never
        # travels back to the browser; the poll endpoint accepts only this id.
        login_id = secrets.token_urlsafe(16)
        async with self._lock:
            # Evict pending logins whose device code already expired: an abandoned
            # login (UI cancel / "start over" resets client state without telling
            # the server) is otherwise never polled again, so repeated
            # start-and-abandon would grow this process-lifetime dict without bound.
            now = datetime.now(timezone.utc)
            expired = [lid for lid, entry in self._pending.items() if entry.auth.expires_at <= now]
            for lid in expired:
                del self._pending[lid]
            self._pending[login_id] = _PendingLogin(auth=auth, provider=provider)
        return {
            "login_id": login_id,
            "user_code": auth.user_code,
            "verification_uri_complete": auth.verification_uri_complete,
            "expires_at": auth.expires_at.astimezone(timezone.utc).isoformat(),
        }

    async def poll_device(self, login_id: str) -> dict[str, Any]:
        """One non-blocking poll of a pending login.

        The caller loops, not us: {status: pending|authorized|expired|error},
        plus provider on authorized. Raises UnknownLoginError for a stale/foreign id.
        """
        async with self._lock:
            pending = self._pending.get(login_id)
        if pending is None:
            raise UnknownLoginError(login_id)

        if datetime.now(timezone.utc) >= pending.auth.expires_at:
            await self._forget(login_id)
            return {"status": "expired"}

        session = await self._http()
        url = f"{social_service_url()}/oauth/device/poll"
        payload = {"deviceCode": pending.auth.device_code, "clientId": USER_AGENT}
        async with session.post(url, json=payload, headers=_HEADERS) as resp:
            if resp.status != 200:
                # Transient service hiccup: the flow's own expiry bounds retries,
                # so report pending rather than killing an approvable login.
                body = await resp.text()
                logger.warning("device poll HTTP %s: %s", resp.status, body)
                return {"status": "pending"}
            try:
                data = await resp.json()
            except (aiohttp.ClientError, ValueError):
                # Malformed 200 body: treat as a transient hiccup, not a crash;
                # the flow's expiry still bounds the caller's retries.
                logger.warning("device poll returned undecodable body", exc_info=True)
                return {"status": "pending"}
        if not isinstance(data, dict):
            logger.warning("device poll returned non-object body: %r", type(data).__name__)
            return {"status": "pending"}

        status = data.get("status", "")
        if status == "authorization_pending":
            return {"status": "pending"}
        if status == "expired_token":
            await self._forget(login_id)
            return {"status": "expired"}
        if status == "authorized":
            try:
                token = device._token_from_poll(data, pending.provider)
            except device.DeviceAuthError as err:
                # Approved but unusable (e.g. no profile ARN) — surface as error,
                # and drop the entry so the dashboard restarts cleanly.
                logger.warning("authorized device poll rejected: %s", err)
                await self._forget(login_id)
                return {"status": "error"}
            try:
                await asyncio.to_thread(self._store.save, token)
            except TokenStoreError as err:
                # Disk full / read-only store: the login was approved but we cannot
                # persist it. Report error (not authorized) and drop the pending
                # entry so a retry starts a fresh flow rather than looping.
                # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure - logs the OSError only, never the token value
                logger.warning("could not persist approved KAS token: %s", err)
                await self._forget(login_id)
                return {"status": "error"}
            await self._forget(login_id)
            return {"status": "authorized", "provider": token.provider}
        # invalid_token and anything unrecognized: unrecoverable for this login_id.
        logger.warning("device poll returned status %r", status)
        await self._forget(login_id)
        return {"status": "error"}

    async def logout(self, identity: str) -> None:
        """Delete the stored token for one identity kind.

        Raises ValueError for an identity outside the known kinds (the store's own
        path guard), so a typo can never unlink an arbitrary file.
        """
        await asyncio.to_thread(self._store.delete, identity)

    async def _forget(self, login_id: str) -> None:
        async with self._lock:
            self._pending.pop(login_id, None)
