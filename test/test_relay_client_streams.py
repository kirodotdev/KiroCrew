"""A relayed stream reports itself done even when it is cancelled before it starts.

The relay can send a stream's opening frame and its cancel back to back, so the
client may cancel a stream's task before the task has run at all. A task cancelled
that early never enters its coroutine, so the stream's own cleanup cannot be what
removes it from the connection; these tests pin that it is removed anyway.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest
from relay_client_helpers import OWNER, TOKEN, ready_frame, valid_message

from kiro_crew.relay_client.connection import HostConnection, RelayTimings
from kiro_crew.relay_client.streams import HttpStream, WsStream
from kiro_crew.relay_client.token_source import HostCredential
from kiro_crew.testing.wait import await_until

pytestmark = pytest.mark.asyncio


class _NeverIngress:
    """An ingress the cancelled streams must never reach."""

    def __init__(self) -> None:
        self.calls: List[str] = []

    async def open_http(self, request: Any, body: Any) -> Any:
        self.calls.append("http")
        raise AssertionError("a stream cancelled before it started opened a request")

    async def open_websocket(self, request: Any) -> Any:
        self.calls.append("ws")
        raise AssertionError("a stream cancelled before it started opened a WebSocket")

    def bind_host(self, *, crew_kind: str, owner_user_id: str) -> None:
        return None

    def unbind_host(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


async def _no_send(frame: Dict[str, Any]) -> None:
    raise AssertionError(f"a stream cancelled before it started sent {frame}")


async def test_an_http_stream_cancelled_before_it_starts_is_reported_done() -> None:
    done: List[int] = []
    ingress = _NeverIngress()
    stream = HttpStream(
        valid_message("frames/request", 1),
        send=_no_send,
        ingress=ingress,
        on_done=done.append,
        credit=None,
        first_byte_seconds=50.0,
    )
    stream.start()
    stream.feed(b"x" * 1024)
    # No await between start and cancel: the task has not run yet.
    stream.cancel()
    await await_until(asyncio.ensure_future(stream.wait()), what="the cancelled stream")
    await asyncio.sleep(0)
    assert done == [stream.id]
    assert ingress.calls == []


async def test_a_ws_stream_cancelled_before_it_starts_is_reported_done() -> None:
    done: List[int] = []
    ingress = _NeverIngress()
    stream = WsStream(
        valid_message("frames/ws-upgrade"),
        send=_no_send,
        ingress=ingress,
        on_done=done.append,
        pause_watchdog_seconds=3600.0,
    )
    stream.start()
    stream.set_paused(True)
    stream.relay_closed(1000, "")
    await await_until(asyncio.ensure_future(stream.wait()), what="the cancelled stream")
    await asyncio.sleep(0)
    assert done == [stream.id]
    assert ingress.calls == []
    # The pause watchdog armed before the cancel is disarmed with the stream.
    assert stream._watchdog is None


async def test_done_is_reported_once_for_a_stream_that_ran() -> None:
    done: List[int] = []

    class _Unreachable(_NeverIngress):
        async def open_http(self, request: Any, body: Any) -> Any:
            from kiro_crew.relay_client.ingress import IngressUnreachable

            raise IngressUnreachable("nothing listens")

    sent: List[Dict[str, Any]] = []

    async def send(frame: Dict[str, Any]) -> None:
        sent.append(frame)

    stream = HttpStream(
        valid_message("frames/request", 0),
        send=send,
        ingress=_Unreachable(),
        on_done=done.append,
        credit=None,
        first_byte_seconds=50.0,
    )
    stream.start()
    await await_until(asyncio.ensure_future(stream.wait()), what="the stream")
    await asyncio.sleep(0)
    stream.cancel()
    await asyncio.sleep(0)
    assert done == [stream.id]
    assert [f["code"] for f in sent] == ["crew_unreachable"]


async def test_one_full_size_ws_message_always_fits_an_empty_queue() -> None:
    from kiro_crew.relay_client.protocol import (
        REQUEST_BUFFER_MAX_BYTES,
        WS_MESSAGE_MAX_BYTES,
        FrameError,
    )

    stream = WsStream(
        valid_message("frames/ws-upgrade"),
        send=_no_send,
        ingress=_NeverIngress(),
        on_done=lambda _sid: None,
        pause_watchdog_seconds=3600.0,
    )
    assert WS_MESSAGE_MAX_BYTES > REQUEST_BUFFER_MAX_BYTES
    # The largest message the contract allows queues behind nothing.
    stream.from_relay(b"x" * WS_MESSAGE_MAX_BYTES)
    # A second one behind it is a backlog past the per-stream bound.
    with pytest.raises(FrameError) as caught:
        stream.from_relay(b"y")
    assert caught.value.code == "message_too_large"


def test_the_connection_budget_bounds_every_stream_together() -> None:
    from kiro_crew.relay_client.protocol import FrameError
    from kiro_crew.relay_client.streams import BufferBudget

    budget = BufferBudget(ceiling=100)
    budget.take(60, 1)
    budget.take(40, 2)
    with pytest.raises(FrameError) as caught:
        budget.take(1, 3)
    assert caught.value.stream_id == 3
    budget.give(40)
    budget.take(40, 3)
    assert budget.held == 100


async def test_a_released_http_stream_gives_its_bytes_back() -> None:
    from kiro_crew.relay_client.streams import BufferBudget

    budget = BufferBudget(ceiling=10_000)
    stream = HttpStream(
        valid_message("frames/request", 1),
        send=_no_send,
        ingress=_NeverIngress(),
        on_done=lambda _sid: None,
        credit=None,
        first_byte_seconds=50.0,
        budget=budget,
    )
    stream.start()
    stream.feed(b"x" * 4000)
    assert budget.held == 4000
    stream.cancel()
    await await_until(asyncio.ensure_future(stream.wait()), what="the cancelled stream")
    await asyncio.sleep(0)
    assert budget.held == 0


async def test_a_ws_message_stays_in_the_budget_until_the_crew_takes_it() -> None:
    from kiro_crew.relay_client.streams import BufferBudget

    taken = asyncio.Event()
    release = asyncio.Event()

    class _SlowSocket:
        close_code = 1000

        async def send(self, data: Any) -> None:
            taken.set()
            await release.wait()

        async def receive(self) -> Any:
            await release.wait()
            return None

        async def close(self, code: int = 1000, reason: str = "") -> None:
            return None

    class _Ingress(_NeverIngress):
        async def open_websocket(self, request: Any) -> Any:
            return _SlowSocket()

    sent: List[Dict[str, Any]] = []

    async def send(frame: Dict[str, Any]) -> None:
        sent.append(frame)

    budget = BufferBudget(ceiling=10_000)
    stream = WsStream(
        valid_message("frames/ws-upgrade"),
        send=send,
        ingress=_Ingress(),
        on_done=lambda _sid: None,
        pause_watchdog_seconds=3600.0,
        budget=budget,
    )
    stream.start()
    stream.from_relay(b"m" * 3000)
    await await_until(taken, what="the Crew's side reading the message")
    # Being read, not yet taken: still held.
    assert budget.held == 3000
    stream.cancel()
    await await_until(asyncio.ensure_future(stream.wait()), what="the cancelled stream")
    await asyncio.sleep(0)
    assert budget.held == 0


async def test_empty_partial_pieces_have_a_count_cap(monkeypatch) -> None:
    from kiro_crew.relay_client import streams
    from kiro_crew.relay_client.protocol import FrameError

    # raising=False also lets this regression run against the uncapped parent.
    monkeypatch.setattr(streams, "WS_REASSEMBLY_MAX_PIECES", 4, raising=False)
    assembler = streams.WsReassembler()
    frame = {"type": "ws-frame", "id": 71, "data": "", "partial": True}
    for _ in range(4):
        assert assembler.add(frame) is None
    with pytest.raises(FrameError) as caught:
        assembler.add(frame)
    assert caught.value.code == "message_too_large"
    assert caught.value.stream_id == 71
    assert assembler._parts == assembler._sizes == assembler._last == {}
    assert assembler._total() == 0


async def test_empty_partial_pieces_are_charged_and_do_not_refresh_timeout() -> None:
    from kiro_crew.relay_client.protocol import WS_REASSEMBLY_PIECE_TIMEOUT_SECONDS
    from kiro_crew.relay_client.streams import WsReassembler

    now = 0.0
    assembler = WsReassembler(monotonic=lambda: now)
    frame = {"type": "ws-frame", "id": 71, "data": "", "partial": True}
    assembler.add(frame)
    now = WS_REASSEMBLY_PIECE_TIMEOUT_SECONDS
    assembler.add(frame)
    assert assembler._total() >= 2
    now += 1
    assert assembler.stale() == [71]


async def test_empty_http_chunks_are_not_queued() -> None:
    stream = HttpStream(
        valid_message("frames/request", 1, headers={"content-type": "application/octet-stream"}),
        send=_no_send,
        ingress=_NeverIngress(),
        on_done=lambda _sid: None,
        credit=None,
        first_byte_seconds=50.0,
    )
    for _ in range(100):
        stream.feed(b"")
    assert stream._body.empty()
    assert stream._buffered == stream._budget.held == 0
    stream.end_body()
    assert [piece async for piece in stream._iter_body()] == []
    # Empty data must not bypass the existing after-end rejection.
    from kiro_crew.relay_client.protocol import FrameError

    with pytest.raises(FrameError):
        stream.feed(b"")


@pytest.mark.parametrize("message", ["", b""])
async def test_ws_message_count_includes_the_inflight_send(monkeypatch, message) -> None:
    from kiro_crew.relay_client import streams
    from kiro_crew.relay_client.protocol import FrameError

    monkeypatch.setattr(streams, "WS_QUEUE_MAX_MESSAGES", 4, raising=False)
    entered = asyncio.Event()
    release = asyncio.Event()
    received = []

    class Socket:
        async def send(self, data):
            received.append(data)
            entered.set()
            await release.wait()

    stream = WsStream(
        valid_message("frames/ws-upgrade"),
        send=_no_send,
        ingress=_NeverIngress(),
        on_done=lambda _sid: None,
        pause_watchdog_seconds=3600.0,
    )
    stream.from_relay(message)
    writer = asyncio.create_task(stream._write_to_crew(Socket()))
    try:
        await await_until(entered, what="the empty message entering the Crew send")
        assert received == [message]
        for _ in range(3):
            stream.from_relay(message)
        with pytest.raises(FrameError) as caught:
            stream.from_relay(b"refused")
        assert caught.value.code == "message_too_large"
        assert caught.value.stream_id == stream.id
        with pytest.raises(FrameError):
            stream.from_relay(message)
        assert stream._to_crew_count == 4
        assert stream._to_crew.qsize() == 3
        assert stream._to_crew_bytes == stream._budget.held == 0
    finally:
        writer.cancel()
        await await_until(
            asyncio.gather(writer, return_exceptions=True), what="the held WebSocket writer"
        )
        stream._finish()
    assert stream._to_crew_count == 0
    assert stream._to_crew_bytes == stream._budget.held == 0


async def test_backoff_delay_history_is_bounded(monkeypatch) -> None:
    from kiro_crew.relay_client import client as client_mod

    monkeypatch.setattr(client_mod, "BACKOFF_HISTORY_MAX_ENTRIES", 4, raising=False)

    async def no_sleep(_delay):
        return None

    client = client_mod.RelayClient(
        host_url="ws://127.0.0.1/unused",
        crew_url_for=lambda _ep: "",
        token_source=None,
        ingress=_NeverIngress(),
        client_version="test",
        sleep=no_sleep,
    )
    for delay in range(10):
        await await_until(asyncio.create_task(client._wait(float(delay))), what="the backoff wait")
    assert list(client.delays) == [6.0, 7.0, 8.0, 9.0]


@pytest.mark.parametrize("lone_padding", [False, True])
async def test_binary_reassembly_accepts_exactly_ten_mib(lone_padding) -> None:
    import base64

    from kiro_crew.relay_client.protocol import WS_MESSAGE_MAX_BYTES, split_ws_data
    from kiro_crew.relay_client.streams import WsReassembler

    payload = b"x" * WS_MESSAGE_MAX_BYTES
    pieces = split_ws_data(base64.b64encode(payload).decode("ascii"))
    if lone_padding:
        assert pieces[-1].endswith("==")
        pieces[-1:] = [pieces[-1][:-1], "="]
    assembler = WsReassembler()
    for index, piece in enumerate(pieces):
        result = assembler.add(
            {
                "type": "ws-frame",
                "id": 71,
                "data": piece,
                "binary": True,
                **({"partial": True} if index < len(pieces) - 1 else {}),
            }
        )
        if index < len(pieces) - 1:
            assert result is None
    assert result == payload
    assert assembler._total() == 0
    # Reusing the stream must start a fresh character count and padding suffix.
    assert assembler.add({"id": 71, "data": "eA", "binary": True, "partial": True}) is None
    assert assembler.add({"id": 71, "data": "==", "binary": True}) == b"x"


@pytest.mark.parametrize("piece_size", [None, 3])
async def test_binary_reassembly_refuses_one_byte_over_the_limit(monkeypatch, piece_size) -> None:
    import base64

    from kiro_crew.relay_client import streams
    from kiro_crew.relay_client.protocol import WS_MESSAGE_MAX_BYTES, FrameError, split_ws_data

    limit = WS_MESSAGE_MAX_BYTES if piece_size is None else 10
    monkeypatch.setattr(streams, "WS_MESSAGE_MAX_BYTES", limit)
    encoded = base64.b64encode(b"x" * (limit + 1)).decode("ascii")
    pieces = (
        split_ws_data(encoded)
        if piece_size is None
        else [encoded[i : i + piece_size] for i in range(0, len(encoded), piece_size)]
    )
    assembler = streams.WsReassembler()
    with pytest.raises(FrameError) as caught:
        for index, piece in enumerate(pieces):
            assembler.add(
                {
                    "type": "ws-frame",
                    "id": 71,
                    "data": piece,
                    "binary": True,
                    **({"partial": True} if index < len(pieces) - 1 else {}),
                }
            )
    assert caught.value.code == "message_too_large"
    assert caught.value.stream_id == 71
    assert assembler._total() == 0
    assert assembler._parts == {}


class _HandOffThenCancelIngress(_NeverIngress):
    """Answers the hand-off, but cancels the stream just before the answer lands."""

    def __init__(self) -> None:
        super().__init__()
        self.stream: Any = None
        self.closed: List[str] = []

    async def open_http(self, request: Any, body: Any) -> Any:
        self.calls.append("http")
        ingress = self

        class Response:
            status = 200
            headers: Dict[str, str] = {}

            async def aclose(self) -> None:
                ingress.closed.append("closed")

        # The viewer goes away as the response head arrives: the stream is
        # cancelled while it waits, after this hand-off has already finished.
        self.stream.cancel()
        return Response()


async def test_a_response_that_arrives_as_the_stream_is_cancelled_is_closed() -> None:
    done: List[int] = []
    ingress = _HandOffThenCancelIngress()
    stream = HttpStream(
        valid_message("frames/request", 0),
        send=_no_send,
        ingress=ingress,
        on_done=done.append,
        credit=None,
        first_byte_seconds=50.0,
    )
    ingress.stream = stream
    stream.start()
    await await_until(asyncio.ensure_future(stream.wait()), what="the cancelled stream")
    await asyncio.sleep(0)
    assert ingress.calls == ["http"]
    # The finished hand-off's response is closed, not left holding the
    # loopback connection after the stream's slot is released.
    assert ingress.closed == ["closed"]
    assert done == [stream.id]


class _RecordingSocket:
    """The relay's side of the socket: records each frame the client sends."""

    closed = False

    def __init__(self) -> None:
        self.sent: List[Dict[str, Any]] = []

    async def send_str(self, text: str) -> None:
        self.sent.append(json.loads(text))


