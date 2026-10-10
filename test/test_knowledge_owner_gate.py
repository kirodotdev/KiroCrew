"""Owner gate on the mutating knowledge routes.

A dashboard-user request (``request["app"] == ""``) must come from the owner
before it may change the knowledge library. The other two caller classes keep
the control that already governs them: an internal-secret loopback caller
arrives with no ``app`` key at all, and an app token carries a non-empty app id
that ``_enforce_app_scope`` confines to its manifest's paths.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import knowledge as kh
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
from kiro_crew.knowledge.store import KnowledgeStore

OWNER = "U0OWNER"
NON_OWNER = "U0NONOWNER"

# Every POST / PATCH / DELETE route setup_knowledge_routes registers.
MUTATING_ROUTES = [
    ("POST", "/api/knowledge/sources", kh.add_source),
    ("POST", "/api/knowledge/pick-folder", kh.pick_folder),
    ("POST", "/api/knowledge/sources/{id}/sync", kh.sync_source),
    ("POST", "/api/knowledge/sources/{id}/confirm", kh.confirm_source),
    ("POST", "/api/knowledge/sources/{id}/pause", kh.pause_source),
    ("POST", "/api/knowledge/sources/{id}/resume", kh.resume_source),
    ("POST", "/api/knowledge/sources/{id}/files/retry", kh.retry_file),
    ("POST", "/api/knowledge/sources/{id}/files/skip", kh.skip_file),
    ("POST", "/api/knowledge/sources/{id}/ingest-text", kh.ingest_text),
    ("DELETE", "/api/knowledge/sources/{id}", kh.delete_source),
    ("PATCH", "/api/knowledge/sources/{id}", kh.rename_source),
    ("POST", "/api/knowledge/ingest", kh.ingest_file),
    ("POST", "/api/knowledge/agent-document", kh.add_agent_document_route),
    ("POST", "/api/knowledge/import", kh.import_bundle),
    ("PATCH", "/api/knowledge/items/{id}", kh.update_item),
    ("DELETE", "/api/knowledge/items/{id}", kh.delete_item),
    ("POST", "/api/knowledge/embedding/generate", kh.batch_embed_items),
]

# POST routes that change nothing, so the owner gate does not apply. Each one
# refuses every caller its own gate does not name.
READ_ONLY_POST_ROUTES = {
    # Raw bedrock_kb results for the sandboxed MCP server's remote leg: strict
    # internal (X-Internal-Secret), so no dashboard session reaches it.
    ("POST", "/api/knowledge/remote-search"),
}


def _caller_middleware(identity: dict, seen: list):
    """Plant one caller class. ``identity`` is copied onto the request as-is,
    so an identity without an ``app`` key yields a request without one."""

    @web.middleware
    async def _mw(request, handler):
        for key, value in identity.items():
            request[key] = value
        seen.append(request)
        return await handler(request)

    return _mw


def _make_app(store, identity: dict, seen: list, pipeline=None):
    app = web.Application(middlewares=[_caller_middleware(identity, seen)])
    state = MagicMock()
    state.knowledge_store = store
    state.owner_id = OWNER
    app["state"] = state
    app["knowledge_pipeline"] = pipeline if pipeline is not None else MagicMock()
    app["knowledge_fetch_pool"] = MagicMock()
    app["knowledge_llm_pool"] = MagicMock(shutdown=AsyncMock())
    app["knowledge_sync"] = MagicMock(get_connector=MagicMock(return_value=None))
    for method, path, handler in MUTATING_ROUTES:
        app.router.add_route(method, path, handler)
    return app


def _concrete(path: str) -> str:
    return path.replace("{id}", "abc123")


@pytest.fixture()
def real_store(tmp_path):
    s = KnowledgeStore(str(tmp_path / "kb.db"))
    yield s
    s._close_all_for_tests()


def test_route_list_covers_every_mutating_registration():
    """The two tables above are the full set setup_knowledge_routes registers."""
    app = web.Application()
    app["state"] = MagicMock()
    app["knowledge_pipeline"] = MagicMock()
    kh.setup_knowledge_routes(app)
    registered = {
        (r.method, r.resource.canonical)
        for r in app.router.routes()
        if r.method in {"POST", "PUT", "PATCH", "DELETE"}
    }
    listed = {(m, p) for m, p, _ in MUTATING_ROUTES}
    assert registered == listed | READ_ONLY_POST_ROUTES


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path,handler", MUTATING_ROUTES, ids=[f"{m} {p}" for m, p, _ in MUTATING_ROUTES]
)
async def test_non_owner_dashboard_user_is_refused(method, path, handler, monkeypatch):
    store = MagicMock()
    pipeline = MagicMock()
    add_doc = AsyncMock()
    monkeypatch.setattr(kh, "add_agent_document", add_doc)
    seen: list = []
    app = _make_app(store, {"user": NON_OWNER, "app": ""}, seen, pipeline=pipeline)
    async with TestClient(TestServer(app)) as client:
        resp = await client.request(method, _concrete(path), json={})
        body = await resp.json()
    assert is_owner_dashboard_request(seen[0]) is False
    assert resp.status == 403
    assert body.get("code") == "owner_only"
    assert store.method_calls == []
    assert pipeline.method_calls == []
    add_doc.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_can_delete_source(real_store):
    sid = real_store.add_source(name="n", source_type="url", uri="https://example.com/a")
    seen: list = []
    app = _make_app(real_store, {"user": OWNER, "app": ""}, seen)
    async with TestClient(TestServer(app)) as client:
        resp = await client.delete(f"/api/knowledge/sources/{sid}")
    assert is_owner_dashboard_request(seen[0]) is True
    assert resp.status == 200
    assert real_store.db.execute("SELECT 1 FROM sources WHERE id = ?", (sid,)).fetchone() is None


@pytest.mark.asyncio
async def test_internal_secret_caller_keeps_agent_document(monkeypatch):
    """An internal-secret loopback caller carries no ``app`` key at all."""
    monkeypatch.setattr(
        kh.KiroCrewConfig,
        "load",
        classmethod(lambda cls: MagicMock(knowledge=MagicMock(auto_add_documents=True))),
    )
    add_doc = AsyncMock(return_value={"status": "added", "title": "t"})
    monkeypatch.setattr(kh, "add_agent_document", add_doc)
    seen: list = []
    app = _make_app(MagicMock(), {}, seen)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/knowledge/agent-document",
            json={"title": "t", "content": "c", "source_uri": "u"},
        )
    assert "app" not in seen[0]
    assert resp.status == 200
    add_doc.assert_awaited_once()


@pytest.mark.asyncio
async def test_internal_secret_caller_keeps_source_delete(real_store):
    sid = real_store.add_source(name="n", source_type="url", uri="https://example.com/b")
    seen: list = []
    app = _make_app(real_store, {}, seen)
    async with TestClient(TestServer(app)) as client:
        resp = await client.delete(f"/api/knowledge/sources/{sid}")
    assert "app" not in seen[0]
    assert resp.status == 200


@pytest.mark.asyncio
async def test_app_token_caller_keeps_its_scope_control(real_store):
    """An app token is governed by _enforce_app_scope, not the owner gate."""
    sid = real_store.add_source(name="n", source_type="url", uri="https://example.com/c")
    seen: list = []
    app = _make_app(real_store, {"user": NON_OWNER, "app": "md-notebook"}, seen)
    async with TestClient(TestServer(app)) as client:
        resp = await client.delete(f"/api/knowledge/sources/{sid}")
    assert seen[0]["app"] == "md-notebook"
    assert resp.status == 200
