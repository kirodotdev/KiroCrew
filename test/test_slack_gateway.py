"""Tests for kiro_crew.slack.gateway — GatewayOrchestrator coverage."""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.autonudge import NudgeLoop
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.slack import gateway as gw
from kiro_crew.slack.gateway import (
    _CRON_MSG_LIMIT,
    _EPOCH_RE,
    _EPOCH_WINDOW_SECS,
    _FAILURE_REMINDER_SECS,
    _MAX_INJECT_ATTEMPTS,
    _SUCCESS_REMINDER_SECS,
    _VOLATILE_RE,
    GatewayOrchestrator,
    _result_hash,
)


def _make_orchestrator(
    *,
    slack_enabled: bool = False,
    owner_id: str = "U_OWNER",
    no_dashboard: bool = False,
    no_crons: bool = False,
    no_open: bool = False,
    test_mode: bool = False,
) -> GatewayOrchestrator:
    """Build a GatewayOrchestrator with mocked credentials."""
    cfg = KiroCrewConfig()
    creds: dict[str, str] = {}
    if slack_enabled:
        creds = {
            "SLACK_APP_TOKEN": "xapp-test",
            "SLACK_BOT_TOKEN": "xoxb-test",
            "KIROCREW_OWNER_ID": owner_id,
        }
    else:
        if owner_id:
            creds["KIROCREW_OWNER_ID"] = owner_id
    with patch.object(cfg, "load_credentials", return_value=creds):
        orch = GatewayOrchestrator(
            cfg,
            no_dashboard=no_dashboard,
            no_crons=no_crons,
            no_open=no_open,
            test_mode=test_mode,
        )
    return orch


# ─── Helper utilities ────────────────────────────────────────────────────


def _fake_async_proc(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""):
    """A stand-in for an ``asyncio.create_subprocess_exec`` child process."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.kill = MagicMock()
    return proc


def _mock_sessions():
    """Return a mock SessionManager with common methods."""
    s = MagicMock()
    s.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))
    s.release = MagicMock()
    s.reset = AsyncMock()
    s.cancel_current = AsyncMock()
    s.get_channel = MagicMock(return_value=None)
    s.get_thread = MagicMock(return_value=None)
    s.set_thread = AsyncMock()
    s.set_channel = AsyncMock()
    s.start_pool = AsyncMock()
    s.close_all = AsyncMock()
    s.recycle_background = AsyncMock()
    return s


def _mock_dashboard_state():
    """Return a mock DashboardState."""
    ds = MagicMock()
    ds._slots = {}
    ds._yolo = False
    ds.notify = MagicMock()
    ds.push_slots_update = MagicMock()
    ds.push_refresh = MagicMock()
    ds.broadcast_ws = MagicMock()
    ds.broadcast_ws_subagent_subscribers = MagicMock()
    ds.request_approval = AsyncMock(return_value=True)
    ds.resolve_approval = MagicMock()
    ds.resolve_slot = MagicMock(return_value=None)
    ds.get_slot = MagicMock(return_value=None)
    ds.get_or_create_slot = MagicMock()
    ds.close_all_ws = AsyncMock()
    ds.file_indexes = MagicMock()
    ds.file_indexes.stop_all = MagicMock()
    ds._background_tasks = set()
    ds.clear_update_progress = MagicMock()
    ds.push_update_progress = MagicMock()
    return ds


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

    def test_strips_iso_timestamps(self):
        a = _result_hash("deployed at 2026-01-15T10:30:00Z successfully")
        b = _result_hash("deployed at 2026-05-20T22:00:00+05:00 successfully")
        assert a == b

    def test_strips_uuids(self):
        a = _result_hash("id=550e8400-e29b-41d4-a716-446655440000 done")
        b = _result_hash("id=a1b2c3d4-e5f6-7890-abcd-ef1234567890 done")
        assert a == b

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
    async def test_raises_on_non_retryable(self):
        from slack_sdk.errors import SlackApiError

        orch = _make_orchestrator(slack_enabled=True)
        mock_slack = MagicMock()
        resp = MagicMock()
        resp.status_code = 403
        err = SlackApiError("forbidden", resp)
        mock_slack.open_dm = AsyncMock(side_effect=err)
        orch.slack = mock_slack
        with pytest.raises(SlackApiError):
            await orch._open_dm_with_retry("U1", "job", max_attempts=3)
        assert mock_slack.open_dm.call_count == 1

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

    @pytest.mark.asyncio
    async def test_raises_after_max_attempts(self):
        from slack_sdk.errors import SlackApiError

        orch = _make_orchestrator(slack_enabled=True)
        mock_slack = MagicMock()
        resp = MagicMock()
        resp.status_code = 500
        err = SlackApiError("server error", resp)
        mock_slack.open_dm = AsyncMock(side_effect=err)
        orch.slack = mock_slack
        with pytest.raises(SlackApiError):
            await orch._open_dm_with_retry("U1", "job", max_attempts=2)
        assert mock_slack.open_dm.call_count == 2


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
                                                        asyncio.run(orch._init_services())

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
                                                        asyncio.run(orch._init_services())

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

        This previously asserted auto-approval. That rule is gone: session
        trust speaks for a chat session, not for an unattended job, and with
        one trusted chat open the `all()` test was trivially satisfied.
        Asserting the return value alone would now pass vacuously, because the
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
    async def test_silent_logs_only(self):
        orch = _make_orchestrator()
        orch.slack = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        await orch._deliver_result("Title", "summary", "result", "silent")
        orch.dashboard_state.notify.assert_not_called()

    @pytest.mark.asyncio
    async def test_dashboard_new_slot(self):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        slot = MagicMock()
        slot.append = MagicMock()
        ds.get_or_create_slot = MagicMock(return_value=slot)
        orch.dashboard_state = ds
        await orch._deliver_result("Title", "task", "result", "dashboard")
        slot.append.assert_called_once()
        ds.notify.assert_called_once()

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
        slot.append.assert_called_once()

    @pytest.mark.asyncio
    async def test_dashboard_slot_not_found(self):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        ds.resolve_slot = MagicMock(return_value=None)
        orch.dashboard_state = ds
        with patch("kiro_crew.slack.gateway.sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            await orch._deliver_result("Title", "task", "result", "dashboard:gone")
        ds.notify.assert_not_called()

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
    async def test_slack_thread_delivery(self):
        from conftest import MockSlackClient

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

    @pytest.mark.asyncio
    async def test_prompt_dashboard_slot_not_found(self):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        ds.resolve_slot = MagicMock(return_value=None)
        orch.dashboard_state = ds
        with patch("kiro_crew.slack.gateway.sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            await orch._deliver_result("Title", "task", "result", "prompt:dashboard:gone")
        ds.notify.assert_not_called()

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
        ds.notify.assert_not_called()


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
        await orch._shutdown()  # should not raise

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


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _check_for_updates
# ═══════════════════════════════════════════════════════════════════════════


class TestCheckForUpdates:
    """Update check logic."""

    @pytest.mark.asyncio
    async def test_no_update_available(self):
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        with patch("kiro_crew.dashboard.handlers._do_update_check", new_callable=AsyncMock):
            with patch("kiro_crew.dashboard.handlers._update_info", {"update_available": False}):
                await orch._check_for_updates()

    @pytest.mark.asyncio
    async def test_update_available_no_auto(self):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch._auto_apply_update = AsyncMock()
        import kiro_crew.dashboard.handlers as _h
        from kiro_crew.platform.governance import UpdatePins

        orig = _h._update_info.copy()
        # Create a config with auto_update=False
        fake_cfg = MagicMock()
        fake_cfg.auto_update = False
        try:
            _h._update_info.update({"update_available": True, "version": "9.9.9"})
            with patch.object(_h, "_do_update_check", new_callable=AsyncMock):
                with patch("kiro_crew.config.KiroCrewConfig.load", return_value=fake_cfg):
                    with patch(
                        "kiro_crew.platform.governance.active_update_pins",
                        return_value=UpdatePins(),
                    ):
                        await orch._check_for_updates()
        finally:
            _h._update_info.clear()
            _h._update_info.update(orig)
        orch._auto_apply_update.assert_not_awaited()
        ds.push_refresh.assert_called_with("update_available")

    @pytest.mark.asyncio
    async def test_commit_distance_alone_lights_the_badge_but_does_not_auto_apply(self):
        """A git checkout behind upstream with an UNCHANGED version is not reset.

        `available` is true on commit distance alone so the dashboard stops
        claiming "you're on the latest version". This path is not the dashboard:
        it applies `git reset --hard`, so acting on commit distance would reset a
        developer's checkout within 12 hours of any upstream commit, where before
        it only did so at a release. The badge is lit instead; the dashboard's own
        apply (`git pull`, dirty tree refused) is the non-destructive way in.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch._auto_apply_update = AsyncMock()
        import kiro_crew.dashboard.handlers as _h

        fake_cfg = MagicMock()
        fake_cfg.auto_update = True
        orig = _h._update_info.copy()
        try:
            _h._update_info.update(
                {"update_available": True, "can_apply": True, "version_newer": False}
            )
            with patch.object(_h, "_do_update_check", new_callable=AsyncMock):
                with patch("kiro_crew.config.KiroCrewConfig.load", return_value=fake_cfg):
                    with patch(
                        "kiro_crew.platform.update_governance.update_required",
                        return_value=False,
                    ):
                        await orch._check_for_updates()
        finally:
            _h._update_info.clear()
            _h._update_info.update(orig)
        orch._auto_apply_update.assert_not_awaited()
        ds.push_refresh.assert_called_with("update_available")

    @pytest.mark.asyncio
    async def test_a_version_bump_still_auto_applies(self):
        """The pre-existing trigger is unchanged: a release moved, so apply."""
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        orch._auto_apply_update = AsyncMock()
        import kiro_crew.dashboard.handlers as _h

        fake_cfg = MagicMock()
        fake_cfg.auto_update = True
        orig = _h._update_info.copy()
        try:
            _h._update_info.update(
                {"update_available": True, "can_apply": True, "version_newer": True}
            )
            with patch.object(_h, "_do_update_check", new_callable=AsyncMock):
                with patch("kiro_crew.config.KiroCrewConfig.load", return_value=fake_cfg):
                    with patch(
                        "kiro_crew.platform.update_governance.update_required",
                        return_value=False,
                    ):
                        await orch._check_for_updates()
        finally:
            _h._update_info.clear()
            _h._update_info.update(orig)
        orch._auto_apply_update.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_min_version_mandate_fires_even_when_not_available(self):
        """The mandate is about THIS host, not the availability heuristic.

        `_do_update_check`'s `_version_tuple` returns (0,) for any pre-release, so
        a `1.4.0-nightly.<stamp>` remote reads as `available=False`. Nested inside
        that branch, a host below a pinned 1.4.0 floor would never update.
        """
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        orch._auto_apply_update = AsyncMock()
        import kiro_crew.dashboard.handlers as _h

        orig = _h._update_info.copy()
        try:
            # A git checkout (`can_apply`) below the floor: the git auto-apply
            # is the correct mandatory action. `_do_update_check` sets this key
            # per layout in the real flow; it is mocked here, so the fixture
            # states the layout explicitly. The wheel layout (no `can_apply`
            # False) takes the notify path instead — see
            # TestMandatoryUpdateOnWheelInstall.
            _h._update_info.update({"update_available": False, "can_apply": True})
            with patch.object(_h, "_do_update_check", new_callable=AsyncMock):
                with patch(
                    "kiro_crew.platform.update_governance.update_required", return_value=True
                ):
                    await orch._check_for_updates()
        finally:
            _h._update_info.clear()
            _h._update_info.update(orig)
        orch._auto_apply_update.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_update_check_exception_handled(self):
        orch = _make_orchestrator()
        with patch(
            "kiro_crew.dashboard.handlers._do_update_check",
            new_callable=AsyncMock,
            side_effect=RuntimeError("network"),
        ):
            await orch._check_for_updates()  # should not raise


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _auto_apply_update
# ═══════════════════════════════════════════════════════════════════════════


class TestAutoApplyUpdate:
    """Auto-update logic (public OSS flow: git reset → frontend → pip → restart)."""

    @pytest.mark.asyncio
    async def test_no_project_dir_returns_early(self):
        orch = _make_orchestrator()
        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": ""}, clear=False):
            await orch._auto_apply_update()  # should not raise

    @pytest.mark.asyncio
    async def test_non_mainline_branch_skips(self):
        orch = _make_orchestrator()
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"feat/test\n", b""))
        proc.returncode = 0
        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}, clear=False):
            with patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=proc,
            ):
                await orch._auto_apply_update()


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _is_brazil_install and _check_missing_deps
# ═══════════════════════════════════════════════════════════════════════════


class TestBrazilInstallAndDeps:
    """Static helper and dep repair."""

    def test_is_brazil_install_with_method_file(self, tmp_path):
        method = tmp_path / ".install-method"
        method.write_text("brazil")
        assert GatewayOrchestrator._is_brazil_install(str(tmp_path)) is True

    def test_is_brazil_install_pip(self, tmp_path):
        method = tmp_path / ".install-method"
        method.write_text("pip")
        assert GatewayOrchestrator._is_brazil_install(str(tmp_path)) is False

    def test_is_brazil_install_no_file_no_brazil(self, tmp_path):
        with patch("shutil.which", return_value=None):
            assert GatewayOrchestrator._is_brazil_install(str(tmp_path)) is False

    def test_check_missing_deps_no_missing(self):
        orch = _make_orchestrator()
        with patch("importlib.util.find_spec", return_value=MagicMock()):
            asyncio.run(orch._check_missing_deps())  # should not raise

    def test_check_missing_deps_brazil_skips(self):
        orch = _make_orchestrator()
        with patch("importlib.util.find_spec", return_value=None):
            with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/proj"}):
                with patch.object(GatewayOrchestrator, "_is_brazil_install", return_value=True):
                    asyncio.run(orch._check_missing_deps())  # should not raise, skips pip


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _init_cron
# ═══════════════════════════════════════════════════════════════════════════


class TestInitCron:
    """Cron service initialization and callback."""

    @pytest.mark.asyncio
    async def test_init_cron_no_crons_flag(self):
        orch = _make_orchestrator(no_crons=True)
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.subagent_mgr = MagicMock()
        orch.subagent_mgr.running = []
        with patch("kiro_crew.slack.gateway.CronService") as mock_cs:
            mock_cs_inst = MagicMock()
            mock_cs_inst.start = AsyncMock()
            mock_cs_inst.start_reaper = MagicMock()
            mock_cs.return_value = mock_cs_inst
            mock_cs.create = AsyncMock(return_value=mock_cs_inst)
            await orch._init_cron()
        assert orch.cron_svc is not None
        mock_cs_inst.start.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_init_cron_starts_when_enabled(self):
        orch = _make_orchestrator(no_crons=False)
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.subagent_mgr = MagicMock()
        orch.subagent_mgr.running = []
        with patch("kiro_crew.slack.gateway.CronService") as mock_cs:
            mock_cs_inst = MagicMock()
            mock_cs_inst.start = AsyncMock()
            mock_cs_inst.start_reaper = MagicMock()
            mock_cs.return_value = mock_cs_inst
            mock_cs.create = AsyncMock(return_value=mock_cs_inst)
            await orch._init_cron()
        mock_cs_inst.start.assert_awaited_once()
        mock_cs_inst.start_reaper.assert_called_once()

    @pytest.mark.asyncio
    async def test_cron_callback_single_agent(self):
        """Cron callback runs single-agent path."""
        orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.build_message = MagicMock(return_value=("full msg", None))
        orch.ctx_builder.hooks = MagicMock()
        orch.subagent_mgr = MagicMock()
        orch.subagent_mgr.running = []
        orch.dashboard_state = _mock_dashboard_state()
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value="D_U1")
        orch.slack.post_blocks = AsyncMock(return_value="ts1")
        orch.slack.post_message = AsyncMock()

        with patch("kiro_crew.slack.gateway.CronService") as mock_cs:
            mock_cs_inst = MagicMock()
            mock_cs_inst.start = AsyncMock()
            mock_cs_inst.start_reaper = MagicMock()
            mock_cs_inst.register_active_session_key = MagicMock()
            mock_cs_inst.clear_active_session_key = MagicMock()
            mock_cs.return_value = mock_cs_inst
            mock_cs.create = AsyncMock(return_value=mock_cs_inst)
            await orch._init_cron()

        # Extract the callback
        callback = mock_cs.create.call_args[1]["on_job"]

        job = MagicMock()
        job.script = ""
        job.command = ""
        job.id = "j1"
        job.name = "test-job"
        job.persistent_session = True
        job.agent_sequence = []
        job.agent_id = None
        job.channel = ""
        job.created_by = "U1"
        job.approval_mode = "auto"
        job.env = None
        job.acked_items = []
        job.silent = False
        job.thread_ts = None
        job.last_posted_hash = ""
        job.consecutive_dupes = 0
        job.last_posted_at = 0.0
        job.last_failure_hash = ""
        job.last_failure_at = 0.0
        job.consecutive_failures = 0

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            return_value="cron result",
        ):
            with patch(
                "kiro_crew.slack.gateway.build_cron_session_context",
                return_value=("cron:j1", "run task"),
            ):
                result = await callback(job)

        assert result == "cron result"
        job.set_run_result.assert_called_once_with("cron result")

    @pytest.mark.asyncio
    async def test_cron_callback_publishes_turn_identity(self):
        """Regression: the cron turn must publish session_pid_<pid>.txt.

        The cron path was the one turn-running surface that never called
        publish_turn_identity. Under session sharing the runtime env carries
        no KIROCREW_SESSION_KEY and macOS sets no KIROCREW_HOST_PID, so the
        ancestor PID-walk over session_pid files is the ONLY parent-identity
        source for spawn_run — without the publish, sub-agents spawned from a
        cron turn resolved an empty parent ('notification only (parent=)')
        unless an unrelated surface happened to be mid-turn."""
        orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.build_message = MagicMock(return_value=("full msg", None))
        orch.ctx_builder.hooks = MagicMock()
        orch.subagent_mgr = MagicMock()
        orch.subagent_mgr.running = []
        orch.dashboard_state = _mock_dashboard_state()
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value="D_U1")
        orch.slack.post_blocks = AsyncMock(return_value="ts1")
        orch.slack.post_message = AsyncMock()

        with patch("kiro_crew.slack.gateway.CronService") as mock_cs:
            mock_cs_inst = MagicMock()
            mock_cs_inst.start = AsyncMock()
            mock_cs_inst.start_reaper = MagicMock()
            mock_cs_inst.register_active_session_key = MagicMock()
            mock_cs_inst.clear_active_session_key = MagicMock()
            mock_cs.return_value = mock_cs_inst
            mock_cs.create = AsyncMock(return_value=mock_cs_inst)
            await orch._init_cron()

        callback = mock_cs.create.call_args[1]["on_job"]

        job = MagicMock()
        job.script = ""
        job.command = ""
        job.id = "j1"
        job.name = "test-job"
        job.persistent_session = True
        job.agent_sequence = []
        job.agent_id = None
        job.channel = ""
        job.created_by = "U1"
        job.approval_mode = "auto"
        job.env = None
        job.acked_items = []
        job.silent = False
        job.thread_ts = None
        job.last_posted_hash = ""
        job.consecutive_dupes = 0
        job.last_posted_at = 0.0
        job.last_failure_hash = ""
        job.last_failure_at = 0.0
        job.consecutive_failures = 0

        publish_events: list[str] = []

        async def _publish(sessions, key):
            publish_events.append(f"publish:{key}")

        async def _stream(*a, **k):
            # Identity must already be published when the model turn starts —
            # spawn_run can fire at any point inside it.
            publish_events.append("stream")
            return "cron result"

        with patch("kiro_crew.slack.gateway.publish_turn_identity", side_effect=_publish):
            with patch("kiro_crew.slack.gateway.stream_and_collect", side_effect=_stream):
                with patch(
                    "kiro_crew.slack.gateway.build_cron_session_context",
                    return_value=("cron:j1", "run task"),
                ):
                    await callback(job)

        # Ordering is the contract: the publish must precede the model turn.
        assert publish_events == ["publish:cron:j1", "stream"]

    @pytest.mark.asyncio
    async def test_cron_callback_publishes_identity_per_sequence_agent(self):
        """Each agent in an agent_sequence runs on its own per-agent session
        key (cron:<job>:<agent>) — identity must be re-published for each."""
        orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.build_message = MagicMock(return_value=("full msg", None))
        orch.ctx_builder.hooks = MagicMock()
        orch.subagent_mgr = MagicMock()
        orch.subagent_mgr.running = []
        orch.dashboard_state = _mock_dashboard_state()
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value="D_U1")
        orch.slack.post_blocks = AsyncMock(return_value="ts1")
        orch.slack.post_message = AsyncMock()

        with patch("kiro_crew.slack.gateway.CronService") as mock_cs:
            mock_cs_inst = MagicMock()
            mock_cs_inst.start = AsyncMock()
            mock_cs_inst.start_reaper = MagicMock()
            mock_cs_inst.register_active_session_key = MagicMock()
            mock_cs_inst.clear_active_session_key = MagicMock()
            mock_cs.return_value = mock_cs_inst
            mock_cs.create = AsyncMock(return_value=mock_cs_inst)
            await orch._init_cron()

        callback = mock_cs.create.call_args[1]["on_job"]

        job = MagicMock()
        job.script = ""
        job.command = ""
        job.id = "j1"
        job.name = "test-job"
        job.persistent_session = True
        job.agent_sequence = ["planner", "worker"]
        job.agent_id = None
        job.channel = ""
        job.created_by = "U1"
        job.approval_mode = "auto"
        job.env = None
        job.acked_items = []
        job.silent = False
        job.thread_ts = None
        job.last_posted_hash = ""
        job.consecutive_dupes = 0
        job.last_posted_at = 0.0
        job.last_failure_hash = ""
        job.last_failure_at = 0.0
        job.consecutive_failures = 0

        publish = AsyncMock()
        with patch("kiro_crew.slack.gateway.publish_turn_identity", publish):
            with patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value="cron result",
            ):
                with patch(
                    "kiro_crew.slack.gateway.build_cron_session_context",
                    return_value=("cron:j1", "run task"),
                ):
                    await callback(job)

        assert publish.await_count == 2
        published_keys = [c.args[1] for c in publish.await_args_list]
        assert published_keys == ["cron:j1:planner", "cron:j1:worker"]

    @pytest.mark.asyncio
    async def test_sequence_agent_reset_deferred_while_subagents_pending(self):
        """The sequential finally must mirror the single-agent deferral.

        Now that the sequence path publishes turn identity, a non-final
        agent's spawn_run resolves a REAL parent key. An unconditional reset
        at the end of that agent's turn would tear down the session a pending
        sub-agent completion is about to inject into (cold-starting a
        context-free replacement) and strip the reaper registration for the
        NEXT agent's still-in-flight turn."""
        orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
        orch.sessions = _mock_sessions()
        orch.sessions.reset = AsyncMock()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.build_message = MagicMock(return_value=("full msg", None))
        orch.ctx_builder.hooks = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value="D_U1")
        orch.slack.post_blocks = AsyncMock(return_value="ts1")
        orch.slack.post_message = AsyncMock()

        # A sub-agent of the FIRST sequence agent is still running when its
        # turn ends; the second agent has no pending sub-agents.
        pending = MagicMock()
        pending.parent_session_key = "cron:j1:planner"
        orch.subagent_mgr = MagicMock()
        orch.subagent_mgr.running = [pending]
        # Real predicate semantics: pending work == a running agent with this
        # parent (no queued spawns in this scenario).
        orch.subagent_mgr.queued_count_for = MagicMock(return_value=0)
        orch.subagent_mgr.has_pending_work_for = MagicMock(
            side_effect=lambda key: any(
                a.parent_session_key == key for a in orch.subagent_mgr.running
            )
        )

        with patch("kiro_crew.slack.gateway.CronService") as mock_cs:
            mock_cs_inst = MagicMock()
            mock_cs_inst.start = AsyncMock()
            mock_cs_inst.start_reaper = MagicMock()
            mock_cs_inst.register_active_session_key = MagicMock()
            mock_cs_inst.clear_active_session_key = MagicMock()
            mock_cs.return_value = mock_cs_inst
            mock_cs.create = AsyncMock(return_value=mock_cs_inst)
            await orch._init_cron()

        callback = mock_cs.create.call_args[1]["on_job"]

        job = MagicMock()
        job.script = ""
        job.command = ""
        job.id = "j1"
        job.name = "test-job"
        job.persistent_session = True
        job.agent_sequence = ["planner", "worker"]
        job.agent_id = None
        job.channel = ""
        job.created_by = "U1"
        job.approval_mode = "auto"
        job.env = None
        job.acked_items = []
        job.silent = False
        job.thread_ts = None
        job.last_posted_hash = ""
        job.consecutive_dupes = 0
        job.last_posted_at = 0.0
        job.last_failure_hash = ""
        job.last_failure_at = 0.0
        job.consecutive_failures = 0

        with patch("kiro_crew.slack.gateway.publish_turn_identity", new_callable=AsyncMock):
            with patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value="cron result",
            ):
                with patch(
                    "kiro_crew.slack.gateway.build_cron_session_context",
                    return_value=("cron:j1", "run task"),
                ):
                    await callback(job)

        # planner's reset deferred (sub-agent pending); worker's reset ran.
        reset_keys = [c.args[0] for c in orch.sessions.reset.await_args_list]
        assert "cron:j1:planner" not in reset_keys
        assert "cron:j1:worker" in reset_keys
        # The reaper registration is cleared only by the agent that actually
        # reset — one clear, not two.
        assert mock_cs_inst.clear_active_session_key.call_count == 1

    @pytest.mark.asyncio
    async def test_sequence_agent_reset_deferred_while_subagents_queued(self):
        """A spawn accepted behind the concurrency/stagger gate is in the
        manager's QUEUE, not `running` — the deferral must see it anyway.
        A `running`-only guard reads "no pending work" during exactly the
        window a wave is ramping, and the reset strands the queued agent's
        completion on a cold-started, context-free replacement session."""
        orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
        orch.sessions = _mock_sessions()
        orch.sessions.reset = AsyncMock()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.build_message = MagicMock(return_value=("full msg", None))
        orch.ctx_builder.hooks = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value="D_U1")
        orch.slack.post_blocks = AsyncMock(return_value="ts1")
        orch.slack.post_message = AsyncMock()

        # Nothing RUNNING for planner — but one spawn is QUEUED for it.
        orch.subagent_mgr = MagicMock()
        orch.subagent_mgr.running = []
        orch.subagent_mgr.queued_count_for = MagicMock(
            side_effect=lambda key: 1 if key == "cron:j1:planner" else 0
        )
        orch.subagent_mgr.has_pending_work_for = MagicMock(
            side_effect=lambda key: key == "cron:j1:planner"
        )

        with patch("kiro_crew.slack.gateway.CronService") as mock_cs:
            mock_cs_inst = MagicMock()
            mock_cs_inst.start = AsyncMock()
            mock_cs_inst.start_reaper = MagicMock()
            mock_cs_inst.register_active_session_key = MagicMock()
            mock_cs_inst.clear_active_session_key = MagicMock()
            mock_cs.return_value = mock_cs_inst
            mock_cs.create = AsyncMock(return_value=mock_cs_inst)
            await orch._init_cron()

        callback = mock_cs.create.call_args[1]["on_job"]

        job = MagicMock()
        job.script = ""
        job.command = ""
        job.id = "j1"
        job.name = "test-job"
        job.persistent_session = True
        job.agent_sequence = ["planner", "worker"]
        job.agent_id = None
        job.channel = ""
        job.created_by = "U1"
        job.approval_mode = "auto"
        job.env = None
        job.acked_items = []
        job.silent = False
        job.thread_ts = None
        job.last_posted_hash = ""
        job.consecutive_dupes = 0
        job.last_posted_at = 0.0
        job.last_failure_hash = ""
        job.last_failure_at = 0.0
        job.consecutive_failures = 0

        with patch("kiro_crew.slack.gateway.publish_turn_identity", new_callable=AsyncMock):
            with patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value="cron result",
            ):
                with patch(
                    "kiro_crew.slack.gateway.build_cron_session_context",
                    return_value=("cron:j1", "run task"),
                ):
                    await callback(job)

        reset_keys = [c.args[0] for c in orch.sessions.reset.await_args_list]
        assert "cron:j1:planner" not in reset_keys
        assert "cron:j1:worker" in reset_keys

    @pytest.mark.asyncio
    async def test_cron_name_is_redacted_before_delivery(self):
        """A cron NAME is LLM-authored text on its way to Slack.

        The name is interpolated into the ``⏰ *Cron: …*`` header, which stays
        outside the render pipeline on purpose (it is already Slack mrkdwn, so
        converting it would re-interpret its ``*bold*``). Skipping conversion also
        means skipping the pipeline's redaction, so the name needs its own pass --
        an agent can create a cron via the ``cron_add`` tool, so a credential can
        land in that field and ride into the channel header.
        """
        secret = "AKIAIOSFODNN7EXAMPLE"
        orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.build_message = MagicMock(return_value=("full msg", None))
        orch.ctx_builder.hooks = MagicMock()
        orch.subagent_mgr = MagicMock()
        orch.subagent_mgr.running = []
        orch.dashboard_state = _mock_dashboard_state()
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value="D_U1")
        orch.slack.post_blocks = AsyncMock(return_value="ts1")
        orch.slack.post_message = AsyncMock()

        with patch("kiro_crew.slack.gateway.CronService") as mock_cs:
            mock_cs_inst = MagicMock()
            mock_cs_inst.start = AsyncMock()
            mock_cs_inst.start_reaper = MagicMock()
            mock_cs_inst.register_active_session_key = MagicMock()
            mock_cs_inst.clear_active_session_key = MagicMock()
            mock_cs.return_value = mock_cs_inst
            mock_cs.create = AsyncMock(return_value=mock_cs_inst)
            await orch._init_cron()

        callback = mock_cs.create.call_args[1]["on_job"]

        job = MagicMock()
        job.script = ""
        job.command = ""
        job.id = "j1"
        job.name = f"nightly {secret} sweep"
        job.persistent_session = True
        job.agent_sequence = []
        job.agent_id = None
        job.channel = ""
        job.created_by = "U1"
        job.approval_mode = "auto"
        job.env = None
        job.acked_items = []
        job.silent = False
        job.thread_ts = None
        job.last_posted_hash = ""
        job.consecutive_dupes = 0
        job.last_posted_at = 0.0
        job.last_failure_hash = ""
        job.last_failure_at = 0.0
        job.consecutive_failures = 0

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            return_value="cron result",
        ):
            with patch(
                "kiro_crew.slack.gateway.build_cron_session_context",
                return_value=("cron:j1", "run task"),
            ):
                await callback(job)

        orch.slack.post_blocks.assert_awaited()
        delivered = json.dumps(orch.slack.post_blocks.call_args.args)
        assert secret not in delivered, "a credential in the cron name reached Slack"
        assert secret[:8] not in delivered, "a credential fragment reached Slack"

    @pytest.mark.asyncio
    async def test_cron_callback_dedup_suppresses(self):
        """Duplicate result suppresses Slack delivery."""
        orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.build_message = MagicMock(return_value=("msg", None))
        orch.ctx_builder.hooks = MagicMock()
        orch.subagent_mgr = MagicMock()
        orch.subagent_mgr.running = []
        orch.dashboard_state = _mock_dashboard_state()
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value="D_U1")
        orch.slack.post_blocks = AsyncMock(return_value="ts1")
        orch.slack.post_message = AsyncMock()

        with patch("kiro_crew.slack.gateway.CronService") as mock_cs:
            mock_cs_inst = MagicMock()
            mock_cs_inst.start = AsyncMock()
            mock_cs_inst.start_reaper = MagicMock()
            mock_cs_inst.register_active_session_key = MagicMock()
            mock_cs_inst.clear_active_session_key = MagicMock()
            mock_cs.return_value = mock_cs_inst
            mock_cs.create = AsyncMock(return_value=mock_cs_inst)
            await orch._init_cron()

        callback = mock_cs.create.call_args[1]["on_job"]

        job = MagicMock()
        job.script = ""
        job.command = ""
        job.id = "j2"
        job.name = "dedup-job"
        job.persistent_session = True
        job.agent_sequence = []
        job.agent_id = None
        job.channel = ""
        job.created_by = "U1"
        job.approval_mode = "auto"
        job.env = None
        job.acked_items = []
        job.silent = False
        job.thread_ts = None
        job.last_posted_hash = _result_hash("stable output")
        job.consecutive_dupes = 1
        job.last_posted_at = time.time()
        job.last_failure_hash = ""
        job.last_failure_at = 0.0
        job.consecutive_failures = 0

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            return_value="stable output",
        ):
            with patch(
                "kiro_crew.slack.gateway.build_cron_session_context",
                return_value=("cron:j2", "run"),
            ):
                with patch("kiro_crew.sel.sel") as mock_sel:
                    mock_sel.return_value.log_tool_invocation = MagicMock()
                    result = await callback(job)

        assert result == "stable output"
        assert job.consecutive_dupes == 2
        # Slack post_blocks should NOT have been called (suppressed)
        orch.slack.post_blocks.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cron_callback_silent_suppresses(self):
        """Silent job suppresses delivery."""
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.build_message = MagicMock(return_value=("msg", None))
        orch.ctx_builder.hooks = MagicMock()
        orch.subagent_mgr = MagicMock()
        orch.subagent_mgr.running = []
        orch.dashboard_state = None
        orch.slack = None

        with patch("kiro_crew.slack.gateway.CronService") as mock_cs:
            mock_cs_inst = MagicMock()
            mock_cs_inst.start = AsyncMock()
            mock_cs_inst.start_reaper = MagicMock()
            mock_cs_inst.register_active_session_key = MagicMock()
            mock_cs_inst.clear_active_session_key = MagicMock()
            mock_cs.return_value = mock_cs_inst
            mock_cs.create = AsyncMock(return_value=mock_cs_inst)
            await orch._init_cron()

        callback = mock_cs.create.call_args[1]["on_job"]

        job = MagicMock()
        job.script = ""
        job.command = ""
        job.id = "j3"
        job.name = "silent-job"
        job.persistent_session = True
        job.agent_sequence = []
        job.agent_id = None
        job.channel = ""
        job.created_by = ""
        job.approval_mode = "auto"
        job.env = None
        job.acked_items = []
        job.silent = True
        job.thread_ts = None
        job.last_posted_hash = ""
        job.consecutive_dupes = 0
        job.last_posted_at = 0.0
        job.last_failure_hash = ""
        job.last_failure_at = 0.0
        job.consecutive_failures = 0

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            return_value="silent result",
        ):
            with patch(
                "kiro_crew.slack.gateway.build_cron_session_context",
                return_value=("cron:j3", "run"),
            ):
                with patch("kiro_crew.sel.sel") as mock_sel:
                    mock_sel.return_value.log_tool_invocation = MagicMock()
                    result = await callback(job)

        assert result == "silent result"


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _init_subagents
# ═══════════════════════════════════════════════════════════════════════════


