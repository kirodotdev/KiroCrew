"""Coverage tests for the module-wide parts of the Knowledge Library handlers.

These are the parts no single endpoint owns: the SEL logging helper, embedder
construction from ``config.json``, the source watcher and artifact-ingest startup
hooks, ``setup_knowledge_routes`` (what it builds and registers, its second-call path
and its cleanup), and the JSON object body guard. Nine JSON-body routes answer 400
with a machine-readable code on a body that is valid JSON but not an object and on
invalid JSON; an absent body is a 400 at add-source and keeps the defaults at
embedding generate. Shared harness pieces come from
``knowledge_handlers_coverage_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from knowledge_handlers_coverage_helpers import (  # noqa: F401 -- the fixture is requested by name
    MODULE,
    _cfg,
    _client,
    _FakeEmbedder,
    _make_app,
    store_fixture,
)

from kiro_crew.dashboard.handlers import knowledge as kh
from kiro_crew.testing.wait import await_until

# --------------------------------------------------------- startup / wiring


class TestSelLog:
    def test_emits_a_namespaced_tool_event(self, monkeypatch):
        recorded = {}
        monkeypatch.setattr(
            f"{MODULE}.sel", lambda: MagicMock(log_tool_invocation=lambda **kw: recorded.update(kw))
        )
        kh._sel_log("item.update", item_id="i1")
        assert recorded["tool_name"] == "knowledge.item.update"
        assert recorded["outcome"] == "completed"
        assert "i1" in recorded["resources"]

    def test_outcome_can_be_overridden_and_leaves_resources_empty(self, monkeypatch):
        recorded = {}
        monkeypatch.setattr(
            f"{MODULE}.sel", lambda: MagicMock(log_tool_invocation=lambda **kw: recorded.update(kw))
        )
        kh._sel_log("batch_embed", outcome="cancelled")
        assert recorded["outcome"] == "cancelled"
        assert recorded["resources"] == ""


class TestCreateEmbedder:
    def test_reads_config_json_when_present(self, monkeypatch, tmp_path):
        (tmp_path / "config.json").write_text(
            json.dumps({"knowledge": {"embed_content_budget": 77}})
        )
        monkeypatch.setattr(f"{MODULE}.config_dir", lambda: tmp_path)
        emb = kh._create_embedder(web.Application())
        assert emb.content_budget == 77

    def test_missing_config_falls_back_to_defaults(self, monkeypatch, tmp_path):
        monkeypatch.setattr(f"{MODULE}.config_dir", lambda: tmp_path)
        assert kh._create_embedder(web.Application()) is not None

    def test_unreadable_config_falls_back_to_defaults(self, monkeypatch, tmp_path):
        (tmp_path / "config.json").write_text("{ broken")
        monkeypatch.setattr(f"{MODULE}.config_dir", lambda: tmp_path)
        assert kh._create_embedder(web.Application()) is not None


class TestStartWatcherAsync:
    @pytest.mark.asyncio
    async def test_stops_previous_watcher_and_registers_the_new_one(self, store, monkeypatch):
        old = MagicMock(stop=AsyncMock())
        started = asyncio.Event()

        class _FakeWatcher:
            def __init__(self, *, store, pipeline):
                self.store = store

            async def start(self):
                started.set()

            async def stop(self):
                return None

        monkeypatch.setattr(f"{MODULE}.KnowledgeWatcher", _FakeWatcher)

        app = _make_app(store, pipeline=MagicMock(), watcher=old)
        await kh._start_watcher_async(app)
        try:
            await await_until(started, what="the replacement watcher starting")
            old.stop.assert_awaited_once()
            assert isinstance(app["knowledge_watcher"], _FakeWatcher)
            # The watcher is constructed with the store and pipeline only: it no
            # longer receives a project-dirs callback, because nothing registers a
            # project directory on its own.
            assert app["knowledge_watcher"].store is store
        finally:
            task = app["_knowledge_watcher_task"]
            task.cancel()
            # flake-ok: the task was cancelled just above, so this gather returns at once
            await asyncio.gather(task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_works_with_no_previous_watcher(self, store, monkeypatch):
        class _FakeWatcher:
            def __init__(self, **_kw):
                pass

            async def start(self):
                return None

        monkeypatch.setattr(f"{MODULE}.KnowledgeWatcher", _FakeWatcher)
        app = _make_app(store, pipeline=MagicMock())
        await kh._start_watcher_async(app)
        task = app["_knowledge_watcher_task"]
        # flake-ok: the stub start() returns at once, so the watcher task ends on its own
        await asyncio.gather(task, return_exceptions=True)
        assert app["knowledge_watcher"] is not None


class TestStartArtifactIngestAsync:
    @pytest.mark.asyncio
    async def test_disabled_toggle_is_a_no_op(self, store, monkeypatch):
        monkeypatch.setattr(
            f"{MODULE}.KiroCrewConfig.load", staticmethod(lambda: _cfg(auto_ingest=False))
        )
        app = _make_app(store, pipeline=MagicMock())
        await kh._start_artifact_ingest_async(app)
        assert "artifact_knowledge_sync" not in app

    @pytest.mark.asyncio
    async def test_enabled_registers_change_listener_and_starts(self, store, monkeypatch):
        monkeypatch.setattr(
            f"{MODULE}.KiroCrewConfig.load",
            staticmethod(lambda: _cfg(auto_ingest=True, kinds=("webapp",))),
        )
        art_store = MagicMock()
        monkeypatch.setattr(f"{MODULE}.get_default_store", lambda: art_store)
        made = {}

        class _FakeSync:
            def __init__(self, *, art_store, pipeline, kinds, loop):
                made["kinds"] = kinds
                self.on_change = object()

            async def start(self):
                made["started"] = True

        monkeypatch.setattr(f"{MODULE}.ArtifactKnowledgeSync", _FakeSync)
        app = _make_app(store, pipeline=MagicMock())
        await kh._start_artifact_ingest_async(app)
        assert made == {"kinds": {"webapp"}, "started": True}
        art_store.set_change_listener.assert_called_once()
        # The binding is held on the app so the listener is not collected.
        assert isinstance(app["artifact_knowledge_sync"], _FakeSync)


class TestSetupKnowledgeRoutes:
    @pytest.mark.asyncio
    async def test_builds_pipeline_connectors_and_routes(self, store, monkeypatch, tmp_path):
        monkeypatch.setattr(f"{MODULE}.config_dir", lambda: tmp_path)
        app = web.Application()
        state = MagicMock()
        state.knowledge_store = store
        app["state"] = state

        kh.setup_knowledge_routes(app)
        try:
            assert app["knowledge_pipeline"] is not None
            assert app["knowledge_embedder"] is not None
            sync = app["knowledge_sync"]
            # Built-in connectors are always present; the edition seam adds to
            # them and the Default contributes nothing.
            assert sync.get_connector("local_folder") is not None
            assert sync.get_connector("obsidian_vault") is not None
            assert sync.get_connector("nope") is None
            # Both startup hooks are registered (watcher + artifact ingest).
            # aiohttp seeds on_startup with its own cleanup-ctx hook, so compare
            # membership rather than a raw length.
            assert kh._start_watcher_async in app.on_startup
            assert kh._start_artifact_ingest_async in app.on_startup

            paths = {r.resource.canonical for r in app.router.routes() if r.resource is not None}
            for expected in (
                "/api/knowledge/items",
                "/api/knowledge/stats",
                "/api/knowledge/search-for-context",
                "/api/knowledge/embedding/generate",
                "/api/knowledge/agent-document",
            ):
                assert expected in paths
        finally:
            for callback in list(app.on_cleanup):
                await callback(app)

    @pytest.mark.asyncio
    async def test_second_call_keeps_the_existing_pipeline(self, store, monkeypatch, tmp_path):
        monkeypatch.setattr(f"{MODULE}.config_dir", lambda: tmp_path)
        app = _make_app(store, pipeline=MagicMock())
        sentinel = app["knowledge_pipeline"]
        kh.setup_knowledge_routes(app)
        assert app["knowledge_pipeline"] is sentinel
        # No LLM pool / embedder were constructed on the re-entry path.
        assert "knowledge_embedder" not in app
        assert kh._start_watcher_async not in app.on_startup

    @pytest.mark.asyncio
    async def test_cleanup_stops_watcher_and_cancels_its_task(self, store):
        app = _make_app(store, pipeline=MagicMock())
        kh.setup_knowledge_routes(app)
        watcher = MagicMock(stop=AsyncMock())
        app["knowledge_watcher"] = watcher
        # flake-ok: the 30 s sleep stands in for a live watcher task the cleanup must cancel
        forever = asyncio.ensure_future(asyncio.sleep(30))
        app["_knowledge_watcher_task"] = forever
        for callback in list(app.on_cleanup):
            await callback(app)
        watcher.stop.assert_awaited_once()
        # flake-ok: the cleanup cancelled forever above; its 30 s sleep bounds even a miss
        await asyncio.gather(forever, return_exceptions=True)
        assert forever.cancelled()


# ------------------------------------------------- JSON object body guard


def _guard_app(store, **kwargs):
    """``_make_app`` plus the mutation routes the body-shape guard tests hit."""
    app = _make_app(store, **kwargs)
    r = app.router
    r.add_post("/api/knowledge/sources", kh.add_source)
    r.add_patch("/api/knowledge/sources/{id}", kh.rename_source)
    r.add_post("/api/knowledge/sources/{id}/files/retry", kh.retry_file)
    r.add_post("/api/knowledge/sources/{id}/files/skip", kh.skip_file)
    return app


# Valid JSON that is not an object: calling ``.get`` on either raised
# AttributeError and surfaced as a 500 before the shared guard.
_NON_OBJECT_BODIES = ([1, 2], 7)


class TestJsonObjectBodyGuard:
    """Every ``request.json()`` site answers 400, not 500, on a body that is
    valid JSON but not an object -- and two further sites
    (files/retry, files/skip) now answer 400 on invalid JSON too."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", _NON_OBJECT_BODIES)
    async def test_update_item_non_object_body_is_400(self, store, payload):
        item_id = store.add_item("a", "body", "note")
        async with _client(_guard_app(store)) as client:
            resp = await client.patch(f"/api/knowledge/items/{item_id}", json=payload)
            assert resp.status == 400
            assert (await resp.json())["code"] == "body_not_object"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", _NON_OBJECT_BODIES)
    async def test_add_source_non_object_body_is_400(self, store, payload):
        async with _client(_guard_app(store)) as client:
            resp = await client.post("/api/knowledge/sources", json=payload)
            assert resp.status == 400
            assert (await resp.json())["code"] == "body_not_object"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", _NON_OBJECT_BODIES)
    async def test_rename_source_non_object_body_is_400(self, store, payload):
        sid = store.add_source("s", "web", "https://example.com")
        async with _client(_guard_app(store)) as client:
            resp = await client.patch(f"/api/knowledge/sources/{sid}", json=payload)
            assert resp.status == 400
            assert (await resp.json())["code"] == "body_not_object"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("endpoint", ["retry", "skip"])
    @pytest.mark.parametrize("payload", _NON_OBJECT_BODIES)
    async def test_file_state_non_object_body_is_400(self, store, endpoint, payload):
        sid = store.add_source("s", "local_folder", "/tmp/x")
        async with _client(_guard_app(store)) as client:
            resp = await client.post(f"/api/knowledge/sources/{sid}/files/{endpoint}", json=payload)
            assert resp.status == 400
            assert (await resp.json())["code"] == "body_not_object"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("endpoint", ["retry", "skip"])
    async def test_file_state_invalid_json_is_400_not_500(self, store, endpoint):
        # Without a try/except these two sites would let a malformed
        # body escape as a raw JSONDecodeError and surface as a 500.
        sid = store.add_source("s", "local_folder", "/tmp/x")
        async with _client(_guard_app(store)) as client:
            resp = await client.post(
                f"/api/knowledge/sources/{sid}/files/{endpoint}",
                data="not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_json"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", _NON_OBJECT_BODIES)
    async def test_ingest_text_non_object_body_is_400(self, store, payload):
        sid = store.add_source("s", "web", "https://example.com")
        async with _client(_guard_app(store, pipeline=MagicMock())) as client:
            resp = await client.post(f"/api/knowledge/sources/{sid}/ingest-text", json=payload)
            assert resp.status == 400
            assert (await resp.json())["code"] == "body_not_object"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", _NON_OBJECT_BODIES)
    async def test_import_bundle_non_object_body_is_400(self, store, payload):
        async with _client(_guard_app(store)) as client:
            resp = await client.post("/api/knowledge/import", json=payload)
            assert resp.status == 400
            assert (await resp.json())["code"] == "body_not_object"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", _NON_OBJECT_BODIES)
    async def test_agent_document_non_object_body_is_400(self, store, monkeypatch, payload):
        monkeypatch.setattr(f"{MODULE}.KiroCrewConfig.load", staticmethod(_cfg))
        async with _client(_guard_app(store, pipeline=MagicMock())) as client:
            resp = await client.post("/api/knowledge/agent-document", json=payload)
            assert resp.status == 400
            assert (await resp.json())["code"] == "body_not_object"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", _NON_OBJECT_BODIES)
    async def test_batch_embed_non_object_body_is_400(self, store, payload):
        async with _client(_guard_app(store, embedder=_FakeEmbedder())) as client:
            resp = await client.post("/api/knowledge/embedding/generate", json=payload)
            assert resp.status == 400
            assert (await resp.json())["code"] == "body_not_object"

    @pytest.mark.asyncio
    async def test_batch_embed_invalid_json_is_400(self, store):
        async with _client(_guard_app(store, embedder=_FakeEmbedder())) as client:
            resp = await client.post(
                "/api/knowledge/embedding/generate",
                data="{oops",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_json"

    @pytest.mark.asyncio
    async def test_batch_embed_absent_body_still_means_defaults(self, store):
        # No body at all keeps the fill-NULL default path (not a 400): the
        # guard's allow_absent branch preserves the pre-guard contract.
        async with _client(_guard_app(store, embedder=_FakeEmbedder())) as client:
            resp = await client.post("/api/knowledge/embedding/generate")
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_invalid_json_yields_code_at_every_site(self, store, monkeypatch):
        # At the already-guarded sites the parse-failure path's entire
        # change is the machine-readable ``code`` field -- pin it everywhere.
        monkeypatch.setattr(f"{MODULE}.KiroCrewConfig.load", staticmethod(_cfg))
        item_id = store.add_item("a", "body", "note")
        sid = store.add_source("s", "web", "https://example.com")
        app = _guard_app(store, pipeline=MagicMock(), embedder=_FakeEmbedder())
        endpoints = [
            ("patch", f"/api/knowledge/items/{item_id}"),
            ("post", "/api/knowledge/sources"),
            ("patch", f"/api/knowledge/sources/{sid}"),
            ("post", f"/api/knowledge/sources/{sid}/files/retry"),
            ("post", f"/api/knowledge/sources/{sid}/files/skip"),
            ("post", f"/api/knowledge/sources/{sid}/ingest-text"),
            ("post", "/api/knowledge/import"),
            ("post", "/api/knowledge/embedding/generate"),
            ("post", "/api/knowledge/agent-document"),
        ]
        async with _client(app) as client:
            for method, path in endpoints:
                resp = await getattr(client, method)(
                    path, data="{oops", headers={"Content-Type": "application/json"}
                )
                assert resp.status == 400, (path, resp.status)
                assert (await resp.json())["code"] == "invalid_json", path

    @pytest.mark.asyncio
    async def test_absent_body_is_400_at_non_allow_absent_site(self, store):
        # Only the embedding-generate site opts into allow_absent; everywhere
        # else an empty body is a client mistake, not silent defaults.
        async with _client(_guard_app(store)) as client:
            resp = await client.post("/api/knowledge/sources")
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_json"

    @pytest.mark.asyncio
    async def test_unknown_charset_is_400_not_500(self, store):
        # charset= names a codec Python does not have: request.json() raises
        # LookupError (not a ValueError), which must also read as a client
        # mistake, not a 500.
        async with _client(_guard_app(store)) as client:
            resp = await client.post(
                "/api/knowledge/sources",
                data=b'{"name": "x"}',
                headers={"Content-Type": "application/json; charset=not-a-codec"},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_json"

    # The RecursionError and transport-error boundaries moved with the guard:
    # they are properties of ``_shared.read_bounded_json``, not of this module,
    # and are pinned in ``test_read_bounded_json.py`` (TestDecodeContract).

    @pytest.mark.asyncio
    async def test_file_state_object_body_still_works(self, store):
        # The guard must not break the success path it fronts.
        sid = store.add_source("s", "local_folder", "/tmp/x")
        store.db.execute(
            "INSERT INTO folder_file_state (source_id, file_path, last_seen, status) "
            "VALUES (?, ?, '2026-01-01', 'failed')",
            (sid, "/tmp/x/a.md"),
        )
        store.db.commit()
        async with _client(_guard_app(store)) as client:
            resp = await client.post(
                f"/api/knowledge/sources/{sid}/files/retry", json={"file_path": "/tmp/x/a.md"}
            )
            assert resp.status == 200
            row = store.db.execute(
                "SELECT status FROM folder_file_state WHERE source_id = ?", (sid,)
            ).fetchone()
            assert row["status"] == "pending"
