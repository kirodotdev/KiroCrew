"""Regression tests for the 'Run now' cron handler (api_cron_run).

Starting an immediate run must not overwrite the reference to an
already-running task: doing so orphans the prior task (it cannot be
tracked/cancelled/joined) and allows overlapping duplicate runs. The handler
must reject with 409 when a run is already in flight.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.cron import CronJob, CronSchedule, CronService
from kiro_crew.dashboard.handlers.cron import api_cron_run


def _make_job(job_id: str = "j1", name: str = "etl job") -> CronJob:
    return CronJob(
        id=job_id,
        name=name,
        message="do something",
        schedule=CronSchedule(kind="every", every_secs=300),
        created_ts=time.time(),
    )


def _make_app(state) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/crons/{job_id}/run", api_cron_run)
    return app


def _make_state(job: CronJob | None, *, is_running: bool = False, running_tasks=None):
    state = MagicMock()
    state.crons = MagicMock()
    # The handler resolves the job through the freshness-guaranteed async form so
    # a job written by another process (CLI / MCP `cron add`) is visible
    # immediately. `list_jobs` is left mocked as an EMPTY cache on purpose: if the
    # handler ever regresses to the cache-only lookup, every test here goes red
    # rather than silently passing against a stale snapshot.
    state.crons.list_jobs.return_value = []
    state.crons.get_job_async = AsyncMock(return_value=job)
    state.crons.is_running.return_value = is_running
    state.crons._running_tasks = running_tasks if running_tasks is not None else {}
    state.crons.run_job = AsyncMock(return_value=True)
    state.push_refresh = MagicMock()
    return state


class TestApiCronRun:
    @pytest.mark.asyncio
    async def test_run_idle_job_starts(self) -> None:
        state = _make_state(_make_job("j1"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/crons/j1/run")
            assert resp.status == 200
            data = await resp.json()
        assert data["ok"] is True
        # A run was started (the task may already have finished and been popped
        # by the done-callback, so assert the invocation rather than the dict).
        state.crons.run_job.assert_called_once_with("j1")

    @pytest.mark.asyncio
    async def test_run_unknown_job_404(self) -> None:
        state = _make_state(None)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/crons/ghost/run")
            assert resp.status == 404
        state.crons.run_job.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_run_while_executing_409(self) -> None:
        state = _make_state(_make_job("j1"), is_running=True)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/crons/j1/run")
            assert resp.status == 409
            data = await resp.json()
        assert "already running" in data["error"]
        state.crons.run_job.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_run_does_not_clobber_existing_task(self) -> None:
        prior = MagicMock(name="prior_task")
        state = _make_state(_make_job("j1"), running_tasks={"j1": prior})
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/crons/j1/run")
            assert resp.status == 409
        # The already-running task reference must be preserved untouched.
        assert state.crons._running_tasks["j1"] is prior
        state.crons.run_job.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_run_finds_job_absent_from_the_cache_only_snapshot(self) -> None:
        """A job created by another process must be runnable immediately.

        `kirocrew cron add` and the MCP cron_add tool write crons.json from a
        separate process. The gateway's in-memory snapshot only picks that up on
        its timer tick, so resolving the id through the cache-only `list_jobs()`
        made this endpoint 404 for up to _TIMER_POLL_SECS after creation. The
        handler must use the freshness-guaranteed lookup instead.
        """
        state = _make_state(_make_job("just-created"))
        # Explicitly model the stale gateway: the cache does not contain it yet.
        state.crons.list_jobs.return_value = []
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/crons/just-created/run")
            assert resp.status == 200
        state.crons.get_job_async.assert_awaited_once_with("just-created")
        state.crons.run_job.assert_called_once_with("just-created")

    @pytest.mark.asyncio
    async def test_concurrent_runs_still_yield_one_200_and_one_409(self) -> None:
        """The added await must not weaken the check-and-set guard.

        Resolving the job is asynchronous, so two concurrent requests can
        both suspend at the lookup and reach the guard. Only one may still start a run,
        because the guard and the `_running_tasks` assignment are not separated
        by an await.
        """
        gate = asyncio.Event()

        async def _blocked_run(_job_id: str) -> bool:
            await gate.wait()
            return True

        state = _make_state(_make_job("j1"))
        state.crons.run_job = AsyncMock(side_effect=_blocked_run)
        try:
            async with TestClient(TestServer(_make_app(state))) as client:
                first, second = await asyncio.gather(
                    client.post("/api/crons/j1/run"),
                    client.post("/api/crons/j1/run"),
                )
                assert sorted([first.status, second.status]) == [200, 409]
        finally:
            gate.set()
        # Exactly one run was started despite both requests passing the lookup.
        state.crons.run_job.assert_called_once_with("j1")


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 5.0) -> None:
    """Poll the loop until *predicate* holds; the handler starts its run as a
    background task, so the test has to let that task get through its
    off-loop store read before inspecting the service."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met before the poll deadline")
        await asyncio.sleep(0.01)


