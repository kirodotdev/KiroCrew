"""Tests for subagent approval prompt threading.

Verifies that _interactive_approval routes approval prompts to the parent
session's Slack thread instead of posting as top-level DMs.
"""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.llm_helpers import LLMEvent
from kiro_crew.slack.handler import _PendingApproval

# ``SubagentManager.spawn`` refuses -- registering no task -- while the host
# looks short of memory, which is the runner's state, not this test's input.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")


def _make_gateway():
    from kiro_crew.slack.gateway import GatewayOrchestrator

    gateway = GatewayOrchestrator.__new__(GatewayOrchestrator)
    gateway.sessions = MagicMock()
    gateway.sessions.get_pid = MagicMock(return_value=None)
    gateway.slack = MagicMock()
    gateway.dashboard_state = MagicMock()
    gateway.dashboard_state._yolo = False
    gateway.dashboard_state._slots = {}
    gateway.dashboard_state.request_approval = AsyncMock(return_value=True)
    gateway.dashboard_state.resolve_approval = MagicMock()
    gateway._owner_id = "U000"
    gateway._cfg = MagicMock()
    gateway._cfg.hooks = MagicMock()
    gateway._cfg.hooks.get = MagicMock(return_value=[])
    gateway._cfg.agent.max_subagents = 4
    gateway.sessions.get_channel = MagicMock(return_value=None)
    gateway.sessions.get_thread = MagicMock(return_value=None)
    gateway._approval_mode = None
    return gateway


def _make_event(request_id: str = "req1", title: str = "shell: ls") -> LLMEvent:
    return LLMEvent(kind="permission_request", request_id=request_id, title=title)


def _pre_approved_pending() -> _PendingApproval:
    """Create a real _PendingApproval with its future already resolved."""
    pending = _PendingApproval(provider=None, request_id="req1")  # type: ignore[arg-type]
    pending.future.set_result("approved")
    return pending


# ── Tests: _interactive_approval threads under parent session ──


