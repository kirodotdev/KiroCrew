"""Tests for kiro_crew.slack.gateway — GatewayOrchestrator coverage."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.autonudge import NudgeLoop
from kiro_crew.config.loader import KiroCrewConfig
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
            cfg, "load_credentials", return_value={
                "SLACK_APP_TOKEN": "xapp-t",
                "SLACK_BOT_TOKEN": "xoxb-t",
                "KIROCREW_OWNER_ID": "U_OWNER",
            }
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
        monkeypatch.setattr(
            "kiro_crew.slack.retry.asyncio.sleep", AsyncMock(return_value=None)
        )

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
                                                with patch("kiro_crew.agent.rebuild_agent_config", return_value=Path("/tmp/a")):
                                                    with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="kiro-cli 1.30.0")):
                                                        orch._init_services()

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
                                                with patch("kiro_crew.agent.rebuild_agent_config", return_value=Path("/tmp/a")):
                                                    with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="kiro-cli 1.30.0")):
                                                        orch._init_services()

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
        with patch(
            "kiro_crew.dashboard.handlers._do_update_check", new_callable=AsyncMock
        ):
            with patch(
                "kiro_crew.dashboard.handlers._update_info", {"available": False}
            ):
                await orch._check_for_updates()

    @pytest.mark.asyncio
    async def test_update_available_no_auto(self):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch._auto_apply_update = AsyncMock()
        import kiro_crew.dashboard.handlers as _h
        orig = _h._update_info.copy()
        # Create a config with auto_update=False
        fake_cfg = MagicMock()
        fake_cfg.auto_update = False
        try:
            _h._update_info.update({"available": True, "version": "9.9.9"})
            with patch.object(_h, "_do_update_check", new_callable=AsyncMock):
                with patch("kiro_crew.config.KiroCrewConfig.load", return_value=fake_cfg):
                    await orch._check_for_updates()
        finally:
            _h._update_info.clear()
            _h._update_info.update(orig)
        orch._auto_apply_update.assert_not_awaited()
        ds.push_refresh.assert_called_with("update_available")

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
            # A git checkout (self_updatable) below the floor: the git auto-apply
            # is the correct mandatory action. `_do_update_check` sets this key
            # per layout in the real flow; it is mocked here, so the fixture
            # states the layout explicitly. The wheel layout (self_updatable
            # False) takes the notify path instead — see
            # TestMandatoryUpdateOnWheelInstall.
            _h._update_info.update({"available": False, "self_updatable": True})
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
        with patch.dict(
            "os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}, clear=False
        ):
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
            orch._check_missing_deps()  # should not raise

    def test_check_missing_deps_brazil_skips(self):
        orch = _make_orchestrator()
        with patch("importlib.util.find_spec", return_value=None):
            with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/proj"}):
                with patch.object(
                    GatewayOrchestrator, "_is_brazil_install", return_value=True
                ):
                    orch._check_missing_deps()  # should not raise, skips pip


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
            with patch("kiro_crew.slack.gateway.build_cron_session_context", return_value=("cron:j1", "run task")):
                result = await callback(job)

        assert result == "cron result"
        assert job.last_result == "cron result"

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
            with patch("kiro_crew.slack.gateway.build_cron_session_context", return_value=("cron:j2", "run")):
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
            with patch("kiro_crew.slack.gateway.build_cron_session_context", return_value=("cron:j3", "run")):
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
        await on_event("subagent_spawn", SubagentInfo(id="a2", task="t", parent_session_key="dashboard:s1"), {})
        await on_event("subagent_done", info, {"elapsed": 1.0})
        assert orch.dashboard_state.push_slots_update.call_count == 0  # debounced, not yet flushed
        await asyncio.sleep(0.3)
        assert orch.dashboard_state.push_slots_update.call_count == 1

        # A later lifecycle event schedules a fresh push.
        await on_event("subagent_done", SubagentInfo(id="a2", task="t", parent_session_key="dashboard:s1"), {"elapsed": 1.0})
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
        with patch(
            "kiro_crew.mcp_discovery.list_servers", side_effect=RuntimeError("fail")
        ):
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
        job._acp_retried = False

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            with patch("kiro_crew.slack.gateway.build_cron_session_context", return_value=("cron:jfail", "run")):
                with patch("kiro_crew.sel.sel") as mock_sel:
                    mock_sel.return_value.log_tool_invocation = MagicMock()
                    with pytest.raises(RuntimeError, match="boom"):
                        await callback(job)

        orch.slack.post_message.assert_awaited()
        assert job.consecutive_failures == 1

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
        job._acp_retried = False

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            with patch("kiro_crew.slack.gateway.build_cron_session_context", return_value=("cron:jfail2", "run")):
                with patch("kiro_crew.sel.sel") as mock_sel:
                    mock_sel.return_value.log_tool_invocation = MagicMock()
                    with pytest.raises(RuntimeError, match="boom"):
                        await callback(job)

        # Slack should NOT be called (suppressed)
        orch.slack.post_message.assert_not_awaited()
        assert job.consecutive_failures == 2

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
            with patch("kiro_crew.slack.gateway.build_cron_session_context", return_value=("cron:jmulti", "run")):
                result = await callback(job)

        assert result == "agent result"
        assert job.last_result == "agent result"
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
            with patch.object(
                GatewayOrchestrator, "run", new_callable=AsyncMock
            ) as mock_run:
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


class TestAutoApplyUpdateGitPath:
    """Git-based auto-update (non-toolbox)."""

    @pytest.mark.asyncio
    async def test_fetch_fails_returns_early(self):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds

        # branch detection succeeds, fetch fails
        call_count = [0]

        async def _fake_exec(*args, **kwargs):
            call_count[0] += 1
            proc = AsyncMock()
            if call_count[0] == 1:
                # branch detection
                proc.communicate = AsyncMock(return_value=(b"mainline\n", b""))
                proc.returncode = 0
            else:
                # fetch fails
                proc.communicate = AsyncMock(return_value=(b"", b"error"))
                proc.returncode = 1
            proc.wait = AsyncMock(return_value=proc.returncode)
            return proc

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

        call_count = [0]

        async def _fake_exec(*args, **kwargs):
            call_count[0] += 1
            proc = AsyncMock()
            if call_count[0] == 1:
                # branch detection
                proc.communicate = AsyncMock(return_value=(b"mainline\n", b""))
                proc.returncode = 0
            elif call_count[0] == 2:
                # fetch succeeds
                proc.communicate = AsyncMock(return_value=(b"", b""))
                proc.returncode = 0
            else:
                # diff --quiet returns 0 (no diff)
                proc.returncode = 0
            proc.wait = AsyncMock(return_value=proc.returncode)
            return proc

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
        orch._init_services = MagicMock()
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
                                with patch("kiro_crew.dashboard.handlers._bg_mcp_probe", new_callable=AsyncMock):
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
    async def test_run_no_dashboard_uses_api_server(self):
        """--no-dashboard uses _init_api_server."""
        import kiro_crew

        orch = _make_orchestrator(no_dashboard=True)

        orch._init_services = MagicMock()
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
                                with patch("kiro_crew.dashboard.handlers._bg_mcp_probe", new_callable=AsyncMock):
                                    with patch("os._exit"):
                                        with patch("resource.getrlimit", return_value=(256, 10240)):
                                            with patch("resource.setrlimit"):
                                                await orch.run()
        finally:
            kiro_crew.shutdown_event.clear()

        orch._init_dashboard.assert_not_awaited()
        orch._init_api_server.assert_awaited_once()


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
            with patch("kiro_crew.slack.gateway.build_cron_session_context", return_value=("cron:j_remind", "run")):
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

        with patch("kiro_crew.dashboard.chat_runner._run_chat", new_callable=AsyncMock, return_value=None):
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

    @pytest.mark.asyncio
    async def test_venv_update_full_path(self):
        """Full venv update: fetch, diff, reset, pip install, restart."""
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        call_count = [0]

        async def _fake_exec(*args, **kwargs):
            call_count[0] += 1
            proc = AsyncMock()
            proc.kill = MagicMock()
            if call_count[0] == 1:
                # branch detection → mainline
                proc.communicate = AsyncMock(return_value=(b"mainline\n", b""))
                proc.returncode = 0
            elif call_count[0] == 2:
                # fetch
                proc.communicate = AsyncMock(return_value=(b"", b""))
                proc.returncode = 0
            elif call_count[0] == 3:
                # diff --quiet → has changes (rc=1)
                proc.returncode = 1
            elif call_count[0] == 4:
                # git status --porcelain → clean
                proc.communicate = AsyncMock(return_value=(b"", b""))
                proc.returncode = 0
            elif call_count[0] == 5:
                # git reset --hard
                proc.returncode = 0
            elif call_count[0] == 6:
                # kiro-cli update
                proc.returncode = 0
            elif call_count[0] == 7:
                # pip install -e .
                proc.communicate = AsyncMock(return_value=(b"", b""))
                proc.returncode = 0
            else:
                # extension installs, provider update
                proc.returncode = 0
            proc.wait = AsyncMock(return_value=proc.returncode)
            if not hasattr(proc, 'communicate'):
                proc.communicate = AsyncMock(return_value=(b"", b""))
            return proc

        with patch("kiro_crew.env.is_toolbox_install", return_value=False):
            with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}):
                with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                    with patch.object(
                        GatewayOrchestrator, "_is_brazil_install", return_value=False
                    ):
                        with patch("kiro_crew.slack.gateway.build_frontend_async", new_callable=AsyncMock):
                            with patch("os.execv", side_effect=OSError("test")):
                                with patch("shutil.which", return_value=None):
                                    await orch._auto_apply_update()

        ds.push_update_progress.assert_any_call("pulling", "Fetching latest changes…")
        ds.push_update_progress.assert_any_call("building", "Building frontend…")


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
        with patch("kiro_crew.slack.gateway.model_file_present", return_value=True), \
             patch("kiro_crew.slack.gateway.make_sync_embed_fn",
                   return_value=fake_embed_fn) as mock_make, \
             patch("kiro_crew.slack.gateway.start_background_model_download",
                   return_value=None) as mock_start:
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
        with patch("kiro_crew.slack.gateway.model_file_present", return_value=False), \
             patch("kiro_crew.slack.gateway.start_background_model_download",
                   return_value=fake_task) as mock_start:
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
        return MagicMock(wait_ready=MagicMock(return_value=True), is_ready=MagicMock(return_value=True))

    @pytest.mark.asyncio
    async def test_migrates_when_not_migrated_and_legacy_present(self):
        orch, store = self._orch_with_store(migrated=False)
        set_migrated = AsyncMock()
        with patch("kiro_crew.slack.gateway.model_file_present", return_value=True), patch(
            "kiro_crew.slack.gateway.make_sync_embed_fn", return_value=(lambda t: [0.1])
        ), patch(
            "kiro_crew.slack.gateway.get_shared_embedder", return_value=self._ready_embedder()
        ), patch(
            "kiro_crew.memory.legacy_memory_present", return_value=True
        ), patch.object(
            orch, "_set_memory_migrated", set_migrated
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
        with patch("kiro_crew.slack.gateway.model_file_present", return_value=True), patch(
            "kiro_crew.slack.gateway.make_sync_embed_fn", return_value=(lambda t: [0.1])
        ), patch(
            "kiro_crew.slack.gateway.get_shared_embedder", return_value=self._ready_embedder()
        ), patch(
            "kiro_crew.memory.legacy_memory_present", return_value=True
        ), patch.object(
            orch, "_set_memory_migrated", set_migrated
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
        with patch("kiro_crew.slack.gateway.model_file_present", return_value=True), patch(
            "kiro_crew.slack.gateway.make_sync_embed_fn", return_value=(lambda t: [0.1])
        ), patch(
            "kiro_crew.slack.gateway.get_shared_embedder", return_value=not_ready
        ), patch(
            "kiro_crew.memory.legacy_memory_present", return_value=True
        ), patch.object(
            orch, "_set_memory_migrated", AsyncMock()
        ):
            await orch._auto_migrate_memory()
        not_ready.wait_ready.assert_called_once()
        store.backfill_missing_embeddings.assert_not_called()

    @pytest.mark.asyncio
    async def test_fresh_install_no_legacy_still_flips_migrated(self):
        orch, store = self._orch_with_store(migrated=False)
        set_migrated = AsyncMock()
        with patch("kiro_crew.slack.gateway.model_file_present", return_value=True), patch(
            "kiro_crew.slack.gateway.make_sync_embed_fn", return_value=(lambda t: [0.1])
        ), patch(
            "kiro_crew.memory.legacy_memory_present", return_value=False
        ), patch.object(
            orch, "_set_memory_migrated", set_migrated
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
        with patch(
            "kiro_crew.slack.gateway.model_file_present",
            side_effect=lambda: next(presence, True),
        ), patch(
            "kiro_crew.slack.gateway.make_sync_embed_fn", return_value=(lambda t: [0.1])
        ), patch(
            "kiro_crew.slack.gateway.get_shared_embedder", return_value=self._ready_embedder()
        ), patch(
            "kiro_crew.memory.legacy_memory_present", return_value=True
        ), patch.object(
            orch, "_set_memory_migrated", AsyncMock()
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
        with patch("kiro_crew.slack.gateway.model_file_present", return_value=True), patch(
            "kiro_crew.slack.gateway.make_sync_embed_fn", return_value=(lambda t: [0.1])
        ), patch(
            "kiro_crew.memory.legacy_memory_present", return_value=True
        ), patch.object(
            orch, "_set_memory_migrated", set_migrated
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


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _auto_apply_update discards local edits before staging frontend
# ═══════════════════════════════════════════════════════════════════════════


class TestAutoApplyUpdateResetPath:
    """Public auto-update reset path: discards local edits, builds frontend, pips."""

    @pytest.mark.asyncio
    async def test_reset_then_frontend_then_pip(self):
        """Local tracked edits are reset, then frontend build + pip install run.

        Public OSS flow (no Brazil ws sync / toolbox / AIM): branch → fetch →
        diff → status → reset → [kiro-cli optional] → build frontend → pip.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        call_count = [0]

        async def _fake_exec(*args, **kwargs):
            call_count[0] += 1
            proc = AsyncMock()
            proc.kill = MagicMock()
            if call_count[0] == 1:
                # branch detection → mainline
                proc.communicate = AsyncMock(return_value=(b"mainline\n", b""))
                proc.returncode = 0
            elif call_count[0] == 2:
                # fetch
                proc.communicate = AsyncMock(return_value=(b"", b""))
                proc.returncode = 0
            elif call_count[0] == 3:
                # diff --quiet → has changes (rc=1)
                proc.returncode = 1
            elif call_count[0] == 4:
                # git status --porcelain → has tracked changes
                proc.communicate = AsyncMock(return_value=(b" M file.py\n", b""))
                proc.returncode = 0
            elif call_count[0] == 5:
                # git reset --hard
                proc.returncode = 0
            else:
                # pip install -e . (kiro-cli skipped via shutil.which=None)
                proc.communicate = AsyncMock(return_value=(b"", b""))
                proc.returncode = 0
            proc.wait = AsyncMock(return_value=proc.returncode)
            if not hasattr(proc, 'communicate'):
                proc.communicate = AsyncMock(return_value=(b"", b""))
            return proc

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
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
            with patch("kiro_crew.slack.gateway.build_cron_session_context", return_value=("cron:jacp", "run")):
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
        """Fire with missing slot → removes loop."""
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
            with patch("kiro_crew.slack.gateway.build_cron_session_context", return_value=("cron:jack", "run")):
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

        orch._init_services = MagicMock()
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
                                        with patch("kiro_crew.dashboard.handlers._bg_mcp_probe", new_callable=AsyncMock):
                                            with patch("os._exit"):
                                                with patch("resource.getrlimit", return_value=(256, 10240)):
                                                    with patch("resource.setrlimit"):
                                                        await orch.run()
        finally:
            pass

        orch._start_embeddings.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _check_missing_deps pip install path
