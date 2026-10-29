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
sign-in, and for an Identity Center sign-in the portal (start URL) it was begun
against, when the sign-in recorded one. It stores no e-mail: the vault's usage
reading asks ``GetUsageLimits`` for it (``isEmailRequired``), the way kiro-cli
does, so the account modal shows the address, the provider badge and the portal
host as it does on the kiro-cli route.

The slot -> ``accountType`` table itself lives in :mod:`kiro_crew.auth.whoami`
(:func:`~kiro_crew.auth.whoami.account_type_for`), the one copy every reader of
the vault's identity spells from: this module, ``kirocrew kas whoami`` and the
cloud sign-in's identity match. With no table of its own here, the dashboard
cannot label an account one way while the remote sign-in matches it another.

``kiro_crew.auth`` is imported at call time only: it brings the ``cryptography``
wheel, which must stay off the handlers package's import path (the boot-path
and tool-approval gates named in :mod:`kiro_crew.acp.kas_host_auth`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

#: Longest ``start_url`` published, matching ``parse_whoami_output``'s bound.
_START_URL_CAP = 200


def account_type_from_claims(identity: str, provider: str) -> str:
    """Map the vault's slot and provider class onto whoami's ``accountType`` vocabulary.

    ``identity`` is the store slot (one of ``kiro_crew.auth.store.KNOWN_IDENTITIES``);
    ``provider`` is the governance class stored beside it. The social slot names
    its provider as the vault spells it (``SocialGoogle``, ``SocialGithub``):
    the account modal's badge humanizes whatever follows ``Social`` by splitting
    at an inner capital, so the one-capital spelling the vault stores is the one
    that renders as a single word. Every other slot has one type. ``""`` when
    neither names a kind the dashboard can label. The spelling is
    :func:`kiro_crew.auth.whoami.account_type_for`'s, so the badge and
    ``kirocrew kas whoami`` can never disagree about one token.
    """
    # Deferred: see the module docstring.
    from kiro_crew.auth.whoami import account_type_for

    return account_type_for(identity, provider)


def identity_fields(claims: Mapping[str, str]) -> dict[str, object]:
    """The whoami-shaped identity fields the vault's public claims support.

    Rendered from :func:`kiro_crew.auth.whoami.whoami_document_for`, the writer
    ``kirocrew kas whoami`` prints, so the account modal and the CLI describe one
    sign-in the same way: ``account_type`` for a stored identity, plus
    ``start_url`` when the sign-in recorded its Identity Center portal. ``{}``
    when ``claims`` is empty (signed out) or names no labelable kind. Never an
    ``email`` key (the vault holds none; the e-mail rides in on the usage reading
    from the API), and never an empty string for a field it does not know: that
    would read as "known to be blank" to the identity-agreement checks on the
    kiro-cli path, which share this shape.
    """
    # Deferred: see the module docstring.
    from kiro_crew.auth.whoami import whoami_document_for

    doc = whoami_document_for(
        str(claims.get("identity") or ""),
        str(claims.get("provider") or ""),
        claims.get("start_url"),
    )
    account_type = doc["accountType"]
    if not account_type:
        return {}
    fields: dict[str, object] = {"account_type": account_type}
    start_url = doc.get("startUrl")
    if start_url:
        # The same bound ``parse_whoami_output`` puts on kiro-cli's own startUrl.
        fields["start_url"] = start_url[:_START_URL_CAP]
    return fields


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


def vault_identity_sync() -> dict[str, object] | None:
    """The vault's signed-in identity in the identity door's three-way shape.

    The identity fields for a stored identity; ``{}`` only when the vault was
    read and PROVABLY stores no identity (:meth:`TokenStore.holds_no_identity`);
    ``None`` when neither can be shown -- the vault did not read, or it holds an
    entry that did not decrypt or parse. The distinction is the identity door's
    contract (``fetch_local_identity``): ``{}`` lets the Remote Crew launch form
    propose the default Builder ID target as a READ answer, so an unreadable
    vault must not collapse onto it and quietly point an Identity Center
    operator at the wrong portal. :func:`vault_identity_snapshot_sync` cannot
    tell the two apart on its own, since ``resolve()`` reports both as no
    token. Never raises. Blocking file IO -- call off the loop, or use
    :func:`vault_identity`.
    """
    fingerprint, fields = vault_identity_snapshot_sync()
    if fingerprint:
        return fields
    # Deferred: see the module docstring.
    from kiro_crew.auth.bridge import default_token_store

    try:
        empty = default_token_store().holds_no_identity()
    except Exception:  # noqa: BLE001 - a vault that will not read proves nothing
        return None
    return {} if empty else None


async def vault_identity() -> dict[str, object] | None:
    """:func:`vault_identity_sync` on a worker thread."""
    return await asyncio.to_thread(vault_identity_sync)
