"""Tests for the cron folder CRUD endpoints.

Covers GET/POST/PATCH/DELETE /api/cron-folders, including the contract that
deleting a folder clears folder_id on any assigned cron jobs (never deletes them).
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.cron import CronService
from kiro_crew.dashboard.handlers.cron import (
    api_cron_folders,
    api_cron_folders_create,
    api_cron_folders_delete,
    api_cron_folders_update,
)
from kiro_crew.dashboard.state import DashboardState


@pytest.fixture(autouse=True)
def _isolate_cron_store(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.cron._DEFAULT_DIR", tmp_path)
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    yield


def _make_state(tmp_path) -> MagicMock:
    state = MagicMock(spec=DashboardState)
    state._cron_folders = []
    state.save_cron_folders = MagicMock()
    state.delete_cron_folder = MagicMock(return_value=True)
    state.create_cron_folder = MagicMock(
        side_effect=lambda name, fid: {"id": fid, "name": name, "order": 0}
    )
    state.rename_cron_folder = MagicMock(
        side_effect=lambda fid, name: next(
            (dict(f, name=name) for f in state._cron_folders if f["id"] == fid), None
        )
    )
    state.push_refresh = MagicMock()
    state.crons = CronService()
    return state


def _request(state, body=None, match_info=None):
    request = MagicMock()
    request.app = {"state": state}
    if body is not None:
        request.json = AsyncMock(return_value=body)
    if match_info:
        request.match_info = match_info
    return request


class TestCronFoldersList:
    """GET /api/cron-folders returns all folders."""

    @pytest.mark.asyncio
    async def test_list_empty(self, tmp_path):
        state = _make_state(tmp_path)
        request = _request(state)
        resp = await api_cron_folders(request)
        assert resp.status == 200
        body = json.loads(resp.body)
        assert body == []

    @pytest.mark.asyncio
    async def test_list_with_folders(self, tmp_path):
        state = _make_state(tmp_path)
        state._cron_folders = [{"id": "a1", "name": "Ops", "order": 0}]
        request = _request(state)
        resp = await api_cron_folders(request)
        body = json.loads(resp.body)
        assert len(body) == 1
        assert body[0]["name"] == "Ops"


class TestCronFoldersCreate:
    """POST /api/cron-folders creates a new folder."""

    @pytest.mark.asyncio
    async def test_create_folder(self, tmp_path):
        state = _make_state(tmp_path)
        request = _request(state, body={"name": "Monitoring"})
        resp = await api_cron_folders_create(request)
        assert resp.status == 200
        body = json.loads(resp.body)
        assert body["name"] == "Monitoring"
        assert "id" in body
        assert body["order"] == 0
        state.create_cron_folder.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_folder_empty_name_rejected(self, tmp_path):
        state = _make_state(tmp_path)
        request = _request(state, body={"name": ""})
        resp = await api_cron_folders_create(request)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_create_folder_non_dict_body_rejected(self, tmp_path):
        """A JSON array body returns 400, not a 500 from .get() on a list."""
        state = _make_state(tmp_path)
        request = _request(state, body=[])
        resp = await api_cron_folders_create(request)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_create_folder_non_string_name_rejected(self, tmp_path):
        """A numeric name returns 400, not a 500 from .strip() on an int."""
        state = _make_state(tmp_path)
        request = _request(state, body={"name": 1})
        resp = await api_cron_folders_create(request)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_create_folder_missing_name_rejected(self, tmp_path):
        state = _make_state(tmp_path)
        request = _request(state, body={})
        resp = await api_cron_folders_create(request)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_create_save_failure_returns_500(self, tmp_path):
        """When persistence fails, the handler returns 500 with code."""
        state = _make_state(tmp_path)
        state.create_cron_folder = MagicMock(side_effect=OSError("disk full"))
        request = _request(state, body={"name": "WillFail"})
        resp = await api_cron_folders_create(request)
        assert resp.status == 500
        body = json.loads(resp.body)
        assert body["code"] == "folder_save_failed"


class TestCronFoldersUpdate:
    """PATCH /api/cron-folders/{folder_id} renames a folder."""

    @pytest.mark.asyncio
    async def test_rename_folder(self, tmp_path):
        state = _make_state(tmp_path)
        state._cron_folders = [{"id": "f1", "name": "Old", "order": 0}]
        request = _request(state, body={"name": "New"}, match_info={"folder_id": "f1"})
        resp = await api_cron_folders_update(request)
        assert resp.status == 200
        body = json.loads(resp.body)
        assert body["name"] == "New"
        state.rename_cron_folder.assert_called_once_with("f1", "New")

    @pytest.mark.asyncio
    async def test_rename_nonexistent_returns_404(self, tmp_path):
        state = _make_state(tmp_path)
        state.rename_cron_folder = MagicMock(return_value=None)
        request = _request(state, body={"name": "X"}, match_info={"folder_id": "nope"})
        resp = await api_cron_folders_update(request)
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_rename_save_failure_returns_500_and_rolls_back(self, tmp_path):
        """When persistence fails on rename, returns 500."""
        state = _make_state(tmp_path)
        state._cron_folders = [{"id": "f1", "name": "Original", "order": 0}]
        state.rename_cron_folder = MagicMock(side_effect=OSError("permission denied"))
        request = _request(state, body={"name": "New"}, match_info={"folder_id": "f1"})
        resp = await api_cron_folders_update(request)
        assert resp.status == 500
        body = json.loads(resp.body)
        assert body["code"] == "folder_save_failed"


class TestCronFoldersDelete:
    """DELETE /api/cron-folders/{folder_id} removes folder and clears assignments."""

    @pytest.mark.asyncio
    async def test_delete_folder(self, tmp_path):
        state = _make_state(tmp_path)
        state._cron_folders = [{"id": "f1", "name": "Kill", "order": 0}]
        state.delete_cron_folder = MagicMock(return_value=True)
        request = _request(state, match_info={"folder_id": "f1"})
        resp = await api_cron_folders_delete(request)
        assert resp.status == 200
        state.delete_cron_folder.assert_called_once_with("f1")

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_404(self, tmp_path):
        state = _make_state(tmp_path)
        state.delete_cron_folder = MagicMock(return_value=False)
        request = _request(state, match_info={"folder_id": "nope"})
        resp = await api_cron_folders_delete(request)
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_delete_clears_folder_id_via_state_method(self, tmp_path):
        """Deleting a folder delegates to state.delete_cron_folder which clears assignments."""
        state = _make_state(tmp_path)
        state.delete_cron_folder = MagicMock(return_value=True)
        request = _request(state, match_info={"folder_id": "f2"})
        resp = await api_cron_folders_delete(request)
        assert resp.status == 200
        # Verify it went through the state method (which handles clearing)
        state.delete_cron_folder.assert_called_once_with("f2")

    @pytest.mark.asyncio
    async def test_delete_save_failure_returns_500(self, tmp_path):
        """When persistence fails on delete, returns 500 with code."""
        state = _make_state(tmp_path)
        state.delete_cron_folder = MagicMock(side_effect=OSError("disk full"))
        request = _request(state, match_info={"folder_id": "f1"})
        resp = await api_cron_folders_delete(request)
        assert resp.status == 500
        body = json.loads(resp.body)
        assert body["code"] == "folder_save_failed"


class TestCronFolderDeleteStateMethod:
    """DashboardState.delete_cron_folder atomically removes folder + clears jobs."""

    def test_delete_removes_folder_and_clears_jobs(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path, raising=False)
        state = DashboardState.__new__(DashboardState)
        state._cron_folders = [
            {"id": "f1", "name": "Ops", "order": 0},
            {"id": "f2", "name": "Keep", "order": 1},
        ]

        # Mock crons service
        job_in_folder = MagicMock()
        job_in_folder.id = "job1"
        job_in_folder.folder_id = "f1"
        job_not_in_folder = MagicMock()
        job_not_in_folder.id = "job2"
        job_not_in_folder.folder_id = ""

        state.crons = MagicMock()
        state.crons.list_jobs = MagicMock(return_value=[job_in_folder, job_not_in_folder])
        state.crons.update_job = MagicMock()

        result = state.delete_cron_folder("f1")
        assert result is True
        assert len(state._cron_folders) == 1
        assert state._cron_folders[0]["id"] == "f2"
        state.crons.update_job.assert_called_once_with("job1", folder_id="")

    def test_delete_completes_when_assignment_clear_fails(self, tmp_path, monkeypatch):
        """A job clear failure does NOT abort deletion: the folder removal is
        the authoritative write; a leftover folder_id is benign (renders as
        ungrouped) so the delete still succeeds."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path, raising=False)
        state = DashboardState.__new__(DashboardState)
        state._cron_folders = [{"id": "f1", "name": "Ops", "order": 0}]
        state.save_cron_folders()

        job = MagicMock()
        job.id = "job1"
        job.folder_id = "f1"
        state.crons = MagicMock()
        state.crons.list_jobs = MagicMock(return_value=[job])
        state.crons.update_job = MagicMock(side_effect=RuntimeError("store busy"))

        assert state.delete_cron_folder("f1") is True
        # Folder is gone from memory and disk despite the failed clear
        assert not any(f["id"] == "f1" for f in state._cron_folders)
        assert json.loads((tmp_path / state._CRON_FOLDERS_FILE).read_text()) == []

    def test_delete_restores_memory_when_save_fails(self, tmp_path, monkeypatch):
        """A persistence failure during delete rolls back the in-memory list.
        Job assignments are untouched — clears only happen after a
        successful save, so there is nothing to restore."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path, raising=False)
        state = DashboardState.__new__(DashboardState)
        state._cron_folders = [{"id": "f1", "name": "Ops", "order": 0}]

        job_in_folder = MagicMock()
        job_in_folder.id = "job1"
        job_in_folder.folder_id = "f1"

        state.crons = MagicMock()
        state.crons.list_jobs = MagicMock(return_value=[job_in_folder])
        state.crons.update_job = MagicMock()

        monkeypatch.setattr(state, "save_cron_folders", MagicMock(side_effect=OSError("disk full")))
        with pytest.raises(OSError):
            state.delete_cron_folder("f1")
        # In-memory list restored — memory stays consistent with disk
        assert any(f["id"] == "f1" for f in state._cron_folders)
        # No job writes happened: the folder still exists, jobs stay grouped
        state.crons.update_job.assert_not_called()

    def test_delete_nonexistent_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path, raising=False)
        state = DashboardState.__new__(DashboardState)
        state._cron_folders = [{"id": "f1", "name": "Ops", "order": 0}]
        state.crons = MagicMock()
        result = state.delete_cron_folder("nonexistent")
        assert result is False


class TestCronFoldersAsyncPersistence:
    """Verify mutations go through asyncio.to_thread (event-loop non-blocking)."""

    @pytest.mark.asyncio
    async def test_create_calls_state_method_via_to_thread(self, tmp_path):
        """Create handler delegates to state.create_cron_folder via asyncio.to_thread."""
        state = _make_state(tmp_path)
        request = _request(state, body={"name": "ThreadTest"})
        with patch(
            "kiro_crew.dashboard.handlers.cron.asyncio.to_thread", new_callable=AsyncMock
        ) as mock_to_thread:
            mock_to_thread.return_value = {"id": "x", "name": "ThreadTest", "order": 0}
            resp = await api_cron_folders_create(request)
            assert resp.status == 200
            mock_to_thread.assert_called_once()
            args = mock_to_thread.call_args[0]
            assert args[0] == state.create_cron_folder

    @pytest.mark.asyncio
    async def test_update_calls_state_method_via_to_thread(self, tmp_path):
        """Update handler delegates to state.rename_cron_folder via asyncio.to_thread."""
        state = _make_state(tmp_path)
        state._cron_folders = [{"id": "f1", "name": "Old", "order": 0}]
        request = _request(state, body={"name": "New"}, match_info={"folder_id": "f1"})
        with patch(
            "kiro_crew.dashboard.handlers.cron.asyncio.to_thread", new_callable=AsyncMock
        ) as mock_to_thread:
            mock_to_thread.return_value = {"id": "f1", "name": "New", "order": 0}
            resp = await api_cron_folders_update(request)
            assert resp.status == 200
            mock_to_thread.assert_called_once()
            args = mock_to_thread.call_args[0]
            assert args[0] == state.rename_cron_folder

    @pytest.mark.asyncio
    async def test_delete_calls_delete_via_to_thread(self, tmp_path):
        """Delete handler delegates to state.delete_cron_folder via asyncio.to_thread."""
        state = _make_state(tmp_path)
        request = _request(state, match_info={"folder_id": "f1"})
        with patch(
            "kiro_crew.dashboard.handlers.cron.asyncio.to_thread", new_callable=AsyncMock
        ) as mock_to_thread:
            mock_to_thread.return_value = True
            resp = await api_cron_folders_delete(request)
            assert resp.status == 200
            mock_to_thread.assert_called_once_with(state.delete_cron_folder, "f1")


class TestCronFoldersPersistence:
    """save_cron_folders -> load_cron_folders round-trips through the real
    DashboardState file store. Guards the startup wiring: the gateway must
    load persisted folders on boot (server.py calls load_cron_folders()
    alongside load_folders()), otherwise folders silently vanish across
    restarts even though the file exists on disk."""

    def test_save_then_load_round_trip(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path, raising=False)
        state = DashboardState.__new__(DashboardState)
        state._cron_folders = [
            {"id": "abc123", "name": "Monitoring", "order": 0},
            {"id": "def456", "name": "Digests", "order": 1},
        ]
        state.save_cron_folders()

        fresh = DashboardState.__new__(DashboardState)
        fresh._cron_folders = []
        fresh.load_cron_folders()
        assert fresh._cron_folders == state._cron_folders

    def test_load_ignores_non_array_json(self, tmp_path, monkeypatch):
        """A hand-edited/corrupt `{}` (valid JSON, wrong shape) must not be
        assigned — it would flow to the frontend and crash grouping."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path, raising=False)
        for bad in ("{}", '"folders"', "42", "null"):
            (tmp_path / "cron_folders.json").write_text(bad, encoding="utf-8")
            fresh = DashboardState.__new__(DashboardState)
            fresh._cron_folders = []
            fresh.load_cron_folders()
            assert fresh._cron_folders == [], f"shape {bad!r} should be ignored"

    def test_load_drops_malformed_entries(self, tmp_path, monkeypatch):
        """Non-dict entries and entries with a missing/invalid id, name, or
        order are dropped; valid entries survive. A non-string ``name`` would
        render as a React child and crash the Schedule page."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path, raising=False)
        (tmp_path / "cron_folders.json").write_text(
            json.dumps(
                [
                    {"id": "good1", "name": "Keep", "order": 0},
                    "not-a-dict",
                    {"name": "no id", "order": 1},
                    {"id": 42, "name": "non-string id", "order": 1},
                    {"id": "", "name": "empty id", "order": 1},
                    {"id": "bad-name", "name": {}, "order": 1},
                    {"id": "no-name", "order": 1},
                    {"id": "empty-name", "name": "", "order": 1},
                    {"id": "bad-order", "name": "X", "order": "first"},
                    {"id": "bool-order", "name": "X", "order": True},
                    {"id": "no-order", "name": "X"},
                    {"id": "good2", "name": "Also keep", "order": 1.5},
                ]
            ),
            encoding="utf-8",
        )
        fresh = DashboardState.__new__(DashboardState)
        fresh._cron_folders = []
        fresh.load_cron_folders()
        assert [f["id"] for f in fresh._cron_folders] == ["good1", "good2"]

    def test_save_raises_on_write_failure(self, tmp_path, monkeypatch):
        """save_cron_folders propagates I/O errors (not swallowed)."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path, raising=False)
        state = DashboardState.__new__(DashboardState)
        state._cron_folders = [{"id": "x", "name": "Y", "order": 0}]
        # Inject a write failure at the persistence primitive. (A chmod-based
        # read-only dir is not portable: chmod is a no-op on Windows, and the
        # permissive restore mode trips SAST.)

        def _boom(self, path, data):
            raise OSError("disk full")

        monkeypatch.setattr(DashboardState, "_atomic_write_json_strict", _boom, raising=True)
        with pytest.raises(OSError):
            state.save_cron_folders()

    def test_startup_wiring_calls_load_cron_folders(self):
        # The two gateway startup paths call load_folders(); each must also
        # call load_cron_folders() immediately after.
        import inspect

        import kiro_crew.dashboard.server as server_mod

        src = inspect.getsource(server_mod)
        assert src.count("await asyncio.to_thread(state.load_cron_folders)") >= 2


