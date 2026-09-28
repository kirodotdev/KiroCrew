"""JSON object contract for the legacy workflow mutation handlers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers.workflows import (
    api_workflow_author,
    api_workflow_run,
    api_workflow_run_intent,
    api_workflow_run_rerun,
)

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    ("route", "path", "handler", "service_method"),
    [
        ("/api/workflows/author", "/api/workflows/author", api_workflow_author, "author"),
        ("/api/workflows/run", "/api/workflows/run", api_workflow_run, "start"),
        (
            "/api/workflows/run_intent",
            "/api/workflows/run_intent",
            api_workflow_run_intent,
            "start_from_intent",
        ),
        (
            "/api/workflows/runs/{run_id}/rerun",
            "/api/workflows/runs/wf_1/rerun",
            api_workflow_run_rerun,
            "rerun_subtree",
        ),
    ],
)
@pytest.mark.parametrize("payload", [[], "not-an-object"])
async def test_mutation_handlers_reject_non_object_json(
    route, path, handler, service_method, payload
) -> None:
    method = AsyncMock()
    app = web.Application()
    app["state"] = SimpleNamespace(workflow_service=SimpleNamespace(**{service_method: method}))
    app.router.add_post(route, handler)

    async with TestClient(TestServer(app)) as client:
        response = await client.post(path, json=payload)
        body = await response.json()

    assert response.status == 400
    assert body == {"error": "body must be a JSON object", "code": "body_not_object"}
    method.assert_not_awaited()


@pytest.mark.parametrize(
    ("route", "path", "handler", "service_method", "payload"),
    [
        (
            "/api/workflows/author",
            "/api/workflows/author",
            api_workflow_author,
            "author",
            {"intent": "ship it"},
        ),
        (
            "/api/workflows/run",
            "/api/workflows/run",
            api_workflow_run,
            "start",
            {"source": "print('ok')"},
        ),
        (
            "/api/workflows/run_intent",
            "/api/workflows/run_intent",
            api_workflow_run_intent,
            "start_from_intent",
            {"intent": "ship it"},
        ),
        (
            "/api/workflows/runs/{run_id}/rerun",
            "/api/workflows/runs/wf_1/rerun",
            api_workflow_run_rerun,
            "rerun_subtree",
            {"from_index": 0},
        ),
    ],
)
async def test_mutation_handlers_keep_valid_object_path(
    route, path, handler, service_method, payload
) -> None:
    method = AsyncMock(return_value={"run_id": "wf_2"})
    app = web.Application()
    app["state"] = SimpleNamespace(workflow_service=SimpleNamespace(**{service_method: method}))
    app.router.add_post(route, handler)

    async with TestClient(TestServer(app)) as client:
        response = await client.post(path, json=payload)

    assert response.status == 200
    method.assert_awaited_once()


async def test_runs_list_is_compact_and_detail_carries_result() -> None:
    """GET /api/workflows/runs ships no result payloads; the detail does.

    A finished run's result can be hundreds of KB, so the list view carries
    metadata only and the payload rides on ``GET /api/workflows/runs/{id}``.
    Both read endpoints redact + serialize on a worker thread; the response
    must still be well-formed JSON.
    """
    from kiro_crew.dashboard.handlers.workflows import (
        api_workflow_run_get,
        api_workflow_runs,
    )

    big_result = {"report": "x" * 50_000}
    compact_row = {"run_id": "wf_1", "name": "d", "status": "finished"}
    detail = {"run_id": "wf_1", "status": "finished", "result": big_result, "events": []}
    svc = SimpleNamespace(list_runs=lambda: [compact_row], result=lambda _rid: detail)
    app = web.Application()
    app["state"] = SimpleNamespace(workflow_service=svc)
    app.router.add_get("/api/workflows/runs", api_workflow_runs)
    app.router.add_get("/api/workflows/runs/{run_id}", api_workflow_run_get)

    async with TestClient(TestServer(app)) as client:
        listing = await client.get("/api/workflows/runs")
        assert listing.status == 200
        assert listing.content_type == "application/json"
        rows = (await listing.json())["runs"]
        assert rows == [compact_row]
        assert "result" not in rows[0]

        got = await client.get("/api/workflows/runs/wf_1")
        assert got.status == 200
        assert (await got.json())["result"] == big_result
