"""Tests for the user-service install/uninstall path.

Two layers tested separately:
  - Pure rendering tests (render_unit / render_plist) — no system calls,
    can run on any platform.
  - Controller dispatch tests — assert that ``current_platform()`` routes
    to the right module and that ``UNSUPPORTED`` produces the expected
    exit code.

Tests do not actually invoke ``systemctl`` or ``launchctl``. The
subprocess calls in :mod:`kiro_crew.service.linux` and
:mod:`kiro_crew.service.macos` are mocked.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.service.common import (
    LAUNCHD_LABEL,
    SERVICE_NAME,
    Platform,
    current_platform,
    kirocrew_bin,
    service_environment,
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
        ):
            unit = svc_linux.render_unit()
        # ExecStart executable is double-quoted (systemd tokenizes on
        # whitespace; a spaced path would otherwise break the exec).
        assert 'ExecStart="/home/u/.toolbox/bin/kirocrew" gateway' in unit
        assert "Restart=on-failure" in unit
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
        service = unit.index("[Service]")
        install = unit.index("[Install]")
        for key in ("HOME", "USER", "PATH", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
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

    def test_uninstall_is_idempotent_when_unit_missing(self, tmp_path, monkeypatch):
        from kiro_crew.service import linux as svc_linux

        # Point UNIT_PATH at a nonexistent file; uninstall should be a no-op.
        unit_path = tmp_path / "missing.service"
        monkeypatch.setattr(svc_linux, "UNIT_PATH", unit_path)
        with patch("kiro_crew.service.linux.subprocess.run") as run:
            svc_linux.uninstall()
        run.assert_not_called()


class TestMacOSPlistRendering:
    def test_render_plist_includes_label_and_program_args(self):
        from kiro_crew.service import macos as svc_macos

        with patch(
            "kiro_crew.service.common.shutil.which",
            return_value="/opt/homebrew/bin/kirocrew",
        ):
            plist = svc_macos.render_plist()
        assert f"<string>{LAUNCHD_LABEL}</string>" in plist
        assert "<string>/opt/homebrew/bin/kirocrew</string>" in plist
        assert "<string>gateway</string>" in plist
        assert "<key>RunAtLoad</key>" in plist
        assert "<key>KeepAlive</key>" in plist

    def test_render_plist_xml_escapes_special_chars(self):
        from kiro_crew.service import macos as svc_macos

        with patch(
            "kiro_crew.service.common.shutil.which",
            return_value="/path/with/<bad>&chars",
        ):
            plist = svc_macos.render_plist()
        # The bad characters should be escaped, not present raw.
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
        assert ["launchctl", "load", "-w", str(plist_path)] in called


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

        with patch(
            "kiro_crew.service.controller.current_platform",
            return_value=Platform.SYSTEMD,
        ), patch.object(svc_linux, "install") as mock_install:
            rc = controller.install_service()
        assert rc == 0
        mock_install.assert_called_once()

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

        with patch(
            "kiro_crew.service.controller.current_platform",
            return_value=Platform.SYSTEMD,
        ), patch.object(svc_linux, "is_active", return_value=False), patch.object(
            svc_linux, "stop"
        ) as mock_stop:
            assert controller.stop_service() is False
        mock_stop.assert_not_called()

    def test_stop_service_returns_true_when_active_systemd(self):
        from kiro_crew.service import controller
        from kiro_crew.service import linux as svc_linux

        with patch(
            "kiro_crew.service.controller.current_platform",
            return_value=Platform.SYSTEMD,
        ), patch.object(svc_linux, "is_active", return_value=True), patch.object(
            svc_linux, "stop"
        ) as mock_stop:
            assert controller.stop_service() is True
        mock_stop.assert_called_once()

    def test_stop_service_routes_to_macos(self):
        from kiro_crew.service import controller
        from kiro_crew.service import macos as svc_macos

        with patch(
            "kiro_crew.service.controller.current_platform",
            return_value=Platform.LAUNCHD,
        ), patch.object(svc_macos, "is_active", return_value=True), patch.object(
            svc_macos, "stop"
        ) as mock_stop:
            assert controller.stop_service() is True
        mock_stop.assert_called_once()

    def test_stop_service_returns_false_when_macos_inactive(self):
        from kiro_crew.service import controller
        from kiro_crew.service import macos as svc_macos

        with patch(
            "kiro_crew.service.controller.current_platform",
            return_value=Platform.LAUNCHD,
        ), patch.object(svc_macos, "is_active", return_value=False), patch.object(
            svc_macos, "stop"
        ) as mock_stop:
            assert controller.stop_service() is False
        mock_stop.assert_not_called()

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

        with patch(
            "kiro_crew.service.controller.current_platform",
            return_value=Platform.SYSTEMD,
        ), patch.object(svc_linux, "is_active", return_value=False), patch.object(
            svc_linux, "restart"
        ) as mock_restart:
            assert controller.restart_service() is False
        mock_restart.assert_not_called()

    def test_restart_service_returns_true_when_active_systemd(self):
        from kiro_crew.service import controller
        from kiro_crew.service import linux as svc_linux

        with patch(
            "kiro_crew.service.controller.current_platform",
            return_value=Platform.SYSTEMD,
        ), patch.object(svc_linux, "is_active", return_value=True), patch.object(
            svc_linux, "restart", return_value=True
        ) as mock_restart:
            assert controller.restart_service() is True
        mock_restart.assert_called_once()

    def test_restart_service_returns_false_when_systemd_restart_fails(self):
        # The core false-success bug: an unprivileged/failed `systemctl
        # restart` exits non-zero, but restart_service() historically returned
        # True regardless (it never checked restart()'s result), printing a
        # bogus success. The controller must propagate the restart outcome so
        # the caller falls back to the foreground path instead of assuming the
        # service manager handled it.
        from kiro_crew.service import controller
        from kiro_crew.service import linux as svc_linux

        with patch(
            "kiro_crew.service.controller.current_platform",
            return_value=Platform.SYSTEMD,
        ), patch.object(svc_linux, "is_active", return_value=True), patch.object(
            svc_linux, "restart", return_value=False
        ) as mock_restart:
            assert controller.restart_service() is False
        mock_restart.assert_called_once()

    def test_restart_service_routes_to_macos(self):
        from kiro_crew.service import controller
        from kiro_crew.service import macos as svc_macos

        with patch(
            "kiro_crew.service.controller.current_platform",
            return_value=Platform.LAUNCHD,
        ), patch.object(svc_macos, "is_active", return_value=True), patch.object(
            svc_macos, "restart", return_value=True
        ) as mock_restart:
            assert controller.restart_service() is True
        mock_restart.assert_called_once()

    def test_restart_service_returns_false_when_macos_restart_fails(self):
        from kiro_crew.service import controller
        from kiro_crew.service import macos as svc_macos

        with patch(
            "kiro_crew.service.controller.current_platform",
            return_value=Platform.LAUNCHD,
        ), patch.object(svc_macos, "is_active", return_value=True), patch.object(
            svc_macos, "restart", return_value=False
        ) as mock_restart:
            assert controller.restart_service() is False
        mock_restart.assert_called_once()

    def test_restart_service_returns_false_when_macos_inactive(self):
        from kiro_crew.service import controller
        from kiro_crew.service import macos as svc_macos

        with patch(
            "kiro_crew.service.controller.current_platform",
            return_value=Platform.LAUNCHD,
        ), patch.object(svc_macos, "is_active", return_value=False), patch.object(
            svc_macos, "restart"
        ) as mock_restart:
            assert controller.restart_service() is False
        mock_restart.assert_not_called()

    def test_restart_service_unsupported_returns_false(self):
        from kiro_crew.service import controller

        with patch(
            "kiro_crew.service.controller.current_platform",
            return_value=Platform.UNSUPPORTED,
        ):
            assert controller.restart_service() is False

    def test_install_systemd_handles_install_error(self, capsys):
        """If linux.install raises ServiceInstallError, controller catches it,
        prints to stderr, and returns 1 — not propagating the exception."""
        from kiro_crew.service import controller
        from kiro_crew.service import linux as svc_linux

        with patch(
            "kiro_crew.service.controller.current_platform",
            return_value=Platform.SYSTEMD,
        ), patch.object(
            svc_linux,
            "install",
            side_effect=svc_linux.ServiceInstallError("simulated failure"),
        ):
            rc = controller.install_service()
        captured = capsys.readouterr()
        assert rc == 1
        assert "simulated failure" in captured.err

    def test_install_routes_to_macos(self, capsys):
        from kiro_crew.service import controller
        from kiro_crew.service import macos as svc_macos

        with patch(
            "kiro_crew.service.controller.current_platform",
            return_value=Platform.LAUNCHD,
        ), patch.object(svc_macos, "install") as mock_install:
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

        with patch(
            "kiro_crew.service.controller.current_platform",
            return_value=Platform.SYSTEMD,
        ), patch.object(svc_linux, "uninstall") as mock_un:
            rc = controller.uninstall_service()
        assert rc == 0
        mock_un.assert_called_once()

    def test_uninstall_routes_to_macos(self):
        from kiro_crew.service import controller
        from kiro_crew.service import macos as svc_macos

        with patch(
            "kiro_crew.service.controller.current_platform",
            return_value=Platform.LAUNCHD,
        ), patch.object(svc_macos, "uninstall") as mock_un:
            rc = controller.uninstall_service()
        assert rc == 0
        mock_un.assert_called_once()

    def test_status_routes_to_systemd_active(self, capsys):
        """status() returns 0 when active, prints the systemctl output."""
        from kiro_crew.service import controller
        from kiro_crew.service import linux as svc_linux

        with patch(
            "kiro_crew.service.controller.current_platform",
            return_value=Platform.SYSTEMD,
        ), patch.object(
            svc_linux, "status", return_value="● kirocrew.service\n"
        ), patch.object(svc_linux, "is_active", return_value=True):
            rc = controller.service_status()
        assert rc == 0
        assert "kirocrew.service" in capsys.readouterr().out

    def test_status_routes_to_systemd_inactive_returns_1(self):
        from kiro_crew.service import controller
        from kiro_crew.service import linux as svc_linux

        with patch(
            "kiro_crew.service.controller.current_platform",
            return_value=Platform.SYSTEMD,
        ), patch.object(svc_linux, "status", return_value=""), patch.object(
            svc_linux, "is_active", return_value=False
        ):
            rc = controller.service_status()
        assert rc == 1

    def test_status_routes_to_macos_active(self, capsys):
        from kiro_crew.service import controller
        from kiro_crew.service import macos as svc_macos

        with patch(
            "kiro_crew.service.controller.current_platform",
            return_value=Platform.LAUNCHD,
        ), patch.object(
            svc_macos, "status", return_value='"PID" = 1234;\n'
        ), patch.object(svc_macos, "is_active", return_value=True):
            rc = controller.service_status()
        assert rc == 0
        assert "PID" in capsys.readouterr().out

    def test_status_routes_to_macos_inactive_returns_1(self):
        from kiro_crew.service import controller
        from kiro_crew.service import macos as svc_macos

        with patch(
            "kiro_crew.service.controller.current_platform",
            return_value=Platform.LAUNCHD,
        ), patch.object(svc_macos, "status", return_value=""), patch.object(
            svc_macos, "is_active", return_value=False
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

        with patch(
            "kiro_crew.service.controller.current_platform",
            return_value=Platform.SYSTEMD,
        ), patch.object(svc_linux, "is_active", return_value=True):
            assert controller.is_service_active() is True

    def test_is_service_active_macos_routes(self):
        from kiro_crew.service import controller
        from kiro_crew.service import macos as svc_macos

        with patch(
            "kiro_crew.service.controller.current_platform",
            return_value=Platform.LAUNCHD,
        ), patch.object(svc_macos, "is_active", return_value=True):
            assert controller.is_service_active() is True


class TestLinuxControlPaths:
    """Cover uninstall, stop, status, is_active, and the sudo helper paths."""

    def test_uninstall_runs_full_teardown_when_unit_exists(self, tmp_path, monkeypatch):
        from kiro_crew.service import linux as svc_linux

        # Point UNIT_PATH at a real temp file so ``UNIT_PATH.exists()``
        # is True without monkeypatching ``Path.exists`` globally (which
        # would also affect pytest/fixture machinery).
        unit_path = tmp_path / "kirocrew.service"
        unit_path.write_text("")
        data_home = tmp_path / "crew-home"
        data_home.mkdir()
        sentinel = data_home / "memory.db"
        sentinel.write_text("user data")
        monkeypatch.setenv("KIROCREW_HOME", str(data_home))
        monkeypatch.setattr(svc_linux, "UNIT_PATH", unit_path)
        ok = MagicMock(returncode=0, stdout="", stderr="")
        with patch(
            "kiro_crew.service.linux.subprocess.run", return_value=ok
        ) as run:
            svc_linux.uninstall()
        called = [list(c.args[0]) for c in run.call_args_list]
        # Each step must use sudo since /etc/systemd/system requires root.
        assert ["sudo", "systemctl", "stop", f"{SERVICE_NAME}.service"] in called
        assert ["sudo", "systemctl", "disable", f"{SERVICE_NAME}.service"] in called
        assert any(
            c[:3] == ["sudo", "rm", "-f"] for c in called
        ), f"expected sudo rm of unit file; got {called}"
        assert ["sudo", "systemctl", "daemon-reload"] in called
        assert sentinel.read_text() == "user data"

    def test_is_active_returns_true_when_systemctl_says_active(self):
        from kiro_crew.service import linux as svc_linux

        active_result = MagicMock(returncode=0, stdout="active\n", stderr="")
        with patch(
            "kiro_crew.service.linux.subprocess.run", return_value=active_result
        ) as run:
            assert svc_linux.is_active() is True
        # is_active must NOT use sudo (status is queryable as a regular user).
        called = [list(c.args[0]) for c in run.call_args_list]
        assert all("sudo" not in c for c in called), (
            f"is_active must not call sudo; got {called}"
        )

    def test_is_active_returns_false_when_inactive(self):
        from kiro_crew.service import linux as svc_linux

        inactive_result = MagicMock(returncode=3, stdout="inactive\n", stderr="")
        with patch(
            "kiro_crew.service.linux.subprocess.run", return_value=inactive_result
        ):
            assert svc_linux.is_active() is False

    def test_stop_invokes_systemctl_stop(self):
        from kiro_crew.service import linux as svc_linux

        ok = MagicMock(returncode=0, stdout="", stderr="")
        with patch(
            "kiro_crew.service.linux.subprocess.run", return_value=ok
        ) as run:
            svc_linux.stop()
        called = [list(c.args[0]) for c in run.call_args_list]
        assert ["sudo", "systemctl", "stop", f"{SERVICE_NAME}.service"] in called

    def test_restart_returns_true_on_success(self):
        from kiro_crew.service import linux as svc_linux

        ok = MagicMock(returncode=0, stdout="", stderr="")
        with patch(
            "kiro_crew.service.linux.subprocess.run", return_value=ok
        ) as run:
            assert svc_linux.restart() is True
        called = [list(c.args[0]) for c in run.call_args_list]
        assert ["sudo", "systemctl", "restart", f"{SERVICE_NAME}.service"] in called

    def test_restart_returns_false_on_nonzero_exit(self):
        # An unprivileged / failed systemctl restart exits non-zero (systemd
        # refuses a system-scope restart without root). restart() must report
        # that failure, not swallow it -- this is the crux of the false-success
        # bug: the outcome has to reach restart_service() and its caller.
        from kiro_crew.service import linux as svc_linux

        failed = MagicMock(returncode=1, stdout="", stderr="Interactive authentication required")
        with patch(
            "kiro_crew.service.linux.subprocess.run", return_value=failed
        ):
            assert svc_linux.restart() is False

    def test_restart_invokes_systemctl_restart_atomic(self):
        # systemctl restart is preferred over stop+start: it's a single
        # atomic operation, smaller down-window, and the supervisor
        # stays in charge of the lifecycle the whole time.
        from kiro_crew.service import linux as svc_linux

        ok = MagicMock(returncode=0, stdout="", stderr="")
        with patch(
            "kiro_crew.service.linux.subprocess.run", return_value=ok
        ) as run:
            svc_linux.restart()
        called = [list(c.args[0]) for c in run.call_args_list]
        assert [
            "sudo", "systemctl", "restart", f"{SERVICE_NAME}.service"
        ] in called
        # And critically, NOT a stop+start pair — that would widen the
        # down-window and lose atomicity.
        assert not any(
            c[:3] == ["sudo", "systemctl", "stop"] for c in called
        ), f"restart() should be atomic, not stop+start; got {called}"

    def test_status_returns_systemctl_output(self):
        from kiro_crew.service import linux as svc_linux

        result = MagicMock(
            returncode=0, stdout="● kirocrew.service - active\n", stderr=""
        )
        with patch(
            "kiro_crew.service.linux.subprocess.run", return_value=result
        ) as run:
            out = svc_linux.status()
        assert "kirocrew.service" in out
        # status() must NOT use sudo.
        called = [list(c.args[0]) for c in run.call_args_list]
        assert all("sudo" not in c for c in called)

    def test_status_falls_back_to_stderr_when_stdout_empty(self):
        from kiro_crew.service import linux as svc_linux

        result = MagicMock(returncode=4, stdout="", stderr="not found\n")
        with patch(
            "kiro_crew.service.linux.subprocess.run", return_value=result
        ):
            out = svc_linux.status()
        assert "not found" in out

    def _run_responder(self, *steps_and_results: tuple):
        """Helper: route subprocess.run by inspecting the command being run.

        Each step is (substring_to_match, result_mock). The first step
        whose substring appears in the command is returned. Anything
        unmatched returns a default-success mock.

        This is more robust than a positional list because ``render_unit``
        also calls ``subprocess.run`` (for ``id -gn``), and the count of
        calls during install is not stable.
        """
        ok = MagicMock(returncode=0, stdout="", stderr="")

        def respond(cmd_list, *_a, **_k):
            # subprocess.run is called positionally as run([...], **kwargs).
            # MagicMock side_effect receives the same args, so cmd_list is
            # the list of argv strings.
            cmd = " ".join(cmd_list) if isinstance(cmd_list, list) else str(cmd_list)
            for needle, result in steps_and_results:
                if needle in cmd:
                    return result
            return ok

        return respond

    def test_install_propagates_failure_at_daemon_reload(self, monkeypatch):
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setenv("USER", "tester")
        reload_failed = MagicMock(
            returncode=1, stdout="", stderr="systemctl: bad config"
        )
        responder = self._run_responder(("daemon-reload", reload_failed))

        with patch(
            "kiro_crew.service.common.shutil.which",
            return_value="/usr/local/bin/kirocrew",
        ), patch(
            "kiro_crew.service.linux.subprocess.run", side_effect=responder
        ):
            with pytest.raises(svc_linux.ServiceInstallError) as exc_info:
                svc_linux.install()
        assert "daemon-reload" in str(exc_info.value)

    def test_install_propagates_failure_at_enable(self, monkeypatch):
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setenv("USER", "tester")
        enable_failed = MagicMock(
            returncode=1, stdout="", stderr="enable failed: unit invalid"
        )
        responder = self._run_responder(
            ("enable", enable_failed),
        )

        with patch(
            "kiro_crew.service.common.shutil.which",
            return_value="/usr/local/bin/kirocrew",
        ), patch(
            "kiro_crew.service.linux.subprocess.run", side_effect=responder
        ):
            with pytest.raises(svc_linux.ServiceInstallError) as exc_info:
                svc_linux.install()
        assert "enable" in str(exc_info.value)

    def test_install_propagates_failure_at_restart(self, monkeypatch):
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setenv("USER", "tester")
        restart_failed = MagicMock(returncode=1, stdout="", stderr="job failed")
        responder = self._run_responder(("restart", restart_failed))

        with patch(
            "kiro_crew.service.common.shutil.which",
            return_value="/usr/local/bin/kirocrew",
        ), patch(
            "kiro_crew.service.linux.subprocess.run", side_effect=responder
        ):
            with pytest.raises(svc_linux.ServiceInstallError) as exc_info:
                svc_linux.install()
        # Error should mention restart and journalctl pointer for debugging.
        msg = str(exc_info.value)
        assert "restart" in msg
        assert "journalctl" in msg

    def test_current_group_falls_back_to_username_when_id_fails(self, monkeypatch):
        """If `id -gn` is missing or errors, fall back to using the username
        as the group name. Better to fail loudly at systemd start than to
        guess wrong here."""
        from kiro_crew.service import linux as svc_linux

        # FileNotFoundError simulates `id` not being on PATH.
        with patch(
            "kiro_crew.service.linux.subprocess.run",
            side_effect=FileNotFoundError("id"),
        ):
            assert svc_linux._current_group("alice") == "alice"


class TestMacOSControlPaths:
    """Cover uninstall, stop, status, is_active for macOS / launchd."""

    def test_install_unloads_existing_plist_before_writing(self, tmp_path, monkeypatch):
        """Re-running install on a host that already has the plist loaded
        should unload first, then write+load. Otherwise the new plist
        wouldn't take effect."""
        from kiro_crew.service import macos as svc_macos

        plist_dir = tmp_path / "LaunchAgents"
        plist_path = plist_dir / f"{LAUNCHD_LABEL}.plist"
        log_dir = tmp_path / "Logs"
        plist_dir.mkdir(parents=True)
        # Pre-create the plist so install hits the unload-first branch.
        plist_path.write_text("<plist/>")
        monkeypatch.setattr(svc_macos, "PLIST_DIR", plist_dir)
        monkeypatch.setattr(svc_macos, "PLIST_PATH", plist_path)
        monkeypatch.setattr(svc_macos, "LOG_DIR", log_dir)
        monkeypatch.setattr(svc_macos, "STDOUT_LOG", log_dir / "gateway.log")
        monkeypatch.setattr(svc_macos, "STDERR_LOG", log_dir / "gateway.err")

        ok = MagicMock(returncode=0, stdout="", stderr="")
        with patch(
            "kiro_crew.service.common.shutil.which",
            return_value="/opt/homebrew/bin/kirocrew",
        ), patch(
            "kiro_crew.service.macos.subprocess.run", return_value=ok
        ) as run:
            svc_macos.install()
        called = [c.args[0] for c in run.call_args_list]
        # The unload must come BEFORE the load for the new plist to take effect.
        unload_idx = next(
            i for i, c in enumerate(called) if c[:2] == ["launchctl", "unload"]
        )
        load_idx = next(
            i for i, c in enumerate(called) if c[:2] == ["launchctl", "load"]
        )
        assert unload_idx < load_idx

    def test_uninstall_unloads_and_removes_plist(self, tmp_path, monkeypatch):
        from kiro_crew.service import macos as svc_macos

        plist_dir = tmp_path / "LaunchAgents"
        plist_path = plist_dir / f"{LAUNCHD_LABEL}.plist"
        plist_dir.mkdir(parents=True)
        plist_path.write_text("<plist/>")
        data_home = tmp_path / "crew-home"
        data_home.mkdir()
        sentinel = data_home / "memory.db"
        sentinel.write_text("user data")
        monkeypatch.setenv("KIROCREW_HOME", str(data_home))
        monkeypatch.setattr(svc_macos, "PLIST_PATH", plist_path)

        ok = MagicMock(returncode=0, stdout="", stderr="")
        with patch(
            "kiro_crew.service.macos.subprocess.run", return_value=ok
        ) as run:
            svc_macos.uninstall()
        assert not plist_path.exists()
        called = [c.args[0] for c in run.call_args_list]
        assert ["launchctl", "unload", "-w", str(plist_path)] in called
        assert sentinel.read_text() == "user data"

    def test_uninstall_idempotent_when_plist_missing(self, tmp_path, monkeypatch):
        from kiro_crew.service import macos as svc_macos

        monkeypatch.setattr(svc_macos, "PLIST_PATH", tmp_path / "missing.plist")
        with patch("kiro_crew.service.macos.subprocess.run") as run:
            svc_macos.uninstall()
        run.assert_not_called()

    def test_is_active_returns_false_when_launchctl_errors(self):
        from kiro_crew.service import macos as svc_macos

        not_loaded = MagicMock(returncode=1, stdout="", stderr="not loaded")
        with patch("kiro_crew.service.macos.subprocess.run", return_value=not_loaded):
            assert svc_macos.is_active() is False

    def test_is_active_returns_true_with_pid_in_output(self):
        from kiro_crew.service import macos as svc_macos

        loaded = MagicMock(
            returncode=0,
            stdout='{\n\t"PID" = 1234;\n\t"Label" = "dev.kirocrew.gateway";\n}\n',
            stderr="",
        )
        with patch("kiro_crew.service.macos.subprocess.run", return_value=loaded):
            assert svc_macos.is_active() is True

    def test_is_active_returns_true_when_loaded_without_pid_line(self):
        """`launchctl list <label>` succeeds even if the agent is loaded
        but not running. We treat that as active so callers don't trip
        over a transient state."""
        from kiro_crew.service import macos as svc_macos

        loaded_no_pid = MagicMock(
            returncode=0,
            stdout='{\n\t"Label" = "dev.kirocrew.gateway";\n}\n',
            stderr="",
        )
        with patch(
            "kiro_crew.service.macos.subprocess.run", return_value=loaded_no_pid
        ):
            assert svc_macos.is_active() is True

    def test_stop_unloads_plist_when_present(self, tmp_path, monkeypatch):
        # ``launchctl stop`` would just send SIGTERM and KeepAlive would
        # restart the agent immediately. ``unload`` (without ``-w``) is
        # the supported way to actually stop the running gateway, while
        # leaving the plist enabled for the next login.
        from kiro_crew.service import macos as svc_macos

        plist_path = tmp_path / "agent.plist"
        plist_path.write_text("<plist/>")
        monkeypatch.setattr(svc_macos, "PLIST_PATH", plist_path)
        ok = MagicMock(returncode=0, stdout="", stderr="")
        with patch(
            "kiro_crew.service.macos.subprocess.run", return_value=ok
        ) as run:
            svc_macos.stop()
        called = [c.args[0] for c in run.call_args_list]
        assert ["launchctl", "unload", str(plist_path)] in called
        # Crucially, we should NOT have called `launchctl stop`.
        assert not any(c[:2] == ["launchctl", "stop"] for c in called)

    def test_stop_no_op_when_plist_absent(self, tmp_path, monkeypatch):
        from kiro_crew.service import macos as svc_macos

        monkeypatch.setattr(svc_macos, "PLIST_PATH", tmp_path / "missing.plist")
        with patch("kiro_crew.service.macos.subprocess.run") as run:
            svc_macos.stop()
        run.assert_not_called()

    def test_restart_unloads_then_loads_plist_when_present(self, tmp_path, monkeypatch):
        # ``launchctl restart`` is deprecated and behaves like ``stop``
        # under KeepAlive (SIGTERM, immediate respawn — no plist re-read).
        # The supported way to actually pick up plist changes is a
        # transient unload+load. Both calls MUST omit ``-w`` so persistent
        # enable state is unchanged (otherwise we'd flip disabled and
        # re-enabled, which is a no-op state-wise but a confusing audit).
        from kiro_crew.service import macos as svc_macos

        plist_path = tmp_path / "agent.plist"
        plist_path.write_text("<plist/>")
        monkeypatch.setattr(svc_macos, "PLIST_PATH", plist_path)
        ok = MagicMock(returncode=0, stdout="", stderr="")
        with patch(
            "kiro_crew.service.macos.subprocess.run", return_value=ok
        ) as run:
            svc_macos.restart()
        called = [c.args[0] for c in run.call_args_list]
        # unload then load — order matters: load before unload would race.
        unload_idx = next(
            i for i, c in enumerate(called) if c[:2] == ["launchctl", "unload"]
        )
        load_idx = next(
            i for i, c in enumerate(called) if c[:2] == ["launchctl", "load"]
        )
        assert unload_idx < load_idx, f"unload must precede load; got {called}"
        # Neither call uses -w (persistent flag).
        assert ["launchctl", "unload", str(plist_path)] in called
        assert ["launchctl", "load", str(plist_path)] in called
        # And we must NOT have called the deprecated `launchctl restart`.
        assert not any(c[:2] == ["launchctl", "restart"] for c in called)

    def test_restart_no_op_when_plist_absent(self, tmp_path, monkeypatch):
        # Restart on an uninstalled service is a no-op rather than an
        # error. The CLI controller decides whether to fall back to the
        # foreground-gateway path; this layer just refuses to invent a
        # plist that doesn't exist.
        from kiro_crew.service import macos as svc_macos

        monkeypatch.setattr(svc_macos, "PLIST_PATH", tmp_path / "missing.plist")
        with patch("kiro_crew.service.macos.subprocess.run") as run:
            svc_macos.restart()
        run.assert_not_called()

    def test_status_returns_launchctl_output_when_loaded(self):
        from kiro_crew.service import macos as svc_macos

        loaded = MagicMock(
            returncode=0,
            stdout='{\n\t"PID" = 1234;\n}\n',
            stderr="",
        )
        with patch("kiro_crew.service.macos.subprocess.run", return_value=loaded):
            out = svc_macos.status()
        assert "PID" in out

    def test_status_returns_friendly_message_when_not_loaded(self):
        from kiro_crew.service import macos as svc_macos

        not_loaded = MagicMock(returncode=1, stdout="", stderr="no entry")
        with patch("kiro_crew.service.macos.subprocess.run", return_value=not_loaded):
            out = svc_macos.status()
        assert "not loaded" in out

    def test_kirocrew_bin_falls_back_to_argv0(self, monkeypatch):
        """If `kirocrew` is not on PATH, kirocrew_bin should resolve
        sys.argv[0] rather than crash."""
        from kiro_crew.service import common as svc_common

        monkeypatch.setattr(sys, "argv", ["/some/path/kirocrew"])
        with patch("kiro_crew.service.common.shutil.which", return_value=None):
            assert "kirocrew" in svc_common.kirocrew_bin()


