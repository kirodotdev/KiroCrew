"""The stale-turn clock has to cover the wait AFTER a tool finishes.

``_stale_eligible`` arms the stale branch of the dispatch loop's watchdog. It is
armed by a text chunk and cleared by a tool call -- correctly, because the tool
clock covers a call that is still in flight. Nothing re-armed it when the tool
finished, so the gap between a tool's last result and the model's next frame was
covered by no clock at all. A turn whose model never sent the follow-up then sat
outside every watchdog -- rows complete and no terminal event -- and parked the
slot with no probe and no log line to explain it.

The re-arm is gated on a TERMINAL status. A streamed ``tool_call_update`` carrying
content while the tool still runs also yields an event here, and arming the
model-wait clock on one would put a live tool under a watchdog whose probe ends
the turn and truncates its output.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import (
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_RESULT,
    METHOD_SESSION_UPDATE,
    JsonRpcMessage,
)

SESSION = "sA"


def _handle() -> AcpSessionHandle:
    rt = MagicMock()
    rt.pid = None
    rt.is_alive = MagicMock(return_value=True)
    rt.send_notification = AsyncMock()
    return AcpSessionHandle(SESSION, asyncio.Queue(), rt)


def _update_msg(update: dict) -> JsonRpcMessage:
    msg = JsonRpcMessage(
        method=METHOD_SESSION_UPDATE, params={"sessionId": SESSION, "update": update}
    )
    msg.fanout_no_owner = False
    return msg


def _tool_call(tool_call_id: str = "tc1") -> dict:
    return {
        "sessionUpdate": "tool_call",
        "toolCallId": tool_call_id,
        "title": "grep",
        "kind": "read",
        "rawInput": {"pattern": "x"},
    }


def _tool_result(tool_call_id: str = "tc1", status: str = "completed") -> dict:
    return {
        "sessionUpdate": "tool_call_update",
        "toolCallId": tool_call_id,
        "status": status,
        "content": [{"content": {"type": "text", "text": "ok"}}],
    }


async def _drive(handle: AcpSessionHandle, *frames: JsonRpcMessage) -> None:
    for frame in frames:
        handle._queue.put_nowait(frame)
    handle._queue.put_nowait(JsonRpcMessage(id=1, result={"stopReason": "end_turn"}))
    async for _ in handle._dispatch_events(req_id=1, timeout=5.0):
        pass


@pytest.mark.asyncio
async def test_a_completed_tool_re_arms_the_stale_clock() -> None:
    handle = _handle()
    await _drive(handle, _update_msg(_tool_call()), _update_msg(_tool_result()))
    assert handle._tool_dispatched is False
    assert handle._stale_eligible is True, "a finished tool leaves the turn waiting on the model"


@pytest.mark.asyncio
async def test_a_failed_tool_still_arms_the_stale_clock() -> None:
    """Terminal is not the same as completed: the turn waits either way."""
    handle = _handle()
    await _drive(handle, _update_msg(_tool_call()), _update_msg(_tool_result(status="failed")))
    assert handle._stale_eligible is True


@pytest.mark.asyncio
async def test_a_streamed_partial_result_does_not_arm_the_stale_clock() -> None:
    """An in-progress update is not yet the model's turn to speak."""
    handle = _handle()
    await _drive(handle, _update_msg(_tool_call()), _update_msg(_tool_result(status="in_progress")))
    assert handle._stale_eligible is False, "a still-writing tool must not meet the stale probe"


@pytest.mark.asyncio
async def test_a_dispatched_tool_still_disarms_the_stale_clock() -> None:
    """The re-arm must not let the stale clock judge a tool that is running."""
    handle = _handle()
    await _drive(handle, _update_msg(_tool_call()))
    assert handle._stale_eligible is False


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["a", "b"])
@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
async def test_overlapping_tools_keep_tool_watchdog_until_last_result(first, status):
    handle = _handle()
    second = "b" if first == "a" else "a"
    handle._handle_update(_update_msg(_tool_call("a")))
    handle._handle_update(_update_msg(_tool_call("b")))
    survivor = handle._active_tool_calls[second][0]
    handle._handle_update(_update_msg(_tool_result(first, status=status)))
    assert handle._tool_dispatched is True
    assert handle._stale_eligible is False
    assert handle._inflight_tool_call_id == second
    assert handle._inflight_tool is survivor

    handle._handle_update(_update_msg(_tool_result(second, status="in_progress")))
    handle._handle_update(
        _update_msg(
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "still working"},
            }
        )
    )
    assert handle._tool_dispatched is True
    assert handle._stale_eligible is False
    assert handle._inflight_tool is survivor

    handle._handle_update(_update_msg(_tool_result(second)))
    assert handle._tool_dispatched is False
    assert handle._stale_eligible is True
    assert handle._inflight_tool is None
    assert not handle._active_tool_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["a", "b"])
