"""SSRF guard for the mediated-secret request tool.

The mediated tool performs an OUTBOUND HTTPS request that carries a resolved
vault secret. The destination is bound by the owner's per-secret policy to an
exact origin, but the address that origin RESOLVES to is attacker-influenceable
(DNS), so the origin allowlist alone is not enough — a name the owner authorized
today can be pointed at ``127.0.0.1`` or the cloud metadata endpoint tomorrow.

This module fails closed on any address that is loopback / private / link-local
/ reserved / unspecified / multicast, on the cloud metadata endpoints, and on a
non-``https`` scheme, and it PINS the resolved public IP so the socket connects
to the exact address that was validated — closing the validate-then-reconnect
(DNS-rebinding) window that a plain hostname check leaves open. Redirects are
never followed automatically; each hop is re-validated by the caller through
``check_url`` before another request is built, so a 3xx cannot move a
credential-bearing request to a different (or internal) origin.

Per-address classification is delegated to ``link_unfurl.address_is_not_public``,
the repo's single owner of "is this resolved address non-public", so this guard
does not re-enumerate the block ranges. What this module adds on top is specific
to a credential-bearing egress: an https-only + explicit-metadata-literal
pre-check before resolution, a single resolution that fails closed if ANY record
is non-public, and the pinned public IP so the socket connects to the exact
address that was validated — closing the validate-then-reconnect (DNS-rebinding)
window that a plain hostname check leaves open.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

from kiro_crew.link_unfurl import address_is_not_public

#: Hostnames that must never be reachable regardless of what they resolve to.
_BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "metadata.google.internal",
        # Cloud instance metadata, spelled as a name in some resolvers.
        "metadata",
    }
)

#: Cloud instance-metadata literals (IMDS). Kept as strings so a hostname that
#: is already one of these is refused before any resolution.
_METADATA_LITERALS = frozenset({"169.254.169.254", "fd00:ec2::254"})

#: The only scheme a credential-bearing request may use.
_REQUIRED_SCHEME = "https"

#: Bound the resolved address set we will consider, so a hostile resolver
#: returning thousands of records cannot make validation unbounded.
_MAX_RESOLVED_ADDRS = 16


@dataclass(frozen=True)
class PinnedTarget:
    """A validated, DNS-pinned destination for one request hop.

    ``host`` is the original hostname (sent as SNI / Host header); ``ip`` is the
    single public address the socket must connect to. ``port`` is the explicit
    or scheme-default port.
    """

    host: str
    ip: str
    port: int
    family: int


class SsrfError(Exception):
    """A destination was refused. The message is safe to surface to the agent;
    it never contains secret material (only the destination host/reason)."""


def _ip_is_blocked(addr: str) -> bool:
    """True for any resolved address a credential-bearing request must never reach.

    Delegates to :func:`kiro_crew.link_unfurl.address_is_not_public`, the repo's
    single owner of "is this resolved address non-public" — so this guard cannot
    drift from the ranges that predicate already handles (loopback, private,
    link-local, reserved, unspecified, multicast, plus RFC 6598 CGNAT
    ``100.64.0.0/10`` and ``fec0::/10``, and the alternate IPv4 encodings a
    resolver accepts). It fails closed on an address it cannot parse.
    """
    return address_is_not_public(addr)


def check_url(url: str) -> PinnedTarget:
    """Validate *url* for a mediated outbound request and pin its address.

    Returns a :class:`PinnedTarget` (host + single public IP + port) or raises
    :class:`SsrfError`. Performs exactly one DNS resolution and selects one
    public address; the caller must connect to ``target.ip`` (not re-resolve
    ``target.host``) so the connection cannot be rebound to an internal address
    between this check and the socket.

    This is a pure validator plus one read-only DNS lookup: it never opens the
    connection and never touches secret material.
    """
    if not isinstance(url, str) or not url.strip():
        raise SsrfError("A request URL is required.")
    parts = urlsplit(url.strip())
    if parts.scheme != _REQUIRED_SCHEME:
        raise SsrfError("Only https:// URLs are allowed for a mediated request.")
    host = (parts.hostname or "").lower()
    if not host:
        raise SsrfError("The request URL has no host.")
    if host in _BLOCKED_HOSTNAMES or host in _METADATA_LITERALS:
        raise SsrfError("The request URL host is not allowed (internal/metadata address).")

    try:
        port = parts.port or 443
    except ValueError:
        raise SsrfError("The request URL has an invalid port.")

    # Single resolution. Every returned address must be public; if ANY resolves
    # to a blocked range we refuse the whole target rather than cherry-picking a
    # public record (a resolver that mixes a public and a private A record is
    # exactly the rebinding shape we fail closed on).
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        raise SsrfError("The request URL host could not be resolved.")
    if not infos:
        raise SsrfError("The request URL host did not resolve to any address.")
    if len(infos) > _MAX_RESOLVED_ADDRS:
        raise SsrfError("The request URL host resolved to too many addresses.")

    pinned: PinnedTarget | None = None
    for family, _stype, _proto, _canon, sockaddr in infos:
        addr = sockaddr[0]
        if _ip_is_blocked(addr):
            raise SsrfError(
                "The request URL host resolves to a blocked (internal/metadata) address."
            )
        if pinned is None:
            pinned = PinnedTarget(host=host, ip=addr, port=port, family=family)

    if pinned is None:  # pragma: no cover - guarded by the empty check above
        raise SsrfError("The request URL host did not resolve to a usable address.")
    return pinned
