"""The unattended update apply, and the one derivation that decides it.

A wheel install never drives the git-based auto-apply path, a policy-pinned
command provider owns the check, and ``auto_update_effect`` is the action the
loop takes: the status field, every apply route and the frames served before the
first derivation agree with it. The shared autouse fixture lives in
``update_check_install_aware_helpers.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from update_check_install_aware_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _wheel_install,
)

from kiro_crew.dashboard.handlers import updates
from kiro_crew.platform import update_provider
from kiro_crew.platform.update_capability import AutoUpdateEffect
from kiro_crew.platform.update_provider import CommandProvider, UpdateCheckResult

_GATEWAY_LOGGER = "kiro_crew.slack.gateway"
_BUILDING = "Auto-update enabled for managed install — building beside it"
_SHUTTING_DOWN = "Auto-update not started: the gateway is shutting down"


@pytest.fixture
def gateway_logs(caplog):
    """The update coordinator's own log lines, read after the run."""
    caplog.set_level(logging.INFO, logger=_GATEWAY_LOGGER)
    return lambda: [r.getMessage() for r in caplog.records if r.name == _GATEWAY_LOGGER]


class TestAutoApplyGuard:
    """A wheel install must never drive the git-based auto-apply path."""

    @staticmethod
    def _orchestrator():
        from kiro_crew.slack.gateway import GatewayOrchestrator

        # __new__ without __init__: _check_for_updates only touches
        # dashboard_state and _auto_apply_update, and a real construction would
        # drag in credentials, the slot manager and the whole boot path.
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
        orch._auto_apply_update = AsyncMock()
        orch._auto_apply_wheel_update = AsyncMock()
        return orch

    def _run(
        self,
        info: dict[str, object],
        *,
        auto_update: bool,
        managed_venv: bool = True,
        busy: int = 0,
        effect=None,
    ):
        import kiro_crew.dashboard.handlers as handlers

        orch = self._orchestrator()
        orch._in_flight_work_counts = MagicMock(return_value=(busy, 0))
        cfg = MagicMock()
        cfg.auto_update = auto_update
        from kiro_crew.platform.governance import UpdatePins

        original = dict(handlers._update_info)
        try:
            handlers._update_info.clear()
            handlers._update_info.update(info)
            with patch.object(handlers, "_do_update_check", new_callable=AsyncMock):
                with patch("kiro_crew.config.KiroCrewConfig.load", return_value=cfg):
                    with patch(
                        "kiro_crew.platform.update_governance.update_required",
                        return_value=False,
                    ):
                        # Runtime ownership is authoritative. Old managed wheels
                        # predate the build stamp and report "source", while a
                        # foreign wheel must never be rewritten by Kiro Crew.
                        with patch(
                            "kiro_crew.platform.wheel_engine.running_from_managed_venv",
                            return_value=managed_venv,
                        ):
                            # No commands in the policy pins, so resolve_provider
                            # returns None and the code falls through to the legacy
                            # path under test.
                            with (
                                patch(
                                    "kiro_crew.platform.governance.active_update_pins",
                                    return_value=UpdatePins(),
                                ),
                                (
                                    patch(
                                        "kiro_crew.slack.gateway.auto_update_effect",
                                        return_value=effect,
                                    )
                                    if effect is not None
                                    else contextlib.nullcontext()
                                ),
                            ):
                                asyncio.run(orch._check_for_updates())
        finally:
            handlers._update_info.clear()
            handlers._update_info.update(original)
        return orch

    def test_managed_install_auto_applies_without_consulting_the_build_stamp(self, gateway_logs):
        info = {
            "update_available": True,
            "can_apply": False,
            "managed_by": "kirocrew",
            "channel": "stable",
            "latest_version": "9.9.9",
            "remediation": {
                "kind": "command",
                "message": "Re-run the installer to upgrade.",
                "command": "curl -fsSL … | sh",
            },
        }
        with patch(
            "kiro_crew.slack.gateway.distribution",
            side_effect=AssertionError("managed-venv ownership must replace the build stamp"),
        ):
            orch = self._run(
                info,
                auto_update=True,
                managed_venv=True,
                effect=AutoUpdateEffect("install", "wheel"),
            )
        orch._auto_apply_update.assert_not_awaited()
        orch._auto_apply_wheel_update.assert_awaited_once_with("stable", "9.9.9")
        # The new tree builds beside the live one with admission open; only the
        # restart into it (inside the apply) pauses admission.
        orch.sessions.pause_turn_admission_for_update.assert_not_awaited()
        assert _BUILDING in gateway_logs()

    _MANAGED_UPDATE = {
        "update_available": True,
        "can_apply": False,
        "managed_by": "kirocrew",
        "remediation": {
            "kind": "command",
            "message": "Re-run the installer to upgrade.",
            "command": "curl -fsSL … | sh",
        },
    }

    def test_no_apply_starts_once_shutdown_is_signalled(self, gateway_logs):
        # The first coordinator cycle runs while boot is still inside the MCP
        # probe, so a SIGTERM there reaches it before ``_shutdown`` does. An
        # installer admitted now would be stopped mid-write moments later.
        # The effect is pinned so the managed-venv route, and its shutdown gate,
        # is reached on every platform.
        with patch("kiro_crew.slack.gateway.shutdown_event", SimpleNamespace(is_set=lambda: True)):
            orch = self._run(
                dict(self._MANAGED_UPDATE),
                auto_update=True,
                managed_venv=True,
                effect=AutoUpdateEffect("install", "wheel"),
            )
        orch._auto_apply_wheel_update.assert_not_awaited()
        orch.sessions.pause_turn_admission_for_update.assert_not_awaited()
        assert _SHUTTING_DOWN in gateway_logs()

    def test_a_busy_retry_after_shutdown_is_signalled_starts_nothing(self, gateway_logs):
        orch = self._orchestrator()
        orch._pending_update_respawn = MagicMock()
        orch._restart_after_update = AsyncMock()
        orch._finish_auto_update_apply = AsyncMock()
        with patch("kiro_crew.slack.gateway.shutdown_event", SimpleNamespace(is_set=lambda: True)):
            asyncio.run(orch._retry_pending_update_restart())
        orch._restart_after_update.assert_not_awaited()
        orch.sessions.pause_turn_admission_for_update.assert_not_awaited()
        assert gateway_logs() == [_SHUTTING_DOWN]

    def test_finishing_while_the_pause_is_kept_schedules_no_replay(self):
        # During a shutdown the resume keeps the pause and reports it; a replay
        # then would tell senders to resend while the gateway is stopping.
        orch = self._orchestrator()
        orch.sessions.resume_turn_admission_after_update = AsyncMock(return_value=False)

        asyncio.run(orch._finish_auto_update_apply())

        orch.sessions.resume_turn_admission_after_update.assert_awaited_once()
        orch._schedule_inbound_replay.assert_not_called()

    def test_busy_managed_install_still_builds_with_admission_open(self, gateway_logs):
        """In-flight turns delay only the restart, never the build."""
        orch = self._run(
            {
                "update_available": True,
                "can_apply": False,
                "managed_by": "kirocrew",
                "channel": "stable",
                "latest_version": "9.9.9",
                "remediation": {
                    "kind": "command",
                    "message": "Re-run the installer to upgrade.",
                    "command": "curl -fsSL … | sh",
                },
            },
            auto_update=True,
            managed_venv=True,
            busy=1,
            effect=AutoUpdateEffect("install", "wheel"),
        )
        orch._auto_apply_wheel_update.assert_awaited_once_with("stable", "9.9.9")
        orch.sessions.pause_turn_admission_for_update.assert_not_awaited()
        assert _BUILDING in gateway_logs()

    def test_mandatory_busy_update_keeps_deferring_after_the_grace_limit(self):
        orch = self._orchestrator()
        orch._mandatory_update_deferred_at = 0.0
        orch._mandatory_update_deferred_key = "floor:9.9.9"
        orch._in_flight_work_counts = MagicMock(return_value=(1, 0))

        prepared = asyncio.run(
            orch._prepare_auto_update_apply(
                mandatory=True,
                mandatory_key="floor:9.9.9",
            )
        )

        assert prepared is False
        orch.sessions.pause_turn_admission_for_update.assert_awaited_once()
        orch.sessions.drain_active_turns.assert_not_awaited()
        orch.sessions.resume_turn_admission_after_update.assert_awaited_once()
        assert orch._update_apply_deferred is True
        assert orch._mandatory_update_deferred_at == 0.0
        assert orch._mandatory_update_deferred_key == "floor:9.9.9"

    def test_mandatory_update_does_not_drain_through_background_work(self):
        orch = self._orchestrator()
        orch._mandatory_update_deferred_at = 0.0
        orch._mandatory_update_deferred_key = "floor:9.9.9"
        # The active provider turn may belong to the TaskRunner represented by
        # the background count. Cancelling it would lose work while apply still
        # remains deferred.
        orch._in_flight_work_counts = MagicMock(return_value=(1, 1))

        prepared = asyncio.run(
            orch._prepare_auto_update_apply(
                mandatory=True,
                mandatory_key="floor:9.9.9",
            )
        )

        assert prepared is False
        orch.sessions.drain_active_turns.assert_not_awaited()
        orch.sessions.resume_turn_admission_after_update.assert_awaited_once()
        orch._schedule_inbound_replay.assert_called_once()
        assert orch._update_apply_deferred is True

    def test_new_mandatory_target_gets_a_fresh_deferral_window(self):
        orch = self._orchestrator()
        orch._mandatory_update_deferred_at = 0.0
        orch._mandatory_update_deferred_key = "old-floor:1.0.0"
        orch._in_flight_work_counts = MagicMock(return_value=(1, 0))

        prepared = asyncio.run(
            orch._prepare_auto_update_apply(
                mandatory=True,
                mandatory_key="new-floor:2.0.0",
            )
        )

        assert prepared is False
        orch.sessions.drain_active_turns.assert_not_awaited()
        orch.sessions.resume_turn_admission_after_update.assert_awaited_once()
        assert orch._mandatory_update_deferred_at is not None
        assert orch._mandatory_update_deferred_key == "new-floor:2.0.0"

    def test_foreign_environment_never_takes_the_shadow_apply(self, gateway_logs):
        orch = self._run(
            {
                "update_available": True,
                "can_apply": False,
                "managed_by": "kirocrew",
                "remediation": {
                    "kind": "command",
                    "message": "Re-run the installer to upgrade.",
                    "command": "curl -fsSL … | sh",
                },
            },
            auto_update=True,
            managed_venv=False,
        )
        orch._auto_apply_update.assert_not_awaited()
        orch._auto_apply_wheel_update.assert_not_awaited()
        notified = [m for m in gateway_logs() if "cannot apply it unattended" in m]
        assert notified and "this install (kirocrew)" in notified[0]
        assert _BUILDING not in gateway_logs()

    def test_git_checkout_auto_applies_when_the_version_moved(self, gateway_logs):
        """The git apply needs `version_newer`, not just `available`.

        `available` is true on commit distance alone for a checkout, and this
        path applies `git reset --hard`. Requiring the version to have moved
        keeps it firing no more often than while the verdict was version-only
        (see `TestCheckForUpdates` in `test_slack_gateway_update_apply.py` for the negative).
        """
        orch = self._run(
            {
                "update_available": True,
                "can_apply": True,
                "managed_by": "git",
                # The git auto-apply guard requires the version to have moved too,
                # not just commit distance.
                "version_newer": True,
            },
            auto_update=True,
            effect=AutoUpdateEffect("install", "git"),
        )
        orch._auto_apply_update.assert_awaited_once()
        assert "Auto-update enabled — applying update" in gateway_logs()
        # The git apply reloads `kiro_crew`, so its queued restart is never held.
        assert orch._pending_update_version == ""

    def test_a_failed_check_does_not_claim_up_to_date(self, capsys):
        orch = self._run(
            {"update_available": None, "error_code": "feed_unreachable", "managed_by": "kirocrew"},
            auto_update=True,
        )
        orch._auto_apply_update.assert_not_awaited()
        assert "Already on latest version" not in capsys.readouterr().out

    def test_a_clean_check_still_reports_up_to_date(self, capsys):
        self._run(
            {"update_available": False, "check_status": "succeeded", "error_code": None},
            auto_update=True,
        )
        assert "Already on latest version" in capsys.readouterr().out

    def test_a_deferred_check_does_not_claim_up_to_date(self, capsys):
        """A DEFERRAL carries no `error_code`, so keying only on that lies.

        A desktop bundle's own updater owns its bytes: this process never asked the
        feed anything, so it has no verdict to report. Printing "already on latest"
        is the same false reassurance a FAILED check must not print — the deferral
        just arrives through a different field.
        """
        self._run(
            {
                "update_available": None,
                "check_status": "deferred",
                "error_code": None,
                "managed_by": "dmg",
            },
            auto_update=True,
        )
        assert "Already on latest version" not in capsys.readouterr().out

    def test_an_unchecked_state_does_not_claim_up_to_date(self, capsys):
        """Same hole from the other side: no check has run at all yet."""
        self._run(
            {"update_available": None, "check_status": "unchecked", "error_code": None},
            auto_update=True,
        )
        assert "Already on latest version" not in capsys.readouterr().out

    def test_failed_apply_replay_waits_for_the_existing_pass(self, tmp_path):
        from kiro_crew.slack import gateway

        orch = object.__new__(gateway.GatewayOrchestrator)
        replay = AsyncMock()
        orch._replay_spooled_inbound = replay
        release = asyncio.Event()

        async def _scenario() -> None:
            async def _existing() -> None:
                # flake-ok: the stand-in pass parks here until the test sets release below
                await release.wait()

            previous = asyncio.create_task(_existing())
            orch._inbound_replay_task = previous
            with patch.object(gateway.inbound_spool, "spool_path", return_value=tmp_path):
                orch._schedule_inbound_replay()
            scheduled = orch._inbound_replay_task
            # flake-ok: one yield lets the scheduled replay start and find the existing pass still running before release is set
            await asyncio.sleep(0)
            replay.assert_not_awaited()
            release.set()
            # flake-ok: the scheduled replay ends once release is set above; it awaits only the stand-in pass and the mocked replay
            await scheduled

        asyncio.run(_scenario())
        replay.assert_awaited_once_with(spool=tmp_path)


