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
from test_claude_shared_runtime import _SIGNAL_DEADLINE_SECS, _claude_runtime, _Plane

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
        return await _collect(handle.stream_unsolicited())

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
        return await _collect(handle.prompt("next question"))

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
            events = await _collect(handle.stream_unsolicited())
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


def test_a_watched_owner_whose_prompt_was_abandoned_refuses_a_childs_permission(tmp_path):
    """A watched queue whose prompt's reader left early opens no turn until the next
    prompt, so a child's request is answered fail-closed rather than left unread."""
    rt, handle = _session(tmp_path)
    answered: list[str] = []

    async def answer(_msg: Any, _sid: str, reason: str = "") -> None:
        answered.append(reason)

    rt._spawn_answer_task = answer
    request = JsonRpcMessage.from_dict(
        {
            "jsonrpc": "2.0",
            "id": 43,
            "method": "session/request_permission",
            "params": {"sessionId": "child-1", "options": []},
        }
    )

    async def scenario() -> None:
        handle.watch_agent_turns(lambda: None)
        handle._abandoned_prompt_id = 7
        await rt._route_owned_child_frame(request, "child-1", "s-1")

    asyncio.run(scenario())
    assert answered == ["owner_no_active_turn"]
    assert rt._session_queues["s-1"].qsize() == 0
    asyncio.run(rt.terminate_session("s-1"))


def test_an_abandoned_prompts_tail_starts_no_agent_turn(tmp_path):
    """While a prompt's reader left before its terminal, its late frames are that
    prompt's tail, which the next turn's drain discards: nothing is marked."""
    rt, handle = _session(tmp_path)
    queue = rt._session_queues["s-1"]
    calls: list[str] = []

    async def scenario() -> None:
        handle.watch_agent_turns(lambda: calls.append("turn"))
        handle._abandoned_prompt_id = 7
        queue.put_nowait(_chunk("s-1", "the cut-off prompt's tail"))
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert calls == [] and not handle.agent_turn_pending()
    asyncio.run(rt.terminate_session("s-1"))


def test_a_late_prompt_response_ends_an_agent_started_turn(tmp_path, monkeypatch):
    """A response no one waits on, read inside a turn the agent started, is a
    prompt's late answer: the backend finished, so that turn ends on it."""
    rt, handle = _session(tmp_path)
    queue = rt._session_queues["s-1"]

    async def no_send(*_a: Any, **_k: Any) -> int:
        raise AssertionError("an agent-started turn wrote a request")

    monkeypatch.setattr(rt, "send_request", no_send)

    async def scenario() -> list[Any]:
        handle.watch_agent_turns(lambda: None)
        queue.put_nowait(_chunk("s-1", "late text"))
        queue.put_nowait(
            JsonRpcMessage.from_dict(
                {"jsonrpc": "2.0", "id": 9, "result": {"stopReason": "end_turn"}}
            )
        )
        return await _collect(handle.stream_unsolicited())

    events = asyncio.run(scenario())
    assert [e.text for e in events if e.kind == EVENT_TEXT_CHUNK] == ["late text"]
    assert events[-1].kind == EVENT_COMPLETE and events[-1].stop_reason == "end_turn"
    assert not handle.is_turn_active
    asyncio.run(rt.terminate_session("s-1"))


async def _collect(stream: Any) -> list[Any]:
    """Every event *stream* yields, within the test's own deadline."""

    async def _all() -> list[Any]:
        return [event async for event in stream]

    return await asyncio.wait_for(_all(), _SIGNAL_DEADLINE_SECS)


def test_a_reply_queued_behind_an_awaited_response_is_handed_off_after_it(tmp_path):
    """A control request (a model change) awaits its response while Claude Code's
    background reply queues behind it: once nothing waits, that reply is seen as
    the turn the agent started, not left for the next prompt to discard."""
    rt, handle = _session(tmp_path)
    queue = rt._session_queues["s-1"]
    calls: list[str] = []

    async def scenario() -> Any:
        called = asyncio.Event()

        def on_turn() -> None:
            calls.append("turn")
            called.set()

        handle.watch_agent_turns(on_turn)
        handle._awaited_responses.add(5)
        queue.put_nowait(JsonRpcMessage.from_dict({"jsonrpc": "2.0", "id": 5, "result": {}}))
        queue.put_nowait(_chunk("s-1", "the background job finished"))
        queue.put_nowait(_autonomous_end("s-1"))
        handle._awaited_responses.discard(5)
        answer = await handle._wait_for_response(5, _SIGNAL_DEADLINE_SECS)
        await asyncio.wait_for(called.wait(), _SIGNAL_DEADLINE_SECS)
        return answer

    answer = asyncio.run(scenario())
    assert answer.id == 5
    assert handle.agent_turn_pending() and calls == ["turn"]
    asyncio.run(rt.terminate_session("s-1"))


