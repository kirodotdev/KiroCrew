"""Who Crew's own vault is signed in as, in the vocabulary ``kiro-cli whoami`` set.

On the KAS bundle route (``agent.acp_backend == "kas"`` with ``agent.kas_bundle``
on) the vault under ``<data home>/kas`` is the engine's only credential, so the
question "who is this machine signed in as" is answered here and not by
``kiro-cli whoami``, which that route does not install. Every reader of a remote
or local identity -- the cloud login target (:mod:`kiro_crew.cloud.login_target`),
the on-box ``kirocrew kas whoami`` the cloud sign-in runs over SSM -- already
parses ``kiro-cli whoami --format json`` (``accountType``, ``startUrl``, ``email``),
so the vault's answer is rendered in that same document shape and goes through the
same parser. One vocabulary, two credential owners, no second code path on the
reading side.

What the vault can say is the KIND of account: Builder ID, Identity Center,
company SSO, Google or GitHub. It stores no e-mail, so no ``email`` key is ever
written -- an empty string there would read as "known to be blank" to the identity
comparisons on the kiro-cli path, which share this shape. The Identity Center
start URL is recorded on the token at sign-in (``KasToken.extra["start_url"]``),
because it is the one field that tells one organisation's sign-in from another's
(:func:`kiro_crew.cloud.login_target.identity_matches_target`).

Nothing here returns, logs or prints a token value.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.auth.store import KasToken, TokenStore

#: ``KasToken.extra`` key under which an SSO-OIDC sign-in records the start URL it
#: was begun against. Written by :class:`kiro_crew.auth.service.KasLoginService`,
#: read here. Absent on tokens stored before it was recorded, and on every
#: non-OIDC identity.
START_URL_EXTRA_KEY = "start_url"

#: The vault's store slot -> the ``accountType`` kiro-cli prints for that kind of
#: sign-in. The social slot is spelled from its provider instead (below), as
#: ``Social<Provider>`` -- the same family :func:`is_personal_account_type` accepts.
_ACCOUNT_TYPE_BY_SLOT: Mapping[str, str] = {
    "builder_id": "BuilderId",
    "identity_center": "IamIdentityCenter",
    "external_idp": "ExternalIdP",
}


def vault_owns_sign_in() -> bool:
    """Whether THIS host runs the KAS bundle route, so Crew's vault owns the sign-in.

    Positive on both halves, read from the config at call time: ``agent.acp_backend
    == "kas"`` AND ``agent.kas_bundle`` on -- the shape
    ``kiro_prerequisite_service._kas_bundle_route`` uses for the readiness gate.
    The bundle flag alone says nothing when another harness is selected, and the
    backend alone is the kiro-cli relay route, where kiro-cli owns the sign-in.
    An unreadable config answers False, the answer that keeps kiro-cli as the
    owner everywhere it is today. Imports the config loader at call time so this
    module stays cheap for ``kirocrew``'s argument parser.
    """
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
    from kiro_crew.config.loader import KiroCrewConfig

    try:
        agent = KiroCrewConfig.load().agent
    except Exception:  # noqa: BLE001 - a route probe never raises
        return False
    return agent.acp_backend == ACP_BACKEND_KAS and bool(agent.kas_bundle)


def account_type_for(identity: str, provider: str) -> str:
    """Map a vault slot and its provider class onto whoami's ``accountType``.

    ``identity`` is the store slot (one of ``kiro_crew.auth.store.KNOWN_IDENTITIES``);
    ``provider`` the governance class stored beside it. ``""`` when neither names a
    kind this vocabulary has a word for.
    """
    if identity == "social":
        return f"Social{provider}" if provider else ""
    return _ACCOUNT_TYPE_BY_SLOT.get(identity, "")


def whoami_document(token: KasToken) -> dict[str, str]:
    """The ``kiro-cli whoami --format json`` document the stored *token* stands for.

    Keys are kiro-cli's own camelCase, and only the two the parser reads:
    ``accountType`` always, ``startUrl`` when the sign-in recorded one. Never the
    token itself, never the refresh token, never the profile ARN.
    """
    doc = {"accountType": account_type_for(token.identity, token.provider)}
    start_url = token.extra.get(START_URL_EXTRA_KEY) if isinstance(token.extra, dict) else None
    if isinstance(start_url, str) and start_url:
        doc["startUrl"] = start_url
    return doc


def resolve_signed_in(store: TokenStore) -> KasToken | None:
    """The identity *store* is signed in as, or ``None`` when it is not signed in.

    "Signed in" is :meth:`KasToken.is_usable` -- the one predicate the spawn-time
    owner decision, ``kirocrew doctor`` and the dashboard card read -- AND no
    refresh-rejected marker on the slot: an entry whose refresh the issuer has
    refused still carries a refresh token, so ``is_usable`` stays true while every
    credential callback fails. ``kirocrew doctor`` and the sign-in card already call
    that "sign-in expired -- sign in again", and this answers the same, so
    ``kas login`` goes on to a fresh sign-in that replaces the slot instead of
    printing "already signed in" over a dead one. A lapsed entry with nothing to
    renew it answers ``None`` too, exactly as ``kiro-cli whoami`` exits non-zero on a
    session it cannot renew. A vault that cannot be read answers ``None`` as well:
    the caller is asking whether a sign-in is needed, and "cannot tell" must not
    read as "signed in".
    """
    try:
        token = store.resolve()
        if token is None or not token.is_usable():
            return None
        if store.refresh_rejected(token.identity) is not None:
            return None
    except Exception:  # noqa: BLE001 - a status read never raises
        return None
    return token
