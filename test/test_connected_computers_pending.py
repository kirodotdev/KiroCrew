"""Bounded accounting for connected-computer replies larger than the tracking limit."""

from __future__ import annotations

import asyncio
import json
from collections import deque

import pytest

from kiro_crew.connected_computers import launch
from kiro_crew.connected_computers.launch import PendingCalls
from kiro_crew.remote_session.link import LinkUnavailable
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS


def _call(rid: int | str) -> bytes:
    return (json.dumps({"id": rid, "method": "tools/call"}) + "\n").encode("utf-8")


def _feed(pending: PendingCalls, data: bytes, chunk_size: int) -> None:
    for start in range(0, len(data), chunk_size):
        pending.observe_response(data[start : start + chunk_size])
        if pending._down_skip:
            assert len(pending._down_head) <= launch._MAX_RESPONSE_PREFIX
        else:
            assert len(pending._down_head) <= launch._MAX_TRACKED_LINE


@pytest.fixture
def small_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launch, "_MAX_TRACKED_LINE", 256)
    monkeypatch.setattr(launch, "_MAX_RESPONSE_PREFIX", 96)


def test_a_reply_over_the_real_limit_releases_only_its_slot() -> None:
    pending = PendingCalls()
    for rid in (7, 8):
        assert pending.admit(_call(rid)) == (_call(rid), b"")
    pending.observe_response(b'{"jsonrpc":"2.0","id":7,"result":{"data":"')
    chunk = b"x" * (32 * 1024)
    for _ in range(launch._MAX_TRACKED_LINE // len(chunk) + 1):
        _feed(pending, chunk, len(chunk))
    assert pending._down_skip
    assert len(pending._down_head) == launch._MAX_RESPONSE_PREFIX
    assert len(pending) == 2, "the slot stays occupied until the reply's newline"
    pending.observe_response(b'"}}\n')
    assert len(pending) == 1
    assert json.loads(pending.close_out("unknown"))["id"] == 8


@pytest.mark.parametrize("chunk_size", [1, 31, 1024])
@pytest.mark.parametrize("rid", [7, "7", 'quoted-"\\-\u00e9'])
def test_oversized_replies_release_slots_across_chunk_boundaries(
    small_bounds: None, chunk_size: int, rid: int | str
) -> None:
    pending = PendingCalls()
    assert pending.admit(_call(rid)) == (_call(rid), b"")
    reply = (json.dumps({"id": rid, "result": "x" * 512}) + "\n").encode("utf-8")
    _feed(pending, reply, chunk_size)
    assert len(pending) == 0
    assert not pending.mid_response
    assert pending.close_out("unknown") == b""


def test_65_sequential_oversized_replies_do_not_exhaust_admission(small_bounds: None) -> None:
    pending = PendingCalls()
    for rid in range(65):
        call = _call(rid)
        assert pending.admit(call) == (call, b""), f"call {rid} must not be refused"
        reply = (json.dumps({"id": rid, "result": "x" * 512}) + "\n").encode("utf-8")
        _feed(pending, reply, 31)
    assert len(pending) == 0
    assert pending.close_out("unknown") == b""


def test_an_id_beyond_the_prefix_stays_pending_until_close(small_bounds: None) -> None:
    pending = PendingCalls()
    for rid in (8, 7):
        assert pending.admit(_call(rid)) == (_call(rid), b"")
    reply = (json.dumps({"result": "x" * 512, "id": 7}) + "\n").encode("utf-8")
    _feed(pending, reply, 31)
    assert len(pending) == 2, "neither the oldest call nor an unobserved id may be guessed"
    pending.observe_response(b'{"id":8,"result":{}}\n')
    assert len(pending) == 1
    unknown = json.loads(pending.close_out(launch.ended_message("Laptop")))
    assert unknown["id"] == 7
    assert unknown["result"]["isError"] is True
    assert "unknown" in unknown["result"]["content"][0]["text"]
    assert "not retried" in unknown["result"]["content"][0]["text"]
    assert len(pending) == 0


def test_a_half_delivered_oversized_reply_still_gets_an_unknown_outcome(
    small_bounds: None,
) -> None:
    pending = PendingCalls()
    assert pending.admit(_call(7)) == (_call(7), b"")
    _feed(pending, b'{"id":7,"result":"' + b"x" * 512, 31)
    assert pending.mid_response
    assert len(pending) == 1
    unknown = pending.close_out("unknown")
    assert unknown.startswith(b"\n")
    assert json.loads(unknown)["id"] == 7
    assert not pending.mid_response
    assert not pending._down_head
    assert not pending._down_skip


def test_oversized_then_small_reply_in_one_read_resets_the_prefix(small_bounds: None) -> None:
    pending = PendingCalls()
    for rid in (7, 8):
        assert pending.admit(_call(rid)) == (_call(rid), b"")
    pending.observe_response(b'{"id":7,"result":"' + b"x" * 512 + b'"}\n{"id":8,"result":{}}\n')
    assert len(pending) == 0
    assert not pending._down_head
    assert not pending._down_skip


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        (b'{"id":7,"result":"unfinished', "7"),
        (b'{"result":{},"id":"7",', '"7"'),
        (b' { "id" : 7 , "result" : "unfinished', "7"),
        (b'{"meta":{"id":8},"id":7,"result":"unfinished', "7"),
        (b'{"meta":"\\"id\\":8","id":7,"result":"unfinished', "7"),
        (b'{"result":{"id":7,"data":"unfinished', None),
        (b'{"result":"\\"id\\":7 unfinished', None),
        (b'{"method":"server/request","id":7,"params":"unfinished', None),
        (b'{"id":7,"method":"server/request","params":"unfinished', None),
        (b'{"id":7', None),
        (b'{"id":7e', None),
        (b'{"id":"unfinished', None),
        (b'{"\\u0069d":7,"result":"unfinished', "7"),
        (b'{"id":7,"result":"utf8-\xc3', "7"),
        (b'[{"id":7,"result":"unfinished', None),
    ],
)
def test_prefix_matching_uses_complete_top_level_fields(
    prefix: bytes, expected: str | None
) -> None:
    assert launch._response_key_from_prefix(prefix) == expected


