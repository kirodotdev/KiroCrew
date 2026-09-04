"""Tests for the cron folder CRUD endpoints.

Covers GET/POST/PATCH/DELETE /api/cron-folders, including the contract that
deleting a folder clears folder_id on any assigned cron jobs (never deletes them).
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from body_stream_helpers import BodyStreamPayload

from kiro_crew.cron import CronService
from kiro_crew.dashboard.handlers.cron import (
    api_cron_folders,
    api_cron_folders_create,
    api_cron_folders_delete,
    api_cron_folders_update,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.validation import MAX_SHORT_STRING


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
    raw = json.dumps(body).encode() if body is not None else b""
    request.content = BodyStreamPayload(raw)
    request.content_length = len(raw) or None
    request.charset = None
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

    @pytest.mark.asyncio
    async def test_list_serializes_a_snapshot_not_the_live_dicts(self, tmp_path):
        """The GET must hand the encoder a copy, so a concurrent rename that
        mutates a folder dict's name in place cannot tear the read."""
        state = _make_state(tmp_path)
        live = {"id": "a1", "name": "Ops", "order": 0}
        state._cron_folders = [live]
        request = _request(state)

        captured = {}

        def _capture(payload, **kw):
            captured["payload"] = payload
            return MagicMock(status=200)

        with patch("kiro_crew.dashboard.handlers.cron.web.json_response", side_effect=_capture):
            await api_cron_folders(request)

        payload = captured["payload"]
        # The response list is a fresh object, not the live list...
        assert payload is not state._cron_folders
        # ...and each entry is a copy, not the live dict a rename mutates.
        assert payload[0] is not live
        assert payload[0] == {"id": "a1", "name": "Ops", "order": 0}
        # Mutating the returned snapshot must not touch server state.
        payload[0]["name"] = "Renamed"
        payload.append({"id": "b2", "name": "Injected", "order": 1})
        assert live["name"] == "Ops"
        assert state._cron_folders == [live]


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
    async def test_rename_rejects_oversized_folder_id(self, tmp_path):
        """An over-long URL path folder_id is rejected with 400 before any
        lock/thread/state work — parity with the body-param guard on the job
        routes."""
        state = _make_state(tmp_path)
        state.rename_cron_folder = MagicMock(return_value=None)
        request = _request(
            state,
            body={"name": "X"},
            match_info={"folder_id": "a" * (MAX_SHORT_STRING + 1)},
        )
        resp = await api_cron_folders_update(request)
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "invalid_folder_id"
        state.rename_cron_folder.assert_not_called()

    @pytest.mark.asyncio
    async def test_rename_rejects_empty_folder_id(self, tmp_path):
        state = _make_state(tmp_path)
        state.rename_cron_folder = MagicMock(return_value=None)
        request = _request(state, body={"name": "X"}, match_info={"folder_id": ""})
        resp = await api_cron_folders_update(request)
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "invalid_folder_id"
        state.rename_cron_folder.assert_not_called()

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
    async def test_delete_rejects_oversized_folder_id(self, tmp_path):
        """An over-long URL path folder_id is rejected with 400 before any
        lock/thread/state work — parity with the body-param guard."""
        state = _make_state(tmp_path)
        state.delete_cron_folder = MagicMock(return_value=False)
        request = _request(state, match_info={"folder_id": "a" * (MAX_SHORT_STRING + 1)})
        resp = await api_cron_folders_delete(request)
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "invalid_folder_id"
        state.delete_cron_folder.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_rejects_empty_folder_id(self, tmp_path):
        state = _make_state(tmp_path)
        state.delete_cron_folder = MagicMock(return_value=False)
        request = _request(state, match_info={"folder_id": ""})
        resp = await api_cron_folders_delete(request)
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "invalid_folder_id"
        state.delete_cron_folder.assert_not_called()

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


