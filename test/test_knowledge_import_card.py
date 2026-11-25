"""Owner-approved folder import uses the existing source routes, never a second writer."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from aiohttp import ClientTimeout, web
from aiohttp.test_utils import TestClient, TestServer
from guide_route_helpers import FakeState, _fake_auth

from kiro_crew import change_card_catalog as catalog
from kiro_crew.dashboard import change_cards as cards
from kiro_crew.dashboard.handlers import change_cards as card_routes
from kiro_crew.dashboard.handlers.knowledge import add_source, confirm_source, list_source_files
from kiro_crew.knowledge.chunker import HeadingAwareChunker
from kiro_crew.knowledge.connectors.local_folder import LocalFolderConnector
from kiro_crew.knowledge.folder_watcher import FolderWatcher
from kiro_crew.knowledge.ingestion import IngestionPipeline
from kiro_crew.knowledge.readers import FileReader
from kiro_crew.knowledge.retrieval import HybridRetriever
from kiro_crew.knowledge.store import KnowledgeStore
from kiro_crew.testing.synthetic_credentials import SAMPLE_SLACK_BOT_TOKEN
from kiro_crew.testing.wait import await_until, default_timeout

KIND = "knowledge.import_sources"


def source(root="/prepared/main", namespace="engineering-main", mode="documents"):
    return {"root": root, "namespace": namespace, "name": namespace, "mode": mode}


def test_preview_plans_native_add_then_owner_confirm_without_ingesting():
    assert KIND in {k["id"] for k in catalog.list_kinds()}
    p = catalog.validate_params(KIND, {"sources": [source()]})
    before = {
        "sources": [
            {
                "uri": "/prepared/main",
                "files": 2,
                "estimated_chunks": 3,
                "estimated_llm_calls": 5,
                "existing": None,
            }
        ]
    }
    preview = catalog.build_preview(KIND, p, before, {})
    add, confirm = preview["apply"]
    assert (add["method"], add["path"]) == ("POST", "/api/knowledge/sources")
    assert add["body"]["uri"] == "/prepared/main"
    assert add["body"]["source_type"] == "local_folder"
    assert add["body"]["properties"]["confine_to_root"] is True
    assert add["body"]["properties"]["redact_secrets"] is True
    assert set(add["body"]["properties"]["include_extensions"]) == {".md", ".pdf"}
    assert confirm["method"] == "POST"
    assert confirm["path"] == "/api/knowledge/sources/{{step0.id}}/confirm"
    assert confirm["fill"] == [
        {"field": "id", "source": "step", "step": 0, "key": "id", "in": "path"}
    ]
    # The card's scope row counts crewmates, not folder sources.
    assert preview["scope"] is None
    assert "5" in str(preview["changes"])


@pytest.mark.parametrize(
    "sources",
    [
        [],
        [source()] * 5,
        [source("relative/path")],
        [source("//server/share")],
        [source("/prepared", mode="raw")],
        [source(), source()],
        [source(), {**source("/prepared/code", "engineering-code"), "properties": {}}],
    ],
)
def test_unbounded_or_ambiguous_source_requests_are_rejected(sources):
    assert KIND in {k["id"] for k in catalog.list_kinds()}
    with pytest.raises(catalog.CardCatalogError):
        catalog.validate_params(KIND, {"sources": sources})


@pytest.fixture
def prepared(tmp_path, opened):
    root = tmp_path / "prepared"
    root.mkdir()
    (root / "spec.md").write_text("DDA3 frame is 444 bytes.", encoding="utf-8")
    store = opened(KnowledgeStore(str(tmp_path / "knowledge.db")))
    state = SimpleNamespace(knowledge_store=store)
    watcher = FolderWatcher(store, None)
    app = {"knowledge_watcher": SimpleNamespace(_folder_watcher=watcher)}
    params = catalog.validate_params(KIND, {"sources": [source(str(root))]})
    return root, store, state, app, params


@pytest.mark.asyncio
async def test_dry_run_filters_instructions_secret_paths_and_unsupported_code(prepared):
    root, store, state, app, params = prepared
    for name in (
        "AGENTS.md",
        "SKILL.md",
        "AgEnTs.Md",
        "claude.md",
        "gemini.md",
        ".env",
        "entry.ino",
        "source.py",
    ):
        (root / name).write_text("fixture; must not enter this document source", encoding="utf-8")
    preview, before, _ = await cards.build(KIND, params, state=state, app=app)
    assert before["sources"][0]["files"] == 1
    assert preview["apply"][0]["body"]["uri"] == str(root.resolve())
    assert await asyncio.to_thread(store.get_source_by_uri, str(root)) is None
    assert (await asyncio.to_thread(store.aggregate_stats)).items == 0


@pytest.mark.asyncio
async def test_outward_file_link_never_enters_discovery(prepared, tmp_path):
    root, _, state, app, params = prepared
    outside = tmp_path / "outside.md"
    outside.write_text("Outside the approved root", encoding="utf-8")
    try:
        (root / "linked.md").symlink_to(outside)
    except OSError:
        pytest.skip("file symlinks unavailable on this test host")
    _, before, _ = await cards.build(KIND, params, state=state, app=app)
    assert before["sources"][0]["files"] == 1


@pytest.mark.asyncio
async def test_symlink_root_and_nested_roots_are_refused(prepared, tmp_path):
    root, _, state, app, _ = prepared
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(root, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable on this test host")
    p = catalog.validate_params(KIND, {"sources": [source(str(alias))]})
    with pytest.raises(catalog.CardCatalogError) as exc:
        await cards.build(KIND, p, state=state, app=app)
    assert exc.value.code == "invalid_root"
    child = root / "child"
    child.mkdir()
    (child / "design.md").write_text("Nested source", encoding="utf-8")
    p = catalog.validate_params(KIND, {"sources": [source(str(root)), source(str(child), "draft")]})
    with pytest.raises(catalog.CardCatalogError) as exc:
        await cards.build(KIND, p, state=state, app=app)
    assert exc.value.code == "overlapping_roots"


class _Extractor:
    """The external model boundary is offline; reader/chunker/store/retrieval are real."""

    _pool = None

    def __init__(self):
        self.seen = []

    async def extract_batch(self, chunks):
        self.seen.extend(chunks)
        return [
            {"category": "document", "summary": "", "entities": [], "relations": []} for _ in chunks
        ]


@pytest.mark.asyncio
async def test_opted_in_folder_redacts_before_model_storage_and_retrieval(prepared):
    root, store, _, _, _ = prepared
    document = root / "spec.md"
    document.write_text(f"DDA3 frame is 444 bytes.\n{SAMPLE_SLACK_BOT_TOKEN}", encoding="utf-8")
    sid = await asyncio.to_thread(
        store.add_source,
        name="main",
        source_type="local_folder",
        uri=str(root),
        properties={"redact_secrets": True},
    )
    extractor = _Extractor()
    pipeline = IngestionPipeline(store, extractor, HeadingAwareChunker(), FileReader())
    job = await pipeline.ingest_file(str(document), source_id=sid)
    assert (await asyncio.to_thread(pipeline.get_job_status, job))["status"] == "completed"
    assert extractor.seen and all(SAMPLE_SLACK_BOT_TOKEN not in s for s in extractor.seen)
    hits = await asyncio.to_thread(
        lambda: HybridRetriever(store).search("DDA3", source_id=sid, limit=3)
    )
    assert hits and all(SAMPLE_SLACK_BOT_TOKEN not in h["content"] for h in hits)
    assert any("444" in h["content"] for h in hits)


def import_app(store):
    """Actual card hook, source handlers and ingestion over the test's own SQLite file."""
    state = FakeState()
    state.open_slot("chat-1")
    state._change_card_store = cards.CardStore(None)
    state.knowledge_store = store
    state.conversation_log = None
    pipeline = IngestionPipeline(store, _Extractor(), HeadingAwareChunker(), FileReader())
    watcher = FolderWatcher(store, pipeline)
    app = web.Application(middlewares=[_fake_auth, card_routes.change_card_middleware])
    app.update(
        {
            "state": state,
            "knowledge_pipeline": pipeline,
            "knowledge_watcher": SimpleNamespace(_folder_watcher=watcher),
            "knowledge_sync": SimpleNamespace(get_connector=lambda t: LocalFolderConnector()),
            "_scan_tasks": set(),
            "_bg_tasks": set(),
        }
    )
    card_routes.register_change_card_routes(app)
    app.router.add_post("/api/knowledge/sources", add_source)
    app.router.add_post("/api/knowledge/sources/{id}/confirm", confirm_source)
    app.router.add_get("/api/knowledge/sources/{id}/files", list_source_files)
    return app


