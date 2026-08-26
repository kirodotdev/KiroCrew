"""Tests for AcpSessionHandle.stream_command — native slash-command execution.

The dashboard's shared-runtime sessions previously routed slash commands
through session/prompt (a full LLM turn that *summarized* kiro-cli's output).
stream_command sends ``_kiro.dev/commands/execute`` with the TuiCommand OBJECT
form (``{command, args}`` — kiro-cli 2.14.0 returns no response on the string
form) and drains session/update events with prompt()'s turn discipline, so the
command's own structured output comes back deterministically.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.session_handle import AcpRuntimeError, AcpSessionHandle
from kiro_crew.acp.types import (
    EVENT_AGENT_SWITCHED,
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    METHOD_AGENT_SWITCHED,
    METHOD_COMMANDS_EXECUTE,
    METHOD_SESSION_UPDATE,
    JsonRpcMessage,
)

_REQ_ID = 7


class _CommandRuntime:
    """Runtime double for commands/execute turns.

    ``send_request`` records the outbound frame and enqueues the scripted
    session/update frames followed by the JSON-RPC response — AFTER the send,
    so the pre-turn stale-frame drain cannot eat them. ``respond=False``
    enqueues nothing, modelling kiro-cli's known no-response behavior.
    """

    def __init__(
        self,
        queue: asyncio.Queue,
        response_result: dict[str, Any] | None = None,
        updates: list[JsonRpcMessage] | None = None,
        acp_backend: str = "",
        respond: bool = True,
    ) -> None:
        self.pid = None
        self.is_alive = MagicMock(return_value=True)
        self.send_notification = AsyncMock()
        self.supports_image_prompt = False
        self.acp_backend = acp_backend
        self.requests: list[tuple[str, dict]] = []
        self.marks: list[tuple[str, bool]] = []
        self._queue = queue
        self._response_result = response_result if response_result is not None else {}
        self._updates = updates or []
        self._respond = respond
        self._last_activity = time.monotonic()

    def mark_turn_active(self, session_id: str, active: bool) -> None:
        self.marks.append((session_id, active))

    async def send_request(self, method: str, params: dict) -> int:
        self.requests.append((method, params))
        for frame in self._updates:
            self._queue.put_nowait(frame)
        if self._respond:
            self._queue.put_nowait(JsonRpcMessage(id=_REQ_ID, result=self._response_result))
        return _REQ_ID


def _make(
    response_result: dict[str, Any] | None = None,
    updates: list[JsonRpcMessage] | None = None,
    acp_backend: str = "",
    respond: bool = True,
) -> tuple[AcpSessionHandle, _CommandRuntime]:
    queue: asyncio.Queue = asyncio.Queue()
    rt = _CommandRuntime(
        queue,
        response_result=response_result,
        updates=updates,
        acp_backend=acp_backend,
        respond=respond,
    )
    handle = AcpSessionHandle("sA", queue, rt)
    return handle, rt


async def _collect(handle: AcpSessionHandle, command: str) -> list:
    return [ev async for ev in handle.stream_command(command, timeout=5.0)]


# ── Request shape ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sends_object_form_without_args():
    """A bare command goes out as the TuiCommand OBJECT form with empty args —
    the string form returns no response on kiro-cli 2.14.0."""
    handle, rt = _make(response_result={"message": "ok"})
    await _collect(handle, "/tools")
    assert rt.requests == [
        (
            METHOD_COMMANDS_EXECUTE,
            {"sessionId": "sA", "command": {"command": "tools", "args": {}}},
        )
    ]


@pytest.mark.asyncio
async def test_sends_object_form_with_value_arg():
    """``/agent foo`` carries the remainder as the ``value`` arg."""
    handle, rt = _make(response_result={"message": "ok"})
    await _collect(handle, "/agent foo")
    method, params = rt.requests[0]
    assert method == METHOD_COMMANDS_EXECUTE
    assert params["command"] == {"command": "agent", "args": {"value": "foo"}}


# ── Result extraction & completion ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_response_text_yielded_then_complete():
    """The command's output lives in the RESPONSE result, not update chunks —
    it must surface as a text chunk before the terminal event."""
    handle, _ = _make(response_result={"message": "13 tools available"})
    events = await _collect(handle, "/tools")
    kinds = [ev.kind for ev in events]
    assert kinds == [EVENT_TEXT_CHUNK, EVENT_COMPLETE]
    assert events[0].text == "13 tools available"
    # Turn bookkeeping: the handle is reusable afterwards.
    assert handle._turn_done.is_set()
    assert handle.is_turn_active is False


@pytest.mark.asyncio
async def test_structured_data_formatted_as_json_block():
    """result.data renders as a readable JSON block (agent/model filtered)."""
    handle, _ = _make(
        response_result={
            "message": "MCP servers",
            "data": {"servers": ["a", "b"], "agent": {"name": "x"}},
        }
    )
    events = await _collect(handle, "/mcp")
    text_events = [ev for ev in events if ev.kind == EVENT_TEXT_CHUNK]
    assert len(text_events) == 1
    assert "MCP servers" in text_events[0].text
    assert "```json" in text_events[0].text
    assert '"servers"' in text_events[0].text
    # agent metadata is filtered from the block (surfaced separately).
    assert '"agent"' not in text_events[0].text


@pytest.mark.asyncio
async def test_empty_result_yields_only_complete():
    """No message/data → no empty text chunk, just the terminal event."""
    handle, _ = _make(response_result={})
    events = await _collect(handle, "/tools")
    assert [ev.kind for ev in events] == [EVENT_COMPLETE]


@pytest.mark.asyncio
async def test_result_text_is_credential_redacted():
    """Command output is backend-echoed text that reaches the dashboard —
    the two-pass redaction (URLs + credentials) must run on it, matching
    send_command's auditable control at the same surface."""
    secret = "AKIAIOSFODNN7EXAMPLE"
    handle, _ = _make(response_result={"message": f"key {secret} leaked"})
    events = await _collect(handle, "/env")
    text_events = [ev for ev in events if ev.kind == EVENT_TEXT_CHUNK]
    assert len(text_events) == 1
    assert secret not in text_events[0].text


