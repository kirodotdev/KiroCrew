"""The owner Files editor shares resource revisions without app impersonation."""

from __future__ import annotations

import asyncio
import hashlib
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew import pinned_fs
from kiro_crew.apps.document_store import DocumentStoreError
from kiro_crew.apps.resource_access import REGISTRY, BoundDocument, ResourceRegistry
from kiro_crew.dashboard.handlers import files

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not pinned_fs.supports_pinned_walk(), reason="requires pinned publication"),
]


@pytest_asyncio.fixture
async def editor(tmp_path, monkeypatch):
    path = tmp_path / "formal.md"
    path.write_text("first\r\n", encoding="utf-8", newline="")
    bound = BoundDocument(tmp_path, path.name)
    lookups = []

    async def locate(candidate):
        lookups.append(candidate)
        return bound if candidate == path else None

    registry = ResourceRegistry()
    registry.register("fixture", AsyncMock(), owner_locator=locate)
    app = as_owner(web.Application())
    app[REGISTRY] = registry
    app.router.add_get("/api/file-read", files.api_file_read)
    app.router.add_post("/api/file-write", files.api_file_write)
    monkeypatch.setattr(files, "_sel", MagicMock())
    monkeypatch.setattr(files, "_owner_view_bypasses_credential_pass", AsyncMock(return_value=True))
    monkeypatch.setattr(files, "redact_owner_view_via_context", lambda text: text)
    async with TestClient(TestServer(app)) as client:
        yield client, path, bound, lookups


async def test_owner_read_returns_revision_for_exact_snapshot(editor):
    client, path, bound, _ = editor
    response = await client.get("/api/file-read", params={"path": str(path)})
    assert response.status == 200
    assert await response.text() == "first\r\n"
    assert response.headers["X-Resource-Managed"] == "true"
    assert response.headers["X-File-Revision"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert (
        response.headers["X-File-Revision"]
        == (await asyncio.to_thread(bound.store().read, path.name)).revision
    )


async def test_partial_owner_read_has_no_write_revision(editor, monkeypatch):
    client, path, _, _ = editor
    monkeypatch.setattr(files, "_FILE_READ_CAP", 4)
    response = await client.get("/api/file-read", params={"path": str(path)})
    assert response.status == 200
    assert response.headers["X-Truncated"] == "true"
    assert response.headers["X-Resource-Managed"] == "true"
    assert "X-File-Revision" not in response.headers


async def test_owner_write_requires_revision_without_changing_document(editor):
    client, path, _, _ = editor
    response = await client.post("/api/file-write", json={"path": str(path), "content": "lost"})
    assert response.status == 409
    assert (await response.json())["code"] == "revision_required"
    assert path.read_bytes() == b"first\r\n"


async def test_app_edit_invalidates_owner_revision_and_owner_edit_invalidates_app_revision(editor):
    client, path, bound, _ = editor
    store = bound.store()
    original = await asyncio.to_thread(store.read, path.name)
    current = await asyncio.to_thread(
        store.write, path.name, "app", operation="update", expected_revision=original.revision
    )
    response = await client.post(
        "/api/file-write",
        json={"path": str(path), "content": "stale owner", "expectedRevision": original.revision},
    )
    assert response.status == 409
    assert (await response.json())["code"] == "revision_conflict"
    assert path.read_text(encoding="utf-8") == "app"
    response = await client.post(
        "/api/file-write",
        json={"path": str(path), "content": "owner", "expectedRevision": current.revision},
    )
    assert response.status == 200
    assert (await response.json())["revision"] == hashlib.sha256(b"owner").hexdigest()
    with pytest.raises(DocumentStoreError, match="conflict"):
        await asyncio.to_thread(
            store.write,
            path.name,
            "stale app",
            operation="update",
            expected_revision=current.revision,
        )


async def test_unmanaged_file_keeps_legacy_write_contract(editor):
    client, managed, _, _ = editor
    path = managed.with_name("ordinary.md")
    path.write_text("old", encoding="utf-8")
    response = await client.post("/api/file-write", json={"path": str(path), "content": "new"})
    assert response.status == 200
    assert await response.json() == {"ok": True}
    assert path.read_text(encoding="utf-8") == "new"


async def test_app_token_never_reaches_owner_resource_locator(editor):
    client, path, _, lookups = editor
    headers = {"X-Test-App": "other-app"}
    response = await client.get("/api/file-read", params={"path": str(path)}, headers=headers)
    assert response.status == 403
    response = await client.post(
        "/api/file-write",
        json={"path": str(path), "content": "lost", "expectedRevision": "0" * 64},
        headers=headers,
    )
    assert response.status == 403
    assert lookups == []
    assert path.read_bytes() == b"first\r\n"


async def test_owner_windows_branch_keeps_legacy_writer_when_app_store_is_unsupported(
    editor, monkeypatch
):
    client, path, _, lookups = editor
    monkeypatch.setattr(files.pinned_fs, "supports_pinned_walk", lambda: False)
    response = await client.post("/api/file-write", json={"path": str(path), "content": "owner"})
    assert response.status == 200
    assert path.read_text(encoding="utf-8") == "owner"
    assert lookups == []