class TestApprovalThreading:
    """Approval prompts should thread under the parent conversation."""

    @pytest.mark.asyncio
    async def test_uses_stored_thread_ts(self) -> None:
        """When parent has stored channel and thread, approval uses stored thread_ts."""
        gateway = _make_gateway()
        gateway.sessions.get_channel = MagicMock(return_value="C_PARENT")
        gateway.sessions.get_thread = MagicMock(return_value="1775113012.860459")
        gateway.slack.post_blocks = AsyncMock(return_value="approval_ts")
        gateway.slack.update_message = AsyncMock()

        with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False), patch(
            "kiro_crew.slack.handler._PendingApproval", return_value=_pre_approved_pending()
        ):
            approve_fn = gateway._interactive_approval("subagent")
            result = await approve_fn(_make_event(), "1775113012.860459")

        gateway.slack.post_blocks.assert_awaited_once()
        call_args = gateway.slack.post_blocks.call_args
        assert call_args.args[0] == "C_PARENT"
        assert call_args.args[3] == "1775113012.860459"
        assert result is True

    @pytest.mark.asyncio
    async def test_uses_session_key_as_thread_ts_when_no_stored_thread(self) -> None:
        """When parent has channel but no stored thread, timestamp-shaped key is used."""
        gateway = _make_gateway()
        gateway.sessions.get_channel = MagicMock(return_value="C_PARENT")
        gateway.sessions.get_thread = MagicMock(return_value=None)
        gateway.slack.post_blocks = AsyncMock(return_value="approval_ts")
        gateway.slack.update_message = AsyncMock()

        with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False), patch(
            "kiro_crew.slack.handler._PendingApproval", return_value=_pre_approved_pending()
        ):
            approve_fn = gateway._interactive_approval("subagent")
            await approve_fn(_make_event(), "1775113012.860459")

        call_args = gateway.slack.post_blocks.call_args
        assert call_args.args[0] == "C_PARENT"
        assert call_args.args[3] == "1775113012.860459"

    @pytest.mark.asyncio
    async def test_uses_stored_thread_for_cron(self) -> None:
        """When parent is a cron session, uses get_thread() for thread_ts."""
        gateway = _make_gateway()
        gateway.sessions.get_channel = MagicMock(return_value="C_CRON")
        gateway.sessions.get_thread = MagicMock(return_value="1711957800.001234")
        gateway.slack.post_blocks = AsyncMock(return_value="approval_ts")
        gateway.slack.update_message = AsyncMock()

        with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False), patch(
            "kiro_crew.slack.handler._PendingApproval", return_value=_pre_approved_pending()
        ):
            approve_fn = gateway._interactive_approval("subagent")
            await approve_fn(_make_event(), "cron:j1")

        call_args = gateway.slack.post_blocks.call_args
        assert call_args.args[0] == "C_CRON"
        assert call_args.args[3] == "1711957800.001234"

    @pytest.mark.asyncio
    async def test_cron_key_not_used_as_thread_ts(self) -> None:
        """Cron key without stored thread must NOT be used as thread_ts."""
        gateway = _make_gateway()
        gateway.sessions.get_channel = MagicMock(return_value="C_CRON")
        gateway.sessions.get_thread = MagicMock(return_value=None)  # no thread stored yet
        gateway.slack.post_blocks = AsyncMock(return_value="approval_ts")
        gateway.slack.update_message = AsyncMock()

        with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False), patch(
            "kiro_crew.slack.handler._PendingApproval", return_value=_pre_approved_pending()
        ):
            approve_fn = gateway._interactive_approval("subagent")
            await approve_fn(_make_event(), "cron:j1")

        call_args = gateway.slack.post_blocks.call_args
        assert call_args.args[0] == "C_CRON"
        # "cron:j1" is NOT a valid Slack ts — should be None
        assert call_args.args[3] is None

    @pytest.mark.asyncio
    async def test_falls_back_to_dm_without_parent(self) -> None:
        """Without parent_session_key, falls back to DM (original behaviour)."""
        gateway = _make_gateway()
        gateway.slack.open_dm = AsyncMock(return_value="D_DM")
        gateway.slack.post_blocks = AsyncMock(return_value="approval_ts")
        gateway.slack.update_message = AsyncMock()

        with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False), patch(
            "kiro_crew.slack.handler._PendingApproval", return_value=_pre_approved_pending()
        ):
            approve_fn = gateway._interactive_approval("subagent")
            await approve_fn(_make_event(), "")

        gateway.slack.open_dm.assert_awaited_once()
        call_args = gateway.slack.post_blocks.call_args
        assert call_args.args[0] == "D_DM"
        assert call_args.args[3] is None

    @pytest.mark.asyncio
    async def test_falls_back_to_dm_for_dashboard_session(self) -> None:
        """Dashboard sessions have no Slack channel — should fall back to DM."""
        gateway = _make_gateway()
        gateway.sessions.get_channel = MagicMock(return_value=None)
        gateway.slack.open_dm = AsyncMock(return_value="D_DM")
        gateway.slack.post_blocks = AsyncMock(return_value="approval_ts")
        gateway.slack.update_message = AsyncMock()

        with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False), patch(
            "kiro_crew.slack.handler._PendingApproval", return_value=_pre_approved_pending()
        ):
            approve_fn = gateway._interactive_approval("subagent")
            await approve_fn(_make_event(), "dashboard:main")

        gateway.slack.open_dm.assert_awaited_once()


# ── Tests: subagent passes parent_session_key to approval ──


