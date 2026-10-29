"""Request-body credit (contract section 5.4): the client half of ``request-credit``.

Once the relay agrees ``request-credit`` in ``ready``, it sends each request body
only as far as the client has granted, starting from 128 KiB per stream. The client
gives credit back only for body bytes the Crew's web server has taken, so one large
upload waits for the Crew instead of filling the host connection ahead of every
other stream. When the relay does not agree it, nothing changes: the body comes
without waiting and the client sends no credit.

The fake relay here plays the relay's half from the fixtures: :class:`_CreditedUpload`
sends a body no further than its credit, the way the relay does once the feature is
agreed. :class:`_GatedIngress` stands in for the Crew's web server and takes an
upload's body only as the test allows, so the credit the client grants can be
checked against exactly what was taken.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections import defaultdict
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

import pytest
from relay_client_helpers import (
    OWNER,
    TOKEN,
    FakeCrew,
    FakeHostLeg,
    FakeRelay,
    TopRng,
    make_client,
    ready_frame,
    stop,
    valid_message,
)

from kiro_crew.relay_client import ingress as ingress_module
from kiro_crew.relay_client.client import RelayClient
from kiro_crew.relay_client.connection import RelayTimings
from kiro_crew.relay_client.ingress import RelayedRequest
from kiro_crew.relay_client.protocol import (
    BODY_CHUNK_RAW_BYTES,
    REQUEST_CREDIT_INITIAL_BYTES,
)
from kiro_crew.relay_client.streams import HttpStream
from kiro_crew.relay_client.token_source import HostCredential, StaticHostTokenSource
from kiro_crew.testing.wait import async_wait_until, await_until, until_parked

pytestmark = pytest.mark.asyncio

#: A body several times the starting credit.
UPLOAD = bytes(range(251)) * (4 * 1024 + 7)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _agreeing_ready() -> Dict[str, Any]:
    ready = ready_frame()
    ready["features"] = [*ready["features"], "request-credit"]
    return ready


def _upload_head(payload: bytes) -> Dict[str, Any]:
    return valid_message(
        "frames/request",
        1,
        path="/upload",
        user=OWNER,
        headers={
            "content-type": "application/octet-stream",
            "content-length": str(len(payload)),
        },
    )


class _Answer:
    """A complete response from the stand-in web server."""

    def __init__(self, body: Dict[str, Any]) -> None:
        self.status = 200
        self.headers = (("content-type", "application/json"),)
        self._body = json.dumps(body).encode("utf-8")

    async def iter_body(self, chunk_size: int) -> AsyncIterator[bytes]:
        yield self._body

    async def aclose(self) -> None:
        return None


class _GatedIngress:
    """The Crew's web server, taking an upload's body only as the test allows.

    Each :meth:`allow` lets the server ask for one more piece; :meth:`open` lets it
    read the rest freely. Asking for the next piece is what tells the client the
    previous one was taken, as the loopback hand-off does for a credited body once
    that piece has left the process. Any other path is answered at once.
    """

    def __init__(self) -> None:
        self.taken = 0
        self._gate = asyncio.Semaphore(0)
        self._open = False

    def allow(self, pieces: int = 1) -> None:
        for _ in range(pieces):
            self._gate.release()

    def open(self) -> None:
        self._open = True
        self._gate.release()

    async def parked(self) -> None:
        """Return once the server is waiting to be allowed its next piece."""
        await until_parked(self._gate)

    def bind_host(self, *, crew_kind: str, owner_user_id: str) -> None:
        return None

    def unbind_host(self) -> None:
        return None

    async def open_http(
        self, request: RelayedRequest, body: Optional[AsyncIterator[bytes]]
    ) -> _Answer:
        if request.path != "/upload":
            return _Answer({"ok": True})
        assert body is not None
        digest = hashlib.sha256()
        while True:
            if not self._open:
                await self._gate.acquire()
            try:
                piece = await body.__anext__()
            except StopAsyncIteration:
                break
            self.taken += len(piece)
            digest.update(piece)
        return _Answer({"bytes": self.taken, "sha256": digest.hexdigest()})

    async def open_websocket(self, request: Any) -> Any:
        raise AssertionError("no WebSocket is relayed here")

    async def aclose(self) -> None:
        return None


class _Demux:
    """Sorts what the client sends to the fake relay by stream, grants apart.

    ``on_grant`` runs as each request-body grant arrives, so a test can hold every
    grant to what the Crew's server had taken by then.
    """

    def __init__(self, leg: FakeHostLeg) -> None:
        self.leg = leg
        self.frames: Dict[Any, asyncio.Queue] = defaultdict(asyncio.Queue)
        self.credit: Dict[int, asyncio.Queue] = defaultdict(asyncio.Queue)
        self.grants: Dict[int, List[int]] = defaultdict(list)
        self.pongs: asyncio.Queue = asyncio.Queue()
        self.on_grant: Callable[[int], None] = lambda sid: None
        self.problems: List[str] = []
        self._task: Optional[asyncio.Task[None]] = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self) -> None:
        while True:
            frame = await self.leg.inbox.get()
            kind = frame.get("type")
            if kind == "pong":
                self.pongs.put_nowait(frame)
            elif kind == "flow-credit":
                sid = frame["id"]
                if not 1 <= frame["credit"] <= REQUEST_CREDIT_INITIAL_BYTES:
                    self.problems.append(f"grant out of range: {frame}")
                self.grants[sid].append(frame["credit"])
                try:
                    self.on_grant(sid)
                except AssertionError as exc:
                    self.problems.append(str(exc))
                self.credit[sid].put_nowait(frame["credit"])
            elif kind != "ping":
                self.frames[frame.get("id")].put_nowait(frame)

    async def barrier(self, nonce: str) -> None:
        """Return once the client has answered a ping sent now, so every frame it
        sent before that answer has already been sorted."""
        await self.leg.send(valid_message("frames/ping", nonce=nonce))
        while True:
            pong = await await_until(asyncio.ensure_future(self.pongs.get()), what="a pong")
            if pong.get("nonce") == nonce:
                return

    async def next_frame(self, sid: int) -> Dict[str, Any]:
        return await await_until(
            asyncio.ensure_future(self.frames[sid].get()), what=f"a frame for stream {sid}"
        )

    async def read_response(self, sid: int) -> tuple[Dict[str, Any], bytes]:
        head = await self.next_frame(sid)
        assert head["type"] == "response", head
        body = b""
        while True:
            frame = await self.next_frame(sid)
            if frame["type"] == "response-end":
                return head, body
            assert frame["type"] == "response-chunk", frame
            body += base64.b64decode(frame["data"])


class _CreditedUpload:
    """The relay's half: send a body no further than the stream's credit."""

    def __init__(self, leg: FakeHostLeg, demux: _Demux, sid: int, payload: bytes) -> None:
        self._leg = leg
        self._demux = demux
        self._sid = sid
        self._payload = payload
        self.credit = REQUEST_CREDIT_INITIAL_BYTES
        self.sent = 0
        #: Set while every byte of credit is spent and more of the body is left.
        self.starved = asyncio.Event()

    async def run(self) -> None:
        grants = self._demux.credit[self._sid]
        while self.sent < len(self._payload):
            while self.credit == 0:
                self.starved.set()
                self.credit += await grants.get()
                self.starved.clear()
            size = min(BODY_CHUNK_RAW_BYTES, self.credit, len(self._payload) - self.sent)
            piece = self._payload[self.sent : self.sent + size]
            await self._leg.send(
                valid_message("frames/request-chunk", id=self._sid, data=_b64(piece))
            )
            self.credit -= size
            self.sent += size
        await self._leg.send(valid_message("frames/request-end", id=self._sid))


def _gated_client(relay: FakeRelay, ingress: _GatedIngress) -> RelayClient:
    source = StaticHostTokenSource(HostCredential(TOKEN, crew_kind="personal", owner_user_id=OWNER))
    return RelayClient(
        host_url=relay.url,
        crew_url_for=lambda ep: f"https://{ep}.kiro-relay.us-east-1.on.aws",
        token_source=source,
        ingress=ingress,
        client_version="0.0.0-test",
        # No keepalive ping inside a test, so the only waiter on the send lock is
        # the frame a test is watching.
        timings=RelayTimings(ping_interval=3600.0, silence=7200.0),
        rng=TopRng(),
    )


async def _stream(client: RelayClient, sid: int) -> HttpStream:
    """The client's stream *sid*, once its connection is ready and holds it."""

    def find() -> Optional[HttpStream]:
        for conn in client._live:
            stream = conn._streams.get(sid) if conn.ready.is_set() else None
            if isinstance(stream, HttpStream):
                return stream
        return None

    return await async_wait_until(find, describe=lambda: [c.ready.is_set() for c in client._live])