class TestInitSubagents:
    """Subagent manager initialization."""

    @pytest.mark.asyncio
    async def test_init_subagents_creates_manager(self):
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.hooks = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False):
            with patch("kiro_crew.slack.gateway.SubagentManager") as mock_sm:
                mock_sm_inst = MagicMock()
                mock_sm_inst.start_reaper = MagicMock()
                mock_sm_inst._max_concurrent = 10
                mock_sm.return_value = mock_sm_inst
                orch._init_subagents()
        assert orch.subagent_mgr is not None
        mock_sm_inst.start_reaper.assert_not_called()

    @pytest.mark.asyncio
    async def test_init_subagents_respects_max_concurrent(self):
        orch = _make_orchestrator()
        orch._cfg.agent.max_subagents = 5
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.hooks = MagicMock()
        orch.dashboard_state = None
        with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False):
            with patch("kiro_crew.slack.gateway.SubagentManager") as mock_sm:
                mock_sm_inst = MagicMock()
                mock_sm_inst.start_reaper = MagicMock()
                mock_sm_inst._max_concurrent = 5
                mock_sm.return_value = mock_sm_inst
                orch._init_subagents()
        assert orch.subagent_mgr._max_concurrent == 5

    def _capture_on_event(self, orch):
        """Run _init_subagents with SubagentManager patched; return the on_event callback."""
        with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False):
            with patch("kiro_crew.slack.gateway.SubagentManager") as mock_sm:
                mock_sm_inst = MagicMock()
                mock_sm_inst.start_reaper = MagicMock()
                mock_sm.return_value = mock_sm_inst
                orch._init_subagents()
                return mock_sm.call_args.kwargs["on_event"]

    @pytest.mark.asyncio
    async def test_subagent_spawn_and_done_push_slots_update_debounced(self):
        """subagents_running flips at spawn/done — the on_event handler schedules a
        debounced push_slots_update so slots-stream consumers (composer busy
        affordance, Board working lane) stay live. Multiple events inside the
        debounce window coalesce into one push. Covers the reaper too, since
        _force_reap fires subagent_done through the same on_event path."""
        from kiro_crew.subagent import SubagentInfo

        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.hooks = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        on_event = self._capture_on_event(orch)

        info = SubagentInfo(id="a1", task="t", parent_session_key="dashboard:s1")
        # Batch: two spawns + one done inside the 0.2s window -> one push.
        await on_event("subagent_spawn", info, {})
        await on_event(
            "subagent_spawn", SubagentInfo(id="a2", task="t", parent_session_key="dashboard:s1"), {}
        )
        await on_event("subagent_done", info, {"elapsed": 1.0})
        assert orch.dashboard_state.push_slots_update.call_count == 0  # debounced, not yet flushed
        await asyncio.sleep(0.3)
        assert orch.dashboard_state.push_slots_update.call_count == 1

        # A later lifecycle event schedules a fresh push.
        await on_event(
            "subagent_done",
            SubagentInfo(id="a2", task="t", parent_session_key="dashboard:s1"),
            {"elapsed": 1.0},
        )
        await asyncio.sleep(0.3)
        assert orch.dashboard_state.push_slots_update.call_count == 2

    @pytest.mark.asyncio
    async def test_subagent_tool_event_does_not_push_slots(self):
        """High-frequency subagent_tool events must NOT trigger slots pushes —
        only spawn/done flip the subagents_running truth value."""
        from kiro_crew.subagent import SubagentInfo

        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.hooks = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        on_event = self._capture_on_event(orch)

        info = SubagentInfo(id="a1", task="t", parent_session_key="dashboard:s1")
        await on_event("subagent_tool", info, {"tool": "grep"})
        await asyncio.sleep(0.3)
        assert orch.dashboard_state.push_slots_update.call_count == 0


class TestSubagentDoneStoppedClassification:
    """A user-stopped subagent (error-free record) must never be classified as
    a successful completion by _subagent_done — not in the announce text and
    not in the orchestration tracker."""

    def _capture_on_done(self, orch):
        with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False):
            with patch("kiro_crew.slack.gateway.SubagentManager") as mock_sm:
                mock_sm_inst = MagicMock()
                mock_sm_inst.start_reaper = MagicMock()
                mock_sm.return_value = mock_sm_inst
                orch._init_subagents()
                return mock_sm.call_args.kwargs["on_done"]

    def _stopped_info(self):
        from kiro_crew.subagent import SubagentInfo

        info = SubagentInfo(
            id="stop1",
            task="long research task",
            parent_session_key="dashboard:gone",
        )
        info.done = True
        info.user_stopped = True
        info.error = None
        info.result = "partial notes so far"
        return info

    @pytest.mark.asyncio
    async def test_stopped_agent_notification_says_stopped_not_completed(self):
        """Slot-gone path: title carries ⏹ and body frames a stop with partial
        output — never 'completed ✅'."""
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.hooks = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        orch.dashboard_state.get_slot = MagicMock(return_value=None)  # slot gone
        on_done = self._capture_on_done(orch)

        await on_done(self._stopped_info())

        orch.dashboard_state.notify.assert_called_once()
        args = orch.dashboard_state.notify.call_args
        title, body = args.args[1], args.args[2]
        assert "⏹" in title
        assert "✅" not in title
        assert "Stopped by the user" in body
        assert "partial notes so far" in body

    @pytest.mark.asyncio
    async def test_stopped_agent_records_neither_success_nor_failure(self):
        """Orchestrator mode: a user stop must not advance orchestration —
        no record_success (killed work is not done work) and no
        record_failure (a deliberate stop is not a retryable failure)."""
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.hooks = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()

        tracker = MagicMock()
        tracker.stopped = False
        slot = MagicMock()
        slot.mode = "orchestrator"
        slot._orch_tracker = tracker
        slot.running = False
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        on_done = self._capture_on_done(orch)
        # Injection path launches _run_chat on the idle slot — stub it out.
        with patch("kiro_crew.slack.gateway._run_chat", new_callable=AsyncMock):
            await on_done(self._stopped_info())
            await asyncio.sleep(0)

        tracker.record_success.assert_not_called()
        tracker.record_failure.assert_not_called()


class TestSubagentFinalSummaryDirective:
    """Fix 2 (B1): the LAST sub-agent completion ARMS a one-shot synthesis turn
    (slot._pending_synthesis) in chat mode; earlier completions do not."""

    def _capture_on_done(self, orch):
        with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False):
            with patch("kiro_crew.slack.gateway.SubagentManager") as mock_sm:
                mock_sm_inst = MagicMock()
                mock_sm_inst.start_reaper = MagicMock()
                mock_sm.return_value = mock_sm_inst
                orch._init_subagents()
                return mock_sm.call_args.kwargs["on_done"]

    async def _done_slot(self, running_agents_for_return):
        """Fire the on_done callback through a chat-mode dashboard slot and return
        the slot so the caller can inspect _pending_synthesis."""
        from kiro_crew.subagent import SubagentInfo

        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.hooks = MagicMock()
        ds = _mock_dashboard_state()
        slot = MagicMock()
        slot.running = False
        slot.key = "s1"
        slot.mode = "chat"  # non-orchestrator → _is_orchestrator is False
        slot.task = None
        slot._pending_synthesis = False  # explicit start (not a MagicMock auto-attr)
        slot._subagent_deliveries_inflight = 0  # real int so the gateway counter works
        ds.get_slot = MagicMock(return_value=slot)
        orch.dashboard_state = ds
        on_done = self._capture_on_done(orch)
        orch.subagent_mgr.running_agents_for = MagicMock(return_value=running_agents_for_return)

        info = SubagentInfo(id="a1", task="do X", parent_session_key="dashboard:s1")
        with patch("kiro_crew.slack.gateway._run_chat", new=AsyncMock()):
            await on_done(info)
            await asyncio.sleep(0.05)  # let the injection task settle
        return slot

    @pytest.mark.asyncio
    async def test_last_completion_arms_synthesis(self):
        """No sub-agents left running → the slot is armed for a synthesis turn."""
        slot = await self._done_slot([])
        assert slot._pending_synthesis is True
        # Delivery counter must be balanced back to 0 (no leak → gate not stuck).
        assert slot._subagent_deliveries_inflight == 0

    @pytest.mark.asyncio
    async def test_pending_completion_does_not_arm(self):
        """Another sub-agent still running → synthesis is not armed yet."""
        slot = await self._done_slot([{"id": "a2"}])
        assert slot._pending_synthesis is False


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _init_heartbeat
# ═══════════════════════════════════════════════════════════════════════════


class TestInitHeartbeat:
    """Heartbeat service initialization."""

    @pytest.mark.asyncio
    async def test_init_heartbeat_creates_service(self):
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.memory = MagicMock()
        orch.ctx_builder.hooks = MagicMock()
        orch.consolidator = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        with patch("kiro_crew.slack.gateway.HeartbeatService") as mock_hs:
            mock_hs_inst = MagicMock()
            mock_hs_inst.start = AsyncMock()
            mock_hs.return_value = mock_hs_inst
            await orch._init_heartbeat()
        assert orch.heartbeat_svc is not None
        mock_hs_inst.start.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _init_task_runner
# ═══════════════════════════════════════════════════════════════════════════


class TestInitTaskRunner:
    """Task runner initialization."""

    def test_init_task_runner_creates_runner(self):
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.hooks = MagicMock()
        orch.conv_log = MagicMock()
        orch.consolidator = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        with patch("kiro_crew.slack.gateway.TaskRunner") as mock_tr:
            mock_tr_inst = MagicMock()
            mock_tr.return_value = mock_tr_inst
            orch._init_task_runner()
        assert orch.task_runner is not None


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _notif_meta
# ═══════════════════════════════════════════════════════════════════════════


class TestNotifMeta:
    """Notification metadata builder."""

    def test_none_for_empty_key(self):
        assert GatewayOrchestrator._notif_meta("") is None
        assert GatewayOrchestrator._notif_meta(None) is None

    def test_dashboard_slot(self):
        result = GatewayOrchestrator._notif_meta("dashboard:my-slot")
        assert result == {"slot": "my-slot"}

    def test_slack_link(self):
        result = GatewayOrchestrator._notif_meta("C123:1234.567890")
        assert result is not None
        assert "slack_link" in result
        assert "C123" in result["slack_link"]

    def test_cron_key_returns_none(self):
        assert GatewayOrchestrator._notif_meta("cron:j1") is None

    def test_subagent_key_returns_none(self):
        assert GatewayOrchestrator._notif_meta("subagent:a1") is None

    def test_hook_key_returns_none(self):
        assert GatewayOrchestrator._notif_meta("hook:h1") is None


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _notify_nudge_expired
# ═══════════════════════════════════════════════════════════════════════════


class TestNotifyNudgeExpired:
    """A monitoring loop that stops at its cycle cap must tell the user.

    Reaching ``max_cycles`` is a runaway backstop, not a finish line — the loop
    stopped with its goal possibly unmet. Previously the only trace was a log
    line plus an ``active=False`` state change indistinguishable from a manual
    Stop, so a capped-out loop looked the same as the agent stopping itself.
    """

    @staticmethod
    def _orch(state):
        """A minimal stand-in — the method only needs dashboard_state."""
        orch = SimpleNamespace(
            dashboard_state=state,
            _notif_meta=GatewayOrchestrator._notif_meta,
        )
        return orch

    def _loop(self, slot_key="chat-7-1700000000"):
        return NudgeLoop(
            id="loop-x",
            slot_key=slot_key,
            message="babysit the PR",
            idle_secs=300,
            max_cycles=24,
            cycle_count=24,
        )

    def test_notifies_with_cycle_counts_and_slot_link(self):
        state = MagicMock()
        GatewayOrchestrator._notify_nudge_expired(self._orch(state), self._loop())
        state.notify.assert_called_once()
        args, kwargs = state.notify.call_args
        # Body names the real numbers so the user can judge whether to resume.
        assert "24 of 24" in args[2]
        # Dashboard loops bind on the BARE slot key; the notification must
        # still deep-link, which requires re-qualifying it for _notif_meta.
        assert kwargs["meta"] == {"slot": "chat-7-1700000000"}

    def test_channel_loop_gets_no_synthesized_meta(self):
        """A channel key must not be fed to _notif_meta at all.

        Its generic ``chan:ts`` split would read the NAMESPACE as the channel
        id — ``slack:1700000000.123456`` became a link to an "archives/slack"
        channel, and a Discord loop got a Slack URL. Asserting only "not a
        slot" passed on exactly that bogus link, so assert the value exactly.
        """
        for key in ("slack:1700000000.123456", "discord:kirocrew:direct:42"):
            state = MagicMock()
            loop = self._loop(slot_key=key)
            GatewayOrchestrator._notify_nudge_expired(self._orch(state), loop)
            state.notify.assert_called_once()
            assert state.notify.call_args.kwargs["meta"] is None, key

    def test_no_dashboard_state_is_a_noop(self):
        # Must not raise when the dashboard isn't wired up (Slack-only host).
        GatewayOrchestrator._notify_nudge_expired(self._orch(None), self._loop())

    def test_notify_failure_never_propagates(self):
        """Runs inside the observer loop — an exception here would also skip
        the WS broadcast that follows it, so it must be swallowed."""
        state = MagicMock()
        state.notify.side_effect = RuntimeError("bus down")
        GatewayOrchestrator._notify_nudge_expired(self._orch(state), self._loop())


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _init_dashboard and _init_mcp_discovery
# ═══════════════════════════════════════════════════════════════════════════


class TestInitDashboard:
    """Dashboard initialization."""

    @pytest.mark.asyncio
    async def test_init_dashboard_creates_state(self):
        orch = _make_orchestrator(test_mode=True)
        orch.sessions = _mock_sessions()
        orch.cron_svc = MagicMock()
        orch.subagent_mgr = MagicMock()
        orch.ctx_builder = MagicMock()
        orch.conv_log = MagicMock()
        orch.consolidator = MagicMock()
        orch.task_runner = MagicMock()
        orch.slack = None
        ds = _mock_dashboard_state()
        runner = MagicMock()
        with patch(
            "kiro_crew.slack.gateway.start_dashboard",
            new_callable=AsyncMock,
            return_value=(runner, ds),
        ) as start:
            await orch._init_dashboard()
        assert orch.dashboard_state is ds
        assert orch._dashboard_runner is runner
        assert start.await_args.kwargs["assume_kiro_ready"] is True

    def test_init_mcp_discovery_logs(self):
        orch = _make_orchestrator()
        with patch("kiro_crew.mcp_discovery.list_servers", return_value=[]):
            orch._init_mcp_discovery()  # should not raise

    def test_init_mcp_discovery_handles_error(self):
        orch = _make_orchestrator()
        with patch("kiro_crew.mcp_discovery.list_servers", side_effect=RuntimeError("fail")):
            orch._init_mcp_discovery()  # should not raise


# ═══════════════════════════════════════════════════════════════════════════
# Tests: Volatile regex patterns
# ═══════════════════════════════════════════════════════════════════════════


class TestVolatilePatterns:
    """Regex constants used in dedup."""

    def test_volatile_re_matches_iso_timestamp(self):
        assert _VOLATILE_RE.search("2026-05-14T10:30:00Z")

    def test_volatile_re_matches_uuid(self):
        assert _VOLATILE_RE.search("550e8400-e29b-41d4-a716-446655440000")

    def test_epoch_re_matches_10_digit(self):
        assert _EPOCH_RE.search("1715700000")

    def test_epoch_re_matches_13_digit(self):
        assert _EPOCH_RE.search("1715700000000")

    def test_constants_values(self):
        assert _MAX_INJECT_ATTEMPTS == 2
        assert _CRON_MSG_LIMIT == 3000
        assert _SUCCESS_REMINDER_SECS == 86400
        assert _FAILURE_REMINDER_SECS == 3600
        assert _EPOCH_WINDOW_SECS == 300


# ═══════════════════════════════════════════════════════════════════════════
# Tests: Cron failure paths
# ═══════════════════════════════════════════════════════════════════════════


class TestCronFailurePaths:
    """Cron callback error handling."""

    @pytest.mark.asyncio
    async def test_cron_callback_failure_alerts(self):
        """First failure sends alert."""
        orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.build_message = MagicMock(return_value=("msg", None))
        orch.ctx_builder.hooks = MagicMock()
        orch.subagent_mgr = MagicMock()
        orch.subagent_mgr.running = []
        orch.dashboard_state = _mock_dashboard_state()
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value="D_U1")
        orch.slack.post_message = AsyncMock()

        with patch("kiro_crew.slack.gateway.CronService") as mock_cs:
            mock_cs_inst = MagicMock()
            mock_cs_inst.start = AsyncMock()
            mock_cs_inst.start_reaper = MagicMock()
            mock_cs_inst.register_active_session_key = MagicMock()
            mock_cs_inst.clear_active_session_key = MagicMock()
            mock_cs.return_value = mock_cs_inst
            mock_cs.create = AsyncMock(return_value=mock_cs_inst)
            await orch._init_cron()

        callback = mock_cs.create.call_args[1]["on_job"]

        job = MagicMock()
        job.script = ""
        job.command = ""
        job.id = "jfail"
        job.name = "fail-job"
        job.persistent_session = True
        job.agent_sequence = []
        job.agent_id = None
        job.channel = ""
        job.created_by = "U1"
        job.approval_mode = "auto"
        job.env = None
        job.acked_items = []
        job.silent = False
        job.thread_ts = None
        job.last_posted_hash = ""
        job.consecutive_dupes = 0
        job.last_posted_at = 0.0
        job.last_failure_hash = ""
        job.last_failure_at = 0.0
        job.consecutive_failures = 0
        job.auto_paused = False
        job._acp_retried = False

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            with patch(
                "kiro_crew.slack.gateway.build_cron_session_context",
                return_value=("cron:jfail", "run"),
            ):
                with patch("kiro_crew.sel.sel") as mock_sel:
                    mock_sel.return_value.log_tool_invocation = MagicMock()
                    with pytest.raises(RuntimeError, match="boom"):
                        await callback(job)

        orch.slack.post_message.assert_awaited()
        # Failure accounting is single-owned by CronJob.record_failure() so the
        # auto-pause threshold stays reachable from the delivery path.
        job.record_failure.assert_called_once()

    @pytest.mark.asyncio
    async def test_cron_callback_failure_dedup_suppresses(self):
        """Duplicate failure within window suppresses Slack."""
        orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.build_message = MagicMock(return_value=("msg", None))
        orch.ctx_builder.hooks = MagicMock()
        orch.subagent_mgr = MagicMock()
        orch.subagent_mgr.running = []
        orch.dashboard_state = _mock_dashboard_state()
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value="D_U1")
        orch.slack.post_message = AsyncMock()

        with patch("kiro_crew.slack.gateway.CronService") as mock_cs:
            mock_cs_inst = MagicMock()
            mock_cs_inst.start = AsyncMock()
            mock_cs_inst.start_reaper = MagicMock()
            mock_cs_inst.register_active_session_key = MagicMock()
            mock_cs_inst.clear_active_session_key = MagicMock()
            mock_cs.return_value = mock_cs_inst
            mock_cs.create = AsyncMock(return_value=mock_cs_inst)
            await orch._init_cron()

        callback = mock_cs.create.call_args[1]["on_job"]

        job = MagicMock()
        job.script = ""
        job.command = ""
        job.id = "jfail2"
        job.name = "fail-dedup"
        job.persistent_session = True
        job.agent_sequence = []
        job.agent_id = None
        job.channel = ""
        job.created_by = "U1"
        job.approval_mode = "auto"
        job.env = None
        job.acked_items = []
        job.silent = False
        job.thread_ts = None
        job.last_posted_hash = ""
        job.consecutive_dupes = 0
        job.last_posted_at = 0.0
        # Pre-set failure hash to match what will be generated
        job.last_failure_hash = _result_hash("RuntimeError: boom")
        job.last_failure_at = time.time()
        job.consecutive_failures = 1
        job.auto_paused = False
        job._acp_retried = False

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            with patch(
                "kiro_crew.slack.gateway.build_cron_session_context",
                return_value=("cron:jfail2", "run"),
            ):
                with patch("kiro_crew.sel.sel") as mock_sel:
                    mock_sel.return_value.log_tool_invocation = MagicMock()
                    with pytest.raises(RuntimeError, match="boom"):
                        await callback(job)

        # Slack should NOT be called (suppressed)
        orch.slack.post_message.assert_not_awaited()
        # A suppressed duplicate still counts toward auto-pause via the
        # counter's single owner.
        job.record_failure.assert_called_once()

    @pytest.mark.asyncio
    async def test_cron_multi_agent_sequence(self):
        """Multi-agent sequence runs agents sequentially."""
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.build_message = MagicMock(return_value=("msg", None))
        orch.ctx_builder.hooks = MagicMock()
        orch.subagent_mgr = MagicMock()
        orch.subagent_mgr.running = []
        orch.dashboard_state = None
        orch.slack = None

        with patch("kiro_crew.slack.gateway.CronService") as mock_cs:
            mock_cs_inst = MagicMock()
            mock_cs_inst.start = AsyncMock()
            mock_cs_inst.start_reaper = MagicMock()
            mock_cs_inst.register_active_session_key = MagicMock()
            mock_cs_inst.clear_active_session_key = MagicMock()
            mock_cs.return_value = mock_cs_inst
            mock_cs.create = AsyncMock(return_value=mock_cs_inst)
            await orch._init_cron()

        callback = mock_cs.create.call_args[1]["on_job"]

        job = MagicMock()
        job.script = ""
        job.command = ""
        job.id = "jmulti"
        job.name = "multi-agent"
        job.persistent_session = True
        job.agent_sequence = ["agent-a", "agent-b"]
        job.agent_id = None
        job.channel = ""
        job.created_by = ""
        job.approval_mode = "auto"
        job.env = None
        job.acked_items = []
        job.silent = False
        job.thread_ts = None
        job.last_posted_hash = ""
        job.consecutive_dupes = 0
        job.last_posted_at = 0.0
        job.last_failure_hash = ""
        job.last_failure_at = 0.0
        job.consecutive_failures = 0

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            return_value="agent result",
        ):
            with patch(
                "kiro_crew.slack.gateway.build_cron_session_context",
                return_value=("cron:jmulti", "run"),
            ):
                result = await callback(job)

        assert result == "agent result"
        job.set_run_result.assert_called_once_with("agent result")
        # get_or_create called twice (once per agent)
        assert orch.sessions.get_or_create.await_count == 2


# ═══════════════════════════════════════════════════════════════════════════
# Tests: run_gateway entry point
# ═══════════════════════════════════════════════════════════════════════════


class TestRunGateway:
    """Top-level run_gateway function."""

    @pytest.mark.asyncio
    async def test_run_gateway_creates_orchestrator(self):
        from kiro_crew.slack.gateway import run_gateway

        cfg = KiroCrewConfig()
        with patch.object(cfg, "load_credentials", return_value={}):
            # The aggregate-cgroup-ceiling apply shells out to systemctl —
            # a host-service mutation the rootdir guard refuses; stub it.
            with patch("kiro_crew.slack.gateway.ensure_agents_slice_limits", return_value=True):
                with patch.object(GatewayOrchestrator, "run", new_callable=AsyncMock) as mock_run:
                    await run_gateway(cfg, no_dashboard=True, no_crons=True)
        mock_run.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _init_autonudge
# ═══════════════════════════════════════════════════════════════════════════


class TestInitAutonudge:
    """AutoNudge service initialization."""

    @pytest.mark.asyncio
    async def test_disabled_when_feature_flag_off(self):
        orch = _make_orchestrator()
        with patch("kiro_crew.slack.gateway.autonudge_enabled", return_value=False):
            await orch._init_autonudge()
        assert not hasattr(orch, "autonudge_svc") or orch.autonudge_svc is None  # noqa: E501

    @pytest.mark.asyncio
    async def test_enabled_creates_service(self):
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        with patch("kiro_crew.slack.gateway.autonudge_enabled", return_value=True):
            with patch("kiro_crew.slack.gateway.AutoNudgeService") as mock_ans:
                mock_inst = MagicMock()
                mock_inst.start = AsyncMock()
                mock_inst.subscribe = MagicMock()
                mock_ans.return_value = mock_inst
                await orch._init_autonudge()
        assert orch.autonudge_svc is not None
        mock_inst.start.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _init_secretary
# ═══════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _auto_apply_update git path
# ═══════════════════════════════════════════════════════════════════════════


def _git_exec_fake(
    *,
    branch: bytes = b"mainline\n",
    fetch_rc: int = 0,
    diff_rc: int = 1,
    status_out: bytes = b"",
    status_rc: int = 0,
    reset_rc: int = 0,
    other_rc: int = 0,
    target: bytes = b"0" * 40 + b"\n",
    added_out: bytes = b"",
    added_rc: int = 0,
    record: list | None = None,
):
    """A `create_subprocess_exec` fake that dispatches on the GIT SUBCOMMAND.

    Deliberately NOT a call counter. `_auto_apply_update` has gained git calls
    three times, and each time every counter-keyed fake silently re-mapped its
    own cases -- the stub written for `fetch` started answering the `diff`, and
    the tests failed for a reason that had nothing to do with what they assert.
    Dispatching on argv keeps each stub bound to the command it describes, so
    inserting a call is not a test edit.

    Defaults describe the interesting path: a branch is detected, the fetch
    succeeds, the diff reports changes (rc=1), the tree is clean, the target adds
    no colliding paths, the reset succeeds, and non-git spawns succeed.
    `other_rc` fails the latter while leaving every git step green.

    Note that `git diff` is now TWO distinct calls with opposite rc conventions,
    so this dispatches on their flags. Subcommand alone is not always enough --
    when a new call reuses a subcommand, the discriminator has to get narrower.
    """

    async def _fake(*args, **kwargs):
        if record is not None:
            record.append(args)
        argv = [a for a in args if isinstance(a, str)]
        proc = AsyncMock()
        proc.kill = MagicMock()
        out: bytes = b""
        rc = 0
        if "rev-parse" in argv and "--abbrev-ref" in argv:
            out = branch
        elif "rev-parse" in argv:
            out = target
        elif "rev-list" in argv:
            out = b"0\n"
        elif "fetch" in argv:
            rc = fetch_rc
        elif "diff" in argv and any(a.startswith("--diff-filter") for a in argv):
            # The added-paths listing before the reset. Distinguished by its
            # flags, not by call order: the `--quiet` check below uses rc as a
            # BOOLEAN ("there are differences"), while this one uses rc for
            # success and returns paths on stdout -- so answering both from one
            # `"diff" in argv` branch made every happy-path test refuse.
            out = added_out
            rc = added_rc
        elif "diff" in argv:
            rc = diff_rc
        elif "status" in argv:
            out = status_out
            rc = status_rc
        elif "reset" in argv:
            rc = reset_rc
        else:
            # Not a git step: the dependency install, the core-dep repair, the
            # optional kiro-cli update.
            rc = other_rc
        proc.returncode = rc
        proc.communicate = AsyncMock(return_value=(out, b""))
        proc.wait = AsyncMock(return_value=rc)
        return proc

    return _fake


class TestAutoApplyUpdateGitPath:
    """Git-based auto-update (non-toolbox)."""

    @pytest.fixture(autouse=True)
    def _permit_update_preconditions(self):
        """Neutralize the two seam preconditions so these tests keep their subject.

        `_auto_apply_update` refuses outright when the checkout declares a
        repo-named git driver, when the branch does not track the remote it resets
        to, or when a tracked edit is hidden by assume-unchanged. All read the REAL
        git metadata of ``KIROCREW_PROJECT_DIR``, which these tests point at a path
        that is not a repo — so without this they would all pass vacuously by
        refusing before reaching the fetch/reset sequence they exist to cover. The
        refusals have their own tests in ``TestAutoApplyUpdatePreconditions`` and
        ``TestAutoApplyUpdateResetPath``.
        """
        with (
            patch("kiro_crew.slack.gateway.hidden_worktree_edits", return_value=[]),
            patch(
                "kiro_crew.slack.gateway.repo_exec_config_reason",
                return_value="",
            ),
            patch("kiro_crew.slack.gateway.tracks_upstream", return_value=True),
            patch("kiro_crew.slack.gateway.commits_ahead", return_value=0),
        ):
            yield

    @pytest.mark.asyncio
    async def test_fetch_fails_returns_early(self):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds

        # branch detection succeeds, fetch fails
        _fake_exec = _git_exec_fake(fetch_rc=1)

        with patch("kiro_crew.env.is_toolbox_install", return_value=False):
            with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}):
                with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                    await orch._auto_apply_update()
        ds.clear_update_progress.assert_called()

    @pytest.mark.asyncio
    async def test_no_diff_returns_early(self):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds

        # diff --quiet reports no difference, so the run stops before the reset.
        _fake_exec = _git_exec_fake(diff_rc=0)

        with patch("kiro_crew.env.is_toolbox_install", return_value=False):
            with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}):
                with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                    await orch._auto_apply_update()
        ds.clear_update_progress.assert_called()


# ═══════════════════════════════════════════════════════════════════════════
# Tests: run method (partial — covers init sequence)
# ═══════════════════════════════════════════════════════════════════════════


