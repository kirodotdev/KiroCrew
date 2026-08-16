"""Tests for the cron-job ``name`` cap across every surface (issue #3831).

``POST /api/crons`` capped ``name`` at ``MAX_SHORT_STRING`` through
``validate_string_field``, but ``PATCH /api/crons/{id}`` copied the raw body
value into kwargs and ``_update_job_locked`` assigned it with only a truthiness
guard -- so a non-string or oversize name the create surface rejected was
persisted verbatim by the update surface. The same divergence existed one layer
down: ``_build_job`` capped ``message`` but not ``name``, leaving the non-REST
create paths (MCP ``cron_add``, the CLI, the apps SDK) uncapped too.

Locks in that every surface now agrees:

- the persistence owner (``_build_job`` / ``_update_job_locked``) rejects a
  non-string or oversize name, so the CLI/MCP/SDK paths are bound by it;
- both REST surfaces reject with 400 rather than persisting or 500-ing;
- a name at the exact cap is still accepted (the check is a cap, not an
  off-by-one rejection), and PATCH sanitizes like POST.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.cron import CronService
from kiro_crew.dashboard.handlers import api_cron_update, api_crons_create
from kiro_crew.validation import MAX_SHORT_STRING

OVERSIZE_NAME = "x" * (MAX_SHORT_STRING + 1)
EXACT_NAME = "x" * MAX_SHORT_STRING


@pytest.fixture(autouse=True)
def _isolate_cron_store(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.cron._DEFAULT_DIR", tmp_path)
    yield


# ── CronService chokepoint (the path the CLI, MCP and apps SDK use) ──


class TestServiceChokepoint:
    def test_add_job_rejects_oversize_name(self, tmp_path):
        svc = CronService(base_dir=tmp_path)
        with pytest.raises(ValueError, match="max length"):
            svc.add_job(name=OVERSIZE_NAME, message="m", every_secs=3600)
        assert svc.list_jobs() == []

    def test_add_job_rejects_non_string_name(self, tmp_path):
        svc = CronService(base_dir=tmp_path)
        with pytest.raises(ValueError, match="must be a string"):
            svc.add_job(name=["not", "a", "str"], message="m", every_secs=3600)  # type: ignore[arg-type]
        assert svc.list_jobs() == []

    def test_add_job_accepts_name_at_exact_cap(self, tmp_path):
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name=EXACT_NAME, message="m", every_secs=3600)
        assert len(job.name) == MAX_SHORT_STRING

    def test_update_job_rejects_oversize_and_leaves_job_unchanged(self, tmp_path):
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="j", message="m", every_secs=3600)
        with pytest.raises(ValueError, match="max length"):
            svc.update_job(job.id, name=OVERSIZE_NAME)
        assert svc.list_jobs()[0].name == "j"

    def test_update_job_rejects_non_string_name(self, tmp_path):
        """A list is truthy, so the old `if kwargs["name"]:` guard let it
        through and `job.name` became a list on disk."""
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="j", message="m", every_secs=3600)
        with pytest.raises(ValueError, match="must be a string"):
            svc.update_job(job.id, name=["not", "a", "str"])
        assert svc.list_jobs()[0].name == "j"

    def test_update_job_accepts_name_at_exact_cap(self, tmp_path):
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="j", message="m", every_secs=3600)
        updated = svc.update_job(job.id, name=EXACT_NAME)
        assert updated is not None
        assert updated.name == EXACT_NAME

    def test_rejected_update_does_not_reach_disk(self, tmp_path):
        """Validation runs before any mutation, so a reload sees the old name
        -- not a half-applied update that only looks right in memory."""
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="j", message="m", every_secs=3600)
        with pytest.raises(ValueError):
            svc.update_job(job.id, name=OVERSIZE_NAME)
        assert CronService(base_dir=tmp_path).list_jobs()[0].name == "j"


# ── Dashboard REST surface ──


def _create_request(body: dict, crons: CronService) -> MagicMock:
    state = MagicMock()
    state.crons = crons
    request = MagicMock()
    request.app = {"state": state}
    request.json = AsyncMock(return_value=body)
    return request


def _update_request(body: dict, crons: CronService, job_id: str) -> MagicMock:
    request = _create_request(body, crons)
    request.match_info = {"job_id": job_id}
    return request


class TestDashboardCreate:
    @pytest.mark.asyncio
    async def test_post_rejects_oversize_name(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        resp = await api_crons_create(
            _create_request({"name": OVERSIZE_NAME, "message": "m", "every": 3600}, crons)
        )
        assert resp.status == 400
        assert crons.list_jobs() == []


class TestDashboardUpdate:
    @pytest.mark.asyncio
    async def test_patch_rejects_oversize_name(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="j", message="m", every_secs=3600)
        resp = await api_cron_update(_update_request({"name": OVERSIZE_NAME}, crons, job.id))
        assert resp.status == 400
        assert b"invalid_name" in resp.body
        assert crons.list_jobs()[0].name == "j"

    @pytest.mark.asyncio
    async def test_patch_rejects_non_string_name(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="j", message="m", every_secs=3600)
        resp = await api_cron_update(_update_request({"name": [1, 2]}, crons, job.id))
        assert resp.status == 400
        assert crons.list_jobs()[0].name == "j"

    @pytest.mark.asyncio
    async def test_patch_rejects_falsy_non_string_name(self, tmp_path):
        """A falsy non-string (0) must 400, not silently no-op with a 200 --
        the truthiness guard at the mutation site would have skipped it."""
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="j", message="m", every_secs=3600)
        resp = await api_cron_update(_update_request({"name": 0}, crons, job.id))
        assert resp.status == 400
        assert b"invalid_name" in resp.body
        assert crons.list_jobs()[0].name == "j"

    @pytest.mark.asyncio
    async def test_patch_accepts_name_at_exact_cap(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="j", message="m", every_secs=3600)
        resp = await api_cron_update(_update_request({"name": EXACT_NAME}, crons, job.id))
        assert resp.status == 200
        assert crons.list_jobs()[0].name == EXACT_NAME

    @pytest.mark.asyncio
    async def test_patch_sanitizes_name_like_post(self, tmp_path):
        """PATCH routes through the same sanitizer as POST: hidden unicode
        (zero-width space) is stripped before persistence, matching create."""
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="j", message="m", every_secs=3600)
        resp = await api_cron_update(_update_request({"name": "new​name"}, crons, job.id))
        assert resp.status == 200
        assert crons.list_jobs()[0].name == "newname"

    @pytest.mark.asyncio
    async def test_patch_with_null_name_leaves_it_unchanged(self, tmp_path):
        """`null` sanitizes to "" and the mutation site skips empty names, so
        a PATCH cannot blank a job's name -- unchanged from before the fix."""
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="j", message="m", every_secs=3600)
        resp = await api_cron_update(
            _update_request({"name": None, "silent": True}, crons, job.id)
        )
        assert resp.status == 200
        assert crons.list_jobs()[0].name == "j"
