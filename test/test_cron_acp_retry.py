"""Tests for ACP process death recovery in cron callback."""

from __future__ import annotations

import asyncio
from typing import Any, Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp.client import AcpError, AcpProcessDied
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
    gw._cron_runs_holding = set()
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
        held_at_reset: list[set[Any]] = []
        run_tasks: list[Any] = []
        gw.sessions.reset = AsyncMock(
            side_effect=lambda *_a, **_k: held_at_reset.append(set(gw._cron_runs_holding))
        )

        async def mock_stream(*args: Any, **kwargs: Any) -> str:
            nonlocal call_count
            call_count += 1
            run_tasks.append(asyncio.current_task())
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
        # The retry re-enters the callback in the run's own task, so the run
        # still holds its entry across the reset and the retry, and the
        # outermost frame drops it when the run ends.
        assert run_tasks[0] is run_tasks[1]
        assert held_at_reset[0] == {run_tasks[0]}
        assert gw._cron_runs_holding == set()

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

    def test_acp_pipe_broken_before_dispatch_triggers_retry(
        self, gw_and_cb: tuple[Any, Any, Any]
    ) -> None:
        """AcpProcessDied worded 'pipe broken' before dispatch is retried by type."""
        gw, get_cb, capture_cron = gw_and_cb
        live = gw.sessions.get_or_create.return_value
        gw.sessions.get_or_create = AsyncMock(
            side_effect=[AcpProcessDied("ACP process pipe broken: Connection lost"), live]
        )

        async def mock_stream(*args: Any, **kwargs: Any) -> str:
            return "recovered from pipe"

        job = CronJob(
            id="j-pipe",
            name="test-pipe",
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

        assert gw.sessions.get_or_create.await_count == 2
        assert result == "recovered from pipe"
        gw.sessions.reset.assert_awaited()

    def test_acp_pipe_broken_after_dispatch_is_not_replayed(
        self, gw_and_cb: tuple[Any, Any, Any]
    ) -> None:
        """A pipe-broken death mid-turn is not replayed: tools may already have run."""
        gw, get_cb, capture_cron = gw_and_cb
        gw.dashboard_state = MagicMock()
        call_count = 0

        async def mock_stream(*args: Any, **kwargs: Any) -> str:
            nonlocal call_count
            call_count += 1
            raise AcpProcessDied("ACP process pipe broken: Connection lost")

        job = CronJob(
            id="j-pipe-mid",
            name="test-pipe-mid",
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

            with pytest.raises(AcpProcessDied):
                asyncio.run(_init_and_run())

        assert call_count == 1  # no replay of the turn

    def test_failed_acp_retry_does_not_reach_the_transient_ladder(
        self, gw_and_cb: tuple[Any, Any, Any]
    ) -> None:
        """A transient pre-dispatch death whose retry dispatched and died is sent once."""
        gw, get_cb, capture_cron = gw_and_cb
        gw.dashboard_state = MagicMock()
        live = gw.sessions.get_or_create.return_value
        gw.sessions.get_or_create = AsyncMock(
            side_effect=[
                AcpProcessDied("ACP process pipe broken: [Errno 104] Connection reset by peer"),
                live,
                live,
            ]
        )
        dispatches = 0

        async def mock_stream(*args: Any, **kwargs: Any) -> str:
            nonlocal dispatches
            dispatches += 1
            raise AcpProcessDied("ACP process pipe broken: Connection lost")

        job = CronJob(
            id="j-once",
            name="test-once",
            message="msg",
            schedule=CronSchedule(kind="every", every_secs=60),
        )

        with (
            patch("kiro_crew.slack.gateway.stream_and_collect", side_effect=mock_stream),
            patch("kiro_crew.slack.gateway.redact_exfiltration_urls", return_value=("", False)),
            patch("kiro_crew.slack.gateway.redact_credentials", return_value=("", False)),
            patch("kiro_crew.slack.gateway.asyncio.sleep", new=AsyncMock()),
            patch("kiro_crew.slack.gateway.CronService.create", new=AsyncMock(side_effect=capture_cron)),
        ):

            async def _init_and_run() -> str:
                await gw._init_cron()
                cb = get_cb()
                assert cb is not None
                return await cb(job)

            with pytest.raises(AcpProcessDied):
                asyncio.run(_init_and_run())

        assert dispatches == 1

    def test_failed_reset_still_gets_the_transient_retry(
        self, gw_and_cb: tuple[Any, Any, Any]
    ) -> None:
        """A reset that raises runs no attempt, so the transient ladder still runs one."""
        gw, get_cb, capture_cron = gw_and_cb
        live = gw.sessions.get_or_create.return_value
        gw.sessions.get_or_create = AsyncMock(
            side_effect=[
                AcpProcessDied("ACP process pipe broken: [Errno 104] Connection reset by peer"),
                live,
            ]
        )
        # Only the ACP arm's reset fails; later cleanup resets succeed.
        gw.sessions.reset = AsyncMock(
            side_effect=[RuntimeError("reset failed"), None, None, None]
        )
        dispatches = 0

        async def mock_stream(*args: Any, **kwargs: Any) -> str:
            nonlocal dispatches
            dispatches += 1
            return "recovered by the ladder"

        job = CronJob(
            id="j-reset",
            name="test-reset",
            message="msg",
            schedule=CronSchedule(kind="every", every_secs=60),
        )

        with (
            patch("kiro_crew.slack.gateway.stream_and_collect", side_effect=mock_stream),
            patch("kiro_crew.slack.gateway.redact_exfiltration_urls", return_value=("", False)),
            patch("kiro_crew.slack.gateway.redact_credentials", return_value=("", False)),
            patch("kiro_crew.slack.gateway.asyncio.sleep", new=AsyncMock()),
            patch("kiro_crew.slack.gateway.CronService.create", new=AsyncMock(side_effect=capture_cron)),
        ):

            async def _init_and_run() -> str:
                await gw._init_cron()
                cb = get_cb()
                assert cb is not None
                return await cb(job)

            result = asyncio.run(_init_and_run())

        assert dispatches == 1
        assert result == "recovered by the ladder"

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

    def test_the_run_holds_its_task_through_a_transient_backoff_and_drops_it_on_exit(
        self, gw_and_cb: tuple[Any, Any, Any]
    ) -> None:
        """A transient error after the acquire and before dispatch takes the transient
        arm: it releases the session, sleeps, and re-enters the callback in the same
        task. The run is still the run through the backoff, so its task stays in
        ``_cron_runs_holding``; the retry's claim then fails before it acquires, and
        the run's task leaves the set on that exit too."""
        gw, get_cb, capture_cron = gw_and_cb
        gw.sessions.get_or_create = AsyncMock(
            side_effect=[(MagicMock(), True, False), RuntimeError("the retry cannot claim")]
        )
        err = AcpError("backend throttled the request")
        err.transient = True
        gw.ctx_builder.build_message = MagicMock(side_effect=err)  # before dispatch
        in_backoff: list[tuple[Any, set[Any]]] = []

        def backoff_delay(_attempt: int) -> float:
            # The arm computes its backoff here, then releases and sleeps: the run's
            # task must be held across that whole stretch, so this is where it is read.
            in_backoff.append((asyncio.current_task(), set(gw._cron_runs_holding)))
            return 0.0

        job = CronJob(
            id="j-transient",
            name="test-transient",
            message="msg",
            schedule=CronSchedule(kind="every", every_secs=60),
        )

        with (
            patch("kiro_crew.slack.gateway.stream_and_collect", new=AsyncMock(return_value="x")),
            patch("kiro_crew.slack.gateway.redact_exfiltration_urls", return_value=("", False)),
            patch("kiro_crew.slack.gateway.redact_credentials", return_value=("", False)),
            patch("kiro_crew.slack.gateway.transient_retry_delay", side_effect=backoff_delay),
            patch(
                "kiro_crew.slack.gateway.CronService.create",
                new=AsyncMock(side_effect=capture_cron),
            ),
        ):

            async def _init_and_run() -> None:
                await gw._init_cron()
                cb = get_cb()
                assert cb is not None
                with pytest.raises(RuntimeError):
                    await cb(job)

            asyncio.run(_init_and_run())

        assert in_backoff, "the transient arm did not run"
        run_task, held = in_backoff[0]
        assert held == {run_task}, f"the run's task was not held through the backoff: {held!r}"
        assert gw._cron_runs_holding == set(), "a run that ended is still held"

    def test_a_run_whose_first_claim_fails_leaves_no_task_held(
        self, gw_and_cb: tuple[Any, Any, Any]
    ) -> None:
        """A run that raises before its first acquire still drops its task."""
        gw, get_cb, capture_cron = gw_and_cb
        gw.sessions.get_or_create = AsyncMock(side_effect=RuntimeError("no claim"))
        job = CronJob(
            id="j-noclaim",
            name="test-noclaim",
            message="msg",
            schedule=CronSchedule(kind="every", every_secs=60),
        )

        with (
            patch("kiro_crew.slack.gateway.redact_exfiltration_urls", return_value=("", False)),
            patch("kiro_crew.slack.gateway.redact_credentials", return_value=("", False)),
            patch(
                "kiro_crew.slack.gateway.CronService.create",
                new=AsyncMock(side_effect=capture_cron),
            ),
        ):

            async def _init_and_run() -> None:
                await gw._init_cron()
                cb = get_cb()
                assert cb is not None
                with pytest.raises(RuntimeError):
                    await cb(job)

            asyncio.run(_init_and_run())

        assert gw._cron_runs_holding == set(), "a run that never acquired is still held"
