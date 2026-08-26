"""Coverage tests for :mod:`kiro_crew.slack.events`.

Focus areas that the existing Slack suites leave untouched:

* the built-in ``/kirocrew <sub-command>`` handlers (dashboard, agent, voice,
  yolo, config, users, channels) and their guard branches,
* :func:`init_socket_mode` plus the ``_on_event`` Socket Mode dispatcher it
  installs,
* the ``_handle_slash`` fallbacks (``@user`` / ``#channel`` / help),
* the pure Block Kit text-recovery helpers,
* the voice-transcription and ``message_deleted`` helpers,
* selected ``_route_message`` guard branches (activation=off passthrough,
  channels-governance denial, display-name fallback, ``!stop``).

Conventions mirror ``test_channel_activation.py`` and ``test_restart_command.py``:
a ``MagicMock`` stand-in for ``GatewayOrchestrator``, ``AsyncMock`` for the Slack
client, and ``kiro_crew.slack.events.sel`` patched so no audit file is written.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.config.loader import (
    ACTIVATION_ALWAYS,
    ACTIVATION_OBSERVE,
    ACTIVATION_OFF,
    ChannelConfig,
    KiroCrewConfig,
    MessagingConfig,
)
from kiro_crew.slack import events as ev

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mock_sel():
    """Keep every SEL audit write in memory (no filesystem side effects)."""
    fake = MagicMock()
    with patch("kiro_crew.slack.events.sel", return_value=fake):
        yield fake


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Point every home-derived path at ``tmp_path`` so nothing touches real HOME."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / ".kiro" / "crew"))
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / ".kiro"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path


def _make_orch(
    channels: dict[str, ChannelConfig] | None = None,
    dm_activation: str = ACTIVATION_ALWAYS,
    use_transport: bool = False,
) -> MagicMock:
    """Minimal mock ``GatewayOrchestrator`` (mirrors ``test_channel_activation``)."""
    orch = MagicMock()
    orch._cfg = KiroCrewConfig(
        slack_channels=channels or {},
        slack_dm_activation=dm_activation,
        messaging=MessagingConfig(use_transport=use_transport),
    )
    orch.slack_command = "kirocrew"
    orch._owner_id = "U_OWNER"
    orch._allowed_users = {"U_OWNER"}
    orch._tracking_channels = set()
    orch._open_channels = set()
    orch._approval_mode = ""
    orch.channel_history = MagicMock()
    orch.channel_history._user_names = {}
    orch.slack = AsyncMock()
    # Sync accessor on an AsyncMock would return an un-awaited coroutine.
    orch.slack.record_channel_team = MagicMock()
    # Same for the home-team resolver the inbound path awaits.
    orch.slack.ensure_channel_team = AsyncMock()
    # A bare AsyncMock's return_value is itself an AsyncMock, so `info.get(...)`
    # in the display-name lookup would hand back a coroutine. Return a real dict.
    orch.slack.get_user_info = AsyncMock(return_value={})
    orch.sessions = AsyncMock()
    orch.sessions.enqueue = MagicMock(return_value=False)
    orch.sessions.is_busy = MagicMock(return_value=False)
    orch.sessions.is_cancelled = MagicMock(return_value=False)
    orch.sessions.has_session = MagicMock(return_value=False)
    orch.sessions.get_session_for_thread = MagicMock(return_value=None)
    orch.sessions.dequeue = MagicMock(return_value=None)
    orch.sessions.clear_queue = MagicMock()
    orch.sessions.cancel_queued = MagicMock(return_value=False)
    orch.ctx_builder = None
    orch.cron_svc = None
    orch.conv_log = None
    orch.consolidator = None
    orch.subagent_mgr = None
    orch.task_runner = None
    orch.dashboard_state = None
    orch._handler_tasks = set()
    orch._session_tasks = {}
    orch._pending_queue = {}
    return orch


async def _drain(orch: MagicMock) -> None:
    """Let fire-and-forget handler tasks run to completion."""
    for _ in range(3):
        await asyncio.sleep(0)
    tasks = list(orch._handler_tasks) + list(ev._bg_tasks)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# ---------------------------------------------------------------------------
# SeenCache / background-task bookkeeping
# ---------------------------------------------------------------------------


class TestSeenCache:
    def test_check_and_add_reports_repeat(self):
        cache = ev.SeenCache()
        assert cache.check_and_add("e1") is False
        assert cache.check_and_add("e1") is True

    def test_check_does_not_mark(self):
        cache = ev.SeenCache()
        assert cache.check("e1") is False
        assert cache.check_and_add("e1") is False

    def test_add_is_idempotent_and_bounded(self):
        cache = ev.SeenCache(maxlen=2)
        cache.add("a")
        cache.add("a")
        cache.add("b")
        cache.add("c")
        # "a" was evicted (LRU by insertion order), "b"/"c" retained.
        assert cache.check("a") is False
        assert cache.check("b") is True
        assert cache.check("c") is True

    def test_check_and_add_evicts_oldest(self):
        cache = ev.SeenCache(maxlen=2)
        cache.check_and_add("a")
        cache.check_and_add("b")
        cache.check_and_add("c")
        assert cache.check("a") is False
        assert cache.check("c") is True


class TestSpawnTracked:
    @pytest.mark.asyncio
    async def test_success_discards_reference(self):
        async def _work() -> None:
            return None

        task = ev._spawn_tracked(_work())
        assert task in ev._bg_tasks
        await task
        await asyncio.sleep(0)
        assert task not in ev._bg_tasks

    @pytest.mark.asyncio
    async def test_failure_is_logged_not_raised(self, caplog):
        async def _boom() -> None:
            raise RuntimeError("respond failed")

        with caplog.at_level("DEBUG", logger="kiro_crew.slack.events"):
            task = ev._spawn_tracked(_boom())
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0)
        assert task not in ev._bg_tasks
        assert any("Tracked slash task failed" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_cancelled_task_short_circuits(self):
        started = asyncio.Event()

        async def _slow() -> None:
            started.set()
            await asyncio.sleep(10)

        task = ev._spawn_tracked(_slow())
        await started.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
        assert task not in ev._bg_tasks


class TestBuildHelpText:
    def test_lists_registered_subcommands_and_channel_hint(self):
        text = ev._build_help_text("kirocrew")
        assert text.startswith("*Available commands:*")
        assert "`/kirocrew dashboard`" in text
        assert "`/kirocrew #channel`" in text

    def test_honours_custom_command_name(self):
        assert "`/crew status`" in ev._build_help_text("crew")

    def test_description_less_command_renders_bare(self):
        ev.register_slash_command("zzcovtmp", AsyncMock(), "")
        try:
            line = "• `/kirocrew zzcovtmp`"
            assert line in ev._build_help_text("kirocrew")
        finally:
            ev.SLASH_REGISTRY.pop("zzcovtmp", None)


# ---------------------------------------------------------------------------
# /kirocrew dashboard
# ---------------------------------------------------------------------------


class TestHandleDashboard:
    @pytest.mark.asyncio
    async def test_unparseable_duration_returns_usage(self):
        orch = _make_orch()
        respond = AsyncMock()
        await ev._handle_dashboard(orch, "U_OWNER", "banana", respond)
        assert "Usage:" in respond.call_args[0][0]

    @pytest.mark.asyncio
    async def test_default_ttl_sends_link(self):
        orch = _make_orch()
        respond = AsyncMock()
        with patch(
            "kiro_crew.slack.events.send_dashboard_link",
            new_callable=AsyncMock,
            return_value="https://example.invalid/d?t=1",
        ) as send:
            await ev._handle_dashboard(orch, "U_OWNER", "", respond)
        assert send.await_args[0][2] == 3600
        assert "Dashboard link sent" in respond.call_args[0][0]
        assert respond.call_args.kwargs["blocks"]

    @pytest.mark.asyncio
    async def test_explicit_duration_is_capped_to_max_session_ttl(self):
        orch = _make_orch()
        respond = AsyncMock()
        with patch(
            "kiro_crew.slack.events.send_dashboard_link",
            new_callable=AsyncMock,
            return_value="https://example.invalid/d",
        ) as send:
            await ev._handle_dashboard(orch, "U_OWNER", "9999h extra", respond)
        assert send.await_args[0][2] == ev.MAX_SESSION_TTL_SECS

    @pytest.mark.asyncio
    async def test_link_failure_reports_error(self):
        orch = _make_orch()
        respond = AsyncMock()
        with patch(
            "kiro_crew.slack.events.send_dashboard_link",
            new_callable=AsyncMock,
            return_value="",
        ):
            await ev._handle_dashboard(orch, "U_OWNER", "", respond)
        assert "Failed to send dashboard link" in respond.call_args[0][0]


# ---------------------------------------------------------------------------
# /kirocrew agent
# ---------------------------------------------------------------------------


class TestHandleAgent:
    @pytest.mark.asyncio
    async def test_non_owner_denied(self):
        respond = AsyncMock()
        with patch("kiro_crew.slack.handler.is_owner", return_value=False):
            await ev._handle_agent(_make_orch(), "U_OTHER", "", respond)
        assert "Only the owner" in respond.call_args[0][0]

    @pytest.mark.asyncio
    async def test_off_resets_default_agent(self):
        respond = AsyncMock()
        with patch("kiro_crew.slack.handler.is_owner", return_value=True):
            with patch("kiro_crew.slack.handler._set_default_agent") as setter:
                await ev._handle_agent(_make_orch(), "U_OWNER", "off", respond)
        setter.assert_called_once_with("")
        assert "Reset to default agent" in respond.call_args[0][0]

    @pytest.mark.asyncio
    async def test_known_agent_switches(self):
        respond = AsyncMock()
        with patch("kiro_crew.slack.handler.is_owner", return_value=True):
            with patch("kiro_crew.slack.handler._resolve_agent_name", return_value="scout"):
                with patch("kiro_crew.slack.handler._set_default_agent") as setter:
                    await ev._handle_agent(_make_orch(), "U_OWNER", "Scout", respond)
        setter.assert_called_once_with("scout")
        assert "Switched to agent" in respond.call_args[0][0]

    @pytest.mark.asyncio
    async def test_unknown_agent_falls_through_to_selector(self, _isolated_home):
        agents = _isolated_home / ".kiro" / "agents"
        agents.mkdir(parents=True)
        (agents / "scout.json").write_text(json.dumps({"name": "scout"}))
        respond = AsyncMock()
        with patch("kiro_crew.slack.handler.is_owner", return_value=True):
            with patch("kiro_crew.slack.handler._resolve_agent_name", return_value=""):
                with patch("kiro_crew.slack.handler._get_default_agent", return_value="scout"):
                    await ev._handle_agent(_make_orch(), "U_OWNER", "ghost", respond)
        assert respond.await_count == 2
        assert "Unknown agent" in respond.await_args_list[0][0][0]
        blocks = respond.await_args_list[1].kwargs["blocks"]
        accessory = blocks[0]["accessory"]
        assert accessory["action_id"] == "mc_agent_select"
        assert [o["value"] for o in accessory["options"]] == ["scout", "off"]
        assert accessory["initial_option"]["value"] == "scout"

    @pytest.mark.asyncio
    async def test_selector_defaults_to_off_when_no_agents_dir(self):
        respond = AsyncMock()
        with patch("kiro_crew.slack.handler.is_owner", return_value=True):
            with patch("kiro_crew.slack.handler._get_default_agent", return_value=""):
                await ev._handle_agent(_make_orch(), "U_OWNER", "", respond)
        accessory = respond.await_args.kwargs["blocks"][0]["accessory"]
        assert accessory["options"] == [
            {"text": {"type": "plain_text", "text": "off (default)"}, "value": "off"}
        ]
        assert accessory["initial_option"]["value"] == "off"


# ---------------------------------------------------------------------------
# /kirocrew voice
# ---------------------------------------------------------------------------


class TestHandleVoice:
    @pytest.mark.asyncio
    async def test_missing_trigger_id_reports(self):
        orch = _make_orch()
        orch._last_trigger_id = ""
        respond = AsyncMock()
        await ev._handle_voice(orch, "U_OWNER", "", respond)
        assert "Missing trigger_id" in respond.call_args[0][0]
        orch.slack.views_open.assert_not_called()

    @pytest.mark.asyncio
    async def test_opens_modal(self):
        orch = _make_orch()
        orch._last_trigger_id = "T1"
        respond = AsyncMock()
        await ev._handle_voice(orch, "U_OWNER", "", respond)
        orch.slack.views_open.assert_awaited_once()
        assert orch.slack.views_open.await_args.kwargs["trigger_id"] == "T1"
        respond.assert_not_called()

    @pytest.mark.asyncio
    async def test_views_open_failure_reports(self):
        orch = _make_orch()
        orch._last_trigger_id = "T1"
        orch.slack.views_open = AsyncMock(side_effect=RuntimeError("slack down"))
        respond = AsyncMock()
        await ev._handle_voice(orch, "U_OWNER", "", respond)
        assert "Failed to open voice settings modal" in respond.call_args[0][0]


# ---------------------------------------------------------------------------
# /kirocrew yolo
# ---------------------------------------------------------------------------


@pytest.fixture()
def _yolo_env():
    """Patch the safety-override singleton and grant-lifetime describer."""
    so = MagicMock()
    with patch("kiro_crew.slack.events.safety_override", return_value=so):
        with patch(
            "kiro_crew.slack.events.describe_grant_lifetime",
            return_value="expires in 6h",
        ):
            with patch("kiro_crew.slack.events.is_owner", return_value=True):
                yield so


class TestHandleYolo:
    @pytest.mark.asyncio
    async def test_non_owner_denied(self):
        respond = AsyncMock()
        with patch("kiro_crew.slack.events.is_owner", return_value=False):
            await ev._handle_yolo(_make_orch(), "U_OTHER", "on", respond)
        assert "Only the owner" in respond.call_args[0][0]

    @pytest.mark.asyncio
    async def test_on_when_already_active_is_noop(self, _yolo_env):
        _yolo_env.is_active.return_value = True
        respond = AsyncMock()
        await ev._handle_yolo(_make_orch(), "U_OWNER", "on", respond)
        assert "already *ON*" in respond.call_args[0][0]
        _yolo_env.activate.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_activation_failure_reports(self, _yolo_env):
        _yolo_env.is_active.return_value = False
        _yolo_env.activate.return_value = SimpleNamespace(active=False)
        respond = AsyncMock()
        await ev._handle_yolo(_make_orch(), "U_OWNER", "on", respond)
        assert "Failed to activate YOLO mode" in respond.call_args[0][0]

    @pytest.mark.asyncio
    async def test_on_success_pushes_slots_update(self, _yolo_env, _mock_sel):
        _yolo_env.is_active.return_value = False
        _yolo_env.activate.return_value = SimpleNamespace(active=True)
        orch = _make_orch()
        orch.dashboard_state = MagicMock()
        respond = AsyncMock()
        await ev._handle_yolo(orch, "U_OWNER", "ON", respond)
        orch.dashboard_state.push_slots_update.assert_called_once()
        assert "YOLO mode *ON*" in respond.call_args[0][0]
        assert _mock_sel.log_api_access.call_args.kwargs["resources"] == "yolo_on"

    @pytest.mark.asyncio
    async def test_off_disables(self, _yolo_env, _mock_sel):
        orch = _make_orch()
        orch.dashboard_state = MagicMock()
        respond = AsyncMock()
        with patch("kiro_crew.slack.handler.disable_yolo") as disable:
            await ev._handle_yolo(orch, "U_OWNER", "off", respond)
        disable.assert_called_once()
        assert "YOLO mode *OFF*" in respond.call_args[0][0]
        assert _mock_sel.log_api_access.call_args.kwargs["resources"] == "yolo_off"

    @pytest.mark.asyncio
    async def test_renew_success_reports_minutes(self, _yolo_env):
        _yolo_env.renew.return_value = SimpleNamespace(renewed=True, ttl=1800)
        orch = _make_orch()
        orch.dashboard_state = MagicMock()
        respond = AsyncMock()
        await ev._handle_yolo(orch, "U_OWNER", "renew", respond)
        assert "renewed* (auto-expires in 30min)" in respond.call_args[0][0]

    @pytest.mark.asyncio
    async def test_renew_when_inactive_reports(self, _yolo_env):
        _yolo_env.renew.return_value = SimpleNamespace(renewed=False, ttl=0)
        respond = AsyncMock()
        await ev._handle_yolo(_make_orch(), "U_OWNER", "renew", respond)
        assert "not active" in respond.call_args[0][0]

    @pytest.mark.asyncio
    async def test_bare_invocation_shows_status_on(self, _yolo_env):
        _yolo_env.is_active.return_value = True
        respond = AsyncMock()
        await ev._handle_yolo(_make_orch(), "U_OWNER", "", respond)
        body = respond.call_args[0][0]
        assert "currently *ON" in body
        assert "yolo on|off|renew" in body

    @pytest.mark.asyncio
    async def test_bare_invocation_shows_status_off(self, _yolo_env):
        _yolo_env.is_active.return_value = False
        respond = AsyncMock()
        await ev._handle_yolo(_make_orch(), "U_OWNER", "huh", respond)
        assert "currently *OFF" in respond.call_args[0][0]


# ---------------------------------------------------------------------------
# /kirocrew config, users, channels
# ---------------------------------------------------------------------------


class TestHandleConfig:
    @pytest.mark.asyncio
    async def test_non_owner_denied(self):
        respond = AsyncMock()
        with patch("kiro_crew.slack.events.is_owner", return_value=False):
            await ev._handle_config(_make_orch(), "U_OTHER", "", respond)
        assert "Only the owner" in respond.call_args[0][0]

    @pytest.mark.asyncio
    async def test_missing_trigger_id_reports(self):
        orch = _make_orch()
        orch._last_trigger_id = ""
        respond = AsyncMock()
        with patch("kiro_crew.slack.events.is_owner", return_value=True):
            await ev._handle_config(orch, "U_OWNER", "", respond)
        assert "missing trigger_id" in respond.call_args[0][0]

    @pytest.mark.asyncio
    async def test_opens_modal_with_initial_channels(self):
        orch = _make_orch()
        orch._tracking_channels = {"C1"}
        orch._last_trigger_id = "T9"
        respond = AsyncMock()
        with patch("kiro_crew.slack.events.is_owner", return_value=True):
            await ev._handle_config(orch, "U_OWNER", "", respond)
        view = orch.slack.views_open.await_args.kwargs["view"]
        assert view["callback_id"] == "mc_config_panel"
        assert view["blocks"][1]["element"]["initial_channels"] == ["C1"]

    @pytest.mark.asyncio
    async def test_views_open_failure_reports(self):
        orch = _make_orch()
        orch._last_trigger_id = "T9"
        orch.slack.views_open = AsyncMock(side_effect=RuntimeError("nope"))
        respond = AsyncMock()
        with patch("kiro_crew.slack.events.is_owner", return_value=True):
            await ev._handle_config(orch, "U_OWNER", "", respond)
        assert "Failed to open config modal" in respond.call_args[0][0]


class TestHandleAllowlistCmd:
    @pytest.mark.asyncio
    async def test_multi_user_is_refused(self):
        respond = AsyncMock()
        await ev._handle_allowlist_cmd(_make_orch(), "U_OWNER", "add U2", respond)
        assert "Multi-user access is disabled" in respond.call_args[0][0]


class TestHandleChannelCmd:
    @pytest.mark.asyncio
    async def test_non_owner_denied(self):
        respond = AsyncMock()
        with patch("kiro_crew.slack.events.is_owner", return_value=False):
            await ev._handle_channel_cmd(_make_orch(), "U_OTHER", "", respond)
        assert "Only the owner" in respond.call_args[0][0]

    @pytest.mark.asyncio
    async def test_missing_trigger_id_reports(self):
        orch = _make_orch(channels={"C1": ChannelConfig(activation=ACTIVATION_ALWAYS)})
        orch._tracking_channels = {"C1"}
        orch._last_trigger_id = ""
        respond = AsyncMock()
        with patch("kiro_crew.slack.events.is_owner", return_value=True):
            await ev._handle_channel_cmd(orch, "U_OWNER", "", respond)
        assert "missing trigger_id" in respond.call_args[0][0]

    @pytest.mark.asyncio
    async def test_opens_channels_modal(self):
        orch = _make_orch(channels={"C1": ChannelConfig(activation=ACTIVATION_ALWAYS)})
        orch._tracking_channels = {"C1"}
        orch._last_trigger_id = "T3"
        respond = AsyncMock()
        with patch("kiro_crew.slack.events.is_owner", return_value=True):
            await ev._handle_channel_cmd(orch, "U_OWNER", "", respond)
        orch.slack.views_open.assert_awaited_once()
        respond.assert_not_called()

    @pytest.mark.asyncio
    async def test_views_open_failure_reports(self):
        orch = _make_orch()
        orch._tracking_channels = set()
        orch._last_trigger_id = "T3"
        orch.slack.views_open = AsyncMock(side_effect=RuntimeError("nope"))
        respond = AsyncMock()
        with patch("kiro_crew.slack.events.is_owner", return_value=True):
            await ev._handle_channel_cmd(orch, "U_OWNER", "", respond)
        assert "Failed to open channels modal" in respond.call_args[0][0]


class TestGetAgentNamesGuards:
    """The blocked-symlink audit branch of ``_get_agent_names``."""

    def test_permission_error_falls_back_to_stem_and_audits(self, _isolated_home, _mock_sel):
        agents = _isolated_home / ".kiro" / "agents"
        agents.mkdir(parents=True)
        (agents / "sneaky.json").write_text("{}")
        with patch(
            "kiro_crew.slack.events.safe_read_file",
            side_effect=PermissionError("sensitive path"),
        ):
            assert ev._get_agent_names() == ["sneaky"]
        ops = [c.kwargs.get("operation") for c in _mock_sel.log_api_access.call_args_list]
        assert "sensitive_path_blocked" in ops

    def test_audit_failure_is_swallowed(self, _isolated_home, _mock_sel):
        agents = _isolated_home / ".kiro" / "agents"
        agents.mkdir(parents=True)
        (agents / "sneaky.json").write_text("{}")
        _mock_sel.log_api_access.side_effect = RuntimeError("audit sink down")
        with patch(
            "kiro_crew.slack.events.safe_read_file",
            side_effect=PermissionError("sensitive path"),
        ):
            assert ev._get_agent_names() == ["sneaky"]

    def test_non_utf8_json_falls_back_to_stem(self, _isolated_home):
        agents = _isolated_home / ".kiro" / "agents"
        agents.mkdir(parents=True)
        (agents / "._apple.json").write_bytes(b"\x00\x05\x16\xff\xfe")
        assert ev._get_agent_names() == ["._apple"]

    def test_non_dict_json_falls_back_to_stem(self, _isolated_home):
        agents = _isolated_home / ".kiro" / "agents"
        agents.mkdir(parents=True)
        (agents / "listy.json").write_text("[1, 2, 3]")
        assert ev._get_agent_names() == ["listy"]


# ---------------------------------------------------------------------------
# init_socket_mode + the _on_event dispatcher
# ---------------------------------------------------------------------------


def _socket_orch() -> MagicMock:
    orch = _make_orch()
    orch._slack_enabled = True
    orch._bot_token = "xoxb-not-a-real-value"
    orch._app_token = "xapp-not-a-real-value"
    orch._socket_client = None
    return orch


class _SocketPatches:
    """Patch every module-global ``init_socket_mode`` reaches out to."""

    def __init__(self, validate: bool = True):
        self._validate = validate
        self._stack: list = []
        self.setters: dict[str, MagicMock] = {}
        self.client_cls = MagicMock()
        self.client_cls.return_value.socket_mode_request_listeners = []

    def __enter__(self) -> _SocketPatches:
        for name in (
            "set_allowed_users",
            "set_tracking_channels",
            "set_open_channels",
            "set_owner_id",
            "set_orch_cfg",
            "set_dashboard_state",
            "set_yolo_mode",
        ):
            p = patch(f"kiro_crew.slack.events.{name}")
            self.setters[name] = p.start()
            self._stack.append(p)
        for target, new in (
            ("kiro_crew.slack.events.WSSocketModeClient", self.client_cls),
            ("kiro_crew.slack.events.AsyncWebClient", MagicMock()),
        ):
            p = patch(target, new)
            p.start()
            self._stack.append(p)
        ctx = MagicMock()
        ctx.return_value.slack_gate.validate_enterprise.return_value = self._validate
        p = patch("kiro_crew.slack.events.current_context", ctx)
        p.start()
        self._stack.append(p)
        return self

    def __exit__(self, *exc: object) -> None:
        for p in reversed(self._stack):
            p.stop()


class TestInitSocketMode:
    def test_disabled_gateway_is_a_noop(self):
        orch = _socket_orch()
        orch._slack_enabled = False
        with _SocketPatches() as sp:
            ev.init_socket_mode(orch, ev.SeenCache())
        assert orch._socket_client is None
        sp.setters["set_owner_id"].assert_not_called()

    def test_missing_owner_disables_slack(self):
        orch = _socket_orch()
        orch._owner_id = ""
        with _SocketPatches():
            ev.init_socket_mode(orch, ev.SeenCache())
        assert orch._slack_enabled is False
        assert orch.slack is None

    def test_enterprise_validation_failure_disables_slack(self):
        orch = _socket_orch()
        with _SocketPatches(validate=False):
            ev.init_socket_mode(orch, ev.SeenCache())
        assert orch._slack_enabled is False
        assert orch.slack is None
        assert orch._socket_client is None

    def test_success_installs_listener_and_shares_state(self):
        orch = _socket_orch()
        orch.dashboard_state = MagicMock()
        with _SocketPatches() as sp:
            ev.init_socket_mode(orch, ev.SeenCache())
        sp.setters["set_owner_id"].assert_called_once_with("U_OWNER")
        sp.setters["set_orch_cfg"].assert_called_once_with(orch._cfg)
        sp.setters["set_dashboard_state"].assert_called_once_with(orch.dashboard_state)
        sp.setters["set_yolo_mode"].assert_not_called()
        assert len(orch._socket_client.socket_mode_request_listeners) == 1

    def test_dangerously_skip_permissions_enables_yolo(self):
        orch = _socket_orch()
        orch._cfg.agent.dangerously_skip_permissions = True
        with _SocketPatches() as sp:
            ev.init_socket_mode(orch, ev.SeenCache())
        sp.setters["set_yolo_mode"].assert_called_once_with(True)


def _install_on_event(orch: MagicMock, seen: ev.SeenCache):
    with _SocketPatches():
        ev.init_socket_mode(orch, seen)
    return orch._socket_client.socket_mode_request_listeners[0]


def _req(req_type: str, payload: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(type=req_type, payload=payload or {}, envelope_id="env-1")


def _client(ack_fails: bool = False) -> MagicMock:
    client = MagicMock()
    client.send_socket_mode_response = AsyncMock(
        side_effect=RuntimeError("socket not ready") if ack_fails else None
    )
    return client


class TestOnEventDispatch:
    @pytest.mark.asyncio
    async def test_ack_failure_short_circuits(self):
        orch = _socket_orch()
        on_event = _install_on_event(orch, ev.SeenCache())
        with patch(
            "kiro_crew.slack.events.dispatch_interactive", new_callable=AsyncMock
        ) as dispatch:
            await on_event(_client(ack_fails=True), _req("interactive"))
        dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_interactive_is_dispatched(self):
        orch = _socket_orch()
        on_event = _install_on_event(orch, ev.SeenCache())
        with patch(
            "kiro_crew.slack.events.dispatch_interactive", new_callable=AsyncMock
        ) as dispatch:
            await on_event(_client(), _req("interactive", {"action": "x"}))
            await _drain(orch)
        dispatch.assert_awaited_once_with({"action": "x"})

    @pytest.mark.asyncio
    async def test_slash_command_is_dispatched(self):
        orch = _socket_orch()
        on_event = _install_on_event(orch, ev.SeenCache())
        with patch("kiro_crew.slack.events._handle_slash", new_callable=AsyncMock) as slash:
            await on_event(_client(), _req("slash_commands", {"command": "/kirocrew"}))
            await _drain(orch)
        slash.assert_awaited_once_with(orch, {"command": "/kirocrew"})

    @pytest.mark.asyncio
    async def test_unknown_envelope_type_ignored(self):
        orch = _socket_orch()
        on_event = _install_on_event(orch, ev.SeenCache())
        with patch("kiro_crew.slack.events._route_message", new_callable=AsyncMock) as route:
            await on_event(_client(), _req("hello"))
        route.assert_not_called()

    @pytest.mark.asyncio
    async def test_member_joined_channel_routed_to_prompt(self):
        orch = _socket_orch()
        on_event = _install_on_event(orch, ev.SeenCache())
        payload = {"event": {"type": "member_joined_channel", "channel": "C1"}}
        with patch("kiro_crew.slack.events._maybe_prompt_owner") as prompt:
            await on_event(_client(), _req("events_api", payload))
        prompt.assert_called_once()

    @pytest.mark.asyncio
    async def test_home_tab_published_for_allowed_user(self, _mock_sel):
        orch = _socket_orch()
        on_event = _install_on_event(orch, ev.SeenCache())
        payload = {"event": {"type": "app_home_opened", "tab": "home", "user": "U_OWNER"}}
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch(
                "kiro_crew.slack.events._publish_home_tab", new_callable=AsyncMock
            ) as publish:
                await on_event(_client(), _req("events_api", payload))
                await asyncio.sleep(0)
                await asyncio.gather(*list(ev._background_tasks), return_exceptions=True)
        publish.assert_awaited_once_with(orch, "U_OWNER")
        assert _mock_sel.log_api_access.call_args.kwargs["outcome"] == "allowed"

    @pytest.mark.asyncio
    async def test_home_tab_denied_for_unauthorized_user(self, _mock_sel):
        orch = _socket_orch()
        on_event = _install_on_event(orch, ev.SeenCache())
        payload = {"event": {"type": "app_home_opened", "tab": "home", "user": "U_BAD"}}
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=False):
            with patch(
                "kiro_crew.slack.events._publish_home_tab", new_callable=AsyncMock
            ) as publish:
                await on_event(_client(), _req("events_api", payload))
        publish.assert_not_called()
        assert _mock_sel.log_api_access.call_args.kwargs["error"] == "unauthorized sender"

    @pytest.mark.asyncio
    async def test_home_tab_other_tab_ignored(self):
        orch = _socket_orch()
        on_event = _install_on_event(orch, ev.SeenCache())
        payload = {"event": {"type": "app_home_opened", "tab": "messages", "user": "U_OWNER"}}
        with patch("kiro_crew.slack.events._publish_home_tab", new_callable=AsyncMock) as publish:
            await on_event(_client(), _req("events_api", payload))
        publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_message_event_ignored(self):
        orch = _socket_orch()
        on_event = _install_on_event(orch, ev.SeenCache())
        payload = {"event": {"type": "reaction_added"}}
        with patch("kiro_crew.slack.events._route_message", new_callable=AsyncMock) as route:
            await on_event(_client(), _req("events_api", payload))
        route.assert_not_called()

    @pytest.mark.asyncio
    async def test_message_deleted_is_delegated(self):
        orch = _socket_orch()
        on_event = _install_on_event(orch, ev.SeenCache())
        event = {"type": "message", "subtype": "message_deleted", "deleted_ts": "1.0"}
        with patch(
            "kiro_crew.slack.events._handle_message_deleted", new_callable=AsyncMock
        ) as deleted:
            await on_event(_client(), _req("events_api", {"event": event}))
        deleted.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unhandled_subtype_ignored(self):
        orch = _socket_orch()
        on_event = _install_on_event(orch, ev.SeenCache())
        event = {"type": "message", "subtype": "channel_join"}
        with patch("kiro_crew.slack.events._route_message", new_callable=AsyncMock) as route:
            await on_event(_client(), _req("events_api", {"event": event}))
        route.assert_not_called()

    @pytest.mark.asyncio
    async def test_envelope_team_id_overrides_event_team(self):
        orch = _socket_orch()
        on_event = _install_on_event(orch, ev.SeenCache())
        event = {"type": "message", "user": "U1", "channel": "D1", "text": "hi", "team": "T_EVIL"}
        payload = {"event": event, "team_id": "T_REAL"}
        with patch("kiro_crew.slack.events._route_message", new_callable=AsyncMock) as route:
            await on_event(_client(), _req("events_api", payload))
        assert route.await_args[0][1]["team"] == "T_REAL"
        assert route.await_args.kwargs["is_mention"] is False

    @pytest.mark.asyncio
    async def test_app_mention_sets_is_mention(self):
        orch = _socket_orch()
        on_event = _install_on_event(orch, ev.SeenCache())
        event = {"type": "app_mention", "user": "U1", "channel": "C1", "team": "T1"}
        with patch("kiro_crew.slack.events._route_message", new_callable=AsyncMock) as route:
            await on_event(_client(), _req("events_api", {"event": event}))
        assert route.await_args.kwargs["is_mention"] is True

    @pytest.mark.asyncio
    async def test_missing_team_id_is_rejected(self, _mock_sel):
        orch = _socket_orch()
        on_event = _install_on_event(orch, ev.SeenCache())
        event = {"type": "message", "user": "U1", "channel": "C1", "text": "hi"}
        with patch("kiro_crew.slack.events._route_message", new_callable=AsyncMock) as route:
            await on_event(_client(), _req("events_api", {"event": event}))
        route.assert_not_called()
        assert _mock_sel.log_api_access.call_args.kwargs["error"] == "missing_team_id"


# ---------------------------------------------------------------------------
# _handle_slash dispatcher
# ---------------------------------------------------------------------------


class TestHandleSlash:
    @pytest.mark.asyncio
    async def test_foreign_command_ignored(self, _mock_sel):
        orch = _make_orch()
        await ev._handle_slash(orch, {"command": "/other", "user_id": "U_OWNER"})
        _mock_sel.log_api_access.assert_not_called()

    @pytest.mark.asyncio
    async def test_unauthorized_caller_denied(self, _mock_sel):
        orch = _make_orch()
        payload = {"command": "/kirocrew", "user_id": "U_BAD", "text": "status"}
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=False):
            await ev._handle_slash(orch, payload)
            await _drain(orch)
        assert _mock_sel.log_api_access.call_args.kwargs["error"] == "unauthorized sender"

    @pytest.mark.asyncio
    async def test_owner_not_configured_reports(self):
        orch = _make_orch()
        orch._owner_id = ""
        posted: list[dict] = []
        payload = {"command": "/kirocrew", "user_id": "U_OWNER", "text": "status"}
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with _capture_respond(posted):
                await ev._handle_slash(orch, payload)
                await _drain(orch)
        assert posted and "Owner not configured" in posted[0]["text"]

    @pytest.mark.asyncio
    async def test_registry_handler_receives_args_and_trigger_id(self):
        orch = _make_orch()
        seen: list[tuple] = []

        async def _handler(o, caller, args, respond):
            seen.append((caller, args))

        ev.register_slash_command("zzcovsub", _handler, "coverage probe")
        try:
            payload = {
                "command": "/kirocrew",
                "user_id": "U_OWNER",
                "text": "zzcovsub  alpha beta",
                "trigger_id": "TRIG",
            }
            with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
                await ev._handle_slash(orch, payload)
                await _drain(orch)
        finally:
            ev.SLASH_REGISTRY.pop("zzcovsub", None)
        assert seen == [("U_OWNER", "alpha beta")]
        assert orch._last_trigger_id == "TRIG"

    @pytest.mark.asyncio
    async def test_user_mention_fallback_refuses_multi_user(self):
        orch = _make_orch()
        posted: list[dict] = []
        payload = {"command": "/kirocrew", "user_id": "U_OWNER", "text": "<@U123|bob>"}
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with _capture_respond(posted):
                await ev._handle_slash(orch, payload)
                await _drain(orch)
        assert posted and "Multi-user access is disabled" in posted[0]["text"]

    @pytest.mark.asyncio
    async def test_channel_mention_fallback_sends_track_request(self):
        orch = _make_orch()
        posted: list[dict] = []
        payload = {"command": "/kirocrew", "user_id": "U_OWNER", "text": "<#C123|general>"}
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch(
                "kiro_crew.slack.events.prompt_track_channel", new_callable=AsyncMock
            ) as prompt:
                with _capture_respond(posted):
                    await ev._handle_slash(orch, payload)
                    await _drain(orch)
        prompt.assert_awaited_once_with(orch.slack, "U_OWNER", "C123", "general")
        assert "Track request sent for #general" in posted[0]["text"]

    @pytest.mark.asyncio
    async def test_channel_mention_without_name_uses_secret(self):
        orch = _make_orch()
        posted: list[dict] = []
        payload = {"command": "/kirocrew", "user_id": "U_OWNER", "text": "<#C123>"}
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.prompt_track_channel", new_callable=AsyncMock):
                with _capture_respond(posted):
                    await ev._handle_slash(orch, payload)
                    await _drain(orch)
        assert "#Secret" in posted[0]["text"]

    @pytest.mark.asyncio
    async def test_unknown_subcommand_returns_help(self):
        orch = _make_orch()
        posted: list[dict] = []
        payload = {"command": "/kirocrew", "user_id": "U_OWNER", "text": "flibbertigibbet"}
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with _capture_respond(posted):
                await ev._handle_slash(orch, payload)
                await _drain(orch)
        assert "*Available commands:*" in posted[0]["text"]

    @pytest.mark.asyncio
    async def test_respond_without_response_url_posts_nothing(self):
        orch = _make_orch()
        posted: list[dict] = []
        payload = {"command": "/kirocrew", "user_id": "U_OWNER", "text": "nope"}
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with _capture_respond(posted, response_url=""):
                await ev._handle_slash(orch, payload)
                await _drain(orch)
        assert posted == []

    @pytest.mark.asyncio
    async def test_respond_post_failure_is_swallowed(self):
        orch = _make_orch()
        payload = {
            "command": "/kirocrew",
            "user_id": "U_OWNER",
            "text": "nope",
            "response_url": "https://hooks.example.invalid/x",
        }
        session = MagicMock()
        session.post = AsyncMock(side_effect=RuntimeError("network down"))
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.aiohttp.ClientSession", return_value=session):
                await ev._handle_slash(orch, payload)
                await _drain(orch)
        session.post.assert_awaited_once()


class _capture_respond:
    """Capture ``_handle_slash``'s ``response_url`` POST bodies without networking."""

    def __init__(self, sink: list[dict], response_url: str = "https://hooks.example.invalid/x"):
        self._sink = sink
        self._response_url = response_url
        self._patches: list = []

    def __enter__(self) -> _capture_respond:
        sink = self._sink

        class _Session:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *exc):
                return False

            async def post(self_inner, url, json=None):  # noqa: A002 - aiohttp kwarg name
                sink.append(json or {})
                return MagicMock()

        p = patch("kiro_crew.slack.events.aiohttp.ClientSession", _Session)
        p.start()
        self._patches.append(p)
        # _handle_slash reads response_url straight off the payload, so inject it
        # by patching the module's dict lookup target instead: simplest is to let
        # callers pass the URL through the payload. Tests using this helper set a
        # response_url on the payload via _patch_payload below.
        self._orig = ev._handle_slash
        url = self._response_url

        async def _wrapped(orch, payload):
            payload = dict(payload)
            payload.setdefault("response_url", url)
            return await self._orig(orch, payload)

        p2 = patch("kiro_crew.slack.events._handle_slash", _wrapped)
        p2.start()
        self._patches.append(p2)
        return self

    def __exit__(self, *exc: object) -> None:
        for p in reversed(self._patches):
            p.stop()


