"""WakaTime OAuth 2.0: authorize, exchange, refresh, revoke, and token storage.

WakaTime makes API keys write-only on 1 November 2026, so reading stats and
summaries needs an OAuth access token. This module owns the authorization-code
flow and where the resulting credentials live.

The split follows the secret boundary. The client ID and the access token's
expiry are not secrets and sit in ``config.wakatime``. The client secret and
the access and refresh tokens are secrets and live in the encrypted vault, next
to ``WAKATIME_API_KEY``, for the reason ``service.resolve_api_key`` already
records: a value in ``.env`` is loaded into the process environment, and the
agent-spawn scrub strips only credential names it knows.

The provider endpoints are WakaTime's documented OAuth routes. The ``state``
value is minted per authorize request and checked on the callback, so a
forged callback with a stolen code cannot complete the exchange.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

import aiohttp

from kiro_crew.config.loader import (
    CRED_WAKATIME_OAUTH_ACCESS_TOKEN,
    CRED_WAKATIME_OAUTH_CLIENT_SECRET,
    CRED_WAKATIME_OAUTH_REFRESH_TOKEN,
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
# read cannot interleave with a disconnect running for the owner. Without it a
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

    Both the read path (refresh then store) and the disconnect path (revoke
    then clear) take this lock, so the two never interleave on the stored
    credentials.
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
    """A token-endpoint result: the two tokens and an absolute expiry.

    ``expires_at`` is epoch seconds, derived from the provider's ``expires_in``
    at exchange time, so a later caller compares against the clock rather than
    tracking a relative countdown.
    """

    access_token: str
    refresh_token: str
    expires_at: int


def build_authorize_url(
    redirect_uri: str, *, scopes: tuple[str, ...] = DEFAULT_SCOPES
) -> tuple[str, str]:
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
            "scope": " ".join(scopes),
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


def _tokens_from_body(body: dict[str, Any]) -> OAuthTokens:
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
        # token as already due so the 401 path is the only backstop.
        ttl = 0
    expires_at = int(time.time()) + ttl if ttl > 0 else 0
    return OAuthTokens(
        access_token=access,
        refresh_token=refresh if isinstance(refresh, str) and refresh else "",
        expires_at=expires_at,
    )


async def exchange_code(code: str, redirect_uri: str) -> OAuthTokens:
    """Exchange an authorization code for tokens.

    ``redirect_uri`` must be the same value sent to the authorize step. The
    caller persists the result with :func:`store_tokens`.
    """
    client_id, client_secret = _client_credentials()
    body = await _post_token(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code": code,
        }
    )
    tokens = _tokens_from_body(body)
    if not tokens.refresh_token:
        raise WakaTimeOAuthError("exchange returned no refresh_token", code="exchange_failed")
    return tokens


async def refresh_tokens(redirect_uri: str) -> OAuthTokens:
    """Refresh the stored access token using the stored refresh token.

    A refresh response may omit a new refresh token, in which case the stored
    one is carried forward. Raises ``not_configured`` when no refresh token is
    stored.
    """
    client_id, client_secret = _client_credentials()
    stored_refresh = _read_secret(CRED_WAKATIME_OAUTH_REFRESH_TOKEN)
    if not stored_refresh:
        raise WakaTimeOAuthError("no refresh token stored", code="not_configured")
    body = await _post_token(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "refresh_token",
            "refresh_token": stored_refresh,
        }
    )
    tokens = _tokens_from_body(body)
    if not tokens.refresh_token:
        tokens = OAuthTokens(
            access_token=tokens.access_token,
            refresh_token=stored_refresh,
            expires_at=tokens.expires_at,
        )
    return tokens


async def revoke_tokens() -> None:
    """Revoke the stored access token at the provider, best effort.

    A failed revoke still proceeds to local deletion in :func:`clear_tokens`:
    the local copy must go even if the network call fails, so a disconnect is
    never left half done. This raises only on a configuration gap, not on a
    provider or transport failure.
    """
    access = _read_secret(CRED_WAKATIME_OAUTH_ACCESS_TOKEN)
    if not access:
        return
    try:
        client_id, client_secret = _client_credentials()
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


def _read_secret(name: str) -> str:
    """Read one vault secret by name, or ``""`` when absent/unreadable."""
    try:
        value = SecretVault(config_dir()).get(name)
    except Exception:
        return ""
    return value.reveal() if value is not None else ""


def store_tokens(tokens: OAuthTokens) -> None:
    """Persist tokens: the two secrets to the vault, the expiry to config.

    The access and refresh tokens are secrets and go in the vault. The expiry
    is not a secret and is written to ``config.wakatime.oauth_token_expires_at``
    so a later read can refresh before the boundary rather than after a 401.
    """
    vault = SecretVault(config_dir())
    vault.set_sync(CRED_WAKATIME_OAUTH_ACCESS_TOKEN, tokens.access_token)
    vault.set_sync(CRED_WAKATIME_OAUTH_REFRESH_TOKEN, tokens.refresh_token)
    _write_expiry(tokens.expires_at)


def clear_tokens() -> None:
    """Delete the stored tokens and reset the expiry. Idempotent.

    Used on disconnect, after :func:`revoke_tokens`. Deletes the local copy
    even if the provider revoke failed, so a disconnect always ends with no
    local token.
    """
    vault = SecretVault(config_dir())
    vault.delete_sync(CRED_WAKATIME_OAUTH_ACCESS_TOKEN)
    vault.delete_sync(CRED_WAKATIME_OAUTH_REFRESH_TOKEN)
    _write_expiry(0)


def _write_expiry(expires_at: int) -> None:
    """Write the access-token expiry to config under the config write lock.

    Goes through ``update_config_locked`` rather than a bare ``load``/``save``:
    ``save`` publishes a whole in-memory snapshot and can overwrite a concurrent
    writer's change, while this read-modify-write touches only the one field
    inside the lock. A failure is logged, not raised: the tokens are already in
    the vault, and a stale expiry costs only one 401-then-refresh, which the
    client handles.
    """
    from kiro_crew.config.loader import config_path, update_config_locked

    def _mutate(data: dict[str, Any]) -> dict[str, Any]:
        section = dict(data.get("wakatime") or {})
        section["oauth_token_expires_at"] = int(expires_at)
        data["wakatime"] = section
        return data

    try:
        update_config_locked(config_path(), mutate=_mutate)
    except Exception as exc:
        logger.warning("wakatime oauth expiry persist failed type=%s", type(exc).__name__)


def read_access_token() -> str:
    """Return the stored access token, or ``""`` when none is stored."""
    return _read_secret(CRED_WAKATIME_OAUTH_ACCESS_TOKEN)


def token_is_fresh(config: KiroCrewConfig | None = None) -> bool:
    """True when a stored access token exists and has not reached its expiry.

    A stored token with an unknown expiry (0) is treated as not fresh, so the
    caller refreshes rather than sending a possibly-dead token into a read.
    """
    if not read_access_token():
        return False
    cfg = config or KiroCrewConfig.load()
    expires_at = int(cfg.wakatime.oauth_token_expires_at or 0)
    if expires_at <= 0:
        return False
    return time.time() < (expires_at - _EXPIRY_SKEW_SECS)
