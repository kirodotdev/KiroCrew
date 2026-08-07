"""REST input-validation tests for /api/crons and /api/lessons POST handlers.

Pentest finding: these endpoints called string methods (``.strip()``) directly
on ``body.get(...)`` values, so a JSON array/dict/int in a string field raised
an unhandled AttributeError -> HTTP 500. The fix routes field extraction
through validation.py helpers (the same rules the MCP tool layer enforces), so
bad types now return a structured HTTP 400 instead of crashing. These tests
assert 400-not-500 for the exact reproduction payloads plus enum/length bounds.
"""

from __future__ import annotations

import json
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


class TestLessonsCreateNegativeForwarding:
    """The 'what not to do' half must survive the write, not be silently
    dropped: the vector path passed a literal ``None`` for negative, and the
    JSONL path constructed the Lesson without it."""

    @pytest.mark.asyncio
    async def test_vector_path_forwards_negative(self):
        request = _lessons_request(
            {"rule": "always X", "category": "tool", "negative": "never Y"}
        )
        state = request.app["state"]
        vs = state.memory.vector_store
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
    async def test_unapplied_write_with_negative_is_422_not_silent_200(self):
        """FALSE SUCCESS GUARD: write_lesson returning False with a supplied
        negative means the caller's guidance may be nowhere — the handler must
        say so instead of reporting 200 (the bug class this PR fixes)."""
        request = _lessons_request(
            {"rule": "always X", "category": "tool", "negative": "never Y"}
        )
        state = request.app["state"]
        vs = state.memory.vector_store
        vs.write_lesson = MagicMock(return_value=False)  # deduped or rejected
        vs.embed_lesson = MagicMock(return_value=[0.0])
        with (
            patch("kiro_crew.dashboard.handlers.cron._sel"),
            patch("kiro_crew.dashboard.handlers.cron._is_restricted_session", return_value=False),
            patch("kiro_crew.dashboard.handlers.cron._get_memory", return_value=state.memory),
        ):
            resp = await api_lessons_create(request)
        assert resp.status == 422
        body = json.loads(resp.body)
        assert body["code"] == "lesson_write_not_applied"

    @pytest.mark.asyncio
    async def test_unapplied_write_without_negative_stays_200(self):
        """Plain dedup without a negative keeps the long-standing silent-200
        semantics — only a potentially-lost negative escalates."""
        request = _lessons_request({"rule": "always X", "category": "tool"})
        state = request.app["state"]
        vs = state.memory.vector_store
        vs.write_lesson = MagicMock(return_value=False)
        vs.find_contradiction_candidates = MagicMock(return_value=[])
        vs.embed_lesson = MagicMock(return_value=[0.0])
        with (
            patch("kiro_crew.dashboard.handlers.cron._sel"),
            patch("kiro_crew.dashboard.handlers.cron._is_restricted_session", return_value=False),
            patch("kiro_crew.dashboard.handlers.cron._get_memory", return_value=state.memory),
        ):
            resp = await api_lessons_create(request)
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_jsonl_path_persists_negative(self):
        request = _lessons_request(
            {"rule": "always X", "category": "tool", "negative": "never Y"}
        )
        state = request.app["state"]
        saved = []
        state.lessons.save = MagicMock(side_effect=lambda le: (saved.append(le), "written")[1])
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

    @pytest.mark.asyncio
    async def test_jsonl_conflicting_negative_is_422_not_silent_200(self):
        """JSONL parity with the vector-branch guard (design review): a
        conflicting re-teach whose negative is discarded by the
        never-overwrite rule must surface as 422, not silent 200."""
        request = _lessons_request(
            {"rule": "always X", "category": "tool", "negative": "never Z"}
        )
        state = request.app["state"]
        state.lessons.save = MagicMock(return_value="deduped")
        memory = MagicMock()
        memory.vector_store = None
        with (
            patch("kiro_crew.dashboard.handlers.cron._sel"),
            patch("kiro_crew.dashboard.handlers.cron._is_restricted_session", return_value=False),
            patch("kiro_crew.dashboard.handlers.cron._get_memory", return_value=memory),
        ):
            resp = await api_lessons_create(request)
        assert resp.status == 422
        assert json.loads(resp.body)["code"] == "lesson_write_not_applied"


