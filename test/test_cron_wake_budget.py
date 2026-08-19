"""Tests for the per-wake budget knob (timeout_secs) and cron transient retry.

Phase 1 foundations for perpetual agents (RFC rev 3, items 2 and 3):

- ``timeout_secs`` (the asyncio deadline for one run) was read, clamped and
  persisted but absent from every public mutation path — a job could only ever
  carry the creation-time default. These tests pin it reachable through
  ``add_job`` and ``update_job`` with validation.
- ``timeout`` (script/command subprocess bound) was accepted by MCP
  ``cron_update`` but silently dropped by ``_update_job_locked``; now consumed.
- A transient backend error raised OUTSIDE the prompt stream (session acquire /
  client creation) used to go straight to ``record_failure()``, marching a
  healthy job toward auto-pause on infrastructure weather (Phase 0, Finding 1).
  The callback now retries with backoff, mirroring the subagent path.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp.client import AcpError
from kiro_crew.cron import _JOB_TIMEOUT_SECS, CronJob, CronSchedule, CronService


class TestWakeBudgetKnob:
    """timeout_secs must be settable at creation and by update, with bounds."""

    def test_add_job_default(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="t", message="m", every_secs=300)
        assert job.timeout_secs == _JOB_TIMEOUT_SECS

    def test_add_job_explicit(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="t", message="m", every_secs=300, timeout_secs=3000)
        assert job.timeout_secs == 3000
        # Persisted, not just in-memory: reload from disk.
        svc2 = CronService(base_dir=tmp_path)
        svc2._load()
        assert svc2.list_jobs()[0].timeout_secs == 3000

    def test_add_job_rejects_out_of_range(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        with pytest.raises(ValueError, match="timeout_secs"):
            svc.add_job(name="t", message="m", every_secs=300, timeout_secs=86401)

    def test_update_job_sets_and_persists(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="t", message="m", every_secs=300)
        updated = svc.update_job(job.id, timeout_secs=3000)
        assert updated is not None
        assert updated.timeout_secs == 3000
        svc2 = CronService(base_dir=tmp_path)
        svc2._load()
        assert svc2.list_jobs()[0].timeout_secs == 3000

    @pytest.mark.parametrize("bad", [0, -1, 86401])
    def test_update_job_rejects_out_of_range(self, tmp_path: Path, bad: int) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="t", message="m", every_secs=300)
        with pytest.raises(ValueError, match="timeout_secs"):
            svc.update_job(job.id, timeout_secs=bad)
        # Unchanged after the rejected update.
        assert svc.list_jobs()[0].timeout_secs == _JOB_TIMEOUT_SECS

    def test_update_job_rejects_non_int(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="t", message="m", every_secs=300)
        with pytest.raises(ValueError, match="timeout_secs"):
            svc.update_job(job.id, timeout_secs="soon")

    def test_update_job_none_is_noop(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="t", message="m", every_secs=300, timeout_secs=3000)
        updated = svc.update_job(job.id, timeout_secs=None, name="renamed")
        assert updated is not None
        assert updated.timeout_secs == 3000
        assert updated.name == "renamed"

    def test_runtime_deadline_reads_updated_value(self, tmp_path: Path) -> None:
        """_execute_with_timeout derives its deadline from the updated field."""
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="t", message="m", every_secs=300)
        svc.update_job(job.id, timeout_secs=2)
        target = svc.list_jobs()[0]

        async def _slow(_job: CronJob) -> None:
            await asyncio.sleep(30)

        svc._on_job = _slow

        async def _run() -> None:
            await asyncio.wait_for(svc._execute_with_timeout(target), timeout=10)

        asyncio.run(_run())
        assert target.last_status == "error"
        assert "Timed out after 2s" in (target.last_error or "")


class TestSubprocessTimeoutUpdate:
    """The 'timeout' field (script/command bound) is now consumed by update."""

    def test_update_job_sets_timeout(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="t", message="m", every_secs=300, command="true")
        updated = svc.update_job(job.id, timeout=600)
        assert updated is not None
        assert updated.timeout == 600

    def test_update_job_rejects_bad_timeout(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="t", message="m", every_secs=300, command="true")
        with pytest.raises(ValueError, match="timeout"):
            svc.update_job(job.id, timeout=-5)


@pytest.fixture
def gw_and_cb() -> tuple[Any, Callable[[], Any], Callable[..., Any]]:
    """GatewayOrchestrator with mocked sessions; capture the cron callback.

    Mirrors test_cron_acp_retry.py's fixture (uses __new__ to bypass
    __init__). Update both if GatewayOrchestrator gains required attributes.
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