class TestRestartCommandHint:
    """`restart_command_hint` returns a command that matches how the
    service is actually installed.

    The bug was the update path and the Slack restart-failure hint both
    hardcoding ``systemctl --user restart kirocrew``, which fails on the
    system-level systemd unit. The helper centralises the correct command
    per platform.
    """

    def test_systemd_returns_sudo_systemctl(self, monkeypatch):
        from kiro_crew.service import common as svc_common

        monkeypatch.setattr(
            svc_common, "current_platform", lambda: Platform.SYSTEMD
        )
        assert svc_common.restart_command_hint() == f"sudo systemctl restart {SERVICE_NAME}"

    def test_launchd_returns_service_aware_cli(self, monkeypatch):
        from kiro_crew.service import common as svc_common

        monkeypatch.setattr(
            svc_common, "current_platform", lambda: Platform.LAUNCHD
        )
        assert svc_common.restart_command_hint() == "kirocrew restart"

    def test_unsupported_returns_service_aware_cli(self, monkeypatch):
        from kiro_crew.service import common as svc_common

        monkeypatch.setattr(
            svc_common, "current_platform", lambda: Platform.UNSUPPORTED
        )
        assert svc_common.restart_command_hint() == "kirocrew restart"

    def test_never_returns_broken_user_scope_command(self, monkeypatch):
        """Regression: no platform may emit the broken `systemctl --user`
        string that was filed against."""
        from kiro_crew.service import common as svc_common

        for platform in Platform:
            monkeypatch.setattr(
                svc_common, "current_platform", lambda p=platform: p
            )
            assert "systemctl --user" not in svc_common.restart_command_hint()


