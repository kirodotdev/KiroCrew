"""Additional coverage for ``kiro_crew.slack.gateway``.

Focuses on the orchestrator surfaces the existing ``test_slack_gateway.py`` and
``test_turn_duration_slack.py`` do not reach:

* ``_fire_discord_nudge`` — the whole method had no test anywhere in the suite.
* ``_fire_slack_nudge`` guard/skip/best-effort branches (busy, unroutable,
  missing hooks, post failure, transcript persistence).
* the ``_fire`` router and ``_observer`` closures built by ``_init_autonudge``.
* the orphan-notification and task-notification closures handed to
  ``SubagentManager`` / ``TaskRunner``.
* the MCP-gateway control-plane methods (``_init_mcp_gateway``,
  ``_stop_mcp_broker``, ``_apply_mcp_poolable``, ``_wire_mcp_gateway_dashboard``).
* ``_channel_transport_permitted``'s audit-failure and fail-closed branches.

Everything is driven through mocked collaborators: no network, no subprocess, no
Slack/Discord client, no real broker socket. Style, helpers and patch seams
mirror ``test_slack_gateway.py`` / ``test_turn_duration_slack.py``.
"""

from __future__ import annotations

import asyncio
import math
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import subagent as _sa
from kiro_crew.autonudge import NudgeLoop
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.slack import gateway as gw

# ─── Helpers ─────────────────────────────────────────────────────────────


def _awaited(mock: Any) -> Any:
    """The recorded ``Call`` for a single expected await (fails if there was none)."""
    call = mock.await_args
    assert call is not None
    return call


def _make_orchestrator(**kwargs: Any) -> Any:
    """Build a GatewayOrchestrator with mocked credentials (no Slack tokens).

    Returned as ``Any`` on purpose: every test below swaps real collaborators
    (``sessions`` / ``slack`` / ``ctx_builder`` ...) for mocks, which do not
    satisfy the orchestrator's declared attribute types.
    """
    cfg = KiroCrewConfig()
    creds = {"KIROCREW_OWNER_ID": "U_OWNER"}
    with patch.object(cfg, "load_credentials", return_value=creds):
        return gw.GatewayOrchestrator(
            cfg,
            no_dashboard=kwargs.pop("no_dashboard", True),
            no_crons=kwargs.pop("no_crons", True),
            no_open=True,
        )


def _mock_dashboard_state() -> MagicMock:
    ds = MagicMock()
    ds._slots = {}
    ds.notify = MagicMock()
    ds.push_slots_update = MagicMock()
    ds.push_refresh = MagicMock()
    ds.broadcast_ws = MagicMock()
    ds.get_slot = MagicMock(return_value=None)
    ds.channel_transports = {}
    return ds


def _nudge_sessions(client: object) -> MagicMock:
    s = MagicMock()
    s.is_busy = MagicMock(return_value=False)
    s.get_channel = MagicMock(return_value="C123")
    s.get_thread = MagicMock(return_value="111.222")
    s.get_or_create = AsyncMock(return_value=(client, True, False))
    s.cancel_current = AsyncMock()
    s.release = MagicMock()
    return s


def _slack_nudge_orchestrator() -> Any:
    """Orchestrator wired for the Slack auto-nudge path."""
    orch = _make_orchestrator()
    orch.sessions = _nudge_sessions(SimpleNamespace())
    orch.slack = MagicMock()
    orch.slack.post_message = AsyncMock()
    orch.ctx_builder = SimpleNamespace(
        hooks=object(), build_message=lambda *a, **k: ("MSG", None)
    )
    orch.conv_log = None
    orch.autonudge_svc = None

    async def _approve(_event: object, _parent: str = "") -> bool:
        return True

    orch._interactive_approval = lambda *a, **k: _approve
    return orch


def _loop(key: str = "slack:111.222", **kwargs: Any) -> NudgeLoop:
    return NudgeLoop(
        id=kwargs.pop("id", "loop-1"),
        slot_key=key,
        message=kwargs.pop("message", "keep checking"),
        **kwargs,
    )


def _discord_transport(*, authorized: bool = True, current_key: str | None = None) -> MagicMock:
    """A Discord transport double exposing the dispatcher surface the fire path uses."""
    dispatcher = MagicMock()
    dispatcher.is_authorized = MagicMock(return_value=authorized)
    dispatcher.current_session_key = MagicMock(
        return_value=current_key if current_key is not None else "discord:kirocrew:direct:U9"
    )
    dispatcher.handle_message = AsyncMock()
    sessions = MagicMock()
    sessions.is_busy = MagicMock(return_value=False)
    dispatcher.sessions = sessions
    transport = MagicMock()
    transport.dispatcher = dispatcher
    transport.resolve_conversation = AsyncMock(return_value="DM123")
    return transport


def _discord_orchestrator(transport: MagicMock | None) -> Any:
    orch = _make_orchestrator()
    ds = _mock_dashboard_state()
    ds.channel_transports = {"discord": transport} if transport is not None else {}
    orch.dashboard_state = ds
    orch.autonudge_svc = MagicMock()
    orch.autonudge_svc.remove = AsyncMock()
    return orch


_DKEY = "discord:kirocrew:direct:U9"


# ═════════════════════════════════════════════════════════════════════════
# _fire_discord_nudge
# ═════════════════════════════════════════════════════════════════════════


