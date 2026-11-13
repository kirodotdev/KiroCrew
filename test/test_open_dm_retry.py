"""Tests for the shared open_dm retry (slack/retry.py) via the gateway method."""

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
    with patch("kiro_crew.slack.retry.asyncio.sleep", new_callable=AsyncMock):
        result = await gw._open_dm_with_retry("U1", "job1")
    assert result == "D123"
    assert gw.slack.open_dm.await_count == 2


@pytest.mark.asyncio
async def test_all_attempts_exhausted():
    gw = _make_gateway()
    gw.slack.open_dm.side_effect = [_slack_error(500)] * 3
    with patch("kiro_crew.slack.retry.asyncio.sleep", new_callable=AsyncMock):
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
    with patch("kiro_crew.slack.retry.asyncio.sleep", mock_sleep):
        with pytest.raises(SlackApiError):
            await gw._open_dm_with_retry("U1", "job1", max_attempts=3)
    mock_sleep.assert_any_await(1)
    mock_sleep.assert_any_await(2)
    assert mock_sleep.await_count == 2


@pytest.mark.asyncio
async def test_logging_with_exc_info():
    gw = _make_gateway()
    gw.slack.open_dm.side_effect = [_slack_error(429), "D123"]
    with patch("kiro_crew.slack.retry.asyncio.sleep", new_callable=AsyncMock), \
         patch("kiro_crew.slack.retry.logger") as mock_logger:
        await gw._open_dm_with_retry("U1", "job1")
    mock_logger.warning.assert_called_once()
    _, kwargs = mock_logger.warning.call_args
    assert kwargs.get("exc_info") is True


@pytest.mark.asyncio
async def test_connection_error_retried():
    gw = _make_gateway()
    gw.slack.open_dm.side_effect = [ConnectionError("reset"), "D123"]
    with patch("kiro_crew.slack.retry.asyncio.sleep", new_callable=AsyncMock):
        result = await gw._open_dm_with_retry("U1", "job1")
    assert result == "D123"
    assert gw.slack.open_dm.await_count == 2


@pytest.mark.asyncio
async def test_aiohttp_client_error_retried():
    # AsyncWebClient rides on aiohttp, whose
    # connector/DNS failures are aiohttp.ClientError subclasses, NOT
    # ConnectionError -- they must be retryable or a transient blip
    # abandons the DM after one attempt.
    import aiohttp

    gw = _make_gateway()
    gw.slack.open_dm.side_effect = [aiohttp.ClientConnectionError("dns blip"), "D123"]
    with patch("kiro_crew.slack.retry.asyncio.sleep", new_callable=AsyncMock):
        result = await gw._open_dm_with_retry("U1", "job1")
    assert result == "D123"
    assert gw.slack.open_dm.await_count == 2


def _recorded_sleep():
    """Patch the retry's sleep to record the requested delay without waiting."""
    mock_sleep = AsyncMock()
    sleeping = patch(
        "kiro_crew.slack.retry.asyncio.sleep", mock_sleep
    )  # flake-ok: records the requested delay, reads no clock
    return mock_sleep, sleeping


def _rate_limited(headers: object) -> SlackApiError:
    resp = MagicMock()
    resp.status_code = 429
    resp.headers = headers
    return SlackApiError(message="ratelimited", response=resp)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"Retry-After": "12"}, 12.0),
        ({"retry-after": ["7"]}, 7.0),
        ({"Retry-After": "600"}, 30.0),
    ],
    ids=["string", "list-lowercase", "capped"],
)
async def test_a_429_waits_out_its_retry_after(headers, expected):
    # Slack names the seconds left in the rate-limit window. A retry after the
    # linear 1s would be another 429 and spend the attempt for nothing.
    gw = _make_gateway()
    gw.slack.open_dm.side_effect = [_rate_limited(headers), "D123"]
    mock_sleep, sleeping = _recorded_sleep()
    with sleeping:
        result = await gw._open_dm_with_retry("U1", "job1")
    assert result == "D123"
    mock_sleep.assert_awaited_once_with(expected)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [{}, {"Retry-After": "soon"}, {"Retry-After": "-5"}, {"Retry-After": "nan"}, None],
    ids=["absent", "not-a-number", "negative", "nan", "no-headers"],
)
async def test_a_429_without_a_usable_retry_after_keeps_the_linear_backoff(headers):
    gw = _make_gateway()
    gw.slack.open_dm.side_effect = [_rate_limited(headers), "D123"]
    mock_sleep, sleeping = _recorded_sleep()
    with sleeping:
        await gw._open_dm_with_retry("U1", "job1")
    mock_sleep.assert_awaited_once_with(1.0)


@pytest.mark.asyncio
async def test_a_short_retry_after_never_shortens_the_linear_backoff():
    gw = _make_gateway()
    gw.slack.open_dm.side_effect = [_slack_error(500), _rate_limited({"Retry-After": "0"}), "D1"]
    mock_sleep, sleeping = _recorded_sleep()
    with sleeping:
        await gw._open_dm_with_retry("U1", "job1", max_attempts=3)
    assert [c.args[0] for c in mock_sleep.await_args_list] == [1.0, 2.0]


@pytest.mark.asyncio
async def test_a_5xx_ignores_a_retry_after_header():
    resp = MagicMock()
    resp.status_code = 503
    resp.headers = {"Retry-After": "20"}
    gw = _make_gateway()
    gw.slack.open_dm.side_effect = [SlackApiError(message="down", response=resp), "D1"]
    mock_sleep, sleeping = _recorded_sleep()
    with sleeping:
        await gw._open_dm_with_retry("U1", "job1")
    mock_sleep.assert_awaited_once_with(1.0)
