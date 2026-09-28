"""Tests for _handle_session_end in slack/interactions.py."""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.slack.interactions import _handle_session_end, _handle_session_resume


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


# ---------------------------------------------------------------------------
# The dismissal record: the half that makes End take the row off the list
# ---------------------------------------------------------------------------


def _dismissal(orch) -> dict:
    """The metadata fields the handler stamped, from the recorded call."""
    orch.conv_log.update_metadata_if.assert_called_once()
    return orch.conv_log.update_metadata_if.call_args[0][1]


def _dismissed_key(orch) -> str:
    return orch.conv_log.update_metadata_if.call_args[0][0]


def _dismissal_guard(orch):
    return orch.conv_log.update_metadata_if.call_args[0][2]


@pytest.mark.asyncio
@patch("kiro_crew.slack.interactions.is_owner", return_value=True)
async def test_end_records_dismissal_for_an_idle_row(_mock_owner):
    """The case the reporter hits: End on a row with no live session.

    Nothing is running for the key, so there is nothing to remove — and before
    this record existed the handler did nothing whatsoever, which is why the row
    came straight back on the next ``sessions`` call.
    """
    orch = _make_orch(find_key=None)
    orch.sessions.has_session.return_value = False
    with patch("kiro_crew.slack.interactions._orch", orch):
        await _handle_session_end(
            payload={},
            action={"value": "slack:C1.170000"},
            channel="C1",
            msg_ts="1234",
            user_id="U_OWNER",
        )
    orch.sessions.remove.assert_not_awaited()
    assert _dismissed_key(orch) == "slack:C1.170000"
    assert _dismissal(orch)["closed"] is True


@pytest.mark.asyncio
@patch("kiro_crew.slack.interactions.is_owner", return_value=True)
async def test_end_records_dismissal_for_a_live_row_too(_mock_owner):
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
    # The resolved session key, never the sid the button carried.
    assert _dismissed_key(orch) == "dashboard:chat-1-100"
    assert _dismissal(orch)["closed"] is True


@pytest.mark.asyncio
@patch("kiro_crew.slack.interactions.is_owner", return_value=True)
async def test_closed_at_is_stamped_after_the_teardown(_mock_owner):
    """Consolidation and skill extraction write the file on the way out.

    Stamped before the teardown, those writes would land after ``closed_at``
    and any reader comparing the two would read them as the user coming back.
    """
    torn_down_at: list[float] = []

    async def _slow_remove(_key):
        await asyncio.sleep(0.05)
        torn_down_at.append(time.time())

    orch = _make_orch(find_key="slack:C1.170000", remove_side_effect=_slow_remove)
    with patch("kiro_crew.slack.interactions._orch", orch):
        await _handle_session_end(
            payload={},
            action={"value": "sid"},
            channel="C1",
            msg_ts="1234",
            user_id="U_OWNER",
        )
    assert torn_down_at, "remove did not run"
    assert _dismissal(orch)["closed_at"] >= torn_down_at[0]


@pytest.mark.asyncio
@patch("kiro_crew.slack.interactions.is_owner", return_value=True)
async def test_dismissal_is_guarded_on_the_transcript_existing(_mock_owner):
    """A click must never invent a session.

    The metadata writer creates the file when it is missing, so an unresolvable
    or stale button value would leave a brand-new empty transcript behind — and
    that transcript would then show up in the very list this is meant to trim.
    """
    orch = _make_orch(find_key=None)
    orch.sessions.has_session.return_value = False
    with patch("kiro_crew.slack.interactions._orch", orch):
        await _handle_session_end(
            payload={},
            action={"value": "slack:C1.170000"},
            channel="C1",
            msg_ts="1234",
            user_id="U_OWNER",
        )
    guard = _dismissal_guard(orch)
    assert guard({}) is False  # no readable metadata line -> no write
    assert guard({"_type": "metadata", "title": "real"}) is True


@pytest.mark.asyncio
@patch("kiro_crew.slack.interactions.is_owner", return_value=True)
async def test_end_survives_a_failed_dismissal_write(_mock_owner):
    orch = _make_orch(find_key="slack:C1.170000")
    orch.conv_log.update_metadata_if.side_effect = OSError("disk gone")
    with patch("kiro_crew.slack.interactions._orch", orch):
        await _handle_session_end(
            payload={},
            action={"value": "sid"},
            channel="C1",
            msg_ts="1234",
            user_id="U_OWNER",
        )
    orch.sessions.remove.assert_awaited_once()


@pytest.mark.asyncio
@patch("kiro_crew.slack.interactions.is_owner", return_value=True)
async def test_end_without_a_conversation_log_is_a_no_op(_mock_owner):
    orch = _make_orch(find_key="slack:C1.170000")
    orch.conv_log = None
    with patch("kiro_crew.slack.interactions._orch", orch):
        await _handle_session_end(
            payload={},
            action={"value": "sid"},
            channel="C1",
            msg_ts="1234",
            user_id="U_OWNER",
        )
    orch.sessions.remove.assert_awaited_once()


@pytest.mark.asyncio
@patch("kiro_crew.slack.interactions.is_owner", return_value=True)
async def test_resume_retracts_the_dismissal(_mock_owner):
    """Otherwise the row would drop out again as soon as the process exits."""
    orch = _make_orch()
    orch.sessions.get_slack_link.return_value = ("", "")
    orch.slack = MagicMock()
    orch.slack.post_blocks = AsyncMock()
    orch.slack.post_message = AsyncMock()
    orch.slack.open_dm = AsyncMock(return_value="D1")
    with patch("kiro_crew.slack.interactions._orch", orch):
        await _handle_session_resume(
            payload={},
            action={"value": '{"key": "slack:C1.170000", "title": "back"}'},
            channel="C1",
            msg_ts="1234",
            user_id="U_OWNER",
        )
    orch.conv_log.clear_closed.assert_called_once_with("slack:C1.170000")
