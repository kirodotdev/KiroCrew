"""An update the user put off is never installed and restarted unattended.

The update prompts record "Remind me tomorrow" / "Skip this version" in
``dashboard.update_nudge``. The background coordinator applies an update and
restarts the gateway on its own when ``auto_update`` is on, so it must read that
record too: otherwise the app restarts minutes after the user said "later".
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from update_check_install_aware_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _wheel_install,
)

from kiro_crew.platform.update_capability import AutoUpdateEffect
from kiro_crew.platform.update_provider import CommandProvider, UpdateCheckResult

_NOW = 1_800_000_000.0
_DAY = 24 * 60 * 60
# The coordinator reads the real clock; these bounds sit decades on either side of it.
_FAR_FUTURE = 4_000_000_000.0
_LONG_AGO = 1.0


def _cfg(record: object, *, auto_update: bool = True) -> SimpleNamespace:
    return SimpleNamespace(auto_update=auto_update, dashboard=SimpleNamespace(update_nudge=record))


def _snoozed(version: str, until: float) -> dict:
    return {"version": version, "snoozed_until": until, "skipped": False}


_GATEWAY_LOGGER = "kiro_crew.slack.gateway"
_NOT_INSTALLED = "was put off by the user — not installing it unattended"
_NOT_RESTARTED = "is ready but was put off by the user — not restarting"
_BUILDING = "Auto-update enabled for managed install — building beside it"


@pytest.fixture
def gateway_logs(caplog):
    """The update coordinator's own log lines, read after the run."""
    caplog.set_level(logging.INFO, logger=_GATEWAY_LOGGER)
    return lambda: [r.getMessage() for r in caplog.records if r.name == _GATEWAY_LOGGER]


# ── the record ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("record", "deferred"),
    [
        (_snoozed("9.9.9", _NOW + _DAY), True),
        ({"version": "9.9.9", "snoozed_until": 0, "skipped": True}, True),
        # The prompt reopens when the snooze lapses; it gets one more window.
        (_snoozed("9.9.9", _NOW - 1), True),
        (_snoozed("9.9.9", _NOW - _DAY + 1), True),
        (_snoozed("9.9.9", _NOW - _DAY - 1), False),
        (_snoozed("9.9.8", _NOW + _DAY), False),
        ({}, False),
        (None, False),
        ({"version": "9.9.9", "snoozed_until": "soon"}, False),
        ({"version": "9.9.9", "snoozed_until": True}, False),
    ],
)
def test_the_record_defers_only_its_own_version_while_it_lasts(record, deferred) -> None:
    from kiro_crew.update_deferral import user_deferred_update

    assert user_deferred_update(_cfg(record), "9.9.9", now=_NOW) is deferred


def test_an_unknown_version_is_never_deferred() -> None:
    from kiro_crew.update_deferral import user_deferred_update

    assert user_deferred_update(_cfg(_snoozed("", _NOW + _DAY)), "", now=_NOW) is False


# ── the coordinator ─────────────────────────────────────────────────────────


def _orchestrator():
    from kiro_crew.slack.gateway import GatewayOrchestrator

    orch = object.__new__(GatewayOrchestrator)
    orch.dashboard_state = MagicMock()
    orch._update_apply_deferred = False
    orch._mandatory_update_deferred_at = None
    orch._mandatory_update_deferred_key = None
    orch._session_tasks = {}
    orch.sessions = MagicMock()
    orch.sessions.pause_turn_admission_for_update = AsyncMock(return_value=True)
    orch.sessions.resume_turn_admission_after_update = AsyncMock()
    orch.sessions.drain_active_turns = AsyncMock(return_value=0)
    orch._schedule_inbound_replay = MagicMock()
    orch._in_flight_work_counts = MagicMock(return_value=(0, 0))
    orch._auto_apply_update = AsyncMock()
    orch._auto_apply_wheel_update = AsyncMock()
    orch._restart_after_update = AsyncMock()
    return orch


