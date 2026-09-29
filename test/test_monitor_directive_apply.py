from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.autonudge import APPROVAL_STALL_REASON, AutoNudgeService
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
@pytest.mark.parametrize("kind", ["monitor_start", "monitor_watch"])
async def test_late_monitor_wake_cannot_rearm_after_user_stop(tmp_path, kind):
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add(slot_key="chat-1", message="check", idle_secs=86400)
    await service.remove(loop.id)  # ordinary prompt-loop Stop removes its row
    state = SimpleNamespace(
        _slots={"chat-1": SimpleNamespace(workspace="default", is_closing=False)},
        sessions=None,
        channel_transports={},
    )
    args = (
        {"message": "check", "idle_secs": 86400, "max_runtime_secs": 600, "gate": False}
        if kind == "monitor_start"
        else {
            "kind": "github_pull_request",
            "target": "https://github.com/a/b/pull/1",
            "objective": "review_ready",
            "max_runtime_secs": 600,
            "max_agent_turns": 4,
            "max_tokens": 10000,
            "max_provider_errors": 2,
        }
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
                kind,
                args,
                producer_is_self_wake=True,
            )
        assert service.get_by_slot("chat-1") is None
        assert "NOT armed" in result
    finally:
        service.stop()


def _wake_arm_args(kind: str) -> dict:
    if kind == "monitor_start":
        return {"message": "check", "idle_secs": 86400, "max_runtime_secs": 600, "gate": False}
    return {
        "kind": "github_pull_request",
        "target": "https://github.com/a/b/pull/1",
        "objective": "review_ready",
        "max_runtime_secs": 600,
        "max_agent_turns": 4,
        "max_tokens": 10000,
        "max_provider_errors": 2,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["monitor_start", "monitor_watch"])
async def test_late_wake_of_a_user_stopped_structured_monitor_cannot_rearm(tmp_path, kind):
    """A structured stop retains the row as ``USER_STOP``; the wake it
    interrupted, which carries that loop's id, is refused before the
    authorizer runs, and the retained record is left in place."""
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add_monitor(
        slot_key="chat-1",
        kind="github_pull_request",
        target="https://github.com/a/b/pull/1",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(),
    )
    await service.stop_monitor(loop.id, user_reason="done")
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
                kind,
                _wake_arm_args(kind),
                producer_is_self_wake=True,
                producer_wake_loop_id=loop.id,
            )
        retained = service.get_by_slot("chat-1")
        assert retained is not None and retained.id == loop.id and not retained.active
        assert retained.monitor.outcome is MonitorOutcome.USER_STOP
        assert "NOT armed" in result and "stopped by a person" in result
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_final_cycle_wake_may_arm_the_successor_of_its_capped_loop(tmp_path):
    """The narrow rule: a loop its OWN cycle cap deactivated is not a user Stop,
    so the wake that carries its id may start the successor loop, and the
    ``replace_stopped`` rule displaces the spent row."""
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add(slot_key="chat-1", message="check", max_cycles=1)
    loop.cycle_count = 1
    await service.update(loop.id, active=False, stopped_reason="cycle_cap")
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
                "monitor_start",
                _wake_arm_args("monitor_start"),
                producer_is_self_wake=True,
                producer_wake_loop_id=loop.id,
            )
        successor = service.get_by_slot("chat-1")
        assert successor is not None and successor.active and successor.id != loop.id
        assert "started on this session" in result
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_self_wake_revives_cap_expired_loop_by_raising_cap(tmp_path):
    """A loop its OWN cycle cap deactivated is not a user Stop, so the
    final-cycle wake may revive it by raising the cap (unattended patrol)."""
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add(slot_key="chat-1", message="check", max_cycles=1)
    loop.cycle_count = 1
    await service.update(loop.id, active=False, stopped_reason="cycle_cap")
    state = SimpleNamespace(
        _slots={"chat-1": SimpleNamespace(workspace="default", is_closing=False)}
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
                {"patch": {"max_cycles": 3}},
                producer_is_self_wake=True,
            )
        assert loop.active and loop.stopped_reason == ""
        assert loop.max_cycles == 3
        assert "new user request" not in result
    finally:
        service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["monitor_update", "monitor_stop", "autonudge_stop"])