class TestCommandManagedCheck:
    """A policy-pinned command provider owns the check: no feed, no git, no channel.

    Before this, ``resolve_provider`` was consulted only on the apply path, so a
    command-managed host's badge was still computed against the feed/git
    mechanism its policy excluded: the panel could advertise an update the
    Update button (which honors the provider) would never deliver, and named a
    release channel the provider never reads.
    """

    @pytest.fixture(autouse=True)
    def _isolated_cache(self):
        saved_info = dict(updates._update_info)
        saved_clock = updates._last_update_check
        saved_generation = updates._check_generation
        saved_task = updates._check_task
        saved_task_generation = updates._check_task_generation
        yield
        updates._update_info.clear()
        updates._update_info.update(saved_info)
        updates._last_update_check = saved_clock
        updates._check_generation = saved_generation
        updates._check_task = saved_task
        updates._check_task_generation = saved_task_generation

    def _run(self, provider: CommandProvider, result: UpdateCheckResult) -> dict:
        # ``derive_capability`` and both built-in checkers are booby-trapped:
        # the provider branch must bypass the built-in mechanism entirely, and
        # a silent fall-through here would be the exact badge/apply divergence
        # this feature removes. ``_shell_exec_args`` is pinned so ``can_apply``
        # reflects command PRESENCE on every platform — on Windows it refuses
        # every command (fail-closed), which is its own behavior under test in
        # test_update_provider.py, not this dispatch contract's.
        with (
            patch.object(updates, "resolve_provider", return_value=provider),
            patch.object(CommandProvider, "check", AsyncMock(return_value=result)),
            patch.object(
                update_provider, "_shell_exec_args", return_value=["/bin/sh", "-c", "cmd"]
            ),
            patch.object(
                updates,
                "derive_capability",
                side_effect=AssertionError("built-in derivation must not run"),
            ),
            patch.object(
                updates,
                "_check_release_feed",
                side_effect=AssertionError("feed check must not run"),
            ),
            patch.object(
                updates,
                "_check_git_checkout",
                side_effect=AssertionError("git check must not run"),
            ),
        ):
            asyncio.run(updates._do_update_check())
        return updates.get_update_info()

    def test_an_available_update_reports_success_with_no_channel(self):
        provider = CommandProvider(check_command="check-cmd", apply_command="apply-cmd")
        info = self._run(provider, UpdateCheckResult(available=True, remote_version="2.0.0"))
        assert info["update_available"] is True
        assert info["version_newer"] is True
        assert info["latest_version"] == "2.0.0"
        assert info["check_status"] == "succeeded"
        assert info["managed_by"] == "command"
        assert info["can_apply"] is True
        # The core invariant: a command-managed install has no release channel,
        # which is what tells the panel to hide the channel switcher.
        assert info["channel"] == ""
        assert updates.status_update_fields()["update_channel"] == ""

    def test_up_to_date_reports_no_update_not_an_error(self):
        provider = CommandProvider(check_command="check-cmd", apply_command="apply-cmd")
        info = self._run(provider, UpdateCheckResult(available=False))
        assert info["update_available"] is False
        assert info["check_status"] == "succeeded"
        assert info["error_code"] is None
        assert info["latest_version"] == ""

    def test_check_only_pins_offer_no_apply_button(self):
        provider = CommandProvider(check_command="check-cmd")
        info = self._run(provider, UpdateCheckResult(available=True, remote_version="2.0.0"))
        assert info["can_apply"] is False
        assert info["update_available"] is True

    def test_a_failing_provider_reports_a_failed_check_not_a_stale_verdict(self):
        provider = CommandProvider(check_command="check-cmd", apply_command="apply-cmd")
        info = self._run(provider, UpdateCheckResult(error="command timed out"))
        assert info["check_status"] == "failed"
        assert info["update_available"] is None
        assert info["error_code"] == "unknown"
        assert info["latest_version"] == ""


