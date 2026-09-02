from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.autonudge import AutoNudgeService
from kiro_crew.autonudge_authz import authorize_and_update_monitor
from kiro_crew.dashboard.session_directive_apply import apply_session_directive
from kiro_crew.monitoring.models import (
    MonitorBudgets,
    MonitorDispatchResult,
    MonitorOutcome,
)


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
async def test_webex_structured_stop_is_refused_by_authoritative_consumer(tmp_path):
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
            "monitor_stop",
            {},
        )

    assert "not supported" in result
    audit.assert_called_once_with(session_key, "monitor_stop", "denied")
    service.stop()


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
        _slots={"chat-1": SimpleNamespace(workspace="default")},
        sessions=None,
        channel_transports={},
    )
    slot = SimpleNamespace(key="chat-1", _app="")
    audit = MagicMock()
    with (
        patch("kiro_crew.autonudge.get_instance", return_value=service),
        patch("kiro_crew.autonudge_authz.sel", return_value=audit),
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
        await apply_session_directive(
            state, slot, "dashboard:chat-1", "autonudge_stop", {"reason": "legacy caller"}
        )
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


@pytest.mark.asyncio
async def test_monitor_watch_does_not_replace_a_legacy_loop(tmp_path):
    service = AutoNudgeService(base_dir=tmp_path)
    legacy = await service.add("chat-1", "legacy prompt", idle_secs=60)
    slot = SimpleNamespace(key="chat-1", workspace="default", _app="")
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
    slot = SimpleNamespace(key="chat-1", workspace="default", _app="")
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
        _slots={"chat-1": SimpleNamespace(workspace="default", mode="", memory_mode="persistent")},
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
