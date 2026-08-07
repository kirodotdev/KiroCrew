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

from kiro_crew.dashboard.handlers import api_crons_create, api_lessons_create


def _crons_request(body: object) -> MagicMock:
    mock_state = MagicMock()
    mock_job = MagicMock()
    mock_job.id = "job-1"
    mock_job.agent_id = ""
    mock_state.crons.add_job_async = AsyncMock(return_value=mock_job)
    request = MagicMock()
    request.app = {"state": mock_state}
    request.json = AsyncMock(return_value=body)
    return request


def _lessons_request(body: object) -> MagicMock:
    """Build a /api/lessons request that passes the session-key + restricted
    gates so execution reaches body validation (the code under test)."""
    mock_state = MagicMock()
    request = MagicMock()
    request.app = {"state": mock_state}
    request.headers = {"X-Session-Key": "dashboard:ui"}
    request.json = AsyncMock(return_value=body)
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
        """A 100KB+ rule exceeds the schema length bound and is rejected."""
        request = _lessons_request({"rule": "x" * 100_000, "category": "tool"})
        with (
            patch("kiro_crew.dashboard.handlers.cron._sel"),
            patch("kiro_crew.dashboard.handlers.cron._is_restricted_session", return_value=False),
        ):
            resp = await api_lessons_create(request)
        assert resp.status == 400


class TestLessonsCreateFieldForwarding:
    """negative/evidence must survive the write, not be silently dropped.

    Regression coverage: the vector path passed a literal ``None`` for
    negative (discarding the caller's "what not to do"), and the JSONL path
    constructed the Lesson without negative at all.
    """

    @pytest.mark.asyncio
    async def test_vector_path_forwards_negative(self):
        request = _lessons_request(
            {"rule": "always X", "category": "tool", "negative": "never Y"}
        )
        state = request.app["state"]
        vs = state.memory.vector_store  # MagicMock chain from _lessons_request
        vs.write_lesson = MagicMock(return_value=True)
        vs.find_contradiction_candidates = MagicMock(return_value=[])
        vs.embed_lesson = MagicMock(return_value=[0.0])
        with (
            patch("kiro_crew.dashboard.handlers.cron._sel"),
            patch("kiro_crew.dashboard.handlers.cron._is_restricted_session", return_value=False),
            patch("kiro_crew.dashboard.handlers.cron._get_memory", return_value=state.memory),
        ):
            resp = await api_lessons_create(request)
        assert resp.status == 200
        args = vs.write_lesson.call_args.args
        assert args[0] == "always X"
        assert args[2] == "never Y", "negative must reach write_lesson, not a literal None"

    @pytest.mark.asyncio
    async def test_jsonl_path_persists_negative_and_evidence(self):
        request = _lessons_request(
            {
                "rule": "always X",
                "category": "tool",
                "negative": "never Y",
                "evidence": "user corrected this in review",
            }
        )
        state = request.app["state"]
        saved = []
        state.lessons.save = MagicMock(side_effect=saved.append)
        memory = MagicMock()
        memory.vector_store = None  # force the JSONL fallback path
        with (
            patch("kiro_crew.dashboard.handlers.cron._sel"),
            patch("kiro_crew.dashboard.handlers.cron._is_restricted_session", return_value=False),
            patch("kiro_crew.dashboard.handlers.cron._get_memory", return_value=memory),
        ):
            resp = await api_lessons_create(request)
        assert resp.status == 200
        assert len(saved) == 1
        assert saved[0].negative == "never Y"
        assert saved[0].evidence == "user corrected this in review"

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
        request.app["state"].lessons.save.assert_called_once()
