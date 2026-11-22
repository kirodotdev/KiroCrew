"""opencode's ``task`` subagent becomes its own Activity card on the opencode backend.

opencode sends nothing on the child's own session: the subagent is only the parent's
``task`` tool row. ``kiro_crew.acp.client_native_children._opencode_task_events``
reads that row into the roster and activity events the native cards read, in the
shape a Claude Code child gets, while the row still renders as the parent's call.

The capture is ``test/fixtures/opencode_native_subagents/task-1.19.1.jsonl``,
``opencode acp`` 1.19.1 running one ``task`` call against a stub model.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from turn_harness import REJECTED, Do, ScriptedProvider, TurnScript, run_turn

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_SUBAGENT_ACTIVITY,
    EVENT_SUBAGENT_LIST,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    STOP_REASON_END_TURN,
    AcpEvent,
    JsonRpcMessage,
)
from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_OPENCODE
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard.chat_runner import _native_subagent_sync
from kiro_crew.hook_runtime.hook_types import HooksConfig
from kiro_crew.hooks import HookManager
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

CAPTURE = Path(__file__).parent / "fixtures" / "opencode_native_subagents" / "task-1.19.1.jsonl"
PARENT = "parent-0001"
TASK_CALL = "call_0_0"


def _capture() -> tuple[dict, list[dict]]:
    """The capture's header and every frame after it."""
    lines = [json.loads(line) for line in CAPTURE.read_text(encoding="utf-8").splitlines()]
    return lines[0]["_meta"], lines[1:]


def _updates() -> list[JsonRpcMessage]:
    """The capture's ``session/update`` frames, the ones a turn reads as updates."""
    _, frames = _capture()
    return [
        JsonRpcMessage.from_dict(frame)
        for frame in frames
        if frame.get("method") == "session/update"
    ]


def _client(backend: str = ACP_BACKEND_OPENCODE) -> AcpClient:
    client = AcpClient(acp_backend=backend)
    client._session_id = PARENT
    return client


async def _turn(client: AcpClient, messages: list[JsonRpcMessage]) -> list[AcpEvent]:
    """Run one ``_dispatch_events`` turn over *messages*, then the prompt's answer."""
    frames = [("update", msg) for msg in messages]
    frames.append(("complete", JsonRpcMessage(id=1, result={"stopReason": "end_turn"})))

    async def _fake_loop(req_id, timeout):
        for frame in frames:
            yield frame

    client._prompt_loop = _fake_loop  # type: ignore[assignment]
    return [
        event async for event in client._dispatch_events(req_id=1, timeout=FALLBACK_TIMEOUT_SECS)
    ]


def _rosters(events: list[AcpEvent]) -> list[list[dict]]:
    return [event.subagents or [] for event in events if event.kind == EVENT_SUBAGENT_LIST]


def _failed_end() -> JsonRpcMessage:
    return JsonRpcMessage(
        method="session/update",
        params={
            "sessionId": PARENT,
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": TASK_CALL,
                "status": "failed",
                "content": [{"type": "content", "content": {"type": "text", "text": "boom"}}],
            },
        },
    )


def test_the_capture_is_one_live_task_row_on_the_parent() -> None:
    meta, frames = _capture()
    assert meta["backend"] == ACP_BACKEND_OPENCODE and meta["recorded"] == "live"
    assert len(frames) < 50
    sessions = {frame["params"]["sessionId"] for frame in frames if "params" in frame}
    assert sessions == {PARENT}


@pytest.mark.asyncio
async def test_the_task_row_lists_its_child_running_then_completed() -> None:
    events = await _turn(_client(), _updates())
    rosters = _rosters(events)
    assert rosters[0] == [
        {
            "sessionId": TASK_CALL,
            "sessionName": "probe child",
            "agentName": "general",
            "initialQuery": "probe child",
            "status": {"type": "working", "message": ""},
        }
    ]
    assert rosters[-1][0]["status"] == {"type": "completed", "message": ""}
    assert events[-1].kind == EVENT_COMPLETE


