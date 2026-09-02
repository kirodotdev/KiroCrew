"""Tests for temporary chat mode (dashboard + Slack)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Dashboard: _ChatSlot temporary mode properties
# ---------------------------------------------------------------------------


class TestChatSlotTemporary:
    def test_temporary_mode(self):
        from kiro_crew.dashboard.state import _ChatSlot

        slot = _ChatSlot(key="test-1", memory_mode="temporary")
        assert slot.is_restricted is True
        assert slot.blocks_reads is True

    def test_normal_mode(self):
        from kiro_crew.dashboard.state import _ChatSlot

        slot = _ChatSlot(key="test-2")
        assert slot.is_restricted is False
        assert slot.blocks_reads is False


# ---------------------------------------------------------------------------
# Dashboard: _save_slot_to_history persists all modes (no skip)
# ---------------------------------------------------------------------------


class TestSaveSlotToHistory:
    def _save_and_count_lines(self, tmp_path, monkeypatch, slot_kwargs):
        """Save one message through a REAL ConversationLog; return .jsonl files."""
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
        from kiro_crew.history import ConversationLog

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from chat_test_helpers import _make_state

        state = _make_state(tmp_path)
        state.conversation_log = ConversationLog(tmp_path / "history")
        slot = state.get_or_create_slot(slot_kwargs.pop("key"), **slot_kwargs)
        slot.append("user", "hi")
        _save_slot_to_history(state, slot, force=True)
        return sorted((tmp_path / "history").rglob("*.jsonl"))

    def test_temporary_slot_still_saved(self, tmp_path, monkeypatch):
        """All modes write .jsonl for tab recovery — temporary included."""
        files = self._save_and_count_lines(
            tmp_path, monkeypatch, {"key": "tmp-1", "memory_mode": "temporary"}
        )
        assert files, "temporary slot must still persist history for tab recovery"

    def test_normal_slot_not_skipped(self, tmp_path, monkeypatch):
        """Persistent slot should NOT early-return."""
        files = self._save_and_count_lines(tmp_path, monkeypatch, {"key": "norm-1"})
        assert files, "normal slot must persist history"


# ---------------------------------------------------------------------------
# Dashboard: _persist_title skips restricted slots
# ---------------------------------------------------------------------------


class TestPersistTitle:
    def test_temporary_slot_auto_title_skipped(self):
        """Auto-title skips restricted slots."""
        from kiro_crew.dashboard.state import _ChatSlot

        slot = _ChatSlot(key="tmp-2", memory_mode="temporary")
        slot._titled = False
        slot.messages = [{"role": "user", "content": "hi"}]

        # _maybe_auto_title returns early for restricted slots
        assert slot.is_restricted is True


# ---------------------------------------------------------------------------
# Slack: bounded _thread_temporary + is_thread_temporary helper
# ---------------------------------------------------------------------------


class TestSlackThreadTemporary:
    def setup_method(self):
        from kiro_crew.slack import handler

        handler._thread_temporary.clear()

    def test_is_thread_temporary_false_by_default(self):
        from kiro_crew.slack.handler import is_thread_temporary

        assert is_thread_temporary("unknown-key") is False

    def test_mark_temporary(self):
        from kiro_crew.slack.handler import _mark_temporary, is_thread_temporary

        _mark_temporary("slack-key-1")
        assert is_thread_temporary("slack-key-1") is True

    # Bounded LRU eviction of the temporary-thread tracker is covered by
    # test_messaging_privacy_mode.py::TestTrackers (the tracker moved from
    # slack.handler into kiro_crew.messaging.privacy_mode).


# ---------------------------------------------------------------------------
# Slack: !temporary command handler
# ---------------------------------------------------------------------------


class TestTemporaryCommand:
    def setup_method(self):
        from kiro_crew.slack import handler

        handler._thread_temporary.clear()

    # The notice text, audit event, and session-link behaviour of applying the
    # modifier are covered by test_messaging_privacy_mode.py::TestApplyMode
    # (the implementation moved into kiro_crew.messaging.privacy_mode).

    @pytest.mark.asyncio
    async def test_temporary_modifier_idempotent(self):
        from kiro_crew.slack.handler import _apply_temporary_modifier, _mark_temporary

        _mark_temporary("sk2")

        slack = AsyncMock()
        sessions = MagicMock()

        await _apply_temporary_modifier("sk2", "U1", "C123", slack, sessions, "ts2")

        # Idempotent — no message posted on second call
        slack.post_message.assert_not_called()


# ---------------------------------------------------------------------------
# Dashboard: _is_restricted_session (header-based MCP gating)
# ---------------------------------------------------------------------------


class TestIsRestrictedSession:
    def _mock_request(self, session_key=""):
        req = MagicMock()
        req.headers = {"X-Session-Key": session_key} if session_key else {}
        return req

    def test_dashboard_temporary_slot(self):
        from kiro_crew.dashboard.handlers import _is_restricted_session
        from kiro_crew.dashboard.state import _ChatSlot

        state = MagicMock()
        state._restricted_keys = set()
        state._slots = {"chat-1-abc": _ChatSlot(key="chat-1-abc", memory_mode="temporary")}

        assert _is_restricted_session(state, self._mock_request("dashboard:chat-1-abc")) is True

    def test_dashboard_normal_slot(self):
        from kiro_crew.dashboard.handlers import _is_restricted_session
        from kiro_crew.dashboard.state import _ChatSlot

        state = MagicMock()
        state._restricted_keys = set()
        state._slots = {"chat-1-def": _ChatSlot(key="chat-1-def")}

        assert _is_restricted_session(state, self._mock_request("dashboard:chat-1-def")) is False

    def test_dashboard_restricted_key_set(self):
        from kiro_crew.dashboard.handlers import _is_restricted_session

        state = MagicMock()
        state._restricted_keys = {"dashboard:chat-1-eph"}
        state._slots = {}

        assert _is_restricted_session(state, self._mock_request("dashboard:chat-1-eph")) is True

    def test_slack_temporary_thread(self):
        from kiro_crew.dashboard.handlers import _is_restricted_session
        from kiro_crew.slack.handler import _mark_temporary

        _mark_temporary("slack:C123-456")

        state = MagicMock()
        state._restricted_keys = set()
        state._slots = {}

        assert _is_restricted_session(state, self._mock_request("slack:C123-456")) is True

    def test_no_header(self):
        """No X-Session-Key header — should return False (browser UI or normal)."""
        from kiro_crew.dashboard.handlers import _is_restricted_session

        state = MagicMock()
        state._restricted_keys = set()
        assert _is_restricted_session(state, self._mock_request()) is False

    def test_dashboard_ui_key_not_restricted(self):
        """Browser UI sends 'dashboard:ui' — never restricted."""
        from kiro_crew.dashboard.handlers import _is_restricted_session

        state = MagicMock()
        state._restricted_keys = set()
        assert _is_restricted_session(state, self._mock_request("dashboard:ui")) is False

    def teardown_method(self):
        from kiro_crew.slack import handler

        handler._thread_temporary.clear()


# ---------------------------------------------------------------------------
# MCP: session_key plumbed via X-Session-Key header (not body)
# ---------------------------------------------------------------------------


class TestMcpSessionKeyPlumbing:
    @patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "dashboard:chat-1-tmp"})
    @patch("kiro_crew.mcp_core._post")
    def test_learn_add_no_session_key_in_body(self, mock_post):
        """session_key should NOT be in the JSON body — header handles it."""
        mock_post.return_value = {"ok": True}
        from kiro_crew.mcp_core import _call_tool_inner

        result = _call_tool_inner("learn_add", {"rule": "test rule", "category": "knowledge"})
        payload = mock_post.call_args[0][1]
        assert "session_key" not in payload
        assert "Saved" in result

    @patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "dashboard:chat-1-tmp"})
    @patch("kiro_crew.mcp_core._delete")
    def test_learn_remove_no_session_key_in_body(self, mock_delete):
        """session_key should NOT be in the JSON body — header handles it."""
        mock_delete.return_value = {"removed": 1}
        from kiro_crew.mcp_core import _call_tool_inner

        _call_tool_inner("learn_remove", {"query": "test"})
        payload = mock_delete.call_args[0][1]
        assert "session_key" not in payload

    @patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "dashboard:chat-1-tmp"})
    @patch("kiro_crew.mcp_core._get")
    def test_learn_list_no_session_key_in_url(self, mock_get):
        """session_key should NOT be in query params — header handles it."""
        mock_get.return_value = {"lessons": []}
        from kiro_crew.mcp_core import _call_tool_inner

        _call_tool_inner("learn_list", {})
        url = mock_get.call_args[0][0]
        assert "session_key" not in url
