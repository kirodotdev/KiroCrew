"""Client-sent relay identity headers never reach a handler or an app backend.

A relay identity header (``x-kiro-relay-user`` and every other name under
:data:`RELAY_HEADER_PREFIX`) names the person a relay forwarded a request for. This
module holds the one definition of that prefix; the relay-path header rule in
:mod:`kiro_crew.dashboard.kiro_relay` reads it too. The only legitimate writer is
the Crew's own relay client, which hands each relayed request to the gateway's
loopback listener stamped with a per-process handover credential; the relay-path
rule verifies that stamp and is the only layer that honors one. Both middleware
chains (the dashboard's and the headless API server's) run
:func:`relay_header_strip_middleware` just inside that rule, ahead of every barrier
and auth layer. Every listener serves one of those two apps through its runner: the
main TCP and Unix listeners, the second loopback family, the tailnet direct listener, and the loopback port that
``tailscale serve`` and the tunnel forward to. The app-backend proxy drops the same
names with :func:`is_relay_header`.

A verified handover the relay-path rule serves carries no relay header, so
:func:`strip_client_relay_headers` returns it unchanged and cannot remove the
identity the rule set; a clone here would reset ``request.remote`` to the socket's
peer. Every other request reaches this layer as it arrived, so this is the one place
a client-sent relay header is dropped.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable

from aiohttp import web
from multidict import CIMultiDict

logger = logging.getLogger(__name__)

#: Lower-case prefix of every relay identity header. A header whose lower-cased
#: name starts with it is the relay's to write and nobody else's.
RELAY_HEADER_PREFIX = "x-kiro-relay-"

_Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def is_relay_header(name: str) -> bool:
    """Whether *name* is a relay identity header, in any letter case."""

    return name.lower().startswith(RELAY_HEADER_PREFIX)


def without_relay_headers(headers: Iterable[tuple[str, str]]) -> CIMultiDict[str]:
    """A copy of *headers* with every relay identity header removed, order kept."""

    return CIMultiDict((name, value) for name, value in headers if not is_relay_header(name))


def strip_client_relay_headers(request: web.Request) -> web.Request:
    """*request*, or a clone of it with every client-sent relay header dropped.

    aiohttp request headers are immutable, so a request that carries one is cloned
    with the filtered set; one that carries none is returned unchanged, which keeps
    the common path free of a copy. Raises :class:`web.HTTPBadRequest` when the
    clone cannot re-encode a header value, so a request that carried a relay header
    is refused rather than served with it.
    """

    if not any(is_relay_header(name) for name in request.headers):
        return request
    try:
        return request.clone(headers=without_relay_headers(request.headers.items()))
    except UnicodeEncodeError:
        raise web.HTTPBadRequest(text="invalid request header") from None


@web.middleware  # type: ignore[misc]
async def relay_header_strip_middleware(
    request: web.Request,
    handler: _Handler,
) -> web.StreamResponse:
    """Hand the rest of the chain a request with no client-sent relay header."""

    stripped = strip_client_relay_headers(request)
    if stripped is not request:
        # The values claim an identity nobody verified, so none of them is logged.
        logger.warning(
            "dropped client-sent relay header(s): peer=%s method=%s path=%r",
            request.remote,
            request.method,
            request.rel_url.path,
        )
    return await handler(stripped)