class _DrainSignal(asyncio.Event):
    """Expose the actual reservation wait, not an assumed scheduling delay."""

    def __init__(self) -> None:
        super().__init__()
        self.parked = asyncio.Event()

    async def wait(self) -> bool:
        self.parked.set()
        return await super().wait()


class _Channel:
    def __init__(self) -> None:
        self.stdout = asyncio.StreamReader()
        self.sent: list[bytes] = []
        self.input_closed = False

    async def write(self, data: bytes) -> None:
        self.sent.append(data)

    def close_input(self) -> None:
        self.input_closed = True

    async def aclose(self) -> None:
        self.stdout.feed_eof()


async def _link() -> tuple[launch.ChannelLink, _Channel, _DrainSignal]:
    channel = _Channel()

    async def dial() -> _Channel:
        return channel

    link = launch.ChannelLink("worker", b"", dial, label="Laptop")
    drain = _DrainSignal()
    link._drained = drain
    link.start()
    await link.wait_connected(FALLBACK_TIMEOUT_SECS)
    return link, channel, drain


def _assert_retained_bounds(link: launch.ChannelLink) -> None:
    pending = link.pending
    # Every variable-length transport field is named, including backing bytes
    # which a partially consumed memoryview still retains.
    bounds = {
        "PendingCalls._ids.count": (len(pending._ids), launch._MAX_PENDING_CALLS),
        "PendingCalls._ids.bytes": (sum(map(len, pending._ids)), launch._MAX_ID_BYTES),
        "PendingCalls._refusal": (len(pending._refusal), launch._MAX_ERROR_TEXT_CHARS),
        "PendingCalls._up": (len(pending._up), launch._MAX_TRACKED_LINE),
        "PendingCalls._down_head": (
            len(pending._down_head),
            launch._MAX_RESPONSE_PREFIX if pending._down_skip else launch._MAX_TRACKED_LINE,
        ),
        "ChannelLink._refusals + _synthetic.count": (
            len(link._refusals) + bool(link._synthetic),
            launch._MAX_QUEUED_REPLIES,
        ),
        "ChannelLink._refusals + _synthetic.bytes": (
            sum(map(len, link._refusals)) + memoryview(link._synthetic.obj).nbytes,
            launch._MAX_QUEUED_REPLY_BYTES,
        ),
        "ChannelLink._synthetic": (memoryview(link._synthetic.obj).nbytes, launch._MAX_ERROR_BYTES),
        "ChannelLink._outcome": (len(link._outcome), launch._MAX_ERROR_TEXT_CHARS),
        "ChannelLink.error": (len(link.error), launch._MAX_ERROR_TEXT_CHARS),
        "ChannelLink.label": (len(link.label), launch.LABEL_MAX),
    }
    for field, (retained, budget) in bounds.items():
        assert retained <= budget, (field, retained, budget)
    assert (
        link._queued_bytes == sum(map(len, link._refusals)) + memoryview(link._synthetic.obj).nbytes
    )
    assert all(value is None for value in pending._ids.values())
    assert isinstance(link._refusals, deque)
    assert isinstance(link._synthetic, memoryview)
    assert set(vars(link.stdout)) == {"_link", "_reading"}, "reader has no hidden byte queue"
    assert {
        name for name, value in vars(pending).items() if isinstance(value, (dict, bytearray))
    } == {"_ids", "_up", "_down_head"}
    assert {
        name for name, value in vars(link).items() if isinstance(value, (deque, memoryview))
    } == {"_refusals", "_synthetic"}


