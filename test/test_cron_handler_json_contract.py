"""JSON object contract for the cron and lesson mutation handlers.

``await request.json()`` returns whatever the body parsed to, and a scalar,
an array, or ``null`` is perfectly valid JSON. The handlers below then read
fields off it. There is no ``.get`` on a list and no string key lookup on a
list or an int, so the read raises out of the handler and aiohttp answers
**500** -- an internal-server error for a request the caller malformed, and one
that reports nothing a client can act on.

The handlers route through ``read_bounded_json`` (issue #5587), which owns
both the parse guard and the object-shape guard:

* ``api_cron_update`` and ``api_lessons_delete`` refuse an unparseable body
  with the helper's 400 ``invalid_json`` and a non-object with 400
  ``body_not_object``.
* ``api_cron_enable`` and ``api_cron_ack`` keep their defaults only for an
  ABSENT body (``allow_absent``): "the client sent nothing" and "the client
  sent garbage" are different facts, so a body that is present but not an
  object is a 400; only an absent body defaults.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers.cron import (
    api_cron_ack,
    api_cron_enable,
    api_cron_update,
    api_lessons_delete,
)

pytestmark = pytest.mark.asyncio

# Bodies that parse cleanly and then have no fields to read. The string is not
# arbitrary: ``api_cron_update`` probes membership with ``"model" in body``
# before subscripting, and that is true of any string containing the substring.
# Sent as raw bytes rather than through the client's ``json=`` helper: a literal
# ``null`` body is a real request an HTTP client can make, and ``json=None``
# would send no body at all -- which is the already-handled unreadable case, not
# this one.
NON_OBJECT_BODIES = ["[]", '["name"]', '"model"', "5", "null"]
JSON_HEADERS = {"Content-Type": "application/json"}


def _cron_app(handler, route: str, **store) -> web.Application:
    app = web.Application()
    app["state"] = SimpleNamespace(
        crons=SimpleNamespace(**store),
        push_refresh=MagicMock(),
        ack_notification=AsyncMock(),
    )
    app.router.add_route("*", route, handler)
    return app


@pytest.mark.parametrize("payload", NON_OBJECT_BODIES)
async def test_cron_update_refuses_a_non_object_body(payload) -> None:
    """The reject-shaped contract: 400 with a code, and the store is untouched."""
    update = AsyncMock()
    app = _cron_app(api_cron_update, "/api/crons/{job_id}", update_job_async=update)

    async with TestClient(TestServer(app)) as client:
        response = await client.patch("/api/crons/job-1", data=payload, headers=JSON_HEADERS)
        body = await response.json()

    assert response.status == 400
    assert body == {"error": "body must be a JSON object", "code": "body_not_object"}
    update.assert_not_awaited()


async def test_cron_update_keeps_the_object_path() -> None:
    """The guard must refuse only non-objects: a real patch still reaches the store."""
    job = SimpleNamespace(id="job-1", agent_id="", to_dict=lambda: {"id": "job-1"})
    update = AsyncMock(return_value=job)
    app = _cron_app(api_cron_update, "/api/crons/{job_id}", update_job_async=update)

    async with TestClient(TestServer(app)) as client:
        response = await client.patch("/api/crons/job-1", json={"name": "renamed"})

    assert response.status == 200
    update.assert_awaited_once()
    assert update.await_args.kwargs["name"] == "renamed"


@pytest.mark.parametrize("payload", NON_OBJECT_BODIES)
async def test_cron_enable_refuses_a_non_object_body(payload) -> None:
    """A present non-object body is a 400, not a silent default: only an
    ABSENT body keeps the route's tolerance (``allow_absent``)."""
    enable = AsyncMock(return_value=True)
    app = _cron_app(api_cron_enable, "/api/crons/{job_id}/enable", enable_job_async=enable)

    async with TestClient(TestServer(app)) as client:
        response = await client.post("/api/crons/job-1/enable", data=payload, headers=JSON_HEADERS)
        body = await response.json()

    assert response.status == 400
    assert body == {"error": "body must be a JSON object", "code": "body_not_object"}
    enable.assert_not_awaited()


async def test_cron_enable_treats_an_absent_body_as_defaults() -> None:
    """No body at all still means the route's defaults -- the tolerant half
    of the old contract that ``allow_absent`` preserves."""
    enable = AsyncMock(return_value=True)
    app = _cron_app(api_cron_enable, "/api/crons/{job_id}/enable", enable_job_async=enable)

    async with TestClient(TestServer(app)) as client:
        response = await client.post("/api/crons/job-1/enable")

    assert response.status == 200
    enable.assert_awaited_once_with("job-1", enabled=True)


