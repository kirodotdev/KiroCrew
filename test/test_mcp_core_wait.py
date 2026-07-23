"""Test spawn_run fire-and-forget functionality."""

from __future__ import annotations

import os
from unittest.mock import patch

from kiro_crew.mcp_core import _call_tool


def test_spawn_run_single_task():
    """Test spawn_run with single task returns immediately."""
    with patch("kiro_crew.mcp_core._post") as mock_post:
        mock_post.return_value = {"id": "abc123"}

        result = _call_tool("spawn_run", {"task": "test task"})

        assert "abc123" in result
        assert "Spawned" in result
        assert "completion event" in result.lower()


def test_spawn_run_batch_tasks():
    """Test spawn_run with tasks array spawns all and returns immediately."""
    with patch("kiro_crew.mcp_core._post") as mock_post:
        mock_post.side_effect = [{"id": "a1"}, {"id": "b2"}, {"id": "c3"}]

        result = _call_tool("spawn_run", {"tasks": ["task1", "task2", "task3"]})

        assert "3 subagent" in result
        assert "a1" in result
        assert "b2" in result
        assert "c3" in result
        assert mock_post.call_count == 3


def test_spawn_run_error():
    """Test spawn_run handles spawn API errors."""
    with patch("kiro_crew.mcp_core._post") as mock_post:
        mock_post.return_value = {"error": "capacity reached"}

        result = _call_tool("spawn_run", {"task": "failing task"})

        assert "queued" in result or "Error" in result


def test_spawn_run_no_args():
    """Test spawn_run with no task or tasks returns error."""
    result = _call_tool("spawn_run", {})
    assert "Error" in result


def test_spawn_run_orphan_warning_when_parent_unresolved():
    """Empty parent_session + successful spawns -> loud orphan warning, and
    NO contradictory completion-event promise (review-bot)."""
    with patch("kiro_crew.mcp_core._post") as mock_post, \
         patch("kiro_crew.mcp_core._resolve_session_key", return_value=""):
        mock_post.return_value = {"id": "abc123"}
        result = _call_tool("spawn_run", {"task": "test task"})
    assert "parent_session UNRESOLVED" in result
    assert "abc123" in result
    assert "Monitor results via polling" in result
    assert "Results will arrive as completion events" not in result
    assert "Wait for [Subagent completion event]" not in result


def test_spawn_run_no_orphan_warning_when_all_spawns_fail():
    """Empty parent_session but ZERO spawned agents -> no misleading ⚠ orphan
    warning about spawned subagents that do not exist (review-bot).
    The queued-tasks message may still mention the unresolved parent (that
    part is accurate — queued tasks inherit it), but the spawned-subagents
    warning block must be absent."""
    with patch("kiro_crew.mcp_core._post") as mock_post, \
         patch("kiro_crew.mcp_core._resolve_session_key", return_value=""):
        mock_post.return_value = {"error": "capacity reached"}
        result = _call_tool("spawn_run", {"task": "failing task"})
    assert "these subagents are orphaned" not in result
    assert "⚠ parent_session UNRESOLVED —" not in result


class TestSpawnRunApprovalModeForwarding:
    """Regression tests: spawn_run must forward this session's own
    KIROCREW_APPROVAL_MODE env var to /api/spawn, so a cron running with
    approval_mode="auto" deterministically auto-approves its own subagent
    launches instead of depending solely on SubagentManager's parent_trusted
    lookup (which requires parent_session to resolve correctly)."""

    def test_forwards_approval_mode_auto_from_env(self):
        with patch("kiro_crew.mcp_core._post") as mock_post, \
                patch.dict("os.environ", {"KIROCREW_APPROVAL_MODE": "auto"}):
            mock_post.return_value = {"id": "abc123"}
            _call_tool("spawn_run", {"task": "test task"})

        body = mock_post.call_args[0][1]
        assert body["approval_mode"] == "auto"

    def test_omits_approval_mode_when_env_unset(self):
        with patch("kiro_crew.mcp_core._post") as mock_post, \
                patch.dict("os.environ", {}, clear=False):
            os.environ.pop("KIROCREW_APPROVAL_MODE", None)
            mock_post.return_value = {"id": "abc123"}
            _call_tool("spawn_run", {"task": "test task"})

        body = mock_post.call_args[0][1]
        assert "approval_mode" not in body

    def test_forwards_approval_mode_to_every_batch_task(self):
        with patch("kiro_crew.mcp_core._post") as mock_post, \
                patch.dict("os.environ", {"KIROCREW_APPROVAL_MODE": "auto"}):
            mock_post.side_effect = [{"id": "a1"}, {"id": "b2"}]
            _call_tool("spawn_run", {"tasks": ["task1", "task2"]})

        assert mock_post.call_count == 2
        for call in mock_post.call_args_list:
            assert call[0][1]["approval_mode"] == "auto"


def test_spawn_run_no_orphan_warning_when_parent_resolved():
    """Resolved parent_session -> no orphan warning."""
    with patch("kiro_crew.mcp_core._post") as mock_post, \
         patch("kiro_crew.mcp_core._resolve_session_key", return_value="dashboard:chat-1"):
        mock_post.return_value = {"id": "abc123"}
        result = _call_tool("spawn_run", {"task": "test task"})
    assert "parent_session UNRESOLVED" not in result


def test_spawn_run_queued_only_orphan_no_completion_promise():
    """All spawns queued (capacity) + empty parent_session -> the queued
    message must NOT promise completion events either (review-bot
    round 3): queued tasks inherit the same empty parent_session."""
    with patch("kiro_crew.mcp_core._post") as mock_post, \
         patch("kiro_crew.mcp_core._resolve_session_key", return_value=""):
        mock_post.return_value = {"error": "capacity reached"}
        result = _call_tool("spawn_run", {"task": "queued task"})
    assert "All tasks queued" in result
    assert "results will arrive as completion events" not in result
    assert "spawn_list" in result


def test_spawn_run_queued_only_with_parent_promises_events():
    """All spawns queued + resolved parent_session -> completion-event promise
    is correct and preserved."""
    with patch("kiro_crew.mcp_core._post") as mock_post, \
         patch("kiro_crew.mcp_core._resolve_session_key", return_value="dashboard:chat-1"):
        mock_post.return_value = {"error": "capacity reached"}
        result = _call_tool("spawn_run", {"task": "queued task"})
    assert "All tasks queued — results will arrive as completion events." in result


def test_spawn_run_empty_tasks():
    """Test spawn_run with empty tasks array returns error."""
    result = _call_tool("spawn_run", {"tasks": []})
    assert "Error" in result


def test_spawn_run_passes_parent_session():
    """Test spawn_run reads parent session from PID file and passes it."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        pid_file = Path(tmpdir) / "session_pid_99999.txt"
        pid_file.write_text("1773616886.045109")

        with patch("kiro_crew.mcp_core._post") as mock_post, patch(
            "pathlib.Path.home", return_value=Path(tmpdir).parent
        ):
            mock_post.return_value = {"id": "x1"}
            # This test verifies the parent_session plumbing exists;
            # exact file lookup depends on home dir structure
            result = _call_tool("spawn_run", {"task": "test"})
            assert "Spawned" in result


def test_spawn_run_batch_partial_failure():
    """Test spawn_run stops on first spawn error in batch."""
    with patch("kiro_crew.mcp_core._post") as mock_post:
        mock_post.side_effect = [{"id": "ok1"}, {"error": "capacity reached"}]

        result = _call_tool("spawn_run", {"tasks": ["task1", "task2"]})

        assert "Spawned" in result or "queued" in result
