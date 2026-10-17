"""Tests for the service controller's per-platform dispatch.

``current_platform()`` routing each verb to the Linux or macOS module, the
exit code an unsupported platform gets, the restart hint that matches how the
service is installed, and the per-platform installed signal.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from service_helpers import (  # noqa: F401 -- fixtures are found by name
    _DEAD,
    _NO_BUS,
    _RUNNING,
    _clear_sudo_user,
    _fake_systemctl,
    _no_host_env_file,
    _no_host_unit_files,
    _restart_settle_without_waiting,
)

from kiro_crew.service import controller
from kiro_crew.service.common import RESTART_REFUSED, SERVICE_NAME, Platform


class TestControllerDispatch:
    def test_install_unsupported_returns_2(self):
        from kiro_crew.service import controller

        with patch(
            "kiro_crew.service.controller.current_platform",
            return_value=Platform.UNSUPPORTED,
        ):
            rc = controller.install_service()
        assert rc == 2

    def test_install_systemd_returns_0(self):
        from kiro_crew.service import controller
        from kiro_crew.service import linux as svc_linux

        with (
            patch(
                "kiro_crew.service.controller.current_platform",
                return_value=Platform.SYSTEMD,
            ),
            patch.object(svc_linux, "install") as mock_install,
        ):
            rc = controller.install_service()
        assert rc == 0
        mock_install.assert_called_once()

    def test_install_systemd_prints_linger_warning(self, capsys):
        from kiro_crew.service import apparmor, controller
        from kiro_crew.service import linux as svc_linux

        with (
            patch(
                "kiro_crew.service.controller.current_platform",
                return_value=Platform.SYSTEMD,
            ),
            patch.object(svc_linux, "install", return_value=apparmor.ProfileOutcome(False, "")),
            patch.object(
                svc_linux,
                "linger_warning_for_service_account",
                return_value="linger is off for tester: ...",
            ),
        ):
            rc = controller.install_service()
        assert rc == 0
        assert "linger is off for tester" in capsys.readouterr().out

    def test_install_systemd_silent_when_linger_ok(self, capsys):
        from kiro_crew.service import apparmor, controller
        from kiro_crew.service import linux as svc_linux

        with (
            patch(
                "kiro_crew.service.controller.current_platform",
                return_value=Platform.SYSTEMD,
            ),
            patch.object(svc_linux, "install", return_value=apparmor.ProfileOutcome(False, "")),
            patch.object(svc_linux, "linger_warning_for_service_account", return_value=None),
        ):
            rc = controller.install_service()
        assert rc == 0
        assert "linger" not in capsys.readouterr().out

    def test_uninstall_unsupported_returns_2(self):
        from kiro_crew.service import controller

        with patch(
            "kiro_crew.service.controller.current_platform",
            return_value=Platform.UNSUPPORTED,
        ):
            rc = controller.uninstall_service()
        assert rc == 2

    def test_is_service_active_unsupported_returns_false(self):
        from kiro_crew.service import controller

        with patch(
            "kiro_crew.service.controller.current_platform",
            return_value=Platform.UNSUPPORTED,
        ):
            assert controller.is_service_active() is False

    def test_stop_service_returns_false_when_inactive(self):
        from kiro_crew.service import controller
        from kiro_crew.service import linux as svc_linux

        with (
            patch(
                "kiro_crew.service.controller.current_platform",
                return_value=Platform.SYSTEMD,
            ),
            patch.object(svc_linux, "is_active", return_value=False),
            patch.object(svc_linux, "stop") as mock_stop,
        ):
            assert controller.stop_service() is False
        mock_stop.assert_not_called()

    def test_stop_service_returns_true_when_active_systemd(self):
        from kiro_crew.service import controller
        from kiro_crew.service import linux as svc_linux

        with (
            patch(
                "kiro_crew.service.controller.current_platform",
                return_value=Platform.SYSTEMD,
            ),
            patch.object(svc_linux, "is_active", return_value=True),
            patch.object(svc_linux, "stop") as mock_stop,
        ):
            assert controller.stop_service() is True
        mock_stop.assert_called_once()

    def test_stop_service_routes_to_macos(self):
        from kiro_crew.service import controller
        from kiro_crew.service import macos as svc_macos

        with (
            patch(
                "kiro_crew.service.controller.current_platform",
                return_value=Platform.LAUNCHD,
            ),
            patch.object(svc_macos, "is_active", return_value=True),
            patch.object(svc_macos, "stop", return_value=True) as mock_stop,
        ):
            assert controller.stop_service() is True
        mock_stop.assert_called_once()

    def test_stop_service_returns_false_when_macos_has_nothing_loaded(self):
        from kiro_crew.service import controller
        from kiro_crew.service import macos as svc_macos

        with (
            patch(
                "kiro_crew.service.controller.current_platform",
                return_value=Platform.LAUNCHD,
            ),
            patch.object(svc_macos, "stop", return_value=False) as mock_stop,
        ):
            assert controller.stop_service() is False
        mock_stop.assert_called_once()

    def test_stop_service_unsupported_returns_false(self):
        from kiro_crew.service import controller

        with patch(
            "kiro_crew.service.controller.current_platform",
            return_value=Platform.UNSUPPORTED,
        ):
            assert controller.stop_service() is False

    def test_restart_service_returns_false_when_inactive(self):
        # Same behavior as stop_service: the controller should refuse to
        # restart an inactive service rather than masking the state issue.
        # Callers fall back to the foreground-gateway path on False.
        from kiro_crew.service import controller
        from kiro_crew.service import linux as svc_linux

        with (
            patch(
                "kiro_crew.service.controller.current_platform",
                return_value=Platform.SYSTEMD,
            ),
            patch.object(svc_linux, "is_active", return_value=False),
            patch.object(svc_linux, "restart") as mock_restart,
        ):
            report = controller.restart_service()
        assert not report and report.attempted is False
        mock_restart.assert_not_called()

    def test_restart_service_returns_true_when_active_systemd(self):
        from kiro_crew.service import controller
        from kiro_crew.service import linux as svc_linux
        from kiro_crew.service.common import RestartReport, ScopeRestart

        restarted = RestartReport((ScopeRestart("system", True),))
        with (
            patch(
                "kiro_crew.service.controller.current_platform",
                return_value=Platform.SYSTEMD,
            ),
            patch.object(svc_linux, "is_active", return_value=True),
            patch.object(svc_linux, "restart", return_value=restarted) as mock_restart,
        ):
            report = controller.restart_service()
        assert report and report.ok is True
        mock_restart.assert_called_once()

    def test_restart_service_returns_false_when_systemd_restart_fails(self):
        # The core false-success bug: an unprivileged/failed `systemctl
        # restart` exits non-zero; restart_service() must not read as success
        # regardless (it must carry restart()'s report), or the CLI prints a
        # bogus success. The controller must propagate the restart outcome so
        # the caller diagnoses it instead of assuming the service manager
        # handled it.
        from kiro_crew.service import controller
        from kiro_crew.service import linux as svc_linux
        from kiro_crew.service.common import RestartReport, ScopeRestart

        refused = RestartReport(
            (
                ScopeRestart(
                    "system",
                    False,
                    reason="the system manager refused the restart: Interactive authentication required",
                    kind=RESTART_REFUSED,
                    hint="sudo systemctl restart kirocrew",
                ),
            )
        )
        with (
            patch(
                "kiro_crew.service.controller.current_platform",
                return_value=Platform.SYSTEMD,
            ),
            patch.object(svc_linux, "is_active", return_value=True),
            patch.object(svc_linux, "restart", return_value=refused) as mock_restart,
        ):
            report = controller.restart_service()
        assert not report and report.attempted is True
        assert report.failures == refused.outcomes
        mock_restart.assert_called_once()

    def test_a_mixed_report_keeps_the_restarted_scope_apart_from_the_failed_one(self):
        # A unit running in BOTH scopes (a stale crash-looping system unit
        # beside the working per-user one): one scope restarts, the other does
        # not. The report is not ok — a scope still needs a hand — but it must
        # let the CLI say which scope restarted, or the operator reads "not
        # restarted" about the gateway they use.
        from kiro_crew.service.common import RESTART_NOT_UP, RestartReport, ScopeRestart

        user_ok = ScopeRestart("user", True)
        system_down = ScopeRestart(
            "system",
            False,
            reason="kirocrew.service is activating (auto-restart) (last result: exit-code)",
            kind=RESTART_NOT_UP,
            hint="sudo journalctl -u kirocrew.service -n 50 --no-pager",
        )
        mixed = RestartReport((system_down, user_ok))
        assert mixed.attempted is True
        assert not mixed and mixed.ok is False
        assert mixed.restarted == (user_ok,)
        assert mixed.failures == (system_down,)
        assert RestartReport((user_ok,)).restarted == (user_ok,)
        assert RestartReport().restarted == ()

    def test_restart_service_routes_to_macos(self):
        from kiro_crew.service import controller
        from kiro_crew.service import macos as svc_macos

        with (
            patch(
                "kiro_crew.service.controller.current_platform",
                return_value=Platform.LAUNCHD,
            ),
            patch.object(svc_macos, "is_loaded", return_value=True),
            patch.object(svc_macos, "restart", return_value=True) as mock_restart,
        ):
            report = controller.restart_service()
        assert report and report.ok is True
        assert [o.scope for o in report.outcomes] == ["launchd"]
        mock_restart.assert_called_once()

    def test_restart_service_returns_false_when_macos_restart_fails(self):
        from kiro_crew.service import controller
        from kiro_crew.service import macos as svc_macos

        with (
            patch(
                "kiro_crew.service.controller.current_platform",
                return_value=Platform.LAUNCHD,
            ),
            patch.object(svc_macos, "is_loaded", return_value=True),
            patch.object(svc_macos, "restart", return_value=False) as mock_restart,
        ):
            report = controller.restart_service()
        assert not report and report.attempted is True
        # A launchctl refusal carries the by-hand recovery pair, never the
        # circular `kirocrew restart`.
        (failure,) = report.failures
        assert failure.kind == RESTART_REFUSED
        assert "launchctl bootout" in failure.hint and "launchctl bootstrap" in failure.hint
        mock_restart.assert_called_once()

    def test_restart_service_returns_false_when_macos_inactive(self):
        from kiro_crew.service import controller
        from kiro_crew.service import macos as svc_macos

        with (
            patch(
                "kiro_crew.service.controller.current_platform",
                return_value=Platform.LAUNCHD,
            ),
            patch.object(svc_macos, "is_loaded", return_value=False),
            patch.object(svc_macos, "restart") as mock_restart,
        ):
            report = controller.restart_service()
        assert not report and report.attempted is False
        mock_restart.assert_not_called()

    def test_restart_service_unsupported_returns_false(self):
        from kiro_crew.service import controller

        with patch(
            "kiro_crew.service.controller.current_platform",
            return_value=Platform.UNSUPPORTED,
        ):
            report = controller.restart_service()
        assert not report and report.attempted is False

    def test_manual_restart_hint_systemd_names_sudo_systemctl(self):
        # The hint is printed precisely when the CLI restart path just failed
        # for privileges, so it must name the privileged command — never a
        # circular "kirocrew restart".
        from kiro_crew.service import controller

        with (
            patch(
                "kiro_crew.service.controller.current_platform",
                return_value=Platform.SYSTEMD,
            ),
            patch(
                "kiro_crew.service.common.current_platform",
                return_value=Platform.SYSTEMD,
            ),
        ):
            hint = controller.manual_restart_hint()
        assert hint == "sudo systemctl restart kirocrew"

    def test_manual_restart_hint_systemd_is_the_system_command_even_with_no_unit_file(
        self, monkeypatch, tmp_path
    ):
        # `restart_command_hint()` defers to the CLI when neither unit file
        # exists; the MANUAL hint must not, because `kirocrew restart` is the
        # command that just failed. The per-scope report carries the user-scope
        # command for a refused user unit, so this function has one answer.
        from kiro_crew.service import common as svc_common
        from kiro_crew.service import controller
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setattr(svc_linux, "UNIT_PATH", tmp_path / "absent.service")
        monkeypatch.setattr(svc_linux, "user_unit_file_path", lambda: tmp_path / "nope.service")
        with (
            patch(
                "kiro_crew.service.controller.current_platform",
                return_value=Platform.SYSTEMD,
            ),
            patch(
                "kiro_crew.service.common.current_platform",
                return_value=Platform.SYSTEMD,
            ),
        ):
            hint = controller.manual_restart_hint()
        assert hint == "sudo systemctl restart kirocrew"
        assert svc_common.restart_command_hint() == "kirocrew restart"

    def test_manual_restart_hint_launchd_names_a_recovery_pair(self):
        # Not kickstart: macos.restart() already ran kickstart and it was
        # refused, so the hint must be a different mechanism (bootout +
        # bootstrap from the installed plist), not a retry of the failure.
        from kiro_crew.service import controller
        from kiro_crew.service import macos as svc_macos
        from kiro_crew.service.common import LAUNCHD_LABEL

        with (
            patch(
                "kiro_crew.service.controller.current_platform",
                return_value=Platform.LAUNCHD,
            ),
            patch("kiro_crew.service.controller.os.getuid", return_value=501, create=True),
        ):
            hint = controller.manual_restart_hint()
        assert "kickstart" not in hint
        assert f"launchctl bootout gui/501/{LAUNCHD_LABEL}" in hint
        assert f'launchctl bootstrap gui/501 "{svc_macos.PLIST_PATH}"' in hint

    def test_manual_restart_hint_never_circular(self):
        # Whatever the platform, the hint must not send the operator back into
        # the command that just failed.
        from kiro_crew.service import controller

        for plat in (Platform.SYSTEMD, Platform.LAUNCHD, Platform.UNSUPPORTED):
            with (
                patch(
                    "kiro_crew.service.controller.current_platform",
                    return_value=plat,
                ),
                patch(
                    "kiro_crew.service.common.current_platform",
                    return_value=plat,
                ),
            ):
                assert controller.manual_restart_hint() != "kirocrew restart"

    def test_install_systemd_handles_install_error(self, capsys):
        """If linux.install raises ServiceInstallError, controller catches it,
        prints to stderr, and returns 1 — not propagating the exception."""
        from kiro_crew.service import controller
        from kiro_crew.service import linux as svc_linux

        with (
            patch(
                "kiro_crew.service.controller.current_platform",
                return_value=Platform.SYSTEMD,
            ),
            patch.object(
                svc_linux,
                "install",
                side_effect=svc_linux.ServiceInstallError("simulated failure"),
            ),
        ):
            rc = controller.install_service()
        captured = capsys.readouterr()
        assert rc == 1
        assert "simulated failure" in captured.err

    def test_install_routes_to_macos(self, capsys):
        from kiro_crew.service import controller
        from kiro_crew.service import macos as svc_macos

        with (
            patch(
                "kiro_crew.service.controller.current_platform",
                return_value=Platform.LAUNCHD,
            ),
            patch.object(svc_macos, "install") as mock_install,
        ):
            rc = controller.install_service()
        assert rc == 0
        mock_install.assert_called_once()
        # User-facing success summary references the plist path so the user
        # knows where the agent lives.
        captured = capsys.readouterr()
        assert "plist:" in captured.out

    def test_uninstall_routes_to_systemd(self):
        from kiro_crew.service import controller
        from kiro_crew.service import linux as svc_linux

        report = svc_linux.UninstallReport("removed (/etc/x.service)", "not installed")
        with (
            patch(
                "kiro_crew.service.controller.current_platform",
                return_value=Platform.SYSTEMD,
            ),
            patch.object(svc_linux, "uninstall", return_value=report) as mock_un,
        ):
            rc = controller.uninstall_service()
        assert rc == 0
        mock_un.assert_called_once()

    def test_uninstall_systemd_handles_service_install_error(self, capsys):
        """uninstall() needs root to remove the root-owned unit, so it can raise
        ServiceInstallError on a non-root host without sudo. The controller must
        catch it and return non-zero, not let a traceback escape."""
        from kiro_crew.service import controller
        from kiro_crew.service import linux as svc_linux

        with (
            patch(
                "kiro_crew.service.controller.current_platform",
                return_value=Platform.SYSTEMD,
            ),
            patch.object(
                svc_linux, "uninstall", side_effect=svc_linux.ServiceInstallError("needs sudo")
            ),
        ):
            rc = controller.uninstall_service()
        assert rc == 1
        assert "needs sudo" in capsys.readouterr().err

    def test_uninstall_routes_to_macos(self):
        from kiro_crew.service import controller
        from kiro_crew.service import macos as svc_macos

        with (
            patch(
                "kiro_crew.service.controller.current_platform",
                return_value=Platform.LAUNCHD,
            ),
            patch.object(svc_macos, "uninstall") as mock_un,
        ):
            rc = controller.uninstall_service()
        assert rc == 0
        mock_un.assert_called_once()

    def test_status_routes_to_systemd_active(self, capsys):
        """status() returns 0 when the unit is UP, prints the systemctl output."""
        from kiro_crew.service import controller
        from kiro_crew.service import linux as svc_linux

        with (
            patch(
                "kiro_crew.service.controller.current_platform",
                return_value=Platform.SYSTEMD,
            ),
            patch.object(svc_linux, "status", return_value="● kirocrew.service\n"),
            patch.object(svc_linux, "is_up", return_value=True),
        ):
            rc = controller.service_status()
        assert rc == 0
        assert "kirocrew.service" in capsys.readouterr().out

    def test_status_routes_to_systemd_inactive_returns_1(self):
        from kiro_crew.service import controller
        from kiro_crew.service import linux as svc_linux

        with (
            patch(
                "kiro_crew.service.controller.current_platform",
                return_value=Platform.SYSTEMD,
            ),
            patch.object(svc_linux, "status", return_value=""),
            patch.object(svc_linux, "is_up", return_value=False),
        ):
            rc = controller.service_status()
        assert rc == 1

    def test_status_exit_code_is_health_not_reach(self, capsys):
        # The two predicates part on a crash loop: `is_active()` (reach — the
        # scope `stop` / `restart` must address) is True for a unit in
        # `activating (auto-restart)`, and the exit code must NOT follow it —
        # a script gating on `kirocrew service status` would read a gateway
        # that never started as healthy. The headline still prints the state.
        from kiro_crew.service import controller
        from kiro_crew.service import linux as svc_linux

        looping = {"ActiveState": "activating", "SubState": "auto-restart"}
        run = _fake_systemctl(system=None, user=looping)
        with (
            patch(
                "kiro_crew.service.controller.current_platform",
                return_value=Platform.SYSTEMD,
            ),
            patch("kiro_crew.service.linux.subprocess.run", side_effect=run),
        ):
            assert svc_linux.is_active() is True
            assert svc_linux.is_up() is False
            rc = controller.service_status()
        assert rc == 1
        assert "user scope: activating (auto-restart)" in capsys.readouterr().out

    def test_status_routes_to_macos_active(self, capsys):
        from kiro_crew.service import controller
        from kiro_crew.service import macos as svc_macos

        with (
            patch(
                "kiro_crew.service.controller.current_platform",
                return_value=Platform.LAUNCHD,
            ),
            patch.object(svc_macos, "status", return_value='"PID" = 1234;\n'),
            patch.object(svc_macos, "is_active", return_value=True),
        ):
            rc = controller.service_status()
        assert rc == 0
        assert "PID" in capsys.readouterr().out

    def test_status_routes_to_macos_inactive_returns_1(self):
        from kiro_crew.service import controller
        from kiro_crew.service import macos as svc_macos

        with (
            patch(
                "kiro_crew.service.controller.current_platform",
                return_value=Platform.LAUNCHD,
            ),
            patch.object(svc_macos, "status", return_value=""),
            patch.object(svc_macos, "is_active", return_value=False),
        ):
            rc = controller.service_status()
        assert rc == 1

    def test_status_unsupported_returns_2(self):
        from kiro_crew.service import controller

        with patch(
            "kiro_crew.service.controller.current_platform",
            return_value=Platform.UNSUPPORTED,
        ):
            rc = controller.service_status()
        assert rc == 2

    def test_is_service_active_systemd_routes(self):
        from kiro_crew.service import controller
        from kiro_crew.service import linux as svc_linux

        with (
            patch(
                "kiro_crew.service.controller.current_platform",
                return_value=Platform.SYSTEMD,
            ),
            patch.object(svc_linux, "is_active", return_value=True),
        ):
            assert controller.is_service_active() is True

    def test_is_service_active_macos_routes(self):
        from kiro_crew.service import controller
        from kiro_crew.service import macos as svc_macos

        with (
            patch(
                "kiro_crew.service.controller.current_platform",
                return_value=Platform.LAUNCHD,
            ),
            patch.object(svc_macos, "is_active", return_value=True),
        ):
            assert controller.is_service_active() is True

    def test_service_alias_holder_routes_to_linux_on_systemd(self):
        from kiro_crew.service import controller
        from kiro_crew.service import linux as svc_linux

        holder = svc_linux.AliasHolder("system", "other.service")
        with (
            patch(
                "kiro_crew.service.controller.current_platform",
                return_value=Platform.SYSTEMD,
            ),
            patch.object(svc_linux, "alias_holder", return_value=holder) as mock_alias,
        ):
            assert controller.service_alias_holder() is holder
        mock_alias.assert_called_once()

    def test_service_alias_holder_is_none_on_launchd(self):
        """launchd has no alias concept for labels — macOS always answers None.
        Patch `linux.alias_holder` and assert it is never reached: the platform
        gate must short-circuit, not merely return what real linux would answer
        on a host that happens to run our own unit (the M5 mutation)."""
        from kiro_crew.service import controller
        from kiro_crew.service import linux as svc_linux

        with (
            patch(
                "kiro_crew.service.controller.current_platform",
                return_value=Platform.LAUNCHD,
            ),
            patch.object(svc_linux, "alias_holder") as mock_alias,
        ):
            assert controller.service_alias_holder() is None
        mock_alias.assert_not_called()

    def test_service_alias_holder_is_none_on_unsupported(self):
        from kiro_crew.service import controller
        from kiro_crew.service import linux as svc_linux

        with (
            patch(
                "kiro_crew.service.controller.current_platform",
                return_value=Platform.UNSUPPORTED,
            ),
            patch.object(svc_linux, "alias_holder") as mock_alias,
        ):
            assert controller.service_alias_holder() is None
        mock_alias.assert_not_called()


class TestRestartCommandHint:
    """`restart_command_hint` returns a command that matches how the
    service is actually installed.

    The bug was the update path and the Slack restart-failure hint both
    hardcoding ``systemctl --user restart kirocrew``, which fails on the
    system-level systemd unit. The helper centralises the correct command
    per platform — and, on systemd, per SCOPE: decided by which unit file
    exists (two stats, no spawn), because the same two callers print the
    system command's own dead end (`Unit kirocrew.service not found`) on a host
    whose only unit is the SELinux remedy's per-user one.
    """

    @pytest.fixture
    def unit_files(self, monkeypatch, tmp_path):
        """Both unit-file seams — the Linux module's, the one binding the hint
        reads and every other test patches — pointed at a temp dir; tests create
        the files."""
        from kiro_crew.service import linux as svc_linux

        system = tmp_path / "etc" / "kirocrew.service"
        user = tmp_path / "home" / ".config" / "systemd" / "user" / "kirocrew.service"
        monkeypatch.setattr(svc_linux, "UNIT_PATH", system)
        monkeypatch.setattr(svc_linux, "user_unit_file_path", lambda: user)
        return system, user

    @staticmethod
    def _touch(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[Unit]\n", encoding="utf-8")

    def test_systemd_returns_sudo_systemctl(self, monkeypatch, unit_files):
        from kiro_crew.service import common as svc_common

        system, _user = unit_files
        self._touch(system)
        monkeypatch.setattr(svc_common, "current_platform", lambda: Platform.SYSTEMD)
        assert svc_common.restart_command_hint() == f"sudo systemctl restart {SERVICE_NAME}"

    def test_systemd_with_only_the_user_unit_returns_the_user_manager_command(
        self, monkeypatch, unit_files
    ):
        # The SELinux remedy's layout: no system unit file, the per-user one at
        # the remedy's location. `sudo systemctl restart kirocrew` answers
        # "Unit kirocrew.service not found" there; the account's own manager is
        # the one to address.
        from kiro_crew.service import common as svc_common

        _system, user = unit_files
        self._touch(user)
        monkeypatch.setattr(svc_common, "current_platform", lambda: Platform.SYSTEMD)
        assert svc_common.restart_command_hint() == f"systemctl --user restart {SERVICE_NAME}"

    def test_systemd_with_both_unit_files_defers_to_the_service_aware_cli(
        self, monkeypatch, unit_files
    ):
        # A stale system unit beside the remedy's user unit: a file says nothing
        # about which scope RUNS the gateway, and either systemctl command could
        # restart a dead unit or start a competitor. `kirocrew restart` reads
        # both managers and acts on the running scope.
        from kiro_crew.service import common as svc_common

        system, user = unit_files
        self._touch(system)
        self._touch(user)
        monkeypatch.setattr(svc_common, "current_platform", lambda: Platform.SYSTEMD)
        assert svc_common.restart_command_hint() == "kirocrew restart"

    def test_systemd_with_no_unit_file_defers_to_the_service_aware_cli(
        self, monkeypatch, unit_files
    ):
        # A foreground `kirocrew gateway` on a systemd host: neither unit exists,
        # so neither systemctl command can restart it — the CLI resolves it.
        from kiro_crew.service import common as svc_common

        monkeypatch.setattr(svc_common, "current_platform", lambda: Platform.SYSTEMD)
        assert svc_common.restart_command_hint() == "kirocrew restart"

    def test_launchd_returns_service_aware_cli(self, monkeypatch, unit_files):
        from kiro_crew.service import common as svc_common

        monkeypatch.setattr(svc_common, "current_platform", lambda: Platform.LAUNCHD)
        assert svc_common.restart_command_hint() == "kirocrew restart"

    def test_unsupported_returns_service_aware_cli(self, monkeypatch, unit_files):
        from kiro_crew.service import common as svc_common

        monkeypatch.setattr(svc_common, "current_platform", lambda: Platform.UNSUPPORTED)
        assert svc_common.restart_command_hint() == "kirocrew restart"

    def test_never_returns_the_user_scope_command_without_a_user_unit(
        self, monkeypatch, unit_files
    ):
        """Regression: the `systemctl --user` string that was filed against
        (AL2, no per-user manager) must not come back for a system unit or for
        no unit at all — only a per-user unit file earns it."""
        from kiro_crew.service import common as svc_common

        system, _user = unit_files
        for present in (False, True):
            if present:
                self._touch(system)
            for platform in Platform:
                monkeypatch.setattr(svc_common, "current_platform", lambda p=platform: p)
                assert "systemctl --user" not in svc_common.restart_command_hint()

    def test_the_hint_reads_the_linux_modules_unit_path_binding(self, monkeypatch, tmp_path):
        """One binding, not a copy: patching `linux.UNIT_PATH` — the seam every
        other test of the module uses — is what the hint sees."""
        from kiro_crew.service import common as svc_common
        from kiro_crew.service import linux as svc_linux

        system = tmp_path / "kirocrew.service"
        self._touch(system)
        monkeypatch.setattr(svc_linux, "UNIT_PATH", system)
        monkeypatch.setattr(svc_linux, "user_unit_file_path", lambda: tmp_path / "nope.service")
        monkeypatch.setattr(svc_common, "current_platform", lambda: Platform.SYSTEMD)
        assert svc_common.restart_command_hint() == f"sudo systemctl restart {SERVICE_NAME}"
        system.unlink()
        assert svc_common.restart_command_hint() == "kirocrew restart"

    def test_user_unit_file_path_is_the_remedys_location(self, monkeypatch, tmp_path):
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setattr(svc_linux.Path, "home", classmethod(lambda cls: tmp_path))
        assert svc_linux.user_unit_file_path() == (
            tmp_path / ".config" / "systemd" / "user" / "kirocrew.service"
        )


class TestSandboxProfileControllerDispatch:
    """Non-Linux hosts get a clean no-op, not an error."""

    def test_install_is_a_noop_off_systemd(self, capsys):
        from kiro_crew.service import controller

        with patch.object(controller, "current_platform", return_value=Platform.LAUNCHD):
            rc = controller.install_launcher_profile(None)

        assert rc == 0
        assert "Linux-only" in capsys.readouterr().out

    def test_status_is_a_noop_off_systemd(self, capsys):
        from kiro_crew.service import controller

        with patch.object(controller, "current_platform", return_value=Platform.LAUNCHD):
            rc = controller.sandbox_profile_status(None)

        assert rc == 0
        assert "does not restrict" in capsys.readouterr().out

    def test_install_returns_nonzero_when_the_outcome_is_not_ok(self, capsys):
        from kiro_crew.service import apparmor, controller, linux

        with (
            patch.object(controller, "current_platform", return_value=Platform.SYSTEMD),
            patch.object(
                linux,
                "install_launcher_profile",
                return_value=apparmor.ProfileOutcome(False, "nope", ok=False),
            ),
        ):
            rc = controller.install_launcher_profile("/x")

        assert rc == 1
        assert "⚠️" in capsys.readouterr().out

    def test_status_exit_code_is_the_answer(self):
        from kiro_crew.service import apparmor, controller

        with (
            patch.object(controller, "current_platform", return_value=Platform.SYSTEMD),
            patch.object(apparmor, "launcher_status", return_value=(False, "not covered")),
        ):
            assert controller.sandbox_profile_status(None) == 1

        with (
            patch.object(controller, "current_platform", return_value=Platform.SYSTEMD),
            patch.object(apparmor, "launcher_status", return_value=(True, "covered")),
        ):
            assert controller.sandbox_profile_status(None) == 0


class TestInstalledUnitPath:
    """Presence of the definition file is the installed signal, per platform —
    and on Linux the per-user unit the account's own manager has loaded counts."""

    def test_systemd_reports_the_unit_when_present(self, monkeypatch, tmp_path):
        unit = tmp_path / "kirocrew.service"
        unit.write_text("[Unit]\n", encoding="utf-8")
        monkeypatch.setattr(controller, "current_platform", lambda: Platform.SYSTEMD)
        monkeypatch.setattr(controller.linux, "UNIT_PATH", unit)
        run = _fake_systemctl(system=_RUNNING, user=None)
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            assert controller.installed_unit_path() == unit
        # A system unit file answers by itself: no spawn at all.
        assert run.calls == []

    def test_launchd_reports_the_plist_when_present(self, monkeypatch, tmp_path):
        plist = tmp_path / "dev.kirocrew.gateway.plist"
        plist.write_text("<plist/>", encoding="utf-8")
        monkeypatch.setattr(controller, "current_platform", lambda: Platform.LAUNCHD)
        monkeypatch.setattr(controller.macos, "PLIST_PATH", plist)
        assert controller.installed_unit_path() == plist

    def test_absent_definition_is_not_installed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(controller, "current_platform", lambda: Platform.SYSTEMD)
        monkeypatch.setattr(controller.linux, "UNIT_PATH", tmp_path / "nope.service")
        run = _fake_systemctl(system=None, user=None)
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            assert controller.installed_unit_path() is None
        # Only the user manager was asked, and without sudo.
        assert [c[:3] for c in run.calls] == [["systemctl", "--user", "show"]], run.calls

    def test_user_unit_the_manager_has_loaded_is_the_installed_definition(
        self, monkeypatch, tmp_path
    ):
        fragment = tmp_path / ".config" / "systemd" / "user" / "kirocrew.service"
        fragment.parent.mkdir(parents=True)
        fragment.write_text("[Unit]\n", encoding="utf-8")
        monkeypatch.setattr(controller, "current_platform", lambda: Platform.SYSTEMD)
        monkeypatch.setattr(controller.linux, "UNIT_PATH", tmp_path / "nope.service")
        # A stopped user unit is still an installed definition (the check is
        # about what a service would inherit, not whether it runs right now).
        run = _fake_systemctl(system=None, user=_DEAD, user_fragment=str(fragment))
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            assert controller.installed_unit_path() == fragment

    @pytest.mark.parametrize(
        "kwargs",
        [
            # The manager does not have the unit loaded (a file dropped without a
            # daemon-reload is not a definition a gateway can run under).
            dict(user=None),
            # A mask: the fragment systemd reports is /dev/null, no definition.
            dict(user=_DEAD, user_load="masked", user_fragment="/dev/null"),
            # An alias resolves to another unit's file — not ours to read.
            dict(user=_RUNNING, user_id="shared.service", user_fragment="/tmp/shared.service"),
            # Unreachable user scope: nothing this account can point doctor at.
            dict(user_bus_error=_NO_BUS),
        ],
        ids=["not-found", "masked", "alias", "unreachable"],
    )
    def test_user_scope_without_a_readable_definition_is_not_installed(
        self, monkeypatch, tmp_path, kwargs
    ):
        monkeypatch.setattr(controller, "current_platform", lambda: Platform.SYSTEMD)
        monkeypatch.setattr(controller.linux, "UNIT_PATH", tmp_path / "nope.service")
        run = _fake_systemctl(system=None, **kwargs)
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            assert controller.installed_unit_path() is None

    def test_user_unit_whose_file_is_gone_is_not_installed(self, monkeypatch, tmp_path):
        # The manager still lists a fragment the operator has since deleted:
        # there is no file to read a marker or an environment from.
        monkeypatch.setattr(controller, "current_platform", lambda: Platform.SYSTEMD)
        monkeypatch.setattr(controller.linux, "UNIT_PATH", tmp_path / "nope.service")
        gone = tmp_path / "removed" / "kirocrew.service"
        run = _fake_systemctl(system=None, user=_RUNNING, user_fragment=str(gone))
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            assert controller.installed_unit_path() is None

    def test_unsupported_platform_is_not_installed(self, monkeypatch):
        monkeypatch.setattr(controller, "current_platform", lambda: Platform.UNSUPPORTED)
        assert controller.installed_unit_path() is None
