"""Refusal contract for the portability export/import/preview API.

Every non-2xx body carries a machine-readable ``code`` (see
``test/test_error_code_contract.py``). These tests drive the real handlers
through an aiohttp ``TestServer`` rather than calling them directly, so the
multipart and auth paths are the ones a client actually hits.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import threading
from typing import Any

import pytest
from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.testing.wait import async_wait_until, await_until


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


class _FakePlan:
    """Stand-in for portability.ExportPlan: the handler reads these attributes to
    build the response head (filename, both advisory headers) before streaming."""

    def __init__(
        self,
        *,
        created_at: str = "2026-01-01T00:00:00Z",
        include_sessions: bool = False,
        memory_only: bool = False,
        include_agent_templates: bool = False,
        bundled_templates: list[str] | None = None,
        agent_templates_past_cap: int = 0,
        sessions_skipped: int = 0,
    ) -> None:
        self.prefix = "kirocrew-export-20260101T000000Z"
        self.created_at = created_at
        self.include_sessions = include_sessions
        self.memory_only = memory_only
        self.include_agent_templates = include_agent_templates
        self.bundled_templates = bundled_templates or []
        self.agent_templates_past_cap = agent_templates_past_cap
        self.session_plan = ([], 0, 0, sessions_skipped) if include_sessions else None
        self.sessions_skipped = sessions_skipped


def _patch_streaming_export(
    monkeypatch,
    module,
    *,
    plan: _FakePlan,
    manifest: dict | None = None,
    body: bytes = b"PK",
    unbundled=([], 0),
) -> list[dict]:
    """Point the handler's streaming seam at a fake plan + builder; capture plan kwargs.

    Returns the list the fake ``plan_export`` appends its call-kwargs to, so a test
    can assert what the handler asked the planner for.
    """
    seen_kwargs: list[dict] = []

    def _plan_export(**kwargs):
        seen_kwargs.append(kwargs)
        return plan

    def _stream(write, p):
        if body:
            write(body)
        return manifest if manifest is not None else {"created_at": p.created_at, "contents": {}}

    monkeypatch.setattr(module, "plan_export", _plan_export)
    monkeypatch.setattr(module, "stream_export_zip", _stream)
    monkeypatch.setattr(module, "unbundled_agent_templates", lambda *a, **k: unbundled)
    return seen_kwargs


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

    # Fail in the stat-only pre-pass, before any byte is sent: still a coded 500.
    monkeypatch.setattr(module, "plan_export", fail)
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
    seen = _patch_streaming_export(monkeypatch, module, plan=_FakePlan(include_sessions=expected))
    monkeypatch.setattr(module, "_sel", lambda: _AuditLog())

    async with TestClient(TestServer(_make_app(module))) as client:
        response = await client.get(
            f"/api/portability/export{query}", headers={"X-Test-User": "owner"}
        )

    assert response.status == 200
    assert [kw.get("include_sessions") for kw in seen] == [expected]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "plan_skipped", "contents", "header", "audit"),
    [
        ("?include_sessions=true", 3, {"session_count": 2, "sessions_skipped_size": 3}, "3", 3),
        ("?include_sessions=true", 0, {"session_count": 2}, "0", 0),
        # A hostile value in the plan cannot break the header line out.
        ("?include_sessions=true", "9\r\nX-Injected: 1", {}, "0", 0),
        ("?include_sessions=true", -1, {}, "0", 0),
        ("", 0, {}, None, None),
    ],
)
async def test_export_reports_chats_left_out_for_size(
    monkeypatch, query, plan_skipped, contents, header, audit
) -> None:
    """The count rides a bare-integer header (from the pre-pass plan) and the audit
    line (from the streamed manifest), only for a chat export."""
    module = _handler_module()
    log = _AuditLog()
    monkeypatch.setattr(module, "_sel", lambda: log)
    _patch_streaming_export(
        monkeypatch,
        module,
        plan=_FakePlan(include_sessions="include_sessions" in query, sessions_skipped=plan_skipped),
        manifest={"created_at": "t", "contents": contents},
    )

    async with TestClient(TestServer(_make_app(module))) as client:
        response = await client.get(
            f"/api/portability/export{query}", headers={"X-Test-User": "owner"}
        )
        # The "ok" audit line is written after the whole body has streamed, so the
        # body must be drained before it exists -- unlike the old synchronous handler.
        await response.read()

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
    _patch_streaming_export(monkeypatch, module, plan=_FakePlan(), unbundled=templates)

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
    plan = _FakePlan(memory_only=memory_only, include_sessions=sessions)
    seen = _patch_streaming_export(monkeypatch, module, plan=plan)
    monkeypatch.setattr(module, "_sel", lambda: _AuditLog())

    async with TestClient(TestServer(_make_app(module))) as client:
        response = await client.get(
            f"/api/portability/export{query}", headers={"X-Test-User": "owner"}
        )

    assert response.status == 200
    (kw,) = seen
    assert (kw.get("memory_only"), kw.get("include_sessions")) == (memory_only, sessions)
    assert ("memory-export" in response.headers["Content-Disposition"]) is memory_only


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "templates"),
    [
        ("", False),
        ("?include_agent_templates=true", True),
        ("?include_agent_templates=1", True),
        ("?include_agent_templates=no", False),
        ("?components=memory&include_agent_templates=true", False),
    ],
)
async def test_export_carries_agent_templates_only_when_asked(
    monkeypatch, query, templates
) -> None:
    module = _handler_module()
    memory_only = "components=memory" in query
    plan = _FakePlan(
        include_agent_templates=templates,
        memory_only=memory_only,
        bundled_templates=["carried"] if templates else [],
    )
    seen = _patch_streaming_export(monkeypatch, module, plan=plan)
    monkeypatch.setattr(module, "_sel", lambda: _AuditLog())

    def unbundled(bundled=()):
        names = [n for n in ["carried", "left"] if n not in set(bundled)]
        return names, 0

    monkeypatch.setattr(module, "unbundled_agent_templates", unbundled)

    async with TestClient(TestServer(_make_app(module))) as client:
        response = await client.get(
            f"/api/portability/export{query}", headers={"X-Test-User": "owner"}
        )

    assert response.status == 200
    assert [kw.get("include_agent_templates") for kw in seen] == [templates]
    if memory_only:
        assert module.UNBUNDLED_TEMPLATES_HEADER not in response.headers
    else:
        warned = json.loads(response.headers[module.UNBUNDLED_TEMPLATES_HEADER])
        assert warned == (["left"] if templates else ["carried", "left"])


@pytest.mark.asyncio
async def test_export_counts_templates_past_the_cap_in_the_warning(monkeypatch) -> None:
    module = _handler_module()
    plan = _FakePlan(
        include_agent_templates=True,
        bundled_templates=["carried"],
        agent_templates_past_cap=2,
    )
    _patch_streaming_export(monkeypatch, module, plan=plan, unbundled=([], 0))
    monkeypatch.setattr(module, "_sel", lambda: _AuditLog())

    async with TestClient(TestServer(_make_app(module))) as client:
        response = await client.get(
            "/api/portability/export?include_agent_templates=true",
            headers={"X-Test-User": "owner"},
        )

    assert response.status == 200
    assert json.loads(response.headers[module.UNBUNDLED_TEMPLATES_HEADER]) == ["+2"]


@pytest.mark.asyncio
@pytest.mark.parametrize("picked", ["../escape", "a/b", ".hidden", ",".join(["t"] * 201)])
async def test_import_refuses_a_bad_agent_template_pick(picked: str) -> None:
    module = _handler_module()
    async with TestClient(TestServer(_make_app(module))) as client:
        response = await client.post(
            "/api/portability/import",
            params={"agent_templates": picked},
            data=_zip_upload(),
            headers={"X-Test-User": "owner"},
        )
        body = await response.json()

    assert response.status == 400
    assert body["code"] == "invalid_agent_templates"


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
    _patch_streaming_export(monkeypatch, module, plan=_FakePlan(), unbundled=(["reviewer"], 0))

    async with TestClient(TestServer(_make_app(module))) as client:
        memory = await client.get(
            "/api/portability/export?components=memory", headers={"X-Test-User": "owner"}
        )
        whole = await client.get("/api/portability/export", headers={"X-Test-User": "owner"})

    assert module.UNBUNDLED_TEMPLATES_HEADER not in memory.headers
    assert whole.headers.get(module.UNBUNDLED_TEMPLATES_HEADER) == '["reviewer"]'


# --- The export streams, so the first byte does not wait for the whole build ---


@pytest.mark.asyncio
async def test_export_head_arrives_before_the_build_finishes(monkeypatch) -> None:
    """The response head (first byte on the wire) must reach
    the client while the archive build is still running -- otherwise a proxy with a
    first-byte timeout closes a large export before it ever starts. The build is
    blocked here; the head must still arrive.
    """
    module = _handler_module()
    monkeypatch.setattr(module, "_sel", lambda: _AuditLog())
    monkeypatch.setattr(module, "plan_export", lambda **_k: _FakePlan())
    monkeypatch.setattr(module, "unbundled_agent_templates", lambda *a, **k: ([], 0))

    build_started = threading.Event()
    release = threading.Event()

    def slow_stream(write, _plan):
        build_started.set()
        release.wait(timeout=30)
        write(b"PKdone")
        return {"created_at": "t", "contents": {}}

    monkeypatch.setattr(module, "stream_export_zip", slow_stream)

    async with TestClient(TestServer(_make_app(module))) as client:
        get_task = asyncio.ensure_future(
            client.get("/api/portability/export", headers={"X-Test-User": "owner"})
        )
        # Wait on the signal the builder emits, not a fixed poll (testing rule D1).
        await async_wait_until(
            build_started.is_set, describe=lambda: "the build to start", off_loop=True
        )

        try:
            # The build is blocked; the head must still arrive. await_until fails by
            # name on a hang under its own lost-run bound rather than a tuned number.
            resp = await await_until(get_task, what="the response head")
            assert resp.status == 200
            assert "attachment" in resp.headers["Content-Disposition"]
        finally:
            release.set()
            resp = await await_until(get_task, what="the finished response")
            body = await resp.read()
            assert body == b"PKdone"


@pytest.mark.asyncio
async def test_export_streams_every_chunk_the_builder_writes(monkeypatch) -> None:
    """The body the client receives is exactly what the builder wrote, in order."""
    module = _handler_module()
    monkeypatch.setattr(module, "_sel", lambda: _AuditLog())
    monkeypatch.setattr(module, "plan_export", lambda **_k: _FakePlan())
    monkeypatch.setattr(module, "unbundled_agent_templates", lambda *a, **k: ([], 0))

    chunks = [b"PK\x03\x04", b"a" * 1000, b"b" * 1000, b"PK\x05\x06tail"]

    def stream(write, _plan):
        for c in chunks:
            write(c)
        return {"created_at": "t", "contents": {}}

    monkeypatch.setattr(module, "stream_export_zip", stream)

    async with TestClient(TestServer(_make_app(module))) as client:
        resp = await client.get("/api/portability/export", headers={"X-Test-User": "owner"})
        body = await resp.read()

    assert resp.status == 200
    assert body == b"".join(chunks)


@pytest.mark.asyncio
async def test_export_head_carries_security_headers(monkeypatch) -> None:
    """The streamed export flushes its own head on prepare() inside the handler,
    before no_cache_middleware runs, so the handler must apply the dashboard's
    cache-control and security headers itself. Assert the head carries no-store and
    nosniff -- without the handler-side call they would be absent on this response.
    The test app installs NO security middleware, so a present header proves the
    handler set it.
    """
    module = _handler_module()
    monkeypatch.setattr(module, "_sel", lambda: _AuditLog())
    monkeypatch.setattr(module, "plan_export", lambda **_k: _FakePlan())
    monkeypatch.setattr(module, "unbundled_agent_templates", lambda *a, **k: ([], 0))

    def stream(write, _plan):
        write(b"PK\x03\x04done")
        return {"created_at": "t", "contents": {}}

    monkeypatch.setattr(module, "stream_export_zip", stream)

    async with TestClient(TestServer(_make_app(module))) as client:
        resp = await client.get("/api/portability/export", headers={"X-Test-User": "owner"})
        await resp.read()

    assert resp.status == 200
    assert resp.headers["Cache-Control"] == "no-store, no-cache, must-revalidate, max-age=0"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    # The export's own advisory headers survive the security pass (setdefault).
    assert "attachment" in resp.headers["Content-Disposition"]


@pytest.mark.asyncio
async def test_export_build_failure_after_headers_is_fail_safe(monkeypatch) -> None:
    """An error once streaming has begun cannot become an HTTP status. aiohttp's
    finish_response would call write_eof() on a returned StreamResponse and emit the
    chunked terminator -- making a half archive look complete. The handler must
    instead abort the transport so the client's body read FAILS (an incomplete-body
    error), never returns a short "complete" zip. SEL records an error, not an ok.
    """
    from aiohttp import ClientPayloadError

    module = _handler_module()
    log = _AuditLog()
    monkeypatch.setattr(module, "_sel", lambda: log)
    monkeypatch.setattr(module, "plan_export", lambda **_k: _FakePlan())
    monkeypatch.setattr(module, "unbundled_agent_templates", lambda *a, **k: ([], 0))

    def stream(write, _plan):
        write(b"PKpartial-archive-bytes" * 100)  # enough to flush out the head + body
        raise RuntimeError("disk vanished mid-build")

    monkeypatch.setattr(module, "stream_export_zip", stream)

    async with TestClient(TestServer(_make_app(module))) as client:
        resp = await client.get("/api/portability/export", headers={"X-Test-User": "owner"})
        # The head went out as 200 (it had to -- the error came after it).
        assert resp.status == 200
        # The aborted transport makes the body read raise, rather than returning a
        # clean (and misleadingly "complete") short archive.
        with pytest.raises(ClientPayloadError):
            await resp.read()

    # The failure is recorded, and no "ok" outcome was logged for this request.
    assert any(e.get("outcome") == "error" for e in log.events)
    assert not any(e.get("outcome") == "ok" for e in log.events)


def _home_with_one_chat(tmp_path):
    """A minimal crew home whose sessions/ holds one exportable persistent chat."""
    mc = tmp_path / ".kirocrew"
    (mc / "sessions").mkdir(parents=True)
    (mc / "config.json").write_text("{}")
    header = json.dumps({"_type": "metadata", "title": "chat", "memory_mode": "persistent"})
    row = json.dumps({"role": "user", "content": "hi", "ts": "2026-01-01T00:00:00"})
    (mc / "sessions" / "dashboard_kept.jsonl").write_text(header + "\n" + row + "\n")
    return mc


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["disconnect", "cancel"])
async def test_export_disconnect_releases_the_builder_and_its_lock(
    monkeypatch, tmp_path, how
) -> None:
    """When the client goes away mid-stream (hang up, or handler cancellation), the
    builder thread must not stay parked on backpressure holding a chat's history
    lock -- that would wedge every later write to that chat. After the client goes,
    the build thread exits and the lock is free: a fresh locked_stems acquires it.
    """
    from kiro_crew.history import ConversationLog

    module = _handler_module()
    monkeypatch.setattr(module, "_sel", lambda: _AuditLog())
    monkeypatch.setattr(module, "plan_export", lambda **_k: _FakePlan())
    monkeypatch.setattr(module, "unbundled_agent_templates", lambda *a, **k: ([], 0))

    mc = _home_with_one_chat(tmp_path)
    monkeypatch.setenv("KIROCREW_HOME", str(mc))
    sessions_dir = mc / "sessions"

    lock_held = threading.Event()
    build_done = threading.Event()

    def stream(write, _plan):
        # Hold the real per-chat lock, like _write_planned_sessions does, then keep
        # handing chunks over. Once the client is gone the handler sets abort and
        # frees the backpressure slots, so the next write() raises _ExportAborted
        # out of here -- which must unwind this `with`, releasing the lock.
        log = ConversationLog(base_dir=sessions_dir)
        try:
            with log.locked_stems(["dashboard_kept"]):
                lock_held.set()
                while True:
                    write(b"x" * 2048)
        finally:
            build_done.set()
        return {"created_at": "t", "contents": {}}

    monkeypatch.setattr(module, "stream_export_zip", stream)

    async with TestClient(TestServer(_make_app(module))) as client:
        resp = await client.get("/api/portability/export", headers={"X-Test-User": "owner"})
        assert resp.status == 200
        # Read one chunk so the build is underway and holding the lock.
        await resp.content.readany()
        assert lock_held.wait(timeout=5), "the build never acquired the chat lock"
        if how == "disconnect":
            resp.close()  # client hangs up
        else:
            # Cancel the handler's task the way a disconnect does under aiohttp.
            await client.close()

    # The builder thread must unwind (not stay parked on backpressure).
    assert build_done.wait(timeout=10), "the build thread did not exit after disconnect"

    # And its lock must be free: a fresh acquire succeeds promptly rather than
    # blocking on a parked holder.
    freed = threading.Event()

    def _try_lock():
        log = ConversationLog(base_dir=sessions_dir)
        with log.locked_stems(["dashboard_kept"]):
            freed.set()

    t = threading.Thread(target=_try_lock, daemon=True)
    t.start()
    t.join(timeout=10)
    assert freed.is_set(), "the chat lock was never released by the torn-down build"


@pytest.mark.asyncio
async def test_export_stalled_client_tears_down_and_frees_the_lock(monkeypatch, tmp_path) -> None:
    """A client that reads the head and then STOPS reading leaves the socket open
    with a full send buffer, so ``resp.write()`` blocks indefinitely -- and the
    builder thread stays parked inside the per-chat history lock, wedging every
    later write to that chat. The handler bounds each write with
    ``_EXPORT_STREAM_WRITE_TIMEOUT_SECS``; when it expires it must tear the builder
    down (releasing the lock), abort the transport, and record an SEL error. A fresh
    ``locked_stems`` on the chat must then acquire the lock within a bound.
    """
    from kiro_crew.history import ConversationLog

    module = _handler_module()
    log = _AuditLog()
    monkeypatch.setattr(module, "_sel", lambda: log)
    monkeypatch.setattr(module, "plan_export", lambda **_k: _FakePlan())
    monkeypatch.setattr(module, "unbundled_agent_templates", lambda *a, **k: ([], 0))
    # Trip the write deadline quickly, and keep the queue + pieces tiny so the
    # backpressure buffer and the socket buffer fill without needing a huge volume.
    monkeypatch.setattr(module, "_EXPORT_STREAM_WRITE_TIMEOUT_SECS", 0.2)
    monkeypatch.setattr(module, "_EXPORT_STREAM_QUEUE_DEPTH", 1)
    monkeypatch.setattr(module, "_EXPORT_STREAM_CHUNK_BYTES", 1024)

    mc = _home_with_one_chat(tmp_path)
    monkeypatch.setenv("KIROCREW_HOME", str(mc))
    sessions_dir = mc / "sessions"

    lock_held = threading.Event()
    build_done = threading.Event()

    def stream(write, _plan):
        # Hold the real per-chat lock, then push chunks forever. Once the stalled
        # client trips the write timeout the handler sets abort and frees the
        # backpressure slots, so the next write() raises _ExportAborted out of here,
        # unwinding this `with` and releasing the lock.
        log_ = ConversationLog(base_dir=sessions_dir)
        try:
            with log_.locked_stems(["dashboard_kept"]):
                lock_held.set()
                while True:
                    write(b"x" * 4096)
        finally:
            build_done.set()
        return {"created_at": "t", "contents": {}}

    monkeypatch.setattr(module, "stream_export_zip", stream)

    async with TestClient(TestServer(_make_app(module))) as client:
        resp = await client.get("/api/portability/export", headers={"X-Test-User": "owner"})
        # The head arrived; now stop reading entirely. The builder keeps producing,
        # the send buffer fills, and a resp.write() blocks past the deadline.
        assert resp.status == 200
        await async_wait_until(
            lock_held.is_set, describe=lambda: "the build to acquire the chat lock", off_loop=True
        )

        # The torn-down build thread must exit (not stay parked on backpressure).
        await async_wait_until(
            build_done.is_set,
            describe=lambda: "the build thread to exit after the stalled write",
            off_loop=True,
        )
        resp.close()

    # The lock must be free: a fresh acquire succeeds rather than blocking on a
    # parked holder.
    freed = threading.Event()

    def _try_lock():
        log_ = ConversationLog(base_dir=sessions_dir)
        with log_.locked_stems(["dashboard_kept"]):
            freed.set()

    threading.Thread(target=_try_lock, daemon=True).start()
    await async_wait_until(
        freed.is_set,
        describe=lambda: "the chat lock to be released after the stalled-client teardown",
        off_loop=True,
    )

    # The stall was recorded as an error, and no "ok" outcome was logged.
    assert any(
        e.get("outcome") == "error" and e.get("error") == "client stalled mid-stream"
        for e in log.events
    )
    assert not any(e.get("outcome") == "ok" for e in log.events)
