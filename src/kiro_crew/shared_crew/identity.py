"""Derive the trusted Shared Crew actor of an already-authorized request.

``token_auth_middleware`` decides whether a request may proceed. Only after it
has said yes does it call :func:`publish_actor`, which turns what the middleware
verified into a :class:`~kiro_crew.shared_crew.contracts.ActorContext` under
``request[ACTOR_REQUEST_KEY]``. Nothing here can change that decision: every
failure in this module publishes the Unknown actor and returns, and no function
here is called before the middleware's own deny paths.

Evidence, strongest first:

* a caller the Kiro relay verified, handed over by this Crew's relay client
  (``kiro_relay``): a teammate keyed by their Kiro user ID, or the relayed owner
  under the owner's own principal id (:func:`relay_principal`);
* an app identity (an app token, or an internal caller the middleware derived
  an app for): the app's system principal, ``app:<name>``;
* the internal loopback secret: Crew's own processes, the ``internal`` system
  principal;
* a tailnet peer the local daemon resolved behind ``tailscale serve``
  (``tailscale_serve``), already resolved and allowlisted by the middleware;
* a tailnet peer the direct tailnet listener admitted through its access set
  (``tailscale_listener``);
* a person the SSM listener admitted by a Crew invite (``ssm_invite``);
* a tailnet peer connected to the gateway's tailnet address directly, resolved
  here through the daemon's ``whois`` of the socket address
  (``tailscale_direct``, attribution only);
* a loopback socket with no forwarding header at all: the local operator, keyed
  by the token subject;
* anything else: Unknown.

A request body, query, display name, email or forwarding header never names a
principal. ``Tailscale-User-Login`` is read by ``tailnet`` only as
corroboration of the daemon's answer.
"""

from __future__ import annotations

import dataclasses
import logging
import re
import uuid
from typing import TYPE_CHECKING, Any

from kiro_crew.shared_crew.contracts import (
    ACTOR_REQUEST_KEY,
    SSM_GUEST_REQUEST_KEY,
    ActorContext,
    AuthSnapshot,
    DisplaySnapshot,
    Principal,
    PrincipalKind,
    Provenance,
    Transport,
    principal_id_for,
)

if TYPE_CHECKING:
    from aiohttp import web

    from kiro_crew.dashboard.kiro_relay.caller import RelayCaller
    from kiro_crew.dashboard.tailnet import ForwardedPeer, TailnetTrust
    from kiro_crew.dashboard.tailnet_direct import DirectPeer

logger = logging.getLogger(__name__)

PROVIDER_TAILSCALE = "tailscale"
#: The daemon answers for one tailnet, the one this host is on, so its logins
#: share one issuer scope.
TAILNET_ISSUER = "tailnet"
PROVIDER_LOCAL = "local"
LOCAL_ISSUER = "crew"
PROVIDER_SSM = "ssm"
SSM_ISSUER = "crew"
#: A person the Kiro relay verified: keyed by their Kiro user ID.
PROVIDER_KIRO = "kiro"
KIRO_ISSUER = "kiro-relay"

#: The system component every internal-secret caller (kiro-cli, MCP, doctor) is
#: attributed to. The secret proves the call came from inside, not which person
#: asked; the person rides on the session the work belongs to.
INTERNAL_COMPONENT = "internal"
_APP_COMPONENT_PREFIX = "app:"

#: Request header a client may set to correlate its own retries in audit. It is
#: a label, never identity, and is used only when it is a short plain token.
CORRELATION_HEADER = "X-Correlation-Id"
_CORRELATION_RE = re.compile(r"\A[A-Za-z0-9._\-]{1,64}\Z")

#: Request key holding the device a principal was seen from, for the People
#: registry's device count. Never published to a browser.
DEVICE_REQUEST_KEY = "shared_crew_device"


