"""pi user steer over ``_session/steering`` (``ACP_BACKENDS_STEERING_REQUEST``).

pi-acp runs one adapter per session, so ``AcpClient`` speaks the request for
it, exactly as for claude-agent-acp. pi-acp 0.1.8 advertises
``initialize._meta.steering.supported: true`` and answers ``injected`` while a
turn runs (delivered after the current tool calls, before the next model
call) or ``promptRequired`` with no running turn. Older adapters advertise
nothing, so there is no version floor: the advertisement alone gates it. A
steer is reported consumed only when the turn ends with ``end_turn``; anything
else queues the text.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_STEER_CONSUMED,
    METHOD_REQUEST_PERMISSION,
    METHOD_SESSION_STEERING,
    JsonRpcMessage,
)
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_PI,
    ACP_BACKENDS_STEER,
    ACP_BACKENDS_STEERING_REQUEST,
)
from kiro_crew.testing.wait import async_wait_until, await_until

PROMPT_ID = 1000


class _Wire:
    """A pi session's stdio: frames Crew wrote, and frames the adapter sends."""

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


def _client(version: str = "0.1.8", *, advertised: bool = True) -> AcpClient:
    client = AcpClient(acp_backend=ACP_BACKEND_PI)
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

        steer = _spawn(spawned, client.steer("also check the tests"))
        wire.answer(await wire.next_steering_id(), "injected")
        assert await await_until(steer, what="the steer") is True

        frame = wire.steering_frames()[0]
        assert frame["params"] == {
            "sessionId": "sess-1",
            "prompt": [{"type": "text", "text": "also check the tests"}],
            "_meta": {"steering": {"idleBehavior": "promptRequired"}},
        }
        wire.end_turn()
        events = await await_until(turn, what="the turn")
        kinds = [e.kind for e in events]
        assert kinds == [EVENT_STEER_CONSUMED, EVENT_COMPLETE]
        assert events[0].text == "<user_message>\nalso check the tests\n</user_message>"
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
    """An old adapter without the method answers ``-32601``: queue instead."""
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
        ("0.1.8", True, True),
        ("0.0.33", True, True),
        ("", True, True),
        ("0.1.8", False, False),
        ("0.1.3", False, False),
    ],
)
def test_supports_steer_needs_the_advertisement_and_no_floor(version, advertised, expected):
    """pi has no version floor: steering first appears in 0.1.8 already behaving,
    and older adapters never advertise it, so the advertisement alone gates it."""
    client = _client(version, advertised=advertised)
    assert client.supports_steer is expected
    assert client.supports_refusal_steer is False
    assert client.steer_needs_loss_recovery is True


@pytest.mark.asyncio
async def test_unadvertised_a_steer_sends_nothing():
    """An old pi-acp answers ``_session/steering`` with method-not-found, so an
    unadvertised adapter is never sent the request."""
    async with _Tasks() as spawned:
        client = _client(advertised=False)
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


def test_pi_joins_the_steering_request_set_and_not_the_steer_set():
    assert ACP_BACKEND_PI in ACP_BACKENDS_STEERING_REQUEST
    assert ACP_BACKEND_PI not in ACP_BACKENDS_STEER