class TestKirocrewBinOverride:
    def test_service_bin_override_wins_over_which(self, monkeypatch):
        monkeypatch.setenv("KIROCREW_SERVICE_BIN", "/opt/wrapper/kirocrew")
        with patch(
            "kiro_crew.service.common.shutil.which",
            return_value="/usr/local/bin/kirocrew",
        ):
            assert kirocrew_bin() == "/opt/wrapper/kirocrew"

    def test_falls_back_to_which_when_override_unset(self, monkeypatch):
        monkeypatch.delenv("KIROCREW_SERVICE_BIN", raising=False)
        with patch(
            "kiro_crew.service.common.shutil.which",
            return_value="/usr/local/bin/kirocrew",
        ):
            assert kirocrew_bin() == "/usr/local/bin/kirocrew"

    def test_blank_override_is_ignored(self, monkeypatch):
        monkeypatch.setenv("KIROCREW_SERVICE_BIN", "   ")
        with patch(
            "kiro_crew.service.common.shutil.which",
            return_value="/usr/local/bin/kirocrew",
        ):
            assert kirocrew_bin() == "/usr/local/bin/kirocrew"

    def test_relative_override_is_made_absolute(self, monkeypatch):
        # A relative override would produce an invalid ExecStart/ProgramArguments
        # under launchd/systemd (no meaningful cwd), so it must be absolutised.
        import os

        monkeypatch.setenv("KIROCREW_SERVICE_BIN", "./.venv/bin/kirocrew")
        result = kirocrew_bin()
        assert os.path.isabs(result)
        assert result == os.path.abspath("./.venv/bin/kirocrew")


