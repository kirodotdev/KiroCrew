"""Aborted guest Git transfers retire their gateway request and upstream response."""

from __future__ import annotations

import asyncio
import base64
import io
import json
from email.message import Message
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from test_remote_git_proxy import FrameLink

from kiro_crew.remote_session import git_gateway, git_proxy
from kiro_crew.testing.wait import async_wait_until


def _guest_handler(*, broken=False, upload=False):
    output = io.StringIO()

    class Gateway(git_proxy.Gateway):
        def send(self, frame):
            super().send(frame)
            if frame["t"] == "end":
                for response in (
                    {"t": "resp", "status": 200, "headers": {}},
                    {"t": "body", "b": base64.b64encode(b"pack").decode()},
                    {"t": "end"},
                ):
                    self._queues[frame["id"]].put_nowait(response)

    class Writer(io.BytesIO):
        def write(self, data):
            if broken:
                raise BrokenPipeError("git client disconnected")
            return super().write(data)

    handler = object.__new__(git_proxy.Handler)
    handler.gateway = Gateway(out=output)
    handler.secret = "a" * 64
    handler.server = SimpleNamespace(server_address=("127.0.0.1", 12345))
    handler.headers = Message()
    handler.headers["Host"] = "127.0.0.1:12345"
    handler.headers["Authorization"] = "Bearer " + handler.secret
    handler.path = "/github.com/a/r/info/refs?service=git-upload-pack"
    handler.command = "POST" if upload else "GET"
    handler.send_response = Mock()
    handler.send_header = Mock()
    handler.end_headers = Mock()
    handler.wfile = Writer()
    handler.close_connection = False
    if upload:
        handler.headers["Content-Length"] = "4"
        handler.rfile = Mock()
        handler.rfile.read.side_effect = ConnectionResetError("upload interrupted")
    return handler, output


@pytest.mark.parametrize("phase", ["download", "upload", "complete"])
def test_guest_sends_cancel_only_when_delivery_is_incomplete(phase):
    handler, output = _guest_handler(broken=phase == "download", upload=phase == "upload")
    handler._proxy()
    frames = [json.loads(line) for line in output.getvalue().splitlines()]
    assert frames[0]["t"] == "req"
    if phase == "complete":
        assert [frame["t"] for frame in frames] == ["req", "end"]
        assert handler.wfile.getvalue().endswith(b"0\r\n\r\n")
    else:
        assert frames[-1] == {"t": "cancel", "id": frames[0]["id"]}
        assert handler.close_connection
    assert not handler.gateway._queues


@pytest.mark.asyncio
async def test_guest_disconnect_cancels_stream_and_reuses_the_live_request_slot(monkeypatch):
    link = FrameLink()
    entered, streaming, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release = asyncio.Event()
    chunks = []
    cancelled = []

    class Content:
        async def iter_chunked(self, size):
            chunks.append(b"pack")
            yield chunks[-1]
            streaming.set()
            await release.wait()
            chunks.append(b"unwanted remainder")
            yield chunks[-1]

    class Response:
        status = 200
        headers = {}
        content = Content()

        async def __aenter__(self):
            entered.set()
            return self

        async def __aexit__(self, kind, exc, tb):
            cancelled.append(kind)
            closed.set()

    upstream = Mock()
    upstream.request.return_value = Response()
    channel = git_gateway.GitProxyChannel(
        link,
        "dashboard:git",
        agent="researcher",
        credential_source=AsyncMock(return_value="Basic dTpw"),
        egress_permitted=lambda *args: True,
        http_session=upstream,
    )
    monkeypatch.setattr(git_gateway, "MAX_LIVE_REQUESTS", 1)
    serve = asyncio.create_task(channel.serve())
    path = "/github.com/a/r/info/refs?service=git-upload-pack"
    try:
        link.get(1, path)
        await asyncio.wait_for(entered.wait(), 30)
        await asyncio.wait_for(streaming.wait(), 30)
        link.get(2, path)
        await async_wait_until(lambda: any(f["id"] == 2 for f in link.frames))
        assert next(f for f in link.frames if f["id"] == 2)["status"] == 503
        guest, output = _guest_handler(broken=True)
        guest._proxy()
        cancel = json.loads(output.getvalue().splitlines()[-1])
        assert cancel == {"t": "cancel", "id": 1}
        link.guest(cancel)
        await asyncio.wait_for(closed.wait(), 30)
        await async_wait_until(lambda: not channel._tasks and not channel._requests)
        assert cancelled == [asyncio.CancelledError]
        assert chunks == [b"pack"]
        assert not any(f["id"] == 1 and f["t"] == "end" for f in link.frames)
        # Unknown or repeated cancellation must not retire a different request.
        streaming.clear()
        link.get(3, path)
        link.guest(cancel)
        link.guest({"t": "cancel", "id": 99})
        await asyncio.wait_for(streaming.wait(), 30)
        assert 3 in channel._tasks and not channel._tasks[3].cancelled()
        release.set()
        await async_wait_until(lambda: any(f["id"] == 3 and f["t"] == "end" for f in link.frames))
        assert next(f for f in link.frames if f["id"] == 3)["status"] == 200
    finally:
        release.set()
        link.stdout.feed_eof()
        await asyncio.wait_for(serve, 30)
    assert not channel._tasks and not channel._requests


@pytest.mark.asyncio
async def test_cancel_before_handler_starts_releases_request_id_and_admission():
    link = FrameLink()
    credential = AsyncMock(return_value="Basic dTpw")
    channel = git_gateway.GitProxyChannel(
        link,
        "dashboard:git",
        agent="researcher",
        credential_source=credential,
        egress_permitted=lambda *args: True,
    )
    # Both frames are already readable, so serve cancels before _handle is scheduled.
    link.get(1, "/github.com/a/r/info/refs?service=git-upload-pack")
    link.guest({"t": "cancel", "id": 1})
    serve = asyncio.create_task(channel.serve())
    try:
        await async_wait_until(lambda: link.stdout.at_eof() or not link.stdout._buffer)
        await async_wait_until(lambda: not channel._tasks and not channel._requests)
        credential.assert_not_awaited()
    finally:
        link.stdout.feed_eof()
        await asyncio.wait_for(serve, 30)


@pytest.mark.asyncio
async def test_cancel_retires_an_interrupted_fetch_upload_before_end():
    link = FrameLink()
    credential = AsyncMock(return_value="Basic dTpw")
    channel = git_gateway.GitProxyChannel(
        link,
        "dashboard:git",
        agent="researcher",
        credential_source=credential,
        egress_permitted=lambda *args: True,
    )
    serve = asyncio.create_task(channel.serve())
    try:
        link.guest(
            {"t": "req", "id": 1, "method": "POST", "path": "/github.com/a/r/git-upload-pack"}
        )
        link.guest({"t": "body", "id": 1, "b": "cGFjaw=="})
        await async_wait_until(lambda: credential.await_count == 1)
        link.guest({"t": "cancel", "id": 1})
        await async_wait_until(lambda: not channel._tasks and not channel._requests)
        assert not link.frames and channel._http is None
    finally:
        link.stdout.feed_eof()
        await asyncio.wait_for(serve, 30)