# ---------------------------------------------------------------------------
# Block Kit text recovery (pure helpers)
# ---------------------------------------------------------------------------


class TestRenderRichTextElement:
    @pytest.mark.parametrize(
        "element,expected",
        [
            ({"type": "text", "text": "hello"}, "hello"),
            ({"type": "link", "text": "site", "url": "https://a.invalid"},
             "site (https://a.invalid)"),
            ({"type": "link", "url": "https://a.invalid"}, "https://a.invalid"),
            ({"type": "link", "text": "bare"}, "bare"),
            ({"type": "emoji", "name": "tada"}, ":tada:"),
            ({"type": "emoji", "unicode": "1f389"}, "1f389"),
            ({"type": "user", "user_id": "U1"}, "<@U1>"),
            ({"type": "user"}, ""),
            ({"type": "usergroup", "usergroup_id": "S1"}, "<!subteam^S1>"),
            ({"type": "usergroup"}, ""),
            ({"type": "channel", "channel_id": "C1"}, "<#C1>"),
            ({"type": "channel"}, ""),
            ({"type": "broadcast", "range": "here"}, "<!here>"),
            ({"type": "broadcast"}, ""),
            ({"type": "date", "fallback": "Jan 1"}, "Jan 1"),
            ({"type": "mystery", "text": "raw"}, "raw"),
        ],
    )
    def test_renders_every_documented_type(self, element, expected):
        assert ev._render_rich_text_element(element) == expected

    def test_non_dict_returns_empty(self):
        assert ev._render_rich_text_element("nope") == ""  # type: ignore[arg-type]


