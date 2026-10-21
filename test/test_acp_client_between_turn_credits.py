"""Credits kiro reports while ``AcpClient`` waits on a compaction BETWEEN turns.

``wait_for_compaction`` and its post-compaction grace drain run after the turn
that asked for the compaction has already billed its ``EVENT_COMPLETE``, and
the next turn's stats re-init starts its credit counter at zero. So the credits
they read are held on the client and billed by the next turn, exactly once;
credits read inside a turn stay on that turn; a claimed (rekeyed) client
never hands credits to the session that claimed it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from acp_client_helpers import _native_projection_for_fake_processes  # noqa: F401 - fixture
from acp_client_helpers import _pin_the_installed_kiro_cli_version  # noqa: F401 - fixture

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    METHOD_COMPACTION_STATUS,
    METHOD_METADATA,
    JsonRpcMessage,
)

_REQ_ID = 7


def _metering(value: float, *, pct: float | None = None) -> JsonRpcMessage:
    params: dict = {"meteringUsage": [{"unit": "credit", "value": value}]}
    if pct is not None:
        params["contextUsagePercentage"] = pct
    return JsonRpcMessage(method=METHOD_METADATA, params=params)


def _pct(value: float) -> JsonRpcMessage:
    return JsonRpcMessage(method=METHOD_METADATA, params={"contextUsagePercentage": value})


def _completed() -> JsonRpcMessage:
    return JsonRpcMessage(
        method=METHOD_COMPACTION_STATUS,
        params={"status": {"type": "completed"}, "summary": "ok"},
    )


def _end_turn() -> JsonRpcMessage:
    return JsonRpcMessage(id=_REQ_ID, result={"stopReason": "end_turn"})


def _client(tmp_path) -> tuple[AcpClient, list[JsonRpcMessage]]:
    """A client whose reads come from the returned list, then silence."""
    frames: list[JsonRpcMessage] = []
    client = AcpClient(work_dir=tmp_path)
    client._session_id = "s1"
    client.ensure_ready = AsyncMock()
    client._send_prompt = AsyncMock(return_value=_REQ_ID)
    client._send_request = AsyncMock(return_value=_REQ_ID)
    client._is_process_alive = lambda: True

    async def _read(timeout: float = 0.0):
        return frames.pop(0) if frames else None

    client._read_message = _read
    return client, frames


async def _turn_via_stream_events(client: AcpClient) -> float:
    complete = [e async for e in client.stream_events("next", timeout=5.0)]
    complete = [e for e in complete if e.kind == EVENT_COMPLETE]
    assert len(complete) == 1
    return complete[0].usage.credits


async def _turn_via_send_message(client: AcpClient) -> float:
    await client.send_message("next", timeout=5.0)
    return client.last_prompt_stats.credits


async def _turn_via_send_message_stream(client: AcpClient) -> float:
    async for _ in client.send_message_stream("next", timeout=5.0):
        pass
    return client.last_prompt_stats.credits


_TURNS = {
    "stream_events": _turn_via_stream_events,
    "send_message": _turn_via_send_message,
    "send_message_stream": _turn_via_send_message_stream,
}


async def _turn(client: AcpClient, frames: list, entry: str, *before_end) -> float:
    frames.extend([*before_end, _end_turn()])
    return await _TURNS[entry](client)


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", sorted(_TURNS))
@pytest.mark.parametrize(
    "shape",
    ["metering_after_completed", "metering_before_completed", "metering_with_pct_after_completed"],
)
async def test_credits_read_by_wait_for_compaction_are_billed_once_by_the_next_turn(
    tmp_path, shape, entry
):
    client, frames = _client(tmp_path)
    frames.extend(
        {
            "metering_after_completed": [_completed(), _metering(0.5), _pct(5.0)],
            "metering_before_completed": [_metering(0.5), _completed(), _pct(5.0)],
            "metering_with_pct_after_completed": [_completed(), _metering(0.5, pct=5.0)],
        }[shape]
    )

    result = await client.wait_for_compaction(timeout=5.0)

    assert result["type"] == "completed"
    assert client.last_prompt_stats.context_pct == 5.0
    assert frames == []
    assert await _turn(client, frames, entry) == pytest.approx(0.5)
    # Billed exactly once: the turn after that owes nothing.
    assert await _turn(client, frames, entry) == 0.0


@pytest.mark.asyncio
async def test_credits_read_by_the_cached_result_grace_drain_go_to_the_next_turn(tmp_path):
    """``AcpProvider.wait_for_compaction`` consumes a terminal ``compact()``
    caught inside its own turn and calls only the grace drain."""
    client, frames = _client(tmp_path)
    frames.extend([_metering(0.5), _pct(5.0)])

    await client._drain_post_compaction_metadata()

    assert frames == []
    assert await _turn(client, frames, "stream_events") == pytest.approx(0.5)
    assert await _turn(client, frames, "stream_events") == 0.0


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", sorted(_TURNS))
async def test_credits_reported_inside_a_turn_stay_on_that_turn(tmp_path, entry):
    client, frames = _client(tmp_path)

    assert await _turn(client, frames, entry, _metering(0.25)) == pytest.approx(0.25)
    assert await _turn(client, frames, entry) == 0.0


@pytest.mark.asyncio
async def test_credits_reported_inside_a_slash_command_stay_on_that_command(tmp_path):
    """``stream_command`` (the autocompact path) never clears ``_turn_done``,
    so turn state is no signal for which window a credit belongs to."""
    client, frames = _client(tmp_path)
    frames.extend([_metering(0.25), _end_turn()])

    events = [e async for e in client.stream_command("/compact", timeout=5.0)]

    complete = [e for e in events if e.kind == EVENT_COMPLETE]
    assert [e.usage.credits for e in complete] == [pytest.approx(0.25)]
    assert await _turn(client, frames, "stream_events") == 0.0


@pytest.mark.asyncio
async def test_a_claimed_client_does_not_bill_credits_held_for_the_previous_session(tmp_path):
    client, frames = _client(tmp_path)
    frames.extend([_completed(), _metering(0.5), _pct(5.0)])
    await client.wait_for_compaction(timeout=5.0)

    client.rekey("dashboard:claimer", "chan-2")

    assert await _turn(client, frames, "stream_events") == 0.0