@pytest.fixture
def held_peak(monkeypatch) -> List[int]:
    """Every request-body byte count a stream holds right after taking a chunk."""
    peaks: List[int] = []
    original = HttpStream.feed

    def feed(self: HttpStream, data: bytes) -> None:
        original(self, data)
        peaks.append(self._buffered)

    monkeypatch.setattr(HttpStream, "feed", feed)
    return peaks


async def test_a_large_upload_waits_for_credit_without_stalling_other_streams(held_peak) -> None:
    ingress = _GatedIngress()
    async with FakeRelay() as relay:
        client = _gated_client(relay, ingress)
        await client.start()
        leg = await relay.next_leg()
        demux = _Demux(leg)
        try:
            hello = await leg.recv_type("hello")
            assert "request-credit" in hello["features"]
            await leg.send(_agreeing_ready())
            demux.start()

            def within_what_was_taken(sid: int) -> None:
                assert sum(demux.grants[sid]) <= ingress.taken, (demux.grants[sid], ingress.taken)

            demux.on_grant = within_what_was_taken
            head = _upload_head(UPLOAD)
            sid = head["id"]
            await leg.send(head)
            upload = _CreditedUpload(leg, demux, sid, UPLOAD)
            uploading = asyncio.ensure_future(upload.run())

            # The relay spends its starting credit and then waits on the client.
            await await_until(upload.starved, what="the upload to spend its credit")
            assert upload.sent == REQUEST_CREDIT_INITIAL_BYTES
            stream = await _stream(client, sid)
            await async_wait_until(
                lambda: stream._received == REQUEST_CREDIT_INITIAL_BYTES,
                describe=lambda: stream._received,
            )
            await demux.barrier("before-the-crew-reads")
            assert demux.grants[sid] == []

            # Another stream on the same connection goes through while it waits.
            other = valid_message("frames/request")
            await leg.send(other)
            await leg.send(valid_message("frames/request-end", id=other["id"]))
            response, body = await demux.read_response(other["id"])
            assert response["status"] == 200 and json.loads(body) == {"ok": True}
            assert upload.starved.is_set() and upload.sent == REQUEST_CREDIT_INITIAL_BYTES

            ingress.open()
            await await_until(uploading, what="the upload to finish")
            response, body = await demux.read_response(sid)
            assert response["status"] == 200
            assert json.loads(body) == {
                "bytes": len(UPLOAD),
                "sha256": hashlib.sha256(UPLOAD).hexdigest(),
            }
            assert demux.problems == []
            assert sum(demux.grants[sid]) <= len(UPLOAD)
            assert max(held_peak) <= REQUEST_CREDIT_INITIAL_BYTES
        finally:
            await demux.aclose()
            await stop(client, ingress)