class TestCronFolderCreateStateMethod:
    """DashboardState.create_cron_folder guards against a duplicate folder id."""

    def test_create_rejects_duplicate_id(self, tmp_path, monkeypatch):
        """A colliding folder_id is refused: rename/delete act on the first id
        match, so a duplicate would strand the shadowed folder as
        un-renameable/un-deletable. The guard keeps ids unique."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path, raising=False)
        state = DashboardState.__new__(DashboardState)
        state._cron_folders = [{"id": "dup", "name": "Ops", "order": 0}]
        state.save_cron_folders()

        with pytest.raises(ValueError, match="collision"):
            state.create_cron_folder("Second", "dup")
        # No shadow folder appended; the store is untouched
        assert len(state._cron_folders) == 1
        assert json.loads((tmp_path / state._CRON_FOLDERS_FILE).read_text()) == [
            {"id": "dup", "name": "Ops", "order": 0}
        ]

    def test_create_appends_unique_id(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path, raising=False)
        state = DashboardState.__new__(DashboardState)
        state._cron_folders = [{"id": "f1", "name": "Ops", "order": 0}]

        folder = state.create_cron_folder("Keep", "f2")
        assert folder == {"id": "f2", "name": "Keep", "order": 1}
        assert [f["id"] for f in state._cron_folders] == ["f1", "f2"]


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

    def test_load_keeps_malformed_entries_inactive(self, tmp_path, monkeypatch):
        """Non-dict entries and entries with a missing/invalid id, name, or
        order are excluded from the ACTIVE folder list (a non-string ``name``
        would render as a React child and crash the Schedule page) but are
        preserved verbatim in ``_unparsed_cron_folder_entries`` so a later save
        does not erase them."""
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
        fresh._unparsed_cron_folder_entries = []
        fresh.load_cron_folders()
        # Only well-formed entries are active.
        assert [f["id"] for f in fresh._cron_folders] == ["good1", "good2"]
        # Every malformed entry is preserved verbatim (10 of the 12 above).
        assert len(fresh._unparsed_cron_folder_entries) == 10
        assert "not-a-dict" in fresh._unparsed_cron_folder_entries

    def test_malformed_entry_survives_a_subsequent_save(self, tmp_path, monkeypatch):
        """Regression: a hand-edited file with a typo'd entry must NOT lose that
        entry when an unrelated folder operation triggers a save. Previously the
        malformed entry was dropped in-memory and the next save erased its bytes.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path, raising=False)
        path = tmp_path / "cron_folders.json"
        # User hand-edits the file and typos "order" as "oder" on one folder.
        path.write_text(
            json.dumps(
                [
                    {"id": "aaa", "name": "Backups", "order": 0},
                    {"id": "bbb", "name": "Reports", "oder": 1},  # malformed
                    {"id": "ccc", "name": "Alerts", "order": 2},
                ]
            ),
            encoding="utf-8",
        )
        state = DashboardState.__new__(DashboardState)
        state._cron_folders = []
        state._unparsed_cron_folder_entries = []
        state.load_cron_folders()
        assert [f["id"] for f in state._cron_folders] == ["aaa", "ccc"]

        # An unrelated folder operation persists — the malformed entry must ride along.
        state.create_cron_folder("NewFolder", "ddd")

        on_disk = json.loads(path.read_text(encoding="utf-8"))
        ids_and_raw = [f.get("id") if isinstance(f, dict) else f for f in on_disk]
        # The typo'd "Reports" folder is still on disk, not erased.
        assert "bbb" in ids_and_raw
        malformed = next(f for f in on_disk if isinstance(f, dict) and f.get("id") == "bbb")
        assert malformed == {"id": "bbb", "name": "Reports", "oder": 1}
        # And the valid + newly created folders are all present.
        assert {"aaa", "ccc", "ddd"}.issubset(
            {f["id"] for f in on_disk if isinstance(f, dict) and "order" in f}
        )

    def test_no_unparsed_entries_leaves_payload_clean(self, tmp_path, monkeypatch):
        """When nothing was malformed, the persisted file is exactly the active
        folder list — no empty/sentinel padding leaks in."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path, raising=False)
        path = tmp_path / "cron_folders.json"
        path.write_text(
            json.dumps([{"id": "aaa", "name": "Backups", "order": 0}]), encoding="utf-8"
        )
        state = DashboardState.__new__(DashboardState)
        state._cron_folders = []
        state._unparsed_cron_folder_entries = []
        state.load_cron_folders()
        assert state._unparsed_cron_folder_entries == []
        state.create_cron_folder("NewFolder", "ddd")
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert [f["id"] for f in on_disk] == ["aaa", "ddd"]

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

    def test_create_does_not_mutate_live_list_before_save_succeeds(self, tmp_path, monkeypatch):
        """Ghost-folder regression that distinguishes persist-first from the old
        append-then-save-then-pop: capture the LIVE ``_cron_folders`` at the
        moment the persist runs. The old code had already appended the new
        folder to the live list by then (a concurrent GET would see the ghost);
        the fix persists a candidate and leaves the live list untouched until
        the save returns, so the live list must NOT contain the folder at
        persist time. Reverting the fix makes this assertion fail.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path, raising=False)
        state = DashboardState.__new__(DashboardState)
        state._cron_folders = [{"id": "existing", "name": "Keep", "order": 0}]
        live_at_persist: list[bool] = []

        def _observe(path, data):
            # Read the LIVE attribute (not the data being written): the fix must
            # not have committed the new folder to _cron_folders yet.
            live_at_persist.append(any(f["id"] == "newid" for f in state._cron_folders))

        monkeypatch.setattr(state, "_persist_cron_folders", lambda folders: _observe(None, folders))
        state.create_cron_folder("New", "newid")
        # At persist time the live list still held only the pre-existing folder.
        assert live_at_persist == [False]
        # After a successful create the folder is committed to the live list.
        assert [f["id"] for f in state._cron_folders] == ["existing", "newid"]

    def test_create_leaves_live_list_unchanged_on_save_failure(self, tmp_path, monkeypatch):
        """A failed create must leave ``_cron_folders`` exactly as it was — the
        new folder is never exposed."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path, raising=False)
        state = DashboardState.__new__(DashboardState)
        state._cron_folders = [{"id": "existing", "name": "Keep", "order": 0}]
        before = list(state._cron_folders)

        def _boom(self, path, data):
            raise OSError("disk full")

        monkeypatch.setattr(DashboardState, "_atomic_write_json_strict", _boom, raising=True)
        with pytest.raises(OSError):
            state.create_cron_folder("New", "newid")
        assert state._cron_folders == before

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

        # Build mock requests (real body bytes: the handler streams request.content)
        req_a = _request(state, body={"name": "FolderA"})
        req_b = _request(state, body={"name": "FolderB"})

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

        req_create = _request(state, body={"name": "NewFolder"})

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