class TestExtractBlocksText:
    def test_rich_text_section(self):
        blocks = [
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [{"type": "text", "text": "plain body"}],
                    }
                ],
            }
        ]
        assert ev._extract_blocks_text(blocks) == "plain body"

    def test_rich_text_list_and_quote(self):
        blocks = [
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_list",
                        "elements": [
                            {"elements": [{"type": "text", "text": "one"}]},
                            {"elements": [{"type": "text", "text": "two"}]},
                            {"elements": []},
                            "not-a-dict",
                        ],
                    },
                    {
                        "type": "rich_text_quote",
                        "elements": [{"type": "text", "text": "quoted"}],
                    },
                ],
            }
        ]
        assert ev._extract_blocks_text(blocks) == "- one\n- two\n> quoted"

    def test_section_and_context_blocks(self):
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": "sec"}},
            {"type": "section", "text": "not-a-dict"},
            {"type": "section", "text": {"type": "mrkdwn", "text": ""}},
            {"type": "context", "elements": [{"text": "ctx"}, {"text": ""}, "bad"]},
            {"type": "context", "elements": "bad"},
        ]
        assert ev._extract_blocks_text(blocks) == "sec\nctx"

    def test_malformed_input_never_raises(self):
        blocks = [
            "not-a-dict",
            {"type": "rich_text", "elements": "bad"},
            {"type": "rich_text", "elements": ["bad", {"type": "rich_text_section",
                                                       "elements": "bad"}]},
            {"type": "rich_text", "elements": [{"type": "rich_text_list",
                                                "elements": [{"elements": "bad"}]}]},
            {"type": "divider"},
        ]
        assert ev._extract_blocks_text(blocks) == ""

    def test_result_is_capped(self):
        long_text = "x" * (ev._MAX_RECOVERED_TEXT_CHARS + 500)
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": long_text}}]
        assert len(ev._extract_blocks_text(blocks)) == ev._MAX_RECOVERED_TEXT_CHARS


