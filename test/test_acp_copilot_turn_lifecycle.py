"""Copilot hold recovery respects liveness and the current read turn."""

import asyncio
from contextlib import aclosing
from unittest.mock import AsyncMock, MagicMock

import pytest

import kiro_crew.acp.client as acp_client
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.client_transport import _AGENT_STARTED_REQUEST_ID
from kiro_crew.acp.liveness import EVIDENCE_SAMPLING, VERDICT_UNKNOWN, VERDICT_WORKING
from kiro_crew.acp.types import JsonRpcMessage
from kiro_crew.testing.clock import ManualClock
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, until_parked

ENTRY_POINTS = (
    "send_message",
    "send_message_stream",
    "stream_events",
    "stream_command",
    "stream_unsolicited",
)


def _client(tmp_path, monkeypatch, entry="stream_events", backend="copilot"):
    clock = ManualClock()
    clock.install(monkeypatch, acp_client)
    client = AcpClient(work_dir=tmp_path, acp_backend=backend)
    client._session_id = "s"
    client.ensure_ready = AsyncMock()
    client._send_prompt = AsyncMock(return_value=1)
    client._send_request = AsyncMock(return_value=1)
    client._send_response = AsyncMock()
    client._is_process_alive = lambda: True
    client._process = MagicMock()
    client._process.stdin.drain = AsyncMock()
    client._read_new_tool_results_sync = lambda: []
    client._consult_liveness_model_wait = AsyncMock(return_value=(VERDICT_WORKING, "cpu"))
    client._agent_turn_gate = asyncio.Lock()
    client._agent_turn_pending = lambda: True
    req_id = _AGENT_STARTED_REQUEST_ID if entry == "stream_unsolicited" else 1
    return client, clock, req_id


async def _consume(client, entry):
    if entry == "send_message":
        return await client.send_message("continue", timeout=FALLBACK_TIMEOUT_SECS)
    args = () if entry == "stream_unsolicited" else ("continue",)
    async with aclosing(getattr(client, entry)(*args, timeout=FALLBACK_TIMEOUT_SECS)) as events:
        return [event async for event in events]


def _answer(text):
    return JsonRpcMessage(
        method="session/update",
        params={
            "sessionId": "s",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": text},
            },
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ENTRY_POINTS)
@pytest.mark.parametrize("reject_now", [False, True])
async def test_rejection_belongs_to_current_turn(tmp_path, monkeypatch, entry, reject_now):
    client, clock, req_id = _client(tmp_path, monkeypatch, entry)
    # Exercise the real rejection writer left over from an earlier turn.
    await client.reject_tool(70)
    assert client._turn_permission_denied
    observed = []

    async def read(timeout):
        assert len(observed) < 2, "turn did not consume its terminal response"
        observed.append(client._turn_permission_denied)
        if len(observed) == 1:
            if reject_now:
                await client.reject_tool(71)
            clock.advance(acp_client._REJECTED_TURN_SETTLE_SECS + 1)
            return None
        return JsonRpcMessage(
            id=req_id, result={"stopReason": "cancelled" if client._cancelled else "end_turn"}
        )

    client._read_message = read
    await _consume(client, entry)

    assert observed[0] is False
    assert client._turn_permission_denied is reject_now
    assert client._cancelled is reject_now
    assert client._last_stop_reason == ("cancelled" if reject_now else "end_turn")
    assert client._process.stdin.write.call_count == int(reject_now)
    assert client._shell_hold_cancelled is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "verdict,evidence",
    [
        (VERDICT_WORKING, "cpu"),
        (VERDICT_UNKNOWN, EVIDENCE_SAMPLING),
    ],
)
async def test_tracked_shell_does_not_override_live_generation(
    tmp_path, monkeypatch, verdict, evidence
):
    client, clock, req_id = _client(tmp_path, monkeypatch)
    # A prior shell may have exited without ever sending shell_exit.
    client._copilot_live_shells.add("missing-exit")
    client._consult_liveness_model_wait.return_value = (verdict, evidence)
    reads = 0

    async def read(timeout):
        nonlocal reads
        reads += 1
        assert reads <= 4, "turn did not consume its terminal response"
        if client._cancelled:
            return JsonRpcMessage(id=req_id, result={"stopReason": "cancelled"})
        if reads == 1:
            return _answer("Starting. ")
        if reads == 2:
            clock.advance(acp_client._STALE_TURN_TIMEOUT + 1)
            return None
        if reads == 3:
            return _answer("Finished the long generation.")
        return JsonRpcMessage(id=req_id, result={"stopReason": "end_turn"})

    client._read_message = read
    # The virtual read crosses the production stale window, so allow that turn.
    async with aclosing(client.stream_events("continue", timeout=None)) as stream:
        events = [event async for event in stream]
    assert "".join(e.text for e in events if e.kind == "text_chunk") == (
        "Starting. Finished the long generation."
    )
    client._process.stdin.write.assert_not_called()
    client._consult_liveness_model_wait.assert_awaited_once()
    assert events[-1].stop_reason == "end_turn"
    assert client._copilot_live_shells == {"missing-exit"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "backend,live,tool_open,user_cancel",
    [
        ("copilot", True, False, False),
        ("copilot", True, True, False),
        ("copilot", True, False, True),
        ("copilot", False, False, False),
        ("claude", True, False, False),
    ],
)
async def test_settled_shell_hold_requires_no_open_tool_and_owns_its_cancel(
    tmp_path, monkeypatch, backend, live, tool_open, user_cancel
):
    client, clock, req_id = _client(tmp_path, monkeypatch, backend=backend)
    client._consult_liveness_model_wait.return_value = (VERDICT_UNKNOWN, "flat")
    if live:
        client._copilot_live_shells.add("0")
    client._stale_eligible = True
    client._tool_dispatched = tool_open
    reads = 0

    async def read(timeout):
        nonlocal reads
        reads += 1
        assert reads <= 4, "turn did not consume its terminal response"
        if reads == 1:
            clock.advance(acp_client._STALE_TURN_TIMEOUT + 1)
            if user_cancel:
                await client.cancel_session()
                return JsonRpcMessage(id=req_id, result={"stopReason": "cancelled"})
            return None
        return JsonRpcMessage(
            id=req_id, result={"stopReason": "cancelled" if client._cancelled else "end_turn"}
        )

    client._read_message = read
    async with aclosing(client._prompt_loop(req_id, acp_client._DEFAULT_PROMPT_TIMEOUT)) as stream:
        actions = []
        async for pair in stream:
            actions.append(pair)
            if pair[0] == "complete":
                break
    recovered = backend == "copilot" and live and not tool_open and not user_cancel
    assert client._shell_hold_cancelled is recovered
    assert client._process.stdin.write.call_count == int(recovered or user_cancel)
    assert client._cancelled is user_cancel
    if recovered or tool_open or user_cancel:
        assert actions[-1][0] == "complete"
        assert actions[-1][1].result["stopReason"] == ("cancelled" if user_cancel else "end_turn")
    else:
        assert actions == []


