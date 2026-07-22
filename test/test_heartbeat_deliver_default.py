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


class TestDashboardNewSlotTitle:
    """Regression: heartbeat ``dashboard`` (new-slot) delivery must title the slot.

    A heartbeat slot receives only an assistant message and never a user
    message, so the interactive LLM auto-titler (``_maybe_auto_title``, gated on
    ``user_count >= 1``) never fires. Without an explicit title the slot is stuck
    on the ``NEW_SESSION_TITLE`` placeholder ("New Session…") forever — the
    reported bug. The delivery path must seed a title from the (already-redacted)
    task summary, lock ``_titled`` so ``display_title`` returns it, push it to the
    sidebar, and persist it so it survives a gateway restart.
    """

    @pytest.mark.asyncio()
    async def test_new_slot_gets_title_from_task_summary(self, monkeypatch):
        orch, state = _make_orch("dashboard", monkeypatch)
        slot = state.get_or_create_slot.return_value
        await orch._deliver_result(
            "💓 Heartbeat", "Checking PR #163 CI status now.", "body", "dashboard"
        )
        assert slot.title == "💓 Checking PR #163 CI status now."
        assert slot._titled is True
        # Title pushed to the sidebar via the dedicated SSE event.
        state.push_slot_title.assert_called_once_with(slot.key, slot.title)

    @pytest.mark.asyncio()
    async def test_new_slot_title_is_persisted(self, monkeypatch):
        orch, state = _make_orch("dashboard", monkeypatch)
        slot = state.get_or_create_slot.return_value
        await orch._deliver_result("💓 Heartbeat", "task summary", "body", "dashboard")
        state.conversation_log.set_title.assert_called_once()
        key_arg, title_arg = state.conversation_log.set_title.call_args.args
        assert key_arg == "dashboard:chat-1"  # _history_key_for(slot.key)
        assert title_arg == slot.title

    @pytest.mark.asyncio()
    async def test_new_slot_title_falls_back_when_summary_blank(self, monkeypatch):
        orch, state = _make_orch("dashboard", monkeypatch)
        slot = state.get_or_create_slot.return_value
        # Whitespace-only summary collapses to empty → use the generic title arg.
        await orch._deliver_result("💓 Heartbeat", "   ", "body", "dashboard")
        assert slot.title == "💓 Heartbeat"
        assert slot._titled is True

    @pytest.mark.asyncio()
    async def test_new_slot_title_capped_at_80_chars(self, monkeypatch):
        orch, state = _make_orch("dashboard", monkeypatch)
        slot = state.get_or_create_slot.return_value
        await orch._deliver_result("💓 Heartbeat", "x" * 200, "body", "dashboard")
        assert len(slot.title) <= 80

    @pytest.mark.asyncio()
    async def test_persist_failure_does_not_break_delivery(self, monkeypatch):
        """Title persistence is best-effort: a set_title error must not stop the
        assistant message from being appended or the slot update from firing."""
        orch, state = _make_orch("dashboard", monkeypatch)
        slot = state.get_or_create_slot.return_value
        state.conversation_log.set_title.side_effect = OSError("disk full")
        await orch._deliver_result("💓 Heartbeat", "task", "body", "dashboard")
        slot.append.assert_called_once()
        state.push_slots_update.assert_called_once()
        state.notify.assert_called_once()
