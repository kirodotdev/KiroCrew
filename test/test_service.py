"""Tests for the user-service install/uninstall path.

This file holds what ``service install`` renders and writes: platform
detection, the systemd unit and launchd plist, the
operator's overrides file and the privilege that writes it, plus the install
and uninstall steps that write them. One install test is a Windows gap tracked
in ``test/windows-expected-failures.txt``.

The rest of the suite lives beside it, one file per behaviour, in the nine
sibling suites ``service_helpers`` names; they share its autouse isolation and
systemctl fakes. The SELinux and launchd-domain ``test_service_*.py`` files are
separate suites.

Tests do not actually invoke ``systemctl`` or ``launchctl``. The
subprocess calls in :mod:`kiro_crew.service.linux` and
:mod:`kiro_crew.service.macos` are mocked.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from service_helpers import (  # noqa: F401 -- fixtures are found by name
    _RUNNING,
    _clear_sudo_user,
    _fake_systemctl,
    _no_host_env_file,
    _no_host_unit_files,
    _restart_settle_without_waiting,
    _systemctl_stub,
)

from kiro_crew.service import controller
from kiro_crew.service.common import (
    LAUNCHD_LABEL,
    RESTART_REFUSED,
    SERVICE_NAME,
    Platform,
    current_platform,
)


class TestPlatformDetection:
    def test_linux_with_systemctl_returns_systemd(self):
        with patch("kiro_crew.service.common.sys") as mock_sys, patch(
            "kiro_crew.service.common.shutil.which",
            return_value="/usr/bin/systemctl",
        ):
            mock_sys.platform = "linux"
            assert current_platform() == Platform.SYSTEMD

    def test_linux_without_systemctl_returns_unsupported(self):
        with patch("kiro_crew.service.common.sys") as mock_sys, patch(
            "kiro_crew.service.common.shutil.which", return_value=None
        ):
            mock_sys.platform = "linux"
            assert current_platform() == Platform.UNSUPPORTED

    def test_darwin_with_launchctl_returns_launchd(self):
        with patch("kiro_crew.service.common.sys") as mock_sys, patch(
            "kiro_crew.service.common.shutil.which",
            return_value="/bin/launchctl",
        ):
            mock_sys.platform = "darwin"
            assert current_platform() == Platform.LAUNCHD

    def test_unknown_platform_returns_unsupported(self):
        with patch("kiro_crew.service.common.sys") as mock_sys, patch(
            "kiro_crew.service.common.shutil.which",
            return_value="/usr/bin/anything",
        ):
            mock_sys.platform = "win32"
            assert current_platform() == Platform.UNSUPPORTED


class TestManagedServiceMarkerDetection:
    def test_no_installed_definition_is_not_applicable(self, monkeypatch, tmp_path):
        monkeypatch.setattr(controller, "current_platform", lambda: Platform.SYSTEMD)
        monkeypatch.setattr(controller.linux, "UNIT_PATH", tmp_path / "missing.service")
        # No system unit file, and the account's own manager has no unit either:
        # the user scope is ASKED (never the host's real manager from a test).
        run = _fake_systemctl(system=None, user=None)
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            assert controller.installed_service_has_managed_marker() is None
        assert ["systemctl", "--user", "show"] == run.calls[0][:3], run.calls

    def test_systemd_definition_requires_explicit_marker(self, monkeypatch, tmp_path):
        path = tmp_path / "kirocrew.service"
        monkeypatch.setattr(controller, "current_platform", lambda: Platform.SYSTEMD)
        monkeypatch.setattr(controller.linux, "UNIT_PATH", path)
        path.write_text("[Service]\nExecStart=kirocrew gateway\n", encoding="utf-8")
        run = _fake_systemctl(system=_RUNNING, user=None)
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            assert controller.installed_service_has_managed_marker() is False
            path.write_text(
                '[Service]\nEnvironment="KIROCREW_SERVICE_MANAGED=1"\n',
                encoding="utf-8",
            )
            assert controller.installed_service_has_managed_marker() is True
        # The system unit file answers on its own; the user manager is not asked.
        assert run.calls == []

    def test_user_scope_definition_is_read_for_the_marker(self, monkeypatch, tmp_path):
        # The SELinux remedy's per-user unit bakes the same Environment= line, so
        # doctor's managed-marker check must read it where the base read "no
        # service installed" — from the file the user manager reports as loaded.
        fragment = tmp_path / ".config" / "systemd" / "user" / "kirocrew.service"
        fragment.parent.mkdir(parents=True)
        fragment.write_text("[Service]\nExecStart=kirocrew gateway\n", encoding="utf-8")
        monkeypatch.setattr(controller, "current_platform", lambda: Platform.SYSTEMD)
        monkeypatch.setattr(controller.linux, "UNIT_PATH", tmp_path / "missing.service")
        run = _fake_systemctl(system=None, user=_RUNNING, user_fragment=str(fragment))
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            assert controller.installed_service_has_managed_marker() is False
            fragment.write_text(
                '[Service]\nEnvironment="KIROCREW_SERVICE_MANAGED=1"\n',
                encoding="utf-8",
            )
            assert controller.installed_service_has_managed_marker() is True
        assert all("sudo" not in c for c in run.calls), run.calls

    def test_launchd_definition_reads_environment_dictionary(self, monkeypatch, tmp_path):
        path = tmp_path / "dev.kirocrew.gateway.plist"
        monkeypatch.setattr(controller, "current_platform", lambda: Platform.LAUNCHD)
        monkeypatch.setattr(controller.macos, "PLIST_PATH", path)
        path.write_bytes(plistlib.dumps({"Label": "dev.kirocrew.gateway"}))
        assert controller.installed_service_has_managed_marker() is False
        path.write_bytes(
            plistlib.dumps(
                {"EnvironmentVariables": {"KIROCREW_SERVICE_MANAGED": "1"}}
            )
        )
        assert controller.installed_service_has_managed_marker() is True

    def test_malformed_launchd_definition_is_reported_stale(self, monkeypatch, tmp_path):
        path = tmp_path / "dev.kirocrew.gateway.plist"
        monkeypatch.setattr(controller, "current_platform", lambda: Platform.LAUNCHD)
        monkeypatch.setattr(controller.macos, "PLIST_PATH", path)
        path.write_text(
            "<plist><dict><key>Label</key><string>Kiro & Crew</string></dict></plist>",
            encoding="utf-8",
        )

        assert controller.installed_service_has_managed_marker() is False


class TestShutdownBudget:
    def test_service_deadline_covers_gateway_grace(self):
        from kiro_crew.gateway_shutdown_budget import (
            GRACEFUL_SHUTDOWN_SECS,
            SIGNAL_MARGIN_SECS,
            TOTAL_SHUTDOWN_BUDGET_SECS,
        )

        assert TOTAL_SHUTDOWN_BUDGET_SECS == (
            GRACEFUL_SHUTDOWN_SECS + SIGNAL_MARGIN_SECS
        )
        assert (GRACEFUL_SHUTDOWN_SECS, TOTAL_SHUTDOWN_BUDGET_SECS) == (10, 20)


class TestLinuxUnitRendering:
    """The rendered systemd unit should reference the resolved kirocrew bin."""

    def test_render_unit_includes_exec_start(self, tmp_path, monkeypatch):
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setenv("USER", "tester")
        # `id -gn tester` would return some real group; mock it to a known value
        # so the test asserts both User= and Group= are populated correctly.
        gid_result = MagicMock(returncode=0, stdout="amazon\n", stderr="")
        with patch(
            "kiro_crew.service.common.shutil.which",
            return_value="/home/u/.toolbox/bin/kirocrew",
        ), patch(
            "kiro_crew.service.linux.subprocess.run", return_value=gid_result
        ), patch(
            "kiro_crew.service.linux.trusted_system_bin", return_value="/usr/bin/id"
        ):
            unit = svc_linux.render_unit()
        # ExecStart executable is double-quoted (systemd tokenizes on
        # whitespace; a spaced path would otherwise break the exec). `--no-open`
        # is asserted as part of the SAME string rather than separately: a bare
        # `in unit` check for the prefix passes even if the flag is dropped,
        # because the prefix is still a substring of the shorter line.
        assert 'ExecStart="/home/u/.toolbox/bin/kirocrew" gateway --no-open' in unit
        # `always`, not `on-failure`: the stale-asset watchdog exits the
        # gateway cleanly so the supervisor relaunches a fresh install, and
        # on-failure never restarts an exit 0 (gateway stranded for hours).
        assert "Restart=always" in unit
        assert "Restart=on-failure" not in unit
        assert "RestartSec=10" in unit
        # System-level unit must run as the invoking user with the user's
        # actual primary group (which on Amazon Linux is `amazon`, not the
        # username — getting this wrong causes status=216/GROUP at startup).
        assert "User=tester" in unit
        assert "Group=amazon" in unit
        # Safety net: cap restart loops at 3 in 5 minutes so a bad
        # gateway start cannot melt the user's terminal with journal output.
        assert "StartLimitBurst=3" in unit
        assert "StartLimitIntervalSec=300" in unit
        # systemd 219 (<= 228) reads StartLimit* only from [Service]; both
        # sections are populated so the cap works on all supported distros.
        assert "StartLimitInterval=300" in unit
        service_idx = unit.index("[Service]")
        install_idx = unit.index("[Install]")
        assert unit.index("StartLimitBurst=3", service_idx) < install_idx
        assert unit.index("StartLimitInterval=300", service_idx) < install_idx
        # Pin a high open-file limit so the gateway (and the FD-hungry
        # frontend build it may launch) never depends on the host's ambient
        # DefaultLimitNOFILE — stock systemd defaults to 1024, which the
        # vite/rollup build exhausts with EMFILE.
        assert "LimitNOFILE=65536" in unit
        assert "[Install]" in unit
        # System-level units want multi-user.target (the default boot target),
        # not default.target (which is user-session-scoped and only used
        # by `systemctl --user`).
        assert "WantedBy=multi-user.target" in unit

    @pytest.mark.parametrize("user_scope", [False, True])
    def test_render_unit_exempts_the_live_holder_refusal_from_restart(
        self, monkeypatch, user_scope
    ):
        """``Restart=always`` relaunches every exit -- including the lock refusal
        for a home a live sibling gateway already serves. That refusal stands
        for as long as the incumbent runs, so each relaunch boots the stack only
        to meet it again until StartLimit* parks the unit ``failed`` (or forever
        on a unit without them). ``RestartPreventExitStatus`` names that ONE
        status so the unit stands down instead, and it names the constant the
        CLI exits with, so the exit and the exemption cannot drift apart.
        """
        from kiro_crew.gateway_lock import LIVE_HOLDER_EXIT_CODE
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setenv("USER", "tester")
        gid_result = MagicMock(returncode=0, stdout="amazon\n", stderr="")
        with patch(
            "kiro_crew.service.common.shutil.which",
            return_value="/home/u/.toolbox/bin/kirocrew",
        ), patch(
            "kiro_crew.service.linux.subprocess.run", return_value=gid_result
        ):
            unit = svc_linux.render_unit(user_scope=user_scope)

        directives = [
            ln for ln in unit.splitlines() if ln.startswith("RestartPreventExitStatus=")
        ]
        assert len(directives) == 1, unit
        exempt = {int(tok) for tok in directives[0].split("=", 1)[1].split()}
        assert exempt == {LIVE_HOLDER_EXIT_CODE}
        # Pinned: the value is baked into every installed unit, which is not
        # re-rendered on upgrade, so changing it is a deliberate act that must
        # also re-render those units -- not a drive-by. 78 is EX_CONFIG.
        assert LIVE_HOLDER_EXIT_CODE == 78
        # The exemption must not widen: a transient lock failure exits 1 and
        # the running gateway's own relaunch requests (69 listener lost, 75
        # stale assets) rely on being restarted.
        assert not exempt & {0, 1, 69, 75}
        # The restart policy itself is untouched: `always` (a clean self-exit
        # still relaunches) and the StartLimit* cap on a tight loop stay.
        assert "Restart=always" in unit
        assert "RestartSec=10" in unit
        assert "StartLimitBurst=3" in unit
        assert "StartLimitIntervalSec=300" in unit
        # The directive belongs to [Service], not [Unit] or [Install].
        service_block = unit.split("[Service]", 1)[1].split("[Install]", 1)[0]
        # systemd 219 (<= 228) compatibility: also present in [Service].
        assert "StartLimitBurst=3" in service_block
        assert "StartLimitInterval=300" in service_block
        assert directives[0] in service_block.splitlines()

    def test_render_unit_carries_the_session_bus_environment(self, monkeypatch):
        """A system unit inherits no login-session env, so pods (systemd --user
        units) were unreachable from the service-installed gateway. The unit must
        wire up the per-user systemd instance explicitly."""
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setenv("USER", "tester")
        gid_result = MagicMock(returncode=0, stdout="staff\n", stderr="")
        with patch(
            "kiro_crew.service.common.shutil.which",
            return_value="/usr/local/bin/kirocrew",
        ), patch(
            "kiro_crew.service.linux.subprocess.run", return_value=gid_result
        ), patch.object(
            svc_linux, "_current_uid", return_value=4242
        ):
            unit = svc_linux.render_unit()

        assert 'Environment="XDG_RUNTIME_DIR=/run/user/4242"\n' in unit
        assert (
            'Environment="DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/4242/bus"\n' in unit
        )
        # No reordering regression: the pre-existing Environment lines survive,
        # still inside [Service] and still ahead of the new ones.
        assert 'Environment="USER=tester"\n' in unit
        assert 'Environment="HOME=' in unit
        assert 'Environment="PATH=' in unit
        assert 'Environment="KIROCREW_SERVICE_MANAGED=1"\n' in unit
        service = unit.index("[Service]")
        install = unit.index("[Install]")
        for key in (
            "HOME",
            "USER",
            "PATH",
            "KIROCREW_SERVICE_MANAGED",
            "XDG_RUNTIME_DIR",
            "DBUS_SESSION_BUS_ADDRESS",
        ):
            at = unit.index(f'Environment="{key}=')
            assert service < at < install, f"Environment={key} escaped [Service]"
        assert unit.index('Environment="PATH=') < unit.index('Environment="XDG_RUNTIME_DIR=')

    def test_render_unit_omits_session_bus_when_uid_unresolvable(self, monkeypatch):
        """Rather than bake in a guessed uid, omit the pair — the pod runtime
        backfills the same values at call time anyway."""
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setenv("USER", "tester")
        gid_result = MagicMock(returncode=0, stdout="staff\n", stderr="")
        with patch(
            "kiro_crew.service.common.shutil.which",
            return_value="/usr/local/bin/kirocrew",
        ), patch(
            "kiro_crew.service.linux.subprocess.run", return_value=gid_result
        ), patch.object(
            svc_linux, "_current_uid", return_value=None
        ):
            unit = svc_linux.render_unit()

        assert "XDG_RUNTIME_DIR" not in unit
        assert "DBUS_SESSION_BUS_ADDRESS" not in unit
        # The rest of the unit is still well-formed.
        assert 'Environment="PATH=' in unit
        assert "[Install]" in unit

    def test_session_bus_is_systemd_only_not_in_the_shared_environment(self):
        """`/run/user/<uid>` is a Linux/systemd path with no launchd equivalent,
        so it must NOT leak into the env shared with the macOS plist."""
        from kiro_crew.service.common import service_environment

        keys = set(service_environment("/home/tester"))
        assert "XDG_RUNTIME_DIR" not in keys
        assert "DBUS_SESSION_BUS_ADDRESS" not in keys
        assert service_environment("/home/tester")["KIROCREW_SERVICE_MANAGED"] == "1"

    def test_current_uid_returns_none_for_an_unknown_user(self):
        from kiro_crew.service import linux as svc_linux

        assert svc_linux._current_uid("no-such-user-e2b9f1") is None

    def test_render_unit_falls_back_to_argv0_when_kirocrew_not_on_path(self, monkeypatch):
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setenv("USER", "tester")
        with patch("kiro_crew.service.common.shutil.which", return_value=None), patch.object(
            sys, "argv", ["/some/path/kirocrew"]
        ):
            unit = svc_linux.render_unit()
        # argv[0] is realpathed; just check the unit references *something*
        # that ends in the (quoted) kirocrew executable followed by gateway.
        assert 'kirocrew" gateway' in unit

    def test_install_writes_unit_via_sudo_install_and_invokes_systemctl(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setenv("USER", "tester")
        # Every subprocess call below is captured, so nothing is written: this test
        # opts back in to the production unit path to check the argv names it.
        monkeypatch.setattr(svc_linux, "UNIT_PATH", Path(f"/etc/systemd/system/{SERVICE_NAME}.service"))
        # Pin a non-root euid so the privilege prefix is deterministically
        # `sudo` regardless of the CI runner's uid (root CI would drop it).
        monkeypatch.setattr(svc_linux.os, "geteuid", lambda: 1000, raising=False)

        # Capture every subprocess.run call. All return success.
        ok = MagicMock(returncode=0, stdout="", stderr="")
        with patch(
            "kiro_crew.service.common.shutil.which",
            return_value="/usr/local/bin/kirocrew",
        ), patch(
            "kiro_crew.service.linux.subprocess.run", return_value=ok
        ) as run:
            svc_linux.install()

        # Four things must happen:
        # 1) `sudo install -m 0644 -o root -g root <tmp> /etc/systemd/system/kirocrew.service`
        # 2) `sudo systemctl daemon-reload`
        # 3) `sudo systemctl enable kirocrew.service`
        # 4) `sudo systemctl restart kirocrew.service`
        called = [list(c.args[0]) for c in run.call_args_list]
        install_calls = [
            c
            for c in called
            if len(c) >= 9
            and c[:2] == ["sudo", "install"]
            and c[-1] == f"/etc/systemd/system/{SERVICE_NAME}.service"
        ]
        assert install_calls, f"expected sudo install of unit path; got {called}"
        # The destination must be set with root ownership and 0644 mode so
        # systemd accepts it on daemon-reload.
        assert "-m" in install_calls[0] and "0644" in install_calls[0]
        assert "-o" in install_calls[0] and "root" in install_calls[0]
        assert ["sudo", "systemctl", "daemon-reload"] in called
        assert ["sudo", "systemctl", "enable", f"{SERVICE_NAME}.service"] in called
        assert ["sudo", "systemctl", "restart", f"{SERVICE_NAME}.service"] in called

    def test_install_raises_with_clear_error_when_sudo_install_fails(
        self, monkeypatch
    ):
        """If `sudo install` fails (user denies password, sudoers misconfigured),
        install MUST raise with a clear message rather than continuing on
        and silently leaving the system half-configured."""
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setenv("USER", "tester")
        monkeypatch.setattr(svc_linux.os, "geteuid", lambda: 1000, raising=False)
        install_failed = MagicMock(
            returncode=1, stdout="", stderr="sudo: a password is required"
        )

        with patch(
            "kiro_crew.service.common.shutil.which",
            return_value="/usr/local/bin/kirocrew",
        ), patch(
            "kiro_crew.service.linux.subprocess.run", return_value=install_failed
        ):
            with pytest.raises(svc_linux.ServiceInstallError) as exc_info:
                svc_linux.install()

        msg = str(exc_info.value)
        # Error must mention which step failed and reference sudo so the
        # user knows what's going on.
        assert "unit file" in msg.lower()
        assert "sudo" in msg.lower() or "password" in msg.lower()

    def test_install_raises_when_user_env_unset(self, monkeypatch):
        """Defensive: render_unit needs the user's name to fill `User=`. If
        the env doesn't expose it, fail fast rather than render a unit
        with an empty User= line that systemd will reject."""
        from kiro_crew.service import linux as svc_linux

        monkeypatch.delenv("USER", raising=False)
        monkeypatch.delenv("LOGNAME", raising=False)

        with patch(
            "kiro_crew.service.common.shutil.which",
            return_value="/usr/local/bin/kirocrew",
        ):
            with pytest.raises(svc_linux.ServiceInstallError):
                svc_linux.install()

    def test_linger_warning_fires_when_linger_is_off(self, monkeypatch):
        """A system unit parents every runtime scope under ``user@<uid>.service``
        (the baked ``XDG_RUNTIME_DIR`` / ``DBUS_SESSION_BUS_ADDRESS``), so with
        ``Linger=no`` the account's manager stops at logout and takes the
        runtimes down with it. The install path must say so and name the fix."""
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setenv("USER", "tester")
        monkeypatch.setattr(svc_linux, "trusted_system_bin", lambda _n: "/usr/bin/loginctl")

        def _loginctl(argv, *_a, **_k):
            assert argv == ["/usr/bin/loginctl", "show-user", "tester", "-p", "Linger"], argv
            return subprocess.CompletedProcess(argv, 0, "Linger=no\n", "")

        monkeypatch.setattr(svc_linux.subprocess, "run", _loginctl)
        warning = svc_linux.linger_warning_for_service_account()
        assert warning is not None
        assert "linger" in warning.lower()
        assert "loginctl enable-linger tester" in warning

    def test_linger_warning_silent_when_linger_is_on(self, monkeypatch):
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setenv("USER", "tester")
        monkeypatch.setattr(svc_linux, "trusted_system_bin", lambda _n: "/usr/bin/loginctl")

        def _loginctl(argv, *_a, **_k):
            return subprocess.CompletedProcess(argv, 0, "Linger=yes\n", "")

        monkeypatch.setattr(svc_linux.subprocess, "run", _loginctl)
        assert svc_linux.linger_warning_for_service_account() is None

    def test_linger_warning_silent_when_undeterminable(self, monkeypatch):
        """No ``loginctl``, an unknown user, or an unrecognised value stays quiet
        rather than guessing — the install still succeeds unqualified."""
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setenv("USER", "tester")
        monkeypatch.setattr(svc_linux, "trusted_system_bin", lambda _n: None)
        assert svc_linux.linger_warning_for_service_account() is None

    def test_uninstall_is_idempotent_when_unit_missing(self, tmp_path, monkeypatch):
        from kiro_crew.service import linux as svc_linux

        # Point UNIT_PATH at a nonexistent file; both scopes are only QUERIED
        # (an unprivileged `show` each — the system manager is asked even with
        # no file, since a deleted file leaves a loaded unit running) and neither
        # gets a verb, so an uninstall on a host with nothing installed never
        # prompts for a password.
        unit_path = tmp_path / "missing.service"
        monkeypatch.setattr(svc_linux, "UNIT_PATH", unit_path)
        monkeypatch.setattr(svc_linux.os, "geteuid", lambda: 1000, raising=False)
        run = _fake_systemctl(system=None, user=None)
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.uninstall()
        assert report.system == "not installed"
        assert report.user == "not installed"
        assert all("show" in c and "sudo" not in c for c in run.calls), run.calls
        assert any(c[:3] == ["systemctl", "--user", "show"] for c in run.calls), run.calls


class TestLinuxPrivilegeResolution:
    """Root fast-path and the missing-sudo error, so a minimal CentOS /
    container image (root, no sudo) neither shells out to a nonexistent sudo
    nor lets a raw FileNotFoundError escape controller.install_service."""

    def test_privilege_prefix_is_empty_as_root(self, monkeypatch):
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setattr(svc_linux.os, "geteuid", lambda: 0, raising=False)
        assert svc_linux._privilege_prefix() == []

    def test_privilege_prefix_is_sudo_when_not_root(self, monkeypatch):
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setattr(svc_linux.os, "geteuid", lambda: 1000, raising=False)
        assert svc_linux._privilege_prefix() == ["sudo"]

    def test_require_privilege_ok_as_root_without_sudo(self, monkeypatch):
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setattr(svc_linux.os, "geteuid", lambda: 0, raising=False)
        monkeypatch.setattr(svc_linux.shutil, "which", lambda _n: None)
        # Root needs no sudo — must not raise.
        svc_linux._require_privilege()

    def test_require_privilege_raises_when_not_root_and_no_sudo(self, monkeypatch):
        from kiro_crew.service import linux as svc_linux

        # The guard is Linux-scoped (systemd module); pin the platform so the
        # test asserts the raising branch on any host it runs on.
        monkeypatch.setattr(svc_linux.sys, "platform", "linux")
        monkeypatch.setattr(svc_linux.os, "geteuid", lambda: 1000, raising=False)
        monkeypatch.setattr(svc_linux.shutil, "which", lambda _n: None)
        with pytest.raises(svc_linux.ServiceInstallError) as exc:
            svc_linux._require_privilege()
        assert "sudo" in str(exc.value).lower()

    def test_require_privilege_is_noop_off_linux(self, monkeypatch):
        from kiro_crew.service import linux as svc_linux

        # On a non-Linux host (macOS/Windows) the systemd path is never the real
        # dispatch target, and cross-platform unit tests call these functions
        # with a mocked subprocess layer, so the guard must not raise there.
        monkeypatch.setattr(svc_linux.sys, "platform", "darwin")
        monkeypatch.setattr(svc_linux.os, "geteuid", lambda: 1000, raising=False)
        monkeypatch.setattr(svc_linux.shutil, "which", lambda _n: None)
        svc_linux._require_privilege()  # must not raise

    def test_install_refuses_to_run_agent_as_root(self, monkeypatch):
        """A bare-root install (root login, or sudo with no SUDO_USER) must NOT
        produce a User=root unit — the gateway runs untrusted tools and the
        module invariant is that it runs as a normal user."""
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setenv("USER", "root")
        monkeypatch.delenv("SUDO_USER", raising=False)
        monkeypatch.delenv("LOGNAME", raising=False)
        monkeypatch.setattr(svc_linux.os, "geteuid", lambda: 0, raising=False)
        with pytest.raises(svc_linux.ServiceInstallError) as exc:
            svc_linux.install()
        assert "root" in str(exc.value).lower()

    def test_current_user_prefers_sudo_user_over_root(self, monkeypatch):
        """`sudo kirocrew service install` must target the human behind sudo,
        not the root sudo elevated to — so the unit gets User=<human>."""
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setenv("USER", "root")
        monkeypatch.setenv("SUDO_USER", "alice")
        assert svc_linux._current_user() == "alice"

    def test_unit_home_matches_the_resolved_user_not_process_home(self, monkeypatch):
        """Under `sudo -H` the process HOME is /root but User= is the sudo human.
        HOME=/WorkingDirectory= in the unit must follow the resolved USER (from
        that user's passwd home), never the process's /root — otherwise the
        non-root service cannot enter its working dir and fails to start."""
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setenv("USER", "root")
        monkeypatch.setenv("SUDO_USER", "alice")
        # Keep inherited tool paths independent of the HOME assertion.
        monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin:/bin")
        # Simulate `sudo -H`: process home is /root.
        monkeypatch.setattr(svc_linux.Path, "home", classmethod(lambda cls: Path("/root")))
        # alice's passwd home.
        monkeypatch.setattr(svc_linux, "_home_for_user", lambda u: "/home/alice" if u == "alice" else "/root")
        gid = MagicMock(returncode=0, stdout="alice\n", stderr="")
        with patch(
            "kiro_crew.service.common.shutil.which", return_value="/usr/local/bin/kirocrew"
        ), patch("kiro_crew.service.linux.subprocess.run", return_value=gid):
            unit = svc_linux.render_unit()
        assert "User=alice" in unit
        assert "WorkingDirectory=/home/alice" in unit
        assert 'Environment="HOME=/home/alice"' in unit
        assert "/root" not in unit

    def test_install_raises_clean_error_when_sudo_missing(self, monkeypatch):
        """The reported bug: on a root-only/minimal host without sudo, install
        must not crash with an uncaught FileNotFoundError; it must raise the
        friendly ServiceInstallError instead."""
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setattr(svc_linux.sys, "platform", "linux")
        monkeypatch.setenv("USER", "tester")
        monkeypatch.delenv("SUDO_USER", raising=False)
        monkeypatch.setattr(svc_linux.os, "geteuid", lambda: 1000, raising=False)
        monkeypatch.setattr(svc_linux.shutil, "which", lambda _n: None)
        with pytest.raises(svc_linux.ServiceInstallError):
            svc_linux.install()

    def test_sudo_run_survives_missing_sudo_binary(self, monkeypatch):
        """restart()/stop() are best-effort and reachable from the update path;
        a missing sudo must degrade to a failed result, never a raised
        FileNotFoundError."""
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setattr(svc_linux.os, "geteuid", lambda: 1000, raising=False)
        active = _fake_systemctl(system=_RUNNING, user=None)

        def _boom(argv, *a, **k):
            # Only the escalated spawn is missing its binary; the unprivileged
            # `is-active` queries that gate restart() answer normally.
            if list(argv)[:1] == ["sudo"]:
                raise FileNotFoundError("sudo")
            return active(argv, *a, **k)

        monkeypatch.setattr(svc_linux.subprocess, "run", _boom)
        res = svc_linux._sudo_run("systemctl", "restart", "kirocrew.service")
        assert res.returncode == 127
        # restart() surfaces the failure as a refused scope rather than crashing.
        report = svc_linux.restart()
        assert report.ok is False
        assert [(f.scope, f.kind) for f in report.failures] == [("system", RESTART_REFUSED)]


class TestLinuxEnvironmentFile:
    """The operator-editable overrides file — the honest fix to 'I set
    KIROCREW_PORT on the service and it did not change the port'."""

    def test_unit_references_the_env_file(self, monkeypatch):
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setenv("USER", "tester")
        with patch(
            "kiro_crew.service.common.shutil.which",
            return_value="/usr/local/bin/kirocrew",
        ):
            unit = svc_linux.render_unit()
        assert f"EnvironmentFile=-{svc_linux.ENV_FILE_PATH}\n" in unit
        # The overrides file is read after (and thus overrides) the baked
        # Environment= snapshot; both must sit inside [Service].
        service = unit.index("[Service]")
        install = unit.index("[Install]")
        assert service < unit.index("EnvironmentFile=") < install

    def test_seed_env_file_creates_when_absent(self, tmp_path, monkeypatch):
        from kiro_crew.service import linux as svc_linux

        env_file = tmp_path / "kirocrew" / "kirocrew.env"
        monkeypatch.setattr(svc_linux, "ENV_DIR", env_file.parent)
        monkeypatch.setattr(svc_linux, "ENV_FILE_PATH", env_file)

        written: dict[str, str] = {}

        def _fake_install(contents, dest, mode="0644"):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(contents)
            written["contents"] = contents

        # _seed_env_file probes existence via `_sudo_run("test", "-e", path)`;
        # answer it from the real tmp file so the create-if-absent logic runs.
        def _fake_sudo(*args, **_k):
            if args and args[0] == "test":
                rc = 0 if Path(args[-1]).exists() else 1
                return MagicMock(returncode=rc, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(svc_linux, "_install_file_via_sudo", _fake_install)
        monkeypatch.setattr(svc_linux, "_sudo_run", _fake_sudo)

        svc_linux._seed_env_file()
        assert env_file.exists()
        # Seed is inert until an operator opts in: the port line is commented.
        assert "#KIROCREW_PORT=" in written["contents"]

    def test_seed_env_file_never_clobbers_operator_edits(self, tmp_path, monkeypatch):
        from kiro_crew.service import linux as svc_linux

        env_file = tmp_path / "kirocrew" / "kirocrew.env"
        env_file.parent.mkdir(parents=True)
        env_file.write_text("KIROCREW_PORT=5477\n")
        monkeypatch.setattr(svc_linux, "ENV_DIR", env_file.parent)
        monkeypatch.setattr(svc_linux, "ENV_FILE_PATH", env_file)

        def _fake_sudo(*args, **_k):
            if args and args[0] == "test":
                rc = 0 if Path(args[-1]).exists() else 1
                return MagicMock(returncode=rc, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        called = MagicMock()
        monkeypatch.setattr(svc_linux, "_install_file_via_sudo", called)
        monkeypatch.setattr(svc_linux, "_sudo_run", _fake_sudo)
        svc_linux._seed_env_file()
        # An existing file is left exactly as the operator wrote it.
        called.assert_not_called()
        assert env_file.read_text() == "KIROCREW_PORT=5477\n"

    def test_seed_env_file_is_non_fatal_when_probe_denied(self, tmp_path, monkeypatch):
        """A pre-existing root-only /etc/kirocrew must not abort install: the
        existence probe goes through privileged `test -e`, and any error still
        degrades to a warning instead of propagating."""
        from kiro_crew.service import linux as svc_linux

        env_file = tmp_path / "kirocrew" / "kirocrew.env"
        monkeypatch.setattr(svc_linux, "ENV_DIR", env_file.parent)
        monkeypatch.setattr(svc_linux, "ENV_FILE_PATH", env_file)

        # Even if the privileged probe itself raised, _seed_env_file swallows it.
        def _boom(*_a, **_k):
            raise OSError("permission denied")

        monkeypatch.setattr(svc_linux, "_sudo_run", _boom)
        monkeypatch.setattr(svc_linux, "_install_file_via_sudo", MagicMock())
        svc_linux._seed_env_file()  # must not raise

    def test_uninstall_preserves_an_operator_edited_env_file(self, tmp_path, monkeypatch):
        """Uninstall must delete ONLY our untouched seed — an operator-authored
        or -edited overrides file (including one pre-provisioned before install)
        is their config, not ours to remove."""
        from kiro_crew.service import linux as svc_linux

        unit = tmp_path / "kirocrew.service"
        unit.write_text("[Unit]\n")
        env_file = tmp_path / "kirocrew" / "kirocrew.env"
        env_file.parent.mkdir(parents=True)
        env_file.write_text("KIROCREW_PORT=5477\n")  # operator content, not our seed
        monkeypatch.setattr(svc_linux, "UNIT_PATH", unit)
        monkeypatch.setattr(svc_linux, "ENV_DIR", env_file.parent)
        monkeypatch.setattr(svc_linux, "ENV_FILE_PATH", env_file)
        monkeypatch.setattr(svc_linux.os, "geteuid", lambda: 1000, raising=False)

        removed: list[str] = []

        def _fake_sudo(*args, **_k):
            if args and args[0] == "rm":
                removed.append(args[-1])
            return MagicMock(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(svc_linux, "_sudo_run", _fake_sudo)
        monkeypatch.setattr(svc_linux, "_systemctl", _systemctl_stub(unit))
        svc_linux.uninstall()
        # The unit is removed; the operator's env file is NOT.
        assert str(unit) in removed
        assert str(env_file) not in removed

    def test_uninstall_removes_our_untouched_seed(self, tmp_path, monkeypatch):
        from kiro_crew.service import linux as svc_linux

        unit = tmp_path / "kirocrew.service"
        unit.write_text("[Unit]\n")
        env_file = tmp_path / "kirocrew" / "kirocrew.env"
        env_file.parent.mkdir(parents=True)
        env_file.write_text(svc_linux._ENV_FILE_TEMPLATE)  # our exact untouched seed
        monkeypatch.setattr(svc_linux, "UNIT_PATH", unit)
        monkeypatch.setattr(svc_linux, "ENV_DIR", env_file.parent)
        monkeypatch.setattr(svc_linux, "ENV_FILE_PATH", env_file)
        monkeypatch.setattr(svc_linux.os, "geteuid", lambda: 1000, raising=False)

        removed: list[str] = []

        def _fake_sudo(*args, **_k):
            if args and args[0] in ("rm", "rmdir"):
                removed.append(args[-1])
            return MagicMock(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(svc_linux, "_sudo_run", _fake_sudo)
        monkeypatch.setattr(svc_linux, "_systemctl", _systemctl_stub(unit))
        svc_linux.uninstall()
        assert str(env_file) in removed


def _text_writes_missing_encoding(source: str) -> list[str]:
    """Return ``"<line>: <call>"`` for every TEXT-mode open in ``source`` that
    does not name an explicit ``encoding=``.

    Binary mode is skipped -- it has no encoding to name. A call whose mode is
    not a literal is treated as text, which is the conservative direction: a
    dynamic mode still needs an encoding on the text branch.
    """
    import ast

    found: list[str] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        else:
            continue
        if name not in ("open", "fdopen"):
            continue
        # Mode is the second positional arg for both builtins.open and
        # os.fdopen, or the `mode=` keyword.
        mode = None
        if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
            mode = node.args[1].value
        for kw in node.keywords:
            if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                mode = kw.value.value
        if isinstance(mode, str) and "b" in mode:
            continue
        if any(kw.arg == "encoding" for kw in node.keywords):
            continue
        found.append(f"{node.lineno}: {name}(...)")
    return found


class TestLinuxServiceWritesUtf8:
    """systemd reads unit and environment files as UTF-8. The writers here must
    therefore EMIT UTF-8, not whatever ``locale.getpreferredencoding()`` happens
    to return on the installing host.

    Both staging writes went through ``os.fdopen(fd, "w")`` with no
    ``encoding=``, so the bytes handed to ``sudo install`` were locale-dependent
    while the consumer's contract is fixed. The same module already reads its
    environment file back with ``encoding="utf-8"`` (``_env_file_is_untouched_seed``), so the
    round trip crossed two different encodings.

    The same argument applies here as to the drop-in case.
    """

    def _capture_staged_bytes(self, call, contents: str) -> bytes:
        """Run ``call(contents)`` with the privileged step stubbed, and return
        the raw bytes of the temp file it staged.

        The bytes must be read from inside the stub: both writers unlink the
        temp file in a ``finally``, so by the time the call returns there is
        nothing left to inspect.
        """
        import subprocess
        from pathlib import Path

        staged: dict[str, bytes] = {}

        def _fake_run(argv, *a, **kw):
            # The staged temp path is the second-to-last argv entry for both
            # writers (`install -m MODE -o root -g root <tmp> <dest>`).
            staged["raw"] = Path(argv[-2]).read_bytes()
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with (
            patch("kiro_crew.service.linux.subprocess.run", side_effect=_fake_run),
            patch("kiro_crew.service.linux._privilege_prefix", return_value=["sudo"]),
        ):
            call(contents)
        return staged["raw"]

    def test_a_non_ascii_home_reaches_the_unit_writer(self, monkeypatch):
        """Premise, not a regression pin: the unit is not ASCII by construction.

        ``render_unit`` interpolates the account name and its home directory
        into ``User=``, ``WorkingDirectory=`` and every ``Environment=`` line,
        and both are ordinary UTF-8 filesystem values on Linux. So the string
        handed to the writer can legitimately carry non-ASCII, which is what
        makes the writer's encoding load-bearing rather than cosmetic.
        """
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setenv("USER", "usuário")
        gid_result = MagicMock(returncode=0, stdout="usuário\n", stderr="")
        with (
            patch(
                "kiro_crew.service.common.shutil.which", return_value="/home/usuario/bin/kirocrew"
            ),
            patch("kiro_crew.service.linux.subprocess.run", return_value=gid_result),
            patch("kiro_crew.service.linux._home_for_user", return_value="/home/usuario"),
        ):
            unit = svc_linux.render_unit()

        assert not unit.isascii(), "expected the rendered unit to carry the non-ASCII home"
        assert "User=usuário" in unit

    def test_the_unit_write_stages_utf8_bytes(self):
        """``_write_unit_via_sudo`` must hand ``install`` UTF-8 bytes."""
        from kiro_crew.service import linux as svc_linux

        contents = "[Service]\nWorkingDirectory=/home/usuario\nEnvironment=USER=usuário\n"
        raw = self._capture_staged_bytes(svc_linux._write_unit_via_sudo, contents)

        # Assert the ENCODING, not the line terminator: text mode translates
        # newlines on Windows, which this Linux-only module never sees in
        # production but a developer box does.
        assert "usuário".encode("utf-8") in raw
        assert raw.decode("utf-8").replace("\r\n", "\n") == contents

    def test_the_install_file_write_stages_utf8_bytes(self, tmp_path):
        """``_install_file_via_sudo`` is the same staging shape and the same
        contract. Its one current caller seeds an ASCII template, so this is a
        contract pin rather than a live-harm test -- but the helper takes
        arbitrary ``contents`` and publishes to a root-owned path, so the
        encoding must not be left to the host."""
        from kiro_crew.service import linux as svc_linux

        contents = "# Kiro Crew — überschrift\nKIROCREW_PORT=5477\n"
        raw = self._capture_staged_bytes(
            lambda c: svc_linux._install_file_via_sudo(c, tmp_path / "env"), contents
        )

        assert "— überschrift".encode("utf-8") in raw
        assert raw.decode("utf-8").replace("\r\n", "\n") == contents

    def test_every_text_write_in_the_linux_service_names_its_encoding(self):
        """Ratchet. This is the assertion that goes red on the unfixed tree, and
        it is what stops the seam decaying again -- the two sites here were
        themselves the residue of an earlier pass that fixed a sibling."""
        import importlib
        from pathlib import Path

        from kiro_crew.service import linux as svc_linux

        family = (svc_linux, *map(importlib.import_module, svc_linux._OWNERS))
        source = "".join(Path(module.__file__).read_text(encoding="utf-8") for module in family)
        offenders = _text_writes_missing_encoding(source)

        assert offenders == [], (
            "text-mode open without encoding= in service/linux.py: "
            + "; ".join(offenders)
            + " — systemd reads these files as UTF-8, so the writer must not "
            "depend on the installing host's locale"
        )

    def test_the_encoding_ratchet_can_actually_fail(self):
        """A scan that matches nothing passes as green and proves nothing. Pin
        that the detector fires on a violation and stays quiet on the fixed
        form and on binary mode."""
        offending = 'import os\nwith os.fdopen(fd, "w") as fh:\n    fh.write(x)\n'
        fixed = 'import os\nwith os.fdopen(fd, "w", encoding="utf-8") as fh:\n    fh.write(x)\n'
        binary = 'import os\nwith os.fdopen(fd, "wb") as fh:\n    fh.write(x)\n'

        assert len(_text_writes_missing_encoding(offending)) == 1
        assert _text_writes_missing_encoding(fixed) == []
        assert _text_writes_missing_encoding(binary) == []


class TestMacOSPlistRendering:
    def test_plist_and_unit_never_auto_open_a_browser(self, monkeypatch, tmp_path):
        """Both installers pass `--no-open`.

        A service starts at login, on every KeepAlive respawn, and on every
        `launchctl kickstart` — which is what Dev Fleet's Restart button runs. Without
        the flag each of those opens a new dashboard tab in the default browser. The
        surface the user already has (browser tab or Electron window) reconnects on
        its own, so there is nothing for the service to open.

        Asserted on the plist AND the unit in one test because the flag was missing
        from both: on a headless Linux box the auto-open has nothing to reach, which
        is why the gap survived there unnoticed.
        """
        from kiro_crew.service import linux as svc_linux
        from kiro_crew.service import macos as svc_macos

        monkeypatch.setattr(svc_macos, "LIVE_PROGRAM", tmp_path / "live-gateway")
        with patch(
            "kiro_crew.service.common.shutil.which", return_value="/opt/homebrew/bin/kirocrew"
        ):
            plist = svc_macos.render_plist()
        args = plist.split("<key>ProgramArguments</key>", 1)[1].split("</array>", 1)[0]
        assert "<string>--no-open</string>" in args, (
            "--no-open must be inside ProgramArguments, not merely somewhere in the plist"
        )

        monkeypatch.setenv("USER", "tester")
        gid = MagicMock(returncode=0, stdout="staff\n", stderr="")
        with patch(
            "kiro_crew.service.common.shutil.which", return_value="/usr/local/bin/kirocrew"
        ), patch("kiro_crew.service.linux.subprocess.run", return_value=gid):
            unit = svc_linux.render_unit()
        assert 'ExecStart="/usr/local/bin/kirocrew" gateway --no-open' in unit

    def test_render_plist_runs_the_live_program_not_the_resolved_bin(self, monkeypatch, tmp_path):
        """ProgramArguments[0] is the live-gateway launcher.

        The resolved binary deliberately does NOT appear in the plist: the agent
        runs the launcher so Dev Fleet can repoint it - working directory and
        PATH included - without rewriting and re-bootstrapping the plist (see
        service.common.launchd_live_program).
        """
        from kiro_crew.service import macos as svc_macos

        link = tmp_path / "live-gateway"
        monkeypatch.setattr(svc_macos, "LIVE_PROGRAM", link)
        with patch(
            "kiro_crew.service.common.shutil.which",
            return_value="/opt/homebrew/bin/kirocrew",
        ):
            plist = svc_macos.render_plist()
        assert f"<string>{LAUNCHD_LABEL}</string>" in plist
        assert f"<string>{link}</string>" in plist
        assert "/opt/homebrew/bin/kirocrew" not in plist
        assert "<string>gateway</string>" in plist
        assert "<key>RunAtLoad</key>" in plist
        assert "<key>KeepAlive</key>" in plist

    def test_render_plist_pins_bounded_graceful_restart_contract(self, tmp_path):
        from kiro_crew.gateway_shutdown_budget import TOTAL_SHUTDOWN_BUDGET_SECS
        from kiro_crew.service import macos as svc_macos

        rendered = svc_macos.render_plist()
        payload = plistlib.loads(rendered.encode())
        assert payload["KeepAlive"] is True
        assert payload["ExitTimeOut"] == TOTAL_SHUTDOWN_BUDGET_SECS
        plist = tmp_path / "agent.plist"
        plist.write_text(rendered)
        assert svc_macos.restart_contract_current(plist) is True

    def test_restart_contract_rejects_legacy_and_unbounded_definitions(self, tmp_path):
        from kiro_crew.gateway_shutdown_budget import TOTAL_SHUTDOWN_BUDGET_SECS
        from kiro_crew.service import macos as svc_macos

        plist = tmp_path / "agent.plist"
        for payload in (
            {"KeepAlive": {"SuccessfulExit": False}},
            {"KeepAlive": True},
            {"KeepAlive": True, "ExitTimeOut": 0},
        ):
            plist.write_bytes(plistlib.dumps(payload))
            assert svc_macos.restart_contract_current(plist) is False

        current = (
            f"exit timeout = {TOTAL_SHUTDOWN_BUDGET_SECS}\n"
            "properties = keepalive | runatload\n"
        )
        assert svc_macos.loaded_restart_contract_current(current) is True
        assert svc_macos.loaded_restart_contract_current(
            current.replace("keepalive | ", "")
        ) is False
        assert svc_macos.loaded_restart_contract_current(
            current.replace(str(TOTAL_SHUTDOWN_BUDGET_SECS), "5")
        ) is False

    def test_render_plist_xml_escapes_special_chars(self, monkeypatch, tmp_path):
        """The Program path is XML-escaped.

        It is now the launcher path, which sits under $HOME — a home directory
        containing ``&`` or ``<`` would otherwise emit invalid XML that
        ``launchctl load`` rejects.
        """
        from kiro_crew.service import macos as svc_macos

        monkeypatch.setattr(
            svc_macos, "LIVE_PROGRAM", Path("/path/with/<bad>&chars/live-gateway")
        )
        plist = svc_macos.render_plist()
        assert "<bad>" not in plist
        assert "&chars" not in plist
        assert "&lt;bad&gt;" in plist
        assert "&amp;chars" in plist

    def test_install_writes_plist_and_loads(self, tmp_path, monkeypatch):
        from kiro_crew.service import macos as svc_macos

        plist_dir = tmp_path / "LaunchAgents"
        log_dir = tmp_path / "Logs"
        plist_path = plist_dir / f"{LAUNCHD_LABEL}.plist"
        monkeypatch.setattr(svc_macos, "PLIST_DIR", plist_dir)
        monkeypatch.setattr(svc_macos, "PLIST_PATH", plist_path)
        monkeypatch.setattr(svc_macos, "LOG_DIR", log_dir)
        monkeypatch.setattr(svc_macos, "STDOUT_LOG", log_dir / "gateway.log")
        monkeypatch.setattr(svc_macos, "STDERR_LOG", log_dir / "gateway.err")

        run = MagicMock(returncode=0, stdout="", stderr="")
        with patch(
            "kiro_crew.service.common.shutil.which",
            return_value="/opt/homebrew/bin/kirocrew",
        ), patch("kiro_crew.service.macos.subprocess.run", return_value=run) as proc:
            svc_macos.install()

        assert plist_path.exists()
        called = [c.args[0] for c in proc.call_args_list]
        uid = getattr(os, "getuid", lambda: -1)()
        assert ["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)] in called