def owner_step(card, index, role="owner"):
    return {
        "X-Test-Auth": role,
        "X-Card-Id": card["id"],
        "X-Card-Revision": str(card["revision"]),
        "X-Card-Op": "apply",
        "X-Card-Step": str(index),
    }


async def propose(client, sources):
    response = await client.post(
        "/api/cards/agent/propose",
        headers={
            "X-Test-Auth": "internal",
            "X-Session-Key": "dashboard:chat-1",
        },
        json={"kind": KIND, "params": {"sources": sources}},
    )
    assert response.status == 200, await response.text()
    return await response.json()


async def apply_step(client, card, index, responses):
    step = card["plan"]["apply"][index]
    path = step["path"]
    for fill in step.get("fill", []):
        path = path.replace(
            "{{step" + str(fill["step"]) + "." + fill["key"] + "}}",
            responses[fill["step"]][fill["key"]],
        )
    response = await client.request(
        step["method"], path, json=step["body"], headers=owner_step(card, index)
    )
    return response.status, await response.json()


async def drain(app):
    tasks = set(app["_scan_tasks"]) | set(app["_bg_tasks"]) | set(card_routes._BACKGROUND)
    if tasks:
        await await_until(asyncio.gather(*tasks))


@pytest.mark.asyncio
async def test_only_owner_applies_and_repeat_import_creates_no_duplicates(prepared, tmp_path):
    root, store, _, _, _ = prepared
    code = tmp_path / "code-draft"
    code.mkdir()
    (code / "protocol.py").write_text("DDA3_TEST_FRAME = 444\n", encoding="utf-8")
    inputs = [source(str(root)), source(str(code), "engineering-draft", "code")]
    app = import_app(store)
    async with TestClient(  # flake-ok: aiohttp wrapper uses the runner's lost-run bound
        TestServer(app, host="127.0.0.1"), timeout=ClientTimeout(total=default_timeout())
    ) as c:
        card = await propose(c, inputs)
        assert (await asyncio.to_thread(store.aggregate_stats)).sources == 0
        add = card["plan"]["apply"][0]
        for role in ("internal", "app"):
            response = await c.post(
                add["path"], json=add["body"], headers=owner_step(card, 0, role)
            )
            assert response.status == 403, await response.text()
        responses = []
        for index in range(4):
            status, data = await apply_step(c, card, index, responses)
            assert status in (200, 201), data
            responses.append(data)
        await drain(app)
        assert (await asyncio.to_thread(store.aggregate_stats)).sources == 2
        for sid in (responses[0]["id"], responses[2]["id"]):
            response = await c.get(
                f"/api/knowledge/sources/{sid}/files", headers={"X-Test-Auth": "owner"}
            )
            files = await response.json()
            assert (files["done"], files["failed"], files["skipped"]) == (1, 0, 0)
        response = await c.post(
            "/api/cards/agent/propose",
            headers={
                "X-Test-Auth": "internal",
                "X-Session-Key": "dashboard:chat-1",
            },
            json={"kind": KIND, "params": {"sources": inputs}},
        )
        assert response.status == 400
        assert (await response.json())["code"] == "no_change"
        assert (await asyncio.to_thread(store.aggregate_stats)).sources == 2
        hits = await asyncio.to_thread(
            lambda: HybridRetriever(store).search(
                "DDA3_TEST_FRAME", source_id=responses[2]["id"], limit=3
            )
        )
        assert hits and all(h["source_uri"] == str(code.resolve()) for h in hits)
        assert all(h["file_path"] == str(code / "protocol.py") for h in hits)
        await drain(app)


