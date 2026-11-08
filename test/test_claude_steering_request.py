"""claude user steer over ``_session/steering`` (``ACP_BACKENDS_STEERING_REQUEST``).

claude-agent-acp runs one adapter per session, so ``AcpClient`` speaks the request
for it. The turn's own read loop reads the answer; ``injected`` is an acceptance,
and the steer is reported consumed only when the turn ends with ``end_turn``. A
steer that reaches the adapter after its turn settled is answered
``promptRequired`` (the request opts into it) and the caller queues the text. A
steer that arrives while a tool call runs is held and sent once no call is open.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.client import AcpClient, steering_advertised
from kiro_crew.acp.transport_errors import AcpError, AcpProcessDied
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_STEER_CONSUMED,
    METHOD_REQUEST_PERMISSION,
    METHOD_SESSION_STEERING,
    JsonRpcMessage,
)
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKENDS_STEER,
    ACP_BACKENDS_STEERING_REQUEST,
)
from kiro_crew.testing.clock import ManualClock
from kiro_crew.testing.wait import async_wait_until, await_until

PROMPT_ID = 1000


class _Wire:
    """A claude session's stdio: frames Crew wrote, and frames the adapter sends."""

    def __init__(self, client: AcpClient) -> None:
        self.inbound: asyncio.Queue[JsonRpcMessage] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []

        async def _write(data: bytes, request_id: int, method: str) -> None:
            self.sent.append(json.loads(data))

        async def _read(timeout: float = 0.0) -> JsonRpcMessage | None:
            # Every test ends its turn with a terminal frame, so the loop is
            # never left waiting on an empty wire.
            return await self.inbound.get()

        client._write_request_bounded = _write  # type: ignore[method-assign]
        client._read_message = _read  # type: ignore[method-assign]

    def steering_frames(self) -> list[dict[str, Any]]:
        return [f for f in self.sent if f.get("method") == METHOD_SESSION_STEERING]

    async def next_steering_id(self) -> int:
        frames = await async_wait_until(self.steering_frames, describe=lambda: self.sent)
        return frames[-1]["id"]

    def answer(self, request_id: int, outcome: str) -> None:
        self.inbound.put_nowait(JsonRpcMessage(id=request_id, result={"outcome": outcome}))

    def end_turn(self, stop_reason: str = "end_turn") -> None:
        self.inbound.put_nowait(JsonRpcMessage(id=PROMPT_ID, result={"stopReason": stop_reason}))


def _client(version: str = "0.89.1", *, advertised: bool = True, backend: str = "") -> AcpClient:
    client = AcpClient(acp_backend=backend or ACP_BACKEND_CLAUDE)
    client._session_id = "sess-1"
    client._agent_version = version
    client._agent_version_read = True
    client._steering_advertised = advertised
    client._process = MagicMock()
    client._process.returncode = None
    client.ensure_ready = AsyncMock()  # type: ignore[method-assign]
    client._send_prompt = AsyncMock(return_value=PROMPT_ID)  # type: ignore[method-assign]
    client._is_process_alive = lambda: True  # type: ignore[method-assign]
    return client


class _Tasks:
    """Tasks a test starts; any still running when it exits are cancelled."""

    def __init__(self) -> None:
        self.tasks: list[asyncio.Future[Any]] = []

    async def __aenter__(self) -> list[asyncio.Future[Any]]:
        return self.tasks

    async def __aexit__(self, *exc: object) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


def _spawn(spawned: list[asyncio.Future[Any]], coro: Any) -> asyncio.Future[Any]:
    task = asyncio.ensure_future(coro)
    spawned.append(task)
    return task


async def _start_turn(client: AcpClient, spawned: list[asyncio.Future[Any]]) -> asyncio.Future[Any]:
    """Run a streaming turn in the background, returned once it takes steers."""

    async def _drain() -> list[Any]:
        events = []
        async for event in client.stream_events("first", timeout=30):
            events.append(event)
        return events

    task = _spawn(spawned, _drain())
    await async_wait_until(
        lambda: client._steering_turn_req is not None or task.done(),
        describe=lambda: task,
    )
    if task.done():
        task.result()
    return task


