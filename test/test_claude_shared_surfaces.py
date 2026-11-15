"""The claude-agent-acp surfaces two chats on one shared process each see as their own.

Frames go through the runtime's own reader (stdout -> per-session queue), and each
chat's turn reads its queue, so a surface of chat A reaching chat B would show here.
Each surface: chat A's frame makes A's event, chat B's turn sees none of it. The
notice, mode change, checklist, Read preview and compaction summary are read by
``session_handle_harness_surfaces``; thinking and the Fast mode switch come from the
shared parser and the handle's config options, routed by the runtime per session.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from test_claude_shared_runtime import harness  # noqa: F401  (fixture, by name)
from test_claude_shared_runtime import _claude_runtime, _Plane

from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.types import (
    EVENT_COMPACTION_STATUS,
    EVENT_HARNESS_NOTICE,
    EVENT_TEXT_CHUNK,
    EVENT_THINKING_CHUNK,
    EVENT_TODO_UPDATE,
    EVENT_TOOL_CALL,
)
from kiro_crew.agent_sdk.backends import FAST_MODE_CONFIG_OPTION_ID
from kiro_crew.providers import acp_model_effort

pytestmark = pytest.mark.usefixtures("harness")

#: The longest a test waits for a turn it started; past it the test fails.
_TURN_DEADLINE_SECS = 30.0


class _TwoChatPlane(_Plane):
    """``session/new`` answers a fresh id per start, as one shared adapter does."""

    def __init__(self) -> None:
        super().__init__({})
        self._ids = iter(("s-a", "s-b"))

    async def send_and_await(self, method: str, params: dict, timeout: Any = None) -> dict:
        if method == "session/new":
            self.sent.append((method, params))
            return {
                "sessionId": next(self._ids),
                "modes": {
                    "currentModeId": "default",
                    "availableModes": [
                        {"id": "default", "name": "Default"},
                        {"id": "plan", "name": "Plan Mode"},
                    ],
                },
                "configOptions": [_fast_option("off")],
            }
        return await super().send_and_await(method, params, timeout)


def _fast_option(value: str) -> dict[str, Any]:
    """claude-agent-acp's Fast mode switch, an ``on``/``off`` select."""
    return {
        "id": FAST_MODE_CONFIG_OPTION_ID,
        "type": "select",
        "currentValue": value,
        "options": [{"value": "on", "name": "On"}, {"value": "off", "name": "Off"}],
    }


def _update(sid: str, update: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {"sessionId": sid, "update": update},
    }


def _turn_frames(sid: str, req_id: int, *updates: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        *(_update(sid, u) for u in updates),
        {"jsonrpc": "2.0", "id": req_id, "result": {"stopReason": "end_turn"}},
    ]


async def _two_chats(tmp_path) -> tuple[Any, Any, Any, asyncio.StreamReader]:
    """Two claude chats on one runtime whose stdout the test writes."""
    plane = _TwoChatPlane()
    rt = _claude_runtime(plane)
    stdout = asyncio.StreamReader()
    rt._process.stdout = stdout
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    chat_a = await rt.create_session(cwd=str(tmp_path / "a"), agent="kirocrew")
    chat_b = await rt.create_session(cwd=str(tmp_path / "b"), agent="kirocrew")
    rt._reader_task = asyncio.ensure_future(rt._reader_loop())
    return rt, chat_a, chat_b, stdout


async def _turn(rt: Any, handle: Any, stdout: asyncio.StreamReader, frames: list) -> list[Any]:
    """Run one prompt on *handle*; the adapter answers with *frames* on stdout."""
    req_ids = iter(range(100, 200))

    async def send_request(method: str, params: dict, **_: Any) -> int:
        req_id = next(req_ids)
        rt._routed_requests[req_id] = handle.session_id
        for frame in frames:
            if frame.get("id") == "REQ":
                frame = {**frame, "id": req_id}
            stdout.feed_data((json.dumps(frame) + "\n").encode())
        return req_id

    rt.send_request = send_request  # type: ignore[method-assign]

    async def read() -> list[Any]:
        return [event async for event in handle.prompt("go")]

    return await asyncio.wait_for(read(), _TURN_DEADLINE_SECS)


