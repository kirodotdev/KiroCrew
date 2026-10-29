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
from kiro_crew.auth.whoami import recorded_start_url
from kiro_crew.config.paths import data_home

logger = logging.getLogger(__name__)


def default_token_store() -> TokenStore:
    """The gateway's own vault: one machine-global store under the data home."""
    return TokenStore(data_home())


def _usable(token: KasToken) -> bool:
    """Can this stored identity still produce an access token without a sign-in?

    :meth:`KasToken.is_usable` -- the predicate shared with ``kirocrew doctor``
    and the dashboard's sign-in card. Kept as a module function so the callers
    below read the same way they always did. What it cannot know without a
    network call is whether the refresh token is still accepted by the issuer; a
    rejected one surfaces as a failed callback (the engine's sign-in prompt), is
    recorded by the refresher (:meth:`TokenStore.refresh_rejected`), and shows in
    both ``kirocrew doctor`` and the sign-in card so it can be diagnosed before
    the first spawn. It deliberately does NOT feed this predicate: a lapsed Crew
    identity is told to the user, never silently swapped for kiro-cli's login.
    """
    return token.is_usable()


def vault_holds_identity(*, warn: bool = True) -> bool:
    """True when Crew's own vault stores a USABLE Kiro identity (of any stored kind).

    The spawn-time question the KAS relay asks before choosing its auth owner
    (:mod:`kiro_crew.acp.kas_host_auth`), also reported by ``kirocrew doctor``
    and asked by the dashboard's usage poll. A plain read of the vault -- no
    refresh, no network -- that never raises: a missing, unreadable, or linked
    vault directory is ``False`` (the unreadable case logged at WARNING), and so
    is an identity whose access token has expired with no refresh token to renew
    it -- that entry cannot answer a single callback, so handing the spawn to it
    would only shadow a working ``kiro-cli login``. Both degrade to the
    kiro-cli-owned spawn rather than failing the spawn. Blocking file IO -- call
    off the loop.

    ``warn=False`` logs those diagnostics at DEBUG instead of WARNING, for a
    caller that asks on a timer (the usage poll) where a spawn-time diagnostic
    would repeat on every tick; the verdict is the same either way.
    """
    log = logger.warning if warn else logger.debug
    try:
        token = default_token_store().resolve()
        if token is None:
            return False
        if not _usable(token):
            log(
                "Crew sign-in vault holds an expired %s identity with nothing to renew it; "
                "KAS spawns cli-owned (sign in again or sign out to clear it)",
                token.identity,
            )
            return False
        return True
    except TokenStoreError as exc:
        # `exc` is a store-level reason (a path or a permissions verdict), never
        # a stored value -- the store raises before decrypting anything.
        log("Crew sign-in vault unreadable; KAS spawns cli-owned: %s", exc)
        return False
    except Exception as exc:  # noqa: BLE001 - a vault probe must never fail a spawn
        log(
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


#: The two fingerprinted claims that name a KIND of account and nothing else:
#: the store slot the identity came from (``builder_id`` / ``identity_center`` /
#: ``external_idp`` / ``social``) and the governance classification (``BuilderId``
#: / ``Enterprise`` / ``ExternalIdp`` / ``Google`` / ``Github``). What
#: :func:`vault_identity_claims` returns in the clear: the ARN, region and client
#: registration stay inside the digest.
_VAULT_PUBLIC_CLAIMS = ("identity", "provider")


def _identity_digest(token: KasToken) -> str:
    """The identity fingerprint of one resolved token (see :data:`_VAULT_IDENTITY_CLAIMS`)."""
    digest = hashlib.sha256()
    for claim in _VAULT_IDENTITY_CLAIMS:
        digest.update(claim.encode())
        digest.update(b"=")
        digest.update(str(getattr(token, claim, None) or "").encode())
        digest.update(b"\n")
    return digest.hexdigest()


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
    return vault_identity_claims()[0]


def vault_identity_claims() -> tuple[str, dict[str, str]]:
    """ONE vault read: the identity fingerprint and the claims naming the account's kind.

    The dashboard labels a credit reading with the account it was read for, and
    it brackets the credential read with the fingerprint to prove no sign-in
    replaced that account meanwhile. Both halves come from the same ``resolve()``
    so the labels a caller publishes describe the fingerprinted account, never
    one that landed between a fingerprint read and a separate claims read. The
    claims are :data:`_VAULT_PUBLIC_CLAIMS` -- the slot and the provider class,
    which together say "a Builder ID sign-in", "a Google sign-in" -- plus the
    Identity Center ``start_url`` the sign-in recorded, when it recorded one, and
    no secret, ARN or registration detail. ``("", {})`` when nothing is stored or
    the vault cannot be read; never raises. Blocking file IO -- call off the loop.
    """
    try:
        token = default_token_store().resolve()
    except Exception:  # noqa: BLE001 - detection must never fail a turn
        return "", {}
    if token is None:
        return "", {}
    claims = {
        claim: value
        for claim in _VAULT_PUBLIC_CLAIMS
        if isinstance(value := getattr(token, claim, None), str) and value
    }
    # The Identity Center portal the sign-in was begun against, when it recorded
    # one: the same field ``kirocrew kas whoami`` prints as ``startUrl``. A portal
    # URL, not a secret, and outside the fingerprint (a token minted before the
    # URL was recorded is the same account as one minted after).
    start_url = recorded_start_url(token)
    if start_url:
        claims["start_url"] = start_url
    return _identity_digest(token), claims


def describe_vault_identity() -> str | None:
    """One token-free line about the stored identity, for ``kirocrew doctor``.

    Names the identity kind and provider, whether the access token is inside the
    engine's refresh margin, and whether a refresh token is present -- the two
    fields :func:`vault_holds_identity` decides on -- so a vault that will fail
    its first callback is diagnosable before the first spawn. ``None`` when
    nothing is stored or the vault cannot be read.
    """
    try:
        store = default_token_store()
        token = store.resolve()
    except Exception:  # noqa: BLE001 - diagnostics never raise
        return None
    if token is None:
        return None
    remaining = token.expires_at - datetime.now(timezone.utc)
    minutes = int(remaining.total_seconds() // 60)
    expiry = f"expires in {minutes}m" if minutes > 0 else "access token expired"
    renew = "refresh token present" if token.refresh_token else "no refresh token"
    rejected = store.refresh_rejected(token.identity)
    if rejected is not None:
        # The issuer refused the refresh: the entry looks renewable but is not.
        # Say so (the dashboard card says the same), and name the remedy -- the
        # spawn still goes to this identity, so a failing callback is what the
        # user sees until they act.
        renew += f", refresh REJECTED by issuer at {rejected.isoformat(timespec='minutes')}"
        verdict = "sign-in expired -- sign in again from the dashboard or sign out"
    else:
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
