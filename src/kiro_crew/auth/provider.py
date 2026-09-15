"""KasAuthProvider — feeds the embedded KAS engine a live Kiro token.

Fulfills the ``IAuthProvider`` contract KAS consumes (getToken / getProfileArn /
isAuthenticated / readToken) and can render the ``_kiro/auth/getAccessToken``
acp-callback response. It resolves the highest-priority stored identity
(External > Builder > Social), refreshes it under the cross-process lock when it is
inside KAS's refresh margin, and returns the fields KAS needs — never the refresh token,
which stays inside Kiro Crew.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

import aiohttp

from kiro_crew.auth.refresh import IdentitySignedOut, ensure_fresh
from kiro_crew.auth.store import KasToken, TokenStore

logger = logging.getLogger(__name__)


class NotAuthenticated(Exception):
    """No usable Kiro credential is stored."""


class KasAuthProvider:
    """Adapts the token store + refresh to what KAS asks for.

    One instance may be shared across sessions; it holds no per-caller state and
    re-resolves the store on each ``current`` call so a login/logout in another session
    is picked up.
    """

    def __init__(
        self,
        store: TokenStore,
        *,
        session: aiohttp.ClientSession | None = None,
        allow_env_api_key: bool = True,
    ) -> None:
        """``allow_env_api_key`` controls the ``KIRO_API_KEY`` headless bypass.

        Default on, matching KAS's own EnvAuthProvider for an in-process host. A
        consumer that answers for the VAULT specifically -- the relay's
        ``_kiro/auth/getAccessToken`` callback -- passes ``False``: there, an
        ambient environment key must not stand in for an identity the operator
        just signed out of, and the engine would send it with the wrong token
        type anyway (it is not an OIDC bearer).
        """
        self._store = store
        self._session = session
        self._allow_env_api_key = allow_env_api_key

    async def _session_or_temp(self):
        if self._session is not None:
            return self._session, False
        return aiohttp.ClientSession(), True

    def _env_api_key(self) -> str | None:
        if not self._allow_env_api_key:
            return None
        return os.environ.get("KIRO_API_KEY") or None

    async def current(self) -> KasToken:
        """Resolve + refresh the active token, or raise NotAuthenticated."""
        # KIRO_API_KEY is a headless bypass matching KAS's EnvAuthProvider: a raw bearer
        # with no refresh and no profile ARN (region falls back us-east-1).
        api_key = self._env_api_key()
        # Vault reads do file IO (plus owner-only key checks) — off the
        # event loop.
        token = await asyncio.to_thread(self._store.resolve)
        if token is None:
            if api_key:
                return KasToken(
                    access_token=api_key,
                    expires_at=datetime.now(timezone.utc) + timedelta(days=3650),
                    provider="ApiKey",
                    identity="social",  # storage slot unused for env key; never persisted
                )
            raise NotAuthenticated("no stored Kiro credential")

        session, temp = await self._session_or_temp()
        try:
            return await ensure_fresh(self._store, token, session=session)
        except IdentitySignedOut as exc:
            # A logout landed between the resolve above and the refresh: the same
            # verdict as an empty vault, so callers (the KAS callback) render the
            # sign-in prompt rather than a refresh error.
            raise NotAuthenticated("signed out during refresh") from exc
        finally:
            if temp:
                await session.close()

    # ---- IAuthProvider surface -------------------------------------------------

    async def get_token(self) -> str:
        return (await self.current()).access_token

    async def get_profile_arn(self) -> str | None:
        return (await self.current()).profile_arn

    def is_authenticated(self) -> bool:
        return self._store.resolve() is not None or bool(self._env_api_key())

    def read_token(self) -> dict | None:
        """Non-refreshing peek at auth_method / provider (KAS readToken)."""
        token = self._store.resolve()
        if token is None:
            return None
        return {"authMethod": token.auth_method, "provider": token.provider}

    async def resolve_request_credential(self) -> dict:
        """Atomic snapshot for KAS's BFF header middleware (addKiroAuthHeaders)."""
        token = await self.current()
        return {
            "accessToken": token.access_token,
            "profileArn": token.profile_arn,
            "provider": token.provider,
        }

    # ---- acp-callback ----------------------------------------------------------

    async def get_access_token_callback(self) -> dict:
        """Build the ``_kiro/auth/getAccessToken`` response.

        expiresAt is delivered as ISO-8601 UTC; ``current`` guarantees it is beyond the
        3-minute refresh margin. Refresh token is intentionally excluded — KAS only ever
        sees the access token.
        """
        token = await self.current()
        resp: dict = {
            "accessToken": token.access_token,
            "expiresAt": token.expires_at.astimezone(timezone.utc).isoformat(),
        }
        if token.profile_arn:
            resp["profileArn"] = token.profile_arn
        if token.provider:
            resp["provider"] = token.provider
        if token.auth_method:
            resp["authMethod"] = token.auth_method
        return resp