class TestServiceEnvironment:
    # The pinned UTF-8 locale is platform-specific: en_US.UTF-8 on macOS (BSD
    # libc has no C.UTF-8), C.UTF-8 on Linux (always present on glibc/musl).
    EXPECTED_UTF8 = "en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8"

    def test_always_sets_home_path_and_locale(self, monkeypatch):
        monkeypatch.delenv("LANG", raising=False)
        monkeypatch.delenv("LC_ALL", raising=False)
        monkeypatch.delenv("KIROCREW_KIRO_BIN", raising=False)
        env = service_environment("/home/tester")
        assert env["HOME"] == "/home/tester"
        assert "PATH" in env
        # A valid UTF-8 locale is pinned so subprocesses that read non-ASCII
        # files do not crash under the US-ASCII default codec.
        assert env["LANG"] == self.EXPECTED_UTF8
        assert env["LC_ALL"] == self.EXPECTED_UTF8

    def test_locale_is_pinned_ignoring_installer(self, monkeypatch):
        # The installer's locale is NOT trusted. A UTF-8-named installer locale
        # can still be one the target host never generated (SSH-forwarded
        # LC_ALL=zz_ZZ.UTF-8), where setlocale falls back to C; the fixed
        # platform UTF-8 locale is used regardless.
        monkeypatch.setenv("LANG", "en_GB.UTF-8")
        monkeypatch.setenv("LC_ALL", "zz_ZZ.UTF-8")
        env = service_environment("/home/tester")
        assert env["LANG"] == self.EXPECTED_UTF8
        assert env["LC_ALL"] == self.EXPECTED_UTF8

    def test_locale_is_platform_appropriate(self, monkeypatch):
        # C.UTF-8 is invalid on macOS BSD libc; en_US.UTF-8 is invalid-by-
        # absence on minimal Linux. Assert each platform gets its always-valid
        # UTF-8 locale.
        env = service_environment("/home/tester")
        if sys.platform == "darwin":
            assert env["LANG"] == "en_US.UTF-8"
            assert env["LC_ALL"] == "en_US.UTF-8"
        else:
            assert env["LANG"] == "C.UTF-8"
            assert env["LC_ALL"] == "C.UTF-8"

    def test_propagates_kiro_bin_pin_only_when_set(self, monkeypatch):
        monkeypatch.delenv("KIROCREW_KIRO_BIN", raising=False)
        assert "KIROCREW_KIRO_BIN" not in service_environment("/home/tester")
        monkeypatch.setenv("KIROCREW_KIRO_BIN", "/opt/shim/kiro-cli")
        env = service_environment("/home/tester")
        assert env["KIROCREW_KIRO_BIN"] == "/opt/shim/kiro-cli"

    def test_kiro_bin_pin_is_absolutized(self, monkeypatch):
        # A relative pin is meaningless once the service runs from a different
        # cwd; it must be absolutised like the service-bin override.
        import os

        monkeypatch.setenv("KIROCREW_KIRO_BIN", "./kiro-cli")
        env = service_environment("/home/tester")
        assert os.path.isabs(env["KIROCREW_KIRO_BIN"])
        assert env["KIROCREW_KIRO_BIN"] == os.path.abspath("./kiro-cli")

    def test_non_utf8_installer_locale_not_preserved(self, monkeypatch):
        # LANG=C / POSIX must NOT be preserved: with LC_ALL then explicitly set
        # to it, PEP 538 coercion is suppressed and subprocesses crash on the
        # ASCII codec. The fixed platform UTF-8 locale is used instead.
        for bad in ("C", "POSIX", "en_US"):
            monkeypatch.setenv("LANG", bad)
            monkeypatch.delenv("LC_ALL", raising=False)
            env = service_environment("/home/tester")
            assert env["LANG"] == self.EXPECTED_UTF8, bad
            assert env["LC_ALL"] == self.EXPECTED_UTF8, bad

    def test_plist_includes_locale_and_kiro_bin(self, monkeypatch):
        from kiro_crew.service import macos as svc_macos

        monkeypatch.setenv("KIROCREW_KIRO_BIN", "/opt/shim/kiro-cli")
        monkeypatch.setenv("LANG", "en_US.UTF-8")
        with patch(
            "kiro_crew.service.common.shutil.which",
            return_value="/opt/homebrew/bin/kirocrew",
        ):
            plist = svc_macos.render_plist()
        assert "<key>LANG</key>" in plist
        assert "<key>LC_ALL</key>" in plist
        assert "<key>KIROCREW_KIRO_BIN</key>" in plist
        assert "<string>/opt/shim/kiro-cli</string>" in plist
        assert "<key>HOME</key>" in plist
        assert "<key>PATH</key>" in plist

    def test_unit_includes_locale_and_kiro_bin(self, monkeypatch):
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setenv("USER", "tester")
        monkeypatch.setenv("KIROCREW_KIRO_BIN", "/opt/shim/kiro-cli")
        with patch(
            "kiro_crew.service.common.shutil.which",
            return_value="/usr/local/bin/kirocrew",
        ):
            unit = svc_linux.render_unit()
        # Environment values are double-quoted (systemd tokenizes on whitespace).
        assert 'Environment="USER=tester"\n' in unit
        assert 'Environment="LANG=' in unit
        assert 'Environment="KIROCREW_KIRO_BIN=/opt/shim/kiro-cli"\n' in unit

    def test_unit_quotes_spaced_program_and_env(self, monkeypatch):
        # A spaced KIROCREW_SERVICE_BIN / KIROCREW_KIRO_BIN must not split the
        # ExecStart exec (203/EXEC) or truncate the env value at the space.
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setenv("USER", "tester")
        monkeypatch.setenv("KIROCREW_SERVICE_BIN", "/opt/Kiro Crew/kirocrew")
        monkeypatch.setenv("KIROCREW_KIRO_BIN", "/opt/Kiro Crew/kiro-cli")
        unit = svc_linux.render_unit()
        assert 'ExecStart="/opt/Kiro Crew/kirocrew" gateway' in unit
        assert 'Environment="KIROCREW_KIRO_BIN=/opt/Kiro Crew/kiro-cli"\n' in unit
        # The bare unquoted forms must NOT appear (would break systemd parsing).
        assert "ExecStart=/opt/Kiro Crew/kirocrew gateway" not in unit

    def test_unit_escapes_percent_specifiers(self, monkeypatch):
        # systemd expands %-specifiers (%h=home, %i=instance) in ExecStart /
        # Environment= regardless of quoting; a literal % in a path (e.g. a dir
        # named "100%") must be escaped to %% or the exec targets the wrong path.
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setenv("USER", "tester")
        monkeypatch.setenv("KIROCREW_SERVICE_BIN", "/opt/100%/kirocrew")
        unit = svc_linux.render_unit()
        assert 'ExecStart="/opt/100%%/kirocrew" gateway' in unit
        # The single-% form must NOT survive (systemd would treat %/ as a
        # specifier). Guard against a bare "/opt/100%/kirocrew" in ExecStart.
        assert "/opt/100%/kirocrew" not in unit

    def test_sd_quote_escape_order(self):
        from kiro_crew.service.linux import _sd_quote

        # %% before \\ before \" — a value with all three renders correctly.
        assert _sd_quote("a%b") == '"a%%b"'
        assert _sd_quote('x"y') == '"x\\"y"'
        assert _sd_quote("p\\q") == '"p\\\\q"'

    def test_sd_quote_rejects_control_chars(self):
        # A newline (or other C0/DEL) in a value would break out of the quoted
        # systemd token and let the remainder be parsed as fresh unit
        # directives (e.g. User=root injection into the root-owned unit) — must
        # raise, not escape.
        from kiro_crew.service.linux import _sd_quote

        for bad in ("/opt/x\nUser=root", "a\tb", "a\x00b", "a\x7fb", "a\rb"):
            with pytest.raises(ValueError):
                _sd_quote(bad)

    def test_render_unit_rejects_newline_injection(self, monkeypatch):
        # End-to-end: a newline-bearing override must abort render_unit(), not
        # emit an injectable unit file.
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setenv("USER", "tester")
        monkeypatch.setenv(
            "KIROCREW_SERVICE_BIN", "/opt/x/kirocrew\nUser=root\nExecStart=/evil"
        )
        with pytest.raises(ValueError):
            svc_linux.render_unit()

    def test_plist_program_uses_spaced_override_verbatim(self, monkeypatch):
        # launchd plist ProgramArguments are separate XML <string> elements, so
        # a spaced path needs no quoting — just XML escaping (none needed here).
        from kiro_crew.service import macos as svc_macos

        monkeypatch.setenv("KIROCREW_SERVICE_BIN", "/opt/Kiro Crew/kirocrew")
        plist = svc_macos.render_plist()
        assert "<string>/opt/Kiro Crew/kirocrew</string>" in plist


