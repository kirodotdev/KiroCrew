"""``timeout_secs`` over the dashboard's cron REST surface.

``timeout_secs`` is a job's whole-run budget: a run still going after it is
stopped and recorded as timed out. The store, the CLI and the cron tools carry
it, but the dashboard's three cron routes did not. Create dropped it, update
would not accept it, and the list payload omitted it, so the Schedule page could
neither show a job's limit nor change it.

The list carries the EFFECTIVE limit (``effective_wake_budget``), not the raw
field, because a stored value outside ``1..86400`` runs under the default and the
form must show the limit a run actually gets.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew.cron import _JOB_TIMEOUT_SECS, CronJob
from kiro_crew.dashboard.handlers.cron import api_cron_update, api_crons, api_crons_create

pytestmark = pytest.mark.asyncio

_CREATE = {"name": "poller", "message": "Check the timestamp.", "every": 3600}


def _app(handler, route: str, **store) -> web.Application:
    app = web.Application()
    app["state"] = SimpleNamespace(
        crons=SimpleNamespace(**store),
        push_refresh=MagicMock(),
        ack_notification=AsyncMock(),
        has_slot=MagicMock(return_value=False),
    )
    app.router.add_route("*", route, handler)
    return as_owner(app)


def _job(**over) -> CronJob:
    fields = {"id": "j1", "name": "poller", "message": "Check the timestamp."}
    fields.update(over)
    return CronJob(**fields)


_INVALID = [0, -5, 86401, 1.5, "3600", None, True]


class TestCreate:
    async def test_the_limit_reaches_the_store(self) -> None:
        add = AsyncMock(return_value=_job(timeout_secs=7200))
        app = _app(api_crons_create, "/api/crons", add_job_async=add)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/crons", json={**_CREATE, "timeout_secs": 7200})
            assert resp.status == 200
        assert add.await_args.kwargs["timeout_secs"] == 7200

    async def test_an_absent_field_leaves_the_store_default(self) -> None:
        add = AsyncMock(return_value=_job())
        app = _app(api_crons_create, "/api/crons", add_job_async=add)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/crons", json=_CREATE)
            assert resp.status == 200
        assert "timeout_secs" not in add.await_args.kwargs

    @pytest.mark.parametrize("value", _INVALID)
    async def test_an_invalid_limit_is_refused_before_anything_is_created(self, value) -> None:
        add = AsyncMock(return_value=_job())
        app = _app(api_crons_create, "/api/crons", add_job_async=add)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/crons", json={**_CREATE, "timeout_secs": value})
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_timeout_secs"
        add.assert_not_awaited()


class TestUpdate:
    async def test_the_limit_is_forwarded(self) -> None:
        update = AsyncMock(return_value=_job(timeout_secs=600))
        app = _app(api_cron_update, "/api/crons/{job_id}", update_job_async=update)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/crons/j1", json={"timeout_secs": 600})
            assert resp.status == 200
        assert update.await_args.kwargs["timeout_secs"] == 600

    async def test_an_unrelated_patch_leaves_the_limit_alone(self) -> None:
        update = AsyncMock(return_value=_job(timeout_secs=600))
        app = _app(api_cron_update, "/api/crons/{job_id}", update_job_async=update)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/crons/j1", json={"name": "renamed"})
            assert resp.status == 200
        assert "timeout_secs" not in update.await_args.kwargs

    @pytest.mark.parametrize("value", _INVALID)
    async def test_an_invalid_limit_is_refused_without_writing(self, value) -> None:
        update = AsyncMock(return_value=_job())
        app = _app(api_cron_update, "/api/crons/{job_id}", update_job_async=update)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/crons/j1", json={"timeout_secs": value})
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_timeout_secs"
        update.assert_not_awaited()

    async def test_the_stores_own_refusal_reaches_the_client(self) -> None:
        """A command job's budget must cover its subprocess timeout; the store
        refuses a smaller one, and that message is what the form shows."""
        update = AsyncMock(side_effect=ValueError("timeout_secs (wake budget) must cover"))
        app = _app(api_cron_update, "/api/crons/{job_id}", update_job_async=update)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/crons/j1", json={"timeout_secs": 60})
            assert resp.status == 400
            assert "must cover" in (await resp.json())["error"]


class TestList:
    @staticmethod
    def _request(job: CronJob) -> MagicMock:
        state = MagicMock()
        state.has_slot.return_value = False
        state.crons.list_jobs.return_value = [job]
        state.crons.list_jobs_async = AsyncMock(return_value=[job])
        state.crons.is_running.return_value = False
        state.crons.running_since.return_value = None
        request = MagicMock()
        request.app = {"state": state}
        return request

    async def _listed(self, job: CronJob) -> int:
        resp = await api_crons(self._request(job))
        return json.loads(resp.body)["jobs"][0]["timeout_secs"]

    async def test_a_set_limit_is_returned(self) -> None:
        assert await self._listed(_job(timeout_secs=5400)) == 5400

    async def test_a_job_without_one_reports_the_default(self) -> None:
        assert await self._listed(_job()) == _JOB_TIMEOUT_SECS

    async def test_an_out_of_range_stored_value_reports_what_a_run_gets(self) -> None:
        assert await self._listed(_job(timeout_secs=999999)) == _JOB_TIMEOUT_SECS


class TestRealStore:
    """Against a real ``CronService``: what the form sends is what a run gets,
    and the store's cover check refuses a budget a command job cannot finish in."""

    @staticmethod
    def _service(tmp_path):
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path)
        svc._dir.mkdir(parents=True, exist_ok=True)
        return svc

    async def test_a_patched_limit_is_the_runs_budget(self, tmp_path) -> None:
        from kiro_crew.cron_service.execution import effective_wake_budget

        svc = self._service(tmp_path)
        job = svc.add_job("poller", "Check it.", every_secs=3600)
        app = _app(api_cron_update, "/api/crons/{job_id}", update_job_async=svc.update_job_async)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch(f"/api/crons/{job.id}", json={"timeout_secs": 5400})
            assert resp.status == 200
        assert effective_wake_budget(svc.get_job(job.id)) == 5400

    async def test_a_budget_shorter_than_a_command_needs_is_refused(self, tmp_path) -> None:
        svc = self._service(tmp_path)
        job = svc.add_job("disk", "", every_secs=3600, command="df -h")
        app = _app(api_cron_update, "/api/crons/{job_id}", update_job_async=svc.update_job_async)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch(f"/api/crons/{job.id}", json={"timeout_secs": 60})
            assert resp.status == 400
            assert "must cover" in (await resp.json())["error"]
        assert svc.get_job(job.id).timeout_secs == _JOB_TIMEOUT_SECS