# --- auto_update_effect: one derivation, the status field and the loop agree ---


def _pin_install_shape(
    monkeypatch,
    *,
    managed_by: str,
    branch: str = "main",
    tracks: bool = True,
    exec_config: str = "",
    blocked: str = "",
    managed_venv: bool = True,
    cdn_safe: bool = True,
    provider=None,
    floor: bool = False,
    platform: str = "linux",
    shell: bool = True,
    manual: bool = False,
):
    """Pin every input ``auto_update_effect`` reads, at the seams it reads them.

    ``manual`` is a policy with ``updates.unattended_apply: false``.
    """
    from kiro_crew.platform import (
        update_capability,
        update_governance,
        update_layout,
        update_provider,
        wheel_engine,
    )

    monkeypatch.setattr(
        update_capability,
        "derive_capability",
        lambda **_kw: update_capability.UpdateCapability(
            supported=True,
            managed_by=managed_by,
            mode="notify",
            can_download=True,
            can_apply=managed_by == "git",
            requires_restart=True,
        ),
    )
    monkeypatch.setattr("kiro_crew.platform_compat.trusted_git_bin", lambda: "/usr/bin/git")
    monkeypatch.setattr(update_governance, "_git", lambda _root, *_args: branch)
    monkeypatch.setattr(update_governance, "repo_exec_config_reason", lambda _root: exec_config)
    monkeypatch.setattr(update_governance, "tracks_upstream", lambda _root, _b, **_k: tracks)
    monkeypatch.setattr(update_governance, "resolve_remote_url", lambda *_a, **_k: "https://x")
    monkeypatch.setattr(update_governance, "update_blocked_reason", lambda _url: blocked)
    monkeypatch.setattr(update_governance, "update_required", lambda _v: floor)
    monkeypatch.setattr(update_governance, "unattended_apply_allowed", lambda: not manual)
    monkeypatch.setattr(wheel_engine, "running_from_managed_venv", lambda: managed_venv)
    monkeypatch.setattr(update_layout, "cdn_bases_are_safe", lambda: cdn_safe)
    monkeypatch.setattr(update_layout, "cdn_bases", lambda: ("https://a", "https://b"))
    monkeypatch.setattr(update_capability, "_installer_runs_here", lambda: platform != "win32")
    monkeypatch.setattr(
        "kiro_crew.platform_compat.trusted_system_bin", lambda _n: "/bin/sh" if shell else None
    )
    monkeypatch.setattr(update_provider, "resolve_provider", lambda: provider)


def _provider(*, can_apply: bool):
    from kiro_crew.platform.update_provider import UpdateCheckResult

    provider = MagicMock()
    provider.can_apply = MagicMock(return_value=can_apply)
    provider.check = AsyncMock(return_value=UpdateCheckResult(available=True, remote_version="9"))
    provider.apply = AsyncMock(return_value=False)
    return provider


