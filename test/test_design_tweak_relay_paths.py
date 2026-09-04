"""Tests for _DevProxyHandler._relay_http, _relay_ws, and _start_inject_proxy.

Covers lines ~1240-1560 of server.py: the HTTP relay (body/header forwarding,
size caps, credential stripping, redirect rewriting, error paths), the WebSocket
relay (handshake replay, header sanitizing, selectors pump, idle cap), and the
proxy startup helper.
"""

from __future__ import annotations

import io
import socket
import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

from kiro_crew.apps.builtins.design_tweak.backend import server

# ---------------------------------------------------------------------------
# Probe class: drives _DevProxyHandler without a real socket.
# ---------------------------------------------------------------------------


class _FakeHeaders(dict):
    """Minimal headers container that supports .get() and iteration."""

    def items(self):
        return list(super().items())

    def get(self, key, default=None):
        # Case-insensitive lookup matching http.server's behavior.
        for k, v in super().items():
            if k.lower() == key.lower():
                return v
        return default


class _Probe(server._DevProxyHandler):
    """Handler probe that avoids BaseHTTPRequestHandler.__init__ entirely."""

    def __init__(
        self,
        *,
        command: str = "GET",
        path: str = "/",
        headers: dict | None = None,
        body: bytes = b"",
        connection: Any = None,
    ):
        self.command = command
        self.path = path
        self.headers = _FakeHeaders(headers or {})
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.upstream_host = "127.0.0.1"
        self.upstream_port = 5173
        self.proxy_port = 45678
        self.close_connection = False
        self.connection = connection
        self.errors: list[tuple[int, str]] = []
        self._response_code: int | None = None
        self._sent_headers: list[tuple[str, str]] = []
        self._ended = False

    def send_error(self, code, message=None, explain=None):
        self.errors.append((code, message or ""))

    def send_response(self, code, message=None):
        self._response_code = code

    def send_header(self, keyword, value):
        self._sent_headers.append((keyword, value))

    def end_headers(self):
        self._ended = True


def _make_probe(**kwargs) -> _Probe:
    p = _Probe.__new__(_Probe)
    _Probe.__init__(p, **kwargs)
    return p


# ---------------------------------------------------------------------------
# Fake HTTP response mimicking http.client.HTTPResponse.
# ---------------------------------------------------------------------------

class _FakeHTTPResponse:
    def __init__(self, status=200, headers=None, body=b""):
        self.status = status
        self._headers = headers or []
        self._body = body

    def getheader(self, name):
        for k, v in self._headers:
            if k.lower() == name.lower():
                return v
        return None

    def getheaders(self):
        return self._headers

    def read(self, size=-1):
        if size < 0:
            return self._body
        return self._body[:size]


class _FakeHTTPConnection:
    """Replaces http.client.HTTPConnection for relay tests."""

    def __init__(self, response: _FakeHTTPResponse):
        self._response = response
        self.closed = False
        self.last_request: tuple[str, str, bytes | None, dict] | None = None

    def __call__(self, host, port, **kwargs):
        return self

    def request(self, method, path, body=None, headers=None):
        self.last_request = (method, path, body, headers or {})

    def getresponse(self):
        return self._response

    def close(self):
        self.closed = True


# ===========================================================================
# _relay_http tests
# ===========================================================================


class TestRelayHttpBodySizeCap:
    """Request body exceeding MAX_BODY_BYTES triggers 413 and closes connection."""

    def test_oversized_body_returns_413(self):
        """If this fails: requests larger than the memory cap reach the dev server,
        enabling denial-of-service by exhausting backend memory."""
        p = _make_probe(
            headers={"Content-Length": str(server.MAX_BODY_BYTES + 1)},
        )
        p._relay_http()
        assert p.errors == [(413, f"request body over the {server.MAX_BODY_BYTES}-byte proxy limit")]
        assert p.close_connection is True

    def test_invalid_content_length_treated_as_no_body(self):
        """If this fails: a non-numeric Content-Length causes an unhandled
        exception instead of being treated as a zero-length body."""
        fake_resp = _FakeHTTPResponse(200, [], b"ok")
        fake_conn = _FakeHTTPConnection(fake_resp)
        p = _make_probe(headers={"Content-Length": "not-a-number"})

        with patch.object(server.http.client, "HTTPConnection", fake_conn):
            p._relay_http()

        # size = -1 falls through both guards (not > MAX, not > 0), so the
        # request proceeds with an empty body — no error, relay succeeds.
        assert p.errors == []
        assert p._response_code == 200


