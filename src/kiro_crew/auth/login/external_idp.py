"""External IdP (enterprise SSO) login via authorization-code against the customer IdP.

The portal returns the customer's IdP metadata (issuer, client_id, scopes, etc.); we
run a standard authorization-code + PKCE flow directly against that IdP's endpoints and
exchange the code at its token endpoint. ``offline_access`` is ensured in scope so a
refresh token is issued (refresh itself lives in refresh.py).

The resulting token carries ``provider='ExternalIdp'`` and ``auth_method='external_idp'``
so KAS classifies it as enterprise governance and emits the EXTERNAL_IDP TokenType.
"""

from __future__ import annotations

import logging
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import aiohttp

from kiro_crew.auth.login.endpoints import DEFAULT_TOKEN_TTL_SECS, USER_AGENT
from kiro_crew.auth.store import KasToken

logger = logging.getLogger(__name__)


class ExternalIdpAuthError(Exception):
    """External IdP authorization-code login failed."""


@dataclass
class ExternalIdpMetadata:
    """IdP details the portal hands back for the enterprise SSO flow."""

    issuer_url: str
    client_id: str
    scopes: str
    authorization_endpoint: str
    token_endpoint: str
    login_hint: str | None = None
    audience: str | None = None

    def with_offline_access(self) -> str:
        """Return scopes guaranteed to include offline_access (for refresh tokens)."""
        parts = self.scopes.split()
        if "offline_access" not in parts:
            parts.append("offline_access")
        return " ".join(parts)


def build_authorization_url(
    meta: ExternalIdpMetadata,
    *,
    redirect_uri: str,
    state: str,
    code_challenge: str,
) -> str:
    """Standard OIDC authorization-code + PKCE request URL against the customer IdP."""
    params = {
        "response_type": "code",
        "client_id": meta.client_id,
        "redirect_uri": redirect_uri,
        "scope": meta.with_offline_access(),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if meta.login_hint:
        params["login_hint"] = meta.login_hint
    if meta.audience:
        params["audience"] = meta.audience
    return f"{meta.authorization_endpoint}?{urllib.parse.urlencode(params)}"


async def exchange_code(
    meta: ExternalIdpMetadata,
    *,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    session: aiohttp.ClientSession,
) -> KasToken:
    """Exchange the authorization code at the IdP token endpoint (form-encoded)."""
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": meta.client_id,
        "code_verifier": code_verifier,
    }
    async with session.post(
        meta.token_endpoint, data=form, headers={"User-Agent": USER_AGENT}
    ) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise ExternalIdpAuthError(f"token exchange failed: HTTP {resp.status} {body}")
        data = await resp.json()

    access_token = data.get("access_token")
    if not access_token:
        raise ExternalIdpAuthError("token exchange returned no access token")
    expires_in = int(data.get("expires_in") or DEFAULT_TOKEN_TTL_SECS)
    return KasToken(
        access_token=access_token,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
        provider="ExternalIdp",
        identity="external_idp",
        refresh_token=data.get("refresh_token"),
        auth_method="external_idp",
        client_id=meta.client_id,
        token_endpoint=meta.token_endpoint,
    )