class TestNormalizeMessageBlocks:
    def test_flattens_wrapper(self):
        raw = [
            {"message": {"blocks": [{"type": "divider"}, "bad"]}},
            {"message": {"blocks": "bad"}},
            {"message": "bad"},
            "bad",
        ]
        assert ev._normalize_message_blocks(raw) == [{"type": "divider"}]

    def test_non_list_returns_empty(self):
        assert ev._normalize_message_blocks("nope") == []  # type: ignore[arg-type]


class TestExtractSharedText:
    def test_share_attachment_text_wins(self):
        event = {"attachments": [{"is_share": True, "text": "forwarded body"}]}
        assert ev._extract_shared_text(event) == "forwarded body"

    def test_link_unfurl_attachments_are_skipped(self):
        event = {"attachments": [{"text": "preview leak"}]}
        assert ev._extract_shared_text(event) == ""

    def test_attachment_blocks_recovered(self):
        event = {
            "attachments": [
                {
                    "is_msg_unfurl": True,
                    "text": "",
                    "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "from blk"}}],
                }
            ]
        }
        assert ev._extract_shared_text(event) == "from blk"

    def test_message_blocks_wrapper_recovered(self):
        event = {
            "attachments": [
                {
                    "is_share": True,
                    "text": "",
                    "blocks": [],
                    "message_blocks": [
                        {
                            "message": {
                                "blocks": [
                                    {
                                        "type": "section",
                                        "text": {"type": "mrkdwn", "text": "wrapped"},
                                    }
                                ]
                            }
                        }
                    ],
                }
            ]
        }
        assert ev._extract_shared_text(event) == "wrapped"

    def test_generic_placeholder_fallback_is_dropped(self):
        event = {
            "attachments": [
                {
                    "is_share": True,
                    "text": "",
                    "fallback": "This message contains interactive elements.",
                }
            ]
        }
        assert ev._extract_shared_text(event) == ""

    def test_real_fallback_is_used(self):
        event = {"attachments": [{"is_share": True, "text": "", "fallback": "real fallback"}]}
        assert ev._extract_shared_text(event) == "real fallback"

    def test_event_level_blocks_used_when_attachments_yield_nothing(self):
        event = {
            "attachments": [],
            "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "event blk"}}],
        }
        assert ev._extract_shared_text(event) == "event blk"

    def test_empty_event_returns_empty(self):
        assert ev._extract_shared_text({}) == ""


