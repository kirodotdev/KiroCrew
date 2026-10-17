"""Regression tests for the Slack web client's retry policy (lane 51 / D77).

A chat.postMessage whose connection drops AFTER Slack has read it must not be
retried, or Slack posts the message twice. Only a connection that never reached
Slack (``ClientConnectorError``, a pre-send TCP-setup failure) is retried, once.

Every fake server here binds to 127.0.0.1 only and never calls Slack; while a
post runs, name lookups for any host other than loopback are refused.
"""

from __future__ import annotations

import asyncio
import socket
import struct
import threading
from unittest import mock

import aiohttp
import pytest
from slack_sdk.http_retry.state import RetryState
from slack_sdk.web import WebClient
from slack_sdk.web.async_client import AsyncWebClient

from kiro_crew.slack.client import RealSlackClient

_POST_LINE = "POST /api/chat.postMessage "
_OK_BODY = b'{"ok": true, "ts": "1700000000.000100", "channel": "C0LANE51"}'


class _FakeSlack:
    """Loopback HTTP endpoint. Records each request, then acts per ``mode``.

    - ``disconnect``: close with no response (FIN) once the request is read.
    - ``reset``: close with SO_LINGER 0 so the close sends RST.
    - ``ok``: write ``200 OK`` with a valid chat.postMessage JSON body.
    """

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.requests: list[tuple[str, bytes]] = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with conn:
                conn.settimeout(5)
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                head, _, body = data.partition(b"\r\n\r\n")
                length = 0
                for line in head.split(b"\r\n")[1:]:
                    name, _, value = line.partition(b":")
                    if name.strip().lower() == b"content-length":
                        length = int(value.strip())
                while len(body) < length:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    body += chunk
                self.requests.append((head.split(b"\r\n", 1)[0].decode("latin-1"), body))
                if self.mode == "ok":
                    conn.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                        b"Content-Length: " + str(len(_OK_BODY)).encode() + b"\r\n\r\n" + _OK_BODY
                    )
                elif self.mode == "reset":
                    conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                # "disconnect": leaving the block closes with no response.

    @property
    def posts(self) -> list[bytes]:
        return [body for line, body in self.requests if line.startswith(_POST_LINE)]

    def close(self) -> None:
        self._stop.set()
        self._sock.close()
        self._thread.join(timeout=2)


def _send_one(server: _FakeSlack):
    """Send ONE post through RealSlackClient at the fake; return (posts, error)."""
    real_getaddrinfo = socket.getaddrinfo

    def loopback_only(host, *args, **kwargs):
        if host not in ("127.0.0.1", "localhost"):
            raise OSError(f"test guard: refused name lookup for {host!r} (loopback only)")
        return real_getaddrinfo(host, *args, **kwargs)

    client = RealSlackClient("xoxb-not-a-real-token")
    client._web.base_url = f"http://127.0.0.1:{server.port}/api/"
    assert client._web.base_url.startswith("http://127.0.0.1:")
    error: BaseException | None = None
    ts: str | None = None
    with mock.patch.object(socket, "getaddrinfo", loopback_only):
        try:
            ts = asyncio.run(client.post_message(channel="C0LANE51", text="one send"))
        except Exception as exc:
            error = exc
    return server.posts, error, ts


@pytest.mark.parametrize("mode", ["disconnect", "reset"])
def test_a_post_whose_connection_drops_after_delivery_is_not_resent(mode):
    server = _FakeSlack(mode)
    try:
        posts, error, _ = _send_one(server)
    finally:
        server.close()
    assert len(posts) == 1, (
        f"{len(posts)} chat.postMessage requests reached the server for one send "
        f"(connection {mode} after the request was read; the send then raised "
        f"{type(error).__name__}). The client resent a request Slack may already have "
        f"delivered."
    )
    assert error is not None  # the single attempt still surfaces the drop


def test_a_normal_post_is_sent_once_and_returns_the_ts():
    server = _FakeSlack("ok")
    try:
        posts, error, ts = _send_one(server)
    finally:
        server.close()
    assert len(posts) == 1
    assert error is None
    assert ts == "1700000000.000100"