class TestRunMethod:
    """Gateway run method."""

    @pytest.mark.asyncio
    async def test_run_raises_on_shutdown(self):
        """run() exits when shutdown_event is set."""
        import kiro_crew

        orch = _make_orchestrator()

        # Mock all init methods
        orch._init_services = AsyncMock()
        # _init_services is mocked so vector_memory never gets created — mock
        # the default-on embeddings wiring too (it dereferences vector_memory).
        orch._start_embeddings = AsyncMock()
        # run() spawns _auto_migrate_memory as a fire-and-forget task; with
        # _init_services mocked it would raise AttributeError on
        # vector_memory and surface as an unretrieved-task error at GC time.
        orch._auto_migrate_memory = AsyncMock()
        orch._init_cron = AsyncMock()
        orch._init_heartbeat = AsyncMock()
        orch._init_mcp_discovery = MagicMock()
        orch._init_subagents = MagicMock()
        orch._init_task_runner = MagicMock()
        orch._init_dashboard = AsyncMock()
        orch._init_autonudge = AsyncMock()
        orch._check_for_updates = AsyncMock()
        orch._shutdown = AsyncMock()

        # Set shutdown immediately
        kiro_crew.shutdown_event.set()
        # loop.add_signal_handler -> set_wakeup_fd fails on xdist worker threads.
        loop = asyncio.get_running_loop()
        try:
            with patch.object(loop, "add_signal_handler"):
                with patch("kiro_crew.slack.events.init_socket_mode"):
                    with patch("kiro_crew.slack.interactions.init"):
                        with patch("kiro_crew.slack.events.SeenCache"):
                            with patch("kiro_crew.session.cleanup_orphaned_sessions"):
                                with patch(
                                    "kiro_crew.dashboard.handlers._bg_mcp_probe",
                                    new_callable=AsyncMock,
                                ):
                                    with patch("os._exit"):
                                        with patch("resource.getrlimit", return_value=(256, 10240)):
                                            with patch("resource.setrlimit"):
                                                await orch.run()
        finally:
            kiro_crew.shutdown_event.clear()

        orch._init_services.assert_called_once()
        orch._init_cron.assert_awaited_once()
        orch._shutdown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_run_no_dashboard_uses_api_server(self, tmp_path, monkeypatch):
        """--no-dashboard waits for and then clears its run marker."""
        import kiro_crew
        from kiro_crew.instances import run_marker

        orch = _make_orchestrator(no_dashboard=True)
        orch._dashboard_port = 5476

        monkeypatch.setattr(run_marker, "config_dir", lambda: tmp_path)
        run_marker.write_marker(orch._dashboard_port)
        original_write_marker = run_marker.write_marker
        events = []

        def slow_write_marker(port):
            events.append("write-start")
            time.sleep(0.05)
            original_write_marker(port)
            events.append("write-end")

        original_clear_marker = run_marker.clear_marker

        def recording_clear_marker(port):
            events.append("clear")
            original_clear_marker(port)

        monkeypatch.setattr(run_marker, "write_marker", slow_write_marker)
        monkeypatch.setattr(run_marker, "clear_marker", recording_clear_marker)
        marker = run_marker.marker_path(orch._dashboard_port)
        pid_marker = run_marker.pid_path(orch._dashboard_port)
        assert marker.exists()
        assert pid_marker.exists()

        orch._init_services = AsyncMock()
        orch._start_embeddings = AsyncMock()
        # run() spawns _auto_migrate_memory as a fire-and-forget task; with
        # _init_services mocked it would raise AttributeError on
        # vector_memory and surface as an unretrieved-task error at GC time.
        orch._auto_migrate_memory = AsyncMock()
        orch._init_cron = AsyncMock()
        orch._init_heartbeat = AsyncMock()
        orch._init_mcp_discovery = MagicMock()
        orch._init_subagents = MagicMock()
        orch._init_task_runner = MagicMock()
        orch._init_dashboard = AsyncMock()
        orch._init_api_server = AsyncMock()
        orch._init_autonudge = AsyncMock()
        orch._check_for_updates = AsyncMock()
        orch._shutdown = AsyncMock()

        kiro_crew.shutdown_event.set()
        loop = asyncio.get_running_loop()
        try:
            with patch.object(loop, "add_signal_handler"):
                with patch("kiro_crew.slack.events.init_socket_mode"):
                    with patch("kiro_crew.slack.interactions.init"):
                        with patch("kiro_crew.slack.events.SeenCache"):
                            with patch("kiro_crew.session.cleanup_orphaned_sessions"):
                                with patch(
                                    "kiro_crew.dashboard.handlers._bg_mcp_probe",
                                    new_callable=AsyncMock,
                                ):
                                    with patch("os._exit"):
                                        with patch("resource.getrlimit", return_value=(256, 10240)):
                                            with patch("resource.setrlimit"):
                                                await orch.run()
        finally:
            kiro_crew.shutdown_event.clear()

        orch._init_dashboard.assert_not_awaited()
        orch._init_api_server.assert_awaited_once()
        assert events == ["write-start", "write-end", "clear"]
        assert not marker.exists()
        assert not pid_marker.exists()

    @pytest.mark.asyncio
    async def test_run_stalled_marker_write_does_not_block_shutdown(self, tmp_path, monkeypatch):
        """A hung marker write times out; the marker is cleared and _shutdown runs.

        Regression: an unbounded ``await self._marker_write_task`` sat before
        the bounded ``_shutdown()`` call, so a stalled write consumed the
        graceful-shutdown deadline and active slots were SIGKILLed unsaved.
        """
        import kiro_crew
        from kiro_crew.instances import run_marker
        from kiro_crew.slack import gateway as gateway_mod

        orch = _make_orchestrator(no_dashboard=True)
        orch._dashboard_port = 5477

        monkeypatch.setattr(run_marker, "config_dir", lambda: tmp_path)
        run_marker.write_marker(orch._dashboard_port)
        # Shrink the wait budget so the timeout path runs fast in tests.
        monkeypatch.setattr(gateway_mod, "_MARKER_WRITE_WAIT_SECS", 0.05)

        events = []
        stall = threading.Event()
        original_write_marker = run_marker.write_marker
        original_clear_marker = run_marker.clear_marker

        def stalled_write_marker(port):
            events.append("write-start")
            stall.wait(5.0)  # far longer than the shrunk 0.05s budget
            original_write_marker(port)  # late write republishes the marker
            events.append("write-end")

        def recording_clear_marker(port):
            events.append("clear")
            original_clear_marker(port)

        monkeypatch.setattr(run_marker, "write_marker", stalled_write_marker)
        monkeypatch.setattr(run_marker, "clear_marker", recording_clear_marker)
        marker = run_marker.marker_path(orch._dashboard_port)
        pid_marker = run_marker.pid_path(orch._dashboard_port)
        assert marker.exists()

        orch._init_services = AsyncMock()
        orch._start_embeddings = AsyncMock()
        orch._auto_migrate_memory = AsyncMock()
        orch._init_cron = AsyncMock()
        orch._init_heartbeat = AsyncMock()
        orch._init_mcp_discovery = MagicMock()
        orch._init_subagents = MagicMock()
        orch._init_task_runner = MagicMock()
        orch._init_dashboard = AsyncMock()
        orch._init_api_server = AsyncMock()
        orch._init_autonudge = AsyncMock()
        orch._check_for_updates = AsyncMock()
        orch._shutdown = AsyncMock()

        kiro_crew.shutdown_event.set()
        loop = asyncio.get_running_loop()
        try:
            with patch.object(loop, "add_signal_handler"):
                with patch("kiro_crew.slack.events.init_socket_mode"):
                    with patch("kiro_crew.slack.interactions.init"):
                        with patch("kiro_crew.slack.events.SeenCache"):
                            with patch("kiro_crew.session.cleanup_orphaned_sessions"):
                                with patch(
                                    "kiro_crew.dashboard.handlers._bg_mcp_probe",
                                    new_callable=AsyncMock,
                                ):
                                    with patch("os._exit"):
                                        with patch("resource.getrlimit", return_value=(256, 10240)):
                                            with patch("resource.setrlimit"):
                                                await orch.run()
        finally:
            kiro_crew.shutdown_event.clear()
            # Release AND drain the stalled writer inside this finally: if an
            # assertion below failed with the worker still parked, monkeypatch
            # teardown would restore the real config_dir/write_marker and the
            # worker would wake later and write markers OUTSIDE tmp_path.
            stall.set()
            for _ in range(200):  # up to ~10s; normally a few ms
                if "write-start" not in events or events.count("clear") >= 2:
                    break
                await asyncio.sleep(0.05)

        # The stalled write did not complete before the bounded wait expired,
        # yet the marker was cleared and graceful shutdown still ran — the
        # timeout kept the deadline intact.
        assert events[:2] == ["write-start", "clear"]
        orch._shutdown.assert_awaited_once()

        # The released writer republished the marker files after the
        # timed-out clear. The writer thread must then self-clear them
        # (same thread, no event-loop callback os._exit could beat),
        # otherwise a stopped gateway leaves stale runtime state behind.
        assert "write-end" in events
        assert events.index("write-end") > events.index("clear")
        assert events.count("clear") == 2  # timed-out clear + writer self-clear
        assert not marker.exists()
        assert not pid_marker.exists()

    @pytest.mark.asyncio
    async def test_run_failed_marker_write_still_clears(self, tmp_path, monkeypatch):
        """A marker write that raises must not skip the shutdown clear."""
        import kiro_crew
        from kiro_crew.instances import run_marker

        orch = _make_orchestrator(no_dashboard=True)
        orch._dashboard_port = 5478

        monkeypatch.setattr(run_marker, "config_dir", lambda: tmp_path)
        run_marker.write_marker(orch._dashboard_port)  # pre-existing marker

        def failing_write_marker(port):
            raise OSError("disk full")

        monkeypatch.setattr(run_marker, "write_marker", failing_write_marker)
        marker = run_marker.marker_path(orch._dashboard_port)
        pid_marker = run_marker.pid_path(orch._dashboard_port)
        assert marker.exists()

        orch._init_services = AsyncMock()
        orch._start_embeddings = AsyncMock()
        orch._auto_migrate_memory = AsyncMock()
        orch._init_cron = AsyncMock()
        orch._init_heartbeat = AsyncMock()
        orch._init_mcp_discovery = MagicMock()
        orch._init_subagents = MagicMock()
        orch._init_task_runner = MagicMock()
        orch._init_dashboard = AsyncMock()
        orch._init_api_server = AsyncMock()
        orch._init_autonudge = AsyncMock()
        orch._check_for_updates = AsyncMock()
        orch._shutdown = AsyncMock()

        kiro_crew.shutdown_event.set()
        loop = asyncio.get_running_loop()
        try:
            with patch.object(loop, "add_signal_handler"):
                with patch("kiro_crew.slack.events.init_socket_mode"):
                    with patch("kiro_crew.slack.interactions.init"):
                        with patch("kiro_crew.slack.events.SeenCache"):
                            with patch("kiro_crew.session.cleanup_orphaned_sessions"):
                                with patch(
                                    "kiro_crew.dashboard.handlers._bg_mcp_probe",
                                    new_callable=AsyncMock,
                                ):
                                    with patch("os._exit"):
                                        with patch("resource.getrlimit", return_value=(256, 10240)):
                                            with patch("resource.setrlimit"):
                                                await orch.run()
        finally:
            kiro_crew.shutdown_event.clear()

        # The failed write must not divert control past the clear: the
        # pre-existing marker files are removed and shutdown completed.
        assert not marker.exists()
        assert not pid_marker.exists()
        orch._shutdown.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _init_api_server
# ═══════════════════════════════════════════════════════════════════════════


class TestInitApiServer:
    """API-only server initialization."""

    @pytest.mark.asyncio
    async def test_init_api_server(self):
        orch = _make_orchestrator(test_mode=True)
        orch.sessions = _mock_sessions()
        orch.cron_svc = MagicMock()
        orch.subagent_mgr = MagicMock()
        orch.task_runner = MagicMock()
        orch.slack = None
        ds = _mock_dashboard_state()
        runner = MagicMock()
        with patch(
            "kiro_crew.dashboard.start_api_server",
            new_callable=AsyncMock,
            return_value=(runner, ds),
        ) as start:
            await orch._init_api_server()
        assert orch.dashboard_state is ds
        assert start.await_args.kwargs["assume_kiro_ready"] is True


# ═══════════════════════════════════════════════════════════════════════════
# Tests: Cron success reminder after 24h
# ═══════════════════════════════════════════════════════════════════════════


class TestCronSuccessReminder:
    """Cron dedup reminder after 24h."""

    @pytest.mark.asyncio
    async def test_success_reminder_after_24h(self):
        """After 24h of same result, re-posts with warning."""
        orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.build_message = MagicMock(return_value=("msg", None))
        orch.ctx_builder.hooks = MagicMock()
        orch.subagent_mgr = MagicMock()
        orch.subagent_mgr.running = []
        orch.dashboard_state = _mock_dashboard_state()
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value="D_U1")
        orch.slack.post_blocks = AsyncMock(return_value="ts1")
        orch.slack.post_message = AsyncMock()

        with patch("kiro_crew.slack.gateway.CronService") as mock_cs:
            mock_cs_inst = MagicMock()
            mock_cs_inst.start = AsyncMock()
            mock_cs_inst.start_reaper = MagicMock()
            mock_cs_inst.register_active_session_key = MagicMock()
            mock_cs_inst.clear_active_session_key = MagicMock()
            mock_cs.return_value = mock_cs_inst
            mock_cs.create = AsyncMock(return_value=mock_cs_inst)
            await orch._init_cron()

        callback = mock_cs.create.call_args[1]["on_job"]

        job = MagicMock()
        job.script = ""
        job.command = ""
        job.id = "j_remind"
        job.name = "reminder-job"
        job.persistent_session = True
        job.agent_sequence = []
        job.agent_id = None
        job.channel = ""
        job.created_by = "U1"
        job.approval_mode = "auto"
        job.env = None
        job.acked_items = []
        job.silent = False
        job.thread_ts = None
        job.last_posted_hash = _result_hash("same output")
        job.consecutive_dupes = 5
        # Posted more than 24h ago
        job.last_posted_at = time.time() - _SUCCESS_REMINDER_SECS - 100
        job.last_failure_hash = ""
        job.last_failure_at = 0.0
        job.consecutive_failures = 0

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            return_value="same output",
        ):
            with patch(
                "kiro_crew.slack.gateway.build_cron_session_context",
                return_value=("cron:j_remind", "run"),
            ):
                result = await callback(job)

        # Should have posted (reminder path)
        orch.slack.post_blocks.assert_awaited()
        assert "same result" in result


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _subagent_done callback (via _init_subagents)
# ═══════════════════════════════════════════════════════════════════════════


class TestSubagentDone:
    """Subagent completion routing."""

    def _setup_orch_with_subagent_mgr(self):
        """Create orchestrator with subagent manager initialized."""
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.hooks = MagicMock()
        orch.ctx_builder.build_message = MagicMock(return_value=("msg", None))
        orch.dashboard_state = _mock_dashboard_state()
        with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False):
            with patch("kiro_crew.slack.gateway.SubagentManager") as mock_sm:
                mock_sm_inst = MagicMock()
                mock_sm_inst.start_reaper = MagicMock()
                mock_sm_inst.running = []
                mock_sm_inst.queued_count_for = MagicMock(return_value=0)
                mock_sm_inst.has_pending_work_for = MagicMock(return_value=False)
                mock_sm_inst.running_agents_for = MagicMock(return_value=[])
                mock_sm_inst.get = MagicMock(return_value=None)
                mock_sm_inst.notify_injection_failed = MagicMock()
                mock_sm.return_value = mock_sm_inst
                orch._init_subagents()
        return orch, mock_sm

    @pytest.mark.asyncio
    async def test_dashboard_slot_idle_triggers_run_chat(self):
        """Subagent done → dashboard slot idle → _run_chat."""
        orch, mock_sm = self._setup_orch_with_subagent_mgr()
        # Get the on_done callback
        on_done = mock_sm.call_args[1]["on_done"]

        slot = MagicMock()
        slot.running = False
        slot.task = None
        slot.key = "test-slot"
        slot.mode = ""
        slot._recovery_chat_triggered = False
        slot._pending_subagent_failures = []
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)

        info = MagicMock()
        info.id = "agent-1"
        info.parent_session_key = "dashboard:test-slot"
        info.error = None
        info.result = "done!"
        info.result_path = ""
        info.task = "do something"
        info.agent = "coder"
        info.silent = False
        info.elapsed = 5.0
        info.started = 0.0

        with patch(
            "kiro_crew.dashboard.chat_runner._run_chat", new_callable=AsyncMock, return_value=None
        ):
            await on_done(info)

        orch.dashboard_state.notify.assert_not_called()
        orch.dashboard_state.push_slots_update.assert_called()

    @pytest.mark.asyncio
    async def test_dashboard_slot_busy_queues(self):
        """Subagent done → dashboard slot busy → queues message."""
        orch, mock_sm = self._setup_orch_with_subagent_mgr()
        on_done = mock_sm.call_args[1]["on_done"]

        slot = MagicMock()
        slot.running = True
        # Create a task that completes but slot stays running
        never_done = asyncio.get_event_loop().create_future()
        never_done.set_result(None)
        slot.task = asyncio.ensure_future(asyncio.sleep(0))
        await slot.task  # let it complete
        # But slot.running stays True (simulating another claim)
        slot.running = True
        slot.key = "busy-slot"
        slot.mode = ""
        slot._recovery_chat_triggered = False
        slot._pending_subagent_failures = []
        slot.queue_append = MagicMock()
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)

        info = MagicMock()
        info.id = "agent-2"
        info.parent_session_key = "dashboard:busy-slot"
        info.error = None
        info.result = "queued result"
        info.result_path = ""
        info.task = "task"
        info.agent = ""
        info.silent = False
        info.elapsed = 1.0
        info.started = 0.0

        await on_done(info)
        slot.queue_append.assert_called_once()

    @pytest.mark.asyncio
    async def test_recovered_interruption_keeps_neutral_outcome_and_result_path(self):
        from kiro_crew.constants import SUBAGENT_COMPLETION_META_KEY
        from kiro_crew.subagent import SubagentInfo

        orch, mock_sm = self._setup_orch_with_subagent_mgr()
        on_done = mock_sm.call_args[1]["on_done"]

        slot = MagicMock()
        slot.running = True
        slot.task = asyncio.ensure_future(asyncio.sleep(0))
        await slot.task
        slot.key = "busy-slot"
        slot.mode = ""
        slot._recovery_chat_triggered = False
        slot._pending_subagent_failures = []
        slot._subagents_inline_collected = set()
        slot.queue_append = MagicMock()
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)

        info = SubagentInfo(
            id="recovered-1",
            task="recover work",
            parent_session_key="dashboard:busy-slot",
            done=True,
            error="interrupted by gateway restart",
            result_path="/results/recovered-1.txt",
            result_truncated=True,
        )
        info._recovered_outcome = "interrupted"

        await on_done(info)

        announce = slot.queue_append.call_args.args[0]
        meta = slot.queue_append.call_args.kwargs["meta"][SUBAGENT_COMPLETION_META_KEY]
        assert "interrupted by gateway restart" in announce
        assert "/results/recovered-1.txt" in announce
        assert meta["outcome"] == "interrupted"

    @pytest.mark.asyncio
    async def test_cron_parent_injects_result(self):
        """Subagent done → cron parent → injects into session."""
        orch, mock_sm = self._setup_orch_with_subagent_mgr()
        on_done = mock_sm.call_args[1]["on_done"]
        orch.subagent_mgr.running = []

        info = MagicMock()
        info.id = "agent-3"
        info.parent_session_key = "cron:job1"
        info.error = None
        info.result = "cron agent result"
        info.result_path = ""
        info.task = "cron task"
        info.agent = ""
        info.silent = False
        info.elapsed = 2.0
        info.started = 0.0

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            return_value="llm response",
        ):
            await on_done(info)

        orch.dashboard_state.notify.assert_called()

    @pytest.mark.asyncio
    async def test_notification_only_for_unknown_parent(self):
        """Subagent done → unknown parent → notification only."""
        orch, mock_sm = self._setup_orch_with_subagent_mgr()
        on_done = mock_sm.call_args[1]["on_done"]

        info = MagicMock()
        info.id = "agent-4"
        info.parent_session_key = "subagent:parent"
        info.error = "something failed"
        info.result = None
        info.result_path = ""
        info.task = "failed task"
        info.agent = ""
        info.silent = False
        info.elapsed = 0.5
        info.started = 0.0

        await on_done(info)
        orch.dashboard_state.notify.assert_called()

    @pytest.mark.asyncio
    async def test_silent_subagent_no_notification(self):
        """Silent subagent → no dashboard notification."""
        orch, mock_sm = self._setup_orch_with_subagent_mgr()
        on_done = mock_sm.call_args[1]["on_done"]

        info = MagicMock()
        info.id = "agent-5"
        info.parent_session_key = "subagent:x"
        info.error = None
        info.result = "silent"
        info.result_path = ""
        info.task = "quiet task"
        info.agent = ""
        info.silent = True
        info.elapsed = 1.0
        info.started = 0.0

        await on_done(info)
        orch.dashboard_state.notify.assert_not_called()

    @pytest.mark.asyncio
    async def test_slack_parent_injects(self):
        """Subagent done → Slack parent → injects into session."""
        orch, mock_sm = self._setup_orch_with_subagent_mgr()
        on_done = mock_sm.call_args[1]["on_done"]
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value="D_U1")
        orch.slack.post_message = AsyncMock()
        orch.slack.post_blocks = AsyncMock(return_value="ts")

        info = MagicMock()
        info.id = "agent-6"
        info.parent_session_key = "C123:1234.567890"
        info.error = None
        info.result = "slack result"
        info.result_path = ""
        info.task = "slack task"
        info.agent = ""
        info.silent = False
        info.elapsed = 3.0
        info.started = time.monotonic() - 3.0

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            return_value="synthesized response",
        ):
            await on_done(info)

        orch.dashboard_state.notify.assert_called()

    @pytest.mark.asyncio
    async def test_dashboard_slot_gone_notification_only(self):
        """Subagent done → dashboard slot gone → notification only."""
        orch, mock_sm = self._setup_orch_with_subagent_mgr()
        on_done = mock_sm.call_args[1]["on_done"]
        orch.dashboard_state.get_slot = MagicMock(return_value=None)

        info = MagicMock()
        info.id = "agent-7"
        info.parent_session_key = "dashboard:gone-slot"
        info.error = None
        info.result = "orphan result"
        info.result_path = ""
        info.task = "orphan task"
        info.agent = ""
        info.silent = False
        info.elapsed = 1.0
        info.started = 0.0

        await on_done(info)
        orch.dashboard_state.notify.assert_called()


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
# Tests: Heartbeat callback
# ═══════════════════════════════════════════════════════════════════════════


class TestHeartbeatCallback:
    """Heartbeat task execution callback."""

    @pytest.mark.asyncio
    async def test_heartbeat_task_success(self):
        orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.memory = MagicMock()
        orch.ctx_builder.build_message = MagicMock(return_value=("msg", None))
        orch.ctx_builder.hooks = MagicMock()
        orch.consolidator = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value="D_U1")
        orch.slack.post_message = AsyncMock()
        orch._deliver_result = AsyncMock()

        with patch("kiro_crew.slack.gateway.HeartbeatService") as mock_hs:
            mock_hs_inst = MagicMock()
            mock_hs_inst.start = AsyncMock()
            mock_hs.return_value = mock_hs_inst
            await orch._init_heartbeat()

        # Get the on_task callback
        callback = mock_hs.call_args[1]["on_task"]

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            return_value="heartbeat done",
        ):
            result = await callback("check status", "")

        assert result == "heartbeat done"
        orch._deliver_result.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_heartbeat_task_keep_response(self):
        """HEARTBEAT_KEEP response suppresses delivery."""
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.memory = MagicMock()
        orch.ctx_builder.build_message = MagicMock(return_value=("msg", None))
        orch.ctx_builder.hooks = MagicMock()
        orch.consolidator = MagicMock()
        orch.dashboard_state = None
        orch._deliver_result = AsyncMock()

        with patch("kiro_crew.slack.gateway.HeartbeatService") as mock_hs:
            mock_hs_inst = MagicMock()
            mock_hs_inst.start = AsyncMock()
            mock_hs.return_value = mock_hs_inst
            await orch._init_heartbeat()

        callback = mock_hs.call_args[1]["on_task"]

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            return_value="still checking HEARTBEAT_KEEP",
        ):
            result = await callback("poll endpoint", "dashboard:s1")

        assert "HEARTBEAT_KEEP" in result
        orch._deliver_result.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_heartbeat_task_failure(self):
        """Heartbeat task exception propagates."""
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.memory = MagicMock()
        orch.ctx_builder.build_message = MagicMock(return_value=("msg", None))
        orch.ctx_builder.hooks = MagicMock()
        orch.consolidator = MagicMock()
        orch.dashboard_state = None

        with patch("kiro_crew.slack.gateway.HeartbeatService") as mock_hs:
            mock_hs_inst = MagicMock()
            mock_hs_inst.start = AsyncMock()
            mock_hs.return_value = mock_hs_inst
            await orch._init_heartbeat()

        callback = mock_hs.call_args[1]["on_task"]

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            side_effect=RuntimeError("llm error"),
        ):
            with pytest.raises(RuntimeError, match="llm error"):
                await callback("broken task", "")


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _auto_apply_update venv path
# ═══════════════════════════════════════════════════════════════════════════