_SHAPES = [
    pytest.param({"managed_by": "git"}, "install", "git", id="git-primary-branch"),
    pytest.param({"managed_by": "git", "branch": "feature/x"}, "notify", None, id="git-feature"),
    pytest.param({"managed_by": "git", "branch": "HEAD"}, "notify", None, id="git-detached"),
    pytest.param({"managed_by": "git", "tracks": False}, "notify", None, id="git-untracked"),
    pytest.param({"managed_by": "git", "branch": "develop"}, "notify", None, id="fork-develop"),
    pytest.param(
        {"managed_by": "git", "exec_config": "a filter driver"}, "notify", None, id="git-exec"
    ),
    pytest.param({"managed_by": "git", "blocked": "pinned"}, "notify", None, id="git-pinned-away"),
    pytest.param({"managed_by": "git", "floor": True}, "mandatory", "git", id="git-below-floor"),
    pytest.param(
        {"managed_by": "git", "branch": "feature/x", "floor": True},
        "notify",
        None,
        id="git-feature-below-floor",
    ),
    pytest.param({"managed_by": "kirocrew"}, "install", "wheel", id="managed-venv"),
    pytest.param(
        {"managed_by": "kirocrew", "cdn_safe": False}, "notify", None, id="managed-venv-unsafe-cdn"
    ),
    pytest.param(
        {"managed_by": "kirocrew", "managed_venv": False}, "notify", None, id="foreign-wheel"
    ),
    pytest.param(
        {"managed_by": "kirocrew", "platform": "win32"}, "notify", None, id="managed-on-windows"
    ),
    pytest.param(
        {"managed_by": "kirocrew", "shell": False}, "notify", None, id="managed-without-openssl"
    ),
    pytest.param(
        {"managed_by": "kirocrew", "blocked": "pinned"}, "notify", None, id="managed-pinned-away"
    ),
    pytest.param(
        {"managed_by": "kirocrew", "floor": True}, "mandatory", "wheel", id="managed-below-floor"
    ),
    pytest.param({"managed_by": "electron"}, "notify", None, id="desktop-bundle"),
    pytest.param({"managed_by": "container", "floor": True}, "notify", None, id="container-floor"),
    pytest.param(
        {"managed_by": "kirocrew", "provider": "apply"}, "install", "provider", id="provider-apply"
    ),
    pytest.param(
        {"managed_by": "kirocrew", "provider": "check-only"}, "notify", None, id="provider-no-apply"
    ),
    pytest.param(
        {"managed_by": "kirocrew", "provider": "check-only", "floor": True},
        "notify",
        None,
        id="provider-no-apply-below-floor",
    ),
    pytest.param(
        {"managed_by": "kirocrew", "provider": "apply", "floor": True},
        "mandatory",
        "provider",
        id="provider-apply-below-floor",
    ),
    # `updates.unattended_apply: false`: a route that can install only notifies,
    # and keeps its route for a person's Update; the floor still installs.
    pytest.param(
        {"managed_by": "kirocrew", "provider": "apply", "manual": True},
        "notify",
        "provider",
        id="provider-apply-manual-only",
    ),
    pytest.param(
        {"managed_by": "kirocrew", "provider": "apply", "manual": True, "floor": True},
        "mandatory",
        "provider",
        id="provider-apply-manual-only-below-floor",
    ),
    pytest.param({"managed_by": "git", "manual": True}, "notify", "git", id="git-manual-only"),
    pytest.param(
        {"managed_by": "git", "manual": True, "floor": True},
        "mandatory",
        "git",
        id="git-manual-only-below-floor",
    ),
    pytest.param(
        {"managed_by": "kirocrew", "manual": True}, "notify", "wheel", id="managed-venv-manual-only"
    ),
    pytest.param(
        {"managed_by": "kirocrew", "provider": "check-only", "manual": True},
        "notify",
        None,
        id="provider-no-apply-manual-only",
    ),
]


class TestAutoUpdateEffect:
    """The effect the status surface publishes is the action the loop takes."""

    @staticmethod
    def _shape(monkeypatch, shape):
        shape = dict(shape)
        kind = shape.pop("provider", None)
        provider = None if kind is None else _provider(can_apply=kind == "apply")
        _pin_install_shape(monkeypatch, provider=provider, **shape)
        return provider

    @pytest.mark.parametrize("shape, effect, route", _SHAPES)
    def test_the_effect_of_each_install_shape(self, monkeypatch, shape, effect, route):
        from kiro_crew.platform.update_capability import auto_update_effect

        self._shape(monkeypatch, shape)
        answer = auto_update_effect(running_version="1.0.0")

        assert (answer.effect, answer.route) == (effect, route)
        # A shape the loop does not install on says why, for the log line it ends
        # in -- including one whose route is kept for a person's Update.
        assert bool(answer.reason) is (effect == "notify")
        # Only the source pin refuses every path, the manual ones included.
        assert answer.blocked is bool(shape.get("blocked"))

    @pytest.mark.parametrize("shape, effect, route", _SHAPES)
    def test_the_update_loop_acts_on_that_effect(self, monkeypatch, shape, effect, route):
        """``install`` applies with the switch on; ``mandatory`` applies with it off;
        ``notify`` never pauses admission, even with the switch on."""
        import kiro_crew.dashboard.handlers as handlers
        from kiro_crew.platform.governance import UpdatePins

        provider = self._shape(monkeypatch, shape)
        orch = TestAutoApplyGuard._orchestrator()
        orch._prepare_auto_update_apply = AsyncMock(return_value=True)
        orch._finish_auto_update_apply = AsyncMock()
        orch._restart_after_update = AsyncMock()
        cfg = MagicMock()
        cfg.auto_update = effect != "mandatory"

        original = dict(handlers._update_info)
        try:
            handlers._update_info.clear()
            handlers._update_info.update(
                {
                    "update_available": True,
                    "version_newer": True,
                    "can_apply": shape["managed_by"] == "git",
                    "managed_by": shape["managed_by"],
                    "remediation": {"kind": "command", "message": "m", "command": "c"},
                }
            )
            with (
                patch.object(handlers, "_do_update_check", new_callable=AsyncMock),
                patch("kiro_crew.config.KiroCrewConfig.load", return_value=cfg),
                patch(
                    "kiro_crew.platform.governance.active_update_pins", return_value=UpdatePins()
                ),
            ):
                asyncio.run(orch._check_for_updates())
            published = dict(handlers._update_info)
        finally:
            handlers._update_info.clear()
            handlers._update_info.update(original)

        applied = {
            "git": orch._auto_apply_update.await_count,
            "wheel": orch._auto_apply_wheel_update.await_count,
            "provider": provider.apply.await_count if provider is not None else 0,
        }
        if effect == "notify":
            orch._prepare_auto_update_apply.assert_not_awaited()
            assert set(applied.values()) == {0}
            if provider is not None:
                # Notify really notifies: the provider's verdict reaches the badge.
                assert published["update_available"] is True
                # And keeps the Update button wherever a person's Update applies.
                assert published["can_apply"] is (route == "provider")
        else:
            if route == "wheel":
                # The shadow apply builds with admission open and pauses it only
                # for its own restart, inside the apply.
                orch._prepare_auto_update_apply.assert_not_awaited()
            else:
                orch._prepare_auto_update_apply.assert_awaited_once()
            assert applied[route] == 1
            assert sum(applied.values()) == 1