class TestAppArmorGate:
    """The profile must install ONLY where that mechanism is the one in play.

    Gating on the detected mechanism rather than the distro is deliberate:
    Ubuntu derivatives (Pop!_OS, Mint, Zorin, elementary) inherit the
    restriction and an ID check would miss them, while Debian 13 ships AppArmor
    *without* the restriction and must be left completely alone.
    """

    @staticmethod
    def _gate(monkeypatch, *, lsm="apparmor,capability", sysctl="1", parser="/usr/sbin/apparmor_parser", version=(5, 0)):
        from kiro_crew.service import apparmor as aa

        monkeypatch.setattr(aa, "apparmor_is_active", lambda: "apparmor" in lsm)
        monkeypatch.setattr(aa, "userns_restricted", lambda: sysctl == "1")
        monkeypatch.setattr(aa, "parser_path", lambda: parser)
        monkeypatch.setattr(aa, "parser_version", lambda _p: version)
        return aa

    def test_skips_when_apparmor_is_not_an_active_lsm(self, monkeypatch):
        aa = self._gate(monkeypatch, lsm="selinux,capability")
        needed, reason = aa.should_install()
        assert needed is False
        assert "not an active LSM" in reason

    def test_skips_when_the_sysctl_is_not_one(self, monkeypatch):
        """Debian 13 has AppArmor loaded and is unaffected — the sysctl decides."""
        aa = self._gate(monkeypatch, sysctl="0")
        needed, reason = aa.should_install()
        assert needed is False
        assert "apparmor_restrict_unprivileged_userns" in reason

    def test_skips_when_parser_is_missing(self, monkeypatch):
        aa = self._gate(monkeypatch, parser=None)
        needed, reason = aa.should_install()
        assert needed is False
        assert "apparmor_parser is not installed" in reason

    def test_skips_when_parser_predates_the_userns_rule(self, monkeypatch):
        """The `userns,` rule needs AppArmor 4.x; on 3.x the profile would not compile."""
        aa = self._gate(monkeypatch, version=(3, 0))
        needed, reason = aa.should_install()
        assert needed is False
        assert "older than 4.x" in reason

    def test_proceeds_when_every_condition_holds(self, monkeypatch):
        aa = self._gate(monkeypatch)
        needed, reason = aa.should_install()
        assert needed is True
        assert "userns restricted" in reason

    def test_sysctl_absent_reads_as_unrestricted(self, monkeypatch, tmp_path):
        """An absent knob (Debian, older kernels) must not look like `1`."""
        from kiro_crew.service import apparmor as aa

        monkeypatch.setattr(aa, "_SYSCTL_PATH", tmp_path / "nope")
        assert aa.userns_restricted() is False

    def test_lsm_read_failure_reads_as_inactive(self, monkeypatch, tmp_path):
        from kiro_crew.service import apparmor as aa

        monkeypatch.setattr(aa, "_LSM_PATH", tmp_path / "nope")
        assert aa.apparmor_is_active() is False