class TestAutoApplyUpdateVenvPath:
    """Venv-based auto-update (pip install -e .)."""

    @pytest.fixture(autouse=True)
    def _permit_update_preconditions(self):
        """Neutralize the two seam preconditions so these tests keep their subject.

        `_auto_apply_update` refuses outright when the checkout declares a
        repo-named git driver, when the branch does not track the remote it resets
        to, or when a tracked edit is hidden by assume-unchanged. All read the REAL
        git metadata of ``KIROCREW_PROJECT_DIR``, which these tests point at a path
        that is not a repo — so without this they would all pass vacuously by
        refusing before reaching the fetch/reset sequence they exist to cover. The
        refusals have their own tests in ``TestAutoApplyUpdatePreconditions`` and
        ``TestAutoApplyUpdateResetPath``.
        """
        with (
            patch("kiro_crew.slack.gateway.hidden_worktree_edits", return_value=[]),
            patch(
                "kiro_crew.slack.gateway.repo_exec_config_reason",
                return_value="",
            ),
            patch("kiro_crew.slack.gateway.tracks_upstream", return_value=True),
            patch("kiro_crew.slack.gateway.commits_ahead", return_value=0),
        ):
            yield

    @pytest.mark.asyncio
    async def test_venv_update_full_path(self):
        """Full venv update: fetch, diff, reset, pip install, restart."""
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        _fake_exec = _git_exec_fake()

        with patch("kiro_crew.env.is_toolbox_install", return_value=False):
            with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}):
                with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                    with patch(
                        "kiro_crew.dep_sync.sync_or_reinstall", return_value=0
                    ) as mock_install:
                        with patch.object(
                            GatewayOrchestrator, "_is_brazil_install", return_value=False
                        ):
                            with patch(
                                "kiro_crew.slack.gateway.build_frontend_async",
                                new_callable=AsyncMock,
                            ):
                                with patch("os.execv", side_effect=OSError("test")):
                                    with patch("shutil.which", return_value=None):
                                        await orch._auto_apply_update()

        # The install runs through the shared entry point, which picks a reinstall
        # or a dependency-only sync — the gateway is normally started through the
        # console script pip would have to rewrite.
        assert mock_install.call_count == 1
        assert str(mock_install.call_args[0][0]) == str(Path("/tmp/proj"))
        assert str(mock_install.call_args[0][1]) == sys.executable

        ds.push_update_progress.assert_any_call("pulling", "Fetching latest changes…")
        ds.push_update_progress.assert_any_call("building", "Building frontend…")

    @pytest.mark.asyncio
    async def test_kiro_cli_update_timeout_kills_child_and_stays_nonfatal(self):
        """A hung `kiro-cli update` is tree-killed AND the update stays non-fatal.

        Both halves matter (issue #4210). Before the fix, the 120s timeout was
        swallowed by the bare ``except Exception`` → DEBUG, so the run fell
        through to the frontend build and dep reinstall while the ABANDONED
        `kiro-cli update` kept mutating the installation concurrently — the
        same half-replaced-install race the wheel path's CancelledError branch
        exists to prevent. A kill-only assertion would pass on a fix that
        turned the timeout fatal; the non-fatal half pins that the surrounding
        contract (log at DEBUG, continue the update) is unchanged.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        _git_fake = _git_exec_fake()
        kiro_procs: list = []

        async def _fake_exec(*args, **kwargs):
            argv = [a for a in args if isinstance(a, str)]
            if argv and argv[0] == "kiro-cli":
                proc = AsyncMock()
                proc.kill = MagicMock()
                proc.returncode = None
                # The wait never completes inside its 120s budget; raising the
                # timeout from the awaited side is this file's precedent for
                # an expired `asyncio.wait_for` (the arm under test catches
                # the same exception either way).
                proc.wait = AsyncMock(side_effect=asyncio.TimeoutError())
                proc.communicate = AsyncMock(return_value=(b"", b""))
                kiro_procs.append(proc)
                return proc
            return await _git_fake(*args, **kwargs)

        killed: list = []

        async def _fake_kill_and_reap(proc):
            killed.append(proc)

        with patch("kiro_crew.env.is_toolbox_install", return_value=False):
            with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}):
                with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                    with patch(
                        "kiro_crew.dep_sync.sync_or_reinstall", return_value=0
                    ) as mock_install:
                        with patch.object(
                            GatewayOrchestrator, "_is_brazil_install", return_value=False
                        ):
                            with patch(
                                "kiro_crew.slack.gateway.build_frontend_async",
                                new_callable=AsyncMock,
                            ) as mock_build:
                                with patch("os.execv", side_effect=OSError("test")):
                                    # Truthy: the optional kiro-cli step runs.
                                    with patch("shutil.which", return_value="/usr/bin/kiro-cli"):
                                        # The gateway resolves _kill_and_reap
                                        # function-locally on every call, so
                                        # patching the source module reaches it.
                                        with patch(
                                            "kiro_crew.platform.update_provider._kill_and_reap",
                                            side_effect=_fake_kill_and_reap,
                                        ):
                                            await orch._auto_apply_update()

        # Half 1: the hung child was killed (tree kill + bounded reap).
        assert kiro_procs, "the kiro-cli update spawn never happened"
        assert killed == kiro_procs
        # Half 2: the timeout stayed NON-FATAL — the update continued into the
        # frontend build and the dependency install exactly as before.
        mock_build.assert_awaited_once()
        assert mock_install.call_count == 1


# ═══════════════════════════════════════════════════════════════════════════
# Tests: Subagent Slack injection timeout
# ═══════════════════════════════════════════════════════════════════════════


class TestSubagentSlackInjection:
    """Subagent injection into Slack sessions."""

    def _setup(self):
        orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.hooks = MagicMock()
        orch.ctx_builder.build_message = MagicMock(return_value=("msg", None))
        orch.dashboard_state = _mock_dashboard_state()
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value="D_U1")
        orch.slack.post_message = AsyncMock()
        orch.slack.post_blocks = AsyncMock(return_value="ts")
        with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False):
            with patch("kiro_crew.slack.gateway.SubagentManager") as mock_sm:
                mock_sm_inst = MagicMock()
                mock_sm_inst.start_reaper = MagicMock()
                mock_sm_inst.running = []
                mock_sm_inst.queued_count_for = MagicMock(return_value=0)
                mock_sm_inst.has_pending_work_for = MagicMock(return_value=False)
                mock_sm_inst.running_agents_for = MagicMock(return_value=[])
                mock_sm_inst.get = MagicMock(return_value=None)
                mock_sm_inst.notify_injection_failed = MagicMock()
                mock_sm.return_value = mock_sm_inst
                orch._init_subagents()
        return orch, mock_sm

    @pytest.mark.asyncio
    async def test_slack_injection_timeout_retries(self):
        """Slack injection timeout → retries then fails."""
        orch, mock_sm = self._setup()
        on_done = mock_sm.call_args[1]["on_done"]

        info = MagicMock()
        info.id = "agent-timeout"
        info.parent_session_key = "C123:ts.123"
        info.error = None
        info.result = "result"
        info.result_path = ""
        info.task = "task"
        info.agent = ""
        info.silent = False
        info.elapsed = 1.0
        info.started = 0.0

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            side_effect=asyncio.TimeoutError,
        ):
            await on_done(info)

        # Should have notified injection failed
        orch.subagent_mgr.notify_injection_failed.assert_called()

    @pytest.mark.asyncio
    async def test_cron_injection_timeout(self):
        """Cron injection timeout → notifies failure."""
        orch, mock_sm = self._setup()
        on_done = mock_sm.call_args[1]["on_done"]
        orch.subagent_mgr.running = []

        info = MagicMock()
        info.id = "agent-cron-timeout"
        info.parent_session_key = "cron:job1"
        info.error = None
        info.result = "result"
        info.result_path = ""
        info.task = "cron task"
        info.agent = ""
        info.silent = False
        info.elapsed = 1.0
        info.started = 0.0

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            side_effect=asyncio.TimeoutError,
        ):
            await on_done(info)

        orch.subagent_mgr.notify_injection_failed.assert_called()


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _deliver_result truncation
# ═══════════════════════════════════════════════════════════════════════════


class TestDeliverResultTruncation:
    """Prompt truncation for large results."""

    @pytest.mark.asyncio
    async def test_prompt_truncates_large_result(self):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        slot = MagicMock()
        slot.key = "s1"
        slot.enqueue_or_run_prompt = MagicMock(return_value=True)
        slot.queue_depth = 0
        ds.resolve_slot = MagicMock(return_value=slot)
        orch.dashboard_state = ds
        # Create a result larger than MAX_PROMPT_BYTES
        large_result = "x" * 200000
        with patch("kiro_crew.slack.gateway.sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            await orch._deliver_result("T", "s", large_result, "prompt:dashboard:s1")
        slot.enqueue_or_run_prompt.assert_called_once()
        # Verify the prompt was truncated
        call_args = slot.enqueue_or_run_prompt.call_args[0]
        assert len(call_args[0].encode("utf-8")) <= 131072 + 100  # MAX_PROMPT_BYTES + overhead


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _init_task_runner approval callback
# ═══════════════════════════════════════════════════════════════════════════


class TestTaskRunnerApproval:
    """Task runner approval callback."""

    def test_task_runner_has_approval_callbacks(self):
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
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
        # Verify approval callbacks were set
        assert mock_tr_inst._on_tool_approval is not None
        assert mock_tr_inst._on_approval is not None


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _start_embeddings
# ═══════════════════════════════════════════════════════════════════════════


class TestStartEmbeddings:
    """In-process embedding wiring + background model download kick."""

    @pytest.mark.asyncio
    async def test_model_present_binds_embed_fn_immediately(self):
        orch = _make_orchestrator()
        orch.vector_memory = MagicMock(embed_fn=None, embed_fn_factory=None)
        fake_embed_fn = lambda text: [0.1]  # noqa: E731
        with (
            patch("kiro_crew.slack.gateway.model_file_present", return_value=True),
            patch(
                "kiro_crew.slack.gateway.make_sync_embed_fn", return_value=fake_embed_fn
            ) as mock_make,
            patch(
                "kiro_crew.slack.gateway.start_background_model_download", return_value=None
            ) as mock_start,
        ):
            await orch._start_embeddings()
        # Factory wired unconditionally (lazy rebind), fn bound immediately.
        assert orch.vector_memory.embed_fn_factory is mock_make
        assert orch.vector_memory.embed_fn is fake_embed_fn
        mock_start.assert_called_once_with()
        assert orch._model_download_task is None

    @pytest.mark.asyncio
    async def test_model_absent_defers_embed_fn_and_kicks_download(self):
        orch = _make_orchestrator()
        orch.vector_memory = MagicMock(embed_fn=None, embed_fn_factory=None)
        fake_task = MagicMock()
        with (
            patch("kiro_crew.slack.gateway.model_file_present", return_value=False),
            patch(
                "kiro_crew.slack.gateway.start_background_model_download", return_value=fake_task
            ) as mock_start,
        ):
            await orch._start_embeddings()
        # embed_fn stays unbound (lazy rebind picks it up once the model lands)
        # but the factory is wired and the background download task is stored.
        assert orch.vector_memory.embed_fn is None
        assert orch.vector_memory.embed_fn_factory is not None
        mock_start.assert_called_once_with()
        assert orch._model_download_task is fake_task


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _auto_migrate_memory (boot-time auto-migration + re-embed sweep)
# ═══════════════════════════════════════════════════════════════════════════


class TestAutoMigrateMemory:
    """Background auto-migration of legacy markdown + re-embed sweep at boot."""

    def _orch_with_store(self, *, migrated: bool):
        orch = _make_orchestrator()
        orch._cfg.memory.migrated = migrated
        store = MagicMock()
        store.embed_fn = None
        store.migrate_from_markdown = MagicMock(
            return_value={"semantic": 3, "episodic": 5, "skipped": 1}
        )
        store.backfill_missing_embeddings = MagicMock(return_value=0)
        store._log_event = MagicMock()
        orch.vector_memory = store
        orch.consolidator = MagicMock(_migrated=False)
        return orch, store

    @staticmethod
    def _ready_embedder():
        """A shared embedder whose model is loaded (wait_ready -> True)."""
        return MagicMock(
            wait_ready=MagicMock(return_value=True), is_ready=MagicMock(return_value=True)
        )

    @pytest.mark.asyncio
    async def test_migrates_when_not_migrated_and_legacy_present(self):
        orch, store = self._orch_with_store(migrated=False)
        set_migrated = AsyncMock()
        with (
            patch("kiro_crew.slack.gateway.model_file_present", return_value=True),
            patch("kiro_crew.slack.gateway.make_sync_embed_fn", return_value=(lambda t: [0.1])),
            patch(
                "kiro_crew.slack.gateway.get_shared_embedder", return_value=self._ready_embedder()
            ),
            patch("kiro_crew.memory.legacy_memory_present", return_value=True),
            patch.object(orch, "_set_memory_migrated", set_migrated),
        ):
            await orch._auto_migrate_memory()
        store.migrate_from_markdown.assert_called_once()
        set_migrated.assert_awaited_once_with(True)
        assert orch._cfg.memory.migrated is True
        assert orch.consolidator._migrated is True
        # Ack: audit event with counts summary.
        store._log_event.assert_called_once()
        assert store._log_event.call_args[0][0] == "migration"
        # Model present + loaded → re-embed sweep runs.
        store.backfill_missing_embeddings.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_migrate_when_already_migrated(self):
        orch, store = self._orch_with_store(migrated=True)
        set_migrated = AsyncMock()
        with (
            patch("kiro_crew.slack.gateway.model_file_present", return_value=True),
            patch("kiro_crew.slack.gateway.make_sync_embed_fn", return_value=(lambda t: [0.1])),
            patch(
                "kiro_crew.slack.gateway.get_shared_embedder", return_value=self._ready_embedder()
            ),
            patch("kiro_crew.memory.legacy_memory_present", return_value=True),
            patch.object(orch, "_set_memory_migrated", set_migrated),
        ):
            await orch._auto_migrate_memory()
        store.migrate_from_markdown.assert_not_called()
        set_migrated.assert_not_awaited()
        # Phase 2 sweep still runs (independent of the migrated flag).
        store.backfill_missing_embeddings.assert_called_once()

    @pytest.mark.asyncio
    async def test_sweep_deferred_when_model_not_ready(self):
        # GGUF present on disk but the in-memory load hasn't finished:
        # wait_ready() -> False, so the sweep is deferred (not run with a cold
        # model that would embed zero rows).
        orch, store = self._orch_with_store(migrated=True)
        not_ready = MagicMock(
            wait_ready=MagicMock(return_value=False), is_ready=MagicMock(return_value=False)
        )
        with (
            patch("kiro_crew.slack.gateway.model_file_present", return_value=True),
            patch("kiro_crew.slack.gateway.make_sync_embed_fn", return_value=(lambda t: [0.1])),
            patch("kiro_crew.slack.gateway.get_shared_embedder", return_value=not_ready),
            patch("kiro_crew.memory.legacy_memory_present", return_value=True),
            patch.object(orch, "_set_memory_migrated", AsyncMock()),
        ):
            await orch._auto_migrate_memory()
        not_ready.wait_ready.assert_called_once()
        store.backfill_missing_embeddings.assert_not_called()

    @pytest.mark.asyncio
    async def test_fresh_install_no_legacy_still_flips_migrated(self):
        orch, store = self._orch_with_store(migrated=False)
        set_migrated = AsyncMock()
        with (
            patch("kiro_crew.slack.gateway.model_file_present", return_value=True),
            patch("kiro_crew.slack.gateway.make_sync_embed_fn", return_value=(lambda t: [0.1])),
            patch("kiro_crew.memory.legacy_memory_present", return_value=False),
            patch.object(orch, "_set_memory_migrated", set_migrated),
        ):
            await orch._auto_migrate_memory()
        # No legacy → don't parse markdown, but still flip the flag + ack (0 counts).
        store.migrate_from_markdown.assert_not_called()
        set_migrated.assert_awaited_once_with(True)
        assert orch._cfg.memory.migrated is True
        store._log_event.assert_called_once()
        assert "semantic=0 episodic=0 skipped=0" in store._log_event.call_args[0][4]

    @pytest.mark.asyncio
    async def test_model_absent_awaits_download_then_sweeps(self):
        orch, store = self._orch_with_store(migrated=False)
        # Model absent at migrate time, present after the download task resolves.
        presence = iter([False, False, True, True])
        orch._model_download_task = asyncio.ensure_future(asyncio.sleep(0))
        with (
            patch(
                "kiro_crew.slack.gateway.model_file_present",
                side_effect=lambda: next(presence, True),
            ),
            patch("kiro_crew.slack.gateway.make_sync_embed_fn", return_value=(lambda t: [0.1])),
            patch(
                "kiro_crew.slack.gateway.get_shared_embedder", return_value=self._ready_embedder()
            ),
            patch("kiro_crew.memory.legacy_memory_present", return_value=True),
            patch.object(orch, "_set_memory_migrated", AsyncMock()),
        ):
            await orch._auto_migrate_memory()
        # Migrated even though the model was absent; sweep ran after the wait.
        store.migrate_from_markdown.assert_called_once()
        store.backfill_missing_embeddings.assert_called_once()

    @pytest.mark.asyncio
    async def test_migrate_error_leaves_flag_false_and_survives(self):
        orch, store = self._orch_with_store(migrated=False)
        store.migrate_from_markdown.side_effect = RuntimeError("boom")
        set_migrated = AsyncMock()
        with (
            patch("kiro_crew.slack.gateway.model_file_present", return_value=True),
            patch("kiro_crew.slack.gateway.make_sync_embed_fn", return_value=(lambda t: [0.1])),
            patch("kiro_crew.memory.legacy_memory_present", return_value=True),
            patch.object(orch, "_set_memory_migrated", set_migrated),
        ):
            # Must not raise — boot survives.
            await orch._auto_migrate_memory()
        set_migrated.assert_not_awaited()
        assert orch._cfg.memory.migrated is False

    @pytest.mark.asyncio
    async def test_no_vector_store_returns_without_raising(self):
        """A boot where ``_init_services`` never ran must not raise.

        The task is fire-and-forget, so an escaping AttributeError would only
        surface later as an unretrieved-task error, logged far from its cause.
        """
        orch = _make_orchestrator()
        orch._cfg.memory.migrated = False
        assert not hasattr(orch, "vector_memory")
        set_migrated = AsyncMock()
        with patch.object(orch, "_set_memory_migrated", set_migrated):
            await orch._auto_migrate_memory()
        set_migrated.assert_not_awaited()
        assert orch._cfg.memory.migrated is False


class _LoadRecordingEmbedder:
    """A backend that records whether the model load was kicked.

    Mirrors ``LlamaCppEmbedder``: ``wait_ready()`` kicks the background load (the
    ~700MB GGUF mmap plus its KV/compute buffers) before joining the loader
    thread, so a call to ``wait_ready`` IS the cost this sweep must avoid paying
    on a boot with nothing to embed. ``model_id``/``dim`` are set at construction
    and readable without a load, which is what lets the staleness probe run
    ahead of it.
    """

    def __init__(self, *, model_id: str = "qwen3-embedding:0.6b", dim: int = 1024) -> None:
        self.model_id = model_id
        self.dim = dim
        self.load_kicks = 0
        self.wait_ready_calls = 0

    def _kick_background_load(self) -> None:
        self.load_kicks += 1

    def wait_ready(self, timeout: float | None = None) -> bool:
        self.wait_ready_calls += 1
        self._kick_background_load()
        return True

    def is_ready(self) -> bool:
        return self.load_kicks > 0


class TestReembedSweepDefersTheModelLoad:
    """The sweep must probe with SQL before it loads a ~700MB embedding model.

    ``wait_ready()`` is not a free question: it kicks the GGUF load, costing
    ~1GB RSS for the process's lifetime (measured: VmRSS +1069 MiB — RssAnon
    +455 MiB private buffers, RssFile +614 MiB mmap'd weights). Steady state has
    nothing to embed, so a boot that loads the model to discover that is pure
    waste. These tests pin the load itself, not a proxy for it.
    """

    def _orch(self, *, pending: bool):
        orch = _make_orchestrator()
        orch._cfg.memory.migrated = True  # phase 1 already done
        store = MagicMock()
        store.embed_fn = None
        store.has_pending_embeddings = MagicMock(return_value=pending)
        store.backfill_missing_embeddings = MagicMock(return_value=0)
        orch.vector_memory = store
        orch.consolidator = None
        return orch, store

    @pytest.mark.asyncio
    async def test_a_boot_with_no_work_never_loads_the_model(self):
        orch, store = self._orch(pending=False)
        embedder = _LoadRecordingEmbedder()
        with (
            patch.object(gw, "get_shared_embedder", return_value=embedder),
            patch.object(gw, "model_file_present", return_value=True),
            patch.object(gw, "make_sync_embed_fn", return_value=lambda s: [0.0]),
            patch.object(gw, "store_embedding_space_is_stale", return_value=False),
            patch.object(gw, "reconcile_store_embedding_space") as reconcile,
        ):
            await orch._auto_migrate_memory()
        store.has_pending_embeddings.assert_called_once_with()
        assert embedder.load_kicks == 0, "the model must not be loaded for a no-op sweep"
        assert embedder.wait_ready_calls == 0
        store.backfill_missing_embeddings.assert_not_called()
        # No destructive reconcile either: nothing was cleared, so nothing needs
        # re-embedding, and the store's recorded space already matches.
        reconcile.assert_not_called()

    @pytest.mark.asyncio
    async def test_pending_rows_do_load_the_model_and_sweep(self):
        orch, store = self._orch(pending=True)
        embedder = _LoadRecordingEmbedder()
        stale = MagicMock(return_value=False)
        with (
            patch.object(gw, "get_shared_embedder", return_value=embedder),
            patch.object(gw, "model_file_present", return_value=True),
            patch.object(gw, "make_sync_embed_fn", return_value=lambda s: [0.0]),
            patch.object(gw, "store_embedding_space_is_stale", stale),
            patch.object(gw, "reconcile_store_embedding_space") as reconcile,
        ):
            await orch._auto_migrate_memory()
        assert embedder.wait_ready_calls == 1
        assert embedder.load_kicks == 1, "pending rows must still load the model"
        reconcile.assert_called_once_with(store)
        store.backfill_missing_embeddings.assert_called_once()
        # Short-circuit: pending work needs no staleness question.
        stale.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_stale_vector_space_loads_the_model_with_no_pending_rows(self):
        """A model swap leaves every row embedded — and every vector wrong.

        ``has_pending_embeddings()`` is False here (no NULL vectors yet), so
        without the staleness arm the store would never reconcile and search
        would keep scoring old-space vectors against new-space queries.
        """
        orch, store = self._orch(pending=False)
        embedder = _LoadRecordingEmbedder()
        with (
            patch.object(gw, "get_shared_embedder", return_value=embedder),
            patch.object(gw, "model_file_present", return_value=True),
            patch.object(gw, "make_sync_embed_fn", return_value=lambda s: [0.0]),
            patch.object(gw, "store_embedding_space_is_stale", return_value=True),
            patch.object(gw, "reconcile_store_embedding_space") as reconcile,
        ):
            await orch._auto_migrate_memory()
        assert embedder.load_kicks == 1, "a stale space must still load and re-embed"
        reconcile.assert_called_once_with(store)
        store.backfill_missing_embeddings.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_store_without_the_probe_keeps_its_sweep(self):
        """A foreign/stub store must not silently lose the sweep.

        The probe is an optimisation; its absence has to fail toward doing the
        work, not toward skipping it forever.
        """
        orch = _make_orchestrator()
        orch._cfg.memory.migrated = True
        store = MagicMock(spec=["embed_fn", "backfill_missing_embeddings"])
        store.embed_fn = None
        store.backfill_missing_embeddings = MagicMock(return_value=0)
        orch.vector_memory = store
        orch.consolidator = None
        embedder = _LoadRecordingEmbedder()
        with (
            patch.object(gw, "get_shared_embedder", return_value=embedder),
            patch.object(gw, "model_file_present", return_value=True),
            patch.object(gw, "make_sync_embed_fn", return_value=lambda s: [0.0]),
            patch.object(gw, "reconcile_store_embedding_space"),
        ):
            await orch._auto_migrate_memory()
        assert not hasattr(store, "has_pending_embeddings")
        assert embedder.load_kicks == 1
        store.backfill_missing_embeddings.assert_called_once()

    def test_binding_embed_fn_does_not_load_the_model(self):
        """The lazy path this fix relies on: binding is not loading.

        ``_start_embeddings`` binds ``embed_fn``/``embed_fn_factory`` at boot. If
        that bind loaded the model, deferring the sweep's load would buy nothing.
        ``make_sync_embed_fn`` returns a closure and the load is kicked inside
        ``embed_batch`` only when it finds no resident model.
        """
        import inspect

        from kiro_crew import embeddings as emb

        src = inspect.getsource(emb.make_sync_embed_fn)
        assert "_kick_background_load" not in src
        assert "wait_ready" not in src
        # The kick lives on the embed path instead.
        assert "_kick_background_load()" in inspect.getsource(emb.LlamaCppEmbedder.embed_batch)
        # And wait_ready() is what makes the sweep's question expensive.
        assert "_kick_background_load()" in inspect.getsource(emb.LlamaCppEmbedder.wait_ready)


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _auto_apply_update discards local edits before staging frontend
# ═══════════════════════════════════════════════════════════════════════════


class TestAutoApplyUpdateResetPath:
    """Public auto-update reset path: discards local edits, builds frontend, pips."""

    @pytest.fixture(autouse=True)
    def _permit_update_preconditions(self):
        """Neutralize the two seam preconditions so these tests keep their subject.

        `_auto_apply_update` refuses outright when the checkout declares a
        repo-named git driver, when the branch does not track the remote it resets
        to, or when a tracked edit is hidden by assume-unchanged. All read the REAL
        git metadata of ``KIROCREW_PROJECT_DIR``, which these tests point at a path
        that is not a repo — so without this they would all pass vacuously by
        refusing before reaching the fetch/reset sequence they exist to cover. The
        refusals have their own tests in ``TestAutoApplyUpdatePreconditions`` and
        ``TestAutoApplyUpdateResetPath``.
        """
        with (
            patch("kiro_crew.slack.gateway.hidden_worktree_edits", return_value=[]),
            patch(
                "kiro_crew.slack.gateway.repo_exec_config_reason",
                return_value="",
            ),
            patch("kiro_crew.slack.gateway.tracks_upstream", return_value=True),
            patch("kiro_crew.slack.gateway.commits_ahead", return_value=0),
        ):
            yield

    @pytest.mark.asyncio
    async def test_reset_then_frontend_then_pip(self):
        """A CLEAN checkout resets, then frontend build + pip install run.

        Public OSS flow (no Brazil ws sync / toolbox / AIM): branch → fetch →
        diff → status → reset → [kiro-cli optional] → build frontend → pip.

        This test used to pass ` M file.py` here and assert that the reset ran
        anyway -- it encoded the warn-and-destroy behaviour that
        `test_uncommitted_tracked_changes_refuse_the_reset` now forbids. The
        happy path is a clean tree; the dirty tree is a refusal, not a variant
        of this flow.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        # Clean tracked tree: nothing for the reset to discard.
        _fake_exec = _git_exec_fake(status_out=b"")

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                with patch("kiro_crew.dep_sync.sync_or_reinstall", return_value=0):
                    with patch(
                        "kiro_crew.slack.gateway.build_frontend_async", new_callable=AsyncMock
                    ) as mock_build:
                        with patch("os.execv", side_effect=OSError("test")):
                            with patch("shutil.which", return_value=None):
                                await orch._auto_apply_update()

        # Frontend build+stage runs, and the package is reinstalled.
        mock_build.assert_awaited()
        ds.push_update_progress.assert_any_call("building", "Building frontend…")
        ds.push_update_progress.assert_any_call("building", "Rebuilding package…")

    @pytest.mark.asyncio
    async def test_uncommitted_tracked_changes_refuse_the_reset(self):
        """An unattended update must not delete a developer's uncommitted work.

        This check used to log a warning and reset anyway, which made the
        boot-time path the one place that could silently destroy uncommitted
        edits. It now refuses, like the committed-work and exec-config checks
        immediately above it, and defers to `kirocrew update` -- where a human
        chose the destructive semantics.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        spawned: list[tuple] = []
        _fake_exec = _git_exec_fake(status_out=b" M file.py\n", record=spawned)

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                with patch(
                    "kiro_crew.slack.gateway.build_frontend_async", new_callable=AsyncMock
                ) as mock_build:
                    with patch("os.execv") as mock_execv:
                        with patch("shutil.which", return_value=None):
                            await orch._auto_apply_update()

        # The destructive step never ran.
        assert not any("reset" in [str(a) for a in args] for args in spawned), spawned
        # And nothing downstream of it ran either.
        mock_build.assert_not_awaited()
        mock_execv.assert_not_called()
        ds.push_refresh.assert_any_call("update_available")

    @pytest.mark.asyncio
    async def test_untracked_files_alone_do_not_refuse(self):
        """`reset --hard` preserves untracked files, so they are not a reason to stop.

        Task specs and notes live untracked in a checkout. Refusing on them would
        disable auto-update for essentially every real install, which is the same
        silent no-op this PR exists to remove.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        spawned: list[tuple] = []
        _fake_exec = _git_exec_fake(status_out=b"?? notes.md\n", record=spawned)

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                with patch("kiro_crew.dep_sync.sync_or_reinstall", return_value=0):
                    with patch(
                        "kiro_crew.slack.gateway.build_frontend_async", new_callable=AsyncMock
                    ):
                        with patch("os.execv", side_effect=OSError("test")):
                            with patch("shutil.which", return_value=None):
                                await orch._auto_apply_update()

        assert any("reset" in [str(a) for a in args] for args in spawned), spawned

    @pytest.mark.asyncio
    async def test_an_unresolvable_git_refuses_the_whole_update(self):
        """With no trustworthy git, the unattended path does nothing at all.

        Not even the branch probe: the first spawn would already be the planted
        shim, and it is the one that decides every later step.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        spawned: list[tuple] = []
        _fake_exec = _git_exec_fake(record=spawned)

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                with patch(
                    "kiro_crew.slack.gateway.platform_compat.trusted_git_bin",
                    return_value=None,
                ):
                    with patch(
                        "kiro_crew.slack.gateway.build_frontend_async",
                        new_callable=AsyncMock,
                    ) as mock_build:
                        with patch("os.execv") as mock_execv:
                            with patch("shutil.which", return_value=None):
                                await orch._auto_apply_update()

        assert spawned == [], spawned
        mock_build.assert_not_awaited()
        mock_execv.assert_not_called()

    @pytest.mark.asyncio
    async def test_every_git_step_runs_the_resolved_binary(self):
        """One resolution, used by every step -- no bare `git` anywhere.

        Resolved once rather than per call so the whole sequence runs the same
        binary; re-resolving per spawn would leave a window for the answer to
        change mid-update.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        spawned: list[tuple] = []
        _fake_exec = _git_exec_fake(record=spawned)

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                with patch(
                    "kiro_crew.slack.gateway.platform_compat.trusted_git_bin",
                    return_value="/trusted/bin/git",
                ):
                    with patch("kiro_crew.dep_sync.sync_or_reinstall", return_value=0):
                        with patch(
                            "kiro_crew.slack.gateway.build_frontend_async",
                            new_callable=AsyncMock,
                        ):
                            with patch("os.execv", side_effect=OSError("test")):
                                with patch("shutil.which", return_value=None):
                                    await orch._auto_apply_update()

        git_calls = [
            [str(a) for a in args] for args in spawned if args and str(args[0]).endswith("git")
        ]
        assert git_calls, spawned
        for argv in git_calls:
            assert argv[0] == "/trusted/bin/git", argv

    @pytest.mark.asyncio
    async def test_a_hidden_tracked_edit_refuses(self):
        """An assume-unchanged edit must stop the unattended reset.

        `status --porcelain` reports a clean tree for it, so check 3 passes and the
        reset would silently overwrite the developer's edit.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        spawned: list[tuple] = []
        _fake_exec = _git_exec_fake(record=spawned)

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                with patch(
                    "kiro_crew.slack.gateway.hidden_worktree_edits",
                    return_value=["config/local.py"],
                ):
                    with patch("os.execv") as mock_execv:
                        with patch("shutil.which", return_value=None):
                            await orch._auto_apply_update()

        assert not any("reset" in [str(a) for a in args] for args in spawned), spawned
        mock_execv.assert_not_called()
        ds.push_refresh.assert_any_call("update_available")

    @pytest.mark.asyncio
    async def test_an_unknown_hidden_edit_state_refuses(self):
        """`None` cannot prove the tree is safe, so it fails closed.

        Same rule as an unknown ahead-count and an unreadable status.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        spawned: list[tuple] = []
        _fake_exec = _git_exec_fake(record=spawned)

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                with patch("kiro_crew.slack.gateway.hidden_worktree_edits", return_value=None):
                    with patch("os.execv") as mock_execv:
                        with patch("shutil.which", return_value=None):
                            await orch._auto_apply_update()

        assert not any("reset" in [str(a) for a in args] for args in spawned), spawned
        mock_execv.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_untracked_collision_refuses(self, tmp_path):
        """`reset --hard` OVERWRITES an untracked file the target adds.

        This is the data-loss case that survives a clean tracked tree:
        `git status --porcelain` reports the local file as `??`, and the
        tracked-change refusal deliberately skips those -- so without this check
        the reset silently replaces it. Verified against real git while
        developing the fix.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        # The path the target would add EXISTS locally (untracked).
        (tmp_path / "newfile.txt").write_text("MY PRECIOUS UNTRACKED WORK\n")

        spawned: list[tuple] = []
        _fake_exec = _git_exec_fake(added_out=b"newfile.txt\0", record=spawned)

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": str(tmp_path)}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                with patch(
                    "kiro_crew.slack.gateway.build_frontend_async", new_callable=AsyncMock
                ) as mock_build:
                    with patch("os.execv") as mock_execv:
                        with patch("shutil.which", return_value=None):
                            await orch._auto_apply_update()

        assert not any("reset" in [str(a) for a in args] for args in spawned), spawned
        mock_build.assert_not_awaited()
        mock_execv.assert_not_called()
        ds.push_refresh.assert_any_call("update_available")
        # The file is still the developer's.
        assert (tmp_path / "newfile.txt").read_text() == "MY PRECIOUS UNTRACKED WORK\n"

    @pytest.mark.asyncio
    async def test_an_obstructing_untracked_ancestor_refuses(self, tmp_path):
        """Target adds `pkg/mod.py`; locally `pkg` is an untracked FILE.

        git must replace that file with a directory, destroying it -- but
        `lexists("pkg/mod.py")` is False, so checking only the full path misses
        it. Verified against real git while developing the fix.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        (tmp_path / "pkg").write_text("MY PRECIOUS NOTES\n")

        spawned: list[tuple] = []
        _fake_exec = _git_exec_fake(added_out=b"pkg/mod.py\0", record=spawned)

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": str(tmp_path)}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                with patch("os.execv") as mock_execv:
                    with patch("shutil.which", return_value=None):
                        await orch._auto_apply_update()

        assert not any("reset" in [str(a) for a in args] for args in spawned), spawned
        mock_execv.assert_not_called()
        assert (tmp_path / "pkg").read_text() == "MY PRECIOUS NOTES\n"

    @pytest.mark.asyncio
    async def test_a_symlinked_directory_ancestor_refuses(self, tmp_path):
        """`isdir` follows a symlink, so the guard called it a plain directory.

        Verified against real git: target adds `pkg/mod.py`, local untracked `pkg`
        is a symlink to a directory, and the reset REPLACED the symlink with a real
        directory -- destroying the developer's deliberate structure while the
        collision guard reported nothing.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        real = tmp_path / "elsewhere"
        real.mkdir()
        link = tmp_path / "pkg"
        try:
            link.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("this platform cannot create directory symlinks here")

        spawned: list[tuple] = []
        _fake_exec = _git_exec_fake(added_out=b"pkg/mod.py\0", record=spawned)

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": str(tmp_path)}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                with patch("os.execv") as mock_execv:
                    with patch("shutil.which", return_value=None):
                        await orch._auto_apply_update()

        assert not any("reset" in [str(a) for a in args] for args in spawned), spawned
        mock_execv.assert_not_called()
        # The developer's symlink is still a symlink.
        assert link.is_symlink()

    @pytest.mark.asyncio
    async def test_the_collision_scan_does_not_run_on_the_event_loop(self, tmp_path):
        """`no-blocking-call-on-event-loop`: the ancestor walk is offloaded.

        `_obstructions` stats every added path AND each of its ancestors, so a
        large update would run an unbounded stat walk on the loop thread and stall
        every chat and the heartbeat.

        Asserts the THREAD the scan's own probes run on, not which executor object
        some offload was handed: the first version of this test asserted the
        latter and passed even with the scan inline, because another offload in
        the same function already used that executor. Thread identity is the
        property the rule is actually about.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        loop_thread = threading.current_thread()
        probe_threads: list[threading.Thread] = []
        real_lexists = os.path.lexists

        def recording_lexists(path):
            if "scanned-target.py" in str(path):
                probe_threads.append(threading.current_thread())
            return real_lexists(path)

        _fake_exec = _git_exec_fake(added_out=b"deep/nested/scanned-target.py\0")

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": str(tmp_path)}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                with patch("os.path.lexists", side_effect=recording_lexists):
                    with patch("kiro_crew.dep_sync.sync_or_reinstall", return_value=0):
                        with patch(
                            "kiro_crew.slack.gateway.build_frontend_async",
                            new_callable=AsyncMock,
                        ):
                            with patch("os.execv", side_effect=OSError("test")):
                                with patch("shutil.which", return_value=None):
                                    await orch._auto_apply_update()

        assert probe_threads, "the collision scan never probed the added path"
        assert all(
            t is not loop_thread for t in probe_threads
        ), f"scan ran on the event-loop thread: {[t.name for t in probe_threads]}"

    @pytest.mark.asyncio
    async def test_a_junction_ancestor_refuses(self, tmp_path):
        """A Windows junction must be treated as a link, not a directory.

        `os.path.islink` returns False for a junction, so the round-20 check would
        read one as a plain directory and let the reset write THROUGH it, outside
        the checkout. `AGENTS.md` names `is_link_or_junction` as the required form
        for exactly this. Asserted by faking the junction verdict, since a real
        junction cannot be created on POSIX.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        (tmp_path / "pkg").mkdir()  # a plain dir to islink, a junction to the helper

        spawned: list[tuple] = []
        _fake_exec = _git_exec_fake(added_out=b"pkg/mod.py\0", record=spawned)

        def fake_link_or_junction(path):
            return str(path).endswith("pkg")

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": str(tmp_path)}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                with patch(
                    "kiro_crew.slack.gateway.platform_compat.is_link_or_junction",
                    side_effect=fake_link_or_junction,
                ):
                    with patch("os.execv") as mock_execv:
                        with patch("shutil.which", return_value=None):
                            await orch._auto_apply_update()

        assert not any("reset" in [str(a) for a in args] for args in spawned), spawned
        mock_execv.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_directory_ancestor_is_not_an_obstruction(self, tmp_path):
        """An existing DIRECTORY ancestor is normal and must not refuse.

        Every update that adds a file into an existing package would otherwise
        stop -- the over-refusal that would disable auto-update in practice.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        (tmp_path / "pkg").mkdir()

        spawned: list[tuple] = []
        _fake_exec = _git_exec_fake(added_out=b"pkg/mod.py\0", record=spawned)

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": str(tmp_path)}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                with patch("kiro_crew.dep_sync.sync_or_reinstall", return_value=0):
                    with patch(
                        "kiro_crew.slack.gateway.build_frontend_async", new_callable=AsyncMock
                    ):
                        with patch("os.execv", side_effect=OSError("test")):
                            with patch("shutil.which", return_value=None):
                                await orch._auto_apply_update()

        assert any("reset" in [str(a) for a in args] for args in spawned), spawned

    @pytest.mark.asyncio
    async def test_a_non_utf8_added_path_is_still_matched(self, tmp_path):
        """A path byte that is not valid UTF-8 must not decode into a miss.

        Under `errors="replace"` the name becomes `bad\ufffdname.txt`, which does
        not exist on disk, so the guard passes while the reset overwrites the real
        file. `os.fsdecode` round-trips it.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        raw = b"bad\xffname.txt"
        try:
            (tmp_path / os.fsdecode(raw)).write_bytes(b"MY PRECIOUS\n")
        except (OSError, UnicodeError):
            pytest.skip("this filesystem rejects non-UTF-8 names")

        spawned: list[tuple] = []
        _fake_exec = _git_exec_fake(added_out=raw + b"\0", record=spawned)

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": str(tmp_path)}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                with patch("os.execv") as mock_execv:
                    with patch("shutil.which", return_value=None):
                        await orch._auto_apply_update()

        assert not any("reset" in [str(a) for a in args] for args in spawned), spawned
        mock_execv.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_collision_refusal_logs_an_encodable_record(self, tmp_path, caplog):
        """Reproduces the CI shard crash without needing xdist.

        The collision refusal is the one log line carrying a filename straight
        from git output. When that name is not valid UTF-8, an unsanitized record
        raises inside the handler -- `logging` drops it, and `pytest-xdist`, which
        serializes reports as UTF-8, dies with `DumpError` and takes the WHOLE
        shard with it. That is what happened: one test killed shard 4 on both
        Python versions while passing locally, because the local run disabled
        `-n auto`.

        Asserting encodability here is what makes the guard durable -- reverting
        the sanitizer fails this test in a plain single-process run.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        raw = b"bad\xffname.txt"
        # `os.fsdecode` itself RAISES on Windows (UTF-8 + surrogatepass cannot
        # decode an invalid start byte), so it belongs inside the guard: the
        # hazard does not exist on a platform whose filenames are UTF-16, and this
        # must SKIP there rather than error.
        try:
            name = os.fsdecode(raw)
            name.encode("utf-8")
        except UnicodeDecodeError:
            pytest.skip("this platform cannot represent a non-UTF-8 name")
        except UnicodeEncodeError:
            pass
        else:
            pytest.skip("this platform's fsdecode produced an encodable name")
        try:
            (tmp_path / name).write_bytes(b"MY PRECIOUS\n")
        except (OSError, UnicodeError):
            pytest.skip("this filesystem rejects non-UTF-8 names")

        _fake_exec = _git_exec_fake(added_out=raw + b"\0")

        with caplog.at_level(logging.WARNING):
            with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": str(tmp_path)}):
                with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                    with patch("os.execv"):
                        with patch("shutil.which", return_value=None):
                            await orch._auto_apply_update()

        # The refusal must have been recorded...
        refusals = [r for r in caplog.records if "already" in r.getMessage()]
        assert refusals, [r.getMessage() for r in caplog.records]
        # ...and EVERY record must survive a UTF-8 log sink / xdist report.
        for record in caplog.records:
            record.getMessage().encode("utf-8")

    @pytest.mark.asyncio
    async def test_the_added_paths_query_disables_rename_detection(self, tmp_path):
        """Without `--no-renames` the collision guard is silently bypassable.

        Rename detection is on by default for porcelain diffs, so a pure `git mv`
        upstream is ONE `R` entry -- and `--diff-filter=A` excludes it, leaving the
        destination path absent from the added list while the reset still
        overwrites an untracked local file there.
        `test_governance_updates` proves that against real git; this pins the flag
        so the argv cannot regress.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        spawned: list[tuple] = []
        _fake_exec = _git_exec_fake(record=spawned)

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": str(tmp_path)}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                with patch("kiro_crew.dep_sync.sync_or_reinstall", return_value=0):
                    with patch(
                        "kiro_crew.slack.gateway.build_frontend_async", new_callable=AsyncMock
                    ):
                        with patch("os.execv", side_effect=OSError("test")):
                            with patch("shutil.which", return_value=None):
                                await orch._auto_apply_update()

        added = [
            [str(a) for a in args]
            for args in spawned
            if any(str(a).startswith("--diff-filter") for a in args)
        ]
        assert added, spawned
        for argv in added:
            assert "--no-renames" in argv, argv

    @pytest.mark.asyncio
    async def test_added_paths_that_do_not_exist_locally_do_not_refuse(self, tmp_path):
        """Most updates add files. Only a COLLISION is a reason to stop.

        Refusing whenever the target adds anything would disable auto-update for
        ordinary upstream commits -- the silent no-op this PR exists to remove,
        reintroduced by an over-broad guard.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        spawned: list[tuple] = []
        # Target adds two paths; neither exists in tmp_path.
        _fake_exec = _git_exec_fake(added_out=b"a/new.py\0b/other.py\0", record=spawned)

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": str(tmp_path)}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                with patch("kiro_crew.dep_sync.sync_or_reinstall", return_value=0):
                    with patch(
                        "kiro_crew.slack.gateway.build_frontend_async", new_callable=AsyncMock
                    ):
                        with patch("os.execv", side_effect=OSError("test")):
                            with patch("shutil.which", return_value=None):
                                await orch._auto_apply_update()

        assert any("reset" in [str(a) for a in args] for args in spawned), spawned

    @pytest.mark.asyncio
    async def test_an_unlistable_added_set_refuses(self, tmp_path):
        """If the added-path list cannot be read, safety cannot be proven.

        Fails closed, like the unreadable-status and unknown-ahead-count cases.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        spawned: list[tuple] = []
        _fake_exec = _git_exec_fake(added_rc=1, record=spawned)

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": str(tmp_path)}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                with patch("kiro_crew.slack.gateway.build_frontend_async", new_callable=AsyncMock):
                    with patch("os.execv") as mock_execv:
                        with patch("shutil.which", return_value=None):
                            await orch._auto_apply_update()

        assert not any("reset" in [str(a) for a in args] for args in spawned), spawned
        mock_execv.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_unreadable_status_refuses(self):
        """An unreadable work-tree status cannot prove the tree is clean.

        The next step is irreversible, so an unanswerable question is treated as
        the unsafe answer -- the same fail-closed rule the ahead-count uses when
        `commits_ahead` returns None.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        spawned: list[tuple] = []
        _fake_exec = _git_exec_fake(status_rc=1, record=spawned)

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                with patch("kiro_crew.slack.gateway.build_frontend_async", new_callable=AsyncMock):
                    with patch("os.execv") as mock_execv:
                        with patch("shutil.which", return_value=None):
                            await orch._auto_apply_update()

        assert not any("reset" in [str(a) for a in args] for args in spawned), spawned
        mock_execv.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_refusal_skips_the_core_dep_repair_entirely(self):
        """A REFUSED sync must not be followed by a repair into the same venv.

        REFUSED means the sync stopped before touching anything — most
        importantly when the venv serves a DIFFERENT checkout. The core-dep
        repair writes into exactly that venv, so running it after a refusal
        would perform the mutation the guard exists to prevent, and the restart
        would then bring up the wrong checkout with changed dependencies.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        spawned: list[tuple] = []
        _fake_exec = _git_exec_fake(record=spawned)

        from kiro_crew import dep_sync

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                with patch("kiro_crew.dep_sync.sync_or_reinstall", return_value=dep_sync.REFUSED):
                    with patch(
                        "kiro_crew.slack.gateway.build_frontend_async",
                        new_callable=AsyncMock,
                    ):
                        with patch("os.execv") as mock_execv:
                            with patch("shutil.which", return_value=None):
                                await orch._auto_apply_update()

        # No pip spawn at all: the repair is what would have written to the venv.
        assert not any("pip" in [str(a) for a in args] for args in spawned), spawned
        mock_execv.assert_not_called()
        steps = [c.args[0] for c in ds.push_update_progress.call_args_list]
        assert "restarting" not in steps

    @pytest.mark.asyncio
    async def test_reset_target_is_resolved_through_the_full_remote_ref(self):
        """The target capture must spell `refs/remotes/origin/<branch>`.

        The short `origin/<branch>` is ambiguous in the attacker's favour:
        rev-parse's disambiguation order checks `refs/tags/<name>` BEFORE
        `refs/remotes/<name>`, so a tag literally named `origin/main` resolves
        instead of the remote-tracking branch. The update's own fetch auto-follows
        tags, so publishing that tag upstream creates it locally. git writes
        "refname is ambiguous" to stderr and still prints the TAG's OID on stdout,
        which is the stream this capture reads -- so the short form does not fail
        loudly, it resets to attacker code.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        spawned: list[tuple] = []
        _fake_exec = _git_exec_fake(branch=b"main\n", record=spawned)

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                with patch("kiro_crew.slack.gateway.build_frontend_async", new_callable=AsyncMock):
                    with patch("os.execv"):
                        with patch("shutil.which", return_value=None):
                            await orch._auto_apply_update()

        revparses = [
            [str(a) for a in args]
            for args in spawned
            if "rev-parse" in [str(a) for a in args]
            and "--abbrev-ref" not in [str(a) for a in args]
        ]
        assert revparses, spawned
        assert any("refs/remotes/origin/main^{commit}" in argv for argv in revparses), revparses
        # The bare form must not be what git is asked to resolve.
        assert not any("origin/main^{commit}" in argv for argv in revparses), revparses

    @pytest.mark.asyncio
    async def test_no_restart_after_any_unclean_sync_even_when_the_repair_works(self):
        """A nonzero sync never restarts, even when the core-dep repair succeeds.

        Every nonzero result names something the restart cannot fix on its own.
        Dependencies may still be unsatisfied — the repair covers only the CORE
        deps, not whatever the revision actually added. Or the revision repointed
        the console script, which no dependency install rewrites: this restart uses
        `-m kiro_crew` and would survive it, but the next restart through the
        service manager runs `kirocrew` and would not. Staying up on
        already-imported modules tells the operator now instead of then.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        # diff --quiet reports changes; everything else, INCLUDING the
        # core-dep repair, succeeds.
        _fake_exec = _git_exec_fake()

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                # rc=1, not REFUSED: an install that ran and came back unclean.
                with patch("kiro_crew.dep_sync.sync_or_reinstall", return_value=1):
                    with patch(
                        "kiro_crew.slack.gateway.build_frontend_async",
                        new_callable=AsyncMock,
                    ):
                        with patch("os.execv") as mock_execv:
                            with patch("shutil.which", return_value=None):
                                await orch._auto_apply_update()

        mock_execv.assert_not_called()
        orch.sessions.close_all.assert_not_called()
        steps = [c.args[0] for c in ds.push_update_progress.call_args_list]
        assert "error" in steps
        assert "restarting" not in steps

    @pytest.mark.asyncio
    async def test_a_failed_install_with_a_failed_repair_does_not_restart(self):
        """Restarting into unsatisfied dependencies would kill the gateway.

        The reset already moved the tree to the new revision. If neither the
        dependency install nor the core-dep repair succeeded, the process this
        restart brings up is a revision whose dependencies are known to be
        missing — it dies at import. Staying up on already-imported modules
        leaves the operator a working gateway to finish the install from.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        # Every git step succeeds; the dependency install AND the core-dep
        # repair both fail, which is the condition this test is about.
        _fake_exec = _git_exec_fake(other_rc=1)

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                with patch("kiro_crew.dep_sync.sync_or_reinstall", return_value=1):
                    with patch(
                        "kiro_crew.slack.gateway.build_frontend_async",
                        new_callable=AsyncMock,
                    ):
                        with patch("os.execv") as mock_execv:
                            with patch("shutil.which", return_value=None):
                                await orch._auto_apply_update()

        mock_execv.assert_not_called()
        orch.sessions.close_all.assert_not_called()
        steps = [c.args[0] for c in ds.push_update_progress.call_args_list]
        assert "error" in steps
        assert "restarting" not in steps


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
# Tests: Cron ACP retry path
# ═══════════════════════════════════════════════════════════════════════════


