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
  ``_stop_mcp_broker``, ``_apply_mcp_stub``, ``_wire_mcp_gateway_dashboard``).
* ``_channel_transport_permitted``'s audit-failure and fail-closed branches.

Everything is driven through mocked collaborators: no network, no subprocess, no
Slack/Discord client, no real broker socket. Style, helpers and patch seams
mirror ``test_slack_gateway.py`` / ``test_turn_duration_slack.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_state

from kiro_crew import session_directive
from kiro_crew import subagent as _sa
from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TOOL_CALL, EVENT_TOOL_RESULT, AcpEvent
from kiro_crew.autonudge import AutoNudgeService, NudgeLoop
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.monitoring import models as monitor_models
from kiro_crew.monitoring.completion import MonitorCompletionHook
from kiro_crew.monitoring.models import (
    MonitorActionDisposition,
    MonitorBudgets,
    MonitorOutcome,
    MonitorState,
)
from kiro_crew.session import SessionBusyError, SessionClosingError
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
    ds.conversation_log = None
    ds.last_notification_persist = None
    ds.notify = MagicMock()
    ds.push_slots_update = MagicMock()
    ds.push_refresh = MagicMock()
    ds.broadcast_ws = MagicMock()
    ds.broadcast_ws_owners = MagicMock()
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
    orch.ctx_builder = SimpleNamespace(hooks=object(), build_message=lambda *a, **k: ("MSG", None))
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
    dispatcher.handle_message = AsyncMock(
        return_value=monitor_models.MonitorDispatchResult.DISPATCHED
    )
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
    async def test_structured_delivery_distinguishes_busy_and_unavailable(self):
        busy_transport = _discord_transport()
        busy_transport.dispatcher.sessions.is_busy.return_value = True
        busy = _discord_orchestrator(busy_transport)
        unavailable = _discord_orchestrator(
            _discord_transport(current_key="discord:kirocrew:direct:U9:gen2")
        )

        assert await busy._fire_discord_nudge(_loop(_DKEY), "[Monitor wake]") is (
            monitor_models.MonitorDispatchResult.BUSY
        )
        assert (
            await unavailable._fire_discord_nudge(_loop(_DKEY), "[Monitor wake]")
            is monitor_models.MonitorDispatchResult.UNAVAILABLE
        )

    @pytest.mark.asyncio
    async def test_discord_stop_during_conversation_lookup_never_dispatches_as_ordinary(self):
        """A revoked structured claim cannot lose its hook and enter the legacy path."""
        transport = _discord_transport()
        orch = _discord_orchestrator(transport)
        loop = _loop(_DKEY)
        loop.monitor = MonitorState(
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            created_ts=1_000.0,
            last_wake_fingerprint="failure-a",
            wake_in_flight=True,
        )

        async def _stop_during_lookup(_user_id):
            assert loop.monitor is not None
            loop.monitor.wake_in_flight = False
            return "DM123"

        transport.resolve_conversation.side_effect = _stop_during_lookup

        result = await orch._fire_discord_nudge(loop, "[Monitor wake]")

        assert result is monitor_models.MonitorDispatchResult.UNAVAILABLE
        transport.dispatcher.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "boundary_result",
        [
            monitor_models.MonitorDispatchResult.BUSY,
            monitor_models.MonitorDispatchResult.UNAVAILABLE,
        ],
    )
    async def test_structured_delivery_propagates_the_dispatch_boundary_result(
        self, boundary_result
    ):
        """The outer idle check can race; the dispatcher's typed result is authoritative."""
        transport = _discord_transport()
        transport.dispatcher.sessions.is_busy.return_value = False
        transport.dispatcher.handle_message.return_value = boundary_result
        orch = _discord_orchestrator(transport)
        loop = _loop(_DKEY)
        loop.monitor = MonitorState(
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            created_ts=1_000.0,
            last_wake_fingerprint="failure-a",
            wake_in_flight=True,
        )

        result = await orch._fire_discord_nudge(loop, "[Monitor wake]")

        assert result is boundary_result

    @pytest.mark.asyncio
    async def test_structured_delivery_stays_dispatched_after_accepted_turn_error(self):
        """An error after driver acceptance belongs to completion-evidence recovery."""
        transport = _discord_transport()

        async def _accept_then_fail(_message, **kwargs):
            kwargs["monitor_completion"].mark_accepted()
            raise RuntimeError("post-accept failure")

        transport.dispatcher.handle_message.side_effect = _accept_then_fail
        orch = _discord_orchestrator(transport)
        loop = _loop(_DKEY)
        loop.monitor = MonitorState(
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            created_ts=1_000.0,
            last_wake_fingerprint="failure-a",
            wake_in_flight=True,
        )

        result = await orch._fire_discord_nudge(loop, "[Monitor wake]")

        assert result is monitor_models.MonitorDispatchResult.DISPATCHED

    @pytest.mark.asyncio
    async def test_structured_delivery_timeout_preserves_accepted_evidence(self, monkeypatch):
        """A wedged Discord turn is bounded without erasing accepted correlation."""
        transport = _discord_transport()
        orch = _discord_orchestrator(transport)
        cancelled = asyncio.Event()

        async def _accept_then_block(_message, **kwargs):
            kwargs["monitor_completion"].mark_accepted()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        transport.dispatcher.handle_message.side_effect = _accept_then_block
        monkeypatch.setattr(gw, "_NUDGE_TURN_TIMEOUT", 0.01)
        loop = _loop(_DKEY)
        loop.monitor = MonitorState(
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            created_ts=1_000.0,
            last_wake_fingerprint="failure-a",
            wake_in_flight=True,
        )

        result = await asyncio.wait_for(
            orch._fire_discord_nudge(loop, "[Monitor wake]"),
            timeout=0.2,
        )

        assert result is monitor_models.MonitorDispatchResult.DISPATCHED
        assert cancelled.is_set()

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
    async def test_structured_monitor_supplies_completion_hook(self):
        """Only a structured synthetic turn carries monitor accounting state."""
        transport = _discord_transport()
        orch = _discord_orchestrator(transport)
        structured = _loop(_DKEY)
        structured.monitor = MonitorState(
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            created_ts=1_000.0,
            last_wake_fingerprint="failure-a",
            wake_in_flight=True,
        )

        assert await orch._fire_discord_nudge(structured) is True
        _, structured_kwargs = _awaited(transport.dispatcher.handle_message)
        assert isinstance(structured_kwargs["monitor_completion"], MonitorCompletionHook)
        assert structured_kwargs["monitor_session_key"] == _DKEY

        transport.dispatcher.handle_message.reset_mock()
        assert await orch._fire_discord_nudge(_loop(_DKEY)) is True
        _, legacy_kwargs = _awaited(transport.dispatcher.handle_message)
        assert "monitor_completion" not in legacy_kwargs
        assert "monitor_session_key" not in legacy_kwargs

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
    async def test_structured_delivery_distinguishes_busy_and_unavailable(self):
        busy = _slack_nudge_orchestrator()
        busy.sessions.is_busy.return_value = True
        unavailable = _slack_nudge_orchestrator()
        unavailable.sessions.get_channel.return_value = None

        assert await busy._fire_slack_nudge(_loop(), "[Monitor wake]") is (
            monitor_models.MonitorDispatchResult.BUSY
        )
        assert await unavailable._fire_slack_nudge(_loop(), "[Monitor wake]") is (
            monitor_models.MonitorDispatchResult.UNAVAILABLE
        )

    @pytest.mark.asyncio
    async def test_structured_delivery_claims_slack_session_without_waiting(self):
        """A user turn winning after the advisory check keeps the wake unclaimed."""
        orch = _slack_nudge_orchestrator()
        orch.sessions.get_or_create.side_effect = SessionBusyError("slack:111.222")

        result = await orch._fire_slack_nudge(_loop(), "[Monitor wake]")

        assert result is monitor_models.MonitorDispatchResult.BUSY
        orch.sessions.get_or_create.assert_awaited_once_with("slack:111.222", wait_if_busy=False)
        orch.sessions.cancel_current.assert_not_awaited()
        orch.sessions.release.assert_not_called()

    @pytest.mark.asyncio
    async def test_structured_delivery_refuses_shutdown_before_provider_stream(self):
        orch = _slack_nudge_orchestrator()
        loop = _loop()
        loop.monitor = MonitorState(
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            created_ts=1_000.0,
            last_wake_fingerprint="failure-a",
            wake_in_flight=True,
        )
        service = MagicMock()
        service.record_monitor_turn_completion = AsyncMock()
        service.monitor_dispatch_is_authorized = AsyncMock(return_value=True)
        orch.autonudge_svc = service
        orch.sessions.begin_turn.side_effect = SessionClosingError("closing")

        result = await orch._fire_slack_nudge(loop, "[Monitor wake]")

        assert result is monitor_models.MonitorDispatchResult.BUSY
        orch.sessions.begin_turn.assert_called_once_with("slack:111.222")
        orch.sessions.cancel_current.assert_awaited_once_with("slack:111.222")
        orch.sessions.release.assert_called_once_with("slack:111.222")

    @pytest.mark.asyncio
    async def test_structured_delivery_rechecks_slack_policy_at_fire_time(self, monkeypatch):
        orch = _slack_nudge_orchestrator()
        permitted = AsyncMock(return_value=False)
        monkeypatch.setattr(gw, "channel_inbound_permitted", permitted)

        result = await orch._fire_slack_nudge(_loop(), "[Monitor wake]")

        assert result is monitor_models.MonitorDispatchResult.UNAVAILABLE
        permitted.assert_awaited_once_with("slack")
        orch.sessions.get_or_create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_slack_stop_during_context_build_never_streams_as_ordinary(self, monkeypatch):
        """A revoked structured claim cannot lose its hook and enter the legacy path."""
        orch = _slack_nudge_orchestrator()
        orch.autonudge_svc = MagicMock()
        loop = _loop()
        loop.monitor = MonitorState(
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            created_ts=1_000.0,
            last_wake_fingerprint="failure-a",
            wake_in_flight=True,
        )

        async def _stop_during_build(_builder, *_args, **_kwargs):
            assert loop.monitor is not None
            loop.monitor.wake_in_flight = False
            return "MSG", None

        stream = AsyncMock(side_effect=AssertionError("revoked wake reached provider"))
        monkeypatch.setattr(gw, "run_in_embed_pool", _stop_during_build)
        monkeypatch.setattr(gw, "stream_and_collect", stream)

        result = await orch._fire_slack_nudge(loop, "[Monitor wake]")

        assert result is monitor_models.MonitorDispatchResult.UNAVAILABLE
        stream.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_structured_stream_exhaustion_without_complete_is_only_dispatched(self):
        """Stream exhaustion is not completion evidence; the supervisor owns recovery."""
        orch = _slack_nudge_orchestrator()
        loop = _loop()
        loop.monitor = MonitorState(
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            created_ts=1_000.0,
            last_wake_fingerprint="failure-a",
            wake_in_flight=True,
        )
        service = MagicMock()
        service.record_monitor_turn_completion = AsyncMock()
        service.monitor_dispatch_is_authorized = AsyncMock(return_value=True)
        orch.autonudge_svc = service

        class _ExhaustedProvider:
            async def stream(self, _message):
                if False:
                    yield

            async def approve_tool(self, _request_id, *, always=False):
                return None

            async def reject_tool(self, _request_id):
                return None

        orch.sessions.get_or_create = AsyncMock(return_value=(_ExhaustedProvider(), False, False))

        result = await orch._fire_slack_nudge(loop, "[Monitor wake]")

        assert result is monitor_models.MonitorDispatchResult.DISPATCHED
        service.record_monitor_turn_completion.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_structured_turn_rechecks_claim_before_slack_provider_stream(self):
        orch = _slack_nudge_orchestrator()
        loop = _loop()
        loop.monitor = MonitorState(
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            created_ts=1_000.0,
            last_wake_fingerprint="failure-a",
            wake_in_flight=True,
        )
        service = MagicMock()
        service.record_monitor_turn_completion = AsyncMock()
        service.monitor_dispatch_is_authorized = AsyncMock(return_value=False)
        orch.autonudge_svc = service

        class _RefusingProvider:
            async def stream(self, _message):
                raise AssertionError("revoked monitor claim reached the provider")
                yield

            async def approve_tool(self, _request_id, *, always=False):
                return None

            async def reject_tool(self, _request_id):
                return None

        orch.sessions.get_or_create = AsyncMock(return_value=(_RefusingProvider(), False, False))

        result = await orch._fire_slack_nudge(loop, "[Monitor wake]")

        assert result is monitor_models.MonitorDispatchResult.UNAVAILABLE
        service.monitor_dispatch_is_authorized.assert_awaited_once_with(loop.id, "failure-a")
        service.record_monitor_turn_completion.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_structured_timeout_before_acceptance_retries_as_busy(self, monkeypatch):
        """A stalled authorization never starts an evidence deadline."""
        orch = _slack_nudge_orchestrator()
        loop = _loop()
        loop.monitor = MonitorState(
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            created_ts=1_000.0,
            last_wake_fingerprint="failure-a",
            wake_in_flight=True,
        )

        async def _stall_authorization(_monitor_id: str, _fingerprint: str) -> bool:
            await asyncio.Event().wait()
            return True

        service = MagicMock()
        service.record_monitor_turn_completion = AsyncMock()
        service.monitor_dispatch_is_authorized = AsyncMock(side_effect=_stall_authorization)
        orch.autonudge_svc = service
        persist = AsyncMock()
        monkeypatch.setattr(gw, "_NUDGE_TURN_TIMEOUT", 0.01)
        monkeypatch.setattr(gw, "_persist_turn_row", persist)

        result = await orch._fire_slack_nudge(loop, "[Monitor wake]")

        assert result is monitor_models.MonitorDispatchResult.BUSY
        service.monitor_dispatch_is_authorized.assert_awaited_once_with(loop.id, "failure-a")
        service.mark_monitor_turn_accepted.assert_not_called()
        service.record_monitor_turn_completion.assert_not_awaited()
        persist.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_structured_setup_failure_before_acceptance_retries_as_busy(self, monkeypatch):
        orch = _slack_nudge_orchestrator()
        loop = _loop()
        loop.monitor = MonitorState(
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            created_ts=1_000.0,
            last_wake_fingerprint="failure-a",
            wake_in_flight=True,
        )
        service = MagicMock()
        service.record_monitor_turn_completion = AsyncMock()
        service.monitor_dispatch_is_authorized = AsyncMock(return_value=True)
        orch.autonudge_svc = service
        monkeypatch.setattr(gw, "build_tool_gate", MagicMock(side_effect=RuntimeError("boom")))

        result = await orch._fire_slack_nudge(loop, "[Monitor wake]")

        assert result is monitor_models.MonitorDispatchResult.BUSY
        service.mark_monitor_turn_accepted.assert_not_called()

    @pytest.mark.asyncio
    async def test_structured_turn_applies_monitor_stop_directive(
        self,
        tmp_path,
        monkeypatch,
    ):
        """A genuine Slack tool result must stop its owning monitor before completion."""
        orch = _slack_nudge_orchestrator()
        service = AutoNudgeService(base_dir=tmp_path)
        now = time.time()
        loop = await service.add_monitor(
            slot_key="slack:111.222",
            kind="github_pull_request",
            target="https://github.com/acme/widgets/pull/7",
            objective="review_ready",
            cadence_secs=60,
            budgets=MonitorBudgets(),
            now=now,
        )
        assert await service.mark_monitor_action_in_flight(loop.id, "failure-a", now=now)

        class _DirectiveProvider:
            async def stream(self, _message):
                yield AcpEvent(
                    kind=EVENT_TOOL_CALL,
                    tool_call_id="stop-1",
                    title="monitor_stop",
                    tool_name="monitor_stop",
                    mcp_server_name=session_directive.CORE_MCP_SERVER,
                )
                yield AcpEvent(
                    kind=EVENT_TOOL_RESULT,
                    tool_call_id="stop-1",
                    tool_output=session_directive.encode(
                        "monitor_stop",
                        {"reason": "objective complete"},
                        "Monitor stop requested.",
                    ),
                    tool_final=True,
                )
                yield AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

            async def approve_tool(self, _request_id, *, always=False):
                return None

            async def reject_tool(self, _request_id):
                return None

        orch.sessions.get_or_create = AsyncMock(return_value=(_DirectiveProvider(), False, False))
        orch.autonudge_svc = service
        monkeypatch.setattr("kiro_crew.autonudge.get_instance", lambda: service)
        monkeypatch.setattr(gw, "_persist_turn_row", AsyncMock())

        result = await orch._fire_slack_nudge(loop, "[Monitor wake]")

        assert result is monitor_models.MonitorDispatchResult.DISPATCHED
        assert loop.monitor is not None
        assert loop.monitor.outcome is MonitorOutcome.USER_STOP
        assert not loop.active

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
    @pytest.mark.parametrize("times_out", [False, True])
    async def test_safe_completion_is_recorded_before_cancellable_usage_persistence(
        self, monkeypatch, times_out
    ):
        """A completed turn cannot remain in flight if analytics persistence is cancelled."""
        orch = _slack_nudge_orchestrator()
        orch.autonudge_svc = SimpleNamespace(record_monitor_turn_completion=AsyncMock())
        loop = _loop()
        loop.monitor = MonitorState(
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            created_ts=1_000.0,
            last_wake_fingerprint="failure-a",
            wake_in_flight=True,
        )
        order: list[str] = []

        async def _stream(*_args, **kwargs):
            kwargs["on_complete"](
                SimpleNamespace(stop_reason="max_tokens", synthetic_completion=False)
            )
            if times_out:
                await asyncio.Event().wait()
            return "reply body"

        async def _report(*_args, **_kwargs):
            order.append("completion")

        async def _persist(*_args, **_kwargs):
            order.append("persist")
            raise asyncio.CancelledError

        monkeypatch.setattr(gw, "stream_and_collect", _stream)
        monkeypatch.setattr(gw, "_persist_turn_row", _persist)
        monkeypatch.setattr(orch, "_report_monitor_completion", _report)
        if times_out:
            monkeypatch.setattr(gw, "_NUDGE_TURN_TIMEOUT", 0.01)

        with pytest.raises(asyncio.CancelledError):
            await orch._fire_slack_nudge(loop)

        assert order == ["completion", "persist"]

    @pytest.mark.asyncio
    async def test_cancellation_after_safe_completion_records_monitor_once(self):
        """Shutdown cancellation cannot discard an already captured completion."""
        orch = _slack_nudge_orchestrator()
        service = SimpleNamespace(
            record_monitor_turn_completion=AsyncMock(),
            monitor_dispatch_is_authorized=AsyncMock(return_value=True),
        )
        orch.autonudge_svc = service
        loop = _loop()
        loop.monitor = MonitorState(
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            created_ts=1_000.0,
            last_wake_fingerprint="failure-a",
            wake_in_flight=True,
        )
        completed = asyncio.Event()

        class _CompletedThenBlockedProvider:
            async def stream(self, _message):
                yield AcpEvent(kind=EVENT_COMPLETE, stop_reason="max_tokens")
                completed.set()
                await asyncio.Event().wait()

            async def approve_tool(self, _request_id, *, always=False):
                return None

            async def reject_tool(self, _request_id):
                return None

        orch.sessions.get_or_create = AsyncMock(
            return_value=(_CompletedThenBlockedProvider(), False, False)
        )
        task = asyncio.create_task(orch._fire_slack_nudge(loop, "[Monitor wake]"))
        await asyncio.wait_for(completed.wait(), timeout=1)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        service.record_monitor_turn_completion.assert_awaited_once()
        completion = _awaited(service.record_monitor_turn_completion).args[0]
        assert completion.disposition is MonitorActionDisposition.FAILURE

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

    @pytest.mark.asyncio
    async def test_disabled_unconfined_boot_purges_scheduled_plaintext(self, monkeypatch, tmp_path):
        provenance_dir = tmp_path / gw.autonudge_selfarm.SCHEDULED_MESSAGE_RECORD_NAME
        provenance_dir.mkdir()
        plaintext = provenance_dir / "legacy.json"
        plaintext.write_text("deferred user text", encoding="utf-8")
        monkeypatch.setattr(gw.autonudge_selfarm, "data_home", lambda: tmp_path)
        monkeypatch.setattr(
            gw.autonudge_selfarm,
            "scheduled_message_hidden_leaf_confined",
            MagicMock(return_value=False),
        )
        monkeypatch.setattr(gw, "autonudge_enabled", lambda: False)
        orch = _make_orchestrator()

        await orch._init_autonudge()

        assert not provenance_dir.exists()
        assert orch.autonudge_svc is None

    async def _wire(
        self,
        orch: Any,
        *,
        existing_loops: list[NudgeLoop] | None = None,
        emit_during_start: NudgeLoop | None = None,
    ):
        with patch("kiro_crew.slack.gateway.autonudge_enabled", return_value=True):
            with (
                patch("kiro_crew.slack.gateway.AutoNudgeService") as mock_svc,
                patch("kiro_crew.monitoring.controller.MonitorController") as mock_controller,
            ):
                inst = MagicMock()
                inst.subscribe = MagicMock()

                async def _start():
                    if emit_during_start is not None and inst.subscribe.call_args is not None:
                        observer = inst.subscribe.call_args.args[0]
                        observer("updated", emit_during_start)

                inst.start = AsyncMock(side_effect=_start)
                inst.remove = AsyncMock()
                inst.mark_terminal_notification_delivered = AsyncMock()
                inst.list_all.return_value = existing_loops or []
                mock_svc.return_value = inst
                await orch._init_autonudge()
                inst.monitor_dispatch = mock_controller.call_args.args[1]
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
    async def test_structured_envelope_is_identical_across_delivery_surfaces(self):
        orch = _make_orchestrator()
        orch._fire_slack_nudge = AsyncMock(
            return_value=monitor_models.MonitorDispatchResult.DISPATCHED
        )
        orch._fire_discord_nudge = AsyncMock(
            return_value=monitor_models.MonitorDispatchResult.DISPATCHED
        )
        orch._fire_dashboard_nudge = AsyncMock(
            return_value=monitor_models.MonitorDispatchResult.DISPATCHED
        )
        _on_fire, _observer, inst = await self._wire(orch)
        envelope = "[Monitor wake]\ncanonical facts"

        slack_loop = _loop("slack:111.222")
        discord_loop = _loop(_DKEY)
        dashboard_loop = _loop("chat-1-1721")
        assert (
            await inst.monitor_dispatch(slack_loop, envelope)
            is monitor_models.MonitorDispatchResult.DISPATCHED
        )
        assert (
            await inst.monitor_dispatch(discord_loop, envelope)
            is monitor_models.MonitorDispatchResult.DISPATCHED
        )
        assert (
            await inst.monitor_dispatch(dashboard_loop, envelope)
            is monitor_models.MonitorDispatchResult.DISPATCHED
        )

        orch._fire_slack_nudge.assert_awaited_once_with(slack_loop, envelope)
        orch._fire_discord_nudge.assert_awaited_once_with(discord_loop, envelope)
        orch._fire_dashboard_nudge.assert_awaited_once_with(dashboard_loop, envelope)

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
    async def test_observer_broadcasts_structured_state_to_owners_only(self):
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        _on_fire, observer, _inst = await self._wire(orch)
        secret = "AKIAIOSFODNN7EXAMPLE"
        loop = _loop("chat-1-1721")
        loop.monitor = MonitorState(
            kind="github_pull_request",
            target="https://github.com/acme/widgets/pull/7",
            objective="review_ready",
            created_ts=1.0,
        )
        loop.monitor.last_observation = {
            "checks": {"failed": [f"deploy?token={secret}"]},
            "target": f"github.com/acme/widgets#7?token={secret}",
        }
        loop.monitor.last_provider_error = f"provider rejected token {secret}"

        observer("armed", loop)

        orch.dashboard_state.broadcast_ws.assert_not_called()
        topic, payload = orch.dashboard_state.broadcast_ws_owners.call_args.args
        assert topic == "autonudge_state"
        assert payload["loop"]["monitor"]["target"].endswith("/pull/7")
        rendered = json.dumps(payload)
        assert secret not in rendered
        assert "provider rejected token" in payload["loop"]["monitor"]["last_provider_error"]

    @pytest.mark.asyncio
    async def test_observer_broadcasts_scheduled_text_from_provenance_to_owners_only(
        self, monkeypatch
    ):
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        _on_fire, observer, _inst = await self._wire(orch)
        loop = _loop("chat-1-1721")
        loop.message = "scrubbed mutable mirror"
        loop.scheduled_message = True
        loop.scheduled_at = 2_000.0
        monkeypatch.setattr(
            gw.autonudge_selfarm,
            "read_scheduled_message",
            lambda _record_id, _slot_key: gw.autonudge_selfarm.ScheduledMessageProvenance(
                slot_key=loop.slot_key,
                message="exact protected composer text",
                scheduled_at=2_100.0,
            ),
        )

        observer("updated", loop)
        broadcast_tasks = tuple(orch._background_tasks)
        assert len(broadcast_tasks) == 1
        await asyncio.gather(*broadcast_tasks)

        orch.dashboard_state.broadcast_ws.assert_not_called()
        topic, payload = orch.dashboard_state.broadcast_ws_owners.call_args.args
        assert topic == "autonudge_state"
        assert payload["loop"]["message"] == "exact protected composer text"
        assert payload["loop"]["scheduled_at"] == 2_100.0
        assert "scrubbed mutable mirror" not in json.dumps(payload)

    @pytest.mark.asyncio
    async def test_observer_busy_provenance_suppresses_frame_without_removing_loop(
        self, monkeypatch
    ):
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        _on_fire, observer, _inst = await self._wire(orch)
        loop = _loop("chat-1-1721")
        loop.scheduled_message = True
        loop.scheduled_at = 2_100.0
        monkeypatch.setattr(
            gw.autonudge_selfarm,
            "read_scheduled_message",
            lambda *_args: (_ for _ in ()).throw(OSError("store busy")),
        )

        observer("updated", loop)
        await asyncio.gather(*tuple(orch._background_tasks))

        orch.dashboard_state.broadcast_ws.assert_not_called()
        orch.dashboard_state.broadcast_ws_owners.assert_not_called()
        orch.autonudge_svc.remove.assert_not_called()

    @pytest.mark.asyncio
    async def test_observer_suppresses_an_older_scheduled_update_that_finishes_last(
        self, monkeypatch
    ):
        import threading

        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        _on_fire, observer, _inst = await self._wire(orch)
        first_started = threading.Event()
        second_started = threading.Event()
        release_first = threading.Event()
        read_lock = threading.Lock()
        read_count = 0

        def read_provenance(_record_id, slot_key):
            nonlocal read_count
            with read_lock:
                index = read_count
                read_count += 1
            if index == 0:
                first_started.set()
                assert release_first.wait(timeout=2)
                message, scheduled_at = "older protected text", 2_100.0
            else:
                second_started.set()
                message, scheduled_at = "newest protected text", 2_200.0
            return gw.autonudge_selfarm.ScheduledMessageProvenance(
                slot_key=slot_key,
                message=message,
                scheduled_at=scheduled_at,
            )

        monkeypatch.setattr(gw.autonudge_selfarm, "read_scheduled_message", read_provenance)
        older = _loop("chat-1-1721")
        older.scheduled_message = True
        older.scheduled_at = 2_100.0
        newer = _loop("chat-1-1721")
        newer.scheduled_message = True
        newer.scheduled_at = 2_200.0

        observer("updated", older)
        assert await asyncio.to_thread(first_started.wait, 2)
        first_task = next(iter(orch._background_tasks))
        observer("updated", newer)
        tasks = tuple(orch._background_tasks)
        assert await asyncio.to_thread(second_started.wait, 2)
        release_first.set()
        await asyncio.gather(first_task, *tasks)

        orch.dashboard_state.broadcast_ws.assert_not_called()
        assert orch.dashboard_state.broadcast_ws_owners.call_count == 1
        _topic, payload = orch.dashboard_state.broadcast_ws_owners.call_args.args
        assert payload["event"] == "updated"
        assert payload["loop"]["message"] == "newest protected text"
        assert payload["loop"]["scheduled_at"] == 2_200.0

    @pytest.mark.asyncio
    async def test_observer_removal_suppresses_a_pending_scheduled_update(self, monkeypatch):
        import threading

        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        _on_fire, observer, _inst = await self._wire(orch)
        read_started = threading.Event()
        release_read = threading.Event()

        def read_provenance(_record_id, slot_key):
            read_started.set()
            assert release_read.wait(timeout=2)
            return gw.autonudge_selfarm.ScheduledMessageProvenance(
                slot_key=slot_key,
                message="stale protected text",
                scheduled_at=2_100.0,
            )

        monkeypatch.setattr(gw.autonudge_selfarm, "read_scheduled_message", read_provenance)
        loop = _loop("chat-1-1721")
        loop.scheduled_message = True
        loop.scheduled_at = 2_100.0

        observer("updated", loop)
        assert await asyncio.to_thread(read_started.wait, 2)
        pending = tuple(orch._background_tasks)
        observer("removed", loop)
        release_read.set()
        await asyncio.gather(*pending)

        orch.dashboard_state.broadcast_ws.assert_not_called()
        assert orch.dashboard_state.broadcast_ws_owners.call_count == 1
        _topic, payload = orch.dashboard_state.broadcast_ws_owners.call_args.args
        assert payload["event"] == "removed"
        assert "message" not in payload["loop"]

    @pytest.mark.asyncio
    async def test_observer_removal_then_same_id_recreation_suppresses_old_read(self, monkeypatch):
        import threading

        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        _on_fire, observer, _inst = await self._wire(orch)
        first_started = threading.Event()
        release_first = threading.Event()
        read_count = 0

        def read_provenance(_record_id, slot_key):
            nonlocal read_count
            read_count += 1
            if read_count == 1:
                first_started.set()
                assert release_first.wait(timeout=2)
                message, scheduled_at = "removed text", 2_100.0
            else:
                message, scheduled_at = "replacement text", 2_200.0
            return gw.autonudge_selfarm.ScheduledMessageProvenance(
                slot_key=slot_key,
                message=message,
                scheduled_at=scheduled_at,
            )

        monkeypatch.setattr(gw.autonudge_selfarm, "read_scheduled_message", read_provenance)
        removed = _loop("chat-1-1721")
        removed.scheduled_message = True
        removed.scheduled_at = 2_100.0
        observer("updated", removed)
        assert await asyncio.to_thread(first_started.wait, 2)
        pending = tuple(orch._background_tasks)

        observer("removed", removed)
        replacement = _loop("chat-1-1721")
        replacement.scheduled_message = True
        replacement.scheduled_at = 2_200.0
        observer("added", replacement)
        await asyncio.sleep(0)
        release_first.set()
        await asyncio.gather(*pending, *tuple(orch._background_tasks))

        frames = [call.args[1] for call in orch.dashboard_state.broadcast_ws_owners.call_args_list]
        assert [frame["event"] for frame in frames] == ["removed", "added"]
        assert "message" not in frames[0]["loop"]
        assert frames[1]["loop"]["message"] == "replacement text"

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
    async def test_observer_notifies_one_structured_terminal_transition(self):
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        orch._notify_nudge_expired = MagicMock()
        _on_fire, observer, _inst = await self._wire(orch)
        loop = _loop("chat-1-1721")
        loop.active = False
        loop.monitor = MonitorState(
            kind="github_pull_request",
            target="https://github.com/acme/widgets/pull/7",
            objective="review_ready",
            created_ts=1.0,
            outcome=MonitorOutcome.SUCCESS,
            stopped_at=2.0,
        )

        observer("updated", loop)
        observer("updated", loop)
        await asyncio.sleep(0)

        orch._notify_nudge_expired.assert_called_once_with(loop)

    @pytest.mark.asyncio
    async def test_observer_marks_delivery_only_after_notification_persistence(self):
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        persisted = asyncio.get_running_loop().create_future()
        orch.dashboard_state.last_notification_persist = persisted
        orch._notify_nudge_expired = MagicMock(return_value=True)
        _on_fire, observer, inst = await self._wire(orch)
        loop = _loop("chat-1-1721")
        loop.active = False
        loop.monitor = MonitorState(
            kind="github_pull_request",
            target="https://github.com/acme/widgets/pull/7",
            objective="review_ready",
            created_ts=1.0,
            outcome=MonitorOutcome.SUCCESS,
            stopped_at=2.0,
        )

        observer("updated", loop)
        await asyncio.sleep(0)

        inst.mark_terminal_notification_delivered.assert_not_awaited()
        persisted.set_result(True)
        await asyncio.sleep(0)

        inst.mark_terminal_notification_delivered.assert_awaited_once_with(
            loop.id,
            MonitorOutcome.SUCCESS,
            2.0,
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("persistence_result", [False, RuntimeError("write failed")])
    async def test_observer_retries_when_notification_persistence_fails(self, persistence_result):
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        persisted = asyncio.get_running_loop().create_future()
        if isinstance(persistence_result, Exception):
            persisted.set_exception(persistence_result)
        else:
            persisted.set_result(persistence_result)
        orch.dashboard_state.last_notification_persist = persisted
        orch._notify_nudge_expired = MagicMock(return_value=True)
        _on_fire, observer, inst = await self._wire(orch)
        loop = _loop("chat-1-1721")
        loop.active = False
        loop.monitor = MonitorState(
            kind="github_pull_request",
            target="https://github.com/acme/widgets/pull/7",
            objective="review_ready",
            created_ts=1.0,
            outcome=MonitorOutcome.SUCCESS,
            stopped_at=2.0,
        )

        observer("updated", loop)
        await asyncio.sleep(0)
        observer("updated", loop)
        await asyncio.sleep(0)

        inst.mark_terminal_notification_delivered.assert_not_awaited()
        assert orch._notify_nudge_expired.call_count == 2

    @pytest.mark.asyncio
    async def test_restart_replays_terminal_notice_without_delivery_record(self):
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        notification_started = asyncio.Event()

        def _notify(_loop: NudgeLoop) -> bool:
            notification_started.set()
            return True

        orch._notify_nudge_expired = MagicMock(side_effect=_notify)
        persisted = asyncio.get_running_loop().create_future()
        orch.dashboard_state.last_notification_persist = persisted
        loop = _loop("chat-1-1721")
        loop.active = False
        loop.monitor = MonitorState(
            kind="github_pull_request",
            target="https://github.com/acme/widgets/pull/7",
            objective="review_ready",
            created_ts=1.0,
            outcome=MonitorOutcome.SUCCESS,
            stopped_at=2.0,
        )

        startup = asyncio.create_task(self._wire(orch, existing_loops=[loop]))
        await notification_started.wait()
        await asyncio.sleep(0)
        startup_blocked = not startup.done()

        persisted.set_result(True)
        _on_fire, observer, inst = await startup
        await asyncio.sleep(0)

        observer("updated", loop)

        assert not startup_blocked
        orch._notify_nudge_expired.assert_called_once_with(loop)
        inst.mark_terminal_notification_delivered.assert_awaited_once_with(
            loop.id,
            MonitorOutcome.SUCCESS,
            2.0,
        )

    @pytest.mark.asyncio
    async def test_terminal_transition_during_start_is_not_lost(self):
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        orch._notify_nudge_expired = MagicMock(return_value=True)
        loop = _loop("chat-1-1721")
        loop.active = False
        loop.monitor = MonitorState(
            kind="github_pull_request",
            target="https://github.com/acme/widgets/pull/7",
            objective="review_ready",
            created_ts=1.0,
            outcome=MonitorOutcome.SUCCESS,
            stopped_at=2.0,
        )

        await self._wire(orch, emit_during_start=loop)
        await asyncio.sleep(0)

        orch._notify_nudge_expired.assert_called_once_with(loop)

    @pytest.mark.asyncio
    async def test_restart_deduplicates_terminal_notice_with_delivery_record(self):
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        orch._notify_nudge_expired = MagicMock()
        loop = _loop("chat-1-1721")
        loop.active = False
        loop.monitor = MonitorState(
            kind="github_pull_request",
            target="https://github.com/acme/widgets/pull/7",
            objective="review_ready",
            created_ts=1.0,
            outcome=MonitorOutcome.SUCCESS,
            stopped_at=2.0,
            terminal_notification_delivered=True,
        )
        _on_fire, observer, inst = await self._wire(orch, existing_loops=[loop])

        observer("updated", loop)

        orch._notify_nudge_expired.assert_not_called()
        inst.mark_terminal_notification_delivered.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_observer_does_not_repeat_a_gated_legacy_terminal_notification(self):
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        orch._notify_nudge_expired = MagicMock()
        _on_fire, observer, _inst = await self._wire(orch)
        loop = _loop("slack:111.222")
        loop.gate = True
        loop.active = False
        loop.monitor = MonitorState(
            kind="github_pull_request",
            target="https://github.com/acme/widgets/pull/7",
            objective="review_ready",
            created_ts=1.0,
            outcome=MonitorOutcome.SUCCESS,
            stopped_at=2.0,
        )

        observer("expired", loop)
        observer("fired", loop)

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
    async def test_nothing_routed_returns_without_touching_platform_probe(self):
        """The shipped default: no stubbed server, so no broker and no probe."""
        orch = _make_orchestrator()
        orch._cfg.mcp_gateway.enabled = False
        orch._cfg.mcp_gateway.stub_servers = []
        with patch("kiro_crew.slack.gateway.is_gateway_supported") as probe:
            await orch._init_mcp_gateway()
        probe.assert_not_called()
        assert orch._mcp_gateway_manager is None

    @pytest.mark.asyncio
    async def test_a_routed_server_starts_the_broker_with_sharing_off(self):
        """Sharing off must not keep the broker down for a stubbed server.

        A stubbed server needs its stub, and the stub needs the broker's socket.
        Sharing decides how that server's backend is acquired, so gating the
        broker on it would make stub-only — the useful state for a stateful
        server — unreachable.
        """
        orch = _make_orchestrator()
        orch._cfg.mcp_gateway.enabled = False
        orch._cfg.mcp_gateway.stub_servers = ["alpha-mcp"]
        with patch("kiro_crew.slack.gateway.is_gateway_supported", return_value=False) as probe:
            await orch._init_mcp_gateway()
        probe.assert_called_once()

    @pytest.mark.asyncio
    async def test_turning_sharing_off_restarts_rather_than_stops_when_routed(self):
        """The live-apply path must not strand MCP Apps.

        Two things have to happen when sharing goes off while apps stays on, and
        only asserting both distinguishes the fix from either failure mode:

        * the broker must come back — a plain stop would take away the render
          and callback paths of servers the operator never unstubbed;
        * it must be a RESTART, not a no-op — the rewriter reads the sharing flag
          when the broker starts, so re-running it is what re-emits every stub
          without ``--poolable`` and actually stops the sharing just turned off.

        The set it re-emits is the one the broker is SERVING, so the fixture has
        to say what that is; the configured list alone is not it, because a stub
        change is recorded for the next gateway start rather than applied.
        """
        orch = _make_orchestrator()
        orch._cfg.mcp_gateway.enabled = False
        orch._cfg.mcp_gateway.stub_servers = ["alpha-mcp"]
        orch._mcp_gateway_manager = object()  # a broker is currently up
        orch._mcp_stub_servers_started = frozenset({"alpha-mcp"})  # serving that stub
        calls: list[str] = []

        async def _stop() -> None:
            calls.append("stop")
            orch._mcp_gateway_manager = None

        async def _init(stub_servers: frozenset[str] | None = None) -> None:
            calls.append("init")
            assert stub_servers == frozenset({"alpha-mcp"})

        with patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=orch._cfg):
            with (
                patch.object(orch, "_stop_mcp_broker", _stop),
                patch.object(orch, "_init_mcp_gateway", _init),
            ):
                await orch._apply_mcp_gateway_enabled(False)

        assert calls == ["stop", "init"]

    @pytest.mark.asyncio
    async def test_turning_sharing_off_with_apps_off_leaves_the_broker_down(self):
        """The guard must not leak the other way: with neither switch on, the
        broker stays stopped rather than being restarted for nothing."""
        orch = _make_orchestrator()
        orch._cfg.mcp_gateway.enabled = False
        orch._cfg.mcp_gateway.stub_servers = []
        orch._mcp_gateway_manager = object()
        calls: list[str] = []

        async def _stop() -> None:
            calls.append("stop")
            orch._mcp_gateway_manager = None

        async def _init() -> None:
            calls.append("init")

        with patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=orch._cfg):
            with (
                patch.object(orch, "_stop_mcp_broker", _stop),
                patch.object(orch, "_init_mcp_gateway", _init),
            ):
                await orch._apply_mcp_gateway_enabled(False)

        assert calls == ["stop"]

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
        with (
            patch("kiro_crew.slack.gateway.is_gateway_supported", return_value=True),
            patch("kiro_crew.slack.gateway.resolve_overlay_dir", return_value=tmp_path / "overlay"),
            patch("kiro_crew.slack.gateway.default_socket_path", return_value=tmp_path / "gw.sock"),
            patch("kiro_crew.slack.gateway.kiro_agents_dir", return_value=tmp_path / "agents"),
            patch("kiro_crew.slack.gateway.rewrite_agents", side_effect=RuntimeError("bad spec")),
            patch("kiro_crew.slack.gateway.GatewayManager") as mgr_cls,
        ):
            await orch._init_mcp_gateway()
        mgr_cls.assert_not_called()
        assert orch._mcp_gateway_manager is None

    @pytest.mark.asyncio
    async def test_successful_start_records_the_manager(self, tmp_path):
        orch = _make_orchestrator()
        orch._cfg.mcp_gateway.enabled = True
        # A stubbed server is what asks for a broker at all; sharing only decides
        # how that server's backend is acquired.
        orch._cfg.mcp_gateway.stub_servers = ["alpha-mcp"]
        manager = MagicMock()
        manager.start = AsyncMock(return_value=True)
        with (
            patch("kiro_crew.slack.gateway.is_gateway_supported", return_value=True),
            patch("kiro_crew.slack.gateway.resolve_overlay_dir", return_value=tmp_path / "overlay"),
            patch("kiro_crew.slack.gateway.default_socket_path", return_value=tmp_path / "gw.sock"),
            patch("kiro_crew.slack.gateway.kiro_agents_dir", return_value=tmp_path / "agents"),
            patch(
                "kiro_crew.slack.gateway.rewrite_agents",
                return_value=(None, {"MC_MCP_TARGET_X": "1"}),
            ),
            patch("kiro_crew.slack.gateway.GatewayManager", return_value=manager),
        ):
            await orch._init_mcp_gateway()
        assert orch._mcp_gateway_manager is manager

    @pytest.mark.asyncio
    async def test_the_started_set_is_recorded_and_the_override_wins(self, tmp_path):
        """Two properties of the real start, both load-bearing for the sharing path.

        The set handed to the rewriter is what the broker ends up serving, and an
        explicit ``stub_servers`` must beat the configured list -- that argument
        is how an unrelated restart avoids applying a stub change recorded for the
        next gateway start. And the served set has to be REMEMBERED, because the
        sharing path re-emits it rather than re-reading config; if it were not
        recorded, that path would find nothing to serve and silently stop the
        broker it was supposed to restart.
        """
        orch = _make_orchestrator()
        orch._cfg.mcp_gateway.enabled = True
        orch._cfg.mcp_gateway.stub_servers = ["alpha-mcp", "beta-mcp"]  # pending
        manager = MagicMock()
        manager.start = AsyncMock(return_value=True)
        with (
            patch("kiro_crew.slack.gateway.is_gateway_supported", return_value=True),
            patch("kiro_crew.slack.gateway.resolve_overlay_dir", return_value=tmp_path / "overlay"),
            patch("kiro_crew.slack.gateway.default_socket_path", return_value=tmp_path / "gw.sock"),
            patch("kiro_crew.slack.gateway.kiro_agents_dir", return_value=tmp_path / "agents"),
            patch("kiro_crew.slack.gateway.rewrite_agents", return_value=(None, {})) as rewriter,
            patch("kiro_crew.slack.gateway.GatewayManager", return_value=manager),
        ):
            await orch._init_mcp_gateway(stub_servers=frozenset({"alpha-mcp"}))

        assert rewriter.call_args.kwargs["stub_servers"] == frozenset({"alpha-mcp"}), (
            "the configured list was used, so an unrelated restart would apply a "
            "stub change reported as pending"
        )
        assert orch._mcp_stub_servers_started == frozenset({"alpha-mcp"})

    @pytest.mark.asyncio
    async def test_the_ready_log_counts_the_served_set_not_the_configured_one(
        self, tmp_path, caplog
    ):
        """This line is read during "why is my stub not live?".

        Config and the served set diverge exactly when a stub change is waiting
        for the next gateway start, so counting the configured list here would
        answer that question wrongly -- claiming two routed servers beside a
        broker serving one.
        """
        orch = _make_orchestrator()
        orch._cfg.mcp_gateway.enabled = True
        orch._cfg.mcp_gateway.stub_servers = ["alpha-mcp", "beta-mcp"]  # beta pending
        manager = MagicMock()
        manager.start = AsyncMock(return_value=True)
        with (
            patch("kiro_crew.slack.gateway.is_gateway_supported", return_value=True),
            patch("kiro_crew.slack.gateway.resolve_overlay_dir", return_value=tmp_path / "overlay"),
            patch("kiro_crew.slack.gateway.default_socket_path", return_value=tmp_path / "gw.sock"),
            patch("kiro_crew.slack.gateway.kiro_agents_dir", return_value=tmp_path / "agents"),
            patch("kiro_crew.slack.gateway.rewrite_agents", return_value=(None, {})),
            patch("kiro_crew.slack.gateway.GatewayManager", return_value=manager),
        ):
            with caplog.at_level(logging.INFO, logger="kiro_crew.slack.gateway"):
                await orch._init_mcp_gateway(stub_servers=frozenset({"alpha-mcp"}))

        ready = [r for r in caplog.records if "broker ready" in r.getMessage()]
        assert ready, "no broker-ready line was emitted"
        assert (
            "1 stubbed server(s)" in ready[0].getMessage()
        ), f"the ready line counted the configured set: {ready[0].getMessage()}"

    @pytest.mark.asyncio
    async def test_a_failed_start_still_records_the_set_so_a_retry_can_bring_it_up(self, tmp_path):
        """A start that fails leaves the broker down, and the set has to survive.

        Recording only on success would make a transient start failure permanent:
        the broker is absent, and the next restart for an unrelated reason -- the
        sharing toggle -- would find nothing to serve and skip the start instead
        of retrying it. The set says what the attempt was made with, not that the
        attempt worked; ``_mcp_gateway_manager`` is what says a broker is up.
        """
        orch = _make_orchestrator()
        orch._cfg.mcp_gateway.enabled = True
        orch._cfg.mcp_gateway.stub_servers = ["alpha-mcp"]
        manager = MagicMock()
        manager.start = AsyncMock(return_value=False)  # transient failure
        with (
            patch("kiro_crew.slack.gateway.is_gateway_supported", return_value=True),
            patch("kiro_crew.slack.gateway.resolve_overlay_dir", return_value=tmp_path / "overlay"),
            patch("kiro_crew.slack.gateway.default_socket_path", return_value=tmp_path / "gw.sock"),
            patch("kiro_crew.slack.gateway.kiro_agents_dir", return_value=tmp_path / "agents"),
            patch("kiro_crew.slack.gateway.rewrite_agents", return_value=(None, {})),
            patch("kiro_crew.slack.gateway.GatewayManager", return_value=manager),
        ):
            await orch._init_mcp_gateway()

        assert orch._mcp_gateway_manager is None, "a failed start must leave no manager"
        assert orch._mcp_stub_servers_started == frozenset({"alpha-mcp"}), (
            "the failed start dropped the set, so a later restart would skip the "
            "broker instead of retrying it"
        )

    @pytest.mark.asyncio
    async def test_failed_start_leaves_no_manager(self, tmp_path):
        orch = _make_orchestrator()
        orch._cfg.mcp_gateway.enabled = True
        manager = MagicMock()
        manager.start = AsyncMock(return_value=False)
        with (
            patch("kiro_crew.slack.gateway.is_gateway_supported", return_value=True),
            patch("kiro_crew.slack.gateway.resolve_overlay_dir", return_value=tmp_path / "overlay"),
            patch("kiro_crew.slack.gateway.default_socket_path", return_value=tmp_path / "gw.sock"),
            patch("kiro_crew.slack.gateway.kiro_agents_dir", return_value=tmp_path / "agents"),
            patch("kiro_crew.slack.gateway.rewrite_agents", return_value=(None, {})),
            patch("kiro_crew.slack.gateway.GatewayManager", return_value=manager),
        ):
            await orch._init_mcp_gateway()
        assert orch._mcp_gateway_manager is None