class TestAppArmorProfileRendering:
    """The rendered profile's shape is load-bearing for security."""

    def test_has_no_attachment_path(self):
        """A path attachment here would be a privilege leak, not a detail.

        The gateway's interpreter (``~/.kiro/crew-venv/bin/python3``) is a
        SYMLINK to the system python, and AppArmor matches the resolved path. So
        attaching to the venv path silently never matches, and attaching to the
        resolved path grants unprivileged userns to EVERY Python process on the
        host. The profile is therefore named-only and applied by systemd to the
        one unit. This test fails if anyone reintroduces an attachment.
        """
        from kiro_crew.service import apparmor as aa

        text = aa.render_profile("4.0")

        assert f"profile {aa.PROFILE_NAME} flags=(unconfined) {{" in text
        # The declaration line must carry no path between the name and the flags.
        decl = [ln for ln in text.splitlines() if ln.startswith(f"profile {aa.PROFILE_NAME}")]
        assert decl == [f"profile {aa.PROFILE_NAME} flags=(unconfined) {{"]
        # And no interpreter path anywhere in the RULES (comments may explain why).
        body = text.split("{", 1)[1]
        assert "python" not in body
        assert "crew-venv" not in body

    def test_grants_only_userns(self):
        from kiro_crew.service import apparmor as aa

        body = aa.render_profile("4.0").split("{", 1)[1]

        assert "userns," in body
        # No capability/file grants smuggled in alongside.
        assert "capability" not in body
        assert " mr," not in body

    def test_abi_line_matches_the_detected_abi(self):
        from kiro_crew.service import apparmor as aa

        assert "abi <abi/4.0>," in aa.render_profile("4.0")
        assert "abi <abi/5.0>," in aa.render_profile("5.0")

    def test_abi_line_is_omitted_when_none_is_available(self):
        """Declaring an abi file the host lacks makes the profile fail to load."""
        from kiro_crew.service import apparmor as aa

        assert "abi <" not in aa.render_profile(None)

    def test_detect_abi_picks_the_highest_numeric_file(self, monkeypatch, tmp_path):
        """Ubuntu 25.10 ships parser 5.x but only abi/3.0 and abi/4.0 on disk."""
        from kiro_crew.service import apparmor as aa

        for name in ("3.0", "4.0", "4.0-ip", "kernel-5.4-vanilla"):
            (tmp_path / name).write_text("", encoding="utf-8")
        monkeypatch.setattr(aa, "_ABI_DIR", tmp_path)

        assert aa.detect_abi() == "4.0"

    def test_detect_abi_returns_none_without_any_numeric_file(self, monkeypatch, tmp_path):
        from kiro_crew.service import apparmor as aa

        monkeypatch.setattr(aa, "_ABI_DIR", tmp_path / "missing")
        assert aa.detect_abi() is None

    def test_documents_that_removal_rebreaks_the_sandbox(self):
        """The file is the only record a future reader has — it must say why."""
        from kiro_crew.service import apparmor as aa

        text = aa.render_profile("4.0")
        assert "Managed by KiroCrew" in text
        assert "Removing this file" in text