def _answer(*updates_by_sid: tuple[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """The frames of one turn: each ``(sid, update)``, then the prompt's response."""
    return [
        *(_update(sid, u) for sid, u in updates_by_sid),
        {"jsonrpc": "2.0", "id": "REQ", "result": {"stopReason": "end_turn"}},
    ]


def _run(tmp_path, scenario):
    async def main():
        rt, chat_a, chat_b, stdout = await _two_chats(tmp_path)
        try:
            return await scenario(rt, chat_a, chat_b, stdout)
        finally:
            rt._reader_task.cancel()

    return asyncio.run(main())


def test_an_adapter_notice_reaches_only_its_own_chat(tmp_path):
    notice = {"sessionUpdate": "notice", "title": "Rate limited", "description": "retrying"}

    async def scenario(rt, chat_a, chat_b, stdout):
        events_a = await _turn(rt, chat_a, stdout, _answer(("s-a", notice)))
        events_b = await _turn(rt, chat_b, stdout, _answer(("s-a", notice)))
        return events_a, events_b

    events_a, events_b = _run(tmp_path, scenario)
    notices_a = [e for e in events_a if e.kind == EVENT_TEXT_CHUNK and e.harness_notice]
    assert len(notices_a) == 1 and "Rate limited" in notices_a[0].harness_notice.title
    # Chat A's frame arrives while chat B reads: the reader routes it to A, not B.
    assert not [e for e in events_b if getattr(e, "harness_notice", None)]


def test_a_mode_change_notice_and_a_checklist_stay_with_their_chat(tmp_path):
    mode = {"sessionUpdate": "current_mode_update", "currentModeId": "plan"}
    plan = {
        "sessionUpdate": "plan",
        "entries": [{"content": "write the test", "status": "in_progress", "priority": "high"}],
    }

    async def scenario(rt, chat_a, chat_b, stdout):
        events_a = await _turn(rt, chat_a, stdout, _answer(("s-a", mode), ("s-a", plan)))
        events_b = await _turn(rt, chat_b, stdout, _answer(("s-a", mode), ("s-a", plan)))
        return events_a, events_b

    events_a, events_b = _run(tmp_path, scenario)
    modes_a = [e for e in events_a if e.kind == EVENT_HARNESS_NOTICE]
    assert modes_a and modes_a[0].harness_notice.mode_name == "Plan Mode"
    assert [e for e in events_a if e.kind == EVENT_TODO_UPDATE]
    assert not [e for e in events_b if e.kind in (EVENT_HARNESS_NOTICE, EVENT_TODO_UPDATE)]


def test_a_read_call_names_its_preview_argument_in_its_own_chat(tmp_path):
    read = {
        "sessionUpdate": "tool_call",
        "toolCallId": "t-1",
        "title": "Read image.png",
        "kind": "read",
        "status": "pending",
        "rawInput": {"file_path": "/tmp/image.png"},
    }

    async def scenario(rt, chat_a, chat_b, stdout):
        return await _turn(rt, chat_a, stdout, _answer(("s-a", read)))

    events = _run(tmp_path, scenario)
    calls = [e for e in events if e.kind == EVENT_TOOL_CALL and e.tool_call_id == "t-1"]
    assert calls and calls[0].read_path_arg


def test_a_compaction_summary_folds_into_its_own_chats_card(tmp_path):
    started = {"sessionUpdate": "compaction_update", "compactionId": "c-1", "status": "in_progress"}
    chunk = {
        "sessionUpdate": "compaction_summary_chunk",
        "compactionId": "c-1",
        "content": {"type": "text", "text": "Summary of chat A."},
    }
    done = {"sessionUpdate": "compaction_update", "compactionId": "c-1", "status": "completed"}

    async def scenario(rt, chat_a, chat_b, stdout):
        events_a = await _turn(
            rt, chat_a, stdout, _answer(("s-a", started), ("s-a", chunk), ("s-a", done))
        )
        events_b = await _turn(rt, chat_b, stdout, _answer())
        return events_a, events_b

    events_a, events_b = _run(tmp_path, scenario)
    completed = [e for e in events_a if e.kind == EVENT_COMPACTION_STATUS and e.text == "completed"]
    assert completed and "Summary of chat A." in completed[0].title
    # The streamed summary is no text of the turn.
    assert not [e for e in events_a if e.kind == EVENT_TEXT_CHUNK and "Summary" in (e.text or "")]
    assert not [e for e in events_b if e.kind == EVENT_COMPACTION_STATUS]


def test_a_thought_reaches_only_its_own_chat_as_thinking(tmp_path):
    thought = {
        "sessionUpdate": "agent_thought_chunk",
        "content": {"type": "text", "text": "chat A thinks"},
    }

    async def scenario(rt, chat_a, chat_b, stdout):
        events_a = await _turn(rt, chat_a, stdout, _answer(("s-a", thought)))
        events_b = await _turn(rt, chat_b, stdout, _answer(("s-a", thought)))
        return events_a, events_b

    events_a, events_b = _run(tmp_path, scenario)
    thinking_a = [e for e in events_a if e.kind == EVENT_THINKING_CHUNK]
    assert [e.text for e in thinking_a] == ["chat A thinks"]
    assert not [e for e in events_a if e.kind == EVENT_TEXT_CHUNK and "thinks" in (e.text or "")]
    assert not [e for e in events_b if "thinks" in (e.text or "")]


class _ChatFront:
    """The dashboard provider's Fast mode reader over one chat's session provider."""

    fast_mode_enabled = acp_model_effort.fast_mode_enabled

    def __init__(self, client: AcpSessionProvider) -> None:
        self._client = client


def test_a_fast_mode_switch_reads_per_chat(tmp_path):
    turned_on = {"sessionUpdate": "config_option_update", "configOptions": [_fast_option("on")]}

    async def scenario(rt, chat_a, chat_b, stdout):
        front_a = _ChatFront(AcpSessionProvider(chat_a, rt, session_key="a"))
        front_b = _ChatFront(AcpSessionProvider(chat_b, rt, session_key="b"))
        before = (front_a.fast_mode_enabled(), front_b.fast_mode_enabled())
        await _turn(rt, chat_a, stdout, _answer(("s-a", turned_on)))
        return before, (front_a.fast_mode_enabled(), front_b.fast_mode_enabled())

    before, after = _run(tmp_path, scenario)
    assert before == (False, False)
    assert after == (True, False)
