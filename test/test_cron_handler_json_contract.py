"""JSON object contract for the cron and lesson mutation handlers.

``await request.json()`` returns whatever the body parsed to, and a scalar,
an array, or ``null`` is perfectly valid JSON. The handlers below then read
fields off it. There is no ``.get`` on a list and no string key lookup on a
list or an int, so the read raises out of the handler and aiohttp answers
**500** -- an internal-server error for a request the caller malformed, and one
that reports nothing a client can act on.

The two contracts already in this module are both kept, because a handler's
answer to a non-object body should match its answer to a body it could not read
at all:

* ``api_cron_update`` and ``api_lessons_delete`` already refuse an unparseable
  body with 400, so a non-object is refused the same way, with the
  ``invalid_json`` code the module uses elsewhere.
* ``api_cron_enable`` and ``api_cron_ack`` already fall back to their defaults
  when the body cannot be read, so a non-object gets that same tolerance. A
  scalar carries no fields either way, and answering it with a 500 while
  answering unreadable bytes with 200 is the asymmetry, not the rule.
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
    assert body == {"error": "request body must be a JSON object", "code": "invalid_json"}
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
async def test_cron_enable_treats_a_non_object_body_as_no_body(payload) -> None:
    """The tolerant contract: the same answer an unreadable body already gets --
    the route's default, not a 500."""
    enable = AsyncMock(return_value=True)
    app = _cron_app(api_cron_enable, "/api/crons/{job_id}/enable", enable_job_async=enable)

    async with TestClient(TestServer(app)) as client:
        response = await client.post("/api/crons/job-1/enable", data=payload, headers=JSON_HEADERS)

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
async def test_cron_ack_treats_a_non_object_body_as_no_body(payload) -> None:
    ack = AsyncMock(return_value=True)
    app = _cron_app(api_cron_ack, "/api/crons/{job_id}/ack", ack_job_async=ack)

    async with TestClient(TestServer(app)) as client:
        response = await client.post("/api/crons/job-1/ack", data=payload, headers=JSON_HEADERS)

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
    assert body == {"error": "request body must be a JSON object", "code": "invalid_json"}
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
