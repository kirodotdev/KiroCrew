"""Tests for the cron-message character cap (issue #1682).

A cron job's message is a task prompt, so it gets its own generous cap
(``MAX_CRON_MESSAGE``) instead of borrowing the shared ``MAX_MEDIUM_STRING``
(5000). Locks in:

- messages longer than the old 5000-char limit are accepted on EVERY surface
  (MCP schemas, dashboard POST/PATCH, CronService chokepoint) and round-trip
  through create + disk reload intact;
- the new cap is still enforced (oversize is rejected, not unbounded);
- other fields sharing ``MAX_MEDIUM_STRING`` still reject at their old limit
  (the shared constant was NOT raised).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from body_stream_helpers import attach_body

from kiro_crew.cron import CronService
from kiro_crew.dashboard.handlers import api_cron_update, api_crons_create
from kiro_crew.validation import (
    CRON_ADD_SCHEMA,
    MAX_CRON_MESSAGE,
    MAX_MEDIUM_STRING,
    MCP_CRON_SCHEMAS,
    SPAWN_STEER_SCHEMA,
    ValidationError,
    validate_tool_args,
)

# Longer than the old shared cap, comfortably under the new cron cap.
# No edge whitespace: validate_string_field/sanitize_string strip edges.
LONG_MSG = ("task prompt " * 500).strip()  # 5999 chars
assert MAX_MEDIUM_STRING < len(LONG_MSG) <= MAX_CRON_MESSAGE

OVERSIZE_MSG = "x" * (MAX_CRON_MESSAGE + 1)


@pytest.fixture(autouse=True)
def _isolate_cron_store(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.cron._DEFAULT_DIR", tmp_path)
    yield


def test_cap_is_actually_more_generous():
    assert MAX_CRON_MESSAGE > MAX_MEDIUM_STRING


# ── MCP schema surface ──


class TestMcpSchemas:
    def test_cron_add_accepts_message_beyond_old_cap(self):
        cleaned = validate_tool_args(
            {"name": "big", "message": LONG_MSG, "every": 3600}, CRON_ADD_SCHEMA
        )
        assert cleaned["message"] == LONG_MSG

    def test_cron_add_rejects_message_beyond_new_cap(self):
        with pytest.raises(ValidationError):
            validate_tool_args(
                {"name": "big", "message": OVERSIZE_MSG, "every": 3600}, CRON_ADD_SCHEMA
            )

    def test_cron_update_accepts_message_beyond_old_cap(self):
        cleaned = validate_tool_args(
            {"job_id": "abc123", "message": LONG_MSG}, MCP_CRON_SCHEMAS["cron_update"]
        )
        assert cleaned["message"] == LONG_MSG

    def test_cron_update_rejects_message_beyond_new_cap(self):
        with pytest.raises(ValidationError):
            validate_tool_args(
                {"job_id": "abc123", "message": OVERSIZE_MSG},
                MCP_CRON_SCHEMAS["cron_update"],
            )

    def test_other_medium_string_fields_still_reject_at_old_limit(self):
        """Control: the shared MAX_MEDIUM_STRING cap was not raised globally."""
        with pytest.raises(ValidationError):
            validate_tool_args(
                {"agent_id": "a1", "message": "y" * (MAX_MEDIUM_STRING + 1)},
                SPAWN_STEER_SCHEMA,
            )


# ── CronService chokepoint (the path the CLI and apps SDK use) ──


class TestServiceChokepoint:
    def test_add_job_roundtrips_long_message(self, tmp_path):
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="big", message=LONG_MSG, every_secs=3600)
        assert job.message == LONG_MSG
        # Round-trip: a fresh service instance reloads it from disk intact.
        reloaded = CronService(base_dir=tmp_path)
        jobs = reloaded.list_jobs()
        assert len(jobs) == 1
        assert jobs[0].message == LONG_MSG

    def test_add_job_rejects_oversize_message(self, tmp_path):
        svc = CronService(base_dir=tmp_path)
        with pytest.raises(ValueError, match="max length"):
            svc.add_job(name="big", message=OVERSIZE_MSG, every_secs=3600)
        assert svc.list_jobs() == []

    def test_add_job_accepts_message_at_exact_cap(self, tmp_path):
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="edge", message="x" * MAX_CRON_MESSAGE, every_secs=3600)
        assert len(job.message) == MAX_CRON_MESSAGE

    def test_add_job_rejects_non_string_message(self, tmp_path):
        svc = CronService(base_dir=tmp_path)
        with pytest.raises(ValueError, match="must be a string"):
            svc.add_job(name="bad", message=["not", "a", "str"], every_secs=3600)  # type: ignore[arg-type]
        assert svc.list_jobs() == []

    def test_update_job_rejects_non_string_message(self, tmp_path):
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="j", message="short", every_secs=3600)
        with pytest.raises(ValueError, match="must be a string"):
            svc.update_job(job.id, message=["not", "a", "str"])
        assert svc.list_jobs()[0].message == "short"

    def test_update_job_accepts_long_message(self, tmp_path):
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="j", message="short", every_secs=3600)
        updated = svc.update_job(job.id, message=LONG_MSG)
        assert updated is not None
        assert updated.message == LONG_MSG

    def test_update_job_rejects_oversize_and_leaves_job_unchanged(self, tmp_path):
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="j", message="short", every_secs=3600)
        with pytest.raises(ValueError, match="max length"):
            svc.update_job(job.id, message=OVERSIZE_MSG)
        assert svc.list_jobs()[0].message == "short"


# ── Dashboard REST surface ──


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


class TestDashboardCreate:
    @pytest.mark.asyncio
    async def test_post_accepts_message_beyond_old_cap(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        resp = await api_crons_create(
            _create_request({"name": "big", "message": LONG_MSG, "every": 3600}, crons)
        )
        assert resp.status == 200
        assert crons.list_jobs()[0].message == LONG_MSG

    @pytest.mark.asyncio
    async def test_post_rejects_message_beyond_new_cap(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        resp = await api_crons_create(
            _create_request({"name": "big", "message": OVERSIZE_MSG, "every": 3600}, crons)
        )
        assert resp.status == 400
        assert crons.list_jobs() == []


class TestDashboardUpdate:
    @pytest.mark.asyncio
    async def test_patch_accepts_message_beyond_old_cap(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="j", message="short", every_secs=3600)
        resp = await api_cron_update(_update_request({"message": LONG_MSG}, crons, job.id))
        assert resp.status == 200
        assert crons.list_jobs()[0].message == LONG_MSG

    @pytest.mark.asyncio
    async def test_patch_rejects_message_beyond_new_cap(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="j", message="short", every_secs=3600)
        resp = await api_cron_update(_update_request({"message": OVERSIZE_MSG}, crons, job.id))
        assert resp.status == 400
        assert crons.list_jobs()[0].message == "short"

    @pytest.mark.asyncio
    async def test_patch_rejects_non_string_message(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="j", message="short", every_secs=3600)
        resp = await api_cron_update(_update_request({"message": [1, 2]}, crons, job.id))
        assert resp.status == 400
        assert crons.list_jobs()[0].message == "short"

    @pytest.mark.asyncio
    async def test_patch_rejects_falsy_non_string_message(self, tmp_path):
        """A falsy non-string (0) must 400, not silently no-op with a 200."""
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="j", message="short", every_secs=3600)
        resp = await api_cron_update(_update_request({"message": 0}, crons, job.id))
        assert resp.status == 400
        assert b"invalid_message" in resp.body
        assert crons.list_jobs()[0].message == "short"

    @pytest.mark.asyncio
    async def test_patch_sanitizes_message_like_post(self, tmp_path):
        """PATCH routes through the same sanitizer as POST: hidden unicode
        (zero-width space) is stripped before persistence, matching create."""
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="j", message="short", every_secs=3600)
        resp = await api_cron_update(
            _update_request({"message": "new\u200bplan"}, crons, job.id)
        )
        assert resp.status == 200
        assert crons.list_jobs()[0].message == "newplan"