# ═══════════════════════════════════════════════════════════════════════════


class TestBgSessionDashboardBranch:
    """run() -> _start_bg_session dashboard URL printing path."""

    @pytest.mark.asyncio
    async def test_bg_session_prints_dashboard_url(self):
        """_start_bg_session prints dashboard URLs when not _no_dashboard."""
        orch = _make_orchestrator(no_dashboard=False, no_open=True)

        orch._init_services = MagicMock()
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
                    with patch("kiro_crew.slack.gateway.resolve_dashboard_host",
                               return_value="127.0.0.1"):
                        with patch("kiro_crew.slack.gateway.build_dashboard_url",
                                   return_value="http://127.0.0.1:6779/?t=tok"):
                            with patch("kiro_crew.slack.gateway.format_dashboard_urls",
                                       return_value=["url-line-1", "url-line-2"]):
                                with patch("kiro_crew.slack.events.init_socket_mode"):
                                    with patch("kiro_crew.slack.interactions.init"):
                                        with patch("kiro_crew.slack.events.SeenCache"):
                                            with patch("kiro_crew.session.cleanup_orphaned_sessions"):
                                                with patch("kiro_crew.dashboard.handlers._bg_mcp_probe", new_callable=AsyncMock):
                                                    with patch("os._exit"):
                                                        with patch("resource.getrlimit", return_value=(256, 10240)):
                                                            with patch("resource.setrlimit"):
                                                                await orch.run()
                                                                # Let bg_session task drain
                                                                await asyncio.sleep(0)
                                                                await asyncio.sleep(0)

        orch.sessions.start_pool.assert_awaited_once_with(blocking=False)