@pytest.mark.asyncio
@pytest.mark.parametrize("byte_limited", [False, True])
async def test_a_stalled_bridge_bounds_1000_calls_and_resumes_in_order(
    monkeypatch: pytest.MonkeyPatch, byte_limited: bool
) -> None:
    if byte_limited:
        # Leave space for a worst-case id, but exercise the byte watermark
        # before the independent answer-count watermark.
        monkeypatch.setattr(launch, "_MAX_QUEUED_REPLY_BYTES", launch._MAX_ERROR_BYTES + 1024)
    link, channel, drain = await _link()
    writer = asyncio.create_task(link.write(b"".join(_call(rid) for rid in range(1000))))
    try:
        await asyncio.wait_for(drain.parked.wait(), FALLBACK_TIMEOUT_SECS)
        assert not writer.done(), "write must park without the bridge reading replies"
        _assert_retained_bounds(link)
        if byte_limited:
            assert len(link._refusals) < launch._MAX_QUEUED_REPLIES
            assert link._queued_bytes + launch._MAX_ERROR_BYTES > launch._MAX_QUEUED_REPLY_BYTES
        else:
            assert len(link._refusals) == launch._MAX_QUEUED_REPLIES
        assert [json.loads(line)["id"] for line in channel.sent] == list(range(64))
        replies: list[dict] = []
        partial = bytearray()
        while len(replies) < 1000 - launch._MAX_PENDING_CALLS:
            data = await asyncio.wait_for(link.stdout.read(97), FALLBACK_TIMEOUT_SECS)
            assert data, "the stream ended before every refusal was answered"
            partial.extend(data)
            _assert_retained_bounds(link)
            while b"\n" in partial:
                line, _, rest = partial.partition(b"\n")
                partial[:] = rest
                replies.append(json.loads(line))
        await asyncio.wait_for(writer, FALLBACK_TIMEOUT_SECS)
        assert not partial
        assert [reply["id"] for reply in replies] == list(range(64, 1000))
        assert all("not sent" in reply["result"]["content"][0]["text"] for reply in replies)
        assert all("did not run" in reply["result"]["content"][0]["text"] for reply in replies)
        assert link._queued_bytes == 0
        assert len(channel.sent) == 64
    finally:
        writer.cancel()
        await asyncio.wait_for(
            asyncio.gather(writer, return_exceptions=True), FALLBACK_TIMEOUT_SECS
        )
        await asyncio.wait_for(link.aclose(), FALLBACK_TIMEOUT_SECS)


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["lost", "ended", "closed"])
async def test_terminal_state_wakes_a_backpressured_writer_without_growing_output(
    terminal: str,
) -> None:
    link, channel, drain = await _link()
    writer = asyncio.create_task(link.write(b"".join(_call(rid) for rid in range(1000))))
    try:
        await asyncio.wait_for(drain.parked.wait(), FALLBACK_TIMEOUT_SECS)
        expected_refusals = list(range(64, 64 + len(link._refusals)))
        queued = link._queued_bytes
        if terminal == "lost":
            link._lost("disconnected")
        elif terminal == "ended":
            link._ended()
        else:
            await asyncio.wait_for(link.aclose(), FALLBACK_TIMEOUT_SECS)
        assert link._queued_bytes == queued, "terminal outcomes must be generated lazily"
        with pytest.raises(LinkUnavailable):
            await asyncio.wait_for(writer, FALLBACK_TIMEOUT_SECS)
        out = bytearray()
        while True:
            try:
                data = await asyncio.wait_for(link.stdout.read(1024), FALLBACK_TIMEOUT_SECS)
            except LinkUnavailable:
                break
            if not data:
                break
            out.extend(data)
            _assert_retained_bounds(link)
        replies = [json.loads(line) for line in out.splitlines()]
        expected = expected_refusals + (list(range(64)) if terminal != "closed" else [])
        assert [reply["id"] for reply in replies] == expected
        assert len(channel.sent) == 64
    finally:
        writer.cancel()
        await asyncio.wait_for(
            asyncio.gather(writer, return_exceptions=True), FALLBACK_TIMEOUT_SECS
        )
        await asyncio.wait_for(link.aclose(), FALLBACK_TIMEOUT_SECS)