@pytest.mark.parametrize(
    ("lands_during", "copies"),
    [
        pytest.param("check", 0, id="during-the-check"),
        pytest.param("copy", 1, id="during-the-memory-copy"),
    ],
)
def test_unattended_installs_turned_off_mid_cycle_stop_the_provider_apply(
    monkeypatch, lands_during, copies
):
    """The effect is derived before the check; a policy installed since then decides.

    The derivation answers ``install``, then the policy turns unattended installs
    off: during the check, which the read before the memory copy catches without
    spending a copy, or during the copy, which the read just before the apply
    catches. Either way the apply never runs, admission reopens, and the update
    is still offered.
    """
    import kiro_crew.dashboard.handlers as handlers
    from kiro_crew.platform import update_governance
    from kiro_crew.platform.governance import UpdatePins
    from kiro_crew.platform.update_provider import UpdateCheckResult

    provider = _provider(can_apply=True)
    _pin_install_shape(monkeypatch, managed_by="kirocrew", provider=provider)
    policy = {"unattended_apply": True}
    monkeypatch.setattr(
        update_governance, "unattended_apply_allowed", lambda: policy["unattended_apply"]
    )

    async def _check():
        if lands_during == "check":
            policy["unattended_apply"] = False
        return UpdateCheckResult(available=True, remote_version="9")

    taken: list[int] = []

    def _snapshot():
        taken.append(1)
        if lands_during == "copy":
            policy["unattended_apply"] = False
        return 1, ""

    provider.check = AsyncMock(side_effect=_check)
    monkeypatch.setattr("kiro_crew.platform.wheel_apply.snapshot_memory_before_update", _snapshot)
    orch = TestAutoApplyGuard._orchestrator()
    orch._prepare_auto_update_apply = AsyncMock(return_value=True)
    orch._finish_auto_update_apply = AsyncMock()
    orch._restart_after_update = AsyncMock()
    cfg = MagicMock()
    cfg.auto_update = True

    original = dict(handlers._update_info)
    try:
        handlers._update_info.clear()
        with (
            patch("kiro_crew.config.KiroCrewConfig.load", return_value=cfg),
            patch("kiro_crew.platform.governance.active_update_pins", return_value=UpdatePins()),
        ):
            asyncio.run(orch._check_for_updates())
        published = dict(handlers._update_info)
    finally:
        handlers._update_info.clear()
        handlers._update_info.update(original)

    orch._prepare_auto_update_apply.assert_awaited_once_with(mandatory=False)
    provider.apply.assert_not_awaited()
    orch._restart_after_update.assert_not_awaited()
    orch._finish_auto_update_apply.assert_awaited_once()
    assert published["update_available"] is True
    assert len(taken) == copies


def test_a_provider_the_check_resolves_stops_the_built_in_routes(monkeypatch):
    """A live policy refresh must not let a built-in route apply.

    The effect is derived before the check (it reads no check result), so a
    provider configured in between is first seen by the check itself. A provider
    OWNS the update, so no built-in route may apply that cycle.
    """
    import kiro_crew.dashboard.handlers as handlers
    from kiro_crew.platform.governance import UpdatePins
    from kiro_crew.platform.update_capability import MANAGED_BY_COMMAND

    _pin_install_shape(monkeypatch, managed_by="git")
    orch = TestAutoApplyGuard._orchestrator()
    orch._prepare_auto_update_apply = AsyncMock(return_value=True)
    orch._finish_auto_update_apply = AsyncMock()
    cfg = MagicMock()
    cfg.auto_update = True

    async def _check_installs_a_provider():
        # What `_do_update_check` does once a policy provider is configured.
        handlers._update_info.update(
            {
                "managed_by": MANAGED_BY_COMMAND,
                "update_available": True,
                "version_newer": True,
                "can_apply": False,
            }
        )

    original = dict(handlers._update_info)
    try:
        handlers._update_info.clear()
        with (
            patch.object(handlers, "_do_update_check", _check_installs_a_provider),
            patch("kiro_crew.config.KiroCrewConfig.load", return_value=cfg),
            patch("kiro_crew.platform.governance.active_update_pins", return_value=UpdatePins()),
        ):
            asyncio.run(orch._check_for_updates())
    finally:
        handlers._update_info.clear()
        handlers._update_info.update(original)

    orch._prepare_auto_update_apply.assert_not_awaited()
    orch._auto_apply_update.assert_not_awaited()
    orch._auto_apply_wheel_update.assert_not_awaited()


def test_a_wheel_route_without_an_installer_command_does_not_pause_admission(monkeypatch):
    """The command comes from the CHECK, so the route cannot vouch for it."""
    import kiro_crew.dashboard.handlers as handlers
    from kiro_crew.platform.governance import UpdatePins

    _pin_install_shape(monkeypatch, managed_by="kirocrew")
    orch = TestAutoApplyGuard._orchestrator()
    orch._prepare_auto_update_apply = AsyncMock(return_value=True)
    orch._finish_auto_update_apply = AsyncMock()
    cfg = MagicMock()
    cfg.auto_update = True

    original = dict(handlers._update_info)
    try:
        handlers._update_info.clear()
        handlers._update_info.update(
            {"update_available": True, "managed_by": "kirocrew", "remediation": None}
        )
        with (
            patch.object(handlers, "_do_update_check", new_callable=AsyncMock),
            patch("kiro_crew.config.KiroCrewConfig.load", return_value=cfg),
            patch("kiro_crew.platform.governance.active_update_pins", return_value=UpdatePins()),
        ):
            asyncio.run(orch._check_for_updates())
    finally:
        handlers._update_info.clear()
        handlers._update_info.update(original)

    orch._prepare_auto_update_apply.assert_not_awaited()
    orch._auto_apply_wheel_update.assert_not_awaited()


def test_every_unattended_apply_consults_the_effect_first():
    """A ``_prepare_auto_update_apply`` call follows an ``_auto_update_effect`` read.

    Read from the source: a new apply branch that skips the derivation would
    pause admission for an update the status surface says this install will not
    apply. ``_retry_pending_update_restart`` is the one exception: it retries the
    restart of an update already applied, and decides nothing about installing.
    """
    import ast
    import inspect
    from pathlib import Path

    from kiro_crew.slack import gateway

    cls = next(
        node
        for node in ast.walk(ast.parse(inspect.getsource(gateway)))
        if isinstance(node, ast.ClassDef) and node.name == "GatewayOrchestrator"
    )
    # The orchestrator's methods are its class body plus the ``self``-taking
    # functions its composed owners define (``gateway_runtime/*.py``).
    owners = sorted((Path(gateway.__file__).parent / "gateway_runtime").glob("*.py"))
    owner_methods = [
        node
        for path in owners
        for node in ast.parse(path.read_text(encoding="utf-8")).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.args.args
        and node.args.args[0].arg == "self"
    ]

    def _calls(fn, name):
        return [
            node.lineno
            for node in ast.walk(fn)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == name
        ]

    callers = {}
    for fn in [*cls.body, *owner_methods]:
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            prepares = _calls(fn, "_prepare_auto_update_apply")
            if prepares:
                callers[fn.name] = (prepares, _calls(fn, "_auto_update_effect"))

    assert set(callers) == {
        "_check_for_updates_legacy",
        "_check_for_updates_via_provider",
        "_retry_pending_update_restart",
    }
    # Non-vacuity: the owner walk is what reaches all three callers today.
    assert set(callers) <= {fn.name for fn in owner_methods}
    for name, (prepares, effects) in callers.items():
        if name == "_retry_pending_update_restart":
            continue
        assert effects and min(effects) < min(prepares), name