async def test_stale_wake_cannot_mutate_replacement_loop(tmp_path, kind):
    service = AutoNudgeService(base_dir=tmp_path)
    loop_a = await service.add(slot_key="chat-1", message="old", idle_secs=86400)
    await service.remove(loop_a.id)
    loop_b = await service.add(slot_key="chat-1", message="replacement", idle_secs=86400)
    args = (
        {"patch": {"message": "stale rewrite"}} if kind == "monitor_update" else {"reason": "done"}
    )
    try:
        with (
            patch("kiro_crew.autonudge.get_instance", return_value=service),
            patch("kiro_crew.autonudge_authz.sel", return_value=MagicMock()),
        ):
            result = await apply_session_directive(
                SimpleNamespace(),
                SimpleNamespace(key="chat-1", _app=""),
                "dashboard:chat-1",
                kind,
                args,
                producer_is_self_wake=True,
                producer_wake_loop_id=loop_a.id,
            )
        current = service.get_by_slot("chat-1")
        assert current is not None and current.id == loop_b.id and current.active
        assert current.message == "replacement"
        assert "new user request is required" in result
    finally:
        service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["monitor_stop", "autonudge_stop"])
async def test_stale_wake_stop_refusal_uses_stop_wording_and_is_surfaced(tmp_path, kind):
    """A stop refused because the wake's loop is not this session's monitor any more
    says so in stop terms (nothing was armed or revised here) and lands a
    transcript row, the same way a refused arm or revision does."""
    service = AutoNudgeService(base_dir=tmp_path)
    loop_a = await service.add(slot_key="chat-1", message="old", idle_secs=86400)
    await service.remove(loop_a.id)
    loop_b = await service.add(slot_key="chat-1", message="replacement", idle_secs=86400)
    surfaced = MagicMock()
    slot = SimpleNamespace(key="chat-1", _app="", messages=[])
    state = SimpleNamespace()
    try:
        with (
            patch("kiro_crew.autonudge.get_instance", return_value=service),
            patch("kiro_crew.autonudge_authz.sel", return_value=MagicMock()),
            patch("kiro_crew.dashboard.state.append_and_surface", surfaced),
        ):
            result = await apply_session_directive(
                state,
                slot,
                "dashboard:chat-1",
                kind,
                {"reason": "done"},
                producer_is_self_wake=True,
                producer_wake_loop_id=loop_a.id,
            )
        current = service.get_by_slot("chat-1")
        assert current is not None and current.id == loop_b.id and current.active
        assert result.startswith("Monitor NOT stopped:")
        assert "NOT armed" not in result and "NOT changed" not in result
        assert "new user request is required" in result
        surfaced.assert_called_once()
        called_state, called_slot, role, text, cls = surfaced.call_args.args
        assert called_state is state and called_slot is slot
        assert role == "notice" and cls == "msg msg-info"
        assert text.startswith(sda.STOP_REFUSAL_NOTICE_PREFIX)
        assert not text.startswith(sda.ARM_REFUSAL_NOTICE_PREFIX)
        assert not text.startswith(sda.REVISION_REFUSAL_NOTICE_PREFIX)
    finally:
        service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["monitor_stop", "autonudge_stop"])
@pytest.mark.parametrize("stopped_reason", ["manual", ""])
async def test_stale_wake_cannot_remove_person_paused_loop(tmp_path, kind, stopped_reason):
    """A person pauses the loop while its delivered wake is still running; the
    wake's stop passes the identity check but must not delete the retained row.
    The row survives untouched and the refusal is surfaced in stop wording."""
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add(slot_key="chat-1", message="old", idle_secs=86400)
    await service.update(loop.id, active=False, stopped_reason=stopped_reason)
    paused_reason = service.get_by_id(loop.id).stopped_reason
    assert paused_reason in ("manual", "")
    surfaced = MagicMock()
    slot = SimpleNamespace(key="chat-1", _app="", messages=[])
    try:
        with (
            patch("kiro_crew.autonudge.get_instance", return_value=service),
            patch("kiro_crew.autonudge_authz.sel", return_value=MagicMock()),
            patch("kiro_crew.dashboard.state.append_and_surface", surfaced),
        ):
            result = await apply_session_directive(
                SimpleNamespace(),
                slot,
                "dashboard:chat-1",
                kind,
                {"reason": "done"},
                producer_is_self_wake=True,
                producer_wake_loop_id=loop.id,
            )
        current = service.get_by_slot("chat-1")
        assert current is not None and current.id == loop.id
        assert current.active is False and current.stopped_reason == paused_reason
        assert current.message == "old"
        assert result.startswith("Monitor NOT stopped:")
        assert "new user request is required" in result
        surfaced.assert_called_once()
        text = surfaced.call_args.args[3]
        assert text.startswith(sda.STOP_REFUSAL_NOTICE_PREFIX)
    finally:
        service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["monitor_stop", "autonudge_stop"])
