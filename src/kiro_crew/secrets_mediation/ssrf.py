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
import threading
import time
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

#: Cap on concurrently-outstanding ``mediated-dns-resolve`` worker threads.
#: ``socket.getaddrinfo`` has no timeout and no cancellation, so a resolver that
#: stalls for an authorized hostname leaves a LIVE daemon thread behind on every
#: deadline overrun — it exits only when the C call itself returns. Without a
#: bound, repeated short-timeout mediated calls to that name would pile up
#: abandoned threads until the process hits ``RuntimeError: can't start new
#: thread`` and UNRELATED gateway requests begin to crash. This bounds the leak
#: the same way the HTTP side bounds its worker (whose live socket is force-closed
#: on overrun): a slot is held for the whole life of the resolver thread, released
#: only when ``getaddrinfo`` finally returns, so a stalled thread keeps occupying
#: its slot. At the cap, a new lookup is refused on the ordinary resolution-failure
#: path rather than spawning another abandoned thread. Kept well below the mediated
#: capability's 256-outstanding ceiling: a credential-bearing lookup is short and a
#: handful in flight at once is already pathological.
_MAX_INFLIGHT_RESOLVERS = 16
_resolver_slots = threading.BoundedSemaphore(_MAX_INFLIGHT_RESOLVERS)


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


def _resolve_within_deadline(
    host: str, port: int, deadline: float
) -> list[tuple[int, int, int, str, tuple]]:
    """Resolve *host*:*port* without letting the lookup outlive *deadline*.

    ``socket.getaddrinfo`` is a blocking C call with no timeout and no
    cancellation, so a hostile resolver for an authorized name can stall it for
    an origin-chosen duration. Left inside the mediated call's padded region that
    stall becomes a timing channel: the deadline padding only waits UP TO the
    normalized budget, so a resolution that overruns it pushes total completion
    PAST the deadline by an origin-controlled amount — one bit of a credential the
    origin already learned. Running the lookup on a worker thread and waiting on
    it only until ``deadline`` makes resolution latency bounded by the same
    normalized budget as every other outcome: it can never extend completion past
    the deadline, so it carries no origin-attributable bit. An overrun raises
    :class:`SsrfError`, which the caller collapses to the padded withheld
    constant. The abandoned thread is a daemon and finishes harmlessly.
    """
    result: list = []
    error: list[BaseException] = []

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        # Budget already spent: refuse without consulting the resolver at all, so
        # a spent deadline never even starts an origin-controlled lookup.
        raise SsrfError("The request URL host could not be resolved within the time budget.")

    def _lookup() -> None:
        try:
            result.append(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller thread
            error.append(exc)
        finally:
            # Release the slot HERE, on the worker, not on the caller: an overrun
            # abandons this thread while getaddrinfo is still blocked, and the slot
            # must stay held until the C call actually returns. Releasing on the
            # caller's overrun path would free a slot a still-live thread occupies,
            # defeating the cap on exactly the stall it exists to bound.
            _resolver_slots.release()

    # Cap outstanding resolver workers: at the ceiling, refuse this lookup on the
    # ordinary resolution-failure path instead of spawning another uncancellable
    # daemon thread. acquire(blocking=False) returns False when no slot is free.
    if not _resolver_slots.acquire(blocking=False):
        raise SsrfError("The request URL host could not be resolved within the time budget.")

    worker = threading.Thread(target=_lookup, name="mediated-dns-resolve", daemon=True)
    try:
        worker.start()
    except RuntimeError:
        # The platform could not start the thread (e.g. the leak this cap exists to
        # prevent already happened elsewhere). Release the slot we reserved and
        # refuse rather than leak it.
        _resolver_slots.release()
        raise SsrfError("The request URL host could not be resolved within the time budget.")
    worker.join(timeout=remaining)
    if worker.is_alive():
        # The lookup did not finish inside the budget. Refuse rather than block:
        # the daemon thread cannot be cancelled, but it holds no secret, its
        # eventual completion is discarded, and it keeps its resolver slot until
        # getaddrinfo returns (released in _lookup's finally) so the cap bounds the
        # number of such stalled threads.
        raise SsrfError("The request URL host could not be resolved within the time budget.")
    if error:
        exc = error[0]
        if isinstance(exc, (OSError, UnicodeError)):
            raise SsrfError("The request URL host could not be resolved.") from None
        raise exc
    return result[0] if result else []


def check_url(url: str, deadline: float) -> PinnedTarget:
    """Validate *url* for a mediated outbound request and pin its address.

    Returns a :class:`PinnedTarget` (host + single public IP + port) or raises
    :class:`SsrfError`. Performs exactly one DNS resolution and selects one
    public address; the caller must connect to ``target.ip`` (not re-resolve
    ``target.host``) so the connection cannot be rebound to an internal address
    between this check and the socket.

    *deadline* is the mediated call's absolute (``time.monotonic``) budget: the
    DNS resolution is bounded so it cannot outlive it, keeping resolution latency
    constant with respect to the secret (see :func:`_resolve_within_deadline`).

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

    # Single resolution, bounded so it cannot outlive the mediated call's
    # deadline. Every returned address must be public; if ANY resolves to a
    # blocked range we refuse the whole target rather than cherry-picking a
    # public record (a resolver that mixes a public and a private A record is
    # exactly the rebinding shape we fail closed on).
    infos = _resolve_within_deadline(host, port, deadline)
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
