"""Slack mid-turn on adapters: steer when supported, else enqueue immediately."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.slack.handler import (
    _FOLLOW_UP_ACK_REACTION,
    _STEER_ACK_REACTION,
    dequeue_busy_followup,
    inject_busy_followup,
)


def _sessions(
    *,
    busy: bool = True,
    provider: Any = None,
    enqueue_ok: bool = True,
    owner: str | None = None,
) -> MagicMock:
    sessions = MagicMock()
    sessions.get_session_for_thread.return_value = owner
    sessions.is_busy.return_value = busy
    sessions.get_provider.return_value = provider
    sessions.enqueue.return_value = enqueue_ok
    sessions.dequeue.return_value = None
    return sessions


def _provider(*, supports_steer: bool, steer_ok: bool = True) -> MagicMock:
    provider = MagicMock()
    provider.supports_steer = supports_steer
    provider.steer = AsyncMock(return_value=steer_ok)
    provider.has_active_turn = MagicMock(return_value=True)
    return provider


@pytest.mark.asyncio
async def test_idle_when_the_session_is_not_busy() -> None:
    sessions = _sessions(busy=False)
    result = await inject_busy_followup(sessions, "slack:1", "hi", "1.2")
    assert result == "idle"
    sessions.enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_adapter_enqueues_immediately_without_waiting() -> None:
    """Spec adapters stay out of ACP_BACKENDS_STEER; follow-up must not block."""
    provider = _provider(supports_steer=False)
    sessions = _sessions(provider=provider)
    slack = AsyncMock()

    result = await inject_busy_followup(
        sessions,
        "slack:1",
        "follow up",
        "1.2",
        slack=slack,
        channel="C1",
    )

    assert result == "follow_up"
    sessions.enqueue.assert_called_once()
    assert sessions.enqueue.call_args.kwargs["force"] is True
    provider.steer.assert_not_awaited()
    slack.add_reaction.assert_awaited_once_with("C1", "1.2", _FOLLOW_UP_ACK_REACTION)


@pytest.mark.asyncio
async def test_steer_when_the_named_capability_is_on() -> None:
    provider = _provider(supports_steer=True)
    sessions = _sessions(provider=provider)
    slack = AsyncMock()

    result = await inject_busy_followup(
        sessions,
        "slack:1",
        "nudge",
        "1.2",
        slack=slack,
        channel="C1",
    )

    assert result == "steer"
    provider.steer.assert_awaited_once_with("nudge")
    sessions.enqueue.assert_not_called()
    slack.add_reaction.assert_awaited_once_with("C1", "1.2", _STEER_ACK_REACTION)


@pytest.mark.asyncio
async def test_attachment_follow_up_is_queued_instead_of_steered() -> None:
    """Steering accepts text only, so the queue must retain attachment ownership."""
    provider = _provider(supports_steer=True)
    sessions = _sessions(provider=provider)
    image_paths = ["/tmp/kirocrew-slack-image.png"]

    result = await inject_busy_followup(
        sessions,
        "slack:1",
        "look at this image",
        "1.2",
        enqueue_kwargs={"image_temp_paths": image_paths},
    )

    assert result == "follow_up"
    provider.steer.assert_not_awaited()
    sessions.enqueue.assert_called_once()
    assert sessions.enqueue.call_args.kwargs["image_temp_paths"] == image_paths


@pytest.mark.asyncio
async def test_steer_false_falls_through_to_follow_up() -> None:
    provider = _provider(supports_steer=True, steer_ok=False)
    sessions = _sessions(provider=provider)

    result = await inject_busy_followup(sessions, "slack:1", "nudge", "1.2")

    assert result == "follow_up"
    sessions.enqueue.assert_called_once()


@pytest.mark.asyncio
async def test_linked_dashboard_owner_is_the_enqueue_key() -> None:
    provider = _provider(supports_steer=False)
    sessions = _sessions(provider=provider, owner="dashboard:chat-3")

    result = await inject_busy_followup(
        sessions,
        "1783733803.877979",
        "later",
        "1.2",
        thread_ts="1783733803.877979",
    )

    assert result == "follow_up"
    sessions.enqueue.assert_called_once()
    assert sessions.enqueue.call_args.args[0] == "dashboard:chat-3"
    sessions.get_provider.assert_called_once_with("dashboard:chat-3")


def test_drain_tries_the_linked_owner_first() -> None:
    sessions = MagicMock()
    sessions.get_session_for_thread.return_value = "dashboard:chat-3"
    sessions.dequeue.side_effect = [("ts", "text", {}), None]

    item = dequeue_busy_followup(sessions, "1783733803.877979")

    assert item == ("ts", "text", {})
    sessions.dequeue.assert_called_once_with("dashboard:chat-3")


@pytest.mark.asyncio
async def test_force_busy_covers_the_startup_race() -> None:
    provider = _provider(supports_steer=False)
    sessions = _sessions(busy=False, provider=provider)

    result = await inject_busy_followup(sessions, "slack:1", "early", "1.2", force_busy=True)

    assert result == "follow_up"
    sessions.enqueue.assert_called_once()
    assert sessions.enqueue.call_args.kwargs["force"] is True
