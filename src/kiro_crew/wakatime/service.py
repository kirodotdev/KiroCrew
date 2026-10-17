"""Resolve WakaTime config + secret into a ready client.

The API key is read only from the encrypted vault. There is deliberately no
``.env``/environment fallback: a key placed in ``.env`` is loaded by the
gateway into the process environment, and the agent-spawn scrub strips only the
credential names it knows, so an unknown key can reach an agent subprocess and
be exfiltrated. Vault-only keeps the key off that path entirely.

The base URL comes from ``config.wakatime.api_base_url`` (empty = the public
WakaTime API), so a self-hosted Wakapi/Hackatime backend is a config change,
not a code change.
"""

from __future__ import annotations

import asyncio
import logging

from kiro_crew.config.loader import CRED_WAKATIME_API_KEY, KiroCrewConfig
from kiro_crew.config.paths import config_dir
from kiro_crew.secrets.vault import SecretVault
from kiro_crew.wakatime.client import DEFAULT_API_BASE, WakaTimeClient

logger = logging.getLogger(__name__)


def resolve_api_key() -> str:
    """Return the WakaTime API key from the vault, or ``""`` if not set.

    Best-effort: a missing or unreadable vault yields ``""`` (integration stays
    off) rather than raising.
    """
    try:
        secret = SecretVault(config_dir()).get(CRED_WAKATIME_API_KEY)
    except Exception:
        return ""
    return secret.reveal() if secret is not None else ""


def resolve_base_url(config: KiroCrewConfig | None = None) -> str:
    """Return the configured API base URL, or the public default."""
    cfg = config or KiroCrewConfig.load()
    configured = (cfg.wakatime.api_base_url or "").strip()
    return configured or DEFAULT_API_BASE


def build_client(
    config: KiroCrewConfig | None = None,
    *,
    oauth_access_token: str | None = None,
) -> WakaTimeClient | None:
    """Build a ready WakaTimeClient, or ``None`` when the integration is
    disabled or has no API key.

    Returning ``None`` (rather than raising) lets callers treat "not set up" as
    an ordinary empty state instead of an error path.

    ``oauth_access_token`` makes reads authenticate with a bearer token. The
    API key is still required and still carries the heartbeat-send path, which
    keeps the key after the read cutoff. OAuth-only (a token but no key) is not
    supported: the write path needs the key, so a client is built only when the
    key is present. A caller on the read path should pass the token from
    :func:`resolve_oauth_access_token`, which refreshes it ahead of its expiry;
    passing ``None`` leaves the client on the API key.
    """
    cfg = config or KiroCrewConfig.load()
    if not cfg.wakatime.enabled:
        return None
    api_key = resolve_api_key()
    if not api_key:
        return None
    return WakaTimeClient(
        api_key=api_key,
        api_base=resolve_base_url(cfg),
        oauth_access_token=oauth_access_token or None,
    )


async def resolve_oauth_access_token(
    redirect_uri: str, config: KiroCrewConfig | None = None
) -> str:
    """Return a usable OAuth access token, refreshing it first when it is due.

    Returns ``""`` when no token is stored (the integration stays on the API
    key) or when a refresh fails (the caller falls back to the key, which still
    works before the cutoff and for self-hosted backends). A fresh stored token
    is returned without a provider round trip; a due one is refreshed, stored,
    and the new token returned.

    ``redirect_uri`` must match the one used at authorize and exchange, because
    WakaTime's refresh grant requires it. Any failure degrades to ``""`` rather
    than raising: a read must not break because a refresh could not reach the
    provider.
    """
    try:
        from kiro_crew.wakatime import oauth
    except Exception:
        return ""
    if config is not None:
        cfg = config
    else:
        cfg = await asyncio.to_thread(KiroCrewConfig.load)
    # When the integration is off, build_client returns None regardless, so a
    # resolved token is never used. Bail before the vault read and the refresh:
    # otherwise a disabled integration with a stored stale token still POSTs the
    # client secret to WakaTime and rewrites config on every stats request.
    if not cfg.wakatime.enabled:
        return ""
    # The vault read and freshness check touch disk and decrypt, so run them off
    # the event loop. A stored-but-stale token takes the refresh branch.
    stored: str = await asyncio.to_thread(oauth.read_access_token)
    if not stored:
        return ""
    is_fresh: bool = await asyncio.to_thread(oauth.token_is_fresh, cfg)
    if is_fresh:
        return stored
    # Serialize the refresh+store so a concurrent disconnect cannot clear the
    # tokens between our refresh and our store (which would otherwise resurrect
    # them). Re-check freshness inside the lock: a sibling read may have already
    # refreshed while we waited, making another provider round trip wasteful.
    async with oauth.token_mutation_lock():
        fresh_token: str = await asyncio.to_thread(oauth.read_access_token)
        if not fresh_token:
            # Disconnected while we waited for the lock. Fall back to the key.
            return ""
        still_fresh: bool = await asyncio.to_thread(oauth.token_is_fresh, cfg)
        if still_fresh:
            return fresh_token
        try:
            tokens = await oauth.refresh_tokens(redirect_uri)
        except oauth.WakaTimeOAuthError:
            return ""
        await asyncio.to_thread(oauth.store_tokens, tokens)
        return tokens.access_token
