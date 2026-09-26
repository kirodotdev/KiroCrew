"""Peer transports: one object per way the gateway reaches a remote crew.

``resolve_peer_transport`` is the only mapping from ``Instance.connection_method``
to behaviour. Registering a transport here is what makes a method real; the
registry's keys are asserted by a test, so a new method cannot arrive by accident
and an existing one cannot silently disappear.

The lookup is exhaustive and fail-closed: an unmapped method raises
:class:`UnknownTransportError` instead of falling through to SSH. That is a
behaviour change against the conditional this replaces, and a deliberate one —
inheriting SSH's addressing, SSH's mint and SSH's diagnosis by default is the
defect class RFC §3.9 documents on the dashboard side, where a ``fargate`` crew
reported ``transport: ssh``. An absent method still means SSH: that is the
documented default for a record written before the field existed, and
:func:`normalize_method` is where the two cases are told apart.
"""

from __future__ import annotations

from kiro_crew.instances.registry import CONNECTION_METHODS
from kiro_crew.instances.transports.base import (
    ChildForwardTransport,
    PeerTransport,
    TransportRefusal,
    TransportSeams,
)
from kiro_crew.instances.transports.fargate import FargateTransport
from kiro_crew.instances.transports.params import TransportParams
from kiro_crew.instances.transports.ssh import SshTransport
from kiro_crew.instances.transports.ssm import SsmTransport
from kiro_crew.instances.validation import SshValidationError, SsmValidationError

#: The method assumed when a record carries none — records predate the field.
DEFAULT_METHOD = "ssh"


class UnknownTransportError(SshValidationError, SsmValidationError):
    """Raised for a ``connection_method`` no transport is registered for.

    Inherits both validation errors on purpose: every caller that resolves a
    transport already catches ``(SshValidationError, SsmValidationError)`` to turn
    a bad record into a clean per-instance error, so an unmapped method is
    reported the same way rather than escaping as an unhandled 500.
    """


_TRANSPORTS: dict[str, PeerTransport] = {
    "ssh": SshTransport(),
    "ssm": SsmTransport(),
    "fargate": FargateTransport(),
}


def normalize_method(value: str | None) -> str:
    """Normalize a record's ``connection_method`` for lookup.

    Absent, empty or whitespace means :data:`DEFAULT_METHOD`; anything else is
    lowercased and stripped but NOT mapped to a default, so an unrecognized value
    reaches :func:`resolve_peer_transport` and is refused there.
    """
    return (value or DEFAULT_METHOD).strip().lower() or DEFAULT_METHOD


def resolve_peer_transport(method: str | None) -> PeerTransport:
    """Return the transport owning *method*, or raise :class:`UnknownTransportError`."""
    normalized = normalize_method(method)
    transport = _TRANSPORTS.get(normalized)
    if transport is None:
        raise UnknownTransportError(
            f"unsupported connection_method {normalized!r}: "
            f"must be one of {tuple(sorted(_TRANSPORTS))}"
        )
    return transport


def registered_methods() -> tuple[str, ...]:
    """The registered method names, in registration order."""
    return tuple(_TRANSPORTS)


__all__ = [
    "CONNECTION_METHODS",
    "DEFAULT_METHOD",
    "ChildForwardTransport",
    "FargateTransport",
    "PeerTransport",
    "SshTransport",
    "SsmTransport",
    "TransportParams",
    "TransportRefusal",
    "TransportSeams",
    "UnknownTransportError",
    "normalize_method",
    "registered_methods",
    "resolve_peer_transport",
]
