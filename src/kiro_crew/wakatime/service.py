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

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.paths import config_dir
from kiro_crew.secrets.vault import SecretVault
from kiro_crew.wakatime.client import DEFAULT_API_BASE, WakaTimeClient

logger = logging.getLogger(__name__)

# Credential name in the vault (the dashboard secrets store).
WAKATIME_API_KEY = "WAKATIME_API_KEY"


def resolve_api_key() -> str:
    """Return the WakaTime API key from the vault, or ``""`` if not set.

    Best-effort: a missing or unreadable vault yields ``""`` (integration stays
    off) rather than raising.
    """
    try:
        secret = SecretVault(config_dir()).get(WAKATIME_API_KEY)
    except Exception:
        return ""
    if secret is None:
        return ""
    return secret.reveal()


def resolve_base_url(config: KiroCrewConfig | None = None) -> str:
    """Return the configured API base URL, or the public default."""
    cfg = config or KiroCrewConfig.load()
    configured = (cfg.wakatime.api_base_url or "").strip()
    return configured or DEFAULT_API_BASE


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
