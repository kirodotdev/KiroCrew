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


class _OwnerState:
    """The one gateway-state field the shared owner gate reads."""

    owner_id = "owner"


def _make_app(module) -> web.Application:
    @web.middleware
    async def test_auth(request: web.Request, handler):
        caller = request.headers.get("X-Test-User")
        if caller:
            request["user"] = caller
            # An empty app id is how the real middleware marks a dashboard
            # subject; the owner gate refuses any other value.
            request["app"] = ""
        return await handler(request)

    app = web.Application(middlewares=[test_auth])
    app["state"] = _OwnerState()
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

    def fail(**_kwargs):
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
@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("", False),
        ("?include_sessions=true", True),
        ("?include_sessions=1", True),
        ("?include_sessions=no", False),
    ],
)
async def test_export_includes_chats_only_when_the_request_asks(
    monkeypatch, query, expected
) -> None:
    module = _handler_module()
    seen: list[bool] = []

    def export(*, include_sessions: bool = False, memory_only: bool = False):
        seen.append(include_sessions)
        return b"PK", {"created_at": "t", "contents": {"session_count": 2}}

    monkeypatch.setattr(module, "_sel", lambda: _AuditLog())
    monkeypatch.setattr(module, "create_export_zip", export)
    monkeypatch.setattr(module, "unbundled_agent_templates", lambda: ([], 0))

    async with TestClient(TestServer(_make_app(module))) as client:
        response = await client.get(
            f"/api/portability/export{query}", headers={"X-Test-User": "owner"}
        )

    assert response.status == 200
    assert seen == [expected]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "contents", "header", "audit"),
    [
        ("?include_sessions=true", {"session_count": 2, "sessions_skipped_size": 3}, "3", 3),
        ("?include_sessions=true", {"session_count": 2}, "0", 0),
        ("?include_sessions=true", {"sessions_skipped_size": "9\r\nX-Injected: 1"}, "0", 0),
        ("?include_sessions=true", {"sessions_skipped_size": -1}, "0", 0),
        ("", {}, None, None),
    ],
)
async def test_export_reports_chats_left_out_for_size(
    monkeypatch, query, contents, header, audit
) -> None:
    """The count rides a bare-integer header and the audit line, only for a chat export."""
    module = _handler_module()
    log = _AuditLog()
    monkeypatch.setattr(module, "_sel", lambda: log)
    monkeypatch.setattr(
        module,
        "create_export_zip",
        lambda **_kwargs: (b"PK", {"created_at": "t", "contents": contents}),
    )
    monkeypatch.setattr(module, "unbundled_agent_templates", lambda: ([], 0))

    async with TestClient(TestServer(_make_app(module))) as client:
        response = await client.get(
            f"/api/portability/export{query}", headers={"X-Test-User": "owner"}
        )

    assert response.status == 200
    assert response.headers.get(module.SESSIONS_SKIPPED_SIZE_HEADER) == header
    assert "X-Injected" not in response.headers
    (event,) = [e for e in log.events if e.get("outcome") == "ok"]
    if audit is None:
        assert "sessions_skipped_size" not in event["resources"]
    else:
        assert f",sessions_skipped_size={audit}" in event["resources"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("templates", "header"),
    [
        ((["a", "b\nX-Injected: 1"], 0), '["a", "b\\nX-Injected: 1"]'),
        ((["a"], 3), '["a", "+3"]'),
        (([], 0), None),
    ],
)
async def test_export_names_the_unbundled_templates_in_a_header(
    monkeypatch, templates, header
) -> None:
    """The body is the archive, so the warning rides a header -- JSON-escaped."""
    module = _handler_module()
    monkeypatch.setattr(module, "_sel", lambda: _AuditLog())
    monkeypatch.setattr(module, "create_export_zip", lambda **_kwargs: (b"PK", {"created_at": "t"}))
    monkeypatch.setattr(module, "unbundled_agent_templates", lambda: templates)

    async with TestClient(TestServer(_make_app(module))) as client:
        response = await client.get("/api/portability/export", headers={"X-Test-User": "owner"})

    assert response.status == 200
    assert response.headers.get(module.UNBUNDLED_TEMPLATES_HEADER) == header
    assert "X-Injected" not in response.headers


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "memory_only", "sessions"),
    [
        ("?components=memory", True, False),
        ("?components=memory&include_sessions=true", True, False),
        ("?include_sessions=true", False, True),
        ("", False, False),
    ],
)
async def test_export_memory_only_when_asked(monkeypatch, query, memory_only, sessions) -> None:
    module = _handler_module()
    seen: list[tuple[bool, bool]] = []

    def export(*, include_sessions: bool = False, memory_only: bool = False):
        seen.append((memory_only, include_sessions))
        return b"PK", {"created_at": "t", "contents": {}}

    monkeypatch.setattr(module, "_sel", lambda: _AuditLog())
    monkeypatch.setattr(module, "create_export_zip", export)
    monkeypatch.setattr(module, "unbundled_agent_templates", lambda: ([], 0))

    async with TestClient(TestServer(_make_app(module))) as client:
        response = await client.get(
            f"/api/portability/export{query}", headers={"X-Test-User": "owner"}
        )

    assert response.status == 200
    assert seen == [(memory_only, sessions)]
    assert ("memory-export" in response.headers["Content-Disposition"]) is memory_only


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/portability/export", "/api/portability/import"])
async def test_an_unknown_component_is_coded(path: str) -> None:
    module = _handler_module()
    async with TestClient(TestServer(_make_app(module))) as client:
        method = client.get if path.endswith("export") else client.post
        kwargs = {} if path.endswith("export") else {"data": _zip_upload()}
        response = await method(
            f"{path}?components=config", headers={"X-Test-User": "owner"}, **kwargs
        )
        body = await response.json()

    assert response.status == 400
    assert body["code"] == "invalid_components"


