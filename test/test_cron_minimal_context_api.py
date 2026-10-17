"""``minimal_context`` over the dashboard's cron REST surface.

A cron job dispatched to the agent pays for its whole injected context on every
wake, whether or not the wake had anything to do. ``minimal_context`` is how a
routine job stops paying for context it never reads, and the store has carried
the flag since the tool path gained it. The three HTTP surfaces did not: create
dropped it, update would not accept it, and the list payload omitted it. A job
created from the dashboard therefore could not opt out without a later edit from
chat or the CLI, and even that could not be seen or undone from the form.

The omission from the LIST payload was the one that could corrupt state rather
than merely withhold a setting: with the field absent, the edit form defaults its
control to off, so saving any unrelated change on a job that HAD the flag set
would silently clear it. That is why the read side is pinned here alongside the
two write sides.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.cron import CronJob
from kiro_crew.dashboard.handlers.cron import api_cron_update, api_crons, api_crons_create

pytestmark = pytest.mark.asyncio


def _app(handler, route: str, **store) -> web.Application:
    app = web.Application()
    app["state"] = SimpleNamespace(
        crons=SimpleNamespace(**store),
        push_refresh=MagicMock(),
        ack_notification=AsyncMock(),
        has_slot=MagicMock(return_value=False),
    )
    app.router.add_route("*", route, handler)
    return app


def _job(**over) -> CronJob:
    fields = {"id": "j1", "name": "poller", "message": "Check the timestamp."}
    fields.update(over)
    return CronJob(**fields)


class TestCreate:
    async def test_true_reaches_the_store(self) -> None:
        add = AsyncMock(return_value=_job(minimal_context=True))
        app = _app(api_crons_create, "/api/crons", add_job_async=add)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/crons",
                json={
                    "name": "poller",
                    "message": "Check the timestamp.",
                    "every": 3600,
                    "minimal_context": True,
                },
            )
            assert resp.status == 200
        assert add.await_args.kwargs["minimal_context"] is True

    async def test_absent_field_defaults_to_a_full_context(self) -> None:
        """Omitting it must not quietly opt an existing client's jobs out."""
        add = AsyncMock(return_value=_job())
        app = _app(api_crons_create, "/api/crons", add_job_async=add)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/crons",
                json={"name": "poller", "message": "Check it.", "every": 3600},
            )
            assert resp.status == 200
        assert add.await_args.kwargs["minimal_context"] is False

    async def test_a_truthy_non_bool_is_coerced_not_stored_raw(self) -> None:
        add = AsyncMock(return_value=_job(minimal_context=True))
        app = _app(api_crons_create, "/api/crons", add_job_async=add)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/crons",
                json={
                    "name": "poller",
                    "message": "Check it.",
                    "every": 3600,
                    "minimal_context": "yes",
                },
            )
            assert resp.status == 200
        stored = add.await_args.kwargs["minimal_context"]
        assert stored is True and isinstance(stored, bool)


class TestList:
    """The read side, exercised by calling the handler directly: the list payload
    reaches the form as JSON, so what matters is the serialized field."""

    @staticmethod
    def _payload(job: CronJob) -> dict:
        state = MagicMock()
        state.has_slot.return_value = False
        state.crons.list_jobs.return_value = [job]
        state.crons.list_jobs_async = AsyncMock(return_value=[job])
        state.crons.is_running.return_value = False
        state.crons.running_since.return_value = None
        request = MagicMock()
        request.app = {"state": state}
        return request, state

    async def test_the_field_is_returned_so_the_form_can_show_the_real_setting(self) -> None:
        """Absent here, the edit form defaults its control to off and a save
        clears a flag the user set. That is the corrupting case, not a cosmetic
        one, so the read side is pinned too."""
        request, _ = self._payload(_job(minimal_context=True))
        resp = await api_crons(request)
        assert json.loads(resp.body)["jobs"][0]["minimal_context"] is True

    async def test_a_job_without_the_flag_reports_false_rather_than_omitting_it(self) -> None:
        request, _ = self._payload(_job())
        resp = await api_crons(request)
        assert json.loads(resp.body)["jobs"][0]["minimal_context"] is False


class TestUpdate:
    async def test_the_field_is_forwarded(self) -> None:
        update = AsyncMock(return_value=_job(minimal_context=True))
        app = _app(api_cron_update, "/api/crons/{job_id}", update_job_async=update)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/crons/j1", json={"minimal_context": True})
            assert resp.status == 200
        assert update.await_args.kwargs["minimal_context"] is True

    async def test_turning_it_back_off_is_forwarded_rather_than_read_as_absent(self) -> None:
        update = AsyncMock(return_value=_job(minimal_context=False))
        app = _app(api_cron_update, "/api/crons/{job_id}", update_job_async=update)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/crons/j1", json={"minimal_context": False})
            assert resp.status == 200
        assert update.await_args.kwargs["minimal_context"] is False

    async def test_an_unrelated_patch_leaves_the_flag_alone(self) -> None:
        update = AsyncMock(return_value=_job(minimal_context=True))
        app = _app(api_cron_update, "/api/crons/{job_id}", update_job_async=update)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/crons/j1", json={"name": "renamed"})
            assert resp.status == 200
        assert "minimal_context" not in update.await_args.kwargs