class TestRelayHttpBodyReadErrors:
    """OSError during body read (e.g. timeout) yields 408."""

    def test_body_read_timeout(self):
        """If this fails: a client that stalls mid-body hangs the handler thread
        forever, exhausting the thread pool."""

        class _FailingRead(io.BytesIO):
            def read(self, n=-1):
                raise OSError("timed out")

        p = _make_probe(headers={"Content-Length": "100"})
        p.rfile = _FailingRead()
        p._relay_http()
        assert p.errors == [(408, "request body timed out")]
        assert p.close_connection is True

    def test_short_body(self):
        """If this fails: a truncated request body is forwarded as a valid request,
        causing the dev server to act on partial/corrupt data."""
        p = _make_probe(
            headers={"Content-Length": "100"},
            body=b"short",  # only 5 bytes, not 100
        )
        p._relay_http()
        assert p.errors == [(400, "request body shorter than Content-Length")]
        assert p.close_connection is True


class TestRelayHttpCredentialStripping:
    """Credential headers must never reach the dev server."""

    def test_cookie_stripped_from_request(self):
        """If this fails: the dashboard's session cookie leaks to the user's dev
        server process, enabling session hijacking."""
        fake_resp = _FakeHTTPResponse(200, [("Content-Type", "text/plain")], b"ok")
        fake_conn = _FakeHTTPConnection(fake_resp)

        p = _make_probe(
            headers={
                "Cookie": "kirocrew_session=secret123",
                "Authorization": "Bearer token",
                "X-Custom": "safe",
            },
        )
        with patch.object(server.http.client, "HTTPConnection", fake_conn):
            p._relay_http()

        _, _, _, sent_headers = fake_conn.last_request
        # Neither credential header should be forwarded.
        assert "Cookie" not in sent_headers
        assert "cookie" not in sent_headers
        assert "Authorization" not in sent_headers
        assert "authorization" not in sent_headers
        # Non-credential header passes through.
        assert sent_headers.get("X-Custom") == "safe"

    def test_set_cookie_stripped_from_response(self):
        """If this fails: a malicious dev server can overwrite the dashboard session
        cookie, logging the user out or hijacking their session."""
        fake_resp = _FakeHTTPResponse(
            200,
            [
                ("Content-Type", "text/plain"),
                ("Set-Cookie", "kirocrew_session=evil"),
                ("X-Safe", "keep"),
            ],
            b"body",
        )
        fake_conn = _FakeHTTPConnection(fake_resp)
        p = _make_probe()

        with patch.object(server.http.client, "HTTPConnection", fake_conn):
            p._relay_http()

        header_names = [h[0].lower() for h in p._sent_headers]
        assert "set-cookie" not in header_names
        assert "x-safe" in header_names


class TestRelayHttpHopByHopStripping:
    """Hop-by-hop headers must be stripped from both request and response."""

    def test_request_hop_by_hop_stripped(self):
        """If this fails: HTTP framing headers leak to the upstream, potentially
        desynchronizing the connection (request smuggling)."""
        fake_resp = _FakeHTTPResponse(200, [], b"")
        fake_conn = _FakeHTTPConnection(fake_resp)

        p = _make_probe(
            headers={
                "Connection": "keep-alive",
                "Transfer-Encoding": "chunked",
                "Accept": "text/html",
            },
        )
        with patch.object(server.http.client, "HTTPConnection", fake_conn):
            p._relay_http()

        _, _, _, sent_headers = fake_conn.last_request
        assert "Connection" not in sent_headers
        assert "Transfer-Encoding" not in sent_headers
        assert sent_headers.get("Accept") == "text/html"

    def test_response_hop_by_hop_stripped(self):
        """If this fails: hop-by-hop headers in the upstream response corrupt
        the proxy-to-client framing."""
        fake_resp = _FakeHTTPResponse(
            200,
            [
                ("Transfer-Encoding", "chunked"),
                ("X-Good", "value"),
            ],
            b"data",
        )
        fake_conn = _FakeHTTPConnection(fake_resp)
        p = _make_probe()

        with patch.object(server.http.client, "HTTPConnection", fake_conn):
            p._relay_http()

        header_names = [h[0].lower() for h in p._sent_headers]
        assert "transfer-encoding" not in header_names
        assert "x-good" in header_names


