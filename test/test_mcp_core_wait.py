"""Test spawn_run fire-and-forget functionality."""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import uuid
from unittest.mock import patch

from kiro_crew.mcp_core import _call_tool, _post


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
    """A rejected spawn is reported as failed, never queued or running."""
    with (
        patch("kiro_crew.mcp_core._post") as mock_post,
        patch("kiro_crew.mcp_core._resolve_session_key", return_value="dashboard:chat-1"),
    ):
        mock_post.return_value = {"error": "Forbidden"}

        result = _call_tool("spawn_run", {"task": "failing task"})

        assert "Error: 1 task(s) failed to start" in result
        assert "failing task: Forbidden" in result
        assert "none of the requested subagents were started" in result
        assert "queued" not in result


def test_post_marks_transport_errors_as_uncertain():
    """A failed response does not prove that the gateway rejected the spawn."""
    with (
        patch("kiro_crew.mcp_core._resolve_session_key", return_value=""),
        patch("kiro_crew.mcp_core._internal_secret", return_value="secret"),
        patch("kiro_crew.mcp_core.loopback_urlopen", side_effect=TimeoutError("timed out")),
    ):
        result = _post("/api/spawn", {"task": "maybe accepted"})

    assert result == {"error": "timed out", "transport_error": True}


def test_post_marks_connection_refusal_as_definite_failure():
    """A refused connection proves the gateway did not accept the spawn."""
    refused = urllib.error.URLError(ConnectionRefusedError("connection refused"))
    with (
        patch("kiro_crew.mcp_core._resolve_session_key", return_value=""),
        patch("kiro_crew.mcp_core._internal_secret", return_value="secret"),
        patch("kiro_crew.mcp_core.loopback_urlopen", side_effect=refused),
    ):
        result = _post("/api/spawn", {"task": "not accepted"})

    assert "connection refused" in result["error"]
    assert "transport_error" not in result


def test_spawn_run_connection_refusal_reconciles_lost_batch_member():
    """A definite uncounted rejection is immediately reconciled as lost."""
    with (
        patch("kiro_crew.mcp_core._post") as mock_post,
        patch("kiro_crew.mcp_core._resolve_session_key", return_value="dashboard:chat-1"),
    ):
        mock_post.side_effect = [
            {"error": "connection refused"},
            {"ok": True},
            {"id": "ok2"},
        ]
        result = _call_tool("spawn_run", {"tasks": ["task1", "task2"]})

    assert "ok2: task2" in result
    assert "task1: connection refused" in result
    assert [call.args[0] for call in mock_post.call_args_list] == [
        "/api/spawn",
        "/api/spawn/lost",
        "/api/spawn",
    ]


def test_spawn_run_transport_failure_reports_unknown_acceptance():
    """Transport uncertainty is not a rejection and is never auto-reconciled."""
    with (
        patch("kiro_crew.mcp_core._post") as mock_post,
        patch("kiro_crew.mcp_core._resolve_session_key", return_value="dashboard:chat-1"),
    ):
        mock_post.side_effect = [
            {"error": "timed out", "transport_error": True},
            {"id": "ok2"},
        ]
        result = _call_tool("spawn_run", {"tasks": ["task1", "task2"]})

    assert "ok2: task2" in result
    assert "unknown acceptance status" in result
    assert "task1: timed out" in result
    assert "may have accepted" in result
    assert "Do not retry automatically" in result
    assert "empty spawn_list result is inconclusive" in result
    assert "wait and recheck" in result
    assert "none of the requested subagents were started" not in result
    assert "task(s) failed to start" not in result
    assert [call.args[0] for call in mock_post.call_args_list] == [
        "/api/spawn",
        "/api/spawn",
    ]


