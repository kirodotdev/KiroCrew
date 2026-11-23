"""The relay client in the tunnel provider slot.

:class:`RelayTunnelProvider` implements the ``TunnelProvider`` protocol
(``kiro_crew.platform.interfaces``) over a :class:`~kiro_crew.relay_client.client.RelayClient`.
The tunnel wrapper (``tunnel/manager.py``) drives it like any provider: it registers
the connect and disconnect callbacks, which put the Crew's relay URL into the allowed
origins, and it reaches ``start()`` only through ``tunnel/setup.py``, whose gates
refuse to start any provider under ``--no-tunnel`` or without dashboard token auth.

:func:`public_tunnel_provider` builds this provider for the public composition root,
which calls it only when ``kiro_crew.edition_locks.relay_selected`` says
``tunnel.relay_enabled`` asks for it (``remote_control.hosting``) and the build does not lock local
enables, and installs the no-op ``DefaultTunnelProvider`` otherwise. That rule lives
outside this package so a composition with the relay off never imports it. The
provider's host token source is the lease client, on ``tunnel.relay_lease_url`` or,
when that is empty, on the built-in Kiro control plane URL. An edition that composes
its own context puts its own provider in the slot and never reaches this function,
so the relay client is never chosen over an edition's provider.
"""

from __future__ import annotations

import ipaddress
import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Optional

from kiro_crew.relay_client.client import (
    STATE_CONNECTED,
    STATE_ERROR,
    STATE_STOPPED,
    RelayClient,
)
from kiro_crew.relay_client.ingress import CrewIngress, LoopbackIngress
from kiro_crew.relay_client.lease import (
    DEFAULT_LEASE_REGION,
    SECRET_URI_PREFIX,
    SIGN_IN_CREDENTIAL,
    TOKEN_TYPE_API_KEY,
    TOKEN_TYPES,
    LeaseHostTokenSource,
    credential_reference_problem,
    lease_url_problem,
)
from kiro_crew.relay_client.protocol import (
    CREW_HOST_RE,
    WS_MESSAGE_MAX_BYTES,
    host_url_problem,
    is_loopback_host,
)
from kiro_crew.relay_client.token_source import HostTokenSource, UnavailableHostTokenSource
from kiro_crew.remote_control.hosting import personal_by_default, relay_requested

if TYPE_CHECKING:
    from kiro_crew.dashboard.kiro_relay.ingress import RelayIngress as GatewayRelayIngress

logger = logging.getLogger(__name__)

#: The relay's own names have no built-in default: they come from the lease
#: answer, or from ``RelaySettings.host_url`` and ``crew_domain`` when an edition
#: builds the settings with its own host token source.
#: Room for one endpoint label (63) and its dot inside a 253-character host.
_CREW_DOMAIN_MAX_CHARS = 189


class RelaySettingsError(ValueError):
    """A relay setting that cannot be used; the message names the setting."""


class RelayBindUnreachable(RelaySettingsError):
    """The gateway is bound to one non-loopback address, which the relay cannot hand to."""


