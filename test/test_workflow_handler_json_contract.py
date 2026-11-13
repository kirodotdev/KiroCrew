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


@pytest.mark.parametrize(
    ("route", "path", "handler", "service_method", "payload", "code"),
    [
        (
            "/api/workflows/author",
            "/api/workflows/author",
            api_workflow_author,
            "author",
            {"intent": "ship it"},
            "workflow_author_failed",
        ),
        (
            "/api/workflows/run",
            "/api/workflows/run",
            api_workflow_run,
            "start",
            {"source": "print('ok')"},
            "workflow_run_failed",
        ),
        (
            "/api/workflows/run_intent",
            "/api/workflows/run_intent",
            api_workflow_run_intent,
            "start_from_intent",
            {"intent": "ship it"},
            "workflow_run_intent_failed",
        ),
        (
            "/api/workflows/runs/{run_id}/rerun",
            "/api/workflows/runs/wf_1/rerun",
            api_workflow_run_rerun,
            "rerun_subtree",
            {"from_index": 0},
            "workflow_rerun_failed",
        ),
    ],
)
async def test_mutation_handlers_return_structured_500_on_service_error(
    route, path, handler, service_method, payload, code
) -> None:
    """A service-layer exception becomes a diagnosable JSON 500, not an opaque one.

    Without the handler-level try/except, a deterministic failure such as the
    glibc < 2.28 ``NotImplementedError("atomic no-replace rename is
    unavailable")`` raised deep in the service propagates as a bare aiohttp 500
    with no body, so a caller cannot tell a deterministic fault from a transient
    one. The handler must catch it and return ``{"error", "code"}`` with the
    handler's own stable code.
    """
    method = AsyncMock(side_effect=NotImplementedError("atomic no-replace rename is unavailable"))
    app = web.Application()
    app["state"] = SimpleNamespace(workflow_service=SimpleNamespace(**{service_method: method}))
    app.router.add_post(route, handler)

    async with TestClient(TestServer(app)) as client:
        response = await client.post(path, json=payload)
        body = await response.json()

    assert response.status == 500
    assert body["code"] == code
    assert isinstance(body["error"], str) and body["error"]
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


_INTENT_HANDLERS = [
    pytest.param("/api/workflows/author", api_workflow_author, "author", id="author"),
    pytest.param(
        "/api/workflows/run_intent", api_workflow_run_intent, "start_from_intent", id="run_intent"
    ),
]


@pytest.mark.parametrize(("path", "handler", "service_method"), _INTENT_HANDLERS)
@pytest.mark.parametrize("intent", [5, ["a"], {"a": 1}], ids=["int", "list", "dict"])
async def test_intent_handlers_answer_non_string_intent_with_coded_400(
    path, handler, service_method, intent
) -> None:
    """A truthy non-string intent is a coded 400, not a 500 from ``.strip()``."""
    method = AsyncMock()
    app = web.Application()
    app["state"] = SimpleNamespace(workflow_service=SimpleNamespace(**{service_method: method}))
    app.router.add_post(path, handler)

    async with TestClient(TestServer(app)) as client:
        response = await client.post(path, json={"intent": intent})
        body = await response.json()

    assert response.status == 400
    assert body == {"error": "intent must be a string", "code": "invalid_field_type"}
    method.assert_not_awaited()


@pytest.mark.parametrize(("path", "handler", "service_method"), _INTENT_HANDLERS)
@pytest.mark.parametrize("payload", [{}, {"intent": None}, {"intent": "  "}, {"intent": 0}])
async def test_intent_handlers_keep_missing_or_blank_intent_answer(
    path, handler, service_method, payload
) -> None:
    method = AsyncMock()
    app = web.Application()
    app["state"] = SimpleNamespace(workflow_service=SimpleNamespace(**{service_method: method}))
    app.router.add_post(path, handler)

    async with TestClient(TestServer(app)) as client:
        response = await client.post(path, json=payload)
        body = await response.json()

    assert response.status == 400
    assert body == {"error": "intent is required"}
    method.assert_not_awaited()


async def test_run_rejects_bad_folder_before_admission_reserves_capacity() -> None:
    """An unresolvable ``folder`` must 400 BEFORE fair admission reserves a slot.

    ``api_workflow_run`` admits the run (Shared Crew fair admission) and then
    starts it. If the folder were resolved AFTER admission, a bad folder would
    return a 400 while the ADMITTED record kept its reserved capacity — the
    deadline sweep ignores ADMITTED work and the probe sweep needs an
    execution_id, so each bad-folder request would permanently leak one of the
    caller's admission slots. The folder is therefore resolved first; this test
    pins that order by failing if admission is reached on a bad folder.
    """
    from unittest.mock import patch

    import kiro_crew.dashboard.handlers.workflows as wf

    start = AsyncMock(return_value={"run_id": "wf_never"})
    app = web.Application()
    app["state"] = SimpleNamespace(workflow_service=SimpleNamespace(start=start))
    app.router.add_post("/api/workflows/run", api_workflow_run)

    admit = AsyncMock()

    async def _bad_folder(_request, _ref):
        return "", web.json_response(
            {"error": "folder not found: Nope", "code": "folder_unresolvable"}, status=400
        )

    with (
        patch.object(wf, "_resolve_fallback_folder_id", _bad_folder),
        patch.object(wf, "_admit_workflow_run", admit),
    ):
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/api/workflows/run", json={"source": "print('ok')", "folder": "Nope"}
            )
            body = await response.json()

    assert response.status == 400
    assert body["code"] == "folder_unresolvable"
    admit.assert_not_awaited()  # admission never ran -> no slot leaked
    start.assert_not_awaited()


async def test_run_refuses_mate_home_folder_before_admission() -> None:
    """A fallback folder inside a member's home is a 403 ``mate_home_locked``
    returned BEFORE admission reserves a slot — the real resolver runs, so no
    slot is leaked and the run never starts."""
    from unittest.mock import patch

    import kiro_crew.dashboard.handlers.workflows as wf

    start = AsyncMock(return_value={"run_id": "wf_never"})
    folders = [
        {
            "id": "home-1",
            "name": "Home",
            "parent_id": "",
            "mate_home": True,
            "owner_app": "member:store-x",
        },
        {"id": "child-1", "name": "Inside", "parent_id": "home-1"},
    ]
    app = web.Application()
    app["state"] = SimpleNamespace(workflow_service=SimpleNamespace(start=start), _folders=folders)
    app.router.add_post("/api/workflows/run", api_workflow_run)

    admit = AsyncMock()

    async def _pass(_request, _op):
        return None

    with (
        patch.object(wf, "_private_memory_refusal", _pass),
        patch.object(wf, "_admit_workflow_run", admit),
    ):
        async with TestClient(TestServer(app)) as client:
            # Both the home itself (by id) and a folder inside it (by path) refuse.
            for ref in ("home-1", "Home/Inside"):
                response = await client.post(
                    "/api/workflows/run", json={"source": "print('ok')", "folder": ref}
                )
                body = await response.json()
                assert response.status == 403
                assert body["code"] == "mate_home_locked"

    admit.assert_not_awaited()  # admission never ran -> no slot leaked
    start.assert_not_awaited()