async def test_live_wake_can_stop_its_own_active_loop(tmp_path, kind):
    """A wake whose loop is still this session's ACTIVE monitor may stop it."""
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add(slot_key="chat-1", message="old", idle_secs=86400)
    try:
        with (
            patch("kiro_crew.autonudge.get_instance", return_value=service),
            patch("kiro_crew.autonudge_authz.sel", return_value=MagicMock()),
        ):
            result = await apply_session_directive(
                SimpleNamespace(),
                SimpleNamespace(key="chat-1", _app=""),
                "dashboard:chat-1",
                kind,
                {"reason": "done"},
                producer_is_self_wake=True,
                producer_wake_loop_id=loop.id,
            )
        assert service.get_by_slot("chat-1") is None
        assert "stopped" in result
        assert "NOT stopped" not in result
    finally:
        service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["monitor_stop", "autonudge_stop"])
async def test_live_wake_can_stop_its_own_system_deactivated_loop(tmp_path, kind):
    """A row the system deactivated (cycle cap) is not retained evidence, so the
    wake's stop still goes through, as the arm gate would admit a re-arm."""
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add(slot_key="chat-1", message="old", idle_secs=86400)
    await service.update(loop.id, active=False, stopped_reason="cycle_cap")
    try:
        with (
            patch("kiro_crew.autonudge.get_instance", return_value=service),
            patch("kiro_crew.autonudge_authz.sel", return_value=MagicMock()),
        ):
            result = await apply_session_directive(
                SimpleNamespace(),
                SimpleNamespace(key="chat-1", _app=""),
                "dashboard:chat-1",
                kind,
                {"reason": "done"},
                producer_is_self_wake=True,
                producer_wake_loop_id=loop.id,
            )
        assert service.get_by_slot("chat-1") is None
        assert "stopped" in result and "NOT stopped" not in result
    finally:
        service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["monitor_update", "monitor_stop", "autonudge_stop"])
