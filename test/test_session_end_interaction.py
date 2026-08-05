"""Tests for _handle_session_end in slack/interactions.py."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.slack.interactions import _handle_session_end


def _make_orch(*, find_key: str | None = None, remove_side_effect=None):
    orch = MagicMock()
    orch.sessions.find_key_by_sid.return_value = find_key
    orch.sessions.remove = AsyncMock(side_effect=remove_side_effect)
    orch.slack = None  # skip Slack post
    return orch


@pytest.mark.asyncio
@patch("kiro_crew.slack.interactions.is_owner", return_value=True)
async def test_session_end_calls_remove(_mock_owner):
    """End Session button calls remove() (soft) not destroy()."""
    orch = _make_orch(find_key="dashboard:chat-1-100")
    with patch("kiro_crew.slack.interactions._orch", orch):
        await _handle_session_end(
            payload={},
            action={"value": "abc-123-sid"},
            channel="C1",
            msg_ts="1234",
            user_id="U_OWNER",
        )
    orch.sessions.remove.assert_awaited_once_with("dashboard:chat-1-100")
    orch.sessions.destroy.assert_not_called()


@pytest.mark.asyncio
@patch("kiro_crew.slack.interactions.is_owner", return_value=True)
async def test_session_end_remove_exception_swallowed(_mock_owner):
    """If remove() raises, the handler doesn't propagate."""
    orch = _make_orch(find_key="dashboard:chat-1-100", remove_side_effect=RuntimeError("gone"))
    with patch("kiro_crew.slack.interactions._orch", orch):
        await _handle_session_end(
            payload={},
            action={"value": "abc-123-sid"},
            channel="C1",
            msg_ts="1234",
            user_id="U_OWNER",
        )
    orch.sessions.remove.assert_awaited_once()