def test_request_bound_applies_before_copying_a_complete_or_partial_line(
    small_bounds: None,
) -> None:
    for suffix in (b"", b"\n"):
        pending = PendingCalls()
        assert pending.admit(b"x" * (launch._MAX_TRACKED_LINE + 1) + suffix) == (b"", b"")
        assert pending.too_large
        assert not pending._up
        assert not pending._ids


def test_pending_ids_share_a_byte_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = "a" * 30, "b" * 30
    monkeypatch.setattr(launch, "_MAX_ID_BYTES", len(json.dumps(first)))
    pending = PendingCalls()
    assert pending.admit(_call(first)) == (_call(first), b"")
    sent, refused = pending.admit(_call(second))
    assert not sent
    assert json.loads(refused)["id"] == second
    assert list(pending._ids) == [json.dumps(first)]
    pending.observe_response((json.dumps({"id": first, "result": {}}) + "\n").encode())
    assert pending.admit(_call(second)) == (_call(second), b"")


@pytest.mark.asyncio
async def test_eof_refusal_uses_the_shared_queue_and_close_input_is_idempotent() -> None:
    link, channel, _ = await _link()
    try:
        for rid in range(64 + launch._MAX_QUEUED_REPLIES - 1):
            await asyncio.wait_for(link.write(_call(rid)), FALLBACK_TIMEOUT_SECS)
        rid = 64 + launch._MAX_QUEUED_REPLIES - 1
        await asyncio.wait_for(link.write(_call(rid).rstrip(b"\n")), FALLBACK_TIMEOUT_SECS)
        link.close_input()
        task = link._tail
        assert task is not None
        link.close_input()
        assert link._tail is task
        await asyncio.wait_for(task, FALLBACK_TIMEOUT_SECS)
        assert channel.input_closed
        assert len(link._refusals) == launch._MAX_QUEUED_REPLIES
        _assert_retained_bounds(link)
        assert json.loads(link._refusals[-1])["id"] == rid
    finally:
        await asyncio.wait_for(link.aclose(), FALLBACK_TIMEOUT_SECS)