class TestRelayHttpAcceptEncodingStripped:
    """Accept-Encoding is dropped so we get identity-encoded HTML for rewriting."""

    def test_accept_encoding_not_forwarded(self):
        """If this fails: the upstream returns gzip-encoded HTML, making the
        overlay injection produce garbled bytes the browser cannot parse."""
        fake_resp = _FakeHTTPResponse(200, [], b"")
        fake_conn = _FakeHTTPConnection(fake_resp)
        p = _make_probe(headers={"Accept-Encoding": "gzip, deflate"})

        with patch.object(server.http.client, "HTTPConnection", fake_conn):
            p._relay_http()

        _, _, _, sent_headers = fake_conn.last_request
        assert "Accept-Encoding" not in sent_headers
        assert "accept-encoding" not in sent_headers


class TestRelayHttpHostRewrite:
    """Host header is rewritten to the upstream target."""

    def test_host_header_set_to_upstream(self):
        """If this fails: the upstream dev server receives the proxy's Host header,
        causing virtual-host routing to serve the wrong site."""
        fake_resp = _FakeHTTPResponse(200, [], b"")
        fake_conn = _FakeHTTPConnection(fake_resp)
        p = _make_probe(headers={"Host": "127.0.0.1:45678"})

        with patch.object(server.http.client, "HTTPConnection", fake_conn):
            p._relay_http()

        _, _, _, sent_headers = fake_conn.last_request
        assert sent_headers["Host"] == "127.0.0.1:5173"


class TestRelayHttpUpstreamError:
    """Connection failure to dev server returns 502."""

    def test_connection_refused(self):
        """If this fails: an unresponsive dev server causes an unhandled exception
        that crashes the handler thread."""
        p = _make_probe()

        fake_conn = MagicMock()
        fake_conn.return_value = fake_conn
        fake_conn.request.side_effect = OSError("connection refused")

        with patch.object(server.http.client, "HTTPConnection", fake_conn):
            p._relay_http()

        assert p.errors == [(502, "dev server unreachable: connection refused")]


class TestRelayHttpResponseSizeCap:
    """Upstream response over MAX_STATIC_BYTES yields 502."""

    def test_oversized_response(self):
        """If this fails: a dev server returning a huge asset exhausts backend
        memory, causing denial-of-service."""
        big_body = b"X" * (server.MAX_STATIC_BYTES + 1)
        fake_resp = _FakeHTTPResponse(200, [], big_body)
        fake_conn = _FakeHTTPConnection(fake_resp)
        p = _make_probe()

        with patch.object(server.http.client, "HTTPConnection", fake_conn):
            p._relay_http()

        assert p.errors[0] == (
            502, f"dev server response over the {server.MAX_STATIC_BYTES}-byte preview limit"
        )


class TestRelayHttpHtmlRewrite:
    """HTML responses get the overlay script injected."""

    def test_html_body_is_rewritten(self):
        """If this fails: the select-to-edit overlay never loads in HTML previews,
        breaking the core design-tweak interaction."""
        html = b"<html><head></head><body>hello</body></html>"
        fake_resp = _FakeHTTPResponse(
            200, [("Content-Type", "text/html; charset=utf-8")], html
        )
        fake_conn = _FakeHTTPConnection(fake_resp)
        p = _make_probe()

        with patch.object(server.http.client, "HTTPConnection", fake_conn):
            p._relay_http()

        written = p.wfile.getvalue()
        assert server._OVERLAY_PATH.encode() in written

    def test_non_html_not_rewritten(self):
        """If this fails: non-HTML assets like CSS/JS get corrupted by the
        overlay injection, breaking the preview."""
        css = b"body { color: red; }"
        fake_resp = _FakeHTTPResponse(
            200, [("Content-Type", "text/css")], css
        )
        fake_conn = _FakeHTTPConnection(fake_resp)
        p = _make_probe()

        with patch.object(server.http.client, "HTTPConnection", fake_conn):
            p._relay_http()

        written = p.wfile.getvalue()
        assert written == css


class TestRelayHttpHeadMethod:
    """HEAD responses relay headers but suppress the body."""

    def test_head_no_body(self):
        """If this fails: HEAD responses include a body, violating HTTP semantics
        and confusing caches/clients."""
        fake_resp = _FakeHTTPResponse(
            200, [("Content-Type", "text/plain")], b"should not appear"
        )
        fake_conn = _FakeHTTPConnection(fake_resp)
        p = _make_probe(command="HEAD")

        with patch.object(server.http.client, "HTTPConnection", fake_conn):
            p._relay_http()

        assert p.wfile.getvalue() == b""
        # Headers are still sent.
        assert p._ended is True


