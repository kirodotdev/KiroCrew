"""Tests for kiro_crew.slack.gateway — GatewayOrchestrator coverage.

This file covers construction, the dedup hash, the DM-open retry, service
initialization, the tool-approval callback, result delivery and graceful shutdown.
The orchestrator's other topics are the ``test_slack_gateway_<topic>.py`` files
split from it. They share the builders in ``slack_gateway_helpers`` with this file,
and the builders other test files import from here are re-exported below.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_gateway_helpers import _mock_context_builder  # noqa: F401
from slack_gateway_helpers import _restore_update_check_cache  # noqa: F401
from slack_gateway_helpers import (
    _LOST_RUN_SECS,
    _fake_async_proc,
    _make_orchestrator,
    _mock_dashboard_state,
    _mock_sessions,
    _within_lost_run,
)

from kiro_crew.autonudge import AutoNudgeService, NudgeLoop
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.monitoring.models import (
    MonitorActionCompletion,
    MonitorActionDisposition,
    MonitorState,
)
from kiro_crew.slack import gateway as gw
from kiro_crew.slack.gateway import _EPOCH_WINDOW_SECS, GatewayOrchestrator, _result_hash
from kiro_crew.testing.wait import async_wait_until, await_until

# ═══════════════════════════════════════════════════════════════════════════
# Tests: __init__ and constructor
# ═══════════════════════════════════════════════════════════════════════════


class TestGatewayOrchestratorInit:
    """Constructor and attribute initialization."""

    def test_default_flags(self):
        orch = _make_orchestrator()
        assert orch._no_dashboard is False
        assert orch._no_crons is False
        assert orch._no_open is False

    def test_custom_flags(self):
        orch = _make_orchestrator(no_dashboard=True, no_crons=True, no_open=True)
        assert orch._no_dashboard is True
        assert orch._no_crons is True
        assert orch._no_open is True

    def test_slack_disabled_without_tokens(self):
        orch = _make_orchestrator(slack_enabled=False)
        assert orch._slack_enabled is False
        assert orch.slack is None

    def test_slack_enabled_with_tokens(self):
        orch = _make_orchestrator(slack_enabled=True)
        assert orch._slack_enabled is True

    def test_owner_id_stored(self):
        orch = _make_orchestrator(slack_enabled=True, owner_id="U123")
        assert orch._owner_id == "U123"
        assert "U123" in orch._allowed_users

    def test_services_initially_none(self):
        orch = _make_orchestrator()
        assert orch.sessions is None
        assert orch.cron_svc is None
        assert orch.heartbeat_svc is None
        assert orch.subagent_mgr is None
        assert orch.task_runner is None
        assert orch.dashboard_state is None

    def test_tracking_channels_from_config(self):
        cfg = KiroCrewConfig()
        cfg.slack.tracking_channels = [{"channel_id": "C1"}, {"channel_id": "C2"}]
        with patch.object(cfg, "load_credentials", return_value={"owner_id": "U1"}):
            orch = GatewayOrchestrator(cfg)
        assert orch._tracking_channels == {"C1", "C2"}

    def test_open_channels_from_config(self):
        cfg = KiroCrewConfig()
        cfg.slack.open_channels = ["C_OPEN"]
        with patch.object(cfg, "load_credentials", return_value={}):
            orch = GatewayOrchestrator(cfg)
        assert "C_OPEN" in orch._open_channels

    def test_stale_allowed_users_pruned(self):
        cfg = KiroCrewConfig()
        cfg.slack.allowed_users = [{"slack_id": "U_STALE"}]
        with patch.object(
            cfg,
            "load_credentials",
            return_value={
                "SLACK_APP_TOKEN": "xapp-t",
                "SLACK_BOT_TOKEN": "xoxb-t",
                "KIROCREW_OWNER_ID": "U_OWNER",
            },
        ):
            orch = GatewayOrchestrator(cfg)
        assert "U_STALE" not in orch._allowed_users
        assert "U_OWNER" in orch._allowed_users


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _result_hash utility
# ═══════════════════════════════════════════════════════════════════════════


class TestResultHash:
    """Dedup hash strips volatile data."""

    def test_stable_text_produces_consistent_hash(self):
        assert _result_hash("hello world") == _result_hash("hello world")

    def test_different_text_different_hash(self):
        assert _result_hash("foo") != _result_hash("bar")

    @pytest.mark.parametrize(
        "first, second",
        [
            pytest.param(
                "deployed at 2026-01-15T10:30:00Z successfully",
                "deployed at 2026-05-20T22:00:00+05:00 successfully",
                id="test_strips_iso_timestamps",
            ),
            pytest.param(
                "id=550e8400-e29b-41d4-a716-446655440000 done",
                "id=a1b2c3d4-e5f6-7890-abcd-ef1234567890 done",
                id="test_strips_uuids",
            ),
        ],
    )
    def test_strips_volatile_ids(self, first, second):
        assert _result_hash(first) == _result_hash(second)

    def test_strips_epoch_within_window(self):
        now_epoch = str(int(time.time()))
        a = _result_hash(f"ts={now_epoch} ok")
        b = _result_hash("ts= ok")
        assert a == b

    def test_preserves_epoch_outside_window(self):
        old_epoch = str(int(time.time()) - _EPOCH_WINDOW_SECS - 1000)
        a = _result_hash(f"ts={old_epoch} ok")
        b = _result_hash("ts= ok")
        assert a != b

    def test_hash_length_is_16(self):
        assert len(_result_hash("anything")) == 16

    def test_millis_epoch_stripped(self):
        now_ms = str(int(time.time() * 1000))
        a = _result_hash(f"ts={now_ms} ok")
        b = _result_hash("ts= ok")
        assert a == b


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _open_dm_with_retry
# ═══════════════════════════════════════════════════════════════════════════


class TestOpenDmWithRetry:
    """Retry logic for open_dm."""

    @pytest.fixture(autouse=True)
    def _no_backoff(self, monkeypatch):
        """Skip the production linear backoff's real sleep (1s then 2s).

        These tests assert the retry COUNT and the final result, never the delay, so
        the 5s this class spent asleep bought no coverage. The retry loop still runs.
        """
        monkeypatch.setattr("kiro_crew.slack.retry.asyncio.sleep", AsyncMock(return_value=None))

    @pytest.mark.asyncio
    async def test_success_first_attempt(self):
        orch = _make_orchestrator(slack_enabled=True)
        mock_slack = MagicMock()
        mock_slack.open_dm = AsyncMock(return_value="D_CHAN")
        orch.slack = mock_slack
        result = await orch._open_dm_with_retry("U1", "test-job")
        assert result == "D_CHAN"
        assert mock_slack.open_dm.call_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_server_error(self):
        from slack_sdk.errors import SlackApiError

        orch = _make_orchestrator(slack_enabled=True)
        mock_slack = MagicMock()
        resp = MagicMock()
        resp.status_code = 500
        err = SlackApiError("server error", resp)
        mock_slack.open_dm = AsyncMock(side_effect=[err, "D_OK"])
        orch.slack = mock_slack
        result = await orch._open_dm_with_retry("U1", "job", max_attempts=2)
        assert result == "D_OK"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status_code, max_attempts, expected_calls",
        [
            pytest.param(403, 3, 1, id="test_raises_on_non_retryable"),
            pytest.param(500, 2, 2, id="test_raises_after_max_attempts"),
        ],
    )
    async def test_raises_when_not_retried_to_success(
        self, status_code, max_attempts, expected_calls
    ):
        from slack_sdk.errors import SlackApiError

        orch = _make_orchestrator(slack_enabled=True)
        mock_slack = MagicMock()
        resp = MagicMock()
        resp.status_code = status_code
        err = SlackApiError("slack error", resp)
        mock_slack.open_dm = AsyncMock(side_effect=err)
        orch.slack = mock_slack
        with pytest.raises(SlackApiError):
            await orch._open_dm_with_retry("U1", "job", max_attempts=max_attempts)
        assert mock_slack.open_dm.call_count == expected_calls

    @pytest.mark.asyncio
    async def test_returns_none_when_slack_is_none(self):
        orch = _make_orchestrator(slack_enabled=False)
        result = await orch._open_dm_with_retry("U1", "job")
        assert result is None

    @pytest.mark.asyncio
    async def test_retries_on_rate_limit(self):
        from slack_sdk.errors import SlackApiError

        orch = _make_orchestrator(slack_enabled=True)
        mock_slack = MagicMock()
        resp = MagicMock()
        resp.status_code = 429
        err = SlackApiError("rate limited", resp)
        mock_slack.open_dm = AsyncMock(side_effect=[err, err, "D_OK"])
        orch.slack = mock_slack
        result = await orch._open_dm_with_retry("U1", "job", max_attempts=3)
        assert result == "D_OK"


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _init_services
# ═══════════════════════════════════════════════════════════════════════════


class TestInitServices:
    """Service initialization cluster."""

    def test_init_services_creates_all(self):
        orch = _make_orchestrator(slack_enabled=True)
        with patch("kiro_crew.slack.gateway.MemoryStore") as mock_mem:
            mock_mem_inst = MagicMock()
            mock_mem_inst.init = MagicMock()
            mock_mem_inst.rebuild_index = MagicMock(return_value=5)
            mock_mem.return_value = mock_mem_inst
            with patch("kiro_crew.vector_memory.VectorMemoryStore") as mock_vm:
                mock_vm_inst = MagicMock()
                mock_vm_inst.init = MagicMock()
                mock_vm.return_value = mock_vm_inst
                with patch("kiro_crew.slack.gateway.SkillsLoader"):
                    with patch("kiro_crew.slack.gateway.HookManager"):
                        with patch("kiro_crew.slack.gateway.LessonStore"):
                            with patch("kiro_crew.slack.gateway.ContextBuilder"):
                                with patch("kiro_crew.slack.gateway.ConversationLog") as mock_cl:
                                    mock_cl_inst = MagicMock()
                                    mock_cl_inst.init = MagicMock()
                                    mock_cl.return_value = mock_cl_inst
                                    with patch("kiro_crew.slack.gateway.SessionManager"):
                                        with patch("kiro_crew.slack.gateway.HistoryConsolidator"):
                                            with patch("kiro_crew.slack.gateway.ChannelHistory"):
                                                with patch(
                                                    "kiro_crew.agent.rebuild_agent_config",
                                                    return_value=Path("/tmp/a"),
                                                ):
                                                    with patch(
                                                        "asyncio.create_subprocess_exec",
                                                        new=AsyncMock(
                                                            return_value=_fake_async_proc(
                                                                stdout=b"kiro-cli 1.30.0"
                                                            )
                                                        ),
                                                    ):
                                                        try:
                                                            asyncio.run(orch._init_services())
                                                            mock_mem_inst.init.assert_not_called()
                                                            mock_vm_inst.init.assert_not_called()
                                                            mock_mem_inst.rebuild_index.assert_not_called()
                                                        finally:
                                                            orch._stop_memory_startup()

        assert orch.sessions is not None
        assert orch.ctx_builder is not None
        assert orch.conv_log is not None
        assert orch.consolidator is not None
        assert orch.channel_history is not None

    def test_init_services_dashboard_only_mode(self):
        orch = _make_orchestrator(slack_enabled=False)
        with patch("kiro_crew.slack.gateway.MemoryStore") as mock_mem:
            mock_mem_inst = MagicMock()
            mock_mem_inst.init = MagicMock()
            mock_mem_inst.rebuild_index = MagicMock(return_value=0)
            mock_mem.return_value = mock_mem_inst
            with patch("kiro_crew.vector_memory.VectorMemoryStore") as mock_vm:
                mock_vm_inst = MagicMock()
                mock_vm_inst.init = MagicMock()
                mock_vm.return_value = mock_vm_inst
                with patch("kiro_crew.slack.gateway.SkillsLoader"):
                    with patch("kiro_crew.slack.gateway.HookManager"):
                        with patch("kiro_crew.slack.gateway.LessonStore"):
                            with patch("kiro_crew.slack.gateway.ContextBuilder"):
                                with patch("kiro_crew.slack.gateway.ConversationLog") as mock_cl:
                                    mock_cl_inst = MagicMock()
                                    mock_cl_inst.init = MagicMock()
                                    mock_cl.return_value = mock_cl_inst
                                    with patch("kiro_crew.slack.gateway.SessionManager"):
                                        with patch("kiro_crew.slack.gateway.HistoryConsolidator"):
                                            with patch("kiro_crew.slack.gateway.ChannelHistory"):
                                                with patch(
                                                    "kiro_crew.agent.rebuild_agent_config",
                                                    return_value=Path("/tmp/a"),
                                                ):
                                                    with patch(
                                                        "asyncio.create_subprocess_exec",
                                                        new=AsyncMock(
                                                            return_value=_fake_async_proc(
                                                                stdout=b"kiro-cli 1.30.0"
                                                            )
                                                        ),
                                                    ):
                                                        try:
                                                            asyncio.run(orch._init_services())
                                                            mock_mem_inst.init.assert_not_called()
                                                            mock_vm_inst.init.assert_not_called()
                                                        finally:
                                                            orch._stop_memory_startup()

        assert orch.slack is None
        assert orch.sessions is not None


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _interactive_approval
# ═══════════════════════════════════════════════════════════════════════════


class TestInteractiveApproval:
    """Tool approval callback logic."""

    @pytest.mark.asyncio
    async def test_auto_approve_when_no_ui(self):
        """No slack, no dashboard → auto-approve."""
        orch = _make_orchestrator(slack_enabled=False)
        orch.dashboard_state = None
        callback = orch._interactive_approval("cron")
        event = MagicMock()
        event.request_id = "req-1"
        event.title = "bash: ls"
        event.tool_input = ""
        event.tool_purpose = ""
        result = await callback(event, "")
        assert result is True

    @pytest.mark.asyncio
    async def test_yolo_mode_approves(self):
        """YOLO mode → auto-approve."""
        orch = _make_orchestrator(slack_enabled=False)
        orch.dashboard_state = _mock_dashboard_state()
        orch.dashboard_state._yolo = True
        callback = orch._interactive_approval("subagent")
        event = MagicMock()
        event.request_id = "req-2"
        event.title = "dangerous command"
        event.tool_input = ""
        event.tool_purpose = ""
        with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False):
            result = await callback(event, "")
        assert result is True

    @pytest.mark.asyncio
    async def test_slack_yolo_mode_approves(self):
        """Slack YOLO mode → auto-approve."""
        orch = _make_orchestrator(slack_enabled=False)
        orch.dashboard_state = None
        callback = orch._interactive_approval("cron")
        event = MagicMock()
        event.request_id = "req-3"
        event.title = "cmd"
        event.tool_input = ""
        event.tool_purpose = ""
        with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=True):
            result = await callback(event, "")
        assert result is True

    @pytest.mark.asyncio
    async def test_dashboard_only_approval(self):
        """Dashboard approval without Slack."""
        orch = _make_orchestrator(slack_enabled=False)
        ds = _mock_dashboard_state()
        ds.request_approval = AsyncMock(return_value=False)
        ds._yolo = False
        orch.dashboard_state = ds
        callback = orch._interactive_approval("heartbeat")
        event = MagicMock()
        event.request_id = "req-4"
        event.title = "rm -rf /"
        event.tool_input = ""
        event.tool_purpose = ""
        with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False):
            result = await callback(event, "")
        assert result is False
        ds.request_approval.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_scoped_trust_auto_approves(self):
        """Slot with _trust=True → auto-approve."""
        orch = _make_orchestrator(slack_enabled=False)
        ds = _mock_dashboard_state()
        ds._yolo = False
        slot = MagicMock()
        slot._trust = True
        slot.running = True
        ds._slots = {"my-slot": slot}
        orch.dashboard_state = ds
        resolver = MagicMock(return_value="my-slot")
        callback = orch._interactive_approval("subagent", slot_resolver=resolver)
        event = MagicMock()
        event.request_id = "req-5"
        event.title = "safe cmd"
        event.tool_input = ""
        event.tool_purpose = ""
        with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False):
            with patch("kiro_crew.sel.sel") as mock_sel:
                mock_sel.return_value.log_api_access = MagicMock()
                result = await callback(event, "")
        assert result is True

    @pytest.mark.asyncio
    async def test_parent_session_beats_spawn_resolver_for_tool_trust(self):
        """Opaque tool request IDs still inherit trust from the parent dashboard slot."""
        orch = _make_orchestrator(slack_enabled=False)
        ds = _mock_dashboard_state()
        ds._yolo = False
        slot = MagicMock()
        slot._trust = True
        slot.running = False
        ds._slots = {"parent-slot": slot}
        ds.request_approval = AsyncMock(return_value=False)
        orch.dashboard_state = ds
        resolver = MagicMock(return_value="")
        callback = orch._interactive_approval("subagent", slot_resolver=resolver)
        event = MagicMock()
        event.request_id = "opaque-tool-request-id"
        event.title = "git diff"
        event.tool_input = ""
        event.tool_purpose = ""

        with patch("kiro_crew.sel.sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            result = await callback(event, "dashboard:parent-slot")

        assert result is True
        resolver.assert_not_called()
        ds.request_approval.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_parent_session_routes_tool_approval_when_spawn_resolver_misses(self):
        """Tool approval renders in its parent slot even though its ID is not spawn:<id>."""
        orch = _make_orchestrator(slack_enabled=False)
        ds = _mock_dashboard_state()
        ds._yolo = False
        slot = MagicMock()
        slot._trust = False
        slot.running = False
        ds._slots = {"parent-slot": slot}
        ds.request_approval = AsyncMock(return_value=False)
        orch.dashboard_state = ds
        resolver = MagicMock(return_value="")
        callback = orch._interactive_approval("subagent", slot_resolver=resolver)
        event = MagicMock()
        event.request_id = "opaque-tool-request-id"
        event.title = "git diff"
        event.tool_input = ""
        event.tool_purpose = "review changes"

        with patch("kiro_crew.sel.sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            result = await callback(event, "dashboard:parent-slot")

        assert result is False
        resolver.assert_not_called()
        ds.request_approval.assert_awaited_once_with(
            "opaque-tool-request-id",
            "subagent",
            "git diff",
            tool_input="",
            tool_purpose="review changes",
            slot="parent-slot",
            is_background=False,
        )

    @pytest.mark.asyncio
    async def test_all_slots_trusted_does_not_auto_approve(self):
        """All slots trusted, no resolver → still PROMPTS (no implicit trust).

        Session trust speaks for a chat session, not for an unattended job, and
        with one trusted chat open the `all()` test is trivially satisfied.
        Asserting the return value alone would pass vacuously, because the
        mocked `request_approval` also returns True -- so assert the prompt was
        actually raised.
        """
        orch = _make_orchestrator(slack_enabled=False)
        ds = _mock_dashboard_state()
        ds._yolo = False
        slot1 = MagicMock()
        slot1._trust = True
        slot1.running = False
        ds._slots = {"s1": slot1}
        orch.dashboard_state = ds
        callback = orch._interactive_approval("cron")
        event = MagicMock()
        event.request_id = "req-6"
        event.title = "cmd"
        event.tool_input = ""
        event.tool_purpose = ""
        with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False):
            with patch("kiro_crew.sel.sel") as mock_sel:
                mock_sel.return_value.log_api_access = MagicMock()
                await callback(event, "")
        ds.request_approval.assert_awaited_once()
        assert ds.request_approval.await_args.args[:3] == ("req-6", "cron", "cmd")

    @pytest.mark.asyncio
    async def test_auto_approve_sources_config(self):
        """Source in auto_approve_sources config → auto-approve."""
        cfg = KiroCrewConfig()
        cfg.hooks = {"auto_approve_sources": ["cron"]}
        with patch.object(cfg, "load_credentials", return_value={}):
            orch = GatewayOrchestrator(cfg)
        orch.dashboard_state = _mock_dashboard_state()
        orch.dashboard_state._yolo = False
        orch.dashboard_state._slots = {}
        callback = orch._interactive_approval("cron")
        event = MagicMock()
        event.request_id = "req-7"
        event.title = "auto"
        event.tool_input = ""
        event.tool_purpose = ""
        with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False):
            result = await callback(event, "")
        assert result is True


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _deliver_result
# ═══════════════════════════════════════════════════════════════════════════


class TestDeliverResult:
    """Result routing to various surfaces."""

    @pytest.mark.asyncio
    async def test_silent_logs_only(self, caplog):
        orch = _make_orchestrator()
        orch.slack = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        with caplog.at_level(logging.INFO, logger="kiro_crew.slack.gateway"):
            await orch._deliver_result("Title", "summary", "result", "silent")
        messages = [r.getMessage() for r in caplog.records if r.name == "kiro_crew.slack.gateway"]
        assert "Title (silent): summary" in messages
        orch.dashboard_state.notify.assert_not_called()
        assert orch.slack.method_calls == []

    @pytest.mark.asyncio
    async def test_dashboard_new_slot(self):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        slot = MagicMock()
        slot.append = MagicMock()
        ds.get_or_create_slot = MagicMock(return_value=slot)
        orch.dashboard_state = ds
        await orch._deliver_result("Title", "task", "result", "dashboard")
        slot.append.assert_called_once_with("assistant", "Title\n\nresult", "msg msg-a")
        ds.notify.assert_called_once_with(
            "heartbeat", "Title", "task\n\nresult", meta={"slot": slot.key}
        )

    @pytest.mark.asyncio
    async def test_dashboard_specific_slot(self):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        slot = MagicMock()
        slot.append = MagicMock()
        slot.key = "my-slot"
        ds.resolve_slot = MagicMock(return_value=slot)
        orch.dashboard_state = ds
        with patch("kiro_crew.slack.gateway.sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            await orch._deliver_result("Title", "task", "result", "dashboard:my-slot")
        ds.resolve_slot.assert_called_with("my-slot")
        slot.append.assert_called_once_with("assistant", "Title\n\nresult", "msg msg-a")
        ds.notify.assert_called_once_with(
            "heartbeat", "Title", "task\n\nresult", meta={"slot": "my-slot"}
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "deliver, operation",
        [
            pytest.param(
                "dashboard:gone", "heartbeat_inject_deliver", id="test_dashboard_slot_not_found"
            ),
            pytest.param(
                "prompt:dashboard:gone",
                "heartbeat_prompt_deliver",
                id="test_prompt_dashboard_slot_not_found",
            ),
        ],
    )
    async def test_dashboard_target_slot_not_found(self, deliver, operation):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        ds.resolve_slot = MagicMock(return_value=None)
        orch.dashboard_state = ds
        with patch("kiro_crew.slack.gateway.sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            await orch._deliver_result("Title", "task", "result", deliver)
        ds.resolve_slot.assert_called_once_with("gone")
        mock_sel.return_value.log_api_access.assert_called_once_with(
            caller="heartbeat",
            operation=operation,
            outcome="not_found",
            source="gateway",
            resources="requested=gone",
        )
        ds.notify.assert_not_called()
        ds.push_slots_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_slack_dm_delivery(self):
        from conftest import MockSlackClient

        orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
        mock_slack = MockSlackClient()
        orch.slack = mock_slack
        orch.dashboard_state = None
        await orch._deliver_result("Title", "task", "result", "slack")
        assert any(a[0] == "open_dm" for a in mock_slack.actions)
        assert any(a[0] == "post" for a in mock_slack.actions)

    @pytest.mark.asyncio
    async def test_slack_thread_delivery(self, monkeypatch):
        from conftest import MockSlackClient

        # The deliver tag is agent-writable, so the named channel must be tracked
        # (or be the owner's own DM channel) before an unattended report is posted
        # into it — see test_slack_heartbeat_channel_deliver.py for the refusals.
        monkeypatch.setattr("kiro_crew.slack.gateway.is_tracked_channel", lambda c: c == "C123")
        orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
        mock_slack = MockSlackClient()
        orch.slack = mock_slack
        orch.dashboard_state = _mock_dashboard_state()
        await orch._deliver_result("Title", "task", "result", "slack:C123:1234.5678")
        posts = [a for a in mock_slack.actions if a[0] == "post"]
        assert len(posts) == 1
        assert posts[0][1]["thread_ts"] == "1234.5678"

    @pytest.mark.asyncio
    async def test_default_deliver_slack_and_dashboard(self):
        from conftest import MockSlackClient

        orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
        mock_slack = MockSlackClient()
        orch.slack = mock_slack
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        await orch._deliver_result("Title", "task", "result", "")
        assert any(a[0] == "post" for a in mock_slack.actions)
        ds.notify.assert_called_once()

    @pytest.mark.asyncio
    async def test_prompt_dashboard_slot(self):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        slot = MagicMock()
        slot.key = "s1"
        slot.enqueue_or_run_prompt = MagicMock(return_value=True)
        slot.queue_depth = 0
        ds.resolve_slot = MagicMock(return_value=slot)
        orch.dashboard_state = ds
        with patch("kiro_crew.slack.gateway.sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            await orch._deliver_result("Title", "task", "result", "prompt:dashboard:s1")
        slot.enqueue_or_run_prompt.assert_called_once()
        prompt, _runner, state = slot.enqueue_or_run_prompt.call_args.args
        assert prompt == "Title\n\nresult"
        assert state is ds
        ds.notify.assert_called_once_with(
            "heartbeat", "Title", "task\n\nresult", meta={"slot": "s1"}
        )

    @pytest.mark.asyncio
    async def test_prompt_dashboard_queued(self):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        slot = MagicMock()
        slot.key = "s1"
        slot.enqueue_or_run_prompt = MagicMock(return_value=False)
        slot.queue_depth = 2
        ds.resolve_slot = MagicMock(return_value=slot)
        orch.dashboard_state = ds
        with patch("kiro_crew.slack.gateway.sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            await orch._deliver_result("Title", "task", "result", "prompt:dashboard:s1")
        assert slot.enqueue_or_run_prompt.call_args.args[0] == "Title\n\nresult"
        ds.notify.assert_not_called()
        ds.push_slots_update.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _shutdown
# ═══════════════════════════════════════════════════════════════════════════


class TestShutdown:
    """Graceful shutdown."""

    def test_autonudge_service_has_a_declared_default(self):
        """`_init_autonudge` returns before assigning when the flag is off.

        `KIROCREW_AUTONUDGE=0` makes `_init_autonudge` return at its feature-flag
        guard, before its only `self.autonudge_svc = ...`. Without a declaration
        the attribute does not exist, and the seven `if self.autonudge_svc:`
        sites in the loop-CRUD handlers raise AttributeError instead of reading
        a default -- unlike `cron_svc` and `heartbeat_svc`, which are declared.
        """
        orch = _make_orchestrator()
        assert orch.autonudge_svc is None

    @pytest.mark.asyncio
    async def test_shutdown_with_no_services(self):
        orch = _make_orchestrator()
        await orch._shutdown()
        assert orch._memory_repair_stop.is_set()

    @pytest.mark.asyncio
    async def test_shutdown_awaits_autonudge_drain(self):
        orch = _make_orchestrator()
        orch.autonudge_svc = MagicMock()
        orch.autonudge_svc.shutdown = AsyncMock()
        orch.autonudge_svc.settle_admitted_writes = AsyncMock(return_value=0)

        await orch._shutdown()

        orch.autonudge_svc.shutdown.assert_awaited_once()
        orch.autonudge_svc.settle_admitted_writes.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_shutdown_drains_autonudge_before_session_storage_close(self):
        orch = _make_orchestrator()
        started = asyncio.Event()
        release = asyncio.Event()
        order: list[str] = []

        async def drain_autonudge():
            order.append("autonudge-start")
            started.set()
            await _within_lost_run(release.wait(), "the AutoNudge drain release")
            order.append("autonudge-durable")

        async def close_sessions():
            order.append("sessions-close")

        orch.autonudge_svc = MagicMock()
        orch.autonudge_svc.shutdown = AsyncMock(side_effect=drain_autonudge)
        orch.autonudge_svc.settle_admitted_writes = AsyncMock(return_value=0)
        orch.sessions = _mock_sessions()
        orch.sessions.close_all = AsyncMock(side_effect=close_sessions)

        shutdown = asyncio.create_task(orch._shutdown())
        await _within_lost_run(started.wait(), "the AutoNudge drain start")
        await asyncio.sleep(0)
        assert "sessions-close" not in order

        release.set()
        await _within_lost_run(shutdown, "the shutdown waiting on the AutoNudge drain")

        assert order.index("autonudge-durable") < order.index("sessions-close")

    @pytest.mark.asyncio
    async def test_shutdown_keeps_autonudge_published_through_dashboard_cleanup(
        self, tmp_path, monkeypatch
    ):
        """A close still reaches the closed service until the dashboard is gone."""
        from kiro_crew import autonudge as autonudge_module

        monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")
        service = AutoNudgeService(base_dir=tmp_path)
        await _within_lost_run(service.start(), "the AutoNudge test service start")
        observed: list[tuple[bool, object | None]] = []

        async def cleanup_dashboard():
            observed.append((service._accepting_mutations, autonudge_module.get_instance()))

        orch = _make_orchestrator()
        orch.autonudge_svc = service
        orch._dashboard_runner = MagicMock()
        orch._dashboard_runner.cleanup = AsyncMock(side_effect=cleanup_dashboard)
        try:
            await _within_lost_run(
                orch._shutdown(),
                "gateway shutdown through dashboard cleanup",
            )
            assert observed == [
                (False, service)
            ], "the singleton was withdrawn before the dashboard runner stopped"
            assert (
                autonudge_module.get_instance() is None
            ), "the singleton survived the completed gateway shutdown"
        finally:
            service.stop()

    @pytest.mark.asyncio
    async def test_interrupted_shutdown_keeps_autonudge_published_through_owned_cleanup(
        self, tmp_path, monkeypatch
    ):
        """The owned teardown task has the same publication boundary."""
        from kiro_crew import autonudge as autonudge_module

        monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")
        service = AutoNudgeService(base_dir=tmp_path)
        await _within_lost_run(service.start(), "the interrupted AutoNudge test start")
        drained = asyncio.Event()
        real_shutdown = service.shutdown

        async def interrupt_after_drain():
            await real_shutdown()
            drained.set()
            raise asyncio.CancelledError()

        observed: list[tuple[bool, object | None]] = []

        async def cleanup_dashboard():
            observed.append((drained.is_set(), autonudge_module.get_instance()))

        monkeypatch.setattr(service, "shutdown", interrupt_after_drain)
        orch = _make_orchestrator()
        orch.autonudge_svc = service
        orch._dashboard_runner = MagicMock()
        orch._dashboard_runner.cleanup = AsyncMock(side_effect=cleanup_dashboard)
        try:
            with pytest.raises(asyncio.CancelledError):
                await _within_lost_run(
                    orch._shutdown(),
                    "the interrupted gateway's owned teardown",
                )
            assert observed == [
                (True, service)
            ], "the owned teardown withdrew the singleton before dashboard cleanup"
            assert (
                autonudge_module.get_instance() is None
            ), "the singleton survived the completed owned teardown"
        finally:
            service.stop()

    @pytest.mark.asyncio
    async def test_started_dashboard_accounting_is_durable_before_session_close(
        self,
        tmp_path,
    ):
        service = AutoNudgeService(base_dir=tmp_path)
        loop = NudgeLoop(
            id="monitor1",
            slot_key="chat-1-123",
            message="inspect the changed pull request",
            monitor=MonitorState(
                kind="github_pull_request",
                target="owner/repo#123",
                objective="review_ready",
                created_ts=1_000.0,
            ),
        )
        service._loops[loop.id] = loop
        assert await service.mark_monitor_action_in_flight(
            loop.id,
            "failure-a",
            now=1_100.0,
        )
        await service._lock.acquire()
        completion = asyncio.create_task(
            service.record_monitor_turn_completion(
                MonitorActionCompletion(
                    monitor_id=loop.id,
                    fingerprint="failure-a",
                    disposition=MonitorActionDisposition.SUCCESS,
                    completed_ts=1_120.0,
                    input_tokens=12,
                    output_tokens=4,
                )
            )
        )
        for _ in range(100):
            if service._inflight_adds:
                break
            await asyncio.sleep(0)
        else:
            raise AssertionError("dashboard accounting never entered the service drain")

        sessions = _mock_sessions()

        async def close_sessions():
            assert service._inflight_adds == set()
            restored = AutoNudgeService(base_dir=tmp_path)
            await asyncio.to_thread(restored._load)
            restored_loop = restored.get_by_id(loop.id)
            assert restored_loop is not None and restored_loop.monitor is not None
            assert restored_loop.monitor.agent_turns == 1
            assert restored_loop.monitor.total_tokens == 16

        sessions.close_all = AsyncMock(side_effect=close_sessions)
        orch = _make_orchestrator()
        orch.autonudge_svc = service
        orch.sessions = sessions

        async def release_after_shutdown_closes_admission():
            async def wait_for_closed_admission():
                while service._accepting_mutations:
                    await asyncio.sleep(0)

            await asyncio.wait_for(wait_for_closed_admission(), timeout=_LOST_RUN_SECS)
            sessions.close_all.assert_not_awaited()
            service._lock.release()

        release = asyncio.create_task(release_after_shutdown_closes_admission())
        shutdown = asyncio.create_task(orch._shutdown())
        try:
            await _within_lost_run(shutdown, "shutdown after dashboard accounting")
            await _within_lost_run(completion, "dashboard completion accounting")
            await _within_lost_run(release, "the admission-close release task")
        finally:
            if service._lock.locked():
                service._lock.release()
            pending = [task for task in (completion, release, shutdown) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                await _within_lost_run(
                    asyncio.gather(*pending, return_exceptions=True),
                    "the dashboard accounting test cleanup",
                )

        sessions.close_all.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cancelled_autonudge_shutdown_continues_gateway_cleanup(self):
        orch = _make_orchestrator()
        started = asyncio.Event()
        release = asyncio.Event()
        drained = asyncio.Event()

        async def durable_shutdown():
            started.set()
            drain = asyncio.create_task(
                _within_lost_run(release.wait(), "the durable AutoNudge drain release")
            )
            interrupted = False
            try:
                await asyncio.shield(drain)
            except asyncio.CancelledError:
                interrupted = True
                while not drain.done():
                    try:
                        await asyncio.shield(drain)
                    except asyncio.CancelledError:
                        continue
            drained.set()
            if interrupted:
                raise asyncio.CancelledError()

        orch.autonudge_svc = MagicMock()
        orch.autonudge_svc.shutdown = AsyncMock(side_effect=durable_shutdown)
        orch.cron_svc = MagicMock()
        orch.cron_svc.stop = AsyncMock()
        orch.heartbeat_svc = MagicMock()
        orch.heartbeat_svc.stop = MagicMock()

        shutdown = asyncio.create_task(orch._shutdown())
        await _within_lost_run(started.wait(), "the cancellable AutoNudge drain start")
        shutdown.cancel()
        await asyncio.sleep(0)
        assert not shutdown.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await _within_lost_run(shutdown, "the cancelled AutoNudge drain")

        assert drained.is_set()
        orch.cron_svc.stop.assert_awaited_once()
        orch.heartbeat_svc.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_wedged_autonudge_drain_is_cut_so_the_teardown_still_runs(self, monkeypatch):
        """A drain whose write never settles is cut on its own budget. Its window
        closes inside the graceful bound, and the teardown after it runs in full
        instead of being cancelled before its first step."""
        monkeypatch.setattr(gw, "GRACEFUL_SHUTDOWN_SECS", 1.0)
        # Absent before the drain had a budget of its own; set so a regression
        # reaches the graceful bound below instead of failing on the attribute.
        monkeypatch.setattr(gw, "PERSISTENCE_DRAIN_GRACE_SECS", 0.05, raising=False)
        orch = _make_orchestrator()
        drain_cut = asyncio.Event()

        async def wedged_drain():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # The post-cancellation window closes on the still-wedged write.
                drain_cut.set()
                raise

        orch.autonudge_svc = MagicMock()
        orch.autonudge_svc.shutdown = AsyncMock(side_effect=wedged_drain)
        orch.autonudge_svc.settle_admitted_writes = AsyncMock(return_value=0)
        orch.cron_svc = MagicMock()
        orch.cron_svc.stop = AsyncMock()
        orch.heartbeat_svc = MagicMock()
        orch.heartbeat_svc.stop = MagicMock()
        orch.sessions = _mock_sessions()

        # Under the graceful bound ``_shutdown_and_exit`` applies. It returns
        # normally: the cut was this method's own, not that bound.
        await _within_lost_run(
            asyncio.wait_for(orch._shutdown(), timeout=gw.GRACEFUL_SHUTDOWN_SECS),
            "shutdown past a wedged AutoNudge drain",
        )

        assert drain_cut.is_set()
        orch.cron_svc.stop.assert_awaited_once()
        orch.heartbeat_svc.stop.assert_called_once()
        orch.sessions.close_all.assert_awaited_once()

    @staticmethod
    async def _cut_drain_over_a_slow_write(tmp_path, monkeypatch, *, graceful_secs: float):
        """A real service whose admitted write outlasts the gateway's drain cut.

        Returns (orch, service, add_task, release, finished): the write blocks on the
        ``release`` threading event and sets ``finished`` once the real store write
        has landed.
        """
        from kiro_crew import autonudge as autonudge_module

        monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")
        monkeypatch.setattr(gw, "GRACEFUL_SHUTDOWN_SECS", graceful_secs)
        monkeypatch.setattr(gw, "PERSISTENCE_DRAIN_GRACE_SECS", 0.05)
        monkeypatch.setattr(autonudge_module, "_DRAIN_CANCEL_GRACE_SECS", 0.05)
        service = AutoNudgeService(base_dir=tmp_path)
        await _within_lost_run(service.start(), "the slow-write AutoNudge test start")
        real_write = service._write_state
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def slow_write(payload):
            started.set()
            assert release.wait(timeout=_LOST_RUN_SECS)
            real_write(payload)
            finished.set()

        monkeypatch.setattr(service, "_write_state", slow_write)
        add_task = asyncio.create_task(
            service.add(slot_key="slot-slow-write", message="later", idle_secs=60)
        )
        await async_wait_until(started.is_set)
        orch = _make_orchestrator()
        orch.autonudge_svc = service
        orch.sessions = _mock_sessions()
        return orch, service, add_task, release, finished

    @pytest.mark.asyncio
    async def test_a_write_slower_than_the_drain_cut_lands_before_shutdown_returns(
        self, tmp_path, monkeypatch
    ):
        """The gateway's own drain cut is not durability. A write still running past
        it lands inside the graceful budget before ``_shutdown`` returns, so the hard
        exit after it cannot abandon the admitted row."""
        orch, service, add_task, release, finished = await self._cut_drain_over_a_slow_write(
            tmp_path, monkeypatch, graceful_secs=_LOST_RUN_SECS
        )
        real_teardown = orch._stop_services_and_sessions

        async def teardown_then_release(**kwargs):
            await real_teardown(**kwargs)
            # Released on the next loop pass, so the write cannot land before the
            # teardown has returned: only a wait after it can observe the write.
            asyncio.get_running_loop().call_soon(release.set)

        orch._stop_services_and_sessions = teardown_then_release

        async def shutdown_then_observe() -> bool:
            await orch._shutdown()
            return finished.is_set()

        try:
            landed = await _within_lost_run(
                shutdown_then_observe(), "shutdown past the AutoNudge drain cut"
            )
            assert landed, "_shutdown returned to the hard exit with the admitted write pending"
            loop = await _within_lost_run(add_task, "the admitted slow add")
        finally:
            release.set()
            if not add_task.done():
                await _within_lost_run(
                    asyncio.gather(add_task, return_exceptions=True), "the slow add cleanup"
                )
            service.stop()
        restored = AutoNudgeService(base_dir=tmp_path)
        await asyncio.to_thread(restored._load)
        assert restored.get_by_slot("slot-slow-write").id == loop.id

    @pytest.mark.parametrize("under_graceful_bound", [False, True])
    @pytest.mark.asyncio
    async def test_a_wedged_write_past_the_drain_cut_still_ends_shutdown_at_the_graceful_bound(
        self, tmp_path, monkeypatch, caplog, under_graceful_bound
    ):
        """The wait after the cut is bounded by the graceful deadline, whether that
        deadline is ``_shutdown``'s own or the bound ``_shutdown_and_exit`` applies,
        and the write it leaves is named at WARNING."""
        orch, service, add_task, release, finished = await self._cut_drain_over_a_slow_write(
            tmp_path, monkeypatch, graceful_secs=0.3
        )
        caplog.set_level("WARNING", logger="kiro_crew.autonudge")
        try:
            shutdown = orch._shutdown()
            if under_graceful_bound:
                shutdown = asyncio.wait_for(shutdown, timeout=gw.GRACEFUL_SHUTDOWN_SECS)
            with contextlib.suppress(asyncio.TimeoutError):
                await _within_lost_run(shutdown, "shutdown over a wedged AutoNudge write")
            assert not finished.is_set()
            assert any(
                "at the graceful shutdown deadline" in record.getMessage()
                for record in caplog.records
            ), "shutdown ended without naming the admitted write it left pending"
        finally:
            release.set()
            await _within_lost_run(
                asyncio.gather(add_task, return_exceptions=True), "the wedged add cleanup"
            )
            service.stop()

    @pytest.mark.asyncio
    async def test_teardown_after_an_interrupted_autonudge_drain_stops_at_the_graceful_deadline(
        self, monkeypatch
    ):
        """The drain absorbs the graceful bound's one cancellation to finish its
        writes, so the teardown after it must carry that bound itself: a slow step is
        cut at the deadline instead of running unbounded past it."""
        monkeypatch.setattr(gw, "GRACEFUL_SHUTDOWN_SECS", 0.3)
        orch = _make_orchestrator()
        cron_cut = asyncio.Event()

        async def interrupted_drain():
            raise asyncio.CancelledError()

        async def slow_cron_stop():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cron_cut.set()
                raise

        orch.autonudge_svc = MagicMock()
        orch.autonudge_svc.shutdown = AsyncMock(side_effect=interrupted_drain)
        orch.cron_svc = MagicMock()
        orch.cron_svc.stop = AsyncMock(side_effect=slow_cron_stop)
        orch.sessions = _mock_sessions()

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(orch._shutdown(), timeout=_LOST_RUN_SECS)
        await _within_lost_run(cron_cut.wait(), "the graceful teardown cancellation")

        assert cron_cut.is_set()
        orch.sessions.close_all.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_teardown_bound_survives_a_step_that_swallows_its_cancellation(self, monkeypatch):
        """The deadline belongs to the waiter, not to the teardown step's await."""
        monkeypatch.setattr(gw, "GRACEFUL_SHUTDOWN_SECS", 0.3)
        orch = _make_orchestrator()
        cron_cut = asyncio.Event()
        release_swallowed_cancel = asyncio.Event()
        teardown_tasks: list[asyncio.Task] = []

        async def interrupted_drain():
            raise asyncio.CancelledError()

        async def swallowing_cron_stop():
            task = asyncio.current_task()
            assert task is not None
            teardown_tasks.append(task)
            release = asyncio.create_task(
                _within_lost_run(
                    release_swallowed_cancel.wait(),
                    "the cancellation-swallowing teardown stub release",
                )
            )
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cron_cut.set()
                await release

        orch.autonudge_svc = MagicMock()
        orch.autonudge_svc.shutdown = AsyncMock(side_effect=interrupted_drain)
        orch.cron_svc = MagicMock()
        orch.cron_svc.stop = AsyncMock(side_effect=swallowing_cron_stop)
        orch.sessions = _mock_sessions()

        try:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(orch._shutdown(), timeout=_LOST_RUN_SECS)
            await _within_lost_run(
                cron_cut.wait(), "the cancellation-swallowing teardown cancellation"
            )

            assert cron_cut.is_set()
            orch.sessions.close_all.assert_not_awaited()
        finally:
            release_swallowed_cancel.set()
            for task in teardown_tasks:
                task.cancel()
            if teardown_tasks:
                await _within_lost_run(
                    asyncio.gather(*teardown_tasks, return_exceptions=True),
                    "the cancellation-swallowing teardown stub cleanup",
                )

    @pytest.mark.asyncio
    async def test_shutdown_stops_cron(self):
        orch = _make_orchestrator()
        orch.cron_svc = MagicMock()
        orch.cron_svc.stop = AsyncMock()
        orch.heartbeat_svc = MagicMock()
        orch.heartbeat_svc.stop = MagicMock()
        orch.secretary_svc = None
        orch.subagent_mgr = MagicMock()
        orch.subagent_mgr.cancel_all = AsyncMock()
        orch.sessions = _mock_sessions()
        orch.dashboard_state = _mock_dashboard_state()
        orch._dashboard_runner = MagicMock()
        orch._dashboard_runner.cleanup = AsyncMock()
        await orch._shutdown()
        orch.cron_svc.stop.assert_awaited_once()
        orch.heartbeat_svc.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_shutdown_releases_the_subagent_task_store(self):
        """Shutdown closes the manager's durable store, and only after cancel_all.

        ``SubagentManager.__init__`` opens a SQLite connection plus a writer thread
        for the durable task queue; ``cancel_all`` stops the runs but leaves that
        store open, so a gateway that never calls ``close`` holds the descriptors
        and the executor for the life of the process. The ORDER is the other half
        of the contract: ``cancel_all`` is what stops the runs still writing to the
        store, so closing first would pull the connection out from under them.
        """
        order: list[str] = []
        orch = _make_orchestrator()
        orch.cron_svc = None
        orch.heartbeat_svc = None
        orch.secretary_svc = None
        orch.subagent_mgr = MagicMock()
        orch.subagent_mgr.cancel_all = AsyncMock(side_effect=lambda: order.append("cancel_all"))
        orch.subagent_mgr.close = MagicMock(side_effect=lambda: order.append("close"))
        orch.sessions = None
        orch.dashboard_state = None
        orch._dashboard_runner = None
        await orch._shutdown()
        orch.subagent_mgr.close.assert_called_once_with()
        assert order == ["cancel_all", "close"]

    @pytest.mark.asyncio
    async def test_shutdown_cancels_handler_tasks(self):
        orch = _make_orchestrator()
        task = asyncio.create_task(asyncio.sleep(100))
        orch._handler_tasks.add(task)
        orch.cron_svc = None
        orch.heartbeat_svc = None
        orch.secretary_svc = None
        orch.subagent_mgr = None
        orch.sessions = None
        orch.dashboard_state = None
        orch._dashboard_runner = None
        await orch._shutdown()
        assert task.cancelled()

    @staticmethod
    def _orch_with_dashboard_turn(turn: "asyncio.Task"):
        orch = _make_orchestrator()
        for name in ("cron_svc", "heartbeat_svc", "secretary_svc", "subagent_mgr"):
            setattr(orch, name, None)
        orch._dashboard_runner = None
        orch.sessions = MagicMock()
        orch.sessions.close_all = AsyncMock()
        state = MagicMock()
        state._slots = {"s1": SimpleNamespace(task=turn)}
        state.close_all_ws = AsyncMock()
        orch.dashboard_state = state
        return orch

    @pytest.mark.asyncio
    async def test_shutdown_waits_for_a_dashboard_turn_that_is_unwinding(self, monkeypatch):
        """A turn cut by close_all() saves its partial reply as it unwinds; the hard
        exit after shutdown must not beat that save."""
        monkeypatch.setattr(
            "kiro_crew.gateway_shutdown_budget.DASHBOARD_TURN_UNWIND_SECS", 5.0, raising=False
        )
        order: list[str] = []
        closed = asyncio.Event()
        collected = asyncio.Event()
        release = asyncio.Event()

        async def _turn():
            await asyncio.wait_for(closed.wait(), timeout=30)
            # The unwind and its save: parked until the test lets it finish.
            await asyncio.wait_for(release.wait(), timeout=30)
            order.append("saved")

        turn = asyncio.create_task(_turn())
        try:
            orch = self._orch_with_dashboard_turn(turn)
            orch.sessions.close_all = AsyncMock(side_effect=lambda: closed.set())
            state, slots = orch.dashboard_state, orch.dashboard_state._slots

            def _slots_read(_self):
                # Shutdown reads the slots after close_all() only to wait for
                # their turns; that read is the signal to let the turn finish.
                if closed.is_set():
                    collected.set()
                return slots

            type(state)._slots = property(_slots_read)
            shutdown = asyncio.create_task(orch._shutdown())
            shutdown.add_done_callback(lambda _t: order.append("shutdown returned"))
            collecting = asyncio.create_task(collected.wait())
            await asyncio.wait_for(
                asyncio.wait({collecting, shutdown}, return_when=asyncio.FIRST_COMPLETED), 30
            )
            collecting.cancel()
            release.set()
            await asyncio.wait_for(asyncio.gather(turn, shutdown), timeout=30)
            assert order == [
                "saved",
                "shutdown returned",
            ], f"shutdown returned before the cut turn saved: {order}"
        finally:
            turn.cancel()
            await asyncio.wait_for(asyncio.gather(turn, return_exceptions=True), timeout=30)

    @pytest.mark.asyncio
    async def test_shutdown_never_cancels_or_outwaits_a_hung_dashboard_turn(self, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.gateway_shutdown_budget.DASHBOARD_TURN_UNWIND_SECS", 0.2, raising=False
        )
        turn = asyncio.create_task(asyncio.Event().wait())
        orch = self._orch_with_dashboard_turn(turn)
        try:
            await asyncio.wait_for(orch._shutdown(), timeout=30)
            assert not turn.done(), "a hung turn must be left to the exit, not cancelled"
        finally:
            turn.cancel()
            await asyncio.wait_for(asyncio.gather(turn, return_exceptions=True), timeout=30)

    @pytest.mark.asyncio
    async def test_handler_removal_cannot_keep_shutdown_from_autonudge_drain(self, monkeypatch):
        """A cancellation-absorbing handler is awaited only after the drain."""
        monkeypatch.setattr(gw, "GRACEFUL_SHUTDOWN_SECS", 0.05)
        orch = _make_orchestrator()
        remove_entered = asyncio.Event()
        remove_cancelled = asyncio.Event()
        release_remove = asyncio.Event()
        drain_reached = asyncio.Event()

        async def absorbing_remove(_loop_id):
            remove_entered.set()
            release = asyncio.create_task(
                _within_lost_run(release_remove.wait(), "the handler-removal stub release")
            )
            while not release.done():
                try:
                    await asyncio.shield(release)
                except asyncio.CancelledError:
                    remove_cancelled.set()
            await release

        async def interrupted_drain():
            drain_reached.set()
            raise asyncio.CancelledError()

        service = MagicMock()
        service.remove = AsyncMock(side_effect=absorbing_remove)
        service.shutdown = AsyncMock(side_effect=interrupted_drain)
        orch.autonudge_svc = service
        orch.sessions = _mock_sessions()
        handler = asyncio.create_task(service.remove("loop-wedged-removal"))
        orch._handler_tasks.add(handler)
        handler.add_done_callback(orch._handler_tasks.discard)
        await asyncio.wait_for(remove_entered.wait(), timeout=_LOST_RUN_SECS)

        shutdown = asyncio.create_task(orch._shutdown())
        owned_teardown: list[asyncio.Task] = []
        try:
            with pytest.raises(asyncio.CancelledError):
                await _within_lost_run(
                    shutdown,
                    "gateway shutdown with a cancellation-absorbing handler removal",
                )
            await asyncio.wait_for(drain_reached.wait(), timeout=_LOST_RUN_SECS)
            await asyncio.wait_for(remove_cancelled.wait(), timeout=_LOST_RUN_SECS)
            service.shutdown.assert_awaited_once_with()
            orch.sessions.close_all.assert_not_awaited()
            owned_teardown = [
                task
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
                and task.get_name() == "gateway-shutdown-teardown"
            ]
        finally:
            release_remove.set()
            await _within_lost_run(handler, "the released handler removal")
            if owned_teardown:
                await _within_lost_run(
                    asyncio.gather(*owned_teardown, return_exceptions=True),
                    "the cancelled owned teardown",
                )

    @pytest.mark.asyncio
    async def test_shutdown_cancels_and_reaps_console_script_repair(self, tmp_path):
        """Shutdown owns the repair task, not only its direct cancellation path."""
        (tmp_path / ".install-method").write_text("pip")
        venv_py = gw.dep_sync.project_venv_python(tmp_path)
        venv_py.parent.mkdir(parents=True)
        started = asyncio.Event()
        never = asyncio.Event()
        proc = _fake_async_proc()

        async def _communicate():
            started.set()
            await never.wait()
            return b"", b""

        proc.communicate = AsyncMock(side_effect=_communicate)
        orch = _make_orchestrator()

        async def fake_prepare(cmd, **kwargs):
            return list(cmd), {}, None

        with (
            patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": str(tmp_path)}, clear=False),
            patch.object(gw, "sandboxed_spawn_argv_async", side_effect=fake_prepare),
            patch.object(
                gw,
                "create_subprocess_limited",
                new_callable=AsyncMock,
                return_value=proc,
            ),
            patch.object(orch, "_kill_startup_child", new_callable=AsyncMock) as kill,
            patch.object(orch, "_reap_startup_child", new_callable=AsyncMock) as reap,
        ):
            repair_task = orch._schedule_console_script_repair()
            await await_until(started)
            await orch._shutdown()
            await asyncio.sleep(0)

        assert repair_task.cancelled()
        kill.assert_awaited_once_with(proc)
        reap.assert_awaited_once_with(proc)
        assert orch._console_script_repair_task is None
        assert repair_task not in orch._background_tasks

    @pytest.mark.asyncio
    async def test_shutdown_disarms_watchdog_before_reaping(self):
        # The loop-stall watchdog's armed dump-then-exit timer MUST be cancelled
        # at the very start of shutdown — before close_all()/cancel_all() trigger
        # the child-reaping burst that can wedge the loop — or that wedge would
        # _exit(1) the process mid-shutdown. Assert stop() runs and the heartbeat
        # is cancelled, and that ordering: the watchdog is disarmed before the
        # session/subagent teardown that does the reaping.
        order: list[str] = []
        orch = _make_orchestrator()
        orch.cron_svc = None
        orch.heartbeat_svc = None
        orch.secretary_svc = None
        orch.subagent_mgr = MagicMock()
        orch.subagent_mgr.cancel_all = AsyncMock(side_effect=lambda: order.append("reap"))
        orch.sessions = _mock_sessions()
        orch.sessions.close_all = AsyncMock(side_effect=lambda: order.append("reap"))
        ds = _mock_dashboard_state()
        wd = MagicMock()
        wd.stop = MagicMock(side_effect=lambda: order.append("watchdog_stop"))
        hb = MagicMock()
        hb.cancel = MagicMock(side_effect=lambda: order.append("heartbeat_cancel"))
        ds._loop_watchdog = wd
        ds._loop_heartbeat = hb
        orch.dashboard_state = ds
        orch._dashboard_runner = MagicMock()
        orch._dashboard_runner.cleanup = AsyncMock()
        await orch._shutdown()
        wd.stop.assert_called_once()
        hb.cancel.assert_called_once()
        # Disarm happens before the first reaping step.
        assert order[0] == "watchdog_stop"
        assert "heartbeat_cancel" in order[:2]
        assert order.index("watchdog_stop") < order.index("reap")
