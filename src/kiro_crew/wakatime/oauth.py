"""WakaTime OAuth 2.0: authorize, exchange, refresh, revoke, and token storage.

WakaTime makes API keys write-only on 1 November 2026, so reading stats and
summaries needs an OAuth access token. This module owns the authorization-code
flow and where the resulting credentials live.

The split follows the secret boundary. The client ID is not a secret and sits
in ``config.wakatime``. The client secret is a secret and lives in the
encrypted vault, next to ``WAKATIME_API_KEY``. The tokens live in one vault
entry, ``WAKATIME_OAUTH_TOKENS``: a JSON object holding the access token, the
refresh token, the access token's expiry, and the redirect URI used at
authorize. One entry commits all four in a single atomic replace, so a reader
never sees a new access token with an old expiry or a mismatched refresh token.
The vault, not ``.env``, holds both, for the reason ``service.resolve_api_key``
already records: a value in ``.env`` is loaded into the process environment,
and the agent-spawn scrub strips only credential names it knows.

The provider endpoints are WakaTime's documented OAuth routes. The ``state``
value is minted per authorize request and checked on the callback, so a
forged callback with a stolen code cannot complete the exchange.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

import aiohttp

from kiro_crew.config.loader import (
    CRED_WAKATIME_OAUTH_CLIENT_SECRET,
    CRED_WAKATIME_OAUTH_TOKENS,
    KiroCrewConfig,
)
from kiro_crew.config.paths import config_dir
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.secrets.vault import SecretVault

logger = logging.getLogger(__name__)

AUTHORIZE_URL = "https://wakatime.com/oauth/authorize"
TOKEN_URL = "https://wakatime.com/oauth/token"
REVOKE_URL = "https://wakatime.com/oauth/revoke"

# Least-privilege scopes: the stats panel and the export read only these. No
# write_* scope (heartbeats keep the API key) and no email scope.
DEFAULT_SCOPES = ("read_stats", "read_summaries")

# Refresh a little before the real expiry so a read never races the boundary.
_EXPIRY_SKEW_SECS = 60

# A token exchange or refresh should answer fast or fail; it is a user-facing
# step on the settings page, not a background read.
_TOKEN_TIMEOUT_SECS = 15

# Serializes every mutation of the stored tokens so a refresh running for a
# read cannot interleave with a disconnect or a reconnect callback running for
# the owner. Without it a
# refresh that was already in flight when the owner disconnected can resume and
# re-store credentials the disconnect just cleared, leaving a token the user
# asked to remove. One process-level lock is enough: the stored tokens are the
# single dashboard owner's, and the gateway is one process. LoopBoundLock
# rather than a bare asyncio.Lock so it rebinds per event loop (a gateway
# restart-in-process or pytest-asyncio's per-test loop would otherwise raise
# RuntimeError on acquire).
_token_mutation_lock = LoopBoundLock()


def token_mutation_lock() -> LoopBoundLock:
    """Return the process-level lock guarding stored-token mutations.

    The read path (refresh then store), the callback (exchange then store),
    and the disconnect path (revoke then clear) all take this lock, so none of
    them interleave on the stored credentials.
    """
    return _token_mutation_lock


class WakaTimeOAuthError(Exception):
    """Raised when an authorize, exchange, refresh, or revoke step fails.

    Carries a short machine code so a handler can map it to an HTTP status and
    a generic client message without leaking the provider's error text.
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class OAuthTokens:
    """A token-endpoint result: the two tokens, an absolute expiry, and the
    redirect URI the grant was made for.

    ``expires_at`` is epoch seconds, derived from the provider's ``expires_in``
    at exchange time, so a later caller compares against the clock rather than
    tracking a relative countdown. ``redirect_uri`` is kept because WakaTime's
    refresh grant must send the same one used at authorize; rebuilding it from
    whichever host served a later read would fail when that host differs.
    """

    access_token: str
    refresh_token: str
    expires_at: int
    redirect_uri: str = ""