async def test_credit_is_never_granted_past_what_the_crews_server_took() -> None:
    ingress = _GatedIngress()
    async with FakeRelay() as relay:
        client = _gated_client(relay, ingress)
        await client.start()
        leg = await relay.next_leg()
        demux = _Demux(leg)
        try:
            await leg.handshake(_agreeing_ready())
            demux.start()
            head = _upload_head(UPLOAD)
            sid = head["id"]
            await leg.send(head)
            upload = _CreditedUpload(leg, demux, sid, UPLOAD)
            uploading = asyncio.ensure_future(upload.run())
            await await_until(upload.starved, what="the upload to spend its credit")
            stream = await _stream(client, sid)
            await async_wait_until(lambda: stream._received == REQUEST_CREDIT_INITIAL_BYTES)

            # The server takes one piece. It has not asked for the next, so that
            # piece may still be in flight to it: nothing is given back yet.
            ingress.allow()
            await ingress.parked()
            assert ingress.taken == BODY_CHUNK_RAW_BYTES
            await demux.barrier("one-piece-taken")
            assert demux.grants[sid] == []

            # Asking for the second piece gives back the first, and no more.
            ingress.allow()
            await ingress.parked()
            assert ingress.taken == 2 * BODY_CHUNK_RAW_BYTES
            await demux.barrier("two-pieces-taken")
            assert demux.grants[sid] == [BODY_CHUNK_RAW_BYTES]

            # The relay sends exactly what that grant allowed, then waits again.
            await async_wait_until(
                lambda: upload.starved.is_set()
                and upload.sent == REQUEST_CREDIT_INITIAL_BYTES + BODY_CHUNK_RAW_BYTES,
                describe=lambda: upload.sent,
            )

            # Step through the rest; each grant is checked as it arrives.
            def within_what_was_taken(_sid: int) -> None:
                assert sum(demux.grants[sid]) <= ingress.taken, (demux.grants[sid], ingress.taken)

            demux.on_grant = within_what_was_taken
            for step in range(6):
                ingress.allow()
                await ingress.parked()
                await demux.barrier(f"step-{step}")
                assert sum(demux.grants[sid]) <= ingress.taken
            ingress.open()
            await await_until(uploading, what="the upload to finish")
            response, body = await demux.read_response(sid)
            assert json.loads(body)["bytes"] == len(UPLOAD)
            assert demux.problems == []
        finally:
            await demux.aclose()
            await stop(client, ingress)


