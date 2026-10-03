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


def test_the_liveness_map_is_count_bounded_and_keeps_the_call_in_flight(caplog) -> None:
    """A backend emitting distinct tool_call frames whose results never report a
    terminal status must not grow the attribution map for the turn. At the cap
    the OLDEST entry is evicted and the call just dispatched is retained, so the
    oracle keeps reporting a tool in flight; a terminal for a retained id still
    pops; the eviction is said once per turn; the turn's clear resets it."""
    import logging

    from kiro_crew.acp.session_handle import MAX_ACTIVE_TOOL_CALLS

    handle = _handle()
    with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.session_handle"):
        for i in range(MAX_ACTIVE_TOOL_CALLS + 5):
            handle._handle_update(_update_msg(_tool_call(f"tc-{i}")))
    assert len(handle._active_tool_calls) == MAX_ACTIVE_TOOL_CALLS
    assert f"tc-{MAX_ACTIVE_TOOL_CALLS + 4}" in handle._active_tool_calls
    assert "tc-0" not in handle._active_tool_calls and "tc-4" not in handle._active_tool_calls
    assert "tc-5" in handle._active_tool_calls
    assert handle._inflight_tool_call_id == f"tc-{MAX_ACTIVE_TOOL_CALLS + 4}"
    assert handle._tool_dispatched is True and handle._stale_eligible is False
    evictions = [r for r in caplog.records if "liveness map reached its bound" in r.getMessage()]
    assert len(evictions) == 1 and str(MAX_ACTIVE_TOOL_CALLS) in evictions[0].getMessage()
    # A retained call's terminal still pops it.
    handle._handle_update(_update_msg(_tool_result("tc-5")))
    assert "tc-5" not in handle._active_tool_calls
    assert len(handle._active_tool_calls) == MAX_ACTIVE_TOOL_CALLS - 1
    # A re-dispatch under an id already held does not evict.
    handle._handle_update(_update_msg(_tool_call("tc-6")))
    assert len(handle._active_tool_calls) == MAX_ACTIVE_TOOL_CALLS - 1
    # The turn's clear resets the map and the once-per-turn mark.
    handle._active_tool_calls.clear()
    handle._active_tool_calls_evicted = False
    assert handle._active_tool_calls_evicted is False


def test_a_retained_attribution_is_bounded_in_every_field() -> None:
    """The map's count bound is the provenance caches' by name, and each row is
    bounded in the strings it keeps: a frame whose backend-authored title and
    input are megabytes long is retained as a head of each, at the point of
    retention, so the bound on the count is a bound on memory."""
    from kiro_crew.acp._dispatch import TOOL_CALL_CACHE_MAX_ENTRIES
    from kiro_crew.acp.liveness import (
        MAX_RETAINED_COMMAND_CHARS,
        MAX_RETAINED_TITLE_CHARS,
        ToolCallState,
    )
    from kiro_crew.acp.session_handle import MAX_ACTIVE_TOOL_CALLS

    assert MAX_ACTIVE_TOOL_CALLS is TOOL_CALL_CACHE_MAX_ENTRIES
    big = "x" * (2 * 1024 * 1024)
    state = ToolCallState(title=big, command="sleep 30 " + big + " > build.log 2>&1")
    assert len(state.title) == MAX_RETAINED_TITLE_CHARS
    assert len(state.command) == MAX_RETAINED_COMMAND_CHARS
    assert state.command.startswith("sleep 30 ")
    assert state.command.endswith(" > build.log 2>&1")
    # A short pair is kept verbatim.
    assert ToolCallState(title="grep", command="grep x").command == "grep x"
    # Through the handle: the frame's title and input reach the map bounded.
    handle = _handle()
    frame = _tool_call("tc-big")
    frame["title"] = big
    frame["rawInput"] = {"command": big}
    frame["kind"] = "execute"
    handle._handle_update(_update_msg(frame))
    retained = handle._active_tool_calls["tc-big"][0]
    assert len(retained.title) <= MAX_RETAINED_TITLE_CHARS
    assert len(retained.command) <= MAX_RETAINED_COMMAND_CHARS