class TestApiCronRunStaleFinishedTask:
    """A tracked task that has already finished is not a run in flight.

    ``_executing`` and ``_running_tasks`` are released by ``_run_job_isolated``'s
    ``finally``. A run whose task ends without reaching it leaves both populated
    with nothing on that path to clear them, and a guard that trusts the markers
    alone answers ``POST /api/crons/{id}/run`` with 409 "job is already running"
    for that job until the reaper sweep meets the finished task, while
    ``crons.json`` and the in-flight directory show it idle. The route must not
    wait for that sweep. These tests run the handler against a REAL
    ``CronService`` so the guard's self-heal is exercised, not mocked away.
    """

    @staticmethod
    def _service(tmp_path: Path) -> tuple[CronService, CronJob]:
        svc = CronService(base_dir=tmp_path)
        job = _make_job("j1")
        svc._jobs = [job]
        svc._save()
        return svc, job

    @staticmethod
    def _state(svc: CronService) -> MagicMock:
        state = MagicMock()
        state.crons = svc
        state.push_refresh = MagicMock()
        return state

    @pytest.mark.asyncio
    async def test_finished_task_left_in_the_maps_does_not_refuse_a_manual_run(
        self, tmp_path: Path
    ) -> None:
        svc, job = self._service(tmp_path)

        async def _died_before_cleanup() -> None:
            raise RuntimeError("run ended without reaching its finally")

        # The state a run leaves behind when its task ends ahead of the
        # try/finally: a finished task still stored, the job still "executing",
        # its start stamps still claimed.
        stale = asyncio.get_running_loop().create_task(_died_before_cleanup())
        await asyncio.gather(stale, return_exceptions=True)
        assert stale.done()
        svc._running_tasks[job.id] = stale
        svc._executing.add(job.id)
        svc._job_start_times[job.id] = time.time() - 60
        svc._job_start_monotonic[job.id] = time.monotonic() - 60
        assert svc.is_running(job.id)

        state = self._state(svc)
        with patch.object(svc, "_run_job_isolated", new=AsyncMock(return_value=None)) as run:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/crons/j1/run")
                body = await resp.json()
                assert resp.status == 200, (
                    "a finished task left in _running_tasks must not refuse a manual run: "
                    f"got {resp.status} {body}"
                )
                # The handler returns before run_job has read the store off-loop;
                # let the real run_job get through its own _executing claim.
                await _wait_until(lambda: run.await_count == 1)
                await _wait_until(lambda: job.id not in svc._executing)

        assert run.await_args is not None
        assert run.await_args.args[0].id == job.id
        # Every trace of the dead run is gone, and the new run released its own.
        assert job.id not in svc._running_tasks
        assert job.id not in svc._executing
        assert job.id not in svc._job_start_times
        assert job.id not in svc._job_start_monotonic

    @pytest.mark.asyncio
    async def test_live_task_still_refuses_a_manual_run(self, tmp_path: Path) -> None:
        """The self-heal must be keyed on ``done()``: a run that is genuinely
        in flight keeps the 409 and keeps its task handle untouched."""
        svc, job = self._service(tmp_path)
        gate = asyncio.Event()

        async def _still_running() -> None:
            await gate.wait()

        live = asyncio.get_running_loop().create_task(_still_running())
        svc._running_tasks[job.id] = live
        svc._executing.add(job.id)

        state = self._state(svc)
        try:
            with patch.object(svc, "_run_job_isolated", new=AsyncMock(return_value=None)) as run:
                async with TestClient(TestServer(_make_app(state))) as client:
                    resp = await client.post("/api/crons/j1/run")
                    assert resp.status == 409
                    body = await resp.json()
            assert body["error"] == "job is already running"
            assert svc._running_tasks[job.id] is live
            assert job.id in svc._executing
            assert not live.done()
            run.assert_not_awaited()
        finally:
            gate.set()
            await live