class TestRelayHttpMalformedHeaderName:
    """Response headers with invalid names are silently dropped."""

    def test_invalid_header_name_dropped(self):
        """If this fails: a malformed header name from the dev server enables
        response splitting (HTTP header injection)."""
        fake_resp = _FakeHTTPResponse(
            200,
            [
                ("Valid-Header", "ok"),
                ("Invalid\r\nHeader", "evil"),
            ],
            b"ok",
        )
        fake_conn = _FakeHTTPConnection(fake_resp)
        p = _make_probe()

        with patch.object(server.http.client, "HTTPConnection", fake_conn):
            p._relay_http()

        header_names = [h[0] for h in p._sent_headers]
        assert "Valid-Header" in header_names
        assert "Invalid\r\nHeader" not in header_names


class TestRelayHttpRedirectRewrite:
    """Upstream redirects pointing at the dev server are rewritten to the proxy."""

    def test_redirect_rewritten_to_proxy(self):
        """If this fails: following a redirect takes the iframe off-proxy onto the
        bare dev server port, leaking the dashboard cookie."""
        fake_resp = _FakeHTTPResponse(
            302,
            [("Location", "http://127.0.0.1:5173/new-path?q=1")],
            b"",
        )
        fake_conn = _FakeHTTPConnection(fake_resp)
        p = _make_probe()

        with patch.object(server.http.client, "HTTPConnection", fake_conn):
            p._relay_http()

        loc_headers = [v for k, v in p._sent_headers if k == "Location"]
        assert len(loc_headers) == 1
        assert loc_headers[0] == "http://127.0.0.1:45678/new-path?q=1"

    def test_redirect_to_different_port_not_rewritten(self):
        """If this fails: redirects to other services are incorrectly rewritten,
        breaking external navigation."""
        fake_resp = _FakeHTTPResponse(
            302,
            [("Location", "http://127.0.0.1:8080/other")],
            b"",
        )
        fake_conn = _FakeHTTPConnection(fake_resp)
        p = _make_probe()

        with patch.object(server.http.client, "HTTPConnection", fake_conn):
            p._relay_http()

        loc_headers = [v for k, v in p._sent_headers if k == "Location"]
        assert loc_headers[0] == "http://127.0.0.1:8080/other"

    def test_relative_redirect_unchanged(self):
        """If this fails: relative redirects are incorrectly rewritten, breaking
        in-app navigation."""
        fake_resp = _FakeHTTPResponse(
            302,
            [("Location", "/relative/path")],
            b"",
        )
        fake_conn = _FakeHTTPConnection(fake_resp)
        p = _make_probe()

        with patch.object(server.http.client, "HTTPConnection", fake_conn):
            p._relay_http()

        loc_headers = [v for k, v in p._sent_headers if k == "Location"]
        assert loc_headers[0] == "/relative/path"

    def test_external_host_redirect_unchanged(self):
        """If this fails: redirects to external hosts are incorrectly rewritten."""
        fake_resp = _FakeHTTPResponse(
            302,
            [("Location", "https://example.com/page")],
            b"",
        )
        fake_conn = _FakeHTTPConnection(fake_resp)
        p = _make_probe()

        with patch.object(server.http.client, "HTTPConnection", fake_conn):
            p._relay_http()

        loc_headers = [v for k, v in p._sent_headers if k == "Location"]
        assert loc_headers[0] == "https://example.com/page"


class TestKeepRedirectLocal:
    """Unit tests for _keep_redirect_local edge cases."""

    def test_localhost_variant(self):
        """If this fails: localhost-addressed redirects bypass rewriting, leaking
        the cookie via port-agnostic cookie scoping."""
        p = _make_probe()
        result = p._keep_redirect_local("http://localhost:5173/path")
        assert result == "http://127.0.0.1:45678/path"

    def test_fragment_preserved(self):
        """If this fails: hash fragments are lost during redirect rewriting,
        breaking single-page-app navigation."""
        p = _make_probe()
        result = p._keep_redirect_local("http://127.0.0.1:5173/page#section")
        assert result == "http://127.0.0.1:45678/page#section"

    def test_unparseable_location_returned_as_is(self):
        """If this fails: a malformed Location header causes an unhandled
        exception, crashing the relay instead of forwarding harmlessly."""
        p = _make_probe()
        # A port with non-numeric characters triggers ValueError in urlparse.
        result = p._keep_redirect_local("http://127.0.0.1:abc/path")
        assert result == "http://127.0.0.1:abc/path"

    def test_non_http_scheme_unchanged(self):
        """If this fails: ftp/data URIs are incorrectly rewritten."""
        p = _make_probe()
        result = p._keep_redirect_local("ftp://127.0.0.1:5173/file")
        assert result == "ftp://127.0.0.1:5173/file"

    def test_https_default_port(self):
        """If this fails: HTTPS redirect without explicit port defaults wrong."""
        p = _make_probe()
        p.upstream_port = 443
        result = p._keep_redirect_local("https://127.0.0.1/path")
        assert result == "http://127.0.0.1:45678/path"


