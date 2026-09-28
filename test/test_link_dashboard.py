"""Tests for the 'Link to Dashboard' button feature.

Covers:
1. format.py — LINK_DASHBOARD_ACTION constant and build_link_dashboard_button()
2. handler.py — footer_blocks Link to Dashboard button logic
3. interactions.py — mc_link_dashboard action handler
4. dashboard/chat.py — real-time Slack stream mirroring
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.slack.format import (
    LINK_DASHBOARD_ACTION,
    build_link_dashboard_button,
    split_message,
)
from kiro_crew.slack.handler import _append_footer_actions

# ═══════════════════════════════════════════════════════════════════════════
# 1. format.py — LINK_DASHBOARD_ACTION + build_link_dashboard_button
# ═══════════════════════════════════════════════════════════════════════════


class TestLinkDashboardAction:
    def test_constant_value(self) -> None:
        assert LINK_DASHBOARD_ACTION == "mc_link_dashboard"

    def test_build_link_dashboard_button_structure(self) -> None:
        btn = build_link_dashboard_button()
        assert btn["type"] == "button"
        assert btn["action_id"] == LINK_DASHBOARD_ACTION
        assert btn["text"]["type"] == "plain_text"
        assert "Dashboard" in btn["text"]["text"]

    def test_build_link_dashboard_button_no_style(self) -> None:
        """Button should not have a style (not primary/danger)."""
        btn = build_link_dashboard_button()
        assert "style" not in btn


# ═══════════════════════════════════════════════════════════════════════════
# 2. handler.py — footer_blocks with Link to Dashboard button
# ═══════════════════════════════════════════════════════════════════════════


class TestFooterBlocksLinkDashboard:
    """Test the footer_blocks logic that conditionally adds the Link to Dashboard button."""

    def _build_footer(
        self,
        thread_ts: str,
        linked_session_key: str | None,
        dashboard_state: object | None,
        options: list[str] | None = None,
    ) -> list[dict]:
        """Build footer blocks using the real production helper."""
        footer_blocks: list[dict] = [
            {"type": "context", "elements": [{"type": "mrkdwn", "text": "Finished in 5s"}]}
        ]
        return _append_footer_actions(
            footer_blocks, options, thread_ts, linked_session_key, dashboard_state,
        )

    def test_button_added_when_in_thread_not_linked_with_dashboard(self) -> None:
        blocks = self._build_footer("1234.0", None, MagicMock())
        actions = [b for b in blocks if b.get("type") == "actions"]
        assert len(actions) == 1
        assert any(
            e["action_id"] == LINK_DASHBOARD_ACTION for e in actions[0]["elements"]
        )

    def test_button_not_added_when_no_thread_ts(self) -> None:
        blocks = self._build_footer("", None, MagicMock())
        actions = [b for b in blocks if b.get("type") == "actions"]
        assert len(actions) == 0

    def test_button_not_added_when_already_linked(self) -> None:
        blocks = self._build_footer("1234.0", "dashboard:slot1", MagicMock())
        actions = [b for b in blocks if b.get("type") == "actions"]
        assert len(actions) == 0

    def test_button_not_added_when_no_dashboard_state(self) -> None:
        blocks = self._build_footer("1234.0", None, None)
        actions = [b for b in blocks if b.get("type") == "actions"]
        assert len(actions) == 0

    def test_button_appended_to_existing_actions_block(self) -> None:
        """When OPTIONS are present, button is appended to the existing actions block."""
        blocks = self._build_footer(
            "1234.0", None, MagicMock(), options=["A", "B"]
        )
        actions = [b for b in blocks if b.get("type") == "actions"]
        assert len(actions) == 1
        action_ids = [e["action_id"] for e in actions[0]["elements"]]
        assert LINK_DASHBOARD_ACTION in action_ids
        # OPTIONS checkboxes are also present
        assert any(aid == "options_checkboxes" for aid in action_ids)


# ═══════════════════════════════════════════════════════════════════════════
# 3. interactions.py — mc_link_dashboard action handler
# ═══════════════════════════════════════════════════════════════════════════


def _make_orch_for_link() -> MagicMock:
    orch = MagicMock()
    orch.slack = MagicMock()
    orch.slack.fetch_thread_replies = AsyncMock(
        return_value=[
            {"user": "U1", "text": "hello"},
            {"bot_id": "B1", "text": "hi there"},
            {"user": "U1", "text": "thanks"},
        ]
    )
    slot = MagicMock()
    slot.key = "slot_abc"
    slot.append = MagicMock()
    ds = MagicMock()
    ds.get_linked_slot = MagicMock(return_value=None)
    ds.get_or_create_slot = MagicMock(return_value=slot)
    ds._self_bot_id = "B1"
    ds.link_slack = MagicMock()
    ds.push_slots_update = MagicMock()
    orch.dashboard_state = ds
    return orch


@pytest.fixture
def link_orch(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    from kiro_crew.slack import interactions

    orch = _make_orch_for_link()
    monkeypatch.setattr(interactions, "_orch", orch)
    monkeypatch.setattr(interactions, "is_allowed_user", lambda uid: True)
    return orch


@pytest.mark.asyncio
async def test_link_dashboard_creates_slot_and_imports(link_orch: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.slack import interactions

    mock_sel_inst = MagicMock()
    monkeypatch.setattr(interactions, "sel", lambda: mock_sel_inst)

    payload = {
        "type": "block_actions",
        "user": {"id": "U1"},
        "channel": {"id": "C1"},
        "message": {"ts": "200.0", "thread_ts": "100.0", "blocks": []},
        "actions": [{"action_id": "mc_link_dashboard", "value": ""}],
        "response_url": "",
    }

    with patch("kiro_crew.dashboard.chat._save_slot_to_history"):
        await interactions.dispatch(payload)

    ds = link_orch.dashboard_state
    ds.get_or_create_slot.assert_called_once()
    slot = ds.get_or_create_slot.return_value
    assert slot.append.call_count == 3
    roles = [c.args[0] for c in slot.append.call_args_list]
    assert roles == ["user", "assistant", "user"]
    ds.link_slack.assert_called_once_with("slot_abc", "100.0", "C1")
    ds.push_slots_update.assert_called_once()
    mock_sel_inst.log_tool_invocation.assert_called_once()


@pytest.mark.asyncio
async def test_link_dashboard_null_slot_returns(link_orch: MagicMock) -> None:
    """When _import_thread_to_slot returns None, handler should return without crash."""
    from kiro_crew.slack import interactions

    link_orch.slack.fetch_thread_replies = AsyncMock(return_value=[])

    payload = {
        "type": "block_actions",
        "user": {"id": "U1"},
        "channel": {"id": "C1"},
        "message": {"ts": "200.0", "thread_ts": "100.0", "blocks": []},
        "actions": [{"action_id": "mc_link_dashboard", "value": ""}],
        "response_url": "",
    }
    await interactions.dispatch(payload)
    # No crash, no slot created
    link_orch.dashboard_state.link_slack.assert_not_called()


@pytest.mark.asyncio
async def test_link_dashboard_no_thread_ts_returns(link_orch: MagicMock) -> None:
    from kiro_crew.slack import interactions

    payload = {
        "type": "block_actions",
        "user": {"id": "U1"},
        "channel": {"id": "C1"},
        "message": {"ts": "200.0", "blocks": []},
        "actions": [{"action_id": "mc_link_dashboard", "value": ""}],
        "container": {},
    }
    await interactions.dispatch(payload)
    link_orch.dashboard_state.get_or_create_slot.assert_not_called()


@pytest.mark.asyncio
async def test_link_dashboard_no_dashboard_state_returns(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.slack import interactions

    orch = MagicMock()
    orch.slack = MagicMock()
    orch.dashboard_state = None
    monkeypatch.setattr(interactions, "_orch", orch)
    monkeypatch.setattr(interactions, "is_allowed_user", lambda uid: True)

    payload = {
        "type": "block_actions",
        "user": {"id": "U1"},
        "channel": {"id": "C1"},
        "message": {"ts": "200.0", "thread_ts": "100.0", "blocks": []},
        "actions": [{"action_id": "mc_link_dashboard", "value": ""}],
    }
    await interactions.dispatch(payload)
    # No crash, no slot created


@pytest.mark.asyncio
async def test_link_dashboard_skips_link_command_messages(link_orch: MagicMock) -> None:
    """Messages starting with '!link-to-dashboard' should be skipped."""
    from kiro_crew.slack import interactions

    link_orch.slack.fetch_thread_replies = AsyncMock(
        return_value=[
            {"user": "U1", "text": "!link-to-dashboard"},
            {"user": "U1", "text": "real message"},
        ]
    )

    payload = {
        "type": "block_actions",
        "user": {"id": "U1"},
        "channel": {"id": "C1"},
        "message": {"ts": "200.0", "thread_ts": "100.0", "blocks": []},
        "actions": [{"action_id": "mc_link_dashboard", "value": ""}],
        "response_url": "",
    }
    with patch("kiro_crew.dashboard.chat._save_slot_to_history"):
        await interactions.dispatch(payload)

    slot = link_orch.dashboard_state.get_or_create_slot.return_value
    # Only 1 message imported (the "real message"), not the !link-to-dashboard
    assert slot.append.call_count == 1
    assert slot.append.call_args.args[1] == "real message"


@pytest.mark.asyncio
async def test_link_dashboard_unauthorized_user(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.slack import interactions

    orch = _make_orch_for_link()
    monkeypatch.setattr(interactions, "_orch", orch)
    monkeypatch.setattr(interactions, "is_allowed_user", lambda uid: False)

    payload = {
        "type": "block_actions",
        "user": {"id": "UBAD"},
        "channel": {"id": "C1"},
        "message": {"ts": "200.0", "thread_ts": "100.0", "blocks": []},
        "actions": [{"action_id": "mc_link_dashboard", "value": ""}],
    }
    await interactions.dispatch(payload)
    orch.dashboard_state.get_or_create_slot.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# 4. dashboard/chat.py — Slack stream mirroring
# ═══════════════════════════════════════════════════════════════════════════


class TestMirrorStreamLogic:
    """Test split_message used by the mirror stream."""

    @pytest.mark.asyncio
    async def test_long_response_split_into_parts(self) -> None:
        """Long responses should be split via split_message before posting."""
        text = "x" * 8000
        parts = split_message(text)
        assert len(parts) > 1
        for part in parts:
            assert len(part) <= 3900 + len("\n\n_(continued...)_") + 10

    def test_short_response_not_split(self) -> None:
        parts = split_message("short text")
        assert len(parts) == 1
        assert parts[0] == "short text"