class TestSafeLog:
    def test_strips_newlines_and_tabs(self):
        assert ev._safe_log("a\r\nb\tc") == "a  b c"

    def test_empty_passthrough(self):
        assert ev._safe_log("") == ""


# ---------------------------------------------------------------------------
# Voice transcription helpers
# ---------------------------------------------------------------------------


class TestTranscribeWithReaction:
    @pytest.mark.asyncio
    async def test_reaction_added_and_removed(self):
        orch = _make_orch()
        slack = AsyncMock()
        with patch(
            "kiro_crew.slack.events._transcribe_files",
            new_callable=AsyncMock,
            return_value=["hello"],
        ):
            out = await ev._transcribe_with_reaction(slack, "C1", "1.0", orch, [{}])
        assert out == ["hello"]
        slack.add_reaction.assert_awaited_once_with("C1", "1.0", "studio_microphone")
        slack.remove_reaction.assert_awaited_once_with("C1", "1.0", "studio_microphone")

    @pytest.mark.asyncio
    async def test_add_reaction_failure_skips_removal(self):
        orch = _make_orch()
        slack = AsyncMock()
        slack.add_reaction.side_effect = RuntimeError("no perms")
        with patch(
            "kiro_crew.slack.events._transcribe_files",
            new_callable=AsyncMock,
            return_value=[],
        ):
            assert await ev._transcribe_with_reaction(slack, "C1", "1.0", orch, [{}]) == []
        slack.remove_reaction.assert_not_called()

    @pytest.mark.asyncio
    async def test_remove_reaction_failure_is_swallowed(self):
        orch = _make_orch()
        slack = AsyncMock()
        slack.remove_reaction.side_effect = RuntimeError("gone")
        with patch(
            "kiro_crew.slack.events._transcribe_files",
            new_callable=AsyncMock,
            return_value=["x"],
        ):
            assert await ev._transcribe_with_reaction(slack, "C1", "1.0", orch, [{}]) == ["x"]

    @pytest.mark.asyncio
    async def test_reaction_removed_even_when_transcription_raises(self):
        orch = _make_orch()
        slack = AsyncMock()
        with patch(
            "kiro_crew.slack.events._transcribe_files",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            with pytest.raises(RuntimeError):
                await ev._transcribe_with_reaction(slack, "C1", "1.0", orch, [{}])
        slack.remove_reaction.assert_awaited_once()


class TestTranscribeFiles:
    @pytest.mark.asyncio
    async def test_non_audio_and_urlless_files_skipped(self):
        orch = _make_orch()
        files = [
            {"mimetype": "image/png", "url_private": "https://x.invalid/a.png"},
            {"mimetype": "audio/webm"},
        ]
        assert await ev._transcribe_files(orch, files) == []
        orch.slack.download_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_successful_transcription(self, _mock_sel):
        orch = _make_orch()
        files = [
            {
                "mimetype": "audio/webm",
                "url_private_download": "https://x.invalid/a.webm",
                "filetype": "webm",
                "name": "memo.webm",
            }
        ]
        with patch(
            "kiro_crew.slack.events.transcribe_audio",
            new_callable=AsyncMock,
            return_value="spoken words",
        ):
            assert await ev._transcribe_files(orch, files) == ["spoken words"]
        outcomes = [c.kwargs.get("outcome") for c in _mock_sel.log_api_access.call_args_list]
        assert "success" in outcomes

    @pytest.mark.asyncio
    async def test_empty_transcript_warns_and_returns_nothing(self, _mock_sel):
        orch = _make_orch()
        files = [
            {
                "mimetype": "audio/mp4",
                "url_private": "https://x.invalid/a.m4a",
                "filetype": "m 4 a!",
                "name": "quiet.m4a",
            }
        ]
        with patch(
            "kiro_crew.slack.events.transcribe_audio",
            new_callable=AsyncMock,
            return_value="",
        ):
            assert await ev._transcribe_files(orch, files) == []
        outcomes = [c.kwargs.get("outcome") for c in _mock_sel.log_api_access.call_args_list]
        assert "empty" in outcomes

    @pytest.mark.asyncio
    async def test_download_failure_audits_error(self, _mock_sel):
        orch = _make_orch()
        orch.slack.download_file = AsyncMock(side_effect=RuntimeError("404"))
        files = [
            {
                "mimetype": "audio/webm",
                "url_private": "https://x.invalid/a.webm",
                "name": "bad.webm",
            }
        ]
        assert await ev._transcribe_files(orch, files) == []
        errors = [c.kwargs.get("error") for c in _mock_sel.log_api_access.call_args_list]
        assert "transcription_failed" in errors

    @pytest.mark.asyncio
    async def test_temp_unlink_failure_is_tolerated(self, tmp_path, monkeypatch):
        orch = _make_orch()
        files = [
            {
                "mimetype": "audio/webm",
                "url_private": "https://x.invalid/a.webm",
                "name": "memo.webm",
            }
        ]
        # This test deliberately makes unlink fail, so the downloaded file survives
        # the run. events.py allocates it with tempfile.mkstemp(), which would put it
        # in the SYSTEM temp dir -- a real artifact left on the host, which the
        # blocking no-test-side-effects rule forbids. Pin the allocation into
        # tmp_path so the surviving file is inside pytest's own scratch dir.
        real_mkstemp = tempfile.mkstemp

        def _mkstemp_in_tmp(*args, **kwargs):
            kwargs["dir"] = str(tmp_path)
            return real_mkstemp(*args, **kwargs)

        monkeypatch.setattr(tempfile, "mkstemp", _mkstemp_in_tmp)
        with patch(
            "kiro_crew.slack.events.transcribe_audio",
            new_callable=AsyncMock,
            return_value="words",
        ):
            with patch(
                "kiro_crew.slack.events.os.unlink", side_effect=OSError("locked")
            ) as unlink:
                assert await ev._transcribe_files(orch, files) == ["words"]
        unlink.assert_called_once()
        # The surviving file is inside tmp_path, not the system temp dir.
        assert list(tmp_path.iterdir()), "the download should have landed in tmp_path"


# ---------------------------------------------------------------------------
# message_deleted
# ---------------------------------------------------------------------------


class TestHandleMessageDeleted:
    @pytest.mark.asyncio
    async def test_unauthorized_deleter_ignored(self, _mock_sel):
        orch = _make_orch()
        event = {"deleted_ts": "1.0", "channel": "C1", "previous_message": {"user": "U_BAD"}}
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=False):
            await ev._handle_message_deleted(orch, event)
        _mock_sel.log_api_access.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_deleted_ts_ignored(self, _mock_sel):
        orch = _make_orch()
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            await ev._handle_message_deleted(orch, {"channel": "C1"})
        _mock_sel.log_api_access.assert_not_called()

    @pytest.mark.asyncio
    async def test_session_queue_cancellation_recorded(self, _mock_sel):
        orch = _make_orch()
        orch.sessions.cancel_queued = MagicMock(return_value=True)
        event = {
            "deleted_ts": "2.0",
            "channel": "C1",
            "previous_message": {"user": "U_OWNER", "thread_ts": "1.0"},
        }
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            await ev._handle_message_deleted(orch, event)
        orch.sessions.cancel_queued.assert_called_once_with("1.0", "2.0")
        assert "queued=True" in _mock_sel.log_api_access.call_args.kwargs["resources"]

    @pytest.mark.asyncio
    async def test_pending_queue_entry_removed_and_key_dropped(self):
        orch = _make_orch()
        orch._pending_queue = {"3.0": [("3.0", "text", {})]}
        event = {"deleted_ts": "3.0", "channel": "C1", "previous_message": {"user": "U_OWNER"}}
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            await ev._handle_message_deleted(orch, event)
        assert "3.0" not in orch._pending_queue

    @pytest.mark.asyncio
    async def test_pending_queue_keeps_surviving_entries(self):
        orch = _make_orch()
        orch._pending_queue = {"4.0": [("4.0", "gone", {}), ("5.0", "stays", {})]}
        event = {"deleted_ts": "4.0", "channel": "C1", "previous_message": {"user": "U_OWNER"}}
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            await ev._handle_message_deleted(orch, event)
        assert orch._pending_queue["4.0"] == [("5.0", "stays", {})]

    @pytest.mark.asyncio
    async def test_nothing_queued_still_audits(self, _mock_sel):
        orch = _make_orch()
        event = {"deleted_ts": "6.0", "channel": "C1", "previous_message": {"user": "U_OWNER"}}
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            await ev._handle_message_deleted(orch, event)
        assert "queued=False" in _mock_sel.log_api_access.call_args.kwargs["resources"]


