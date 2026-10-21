"""Tests for what a monitor's own wake may do through ``apply_session_directive``.

Among them: a late wake cannot re-arm a loop a person stopped, a stale wake cannot revise or stop
the loop that replaced its own, a wake cannot remove or revive a loop a person paused, and a
legacy loop its own cycle cap or runtime budget deactivated is revived only by a raise of that
bound. A stop's write racing another change to the loop is tested in
``test_monitor_directive_apply_stop_races.py``; routing, which surfaces may act on a loop, and
the revision refusal notices in ``test_monitor_directive_apply.py``.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.autonudge import AutoNudgeService
from kiro_crew.dashboard import session_directive_apply as sda
from kiro_crew.dashboard.session_directive_apply import apply_session_directive
from kiro_crew.monitoring.models import MonitorBudgets, MonitorOutcome


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
        # The raise buys its increment: the count is kept, so 1 -> 3 is two more
        # cycles, not a fresh three (the reset is the user's resume alone).
        assert loop.cycle_count == 1
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
    # flake-ok: ages the loop on the clock the budget check reads; 120 s is 58 min under 3600 s
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