_MANAGED_UPDATE = {
    "update_available": True,
    "can_apply": False,
    "managed_by": "kirocrew",
    "channel": "stable",
    "latest_version": "9.9.9",
    "remediation": {"kind": "command", "message": "m", "command": "curl -fsSL … | sh"},
}


_GIT_UPDATE = {
    "update_available": True,
    "version_newer": True,
    "can_apply": True,
    "channel": "stable",
    "latest_version": "9.9.9",
}


def _run_legacy(cfg: SimpleNamespace, *, info: dict = _MANAGED_UPDATE, route: str = "wheel"):
    import kiro_crew.dashboard.handlers as handlers
    from kiro_crew.platform.governance import UpdatePins

    orch = _orchestrator()
    orch._prepare_auto_update_apply = AsyncMock(return_value=True)
    orch._finish_auto_update_apply = AsyncMock()
    original = dict(handlers._update_info)
    try:
        handlers._update_info.clear()
        handlers._update_info.update(info)
        with (
            patch.object(handlers, "_do_update_check", new_callable=AsyncMock),
            patch("kiro_crew.config.KiroCrewConfig.load", return_value=cfg),
            patch("kiro_crew.platform.update_governance.update_required", return_value=False),
            patch("kiro_crew.platform.wheel_engine.running_from_managed_venv", return_value=True),
            patch("kiro_crew.platform.governance.active_update_pins", return_value=UpdatePins()),
            patch(
                "kiro_crew.slack.gateway.auto_update_effect",
                return_value=AutoUpdateEffect("install", route),
            ),
        ):
            asyncio.run(orch._check_for_updates())
    finally:
        handlers._update_info.clear()
        handlers._update_info.update(original)
    return orch


def test_a_snoozed_update_is_not_installed_unattended(gateway_logs) -> None:
    orch = _run_legacy(_cfg(_snoozed("9.9.9", _FAR_FUTURE)))
    orch._auto_apply_wheel_update.assert_not_awaited()
    orch._auto_apply_update.assert_not_awaited()
    orch.dashboard_state.push_refresh.assert_called_with("update_available")
    assert f"Update 9.9.9 {_NOT_INSTALLED}" in gateway_logs()


def test_a_skipped_update_is_not_installed_unattended(gateway_logs) -> None:
    orch = _run_legacy(_cfg({"version": "9.9.9", "snoozed_until": 0, "skipped": True}))
    orch._auto_apply_wheel_update.assert_not_awaited()
    assert f"Update 9.9.9 {_NOT_INSTALLED}" in gateway_logs()


@pytest.mark.parametrize(
    "record",
    [_snoozed("9.9.8", _FAR_FUTURE), _snoozed("9.9.9", _LONG_AGO), {}],
    ids=["other-version", "snooze-over", "no-record"],
)
def test_without_a_live_deferral_the_update_still_installs(record, gateway_logs) -> None:
    orch = _run_legacy(_cfg(record))
    orch._auto_apply_wheel_update.assert_awaited_once_with("stable", "9.9.9")
    logs = gateway_logs()
    assert _BUILDING in logs
    assert not [m for m in logs if _NOT_INSTALLED in m]


def _run_provider(cfg: SimpleNamespace):
    import kiro_crew.dashboard.handlers as handlers

    orch = _orchestrator()
    orch._auto_update_effect = AsyncMock(return_value=AutoUpdateEffect("install", "provider"))
    orch._prepare_auto_update_apply = AsyncMock(return_value=True)
    orch._finish_auto_update_apply = AsyncMock()
    provider = CommandProvider(check_command="check-cmd", apply_command="apply-cmd")
    original = dict(handlers._update_info)
    try:
        with (
            patch.object(
                CommandProvider,
                "check",
                AsyncMock(
                    side_effect=[
                        UpdateCheckResult(available=True, remote_version="2.0.0"),
                        UpdateCheckResult(available=False),
                    ]
                ),
            ),
            patch.object(CommandProvider, "apply", AsyncMock(return_value=True)) as apply,
            patch("kiro_crew.config.KiroCrewConfig.load", return_value=cfg),
            patch("kiro_crew.platform.update_governance.update_required", return_value=False),
            patch(
                "kiro_crew.platform.wheel_apply.snapshot_memory_before_update",
                return_value=(0, ""),
            ),
        ):
            asyncio.run(orch._check_for_updates_via_provider(provider))
    finally:
        handlers._update_info.clear()
        handlers._update_info.update(original)
    return orch, apply