# ---------------------------------------------------------------------------
# _resolve_approval_mode
# ---------------------------------------------------------------------------


class TestResolveApprovalMode:
    def test_yolo_forces_auto(self):
        orch = _make_orch()
        orch._approval_mode = "interactive"
        with patch("kiro_crew.slack.events.is_yolo_mode", return_value=True):
            assert ev._resolve_approval_mode(orch) == ev.APPROVAL_AUTO

    def test_cli_flag_wins_over_config(self):
        orch = _make_orch()
        orch._approval_mode = ev.APPROVAL_AUTO
        with patch("kiro_crew.slack.events.is_yolo_mode", return_value=False):
            assert ev._resolve_approval_mode(orch) == ev.APPROVAL_AUTO

    def test_anything_else_is_interactive(self):
        orch = _make_orch()
        orch._approval_mode = "reads"
        with patch("kiro_crew.slack.events.is_yolo_mode", return_value=False):
            assert ev._resolve_approval_mode(orch) == ev.APPROVAL_INTERACTIVE


# ---------------------------------------------------------------------------
# _route_message guard branches
# ---------------------------------------------------------------------------


def _event(**over: object) -> dict:
    # A DM channel ("D…") so the default slack_dm_activation=always applies —
    # a "C…" group channel defaults to activation=mention and would drop a
    # plain message before reaching the branch under test.
    base = {
        "user": "U_OWNER",
        "channel": "D1",
        "text": "hello",
        "ts": "100.0",
        "team": "T1",
    }
    base.update(over)  # type: ignore[arg-type]
    return base