@pytest.mark.asyncio
async def test_memory_only_import_is_merge_only() -> None:
    module = _handler_module()
    async with TestClient(TestServer(_make_app(module))) as client:
        response = await client.post(
            "/api/portability/import?mode=replace&components=memory",
            data=_zip_upload(),
            headers={"X-Test-User": "owner"},
        )
        body = await response.json()

    assert response.status == 400
    assert body["code"] == "memory_only_merge_only"


@pytest.mark.asyncio
async def test_a_memory_bundle_refuses_replace(monkeypatch) -> None:
    """Replace from a memory bundle would reset everything it does not carry."""
    module = _handler_module()
    applied: list[Any] = []
    monkeypatch.setattr(module, "_sel", lambda: _AuditLog())
    monkeypatch.setattr(
        module,
        "validate_import_zip",
        lambda p: (True, "", {"version": 2, "components": ["memory"]}),
    )
    monkeypatch.setattr(module, "apply_import_zip", lambda *a, **k: applied.append(a))

    async with TestClient(TestServer(_make_app(module))) as client:
        response = await client.post(
            "/api/portability/import?mode=replace",
            data=_zip_upload(),
            headers={"X-Test-User": "owner"},
        )
        body = await response.json()

    assert response.status == 400
    assert body["code"] == "memory_only_merge_only"
    assert applied == []


@pytest.mark.asyncio
async def test_memory_only_import_passes_the_flag(monkeypatch) -> None:
    module = _handler_module()
    seen: list[bool] = []
    monkeypatch.setattr(module, "_sel", lambda: _AuditLog())
    monkeypatch.setattr(module, "validate_import_zip", lambda p: (True, "", {"version": 2}))

    def apply(zip_path, mode, *, channel_settings=None, memory_only=False):
        seen.append(memory_only)
        return {"items": [], "components": ["memory"]}

    monkeypatch.setattr(module, "apply_import_zip", apply)

    async with TestClient(TestServer(_make_app(module))) as client:
        response = await client.post(
            "/api/portability/import?mode=merge&components=memory",
            data=_zip_upload(),
            headers={"X-Test-User": "owner"},
        )

    assert response.status == 200
    assert seen == [True]


@pytest.mark.asyncio
async def test_a_validated_memory_manifest_scopes_the_import(monkeypatch) -> None:
    """Without ?components the validated declaration alone turns the filter on."""
    module = _handler_module()
    seen: list[bool] = []
    monkeypatch.setattr(module, "_sel", lambda: _AuditLog())
    monkeypatch.setattr(
        module,
        "validate_import_zip",
        lambda p: (True, "", {"version": 2, "components": ["memory"]}),
    )

    def apply(zip_path, mode, *, channel_settings=None, memory_only=False):
        seen.append(memory_only)
        return {"items": []}

    monkeypatch.setattr(module, "apply_import_zip", apply)

    async with TestClient(TestServer(_make_app(module))) as client:
        response = await client.post(
            "/api/portability/import?mode=merge",
            data=_zip_upload(),
            headers={"X-Test-User": "owner"},
        )

    assert response.status == 200
    assert seen == [True]


@pytest.mark.asyncio
async def test_a_memory_export_names_no_unbundled_templates(monkeypatch) -> None:
    module = _handler_module()
    monkeypatch.setattr(module, "_sel", lambda: _AuditLog())
    monkeypatch.setattr(module, "create_export_zip", lambda **_k: (b"PK", {"created_at": "t"}))
    monkeypatch.setattr(module, "unbundled_agent_templates", lambda: (["reviewer"], 0))

    async with TestClient(TestServer(_make_app(module))) as client:
        memory = await client.get(
            "/api/portability/export?components=memory", headers={"X-Test-User": "owner"}
        )
        whole = await client.get("/api/portability/export", headers={"X-Test-User": "owner"})

    assert module.UNBUNDLED_TEMPLATES_HEADER not in memory.headers
    assert whole.headers.get(module.UNBUNDLED_TEMPLATES_HEADER) == '["reviewer"]'