class TestFireDiscordNudge:
    """Synthetic-injection path for a Discord DM babysit loop."""

    @pytest.mark.asyncio
    async def test_no_transport_skips_without_removing_loop(self):
        """Transport not running is transient — skip, but keep the loop armed."""
        orch = _discord_orchestrator(None)
        assert await orch._fire_discord_nudge(_loop(_DKEY)) is False
        orch.autonudge_svc.remove.assert_not_called()

    @pytest.mark.asyncio
    async def test_transport_present_but_dispatcher_missing_skips(self):
        transport = _discord_transport()
        transport.dispatcher = None
        orch = _discord_orchestrator(transport)
        assert await orch._fire_discord_nudge(_loop(_DKEY)) is False
        orch.autonudge_svc.remove.assert_not_called()

    @pytest.mark.asyncio
    async def test_unsupported_key_shape_retires_loop(self):
        """A key that is not ``discord:{agent}:direct:{user}`` can never route."""
        orch = _discord_orchestrator(_discord_transport())
        assert await orch._fire_discord_nudge(_loop("discord:kirocrew:channel")) is False
        orch.autonudge_svc.remove.assert_awaited_once_with("loop-1")

    @pytest.mark.asyncio
    async def test_unauthorized_user_retires_loop(self):
        """The allowlist can shrink after a loop was created — re-check at fire time."""
        orch = _discord_orchestrator(_discord_transport(authorized=False))
        assert await orch._fire_discord_nudge(_loop(_DKEY)) is False
        orch.autonudge_svc.remove.assert_awaited_once_with("loop-1")

    @pytest.mark.asyncio
    async def test_rotated_session_retires_loop(self):
        """A `!new` generation bump means the monitored conversation is gone."""
        transport = _discord_transport(current_key="discord:kirocrew:direct:U9:gen2")
        orch = _discord_orchestrator(transport)
        assert await orch._fire_discord_nudge(_loop(_DKEY)) is False
        orch.autonudge_svc.remove.assert_awaited_once_with("loop-1")
        transport.dispatcher.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_current_key_lookup_failure_falls_back_to_loop_key(self):
        """A raising ``current_session_key`` must not retire a healthy loop."""
        transport = _discord_transport()
        transport.dispatcher.current_session_key.side_effect = RuntimeError("boom")
        orch = _discord_orchestrator(transport)
        assert await orch._fire_discord_nudge(_loop(_DKEY)) is True
        orch.autonudge_svc.remove.assert_not_called()
        transport.dispatcher.handle_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_busy_session_skips_without_removing_loop(self):
        transport = _discord_transport()
        transport.dispatcher.sessions.is_busy.return_value = True
        orch = _discord_orchestrator(transport)
        assert await orch._fire_discord_nudge(_loop(_DKEY)) is False
        orch.autonudge_svc.remove.assert_not_called()
        transport.dispatcher.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_dispatcher_without_sessions_attribute_still_fires(self):
        """``sessions`` is optional on the dispatcher double — absence is not busy."""
        transport = _discord_transport()
        transport.dispatcher.sessions = None
        orch = _discord_orchestrator(transport)
        assert await orch._fire_discord_nudge(_loop(_DKEY)) is True

    @pytest.mark.asyncio
    async def test_happy_path_injects_tagged_message_as_non_command(self):
        transport = _discord_transport()
        orch = _discord_orchestrator(transport)
        loop = _loop(_DKEY, cycle_count=4)

        assert await orch._fire_discord_nudge(loop) is True

        transport.resolve_conversation.assert_awaited_once_with("U9")
        args, kwargs = _awaited(transport.dispatcher.handle_message)
        synthetic = args[0]
        assert synthetic.channel_type == "discord"
        assert synthetic.user_id == "U9"
        assert synthetic.conversation_id == "DM123"
        # cycle_count is 0-based internally; the tag shows the human cycle number.
        assert synthetic.text.startswith("[auto-nudge cycle 5]\n")
        assert "keep checking" in synthetic.text
        # interpret_commands=False keeps a nudge body from parsing as `!command`.
        assert kwargs["interpret_commands"] is False

    @pytest.mark.asyncio
    async def test_dispatch_failure_returns_false(self):
        transport = _discord_transport()
        transport.dispatcher.handle_message.side_effect = RuntimeError("dispatch blew up")
        orch = _discord_orchestrator(transport)
        assert await orch._fire_discord_nudge(_loop(_DKEY)) is False
        # A failed turn is a skip, not a retirement — the service re-arms.
        orch.autonudge_svc.remove.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolve_conversation_failure_returns_false(self):
        transport = _discord_transport()
        transport.resolve_conversation.side_effect = RuntimeError("no dm")
        orch = _discord_orchestrator(transport)
        assert await orch._fire_discord_nudge(_loop(_DKEY)) is False
        transport.dispatcher.handle_message.assert_not_called()


# ═════════════════════════════════════════════════════════════════════════
# _fire_slack_nudge — guard / skip / best-effort branches
# ═════════════════════════════════════════════════════════════════════════


