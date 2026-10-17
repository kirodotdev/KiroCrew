#!/usr/bin/env python3
"""Wire-level fake for messaging-channel vendor APIs.

Channel tests that fake the *client* (``FakeClient`` standing in for
``WeixinClient`` / ``WeComClient`` / ...) never exercise the client's own
payload construction, header/signature building, and protocol-error parsing --
exactly the layer where the iLink QR bug lives (``qrcode_img_content`` is a
scannable URL, not image bytes; a client-level fake stays green while production
is broken).

This module fakes ONE level down: the ``aiohttp`` session. Everything above the
socket runs for real -- client, transport, dispatcher, the shared
``messaging.dispatch`` pipeline, and the renderer -- so a test can assert on the
*actual bytes the channel would put on the wire* without any credential,
network, or vendor account.

Seam
----
Every channel client stores its session as a lazily-created attribute
(``self._session: Any = None``, built in ``connect()``). A test assigns a
:class:`FakeWireSession` onto that attribute and the client never creates a real
one::

    client = WeixinClient(token="t", base_url="https://example.invalid")
    wire = FakeWireSession()
    wire.route("POST", "sendmsg", {"errcode": 0})
    client._session = wire            # no monkeypatching of aiohttp internals

    await client.send_message("u1", "hi")
    sent = wire.requests[0]
    assert sent.json_body["base_info"]["app_id"]   # real payload, real headers

What this does and does NOT prove
---------------------------------
It proves our whole stack agrees with a PINNED response shape: if a fixture
says a field is a URL string, every layer above is verified against that.
It does NOT prove the vendor agrees -- only a live probe can establish the
shape in the first place. The intended workflow is therefore: probe once
against the real API (authorized), encode the observed shape here as a
fixture, and let the fixture guard every layer forever after. When a vendor
changes, one fixture changes and the whole stack is re-verified.

It also proves the stack agrees with ``aiohttp`` on how a response is READ,
because the reading is done by ``aiohttp`` itself -- see
:class:`_FakeResponseCM`. It does NOT prove agreement on how a request is
ENCODED: ``aiohttp`` builds a body inside ``ClientRequest``, which lives below
the ``client._session`` seam this module replaces, so
:class:`RecordedRequest` holds the value the client passed rather than the
bytes a real request would carry.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Tuple,
    Union,
)

from aiohttp import ClientResponse, RequestInfo, hdrs
from aiohttp.helpers import HeadersMixin
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

__all__ = [
    "RecordedRequest",
    "WireResponse",
    "FakeWireSession",
    "FakeWireWebSocket",
    "UnroutedRequestError",
]


def _reason_for(status: int) -> str:
    """The reason phrase a real server would put on the status line.

    ``raise_for_status`` asserts ``reason is not None``, so a fixture that
    only sets a status still produces the error production would raise.
    """
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return ""


_QUEUED = object()
"""Sentinel marking a route whose responses live in ``_queues``."""


def _is_sequence_target(target: Any) -> bool:
    """True for a scripted SEQUENCE of responses.

    ``RouteTarget`` declares ``Iterable``, so a tuple or any other non-mapping
    sequence must be treated as a script -- handling only ``list`` would have
    silently used a tuple as a response BODY.

    The mapping exclusion tests ``Mapping``, not ``dict``: a ``MappingProxyType``
    or any custom mapping is Iterable over its KEYS, so a dict-only check would
    turn a perfectly ordinary response body into a script of its key strings.
    """
    if isinstance(target, (WireResponse, Mapping, str, bytes)) or callable(target):
        return False
    return isinstance(target, Iterable)


class UnroutedRequestError(AssertionError):
    """Raised when the code under test calls an endpoint with no fixture.

    Deliberately fail-closed: a silent default would let a channel start
    calling a new vendor endpoint with nobody noticing the shape was never
    pinned.
    """


@dataclass
class RecordedRequest:
    """One outbound call, captured for assertions."""

    method: str
    url: str
    headers: Dict[str, str] = field(default_factory=dict)
    body: Optional[Union[str, bytes, Dict[str, Any]]] = None
    """str/bytes for a JSON or raw body; dict when the caller used aiohttp's
    form-encoding (``data={...}``) -- inspect via ``.form``."""
    params: Optional[Dict[str, Any]] = None

    @property
    def json_body(self) -> Any:
        """Decode the request body as JSON (the common vendor case)."""
        if self.body is None:
            raise AssertionError(f"{self.method} {self.url} carried no body")
        if isinstance(self.body, dict):
            raise AssertionError(
                f"{self.method} {self.url} was form-encoded, not JSON -- use .form"
            )
        raw = self.body.decode("utf-8") if isinstance(self.body, bytes) else self.body
        return json.loads(raw)

    @property
    def form(self) -> Dict[str, Any]:
        """Form-encoded body (aiohttp accepts a dict for ``data=``).

        The Teams app-credential exchange posts this way, so a wire test must be
        able to inspect it without the fake having silently JSON-encoded it.
        """
        if not isinstance(self.body, dict):
            raise AssertionError(f"{self.method} {self.url} was not form-encoded")
        return self.body

    @property
    def path(self) -> str:
        """URL without scheme/host -- what routes are matched on."""
        no_scheme = self.url.split("://", 1)[-1]
        slash = no_scheme.find("/")
        return no_scheme[slash:] if slash >= 0 else "/"


def _json_fallback(value: Any) -> Any:
    """Encode a non-``dict`` ``Mapping`` body that ``json`` refuses on its own.

    A route target that is a ``Mapping`` but NOT a ``dict`` -- the obvious case
    being ``MappingProxyType``, which channel code hands out to keep a payload
    read-only -- is deliberately treated as a response BODY rather than a script
    of its keys. ``json.dumps`` does not accept one: ``mappingproxy`` is not a
    ``dict`` subclass, so encoding raised ``TypeError`` at read time and turned a
    supported body into a crash inside the client under test.

    Hooked in as ``default=`` rather than converting the body once at the top
    level, because ``json`` calls this for EVERY value it cannot encode. A
    mapping nested inside a plain dict is therefore covered at any depth, which a
    single ``dict(self.body)`` at the surface would silently miss.
    """
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(
        f"{type(value).__name__} is not JSON-serializable; pass a str, bytes, "
        f"or a JSON-compatible object as the response body"
    )


@dataclass
class WireResponse:
    """A canned vendor response.

    ``body`` may be a str (returned verbatim) or any JSON-serializable object
    (encoded). ``content_type`` matters more than it looks: the iLink QR bug
    was a ``text/html`` response where the code assumed image bytes. It is
    enforced the way aiohttp enforces it, so a body read with ``.json()``
    under a non-JSON ``content_type`` raises ``ContentTypeError`` here too.
    """

    body: Any = field(default_factory=dict)
    status: int = 200
    content_type: str = "application/json"
    headers: Dict[str, str] = field(default_factory=dict)
    """Extra response headers. Matched case-insensitively, as a real server's
    are: a ``content-type`` here overrides the ``content_type`` field above."""

    def text(self) -> str:
        if isinstance(self.body, str):
            return self.body
        if isinstance(self.body, bytes):
            return self.body.decode("utf-8")
        return json.dumps(self.body, default=_json_fallback)

    def raw(self) -> bytes:
        if isinstance(self.body, bytes):
            return self.body
        return self.text().encode("utf-8")


class _FakeResponseCM(HeadersMixin):
    """Async context manager standing in for ``aiohttp``'s response object.

    The read side is deliberately NOT modelled here. ``json``, ``text``,
    ``get_encoding``, ``raise_for_status``, ``ok`` and the ``content_type`` /
    ``charset`` properties are ``aiohttp``'s own implementations bound onto
    this object, so a canned response answers whatever the real library would
    answer. A wire test therefore cannot pass because this file's idea of
    ``aiohttp`` is wrong:

    * ``.json()`` on a ``text/html`` body raises ``ContentTypeError``, which is
      the shape of the iLink QR bug (a ``text/html`` reply read as image
      bytes) that the harness exists to catch;
    * ``content_type=None`` bypasses that check, as it does in production;
    * an empty body reads as ``None`` rather than raising a decode error;
    * a ``+json`` suffix type is accepted and ``application/json;
      charset=...`` has its parameters stripped, per aiohttp's own matcher;
    * ``ok`` is ``status < 400``, which is aiohttp's rule -- an informational
      1xx status counts as ok, where an intuitive ``200 <= status < 400``
      would not.

    Only the attributes those methods read are supplied: a bytes ``_body``,
    ``_headers``, ``status``, ``reason``, ``request_info``, ``history`` and a
    no-op ``release``. That surface is narrow and stable, unlike
    ``ClientResponse.__init__`` -- constructing a real response is what ties a
    fake to aiohttp's private constructor and breaks it on a minor upgrade.
    """

    json = ClientResponse.json
    text = ClientResponse.text
    get_encoding = ClientResponse.get_encoding
    raise_for_status = ClientResponse.raise_for_status
    ok = ClientResponse.ok

    history: Tuple[Any, ...] = ()
    """aiohttp's redirect chain. A canned response was never redirected."""

    _resolve_charset = staticmethod(lambda *_: "utf-8")
    """aiohttp's own class default, used by ``get_encoding`` when the body is
    neither JSON nor charset-tagged. A real ``ClientSession`` installs its
    ``fallback_charset_resolver`` here; nothing in this harness overrides it."""

    def __init__(self, resp: WireResponse, rec: Optional[RecordedRequest] = None) -> None:
        self._resp = resp
        self.status = resp.status
        self.reason = _reason_for(resp.status)
        self._body = resp.raw()
        # A real response's headers are case-insensitive, so ``content-type``
        # and ``Content-Type`` are one field. A plain dict makes them two, and
        # ``HeadersMixin`` asks for aiohttp's own spelling: a fixture using the
        # lowercase form would be looked past and the default below answered
        # instead, letting ``.json()`` accept a body production refuses. This
        # is the mapping type aiohttp itself carries, for that reason.
        headers: CIMultiDict[str] = CIMultiDict()
        if resp.content_type is not None:
            headers[hdrs.CONTENT_TYPE] = resp.content_type
        # ``update`` replaces case-insensitively, so a fixture header wins over
        # the ``content_type`` field however either one is spelled.
        headers.update(resp.headers)
        # HeadersMixin reads ``_headers``; callers read ``headers``. One mapping
        # serves both so a fixture header cannot be visible through only one.
        self._headers = headers
        self.headers = headers
        self.request_info = RequestInfo(
            URL(rec.url if rec is not None else "https://fake.invalid/"),
            rec.method if rec is not None else "GET",
            CIMultiDictProxy(headers),
        )
        self._in_context = False

    def release(self) -> Any:
        """No connection to release; present because ``raise_for_status`` calls it."""
        return None

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self) -> "_FakeResponseCM":
        self._in_context = True
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        self._in_context = False


