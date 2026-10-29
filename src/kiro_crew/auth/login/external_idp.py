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
from collections.abc import Mapping
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
    # The endpoint may carry its own query (a tenant or policy selector); the OAuth
    # parameters are appended to it, never glued on with a second '?'.
    separator = "&" if urllib.parse.urlsplit(meta.authorization_endpoint).query else "?"
    return f"{meta.authorization_endpoint}{separator}{urllib.parse.urlencode(params)}"


@dataclass
class PortalIdpHandoff:
    """What the Kiro portal's external_idp callback leg names: the org's IdP client.

    The leg carries no code and no endpoints. The endpoints come from the issuer's
    own discovery document (:func:`discover`), never from the redirect.
    """

    issuer_url: str
    client_id: str
    scopes: str
    login_hint: str | None = None
    audience: str | None = None


def require_https_url(value: object, field: str) -> str:
    """Require an absolute, credential-free, fragment-free HTTPS URL; return it.

    Raises :class:`ExternalIdpAuthError` naming *field* otherwise.
    """
    if not isinstance(value, str) or not value or len(value) > _MAX_URL_LEN:
        raise ExternalIdpAuthError(f"{field} is missing or too long")
    parts = urllib.parse.urlsplit(value)
    try:
        parts.port
    except ValueError as err:
        raise ExternalIdpAuthError(f"{field} has an invalid port") from err
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
    ):
        raise ExternalIdpAuthError(f"{field} must be an absolute HTTPS URL")
    return value


def _same_issuer(advertised: str, expected: str) -> bool:
    """Exact issuer equality, plus the one RFC 3986 equivalence: empty path == ``/``."""
    if advertised == expected:
        return True
    a, e = urllib.parse.urlsplit(advertised), urllib.parse.urlsplit(expected)
    if (a.scheme, a.netloc, a.query) != (e.scheme, e.netloc, e.query):
        return False
    return {a.path, e.path} == {"", "/"}


# Upper bound on any URL or identifier taken from the portal leg or the discovery
# document: long enough for every real issuer, short enough that a hostile value
# cannot balloon the stored token or the authorization URL.
_MAX_URL_LEN = 2048
_MAX_FIELD_LEN = 1024


def handoff_from_callback(query: Mapping[str, str]) -> PortalIdpHandoff:
    """Parse the portal's external_idp callback leg; raises on a missing or bad field."""
    issuer_url = require_https_url(query.get("issuer_url"), "issuer_url")
    if urllib.parse.urlsplit(issuer_url).query:
        raise ExternalIdpAuthError("issuer_url must not carry a query string")
    client_id = query.get("client_id") or ""
    if not client_id or len(client_id) > _MAX_FIELD_LEN:
        raise ExternalIdpAuthError("portal callback carried no usable client_id")
    scopes = query.get("scopes") or ""
    if len(scopes) > _MAX_FIELD_LEN:
        raise ExternalIdpAuthError("portal callback scopes are too long")
    # The portal sends the scope list comma- or space-separated; OAuth wants spaces.
    scope_list = " ".join(s for s in scopes.replace(",", " ").split() if s)
    login_hint = query.get("login_hint") or None
    audience = query.get("audience") or None
    for name, value in (("login_hint", login_hint), ("audience", audience)):
        if value is not None and len(value) > _MAX_FIELD_LEN:
            raise ExternalIdpAuthError(f"portal callback {name} is too long")
    return PortalIdpHandoff(
        issuer_url=issuer_url,
        client_id=client_id,
        scopes=scope_list or "openid",
        login_hint=login_hint,
        audience=audience,
    )


def discovery_url(issuer_url: str) -> str:
    """``{issuer}/.well-known/openid-configuration`` (one trailing slash dropped)."""
    return issuer_url.rstrip("/") + "/.well-known/openid-configuration"


async def discover(
    handoff: PortalIdpHandoff, *, session: aiohttp.ClientSession
) -> ExternalIdpMetadata:
    """Read the issuer's OIDC discovery document and return the full IdP metadata.

    The document must name the same issuer the portal named (OIDC Discovery 1.0
    §4.3) and HTTPS authorization and token endpoints; anything else is refused, so
    a redirect that names one issuer cannot send the code exchange to another host.
    """
    url = discovery_url(handoff.issuer_url)
    async with session.get(url, headers={"User-Agent": USER_AGENT}) as resp:
        if resp.status != 200:
            raise ExternalIdpAuthError(f"OIDC discovery failed: HTTP {resp.status}")
        try:
            doc = await resp.json(content_type=None)
        except (aiohttp.ClientError, ValueError) as err:
            raise ExternalIdpAuthError("OIDC discovery returned an undecodable body") from err
    if not isinstance(doc, dict):
        raise ExternalIdpAuthError("OIDC discovery returned a non-object body")
    issuer = doc.get("issuer")
    if not isinstance(issuer, str) or not _same_issuer(issuer, handoff.issuer_url):
        raise ExternalIdpAuthError("OIDC discovery names a different issuer")
    authorization_endpoint = require_https_url(
        doc.get("authorization_endpoint"), "authorization_endpoint"
    )
    token_endpoint = require_https_url(doc.get("token_endpoint"), "token_endpoint")
    return ExternalIdpMetadata(
        issuer_url=handoff.issuer_url,
        client_id=handoff.client_id,
        scopes=handoff.scopes,
        authorization_endpoint=authorization_endpoint,
        token_endpoint=token_endpoint,
        login_hint=handoff.login_hint,
        audience=handoff.audience,
    )


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
            raise ExternalIdpAuthError(f"token exchange failed: HTTP {resp.status} {body[:500]}")
        try:
            data = await resp.json()
        except (aiohttp.ClientError, ValueError) as err:
            raise ExternalIdpAuthError("token exchange returned an undecodable body") from err

    if not isinstance(data, dict):
        raise ExternalIdpAuthError("token exchange returned a non-object body")
    access_token = data.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise ExternalIdpAuthError("token exchange returned no access token")
    try:
        expires_in = int(data.get("expires_in") or DEFAULT_TOKEN_TTL_SECS)
    except (TypeError, ValueError) as err:
        raise ExternalIdpAuthError("token exchange returned a non-numeric expiry") from err
    return KasToken(
        access_token=access_token,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
        provider="ExternalIdp",
        identity="external_idp",
        refresh_token=data.get("refresh_token"),
        auth_method="external_idp",
        client_id=meta.client_id,
        token_endpoint=meta.token_endpoint,
        scopes=granted_scope(data, meta.with_offline_access()),
        audience=meta.audience,
    )


def granted_scope(data: Mapping[str, object], requested: str) -> str:
    """The scope a token response GRANTED (RFC 6749 §5.1), else *requested*.

    An IdP may grant less than was asked for and refuse a refresh that asks for
    more, so the stored scope is the granted one whenever the response names it.
    """
    scope = data.get("scope")
    if isinstance(scope, str) and scope.strip():
        return " ".join(scope.split())
    return requested