class TestAppArmorInstall:
    """Install must be fail-soft, validate before loading, and verify enforcement."""

    @staticmethod
    def _writers():
        writes: list[tuple[str, str]] = []
        runs: list[tuple[str, ...]] = []

        def write(text, dest):
            writes.append((text, str(dest)))

        def run(*argv):
            runs.append(argv)

        return writes, runs, write, run

    def test_skips_cleanly_when_the_host_does_not_need_it(self, monkeypatch):
        from kiro_crew.service import apparmor as aa

        writes, runs, write, run = self._writers()
        monkeypatch.setattr(aa, "should_install", lambda: (False, "no restriction here"))

        outcome = aa.install(write, run, lambda *_a: (0, ""), 1000, 1000)

        assert outcome.changed is False
        assert outcome.ok is True  # a skip is not a failure
        assert writes == [] and runs == []

    def test_refuses_to_install_a_profile_that_does_not_compile(self, monkeypatch):
        """Loading a broken profile is how you get a service that will not start."""
        from kiro_crew.service import apparmor as aa

        writes, runs, write, run = self._writers()
        monkeypatch.setattr(aa, "should_install", lambda: (True, "restricted"))
        monkeypatch.setattr(aa, "parser_path", lambda: "/usr/sbin/apparmor_parser")
        monkeypatch.setattr(aa, "parser_version", lambda _p: (5, 0))
        monkeypatch.setattr(aa, "detect_abi", lambda: "5.0")
        monkeypatch.setattr(aa, "validate", lambda _p, _t: (False, "syntax error at line 9"))

        outcome = aa.install(write, run, lambda *_a: (0, ""), 1000, 1000)

        assert outcome.ok is False
        assert outcome.changed is False
        assert "did NOT compile" in outcome.message
        assert writes == [] and runs == [], "must not touch the host after a failed validate"

    def test_a_sudo_failure_warns_and_never_raises(self, monkeypatch):
        """An install must never die because a hardening step failed."""
        from kiro_crew.service import apparmor as aa

        monkeypatch.setattr(aa, "should_install", lambda: (True, "restricted"))
        monkeypatch.setattr(aa, "parser_path", lambda: "/usr/sbin/apparmor_parser")
        monkeypatch.setattr(aa, "parser_version", lambda _p: (5, 0))
        monkeypatch.setattr(aa, "detect_abi", lambda: "5.0")
        monkeypatch.setattr(aa, "validate", lambda _p, _t: (True, ""))

        def boom(*_a, **_k):
            raise RuntimeError("sudo: a password is required")

        outcome = aa.install(boom, lambda *_a: None, lambda *_a: (0, ""), 1000, 1000)

        assert outcome.ok is False
        assert outcome.changed is False
        assert "still start" in outcome.message
        assert "fail closed" in outcome.message

    def test_does_not_claim_success_when_enforcement_cannot_be_verified(self, monkeypatch):
        """A profile that loads but does not take effect is worse than none."""
        from kiro_crew.service import apparmor as aa

        writes, runs, write, run = self._writers()
        monkeypatch.setattr(aa, "should_install", lambda: (True, "restricted"))
        monkeypatch.setattr(aa, "parser_path", lambda: "/usr/sbin/apparmor_parser")
        monkeypatch.setattr(aa, "parser_version", lambda _p: (5, 0))
        monkeypatch.setattr(aa, "detect_abi", lambda: "5.0")
        monkeypatch.setattr(aa, "validate", lambda _p, _t: (True, ""))
        monkeypatch.setattr(aa, "verify_enforcement", lambda _c, _u, _g: (False, "probe still fails"))

        outcome = aa.install(write, run, lambda *_a: (0, ""), 1000, 1000)

        assert outcome.changed is True  # the file WAS written
        assert outcome.ok is False
        assert "Not claiming success" in outcome.message

    def test_happy_path_validates_loads_then_verifies(self, monkeypatch):
        from kiro_crew.service import apparmor as aa

        writes, runs, write, run = self._writers()
        order: list[str] = []
        monkeypatch.setattr(aa, "should_install", lambda: (True, "restricted"))
        monkeypatch.setattr(aa, "parser_path", lambda: "/usr/sbin/apparmor_parser")
        monkeypatch.setattr(aa, "parser_version", lambda _p: (5, 0))
        monkeypatch.setattr(aa, "detect_abi", lambda: "5.0")
        monkeypatch.setattr(
            aa, "validate", lambda _p, _t: (order.append("validate"), (True, ""))[1]
        )
        monkeypatch.setattr(
            aa,
            "verify_enforcement",
            lambda _c, _u, _g: (order.append("verify"), (True, None))[1],
        )

        def tracked_write(text, dest):
            order.append("write")
            write(text, dest)

        def tracked_run(*argv):
            order.append("load")
            run(*argv)

        outcome = aa.install(tracked_write, tracked_run, lambda *_a: (0, ""), 1000, 1000)

        assert outcome.ok is True and outcome.changed is True
        assert str(aa.PROFILE_PATH) in outcome.message
        # Validate BEFORE writing, load before verifying.
        assert order == ["validate", "write", "load", "verify"]
        assert writes[0][1] == str(aa.PROFILE_PATH)
        assert runs[0][1:] == ("-r", "-W", str(aa.PROFILE_PATH))

    def test_uninstall_is_a_noop_when_no_profile_is_present(self, monkeypatch, tmp_path):
        from kiro_crew.service import apparmor as aa

        _w, runs, _write, run = self._writers()
        monkeypatch.setattr(aa, "PROFILE_PATH", tmp_path / "absent")

        outcome = aa.uninstall(run)

        assert outcome.changed is False
        assert runs == []

    def test_uninstall_unloads_then_removes(self, monkeypatch, tmp_path):
        """Whatever removes the service removes the grant — no orphaned profile."""
        from kiro_crew.service import apparmor as aa

        profile = tmp_path / aa.PROFILE_NAME
        profile.write_text("profile", encoding="utf-8")
        _w, runs, _write, run = self._writers()
        monkeypatch.setattr(aa, "PROFILE_PATH", profile)
        monkeypatch.setattr(aa, "parser_path", lambda: "/usr/sbin/apparmor_parser")

        outcome = aa.uninstall(run)

        assert outcome.changed is True
        assert runs[0][1:] == ("-R", str(profile))
        assert runs[1] == ("rm", "-f", str(profile))


class TestAppArmorUnitDirective:
    """The unit carries the profile, so it applies to this service only."""

    def test_no_directive_by_default(self, monkeypatch):
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setenv("USER", "tester")
        gid = MagicMock(returncode=0, stdout="tester\n", stderr="")
        with patch(
            "kiro_crew.service.common.shutil.which", return_value="/usr/bin/kirocrew"
        ), patch("kiro_crew.service.linux.subprocess.run", return_value=gid):
            unit = svc_linux.render_unit()

        assert "AppArmorProfile" not in unit

    def test_directive_is_best_effort_when_requested(self, monkeypatch):
        """The "-" prefix matters: a missing profile must not stop the gateway.

        Without it systemd refuses to start the unit when the profile is absent,
        turning a hardening step into an outage. With it the gateway starts and
        simply fails closed per-spawn, which is the pre-existing behaviour.
        """
        from kiro_crew.service import apparmor as aa
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setenv("USER", "tester")
        gid = MagicMock(returncode=0, stdout="tester\n", stderr="")
        with patch(
            "kiro_crew.service.common.shutil.which", return_value="/usr/bin/kirocrew"
        ), patch("kiro_crew.service.linux.subprocess.run", return_value=gid):
            unit = svc_linux.render_unit(aa.PROFILE_NAME)

        assert f"AppArmorProfile=-{aa.PROFILE_NAME}" in unit
        assert "AppArmorProfile=kirocrew" not in unit  # never the hard form


class TestAppArmorNeverFailsTheInstall:
    """A hardening step must not be able to turn a working install into a failure.

    Every other step in ``linux.install()`` is fail-hard; this one deliberately is
    not. A gateway running without the profile is the pre-existing status quo,
    whereas aborting the install because a profile could not be loaded would be a
    regression that leaves the user with no service at all.
    """

    @staticmethod
    def _patched(monkeypatch, outcome):
        from kiro_crew.service import controller
        from kiro_crew.service.common import Platform

        monkeypatch.setattr(controller, "current_platform", lambda: Platform.SYSTEMD)
        monkeypatch.setattr(controller.linux, "install", lambda: outcome)
        return controller

    def test_install_still_succeeds_when_the_profile_fails(self, monkeypatch, capsys):
        from kiro_crew.service.apparmor import ProfileOutcome

        controller = self._patched(
            monkeypatch,
            ProfileOutcome(False, "AppArmor profile could not be installed (boom)", ok=False),
        )

        rc = controller.install_service()

        assert rc == 0, "a failed hardening step must not fail the service install"
        out = capsys.readouterr().out
        assert "kirocrew service installed and started" in out
        assert "⚠️" in out, "the failure must still be surfaced, not swallowed"
        assert "could not be installed" in out

    def test_install_reports_the_profile_on_success(self, monkeypatch, capsys):
        from kiro_crew.service.apparmor import ProfileOutcome

        controller = self._patched(
            monkeypatch, ProfileOutcome(True, "AppArmor profile installed at /etc/apparmor.d/x")
        )

        rc = controller.install_service()

        out = capsys.readouterr().out
        assert rc == 0
        assert "AppArmor profile installed at" in out
        assert "⚠️" not in out

    def test_a_silent_skip_prints_nothing_extra(self, monkeypatch, capsys):
        """On Debian/Arch/RHEL the step must be invisible, not chatty."""
        from kiro_crew.service.apparmor import ProfileOutcome

        controller = self._patched(monkeypatch, ProfileOutcome(False, ""))

        rc = controller.install_service()

        out = capsys.readouterr().out
        assert rc == 0
        assert "AppArmor" not in out
        assert "⚠️" not in out

    def test_uninstall_removes_the_profile_and_still_reports_success(self, monkeypatch, capsys):
        from kiro_crew.service import controller
        from kiro_crew.service.apparmor import ProfileOutcome
        from kiro_crew.service.common import Platform

        removed: list[bool] = []

        def remove():
            removed.append(True)
            return ProfileOutcome(True, "AppArmor profile removed from /etc/apparmor.d/x")

        monkeypatch.setattr(controller, "current_platform", lambda: Platform.SYSTEMD)
        monkeypatch.setattr(controller.linux, "uninstall", lambda: None)
        monkeypatch.setattr(controller.linux, "remove_apparmor_profile", remove)

        rc = controller.uninstall_service()

        assert rc == 0
        assert removed == [True], "uninstall must not leave an orphaned userns grant"
        assert "AppArmor profile removed" in capsys.readouterr().out


