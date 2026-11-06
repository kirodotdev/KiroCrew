"""The relay-path header rule, and the seam the Crew's relay client hands requests through.

The rule: Crew trusts ``x-kiro-relay-user`` only on a request its own relay client
handed over, never on a local, tailnet, tunnel or any other request; on those, the
client-sent strip just inside this layer drops every ``x-kiro-relay-*`` header.

How a request is "handed over". The relay client runs in this process. It turns each
request the relay sends down the host connection into an HTTP request to this
gateway's own loopback listener, and builds that request's headers with
:meth:`RelayIngress.stamp_headers`. The stamp drops every relay header the person's
client sent, then adds ``x-kiro-relay-user`` from the relay's own message and the
per-process handover credential. The credential is random, lives only in this
process's memory, is never written to disk, the environment or a log, and is rotated
whenever the host connection is unbound, so no other process on the machine can mark
a request as the relay's.

:func:`relay_ingress_middleware` runs right after route latency in both gateway
chains, outer to every other layer that reads a header. It does exactly one of three
things:

* no handover credential, or the relay is off: hand the request on unchanged, so the
  client-sent strip just inside drops every relay header it carries and it is served
  as the ordinary local, tailnet or tunnel request it is;
* a handover that does not verify, arrives on any transport but loopback TCP, names
  no valid person, or names someone this Crew does not honor: refuse it, typed;
* a verified handover: apply the remote limits, then serve a copy of the request
  with every relay header removed, ``request.remote`` set to
  :data:`~.caller.RELAY_REMOTE`, and the caller published under
  :data:`~.caller.RELAY_CALLER_KEY` for token auth.

The client-sent strip (:mod:`kiro_crew.dashboard.relay_headers`) sits just inside
this layer, reads the same prefix, ``relay_headers.RELAY_HEADER_PREFIX``, and is the
one place a client-sent relay header is dropped. A verified handover this layer
serves carries no relay header, so the strip passes it through as the same object
and cannot clone away the identity set here (a clone would reset ``request.remote``
to the socket's peer).

Off by default. With ``tunnel.relay_enabled`` false no :class:`RelayIngress` accepts a
stamp, so the only behaviour left is the strip.
"""

from __future__ import annotations

import hmac
import logging
import secrets
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from aiohttp import web
from multidict import CIMultiDict

from kiro_crew.dashboard.kiro_relay.caller import (
    RELAY_CALLER_KEY,
    RELAY_HANDOVER_HEADER,
    RELAY_REMOTE,
    RELAY_USER_HEADER,
    HostBinding,
    is_kiro_user_id,
    published_relay_caller,
)
from kiro_crew.dashboard.kiro_relay.limits import RemoteLimit, remote_limit_for
from kiro_crew.dashboard.origin import request_is_unix_socket
from kiro_crew.dashboard.relay_headers import without_relay_headers
from kiro_crew.dashboard.urls import is_loopback

logger = logging.getLogger(__name__)

#: aiohttp app key holding the app's :class:`RelayIngress`. Absent on the headless
#: API server, which serves no relay, so the rule hands every request on there.
RELAY_INGRESS_APP_KEY = "kiro_relay_ingress"

_Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

#: Refusals of the handover itself, beside the remote limits in :mod:`.limits`.
HANDOVER_INVALID = RemoteLimit(
    "relay_handover_invalid", "This request didn't come from the Crew's relay client."
)
HANDOVER_TRANSPORT = RemoteLimit(
    "relay_handover_transport", "The relay client hands requests over loopback TCP only."
)
NOT_CONNECTED = RemoteLimit("relay_not_connected", "The Crew isn't connected to the relay.")
USER_INVALID = RemoteLimit("relay_user_invalid", "The relay didn't name one valid Kiro user.")
NOT_OWNER = RemoteLimit("relay_not_owner", "Only this Crew's owner can reach it through the relay.")
#: A request carrying a relay header whose headers cannot be rebuilt without it.
HEADER_INVALID = RemoteLimit("relay_header_invalid", "A request header couldn't be read.")


class RelayIngressError(RuntimeError):
    """The relay client asked for something the ingress cannot do in its state."""


class RelayIngress:
    """The relay client's door into this gateway: one per dashboard app.

    The relay client calls :meth:`bind_host` once its host connection is up, with
    what the lease told it (the Crew's kind and, for a personal Crew, its owner's
    Kiro user ID), :meth:`stamp_headers` for every request it hands over, and
    :meth:`unbind_host` when the connection ends. Every method runs on the event
    loop; the binding is an immutable value swapped in one assignment.
    """

    def __init__(self, *, enabled: bool) -> None:
        self._enabled = bool(enabled)
        self._handover = secrets.token_urlsafe(32) if self._enabled else ""
        self._binding: HostBinding | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def binding(self) -> HostBinding | None:
        return self._binding

    def bind_host(self, binding: HostBinding) -> None:
        """Start honoring stamps for the host connection *binding* describes."""
        if not self._enabled:
            raise RelayIngressError("the relay is off")
        if not isinstance(binding, HostBinding):
            raise TypeError("binding must be a HostBinding")
        self._binding = binding

    def unbind_host(self) -> None:
        """Stop honoring stamps, and retire the handover credential they carried."""
        self._binding = None
        if self._enabled:
            self._handover = secrets.token_urlsafe(32)

    def stamp_headers(
        self, client_headers: Iterable[tuple[str, str]], *, relay_user: str
    ) -> CIMultiDict[str]:
        """The headers for one handed-over request.

        *client_headers* are the person's request headers as the relay forwarded
        them; *relay_user* is the verified ID from the relay's own message. Any
        relay header among *client_headers* is dropped first, so the stamp always
        overwrites a client value, never sits beside it.
        """
        if not self._enabled:
            raise RelayIngressError("the relay is off")
        if self._binding is None:
            raise RelayIngressError("no host connection is bound")
        if not is_kiro_user_id(relay_user):
            raise ValueError("relay_user is not a Kiro user ID")
        stamped = without_relay_headers(client_headers)
        stamped[RELAY_USER_HEADER] = relay_user
        stamped[RELAY_HANDOVER_HEADER] = self._handover
        return stamped

    def handed_over(self, presented: str) -> bool:
        """Whether *presented* is the live handover credential."""
        if not self._enabled or not self._handover or not isinstance(presented, str):
            return False
        # Bytes, so attacker-chosen non-ASCII header text gets a verdict, not a TypeError.
        return hmac.compare_digest(
            presented.encode("utf-8", "surrogatepass"),
            self._handover.encode("utf-8", "surrogatepass"),
        )