class TestCronFoldersConcurrency:
    """Concurrent folder creates must both persist (no last-writer-wins loss)."""

    @pytest.mark.asyncio
    async def test_concurrent_creates_both_persisted(self, tmp_path, monkeypatch):
        """Two concurrent create requests serialize via _cron_folders_lock.

        Both folders must be present in-memory and on disk after both complete.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path, raising=False)
        # Use a real DashboardState with real persistence
        state = DashboardState.__new__(DashboardState)
        state._cron_folders = []
        state.push_refresh = MagicMock()

        # Build mock requests
        req_a = MagicMock()
        req_a.app = {"state": state}
        req_a.json = AsyncMock(return_value={"name": "FolderA"})

        req_b = MagicMock()
        req_b.app = {"state": state}
        req_b.json = AsyncMock(return_value={"name": "FolderB"})

        # Fire both concurrently
        results = await asyncio.gather(
            api_cron_folders_create(req_a),
            api_cron_folders_create(req_b),
        )
        # Both should succeed
        assert results[0].status == 200
        assert results[1].status == 200
        # Both folders persisted in-memory
        assert len(state._cron_folders) == 2
        names = {f["name"] for f in state._cron_folders}
        assert names == {"FolderA", "FolderB"}
        # Both folders persisted on disk
        on_disk = json.loads((tmp_path / "cron_folders.json").read_text())
        assert len(on_disk) == 2
        disk_names = {f["name"] for f in on_disk}
        assert disk_names == {"FolderA", "FolderB"}

    @pytest.mark.asyncio
    async def test_concurrent_create_and_delete_serialize(self, tmp_path, monkeypatch):
        """A create and delete running concurrently don't corrupt state."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path, raising=False)
        state = DashboardState.__new__(DashboardState)
        state._cron_folders = [{"id": "existing", "name": "Existing", "order": 0}]
        state.push_refresh = MagicMock()
        state.crons = MagicMock()
        state.crons.list_jobs = MagicMock(return_value=[])
        state.save_cron_folders()  # persist initial state

        req_create = MagicMock()
        req_create.app = {"state": state}
        req_create.json = AsyncMock(return_value={"name": "NewFolder"})

        req_delete = MagicMock()
        req_delete.app = {"state": state}
        req_delete.match_info = {"folder_id": "existing"}

        results = await asyncio.gather(
            api_cron_folders_create(req_create),
            api_cron_folders_delete(req_delete),
        )
        # Both should succeed (order depends on lock acquisition)
        statuses = {r.status for r in results}
        assert 200 in statuses
        # After both complete: "existing" deleted, "NewFolder" remains
        assert len(state._cron_folders) == 1
        assert state._cron_folders[0]["name"] == "NewFolder"


