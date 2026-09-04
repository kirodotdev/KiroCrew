"""REST input-validation tests for /api/crons and /api/lessons POST handlers.

Pentest finding: these endpoints called string methods (``.strip()``) directly
on ``body.get(...)`` values, so a JSON array/dict/int in a string field raised
an unhandled AttributeError -> HTTP 500. The fix routes field extraction
through validation.py helpers (the same rules the MCP tool layer enforces), so
bad types now return a structured HTTP 400 instead of crashing. These tests
assert 400-not-500 for the exact reproduction payloads plus enum/length bounds.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from body_stream_helpers import attach_body

from kiro_crew.dashboard.handlers import api_crons_create, api_lessons_create


def _crons_request(body: object) -> MagicMock:
    mock_state = MagicMock()
    mock_job = MagicMock()
    mock_job.id = "job-1"
    mock_job.agent_id = ""
    mock_state.crons.add_job_async = AsyncMock(return_value=mock_job)
    request = MagicMock()
    request.app = {"state": mock_state}
    # api_crons_create reads through read_bounded_json's capped path, which
    # drains request.content -- feed real bytes, not a mocked json.
    attach_body(request, body)
    return request


def _lessons_request(body: object) -> MagicMock:
    """Build a /api/lessons request that passes the session-key + restricted
    gates so execution reaches body validation (the code under test)."""
    mock_state = MagicMock()
    # The route now reports which outcome the store produced, and puts that word in
    # the response body -- so the mocked store has to answer with the string its real
    # counterpart returns rather than a MagicMock.
    mock_state.lessons.save_or_enrich.return_value = "inserted"
    request = MagicMock()
    request.app = {"state": mock_state}
    request.headers = {"X-Session-Key": "dashboard:ui"}
    attach_body(request, body)
    return request


# ── /api/crons ──


class TestCronCreateTypeValidation:
    @pytest.mark.asyncio
    async def test_message_array_returns_400_not_500(self):
        """Reproduction: message as a JSON array must 400, not crash on .strip()."""
        request = _crons_request({"name": "test", "message": ["cat /etc/passwd", "rm -rf /"], "every": 60})
        resp = await api_crons_create(request)
        assert resp.status == 400
        request.app["state"].crons.add_job_async.assert_not_called()

    @pytest.mark.asyncio
    async def test_name_integer_returns_400_not_500(self):
        """Reproduction: name as an integer must 400, not crash on .strip()."""
        request = _crons_request({"name": 12345, "message": "test", "every": 60})
        resp = await api_crons_create(request)
        assert resp.status == 400
        request.app["state"].crons.add_job_async.assert_not_called()

    @pytest.mark.asyncio
    async def test_message_dict_returns_400(self):
        request = _crons_request({"name": "test", "message": {"x": 1}, "every": 60})
        resp = await api_crons_create(request)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_non_object_body_returns_400(self):
        request = _crons_request(["not", "an", "object"])
        resp = await api_crons_create(request)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_valid_request_still_succeeds(self):
        request = _crons_request({"name": "test", "message": "do the thing", "every": 300})
        resp = await api_crons_create(request)
        assert resp.status == 200
        request.app["state"].crons.add_job_async.assert_called_once()

    @pytest.mark.asyncio
    async def test_channel_non_string_returns_400(self):
        request = _crons_request({"name": "t", "message": "m", "every": 60, "channel": ["C1"]})
        resp = await api_crons_create(request)
        assert resp.status == 400


# ── /api/lessons ──


class TestLessonsCreateTypeValidation:
    @pytest.mark.asyncio
    async def test_rule_array_returns_400_not_500(self):
        """Reproduction: rule as a JSON array must 400, not crash on .strip()."""
        request = _lessons_request({"rule": ["injected", "array", "payload"], "category": "tool"})
        with (
            patch("kiro_crew.dashboard.handlers.cron._sel"),
            patch("kiro_crew.dashboard.handlers.cron._is_restricted_session", return_value=False),
        ):
            resp = await api_lessons_create(request)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_rule_dict_returns_400(self):
        request = _lessons_request({"rule": {"x": "y"}, "category": "tool"})
        with (
            patch("kiro_crew.dashboard.handlers.cron._sel"),
            patch("kiro_crew.dashboard.handlers.cron._is_restricted_session", return_value=False),
        ):
            resp = await api_lessons_create(request)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_invalid_category_returns_400(self):
        """category outside the {tool, preference, knowledge} enum is rejected."""
        request = _lessons_request({"rule": "valid rule", "category": "arbitrary_value"})
        with (
            patch("kiro_crew.dashboard.handlers.cron._sel"),
            patch("kiro_crew.dashboard.handlers.cron._is_restricted_session", return_value=False),
        ):
            resp = await api_lessons_create(request)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_oversized_rule_returns_400(self):
        """A rule over the schema length bound (but under the byte cap) is a 400."""
        request = _lessons_request({"rule": "x" * 1_000, "category": "tool"})
        with (
            patch("kiro_crew.dashboard.handlers.cron._sel"),
            patch("kiro_crew.dashboard.handlers.cron._is_restricted_session", return_value=False),
        ):
            resp = await api_lessons_create(request)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_oversized_body_returns_413(self):
        """A 100KB+ body now exceeds read_bounded_json's byte cap before any
        field-level bound is consulted."""
        request = _lessons_request({"rule": "x" * 100_000, "category": "tool"})
        with (
            patch("kiro_crew.dashboard.handlers.cron._sel"),
            patch("kiro_crew.dashboard.handlers.cron._is_restricted_session", return_value=False),
        ):
            resp = await api_lessons_create(request)
        assert resp.status == 413

    @pytest.mark.asyncio
    async def test_non_object_body_returns_400(self):
        request = _lessons_request(["not", "an", "object"])
        with (
            patch("kiro_crew.dashboard.handlers.cron._sel"),
            patch("kiro_crew.dashboard.handlers.cron._is_restricted_session", return_value=False),
        ):
            resp = await api_lessons_create(request)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_valid_lesson_still_succeeds(self):
        """A well-formed lesson writes via the JSONL fallback and returns 200."""
        request = _lessons_request({"rule": "always do X", "category": "preference"})
        with (
            patch("kiro_crew.dashboard.handlers.cron._sel"),
            patch("kiro_crew.dashboard.handlers.cron._is_restricted_session", return_value=False),
            patch(
                "kiro_crew.dashboard.handlers.cron._get_memory",
                MagicMock(return_value=MagicMock(vector_store=None)),
            ),
        ):
            resp = await api_lessons_create(request)
        assert resp.status == 200
        # save_or_enrich, not save: the route switched so a re-submitted rule can have
        # a NOT-clause attached instead of being skipped as a duplicate.
        request.app["state"].lessons.save_or_enrich.assert_called_once()
