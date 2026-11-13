"""``AcpSessionHandle.prompts_written`` counts prompts that reached the adapter.

A caller whose turn timed out reads it to decide whether the prompt went out, so it
must move once the write returns and must not move when the write fails.
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_update_provider import _UNALLOCATABLE_PID

from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.testing.wait import finished


def _handle() -> tuple[AcpSessionHandle, AcpRuntime]:
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
    return AcpSessionHandle("sA", queue, rt), rt


async def _start_and_abandon(handle: AcpSessionHandle) -> None:
    gen = handle.prompt("hi", timeout=0.2)
    with contextlib.suppress(StopAsyncIteration, asyncio.TimeoutError, Exception):
        await finished(gen.__anext__(), "the turn's first event")
    await gen.aclose()


@pytest.mark.asyncio
async def test_a_written_prompt_is_counted_even_when_its_turn_never_finishes():
    handle, rt = _handle()
    rt.send_request = AsyncMock(return_value=7)  # type: ignore[method-assign]
    assert handle.prompts_written == 0

    await _start_and_abandon(handle)

    rt.send_request.assert_awaited_once()
    assert handle.prompts_written == 1


@pytest.mark.asyncio
async def test_a_failed_write_is_not_counted():
    handle, rt = _handle()
    rt.send_request = AsyncMock(side_effect=BrokenPipeError("gone"))  # type: ignore[method-assign]

    await _start_and_abandon(handle)

    rt.send_request.assert_awaited_once()
    assert handle.prompts_written == 0