def tailnet_principal(
    login: str,
    node: str,
    provenance: Provenance,
    *,
    display_name: str = "",
    tagged: bool = False,
) -> Principal:
    """The principal for a daemon-verified tailnet peer.

    A person is keyed by their login, so every device, tab and transport they
    use maps to one id and one budget. The login is the one key both verified
    transports have: ``tailscale serve`` names only the login, while the direct
    listener's ``whois`` also names a stable user id, which stays an alias and
    is never read here, because keying by it would give one person a second
    budget on the listener. The login is also the allowlist key. An ACL-tagged node reports the same
    ``tagged-devices`` login as every other tagged node, so it is a ``machine``
    keyed by its node name instead, and two tagged nodes never share a budget
    or a byline. A display name is a label only and never part of the key.
    """
    from kiro_crew.dashboard.tailnet import TAGGED_DEVICES_LOGIN

    node_name = node.rstrip(".").lower()
    if login == TAGGED_DEVICES_LOGIN or tagged:
        subject = f"node:{node_name}"
        return Principal(
            id=principal_id_for(PROVIDER_TAILSCALE, TAILNET_ISSUER, subject),
            kind=PrincipalKind.MACHINE,
            provider=PROVIDER_TAILSCALE,
            issuer=TAILNET_ISSUER,
            subject=subject,
            provenance=provenance,
            display=DisplaySnapshot(name=node_name, login=""),
        )
    subject = login.strip().lower()
    return Principal(
        id=principal_id_for(PROVIDER_TAILSCALE, TAILNET_ISSUER, subject),
        kind=PrincipalKind.HUMAN,
        provider=PROVIDER_TAILSCALE,
        issuer=TAILNET_ISSUER,
        subject=subject,
        provenance=provenance,
        display=DisplaySnapshot(name=(display_name or login).strip(), login=login.strip()),
    )


def direct_principal(peer: DirectPeer) -> Principal:
    """The principal of a peer the direct tailnet listener verified and admitted."""
    return tailnet_principal(
        peer.login,
        peer.node,
        Provenance.TAILSCALE_LISTENER,
        display_name=peer.display_name,
        tagged=peer.tagged,
    )


def ssm_principal(invite_id: str, alias: str = "") -> Principal:
    """The principal of a person who entered through an SSM invite.

    Keyed by the invite id alone: AWS Systems Manager port forwarding reaches
    the gateway as a plain loopback connection, so nothing about the AWS
    caller is visible here. The alias is a label the owner typed.
    """
    subject = invite_id.strip()
    return Principal(
        id=principal_id_for(PROVIDER_SSM, SSM_ISSUER, subject),
        kind=PrincipalKind.HUMAN,
        provider=PROVIDER_SSM,
        issuer=SSM_ISSUER,
        subject=subject,
        provenance=Provenance.SSM_INVITE,
        display=DisplaySnapshot(name=(alias.strip() or subject)[:128], login=""),
    )


def local_operator_principal(token_subject: str) -> Principal:
    """The local operator, keyed by the subject of the token they hold.

    The owner's subjects are labelled with this machine's name; the id is the
    subject's alone, so the label never changes who the principal is.
    """
    from kiro_crew.shared_crew import owner

    subject = token_subject.strip()
    if not subject:
        return Principal.unknown()
    label = (
        owner.machine_name(token_subject=subject) if owner.is_owner_subject(subject) else subject
    )
    return Principal(
        id=principal_id_for(PROVIDER_LOCAL, LOCAL_ISSUER, subject),
        kind=PrincipalKind.HUMAN,
        provider=PROVIDER_LOCAL,
        issuer=LOCAL_ISSUER,
        subject=subject,
        provenance=Provenance.LOCAL_OPERATOR,
        display=DisplaySnapshot(name=label[:128], login=""),
    )


def _email_label(email: str, handle_domains: frozenset[str]) -> str:
    """*email* as a person's label: its handle when its domain is one of *handle_domains*.

    The whole domain after the last ``@`` must equal a listed one, ignoring case, so
    a subdomain or a longer lookalike keeps the full address.
    """
    handle, _, domain = email.rpartition("@")
    return handle if handle and domain.lower() in handle_domains else email


def relay_principal(
    caller: RelayCaller, owner_subject: str, handle_email_domains: frozenset[str] = frozenset()
) -> Principal:
    """The principal of a person the Kiro relay verified and the relay client handed over.

    A teammate is keyed by their Kiro user ID, exactly as the relay named it (no
    case folding), so every device and tab they use is one person with one budget.
    The relayed owner of a personal Crew is the owner at the machine: the same id
    as :func:`local_operator_principal` of *owner_subject*, so the two tabs are one
    person, but with ``kiro_relay`` provenance, so every gate that requires the
    local operator still refuses them remotely. The email and name are labels only;
    an email in one of *handle_email_domains* (``tunnel.relay_handle_email_domains``)
    is labelled by its handle.
    """
    if caller.is_owner:
        local = local_operator_principal(owner_subject)
        if local.is_unknown:
            return local
        return dataclasses.replace(local, provenance=Provenance.KIRO_RELAY)
    label = (
        caller.display_name
        or _email_label(caller.email, handle_email_domains)
        or f"Kiro user ...{caller.kiro_user_id[-6:]}"
    )
    return Principal(
        id=principal_id_for(PROVIDER_KIRO, KIRO_ISSUER, caller.kiro_user_id),
        kind=PrincipalKind.HUMAN,
        provider=PROVIDER_KIRO,
        issuer=KIRO_ISSUER,
        subject=caller.kiro_user_id,
        provenance=Provenance.KIRO_RELAY,
        # No login: a stored author stamp loses its provenance, so a login here could
        # reach an audit record read back from a transcript. The email is only the
        # label when the relay sent no name.
        display=DisplaySnapshot(name=label[:128], login=""),
    )


