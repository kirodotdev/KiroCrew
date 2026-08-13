"""Tests for the dashboard cron HTTP handlers wiring `folder_id`.

Covers the create (`POST /api/crons`) and update (`PATCH /api/crons/{id}`) paths
that copy the request body `folder_id` field into the job — the exact path the
frontend folder assignment exercises. Mirrors test_dashboard_cron_hide_in_chat.py.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.cron import CronService
from kiro_crew.dashboard.handlers import api_cron_update, api_crons_create


@pytest.fixture(autouse=True)
def _isolate_cron_store(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.cron._DEFAULT_DIR", tmp_path)
    yield


def _create_request(body: dict, crons: CronService) -> MagicMock:
    state = MagicMock()
    state.crons = crons
    request = MagicMock()
    request.app = {"state": state}
    request.json = AsyncMock(return_value=body)
    return request


class TestCronCreateFolderId:
    """POST /api/crons must persist folder_id from the body onto the job."""

    @pytest.mark.asyncio
    async def test_create_with_folder_id_persists(self):
        crons = CronService()
        request = _create_request(
            {"name": "digest", "message": "summarize", "every": 86400, "folder_id": "abc123"},
            crons,
        )
        resp = await api_crons_create(request)
        assert resp.status == 200
        jobs = crons.list_jobs()
        assert len(jobs) == 1
        assert jobs[0].folder_id == "abc123"

    @pytest.mark.asyncio
    async def test_create_without_folder_id_defaults_empty(self):
        crons = CronService()
        request = _create_request(
            {"name": "chatty", "message": "talk", "every": 3600},
            crons,
        )
        resp = await api_crons_create(request)
        assert resp.status == 200
        assert crons.list_jobs()[0].folder_id == ""

    @pytest.mark.asyncio
    async def test_create_with_empty_folder_id(self):
        crons = CronService()
        request = _create_request(
            {"name": "shown", "message": "talk", "every": 3600, "folder_id": ""},
            crons,
        )
        resp = await api_crons_create(request)
        assert resp.status == 200
        assert crons.list_jobs()[0].folder_id == ""


class TestCronUpdateFolderId:
    """PATCH /api/crons/{id} must forward folder_id to update_job_async."""

    def _update_request(self, body: dict, job_id: str = "abc123") -> MagicMock:
        state = MagicMock()
        mock_job = MagicMock()
        mock_job.id = job_id
        state.crons.update_job_async = AsyncMock(return_value=mock_job)
        request = MagicMock()
        request.app = {"state": state}
        request.match_info = {"job_id": job_id}
        request.json = AsyncMock(return_value=body)
        return request

    @pytest.mark.asyncio
    async def test_update_forwards_folder_id(self):
        request = self._update_request({"folder_id": "myf01"})
        resp = await api_cron_update(request)
        assert resp.status == 200
        _, kwargs = request.app["state"].crons.update_job_async.call_args
        assert kwargs.get("folder_id") == "myf01"

    @pytest.mark.asyncio
    async def test_update_clears_folder_id(self):
        request = self._update_request({"folder_id": ""})
        resp = await api_cron_update(request)
        assert resp.status == 200
        _, kwargs = request.app["state"].crons.update_job_async.call_args
        assert kwargs.get("folder_id") == ""

    @pytest.mark.asyncio
    async def test_update_rejects_non_string_folder_id(self):
        """A non-string folder_id must 400, never reach the store — it would
        persist corrupted schema data (job then renders as ungrouped)."""
        for bad in ({"x": 1}, ["f1"], 42, True):
            request = self._update_request({"folder_id": bad})
            resp = await api_cron_update(request)
            assert resp.status == 400, f"folder_id={bad!r} should be rejected"
            assert json.loads(resp.body)["code"] == "invalid_folder_id"
            request.app["state"].crons.update_job_async.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_rejects_non_string_folder_id(self):
        """POST /api/crons shares the PATCH contract: non-string folder_id
        is a 400, never str()-coerced into the store."""
        crons = CronService()
        for bad in ({"x": 1}, ["f1"], 42, True):
            request = _create_request(
                {"name": "job", "message": "msg", "every": 3600, "folder_id": bad},
                crons,
            )
            resp = await api_crons_create(request)
            assert resp.status == 400, f"folder_id={bad!r} should be rejected"
            assert json.loads(resp.body)["code"] == "invalid_folder_id"

    @pytest.mark.asyncio
    async def test_update_coerces_null_folder_id_to_empty(self):
        request = self._update_request({"folder_id": None})
        resp = await api_cron_update(request)
        assert resp.status == 200
        _, kwargs = request.app["state"].crons.update_job_async.call_args
        assert kwargs.get("folder_id") == ""

    @pytest.mark.asyncio
    async def test_folder_id_appears_in_list_response(self):
        """folder_id must be included in the GET /api/crons response."""
        crons = CronService()
        request = _create_request(
            {"name": "job", "message": "msg", "every": 3600, "folder_id": "f99"},
            crons,
        )
        resp = await api_crons_create(request)
        assert resp.status == 200
        # Now call the list endpoint
        from kiro_crew.dashboard.handlers import api_crons

        list_request = MagicMock()
        state = MagicMock()
        state.crons = crons
        state.has_slot = MagicMock(return_value=False)
        list_request.app = {"state": state}
        resp = await api_crons(list_request)
        assert resp.status == 200
        import json

        body = json.loads(resp.body)
        assert body["jobs"][0]["folder_id"] == "f99"
