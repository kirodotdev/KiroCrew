"""The gateway's own origin for a caller on this host, read from its bind evidence."""

from __future__ import annotations

import os


def loopback_gateway_origin() -> str:
    """``http://<loopback host>:<bound port>``, or ``""`` when there is no such evidence.

    The evidence is what the gateway exported once it held its socket:
    ``KIROCREW_BOUND_PORT``, and ``KIROCREW_BOUND_HOST`` for a bind that loopback
    does not reach at ``127.0.0.1``. Absent means loopback (loopback itself, or a
    wildcard bind loopback reaches), and ``::1`` is the IPv6-loopback marker. Only
    these two shapes give an origin: a caller on this host sends no ``Origin`` header,
    and the gateway's CSRF barrier trusts an Origin-less mutating request only from a
    loopback peer, so an origin on a specific interface would have every mutating call
    refused. IPv6 literals are bracketed per RFC 3986. Never a config value, a default
    or a request-derived value, so a caller is never pointed at a sibling gateway.
    """
    bound_port = os.environ.get("KIROCREW_BOUND_PORT", "").strip()
    bound_host = os.environ.get("KIROCREW_BOUND_HOST", "").strip()
    if not (bound_port.isdigit() and 1 <= int(bound_port) <= 65535):
        return ""
    if bound_host not in ("", "::1"):
        return ""
    host = bound_host or "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{int(bound_port)}"