class TestCronAcpRetry:
    """Cron ACP process death retry."""

    @pytest.mark.asyncio
    async def test_acp_retry_on_process_death(self):
        """ACP error with 'not running' triggers retry."""
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.build_message = MagicMock(return_value=("msg", None))
        orch.ctx_builder.hooks = MagicMock()
        orch.subagent_mgr = MagicMock()
        orch.subagent_mgr.running = []
        orch.dashboard_state = _mock_dashboard_state()
        orch.slack = None

        with patch("kiro_crew.slack.gateway.CronService") as mock_cs:
            mock_cs_inst = MagicMock()
            mock_cs_inst.start = AsyncMock()
            mock_cs_inst.start_reaper = MagicMock()
            mock_cs_inst.register_active_session_key = MagicMock()
            mock_cs_inst.clear_active_session_key = MagicMock()
            mock_cs.return_value = mock_cs_inst
            mock_cs.create = AsyncMock(return_value=mock_cs_inst)
            await orch._init_cron()

        callback = mock_cs.create.call_args[1]["on_job"]

        job = MagicMock()
        job.script = ""
        job.command = ""
        job.id = "jacp"
        job.name = "acp-retry"
        job.persistent_session = True
        job.agent_sequence = []
        job.agent_id = None
        job.channel = ""
        job.created_by = ""
        job.approval_mode = "auto"
        job.env = None
        job.acked_items = []
        job.silent = False
        job.thread_ts = None
        job.last_posted_hash = ""
        job.consecutive_dupes = 0
        job.last_posted_at = 0.0
        job.last_failure_hash = ""
        job.last_failure_at = 0.0
        job.consecutive_failures = 0
        job._acp_retried = False

        from kiro_crew.acp.client import AcpError

        call_count = [0]

        async def _fake_stream(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise AcpError("process not running")
            return "retry success"

        with patch("kiro_crew.slack.gateway.stream_and_collect", side_effect=_fake_stream):
            with patch(
                "kiro_crew.slack.gateway.build_cron_session_context",
                return_value=("cron:jacp", "run"),
            ):
                result = await callback(job)

        assert result == "retry success"


# ═══════════════════════════════════════════════════════════════════════════
# Tests: Subagent _inject_with_retry paths
# ═══════════════════════════════════════════════════════════════════════════


class TestInjectWithRetry:
    """_inject_with_retry error handling."""

    def _setup(self):
        orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.hooks = MagicMock()
        orch.ctx_builder.build_message = MagicMock(return_value=("msg", None))
        orch.dashboard_state = _mock_dashboard_state()
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value="D_U1")
        orch.slack.post_message = AsyncMock()
        orch.slack.post_blocks = AsyncMock(return_value="ts")
        with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False):
            with patch("kiro_crew.slack.gateway.SubagentManager") as mock_sm:
                mock_sm_inst = MagicMock()
                mock_sm_inst.start_reaper = MagicMock()
                mock_sm_inst.running = []
                mock_sm_inst.queued_count_for = MagicMock(return_value=0)
                mock_sm_inst.has_pending_work_for = MagicMock(return_value=False)
                mock_sm_inst.running_agents_for = MagicMock(return_value=[])
                mock_sm_inst.get = MagicMock(return_value=None)
                mock_sm_inst.notify_injection_failed = MagicMock()
                mock_sm.return_value = mock_sm_inst
                orch._init_subagents()
        return orch, mock_sm

    @pytest.mark.asyncio
    async def test_acp_process_died_during_injection(self):
        """AcpProcessDied during injection → resets session."""
        from kiro_crew.acp.client import AcpProcessDied

        orch, mock_sm = self._setup()
        on_done = mock_sm.call_args[1]["on_done"]

        info = MagicMock()
        info.id = "agent-died"
        info.parent_session_key = "C123:ts.1"
        info.error = None
        info.result = "result"
        info.result_path = ""
        info.task = "task"
        info.agent = ""
        info.silent = False
        info.elapsed = 1.0
        info.started = 0.0

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            side_effect=AcpProcessDied("dead"),
        ):
            await on_done(info)

        orch.subagent_mgr.notify_injection_failed.assert_called()

    @pytest.mark.asyncio
    async def test_prompt_busy_exhausted(self):
        """PromptBusyExhaustedError → resets session."""
        from kiro_crew.llm_helpers import PromptBusyExhaustedError

        orch, mock_sm = self._setup()
        on_done = mock_sm.call_args[1]["on_done"]

        info = MagicMock()
        info.id = "agent-busy"
        info.parent_session_key = "C123:ts.2"
        info.error = None
        info.result = "result"
        info.result_path = ""
        info.task = "task"
        info.agent = ""
        info.silent = False
        info.elapsed = 1.0
        info.started = 0.0

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            side_effect=PromptBusyExhaustedError("exhausted"),
        ):
            await on_done(info)

        orch.subagent_mgr.notify_injection_failed.assert_called()


# ═══════════════════════════════════════════════════════════════════════════
# Tests: Orchestration guard in _subagent_done
# ═══════════════════════════════════════════════════════════════════════════


class TestOrchestrationGuard:
    """Orchestration tracker in _subagent_done."""

    def _setup(self):
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.hooks = MagicMock()
        orch.ctx_builder.build_message = MagicMock(return_value=("msg", None))
        orch.dashboard_state = _mock_dashboard_state()
        with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False):
            with patch("kiro_crew.slack.gateway.SubagentManager") as mock_sm:
                mock_sm_inst = MagicMock()
                mock_sm_inst.start_reaper = MagicMock()
                mock_sm_inst.running = []
                mock_sm_inst.queued_count_for = MagicMock(return_value=0)
                mock_sm_inst.has_pending_work_for = MagicMock(return_value=False)
                mock_sm_inst.running_agents_for = MagicMock(return_value=[])
                mock_sm_inst.get = MagicMock(return_value=None)
                mock_sm_inst.notify_injection_failed = MagicMock()
                mock_sm.return_value = mock_sm_inst
                orch._init_subagents()
        return orch, mock_sm

    @pytest.mark.asyncio
    async def test_orchestrator_mode_failure_guard(self):
        """Orchestrator mode tracks failures."""
        orch, mock_sm = self._setup()
        on_done = mock_sm.call_args[1]["on_done"]

        # Create a slot in orchestrator mode
        slot = MagicMock()
        slot.running = False
        slot.task = None
        slot.key = "orch-slot"
        slot.mode = "orchestrator"
        slot._recovery_chat_triggered = False
        slot._pending_subagent_failures = []
        slot._orch_tracker = None
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)

        info = MagicMock()
        info.id = "agent-orch"
        info.parent_session_key = "dashboard:orch-slot"
        info.error = "task failed"
        info.result = None
        info.result_path = ""
        info.task = "orchestrated task"
        info.agent = "coder"
        info.silent = False
        info.elapsed = 5.0
        info.started = 0.0

        with patch("kiro_crew.dashboard.chat_runner._run_chat", new_callable=AsyncMock):
            await on_done(info)

        # Tracker should have been created
        assert slot._orch_tracker is not None

    @pytest.mark.asyncio
    async def test_orchestrator_result_with_path(self):
        """Orchestrator mode with result_path shows summary."""
        orch, mock_sm = self._setup()
        on_done = mock_sm.call_args[1]["on_done"]

        slot = MagicMock()
        slot.running = False
        slot.task = None
        slot.key = "orch-slot2"
        slot.mode = "orchestrator"
        slot._recovery_chat_triggered = False
        slot._pending_subagent_failures = []
        slot._orch_tracker = None
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)

        info = MagicMock()
        info.id = "agent-orch2"
        info.parent_session_key = "dashboard:orch-slot2"
        info.error = None
        info.result = "word " * 300  # long result
        info.result_path = "/tmp/result.txt"
        info.task = "big task"
        info.agent = ""
        info.silent = False
        info.elapsed = 10.0
        info.started = 0.0

        with patch("kiro_crew.dashboard.chat_runner._run_chat", new_callable=AsyncMock):
            with patch("os.path.getsize", return_value=5000):
                await on_done(info)

        orch.dashboard_state.notify.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _init_autonudge _fire callback
# ═══════════════════════════════════════════════════════════════════════════


