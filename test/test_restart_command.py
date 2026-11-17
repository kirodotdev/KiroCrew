"""Tests for /kirocrew restart slash command."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from real_dashboard_state import _settle_real_dashboard_states  # noqa: F401
from real_dashboard_state import drain, open_feed, real_dashboard_state

from kiro_crew.config.loader import ACTIVATION_ALWAYS, ChannelConfig, KiroCrewConfig
from kiro_crew.sel import SecurityEventLog
from kiro_crew.slack.events import SeenCache, _route_message
from kiro_crew.slack.handler import set_owner_id


@pytest.fixture(autouse=True)
def _mock_sel():
    _sel = MagicMock()
    with patch("kiro_crew.slack.events.sel", return_value=_sel):
        yield _sel


@pytest.fixture
def restart_audit(tmp_path, monkeypatch):
    """A real synchronous SEL in tmp_path; returns a reader of its restart rows."""
    monkeypatch.setattr(SecurityEventLog, "_instance", None)
    monkeypatch.setattr(SecurityEventLog, "_initialized", False)
    real_sel = SecurityEventLog(base_dir=tmp_path, sync=True)
    monkeypatch.setattr("kiro_crew.slack.events.sel", lambda: real_sel)
    path = tmp_path / "security_events.jsonl"

    def _rows() -> list[tuple[str, str]]:
        if not path.exists():
            return []
        events = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return [
            (e["outcome"], e["resources"])
            for e in events
            if e.get("operation") == "/kirocrew restart"
        ]

    return _rows


def _make_orch(
    channels: dict[str, ChannelConfig] | None = None,
    dm_activation: str = ACTIVATION_ALWAYS,
) -> MagicMock:
    """Build a minimal mock GatewayOrchestrator (mirrors test_channel_activation)."""
    orch = MagicMock()
    cfg = KiroCrewConfig(
        slack_channels=channels or {},
        slack_dm_activation=dm_activation,
    )
    orch._cfg = cfg
    orch.channel_history = MagicMock()
    orch.slack = AsyncMock()
    orch.sessions = AsyncMock()
    orch._handler_tasks = set()
    orch._session_tasks = {}
    orch._pending_queue = {}
    return orch


class TestRestartSlashCommand:
    @pytest.mark.asyncio
    async def test_non_owner_denied(self, restart_audit):
        from kiro_crew.slack.events import _handle_restart

        set_owner_id("U_OWNER")
        orch = MagicMock()
        respond = AsyncMock()
        await _handle_restart(orch, "U_OTHER", "", respond)
        respond.assert_called_once()
        assert "Only the owner" in respond.call_args[0][0]
        assert restart_audit() == [("denied", "user=U_OTHER")]

    @pytest.mark.asyncio
    async def test_no_supervisor_denied(self, restart_audit, monkeypatch):
        from kiro_crew.slack.events import _handle_restart

        set_owner_id("U_OWNER")
        orch = MagicMock()
        respond = AsyncMock()
        monkeypatch.delenv("INVOCATION_ID", raising=False)
        await _handle_restart(orch, "U_OWNER", "", respond)
        assert "supervisor" in respond.call_args[0][0]
        assert restart_audit() == [("denied", "user=U_OWNER,reason=no_supervisor")]

    @pytest.mark.asyncio
    async def test_owner_with_supervisor_exits(self, _mock_sel, restart_audit, monkeypatch):
        from kiro_crew.slack.events import _handle_restart

        set_owner_id("U_OWNER")
        orch = MagicMock()
        orch.sessions = MagicMock()
        orch.sessions.close_all = AsyncMock()
        orch.dashboard_state = None
        respond = AsyncMock()
        monkeypatch.setenv("INVOCATION_ID", "abc")
        with (
            patch("os._exit") as mock_exit,
            patch(
                "kiro_crew.sel.flush_audit_queue_before_hard_exit", new_callable=AsyncMock
            ) as mock_flush_sel,
        ):
            await _handle_restart(orch, "U_OWNER", "", respond)
        mock_exit.assert_called_once_with(1)
        respond.assert_called_with("♻️ Restarting gateway…")
        orch.sessions.close_all.assert_awaited_once()
        # The approved-restart audit event must be drained before os._exit()
        # (which skips atexit / the async SEL writer drain). The restart path
        # routes that drain through the shared flush_audit_queue_before_hard_exit
        # helper, same as the gateway's other hard exits.
        mock_flush_sel.assert_awaited_once_with()
        assert restart_audit() == [("approved", "user=U_OWNER")]

    @pytest.mark.asyncio
    async def test_owner_restart_marks_the_clean_exit_before_it_exits(
        self, _mock_sel, restart_audit, monkeypatch
    ):
        """An owner-asked restart is orderly: its crash dump must not read as unclean."""
        from kiro_crew.dashboard import crash_dump_store
        from kiro_crew.slack.events import _handle_restart

        set_owner_id("U_OWNER")
        orch = MagicMock()
        orch.sessions = MagicMock()
        orch.sessions.close_all = AsyncMock()
        orch.dashboard_state = None
        monkeypatch.setenv("INVOCATION_ID", "abc")
        order: list[str] = []
        monkeypatch.setattr(
            crash_dump_store, "record_clean_exit", lambda *_: order.append("marked")
        )
        with (
            patch("os._exit", side_effect=lambda _code: order.append("exit")),
            patch("kiro_crew.sel.flush_audit_queue_before_hard_exit", new_callable=AsyncMock),
        ):
            await _handle_restart(orch, "U_OWNER", "", AsyncMock())
        assert order == ["marked", "exit"]

    @pytest.mark.asyncio
    async def test_owner_exits_even_if_session_close_hangs(self, _mock_sel):
        """A hung session close must not block the restart: close_all is bounded
        by the restart's session-close cap, and a timeout still proceeds to
        flush + os._exit."""
        from kiro_crew.slack.events import _handle_restart

        set_owner_id("U_OWNER")
        orch = MagicMock()
        orch.sessions = MagicMock()
        never = asyncio.Event()

        async def _never_returns(**_kwargs):
            await never.wait()

        orch.sessions.close_all = AsyncMock(side_effect=_never_returns)
        orch.dashboard_state = None
        respond = AsyncMock()

        with (
            patch.dict("os.environ", {"INVOCATION_ID": "abc"}),
            patch("os._exit") as mock_exit,
            patch("kiro_crew.slack.events._RESTART_SESSION_CLOSE_SECS", 0.05),
            patch(
                "kiro_crew.sel.flush_audit_queue_before_hard_exit", new_callable=AsyncMock
            ) as mock_flush_sel,
        ):
            await asyncio.wait_for(_handle_restart(orch, "U_OWNER", "", respond), timeout=30)
        # Despite the hang, the process still exits and the audit is drained.
        mock_exit.assert_called_once_with(1)
        mock_flush_sel.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_dashboard_state_saved_before_restart(self, _mock_sel, tmp_path):
        """The pre-restart slot save (the critical data-preservation step) must
        run when a dashboard_state is present — exercise the save_all_slots_to_history
        path that the None-dashboard_state tests above skip."""
        from kiro_crew.slack.events import _handle_restart

        set_owner_id("U_OWNER")
        orch = MagicMock()
        orch.sessions = MagicMock()
        orch.sessions.close_all = AsyncMock()
        orch.dashboard_state = real_dashboard_state(tmp_path)
        feed = open_feed(orch.dashboard_state)
        respond = AsyncMock()
        with (
            patch.dict("os.environ", {"INVOCATION_ID": "abc"}),
            patch("os._exit") as mock_exit,
            patch("kiro_crew.dashboard.chat.save_all_slots_to_history") as mock_save,
        ):
            await _handle_restart(orch, "U_OWNER", "", respond)
        mock_save.assert_called_once_with(orch.dashboard_state)
        restarting = {"step": "restarting", "detail": "Restarting server…"}
        assert orch.dashboard_state._update_progress == restarting
        assert {"_type": "update_progress", **restarting} in drain(feed)
        mock_exit.assert_called_once_with(1)

    @pytest.mark.asyncio
    async def test_restart_proceeds_if_slot_save_raises(self, _mock_sel):
        """A failure in the slot save must NOT block the restart: the save is
        best-effort (try/except), so os._exit() is still reached."""
        from kiro_crew.slack.events import _handle_restart

        set_owner_id("U_OWNER")
        orch = MagicMock()
        orch.sessions = MagicMock()
        orch.sessions.close_all = AsyncMock()
        orch.dashboard_state = MagicMock()
        respond = AsyncMock()
        with (
            patch.dict("os.environ", {"INVOCATION_ID": "abc"}),
            patch("os._exit") as mock_exit,
            patch(
                "kiro_crew.dashboard.chat.save_all_slots_to_history",
                side_effect=RuntimeError("disk full"),
            ) as mock_save,
            patch(
                "kiro_crew.sel.flush_audit_queue_before_hard_exit", new_callable=AsyncMock
            ) as mock_flush_sel,
        ):
            await _handle_restart(orch, "U_OWNER", "", respond)
        mock_save.assert_called_once()
        # Save blew up, but the restart still completed.
        mock_exit.assert_called_once_with(1)
        mock_flush_sel.assert_awaited_once_with()


class TestRestartBangAlias:
    @pytest.mark.asyncio
    async def test_bang_restart_routes_to_handler(self):
        """`!restart` is intercepted in _route_message and delegated to
        _handle_restart — it must never reach the LLM session."""
        set_owner_id("U_OWNER")
        orch = _make_orch()
        seen = SeenCache()
        event = {
            "user": "U_OWNER",
            "channel": "D1234",
            "text": "!restart",
            "ts": "10.0",
            "team": "TTEST",
        }
        with (
            patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as mock_hm,
            patch("kiro_crew.slack.events._handle_restart", new_callable=AsyncMock) as mock_restart,
            patch("kiro_crew.slack.events.is_allowed_user", return_value=True),
            patch("kiro_crew.slack.enterprise.check_message_origin", return_value=True),
        ):
            await _route_message(orch, event, seen, is_mention=False)
            await asyncio.sleep(0)
            mock_restart.assert_awaited_once()
            # caller_id (sender) is the owner, args empty
            assert mock_restart.await_args[0][1] == "U_OWNER"
            assert mock_restart.await_args[0][2] == ""
            mock_hm.assert_not_called()

    @pytest.mark.asyncio
    async def test_bang_restart_non_owner_denied_end_to_end(self, restart_audit):
        """A non-owner issuing `!restart` must be denied through the bang path —
        the real _handle_restart runs (not mocked) and rejects on the owner
        check, the denial is posted to Slack, and the message never reaches the
        LLM (handle_message). Guards against a future refactor bypassing the
        owner check on the bang route."""
        set_owner_id("U_OWNER")
        orch = _make_orch()
        seen = SeenCache()
        event = {
            "user": "U_OTHER",
            "channel": "D1234",
            "text": "!restart",
            "ts": "10.0",
            "team": "TTEST",
        }
        with (
            patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as mock_hm,
            patch("kiro_crew.slack.events.is_allowed_user", return_value=True),
            patch("kiro_crew.slack.enterprise.check_message_origin", return_value=True),
            patch("os._exit") as mock_exit,
        ):
            await _route_message(orch, event, seen, is_mention=False)
            await asyncio.sleep(0)
        # Denial posted, process NOT exited, message NOT forwarded to the agent.
        mock_exit.assert_not_called()
        mock_hm.assert_not_called()
        orch.slack.post_message.assert_awaited_once()
        assert "Only the owner" in orch.slack.post_message.await_args[0][1]
        assert restart_audit() == [("denied", "user=U_OTHER")]

    @pytest.mark.asyncio
    async def test_bang_restart_in_alias_map(self):
        """`!restart` must be a recognized bang so linked-thread routing lets it
        fall through to command handling instead of swallowing it into a slot."""
        from kiro_crew.slack.handler import _BANG_TO_SLASH

        assert _BANG_TO_SLASH.get("!restart") == "/kirocrew restart"
