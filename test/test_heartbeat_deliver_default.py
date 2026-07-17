"""Tests for heartbeat.default_deliver config-driven routing in _deliver_result.

A heartbeat completion with no inline ``<!-- deliver:... -->`` tag (empty
``deliver``) routes per the ``heartbeat.default_deliver`` config:

- ``slack`` (default, backward compatible) -> Slack DM + dashboard notification
- ``dashboard``                            -> dashboard slot + bell only, no Slack

An explicit per-task deliver tag always overrides the config default.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_orch(default_deliver, monkeypatch):
    """Build a GatewayOrchestrator (bypassing __init__) wired with a Slack mock
    and a fake config whose heartbeat.default_deliver is *default_deliver*."""
    from kiro_crew.slack import gateway

    orch = gateway.GatewayOrchestrator.__new__(gateway.GatewayOrchestrator)

    state = MagicMock()
    slot = MagicMock()
    slot.key = "chat-1"
    state.get_or_create_slot.return_value = slot
    state.notify = MagicMock()
    state.push_slots_update = MagicMock()
    orch.dashboard_state = state

    slack = MagicMock()
    slack.open_dm = AsyncMock(return_value="D123")
    slack.post_message = AsyncMock()
    orch.slack = slack
    orch._owner_id = "U1"

    cfg = MagicMock()
    cfg.heartbeat.default_deliver = default_deliver
    monkeypatch.setattr(gateway.KiroCrewConfig, "load", lambda *a, **k: cfg)
    return orch, state


class TestDefaultDeliverConfig:
    @pytest.mark.asyncio()
    async def test_empty_deliver_dashboard_default_is_dashboard_only(self, monkeypatch):
        orch, state = _make_orch("dashboard", monkeypatch)
        await orch._deliver_result("💓 Heartbeat", "task", "body", "")
        state.get_or_create_slot.assert_called_once()
        state.notify.assert_called_once()
        orch.slack.open_dm.assert_not_called()  # no Slack DM

    @pytest.mark.asyncio()
    async def test_empty_deliver_slack_default_is_slack_plus_dashboard(self, monkeypatch):
        orch, state = _make_orch("slack", monkeypatch)
        await orch._deliver_result("💓 Heartbeat", "task", "body", "")
        orch.slack.open_dm.assert_awaited_once()
        orch.slack.post_message.assert_awaited_once()
        state.notify.assert_called_once()
        state.get_or_create_slot.assert_not_called()

    @pytest.mark.asyncio()
    async def test_explicit_slack_tag_overrides_dashboard_default(self, monkeypatch):
        orch, _ = _make_orch("dashboard", monkeypatch)
        await orch._deliver_result("💓 Heartbeat", "task", "body", "slack")
        # explicit slack branch posts the DM and never creates a dashboard slot
        orch.slack.open_dm.assert_awaited_once()
        orch.dashboard_state.get_or_create_slot.assert_not_called()
