"""Kiro Crew's OWN Kiro credential, as a credit-usage source.

Why this is a separate module
-----------------------------
``kiro_usage_api`` reads credentials that belong to SOMEBODY ELSE -- kiro-cli's
auth store, the Kiro IDE's SSO cache, amazon-q's store. It reads them as a
bystander: it deliberately does not refresh them ("a refresh belongs to whoever
owns the sign-in"), so a stored token is usable only for as long as its owner
keeps renewing it. That module's own docstring names the consequence:

    on an install where the sign-in is owned elsewhere and kiro-cli is never
    invoked directly, the stored token passes its expiry with nothing to renew
    it. ``_unexpired`` then drops it, no credential is left to try, and this
    module fails closed forever.

In KAS mode that install shape is the NORMAL one: Kiro Crew performs the entire
Kiro OIDC lifecycle itself (``kiro_crew.auth``, kiro-cli parity per
``docs/system-specs/modules/kas-auth.md``) and there may be no kiro-cli at all.
The credential for that identity has an owner who is present and refreshing --
this process -- so it is the one source that can answer when every bystander
store has lapsed.

It lives here rather than in ``kiro_usage_api`` for two reasons. That module is
SYNCHRONOUS by contract (it runs on the subprocess executor because urllib
blocks), while resolving this credential is async and does file IO plus a
possible refresh round-trip. And it must not import ``kiro_crew.auth``, which
pulls in the cryptography stack that the boot path and every non-KAS install
deliberately do not pay for -- the same reason ``acp.kas_host_auth`` defers that
import too.

What this is NOT
----------------
Not a replacement for anything. It adds a candidate to the front of the list
``kiro_usage_api`` already walks; every enumerated store and the ``/usage`` text
scrape stay exactly where they were, so an install whose sign-in kiro-cli owns
reads precisely what it read before this module existed. Users have not been
migrated to Kiro Crew's own OIDC, and nothing here assumes they have.
"""

from __future__ import annotations

import asyncio
import functools
import logging

from kiro_crew.dashboard.handlers import kiro_usage_api
from kiro_crew.executors import subprocess_executor

logger = logging.getLogger(__name__)

# Crew's stored identity KIND -> the spelling kiro-cli's ``whoami`` emits for the
# same account, which is the vocabulary the dashboard's account panel already
# branches on (``accountProviderLabel`` in ``KiroAccountModal.tsx``).
#
# Taken from the CLI source rather than guessed: ``WhoamiArgs::execute`` in
# ``crates/chat-cli/src/cli/user.rs`` (kiro-team/kiro-cli) emits exactly
# ``BuilderId`` / ``IamIdentityCenter`` for the two BuilderIdToken kinds,
# ``Social`` for a social sign-in -- with the issuer in a SEPARATE ``provider``
# member, not folded into the type -- and ``ExternalIdP`` for external IdP.
#
# Translating instead of inventing a second vocabulary is the whole point: the
# panel must not have to know which credential answered in order to label the
# account. A kind absent from this table yields no label rather than a guessed
# one -- the panel renders the name alone, which is what it did before.
_ACCOUNT_TYPE_BY_IDENTITY = {
    "builder_id": "BuilderId",
    "identity_center": "IamIdentityCenter",
    # Capital P, matching the CLI. The panel humanizes an unknown type by
    # splitting on case, so this spelling renders "External IdP" where
    # ``ExternalIdp`` would render "External Idp".
    "external_idp": "ExternalIdP",
    # Bare, for the same reason: whoami reports the issuer alongside the type,
    # never inside it. Appending it here would produce a label
    # (``SocialGoogle``) that no kiro-cli install ever emits, so the panel's
    # social branch would be reached with a value only this code can produce.
    "social": "Social",
}


def _account_type(identity: str | None, provider: str | None) -> str | None:
    """Label Crew's stored identity the way ``whoami`` would label it.

    ``provider`` is accepted and deliberately unused for the type: the CLI keeps
    the social issuer in its own output member, so folding it in would invent a
    spelling. It stays in the signature because the caller holds both halves of
    the identity and a future member for the issuer belongs here, not at a new
    call site.

    ``None`` when the kind is unrecognised, which is deliberately not an error:
    an unlabelled reading is the state the panel already handles, while a wrong
    label is a claim about WHICH account the user is signed in to.
    """
    return _ACCOUNT_TYPE_BY_IDENTITY.get((identity or "").strip())


