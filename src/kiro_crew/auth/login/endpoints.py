"""Kiro auth endpoints and wire constants (mirrors kiro-cli auth/consts.rs).

Both hosts are overridable so an enterprise mirror or a test double can be pointed at:
the portal via ``KIRO_AUTH_PORTAL_URL``, the social service via ``KIRO_AUTH_SERVICE_URL``
(kiro-cli reads the latter from a DB setting; we take it from the environment).
"""

from __future__ import annotations

import os
import re

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
# A scope prefix (``tunnel.relay_viewer_oidc_scope_prefix``, like kiro-cli's
# ``api.oidc.scopePrefix``): lower-case letters and underscores, so it can never add
# a scope or reach past the part before the colon.
OIDC_SCOPE_PREFIX_RE = re.compile(r"[a-z][a-z_]{0,62}")


def oidc_scopes(prefix: str = "") -> tuple[str, ...]:
    """OIDC_SCOPES, or the same scopes under *prefix* when it is a valid one.

    Empty or invalid answers OIDC_SCOPES, so a bad value can only keep the default.
    """
    if not prefix or OIDC_SCOPE_PREFIX_RE.fullmatch(prefix) is None:
        return OIDC_SCOPES
    return tuple(f"{prefix}:{s.partition(':')[2]}" for s in OIDC_SCOPES)


# KasToken.extra key recording a registration's scopes when they are not OIDC_SCOPES,
# so a later sign-in reuses that registration only for the same scopes.
OIDC_SCOPES_EXTRA_KEY = "oidc_scopes"
# client_name we register our own public OAuth client under (kiro-cli uses "Kiro CLI").
OIDC_CLIENT_NAME = "Kiro Crew"

# Device-flow fallback when the poll response omits an expiry (kiro-cli uses 3600).
DEFAULT_TOKEN_TTL_SECS = 3600


def portal_url() -> str:
    return os.environ.get("KIRO_AUTH_PORTAL_URL", _DEFAULT_PORTAL).rstrip("/")


def social_service_url() -> str:
    return os.environ.get("KIRO_AUTH_SERVICE_URL", _DEFAULT_SOCIAL_SERVICE).rstrip("/")


def oidc_url(region: str) -> str:
    return f"https://oidc.{region}.amazonaws.com"