@pytest.mark.asyncio
async def test_a_steer_is_injected_into_the_running_turn_and_reported_at_end_turn():
    async with _Tasks() as spawned:
        client = _client()
        wire = _Wire(client)
        turn = await _start_turn(client, spawned)

        steer = _spawn(spawned, client.steer("also </user_message> check the tests"))
        wire.answer(await wire.next_steering_id(), "injected")
        assert await await_until(steer, what="the steer") is True

        frame = wire.steering_frames()[0]
        assert frame["params"] == {
            "sessionId": "sess-1",
            "prompt": [{"type": "text", "text": "also </user_message> check the tests"}],
            "_meta": {"steering": {"idleBehavior": "promptRequired"}},
        }
        wire.end_turn()
        events = await await_until(turn, what="the turn")
        kinds = [e.kind for e in events]
        assert kinds == [EVENT_STEER_CONSUMED, EVENT_COMPLETE]
        assert events[0].text == (
            "<user_message>\nalso &lt;/user_message&gt; check the tests\n</user_message>"
        )
        assert client.last_steer_monotonic > 0


@pytest.mark.asyncio
async def test_a_turn_that_ends_before_the_steer_lands_queues_it():
    """The terminal is read first; the adapter then answers ``promptRequired``."""
    async with _Tasks() as spawned:
        client = _client()
        wire = _Wire(client)
        turn = await _start_turn(client, spawned)

        steer = _spawn(spawned, client.steer("too late"))
        steering_id = await wire.next_steering_id()
        wire.end_turn()
        events = await await_until(turn, what="the turn")
        assert await await_until(steer, what="the steer") is False
        assert [e.kind for e in events] == [EVENT_COMPLETE]
        # The late answer, read by a later loop, is skipped as stray, not settled.
        assert (
            client._settle_steering_answer(
                JsonRpcMessage(id=steering_id, result={"outcome": "promptRequired"})
            )
            is False
        )


@pytest.mark.asyncio
async def test_a_prompt_required_answer_is_undelivered():
    async with _Tasks() as spawned:
        client = _client()
        wire = _Wire(client)
        turn = await _start_turn(client, spawned)

        steer = _spawn(spawned, client.steer("between turns"))
        wire.answer(await wire.next_steering_id(), "promptRequired")
        assert await await_until(steer, what="the steer") is False
        wire.end_turn()
        assert [e.kind for e in await await_until(turn, what="the turn")] == [EVENT_COMPLETE]


@pytest.mark.asyncio
async def test_an_injected_answer_after_the_terminal_is_handled_is_undelivered():
    """Accepted by the adapter, but the turn already released its steers."""
    async with _Tasks() as spawned:
        client = _client()
        wire = _Wire(client)
        turn = await _start_turn(client, spawned)

        steer = _spawn(spawned, client.steer("racing"))
        steering_id = await wire.next_steering_id()
        wire.end_turn()
        await await_until(turn, what="the turn")
        client._settle_steering_answer(
            JsonRpcMessage(id=steering_id, result={"outcome": "injected"})
        )
        assert await await_until(steer, what="the steer") is False


@pytest.mark.parametrize("stop_reason", ["cancelled", "max_tokens", "refusal"])
@pytest.mark.asyncio
async def test_only_a_clean_end_turn_reports_an_accepted_steer(stop_reason):
    async with _Tasks() as spawned:
        client = _client()
        wire = _Wire(client)
        turn = await _start_turn(client, spawned)

        steer = _spawn(spawned, client.steer("keep going"))
        wire.answer(await wire.next_steering_id(), "injected")
        assert await await_until(steer, what="the steer") is True
        wire.end_turn(stop_reason)
        assert EVENT_STEER_CONSUMED not in [
            e.kind for e in await await_until(turn, what="the turn")
        ]


@pytest.mark.asyncio
async def test_a_cancelled_turn_reports_nothing_even_at_end_turn():
    async with _Tasks() as spawned:
        client = _client()
        wire = _Wire(client)
        turn = await _start_turn(client, spawned)

        steer = _spawn(spawned, client.steer("keep going"))
        wire.answer(await wire.next_steering_id(), "injected")
        assert await await_until(steer, what="the steer") is True
        client._cancelled = True
        wire.end_turn()
        assert EVENT_STEER_CONSUMED not in [
            e.kind for e in await await_until(turn, what="the turn")
        ]