async def test_cron_enable_still_reads_an_object_body() -> None:
    enable = AsyncMock(return_value=True)
    app = _cron_app(api_cron_enable, "/api/crons/{job_id}/enable", enable_job_async=enable)

    async with TestClient(TestServer(app)) as client:
        response = await client.post("/api/crons/job-1/enable", json={"enabled": False})

    assert response.status == 200
    enable.assert_awaited_once_with("job-1", enabled=False)


@pytest.mark.parametrize("payload", NON_OBJECT_BODIES)
async def test_cron_ack_refuses_a_non_object_body(payload) -> None:
    ack = AsyncMock(return_value=True)
    app = _cron_app(api_cron_ack, "/api/crons/{job_id}/ack", ack_job_async=ack)

    async with TestClient(TestServer(app)) as client:
        response = await client.post("/api/crons/job-1/ack", data=payload, headers=JSON_HEADERS)
        body = await response.json()

    assert response.status == 400
    assert body == {"error": "body must be a JSON object", "code": "body_not_object"}
    ack.assert_not_awaited()
    app["state"].ack_notification.assert_not_awaited()


async def test_cron_ack_treats_an_absent_body_as_defaults() -> None:
    ack = AsyncMock(return_value=True)
    app = _cron_app(api_cron_ack, "/api/crons/{job_id}/ack", ack_job_async=ack)

    async with TestClient(TestServer(app)) as client:
        response = await client.post("/api/crons/job-1/ack")

    assert response.status == 200
    ack.assert_awaited_once_with("job-1", "acknowledged")
    # The notification half of the route reads a second field off the same body;
    # with no body there is no timestamp to acknowledge.
    app["state"].ack_notification.assert_not_awaited()


async def test_cron_ack_still_reads_an_object_body() -> None:
    ack = AsyncMock(return_value=True)
    app = _cron_app(api_cron_ack, "/api/crons/{job_id}/ack", ack_job_async=ack)

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/crons/job-1/ack", json={"summary": "seen", "ts": "2026-05-13T00:00:00Z"}
        )

    assert response.status == 200
    ack.assert_awaited_once_with("job-1", "seen")
    app["state"].ack_notification.assert_awaited_once_with("2026-05-13T00:00:00Z")


def _lessons_app() -> web.Application:
    app = web.Application()
    app["state"] = SimpleNamespace(lessons=SimpleNamespace(remove=MagicMock(return_value=0)))
    app.router.add_route("*", "/api/lessons", api_lessons_delete)
    return app


@pytest.mark.parametrize("payload", NON_OBJECT_BODIES)
async def test_lessons_delete_refuses_a_non_object_body(payload) -> None:
    """Reject-shaped, like its create sibling, and reached only after the
    session gate -- so the guard is proven on the path a real caller takes."""
    app = _lessons_app()

    with (
        patch("kiro_crew.dashboard.handlers.cron._recognize_session", AsyncMock(return_value=None)),
        patch("kiro_crew.dashboard.handlers.cron._blocks_reads_session", return_value=False),
        patch("kiro_crew.dashboard.handlers.cron._sel"),
    ):
        async with TestClient(TestServer(app)) as client:
            response = await client.delete(
                "/api/lessons",
                data=payload,
                headers={"X-Session-Key": "dashboard:ui", **JSON_HEADERS},
            )
            body = await response.json()

    assert response.status == 400
    assert body == {"error": "body must be a JSON object", "code": "body_not_object"}
    app["state"].lessons.remove.assert_not_called()


async def test_lessons_delete_keeps_the_object_path() -> None:
    """A missing rule is still the 400 it was; the new guard sits above it and
    does not swallow the field-level contract."""
    app = _lessons_app()

    with (
        patch("kiro_crew.dashboard.handlers.cron._recognize_session", AsyncMock(return_value=None)),
        patch("kiro_crew.dashboard.handlers.cron._blocks_reads_session", return_value=False),
        patch("kiro_crew.dashboard.handlers.cron._sel"),
    ):
        async with TestClient(TestServer(app)) as client:
            response = await client.delete(
                "/api/lessons", json={}, headers={"X-Session-Key": "dashboard:ui"}
            )
            body = await response.json()

    assert response.status == 400
    assert body == {"error": "rule substring required"}