# ===========================================================================
# _relay_ws tests
# ===========================================================================


class TestRelayWsConnectionFailure:
    """WS relay returns 502 when the upstream is unreachable."""

    def test_connection_refused(self):
        """If this fails: an unreachable dev server causes an unhandled exception
        in the WS relay, crashing the handler thread."""
        p = _make_probe(
            headers={"Upgrade": "websocket", "Sec-WebSocket-Key": "dGhlIHNh"},
        )
        with patch.object(
            server.socket, "create_connection", side_effect=OSError("refused")
        ):
            p._relay_ws()

        assert p.errors == [(502, "dev server unreachable")]


class TestRelayWsCredentialStripping:
    """WS handshake must strip credential headers before forwarding."""

    def test_cookie_not_in_handshake(self):
        """If this fails: the dashboard session cookie leaks to the dev server
        via the WebSocket upgrade request, enabling session hijacking."""
        sent_data = []

        class _FakeUpstream:
            def sendall(self, data):
                sent_data.append(data)

            def settimeout(self, t):
                pass

            def recv(self, n):
                # Return a valid 101 handshake.
                return b"HTTP/1.1 101 Switching\r\nUpgrade: websocket\r\n\r\n"

            def close(self):
                pass

            def fileno(self):
                return -1

        p = _make_probe(
            headers={
                "Upgrade": "websocket",
                "Cookie": "kirocrew_session=secret",
                "Authorization": "Bearer tok",
                "Sec-WebSocket-Key": "dGhlIHNh",
            },
            connection=MagicMock(),
        )
        p.connection.sendall = MagicMock()
        # Make the pump exit immediately after handshake.
        p.connection.sendall.side_effect = OSError("done")

        with patch.object(server.socket, "create_connection", return_value=_FakeUpstream()):
            p._relay_ws()

        handshake = sent_data[0].decode("latin-1")
        assert "Cookie" not in handshake
        assert "kirocrew_session" not in handshake
        assert "Authorization" not in handshake
        assert "Bearer" not in handshake


class TestRelayWsHandshakeSanitization:
    """The 101 response must strip Set-Cookie before relaying to the client."""

    def test_set_cookie_stripped_from_101(self):
        """If this fails: a 101 upgrade bypasses the credential response filter,
        letting a dev server overwrite the dashboard session cookie."""
        handshake_response = (
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\n"
            b"Set-Cookie: kirocrew_session=evil\r\n"
            b"Connection: Upgrade\r\n"
            b"\r\n"
        )

        up_a, up_b = socket.socketpair()
        down_a, down_b = socket.socketpair()

        p = _make_probe(
            headers={"Upgrade": "websocket", "Sec-WebSocket-Key": "abc"},
            connection=down_a,
        )

        def feed_upstream():
            # Feed the handshake response, then close to end the pump.
            up_b.sendall(handshake_response)
            time.sleep(0.05)
            up_b.close()

        feeder = threading.Thread(target=feed_upstream, daemon=True)
        feeder.start()

        with patch.object(server.socket, "create_connection", return_value=up_b):
            # Cannot use up_b for both create_connection and the feeder.
            # Instead, use a wrapper that separates the send side.
            pass

        # Redo: create_connection returns up_b, which the relay reads from.
        # We write into up_a (the other end of the pair).
        up_c, up_d = socket.socketpair()

        def feed_up():
            up_c.sendall(handshake_response)
            time.sleep(0.05)
            up_c.close()

        # up_d is what relay sees as the upstream socket.
        t_feed = threading.Thread(target=feed_up, daemon=True)
        t_feed.start()

        with patch.object(server.socket, "create_connection", return_value=up_d):
            p._relay_ws()

        t_feed.join(timeout=2)

        # Read what was sent to the downstream (client) side.
        down_a.close()
        sanitized = b""
        while True:
            chunk = down_b.recv(4096)
            if not chunk:
                break
            sanitized += chunk
        down_b.close()

        decoded = sanitized.decode("latin-1")
        assert "Set-Cookie" not in decoded
        assert "kirocrew_session" not in decoded
        assert "101 Switching Protocols" in decoded
        assert "Upgrade: websocket" in decoded


