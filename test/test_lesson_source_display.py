"""A lesson's write origin is visible wherever lessons are listed.

A lesson the user saved and one the extractor inferred from chat history carry
different authority, so both the agent's ``learn_list`` tool and the dashboard's
``GET /api/lessons`` payload report each row's ``source``.
"""

from __future__ import annotations

import contextlib
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.vector_memory import VectorMemoryStore


def test_learn_list_shows_each_lessons_source(monkeypatch):
    from kiro_crew import mcp_core
    from kiro_crew.mcp_tools import learn as mcp_learn

    monkeypatch.setattr(
        mcp_core,
        "_get",
        lambda *a, **k: {
            "lessons": [
                {"category": "preference", "rule": "use tabs", "source": "user_explicit"},
                {"category": "knowledge", "rule": "cache needs a flush", "source": "consolidation"},
                {"category": "knowledge", "rule": "row with no origin"},
            ]
        },
    )
    out = mcp_learn.learn_list("learn_list", {})
    lines = out.splitlines()
    assert lines[0] == "[preference] use tabs (source: user_explicit)"
    assert lines[1] == "[knowledge] cache needs a flush (source: consolidation)"
    # A row whose store recorded no origin renders bare rather than inventing one.
    assert lines[2] == "[knowledge] row with no origin"


def test_source_suffix_ignores_blank_and_non_string_values():
    from kiro_crew.mcp_tools.learn import _source_suffix

    assert _source_suffix({"source": "  "}) == ""
    assert _source_suffix({"source": None}) == ""
    assert _source_suffix({"source": 3}) == ""
    assert _source_suffix({}) == ""


async def _lessons_api_body(tmp_path, source: str, **patches) -> dict:
    """GET /api/lessons over a real vector store holding one row with *source*."""
    from kiro_crew.dashboard.handlers import cron

    store = VectorMemoryStore(db_path=tmp_path / "m.db", embedding_dim=4)
    store.init()
    try:
        assert (
            store.set_semantic(
                "lesson.userrow",
                {"rule": "always run the formatter", "category": "preference"},
                1.0,
                source,
            )
            is None
        )
        request = MagicMock()
        state = MagicMock()
        request.app = {"state": state}
        request.headers = {"X-Session-Key": "dashboard:ui"}
        request.query = {}
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(cron, "_blocks_reads_session", return_value=False))
            stack.enter_context(
                patch.object(
                    cron, "resolve_lesson_memory_store", new=AsyncMock(return_value=(None, None))
                )
            )
            stack.enter_context(
                patch.object(cron, "_prepare_member_lesson_store", new=AsyncMock(return_value=None))
            )
            stack.enter_context(
                patch.object(cron, "_get_memory", return_value=MagicMock(vector_store=store))
            )
            for name, value in patches.items():
                stack.enter_context(patch.object(cron, name, value))
            resp = await cron.api_lessons(request)
        return json.loads(resp.text)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_lessons_api_reports_the_vector_rows_source(tmp_path):
    body = await _lessons_api_body(tmp_path, "user_explicit")
    assert body["lessons"][0]["rule"] == "always run the formatter"
    assert body["lessons"][0]["source"] == "user_explicit"


@pytest.mark.asyncio
async def test_lessons_api_redacts_the_whole_source_before_bounding_it(tmp_path):
    """A credential straddling the length bound is redacted whole, not cut first.

    The stand-in redactor only recognises the complete marker, as a real pattern
    only recognises a complete credential; bounding first would leave its prefix.
    """
    marker = "SECRET-0123456789"
    body = await _lessons_api_body(
        tmp_path,
        "a" * 55 + marker,
        _redact_memory_field=lambda value: value.replace(marker, "[REDACTED]"),
    )
    source = body["lessons"][0]["source"]
    assert "SECRET" not in source
    assert len(source) <= 64
