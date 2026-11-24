"""The fence round the panel browser: it talks to this gateway and to nothing else.

A panel tab is signed in as the dashboard's owner and shows the dashboard's own
pages. A page of the dashboard can hold a picture, a frame or a link that a Bot
wrote, so without a fence the gateway's host would ask for any address a Bot
names: one on its own loopback, one on the private network, one outside. The
agent's own ``browser`` tool refuses such addresses; this browser must too.

The fence is set for the whole browser, on its command line, so it holds for
every context, every tab, every frame, every worker and every window a page
opens, and for an address handed to the browser from outside as well:

* **A proxy that cannot be reached.** Every request goes to a proxy whose name
  ends in ``.invalid``, except a request to the gateway's own host and port,
  which goes direct. The implicit rule that sends loopback addresses direct is
  taken away (``<-loopback>``), so another port of the loopback is fenced like
  any other address. Sockets (``ws:``, ``wss:``) follow the same setting.
* **A resolver that finds nothing.** No host resolves but the gateway's own
  (the rule covers an address written as numbers too), so the proxy's own name
  is never looked up: a fenced request fails inside the browser and no packet
  leaves it, not even a name lookup.
* **No datagrams past the proxy.** WebRTC may send none.

A request to the gateway's own origin costs nothing more than before: it is
sent direct, with no pause and no round trip through the hub. ``about:``,
``blob:`` and ``data:`` addresses are the page's own and touch no network.

:func:`launch_flags` builds the command line and :func:`passes` is the same rule
as a function, for the document requests the hub pauses (``panel_hub._paused``).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from . import panel_page

#: The proxy no request reaches. ``.invalid`` never resolves (RFC 2606), and the
#: resolver rule below keeps the browser from even asking.
FENCE_PROXY = "http://panel-fence.invalid:9"
#: Addresses of a page's own making: they name no host.
OWN_SCHEMES = frozenset({"about", "blob", "data"})


class FenceError(ValueError):
    """The gateway's origin is not one the fence can be built for."""


def launch_flags(origin: str) -> list[str]:
    """The command-line flags that fence a browser to *origin* (``http://host:port``)."""
    parts = urlsplit(origin)
    try:
        port = parts.port
    except ValueError as exc:
        raise FenceError("the origin has no port") from exc
    host = parts.hostname
    if parts.scheme not in ("http", "https") or not host or port is None:
        raise FenceError("the origin is not http://host:port")
    if panel_page.origin_of(origin) != origin:
        raise FenceError("the origin carries more than a host and a port")
    # An IPv6 host keeps its brackets in a bypass rule.
    rule = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    return [
        f"--proxy-server={FENCE_PROXY}",
        f"--proxy-bypass-list=<-loopback>;{rule}",
        f"--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE {host}",
        "--webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--disable-quic",
    ]


def passes(url: Any, origin: str) -> bool:
    """Whether a request to *url* stays inside the fence of *origin*."""
    if not isinstance(url, str) or not url:
        return False
    try:
        scheme = urlsplit(url).scheme.lower()
    except ValueError:
        return False
    if scheme in OWN_SCHEMES:
        return True
    return panel_page.origin_of(url) == origin


__all__ = ["FENCE_PROXY", "FenceError", "OWN_SCHEMES", "launch_flags", "passes"]
