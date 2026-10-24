"""The dashboard's signed-in Kiro identity when Crew's own vault is the credential.

On the KAS harness the account a session runs as is decided at spawn time by
:mod:`kiro_crew.acp.kas_host_auth`: Crew's vault when it holds a usable identity,
and on the bundle route (``agent.kas_bundle``) always, because that route spawns
the engine without kiro-cli and the engine's access-token callback reaches Crew
alone. The dashboard surfaces that name the account -- the account modal's
provider badge over the credit meter, and the Remote Crew launch form's
identity preselect (``fetch_local_identity``) -- must then describe THAT
account, which ``kiro-cli whoami`` cannot: it reports kiro-cli's own login,
which is a different account or none at all on a host without kiro-cli.

This module answers them from the vault, token-free, in the field vocabulary
``kiro-cli whoami --format json`` established
(:func:`kiro_crew.cloud.login_target.parse_whoami_output`:
``account_type`` and friends), so the frontend's provider labelling
(``accountProviderLabel``) branches on nothing new. What the vault can say is
the KIND of account: a Builder ID, Identity Center, company-SSO or social
sign-in. It stores no e-mail and no portal URL, so a vault-labelled reading
carries neither; the account modal then shows the provider badge and, for an
enterprise profile, the profile's display name the usage API already attaches.

``kiro_crew.auth`` is imported at call time only: it brings the ``cryptography``
wheel, which must stay off the handlers package's import path (the boot-path
and tool-approval gates named in :mod:`kiro_crew.acp.kas_host_auth`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

#: The vault's store slot -> the ``accountType`` kiro-cli prints for that kind of
#: sign-in. The social slot is spelled from its provider instead (below).
_ACCOUNT_TYPE_BY_SLOT: Mapping[str, str] = {
    "builder_id": "BuilderId",
    "identity_center": "IamIdentityCenter",
    "external_idp": "ExternalIdP",
}


def account_type_from_claims(identity: str, provider: str) -> str:
    """Map the vault's slot and provider class onto whoami's ``accountType`` vocabulary.

    ``identity`` is the store slot (one of ``kiro_crew.auth.store.KNOWN_IDENTITIES``);
    ``provider`` is the governance class stored beside it. The social slot names
    its provider as the vault spells it (``SocialGoogle``, ``SocialGithub``):
    the account modal's badge humanizes whatever follows ``Social`` by splitting
    at an inner capital, so the one-capital spelling the vault stores is the one
    that renders as a single word. Every other slot has one type. ``""`` when
    neither names a kind the dashboard can label.
    """
    if identity == "social":
        return f"Social{provider}" if provider else ""
    return _ACCOUNT_TYPE_BY_SLOT.get(identity, "")


def identity_fields(claims: Mapping[str, str]) -> dict[str, object]:
    """The whoami-shaped identity fields the vault's public claims support.

    ``{"account_type": ...}`` for a stored identity, ``{}`` when ``claims`` is
    empty (signed out) or names no labelable kind. Deliberately never an
    ``email`` or ``start_url`` key: the vault holds neither, and an empty string
    there would read as "known to be blank" to the identity-agreement checks on
    the kiro-cli path, which share this shape.
    """
    account_type = account_type_from_claims(
        str(claims.get("identity") or ""), str(claims.get("provider") or "")
    )
    return {"account_type": account_type} if account_type else {}


def vault_identity_snapshot_sync() -> tuple[str, dict[str, object]]:
    """One vault read: its identity fingerprint and the identity fields it labels.

    The fields come from the SAME read as the fingerprint
    (:func:`kiro_crew.auth.bridge.vault_identity_claims`), so a caller that
    brackets a credential read with two fingerprints can publish the fields
    knowing they describe the account both fingerprints proved, not one a
    sign-in replaced between a fingerprint read and a separate fields read.
    ``("", {})`` when the vault is empty or unreadable; never raises. Blocking
    file IO -- call off the loop, or use :func:`vault_identity_snapshot`.
    """
    # Deferred: see the module docstring.
    from kiro_crew.auth.bridge import vault_identity_claims

    fingerprint, claims = vault_identity_claims()
    return fingerprint, identity_fields(claims)


async def vault_identity_snapshot() -> tuple[str, dict[str, object]]:
    """:func:`vault_identity_snapshot_sync` on a worker thread."""
    return await asyncio.to_thread(vault_identity_snapshot_sync)