class TestSubagentPassesParentKey:
    """SubagentManager must pass parent_session_key to approval callbacks."""

    @pytest.mark.asyncio
    async def test_spawn_approval_receives_parent_session_key(self) -> None:
        """on_spawn_approval is called with parent_session_key."""
        from kiro_crew.subagent import SubagentManager

        captured_args: list = []

        async def mock_spawn_approval(
            request_id: str, description: str, parent_session_key: str = ""
        ) -> bool:
            captured_args.append((request_id, description, parent_session_key))
            return False  # reject to avoid running

        sessions = MagicMock()
        sessions.get_pid = MagicMock(return_value=None)
        sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))
        sessions.release = MagicMock()
        sessions.reset = AsyncMock()
        ctx_builder = MagicMock()
        ctx_builder.hooks = MagicMock()

        manager = SubagentManager(
            sessions=sessions,
            ctx_builder=ctx_builder,
            on_spawn_approval=mock_spawn_approval,
            is_yolo=lambda: False,
        )

        info = manager.spawn("check oncall", parent_session_key="1775113012.860459")
        assert info is not None

        # Await the spawned task directly (deterministic, no sleep)
        with contextlib.suppress(Exception):
            await manager._tasks[info.id]

        assert len(captured_args) == 1
        assert captured_args[0][2] == "1775113012.860459"

    @pytest.mark.asyncio
    async def test_tool_approval_receives_parent_session_key(self) -> None:
        """on_tool_approval is called with parent_session_key during tool requests."""
        from kiro_crew.subagent import SubagentManager

        captured: list = []

        async def mock_tool_approval(event: LLMEvent, parent_session_key: str = "") -> bool:
            captured.append(parent_session_key)
            return False  # reject to stop the loop

        # Mock client whose stream yields one permission_request then ends
        mock_client = MagicMock()
        perm_event = LLMEvent(
            kind="permission_request", request_id="tool1", title="shell: ls"
        )

        async def _stream(_msg: str):
            yield perm_event

        mock_client.stream = _stream
        mock_client.reject_tool = AsyncMock()

        sessions = MagicMock()
        sessions.get_pid = MagicMock(return_value=None)
        sessions.get_or_create = AsyncMock(return_value=(mock_client, True, False))
        sessions.get_approval_policy = MagicMock(return_value=None)
        sessions.release = MagicMock()
        sessions.reset = AsyncMock()
        ctx_builder = MagicMock()
        ctx_builder.hooks = MagicMock()
        ctx_builder.build_message = MagicMock(return_value=("task", {}))

        manager = SubagentManager(
            sessions=sessions,
            ctx_builder=ctx_builder,
            on_tool_approval=mock_tool_approval,
            on_spawn_approval=AsyncMock(return_value=True),
            is_yolo=lambda: False,
        )

        info = manager.spawn("ls /tmp", parent_session_key="1775113012.860459")
        assert info is not None

        with contextlib.suppress(Exception):
            await manager._tasks[info.id]

        assert len(captured) == 1
        assert captured[0] == "1775113012.860459"


# ── Tests: is_dm reflects actual destination ──


class TestIsDmReflectsDestination:
    """is_dm must be False when posting to a channel thread, True for DM fallback."""

    @pytest.mark.asyncio
    async def test_channel_thread_sets_is_dm_false(self) -> None:
        """Approval routed to parent channel should pass is_dm=False."""
        gateway = _make_gateway()
        gateway.sessions.get_channel = MagicMock(return_value="C_PARENT")
        gateway.sessions.get_thread = MagicMock(return_value="1775113012.860459")
        gateway.slack.post_blocks = AsyncMock(return_value="approval_ts")
        gateway.slack.update_message = AsyncMock()

        with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False), patch(
            "kiro_crew.slack.handler._PendingApproval", return_value=_pre_approved_pending()
        ), patch(
            "kiro_crew.slack.handler._build_approval_blocks", wraps=None
        ) as mock_blocks:
            mock_blocks.return_value = [{"type": "section", "text": {"type": "mrkdwn", "text": "test"}}]
            approve_fn = gateway._interactive_approval("subagent")
            await approve_fn(_make_event(), "1775113012.860459")

        mock_blocks.assert_called_once()
        assert mock_blocks.call_args.kwargs.get("is_dm") is False or mock_blocks.call_args[1].get("is_dm") is False

    @pytest.mark.asyncio
    async def test_dm_fallback_sets_is_dm_true(self) -> None:
        """Approval falling back to DM should pass is_dm=True."""
        gateway = _make_gateway()
        gateway.slack.open_dm = AsyncMock(return_value="D_DM")
        gateway.slack.post_blocks = AsyncMock(return_value="approval_ts")
        gateway.slack.update_message = AsyncMock()

        with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False), patch(
            "kiro_crew.slack.handler._PendingApproval", return_value=_pre_approved_pending()
        ), patch(
            "kiro_crew.slack.handler._build_approval_blocks", wraps=None
        ) as mock_blocks:
            mock_blocks.return_value = [{"type": "section", "text": {"type": "mrkdwn", "text": "test"}}]
            approve_fn = gateway._interactive_approval("subagent")
            await approve_fn(_make_event(), "")

        mock_blocks.assert_called_once()
        assert mock_blocks.call_args.kwargs.get("is_dm") is True or mock_blocks.call_args[1].get("is_dm") is True


# ── Tests: --approval CLI mode emits SEL audit events ──