def app_principal(app: str) -> Principal:
    """An installed app acting with its own token."""
    return Principal.system(f"{_APP_COMPONENT_PREFIX}{app}")


def derive_principal(
    *,
    internal: bool,
    app: str,
    token_subject: str,
    serve_peer: ForwardedPeer | None,
    direct_peer: ForwardedPeer | None,
    remote: str,
    forwarded: bool,
) -> Principal:
    """Pick the principal the strongest available evidence supports.

    Pure, so the precedence is testable without a request. *forwarded* is
    whether the request carried any forwarding header; a forwarded request is
    never the local operator, because the socket then belongs to a proxy and
    the person behind it is unknown unless the daemon said who.
    """
    from kiro_crew.dashboard.urls import is_loopback

    if app:
        return app_principal(app)
    if internal:
        return Principal.system(INTERNAL_COMPONENT)
    if serve_peer is not None:
        return tailnet_principal(serve_peer.login, serve_peer.node, Provenance.TAILSCALE_SERVE)
    if direct_peer is not None:
        return tailnet_principal(direct_peer.login, direct_peer.node, Provenance.TAILSCALE_DIRECT)
    if remote and is_loopback(remote) and not forwarded:
        return local_operator_principal(token_subject)
    return Principal.unknown()


def _transport_of(request: web.Request) -> Transport:
    if (request.headers.get("Upgrade") or "").lower() == "websocket":
        return Transport.WEBSOCKET
    return Transport.HTTP


def _correlation_of(request: web.Request, request_id: str) -> str:
    raw = (request.headers.get(CORRELATION_HEADER) or "").strip()
    return raw if _CORRELATION_RE.match(raw) else request_id


def _auth_snapshot(request: web.Request, pin_scope: str) -> AuthSnapshot:
    return AuthSnapshot(
        token_subject=str(request.get("user") or ""),
        app=str(request.get("app") or ""),
        is_dashboard_user=request.get("is_dashboard_user") is True,
        pin_scope=pin_scope,
    )


def _device_of(principal: Principal, peer: ForwardedPeer | None) -> str:
    if peer is not None:
        return f"node:{peer.node.rstrip('.').lower()}"
    if principal.provenance is Provenance.LOCAL_OPERATOR:
        return "local"
    return ""


def unknown_actor(request: web.Request | None = None) -> ActorContext:
    """The actor for a request no published actor covers."""
    request_id = uuid.uuid4().hex
    return ActorContext(
        principal=Principal.unknown(),
        request_id=request_id,
        transport=Transport.HTTP if request is None else _transport_of(request),
        correlation_id=request_id if request is None else _correlation_of(request, request_id),
    )


def _people_principal(principal: Principal) -> Principal:
    """The principal People records for *principal*.

    The relayed owner shares the owner's id at the machine, and People keeps the
    newest sighting's principal for that id, so the owner is recorded as the local
    operator: the stored entry then never flips with whichever tab spoke last. The
    published actor keeps ``kiro_relay``, so every request-time gate still sees it.
    """
    if principal.provenance is Provenance.KIRO_RELAY and principal.provider == PROVIDER_LOCAL:
        return dataclasses.replace(principal, provenance=Provenance.LOCAL_OPERATOR)
    return principal


async def publish_actor(
    request: web.Request,
    *,
    trust: TailnetTrust | None,
    serve_peer: ForwardedPeer | None,
    internal: bool = False,
) -> ActorContext:
    """Publish the actor of an authorized request and return it.

    Called by the middleware after its decision and before the handler. Never
    raises: an exception anywhere in derivation publishes Unknown, which only
    loses a label. A direct tailnet peer is resolved here, after the decision,
    so its daemon lookup can never affect admission.
    """
    try:
        actor = await _derive_actor(request, trust=trust, serve_peer=serve_peer, internal=internal)
    except Exception:
        logger.warning("shared crew actor derivation failed; publishing Unknown", exc_info=True)
        actor = unknown_actor(request)
    request[ACTOR_REQUEST_KEY] = actor
    try:
        from kiro_crew.shared_crew.attribution import bind_request_author

        bind_request_author(actor)
    except Exception:
        logger.debug("binding the request author failed", exc_info=True)
    try:
        from kiro_crew.shared_crew.people import observe

        observe(_people_principal(actor.principal), str(request.get(DEVICE_REQUEST_KEY) or ""))
    except Exception:
        logger.debug("people observation failed", exc_info=True)
    return actor