@pytest.mark.asyncio
async def test_an_error_answer_is_undelivered():
    async with _Tasks() as spawned:
        client = _client()
        wire = _Wire(client)
        turn = await _start_turn(client, spawned)

        steer = _spawn(spawned, client.steer("hello"))
        wire.inbound.put_nowait(
            JsonRpcMessage(
                id=await wire.next_steering_id(), error={"code": -32601, "message": "nope"}
            )
        )
        assert await await_until(steer, what="the steer") is False
        wire.end_turn()
        assert [e.kind for e in await await_until(turn, what="the turn")] == [EVENT_COMPLETE]


@pytest.mark.asyncio
async def test_no_running_turn_sends_nothing():
    client = _client()
    wire = _Wire(client)
    assert await client.steer("idle") is False
    assert wire.sent == []


@pytest.mark.asyncio
async def test_a_steer_past_the_text_bound_sends_nothing():
    async with _Tasks() as spawned:
        from kiro_crew.acp.types import MAX_STEERING_TEXT_CHARS

        client = _client()
        wire = _Wire(client)
        turn = await _start_turn(client, spawned)
        assert await client.steer("x" * (MAX_STEERING_TEXT_CHARS + 1)) is False
        assert wire.steering_frames() == []
        wire.end_turn()
        await await_until(turn, what="the turn")


@pytest.mark.parametrize(
    ("version", "advertised", "expected"),
    [
        ("0.89.1", True, True),
        ("0.65.0", True, True),
        ("0.64.2", True, False),
        ("0.63.2", True, False),
        ("", True, False),
        ("0.89.1", False, False),
    ],
)
def test_supports_steer_needs_the_advertisement_and_the_floor(version, advertised, expected):
    client = _client(version, advertised=advertised)
    assert client.supports_steer is expected
    assert client.supports_refusal_steer is False
    assert client.steer_needs_loss_recovery is True


@pytest.mark.asyncio
async def test_below_the_floor_a_steer_sends_nothing():
    async with _Tasks() as spawned:
        client = _client("0.64.2")
        wire = _Wire(client)
        turn = await _start_turn(client, spawned)
        assert await client.steer("hello") is False
        assert wire.sent == []
        wire.end_turn()
        await await_until(turn, what="the turn")


@pytest.mark.asyncio
async def test_no_steer_is_sent_while_the_turn_is_parked_on_an_approval():
    """Nothing reads stdout until the approval is answered, so the steer queues."""
    client = _client()
    wire = _Wire(client)
    wire.inbound.put_nowait(
        JsonRpcMessage(
            id=7,
            method=METHOD_REQUEST_PERMISSION,
            params={
                "sessionId": "sess-1",
                "toolCall": {"title": "Bash", "toolCallId": "tc1"},
                "options": [
                    {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "reject", "name": "Reject", "kind": "reject_once"},
                ],
            },
        )
    )
    events = client.stream_events("first", timeout=30)
    try:
        first = await await_until(
            asyncio.ensure_future(events.__anext__()), what="the permission event"
        )
        assert first.kind == EVENT_PERMISSION_REQUEST
        assert await client.steer("while you wait") is False
        assert wire.steering_frames() == []
        wire.end_turn()

        async def _rest() -> list[Any]:
            return [event async for event in events]

        rest = await await_until(asyncio.ensure_future(_rest()), what="the rest of the turn")
        assert [e.kind for e in rest] == [EVENT_COMPLETE]
        # Resumed past the approval, the turn takes steers again until it ends.
        assert client._parked_on_permission is False
    finally:
        await events.aclose()


