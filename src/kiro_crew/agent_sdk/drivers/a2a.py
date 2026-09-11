"""The A2A driver: construct a remote-agent provider through the SDK boundary.

Application code (``subagent_manager/run.py``) must not import
``kiro_crew.providers`` directly -- ``scripts/check_agent_sdk_boundary.py``
holds that line -- so the one place a remote agent's provider is built lives
here, next to the ACP driver, and hands back the ``LLMProvider`` the run loop
already knows how to drive.

This module also owns the **authentication scheme table**. An A2A client
authenticates the way the reference clients do: the Agent Card declares the
server's security schemes, the credential is obtained out-of-band, and the
client sends it per scheme -- for HTTP bearer, OAuth2 and OIDC alike that is one
``Authorization: Bearer`` header. The public core ships ``none`` and ``bearer``
(credential read from a named environment variable at request time). An edition
that needs another scheme -- request signing, an identity broker -- registers a
:class:`A2aAuthScheme` with :func:`register_auth_scheme`; the provider never
learns where a credential came from, only which headers to send and which card
scheme types they satisfy.

The import of the provider module is FUNCTION-LOCAL for the same reason as in
:mod:`kiro_crew.agent_sdk.drivers.acp`: ``kiro_crew.providers.a2a`` pulls in the
HTTP client, and nothing on the boot path needs it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit

__all__ = [
    "A2A_PROVIDER_LABEL",
    "A2aAuthScheme",
    "create_a2a_provider",
    "register_auth_scheme",
    "resolve_auth",
]

#: The session-map ``provider`` label an A2A run is persisted under. Spelled
#: here as a literal rather than imported from ``kiro_crew.acp.types`` so
#: application code can ask "is this persisted run an A2A run" without adding
#: an ACP-layer import edge (``scripts/check_agent_sdk_boundary.py`` refuses
#: new ones). Pinned equal to ``PROVIDER_LABEL_A2A`` by
#: ``test_a2a_provider.py`` rather than by an import, as
#: :mod:`kiro_crew.agent_sdk.provider_identity` does for the Claude label.
A2A_PROVIDER_LABEL = "a2a"


@dataclass(frozen=True)
class ResolvedCredential:
    """A credential this client may send, and the ONE origin it may be sent to.

    ``headers`` produces the auth headers for one request (read fresh each
    call). ``origin`` is ``scheme://host[:port]``, pinned by the OPERATOR
    outside ``config.json``: the run path refuses to build a provider whose
    ``agent_card_url`` has any other origin, so an agent-written config entry
    that keeps a valid credential but points the URL elsewhere sends nothing.
    """

    headers: Callable[[], dict[str, str] | None]
    origin: str


@dataclass(frozen=True)
class A2aAuthScheme:
    """One way this client can authenticate to a remote agent.

    ``build`` takes the entry's ``A2aAuthConfig`` and returns a
    :class:`ResolvedCredential` (or ``None`` for no authentication); it raises
    ``ValueError`` when the config is unusable (a ``bearer`` entry with no
    ``token_env``, an env var that is unset, no pinned origin) so the spawn fails
    at construction with a reason rather than sending unauthenticated or
    misdirected requests. ``card_scheme_types`` are the Agent Card security
    scheme types those headers satisfy, in the vocabulary
    ``providers.a2a._scheme_type`` folds cards to.
    """

    name: str
    card_scheme_types: frozenset[str]
    build: Callable[[Any], ResolvedCredential | None]


#: Namespace an ``a2a_agents`` entry's ``token_env`` must live in. ``config.json``
#: is agent-writable, so the name it carries cannot be allowed to select ANY
#: variable in the gateway's environment: an entry naming the messaging bot token
#: or a cloud credential, paired with an attacker-controlled card URL, would send
#: that secret off-box as a bearer header on the next remote spawn. Credentials
#: provisioned FOR remote agents are set under this prefix by the operator (a
#: process-environment decision, not a config one), and nothing else is eligible.
A2A_TOKEN_ENV_PREFIX = "KIROCREW_A2A_"

#: Suffix of the companion variable that pins WHERE a credential may go:
#: ``<token_env>_ORIGIN`` holds ``scheme://host[:port]`` (for example
#: ``https://agents.example.com``). Set by the operator in the process
#: environment beside the token, never in config: the namespace rule above stops
#: config from selecting a foreign secret, and this stops config from redirecting
#: a legitimate one -- an entry that keeps a valid ``token_env`` but rewrites
#: ``agent_card_url`` to another host is refused before any request.
A2A_ORIGIN_ENV_SUFFIX = "_ORIGIN"


def _build_none(_auth: Any) -> None:
    return None


def _normalized_origin(value: str) -> str:
    """``scheme://host[:port]`` lowercased, or ``""`` when *value* is not exactly an origin."""
    try:
        u = urlsplit(value.strip())
    except ValueError:
        return ""
    if u.scheme not in ("https", "http") or u.path not in ("", "/") or u.query or u.fragment:
        return ""
    return card_origin(value)


