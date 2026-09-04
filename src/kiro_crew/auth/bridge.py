"""KAS integration seam.

Where the auth module meets the embedded KAS engine. KAS consumes auth one of two ways
(see docs/system-specs/modules/kas-auth.md); this module exposes both against a single
``KasAuthProvider``:

- ``handle_get_access_token(...)`` — answers the ``_kiro/auth/getAccessToken``
  acp-callback KAS raises when it is driven over ACP.
- ``as_iauthprovider(...)`` — a dict of async callables shaped like KAS's
  ``IAuthProvider`` (getToken / getProfileArn / isAuthenticated / readToken /
  resolveRequestCredential), for the in-process library injection path
  (``KiroAgentOptions.authProvider``).

The acp-callback half is live: :mod:`kiro_crew.acp.kas_host_auth` calls
``handle_get_access_token`` when the KAS relay is spawned with Crew as auth owner.
The library-injection half has no consumer in this tree yet; it is kept real and
tested so an embedded-KAS host later is a call, not a rewrite.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

from kiro_crew.auth.provider import KasAuthProvider
from kiro_crew.auth.store import KasToken, TokenStore, TokenStoreError
from kiro_crew.config.paths import data_home

logger = logging.getLogger(__name__)


def default_token_store() -> TokenStore:
    """The gateway's own vault: one machine-global store under the data home."""
    return TokenStore(data_home())


def _usable(token: KasToken) -> bool:
    """Can this stored identity still produce an access token without a sign-in?

    Either the access token is outside the engine's refresh margin, or a refresh
    token is present to renew it. What this cannot know without a network call is
    whether the refresh token is still accepted by the issuer; a persistently
    rejected one surfaces as a failed callback (the engine's sign-in prompt) and
    ``kirocrew doctor`` shows the same fields so it can be diagnosed before the
    first spawn.
    """
    return (not token.is_expired()) or bool(token.refresh_token)


def vault_holds_identity() -> bool:
    """True when Crew's own vault stores a USABLE Kiro identity (of any stored kind).

    The spawn-time question the KAS relay asks before choosing its auth owner
    (:mod:`kiro_crew.acp.kas_host_auth`), also reported by ``kirocrew doctor``.
    A plain read of the vault -- no refresh, no network -- that never raises: a
    missing, unreadable, or linked vault directory is ``False`` (the unreadable
    case logged at WARNING), and so is an identity whose access token has expired
    with no refresh token to renew it -- that entry cannot answer a single
    callback, so handing the spawn to it would only shadow a working ``kiro-cli
    login``. Both degrade to the kiro-cli-owned spawn rather than failing the
    spawn. Blocking file IO -- call off the loop.
    """
    try:
        token = default_token_store().resolve()
        if token is None:
            return False
        if not _usable(token):
            logger.warning(
                "Crew sign-in vault holds an expired %s identity with nothing to renew it; "
                "KAS spawns cli-owned (sign in again or sign out to clear it)",
                token.identity,
            )
            return False
        return True
    except TokenStoreError as exc:
        # `exc` is a store-level reason (a path or a permissions verdict), never
        # a stored value -- the store raises before decrypting anything.
        logger.warning("Crew sign-in vault unreadable; KAS spawns cli-owned: %s", exc)
        return False
    except Exception as exc:  # noqa: BLE001 - a vault probe must never fail a spawn
        logger.warning(
            "Crew sign-in vault probe failed (%s); KAS spawns cli-owned",
            type(exc).__name__,
        )
        return False


#: The stored-identity fields that name WHICH account the vault is signed in as.
#: An allowlist, mirroring :func:`kiro_crew.kiro_prerequisite.identity_fingerprint`
#: for kiro-cli's store: rotating and secret fields (access token, refresh token,
#: expiry, client secret) are excluded so a refresh never reads as an account
#: change, and a field added to the token later cannot join by default.
_VAULT_IDENTITY_CLAIMS = (
    "identity",
    "provider",
    "profile_arn",
    "region",
    "client_id",
    "token_endpoint",
    "auth_method",
)


def vault_identity_fingerprint() -> str:
    """Digest naming which account Crew's own vault is signed in as, or ``""``.

    The vault's contribution to the gateway's identity-change detection
    (``KiroPrerequisiteService.current_identity_fingerprint``): a Crew sign-out
    or account switch changes this value, which makes the per-turn sweep retire
    running KAS processes that loaded the previous identity -- and keep retrying
    until the sweep completes, exactly as it does for a ``kiro-cli logout``.
    Values are hashed, never returned; the read never raises (an unreadable or
    empty vault is ``""``, which the caller treats as "no Crew identity").
    Blocking file IO -- call off the loop.
    """
    try:
        token = default_token_store().resolve()
    except Exception:  # noqa: BLE001 - detection must never fail a turn
        return ""
    if token is None:
        return ""
    digest = hashlib.sha256()
    for claim in _VAULT_IDENTITY_CLAIMS:
        digest.update(claim.encode())
        digest.update(b"=")
        digest.update(str(getattr(token, claim, None) or "").encode())
        digest.update(b"\n")
    return digest.hexdigest()


def describe_vault_identity() -> str | None:
    """One token-free line about the stored identity, for ``kirocrew doctor``.

    Names the identity kind and provider, whether the access token is inside the
    engine's refresh margin, and whether a refresh token is present -- the two
    fields :func:`vault_holds_identity` decides on -- so a vault that will fail
    its first callback is diagnosable before the first spawn. ``None`` when
    nothing is stored or the vault cannot be read.
    """
    try:
        token = default_token_store().resolve()
    except Exception:  # noqa: BLE001 - diagnostics never raise
        return None
    if token is None:
        return None
    remaining = token.expires_at - datetime.now(timezone.utc)
    minutes = int(remaining.total_seconds() // 60)
    expiry = f"expires in {minutes}m" if minutes > 0 else "access token expired"
    renew = "refresh token present" if token.refresh_token else "no refresh token"
    verdict = "usable" if _usable(token) else "NOT usable -- sign in again or sign out"
    return f"{token.identity}/{token.provider}, {expiry}, {renew} -> {verdict}"


async def handle_get_access_token(provider: KasAuthProvider) -> dict:
    """Answer a ``_kiro/auth/getAccessToken`` acp-callback (empty request body)."""
    return await provider.get_access_token_callback()


def as_iauthprovider(provider: KasAuthProvider) -> dict:
    """Return an IAuthProvider-shaped mapping for KAS library injection.

    Keys mirror the TypeScript ``IAuthProvider`` method names KAS calls. The bridge that
    embeds KAS adapts this mapping to the JS object KAS expects; keeping it a plain dict
    here means the auth module has no build-time dependency on the KAS runtime.
    """
    return {
        "getToken": provider.get_token,
        "getProfileArn": provider.get_profile_arn,
        "isAuthenticated": provider.is_authenticated,
        "readToken": provider.read_token,
        "resolveRequestCredential": provider.resolve_request_credential,
    }
