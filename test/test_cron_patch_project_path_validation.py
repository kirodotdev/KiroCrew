"""Tests for cron ``project_path`` validation on the dashboard PATCH surface.

``POST /api/crons`` validates ``project_path`` via ``validate_string_field``
(type check + sanitize + length cap), matching every other cron string field.
``PATCH /api/crons/{id}`` must route ``project_path`` through the same
validation instead of copying the raw body value straight to
``kwargs["project_path"]`` with only a truthiness check and an unguarded
``.strip()`` — a non-string JSON value (array/object/number) would otherwise
raise ``AttributeError`` inside the handler and surface as an HTTP 500 instead
of a clean 400. Same surface-divergence defect class as ``name``/``message``
(see ``test_cron_patch_name_validation.py``, ``test_cron_message_cap.py``).

Locks in that PATCH now routes ``project_path`` through the same validator as
POST, so the two REST surfaces cannot diverge and a malformed body never
reaches ``CronService.update_job_async``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from body_stream_helpers import attach_body

from kiro_crew.cron import CronService
from kiro_crew.dashboard.handlers import api_cron_update
from kiro_crew.validation import MAX_SHORT_STRING

OVERSIZE_PATH = "/" + "x" * MAX_SHORT_STRING


@pytest.fixture(autouse=True)
def _isolate_cron_store(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.cron._DEFAULT_DIR", tmp_path)
    yield


def _create_request(body: dict, crons: CronService) -> MagicMock:
    state = MagicMock()
    state.crons = crons
    request = MagicMock()
    request.app = {"state": state}
    attach_body(request, body)
    return request


def _update_request(body: dict, crons: CronService, job_id: str) -> MagicMock:
    request = _create_request(body, crons)
    request.match_info = {"job_id": job_id}
    return request


class TestDashboardUpdateProjectPath:
    @pytest.mark.asyncio
    async def test_patch_accepts_valid_path(self, tmp_path):
        # project_path requires owner authorization (see
        # test_cron_project_path_owner_gate.py) -- simulate an owner request
        # so this test continues to exercise the validator, not the gate.
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="n", message="m", every_secs=3600)
        with patch(
            "kiro_crew.dashboard.handlers.cron.is_owner_dashboard_request",
            lambda request: True,
        ):
            resp = await api_cron_update(
                _update_request({"project_path": str(tmp_path)}, crons, job.id)
            )
        assert resp.status == 200
        assert crons.list_jobs()[0].project_path == str(tmp_path)

    @pytest.mark.asyncio
    async def test_patch_rejects_path_beyond_cap(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="n", message="m", every_secs=3600)
        with patch(
            "kiro_crew.dashboard.handlers.cron.is_owner_dashboard_request",
            lambda request: True,
        ):
            resp = await api_cron_update(
                _update_request({"project_path": OVERSIZE_PATH}, crons, job.id)
            )
        assert resp.status == 400
        assert b"invalid_project_path" in resp.body
        assert crons.list_jobs()[0].project_path == ""

    @pytest.mark.asyncio
    async def test_patch_rejects_non_string_path(self, tmp_path):
        # An array/object/number JSON value must 400, not raise AttributeError
        # on .strip() and leak an HTTP 500.
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="n", message="m", every_secs=3600)
        resp = await api_cron_update(_update_request({"project_path": [1, 2]}, crons, job.id))
        assert resp.status == 400
        assert b"invalid_project_path" in resp.body
        assert crons.list_jobs()[0].project_path == ""

    @pytest.mark.asyncio
    async def test_patch_rejects_falsy_non_string_path(self, tmp_path):
        """A falsy non-string (0) must 400, not silently no-op with a 200."""
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="n", message="m", every_secs=3600)
        resp = await api_cron_update(_update_request({"project_path": 0}, crons, job.id))
        assert resp.status == 400
        assert b"invalid_project_path" in resp.body
        assert crons.list_jobs()[0].project_path == ""

    @pytest.mark.asyncio
    async def test_patch_clears_path_with_empty_string(self, tmp_path):
        """An explicit empty string is a valid update that clears an existing
        binding back to global-agent-only -- distinct from the field being
        absent from the body entirely. Clearing requires owner authorization
        (see test_cron_project_path_owner_gate.py), so simulate an owner
        request here to keep this test focused on the clearing semantics."""
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="n", message="m", every_secs=3600, project_path=str(tmp_path))
        with patch(
            "kiro_crew.dashboard.handlers.cron.is_owner_dashboard_request",
            lambda request: True,
        ):
            resp = await api_cron_update(_update_request({"project_path": ""}, crons, job.id))
        assert resp.status == 200
        assert crons.list_jobs()[0].project_path == ""
