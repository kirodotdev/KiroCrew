"""``auto_pause_after_failures`` over the dashboard's cron REST surface.

The Schedule page edit form shows and changes a job's auto-pause limit. That
needs the list payload to carry the stored value (otherwise the form shows a
guess and a save writes it back), and the PATCH route to validate the value
before the store sees it and to keep the never-pause audit row that every
other write surface produces.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import cron as cron_facade
from kiro_crew.cron import _AUTO_PAUSE_THRESHOLD, CronJob, CronService
from kiro_crew.cron_service.model import _AUTO_PAUSE_MAX
from kiro_crew.dashboard.handlers.cron import api_cron_update, api_crons

pytestmark = pytest.mark.asyncio


def _app(crons: object, *, app_claim: str | None = None) -> web.Application:
    middlewares = []
    if app_claim is not None:
        # Stands in for token_auth_middleware publishing an app token's claim.
        @web.middleware
        async def _claim(request: web.Request, handler):  # type: ignore[no-untyped-def]
            request["app"] = app_claim
            return await handler(request)

        middlewares.append(_claim)
    app = web.Application(middlewares=middlewares)
    app["state"] = SimpleNamespace(
        crons=crons,
        push_refresh=MagicMock(),
        ack_notification=AsyncMock(),
        has_slot=MagicMock(return_value=False),
    )
    app.router.add_route("*", "/api/crons/{job_id}", api_cron_update)
    return app


def _job(**over: object) -> CronJob:
    fields: dict[str, object] = {"id": "j1", "name": "poller", "message": "Check it."}
    fields.update(over)
    return CronJob(**fields)  # type: ignore[arg-type]


class TestList:
    @staticmethod
    async def _listed(job: CronJob) -> dict:
        state = MagicMock()
        state.has_slot.return_value = False
        state.crons.list_jobs_async = AsyncMock(return_value=[job])
        state.crons.is_running.return_value = False
        state.crons.running_since.return_value = None
        request = MagicMock()
        request.app = {"state": state}
        resp = await api_crons(request)
        return json.loads(resp.body)["jobs"][0]

    async def test_a_default_job_reports_the_default_limit(self) -> None:
        row = await self._listed(_job())
        assert row["auto_pause_after_failures"] == _AUTO_PAUSE_THRESHOLD

    async def test_never_pause_is_reported_as_zero_not_omitted(self) -> None:
        row = await self._listed(_job(auto_pause_after_failures=0))
        assert row["auto_pause_after_failures"] == 0


class TestUpdate:
    async def test_the_value_reaches_the_store_with_the_dashboard_as_actor(self) -> None:
        update = AsyncMock(return_value=_job(auto_pause_after_failures=12))
        app = _app(SimpleNamespace(update_job_async=update))
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/crons/j1", json={"auto_pause_after_failures": 12})
            assert resp.status == 200
        assert update.await_args.kwargs["auto_pause_after_failures"] == 12
        assert update.await_args.kwargs["audit_actor"] == "dashboard"

    async def test_an_app_token_write_is_filed_under_the_app(self) -> None:
        update = AsyncMock(return_value=_job(auto_pause_after_failures=0))
        own_job = _job(created_by="app:poller-app")
        app = _app(
            SimpleNamespace(update_job_async=update, get_job=MagicMock(return_value=own_job)),
            app_claim="poller-app",
        )
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/crons/j1", json={"auto_pause_after_failures": 0})
            assert resp.status == 200
        assert update.await_args.kwargs["audit_actor"] == "app:poller-app"

    async def test_a_forwarded_owner_write_keeps_the_forwarded_marker(self) -> None:
        update = AsyncMock(return_value=_job(auto_pause_after_failures=0))
        app = _app(SimpleNamespace(update_job_async=update))
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch(
                "/api/crons/j1",
                json={"auto_pause_after_failures": 0},
                headers={"X-Forwarded-For": "203.0.113.7"},
            )
            assert resp.status == 200
        actor = update.await_args.kwargs["audit_actor"]
        assert actor.startswith("dashboard") and actor != "dashboard"

    async def test_an_unrelated_patch_does_not_send_the_limit(self) -> None:
        update = AsyncMock(return_value=_job())
        app = _app(SimpleNamespace(update_job_async=update))
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/crons/j1", json={"name": "renamed"})
            assert resp.status == 200
        assert "auto_pause_after_failures" not in update.await_args.kwargs

    @pytest.mark.parametrize("bad", [-1, _AUTO_PAUSE_MAX + 1, "five", 2.5, True, None, [5]])
    async def test_a_bad_value_is_a_400_and_nothing_is_written(self, bad: object) -> None:
        update = AsyncMock(return_value=_job())
        app = _app(SimpleNamespace(update_job_async=update))
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch(
                "/api/crons/j1", json={"auto_pause_after_failures": bad, "name": "renamed"}
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_auto_pause_after_failures"
        update.assert_not_awaited()


class TestAuditThroughTheRealStore:
    @pytest.fixture()
    def audited(self, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
        rows: list[dict] = []

        class _Sel:
            def log_tool_invocation(self, **kw: object) -> None:
                rows.append(kw)

        monkeypatch.setattr(cron_facade.sel, "sel", lambda: _Sel())
        return rows

    async def test_setting_never_pause_writes_the_limit_row(
        self, tmp_path: Path, audited: list[dict]
    ) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="j", message="m", every_secs=60)
        audited.clear()
        async with TestClient(TestServer(_app(svc))) as client:
            resp = await client.patch(f"/api/crons/{job.id}", json={"auto_pause_after_failures": 0})
            assert resp.status == 200
        stored = svc.get_job(job.id)
        assert stored is not None and stored.auto_pause_after_failures == 0
        rows = [r for r in audited if r.get("tool_kind") == "cron_auto_pause_limit"]
        assert [r["outcome"] for r in rows] == ["never_pause_set"]
        assert rows[0]["session_key"] == "dashboard"
        assert rows[0]["metadata"]["previous_limit"] == _AUTO_PAUSE_THRESHOLD

    async def test_a_refused_value_leaves_the_stored_limit_and_writes_no_row(
        self, tmp_path: Path, audited: list[dict]
    ) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="j", message="m", every_secs=60, auto_pause_after_failures=3)
        audited.clear()
        async with TestClient(TestServer(_app(svc))) as client:
            resp = await client.patch(
                f"/api/crons/{job.id}", json={"auto_pause_after_failures": _AUTO_PAUSE_MAX + 1}
            )
            assert resp.status == 400
        stored = svc.get_job(job.id)
        assert stored is not None and stored.auto_pause_after_failures == 3
        assert [r for r in audited if r.get("tool_kind") == "cron_auto_pause_limit"] == []