def test_the_map_key_and_identity_fields_are_bounded_too() -> None:
    """The row's key is the frame's raw ``toolCallId`` and its ``tool_name`` /
    ``mcp_server_name`` are retained verbatim off a first-class channel, so the
    count bound was still a bound on rows of unbounded width. A long id is held
    as its digest -- the same at dispatch, output and terminal, so the terminal
    still pops it -- and the two names are cut at construction; the output-seen
    set is keyed the same way and pruned to in-flight calls at the cap."""
    from kiro_crew.acp._dispatch import TOOL_CALL_CACHE_MAX_KEY_CHARS
    from kiro_crew.acp.liveness import MAX_RETAINED_NAME_CHARS, ToolCallState
    from kiro_crew.acp.session_handle import MAX_ACTIVE_TOOL_CALLS, _liveness_key

    big = "n" * (1024 * 1024)
    state = ToolCallState(tool_name=big, mcp_server_name=big)
    assert len(state.tool_name) == MAX_RETAINED_NAME_CHARS
    assert len(state.mcp_server_name) == MAX_RETAINED_NAME_CHARS
    short = "t" * TOOL_CALL_CACHE_MAX_KEY_CHARS
    assert _liveness_key(short) == short
    long_id = "i" * (TOOL_CALL_CACHE_MAX_KEY_CHARS + 1)
    key = _liveness_key(long_id)
    assert key.startswith("sha256:") and len(key) == len("sha256:") + 64
    assert _liveness_key(long_id) == key and _liveness_key(long_id + "x") != key
    handle = _handle()
    huge_id = "x" * (256 * 1024)
    handle._handle_update(_update_msg(_tool_call(huge_id)))
    assert huge_id not in handle._active_tool_calls
    assert _liveness_key(huge_id) in handle._active_tool_calls
    assert max(len(k) for k in handle._active_tool_calls) <= len("sha256:") + 64
    assert handle._inflight_tool_call_id == _liveness_key(huge_id)
    handle._handle_update(_update_msg(_tool_result(huge_id, status="in_progress")))
    assert _liveness_key(huge_id) in handle._tool_output_seen
    assert huge_id not in handle._tool_output_seen
    handle._handle_update(_update_msg(_tool_result(huge_id)))
    assert not handle._active_tool_calls, "the terminal under the long id still pops its row"
    # The output-seen set is pruned to in-flight calls once it reaches the cap.
    handle = _handle()
    for i in range(MAX_ACTIVE_TOOL_CALLS):
        handle._handle_update(_update_msg(_tool_call(f"tc-{i}")))
        handle._handle_update(_update_msg(_tool_result(f"tc-{i}", status="in_progress")))
        handle._handle_update(_update_msg(_tool_result(f"tc-{i}")))
    assert not handle._active_tool_calls
    assert len(handle._tool_output_seen) == MAX_ACTIVE_TOOL_CALLS
    handle._handle_update(_update_msg(_tool_call("tc-live")))
    handle._handle_update(_update_msg(_tool_result("tc-live", status="in_progress")))
    assert handle._tool_output_seen == {"tc-live"}


def test_after_an_eviction_an_empty_map_does_not_arm_the_stale_clock() -> None:
    """The map evicts its oldest row at the cap, but that call may still be
    running; once the retained rows settle, an empty map must not read as
    "nothing in flight" or the stale clock would cancel-probe the live call.
    After an eviction the turn keeps a tool in flight until its clear()."""
    from kiro_crew.acp.session_handle import MAX_ACTIVE_TOOL_CALLS

    handle = _handle()
    for i in range(MAX_ACTIVE_TOOL_CALLS + 1):
        handle._handle_update(_update_msg(_tool_call(f"tc-{i}")))
    assert handle._active_tool_calls_evicted is True
    assert "tc-0" not in handle._active_tool_calls
    for i in range(1, MAX_ACTIVE_TOOL_CALLS + 1):
        handle._handle_update(_update_msg(_tool_result(f"tc-{i}")))
    assert not handle._active_tool_calls
    assert handle._stale_eligible is False, "tc-0 may still be running"
    assert handle._tool_dispatched is True
    handle._handle_update(
        _update_msg(
            {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hi"}}
        )
    )
    assert handle._stale_eligible is False
    # Without an eviction the same shape arms the clock as before.
    plain = _handle()
    plain._handle_update(_update_msg(_tool_call("a")))
    plain._handle_update(_update_msg(_tool_result("a")))
    assert plain._stale_eligible is True and plain._tool_dispatched is False


def test_a_non_string_result_id_is_no_key_and_does_not_crash_the_turn() -> None:
    """The result parser hands ``toolCallId`` through as it arrived, so a harness
    that spells one as a JSON number reaches the liveness bookkeeping with an
    int; the key helper reads that as no key rather than calling ``len()`` on
    it, and the handle survives the frame with its map untouched."""
    from kiro_crew.acp.session_handle import _liveness_key

    assert _liveness_key(123) == ""
    assert _liveness_key(None) == ""
    assert _liveness_key("") == ""
    handle = _handle()
    handle._handle_update(_update_msg(_tool_call("tc-a")))
    for status in ("in_progress", "completed"):
        frame = _tool_result("tc-a", status=status)
        frame["toolCallId"] = 42
        handle._handle_update(_update_msg(frame))  # must not raise
    assert "tc-a" in handle._active_tool_calls
    assert 42 not in handle._tool_output_seen and "" not in handle._tool_output_seen


def test_a_bounded_command_keeps_the_redirect_the_stall_hint_reads() -> None:
    """The bound must not change what the stall-recovery nudge says on any
    harness: a long shell command typically redirects at its end, and the
    nudge tells the model to tail that file instead of re-running the command.
    The retained command keeps the head every oracle reader uses AND the tail
    the hint reads, so the hint resolves the same target before and after the
    bound."""
    from kiro_crew.acp.liveness import MAX_RETAINED_COMMAND_CHARS, ToolCallState
    from kiro_crew.dashboard.state import extract_log_redirect_target

    body = "python3 -m pytest " + " ".join(f"test/test_{i}.py" for i in range(1200))
    command = body + " > /tmp/pytest-run.log 2>&1"
    assert len(command) > MAX_RETAINED_COMMAND_CHARS
    assert extract_log_redirect_target(command) == "/tmp/pytest-run.log"
    state = ToolCallState(command=command)
    assert len(state.command) == MAX_RETAINED_COMMAND_CHARS
    assert extract_log_redirect_target(state.command) == "/tmp/pytest-run.log"
    assert state.command.startswith("python3 -m pytest ")
    # A redirect in the head (a heredoc file write) is kept as well.
    heredoc = "cat > /tmp/out.txt <<'EOF'\n" + "line\n" * 3000 + "EOF"
    assert extract_log_redirect_target(ToolCallState(command=heredoc).command) == "/tmp/out.txt"
    # Under the bound nothing is touched.
    short = "ls -la > listing.txt"
    assert ToolCallState(command=short).command == short
