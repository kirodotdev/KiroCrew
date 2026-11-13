"""Native resource HTTP contract against isolated on-disk Spec documents."""

from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


@pytest.mark.asyncio
async def test_scoped_http_create_cas_refusal_and_persistent_readback(tmp_path, monkeypatch):
    from kiro_crew.apps import resource_access as api
    from kiro_crew.apps.builtins.spec_builder.backend import repository_discovery, scoped_documents

    folder = tmp_path / ".kiro" / "specs" / "fixture"
    folder.mkdir(parents=True)
    (folder / "requirements.md").write_text("original", encoding="utf-8")
    monkeypatch.setattr(
        scoped_documents,
        "_aload_index",
        AsyncMock(return_value={"fixture": {"spec_dir": str(folder)}}),
    )
    monkeypatch.setattr(api, "_audit", lambda *args: None)
    actor = {"app": "fixture-bridge", "user": "fixture-owner"}
    paths = {
        api.resource_path("fixture", "spec-builder", "fixture", document, operation)
        for document in ("requirements.md", "design.md")
        for operation in ("read", "create", "update")
    }

    @web.middleware
    async def fixture_identity(request, handler):
        # This fixture stands at the already-tested authentication boundary.
        # HTTP input never selects or changes the middleware's claims.
        request.update(actor)
        return await handler(request)

    def application():
        app = web.Application(middlewares=[fixture_identity])
        registry = api.ResourceRegistry(
            lambda name: tmp_path if name == "fixture" else None,
            lambda name: tuple(paths) if name == "fixture-bridge" else (),
            lambda request: False,
        )
        registry.register("spec-builder", scoped_documents.read_resource)
        app[api.REGISTRY] = registry
        api.register_routes(app)
        return app

    async with TestClient(TestServer(application())) as client:
        read_path = api.resource_path(
            "fixture", "spec-builder", "fixture", "requirements.md", "read"
        )
        initial = await (await client.post(read_path, json={})).json()
        original_revision = initial["result"]["revision"]
        update_path = read_path.removesuffix("read") + "update"
        changed = await client.post(
            update_path, json={"content": "edited", "expectedRevision": original_revision}
        )
        assert changed.status == 200
        saved = (await changed.json())["result"]
        assert saved["content"] == "edited"
        stale = await client.post(
            update_path, json={"content": "lost", "expectedRevision": original_revision}
        )
        assert stale.status == 409
        assert (await stale.json())["error"] == "revision_conflict"
        create_path = api.resource_path("fixture", "spec-builder", "fixture", "design.md", "create")
        created = await client.post(
            create_path, json={"content": "# Design\n", "expectedRevision": None}
        )
        assert created.status == 200
        assert (
            await client.post(create_path, json={"content": "replace", "expectedRevision": None})
        ).status == 409
        actor["app"] = "unapproved"
        assert (
            await client.post(
                update_path, json={"content": "bad", "expectedRevision": saved["revision"]}
            )
        ).status == 403
        actor["app"] = "fixture-bridge"
        assert (
            await client.post(
                read_path.replace("/fixture/spec-builder", "/other/spec-builder", 1), json={}
            )
        ).status == 403
        assert (folder / "requirements.md").read_text() == "edited"
        assert not (folder / "tasks.md").exists()

    # A new HTTP application and the native Spec Builder reader both see disk,
    # not a response cache belonging to the original request or registry.
    async with TestClient(TestServer(application())) as client:
        current = await (await client.post(read_path, json={})).json()
        assert current["result"] == saved
    files, _docs, _tasks = repository_discovery._read_spec_files(folder)
    assert "edited" in files.values()
    assert "# Design\n" in files.values()
