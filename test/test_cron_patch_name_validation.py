"""Tests for cron ``name`` validation on the dashboard PATCH surface (issue #3831).

``POST /api/crons`` caps ``name`` at ``MAX_SHORT_STRING`` via
``validate_string_field``, but ``PATCH /api/crons/{id}`` previously copied the
raw body value straight to ``job.name`` with only a truthiness check — a
non-string or oversize name was persisted verbatim into ``crons.json``. This
is the same surface-divergence defect class fixed for ``message`` in #3829.

Locks in that PATCH now routes ``name`` through the same validator as POST:
type check + ``sanitize_string`` + length cap, so the two REST surfaces cannot
diverge.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from body_stream_helpers import attach_body

from kiro_crew.cron import CronService
from kiro_crew.dashboard.handlers import api_cron_update
from kiro_crew.validation import MAX_SHORT_STRING

OVERSIZE_NAME = "x" * (MAX_SHORT_STRING + 1)


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


class TestDashboardUpdateName:
    @pytest.mark.asyncio
    async def test_patch_accepts_valid_name(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="old", message="m", every_secs=3600)
        resp = await api_cron_update(_update_request({"name": "renamed"}, crons, job.id))
        assert resp.status == 200
        assert crons.list_jobs()[0].name == "renamed"

    @pytest.mark.asyncio
    async def test_patch_rejects_name_beyond_cap(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="old", message="m", every_secs=3600)
        resp = await api_cron_update(_update_request({"name": OVERSIZE_NAME}, crons, job.id))
        assert resp.status == 400
        assert b"invalid_name" in resp.body
        assert crons.list_jobs()[0].name == "old"

    @pytest.mark.asyncio
    async def test_patch_rejects_non_string_name(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="old", message="m", every_secs=3600)
        resp = await api_cron_update(_update_request({"name": [1, 2]}, crons, job.id))
        assert resp.status == 400
        assert b"invalid_name" in resp.body
        assert crons.list_jobs()[0].name == "old"

    @pytest.mark.asyncio
    async def test_patch_rejects_falsy_non_string_name(self, tmp_path):
        """A falsy non-string (0) must 400, not silently no-op with a 200."""
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="old", message="m", every_secs=3600)
        resp = await api_cron_update(_update_request({"name": 0}, crons, job.id))
        assert resp.status == 400
        assert b"invalid_name" in resp.body
        assert crons.list_jobs()[0].name == "old"

    @pytest.mark.asyncio
    async def test_patch_sanitizes_name_like_post(self, tmp_path):
        """PATCH routes through the same sanitizer as POST: hidden unicode
        (zero-width space) is stripped before persistence, matching create."""
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="old", message="m", every_secs=3600)
        resp = await api_cron_update(_update_request({"name": "new\u200bname"}, crons, job.id))
        assert resp.status == 200
        assert crons.list_jobs()[0].name == "newname"
