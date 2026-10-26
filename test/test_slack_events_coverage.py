"""Coverage tests for :mod:`kiro_crew.slack.events`.

Focus areas that the existing Slack suites leave untouched:

* the built-in ``/kirocrew <sub-command>`` handlers (dashboard, agent, voice,
  yolo, config, users, channels) and their guard branches,
* :func:`init_socket_mode` plus the ``_on_event`` Socket Mode dispatcher it
  installs,
* the ``_handle_slash`` fallbacks (``@user`` / ``#channel`` / help),
* the pure Block Kit text-recovery helpers.

The voice-transcription, ``message_deleted`` and approval-mode helpers live in
``test_slack_events_coverage_message_helpers.py``; selected ``_route_message`` guard
branches (activation=off passthrough, channels-governance denial, display-name
fallback) in ``..._route_guards.py``; the rest of the route (``!stop``, queueing,
attachments, the transport path), the queued dispatch, ``/status`` and slash reply
blocks in ``..._route_flow.py``. Shared fixtures live in
``slack_events_coverage_helpers.py``.

Conventions mirror ``test_channel_activation.py`` and ``test_restart_command.py``:
a ``MagicMock`` stand-in for ``GatewayOrchestrator``, ``AsyncMock`` for the Slack
client, and ``kiro_crew.slack.events.sel`` patched so no audit file is written.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_events_coverage_helpers import _event  # noqa: F401 -- compaction_stop_declined_helpers
from slack_events_coverage_helpers import _fresh_trusted_bot_turn_ledger  # noqa: F401
from slack_events_coverage_helpers import _isolated_home_fixture  # noqa: F401
from slack_events_coverage_helpers import _mock_sel_fixture  # noqa: F401
from slack_events_coverage_helpers import _capture_respond, _drain, _make_orch

from kiro_crew.config.loader import ACTIVATION_ALWAYS, ChannelConfig
from kiro_crew.slack import events as ev

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

    @pytest.mark.asyncio
    async def test_owner_registry_retains_detached_work_until_completion(self):
        orch = _make_orch()
        started = asyncio.Event()
        release = asyncio.Event()

        async def _work() -> None:
            started.set()
            await release.wait()

        task = ev._spawn_tracked(_work(), owner=orch)
        await started.wait()
        assert task in ev._bg_tasks
        assert task in orch._handler_tasks

        release.set()
        await task
        await asyncio.sleep(0)
        assert task not in ev._bg_tasks
        assert task not in orch._handler_tasks


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
        assert orch.slack.views_open.await_args.kwargs["trigger_id"] == "T3"
        assert "C1" in str(orch.slack.views_open.await_args.kwargs["view"])
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
    """Patch every module-global ``init_socket_mode`` reaches out to.

    ``real_client=True`` leaves ``WSSocketModeClient`` and ``AsyncWebClient``
    UNPATCHED so the real constructor runs — the loop-requirement regression
    tests need that, because a mocked constructor is exactly how a boot crash
    in this path hides from CI.
    """

    def __init__(self, validate: bool = True, real_client: bool = False):
        self._validate = validate
        self._real_client = real_client
        self._stack: list = []
        self.setters: dict[str, MagicMock] = {}
        self.ctx = MagicMock()
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
        if not self._real_client:
            for target, new in (
                ("kiro_crew.slack.events.WSSocketModeClient", self.client_cls),
                ("kiro_crew.slack.events.AsyncWebClient", MagicMock()),
            ):
                p = patch(target, new)
                p.start()
                self._stack.append(p)
        self.ctx.return_value.slack_gate.validate_enterprise.return_value = self._validate
        p = patch("kiro_crew.slack.events.current_context", self.ctx)
        p.start()
        self._stack.append(p)
        return self

    def __exit__(self, *exc: object) -> None:
        for p in reversed(self._stack):
            p.stop()


class TestInitSocketMode:
    @pytest.mark.asyncio
    async def test_disabled_gateway_is_a_noop(self):
        orch = _socket_orch()
        orch._slack_enabled = False
        with _SocketPatches() as sp:
            await ev.init_socket_mode(orch, ev.SeenCache())
        assert orch._socket_client is None
        sp.setters["set_owner_id"].assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_owner_disables_slack(self):
        orch = _socket_orch()
        orch._owner_id = ""
        with _SocketPatches():
            await ev.init_socket_mode(orch, ev.SeenCache())
        assert orch._slack_enabled is False
        assert orch.slack is None

    @pytest.mark.asyncio
    async def test_enterprise_validation_failure_disables_slack(self):
        orch = _socket_orch()
        with _SocketPatches(validate=False):
            await ev.init_socket_mode(orch, ev.SeenCache())
        assert orch._slack_enabled is False
        assert orch.slack is None
        assert orch._socket_client is None

    @pytest.mark.asyncio
    async def test_success_installs_listener_and_shares_state(self):
        orch = _socket_orch()
        orch.dashboard_state = MagicMock()
        with _SocketPatches() as sp:
            await ev.init_socket_mode(orch, ev.SeenCache())
        sp.setters["set_owner_id"].assert_called_once_with("U_OWNER")
        sp.setters["set_orch_cfg"].assert_called_once_with(orch._cfg)
        sp.setters["set_dashboard_state"].assert_called_once_with(orch.dashboard_state)
        sp.setters["set_yolo_mode"].assert_not_called()
        assert len(orch._socket_client.socket_mode_request_listeners) == 1

    @pytest.mark.asyncio
    async def test_dangerously_skip_permissions_enables_yolo(self):
        orch = _socket_orch()
        orch._cfg.agent.dangerously_skip_permissions = True
        with _SocketPatches() as sp:
            await ev.init_socket_mode(orch, ev.SeenCache())
        sp.setters["set_yolo_mode"].assert_called_once_with(True)

    # ── Loop-requirement pins ──
    #
    # WSSocketModeClient.__init__ ends in ``asyncio.ensure_future``, which
    # raises ``RuntimeError: There is no current event loop`` in any thread
    # without a running loop.  Offloading the WHOLE of init_socket_mode via
    # ``asyncio.to_thread`` therefore crashes every Slack-enabled gateway at
    # boot, and a test on this path that mocks either init_socket_mode itself
    # or WSSocketModeClient hides it.  The tests below
    # close that hole: the constructor runs REAL, and the call form the
    # gateway uses is pinned at the source level.

    @pytest.mark.asyncio
    async def test_real_client_is_constructed_on_the_running_loop(self):
        """Exercise the REAL WSSocketModeClient constructor (no mock).

        Fails on the pre-fix code in both directions: the sync function
        cannot be awaited, and running it whole in a worker thread (the old
        gateway call form) raises RuntimeError from ensure_future.
        """
        orch = _socket_orch()
        with _SocketPatches(real_client=True):
            await ev.init_socket_mode(orch, ev.SeenCache())
        client = orch._socket_client
        try:
            assert client is not None
            # The processor task landed on THIS loop — the loop requirement.
            assert client.message_processor.get_loop() is asyncio.get_running_loop()
            assert len(client.socket_mode_request_listeners) == 1
        finally:
            if client is not None:
                client.message_processor.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await client.message_processor

    @pytest.mark.asyncio
    async def test_blocking_calls_run_off_the_loop_thread(self):
        """The YOLO grant and enterprise auth.test stay off the loop.

        Blocking calls must stay off the loop thread; the code offloads them
        per-call.
        """
        loop_thread = threading.current_thread()
        seen_threads: dict[str, threading.Thread] = {}

        orch = _socket_orch()
        orch._cfg.agent.dangerously_skip_permissions = True
        with _SocketPatches() as sp:
            sp.setters["set_yolo_mode"].side_effect = lambda *_a: seen_threads.__setitem__(
                "yolo", threading.current_thread()
            )

            def _validate(*_a, **_k):
                seen_threads["enterprise"] = threading.current_thread()
                return True

            sp.ctx.return_value.slack_gate.validate_enterprise.side_effect = _validate
            await ev.init_socket_mode(orch, ev.SeenCache())

        assert seen_threads["yolo"] is not loop_thread
        assert seen_threads["enterprise"] is not loop_thread

    def test_gateway_awaits_init_socket_mode_on_the_loop(self):
        """Source pin: the gateway call site awaits the coroutine directly.

        ``asyncio.to_thread(init_socket_mode, ...)`` is the exact spelling
        that shipped the boot crash; refuse any reappearance of it.
        """
        assert asyncio.iscoroutinefunction(ev.init_socket_mode)
        slack_dir = Path(ev.__file__).resolve().parent
        gateway_src = "\n".join(
            path.read_text(encoding="utf-8")
            for path in [
                slack_dir / "gateway.py",
                *sorted((slack_dir / "gateway_runtime").glob("*.py")),
            ]
        )
        assert "await init_socket_mode(self, seen)" in gateway_src
        assert not re.search(r"to_thread\(\s*init_socket_mode", gateway_src)


async def _install_on_event(orch: MagicMock, seen: ev.SeenCache):
    with _SocketPatches():
        await ev.init_socket_mode(orch, seen)
    return orch._socket_client.socket_mode_request_listeners[0]


def _req(req_type: str, payload: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(type=req_type, payload=payload or {}, envelope_id="env-1")


def _client(ack_fails: bool = False) -> MagicMock:
    client = MagicMock()
    client.send_socket_mode_response = AsyncMock(
        side_effect=RuntimeError("socket not ready") if ack_fails else None
    )
    return client


def _acked_envelopes(client: MagicMock) -> list[str]:
    return [c.args[0].envelope_id for c in client.send_socket_mode_response.await_args_list]


class TestOnEventDispatch:
    @pytest.mark.asyncio
    async def test_ack_failure_short_circuits(self):
        orch = _socket_orch()
        on_event = await _install_on_event(orch, ev.SeenCache())
        with patch(
            "kiro_crew.slack.events.dispatch_interactive", new_callable=AsyncMock
        ) as dispatch:
            client = _client(ack_fails=True)
            await on_event(client, _req("interactive"))
        client.send_socket_mode_response.assert_awaited_once()
        dispatch.assert_not_called()
        assert orch._handler_tasks == set()

    @pytest.mark.asyncio
    async def test_interactive_is_dispatched(self):
        orch = _socket_orch()
        on_event = await _install_on_event(orch, ev.SeenCache())
        with patch(
            "kiro_crew.slack.events.dispatch_interactive", new_callable=AsyncMock
        ) as dispatch:
            await on_event(_client(), _req("interactive", {"action": "x"}))
            await _drain(orch)
        dispatch.assert_awaited_once_with({"action": "x"})

    @pytest.mark.asyncio
    async def test_update_pause_refuses_before_interaction_ack(self):
        orch = _socket_orch()
        orch.sessions.reserve_inbound_callback = MagicMock(return_value=None)
        on_event = await _install_on_event(orch, ev.SeenCache())
        client = _client()
        with patch(
            "kiro_crew.slack.events.dispatch_interactive", new_callable=AsyncMock
        ) as dispatch:
            await on_event(client, _req("interactive", {"action": "x"}))
            await _drain(orch)

        client.send_socket_mode_response.assert_not_awaited()
        dispatch.assert_not_awaited()
        orch.sessions.reserve_inbound_callback.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_slash_command_is_dispatched(self):
        orch = _socket_orch()
        on_event = await _install_on_event(orch, ev.SeenCache())
        with patch("kiro_crew.slack.events._handle_slash", new_callable=AsyncMock) as slash:
            await on_event(_client(), _req("slash_commands", {"command": "/kirocrew"}))
            await _drain(orch)
        slash.assert_awaited_once_with(orch, {"command": "/kirocrew"})

    @pytest.mark.asyncio
    async def test_unknown_envelope_type_ignored(self):
        orch = _socket_orch()
        on_event = await _install_on_event(orch, ev.SeenCache())
        client = _client()
        with patch("kiro_crew.slack.events._route_message", new_callable=AsyncMock) as route:
            await on_event(client, _req("hello"))
        route.assert_not_called()
        assert _acked_envelopes(client) == ["env-1"]

    @pytest.mark.asyncio
    async def test_member_joined_channel_routed_to_prompt(self):
        orch = _socket_orch()
        on_event = await _install_on_event(orch, ev.SeenCache())
        payload = {"event": {"type": "member_joined_channel", "channel": "C1"}}
        with patch("kiro_crew.slack.events._maybe_prompt_owner") as prompt:
            await on_event(_client(), _req("events_api", payload))
        prompt.assert_called_once_with(orch, payload["event"])

    @pytest.mark.asyncio
    async def test_home_tab_published_for_allowed_user(self, _mock_sel):
        orch = _socket_orch()
        on_event = await _install_on_event(orch, ev.SeenCache())
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
        on_event = await _install_on_event(orch, ev.SeenCache())
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
        on_event = await _install_on_event(orch, ev.SeenCache())
        client = _client()
        payload = {"event": {"type": "app_home_opened", "tab": "messages", "user": "U_OWNER"}}
        with patch("kiro_crew.slack.events._publish_home_tab", new_callable=AsyncMock) as publish:
            await on_event(client, _req("events_api", payload))
        publish.assert_not_called()
        assert _acked_envelopes(client) == ["env-1"]

    @pytest.mark.asyncio
    async def test_non_message_event_ignored(self):
        orch = _socket_orch()
        on_event = await _install_on_event(orch, ev.SeenCache())
        client = _client()
        payload = {"event": {"type": "reaction_added"}}
        with patch("kiro_crew.slack.events._route_message", new_callable=AsyncMock) as route:
            await on_event(client, _req("events_api", payload))
        route.assert_not_called()
        assert _acked_envelopes(client) == ["env-1"]

    @pytest.mark.asyncio
    async def test_message_deleted_is_delegated(self):
        orch = _socket_orch()
        on_event = await _install_on_event(orch, ev.SeenCache())
        event = {"type": "message", "subtype": "message_deleted", "deleted_ts": "1.0"}
        with patch(
            "kiro_crew.slack.events._handle_message_deleted", new_callable=AsyncMock
        ) as deleted:
            await on_event(_client(), _req("events_api", {"event": event}))
        deleted.assert_awaited_once_with(orch, event)

    @pytest.mark.asyncio
    async def test_unhandled_subtype_ignored(self):
        orch = _socket_orch()
        on_event = await _install_on_event(orch, ev.SeenCache())
        client = _client()
        event = {"type": "message", "subtype": "channel_join"}
        with patch("kiro_crew.slack.events._route_message", new_callable=AsyncMock) as route:
            await on_event(client, _req("events_api", {"event": event}))
        route.assert_not_called()
        assert _acked_envelopes(client) == ["env-1"]

    @pytest.mark.asyncio
    async def test_envelope_team_id_overrides_event_team(self):
        orch = _socket_orch()
        on_event = await _install_on_event(orch, ev.SeenCache())
        event = {"type": "message", "user": "U1", "channel": "D1", "text": "hi", "team": "T_EVIL"}
        payload = {"event": event, "team_id": "T_REAL"}
        with patch("kiro_crew.slack.events._route_message", new_callable=AsyncMock) as route:
            await on_event(_client(), _req("events_api", payload))
        assert route.await_args[0][1]["team"] == "T_REAL"
        assert route.await_args.kwargs["is_mention"] is False

    @pytest.mark.asyncio
    async def test_app_mention_sets_is_mention(self):
        orch = _socket_orch()
        on_event = await _install_on_event(orch, ev.SeenCache())
        event = {"type": "app_mention", "user": "U1", "channel": "C1", "team": "T1"}
        with patch("kiro_crew.slack.events._route_message", new_callable=AsyncMock) as route:
            await on_event(_client(), _req("events_api", {"event": event}))
        assert route.await_args.kwargs["is_mention"] is True

    @pytest.mark.asyncio
    async def test_missing_team_id_is_rejected(self, _mock_sel):
        orch = _socket_orch()
        on_event = await _install_on_event(orch, ev.SeenCache())
        event = {"type": "message", "user": "U1", "channel": "C1", "text": "hi"}
        with patch("kiro_crew.slack.events._route_message", new_callable=AsyncMock) as route:
            await on_event(_client(), _req("events_api", {"event": event}))
        route.assert_not_called()
        assert _mock_sel.log_api_access.call_args.kwargs["error"] == "missing_team_id"

    @pytest.mark.asyncio
    async def test_bot_message_denied_when_allowlist_unset(self, _mock_sel):
        """Empty/unset slack.trusted_bot_ids drops every bot-authored event (default)."""
        orch = _socket_orch()
        on_event = await _install_on_event(orch, ev.SeenCache())
        event = {"type": "message", "bot_id": "B_EVIL", "channel": "C1", "text": "hi", "team": "T1"}
        with patch("kiro_crew.slack.events._route_message", new_callable=AsyncMock) as route:
            await on_event(_client(), _req("events_api", {"event": event}))
        route.assert_not_called()
        kw = _mock_sel.log_api_access.call_args.kwargs
        assert kw["error"] == "untrusted_bot"
        assert kw["outcome"] == "denied"
        assert kw["caller"] == "B_EVIL"

    @pytest.mark.asyncio
    async def test_bot_not_in_allowlist_still_denied(self, _mock_sel):
        """Fail-closed pin: a configured allowlist admits ONLY exact members."""
        orch = _socket_orch()
        orch._cfg.slack.trusted_bot_ids = {"B_TRUSTED"}
        on_event = await _install_on_event(orch, ev.SeenCache())
        event = {"type": "message", "bot_id": "B_EVIL", "channel": "C1", "text": "hi", "team": "T1"}
        with patch("kiro_crew.slack.events._route_message", new_callable=AsyncMock) as route:
            await on_event(_client(), _req("events_api", {"event": event}))
        route.assert_not_called()
        assert _mock_sel.log_api_access.call_args.kwargs["error"] == "untrusted_bot"

    @pytest.mark.parametrize(
        "bot_id",
        [
            # A bot_id in slack.trusted_bot_ids is routed with from_trusted_bot=True.
            pytest.param("B_TRUSTED", id="test_trusted_bot_routed_with_flag"),
            # The self-id guard does not block a legitimate peer bot.
            pytest.param("B_PEER", id="test_peer_bot_still_trusted_when_self_id_known"),
        ],
    )
    @pytest.mark.asyncio
    async def test_listed_bot_routed_with_trusted_flag(self, bot_id):
        orch = _socket_orch()
        orch._cfg.slack.trusted_bot_ids = {bot_id}
        on_event = await _install_on_event(orch, ev.SeenCache())
        event = {
            "type": "message",
            "bot_id": bot_id,
            "channel": "C1",
            "text": "hi",
            "team": "T1",
        }
        with patch("kiro_crew.slack.enterprise.validated_self_bot_id", return_value="B_SELF"):
            with patch("kiro_crew.slack.events._route_message", new_callable=AsyncMock) as route:
                await on_event(_client(), _req("events_api", {"event": event}))
        route.assert_awaited_once()
        assert route.await_args.kwargs["from_trusted_bot"] is True

    @pytest.mark.parametrize(
        ("bot_id", "self_id", "error"),
        [
            # Fail-closed pin: no verified self identity (auth.test failed) trusts nobody.
            pytest.param(
                "B_TRUSTED",
                "",
                "trusted_bot_requires_verified_self_id",
                id="test_allowlisted_peer_denied_when_self_id_unknown",
            ),
            # Self-reply loop guard: the gateway's own bot id is denied even if allowlisted.
            pytest.param(
                "B_SELF",
                "B_SELF",
                "own_bot_id_never_trusted",
                id="test_own_bot_id_never_trusted_even_when_listed",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_listed_bot_denied_without_a_distinct_verified_self_id(
        self, _mock_sel, bot_id, self_id, error
    ):
        orch = _socket_orch()
        orch._cfg.slack.trusted_bot_ids = {bot_id}
        on_event = await _install_on_event(orch, ev.SeenCache())
        event = {"type": "message", "bot_id": bot_id, "channel": "C1", "text": "hi", "team": "T1"}
        with patch("kiro_crew.slack.enterprise.validated_self_bot_id", return_value=self_id):
            with patch("kiro_crew.slack.events._route_message", new_callable=AsyncMock) as route:
                await on_event(_client(), _req("events_api", {"event": event}))
        route.assert_not_called()
        kw = _mock_sel.log_api_access.call_args.kwargs
        assert kw["outcome"] == "denied"
        assert kw["error"] == error
        assert kw["caller"] == bot_id

    @pytest.mark.asyncio
    async def test_trusted_bot_message_subtype_routed(self):
        """subtype=bot_message (the standard bot-authored shape) passes for a trusted bot."""
        orch = _socket_orch()
        orch._cfg.slack.trusted_bot_ids = {"B_TRUSTED"}
        on_event = await _install_on_event(orch, ev.SeenCache())
        event = {
            "type": "message",
            "bot_id": "B_TRUSTED",
            "subtype": "bot_message",
            "channel": "C1",
            "text": "hi",
            "team": "T1",
        }
        with patch("kiro_crew.slack.enterprise.validated_self_bot_id", return_value="B_SELF"):
            with patch("kiro_crew.slack.events._route_message", new_callable=AsyncMock) as route:
                await on_event(_client(), _req("events_api", {"event": event}))
        route.assert_awaited_once()
        assert route.await_args.kwargs["from_trusted_bot"] is True

    @pytest.mark.asyncio
    async def test_untrusted_bot_message_subtype_denied_with_audit(self, _mock_sel):
        """subtype=bot_message from an untrusted bot is audit-denied, not silently dropped."""
        orch = _socket_orch()
        orch._cfg.slack.trusted_bot_ids = {"B_TRUSTED"}
        on_event = await _install_on_event(orch, ev.SeenCache())
        event = {
            "type": "message",
            "bot_id": "B_EVIL",
            "subtype": "bot_message",
            "channel": "C1",
            "text": "hi",
            "team": "T1",
        }
        with patch("kiro_crew.slack.events._route_message", new_callable=AsyncMock) as route:
            await on_event(_client(), _req("events_api", {"event": event}))
        route.assert_not_called()
        kw = _mock_sel.log_api_access.call_args.kwargs
        assert kw["error"] == "untrusted_bot"
        assert kw["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_trusted_bot_other_subtype_still_dropped(self):
        """Trust does not exempt non-bot_message subtypes from the subtype gate."""
        orch = _socket_orch()
        orch._cfg.slack.trusted_bot_ids = {"B_TRUSTED"}
        on_event = await _install_on_event(orch, ev.SeenCache())
        client = _client()
        event = {
            "type": "message",
            "bot_id": "B_TRUSTED",
            "subtype": "message_changed",
            "channel": "C1",
            "text": "hi",
            "team": "T1",
        }
        with patch("kiro_crew.slack.enterprise.validated_self_bot_id", return_value="B_SELF"):
            with patch("kiro_crew.slack.events._route_message", new_callable=AsyncMock) as route:
                await on_event(client, _req("events_api", {"event": event}))
        route.assert_not_called()
        assert _acked_envelopes(client) == ["env-1"]

    @pytest.mark.asyncio
    async def test_human_message_routed_without_trusted_flag(self):
        """A human-authored event never carries from_trusted_bot=True."""
        orch = _socket_orch()
        orch._cfg.slack.trusted_bot_ids = {"B_TRUSTED"}
        on_event = await _install_on_event(orch, ev.SeenCache())
        event = {"type": "message", "user": "U1", "channel": "C1", "text": "hi", "team": "T1"}
        with patch("kiro_crew.slack.events._route_message", new_callable=AsyncMock) as route:
            await on_event(_client(), _req("events_api", {"event": event}))
        route.assert_awaited_once()
        assert route.await_args.kwargs["from_trusted_bot"] is False


# ---------------------------------------------------------------------------
# _handle_slash dispatcher
# ---------------------------------------------------------------------------


class TestHandleSlash:
    @pytest.mark.asyncio
    async def test_foreign_command_ignored(self, _mock_sel):
        orch = _make_orch()
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True) as allowed:
            await ev._handle_slash(orch, {"command": "/other", "user_id": "U_OWNER"})
        _mock_sel.log_api_access.assert_not_called()
        # Not a manifest alias of ours either, so not even the owner hint is spawned.
        allowed.assert_not_called()
        assert orch._handler_tasks == set()

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
        assert session.post.await_args.args == ("https://hooks.example.invalid/x",)
        body = session.post.await_args.kwargs["json"]
        assert body["response_type"] == "ephemeral"
        assert "*Available commands:*" in body["text"]


# ---------------------------------------------------------------------------
# Block Kit text recovery (pure helpers)
# ---------------------------------------------------------------------------


class TestRenderRichTextElement:
    @pytest.mark.parametrize(
        "element,expected",
        [
            ({"type": "text", "text": "hello"}, "hello"),
            (
                {"type": "link", "text": "site", "url": "https://a.invalid"},
                "site (https://a.invalid)",
            ),
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
            {
                "type": "rich_text",
                "elements": ["bad", {"type": "rich_text_section", "elements": "bad"}],
            },
            {
                "type": "rich_text",
                "elements": [{"type": "rich_text_list", "elements": [{"elements": "bad"}]}],
            },
            {"type": "divider"},
        ]
        assert ev._extract_blocks_text(blocks) == ""

    def test_result_is_capped(self):
        long_text = "x" * (ev._MAX_RECOVERED_TEXT_CHARS + 500)
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": long_text}}]
        assert len(ev._extract_blocks_text(blocks)) == ev._MAX_RECOVERED_TEXT_CHARS

    def test_explicit_limit_cannot_bypass_the_global_cap(self):
        class _ExplodingBlock(dict):
            def get(self, key, default=None):
                raise AssertionError("block inspected after the global cap")

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "x" * (ev._MAX_RECOVERED_TEXT_CHARS + 4000),
                },
            },
            _ExplodingBlock(),
        ]
        recovered = ev._extract_blocks_text(blocks, ev._MAX_RECOVERED_TEXT_CHARS + 4000)
        assert recovered == "x" * ev._MAX_RECOVERED_TEXT_CHARS

    def test_limit_stops_the_traversal_before_a_later_block(self):
        class _ExplodingBlock(dict):
            def get(self, key, default=None):
                raise AssertionError("block inspected after the budget was spent")

        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": "x" * 10}},
            _ExplodingBlock(),
        ]
        assert ev._extract_blocks_text(blocks, 10) == "x" * 10
        assert ev._extract_blocks_text(blocks, 4) == "xxxx"

    def test_limit_stops_inline_rendering_before_a_later_element(self):
        class _ExplodingElement(dict):
            def get(self, key, default=None):
                raise AssertionError("inline element rendered after the budget was spent")

        blocks = [
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [
                            {"type": "text", "text": "ab" + "x" * 100},
                            _ExplodingElement(),
                        ],
                    }
                ],
            }
        ]
        assert ev._extract_blocks_text(blocks, 2) == "ab"

    def test_zero_limit_reads_nothing(self):
        class _ExplodingBlocks(list):
            def __iter__(self):
                raise AssertionError("blocks traversed with no budget")

        assert ev._extract_blocks_text(_ExplodingBlocks([{"type": "divider"}]), 0) == ""
        assert ev._extract_blocks_text(_ExplodingBlocks([{"type": "divider"}]), -1) == ""

    def test_limit_keeps_the_joined_line_shape(self):
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": "abc"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "def"}},
        ]
        assert ev._extract_blocks_text(blocks) == "abc\ndef"
        assert ev._extract_blocks_text(blocks, 5) == "abc\nd"
        # A budget that ends on the separator leaves no dangling newline.
        assert ev._extract_blocks_text(blocks, 4) == "abc"

    def test_leading_whitespace_does_not_spend_the_budget(self):
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": "   "}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "  " + "x" * 20}},
        ]
        assert ev._extract_blocks_text(blocks, 3) == "xxx"

    def test_trusted_section_fields_follow_section_text(self):
        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "summary"},
                "fields": [
                    {"type": "mrkdwn", "text": "first"},
                    {"type": "mrkdwn", "text": "second"},
                ],
            }
        ]
        assert ev._extract_blocks_text(blocks) == "summary"
        assert (
            ev._extract_blocks_text(blocks, include_section_fields=True) == "summary\nfirst\nsecond"
        )

    # A string is iterable, so it survives a dropped ``isinstance(fields, list)``
    # guard (its characters are skipped as non-dicts); only a non-iterable scalar
    # makes that mutant raise, so both shapes are pinned.
    @pytest.mark.parametrize(
        "fields",
        [pytest.param("not-a-list", id="iterable-string"), pytest.param(42, id="scalar")],
    )
    def test_trusted_section_fields_ignore_non_list_value(self, fields):
        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "summary"},
                "fields": fields,
            }
        ]
        assert ev._extract_blocks_text(blocks, include_section_fields=True) == "summary"

    def test_list_prefix_counts_toward_the_budget(self):
        blocks = [
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_list",
                        "elements": [
                            {"elements": [{"type": "text", "text": "one"}]},
                            {"elements": [{"type": "text", "text": "two"}]},
                        ],
                    }
                ],
            }
        ]
        assert ev._extract_blocks_text(blocks, 9) == "- one\n- t"


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

    @pytest.mark.parametrize(
        ("fallback", "expected"),
        [
            pytest.param(
                "This message contains interactive elements.",
                "",
                id="test_generic_placeholder_fallback_is_dropped",
            ),
            pytest.param("real fallback", "real fallback", id="test_real_fallback_is_used"),
        ],
    )
    def test_share_fallback(self, fallback, expected):
        event = {"attachments": [{"is_share": True, "text": "", "fallback": fallback}]}
        assert ev._extract_shared_text(event) == expected

    def test_event_level_blocks_used_when_attachments_yield_nothing(self):
        event = {
            "attachments": [],
            "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "event blk"}}],
        }
        assert ev._extract_shared_text(event) == "event blk"

    def test_empty_event_returns_empty(self):
        assert ev._extract_shared_text({}) == ""


class TestSafeLog:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param("a\r\nb\tc", "a  b c", id="test_strips_newlines_and_tabs"),
            pytest.param("", "", id="test_empty_passthrough"),
        ],
    )
    def test_safe_log(self, raw, expected):
        assert ev._safe_log(raw) == expected
