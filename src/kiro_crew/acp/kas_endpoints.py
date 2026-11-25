"""Resolve the bearer-carrying BFF endpoint for Crew's KAS bundle."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

CLOUD_CONFIG_ENDPOINT_ENV = "CLOUD_CONFIG_ENDPOINT"
_DEFAULT_BFF_ENDPOINT = "https://app.kiro.dev"
_STAGE_BFF_ENDPOINTS = ("https://gamma.app.kiro.dev", "https://beta.app.kiro.dev")
_TRUSTED_BFF_HOSTS = ("app.kiro.dev", "gamma.app.kiro.dev", "beta.app.kiro.dev")
_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")


def _allowed_endpoint(endpoint: str) -> bool:
    # URL parsers disagree on backslashes and stripped control characters.
    if "\\" in endpoint or any(ord(char) <= 32 or ord(char) == 127 for char in endpoint):
        return False
    try:
        parsed = urlsplit(endpoint)
        if parsed.username is not None or parsed.password is not None:
            return False
        if parsed.port is not None and not 0 < parsed.port <= 65535:
            return False
        if parsed.scheme == "https":
            return parsed.hostname in (*_TRUSTED_BFF_HOSTS, *_LOOPBACK_HOSTS)
        return parsed.scheme == "http" and parsed.hostname in _LOOPBACK_HOSTS
    except ValueError:
        return False


def resolve_cloud_config_endpoint(environ: Mapping[str, str]) -> str:
    """Choose a trusted override, or the BFF matching the sign-in portal stage."""
    override = environ.get(CLOUD_CONFIG_ENDPOINT_ENV, "").strip()
    if override:
        if _allowed_endpoint(override):
            return override
        # Rejected URLs can hold credentials or signed queries; log no part of them.
        logger.warning(
            "Ignoring %s: expected a trusted Kiro BFF host over HTTPS or local loopback",
            CLOUD_CONFIG_ENDPOINT_ENV,
        )
    portal = environ.get("KIRO_AUTH_PORTAL_URL", "").strip().rstrip("/")
    if portal in _STAGE_BFF_ENDPOINTS:
        return portal
    return _DEFAULT_BFF_ENDPOINT