def _clone(request: web.Request, **changes: Any) -> web.Request | None:
    """*request* with every relay header dropped, plus *changes*.

    ``None`` when the clone cannot re-encode a header value: the caller refuses
    such a request rather than serving it with its relay header.
    """
    try:
        return request.clone(headers=without_relay_headers(request.headers.items()), **changes)
    except UnicodeEncodeError:
        return None


def _audit_refusal(
    request: web.Request,
    caller_label: str,
    limit: RemoteLimit,
    *,
    operation: str = "relay_ingress",
) -> None:
    """Record one refusal of a relayed request in the SEL, best-effort."""
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller=caller_label or (request.remote or ""),
            forwarded=True,
            operation=operation,
            outcome="denied",
            source="kiro_relay",
            resources=request.path,
            error=limit.code,
        )
    except Exception:
        logger.debug("relay ingress refusal audit failed", exc_info=True)


def _refuse(
    request: web.Request, caller_label: str, limit: RemoteLimit, *, status: int = 403
) -> web.Response:
    _audit_refusal(request, caller_label, limit)
    return web.json_response({"error": limit.message, "code": limit.code}, status=status)


def refuse_relayed(request: web.Request, limit: RemoteLimit, *, operation: str) -> web.Response:
    """A handler's typed 403 for an admitted relayed request, with the ingress's SEL row.

    For a remote limit a handler applies itself, where the path alone cannot decide
    it (the request body names the resource), so the refusal reads the same in the
    security event log as one the ingress gave.
    """
    caller = published_relay_caller(request)
    _audit_refusal(request, caller.audit_label if caller else "", limit, operation=operation)
    return web.json_response({"error": limit.message, "code": limit.code}, status=403)


async def _serve_handover(
    request: web.Request,
    handler: _Handler,
    ingress: RelayIngress,
    strict_internal_paths: frozenset[str],
) -> web.StreamResponse:
    """Verify a request presenting the handover credential, then serve or refuse it."""
    presented = request.headers.getall(RELAY_HANDOVER_HEADER, [])
    if len(presented) != 1 or not ingress.handed_over(presented[0]):
        return _refuse(request, "", HANDOVER_INVALID)
    # The relay client dials the loopback TCP listener. A credential arriving any
    # other way (the Unix socket, which several gates trust as local; a widened
    # bind) did not come from it.
    if request_is_unix_socket(request) or not is_loopback(request.remote or ""):
        return _refuse(request, "", HANDOVER_TRANSPORT)
    binding = ingress.binding
    if binding is None:
        return _refuse(request, "", NOT_CONNECTED)
    users = request.headers.getall(RELAY_USER_HEADER, [])
    if len(users) != 1 or not is_kiro_user_id(users[0]):
        return _refuse(request, "", USER_INVALID)
    caller = binding.caller_for(users[0])
    if caller is None:
        return _refuse(request, f"relay:{users[0]}", NOT_OWNER)
    limit = remote_limit_for(
        request.path, request.headers, caller, strict_internal_paths=strict_internal_paths
    )
    if limit is not None:
        return _refuse(request, caller.audit_label, limit)
    relayed = _clone(request, remote=RELAY_REMOTE)
    if relayed is None:
        return _refuse(request, caller.audit_label, HEADER_INVALID, status=400)
    relayed[RELAY_CALLER_KEY] = caller
    return await handler(relayed)


def relay_ingress_middleware(*, strict_internal_paths: frozenset[str]) -> Any:
    """The relay-path header rule as a middleware, outermost in both gateway chains.

    *strict_internal_paths* is the strict internal set the chain's token auth is
    built with, so the local-only limit refuses exactly the paths token auth keeps
    to loopback plus the internal secret.
    """

    @web.middleware  # type: ignore[misc]
    async def _relay_ingress(request: web.Request, handler: _Handler) -> web.StreamResponse:
        """Apply the rule before any other layer reads a header."""
        ingress = request.app.get(RELAY_INGRESS_APP_KEY)
        if (
            isinstance(ingress, RelayIngress)
            and ingress.enabled
            and RELAY_HANDOVER_HEADER in request.headers
        ):
            return await _serve_handover(request, handler, ingress, strict_internal_paths)
        # Not the relay client's request: the client-sent strip just inside this
        # layer drops whatever relay header it carries.
        return await handler(request)

    _relay_ingress._is_relay_ingress = True  # type: ignore[attr-defined]
    return _relay_ingress