class TestRelayWsMalformedHandshake:
    """A dev server that never terminates its handshake headers yields 502."""

    def test_no_header_terminator(self):
        """If this fails: a misbehaving dev server hangs the handler indefinitely
        or grows memory unboundedly waiting for \\r\\n\\r\\n."""

        class _NeverEndsHeaders:
            def sendall(self, data):
                pass

            def settimeout(self, t):
                pass

            def recv(self, n):
                # Return data without \r\n\r\n, then close.
                return b""

            def close(self):
                pass

        p = _make_probe(
            headers={"Upgrade": "websocket", "Sec-WebSocket-Key": "x"},
        )
        with patch.object(
            server.socket, "create_connection", return_value=_NeverEndsHeaders()
        ):
            p._relay_ws()

        assert p.errors == [(502, "malformed upstream handshake")]


class TestRelayWsSendallFailure:
    """Failure to send the handshake upstream closes cleanly."""

    def test_upstream_sendall_fails(self):
        """If this fails: a broken upstream socket causes an unhandled exception
        instead of a clean close."""

        class _FailSend:
            def sendall(self, data):
                raise OSError("broken pipe")

            def settimeout(self, t):
                pass

            def close(self):
                pass

        p = _make_probe(
            headers={"Upgrade": "websocket", "Sec-WebSocket-Key": "x"},
        )
        with patch.object(
            server.socket, "create_connection", return_value=_FailSend()
        ):
            p._relay_ws()

        # Should return cleanly without errors (just closes).
        assert p.errors == []


class TestRelayWsHeadReadOSError:
    """OSError during handshake header read closes cleanly."""

    def test_recv_oserror_during_head_read(self):
        """If this fails: an OSError while reading the upstream handshake causes
        an unhandled exception, crashing the handler thread."""
        call_count = [0]

        class _ErrorAfterSend:
            def sendall(self, data):
                pass

            def settimeout(self, t):
                pass

            def recv(self, n):
                call_count[0] += 1
                if call_count[0] == 1:
                    return b"HTTP/1.1 101 OK\r\n"  # partial — no \r\n\r\n yet
                raise OSError("connection reset")

            def close(self):
                pass

        p = _make_probe(
            headers={"Upgrade": "websocket", "Sec-WebSocket-Key": "x"},
        )
        with patch.object(
            server.socket, "create_connection", return_value=_ErrorAfterSend()
        ):
            p._relay_ws()

        # Should return cleanly — the OSError is caught in the head-read loop.
        assert p.errors == []


class TestRelayWsPumpSendallError:
    """OSError on dst_sock.sendall in the pump exits cleanly."""

    def test_pump_sendall_oserror(self):
        """If this fails: a broken client socket during frame forwarding causes
        an unhandled exception instead of a clean pump exit."""
        handshake = b"HTTP/1.1 101 OK\r\nUpgrade: ws\r\n\r\n"

        up_a, up_b = socket.socketpair()
        down_a, down_b = socket.socketpair()

        # Wrap down_a so sendall raises after the handshake passes through.
        call_count = [0]

        class _FailAfterHandshake:
            """Wraps down_a: passes handshake, then fails on sendall."""

            def sendall(self, data):
                call_count[0] += 1
                if call_count[0] <= 1:
                    return down_a.sendall(data)
                raise OSError("broken pipe")

            def settimeout(self, t):
                down_a.settimeout(t)

            def fileno(self):
                return down_a.fileno()

            def recv(self, n):
                return down_a.recv(n)

            def close(self):
                down_a.close()

        wrapped_down = _FailAfterHandshake()
        p = _make_probe(
            headers={"Upgrade": "websocket", "Sec-WebSocket-Key": "k"},
            connection=wrapped_down,
        )

        def feed():
            up_a.sendall(handshake)
            time.sleep(0.05)
            up_a.sendall(b"frame data that will fail on dst sendall")
            time.sleep(0.1)
            up_a.close()

        feeder = threading.Thread(target=feed, daemon=True)
        feeder.start()

        with patch.object(server.socket, "create_connection", return_value=up_b):
            p._relay_ws()

        feeder.join(timeout=2)
        # Should exit cleanly via the sendall OSError path.
        assert p.errors == []
        down_a.close()
        down_b.close()

    def test_pump_recv_oserror(self):
        """If this fails: an OSError on recv inside the pump (e.g. connection
        reset) causes an unhandled exception instead of a clean exit."""
        handshake = b"HTTP/1.1 101 OK\r\nUpgrade: ws\r\n\r\n"

        up_a, up_b = socket.socketpair()
        down_a, down_b = socket.socketpair()

        p = _make_probe(
            headers={"Upgrade": "websocket", "Sec-WebSocket-Key": "k"},
            connection=down_a,
        )

        def feed_then_break():
            up_a.sendall(handshake)
            time.sleep(0.05)
            # RST the connection so recv on up_b raises or returns empty.
            up_a.setsockopt(
                socket.SOL_SOCKET, socket.SO_LINGER,
                b'\x01\x00\x00\x00\x00\x00\x00\x00'
            )
            up_a.close()

        feeder = threading.Thread(target=feed_then_break, daemon=True)
        feeder.start()

        with patch.object(server.socket, "create_connection", return_value=up_b):
            p._relay_ws()

        feeder.join(timeout=2)
        assert p.errors == []
        down_a.close()
        down_b.close()


