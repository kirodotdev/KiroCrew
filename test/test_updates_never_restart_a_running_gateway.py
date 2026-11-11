"""No unattended path updates or restarts a running gateway.

The maintainer's ruling (the decision entry "updates never restart a running gateway"):
auto update does not update while the gateway runs. It may build the new version
beside the running install (the managed venv), which is then "update ready,
restart to use it"; a git checkout and a policy update command replace the
running tree in place, so they only notify. A mandatory floor keeps its notice
and forces nothing.

Each guard here is red against the code that restarted or applied unattended.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_gateway_helpers import _make_orchestrator, _mock_dashboard_state
from update_check_install_aware_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _wheel_install,
)
from wheel_update_test_helpers import wire_wheel_apply

from kiro_crew import update_restart_ready
from kiro_crew.platform import update_capability
from kiro_crew.platform.governance import UpdatePins
from kiro_crew.platform.update_capability import (
    AUTO_EFFECT_INSTALL,
    AUTO_EFFECT_MANDATORY,
    AUTO_EFFECT_NOTIFY,
    AUTO_ROUTE_GIT,
    AUTO_ROUTE_PROVIDER,
    AUTO_ROUTE_WHEEL,
    AutoUpdateEffect,
)
from kiro_crew.platform.update_provider import CommandProvider, UpdateCheckResult

_GIT_UPDATE = {
    "update_available": True,
    "version_newer": True,
    "can_apply": True,
    "channel": "stable",
    "latest_version": "9.9.9",
}
_MANAGED_UPDATE = {
    "update_available": True,
    "can_apply": False,
    "managed_by": "kirocrew",
    "channel": "stable",
    "latest_version": "9.9.9",
    "remediation": {"kind": "command", "message": "m", "command": "curl -fsSL | sh"},
}


def _cfg(*, auto_update: bool = True) -> SimpleNamespace:
    return SimpleNamespace(auto_update=auto_update, dashboard=SimpleNamespace(update_nudge={}))


def _orchestrator():
    """A real orchestrator whose every apply and restart is a recording stand-in."""
    orch = _make_orchestrator()
    orch.dashboard_state = _mock_dashboard_state()
    orch.sessions = MagicMock()
    orch.sessions.pause_turn_admission_for_update = AsyncMock(return_value=True)
    orch.sessions.resume_turn_admission_after_update = AsyncMock(return_value=True)
    orch._in_flight_work_counts = MagicMock(return_value=(0, 0))
    orch._auto_apply_wheel_update = AsyncMock()
    return orch


def _run_legacy(effect: AutoUpdateEffect, *, info: dict, floor: bool = False):
    import kiro_crew.dashboard.handlers as handlers

    orch = _orchestrator()
    original = dict(handlers._update_info)
    try:
        handlers._update_info.clear()
        handlers._update_info.update(info)
        with (
            patch.object(handlers, "_do_update_check", new_callable=AsyncMock),
            patch("kiro_crew.config.KiroCrewConfig.load", return_value=_cfg()),
            patch("kiro_crew.platform.update_governance.update_required", return_value=floor),
            patch("kiro_crew.platform.update_governance.min_version", return_value="9.9.0"),
            patch("kiro_crew.platform.wheel_engine.running_from_managed_venv", return_value=True),
            patch("kiro_crew.platform.governance.active_update_pins", return_value=UpdatePins()),
            patch("kiro_crew.slack.gateway.auto_update_effect", return_value=effect),
        ):
            asyncio.run(orch._check_for_updates())
    finally:
        handlers._update_info.clear()
        handlers._update_info.update(original)
    return orch


def _assert_nothing_applied_or_restarted(orch) -> None:
    orch._auto_apply_wheel_update.assert_not_awaited()
    orch.sessions.pause_turn_admission_for_update.assert_not_awaited()


# -- git checkout: replaces the running tree in place, so it only notifies ----------------


def test_a_voluntary_git_update_is_only_announced() -> None:
    orch = _run_legacy(AutoUpdateEffect(AUTO_EFFECT_INSTALL, AUTO_ROUTE_GIT), info=_GIT_UPDATE)

    _assert_nothing_applied_or_restarted(orch)
    orch.dashboard_state.push_refresh.assert_called_with("update_available")


def test_a_mandatory_floor_on_a_git_checkout_keeps_its_notice_and_forces_nothing() -> None:
    orch = _run_legacy(
        AutoUpdateEffect(AUTO_EFFECT_MANDATORY, AUTO_ROUTE_GIT), info=_GIT_UPDATE, floor=True
    )

    _assert_nothing_applied_or_restarted(orch)
    orch.dashboard_state.push_refresh.assert_called_with("update_available")


# -- managed venv: built beside the running install, then staged --------------------------


def test_a_voluntary_managed_venv_update_is_staged_not_restarted() -> None:
    orch = _run_legacy(
        AutoUpdateEffect(AUTO_EFFECT_INSTALL, AUTO_ROUTE_WHEEL), info=_MANAGED_UPDATE
    )

    orch._auto_apply_wheel_update.assert_awaited_once_with("stable", "9.9.9")


def test_a_mandatory_floor_on_a_managed_venv_stages_without_a_grace_key() -> None:
    orch = _run_legacy(
        AutoUpdateEffect(AUTO_EFFECT_MANDATORY, AUTO_ROUTE_WHEEL),
        info=_MANAGED_UPDATE,
        floor=True,
    )

    orch._auto_apply_wheel_update.assert_awaited_once_with("stable", "9.9.9", mandatory=True)


# -- a policy update command: replaces the install in place, so it only notifies ----------


def _run_provider(*, floor: bool, available: bool = True):
    import kiro_crew.dashboard.handlers as handlers

    orch = _orchestrator()
    orch._auto_update_effect = AsyncMock(
        return_value=AutoUpdateEffect(
            AUTO_EFFECT_MANDATORY if floor else AUTO_EFFECT_INSTALL, AUTO_ROUTE_PROVIDER
        )
    )
    orch._publish_provider_update_state = MagicMock()
    provider = CommandProvider(check_command="check-cmd", apply_command="apply-cmd")
    original = dict(handlers._update_info)
    try:
        with (
            patch.object(
                CommandProvider,
                "check",
                AsyncMock(
                    return_value=UpdateCheckResult(available=available, remote_version="2.0.0")
                ),
            ),
            patch.object(CommandProvider, "apply", AsyncMock(return_value=True)) as apply,
            patch("kiro_crew.config.KiroCrewConfig.load", return_value=_cfg()),
            patch("kiro_crew.platform.update_governance.update_required", return_value=floor),
            patch("kiro_crew.platform.update_governance.min_version", return_value="9.0.0"),
        ):
            asyncio.run(orch._check_for_updates_via_provider(provider))
    finally:
        handlers._update_info.clear()
        handlers._update_info.update(original)
    return orch, apply


@pytest.mark.parametrize("floor", [False, True], ids=["voluntary", "mandatory-floor"])
def test_a_policy_update_command_is_never_run_unattended(floor) -> None:
    orch, apply = _run_provider(floor=floor)

    apply.assert_not_awaited()
    _assert_nothing_applied_or_restarted(orch)
    orch._publish_provider_update_state.assert_called_once()
    orch.dashboard_state.push_refresh.assert_called_with("update_available")


def test_a_floor_above_every_build_is_reported_and_applies_nothing() -> None:
    orch, apply = _run_provider(floor=True, available=False)

    apply.assert_not_awaited()
    _assert_nothing_applied_or_restarted(orch)


# -- the derivation the switch reads ------------------------------------------------------


@pytest.fixture
def _governance(monkeypatch):
    monkeypatch.setattr("kiro_crew.platform.update_governance.update_required", lambda _v: False)
    monkeypatch.setattr(
        "kiro_crew.platform.update_governance.unattended_apply_allowed", lambda: True
    )


def test_a_provider_route_only_notifies_but_keeps_its_route(_governance) -> None:
    effect = update_capability.auto_update_effect(
        provider=SimpleNamespace(can_apply=lambda: True), running_version="1.0.0"
    )

    assert effect == AutoUpdateEffect(AUTO_EFFECT_NOTIFY, AUTO_ROUTE_PROVIDER, effect.reason)
    assert effect.manual_only, "a person's Update still applies it"
    assert "in place" in effect.reason


def test_a_git_route_only_notifies_but_keeps_its_route(_governance, monkeypatch) -> None:
    monkeypatch.setattr(
        update_capability,
        "derive_capability",
        lambda **_kw: SimpleNamespace(managed_by=update_capability.MANAGED_BY_GIT),
    )
    monkeypatch.setattr(update_capability, "_git_route", lambda _root: (AUTO_ROUTE_GIT, ""))

    effect = update_capability.auto_update_effect(
        install_root="/repo", dist="source", provider=None, running_version="1.0.0"
    )

    assert (effect.effect, effect.route) == (AUTO_EFFECT_NOTIFY, AUTO_ROUTE_GIT)
    assert effect.manual_only


def test_the_managed_venv_is_the_one_route_that_installs(_governance, monkeypatch) -> None:
    monkeypatch.setattr(update_capability, "_source_checkout_root", lambda: None)
    monkeypatch.setattr(update_capability, "_wheel_route", lambda: (AUTO_ROUTE_WHEEL, ""))

    effect = update_capability.auto_update_effect(
        provider=None, dist="wheel", git_probes=False, running_version="1.0.0"
    )

    assert (effect.effect, effect.route) == (AUTO_EFFECT_INSTALL, AUTO_ROUTE_WHEEL)


def test_a_floor_keeps_the_mandatory_effect_on_every_route(monkeypatch) -> None:
    monkeypatch.setattr("kiro_crew.platform.update_governance.update_required", lambda _v: True)

    effect = update_capability.auto_update_effect(
        provider=SimpleNamespace(can_apply=lambda: True), running_version="1.0.0"
    )

    assert (effect.effect, effect.route) == (AUTO_EFFECT_MANDATORY, AUTO_ROUTE_PROVIDER)


# -- the staged tree ----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mandatory", [False, True], ids=["routine", "floor"])
async def test_a_promoted_tree_is_staged_and_the_gateway_is_not_restarted(
    monkeypatch, mandatory
) -> None:
    orch = _make_orchestrator()
    orch.dashboard_state = _mock_dashboard_state()
    wire_wheel_apply(monkeypatch)

    await orch._auto_apply_wheel_update("stable", "9.9.9", mandatory=mandatory)

    note = update_restart_ready.current()
    assert note is not None
    assert (note.reason, note.version, note.mandatory) == (
        update_restart_ready.REASON_STAGED,
        "9.9.9",
        mandatory,
    )
    orch.dashboard_state.push_refresh.assert_called_with("update_available")


@pytest.mark.asyncio
async def test_a_tree_already_staged_is_not_built_again(monkeypatch) -> None:
    orch = _make_orchestrator()
    orch.dashboard_state = _mock_dashboard_state()
    engine = wire_wheel_apply(monkeypatch)

    await orch._auto_apply_wheel_update("stable", "9.9.9")
    await orch._auto_apply_wheel_update("stable", "9.9.9")

    assert engine.call_count == 1


@pytest.mark.asyncio
async def test_a_newer_release_than_the_staged_one_is_built(monkeypatch) -> None:
    orch = _make_orchestrator()
    orch.dashboard_state = _mock_dashboard_state()
    engine = wire_wheel_apply(monkeypatch)

    await orch._auto_apply_wheel_update("stable", "9.9.9")
    await orch._auto_apply_wheel_update("stable", "9.9.10")

    assert engine.call_count == 2
    note = update_restart_ready.current()
    assert note is not None and note.version == "9.9.10"


@pytest.mark.asyncio
async def test_a_gateway_loading_through_the_stable_link_is_not_restarted_to_build(
    monkeypatch,
) -> None:
    """Building under it would swap its modules; getting off the link means a restart."""
    orch = _make_orchestrator()
    orch.dashboard_state = _mock_dashboard_state()
    engine = wire_wheel_apply(monkeypatch)
    monkeypatch.setattr("kiro_crew.platform.wheel_apply.relaunch_before_apply", lambda: True)

    await orch._auto_apply_wheel_update("stable", "9.9.9")

    engine.assert_not_called()
    assert update_restart_ready.current() is None
    orch.dashboard_state.push_refresh.assert_called_with("update_available")


@pytest.mark.asyncio
async def test_a_promotion_the_restart_cannot_reach_is_not_called_ready(monkeypatch) -> None:
    orch = _make_orchestrator()
    orch.dashboard_state = _mock_dashboard_state()
    wire_wheel_apply(monkeypatch, reaches=False)

    await orch._auto_apply_wheel_update("stable", "9.9.9")

    assert update_restart_ready.current() is None


# -- what the dashboard reads -------------------------------------------------------------


def test_the_status_frame_says_nothing_is_waiting_until_something_is() -> None:
    from kiro_crew.dashboard.handlers.updates_check import status_update_fields

    fields = status_update_fields()
    assert fields["update_restart_ready"] is False
    assert fields["update_restart_ready_version"] == ""

    update_restart_ready.mark(update_restart_ready.REASON_STAGED, version="9.9.9")
    fields = status_update_fields()
    assert fields["update_restart_ready"] is True
    assert fields["update_restart_ready_version"] == "9.9.9"
    assert fields["update_restart_ready_reason"] == "staged"


def test_the_status_snapshot_carries_the_note_through_to_the_frame() -> None:
    from kiro_crew.dashboard.handlers.updates_check import status_update_fields
    from kiro_crew.dashboard.state_parts.status_payload import status_snapshot

    update_restart_ready.mark(update_restart_ready.REASON_ASSETS_GONE)
    state = MagicMock()
    state.start_time = 0.0
    state._build_info = ("main", "abc")
    state.sessions.count = 0
    state.subagents = None

    frame = status_snapshot(state, cron_jobs=0, lessons=0, **status_update_fields())  # type: ignore[arg-type]

    assert frame["update_restart_ready"] is True
    assert frame["update_restart_ready_reason"] == "assets_gone"


def test_marking_the_same_note_twice_changes_nothing() -> None:
    assert update_restart_ready.mark(update_restart_ready.REASON_STAGED, version="1.0.0") is True
    assert update_restart_ready.mark(update_restart_ready.REASON_STAGED, version="1.0.0") is False
    assert update_restart_ready.mark(update_restart_ready.REASON_STAGED, version="1.0.1") is True


def test_a_clear_for_one_reason_leaves_another_reasons_note() -> None:
    update_restart_ready.mark(update_restart_ready.REASON_STAGED, version="1.0.0")

    assert update_restart_ready.clear(update_restart_ready.REASON_ASSETS_GONE) is False
    assert update_restart_ready.current() is not None
    assert update_restart_ready.clear() is True
    assert update_restart_ready.current() is None


def test_the_boot_no_longer_hands_the_watchdog_a_way_to_exit_the_gateway() -> None:
    """The watchdog's only seams are the shutdown event it reads and a change callback."""
    import inspect

    from kiro_crew.dashboard import stale_asset_watchdog

    params = set(inspect.signature(stale_asset_watchdog.run_stale_asset_watchdog).parameters)
    assert params == {"shutdown_event", "interval", "confirm_delay", "on_change"}
    assert "shutdown_event.set" not in Path(stale_asset_watchdog.__file__).read_text()