def test_a_snoozed_provider_update_is_not_applied_or_restarted(gateway_logs) -> None:
    orch, apply = _run_provider(_cfg(_snoozed("2.0.0", _FAR_FUTURE)))
    apply.assert_not_awaited()
    orch._restart_after_update.assert_not_awaited()
    orch._prepare_auto_update_apply.assert_not_awaited()
    assert f"Update 2.0.0 {_NOT_INSTALLED}" in gateway_logs()


def test_a_provider_update_nobody_put_off_still_applies(gateway_logs) -> None:
    orch, apply = _run_provider(_cfg(_snoozed("1.9.0", _FAR_FUTURE)))
    apply.assert_awaited_once()
    orch._restart_after_update.assert_awaited_once()
    logs = gateway_logs()
    assert "Auto-update enabled — applying update via provider" in logs
    assert not [m for m in logs if _NOT_INSTALLED in m]


# ── the git route ───────────────────────────────────────────────────────────


def test_a_snoozed_git_update_is_not_pulled_unattended(gateway_logs) -> None:
    orch = _run_legacy(_cfg(_snoozed("9.9.9", _FAR_FUTURE)), info=_GIT_UPDATE, route="git")
    orch._auto_apply_update.assert_not_awaited()
    orch.dashboard_state.push_refresh.assert_called_with("update_available")
    assert f"Update 9.9.9 {_NOT_INSTALLED}" in gateway_logs()


def test_a_git_update_nobody_put_off_still_pulls_and_queues_an_unheld_restart() -> None:
    orch = _run_legacy(_cfg(_snoozed("9.9.8", _FAR_FUTURE)), info=_GIT_UPDATE, route="git")
    orch._auto_apply_update.assert_awaited_once_with()
    # The git apply reloads `kiro_crew`; its queued restart must never be held.
    assert orch._pending_update_version == ""


def test_a_provider_update_queues_an_unheld_restart() -> None:
    orch, _apply = _run_provider(_cfg(_snoozed("1.9.0", _FAR_FUTURE)))
    assert orch._pending_update_version == ""


def test_a_reloaded_git_restart_still_restarts_after_skip_and_a_raised_floor() -> None:
    """After a git apply the floor check reads the reloaded `__version__`, so it
    cannot see the old code is below a raised floor; the queued restart is the
    only way onto the new code, and a skip of that version must not hold it."""
    orch = _run_legacy(_cfg({}), info=_GIT_UPDATE, route="git")
    orch._pending_update_respawn = lambda: "/venv/bin/python"
    orch._pending_update_mandatory = False
    orch._restart_after_update = AsyncMock()
    orch._check_for_updates = AsyncMock()
    with (
        patch("kiro_crew.config.KiroCrewConfig.load", return_value=_cfg(_SKIPPED_999)),
        # The raised floor reads as met against the reloaded version.
        patch("kiro_crew.platform.update_governance.update_required", return_value=False),
        _stop_after_one_cycle(),
        pytest.raises(_OneCycle),
    ):
        asyncio.run(orch._run_update_checks())
    orch._restart_after_update.assert_awaited_once()


# ── a restart already queued ────────────────────────────────────────────────


class _OneCycle(Exception):
    """Raised by the patched sleep so the coordinator runs exactly one cycle."""


def _stop_after_one_cycle():
    """End the coordinator's loop at its sleep; no test here asserts timing."""
    return patch.object(
        asyncio, "sleep", AsyncMock(side_effect=_OneCycle)
    )  # flake-ok: loop stop only


