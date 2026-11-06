"""A stdio MCP server that relays to a streamable-HTTP one, adding headers kept local.

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

What it does not do: open the optional GET stream a server may use for messages
that answer no request, or speak the older two-endpoint ``sse`` transport (the
projection does not bridge such an entry). Redirects are not followed, so a
header is only ever sent to the recorded url's origin. No header value is ever
written to stderr or into an error the engine sees.

Standard library only: the engine starts this once per server.
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

_SESSION_HEADER = "Mcp-Session-Id"
_PROTOCOL_HEADER = "MCP-Protocol-Version"
#: How long one read from the server may block. A tool call can run for minutes;
#: an event stream sends nothing between events.
_READ_TIMEOUT_SECS = 600.0
#: How long shutdown waits for in-flight requests once the engine closes stdin.
_DRAIN_SECS = 5.0
#: JSON-RPC "server error" range; the bridge's own failures use it.
_BRIDGE_ERROR = -32000


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


def _sse_data(stream: IO[bytes]) -> Iterator[bytes]:
    """The ``data`` of each event in an event stream, joined across its lines."""
    data: list[bytes] = []
    for raw in stream:
        line = raw.rstrip(b"\r\n")
        if not line:
            if data:
                yield b"\n".join(data)
            data = []
        elif line.startswith(b"data:"):
            value = line[5:]
            data.append(value[1:] if value.startswith(b" ") else value)
    if data:
        yield b"\n".join(data)


class Bridge:
    """Relay one engine's stdio to one remote server."""

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
        self._server = server
        self._out, self._err = out, err
        self._opener = opener or _opener(url)
        self._lock = threading.Lock()
        self._session_id: str | None = None
        self._protocol: str | None = None
        self._initialize_ids: set[Any] = set()

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

    def _relay_body(self, body: bytes) -> list[Any]:
        try:
            msg = json.loads(body)
        except ValueError:
            return []
        return self._emit(msg)

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
        if isinstance(msg, dict) and msg.get("method") == "initialize" and "id" in msg:
            with self._lock:
                self._initialize_ids.add(msg["id"])
        body = json.dumps(msg, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self._url, data=body, headers=self._request_headers(), method="POST"
        )
        try:
            resp = self._opener.open(request, timeout=_READ_TIMEOUT_SECS)
        except urllib.error.HTTPError as exc:
            exc.close()
            hint = " (check its headers)" if exc.code in (401, 403) else ""
            if exc.code in (301, 302, 303, 307, 308):
                hint = " (a redirect, which the bridge does not follow)"
            self._fail(ids, f"answered HTTP {exc.code}{hint}")
            return
        except http.client.HTTPException as exc:
            # A malformed answer (a status line that is not HTTP): the class name
            # alone, since the exception text carries the server's bytes.
            self._fail(ids, f"could not be reached ({type(exc).__name__})")
            return
        except (urllib.error.URLError, OSError, ValueError) as exc:
            reason = getattr(exc, "reason", None) or type(exc).__name__
            self._fail(ids, f"could not be reached ({reason})")
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
                    for data in _sse_data(resp):
                        answered += self._relay_body(data)
                else:
                    answered += self._relay_body(resp.read())
        except http.client.HTTPException as exc:
            # A body cut off before its declared length (``IncompleteRead``).
            reason = type(exc).__name__
            self._fail([i for i in ids if i not in answered], f"stopped answering ({reason})")
            return
        except OSError as exc:
            self._fail([i for i in ids if i not in answered], f"stopped answering ({exc})")
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
        self.close()
        return 0


def run_bridge(url: str, headers: dict[str, str], *, server: str) -> int:
    """Serve the engine on this process's stdio; the exit status."""
    bridge = Bridge(url, headers, server=server, out=sys.stdout.buffer, err=sys.stderr)
    return bridge.serve(sys.stdin.buffer)