class TestAutonudgeFire:
    """AutoNudge fire callback."""

    @pytest.mark.asyncio
    async def test_fire_no_dashboard(self):
        """Fire with no dashboard → returns False."""
        orch = _make_orchestrator()
        orch.dashboard_state = None
        with patch("kiro_crew.slack.gateway.autonudge_enabled", return_value=True):
            with patch("kiro_crew.slack.gateway.AutoNudgeService") as mock_ans:
                mock_inst = MagicMock()
                mock_inst.start = AsyncMock()
                mock_inst.subscribe = MagicMock()
                mock_ans.return_value = mock_inst
                await orch._init_autonudge()

        # Get the on_fire callback
        on_fire = mock_ans.call_args[1]["on_fire"]
        loop = MagicMock()
        loop.id = "loop1"
        loop.slot_key = "s1"
        loop.message = "nudge"
        loop.stop_sentinel_path = None
        loop.cycle_count = 0
        result = await on_fire(loop)
        assert result is False

    @pytest.mark.asyncio
    async def test_fire_slot_missing(self):
        """Fire with missing slot → removes loop.

        The rehydrate fallback is stubbed to a miss because that is what "slot
        missing" means here. It used to be produced incidentally: the mock
        dashboard state's MagicMock metadata read as ``closed``, and the
        rehydrate helper's closed-guard bailed. The fire path now passes
        ``adopt_closed=True`` (idle archival must not destroy a loop), so that
        accident no longer stops the walk.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        ds._slots = {}
        orch.dashboard_state = ds
        with patch("kiro_crew.slack.gateway.autonudge_enabled", return_value=True):
            with patch("kiro_crew.slack.gateway.AutoNudgeService") as mock_ans:
                mock_inst = MagicMock()
                mock_inst.start = AsyncMock()
                mock_inst.subscribe = MagicMock()
                mock_inst.remove = AsyncMock()
                mock_ans.return_value = mock_inst
                await orch._init_autonudge()

        on_fire = mock_ans.call_args[1]["on_fire"]
        loop = MagicMock()
        loop.id = "loop2"
        loop.slot_key = "gone"
        loop.message = "nudge"
        loop.stop_sentinel_path = None
        loop.cycle_count = 0
        with patch(
            "kiro_crew.slack.gateway.rehydrate_slot_from_history_async",
            new=AsyncMock(return_value=None),
        ):
            result = await on_fire(loop)
        assert result is False

    @pytest.mark.asyncio
    async def test_fire_slot_running_skips(self):
        """Fire with running slot → returns False (skip)."""
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        slot = MagicMock()
        slot.running = True
        slot.key = "busy"
        ds._slots = {"busy": slot}
        orch.dashboard_state = ds
        with patch("kiro_crew.slack.gateway.autonudge_enabled", return_value=True):
            with patch("kiro_crew.slack.gateway.AutoNudgeService") as mock_ans:
                mock_inst = MagicMock()
                mock_inst.start = AsyncMock()
                mock_inst.subscribe = MagicMock()
                mock_ans.return_value = mock_inst
                await orch._init_autonudge()

        on_fire = mock_ans.call_args[1]["on_fire"]
        loop = MagicMock()
        loop.id = "loop3"
        loop.slot_key = "busy"
        loop.message = "nudge"
        loop.stop_sentinel_path = None
        loop.cycle_count = 0
        result = await on_fire(loop)
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
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.hooks = MagicMock()
        orch.conv_log = MagicMock()
        orch.consolidator = MagicMock()
        orch.dashboard_state = None
        with patch("kiro_crew.slack.gateway.TaskRunner") as mock_tr:
            mock_tr_inst = MagicMock()
            mock_tr.return_value = mock_tr_inst
            orch._init_task_runner()
        # Get the _on_approval callback
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
        orch.ctx_builder = MagicMock()
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


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _init_dashboard wiring
# ═══════════════════════════════════════════════════════════════════════════


class TestInitDashboardWiring:
    """Dashboard wiring with slack and no_crons."""

    @pytest.mark.asyncio
    async def test_dashboard_wires_slack_client(self):
        orch = _make_orchestrator(slack_enabled=True)
        orch.sessions = _mock_sessions()
        orch.cron_svc = MagicMock()
        orch.subagent_mgr = MagicMock()
        orch.ctx_builder = MagicMock()
        orch.conv_log = MagicMock()
        orch.consolidator = MagicMock()
        orch.task_runner = MagicMock()
        mock_slack = MagicMock()
        orch.slack = mock_slack
        ds = _mock_dashboard_state()
        runner = MagicMock()
        with patch(
            "kiro_crew.slack.gateway.start_dashboard",
            new_callable=AsyncMock,
            return_value=(runner, ds),
        ):
            await orch._init_dashboard()
        assert ds.slack_client == mock_slack
        assert ds.no_crons is False

    @pytest.mark.asyncio
    async def test_dashboard_no_crons_flag(self):
        orch = _make_orchestrator(no_crons=True)
        orch.sessions = _mock_sessions()
        orch.cron_svc = MagicMock()
        orch.subagent_mgr = MagicMock()
        orch.ctx_builder = MagicMock()
        orch.conv_log = MagicMock()
        orch.consolidator = MagicMock()
        orch.task_runner = MagicMock()
        orch.slack = None
        ds = _mock_dashboard_state()
        runner = MagicMock()
        with patch(
            "kiro_crew.slack.gateway.start_dashboard",
            new_callable=AsyncMock,
            return_value=(runner, ds),
        ):
            await orch._init_dashboard()
        assert ds.no_crons is True


# ═══════════════════════════════════════════════════════════════════════════
# Tests: Cron with acked_items
# ═══════════════════════════════════════════════════════════════════════════


class TestCronAckedItems:
    """Cron callback with acked_items."""

    @pytest.mark.asyncio
    async def test_acked_items_appended_to_message(self):
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.build_message = MagicMock(return_value=("msg", None))
        orch.ctx_builder.hooks = MagicMock()
        orch.subagent_mgr = MagicMock()
        orch.subagent_mgr.running = []
        orch.dashboard_state = None
        orch.slack = None

        with patch("kiro_crew.slack.gateway.CronService") as mock_cs:
            mock_cs_inst = MagicMock()
            mock_cs_inst.start = AsyncMock()
            mock_cs_inst.start_reaper = MagicMock()
            mock_cs_inst.register_active_session_key = MagicMock()
            mock_cs_inst.clear_active_session_key = MagicMock()
            mock_cs.return_value = mock_cs_inst
            mock_cs.create = AsyncMock(return_value=mock_cs_inst)
            await orch._init_cron()

        callback = mock_cs.create.call_args[1]["on_job"]

        job = MagicMock()
        job.script = ""
        job.command = ""
        job.id = "jack"
        job.name = "acked-job"
        job.persistent_session = True
        job.agent_sequence = []
        job.agent_id = None
        job.channel = ""
        job.created_by = ""
        job.approval_mode = "auto"
        job.env = None
        job.acked_items = ["item1", "item2"]
        job.silent = True
        job.thread_ts = None
        job.last_posted_hash = ""
        job.consecutive_dupes = 0
        job.last_posted_at = 0.0
        job.last_failure_hash = ""
        job.last_failure_at = 0.0
        job.consecutive_failures = 0

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            return_value="acked result",
        ):
            with patch(
                "kiro_crew.slack.gateway.build_cron_session_context",
                return_value=("cron:jack", "run"),
            ):
                with patch("kiro_crew.sel.sel") as mock_sel:
                    mock_sel.return_value.log_tool_invocation = MagicMock()
                    result = await callback(job)

        assert result == "acked result"
        # Verify acked_items were passed to build_message
        call_args = orch.ctx_builder.build_message.call_args[0][0]
        assert "item1" in call_args
        assert "item2" in call_args


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _retrigger_recovery
# ═══════════════════════════════════════════════════════════════════════════


class TestRetriggerRecovery:
    """Recovery retrigger for queued subagent failures."""

    def _setup(self):
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.hooks = MagicMock()
        orch.ctx_builder.build_message = MagicMock(return_value=("msg", None))
        orch.dashboard_state = _mock_dashboard_state()
        with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False):
            with patch("kiro_crew.slack.gateway.SubagentManager") as mock_sm:
                mock_sm_inst = MagicMock()
                mock_sm_inst.start_reaper = MagicMock()
                mock_sm_inst.running = []
                mock_sm_inst.queued_count_for = MagicMock(return_value=0)
                mock_sm_inst.has_pending_work_for = MagicMock(return_value=False)
                mock_sm_inst.running_agents_for = MagicMock(return_value=[])
                mock_sm_inst.get = MagicMock(return_value=None)
                mock_sm_inst.notify_injection_failed = MagicMock()
                mock_sm.return_value = mock_sm_inst
                orch._init_subagents()
        return orch, mock_sm

    @pytest.mark.asyncio
    async def test_subagent_event_injection_failed(self):
        """Subagent injection_failed event updates slot."""
        orch, mock_sm = self._setup()
        on_event = mock_sm.call_args[1]["on_event"]

        slot = MagicMock()
        slot.append = MagicMock()
        slot._pending_subagent_failures = []
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)

        info = MagicMock()
        info.id = "agent-fail"
        info.parent_session_key = "dashboard:slot1"
        info.task = "failed task"

        await on_event(
            "subagent_injection_failed",
            info,
            {"error": "timed out", "failure_msg": "Agent failed"},
        )

        slot.append.assert_called_once()
        assert len(slot._pending_subagent_failures) == 1
        orch.dashboard_state.push_slots_update.assert_called()

    @pytest.mark.asyncio
    async def test_subagent_event_chunk(self):
        """Subagent chunk event broadcasts to subscribers."""
        orch, mock_sm = self._setup()
        on_event = mock_sm.call_args[1]["on_event"]

        info = MagicMock()
        info.id = "agent-chunk"
        info.parent_session_key = "dashboard:slot1"

        await on_event("subagent_chunk", info, {"text": "partial"})
        orch.dashboard_state.broadcast_ws_subagent_subscribers.assert_called()

    @pytest.mark.asyncio
    async def test_subagent_event_status(self):
        """Generic subagent status event broadcasts to all."""
        orch, mock_sm = self._setup()
        on_event = mock_sm.call_args[1]["on_event"]

        info = MagicMock()
        info.id = "agent-status"
        info.parent_session_key = "dashboard:slot1"

        await on_event("subagent_started", info, {})
        orch.dashboard_state.broadcast_ws.assert_called()

    @pytest.mark.asyncio
    async def test_subagent_event_routes_cron_parent_to_the_cron_tab(self):
        """Regression: a cron-born parent's events must carry the TAB's slot
        key (``cron-<id>``), not the raw session key (``cron:<id>``). The
        frontend routes frames by exact slot match, so the raw key left the
        Subagents panel permanently on "No subagents running" for every agent
        spawned from a cron-born session."""
        from kiro_crew.session_surface import set_dashboard_surfaced

        orch, mock_sm = self._setup()
        on_event = mock_sm.call_args[1]["on_event"]

        info = MagicMock()
        info.id = "agent-cron"
        info.parent_session_key = "cron:188f71e5"
        info.batch_id = ""

        set_dashboard_surfaced({"cron:188f71e5"})
        try:
            await on_event("subagent_spawn", info, {"task": "t", "agent": "a"})
        finally:
            set_dashboard_surfaced(())

        orch.dashboard_state.broadcast_ws.assert_called()
        etype, payload = orch.dashboard_state.broadcast_ws.call_args[0]
        assert etype == "subagent_spawn"
        assert payload["slot"] == "cron-188f71e5"


# ═══════════════════════════════════════════════════════════════════════════
# Tests: run() signal handling and bg session
# ═══════════════════════════════════════════════════════════════════════════


class TestRunSignalAndBgSession:
    """Run method signal handling and background session."""

    @pytest.mark.asyncio
    async def test_run_with_ollama_config(self):
        """run() starts ollama when configured."""
        # no_dashboard=True so the bg-session task short-circuits the dashboard
        # branch (otherwise it races on _local_only/_dashboard_port set by the
        # mocked _init_dashboard).
        orch = _make_orchestrator(no_dashboard=True)
        orch._cfg.memory.embedding_provider = "llama_cpp"

        orch._init_services = AsyncMock()
        orch._start_embeddings = AsyncMock()
        # run() spawns _auto_migrate_memory as a fire-and-forget task; with
        # _init_services mocked it would raise AttributeError on
        # vector_memory and surface as an unretrieved-task error at GC time.
        orch._auto_migrate_memory = AsyncMock()
        orch._init_cron = AsyncMock()
        orch._init_heartbeat = AsyncMock()
        orch._init_mcp_discovery = MagicMock()
        orch._init_subagents = MagicMock()
        orch._init_task_runner = MagicMock()
        orch._init_dashboard = AsyncMock()
        orch._init_autonudge = AsyncMock()
        orch._init_api_server = AsyncMock()
        orch._check_for_updates = AsyncMock()
        orch._shutdown = AsyncMock()

        # Use a fresh asyncio.Event bound to this test's loop. The shared
        # _LazyShutdownEvent can be polluted by prior tests in full-file runs.
        fresh_event = asyncio.Event()
        fresh_event.set()
        loop = asyncio.get_running_loop()
        try:
            with patch.object(loop, "add_signal_handler"):
                with patch("kiro_crew.shutdown_event", fresh_event):
                    with patch("kiro_crew.slack.gateway.shutdown_event", fresh_event):
                        with patch("kiro_crew.slack.events.init_socket_mode"):
                            with patch("kiro_crew.slack.interactions.init"):
                                with patch("kiro_crew.slack.events.SeenCache"):
                                    with patch("kiro_crew.session.cleanup_orphaned_sessions"):
                                        with patch(
                                            "kiro_crew.dashboard.handlers._bg_mcp_probe",
                                            new_callable=AsyncMock,
                                        ):
                                            with patch("os._exit"):
                                                with patch(
                                                    "resource.getrlimit", return_value=(256, 10240)
                                                ):
                                                    with patch("resource.setrlimit"):
                                                        await orch.run()
        finally:
            pass

        orch._start_embeddings.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _check_missing_deps pip install path
# ═══════════════════════════════════════════════════════════════════════════


class TestBgSessionDashboardBranch:
    """run() -> dashboard URL announcement and the probe-gated session warm."""

    @pytest.mark.asyncio
    async def test_bg_session_prints_dashboard_url(self):
        """_start_bg_session still warms the session pool behind the probe."""
        orch = _make_orchestrator(no_dashboard=False, no_open=True)

        orch._init_services = AsyncMock()
        orch._start_embeddings = AsyncMock()
        # run() spawns _auto_migrate_memory as a fire-and-forget task; with
        # _init_services mocked it would raise AttributeError on
        # vector_memory and surface as an unretrieved-task error at GC time.
        orch._auto_migrate_memory = AsyncMock()
        orch._init_cron = AsyncMock()
        orch._init_heartbeat = AsyncMock()
        orch._init_mcp_discovery = MagicMock()
        orch._init_subagents = MagicMock()
        orch._init_task_runner = MagicMock()
        orch._init_autonudge = AsyncMock()
        orch._check_for_updates = AsyncMock()
        orch._shutdown = AsyncMock()

        # Real-ish sessions stub so _start_bg_session passes the assert
        orch.sessions = MagicMock()
        orch.sessions.start_pool = AsyncMock()

        # Stub _init_dashboard to set the attributes _start_bg_session reads
        async def _init_dash():
            orch._local_only = True
            orch._configured_host = None
            orch._dashboard_port = 6779

        orch._init_dashboard = _init_dash

        fresh_event = asyncio.Event()
        fresh_event.set()
        loop = asyncio.get_running_loop()
        with patch.object(loop, "add_signal_handler"):
            with patch("kiro_crew.shutdown_event", fresh_event):
                with patch("kiro_crew.slack.gateway.shutdown_event", fresh_event):
                    with patch(
                        "kiro_crew.slack.gateway.resolve_dashboard_host", return_value="127.0.0.1"
                    ):
                        with patch(
                            "kiro_crew.slack.gateway.build_dashboard_url",
                            return_value="http://127.0.0.1:6779/?t=tok",
                        ):
                            with patch(
                                "kiro_crew.slack.gateway.format_dashboard_urls",
                                return_value=["url-line-1", "url-line-2"],
                            ):
                                with patch("kiro_crew.slack.events.init_socket_mode"):
                                    with patch("kiro_crew.slack.interactions.init"):
                                        with patch("kiro_crew.slack.events.SeenCache"):
                                            with patch(
                                                "kiro_crew.session.cleanup_orphaned_sessions"
                                            ):
                                                with patch(
                                                    "kiro_crew.dashboard.handlers._bg_mcp_probe",
                                                    new_callable=AsyncMock,
                                                ):
                                                    with patch("os._exit"):
                                                        with patch(
                                                            "resource.getrlimit",
                                                            return_value=(256, 10240),
                                                        ):
                                                            with patch("resource.setrlimit"):
                                                                await orch.run()
                                                                # Let bg_session task drain
                                                                await asyncio.sleep(0)
                                                                await asyncio.sleep(0)

        orch.sessions.start_pool.assert_awaited_once_with(blocking=False)

    @pytest.mark.asyncio
    async def test_dashboard_url_is_printed_before_the_mcp_probe_is_awaited(self):
        """The URL must not wait on the MCP probe.

        The port is bound before either happens, and nothing about formatting a
        URL depends on MCP state — only session spawn does (kiro-cli reads
        mcp.json at spawn time). Printing after the probe cost the operator up
        to mcp_probe_timeout_secs+15 of blank screen, and all of it whenever the
        probe timed out.

        Asserted as an ORDER, not a call count, because the defect this pins is
        purely positional: both the print and the probe happened either way.
        """
        orch = _make_orchestrator(no_dashboard=False, no_open=True)

        orch._init_services = AsyncMock()
        orch._start_embeddings = AsyncMock()
        orch._auto_migrate_memory = AsyncMock()
        orch._init_cron = AsyncMock()
        orch._init_heartbeat = AsyncMock()
        orch._init_mcp_discovery = MagicMock()
        orch._init_subagents = MagicMock()
        orch._init_task_runner = MagicMock()
        orch._init_autonudge = AsyncMock()
        orch._check_for_updates = AsyncMock()
        orch._shutdown = AsyncMock()

        orch.sessions = MagicMock()
        orch.sessions.start_pool = AsyncMock()

        async def _init_dash():
            orch._local_only = True
            orch._configured_host = None
            orch._dashboard_port = 6779

        orch._init_dashboard = _init_dash

        # One ordered trace of both events. The URL lines are a distinctive
        # sentinel so ordinary boot chatter cannot be mistaken for them.
        trace: list[str] = []
        real_print = print

        def _tracing_print(*args, **kwargs):
            if args and isinstance(args[0], str) and args[0].startswith("url-line"):
                trace.append("url")
            real_print(*args, **kwargs)

        async def _tracing_probe():
            trace.append("probe")

        fresh_event = asyncio.Event()
        fresh_event.set()
        loop = asyncio.get_running_loop()
        with patch.object(loop, "add_signal_handler"):
            with patch("kiro_crew.shutdown_event", fresh_event):
                with patch("kiro_crew.slack.gateway.shutdown_event", fresh_event):
                    with patch(
                        "kiro_crew.slack.gateway.resolve_dashboard_host", return_value="127.0.0.1"
                    ):
                        with patch(
                            "kiro_crew.slack.gateway.build_dashboard_url",
                            return_value="http://127.0.0.1:6779/?t=tok",
                        ):
                            with patch(
                                "kiro_crew.slack.gateway.format_dashboard_urls",
                                return_value=["url-line-1", "url-line-2"],
                            ):
                                with patch("builtins.print", _tracing_print):
                                    with patch("kiro_crew.slack.events.init_socket_mode"):
                                        with patch("kiro_crew.slack.interactions.init"):
                                            with patch("kiro_crew.slack.events.SeenCache"):
                                                with patch(
                                                    "kiro_crew.session.cleanup_orphaned_sessions"
                                                ):
                                                    with patch(
                                                        "kiro_crew.dashboard.handlers._bg_mcp_probe",
                                                        _tracing_probe,
                                                    ):
                                                        with patch("os._exit"):
                                                            with patch(
                                                                "resource.getrlimit",
                                                                return_value=(256, 10240),
                                                            ):
                                                                with patch("resource.setrlimit"):
                                                                    await orch.run()
                                                                    await asyncio.sleep(0)
                                                                    await asyncio.sleep(0)

        assert "url" in trace, f"dashboard URL was never printed; trace={trace}"
        assert "probe" in trace, f"MCP probe was never awaited; trace={trace}"
        assert trace.index("url") < trace.index("probe"), (
            "dashboard URL was printed only AFTER the MCP probe was awaited — "
            f"the boot-delay regression is back; trace={trace}"
        )

    @pytest.mark.asyncio
    async def test_failing_url_announcement_does_not_abort_boot(self):
        """Announcing the URL is best effort — it must not take the gateway down.

        This block used to live inside a fire-and-forget task, where a raise
        could not reach the boot path. Hoisting it ahead of the MCP probe put it
        on the synchronous path, so the fault isolation has to be explicit or a
        formatting/token failure becomes a failed boot of an already-listening
        dashboard.
        """
        orch = _make_orchestrator(no_dashboard=False, no_open=True)

        orch._init_services = AsyncMock()
        orch._start_embeddings = AsyncMock()
        orch._auto_migrate_memory = AsyncMock()
        orch._init_cron = AsyncMock()
        orch._init_heartbeat = AsyncMock()
        orch._init_mcp_discovery = MagicMock()
        orch._init_subagents = MagicMock()
        orch._init_task_runner = MagicMock()
        orch._init_autonudge = AsyncMock()
        orch._check_for_updates = AsyncMock()
        orch._shutdown = AsyncMock()

        orch.sessions = MagicMock()
        orch.sessions.start_pool = AsyncMock()

        async def _init_dash():
            orch._local_only = True
            orch._configured_host = None
            orch._dashboard_port = 6779

        orch._init_dashboard = _init_dash

        fresh_event = asyncio.Event()
        fresh_event.set()
        loop = asyncio.get_running_loop()
        with patch.object(loop, "add_signal_handler"):
            with patch("kiro_crew.shutdown_event", fresh_event):
                with patch("kiro_crew.slack.gateway.shutdown_event", fresh_event):
                    with patch(
                        "kiro_crew.slack.gateway.resolve_dashboard_host", return_value="127.0.0.1"
                    ):
                        with patch(
                            "kiro_crew.slack.gateway.format_dashboard_urls",
                            side_effect=RuntimeError("cannot format URL"),
                        ):
                            with patch("kiro_crew.slack.events.init_socket_mode"):
                                with patch("kiro_crew.slack.interactions.init"):
                                    with patch("kiro_crew.slack.events.SeenCache"):
                                        with patch("kiro_crew.session.cleanup_orphaned_sessions"):
                                            with patch(
                                                "kiro_crew.dashboard.handlers._bg_mcp_probe",
                                                new_callable=AsyncMock,
                                            ):
                                                with patch("os._exit"):
                                                    with patch(
                                                        "resource.getrlimit",
                                                        return_value=(256, 10240),
                                                    ):
                                                        with patch("resource.setrlimit"):
                                                            # Must not raise.
                                                            await orch.run()
                                                            await asyncio.sleep(0)
                                                            await asyncio.sleep(0)

        # Boot carried on past the failed announcement.
        orch.sessions.start_pool.assert_awaited_once_with(blocking=False)


class TestCheckMissingDepsPip:
    """Dep repair via pip install."""

    def test_pip_install_on_missing_dep(self):
        orch = _make_orchestrator()
        with patch("importlib.util.find_spec", return_value=None):
            with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/proj"}):
                with patch.object(GatewayOrchestrator, "_is_brazil_install", return_value=False):
                    mock_exec = AsyncMock(return_value=_fake_async_proc(returncode=0))
                    with patch("asyncio.create_subprocess_exec", mock_exec):
                        asyncio.run(orch._check_missing_deps())
                    mock_exec.assert_awaited_once()
                    # Pin the command shape so a refactor cannot silently stop
                    # installing (sys.executable -m pip install ...).
                    import sys as _sys

                    args = mock_exec.await_args.args
                    assert args[:4] == (_sys.executable, "-m", "pip", "install")

    def test_pip_install_failure(self):
        orch = _make_orchestrator()
        with patch("importlib.util.find_spec", return_value=None):
            with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/proj"}):
                with patch.object(GatewayOrchestrator, "_is_brazil_install", return_value=False):
                    mock_exec = AsyncMock(
                        return_value=_fake_async_proc(returncode=1, stderr=b"error")
                    )
                    with patch("asyncio.create_subprocess_exec", mock_exec):
                        asyncio.run(orch._check_missing_deps())  # should not raise

    def test_pip_install_timeout_kills_child(self):
        """A wedged pip is killed and reaped; boot continues without raising."""
        orch = _make_orchestrator()
        proc = MagicMock()
        proc.returncode = None
        proc.kill = MagicMock()

        async def _communicate():
            if proc.kill.called:
                return (b"", b"")  # post-kill reap returns immediately
            await asyncio.sleep(3600)  # hang until wait_for cancels us
            return (b"", b"")

        proc.communicate = MagicMock(side_effect=_communicate)
        with patch("importlib.util.find_spec", return_value=None):
            with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/proj"}):
                with patch.object(GatewayOrchestrator, "_is_brazil_install", return_value=False):
                    with patch.object(GatewayOrchestrator, "_DEP_INSTALL_TIMEOUT_SECS", 0.05):
                        with patch(
                            "asyncio.create_subprocess_exec",
                            AsyncMock(return_value=proc),
                        ):
                            asyncio.run(orch._check_missing_deps())  # no raise
        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_version_probe_transport_error_does_not_abort_boot(self):
        """_warn_if_kiro_cli_outdated must never raise.

        A pipe/transport error from communicate() propagating out of this
        helper would abort run() before the dashboard binds. The child is
        still killed+reaped best-effort.
        """
        calls = {"n": 0}

        async def _communicate():
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("broken pipe")
            return (b"", b"")  # the post-kill reap

        proc = MagicMock()
        proc.returncode = None
        proc.kill = MagicMock()
        proc.communicate = MagicMock(side_effect=_communicate)
        orch = _make_orchestrator()
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            await orch._warn_if_kiro_cli_outdated()  # must not raise
        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_pip_install_cancellation_kills_child(self):
        """Gateway shutdown mid-install must not orphan pip.

        A leaked pip would race the NEXT boot's install of the same
        distributions — the half-installed state this repair path exists to
        fix.
        """
        proc = MagicMock()
        proc.returncode = None
        proc.kill = MagicMock()

        async def _communicate():
            await asyncio.sleep(3600)  # hang until cancelled
            return (b"", b"")

        proc.communicate = MagicMock(side_effect=_communicate)
        orch = _make_orchestrator()
        with patch("importlib.util.find_spec", return_value=None):
            with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/proj"}):
                with patch.object(GatewayOrchestrator, "_is_brazil_install", return_value=False):
                    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
                        task = asyncio.create_task(orch._check_missing_deps())
                        await asyncio.sleep(0.05)  # let it reach the await
                        task.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await task
        proc.kill.assert_called_once()


class TestInitServicesLoopResponsiveness:
    """The event loop must keep servicing callbacks during slow startup work.

    Invariant (issue #3051): the loop runs callbacks one at a time, so a
    synchronous subprocess/scan inside ``_init_services`` starves every other
    coroutine — including the loop-stall watchdog heartbeat once armed — for
    its whole duration.

    These tests assert the PROPERTY, not a duration (#4235): a fast ticker
    runs concurrently with the init work, and the stand-in for each slow
    call blocks — in whatever execution context production invoked it —
    until it OBSERVES the ticker advance. Fixed code runs the work off the
    loop (``asyncio.to_thread`` / an async subprocess), so the loop stays
    free, the ticker advances, and the probe returns almost immediately. A
    mutant reverted to the old synchronous on-loop shape blocks the loop
    itself, the ticker can never advance while the probe waits, and the
    probe gives up at a deliberately generous deadline and flags
    starvation.

    The previous shape bounded the ticker's max inter-tick gap by an
    absolute, load-adaptive ceiling; a scheduler stall on a saturated
    runner blew the ceiling with the invariant intact (a 1.87s gap against
    the 0.40s floor at a commit whose diff contained zero Python files).
    Polling for the property makes host contention cost only wall-clock,
    never the verdict, while an on-loop regression still fails
    deterministically: ticks CANNOT happen while the loop is blocked, so no
    amount of waiting turns a mutant green.
    """

    # Ticks a probe must observe while the slow work is in flight. Each tick
    # proves the loop scheduled another coroutine DURING the work; requiring
    # a handful rules out a single lucky wakeup counting as liveness.
    _MIN_TICKS_DURING_WORK = 5
    # How long a probe waits for those ticks before declaring the loop
    # starved. Generous on purpose (testing-conventions § Determinism: poll
    # with a generous deadline): a loaded host only DELAYS ticks, so
    # contention costs wall-clock, never the verdict. Only a blocked loop —
    # where ticks cannot happen at all — exhausts it, so this is paid only
    # on a genuinely regressed run.
    _PROBE_DEADLINE_SECS = 30.0

    @staticmethod
    def _make_ticker(state: dict):
        """A coroutine that advances ``state['ticks']`` whenever the loop is free.

        Stand-in for the loop-stall watchdog heartbeat: anything that blocks
        the loop freezes this counter for the whole block.
        """

        async def _ticker():
            while True:
                await asyncio.sleep(0.01)
                state["ticks"] += 1

        return _ticker

    def _make_probe(self, state: dict, label: str, result=None):
        """A stand-in for slow init work that polls the loop-liveness property.

        Runs in whatever execution context production invokes it in. Off the
        loop (the fixed shape) the ticker keeps running, the tick delta
        reaches ``_MIN_TICKS_DURING_WORK`` almost immediately, and *label* is
        recorded in ``state['probed']``. On the loop (the regressed shape)
        the ticker is starved for exactly as long as this function runs, the
        delta can never advance, and *label* lands in ``state['starved']``
        once the deadline expires.
        """

        def _probe(*args, **kwargs):
            start = state["ticks"]
            give_up = time.monotonic() + self._PROBE_DEADLINE_SECS
            while state["ticks"] - start < self._MIN_TICKS_DURING_WORK:
                if time.monotonic() >= give_up:
                    state["starved"].append(label)
                    return result
                time.sleep(0.01)
            state["probed"].append(label)
            return result

        return _probe

    @pytest.mark.asyncio
    async def test_slow_pip_install_does_not_starve_heartbeat(self):
        orch = _make_orchestrator()
        state: dict = {"ticks": 0, "starved": [], "probed": []}
        _ticker = self._make_ticker(state)

        async def _async_exec(*args, **kwargs):
            # The FIXED shape: production awaits communicate() on the loop,
            # so this waits for the ticker asynchronously — each await IS the
            # loop servicing another callback, which is the property.
            proc = MagicMock()
            proc.returncode = 0
            proc.kill = MagicMock()

            async def _communicate():
                start = state["ticks"]
                give_up = time.monotonic() + self._PROBE_DEADLINE_SECS
                while state["ticks"] - start < self._MIN_TICKS_DURING_WORK:
                    if time.monotonic() >= give_up:
                        state["starved"].append("pip-communicate")
                        return (b"", b"")
                    await asyncio.sleep(0.01)
                state["probed"].append("pip-communicate")
                return (b"", b"")

            proc.communicate = MagicMock(side_effect=_communicate)
            return proc

        # The OLD, buggy shape: a mutant reverted to subprocess.run invokes
        # this synchronously ON the loop, where the probe's ticks can never
        # arrive — it flags starvation at the deadline. Patched on the
        # subprocess MODULE (not the gateway namespace) because the fixed
        # gateway no longer imports subprocess at all.
        _blocking_run = self._make_probe(
            state,
            "pip-blocking-run",
            result=MagicMock(returncode=0, stdout="", stderr=b""),
        )

        with patch("importlib.util.find_spec", return_value=None):
            with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/proj"}):
                with patch.object(GatewayOrchestrator, "_is_brazil_install", return_value=False):
                    with patch("asyncio.create_subprocess_exec", side_effect=_async_exec):
                        with patch(
                            "subprocess.run",
                            side_effect=_blocking_run,
                        ):
                            ticker_task = asyncio.create_task(_ticker())
                            try:
                                # Above every probe deadline so a regressed
                                # run fails on the starvation assert below,
                                # not on a torn-down timeout.
                                await asyncio.wait_for(orch._check_missing_deps(), timeout=60)
                            finally:
                                ticker_task.cancel()
        assert state["starved"] == [], (
            f"loop starved during dep install: {state['starved']} observed no "
            f"ticker progress within {self._PROBE_DEADLINE_SECS:.0f}s"
        )
        assert "pip-communicate" in state["probed"], (
            "dep install never awaited the async subprocess — the loop-free "
            f"path was not taken (probed={state['probed']})"
        )

    @pytest.mark.asyncio
    async def test_slow_fts_rebuild_does_not_starve_heartbeat(self):
        """rebuild_index and vector init scale with usage; both must run off-loop."""
        orch = _make_orchestrator(slack_enabled=False)
        state: dict = {"ticks": 0, "starved": [], "probed": []}
        _ticker = self._make_ticker(state)

        mock_mem_inst = MagicMock()
        mock_mem_inst.init = MagicMock()
        mock_mem_inst.rebuild_index = MagicMock(
            side_effect=self._make_probe(state, "rebuild-index", result=3)
        )
        mock_vm_inst = MagicMock()
        mock_vm_inst.init = MagicMock(side_effect=self._make_probe(state, "vector-init"))
        with patch("kiro_crew.slack.gateway.MemoryStore", return_value=mock_mem_inst):
            with patch("kiro_crew.vector_memory.VectorMemoryStore") as mock_vm:
                mock_vm.return_value = mock_vm_inst
                with patch("kiro_crew.slack.gateway.SkillsLoader"):
                    with patch("kiro_crew.slack.gateway.HookManager"):
                        with patch("kiro_crew.slack.gateway.LessonStore"):
                            with patch("kiro_crew.slack.gateway.ContextBuilder"):
                                with patch(
                                    "kiro_crew.slack.gateway.ConversationLog",
                                    return_value=MagicMock(),
                                ):
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
                                                        ticker_task = asyncio.create_task(_ticker())
                                                        try:
                                                            # Above the sum of both probe
                                                            # deadlines so a regressed run
                                                            # fails on the starvation
                                                            # assert, not on teardown.
                                                            await asyncio.wait_for(
                                                                orch._init_services(), timeout=90
                                                            )
                                                        finally:
                                                            ticker_task.cancel()
        assert state["starved"] == [], (
            f"loop starved during service init: {state['starved']} observed no "
            f"ticker progress within {self._PROBE_DEADLINE_SECS:.0f}s"
        )
        assert {"rebuild-index", "vector-init"} <= set(state["probed"]), (
            "service init skipped a probed call — the invariant was not "
            f"exercised (probed={state['probed']})"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Tests: Cron with Slack delivery failure
# ═══════════════════════════════════════════════════════════════════════════


class TestCronSlackDeliveryFailure:
    """Cron Slack delivery exception handling."""

    @pytest.mark.asyncio
    async def test_slack_delivery_exception_notifies_dashboard(self):
        orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.build_message = MagicMock(return_value=("msg", None))
        orch.ctx_builder.hooks = MagicMock()
        orch.subagent_mgr = MagicMock()
        orch.subagent_mgr.running = []
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value="D_U1")
        orch.slack.post_blocks = AsyncMock(side_effect=RuntimeError("slack error"))

        with patch("kiro_crew.slack.gateway.CronService") as mock_cs:
            mock_cs_inst = MagicMock()
            mock_cs_inst.start = AsyncMock()
            mock_cs_inst.start_reaper = MagicMock()
            mock_cs_inst.register_active_session_key = MagicMock()
            mock_cs_inst.clear_active_session_key = MagicMock()
            mock_cs.return_value = mock_cs_inst
            mock_cs.create = AsyncMock(return_value=mock_cs_inst)
            await orch._init_cron()

        callback = mock_cs.create.call_args[1]["on_job"]

        job = MagicMock()
        job.script = ""
        job.command = ""
        job.id = "jslack"
        job.name = "slack-fail"
        job.persistent_session = True
        job.agent_sequence = []
        job.agent_id = None
        job.channel = ""
        job.created_by = "U1"
        job.approval_mode = "auto"
        job.env = None
        job.acked_items = []
        job.silent = False
        job.thread_ts = None
        job.last_posted_hash = ""
        job.consecutive_dupes = 0
        job.last_posted_at = 0.0
        job.last_failure_hash = ""
        job.last_failure_at = 0.0
        job.consecutive_failures = 0

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            return_value="result",
        ):
            with patch(
                "kiro_crew.slack.gateway.build_cron_session_context",
                return_value=("cron:jslack", "run"),
            ):
                result = await callback(job)

        assert result == "result"
        # Dashboard should have been notified about the Slack failure
        assert ds.notify.call_count >= 2  # once for result, once for slack failure


class TestDeliverCronResponse:
    """_deliver_cron_response — Slack delivery of post-subagent cron output."""

    def _orch_with_slack(self):
        orch = _make_orchestrator(owner_id="U_OWNER")
        orch.sessions = _mock_sessions()
        slack = MagicMock()
        slack.post_message = AsyncMock()
        slack.open_dm = AsyncMock(return_value="D_OWNER")
        orch.slack = slack
        return orch, slack

    @pytest.mark.asyncio
    async def test_posts_to_stored_channel_and_thread(self):
        orch, slack = self._orch_with_slack()
        orch.sessions.get_channel = MagicMock(return_value="C123")
        orch.sessions.get_thread = MagicMock(return_value="T456")

        posted = await orch._deliver_cron_response("cron:job1", "hello world")

        assert posted is True
        slack.post_message.assert_awaited_once()
        args = slack.post_message.call_args.args
        assert args[0] == "C123"
        assert "hello world" in args[1]
        assert args[2] == "T456"

    @pytest.mark.asyncio
    async def test_falls_back_to_owner_dm(self):
        orch, slack = self._orch_with_slack()
        # No stored channel → open owner DM. A stale thread_ts from another
        # channel must be dropped (invalid in a DM).
        orch.sessions.get_thread = MagicMock(return_value="T_STALE")
        posted = await orch._deliver_cron_response("cron:job1", "hi")

        assert posted is True
        slack.open_dm.assert_awaited_once_with("U_OWNER")
        assert slack.post_message.call_args.args[0] == "D_OWNER"
        assert slack.post_message.call_args.args[2] is None

    @pytest.mark.asyncio
    async def test_noop_when_silent(self):
        orch, slack = self._orch_with_slack()
        orch.sessions.get_channel = MagicMock(return_value="C123")

        posted = await orch._deliver_cron_response("cron:job1", "hi", silent=True)

        assert posted is False
        slack.post_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_noop_when_text_blank(self):
        orch, slack = self._orch_with_slack()
        orch.sessions.get_channel = MagicMock(return_value="C123")

        posted = await orch._deliver_cron_response("cron:job1", "   ")

        assert posted is False
        slack.post_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_renders_options_action_block(self):
        # an [OPTIONS: ...] tag in cron output renders as an
        # actions block posted after the message.
        orch, slack = self._orch_with_slack()
        slack.post_blocks = AsyncMock()
        orch.sessions.get_channel = MagicMock(return_value="C123")
        orch.sessions.get_thread = MagicMock(return_value="T456")

        posted = await orch._deliver_cron_response("cron:job1", "pick one\n\n[OPTIONS: Yes | No]")

        assert posted is True
        body = slack.post_message.call_args.args[1]
        assert "OPTIONS" not in body
        slack.post_blocks.assert_awaited_once()
        assert slack.post_blocks.call_args.args[0] == "C123"

    @pytest.mark.asyncio
    async def test_no_options_no_action_block(self):
        orch, slack = self._orch_with_slack()
        slack.post_blocks = AsyncMock()
        orch.sessions.get_channel = MagicMock(return_value="C123")

        posted = await orch._deliver_cron_response("cron:job1", "plain text")

        assert posted is True
        slack.post_blocks.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_redacts_before_posting(self):
        # Defense-in-depth: the helper must redact at the Slack boundary even
        # if the caller already redacted (security-controls).
        #
        # Asserted on the OUTCOME rather than on which redactor got called. The
        # boundary is now render_for_slack (which runs redact_via_context on both
        # sides of the mrkdwn conversion), so a mock-call assertion against
        # gateway.redact_credentials would only prove the old wiring still
        # existed -- it would pass for a path that redacted nothing and fail for
        # a correct path that redacts somewhere else. What must be true is that
        # the secret does not reach Slack.
        orch, slack = self._orch_with_slack()
        orch.sessions.get_channel = MagicMock(return_value="C123")

        secret = "AKIAIOSFODNN7EXAMPLE"
        posted = await orch._deliver_cron_response("cron:job1", f"tok {secret}")

        assert posted is True
        body = slack.post_message.call_args.args[1]
        assert secret not in body
        assert secret[:8] not in body, "a credential fragment reached Slack"

    @pytest.mark.asyncio
    async def test_redacts_a_credential_ansi_escapes_had_split(self):
        """The reassembly hazard, at this call site.

        An escape sequence dropped into the middle of a key hides it from the
        credential regex, and the ANSI strip inside to_slack_mrkdwn puts it back
        together -- so a path that redacts BEFORE normalising posts the key
        intact. This is the case the old redact-then-convert ordering here got
        wrong, and it is why the shared pipeline strips ANSI first.
        """
        orch, slack = self._orch_with_slack()
        orch.sessions.get_channel = MagicMock(return_value="C123")

        secret = "AKIAIOSFODNN7EXAMPLE"
        obfuscated = secret[:4] + "\x1b[0m" + secret[4:]
        posted = await orch._deliver_cron_response("cron:job1", f"tok {obfuscated}")

        assert posted is True
        body = slack.post_message.call_args.args[1]
        assert secret not in body, "the ANSI strip reassembled the credential"


# ═══════════════════════════════════════════════════════════════════════════
# Tests: Slack subagent completion persistence
# ═══════════════════════════════════════════════════════════════════════════


class TestSlackSubagentCompletionPersistence:
    """Verify subagent completions injected into Slack sessions are persisted."""

    def _setup(self):
        orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.hooks = MagicMock()
        orch.ctx_builder.build_message = MagicMock(return_value=("msg", None))
        orch.dashboard_state = _mock_dashboard_state()
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value="D_U1")
        orch.slack.post_message = AsyncMock()
        orch.slack.post_blocks = AsyncMock(return_value="ts")
        orch.conv_log = MagicMock()
        with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False):
            with patch("kiro_crew.slack.gateway.SubagentManager") as mock_sm:
                mock_sm_inst = MagicMock()
                mock_sm_inst.start_reaper = MagicMock()
                mock_sm_inst.running = []
                mock_sm_inst.queued_count_for = MagicMock(return_value=0)
                mock_sm_inst.has_pending_work_for = MagicMock(return_value=False)
                mock_sm_inst.running_agents_for = MagicMock(return_value=[])
                mock_sm_inst.get = MagicMock(return_value=None)
                mock_sm_inst.notify_injection_failed = MagicMock()
                mock_sm.return_value = mock_sm_inst
                orch._init_subagents()
        return orch, mock_sm

    def _make_info(self, parent_key="C123:1234567890.123456"):
        info = MagicMock()
        info.id = "agent-persist"
        info.parent_session_key = parent_key
        info.error = None
        info.result = "synthesis result"
        info.result_path = ""
        info.task = "analyze code"
        info.agent = "kirocrew"
        info.silent = False
        info.elapsed = 5.0
        info.started = time.time() - 5.0
        info._delivery_event_id = "event-persist"
        return info

    @pytest.mark.asyncio
    async def test_slack_subagent_persists_to_conversation_log(self):
        """Successful Slack injection persists both announce and response."""
        orch, mock_sm = self._setup()
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info()

        with (
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value="synthesized response",
            ),
            patch("kiro_crew.slack.gateway.is_thread_temporary", return_value=False),
            patch("kiro_crew.slack.gateway.is_thread_incognito", return_value=False),
        ):
            await on_done(info)

        # conv_log.append should have been called (via save_conversation_turn)
        assert orch.conv_log.append.call_count == 2
        user_call = orch.conv_log.append.call_args_list[0]
        assistant_call = orch.conv_log.append.call_args_list[1]
        # First call: user role (the subagent completion event)
        assert user_call[0][0] == info.parent_session_key
        assert user_call[0][1] == "user"
        assert "[Subagent completion event]" in user_call[0][2]
        assert "Event: `event-persist`" in user_call[0][2]
        # Second call: assistant role (the LLM response)
        assert assistant_call[0][0] == info.parent_session_key
        assert assistant_call[0][1] == "assistant"
        assert assistant_call[0][2] == "synthesized response"

    @pytest.mark.asyncio
    async def test_slack_subagent_redacts_response_before_persist(self):
        """LLM response is redacted (credentials/exfil URLs) before persisting,
        since the dashboard replay is an external surface."""
        orch, mock_sm = self._setup()
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info()
        # Response carries a credential-shaped token that must not reach disk raw.
        leaked = "result aws_secret_access_key=AKIAIOSFODNN7EXAMPLE done"

        with (
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value=leaked,
            ),
            patch("kiro_crew.slack.gateway.is_thread_temporary", return_value=False),
            patch("kiro_crew.slack.gateway.is_thread_incognito", return_value=False),
        ):
            await on_done(info)

        assert orch.conv_log.append.call_count == 2
        persisted_response = orch.conv_log.append.call_args_list[1][0][2]
        # The raw secret value must not be persisted verbatim.
        assert "AKIAIOSFODNN7EXAMPLE" not in persisted_response

    @pytest.mark.asyncio
    async def test_slack_subagent_skips_persistence_for_temporary_thread(self):
        """Temporary (restricted) threads should not be persisted."""
        orch, mock_sm = self._setup()
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info()

        with (
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value="response",
            ),
            patch("kiro_crew.slack.gateway.is_thread_temporary", return_value=True),
            patch("kiro_crew.slack.gateway.is_thread_incognito", return_value=False),
        ):
            await on_done(info)

        orch.conv_log.append.assert_not_called()

    @pytest.mark.asyncio
    async def test_slack_subagent_skips_persistence_for_incognito_thread(self):
        """Incognito threads should not be persisted."""
        orch, mock_sm = self._setup()
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info()

        with (
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value="response",
            ),
            patch("kiro_crew.slack.gateway.is_thread_temporary", return_value=False),
            patch("kiro_crew.slack.gateway.is_thread_incognito", return_value=True),
        ):
            await on_done(info)

        orch.conv_log.append.assert_not_called()

    @pytest.mark.asyncio
    async def test_slack_subagent_persistence_failure_does_not_break_flow(self):
        """Persistence failure should not prevent Slack posting or break the flow."""
        orch, mock_sm = self._setup()
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info()
        orch.conv_log.append = MagicMock(side_effect=OSError("disk full"))

        with (
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value="response",
            ),
            patch("kiro_crew.slack.gateway.is_thread_temporary", return_value=False),
            patch("kiro_crew.slack.gateway.is_thread_incognito", return_value=False),
        ):
            # Should not raise
            await on_done(info)

        # Slack posting should still have happened
        orch.slack.post_message.assert_called()

    @pytest.mark.asyncio
    async def test_slack_subagent_no_persistence_without_conv_log(self):
        """When conv_log is None, persistence is skipped gracefully."""
        orch, mock_sm = self._setup()
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info()
        orch.conv_log = None

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            return_value="response",
        ):
            # Should not raise
            await on_done(info)

        # Slack posting should still work
        orch.slack.post_message.assert_called()

    @pytest.mark.asyncio
    async def test_slack_subagent_persists_even_when_slack_post_fails(self):
        """Persistence is gated on ACP injection, not Slack delivery: a failed
        Slack post must NOT prevent the completion turn from being persisted."""
        orch, mock_sm = self._setup()
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info()
        # Slack delivery fails (best-effort), but injection already succeeded.
        orch.slack.post_message = AsyncMock(side_effect=RuntimeError("slack down"))

        with (
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value="synthesized response",
            ),
            patch("kiro_crew.slack.gateway.is_thread_temporary", return_value=False),
            patch("kiro_crew.slack.gateway.is_thread_incognito", return_value=False),
        ):
            # Must not raise despite the Slack failure.
            await on_done(info)

        # Slack post was attempted and failed...
        orch.slack.post_message.assert_called()
        # ...yet the turn was still persisted because injection succeeded.
        assert orch.conv_log.append.call_count == 2
        assert orch.conv_log.append.call_args_list[0][0][1] == "user"
        assert orch.conv_log.append.call_args_list[1][0][1] == "assistant"

    @pytest.mark.asyncio
    async def test_slack_subagent_persists_exactly_once_after_timeout_retry(self):
        """Unit guard: persistence fires once across a timeout-retry cycle."""
        orch, mock_sm = self._setup()
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info()

        with (
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                side_effect=[asyncio.TimeoutError(), "response text"],
            ),
            patch("kiro_crew.slack.gateway.is_thread_temporary", return_value=False),
            patch("kiro_crew.slack.gateway.is_thread_incognito", return_value=False),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await on_done(info)

        # Exactly ONE completion persisted (2 appends: user + assistant), not 4.
        assert orch.conv_log.append.call_count == 2


# ═══════════════════════════════════════════════════════════════════════════
# Tests: subagent completion delivery to non-Slack channel parents
# ═══════════════════════════════════════════════════════════════════════════


class TestSubagentChannelTransportDelivery:
    """Completion replies for channel-born parents reach the channel transport.

    A parent session started on Telegram/Discord has no dashboard tab and no
    Slack conversation, so its synthesized reply must go through the governed
    cross-surface transport ladder; a missing transport degrades to the
    dashboard notification without raising.
    """

    def _setup(self, *, parent_channel=None, transport=None):
        orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
        orch.sessions = _mock_sessions()
        # MagicMock attribute lookups return truthy mocks; the link ladder
        # treats those as real links, so pin the optional sources to None.
        orch.sessions.get_origin_link = MagicMock(return_value=None)
        orch.sessions.get_mirror_link = MagicMock(return_value=None)
        orch.sessions.get_channel = MagicMock(return_value=parent_channel)
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.hooks = MagicMock()
        orch.ctx_builder.build_message = MagicMock(return_value=("msg", None))
        orch.dashboard_state = _mock_dashboard_state()
        orch.dashboard_state.get_channel_transport = MagicMock(return_value=transport)
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value="D_U1")
        orch.slack.post_message = AsyncMock()
        orch.slack.post_blocks = AsyncMock(return_value="ts")
        with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False):
            with patch("kiro_crew.slack.gateway.SubagentManager") as mock_sm:
                mock_sm_inst = MagicMock()
                mock_sm_inst.start_reaper = MagicMock()
                mock_sm_inst.running = []
                mock_sm_inst.queued_count_for = MagicMock(return_value=0)
                mock_sm_inst.has_pending_work_for = MagicMock(return_value=False)
                mock_sm_inst.running_agents_for = MagicMock(return_value=[])
                mock_sm_inst.get = MagicMock(return_value=None)
                mock_sm_inst.notify_injection_failed = MagicMock()
                mock_sm.return_value = mock_sm_inst
                orch._init_subagents()
        return orch, mock_sm

    @staticmethod
    def _fake_transport(channel_type="telegram", proactive=True, max_chars=4096):
        async def _identity_target(target_id):
            # Mirror the Telegram transport: "user:<id>" -> (<id>, None).
            kind, _, value = target_id.partition(":")
            return (value, None) if kind == "user" and value else None

        return SimpleNamespace(
            channel_type=channel_type,
            capabilities=SimpleNamespace(
                supports_proactive_send=proactive,
                max_message_chars=max_chars,
                # 0 = not byte-capped; the byte path is Webex's and is covered
                # by test_messaging_split.py.
                max_message_bytes=0,
            ),
            send_message=AsyncMock(return_value="mid-1"),
            resolve_configured_target=AsyncMock(side_effect=_identity_target),
            # Part of the MessagingTransport contract the send ladder consults: a
            # proactive send re-checks that the link's recipient is still on the
            # roster. Permissive here so these tests keep exercising delivery;
            # test_channel_transport_outbound_authz owns the refusal path.
            may_send_to=lambda conversation_id, thread_id=None, principal="": True,
        )

    def _make_info(self, parent_key):
        info = MagicMock()
        info.id = "agent-channel"
        info.parent_session_key = parent_key
        info.error = None
        info.result = "channel result"
        info.result_path = ""
        info.task = "analyze code"
        info.agent = "kirocrew"
        info.silent = False
        info.elapsed = 5.0
        info.started = time.time() - 5.0
        return info

    @staticmethod
    def _permit_governance():
        return patch(
            "kiro_crew.platform.governance_profiles.vet_and_audit",
            return_value=SimpleNamespace(permitted=True),
        )

    @pytest.mark.asyncio
    async def test_telegram_parent_reply_reaches_registered_transport(self):
        """A telegram:-born parent's synthesized reply is sent via the transport,
        addressed to the session's own conversation id, never through Slack."""
        transport = self._fake_transport("telegram")
        orch, mock_sm = self._setup(parent_channel="telegram:12345", transport=transport)
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info("telegram:kirocrew:direct:12345")

        with (
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value="synthesized reply",
            ),
            self._permit_governance(),
        ):
            await on_done(info)

        transport.send_message.assert_awaited_once_with(
            "12345", "synthesized reply", thread_id=None
        )
        orch.slack.post_message.assert_not_awaited()
        orch.slack.open_dm.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_origin_link_wins_over_stored_channel(self):
        """A recorded origin link (the conversation's real send target) takes
        precedence over the stored channel value."""
        transport = self._fake_transport("discord")
        orch, mock_sm = self._setup(parent_channel="discord:U999", transport=transport)
        from kiro_crew.messaging.link import ChannelLink

        orch.sessions.get_origin_link = MagicMock(
            return_value=ChannelLink("discord", channel_id="C777")
        )
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info("discord:kirocrew:direct:U999")

        with (
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value="reply",
            ),
            self._permit_governance(),
        ):
            await on_done(info)

        transport.send_message.assert_awaited_once_with("C777", "reply", thread_id=None)

    @pytest.mark.asyncio
    async def test_slack_parent_still_posts_through_slack_client(self):
        """Regression: a Slack-born parent keeps the dedicated Slack posting."""
        orch, mock_sm = self._setup(parent_channel="C123")
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info("slack:1234567890.123456")

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            return_value="slack reply",
        ):
            await on_done(info)

        orch.slack.post_message.assert_awaited()
        posted_channel = orch.slack.post_message.await_args_list[0][0][0]
        assert posted_channel == "C123"
        orch.dashboard_state.get_channel_transport.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_transport_degrades_to_notification_only(self):
        """No registered transport: no crash, no Slack misdelivery, and the
        dashboard notification still fires."""
        orch, mock_sm = self._setup(parent_channel="telegram:12345", transport=None)
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info("telegram:kirocrew:direct:12345")

        with (
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value="synthesized reply",
            ),
            self._permit_governance(),
        ):
            await on_done(info)

        orch.slack.post_message.assert_not_awaited()
        orch.dashboard_state.notify.assert_called()

    @pytest.mark.asyncio
    async def test_transport_send_failure_never_raises(self):
        """A transport that fails to send must not break completion handling."""
        transport = self._fake_transport("telegram")
        transport.send_message = AsyncMock(side_effect=RuntimeError("network down"))
        orch, mock_sm = self._setup(parent_channel="telegram:12345", transport=transport)
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info("telegram:kirocrew:direct:12345")

        with (
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value="synthesized reply",
            ),
            self._permit_governance(),
        ):
            await on_done(info)  # must not raise

        orch.dashboard_state.notify.assert_called()

    @pytest.mark.asyncio
    async def test_target_snapshotted_before_injection_survives_session_reset(self):
        """A timeout-path sessions.reset() evicts the in-memory origin link;
        the delivery target is snapshotted before injection, so a retry that
        then succeeds still delivers to the original conversation."""
        transport = self._fake_transport("discord")
        orch, mock_sm = self._setup(parent_channel=None, transport=transport)
        from kiro_crew.messaging.link import ChannelLink

        # Origin link present at entry, gone after the first (timed-out)
        # injection attempt — exactly what reset() does to a live session.
        orch.sessions.get_origin_link = MagicMock(
            side_effect=[ChannelLink("discord", channel_id="C777", thread_id="T1")] + [None] * 8
        )
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info("discord:kirocrew:direct:U999")

        with (
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                side_effect=[asyncio.TimeoutError, "reply after retry"],
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
            self._permit_governance(),
        ):
            await on_done(info)

        transport.send_message.assert_awaited_once_with("C777", "reply after retry", thread_id="T1")

    @pytest.mark.asyncio
    async def test_peer_resolution_outcome_is_sel_audited(self):
        """The configured-target allow-list decision lands in the SEL trail
        (allowed and denied alike), matching the chat_mirror precedent."""
        for resolved_target, expected in ((("12345", None), "allowed"), (None, "denied")):
            transport = self._fake_transport("telegram")
            transport.resolve_configured_target = AsyncMock(return_value=resolved_target)
            orch, mock_sm = self._setup(parent_channel="telegram:12345", transport=transport)
            on_done = mock_sm.call_args[1]["on_done"]
            info = self._make_info("telegram:kirocrew:direct:12345")

            with (
                patch(
                    "kiro_crew.slack.gateway.stream_and_collect",
                    new_callable=AsyncMock,
                    return_value="reply",
                ),
                self._permit_governance(),
                patch("kiro_crew.slack.gateway.sel") as mock_sel,
            ):
                mock_sel.return_value.log_api_access = MagicMock()
                await on_done(info)

            audit_calls = [
                c
                for c in mock_sel.return_value.log_api_access.call_args_list
                if c.kwargs.get("operation") == "subagent.reply_target_resolve"
            ]
            assert len(audit_calls) == 1
            assert audit_calls[0].kwargs["outcome"] == expected

    @pytest.mark.asyncio
    async def test_reply_is_redacted_before_send(self):
        """Fresh LLM output is redacted at the channel egress."""
        transport = self._fake_transport("telegram")
        orch, mock_sm = self._setup(parent_channel="telegram:12345", transport=transport)
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info("telegram:kirocrew:direct:12345")
        leaked = "result aws_secret_access_key=AKIAIOSFODNN7EXAMPLE done"

        with (
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value=leaked,
            ),
            self._permit_governance(),
        ):
            await on_done(info)

        transport.send_message.assert_awaited_once()
        sent_text = transport.send_message.await_args[0][1]
        assert "AKIAIOSFODNN7EXAMPLE" not in sent_text

    @pytest.mark.asyncio
    async def test_forum_parent_without_links_degrades_to_notification(self):
        """A forum-born parent's stored channel value carries the SENDER's user
        id; without an origin/mirror link the reply must NOT be sent (a send
        would leak group conversation content into a private DM)."""
        transport = self._fake_transport("telegram")
        orch, mock_sm = self._setup(parent_channel="telegram:12345", transport=transport)
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info("telegram:kirocrew:forum:987:5")

        with (
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value="synthesized reply",
            ),
            self._permit_governance(),
        ):
            await on_done(info)

        transport.send_message.assert_not_awaited()
        orch.dashboard_state.notify.assert_called()

    @pytest.mark.asyncio
    async def test_mirror_link_delivers_into_forum_topic(self):
        """A Telegram /link mirror binding wins over the stored value and
        carries the forum Topic thread id."""
        transport = self._fake_transport("telegram")
        orch, mock_sm = self._setup(parent_channel="telegram:12345", transport=transport)
        from kiro_crew.messaging.link import ChannelLink

        orch.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="987", thread_id="5")
        )
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info("telegram:kirocrew:forum:987:5")

        with (
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value="reply",
            ),
            self._permit_governance(),
        ):
            await on_done(info)

        transport.send_message.assert_awaited_once_with("987", "reply", thread_id="5")
        # A link recorded by the transport is already a postable conversation.
        transport.resolve_configured_target.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unified_parent_uses_stored_channel_value(self):
        """A unified: DM bucket (direct-only by construction) resolves the
        stored channel value even though its namespace differs from the key's."""
        transport = self._fake_transport("telegram")
        orch, mock_sm = self._setup(parent_channel="telegram:12345", transport=transport)
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info("unified:kirocrew")

        with (
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value="reply",
            ),
            self._permit_governance(),
        ):
            await on_done(info)

        transport.send_message.assert_awaited_once_with("12345", "reply", thread_id=None)

    @pytest.mark.asyncio
    async def test_stored_peer_id_is_resolved_to_a_postable_conversation(self):
        """The stored value is the peer's USER id; the transport resolves the
        postable conversation (e.g. Discord DM-channel creation, Teams'
        learned conversation) via resolve_configured_target."""
        transport = self._fake_transport("discord")
        transport.resolve_configured_target = AsyncMock(return_value=("DM123", None))
        orch, mock_sm = self._setup(parent_channel="discord:U999", transport=transport)
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info("discord:kirocrew:direct:U999")

        with (
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value="reply",
            ),
            self._permit_governance(),
        ):
            await on_done(info)

        transport.resolve_configured_target.assert_awaited_once_with("user:U999")
        transport.send_message.assert_awaited_once_with("DM123", "reply", thread_id=None)

    @pytest.mark.asyncio
    async def test_unreachable_peer_fails_closed(self):
        """A peer the transport cannot reach (e.g. Teams with no learned
        conversation/serviceUrl) degrades to notification-only, no send."""
        transport = self._fake_transport("teams")
        transport.resolve_configured_target = AsyncMock(return_value=None)
        orch, mock_sm = self._setup(parent_channel="teams:user@example.com", transport=transport)
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info("teams:kirocrew:direct:user@example.com")

        with (
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value="reply",
            ),
            self._permit_governance(),
        ):
            await on_done(info)

        transport.send_message.assert_not_awaited()
        orch.dashboard_state.notify.assert_called()

    @pytest.mark.asyncio
    async def test_governance_denial_blocks_the_send(self):
        """A non-permitting governance decision must block the egress."""
        transport = self._fake_transport("telegram")
        orch, mock_sm = self._setup(parent_channel="telegram:12345", transport=transport)
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info("telegram:kirocrew:direct:12345")

        with (
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value="reply",
            ),
            patch(
                "kiro_crew.platform.governance_profiles.vet_and_audit",
                return_value=SimpleNamespace(permitted=False),
            ),
        ):
            await on_done(info)

        transport.send_message.assert_not_awaited()
        orch.dashboard_state.notify.assert_called()

    @pytest.mark.asyncio
    async def test_long_reply_is_chunked_to_the_transport_limit(self):
        """A reply longer than max_message_chars arrives as multiple sends."""
        transport = self._fake_transport("telegram", max_chars=20)
        orch, mock_sm = self._setup(parent_channel="telegram:12345", transport=transport)
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info("telegram:kirocrew:direct:12345")
        long_reply = "\n".join(f"line {i} of the reply" for i in range(6))

        with (
            patch(
                "kiro_crew.slack.gateway.stream_and_collect",
                new_callable=AsyncMock,
                return_value=long_reply,
            ),
            self._permit_governance(),
        ):
            await on_done(info)

        assert transport.send_message.await_count > 1
        reassembled = "".join(c.args[1] for c in transport.send_message.await_args_list)
        assert "line 5 of the reply" in reassembled


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _connect_slack resilience (Slack connect must never crash the gateway)
# ═══════════════════════════════════════════════════════════════════════════