class TestRouteMessageGuards:
    @pytest.mark.asyncio
    async def test_activation_off_drops_plain_message(self, _mock_sel):
        orch = _make_orch(channels={"C1": ChannelConfig(activation=ACTIVATION_OFF)})
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                await ev._route_message(orch, _event(channel="C1"), ev.SeenCache())
        hm.assert_not_called()
        assert _mock_sel.log_api_access.call_args.kwargs["error"] == "activation=off"

    @pytest.mark.asyncio
    async def test_activation_off_lets_bang_channel_through(self):
        orch = _make_orch(channels={"C1": ChannelConfig(activation=ACTIVATION_OFF)})
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                await ev._route_message(
                    orch, _event(channel="C1", text="!channel on"), ev.SeenCache()
                )
                await _drain(orch)
        hm.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_activation_off_strips_mention_before_bang_check(self):
        orch = _make_orch(channels={"C1": ChannelConfig(activation=ACTIVATION_OFF)})
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                await ev._route_message(
                    orch,
                    _event(channel="C1", text="<@BOT1> !channel on"),
                    ev.SeenCache(),
                    is_mention=True,
                )
                await _drain(orch)
        hm.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_missing_sender_or_text_returns_early(self):
        orch = _make_orch()
        with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
            await ev._route_message(orch, _event(user=""), ev.SeenCache())
            await ev._route_message(orch, _event(channel=""), ev.SeenCache())
            await ev._route_message(orch, _event(text="", files=[]), ev.SeenCache())
        hm.assert_not_called()

    @pytest.mark.asyncio
    async def test_channels_governance_denial_blocks_message(self, _mock_sel):
        orch = _make_orch()
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch(
                "kiro_crew.slack.events.channel_inbound_permitted",
                new_callable=AsyncMock,
                return_value=False,
            ):
                with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                    await ev._route_message(orch, _event(), ev.SeenCache())
        hm.assert_not_called()
        assert (
            _mock_sel.log_api_access.call_args.kwargs["error"] == "channels governance policy"
        )

    @pytest.mark.asyncio
    async def test_pure_stop_is_exempt_from_governance_denial(self):
        orch = _make_orch()
        orch.sessions.has_session = MagicMock(return_value=False)
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.is_owner", return_value=True):
                with patch(
                    "kiro_crew.slack.events.channel_inbound_permitted",
                    new_callable=AsyncMock,
                    return_value=False,
                ) as gate:
                    await ev._route_message(orch, _event(text="!stop"), ev.SeenCache())
        gate.assert_not_called()
        orch.slack.post_message.assert_awaited_with("D1", "Nothing running.", "100.0")

    @pytest.mark.asyncio
    async def test_display_name_resolved_from_slack(self):
        orch = _make_orch()
        orch.slack.get_user_info = AsyncMock(return_value={"real_name": "Ada L"})
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                await ev._route_message(orch, _event(), ev.SeenCache())
                await _drain(orch)
        assert hm.await_args.kwargs["user_display_name"] == "Ada L"
        orch.channel_history.set_user_name.assert_called_with("U_OWNER", "Ada L")

    @pytest.mark.asyncio
    async def test_display_name_lookup_failure_falls_back_to_config(self):
        orch = _make_orch()
        orch._cfg.slack.allowed_users = [{"slack_id": "U_OWNER", "name": "Ada From Config"}]
        orch.slack.get_user_info = AsyncMock(side_effect=RuntimeError("no scope"))
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                await ev._route_message(orch, _event(), ev.SeenCache())
                await _drain(orch)
        assert hm.await_args.kwargs["user_display_name"] == "Ada From Config"

    @pytest.mark.asyncio
    async def test_unauthorized_sender_gets_ephemeral(self):
        orch = _make_orch()
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=False):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                await ev._route_message(orch, _event(), ev.SeenCache())
        hm.assert_not_called()
        assert "not authorized" in orch.slack.post_ephemeral.await_args[0][2]

    @pytest.mark.asyncio
    async def test_ephemeral_rejection_failure_is_swallowed(self):
        orch = _make_orch()
        orch.slack.post_ephemeral = AsyncMock(side_effect=RuntimeError("channel_not_found"))
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=False):
            await ev._route_message(orch, _event(), ev.SeenCache())

    @pytest.mark.asyncio
    async def test_duplicate_event_is_dropped(self):
        orch = _make_orch()
        seen = ev.SeenCache()
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                await ev._route_message(orch, _event(), seen)
                await _drain(orch)
                await ev._route_message(orch, _event(), seen)
                await _drain(orch)
        assert hm.await_count == 1

    @pytest.mark.asyncio
    async def test_missing_channel_history_is_reported_not_fatal(self, caplog):
        orch = _make_orch()
        orch.channel_history = None
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                with caplog.at_level("ERROR", logger="kiro_crew.slack.events"):
                    await ev._route_message(orch, _event(), ev.SeenCache())
                    await _drain(orch)
        hm.assert_awaited_once()
        assert any("channel_history not initialised" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_mention_only_text_is_dropped(self):
        orch = _make_orch()
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                await ev._route_message(
                    orch, _event(text="<@BOT1>"), ev.SeenCache(), is_mention=True
                )
                await _drain(orch)
        hm.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_message_task_creation_failure_is_contained(self):
        orch = _make_orch()
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            # `new=` (not side_effect on an autospecced AsyncMock) so the *call*
            # raises, exercising the create_task guard rather than the task body.
            with patch(
                "kiro_crew.slack.events.handle_message",
                new=MagicMock(side_effect=RuntimeError("cannot build coroutine")),
            ):
                await ev._route_message(orch, _event(), ev.SeenCache())
        assert orch._session_tasks == {}


class TestRouteMessageStopCommand:
    @pytest.mark.asyncio
    async def test_unauthorized_stop_denied(self, _mock_sel):
        orch = _make_orch()
        with patch("kiro_crew.slack.events.is_allowed_user", side_effect=[True, False]):
            with patch("kiro_crew.slack.events.is_owner", return_value=False):
                await ev._route_message(orch, _event(text="!stop"), ev.SeenCache())
        orch.slack.post_message.assert_awaited_with("D1", "⛔ Not authorized.", "100.0")
        assert _mock_sel.log_api_access.call_args.kwargs["error"] == "unauthorized sender"

    @pytest.mark.asyncio
    async def test_no_session_manager_reports_nothing_running(self, _mock_sel):
        orch = _make_orch()
        orch.sessions = None
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.is_owner", return_value=True):
                await ev._route_message(orch, _event(text="!stop"), ev.SeenCache())
        orch.slack.post_message.assert_awaited_with("D1", "Nothing running.", "100.0")
        assert _mock_sel.log_tool_invocation.call_args.kwargs["outcome"] == "no_session"

    @pytest.mark.asyncio
    async def test_active_session_stopped_and_callbacks_post(self, _mock_sel):
        orch = _make_orch()
        orch.sessions.has_session = MagicMock(return_value=True)

        async def _stop_turn(key, on_soft=None, on_hard=None):
            await on_soft()
            await on_hard()
            return "soft"

        orch.sessions.stop_turn = AsyncMock(side_effect=_stop_turn)
        active = asyncio.get_running_loop().create_future()
        orch._session_tasks = {"100.0": active}
        orch._pending_queue = {"100.0": []}
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.is_owner", return_value=True):
                await ev._route_message(orch, _event(text="!stop"), ev.SeenCache())
        posted = [c[0][1] for c in orch.slack.post_message.await_args_list]
        assert "⏹ Execution stopped." in posted
        assert "⛔ Execution stopped — session reset." in posted
        assert active.cancelled()
        assert _mock_sel.log_tool_invocation.call_args.kwargs["outcome"] == "soft"

    @pytest.mark.asyncio
    async def test_idle_stop_dismisses_stale_ephemeral(self):
        orch = _make_orch()
        orch.sessions.has_session = MagicMock(return_value=True)
        orch.sessions.stop_turn = AsyncMock(return_value="idle")
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.is_owner", return_value=True):
                await ev._route_message(orch, _event(text="!stop"), ev.SeenCache())
        orch.slack.post_message.assert_awaited_with("D1", "Nothing running.", "100.0")

    @pytest.mark.asyncio
    async def test_bang_restart_delegates_to_restart_handler(self):
        orch = _make_orch()
        captured: list[str] = []

        async def _fake_restart(o, caller, args, respond):
            captured.append(caller)
            await respond("♻️ Restarting gateway…")

        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events._handle_restart", new=_fake_restart):
                await ev._route_message(orch, _event(text="!restart"), ev.SeenCache())
        assert captured == ["U_OWNER"]
        orch.slack.post_message.assert_awaited_with("D1", "♻️ Restarting gateway…", "100.0")


class TestRouteMessageQueueing:
    @pytest.mark.asyncio
    async def test_busy_session_enqueues_and_reacts(self):
        orch = _make_orch()
        orch._session_tasks = {"100.0": MagicMock()}
        orch.sessions.enqueue = MagicMock(return_value=True)
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                await ev._route_message(orch, _event(), ev.SeenCache())
        hm.assert_not_called()
        orch.slack.add_reaction.assert_awaited_once_with(
            "D1", "100.0", "hourglass_flowing_sand"
        )

    @pytest.mark.asyncio
    async def test_busy_session_without_session_object_uses_pending_queue(self):
        orch = _make_orch()
        orch._session_tasks = {"100.0": MagicMock()}
        orch.sessions.enqueue = MagicMock(return_value=False)
        orch.slack.add_reaction = AsyncMock(side_effect=RuntimeError("rate limited"))
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            await ev._route_message(orch, _event(), ev.SeenCache())
        assert orch._pending_queue["100.0"][0][0] == "100.0"

    @pytest.mark.asyncio
    async def test_session_level_enqueue_short_circuits(self):
        orch = _make_orch()
        orch.sessions.enqueue = MagicMock(return_value=True)
        orch.slack.add_reaction = AsyncMock(side_effect=RuntimeError("rate limited"))
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                await ev._route_message(orch, _event(), ev.SeenCache())
        hm.assert_not_called()


class TestRouteMessageAttachments:
    @pytest.mark.asyncio
    async def test_voice_transcript_is_prefixed(self):
        orch = _make_orch()
        files = [{"mimetype": "audio/webm", "url_private": "https://x.invalid/a.webm"}]
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.stt_available", return_value=True):
                with patch(
                    "kiro_crew.slack.events._transcribe_with_reaction",
                    new_callable=AsyncMock,
                    return_value=["spoken"],
                ):
                    with patch(
                        "kiro_crew.slack.events.process_slack_files",
                        new_callable=AsyncMock,
                        return_value=([], []),
                    ):
                        with patch(
                            "kiro_crew.slack.events.handle_message", new_callable=AsyncMock
                        ) as hm:
                            await ev._route_message(
                                orch, _event(text="", files=files), ev.SeenCache()
                            )
                            await _drain(orch)
        body = hm.await_args[0][3]
        assert "[Voice memo transcription]" in body
        assert hm.await_args.kwargs["had_voice_input"] is True

    @pytest.mark.asyncio
    async def test_image_and_text_blocks_are_appended(self, tmp_path):
        orch = _make_orch()
        img = tmp_path / "shot.png"
        img.write_bytes(b"\x89PNG")
        files = [{"mimetype": "image/png", "url_private": "https://x.invalid/a.png"}]
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.stt_available", return_value=False):
                with patch(
                    "kiro_crew.slack.events.process_slack_files",
                    new_callable=AsyncMock,
                    return_value=([str(img)], ["file body"]),
                ):
                    with patch(
                        "kiro_crew.slack.events.handle_message", new_callable=AsyncMock
                    ) as hm:
                        await ev._route_message(
                            orch, _event(text="look", files=files), ev.SeenCache()
                        )
                        await _drain(orch)
        body = hm.await_args[0][3]
        assert str(img) in body
        assert "file body" in body
        # The temp image is unlinked by the done-callback once the turn finishes.
        assert not img.exists()

    @pytest.mark.asyncio
    async def test_no_recoverable_text_cleans_up_temp_images(self, tmp_path):
        orch = _make_orch()
        img = tmp_path / "empty.png"
        img.write_bytes(b"\x89PNG")
        files = [{"mimetype": "image/png", "url_private": "https://x.invalid/a.png"}]
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.stt_available", return_value=False):
                with patch(
                    "kiro_crew.slack.events.process_slack_files",
                    new_callable=AsyncMock,
                    return_value=([], []),
                ):
                    with patch(
                        "kiro_crew.slack.events.handle_message", new_callable=AsyncMock
                    ) as hm:
                        # A missing temp path must not raise from the cleanup loop.
                        img.unlink()
                        await ev._route_message(
                            orch, _event(text="", files=files), ev.SeenCache()
                        )
        hm.assert_not_called()

    @pytest.mark.asyncio
    async def test_forwarded_attachment_text_is_recovered(self):
        orch = _make_orch()
        event = _event(
            text="This message contains interactive elements.",
            attachments=[{"is_share": True, "text": "the real body"}],
        )
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                await ev._route_message(orch, event, ev.SeenCache())
                await _drain(orch)
        assert hm.await_args[0][3] == "the real body"


class TestRouteMessageTransportPath:
    @pytest.mark.asyncio
    async def test_transport_path_dispatches_and_drains_queue(self):
        orch = _make_orch(use_transport=True)
        queued = [("101.0", "next up", {"channel": "C1"})]
        orch.sessions.dequeue = MagicMock(side_effect=lambda _k: queued.pop(0) if queued else None)
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch(
                "kiro_crew.slack.events.handle_message_transport", new_callable=AsyncMock
            ) as transport:
                with patch(
                    "kiro_crew.slack.events._dispatch_queued", new_callable=AsyncMock
                ) as dispatch:
                    with patch.object(
                        KiroCrewConfig, "load", return_value=KiroCrewConfig()
                    ):
                        await ev._route_message(orch, _event(), ev.SeenCache())
                        await _drain(orch)
                        await _drain(orch)
        transport.assert_awaited_once()
        dispatch.assert_awaited_once()
        assert orch._session_tasks == {}

    @pytest.mark.asyncio
    async def test_transport_drain_falls_back_to_pending_queue(self):
        orch = _make_orch(use_transport=True)
        orch.sessions.dequeue = MagicMock(return_value=None)
        orch._pending_queue = {"100.0": [("101.0", "pending", {"channel": "C1"})]}
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch(
                "kiro_crew.slack.events.handle_message_transport", new_callable=AsyncMock
            ):
                with patch(
                    "kiro_crew.slack.events._dispatch_queued", new_callable=AsyncMock
                ) as dispatch:
                    with patch.object(
                        KiroCrewConfig, "load", return_value=KiroCrewConfig()
                    ):
                        await ev._route_message(orch, _event(), ev.SeenCache())
                        await _drain(orch)
                        await _drain(orch)
        dispatch.assert_awaited_once()
        assert "100.0" not in orch._pending_queue

    @pytest.mark.asyncio
    async def test_transport_drain_failure_is_logged(self, caplog):
        orch = _make_orch(use_transport=True)
        orch.sessions.dequeue = MagicMock(side_effect=RuntimeError("queue corrupt"))
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch(
                "kiro_crew.slack.events.handle_message_transport", new_callable=AsyncMock
            ):
                with patch.object(KiroCrewConfig, "load", return_value=KiroCrewConfig()):
                    with caplog.at_level("ERROR", logger="kiro_crew.slack.events"):
                        await ev._route_message(orch, _event(), ev.SeenCache())
                        await _drain(orch)
        assert any("_on_transport_done drain failed" in r.message for r in caplog.records)


class TestRouteMessageNativeDrain:
    @pytest.mark.asyncio
    async def test_native_done_callback_drains_session_queue(self):
        orch = _make_orch()
        queued = [("101.0", "next up", {"channel": "C1"})]
        orch.sessions.dequeue = MagicMock(side_effect=lambda _k: queued.pop(0) if queued else None)
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock):
                with patch(
                    "kiro_crew.slack.events._dispatch_queued", new_callable=AsyncMock
                ) as dispatch:
                    await ev._route_message(orch, _event(), ev.SeenCache())
                    await _drain(orch)
                    await _drain(orch)
        dispatch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_native_done_callback_drain_failure_is_logged(self, caplog):
        orch = _make_orch()
        orch.sessions.dequeue = MagicMock(side_effect=RuntimeError("queue corrupt"))
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock):
                with caplog.at_level("ERROR", logger="kiro_crew.slack.events"):
                    await ev._route_message(orch, _event(), ev.SeenCache())
                    await _drain(orch)
        assert any("_on_done drain failed" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_native_drain_falls_back_to_pending_queue(self):
        orch = _make_orch()
        orch.sessions.dequeue = MagicMock(return_value=None)
        orch._pending_queue = {"100.0": [("101.0", "pending", {"channel": "D1"})]}
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock):
                with patch(
                    "kiro_crew.slack.events._dispatch_queued", new_callable=AsyncMock
                ) as dispatch:
                    await ev._route_message(orch, _event(), ev.SeenCache())
                    await _drain(orch)
                    await _drain(orch)
        dispatch.assert_awaited_once()
        assert "100.0" not in orch._pending_queue