class TestStopAndApplyMcpBroker:
    """``_stop_mcp_broker`` / ``_apply_mcp_stub`` / ``_wire_mcp_gateway_dashboard``."""

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
    async def test_apply_stub_reports_not_applied_and_asks_for_a_restart(self):
        """``applied: False`` is the designed outcome, not a failure.

        Nothing is applied in place any more, so there is no "reached state" to
        compare against the wanted one. The pair the dashboard needs is
        ``applied: False`` plus ``restart_required: True``: the first stops the
        switch being drawn as live, the second stops that being read as an error.
        """
        orch = _make_orchestrator()
        orch._mcp_gateway_manager = None

        async def _init_that_must_not_run() -> None:  # pragma: no cover
            raise AssertionError("apply must not start a broker")

        orch._init_mcp_gateway = _init_that_must_not_run

        cfg = KiroCrewConfig()
        cfg.mcp_gateway.stub_servers = ["beta", "alpha"]
        with patch.object(KiroCrewConfig, "load", return_value=cfg):
            out = await orch._apply_mcp_stub()
        assert out == {
            "applied": False,
            "restart_required": True,
            "stub_servers": ["alpha", "beta"],
        }

    @pytest.mark.asyncio
    async def test_apply_stub_leaves_a_live_broker_alone(self):
        """The drain is the destructive part: sessions attached to this manager
        lose their in-flight tool calls to it and never re-handshake."""
        orch = _make_orchestrator()
        old = MagicMock()
        old.shutdown = AsyncMock()
        orch._mcp_gateway_manager = old
        ds = _mock_dashboard_state()
        ds._mcp_gateway_manager = old
        orch.dashboard_state = ds

        async def _init_that_must_not_run() -> None:  # pragma: no cover
            raise AssertionError("apply must not respawn the broker")

        orch._init_mcp_gateway = _init_that_must_not_run

        cfg = KiroCrewConfig()
        cfg.mcp_gateway.stub_servers = ["alpha"]
        with patch.object(KiroCrewConfig, "load", return_value=cfg):
            out = await orch._apply_mcp_stub()

        old.shutdown.assert_not_awaited()
        assert orch._mcp_gateway_manager is old
        assert ds._mcp_gateway_manager is old
        assert out == {
            "applied": False,
            "restart_required": True,
            "stub_servers": ["alpha"],
        }

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
        assert ds._mcp_gateway_apply_stub == orch._apply_mcp_stub


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
        with (
            patch.object(gw, "governance_permits", return_value=_decision(permitted=False)),
            patch.object(gw, "sel", return_value=audit),
        ):
            assert gw._channel_transport_permitted("telegram") is False

    def test_ungoverned_allow_survives_an_unwritable_audit(self):
        """No policy governs ``channels`` → SEL disk health must not block startup."""
        audit = MagicMock()
        audit.log_governance_decision.side_effect = RuntimeError("sel unwritable")
        with (
            patch.object(
                gw, "governance_permits", return_value=_decision(permitted=True, layer="default")
            ),
            patch.object(gw, "sel", return_value=audit),
        ):
            assert gw._channel_transport_permitted("telegram") is True

    def test_governed_allow_denies_when_its_audit_cannot_be_written(self):
        """audit-or-deny: a policy-governed transport never connects unaudited."""
        audit = MagicMock()
        audit.log_governance_decision.side_effect = RuntimeError("sel unwritable")
        with (
            patch.object(
                gw, "governance_permits", return_value=_decision(permitted=True, layer="policy")
            ),
            patch.object(gw, "sel", return_value=audit),
            patch.object(gw, "audit_governance_degraded") as degraded,
        ):
            assert gw._channel_transport_permitted("telegram") is False
        assert degraded.call_args.kwargs["failed_closed"] is True

    def test_governed_allow_is_audited_critically(self):
        audit = MagicMock()
        with (
            patch.object(
                gw, "governance_permits", return_value=_decision(permitted=True, layer="profile")
            ),
            patch.object(gw, "sel", return_value=audit),
        ):
            assert gw._channel_transport_permitted("webex") is True
        assert audit.log_governance_decision.call_args.kwargs["critical"] is True

    def test_ungoverned_allow_is_audited_best_effort(self):
        audit = MagicMock()
        with (
            patch.object(
                gw, "governance_permits", return_value=_decision(permitted=True, layer="")
            ),
            patch.object(gw, "sel", return_value=audit),
        ):
            assert gw._channel_transport_permitted("webex") is True
        assert audit.log_governance_decision.call_args.kwargs["critical"] is False

    def test_evaluation_error_fails_closed(self):
        with (
            patch.object(gw, "governance_permits", side_effect=RuntimeError("resolver broke")),
            patch.object(gw, "audit_governance_degraded") as degraded,
        ):
            assert gw._channel_transport_permitted("wecom") is False
        degraded.assert_called_once()

    def test_degrade_audit_failure_does_not_mask_the_deny(self):
        with (
            patch.object(gw, "governance_permits", side_effect=RuntimeError("resolver broke")),
            patch.object(
                gw, "audit_governance_degraded", side_effect=RuntimeError("audit import failed")
            ),
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
        # Real _ChatSlot defaults this False; a bare MagicMock returns a truthy
        # Mock and would make the nudge defer on the busy guard.
        slot._in_stage_execution = False
        slot.key = "chat-1"
        ds.get_slot.return_value = slot
        orch.dashboard_state = ds

        # Keep the wrapper construction real, but close it at the patched task
        # boundary because this unit test only verifies dispatch bookkeeping.
        ds.run_background_turn.side_effect = lambda _slot, coro: coro
        task = MagicMock()

        def discard_turn(_state, _slot, coro):
            coro.close()
            return task

        monkeypatch.setattr("kiro_crew.dashboard.chat._run_chat", MagicMock(return_value="CORO"))
        monkeypatch.setattr(gw, "spawn_guarded_turn", discard_turn)

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
    @pytest.mark.parametrize("linked_session_key", ["", "slack:111.222"])
    async def test_scheduled_message_is_an_exact_user_turn(self, monkeypatch, linked_session_key):
        """Reuse the nudge dispatcher without leaking nudge protocol into the prompt."""
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        slot = MagicMock()
        slot.running = False
        slot._in_stage_execution = False
        slot._has_reader = False
        slot.key = "chat-1"
        slot.linked_session_key = linked_session_key
        slot.messages = []
        slot._pending = []
        slot.event = MagicMock()
        order: list[str] = []

        def append(role, content, css, **kwargs):
            order.append("append")
            row = {"role": role, "content": content, "cls": css, "meta": kwargs["meta"], "ts": "1"}
            slot.messages.append(row)
            slot._pending.append(row)
            return row

        async def persist(*_args, **_kwargs):
            assert slot._pending == [], "scheduled row became live before persistence"
            order.append("persist")
            return True

        slot.append.side_effect = append
        ds.broadcast_ws.side_effect = lambda *_args, **_kwargs: order.append("publish")
        monkeypatch.setattr(gw, "save_slot_off_loop", persist)
        ds.get_slot.return_value = slot
        ds.run_background_turn.side_effect = lambda _slot, coro: coro
        orch.dashboard_state = ds
        orch.autonudge_svc = MagicMock()
        orch.autonudge_svc.settle_unclaimed_scheduled_delivery = AsyncMock(return_value=False)

        spawned: list[asyncio.Task] = []

        def spawn(_state, _slot, coro):
            task = asyncio.create_task(coro)
            spawned.append(task)
            return task

        run_chat = AsyncMock(return_value=None)
        monkeypatch.setattr(
            gw.autonudge_selfarm,
            "read_scheduled_message",
            lambda _i, _s: gw.autonudge_selfarm.ScheduledMessageProvenance(
                slot_key="chat-1",
                message="follow up with the release owner",
                scheduled_at=2_000.0,
            ),
        )
        monkeypatch.setattr("kiro_crew.dashboard.chat._run_chat", run_chat)
        monkeypatch.setattr(gw, "spawn_guarded_turn", spawn)
        loop = _loop(
            "chat-1",
            message="follow up with the release owner",
            max_cycles=1,
            scheduled_message=True,
            scheduled_at=2_000.0,
        )

        assert await orch._fire_dashboard_nudge(loop) is True
        await asyncio.gather(*spawned)
        await asyncio.sleep(0)

        assert slot.append.call_args.args == (
            "user",
            "follow up with the release owner",
            "msg msg-u",
        )
        assert "broadcast_user" not in slot.append.call_args.kwargs
        assert slot.append.call_args.kwargs["broadcast"] is False
        delivery = slot.append.call_args.kwargs["meta"]
        assert delivery["scheduled_message"] == {
            "at": 2_000.0,
            "loop_id": "loop-1",
        }
        assert isinstance(delivery.get("mid"), str) and delivery["mid"]
        assert gw.row_mid(slot.messages[0]) == delivery["mid"]
        assert order[:3] == ["append", "persist", "publish"]
        assert run_chat.call_args.args[2] == "follow up with the release owner"
        assert run_chat.call_args.kwargs["_directive_user_origin"] is False
        assert run_chat.call_args.kwargs["_directive_self_wake"] is True
        assert "_directive_loop_id" not in run_chat.call_args.kwargs
        assert "_directive_loop_gen" not in run_chat.call_args.kwargs
        # Completion belongs to chat_runner's landed-turn verdict, not task exit.
        orch.autonudge_svc.notify_turn_complete.assert_not_called()
        orch.autonudge_svc.remove.assert_not_called()
        # The local slash-command backstop is armed before the turn spawns and
        # consulted on the turn's return; the runner's own completion signal is
        # what makes it a no-op here.
        orch.autonudge_svc.note_scheduled_delivery_dispatched.assert_called_once_with("loop-1")
        orch.autonudge_svc.settle_unclaimed_scheduled_delivery.assert_awaited_once_with("loop-1")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "save_outcome",
        [False, OSError("save failed")],
        ids=("refused", "exception"),
    )
    async def test_scheduled_persistence_failure_publishes_and_spawns_nothing(
        self, monkeypatch, save_outcome
    ):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        slot = MagicMock(
            running=False,
            _in_stage_execution=False,
            _has_reader=True,
            key="chat-1",
        )
        slot.messages = []
        slot._pending = []
        slot.total_messages = 0
        slot.event = MagicMock()

        def append(role, content, css, **kwargs):
            row = {"role": role, "content": content, "cls": css, "meta": kwargs["meta"], "ts": "1"}
            slot.messages.append(row)
            slot._pending.append(row)
            slot.total_messages += 1
            return row

        slot.append.side_effect = append
        ds.get_slot.return_value = slot
        orch.dashboard_state = ds
        orch.autonudge_svc = MagicMock()
        save = (
            AsyncMock(side_effect=save_outcome)
            if isinstance(save_outcome, Exception)
            else AsyncMock(return_value=save_outcome)
        )
        ds.conversation_log = MagicMock()
        rollback = AsyncMock(return_value=False)
        monkeypatch.setattr(gw, "save_slot_off_loop", save)
        monkeypatch.setattr(gw, "_rollback_staged_transcript_row", rollback)
        monkeypatch.setattr(
            gw.autonudge_selfarm,
            "read_scheduled_message",
            lambda _record_id, _slot: gw.autonudge_selfarm.ScheduledMessageProvenance(
                slot_key="chat-1", message="persist me", scheduled_at=2_000.0
            ),
        )
        spawned: list[asyncio.Task] = []

        def spawn(_state, _slot, coro):
            task = asyncio.create_task(coro)
            spawned.append(task)
            return task

        monkeypatch.setattr(gw, "spawn_guarded_turn", spawn)

        fire = orch._fire_dashboard_nudge(
            _loop(
                "chat-1",
                max_cycles=1,
                scheduled_message=True,
                scheduled_at=2_000.0,
            )
        )
        if isinstance(save_outcome, Exception):
            with pytest.raises(OSError, match="save failed"):
                await fire
        else:
            assert await fire is False
        await asyncio.gather(*spawned)
        if isinstance(save_outcome, Exception):
            rollback.assert_awaited_once()
        else:
            rollback.assert_not_awaited()
        assert slot.messages == []
        assert slot._pending == []
        assert slot.total_messages == 0
        ds.broadcast_ws.assert_not_called()
        ds.clear_question_pending.assert_not_called()
        assert len(spawned) == 1
        orch.autonudge_svc.note_scheduled_delivery_dispatched.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("fully_bound", "connected"),
        [(False, True), (True, False)],
        ids=("incomplete-remote-binding", "disconnected-peer"),
    )
    async def test_scheduled_remote_unavailable_stays_pending_without_a_row(
        self, monkeypatch, fully_bound, connected
    ):
        from kiro_crew.dashboard import remote_relay

        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        slot = MagicMock(
            running=False,
            _in_stage_execution=False,
            key="chat-1",
            executor="remote",
            instance_id="peer-1",
            remote_slot="peer-chat-1",
        )
        slot.is_remote = fully_bound
        ds.get_slot.return_value = slot
        orch.dashboard_state = ds
        orch.autonudge_svc = MagicMock()
        monkeypatch.setattr(remote_relay, "peer_is_connected", lambda *_args: connected)
        monkeypatch.setattr(
            gw.autonudge_selfarm,
            "read_scheduled_message",
            lambda _record_id, _slot: gw.autonudge_selfarm.ScheduledMessageProvenance(
                slot_key="chat-1", message="run remotely", scheduled_at=2_000.0
            ),
        )
        persist = AsyncMock(return_value=True)
        monkeypatch.setattr(gw, "save_slot_off_loop", persist)
        spawn = MagicMock()
        monkeypatch.setattr(gw, "spawn_guarded_turn", spawn)

        result = await orch._fire_dashboard_nudge(
            _loop(
                "chat-1",
                max_cycles=1,
                scheduled_message=True,
                scheduled_at=2_000.0,
            )
        )

        assert result is False
        slot.append.assert_not_called()
        persist.assert_not_awaited()
        ds.broadcast_ws.assert_not_called()
        spawn.assert_not_called()
        orch.autonudge_svc.note_scheduled_delivery_dispatched.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "first_outcome",
        ["truncated", "failed", "cancelled"],
    )
    async def test_scheduled_remote_delivery_retries_without_duplicate_user_rows(
        self, monkeypatch, first_outcome
    ):
        from kiro_crew.dashboard import remote_relay

        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        slot = MagicMock(
            running=False,
            _in_stage_execution=False,
            _has_reader=False,
            key="chat-1",
            executor="remote",
            instance_id="peer-1",
            remote_slot="peer-chat-1",
        )
        slot.is_remote = True
        slot.messages = []
        slot._pending = []
        slot.event = MagicMock()

        def append(role, content, css, **kwargs):
            row = {"role": role, "content": content, "cls": css, "meta": kwargs["meta"], "ts": "1"}
            slot.messages.append(row)
            slot._pending.append(row)
            return row

        slot.append.side_effect = append
        ds.get_slot.return_value = slot
        ds.run_background_turn.side_effect = lambda _slot, coro: coro
        orch.dashboard_state = ds
        orch.autonudge_svc = MagicMock()
        orch.autonudge_svc.settle_unclaimed_scheduled_delivery = AsyncMock(return_value=True)
        monkeypatch.setattr(remote_relay, "peer_is_connected", lambda *_args: True)
        monkeypatch.setattr(
            gw.autonudge_selfarm,
            "read_scheduled_message",
            lambda _record_id, _slot: gw.autonudge_selfarm.ScheduledMessageProvenance(
                slot_key="chat-1", message="run remotely", scheduled_at=2_000.0
            ),
        )
        monkeypatch.setattr(gw, "save_slot_off_loop", AsyncMock(return_value=True))
        relay_attempt = 0

        async def relay(_state, target, _message):
            nonlocal relay_attempt
            relay_attempt += 1
            if relay_attempt > 1:
                return True
            if first_outcome == "failed":
                target.messages[:] = [
                    row
                    for row in target.messages
                    if not row.get("meta", {}).get("scheduled_message")
                ]
                target._pending[:] = [
                    row
                    for row in target._pending
                    if not row.get("meta", {}).get("scheduled_message")
                ]
                return False
            if first_outcome == "cancelled":
                raise asyncio.CancelledError
            return False

        monkeypatch.setattr(remote_relay, "relay_remote_turn", relay)
        local_run = AsyncMock(side_effect=AssertionError("remote schedule ran locally"))
        monkeypatch.setattr("kiro_crew.dashboard.chat._run_chat", local_run)
        spawned: list[asyncio.Task] = []

        def spawn(_state, _slot, coro):
            task = asyncio.create_task(coro)
            spawned.append(task)
            return task

        monkeypatch.setattr(gw, "spawn_guarded_turn", spawn)
        loop = _loop(
            "chat-1",
            max_cycles=1,
            scheduled_message=True,
            scheduled_at=2_000.0,
        )

        assert await orch._fire_dashboard_nudge(loop) is True
        first_tasks = tuple(spawned)
        first_results = await asyncio.gather(*first_tasks, return_exceptions=True)
        if first_outcome == "cancelled":
            assert isinstance(first_results[0], asyncio.CancelledError)
        else:
            assert first_results == [None]
        orch.autonudge_svc.settle_unclaimed_scheduled_delivery.assert_not_awaited()
        orch.autonudge_svc.notify_turn_complete.assert_called_once_with(
            "chat-1", turn_completed=False
        )

        assert await orch._fire_dashboard_nudge(loop) is True
        await asyncio.gather(*spawned[len(first_tasks) :])

        local_run.assert_not_awaited()
        assert relay_attempt == 2
        assert len(slot.messages) == 1
        assert slot.messages[0]["content"] == "run remotely"
        assert slot.messages[0]["meta"]["scheduled_message"]["loop_id"] == "loop-1"
        orch.autonudge_svc.settle_unclaimed_scheduled_delivery.assert_awaited_once_with("loop-1")

    @pytest.mark.asyncio
    async def test_scheduled_delivery_reserves_slot_before_and_during_persist(self, monkeypatch):
        """A concurrent user send queues behind the reserved scheduled turn.

        The reservation is the slot task before the row is appended or persisted.
        A user send arriving during the write therefore cannot take or clobber the
        task; the scheduled row is committed, published, and dispatched once.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        slot = MagicMock()
        slot.running = False
        slot._in_stage_execution = False
        slot._has_reader = False
        slot.key = "chat-1"
        slot.messages = []
        slot._queue = []
        published: list[str] = []

        def append(role, content, css, **kwargs):
            row = {"role": role, "content": content, "cls": css, "meta": kwargs["meta"], "ts": "1"}
            slot.messages.append(row)
            return row

        async def persist(*_args, **_kwargs):
            assert slot.task is not None and not slot.task.done()
            slot._queue.append({"id": "queued-user"})
            return True

        slot.append.side_effect = append
        ds.broadcast_ws.side_effect = lambda *_a, **_k: published.append("publish")
        monkeypatch.setattr(gw, "save_slot_off_loop", persist)
        ds.get_slot.return_value = slot
        ds.run_background_turn.side_effect = lambda _slot, coro: coro
        orch.dashboard_state = ds
        orch.autonudge_svc = MagicMock()
        orch.autonudge_svc.settle_unclaimed_scheduled_delivery = AsyncMock(return_value=False)

        spawn_calls: list[asyncio.Task] = []

        def spawn(_state, _slot, coro):
            task = asyncio.create_task(coro)
            spawn_calls.append(task)
            return task

        monkeypatch.setattr(
            gw.autonudge_selfarm,
            "read_scheduled_message",
            lambda _i, _s: gw.autonudge_selfarm.ScheduledMessageProvenance(
                slot_key="chat-1",
                message="follow up with the release owner",
                scheduled_at=2_000.0,
            ),
        )
        monkeypatch.setattr("kiro_crew.dashboard.chat._run_chat", AsyncMock())
        monkeypatch.setattr(gw, "spawn_guarded_turn", spawn)
        loop = _loop(
            "chat-1",
            message="follow up with the release owner",
            max_cycles=1,
            scheduled_message=True,
            scheduled_at=2_000.0,
        )

        result = await orch._fire_dashboard_nudge(loop)

        assert result is True
        await asyncio.gather(*spawn_calls)

        assert len(spawn_calls) == 1
        assert slot.task is spawn_calls[0], "the reservation task was not clobbered"
        assert len(slot.messages) == 1, "the scheduled row was duplicated or lost"
        assert published == ["publish"]
        assert slot._queue == [{"id": "queued-user"}]
        orch.autonudge_svc.note_scheduled_delivery_dispatched.assert_called_once_with("loop-1")

    @pytest.mark.asyncio
    async def test_scheduled_delivery_refuses_an_occupied_slot_before_persist(self, monkeypatch):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        slot = MagicMock(running=True, _in_stage_execution=False, key="chat-1")
        slot.messages = []
        ds.get_slot.return_value = slot
        orch.dashboard_state = ds
        orch.autonudge_svc = MagicMock()
        persist = AsyncMock(return_value=True)
        spawn = MagicMock()
        monkeypatch.setattr(gw, "save_slot_off_loop", persist)
        monkeypatch.setattr(gw, "spawn_guarded_turn", spawn)
        monkeypatch.setattr(
            gw.autonudge_selfarm,
            "read_scheduled_message",
            lambda _record_id, _slot: gw.autonudge_selfarm.ScheduledMessageProvenance(
                slot_key="chat-1", message="wait", scheduled_at=2_000.0
            ),
        )

        result = await orch._fire_dashboard_nudge(
            _loop("chat-1", max_cycles=1, scheduled_message=True, scheduled_at=2_000.0)
        )

        assert result is False
        slot.append.assert_not_called()
        persist.assert_not_awaited()
        spawn.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "delete_outcome",
        [True, "cancelled", False, OSError("delete failed")],
        ids=("committed", "cancelled", "refused", "exception"),
    )
    async def test_scheduled_delivery_rolls_back_row_when_reservation_is_displaced(
        self, monkeypatch, delete_outcome
    ):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        slot = MagicMock(
            running=False,
            _in_stage_execution=False,
            _has_reader=False,
            key="chat-1",
        )
        slot.messages = []
        slot._pending = []
        slot.total_messages = 0
        slot.event = MagicMock()
        slot.linked_session_key = ""
        competing_release = asyncio.Event()
        competing_task: asyncio.Task | None = None
        persist_calls = 0
        staged_mid = ""

        def append(role, content, css, **kwargs):
            row = {"role": role, "content": content, "cls": css, "meta": kwargs["meta"]}
            slot.messages.append(row)
            slot.total_messages += 1
            return row

        async def persist(*_args, **_kwargs):
            nonlocal competing_task, persist_calls, staged_mid
            persist_calls += 1
            competing_task = asyncio.create_task(competing_release.wait())
            slot.task = competing_task
            assert len(slot.messages) == 1
            staged_mid = gw.row_mid(slot.messages[0]) or ""
            assert staged_mid
            return True

        slot.append.side_effect = append
        ds.get_slot.return_value = slot
        orch.dashboard_state = ds
        orch.autonudge_svc = MagicMock()
        conversation_log = MagicMock()
        ds.conversation_log = conversation_log
        delete_row = (
            AsyncMock(side_effect=delete_outcome)
            if isinstance(delete_outcome, Exception)
            else AsyncMock(
                return_value=(
                    delete_outcome is not False,
                    delete_outcome == "cancelled",
                    delete_outcome is not False,
                )
            )
        )
        monkeypatch.setattr(gw, "save_slot_off_loop", persist)
        monkeypatch.setattr(gw, "_delete_transcript_row_by_mid", delete_row)
        monkeypatch.setattr(
            gw.autonudge_selfarm,
            "read_scheduled_message",
            lambda _record_id, _slot: gw.autonudge_selfarm.ScheduledMessageProvenance(
                slot_key="chat-1", message="wait", scheduled_at=2_000.0
            ),
        )
        spawned: list[asyncio.Task] = []

        def spawn(_state, _slot, coro):
            task = asyncio.create_task(coro)
            spawned.append(task)
            return task

        monkeypatch.setattr(gw, "spawn_guarded_turn", spawn)

        fire = orch._fire_dashboard_nudge(
            _loop("chat-1", max_cycles=1, scheduled_message=True, scheduled_at=2_000.0)
        )
        if delete_outcome is True:
            result = await fire
            assert result is False
        elif delete_outcome == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await fire
        else:
            with pytest.raises(OSError, match="delete failed|rollback was refused"):
                await fire
        await asyncio.gather(*spawned)

        assert persist_calls == 1
        delete_row.assert_awaited_once_with(
            slot,
            conversation_log,
            "dashboard:chat-1",
            staged_mid,
        )
        if delete_outcome is True or delete_outcome == "cancelled":
            assert slot.messages == []
            assert slot.total_messages == 0
        else:
            assert len(slot.messages) == 1
            assert gw.row_mid(slot.messages[0]) == staged_mid
            assert slot.total_messages == 1
        ds.broadcast_ws.assert_not_called()
        orch.autonudge_svc.note_scheduled_delivery_dispatched.assert_not_called()
        assert competing_task is not None and slot.task is competing_task
        competing_release.set()
        await competing_task

    @pytest.mark.asyncio
    async def test_stable_mid_deletion_preserves_nonempty_history_and_is_idempotent(self, tmp_path):
        conversation_log = gw.ConversationLog(base_dir=tmp_path)
        slot = SimpleNamespace(_history_persist_lock=threading.RLock())
        history_key = "dashboard:chat-1"
        await asyncio.to_thread(
            conversation_log.append,
            history_key,
            "assistant",
            "keep me",
            mid="m-keep",
        )
        await asyncio.to_thread(
            conversation_log.append,
            history_key,
            "user",
            "remove me",
            mid="m-remove",
        )

        assert await gw._delete_transcript_row_by_mid(
            slot,
            conversation_log,
            history_key,
            "m-remove",
        ) == (True, False, True)
        assert await gw._delete_transcript_row_by_mid(
            slot,
            conversation_log,
            history_key,
            "m-remove",
        ) == (True, False, False)

        retained = await asyncio.to_thread(conversation_log.read_messages, history_key)
        assert [(row["content"], gw.row_mid(row)) for row in retained] == [("keep me", "m-keep")]

    @pytest.mark.asyncio
    async def test_stable_mid_deletion_drains_cancellation_before_returning(self, tmp_path):
        conversation_log = gw.ConversationLog(base_dir=tmp_path)
        history_key = "dashboard:chat-1"
        await asyncio.to_thread(
            conversation_log.append,
            history_key,
            "user",
            "remove me",
            mid="m-remove",
        )
        base_lock = threading.RLock()
        entered = threading.Event()

        class ObservedLock:
            def __enter__(self):
                entered.set()
                return base_lock.__enter__()

            def __exit__(self, *args):
                return base_lock.__exit__(*args)

        slot = SimpleNamespace(_history_persist_lock=ObservedLock())
        base_lock.acquire()
        deletion = asyncio.create_task(
            gw._delete_transcript_row_by_mid(
                slot,
                conversation_log,
                history_key,
                "m-remove",
            )
        )
        try:
            assert await asyncio.to_thread(entered.wait, 2.0)
            deletion.cancel()
        finally:
            base_lock.release()

        assert await deletion == (True, True, True)
        assert await asyncio.to_thread(conversation_log.read_messages, history_key) == []

    @pytest.mark.asyncio
    async def test_displaced_first_scheduled_row_is_deleted_from_real_history(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.dashboard import chat_persistence

        orch = _make_orchestrator()
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        assert slot.messages == []
        orch.dashboard_state = state
        orch.autonudge_svc = MagicMock()
        orch.autonudge_svc.settle_unclaimed_scheduled_delivery = AsyncMock(return_value=True)
        monkeypatch.setattr(
            state,
            "run_background_turn",
            lambda _slot, coro: coro,
        )
        monkeypatch.setattr(
            gw.autonudge_selfarm,
            "read_scheduled_message",
            lambda _record_id, _slot: gw.autonudge_selfarm.ScheduledMessageProvenance(
                slot_key="chat-1", message="deliver once", scheduled_at=2_000.0
            ),
        )
        run_chat = AsyncMock(return_value=None)
        monkeypatch.setattr("kiro_crew.dashboard.chat._run_chat", run_chat)
        spawned: list[asyncio.Task] = []

        def spawn(_state, _slot, coro):
            task = asyncio.create_task(coro)
            spawned.append(task)
            return task

        monkeypatch.setattr(gw, "spawn_guarded_turn", spawn)
        first_row_committed = threading.Event()
        release_first_save = threading.Event()
        original_atomic_write = chat_persistence.atomic_write
        blocked = False
        stale_snapshot: list[dict] = []
        periodic_before_commit = threading.Event()
        release_periodic = threading.Event()
        deletion_waiting = threading.Event()
        periodic_thread_id: int | None = None
        periodic_task: asyncio.Task | None = None
        original_locked = state.conversation_log._locked
        original_delete = gw._delete_transcript_row_by_mid

        @contextmanager
        def gated_conversation_lock(history_key):
            if threading.get_ident() == periodic_thread_id:
                periodic_before_commit.set()
                assert release_periodic.wait(2.0)
            with original_locked(history_key):
                yield

        monkeypatch.setattr(state.conversation_log, "_locked", gated_conversation_lock)

        async def delete_after_stale_periodic_save(target_slot, conversation_log, history_key, mid):
            nonlocal periodic_task, periodic_thread_id

            def run_periodic_save():
                nonlocal periodic_thread_id
                periodic_thread_id = threading.get_ident()
                return chat_persistence._save_slot_to_history(
                    state,
                    slot,
                    list(stale_snapshot),
                )

            periodic_task = asyncio.create_task(asyncio.to_thread(run_periodic_save))
            assert await asyncio.to_thread(periodic_before_commit.wait, 2.0)
            deletion_waiting.set()
            result = await original_delete(
                target_slot,
                conversation_log,
                history_key,
                mid,
            )
            assert await periodic_task
            return result

        monkeypatch.setattr(
            gw,
            "_delete_transcript_row_by_mid",
            delete_after_stale_periodic_save,
        )

        def atomic_write_then_block(path, payload, *args, **kwargs):
            nonlocal blocked
            result = original_atomic_write(path, payload, *args, **kwargs)
            if not blocked and '"scheduled_message"' in payload:
                blocked = True
                stale_snapshot[:] = list(slot.messages)
                first_row_committed.set()
                assert release_first_save.wait(2.0)
            return result

        monkeypatch.setattr(chat_persistence, "atomic_write", atomic_write_then_block)
        loop = _loop(
            "chat-1",
            max_cycles=1,
            scheduled_message=True,
            scheduled_at=2_000.0,
        )
        first_fire = asyncio.create_task(orch._fire_dashboard_nudge(loop))
        assert await asyncio.to_thread(first_row_committed.wait, 2.0)
        assert len(slot.messages) == 1
        first_mid = gw.row_mid(slot.messages[0])
        assert first_mid

        competing_release = asyncio.Event()
        competing_task = asyncio.create_task(competing_release.wait())
        slot.task = competing_task
        release_first_save.set()
        assert await asyncio.to_thread(deletion_waiting.wait, 2.0)
        assert not first_fire.done()
        release_periodic.set()
        assert await first_fire is False
        await asyncio.gather(*spawned)

        rolled_back = await asyncio.to_thread(
            state.conversation_log.read_messages,
            "dashboard:chat-1",
        )
        assert rolled_back == []
        assert slot.messages == []
        state.broadcast_ws = MagicMock()

        competing_release.set()
        await competing_task
        assert await orch._fire_dashboard_nudge(loop) is True
        await asyncio.gather(*spawned)

        retried = await asyncio.to_thread(
            state.conversation_log.read_messages,
            "dashboard:chat-1",
        )
        assert len(retried) == 1
        assert retried[0]["meta"]["scheduled_message"]["loop_id"] == loop.id
        assert gw.row_mid(retried[0]) != first_mid
        assert len(slot.messages) == 1
        run_chat.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cancelled_initial_scheduled_save_deletes_committed_row_before_retry(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.dashboard import chat_persistence

        orch = _make_orchestrator()
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        orch.dashboard_state = state
        orch.autonudge_svc = MagicMock()
        orch.autonudge_svc.settle_unclaimed_scheduled_delivery = AsyncMock(return_value=True)
        monkeypatch.setattr(state, "run_background_turn", lambda _slot, coro: coro)
        monkeypatch.setattr(
            gw.autonudge_selfarm,
            "read_scheduled_message",
            lambda _record_id, _slot: gw.autonudge_selfarm.ScheduledMessageProvenance(
                slot_key="chat-1", message="deliver once", scheduled_at=2_000.0
            ),
        )
        run_chat = AsyncMock(return_value=None)
        monkeypatch.setattr("kiro_crew.dashboard.chat._run_chat", run_chat)
        spawned: list[asyncio.Task] = []

        def spawn(_state, _slot, coro):
            task = asyncio.create_task(coro)
            spawned.append(task)
            return task

        monkeypatch.setattr(gw, "spawn_guarded_turn", spawn)
        payload_written = threading.Event()
        release_write = threading.Event()
        original_atomic_write = chat_persistence.atomic_write
        blocked = False

        def atomic_write_then_block(path, payload, *args, **kwargs):
            nonlocal blocked
            result = original_atomic_write(path, payload, *args, **kwargs)
            if not blocked and '"scheduled_message"' in payload:
                blocked = True
                payload_written.set()
                assert release_write.wait(2.0)
            return result

        monkeypatch.setattr(chat_persistence, "atomic_write", atomic_write_then_block)
        loop = _loop(
            "chat-1",
            max_cycles=1,
            scheduled_message=True,
            scheduled_at=2_000.0,
        )
        fire = asyncio.create_task(orch._fire_dashboard_nudge(loop))
        assert await asyncio.to_thread(payload_written.wait, 2.0)
        first_mid = gw.row_mid(slot.messages[0])
        assert first_mid
        fire.cancel()
        await asyncio.sleep(0)
        assert not fire.done()
        release_write.set()
        with pytest.raises(asyncio.CancelledError):
            await fire
        await asyncio.gather(*spawned)

        assert slot.messages == []
        assert (
            await asyncio.to_thread(
                state.conversation_log.read_messages,
                "dashboard:chat-1",
            )
            == []
        )
        orch.autonudge_svc.note_scheduled_delivery_dispatched.assert_not_called()

        state.broadcast_ws = MagicMock()
        assert await orch._fire_dashboard_nudge(loop) is True
        await asyncio.gather(*spawned)
        retried = await asyncio.to_thread(
            state.conversation_log.read_messages,
            "dashboard:chat-1",
        )
        assert len(retried) == 1
        assert gw.row_mid(retried[0]) != first_mid
        assert len(slot.messages) == 1
        run_chat.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_scheduled_local_command_exception_rearms_instead_of_settling(self, monkeypatch):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        slot = MagicMock(
            running=False,
            _in_stage_execution=False,
            _has_reader=False,
            key="chat-1",
        )
        slot.messages = []
        slot._pending = []
        slot.event = MagicMock()

        def append(role, content, css, **kwargs):
            row = {"role": role, "content": content, "cls": css, "meta": kwargs["meta"]}
            slot.messages.append(row)
            return row

        slot.append.side_effect = append
        ds.get_slot.return_value = slot
        ds.run_background_turn.side_effect = lambda _slot, coro: coro
        orch.dashboard_state = ds
        orch.autonudge_svc = MagicMock()
        orch.autonudge_svc.settle_unclaimed_scheduled_delivery = AsyncMock(return_value=True)
        monkeypatch.setattr(gw, "save_slot_off_loop", AsyncMock(return_value=True))
        monkeypatch.setattr(
            gw.autonudge_selfarm,
            "read_scheduled_message",
            lambda _record_id, _slot: gw.autonudge_selfarm.ScheduledMessageProvenance(
                slot_key="chat-1", message="/goal status", scheduled_at=2_000.0
            ),
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat._run_chat",
            AsyncMock(side_effect=RuntimeError("local command failed")),
        )
        spawned: list[asyncio.Task] = []

        def spawn(_state, _slot, coro):
            task = asyncio.create_task(coro)
            spawned.append(task)
            return task

        monkeypatch.setattr(gw, "spawn_guarded_turn", spawn)

        assert (
            await orch._fire_dashboard_nudge(
                _loop("chat-1", max_cycles=1, scheduled_message=True, scheduled_at=2_000.0)
            )
            is True
        )
        outcomes = await asyncio.gather(*spawned, return_exceptions=True)

        assert len(outcomes) == 1 and isinstance(outcomes[0], RuntimeError)
        orch.autonudge_svc.note_scheduled_delivery_dispatched.assert_called_once_with("loop-1")
        orch.autonudge_svc.notify_turn_complete.assert_called_once_with(
            "chat-1", turn_completed=False
        )
        orch.autonudge_svc.settle_unclaimed_scheduled_delivery.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_scheduled_local_slash_command_is_retired_by_the_backstop(self, monkeypatch):
        """A local slash-command delivery never signals completion, so the fire
        path settles the charged one-shot on the turn's return."""
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        slot = MagicMock()
        slot.running = False
        slot._in_stage_execution = False
        slot._has_reader = False
        slot.key = "chat-1"
        slot.messages = []

        def append(role, content, css, **kwargs):
            row = {"role": role, "content": content, "cls": css, "meta": kwargs["meta"], "ts": "1"}
            slot.messages.append(row)
            return row

        async def persist(*_args, **_kwargs):
            return True

        slot.append.side_effect = append
        monkeypatch.setattr(gw, "save_slot_off_loop", persist)
        ds.get_slot.return_value = slot
        ds.run_background_turn.side_effect = lambda _slot, coro: coro
        orch.dashboard_state = ds
        orch.autonudge_svc = MagicMock()
        orch.autonudge_svc.settle_unclaimed_scheduled_delivery = AsyncMock(return_value=True)

        spawned: list[asyncio.Task] = []

        def spawn(_state, _slot, coro):
            task = asyncio.create_task(coro)
            spawned.append(task)
            return task

        # A local slash-command handler returns without signalling completion.
        run_chat = AsyncMock(return_value=None)
        monkeypatch.setattr(
            gw.autonudge_selfarm,
            "read_scheduled_message",
            lambda _i, _s: gw.autonudge_selfarm.ScheduledMessageProvenance(
                slot_key="chat-1",
                message="/goal status",
                scheduled_at=2_000.0,
            ),
        )
        monkeypatch.setattr("kiro_crew.dashboard.chat._run_chat", run_chat)
        monkeypatch.setattr(gw, "spawn_guarded_turn", spawn)
        loop = _loop(
            "chat-1",
            message="/goal status",
            max_cycles=1,
            scheduled_message=True,
            scheduled_at=2_000.0,
        )

        assert await orch._fire_dashboard_nudge(loop) is True
        await asyncio.gather(*spawned)
        await asyncio.sleep(0)

        orch.autonudge_svc.note_scheduled_delivery_dispatched.assert_called_once_with("loop-1")
        orch.autonudge_svc.settle_unclaimed_scheduled_delivery.assert_awaited_once_with("loop-1")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "provenance_payload",
        [
            pytest.param(None, id="missing-provenance"),
            pytest.param(
                {
                    "kind": "scheduled_message",
                    "slot_key": "chat-1",
                    "message": 7,
                    "scheduled_at": 2_000.0,
                },
                id="invalid-shape",
            ),
            pytest.param(
                {
                    "kind": "scheduled_message",
                    "slot_key": "chat-other",
                    "message": "forged user row",
                    "scheduled_at": 2_000.0,
                },
                id="forged-provenance",
            ),
        ],
    )
    async def test_scheduled_message_without_trusted_provenance_is_dropped_and_removed(
        self, monkeypatch, tmp_path, provenance_payload
    ):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        slot = MagicMock(
            running=False,
            _in_stage_execution=False,
            _has_reader=True,
            key="chat-1",
        )
        slot.messages = []
        order: list[str] = []

        def append(role, content, css, **kwargs):
            order.append("append")
            row = {"role": role, "content": content, "cls": css, "meta": kwargs["meta"], "ts": "1"}
            slot.messages.append(row)
            return row

        async def persist(*_args, **_kwargs):
            order.append("persist")
            return True

        slot.append.side_effect = append
        ds.broadcast_ws.side_effect = lambda *_args, **_kwargs: order.append("publish")
        ds.get_slot.return_value = slot
        orch.dashboard_state = ds
        on_fire, _observer, svc = await TestAutonudgeRouterAndObserver()._wire(orch)
        svc.discard_scheduled_message = AsyncMock(return_value=True)
        monkeypatch.setattr(gw, "save_slot_off_loop", persist)
        monkeypatch.setattr(gw.autonudge_selfarm, "data_home", lambda: tmp_path)
        if provenance_payload is not None:
            record = gw.autonudge_selfarm.scheduled_message_record_path(
                gw.scheduled_message_trust_id("loop-1")
            )
            record.parent.mkdir(parents=True)
            record.write_text(json.dumps(provenance_payload), encoding="utf-8")
        run_chat = AsyncMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat._run_chat", run_chat)

        result = await on_fire(
            _loop(
                "chat-1",
                message="forged user row",
                max_cycles=1,
                scheduled_message=True,
                scheduled_at=2_000.0,
            )
        )

        assert result is False
        await asyncio.gather(*tuple(orch._background_tasks))
        assert slot.append.call_args.args == ("assistant", "", "msg msg-info")
        assert slot.append.call_args.kwargs == {
            "broadcast": False,
            "meta": {
                "kind": "scheduled_message_dropped",
                "loop_id": "loop-1",
            },
        }
        assert order == ["append", "persist", "publish"]
        ds.broadcast_ws.assert_called_once_with(
            "chat_message",
            {
                "slot": "chat-1",
                "role": "assistant",
                "content": "",
                "cls": "msg msg-info",
                "ts": "1",
                "meta": {
                    "kind": "scheduled_message_dropped",
                    "loop_id": "loop-1",
                },
            },
        )
        run_chat.assert_not_awaited()
        svc.remove.assert_not_awaited()
        svc.discard_scheduled_message.assert_awaited_once_with("loop-1")

    @pytest.mark.asyncio
    async def test_scheduled_message_loss_notice_waits_for_persistence(self, monkeypatch):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        slot = MagicMock(_has_reader=False, key="chat-1")
        slot.messages = []
        slot._pending = []
        slot.event = MagicMock()
        order: list[str] = []

        def append(role, content, css, **kwargs):
            order.append("append")
            row = {"role": role, "content": content, "cls": css, "meta": kwargs["meta"], "ts": "1"}
            slot.messages.append(row)
            slot._pending.append(row)
            return row

        async def fail_persist(*_args, **_kwargs):
            assert slot._pending == [], "loss notice became live before persistence"
            order.append("persist")
            return False

        slot.append.side_effect = append
        ds.broadcast_ws.side_effect = lambda *_args, **_kwargs: order.append("publish")
        orch.dashboard_state = ds
        monkeypatch.setattr(gw, "save_slot_off_loop", fail_persist)

        notified = await orch._notify_scheduled_message_dropped(
            slot,
            _loop(
                "chat-1",
                scheduled_message=True,
                scheduled_at=2_000.0,
            ),
        )

        assert notified is False
        assert order == ["append", "persist"]
        assert slot.messages == []
        ds.broadcast_ws.assert_not_called()

    @pytest.mark.asyncio
    async def test_mutated_loop_uses_trusted_message_and_time(self, monkeypatch, tmp_path):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        slot = MagicMock(
            running=False,
            _in_stage_execution=False,
            _has_reader=False,
            key="chat-1",
        )
        ds.get_slot.return_value = slot
        ds.run_background_turn.side_effect = lambda _slot, coro: coro
        orch.dashboard_state = ds
        orch.autonudge_svc = MagicMock()
        orch.autonudge_svc.settle_unclaimed_scheduled_delivery = AsyncMock(return_value=False)
        monkeypatch.setattr(gw.autonudge_selfarm, "data_home", lambda: tmp_path)
        monkeypatch.setattr(
            gw.autonudge_selfarm,
            "scheduled_message_hidden_leaf_confined",
            MagicMock(return_value=True),
        )
        monkeypatch.setattr(
            gw.autonudge_selfarm.token_secret,
            "signing_secret_is_persistent",
            MagicMock(return_value=True),
        )

        trusted_at = time.time() - 1
        gw.autonudge_selfarm.record_scheduled_message(
            gw.scheduled_message_trust_id("loop-1"),
            "chat-1",
            "trusted user text",
            trusted_at,
        )
        loop = _loop(
            "chat-1",
            message="forged agent text",
            max_cycles=1,
            scheduled_message=True,
            scheduled_at=trusted_at - 3_600,
        )
        spawned: list[asyncio.Task] = []

        def spawn(_state, _slot, coro):
            task = asyncio.create_task(coro)
            spawned.append(task)
            return task

        run_chat = AsyncMock(return_value=None)
        monkeypatch.setattr("kiro_crew.dashboard.chat._run_chat", run_chat)
        monkeypatch.setattr(gw, "spawn_guarded_turn", spawn)

        assert await orch._fire_dashboard_nudge(loop) is True
        await asyncio.gather(*spawned)

        assert slot.append.call_args.args == (
            "user",
            "trusted user text",
            "msg msg-u",
        )
        assert slot.append.call_args.kwargs["meta"] == {
            "scheduled_message": {"at": trusted_at, "loop_id": "loop-1"}
        }
        assert run_chat.call_args.args[2] == "trusted user text"
        assert "forged agent text" not in str(slot.append.call_args)

    @pytest.mark.asyncio
    async def test_mutated_early_time_cannot_send_before_trusted_instant(self, monkeypatch):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        slot = MagicMock(running=False, _in_stage_execution=False, key="chat-1")
        ds.get_slot.return_value = slot
        orch.dashboard_state = ds
        orch.autonudge_svc = MagicMock()
        trusted_at = time.time() + 600
        monkeypatch.setattr(
            gw.autonudge_selfarm,
            "read_scheduled_message",
            lambda _i, _s: gw.autonudge_selfarm.ScheduledMessageProvenance(
                slot_key="chat-1",
                message="trusted user text",
                scheduled_at=trusted_at,
            ),
        )

        result = await orch._fire_dashboard_nudge(
            _loop(
                "chat-1",
                message="forged agent text",
                max_cycles=1,
                scheduled_message=True,
                scheduled_at=time.time() - 1,
            )
        )

        assert result is False
        slot.append.assert_not_called()

    @pytest.mark.asyncio
    async def test_scheduled_message_does_not_compose_a_nudge_body(self, monkeypatch):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        slot = MagicMock(running=False, _in_stage_execution=False, key="chat-1")
        ds.get_slot.return_value = slot
        ds.run_background_turn.side_effect = lambda _slot, coro: coro
        orch.dashboard_state = ds
        orch.autonudge_svc = MagicMock()
        orch.autonudge_svc.settle_unclaimed_scheduled_delivery = AsyncMock(return_value=True)
        monkeypatch.setattr(
            gw.autonudge_selfarm,
            "read_scheduled_message",
            lambda _i, _s: gw.autonudge_selfarm.ScheduledMessageProvenance(
                slot_key="chat-1",
                message="exact text",
                scheduled_at=2_000.0,
            ),
        )
        run_chat = AsyncMock(return_value=None)
        monkeypatch.setattr("kiro_crew.dashboard.chat._run_chat", run_chat)
        spawned: list[asyncio.Task] = []

        def spawn(_state, _slot, coro):
            task = asyncio.create_task(coro)
            spawned.append(task)
            return task

        monkeypatch.setattr(gw, "spawn_guarded_turn", spawn)
        compose = AsyncMock(side_effect=AssertionError("scheduled text reached nudge composer"))
        monkeypatch.setattr(gw, "compose_nudge_body", compose)

        result = await orch._fire_dashboard_nudge(
            _loop(
                "chat-1",
                message="exact text",
                max_cycles=1,
                scheduled_message=True,
                scheduled_at=2_000.0,
            )
        )
        assert result is True
        await asyncio.gather(*spawned)
        compose.assert_not_awaited()
        run_chat.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_scheduled_message_retries_a_transient_provenance_read(self, monkeypatch):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        slot = MagicMock(running=False, _in_stage_execution=False, key="chat-1")
        ds.get_slot.return_value = slot
        orch.dashboard_state = ds
        orch.autonudge_svc = MagicMock()
        orch.autonudge_svc.remove = AsyncMock()
        monkeypatch.setattr(
            gw.autonudge_selfarm,
            "read_scheduled_message",
            lambda _record_id, _slot: (_ for _ in ()).throw(OSError("temporary read failure")),
        )

        result = await orch._fire_dashboard_nudge(
            _loop("chat-1", max_cycles=1, scheduled_message=True, scheduled_at=2_000.0)
        )

        assert result is False
        slot.append.assert_not_called()
        orch.autonudge_svc.remove.assert_not_called()

    @pytest.mark.asyncio
    async def test_scheduled_replay_reuses_its_durable_user_row(self, monkeypatch):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        slot = MagicMock(running=False, _in_stage_execution=False, _has_reader=False, key="chat-1")
        slot.messages = [
            {
                "role": "user",
                "content": "old scheduled text",
                "cls": "msg msg-u",
                "ts": "1",
                "meta": {
                    "mid": "stable-mid",
                    "keep": "unrelated",
                    "scheduled_message": {"loop_id": "loop-1", "at": 1_000.0},
                },
            }
        ]
        slot._pending = []
        slot.event = MagicMock()
        ds.get_slot.return_value = slot
        ds.run_background_turn.side_effect = lambda _slot, coro: coro
        orch.dashboard_state = ds
        orch.autonudge_svc = MagicMock()
        orch.autonudge_svc.settle_unclaimed_scheduled_delivery = AsyncMock(return_value=True)
        persisted: list[list[dict[str, Any]]] = []

        async def persist(*_args, **_kwargs):
            persisted.append(json.loads(json.dumps(slot.messages)))
            return True

        monkeypatch.setattr(gw, "save_slot_off_loop", persist)
        monkeypatch.setattr(
            gw.autonudge_selfarm,
            "read_scheduled_message",
            lambda _record_id, _slot: gw.autonudge_selfarm.ScheduledMessageProvenance(
                slot_key="chat-1", message="edited scheduled text", scheduled_at=2_000.0
            ),
        )
        run_chat = AsyncMock(return_value=None)
        monkeypatch.setattr("kiro_crew.dashboard.chat._run_chat", run_chat)
        spawned: list[asyncio.Task] = []

        def spawn(_state, _slot, coro):
            task = asyncio.create_task(coro)
            spawned.append(task)
            return task

        monkeypatch.setattr(gw, "spawn_guarded_turn", spawn)

        assert (
            await orch._fire_dashboard_nudge(
                _loop("chat-1", max_cycles=1, scheduled_message=True, scheduled_at=1_000.0)
            )
            is True
        )
        await asyncio.gather(*spawned)

        slot.append.assert_not_called()
        assert len(slot.messages) == 1
        row = slot.messages[0]
        assert row["content"] == "edited scheduled text"
        assert row["meta"] == {
            "mid": "stable-mid",
            "keep": "unrelated",
            "scheduled_message": {"loop_id": "loop-1", "at": 2_000.0},
        }
        assert persisted == [slot.messages]
        assert run_chat.await_args.args[2] == "edited scheduled text"
        assert ds.broadcast_ws.call_args.args[1]["content"] == "edited scheduled text"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("rollback_persists", [True, False], ids=["restored", "refused"])
    async def test_cancelled_existing_scheduled_row_reconciliation_is_authoritative(
        self, tmp_path, monkeypatch, rollback_persists
    ):
        orch = _make_orchestrator()
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        slot._has_reader = False
        prior_row = slot.append(
            "user",
            "old scheduled text",
            "msg msg-u",
            broadcast=False,
            meta={
                "mid": "stable-mid",
                "keep": "unrelated",
                "scheduled_message": {"loop_id": "loop-1", "at": 1_000.0},
            },
        )
        assert await gw.save_slot_off_loop(
            state,
            slot,
            best_effort=False,
            expected_history_key="dashboard:chat-1",
        )
        prior_meta = json.loads(json.dumps(prior_row["meta"]))
        prior_mid = gw.row_mid(prior_row)
        assert prior_mid == "stable-mid"

        state.broadcast_ws = MagicMock()
        monkeypatch.setattr(state, "run_background_turn", lambda _slot, coro: coro)
        orch.dashboard_state = state
        orch.autonudge_svc = MagicMock()
        orch.autonudge_svc.settle_unclaimed_scheduled_delivery = AsyncMock(return_value=True)
        monkeypatch.setattr(
            gw.autonudge_selfarm,
            "read_scheduled_message",
            lambda _record_id, _slot: gw.autonudge_selfarm.ScheduledMessageProvenance(
                slot_key="chat-1", message="edited scheduled text", scheduled_at=2_000.0
            ),
        )
        run_chat = AsyncMock(return_value=None)
        monkeypatch.setattr("kiro_crew.dashboard.chat._run_chat", run_chat)
        spawned: list[asyncio.Task] = []

        def spawn(_state, _slot, coro):
            task = asyncio.create_task(coro)
            spawned.append(task)
            return task

        monkeypatch.setattr(gw, "spawn_guarded_turn", spawn)
        original_save = gw.save_slot_off_loop
        reconciled_saved = asyncio.Event()
        release_reconciled_save = asyncio.Event()
        save_calls = 0

        async def controlled_save(*args, **kwargs):
            nonlocal save_calls
            save_calls += 1
            if save_calls == 1:
                result = await original_save(*args, **kwargs)
                reconciled_saved.set()
                await release_reconciled_save.wait()
                return result
            if not rollback_persists:
                return False
            return await original_save(*args, **kwargs)

        monkeypatch.setattr(gw, "save_slot_off_loop", controlled_save)
        fire = asyncio.create_task(
            orch._fire_dashboard_nudge(
                _loop("chat-1", max_cycles=1, scheduled_message=True, scheduled_at=1_000.0)
            )
        )
        await reconciled_saved.wait()
        persisted_reconciled = await asyncio.to_thread(
            state.conversation_log.read_messages,
            "dashboard:chat-1",
        )
        assert persisted_reconciled[0]["content"] == "edited scheduled text"

        fire.cancel()
        await asyncio.sleep(0)
        release_reconciled_save.set()
        if rollback_persists:
            with pytest.raises(asyncio.CancelledError):
                await fire
            expected_content = "old scheduled text"
            expected_meta = prior_meta
        else:
            with pytest.raises(
                OSError, match="scheduled transcript reconciliation rollback was refused"
            ):
                await fire
            expected_content = "edited scheduled text"
            expected_meta = {
                **prior_meta,
                "scheduled_message": {"loop_id": "loop-1", "at": 2_000.0},
            }
        await asyncio.gather(*spawned)

        assert len(slot.messages) == 1
        assert slot.messages[0]["content"] == expected_content
        assert slot.messages[0]["meta"] == expected_meta
        assert gw.row_mid(slot.messages[0]) == prior_mid
        durable = await asyncio.to_thread(
            state.conversation_log.read_messages,
            "dashboard:chat-1",
        )
        assert len(durable) == 1
        assert durable[0]["content"] == expected_content
        assert durable[0]["meta"] == expected_meta
        assert gw.row_mid(durable[0]) == prior_mid
        state.broadcast_ws.assert_not_called()
        run_chat.assert_not_awaited()
        orch.autonudge_svc.note_scheduled_delivery_dispatched.assert_not_called()

    @pytest.mark.asyncio
    async def test_structured_delivery_distinguishes_busy_and_unavailable(self, monkeypatch):
        busy = _make_orchestrator()
        busy_state = _mock_dashboard_state()
        busy_slot = MagicMock(running=True, _in_stage_execution=False)
        busy_state.get_slot.return_value = busy_slot
        busy.dashboard_state = busy_state
        unavailable = _make_orchestrator()
        unavailable_state = _mock_dashboard_state()
        unavailable_state.get_slot.return_value = None
        unavailable.dashboard_state = unavailable_state
        monkeypatch.setattr(gw, "rehydrate_slot_from_history_async", AsyncMock(return_value=None))

        assert await busy._fire_dashboard_nudge(_loop("chat-1"), "[Monitor wake]") is (
            monitor_models.MonitorDispatchResult.BUSY
        )
        assert (
            await unavailable._fire_dashboard_nudge(_loop("chat-2"), "[Monitor wake]")
            is monitor_models.MonitorDispatchResult.UNAVAILABLE
        )

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
        restored._in_stage_execution = False
        restored.key = "chat-9"

        async def _rehydrate(_state, _key, *, adopt_closed=False):
            assert (
                adopt_closed is True
            ), "a nudge loop must survive its slot being archived by idle cleanup"
            return restored

        monkeypatch.setattr(gw, "rehydrate_slot_from_history_async", _rehydrate)
        ds.run_background_turn.side_effect = lambda _slot, coro: coro
        task = MagicMock()

        def discard_turn(_state, _slot, coro):
            coro.close()
            return task

        monkeypatch.setattr("kiro_crew.dashboard.chat._run_chat", MagicMock(return_value="CORO"))
        monkeypatch.setattr(gw, "spawn_guarded_turn", discard_turn)

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
    """``KIROCREW_SUBAGENT_DIGEST_HOLD_SECS`` parse guard: the
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


# ═════════════════════════════════════════════════════════════════════════
# _fire_webex_nudge  (adapter over the shared _fire_dm_nudge ladder)
# ═════════════════════════════════════════════════════════════════════════


_WKEY = "webex:kirocrew:direct:a@b.test"


def _webex_transport(
    *,
    authorized: bool = True,
    current_key: str | None = None,
    origin_room: str | None = None,
) -> MagicMock:
    """A Webex transport double exposing the surface the fire path uses.

    Note where authorization lives: Webex holds its allow-list on the TRANSPORT,
    where Discord holds it on the dispatcher. That difference is the whole reason
    the adapter supplies ``authorize`` instead of the ladder calling one of them.
    """
    dispatcher = MagicMock()
    dispatcher.current_session_key = MagicMock(
        return_value=current_key if current_key is not None else _WKEY
    )
    dispatcher.handle_message = AsyncMock(return_value=None)
    sessions = MagicMock()
    sessions.is_busy = MagicMock(return_value=False)
    sessions.get_origin_link = MagicMock(
        return_value=SimpleNamespace(channel_id=origin_room) if origin_room else None
    )
    dispatcher.sessions = sessions
    transport = MagicMock()
    transport.dispatcher = dispatcher
    transport.is_authorized = MagicMock(return_value=authorized)
    transport.resolve_conversation = AsyncMock(return_value="a@b.test")
    return transport


def _webex_orchestrator(transport: MagicMock | None) -> Any:
    orch = _make_orchestrator()
    ds = _mock_dashboard_state()
    ds.channel_transports = {"webex": transport} if transport is not None else {}
    orch.dashboard_state = ds
    orch.autonudge_svc = MagicMock()
    orch.autonudge_svc.remove = AsyncMock()
    return orch


class TestFireWebexNudge:
    """Synthetic-injection path for a Webex DM babysit loop."""

    @pytest.mark.asyncio
    async def test_no_transport_skips_without_removing_loop(self):
        """Transport not running is transient: skip, but keep the loop armed."""
        orch = _webex_orchestrator(None)
        assert await orch._fire_webex_nudge(_loop(_WKEY)) is False
        orch.autonudge_svc.remove.assert_not_called()

    @pytest.mark.asyncio
    async def test_unsupported_key_shape_retires_loop(self):
        """A key that is not ``webex:{agent}:direct:{email}`` can never route."""
        orch = _webex_orchestrator(_webex_transport())
        assert await orch._fire_webex_nudge(_loop("webex:kirocrew:space")) is False
        orch.autonudge_svc.remove.assert_awaited_once_with("loop-1")

    @pytest.mark.asyncio
    async def test_unauthorized_email_retires_loop(self):
        """The allow-list is re-checked at fire time because it can shrink."""
        transport = _webex_transport(authorized=False)
        orch = _webex_orchestrator(transport)
        assert await orch._fire_webex_nudge(_loop(_WKEY)) is False
        transport.is_authorized.assert_called_once_with("a@b.test")
        orch.autonudge_svc.remove.assert_awaited_once_with("loop-1")
        transport.dispatcher.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rotated_session_retires_loop(self):
        """Firing into a rotated key would run without the loop's context."""
        transport = _webex_transport(current_key="webex:kirocrew:direct:a@b.test:gen2")
        orch = _webex_orchestrator(transport)
        assert await orch._fire_webex_nudge(_loop(_WKEY)) is False
        orch.autonudge_svc.remove.assert_awaited_once_with("loop-1")
        transport.dispatcher.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_busy_session_skips_without_removing_loop(self):
        """A human's own turn is running, so the cycle is skipped, not queued."""
        transport = _webex_transport()
        transport.dispatcher.sessions.is_busy = MagicMock(return_value=True)
        orch = _webex_orchestrator(transport)
        assert await orch._fire_webex_nudge(_loop(_WKEY)) is False
        orch.autonudge_svc.remove.assert_not_called()

    @pytest.mark.asyncio
    async def test_delivers_a_direct_room_inbound_without_command_parsing(self):
        transport = _webex_transport()
        orch = _webex_orchestrator(transport)
        assert await orch._fire_webex_nudge(_loop(_WKEY)) is True
        transport.dispatcher.handle_message.assert_awaited_once()
        inbound = transport.dispatcher.handle_message.await_args.args[0]
        kwargs = transport.dispatcher.handle_message.await_args.kwargs
        assert kwargs == {"interpret_commands": False}
        assert inbound.person_email == "a@b.test"
        assert inbound.text.startswith("[auto-nudge cycle 1]")
        from kiro_crew.webex.transport import ROOM_DIRECT

        assert inbound.room_type == ROOM_DIRECT

    @pytest.mark.asyncio
    async def test_persisted_origin_link_wins_over_resolve_conversation(self):
        """The bind is matched by VALUE, so the nudge must reuse its spelling.

        ``resolve_conversation`` answers with the EMAIL, which delivers but is a
        second spelling of the same room. Writing that spelling would make a
        later unlink miss the binding, so a persisted link wins.
        """
        transport = _webex_transport(origin_room="ROOM_FROM_LINK")
        orch = _webex_orchestrator(transport)
        assert await orch._fire_webex_nudge(_loop(_WKEY)) is True
        inbound = transport.dispatcher.handle_message.await_args.args[0]
        assert inbound.room_id == "ROOM_FROM_LINK"
        transport.resolve_conversation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_email_is_the_first_turn_fallback_when_no_link_exists(self):
        """With no binding yet there is nothing to disagree with."""
        transport = _webex_transport(origin_room=None)
        orch = _webex_orchestrator(transport)
        assert await orch._fire_webex_nudge(_loop(_WKEY)) is True
        inbound = transport.dispatcher.handle_message.await_args.args[0]
        assert inbound.room_id == "a@b.test"
        transport.resolve_conversation.assert_awaited_once_with("a@b.test")

    @pytest.mark.asyncio
    async def test_dispatch_failure_reports_false_and_keeps_the_loop(self):
        transport = _webex_transport()
        transport.dispatcher.handle_message = AsyncMock(side_effect=RuntimeError("boom"))
        orch = _webex_orchestrator(transport)
        assert await orch._fire_webex_nudge(_loop(_WKEY)) is False
        orch.autonudge_svc.remove.assert_not_called()


