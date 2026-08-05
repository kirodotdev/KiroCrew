"""Tests for inline stop button and session_task_card End button.

Covers:
- build_working_blocks (blocks.py)
- session_task_card End button (blocks.py)
- _handle_inline_stop denied/allowed/idle paths (interactions.py)
- _handle_session_end key fallback (interactions.py)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.slack.blocks import build_working_blocks, session_task_card

# ---------------------------------------------------------------------------
# build_working_blocks
# ---------------------------------------------------------------------------


class TestBuildWorkingBlocks:
    def test_returns_context_and_actions(self):
        blocks = build_working_blocks("my-session-key")
        assert len(blocks) == 2
        assert blocks[0]["type"] == "context"
        assert blocks[1]["type"] == "actions"

    def test_action_id_contains_session_key(self):
        blocks = build_working_blocks("abc-123")
        btn = blocks[1]["elements"][0]
        assert btn["action_id"] == "mc_inline_stop_abc-123"
        assert btn["value"] == "abc-123"

    def test_stop_button_is_danger(self):
        blocks = build_working_blocks("k")
        btn = blocks[1]["elements"][0]
        assert btn["style"] == "danger"


# ---------------------------------------------------------------------------
# session_task_card — End button
# ---------------------------------------------------------------------------


class TestSessionTaskCard:
    def test_end_button_present(self):
        blocks = session_task_card(
            idx=0, key="sess-1", title="Test", agent="kirocrew",
            status="active", messages=[],
        )
        actions_block = blocks[1]
        assert actions_block["type"] == "actions"
        buttons = actions_block["elements"]
        end_btn = [b for b in buttons if "End" in b["text"]["text"]]
        assert len(end_btn) == 1
        assert end_btn[0]["action_id"] == "mc_session_end_sess-1"
        assert end_btn[0]["value"] == "sess-1"
        assert end_btn[0]["style"] == "danger"

    def test_resume_button_present(self):
        blocks = session_task_card(
            idx=0, key="s1", title="T", agent="a", status="paused", messages=[],
        )
        buttons = blocks[1]["elements"]
        resume_btn = [b for b in buttons if "Resume" in b["text"]["text"]]
        assert len(resume_btn) == 1

    def test_active_status_emoji(self):
        blocks = session_task_card(
            idx=0, key="s", title="T", agent="a", status="active", messages=[],
        )
        assert "🟢" in blocks[0]["title"]

    def test_paused_status_emoji(self):
        blocks = session_task_card(
            idx=0, key="s", title="T", agent="a", status="paused", messages=[],
        )
        assert "⏸️" in blocks[0]["title"]


# ---------------------------------------------------------------------------
# _handle_inline_stop
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_orch():
    """Create a mock orchestrator with sessions and slack."""
    orch = MagicMock()
    orch.slack = MagicMock()
    orch.slack.update_message = AsyncMock()
    orch.sessions = MagicMock()
    orch.sessions.stop_turn = AsyncMock(return_value="soft")
    return orch


@pytest.fixture
def setup_interactions(mock_orch, monkeypatch):
    """Set up interactions module state for testing."""
    import kiro_crew.slack.interactions as interactions
    monkeypatch.setattr(interactions, "_orch", mock_orch)
    # Set owner so is_owner returns True for U_OWNER
    from kiro_crew.slack.handler import set_owner_id
    set_owner_id("U_OWNER")
    return interactions


class TestHandleInlineStop:
    @pytest.mark.asyncio
    async def test_denied_for_non_owner(self, setup_interactions, mock_orch):
        """Non-owner clicks stop → SEL denied log, no stop_turn call."""
        interactions = setup_interactions
        payload = {"user": {"id": "U_OTHER"}}
        action = {"value": "sess-key"}

        with patch("kiro_crew.slack.interactions.sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            mock_sel.return_value.log_tool_invocation = MagicMock()
            await interactions._handle_inline_stop(payload, action, "C1", "ts1", "U_OTHER")

        # stop_turn should NOT be called
        mock_orch.sessions.stop_turn.assert_not_called()

    @pytest.mark.asyncio
    async def test_allowed_soft_stop(self, setup_interactions, mock_orch):
        """Owner clicks stop → stop_turn called, on_soft callback invoked."""
        interactions = setup_interactions

        # Make stop_turn invoke on_soft callback (simulating soft stop)
        async def _invoke_soft(key, on_soft=None, on_hard=None):
            if on_soft:
                await on_soft()
            return "soft"

        mock_orch.sessions.stop_turn = AsyncMock(side_effect=_invoke_soft)
        payload = {"user": {"id": "U_OWNER"}}
        action = {"value": "my-session"}

        with patch("kiro_crew.slack.interactions.sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            mock_sel.return_value.log_tool_invocation = MagicMock()
            await interactions._handle_inline_stop(payload, action, "C1", "msg_ts", "U_OWNER")

        # Immediate feedback: "Stopping..."
        mock_orch.slack.update_message.assert_any_call("C1", "msg_ts", text="⏹ _Stopping…_")
        # on_soft callback: "Execution stopped."
        mock_orch.slack.update_message.assert_any_call("C1", "msg_ts", text="⏹ Execution stopped.")
        # SEL tool invocation logged
        mock_sel.return_value.log_tool_invocation.assert_called_once()

    @pytest.mark.asyncio
    async def test_hard_stop_callback(self, setup_interactions, mock_orch):
        """Hard stop invokes on_hard callback with reset message."""
        interactions = setup_interactions

        async def _invoke_hard(key, on_soft=None, on_hard=None):
            if on_hard:
                await on_hard()
            return "hard"

        mock_orch.sessions.stop_turn = AsyncMock(side_effect=_invoke_hard)
        payload = {"user": {"id": "U_OWNER"}}
        action = {"value": "sess-x"}

        with patch("kiro_crew.slack.interactions.sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            mock_sel.return_value.log_tool_invocation = MagicMock()
            await interactions._handle_inline_stop(payload, action, "C1", "ts1", "U_OWNER")

        mock_orch.slack.update_message.assert_any_call("C1", "ts1", text="⛔ Execution stopped — session reset.")

    @pytest.mark.asyncio
    async def test_idle_outcome_shows_nothing_running(self, setup_interactions, mock_orch):
        """stop_turn returns 'idle' → shows 'Nothing running.'"""
        interactions = setup_interactions
        mock_orch.sessions.stop_turn.return_value = "idle"
        payload = {"user": {"id": "U_OWNER"}}
        action = {"value": "idle-sess"}

        with patch("kiro_crew.slack.interactions.sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            mock_sel.return_value.log_tool_invocation = MagicMock()
            await interactions._handle_inline_stop(payload, action, "C1", "ts1", "U_OWNER")

        # Last update should be "Nothing running."
        calls = mock_orch.slack.update_message.call_args_list
        final_text = calls[-1][1]["text"] if calls[-1][1] else calls[-1][0][2]
        assert "Nothing running" in final_text

    @pytest.mark.asyncio
    async def test_no_session_key_returns_early(self, setup_interactions, mock_orch):
        """Empty session key → SEL 'invalid' log, no stop_turn."""
        interactions = setup_interactions
        payload = {"user": {"id": "U_OWNER"}}
        action = {"value": ""}

        with patch("kiro_crew.slack.interactions.sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            mock_sel.return_value.log_tool_invocation = MagicMock()
            await interactions._handle_inline_stop(payload, action, "C1", "ts1", "U_OWNER")

        mock_orch.sessions.stop_turn.assert_not_called()
        mock_sel.return_value.log_api_access.assert_called_once()
        assert mock_sel.return_value.log_api_access.call_args[1]["outcome"] == "invalid"


# ---------------------------------------------------------------------------
# _handle_session_end — key fallback logic
# ---------------------------------------------------------------------------


class TestHandleSessionEnd:
    @pytest.mark.asyncio
    async def test_end_by_session_id(self, setup_interactions, mock_orch):
        """Session end uses find_key_by_sid to resolve the key."""
        interactions = setup_interactions
        mock_orch.sessions.find_key_by_sid = MagicMock(return_value="resolved-key")
        mock_orch.sessions.has_session = MagicMock(return_value=False)
        mock_orch.sessions.remove = AsyncMock()
        mock_orch.consolidator = None
        payload = {"user": {"id": "U_OWNER"}, "response_url": ""}
        action = {"value": "sid-abc"}

        with patch("kiro_crew.slack.interactions.sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            await interactions._handle_session_end(payload, action, "C1", "ts1", "U_OWNER")

        mock_orch.sessions.remove.assert_called_once_with("resolved-key")

    @pytest.mark.asyncio
    async def test_end_falls_back_to_direct_key(self, setup_interactions, mock_orch):
        """When find_key_by_sid returns None, falls back to has_session(value)."""
        interactions = setup_interactions
        mock_orch.sessions.find_key_by_sid = MagicMock(return_value=None)
        mock_orch.sessions.has_session = MagicMock(return_value=True)
        mock_orch.sessions.remove = AsyncMock()
        mock_orch.consolidator = None
        payload = {"user": {"id": "U_OWNER"}, "response_url": ""}
        action = {"value": "direct-key"}

        with patch("kiro_crew.slack.interactions.sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            await interactions._handle_session_end(payload, action, "C1", "ts1", "U_OWNER")

        mock_orch.sessions.remove.assert_called_once_with("direct-key")

    @pytest.mark.asyncio
    async def test_end_rejected_for_non_owner(self, setup_interactions, mock_orch):
        """Non-owner cannot end sessions."""
        interactions = setup_interactions
        mock_orch.sessions.remove = AsyncMock()
        payload = {"user": {"id": "U_OTHER"}, "response_url": ""}
        action = {"value": "some-key"}

        with patch("kiro_crew.slack.interactions.sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            await interactions._handle_session_end(payload, action, "C1", "ts1", "U_OTHER")

        mock_orch.sessions.remove.assert_not_called()

    @pytest.mark.asyncio
    async def test_end_with_consolidator(self, setup_interactions, mock_orch):
        """Session end triggers consolidator before removal."""
        interactions = setup_interactions
        mock_orch.sessions.find_key_by_sid = MagicMock(return_value="key-1")
        mock_orch.sessions.has_session = MagicMock(return_value=False)
        mock_orch.sessions.remove = AsyncMock()
        mock_orch.consolidator = MagicMock()
        mock_orch.consolidator.consolidate_session = MagicMock()
        payload = {"user": {"id": "U_OWNER"}, "response_url": ""}
        action = {"value": "sid-with-consol"}

        with patch("kiro_crew.slack.interactions.sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            await interactions._handle_session_end(payload, action, "C1", "ts1", "U_OWNER")

        mock_orch.consolidator.consolidate_session.assert_called_once_with("key-1")
        mock_orch.sessions.remove.assert_called_once_with("key-1")

    @pytest.mark.asyncio
    async def test_end_updates_message_when_no_response_url(self, setup_interactions, mock_orch):
        """Without response_url, falls back to update_message."""
        interactions = setup_interactions
        mock_orch.sessions.find_key_by_sid = MagicMock(return_value="k1")
        mock_orch.sessions.has_session = MagicMock(return_value=False)
        mock_orch.sessions.remove = AsyncMock()
        mock_orch.consolidator = None
        payload = {"user": {"id": "U_OWNER"}, "response_url": ""}
        action = {"value": "sid-123456789abc"}

        with patch("kiro_crew.slack.interactions.sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            await interactions._handle_session_end(payload, action, "C1", "ts1", "U_OWNER")

        mock_orch.slack.update_message.assert_called_once()
        call_args = mock_orch.slack.update_message.call_args
        assert "ended" in call_args[1]["text"]