async def test_without_request_credit_agreed_the_body_flows_as_before(held_peak) -> None:
    ingress = _GatedIngress()
    async with FakeRelay() as relay:
        client = _gated_client(relay, ingress)
        await client.start()
        leg = await relay.next_leg()
        demux = _Demux(leg)
        try:
            hello = await leg.recv_type("hello")
            assert "request-credit" in hello["features"]
            ready = ready_frame()
            assert "request-credit" not in ready["features"]
            await leg.send(ready)
            demux.start()
            head = _upload_head(UPLOAD)
            sid = head["id"]
            await leg.send(head)
            # No credit to wait for: the whole body goes at once, read or not.
            for start in range(0, len(UPLOAD), BODY_CHUNK_RAW_BYTES):
                piece = UPLOAD[start : start + BODY_CHUNK_RAW_BYTES]
                await leg.send(valid_message("frames/request-chunk", id=sid, data=_b64(piece)))
            stream = await _stream(client, sid)
            await async_wait_until(
                lambda: stream._received == len(UPLOAD), describe=lambda: stream._received
            )
            assert stream._request_granted is None
            assert max(held_peak) > REQUEST_CREDIT_INITIAL_BYTES

            await leg.send(valid_message("frames/request-end", id=sid))
            ingress.open()
            response, body = await demux.read_response(sid)
            assert json.loads(body)["bytes"] == len(UPLOAD)
            await demux.barrier("after-the-response")
            assert dict(demux.grants) == {}
        finally:
            await demux.aclose()
            await stop(client, ingress)


