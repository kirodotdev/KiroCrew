"""Tests for folder watch API endpoints (confirm, pause, resume, files, retry, skip)."""

from __future__ import annotations

import asyncio
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


def _props(store, sid) -> dict:
    row = store.db.execute("SELECT properties FROM sources WHERE id = ?", (sid,)).fetchone()
    return json.loads(row["properties"] or "{}")


class TestEditIgnorePatterns:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("source_type", ["local_folder", "obsidian_vault"])
    async def test_sets_ignore_patterns_on_folder_source(self, store, source_type):
        sid = store.add_source("Vault", source_type, "/tmp/vault",
                               properties={"namespace": "work", "ignore_patterns": ["old/**"]})
        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.patch(f"/api/knowledge/sources/{sid}",
                                      json={"ignore_patterns": ["  generated/**  ", "", "*.tmp", "*.tmp"]})
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert data["ignore_patterns"] == ["generated/**", "*.tmp"]
        props = _props(store, sid)
        assert props["ignore_patterns"] == ["generated/**", "*.tmp"]
        assert props["namespace"] == "work"  # other properties survive the edit
        row = store.db.execute("SELECT name FROM sources WHERE id = ?", (sid,)).fetchone()
        assert row["name"] == "Vault"  # name untouched when not sent

    @pytest.mark.asyncio
    async def test_empty_list_clears_ignore_patterns(self, store):
        sid = store.add_source("Vault", "local_folder", "/tmp/vault",
                               properties={"ignore_patterns": ["old/**"]})
        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.patch(f"/api/knowledge/sources/{sid}", json={"ignore_patterns": []})
            assert resp.status == 200
            assert (await resp.json())["ignore_patterns"] == []
        assert "ignore_patterns" not in _props(store, sid)

    @pytest.mark.asyncio
    async def test_name_and_patterns_in_one_request(self, store):
        sid = store.add_source("Old", "local_folder", "/tmp/vault")
        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.patch(f"/api/knowledge/sources/{sid}",
                                      json={"name": "New", "ignore_patterns": ["a/**"]})
            assert resp.status == 200
        row = store.db.execute("SELECT name FROM sources WHERE id = ?", (sid,)).fetchone()
        assert row["name"] == "New"
        assert _props(store, sid)["ignore_patterns"] == ["a/**"]

    @pytest.mark.asyncio
    async def test_rejected_for_non_folder_source(self, store):
        sid = store.add_source("Doc", "local_file", "/tmp/doc.md")
        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.patch(f"/api/knowledge/sources/{sid}", json={"ignore_patterns": ["a"]})
            assert resp.status == 400
        assert "ignore_patterns" not in _props(store, sid)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", [
        "a/**",                 # not a list
        ["a/**", 3],            # non-string entry
        ["x" * 1025],           # entry over the length cap
        [f"p{i}" for i in range(501)],  # list over the count cap
    ])
    async def test_invalid_patterns_rejected_without_write(self, store, payload):
        sid = store.add_source("Vault", "local_folder", "/tmp/vault",
                               properties={"ignore_patterns": ["keep/**"]})
        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.patch(f"/api/knowledge/sources/{sid}", json={"ignore_patterns": payload})
            assert resp.status == 400
        assert _props(store, sid)["ignore_patterns"] == ["keep/**"]

    @pytest.mark.asyncio
    async def test_invalid_patterns_do_not_apply_a_valid_rename(self, store):
        sid = store.add_source("Old", "local_folder", "/tmp/vault")
        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.patch(f"/api/knowledge/sources/{sid}",
                                      json={"name": "New", "ignore_patterns": [1]})
            assert resp.status == 400
        row = store.db.execute("SELECT name FROM sources WHERE id = ?", (sid,)).fetchone()
        assert row["name"] == "Old"

    @pytest.mark.asyncio
    async def test_body_without_editable_fields_rejected(self, store):
        sid = store.add_source("Old", "local_folder", "/tmp/vault")
        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.patch(f"/api/knowledge/sources/{sid}", json={})
            assert resp.status == 400