@dataclass(frozen=True)
class RelaySettings:
    """The relay's names for this Crew, resolved from ``config.json``'s ``tunnel`` section.

    The lease answer's ``hostUrl`` and ``crewUrl`` take precedence over
    ``host_url`` and ``crew_domain``, which ``config.json`` never sets: an edition
    with its own host token source passes them when it builds the settings.
    ``lease_url`` names the lease service; while it is empty the Crew leases from
    the built-in URL, unless an edition named the relay. ``lease_credential`` says where the Crew's credential for that
    service is, never the credential itself. Empty, with no ``lease_token_type``, means
    the Crew's own Kiro sign-in, as ``kiro-sign-in`` does (:attr:`uses_sign_in`),
    unless the host has an agent workload identity.
    """

    enabled: bool
    host_url: str
    crew_domain: str
    lease_url: str = ""
    lease_credential: str = field(default="", repr=False)
    #: The lease call's ``TokenType`` header; empty sends none.
    lease_token_type: str = ""
    #: The built-in lease service's Region for a Crew whose credential names none.
    lease_region: str = ""
    #: A personal Crew that hosts only by default: a bind the relay cannot reach
    #: leaves it stopped with a WARNING, not an error
    #: (``remote_control.hosting.personal_by_default``).
    personal_default: bool = False

    @classmethod
    def from_config(cls, cfg: Any) -> "RelaySettings":
        tunnel = getattr(cfg, "tunnel", None)
        return cls(
            # A stand-in config object answers any attribute truthily, so only the
            # one rule's answer turns it on.
            enabled=relay_requested(tunnel),
            personal_default=personal_by_default(tunnel),
            host_url="",
            crew_domain="",
            lease_url=_setting_text(tunnel, "relay_lease_url"),
            lease_credential=_setting_text(tunnel, "relay_lease_credential"),
            lease_token_type=_setting_text(tunnel, "relay_lease_token_type"),
            lease_region=_setting_text(tunnel, "relay_lease_region"),
        )

    def validate(self) -> None:
        """Raise :class:`RelaySettingsError` for a setting the client must not use.

        The host token rides in the dial's ``Authorization`` header, so the dial is
        ``wss://`` (TLS) unless it names this machine's loopback, which only a local
        test relay does. The lease credential rides the same way to the lease
        service, under the same rule.

        With a lease service the relay's names come from each lease answer
        (``hostUrl`` and ``crewUrl``), so ``host_url`` and ``crew_domain`` may stay
        empty. With no lease URL and neither name, the Crew leases from the built-in
        URL (:func:`~kiro_crew.relay_client.lease.built_in_lease_url`). An edition
        that names the relay must name both: no name is guessed.
        """
        if not self.lease_url and not self.uses_built_in_lease:
            for name, value in (
                ("the relay host URL", self.host_url),
                ("the relay Crew domain", self.crew_domain),
            ):
                if not value:
                    raise RelaySettingsError(
                        f"{name} is not set, and with no tunnel.relay_lease_url nothing "
                        "else names the relay"
                    )
        if self.host_url:
            validate_host_url(self.host_url)
        if self.crew_domain and (
            len(self.crew_domain) > _CREW_DOMAIN_MAX_CHARS
            or not CREW_HOST_RE.match(self.crew_domain)
        ):
            raise RelaySettingsError(
                f"the relay Crew domain is not a host name: {self.crew_domain!r}"
            )
        if self.lease_url:
            problem = lease_url_problem(self.lease_url)
            if problem is not None:
                raise RelaySettingsError(f"tunnel.relay_lease_url: {problem}")
        if self.lease_url or self.uses_built_in_lease:
            if self.uses_sign_in and self.lease_credential and _workload_identity_named():
                # A workload host never leases as a person (``kiro_crew.workload_identity``).
                raise RelaySettingsError(
                    f"tunnel.relay_lease_credential is {SIGN_IN_CREDENTIAL}, but this Crew "
                    "runs on its workload identity: name the JWT-SVID file instead"
                )
            signs_in = self.uses_sign_in and not (
                # An empty credential is the sign-in only on a personal Crew: a host with
                # an agent workload identity keeps the refusal by name.
                not self.lease_credential
                and _agent_identity_enabled()
            )
            if signs_in:
                # The sign-in names its own TokenType.
                if self.lease_token_type:
                    raise RelaySettingsError(
                        "tunnel.relay_lease_token_type must be empty when "
                        f"tunnel.relay_lease_credential is {SIGN_IN_CREDENTIAL}"
                    )
            else:
                problem = credential_reference_problem(self.lease_credential)
                if problem is not None:
                    raise RelaySettingsError(problem)
            if self.lease_token_type and self.uses_workload_identity:
                # A team Crew's config copied into a pod: the JWT-SVID goes with no
                # TokenType whatever this says, so the pod still leases.
                logger.warning(
                    "tunnel.relay_lease_token_type is ignored: this Crew leases on its "
                    "workload identity, which sends no TokenType"
                )
            elif self.lease_token_type and self.lease_token_type not in TOKEN_TYPES:
                # Not echoed: a credential pasted in here by mistake stays out of logs.
                raise RelaySettingsError(
                    "tunnel.relay_lease_token_type must be empty or one of "
                    + ", ".join(sorted(TOKEN_TYPES))
                )
            if not signs_in:
                self.built_in_lease_url()

    def built_in_lease_url(self) -> str:
        """The built-in lease URL off the sign-in (whose profile names its own): ``""`` there.

        Every other Crew, a crew key or a JWT-SVID lease alike, leases from
        ``relay_lease_region``, else us-east-1. Refused: a Region outside the table.
        """
        if not self.uses_built_in_lease or self.uses_sign_in:
            return ""
        from kiro_crew.auth.login.control_plane import (
            CONTROL_PLANE_REGIONS,
            known_control_plane_url,
        )

        region, name = self.lease_region, "tunnel.relay_lease_region"
        if not region:
            # shortcut: the built-in default, so a Crew set up before the setting keeps
            # leasing as it did; a key from another Region fails there until the setting
            # names it. Refuse an empty one once every such Crew names its Region.
            region = DEFAULT_LEASE_REGION
        url = known_control_plane_url(region)
        if not url:
            # The value is not echoed, like the token type's: a pasted credential stays out.
            raise RelaySettingsError(
                f"{name} names no Region the built-in lease service covers "
                f"({', '.join(CONTROL_PLANE_REGIONS)}): name one, or set tunnel.relay_lease_url"
            )
        return url

    def lease_region_problem(self) -> Optional[str]:
        """Why :meth:`built_in_lease_url` refuses, or ``None``; for a status to show."""
        try:
            self.built_in_lease_url()
        except RelaySettingsError as exc:
            return str(exc)
        return None

    @property
    def uses_built_in_lease(self) -> bool:
        """Whether the Crew leases from the built-in URL: no lease URL and no relay names.

        An edition that sets ``host_url`` or ``crew_domain`` never gets it.
        """
        return not (self.lease_url or self.host_url or self.crew_domain)

    @property
    def uses_sign_in(self) -> bool:
        """Whether the Crew leases on its own Kiro sign-in: a personal Crew.

        ``kiro-sign-in`` says so, and so does an empty credential with no token type.
        A team Crew names its type (``API_KEY``), so an empty credential there is
        still refused by name; so is one on a host with an agent workload identity,
        which :meth:`validate` checks when the provider starts.
        """
        if self.lease_credential == SIGN_IN_CREDENTIAL:
            return True
        return not (self.lease_credential or self.lease_token_type)

    @property
    def uses_workload_identity(self) -> bool:
        """Whether the Crew leases on its own JWT-SVID: the SVID file on a workload host.

        On a host with ``KIROCREW_WORKLOAD_IDENTITY`` set, ``tunnel.relay_lease_credential``
        naming the file ``KIROCREW_WORKLOAD_IDENTITY_SVID_FILE`` names is the JWT-SVID
        (:mod:`kiro_crew.workload_identity`). Any other file, and a ``secret://``
        reference, stays a crew API key. Reads the environment only.
        """
        credential = self.lease_credential
        if not credential or credential == SIGN_IN_CREDENTIAL:
            return False
        if credential.startswith(SECRET_URI_PREFIX) or not _workload_identity_named():
            return False
        from kiro_crew import workload_identity

        svid_file = (os.environ.get(workload_identity.ENV_SVID_FILE) or "").strip()
        return bool(svid_file) and _same_path(credential, svid_file)

    @property
    def is_team_crew(self) -> bool:
        """Whether this Crew leases as a team Crew, on a crew API key.

        Rules meant for personal Remote Control can use it to skip team Crews.
        """
        return self.lease_token_type == TOKEN_TYPE_API_KEY

    def crew_url_for(self, endpoint_id: str) -> str:
        """The Crew's relay URL from ``crew_domain``.

        Only reached when the credential names no ``crewUrl``. A lease answer
        always does, so with the domain empty this refuses rather than guess.
        """
        if not self.crew_domain:
            raise RelaySettingsError(
                "the relay Crew domain is not set and the host token names no Crew URL"
            )
        return f"https://{endpoint_id}.{self.crew_domain}"


