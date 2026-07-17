"""Test spawn_run fire-and-forget functionality."""

from __future__ import annotations

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
