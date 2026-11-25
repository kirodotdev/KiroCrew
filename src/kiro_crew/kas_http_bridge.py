"""A stdio MCP server that relays to a remote one, adding headers kept local.

On KAS a remote server's ``headers`` never cross the wire
(:func:`kiro_crew.acp.kas_agents._withhold_credential_fields`). Without them a
server that authenticates with a static header (an ``Authorization: Bearer``
token, an API key header) answers the engine 401, the engine starts an OAuth
sign-in the server does not need, and the server is missing from the session. The
launcher (:mod:`kiro_crew.kas_env_shim`) runs this bridge in its place: the engine
talks newline-delimited JSON-RPC to it over stdio, and each message is POSTed to
the recorded url with the recorded headers, following the streamable-HTTP
transport (``Mcp-Session-Id``, ``MCP-Protocol-Version``, a JSON or an event-stream
answer).

A server that refuses ``initialize`` with a 4xx other than 401 or 403 is spoken to
over the older two-endpoint ``sse`` transport instead, the fallback KAS itself
makes: a GET to the url opens an event stream whose ``endpoint`` event names where
each message is POSTed, and the answers arrive on that stream as ``message``
events. A stream that ends is opened again by the next message, which first sends
the engine's ``initialize`` again, so a server that restarted still serves the
engine's session.

What it does not do: open the optional GET stream a streamable-HTTP server may use
for messages that answer no request. Redirects are not followed, and an
``endpoint`` on another origin is refused, so a header is only ever sent to the
recorded url's origin. No header value is ever written to stderr or into an error
the engine sees.

Standard library only, plus the standard-library leaf
:mod:`kiro_crew.mcp_structured_text`, until a request fails: the engine starts
this once per server. A failed request then imports
:mod:`kiro_crew.asset_downloader`, which carries the platform layer, to name its
cause in plain words (``connection refused``). A result whose payload is only in
``structuredContent`` is relayed with that object added as a JSON text block,
since the engine reads only ``content``.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import IO, Any, Iterator

from kiro_crew.mcp_structured_text import with_structured_text

_SESSION_HEADER = "Mcp-Session-Id"
_PROTOCOL_HEADER = "MCP-Protocol-Version"
#: How long one read from the server may block. A tool call can run for minutes;
#: an event stream sends nothing between events.
_READ_TIMEOUT_SECS = 600.0
#: How long shutdown waits for in-flight requests once the engine closes stdin.
_DRAIN_SECS = 5.0
#: JSON-RPC "server error" range; the bridge's own failures use it.
_BRIDGE_ERROR = -32000
#: Sent unless the entry's own headers name one. urllib's default
#: ``Python-urllib/x.y`` is refused 403 by bot filters (Atlassian Rovo's), which
#: then reads as a bad auth header.
_USER_AGENT = "kirocrew-mcp-bridge/1"
#: The id of the ``initialize`` sent again on a reopened ``sse`` stream; its answer
#: is the bridge's own and never reaches the engine.
_REPEAT_ID = "kirocrew-bridge-reinitialize"
#: How long a reopened ``sse`` stream waits for the answer to that ``initialize``.
_REPEAT_SECS = 30.0
_STREAM_CLOSED = "stopped answering (its event stream closed)"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect, so the headers never follow one to another origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


#: The loopback hosts, as :func:`_normalized_host` spells them.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _normalized_host(host: str | None) -> str | None:
    """*host* as a WHATWG URL parser spells an IP literal (``127.1`` -> ``127.0.0.1``,
    ``0:0:0:0:0:0:0:1`` -> ``::1``), so the loopback test matches KAS's ``new URL``.

    Owned here, in the standard-library leaf, so the bridge and the projection
    (``kas_env_launch``) read a loopback host the same way."""
    if not host:
        return host
    if ":" in host:
        try:
            return ipaddress.IPv6Address(host).compressed
        except ValueError:
            return host
    candidate = host[:-1] if host.endswith(".") else host
    if candidate and all(c in "0123456789abcdefx." for c in candidate):
        try:
            return socket.inet_ntoa(socket.inet_aton(candidate))
        except OSError:
            return host
    return host


def _opener(url: str = "") -> urllib.request.OpenerDirector:
    """No redirects; and for plain ``http`` to a loopback host no environment proxy.

    ``no_proxy`` matches host spellings literally, so ``127.1`` would otherwise
    reach an ``http_proxy`` in cleartext with the headers. A plain ``http`` url to
    another host keeps the environment proxy, which may be its only route there.
    """
    handlers: list[urllib.request.BaseHandler] = [_NoRedirect()]
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() == "http" and _normalized_host(parts.hostname) in _LOOPBACK_HOSTS:
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def _network_cause(exc: BaseException) -> str:
    """:func:`kiro_crew.asset_downloader.network_failure_cause`, imported once a request
    failed: at module scope every server start would pay for the platform layer."""
    from kiro_crew.asset_downloader import network_failure_cause

    return network_failure_cause(exc)


def _request_ids(msg: Any) -> list[Any]:
    """The ids of the requests in one message (a request has a method and an id)."""
    items = msg if isinstance(msg, list) else [msg]
    return [
        m["id"] for m in items if isinstance(m, dict) and "method" in m and m.get("id") is not None
    ]


def _sent_inline(line: bytes) -> bool:
    """Whether *line* is sent before the next one is read: no request but initialize."""
    try:
        msg = json.loads(line)
    except ValueError:
        return True
    items = msg if isinstance(msg, list) else [msg]
    return all(
        not isinstance(m, dict)
        or "method" not in m
        or m.get("id") is None
        or m["method"] == "initialize"
        for m in items
    )


def _sse_events(stream: IO[bytes]) -> Iterator[tuple[str, bytes]]:
    """``(event, data)`` of each event in an event stream, ``data`` joined across its
    lines; an event that names no type is a ``message``."""
    event = "message"
    data: list[bytes] = []
    for raw in stream:
        line = raw.rstrip(b"\r\n")
        if not line:
            if data:
                yield event, b"\n".join(data)
            event, data = "message", []
        elif line.startswith(b"data:"):
            value = line[5:]
            data.append(value[1:] if value.startswith(b" ") else value)
        elif line.startswith(b"event:"):
            event = line[6:].strip().decode("utf-8", "replace")
    if data:
        yield event, b"\n".join(data)


#: The port a url that names none is reached on.
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _same_origin(url: str, other: str) -> bool:
    """Whether *url* has *other*'s scheme, host and port, a port left out being its
    scheme's default (``https://host`` is ``https://host:443``)."""

    def origin(text: str) -> tuple[str, str | None, int | None]:
        parts = urllib.parse.urlsplit(text)
        scheme, port = parts.scheme.lower(), parts.port
        return scheme, parts.hostname, _DEFAULT_PORTS.get(scheme) if port is None else port

    try:
        return origin(url) == origin(other)
    except ValueError:
        return False