@pytest.mark.asyncio
async def test_an_answer_behind_a_slow_approval_is_still_read(monkeypatch):
    """A permission frame ahead of the answer parks the loop past the wait budget.

    The parked time is not charged, so the steer is settled by its own answer once
    the approval is answered, instead of timing out and queueing a second copy.
    The client's clock is frozen, and each wait slice the steer spends parked
    moves it one second, so the approval outlasts the 8s budget in virtual time
    and nothing before the park can spend any of it.
    """
    import kiro_crew.acp.client as client_mod

    clock = ManualClock()
    clock.install(monkeypatch, client_mod)
    monkeypatch.setattr(client_mod, "_STEERING_WAIT_SLICE_SECS", 0.01)
    parked_slices = [0]
    real_wait = asyncio.wait

    async def _counting_wait(fs: Any, *, timeout: Any = None, **kwargs: Any) -> Any:
        # A steer wait slice that runs while the loop is parked is one second of
        # the approval being up.
        if timeout == 0.01 and client._parked_on_permission:
            parked_slices[0] += 1
            clock.advance(1.0)
        return await real_wait(fs, timeout=timeout, **kwargs)

    monkeypatch.setattr(client_mod.asyncio, "wait", _counting_wait)
    async with _Tasks() as spawned:
        client = _client()
        wire = _Wire(client)
        approved = asyncio.Event()
        parked = asyncio.Event()

        async def _consume() -> list[Any]:
            events = []
            async for event in client.stream_events("first", timeout=30):
                events.append(event)
                if event.kind == EVENT_PERMISSION_REQUEST:
                    parked.set()
                    await await_until(approved, what="the approval")
            return events

        turn = _spawn(spawned, _consume())
        await async_wait_until(lambda: client._steering_turn_req is not None or turn.done())
        steer = _spawn(spawned, client.steer("change course"))
        steering_id = await wire.next_steering_id()
        wire.inbound.put_nowait(
            JsonRpcMessage(
                id=7,
                method=METHOD_REQUEST_PERMISSION,
                params={
                    "sessionId": "sess-1",
                    "toolCall": {"title": "Bash", "toolCallId": "tc1"},
                    "options": [
                        {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                        {"optionId": "reject", "name": "Reject", "kind": "reject_once"},
                    ],
                },
            )
        )
        wire.answer(steering_id, "injected")
        await await_until(parked, what="the approval park")
        # Twelve parked seconds, past the 8s budget: a wait that charged them
        # would have returned by now.
        await async_wait_until(lambda: parked_slices[0] >= 12 or steer.done())
        assert not steer.done()
        approved.set()
        assert await await_until(steer, what="the steer") is True
        wire.end_turn()
        kinds = [e.kind for e in await await_until(turn, what="the turn")]
        assert kinds == [EVENT_PERMISSION_REQUEST, EVENT_STEER_CONSUMED, EVENT_COMPLETE]


def test_a_steering_member_with_no_client_floor_is_not_steerable_here():
    """codex is a member, but this client has no floor for it: fail closed."""
    client = _client(backend=ACP_BACKEND_CODEX)
    assert client.supports_steer is False


def test_claude_joins_the_steering_request_set_and_not_the_steer_set():
    assert ACP_BACKEND_CLAUDE in ACP_BACKENDS_STEERING_REQUEST
    assert ACP_BACKEND_CLAUDE not in ACP_BACKENDS_STEER


@pytest.mark.parametrize(
    ("init_resp", "expected"),
    [
        ({"_meta": {"steering": {"supported": True}}}, True),
        ({"_meta": {"steering": {"supported": "true"}}}, False),
        ({"_meta": {"steering": {}}}, False),
        ({"agentCapabilities": {"_meta": {"steering": {"supported": True}}}}, False),
        ({}, False),
        ([], False),
    ],
)
def test_the_handshake_advertisement_is_read_from_the_top_level_meta(init_resp, expected):
    assert steering_advertised(init_resp) is expected


def _tool_update(update: dict[str, Any]) -> JsonRpcMessage:
    return JsonRpcMessage(method="session/update", params={"sessionId": "sess-1", "update": update})


def _open_tool(tool_call_id: str = "tc1") -> JsonRpcMessage:
    return _tool_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": tool_call_id,
            "title": "Bash",
            "kind": "execute",
            "status": "in_progress",
        }
    )


def _close_tool(tool_call_id: str = "tc1") -> JsonRpcMessage:
    return _tool_update(
        {"sessionUpdate": "tool_call_update", "toolCallId": tool_call_id, "status": "completed"}
    )


def _steered_texts(wire: _Wire) -> list[str]:
    return [frame["params"]["prompt"][0]["text"] for frame in wire.steering_frames()]


async def _wait_closed(client: AcpClient, tool_call_id: str) -> None:
    """Until the read loop has handled the call's terminal update.

    The loop releases held steers in the same step that drops the call from the
    open set, so once the call is gone the release has already decided.
    """
    await async_wait_until(
        lambda: tool_call_id in client._observed_tool_calls
        and tool_call_id not in client._active_tool_calls,
        describe=lambda: list(client._active_tool_calls),
    )