def _job(jid: str = "j1") -> CronJob:
    return CronJob(
        id=jid,
        name="t-" + jid,
        message="msg",
        schedule=CronSchedule(kind="every", every_secs=60),
    )


def _transient_err(msg: str = "Bedrock ThrottlingException: rate exceeded") -> AcpError:
    err = AcpError(msg)
    err.transient = True  # structured verdict, the authoritative path
    return err


class TestCronTransientRetry:
    """Pre-dispatch transient errors retry the callback instead of counting.

    The retry is deliberately restricted to failures raised BEFORE the prompt
    is handed to the provider (session acquire / client creation): after
    dispatch, tools may already have executed, so a whole-callback resubmit
    risks duplicating their side effects. In-stream transient errors are
    stream_and_collect's own retry's job.
    """

    def _run(
        self,
        gw_and_cb: tuple[Any, Any, Any],
        job: CronJob,
        *,
        get_or_create: Any,
        mock_stream: Any,
    ) -> Any:
        gw, get_cb, capture_cron = gw_and_cb
        gw.sessions.get_or_create = get_or_create
        with (
            patch("kiro_crew.slack.gateway.stream_and_collect", side_effect=mock_stream),
            patch("kiro_crew.slack.gateway.redact_exfiltration_urls", return_value=("", False)),
            patch("kiro_crew.slack.gateway.redact_credentials", return_value=("", False)),
            patch(
                "kiro_crew.slack.gateway.CronService.create",
                new=AsyncMock(side_effect=capture_cron),
            ),
            patch("kiro_crew.slack.gateway.transient_retry_delay", return_value=0.0),
        ):

            async def _init_and_run() -> Any:
                await gw._init_cron()
                cb = get_cb()
                assert cb is not None
                return await cb(job)

            return asyncio.run(_init_and_run())

    def test_pre_dispatch_transient_error_retries_and_recovers(
        self, gw_and_cb: tuple[Any, Any, Any]
    ) -> None:
        """A throttle during session acquire recovers on the retry, no count."""
        acquire_calls = 0

        async def flaky_acquire(*a: Any, **kw: Any) -> Any:
            nonlocal acquire_calls
            acquire_calls += 1
            if acquire_calls == 1:
                raise _transient_err()
            return (MagicMock(), True, False)

        async def mock_stream(*a: Any, **kw: Any) -> str:
            return "recovered"

        job = _job("jt1")
        result = self._run(gw_and_cb, job, get_or_create=flaky_acquire, mock_stream=mock_stream)
        assert result == "recovered"
        assert acquire_calls == 2
        assert job.consecutive_failures == 0

    def test_transient_retries_exhausted_still_counts(
        self, gw_and_cb: tuple[Any, Any, Any]
    ) -> None:
        """A persistent pre-dispatch outage exhausts retries and still counts.

        The callback re-raises after alerting (cron's _execute owns
        last_status), so the exception is expected to propagate.
        """
        acquire_calls = 0

        async def dead_acquire(*a: Any, **kw: Any) -> Any:
            nonlocal acquire_calls
            acquire_calls += 1
            raise _transient_err()

        async def mock_stream(*a: Any, **kw: Any) -> str:
            raise AssertionError("must not be reached")

        job = _job("jt2")
        with pytest.raises(AcpError):
            self._run(gw_and_cb, job, get_or_create=dead_acquire, mock_stream=mock_stream)
        # 1 original + _CRON_TRANSIENT_RETRIES recursive attempts.
        from kiro_crew.slack.gateway import _CRON_TRANSIENT_RETRIES

        assert acquire_calls == 1 + _CRON_TRANSIENT_RETRIES
        # Exactly ONE failure for the whole chain, not one per attempt.
        assert job.consecutive_failures == 1
        # Counter cleared for the next scheduled run.
        assert getattr(job, "_transient_attempts", 0) == 0

    def test_post_dispatch_transient_error_does_not_retry(
        self, gw_and_cb: tuple[Any, Any, Any]
    ) -> None:
        """A transient error AFTER prompt dispatch must not resubmit the turn.

        Tools may already have executed; a whole-callback retry would repeat
        their side effects (GPT review finding on the first revision).
        """
        stream_calls = 0

        async def mock_stream(*a: Any, **kw: Any) -> str:
            nonlocal stream_calls
            stream_calls += 1
            raise _transient_err()

        job = _job("jt3")
        with pytest.raises(AcpError):
            self._run(
                gw_and_cb,
                job,
                get_or_create=AsyncMock(return_value=(MagicMock(), True, False)),
                mock_stream=mock_stream,
            )
        assert stream_calls == 1
        assert job.consecutive_failures == 1

    def test_non_transient_error_does_not_retry(self, gw_and_cb: tuple[Any, Any, Any]) -> None:
        """An auth/validation error goes straight to the failure path."""
        acquire_calls = 0

        async def denied_acquire(*a: Any, **kw: Any) -> Any:
            nonlocal acquire_calls
            acquire_calls += 1
            err = AcpError("AccessDeniedException: credential invalid")
            err.transient = False
            raise err

        async def mock_stream(*a: Any, **kw: Any) -> str:
            raise AssertionError("must not be reached")

        job = _job("jt4")
        with pytest.raises(AcpError):
            self._run(gw_and_cb, job, get_or_create=denied_acquire, mock_stream=mock_stream)
        assert acquire_calls == 1
        assert job.consecutive_failures == 1

    def test_transient_counter_resets_between_runs(self, gw_and_cb: tuple[Any, Any, Any]) -> None:
        """Attempts do not accumulate across separate scheduled runs."""
        gw, get_cb, capture_cron = gw_and_cb
        acquire_calls = 0

        async def flaky_acquire(*a: Any, **kw: Any) -> Any:
            nonlocal acquire_calls
            acquire_calls += 1
            # Fail transiently once per RUN, succeed on the run's retry.
            if acquire_calls % 2 == 1:
                raise _transient_err("HTTP 503 from backend")
            return (MagicMock(), True, False)

        async def mock_stream(*a: Any, **kw: Any) -> str:
            return "ok"

        gw.sessions.get_or_create = flaky_acquire
        job = _job("jt5")
        with (
            patch("kiro_crew.slack.gateway.stream_and_collect", side_effect=mock_stream),
            patch("kiro_crew.slack.gateway.redact_exfiltration_urls", return_value=("", False)),
            patch("kiro_crew.slack.gateway.redact_credentials", return_value=("", False)),
            patch(
                "kiro_crew.slack.gateway.CronService.create",
                new=AsyncMock(side_effect=capture_cron),
            ),
            patch("kiro_crew.slack.gateway.transient_retry_delay", return_value=0.0),
        ):

            async def _two_runs() -> tuple[Any, Any]:
                await gw._init_cron()
                cb = get_cb()
                r1 = await cb(job)
                r2 = await cb(job)
                return r1, r2

            r1, r2 = asyncio.run(_two_runs())

        assert (r1, r2) == ("ok", "ok")
        assert acquire_calls == 4  # two runs x (1 transient failure + 1 retry)
        assert job.consecutive_failures == 0


