"""Tests for allowed_users config fallback in sender display resolution."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.config.loader import KiroCrewConfig, MessagingConfig, SlackConfig
from kiro_crew.slack.events import SeenCache, _route_message


def _make_orch(allowed_users: list[dict] | None = None) -> MagicMock:
    """Build a minimal mock orchestrator with allowed_users config."""
    orch = MagicMock()
    slack_cfg = SlackConfig(allowed_users=allowed_users or [])
    orch._cfg = KiroCrewConfig(slack=slack_cfg, messaging=MessagingConfig(use_transport=False))
    orch.channel_history = MagicMock()
    orch.channel_history._user_names = {}
    orch.slack = MagicMock()
    orch.slack.get_user_info = AsyncMock(return_value={})
    orch.sessions = AsyncMock()
    orch.sessions.enqueue = MagicMock(return_value=False)
    orch.sessions.is_cancelled = MagicMock(return_value=False)
    orch.sessions.dequeue = MagicMock(return_value=None)
    orch.sessions.clear_queue = MagicMock()
    orch.ctx_builder = None
    orch.cron_svc = None
    orch.conv_log = None
    orch.consolidator = None
    orch.subagent_mgr = None
    orch.task_runner = None
    orch._handler_tasks = set()
    orch._session_tasks = {}
    orch._pending_queue = {}
    return orch


class TestUserResolutionFallback:
    """Verify config fallback fires when Slack API returns raw ID as display name."""

    @pytest.mark.asyncio
    async def test_fallback_resolves_from_allowed_users(self):
        """When get_user_info returns no real_name, allowed_users config is used."""
        orch = _make_orch(
            allowed_users=[{"slack_id": "U055HN562JG", "name": "shahtani"}],
        )
        # get_user_info returns empty dict -> _sender_display = sender_id
        orch.slack.get_user_info = AsyncMock(return_value={})
        seen = SeenCache()
        event = {
            "user": "U055HN562JG",
            "channel": "D1234",
            "text": "hello",
            "ts": "1.0",
            "team": "TTEST",
        }

        with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as mock_hm:
            with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
                await _route_message(orch, event, seen, is_mention=False)
                await asyncio.sleep(0)
                tasks = list(orch._handler_tasks)
                assert len(tasks) == 1
                await asyncio.gather(*tasks, return_exceptions=True)
                mock_hm.assert_called_once()
                # Verify the resolved name was passed, not the raw Slack ID
                call_kwargs = mock_hm.call_args
                assert call_kwargs.kwargs.get("user_display_name") == "shahtani"
                # Verify cache was updated so fallback doesn't re-run next message
                orch.channel_history.set_user_name.assert_called_with(
                    "U055HN562JG", "shahtani"
                )

    @pytest.mark.asyncio
    async def test_fallback_skipped_when_real_name_resolved(self):
        """When get_user_info returns a real_name, config fallback is not needed."""
        orch = _make_orch(
            allowed_users=[{"slack_id": "U055HN562JG", "name": "shahtani"}],
        )
        orch.slack.get_user_info = AsyncMock(return_value={"real_name": "Tanish Shah"})
        seen = SeenCache()
        event = {
            "user": "U055HN562JG",
            "channel": "D1234",
            "text": "hello",
            "ts": "2.0",
            "team": "TTEST",
        }

        with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as mock_hm:
            with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
                await _route_message(orch, event, seen, is_mention=False)
                await asyncio.sleep(0)
                tasks = list(orch._handler_tasks)
                assert len(tasks) == 1
                await asyncio.gather(*tasks, return_exceptions=True)
                mock_hm.assert_called_once()
                # Slack API name is used, not config name
                assert mock_hm.call_args.kwargs.get("user_display_name") == "Tanish Shah"

    @pytest.mark.asyncio
    async def test_fallback_no_match_in_config(self):
        """When user is not in allowed_users, _sender_display stays as raw ID."""
        orch = _make_orch(
            allowed_users=[{"slack_id": "U_OTHER", "name": "someone"}],
        )
        orch.slack.get_user_info = AsyncMock(return_value={})
        seen = SeenCache()
        event = {
            "user": "U055HN562JG",
            "channel": "D1234",
            "text": "hello",
            "ts": "3.0",
            "team": "TTEST",
        }

        with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as mock_hm:
            with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
                await _route_message(orch, event, seen, is_mention=False)
                await asyncio.sleep(0)
                tasks = list(orch._handler_tasks)
                assert len(tasks) == 1
                await asyncio.gather(*tasks, return_exceptions=True)
                mock_hm.assert_called_once()
                # Falls back to raw sender_id since no config match
                assert mock_hm.call_args.kwargs.get("user_display_name") == "U055HN562JG"
