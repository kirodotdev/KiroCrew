"""Tests for ACP process death recovery in cron callback."""

from __future__ import annotations

import asyncio
from typing import Any, Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp.client import AcpError
from kiro_crew.cron import CronJob, CronSchedule


@pytest.fixture
def gw_and_cb() -> tuple[Any, Callable[[], Any], Callable[..., Any]]:
    """Create a GatewayOrchestrator with mocked sessions and capture cron callback.

    Uses __new__ to bypass __init__ and avoid Slack/dashboard dependencies.
    Update this fixture if GatewayOrchestrator gains new required attributes.
    """
    from kiro_crew.slack.gateway import GatewayOrchestrator

    gw = GatewayOrchestrator.__new__(GatewayOrchestrator)
    gw.sessions = MagicMock()
    gw.sessions.get_pid = MagicMock(return_value=None)
    gw.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))
    gw.sessions.release = MagicMock()
    gw.sessions.reset = AsyncMock()
    gw.ctx_builder = MagicMock()
    gw.ctx_builder.build_message = MagicMock(return_value=("msg", None))
    gw.ctx_builder.hooks = MagicMock()
    gw.slack = None
    gw.conv_log = None
    gw.dashboard_state = None
    gw._owner_id = "U000"
    gw.subagent_mgr = None
    gw._cron_injecting = {}
    gw._no_crons = False
    gw._interactive_approval = MagicMock(return_value="interactive_cb")

    captured_cb: list[Any] = [None]

    def capture_cron(on_job: Any = None, **kw: Any) -> MagicMock:
        captured_cb[0] = on_job
        svc = MagicMock()
        svc.start = AsyncMock()
        return svc

    return gw, lambda: captured_cb[0], capture_cron


class TestCronAcpRetry:
    """Test ACP error retry logic in _cron_callback."""

    def test_acp_not_running_triggers_retry(self, gw_and_cb: tuple[Any, Any, Any]) -> None:
        """AcpError with 'not running' resets session and retries once."""
        gw, get_cb, capture_cron = gw_and_cb
        call_count = 0

        async def mock_stream(*args: Any, **kwargs: Any) -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise AcpError("ACP process not running")
            return "success after retry"

        job = CronJob(
            id="j1",
            name="test",
            message="msg",
            schedule=CronSchedule(kind="every", every_secs=60),
        )

        with (
            patch("kiro_crew.slack.gateway.stream_and_collect", side_effect=mock_stream),
            patch("kiro_crew.slack.gateway.redact_exfiltration_urls", return_value=("", False)),
            patch("kiro_crew.slack.gateway.redact_credentials", return_value=("", False)),
            patch("kiro_crew.slack.gateway.CronService.create", new=AsyncMock(side_effect=capture_cron)),
        ):

            async def _init_and_run() -> str:
                await gw._init_cron()
                cb = get_cb()
                assert cb is not None
                return await cb(job)

            result = asyncio.run(_init_and_run())

        assert call_count == 2  # First call fails, retry succeeds
        assert result == "success after retry"
        gw.sessions.reset.assert_awaited()

    def test_acp_process_exited_triggers_retry(self, gw_and_cb: tuple[Any, Any, Any]) -> None:
        """AcpError with 'process exited' resets session and retries once."""
        gw, get_cb, capture_cron = gw_and_cb
        call_count = 0

        async def mock_stream(*args: Any, **kwargs: Any) -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise AcpError("ACP process exited unexpectedly")
            return "recovered"

        job = CronJob(
            id="j2",
            name="test2",
            message="msg",
            schedule=CronSchedule(kind="every", every_secs=60),
        )

        with (
            patch("kiro_crew.slack.gateway.stream_and_collect", side_effect=mock_stream),
            patch("kiro_crew.slack.gateway.redact_exfiltration_urls", return_value=("", False)),
            patch("kiro_crew.slack.gateway.redact_credentials", return_value=("", False)),
            patch("kiro_crew.slack.gateway.CronService.create", new=AsyncMock(side_effect=capture_cron)),
        ):

            async def _init_and_run() -> str:
                await gw._init_cron()
                cb = get_cb()
                assert cb is not None
                return await cb(job)

            result = asyncio.run(_init_and_run())

        assert call_count == 2  # First call fails, retry succeeds
        assert result == "recovered"
        gw.sessions.reset.assert_awaited()

    def test_acp_retry_only_once(self, gw_and_cb: tuple[Any, Any, Any]) -> None:
        """Second AcpError after retry raises instead of infinite loop."""
        gw, get_cb, capture_cron = gw_and_cb
        gw.dashboard_state = MagicMock()
        call_count = 0

        async def mock_stream(*args: Any, **kwargs: Any) -> str:
            nonlocal call_count
            call_count += 1
            raise AcpError("ACP process not running")

        job = CronJob(
            id="j3",
            name="test3",
            message="msg",
            schedule=CronSchedule(kind="every", every_secs=60),
        )

        with (
            patch("kiro_crew.slack.gateway.stream_and_collect", side_effect=mock_stream),
            patch("kiro_crew.slack.gateway.redact_exfiltration_urls", return_value=("", False)),
            patch("kiro_crew.slack.gateway.redact_credentials", return_value=("", False)),
            patch("kiro_crew.slack.gateway.CronService.create", new=AsyncMock(side_effect=capture_cron)),
        ):

            async def _init_and_run() -> str:
                await gw._init_cron()
                cb = get_cb()
                assert cb is not None
                return await cb(job)

            with pytest.raises(AcpError):
                asyncio.run(_init_and_run())

        # First call + one retry = 2 calls max
        assert call_count == 2
        # notify only in outer handler (suppressed during retry via _acp_retried guard)
        gw.dashboard_state.notify.assert_called_once()

    def test_non_retryable_acp_error_raises_immediately(
        self, gw_and_cb: tuple[Any, Any, Any]
    ) -> None:
        """AcpError without 'not running'/'process exited' raises without retry."""
        gw, get_cb, capture_cron = gw_and_cb
        gw.dashboard_state = MagicMock()
        call_count = 0

        async def mock_stream(*args: Any, **kwargs: Any) -> str:
            nonlocal call_count
            call_count += 1
            raise AcpError("some other ACP error")

        job = CronJob(
            id="j4",
            name="test4",
            message="msg",
            schedule=CronSchedule(kind="every", every_secs=60),
        )

        with (
            patch("kiro_crew.slack.gateway.stream_and_collect", side_effect=mock_stream),
            patch("kiro_crew.slack.gateway.redact_exfiltration_urls", return_value=("", False)),
            patch("kiro_crew.slack.gateway.redact_credentials", return_value=("", False)),
            patch("kiro_crew.slack.gateway.CronService.create", new=AsyncMock(side_effect=capture_cron)),
        ):

            async def _init_and_run() -> str:
                await gw._init_cron()
                cb = get_cb()
                assert cb is not None
                return await cb(job)

            with pytest.raises(AcpError):
                asyncio.run(_init_and_run())

        # No retry for non-retryable errors
        assert call_count == 1