def test_spawn_run_recovers_uncertain_submission_by_durable_command_lookup():
    """A lost response is resolved from its stable command, not declared lost."""
    identities = iter(
        [
            uuid.UUID("00000000-0000-0000-0000-000000000001"),
            uuid.UUID("00000000-0000-0000-0000-000000000002"),
            uuid.UUID("00000000-0000-0000-0000-000000000003"),
        ]
    )
    with (
        patch(
            "kiro_crew.mcp_core._post", return_value={"error": "timed out", "transport_error": True}
        ) as mock_post,
        patch(
            "kiro_crew.mcp_core._get",
            return_value={"found": True, "id": "durable1", "status": "spawned"},
        ) as mock_get,
        patch(
            "kiro_crew.mcp_tools.spawn.uuid.uuid4",
            side_effect=lambda: next(identities, uuid.UUID("00000000-0000-0000-0000-000000000099")),
        ),
    ):
        result = _call_tool("spawn_run", {"task": "test task"})

    body = mock_post.call_args.args[1]
    assert body["run_id"] == "00000000"
    assert body["command_id"] == "00000000000000000000000000000002"
    assert body["idempotency_key"] == "00000000000000000000000000000003"
    semantic_body = {
        key: value
        for key, value in body.items()
        if key not in {"command_id", "idempotency_key", "payload_hash"}
    }
    canonical = json.dumps(
        {"operation": "spawn", **semantic_body}, separators=(",", ":"), sort_keys=True
    )
    assert body["payload_hash"] == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert mock_get.call_args.args == ("/api/spawn/commands/00000000000000000000000000000003",)
    assert "durable1" in result
    assert "unknown acceptance status" not in result


def test_spawn_run_resolves_machine_coded_coordinator_uncertainty() -> None:
    """HTTP error flattening must not bypass the stable command lookup."""
    for code in ("coordinator_outcome_uncertain", "coordinator_unavailable"):
        with (
            patch(
                "kiro_crew.mcp_core._post",
                return_value={"error": "coordinator unavailable", "code": code, "counted": True},
            ),
            patch(
                "kiro_crew.mcp_core._get",
                return_value={"found": True, "id": "durable-coded", "status": "spawned"},
            ) as mock_get,
        ):
            result = _call_tool("spawn_run", {"task": "test task"})

        mock_get.assert_called_once()
        assert "durable-coded" in result
        assert "unknown acceptance status" not in result


def test_pending_command_lookup_preserves_transport_uncertainty() -> None:
    """A durable pending row may still execute and must not close its batch early."""
    pending = {
        "found": True,
        "id": "durable1",
        "error": "command outcome is still pending",
        "status": "pending",
        "code": "command_pending",
    }
    with (
        patch(
            "kiro_crew.mcp_core._post",
            return_value={"error": "timed out", "transport_error": True},
        ) as mock_post,
        patch("kiro_crew.mcp_core._get", return_value=pending),
    ):
        result = _call_tool("spawn_run", {"tasks": ["task1", "task2"]})

    assert "acceptance status is unknown" in result
    assert "command outcome is still pending" in result
    assert [call.args[0] for call in mock_post.call_args_list] == [
        "/api/spawn",
        "/api/spawn",
    ]


def test_spawn_run_zero_confirmed_starts_retains_error_prefix():
    """Rejected plus uncertain submissions still report an overall error."""
    with (
        patch("kiro_crew.mcp_core._post") as mock_post,
        patch("kiro_crew.mcp_core._resolve_session_key", return_value="dashboard:chat-1"),
    ):
        mock_post.side_effect = [
            {"error": "Forbidden", "counted": True},
            {"error": "timed out", "transport_error": True},
        ]
        result = _call_tool("spawn_run", {"tasks": ["task1", "task2"]})

    assert result.startswith("Error:")
    assert "1 task(s) failed to start" in result
    assert "1 task(s) have unknown acceptance status" in result


def test_spawn_run_no_args():
    """Test spawn_run with no task or tasks returns error."""
    result = _call_tool("spawn_run", {})
    assert "Error" in result


def test_spawn_run_orphan_warning_when_parent_unresolved():
    """Empty parent_session + successful spawns -> loud orphan warning, and
    NO contradictory completion-event promise (review-bot)."""
    with (
        patch("kiro_crew.mcp_core._post") as mock_post,
        patch("kiro_crew.mcp_core._resolve_session_key", return_value=""),
    ):
        mock_post.return_value = {"id": "abc123"}
        result = _call_tool("spawn_run", {"task": "test task"})
    assert "parent_session UNRESOLVED" in result
    assert "abc123" in result
    assert "Monitor results via polling" in result
    assert "Results will arrive as completion events" not in result
    assert "Wait for [Subagent completion event]" not in result


def test_spawn_run_no_orphan_warning_when_all_spawns_fail():
    """A total rejection has no orphan warning or monitoring guidance."""
    with (
        patch("kiro_crew.mcp_core._post") as mock_post,
        patch("kiro_crew.mcp_core._resolve_session_key", return_value=""),
    ):
        mock_post.return_value = {"error": "Forbidden"}
        result = _call_tool("spawn_run", {"task": "failing task"})
    assert "these subagents are orphaned" not in result
    assert "⚠ parent_session UNRESOLVED —" not in result
    assert "none of the requested subagents were started" in result
    assert "queued" not in result