class TestRouteMessageEnterpriseAndObserve:
    @pytest.mark.asyncio
    async def test_enterprise_origin_mismatch_rejects(self, _mock_sel):
        orch = _make_orch()
        ctx = MagicMock()
        ctx.return_value.slack_gate.check_message_origin.return_value = False
        with patch("kiro_crew.slack.events.current_context", ctx):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                await ev._route_message(orch, _event(), ev.SeenCache())
        hm.assert_not_called()
        assert (
            _mock_sel.log_api_access.call_args.kwargs["error"] == "enterprise_origin_mismatch"
        )

    @pytest.mark.asyncio
    async def test_observe_records_history_then_drops_plain_message(self, _mock_sel):
        orch = _make_orch(dm_activation=ACTIVATION_OBSERVE)
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                await ev._route_message(orch, _event(), ev.SeenCache())
        hm.assert_not_called()
        orch.channel_history.push.assert_called_once()
        assert "activation=observe" in _mock_sel.log_api_access.call_args.kwargs["error"]

    @pytest.mark.asyncio
    async def test_observe_processes_mention(self):
        orch = _make_orch(dm_activation=ACTIVATION_OBSERVE)
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                await ev._route_message(
                    orch, _event(text="<@BOT1> hi"), ev.SeenCache(), is_mention=True
                )
                await _drain(orch)
        hm.assert_awaited_once()
        # Observe mode pushed history up-front, so the post-activation push is skipped.
        assert orch.channel_history.push.call_count == 1

    @pytest.mark.asyncio
    async def test_observe_follows_active_thread(self):
        orch = _make_orch(dm_activation=ACTIVATION_OBSERVE)
        orch.sessions.has_session = MagicMock(return_value=True)
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                await ev._route_message(orch, _event(thread_ts="99.0"), ev.SeenCache())
                await _drain(orch)
        hm.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_observe_skips_history_for_unauthorized_sender(self):
        orch = _make_orch(dm_activation=ACTIVATION_OBSERVE)
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=False):
            await ev._route_message(orch, _event(), ev.SeenCache())
        orch.channel_history.push.assert_not_called()


class TestRouteMessageTempCleanup:
    @pytest.mark.asyncio
    async def test_missing_temp_image_does_not_raise_on_cleanup(self, tmp_path):
        orch = _make_orch()
        ghost = tmp_path / "already-gone.png"
        files = [{"mimetype": "image/png", "url_private": "https://x.invalid/a.png"}]
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.stt_available", return_value=False):
                with patch(
                    "kiro_crew.slack.events.process_slack_files",
                    new_callable=AsyncMock,
                    return_value=([str(ghost)], []),
                ):
                    with patch(
                        "kiro_crew.slack.events.handle_message", new_callable=AsyncMock
                    ) as hm:
                        await ev._route_message(
                            orch, _event(text="see", files=files), ev.SeenCache()
                        )
                        await _drain(orch)
        hm.assert_awaited_once()


class TestDispatchQueued:
    @pytest.mark.asyncio
    async def test_temp_paths_are_unlinked_after_the_turn(self, tmp_path):
        orch = _make_orch()
        img = tmp_path / "queued.png"
        img.write_bytes(b"\x89PNG")
        kwargs = {"channel": "D1", "thread_ts": None, "image_temp_paths": [str(img)]}
        with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
            await ev._dispatch_queued(orch, "100.0", "101.0", "queued text", kwargs)
        hm.assert_awaited_once()
        assert not img.exists()

    @pytest.mark.asyncio
    async def test_missing_temp_path_is_tolerated(self, tmp_path):
        orch = _make_orch()
        kwargs = {"channel": "D1", "image_temp_paths": [str(tmp_path / "nope.png")]}
        with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock):
            await ev._dispatch_queued(orch, "100.0", "101.0", "queued text", kwargs)


class TestHandleStatus:
    @pytest.mark.asyncio
    async def test_reports_stats_summary_with_identity_line(self):
        respond = AsyncMock()
        with patch("kiro_crew.slack.events.Stats") as stats:
            stats.return_value.summary.return_value = "turns: 3"
            await ev._handle_status(_make_orch(), "U_OWNER", "", respond)
        assert respond.call_args[0][0].startswith("turns: 3")


class TestHandleSlashRespondBlocks:
    @pytest.mark.asyncio
    async def test_blocks_are_forwarded_in_the_response_body(self):
        orch = _make_orch()
        posted: list[dict] = []
        blocks = [{"type": "divider"}]

        async def _handler(o, caller, args, respond):
            await respond("with blocks", blocks=blocks)

        ev.register_slash_command("zzcovblocks", _handler, "coverage probe")
        try:
            payload = {
                "command": "/kirocrew",
                "user_id": "U_OWNER",
                "text": "zzcovblocks",
                "response_url": "https://hooks.example.invalid/x",
            }
            with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
                with _capture_respond(posted):
                    await ev._handle_slash(orch, payload)
                    await _drain(orch)
        finally:
            ev.SLASH_REGISTRY.pop("zzcovblocks", None)
        assert posted[0]["blocks"] == blocks
        assert posted[0]["response_type"] == "ephemeral"

    @pytest.mark.asyncio
    async def test_without_response_url_nothing_is_posted(self):
        orch = _make_orch()
        posted: list[dict] = []

        async def _handler(o, caller, args, respond):
            await respond("dropped")

        ev.register_slash_command("zzcovnourl", _handler, "coverage probe")
        try:
            payload = {"command": "/kirocrew", "user_id": "U_OWNER", "text": "zzcovnourl"}
            with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
                with _capture_respond(posted, response_url=""):
                    await ev._handle_slash(orch, payload)
                    await _drain(orch)
        finally:
            ev.SLASH_REGISTRY.pop("zzcovnourl", None)
        assert posted == []
