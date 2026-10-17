"""Operator-registered OAuth clients for providers that refuse dynamic registration.

Most Connections providers let kiro-cli register a public OAuth client at
runtime (RFC 7591), so nothing about the client needs to exist before the first
Connect. A few (GitHub, Asana, Google, Slack, HubSpot, Box) do not: the
OPERATOR registers an app in the vendor console and hands Kiro Crew the result.
This module is where that result lives and how it reaches the runtime.

Three sources, one precedence
-----------------------------
For each pre-registered provider ``<slug>``:

* environment: ``KIROCREW_CONNECTIONS_<SLUG>_CLIENT_ID`` /
  ``KIROCREW_CONNECTIONS_<SLUG>_CLIENT_SECRET`` (SLUG upper-cased, hyphens as
  underscores) -- the container / CI shape, and the highest precedence so an
  image-level value cannot be shadowed by a stale dashboard entry;
* the public client id in ``config.json`` under
  ``connections.oauth_clients.<slug>.client_id``;
* the secret in the encrypted vault under :func:`client_secret_name`.

The client id is public (it is in every consent URL) and stays in plain config;
the secret is a credential and never touches ``config.json``.

What custody this is and is not
-------------------------------
The Connections invariant is that Kiro Crew never holds a connection's
CREDENTIAL -- the user's OAuth grant, which kiro-cli owns end to end. A client
secret is a different thing: it is the operator's own APPLICATION credential,
issued to them by the vendor for the app they registered, and there is no way to
use a confidential client without presenting it at the token endpoint. Kiro Crew
therefore holds it the way it already holds ``headers`` on a remote MCP entry:
the vault is the source of truth, and the emitted agent spec is a runtime
PROJECTION that carries the value in plaintext because that file is the only
thing kiro-cli reads (its ``${env:...}`` expansion covers stdio ``env`` maps,
not ``oauth.*``). Nothing here changes who holds the grant.

Wire shape
----------
:func:`apply_preregistered_oauth_client` writes kiro-cli's ``oauth`` block --
``clientId``, ``clientSecret`` and ``redirectUri`` (its ``OAuthConfig``, which
skips DCR when a client id is present and pins the loopback listener to the
given host, port and path). The redirect URI is derived from the registry so
the runbook the operator followed and the listener kiro-cli opens can never
name different strings.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping, TypedDict

from kiro_crew.connections.registry import (
    Provider,
    get_preregistered_providers,
    get_provider,
    is_preregistered,
    redirect_uri,
)
from kiro_crew.mcp_utils import KIRO_OAUTH_KEY

#: Root of the public client-id records in ``config.json``.
CONFIG_ROOT_KEY = "connections"
CONFIG_CLIENTS_KEY = "oauth_clients"
_ENV_PREFIX = "KIROCREW_CONNECTIONS_"
_SECRET_NAME_PREFIX = "CONNECTIONS_"
_SECRET_NAME_SUFFIX = "_CLIENT_SECRET"
# Vendor client ids are opaque tokens; the widest set any launch vendor uses is
# printable ASCII without whitespace. Bounding it keeps a pasted URL or a JSON
# blob out of the consent URL kiro-cli will build from it.
_CLIENT_ID_PATTERN = re.compile(r"^[\x21-\x7e]{1,512}$")
_CLIENT_SECRET_MAX_LEN = 4096

Source = Literal["env", "config", "vault", "registry"]


def _slug_token(slug: str) -> str:
    return slug.upper().replace("-", "_")


def client_id_env_name(slug: str) -> str:
    """Environment variable carrying the client id for ``slug``."""

    return f"{_ENV_PREFIX}{_slug_token(slug)}_CLIENT_ID"


def client_secret_env_name(slug: str) -> str:
    """Environment variable carrying the client secret for ``slug``."""

    return f"{_ENV_PREFIX}{_slug_token(slug)}_CLIENT_SECRET"


def client_secret_name(slug: str) -> str:
    """Vault entry name for the client secret of ``slug``.

    Also the name the Secrets panel lists the entry under, so it is spelled to
    read as what it is rather than as an opaque hash.
    """

    return f"{_SECRET_NAME_PREFIX}{_slug_token(slug)}{_SECRET_NAME_SUFFIX}"


def managed_client_secret_names() -> dict[str, str]:
    """Vault name -> slug for every pre-registered provider.

    Consumed by the Secrets panel's "managed" listing so the entries this module
    writes are labelled with the provider they belong to rather than shown as
    anonymous user secrets a cleanup might delete.
    """

    return {client_secret_name(p["slug"]): str(p["slug"]) for p in get_preregistered_providers()}


def validate_client_id(value: object) -> str | None:
    """Return the trimmed client id, or ``None`` when it is not usable."""

    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not _CLIENT_ID_PATTERN.fullmatch(candidate):
        return None
    return candidate


def validate_client_secret(value: object) -> str | None:
    """Return the client secret VERBATIM, or ``None`` when it is not usable.

    Only the emptiness check strips: a vendor secret can legitimately carry
    surrounding whitespace, and trimming it before storage would corrupt it.
    """

    if not isinstance(value, str) or not value.strip():
        return None
    if len(value) > _CLIENT_SECRET_MAX_LEN or any(ch in value for ch in "\r\n\x00"):
        return None
    return value


class OAuthClientView(TypedDict, total=False):
    """What the dashboard is told about one provider's client -- never the secret."""

    slug: str
    confidential: bool
    redirect_uri: str
    registration_guide: str
    client_id: str | None
    client_id_source: Source | None
    client_secret_set: bool
    client_secret_source: Source | None
    #: True when the runtime can attempt an authorization with what is stored:
    #: a client id, plus a secret for a confidential client.
    configured: bool