class TestFireSlackNudgeGuards:
    """Everything that makes the Slack nudge bail before or after the turn."""

    @pytest.mark.asyncio
    async def test_no_sessions_returns_false(self):
        orch = _slack_nudge_orchestrator()
        orch.sessions = None
        assert await orch._fire_slack_nudge(_loop()) is False

    @pytest.mark.asyncio
    async def test_no_slack_client_returns_false(self):
        orch = _slack_nudge_orchestrator()
        orch.slack = None
        assert await orch._fire_slack_nudge(_loop()) is False

    @pytest.mark.asyncio
    async def test_busy_session_skips(self):
        orch = _slack_nudge_orchestrator()
        orch.sessions.is_busy.return_value = True
        assert await orch._fire_slack_nudge(_loop()) is False
        orch.sessions.get_or_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_unroutable_session_retires_loop(self):
        """No channel means nowhere to post — the loop can never succeed."""
        orch = _slack_nudge_orchestrator()
        orch.sessions.get_channel.return_value = None
        orch.autonudge_svc = MagicMock()
        orch.autonudge_svc.remove = AsyncMock()
        assert await orch._fire_slack_nudge(_loop()) is False
        orch.autonudge_svc.remove.assert_awaited_once_with("loop-1")

    @pytest.mark.asyncio
    async def test_unroutable_without_service_still_returns_false(self):
        orch = _slack_nudge_orchestrator()
        orch.sessions.get_channel.return_value = None
        orch.autonudge_svc = None
        assert await orch._fire_slack_nudge(_loop()) is False

    @pytest.mark.asyncio
    async def test_missing_hooks_refuses_unattended_turn(self):
        """Fail closed: no HookManager means no PreToolUse governance gate."""
        orch = _slack_nudge_orchestrator()
        orch.ctx_builder = SimpleNamespace(hooks=None, build_message=lambda *a, **k: ("M", None))
        assert await orch._fire_slack_nudge(_loop()) is False
        orch.sessions.get_or_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_ctx_builder_refuses_unattended_turn(self):
        orch = _slack_nudge_orchestrator()
        orch.ctx_builder = None
        assert await orch._fire_slack_nudge(_loop()) is False
        orch.sessions.get_or_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_thread_ts_derived_from_canonical_key(self, monkeypatch):
        """With no stored thread, a ``slack:<ts>`` key supplies the thread root."""
        orch = _slack_nudge_orchestrator()
        orch.sessions.get_thread.return_value = None

        async def _stream_ok(*_a, **_k):
            return "reply body"

        monkeypatch.setattr(gw, "stream_and_collect", _stream_ok)

        assert await orch._fire_slack_nudge(_loop("slack:999.888")) is True
        _chan, _part, thread_ts = _awaited(orch.slack.post_message).args
        assert thread_ts == "999.888"

    @pytest.mark.asyncio
    async def test_turn_exception_returns_false_and_releases_session(self, monkeypatch):
        orch = _slack_nudge_orchestrator()

        async def _stream_boom(*_a, **_k):
            raise RuntimeError("provider exploded")

        monkeypatch.setattr(gw, "stream_and_collect", _stream_boom)

        assert await orch._fire_slack_nudge(_loop()) is False
        orch.sessions.cancel_current.assert_awaited_once()
        orch.sessions.release.assert_called_once()
        orch.slack.post_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_cleanup_failures_do_not_mask_the_turn_result(self, monkeypatch):
        """cancel_current / release failures are swallowed; the turn still counts."""
        orch = _slack_nudge_orchestrator()
        orch.sessions.cancel_current.side_effect = RuntimeError("cancel failed")
        orch.sessions.release.side_effect = RuntimeError("release failed")

        async def _stream_ok(*_a, **_k):
            return "reply body"

        monkeypatch.setattr(gw, "stream_and_collect", _stream_ok)

        assert await orch._fire_slack_nudge(_loop()) is True

    @pytest.mark.asyncio
    async def test_posting_failure_does_not_fail_the_cycle(self, monkeypatch):
        """The turn already ran, so a Slack post failure must not undo it."""
        orch = _slack_nudge_orchestrator()
        orch.slack.post_message.side_effect = RuntimeError("slack down")

        async def _stream_ok(*_a, **_k):
            return "reply body"

        monkeypatch.setattr(gw, "stream_and_collect", _stream_ok)

        assert await orch._fire_slack_nudge(_loop()) is True

    @pytest.mark.asyncio
    async def test_empty_response_posts_nothing(self, monkeypatch):
        orch = _slack_nudge_orchestrator()

        async def _stream_empty(*_a, **_k):
            return ""

        monkeypatch.setattr(gw, "stream_and_collect", _stream_empty)

        assert await orch._fire_slack_nudge(_loop()) is True
        orch.slack.post_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_persists_redacted_turn_for_dashboard_replay(self, monkeypatch):
        orch = _slack_nudge_orchestrator()
        orch.conv_log = MagicMock()
        saved = AsyncMock()
        monkeypatch.setattr(gw, "save_conversation_turn_off_loop", saved)

        async def _stream_ok(*_a, **_k):
            return "reply body"

        monkeypatch.setattr(gw, "stream_and_collect", _stream_ok)

        assert await orch._fire_slack_nudge(_loop()) is True
        args = _awaited(saved).args
        assert args[0] is orch.conv_log
        assert args[1] == "slack:111.222"
        assert "keep checking" in args[2]
        assert args[3] == "reply body"
        assert _awaited(saved).kwargs["source_user"] == "autonudge"

    @pytest.mark.asyncio
    async def test_persistence_failure_never_fails_the_cycle(self, monkeypatch):
        orch = _slack_nudge_orchestrator()
        orch.conv_log = MagicMock()
        monkeypatch.setattr(
            gw,
            "save_conversation_turn_off_loop",
            AsyncMock(side_effect=RuntimeError("disk full")),
        )

        async def _stream_ok(*_a, **_k):
            return "reply body"

        monkeypatch.setattr(gw, "stream_and_collect", _stream_ok)

        assert await orch._fire_slack_nudge(_loop()) is True

    @pytest.mark.asyncio
    async def test_temporary_thread_is_not_persisted(self, monkeypatch):
        orch = _slack_nudge_orchestrator()
        orch.conv_log = MagicMock()
        saved = AsyncMock()
        monkeypatch.setattr(gw, "save_conversation_turn_off_loop", saved)
        monkeypatch.setattr(gw, "is_thread_temporary", lambda _k: True)

        async def _stream_ok(*_a, **_k):
            return "reply body"

        monkeypatch.setattr(gw, "stream_and_collect", _stream_ok)

        assert await orch._fire_slack_nudge(_loop()) is True
        saved.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_incognito_thread_is_not_persisted(self, monkeypatch):
        orch = _slack_nudge_orchestrator()
        orch.conv_log = MagicMock()
        saved = AsyncMock()
        monkeypatch.setattr(gw, "save_conversation_turn_off_loop", saved)
        monkeypatch.setattr(gw, "is_thread_incognito", lambda _k: True)

        async def _stream_ok(*_a, **_k):
            return "reply body"

        monkeypatch.setattr(gw, "stream_and_collect", _stream_ok)

        assert await orch._fire_slack_nudge(_loop()) is True
        saved.assert_not_awaited()


# ═════════════════════════════════════════════════════════════════════════
# _init_autonudge closures: the _fire router and the _observer
# ═════════════════════════════════════════════════════════════════════════


