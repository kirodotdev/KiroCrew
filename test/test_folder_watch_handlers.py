"""Tests for folder watch API endpoints (confirm, pause, resume, files, retry, skip)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers.knowledge import (
    add_source,
    confirm_source,
    delete_source,
    list_source_files,
    pause_source,
    rename_source,
    resume_source,
    retry_file,
    skip_file,
)
from kiro_crew.knowledge.store import KnowledgeStore


@pytest.fixture()
def store(tmp_path):
    s = KnowledgeStore(str(tmp_path / "test.db"))
    yield s
    s.close()


def _make_app(store, watcher=None):
    """Create minimal app with folder watch routes."""
    from kiro_crew.knowledge.connectors.local_folder import LocalFolderConnector

    app = web.Application()
    state = MagicMock()
    state.knowledge_store = store
    app["state"] = state
    # Register LocalFolderConnector so folder sources pass validation
    sync = MagicMock()
    sync.get_connector = lambda t: LocalFolderConnector() if t in ("local_folder", "obsidian_vault") else None
    app["knowledge_sync"] = sync
    if watcher:
        app["knowledge_watcher"] = watcher
    app.router.add_post("/api/knowledge/sources", add_source)
    app.router.add_post("/api/knowledge/sources/{id}/confirm", confirm_source)
    app.router.add_post("/api/knowledge/sources/{id}/pause", pause_source)
    app.router.add_post("/api/knowledge/sources/{id}/resume", resume_source)
    app.router.add_get("/api/knowledge/sources/{id}/files", list_source_files)
    app.router.add_post("/api/knowledge/sources/{id}/files/retry", retry_file)
    app.router.add_post("/api/knowledge/sources/{id}/files/skip", skip_file)
    app.router.add_patch("/api/knowledge/sources/{id}", rename_source)
    app.router.add_delete("/api/knowledge/sources/{id}", delete_source)
    return app


class TestAddSourceFolder:
    @pytest.mark.asyncio
    async def test_folder_returns_pending_confirmation(self, store, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "note.md").write_text("hello")

        watcher = MagicMock()
        watcher._folder_watcher = MagicMock()
        watcher._folder_watcher._walk = MagicMock(return_value=[(str(vault / "note.md"), 1000.0)])

        async with TestClient(TestServer(_make_app(store, watcher))) as client:
            resp = await client.post("/api/knowledge/sources", json={
                "name": "test", "source_type": "local_folder", "uri": str(vault)
            })
            assert resp.status == 201
            data = await resp.json()
            assert data["status"] == "pending_confirmation"
            assert data["file_count"] == 1
            # The dashboard picks the row's control from the sync_status COLUMN,
            # not the properties JSON: only 'pending_confirmation' there renders
            # the Confirm button that starts the scan. A column left at its
            # 'pending' default makes the source unstartable.
            row = store.db.execute(
                "SELECT sync_status FROM sources WHERE id = ?", (data["id"],)).fetchone()
            assert row["sync_status"] == "pending_confirmation"

    @pytest.mark.asyncio
    async def test_folder_sensitive_path_rejected(self, store, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        async with TestClient(TestServer(_make_app(store))) as client:
            with patch("kiro_crew.dashboard.handlers.knowledge.is_sensitive_path", return_value=True):
                resp = await client.post("/api/knowledge/sources", json={
                    "name": "test", "source_type": "local_folder", "uri": str(vault)
                })
            assert resp.status == 403


class TestConfirmSource:
    @pytest.mark.asyncio
    async def test_confirm_starts_scan(self, store, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        sid = store.add_source("test", "local_folder", str(vault),
                               properties={"sync_status": "pending_confirmation"})

        watcher = MagicMock()
        watcher._folder_watcher = MagicMock()
        watcher._folder_watcher.scan_source = AsyncMock(return_value={"new": 0})

        async with TestClient(TestServer(_make_app(store, watcher))) as client:
            resp = await client.post(f"/api/knowledge/sources/{sid}/confirm")
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "scanning"

    @pytest.mark.asyncio
    async def test_confirm_not_found(self, store):
        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.post("/api/knowledge/sources/nonexistent/confirm")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_confirm_sensitive_path_blocked(self, store, tmp_path):
        sid = store.add_source("test", "local_folder", str(tmp_path))
        async with TestClient(TestServer(_make_app(store))) as client:
            with patch("kiro_crew.dashboard.handlers.knowledge.is_sensitive_path", return_value=True):
                resp = await client.post(f"/api/knowledge/sources/{sid}/confirm")
            assert resp.status == 403


class TestPauseSource:
    @pytest.mark.asyncio
    async def test_pause_sets_paused(self, store, tmp_path):
        sid = store.add_source("test", "local_folder", str(tmp_path), properties={})
        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.post(f"/api/knowledge/sources/{sid}/pause")
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "paused"

        # Verify DB state
        row = store.db.execute("SELECT properties, sync_status FROM sources WHERE id = ?", (sid,)).fetchone()
        props = json.loads(row["properties"])
        assert props["scan_paused"] is True
        assert row["sync_status"] == "paused"

    @pytest.mark.asyncio
    async def test_pause_records_the_status_in_one_place(self, store, tmp_path):
        """The pause lands in the COLUMN the watcher's pre-scan skip reads, and
        nowhere else: a second copy in the properties JSON is what let a paused
        folder go on being walked every sweep while one store said "active"."""
        sid = store.add_source(
            "test", "local_folder", str(tmp_path), properties={"sync_status": "active"},
        )
        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.post(f"/api/knowledge/sources/{sid}/pause")
            assert resp.status == 200
        row = store.db.execute(
            "SELECT sync_status, properties FROM sources WHERE id = ?", (sid,)).fetchone()
        assert row["sync_status"] == "paused"
        assert "sync_status" not in json.loads(row["properties"])


class TestResumeSource:
    @pytest.mark.asyncio
    async def test_resume_clears_pause(self, store, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        sid = store.add_source("test", "local_folder", str(vault),
                               properties={"scan_paused": True})

        watcher = MagicMock()
        watcher._folder_watcher = MagicMock()
        watcher._folder_watcher.scan_source = AsyncMock(return_value={"new": 0})

        async with TestClient(TestServer(_make_app(store, watcher))) as client:
            resp = await client.post(f"/api/knowledge/sources/{sid}/resume")
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_resume_sensitive_path_blocked(self, store, tmp_path):
        sid = store.add_source("test", "local_folder", str(tmp_path))
        async with TestClient(TestServer(_make_app(store))) as client:
            with patch("kiro_crew.dashboard.handlers.knowledge.is_sensitive_path", return_value=True):
                resp = await client.post(f"/api/knowledge/sources/{sid}/resume")
            assert resp.status == 403


class TestListSourceFiles:
    @pytest.mark.asyncio
    async def test_returns_file_list(self, store):
        sid = store.add_source("test", "local_folder", "/tmp/vault")
        store.db.execute(
            "INSERT INTO folder_file_state (source_id, file_path, last_seen, status, item_ids) VALUES (?, ?, ?, ?, ?)",
            (sid, "/tmp/vault/a.md", "2026-01-01", "done", '["item1"]'))
        store.db.execute(
            "INSERT INTO folder_file_state (source_id, file_path, last_seen, status, error_message) VALUES (?, ?, ?, ?, ?)",
            (sid, "/tmp/vault/b.md", "2026-01-01", "failed", "parse error"))
        store.db.execute(
            "INSERT INTO folder_file_state (source_id, file_path, last_seen, status) VALUES (?, ?, ?, ?)",
            (sid, "/tmp/vault/c.md", "2026-01-01", "skipped"))
        store.db.commit()

        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.get(f"/api/knowledge/sources/{sid}/files")
            assert resp.status == 200
            data = await resp.json()
            assert data["total"] == 3
            assert data["done"] == 1
            assert data["failed"] == 1
            assert data["skipped"] == 1


class TestRetryFile:
    @pytest.mark.asyncio
    async def test_retry_resets_to_pending(self, store):
        sid = store.add_source("test", "local_folder", "/tmp/vault")
        store.db.execute(
            "INSERT INTO folder_file_state (source_id, file_path, last_seen, status, error_message) VALUES (?, ?, ?, ?, ?)",
            (sid, "/tmp/vault/a.md", "2026-01-01", "failed", "error"))
        store.db.commit()

        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.post(f"/api/knowledge/sources/{sid}/files/retry",
                                     json={"file_path": "/tmp/vault/a.md"})
            assert resp.status == 200

        row = store.db.execute(
            "SELECT status, error_message FROM folder_file_state WHERE source_id = ? AND file_path = ?",
            (sid, "/tmp/vault/a.md")).fetchone()
        assert row["status"] == "pending"
        assert row["error_message"] is None

    @pytest.mark.asyncio
    async def test_retry_sensitive_path_blocked(self, store):
        sid = store.add_source("test", "local_folder", "/tmp/vault")
        async with TestClient(TestServer(_make_app(store))) as client:
            with patch("kiro_crew.dashboard.handlers.knowledge.is_sensitive_path", return_value=True):
                resp = await client.post(f"/api/knowledge/sources/{sid}/files/retry",
                                         json={"file_path": "/home/user/.ssh/id_rsa"})
            assert resp.status == 403

    @pytest.mark.asyncio
    async def test_retry_missing_file_path(self, store):
        sid = store.add_source("test", "local_folder", "/tmp/vault")
        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.post(f"/api/knowledge/sources/{sid}/files/retry", json={})
            assert resp.status == 400


class TestSkipFile:
    @pytest.mark.asyncio
    async def test_skip_marks_skipped(self, store):
        sid = store.add_source("test", "local_folder", "/tmp/vault")
        store.db.execute(
            "INSERT INTO folder_file_state (source_id, file_path, last_seen, status) VALUES (?, ?, ?, ?)",
            (sid, "/tmp/vault/a.md", "2026-01-01", "failed"))
        store.db.commit()

        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.post(f"/api/knowledge/sources/{sid}/files/skip",
                                     json={"file_path": "/tmp/vault/a.md"})
            assert resp.status == 200

        row = store.db.execute(
            "SELECT status FROM folder_file_state WHERE source_id = ? AND file_path = ?",
            (sid, "/tmp/vault/a.md")).fetchone()
        assert row["status"] == "skipped"


class TestRenameSource:
    @pytest.mark.asyncio
    async def test_rename_updates_name(self, store):
        sid = store.add_source("Old Name", "local_file", "/tmp/doc.md")
        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.patch(f"/api/knowledge/sources/{sid}", json={"name": "New Name"})
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert data["name"] == "New Name"
        row = store.db.execute("SELECT name FROM sources WHERE id = ?", (sid,)).fetchone()
        assert row["name"] == "New Name"

    @pytest.mark.asyncio
    async def test_rename_trims_whitespace(self, store):
        sid = store.add_source("Old", "local_file", "/tmp/doc.md")
        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.patch(f"/api/knowledge/sources/{sid}", json={"name": "  Trimmed  "})
            assert resp.status == 200
        row = store.db.execute("SELECT name FROM sources WHERE id = ?", (sid,)).fetchone()
        assert row["name"] == "Trimmed"

    @pytest.mark.asyncio
    async def test_rename_empty_name_rejected(self, store):
        sid = store.add_source("Old", "local_file", "/tmp/doc.md")
        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.patch(f"/api/knowledge/sources/{sid}", json={"name": "   "})
            assert resp.status == 400
        row = store.db.execute("SELECT name FROM sources WHERE id = ?", (sid,)).fetchone()
        assert row["name"] == "Old"

    @pytest.mark.asyncio
    async def test_rename_non_string_rejected(self, store):
        sid = store.add_source("Old", "local_file", "/tmp/doc.md")
        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.patch(f"/api/knowledge/sources/{sid}", json={"name": 123})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_rename_too_long_rejected(self, store):
        sid = store.add_source("Old", "local_file", "/tmp/doc.md")
        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.patch(f"/api/knowledge/sources/{sid}", json={"name": "x" * 201})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_rename_unknown_id_404(self, store):
        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.patch("/api/knowledge/sources/nonexistent", json={"name": "X"})
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_rename_invalid_json_400(self, store):
        sid = store.add_source("Old", "local_file", "/tmp/doc.md")
        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.patch(f"/api/knowledge/sources/{sid}", data="not json",
                                      headers={"Content-Type": "application/json"})
            assert resp.status == 400
