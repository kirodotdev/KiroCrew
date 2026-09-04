"""Regression tests for the 'Run now' cron handler (api_cron_run).

Starting an immediate run must not overwrite the reference to an
already-running task: doing so orphans the prior task (it can no longer be
tracked/cancelled/joined) and allows overlapping duplicate runs. The handler
must reject with 409 when a run is already in flight.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.cron import CronJob, CronSchedule
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
        # The previously-running task reference must be preserved untouched.
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

        Resolving the job is now asynchronous, so two concurrent requests can
        both get past the lookup — where previously the handler ran straight
        through to the guard without suspending. Only one may still start a run,
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
