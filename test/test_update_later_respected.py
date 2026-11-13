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
        # A skip survives a later snooze of another version.
        ({**_snoozed("9.9.10", _NOW + _DAY), "skipped_versions": ["9.9.9"]}, True),
        ({**_snoozed("9.9.10", _NOW + _DAY), "skipped_versions": ["9.9.8"]}, False),
        ({**_snoozed("9.9.10", _NOW + _DAY), "skipped_versions": "9.9.9"}, False),
        ({}, False),
        (None, False),
        ({"version": "9.9.9", "snoozed_until": "soon"}, False),
        ({"version": "9.9.9", "snoozed_until": True}, False),
    ],
)
def test_the_record_defers_only_its_own_version_while_it_lasts(record, deferred) -> None:
    from kiro_crew.update_deferral import user_deferred_update

    assert user_deferred_update(_cfg(record), "9.9.9", now=_NOW) is deferred


def test_a_verdict_keeps_the_saved_skips_in_order_and_adds_its_own_last() -> None:
    from kiro_crew.update_deferral import (
        MAX_SKIPPED_VERSIONS,
        SkipListFull,
        merge_update_nudge_record,
    )

    saved = {"version": "99.1", "skipped": True, "skipped_versions": ["99.0"]}
    snooze = {"version": "99.2", "snoozed_until": 5, "skipped": False}
    assert merge_update_nudge_record(saved, snooze)["skipped_versions"] == ["99.0", "99.1"]
    skip = {"version": "99.0", "snoozed_until": 0, "skipped": True}
    assert merge_update_nudge_record(saved, skip)["skipped_versions"] == ["99.1", "99.0"]
    # A skip past the cap is refused; none that still holds is evicted.
    many = {"skipped_versions": [f"99.0.{i}" for i in range(MAX_SKIPPED_VERSIONS)]}
    new = {"version": "99.1.0", "snoozed_until": 0, "skipped": True}
    with pytest.raises(SkipListFull):
        merge_update_nudge_record(many, new)
    assert merge_update_nudge_record(many, snooze)["skipped_versions"] == many["skipped_versions"]


def test_a_skip_of_the_running_version_is_kept() -> None:
    # A git checkout can offer an update without a version bump, so a skip is
    # never dropped by comparing it with this build.
    from kiro_crew import __version__
    from kiro_crew.update_deferral import merge_update_nudge_record

    saved = {"skipped_versions": ["0.0.1", __version__]}
    snooze = {"version": __version__, "snoozed_until": 5, "skipped": False}
    assert merge_update_nudge_record(saved, snooze)["skipped_versions"] == ["0.0.1", __version__]


def test_a_record_that_sends_the_list_is_stored_as_sent() -> None:
    from kiro_crew.update_deferral import merge_update_nudge_record, with_skip_list

    saved = {"version": "1.0", "snoozed_until": 0, "skipped": True}
    whole = {"version": "1.0", "snoozed_until": 0, "skipped": False, "skipped_versions": []}
    assert merge_update_nudge_record(saved, whole) == whole
    assert with_skip_list(saved) == {**saved, "skipped_versions": ["1.0"]}
    assert with_skip_list(whole) is whole


def test_an_unknown_version_is_never_deferred() -> None:
    from kiro_crew.update_deferral import user_deferred_update

    assert user_deferred_update(_cfg(_snoozed("", _NOW + _DAY)), "", now=_NOW) is False


@pytest.mark.parametrize(
    ("browser_skew", "now", "deferred"),
    [
        # Two days behind: the snooze is already over by this host's clock, yet the
        # stamp still holds for the snooze plus one prompt window.
        (-2 * _DAY, _NOW, True),
        (-2 * _DAY, _NOW + 2 * _DAY - 1, True),
        (-2 * _DAY, _NOW + 2 * _DAY, False),
        # Ahead: the later browser reading still holds, so a browser with a right clock,
        # which stays quiet until then, is not installed over before it asks again.
        (5 * _DAY, _NOW + 6 * _DAY, True),
    ],
)
def test_the_hold_counts_from_when_the_gateway_stored_the_snooze(
    browser_skew, now, deferred
) -> None:
    from kiro_crew.update_deferral import user_deferred_update

    # What "Remind me tomorrow" writes from a browser clock off by browser_skew,
    # stamped by the gateway's PATCH at _NOW.
    record = {**_snoozed("9.9.9", _NOW + browser_skew + _DAY), "stored_at": _NOW}
    assert user_deferred_update(_cfg(record), "9.9.9", now=now) is deferred


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


def test_a_snoozed_provider_update_is_only_announced(gateway_logs) -> None:
    orch, apply = _run_provider(_cfg(_snoozed("2.0.0", _FAR_FUTURE)))
    apply.assert_not_awaited()
    orch._restart_after_update.assert_not_awaited()
    orch._prepare_auto_update_apply.assert_not_awaited()


def test_a_provider_update_nobody_put_off_is_announced_too(gateway_logs) -> None:
    """The policy command rewrites the running install, so no unattended path runs it."""
    orch, apply = _run_provider(_cfg(_snoozed("1.9.0", _FAR_FUTURE)))
    apply.assert_not_awaited()
    orch._restart_after_update.assert_not_awaited()
    orch._prepare_auto_update_apply.assert_not_awaited()
    logs = gateway_logs()
    assert not [m for m in logs if "applying update via provider" in m]


# ── the git route ───────────────────────────────────────────────────────────


def test_a_snoozed_git_update_is_not_pulled_unattended(gateway_logs) -> None:
    orch = _run_legacy(_cfg(_snoozed("9.9.9", _FAR_FUTURE)), info=_GIT_UPDATE, route="git")
    orch._auto_apply_update.assert_not_awaited()
    orch.dashboard_state.push_refresh.assert_called_with("update_available")
    assert f"Update 9.9.9 {_NOT_INSTALLED}" in gateway_logs()


def test_a_git_update_nobody_put_off_is_announced_not_pulled() -> None:
    """A reset rewrites the tree the gateway serves from, so a person applies it."""
    orch = _run_legacy(_cfg(_snoozed("9.9.8", _FAR_FUTURE)), info=_GIT_UPDATE, route="git")
    orch._auto_apply_update.assert_not_awaited()
    orch._restart_after_update.assert_not_awaited()
    orch.dashboard_state.push_refresh.assert_called_with("update_available")


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