async def test_live_wake_can_mutate_its_own_loop(tmp_path, kind):
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add(slot_key="chat-1", message="old", idle_secs=86400)
    args = {"patch": {"message": "revised"}} if kind == "monitor_update" else {"reason": "done"}
    try:
        with (
            patch("kiro_crew.autonudge.get_instance", return_value=service),
            patch("kiro_crew.autonudge_authz.sel", return_value=MagicMock()),
        ):
            result = await apply_session_directive(
                SimpleNamespace(),
                SimpleNamespace(key="chat-1", _app=""),
                "dashboard:chat-1",
                kind,
                args,
                producer_is_self_wake=True,
                producer_wake_loop_id=loop.id,
            )
        if kind == "monitor_update":
            current = service.get_by_slot("chat-1")
            assert current is not None and current.id == loop.id
            assert current.message == "revised"
            assert "updated" in result
        else:
            assert service.get_by_slot("chat-1") is None
            assert "stopped" in result
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_self_wake_without_loop_id_keeps_binding_fallback(tmp_path):
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add(slot_key="chat-1", message="old", idle_secs=86400)
    try:
        with (
            patch("kiro_crew.autonudge.get_instance", return_value=service),
            patch("kiro_crew.autonudge_authz.sel", return_value=MagicMock()),
        ):
            result = await apply_session_directive(
                SimpleNamespace(),
                SimpleNamespace(key="chat-1", _app=""),
                "dashboard:chat-1",
                "monitor_update",
                {"patch": {"message": "fallback rewrite"}},
                producer_is_self_wake=True,
            )
        current = service.get_by_slot("chat-1")
        assert current is not None and current.id == loop.id
        assert current.message == "fallback rewrite"
        assert "updated" in result
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_self_wake_revives_budget_expired_loop_by_raising_budget(tmp_path):
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add(slot_key="chat-1", message="check", max_runtime_secs=60)
    loop.created_ts = time.time() - 120
    await service.update(loop.id, active=False, stopped_reason="runtime_budget")
    state = SimpleNamespace(
        _slots={"chat-1": SimpleNamespace(workspace="default", is_closing=False)}
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
                {"patch": {"max_runtime_secs": 3600}},
                producer_is_self_wake=True,
            )
        assert loop.active and loop.stopped_reason == ""
        assert "new user request" not in result
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_self_wake_cannot_revive_cap_expired_loop_without_raising_cap(tmp_path):
    """The bound-revival rule still governs: a wake whose patch does not raise
    the stopping bound is refused like any other patch."""
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add(slot_key="chat-1", message="check", max_cycles=1)
    loop.cycle_count = 1
    await service.update(loop.id, active=False, stopped_reason="cycle_cap")
    state = SimpleNamespace(
        _slots={"chat-1": SimpleNamespace(workspace="default", is_closing=False)}
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
                {"patch": {"message": "still check"}},
                producer_is_self_wake=True,
            )
        assert not loop.active and loop.stopped_reason == "cycle_cap"
        # Refused by the existing capped-loop guard (the unraised cap is at the
        # delivered count), not by the self-wake gate: revival did not happen.
        assert "would deactivate without firing again" in result
    finally:
        service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stopped_reason",
    [
        None,  # REST pause / deactivate-mid-fire: the store records "manual"
        "",  # pre-field pause or torn write: empty is evidence, fails closed
    ],
)
async def test_self_wake_cannot_revive_user_paused_loop_by_raising_cap(tmp_path, stopped_reason):
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add(slot_key="chat-1", message="check", max_cycles=1)
    loop.cycle_count = 1
    await service.update(loop.id, active=False)
    if stopped_reason is not None:
        loop.stopped_reason = stopped_reason
    expected_reason = loop.stopped_reason
    state = SimpleNamespace(
        _slots={"chat-1": SimpleNamespace(workspace="default", is_closing=False)}
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
                {"patch": {"max_cycles": 3}},
                producer_is_self_wake=True,
            )
        assert not loop.active and loop.stopped_reason == expected_reason
        assert "new user request" in result
    finally:
        service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["monitor_stop", "autonudge_stop"])
async def test_stale_wake_stop_preserves_pause_landing_before_remove(tmp_path, kind, monkeypatch):
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add(
        slot_key="chat-1",
        message="keep this paused loop",
        idle_secs=86400,
        max_cycles=9,
        max_runtime_secs=7200,
    )
    real_acquire = service._acquire_mutation_lock
    stop_waiting = False

    async def _pause_before_stop_acquires(loop_id):
        nonlocal stop_waiting
        if not stop_waiting:
            stop_waiting = True
            await service.update(loop_id, active=False)
        return await real_acquire(loop_id)

    monkeypatch.setattr(service, "_acquire_mutation_lock", _pause_before_stop_acquires)
    try:
        with (
            patch("kiro_crew.autonudge.get_instance", return_value=service),
            patch("kiro_crew.autonudge_authz.sel", return_value=MagicMock()),
        ):
            result = await apply_session_directive(
                SimpleNamespace(),
                SimpleNamespace(key="chat-1", _app=""),
                "dashboard:chat-1",
                kind,
                {"reason": "wake finished"},
                producer_is_self_wake=True,
                producer_wake_loop_id=loop.id,
            )

        paused = service.get_by_slot("chat-1")
        assert paused is not None and paused.id == loop.id
        assert not paused.active and paused.stopped_reason == "manual"
        assert paused.message == "keep this paused loop"
        assert paused.max_cycles == 9 and paused.max_runtime_secs == 7200
        assert "new user request is required" in result
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_remove_unserialized_requires_mutation_lock(tmp_path):
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add(slot_key="chat-1", message="keep", idle_secs=86400)
    try:
        with pytest.raises(RuntimeError, match="mutation lock"):
            await service._remove_unserialized(loop.id)
        assert service.get_by_id(loop.id) is loop
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_remove_unserialized_requires_lock_owned_by_caller(tmp_path):
    from kiro_crew import autonudge as an

    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add(slot_key="chat-1", message="keep", idle_secs=86400)
    acquired = asyncio.Future()
    release = asyncio.Event()

    async def _hold_service_lock():
        lock = await service._acquire_mutation_lock(loop.id)
        assert lock is not None
        acquired.set_result(lock)
        try:
            await release.wait()
        finally:
            an._release_mutation_lock(lock)

    holder = asyncio.create_task(_hold_service_lock())
    try:
        lock = await acquired
        with pytest.raises(RuntimeError, match="mutation lock must be held by the caller"):
            await service._remove_unserialized(loop.id, mutation_lock=lock)
        assert service.get_by_id(loop.id) is loop
    finally:
        release.set()
        await holder
        service.stop()