def _build_bearer(auth: Any) -> ResolvedCredential:
    env_name = str(getattr(auth, "token_env", "") or "").strip()
    if not env_name:
        raise ValueError("a2a auth scheme 'bearer' requires 'token_env' (the NAME of the env var)")
    if not env_name.startswith(A2A_TOKEN_ENV_PREFIX) or env_name == A2A_TOKEN_ENV_PREFIX:
        raise ValueError(
            f"a2a bearer 'token_env' {env_name!r} must name a variable under the "
            f"{A2A_TOKEN_ENV_PREFIX}* namespace; config cannot select other environment "
            "variables as remote credentials"
        )
    if not os.environ.get(env_name):
        # Checked at construction so the failure is a spawn refusal with a
        # reason, not a 401 from the remote a minute later.
        raise ValueError(f"a2a bearer credential env var {env_name!r} is unset or empty")
    origin_env = env_name + A2A_ORIGIN_ENV_SUFFIX
    origin = _normalized_origin(os.environ.get(origin_env, ""))
    if not origin:
        raise ValueError(
            f"a2a bearer credential {env_name!r} has no pinned origin: set {origin_env!r} to "
            "the scheme://host[:port] the credential may be sent to (operator environment, "
            "not config)"
        )

    def _headers() -> dict[str, str] | None:
        # Read on EVERY request: a rotated value is picked up without a restart,
        # and the value itself is never held on the provider or in config.
        value = os.environ.get(env_name, "")
        return {"Authorization": f"Bearer {value}"} if value else None

    return ResolvedCredential(headers=_headers, origin=origin)


_SCHEMES: dict[str, A2aAuthScheme] = {
    "none": A2aAuthScheme("none", frozenset(), _build_none),
    "bearer": A2aAuthScheme("bearer", frozenset({"bearer"}), _build_bearer),
}


def register_auth_scheme(scheme: A2aAuthScheme) -> None:
    """Register (or replace) an auth scheme -- the edition extension point."""
    _SCHEMES[scheme.name] = scheme


def resolve_auth(auth: Any) -> tuple[ResolvedCredential | None, frozenset[str]]:
    """The resolved credential (or ``None``) and ``supported_schemes`` for *auth*.

    Raises ``ValueError`` for an unknown scheme or an unusable config; the
    caller turns that into a spawn refusal.
    """
    name = str(getattr(auth, "scheme", "") or "none").strip().lower()
    scheme = _SCHEMES.get(name)
    if scheme is None:
        raise ValueError(
            f"a2a auth scheme {name!r} is not supported by this edition "
            f"(known: {', '.join(sorted(_SCHEMES))})"
        )
    return scheme.build(auth), scheme.card_scheme_types


def create_a2a_provider(entry: Any, *, context_id: str | None) -> Any:
    """Build (but do not start) the provider for one ``a2a_agents`` entry.

    ``entry`` is the registry record (``name``, ``agent_card_url``, ``auth``);
    ``context_id`` is the retained A2A ``contextId`` on a continuation, or
    ``None`` for a fresh conversation. Starting the provider (the Agent Card
    fetch) is the caller's step, because the caller decides what a fetch
    failure means -- a tombstone on a fresh spawn, ``resume_failed`` on a
    continuation. An unusable ``auth`` raises ``ValueError`` here, before any
    network call -- including a card URL whose origin is not the one the
    operator pinned the credential to.
    """
    from kiro_crew.providers.a2a import A2AProvider

    name = str(getattr(entry, "name", "") or "")
    card_url = str(getattr(entry, "agent_card_url", "") or "")
    credential, supported = resolve_auth(getattr(entry, "auth", None))
    if credential is not None and card_origin(card_url) != credential.origin:
        raise ValueError(
            f"a2a agent {name!r}: agent_card_url origin {card_origin(card_url) or '?'} is not "
            f"the origin its credential is pinned to ({credential.origin}); config cannot "
            "redirect a credential"
        )
    return A2AProvider(
        name=name,
        agent_card_url=card_url,
        credentials=credential.headers if credential is not None else None,
        supported_schemes=supported,
        credential_origin=credential.origin if credential is not None else "",
        context_id=context_id,
    )


def card_origin(url: str) -> str:
    """``scheme://host[:port]`` of an agent card URL (any path), lowercased; ``""`` if unparsable.

    The one origin normalization application code uses: the string the
    ``capabilities.remote_spawn`` ``origins`` ruleset is matched against, and
    what a credential's pinned origin is compared to. Built from the parsed
    hostname and port, never the raw netloc, and a URL carrying userinfo
    (``https://allowed.example:x@evil.example/``) is refused outright: a netloc
    string would let the ``allowed.example`` prefix satisfy a host glob while
    the request went to ``evil.example``.
    """
    try:
        u = urlsplit(str(url or "").strip())
        host = u.hostname
        port = u.port
    except ValueError:
        return ""
    if not u.scheme or not host or u.username is not None or u.password is not None:
        return ""
    host = host.lower()
    if ":" in host:  # IPv6 literal: hostname strips the brackets, the origin keeps them
        host = f"[{host}]"
    return f"{u.scheme.lower()}://{host}" + (f":{port}" if port is not None else "")