@pytest.mark.asyncio
async def test_agent_switch_extracted_from_result():
    """A command result carrying data.agent (e.g. /agent) emits
    EVENT_AGENT_SWITCHED so the dashboard's indicator updates."""
    handle, _ = _make(
        response_result={
            "message": "switched",
            "data": {"agent": {"name": "kirocrew"}},
        }
    )
    events = await _collect(handle, "/agent kirocrew")
    switches = [ev for ev in events if ev.kind == EVENT_AGENT_SWITCHED]
    assert [ev.text for ev in switches] == ["kirocrew"]


@pytest.mark.asyncio
async def test_agent_switch_not_duplicated_when_notification_arrived():
    """When the native agent-switch notification already reported the switch,
    the result-extracted fallback must not emit a second event."""
    notification = JsonRpcMessage(
        method=METHOD_AGENT_SWITCHED,
        params={"sessionId": "sA", "agentName": "kirocrew"},
    )
    handle, _ = _make(
        response_result={"data": {"agent": {"name": "kirocrew"}}},
        updates=[notification],
    )
    events = await _collect(handle, "/agent kirocrew")
    switches = [ev for ev in events if ev.kind == EVENT_AGENT_SWITCHED]
    assert len(switches) == 1


# ── Event draining ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_drains_session_update_chunks_before_response():
    """session/update frames emitted during command execution stream through
    with prompt()'s dispatch logic, ahead of the response-extracted text."""
    update = JsonRpcMessage(
        method=METHOD_SESSION_UPDATE,
        params={
            "sessionId": "sA",
            "update": {"sessionUpdate": "agent_message_chunk", "text": "streamed"},
        },
    )
    handle, _ = _make(response_result={"message": "final"}, updates=[update])
    events = await _collect(handle, "/tools")
    texts = [ev.text for ev in events if ev.kind == EVENT_TEXT_CHUNK]
    assert texts == ["streamed", "final"]
    assert events[-1].kind == EVENT_COMPLETE