@pytest.mark.asyncio
async def test_remove_unserialized_requires_service_maintenance_lock(tmp_path):
    from kiro_crew import autonudge as an

    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add(slot_key="chat-1", message="keep", idle_secs=86400)
    different_lock = asyncio.Lock()
    await different_lock.acquire()
    an._claim_mutation_lock(different_lock)
    try:
        with pytest.raises(
            RuntimeError, match="mutation lock must be the service maintenance lock"
        ):
            await service._remove_unserialized(loop.id, mutation_lock=different_lock)
        assert service.get_by_id(loop.id) is loop
    finally:
        an._release_mutation_lock(different_lock)
        service.stop()


@pytest.mark.asyncio
async def test_stale_wake_stop_preserves_pause_landing_before_research_update(
    tmp_path, monkeypatch
):
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add(
        slot_key="research-0123abcd",
        message="keep this paused campaign loop",
        idle_secs=86400,
        max_cycles=9,
        max_runtime_secs=7200,
    )
    real_acquire = service._acquire_mutation_lock
    stop_waiting = False

    async def _pause_before_stop_acquires(loop_id):
        nonlocal stop_waiting
        if not stop_waiting:
            stop_waiting = True
            await service.update(loop_id, active=False)
        return await real_acquire(loop_id)

    monkeypatch.setattr(service, "_acquire_mutation_lock", _pause_before_stop_acquires)
    try:
        with (
            patch("kiro_crew.autonudge.get_instance", return_value=service),
            patch("kiro_crew.autonudge_authz.sel", return_value=MagicMock()),
        ):
            result = await apply_session_directive(
                SimpleNamespace(),
                SimpleNamespace(key="research-0123abcd", _app="auto-research"),
                "dashboard:research-0123abcd",
                "autonudge_stop",
                {"reason": "wake finished"},
                producer_is_self_wake=True,
                producer_wake_loop_id=loop.id,
            )

        paused = service.get_by_slot("research-0123abcd")
        assert paused is not None and paused.id == loop.id
        assert not paused.active and paused.stopped_reason == "manual"
        assert paused.max_cycles == 9 and paused.max_runtime_secs == 7200
        assert "new user request is required" in result
    finally:
        service.stop()


