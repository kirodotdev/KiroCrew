"""Approval routing beyond the base cases of the tool-approval callback.

Covers the Slack button approval (approved, timeout, fallback to the dashboard), an
approval with a parent thread's context and one from an untrusted scoped slot, and
the task runner's approval callbacks with and without a dashboard.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_gateway_helpers import _restore_update_check_cache  # noqa: F401
from slack_gateway_helpers import (
    _make_orchestrator,
    _mock_context_builder,
    _mock_dashboard_state,
    _mock_sessions,
)

# ═══════════════════════════════════════════════════════════════════════════
# Tests: _interactive_approval Slack path
# ═══════════════════════════════════════════════════════════════════════════


class TestInteractiveApprovalSlack:
    """Slack-based approval with buttons."""

    @pytest.mark.asyncio
    async def test_slack_approval_approved(self):
        """Slack approval flow → approved."""
        orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
        mock_slack = MagicMock()
        mock_slack.open_dm = AsyncMock(return_value="D_U1")
        mock_slack.post_blocks = AsyncMock(return_value="approval_ts")
        mock_slack.update_message = AsyncMock()
        orch.slack = mock_slack
        orch.sessions = _mock_sessions()
        orch.sessions.get_channel = MagicMock(return_value=None)
        orch.sessions.get_thread = MagicMock(return_value=None)
        ds = _mock_dashboard_state()
        ds._yolo = False
        ds._slots = {}
        orch.dashboard_state = ds

        callback = orch._interactive_approval("subagent")
        event = MagicMock()
        event.request_id = "req-slack-1"
        event.title = "bash: echo hello"
        event.tool_input = ""
        event.tool_purpose = ""

        with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False):
            with patch("kiro_crew.slack.handler._build_approval_blocks", return_value=[]):
                with patch("kiro_crew.slack.handler._pending_approvals", {}):
                    # Make the pending future resolve immediately
                    async def _fake_wait_for(fut, timeout):
                        return "approved"

                    # flake-ok: pre-existing worker-wide wait_for patch inside the test, moved verbatim; tracked for a de-flake pass
                    with patch("asyncio.wait_for", side_effect=_fake_wait_for):
                        result = await callback(event, "")

        assert result is True

    @pytest.mark.asyncio
    async def test_slack_approval_timeout_rejects(self):
        """Slack approval timeout → rejected."""
        orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
        mock_slack = MagicMock()
        mock_slack.open_dm = AsyncMock(return_value="D_U1")
        mock_slack.post_blocks = AsyncMock(return_value="ts")
        mock_slack.update_message = AsyncMock()
        orch.slack = mock_slack
        orch.sessions = _mock_sessions()
        orch.sessions.get_channel = MagicMock(return_value=None)
        orch.sessions.get_thread = MagicMock(return_value=None)
        ds = _mock_dashboard_state()
        ds._yolo = False
        ds._slots = {}
        orch.dashboard_state = ds

        callback = orch._interactive_approval("cron")
        event = MagicMock()
        event.request_id = "req-slack-2"
        event.title = "dangerous"
        event.tool_input = ""
        event.tool_purpose = ""

        with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False):
            with patch("kiro_crew.slack.handler._build_approval_blocks", return_value=[]):
                with patch("kiro_crew.slack.handler._pending_approvals", {}):
                    # flake-ok: pre-existing worker-wide wait_for patch inside the test, moved verbatim; tracked for a de-flake pass
                    with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
                        result = await callback(event, "")

        assert result is False

    @pytest.mark.asyncio
    async def test_slack_approval_exception_falls_to_dashboard(self):
        """Slack approval exception → falls back to dashboard."""
        orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
        mock_slack = MagicMock()
        mock_slack.open_dm = AsyncMock(side_effect=RuntimeError("slack down"))
        orch.slack = mock_slack
        orch.sessions = _mock_sessions()
        ds = _mock_dashboard_state()
        ds._yolo = False
        ds._slots = {}
        ds.request_approval = AsyncMock(return_value=True)
        orch.dashboard_state = ds

        callback = orch._interactive_approval("heartbeat")
        event = MagicMock()
        event.request_id = "req-slack-3"
        event.title = "cmd"
        event.tool_input = ""
        event.tool_purpose = ""

        with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False):
            result = await callback(event, "")

        assert result is True
        ds.request_approval.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _init_task_runner approval callback
# ═══════════════════════════════════════════════════════════════════════════


class TestTaskRunnerApproval:
    """Task runner approval callback."""

    def test_task_runner_has_approval_callbacks(self):
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = _mock_context_builder()
        orch.ctx_builder.hooks = MagicMock()
        orch.conv_log = MagicMock()
        orch.consolidator = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        with patch("kiro_crew.slack.gateway.TaskRunner") as mock_tr:
            mock_tr_inst = MagicMock()
            # Set to None first so MagicMock won't auto-create child mocks on
            # attribute access — otherwise the assertions below are vacuous.
            mock_tr_inst._on_tool_approval = None
            mock_tr_inst._on_approval = None
            mock_tr.return_value = mock_tr_inst
            orch._init_task_runner()
        assert mock_tr_inst._on_tool_approval is not None
        assert mock_tr_inst._on_approval is not None


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _interactive_approval with thread context
# ═══════════════════════════════════════════════════════════════════════════


class TestApprovalThreadContext:
    """Approval with parent thread context."""

    @pytest.mark.asyncio
    async def test_approval_with_parent_thread(self):
        """Approval resolves parent thread for threaded prompt."""
        orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
        mock_slack = MagicMock()
        mock_slack.open_dm = AsyncMock(return_value="D_U1")
        mock_slack.post_blocks = AsyncMock(return_value="ts")
        mock_slack.update_message = AsyncMock()
        orch.slack = mock_slack
        orch.sessions = _mock_sessions()
        orch.sessions.get_channel = MagicMock(return_value="C_CHAN")
        orch.sessions.get_thread = MagicMock(return_value="thread.ts")
        ds = _mock_dashboard_state()
        ds._yolo = False
        ds._slots = {}
        orch.dashboard_state = ds

        callback = orch._interactive_approval("subagent")
        event = MagicMock()
        event.request_id = "req-thread"
        event.title = "cmd"
        event.tool_input = ""
        event.tool_purpose = ""

        with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False):
            with patch("kiro_crew.slack.handler._build_approval_blocks", return_value=[]):
                with patch("kiro_crew.slack.handler._pending_approvals", {}):
                    # flake-ok: pre-existing worker-wide wait_for patch inside the test, moved verbatim; tracked for a de-flake pass
                    with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
                        result = await callback(event, "C_CHAN:thread.ts")

        assert result is False
        # Should have posted to the channel, not DM
        mock_slack.post_blocks.assert_awaited()
        call_args = mock_slack.post_blocks.call_args
        assert call_args[0][0] == "C_CHAN"

    @pytest.mark.asyncio
    async def test_scoped_trust_not_trusted(self):
        """Slot exists but not trusted → falls through to interactive."""
        orch = _make_orchestrator(slack_enabled=False)
        ds = _mock_dashboard_state()
        ds._yolo = False
        slot = MagicMock()
        slot._trust = False
        slot.running = False
        ds._slots = {"my-slot": slot}
        ds.request_approval = AsyncMock(return_value=False)
        orch.dashboard_state = ds
        resolver = MagicMock(return_value="my-slot")
        callback = orch._interactive_approval("subagent", slot_resolver=resolver)
        event = MagicMock()
        event.request_id = "req-notrust"
        event.title = "cmd"
        event.tool_input = ""
        event.tool_purpose = ""
        with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False):
            with patch("kiro_crew.sel.sel") as mock_sel:
                mock_sel.return_value.log_api_access = MagicMock()
                result = await callback(event, "")
        assert result is False


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _init_task_runner _task_approval
# ═══════════════════════════════════════════════════════════════════════════


class TestTaskApprovalCallback:
    """Task-level approval in task runner."""

    @pytest.mark.asyncio
    async def test_task_approval_no_dashboard(self):
        """No dashboard → denies task."""
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = _mock_context_builder()
        orch.ctx_builder.hooks = MagicMock()
        orch.conv_log = MagicMock()
        orch.consolidator = MagicMock()
        orch.dashboard_state = None
        with patch("kiro_crew.slack.gateway.TaskRunner") as mock_tr:
            mock_tr_inst = MagicMock()
            mock_tr.return_value = mock_tr_inst
            orch._init_task_runner()
        approval_cb = mock_tr_inst._on_approval
        task = MagicMock()
        task.index = 1
        task.title = "Test task"
        with patch("kiro_crew.slack.gateway.sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            result = await approval_cb(task)
        assert result is False

    @pytest.mark.asyncio
    async def test_task_approval_with_dashboard(self):
        """Dashboard available → requests approval."""
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = _mock_context_builder()
        orch.ctx_builder.hooks = MagicMock()
        orch.conv_log = MagicMock()
        orch.consolidator = MagicMock()
        ds = _mock_dashboard_state()
        ds.request_approval = AsyncMock(return_value=True)
        orch.dashboard_state = ds
        with patch("kiro_crew.slack.gateway.TaskRunner") as mock_tr:
            mock_tr_inst = MagicMock()
            mock_tr.return_value = mock_tr_inst
            orch._init_task_runner()
        approval_cb = mock_tr_inst._on_approval
        task = MagicMock()
        task.index = 2
        task.title = "Approved task"
        with patch("kiro_crew.slack.gateway.sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            result = await approval_cb(task)
        assert result is True