def _moves_to_sse(status: int) -> bool:
    """Whether a refused ``initialize`` means the older ``sse`` transport, as KAS reads
    it: any 4xx but a refused credential, which that transport would refuse too."""
    return 400 <= status < 500 and status not in (401, 403)


class _NoAnswer(Exception):
    """A request the server did not answer: *reason* in plain words, never a header
    value, and the HTTP *status* when it answered with an error."""

    def __init__(self, reason: str, status: int | None = None) -> None:
        super().__init__(reason)
        self.reason, self.status = reason, status


def _cut_off(exc: BaseException) -> str:
    """Why an answer stopped part way: the class name alone for a malformed one
    (``IncompleteRead``), since its text carries the server's bytes."""
    if isinstance(exc, http.client.HTTPException):
        return f"stopped answering ({type(exc).__name__})"
    return f"stopped answering ({_network_cause(exc) or exc})"


class Bridge:
    """Relay one engine's stdio to one remote server."""

    #: Whether a refused ``initialize`` is tried again over the older ``sse``
    #: transport (:func:`_moves_to_sse`).
    falls_back_to_sse = True

    def __init__(
        self,
        url: str,
        headers: dict[str, str],
        *,
        server: str,
        out: IO[bytes],
        err: IO[str],
        opener: urllib.request.OpenerDirector | None = None,
    ) -> None:
        self._url = url
        self._headers = dict(headers)
        if not any(name.lower() == "user-agent" for name in self._headers):
            self._headers["User-Agent"] = _USER_AGENT
        self._server = server
        self._out, self._err = out, err
        self._opener = opener or _opener(url)
        self._lock = threading.Lock()
        self._session_id: str | None = None
        self._protocol: str | None = None
        self._initialize_ids: set[Any] = set()
        #: The older transport, once a stream it opened named where to POST.
        self._sse: _LegacySse | None = None

    def _request_headers(self) -> dict[str, str]:
        headers = dict(self._headers)
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "application/json, text/event-stream"
        with self._lock:
            if self._session_id:
                headers[_SESSION_HEADER] = self._session_id
            if self._protocol:
                headers[_PROTOCOL_HEADER] = self._protocol
        return headers

    def _emit(self, msg: Any) -> Any:
        """Write one message to the engine; return the ids of responses it carried."""
        items = msg if isinstance(msg, list) else [msg]
        answered = []
        with self._lock:
            for item in items:
                if not isinstance(item, dict):
                    continue
                if "method" not in item and "id" in item:
                    answered.append(item["id"])
                    result = item.get("result")
                    if item["id"] in self._initialize_ids and isinstance(result, dict):
                        version = result.get("protocolVersion")
                        if isinstance(version, str) and version:
                            self._protocol = version
                    # The engine reads only ``content``: a payload only in
                    # ``structuredContent`` reached it as no output.
                    with_text = with_structured_text(result)
                    if with_text is not None:
                        item = {**item, "result": with_text}
                self._out.write(json.dumps(item, separators=(",", ":")).encode("utf-8") + b"\n")
            self._out.flush()
        return answered

    def _fail(self, ids: list[Any], reason: str) -> None:
        """Answer each unanswered request with an error naming *reason*, never a value."""
        message = f"kirocrew: MCP server {self._server!r} {reason}"
        print(message, file=self._err, flush=True)
        for rid in ids:
            self._emit(
                {"jsonrpc": "2.0", "id": rid, "error": {"code": _BRIDGE_ERROR, "message": message}}
            )

    def _http_error_reason(self, exc: urllib.error.HTTPError) -> str:
        """The reason an HTTP error answer is reported with; never a header value."""
        hint = " (check its headers)" if exc.code in (401, 403) else ""
        if exc.code in (301, 302, 303, 307, 308):
            hint = " (a redirect, which the bridge does not follow)"
        return f"answered HTTP {exc.code}{hint}"

    def _relay_body(self, body: bytes) -> list[Any]:
        try:
            msg = json.loads(body)
        except ValueError:
            return []
        return self._emit(msg)

    def _open(self, request: urllib.request.Request) -> Any:
        """The server's answer to *request*; :class:`_NoAnswer` says why there is none."""
        try:
            return self._opener.open(request, timeout=_READ_TIMEOUT_SECS)
        except urllib.error.HTTPError as exc:
            reason = self._http_error_reason(exc)
            exc.close()
            raise _NoAnswer(reason, exc.code) from None
        except http.client.HTTPException as exc:
            # A malformed answer (a status line that is not HTTP): the class name
            # alone, since the exception text carries the server's bytes.
            raise _NoAnswer(f"could not be reached ({type(exc).__name__})") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            reason = _network_cause(exc) or getattr(exc, "reason", None) or type(exc).__name__
            raise _NoAnswer(f"could not be reached ({reason})") from None

    def send(self, line: bytes) -> None:
        """POST one engine message and relay whatever the server answers."""
        try:
            msg = json.loads(line)
        except ValueError:
            print(
                f"kirocrew: MCP bridge for {self._server!r} dropped a line that is not JSON",
                file=self._err,
                flush=True,
            )
            return
        ids = _request_ids(msg)
        initialize = isinstance(msg, dict) and msg.get("method") == "initialize"
        if initialize and "id" in msg:
            with self._lock:
                self._initialize_ids.add(msg["id"])
        if self._sse is not None:
            self._sse.send(msg, ids)
            return
        body = json.dumps(msg, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self._url, data=body, headers=self._request_headers(), method="POST"
        )
        try:
            resp = self._open(request)
        except _NoAnswer as exc:
            if initialize and self.falls_back_to_sse and _moves_to_sse(exc.status or 0):
                sse = _LegacySse(self)
                if sse.send(msg, ids):
                    self._sse = sse
                return
            self._fail(ids, exc.reason)
            return
        answered: list[Any] = []
        try:
            with resp:
                session_id = resp.headers.get(_SESSION_HEADER)
                if session_id:
                    with self._lock:
                        self._session_id = session_id
                if resp.status in (202, 204):
                    return
                if resp.headers.get_content_type() == "text/event-stream":
                    for _event, data in _sse_events(resp):
                        answered += self._relay_body(data)
                else:
                    answered += self._relay_body(resp.read())
        except (http.client.HTTPException, OSError) as exc:
            # A body cut off before its declared length (``IncompleteRead``), or the
            # network dropped it.
            self._fail([i for i in ids if i not in answered], _cut_off(exc))
            return
        missing = [i for i in ids if i not in answered]
        if missing:
            self._fail(missing, "ended its answer without a response")

    def close(self) -> None:
        """End the server's session, when it gave one; best effort."""
        with self._lock:
            session_id = self._session_id
        if not session_id:
            return
        headers = {**self._headers, _SESSION_HEADER: session_id}
        request = urllib.request.Request(self._url, headers=headers, method="DELETE")
        try:
            self._opener.open(request, timeout=_DRAIN_SECS).close()
        except (urllib.error.URLError, OSError, ValueError):
            pass

    def serve(self, stdin: IO[bytes]) -> int:
        """Relay every line of *stdin* until it closes; the process exit status.

        A message that carries no request (a notification, a response to a server
        request) and ``initialize`` are sent inline, so their POST completes before
        the next line is read: a stateful server sees ``notifications/initialized``
        before the request that follows it. Every other request runs on its own
        thread, so a long tool call does not hold up the rest.
        """
        workers: list[threading.Thread] = []
        for line in stdin:
            if not line.strip():
                continue
            if _sent_inline(line):
                self.send(line)
                continue
            worker = threading.Thread(target=self.send, args=(line,), daemon=True)
            worker.start()
            workers.append(worker)
            workers = [w for w in workers if w.is_alive()]
        for worker in workers:
            worker.join(_DRAIN_SECS)
        if self._sse is not None:
            self._sse.drain()
        self.close()
        return 0