class TestAutonudgeRouterAndObserver:
    """``_init_autonudge`` builds the key-namespace router and the WS observer."""

    async def _wire(self, orch: Any):
        with patch("kiro_crew.slack.gateway.autonudge_enabled", return_value=True):
            with patch("kiro_crew.slack.gateway.AutoNudgeService") as mock_svc:
                inst = MagicMock()
                inst.start = AsyncMock()
                inst.subscribe = MagicMock()
                inst.remove = AsyncMock()
                mock_svc.return_value = inst
                await orch._init_autonudge()
        on_fire = mock_svc.call_args.kwargs["on_fire"]
        observer = inst.subscribe.call_args.args[0]
        return on_fire, observer, inst

    @pytest.mark.asyncio
    async def test_slack_key_routes_to_slack_fire(self):
        orch = _make_orchestrator()
        orch._fire_slack_nudge = AsyncMock(return_value=True)
        orch._fire_discord_nudge = AsyncMock(return_value=True)
        on_fire, _observer, _inst = await self._wire(orch)

        loop = _loop("slack:111.222")
        assert await on_fire(loop) is True
        orch._fire_slack_nudge.assert_awaited_once_with(loop)
        orch._fire_discord_nudge.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_discord_key_routes_to_discord_fire(self):
        orch = _make_orchestrator()
        orch._fire_slack_nudge = AsyncMock(return_value=True)
        orch._fire_discord_nudge = AsyncMock(return_value=True)
        on_fire, _observer, _inst = await self._wire(orch)

        loop = _loop(_DKEY)
        assert await on_fire(loop) is True
        orch._fire_discord_nudge.assert_awaited_once_with(loop)
        orch._fire_slack_nudge.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_bare_key_routes_to_dashboard_fire(self):
        orch = _make_orchestrator()
        orch._fire_dashboard_nudge = AsyncMock(return_value=True)
        on_fire, _observer, _inst = await self._wire(orch)

        loop = _loop("chat-1-1721")
        assert await on_fire(loop) is True
        orch._fire_dashboard_nudge.assert_awaited_once_with(loop)

    @pytest.mark.asyncio
    async def test_unsupported_channel_namespace_retires_loop(self, monkeypatch):
        """A channel key with no fire implementation can never succeed."""
        orch = _make_orchestrator()
        monkeypatch.setattr(gw, "is_channel_key", lambda key: key.startswith("telegram:"))
        on_fire, _observer, inst = await self._wire(orch)

        assert await on_fire(_loop("telegram:42")) is False
        inst.remove.assert_awaited_once_with("loop-1")

    @pytest.mark.asyncio
    async def test_observer_broadcasts_loop_state(self):
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        _on_fire, observer, _inst = await self._wire(orch)

        loop = _loop("chat-1-1721", cycle_count=2)
        observer("armed", loop)

        topic, payload = orch.dashboard_state.broadcast_ws.call_args.args
        assert topic == "autonudge_state"
        assert payload["event"] == "armed"
        assert payload["slot"] == "chat-1-1721"
        assert payload["loop"]["id"] == "loop-1"
        assert payload["loop"]["cycle_count"] == 2

    @pytest.mark.asyncio
    async def test_observer_expired_event_also_notifies(self):
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        orch._notify_nudge_expired = MagicMock()
        _on_fire, observer, _inst = await self._wire(orch)

        loop = _loop("chat-1-1721")
        observer("expired", loop)
        orch._notify_nudge_expired.assert_called_once_with(loop)

    @pytest.mark.asyncio
    async def test_observer_without_loop_broadcasts_nothing(self):
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        _on_fire, observer, _inst = await self._wire(orch)

        observer("stopped", None)
        orch.dashboard_state.broadcast_ws.assert_not_called()


# ═════════════════════════════════════════════════════════════════════════
# _init_subagents orphan-notification closures
# ═════════════════════════════════════════════════════════════════════════


def _capture_subagent_kwargs(orch: Any) -> dict:
    with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False):
        with patch("kiro_crew.slack.gateway.SubagentManager") as mock_sm:
            inst = MagicMock()
            inst.start_reaper = MagicMock()
            mock_sm.return_value = inst
            orch._init_subagents()
            return mock_sm.call_args.kwargs


class TestOrphanNotifications:
    """``on_orphan_notify`` (slot injection) and ``on_orphan_dm`` (owner fallback)."""

    def _orch(self, ds: MagicMock | None) -> Any:
        orch = _make_orchestrator()
        orch.sessions = MagicMock()
        orch.ctx_builder = MagicMock()
        orch.dashboard_state = ds
        return orch

    @pytest.mark.asyncio
    async def test_notify_injects_into_slot_and_queues_for_llm(self, monkeypatch):
        ds = _mock_dashboard_state()
        slot = MagicMock()
        slot._pending_subagent_failures = []
        ds.get_slot.return_value = slot
        orch = self._orch(ds)
        monkeypatch.setattr(gw, "dashboard_slot_key", lambda _k: "chat-1")

        notify = _capture_subagent_kwargs(orch)["on_orphan_notify"]
        assert await notify("dashboard:chat-1", "agent a1 was orphaned") is True

        slot.append.assert_called_once()
        assert slot.append.call_args.args[0] == "assistant"
        assert slot._pending_subagent_failures == ["agent a1 was orphaned"]
        ds.push_slots_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_notify_returns_false_when_no_tab_shows_the_parent(self, monkeypatch):
        orch = self._orch(_mock_dashboard_state())
        monkeypatch.setattr(gw, "dashboard_slot_key", lambda _k: "")
        notify = _capture_subagent_kwargs(orch)["on_orphan_notify"]
        assert await notify("cron:nightly", "orphaned") is False

    @pytest.mark.asyncio
    async def test_notify_returns_false_without_dashboard_state(self, monkeypatch):
        orch = self._orch(None)
        monkeypatch.setattr(gw, "dashboard_slot_key", lambda _k: "chat-1")
        notify = _capture_subagent_kwargs(orch)["on_orphan_notify"]
        assert await notify("dashboard:chat-1", "orphaned") is False

    @pytest.mark.asyncio
    async def test_notify_returns_false_when_slot_is_gone(self, monkeypatch):
        ds = _mock_dashboard_state()
        ds.get_slot.return_value = None
        orch = self._orch(ds)
        monkeypatch.setattr(gw, "dashboard_slot_key", lambda _k: "chat-1")
        notify = _capture_subagent_kwargs(orch)["on_orphan_notify"]
        assert await notify("dashboard:chat-1", "orphaned") is False

    @pytest.mark.asyncio
    async def test_dm_uses_bell_and_slack_dm(self):
        ds = _mock_dashboard_state()
        orch = self._orch(ds)
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value="D1")
        orch.slack.post_message = AsyncMock()

        dm = _capture_subagent_kwargs(orch)["on_orphan_dm"]
        assert await dm("a1 orphaned by restart") is True

        ds.notify.assert_called_once()
        orch.slack.post_message.assert_awaited_once_with("D1", "a1 orphaned by restart")

    @pytest.mark.asyncio
    async def test_dm_bell_failure_still_reports_slack_delivery(self):
        ds = _mock_dashboard_state()
        ds.notify.side_effect = RuntimeError("bell broken")
        orch = self._orch(ds)
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value="D1")
        orch.slack.post_message = AsyncMock()

        dm = _capture_subagent_kwargs(orch)["on_orphan_dm"]
        assert await dm("a1 orphaned") is True

    @pytest.mark.asyncio
    async def test_dm_slack_failure_still_reports_bell_delivery(self):
        ds = _mock_dashboard_state()
        orch = self._orch(ds)
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(side_effect=RuntimeError("slack down"))

        dm = _capture_subagent_kwargs(orch)["on_orphan_dm"]
        assert await dm("a1 orphaned") is True

    @pytest.mark.asyncio
    async def test_dm_returns_false_when_nothing_can_deliver(self):
        orch = self._orch(None)
        orch.slack = None
        dm = _capture_subagent_kwargs(orch)["on_orphan_dm"]
        assert await dm("a1 orphaned") is False

    @pytest.mark.asyncio
    async def test_dm_skips_slack_when_open_dm_returns_no_channel(self):
        orch = self._orch(None)
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value=None)
        orch.slack.post_message = AsyncMock()

        dm = _capture_subagent_kwargs(orch)["on_orphan_dm"]
        assert await dm("a1 orphaned") is False
        orch.slack.post_message.assert_not_called()


