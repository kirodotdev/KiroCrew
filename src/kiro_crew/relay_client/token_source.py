"""Where the relay client gets its host token.

The host token lets one Crew hold the host connection for its endpoint. It comes
from the lease (``RenewCrewLease``). The client only ever talks to a
:class:`HostTokenSource`, so where the token comes from never touches the
connection code. The lease client is
:class:`~kiro_crew.relay_client.lease.LeaseHostTokenSource`; this module ships the
seam and two more sources:

* :class:`StaticHostTokenSource` holds one credential, for tests and for driving a
  fake relay by hand. :meth:`StaticHostTokenSource.publish` stands in for a lease
  renewal that brought a new host token.
* :class:`UnavailableHostTokenSource` is the default while no lease service is
  configured: it refuses, so turning the relay on without a source reports why it
  cannot connect instead of dialling with nothing.

A credential is never logged and never part of a ``repr``.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional, Protocol, Tuple

from kiro_crew.relay_client.protocol import crew_url_problem, host_url_problem

NewTokenCallback = Callable[["HostCredential"], Awaitable[None]]
#: Answers whether a host connection is up right now (from ``ready`` until it ends).
RelayConnected = Callable[[], bool]


@dataclass(frozen=True)
class HostCredential:
    """A host token, plus what a lease answer names beside it.

    ``host_url`` is the relay's dial URL and ``crew_url`` the Crew's own URL on the
    relay; a source that leaves either as ``None`` lets the client fall back to its
    settings. ``crew_kind`` (``personal`` or ``team``) and ``owner_user_id`` (a
    personal Crew's owner) bind the host connection in the dashboard's relay
    ingress; a source that leaves them as ``None`` lets the client read them from
    the host token's own ``kind`` and ``sub`` claims.
    """

    token: str = field(repr=False)
    host_url: Optional[str] = None
    crew_url: Optional[str] = None
    crew_kind: Optional[str] = None
    owner_user_id: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.token, str) or not self.token or not self.token.isascii():
            raise ValueError("a host token is a non-empty ASCII string")
        if any(ch in self.token for ch in "\r\n "):
            raise ValueError("a host token carries no whitespace")
        # Checked here, where a source hands them over, so no credential the
        # client holds can send the host token over clear text or put an
        # arbitrary origin into the dashboard's allowed origins.
        if self.host_url is not None:
            problem = host_url_problem(self.host_url)
            if problem is not None:
                raise ValueError(problem)
        if self.crew_url is not None:
            problem = crew_url_problem(self.crew_url)
            if problem is not None:
                raise ValueError(problem)


def token_claims(token: str) -> dict:
    """The host token's payload, read but not verified: the relay verifies it.

    The client reads only ``kind`` and ``sub``, which say whose Crew this is, to
    tell the dashboard's relay ingress who the owner is, and the lease client reads
    ``iat`` and ``exp`` for the token's lifetime. Nothing here trusts the token for
    access; the relay checks its signature on every dial.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (binascii.Error, ValueError):
        return {}
    return claims if isinstance(claims, dict) else {}


def host_binding(credential: HostCredential) -> Optional[Tuple[str, str]]:
    """``(crew_kind, owner_user_id)`` for *credential*, or ``None`` when it names none.

    A team Crew has no relayed owner, so its owner is ``""``.
    """
    kind = credential.crew_kind
    owner = credential.owner_user_id
    if kind is None:
        claims = token_claims(credential.token)
        kind = claims.get("kind")
        if owner is None and kind == "personal":
            owner = claims.get("sub")
    if kind == "team":
        return "team", ""
    if kind == "personal" and isinstance(owner, str) and owner:
        return "personal", owner
    return None


class HostTokenUnavailable(Exception):
    """No host token can be had, and retrying will not change that.

    The client stops and reports the message, for example when no source is
    configured or the backend refused this Crew for good.
    """


class HostTokenRetryLater(Exception):
    """No host token right now, for a reason that can pass (the backend is down).

    The client backs off with the same steps as a reconnect and asks again.
    """


class HostTokenSource(Protocol):
    """The seam between the relay client and whatever renews the host token."""

    async def current(self) -> HostCredential:
        """The newest host token this source holds, fetching one if it has none."""
        ...

    async def renew(self, *, host_token_requested: bool) -> HostCredential:
        """Ask for a new host token now.

        ``host_token_requested`` is ``True`` when the client holds no usable host
        token, after the relay refused one or let one expire, so the source asks
        the backend to issue one rather than waiting for the usual renewal.
        """
        ...

    def on_new_token(self, callback: Optional[NewTokenCallback]) -> None:
        """Register the callback that sends each new host token down the connection."""
        ...

    def track_relay(self, connected: Optional[RelayConnected]) -> None:
        """Register what says whether a host connection is up right now.

        A lease renewal reports it as ``relay.connected``. ``None`` unregisters it.
        """
        ...

    async def aclose(self) -> None:
        """Stop any renewals in the background and release what the source holds."""
        ...


class StaticHostTokenSource:
    """One fixed credential, replaced only through :meth:`publish`."""

    def __init__(self, credential: HostCredential) -> None:
        self._credential = credential
        self._callback: Optional[NewTokenCallback] = None
        self.renewals: list[bool] = []

    async def current(self) -> HostCredential:
        return self._credential

    async def renew(self, *, host_token_requested: bool) -> HostCredential:
        self.renewals.append(host_token_requested)
        return self._credential

    def on_new_token(self, callback: Optional[NewTokenCallback]) -> None:
        self._callback = callback

    def track_relay(self, connected: Optional[RelayConnected]) -> None:
        return None

    async def aclose(self) -> None:
        return None

    async def publish(self, credential: HostCredential) -> None:
        """Hold *credential* from now on, and hand it to the client as a new token."""
        self._credential = credential
        if self._callback is not None:
            await self._callback(credential)


class UnavailableHostTokenSource:
    """The default source: no lease service is configured, so there is no host token."""

    MESSAGE = (
        "Remote Control has no host token source: set tunnel.relay_lease_url and "
        "tunnel.relay_lease_credential so the Crew can hold a lease"
    )

    async def current(self) -> HostCredential:
        raise HostTokenUnavailable(self.MESSAGE)

    async def renew(self, *, host_token_requested: bool) -> HostCredential:
        raise HostTokenUnavailable(self.MESSAGE)

    def on_new_token(self, callback: Optional[NewTokenCallback]) -> None:
        return None

    def track_relay(self, connected: Optional[RelayConnected]) -> None:
        return None

    async def aclose(self) -> None:
        return None