class TestApprovalModeSelAudit:
    """`--approval yolo` and `reads` auto-approvals must emit SEL events."""

    @pytest.mark.asyncio
    async def test_yolo_emits_sel_audit_on_approve(self) -> None:
        """`--approval yolo` records the decision via sel().log_api_access."""
        gateway = _make_gateway()
        gateway._approval_mode = "yolo"

        mock_sel = MagicMock()
        mock_sel.log_api_access = MagicMock()
        with patch("kiro_crew.slack.gateway.sel", return_value=mock_sel), patch(
            "kiro_crew.slack.handler.is_yolo_mode", return_value=False
        ), patch("kiro_crew.slack.handler._PendingApproval", return_value=_pre_approved_pending()):
            approve_fn = gateway._interactive_approval("cron")
            result = await approve_fn(_make_event(title="shell: rm -rf /"), "")

        assert result is True
        mock_sel.log_api_access.assert_called_once()
        kwargs = mock_sel.log_api_access.call_args.kwargs
        assert kwargs["caller"] == "cli:approval=yolo"
        assert kwargs["operation"] == "cron.cli_approval_auto_approve"
        assert kwargs["outcome"] == "ok"
        # Title is redacted — destructive command body still passes through
        # (redaction targets credentials/URLs, not shell args), so the
        # operation context is preserved for triage.
        assert "rm" in kwargs["resources"]

    @pytest.mark.asyncio
    async def test_reads_emits_sel_audit_on_read_only_tool(self) -> None:
        """`--approval reads` records the decision when the tool is read-only."""
        gateway = _make_gateway()
        gateway._approval_mode = "reads"

        mock_sel = MagicMock()
        mock_sel.log_api_access = MagicMock()
        with patch("kiro_crew.slack.gateway.sel", return_value=mock_sel), patch(
            "kiro_crew.slack.handler.is_yolo_mode", return_value=False
        ), patch("kiro_crew.slack.handler._PendingApproval", return_value=_pre_approved_pending()):
            approve_fn = gateway._interactive_approval("subagent")
            result = await approve_fn(_make_event(title="read /tmp/foo.txt"), "")

        assert result is True
        mock_sel.log_api_access.assert_called_once()
        kwargs = mock_sel.log_api_access.call_args.kwargs
        assert kwargs["caller"] == "cli:approval=reads"
        assert kwargs["operation"] == "subagent.cli_approval_auto_approve"

    @pytest.mark.asyncio
    async def test_reads_no_sel_audit_on_write_tool_falls_through(self) -> None:
        """`--approval reads` with a write tool must NOT emit a yolo-style auto-approve event.

        The decision falls through to the standard interactive flow; SEL
        emission for the eventual decision happens elsewhere (or not at
        all, matching the rest of the function's pre-existing pattern).
        """
        gateway = _make_gateway()
        gateway._approval_mode = "reads"
        gateway.slack.post_blocks = AsyncMock(return_value="approval_ts")

        mock_sel = MagicMock()
        mock_sel.log_api_access = MagicMock()
        with patch("kiro_crew.slack.gateway.sel", return_value=mock_sel), patch(
            "kiro_crew.slack.handler.is_yolo_mode", return_value=False
        ), patch("kiro_crew.slack.handler._PendingApproval", return_value=_pre_approved_pending()):
            approve_fn = gateway._interactive_approval("subagent")
            await approve_fn(_make_event(title="shell: rm -rf /"), "")

        # No `cli_approval_auto_approve` event — the write tool fell
        # through to the standard flow, not the auto-approve path.
        for call in mock_sel.log_api_access.call_args_list:
            assert "cli_approval_auto_approve" not in call.kwargs.get("operation", "")

    @pytest.mark.asyncio
    async def test_yolo_returns_true_even_if_sel_raises(self) -> None:
        """SEL failures must NOT block the approval — fail-open on logging."""
        gateway = _make_gateway()
        gateway._approval_mode = "yolo"

        broken_sel = MagicMock(side_effect=RuntimeError("sel unavailable"))
        with patch("kiro_crew.slack.gateway.sel", broken_sel), patch(
            "kiro_crew.slack.handler.is_yolo_mode", return_value=False
        ), patch("kiro_crew.slack.handler._PendingApproval", return_value=_pre_approved_pending()):
            approve_fn = gateway._interactive_approval("cron")
            result = await approve_fn(_make_event(), "")

        assert result is True  # approval still proceeds