class TestPurgeIgnored:
    def _watcher(self, purged=2):
        watcher = MagicMock()
        watcher._folder_watcher.purge_ignored = AsyncMock(return_value=purged)
        return watcher

    @pytest.mark.asyncio
    async def test_save_without_purge_leaves_indexed_items_alone(self, store):
        sid = store.add_source("Vault", "local_folder", "/tmp/vault")
        watcher = self._watcher()
        async with TestClient(TestServer(_make_app(store, watcher))) as client:
            resp = await client.patch(f"/api/knowledge/sources/{sid}", json={"ignore_patterns": ["a/**"]})
            assert resp.status == 200
            assert "purge" not in await resp.json()
        watcher._folder_watcher.purge_ignored.assert_not_called()

    @pytest.mark.asyncio
    async def test_purge_ignored_removes_matches_after_saving(self, store):
        sid = store.add_source("Vault", "local_folder", "/tmp/vault")
        watcher = self._watcher(purged=3)
        async with TestClient(TestServer(_make_app(store, watcher))) as client:
            resp = await client.patch(f"/api/knowledge/sources/{sid}",
                                      json={"ignore_patterns": ["a/**"], "purge_ignored": True})
            assert resp.status == 200
            # The purge is started in the background, like resume/confirm scans,
            # so a large folder does not hold the request open.
            body = await resp.json()
            assert body["purge"] == "started"

            async def recorded() -> dict:
                while "last_purge" not in _props(store, sid):
                    await asyncio.sleep(0.01)
                return _props(store, sid)["last_purge"]
            outcome = await asyncio.wait_for(recorded(), timeout=5)
        watcher._folder_watcher.purge_ignored.assert_awaited_once_with(sid, "/tmp/vault", ["a/**"])
        assert _props(store, sid)["ignore_patterns"] == ["a/**"]
        # The outcome is recorded under the request's id so the UI can report it.
        assert outcome == {"id": body["purge_id"], "removed": 3}

    @pytest.mark.asyncio
    async def test_failed_purge_is_recorded_and_keeps_the_patterns(self, store):
        sid = store.add_source("Vault", "local_folder", "/tmp/vault")
        watcher = self._watcher()
        watcher._folder_watcher.purge_ignored = AsyncMock(side_effect=RuntimeError("disk"))
        async with TestClient(TestServer(_make_app(store, watcher))) as client:
            resp = await client.patch(f"/api/knowledge/sources/{sid}",
                                      json={"ignore_patterns": ["a/**"], "purge_ignored": True})
            body = await resp.json()

            async def recorded() -> dict:
                while "last_purge" not in _props(store, sid):
                    await asyncio.sleep(0.01)
                return _props(store, sid)["last_purge"]
            outcome = await asyncio.wait_for(recorded(), timeout=5)
        assert outcome == {"id": body["purge_id"], "failed": True}
        assert _props(store, sid)["ignore_patterns"] == ["a/**"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("body, code", [
        ({"purge_ignored": True}, "source_edit_empty"),                        # no editable field
        ({"name": "Renamed", "purge_ignored": True}, "purge_requires_ignore_patterns"),
        ({"ignore_patterns": ["a/**"], "purge_ignored": "yes"}, "invalid_purge_ignored"),
    ])
    async def test_invalid_purge_rejected_without_write(self, store, body, code):
        sid = store.add_source("Vault", "local_folder", "/tmp/vault")
        watcher = self._watcher()
        async with TestClient(TestServer(_make_app(store, watcher))) as client:
            resp = await client.patch(f"/api/knowledge/sources/{sid}", json=body)
            assert resp.status == 400
            assert (await resp.json())["code"] == code
        assert "ignore_patterns" not in _props(store, sid)
        assert store.db.execute("SELECT name FROM sources WHERE id = ?", (sid,)).fetchone()["name"] == "Vault"
        watcher._folder_watcher.purge_ignored.assert_not_called()

    @pytest.mark.asyncio
    async def test_purge_without_watcher_is_refused_before_writing(self, store):
        sid = store.add_source("Vault", "local_folder", "/tmp/vault")
        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.patch(f"/api/knowledge/sources/{sid}",
                                      json={"ignore_patterns": ["a/**"], "purge_ignored": True})
            assert resp.status == 503
        assert "ignore_patterns" not in _props(store, sid)
