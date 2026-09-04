"""Tests for unrecognized bang command catch-all in handler.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.slack import handler


def _make_slack():
    slack = MagicMock()
    slack.post_message = AsyncMock()
    slack.post_blocks = AsyncMock()
    return slack


@pytest.mark.asyncio
async def test_unrecognized_bang_returns_error():
    """Given message text '!foo', when _handle_slash_command is called,
    then it posts error message and returns '' (not None)."""
    slack = _make_slack()
    with patch.object(handler, "is_allowed_user", return_value=True):
        result = await handler._handle_slash_command(
            "!foo", slack, MagicMock(), "C1", "t1", "msg1", "t1", "U1",
        )
    assert result == ""
    slack.post_message.assert_called_once_with(
        "C1",
        "❌ Unknown command `!foo`. Type `/kirocrew help` for available commands.",
        "t1",
    )


@pytest.mark.asyncio
async def test_recognized_bang_still_works():
    """Given message text '!yolo on', when _handle_slash_command is called,
    then it processes normally (does not hit the catch-all)."""
    slack = _make_slack()
    with (
        patch.object(handler, "is_allowed_user", return_value=True),
        patch.object(handler, "is_yolo_mode", return_value=False),
        patch.object(handler, "enable_yolo_with_ttl"),
        patch.object(handler, "sel") as mock_sel,
        patch.object(handler, "deprecation_warning_block", return_value={}),
    ):
        mock_sel.return_value.log_api_access = MagicMock()
        result = await handler._handle_slash_command(
            "!yolo on", slack, MagicMock(), "C1", "t1", "msg1", "t1", "U1",
        )
    assert result == ""
    # The error message should NOT have been posted
    for call in slack.post_message.call_args_list:
        assert "Unknown command" not in str(call)


@pytest.mark.asyncio
async def test_bare_exclamation_returns_error():
    """Given message text '!', when _handle_slash_command is called,
    then it posts an error message and returns '' (not None)."""
    slack = _make_slack()
    with patch.object(handler, "is_allowed_user", return_value=True):
        result = await handler._handle_slash_command(
            "!", slack, MagicMock(), "C1", "t1", "msg1", "t1", "U1",
        )
    assert result == ""
    slack.post_message.assert_called_once_with(
        "C1",
        "❌ Unknown command `!`. Type `/kirocrew help` for available commands.",
        "t1",
    )
