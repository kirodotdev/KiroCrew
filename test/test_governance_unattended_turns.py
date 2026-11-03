"""Governance judges an unattended turn by its own session, and refuses when it cannot judge.

Two properties of the PreToolUse gate's governance tier on the turns nobody is
watching -- cron jobs, heartbeat tasks and monitor wakes:

* A cron turn hands the gate its own session key and agent, as an interactive
  turn does, so a profile bound to that session's surface or agent governs the
  cron turn exactly as it governs a chat turn.
* When the governance evaluation itself raises, an unattended turn is refused:
  no person is there to notice that policy was skipped. An interactive turn keeps
  the existing behavior (no governance opinion), so an ordinary user action is
  never newly refused by a transient fault. Both paths record the fault through
  the degrade audit, as failed closed or failed open.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from slack_gateway_cron_exec_coverage_helpers import (
    _cron_message_cb,
    _job,
    _make_orchestrator,
    _message_arm_sessions,
    _mock_dashboard_state,
)

from kiro_crew import hooks
from kiro_crew.hooks import HookManager, HooksConfig, unattended_turn
from kiro_crew.llm_helpers import ToolApprovalPolicy, stream_and_collect
from kiro_crew.providers.base import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    LLMEvent,
)

_BENIGN_TITLE = "Read README.md"


def _broken_profile_lookup(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Make the active-profile lookup raise, as a corrupt profile store would.

    Returns the ``failed_closed`` flag of every degrade audit the gate records.
    """
    from kiro_crew.platform import governance_profiles

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("profile store unreadable")

    audited: list[bool] = []

    def _audit(*_args: object, failed_closed: bool = False, **_kwargs: object) -> None:
        audited.append(failed_closed)

    monkeypatch.setattr(governance_profiles, "resolve_active_scope", _boom)
    monkeypatch.setattr(governance_profiles, "audit_governance_degraded", _audit)
    return audited


class TestEvaluationErrorFailsClosedOnlyWhenUnattended:
    def test_interactive_turn_keeps_no_opinion(self, monkeypatch):
        audited = _broken_profile_lookup(monkeypatch)
        ctx = SimpleNamespace(governance=None)
        reason = hooks._governance_denial(ctx, _BENIGN_TITLE, "dashboard:s1", "", "")
        assert reason is None
        # The fault is logged and recorded through the degrade audit, as failed open.
        assert audited == [False]

    def test_unattended_turn_is_refused(self, monkeypatch):
        audited = _broken_profile_lookup(monkeypatch)
        ctx = SimpleNamespace(governance=None)
        with unattended_turn():
            reason = hooks._governance_denial(ctx, _BENIGN_TITLE, "cron:j1", "", "")
        assert reason is not None
        assert reason.startswith("Blocked by governance policy:")
        # The degrade record says the call was refused, not that governance was skipped.
        assert audited == [True]

    def test_the_marker_ends_with_its_block(self, monkeypatch):
        _broken_profile_lookup(monkeypatch)
        ctx = SimpleNamespace(governance=None)
        with unattended_turn():
            pass
        assert hooks._governance_denial(ctx, _BENIGN_TITLE, "cron:j1", "", "") is None

    def test_an_inactive_marker_changes_nothing(self, monkeypatch):
        _broken_profile_lookup(monkeypatch)
        ctx = SimpleNamespace(governance=None)
        with unattended_turn(False):
            assert hooks._governance_denial(ctx, _BENIGN_TITLE, "cron:j1", "", "") is None

    def test_the_gate_refuses_an_unattended_call_and_allows_an_interactive_one(self, monkeypatch):
        _broken_profile_lookup(monkeypatch)
        manager = HookManager(HooksConfig())
        interactive = manager.on_tool_call(_BENIGN_TITLE, session_key="dashboard:s1")
        assert interactive.action != "deny"
        with unattended_turn():
            unattended = manager.on_tool_call(_BENIGN_TITLE, session_key="cron:j1")
        assert unattended.action == "deny"
        assert "governance" in (unattended.reason or "").lower()