async def test_client_overlapping_tools_keep_tool_watchdog(monkeypatch, first):
    client = AcpClient()
    second = "b" if first == "a" else "a"

    async def prompt_loop(*args):
        yield "update", _update_msg(_tool_call("a"))
        yield "update", _update_msg(_tool_call("b"))
        yield "update", _update_msg(_tool_result(first))
        assert client._tool_dispatched is True
        assert client._stale_eligible is False
        assert client._active_tool_calls == {second}
        yield "update", _update_msg(_tool_result(second, status="in_progress"))
        yield "update", _update_msg(
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "still working"},
            }
        )
        assert client._tool_dispatched is True
        assert client._stale_eligible is False
        yield "update", _update_msg(_tool_result(second))
        assert client._tool_dispatched is False
        assert client._stale_eligible is True
        assert not client._active_tool_calls
        yield "complete", JsonRpcMessage(id=1, result={"stopReason": "end_turn"})

    monkeypatch.setattr(client, "_prompt_loop", prompt_loop)
    monkeypatch.setattr(client, "_read_new_tool_results_sync", lambda: [])
    async for _ in client._dispatch_events(req_id=1, timeout=5):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "trigger", ["text", "thinking", "tool", "complete", "refusal", "interrupted"]
)
async def test_client_jsonl_result_retires_call_at_every_flush(monkeypatch, tmp_path, trigger):
    client = AcpClient()
    client._session_id = SESSION
    monkeypatch.setattr("kiro_crew.acp.client.kiro_sessions_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.acp.client.error_is_refusal_terminal", lambda *_: True)
    monkeypatch.setattr(client, "_emit_tool_interrupted_sel", lambda *_: None)
    jsonl_path = tmp_path / f"{SESSION}.jsonl"

    def publish_result():
        jsonl_path.write_text(
            json.dumps(
                {
                    "kind": "ToolResults",
                    "data": {
                        "content": [
                            {
                                "kind": "toolResult",
                                "data": {
                                    "toolUseId": "a",
                                    "content": [{"kind": "text", "data": "ok"}],
                                },
                            }
                        ]
                    },
                }
            )
            + "\n"
        )

    async def prompt_loop(*args):
        yield "update", _update_msg(_tool_call("a"))
        assert client._active_tool_calls == {"a"}
        if trigger != "interrupted":
            publish_result()
        if trigger == "complete":
            yield "complete", JsonRpcMessage(id=1, result={"stopReason": "end_turn"})
        elif trigger == "refusal":
            yield "error", JsonRpcMessage(id=1, error={"code": -32603, "message": "refused"})
        elif trigger == "tool":
            yield "update", _update_msg(_tool_call("b"))
        else:
            yield "update", _update_msg(
                {
                    "sessionUpdate": (
                        "agent_thought_chunk" if trigger == "thinking" else "agent_message_chunk"
                    ),
                    "content": {
                        "type": "text",
                        "text": (
                            "Tool uses were interrupted, waiting for the next user prompt"
                            if trigger == "interrupted"
                            else "continuing"
                        ),
                    },
                }
            )
        yield "complete", JsonRpcMessage(id=1, result={"stopReason": "end_turn"})

    monkeypatch.setattr(client, "_prompt_loop", prompt_loop)
    results = []
    async for event in client._dispatch_events(req_id=1, timeout=5):
        if trigger == "interrupted" and event.kind == EVENT_TEXT_CHUNK:
            publish_result()
        if event.kind == EVENT_TOOL_RESULT:
            results.append(event)
            assert not event.tool_status
            assert client._active_tool_calls == ({"b"} if trigger == "tool" else set())
            assert client._tool_dispatched is (trigger == "tool")
            assert client._stale_eligible is (trigger != "tool")
    assert [event.tool_call_id for event in results] == ["a"]


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["a", "b"])
async def test_client_jsonl_results_keep_other_calls_active(monkeypatch, first):
    from kiro_crew.acp.types import AcpEvent

    client = AcpClient()
    second = "b" if first == "a" else "a"
    pending = []

    def read_results():
        results = pending[:]
        pending.clear()
        return results

    async def prompt_loop(*args):
        yield "update", _update_msg(_tool_call("a"))
        yield "update", _update_msg(_tool_call("b"))
        for completed, remaining in [(first, {second}), (second, set())]:
            pending.extend(
                AcpEvent(kind=EVENT_TOOL_RESULT, tool_call_id=call_id, tool_output="ok")
                for call_id in [completed, completed, "unrelated"]
            )
            yield "update", _update_msg(
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "continuing"},
                }
            )
            assert client._active_tool_calls == remaining
            assert client._tool_dispatched is bool(remaining)
            assert client._stale_eligible is (not remaining)
        yield "complete", JsonRpcMessage(id=1, result={"stopReason": "end_turn"})

    monkeypatch.setattr(client, "_prompt_loop", prompt_loop)
    monkeypatch.setattr(client, "_read_new_tool_results_sync", read_results)
    async for _ in client._dispatch_events(req_id=1, timeout=5):
        pass
