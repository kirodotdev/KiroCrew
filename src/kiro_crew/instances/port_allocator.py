"""Local loopback port allocation for SSH ``-L`` tunnels.

Each connected remote instance needs a distinct ``127.0.0.1:<local_port>`` for
its SSH forward. This allocator hands out the first free port at or above a base
(default 7778, see ``kiro_crew.instances.constants``), skipping ports that are
already bound on the loopback interface or reserved by a caller (e.g. ports the
registry has already assigned to other instances).

The "in use" probe binds ``127.0.0.1:<port>`` momentarily; a successful bind
means the port is free. This is advisory — there is an inherent TOCTOU window
between probing and the tunnel actually binding — so callers should treat a
returned port as a best-effort suggestion and handle a late bind failure by
re-allocating.
"""

from __future__ import annotations

import logging
import socket
from collections.abc import Iterable

from kiro_crew.instances.constants import DEFAULT_TUNNEL_BASE_PORT

logger = logging.getLogger(__name__)

# Upper bound for the search; ports above this are ephemeral/unprivileged-noise
# and we should fail loudly rather than wander into them.
_MAX_PORT = 65535


def _is_port_free(port: int, host: str = "127.0.0.1") -> bool:
    """Return True if *port* can be bound on *host* (i.e. it is free).

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

    A bind failure (``OSError``) is interpreted as "in use / unavailable".
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


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