class _ScriptedProvider:
    """Yields one permission request and records how it was answered."""

    def __init__(self) -> None:
        self.approved: list[str] = []
        self.rejected: list[str] = []

    async def stream(self, message: str) -> AsyncIterator[LLMEvent]:
        for event in (
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="on it"),
            LLMEvent(kind=EVENT_PERMISSION_REQUEST, title=_BENIGN_TITLE, request_id="r1"),
            LLMEvent(kind=EVENT_COMPLETE, text=""),
        ):
            yield event

    async def approve_tool(self, request_id: str) -> None:
        self.approved.append(request_id)

    async def reject_tool(self, request_id: str) -> None:
        self.rejected.append(request_id)


class TestStreamCarriesTheTurnToTheGate:
    @pytest.mark.asyncio
    async def test_unattended_stream_refuses_when_governance_cannot_judge(self, monkeypatch):
        _broken_profile_lookup(monkeypatch)
        provider = _ScriptedProvider()
        await stream_and_collect(
            provider,  # type: ignore[arg-type]
            "q",
            approval_policy=ToolApprovalPolicy.HOOK_BASED,
            hooks=HookManager(HooksConfig()),
            on_tool_approval=AsyncMock(return_value=True),
            retry_transient=False,
            session_key="cron:j1",
            unattended=True,
        )
        assert provider.rejected == ["r1"]
        assert provider.approved == []

    @pytest.mark.asyncio
    async def test_interactive_stream_still_reaches_its_approver(self, monkeypatch):
        _broken_profile_lookup(monkeypatch)
        provider = _ScriptedProvider()
        await stream_and_collect(
            provider,  # type: ignore[arg-type]
            "q",
            approval_policy=ToolApprovalPolicy.HOOK_BASED,
            hooks=HookManager(HooksConfig()),
            on_tool_approval=AsyncMock(return_value=True),
            retry_transient=False,
            session_key="dashboard:s1",
        )
        assert provider.approved == ["r1"]
        assert provider.rejected == []

    @pytest.mark.asyncio
    async def test_a_session_rule_judges_a_cron_stream_as_it_judges_a_chat_stream(
        self, monkeypatch
    ):
        """The gate sees the session key, so a rule keyed on it fires on both."""
        seen: list[str] = []

        def _gov(ctx, title, session_key, *args, **kwargs):
            seen.append(session_key)
            if session_key in ("cron:j1", "dashboard:s1"):
                return "Blocked by governance policy: session rule"
            return None

        monkeypatch.setattr(hooks, "_governance_denial", _gov)
        for key, unattended in (("dashboard:s1", False), ("cron:j1", True)):
            provider = _ScriptedProvider()
            await stream_and_collect(
                provider,  # type: ignore[arg-type]
                "q",
                approval_policy=ToolApprovalPolicy.HOOK_BASED,
                hooks=HookManager(HooksConfig()),
                on_tool_approval=AsyncMock(return_value=True),
                retry_transient=False,
                session_key=key,
                unattended=unattended,
            )
            assert provider.rejected == ["r1"], key
        assert seen == ["dashboard:s1", "cron:j1"]


class TestCronTurnPassesItsIdentity:
    @pytest.mark.asyncio
    async def test_cron_turn_hands_the_gate_its_session_key_and_marks_itself_unattended(
        self, monkeypatch
    ):
        from kiro_crew.slack import gateway as gw

        monkeypatch.setattr(gw.live, "snapshot", lambda: None)
        orch = _make_orchestrator()
        orch.sessions = _message_arm_sessions()
        orch.dashboard_state = _mock_dashboard_state()
        job = _job(id="j1", name="probe")

        async with _cron_message_cb(orch, result_text="ok") as callback:
            turn = gw._cron_stream_with_posttoken_resume
            await callback(job)

        assert isinstance(turn, AsyncMock)
        turn.assert_awaited_once()
        session_key = orch.sessions.get_or_create.await_args.args[0]
        kwargs = turn.await_args.kwargs
        assert kwargs["session_key"] == session_key == "cron:j1"
        assert kwargs["unattended"] is True