class TestCronPostTokenResume:
    """One-shot post-token resume for transient errors inside the prompt stream.

    Closes the seam between the two existing retry layers: a transient error
    raised AFTER the prompt was dispatched AND after at least one token had
    streamed used to fail the whole cycle (stream_and_collect stops retrying
    once tokens streamed; the whole-callback retry stops once the prompt is
    dispatched). The callback now re-prompts the SAME live session ONCE with a
    continuation instruction, preserving the partial instead of re-running the
    turn — mirroring chat_runner's ``_posttoken_retry_used`` branch and the
    subagent's post-activity arm.
    """

    def _run(
        self,
        gw_and_cb: tuple[Any, Any, Any],
        job: CronJob,
        *,
        mock_stream: Any,
    ) -> Any:
        gw, get_cb, capture_cron = gw_and_cb
        with (
            patch("kiro_crew.slack.gateway.stream_and_collect", side_effect=mock_stream),
            patch("kiro_crew.slack.gateway.redact_exfiltration_urls", return_value=("", False)),
            patch("kiro_crew.slack.gateway.redact_credentials", return_value=("", False)),
            patch(
                "kiro_crew.slack.gateway.CronService.create",
                new=AsyncMock(side_effect=capture_cron),
            ),
            patch("kiro_crew.slack.gateway.transient_retry_delay", return_value=0.0),
        ):

            async def _init_and_run() -> Any:
                await gw._init_cron()
                cb = get_cb()
                assert cb is not None
                return await cb(job)

            return asyncio.run(_init_and_run())

    def test_posttoken_transient_recovers_and_records_success(
        self, gw_and_cb: tuple[Any, Any, Any]
    ) -> None:
        """Transient after tokens: continuation resumes, partial preserved,
        record_success() runs and the failure counter is untouched."""
        from kiro_crew.slack.gateway import _CRON_POSTTOKEN_CONTINUE_MSG

        calls: list[str] = []

        async def mock_stream(client: Any, message: str, **kw: Any) -> str:
            calls.append(message)
            if len(calls) == 1:
                kw["on_chunk"]("partial ")
                raise _transient_err()
            return "continued"

        job = _job("pt1")
        job.consecutive_failures = 3  # record_success() must reset this
        result = self._run(gw_and_cb, job, mock_stream=mock_stream)
        # Partial is preserved and the continuation appended — never re-run.
        assert result == "partial continued"
        assert len(calls) == 2
        # The resume sends the CONTINUE instruction, not the original message.
        assert calls[1] == _CRON_POSTTOKEN_CONTINUE_MSG
        # Recovered cycle counts as a success: counter reset, no failure spent.
        assert job.consecutive_failures == 0
        assert not job.auto_paused

    def test_pretoken_transient_still_takes_existing_path(
        self, gw_and_cb: tuple[Any, Any, Any]
    ) -> None:
        """Transient with NO tokens streamed: existing path, no resume.

        stream_and_collect owns that window (its budget is spent by the time
        the error propagates here), so the callback must not add a resume.
        """
        stream_calls = 0

        async def mock_stream(client: Any, message: str, **kw: Any) -> str:
            nonlocal stream_calls
            stream_calls += 1
            raise _transient_err()

        job = _job("pt2")
        with pytest.raises(AcpError):
            self._run(gw_and_cb, job, mock_stream=mock_stream)
        assert stream_calls == 1
        assert job.consecutive_failures == 1

    def test_non_transient_error_after_tokens_does_not_resume(
        self, gw_and_cb: tuple[Any, Any, Any]
    ) -> None:
        """A non-transient error after tokens fails fast — no continuation."""
        stream_calls = 0

        async def mock_stream(client: Any, message: str, **kw: Any) -> str:
            nonlocal stream_calls
            stream_calls += 1
            kw["on_chunk"]("partial ")
            err = AcpError("ValidationException: prompt rejected")
            err.transient = False  # type: ignore[attr-defined]
            raise err

        job = _job("pt3")
        with pytest.raises(AcpError):
            self._run(gw_and_cb, job, mock_stream=mock_stream)
        assert stream_calls == 1
        assert job.consecutive_failures == 1

    def test_posttoken_resume_is_one_shot_per_turn(
        self, gw_and_cb: tuple[Any, Any, Any]
    ) -> None:
        """A transient error during the continuation propagates: the one shot
        is spent, and the unrecovered cycle records the error exactly as
        before (one failure, no third prompt)."""
        stream_calls = 0

        async def mock_stream(client: Any, message: str, **kw: Any) -> str:
            nonlocal stream_calls
            stream_calls += 1
            kw["on_chunk"](f"chunk{stream_calls} ")
            raise _transient_err()

        job = _job("pt4")
        with pytest.raises(AcpError):
            self._run(gw_and_cb, job, mock_stream=mock_stream)
        # Original + exactly ONE continuation — never a loop.
        assert stream_calls == 2
        assert job.consecutive_failures == 1

    def test_continuation_owns_no_inner_transient_budget(
        self, gw_and_cb: tuple[Any, Any, Any]
    ) -> None:
        """The continuation call passes retry_transient=False: its in-stream
        retry re-sends the prompt whenever no text has streamed, so a mutating
        tool completed by the continuation followed by a pre-text transient
        would otherwise be re-run — amplifying the one-shot."""
        flags: list[Any] = []

        async def mock_stream(client: Any, message: str, **kw: Any) -> str:
            flags.append(kw.get("retry_transient", "default"))
            if len(flags) == 1:
                kw["on_chunk"]("partial ")
                raise _transient_err()
            return "continued"

        job = _job("pt6")
        result = self._run(gw_and_cb, job, mock_stream=mock_stream)
        assert result == "partial continued"
        # First call keeps the default budget; the continuation gets none.
        assert flags == [True, False]

    def test_resumed_turn_bills_both_prompts(self, gw_and_cb: tuple[Any, Any, Any]) -> None:
        """The usage row for a resumed cycle carries the interrupted prompt's
        credits plus the continuation's — not only the continuation's, which a
        single post-turn read of the (carried-over) per-turn stats would see.
        """
        from kiro_crew.acp.types import TurnUsage

        async def mock_stream(client: Any, message: str, **kw: Any) -> str:
            if not getattr(mock_stream, "_failed", False):
                mock_stream._failed = True  # type: ignore[attr-defined]
                kw["on_chunk"]("partial ")
                raise _transient_err()
            return "continued"

        persisted: list[TurnUsage] = []

        async def capture_persist(session_key: Any, model: Any, usage: Any, **kw: Any) -> None:
            persisted.append(usage)

        job = _job("pt5")
        gw, get_cb, capture_cron = gw_and_cb
        with (
            patch("kiro_crew.slack.gateway.stream_and_collect", side_effect=mock_stream),
            patch("kiro_crew.slack.gateway.redact_exfiltration_urls", return_value=("", False)),
            patch("kiro_crew.slack.gateway.redact_credentials", return_value=("", False)),
            patch(
                "kiro_crew.slack.gateway.CronService.create",
                new=AsyncMock(side_effect=capture_cron),
            ),
            patch("kiro_crew.slack.gateway.transient_retry_delay", return_value=0.0),
            patch(
                "kiro_crew.slack.gateway.provider_last_turn_usage",
                side_effect=[TurnUsage(credits=3.0), TurnUsage(credits=2.0)],
            ),
            patch(
                "kiro_crew.slack.gateway.persist_token_record_async",
                side_effect=capture_persist,
            ),
        ):

            async def _init_and_run() -> Any:
                await gw._init_cron()
                cb = get_cb()
                assert cb is not None
                return await cb(job)

            result = asyncio.run(_init_and_run())
        assert result == "partial continued"
        assert len(persisted) == 1
        assert persisted[0].credits == pytest.approx(5.0)