def _directive_outcomes(audit: MagicMock, kind: str) -> list[str]:
    return [
        call.kwargs["outcome"]
        for call in audit.log_tool_invocation.call_args_list
        if call.kwargs.get("source") == "mcp-directive" and call.kwargs.get("tool_name") == kind
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["monitor_stop", "autonudge_stop"])
async def test_stale_wake_stop_of_row_deleted_before_remove_is_idempotent_success(
    tmp_path, kind, monkeypatch
):
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add(slot_key="chat-1", message="delete me first", idle_secs=86400)
    real_acquire = service._acquire_mutation_lock
    stop_waiting = False

    async def _delete_before_stop_acquires(loop_id):
        nonlocal stop_waiting
        if not stop_waiting:
            stop_waiting = True
            assert await service.remove(loop_id, stop_reason="dashboard_delete")
        return await real_acquire(loop_id)

    monkeypatch.setattr(service, "_acquire_mutation_lock", _delete_before_stop_acquires)
    audit = MagicMock()
    try:
        with (
            patch("kiro_crew.autonudge.get_instance", return_value=service),
            patch("kiro_crew.autonudge_authz.sel", return_value=audit),
            patch("kiro_crew.sel.sel", return_value=audit),
        ):
            result = await apply_session_directive(
                SimpleNamespace(),
                SimpleNamespace(key="chat-1", _app=""),
                "dashboard:chat-1",
                kind,
                {"reason": "wake finished"},
                producer_is_self_wake=True,
                producer_wake_loop_id=loop.id,
            )

        assert service.get_by_slot("chat-1") is None
        assert f"Auto-nudge loop {loop.id} stopped on this session" in result
        assert "new user request" not in result
        assert _directive_outcomes(audit, kind) == ["success"]
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_stale_wake_stop_of_row_deleted_before_research_update_is_idempotent_success(
    tmp_path, monkeypatch
):
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add(
        slot_key="research-0123abcd", message="delete me first", idle_secs=86400
    )
    real_acquire = service._acquire_mutation_lock
    stop_waiting = False

    async def _delete_before_stop_acquires(loop_id):
        nonlocal stop_waiting
        if not stop_waiting:
            stop_waiting = True
            assert await service.remove(loop_id, stop_reason="dashboard_delete")
        return await real_acquire(loop_id)

    monkeypatch.setattr(service, "_acquire_mutation_lock", _delete_before_stop_acquires)
    audit = MagicMock()
    try:
        with (
            patch("kiro_crew.autonudge.get_instance", return_value=service),
            patch("kiro_crew.autonudge_authz.sel", return_value=audit),
            patch("kiro_crew.sel.sel", return_value=audit),
        ):
            result = await apply_session_directive(
                SimpleNamespace(),
                SimpleNamespace(key="research-0123abcd", _app="auto-research"),
                "dashboard:research-0123abcd",
                "autonudge_stop",
                {"reason": "wake finished"},
                producer_is_self_wake=True,
                producer_wake_loop_id=loop.id,
            )

        assert service.get_by_slot("research-0123abcd") is None
        assert f"Auto-nudge loop {loop.id} stopped on this session" in result
        assert "new user request" not in result
        assert _directive_outcomes(audit, "autonudge_stop") == ["success"]
    finally:
        service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("slot_key", "app", "kind"),
    [
        ("chat-1", "", "monitor_stop"),
        ("chat-1", "", "autonudge_stop"),
        ("research-0123abcd", "auto-research", "autonudge_stop"),
    ],
)
async def test_stale_wake_stop_of_row_replaced_before_write_is_refused(
    tmp_path, slot_key, app, kind, monkeypatch
):
    # A concurrent arm REPLACED the wake's loop while the stop waited for the
    # lock. The wake's row is gone, but the goal does not hold: the replacement
    # is still running, so the stop must refuse and leave it untouched.
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add(slot_key=slot_key, message="replace me", idle_secs=86400)
    real_acquire = service._acquire_mutation_lock
    replacement = None
    stop_waiting = False

    async def _replace_before_stop_acquires(loop_id):
        nonlocal replacement, stop_waiting
        if not stop_waiting:
            stop_waiting = True
            assert await service.remove(loop_id, stop_reason="dashboard_delete")
            replacement = await service.add(
                slot_key=slot_key, message="the replacement", idle_secs=86400
            )
        return await real_acquire(loop_id)

    monkeypatch.setattr(service, "_acquire_mutation_lock", _replace_before_stop_acquires)
    audit = MagicMock()
    try:
        with (
            patch("kiro_crew.autonudge.get_instance", return_value=service),
            patch("kiro_crew.autonudge_authz.sel", return_value=audit),
            patch("kiro_crew.sel.sel", return_value=audit),
        ):
            result = await apply_session_directive(
                SimpleNamespace(),
                SimpleNamespace(key=slot_key, _app=app),
                f"dashboard:{slot_key}",
                kind,
                {"reason": "wake finished"},
                producer_is_self_wake=True,
                producer_wake_loop_id=loop.id,
            )

        live = service.get_by_slot(slot_key)
        assert replacement is not None and live is not None
        assert live.id == replacement.id and live.active
        assert "Monitor NOT stopped" in result and "replaced it" in result
        assert "No further nudges will fire" not in result
        assert _directive_outcomes(audit, kind) == ["denied"]
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_stale_wake_stop_that_never_took_the_lock_is_not_reported_stopped(
    tmp_path, monkeypatch
):
    # Maintenance holds the lock, so the write reaches neither the guard nor the
    # absent-row hook. The row is still live; claiming a stop would be false.
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add(slot_key="chat-1", message="still running", idle_secs=86400)

    async def _quiesced(_loop_id):
        return None

    monkeypatch.setattr(service, "_acquire_mutation_lock", _quiesced)
    audit = MagicMock()
    try:
        with (
            patch("kiro_crew.autonudge.get_instance", return_value=service),
            patch("kiro_crew.autonudge_authz.sel", return_value=audit),
            patch("kiro_crew.sel.sel", return_value=audit),
        ):
            result = await apply_session_directive(
                SimpleNamespace(),
                SimpleNamespace(key="chat-1", _app=""),
                "dashboard:chat-1",
                "monitor_stop",
                {"reason": "wake finished"},
                producer_is_self_wake=True,
                producer_wake_loop_id=loop.id,
            )

        live = service.get_by_slot("chat-1")
        assert live is not None and live.id == loop.id and live.active
        assert "Monitor NOT stopped" in result and "Retry the stop" in result
        assert _directive_outcomes(audit, "monitor_stop") == ["denied"]
    finally:
        service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("slot_key", "app", "kind"),
    [
        ("chat-1", "", "monitor_stop"),
        ("chat-1", "", "autonudge_stop"),
        ("research-0123abcd", "auto-research", "autonudge_stop"),
    ],
)
async def test_user_stop_that_never_took_the_lock_is_not_reported_stopped(
    tmp_path, slot_key, app, kind, monkeypatch
):
    # A person's own stop carries no wake id, so there is no guard. It still may
    # not report a stop when maintenance held the lock and nothing was written.
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add(slot_key=slot_key, message="still running", idle_secs=86400)

    async def _quiesced(_loop_id):
        return None

    monkeypatch.setattr(service, "_acquire_mutation_lock", _quiesced)
    audit = MagicMock()
    try:
        with (
            patch("kiro_crew.autonudge.get_instance", return_value=service),
            patch("kiro_crew.autonudge_authz.sel", return_value=audit),
            patch("kiro_crew.sel.sel", return_value=audit),
        ):
            result = await apply_session_directive(
                SimpleNamespace(),
                SimpleNamespace(key=slot_key, _app=app),
                f"dashboard:{slot_key}",
                kind,
                {"reason": "done"},
            )

        live = service.get_by_slot(slot_key)
        assert live is not None and live.id == loop.id and live.active
        assert "Monitor NOT stopped" in result and "Retry the stop" in result
        assert _directive_outcomes(audit, kind) == ["denied"]
    finally:
        service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("slot_key", "app", "kind"),
    [
        ("chat-1", "", "monitor_stop"),
        ("chat-1", "", "autonudge_stop"),
        ("research-0123abcd", "auto-research", "autonudge_stop"),
    ],
)
async def test_user_stop_of_row_replaced_before_write_is_refused(
    tmp_path, slot_key, app, kind, monkeypatch
):
    # A person's stop resolved the loop, then a concurrent arm replaced it
    # before the write took the lock. The replacement is still running, so the
    # stop may not say "No further nudges will fire".
    service = AutoNudgeService(base_dir=tmp_path)
    await service.add(slot_key=slot_key, message="replace me", idle_secs=86400)
    real_acquire = service._acquire_mutation_lock
    replacement = None
    stop_waiting = False

    async def _replace_before_stop_acquires(loop_id):
        nonlocal replacement, stop_waiting
        if not stop_waiting:
            stop_waiting = True
            assert await service.remove(loop_id, stop_reason="dashboard_delete")
            replacement = await service.add(
                slot_key=slot_key, message="the replacement", idle_secs=86400
            )
        return await real_acquire(loop_id)

    monkeypatch.setattr(service, "_acquire_mutation_lock", _replace_before_stop_acquires)
    audit = MagicMock()
    try:
        with (
            patch("kiro_crew.autonudge.get_instance", return_value=service),
            patch("kiro_crew.autonudge_authz.sel", return_value=audit),
            patch("kiro_crew.sel.sel", return_value=audit),
        ):
            result = await apply_session_directive(
                SimpleNamespace(),
                SimpleNamespace(key=slot_key, _app=app),
                f"dashboard:{slot_key}",
                kind,
                {"reason": "done"},
            )

        live = service.get_by_slot(slot_key)
        assert replacement is not None and live is not None
        assert live.id == replacement.id and live.active
        assert "Monitor NOT stopped" in result and "replaced by a new loop" in result
        assert "No further nudges will fire" not in result
        assert _directive_outcomes(audit, kind) == ["denied"]
    finally:
        service.stop()