# ═════════════════════════════════════════════════════════════════════════
# _init_task_runner's _task_notify closure
# ═════════════════════════════════════════════════════════════════════════


class TestTaskNotify:
    """``on_notify`` mirrors bell + refresh, and DMs only approval/denial titles."""

    def _capture(self, orch: Any):
        orch.sessions = MagicMock()
        orch.ctx_builder = MagicMock()
        orch.conv_log = MagicMock()
        orch.consolidator = MagicMock()
        with patch("kiro_crew.slack.gateway.TaskRunner") as mock_tr:
            mock_tr.return_value = MagicMock()
            orch._init_task_runner()
            return mock_tr.call_args.kwargs["on_notify"]

    @pytest.mark.asyncio
    async def test_notifies_dashboard_with_task_meta(self):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        notify = self._capture(orch)

        await notify("Step 2 complete", "all good", "task-7")

        surface, title, body = ds.notify.call_args.args
        assert (surface, title, body) == ("taskrunner", "Step 2 complete", "all good")
        assert ds.notify.call_args.kwargs["meta"] == {"task_id": "task-7"}
        ds.push_refresh.assert_called_once_with("taskrunner")

    @pytest.mark.asyncio
    async def test_no_meta_without_task_id(self):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        notify = self._capture(orch)

        await notify("Step 2 complete", "all good")
        assert ds.notify.call_args.kwargs["meta"] is None

    @pytest.mark.asyncio
    async def test_approval_title_also_dms_the_owner(self):
        orch = _make_orchestrator()
        orch.dashboard_state = None
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value="D1")
        orch.slack.post_message = AsyncMock()
        notify = self._capture(orch)

        await notify("Task 3 requires approval", "run the deploy?")

        channel, text = _awaited(orch.slack.post_message).args
        assert channel == "D1"
        assert text == "*Task 3 requires approval*\nrun the deploy?"

    @pytest.mark.asyncio
    async def test_denied_title_also_dms_the_owner(self):
        orch = _make_orchestrator()
        orch.dashboard_state = None
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value="D1")
        orch.slack.post_message = AsyncMock()
        notify = self._capture(orch)

        await notify("Task 3 denied", "nope")
        orch.slack.post_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_ordinary_title_does_not_dm(self):
        orch = _make_orchestrator()
        orch.dashboard_state = None
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value="D1")
        orch.slack.post_message = AsyncMock()
        notify = self._capture(orch)

        await notify("Investigating gateway error", "looking")
        orch.slack.open_dm.assert_not_called()

    @pytest.mark.asyncio
    async def test_dm_failure_is_swallowed(self):
        orch = _make_orchestrator()
        orch.dashboard_state = None
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(side_effect=RuntimeError("slack down"))
        notify = self._capture(orch)

        await notify("Task 3 requires approval", "run it?")  # must not raise


# ═════════════════════════════════════════════════════════════════════════
# MCP-gateway control plane
# ═════════════════════════════════════════════════════════════════════════


