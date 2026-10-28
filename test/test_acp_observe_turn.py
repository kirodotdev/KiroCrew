"""AcpSessionHandle.observe_turn: reading a turn KAS started on its own.

When a KAS workflow run ends, KAS wakes the run's parent chat with a model turn
no client prompted. ``observe_turn`` is that turn's reader: it sends nothing,
skips the pre-turn drain (the queued frames ARE the turn), routes the turn's
permission requests to its consumer, and ends on KAS's own ``turn_end``.

Driven through the REAL reader loop on a fake stdout (the pattern of
``test_acp_runtime.py``); no process is launched.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.kas_wire import KIND_TURN_END, KIND_TURN_START, turn_boundary
from kiro_crew.acp.runtime import AcpRuntime, AcpSessionHandle
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    METHOD_PROMPT,
    METHOD_REQUEST_PERMISSION,
    METHOD_SESSION_UPDATE,
    STOP_REASON_END_TURN,
)
from kiro_crew.testing.ids import UNALLOCATABLE_PID
from kiro_crew.testing.wait import async_wait_until, await_until


def _kas_runtime() -> tuple[AcpRuntime, asyncio.StreamReader, MagicMock]:
    rt = AcpRuntime(work_dir="/tmp")
    rt._acp_backend = ACP_BACKEND_KAS
    reader = asyncio.StreamReader()
    reader._loop = asyncio.get_running_loop()
    proc = MagicMock()
    proc.stdout = reader
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.returncode = None
    proc.pid = UNALLOCATABLE_PID
    rt._process = proc
    rt._pid = UNALLOCATABLE_PID
    rt._initialized = True
    return rt, reader, proc


def _feed(reader: asyncio.StreamReader, obj: dict) -> None:
    reader.feed_data((json.dumps(obj) + "\n").encode())


def _update(update: dict, session_id: str = "sA") -> dict:
    return {"method": METHOD_SESSION_UPDATE, "params": {"sessionId": session_id, "update": update}}


def _info(kind: str, **fields) -> dict:
    return _update(
        {"sessionUpdate": "session_info_update", "_meta": {"kiro": {"kind": kind, **fields}}}
    )


def _text(text: str) -> dict:
    return _update(
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}
    )


def _written(proc: MagicMock) -> list[dict]:
    out = []
    for call in proc.stdin.write.call_args_list:
        for line in call.args[0].decode().splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


async def _session(
    session_id: str = "sA",
) -> tuple[AcpRuntime, asyncio.StreamReader, MagicMock, AcpSessionHandle, asyncio.Task]:
    rt, reader, proc = _kas_runtime()
    queue: asyncio.Queue = asyncio.Queue()
    rt._session_queues[session_id] = queue
    handle = AcpSessionHandle(session_id, queue, rt)
    task = asyncio.ensure_future(rt._reader_loop())
    await asyncio.sleep(0)
    return rt, reader, proc, handle, task


async def _stop(task: asyncio.Task) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _queued(handle: AcpSessionHandle, n: int) -> None:
    await async_wait_until(
        lambda: handle._queue.qsize() >= n, describe=lambda: f"{n} frames queued"
    )


def test_turn_boundary_reads_only_kas_turn_frames():
    start = _info(KIND_TURN_START)["params"]["update"]
    end = _info(KIND_TURN_END, stopReason="cancelled")["params"]["update"]
    assert turn_boundary(start) == (KIND_TURN_START, "")
    assert turn_boundary(end) == (KIND_TURN_END, "cancelled")
    assert turn_boundary(_info("context_usage")["params"]["update"]) is None
    assert turn_boundary(_text("hi")["params"]["update"]) is None
    assert turn_boundary({"sessionUpdate": "session_info_update", "_meta": "x"}) is None
    assert turn_boundary(None) is None


@pytest.mark.asyncio
async def test_observe_reads_the_queued_wake_turn_and_ends_on_its_turn_end():
    rt, reader, proc, handle, task = await _session()
    try:
        # The wake turn's frames arrive while nobody reads the session.
        _feed(reader, _info(KIND_TURN_START))
        _feed(reader, _text("The run found "))
        _feed(reader, _text("three issues."))
        _feed(reader, _info(KIND_TURN_END, stopReason="end_turn"))
        await _queued(handle, 4)

        events = []

        async def drive():
            async for ev in handle.observe_turn(timeout=5.0, idle_window=5.0):
                events.append(ev)

        await await_until(asyncio.ensure_future(drive()), what="the observed turn ending")
        text = "".join(e.text for e in events if e.kind == EVENT_TEXT_CHUNK)
        assert text == "The run found three issues."
        terminal = [e for e in events if e.kind == EVENT_COMPLETE]
        assert len(terminal) == 1
        assert terminal[0].stop_reason == STOP_REASON_END_TURN
        assert terminal[0].synthetic_completion is False
        # Nothing was sent for it: no prompt, no request of any kind.
        assert all(m.get("method") != METHOD_PROMPT for m in _written(proc))
        assert _written(proc) == []
        assert handle.is_turn_active is False
        assert "sA" not in rt._turn_active_sessions
    finally:
        await _stop(task)


@pytest.mark.asyncio
async def test_a_turn_end_before_the_wakes_turn_start_belongs_to_an_earlier_turn():
    _rt, reader, _proc, handle, task = await _session()
    try:
        # A prompted turn's trailing turn_end (its response already closed it).
        _feed(reader, _info(KIND_TURN_END, stopReason="end_turn"))
        _feed(reader, _info(KIND_TURN_START))
        _feed(reader, _text("Summary."))
        _feed(reader, _info(KIND_TURN_END, stopReason="end_turn"))
        await _queued(handle, 4)
        events = [ev async for ev in handle.observe_turn(timeout=5.0, idle_window=5.0)]
        assert [e.text for e in events if e.kind == EVENT_TEXT_CHUNK] == ["Summary."]
        assert events[-1].kind == EVENT_COMPLETE
    finally:
        await _stop(task)


@pytest.mark.asyncio
async def test_a_permission_during_the_wake_reaches_the_consumer_and_is_answered():
    rt, reader, proc, handle, task = await _session()
    try:
        _feed(reader, _info(KIND_TURN_START))
        events = []
        seen_permission = asyncio.Event()
        active_while_asking: list[bool] = []

        async def drive():
            async for ev in handle.observe_turn(timeout=5.0, idle_window=5.0):
                events.append(ev)
                if ev.kind == EVENT_PERMISSION_REQUEST:
                    active_while_asking.append("sA" in rt._turn_active_sessions)
                    seen_permission.set()

        driver = asyncio.ensure_future(drive())
        _feed(
            reader,
            _update(
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "tc1",
                    "title": "Read notes.txt",
                    "kind": "read",
                    "rawInput": {"path": "/tmp/notes.txt"},
                }
            ),
        )
        _feed(
            reader,
            {
                "id": 77,
                "method": METHOD_REQUEST_PERMISSION,
                "params": {
                    "sessionId": "sA",
                    "toolCall": {"title": "Read notes.txt", "kind": "read", "toolCallId": "tc1"},
                    "options": [
                        {"optionId": "allow", "name": "Allow once", "kind": "allow_once"},
                        {"optionId": "deny", "name": "Reject", "kind": "reject_once"},
                    ],
                },
            },
        )
        await await_until(seen_permission, what="the wake's permission request")
        # Routed to the consumer, not auto-rejected: nothing answered it yet.
        assert _written(proc) == []
        assert active_while_asking == [True]
        await handle.approve_tool(77)
        answers = [m for m in _written(proc) if m.get("id") == 77]
        assert len(answers) == 1
        assert answers[0]["result"]["outcome"]["optionId"] == "allow"
        _feed(reader, _info(KIND_TURN_END, stopReason="end_turn"))
        await await_until(driver, what="the observed turn ending")
        assert events[-1].kind == EVENT_COMPLETE
        assert "sA" not in rt._turn_active_sessions
    finally:
        await _stop(task)


@pytest.mark.asyncio
async def test_no_engine_turn_ends_the_observer_cleanly_after_the_idle_window():
    _rt, reader, proc, handle, task = await _session()
    try:
        # Between-turn chatter that is not a turn start does not hold it open.
        _feed(reader, _info("context_usage", usagePercentage=12))
        await _queued(handle, 1)
        events = [ev async for ev in handle.observe_turn(timeout=30.0, idle_window=0.2)]
        assert [e.kind for e in events] == [EVENT_COMPLETE]
        assert events[0].stop_reason == STOP_REASON_END_TURN
        assert events[0].synthetic_completion is True
        assert handle.is_turn_active is False
        assert _written(proc) == []
        # The handle takes the next prompt as usual.
        assert handle._abandoned_prompt_id is None
    finally:
        await _stop(task)


@pytest.mark.asyncio
async def test_a_child_sessions_turn_end_does_not_end_the_observed_turn():
    _rt, reader, _proc, handle, task = await _session()
    try:
        _feed(reader, _info(KIND_TURN_START))
        await _queued(handle, 1)
        # Placed straight on the queue: the reader would drop an unannounced
        # child's frame, but a runtime that routes one must not end the turn.
        from kiro_crew.acp.types import JsonRpcMessage

        child = _info(KIND_TURN_END, stopReason="cancelled")
        child["params"]["sessionId"] = "child-1"
        handle._queue.put_nowait(JsonRpcMessage(method=child["method"], params=child["params"]))
        fanned = _info(KIND_TURN_END, stopReason="cancelled")
        handle._queue.put_nowait(
            JsonRpcMessage(method=fanned["method"], params=fanned["params"], fanout_no_owner=True)
        )
        _feed(reader, _text("still going"))
        _feed(reader, _info(KIND_TURN_END, stopReason="end_turn"))
        events = [ev async for ev in handle.observe_turn(timeout=5.0, idle_window=5.0)]
        assert [e.text for e in events if e.kind == EVENT_TEXT_CHUNK] == ["still going"]
        assert events[-1].kind == EVENT_COMPLETE
        assert events[-1].stop_reason == STOP_REASON_END_TURN
    finally:
        await _stop(task)


@pytest.mark.asyncio
async def test_prompt_still_drains_leftovers_and_ends_on_its_response():
    """prompt() is unchanged: stale frames are discarded, the response ends it."""
    rt, reader, proc, handle, task = await _session()
    try:
        _feed(reader, _text("leftover from an abandoned turn"))
        _feed(reader, _info(KIND_TURN_START))
        await _queued(handle, 2)
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=5.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        await async_wait_until(
            lambda: rt._routed_requests, describe=lambda: "the prompt being sent"
        )
        req_id = next(iter(rt._routed_requests))
        # A turn_start/turn_end pair inside a prompted turn does not end it.
        _feed(reader, _info(KIND_TURN_START))
        _feed(reader, _text("answer"))
        _feed(reader, _info(KIND_TURN_END, stopReason="end_turn"))
        _feed(reader, _text(" more"))
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the prompted turn ending")
        assert "".join(e.text for e in events if e.kind == EVENT_TEXT_CHUNK) == "answer more"
        assert any(m.get("method") == METHOD_PROMPT for m in _written(proc))
    finally:
        await _stop(task)
