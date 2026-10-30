"""Tests for the Linux service uninstall across both systemd scopes.

What uninstall removes in each scope and what it reports, and how a rejected
daemon-reload or a refused teardown step leaves the scope unfinished with the
unit file in place.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest
from service_helpers import (  # noqa: F401 -- fixtures are found by name
    _DEAD,
    _NO_BUS,
    _OURS_UNIT_TEXT,
    _RUNNING,
    _UNIT,
    _clear_sudo_user,
    _fake_systemctl,
    _no_host_env_file,
    _no_host_unit_files,
    _restart_settle_without_waiting,
)

from kiro_crew.service.common import Platform


class TestLinuxServiceScopesUninstall:
    """``status`` / ``is_active`` / ``uninstall`` (and the ``stop`` / ``restart``
    that ``is_active`` gates) see BOTH systemd scopes and name the one they
    report on, so a gateway running as the SELinux remedy's user unit is never
    reported as a dead system unit.

    The systemctl runner is mocked in every test: the systemd user manager is
    host state, and a real ``systemctl --user`` from here would touch the
    operator's own units.
    """

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse fixture on the shared monkeypatch, moved verbatim; tracked for a de-flake pass
    def _not_root(self, monkeypatch, tmp_path):
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setattr(svc_linux.os, "geteuid", lambda: 1000, raising=False)
        monkeypatch.setenv("USER", "tester")
        # No system unit file unless a test writes one: uninstall's system-scope
        # probe is a stat of this path. The per-user unit's own path is the
        # remedy's layout under tmp_path, so a test that writes there owns the
        # file by path and one that writes elsewhere owns it by the marker.
        monkeypatch.setattr(svc_linux, "UNIT_PATH", tmp_path / "absent" / _UNIT)
        monkeypatch.setattr(
            svc_linux,
            "user_unit_file_path",
            lambda: tmp_path / ".config" / "systemd" / "user" / _UNIT,
        )

    @staticmethod
    def _user_calls(run):
        return [c for c in run.calls if "--user" in c]

    # -- uninstall ---------------------------------------------------------

    def test_uninstall_removes_a_user_unit_via_the_user_manager(self, tmp_path):
        from kiro_crew.service import linux as svc_linux

        fragment = tmp_path / ".config" / "systemd" / "user" / _UNIT
        fragment.parent.mkdir(parents=True)
        fragment.write_text(_OURS_UNIT_TEXT, encoding="utf-8")
        run = _fake_systemctl(system=None, user=_RUNNING, user_fragment=str(fragment))
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.uninstall()

        assert report.system == "not installed"
        assert report.user == f"removed ({fragment})"
        assert not fragment.exists()
        assert ["systemctl", "--user", "stop", _UNIT] in run.calls
        assert ["systemctl", "--user", "disable", _UNIT] in run.calls
        assert ["systemctl", "--user", "daemon-reload"] in run.calls
        assert all("sudo" not in c for c in run.calls), run.calls

    def test_uninstall_with_nothing_installed_removes_nothing_and_says_so(self, capsys):
        from kiro_crew.service import controller
        from kiro_crew.service import linux as svc_linux

        run = _fake_systemctl(system=None, user=None)
        with (
            patch("kiro_crew.service.linux.subprocess.run", side_effect=run),
            patch("kiro_crew.service.controller.current_platform", return_value=Platform.SYSTEMD),
            patch.object(svc_linux, "remove_apparmor_profile", return_value=MagicMock(message="")),
        ):
            report = svc_linux.uninstall()
            rc = controller.uninstall_service()

        assert report.system == "not installed"
        assert report.user == "not installed"
        assert not any(c[-2:-1] in (["stop"], ["disable"]) for c in run.calls), run.calls
        assert rc == 0
        out = capsys.readouterr().out
        assert "system scope: not installed" in out
        assert "user scope: not installed" in out
        assert "stopped and removed" not in out

    def test_uninstall_leaves_an_unreachable_user_scope_alone_and_names_it(self):
        from kiro_crew.service import linux as svc_linux

        run = _fake_systemctl(system=None, user_bus_error=_NO_BUS)
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.uninstall()

        assert report.system == "not installed"
        assert report.user.startswith("not reachable from this shell")
        assert _NO_BUS in report.user
        assert self._user_calls(run) == [
            [
                "systemctl",
                "--user",
                "show",
                "-p",
                "Id",
                "-p",
                "LoadState",
                "-p",
                "ActiveState",
                "-p",
                "SubState",
                "-p",
                "FragmentPath",
                "-p",
                "Result",
                "-p",
                "ExecMainStartTimestampMonotonic",
                _UNIT,
            ]
        ], run.calls

    def test_uninstall_removes_both_scopes_and_the_controller_names_each(
        self, tmp_path, monkeypatch, capsys
    ):
        from kiro_crew.service import controller
        from kiro_crew.service import linux as svc_linux

        system_unit = tmp_path / "etc" / _UNIT
        system_unit.parent.mkdir(parents=True)
        system_unit.write_text("", encoding="utf-8")
        monkeypatch.setattr(svc_linux, "UNIT_PATH", system_unit)
        fragment = tmp_path / "user" / _UNIT
        fragment.parent.mkdir(parents=True)
        fragment.write_text(_OURS_UNIT_TEXT, encoding="utf-8")
        run = _fake_systemctl(system=_DEAD, user=_RUNNING, user_fragment=str(fragment))
        with (
            patch("kiro_crew.service.linux.subprocess.run", side_effect=run),
            patch("kiro_crew.service.controller.current_platform", return_value=Platform.SYSTEMD),
            patch.object(svc_linux, "remove_apparmor_profile", return_value=MagicMock(message="")),
        ):
            rc = controller.uninstall_service()

        assert rc == 0
        assert ["sudo", "systemctl", "stop", _UNIT] in run.calls
        assert ["systemctl", "--user", "stop", _UNIT] in run.calls
        assert not fragment.exists()
        out = capsys.readouterr().out
        assert "✅ kirocrew service stopped and removed." in out
        assert f"system scope: removed ({system_unit})" in out
        assert f"user scope: removed ({fragment})" in out

    def test_a_user_unit_file_that_cannot_be_removed_is_reported_not_raised(
        self, tmp_path, monkeypatch, capsys
    ):
        """The system scope is already torn down by the time the user unit file is
        unlinked, so a failure there must not raise: the controller would exit
        before printing the system line and before removing the AppArmor grant."""
        from kiro_crew.service import controller
        from kiro_crew.service import linux as svc_linux

        system_unit = tmp_path / "etc" / _UNIT
        system_unit.parent.mkdir()
        system_unit.write_text("", encoding="utf-8")
        monkeypatch.setattr(svc_linux, "UNIT_PATH", system_unit)
        fragment = tmp_path / "ro" / _UNIT
        fragment.parent.mkdir()
        fragment.write_text(_OURS_UNIT_TEXT, encoding="utf-8")
        run = _fake_systemctl(system=_DEAD, user=_RUNNING, user_fragment=str(fragment))
        profile_removed: list[bool] = []

        def remove_profile():
            profile_removed.append(True)
            return MagicMock(message="AppArmor profile removed", ok=True)

        with (
            patch("kiro_crew.service.linux.subprocess.run", side_effect=run),
            patch(
                "kiro_crew.service.linux.os.unlink",
                side_effect=PermissionError("Operation not permitted"),
            ),
            patch("kiro_crew.service.controller.current_platform", return_value=Platform.SYSTEMD),
            patch.object(svc_linux, "remove_apparmor_profile", remove_profile),
        ):
            rc = controller.uninstall_service()

        assert rc == 1
        assert profile_removed == [True]
        out = capsys.readouterr().out
        assert f"system scope: removed ({system_unit})" in out
        assert "user scope: stopped and disabled, but its unit file" in out
        assert str(fragment) in out and "daemon-reload" in out
        assert ["systemctl", "--user", "stop", _UNIT] in run.calls

    # -- a rejected closing daemon-reload leaves the scope unfinished ---------

    _REFUSED_RELOAD = subprocess.CompletedProcess(
        [], 1, "", "Failed to reload daemon: Transport endpoint is not connected\n"
    )

    def test_a_rejected_user_daemon_reload_marks_the_scope_unfinished_and_exits_1(
        self, tmp_path, capsys
    ):
        """The unit file is gone, but the manager refused the reload, so it still
        holds the old definition: the scope is unfinished (exit 1), not removed,
        and the headline never says `stopped and removed`."""
        from kiro_crew.service import controller
        from kiro_crew.service import linux as svc_linux

        fragment = tmp_path / ".config" / "systemd" / "user" / _UNIT
        fragment.parent.mkdir(parents=True)
        fragment.write_text(_OURS_UNIT_TEXT, encoding="utf-8")
        run = _fake_systemctl(
            system=None,
            user=_RUNNING,
            user_fragment=str(fragment),
            overrides={"daemon-reload": self._REFUSED_RELOAD},
        )
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.uninstall()

        assert not fragment.exists()
        assert ["systemctl", "--user", "daemon-reload"] in run.calls
        assert report.user == (
            f"removed ({fragment}), but `systemctl --user daemon-reload` failed: "
            "Failed to reload daemon: Transport endpoint is not connected; run it by hand"
        )
        assert report.unfinished == frozenset({"user"})
        assert "user" not in report.removed
        assert report.incomplete is True

        fragment.write_text(_OURS_UNIT_TEXT, encoding="utf-8")
        run = _fake_systemctl(
            system=None,
            user=_RUNNING,
            user_fragment=str(fragment),
            overrides={"daemon-reload": self._REFUSED_RELOAD},
        )
        with (
            patch("kiro_crew.service.linux.subprocess.run", side_effect=run),
            patch("kiro_crew.service.controller.current_platform", return_value=Platform.SYSTEMD),
            patch.object(svc_linux, "remove_apparmor_profile", return_value=MagicMock(message="")),
        ):
            rc = controller.uninstall_service()

        assert rc == 1
        out = capsys.readouterr().out
        assert "stopped and removed" not in out
        assert "`systemctl --user daemon-reload` failed" in out

    def test_a_rejected_system_daemon_reload_marks_the_scope_unfinished(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.service import linux as svc_linux

        unit_path = tmp_path / "kirocrew.service"
        unit_path.write_text("")
        monkeypatch.setattr(svc_linux, "UNIT_PATH", unit_path)
        monkeypatch.setattr(svc_linux, "_SYSTEMD_BOOTED_DIR", tmp_path)
        run = _fake_systemctl(
            system=_DEAD, user=None, overrides={"daemon-reload": self._REFUSED_RELOAD}
        )
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.uninstall()

        assert ["sudo", "systemctl", "daemon-reload"] in run.calls
        assert report.system == (
            f"removed ({unit_path}), but `sudo systemctl daemon-reload` failed: "
            "Failed to reload daemon: Transport endpoint is not connected; run it by hand"
        )
        assert report.unfinished == frozenset({"system"})
        assert "system" not in report.removed

    def test_an_undecodable_overrides_file_is_kept_and_the_system_scope_still_reloads(
        self, tmp_path, monkeypatch
    ):
        """``/etc/kirocrew/kirocrew.env`` is operator-edited by design, and one
        saved in latin-1 is not UTF-8. It is not our untouched seed, so it stays,
        and the scope still reaches its closing ``daemon-reload``."""
        from kiro_crew.service import linux as svc_linux

        unit_path = tmp_path / "kirocrew.service"
        unit_path.write_text("")
        env_file = tmp_path / "etc-kirocrew" / "kirocrew.env"
        env_file.parent.mkdir()
        env_file.write_bytes(b"# Kiro Crew overrides\nNOTE=caf\xe9\n")
        monkeypatch.setattr(svc_linux, "UNIT_PATH", unit_path)
        monkeypatch.setattr(svc_linux, "ENV_DIR", env_file.parent)
        monkeypatch.setattr(svc_linux, "ENV_FILE_PATH", env_file)
        monkeypatch.setattr(svc_linux, "_SYSTEMD_BOOTED_DIR", tmp_path)
        run = _fake_systemctl(system=_DEAD, user=None)
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            try:
                report = svc_linux.uninstall()
            except UnicodeDecodeError as exc:
                pytest.fail(f"uninstall aborted before the system daemon-reload: {exc!r}")

        assert not any(str(env_file) in c for c in run.calls), run.calls
        assert ["sudo", "systemctl", "daemon-reload"] in run.calls
        assert report.system == f"removed ({unit_path})"
        assert "system" in report.removed

    def test_a_rejected_daemon_reload_after_a_stale_file_removal_is_unfinished(
        self, tmp_path, monkeypatch
    ):
        """The not-loaded arm (`_remove_stale_owned`): the reload is the step that
        makes the manager forget the file, so a refused one is unfinished too."""
        from kiro_crew.service import linux as svc_linux

        system_unit = tmp_path / "etc" / _UNIT
        system_unit.parent.mkdir()
        system_unit.write_text("", encoding="utf-8")
        monkeypatch.setattr(svc_linux, "UNIT_PATH", system_unit)
        monkeypatch.setattr(svc_linux, "_SYSTEMD_BOOTED_DIR", tmp_path)
        run = _fake_systemctl(
            system=None, user=None, overrides={"daemon-reload": self._REFUSED_RELOAD}
        )
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.uninstall()

        assert report.system.startswith(
            f"removed ({system_unit}), but `sudo systemctl daemon-reload`"
        )
        assert report.unfinished == frozenset({"system"})
        assert "system" not in report.removed

    # -- a teardown step the manager refuses never reaches the unit file ------

    _REFUSED_STOP = subprocess.CompletedProcess(
        [],
        1,
        "",
        f"Failed to stop {_UNIT}: Operation refused, unit {_UNIT} may be requested by "
        "dependency only (it is configured to refuse manual start/stop).\n",
    )

    def test_a_refused_user_stop_leaves_the_unit_file_in_place_and_exits_1(
        self, tmp_path, monkeypatch, capsys
    ):
        """`RefuseManualStop=yes` (or a bus that went away mid-run): the stop does
        not take, so the unit is still loaded and possibly running. Its file must
        stay -- unlinking it would leave a running gateway with no unit to find it
        by -- and the report must say so rather than `removed`."""
        from kiro_crew.service import controller
        from kiro_crew.service import linux as svc_linux

        fragment = tmp_path / ".config" / "systemd" / "user" / _UNIT
        fragment.parent.mkdir(parents=True)
        fragment.write_text(_OURS_UNIT_TEXT, encoding="utf-8")
        run = _fake_systemctl(
            system=None,
            user=_RUNNING,
            user_fragment=str(fragment),
            overrides={"stop": self._REFUSED_STOP},
        )
        with (
            patch("kiro_crew.service.linux.subprocess.run", side_effect=run),
            patch("kiro_crew.service.controller.current_platform", return_value=Platform.SYSTEMD),
            patch.object(svc_linux, "remove_apparmor_profile", return_value=MagicMock(message="")),
        ):
            report = svc_linux.uninstall()
            rc = controller.uninstall_service()

        assert fragment.exists(), "the unit file of a unit that did not stop was deleted"
        assert report.user.startswith("left in place ("), report.user
        assert f"`systemctl --user stop {_UNIT}` failed" in report.user
        assert "configured to refuse manual start/stop" in report.user
        assert "the unit file was not removed" in report.user
        assert report.system == "not installed"
        assert report.unfinished == frozenset({"user"})
        assert report.incomplete is True
        assert ["systemctl", "--user", "stop", _UNIT] in run.calls
        assert not any("disable" in c for c in run.calls), run.calls
        assert not any(c[-1:] == ["daemon-reload"] for c in run.calls), run.calls
        assert rc == 1
        out = capsys.readouterr().out
        assert "No kirocrew service was removed." in out
        assert "   system scope: not installed" in out
        assert "   ⚠️ user scope: left in place (" in out

    def test_a_refused_user_disable_after_a_successful_stop_still_keeps_the_file(self, tmp_path):
        from kiro_crew.service import linux as svc_linux

        fragment = tmp_path / "user" / _UNIT
        fragment.parent.mkdir()
        fragment.write_text(_OURS_UNIT_TEXT, encoding="utf-8")
        refused = subprocess.CompletedProcess([], 1, "", "Failed to disable unit: Access denied\n")
        run = _fake_systemctl(
            system=None, user=_RUNNING, user_fragment=str(fragment), overrides={"disable": refused}
        )
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.uninstall()

        assert fragment.exists()
        assert ["systemctl", "--user", "stop", _UNIT] in run.calls
        assert ["systemctl", "--user", "disable", _UNIT] in run.calls
        assert f"`systemctl --user disable {_UNIT}` failed: Failed to disable unit" in report.user
        assert report.unfinished == frozenset({"user"})

    def test_a_refused_system_stop_leaves_the_system_unit_and_still_reports_the_user_scope(
        self, tmp_path, monkeypatch, capsys
    ):
        """The same guard on the sibling branch: a system unit whose sudo'd stop the
        manager refuses is not `rm -f`'d, the warning lands on the SYSTEM line, and
        the user scope is still queried and reported."""
        from kiro_crew.service import controller
        from kiro_crew.service import linux as svc_linux

        system_unit = tmp_path / "etc" / _UNIT
        system_unit.parent.mkdir()
        system_unit.write_text("", encoding="utf-8")
        monkeypatch.setattr(svc_linux, "UNIT_PATH", system_unit)
        run = _fake_systemctl(system=_RUNNING, user=None, overrides={"stop": self._REFUSED_STOP})
        with (
            patch("kiro_crew.service.linux.subprocess.run", side_effect=run),
            patch("kiro_crew.service.controller.current_platform", return_value=Platform.SYSTEMD),
            patch.object(svc_linux, "remove_apparmor_profile", return_value=MagicMock(message="")),
        ):
            report = svc_linux.uninstall()
            rc = controller.uninstall_service()

        assert ["sudo", "systemctl", "stop", _UNIT] in run.calls
        assert not any("rm" in c for c in run.calls), run.calls
        assert not any("disable" in c for c in run.calls), run.calls
        assert report.system.startswith("left in place (")
        assert f"`sudo systemctl stop {_UNIT}` failed" in report.system
        assert report.user == "not installed"
        assert any(c[:3] == ["systemctl", "--user", "show"] for c in run.calls), run.calls
        assert report.unfinished == frozenset({"system"})
        assert rc == 1
        out = capsys.readouterr().out
        assert "   ⚠️ system scope: left in place (" in out
        assert "   user scope: not installed" in out
        assert "⚠️ user scope" not in out

    def test_no_sudo_reports_the_system_unit_and_still_removes_the_user_unit(
        self, tmp_path, monkeypatch, capsys
    ):
        """The SELinux remedy's layout on a host without sudo: a stale system unit
        file beside the account's own running user unit. The system scope cannot
        be touched without root, but that must not abort the call before the user
        unit -- the one this account CAN remove -- is torn down."""
        from kiro_crew.service import controller
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setattr(svc_linux.sys, "platform", "linux")
        monkeypatch.setattr(svc_linux.shutil, "which", lambda name: None)
        system_unit = tmp_path / "etc" / _UNIT
        system_unit.parent.mkdir()
        system_unit.write_text("", encoding="utf-8")
        monkeypatch.setattr(svc_linux, "UNIT_PATH", system_unit)
        fragment = tmp_path / "user" / _UNIT
        fragment.parent.mkdir()
        fragment.write_text(_OURS_UNIT_TEXT, encoding="utf-8")
        run = _fake_systemctl(system=_DEAD, user=_RUNNING, user_fragment=str(fragment))
        with (
            patch("kiro_crew.service.linux.subprocess.run", side_effect=run),
            patch("kiro_crew.service.controller.current_platform", return_value=Platform.SYSTEMD),
            patch.object(svc_linux, "remove_apparmor_profile", return_value=MagicMock(message="")),
        ):
            rc = controller.uninstall_service()

        assert not fragment.exists(), "the reachable user unit was not removed"
        assert system_unit.exists()
        assert ["systemctl", "--user", "stop", _UNIT] in run.calls
        assert ["systemctl", "--user", "disable", _UNIT] in run.calls
        assert not any(c[0] == "sudo" for c in run.calls), run.calls
        assert rc == 1
        out = capsys.readouterr().out
        assert "✅ kirocrew service stopped and removed." in out
        assert "   ⚠️ system scope: left in place (privilege unavailable: " in out
        assert "'sudo' was not found" in out
        assert f"   user scope: removed ({fragment})" in out

    def test_a_stale_system_unit_file_is_removed_when_the_manager_is_unreachable(
        self, tmp_path, monkeypatch
    ):
        """A container where systemd is not PID 1: `install()` writes the unit file
        and its daemon-reload raises, leaving the file behind; every later
        `systemctl` verb fails the same way and `/run/systemd/system` does not
        exist. No manager runs on such a host, so nothing can be running under the
        unit -- the file is removed, as the base did."""
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setattr(svc_linux, "_SYSTEMD_BOOTED_DIR", tmp_path / "run-systemd-absent")
        system_unit = tmp_path / "etc" / _UNIT
        system_unit.parent.mkdir()
        system_unit.write_text("", encoding="utf-8")
        monkeypatch.setattr(svc_linux, "UNIT_PATH", system_unit)
        inner = _fake_systemctl(system=None, user=None)
        no_pid1 = "System has not been booted with systemd as init system (PID 1). Can't operate."

        def run(argv, *a, **kw):
            res = inner(argv, *a, **kw)
            if "systemctl" in argv:
                return subprocess.CompletedProcess(list(argv), 1, "", no_pid1 + "\n")
            return res

        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.uninstall()

        assert report.system == f"removed ({system_unit})"
        assert any(c[:3] == ["sudo", "rm", "-f"] for c in inner.calls), inner.calls
        assert not any("stop" in c or "disable" in c for c in inner.calls), inner.calls
        assert report.user.startswith("not reachable from this shell")
        assert no_pid1 in report.user
        assert report.unfinished == frozenset()

    def test_an_unreachable_manager_on_a_systemd_host_keeps_the_system_unit_file(
        self, tmp_path, monkeypatch
    ):
        """The opposite case: `/run/systemd/system` exists, so a system manager IS
        running, but this shell cannot reach it (a sandbox: `Permission denied`).
        Whether the unit is running cannot be confirmed from here, so nothing is
        removed and the line says so."""
        from kiro_crew.service import linux as svc_linux

        booted = tmp_path / "run-systemd"
        booted.mkdir()
        monkeypatch.setattr(svc_linux, "_SYSTEMD_BOOTED_DIR", booted)
        system_unit = tmp_path / "etc" / _UNIT
        system_unit.parent.mkdir()
        system_unit.write_text("", encoding="utf-8")
        monkeypatch.setattr(svc_linux, "UNIT_PATH", system_unit)
        inner = _fake_systemctl(system=None, user=None)
        denied = "Failed to connect to bus: Permission denied"

        def run(argv, *a, **kw):
            res = inner(argv, *a, **kw)
            if "systemctl" in argv:
                return subprocess.CompletedProcess(list(argv), 1, "", denied + "\n")
            return res

        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.uninstall()

        assert system_unit.exists()
        assert not any("rm" in c for c in inner.calls), inner.calls
        assert report.system.startswith("left in place (the system manager is not reachable")
        assert denied in report.system
        assert "cannot be confirmed" in report.system
        assert report.unfinished == frozenset({"system"})

    def test_a_running_masked_system_unit_is_refused_whole(self, tmp_path, monkeypatch, capsys):
        """`systemctl mask` leaves a running unit running: `show` answers
        `LoadState=masked ActiveState=active`. Skipping the stop because the unit
        is not `loaded` and then unlinking would leave the gateway running with no
        unit to find it by -- so nothing is stopped, disabled or unlinked, and the
        operator is told the step that makes it removable."""
        from kiro_crew.service import controller
        from kiro_crew.service import linux as svc_linux

        system_unit = tmp_path / "etc" / _UNIT
        system_unit.parent.mkdir()
        system_unit.write_text("", encoding="utf-8")
        monkeypatch.setattr(svc_linux, "UNIT_PATH", system_unit)
        run = _fake_systemctl(system=_RUNNING, user=None, system_load="masked")
        with (
            patch("kiro_crew.service.linux.subprocess.run", side_effect=run),
            patch("kiro_crew.service.controller.current_platform", return_value=Platform.SYSTEMD),
            patch.object(svc_linux, "remove_apparmor_profile", return_value=MagicMock(message="")),
        ):
            report = svc_linux.uninstall()
            rc = controller.uninstall_service()

        assert system_unit.exists(), "a running masked unit's file was deleted"
        assert not any("rm" in c or "stop" in c or "disable" in c for c in run.calls), run.calls
        assert report.system == (
            "left in place (an active unit whose load state is masked: unmask and stop it "
            "first, then run `kirocrew service uninstall` again)"
        )
        assert report.unfinished == frozenset({"system"})
        assert rc == 1
        assert "⚠️ system scope: left in place (an active unit whose load state is masked" in (
            capsys.readouterr().out
        )

    def test_a_running_masked_user_unit_is_refused_whole(self, tmp_path):
        from kiro_crew.service import linux as svc_linux

        run = _fake_systemctl(
            system=None, user=_RUNNING, user_fragment="/dev/null", user_load="masked"
        )
        with (
            patch("kiro_crew.service.linux.subprocess.run", side_effect=run),
            patch("kiro_crew.service.linux.os.unlink") as unlink,
        ):
            report = svc_linux.uninstall()

        unlink.assert_not_called()
        assert all("show" in c for c in self._user_calls(run)), run.calls
        assert report.user.startswith("left in place (an active unit whose load state is masked")
        assert "unmask and stop it first" in report.user
        assert report.unfinished == frozenset({"user"})

    def test_a_running_unit_whose_file_was_removed_under_it_is_not_reported_not_installed(
        self,
    ):
        """A user unit deleted and daemon-reloaded while running answers
        `LoadState=not-found ActiveState=active`: that is a running gateway, not
        "nothing here", and `disable` / unlink have nothing to act on."""
        from kiro_crew.service import linux as svc_linux

        run = _fake_systemctl(system=None, user=_RUNNING, user_load="not-found")
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.uninstall()

        assert report.user.startswith("left in place (an active unit whose load state is not-found")
        assert "stop it first" in report.user
        assert report.unfinished == frozenset({"user"})
        assert not any("stop" in c for c in run.calls), run.calls
