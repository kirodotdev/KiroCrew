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
            patch(
                "kiro_crew.slack.gateway.CronService.create",
                new=AsyncMock(side_effect=capture_cron),
            ),
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
            patch(
                "kiro_crew.slack.gateway.CronService.create",
                new=AsyncMock(side_effect=capture_cron),
            ),
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
            patch(
                "kiro_crew.slack.gateway.CronService.create",
                new=AsyncMock(side_effect=capture_cron),
            ),
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
            patch(
                "kiro_crew.slack.gateway.CronService.create",
                new=AsyncMock(side_effect=capture_cron),
            ),
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


class TestCronAcpDeathIsTyped:
    """The retry decision reads the classification, not the wording of the message.

    ``AcpProcessDied`` is raised at every site that discovers a dead child and states
    whether resubmitting the turn is safe. Wording cannot carry that: the most common
    death is discovered as a broken pipe on the next write and reads ``ACP process
    pipe broken: <cause>``, matching neither legacy substring, so a wording test
    skips the branch built for process death on the signature that produces it most
    often.
    """

    def test_acp_pipe_broken_triggers_retry(self, gw_and_cb: tuple[Any, Any, Any]) -> None:
        """The real ``pipe broken`` wording resets the session and retries once.

        Returning a result rather than raising is also what keeps a recovered child
        death off the auto-pause ladder: ``CronService._execute`` calls
        ``record_failure()`` only in its ``except`` arm and ``record_success()`` --
        which zeroes ``consecutive_failures`` -- when the callback returns. So the
        assertion that this callback returns is the assertion that one transient
        death does not march the job toward ``_AUTO_PAUSE_THRESHOLD``.
        """
        gw, get_cb, capture_cron = gw_and_cb
        call_count = 0

        async def mock_stream(*args: Any, **kwargs: Any) -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Verbatim shape of the message raised by _send_request /
                # _send_response / _send_error when the child is already gone.
                raise AcpProcessDied("ACP process pipe broken: Connection lost", resubmit_safe=True)
            return "recovered"

        job = CronJob(
            id="j5",
            name="test5",
            message="msg",
            schedule=CronSchedule(kind="every", every_secs=60),
        )

        with (
            patch("kiro_crew.slack.gateway.stream_and_collect", side_effect=mock_stream),
            patch("kiro_crew.slack.gateway.redact_exfiltration_urls", return_value=("", False)),
            patch("kiro_crew.slack.gateway.redact_credentials", return_value=("", False)),
            patch(
                "kiro_crew.slack.gateway.CronService.create",
                new=AsyncMock(side_effect=capture_cron),
            ),
        ):

            async def _init_and_run() -> str:
                await gw._init_cron()
                cb = get_cb()
                assert cb is not None
                return await cb(job)

            result = asyncio.run(_init_and_run())

        assert call_count == 2
        assert result == "recovered"
        gw.sessions.reset.assert_awaited()

    @pytest.mark.parametrize(
        "message",
        [
            "ACP process pipe broken: Broken pipe",
            "something nobody predicted",
            "a wording that happens to say process exited",
        ],
    )
    def test_resubmit_safe_death_retries_regardless_of_wording(
        self, gw_and_cb: tuple[Any, Any, Any], message: str
    ) -> None:
        """A resubmit-safe ``AcpProcessDied`` retries whatever it says.

        The second case shares no substring with the legacy guard, pinning the
        decision to the classification rather than to the current vocabulary. The
        third is its mirror: wording that DOES match a legacy substring must not be
        what earns the retry either, or the guard is still a wording test wearing a
        type test's clothes.

        Every case states ``resubmit_safe=True`` because the default refuses.
        Wording independence is a claim about deaths already classified safe, not a
        licence to retry an unclassified one.
        """
        gw, get_cb, capture_cron = gw_and_cb
        call_count = 0

        async def mock_stream(*args: Any, **kwargs: Any) -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise AcpProcessDied(message, resubmit_safe=True)
            return "recovered"

        job = CronJob(
            id="j6",
            name="test6",
            message="msg",
            schedule=CronSchedule(kind="every", every_secs=60),
        )

        with (
            patch("kiro_crew.slack.gateway.stream_and_collect", side_effect=mock_stream),
            patch("kiro_crew.slack.gateway.redact_exfiltration_urls", return_value=("", False)),
            patch("kiro_crew.slack.gateway.redact_credentials", return_value=("", False)),
            patch(
                "kiro_crew.slack.gateway.CronService.create",
                new=AsyncMock(side_effect=capture_cron),
            ),
        ):

            async def _init_and_run() -> str:
                await gw._init_cron()
                cb = get_cb()
                assert cb is not None
                return await cb(job)

            result = asyncio.run(_init_and_run())

        assert call_count == 2
        assert result == "recovered"

    def test_pipe_broken_retries_only_once(self, gw_and_cb: tuple[Any, Any, Any]) -> None:
        """A child that dies twice is not retried forever -- the ``_acp_retried``
        marker bounds the new type arm exactly as it bounds the substring arm."""
        gw, get_cb, capture_cron = gw_and_cb
        gw.dashboard_state = MagicMock()
        call_count = 0

        async def mock_stream(*args: Any, **kwargs: Any) -> str:
            nonlocal call_count
            call_count += 1
            raise AcpProcessDied("ACP process pipe broken: Connection lost", resubmit_safe=True)

        job = CronJob(
            id="j8",
            name="test8",
            message="msg",
            schedule=CronSchedule(kind="every", every_secs=60),
        )

        with (
            patch("kiro_crew.slack.gateway.stream_and_collect", side_effect=mock_stream),
            patch("kiro_crew.slack.gateway.redact_exfiltration_urls", return_value=("", False)),
            patch("kiro_crew.slack.gateway.redact_credentials", return_value=("", False)),
            patch(
                "kiro_crew.slack.gateway.CronService.create",
                new=AsyncMock(side_effect=capture_cron),
            ),
        ):

            async def _init_and_run() -> str:
                await gw._init_cron()
                cb = get_cb()
                assert cb is not None
                return await cb(job)

            with pytest.raises(AcpProcessDied):
                asyncio.run(_init_and_run())

        assert call_count == 2
        gw.dashboard_state.notify.assert_called_once()

    def test_non_process_death_acp_error_still_not_retried(
        self, gw_and_cb: tuple[Any, Any, Any]
    ) -> None:
        """Control: widening to the type must not widen to every AcpError. A plain
        AcpError with no death wording is still a single-shot failure.

        ``call_count`` is the only sound discriminator here. ``sessions.reset`` is
        NOT: the callback's own cleanup awaits it on every failure, retry or no
        retry, so asserting it was never awaited would fail on fixed and unfixed
        source alike.
        """
        gw, get_cb, capture_cron = gw_and_cb
        gw.dashboard_state = MagicMock()
        call_count = 0

        async def mock_stream(*args: Any, **kwargs: Any) -> str:
            nonlocal call_count
            call_count += 1
            raise AcpError("model refused the request")

        job = CronJob(
            id="j9",
            name="test9",
            message="msg",
            schedule=CronSchedule(kind="every", every_secs=60),
        )

        with (
            patch("kiro_crew.slack.gateway.stream_and_collect", side_effect=mock_stream),
            patch("kiro_crew.slack.gateway.redact_exfiltration_urls", return_value=("", False)),
            patch("kiro_crew.slack.gateway.redact_credentials", return_value=("", False)),
            patch(
                "kiro_crew.slack.gateway.CronService.create",
                new=AsyncMock(side_effect=capture_cron),
            ),
        ):

            async def _init_and_run() -> str:
                await gw._init_cron()
                cb = get_cb()
                assert cb is not None
                return await cb(job)

            with pytest.raises(AcpError):
                asyncio.run(_init_and_run())

        assert call_count == 1


