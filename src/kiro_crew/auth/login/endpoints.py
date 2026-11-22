"""Kiro auth endpoints and wire constants (mirrors kiro-cli auth/consts.rs).

Both hosts are overridable so an enterprise mirror or a test double can be pointed at:
the portal via ``KIRO_AUTH_PORTAL_URL``, the social service via ``KIRO_AUTH_SERVICE_URL``
(kiro-cli reads the latter from a DB setting; we take it from the environment).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

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

#: The Feature Preview (``dashboard.feature_previews.crew_client``, default off) that
#: lets an edition give a start URL its own sign-in profile. Off, none is read.
CREW_CLIENT_PREVIEW = "crew_client"

#: Why no local KAS agent runs while the stored sign-in is on an edition's own start
#: URL: the spawn and the turn gate both say it.
LOCAL_AGENT_UNAVAILABLE = (
    "You can't chat with the agent on this computer while you're signed in with this "
    "company sign-in. Sign in with another account under Settings > Kiro account to "
    "chat again."
)


@dataclass(frozen=True)
class IdcProfile:
    """One IAM Identity Center start URL's own client registration, from an edition."""

    scopes: tuple[str, ...]
    grant_types: tuple[str, ...]
    issuer_url: str
    profile_arn: str = ""


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(v for v in value if isinstance(v, str) and v)


def _key(start_url: str | None) -> str:
    return (start_url or "").strip().rstrip("/")


def edition_idc_profiles() -> dict[str, IdcProfile]:
    """The edition's ``DashboardContributor.idc_sign_in_profiles()``, by start URL.

    ``{}`` while the ``crew_client`` preview is off (the edition is not asked), and
    from the public Default. An entry without scopes, grant types or an issuer URL
    is dropped; a contributor that raises is logged and read as ``{}``, so every
    start URL keeps the public sign-in. Blocking (reads config); never raises.
    """
    from kiro_crew.edition_locks import preview_on

    if not preview_on(CREW_CLIENT_PREVIEW):
        return {}
    from kiro_crew.platform import safe_context_call
    from kiro_crew.platform.context import installed_context

    ctx = installed_context()
    if ctx is None:
        return {}
    raw = safe_context_call(
        lambda: ctx.dashboard.idc_sign_in_profiles(),
        fallback=None,
        log_message="dashboard.idc_sign_in_profiles failed; no edition sign-in profile",
    )
    if not isinstance(raw, dict):
        return {}
    out: dict[str, IdcProfile] = {}
    for url, entry in raw.items():
        if not isinstance(url, str) or not _key(url) or not isinstance(entry, dict):
            continue
        scopes, grants = _strings(entry.get("scopes")), _strings(entry.get("grant_types"))
        issuer, arn = entry.get("issuer_url"), entry.get("profile_arn")
        if scopes and grants and isinstance(issuer, str) and issuer:
            out[_key(url)] = IdcProfile(scopes, grants, issuer, arn if isinstance(arn, str) else "")
    return out


def idc_profile(start_url: str | None) -> IdcProfile | None:
    """The edition's profile for *start_url*, or ``None`` for the public sign-in."""
    return edition_idc_profiles().get(_key(start_url))


def stored_sign_in_registered() -> bool:
    """Whether Crew's stored sign-in is on a start URL with an edition profile.

    No local agent may run on such a sign-in. While no profile applies (the preview
    off, or the public build) the vault is not read. Blocking; never raises: an
    unreadable vault is not a registered sign-in.
    """
    profiles = edition_idc_profiles()
    if not profiles:
        return False
    from kiro_crew.auth.bridge import vault_identity_claims

    return _key(vault_identity_claims()[1].get("start_url")) in profiles


def portal_url() -> str:
    return os.environ.get("KIRO_AUTH_PORTAL_URL", _DEFAULT_PORTAL).rstrip("/")


def social_service_url() -> str:
    return os.environ.get("KIRO_AUTH_SERVICE_URL", _DEFAULT_SOCIAL_SERVICE).rstrip("/")


def oidc_url(region: str) -> str:
    return f"https://oidc.{region}.amazonaws.com"
