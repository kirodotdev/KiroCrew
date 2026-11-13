"""Kiro auth endpoints and wire constants (mirrors kiro-cli auth/consts.rs).

Both hosts are overridable so an enterprise mirror or a test double can be pointed at:
the portal via ``KIRO_AUTH_PORTAL_URL``, the social service via ``KIRO_AUTH_SERVICE_URL``
(kiro-cli reads the latter from a DB setting; we take it from the environment).
"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

# Default hosts (kiro-cli consts.rs).
_DEFAULT_PORTAL = "https://app.kiro.dev"
_DEFAULT_SOCIAL_SERVICE = "https://prod.us-east-1.auth.desktop.kiro.dev"

# The only client identifier the social path sends. Not a secret; it is the
# User-Agent kiro-cli uses, and the portal accepts any caller presenting it.
USER_AGENT = "Kiro-CLI"

# Cognito-allowlisted loopback callback ports (kiro-cli oauth_callback.rs CALLBACK_PORTS).
# The auth service only accepts these pre-registered redirect URIs — do not change
# without auth-service coordination.
CALLBACK_PORTS: tuple[int, ...] = (
    3128,
    4649,
    6588,
    8008,
    9091,
    49153,
    50153,
    51153,
    52153,
    53153,
)

# AWS SSO-OIDC (Builder ID / IAM Identity Center).
BUILDER_ID_REGION = "us-east-1"
BUILDER_ID_START_URL = "https://view.awsapps.com/start"
AMZN_START_URL = "https://amzn.awsapps.com/start"
OIDC_SCOPES = (
    "codewhisperer:completions",
    "codewhisperer:analysis",
    "codewhisperer:conversations",
)
# A non-production control plane can list profiles only for scopes under its own
# prefix. ``KIRO_OIDC_SCOPE_PREFIX`` swaps the prefix of every scope above, like
# kiro-cli's ``api.oidc.scopePrefix`` setting. Unset, OIDC_SCOPES are sent as is.
OIDC_SCOPE_PREFIX_ENV = "KIRO_OIDC_SCOPE_PREFIX"
_PUBLIC_SCOPE_PREFIX = "codewhisperer"
_SCOPE_PREFIX_RE = re.compile(r"[a-z_]{1,64}")
_warned_prefixes: set[str] = set()
# KasToken.extra key recording a registration's scopes when they are not OIDC_SCOPES,
# so a later sign-in reuses that registration only for the same scopes.
OIDC_SCOPES_EXTRA_KEY = "oidc_scopes"
# client_name we register our own public OAuth client under (kiro-cli uses "Kiro CLI").
OIDC_CLIENT_NAME = "Kiro Crew"

# Device-flow fallback when the poll response omits an expiry (kiro-cli uses 3600).
DEFAULT_TOKEN_TTL_SECS = 3600


def oidc_scopes() -> tuple[str, ...]:
    """The scopes an SSO-OIDC client registers for: OIDC_SCOPES under the configured prefix.

    A ``KIRO_OIDC_SCOPE_PREFIX`` that is not lowercase letters and underscores is
    ignored, with one warning per distinct value, so it cannot add or split a scope.
    """
    prefix = os.environ.get(OIDC_SCOPE_PREFIX_ENV, "").strip()
    if not prefix:
        return OIDC_SCOPES
    if not _SCOPE_PREFIX_RE.fullmatch(prefix):
        if prefix not in _warned_prefixes:
            _warned_prefixes.add(prefix)
            logger.warning("%s ignored: %r is not a scope prefix", OIDC_SCOPE_PREFIX_ENV, prefix)
        return OIDC_SCOPES
    return tuple(prefix + s[len(_PUBLIC_SCOPE_PREFIX) :] for s in OIDC_SCOPES)


def portal_url() -> str:
    return os.environ.get("KIRO_AUTH_PORTAL_URL", _DEFAULT_PORTAL).rstrip("/")


def social_service_url() -> str:
    return os.environ.get("KIRO_AUTH_SERVICE_URL", _DEFAULT_SOCIAL_SERVICE).rstrip("/")


def oidc_url(region: str) -> str:
    return f"https://oidc.{region}.amazonaws.com"