async def test_a_relay_that_overruns_the_credit_ends_only_that_stream() -> None:
    ingress = _GatedIngress()
    async with FakeRelay() as relay:
        client = _gated_client(relay, ingress)
        await client.start()
        leg = await relay.next_leg()
        demux = _Demux(leg)
        try:
            await leg.handshake(_agreeing_ready())
            demux.start()
            head = _upload_head(UPLOAD)
            sid = head["id"]
            await leg.send(head)
            sent = 0
            while sent < REQUEST_CREDIT_INITIAL_BYTES:
                size = min(BODY_CHUNK_RAW_BYTES, REQUEST_CREDIT_INITIAL_BYTES - sent)
                piece = UPLOAD[sent : sent + size]
                await leg.send(valid_message("frames/request-chunk", id=sid, data=_b64(piece)))
                sent += size
            # One byte the client never granted.
            await leg.send(
                valid_message("frames/request-chunk", id=sid, data=_b64(UPLOAD[sent : sent + 1]))
            )
            error = await demux.next_frame(sid)
            assert error["type"] == "error" and error["code"] == "invalid_message", error
            assert "credit" in error["message"]

            other = valid_message("frames/request")
            await leg.send(other)
            await leg.send(valid_message("frames/request-end", id=other["id"]))
            response, _ = await demux.read_response(other["id"])
            assert response["status"] == 200
        finally:
            await demux.aclose()
            await stop(client, ingress)


async def test_no_credit_follows_a_body_the_relay_has_finished() -> None:
    ingress = _GatedIngress()
    payload = UPLOAD[: 100 * 1024]
    async with FakeRelay() as relay:
        client = _gated_client(relay, ingress)
        await client.start()
        leg = await relay.next_leg()
        demux = _Demux(leg)
        try:
            await leg.handshake(_agreeing_ready())
            demux.start()
            head = _upload_head(payload)
            sid = head["id"]
            await leg.send(head)
            for start in range(0, len(payload), BODY_CHUNK_RAW_BYTES):
                piece = payload[start : start + BODY_CHUNK_RAW_BYTES]
                await leg.send(valid_message("frames/request-chunk", id=sid, data=_b64(piece)))
            await leg.send(valid_message("frames/request-end", id=sid))
            stream = await _stream(client, sid)
            await async_wait_until(lambda: stream._body_ended)

            ingress.open()
            response, body = await demux.read_response(sid)
            assert json.loads(body)["bytes"] == len(payload)
            await demux.barrier("after-the-response")
            assert demux.grants[sid] == []
        finally:
            await demux.aclose()
            await stop(client, ingress)


async def test_credited_uploads_reach_the_crew_through_the_relay_ingress(held_peak) -> None:
    async with FakeRelay() as relay, FakeCrew() as crew:
        client, _, ingress = make_client(relay, crew)
        await client.start()
        leg = await relay.next_leg()
        demux = _Demux(leg)
        try:
            await leg.handshake(_agreeing_ready())
            demux.start()
            head = _upload_head(UPLOAD)
            sid = head["id"]
            upload = _CreditedUpload(leg, demux, sid, UPLOAD)

            def within_what_was_sent(_sid: int) -> None:
                assert sum(demux.grants[sid]) <= upload.sent, (demux.grants[sid], upload.sent)

            demux.on_grant = within_what_was_sent
            await leg.send(head)
            uploading = asyncio.ensure_future(upload.run())
            other = valid_message("frames/request")
            await leg.send(other)
            await leg.send(valid_message("frames/request-end", id=other["id"]))

            response, _ = await demux.read_response(other["id"])
            assert response["status"] == 200
            await await_until(uploading, what="the upload to finish")
            response, body = await demux.read_response(sid)
            assert response["status"] == 200
            answer = json.loads(body)
            assert answer["bytes"] == len(UPLOAD)
            assert answer["head"] == UPLOAD[:16].decode("latin-1")
            # The real relay ingress stamped both hand-offs with the caller.
            callers = {seen["path"]: seen["caller"] for seen in crew.seen}
            assert callers["/upload"] is not None and callers["/api/sessions"] is not None
            assert demux.grants[sid], "a body past the starting credit needs grants"
            assert demux.problems == []
            assert max(held_peak) <= REQUEST_CREDIT_INITIAL_BYTES
        finally:
            await demux.aclose()
            await stop(client, ingress)