class TestOtherUnattendedSurfacesMarkTheirTurns:
    @pytest.mark.asyncio
    async def test_heartbeat_task_streams_as_unattended(self, monkeypatch):
        from test_heartbeat_task_timeout import _capture_task
        from test_heartbeat_task_timeout import _make_orchestrator as _heartbeat_orchestrator

        import kiro_crew.slack.gateway as gw_mod

        orch, _sessions = _heartbeat_orchestrator()
        stream = AsyncMock(return_value="all good")
        monkeypatch.setattr(gw_mod, "stream_and_collect", stream)

        on_task = await _capture_task(orch)
        assert await on_task("do a thing", "") == "all good"
        assert stream.await_args.kwargs["unattended"] is True

    def test_channel_monitor_wake_streams_as_unattended(self, monkeypatch):
        from test_turn_duration_slack import _build_orchestrator, _fake_client

        from kiro_crew.acp.types import TurnUsage
        from kiro_crew.autonudge import NudgeLoop
        from kiro_crew.slack import gateway as gw

        orch = _build_orchestrator(_fake_client())
        monkeypatch.setattr(gw, "provider_last_turn_usage", lambda _c: TurnUsage())
        stream = AsyncMock(return_value="done")
        monkeypatch.setattr(gw, "stream_and_collect", stream)

        loop = NudgeLoop(id="loop-u", slot_key="slack:111.222", message="check")
        assert asyncio.run(orch._fire_slack_nudge(loop)) is True
        assert stream.await_args.kwargs["unattended"] is True


class TestDashboardMonitorWakeIsJudgedUnattended:
    @staticmethod
    async def _gate_saw_unattended(tmp_path, **run_kwargs: object) -> list[bool]:
        from dashboard_approval_helpers import (
            _async_iter,
            _complete_event,
            _context_builder,
            _make_slot,
            _make_state,
            _patch_stats,
            _permission_event,
        )

        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.hooks import ToolHookResult

        seen: list[bool] = []

        def _on_tool_call(*_args: object, **_kwargs: object) -> ToolHookResult:
            seen.append(hooks._UNATTENDED_TURN.get())
            return ToolHookResult.deny("refused for the test")

        cb = _context_builder()
        cb.hooks.on_tool_call.side_effect = _on_tool_call
        state, client = _make_state(tmp_path, context_builder=cb)
        slot = _make_slot()
        client.context_usage_pct = MagicMock(return_value=0.0)
        client._client = client
        client.last_prompt_stats = None
        calls = {"n": 0}

        def _stream(*_args: object, **_kwargs: object):
            calls["n"] += 1
            if calls["n"] == 1:
                return _async_iter([_permission_event(title="fs_write"), _complete_event()])
            return _async_iter([_complete_event()])

        client.stream = MagicMock(side_effect=_stream)
        with _patch_stats():
            await _run_chat(state, slot, "hello", **run_kwargs)
            if slot.task:
                await slot.task
        return seen

    @pytest.mark.asyncio
    async def test_a_monitor_wake_consults_the_gate_as_unattended(self, tmp_path):
        assert await self._gate_saw_unattended(tmp_path, _directive_self_wake=True) == [True]

    @pytest.mark.asyncio
    async def test_a_typed_turn_consults_the_gate_as_attended(self, tmp_path):
        assert await self._gate_saw_unattended(tmp_path) == [False]


class TestChannelToolGateCarriesTheMarker:
    @pytest.mark.parametrize("unattended", [True, False])
    def test_the_gate_consults_the_hooks_under_its_turns_marker(self, unattended):
        from kiro_crew.messaging.dispatch_tools import build_tool_gate

        seen: list[bool] = []

        def _on_tool_call(*_args: object, **_kwargs: object) -> hooks.ToolHookResult:
            seen.append(hooks._UNATTENDED_TURN.get())
            return hooks.ToolHookResult.allow()

        ctx_builder = SimpleNamespace(hooks=SimpleNamespace(on_tool_call=_on_tool_call))
        gate = build_tool_gate(ctx_builder, session_key="s", agent="a", unattended=unattended)
        assert gate(SimpleNamespace(title=_BENIGN_TITLE)) == ""
        assert seen == [unattended]
        assert hooks._UNATTENDED_TURN.get() is False