# ═════════════════════════════════════════════════════════════════════════
# _fire_dm_nudge  (the shared ladder itself)
# ═════════════════════════════════════════════════════════════════════════


class TestDmFireSpineIsReusable:
    """A dispatcher-routed channel joins by supplying an adapter, not a ladder.

    This is the claim the extraction makes, so it is tested directly rather than
    argued: a channel the gateway has never heard of fires correctly, and
    inherits every guard, with no code beyond the five adapter members.
    """

    @staticmethod
    def _adapter() -> Any:
        return gw._DmDispatchAdapter(
            channel="zulip",
            supports_monitor=False,
            authorize=lambda transport, _dispatcher, principal: bool(
                transport.is_authorized(principal)
            ),
            resolve_conversation=(
                lambda transport, _sessions, _key, principal: transport.resolve_conversation(
                    principal
                )
            ),
            build_inbound=lambda principal, conversation_id, text: SimpleNamespace(
                who=principal, where=conversation_id, text=text
            ),
        )

    @staticmethod
    def _orchestrator(transport: MagicMock | None) -> Any:
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        ds.channel_transports = {"zulip": transport} if transport is not None else {}
        orch.dashboard_state = ds
        orch.autonudge_svc = MagicMock()
        orch.autonudge_svc.remove = AsyncMock()
        return orch

    @staticmethod
    def _transport(*, authorized: bool = True, current_key: str | None = None) -> MagicMock:
        key = "zulip:kirocrew:direct:z1"
        dispatcher = MagicMock()
        dispatcher.current_session_key = MagicMock(
            return_value=current_key if current_key is not None else key
        )
        dispatcher.handle_message = AsyncMock(return_value=None)
        sessions = MagicMock()
        sessions.is_busy = MagicMock(return_value=False)
        dispatcher.sessions = sessions
        transport = MagicMock()
        transport.dispatcher = dispatcher
        transport.is_authorized = MagicMock(return_value=authorized)
        transport.resolve_conversation = AsyncMock(return_value="STREAM1")
        return transport

    @pytest.mark.asyncio
    async def test_an_unknown_channel_delivers_with_only_an_adapter(self):
        transport = self._transport()
        orch = self._orchestrator(transport)
        loop = _loop("zulip:kirocrew:direct:z1")
        assert await orch._fire_dm_nudge(loop, self._adapter()) is True
        transport.dispatcher.handle_message.assert_awaited_once()
        inbound = transport.dispatcher.handle_message.await_args.args[0]
        assert (inbound.who, inbound.where) == ("z1", "STREAM1")
        assert inbound.text.startswith("[auto-nudge cycle 1]")

    @pytest.mark.asyncio
    async def test_the_new_channel_inherits_the_authorization_guard(self):
        transport = self._transport(authorized=False)
        orch = self._orchestrator(transport)
        loop = _loop("zulip:kirocrew:direct:z1")
        assert await orch._fire_dm_nudge(loop, self._adapter()) is False
        orch.autonudge_svc.remove.assert_awaited_once_with("loop-1")
        transport.dispatcher.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_new_channel_inherits_the_generation_guard(self):
        transport = self._transport(current_key="zulip:kirocrew:direct:z1:gen2")
        orch = self._orchestrator(transport)
        loop = _loop("zulip:kirocrew:direct:z1")
        assert await orch._fire_dm_nudge(loop, self._adapter()) is False
        orch.autonudge_svc.remove.assert_awaited_once_with("loop-1")
        transport.dispatcher.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_monitor_wake_on_a_monitorless_channel_delivers_nothing(self):
        """``supports_monitor=False`` refuses a wake instead of half-delivering it.

        A channel with no structured dispatch cannot report whether the wake
        landed. Delivering first and answering UNAVAILABLE afterwards would show
        the text to the reader while the controller counts the wake as
        undelivered and sends it again, so the refusal comes first.
        """
        transport = self._transport()
        orch = self._orchestrator(transport)
        loop = _loop("zulip:kirocrew:direct:z1")
        result = await orch._fire_dm_nudge(loop, self._adapter(), "wake up")
        assert result is monitor_models.MonitorDispatchResult.UNAVAILABLE
        transport.dispatcher.handle_message.assert_not_awaited()
        orch.autonudge_svc.remove.assert_not_called()