class _HeldTransport:
    """A connection's write buffer that empties only when the test flushes it."""

    def __init__(self) -> None:
        self.buffered = 0
        self.limits: Optional[tuple] = None
        self.closing = False

    def set_write_buffer_limits(
        self, high: Optional[int] = None, low: Optional[int] = None
    ) -> None:
        self.limits = (high, low)

    def get_write_buffer_size(self) -> int:
        return self.buffered

    def is_closing(self) -> bool:
        return self.closing


class _HeldWriter:
    """aiohttp's stream writer as the payload sees it, over a :class:`_HeldTransport`."""

    def __init__(self) -> None:
        self.transport = _HeldTransport()
        self.written: List[bytes] = []
        self.flush = asyncio.Event()

    async def write(self, chunk: bytes) -> None:
        self.written.append(bytes(chunk))
        self.transport.buffered += len(chunk)

    async def drain(self) -> None:
        await self.flush.wait()
        self.flush.clear()
        self.transport.buffered = 0


async def test_a_credited_body_asks_for_more_only_once_the_buffer_is_empty() -> None:
    asked: List[int] = []

    async def source() -> AsyncIterator[bytes]:
        for index in range(3):
            asked.append(index)
            yield bytes([index]) * BODY_CHUNK_RAW_BYTES

    payload = ingress_module._FlushedBody(source())
    writer = _HeldWriter()
    writing = asyncio.ensure_future(payload.write(writer))  # type: ignore[arg-type]
    try:
        # The first piece sits in the write buffer, so the second is not asked for:
        # asking is what would give its credit back.
        await until_parked(writer.flush)
        assert asked == [0] and len(writer.written) == 1
        assert writer.transport.limits == (0, None)

        writer.flush.set()
        await async_wait_until(lambda: len(writer.written) == 2, describe=lambda: asked)
        await until_parked(writer.flush)
        assert asked == [0, 1]

        writer.flush.set()
        await until_parked(writer.flush)
        writer.flush.set()
        await await_until(writing, what="the body to be written")
        assert writer.written == [bytes([i]) * BODY_CHUNK_RAW_BYTES for i in range(3)]
    finally:
        writing.cancel()
        await asyncio.gather(writing, return_exceptions=True)


@pytest.mark.parametrize("failure", ["closing_before", "closing_during_drain", "still_full"])
async def test_a_credited_body_fails_closed_when_the_buffer_cannot_empty(failure) -> None:
    asked: List[int] = []

    async def source() -> AsyncIterator[bytes]:
        for index in range(3):
            asked.append(index)
            yield bytes([index]) * BODY_CHUNK_RAW_BYTES

    class Writer(_HeldWriter):
        async def write(self, chunk: bytes) -> None:
            await super().write(chunk)
            if failure == "closing_before":
                self.transport.closing = True

        async def drain(self) -> None:
            if failure == "closing_during_drain":
                # Emptied, but by a connection going away.
                self.transport.closing = True
                self.transport.buffered = 0
            # "still_full": the drain returns with the bytes still buffered.

    payload = ingress_module._FlushedBody(source())
    writer = Writer()
    with pytest.raises(ConnectionResetError):
        await await_until(
            asyncio.ensure_future(payload.write(writer)),  # type: ignore[arg-type]
            what="the write to fail",
        )
    # The second piece was never asked for, so no credit went back for the first.
    assert asked == [0] and len(writer.written) == 1


