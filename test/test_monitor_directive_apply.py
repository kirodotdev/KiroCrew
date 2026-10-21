"""Tests for the monitor directives a session applies (``apply_session_directive``).

Among them: routing between structured monitors and legacy loops, which surfaces may watch,
update or stop a loop, the audit outcome of a refusal, the notice a refused revision surfaces
into the session, a structured monitor retained by a session close and restored when the close
fails, and ``monitor_update``'s judge clear. What a monitor's own wake may do is tested in
``test_monitor_directive_apply_wake_authority.py``, and a stop's write racing another change to
the same loop in ``test_monitor_directive_apply_stop_races.py``.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.autonudge import (
    APPROVAL_STALL_REASON,
    CONSECUTIVE_FAILURE_REASON,
    STOP_SENTINEL_REASON,
    AutoNudgeService,
)
from kiro_crew.autonudge_authz import authorize_and_update_monitor
from kiro_crew.dashboard import session_directive_apply as sda
from kiro_crew.dashboard.session_directive_apply import apply_session_directive
from kiro_crew.monitoring.models import (
    MonitorBudgets,
    MonitorCreationSurface,
    MonitorDispatchResult,
    MonitorOutcome,
)


@pytest.mark.asyncio
async def test_channel_origin_survives_a_linked_dashboard_monitor_binding(tmp_path):
    service = AutoNudgeService(base_dir=tmp_path)
    state = SimpleNamespace(
        _slots={"chat-1": SimpleNamespace(workspace="default", is_closing=False)},
        sessions=None,
        channel_transports={},
    )
    with (
        patch("kiro_crew.autonudge.get_instance", return_value=service),
        patch("kiro_crew.autonudge_authz.sel", return_value=MagicMock()),
    ):
        result = await apply_session_directive(
            state,
            SimpleNamespace(key="chat-1", _app=""),
            "dashboard:chat-1",
            "monitor_watch",
            {
                "kind": "bitbucket_pull_request",
                "target": "https://bitbucket.org/acme/widgets/pull-requests/10",
                "objective": "review_ready",
                "cadence_secs": 60,
                "max_runtime_secs": 600,
                "max_agent_turns": 4,
                "max_tokens": 10_000,
                "max_provider_errors": 2,
                "wake_instructions": "Check CI.",
            },
            producer_is_user_facing=True,
            producer_is_channel=True,
        )

    assert "started" in result
    loop = service.get_by_slot("chat-1")
    assert loop is not None and loop.monitor is not None
    assert loop.monitor.creation_surface is MonitorCreationSurface.CHANNEL
    service.stop()


@pytest.mark.asyncio
async def test_channel_origin_retarget_downgrades_dashboard_monitor_credentials(tmp_path):
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add_monitor(
        slot_key="chat-1",
        kind="bitbucket_pull_request",
        target="https://bitbucket.org/acme/widgets/pull-requests/10",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(),
        creation_surface=MonitorCreationSurface.DASHBOARD,
    )
    state = SimpleNamespace(
        _slots={"chat-1": SimpleNamespace(workspace="default", is_closing=False)},
        sessions=None,
        channel_transports={},
    )
    try:
        with (
            patch("kiro_crew.autonudge.get_instance", return_value=service),
            patch("kiro_crew.autonudge_authz.sel", return_value=MagicMock()),
        ):
            result = await apply_session_directive(
                state,
                SimpleNamespace(key="chat-1", _app=""),
                "dashboard:chat-1",
                "monitor_update",
                {"patch": {"target": "https://bitbucket.org/acme/widgets/pull-requests/11"}},
                producer_is_user_facing=True,
                producer_is_channel=True,
            )
        monitor = loop.monitor
    finally:
        service.stop()
    assert "updated" in result
    assert monitor is not None
    assert monitor.target == "https://bitbucket.org/acme/widgets/pull-requests/11"
    assert monitor.creation_surface is MonitorCreationSurface.CHANNEL


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["monitor_watch", "monitor_update", "monitor_stop"])
async def test_disabled_structured_monitor_directives_are_audited_as_denied(kind):
    audit = MagicMock()
    with (
        patch("kiro_crew.autonudge.get_instance", return_value=None),
        patch("kiro_crew.sel.sel", return_value=audit),
    ):
        result = await apply_session_directive(
            SimpleNamespace(),
            SimpleNamespace(key="chat-1", _app=""),
            "dashboard:chat-1",
            kind,
            {},
        )

    assert "disabled" in result
    audit.log_tool_invocation.assert_called_once_with(
        session_key="dashboard:chat-1",
        source="mcp-directive",
        tool_name=kind,
        outcome="denied",
    )


@pytest.mark.asyncio
async def test_unsupported_structured_monitor_stop_is_audited_as_denied(tmp_path):
    service = AutoNudgeService(base_dir=tmp_path)
    audit = MagicMock()
    with (
        patch("kiro_crew.autonudge.get_instance", return_value=service),
        patch("kiro_crew.dashboard.session_directive_apply._audit", audit),
    ):
        result = await apply_session_directive(
            SimpleNamespace(),
            SimpleNamespace(key="unsupported-1", _app=""),
            "telegram:unsupported-1",
            "monitor_stop",
            {},
        )

    assert "not supported" in result
    audit.assert_called_once_with("telegram:unsupported-1", "monitor_stop", "denied")
    service.stop()


@pytest.mark.asyncio
async def test_webex_structured_watch_is_refused_by_authoritative_consumer(tmp_path):
    service = AutoNudgeService(base_dir=tmp_path)
    audit = MagicMock()
    session_key = "webex:kirocrew:direct:operator@example.com"
    with (
        patch("kiro_crew.autonudge.get_instance", return_value=service),
        patch("kiro_crew.dashboard.session_directive_apply._audit", audit),
    ):
        result = await apply_session_directive(
            SimpleNamespace(),
            None,
            session_key,
            "monitor_watch",
            {
                "kind": "github_pull_request",
                "target": "https://github.com/acme/widgets/pull/7",
                "objective": "review_ready",
                "cadence_secs": 300,
                "max_runtime_secs": 14_400,
                "max_agent_turns": 8,
                "max_tokens": 250_000,
                "max_provider_errors": 3,
                "wake_instructions": "Inspect the blocker.",
            },
        )

    assert "not supported" in result
    assert service.get_by_slot(session_key) is None
    audit.assert_called_once_with(session_key, "monitor_watch", "denied")
    service.stop()


@pytest.mark.asyncio
async def test_webex_legacy_loop_is_stopped_by_monitor_stop(tmp_path):
    """monitor_stop binds the general key, so a Webex legacy loop stops here.

    Webex hosts a legacy timer loop but no structured monitor. Before the stop
    resolved the general binding, this call was refused as an unsupported
    session type; now it stops the loop the session was allowed to arm.
    """
    service = AutoNudgeService(base_dir=tmp_path)
    audit = MagicMock()
    session_key = "webex:kirocrew:direct:operator@example.com"
    loop = await service.add(
        slot_key=session_key,
        message="Watch the pull request.",
        idle_secs=300,
    )
    try:
        with (
            patch("kiro_crew.autonudge.get_instance", return_value=service),
            patch("kiro_crew.dashboard.session_directive_apply._audit", audit),
        ):
            result = await apply_session_directive(
                SimpleNamespace(),
                None,
                session_key,
                "monitor_stop",
                {"reason": "done"},
            )

        assert not result.startswith("Error:")
        assert "stopped" in result
        # A legacy loop is REMOVED, not retained: nothing is left to inspect.
        assert service.get_by_slot(session_key) is None
        audit.assert_called_once_with(session_key, "monitor_stop", "success")
    finally:
        service.stop()
    # Referenced so a future reader sees the armed loop id is not asserted on.
    assert loop is not None


@pytest.mark.asyncio
async def test_monitor_stop_removes_a_dashboard_legacy_loop(tmp_path):
    """The defect this fix targets: monitor_stop on a legacy timer loop.

    monitor_stop resolved only a structured monitor, so a session that armed a
    timer loop and called monitor_stop got a silent no-op while the loop kept
    firing. Binding the general key makes it stop the legacy loop.
    """
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add(slot_key="chat-1", message="Watch it.", idle_secs=300)
    try:
        with (
            patch("kiro_crew.autonudge.get_instance", return_value=service),
            patch("kiro_crew.dashboard.session_directive_apply._audit", MagicMock()),
        ):
            result = await apply_session_directive(
                SimpleNamespace(),
                SimpleNamespace(key="chat-1", _app=""),
                "dashboard:chat-1",
                "monitor_stop",
                {"reason": "done"},
            )

        assert not result.startswith("Error:")
        assert "stopped" in result
        assert service.get_by_slot("chat-1") is None
    finally:
        service.stop()
    assert loop is not None


@pytest.mark.asyncio
async def test_monitor_stop_and_autonudge_stop_route_a_structured_loop_identically(tmp_path):
    """One implementation, two entry points: both retain the structured record.

    A structured monitor stopped through either tool is retained for inspection
    rather than removed, because both delegate to the same resolve-and-route
    path.
    """
    for kind in ("monitor_stop", "autonudge_stop"):
        service = AutoNudgeService(base_dir=tmp_path / kind)
        loop = await service.add_monitor(
            slot_key="chat-1",
            kind="github_pull_request",
            target="https://github.com/acme/widgets/pull/7",
            objective="review_ready",
            cadence_secs=300,
            budgets=MonitorBudgets(),
        )
        try:
            with (
                patch("kiro_crew.autonudge.get_instance", return_value=service),
                patch("kiro_crew.autonudge_authz.sel", return_value=MagicMock()),
                patch("kiro_crew.dashboard.session_directive_apply._audit", MagicMock()),
            ):
                result = await apply_session_directive(
                    SimpleNamespace(),
                    SimpleNamespace(key="chat-1", _app=""),
                    "dashboard:chat-1",
                    kind,
                    {"reason": "done"},
                )

            assert "retained for inspection" in result, kind
            # Retained, not removed: the record survives for monitor_inspect.
            retained = service.get_by_slot("chat-1")
            assert retained is not None, kind
            assert not retained.active, kind
        finally:
            service.stop()
        assert loop is not None


@pytest.mark.asyncio
async def test_webex_cannot_update_a_persisted_structured_monitor(tmp_path):
    service = AutoNudgeService(base_dir=tmp_path)
    session_key = "webex:kirocrew:direct:operator@example.com"
    loop = await service.add_monitor(
        slot_key=session_key,
        kind="github_pull_request",
        target="https://github.com/acme/widgets/pull/7",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(),
    )
    audit = MagicMock()
    with (
        patch("kiro_crew.autonudge.get_instance", return_value=service),
        patch("kiro_crew.dashboard.session_directive_apply._audit", audit),
    ):
        result = await apply_session_directive(
            SimpleNamespace(),
            None,
            session_key,
            "monitor_update",
            {"patch": {"idle_secs": 120}},
        )

    assert "not supported" in result
    assert loop.monitor is not None and loop.monitor.cadence_secs == 60
    audit.assert_called_once_with(session_key, "monitor_update", "denied")
    service.stop()


@pytest.mark.asyncio
async def test_refused_structured_monitor_stop_is_audited_as_denied(tmp_path):
    service = AutoNudgeService(base_dir=tmp_path)
    await service.add_monitor(
        slot_key="chat-1",
        kind="github_pull_request",
        target="https://github.com/acme/widgets/pull/7",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(),
    )
    audit = MagicMock()
    with (
        patch("kiro_crew.autonudge.get_instance", return_value=service),
        patch(
            "kiro_crew.autonudge_authz.authorize_and_stop_monitor",
            new=AsyncMock(return_value=(None, "audit unavailable", 503)),
        ),
        patch("kiro_crew.dashboard.session_directive_apply._audit", audit),
    ):
        result = await apply_session_directive(
            SimpleNamespace(),
            SimpleNamespace(key="chat-1", _app=""),
            "dashboard:chat-1",
            "monitor_stop",
            {},
        )

    assert "Failed to stop structured monitor" in result
    audit.assert_called_once_with("dashboard:chat-1", "monitor_stop", "denied")
    service.stop()


@pytest.mark.asyncio
async def test_watch_update_and_stop_are_authoritative_and_owned(tmp_path):
    service = AutoNudgeService(base_dir=tmp_path)
    state = SimpleNamespace(
        _slots={"chat-1": SimpleNamespace(workspace="default", is_closing=False)},
        sessions=None,
        channel_transports={},
    )
    slot = SimpleNamespace(key="chat-1", _app="")
    audit = MagicMock()
    load_hosts = AsyncMock(return_value=frozenset())
    with (
        patch("kiro_crew.autonudge.get_instance", return_value=service),
        patch("kiro_crew.autonudge_authz.sel", return_value=audit),
        patch(
            "kiro_crew.dashboard.handlers.source_providers.ensure_gitlab_hosts_loaded",
            load_hosts,
        ),
    ):
        created = await apply_session_directive(
            state,
            slot,
            "dashboard:chat-1",
            "monitor_watch",
            {
                "kind": "github_pull_request",
                "target": "https://github.com/acme/widgets/pull/7",
                "objective": "review_ready",
                "cadence_secs": 60,
                "max_runtime_secs": 600,
                "max_agent_turns": 4,
                "max_tokens": 10000,
                "max_provider_errors": 2,
                "wake_instructions": "Check CI.",
            },
        )
        assert "started" in created
        loop = service.get_by_slot("chat-1")
        assert loop is not None and loop.monitor is not None
        loop.monitor.last_observation = {"head_revision": "old"}
        loop.monitor.last_fingerprint = "old-fp"
        loop.monitor.last_wake_fingerprint = "old-wake"
        loop.monitor.last_completion_fingerprint = "old-completion"
        loop.monitor.consecutive_provider_errors = 2
        baseline = await apply_session_directive(
            state,
            slot,
            "dashboard:chat-1",
            "monitor_update",
            {
                "patch": {
                    "idle_secs": 120,
                    "max_tokens": 20_000,
                    "wake_instructions": "Check AKIAIOSFODNN7EXAMPLE review threads.",
                }
            },
        )
        assert "updated" in baseline
        assert loop.monitor.last_fingerprint == "old-fp"
        assert loop.monitor.last_wake_fingerprint == "old-wake"
        assert loop.monitor.last_completion_fingerprint == "old-completion"
        assert loop.monitor.consecutive_provider_errors == 2
        assert "AKIAIOSFODNN7EXAMPLE" not in loop.monitor.wake_instructions
        reset = await apply_session_directive(
            state,
            slot,
            "dashboard:chat-1",
            "monitor_update",
            {"patch": {"target": "https://github.com/acme/widgets/pull/8"}},
        )
        assert "updated" in reset
        assert loop.monitor.last_fingerprint == ""
        assert loop.monitor.last_observation == {}
        assert loop.monitor.last_wake_fingerprint == ""
        assert loop.monitor.last_completion_fingerprint == ""
        assert loop.monitor.consecutive_provider_errors == 0
        load_hosts.assert_awaited_once_with()
        stopped = await apply_session_directive(
            state,
            slot,
            "dashboard:chat-1",
            "monitor_stop",
            {"reason": "done AKIAIOSFODNN7EXAMPLE"},
        )
    assert "stopped" in stopped
    assert loop.monitor.outcome is MonitorOutcome.USER_STOP
    assert "done" in loop.monitor.user_stop_reason
    assert "AKIAIOSFODNN7EXAMPLE" not in loop.monitor.user_stop_reason
    assert service.get_by_slot("chat-1") is loop
    critical = [
        call.kwargs
        for call in audit.log_tool_invocation.call_args_list
        if call.kwargs.get("critical") is True
    ]
    assert {entry["tool_name"] for entry in critical} == {
        "monitor_watch",
        "monitor_update",
        "monitor_stop",
    }
    assert {entry["session_key"] for entry in critical} == {"chat-1"}
    service.stop()


@pytest.mark.asyncio
async def test_legacy_autonudge_stop_retains_only_structured_records(tmp_path):
    service = AutoNudgeService(base_dir=tmp_path)
    structured = await service.add_monitor(
        slot_key="chat-1",
        kind="github_pull_request",
        target="https://github.com/acme/widgets/pull/7",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(),
    )
    state = SimpleNamespace(_slots={}, sessions=None, channel_transports={})
    slot = SimpleNamespace(key="chat-1", _app="")
    with patch("kiro_crew.autonudge.get_instance", return_value=service):
        result = await apply_session_directive(
            state, slot, "dashboard:chat-1", "autonudge_stop", {"reason": "legacy caller"}
        )
    assert result.startswith(f"Structured monitor {structured.id} stopped and retained")
    assert "No further monitor wakes" in result
    assert service.get_by_slot("chat-1") is structured
    assert structured.monitor is not None
    assert structured.monitor.outcome is MonitorOutcome.USER_STOP
    service.stop()


@pytest.mark.asyncio
async def test_legacy_structured_stop_failure_is_audited_as_denied(tmp_path):
    service = AutoNudgeService(base_dir=tmp_path)
    await service.add_monitor(
        slot_key="chat-1",
        kind="github_pull_request",
        target="https://github.com/acme/widgets/pull/7",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(),
    )
    audit = MagicMock()
    with (
        patch("kiro_crew.autonudge.get_instance", return_value=service),
        patch(
            "kiro_crew.autonudge_authz.authorize_and_stop_monitor",
            new=AsyncMock(return_value=(None, "audit unavailable", 503)),
        ),
        patch("kiro_crew.dashboard.session_directive_apply._audit", audit),
    ):
        result = await apply_session_directive(
            SimpleNamespace(),
            SimpleNamespace(key="chat-1", _app=""),
            "dashboard:chat-1",
            "autonudge_stop",
            {"reason": "legacy caller"},
        )

    assert "Failed to stop structured monitor" in result
    audit.assert_called_once_with("dashboard:chat-1", "autonudge_stop", "denied")
    service.stop()


@pytest.mark.asyncio
async def test_session_close_is_retained_and_failed_close_can_rollback(tmp_path):
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add_monitor(
        slot_key="chat-1",
        kind="github_pull_request",
        target="https://github.com/acme/widgets/pull/7",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(),
        now=100.0,
    )

    await service.retire_monitor_for_session_close(loop.id, now=120.0)
    assert loop.monitor is not None
    assert loop.monitor.outcome is MonitorOutcome.SESSION_CLOSE
    assert service.get_by_slot("chat-1") is loop

    await service.restore_monitor_after_failed_session_close(loop.id, now=125.0)
    assert loop.active
    assert loop.monitor.outcome is None
    assert loop.next_due_ts == loop.monitor.next_probe_at == 185.0
    service.stop()


@pytest.mark.asyncio
async def test_failed_session_close_restores_dispatched_completion_evidence(tmp_path):
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add_monitor(
        slot_key="chat-1",
        kind="github_pull_request",
        target="https://github.com/acme/widgets/pull/7",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(),
        now=100.0,
    )
    assert loop.monitor is not None
    fingerprint = "actionable-fingerprint"
    assert await service.mark_monitor_action_in_flight(loop.id, fingerprint, now=105.0)
    service.mark_monitor_turn_accepted(loop.id, fingerprint)
    await service.record_monitor_dispatched(loop.id, fingerprint, now=110.0)
    deadline = loop.monitor.completion_evidence_deadline

    await service.retire_monitor_for_session_close(loop.id, now=120.0)

    assert loop.monitor.wake_in_flight is True
    assert loop.monitor.wake_delivery is MonitorDispatchResult.DISPATCHED
    assert loop.monitor.completion_evidence_deadline == deadline
    assert loop.next_due_ts == loop.monitor.next_probe_at == deadline

    await service.restore_monitor_after_failed_session_close(loop.id, now=125.0)

    assert loop.active is True
    assert loop.monitor.outcome is None
    assert loop.monitor.wake_in_flight is True
    assert loop.monitor.wake_delivery is MonitorDispatchResult.DISPATCHED
    assert loop.next_due_ts == loop.monitor.next_probe_at == deadline
    service.stop()


@pytest.mark.asyncio
async def test_structured_fields_cannot_silently_patch_a_legacy_loop(tmp_path):
    service = AutoNudgeService(base_dir=tmp_path)
    await service.add("chat-1", "legacy prompt", idle_secs=60)
    with patch("kiro_crew.autonudge.get_instance", return_value=service):
        result = await apply_session_directive(
            SimpleNamespace(),
            SimpleNamespace(key="chat-1", _app=""),
            "dashboard:chat-1",
            "monitor_update",
            {"patch": {"target": "https://github.com/acme/widgets/pull/7"}},
        )

    assert result.startswith("monitor_update cannot apply")
    assert "structured fields" in result
    service.stop()


class _SteppedTime:
    """The ``time`` module with ``time()`` stepped by *step* seconds; every other name is real.

    Bound as a module's own ``time``, never over the stdlib clock, so the event loop and
    every other module keep the real one.
    """

    def __init__(self, step: float) -> None:
        self._step = step

    def time(self) -> float:
        return time.time() + self._step

    def __getattr__(self, name: str):
        return getattr(time, name)


@pytest.mark.parametrize("step", [0.0, 7200.0], ids=["no_step", "forward_step"])
@pytest.mark.asyncio
async def test_budget_raise_is_judged_on_the_timers_clock(tmp_path, monkeypatch, step):
    """The spent-budget guard reads the loop's age as the timer reads it.

    The loop is 30 minutes into a 1 hour budget. Read as ``time.time() - created_ts``,
    a 2 hour forward step makes it 2.5 hours old, and a raise to 2 hours is refused as
    spent while the timer would still fire.
    """
    from kiro_crew.autonudge_service.model import runtime_budget_exceeded

    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add("chat-1", "legacy prompt", idle_secs=600, max_runtime_secs=3600)
    try:
        loop.created_ts = time.time() - 1800
        runtime_budget_exceeded(loop)  # the timer's read in this process
        from kiro_crew.autonudge_service import model, mutations

        for module in (sda, model, mutations):
            monkeypatch.setattr(module, "time", _SteppedTime(step))
        with patch("kiro_crew.autonudge.get_instance", return_value=service):
            result = await apply_session_directive(
                SimpleNamespace(),
                SimpleNamespace(key="chat-1", _app=""),
                "dashboard:chat-1",
                "monitor_update",
                {"patch": {"max_runtime_secs": 7200}},
            )

        assert "elapsed runtime" not in result, result
        assert service.get_by_slot("chat-1").max_runtime_secs == 7200, result
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_monitor_watch_does_not_replace_a_legacy_loop(tmp_path):
    service = AutoNudgeService(base_dir=tmp_path)
    legacy = await service.add("chat-1", "legacy prompt", idle_secs=60)
    slot = SimpleNamespace(key="chat-1", workspace="default", _app="", is_closing=False)
    state = SimpleNamespace(_slots={"chat-1": slot}, sessions=None, channel_transports={})
    with patch("kiro_crew.autonudge.get_instance", return_value=service):
        result = await apply_session_directive(
            state,
            slot,
            "dashboard:chat-1",
            "monitor_watch",
            {
                "kind": "github_pull_request",
                "target": "https://github.com/acme/widgets/pull/7",
                "objective": "review_ready",
                "cadence_secs": 300,
                "max_runtime_secs": 14_400,
                "max_agent_turns": 8,
                "max_tokens": 250_000,
                "max_provider_errors": 3,
                "wake_instructions": "Inspect the blocker.",
            },
        )

    assert result.startswith("Failed to start structured monitor")
    assert service.get_by_slot("chat-1") is legacy
    service.stop()


@pytest.mark.asyncio
async def test_monitor_start_does_not_replace_a_structured_monitor(tmp_path):
    service = AutoNudgeService(base_dir=tmp_path)
    structured = await service.add_monitor(
        slot_key="chat-1",
        kind="github_pull_request",
        target="https://github.com/acme/widgets/pull/7",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(),
    )
    slot = SimpleNamespace(key="chat-1", workspace="default", _app="", is_closing=False)
    state = SimpleNamespace(_slots={"chat-1": slot}, sessions=None, channel_transports={})
    with patch("kiro_crew.autonudge.get_instance", return_value=service):
        result = await apply_session_directive(
            state,
            slot,
            "dashboard:chat-1",
            "monitor_start",
            {"message": "legacy prompt", "idle_secs": 60},
        )

    assert result.startswith("Failed to start monitor loop")
    assert service.get_by_slot("chat-1") is structured
    service.stop()


@pytest.mark.asyncio
async def test_identity_update_conflict_is_a_controlled_authorizer_denial(tmp_path):
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add_monitor(
        slot_key="chat-1",
        kind="github_pull_request",
        target="https://github.com/acme/widgets/pull/7",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(),
        now=100.0,
    )
    assert await service.mark_monitor_action_in_flight(loop.id, "failure-a", now=120.0)

    with patch("kiro_crew.autonudge_authz.sel", return_value=MagicMock()):
        updated, error, status = await authorize_and_update_monitor(
            svc=service,
            state=SimpleNamespace(
                _slots={loop.slot_key: SimpleNamespace(mode="", memory_mode="persistent")}
            ),
            loop_id=loop.id,
            session_key=loop.slot_key,
            patch={"target": "https://github.com/acme/widgets/pull/8"},
            source="dashboard",
        )

    assert updated is None
    assert status == 409
    assert error is not None and "wake is in flight" in error
    assert loop.monitor is not None
    assert loop.monitor.target == "https://github.com/acme/widgets/pull/7"
    assert loop.monitor.wake_in_flight
    service.stop()


@pytest.mark.asyncio
async def test_banner_cannot_silently_patch_a_structured_monitor(tmp_path):
    # ``banner`` is a message-loop-only field: a structured monitor shows its
    # objective as the transcript row, so it has no banner to set. monitor_update
    # must REFUSE a banner on the structured path -- the mirror of the legacy
    # path refusing structured-only fields -- rather than accept it into the
    # patch, silently drop it, and still report success.
    service = AutoNudgeService(base_dir=tmp_path)
    await service.add_monitor(
        slot_key="chat-1",
        kind="github_pull_request",
        target="https://github.com/acme/widgets/pull/7",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(),
    )
    state = SimpleNamespace(
        _slots={
            "chat-1": SimpleNamespace(
                workspace="default", mode="", memory_mode="persistent", is_closing=False
            )
        },
        sessions=None,
        channel_transports={},
    )
    slot = SimpleNamespace(key="chat-1", _app="")
    with (
        patch("kiro_crew.autonudge.get_instance", return_value=service),
        patch("kiro_crew.autonudge_authz.sel", return_value=MagicMock()),
    ):
        result = await apply_session_directive(
            state,
            slot,
            "dashboard:chat-1",
            "monitor_update",
            {"patch": {"banner": "short transcript row"}},
        )

    assert result.startswith("monitor_update cannot apply")
    assert "banner" in result
    service.stop()


@pytest.mark.asyncio
async def test_denied_monitor_update_is_surfaced_into_the_session(tmp_path):
    # A denied REVISION is as unobservable as a denied arm: the MCP tool has
    # already answered "update requested" over its own pipe, so a denial that
    # stays in the gateway log leaves the agent reporting a revision that never
    # landed. The consumer must put a row where the
    # session's reader can see it, worded for a revision -- the loop kept its
    # PREVIOUS instruction, which is not the same fact as "nothing is running".
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add("chat-1", "watch the build", idle_secs=60)
    # Pause it the way an unanswered approval does, so monitor_update's
    # paused-loop protection denies the patch at apply time.
    await service.update(loop.id, active=False, stopped_reason=APPROVAL_STALL_REASON)
    surfaced = MagicMock()
    state = SimpleNamespace(
        _slots={
            "chat-1": SimpleNamespace(
                workspace="default", mode="", memory_mode="persistent", is_closing=False
            )
        },
        sessions=None,
        channel_transports={},
    )
    slot = SimpleNamespace(key="chat-1", _app="", messages=[])
    with (
        patch("kiro_crew.autonudge.get_instance", return_value=service),
        patch("kiro_crew.dashboard.state.append_and_surface", surfaced),
    ):
        result = await apply_session_directive(
            state,
            slot,
            "dashboard:chat-1",
            "monitor_update",
            {"patch": {"message": "revised instruction"}},
        )

    assert "is PAUSED" in result
    surfaced.assert_called_once()
    called_state, called_slot, role, text, cls = surfaced.call_args.args
    assert called_state is state and called_slot is slot
    assert role == "notice" and cls == "msg msg-info"
    assert text.startswith(sda.REVISION_REFUSAL_NOTICE_PREFIX)
    # The revision wording, not the arming wording: a paused loop that kept its
    # old instruction is not a session with no automation at all.
    assert not text.startswith(sda.ARM_REFUSAL_NOTICE_PREFIX)
    assert "kept its previous instruction" in text
    assert "approval prompt" in text
    service.stop()


@pytest.mark.asyncio
async def test_denied_arm_in_a_crewmate_session_is_queued_for_the_agent(tmp_path):
    # A crewmate's DM draws messages only (crewmateBubbles.isCrewmateChatRow
    # drops every notice) and never replays a notice row to the model, so the
    # notice row written for a refused arm reaches NEITHER the person nor the
    # crewmate. There the refusal is ALSO queued for the crewmate's next turn
    # with a system-injection kind, so it drains as an inject row (never a user
    # bubble) and the crewmate can stop and re-arm the loop itself.
    from kiro_crew.dashboard import session_directive_apply as _sda
    from kiro_crew.dashboard.chat_turn.steer_queue import _QUEUE_KIND_ACTORS
    from kiro_crew.dashboard.chat_utils import (
        AUTOMATION_NOTICE_KIND,
        is_system_injection_item,
    )

    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add("member-helper", "watch the build", idle_secs=60)
    await service.update(loop.id, active=False, stopped_reason=APPROVAL_STALL_REASON)
    surfaced = MagicMock()
    queued: list[dict] = []
    state = SimpleNamespace(
        _slots={
            "member-helper": SimpleNamespace(
                workspace="default", mode="", memory_mode="persistent", is_closing=False
            )
        },
        sessions=None,
        channel_transports={},
    )
    slot = SimpleNamespace(
        key="member-helper",
        _app="",
        messages=[],
        queue_append=lambda content, *a, **k: queued.append({"content": content, **k}) or "qid-1",
    )
    with (
        patch("kiro_crew.autonudge.get_instance", return_value=service),
        patch("kiro_crew.dashboard.state.append_and_surface", surfaced),
    ):
        result = await apply_session_directive(
            state,
            slot,
            "dashboard:member-helper",
            "monitor_update",
            {"patch": {"message": "revised instruction"}},
        )

    assert "is PAUSED" in result
    # The person-facing notice row is still written, tagged so the crewmate
    # chat's allowlist can hide it (and only it, not person-actionable notices).
    surfaced.assert_called_once()
    _, _, role, notice_text, _ = surfaced.call_args.args
    assert role == "notice"
    assert surfaced.call_args.kwargs["meta"] == {"kind": _sda.ARM_REFUSAL_NOTICE_KIND}
    # AND the refusal reaches the crewmate's next turn carrying the same text,
    # stamped AUTOMATION_NOTICE_KIND so the drain treats it as a system
    # injection (inject row, not a user follow-up) and never a person's bubble.
    assert len(queued) == 1
    assert queued[0]["content"].startswith("[Automation notice] ")
    assert notice_text in queued[0]["content"]
    assert queued[0]["kind"] == AUTOMATION_NOTICE_KIND
    assert is_system_injection_item(
        {"kind": AUTOMATION_NOTICE_KIND, "content": queued[0]["content"]}
    )
    assert _QUEUE_KIND_ACTORS[AUTOMATION_NOTICE_KIND] == "autonudge"
    # A system-injection kind is orchestration (the user-follow-up guard is
    # synthetic_payload OR system_injection), so the refusal is never mistaken
    # for the person stepping in and never aborts a pending recovery.
    service.stop()


@pytest.mark.asyncio
async def test_denied_arm_in_a_plain_session_is_notice_only(tmp_path):
    # Every non-crewmate session keeps the notice row as its only surface: the
    # notice draws for the person and nothing is queued to the agent.
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add("chat-1", "watch the build", idle_secs=60)
    await service.update(loop.id, active=False, stopped_reason=APPROVAL_STALL_REASON)
    surfaced = MagicMock()
    queued: list[str] = []
    state = SimpleNamespace(
        _slots={
            "chat-1": SimpleNamespace(
                workspace="default", mode="", memory_mode="persistent", is_closing=False
            )
        },
        sessions=None,
        channel_transports={},
    )
    slot = SimpleNamespace(
        key="chat-1",
        _app="",
        messages=[],
        queue_append=lambda content, *a, **k: queued.append(content) or "qid-1",
    )
    with (
        patch("kiro_crew.autonudge.get_instance", return_value=service),
        patch("kiro_crew.dashboard.state.append_and_surface", surfaced),
    ):
        result = await apply_session_directive(
            state,
            slot,
            "dashboard:chat-1",
            "monitor_update",
            {"patch": {"message": "revised instruction"}},
        )

    assert "is PAUSED" in result
    surfaced.assert_called_once()
    assert queued == []
    service.stop()


@pytest.mark.asyncio
async def test_denied_monitor_update_names_the_consecutive_failure_bound(tmp_path):
    """A loop stopped on CONSECUTIVE_FAILURE_REASON names its own remedy.

    Without a branch the bound falls to the generic "paused manually; ask the
    user", which sends the operator to a human who made no decision. The real
    cause is the loop's own cycles dying, so the remedy is to look at the error
    and re-arm.
    """
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add("chat-1", "watch the build", idle_secs=60)
    await service.update(loop.id, active=False, stopped_reason=CONSECUTIVE_FAILURE_REASON)
    surfaced = MagicMock()
    state = SimpleNamespace(
        _slots={
            "chat-1": SimpleNamespace(
                workspace="default", mode="", memory_mode="persistent", is_closing=False
            )
        },
        sessions=None,
        channel_transports={},
    )
    slot = SimpleNamespace(key="chat-1", _app="", messages=[])
    with (
        patch("kiro_crew.autonudge.get_instance", return_value=service),
        patch("kiro_crew.dashboard.state.append_and_surface", surfaced),
    ):
        result = await apply_session_directive(
            state,
            slot,
            "dashboard:chat-1",
            "monitor_update",
            {"patch": {"message": "revised instruction"}},
        )

    assert "is PAUSED" in result
    surfaced.assert_called_once()
    _, _, _, text, _ = surfaced.call_args.args
    assert "paused manually" not in text
    assert "reached a model session and then died" in text
    assert "re-arm it with monitor_start" in text
    service.stop()


@pytest.mark.asyncio
async def test_monitor_update_names_a_stop_file_finish_and_revives_nothing(tmp_path):
    """A loop its stop file finished is not paused: raising a bound buys nothing
    and the service refuses the revival, so the denial has to say the goal is
    finished and point at a NEW goal, not at a bound or at the user."""
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add("chat-1", "finish the report", idle_secs=60, max_cycles=5)
    await service.update(loop.id, active=False, stopped_reason=STOP_SENTINEL_REASON)
    state = SimpleNamespace(
        _slots={
            "chat-1": SimpleNamespace(
                workspace="default", mode="", memory_mode="persistent", is_closing=False
            )
        },
        sessions=None,
        channel_transports={},
    )
    slot = SimpleNamespace(key="chat-1", _app="", messages=[])
    with (
        patch("kiro_crew.autonudge.get_instance", return_value=service),
        patch("kiro_crew.dashboard.state.append_and_surface", MagicMock()),
    ):
        result = await apply_session_directive(
            state,
            slot,
            "dashboard:chat-1",
            "monitor_update",
            {"patch": {"max_cycles": 50}},
        )

    assert "finished" in result and "NEW goal" in result
    assert "paused manually" not in result
    kept = service.get_by_slot("chat-1")
    assert kept is not None and kept.active is False
    assert kept.stopped_reason == STOP_SENTINEL_REASON
    assert kept.max_cycles == 5, "a refused revival applies nothing"
    service.stop()


# ── monitor_update: the judge clear that survives a client dropping ``{}`` ──


_OWN_BRIEF = {"wake_when": "a worker line says RULING", "quiet_when": "workers say WORKING"}


async def _monitor_update_through_the_tool(monkeypatch, service, judge_arg):
    """``monitor_update`` from the tool's own entry, then the consumer's apply.

    The tool validates and encodes the patch; the consumer applies whatever the tool
    produced, which is the path a live turn takes whether the directive travels in
    the tool result or out of band. Returns ``(tool reply, decoded payload, result)``.
    """
    from kiro_crew import mcp_core, session_directive

    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "dashboard:chat-1")
    # The tool's retained-stop preflight reads the gateway; none runs in this test,
    # and the preflight is fail-open by design.
    monkeypatch.setattr(mcp_core, "_get", lambda *a, **k: {"error": "no gateway here"})
    reply = await asyncio.wait_for(
        asyncio.to_thread(mcp_core._call_tool, "monitor_update", {"judge": judge_arg}),
        timeout=30,
    )
    payload = session_directive.decode(reply, "monitor_update")
    if payload is None:
        return reply, None, ""
    with patch("kiro_crew.autonudge.get_instance", return_value=service):
        result = await apply_session_directive(
            SimpleNamespace(),
            SimpleNamespace(key="chat-1", _app=""),
            "dashboard:chat-1",
            "monitor_update",
            payload,
        )
    return reply, payload, result


@pytest.mark.parametrize(
    "spelling",
    [{}, {"wake_when": "", "quiet_when": ""}],
    ids=["empty_object", "blank_criteria"],
)
@pytest.mark.asyncio
async def test_blank_criteria_drop_the_owners_brief_as_an_empty_object_does(
    tmp_path, monkeypatch, spelling
):
    """``{"wake_when": "", "quiet_when": ""}`` is the documented way to drop a brief.

    A client that drops empty objects never delivers ``judge: {}``, so the tool
    description names the blank-criteria spelling instead. The two must leave the
    loop in the same state: no criterion of the owner's own (the reading the wake
    tick keys the default brief on), the judge's read state reset, and ``{}`` once
    the store is reloaded.
    """
    from kiro_crew import autonudge_judge

    service = AutoNudgeService(base_dir=tmp_path)
    try:
        armed = await service.add("chat-1", "watch chat-2", idle_secs=60, judge=_OWN_BRIEF)
        armed.judge_quiet_streak = 3
        armed.judge_cursors = {"chat-2": 5}

        _reply, payload, result = await _monitor_update_through_the_tool(
            monkeypatch, service, spelling
        )

        assert payload == {"patch": {"judge": spelling}}
        assert "updated on this session (judge)" in result, result
        loop = service.get_by_slot("chat-1")
        assert loop is not None
        assert autonudge_judge.criteria_of(autonudge_judge.spec_of(loop)) == ("", "")
        assert loop.judge_quiet_streak == 0 and loop.judge_cursors == {}
    finally:
        service.stop()
    # The durable record, read back the way startup reads it (without arming timers).
    reloaded = await AutoNudgeService.load_for_maintenance(base_dir=tmp_path)
    stored = reloaded.get_by_slot("chat-1")
    assert stored is not None and stored.judge == {}


@pytest.mark.asyncio
async def test_a_judge_with_criteria_still_replaces_the_brief(tmp_path, monkeypatch):
    service = AutoNudgeService(base_dir=tmp_path)
    try:
        await service.add("chat-1", "watch chat-2", idle_secs=60, judge=_OWN_BRIEF)

        _reply, _payload, result = await _monitor_update_through_the_tool(
            monkeypatch, service, {"wake_when": "a worker says BLOCKED", "quiet_when": ""}
        )

        assert "updated on this session (judge)" in result, result
        loop = service.get_by_slot("chat-1")
        assert loop is not None
        assert loop.judge.get("wake_when") == "a worker says BLOCKED"
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_blank_brief_with_an_unknown_key_is_refused_and_changes_nothing(
    tmp_path, monkeypatch
):
    service = AutoNudgeService(base_dir=tmp_path)
    try:
        await service.add("chat-1", "watch chat-2", idle_secs=60, judge=_OWN_BRIEF)

        reply, payload, _result = await _monitor_update_through_the_tool(
            monkeypatch, service, {"wake_when": "", "quiet_when": "", "wake": ""}
        )

        assert payload is None, "a refused brief emitted a directive"
        assert reply.startswith("monitor_update:") and "wake" in reply, reply
        loop = service.get_by_slot("chat-1")
        assert loop is not None and loop.judge == _OWN_BRIEF
    finally:
        service.stop()
