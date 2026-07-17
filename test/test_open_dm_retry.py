"""Tests for _open_dm_with_retry transient-error handling."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_sdk.errors import SlackApiError


def _make_gateway():
    """Build a minimal GatewayOrchestrator with mocked dependencies."""
    from kiro_crew.slack.gateway import GatewayOrchestrator

    gw = GatewayOrchestrator.__new__(GatewayOrchestrator)
    gw.slack = MagicMock()
    gw.slack.open_dm = AsyncMock()
    return gw


def _slack_error(status_code: int, message: str = "error") -> SlackApiError:
    resp = MagicMock()
    resp.status_code = status_code
    return SlackApiError(message=message, response=resp)


@pytest.mark.asyncio
async def test_success_first_attempt():
    gw = _make_gateway()
    gw.slack.open_dm.return_value = "D123"
    result = await gw._open_dm_with_retry("U1", "job1")
    assert result == "D123"
    gw.slack.open_dm.assert_awaited_once_with("U1")


@pytest.mark.asyncio
async def test_transient_failure_then_success():
    gw = _make_gateway()
    gw.slack.open_dm.side_effect = [_slack_error(429), "D123"]
    with patch("kiro_crew.slack.gateway.asyncio.sleep", new_callable=AsyncMock):
        result = await gw._open_dm_with_retry("U1", "job1")
    assert result == "D123"
    assert gw.slack.open_dm.await_count == 2


@pytest.mark.asyncio
async def test_all_attempts_exhausted():
    gw = _make_gateway()
    gw.slack.open_dm.side_effect = [_slack_error(500)] * 3
    with patch("kiro_crew.slack.gateway.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(SlackApiError):
            await gw._open_dm_with_retry("U1", "job1", max_attempts=3)
    assert gw.slack.open_dm.await_count == 3


@pytest.mark.asyncio
async def test_non_retryable_error_raises_immediately():
    gw = _make_gateway()
    gw.slack.open_dm.side_effect = _slack_error(404)
    with pytest.raises(SlackApiError):
        await gw._open_dm_with_retry("U1", "job1")
    gw.slack.open_dm.assert_awaited_once()


@pytest.mark.asyncio
async def test_slack_is_none_returns_none():
    gw = _make_gateway()
    gw.slack = None
    result = await gw._open_dm_with_retry("U1", "job1")
    assert result is None


@pytest.mark.asyncio
async def test_backoff_values():
    gw = _make_gateway()
    gw.slack.open_dm.side_effect = [_slack_error(500)] * 3
    mock_sleep = AsyncMock()
    with patch("kiro_crew.slack.gateway.asyncio.sleep", mock_sleep):
        with pytest.raises(SlackApiError):
            await gw._open_dm_with_retry("U1", "job1", max_attempts=3)
    mock_sleep.assert_any_await(1)
    mock_sleep.assert_any_await(2)
    assert mock_sleep.await_count == 2


@pytest.mark.asyncio
async def test_logging_with_exc_info():
    gw = _make_gateway()
    gw.slack.open_dm.side_effect = [_slack_error(429), "D123"]
    with patch("kiro_crew.slack.gateway.asyncio.sleep", new_callable=AsyncMock), \
         patch("kiro_crew.slack.gateway.logger") as mock_logger:
        await gw._open_dm_with_retry("U1", "job1")
    mock_logger.warning.assert_called_once()
    _, kwargs = mock_logger.warning.call_args
    assert kwargs.get("exc_info") is True


@pytest.mark.asyncio
async def test_connection_error_retried():
    gw = _make_gateway()
    gw.slack.open_dm.side_effect = [ConnectionError("reset"), "D123"]
    with patch("kiro_crew.slack.gateway.asyncio.sleep", new_callable=AsyncMock):
        result = await gw._open_dm_with_retry("U1", "job1")
    assert result == "D123"
    assert gw.slack.open_dm.await_count == 2
