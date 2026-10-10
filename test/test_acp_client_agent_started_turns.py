"""The idle reader and ``stream_unsolicited``: a turn the agent starts between prompts.

claude-agent-acp streams such a turn (a background command finished) while no
prompt is in flight. These tests drive ``AcpClient`` over a real
``asyncio.StreamReader`` fed with JSON-RPC lines, so the stream's rule of one
waiting ``readline()`` at a time is the real one. See
docs/request-for-change/rfc-agent-started-turns.md, Phase 1.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import json
import time
from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.client import (
    AcpClient,
    AcpError,
    _ends_agent_started_turn,
    _starts_agent_turn,
)
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    JsonRpcMessage,
)
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_AGENT_STARTED_TURNS,
)
from kiro_crew.testing.wait import async_wait_until

_CLIENT_SOURCE = Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "acp" / "client.py"


def _client(
    backend: str = "claude", *, on_turn=None
) -> tuple[AcpClient, asyncio.StreamReader, MagicMock]:
    client = AcpClient(acp_backend=backend)
    if backend in ACP_BACKENDS_AGENT_STARTED_TURNS:
        # The claude adapter's spawn step does this (ClaudeLaunch.resolve_spawn).
        client._install_idle_reader()
    reader = asyncio.StreamReader()
    proc = MagicMock()
    proc.stdout = reader
    proc.stdin = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.returncode = None
    client._process = proc
    client.on_agent_turn = on_turn
    return client, reader, proc


def _line(frame: dict) -> bytes:
    return (json.dumps(frame) + "\n").encode()


def _feed(reader: asyncio.StreamReader, *frames: dict) -> None:
    for frame in frames:
        reader.feed_data(_line(frame))


def _update(update: dict) -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {"sessionId": "s1", "update": update},
    }


def _chunk(text: str) -> dict:
    return _update(
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}
    )


def _usage(origin: dict | None = None) -> dict:
    update: dict = {"sessionUpdate": "usage_update", "used": 10, "size": 100}
    if origin is not None:
        update["_meta"] = {"_claude/origin": origin}
    return _update(update)


_AUTONOMOUS_END = _usage({"kind": "task-notification", "producer": "session-task", "runId": "r"})
_HUMAN_END = _usage({"kind": "human"})
_SESSION_INFO = _update({"sessionUpdate": "session_info_update", "title": "probe"})
_PERMISSION = {
    "jsonrpc": "2.0",
    "id": 3,
    "method": "session/request_permission",
    "params": {"sessionId": "s1", "toolCall": {"toolCallId": "t1"}, "options": []},
}


def _kinds(client: AcpClient) -> list[str]:
    return [m.params["update"]["sessionUpdate"] if m.params else str(m.id) for m in client._buffer]


async def _parked(reader: asyncio.StreamReader) -> None:
    """Wait until a coroutine waits on *reader* for a line."""
    await async_wait_until(lambda: reader._waiter is not None, timeout=5)


async def _reader_exits(client: AcpClient) -> None:
    task = client._idle_reader
    assert task is not None
    await asyncio.wait_for(asyncio.wait({task}), timeout=5)


async def _stop_restarted_reader(client: AcpClient) -> None:
    """Stop the reader a finished turn restarts once its read loop is closed."""
    await async_wait_until(lambda: client._stdout_claims == 0, timeout=5)
    await client._stop_idle_reader()


async def _handed_off(client: AcpClient, reader: asyncio.StreamReader, *frames: dict) -> None:
    """Start the idle reader, feed *frames*, and wait for the hand-off."""
    client._maybe_start_idle_reader()
    _feed(reader, *frames)
    await _reader_exits(client)
    assert client._agent_turn_pending() is True


# ── The reader hand-off ──


@pytest.mark.asyncio
async def test_activity_between_turns_hands_off_once_and_keeps_every_frame_in_order() -> None:
    turns: list[AcpClient] = []
    client, reader, _proc = _client(on_turn=turns.append)

    await _handed_off(client, reader, _usage(), _chunk("build passed"))

    assert turns == [client]
    assert _kinds(client) == ["usage_update", "agent_message_chunk"]


@pytest.mark.asyncio
async def test_a_permission_request_between_turns_starts_a_turn() -> None:
    turns: list[AcpClient] = []
    client, reader, _proc = _client(on_turn=turns.append)

    await _handed_off(client, reader, _PERMISSION)

    assert turns == [client]
    assert client._buffer[0].method == "session/request_permission"


@pytest.mark.parametrize(
    "kind", ["agent_message_chunk", "agent_thought_chunk", "tool_call", "tool_call_update", "plan"]
)
def test_each_activity_kind_starts_a_turn(kind: str) -> None:
    assert _starts_agent_turn(JsonRpcMessage.from_dict(_update({"sessionUpdate": kind})))


@pytest.mark.parametrize(
    "kind", ["task-notification", "peer", "coordinator", "observer", "observer-activity"]
)
def test_each_autonomous_origin_ends_the_turn(kind: str) -> None:
    assert _ends_agent_started_turn(JsonRpcMessage.from_dict(_usage({"kind": kind})))


@pytest.mark.parametrize("frame", [_SESSION_INFO, _AUTONOMOUS_END, _HUMAN_END])
@pytest.mark.asyncio
async def test_a_frame_that_reports_state_starts_no_turn(frame: dict) -> None:
    turns: list[AcpClient] = []
    client, reader, _proc = _client(on_turn=turns.append)
    client._maybe_start_idle_reader()
    _feed(reader, frame)
    await async_wait_until(lambda: reader._buffer == b"", timeout=5)
    await _parked(reader)

    await client._stop_idle_reader()

    assert turns == []
    assert client._agent_turn_pending() is False
    assert _kinds(client) == [frame["params"]["update"]["sessionUpdate"]]


@pytest.mark.asyncio
async def test_frames_go_back_ahead_of_what_was_already_buffered() -> None:
    client, reader, _proc = _client(on_turn=lambda _c: None)
    client._buffer.extend(
        [
            JsonRpcMessage.from_dict(_usage()),
            JsonRpcMessage.from_dict(_chunk("a")),
            JsonRpcMessage(id=9, result={}),
        ]
    )

    client._maybe_start_idle_reader()
    await _reader_exits(client)

    # The reader stopped at the chunk, the first activity, with the response still queued.
    assert _kinds(client) == ["usage_update", "agent_message_chunk", "9"]


@pytest.mark.asyncio
async def test_the_reader_stops_at_the_buffer_bound_and_leaves_the_rest_in_the_pipe() -> None:
    turns: list[AcpClient] = []
    client, reader, _proc = _client(on_turn=turns.append)
    client._buffer = deque(maxlen=3)
    client._maybe_start_idle_reader()
    _feed(reader, _usage(), _usage(), _usage(), _chunk("late"))

    await _reader_exits(client)

    assert turns == []
    assert _kinds(client) == ["usage_update"] * 3
    assert (await reader.readline()) == _line(_chunk("late"))


@pytest.mark.asyncio
async def test_a_cancel_inside_readline_loses_no_bytes_of_a_partial_line() -> None:
    client, reader, _proc = _client(on_turn=lambda _c: None)
    client._maybe_start_idle_reader()
    whole = _line(_chunk("split across writes"))
    reader.feed_data(whole[:10])
    await _parked(reader)

    await client._stop_idle_reader()
    reader.feed_data(whole[10:])
    msg = await client._read_message(timeout=5)

    assert msg is not None
    assert msg.params["update"]["content"]["text"] == "split across writes"


@pytest.mark.asyncio
async def test_the_reader_runs_after_a_cancelled_turn() -> None:
    """A turn the user stopped still lets a later background result through."""
    turns: list[AcpClient] = []
    client, reader, _proc = _client(on_turn=turns.append)
    client._cancelled = True
    client._cancel_ts = time.monotonic() - 3600

    await _handed_off(client, reader, _chunk("background finished"))

    assert turns == [client]


@pytest.mark.parametrize("case", ["no_callback", "turn_in_flight", "dead"])
@pytest.mark.asyncio
async def test_no_idle_reader_when_nothing_could_take_the_turn(case: str) -> None:
    client, _reader, proc = _client(on_turn=None if case == "no_callback" else (lambda _c: None))
    if case == "turn_in_flight":
        client._turn_done.clear()
    if case == "dead":
        proc.returncode = 1

    client._maybe_start_idle_reader()

    assert client._idle_reader is None


@pytest.mark.parametrize("stopped_by", ["claim", "reset"])
@pytest.mark.asyncio
async def test_held_frames_go_back_unless_the_process_was_reset(stopped_by: str) -> None:
    client, reader, _proc = _client(on_turn=lambda _c: None)
    client._maybe_start_idle_reader()
    task = client._idle_reader
    assert task is not None
    _feed(reader, _usage())
    await async_wait_until(lambda: reader._buffer == b"", timeout=5)
    await _parked(reader)

    if stopped_by == "claim":
        await client._stop_idle_reader()
    else:
        client._reset_state()
        await asyncio.wait_for(asyncio.wait({task}), timeout=5)

    assert _kinds(client) == (["usage_update"] if stopped_by == "claim" else [])
    assert task.done()


# ── Every stdout reader claims stdout first ──


def _read_sites() -> dict[str, ast.AsyncFunctionDef]:
    tree = ast.parse(_CLIENT_SOURCE.read_text(encoding="utf-8"))
    sites: dict[str, ast.AsyncFunctionDef] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for call in ast.walk(node):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "_read_message"
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "self"
            ):
                sites[node.name] = node
    return sites


_BOUND_BY_THE_CLAUDE_ADAPTER = (
    "ensure_ready",
    "_initialize_session",
    "_wait_for_response",
    "_drain_notifications",
    "wait_for_compaction",
    "_drain_post_compaction_metadata",
    "_prompt_loop",
    "_kill_process",
    "_reset_state",
)


def test_every_read_message_caller_claims_stdout() -> None:
    client, _reader, _proc = _client()
    sites = set(_read_sites())

    # The idle reader is the one reader that is stopped instead of stopping.
    unclaimed = sorted(name for name in sites if name not in vars(client))

    assert sites >= {
        "_prompt_loop",
        "_wait_for_response",
        "_drain_notifications",
        "wait_for_compaction",
        "_drain_post_compaction_metadata",
    }
    assert unclaimed == ["_idle_read_loop"]


@pytest.mark.asyncio
async def test_a_kiro_client_runs_its_own_methods_and_never_reads_between_turns() -> None:
    """harness-parity H13: the idle reader adds no step to the Kiro path."""
    client, reader, _proc = _client(backend=ACP_BACKEND_KIRO, on_turn=lambda _c: None)

    assert [name for name in _BOUND_BY_THE_CLAUDE_ADAPTER if name in vars(client)] == []
    _feed(reader, {"jsonrpc": "2.0", "id": 5, "result": {}})
    await client._wait_for_response(5, timeout=5)
    assert client._idle_reader is None


def test_the_claude_adapter_binds_the_reader_once_across_respawns(monkeypatch, tmp_path) -> None:
    from kiro_crew.acp.harness import SpawnContext
    from kiro_crew.acp.harness import claude as claude_mod
    from kiro_crew.acp.harness import process_adapter_for
    from kiro_crew.agent_sdk.drivers.acp import forget_cached_resolution

    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
    monkeypatch.setattr(
        claude_mod, "_resolve_claude_acp_bin", lambda: (["/opt/bin/claude-agent-acp"], "")
    )
    monkeypatch.setattr(claude_mod, "_claude_adapter_installed_version", lambda _argv: "0.87.0")
    monkeypatch.setattr(AcpClient, "_seed_session_settings", AsyncMock())
    monkeypatch.setattr(AcpClient, "_prepare_session_mcp", AsyncMock())
    ctx = SpawnContext(
        agent="kirocrew", work_dir=tmp_path, model=None, environ={}, home=tmp_path, session=client
    )
    forget_cached_resolution(ACP_BACKEND_CLAUDE)
    try:
        assert [name for name in _BOUND_BY_THE_CLAUDE_ADAPTER if name in vars(client)] == []
        asyncio.run(process_adapter_for(ACP_BACKEND_CLAUDE).resolve_spawn(ctx))
        first = {name: vars(client)[name] for name in _BOUND_BY_THE_CLAUDE_ADAPTER}
        asyncio.run(process_adapter_for(ACP_BACKEND_CLAUDE).resolve_spawn(ctx))
    finally:
        forget_cached_resolution(ACP_BACKEND_CLAUDE)

    # A respawn runs the step again and wraps nothing twice.
    assert {name: vars(client)[name] for name in _BOUND_BY_THE_CLAUDE_ADAPTER} == first


async def _run_site(client: AcpClient, reader: asyncio.StreamReader, site: str) -> None:
    if site == "ensure_ready":
        # A ready client, as when a prompt starts: nothing to spawn or read.
        client._work_dir_ready = True
        client._session_id = "s1"
        await client.ensure_ready()
    elif site == "_prompt_loop":
        _feed(reader, {"jsonrpc": "2.0", "id": 5, "result": {"stopReason": "end_turn"}})
        async with contextlib.aclosing(client._prompt_loop(5, 5.0)) as loop:
            async for action, _msg in loop:
                if action == "complete":
                    break
    elif site == "_wait_for_response":
        _feed(reader, {"jsonrpc": "2.0", "id": 5, "result": {}})
        await client._wait_for_response(5, timeout=5)
    elif site == "_drain_notifications":
        await client._drain_notifications(duration=0.2, idle_exit=0.1)
    elif site == "wait_for_compaction":
        await client.wait_for_compaction(timeout=0.2)
    else:
        await client._drain_post_compaction_metadata(grace=0.2)


@pytest.mark.parametrize(
    "site",
    [
        "ensure_ready",
        "_prompt_loop",
        "_wait_for_response",
        "_drain_notifications",
        "wait_for_compaction",
        "_drain_post_compaction_metadata",
    ],
)
@pytest.mark.asyncio
async def test_each_read_site_stops_the_idle_reader_and_the_last_release_restarts_it(
    site: str,
) -> None:
    client, reader, _proc = _client(on_turn=lambda _c: None)
    client._maybe_start_idle_reader()
    first = client._idle_reader
    assert first is not None
    await _parked(reader)

    # A second waiting readline() would raise "readuntil() called while another
    # coroutine is already waiting", so this only passes if the site stopped it.
    await asyncio.wait_for(_run_site(client, reader, site), timeout=10)

    assert first.done()
    assert client._idle_reader is not first and client._idle_reader is not None
    await client._stop_idle_reader()


@pytest.mark.asyncio
async def test_a_request_wait_that_reads_the_first_frame_ends_the_hand_off() -> None:
    turns: list[AcpClient] = []
    client, reader, _proc = _client(on_turn=turns.append)
    await _handed_off(client, reader, _chunk("one"))

    _feed(reader, {"jsonrpc": "2.0", "id": 9, "result": {}})
    await client._wait_for_response(9, timeout=5)

    # The wait filed the chunk as it files any notification, so the restarted
    # reader hands off the next frame instead.
    assert client._agent_turn_pending() is False
    assert [m.params["update"]["content"]["text"] for m in client._mcp_notifications] == ["one"]
    _feed(reader, _chunk("two"))
    await async_wait_until(lambda: len(turns) == 2, timeout=5)
    assert client._buffer[0].params["update"]["content"]["text"] == "two"


@pytest.mark.asyncio
async def test_a_request_wait_that_puts_the_first_frame_back_keeps_the_hand_off() -> None:
    turns: list[AcpClient] = []
    client, reader, _proc = _client(on_turn=turns.append)
    await _handed_off(client, reader, _PERMISSION)

    _feed(reader, {"jsonrpc": "2.0", "id": 9, "result": {}})
    await client._wait_for_response(9, timeout=5)

    assert client._agent_turn_pending() is True
    assert client._buffer[0].method == "session/request_permission"
    assert client._idle_reader is not None and client._idle_reader.done()
    assert turns == [client]


# ── stream_unsolicited ──


@pytest.mark.asyncio
async def test_stream_unsolicited_sends_nothing_and_ends_on_the_autonomous_result() -> None:
    turns: list[AcpClient] = []
    client, reader, proc = _client(on_turn=turns.append)
    await _handed_off(client, reader, _chunk("build "))
    _feed(reader, _HUMAN_END, _chunk("passed"), _AUTONOMOUS_END, _chunk("next turn"))

    async with contextlib.aclosing(client.stream_unsolicited(timeout=30)) as stream:
        events = [e async for e in stream]

    # A human origin is a usage frame here; only the autonomous one ends the turn.
    assert [e.text for e in events if e.kind == EVENT_TEXT_CHUNK] == ["build ", "passed"]
    assert events[-1].kind == EVENT_COMPLETE
    assert events[-1].stop_reason == "end_turn"
    proc.stdin.write.assert_not_called()
    # The frame after the end marker is left for the restarted reader: a new turn.
    await async_wait_until(lambda: len(turns) == 2, timeout=5)
    assert client._agent_turn_pending() is True
    assert client._buffer[0].params["update"]["content"]["text"] == "next turn"


@pytest.mark.asyncio
async def test_a_stop_during_the_turn_ends_it_as_cancelled_on_the_autonomous_result() -> None:
    client, reader, _proc = _client(on_turn=lambda _c: None)
    await _handed_off(client, reader, _chunk("long reply"))

    async with contextlib.aclosing(client.stream_unsolicited(timeout=30)) as stream:
        first = await stream.__anext__()
        client._cancelled = True
        client._cancel_ts = time.monotonic()
        _feed(reader, _AUTONOMOUS_END)
        rest = [e async for e in stream]

    assert first.kind == EVENT_TEXT_CHUNK
    assert rest[-1].kind == EVENT_COMPLETE
    assert rest[-1].stop_reason == "cancelled"
    await _stop_restarted_reader(client)


@pytest.mark.asyncio
async def test_stream_unsolicited_raises_when_the_process_dies() -> None:
    client, reader, proc = _client(on_turn=lambda _c: None)
    await _handed_off(client, reader, _chunk("half"))
    proc.returncode = 1
    reader.feed_eof()

    events = []
    with pytest.raises(AcpError):
        async for event in client.stream_unsolicited(timeout=30):
            events.append(event)

    assert [e.text for e in events if e.kind == EVENT_TEXT_CHUNK] == ["half"]


@pytest.mark.asyncio
async def test_stream_unsolicited_ends_on_the_timeout_gate() -> None:
    client, reader, _proc = _client(on_turn=lambda _c: None)
    await _handed_off(client, reader, _chunk("no end marker"))

    async with contextlib.aclosing(client.stream_unsolicited(timeout=0.3)) as stream:
        events = [e async for e in stream]

    # Text streamed, so the deadline ends it the way a user turn's stale end does.
    assert events[-1].kind == EVENT_COMPLETE
    assert events[-1].synthetic_completion is True
    await _stop_restarted_reader(client)


@pytest.mark.asyncio
async def test_a_prompt_that_starts_first_reads_the_frames_as_its_own() -> None:
    client, reader, _proc = _client(on_turn=lambda _c: None)
    await _handed_off(client, reader, _chunk("background result"))

    loop = client._prompt_loop(5, 5.0)
    action, msg = await loop.__anext__()
    await loop.aclose()

    assert action == "update"
    assert msg.params["update"]["content"]["text"] == "background result"
    assert client._agent_turn_pending() is False
    assert [e async for e in client.stream_unsolicited(timeout=30)] == []
    await client._stop_idle_reader()


@pytest.mark.asyncio
async def test_an_end_marker_before_the_first_activity_does_not_end_the_turn() -> None:
    """The lone marker an earlier cycle leaves between turns is read as usage."""
    turns: list[AcpClient] = []
    client, reader, _proc = _client(on_turn=turns.append)
    await _handed_off(client, reader, _AUTONOMOUS_END, _chunk("real "))
    _feed(reader, _chunk("reply"), _AUTONOMOUS_END)

    async with contextlib.aclosing(client.stream_unsolicited(timeout=30)) as stream:
        events = [e async for e in stream]

    assert [e.text for e in events if e.kind == EVENT_TEXT_CHUNK] == ["real ", "reply"]
    assert [e.kind for e in events].count(EVENT_COMPLETE) == 1
    assert turns == [client]
    await _stop_restarted_reader(client)


@pytest.mark.asyncio
async def test_stream_unsolicited_yields_nothing_while_a_prompt_is_in_flight() -> None:
    client, reader, _proc = _client(on_turn=lambda _c: None)
    await _handed_off(client, reader, _chunk("background result"))
    # A prompt was written and its turn has not reached _prompt_loop yet.
    client._turn_done.clear()

    assert [e async for e in client.stream_unsolicited(timeout=30)] == []
    assert client._agent_turn_pending() is True


@pytest.mark.asyncio
async def test_a_claim_cancelled_while_the_reader_stops_restarts_it(monkeypatch) -> None:
    client, reader, _proc = _client(on_turn=lambda _c: None)
    client._maybe_start_idle_reader()
    first = client._idle_reader
    assert first is not None
    await _parked(reader)
    stop = client._stop_idle_reader

    async def _stop_then_cancelled() -> None:
        await stop()
        raise asyncio.CancelledError

    with monkeypatch.context() as patched:
        patched.setattr(client, "_stop_idle_reader", _stop_then_cancelled)
        with pytest.raises(asyncio.CancelledError):
            await client._claim_stdout()

    assert client._stdout_claims == 0
    assert first.done()
    assert client._idle_reader is not first and client._idle_reader is not None
    await client._stop_idle_reader()


@pytest.mark.asyncio
async def test_the_tail_of_a_prompt_whose_turn_ended_early_ends_on_its_response() -> None:
    """A user turn reaped before its reply finished leaves a tail that ends on its response."""
    client, reader, _proc = _client(on_turn=lambda _c: None)
    await _handed_off(client, reader, _chunk("late tail"))
    late_response = {"jsonrpc": "2.0", "id": 7, "result": {"stopReason": "cancelled"}}
    _feed(reader, _HUMAN_END, late_response, _chunk("next"))

    async with contextlib.aclosing(client.stream_unsolicited(timeout=30)) as stream:
        events = [e async for e in stream]

    assert [e.text for e in events if e.kind == EVENT_TEXT_CHUNK] == ["late tail"]
    assert events[-1].kind == EVENT_COMPLETE
    assert events[-1].stop_reason == "cancelled"
    assert events[-1].synthetic_completion is False
    await async_wait_until(client._agent_turn_pending, timeout=5)
    assert client._buffer[0].params["update"]["content"]["text"] == "next"