def _run_one_pending_cycle(cfg: SimpleNamespace, *, mandatory: bool = False):
    orch = _orchestrator()
    orch._pending_update_respawn = lambda: "/venv/bin/python"
    orch._pending_update_mandatory = mandatory
    orch._pending_update_mandatory_key = "floor" if mandatory else ""
    orch._pending_update_version = "" if mandatory else "9.9.9"
    orch._prepare_auto_update_apply = AsyncMock(return_value=True)
    orch._finish_auto_update_apply = AsyncMock()
    orch._check_for_updates = AsyncMock()
    with (
        patch("kiro_crew.config.KiroCrewConfig.load", return_value=cfg),
        _stop_after_one_cycle(),
        pytest.raises(_OneCycle),
    ):
        asyncio.run(orch._run_update_checks())
    return orch


def test_a_queued_restart_waits_while_the_user_put_it_off() -> None:
    orch = _run_one_pending_cycle(_cfg(_snoozed("9.9.9", _FAR_FUTURE)))
    orch._restart_after_update.assert_not_awaited()
    orch._prepare_auto_update_apply.assert_not_awaited()
    assert orch._pending_update_respawn is not None
    orch.dashboard_state.push_refresh.assert_called_with("update_available")


@pytest.mark.parametrize(
    "record",
    [_snoozed("9.9.8", _FAR_FUTURE), _snoozed("9.9.9", _LONG_AGO), {}],
    ids=["other-version", "reprompt-over", "no-record"],
)
def test_a_queued_restart_nobody_put_off_still_restarts(record) -> None:
    orch = _run_one_pending_cycle(_cfg(record))
    orch._restart_after_update.assert_awaited_once()


def test_a_held_restart_still_runs_the_update_check(gateway_logs) -> None:
    orch = _run_one_pending_cycle(_cfg({"version": "9.9.9", "snoozed_until": 0, "skipped": True}))
    orch._restart_after_update.assert_not_awaited()
    orch._check_for_updates.assert_awaited_once()
    assert f"Update 9.9.9 {_NOT_RESTARTED}" in gateway_logs()


def test_a_restart_nobody_held_does_not_also_run_the_check() -> None:
    orch = _run_one_pending_cycle(_cfg({}))
    orch._restart_after_update.assert_awaited_once()
    orch._check_for_updates.assert_not_awaited()


def _run_held_cycle_through_the_real_check(
    cfg: SimpleNamespace, info: dict, effect: AutoUpdateEffect, *, floor: bool
):
    """One coordinator cycle with a skipped restart queued and the real check."""
    import kiro_crew.dashboard.handlers as handlers
    from kiro_crew.platform.governance import UpdatePins

    orch = _orchestrator()
    orch._pending_update_respawn = lambda: "/venv/bin/python"
    orch._pending_update_mandatory = False
    orch._pending_update_mandatory_key = ""
    orch._pending_update_version = "9.9.9"
    orch._prepare_auto_update_apply = AsyncMock(return_value=True)
    orch._finish_auto_update_apply = AsyncMock()
    original = dict(handlers._update_info)
    try:
        handlers._update_info.clear()
        handlers._update_info.update(info)
        with (
            patch.object(handlers, "_do_update_check", new_callable=AsyncMock),
            patch("kiro_crew.config.KiroCrewConfig.load", return_value=cfg),
            patch("kiro_crew.platform.update_governance.update_required", return_value=floor),
            patch("kiro_crew.platform.update_governance.min_version", return_value="9.9.9"),
            patch("kiro_crew.platform.wheel_engine.running_from_managed_venv", return_value=True),
            patch("kiro_crew.platform.governance.active_update_pins", return_value=UpdatePins()),
            patch("kiro_crew.slack.gateway.auto_update_effect", return_value=effect),
            _stop_after_one_cycle(),
            pytest.raises(_OneCycle),
        ):
            asyncio.run(orch._run_update_checks())
    finally:
        handlers._update_info.clear()
        handlers._update_info.update(original)
    return orch