def _connection_on(socket: _RecordingSocket) -> HostConnection:
    conn = HostConnection(
        session=MagicMock(),
        url="ws://127.0.0.1:9/v1/host",
        credential=HostCredential(TOKEN, crew_kind="personal", owner_user_id=OWNER),
        ingress=_NeverIngress(),
        binding=("personal", OWNER),
        client_version="0.0.0-test",
        timings=RelayTimings(),
    )
    conn._ws = socket  # type: ignore[assignment]
    return conn


@pytest.mark.parametrize(
    "ending, expected_errors",
    [
        # The over-long chunk is refused; the request-end behind it must not
        # be refused again as a body shorter than its content-length.
        (
            {"type": "request-chunk", "id": 7, "data": "QUJDRA==", "base64": True},
            ["the request body is longer than its content-length"],
        ),
        # The relay cancelled the stream; nothing it sends after is answered.
        ({"type": "request-cancel", "id": 7}, []),
    ],
    ids=["refused", "cancelled"],
)
async def test_a_stream_this_side_ended_ignores_the_relays_later_frames(
    ending: Dict[str, Any], expected_errors: List[str]
) -> None:
    socket = _RecordingSocket()
    conn = _connection_on(socket)
    head = valid_message("frames/request", 0, method="POST", headers={"content-length": "3"})
    # Back to back with no turn of the loop between them, as frames the relay
    # sent together are read: the ended stream is still registered for the last.
    for frame in (ready_frame(), head, ending, {"type": "request-end", "id": 7}):
        assert await conn._dispatch(json.dumps(frame)) is None
    errors = [f["message"] for f in socket.sent if f["type"] == "error" and f.get("id") == 7]
    assert errors == expected_errors
    await await_until(asyncio.ensure_future(conn._cancel_streams()), what="the streams to end")
    assert not conn._streams and not conn._ended_streams