# -- structure: no scheduler entry point can await a restart -----------------------------

#: Every call that replaces, fences or closes the running gateway. A scheduler path
#: that mentions one of these could restart it.
_RESTART_PRIMITIVES = frozenset(
    {
        "execv",
        "execve",
        "reexec_launcher",
        "reexec_python_module",
        "exit_after_failed_restart_exec",
        "_restart_gateway",
        "_restart_after_update",
        "_restart_after_update_body",
        "_restart_after_update_claimed",
        "_retry_pending_update_restart",
        "_prepare_auto_update_apply",
        "_restart_gate_when_idle",
        "close_sessions_for_restart",
        "fence_update_restart",
        "close_all",
        "run_pre_exec_stops",
        "stop_cron_before_restart",
        "admit_restart",
        "supervisor_reentry",
        "request_supervisor_reentry",
    }
)

#: The scheduler's modules: the coordinator the update task runs, the stale-asset
#: watchdog task, and the apply helpers they call.
_SCHEDULER_MODULES = (
    "kiro_crew.slack.gateway_runtime.update_schedule",
    "kiro_crew.slack.gateway_runtime.update_apply",
    "kiro_crew.dashboard.stale_asset_watchdog",
)


def _names_used(module_name: str) -> dict[str, set[str]]:
    """Function name -> every name and attribute its body mentions."""
    import ast
    import importlib

    tree = ast.parse(Path(importlib.import_module(module_name).__file__).read_text())
    used: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names = used.setdefault(node.name, set())
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name):
                    names.add(sub.id)
                elif isinstance(sub, ast.Attribute):
                    names.add(sub.attr)
                elif isinstance(sub, (ast.ImportFrom, ast.Import)):
                    names.update(alias.name.split(".")[-1] for alias in sub.names)
    return used


