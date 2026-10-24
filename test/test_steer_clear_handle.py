"""kiro-cli's ``_session/steer/clear`` on the shared runtime (``ACP_BACKENDS_STEER_RETRACT``).

``AcpSessionHandle.clear_pending_steers`` writes the request and reads its answer off
the session queue, through the real reader loop. The answer is ``{cleared: true}``
whether or not anything was pending (``kiro/steering-clear-after-inject-live.jsonl``),
so the evidence of a drop is the ``AgentExecutionUserMessageCleared`` that follows
it, which the turn yields as ``EVENT_STEER_DROPPED``. Frames are shaped like the two
``kiro/steering-clear*-live.jsonl`` captures.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest
from acp_runtime_helpers import (
    _await_routed,
    _bounded,
    _feed,
    _make_runtime,
    _register,
    _start_reader,
    _stop_reader,
)

from kiro_crew.acp._dispatch import classify_notification
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import (
    EVENT_STEER_DROPPED,
    METHOD_KIRO_SESSION_UPDATE,
    METHOD_SESSION_STEER_CLEAR,
    JsonRpcMessage,
)
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.testing.wait import async_wait_until, await_until

SID = "kiro-steer-clear-session-1"


def _written(proc) -> list[dict]:
    return [json.loads(c.args[0].decode()) for c in proc.stdin.write.call_args_list]


def _cleared(*ids: str) -> dict:
    return {
        "method": METHOD_KIRO_SESSION_UPDATE,
        "params": {
            "sessionId": SID,
            "update": {
                "sessionUpdate": "AgentExecutionUserMessageCleared",
                "messageIds": list(ids),
            },
        },
    }


async def _clear(backend: str, answer: dict | None):
    """Run ``clear_pending_steers`` against a runtime whose reader feeds *answer*."""
    rt, reader, proc = _make_runtime()
    rt._acp_backend = backend
    queues = _register(rt, SID)
    handle = AcpSessionHandle(SID, queues[SID], rt)
    task = await _start_reader(rt)
    try:
        call = asyncio.ensure_future(handle.clear_pending_steers())
        if answer is not None:
            await async_wait_until(lambda: proc.stdin.write.called or call.done())
            if proc.stdin.write.called:
                (sent,) = _written(proc)
                _feed(reader, {"id": sent["id"], **answer})
        result = await _bounded(call, what="the clear's answer")
    finally:
        await _stop_reader(task)
    return result, handle, proc


@pytest.mark.asyncio
async def test_the_clear_is_written_for_the_session_and_its_answer_read():
    result, handle, proc = await _clear(ACP_BACKEND_KIRO, {"result": {"cleared": True}})

    assert result is True
    (sent,) = _written(proc)
    assert sent["method"] == METHOD_SESSION_STEER_CLEAR
    assert sent["params"] == {"sessionId": SID}
    assert handle.supports_steer_retract is True


@pytest.mark.asyncio
async def test_a_refused_clear_stops_the_offer():
    result, handle, _ = await _clear(
        ACP_BACKEND_KIRO, {"error": {"code": -32601, "message": "Method not found"}}
    )

    assert result is False
    assert handle.supports_steer_retract is False


@pytest.mark.asyncio
async def test_a_refused_handle_sends_nothing_again():
    _, handle, proc = await _clear(
        ACP_BACKEND_KIRO, {"error": {"code": -32601, "message": "Method not found"}}
    )
    proc.stdin.write.reset_mock()

    assert await handle.clear_pending_steers() is False
    proc.stdin.write.assert_not_called()


@pytest.mark.asyncio
async def test_no_answer_in_time_is_unknown_and_forgotten():
    with patch("kiro_crew.acp.session_handle_steering.STEER_CLEAR_ANSWER_WAIT_SECS", 0.0):
        result, handle, proc = await _clear(ACP_BACKEND_KIRO, None)

    assert result is None
    assert proc.stdin.write.called
    assert handle._runtime._pending_requests == {}


@pytest.mark.asyncio
async def test_kas_is_not_sent_the_clear():
    result, handle, proc = await _clear(ACP_BACKEND_KAS, None)

    assert result is False
    assert handle.supports_steer_retract is False
    proc.stdin.write.assert_not_called()


def test_the_cleared_notification_is_a_steer_frame():
    msg = JsonRpcMessage(method=_cleared()["method"], params=_cleared("steer-1")["params"])

    assert classify_notification(msg) == "steer"


@pytest.mark.asyncio
async def test_the_turn_reports_the_drop():
    rt, reader, _ = _make_runtime()
    rt._acp_backend = ACP_BACKEND_KIRO
    queues = _register(rt, SID)
    handle = AcpSessionHandle(SID, queues[SID], rt)
    task = await _start_reader(rt)
    events: list = []
    driver = None
    try:

        async def drive():
            async for ev in handle.prompt("run the command", timeout=600.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, SID))[SID]
        _feed(reader, _cleared("steer-1", "steer-2"))
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the turn ending")
    finally:
        if driver is not None and not driver.done():
            driver.cancel()
            await asyncio.gather(driver, return_exceptions=True)
        await _stop_reader(task)

    dropped = [e for e in events if e.kind == EVENT_STEER_DROPPED]
    assert len(dropped) == 1
    assert dropped[0].runtime_global is False