_SKIPPED_999 = {"version": "9.9.9", "snoozed_until": 0, "skipped": True}


def test_a_floor_raised_while_a_skipped_restart_is_held_is_still_applied(gateway_logs) -> None:
    orch = _run_held_cycle_through_the_real_check(
        _cfg(_SKIPPED_999), _MANAGED_UPDATE, AutoUpdateEffect("mandatory", "wheel"), floor=True
    )
    orch._restart_after_update.assert_not_awaited()
    orch._auto_apply_wheel_update.assert_awaited_once()
    assert orch._auto_apply_wheel_update.await_args.kwargs["mandatory"] is True
    logs = gateway_logs()
    assert f"Update 9.9.9 {_NOT_RESTARTED}" in logs
    assert [m for m in logs if "applying mandatory update beside the managed venv" in m]


def test_a_newer_release_is_still_found_while_a_skipped_restart_is_held(gateway_logs) -> None:
    newer = {**_MANAGED_UPDATE, "latest_version": "9.9.10"}
    orch = _run_held_cycle_through_the_real_check(
        _cfg(_SKIPPED_999), newer, AutoUpdateEffect("install", "wheel"), floor=False
    )
    orch._restart_after_update.assert_not_awaited()
    orch._auto_apply_wheel_update.assert_awaited_once_with("stable", "9.9.10")
    logs = gateway_logs()
    assert f"Update 9.9.9 {_NOT_RESTARTED}" in logs
    assert _BUILDING in logs


def test_a_policy_floor_restart_does_not_wait_for_a_snooze() -> None:
    orch = _run_one_pending_cycle(_cfg(_snoozed("9.9.9", _FAR_FUTURE)), mandatory=True)
    orch._restart_after_update.assert_awaited_once()


def test_a_managed_venv_build_then_later_keeps_the_restart_queued() -> None:
    """The build starts on first detection; "Later" clicked while its restart
    waits on a busy gateway still holds the restart back."""
    orch = _orchestrator()
    del orch._auto_apply_wheel_update  # the real apply, not the helper's stub
    orch._pending_update_respawn = None
    orch._pending_update_mandatory = False
    orch._pending_update_mandatory_key = ""
    orch._pending_update_version = ""
    # Busy on the first attempt, so the promoted build's restart stays queued.
    orch._prepare_auto_update_apply = AsyncMock(return_value=False)
    orch._finish_auto_update_apply = AsyncMock()
    orch._check_for_updates = AsyncMock()
    with (
        patch("kiro_crew.platform.wheel_engine.check_release_version"),
        patch("kiro_crew.platform.wheel_apply.preflight_bases", return_value=("f", "a")),
        patch("kiro_crew.platform.wheel_apply.userns_reattach_needed", return_value=False),
        patch("kiro_crew.platform.wheel_apply.relaunch_before_apply", return_value=False),
        patch(
            "kiro_crew.platform.wheel_apply.run_wheel_apply",
            AsyncMock(return_value=SimpleNamespace(status="promoted")),
        ),
        patch("kiro_crew.platform.wheel_apply.restart_reaches", return_value=True),
        patch("kiro_crew.config.KiroCrewConfig.load", return_value=_cfg({})),
    ):
        asyncio.run(orch._auto_apply_wheel_update("stable", "9.9.9"))
    assert orch._pending_update_respawn is not None
    assert orch._pending_update_version == "9.9.9"

    orch._prepare_auto_update_apply = AsyncMock(return_value=True)
    with (
        patch(
            "kiro_crew.config.KiroCrewConfig.load",
            return_value=_cfg(_snoozed("9.9.9", _FAR_FUTURE)),
        ),
        _stop_after_one_cycle(),
        pytest.raises(_OneCycle),
    ):
        asyncio.run(orch._run_update_checks())
    orch._restart_after_update.assert_not_awaited()