class TestInitMcpGateway:
    """Broker startup, its two early returns and the rewriter-failure fallback."""

    @pytest.mark.asyncio
    async def test_disabled_returns_without_touching_platform_probe(self):
        orch = _make_orchestrator()
        orch._cfg.mcp_gateway.enabled = False
        with patch("kiro_crew.slack.gateway.is_gateway_supported") as probe:
            await orch._init_mcp_gateway()
        probe.assert_not_called()
        assert orch._mcp_gateway_manager is None

    @pytest.mark.asyncio
    async def test_unsupported_platform_returns_early(self):
        orch = _make_orchestrator()
        orch._cfg.mcp_gateway.enabled = True
        with patch("kiro_crew.slack.gateway.is_gateway_supported", return_value=False):
            with patch("kiro_crew.slack.gateway.rewrite_agents") as rewriter:
                await orch._init_mcp_gateway()
        rewriter.assert_not_called()
        assert orch._mcp_gateway_manager is None

    @pytest.mark.asyncio
    async def test_rewriter_failure_falls_back_to_per_session_mcp(self, tmp_path):
        orch = _make_orchestrator()
        orch._cfg.mcp_gateway.enabled = True
        with patch("kiro_crew.slack.gateway.is_gateway_supported", return_value=True), patch(
            "kiro_crew.slack.gateway.resolve_overlay_dir", return_value=tmp_path / "overlay"
        ), patch(
            "kiro_crew.slack.gateway.default_socket_path", return_value=tmp_path / "gw.sock"
        ), patch(
            "kiro_crew.slack.gateway.kiro_agents_dir", return_value=tmp_path / "agents"
        ), patch(
            "kiro_crew.slack.gateway.rewrite_agents", side_effect=RuntimeError("bad spec")
        ), patch(
            "kiro_crew.slack.gateway.GatewayManager"
        ) as mgr_cls:
            await orch._init_mcp_gateway()
        mgr_cls.assert_not_called()
        assert orch._mcp_gateway_manager is None

    @pytest.mark.asyncio
    async def test_successful_start_records_the_manager(self, tmp_path):
        orch = _make_orchestrator()
        orch._cfg.mcp_gateway.enabled = True
        manager = MagicMock()
        manager.start = AsyncMock(return_value=True)
        with patch("kiro_crew.slack.gateway.is_gateway_supported", return_value=True), patch(
            "kiro_crew.slack.gateway.resolve_overlay_dir", return_value=tmp_path / "overlay"
        ), patch(
            "kiro_crew.slack.gateway.default_socket_path", return_value=tmp_path / "gw.sock"
        ), patch(
            "kiro_crew.slack.gateway.kiro_agents_dir", return_value=tmp_path / "agents"
        ), patch(
            "kiro_crew.slack.gateway.rewrite_agents", return_value=(None, {"MC_MCP_TARGET_X": "1"})
        ), patch(
            "kiro_crew.slack.gateway.GatewayManager", return_value=manager
        ):
            await orch._init_mcp_gateway()
        assert orch._mcp_gateway_manager is manager

    @pytest.mark.asyncio
    async def test_failed_start_leaves_no_manager(self, tmp_path):
        orch = _make_orchestrator()
        orch._cfg.mcp_gateway.enabled = True
        manager = MagicMock()
        manager.start = AsyncMock(return_value=False)
        with patch("kiro_crew.slack.gateway.is_gateway_supported", return_value=True), patch(
            "kiro_crew.slack.gateway.resolve_overlay_dir", return_value=tmp_path / "overlay"
        ), patch(
            "kiro_crew.slack.gateway.default_socket_path", return_value=tmp_path / "gw.sock"
        ), patch(
            "kiro_crew.slack.gateway.kiro_agents_dir", return_value=tmp_path / "agents"
        ), patch(
            "kiro_crew.slack.gateway.rewrite_agents", return_value=(None, {})
        ), patch(
            "kiro_crew.slack.gateway.GatewayManager", return_value=manager
        ):
            await orch._init_mcp_gateway()
        assert orch._mcp_gateway_manager is None


class TestStopAndApplyMcpBroker:
    """``_stop_mcp_broker`` / ``_apply_mcp_poolable`` / ``_wire_mcp_gateway_dashboard``."""

    @pytest.mark.asyncio
    async def test_stop_is_a_noop_without_a_broker(self):
        orch = _make_orchestrator()
        orch._mcp_gateway_manager = None
        await orch._stop_mcp_broker()
        assert orch._mcp_gateway_manager is None

    @pytest.mark.asyncio
    async def test_stop_shuts_down_and_clears_the_handle(self):
        orch = _make_orchestrator()
        mgr = MagicMock()
        mgr.shutdown = AsyncMock()
        orch._mcp_gateway_manager = mgr
        await orch._stop_mcp_broker()
        mgr.shutdown.assert_awaited_once()
        assert orch._mcp_gateway_manager is None

    @pytest.mark.asyncio
    async def test_stop_swallows_a_shutdown_failure_but_still_clears(self):
        orch = _make_orchestrator()
        mgr = MagicMock()
        mgr.shutdown = AsyncMock(side_effect=RuntimeError("socket stuck"))
        orch._mcp_gateway_manager = mgr
        await orch._stop_mcp_broker()
        assert orch._mcp_gateway_manager is None

    @pytest.mark.asyncio
    async def test_apply_poolable_without_a_broker_reports_not_applied(self):
        orch = _make_orchestrator()
        orch._mcp_gateway_manager = None
        cfg = KiroCrewConfig()
        cfg.mcp_gateway.poolable_servers = ["beta", "alpha"]
        with patch.object(KiroCrewConfig, "load", return_value=cfg):
            out = await orch._apply_mcp_poolable()
        assert out == {"applied": False, "poolable_servers": ["alpha", "beta"]}

    @pytest.mark.asyncio
    async def test_apply_poolable_restarts_the_broker_and_republishes_it(self):
        orch = _make_orchestrator()
        old = MagicMock()
        old.shutdown = AsyncMock()
        orch._mcp_gateway_manager = old
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds

        new = MagicMock()

        async def _fake_init() -> None:
            orch._mcp_gateway_manager = new

        orch._init_mcp_gateway = _fake_init

        cfg = KiroCrewConfig()
        cfg.mcp_gateway.poolable_servers = ["alpha"]
        with patch.object(KiroCrewConfig, "load", return_value=cfg):
            out = await orch._apply_mcp_poolable()

        old.shutdown.assert_awaited_once()
        assert out == {"applied": True, "poolable_servers": ["alpha"]}
        assert ds._mcp_gateway_manager is new

    def test_wire_dashboard_is_a_noop_without_dashboard_state(self):
        orch = _make_orchestrator()
        orch.dashboard_state = None
        orch._wire_mcp_gateway_dashboard()  # must not raise

    def test_wire_dashboard_publishes_broker_and_callbacks(self):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        mgr = MagicMock()
        orch._mcp_gateway_manager = mgr

        orch._wire_mcp_gateway_dashboard()

        assert ds._mcp_gateway_manager is mgr
        assert ds._mcp_gateway_apply == orch._apply_mcp_gateway_enabled
        assert ds._mcp_gateway_apply_poolable == orch._apply_mcp_poolable