def validate_host_url(url: str) -> None:
    problem = host_url_problem(url)
    if problem is not None:
        raise RelaySettingsError(f"the relay host URL: {problem}")


def _setting_text(tunnel: Any, name: str) -> str:
    value = getattr(tunnel, name, "")
    # ``isinstance``: a stand-in config object answers any attribute with an object.
    return value.strip() if isinstance(value, str) else ""


def _same_path(a: str, b: str) -> bool:
    """Whether *a* and *b* name one path, as written (``~`` expanded, ``..`` folded)."""
    return os.path.normpath(os.path.expanduser(a)) == os.path.normpath(os.path.expanduser(b))


def _workload_identity_named() -> bool:
    from kiro_crew import workload_identity

    return workload_identity.active()


def _agent_identity_enabled() -> bool:
    """Whether this host has a workload identity, so it must not lease as a person.

    True when the deployment names the Crew's own workload identity
    (:func:`kiro_crew.workload_identity.active`) or the platform's agent identity
    seam reports one. Read when the provider starts, never while the context is
    being composed. A failed lookup counts as a workload identity, so it never
    falls to the sign-in.
    """
    from kiro_crew import workload_identity
    from kiro_crew.platform.context import current_context, safe_context_call

    if workload_identity.active():
        return True

    return bool(
        safe_context_call(
            lambda: current_context().agent_identity.enabled(),
            fallback=True,
            log_message="agent_identity.enabled lookup failed; refusing the sign-in default",
        )
    )