class TestAcpProcessDiedRaiseSites:
    """The wording the guard has to survive is not a paraphrase -- it comes out of
    the real transport methods, so pin it there rather than only in the mock."""

    def test_send_request_broken_pipe_message_matches_no_legacy_substring(self) -> None:
        from kiro_crew.acp.client import AcpClient

        client = AcpClient.__new__(AcpClient)
        client._process = MagicMock()
        client._process.stdin = MagicMock()
        client._process.stdin.write = MagicMock(side_effect=BrokenPipeError("Connection lost"))
        client._next_req_id = MagicMock(return_value=1)

        with pytest.raises(AcpProcessDied) as excinfo:
            asyncio.run(client._send_request("session/prompt", {}))

        message = str(excinfo.value).lower()
        assert "pipe broken" in message
        # The exact reason the substring guard could not see this death.
        assert "not running" not in message
        assert "process exited" not in message

    def test_broken_pipe_death_is_unclassified_for_the_transient_ladder(self) -> None:
        """The generic transient ladder cannot cover the gap either: the raise site
        passes no ``transient=`` and the message carries no throttle/5xx marker, so
        the fallback classifier says not-transient."""
        from kiro_crew.llm_helpers import acp_error_is_transient

        exc = AcpProcessDied("ACP process pipe broken: Connection lost")
        assert exc.transient is None
        assert acp_error_is_transient(exc) is False