# ═════════════════════════════════════════════════════════════════════════
# _channel_transport_permitted — audit-failure and fail-closed branches
# ═════════════════════════════════════════════════════════════════════════


def _decision(*, permitted: bool, layer: str = "default", rule: str = "rule2-intersect"):
    return SimpleNamespace(permitted=permitted, layer=layer, rule=rule, reason="")


class TestChannelTransportPermittedAuditPaths:
    """The connect-time ``channels`` gate's audit disposition and error posture."""

    def test_deny_audit_failure_still_denies(self):
        """A best-effort deny audit that cannot be written must not mask the deny."""
        audit = MagicMock()
        audit.log_governance_decision.side_effect = RuntimeError("sel unwritable")
        with patch.object(
            gw, "governance_permits", return_value=_decision(permitted=False)
        ), patch.object(gw, "sel", return_value=audit):
            assert gw._channel_transport_permitted("telegram") is False

    def test_ungoverned_allow_survives_an_unwritable_audit(self):
        """No policy governs ``channels`` → SEL disk health must not block startup."""
        audit = MagicMock()
        audit.log_governance_decision.side_effect = RuntimeError("sel unwritable")
        with patch.object(
            gw, "governance_permits", return_value=_decision(permitted=True, layer="default")
        ), patch.object(gw, "sel", return_value=audit):
            assert gw._channel_transport_permitted("telegram") is True

    def test_governed_allow_denies_when_its_audit_cannot_be_written(self):
        """audit-or-deny: a policy-governed transport never connects unaudited."""
        audit = MagicMock()
        audit.log_governance_decision.side_effect = RuntimeError("sel unwritable")
        with patch.object(
            gw, "governance_permits", return_value=_decision(permitted=True, layer="policy")
        ), patch.object(gw, "sel", return_value=audit), patch.object(
            gw, "audit_governance_degraded"
        ) as degraded:
            assert gw._channel_transport_permitted("telegram") is False
        assert degraded.call_args.kwargs["failed_closed"] is True

    def test_governed_allow_is_audited_critically(self):
        audit = MagicMock()
        with patch.object(
            gw, "governance_permits", return_value=_decision(permitted=True, layer="profile")
        ), patch.object(gw, "sel", return_value=audit):
            assert gw._channel_transport_permitted("webex") is True
        assert audit.log_governance_decision.call_args.kwargs["critical"] is True

    def test_ungoverned_allow_is_audited_best_effort(self):
        audit = MagicMock()
        with patch.object(
            gw, "governance_permits", return_value=_decision(permitted=True, layer="")
        ), patch.object(gw, "sel", return_value=audit):
            assert gw._channel_transport_permitted("webex") is True
        assert audit.log_governance_decision.call_args.kwargs["critical"] is False

    def test_evaluation_error_fails_closed(self):
        with patch.object(
            gw, "governance_permits", side_effect=RuntimeError("resolver broke")
        ), patch.object(gw, "audit_governance_degraded") as degraded:
            assert gw._channel_transport_permitted("wecom") is False
        degraded.assert_called_once()

    def test_degrade_audit_failure_does_not_mask_the_deny(self):
        with patch.object(
            gw, "governance_permits", side_effect=RuntimeError("resolver broke")
        ), patch.object(
            gw, "audit_governance_degraded", side_effect=RuntimeError("audit import failed")
        ):
            assert gw._channel_transport_permitted("wecom") is False

    def test_composition_error_propagates(self):
        """A broken CPP composition must abort, not silently deny."""
        with patch.object(
            gw, "governance_permits", side_effect=gw.PlatformCompositionError("broken")
        ):
            with pytest.raises(gw.PlatformCompositionError):
                gw._channel_transport_permitted("wecom")


# ═════════════════════════════════════════════════════════════════════════
# _fire_dashboard_nudge happy path
# ═════════════════════════════════════════════════════════════════════════


class TestFireDashboardNudgeDispatch:
    """The dispatch half of the dashboard nudge (existing tests cover the skips)."""

    @pytest.mark.asyncio
    async def test_idle_slot_appends_nudge_and_spawns_a_turn(self, monkeypatch):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        slot = MagicMock()
        slot.running = False
        slot.key = "chat-1"
        ds.get_slot.return_value = slot
        orch.dashboard_state = ds

        # _run_chat's return value is handed straight to the (patched)
        # spawn_guarded_turn, so a plain sentinel avoids creating a coroutine
        # nothing will ever await.
        task = MagicMock()
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat._run_chat", MagicMock(return_value="CORO")
        )
        monkeypatch.setattr(gw, "spawn_guarded_turn", MagicMock(return_value=task))

        loop = _loop("chat-1", cycle_count=1)
        assert await orch._fire_dashboard_nudge(loop) is True

        role, body, css = slot.append.call_args.args
        assert role == "nudge"
        assert body.startswith("[auto-nudge cycle 2]\n")
        assert css == "msg msg-nudge"
        meta = slot.append.call_args.kwargs["meta"]
        assert meta == {"nudge": {"cycle": 2, "loop_id": "loop-1"}}
        assert slot.task is task
        assert orch._session_tasks["chat-1"] is task
        ds.push_slots_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_rehydrated_slot_is_used_when_the_registry_is_cold(self, monkeypatch):
        """A get_slot miss is a cold cache, not a dead session — restore it."""
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        ds.get_slot.return_value = None
        orch.dashboard_state = ds
        orch.autonudge_svc = MagicMock()
        orch.autonudge_svc.remove = AsyncMock()

        restored = MagicMock()
        restored.running = False
        restored.key = "chat-9"

        async def _rehydrate(_state, _key):
            return restored

        monkeypatch.setattr(gw, "rehydrate_slot_from_history_async", _rehydrate)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat._run_chat", MagicMock(return_value="CORO")
        )
        monkeypatch.setattr(gw, "spawn_guarded_turn", MagicMock(return_value=MagicMock()))

        assert await orch._fire_dashboard_nudge(_loop("chat-9")) is True
        orch.autonudge_svc.remove.assert_not_called()
        restored.append.assert_called_once()