def test_a_prompt_error_naming_only_background_calls_completes_the_turn(tmp_path, monkeypatch):
    """As on the per-chat client: an ``incomplete_tool_call`` error whose ids are all
    background agents' calls ends the turn as the finished turn it is."""
    from kiro_crew.acp import claude_background_calls

    rt, handle = _session(tmp_path)
    queue = rt._session_queues["s-1"]
    ids = ("toolu_bg1",)
    monkeypatch.setattr(
        claude_background_calls,
        "background_agent_tool_calls",
        lambda _sid, _cwd, wanted: frozenset(wanted),
    )

    async def send_request(method: str, params: dict, **_: Any) -> int:
        queue.put_nowait(_chunk("s-1", "the answer"))
        queue.put_nowait(
            JsonRpcMessage.from_dict(
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "error": {
                        "code": -32603,
                        "message": "Internal error: Claude ended the turn without returning "
                        "results for tool calls: " + ", ".join(ids),
                        "data": {"errorKind": "incomplete_tool_call"},
                    },
                }
            )
        )
        return 7

    monkeypatch.setattr(rt, "send_request", send_request)

    async def scenario() -> list[Any]:
        return await _collect(handle.prompt("question"))

    events = asyncio.run(scenario())
    assert events[-1].kind == EVENT_COMPLETE and events[-1].stop_reason == "end_turn"
    asyncio.run(rt.terminate_session("s-1"))


def test_a_reply_held_by_a_request_wait_reaches_the_dashboard_as_its_own_turn(
    tmp_path, monkeypatch
):
    """The issue's steps through the dashboard's slot claim: the agent's reply is
    handed off, a request wait (a live model change) holds its frames, and the user
    sends a follow-up before the wait's answer arrives. The turn opened for the
    reply waits for the wait and reads the reply whole, and the follow-up, which
    queues behind that turn, gets only its own answer."""
    from unittest.mock import MagicMock, patch

    from dashboard_approval_helpers import _make_slot, _make_state

    from kiro_crew.dashboard import chat_runner
    from kiro_crew.testing.wait import async_wait_until

    state, _client = _make_state(tmp_path)
    slot = _make_slot()
    state._slots[slot.key] = slot
    turns: list[list[str]] = []
    queued: list[str] = []
    sent: list[str] = []
    answers: list[asyncio.Task[None]] = []

    def texts(events: list[Any]) -> list[str]:
        return [e.text for e in events if e.kind == EVENT_TEXT_CHUNK]

    async def scenario() -> None:
        plane = _Plane({"sessionId": "s-1", "modes": {"currentModeId": "default"}})
        rt = _claude_runtime(plane)
        handle = await rt.create_session(cwd=str(tmp_path), agent="kirocrew")
        queue = rt._session_queues["s-1"]
        state.sessions.get_provider = MagicMock(return_value=handle)

        async def answer_follow_up() -> None:
            # The model change is answered first.
            await wait
            queue.put_nowait(_chunk("s-1", "the follow-up's answer"))
            queue.put_nowait(
                JsonRpcMessage.from_dict(
                    {"jsonrpc": "2.0", "id": 7, "result": {"stopReason": "end_turn"}}
                )
            )

        async def send_request(method: str, params: dict, **_: Any) -> int:
            sent.append(method)
            answers.append(asyncio.get_running_loop().create_task(answer_follow_up()))
            return 7

        monkeypatch.setattr(rt, "send_request", send_request)

        async def run_chat(*_a: Any, **_k: Any) -> None:
            turns.append(texts(await _collect(handle.stream_unsolicited(_SIGNAL_DEADLINE_SECS))))

        async def follow_up() -> None:
            # A message sent while a turn holds the slot queues behind it.
            held = slot.task
            if held is not None and not held.done():
                queued.append("next question")
                await held
            turns.append(texts(await _collect(handle.prompt("next question"))))

        def spawn(_state: Any, _slot: Any, coro: Any) -> Any:
            return asyncio.get_running_loop().create_task(coro)

        with (
            patch.object(chat_runner, "_run_chat", run_chat),
            patch.object(chat_runner, "spawn_guarded_turn", spawn),
        ):
            handle.watch_agent_turns(lambda: chat_runner._on_agent_turn(state, slot, handle))
            queue.put_nowait(_chunk("s-1", "printed probe-done"))
            queue.put_nowait(_autonomous_end("s-1"))
            wait = asyncio.create_task(handle._wait_for_response(99, _SIGNAL_DEADLINE_SECS))
            # The wait holds the reply's frames and the hand-off has opened a turn.
            await async_wait_until(lambda: queue.empty() and slot.task is not None)
            asked = asyncio.create_task(follow_up())
            await async_wait_until(lambda: queued or sent)
            queue.put_nowait(JsonRpcMessage.from_dict({"jsonrpc": "2.0", "id": 99, "result": {}}))
            await wait
            await asked
            await asyncio.gather(*answers)
        assert not handle.agent_turn_pending()
        await rt.terminate_session("s-1")

    asyncio.run(scenario())
    assert turns == [["printed probe-done"], ["the follow-up's answer"]]
