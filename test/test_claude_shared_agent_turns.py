"""A turn claude starts on its own, on a chat of the shared runtime.

The session's queue tells its handle about each frame (``AgentTurnQueue``), so a
turn the agent starts between the chat's prompts calls the chat back and is read
with ``stream_unsolicited``; a prompt that starts first reads it as its own.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from test_claude_shared_runtime import harness  # noqa: F401  (fixture, by name)
from test_claude_shared_runtime import _claude_runtime, _Plane

from kiro_crew.acp.session_handle_agent_turns import AgentTurnQueue
from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, JsonRpcMessage

#: Every session here starts through the claude harness with its adapter resolved
#: to a test value, as in ``test_claude_shared_runtime``.
pytestmark = pytest.mark.usefixtures("harness")


def _update(sid: str, update: dict[str, Any]) -> JsonRpcMessage:
    return JsonRpcMessage.from_dict(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": sid, "update": update},
        }
    )


def _chunk(sid: str, text: str) -> JsonRpcMessage:
    return _update(
        sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}
    )


def _autonomous_end(sid: str) -> JsonRpcMessage:
    """The usage_update closing a cycle Claude Code ran on its own."""
    return _update(
        sid,
        {
            "sessionUpdate": "usage_update",
            "used": 10,
            "size": 1000,
            "_meta": {"_claude/origin": {"kind": "task-notification"}},
        },
    )


def _session(tmp_path) -> tuple[Any, Any]:
    plane = _Plane({"sessionId": "s-1", "modes": {"currentModeId": "default"}})
    rt = _claude_runtime(plane)
    handle = asyncio.run(rt.create_session(cwd=str(tmp_path), agent="kirocrew"))
    return rt, handle


def test_a_turn_the_agent_starts_between_prompts_is_read_whole(tmp_path, monkeypatch):
    rt, handle = _session(tmp_path)
    queue = rt._session_queues["s-1"]

    async def no_send(*_a: Any, **_k: Any) -> int:
        raise AssertionError("an agent-started turn wrote a request")

    # Nothing is written to the agent for this turn.
    monkeypatch.setattr(rt, "send_request", no_send)
    assert isinstance(queue, AgentTurnQueue)
    calls: list[str] = []

    async def scenario() -> list[Any]:
        assert handle.watch_agent_turns(lambda: calls.append("turn")) is True
        # A frame that is no activity of the agent's starts nothing.
        queue.put_nowait(_update("s-1", {"sessionUpdate": "available_commands_update"}))
        await asyncio.sleep(0)
        assert calls == [] and not handle.agent_turn_pending()
        queue.put_nowait(_chunk("s-1", "the background job finished"))
        queue.put_nowait(_autonomous_end("s-1"))
        # The callback is scheduled off the reader, not called from it.
        assert calls == [] and handle.agent_turn_pending()
        await asyncio.sleep(0)
        assert calls == ["turn"]
        return [event async for event in handle.stream_unsolicited()]

    events = asyncio.run(scenario())
    assert [e.text for e in events if e.kind == EVENT_TEXT_CHUNK] == ["the background job finished"]
    assert events[-1].kind == EVENT_COMPLETE and events[-1].stop_reason == "end_turn"
    assert not handle.agent_turn_pending() and not handle.is_turn_active
    asyncio.run(rt.terminate_session("s-1"))


def test_a_prompt_that_starts_first_reads_the_agents_frames_as_its_own(tmp_path, monkeypatch):
    """As on the per-chat client: the pre-turn drain keeps a pending turn's frames."""
    rt, handle = _session(tmp_path)
    queue = rt._session_queues["s-1"]

    async def send_request(method: str, params: dict, **_: Any) -> int:
        queue.put_nowait(
            JsonRpcMessage.from_dict(
                {"jsonrpc": "2.0", "id": 7, "result": {"stopReason": "end_turn"}}
            )
        )
        return 7

    monkeypatch.setattr(rt, "send_request", send_request)

    async def scenario() -> list[Any]:
        handle.watch_agent_turns(lambda: None)
        queue.put_nowait(_chunk("s-1", "late reply"))
        assert handle.agent_turn_pending()
        return [event async for event in handle.prompt("next question")]

    events = asyncio.run(scenario())
    assert "late reply" in [e.text for e in events if e.kind == EVENT_TEXT_CHUNK]
    assert events[-1].kind == EVENT_COMPLETE
    assert not handle.agent_turn_pending()
    asyncio.run(rt.terminate_session("s-1"))


