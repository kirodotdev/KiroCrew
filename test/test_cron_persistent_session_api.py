"""``persistent_session`` over the dashboard's cron REST surface.

``persistent_session`` decides whether every run of an agent cron resumes one
long-lived ``cron:<job_id>`` session (True, the default) or gets a fresh
``cron:<job_id>:<run_id>`` session (False). The store and the MCP cron tool have
carried the flag for a long time; the three dashboard HTTP surfaces did not:
create dropped it, update would not accept it, and the list payload omitted it.
A job created or edited from the dashboard therefore could not see or control
the setting at all -- the same hand-maintained-field-list gap that
``test_cron_minimal_context_api.py`` pins for ``minimal_context``.

The list-payload omission is the one that can corrupt state rather than merely
withhold a setting: the moment the edit form grows a control for the field, an
absent value defaults that control, and saving any unrelated change on the job
silently rewrites the flag. That is why the read side is pinned here alongside
the two write sides, even before a form control exists.

The one place this flag differs from its siblings is its default: an absent
field must keep the job PERSISTENT, because that is what the store and the tool
path default to and what every existing dashboard-created job already is.
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
    async def test_false_reaches_the_store(self) -> None:
        """The only way a dashboard-created job can opt into a fresh session per run."""
        add = AsyncMock(return_value=_job(persistent_session=False))
        app = _app(api_crons_create, "/api/crons", add_job_async=add)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/crons",
                json={
                    "name": "poller",
                    "message": "Check the timestamp.",
                    "every": 3600,
                    "persistent_session": False,
                },
            )
            assert resp.status == 200
        assert add.await_args.kwargs["persistent_session"] is False

    async def test_absent_field_defaults_to_a_persistent_session(self) -> None:
        """Omitting it must not quietly flip an existing client's jobs to
        ephemeral sessions: the store, the tool path and every job created so
        far default to True, so the REST default has to match."""
        add = AsyncMock(return_value=_job())
        app = _app(api_crons_create, "/api/crons", add_job_async=add)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/crons",
                json={"name": "poller", "message": "Check it.", "every": 3600},
            )
            assert resp.status == 200
        assert add.await_args.kwargs["persistent_session"] is True

    async def test_a_truthy_non_bool_is_coerced_not_stored_raw(self) -> None:
        add = AsyncMock(return_value=_job(persistent_session=True))
        app = _app(api_crons_create, "/api/crons", add_job_async=add)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/crons",
                json={
                    "name": "poller",
                    "message": "Check it.",
                    "every": 3600,
                    "persistent_session": "yes",
                },
            )
            assert resp.status == 200
        stored = add.await_args.kwargs["persistent_session"]
        assert stored is True and isinstance(stored, bool)


class TestList:
    """The read side, exercised by calling the handler directly: the list payload
    reaches the form as JSON, so what matters is the serialized field."""

    @staticmethod
    def _payload(job: CronJob):
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
        """An ephemeral job must read back as ephemeral. Absent, a future form
        control would default to the store's True and a save would silently
        re-enable the persistent session the user turned off."""
        request, _ = self._payload(_job(persistent_session=False))
        resp = await api_crons(request)
        assert json.loads(resp.body)["jobs"][0]["persistent_session"] is False

    async def test_a_default_job_reports_true_rather_than_omitting_it(self) -> None:
        request, _ = self._payload(_job())
        resp = await api_crons(request)
        assert json.loads(resp.body)["jobs"][0]["persistent_session"] is True


class TestUpdate:
    async def test_turning_it_off_is_forwarded(self) -> None:
        update = AsyncMock(return_value=_job(persistent_session=False))
        app = _app(api_cron_update, "/api/crons/{job_id}", update_job_async=update)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/crons/j1", json={"persistent_session": False})
            assert resp.status == 200
        assert update.await_args.kwargs["persistent_session"] is False

    async def test_turning_it_back_on_is_forwarded_rather_than_read_as_absent(self) -> None:
        update = AsyncMock(return_value=_job(persistent_session=True))
        app = _app(api_cron_update, "/api/crons/{job_id}", update_job_async=update)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/crons/j1", json={"persistent_session": True})
            assert resp.status == 200
        assert update.await_args.kwargs["persistent_session"] is True

    async def test_an_unrelated_patch_leaves_the_flag_alone(self) -> None:
        """A partial update must not carry the default along: forwarding
        ``persistent_session=True`` on a rename would silently re-enable the
        persistent session on a job the user set to ephemeral."""
        update = AsyncMock(return_value=_job(persistent_session=False))
        app = _app(api_cron_update, "/api/crons/{job_id}", update_job_async=update)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/crons/j1", json={"name": "renamed"})
            assert resp.status == 200
        assert "persistent_session" not in update.await_args.kwargs
