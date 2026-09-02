"""Token refresh across the three identity endpoints, under a cross-process lock.

Refresh routing (kiro-cli parity):

  social / builder-id-social  ->  POST {service}/refreshToken   {refreshToken}
  identity_center             ->  SSO-OIDC /token grant refresh_token (needs client creds)
  external_idp                ->  the token's own tokenEndpoint, grant refresh_token

The single-flight lock (mirroring kiro-cli's refresh_coordinator) prevents concurrent
sessions from stampeding the refresh endpoint: the holder re-reads the store inside the
lock and skips the HTTP call if a peer already produced a fresh token.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

import aiohttp

from kiro_crew.auth.login.endpoints import (
    DEFAULT_TOKEN_TTL_SECS,
    USER_AGENT,
    oidc_url,
    social_service_url,
)
from kiro_crew.auth.store import KasToken, TokenStore
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.platform_compat import acquire_lock, release_lock

logger = logging.getLogger(__name__)

# Per-identity in-process locks, so concurrent coroutines in THIS process serialize
# on the asyncio lock (cooperative, no event-loop block) before contending the
# cross-process flock. LoopBoundLock (not a bare module-global asyncio.Lock, which
# binds to the import-time loop and breaks across loops/tests — issue #4800) is
# safe to declare at module scope; the per-identity registry is created lazily.
_identity_locks: dict[str, LoopBoundLock] = {}
_locks_guard = LoopBoundLock()


def _acquire_flock(fd: int) -> None:
    """Blocking cross-process lock acquire — call only via asyncio.to_thread."""
    acquire_lock(fd, exclusive=True)


def _release_flock(fd: int) -> None:
    """Cross-process lock release — call only via asyncio.to_thread."""
    release_lock(fd)


async def _identity_lock(identity: str) -> LoopBoundLock:
    async with _locks_guard:
        lock = _identity_locks.get(identity)
        if lock is None:
            lock = LoopBoundLock()
            _identity_locks[identity] = lock
        return lock


_JSON_HEADERS = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
_AMZ_HEADERS = {"Content-Type": "application/x-amz-json-1.1", "User-Agent": USER_AGENT}


class RefreshError(Exception):
    """A token could not be refreshed."""


async def ensure_fresh(
    store: TokenStore,
    token: KasToken,
    *,
    session: aiohttp.ClientSession,
) -> KasToken:
    """Return a token satisfying the KAS refresh margin, refreshing under lock if needed.

    If ``token`` is still outside the refresh buffer it is returned unchanged. Otherwise
    the cross-process lock is taken; the store is re-read inside the lock and, if a peer
    already refreshed, that fresher token is returned without an HTTP call.
    """
    if not token.is_expired():
        return token

    # Two-layer serialization so a contended refresh never blocks the event loop:
    #  1. a per-identity asyncio.Lock cooperatively serializes coroutines in THIS
    #     process, so same-process callers await rather than racing the flock;
    #  2. the cross-process flock — whose acquire blocks on ANOTHER process's HTTP
    #     refresh — is taken and released in a worker thread via asyncio.to_thread,
    #     so a peer process holding it parks the thread, not the loop.
    identity_lock = await _identity_lock(token.identity)
    async with identity_lock:
        # Re-check after awaiting the in-process lock: a sibling coroutine may have
        # refreshed while we waited, making both the flock and the HTTP call moot.
        # Vault reads do file IO (plus owner-only key checks) — off-loop.
        current = await asyncio.to_thread(store.load, token.identity)
        if current is not None and not current.is_expired():
            return current

        def _open_refresh_lock() -> int:
            # lock_path runs make_owner_only_dir (blocking filesystem work; the
            # Windows DACL is applied in-process) and os.open is blocking IO — the
            # whole lock-file setup stays off the event loop.
            lock_path = store.lock_path(token.identity)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            return os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)

        fd = await asyncio.to_thread(_open_refresh_lock)
        try:
            # Blocking flock acquire runs off-loop.
            await asyncio.to_thread(_acquire_flock, fd)
            try:
                # Re-read inside the cross-process lock: a peer process may have
                # refreshed while we waited on the flock.
                current = await asyncio.to_thread(store.load, token.identity)
                if current is not None and not current.is_expired():
                    return current
                refreshed = await _refresh(current or token, session=session)
                # store.save does blocking file IO (owner-only lockdown included) — off-loop.
                await asyncio.to_thread(store.save, refreshed)
                return refreshed
            finally:
                await asyncio.to_thread(_release_flock, fd)
        finally:
            await asyncio.to_thread(os.close, fd)


async def _refresh(token: KasToken, *, session: aiohttp.ClientSession) -> KasToken:
    if not token.refresh_token:
        raise RefreshError(f"no refresh token for identity {token.identity}")
    if token.identity == "social":
        return await _refresh_social(token, session=session)
    if token.identity == "external_idp":
        return await _refresh_external_idp(token, session=session)
    if token.identity in ("builder_id", "identity_center"):
        return await _refresh_sso_oidc(token, session=session)
    raise RefreshError(f"unknown identity for refresh: {token.identity}")


async def _refresh_social(token: KasToken, *, session: aiohttp.ClientSession) -> KasToken:
    url = f"{social_service_url()}/refreshToken"
    async with session.post(
        url, json={"refreshToken": token.refresh_token}, headers=_JSON_HEADERS
    ) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise RefreshError(f"social refresh failed: HTTP {resp.status} {body}")
        data = await resp.json()
    profile_arn = data.get("profileArn") or token.profile_arn
    if not profile_arn:
        raise RefreshError("social refresh returned no profile ARN")
    return KasToken(
        access_token=data["accessToken"],
        expires_at=_expiry(data.get("expiresIn")),
        provider=token.provider,
        identity=token.identity,
        refresh_token=data.get("refreshToken") or token.refresh_token,
        profile_arn=profile_arn,
        region=token.region,
    )


async def _refresh_sso_oidc(token: KasToken, *, session: aiohttp.ClientSession) -> KasToken:
    if not (token.client_id and token.client_secret):
        raise RefreshError("SSO-OIDC refresh needs stored client credentials")
    region = token.region or "us-east-1"
    url = f"{oidc_url(region)}/token"
    payload = {
        "clientId": token.client_id,
        "clientSecret": token.client_secret,
        "grantType": "refresh_token",
        "refreshToken": token.refresh_token,
    }
    async with session.post(url, json=payload, headers=_AMZ_HEADERS) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise RefreshError(f"SSO-OIDC refresh failed: HTTP {resp.status} {body}")
        data = await resp.json()
    return KasToken(
        access_token=data["accessToken"],
        expires_at=_expiry(data.get("expiresIn")),
        provider=token.provider,
        identity=token.identity,
        refresh_token=data.get("refreshToken") or token.refresh_token,
        profile_arn=token.profile_arn,
        region=region,
        client_id=token.client_id,
        client_secret=token.client_secret,
    )


async def _refresh_external_idp(token: KasToken, *, session: aiohttp.ClientSession) -> KasToken:
    if not token.token_endpoint:
        raise RefreshError("external IdP refresh needs a token endpoint")
    form = {
        "grant_type": "refresh_token",
        "refresh_token": token.refresh_token or "",
    }
    if token.client_id:
        form["client_id"] = token.client_id
    async with session.post(
        token.token_endpoint, data=form, headers={"User-Agent": USER_AGENT}
    ) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise RefreshError(f"external IdP refresh failed: HTTP {resp.status} {body}")
        data = await resp.json()
    return KasToken(
        access_token=data["access_token"],
        expires_at=_expiry(data.get("expires_in")),
        provider=token.provider,
        identity=token.identity,
        refresh_token=data.get("refresh_token") or token.refresh_token,
        profile_arn=token.profile_arn,
        region=token.region,
        auth_method="external_idp",
        client_id=token.client_id,
        token_endpoint=token.token_endpoint,
    )


def _expiry(expires_in: object) -> datetime:
    try:
        secs = int(expires_in)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        secs = DEFAULT_TOKEN_TTL_SECS
    return datetime.now(timezone.utc) + timedelta(seconds=secs)