class TestVectorLessonUpgradeAndListShape:
    def test_write_lesson_exact_rule_with_new_negative_replaces_row(self, tmp_path):
        """Re-teaching a rule WITH a negative repairs a pre-fix row instead of
        being deduped away as 'already covered'."""
        from kiro_crew.vector_memory import VectorMemoryStore

        vs = VectorMemoryStore(db_path=tmp_path / "m.db", embedding_dim=4)
        vs.init()
        try:
            assert vs.write_lesson("always X") is True  # pre-fix shape: no negative
            assert vs.write_lesson("always X", negative="never Y") is True
            values = [json.loads(e["value_json"]) for e in vs.get_lessons()]
            assert values == ["always X — NOT: never Y"]
        finally:
            vs.close()

    def test_case_insensitive_upgrade_reuses_the_stored_key(self, tmp_path):
        """'Always X' re-taught as 'always x' + negative must UPGRADE the
        stored row, not mint a second key from the different casing and leave
        duplicate active records."""
        from kiro_crew.vector_memory import VectorMemoryStore

        vs = VectorMemoryStore(db_path=tmp_path / "m.db", embedding_dim=4)
        vs.init()
        try:
            assert vs.write_lesson("Always X") is True
            assert vs.write_lesson("always x", negative="never Y") is True
            lessons = vs.get_lessons()
            assert len(lessons) == 1, "upgrade must not duplicate the record"
            assert json.loads(lessons[0]["value_json"]) == "always x — NOT: never Y"
        finally:
            vs.close()

    def test_oversize_upgrade_rejection_preserves_the_old_row(self, tmp_path):
        """VALIDATE-BEFORE-WRITE: an upgrade whose combined value exceeds the
        store's size cap must be rejected with the ORIGINAL lesson intact —
        an upgrade path must never convert a validation failure into
        permanent loss."""
        from kiro_crew.vector_memory import VectorMemoryStore

        vs = VectorMemoryStore(db_path=tmp_path / "m.db", embedding_dim=4)
        vs.init()
        try:
            rule = "always " + "x" * 400  # valid alone
            assert vs.write_lesson(rule) is True
            # Combined value blows past the store's value cap.
            ok = vs.write_lesson(rule, negative="n" * 100_000)
            values = [json.loads(e["value_json"]) for e in vs.get_lessons()]
            assert values == [rule], "old row must survive a rejected upgrade"
            assert ok is False
        finally:
            vs.close()

    @pytest.mark.asyncio
    async def test_vector_list_branch_returns_fused_value_without_fabricating(self):
        """The vector store fuses 'rule — NOT: negative' at write time and a
        read-side split cannot be faithful (a rule containing the literal
        separator would be truncated with a fabricated negative). The vector
        branch therefore returns the fused value as `rule` and negative=None
        — the documented backend asymmetry."""
        from kiro_crew.dashboard.handlers.cron import api_lessons

        request = _lessons_request({})
        request.query = {}
        memory = MagicMock()
        memory.vector_store.get_lessons.return_value = [
            {"value_json": json.dumps("always X — NOT: never Y"), "updated_at": "t1"},
            {
                # A rule LEGITIMATELY containing the separator — must not be split.
                "value_json": json.dumps("when parsing ' — NOT: ' treat it as literal"),
                "updated_at": "t2",
            },
        ]
        with (
            patch("kiro_crew.dashboard.handlers.cron._sel"),
            patch(
                "kiro_crew.dashboard.handlers.cron._blocks_reads_session", return_value=False
            ),
            patch("kiro_crew.dashboard.handlers.cron._get_memory", return_value=memory),
        ):
            resp = await api_lessons(request)
        body = json.loads(resp.body)
        assert body["lessons"][0]["rule"] == "always X — NOT: never Y"
        assert body["lessons"][0]["negative"] is None
        assert body["lessons"][1]["rule"] == "when parsing ' — NOT: ' treat it as literal"
        assert body["lessons"][1]["negative"] is None

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