class TestCheckMissingDepsPip:
    """Dep repair via pip install."""

    def test_pip_install_on_missing_dep(self):
        orch = _make_orchestrator()
        with patch("importlib.util.find_spec", return_value=None):
            with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/proj"}):
                with patch.object(
                    GatewayOrchestrator, "_is_brazil_install", return_value=False
                ):
                    with patch("subprocess.run") as mock_run:
                        mock_run.return_value = MagicMock(returncode=0)
                        orch._check_missing_deps()
                    mock_run.assert_called_once()

    def test_pip_install_failure(self):
        orch = _make_orchestrator()
        with patch("importlib.util.find_spec", return_value=None):
            with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/proj"}):
                with patch.object(
                    GatewayOrchestrator, "_is_brazil_install", return_value=False
                ):
                    with patch("subprocess.run") as mock_run:
                        mock_run.return_value = MagicMock(
                            returncode=1, stderr=b"error"
                        )
                        orch._check_missing_deps()  # should not raise


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
            with patch("kiro_crew.slack.gateway.build_cron_session_context", return_value=("cron:jslack", "run")):
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

        posted = await orch._deliver_cron_response(
            "cron:job1", "pick one\n\n[OPTIONS: Yes | No]"
        )

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
        return info

    @pytest.mark.asyncio
    async def test_slack_subagent_persists_to_conversation_log(self):
        """Successful Slack injection persists both announce and response."""
        orch, mock_sm = self._setup()
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info()

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            return_value="synthesized response",
        ), patch(
            "kiro_crew.slack.gateway.is_thread_temporary", return_value=False
        ), patch(
            "kiro_crew.slack.gateway.is_thread_incognito", return_value=False
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

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            return_value=leaked,
        ), patch(
            "kiro_crew.slack.gateway.is_thread_temporary", return_value=False
        ), patch(
            "kiro_crew.slack.gateway.is_thread_incognito", return_value=False
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

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            return_value="response",
        ), patch(
            "kiro_crew.slack.gateway.is_thread_temporary", return_value=True
        ), patch(
            "kiro_crew.slack.gateway.is_thread_incognito", return_value=False
        ):
            await on_done(info)

        orch.conv_log.append.assert_not_called()

    @pytest.mark.asyncio
    async def test_slack_subagent_skips_persistence_for_incognito_thread(self):
        """Incognito threads should not be persisted."""
        orch, mock_sm = self._setup()
        on_done = mock_sm.call_args[1]["on_done"]
        info = self._make_info()

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            return_value="response",
        ), patch(
            "kiro_crew.slack.gateway.is_thread_temporary", return_value=False
        ), patch(
            "kiro_crew.slack.gateway.is_thread_incognito", return_value=True
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

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            return_value="response",
        ), patch(
            "kiro_crew.slack.gateway.is_thread_temporary", return_value=False
        ), patch(
            "kiro_crew.slack.gateway.is_thread_incognito", return_value=False
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

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            return_value="synthesized response",
        ), patch(
            "kiro_crew.slack.gateway.is_thread_temporary", return_value=False
        ), patch(
            "kiro_crew.slack.gateway.is_thread_incognito", return_value=False
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

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            side_effect=[asyncio.TimeoutError(), "response text"],
        ), patch(
            "kiro_crew.slack.gateway.is_thread_temporary", return_value=False
        ), patch(
            "kiro_crew.slack.gateway.is_thread_incognito", return_value=False
        ), patch(
            "asyncio.sleep", new_callable=AsyncMock
        ):
            await on_done(info)

        # Exactly ONE completion persisted (2 appends: user + assistant), not 4.
        assert orch.conv_log.append.call_count == 2


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
            _provider(True), _provider(False), _provider(True)
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
        state.sessions.active_providers.return_value = [
            no_attr, raising, _provider(True)
        ]
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
            ChannelDescriptor(channel_type=name, start=mock)
            for name, mock in mocks.items()
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