# A route resolves to a single response, an iterable of responses (consumed in
# order -- for poll loops that must see a sequence), or a callable taking the
# RecordedRequest and returning one.
RouteTarget = Union[
    WireResponse,
    Dict[str, Any],
    Iterable[Union[WireResponse, Dict[str, Any]]],
    Callable[[RecordedRequest], Union[WireResponse, Dict[str, Any]]],
]


class FakeWireSession:
    """Stand-in for ``aiohttp.ClientSession`` at the channel wire boundary.

    Routes are matched by ``(method, substring-of-path)``; the longest matching
    substring wins so a specific endpoint can override a general one. Every
    call is appended to :attr:`requests` in order.
    """

    def __init__(self, *, strict: bool = True) -> None:
        self.requests: List[RecordedRequest] = []
        self.closed = False
        self._routes: Dict[Tuple[str, str], Any] = {}
        self._queues: Dict[Tuple[str, str], List[Any]] = {}
        self._ws: Optional["FakeWireWebSocket"] = None
        self._strict = strict

    # -- fixture wiring --------------------------------------------------------
    def route(self, method: str, path_contains: str, target: RouteTarget) -> "FakeWireSession":
        """Register a canned response. Chainable.

        A **sequence** target (list, tuple, any non-mapping iterable) is a
        SCRIPT: successive calls consume it in order, and a call past the end
        raises :class:`UnroutedRequestError`. That is deliberate -- silently
        repeating the last response would hide a client that polled more times
        than the test scripted, which is the same class of bug the fail-closed
        routing exists to catch. Script a steady state explicitly by registering
        a single response instead of a sequence; a single target answers every
        call.

        The sequence is COPIED into private state, so a script held in a
        module-level constant is never mutated and cannot carry consumed state
        between tests.
        """
        key = (method.upper(), path_contains)
        if _is_sequence_target(target):
            self._queues[key] = list(target)  # type: ignore[arg-type]
            self._routes[key] = _QUEUED
        else:
            self._routes[key] = target
        return self

    def route_ws(self, ws: "FakeWireWebSocket") -> "FakeWireSession":
        self._ws = ws
        return self

    # -- aiohttp surface -------------------------------------------------------
    def request(
        self,
        method: str,
        url: str,
        *,
        data: Any = None,
        json: Any = None,  # noqa: A002 - mirrors aiohttp's kwarg name
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        **_kw: Any,
    ) -> _FakeResponseCM:
        """Generic verb dispatch -- aiohttp's ``session.request(METHOD, ...)``.

        Webex's ``_api`` drives every verb through this one call, so a fake
        lacking it would make those clients untestable at the wire layer.
        """
        if data is not None:
            body = data
        elif json is not None:
            body = _dumps(json)
        else:
            body = None
        return self._dispatch(method.upper(), url, headers, body, params)

    def post(
        self,
        url: str,
        *,
        data: Any = None,
        json: Any = None,  # noqa: A002 - mirrors aiohttp's kwarg name
        headers: Optional[Dict[str, str]] = None,
        **_kw: Any,
    ) -> _FakeResponseCM:
        if data is not None:
            body = data
        elif json is not None:
            body = _dumps(json)
        else:
            body = None
        return self._dispatch("POST", url, headers, body, None)

    def get(
        self,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        **_kw: Any,
    ) -> _FakeResponseCM:
        return self._dispatch("GET", url, headers, None, params)

    def ws_connect(self, url: str, **_kw: Any) -> "FakeWireWebSocket":
        self.requests.append(RecordedRequest(method="WS", url=url))
        if self._ws is None:
            raise UnroutedRequestError(f"ws_connect({url}) with no route_ws() fixture")
        return self._ws

    async def close(self) -> None:
        self.closed = True

    # -- routing ---------------------------------------------------------------
    def _dispatch(
        self,
        method: str,
        url: str,
        headers: Optional[Dict[str, str]],
        body: Any,
        params: Optional[Dict[str, Any]],
    ) -> _FakeResponseCM:
        rec = RecordedRequest(
            method=method, url=url, headers=dict(headers or {}), body=body, params=params
        )
        self.requests.append(rec)
        target = self._match(method, rec.path)
        if target is None:
            if self._strict:
                raise UnroutedRequestError(
                    f"no fixture for {method} {rec.path!r}. Register one with "
                    f"route({method!r}, <path substring>, <response>) -- a channel "
                    f"calling an unpinned endpoint is exactly what this guards."
                )
            return _FakeResponseCM(WireResponse(), rec)
        return _FakeResponseCM(_coerce(target, rec), rec)

    def _match(self, method: str, path: str) -> Any:
        best: Optional[Tuple[int, Tuple[str, str]]] = None
        for key, _target in self._routes.items():
            m, frag = key
            if m != method or frag not in path:
                continue
            if best is None or len(frag) > best[0]:
                best = (len(frag), key)
        if best is None:
            return None
        key = best[1]
        target = self._routes[key]
        if target is _QUEUED:
            queue = self._queues[key]
            if not queue:
                raise UnroutedRequestError(
                    f"scripted responses for {key[0]} {key[1]!r} are exhausted: the "
                    f"client called it more times than the test scripted. Register a "
                    f"single response instead of a sequence for a steady state."
                )
            return queue.pop(0)
        return target


class FakeWireWebSocket:
    """Scripted stand-in for an ``aiohttp`` WebSocket (WS-based channels).

    ``incoming`` frames are yielded to the channel's read loop in order; once
    drained the socket reports closed so the loop exits instead of spinning.
    Outbound frames land in :attr:`sent` for assertions.
    """

    def __init__(self, incoming: Optional[Iterable[Any]] = None) -> None:
        self._incoming: List[Any] = list(incoming or [])
        self.sent: List[Any] = []
        self.closed = False

    async def send_str(self, data: str) -> None:
        self.sent.append(data)

    async def send_json(self, data: Any) -> None:
        self.sent.append(_dumps(data))

    async def close(self) -> None:
        self.closed = True

    async def __aenter__(self) -> "FakeWireWebSocket":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        self.closed = True

    def __aiter__(self) -> "FakeWireWebSocket":
        return self

    async def __anext__(self) -> Any:
        if not self._incoming:
            self.closed = True
            raise StopAsyncIteration
        return self._incoming.pop(0)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _coerce(target: Any, rec: RecordedRequest) -> WireResponse:
    if callable(target) and not isinstance(target, WireResponse):
        target = target(rec)
    if isinstance(target, WireResponse):
        return target
    return WireResponse(body=target)