def test_a_provider_without_can_apply_still_installs(monkeypatch):
    """A provider predating ``can_apply`` keeps applying when asked, as before."""
    from kiro_crew.platform.update_capability import auto_update_effect

    legacy = MagicMock(spec=["check", "apply"])
    _pin_install_shape(monkeypatch, managed_by="kirocrew", provider=legacy)

    answer = auto_update_effect(running_version="1.0.0")
    assert (answer.effect, answer.route) == ("install", "provider")


def test_a_source_pinned_install_below_the_floor_is_not_told_to_run_kirocrew_update(
    monkeypatch,
):
    """The pin refuses `kirocrew update` too, so no badge points at it."""
    import kiro_crew.dashboard.handlers as handlers
    from kiro_crew.platform.governance import UpdatePins

    _pin_install_shape(monkeypatch, managed_by="git", blocked="pinned", floor=True)
    orch = TestAutoApplyGuard._orchestrator()
    orch._prepare_auto_update_apply = AsyncMock(return_value=True)
    original = dict(handlers._update_info)
    try:
        handlers._update_info.clear()
        handlers._update_info.update(
            {
                "update_available": False,
                "can_apply": True,
                "managed_by": "git",
                "remediation": {"kind": "command", "message": "m", "command": "kirocrew update"},
            }
        )
        with (
            patch.object(handlers, "_do_update_check", new_callable=AsyncMock),
            patch("kiro_crew.platform.governance.active_update_pins", return_value=UpdatePins()),
        ):
            asyncio.run(orch._check_for_updates())
        forced = handlers._update_info["update_available"]
    finally:
        handlers._update_info.clear()
        handlers._update_info.update(original)

    orch._prepare_auto_update_apply.assert_not_awaited()
    assert forced is False


# Every gate an unattended apply path re-checks after admission is paused must
# also be read by the route that decides whether it runs at all, or the
# derivation drifts back into pause-then-skip. The apply path's gate set is
# DERIVED from the helpers it calls, so a newly added gate fails here until the
# route reads it too — or until it is declared dynamic below, with its reason.
_GATE_HELPER_MODULES = (
    "kiro_crew.platform.update_fetch",
    "kiro_crew.platform.update_governance",
    "kiro_crew.platform.update_layout",
    "kiro_crew.platform.wheel_engine",
    "kiro_crew.platform.wheel_engine_contract",
    "kiro_crew.platform.wheel_engine_layout",
    "kiro_crew.platform.wheel_engine_build_child",
    "kiro_crew.platform.wheel_engine_transfer",
)

#: Helpers an apply path calls that the STATIC derivation cannot read, with why.
_DYNAMIC_GATES = {
    # Reads the tree as it is right now, after the fetch this apply ran.
    "commits_ahead": "measured against the commit this apply just fetched",
    "hidden_worktree_edits": "the working tree's state at apply time",
    "resolve_remote_url": "an input to update_blocked_reason, not a gate itself",
    "fetch_and_pin_upstream": "performs the isolated fetch, not a precondition the route gates on",
    "git_command_env": "builds the environment, decides nothing",
    "loggable_path": "formats a path for a log line",
    "cdn_bases": "an input to update_blocked_reason, not a gate itself",
    "wheel_update_command": "composes the installer command the check supplies",
    "respawn_executable": "resolves the restart target after a successful apply",
    "running_from_managed_venv": "read by the route; the apply trusts the route",
    "min_version": "the floor's value, already folded into the effect",
    "update_required": "the floor verdict, already folded into the effect",
    "unattended_apply_allowed": "folded into the effect, and read again: a policy can land mid-cycle",
    "check_release_version": "reads the version this cycle's check reported, never the install",
    "WheelUpdateError": "the exception class a refusal raises, not a gate",
}

_APPLY_ROUTES = [("_auto_apply_update", "_git_route"), ("_auto_apply_wheel_update", "_wheel_route")]
#: The gateway owner module both apply paths are defined in.
_APPLY_OWNER = "kiro_crew.slack.gateway_runtime.update_apply"


def _gate_helper_names() -> set[str]:
    """Every callable the gate-helper modules DEFINE (re-exports are not gates)."""
    import importlib

    names: set[str] = set()
    for module in _GATE_HELPER_MODULES:
        loaded = importlib.import_module(module)
        for name in dir(loaded):
            value = getattr(loaded, name, None)
            if callable(value) and getattr(value, "__module__", None) == module:
                names.add(name)
    return names


@pytest.mark.parametrize("apply_name, route_name", _APPLY_ROUTES)
def test_the_derivation_reads_every_static_gate_its_apply_path_reads(apply_name, route_name):
    import ast
    import importlib
    import inspect

    from kiro_crew.platform import update_capability

    def _names(module, name: str) -> set[str]:
        fn = next(
            node
            for node in ast.walk(ast.parse(inspect.getsource(module)))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
        )
        return {
            node.attr if isinstance(node, ast.Attribute) else node.id
            for node in ast.walk(fn)
            if isinstance(node, (ast.Attribute, ast.Name))
        }

    # platform_compat's two update gates: a trusted git, and a trusted system
    # binary. Named rather than derived, because that module's surface is every
    # POSIX call the product makes.
    helpers = _gate_helper_names() | {"trusted_git_bin", "trusted_system_bin"}
    applied = _names(importlib.import_module(_APPLY_OWNER), apply_name)
    if "preflight_bases" in applied:
        # The managed-venv apply runs its gates through the shared preflight.
        from kiro_crew.platform import wheel_apply

        applied |= _names(wheel_apply, "preflight_bases")
    static_gates = (applied & helpers) - set(_DYNAMIC_GATES)
    assert static_gates, "no gate helper found in the apply path — is the parse right?"
    missing = static_gates - _names(update_capability, route_name)
    assert not missing, (
        f"{apply_name} re-checks {sorted(missing)}, which {route_name} never reads: the "
        "derivation would answer 'install' for an install the apply then refuses, after "
        "admission is already paused. Read it in the route, or declare it in "
        "_DYNAMIC_GATES with its reason."
    )