@pytest.mark.asyncio
async def test_a_steer_while_a_tool_runs_is_sent_when_the_tool_finishes():
    """claude would move a running tool to the background and end the turn early,
    so the steer is held while the call runs and goes out as soon as it ends."""
    async with _Tasks() as spawned:
        client = _client()
        wire = _Wire(client)
        turn = await _start_turn(client, spawned)
        wire.inbound.put_nowait(_open_tool())
        await async_wait_until(lambda: client._active_tool_calls.possibly_open)

        assert await client.steer("change course") is True
        # Held, not sent: no sender was started while the call is open.
        assert client._held_steer_task is None
        assert wire.steering_frames() == []
        # Stamped at the hold, so a `wait` sleep the call is running ends on it.
        assert client.last_steer_monotonic > 0

        wire.inbound.put_nowait(_close_tool())
        wire.answer(await wire.next_steering_id(), "injected")
        assert _steered_texts(wire) == ["change course"]
        await async_wait_until(
            lambda: client._steers_accepted, describe=lambda: client._steers_held
        )
        wire.end_turn()
        events = await await_until(turn, what="the turn")
        assert [e.kind for e in events if e.kind in (EVENT_STEER_CONSUMED, EVENT_COMPLETE)] == [
            EVENT_STEER_CONSUMED,
            EVENT_COMPLETE,
        ]
        consumed = [e for e in events if e.kind == EVENT_STEER_CONSUMED]
        assert consumed[0].text == "<user_message>\nchange course\n</user_message>"


@pytest.mark.asyncio
async def test_a_held_steer_waits_for_every_open_tool_call():
    async with _Tasks() as spawned:
        client = _client()
        wire = _Wire(client)
        turn = await _start_turn(client, spawned)
        wire.inbound.put_nowait(_open_tool("tc1"))
        wire.inbound.put_nowait(_open_tool("tc2"))
        await async_wait_until(lambda: len(client._active_tool_calls) == 2)
        assert await client.steer("after both") is True

        wire.inbound.put_nowait(_close_tool("tc1"))
        await _wait_closed(client, "tc1")
        assert client._held_steer_task is None
        assert wire.steering_frames() == []

        wire.inbound.put_nowait(_close_tool("tc2"))
        wire.answer(await wire.next_steering_id(), "injected")
        await async_wait_until(
            lambda: client._steers_accepted, describe=lambda: client._steers_held
        )
        wire.end_turn()
        kinds = [e.kind for e in await await_until(turn, what="the turn")]
        assert EVENT_STEER_CONSUMED in kinds
        assert _steered_texts(wire) == ["after both"]


@pytest.mark.asyncio
async def test_held_steers_go_out_in_the_order_they_came():
    """A steer sent after the call ended, while a held one still waits for its
    answer, goes out behind it rather than beside it."""
    async with _Tasks() as spawned:
        client = _client()
        wire = _Wire(client)
        turn = await _start_turn(client, spawned)
        wire.inbound.put_nowait(_open_tool())
        await async_wait_until(lambda: client._active_tool_calls.possibly_open)
        assert await client.steer("first") is True
        assert await client.steer("second") is True

        wire.inbound.put_nowait(_close_tool())
        first_id = await wire.next_steering_id()
        assert await client.steer("third") is True
        assert _steered_texts(wire) == ["first"]

        wire.answer(first_id, "injected")
        await async_wait_until(lambda: len(wire.steering_frames()) == 2)
        wire.answer(wire.steering_frames()[-1]["id"], "injected")
        await async_wait_until(lambda: len(wire.steering_frames()) == 3)
        wire.answer(wire.steering_frames()[-1]["id"], "injected")
        await async_wait_until(lambda: len(client._steers_accepted) == 3)
        assert _steered_texts(wire) == ["first", "second", "third"]

        wire.end_turn()
        events = await await_until(turn, what="the turn")
        assert [e.text for e in events if e.kind == EVENT_STEER_CONSUMED] == [
            f"<user_message>\n{text}\n</user_message>" for text in ("first", "second", "third")
        ]


@pytest.mark.parametrize("stop_reason", ["end_turn", "cancelled"])
@pytest.mark.asyncio
async def test_a_steer_still_held_when_the_turn_ends_is_never_sent(stop_reason):
    """Nothing reports it consumed, so the caller queues it as the next turn."""
    async with _Tasks() as spawned:
        client = _client()
        wire = _Wire(client)
        turn = await _start_turn(client, spawned)
        wire.inbound.put_nowait(_open_tool())
        await async_wait_until(lambda: client._active_tool_calls.possibly_open)
        assert await client.steer("never sent") is True

        wire.end_turn(stop_reason)
        events = await await_until(turn, what="the turn")
        assert EVENT_STEER_CONSUMED not in [e.kind for e in events]
        assert client._steers_held == []
        assert client._held_steer_task is None
        assert wire.steering_frames() == []