class TestRelayWsPump:
    """The bidirectional pump forwards frames and exits on hangup."""

    def test_pump_forwards_and_exits_on_close(self):
        """If this fails: WebSocket frames are not forwarded between client and
        upstream, breaking live-reload (HMR)."""
        # Use real socket pairs to exercise the selectors pump.
        # up_a <-> up_b: the relay uses up_b as the upstream socket.
        # down_a <-> down_b: the relay uses down_a as self.connection (client).
        up_a, up_b = socket.socketpair()
        down_a, down_b = socket.socketpair()

        handshake = b"HTTP/1.1 101 OK\r\nUpgrade: websocket\r\n\r\n"

        p = _make_probe(
            headers={"Upgrade": "websocket", "Sec-WebSocket-Key": "k"},
            connection=down_a,
        )

        def feed_handshake_then_frame():
            # Feed the handshake, wait for pump to start, then send a frame.
            up_a.sendall(handshake)
            time.sleep(0.1)
            up_a.sendall(b"hello from upstream")
            time.sleep(0.1)
            up_a.close()

        feeder = threading.Thread(target=feed_handshake_then_frame, daemon=True)
        feeder.start()

        def run_relay():
            with patch.object(server.socket, "create_connection", return_value=up_b):
                p._relay_ws()

        relay_thread = threading.Thread(target=run_relay, daemon=True)
        relay_thread.start()

        # Read the handshake from the client side first.
        down_b.settimeout(2)
        handshake_received = b""
        while b"\r\n\r\n" not in handshake_received:
            handshake_received += down_b.recv(4096)

        # Now read the forwarded frame.
        frame_data = down_b.recv(4096)
        assert frame_data == b"hello from upstream"

        # Send a frame from the "client" side back upstream.
        down_b.sendall(b"hello from client")
        # The relay forwards it to up_b, which comes out on up_a — but up_a
        # may already be closed by the feeder. The point is the relay exits
        # cleanly when up_a closes.
        relay_thread.join(timeout=3)
        feeder.join(timeout=1)
        assert not relay_thread.is_alive()

        # Cleanup.
        down_a.close()
        down_b.close()


class TestRelayWsIdleCap:
    """The pump exits after _WS_IDLE seconds of inactivity."""

    def test_idle_timeout_exits(self):
        """If this fails: an abandoned WebSocket connection holds a handler thread
        and file descriptor forever."""
        handshake = b"HTTP/1.1 101 OK\r\nUpgrade: ws\r\n\r\n"

        up_a, up_b = socket.socketpair()
        down_a, down_b = socket.socketpair()

        # Feed the handshake into the upstream side, then go quiet.
        def feed():
            up_a.sendall(handshake)

        feeder = threading.Thread(target=feed, daemon=True)
        feeder.start()

        p = _make_probe(
            headers={"Upgrade": "websocket", "Sec-WebSocket-Key": "k"},
            connection=down_a,
        )

        # Patch _WS_IDLE to a tiny value so select() times out immediately.
        with (
            patch.object(server.socket, "create_connection", return_value=up_b),
            patch.object(server, "_WS_IDLE", 0.01),
        ):
            p._relay_ws()

        feeder.join(timeout=1)
        # Should exit cleanly (timeout path) without errors.
        assert p.errors == []
        assert p.close_connection is True

        up_a.close()
        up_b.close()
        down_a.close()
        down_b.close()