@pytest.mark.asyncio
async def test_the_task_result_is_the_cards_text_and_the_row_stays_the_parents() -> None:
    events = await _turn(_client(), _updates())
    child = [e for e in events if e.kind == EVENT_SUBAGENT_ACTIVITY]
    assert [(e.sub_session_id, e.text, e.tool_call_id) for e in child] == [
        (TASK_CALL, "STUB-OK", "")
    ]
    assert TASK_CALL in {e.tool_call_id for e in events if e.kind == EVENT_TOOL_CALL}
    assert "".join(e.text or "" for e in events if e.kind == EVENT_TEXT_CHUNK) == "STUB-OK"


@pytest.mark.asyncio
async def test_the_rosters_draw_one_card() -> None:
    events = await _turn(_client(), _updates())
    state = MagicMock()
    slot = MagicMock()
    slot._native_subagent_tracker = {}
    slot._native_subagent_output = {}
    tracker: dict = {}
    for roster in _rosters(events):
        _native_subagent_sync(state, slot, roster, tracker)
    sent = [(c.args[0], c.args[1]) for c in state.broadcast_ws.call_args_list]
    assert [kind for kind, _ in sent] == ["subagent_spawn", "subagent_done"]
    assert sent[0][1]["id"] == f"native:{TASK_CALL}" and sent[0][1]["task"] == "probe child"
    assert sent[1][1]["error"] is None


@pytest.mark.asyncio
async def test_a_failed_task_row_closes_its_card_failed() -> None:
    events = await _turn(_client(), _updates()[:3] + [_failed_end()])
    assert _rosters(events)[-1][0]["status"] == {"type": "failed", "message": "boom"}


@pytest.mark.asyncio
async def test_the_next_turn_lists_only_its_own_children() -> None:
    client = _client()
    await _turn(client, _updates())
    assert not _rosters(await _turn(client, _updates()[-1:]))


@pytest.mark.asyncio
async def test_another_backend_reads_the_task_row_as_before() -> None:
    events = await _turn(_client(ACP_BACKEND_CLAUDE), _updates())
    assert not _rosters(events)
    assert not [e for e in events if e.kind == EVENT_SUBAGENT_ACTIVITY]


class _OpencodeProvider(ScriptedProvider):
    """A scripted provider whose capabilities are opencode's."""

    capabilities = capabilities_for(ACP_BACKEND_OPENCODE)


def _hook_on(ctx) -> None:
    ctx.state.context_builder = SimpleNamespace(
        hooks=HookManager(HooksConfig(auto_approve_subagent_tools=True))
    )


def _hook_off(ctx) -> None:
    ctx.state.context_builder = None


@pytest.mark.asyncio
async def test_a_running_task_card_leaves_the_parents_request_on_a_card() -> None:
    """An opencode card is drawn from the parent's own row, and its requests name no
    child, so with the subagent hook on the parent's next request still asks."""
    running = AcpEvent(
        kind=EVENT_SUBAGENT_LIST,
        subagents=[
            {
                "sessionId": TASK_CALL,
                "agentName": "general",
                "initialQuery": "probe child",
                "status": {"type": "working", "message": ""},
            }
        ],
    )
    parent_request = AcpEvent(
        kind=EVENT_PERMISSION_REQUEST,
        request_id="r1",
        title="touch out.txt",
        tool_name="bash",
        tool_kind="execute",
        is_shell=True,
        tool_input=json.dumps({"command": "touch out.txt"}),
    )
    done = AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
    record = await run_turn(
        TurnScript(
            events=[Do(_hook_on), running, parent_request, Do(_hook_off), done],
            answers={"r1": REJECTED},
            provider=_OpencodeProvider,
        )
    )
    assert len(record.approval_cards) == 1
    assert record.calls("approve_tool") == []
    assert record.audits(request_id="r1", outcome="auto_approved") == []