@pytest.mark.asyncio
async def test_pending_source_is_confirmed_in_place_and_different_namespace_is_refused(prepared):
    root, store, _, _, _ = prepared
    app = import_app(store)
    inputs = [source(str(root))]
    p = catalog.validate_params(KIND, {"sources": inputs})
    preview, _, _ = await cards.build(KIND, p, state=app["state"], app=app)
    add = preview["apply"][0]
    async with TestClient(  # flake-ok: aiohttp wrapper uses the runner's lost-run bound
        TestServer(app, host="127.0.0.1"), timeout=ClientTimeout(total=default_timeout())
    ) as c:
        response = await c.post(add["path"], json=add["body"], headers={"X-Test-Auth": "owner"})
        sid = (await response.json())["id"]
        response = await c.post(
            "/api/cards/agent/propose",
            headers={
                "X-Test-Auth": "internal",
                "X-Session-Key": "dashboard:chat-1",
            },
            json={"kind": KIND, "params": {"sources": [source(str(root), "other")]}},
        )
        assert response.status == 400
        assert (await response.json())["code"] == "source_conflict"
        card = await propose(c, inputs)
        assert len(card["plan"]["apply"]) == 1
        assert card["plan"]["apply"][0]["path"] == f"/api/knowledge/sources/{sid}/confirm"
        status, data = await apply_step(c, card, 0, [])
        assert status == 200, data
        await drain(app)
        assert (await asyncio.to_thread(store.aggregate_stats)).sources == 1
        row = await asyncio.to_thread(store.get_source_by_uri, str(root))
        assert row["id"] == sid