class TestConnectSlackResilience:
    """A failing Slack socket-mode connect must fall back to dashboard-only."""

    @pytest.mark.asyncio
    async def test_returns_false_when_slack_disabled(self):
        orch = _make_orchestrator()
        orch._socket_client = None
        assert await orch._connect_slack() is False

    @pytest.mark.asyncio
    async def test_returns_true_on_successful_connect(self, capsys):
        orch = _make_orchestrator()
        orch._socket_client = MagicMock()
        orch._socket_client.connect = AsyncMock()
        assert await orch._connect_slack() is True
        orch._socket_client.connect.assert_awaited_once()
        assert "connected to Slack" in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_connect_timeout_is_non_fatal(self, capsys):
        # Reproduces the proxy/timeout crash: connect raises TimeoutError.
        orch = _make_orchestrator()
        orch._socket_client = MagicMock()
        orch._socket_client.connect = AsyncMock(side_effect=asyncio.TimeoutError)
        # Must NOT raise — gateway continues in dashboard-only mode.
        assert await orch._connect_slack() is False
        assert "dashboard-only" in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_cancelled_error_propagates(self):
        # CancelledError is BaseException, not Exception — real cancellation
        # must still propagate (we only swallow ordinary connect failures).
        orch = _make_orchestrator()
        orch._socket_client = MagicMock()
        orch._socket_client.connect = AsyncMock(side_effect=asyncio.CancelledError)
        with pytest.raises(asyncio.CancelledError):
            await orch._connect_slack()


def _provider(active: bool):
    """A provider mock whose has_active_turn() returns *active*."""
    p = MagicMock()
    p.has_active_turn = MagicMock(return_value=active)
    return p


class TestCountInFlightWork:
    """Cover GatewayOrchestrator._count_in_flight_work (stale-asset drain)."""

    def test_zero_when_idle(self):
        orch = _make_orchestrator()
        orch.dashboard_state = None
        orch._session_tasks = {}
        assert orch._count_in_flight_work() == 0

    def test_counts_active_provider_turns_only(self):
        orch = _make_orchestrator()
        state = MagicMock()
        state.sessions.active_providers.return_value = [
            _provider(True),
            _provider(False),
            _provider(True),
        ]
        orch.dashboard_state = state
        orch._session_tasks = {}
        assert orch._count_in_flight_work() == 2

    def test_skips_missing_accessor_and_swallows_predicate_errors(self):
        orch = _make_orchestrator()
        no_attr = MagicMock(spec=[])  # no has_active_turn attribute
        raising = MagicMock()
        raising.has_active_turn = MagicMock(side_effect=RuntimeError("boom"))
        state = MagicMock()
        state.sessions.active_providers.return_value = [no_attr, raising, _provider(True)]
        orch.dashboard_state = state
        orch._session_tasks = {}
        # no_attr skipped, raising treated as idle, only the active one counts.
        assert orch._count_in_flight_work() == 1

    def test_counts_undone_session_tasks(self):
        orch = _make_orchestrator()
        orch.dashboard_state = None
        undone1, undone2, done = MagicMock(), MagicMock(), MagicMock()
        undone1.done.return_value = False
        undone2.done.return_value = False
        done.done.return_value = True
        orch._session_tasks = {"a": undone1, "b": done, "c": undone2}
        assert orch._count_in_flight_work() == 2

    def test_active_providers_failure_is_treated_as_idle(self):
        orch = _make_orchestrator()
        state = MagicMock()
        state.sessions.active_providers.side_effect = RuntimeError("nope")
        orch.dashboard_state = state
        orch._session_tasks = {}
        # A broken introspection surface must not wedge shutdown -> counts 0.
        assert orch._count_in_flight_work() == 0

    def test_provider_turns_and_session_tasks_sum(self):
        orch = _make_orchestrator()
        state = MagicMock()
        state.sessions.active_providers.return_value = [_provider(True)]
        orch.dashboard_state = state
        undone = MagicMock()
        undone.done.return_value = False
        orch._session_tasks = {"x": undone}
        assert orch._count_in_flight_work() == 2


class TestUnreadyChannelBadge:
    """An ENABLED channel that cannot start owes the operator a reason.

    Its factory returns None silently, so ``channel_status`` reported
    ``{connected: False, error: ""}``, byte-identical to a channel nobody
    configured, which System > Services deliberately filters out. The badge is what
    makes the two distinguishable, and it names the missing credential rather than
    only producing a row.
    """

    def _orch_with(self, **sections):
        from kiro_crew.messaging.registry import ChannelDescriptor

        orch = _make_orchestrator()
        orch.dashboard_state = MagicMock()
        for name, values in sections.items():
            section = getattr(orch._cfg, name)
            for key, value in values.items():
                object.__setattr__(section, key, value)
        orch._cfg.load_credentials = lambda: {}
        boot = tuple(ChannelDescriptor(channel_type=name, start=AsyncMock()) for name in sections)
        return orch, boot

    def test_an_enabled_channel_missing_its_token_is_badged_with_the_reason(self):
        orch, boot = self._orch_with(telegram={"enabled": True, "bot_token": ""})
        orch._badge_unready_channels(boot)
        error = orch.dashboard_state.telegram_connect_error
        assert "Enabled but not started" in error
        # The actionable half: WHICH credential, so the operator is not left
        # guessing which of several a channel needs.
        assert "TELEGRAM_BOT_TOKEN" in error

    def test_a_disabled_channel_is_not_badged(self):
        """It has nothing to report, and a row for it would be the noise the
        Services filter exists to remove."""
        orch, boot = self._orch_with(telegram={"enabled": False, "bot_token": ""})
        orch._badge_unready_channels(boot)
        assert not isinstance(orch.dashboard_state.telegram_connect_error, str)

    def test_a_ready_channel_is_not_badged(self):
        """A credentialed channel is the gateway's own outcome to report."""
        orch, boot = self._orch_with(telegram={"enabled": True, "bot_token": "12345:AA"})
        orch._badge_unready_channels(boot)
        assert not isinstance(orch.dashboard_state.telegram_connect_error, str)

    def test_a_channel_outside_the_bootable_set_is_not_badged(self):
        """Slack is host-managed (``start=None``); its own connect path reports it."""
        orch, _ = self._orch_with(telegram={"enabled": True, "bot_token": ""})
        orch._badge_unready_channels(())
        assert not isinstance(orch.dashboard_state.telegram_connect_error, str)

    def test_the_badge_never_breaks_boot(self):
        """Best-effort by construction: a diagnostic must not stop a transport."""
        orch, boot = self._orch_with(telegram={"enabled": True, "bot_token": ""})
        orch._cfg.load_credentials = MagicMock(side_effect=RuntimeError("cred store down"))
        orch._badge_unready_channels(boot)  # must not raise

    def test_no_dashboard_state_is_tolerated(self):
        orch, boot = self._orch_with(telegram={"enabled": True, "bot_token": ""})
        orch.dashboard_state = None
        orch._badge_unready_channels(boot)  # must not raise


class TestChannelTransportStartGate:
    """`_start_channel_transports` gates each non-Slack transport start on the
    ``channels`` governance scope, using the same member ids as the send/receive
    chokepoints. Clients are mocked — no real network connections are opened.
    """

    def _install_policy(self, policy_body):
        import dataclasses

        from kiro_crew.platform import context as ctx_mod
        from kiro_crew.platform.bootstrap import build_default_context
        from kiro_crew.platform.governance import parse_policy

        base = build_default_context(KiroCrewConfig.load())
        ceiling = parse_policy(policy_body) if policy_body is not None else None
        ctx_mod.set_context(dataclasses.replace(base, governance=ceiling))

    @staticmethod
    def _enable_all_transports(orch):
        # The start gate now evaluates governance ONLY for config-enabled
        # transports (enabled-only eval), so a test that expects a transport to
        # reach maybe_start_* must mark it enabled — a transport is credential-
        # gated in real use anyway. Set the four flags the orchestrator would set
        # from config so the governance decision (not an off switch) is what
        # decides whether each transport starts.
        for _m in ("wecom", "telegram", "discord", "webex"):
            setattr(orch, f"_{_m}_enabled", True)

    def _patch_starts(self, stack, *, discord_ret=None):
        import contextlib as _cl  # local import; keeps module import block untouched

        assert isinstance(stack, _cl.ExitStack)  # documents the contract
        # The registry rewrite (PR ③) removed the module-level maybe_start_*
        # bindings from slack.gateway — the roster now comes from
        # kiro_crew.channels. Tests inject a descriptor tuple through
        # _start_channel_transports(descriptors=...) instead of patching names;
        # the mocks and every assertion below are unchanged.
        from kiro_crew.messaging.registry import ChannelDescriptor

        mocks = {
            "wecom": AsyncMock(),
            "telegram": AsyncMock(),
            "discord": AsyncMock(return_value=discord_ret),
            "webex": AsyncMock(),
        }
        self._descriptors = tuple(
            ChannelDescriptor(channel_type=name, start=mock) for name, mock in mocks.items()
        )
        return mocks

    def teardown_method(self):
        from kiro_crew.platform import context as ctx_mod
        from kiro_crew.platform import governance_profiles as gp

        ctx_mod.reset_context()
        gp.reset_store()

    @pytest.mark.asyncio
    async def test_denied_transport_is_skipped_and_client_stays_none(self):
        import contextlib

        # Policy allows only discord → telegram/wecom/webex must NOT start.
        self._install_policy(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "channels": {"members": {"mode": "allow", "allow": ["discord"]}},
            }
        )
        orch = _make_orchestrator()
        self._enable_all_transports(orch)
        discord_client = MagicMock(name="discord_client")
        with contextlib.ExitStack() as stack:
            mocks = self._patch_starts(stack, discord_ret=discord_client)
            await orch._start_channel_transports(descriptors=self._descriptors)

        # Denied members: maybe_start_* never invoked, clients stay None.
        mocks["wecom"].assert_not_awaited()
        mocks["telegram"].assert_not_awaited()
        mocks["webex"].assert_not_awaited()
        assert orch._wecom_client is None
        assert orch._telegram_client is None
        assert orch._webex_client is None
        # Allowed member: started, client wired.
        mocks["discord"].assert_awaited_once()
        assert orch._discord_client is discord_client

    @pytest.mark.asyncio
    async def test_no_policy_starts_every_transport_as_today(self):
        import contextlib

        # Default OSS build: no policy governing channels → all maybe_start_*
        # invoked exactly as before (byte-identical default behavior).
        self._install_policy(None)
        orch = _make_orchestrator()
        self._enable_all_transports(orch)
        with contextlib.ExitStack() as stack:
            mocks = self._patch_starts(stack)
            await orch._start_channel_transports(descriptors=self._descriptors)

        mocks["wecom"].assert_awaited_once()
        mocks["telegram"].assert_awaited_once()
        mocks["discord"].assert_awaited_once()
        mocks["webex"].assert_awaited_once()

    @pytest.mark.asyncio
    async def test_host_profile_deny_skips_transport(self, tmp_path, monkeypatch):
        import contextlib
        import json

        from kiro_crew.platform import governance_profiles as gp

        # Policy ALLOWS telegram + discord, but a surface:host profile narrows to
        # discord only → telegram must NOT start. This exercises the full
        # _start_channel_transports path (through the executor) and proves the
        # gate binds the host profile (session_key=HOST_SESSION_KEY); an empty key
        # would classify to "unknown" and silently ignore this profile.
        self._install_policy(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "channels": {"members": {"mode": "allow", "allow": ["telegram", "discord"]}},
            }
        )
        profiles_dir = tmp_path / "profiles"
        profiles_dir.mkdir()
        (profiles_dir / "host.json").write_text(
            json.dumps(
                {
                    "name": "host",
                    "bind": {"type": "surface", "id": "host"},
                    "channels": {"members": {"mode": "allow", "allow": ["discord"]}},
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(gp, "_PROFILES_DIR", profiles_dir)
        gp.reset_store()

        orch = _make_orchestrator()
        self._enable_all_transports(orch)
        discord_client = MagicMock(name="discord_client")
        with contextlib.ExitStack() as stack:
            mocks = self._patch_starts(stack, discord_ret=discord_client)
            await orch._start_channel_transports(descriptors=self._descriptors)

        # Host profile narrows telegram out even though the policy allowed it.
        mocks["telegram"].assert_not_awaited()
        assert orch._telegram_client is None
        # discord is in BOTH policy and profile → starts.
        mocks["discord"].assert_awaited_once()
        assert orch._discord_client is discord_client

    @pytest.mark.asyncio
    async def test_disabled_transport_not_evaluated_for_governance(self, monkeypatch):
        import contextlib

        from kiro_crew.slack import gateway as gw

        # Enabled-only eval: a config-disabled transport is NEVER passed to the
        # governance gate (avoids a spurious deny-SEL for a channel that would
        # never connect anyway). Permissive policy, but only telegram enabled →
        # the gate is queried for telegram alone; the other three never start.
        self._install_policy(None)
        orch = _make_orchestrator()
        orch._telegram_enabled = True  # only telegram enabled
        orch._wecom_enabled = False
        orch._discord_enabled = False
        orch._webex_enabled = False

        queried = []
        real_gate = gw._channel_transport_permitted

        def _spy(member):
            queried.append(member)
            return real_gate(member)

        monkeypatch.setattr(gw, "_channel_transport_permitted", _spy)
        with contextlib.ExitStack() as stack:
            mocks = self._patch_starts(stack)
            await orch._start_channel_transports(descriptors=self._descriptors)

        # Only the enabled transport was evaluated + started.
        assert queried == ["telegram"]
        mocks["telegram"].assert_awaited_once()
        mocks["wecom"].assert_not_awaited()
        mocks["discord"].assert_not_awaited()
        mocks["webex"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_slack_connect_denied_by_policy_drops_socket_client(self):
        # BLOCKING (GPT #593): Slack is a GOVERNED transport like every other
        # channel. A `channels` policy that denies `slack` must stop it from
        # CONNECTING — not merely drop its inbound messages — and must drop the
        # socket client so nothing can reconnect it later.
        self._install_policy(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "channels": {"members": {"mode": "allow", "allow": ["discord"]}},
            }
        )
        orch = _make_orchestrator()
        socket_client = MagicMock(name="socket_client")
        socket_client.connect = AsyncMock()
        orch._socket_client = socket_client

        connected = await orch._connect_slack()

        assert connected is False, "a channels deny must stop the Slack connect"
        socket_client.connect.assert_not_awaited()
        assert orch._socket_client is None, "the denied socket client must be dropped"

    @pytest.mark.asyncio
    async def test_slack_connect_permitted_with_no_policy_connects_as_today(self):
        # Default-build invariant: with no `channels` policy the Slack connect is
        # byte-identical to today (the gate permits and the socket client connects).
        self._install_policy(None)
        orch = _make_orchestrator()
        socket_client = MagicMock(name="socket_client")
        socket_client.connect = AsyncMock()
        orch._socket_client = socket_client

        connected = await orch._connect_slack()

        assert connected is True
        socket_client.connect.assert_awaited_once()
        assert orch._socket_client is socket_client


class TestProviderFailureDoesNotFallBackToLegacy:
    """A policy-selected provider OWNS the update. Falling through to the legacy
    updater on its failure would run the built-in git/CDN update on a host whose
    administrator selected a different package manager."""

    @pytest.mark.asyncio
    async def test_provider_raising_does_not_run_legacy(self, monkeypatch):
        from kiro_crew.platform.update_provider import CommandProvider

        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()

        provider = CommandProvider(check_command="c", apply_command="a")
        monkeypatch.setattr("kiro_crew.platform.update_provider.resolve_provider", lambda: provider)
        boom = AsyncMock(side_effect=RuntimeError("provider exploded"))
        monkeypatch.setattr(orch, "_check_for_updates_via_provider", boom)
        legacy = AsyncMock()
        monkeypatch.setattr(orch, "_check_for_updates_legacy", legacy)

        # Contained, not propagated: this runs on the gateway boot path.
        await orch._check_for_updates()

        legacy.assert_not_awaited()
        boom.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_resolution_failure_still_uses_legacy(self, monkeypatch):
        """Only RESOLUTION failures may fall back: if the policy cannot be read
        we do not know a provider was selected, so built-in behaviour is right."""
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()

        def _boom():
            raise RuntimeError("policy unreadable")

        monkeypatch.setattr("kiro_crew.platform.update_provider.resolve_provider", _boom)
        legacy = AsyncMock()
        monkeypatch.setattr(orch, "_check_for_updates_legacy", legacy)

        await orch._check_for_updates()
        legacy.assert_awaited_once()


class TestWheelInstallerRejectsUnsafeCdnBase:
    """The installer command embeds the CDN bases and is handed to a shell, and
    KIROCREW_CDN_BASE is operator-set, so a metacharacter could append a second
    command. `kirocrew update` already gates on this; the unattended path must too."""

    @pytest.mark.asyncio
    async def test_unsafe_base_refuses_before_spawning(self, monkeypatch):
        import kiro_crew.dashboard.handlers as handlers

        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds

        handlers._update_info.clear()
        handlers._update_info.update(
            {
                "remediation": {
                    "kind": "command",
                    "message": "Re-run the installer to upgrade.",
                    "command": "curl x | sh",
                }
            }
        )
        monkeypatch.setattr("kiro_crew.platform.update_layout.cdn_bases_are_safe", lambda: False)
        spawn = AsyncMock()
        monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)

        await orch._auto_apply_wheel_update()

        spawn.assert_not_awaited()
        ds.push_refresh.assert_called_with("update_available")


class TestWheelApplyReadsTheCapabilityCommand:
    """``_auto_apply_wheel_update`` must read the command the CALLER selected it with.

    The caller enters this branch on ``remediation_command(info)``, and the
    capability contract carries the installer command inside ``remediation``. A
    method reading a separate ``update_command`` key is entered and then no-ops,
    so a mandated update logs a warning instead of applying — and every other test
    here hides that by mocking this method out. This one does not mock it.
    """

    @pytest.mark.asyncio
    async def test_the_installer_is_spawned_from_the_remediation_command(self, monkeypatch):
        import kiro_crew.dashboard.handlers as handlers

        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()

        handlers._update_info.clear()
        handlers._update_info.update(
            {
                "remediation": {
                    "kind": "command",
                    "message": "Re-run the installer to upgrade.",
                    "command": "sh -c true",
                }
            }
        )
        monkeypatch.setattr("kiro_crew.platform.update_layout.cdn_bases_are_safe", lambda: True)
        # cli.sh is POSIX shell, so the method refuses before spawning on a host
        # with no trusted `sh` — which is every Windows runner, and is why this
        # test pins the platform AND the shell lookup. The point under test is the
        # command SOURCE, which is platform-independent; the refusals themselves
        # are pinned by the two tests below.
        monkeypatch.setattr("kiro_crew.slack.gateway.sys.platform", "linux")
        monkeypatch.setattr("kiro_crew.platform_compat.trusted_system_bin", lambda name: "/bin/sh")
        monkeypatch.setattr(
            "kiro_crew.platform.update_provider._trusted_path_env",
            lambda: {"PATH": "/usr/bin:/bin"},
        )

        proc = MagicMock()
        proc.returncode = 1  # a failed install: stops before the execv restart
        # ``None`` streams drain to empty, which is all this assertion needs; the
        # bounded reader awaits ``wait()`` afterwards.
        proc.stdout = None
        proc.stderr = None
        proc.wait = AsyncMock(return_value=1)
        spawn = AsyncMock(return_value=proc)
        monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)

        await orch._auto_apply_wheel_update()

        spawn.assert_awaited_once()
        assert "sh -c true" in " ".join(str(a) for a in spawn.await_args.args)

    @pytest.mark.asyncio
    async def test_windows_refuses_before_spawning(self, monkeypatch):
        """The installer is POSIX shell, so Windows must not reach the spawn."""
        import kiro_crew.dashboard.handlers as handlers

        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()

        handlers._update_info.clear()
        handlers._update_info.update(
            {
                "remediation": {
                    "kind": "command",
                    "message": "Re-run the installer to upgrade.",
                    "command": "sh -c true",
                }
            }
        )
        monkeypatch.setattr("kiro_crew.platform.update_layout.cdn_bases_are_safe", lambda: True)
        monkeypatch.setattr("kiro_crew.slack.gateway.sys.platform", "win32")
        spawn = AsyncMock()
        monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)

        await orch._auto_apply_wheel_update()

        spawn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_trusted_shell_refuses_before_spawning(self, monkeypatch):
        """`curl … | sh` needs a trusted shell; a bare name would reopen the hole."""
        import kiro_crew.dashboard.handlers as handlers

        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()

        handlers._update_info.clear()
        handlers._update_info.update(
            {
                "remediation": {
                    "kind": "command",
                    "message": "Re-run the installer to upgrade.",
                    "command": "sh -c true",
                }
            }
        )
        monkeypatch.setattr("kiro_crew.platform.update_layout.cdn_bases_are_safe", lambda: True)
        monkeypatch.setattr("kiro_crew.slack.gateway.sys.platform", "linux")
        monkeypatch.setattr("kiro_crew.platform_compat.trusted_system_bin", lambda name: None)
        spawn = AsyncMock()
        monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)

        await orch._auto_apply_wheel_update()

        spawn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_command_in_the_capability_does_not_spawn(self, monkeypatch):
        import kiro_crew.dashboard.handlers as handlers

        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()

        handlers._update_info.clear()
        handlers._update_info.update({"remediation": None})
        monkeypatch.setattr("kiro_crew.platform.update_layout.cdn_bases_are_safe", lambda: True)
        spawn = AsyncMock()
        monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)

        await orch._auto_apply_wheel_update()

        spawn.assert_not_awaited()

    """The SSE snapshot renders the update badge from _update_info["available"],
    which only the legacy check writes. A provider carries its own result, so
    notifying without publishing it left the badge reading a stale False and the
    operator never saw a waiting policy-defined update."""

    @pytest.mark.asyncio
    async def test_auto_update_off_publishes_state_before_notifying(self, monkeypatch):
        import kiro_crew.dashboard.handlers as handlers
        import kiro_crew.platform.update_governance as gov
        from kiro_crew.platform.update_provider import UpdateCheckResult

        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds

        handlers._update_info.clear()
        handlers._update_info.update({"update_available": False})
        monkeypatch.setattr(gov, "update_required", lambda _v: False)

        cfg = MagicMock()
        cfg.auto_update = False
        # A real provider: the method asserts isinstance(provider, UpdateProvider),
        # which a bare MagicMock does not satisfy.
        from kiro_crew.platform.update_provider import CommandProvider

        provider = CommandProvider(check_command="c", apply_command="a")
        provider.check = AsyncMock(  # type: ignore[method-assign]
            return_value=UpdateCheckResult(available=True, remote_version="9.9.9")
        )

        with patch("kiro_crew.config.KiroCrewConfig.load", return_value=cfg):
            await orch._check_for_updates_via_provider(provider)

        # The badge must be able to see it, not just the log. `check_status` is
        # asserted too: under the capability contract a verdict without a status is
        # indistinguishable from a check that never ran, so the badge would stay
        # dark on a provider's real answer.
        assert handlers._update_info["update_available"] is True
        assert handlers._update_info["latest_version"] == "9.9.9"
        assert handlers._update_info["check_status"] == "succeeded"
        ds.push_refresh.assert_called_with("update_available")


