"""Refusal contract for the portability export/import/preview API.

Every non-2xx body carries a machine-readable ``code`` (see
``test/test_error_code_contract.py``). These tests drive the real handlers
through an aiohttp ``TestServer`` rather than calling them directly, so the
multipart and auth paths are the ones a client actually hits.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest
from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer


class _AuditLog:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def log_api_access(self, **event: Any) -> None:
        self.events.append(event)


def _handler_module():
    return importlib.import_module("kiro_crew.dashboard.handlers.portability")


def _make_app(module) -> web.Application:
    @web.middleware
    async def test_auth(request: web.Request, handler):
        caller = request.headers.get("X-Test-User")
        if caller:
            request["user"] = caller
        return await handler(request)

    app = web.Application(middlewares=[test_auth])
    app.router.add_get("/api/portability/export", module.api_portability_export)
    app.router.add_post("/api/portability/import", module.api_portability_import)
    app.router.add_post("/api/portability/preview", module.api_portability_preview)
    return app


def _zip_upload(field: str = "file") -> FormData:
    data = FormData()
    data.add_field(field, b"PK\x03\x04not-a-real-zip", filename="export.zip")
    return data


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/portability/export"),
        ("post", "/api/portability/import"),
        ("post", "/api/portability/preview"),
    ],
)
async def test_every_endpoint_denies_anonymously_with_auth_required(method: str, path: str) -> None:
    module = _handler_module()
    async with TestClient(TestServer(_make_app(module))) as client:
        response = await getattr(client, method)(path)
        body = await response.json()

    assert response.status == 401
    assert body["code"] == "auth_required"
    assert body["error"] == "authentication required"


@pytest.mark.asyncio
async def test_export_failure_is_coded_and_stays_opaque(monkeypatch) -> None:
    """The 500 prose is deliberately generic; the code says which operation."""
    module = _handler_module()
    audit = _AuditLog()
    private_detail = "/Users/alice/.kiro/crew/secrets.json"

    def fail():
        raise RuntimeError(private_detail)

    monkeypatch.setattr(module, "create_export_zip", fail)
    monkeypatch.setattr(module, "_sel", lambda: audit)

    async with TestClient(TestServer(_make_app(module))) as client:
        response = await client.get("/api/portability/export", headers={"X-Test-User": "owner"})
        body = await response.json()

    assert response.status == 500
    assert body["code"] == "export_failed"
    assert body["error"] == "Export failed"
    assert private_detail not in str(body)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["", "merge_all", "REPLACE", "delete"])
async def test_an_unrecognized_import_mode_is_coded(mode: str) -> None:
    module = _handler_module()
    async with TestClient(TestServer(_make_app(module))) as client:
        response = await client.post(
            f"/api/portability/import?mode={mode}",
            data=_zip_upload(),
            headers={"X-Test-User": "owner"},
        )
        body = await response.json()

    assert response.status == 400
    assert body["code"] == "invalid_import_mode"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/portability/import", "/api/portability/preview"])
async def test_an_upload_without_the_file_part_is_coded(path: str) -> None:
    module = _handler_module()
    async with TestClient(TestServer(_make_app(module))) as client:
        response = await client.post(
            path, data=_zip_upload(field="archive"), headers={"X-Test-User": "owner"}
        )
        body = await response.json()

    assert response.status == 400
    assert body["code"] == "file_field_required"


@pytest.mark.asyncio
async def test_a_rejected_archive_keeps_the_validator_detail(monkeypatch) -> None:
    """The 400's prose is the validator's own finding, not boilerplate.

    It is the whole value of the message, so the code is added ALONGSIDE it —
    this refusal is why the frontend keeps rendering 4xx prose and only prefers
    its localized fallback on a coded 5xx.
    """
    module = _handler_module()
    monkeypatch.setattr(module, "_sel", lambda: _AuditLog())
    monkeypatch.setattr(
        module, "validate_import_zip", lambda p: (False, "manifest.json is missing", {})
    )

    async with TestClient(TestServer(_make_app(module))) as client:
        response = await client.post(
            "/api/portability/import",
            data=_zip_upload(),
            headers={"X-Test-User": "owner"},
        )
        body = await response.json()

    assert response.status == 400
    assert body["code"] == "import_archive_invalid"
    assert body["error"] == "manifest.json is missing"
    assert body["ok"] is False


@pytest.mark.asyncio
async def test_import_failure_is_coded(monkeypatch) -> None:
    module = _handler_module()
    monkeypatch.setattr(module, "_sel", lambda: _AuditLog())
    monkeypatch.setattr(module, "validate_import_zip", lambda p: (True, "", {}))

    def fail(*args: Any, **kwargs: Any):
        raise RuntimeError("boom")

    monkeypatch.setattr(module, "apply_import_zip", fail)

    async with TestClient(TestServer(_make_app(module))) as client:
        response = await client.post(
            "/api/portability/import",
            data=_zip_upload(),
            headers={"X-Test-User": "owner"},
        )
        body = await response.json()

    assert response.status == 500
    assert body["code"] == "import_failed"
    assert "boom" not in str(body)


@pytest.mark.asyncio
async def test_preview_failure_is_coded(monkeypatch) -> None:
    module = _handler_module()
    monkeypatch.setattr(module, "_sel", lambda: _AuditLog())

    def fail(*args: Any, **kwargs: Any):
        raise RuntimeError("boom")

    monkeypatch.setattr(module, "validate_import_zip", fail)

    async with TestClient(TestServer(_make_app(module))) as client:
        response = await client.post(
            "/api/portability/preview",
            data=_zip_upload(),
            headers={"X-Test-User": "owner"},
        )
        body = await response.json()

    assert response.status == 500
    assert body["code"] == "preview_failed"
    assert "boom" not in str(body)


@pytest.mark.asyncio
async def test_every_refusal_carries_a_code(monkeypatch) -> None:
    """Per-file ratchet: no refusal path may regress to prose-only."""
    module = _handler_module()
    monkeypatch.setattr(module, "_sel", lambda: _AuditLog())

    async with TestClient(TestServer(_make_app(module))) as client:
        collected = [
            await client.get("/api/portability/export"),
            await client.post(
                "/api/portability/import?mode=nope",
                data=_zip_upload(),
                headers={"X-Test-User": "owner"},
            ),
            await client.post(
                "/api/portability/preview",
                data=_zip_upload(field="archive"),
                headers={"X-Test-User": "owner"},
            ),
        ]
        for response in collected:
            body = await response.json()
            assert response.status >= 400, body
            assert isinstance(body.get("code"), str) and body["code"], body
            assert isinstance(body.get("error"), str) and body["error"], body