@pytest.mark.asyncio
async def test_partial_failure_keeps_success_and_retry_only_adds_missing_source(prepared, tmp_path):
    root, store, _, _, _ = prepared
    other = tmp_path / "other"
    other.mkdir()
    document = other / "draft.md"
    document.write_text("Second prepared source", encoding="utf-8")
    inputs = [source(str(root)), source(str(other), "draft")]
    app = import_app(store)
    async with TestClient(  # flake-ok: aiohttp wrapper uses the runner's lost-run bound
        TestServer(app, host="127.0.0.1"), timeout=ClientTimeout(total=default_timeout())
    ) as c:
        card = await propose(c, inputs)
        responses = []
        for index in (0, 1):
            status, data = await apply_step(c, card, index, responses)
            assert status in (200, 201), data
            responses.append(data)
        await drain(app)
        document.unlink()
        other.rmdir()
        status, error = await apply_step(c, card, 2, responses)
        assert status == 400, error
        record = app["state"]._change_card_store.get(card["id"])
        assert record["status"] == "partial"
        assert record["evidence"][0]["id"] == responses[0]["id"]
        assert (await asyncio.to_thread(store.aggregate_stats)).sources == 1
        other.mkdir()
        document.write_text("Second prepared source", encoding="utf-8")
        retry = await propose(c, inputs)
        assert len(retry["plan"]["apply"]) == 2
        assert retry["plan"]["apply"][0]["body"]["uri"] == str(other)
        retried = []
        for index in (0, 1):
            status, data = await apply_step(c, retry, index, retried)
            assert status in (200, 201), data
            retried.append(data)
        await drain(app)
        assert (await asyncio.to_thread(store.aggregate_stats)).sources == 2
        row = await asyncio.to_thread(store.get_source_by_uri, str(root))
        assert row["id"] == responses[0]["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key,value", [("redact_secrets", 1), ("min_file_bytes", 9999), ("chunk_budget", 0)]
)
async def test_existing_source_with_different_safety_or_budget_is_not_adopted(prepared, key, value):
    root, store, _, _, _ = prepared
    app = import_app(store)
    inputs = [source(str(root))]
    p = catalog.validate_params(KIND, {"sources": inputs})
    preview, _, _ = await cards.build(KIND, p, state=app["state"], app=app)
    body = preview["apply"][0]["body"]
    body["properties"][key] = value
    async with TestClient(  # flake-ok: aiohttp wrapper uses the runner's lost-run bound
        TestServer(app, host="127.0.0.1"), timeout=ClientTimeout(total=default_timeout())
    ) as c:
        response = await c.post(
            "/api/knowledge/sources", json=body, headers={"X-Test-Auth": "owner"}
        )
        assert response.status == 201
        response = await c.post(
            "/api/cards/agent/propose",
            headers={
                "X-Test-Auth": "internal",
                "X-Session-Key": "dashboard:chat-1",
            },
            json={"kind": KIND, "params": {"sources": inputs}},
        )
        assert response.status == 400
        assert (await response.json())["code"] == "source_conflict"
        assert (await asyncio.to_thread(store.aggregate_stats)).sources == 1