class TestTheStatusFrameAnswersBeforeAnyCheck:
    """A frame served before the loop's first derivation still reports the truth.

    The modal opens on the first boot after an app update, before any check has
    run, so ``unknown`` is reserved for the one shape whose answer only git can
    give: a checkout's branch and remote.
    """

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing class autouse fixture, moved verbatim; no test in its module calls monkeypatch.undo()
    def _no_derivation_yet(self, monkeypatch):
        from kiro_crew.dashboard.handlers import updates

        monkeypatch.setattr(updates, "_auto_effect", None)
        monkeypatch.setattr(updates, "_auto_effect_task", None)
        monkeypatch.setattr(updates, "_shape_effect", None)
        return updates

    @staticmethod
    def _no_git(monkeypatch):
        def _boom(*_a, **_k):  # pragma: no cover - must not be called
            raise AssertionError("the status frame must not shell out to git")

        monkeypatch.setattr("kiro_crew.platform.update_governance._git_probe", _boom)
        # update_fetch imports the probe by name, so its binding is patched too.
        monkeypatch.setattr("kiro_crew.platform.update_fetch._git_probe", _boom)
        monkeypatch.setattr("kiro_crew.platform.update_capability.subprocess.run", _boom)

    def test_a_desktop_bundle_reports_notify(self, monkeypatch, _no_derivation_yet):
        self._no_git(monkeypatch)
        monkeypatch.setattr("kiro_crew.platform.update_capability.distribution", lambda: "dmg")
        monkeypatch.setattr("kiro_crew.platform.update_provider.resolve_provider", lambda: None)

        assert status_fields_of(_no_derivation_yet)["update_auto_effect"] == "notify"

    def test_a_managed_venv_reports_install(self, monkeypatch, _no_derivation_yet):
        self._no_git(monkeypatch)
        monkeypatch.setattr("kiro_crew.platform.update_capability.distribution", lambda: "wheel")
        monkeypatch.setattr("kiro_crew.platform.update_provider.resolve_provider", lambda: None)
        monkeypatch.setattr(
            "kiro_crew.platform.update_capability._source_checkout_root", lambda: None
        )
        monkeypatch.setattr(
            "kiro_crew.platform.update_capability._runs_from_managed_venv", lambda: True
        )
        monkeypatch.setattr(
            "kiro_crew.platform.update_capability._installer_runs_here", lambda: True
        )
        monkeypatch.setattr("kiro_crew.platform_compat.trusted_system_bin", lambda _n: "/bin/sh")
        monkeypatch.setattr("kiro_crew.platform.update_layout.cdn_bases_are_safe", lambda: True)
        monkeypatch.setattr(
            "kiro_crew.platform.update_layout.cdn_bases", lambda: ("https://a", "https://b")
        )
        monkeypatch.setattr(
            "kiro_crew.platform.update_governance.update_blocked_reason", lambda _u: ""
        )
        monkeypatch.setattr(
            "kiro_crew.platform.update_governance.update_required", lambda _v: False
        )

        assert status_fields_of(_no_derivation_yet)["update_auto_effect"] == "install"

    @pytest.mark.parametrize(
        "can_apply, floor, expected",
        [(True, False, "install"), (True, True, "mandatory"), (False, False, "notify")],
    )
    def test_a_policy_provider_is_answered_from_the_pins(
        self, monkeypatch, _no_derivation_yet, can_apply, floor, expected
    ):
        """The provider path publishes no ``can_apply``, so the field must not read it."""
        self._no_git(monkeypatch)
        monkeypatch.setattr(
            "kiro_crew.platform.update_provider.resolve_provider",
            lambda: _provider(can_apply=can_apply),
        )
        monkeypatch.setattr(
            "kiro_crew.platform.update_governance.update_required", lambda _v: floor
        )

        original = dict(_no_derivation_yet._update_info)
        try:
            _no_derivation_yet._update_info.clear()
            # A frame BEFORE any check: nothing has published can_apply yet.
            _no_derivation_yet._update_info.update(
                {"managed_by": "command", "update_available": True, "check_status": "succeeded"}
            )
            fields = status_fields_of(_no_derivation_yet)
        finally:
            _no_derivation_yet._update_info.clear()
            _no_derivation_yet._update_info.update(original)

        assert fields["update_can_apply"] is False
        assert fields["update_auto_effect"] == expected

    def test_a_checkout_is_the_one_shape_that_waits(self, monkeypatch, _no_derivation_yet):
        """Only git can report a branch and a remote, so this one stays unknown."""
        self._no_git(monkeypatch)
        monkeypatch.setattr("kiro_crew.platform.update_capability.distribution", lambda: "source")
        monkeypatch.setattr("kiro_crew.platform.update_provider.resolve_provider", lambda: None)
        monkeypatch.setattr(
            "kiro_crew.platform.update_capability._source_checkout_root",
            lambda: Path("/somewhere/checkout"),
        )

        assert status_fields_of(_no_derivation_yet)["update_auto_effect"] == "unknown"


def status_fields_of(updates_module) -> dict:
    """The fields as the status funnel serves them: primed off-loop, then read."""
    asyncio.run(updates_module.prime_status_auto_update_effect())
    return updates_module.status_update_fields()


def test_a_non_boolean_auto_update_on_disk_reads_as_the_default(tmp_path, monkeypatch):
    """A hand-edited ``"auto_update": "false"`` reads as OFF, as its owner meant.

    Stored verbatim it is truthy to the update loop, which would install on a
    host whose owner wrote the opposite, and no switch can render a string.
    """
    from kiro_crew.config.loader import KiroCrewConfig

    path = tmp_path / "config.json"
    monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: path)
    monkeypatch.setattr("kiro_crew.config.loader.config_local_path", lambda: tmp_path / "none.json")

    def _loaded(value) -> bool:
        path.write_text(json.dumps({"auto_update": value}), encoding="utf-8")
        return KiroCrewConfig.load().auto_update

    # A recognized spelling is honoured either way.
    assert _loaded("false") is False
    assert _loaded("off") is False
    assert _loaded("yes") is True
    assert _loaded(True) is True
    assert _loaded(False) is False
    # Anything else unreadable falls toward OFF: an update not applied is a
    # notification, one applied against the owner's wish is a restart. Before,
    # 0 and null read OFF too, so this keeps them OFF.
    for unreadable in (0, None, "", [], "maybe"):
        assert _loaded(unreadable) is False, unreadable
    # An absent key still defaults ON.
    path.write_text("{}", encoding="utf-8")
    assert KiroCrewConfig.load().auto_update is True


