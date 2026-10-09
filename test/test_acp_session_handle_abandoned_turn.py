"""A prompt whose consumer leaves before its turn ends is closed before the next turn.

The case that matters is a ``/compact`` cut off by the caller's timeout (the Claude
compaction branch runs ``compact()`` under ``asyncio.wait_for`` and keeps the session).
The backend is still running that turn, and ``session/update`` frames carry no request id,
so without a fence its late output lands in the next turn's reply.

A runtime double that behaves like a real ACP backend: it answers turns in order, so an
unanswered turn ends before the next one starts, and ``session/cancel`` ends it at once.
No child process.
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import MagicMock

import pytest

from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import (
    EVENT_TEXT_CHUNK,
    METHOD_CANCEL,
    METHOD_SESSION_UPDATE,
    JsonRpcMessage,
)


class _Backend:
    """Answers each prompt with ``pong``; a ``/compact`` either ends at once or hangs.

    With ``hold_first_cancel`` the first ``session/cancel`` does not end the open turn:
    its answer is still on the way when the next cancel arrives, which ends it.
    """

    def __init__(
        self, queue: asyncio.Queue, *, compact_ends: bool, hold_first_cancel: bool = False
    ) -> None:
        self.pid = None
        self.is_alive = MagicMock(return_value=True)
        self.supports_image_prompt = False
        self.acp_backend = ""
        self._last_activity = time.monotonic()
        self.wire: list[str] = []
        self._queue = queue
        self._compact_ends = compact_ends
        self._hold_first_cancel = hold_first_cancel
        self._open: int | None = None
        self._next_id = 0
        self.cancel_seen = asyncio.Event()

    def mark_turn_active(self, session_id: str, active: bool) -> None:
        pass

    def _chunk(self, text: str) -> JsonRpcMessage:
        return JsonRpcMessage(
            method=METHOD_SESSION_UPDATE,
            params={
                "sessionId": "sA",
                "update": {"sessionUpdate": "agent_message_chunk", "text": text},
            },
        )

    def _end_open_turn(self, stop_reason: str) -> None:
        if self._open is None:
            return
        self._queue.put_nowait(self._chunk("LATE "))
        self._queue.put_nowait(JsonRpcMessage(id=self._open, result={"stopReason": stop_reason}))
        self._open = None

    async def send_request(self, method: str, params: dict) -> int:
        self._next_id += 1
        req_id = self._next_id
        is_compact = "/compact" in json.dumps(params)
        self.wire.append("compact" if is_compact else "prompt")
        if is_compact:
            self._queue.put_nowait(self._chunk("compacting "))
            if self._compact_ends:
                self._queue.put_nowait(JsonRpcMessage(id=req_id, result={"stopReason": "end_turn"}))
            else:
                self._open = req_id
            return req_id
        # One turn at a time: a turn still running ends before this one starts.
        self._end_open_turn("end_turn")
        self._queue.put_nowait(self._chunk("pong"))
        self._queue.put_nowait(JsonRpcMessage(id=req_id, result={"stopReason": "end_turn"}))
        return req_id

    async def send_notification(self, method: str, params: dict) -> None:
        self.wire.append(method)
        if method == METHOD_CANCEL:
            held = self._hold_first_cancel and not self.cancel_seen.is_set()
            self.cancel_seen.set()
            if not held:
                self._end_open_turn("cancelled")


def _make(
    *, compact_ends: bool, hold_first_cancel: bool = False
) -> tuple[AcpSessionHandle, _Backend]:
    queue: asyncio.Queue = asyncio.Queue()
    backend = _Backend(queue, compact_ends=compact_ends, hold_first_cancel=hold_first_cancel)
    return AcpSessionHandle("sA", queue, backend), backend


async def _reply(handle: AcpSessionHandle) -> str:
    events = [ev async for ev in handle.prompt("hello", timeout=5.0)]
    return "".join(ev.text or "" for ev in events if ev.kind == EVENT_TEXT_CHUNK)


@pytest.mark.asyncio
async def test_a_compact_abandoned_by_its_timeout_is_cancelled_before_the_next_turn():
    handle, backend = _make(compact_ends=False)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(handle.compact(), timeout=0.2)
    reply = await _reply(handle)

    assert reply == "pong", f"the next turn's reply carried the abandoned turn's output: {reply!r}"
    assert backend.wire == ["compact", METHOD_CANCEL, "prompt"]


@pytest.mark.asyncio
async def test_a_compact_that_ends_in_time_sends_no_cancel():
    handle, backend = _make(compact_ends=True)

    await asyncio.wait_for(handle.compact(), timeout=5.0)
    reply = await _reply(handle)

    assert reply == "pong"
    assert backend.wire == ["compact", "prompt"]


@pytest.mark.asyncio
async def test_a_turn_cancelled_while_it_closes_the_abandoned_compact_leaves_the_fence():
    """The next turn's own caller gives up while that turn waits for the abandoned
    compact's answer. The turn after it must still close the compact first."""
    handle, backend = _make(compact_ends=False, hold_first_cancel=True)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(handle.compact(), timeout=0.2)
    closing = asyncio.ensure_future(_reply(handle))
    await asyncio.wait_for(backend.cancel_seen.wait(), timeout=5.0)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    reply = await asyncio.wait_for(_reply(handle), timeout=5.0)

    assert (
        reply == "pong"
    ), f"the abandoned compact's output reached a later turn: {reply!r}; wire={backend.wire}"
    assert backend.wire == ["compact", METHOD_CANCEL, METHOD_CANCEL, "prompt"]
