"""A channel member's account check before each message, in its own module."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew.channel_account_check import RESTART_FAILED_TEXT, session_after_account_check


class _Sessions:
    def __init__(self, *, flagged: bool) -> None:
        self.flagged = flagged
        self.checks = 0

    async def run_identity_check(self) -> None:
        self.checks += 1

    def is_retiring(self, key: str) -> bool:
        return self.flagged


def _agent() -> SimpleNamespace:
    return SimpleNamespace(id="a1", role="dev", session_key="channel:c1:a1", state="listening")


@pytest.mark.asyncio
async def test_an_unflagged_session_keeps_its_client() -> None:
    sessions, channel, client = (
        _Sessions(flagged=False),
        SimpleNamespace(post=AsyncMock()),
        object(),
    )
    replace = AsyncMock()

    assert (
        await session_after_account_check(sessions, _agent(), client, channel, "t1", replace)
        is client
    )
    assert sessions.checks == 1
    replace.assert_not_awaited()
    channel.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_flagged_session_is_replaced_before_the_message() -> None:
    sessions, channel, agent = _Sessions(flagged=True), SimpleNamespace(post=AsyncMock()), _agent()
    fresh = object()
    replace = AsyncMock(return_value=fresh)

    assert (
        await session_after_account_check(sessions, agent, object(), channel, "t1", replace)
        is fresh
    )
    replace.assert_awaited_once_with(sessions, agent)
    channel.post.assert_not_awaited()
    assert agent.state == "listening"


@pytest.mark.asyncio
async def test_a_failed_replacement_fails_the_member_and_tells_the_channel() -> None:
    sessions, channel, agent = _Sessions(flagged=True), SimpleNamespace(post=AsyncMock()), _agent()

    result = await session_after_account_check(
        sessions, agent, object(), channel, "t1", AsyncMock(return_value=None)
    )

    assert result is None
    assert agent.state == "failed"
    channel.post.assert_awaited_once_with(
        "a1", RESTART_FAILED_TEXT, from_role="dev", msg_type="system", thread_id="t1"
    )