class TestTheProviderPathPublishesItsOwnCanApply:
    """``can_apply`` is the provider's answer, not the install shape's.

    The loop runs a policy ``apply_command`` when one is configured, so a frame
    reporting ``can_apply: false`` for that host is wrong for every reader that
    is not reading the effect.
    """

    @pytest.mark.parametrize("can_apply", [True, False])
    def test_a_loop_check_publishes_the_providers_verdict(self, monkeypatch, can_apply):
        import kiro_crew.dashboard.handlers as handlers
        from kiro_crew.platform.governance import UpdatePins

        provider = _provider(can_apply=can_apply)
        _pin_install_shape(monkeypatch, managed_by="kirocrew", provider=provider)
        orch = TestAutoApplyGuard._orchestrator()
        orch._prepare_auto_update_apply = AsyncMock(return_value=True)
        orch._finish_auto_update_apply = AsyncMock()
        orch._restart_after_update = AsyncMock()
        cfg = MagicMock()
        cfg.auto_update = True

        original = dict(handlers._update_info)
        try:
            handlers._update_info.clear()
            with (
                patch("kiro_crew.config.KiroCrewConfig.load", return_value=cfg),
                patch(
                    "kiro_crew.platform.governance.active_update_pins", return_value=UpdatePins()
                ),
                patch("kiro_crew.slack.gateway.respawn_executable", create=True),
            ):
                asyncio.run(orch._check_for_updates())
            fields = handlers.updates.status_update_fields()
        finally:
            handlers._update_info.clear()
            handlers._update_info.update(original)

        assert fields["update_can_apply"] is can_apply
        assert fields["update_auto_effect"] == ("install" if can_apply else "notify")
        assert provider.apply.await_count == (1 if can_apply else 0)


def test_a_pipx_install_answers_notify_without_git(monkeypatch):
    """Not a checkout, not the managed venv: the git-free path already knows."""
    from kiro_crew.dashboard.handlers import updates

    def _boom(*_a, **_k):  # pragma: no cover - must not be called
        raise AssertionError("the status frame must not shell out to git")

    monkeypatch.setattr("kiro_crew.platform.update_governance._git_probe", _boom)
    monkeypatch.setattr("kiro_crew.platform.update_fetch._git_probe", _boom)
    monkeypatch.setattr(updates, "_auto_effect", None)
    monkeypatch.setattr(updates, "_shape_effect", None)
    monkeypatch.setattr("kiro_crew.platform.update_capability.distribution", lambda: "wheel")
    monkeypatch.setattr("kiro_crew.platform.update_provider.resolve_provider", lambda: None)
    monkeypatch.setattr("kiro_crew.platform.update_capability._source_checkout_root", lambda: None)
    monkeypatch.setattr(
        "kiro_crew.platform.update_capability._runs_from_managed_venv", lambda: False
    )
    monkeypatch.setattr("kiro_crew.platform.update_capability._installer_runs_here", lambda: True)
    monkeypatch.setattr("kiro_crew.platform_compat.trusted_system_bin", lambda _n: "/bin/sh")

    assert status_fields_of(updates)["update_auto_effect"] == "notify"


def test_the_git_free_answer_is_derived_once_per_process(monkeypatch):
    from kiro_crew.dashboard.handlers import updates

    calls = {"n": 0}

    def _derive(**_kw):
        calls["n"] += 1
        return None

    monkeypatch.setattr(updates, "_auto_effect", None)
    monkeypatch.setattr(updates, "_shape_effect", None)
    monkeypatch.setattr(updates, "auto_update_effect", _derive)
    for _ in range(5):
        assert status_fields_of(updates)["update_auto_effect"] == "unknown"
    assert calls["n"] == 1


def test_the_status_reader_never_derives_on_the_loop(monkeypatch):
    """The sync reader only reads the memo; the funnel primes it off the loop."""
    from kiro_crew.dashboard.handlers import updates

    def _boom(**_kw):  # pragma: no cover - must not be called
        raise AssertionError("status_update_fields must not derive the effect itself")

    monkeypatch.setattr(updates, "_auto_effect", None)
    monkeypatch.setattr(updates, "_shape_effect", None)
    monkeypatch.setattr(updates, "auto_update_effect", _boom)
    assert updates.status_update_fields()["update_auto_effect"] == "unknown"


def test_the_status_funnel_primes_the_effect_before_reading(monkeypatch):
    from kiro_crew.dashboard import status_counts
    from kiro_crew.dashboard.handlers import updates
    from kiro_crew.platform.update_capability import AutoUpdateEffect

    monkeypatch.setattr(updates, "_auto_effect", None)
    monkeypatch.setattr(updates, "_shape_effect", None)
    monkeypatch.setattr(
        updates, "auto_update_effect", lambda **_kw: AutoUpdateEffect("notify", None, "x")
    )
    monkeypatch.setattr(status_counts, "_refresh_status_counts", AsyncMock(return_value=(0, 0)))
    state = MagicMock()
    state.status_snapshot = lambda **kw: kw

    snapshot = asyncio.run(status_counts.cached_status_snapshot(state))
    assert snapshot["update_auto_effect"] == "notify"


@pytest.mark.parametrize(
    "dist, bundled",
    [
        ("dmg", True),
        ("appimage", True),
        ("deb", True),
        ("rpm", True),
        ("nsis", True),
        ("docker", False),
        ("wheel", False),
        ("source", False),
        ("", False),
    ],
)
def test_only_a_desktop_bundle_reports_itself_bundled_by_the_app(dist, bundled):
    from kiro_crew.platform.update_capability import bundled_by_desktop_app

    assert bundled_by_desktop_app(dist) is bundled


@pytest.mark.parametrize(
    "provider_kind, effect",
    [(None, "notify"), ("apply", "install"), ("check-only", "notify")],
)
def test_a_bundled_gateway_with_a_policy_provider_says_both_facts(
    monkeypatch, provider_kind, effect
):
    """``managed_by`` reads ``command`` there, so the bundle needs its own field.

    The effect is the provider's answer, because the provider is what this
    gateway's loop runs; without one, the app's own updater owns it: notify.
    """
    from kiro_crew.dashboard.handlers import updates

    provider = None if provider_kind is None else _provider(can_apply=provider_kind == "apply")
    monkeypatch.setattr(updates, "_auto_effect", None)
    monkeypatch.setattr(updates, "_shape_effect", None)
    monkeypatch.setattr("kiro_crew.platform.update_capability.distribution", lambda: "dmg")
    monkeypatch.setattr("kiro_crew.platform.update_capability.baked_distribution", lambda: "dmg")
    monkeypatch.setattr("kiro_crew.platform.update_provider.resolve_provider", lambda: provider)
    monkeypatch.setattr("kiro_crew.platform.update_governance.update_required", lambda _v: False)
    original = dict(updates._update_info)
    try:
        updates._update_info.clear()
        if provider is not None:
            updates._update_info.update({"managed_by": "command"})
        fields = status_fields_of(updates)
    finally:
        updates._update_info.clear()
        updates._update_info.update(original)

    assert fields["update_bundled_by_app"] is True
    assert fields["update_auto_effect"] == effect


def test_a_runtime_distribution_override_cannot_claim_the_bundle(monkeypatch):
    """Only the BAKED stamp says "bundled": an env override cannot relabel it."""
    from kiro_crew.platform.update_capability import bundled_by_desktop_app

    monkeypatch.setattr("kiro_crew.platform.update_capability.baked_distribution", lambda: "")
    monkeypatch.setattr("kiro_crew.platform.update_capability.distribution", lambda: "dmg")
    assert bundled_by_desktop_app() is False