class TestRelayWsRestAfterHandshake:
    """Frames received with the handshake are forwarded immediately."""

    def test_trailing_frames_forwarded(self):
        """If this fails: WebSocket frames bundled with the 101 response are lost,
        breaking the first HMR update after connection."""
        # Handshake + trailing frame data in same buffer.
        handshake_plus_frame = (
            b"HTTP/1.1 101 OK\r\nUpgrade: ws\r\n\r\n"
            b"\x81\x05hello"  # a small WS text frame
        )

        up_a, up_b = socket.socketpair()
        down_a, down_b = socket.socketpair()

        p = _make_probe(
            headers={"Upgrade": "websocket", "Sec-WebSocket-Key": "k"},
            connection=down_a,
        )

        def feed():
            # Send handshake + trailing frame, then close to end the pump.
            up_a.sendall(handshake_plus_frame)
            time.sleep(0.05)
            up_a.close()

        feeder = threading.Thread(target=feed, daemon=True)
        feeder.start()

        with patch.object(server.socket, "create_connection", return_value=up_b):
            p._relay_ws()

        feeder.join(timeout=2)

        # Read everything the client side received.
        down_a.close()
        all_received = b""
        while True:
            chunk = down_b.recv(4096)
            if not chunk:
                break
            all_received += chunk
        down_b.close()

        # The trailing frame data should have been forwarded.
        assert b"\x81\x05hello" in all_received


# ===========================================================================
# _start_inject_proxy tests
# ===========================================================================


class TestStartInjectProxy:
    """The proxy startup helper binds and returns the correct URL."""

    def test_starts_and_returns_url(self):
        """If this fails: the overlay-injecting proxy fails to start, leaving the
        preview without the select-to-edit overlay."""
        srv, url = server._start_inject_proxy("http://127.0.0.1:3000")
        try:
            assert srv is not None
            assert url.startswith("http://127.0.0.1:")
            assert url.endswith("/")
            # Verify it actually bound a port.
            port = int(url.split(":")[2].rstrip("/"))
            assert port > 0
        finally:
            if srv:
                srv.shutdown()

    def test_invalid_port_returns_none(self):
        """If this fails: a malformed persisted dev-server URL causes an unhandled
        ValueError, crashing the projects page with a 500."""
        srv, url = server._start_inject_proxy("http://127.0.0.1:not-a-port/")
        assert srv is None
        assert url == ""

    def test_proxy_port_stamped_on_handler(self):
        """If this fails: _keep_redirect_local uses port 0, so redirect rewriting
        produces invalid URLs that break navigation."""
        srv, url = server._start_inject_proxy("http://127.0.0.1:4000")
        try:
            port = int(url.split(":")[2].rstrip("/"))
            # The handler class should have the port stamped.
            handler_cls = srv.RequestHandlerClass
            assert handler_cls.proxy_port == port
        finally:
            if srv:
                srv.shutdown()

    def test_upstream_host_and_port_propagated(self):
        """If this fails: the proxy relays to the wrong upstream, sending requests
        to a different dev server or port."""
        srv, url = server._start_inject_proxy("http://127.0.0.1:9999")
        try:
            handler_cls = srv.RequestHandlerClass
            assert handler_cls.upstream_host == "127.0.0.1"
            assert handler_cls.upstream_port == 9999
        finally:
            if srv:
                srv.shutdown()

    def test_bind_failure_returns_none(self):
        """If this fails: a socket bind failure (port conflict) causes an unhandled
        exception, crashing the caller instead of returning a safe fallback."""
        with patch.object(
            server, "ThreadingHTTPServer", side_effect=OSError("addr in use")
        ):
            srv, url = server._start_inject_proxy("http://127.0.0.1:3000")
        assert srv is None
        assert url == ""

    def test_default_port_for_https(self):
        """If this fails: an HTTPS dev URL without an explicit port uses port 0
        instead of 443, relaying to the wrong upstream."""
        srv, url = server._start_inject_proxy("https://127.0.0.1/")
        try:
            assert srv is not None
            handler_cls = srv.RequestHandlerClass
            assert handler_cls.upstream_port == 443
        finally:
            if srv:
                srv.shutdown()