@pytest.mark.asyncio
async def test_a_steer_held_by_a_failed_turn_is_not_carried_into_the_next():
    """A failed turn has no clean terminal to drop its held steers, so the next
    turn starts with none and sends only its own."""
    async with _Tasks() as spawned:
        client = _client()
        wire = _Wire(client)
        failed = await _start_turn(client, spawned)
        wire.inbound.put_nowait(_open_tool())
        await async_wait_until(lambda: client._active_tool_calls.possibly_open)
        assert await client.steer("from the failed turn") is True
        wire.inbound.put_nowait(
            JsonRpcMessage(id=PROMPT_ID, error={"code": -32603, "message": "internal error"})
        )
        with pytest.raises(AcpError):
            await await_until(failed, what="the failed turn")

        turn = await _start_turn(client, spawned)
        wire.inbound.put_nowait(_open_tool("tc2"))
        await async_wait_until(lambda: client._active_tool_calls.possibly_open)
        assert await client.steer("from the next turn") is True
        wire.inbound.put_nowait(_close_tool("tc2"))
        wire.answer(await wire.next_steering_id(), "injected")
        await async_wait_until(
            lambda: client._steers_accepted, describe=lambda: client._steers_held
        )
        wire.end_turn()
        events = await await_until(turn, what="the next turn")
        assert _steered_texts(wire) == ["from the next turn"]
        assert [e.text for e in events if e.kind == EVENT_STEER_CONSUMED] == [
            "<user_message>\nfrom the next turn\n</user_message>"
        ]


@pytest.mark.asyncio
async def test_no_held_steer_is_written_behind_a_stalled_one():
    """A held steer whose write stalls the adapter's stdin leaves the rest unsent,
    so nothing more is written into a pipe the adapter is not reading."""
    async with _Tasks() as spawned:
        client = _client()
        wire = _Wire(client)
        written = client._write_request_bounded

        async def _stall_on_steering(data: bytes, request_id: int, method: str) -> None:
            await written(data, request_id, method)
            if method == METHOD_SESSION_STEERING:
                client._stdin_stalled = True
                raise AcpProcessDied("ACP stdin stalled", ambiguous_delivery=True)

        client._write_request_bounded = _stall_on_steering  # type: ignore[method-assign]
        turn = await _start_turn(client, spawned)
        wire.inbound.put_nowait(_open_tool())
        await async_wait_until(lambda: client._active_tool_calls.possibly_open)
        assert await client.steer("first") is True
        assert await client.steer("second") is True

        wire.inbound.put_nowait(_close_tool())
        await _wait_closed(client, "tc1")
        sender = client._held_steer_task
        assert sender is not None
        await await_until(sender, what="the held-steer sender")
        assert _steered_texts(wire) == ["first"]
        assert client._steers_held == []

        wire.end_turn()
        events = await await_until(turn, what="the turn")
        assert EVENT_STEER_CONSUMED not in [e.kind for e in events]


@pytest.mark.asyncio
async def test_a_stalled_held_write_is_reported_possibly_delivered_until_the_next_turn():
    """The adapter may read a stalled frame, so the caller must queue the text as
    possibly delivered; only the steer whose write stalled is reported, and the
    report survives the turn's end so the caller's teardown can read it."""
    async with _Tasks() as spawned:
        client = _client()
        wire = _Wire(client)
        written = client._write_request_bounded

        async def _stall_on_steering(data: bytes, request_id: int, method: str) -> None:
            await written(data, request_id, method)
            if method == METHOD_SESSION_STEERING:
                client._stdin_stalled = True
                raise AcpProcessDied("ACP stdin stalled", ambiguous_delivery=True)

        client._write_request_bounded = _stall_on_steering  # type: ignore[method-assign]
        turn = await _start_turn(client, spawned)
        wire.inbound.put_nowait(_open_tool())
        await async_wait_until(lambda: client._active_tool_calls.possibly_open)
        assert await client.steer("  stalled  ") is True
        assert await client.steer("dropped") is True

        wire.inbound.put_nowait(_close_tool())
        await _wait_closed(client, "tc1")
        sender = client._held_steer_task
        assert sender is not None
        await await_until(sender, what="the held-steer sender")
        assert client.held_steers_possibly_delivered == {"stalled"}

        wire.end_turn()
        await await_until(turn, what="the turn")
        assert client.held_steers_possibly_delivered == {"stalled"}

        client._stdin_stalled = False
        client._write_request_bounded = written  # type: ignore[method-assign]
        nxt = await _start_turn(client, spawned)
        assert client.held_steers_possibly_delivered == frozenset()
        wire.end_turn()
        await await_until(nxt, what="the next turn")