def gateway_listener() -> tuple[str, int]:
    """The gateway's own loopback TCP listener, as the relay client dials it.

    Both halves come from the socket the gateway actually bound, never from the
    address it was asked for (``KIROCREW_BIND``), which the bind policy may have
    refused. The listener exports them together once it binds:
    ``KIROCREW_BOUND_PORT``, the truth even under ``--port auto``, and
    ``KIROCREW_BOUND_HOST``, absent for an IPv4 loopback or wildcard bind (reached
    at 127.0.0.1), ``::1`` for an IPv6 one, else the specific address bound.
    The dashboard's relay ingress honours a hand-off only over loopback TCP, so a
    gateway bound to one specific non-loopback address cannot serve the relay and
    is refused here.
    """
    raw = os.environ.get("KIROCREW_BOUND_PORT", "").strip()
    if not raw.isdigit() or not 0 < int(raw) < 65536:
        raise RelaySettingsError("the gateway's bound port is not known yet")
    host = os.environ.get("KIROCREW_BOUND_HOST", "").strip() or "127.0.0.1"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and is_loopback_host(host):
        return str(address), int(raw)
    raise RelayBindUnreachable(
        f"the relay hands requests over loopback, but the gateway is bound to {host!r}"
    )


#: The dashboard app's relay ingress, published by the gateway when it starts the
#: tunnel. Process state: one gateway serves one dashboard app.
_gateway_ingress: Optional["GatewayRelayIngress"] = None


def publish_gateway_ingress(ingress: Optional["GatewayRelayIngress"]) -> None:
    """Record the dashboard app's relay ingress for the relay client to stamp through.

    Called once by the gateway, before the tunnel starts. ``None`` (the headless
    API server, which builds no ingress) leaves the relay client unable to start.
    """
    global _gateway_ingress
    _gateway_ingress = ingress