class TestWakeBudgetSubprocessGuard:
    """The wake budget must cover a command/script subprocess timeout."""

    def test_add_rejects_budget_below_command_timeout(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        with pytest.raises(ValueError, match="wake budget"):
            svc.add_job(
                name="t", message="m", every_secs=300, command="true",
                timeout=600, timeout_secs=60,
            )

    def test_update_rejects_budget_below_default_command_timeout(self, tmp_path: Path) -> None:
        """timeout_secs=1 on a command job: default subprocess bound is 300s."""
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="t", message="m", every_secs=300, command="true")
        with pytest.raises(ValueError, match="wake budget"):
            svc.update_job(job.id, timeout_secs=1)
        # Rejected update leaves the job untouched.
        assert svc.list_jobs()[0].timeout_secs == _JOB_TIMEOUT_SECS

    def test_update_rejects_raising_subprocess_timeout_above_budget(
        self, tmp_path: Path
    ) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(
            name="t", message="m", every_secs=300, command="true", timeout_secs=400,
        )
        with pytest.raises(ValueError, match="wake budget"):
            svc.update_job(job.id, timeout=500)

    def test_llm_jobs_are_not_constrained(self, tmp_path: Path) -> None:
        """The guard only applies to command/script jobs."""
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="t", message="m", every_secs=300, timeout_secs=10)
        assert job.timeout_secs == 10
        updated = svc.update_job(job.id, timeout_secs=5)
        assert updated is not None and updated.timeout_secs == 5

    def test_rejected_update_leaves_other_fields_untouched(self, tmp_path: Path) -> None:
        """A rejected timeout_secs must not strand earlier mutations (name).

        GPT round-2 finding: validation ran after name/message mutated, so a
        later save would persist the rejected partial update.
        """
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="orig", message="m", every_secs=300, command="true")
        with pytest.raises(ValueError, match="wake budget"):
            svc.update_job(job.id, name="renamed", timeout_secs=1)
        got = svc.list_jobs()[0]
        assert got.name == "orig"
        assert got.timeout_secs == _JOB_TIMEOUT_SECS

    def test_add_allows_budget_covering_script_default(self, tmp_path: Path) -> None:
        """Script default is 30s; a 60s budget clears it plus allowance."""
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(
            name="t", message="m", every_secs=300,
            script="~/.kiro/crew/crons/x.py:f", timeout_secs=60,
        )
        assert job.timeout_secs == 60
