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

This file holds the Discord and Slack nudges. The ``_fire`` router and ``_observer``,
and the dashboard, Webex and shared DM nudge ladders, are in
``test_slack_gateway_coverage_nudge_routing.py``; the notification closures, the
channel transport gate and the module-level helpers in
``test_slack_gateway_coverage_notifications.py``; the MCP-gateway control plane in
``test_slack_gateway_coverage_mcp_broker.py``. ``slack_gateway_coverage_helpers.py``
holds the builders the four share.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from slack_gateway_coverage_helpers import (
    _DKEY,
    _awaited,
    _loop,
    _make_orchestrator,
    _mock_dashboard_state,
    _within_lost_run,
)

from kiro_crew import session_directive
from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TOOL_CALL, EVENT_TOOL_RESULT, AcpEvent
from kiro_crew.autonudge import (
    AutoNudgeService,
)
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
        orch.autonudge_svc.remove.assert_awaited_once_with("loop-1", stop_reason="unsupported_key")

    @pytest.mark.asyncio
    async def test_unauthorized_user_retires_loop(self):
        """The allowlist can shrink after a loop was created — re-check at fire time."""
        orch = _discord_orchestrator(_discord_transport(authorized=False))
        assert await orch._fire_discord_nudge(_loop(_DKEY)) is False
        orch.autonudge_svc.remove.assert_awaited_once_with(
            "loop-1", stop_reason="user_not_authorized"
        )

    @pytest.mark.asyncio
    async def test_rotated_session_retires_loop(self):
        """A `!new` generation bump means the monitored conversation is gone."""
        transport = _discord_transport(current_key="discord:kirocrew:direct:U9:gen2")
        orch = _discord_orchestrator(transport)
        assert await orch._fire_discord_nudge(_loop(_DKEY)) is False
        orch.autonudge_svc.remove.assert_awaited_once_with("loop-1", stop_reason="session_rotated")
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

        # Bounded by the patched 0.01 s turn bound, which is what ends the
        # wedged turn. The outer wait only exists so a call that never returns
        # fails instead of hanging, so it uses the load-safe lost-run ceiling
        # like the sibling nudge-timeout tests: the pre-turn chain makes real
        # executor hops, and a tight hand-rolled deadline cancelled this call
        # from the outside on a loaded runner (raw CancelledError) before the
        # inner bound was reached.
        result = await _within_lost_run(
            orch._fire_discord_nudge(loop, "[Monitor wake]"),
            "the wedged Discord nudge turn",
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

        # Bounded: only the patched 0.01 s turn bound ends the stalled authorization.
        result = await _within_lost_run(
            orch._fire_slack_nudge(loop, "[Monitor wake]"), "the nudge with a stalled authorization"
        )

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
        orch.autonudge_svc.remove.assert_awaited_once_with("loop-1", stop_reason="slack_unroutable")

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

        # Bounded: with times_out only the patched 0.01 s turn bound ends the stream.
        with pytest.raises(asyncio.CancelledError):
            await _within_lost_run(orch._fire_slack_nudge(loop), "the nudge turn")

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
        try:
            # The pre-turn chain makes real executor hops before the stream starts.
            await _within_lost_run(completed.wait(), "the nudge turn's completion")
        finally:
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await _within_lost_run(task, "the cancelled nudge")

        service.record_monitor_turn_completion.assert_awaited_once()
        completion = _awaited(service.record_monitor_turn_completion).args[0]
        assert completion.disposition is MonitorActionDisposition.FAILURE

    @pytest.mark.asyncio
    async def test_shutdown_drains_the_reply_bookkeeping_instead_of_cutting_it(
        self, tmp_path, monkeypatch
    ):
        """A stop landing after the provider returned waits for the usage row, the
        thread post and the replay save of that reply rather than cancelling them."""
        service = AutoNudgeService(base_dir=tmp_path)
        orch = _slack_nudge_orchestrator()
        orch.autonudge_svc = service
        orch.conv_log = object()
        posting = asyncio.Event()
        release_post = asyncio.Event()
        steps: list[str] = []

        async def _stream_ok(*_a, **_k):
            return "reply body"

        async def _persist(*_a, **_k):
            steps.append("turn row")

        async def _post(*_a, **_k):
            posting.set()
            await release_post.wait()
            steps.append("reply post")

        async def _save(*_a, **_k):
            steps.append("replay save")

        monkeypatch.setattr(gw, "stream_and_collect", _stream_ok)
        monkeypatch.setattr(gw, "_persist_turn_row", _persist)
        monkeypatch.setattr(gw, "save_conversation_turn_off_loop", _save)
        orch.slack.post_message = AsyncMock(side_effect=_post)

        fire = asyncio.create_task(orch._fire_slack_nudge(_loop()))
        # It runs as an admitted timer callback, which shutdown cancels outside a
        # drained section.
        service._running_callbacks.add(fire)
        shutdown: asyncio.Task[None] | None = None
        try:
            await _within_lost_run(posting.wait(), "the reply's thread post")
            shutdown = asyncio.create_task(service.shutdown())

            async def _admission_closed() -> None:
                while service._accepting_mutations:
                    await asyncio.sleep(0)

            await _within_lost_run(_admission_closed(), "shutdown closing admission")
            # One more turn delivers any cancellation shutdown issued.
            await asyncio.sleep(0)
            assert not fire.done(), "shutdown cut the reply's bookkeeping"
            assert not shutdown.done(), "shutdown did not wait for the registered section"

            release_post.set()
            assert await _within_lost_run(fire, "the nudge's bookkeeping") is True
            await _within_lost_run(shutdown, "the drain waiting on the bookkeeping")
        finally:
            release_post.set()
            pending = [task for task in (fire, shutdown) if task is not None and not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                await _within_lost_run(
                    asyncio.gather(*pending, return_exceptions=True),
                    "the bookkeeping drain test cleanup",
                )
            service.stop()

        assert steps == ["turn row", "reply post", "replay save"]
        assert service._inflight_adds == set()

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
