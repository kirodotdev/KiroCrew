"""A turn or a session start beginning while frames arrive never loses a frame.

The reader loop removes superseded frames only from a parked queue
(``session_frame_queue.drop_superseded_if_parked``). These tests drive the real
``AcpRuntime._reader_loop``: they flip the turn mark between frames at every
position of a stream, and run a session start's mode switch on a new queue. They
check the two things a reader counts on: every frame that arrives once a turn or
a start has begun is delivered, and the queue's length grows by exactly one per
such frame, which is the signal the dispatch loop's oracle guard and the start's
stale-frame count read.
"""

from __future__ import annotations

import pytest
from acp_runtime_helpers import _drain, _feed, _make_runtime, _start_reader, _stop_reader

from kiro_crew.acp.session_frame_queue import SessionFrameQueue
from kiro_crew.acp.types import METHOD_KAS_MCP_STATUS, METHOD_SESSION_UPDATE

_FRAMES = 8


def _status(seq: int) -> dict:
    # The sequence rides in the tool list, which the supersede check ignores, so
    # every frame here supersedes the one before it while the queue is parked.
    return {
        "jsonrpc": "2.0",
        "method": METHOD_KAS_MCP_STATUS,
        "params": {
            "sessionId": "sA",
            "servers": [{"name": "core", "status": "connected", "tools": [{"name": f"t{seq}"}]}],
        },
    }


def _options(seq: int) -> dict:
    return {
        "jsonrpc": "2.0",
        "method": METHOD_SESSION_UPDATE,
        "params": {
            "sessionId": "sA",
            "update": {
                "sessionUpdate": "config_option_update",
                "configOptions": [{"id": "model", "currentValue": f"m{seq}"}],
            },
        },
    }


def _seq_of(msg) -> int:
    params = msg.params
    if msg.method == METHOD_KAS_MCP_STATUS:
        return int(params["servers"][0]["tools"][0]["name"][1:])
    return int(params["update"]["configOptions"][0]["currentValue"][1:])


def _frame(seq: int) -> dict:
    return _status(seq) if seq % 2 == 0 else _options(seq)


def _taken(queue: SessionFrameQueue) -> list[int]:
    out = []
    while not queue.empty():
        out.append(_seq_of(queue.get_nowait()))
        queue.task_done()
    return out


async def _run(begin_at: int, begin, end=None) -> tuple[list[int], list[int]]:
    """Feed _FRAMES frames, calling *begin* before frame *begin_at*.

    Returns the sequences left on the queue and the queue length after each
    frame from *begin_at* on.
    """
    rt, reader, _proc = _make_runtime()
    queue = SessionFrameQueue()
    # A started session waiting for its next turn.
    queue.parked = True
    rt._session_queues["sA"] = queue
    task = await _start_reader(rt)
    lengths: list[int] = []
    try:
        for seq in range(_FRAMES):
            if seq == begin_at:
                begin(rt)
                lengths.append(queue.qsize())
            _feed(reader, _frame(seq))
            await _drain(reader)
            if seq >= begin_at:
                lengths.append(queue.qsize())
        if end is not None:
            end(rt)
        return _taken(queue), lengths
    finally:
        await _stop_reader(task)


def _expected(begin_at: int) -> list[int]:
    """What a parked queue keeps of frames 0..begin_at-1, then every later frame."""
    before = list(range(begin_at))
    kept = [s for s in before if s >= begin_at - 2]  # the newest status and options
    return kept + list(range(begin_at, _FRAMES))


@pytest.mark.asyncio
@pytest.mark.parametrize("begin_at", range(_FRAMES))
async def test_a_turn_that_begins_mid_stream_gets_every_later_frame(begin_at):
    def start_turn(rt):
        rt.mark_turn_active("sA", True)

    kept, lengths = await _run(begin_at, start_turn)

    assert kept == _expected(begin_at)
    assert [b - a for a, b in zip(lengths, lengths[1:])] == [1] * (
        _FRAMES - begin_at
    ), "the queue did not grow by one for each frame the turn is owed"


@pytest.mark.asyncio
@pytest.mark.parametrize("before_switch", range(5))
async def test_a_starting_session_skips_only_the_frames_from_before_its_mode_switch(
    before_switch,
):
    """The start counts the frames queued before ``session/set_mode`` and skips that many.

    The queue is new, so it is not parked: no frame that arrives during the start
    is removed, and the frames past the count are exactly the switched-to agent's.
    """
    rt, reader, _proc = _make_runtime()
    queue = SessionFrameQueue()
    rt._session_queues["sA"] = queue
    task = await _start_reader(rt)
    try:
        for seq in range(before_switch):
            _feed(reader, _frame(seq))
            await _drain(reader)
        staged = queue.qsize()  # ``AcpSessionHandle.queued_frame_count``
        after = list(range(before_switch, before_switch + 4))
        for seq in after:
            _feed(reader, _frame(seq))
            await _drain(reader)
            assert queue.qsize() == staged + after.index(seq) + 1

        kept = _taken(queue)
        assert kept == list(range(before_switch)) + after
        assert kept[staged:] == after, "the skip by count hid a post-switch frame"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_the_turn_mark_unparks_the_queue_and_parks_it_again():
    rt, _reader, _proc = _make_runtime()
    queue = SessionFrameQueue()
    rt._session_queues["sA"] = queue
    assert queue.parked is False
    rt.mark_turn_active("sA", True)
    assert queue.parked is False
    rt.mark_turn_active("sA", False)
    assert queue.parked is True
    rt.mark_turn_active("sA", True)
    assert queue.parked is False


@pytest.mark.asyncio
async def test_a_queue_parked_again_after_a_turn_resumes_keeping_one_of_each():
    def start_turn(rt):
        rt.mark_turn_active("sA", True)

    rt, reader, _proc = _make_runtime()
    queue = SessionFrameQueue()
    queue.parked = True
    rt._session_queues["sA"] = queue
    task = await _start_reader(rt)
    try:
        start_turn(rt)
        for seq in range(4):
            _feed(reader, _frame(seq))
            await _drain(reader)
        assert queue.qsize() == 4
        rt.mark_turn_active("sA", False)
        for seq in range(4, 8):
            _feed(reader, _frame(seq))
            await _drain(reader)
        # Once parked again, each new frame removes the newest older one of its
        # kind, so the queue stops growing: two frames from the turn stay, and
        # one of each kind after them.
        assert _taken(queue) == [0, 1, 6, 7]
    finally:
        await _stop_reader(task)