async def test_the_frames_email_and_name_reach_the_ingress() -> None:
    """A checked head's display labels travel beside the request, never as copied headers."""
    from kiro_crew.relay_client.ingress import IngressUnreachable
    from kiro_crew.relay_client.protocol import parse_inbound

    seen: List[Any] = []

    class _Capture(_NeverIngress):
        async def open_http(self, request: Any, body: Any) -> Any:
            seen.append(request)
            raise IngressUnreachable("stop here")

        async def open_websocket(self, request: Any) -> Any:
            seen.append(request)
            raise IngressUnreachable("stop here")

    async def send(frame: Dict[str, Any]) -> None:
        return None

    labels = {"userEmail": "renee@example.com", "userName": "Ren\u00e9e \u00d8"}
    http = HttpStream(
        parse_inbound(json.dumps(valid_message("frames/request", 0, **labels))),
        send=send,
        ingress=_Capture(),
        on_done=lambda _sid: None,
        credit=None,
        first_byte_seconds=50.0,
    )
    ws = WsStream(
        parse_inbound(json.dumps(valid_message("frames/ws-upgrade", **labels))),
        send=send,
        ingress=_Capture(),
        on_done=lambda _sid: None,
        pause_watchdog_seconds=3600.0,
    )
    for stream in (http, ws):
        stream.start()
        await await_until(asyncio.ensure_future(stream.wait()), what="the stream")
    assert [(r.user_email, r.user_name) for r in seen] == [
        ("renee@example.com", "Ren\u00e9e \u00d8"),
        ("renee@example.com", "Ren\u00e9e \u00d8"),
    ]
    # A head without labels hands over empty ones.
    seen.clear()
    plain = HttpStream(
        valid_message("frames/request", 0),
        send=send,
        ingress=_Capture(),
        on_done=lambda _sid: None,
        credit=None,
        first_byte_seconds=50.0,
    )
    plain.start()
    await await_until(asyncio.ensure_future(plain.wait()), what="the stream")
    assert [(r.user_email, r.user_name) for r in seen] == [("", "")]