@pytest.mark.parametrize("module_name", _SCHEDULER_MODULES)
def test_no_scheduler_function_mentions_a_restart_primitive(module_name) -> None:
    offenders = {
        fn: sorted(names & _RESTART_PRIMITIVES)
        for fn, names in _names_used(module_name).items()
        if names & _RESTART_PRIMITIVES
    }

    assert offenders == {}


def test_the_orchestrator_has_no_unattended_restart_entry_point() -> None:
    from kiro_crew.slack.gateway import GatewayOrchestrator

    gone = [
        name
        for name in (
            "_restart_after_update",
            "_restart_after_update_body",
            "_restart_after_update_claimed",
            "_retry_pending_update_restart",
            "_prepare_auto_update_apply",
            "_restart_gate_when_idle",
            "_restart_gate_held",
            "_release_restart_gate_hold",
            "_auto_apply_update",
        )
        if hasattr(GatewayOrchestrator, name)
    ]

    assert gone == []


@pytest.mark.parametrize("floor", [False, True], ids=["voluntary", "floor"])
@pytest.mark.parametrize("auto_update", [True, False], ids=["auto-on", "auto-off"])
@pytest.mark.parametrize(
    "route",
    [AUTO_ROUTE_GIT, AUTO_ROUTE_PROVIDER, AUTO_ROUTE_WHEEL],
)
def test_a_full_coordinator_cycle_never_reaches_a_restart(route, auto_update, floor) -> None:
    """Run the update task's cycle on every route and trip on any process-ending call."""
    import kiro_crew.dashboard.handlers as handlers
    from kiro_crew import platform_compat

    tripped: list[str] = []

    def trip(name):
        def _raise(*_a, **_k):
            tripped.append(name)
            raise AssertionError(f"{name} reached from the update scheduler")

        return _raise

    orch = _orchestrator()
    info = _MANAGED_UPDATE if route == AUTO_ROUTE_WHEEL else _GIT_UPDATE
    effect = AutoUpdateEffect(AUTO_EFFECT_MANDATORY if floor else AUTO_EFFECT_INSTALL, route)
    original = dict(handlers._update_info)
    try:
        handlers._update_info.clear()
        handlers._update_info.update(info)
        with (
            patch.object(handlers, "_do_update_check", new_callable=AsyncMock),
            patch(
                "kiro_crew.config.KiroCrewConfig.load", return_value=_cfg(auto_update=auto_update)
            ),
            patch("kiro_crew.platform.update_governance.update_required", return_value=floor),
            patch("kiro_crew.platform.update_governance.min_version", return_value="9.9.0"),
            patch("kiro_crew.platform.wheel_engine.running_from_managed_venv", return_value=True),
            patch("kiro_crew.platform.governance.active_update_pins", return_value=UpdatePins()),
            patch("kiro_crew.slack.gateway.auto_update_effect", return_value=effect),
            patch.object(platform_compat, "reexec_launcher", trip("reexec_launcher")),
            patch.object(platform_compat, "reexec_python_module", trip("reexec_python_module")),
            patch("os.execv", trip("os.execv")),
            patch(
                "kiro_crew.dashboard.handlers.updates_apply._restart_gateway",
                trip("_restart_gateway"),
            ),
        ):
            asyncio.run(orch._check_for_updates())
    finally:
        handlers._update_info.clear()
        handlers._update_info.update(original)

    assert tripped == []
    orch.sessions.pause_turn_admission_for_update.assert_not_awaited()