async def crew_vault_credential() -> kiro_usage_api.VaultCredential | None:
    """Crew's own Kiro identity, refreshed, or ``None`` when it holds none.

    Two steps, cheap one first.

    The probe is :func:`kiro_crew.auth.bridge.vault_holds_identity` -- a plain
    read with no refresh and no network, and the same question the KAS relay asks
    before choosing an auth owner. It is what keeps this source from costing
    anything on the install that does not use it: without it, every 30s usage
    tick on a kiro-cli-owned gateway would construct a provider, and an install
    carrying a LAPSED Crew identity (signed in once, then moved back to kiro-cli)
    would attempt a network token refresh on each one. The probe answers False
    for exactly that entry -- an expired token with nothing to renew it -- so the
    refresh is never attempted.

    Only then ``KasAuthProvider.current``, which resolves the vault and refreshes
    the credential past its 3-minute margin before answering, so what comes back
    is live by construction -- the property no bystander store can offer.
    ``profile_arn`` rides the same atomic snapshot as the access token, which is
    what makes it sound to use as the ownership anchor: the two cannot name
    different accounts.

    The snapshot also carries WHO this is, which is why the reading this path
    publishes is not anonymous. Crew performed the sign-in, so the account's kind
    is a stored record rather than something to be re-derived from a subprocess
    (see :func:`_account_type`). The account's EMAIL needs neither: it is a
    member of the GetUsageLimits response itself, which
    ``kiro_usage_api.fetch_usage_limits`` now asks for -- the same place
    kiro-cli's own ``whoami`` gets the email it prints.

    The IdC ``start_url`` is stored too, so the account line can name the
    DIRECTORY and not merely the kind. It has to be captured at login, the way
    kiro-cli captures it: no API returns it (``SsoIdentityDetails`` carries an SSO
    instance ARN, not the portal), so a credential that did not record it cannot
    be repaired later. A sign-in stored before the field existed therefore keeps
    ``None`` and shows the kind alone until the user signs in again -- omitting a
    field is honest; synthesising one is not.

    Vault ONLY (``allow_env_api_key=False``), matching the KAS auth callback's
    choice, and for a second reason specific to this caller: ``KIRO_API_KEY`` is
    not an OIDC bearer and carries no profile ARN, so GetUsageLimits could
    neither authenticate it nor route it to a region. An ambient environment key
    must not stand in for an identity -- least of all one the operator signed
    out of.

    NEVER RAISES. This runs on the 30s usage timer as one of several credential
    sources, so a missing, unreadable, or unrefreshable vault must degrade to
    those other sources exactly as before this one existed -- never fail the
    refresh. Logs at DEBUG and by exception TYPE only: a refresh endpoint's
    error text can echo response bytes, and "no identity stored" is the ordinary
    state of a kiro-cli-owned install, not a fault worth a line every 30s.
    """
    try:
        # Deferred: see the module docstring (boot path + cryptography gate).
        from kiro_crew.auth.bridge import default_token_store, vault_holds_identity
        from kiro_crew.auth.provider import KasAuthProvider, NotAuthenticated
    except Exception as exc:  # noqa: BLE001 - an optional source must not raise
        logger.debug("Kiro usage: Crew sign-in vault unavailable (%s)", type(exc).__name__)
        return None

    # Blocking file IO with a never-raises contract -- off the loop, same shape
    # ``kas_host_auth.vault_holds_identity_off_loop`` uses.
    if not await asyncio.to_thread(vault_holds_identity):
        return None

    try:
        provider = KasAuthProvider(default_token_store(), allow_env_api_key=False)
        snapshot = await provider.current()
    except NotAuthenticated:
        # A sign-out landed between the probe and here. Ordinary, not a fault.
        return None
    except Exception as exc:  # noqa: BLE001 - an optional source must not raise
        logger.debug(
            "Kiro usage: Crew sign-in vault could not supply a credential (%s)",
            type(exc).__name__,
        )
        return None

    if not snapshot.access_token:
        return None
    return kiro_usage_api.VaultCredential(
        token=snapshot.access_token,
        expiry=snapshot.expires_at,
        profile_arn=snapshot.profile_arn or None,
        account_type=_account_type(snapshot.identity, snapshot.provider),
        start_url=snapshot.start_url or None,
    )


async def read_usage_with_crew_credential(
    vault: kiro_usage_api.VaultCredential,
) -> kiro_usage_api.UsageResult:
    """One GetUsageLimits read anchored on Crew's own credential. No subprocess.

    The whole point of this path: it starts no ``kiro-cli``, so it is available
    when the readiness gate has withheld the spawn and when no kiro-cli is
    installed at all.

    ``expected_arn`` is the vault's own profile ARN, which puts this read on the
    STRONGER of the module's two ownership proofs (ARN-anchored) whenever the
    identity carries one -- and an identity that carries none still qualifies by
    provenance, since this is the credential of the account this very process
    signed in. Either way the read is anchored, never unverified.

    Runs on the subprocess executor for the same reason the kiro-cli-anchored
    read does: ``fetch_usage_limits`` makes blocking urllib calls that can hang
    on DNS or a wedged TLS handshake, and those must not sit on the maintenance
    or cron pools.
    """
    return await asyncio.get_running_loop().run_in_executor(
        subprocess_executor(),
        functools.partial(
            kiro_usage_api.fetch_usage_limits,
            expected_arn=vault.profile_arn,
            vault=vault,
        ),
    )