def build_authorize_url(redirect_uri: str) -> tuple[str, str]:
    """Return ``(url, state)`` for the authorize redirect.

    The caller stores ``state`` (in the session or a signed cookie) and checks
    it against the value WakaTime echoes to the callback. ``redirect_uri`` must
    match the one registered for the app and the one sent to the token endpoint.
    """
    client_id = (KiroCrewConfig.load().wakatime.oauth_client_id or "").strip()
    if not client_id:
        raise WakaTimeOAuthError("no OAuth client id configured", code="not_configured")
    state = secrets.token_urlsafe(32)
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": " ".join(DEFAULT_SCOPES),
            "state": state,
        }
    )
    return f"{AUTHORIZE_URL}?{query}", state


def _client_credentials() -> tuple[str, str]:
    """Return ``(client_id, client_secret)`` from config + vault.

    Raises ``not_configured`` when either is missing, so a half-set app does
    not produce a confusing provider error.
    """
    client_id = (KiroCrewConfig.load().wakatime.oauth_client_id or "").strip()
    secret = SecretVault(config_dir()).get(CRED_WAKATIME_OAUTH_CLIENT_SECRET)
    client_secret = secret.reveal() if secret is not None else ""
    if not client_id or not client_secret:
        raise WakaTimeOAuthError("OAuth client id or secret is not set", code="not_configured")
    return client_id, client_secret


