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

import logging
from urllib.parse import urlsplit

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
    """Return a vetted API base URL, or the public default.

    The API key is attached as Basic auth to every request against this base
    URL, and ``config.json`` is agent-writable, so an unvetted base URL is a
    path for a config rewrite to post the vault key to an attacker endpoint.
    A configured value is therefore honored only when it is safe:

    * empty -> the public WakaTime API (the common case),
    * an ``https`` URL whose host is ``wakatime.com`` (or a subdomain),
    * an ``https`` URL to any host ONLY when ``allow_self_hosted`` is set, a
      deliberate opt-in for a trusted self-hosted backend.

    Anything else (a non-https scheme, a non-wakatime host without the opt-in,
    an unparseable value) falls back to the public default rather than sending
    the key onward.
    """
    cfg = config or KiroCrewConfig.load()
    configured = (cfg.wakatime.api_base_url or "").strip()
    if not configured:
        return DEFAULT_API_BASE
    try:
        parsed = urlsplit(configured)
    except ValueError:
        return DEFAULT_API_BASE
    if parsed.scheme != "https" or not parsed.hostname:
        return DEFAULT_API_BASE
    host = parsed.hostname.lower()
    is_wakatime = host == "wakatime.com" or host.endswith(".wakatime.com")
    if is_wakatime or cfg.wakatime.allow_self_hosted:
        return configured
    return DEFAULT_API_BASE


def build_client(config: KiroCrewConfig | None = None) -> WakaTimeClient | None:
    """Build a ready WakaTimeClient, or ``None`` when the integration is
    disabled or has no API key.

    Returning ``None`` (rather than raising) lets callers treat "not set up" as
    an ordinary empty state instead of an error path.
    """
    cfg = config or KiroCrewConfig.load()
    if not cfg.wakatime.enabled:
        return None
    api_key = resolve_api_key()
    if not api_key:
        return None
    return WakaTimeClient(api_key=api_key, api_base=resolve_base_url(cfg))