class TestMandatoryUpdateOnWheelInstall:
    """A policy min-version makes an update mandatory. On a wheel/cli.sh install
    the gateway now auto-applies via the signed installer (cli.sh handles
    RSA-SHA256 verification). The _auto_apply_wheel_update method is called
    instead of merely lighting the dashboard badge."""

    @pytest.mark.asyncio
    async def test_mandatory_update_on_wheel_auto_applies(self, monkeypatch):
        import kiro_crew.dashboard.handlers as handlers
        import kiro_crew.platform.update_governance as gov

        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds

        async def _noop_check():
            return None

        # Wheel install below a policy floor with a NEWER build available: the
        # mandatory update applies through the installer. (The no-newer-build
        # case is test_mandatory_wheel_no_newer_build_notifies below — that path
        # must NOT apply, to avoid an infinite update→restart loop.)
        handlers._update_info.clear()
        handlers._update_info.update(
            {
                "update_available": True,
                "can_apply": False,
                "managed_by": "kirocrew",
                # A feed-checkable wheel carries an installer command; that is
                # what distinguishes it from an externally-managed install.
                "remediation": {
                    "kind": "command",
                    "message": "Re-run the installer to upgrade.",
                    "command": "curl -fsSL … | sh",
                },
            }
        )
        monkeypatch.setattr(handlers, "_do_update_check", _noop_check)
        monkeypatch.setattr(gov, "update_required", lambda _v: True)
        monkeypatch.setattr(gov, "min_version", lambda: "9.9.9")
        # The installer may only be driven for the `wheel` stamp: a `source`
        # install carries the same command but re-running it builds a separate
        # venv and loops forever.
        monkeypatch.setattr("kiro_crew.slack.gateway.distribution", lambda: "wheel")

        apply_called = AsyncMock()
        monkeypatch.setattr(orch, "_auto_apply_update", apply_called)
        wheel_apply_called = AsyncMock()
        monkeypatch.setattr(orch, "_auto_apply_wheel_update", wheel_apply_called)

        await orch._check_for_updates()

        # Must NOT attempt the git apply on a non-git tree.
        apply_called.assert_not_awaited()
        # Must call the wheel auto-apply for mandatory updates.
        wheel_apply_called.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_mandatory_wheel_no_newer_build_notifies(self, monkeypatch):
        """A wheel install below the floor but with NO newer build available
        must NOT apply — applying would reinstall the same below-floor version
        and execv-restart into the same state forever. It notifies instead."""
        import kiro_crew.dashboard.handlers as handlers
        import kiro_crew.platform.update_governance as gov

        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds

        async def _noop_check():
            return None

        handlers._update_info.clear()
        handlers._update_info.update(
            {
                "update_available": False,  # floor pinned above the latest build
                "can_apply": False,
                "managed_by": "kirocrew",
                "remediation": {
                    "kind": "command",
                    "message": "Re-run the installer to upgrade.",
                    "command": "curl -fsSL … | sh",
                },
            }
        )
        monkeypatch.setattr(handlers, "_do_update_check", _noop_check)
        monkeypatch.setattr(gov, "update_required", lambda _v: True)
        monkeypatch.setattr(gov, "min_version", lambda: "9.9.9")
        monkeypatch.setattr("kiro_crew.slack.gateway.distribution", lambda: "wheel")

        wheel_apply_called = AsyncMock()
        monkeypatch.setattr(orch, "_auto_apply_update", AsyncMock())
        monkeypatch.setattr(orch, "_auto_apply_wheel_update", wheel_apply_called)

        await orch._check_for_updates()

        # Must NOT apply (would loop); must notify via the dashboard badge.
        wheel_apply_called.assert_not_awaited()
        ds.push_refresh.assert_called_with("update_available")

    @pytest.mark.asyncio
    async def test_mandatory_update_on_non_wheel_installer_badges(self, monkeypatch):
        """An install that carries an installer command but is NOT the `wheel`
        stamp (a cloud source tree) must notify rather than run the installer,
        and the badge must light even when the check left `update_available`
        False — a pre-release remote reads as not-newer while the floor still
        mandates the update."""
        import kiro_crew.dashboard.handlers as handlers
        import kiro_crew.platform.update_governance as gov

        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds

        async def _noop_check():
            return None

        handlers._update_info.clear()
        handlers._update_info.update(
            {
                "update_available": False,
                "can_apply": False,
                "managed_by": "kirocrew",
                "remediation": {
                    "kind": "command",
                    "message": "Re-run the installer to upgrade.",
                    "command": "curl -fsSL … | sh",
                },
            }
        )
        monkeypatch.setattr(handlers, "_do_update_check", _noop_check)
        monkeypatch.setattr(gov, "update_required", lambda _v: True)
        monkeypatch.setattr(gov, "min_version", lambda: "9.9.9")
        monkeypatch.setattr("kiro_crew.slack.gateway.distribution", lambda: "source")

        apply_called = AsyncMock()
        wheel_apply_called = AsyncMock()
        monkeypatch.setattr(orch, "_auto_apply_update", apply_called)
        monkeypatch.setattr(orch, "_auto_apply_wheel_update", wheel_apply_called)

        await orch._check_for_updates()

        apply_called.assert_not_awaited()
        wheel_apply_called.assert_not_awaited()
        ds.push_refresh.assert_called_once_with("update_available")
        # The dashboard badge reads _update_info["update_available"]; a mandatory
        # update must light it even though the check left it False.
        assert handlers._update_info.get("update_available") is True

    @pytest.mark.asyncio
    async def test_mandatory_update_on_externally_managed_does_not_badge(self, monkeypatch):
        """A dmg/appimage/docker install below the floor has no `can_apply`
        AND no remediation command — it updates via its own surface, so
        the CLI 'run kirocrew update' badge must NOT light."""
        import kiro_crew.dashboard.handlers as handlers
        import kiro_crew.platform.update_governance as gov

        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds

        async def _noop_check():
            return None

        handlers._update_info.clear()
        handlers._update_info.update(
            {
                "update_available": False,
                "can_apply": False,
                "managed_by": "container",
                "remediation": None,  # externally managed: no CLI update path
            }
        )
        monkeypatch.setattr(handlers, "_do_update_check", _noop_check)
        monkeypatch.setattr(gov, "update_required", lambda _v: True)
        monkeypatch.setattr(gov, "min_version", lambda: "9.9.9")
        apply_called = AsyncMock()
        monkeypatch.setattr(orch, "_auto_apply_update", apply_called)

        await orch._check_for_updates()

        apply_called.assert_not_awaited()
        ds.push_refresh.assert_not_called()

    @pytest.mark.asyncio
    async def test_mandatory_update_on_git_still_auto_applies(self, monkeypatch):
        import kiro_crew.dashboard.handlers as handlers
        import kiro_crew.platform.update_governance as gov

        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()

        async def _noop_check():
            return None

        # Git checkout: `can_apply` True, so the mandatory git apply runs.
        handlers._update_info.clear()
        handlers._update_info.update(
            {"update_available": True, "can_apply": True, "managed_by": "git"}
        )
        monkeypatch.setattr(handlers, "_do_update_check", _noop_check)
        monkeypatch.setattr(gov, "update_required", lambda _v: True)
        monkeypatch.setattr(gov, "min_version", lambda: "9.9.9")

        apply_called = AsyncMock()
        monkeypatch.setattr(orch, "_auto_apply_update", apply_called)

        await orch._check_for_updates()
        apply_called.assert_awaited_once()


# ─── Channel skip-reason warning on the PRODUCTION start path (#304, #5418) ──


_UNCREDENTIALED_PROBE_EXEMPTIONS = {
    # Slack has credential operands, but no cfg.slack.enabled setting: an
    # absent token pair means "not configured", rather than an enabled channel
    # that was silently skipped.
    "slack": "token-driven enablement with no config enabled flag",
    # These transports are config-only. Their runtime pairing/prerequisite
    # diagnostics live in the channel implementation, not in credential env.
    "whatsapp": "config-only enablement; pairing state is not a credential operand",
    "imessage": "config-only enablement through the signed-in Messages.app",
}


def _gateway_method(name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse(Path(gw.__file__).read_text(encoding="utf-8"))
    gateway_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "GatewayOrchestrator"
    )
    return next(
        node
        for node in gateway_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )


def _collapsed_enabled_operands() -> dict[str, set[str]]:
    """Return self-attribute operands read by each collapsed enabled flag."""
    found: dict[str, set[str]] = {}
    for node in ast.walk(_gateway_method("__init__")):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
            and target.attr.startswith("_")
            and target.attr.endswith("_enabled")
        ):
            continue
        channel = target.attr.removeprefix("_").removesuffix("_enabled")
        found[channel] = {
            child.attr
            for child in ast.walk(node.value)
            if isinstance(child, ast.Attribute)
            and isinstance(child.value, ast.Name)
            and child.value.id == "self"
        }
    return found


def _uncredentialed_probe_operands() -> dict[str, set[str]]:
    """Return the self-attribute values named by each production probe row."""
    assignment = next(
        node
        for node in ast.walk(_gateway_method("_start_channel_transports"))
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "uncredentialed_probe_rows"
    )
    assert isinstance(assignment.value, ast.Tuple)
    found: dict[str, set[str]] = {}
    for row in assignment.value.elts:
        assert isinstance(row, ast.Tuple) and len(row.elts) == 4
        channel_node, _, _, credentials_node = row.elts
        assert isinstance(channel_node, ast.Constant) and isinstance(channel_node.value, str)
        assert isinstance(credentials_node, ast.Tuple)
        found[channel_node.value] = {
            pair.elts[1].attr
            for pair in credentials_node.elts
            if isinstance(pair, ast.Tuple)
            and len(pair.elts) == 2
            and isinstance(pair.elts[1], ast.Attribute)
            and isinstance(pair.elts[1].value, ast.Name)
            and pair.elts[1].value.id == "self"
        }
    return found


class TestUncredentialedProbeRatchet:
    """Keep collapsed enable predicates and their skip-reason probes aligned."""

    def test_every_rostered_channel_is_accounted_for(self) -> None:
        from kiro_crew.channels import builtin_channel_descriptors

        rostered = {descriptor.channel_type for descriptor in builtin_channel_descriptors()}
        accounted = set(_uncredentialed_probe_operands()) | set(_UNCREDENTIALED_PROBE_EXEMPTIONS)
        assert rostered == accounted, (
            "rostered channels must have an uncredentialed probe row or an "
            "explicit config-only/token-driven exemption; "
            f"missing={sorted(rostered - accounted)}, "
            f"stale={sorted(accounted - rostered)}"
        )

    def test_each_probe_tracks_the_predicate_operands(self) -> None:
        enabled = _collapsed_enabled_operands()
        probes = _uncredentialed_probe_operands()
        mismatched = {
            channel: {
                "predicate": sorted(enabled.get(channel, set())),
                "probe": sorted(operands),
            }
            for channel, operands in probes.items()
            if enabled.get(channel) != operands
        }
        assert not mismatched, (
            "uncredentialed probe rows must name exactly the self-attribute "
            f"operands read by their _<channel>_enabled predicate: {mismatched}"
        )

    def test_exemptions_are_not_credential_probe_rows(self) -> None:
        overlap = set(_uncredentialed_probe_operands()) & set(_UNCREDENTIALED_PROBE_EXEMPTIONS)
        assert not overlap, (
            "a channel cannot be both credential-probed and exempt: " f"{sorted(overlap)}"
        )


# One row per collapsed-flag channel the enabled-but-uncredentialed WARNING
# covers: (channel_type, names the WARNING must carry, names it must NOT carry,
# creds entries + cfg mutations that make the channel FULLY credentialed).
# The name lists are spelled exactly as the warning must emit them, i.e. the
# credential operands each _<channel>_enabled predicate actually reads.
_UNCREDENTIALED_CHANNEL_ROWS = (
    pytest.param(
        "wecom",
        ("WECOM_BOT_ID", "WECOM_SECRET"),
        (),
        {"WECOM_BOT_ID": "wecom-bot-value", "WECOM_SECRET": "wecom-secret-value"},
        (),
        id="wecom",
    ),
    pytest.param(
        "telegram",
        ("TELEGRAM_BOT_TOKEN",),
        (),
        {"TELEGRAM_BOT_TOKEN": "telegram-token-value"},
        (),
        id="telegram",
    ),
    pytest.param(
        "weixin",
        # account_id is not a secret (it comes from the QR setup flow) but the
        # predicate reads it, so it is named like any other missing operand.
        ("WEIXIN_TOKEN", "weixin.account_id"),
        (),
        {"WEIXIN_TOKEN": "weixin-token-value"},
        (("weixin", "account_id", "weixin-account-value"),),
        id="weixin",
    ),
    pytest.param(
        "feishu",
        ("FEISHU_APP_ID", "FEISHU_APP_SECRET"),
        (),
        {
            "FEISHU_APP_ID": "feishu-app-id-value",
            "FEISHU_APP_SECRET": "feishu-app-secret-value",
        },
        (),
        id="feishu",
    ),
    pytest.param(
        "discord",
        ("DISCORD_BOT_TOKEN",),
        (),
        {"DISCORD_BOT_TOKEN": "discord-token-value"},
        (),
        id="discord",
    ),
    pytest.param(
        "webex",
        ("WEBEX_BOT_TOKEN",),
        (),
        {"WEBEX_BOT_TOKEN": "webex-token-value"},
        (),
        id="webex",
    ),
    pytest.param(
        "teams",
        ("MICROSOFT_APP_ID", "MICROSOFT_APP_PASSWORD"),
        # _teams_enabled never reads the tenant id, so the warning must not
        # send the operator to a field that cannot start the channel.
        ("MICROSOFT_APP_TENANT_ID",),
        {
            "MICROSOFT_APP_ID": "teams-app-id-value",
            "MICROSOFT_APP_PASSWORD": "teams-password-value",
        },
        (),
        id="teams",
    ),
)

_UNCREDENTIALED_CHANNEL_TYPES = (
    "wecom",
    "telegram",
    "weixin",
    "feishu",
    "discord",
    "webex",
    "teams",
)


class TestChannelSkipReasonAtTransportStart:
    """The enabled-but-uncredentialed WARNING must fire on the real start path.

    The channel registry's enabled-only gate never calls a channel factory
    when ``_<channel>_enabled`` is False — for a disabled AND for an
    enabled-but-uncredentialed channel alike — so a factory-level log can
    never be reached in production. The skip reason is therefore logged by
    ``_start_channel_transports`` at the decision point, which runs AFTER
    ``KIROCREW_READY`` (outside the boot-path window), via the seven-channel
    table feeding ``warn_if_channel_uncredentialed``. These pin that wiring
    for every collapsed-flag channel (issue #5418, generalizing the
    WeCom-only class issue #304 introduced); the helper's message contract is
    pinned in ``test_wecom_gateway.py``.

    Rows that make a ``_<channel>_enabled`` flag True (fully-credentialed
    cases) stub the governance gate to deny, so ``registry.start_channels``
    skips every factory and no transport or network is ever touched; the
    WARNING under test fires before either.

    Capture is scoped to WARNING+: the wiring contract here is the
    warning-level diagnostic (exactly one, or none), so an unrelated DEBUG/INFO
    line some channel module may grow on the skip path must not flake these.
    Helper-level COMPLETE silence (no records at any level) stays pinned in
    ``test_wecom_gateway.py``.
    """

    def _build(
        self,
        *,
        creds: dict[str, str],
        cfg_mut: tuple[tuple[str, str, object], ...] = (),
    ) -> GatewayOrchestrator:
        cfg = KiroCrewConfig()
        for section, attr, value in cfg_mut:
            setattr(getattr(cfg, section), attr, value)
        with patch.object(cfg, "load_credentials", return_value=creds):
            return GatewayOrchestrator(cfg)

    async def _start(self, orch: GatewayOrchestrator, monkeypatch) -> None:
        from kiro_crew.slack import gateway as gw

        monkeypatch.setattr(gw, "_channel_transport_permitted", lambda member: False)
        await orch._start_channel_transports()

    def _channel_records(self, caplog) -> list[logging.LogRecord]:
        names = {f"kiro_crew.{c}.gateway" for c in _UNCREDENTIALED_CHANNEL_TYPES}
        return [r for r in caplog.records if r.name in names]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "channel,names,forbidden,full_creds,full_cfg_mut", _UNCREDENTIALED_CHANNEL_ROWS
    )
    async def test_enabled_without_credentials_warns_at_default_level(
        self, caplog, monkeypatch, channel, names, forbidden, full_creds, full_cfg_mut
    ) -> None:
        orch = self._build(
            creds={"KIROCREW_OWNER_ID": "U_OWNER"},
            cfg_mut=((channel, "enabled", True),),
        )
        with caplog.at_level(logging.WARNING):
            await self._start(orch, monkeypatch)
        records = self._channel_records(caplog)
        # Exactly one WARNING, on this channel's own gateway logger, and no
        # cross-talk onto any sibling channel's logger.
        assert len(records) == 1
        assert records[0].name == f"kiro_crew.{channel}.gateway"
        assert records[0].levelno == logging.WARNING
        msg = records[0].getMessage()
        for name in names:
            assert name in msg
        for name in forbidden:
            assert name not in msg

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "channel,creds,cfg_mut,named,not_named,present_values",
        (
            pytest.param(
                "wecom",
                {"WECOM_BOT_ID": "wecom-bot-value"},
                (),
                ("WECOM_SECRET",),
                ("WECOM_BOT_ID",),
                ("wecom-bot-value",),
                id="wecom-secret-missing",
            ),
            pytest.param(
                "weixin",
                {"WEIXIN_TOKEN": "weixin-token-value"},
                (),
                ("weixin.account_id",),
                ("WEIXIN_TOKEN",),
                ("weixin-token-value",),
                id="weixin-account-id-missing",
            ),
            pytest.param(
                "weixin",
                {},
                (("weixin", "account_id", "weixin-account-value"),),
                ("WEIXIN_TOKEN",),
                ("weixin.account_id",),
                ("weixin-account-value",),
                id="weixin-token-missing",
            ),
            pytest.param(
                "teams",
                {"MICROSOFT_APP_ID": "teams-app-id-value"},
                (),
                ("MICROSOFT_APP_PASSWORD",),
                ("MICROSOFT_APP_ID",),
                ("teams-app-id-value",),
                id="teams-password-missing",
            ),
        ),
    )
    async def test_a_partial_configuration_names_only_the_missing_credential(
        self, caplog, monkeypatch, channel, creds, cfg_mut, named, not_named, present_values
    ) -> None:
        orch = self._build(
            creds={"KIROCREW_OWNER_ID": "U_OWNER", **creds},
            cfg_mut=((channel, "enabled", True), *cfg_mut),
        )
        with caplog.at_level(logging.WARNING):
            await self._start(orch, monkeypatch)
        records = self._channel_records(caplog)
        assert len(records) == 1
        msg = records[0].getMessage()
        for name in named:
            assert name in msg
        for name in not_named:
            assert name not in msg
        # Credential VALUES must never be logged, only the variable names.
        for value in present_values:
            assert value not in msg

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "channel,names,forbidden,full_creds,full_cfg_mut", _UNCREDENTIALED_CHANNEL_ROWS
    )
    async def test_a_disabled_channel_is_completely_silent(
        self, caplog, monkeypatch, channel, names, forbidden, full_creds, full_cfg_mut
    ) -> None:
        # Even with every credential present: disabled means silence.
        orch = self._build(
            creds={"KIROCREW_OWNER_ID": "U_OWNER", **full_creds},
            cfg_mut=full_cfg_mut,
        )
        with caplog.at_level(logging.WARNING):
            await self._start(orch, monkeypatch)
        assert self._channel_records(caplog) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "channel,names,forbidden,full_creds,full_cfg_mut", _UNCREDENTIALED_CHANNEL_ROWS
    )
    async def test_a_fully_credentialed_channel_is_silent(
        self, caplog, monkeypatch, channel, names, forbidden, full_creds, full_cfg_mut
    ) -> None:
        orch = self._build(
            creds={"KIROCREW_OWNER_ID": "U_OWNER", **full_creds},
            cfg_mut=((channel, "enabled", True), *full_cfg_mut),
        )
        assert getattr(orch, f"_{channel}_enabled") is True  # flag really computed
        with caplog.at_level(logging.WARNING):
            await self._start(orch, monkeypatch)
        assert self._channel_records(caplog) == []

    @pytest.mark.asyncio
    async def test_teams_tenant_id_is_not_a_credential_operand(self, caplog, monkeypatch) -> None:
        # The trap the table must not fall into: the tenant id sits in config
        # right next to the two operands that count, but _teams_enabled never
        # reads it — app id + password present with NO tenant is fully
        # credentialed, so the probe must stay SILENT rather than send the
        # operator to a field that does not gate the channel.
        orch = self._build(
            creds={
                "KIROCREW_OWNER_ID": "U_OWNER",
                "MICROSOFT_APP_ID": "teams-app-id-value",
                "MICROSOFT_APP_PASSWORD": "teams-password-value",
            },
            cfg_mut=(("teams", "enabled", True),),
        )
        assert orch._teams_enabled is True
        assert orch._teams_tenant_id == ""
        with caplog.at_level(logging.WARNING):
            await self._start(orch, monkeypatch)
        assert self._channel_records(caplog) == []

    @pytest.mark.asyncio
    async def test_telegram_deprecated_accounts_suppress_the_warning(
        self, caplog, monkeypatch
    ) -> None:
        # With telegram.accounts set the channel is stopped by DEPRECATED
        # CONFIG, not by the missing token, and that state already has its own
        # warning at config-load time — pointing the operator at
        # TELEGRAM_BOT_TOKEN here would misname the actual blocker.
        orch = self._build(
            creds={"KIROCREW_OWNER_ID": "U_OWNER"},
            cfg_mut=(
                ("telegram", "enabled", True),
                ("telegram", "accounts", {"legacy-bot": object()}),
            ),
        )
        with caplog.at_level(logging.WARNING):
            await self._start(orch, monkeypatch)
        assert self._channel_records(caplog) == []


class TestAutoApplyUpdatePreconditions:
    """The two refusals guarding the unattended reset, at the gateway level.

    The seam functions have their own unit tests; these assert the gateway
    actually HONOURS them — that it returns before spawning anything, rather than
    computing a refusal and proceeding anyway.
    """

    @pytest.mark.asyncio
    async def test_repo_declared_driver_refuses_before_running_a_driver(self):
        """A repo-named filter/textconv driver would be run BY these git commands.

        `-c` cannot pin an arbitrary driver name, so the run is refused. What must
        be proven is that the refusal lands before the commands that would EXECUTE
        such a driver — `status`, `diff` and `reset`. The branch probe ahead of it
        is a pure ref read that runs no driver, and it carries the neutralizer env
        regardless.
        """
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()

        argvs = []

        async def _fake_exec(*args, **kwargs):
            argvs.append(args)
            proc = AsyncMock()
            proc.kill = MagicMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"main\n", b""))
            proc.wait = AsyncMock(return_value=0)
            return proc

        with (
            patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}),
            patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
            patch(
                "kiro_crew.slack.gateway.repo_exec_config_reason",
                return_value="repository declares filter.evil.smudge",
            ),
            patch("kiro_crew.slack.gateway.is_primary_branch", return_value=True),
            patch("kiro_crew.slack.gateway.tracks_upstream", return_value=True),
        ):
            await orch._auto_apply_update()

        for forbidden in ("status", "diff", "reset", "fetch"):
            assert not any(forbidden in a for a in argvs), (forbidden, argvs)

    @pytest.mark.asyncio
    async def test_local_commits_refuse_before_the_reset(self):
        """A checkout ahead of origin must not be `reset --hard` unattended.

        Revalidated after the fetch, immediately before the reset, because the
        availability verdict was reached in an earlier pass and a checkout is a
        live tree. Asserts no `reset` spawn — a warning that still reset would
        already have destroyed the commits.
        """
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()

        argvs = []

        async def _fake_exec(*args, **kwargs):
            argvs.append(args)
            proc = AsyncMock()
            proc.kill = MagicMock()
            # `git diff --quiet` must report a difference so the run reaches the
            # revalidation rather than stopping at "already up to date".
            proc.returncode = 1 if "diff" in args else 0
            proc.communicate = AsyncMock(return_value=(b"main\n", b""))
            proc.wait = AsyncMock(return_value=proc.returncode)
            return proc

        with (
            patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}),
            patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
            patch(
                "kiro_crew.slack.gateway.repo_exec_config_reason",
                return_value="",
            ),
            patch("kiro_crew.slack.gateway.tracks_upstream", return_value=True),
            patch("kiro_crew.slack.gateway.commits_ahead", return_value=2),
        ):
            await orch._auto_apply_update()

        assert not any("reset" in a for a in argvs), argvs

    @pytest.mark.asyncio
    async def test_unknown_ahead_count_also_refuses(self):
        """`None` means git could not answer, which must not read as zero."""
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()

        argvs = []

        async def _fake_exec(*args, **kwargs):
            argvs.append(args)
            proc = AsyncMock()
            proc.kill = MagicMock()
            proc.returncode = 1 if "diff" in args else 0
            proc.communicate = AsyncMock(return_value=(b"main\n", b""))
            proc.wait = AsyncMock(return_value=proc.returncode)
            return proc

        with (
            patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}),
            patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
            patch(
                "kiro_crew.slack.gateway.repo_exec_config_reason",
                return_value="",
            ),
            patch("kiro_crew.slack.gateway.tracks_upstream", return_value=True),
            patch("kiro_crew.slack.gateway.commits_ahead", return_value=None),
        ):
            await orch._auto_apply_update()

        assert not any("reset" in a for a in argvs), argvs

    @pytest.mark.asyncio
    async def test_branch_not_tracking_origin_refuses_before_fetch(self):
        """The check measures `@{u}`; the apply resets `origin/<branch>`.

        When they are different remotes the reset would discard commits, so the
        apply stops. Only the branch probe may have run by then — nothing that
        fetches or writes.
        """
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()

        argvs = []

        async def _fake_exec(*args, **kwargs):
            argvs.append(args)
            proc = AsyncMock()
            proc.kill = MagicMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"main\n", b""))
            proc.wait = AsyncMock(return_value=0)
            return proc

        with (
            patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}),
            patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
            patch(
                "kiro_crew.slack.gateway.repo_exec_config_reason",
                return_value="",
            ),
            patch("kiro_crew.slack.gateway.tracks_upstream", return_value=False),
        ):
            await orch._auto_apply_update()

        assert not any("fetch" in a for a in argvs), argvs
        assert not any("reset" in a for a in argvs), argvs
