"""Remote Crew pane ports: the other ports an app web origin is served on.

A Remote Crew pane loads this dashboard at ``http://<loopback name>:P``, where P is
the port of the hub's SSH forward to this machine. P is usually not one of this
gateway's dashboard ports (the hub allocates it), so without this record a request
for ``<app>.apps.localhost:P`` is refused, and the app page framed in the pane shows
"misdirected request".

The pane proves it is one with its signed dashboard token: the hub minted it with
the ``embed_parent_port`` claim, the port of the hub page that frames the pane.
When such a token authenticates a direct local request on a port P, the token
layer records ``P -> {parent port: session expiry}`` here
(:func:`note_pane_request`). The app web origin router then serves P while a
record for it is unexpired, and lets exactly P's own loopback origins and each
recorded parent's loopback origins frame the answer. P can also equal one of this
gateway's own dashboard ports, since the hub picks it on its own machine; the
router then adds the recorded parents to that port's usual ancestors.

The record decides which origins exist and which ancestors may frame them. It
admits no new peer: a request on P still has to arrive straight from this machine,
which needs SSH to it or code on it, and such a peer already reaches the backend's
own loopback port.

* **Bound to the claim.** A token minted with an ``embed_forward_port`` claim
  records only the port it names, so a pane token presented on another port
  records nothing. A token with no such claim (one minted by an older hub) records
  the port in ``Host``.
* **Union of parents.** Two hubs can reach this gateway on the same P with
  different parent ports. Each parent is a loopback origin, which each browser
  resolves to its own machine, so admitting both breaks neither hub's pane.
* **Bounded and in memory.** At most :data:`MAX_PANE_PORTS` ports and
  :data:`MAX_PARENTS_PER_PORT` parents each, the least recently recorded dropped
  first, so a token holder cannot grow it without limit. Nothing is written to
  disk; after a restart the pane's next authenticated request records it again.
* **Never on the Session Manager listener.** Those requests are judged by its gate
  and never reach the token flow that records.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from typing import Any

from aiohttp import web

from kiro_crew.sel import sel, sel_is_warm

logger = logging.getLogger("kiro_crew.apps.pane_ports")

#: The app key the gateway's record lives under; one record per dashboard app.
PANE_PORTS_APP_KEY = "app_web_origin_pane_ports"

#: Most pane ports recorded at once.
MAX_PANE_PORTS = 16

#: Most parent ports recorded for one pane port.
MAX_PARENTS_PER_PORT = 4


def _port(raw: object) -> int | None:
    """A signed port claim as an ``int``: a decimal string in 1-65535, else None."""
    if not isinstance(raw, str) or not raw.isascii() or not raw.isdigit():
        return None
    port = int(raw)
    return port if 1 <= port <= 65535 else None


class PanePorts:
    """Pane port -> {parent port: expiry}, bounded, least recently recorded first out."""

    def __init__(self) -> None:
        self._ports: OrderedDict[int, OrderedDict[int, float]] = OrderedDict()

    def record(self, port: int, parent: int, expiry: float, now: float) -> bool:
        """Record *parent* for *port* until *expiry*; True when that changed the record.

        A pairing keeps the latest expiry any session gave it: two sessions can
        share one forward and parent port, and an older one's request must not cut
        short the grant a newer one still holds. A repeat, or an expiry no later
        than the recorded one, changes nothing and answers False, so a caller
        writes one audit row per new pairing or extended expiry, not one per
        request.
        """
        if expiry <= now:
            return False
        parents = self._ports.get(port)
        if parents is None:
            parents = self._ports[port] = OrderedDict()
        self._ports.move_to_end(port)
        held = parents.get(parent)
        changed = held is None or expiry > held
        parents[parent] = expiry if held is None else max(held, expiry)
        parents.move_to_end(parent)
        while len(parents) > MAX_PARENTS_PER_PORT:
            parents.popitem(last=False)
        while len(self._ports) > MAX_PANE_PORTS:
            self._ports.popitem(last=False)
        return changed

    def parents(self, port: int, now: float) -> list[int]:
        """The unexpired parent ports recorded for *port*, sorted; expired ones are dropped."""
        parents = self._ports.get(port)
        if parents is None:
            return []
        for parent in [p for p, exp in parents.items() if exp <= now]:
            del parents[parent]
        if not parents:
            del self._ports[port]
            return []
        return sorted(parents)


def pane_ports(app: web.Application) -> PanePorts:
    """The gateway's pane port record, created on first use."""
    record = app.get(PANE_PORTS_APP_KEY)
    if not isinstance(record, PanePorts):
        record = PanePorts()
        app[PANE_PORTS_APP_KEY] = record
    return record