class TestSpawnRunApprovalModeForwarding:
    """Regression tests: spawn_run must forward this session's own
    KIROCREW_APPROVAL_MODE env var to /api/spawn, so a cron running with
    approval_mode="auto" deterministically auto-approves its own subagent
    launches instead of depending solely on SubagentManager's parent_trusted
    lookup (which requires parent_session to resolve correctly)."""

    def test_forwards_approval_mode_auto_from_env(self):
        with (
            patch("kiro_crew.mcp_core._post") as mock_post,
            patch.dict("os.environ", {"KIROCREW_APPROVAL_MODE": "auto"}),
        ):
            mock_post.return_value = {"id": "abc123"}
            _call_tool("spawn_run", {"task": "test task"})

        body = mock_post.call_args[0][1]
        assert body["approval_mode"] == "auto"

    def test_omits_approval_mode_when_env_unset(self):
        with (
            patch("kiro_crew.mcp_core._post") as mock_post,
            patch.dict("os.environ", {}, clear=False),
        ):
            os.environ.pop("KIROCREW_APPROVAL_MODE", None)
            mock_post.return_value = {"id": "abc123"}
            _call_tool("spawn_run", {"task": "test task"})

        body = mock_post.call_args[0][1]
        assert "approval_mode" not in body

    def test_forwards_approval_mode_to_every_batch_task(self):
        with (
            patch("kiro_crew.mcp_core._post") as mock_post,
            patch.dict("os.environ", {"KIROCREW_APPROVAL_MODE": "auto"}),
        ):
            mock_post.side_effect = [{"id": "a1"}, {"id": "b2"}]
            _call_tool("spawn_run", {"tasks": ["task1", "task2"]})

        assert mock_post.call_count == 2
        for call in mock_post.call_args_list:
            assert call[0][1]["approval_mode"] == "auto"


def test_spawn_run_no_orphan_warning_when_parent_resolved():
    """Resolved parent_session -> no orphan warning."""
    with (
        patch("kiro_crew.mcp_core._post") as mock_post,
        patch("kiro_crew.mcp_core._resolve_session_key", return_value="dashboard:chat-1"),
    ):
        mock_post.return_value = {"id": "abc123"}
        result = _call_tool("spawn_run", {"task": "test task"})
    assert "parent_session UNRESOLVED" not in result


def test_spawn_run_failed_only_orphan_no_completion_promise():
    """Failed submissions never promise completion events or polling."""
    with (
        patch("kiro_crew.mcp_core._post") as mock_post,
        patch("kiro_crew.mcp_core._resolve_session_key", return_value=""),
    ):
        mock_post.return_value = {"error": "capacity reached"}
        result = _call_tool("spawn_run", {"task": "failed task"})
    assert "failed to start" in result
    assert "none of the requested subagents were started" in result
    assert "queued" not in result
    assert "results will arrive as completion events" not in result
    assert "spawn_list" not in result


def test_spawn_run_failed_only_with_parent_promises_nothing():
    """A resolved parent does not turn a rejected submission into queued work."""
    with (
        patch("kiro_crew.mcp_core._post") as mock_post,
        patch("kiro_crew.mcp_core._resolve_session_key", return_value="dashboard:chat-1"),
    ):
        mock_post.return_value = {"error": "capacity reached"}
        result = _call_tool("spawn_run", {"task": "failed task"})
    assert "failed to start" in result
    assert "none of the requested subagents were started" in result
    assert "queued" not in result
    assert "completion events" not in result


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

        with (
            patch("kiro_crew.mcp_core._post") as mock_post,
            patch("pathlib.Path.home", return_value=Path(tmpdir).parent),
        ):
            mock_post.return_value = {"id": "x1"}
            # This test verifies the parent_session plumbing exists;
            # exact file lookup depends on home dir structure
            result = _call_tool("spawn_run", {"task": "test"})
            assert "Spawned" in result


def test_spawn_run_batch_partial_failure():
    """Partial batches keep successful ids paired with their actual tasks."""
    with patch("kiro_crew.mcp_core._post") as mock_post:
        mock_post.side_effect = [
            {"error": "Forbidden", "counted": True},
            {"id": "ok2"},
        ]

        result = _call_tool("spawn_run", {"tasks": ["task1", "task2"]})

        assert "Spawned 1 subagent" in result
        assert "ok2: task2" in result
        assert "1 task(s) failed to start" in result
        assert "task1: Forbidden" in result
        assert "ok2: task1" not in result
        assert "queued" not in result