class TestMandatoryUpdateOnWheelInstall:
    """A policy min-version makes an update mandatory. On a wheel/cli.sh install
    the git-based auto-apply cannot run, so the mandatory branch must NOTIFY
    (warn + light the dashboard badge) instead of silently returning — which is
    what it did before, leaving the host below the floor with no signal."""

    @pytest.mark.asyncio
    async def test_mandatory_update_on_wheel_notifies_not_silent(self, monkeypatch):
        import kiro_crew.dashboard.handlers as handlers
        import kiro_crew.platform.update_governance as gov

        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds

        async def _noop_check():
            return None

        # Wheel install below a policy floor: a feed-checkable layout reports
        # self_updatable False, and the check can leave available False (a
        # pre-release remote reads as not-newer) even though the floor mandates
        # the update. Start from available False to prove the branch lights it.
        handlers._update_info.clear()
        handlers._update_info.update(
            {
                "available": False,
                "self_updatable": False,
                "install_kind": "wheel",
                # A feed-checkable wheel carries an installer command; that is
                # what distinguishes it from an externally-managed install.
                "update_command": "curl -fsSL … | sh",
            }
        )
        monkeypatch.setattr(handlers, "_do_update_check", _noop_check)
        monkeypatch.setattr(gov, "update_required", lambda _v: True)
        monkeypatch.setattr(gov, "min_version", lambda: "9.9.9")

        apply_called = AsyncMock()
        monkeypatch.setattr(orch, "_auto_apply_update", apply_called)

        await orch._check_for_updates()

        # Must NOT attempt the git apply on a non-git tree, and must surface it.
        apply_called.assert_not_awaited()
        ds.push_refresh.assert_called_once_with("update_available")
        # The dashboard badge reads _update_info["available"]; a mandatory
        # update must light it even though the check left it False.
        assert handlers._update_info.get("available") is True

    @pytest.mark.asyncio
    async def test_mandatory_update_on_externally_managed_does_not_badge(self, monkeypatch):
        """A dmg/appimage/docker install below the floor is not self_updatable
        AND has no installer update_command — it updates via its own surface, so
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
                "available": False,
                "self_updatable": False,
                "install_kind": "docker",
                "update_command": "",  # externally managed: no CLI update path
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

        # Git checkout: self_updatable True, so the mandatory git apply runs.
        handlers._update_info.clear()
        handlers._update_info.update(
            {"available": True, "self_updatable": True, "install_kind": "git"}
        )
        monkeypatch.setattr(handlers, "_do_update_check", _noop_check)
        monkeypatch.setattr(gov, "update_required", lambda _v: True)
        monkeypatch.setattr(gov, "min_version", lambda: "9.9.9")

        apply_called = AsyncMock()
        monkeypatch.setattr(orch, "_auto_apply_update", apply_called)

        await orch._check_for_updates()
        apply_called.assert_awaited_once()
