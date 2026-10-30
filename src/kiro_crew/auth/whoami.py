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
from typing import TYPE_CHECKING, Any

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
#: THE one table: the dashboard's vault reading
#: (``kiro_crew.dashboard.handlers.kiro_vault_identity``) spells from it too, so
#: a slot added or respelled here reaches every reader at once.
_ACCOUNT_TYPE_BY_SLOT: Mapping[str, str] = {
    "builder_id": "BuilderId",
    "identity_center": "IamIdentityCenter",
    "external_idp": "ExternalIdP",
}


class ConfigUnreadable(Exception):
    """The config could not be read, so which program owns the sign-in is unknown.

    Raised by :func:`load_sign_in_owner_config` for a load that raised AND for a
    load that came back degraded (``config.json`` or ``config.local.json`` present
    but unparseable, so the agent section is a default rather than the user's).
    The two look different to the loader and the same to a sign-in: a default
    agent section says "kiro-cli" about a host that may well be on the bundle
    route, and acting on it signs the wrong store out.
    """


def agent_owns_sign_in(agent: Any) -> bool:
    """THE bundle-route predicate over a loaded ``agent`` section.

    Positive on both halves: ``agent.acp_backend == "kas"`` AND ``agent.kas_bundle``
    on -- the shape ``kiro_prerequisite_service._kas_bundle_route`` uses for the
    readiness gate. The bundle flag alone says nothing when another harness is
    selected, and the backend alone is the kiro-cli relay route, where kiro-cli
    owns the sign-in.
    """
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS

    return getattr(agent, "acp_backend", "") == ACP_BACKEND_KAS and bool(
        getattr(agent, "kas_bundle", False)
    )


def load_sign_in_owner_config() -> Any:
    """Load the config for a sign-in-owner decision, refusing a degraded read.

    Imports the config loader at call time so this module stays cheap for
    ``kirocrew``'s argument parser. Raises :class:`ConfigUnreadable` when the load
    raised or came back with ``_base_unreadable`` / ``_overlay_unreadable`` set.
    """
    from kiro_crew.config.loader import KiroCrewConfig

    try:
        cfg = KiroCrewConfig.load()
    except Exception as exc:  # noqa: BLE001 - every failure is the same refusal
        raise ConfigUnreadable(f"{type(exc).__name__}: {exc}") from exc
    unreadable = [
        name
        for name, flag in (
            ("config.json", getattr(cfg, "_base_unreadable", False)),
            ("config.local.json", getattr(cfg, "_overlay_unreadable", False)),
        )
        if flag
    ]
    if unreadable:
        raise ConfigUnreadable(
            f"{' and '.join(unreadable)} present but unparseable, so the settings read are "
            "defaults, not yours"
        )
    return cfg


def vault_owns_sign_in() -> bool:
    """Whether THIS host runs the KAS bundle route, so Crew's vault owns the sign-in.

    :func:`agent_owns_sign_in` over :func:`load_sign_in_owner_config`. A config
    that cannot be read (or read only as defaults) answers False here, the answer
    that keeps kiro-cli as the owner everywhere it is today -- right for an
    in-process reader that has a kiro-cli fallback, wrong for ``kirocrew kas route``,
    which therefore reports the failure instead (``cli_kas_login._route``).
    """
    try:
        return agent_owns_sign_in(load_sign_in_owner_config().agent)
    except ConfigUnreadable:
        return False


def account_type_for(identity: str, provider: str) -> str:
    """Map a vault slot and its provider class onto whoami's ``accountType``.

    ``identity`` is the store slot (one of ``kiro_crew.auth.store.KNOWN_IDENTITIES``);
    ``provider`` the governance class stored beside it. ``""`` when neither names a
    kind this vocabulary has a word for.
    """
    if identity == "social":
        return f"Social{provider}" if provider else ""
    return _ACCOUNT_TYPE_BY_SLOT.get(identity, "")


def recorded_start_url(token: KasToken) -> str:
    """The start URL *token*'s sign-in recorded, or ``""`` when it recorded none."""
    start_url = token.extra.get(START_URL_EXTRA_KEY) if isinstance(token.extra, dict) else None
    return start_url if isinstance(start_url, str) else ""


def whoami_document_for(identity: str, provider: str, start_url: object) -> dict[str, str]:
    """The ``kiro-cli whoami --format json`` document for a slot, provider and start URL.

    THE one writer of the vault's ``accountType`` and ``startUrl``: ``kirocrew kas
    whoami``, the cloud login target and the dashboard's account modal
    (``kiro_crew.dashboard.handlers.kiro_vault_identity``) all render from it, so
    the card and the CLI cannot describe one sign-in differently. ``startUrl`` is
    present only when *start_url* is a non-empty string.
    """
    doc = {"accountType": account_type_for(identity, provider)}
    if isinstance(start_url, str) and start_url:
        doc["startUrl"] = start_url
    return doc


def whoami_document(token: KasToken) -> dict[str, str]:
    """The ``kiro-cli whoami --format json`` document the stored *token* stands for.

    Keys are kiro-cli's own camelCase, and only the two the parser reads:
    ``accountType`` always, ``startUrl`` when the sign-in recorded one. Never the
    token itself, never the refresh token, never the profile ARN.
    """
    return whoami_document_for(token.identity, token.provider, recorded_start_url(token))


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