@pytest.mark.asyncio
async def test_partial_lines_and_response_prefix_obey_the_field_budget_table(
    small_bounds: None,
) -> None:
    link, _, _ = await _link()
    try:
        await asyncio.wait_for(link.write(b"x" * launch._MAX_TRACKED_LINE), FALLBACK_TIMEOUT_SECS)
        assert len(link.pending._up) == launch._MAX_TRACKED_LINE
        link.pending.observe_response(b"x" * launch._MAX_TRACKED_LINE)
        assert len(link.pending._down_head) == launch._MAX_TRACKED_LINE
        _assert_retained_bounds(link)
        link.pending.observe_response(b"x")
        assert len(link.pending._down_head) == launch._MAX_RESPONSE_PREFIX
        assert link.pending._down_skip
        _assert_retained_bounds(link)
        with pytest.raises(launch.RequestTooLarge):
            await asyncio.wait_for(link.write(b"x\n"), FALLBACK_TIMEOUT_SECS)
        assert not link.pending._up
        _assert_retained_bounds(link)
    finally:
        await asyncio.wait_for(link.aclose(), FALLBACK_TIMEOUT_SECS)


@pytest.mark.asyncio
async def test_a_refusal_waits_for_a_remote_reply_boundary() -> None:
    link, channel, _ = await _link()
    try:
        await asyncio.wait_for(
            link.write(b"".join(_call(rid) for rid in range(64))), FALLBACK_TIMEOUT_SECS
        )
        head = b'{"id":0,"result":'
        channel.stdout.feed_data(head)
        assert await asyncio.wait_for(link.stdout.read(1024), FALLBACK_TIMEOUT_SECS) == head
        await asyncio.wait_for(link.write(_call(64)), FALLBACK_TIMEOUT_SECS)
        assert link._refusals
        _assert_retained_bounds(link)
        channel.stdout.feed_data(b"{}}\n")
        assert await asyncio.wait_for(link.stdout.read(1024), FALLBACK_TIMEOUT_SECS) == b"{}}\n"
        answer = await asyncio.wait_for(link.stdout.read(1024), FALLBACK_TIMEOUT_SECS)
        assert json.loads(answer)["id"] == 64
        _assert_retained_bounds(link)
    finally:
        await asyncio.wait_for(link.aclose(), FALLBACK_TIMEOUT_SECS)


@pytest.mark.asyncio
async def test_concurrent_producers_do_not_create_another_capacity_waiter() -> None:
    link, _, drain = await _link()
    writer = asyncio.create_task(link.write(b"".join(_call(rid) for rid in range(1000))))
    try:
        await asyncio.wait_for(drain.parked.wait(), FALLBACK_TIMEOUT_SECS)
        connect_task = link._task
        link.start()
        assert link._task is connect_task
        with pytest.raises(LinkUnavailable, match="already writing"):
            await asyncio.wait_for(link.write(_call(1001)), FALLBACK_TIMEOUT_SECS)
        with pytest.raises(LinkUnavailable, match="already writing"):
            link.close_input()
        assert not link._input_closed
        assert link._tail is None
        _assert_retained_bounds(link)
    finally:
        writer.cancel()
        await asyncio.wait_for(
            asyncio.gather(writer, return_exceptions=True), FALLBACK_TIMEOUT_SECS
        )
        await asyncio.wait_for(link.aclose(), FALLBACK_TIMEOUT_SECS)


@pytest.mark.asyncio
async def test_a_write_argument_over_its_budget_is_not_retained_or_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(launch, "_MAX_WRITE_BYTES", 128)
    link, channel, _ = await _link()
    try:
        with pytest.raises(launch.RequestTooLarge, match="batch too large"):
            await asyncio.wait_for(link.write(b"x" * 129), FALLBACK_TIMEOUT_SECS)
        assert not channel.sent
        assert channel.input_closed
        assert not link._writing
        assert not link.pending._up
        _assert_retained_bounds(link)
    finally:
        await asyncio.wait_for(link.aclose(), FALLBACK_TIMEOUT_SECS)
