"""An awaited request id is registered BEFORE its write drains.

``AcpRuntime.send_request`` writes the frame and then awaits ``stdin.drain()``.
The backend can answer during that suspension, and the reader routes the answer
onto the session queue at once. If the id joins ``_awaited_responses`` only
after the drain resumes, a queue consumer that runs in the window (the pre-turn
drain of a starting turn, or a live turn's dispatch loop) sees a response owed
to nobody and drops it, and the caller waits out its whole timeout.

These tests answer inside the drain and run a real consumer there.
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_update_provider import _UNALLOCATABLE_PID

from kiro_crew.acp.runtime import AcpRuntime, AcpRuntimeDead
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import JsonRpcMessage


def _handle() -> tuple[AcpSessionHandle, AcpRuntime, MagicMock]:
    rt = AcpRuntime(work_dir="/tmp")
    proc = MagicMock()
    proc.stdout = asyncio.StreamReader()
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.returncode = None
    proc.pid = _UNALLOCATABLE_PID
    rt._process = proc
    rt._pid = _UNALLOCATABLE_PID
    rt._initialized = True
    queue: asyncio.Queue = asyncio.Queue()
    rt._session_queues["sA"] = queue
    return AcpSessionHandle("sA", queue, rt), rt, proc


async def _run_pre_turn_drain(handle: AcpSessionHandle, rt: AcpRuntime) -> None:
    """Start (and abandon) a turn so its pre-turn drain consumes the queue."""
    rt.send_request = AsyncMock(return_value=999)  # type: ignore[method-assign]
    gen = handle.prompt("hi", timeout=0.2)
    with contextlib.suppress(StopAsyncIteration, asyncio.TimeoutError, Exception):
        await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    await gen.aclose()


def _answer_inside_drain(handle: AcpSessionHandle, rt: AcpRuntime, proc: MagicMock, result):
    """While the request's drain is suspended: the backend answers, the reader
    routes the answer to the session queue, and a new turn's pre-turn drain
    runs before the caller has resumed."""

    async def _drain() -> None:
        (req_id,) = list(rt._routed_requests)
        handle._queue.put_nowait(JsonRpcMessage(id=req_id, result=result))
        await _run_pre_turn_drain(handle, rt)

    proc.stdin.drain = AsyncMock(side_effect=_drain)


@pytest.mark.asyncio
async def test_send_command_keeps_a_response_that_lands_during_the_drain():
    handle, rt, proc = _handle()
    _answer_inside_drain(handle, rt, proc, {"text": "compacted"})

    text = await asyncio.wait_for(handle.send_command("/tools"), timeout=5.0)

    assert text == "compacted"
    assert not handle._awaited_responses


@pytest.mark.asyncio
async def test_set_config_option_keeps_a_response_that_lands_during_the_drain():
    handle, rt, proc = _handle()
    _answer_inside_drain(handle, rt, proc, {})

    await asyncio.wait_for(handle.set_config_option("effort", "high"), timeout=5.0)

    assert not handle._awaited_responses


@pytest.mark.asyncio
async def test_a_failed_send_does_not_leave_its_id_registered():
    handle, _rt, proc = _handle()
    proc.stdin.drain = AsyncMock(side_effect=BrokenPipeError("gone"))

    with pytest.raises(AcpRuntimeDead):
        await handle.set_config_option("effort", "high")

    assert not handle._awaited_responses