def test_a_second_cycle_queued_behind_the_first_is_handed_off_after_it(tmp_path, monkeypatch):
    """Two background replies queued at once: each is read as its own turn."""
    rt, handle = _session(tmp_path)
    queue = rt._session_queues["s-1"]

    async def no_send(*_a: Any, **_k: Any) -> int:
        raise AssertionError("an agent-started turn wrote a request")

    monkeypatch.setattr(rt, "send_request", no_send)
    calls: list[str] = []

    async def scenario() -> list[list[str]]:
        handle.watch_agent_turns(lambda: calls.append("turn"))
        for text in ("first job done", "second job done"):
            queue.put_nowait(_chunk("s-1", text))
            queue.put_nowait(_autonomous_end("s-1"))
        await asyncio.sleep(0)
        turns = []
        for _ in range(2):
            events = [e async for e in handle.stream_unsolicited()]
            turns.append([e.text for e in events if e.kind == EVENT_TEXT_CHUNK])
            await asyncio.sleep(0)
        return turns

    assert asyncio.run(scenario()) == [["first job done"], ["second job done"]]
    assert calls == ["turn", "turn"]
    assert not handle.agent_turn_pending()
    asyncio.run(rt.terminate_session("s-1"))


def test_a_frame_during_a_turn_starts_no_agent_turn(tmp_path):
    rt, handle = _session(tmp_path)
    queue = rt._session_queues["s-1"]
    calls: list[str] = []

    async def scenario() -> None:
        handle.watch_agent_turns(lambda: calls.append("turn"))
        handle._turn_done.clear()
        queue.put_nowait(_chunk("s-1", "this turn's own text"))
        await asyncio.sleep(0)
        handle._turn_done.set()

    asyncio.run(scenario())
    assert calls == [] and not handle.agent_turn_pending()
    asyncio.run(rt.terminate_session("s-1"))


def test_a_host_without_per_session_state_keeps_a_plain_queue():
    """harness-parity H13: kiro-cli's sessions never reach the agent-turn seams."""
    from kiro_crew.acp.runtime import AcpRuntime

    rt = AcpRuntime(work_dir="/tmp")
    assert rt._harness.per_session_state is False


def test_a_background_childs_permission_between_turns_opens_its_owners_turn(tmp_path):
    """An announced child's request after the owner's prompt ended reaches the owner.

    A watched owner reads the turns the agent starts, so the request goes to its
    queue and opens a turn there instead of being refused ``owner_no_active_turn``.
    """
    rt, handle = _session(tmp_path)
    answered: list[str] = []

    async def answer(_msg: Any, _sid: str, reason: str = "") -> None:
        answered.append(reason)

    rt._spawn_answer_task = answer
    calls: list[str] = []
    request = JsonRpcMessage.from_dict(
        {
            "jsonrpc": "2.0",
            "id": 41,
            "method": "session/request_permission",
            "params": {"sessionId": "child-1", "options": [], "toolCall": {"toolCallId": "t1"}},
        }
    )

    async def scenario() -> list[JsonRpcMessage]:
        handle.watch_agent_turns(lambda: calls.append("turn"))
        await rt._route_owned_child_frame(request, "child-1", "s-1")
        await rt._route_owned_child_frame(_chunk("child-1", "child working"), "child-1", "s-1")
        await asyncio.sleep(0)
        queue = rt._session_queues["s-1"]
        return [queue.get_nowait() for _ in range(queue.qsize())]

    queued = asyncio.run(scenario())
    assert answered == []
    assert [m.id for m in queued] == [41, None]
    assert calls == ["turn"] and handle.agent_turn_pending()
    asyncio.run(rt.terminate_session("s-1"))


def test_an_unwatched_owner_still_refuses_a_childs_permission_between_turns(tmp_path):
    rt, handle = _session(tmp_path)
    answered: list[str] = []

    async def answer(_msg: Any, _sid: str, reason: str = "") -> None:
        answered.append(reason)

    rt._spawn_answer_task = answer
    request = JsonRpcMessage.from_dict(
        {
            "jsonrpc": "2.0",
            "id": 42,
            "method": "session/request_permission",
            "params": {"sessionId": "child-1", "options": []},
        }
    )
    asyncio.run(rt._route_owned_child_frame(request, "child-1", "s-1"))
    assert answered == ["owner_no_active_turn"]
    assert rt._session_queues["s-1"].qsize() == 0
    asyncio.run(rt.terminate_session("s-1"))