def gateway_ingress() -> Optional["GatewayRelayIngress"]:
    return _gateway_ingress


IngressFactory = Callable[[], CrewIngress]


class RelayTunnelProvider:
    """The ``TunnelProvider`` that holds this Crew's host connection to the Kiro relay."""

    #: Marks the Kiro relay's provider for readers that must not import this module.
    kiro_relay = True

    def __init__(
        self,
        settings: RelaySettings,
        *,
        token_source: Optional[HostTokenSource] = None,
        ingress_factory: Optional[IngressFactory] = None,
        client_version: Optional[str] = None,
        client_factory: Optional[Callable[..., RelayClient]] = None,
    ) -> None:
        self._settings = settings
        self._tokens = token_source or UnavailableHostTokenSource()
        self._ingress_factory = ingress_factory
        self._client_version = client_version
        self._client_factory = client_factory or RelayClient
        self._client: Optional[RelayClient] = None
        self._ingress: Optional[CrewIngress] = None
        self._on_connect: Optional[Callable[[str], Awaitable[None]]] = None
        self._on_disconnect: Optional[Callable[[], Awaitable[None]]] = None
        self._error = ""
        #: The status state a start refusal reports: an error, or stopped for a bind
        #: a default personal Crew cannot host on yet.
        self._error_state = STATE_ERROR

    @property
    def settings(self) -> RelaySettings:
        return self._settings

    def enabled(self) -> bool:
        return self._settings.enabled

    def register_callbacks(
        self,
        *,
        on_connect: Optional[Callable[[str], Awaitable[None]]] = None,
        on_disconnect: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        if self._client is not None:
            self._client.register_callbacks(on_connect=on_connect, on_disconnect=on_disconnect)

    async def start(self) -> None:
        if self._client is not None and self._client.running:
            return
        if not self._settings.enabled:
            return
        try:
            self._settings.validate()
            ingress = self._ingress_factory() if self._ingress_factory else self._loopback()
        except RelaySettingsError as exc:
            # Nobody chose to host a default personal Crew, so a bind the relay
            # cannot reach leaves it stopped, said as a WARNING; the next start with
            # a loopback or wildcard bind hosts. Every other refusal is an error.
            quiet = self._settings.personal_default and isinstance(exc, RelayBindUnreachable)
            logger.log(
                logging.WARNING if quiet else logging.ERROR,
                "Remote Control relay not started: %s",
                exc,
            )
            self._error = str(exc)
            self._error_state = STATE_STOPPED if quiet else STATE_ERROR
            return
        self._error = ""
        self._ingress = ingress
        client = self._client_factory(
            host_url=self._settings.host_url,
            crew_url_for=self._settings.crew_url_for,
            token_source=self._tokens,
            ingress=ingress,
            client_version=self._client_version or _crew_version(),
        )
        client.register_callbacks(on_connect=self._on_connect, on_disconnect=self._on_disconnect)
        self._client = client
        await client.start()

    @staticmethod
    def _loopback() -> CrewIngress:
        gateway = gateway_ingress()
        if gateway is None or not gateway.enabled:
            raise RelaySettingsError("the dashboard's relay ingress is not available")
        host, port = gateway_listener()
        return LoopbackIngress(
            host, port, gateway=gateway, max_ws_message_bytes=WS_MESSAGE_MAX_BYTES
        )

    async def stop(self) -> None:
        client, self._client = self._client, None
        ingress, self._ingress = self._ingress, None
        try:
            if client is not None:
                await client.stop()
        finally:
            # Also when the gateway's stop budget cancels the client's stop: the
            # lease renewals and the hand-off sessions must not outlive the tunnel.
            try:
                await self._tokens.aclose()
            finally:
                if ingress is not None:
                    await ingress.aclose()

    def public_url(self) -> str:
        return self._client.public_url if self._client is not None else ""

    def status_snapshot(self) -> Optional[Dict[str, Any]]:
        if self._client is not None:
            return self._client.snapshot()
        if self._error:
            return {
                "state": self._error_state,
                "url": "",
                "error": self._error,
                # Only the quiet bind refusal is a wait on this Crew, not a fault.
                "cannot_host_yet": self._error_state == STATE_STOPPED,
            }
        return None

    async def ensure_available(self, *, install: bool = True) -> str:
        """Report whether the relay URL is live; never start the client from here.

        The on-demand link path calls this without passing ``tunnel/setup.py``'s
        gates, so starting here would open a public path past the token-auth and
        ``--no-tunnel`` checks. The client starts only through the tunnel wrapper.
        """
        if self._client is None or not self._settings.enabled:
            return "disabled"
        state = self._client.state
        if state == STATE_CONNECTED:
            return "connected"
        if state == STATE_ERROR or not self._client.running:
            return "unavailable"
        return "starting"


def _crew_version() -> str:
    from kiro_crew import __version__

    return __version__


def lease_token_source(settings: RelaySettings) -> Optional[HostTokenSource]:
    """The lease client for *settings*, or ``None`` when an edition names the relay.

    An empty ``lease_url`` leases from the built-in URL for the Crew's Region.
    Construction does no I/O: the credential, the instance ID and the first call
    all wait for the relay client's first request for a host token.
    """
    if not settings.lease_url and not settings.uses_built_in_lease:
        return None
    if settings.uses_sign_in:
        return _sign_in_lease_source(settings)
    if settings.uses_workload_identity:
        return LeaseHostTokenSource(
            lease_url=settings.lease_url,
            credential=settings.lease_credential,
            crew_version=_crew_version(),
            workload=True,
            built_in_url="" if settings.lease_region_problem() else settings.built_in_lease_url(),
        )
    return LeaseHostTokenSource(
        lease_url=settings.lease_url,
        credential=settings.lease_credential,
        crew_version=_crew_version(),
        # An unknown type is refused by validate() when the provider starts, so the
        # composition root, which builds this before any start, never raises here.
        token_type=settings.lease_token_type if settings.lease_token_type in TOKEN_TYPES else None,
        # A refused Region: "", and every call stops before the credential goes out.
        built_in_url="" if settings.lease_region_problem() else settings.built_in_lease_url(),
    )


def _sign_in_lease_source(settings: RelaySettings) -> HostTokenSource:
    """A personal Crew's lease client, on the Crew's own Kiro sign-in.

    Each call reads the sign-in (refreshed as needed), and the profile ARN that
    read named goes on the same call. The sign-in module loads only here.
    """
    from kiro_crew.relay_client.lease_signin import LeaseSignIn, read_lease_sign_in

    latest: dict[str, str] = {}

    async def sign_in(rejected: Optional[str]) -> LeaseSignIn:
        signed_in = await read_lease_sign_in(rejected)
        latest["profile_arn"] = signed_in.profile_arn
        return signed_in

    return LeaseHostTokenSource(
        lease_url=settings.lease_url,
        credential=settings.lease_credential,
        crew_version=_crew_version(),
        # validate() refuses a token type beside the sign-in; the sign-in names its own.
        token_type=None,
        profile_arn=lambda: latest.get("profile_arn"),
        sign_in=sign_in,
    )


def public_tunnel_provider(cfg: Any) -> RelayTunnelProvider:
    """The relay client the public composition root installs once the relay is selected.

    The composition root calls this only after ``edition_locks.relay_selected`` says
    yes. Construction does no I/O and starts nothing, because the composition root
    also backs the lazy default context of every short-lived subprocess.
    """
    settings = RelaySettings.from_config(cfg)
    return RelayTunnelProvider(settings, token_source=lease_token_source(settings))
