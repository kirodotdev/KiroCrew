"""Local loopback port allocation for SSH ``-L`` tunnels.

Each connected remote instance needs a distinct ``127.0.0.1:<local_port>`` for
its SSH forward. This allocator hands out the first free port at or above a base
(default 7778, see ``kiro_crew.instances.constants``), skipping ports that are
already bound on the loopback interface or reserved by a caller (e.g. ports the
registry has already assigned to other instances).

The "in use" probe binds the port on every loopback address momentarily; a
successful bind on all of them means the port is free. This is advisory — there
is an inherent TOCTOU window between probing and the tunnel actually binding —
so callers should treat a returned port as a best-effort suggestion and handle a
late bind failure by re-allocating.
"""

from __future__ import annotations

import errno
import logging
import socket
from collections.abc import Iterable

from kiro_crew.instances.constants import DEFAULT_TUNNEL_BASE_PORT

logger = logging.getLogger(__name__)

# Upper bound for the search; ports above this are ephemeral/unprivileged-noise
# and we should fail loudly rather than wander into them.
_MAX_PORT = 65535

# Loopback addresses a listener on this host can bind independently of each
# other. A port counts as free only when it is free on ALL of them: ``ssh -L``
# binds one address, so a process holding the same port on another loopback
# family leaves ``localhost:<port>`` ambiguous — which socket serves a given
# connection then depends on name resolution order and on platform bind
# precedence rather than on anything this process controls. Refusing such a port
# keeps the forward the only listener reachable at that port.
_LOOPBACK_PROBE_HOSTS: tuple[str, ...] = ("127.0.0.1", "::1")

# Bind errors that mean "this address does not exist here" rather than "occupied"
# — a family compiled in but not configured (no ::1), or not supported at all.
# Nothing can be listening on an address the host cannot assign, so these read as
# free instead of in use; treating them as in use would refuse every port on a
# host with IPv6 disabled.
_ADDRESS_UNUSABLE = frozenset({errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT})

# The same distinction one step earlier, at socket CREATION: these mean the
# kernel has no such protocol family, so nothing can be listening on it and the
# address reads as free. A kernel with IPv6 compiled out reports one or the other
# depending on the stack, which is why both are here. Every OTHER creation errno
# (EMFILE, ENFILE, ENOBUFS) means the probe could not be RUN — neither answer is
# true, so it propagates to the caller instead of being coerced into one.
_FAMILY_UNUSABLE = frozenset({errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT})


def _is_addr_free(port: int, host: str) -> bool:
    """Return True if *port* can be bound on *host* (i.e. nothing holds it there).

    Single-address primitive behind :func:`_is_port_free`. The socket family is
    inferred from *host*, so this probes the same address a listener would
    actually bind rather than assuming IPv4.

    Sets ``SO_REUSEADDR`` before probing so this check mirrors what the SSH
    forward listener actually does at bind time — OpenSSH sets ``SO_REUSEADDR``
    on its ``-L`` listener. This matters for the disconnect -> reconnect path:
    when a tunnel is torn down, ``_SshTunnel.stop()`` reaps the ``ssh`` child so
    the *listener* socket is gone, but the forward's **accepted** data
    connections (the embedded dashboard's WebSocket/API traffic) linger in
    ``TIME_WAIT`` on ``127.0.0.1:<port>`` for the OS's 2*MSL window (~30-60s),
    each still bound to that local addr:port. A probe *without* ``SO_REUSEADDR``
    fails to bind while any such ``TIME_WAIT`` socket exists, so the connect
    pre-flight would falsely report the just-freed port as "in use" and reject
    the reconnect — the observed symptom of having to "wait longer" before a
    just-disconnected instance can be reconnected. With ``SO_REUSEADDR`` set the
    probe matches ssh: a ``TIME_WAIT`` remnant is no longer a false positive,
    while a genuinely *live* listener (a real port collision between two
    connected instances) still fails to bind and is correctly reported in use
    (``SO_REUSEADDR`` exempts ``TIME_WAIT`` only, never an active ``LISTEN``).

    A bind failure (``OSError``) is interpreted as "in use / unavailable", except
    for the errnos in :data:`_ADDRESS_UNUSABLE`, which mean the address itself is
    not assignable on this host and therefore cannot be occupied.

    Socket creation is read against :data:`_FAMILY_UNUSABLE` in the same spirit:
    an absent protocol family cannot hold a listener, so it reads as free. Any
    other creation error PROPAGATES rather than being reported either way — it
    means the probe could not be run, which is neither "free" (that would hand
    out a port a listener may hold) nor "in use" (that would make the allocator
    walk every candidate and fail with a port-exhaustion message naming the wrong
    cause). Letting it out preserves the pre-dual-stack behavior, where a creation
    error was never caught at all.
    """
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        sock = socket.socket(family, socket.SOCK_STREAM)
    except OSError as e:
        if e.errno in _FAMILY_UNUSABLE:
            return True
        raise
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        return True
    except OSError as e:
        return e.errno in _ADDRESS_UNUSABLE
    finally:
        sock.close()


def _is_port_free(port: int) -> bool:
    """Return True if *port* is free on every loopback address.

    Probes all of :data:`_LOOPBACK_PROBE_HOSTS` and requires every one of them to
    be free — stricter than a single-family probe on purpose: a port that is free
    on ``127.0.0.1`` but held on ``::1`` is not safely ours, so it is reported in
    use. Over-refusing costs the allocator one skipped candidate; under-refusing
    hands out a port whose traffic can land in another process.

    This asks only the aggregate question. A caller that wants one address —
    orphan reclaim, which asks whether *our* ``ssh -L`` child released the
    ``127.0.0.1`` address it bound — calls :func:`_is_addr_free` directly rather
    than passing a host here, since with a single host the two are the same
    function.
    """
    return all(_is_addr_free(port, h) for h in _LOOPBACK_PROBE_HOSTS)


class PortAllocator:
    """Allocate free loopback ports starting from a base, skipping used ones."""

    def __init__(self, base_port: int = DEFAULT_TUNNEL_BASE_PORT) -> None:
        if not (1 <= base_port <= _MAX_PORT):
            raise ValueError(f"base_port {base_port} out of range [1, {_MAX_PORT}]")
        self._base = base_port

    @property
    def base_port(self) -> int:
        return self._base

    def allocate(self, exclude: Iterable[int] | None = None) -> int:
        """Return the first free loopback port >= base not in *exclude*.

        *exclude* is a set of ports the caller knows are taken (e.g. local_port
        values already assigned to other instances in the registry) — these are
        skipped even if a momentary probe would find them bindable, so two
        instances are never handed the same port between connect calls.

        Raises :class:`RuntimeError` if no free port is found up to ``65535``.
        """
        reserved = set(exclude or ())
        for port in range(self._base, _MAX_PORT + 1):
            if port in reserved:
                continue
            if _is_port_free(port):
                return port
        raise RuntimeError(
            f"no free loopback port available in [{self._base}, {_MAX_PORT}] "
            f"(excluding {len(reserved)} reserved)"
        )