@dataclass(frozen=True)
class ResolvedOAuthClient:
    """The values the runtime is handed for one pre-registered provider."""

    slug: str
    client_id: str
    client_id_source: Source
    #: The plaintext secret, or ``None`` for a public client. Kept off the
    #: dashboard shape on purpose -- only the emit path constructs this.
    client_secret: str | None
    client_secret_source: Source | None
    redirect_uri: str


def _config_client_id(config: Mapping[str, Any] | None, slug: str) -> str | None:
    if not isinstance(config, Mapping):
        return None
    root = config.get(CONFIG_ROOT_KEY)
    if not isinstance(root, Mapping):
        return None
    clients = root.get(CONFIG_CLIENTS_KEY)
    if not isinstance(clients, Mapping):
        return None
    record = clients.get(slug)
    if not isinstance(record, Mapping):
        return None
    return validate_client_id(record.get("client_id"))


def _vault_secret(vault: Any, name: str) -> str | None:
    """Read one vault entry, treating an unreadable vault as "not set".

    ``vault`` is duck-typed (``get(name) -> SecretValue | None``) so the emit
    path can pass the real :class:`~kiro_crew.secrets.SecretVault` while a test
    passes a dict-backed stub; a vault that raises must not take the agent spec
    with it, so the failure reads as an unconfigured secret and the card says so.
    """

    if vault is None:
        return None
    try:
        held = vault.get(name)
    except Exception:  # noqa: BLE001 -- see docstring
        return None
    if held is None:
        return None
    reveal = getattr(held, "reveal", None)
    value = reveal() if callable(reveal) else held
    return validate_client_secret(value)


def resolve_oauth_client(
    provider: Provider,
    *,
    config: Mapping[str, Any] | None,
    vault: Any,
    environ: Mapping[str, str] | None = None,
) -> ResolvedOAuthClient | None:
    """Resolve the client the runtime should present for ``provider``.

    ``None`` when the provider is not pre-registered, or when what is stored is
    not enough to attempt an authorization: no client id at all, or a
    confidential client without a secret. The registry's own ``client_id`` is
    the last resort for the id, never for the secret.
    """

    if not is_preregistered(provider):
        return None
    slug = str(provider["slug"])
    env = os.environ if environ is None else environ
    uri = redirect_uri(provider)
    if uri is None:  # pragma: no cover -- is_preregistered() guarantees it
        return None

    client_id: str | None = None
    id_source: Source | None = None
    from_env = validate_client_id(env.get(client_id_env_name(slug)))
    if from_env:
        client_id, id_source = from_env, "env"
    else:
        from_config = _config_client_id(config, slug)
        if from_config:
            client_id, id_source = from_config, "config"
        else:
            from_registry = validate_client_id(provider.get("client_id"))
            if from_registry:
                client_id, id_source = from_registry, "registry"
    if client_id is None or id_source is None:
        return None

    secret: str | None = None
    secret_source: Source | None = None
    env_secret = validate_client_secret(env.get(client_secret_env_name(slug)))
    if env_secret:
        secret, secret_source = env_secret, "env"
    else:
        vault_secret = _vault_secret(vault, client_secret_name(slug))
        if vault_secret:
            secret, secret_source = vault_secret, "vault"
    if secret is None and bool(provider["auth"].get("confidential")):
        return None

    return ResolvedOAuthClient(
        slug=slug,
        client_id=client_id,
        client_id_source=id_source,
        client_secret=secret,
        client_secret_source=secret_source,
        redirect_uri=uri,
    )