@pytest.mark.asyncio
async def test_a_held_write_in_flight_is_reported_possibly_delivered():
    """A turn that ends while the held steer's write is still draining finds the
    text reported, as the dashboard counts a direct steer's RPC in flight."""
    async with _Tasks() as spawned:
        client = _client()
        wire = _Wire(client)
        written = client._write_request_bounded
        entered = asyncio.Event()
        release = asyncio.Event()

        async def _slow_steering(data: bytes, request_id: int, method: str) -> None:
            if method == METHOD_SESSION_STEERING:
                entered.set()
                await release.wait()
            await written(data, request_id, method)

        client._write_request_bounded = _slow_steering  # type: ignore[method-assign]
        turn = await _start_turn(client, spawned)
        wire.inbound.put_nowait(_open_tool())
        await async_wait_until(lambda: client._active_tool_calls.possibly_open)
        assert await client.steer("in flight") is True

        wire.inbound.put_nowait(_close_tool())
        await await_until(asyncio.ensure_future(entered.wait()), what="the held write")
        assert client.held_steers_possibly_delivered == {"in flight"}

        release.set()
        wire.answer(await wire.next_steering_id(), "injected")
        await async_wait_until(lambda: client._steers_accepted)
        assert client.held_steers_possibly_delivered == frozenset()
        wire.end_turn()
        await await_until(turn, what="the turn")


@pytest.mark.asyncio
async def test_an_unanswered_held_steer_stays_possibly_delivered_until_the_next_turn(monkeypatch):
    """Spending the answer budget does not prove the adapter ignored the write."""
    import kiro_crew.acp.client as client_mod

    clock = ManualClock()
    clock.install(monkeypatch, client_mod)
    monkeypatch.setattr(client_mod, "_STEERING_WAIT_SLICE_SECS", 0.01)
    real_wait = asyncio.wait

    async def _spend_budget(fs: Any, *, timeout: Any = None, **kwargs: Any) -> Any:
        if timeout == 0.01:
            result = await real_wait(fs, timeout=0, **kwargs)
            clock.advance(1.0)
            return result
        return await real_wait(fs, timeout=timeout, **kwargs)

    monkeypatch.setattr(client_mod.asyncio, "wait", _spend_budget)
    async with _Tasks() as spawned:
        client = _client()
        wire = _Wire(client)
        turn = await _start_turn(client, spawned)
        wire.inbound.put_nowait(_open_tool())
        await async_wait_until(lambda: client._active_tool_calls.possibly_open)
        assert await client.steer("unanswered") is True

        wire.inbound.put_nowait(_close_tool())
        steering_id = await wire.next_steering_id()
        sender = client._held_steer_task
        assert sender is not None
        spawned.append(sender)
        await await_until(sender, what="the expired held-steer sender")
        assert not turn.done()
        assert client.held_steers_possibly_delivered == {"unanswered"}
        assert client._steers_accepted == []
        assert client._steers_held == []
        assert (
            client._settle_steering_answer(
                JsonRpcMessage(id=steering_id, result={"outcome": "injected"})
            )
            is False
        )

        wire.end_turn()
        events = await await_until(turn, what="the turn")
        assert EVENT_STEER_CONSUMED not in [e.kind for e in events]
        assert client.held_steers_possibly_delivered == {"unanswered"}
        nxt = await _start_turn(client, spawned)
        assert client.held_steers_possibly_delivered == frozenset()
        wire.end_turn()
        await await_until(nxt, what="the next turn")


