"""``AcpSessionProvider.stream_observe``: an engine-started turn behind the turn claim."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.client import AcpError
from kiro_crew.acp.runtime import AcpRuntimeDead, AcpRuntimeError
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.session_provider import AcpSessionProvider


def _provider(events=(), raises: BaseException | None = None):
    handle = AcpSessionHandle("s1", asyncio.Queue(), MagicMock())
    seen: list[float | None] = []

    async def _observe(timeout):
        seen.append(timeout)
        for event in events:
            yield event
        if raises is not None:
            raise raises

    handle.observe_turn = _observe
    provider = AcpSessionProvider(handle, runtime=MagicMock())
    provider.reclaim = MagicMock()
    provider._claim_shared_turn = MagicMock(return_value="claim-1")
    provider._end_shared_turn = AsyncMock()
    return provider, seen


async def _drain(provider, timeout=None):
    return [event async for event in provider.stream_observe(timeout)]


@pytest.mark.asyncio
async def test_it_yields_the_observed_turn_and_releases_the_claim():
    provider, seen = _provider(events=["a", "b"])
    assert await _drain(provider, 12.0) == ["a", "b"]
    assert seen == [12.0]
    provider.reclaim.assert_called_once_with()
    provider._end_shared_turn.assert_awaited_once_with("claim-1")


@pytest.mark.asyncio
async def test_a_failed_reclaim_does_not_cost_the_turn():
    provider, _ = _provider(events=["a"])
    provider.reclaim.side_effect = RuntimeError("stub gone")
    assert await _drain(provider) == ["a"]


@pytest.mark.asyncio
async def test_a_dead_runtime_is_translated_and_the_claim_still_released():
    dead = AcpRuntimeDead("process exited")
    provider, _ = _provider(events=["a"], raises=dead)
    translated = AcpError("died")
    provider._translate_dead = MagicMock(return_value=translated)
    with pytest.raises(AcpError) as caught:
        await _drain(provider)
    assert caught.value is translated
    provider._translate_dead.assert_called_once_with(dead)
    provider._end_shared_turn.assert_awaited_once_with("claim-1")


@pytest.mark.asyncio
async def test_a_runtime_error_surfaces_as_an_acp_error():
    provider, _ = _provider(raises=AcpRuntimeError("frame too large"))
    with pytest.raises(AcpError, match="frame too large"):
        await _drain(provider)
    provider._end_shared_turn.assert_awaited_once_with("claim-1")
