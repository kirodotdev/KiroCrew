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
from concurrent.futures import Executor
from contextvars import copy_context
from datetime import datetime, timedelta, timezone

import aiohttp

from kiro_crew.auth.refresh import IdentitySignedOut, ensure_fresh
from kiro_crew.auth.store import KasToken, TokenStore

logger = logging.getLogger(__name__)

#: The profile ARN every AWS Builder ID sign-in is served under. Builder ID is a
#: personal free-tier account with no profile of its own: ``ListAvailableProfiles``
#: answers it ``AccessDeniedException``, yet the Kiro runtime refuses a request
#: that names no profile ("profileArn is required for this request"). kiro-cli
#: answers the engine's ``_kiro/auth/getAccessToken`` for a Builder ID token with
#: this fixed value (``BUILDER_ID_PROFILE_ARN``), and Crew sends the same one so a
#: Builder ID sign-in works on the bundle route exactly as it does behind kiro-cli.
BUILDER_ID_PROFILE_ARN = "arn:aws:codewhisperer:us-east-1:638616132270:profile/AAAACCCCXXXX"


def kas_profile_arn(token: KasToken) -> str | None:
    """The profile ARN the engine is given for *token*.

    A Builder ID identity always gets :data:`BUILDER_ID_PROFILE_ARN`, as kiro-cli
    does: the store never holds one for it, because there is none to look up.
    Every other identity sends what its sign-in stored.
    """
    if token.identity == "builder_id":
        return BUILDER_ID_PROFILE_ARN
    return token.profile_arn


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
        executor: Executor | None = None,
    ) -> None:
        """``allow_env_api_key`` controls the ``KIRO_API_KEY`` headless bypass.

        Default on, matching KAS's own EnvAuthProvider for an in-process host. A
        consumer that answers for the VAULT specifically -- the relay's
        ``_kiro/auth/getAccessToken`` callback -- passes ``False``: there, an
        ambient environment key must not stand in for an identity the operator
        just signed out of, and the engine would send it with the wrong token
        type anyway (it is not an OIDC bearer).

        ``executor`` runs only the initial vault resolve; ``None`` uses the event
        loop's default executor. Refresh-lock work and a temporary refresh session
        stay on the default path so lock waiters cannot exhaust this caller's pool.
        """
        self._store = store
        self._session = session
        self._allow_env_api_key = allow_env_api_key
        self._executor = executor

    async def _session_or_temp(self):
        if self._session is not None:
            return self._session, False
        return aiohttp.ClientSession(), True

    def _env_api_key(self) -> str | None:
        if not self._allow_env_api_key:
            return None
        return os.environ.get("KIRO_API_KEY") or None

    async def current(self, *, rejected_access_token: str | None = None) -> KasToken:
        """Resolve + refresh the active token, or raise NotAuthenticated.

        *rejected_access_token*: see :func:`ensure_fresh`. A refused token that is
        still the stored one is refreshed even before its expiry.
        """
        # KIRO_API_KEY is a headless bypass matching KAS's EnvAuthProvider: a raw bearer
        # with no refresh and no profile ARN (region falls back us-east-1).
        api_key = self._env_api_key()
        # Vault reads do file IO (plus owner-only key checks) — off the
        # event loop.
        if self._executor is None:
            token = await asyncio.to_thread(self._store.resolve)
        else:
            token = await asyncio.get_running_loop().run_in_executor(
                self._executor, copy_context().run, self._store.resolve
            )
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
            return await ensure_fresh(
                self._store, token, session=session, rejected_access_token=rejected_access_token
            )
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
        return kas_profile_arn(await self.current())

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
            "profileArn": kas_profile_arn(token),
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
        profile_arn = kas_profile_arn(token)
        if profile_arn:
            resp["profileArn"] = profile_arn
        if token.provider:
            resp["provider"] = token.provider
        if token.auth_method:
            resp["authMethod"] = token.auth_method
        return resp