@pytest.mark.asyncio
async def test_queued_turn_keeps_active_turn_rejection_until_it_owns_the_lock(
    tmp_path, monkeypatch
):
    client, _clock, req_id = _client(tmp_path, monkeypatch)
    client._read_message = AsyncMock(
        return_value=JsonRpcMessage(id=req_id, result={"stopReason": "end_turn"})
    )
    await client.reject_tool(70)
    await client._turn_lock.acquire()
    queued = asyncio.create_task(_consume(client, "stream_events"))
    try:
        await until_parked(client._turn_lock)
        assert client._turn_permission_denied is True
    finally:
        client._turn_lock.release()
        await asyncio.wait_for(queued, timeout=FALLBACK_TIMEOUT_SECS)
    assert client._turn_permission_denied is False


@pytest.mark.asyncio
async def test_shell_hold_recovers_once_liveness_settles(tmp_path, monkeypatch):
    client, clock, req_id = _client(tmp_path, monkeypatch)
    client._copilot_live_shells.add("0")
    client._consult_liveness_model_wait.side_effect = [
        (VERDICT_WORKING, "cpu"),
        (VERDICT_UNKNOWN, "flat"),
    ]
    reads = 0

    async def read(timeout):
        nonlocal reads
        reads += 1
        assert reads <= 4, "turn did not consume its terminal response"
        if reads == 1:
            return _answer("Answer")
        if reads in (2, 3):
            assert not client._cancelled
            clock.advance(acp_client._STALE_TURN_TIMEOUT + 1)
            return None
        assert client._cancelled
        return JsonRpcMessage(id=req_id, result={"stopReason": "cancelled"})

    client._read_message = read
    async with aclosing(client.stream_events("continue", timeout=None)) as stream:
        events = [event async for event in stream]
    assert client._consult_liveness_model_wait.await_count == 2
    client._process.stdin.write.assert_called_once()
    assert events[-1].stop_reason == "end_turn"
    assert client._last_stop_reason == "end_turn"
    assert client._cancelled is False


@pytest.mark.asyncio
async def test_answered_shell_hold_after_current_rejection_stays_cancelled(tmp_path, monkeypatch):
    client, clock, req_id = _client(tmp_path, monkeypatch)
    client._copilot_live_shells.add("0")
    client._consult_liveness_model_wait.return_value = (VERDICT_UNKNOWN, "flat")
    reads = 0

    async def read(timeout):
        nonlocal reads
        reads += 1
        assert reads <= 3, "turn did not consume its terminal response"
        if reads == 1:
            return _answer("Starting. ")
        if reads == 2:
            await client.reject_tool(71)
            clock.advance(acp_client._STALE_TURN_TIMEOUT + 1)
            return None
        assert client._cancelled
        return JsonRpcMessage(id=req_id, result={"stopReason": "cancelled"})

    client._read_message = read
    async with aclosing(client.stream_events("continue", timeout=None)) as stream:
        events = [event async for event in stream]
    assert events[-1].stop_reason == "cancelled"
    assert client._last_stop_reason == "cancelled"
    assert client._shell_hold_cancelled is False
    assert client._turn_permission_denied is True
    client._process.stdin.write.assert_called_once()


@pytest.mark.asyncio
async def test_consumer_park_does_not_settle_a_shell_hold(tmp_path, monkeypatch):
    client, clock, req_id = _client(tmp_path, monkeypatch)
    client._copilot_live_shells.add("0")
    client._consult_liveness_model_wait.return_value = (VERDICT_UNKNOWN, "flat")
    reads = 0

    async def read(timeout):
        nonlocal reads
        reads += 1
        assert reads <= 3, "turn did not consume its terminal response"
        if reads == 1:
            return _answer("Answer")
        if reads == 2:
            clock.advance(1)
            return None
        return JsonRpcMessage(id=req_id, result={"stopReason": "end_turn"})

    client._read_message = read
    async with aclosing(client.stream_events("continue", timeout=None)) as stream:
        events = []
        async for event in stream:
            events.append(event)
            if event.kind == "text_chunk":
                clock.advance(acp_client._STALE_TURN_TIMEOUT + 1)
    client._process.stdin.write.assert_not_called()
    assert events[-1].stop_reason == "end_turn"
    assert events[-1].synthetic_completion is False
    assert reads == 3
