"""A claimed run that waits out its jitter is reported as waiting, with the time it starts.

A recurring job that is not on a strict schedule is claimed at its scheduled
minute and then sleeps a random jitter before it executes. For that wait
``is_running`` is true, because the claim is what keeps a second run out, and
nothing has run. ``run_waiting_until`` is what a reader tells the two apart by:
the wall-clock time the wait ends, None once the run executes. ``GET /api/crons``
carries it as ``waiting_until`` so the Schedule page can show the job as waiting.

Time is a ``ManualClock`` installed on ``kiro_crew.cron``, and the wait itself is
the ``_sleep_out_jitter`` seam: no test here sleeps.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import cron as cron_module
from kiro_crew.cron import CronJob, CronSchedule, CronService
from kiro_crew.cron_history import CronHistoryStore
from kiro_crew.dashboard.handlers.cron import api_crons
from kiro_crew.testing.clock import ManualClock

_JITTER = 1500.0


def _job(job_id: str = "daily") -> CronJob:
    return CronJob(
        id=job_id,
        name="daily report",
        message="write the report",
        schedule=CronSchedule(kind="cron", cron_expr="39 2 * * *"),
    )


def _service(tmp_path: Any, on_job: AsyncMock, job: CronJob) -> CronService:
    svc = CronService(base_dir=None, on_job=on_job)
    svc._history = CronHistoryStore(base_dir=tmp_path)
    svc._jobs = [job]
    return svc


class TestRunWaitingUntil:
    @pytest.mark.asyncio
    async def test_a_run_in_its_jitter_wait_reports_when_it_starts(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = ManualClock()
        clock.install(monkeypatch, cron_module)
        seen: dict[str, Any] = {}

        async def on_job(_job: CronJob) -> None:
            seen["while_executing"] = svc.run_waiting_until("daily")

        job = _job()
        svc = _service(tmp_path, AsyncMock(side_effect=on_job), job)
        refreshes: list[str] = []
        svc.set_refresh_callback(refreshes.append)
        claim = svc._claim_run("daily", "scheduled")
        wait_starts_at = clock.time()

        async def wait_out(seconds: float) -> None:
            seen["jitter"] = seconds
            seen["while_waiting"] = svc.run_waiting_until("daily")
            seen["running_while_waiting"] = svc.is_running("daily")
            seen["refreshes_at_wait_start"] = list(refreshes)
            clock.advance(seconds)

        with (
            patch.object(svc, "_merge_job_result"),
            patch.object(svc, "_compute_jitter", return_value=_JITTER),
            patch.object(cron_module, "_sleep_out_jitter", wait_out),
        ):
            await svc._run_job_isolated(job, claim)

        assert seen["jitter"] == _JITTER
        assert seen["running_while_waiting"] is True
        assert seen["while_waiting"] == wait_starts_at + _JITTER
        # Pushed when the wait starts, so the page shows it without a reload.
        assert seen["refreshes_at_wait_start"] == ["crons"]
        assert seen["while_executing"] is None
        assert svc.run_waiting_until("daily") is None

    @pytest.mark.asyncio
    async def test_a_run_with_no_jitter_is_never_waiting(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ManualClock().install(monkeypatch, cron_module)
        seen: dict[str, Any] = {}

        async def on_job(_job: CronJob) -> None:
            seen["while_executing"] = svc.run_waiting_until("daily")

        job = _job()
        svc = _service(tmp_path, AsyncMock(side_effect=on_job), job)
        claim = svc._claim_run("daily", "manual")
        seen["claimed"] = svc.run_waiting_until("daily")

        with patch.object(svc, "_merge_job_result"):
            await svc._run_job_isolated(job, claim)

        assert seen == {"claimed": None, "while_executing": None}

    def test_a_job_with_no_run_and_a_taken_run_are_not_waiting(self) -> None:
        svc = CronService(base_dir=None)
        assert svc.run_waiting_until("absent") is None

        claim = svc._claim_run("daily", "scheduled")
        claim.waiting_until = 1_700_000_500.0
        assert svc.run_waiting_until("daily") == 1_700_000_500.0
        # cancel() or the reaper took the run: its terminal row is being written.
        claim.taken = True
        assert svc.run_waiting_until("daily") is None


class TestAJitteredRunStillRuns:
    @pytest.mark.asyncio
    async def test_the_job_runs_once_its_wait_ends_and_files_a_row(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A control for the run itself, not for how it is shown: the run executes
        when the wait ends, releases its claim and leaves one history row."""
        clock = ManualClock()
        clock.install(monkeypatch, cron_module)
        on_job = AsyncMock()
        job = _job()
        svc = _service(tmp_path, on_job, job)
        claim = svc._claim_run("daily", "scheduled")
        claimed_at = claim.claimed_at
        wait_started_at = clock.time()

        async def wait_out(seconds: float) -> None:
            on_job.assert_not_awaited()
            clock.advance(seconds)

        with (
            patch.object(svc, "_merge_job_result") as merge,
            patch.object(svc, "_compute_jitter", return_value=_JITTER),
            patch.object(cron_module, "_sleep_out_jitter", wait_out),
        ):
            await svc._run_job_isolated(job, claim)

        on_job.assert_awaited_once_with(job)
        assert svc.is_running("daily") is False
        assert merge.call_args.args[0].last_status == "ok"
        runs, total = await svc._history.get_job_history("daily")
        assert total == 1
        assert runs[0]["status"] == "success"
        assert runs[0]["started_at"] == claimed_at
        # Stamped after the wait, on the clock the wait moved.
        assert runs[0]["finished_at"] == wait_started_at + _JITTER


def _request(job: CronJob, *, running: bool, waiting_until: float | None) -> MagicMock:
    state = MagicMock()
    state.crons.list_jobs_async = AsyncMock(return_value=[job])
    state.crons.is_running.return_value = running
    state.crons.running_since.return_value = 1_700_000_000.0 if running else None
    state.crons.run_waiting_until.return_value = waiting_until
    state.has_slot.return_value = False
    request = MagicMock()
    request.app = {"state": state}
    return request


class TestTheScheduleListCarriesTheWait:
    @pytest.mark.asyncio
    async def test_a_waiting_run_is_listed_with_the_time_it_starts(self) -> None:
        request = _request(_job(), running=True, waiting_until=1_700_001_500.0)

        row = json.loads((await api_crons(request)).body)["jobs"][0]

        assert row["is_running"] is True
        assert row["waiting_until"] == 1_700_001_500.0

    @pytest.mark.asyncio
    async def test_an_executing_run_and_an_idle_job_carry_no_wait(self) -> None:
        executing = _request(_job(), running=True, waiting_until=None)
        idle = _request(_job(), running=False, waiting_until=None)

        executing_row = json.loads((await api_crons(executing)).body)["jobs"][0]
        idle_row = json.loads((await api_crons(idle)).body)["jobs"][0]

        assert (executing_row["is_running"], executing_row["waiting_until"]) == (True, None)
        assert (idle_row["is_running"], idle_row["waiting_until"]) == (False, None)
        # Only a claimed run can be waiting, so an idle job is not asked.
        idle.app["state"].crons.run_waiting_until.assert_not_called()
