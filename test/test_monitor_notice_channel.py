"""Monitoring-loop stop/finish notices ride their own ``system.monitor`` channel.

A separate channel from ``system.agent``, which carries the notes agents write
when a human must decide something, lets a user mute the "watch finished"
notices alone. These tests pin the split end to end: the channel is registered and
mutable, both gateway producers (structured monitor and legacy loop) land on
it with their legacy ``kind`` unchanged, muting it leaves ``system.agent``
alone, and the Settings channel listing shows it.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew.autonudge import NudgeLoop
from kiro_crew.dashboard.handlers.messaging import api_notification_channels
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.monitoring.models import MonitorOutcome, MonitorState
from kiro_crew.notifications.bus import (
    MONITOR_CHANNEL,
    SYSTEM_CHANNELS,
    NotificationValidationError,
    payload_from_legacy,
)
from kiro_crew.notifications.settings import PROTECTED_CHANNELS
from kiro_crew.slack.gateway import GatewayOrchestrator


def _make_state(monkeypatch, tmp_path) -> DashboardState:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.notifications.settings.config_dir", lambda: tmp_path)
    return DashboardState(
        sessions=MagicMock(count=0),
        crons=MagicMock(),
        lessons=MagicMock(),
        start_time=0.0,
    )


def _orch(state: DashboardState) -> SimpleNamespace:
    # _notify_nudge_expired only reads dashboard_state and _notif_meta.
    return SimpleNamespace(dashboard_state=state, _notif_meta=GatewayOrchestrator._notif_meta)


def _merged_monitor_loop() -> NudgeLoop:
    loop = NudgeLoop(id="monitor-1", slot_key="chat-1-1700000000", message="")
    loop.active = False
    loop.monitor = MonitorState(
        kind="github_pull_request",
        target="https://github.com/acme/widgets/pull/7",
        objective="review_ready",
        created_ts=1.0,
        outcome=MonitorOutcome.SUCCESS,
        stopped_at=2.0,
        stopped_reason="pull_request_merged",
    )
    return loop


def _capped_legacy_loop() -> NudgeLoop:
    return NudgeLoop(
        id="loop-x",
        slot_key="chat-7-1700000000",
        message="babysit the PR",
        idle_secs=300,
        max_cycles=24,
        cycle_count=24,
    )


class TestChannelRegistration:
    def test_monitor_channel_is_a_system_channel_with_the_agent_default(self):
        # Same default as system.agent, so nothing changes until the user acts.
        assert MONITOR_CHANNEL == "system.monitor"
        assert SYSTEM_CHANNELS[MONITOR_CHANNEL] == SYSTEM_CHANNELS["system.agent"]

    def test_monitor_channel_is_not_protected(self, monkeypatch, tmp_path):
        assert MONITOR_CHANNEL not in PROTECTED_CHANNELS
        state = _make_state(monkeypatch, tmp_path)
        state.notification_channel_settings.update(MONITOR_CHANNEL, muted=True, priority="passive")
        assert state.notification_channel_settings.get(MONITOR_CHANNEL) == {
            "muted": True,
            "priority": "passive",
        }


class TestLegacyChannelOverride:
    def test_channel_override_keeps_the_legacy_kind(self):
        payload = payload_from_legacy("agent", "t", "b", channel=MONITOR_CHANNEL)
        assert payload.channel == MONITOR_CHANNEL
        assert payload.kind == "agent"

    def test_no_override_still_maps_kind_to_its_channel(self):
        assert payload_from_legacy("agent", "t", "b").channel == "system.agent"

    @pytest.mark.parametrize("channel", ["system.nope", "someapp.alerts", ""])
    def test_override_must_name_a_system_channel(self, channel):
        with pytest.raises(NotificationValidationError):
            payload_from_legacy("agent", "t", "b", channel=channel)


class TestGatewayNoticesLandOnMonitorChannel:
    @pytest.mark.parametrize("make_loop", [_merged_monitor_loop, _capped_legacy_loop])
    def test_notice_lands_on_system_monitor_with_kind_agent(self, monkeypatch, tmp_path, make_loop):
        state = _make_state(monkeypatch, tmp_path)
        assert GatewayOrchestrator._notify_nudge_expired(_orch(state), make_loop()) is True
        [note] = state._notification_log
        assert note["channel"] == MONITOR_CHANNEL
        assert note["kind"] == "agent"  # feed badge and per-kind sound unchanged
        assert note["priority"] == SYSTEM_CHANNELS["system.agent"]

    def test_muting_monitor_silences_notices_but_not_agent_notes(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        state.notification_channel_settings.update(MONITOR_CHANNEL, muted=True)

        GatewayOrchestrator._notify_nudge_expired(_orch(state), _merged_monitor_loop())
        GatewayOrchestrator._notify_nudge_expired(_orch(state), _capped_legacy_loop())
        state.notify("agent", "PR #1 parked: needs your call", "Decide the design question.")

        monitor_notes = [n for n in state._notification_log if n["channel"] == MONITOR_CHANNEL]
        [agent_note] = [n for n in state._notification_log if n["channel"] == "system.agent"]
        assert len(monitor_notes) == 2
        assert all(n["silenced"] is True and n["priority"] == "passive" for n in monitor_notes)
        assert "silenced" not in agent_note
        assert agent_note["priority"] == "default"
        assert state._unread_count == 1  # only the agent note counts

    def test_muting_agent_no_longer_silences_monitor_notices(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        state.notification_channel_settings.update("system.agent", muted=True)
        GatewayOrchestrator._notify_nudge_expired(_orch(state), _merged_monitor_loop())
        [note] = state._notification_log
        assert "silenced" not in note


class TestSettingsListing:
    @pytest.mark.asyncio
    async def test_channels_endpoint_lists_monitor_as_mutable(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        request = MagicMock()
        request.app = {"state": state}
        resp = await api_notification_channels(request)
        channels = {c["channel"]: c for c in json.loads(resp.body)["channels"]}
        entry = channels[MONITOR_CHANNEL]
        assert entry["source"] == "system"
        assert entry["registered"] is True
        assert entry["protected"] is False
        assert entry["default_priority"] == "default"