def oauth_client_view(
    provider: Provider,
    *,
    config: Mapping[str, Any] | None,
    vault_names: set[str] | frozenset[str],
    environ: Mapping[str, str] | None = None,
) -> OAuthClientView:
    """The dashboard-facing record for ``provider``; carries no secret value.

    Takes the vault's NAME LIST rather than the vault so listing every provider
    costs one ``list_names`` read instead of one decryption per provider, and so
    this function can never be handed a value it might echo.
    """

    slug = str(provider["slug"])
    env = os.environ if environ is None else environ
    auth = provider["auth"]
    view: OAuthClientView = {
        "slug": slug,
        "confidential": bool(auth.get("confidential")),
        "redirect_uri": redirect_uri(provider) or "",
        "registration_guide": str(auth.get("registration_guide") or ""),
        "client_id": None,
        "client_id_source": None,
        "client_secret_set": False,
        "client_secret_source": None,
        "configured": False,
    }
    from_env = validate_client_id(env.get(client_id_env_name(slug)))
    if from_env:
        view["client_id"], view["client_id_source"] = from_env, "env"
    else:
        from_config = _config_client_id(config, slug)
        if from_config:
            view["client_id"], view["client_id_source"] = from_config, "config"
        else:
            from_registry = validate_client_id(provider.get("client_id"))
            if from_registry:
                view["client_id"], view["client_id_source"] = from_registry, "registry"

    if validate_client_secret(env.get(client_secret_env_name(slug))):
        view["client_secret_set"], view["client_secret_source"] = True, "env"
    elif client_secret_name(slug) in vault_names:
        view["client_secret_set"], view["client_secret_source"] = True, "vault"

    view["configured"] = view["client_id"] is not None and (
        view["client_secret_set"] or not view["confidential"]
    )
    return view


def apply_preregistered_oauth_client(
    entry: dict[str, Any], resolved: ResolvedOAuthClient
) -> dict[str, Any]:
    """Write the resolved client into a remote MCP entry, in kiro-cli's wire shape.

    Surgical on ``oauth`` like :func:`kiro_crew.mcp_utils.apply_kiro_oauth_hints`:
    the three keys this module owns are overwritten, every other sub-key
    survives. The operator's record OUTRANKS a ``clientId`` the store may carry
    for the same server -- a stale value there was written before the operator
    configured anything, and honouring it would send the consent to an app the
    operator did not register.
    """

    out = dict(entry)
    raw_oauth = out.get(KIRO_OAUTH_KEY)
    oauth = dict(raw_oauth) if isinstance(raw_oauth, dict) else {}
    oauth["clientId"] = resolved.client_id
    if resolved.client_secret is not None:
        oauth["clientSecret"] = resolved.client_secret
    else:
        oauth.pop("clientSecret", None)
    oauth["redirectUri"] = resolved.redirect_uri
    out[KIRO_OAUTH_KEY] = oauth
    return out


def strip_preregistered_oauth_client(entry: dict[str, Any]) -> dict[str, Any]:
    """Remove the three ``oauth`` keys this module owns from a remote MCP entry.

    The inverse of :func:`apply_preregistered_oauth_client`, for a pre-registered
    provider whose operator record does not resolve: the spec rebuild merges
    onto the installed spec already on disk, so without this a cleared client would
    survive there -- its retired secret still presented at the token endpoint
    and still readable in the file. Sibling ``oauth`` sub-keys survive; the
    mapping is dropped once nothing is left in it.
    """

    out = dict(entry)
    raw_oauth = out.get(KIRO_OAUTH_KEY)
    if not isinstance(raw_oauth, dict):
        return out
    oauth = dict(raw_oauth)
    for key in ("clientId", "clientSecret", "redirectUri"):
        oauth.pop(key, None)
    if oauth:
        out[KIRO_OAUTH_KEY] = oauth
    else:
        out.pop(KIRO_OAUTH_KEY, None)
    return out


def provider_for_server(name: str, entry: Mapping[str, Any] | None) -> Provider | None:
    """The pre-registered provider a configured MCP server is an instance of.

    Keyed on the registry slug AND the transport: the Connections flow installs
    a provider under its slug, but a user can hand-author a server of the same
    name pointing elsewhere, and a credential bound by name alone would land an
    operator's client on a stranger's endpoint. ``None`` for anything else.
    """

    provider = get_provider(name)
    if provider is None or not is_preregistered(provider):
        return None
    if not isinstance(entry, Mapping) or entry.get("url") != provider["mcp_url"]:
        return None
    return provider