class TestCronFolderDeleteOrdering:
    """delete_cron_folder clears job assignments BEFORE removing the folder."""

    def test_folder_removed_before_jobs_cleared(self, tmp_path, monkeypatch):
        """Verify that save_cron_folders persists the folder removal BEFORE
        update_job(folder_id='') clears assignments.

        The folder removal is the single authoritative write: a crash
        between the two leaves only dangling folder_ids, which are benign
        (grouping renders unknown ids as ungrouped). The reverse order
        could durably ungroup jobs for a delete that then fails.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path, raising=False)
        state = DashboardState.__new__(DashboardState)
        state._cron_folders = [{"id": "f1", "name": "Doomed", "order": 0}]

        call_order = []

        job = MagicMock()
        job.id = "job1"
        job.folder_id = "f1"
        state.crons = MagicMock()
        state.crons.list_jobs = MagicMock(return_value=[job])

        def track_update_job(*args, **kwargs):
            call_order.append("clear_job")

        state.crons.update_job = track_update_job

        original_save = DashboardState.save_cron_folders

        def track_save(self_):
            call_order.append("save_folders")
            original_save(self_)

        monkeypatch.setattr(DashboardState, "save_cron_folders", track_save)

        result = state.delete_cron_folder("f1")
        assert result is True
        assert call_order == ["save_folders", "clear_job"]
        # Folder actually removed
        assert len(state._cron_folders) == 0