# ═════════════════════════════════════════════════════════════════════════
# Module-level helpers
# ═════════════════════════════════════════════════════════════════════════


class TestDigestChunkSize:
    """``KIROCREW_SUBAGENT_DIGEST_CHUNK_SIZE`` parse guard: never crash import."""

    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("KIROCREW_SUBAGENT_DIGEST_CHUNK_SIZE", raising=False)
        assert gw._digest_chunk_size() == 10

    def test_explicit_value_is_honoured(self, monkeypatch):
        monkeypatch.setenv("KIROCREW_SUBAGENT_DIGEST_CHUNK_SIZE", "25")
        assert gw._digest_chunk_size() == 25

    def test_malformed_value_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("KIROCREW_SUBAGENT_DIGEST_CHUNK_SIZE", "not-a-number")
        assert gw._digest_chunk_size() == 10

    def test_value_is_clamped_to_a_sane_range(self, monkeypatch):
        monkeypatch.setenv("KIROCREW_SUBAGENT_DIGEST_CHUNK_SIZE", "0")
        assert gw._digest_chunk_size() == 1
        monkeypatch.setenv("KIROCREW_SUBAGENT_DIGEST_CHUNK_SIZE", "99999")
        assert gw._digest_chunk_size() == 1000


class TestDigestHoldSecs:
    """``KIROCREW_SUBAGENT_DIGEST_HOLD_SECS`` parse guard (issue #2215): the
    latency half of the digest split must never crash import, and 0 is the
    documented opt-out back to count-trigger-only delivery."""

    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("KIROCREW_SUBAGENT_DIGEST_HOLD_SECS", raising=False)
        assert _sa._digest_hold_secs() == 120.0

    def test_explicit_value_is_honoured(self, monkeypatch):
        monkeypatch.setenv("KIROCREW_SUBAGENT_DIGEST_HOLD_SECS", "45.5")
        assert _sa._digest_hold_secs() == 45.5

    def test_malformed_value_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("KIROCREW_SUBAGENT_DIGEST_HOLD_SECS", "not-a-number")
        assert _sa._digest_hold_secs() == 120.0

    def test_zero_and_negative_disable_the_deadline(self, monkeypatch):
        monkeypatch.setenv("KIROCREW_SUBAGENT_DIGEST_HOLD_SECS", "0")
        assert _sa._digest_hold_secs() == 0.0
        monkeypatch.setenv("KIROCREW_SUBAGENT_DIGEST_HOLD_SECS", "-30")
        assert _sa._digest_hold_secs() == 0.0

    def test_clamped_to_the_per_agent_hard_ceiling(self, monkeypatch):
        """A deadline beyond the reap window is meaningless — the member is
        already dead by then and the wave closes on its own."""
        monkeypatch.setenv("KIROCREW_SUBAGENT_DIGEST_HOLD_SECS", "999999")
        assert _sa._digest_hold_secs() == float(_sa._TIMEOUT_SECS)

    def test_nan_is_malformed_input_not_a_deadline(self, monkeypatch):
        """GPT 5.6 BLOCKING: NaN parses but loses every comparison, so it is
        neither disabled (``nan <= 0`` False) nor bounded (``min(nan, x)`` is
        nan). It would make the sweep force a flush on the FIRST hold, and
        ``int(nan)`` then raises during digest composition — after the hold
        clocks were cleared and ``flushed`` advanced — permanently withholding
        the results the deadline exists to release."""
        for spelling in ("nan", "NaN", "-nan"):
            monkeypatch.setenv("KIROCREW_SUBAGENT_DIGEST_HOLD_SECS", spelling)
            got = _sa._digest_hold_secs()
            assert not math.isnan(got), f"{spelling!r} leaked NaN into the deadline"
            assert got == 120.0
            # The two downstream operations a NaN would have broken:
            # the sweep's grace-window comparison, and digest composition's int().
            assert (5.0 < got) is True, "a fresh hold must stay inside the window"
            assert (99999.0 < got) is False, "an aged hold must leave the window"
            assert int(got) == 120

    def test_infinity_is_clamped_not_leaked(self, monkeypatch):
        """+inf clamps to the ceiling; -inf is a valid opt-out. Unlike NaN,
        both order correctly, so neither needs rejecting."""
        monkeypatch.setenv("KIROCREW_SUBAGENT_DIGEST_HOLD_SECS", "inf")
        assert _sa._digest_hold_secs() == float(_sa._TIMEOUT_SECS)
        monkeypatch.setenv("KIROCREW_SUBAGENT_DIGEST_HOLD_SECS", "-inf")
        assert _sa._digest_hold_secs() == 0.0


class TestHeartbeatSlackParts:
    """The shared heartbeat render: captioned, split, and redacted after transform."""

    def test_caption_is_prepended(self):
        parts = gw._heartbeat_slack_parts("Nightly sweep", "all clear")
        assert parts
        assert parts[0].startswith("💓 *Nightly sweep*")
        assert "all clear" in parts[0]

    def test_long_result_is_split_instead_of_dropped(self):
        parts = gw._heartbeat_slack_parts("Big", "x" * 20000)
        assert len(parts) > 1
        assert all(len(p) <= 40000 for p in parts)


class TestNudgeTurnTimeout:
    """Unattended turns must stay bounded — no human is present to cancel them."""

    def test_timeout_is_positive_and_finite(self):
        assert 0 < gw._NUDGE_TURN_TIMEOUT < float("inf")

    def test_background_approval_sources_include_autonudge(self):
        assert "autonudge" in gw._BACKGROUND_APPROVAL_SOURCES
        assert "cron" in gw._BACKGROUND_APPROVAL_SOURCES


def test_event_loop_is_not_required_for_module_helpers():
    """Quick check: the pure helpers above run with no running loop (import-time safe)."""
    with pytest.raises(RuntimeError):
        asyncio.get_running_loop()
    assert gw._digest_chunk_size() >= 1