def _handle_email_domains(request: web.Request) -> frozenset[str]:
    """The relay ingress's handle domains for *request*'s app, empty without an ingress."""
    from kiro_crew.dashboard.kiro_relay.ingress import RELAY_INGRESS_APP_KEY, RelayIngress

    ingress = request.app.get(RELAY_INGRESS_APP_KEY)
    return ingress.handle_email_domains if isinstance(ingress, RelayIngress) else frozenset()


async def _derive_actor(
    request: web.Request,
    *,
    trust: TailnetTrust | None,
    serve_peer: ForwardedPeer | None,
    internal: bool,
) -> ActorContext:
    from kiro_crew.dashboard.kiro_relay.caller import published_relay_caller
    from kiro_crew.dashboard.origin import is_proxied_request
    from kiro_crew.dashboard.tailnet import resolve_direct_peer
    from kiro_crew.dashboard.tailnet_direct import DIRECT_PEER_REQUEST_KEY, DirectPeer

    relayed = published_relay_caller(request)
    if relayed is not None:
        # Only a verified handover publishes a caller, and it returns before any
        # tailnet lookup, so a relayed request never triggers a whois.
        request[DEVICE_REQUEST_KEY] = "kiro-relay"
        request_id = uuid.uuid4().hex
        return ActorContext(
            principal=relay_principal(
                relayed, str(request.get("user") or ""), _handle_email_domains(request)
            ),
            request_id=request_id,
            transport=Transport.REMOTE,
            correlation_id=_correlation_of(request, request_id),
            auth=_auth_snapshot(request, ""),
        )
    app = str(request.get("app") or "")
    guest = request.get(SSM_GUEST_REQUEST_KEY)
    if not internal and not app and guest is not None:
        # The SSM listener's gate already admitted this invite; an SSM request
        # is never derived from its loopback address or a token subject.
        request[DEVICE_REQUEST_KEY] = f"ssm:{guest.invite_id}"
        request_id = uuid.uuid4().hex
        return ActorContext(
            principal=ssm_principal(guest.invite_id, guest.alias),
            request_id=request_id,
            transport=_transport_of(request),
            correlation_id=_correlation_of(request, request_id),
            auth=_auth_snapshot(request, ""),
        )
    verified = request.get(DIRECT_PEER_REQUEST_KEY)
    if not internal and not app and isinstance(verified, DirectPeer):
        principal = direct_principal(verified)
        request[DEVICE_REQUEST_KEY] = f"node:{verified.node_key}"
        request_id = uuid.uuid4().hex
        return ActorContext(
            principal=principal,
            request_id=request_id,
            transport=_transport_of(request),
            correlation_id=_correlation_of(request, request_id),
            auth=_auth_snapshot(request, "" if trust is None else trust.pin_scope),
        )
    direct_peer: ForwardedPeer | None = None
    if not internal and not app and serve_peer is None and trust is not None:
        direct_peer = await resolve_direct_peer(request, trust)
    principal = derive_principal(
        internal=internal,
        app=app,
        token_subject=str(request.get("user") or ""),
        serve_peer=serve_peer,
        direct_peer=direct_peer,
        remote=request.remote or "",
        forwarded=is_proxied_request(request),
    )
    request[DEVICE_REQUEST_KEY] = _device_of(principal, serve_peer or direct_peer)
    request_id = uuid.uuid4().hex
    return ActorContext(
        principal=principal,
        request_id=request_id,
        transport=Transport.INTERNAL if internal else _transport_of(request),
        correlation_id=_correlation_of(request, request_id),
        auth=_auth_snapshot(request, "" if trust is None else trust.pin_scope),
    )


def actor_of(request: Any) -> ActorContext:
    """The published actor of *request*, or Unknown when none was published.

    The one accessor handlers use. A request the middleware never covered (a
    bypassed static path, a test app without the middleware) reads as Unknown,
    never as the operator.
    """
    actor = None
    try:
        actor = request.get(ACTOR_REQUEST_KEY)
    except Exception:
        actor = None
    if isinstance(actor, ActorContext):
        return actor
    return unknown_actor(None)


def viewer_principal(request: Any) -> Principal:
    """The authenticated viewer's principal, for ``mine`` and People."""
    return actor_of(request).principal