@pytest.mark.parametrize("stop_reason", ["end_turn", "cancelled"])
@pytest.mark.parametrize("outcome", [None, "injected"])
@pytest.mark.asyncio
async def test_a_held_steer_unresolved_at_turn_end_stays_possibly_delivered(
    monkeypatch, stop_reason, outcome
):
    """An unanswered write or late injection is not confirmed non-delivery."""
    async with _Tasks() as spawned:
        client = _client()
        wire = _Wire(client)
        release = asyncio.Event()
        wait_for_answer = client._await_steering_answer

        async def _hold_sender(answer, turn_done):
            await wait_for_answer(answer, turn_done)
            await await_until(release, what="the sender's outcome check")

        monkeypatch.setattr(client, "_await_steering_answer", _hold_sender)
        turn = await _start_turn(client, spawned)
        wire.inbound.put_nowait(_open_tool())
        await async_wait_until(lambda: client._active_tool_calls.possibly_open)
        assert await client.steer("turn ended") is True
        wire.inbound.put_nowait(_close_tool())
        steering_id = await wire.next_steering_id()
        sender = client._held_steer_task
        assert sender is not None
        spawned.append(sender)

        wire.end_turn(stop_reason)
        events = await await_until(turn, what="the turn")
        assert EVENT_STEER_CONSUMED not in [e.kind for e in events]
        if outcome is not None:
            assert (
                client._settle_steering_answer(
                    JsonRpcMessage(id=steering_id, result={"outcome": outcome})
                )
                is True
            )
        release.set()
        await await_until(sender, what="the held-steer sender")
        assert client.held_steers_possibly_delivered == {"turn ended"}
        assert client._steers_accepted == []
        assert client._steers_held == []
        nxt = await _start_turn(client, spawned)
        assert client.held_steers_possibly_delivered == frozenset()
        wire.end_turn()
        await await_until(nxt, what="the next turn")


@pytest.mark.parametrize("outcome", ["promptRequired", "failed", "error"])
@pytest.mark.asyncio
async def test_a_held_steer_the_adapter_does_not_inject_is_not_reported(outcome):
    async with _Tasks() as spawned:
        client = _client()
        wire = _Wire(client)
        turn = await _start_turn(client, spawned)
        wire.inbound.put_nowait(_open_tool())
        await async_wait_until(lambda: client._active_tool_calls.possibly_open)
        assert await client.steer("too late") is True

        wire.inbound.put_nowait(_close_tool())
        steering_id = await wire.next_steering_id()
        if outcome == "error":
            wire.inbound.put_nowait(
                JsonRpcMessage(id=steering_id, error={"code": -32601, "message": "nope"})
            )
        else:
            wire.answer(steering_id, outcome)
        # The answer is in once the sender drops the steer from the held list.
        await async_wait_until(lambda: not client._steers_held)
        assert client._steers_accepted == []
        # Answered, so the adapter's verdict is known: not possibly delivered.
        assert client.held_steers_possibly_delivered == frozenset()
        wire.end_turn()
        events = await await_until(turn, what="the turn")
        assert EVENT_STEER_CONSUMED not in [e.kind for e in events]


@pytest.mark.asyncio
async def test_held_steers_count_toward_the_turn_bound():
    from kiro_crew.acp.types import MAX_STEERING_ANSWERS

    async with _Tasks() as spawned:
        client = _client()
        wire = _Wire(client)
        turn = await _start_turn(client, spawned)
        wire.inbound.put_nowait(_open_tool())
        await async_wait_until(lambda: client._active_tool_calls.possibly_open)
        for n in range(MAX_STEERING_ANSWERS):
            assert await client.steer(f"steer {n}") is True
        assert await client.steer("one too many") is False
        assert len(client._steers_held) == MAX_STEERING_ANSWERS

        wire.end_turn("cancelled")
        await await_until(turn, what="the turn")
        assert wire.steering_frames() == []


@pytest.mark.asyncio
async def test_a_steer_after_the_tool_finished_is_sent():
    async with _Tasks() as spawned:
        client = _client()
        wire = _Wire(client)
        turn = await _start_turn(client, spawned)
        wire.inbound.put_nowait(_open_tool())
        wire.inbound.put_nowait(_close_tool())
        await async_wait_until(lambda: "tc1" in client._observed_tool_calls)
        await async_wait_until(lambda: not client._active_tool_calls.possibly_open)

        steer = _spawn(spawned, client.steer("now"))
        wire.answer(await wire.next_steering_id(), "injected")
        assert await await_until(steer, what="the steer") is True
        wire.end_turn()
        kinds = [e.kind for e in await await_until(turn, what="the turn")]
        assert EVENT_STEER_CONSUMED in kinds