def _host_port(request: web.Request) -> int | None:
    """The port in ``Host`` when its name is one of the dashboard's loopback names.

    A missing port is the HTTP default, ``80``, as ``app_web_origin.parse_app_host``
    reads it
    (a browser leaves the default port out of ``Host``), so a forward bound on 80
    records the same port the router later looks up.
    """
    from kiro_crew.apps.app_web_origin import _DASHBOARD_LOOPBACK_HOSTS
    from kiro_crew.dashboard.origin import _canonical_host, _split_authority

    parts = _split_authority(request.headers.get("Host", ""))
    if parts is None or _canonical_host(parts[0]) not in _DASHBOARD_LOOPBACK_HOSTS:
        return None
    return _port(parts[1][1:] or "80")


def _claims(token: str) -> dict[str, Any]:
    """The payload of a token the token layer already verified, or ``{}``."""
    from kiro_crew.dashboard.token_auth_tokens import _b64url_decode

    try:
        data = json.loads(_b64url_decode(token.split(".", 1)[0]))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


async def note_pane_request(request: web.Request, token: str) -> None:
    """Record the pane port *request* arrived on, when *token* says it is a pane's.

    *token* is the credential the token layer authenticated this request with, so
    its signature is already verified. Records nothing unless the token carries
    ``embed_parent_port`` (and, when present, an ``embed_forward_port`` equal to
    the port), the request came straight from this machine, and ``Host`` is a
    dashboard loopback name. The port may also be one of this gateway's own
    dashboard ports: the hub allocates its forward port on its own machine, so it
    can equal one, and the app answer there then needs the recorded parents too.
    Never raises: a failure here must not fail the request.
    """
    try:
        from kiro_crew.dashboard.origin import is_direct_local_request, is_proxied_request

        # The claim first: it is the cheap test, and this runs on every
        # authenticated request.
        claims = _claims(token)
        parent = _port(claims.get("embed_parent_port"))
        if parent is None:
            return
        port = _host_port(request)
        if port is None:
            return
        if is_proxied_request(request) or not is_direct_local_request(request):
            return
        forward = claims.get("embed_forward_port")
        if forward is not None and _port(forward) != port:
            return
        try:
            expiry = float(claims.get("session_exp", 0.0))
        except (TypeError, ValueError):
            return
        if not pane_ports(request.app).record(port, parent, expiry, time.time()):
            return
        await _audit_record(port, parent, bound=forward is not None)
    except Exception:
        logger.warning("Failed to record a Remote Crew pane port", exc_info=True)


async def _audit_record(port: int, parent: int, *, bound: bool) -> None:
    """One SEL row per new pane port pairing or extended expiry, best-effort."""
    fields: dict[str, Any] = {
        "caller": "app_web_origin",
        "operation": "record pane port",
        "outcome": "granted",
        "source": "app_web_origin",
        "resources": (
            f"pane port {port} for parent port {parent} " f"({'claimed' if bound else 'from Host'})"
        ),
    }

    def _write() -> None:
        sel().log_api_access(**fields)

    try:
        if sel_is_warm():
            _write()
        else:
            await asyncio.to_thread(_write)
    except Exception:
        logger.warning("Failed to log a pane port record to SEL", exc_info=True)


__all__ = [
    "MAX_PANE_PORTS",
    "MAX_PARENTS_PER_PORT",
    "PANE_PORTS_APP_KEY",
    "PanePorts",
    "note_pane_request",
    "pane_ports",
]
