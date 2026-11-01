"""Regression: a Slack observe channel records each message exactly once.

A Slack @mention is delivered as BOTH a ``message`` event (routed with
``is_mention=False``) and an ``app_mention`` event (``is_mention=True``) that
share one ``msg_ts``, and socket mode can redeliver an envelope. Observe mode
records channel history above the ``msg_ts`` turn-dedup, so before the
``observe:`` push guard the same message was written to history twice while the
turn itself ran once.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from test_channel_activation import _make_orch

from kiro_crew.channel_history import ChannelHistory
from kiro_crew.config.loader import ACTIVATION_OBSERVE, ChannelConfig
from kiro_crew.slack.events import SeenCache, _route_message


def _observe_orch(tmp_path: Path) -> tuple[object, ChannelHistory, SeenCache]:
    orch = _make_orch(channels={"C1": ChannelConfig(activation=ACTIVATION_OBSERVE)})
    hist = ChannelHistory(history_dir=tmp_path / "history")
    hist.set_observe("C1")
    orch.channel_history = hist
    orch.slack.get_user_info = AsyncMock(return_value={"real_name": "Alice"})
    return orch, hist, SeenCache()


@pytest.mark.asyncio
async def test_observe_mention_pair_recorded_once(tmp_path: Path) -> None:
    """The message + app_mention pair for one ts yields one history entry."""
    orch, hist, seen = _observe_orch(tmp_path)
    event = {"user": "U1", "channel": "C1", "text": "<@UBOT> status?", "ts": "100.1", "team": "T1"}

    with (
        patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as mock_hm,
        patch("kiro_crew.slack.events.is_allowed_user", return_value=True),
    ):
        await _route_message(orch, dict(event), seen, is_mention=False)  # `message` event
        await _route_message(orch, dict(event), seen, is_mention=True)  # `app_mention` event
        await asyncio.sleep(0)
        await asyncio.gather(*list(orch._handler_tasks), return_exceptions=True)

    assert hist.entry_count("C1") == 1, "one Slack message recorded twice in observe history"
    # The turn still runs exactly once: only the app_mention event reaches the
    # turn-dedup and dispatches, so the fix does not change turn behavior.
    mock_hm.assert_called_once()


@pytest.mark.asyncio
async def test_observe_redelivered_message_recorded_once(tmp_path: Path) -> None:
    """A socket-mode envelope redelivery of one message is recorded once."""
    orch, hist, seen = _observe_orch(tmp_path)
    event = {"user": "U1", "channel": "C1", "text": "deploy finished", "ts": "200.2", "team": "T1"}

    with (
        patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock),
        patch("kiro_crew.slack.events.is_allowed_user", return_value=True),
    ):
        await _route_message(orch, dict(event), seen, is_mention=False)
        await _route_message(orch, dict(event), seen, is_mention=False)  # redelivered envelope
        await asyncio.sleep(0)
        await asyncio.gather(*list(orch._handler_tasks), return_exceptions=True)

    assert hist.entry_count("C1") == 1, "a redelivered observe message recorded twice"