def _a_real_connector_error() -> aiohttp.ClientConnectorError:
    """Produce a genuine ClientConnectorError by connecting to a refused port."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()  # nothing listens here now -> connection refused

    async def _go() -> aiohttp.ClientConnectorError:
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(f"http://127.0.0.1:{dead_port}/"):
                    pass
            except aiohttp.ClientConnectorError as exc:
                return exc
        raise AssertionError("expected a ClientConnectorError from the refused connect")

    return asyncio.run(_go())


def test_a_connection_that_never_reached_slack_is_retried_once():
    from kiro_crew.slack.client import _ConnectSetupRetryHandler

    handler = _ConnectSetupRetryHandler(max_retry_count=1)
    connector_error = _a_real_connector_error()

    async def _can(error, attempt):
        return await handler.can_retry_async(
            state=RetryState(current_attempt=attempt), request=None, response=None, error=error
        )

    # Pre-send connect failure: retried on the first attempt, not past the cap.
    assert asyncio.run(_can(connector_error, 0)) is True
    assert asyncio.run(_can(connector_error, 1)) is False
    # Errors that can arrive after Slack read the request: never retried.
    assert asyncio.run(_can(aiohttp.ServerDisconnectedError(), 0)) is False
    assert asyncio.run(_can(aiohttp.ClientOSError(104, "Connection reset by peer"), 0)) is False
    assert asyncio.run(_can(aiohttp.ServerConnectionError(), 0)) is False


def test_the_sync_and_socket_mode_clients_keep_the_slack_sdk_defaults():
    """The fix is scoped to RealSlackClient; untouched clients keep the defaults.

    cli_setup/enterprise build a sync ``WebClient``; events builds the Socket
    Mode ``AsyncWebClient``. Neither passes retry_handlers, so each keeps
    slack_sdk's default handler, which this test pins.
    """
    real = RealSlackClient("xoxb-not-a-real-token")
    assert [type(h).__name__ for h in real._web.retry_handlers] == ["_ConnectSetupRetryHandler"]

    assert [type(h).__name__ for h in WebClient(token="xoxb-x").retry_handlers] == [
        "ConnectionErrorRetryHandler"
    ]
    assert [type(h).__name__ for h in AsyncWebClient(token="xoxb-x").retry_handlers] == [
        "AsyncConnectionErrorRetryHandler"
    ]


def _rename_the_private_retry_hook(monkeypatch) -> None:
    """Make the installed slack_sdk behave like a release that renamed ``_can_retry_async``.

    The base ``can_retry_async`` asks a hook under a new name, and that hook raises
    ``NotImplementedError`` in the base, as ``_can_retry_async`` does in 3.45.0.
    """
    from slack_sdk.http_retry.async_handler import AsyncRetryHandler

    async def can_retry_async(self, *, state, request, response=None, error=None):
        if state.current_attempt >= self.max_retry_count:
            return False
        return await self._should_retry_async(
            state=state, request=request, response=response, error=error
        )

    async def _should_retry_async(self, *, state, request, response=None, error=None):
        raise NotImplementedError()

    monkeypatch.delattr(AsyncRetryHandler, "_can_retry_async")
    monkeypatch.setattr(
        AsyncRetryHandler, "_should_retry_async", _should_retry_async, raising=False
    )
    monkeypatch.setattr(AsyncRetryHandler, "can_retry_async", can_retry_async)


def test_a_renamed_private_retry_hook_keeps_posts_working(monkeypatch):
    """A slack_sdk release that renames the private hook must not break every call."""
    _rename_the_private_retry_hook(monkeypatch)
    server = _FakeSlack("ok")
    try:
        posts, error, ts = _send_one(server)
    finally:
        server.close()
    assert error is None, f"a normal post raised {type(error).__name__}: {error!r}"
    assert (len(posts), ts) == (1, "1700000000.000100")


@pytest.mark.parametrize("mode", ["disconnect", "reset"])
def test_a_renamed_private_retry_hook_still_never_resends_a_dropped_post(monkeypatch, mode):
    _rename_the_private_retry_hook(monkeypatch)
    server = _FakeSlack(mode)
    try:
        posts, error, _ = _send_one(server)
    finally:
        server.close()
    assert len(posts) == 1, f"{len(posts)} chat.postMessage requests for one send"
    assert isinstance(error, aiohttp.ClientError), f"the drop surfaced as {error!r}"