@pytest.mark.parametrize("agreed", [True, False])
async def test_only_a_credited_body_is_paced_by_the_loopback_hand_off(monkeypatch, agreed) -> None:
    built: List[Any] = []
    original = ingress_module._FlushedBody

    class Recording(original):  # type: ignore[misc, valid-type]
        def __init__(self, body: AsyncIterator[bytes]) -> None:
            built.append(self)
            super().__init__(body)

    monkeypatch.setattr(ingress_module, "_FlushedBody", Recording)
    payload = UPLOAD[: 3 * BODY_CHUNK_RAW_BYTES]
    async with FakeRelay() as relay, FakeCrew() as crew:
        client, _, ingress = make_client(relay, crew)
        await client.start()
        leg = await relay.next_leg()
        demux = _Demux(leg)
        try:
            await leg.handshake(_agreeing_ready() if agreed else ready_frame())
            demux.start()
            head = _upload_head(payload)
            await leg.send(head)
            for start in range(0, len(payload), BODY_CHUNK_RAW_BYTES):
                piece = payload[start : start + BODY_CHUNK_RAW_BYTES]
                await leg.send(
                    valid_message("frames/request-chunk", id=head["id"], data=_b64(piece))
                )
            await leg.send(valid_message("frames/request-end", id=head["id"]))
            response, body = await demux.read_response(head["id"])
            assert json.loads(body)["bytes"] == len(payload)
            assert len(built) == (1 if agreed else 0)
        finally:
            await demux.aclose()
            await stop(client, ingress)


async def test_a_send_is_skipped_when_its_stream_ended_while_it_waited_for_the_lock() -> None:
    ingress = _GatedIngress()
    async with FakeRelay() as relay:
        client = _gated_client(relay, ingress)
        await client.start()
        leg = await relay.next_leg()
        demux = _Demux(leg)
        try:
            await leg.handshake(_agreeing_ready())
            demux.start()
            await async_wait_until(lambda: any(c.ready.is_set() for c in client._live))
            (conn,) = [c for c in client._live if c.ready.is_set()]
            ended = [False]
            await conn._send_lock.acquire()
            try:
                sending = asyncio.ensure_future(
                    conn.send({"type": "flow-credit", "id": 8, "credit": 1}, lambda: ended[0])
                )
                await until_parked(conn._send_lock)
                ended[0] = True
            finally:
                conn._send_lock.release()
            await await_until(sending, what="the skipped send")
            await demux.barrier("after-the-skip")
            assert dict(demux.grants) == {}
        finally:
            await demux.aclose()
            await stop(client, ingress)


async def test_no_credit_follows_an_error_the_connection_sent_for_the_stream() -> None:
    ingress = _GatedIngress()
    async with FakeRelay() as relay:
        client = _gated_client(relay, ingress)
        await client.start()
        leg = await relay.next_leg()
        demux = _Demux(leg)
        try:
            await leg.handshake(_agreeing_ready())
            demux.start()
            head = _upload_head(UPLOAD)
            sid = head["id"]
            await leg.send(head)
            for start in range(0, 3 * BODY_CHUNK_RAW_BYTES, BODY_CHUNK_RAW_BYTES):
                piece = UPLOAD[start : start + BODY_CHUNK_RAW_BYTES]
                await leg.send(valid_message("frames/request-chunk", id=sid, data=_b64(piece)))
            stream = await _stream(client, sid)
            await async_wait_until(lambda: stream._received == 3 * BODY_CHUNK_RAW_BYTES)
            (conn,) = [c for c in client._live if c.ready.is_set()]

            # The connection's error for this stream waits on a busy send lock...
            await conn._send_lock.acquire()
            try:
                bad = valid_message("frames/request-chunk", id=sid, data="not base64!")
                await leg.send(bad)
                await until_parked(conn._send_lock)
                # ...while the web server takes two pieces, which would give the
                # first one's credit back.
                ingress.allow(2)
                await ingress.parked()
            finally:
                conn._send_lock.release()
            error = await demux.next_frame(sid)
            assert error["type"] == "error" and error["code"] == "invalid_message", error
            await demux.barrier("after-the-error")
            assert demux.grants[sid] == []
        finally:
            await demux.aclose()
            await stop(client, ingress)