# ── Turn discipline ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rejected_while_turn_active():
    """A command during an active turn is refused, not interleaved — it shares
    prompt()'s concurrent-turn guard on the same session queue."""
    handle, _ = _make(response_result={"message": "ok"})
    handle._turn_done.clear()  # a turn is in flight
    with pytest.raises(AcpRuntimeError):
        await _collect(handle, "/tools")


@pytest.mark.asyncio
async def test_turn_marked_active_and_released():
    """The turn is marked active around the send and released at completion,
    so shared-runtime frame routing sees the command like any other turn."""
    handle, rt = _make(response_result={"message": "ok"})
    await _collect(handle, "/tools")
    assert rt.marks == [("sA", True), ("sA", False)]


@pytest.mark.asyncio
async def test_send_failure_keeps_handle_reusable():
    """A send_request failure re-sets _turn_done and unmarks the turn — the
    BaseException guard — so the handle is not wedged permanently active."""
    handle, rt = _make()

    async def _boom(method: str, params: dict) -> int:
        raise AcpRuntimeError("broken pipe")

    rt.send_request = _boom  # type: ignore[method-assign]
    with pytest.raises(AcpRuntimeError):
        await _collect(handle, "/tools")
    assert handle._turn_done.is_set()
    # Marked active before the (failed) write, unmarked by the guard.
    assert rt.marks == [("sA", True), ("sA", False)]


# ── Transport carve-outs & failure modes ─────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/compact", "/compact keep the design", "/help"])
async def test_prompt_transport_commands_stay_on_prompt(command):
    """/compact and /help keep the PROMPT transport: kiro-cli 2.14.0 returns
    no response for them over commands/execute, and the compaction flow
    (session.py, Slack !compact) watches compaction status on the prompt
    stream — routing them natively would strand it until timeout."""
    from kiro_crew.acp.types import METHOD_PROMPT

    handle, rt = _make(response_result={"stopReason": "end_turn"})
    events = await _collect(handle, command)
    assert [m for m, _ in rt.requests] == [METHOD_PROMPT]
    assert events[-1].kind == EVENT_COMPLETE


@pytest.mark.asyncio
async def test_kas_backend_falls_back_to_prompt():
    """_kiro.dev/commands/execute is kiro-cli-specific: a KAS shared-runtime
    session must keep degrading softly through session/prompt instead of
    erroring on an unimplemented method."""
    from kiro_crew.acp.types import ACP_BACKEND_KAS, METHOD_PROMPT

    handle, rt = _make(response_result={"stopReason": "end_turn"}, acp_backend=ACP_BACKEND_KAS)
    events = await _collect(handle, "/tools")
    assert [m for m, _ in rt.requests] == [METHOD_PROMPT]
    assert events[-1].kind == EVENT_COMPLETE


@pytest.mark.asyncio
async def test_no_response_terminates_at_timeout():
    """An unanswered commands/execute request must terminate at the bounded
    command timeout with a terminal event — not drain for the chat-turn
    ceiling — and leave the handle reusable."""
    handle, _ = _make(respond=False)
    start = time.monotonic()
    events = await _collect_with_timeout(handle, "/tools", timeout=0.5)
    elapsed = time.monotonic() - start
    assert events[-1].kind == EVENT_COMPLETE
    assert events[-1].stop_reason == "timeout"
    assert elapsed < 5.0
    assert handle._turn_done.is_set()
    assert handle.is_turn_active is False


async def _collect_with_timeout(handle: AcpSessionHandle, command: str, timeout: float) -> list:
    return [ev async for ev in handle.stream_command(command, timeout=timeout)]