class TestResubmitSafetyContract:
    """A death discovered with a turn in flight must NOT be retried.

    Such a death can follow a tool that already dispatched and landed its side
    effects -- the stall detector fires on "tool dispatched but no data for Ns" and
    kills the agent, and a mid-prompt exit can arrive after a completed tool call.
    Resubmitting the prompt would run those effects a second time, so the invariant
    is about WHEN the death was discovered, not which subsystem noticed.

    Two independent properties are pinned here, because either alone permits the
    replay: the raise sites must not over-claim safety, and the consumer must not
    reach the retry by a route that ignores what they claimed.
    """

    def test_tool_stall_is_not_retried(self, gw_and_cb: tuple[Any, Any, Any]) -> None:
        gw, get_cb, capture_cron = gw_and_cb
        gw.dashboard_state = MagicMock()
        call_count = 0

        async def mock_stream(*args: Any, **kwargs: Any) -> str:
            nonlocal call_count
            call_count += 1
            raise AcpProcessDied(
                "tool stalled -- no data for 300s; agent killed to recover",
                resubmit_safe=False,
            )

        job = CronJob(
            id="j10",
            name="test10",
            message="msg",
            schedule=CronSchedule(kind="every", every_secs=60),
        )

        with (
            patch("kiro_crew.slack.gateway.stream_and_collect", side_effect=mock_stream),
            patch("kiro_crew.slack.gateway.redact_exfiltration_urls", return_value=("", False)),
            patch("kiro_crew.slack.gateway.redact_credentials", return_value=("", False)),
            patch(
                "kiro_crew.slack.gateway.CronService.create",
                new=AsyncMock(side_effect=capture_cron),
            ),
        ):

            async def _init_and_run() -> str:
                await gw._init_cron()
                cb = get_cb()
                assert cb is not None
                return await cb(job)

            with pytest.raises(AcpProcessDied):
                asyncio.run(_init_and_run())

        assert call_count == 1, (
            "a dispatched tool may have completed its side effects; resubmitting "
            "the prompt can repeat them"
        )

    def test_resubmit_safe_states_the_verdict_without_exporting_the_hierarchy(self) -> None:
        """The ACP layer answers "may I re-run this turn?" on the exception itself.

        Application code outside the agent-SDK boundary must not grow its ACP import
        surface (``scripts/check_agent_sdk_boundary.py`` refuses a new symbol on an
        added line), so the cron retry reads this attribute instead of importing
        a second ACP symbol. Mirrors the existing ``AcpError.transient`` precedent.
        """
        assert (
            AcpProcessDied("ACP process pipe broken: x", resubmit_safe=True).resubmit_safe is True
        )
        assert AcpProcessDied("tool stalled -- no data for 300s").resubmit_safe is False
        # Fail closed: a death raised without an opinion REFUSES resubmission, so a
        # site added later is safe by construction rather than by an audit of it.
        assert AcpProcessDied("a death nobody classified").resubmit_safe is False

    def test_tool_stall_is_a_process_death_for_every_other_handler(self) -> None:
        """Subclass, so `except AcpProcessDied` elsewhere keeps catching it. Only
        callers that RESUBMIT work single it out."""
        exc = AcpProcessDied("tool stalled -- no data for 300s", resubmit_safe=False)
        assert isinstance(exc, AcpProcessDied)
        assert isinstance(exc, AcpError)

    def test_only_the_audited_sites_claim_resubmit_safety(self) -> None:
        """Enumerate the sites that OPT IN, which is the enumerable direction.

        The in-flight sites cannot be listed reliably: they outnumber the safe ones,
        several are reachable only through indirection, and two independent audits of
        this class each missed some. So nothing here asserts that list is complete --
        the class default refuses, making an unlisted site safe by construction.

        What this pins is the small, checkable converse: exactly four sites claim
        ``resubmit_safe=True``, and each is one where the turn provably never
        started. A fifth appearing without a matching case here is the regression to
        catch, because opting in is the only way to reach the resubmit path.
        """
        import inspect

        from kiro_crew.acp import client as _client
        from kiro_crew.acp import session_handle as _sh
        from kiro_crew.acp import session_provider as _sp

        # module -> (how many sites may claim the turn never started, why)
        AUDITED_SAFE = {
            "client": (_client, 3, "three transport writes that never reached the child"),
            "session_provider": (_sp, 1, "the pre-conversation liveness check"),
            "session_handle": (_sh, 0, "every death here is reachable with a turn live"),
        }
        for name, (mod, expected, why) in AUDITED_SAFE.items():
            found = inspect.getsource(mod).count("resubmit_safe=True")
            assert found == expected, (
                f"{name}: {found} site(s) claim resubmit_safe=True, {expected} audited "
                f"({why}). Opting in is the only route to the resubmit path, so a new "
                "one must prove the turn never started -- otherwise a tool that "
                "already completed gets its side effects replayed."
            )

    def test_mid_prompt_death_is_not_retried_despite_legacy_wording(
        self, gw_and_cb: tuple[Any, Any, Any]
    ) -> None:
        """The retained substring arm must not out-vote the type's own verdict.

        ``client.py`` raises ``AcpProcessDied("Process exited during prompt ...")``
        with a turn in flight, so it takes the refusing default -- but that wording
        CONTAINS ``process exited``, which the legacy arm matches. Scoping that arm
        to plain ``AcpError`` is what stops the wording from resurrecting a death
        the classification already refused, and this test is the difference: without
        the scoping it retries and a completed tool mutation is replayed.
        """
        gw, get_cb, capture_cron = gw_and_cb
        gw.dashboard_state = MagicMock()
        call_count = 0

        async def mock_stream(*args: Any, **kwargs: Any) -> str:
            nonlocal call_count
            call_count += 1
            raise AcpProcessDied("Process exited during prompt (exit code 1)")

        job = CronJob(
            id="j-inflight",
            name="inflight",
            message="msg",
            schedule=CronSchedule(kind="every", every_secs=60),
        )

        with (
            patch("kiro_crew.slack.gateway.stream_and_collect", side_effect=mock_stream),
            patch("kiro_crew.slack.gateway.redact_exfiltration_urls", return_value=("", False)),
            patch("kiro_crew.slack.gateway.redact_credentials", return_value=("", False)),
            patch(
                "kiro_crew.slack.gateway.CronService.create",
                new=AsyncMock(side_effect=capture_cron),
            ),
        ):

            async def _init_and_run() -> str:
                await gw._init_cron()
                cb = get_cb()
                assert cb is not None
                return await cb(job)

            with pytest.raises(AcpProcessDied):
                asyncio.run(_init_and_run())

        assert call_count == 1, (
            "the wording matched the legacy substring arm and out-voted this "
            "death's own resubmit_safe=False -- a tool that already completed "
            "mid-prompt gets its side effects replayed"
        )

    def test_plain_acp_error_still_matches_the_legacy_wording_arm(self) -> None:
        """Control for the scoping above: the five plain-``AcpError`` death sites
        keep their retry. They are NOT ``AcpProcessDied``, so restricting the
        substring arm to plain errors leaves them matched -- if this stopped holding,
        the scoping fix would have narrowed the guard past the defect it repairs.
        """
        assert not isinstance(AcpError("ACP process not running"), AcpProcessDied)
        assert not isinstance(AcpError("ACP process exited (code=1)"), AcpProcessDied)
