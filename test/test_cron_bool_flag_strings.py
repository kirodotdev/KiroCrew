"""Bool job flags read leniently: ``POST``/``PATCH`` take a string ``"false"``.

Four flags -- ``silent``, ``strict_schedule``, ``hide_in_chat`` and
``minimal_context`` -- are coerced with ``bool(...)`` downstream, under which the
string ``"false"`` is truthy and turns the flag ON. This route reads
``"true"``/``"false"`` (any case) as the bool they name, keeps the ``bool(...)``
reading for any other non-bool, and adds a ``warnings`` entry for either. A real
JSON bool is unchanged with no warning, so no valid value changes meaning.

``persistent_session`` feeds cross-field refusals (a chat folder needs it on; a
new-chat-per-run job needs it off), so its value is NOT changed -- coercing a
string there could newly reject a request main answers 200. It only earns a
warning, and a request that main accepted stays accepted.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.cron import CronService
from kiro_crew.dashboard.handlers.cron import api_cron_update, api_crons_create


@web.middleware
async def _owner(request: web.Request, handler):  # type: ignore[no-untyped-def]
    request["user"] = "local-app"
    request["app"] = ""
    return await handler(request)


def _app(crons: object) -> web.Application:
    app = web.Application(middlewares=[_owner])
    app["state"] = SimpleNamespace(
        crons=crons,
        push_refresh=MagicMock(),
        ack_notification=AsyncMock(),
        has_slot=MagicMock(return_value=False),
    )
    app.router.add_post("/api/crons", api_crons_create)
    app.router.add_patch("/api/crons/{job_id}", api_cron_update)
    return app


@pytest.mark.asyncio
async def test_create_reads_string_false_as_false_and_warns(tmp_path) -> None:
    svc = CronService(base_dir=tmp_path)
    async with TestClient(TestServer(_app(svc))) as client:
        resp = await client.post(
            "/api/crons",
            json={"name": "n", "message": "m", "every": 600, "silent": "false"},
        )
        payload = json.loads(await resp.text())

    assert resp.status == 200, payload
    (job,) = svc.list_jobs(include_disabled=True)
    # A bare bool("false") would be True; the lenient read stores False.
    assert job.silent is False
    assert any("silent" in w for w in payload.get("warnings", []))


@pytest.mark.asyncio
async def test_create_reads_mixed_case_true_string(tmp_path) -> None:
    svc = CronService(base_dir=tmp_path)
    async with TestClient(TestServer(_app(svc))) as client:
        resp = await client.post(
            "/api/crons",
            json={"name": "n", "message": "m", "every": 600, "hide_in_chat": "TRUE"},
        )
        payload = json.loads(await resp.text())

    assert resp.status == 200, payload
    (job,) = svc.list_jobs(include_disabled=True)
    assert job.hide_in_chat is True
    assert any("hide_in_chat" in w for w in payload.get("warnings", []))


@pytest.mark.asyncio
async def test_create_keeps_a_real_bool_unchanged_with_no_warning(tmp_path) -> None:
    svc = CronService(base_dir=tmp_path)
    async with TestClient(TestServer(_app(svc))) as client:
        resp = await client.post(
            "/api/crons",
            json={"name": "n", "message": "m", "every": 600, "minimal_context": True},
        )
        payload = json.loads(await resp.text())

    assert resp.status == 200, payload
    (job,) = svc.list_jobs(include_disabled=True)
    assert job.minimal_context is True
    assert "warnings" not in payload


@pytest.mark.asyncio
async def test_create_keeps_main_reading_for_an_odd_value_and_warns(tmp_path) -> None:
    # A non-bool that is not "true"/"false" keeps main's bool() reading (1 -> True)
    # and is NOT newly rejected; a warning is added.
    svc = CronService(base_dir=tmp_path)
    async with TestClient(TestServer(_app(svc))) as client:
        resp = await client.post(
            "/api/crons",
            json={"name": "n", "message": "m", "every": 600, "strict_schedule": 1},
        )
        payload = json.loads(await resp.text())

    assert resp.status == 200, payload
    (job,) = svc.list_jobs(include_disabled=True)
    assert job.strict_schedule is True
    assert any("strict_schedule" in w for w in payload.get("warnings", []))


@pytest.mark.asyncio
async def test_update_reads_a_string_bool_flag_as_the_bool_and_warns(tmp_path) -> None:
    svc = CronService(base_dir=tmp_path)
    async with TestClient(TestServer(_app(svc))) as client:
        created = json.loads(
            await (
                await client.post("/api/crons", json={"name": "n", "message": "m", "every": 600})
            ).text()
        )
        job_id = created["id"]
        resp = await client.patch(f"/api/crons/{job_id}", json={"silent": "false"})
        payload = json.loads(await resp.text())

    assert resp.status == 200, payload
    (job,) = svc.list_jobs(include_disabled=True)
    assert job.silent is False
    assert any("silent" in w for w in payload.get("warnings", []))


@pytest.mark.asyncio
async def test_persistent_session_string_keeps_its_bool_reading_and_only_warns(tmp_path) -> None:
    # persistent_session feeds cross-field refusals, so its value is NOT changed:
    # bool("false") is True on main, and it stays True here (no newly rejected
    # request), with only a warning added. Changing it could 400 a chat-folder or
    # new-chat-per-run combination main answers 200.
    svc = CronService(base_dir=tmp_path)
    async with TestClient(TestServer(_app(svc))) as client:
        resp = await client.post(
            "/api/crons",
            json={"name": "n", "message": "m", "every": 600, "persistent_session": "false"},
        )
        payload = json.loads(await resp.text())

    assert resp.status == 200, payload
    (job,) = svc.list_jobs(include_disabled=True)
    assert job.persistent_session is True
    assert any("persistent_session" in w for w in payload.get("warnings", []))


def test_persistent_session_is_warned_about_but_never_coerced() -> None:
    # A guard so a later edit cannot quietly move persistent_session into the
    # coerced set and reintroduce the back-compat break.
    from kiro_crew.dashboard.handlers import cron_jobs

    assert "persistent_session" not in cron_jobs._COERCED_BOOL_FLAGS


def test_a_flag_string_reads_like_the_shared_config_reader() -> None:
    # One reader: every spelling config.fields._coerce_bool knows means the same
    # thing on the cron route ("off" and "0" are False, "yes" is True).
    from kiro_crew.config.fields import _coerce_bool
    from kiro_crew.dashboard.handlers import cron_jobs

    for spelling in ("false", "0", "no", "off", "true", "1", "yes", "on", " OFF "):
        coerced, warning = cron_jobs._coerce_bool_flag("silent", spelling)
        assert coerced is _coerce_bool(spelling, True)
        assert warning is not None