class TestEnforcementVerificationIsSafeAndFaithful:
    """Verification must be privileged enough to work, and safe enough to trust.

    Three properties, each of which was a real bug or a real vulnerability:
    it needs privilege to ENTER the profile (bare aa-exec silently execs
    unconfined and yields a false negative); it must not execute anything
    user-writable as root (the venv interpreter is user-writable, so running it
    under sudo is a local privilege escalation); and the probe itself must run
    UNPRIVILEGED or it proves nothing, since root may create namespaces
    regardless of the restriction.
    """

    def test_never_spawns_unprivileged_and_drops_back_to_the_caller(self, monkeypatch):
        from kiro_crew.service import apparmor as aa

        calls: list[tuple[str, ...]] = []
        monkeypatch.setattr(
            aa.subprocess,
            "run",
            lambda *_a, **_k: pytest.fail("verification must not spawn unprivileged"),
        )
        monkeypatch.setattr(aa, "_resolve_trusted", lambda name: f"/usr/bin/{name}")

        def sudo_capture(*argv):
            calls.append(argv)
            return (0, "")

        ok, problem = aa.verify_enforcement(sudo_capture, 1000, 1000)

        assert ok is True and problem is None
        argv = calls[0]
        assert argv[:3] == ("/usr/bin/aa-exec", "-p", aa.PROFILE_NAME)
        # Privilege is dropped back to the invoking user INSIDE the profile.
        assert "/usr/bin/setpriv" in argv
        assert "--reuid=1000" in argv and "--regid=1000" in argv
        assert "--clear-groups" in argv

    def test_uses_a_trusted_python_never_the_user_writable_venv(self, monkeypatch):
        """sys.executable is user-writable; running it under sudo would be an LPE."""
        import sys as _sys

        from kiro_crew.service import apparmor as aa

        calls: list[tuple[str, ...]] = []
        monkeypatch.setattr(aa, "_resolve_trusted", lambda name: f"/usr/bin/{name}")
        aa.verify_enforcement(lambda *argv: (calls.append(argv), (0, ""))[1], 1000, 1000)

        argv = calls[0]
        assert "/usr/bin/python3" in argv
        assert _sys.executable not in argv
        # And the payload must not import our own (user-writable) package.
        assert "kiro_crew" not in " ".join(argv)

    def test_a_missing_trusted_tool_is_inconclusive_not_a_failure_claim(self, monkeypatch):
        from kiro_crew.service import apparmor as aa

        monkeypatch.setattr(aa, "_resolve_trusted", lambda name: None if name == "setpriv" else "/usr/bin/x")

        ok, problem = aa.verify_enforcement(lambda *_a: (0, ""), 1000, 1000)

        assert ok is False
        assert "could not verify" in problem
        assert "setpriv" in problem

    def test_a_failing_probe_inside_the_profile_is_surfaced(self, monkeypatch):
        from kiro_crew.service import apparmor as aa

        monkeypatch.setattr(aa, "_resolve_trusted", lambda name: f"/usr/bin/{name}")

        ok, problem = aa.verify_enforcement(
            lambda *_a: (1, "unshare(CLONE_NEWNS) failed with errno 1 (EPERM)"), 1000, 1000
        )

        assert ok is False
        assert "CLONE_NEWNS" in problem


class TestTrustedToolResolution:
    """Anything handed to sudo must not be resolvable through the user's $PATH."""

    def test_rejects_a_binary_outside_the_trusted_dirs(self, monkeypatch, tmp_path):
        from kiro_crew.service import apparmor as aa

        fake = tmp_path / "apparmor_parser"
        fake.write_text("#!/bin/sh\n", encoding="utf-8")
        fake.chmod(0o755)
        # A PATH-based lookup would find this; a trusted-dir lookup must not.
        monkeypatch.setenv("PATH", f"{tmp_path}:/usr/sbin:/usr/bin")
        monkeypatch.setattr(aa, "_TRUSTED_BIN_DIRS", (str(tmp_path),))

        # Present but user-owned -> refused.
        assert aa._resolve_trusted("apparmor_parser") is None

    def test_rejects_a_group_or_world_writable_binary(self, monkeypatch, tmp_path):
        from kiro_crew.service import apparmor as aa

        target = tmp_path / "aa-exec"
        target.write_text("#!/bin/sh\n", encoding="utf-8")
        target.chmod(0o777)
        monkeypatch.setattr(aa, "_TRUSTED_BIN_DIRS", (str(tmp_path),))

        assert aa._resolve_trusted("aa-exec") is None

    def test_missing_binary_returns_none(self, monkeypatch, tmp_path):
        from kiro_crew.service import apparmor as aa

        monkeypatch.setattr(aa, "_TRUSTED_BIN_DIRS", (str(tmp_path),))
        assert aa._resolve_trusted("nope") is None

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="asserts POSIX ownership/permission semantics on a real system binary; "
        "Windows has neither /bin/sh nor a root uid, and the AppArmor path is Linux-only",
    )
    def test_resolves_a_real_root_owned_system_binary(self):
        """Against the real filesystem, not a fixture: /bin/sh must resolve."""
        from kiro_crew.service import apparmor as aa

        resolved = aa._resolve_trusted("sh")
        assert resolved is not None and resolved.startswith("/")


class TestProfileLoadsBeforeTheServiceStarts:
    """Ordering is load-bearing: the directive only applies at unit START.

    Loading the profile after `systemctl restart` leaves the FIRST gateway process
    unprofiled, so every agent spawn fails closed until someone restarts again —
    which is exactly the state this feature exists to prevent.
    """

    def test_profile_is_installed_before_daemon_reload_and_restart(self, monkeypatch):
        from kiro_crew.service import apparmor as aa
        from kiro_crew.service import linux as svc_linux

        order: list[str] = []
        monkeypatch.setenv("USER", "tester")
        monkeypatch.setattr(svc_linux, "_current_user", lambda: "tester")
        monkeypatch.setattr(svc_linux, "render_unit", lambda *_a: "unit")
        monkeypatch.setattr(aa, "should_install", lambda: (True, "restricted"))
        monkeypatch.setattr(
            svc_linux,
            "_write_unit_via_sudo",
            lambda _c: (order.append("write-unit"), MagicMock(returncode=0))[1],
        )
        monkeypatch.setattr(
            svc_linux,
            "install_apparmor_profile",
            lambda: (order.append("load-profile"), aa.ProfileOutcome(True, "installed"))[1],
        )
        monkeypatch.setattr(
            svc_linux,
            "_systemctl",
            lambda *args: (order.append(args[0]), MagicMock(returncode=0))[1],
        )

        outcome = svc_linux.install()

        assert outcome.ok is True
        assert order == ["write-unit", "load-profile", "daemon-reload", "enable", "restart"], order
        # The profile must be loaded strictly before the unit is started.
        assert order.index("load-profile") < order.index("restart")
