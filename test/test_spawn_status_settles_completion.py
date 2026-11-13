"""A full ``spawn_status`` read of a finished run settles that run's completion.

The parent already holds the result, so once the reply carrying it is written the
tool sends ``POST /api/spawn/mark-collected`` for that run, the mark the blocking
``spawn_sub_agents`` sends. A read that may not hold the result (running, paged,
grepped, failed lookup) and a reply that was not written mark nothing.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew import mcp_core
from kiro_crew.mcp_core import _call_tool_inner
from kiro_crew.mcp_shared import DeferredTool, drive_deferred

_DONE = {"done": True, "result": "final answer", "outcome": "completed"}
_RUNNING = {"done": False, "result": "", "turns": 2}


def _read(args: dict[str, Any], payload: dict[str, Any], session: str = "dashboard:chat-1"):
    posts: list[tuple[str, dict]] = []

    def _post(path: str, body: dict, **_kw: object) -> dict:
        posts.append((path, body))
        return {"status": "ok"}

    with (
        patch.object(mcp_core, "_get", return_value=payload),
        patch.object(mcp_core, "_post", side_effect=_post),
        patch.object(mcp_core, "_resolve_session_key", return_value=session),
        patch.object(mcp_core, "_resolve_session_key_strict", return_value=session),
        patch.object(mcp_core, "sel", MagicMock()),
    ):
        result = _call_tool_inner("spawn_status", args)
        return result, posts, _post


def test_full_read_of_a_finished_run_marks_it_collected_after_the_write() -> None:
    result, posts, post = _read({"agent_id": "abc123"}, _DONE)
    assert isinstance(result, DeferredTool)
    with patch.object(mcp_core, "_post", side_effect=post):
        assert result.step() is not None
        assert posts == [], "nothing is marked before the reply is written"
        text = drive_deferred(result)
    assert "final answer" in text
    assert posts == [
        (
            "/api/spawn/mark-collected",
            {"ids": ["abc123"], "parent_session": "dashboard:chat-1", "written": True},
        )
    ]


def test_an_unwritten_reply_marks_nothing() -> None:
    result, posts, post = _read({"agent_id": "abc123"}, _DONE)
    assert isinstance(result, DeferredTool)
    with patch.object(mcp_core, "_post", side_effect=post):
        result.on_settled(result.step() or "")
        result.on_answered(False)
    assert posts == []


@pytest.mark.parametrize(
    ("args", "payload"),
    [
        ({"agent_id": "abc123"}, _RUNNING),
        ({"agent_id": "abc123", "offset": 10}, _DONE),
        ({"agent_id": "abc123", "grep": "answer"}, {**_DONE, "result_meta": {"total_lines": 3}}),
        ({"agent_id": "abc123"}, {"error": "HTTP 404"}),
    ],
    ids=["running", "paged", "grepped", "lookup-failed"],
)
def test_a_read_that_may_not_hold_the_result_marks_nothing(args, payload) -> None:
    result, posts, _ = _read(args, payload)
    assert isinstance(result, str)
    assert posts == []


def test_an_over_cap_reply_marks_nothing() -> None:
    from kiro_crew.validation import MAX_RESPONSE_LEN

    result, posts, _ = _read(
        {"agent_id": "abc123"}, {**_DONE, "result": "x" * (MAX_RESPONSE_LEN + 1)}
    )
    assert isinstance(result, str)
    assert posts == []


def test_usage_header_counts_toward_the_response_cap() -> None:
    from kiro_crew.validation import MAX_RESPONSE_LEN

    result, posts, _ = _read(
        {"agent_id": "abc123"},
        {**_DONE, "result": "x" * MAX_RESPONSE_LEN, "elapsed": 1, "credits": 0},
    )
    assert isinstance(result, str)
    assert posts == []


def test_a_reply_that_fits_after_sanitization_marks_after_the_write() -> None:
    from kiro_crew.validation import MAX_RESPONSE_LEN, sanitize_string

    text = "x" * MAX_RESPONSE_LEN + "\u200b"
    assert len(text) > MAX_RESPONSE_LEN
    assert len(sanitize_string(text)) == MAX_RESPONSE_LEN
    result, posts, post = _read({"agent_id": "abc123"}, {**_DONE, "result": text})
    assert isinstance(result, DeferredTool)
    assert posts == []
    with patch.object(mcp_core, "_post", side_effect=post):
        drive_deferred(result)
    assert len(posts) == 1


@pytest.mark.parametrize(
    "ending",
    [{"outcome": "stopped"}, {"outcome": "failed"}, {}, {"outcome": "unknown"}],
    ids=["stopped", "failed", "missing", "unknown"],
)
def test_only_a_completed_outcome_can_settle_the_result(ending) -> None:
    result, posts, _ = _read(
        {"agent_id": "abc123"}, {"done": True, "result": "partial output", **ending}
    )
    assert isinstance(result, str)
    assert "partial output" in result
    assert posts == []


def test_no_resolvable_session_answers_plain_text() -> None:
    result, posts, _ = _read({"agent_id": "abc123"}, _DONE, session="")
    assert isinstance(result, str) and "final answer" in result
    assert posts == []


def test_a_transport_failure_is_retried_once_and_never_raises() -> None:
    from kiro_crew.subagent_read_collected import post_mark_collected

    calls: list[float] = []

    def _post(path: str, body: dict, *, timeout: float) -> dict:
        calls.append(timeout)
        raise OSError("connection reset")

    with patch.object(mcp_core, "_post", side_effect=_post):
        post_mark_collected(["abc123"], "dashboard:chat-1", tool="spawn_status")
    assert calls == [5, 10]
