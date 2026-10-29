"""The relay client in the tunnel provider slot, and the rule that puts it there.

:class:`RelayTunnelProvider` implements the ``TunnelProvider`` protocol
(``kiro_crew.platform.interfaces``) over a :class:`~kiro_crew.relay_client.client.RelayClient`.
The tunnel wrapper (``tunnel/manager.py``) drives it like any provider: it registers
the connect and disconnect callbacks, which put the Crew's relay URL into the allowed
origins, and it reaches ``start()`` only through ``tunnel/setup.py``, whose gates
refuse to start any provider under ``--no-tunnel`` or without dashboard token auth.

:func:`public_tunnel_provider` is the selection rule the public composition root
calls. It returns this provider only when ``tunnel.relay_enabled`` is on (off by
default) and the build does not lock local enables, and the no-op
``DefaultTunnelProvider`` otherwise. The provider's host token source is the lease
client when ``tunnel.relay_lease_url`` names a lease service, and the refusing
default otherwise. An edition that composes its own context puts
its own provider in the slot and never reaches this function, so the relay client is
never chosen over an edition's provider.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Optional

from kiro_crew.config.fields import _safe_float
from kiro_crew.relay_client.client import STATE_CONNECTED, STATE_ERROR, RelayClient
from kiro_crew.relay_client.connection import RelayTimings
from kiro_crew.relay_client.ingress import CrewIngress, LoopbackIngress
from kiro_crew.relay_client.lease import (
    LeaseHostTokenSource,
    credential_reference_problem,
    lease_url_problem,
)
from kiro_crew.relay_client.protocol import (
    CREW_HOST_RE,
    MOVE_QUIET_GRACE_MAX_SECONDS,
    MOVE_QUIET_GRACE_SECONDS,
    WS_MESSAGE_MAX_BYTES,
    host_url_problem,
    is_loopback_host,
)
from kiro_crew.relay_client.token_source import HostTokenSource, UnavailableHostTokenSource

if TYPE_CHECKING:
    from kiro_crew.dashboard.kiro_relay.ingress import RelayIngress as GatewayRelayIngress

logger = logging.getLogger(__name__)

#: The default Region, and the shapes the provisional relay names take in it.
DEFAULT_REGION = "us-east-1"
HOST_URL_TEMPLATE = "wss://kiro-relay.{region}.api.aws/v1/host"
CREW_DOMAIN_TEMPLATE = "kiro-relay.{region}.on.aws"
_REGION_RE = re.compile(r"\A[a-z]{2}(-[a-z]+)+-[0-9]+\Z", re.ASCII)
#: Room for one endpoint label (63) and its dot inside a 253-character host.
_CREW_DOMAIN_MAX_CHARS = 189


class RelaySettingsError(ValueError):
    """A relay setting that cannot be used; the message names the setting."""


@dataclass(frozen=True)
class RelaySettings:
    """The relay's names for this Crew, resolved from ``config.json``'s ``tunnel`` section.

    Every name is a setting, because the relay's names are provisional and differ
    by stage. The lease answer's ``hostUrl`` and ``crewUrl`` take precedence over
    ``host_url`` and ``crew_domain``. ``lease_url`` names the lease service; while
    it is empty there is no host token source. ``lease_credential`` says where the
    Crew's credential for that service is, never the credential itself.
    """

    enabled: bool
    region: str
    host_url: str
    crew_domain: str
    #: Seconds an old connection stays open, quiet, after a move (0 to 30).
    move_grace: float = MOVE_QUIET_GRACE_SECONDS
    lease_url: str = ""
    lease_credential: str = field(default="", repr=False)

    @classmethod
    def from_config(cls, cfg: Any) -> "RelaySettings":
        tunnel = getattr(cfg, "tunnel", None)
        region = str(getattr(tunnel, "relay_region", "") or DEFAULT_REGION).strip()
        host_url = str(getattr(tunnel, "relay_host_url", "") or "").strip()
        crew_domain = str(getattr(tunnel, "relay_crew_domain", "") or "").strip().lower()
        return cls(
            # ``is True``: a stand-in config object answers any attribute truthily.
            enabled=getattr(tunnel, "relay_enabled", False) is True,
            region=region,
            host_url=host_url or HOST_URL_TEMPLATE.format(region=region),
            crew_domain=crew_domain or CREW_DOMAIN_TEMPLATE.format(region=region),
            move_grace=_safe_float(
                getattr(tunnel, "relay_move_grace_seconds", MOVE_QUIET_GRACE_SECONDS),
                MOVE_QUIET_GRACE_SECONDS,
                0.0,
                MOVE_QUIET_GRACE_MAX_SECONDS,
            ),
            lease_url=_setting_text(tunnel, "relay_lease_url"),
            lease_credential=_setting_text(tunnel, "relay_lease_credential"),
        )

    def validate(self) -> None:
        """Raise :class:`RelaySettingsError` for a setting the client must not use.

        The host token rides in the dial's ``Authorization`` header, so the dial is
        ``wss://`` (TLS) unless it names this machine's loopback, which only a local
        test relay does. The lease credential rides the same way to the lease
        service, under the same rule.
        """
        if not _REGION_RE.match(self.region):
            raise RelaySettingsError(f"tunnel.relay_region is not a Region name: {self.region!r}")
        validate_host_url(self.host_url)
        if len(self.crew_domain) > _CREW_DOMAIN_MAX_CHARS or not CREW_HOST_RE.match(
            self.crew_domain
        ):
            raise RelaySettingsError(
                f"tunnel.relay_crew_domain is not a host name: {self.crew_domain!r}"
            )
        if self.lease_url:
            problem = lease_url_problem(self.lease_url)
            if problem is not None:
                raise RelaySettingsError(f"tunnel.relay_lease_url: {problem}")
            problem = credential_reference_problem(self.lease_credential)
            if problem is not None:
                raise RelaySettingsError(problem)

    def crew_url_for(self, endpoint_id: str) -> str:
        return f"https://{endpoint_id}.{self.crew_domain}"


def validate_host_url(url: str) -> None:
    problem = host_url_problem(url)
    if problem is not None:
        raise RelaySettingsError(f"tunnel.relay_host_url: {problem}")


def _setting_text(tunnel: Any, name: str) -> str:
    value = getattr(tunnel, name, "")
    # ``isinstance``: a stand-in config object answers any attribute with an object.
    return value.strip() if isinstance(value, str) else ""


def gateway_listener() -> tuple[str, int]:
    """The gateway's own loopback TCP listener, as the relay client dials it.

    The port is the one the gateway exported once it bound (``KIROCREW_BOUND_PORT``),
    which is the truth even under ``--port auto``. The dashboard's relay ingress
    honours a hand-off only over loopback TCP, so a gateway bound to one specific
    non-loopback address cannot serve the relay and is refused here.
    """
    raw = os.environ.get("KIROCREW_BOUND_PORT", "").strip()
    if not raw.isdigit() or not 0 < int(raw) < 65536:
        raise RelaySettingsError("the gateway's bound port is not known yet")
    bind = os.environ.get("KIROCREW_BIND", "").strip()
    if not bind or bind in ("*", "localhost"):
        return "127.0.0.1", int(raw)
    host = bind[1:-1] if bind.startswith("[") and bind.endswith("]") else bind
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if address.is_unspecified:
            return ("127.0.0.1" if address.version == 4 else "::1"), int(raw)
        if is_loopback_host(host):
            return str(address), int(raw)
    raise RelaySettingsError(
        f"the relay hands requests over loopback, but KIROCREW_BIND names {bind!r}"
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
            logger.error("Remote Control relay not started: %s", exc)
            self._error = str(exc)
            return
        self._error = ""
        self._ingress = ingress
        client = self._client_factory(
            host_url=self._settings.host_url,
            crew_url_for=self._settings.crew_url_for,
            token_source=self._tokens,
            ingress=ingress,
            client_version=self._client_version or _crew_version(),
            timings=RelayTimings(move_grace=self._settings.move_grace),
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
        if client is not None:
            await client.stop()
        await self._tokens.aclose()
        if ingress is not None:
            await ingress.aclose()

    def public_url(self) -> str:
        return self._client.public_url if self._client is not None else ""

    def status_snapshot(self) -> Optional[Dict[str, Any]]:
        if self._client is not None:
            return self._client.snapshot()
        if self._error:
            return {"state": STATE_ERROR, "url": "", "error": self._error}
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


def relay_selected(cfg: Any) -> bool:
    """Whether the public composition puts the relay client in the tunnel slot."""
    if not RelaySettings.from_config(cfg).enabled:
        return False
    from kiro_crew.edition_locks import local_enable_locked

    # An enterprise build ignores local enables for the tunnel (RFC D3), this one too.
    return not local_enable_locked("tunnel")


def lease_token_source(settings: RelaySettings) -> Optional[HostTokenSource]:
    """The lease client for *settings*, or ``None`` while no lease service is set.

    Construction does no I/O: the credential, the instance ID and the first call
    all wait for the relay client's first request for a host token.
    """
    if not settings.lease_url:
        return None
    return LeaseHostTokenSource(
        lease_url=settings.lease_url,
        credential=settings.lease_credential,
        crew_version=_crew_version(),
    )


def public_tunnel_provider(cfg: Any) -> Any:
    """The tunnel provider the public composition root installs.

    Construction does no I/O and starts nothing, because the composition root also
    backs the lazy default context of every short-lived subprocess.
    """
    if relay_selected(cfg):
        settings = RelaySettings.from_config(cfg)
        return RelayTunnelProvider(settings, token_source=lease_token_source(settings))
    from kiro_crew.platform.defaults import DefaultTunnelProvider

    return DefaultTunnelProvider()
