"""HTTP management surface for reusable workflow definitions."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import kiro_crew.dashboard.handlers.workflows as workflow_handlers
from kiro_crew.dashboard.handlers.workflows import (
    api_workflow_definition_get,
    api_workflow_definition_run,
    api_workflow_definition_update,
    api_workflow_definitions,
    api_workflow_definitions_create,
    api_workflow_run_promote,
)

pytestmark = pytest.mark.asyncio


class FakeService:
    def __init__(self) -> None:
        self.saved = None
        self.updated = None
        self.started = None
        self.promoted = None

    def list_definitions(self, search=""):
        return [{"id": "wfd_1", "slug": "debug", "search": search}]

    def get_definition(self, workflow_ref):
        if workflow_ref == "missing":
            return None
        return {"id": "wfd_1", "slug": workflow_ref, "source": "source"}

    def save_definition(self, source, **kwargs):
        self.saved = (source, kwargs)
        return {"ok": True, "definition": {"id": "wfd_1", "slug": "debug"}}

    def update_definition(self, workflow_id, **kwargs):
        self.updated = (workflow_id, kwargs)
        if kwargs["expected_revision"] == 1:
            return {"ok": False, "error": "stale", "conflict": True}
        return {"ok": True, "definition": {"id": workflow_id, "revision": 3}}

    async def start_definition(self, workflow_ref, **kwargs):
        self.started = (workflow_ref, kwargs)
        return {"run_id": "wf_1", "workflow_id": "wfd_1", "revision": 2}

    async def promote_run_definition(self, run_id, **kwargs):
        self.promoted = (run_id, kwargs)
        return {"ok": True, "definition": {"id": "wfd_2", "slug": "promoted"}}


def _app(service: FakeService, *, app_name: str | None = "") -> web.Application:
    @web.middleware
    async def authenticated_identity(request, handler):
        if app_name is not None:
            request["user"] = "test-user"
            request["app"] = app_name
            request["is_dashboard_user"] = app_name == ""
        return await handler(request)

    app = web.Application(middlewares=[authenticated_identity])
    app["state"] = SimpleNamespace(workflow_service=service)
    app.router.add_get("/api/workflows/definitions", api_workflow_definitions)
    app.router.add_post("/api/workflows/definitions", api_workflow_definitions_create)
    app.router.add_post(
        "/api/workflows/definitions/{workflow_ref}/run", api_workflow_definition_run
    )
    app.router.add_get("/api/workflows/definitions/{workflow_ref}", api_workflow_definition_get)
    app.router.add_patch(
        "/api/workflows/definitions/{workflow_ref}", api_workflow_definition_update
    )
    app.router.add_post("/api/workflows/runs/{run_id}/promote", api_workflow_run_promote)
    return app


async def test_definition_list_and_create_preserve_lineage() -> None:
    service = FakeService()
    async with TestClient(TestServer(_app(service))) as client:
        listed = await (await client.get("/api/workflows/definitions?q=debugging")).json()
        response = await client.post(
            "/api/workflows/definitions",
            json={
                "source": "source",
                "format": "task-plan",
                "name": "Debug",
                "derived_from": {"workflow_id": "wfd_parent", "revision": 4},
            },
        )
        created = await response.json()

    assert listed["definitions"][0]["search"] == "debugging"
    assert created["definition"]["slug"] == "debug"
    assert service.saved[1]["source_format"] == "task-plan"
    assert service.saved[1]["derived_from"] == {"workflow_id": "wfd_parent", "revision": 4}


async def test_definition_get_missing_has_machine_readable_code() -> None:
    async with TestClient(TestServer(_app(FakeService()))) as client:
        response = await client.get("/api/workflows/definitions/missing")
        body = await response.json()

    assert response.status == 404
    assert body["code"] == "workflow_definition_not_found"


async def test_definition_run_reports_executor_rejection_instead_of_not_found() -> None:
    service = FakeService()

    async def reject_start(_workflow_ref, **_kwargs):
        return {
            "error": "Too many concurrent tasks (3/3).",
            "admission_rejected": True,
        }

    service.start_definition = reject_start  # type: ignore[method-assign]
    async with TestClient(TestServer(_app(service))) as client:
        response = await client.post("/api/workflows/definitions/debug/run", json={})
        body = await response.json()

    assert response.status == 409
    assert body == {
        "error": "Too many concurrent tasks (3/3).",
        "code": "workflow_definition_start_rejected",
    }


async def test_definition_update_returns_conflict_and_run_maps_input() -> None:
    service = FakeService()
    async with TestClient(TestServer(_app(service))) as client:
        conflict = await client.patch(
            "/api/workflows/definitions/wfd_1",
            json={"source": "changed", "expected_revision": 1},
        )
        conflict_body = await conflict.json()
        started = await client.post(
            "/api/workflows/definitions/debug/run",
            json={"input": "failing login", "args": {"level": "deep"}},
            headers={"X-Session-Key": "slot:main"},
        )

    assert conflict.status == 409
    assert conflict_body["code"] == "workflow_definition_conflict"
    assert started.status == 200
    assert service.started == (
        "debug",
        {
            "input_text": "failing login",
            "args": {"level": "deep"},
            "author": "slot:main",
            "session_key": "slot:main",
            "budget_total": None,
            "timeout_secs": None,
        },
    )


@pytest.mark.parametrize("app_name", ["untrusted-app", None])
async def test_definition_mutations_require_dashboard_user_identity(
    app_name: str | None, monkeypatch
) -> None:
    service = FakeService()
    audit_calls: list[dict[str, object]] = []
    audit = SimpleNamespace(log_api_access=lambda **kwargs: audit_calls.append(kwargs))
    monkeypatch.setattr(workflow_handlers, "_sel", lambda: audit)

    async with TestClient(TestServer(_app(service, app_name=app_name))) as client:
        created = await client.post(
            "/api/workflows/definitions",
            json={"source": "source"},
        )
        updated = await client.patch(
            "/api/workflows/definitions/wfd_1",
            json={"source": "changed", "expected_revision": 2},
        )
        promoted = await client.post(
            "/api/workflows/runs/wf_1/promote",
            json={"name": "Promoted"},
        )

    assert created.status == 403
    assert updated.status == 403
    assert promoted.status == 403
    assert service.saved is None
    assert service.updated is None
    assert service.promoted is None
    assert [call["operation"] for call in audit_calls] == [
        "workflow_definition_create",
        "workflow_definition_update",
        "workflow_definition_promote",
    ]


async def test_successful_definition_authorization_is_audited(monkeypatch) -> None:
    service = FakeService()
    audit_calls: list[dict[str, object]] = []
    audit = SimpleNamespace(log_api_access=lambda **kwargs: audit_calls.append(kwargs))
    monkeypatch.setattr(workflow_handlers, "_sel", lambda: audit)

    async with TestClient(TestServer(_app(service))) as client:
        created = await client.post(
            "/api/workflows/definitions",
            json={"source": "source"},
        )
        updated = await client.patch(
            "/api/workflows/definitions/wfd_1",
            json={"source": "changed", "expected_revision": 2},
        )
        started = await client.post(
            "/api/workflows/definitions/debug/run",
            json={"input": "failing login"},
            headers={"X-Session-Key": "slot:main"},
        )
        promoted = await client.post(
            "/api/workflows/runs/wf_1/promote",
            json={
                "source": "display-safe source must be ignored",
                "name": "Promoted",
                "description": "From the completed run",
                "slug": "promoted",
            },
        )

    assert created.status == 201
    assert updated.status == 200
    assert started.status == 200
    assert promoted.status == 201
    assert service.promoted == (
        "wf_1",
        {
            "name": "Promoted",
            "description": "From the completed run",
            "slug": "promoted",
        },
    )
    assert [(call["operation"], call["outcome"]) for call in audit_calls] == [
        ("workflow_definition_create", "allowed"),
        ("workflow_definition_update", "allowed"),
        ("workflow_definition_run", "allowed"),
        ("workflow_definition_promote", "allowed"),
    ]


async def test_app_token_cannot_spoof_saved_run_session_identity(monkeypatch) -> None:
    service = FakeService()
    audit_calls: list[dict[str, object]] = []
    audit = SimpleNamespace(log_api_access=lambda **kwargs: audit_calls.append(kwargs))
    monkeypatch.setattr(workflow_handlers, "_sel", lambda: audit)

    async with TestClient(TestServer(_app(service, app_name="untrusted-app"))) as client:
        response = await client.post(
            "/api/workflows/definitions/debug/run",
            json={"input": "failing login"},
            headers={"X-Session-Key": "slot:victim"},
        )
        body = await response.json()

    assert response.status == 403
    assert body["code"] == "dashboard_user_required"
    assert service.started is None
    assert audit_calls[0]["operation"] == "workflow_definition_run"


async def test_definition_update_returns_not_found_separately_from_conflict() -> None:
    service = FakeService()
    service.update_definition = lambda workflow_id, **kwargs: {
        "ok": False,
        "error": "no such saved workflow",
        "not_found": True,
    }
    async with TestClient(TestServer(_app(service))) as client:
        response = await client.patch(
            "/api/workflows/definitions/wfd_missing",
            json={"source": "changed", "expected_revision": 1},
        )
        body = await response.json()

    assert response.status == 404
    assert body["code"] == "workflow_definition_not_found"


async def test_run_promotion_distinguishes_missing_from_unfinished() -> None:
    service = FakeService()
    outcomes = iter(
        [
            {"ok": False, "error": "missing", "not_found": True},
            {"ok": False, "error": "running", "not_finished": True},
            {"ok": False, "error": "restored", "source_not_original": True},
        ]
    )

    async def promote(*args, **kwargs):
        return next(outcomes)

    service.promote_run_definition = promote
    async with TestClient(TestServer(_app(service))) as client:
        missing = await client.post("/api/workflows/runs/wf_missing/promote", json={})
        missing_body = await missing.json()
        unfinished = await client.post("/api/workflows/runs/wf_running/promote", json={})
        unfinished_body = await unfinished.json()
        restored = await client.post("/api/workflows/runs/wf_restored/promote", json={})
        restored_body = await restored.json()

    assert missing.status == 404
    assert missing_body["code"] == "workflow_run_not_found"
    assert unfinished.status == 409
    assert unfinished_body["code"] == "workflow_run_not_finished"
    assert restored.status == 409
    assert restored_body["code"] == "workflow_run_source_not_original"


async def test_definition_create_rejects_non_object_json() -> None:
    async with TestClient(TestServer(_app(FakeService()))) as client:
        response = await client.post("/api/workflows/definitions", json=["source"])
        body = await response.json()

    assert response.status == 400
    assert body["code"] == "body_not_object"


async def test_definition_create_write_failure_has_machine_readable_code() -> None:
    service = FakeService()

    def fail_save(*args, **kwargs):
        raise OSError("full")

    service.save_definition = fail_save
    async with TestClient(TestServer(_app(service))) as client:
        response = await client.post("/api/workflows/definitions", json={"source": "source"})
        body = await response.json()

    assert response.status == 500
    assert body["code"] == "workflow_definition_write_failed"


async def test_definition_disk_operations_are_offloaded_from_the_gateway_loop(
    monkeypatch,
) -> None:
    service = FakeService()
    calls: list[str] = []

    async def tracked_to_thread(fn, /, *args, **kwargs):
        calls.append(fn.__name__)
        return fn(*args, **kwargs)

    monkeypatch.setattr(workflow_handlers.asyncio, "to_thread", tracked_to_thread)
    async with TestClient(TestServer(_app(service))) as client:
        assert (await client.get("/api/workflows/definitions")).status == 200
        assert (
            await client.post("/api/workflows/definitions", json={"source": "source"})
        ).status == 201
        assert (await client.get("/api/workflows/definitions/debug")).status == 200
        assert (
            await client.patch(
                "/api/workflows/definitions/wfd_1",
                json={"source": "changed", "expected_revision": 2},
            )
        ).status == 200

    assert calls == [
        "list_definitions",
        "save_definition",
        "get_definition",
        "update_definition",
    ]