async def _post_token(data: dict[str, str]) -> dict[str, Any]:
    """POST form data to the token endpoint and return the parsed body.

    Narrow error handling: a non-2xx or a transport failure raises
    ``WakaTimeOAuthError`` with a code, never leaks the provider body, and logs
    the status and exception type only (a token response can carry a secret).
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                TOKEN_URL,
                data=data,
                headers={"Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=_TOKEN_TIMEOUT_SECS),
            ) as resp:
                if not (200 <= resp.status < 300):
                    logger.warning("wakatime oauth exchange endpoint http=%s", resp.status)
                    raise WakaTimeOAuthError(
                        f"token endpoint returned http={resp.status}", code="exchange_failed"
                    )
                body = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        # A total-timeout raises asyncio.TimeoutError, which is NOT a ClientError;
        # catching both keeps a slow provider from escaping as an uncaught error
        # that would 500 the read instead of falling back to the API key.
        logger.warning(
            "wakatime oauth exchange endpoint transport error type=%s", type(exc).__name__
        )
        raise WakaTimeOAuthError("could not reach the token endpoint", code="unreachable") from exc
    except ValueError as exc:
        # A 2xx with a non-JSON body raises ValueError from resp.json; treat it
        # as a failed exchange rather than letting it bubble out as a 500.
        logger.warning("wakatime oauth exchange endpoint returned an unparseable body")
        raise WakaTimeOAuthError(
            "token endpoint returned an unparseable body", code="exchange_failed"
        ) from exc
    if not isinstance(body, dict):
        raise WakaTimeOAuthError(
            "token endpoint returned a non-object body", code="exchange_failed"
        )
    return body


def _tokens_from_body(body: dict[str, Any], redirect_uri: str) -> OAuthTokens:
    """Validate a token-endpoint body into an :class:`OAuthTokens`.

    WakaTime returns ``access_token``, ``refresh_token``, and ``expires_in``
    (seconds). A refresh response may omit ``refresh_token``; the caller carries
    the prior one forward in that case, so this reads what is present and lets
    the caller supply a fallback.
    """
    access = body.get("access_token")
    refresh = body.get("refresh_token")
    expires_in = body.get("expires_in")
    if not isinstance(access, str) or not access:
        raise WakaTimeOAuthError("token body has no access_token", code="exchange_failed")
    try:
        ttl = int(expires_in)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        # No expiry means the client cannot refresh ahead of a 401; treat the
        # token as already due so every read refreshes it first.
        ttl = 0
    expires_at = int(time.time()) + ttl if ttl > 0 else 0
    return OAuthTokens(
        access_token=access,
        refresh_token=refresh if isinstance(refresh, str) and refresh else "",
        expires_at=expires_at,
        redirect_uri=redirect_uri,
    )


async def exchange_code(code: str, redirect_uri: str) -> OAuthTokens:
    """Exchange an authorization code for tokens.

    ``redirect_uri`` must be the same value sent to the authorize step. The
    caller persists the result with :func:`store_tokens`.
    """
    # The config load and vault decrypt are blocking disk I/O; keep them off
    # the event loop.
    client_id, client_secret = await asyncio.to_thread(_client_credentials)
    body = await _post_token(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code": code,
        }
    )
    tokens = _tokens_from_body(body, redirect_uri)
    if not tokens.refresh_token:
        raise WakaTimeOAuthError("exchange returned no refresh_token", code="exchange_failed")
    return tokens


async def refresh_tokens(stored: OAuthTokens) -> OAuthTokens:
    """Refresh ``stored`` using its refresh token and its saved redirect URI.

    A refresh response may omit a new refresh token, in which case the stored
    one is carried forward. Raises ``not_configured`` when ``stored`` has no
    refresh token.
    """
    if not stored.refresh_token:
        raise WakaTimeOAuthError("no refresh token stored", code="not_configured")
    client_id, client_secret = await asyncio.to_thread(_client_credentials)
    body = await _post_token(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": stored.redirect_uri,
            "grant_type": "refresh_token",
            "refresh_token": stored.refresh_token,
        }
    )
    tokens = _tokens_from_body(body, stored.redirect_uri)
    if not tokens.refresh_token:
        tokens = OAuthTokens(
            access_token=tokens.access_token,
            refresh_token=stored.refresh_token,
            expires_at=tokens.expires_at,
            redirect_uri=stored.redirect_uri,
        )
    return tokens


async def revoke_tokens() -> None:
    """Revoke the stored access token at the provider, best effort.

    A failed revoke still proceeds to local deletion in :func:`clear_tokens`:
    the local copy must go even if the network call fails, so a disconnect is
    never left half done. This raises only on a configuration gap, not on a
    provider or transport failure.
    """
    access = await asyncio.to_thread(read_access_token)
    if not access:
        return
    try:
        client_id, client_secret = await asyncio.to_thread(_client_credentials)
    except WakaTimeOAuthError:
        return
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                REVOKE_URL,
                data={"client_id": client_id, "client_secret": client_secret, "token": access},
                timeout=aiohttp.ClientTimeout(total=_TOKEN_TIMEOUT_SECS),
            ) as resp:
                if not (200 <= resp.status < 300):
                    logger.warning("wakatime oauth revoke http=%s", resp.status)
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        logger.warning("wakatime oauth revoke transport error type=%s", type(exc).__name__)


def read_tokens() -> OAuthTokens | None:
    """Return the stored tokens, or ``None`` when absent or unreadable.

    A malformed entry reads as absent, so the integration falls back to the
    API key instead of failing every read.
    """
    try:
        value = SecretVault(config_dir()).get(CRED_WAKATIME_OAUTH_TOKENS)
    except Exception:
        return None
    if value is None:
        return None
    try:
        data = json.loads(value.reveal())
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    access = data.get("access_token")
    if not isinstance(access, str) or not access:
        return None
    refresh = data.get("refresh_token")
    redirect_uri = data.get("redirect_uri")
    expires_at = data.get("expires_at")
    return OAuthTokens(
        access_token=access,
        refresh_token=refresh if isinstance(refresh, str) else "",
        expires_at=expires_at if isinstance(expires_at, int) else 0,
        redirect_uri=redirect_uri if isinstance(redirect_uri, str) else "",
    )


def store_tokens(tokens: OAuthTokens) -> None:
    """Persist ``tokens`` as one vault entry, replacing any stored ones."""
    payload = json.dumps(
        {
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
            "expires_at": int(tokens.expires_at),
            "redirect_uri": tokens.redirect_uri,
        }
    )
    SecretVault(config_dir()).set_sync(CRED_WAKATIME_OAUTH_TOKENS, payload)


def clear_tokens() -> None:
    """Delete the stored tokens. Idempotent.

    Used on disconnect, after :func:`revoke_tokens`. Deletes the local copy
    even if the provider revoke failed, so a disconnect always ends with no
    local token.
    """
    SecretVault(config_dir()).delete_sync(CRED_WAKATIME_OAUTH_TOKENS)


def read_access_token() -> str:
    """Return the stored access token, or ``""`` when none is stored."""
    tokens = read_tokens()
    return tokens.access_token if tokens is not None else ""


def token_is_fresh(tokens: OAuthTokens) -> bool:
    """True when ``tokens`` has a known expiry that is not yet reached.

    An unknown expiry (0) is treated as not fresh, so the caller refreshes
    rather than sending a possibly-dead token into a read.
    """
    if tokens.expires_at <= 0:
        return False
    return time.time() < (tokens.expires_at - _EXPIRY_SKEW_SECS)