class _LegacySse:
    """The older two-endpoint ``sse`` transport, for one :class:`Bridge`.

    A GET to the bridge's url opens the event stream, and its ``endpoint`` event
    names where each message is POSTed. The answers arrive as ``message`` events,
    which a reader thread relays to the engine. The stream is opened by the first
    message that finds none open; one that ends answers each request still waiting
    on it with why, and the next message opens another.
    """

    def __init__(self, bridge: Bridge) -> None:
        self._bridge = bridge
        #: Held while a stream is opened, so concurrent messages open one.
        self._opening = threading.Lock()
        #: Guards the two below, and is notified as requests are answered.
        self._answered = threading.Condition()
        self._post_url: str | None = None
        self._pending: set[Any] = set()
        self._initialize: dict[str, Any] | None = None
        self._repeated = threading.Event()

    def send(self, msg: Any, ids: list[Any]) -> bool:
        """POST *msg* on the open stream, opening one first; whether one was open."""
        initialize = isinstance(msg, dict) and msg.get("method") == "initialize"
        if initialize:
            self._initialize = msg
        with self._opening:
            post_url = self._post_url or self._connect(ids, repeat=not initialize)
        if post_url:
            self._post(post_url, msg, ids)
        return bool(post_url)

    def drain(self) -> None:
        """Wait a bounded while for the answers still on their way."""
        with self._answered:
            self._answered.wait_for(lambda: not self._pending, _DRAIN_SECS)

    def _take(self, ids: list[Any]) -> list[Any]:
        """Those of *ids* still waiting for an answer, taken off the waiting set."""
        with self._answered:
            taken = [i for i in ids if i in self._pending]
            self._pending.difference_update(taken)
            self._answered.notify_all()
        return taken

    def _post(self, url: str, msg: Any, ids: list[Any]) -> None:
        with self._answered:
            ended = self._post_url != url
            if not ended:
                self._pending.update(ids)
        if ended:
            self._bridge._fail(ids, _STREAM_CLOSED)
            return
        body = json.dumps(msg, separators=(",", ":")).encode("utf-8")
        headers = {**self._bridge._headers, "Content-Type": "application/json"}
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            self._bridge._open(request).close()
        except _NoAnswer as exc:
            self._bridge._fail(self._take(ids), exc.reason)

    def _connect(self, ids: list[Any], *, repeat: bool) -> str | None:
        """Open a stream; where to POST, or ``None`` once each of *ids* is answered
        with why. *repeat* first sends the engine's ``initialize`` again, for a server
        that restarted and so does not know the engine's session."""
        bridge = self._bridge
        headers = {**bridge._headers, "Accept": "text/event-stream"}
        try:
            stream = bridge._open(urllib.request.Request(bridge._url, headers=headers))
        except _NoAnswer as exc:
            bridge._fail(ids, exc.reason)
            return None
        events = _sse_events(stream)
        post_url = None
        reason = "ended its event stream without saying where to send messages"
        try:
            endpoint = next((data for event, data in events if event == "endpoint"), None)
        except (http.client.HTTPException, OSError) as exc:
            endpoint, reason = None, _cut_off(exc)
        if endpoint is not None:
            post_url = urllib.parse.urljoin(
                bridge._url, endpoint.decode("utf-8", "replace").strip()
            )
            if not _same_origin(post_url, bridge._url):
                post_url = None
                reason = "asked for its messages on another site, where its headers are not sent"
        if post_url is None:
            stream.close()
            bridge._fail(ids, reason)
            return None
        with self._answered:
            self._post_url = post_url
        threading.Thread(target=self._read, args=(stream, events), daemon=True).start()
        if repeat and self._initialize is not None:
            self._repeated.clear()
            self._post(post_url, {**self._initialize, "id": _REPEAT_ID}, [])
            self._repeated.wait(_REPEAT_SECS)
            self._post(post_url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, [])
        return post_url

    def _read(self, stream: Any, events: Iterator[tuple[str, bytes]]) -> None:
        """Relay each ``message`` event until the stream ends."""
        reason = _STREAM_CLOSED
        try:
            with stream:
                for event, data in events:
                    if event == "message":
                        self._relay(data)
        except (http.client.HTTPException, OSError) as exc:
            reason = _cut_off(exc)
        with self._answered:
            waiting, self._pending = list(self._pending), set()
            self._post_url = None
            self._answered.notify_all()
        self._repeated.set()
        if waiting:
            self._bridge._fail(waiting, reason)

    def _relay(self, data: bytes) -> None:
        try:
            msg = json.loads(data)
        except ValueError:
            return
        if isinstance(msg, dict) and msg.get("id") == _REPEAT_ID and "method" not in msg:
            self._repeated.set()
            return
        self._take(self._bridge._emit(msg))


def run_bridge(url: str, headers: dict[str, str], *, server: str) -> int:
    """Serve the engine on this process's stdio; the exit status."""
    bridge = Bridge(url, headers, server=server, out=sys.stdout.buffer, err=sys.stderr)
    return bridge.serve(sys.stdin.buffer)
