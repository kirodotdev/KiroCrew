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

import os
import plistlib
import sys
from pathlib import Path
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


@pytest.fixture(autouse=True)
def _clear_sudo_user(monkeypatch):
    """Keep ``User=`` resolution deterministic across hosts.

    ``_current_user()`` prefers ``SUDO_USER`` (so ``sudo … service install``
    targets the human, not root). A CI runner that happened to set ``SUDO_USER``
    would otherwise override the ``USER=tester`` these tests set. Clear it once
    for every test in this module; tests that exercise the SUDO_USER path set it
    explicitly themselves.
    """
    monkeypatch.delenv("SUDO_USER", raising=False)


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
        ):
            unit = svc_linux.render_unit()
        # ExecStart executable is double-quoted (systemd tokenizes on
        # whitespace; a spaced path would otherwise break the exec). `--no-open`
        # is asserted as part of the SAME string rather than separately: a bare
        # `in unit` check for the prefix passes even if the flag is dropped,
        # because the prefix is still a substring of the shorter line.
        assert 'ExecStart="/home/u/.toolbox/bin/kirocrew" gateway --no-open' in unit
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

    def test_uninstall_is_idempotent_when_unit_missing(self, tmp_path, monkeypatch):
        from kiro_crew.service import linux as svc_linux

        # Point UNIT_PATH at a nonexistent file; uninstall should be a no-op.
        unit_path = tmp_path / "missing.service"
        monkeypatch.setattr(svc_linux, "UNIT_PATH", unit_path)
        with patch("kiro_crew.service.linux.subprocess.run") as run:
            svc_linux.uninstall()
        run.assert_not_called()


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
        used to crash with an uncaught FileNotFoundError. It must raise the
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

        def _boom(*_a, **_k):
            raise FileNotFoundError("sudo")

        monkeypatch.setattr(svc_linux.subprocess, "run", _boom)
        res = svc_linux._sudo_run("systemctl", "restart", "kirocrew.service")
        assert res.returncode == 127
        # restart() surfaces the failure as False rather than crashing.
        assert svc_linux.restart() is False


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
        monkeypatch.setattr(svc_linux, "_systemctl", lambda *a, **k: MagicMock(returncode=0))
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
        monkeypatch.setattr(svc_linux, "_systemctl", lambda *a, **k: MagicMock(returncode=0))
        svc_linux.uninstall()
        assert str(env_file) in removed


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

    def test_uninstall_systemd_handles_service_install_error(self, capsys):
        """uninstall() needs root to remove the root-owned unit, so it can raise
        ServiceInstallError on a non-root host without sudo. The controller must
        catch it and return non-zero, not let a traceback escape."""
        from kiro_crew.service import controller
        from kiro_crew.service import linux as svc_linux

        with patch(
            "kiro_crew.service.controller.current_platform",
            return_value=Platform.SYSTEMD,
        ), patch.object(
            svc_linux, "uninstall", side_effect=svc_linux.ServiceInstallError("needs sudo")
        ):
            rc = controller.uninstall_service()
        assert rc == 1
        assert "needs sudo" in capsys.readouterr().err

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

    def test_restart_kickstarts_the_service_target(self, tmp_path, monkeypatch):
        # ``launchctl restart`` is deprecated and behaves like ``stop`` under
        # KeepAlive (SIGTERM, immediate respawn — no plist re-read). The former
        # implementation used a transient unload+load, which cannot be issued
        # from INSIDE the gateway: the unload SIGTERMs the caller, so the load
        # never runs and the agent stays down. ``kickstart -k`` is performed by
        # launchd itself, so it survives the caller's death — the property Dev
        # Fleet's Restart control depends on.
        from kiro_crew.service import macos as svc_macos

        plist_path = tmp_path / "agent.plist"
        plist_path.write_text("<plist/>")
        monkeypatch.setattr(svc_macos, "PLIST_PATH", plist_path)
        ok = MagicMock(returncode=0, stdout="", stderr="")
        with patch(
            "kiro_crew.service.macos.subprocess.run", return_value=ok
        ) as run:
            assert svc_macos.restart() is True
        called = [c.args[0] for c in run.call_args_list]
        assert len(called) == 1, "restart must be a single launchd operation"
        argv = called[0]
        assert argv[:3] == ["launchctl", "kickstart", "-k"]
        # Addressed as gui/<uid>/<label>, what the modern verbs require.
        assert argv[3].startswith("gui/")
        assert argv[3].endswith(f"/{LAUNCHD_LABEL}")
        # No unload anywhere: an unload would kill the caller mid-restart.
        assert not any(c[:2] == ["launchctl", "unload"] for c in called)

    def test_restart_is_false_when_launchd_rejects_it(self, tmp_path, monkeypatch):
        """A rejected kickstart must not be reported as a restart."""
        from kiro_crew.service import macos as svc_macos

        plist_path = tmp_path / "agent.plist"
        plist_path.write_text("<plist/>")
        monkeypatch.setattr(svc_macos, "PLIST_PATH", plist_path)
        bad = MagicMock(returncode=1, stdout="", stderr="no such service")
        with patch("kiro_crew.service.macos.subprocess.run", return_value=bad):
            assert svc_macos.restart() is False

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

    def test_propagates_port_only_when_set(self, monkeypatch):
        """KIROCREW_PORT reaches the installed service.

        It is the ONLY input DASHBOARD_PORT reads, so a service definition that
        cannot carry it can only ever bind the default 5476 — broken by
        construction on any host where that port is taken, which includes every
        host running Kiro Crew's own instance tunnel (it pins
        local_port == remote_port).
        """
        monkeypatch.delenv("KIROCREW_PORT", raising=False)
        assert "KIROCREW_PORT" not in service_environment("/home/tester")
        monkeypatch.setenv("KIROCREW_PORT", "5477")
        assert service_environment("/home/tester")["KIROCREW_PORT"] == "5477"

    def test_port_reaches_both_rendered_service_definitions(self, monkeypatch, tmp_path):
        """End-to-end, not just present in the dict.

        `service_environment()` feeds the launchd plist's EnvironmentVariables and
        the systemd unit's Environment= lines. Asserting only the dict would pass
        even if a renderer dropped the key on the way out.
        """
        from kiro_crew.service import linux as svc_linux
        from kiro_crew.service import macos as svc_macos

        monkeypatch.setenv("KIROCREW_PORT", "5477")
        monkeypatch.setattr(svc_macos, "LIVE_PROGRAM", tmp_path / "live-gateway")
        with patch(
            "kiro_crew.service.common.shutil.which", return_value="/opt/homebrew/bin/kirocrew"
        ):
            plist = svc_macos.render_plist()
        envs = plist.split("<key>EnvironmentVariables</key>", 1)[1].split("</dict>", 1)[0]
        assert "<key>KIROCREW_PORT</key>" in envs and "<string>5477</string>" in envs

        monkeypatch.setenv("USER", "tester")
        gid = MagicMock(returncode=0, stdout="staff\n", stderr="")
        with patch(
            "kiro_crew.service.common.shutil.which", return_value="/usr/local/bin/kirocrew"
        ), patch("kiro_crew.service.linux.subprocess.run", return_value=gid):
            unit = svc_linux.render_unit()
        assert "KIROCREW_PORT=5477" in unit

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

    @pytest.mark.skipif(
        os.name != "posix",
        reason=(
            "creates a real symlink to exercise the launchd live-gateway link; "
            "the code under test is macOS-only and Windows has no unprivileged "
            "symlink creation. Still runs on Linux CI and on the macOS job "
            "(which now includes test_service.py), so coverage is not lost."
        ),
    )
    def test_live_program_quotes_a_spaced_override(self, monkeypatch, tmp_path):
        # The resolved binary now goes into a generated shell script, so a spaced
        # path must be QUOTED there or the launcher would exec the wrong argv.
        # Compared against what kirocrew_bin() actually returned rather than a
        # literal, so the test does not re-encode one platform's path shape.
        from kiro_crew.service import macos as svc_macos

        link = tmp_path / "live-gateway"
        monkeypatch.setattr(svc_macos, "LIVE_PROGRAM", link)
        monkeypatch.setenv("KIROCREW_SERVICE_BIN", "/opt/Kiro Crew/kirocrew")
        resolved = svc_macos.kirocrew_bin()
        assert " " in resolved, "the spaced override must survive resolution"
        svc_macos.write_live_program(svc_macos.render_live_program(resolved))
        script = link.read_text()
        assert f"exec '{resolved}' \"$@\"" in script
        assert os.access(link, os.X_OK), "launchd must be able to exec it"

    def test_live_program_escapes_a_single_quote_in_the_path(self):
        # A path containing ' would otherwise terminate the shell quoting and
        # turn the rest of the path into separate argv words.
        from kiro_crew.service import macos as svc_macos

        script = svc_macos.render_live_program("/opt/it's/kirocrew")
        assert """exec '/opt/it'\\''s/kirocrew' "$@\"""" in script

    @pytest.mark.skipif(
        os.name != "posix",
        reason="real-symlink test for macOS-only code; see the test above",
    )
    def test_write_live_program_is_atomic_and_leaves_no_temp_files(self, monkeypatch, tmp_path):
        """Rewriting must never expose a partial or non-executable launcher.

        The agent can be kickstarted at any moment, so the write goes through a
        temp sibling that is chmod'd before the rename.
        """
        from kiro_crew.service import macos as svc_macos

        link = tmp_path / "live-gateway"
        monkeypatch.setattr(svc_macos, "LIVE_PROGRAM", link)
        svc_macos.write_live_program(svc_macos.render_live_program("/first/kirocrew"))
        svc_macos.write_live_program(svc_macos.render_live_program("/second/kirocrew"))
        assert "'/second/kirocrew'" in link.read_text()
        assert "'/first/kirocrew'" not in link.read_text()
        assert os.access(link, os.X_OK)
        # No temp siblings left behind.
        assert [p.name for p in tmp_path.iterdir()] == ["live-gateway"]


class TestEnsureLiveProgram:
    """Self-heal for a deleted launchd launcher.

    The repair has to be surgical: re-running `service install` also restores the
    launcher, but it rewrites the plist and so throws away operator-added
    EnvironmentVariables. These lock in that only the launcher half moves, and
    that the helper stays quiet where there is no agent to repair.
    """

    def _agent(self, monkeypatch, tmp_path, *, indirected=True, fmt=None):
        from kiro_crew.service import macos as svc_macos

        launcher = tmp_path / "live-gateway"
        plist = tmp_path / "dev.kirocrew.gateway.plist"
        target = str(launcher) if indirected else "/usr/local/bin/kirocrew"
        # A REAL plist, in either wire format: launchd accepts XML and binary
        # alike, and the reconcile must not care which one it is handed.
        plist.write_bytes(plistlib.dumps(
            {
                "Label": "dev.kirocrew.gateway",
                "EnvironmentVariables": {"KIROCREW_PORT": "5477"},
                "ProgramArguments": [target, "gateway", "--no-open"],
            },
            fmt=fmt or plistlib.FMT_XML,
        ))
        monkeypatch.setattr(svc_macos, "LIVE_PROGRAM", launcher)
        monkeypatch.setattr(svc_macos, "PLIST_PATH", plist)
        return svc_macos, launcher, plist

    def test_reads_a_binary_plist_rather_than_assuming_utf8_text(
        self, monkeypatch, tmp_path
    ):
        """launchd plists are legitimately binary or UTF-16.

        This runs during gateway startup, so decoding one as UTF-8 text would
        raise UnicodeDecodeError and take the whole gateway down over a check
        that only decides whether to rewrite a launcher.
        """
        svc, launcher, _plist = self._agent(
            monkeypatch, tmp_path, fmt=plistlib.FMT_BINARY
        )
        monkeypatch.setenv(
            "KIROCREW_SERVICE_BIN", str(self._exe(tmp_path / "bin" / "kirocrew"))
        )

        assert svc.ensure_live_program() is True
        assert launcher.exists()

    def test_writes_nothing_when_the_plist_is_not_a_plist(self, monkeypatch, tmp_path):
        """Garbage at the plist path must be declined, not raised through."""
        svc, launcher, plist = self._agent(monkeypatch, tmp_path)
        plist.write_bytes(b"\x00\x01 not a plist at all")

        assert svc.ensure_live_program() is False
        assert not launcher.exists()

    def test_a_malformed_xml_plist_cannot_take_the_gateway_down(
        self, monkeypatch, tmp_path
    ):
        """An unescaped `&` in a hand-added value is the ordinary way to get one.

        plistlib surfaces that as xml.parsers.expat.ExpatError, whose base is
        Exception — and this runs at gateway startup, so anything escaping here is
        a crash loop under launchd KeepAlive rather than a skipped check.
        """
        svc, launcher, plist = self._agent(monkeypatch, tmp_path)
        plist.write_bytes(
            b'<?xml version="1.0"?><plist version="1.0"><dict>'
            b"<key>Label</key><string>a & b</string></dict></plist>"
        )

        assert svc.ensure_live_program() is False
        assert not launcher.exists()

    def test_a_plist_whose_root_is_an_array_is_declined(self, monkeypatch, tmp_path):
        """A plist root may legally be an array, which has no .get()."""
        svc, launcher, plist = self._agent(monkeypatch, tmp_path)
        plist.write_bytes(plistlib.dumps(["not", "a", "dict"]))

        assert svc.ensure_live_program() is False
        assert not launcher.exists()

    def test_a_non_list_program_arguments_is_declined(self, monkeypatch, tmp_path):
        """ProgramArguments carries no type guarantee either."""
        svc, launcher, plist = self._agent(monkeypatch, tmp_path)
        plist.write_bytes(plistlib.dumps({"ProgramArguments": "just a string"}))

        assert svc.ensure_live_program() is False
        assert not launcher.exists()

    @staticmethod
    def _exe(path: Path) -> Path:
        """An executable stub — the repair now refuses a non-executable target."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)  # fmt: skip
        return path

    def test_restores_a_deleted_launcher_and_leaves_the_plist_untouched(
        self, monkeypatch, tmp_path
    ):
        svc, launcher, plist = self._agent(monkeypatch, tmp_path)
        pinned = self._exe(tmp_path / "opt" / "kirocrew")
        monkeypatch.setenv("KIROCREW_SERVICE_BIN", str(pinned))
        # Read the target back off the resolver rather than restating a literal:
        # kirocrew_bin() absolutizes, so the path SHAPE differs per platform
        # (Windows resolves a rooted POSIX path to a drive-qualified one).
        resolved = svc.kirocrew_bin()
        before = plist.read_bytes()

        assert svc.ensure_live_program() is True

        assert f"exec '{resolved}' \"$@\"" in launcher.read_text()
        assert os.access(launcher, os.X_OK), "launchd must be able to exec it"
        # The whole reason this exists rather than deferring to `service install`.
        assert plist.read_bytes() == before
        assert b"5477" in plist.read_bytes()

    @pytest.mark.skipif(
        os.name != "posix",
        reason="os.access(X_OK) has no permission meaning for a .py file on "
               "Windows, and this reconcile only ever runs on darwin",
    )
    def test_refuses_to_write_a_launcher_that_execs_a_non_executable(
        self, monkeypatch, tmp_path
    ):
        """`python -m kiro_crew` with no console script resolves to `__main__.py`.

        Writing that would leave launchd unable to spawn the agent AND suppress
        every later repair, since the launcher would then exist — the self-heal
        would cement the broken state. Refusing keeps the next run able to fix it.
        """
        svc, launcher, _plist = self._agent(monkeypatch, tmp_path)
        not_exec = tmp_path / "pkg" / "__main__.py"
        not_exec.parent.mkdir()
        not_exec.write_text("# a module, not a program\n")
        monkeypatch.setenv("KIROCREW_SERVICE_BIN", str(not_exec))

        with pytest.raises(OSError, match="not an executable file"):
            svc.ensure_live_program()

        assert not launcher.exists(), "a later repair must still be possible"

    def test_refuses_rather_than_falling_back_to_path(self, monkeypatch, tmp_path):
        """No sibling script and no override: refuse, never resolve through PATH.

        PATH cannot answer "which install is running", so an unrelated or older
        `kirocrew` ahead of this one would be persisted into the agent — the
        mismatch this repair exists to end, recreated by the repair.
        """
        svc, launcher, _plist = self._agent(monkeypatch, tmp_path)
        monkeypatch.delenv("KIROCREW_SERVICE_BIN", raising=False)
        stray = self._exe(tmp_path / "stray" / "kirocrew")
        from kiro_crew.service import common as svc_common
        monkeypatch.setattr(svc_common.shutil, "which", lambda _n: str(stray))
        # An interpreter directory with NO kirocrew beside it.
        bare = tmp_path / "bare"
        bare.mkdir()
        monkeypatch.setattr(svc.sys, "executable", str(bare / "python"))

        with pytest.raises(OSError, match="no kirocrew console script"):
            svc.ensure_live_program()

        assert not launcher.exists()

    def test_targets_the_repairing_install_not_whatever_path_finds(
        self, monkeypatch, tmp_path
    ):
        """A stray `kirocrew` earlier on PATH must not be baked into the launcher.

        Restoring the agent onto some OTHER install is a quieter version of the
        mismatch this repair exists to end, so the target comes from the running
        interpreter rather than from PATH resolution.
        """
        svc, launcher, _plist = self._agent(monkeypatch, tmp_path)
        monkeypatch.delenv("KIROCREW_SERVICE_BIN", raising=False)
        stray = self._exe(tmp_path / "stray" / "kirocrew")
        # kirocrew_bin() lives in service.common and resolves through ITS shutil.
        from kiro_crew.service import common as svc_common
        monkeypatch.setattr(svc_common.shutil, "which", lambda _n: str(stray))
        # The console script that ships beside the running interpreter.
        mine = self._exe(
            tmp_path / "mine" / ("kirocrew.exe" if os.name == "nt" else "kirocrew")
        )
        monkeypatch.setattr(svc.sys, "executable", str(mine.parent / "python"))

        assert svc.ensure_live_program() is True

        script = launcher.read_text()
        assert str(mine) in script
        assert str(stray) not in script

    def test_an_explicit_service_bin_override_still_wins(self, monkeypatch, tmp_path):
        """Pinning the service Program is operator intent, not PATH shadowing."""
        svc, launcher, _plist = self._agent(monkeypatch, tmp_path)
        pinned = self._exe(tmp_path / "pinned" / "kirocrew")
        monkeypatch.setenv("KIROCREW_SERVICE_BIN", str(pinned))
        resolved = svc.kirocrew_bin()

        assert svc.ensure_live_program() is True

        assert f"exec '{resolved}' \"$@\"" in launcher.read_text()

    def test_is_a_noop_when_the_launcher_is_already_there(self, monkeypatch, tmp_path):
        """An existing launcher may carry a Dev Fleet cutover — never clobber it."""
        svc, launcher, _plist = self._agent(monkeypatch, tmp_path)
        launcher.write_text("#!/bin/sh\ncd '/wt/live' || exit 1\nexec '/wt/live/.venv/bin/kirocrew' \"$@\"\n")

        assert svc.ensure_live_program() is False
        assert "/wt/live" in launcher.read_text()

    def test_writes_nothing_when_no_agent_is_installed(self, monkeypatch, tmp_path):
        """No plist means no job whose launcher this would be — writing it is litter."""
        svc, launcher, plist = self._agent(monkeypatch, tmp_path)
        plist.unlink()

        assert svc.ensure_live_program() is False
        assert not launcher.exists()

    def test_writes_nothing_when_the_agent_bypasses_the_launcher(
        self, monkeypatch, tmp_path
    ):
        """An older agent execs the binary directly; a launcher it never runs is litter."""
        svc, launcher, _plist = self._agent(monkeypatch, tmp_path, indirected=False)

        assert svc.ensure_live_program() is False
        assert not launcher.exists()


class TestLauncherReconcileIsProductionOnly:
    """Only the real instance may repair the shared launchd launcher.

    LIVE_PROGRAM is a per-user path that KIROCREW_HOME does not scope, so a dev,
    pod, or worktree gateway "repairing" it would repoint the user's REAL agent
    at its own venv — the serving-vs-managed mismatch the reconcile exists to
    prevent, authored by the reconcile itself.
    """

    def test_the_default_home_on_darwin_reconciles(self, monkeypatch):
        from kiro_crew import cli_server

        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setattr(cli_server.sys, "platform", "darwin")

        assert cli_server._should_reconcile_launchd_launcher() is True

    def test_an_isolated_home_does_not_reconcile(self, monkeypatch, tmp_path):
        from kiro_crew import cli_server

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / ".kirocrew-dev"))
        monkeypatch.setattr(cli_server.sys, "platform", "darwin")

        assert cli_server._should_reconcile_launchd_launcher() is False

    def test_non_darwin_never_reconciles(self, monkeypatch):
        from kiro_crew import cli_server

        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setattr(cli_server.sys, "platform", "linux")

        assert cli_server._should_reconcile_launchd_launcher() is False

    def test_a_frozen_build_never_reconciles(self, monkeypatch):
        """The packaged app must not own this artifact.

        launchd would run the bundled interpreter WITHOUT the environment the app
        supplies it — notably PYTHONPYCACHEPREFIX — so bytecode would land inside
        the signed bundle and invalidate its signature. The launchd agent belongs
        to a `service install`, not to an app that manages its own backend.
        """
        from kiro_crew import cli_server

        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setattr(cli_server.sys, "platform", "darwin")
        monkeypatch.setattr(cli_server.sys, "frozen", True, raising=False)

        assert cli_server._should_reconcile_launchd_launcher() is False


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


@pytest.fixture
def durable_dir(tmp_path, monkeypatch):
    """A ``tmp_path`` that :func:`validate_exec_path` will accept.

    Two checks legitimately refuse a pytest temp directory, and both are refusing
    correctly, so tests exercising the OTHER rules neutralise them rather than
    skipping on the platform this feature ships on:

    * the ``/tmp`` prefix denylist — on Linux ``tmp_path`` lives under ``/tmp``;
    * the mode walk — ``/tmp`` itself is 0o1777, and it is an ancestor.

    ``test_rejects_a_world_writable_location`` and the ``_substitutable_by_others``
    tests deliberately do NOT use this fixture: they assert the refusals.
    """
    from kiro_crew.service import apparmor as aa

    monkeypatch.setattr(aa, "_UNSAFE_EXEC_PARENTS", ())
    monkeypatch.setattr(aa, "_substitutable_by_others", lambda _p: None)
    return tmp_path


# The whole feature is Linux-only (one Ubuntu kernel restriction), but the backend
# test suite also runs on Windows, where these POSIX paths are not absolute, do
# not resolve, and render with backslashes.
posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX path semantics; the feature is Linux-only"
)


class TestLauncherExecPathIsSafeToAttach:
    """An attachment is a permission grant keyed on a path.

    These are the two rules the whole direct-launch feature rests on: the path
    must not be substitutable by another local user, and it must not be shared
    with unrelated programs. Everything else in the launcher profile is cosmetic
    by comparison, so each rejection is pinned here.
    """

    def test_rejects_a_relative_path(self):
        from kiro_crew.service import apparmor as aa

        resolved, problem = aa.validate_exec_path("kirocrew.AppImage")

        assert resolved is None
        assert "absolute" in problem

    def test_rejects_an_empty_path(self):
        from kiro_crew.service import apparmor as aa

        assert aa.validate_exec_path("   ")[0] is None

    def test_rejects_a_path_that_does_not_exist(self, tmp_path):
        from kiro_crew.service import apparmor as aa

        resolved, problem = aa.validate_exec_path(str(tmp_path / "nope.AppImage"))

        assert resolved is None
        assert "could not be resolved" in problem

    def test_rejects_a_directory(self, tmp_path):
        from kiro_crew.service import apparmor as aa

        resolved, problem = aa.validate_exec_path(str(tmp_path))

        assert resolved is None
        assert "not a regular file" in problem

    @posix_only
    def test_rejects_a_world_writable_location(self, tmp_path, monkeypatch):
        """/tmp and friends: another local user could put their file there.

        The constant is monkeypatched rather than writing to the real /tmp so the
        assertion holds on macOS too, where /tmp resolves to /private/tmp.
        """
        from kiro_crew.service import apparmor as aa

        app = tmp_path / "kirocrew.AppImage"
        app.write_text("#!/bin/sh\n")
        monkeypatch.setattr(aa, "_UNSAFE_EXEC_PARENTS", (str(tmp_path) + "/",))

        resolved, problem = aa.validate_exec_path(str(app))

        assert resolved is None
        assert "any local user can" in problem
        assert "Move" in problem, "must tell the user what to do instead"

    def test_rejects_the_appimage_runtime_mount(self):
        """/tmp/.mount_XXXXXX is a fresh random path every launch."""
        from kiro_crew.service import apparmor as aa

        assert "/tmp/" in aa._UNSAFE_EXEC_PARENTS

    @pytest.mark.parametrize(
        "path",
        [
            "/usr/bin/python3",
            "/usr/bin/python3.12",
            "/bin/sh",
            "/bin/bash",
            "/usr/bin/node",
            "/usr/local/bin/node",
            "/usr/bin/perl",
            "/usr/bin/env",
            "/bin/busybox",
        ],
    )
    def test_shared_interpreters_are_recognised(self, path):
        """Attaching here would grant userns to every program that runs it."""
        from kiro_crew.service import apparmor as aa

        assert aa._SHARED_INTERPRETER_RE.match(path), path

    @pytest.mark.parametrize(
        "path",
        [
            "/home/user/AppImages/kirocrew.AppImage",
            "/opt/KiroCrew/kirocrew",
            "/usr/bin/kirocrew-desktop",
            "/usr/local/bin/pythonish-app",
        ],
    )
    def test_real_application_paths_are_not_mistaken_for_interpreters(self, path):
        from kiro_crew.service import apparmor as aa

        assert not aa._SHARED_INTERPRETER_RE.match(path), path

    @posix_only
    def test_rejects_a_shared_interpreter_end_to_end(self):
        """/bin/sh exists on every POSIX host, and resolves to a shell either way."""
        from kiro_crew.service import apparmor as aa

        resolved, problem = aa.validate_exec_path("/bin/sh")

        assert resolved is None
        assert "shared system interpreter" in problem
        assert "service install" in problem, "must name the supported alternative"

    @posix_only
    def test_resolves_before_validating_so_a_symlink_cannot_smuggle_a_grant(
        self, tmp_path
    ):
        """A link in a safe directory pointing at a shared interpreter.

        Validating the given path instead of the resolved one would write an
        attachment that the kernel matches against /bin/sh — a host-wide grant
        reached through a name that looks harmless.
        """
        from kiro_crew.service import apparmor as aa

        link = tmp_path / "kirocrew.AppImage"
        link.symlink_to("/bin/sh")

        resolved, problem = aa.validate_exec_path(str(link))

        assert resolved is None
        assert "shared system interpreter" in problem

    @pytest.mark.parametrize("bad", ["star*", "quest?", "brack[et]", "brace{x}", 'quo"te'])
    def test_rejects_glob_metacharacters(self, durable_dir, bad):
        """AppArmor reads an attachment as a glob even inside quotes."""
        from kiro_crew.service import apparmor as aa

        app = durable_dir / f"{bad}.AppImage"
        try:
            app.write_text("#!/bin/sh\n")
        except OSError:
            pytest.skip("filesystem rejects this name")

        resolved, problem = aa.validate_exec_path(str(app))

        assert resolved is None
        assert "glob syntax" in problem

    @posix_only
    def test_accepts_a_durable_path_with_a_space(self, durable_dir):
        """A space is fine — the rendered attachment is quoted."""
        from kiro_crew.service import apparmor as aa

        app = durable_dir / "Kiro Crew.AppImage"
        app.write_text("#!/bin/sh\n")

        resolved, problem = aa.validate_exec_path(str(app))

        assert problem == ""
        assert resolved == app.resolve()


@posix_only
class TestATakeoverOfTheAttachedPathIsRefused:
    """The prefix denylist is a message; filesystem modes are the guarantee.

    A world-writable directory outside the denylist — `/srv/shared` at 0777, a
    group-writable `/opt/apps`, a permissive network mount — would sail past a
    prefix check, and an attachment there lets any local user drop in their own
    executable and inherit the userns grant. Raised as blocking in review of
    #1653; these pin the fix.
    """

    def test_a_world_writable_directory_outside_the_denylist_is_refused(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.service import apparmor as aa

        shared = tmp_path / "shared"
        shared.mkdir()
        app = shared / "kirocrew.AppImage"
        app.write_text("#!/bin/sh\n")
        os.chmod(shared, 0o777)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- the lax mode IS the fixture, not the behaviour under test: this stages a world-writable directory outside the prefix denylist precisely so the assertion below can prove validate_exec_path() refuses to attach an AppArmor userns grant there. Removing it deletes the regression test for the blocking finding in #1653.  # noqa: E501
        # Empty the denylist so this can only be caught by the mode walk — the
        # whole point of the finding is that a prefix list does not cover it.
        monkeypatch.setattr(aa, "_UNSAFE_EXEC_PARENTS", ())

        resolved, problem = aa.validate_exec_path(str(app))

        assert resolved is None
        assert "world-writable" in problem
        assert str(shared) in problem, "must name the offending component"

    def test_a_group_writable_file_is_refused(self, tmp_path, monkeypatch):
        """Group members could replace the binary that receives the grant."""
        from kiro_crew.service import apparmor as aa

        app = tmp_path / "kirocrew.AppImage"
        app.write_text("#!/bin/sh\n")
        os.chmod(app, 0o775)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- group-writable IS the fixture: the test proves a file group members could replace is refused as an attachment target.  # noqa: E501
        monkeypatch.setattr(aa, "_UNSAFE_EXEC_PARENTS", ())
        monkeypatch.setattr(aa, "_substitutable_by_others", aa._substitutable_by_others)

        problem = aa._substitutable_by_others(app.resolve())

        assert problem is not None
        assert "group- or world-writable" in problem

    def test_a_writable_ancestor_is_enough_to_refuse(self, tmp_path, monkeypatch):
        """Renaming a writable parent re-points the same absolute path."""
        from kiro_crew.service import apparmor as aa

        outer = tmp_path / "outer"
        inner = outer / "inner"
        inner.mkdir(parents=True)
        app = inner / "kirocrew.AppImage"
        app.write_text("#!/bin/sh\n")
        os.chmod(app, 0o755)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- tight leaf; the writable ANCESTOR below is what this test exercises.  # noqa: E501
        os.chmod(inner, 0o755)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- tight leaf; the writable ANCESTOR below is what this test exercises.  # noqa: E501
        os.chmod(outer, 0o777)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- the writable ancestor IS the fixture: renaming a world-writable parent re-points the same absolute path at an attacker's file, so the test proves the mode walk climbs to / instead of checking the leaf alone.  # noqa: E501

        problem = aa._substitutable_by_others(app.resolve())

        assert problem is not None
        assert str(outer) in problem

    def test_a_root_owned_system_binary_is_refused(self):
        """Raised as blocking in review of #1653.

        The shared-interpreter regex is a BLOCKLIST and blocklists leak: it names
        python, perl, ruby, node and the shells, but not java, mono, dotnet, php,
        lua, wine, R or qemu-*. `--path /usr/bin/java` would have granted
        unprivileged userns to every Java process on the host. Requiring the target
        to be owned by the caller closes the whole class rather than adding names
        to the list.
        """
        from kiro_crew.service import apparmor as aa

        target = Path("/usr/bin/env")  # root-owned on every POSIX host
        if not target.exists() or target.stat().st_uid == os.getuid():
            pytest.skip("need a root-owned binary not owned by the test user")

        problem = aa._substitutable_by_others(target.resolve())

        assert problem is not None
        assert "owned by root" in problem
        assert "shared with every user" in problem

    @pytest.mark.parametrize(
        "shared",
        ["/usr/bin/java", "/usr/bin/mono", "/usr/bin/php", "/usr/lib/jvm/bin/java"],
    )
    def test_shared_runtimes_absent_from_the_blocklist_are_still_refused(
        self, shared, monkeypatch, tmp_path
    ):
        """The ownership rule covers what the interpreter regex never listed."""
        from kiro_crew.service import apparmor as aa

        stand_in = tmp_path / Path(shared).name
        stand_in.write_text("#!/bin/sh\n")
        assert not aa._SHARED_INTERPRETER_RE.match(shared), (
            f"{shared} is not on the blocklist, which is the point"
        )

        # Same shape as the real thing: root-owned, so not ours.
        class RootStat:
            st_uid = 0
            st_mode = stand_in.stat().st_mode

        monkeypatch.setattr(Path, "stat", lambda self, **_k: RootStat())

        assert aa._substitutable_by_others(stand_in) is not None

    def test_a_file_you_own_under_a_tight_chain_is_accepted(self, tmp_path):
        """Positive control: an AppImage you downloaded is owned by you."""
        from kiro_crew.service import apparmor as aa

        app = tmp_path / "kirocrew.AppImage"
        app.write_text("#!/bin/sh\n")
        os.chmod(app, 0o755)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- an executable must be executable; the point of this test is that a user-owned 0755 file under a tight chain is ACCEPTED.  # noqa: E501
        # The ancestor chain of a pytest tmp_path is 0700 on macOS but includes
        # /tmp on Linux, so only assert the ownership half here; the mode walk has
        # its own tests above.
        problem = aa._substitutable_by_others(app)

        assert problem is None or "world-writable" in problem, problem
        assert problem is None or "owned by" not in problem

    def test_tmp_is_refused_outright(self):
        """/tmp is caught twice over: root-owned AND world-writable.

        Ownership is checked first, so that is the reason reported. Either is
        disqualifying; what matters is that the most obvious wrong answer a user
        could give is refused.
        """
        from kiro_crew.service import apparmor as aa

        problem = aa._substitutable_by_others(Path("/tmp"))

        assert problem is not None
        assert "owned by root" in problem or "world-writable" in problem

    def test_a_file_owned_by_another_user_is_refused(self, tmp_path, monkeypatch):
        """The owner of the file chooses which binary gets the grant."""
        from kiro_crew.service import apparmor as aa

        app = tmp_path / "kirocrew.AppImage"
        app.write_text("#!/bin/sh\n")
        real = app.stat()

        class ForeignStat:
            st_uid = 4242
            st_mode = real.st_mode

        monkeypatch.setattr(Path, "stat", lambda self, **_k: ForeignStat())

        problem = aa._substitutable_by_others(app)

        assert problem is not None
        assert "uid 4242" in problem
        assert "not by you" in problem


class TestLauncherProfileRendering:
    """The rendered profile must grant one permission and attach to one path."""

    def _render(self, path="/home/u/Apps/kirocrew.AppImage", abi="4.0"):
        from kiro_crew.service import apparmor as aa

        return aa.render_launcher_profile(abi, Path(path))

    def test_grants_only_userns(self):
        """The rule body must contain `userns,` and nothing else.

        Asserted against the extracted body rather than the file text: the header
        comment legitimately discusses mount namespaces and credential paths, so
        a substring search over the whole profile would be testing the prose.
        """
        text = self._render()

        body = text.split("{", 1)[1].rsplit("}", 1)[0]
        rules = [
            line.strip()
            for line in body.splitlines()
            if line.strip() and not line.strip().startswith(("#", "include "))
        ]

        assert rules == ["userns,"], rules

    @posix_only
    def test_attaches_to_the_given_path_in_quotes(self):
        from kiro_crew.service import apparmor as aa

        text = self._render("/home/u/My Apps/kirocrew.AppImage")

        assert (
            f'profile {aa.LAUNCHER_PROFILE_NAME} "/home/u/My Apps/kirocrew.AppImage" '
            "flags=(unconfined)" in text
        )

    def test_omits_the_abi_line_when_the_host_ships_none(self):
        """Declaring an abi file that is absent makes the profile fail to load."""
        text = self._render(abi=None)

        assert "abi <abi/" not in text
        assert "include <tunables/global>" in text

    def test_keeps_a_local_override_include(self):
        from kiro_crew.service import apparmor as aa

        assert f"include if exists <local/{aa.LAUNCHER_PROFILE_NAME}>" in self._render()

    def test_explains_that_moving_the_file_breaks_it(self):
        """The one failure mode the kernel gives no error for."""
        text = self._render()

        assert "Moving or renaming" in text
        assert "kirocrew sandbox status" in text

    @posix_only
    def test_round_trips_through_the_attachment_parser(self, monkeypatch, tmp_path):
        from kiro_crew.service import apparmor as aa

        profile = tmp_path / aa.LAUNCHER_PROFILE_NAME
        profile.write_text(self._render("/home/u/Apps/kirocrew.AppImage"))
        monkeypatch.setattr(aa, "LAUNCHER_PROFILE_PATH", profile)

        assert aa.installed_attachment() == "/home/u/Apps/kirocrew.AppImage"

    def test_attachment_parser_returns_none_when_not_installed(self, monkeypatch, tmp_path):
        from kiro_crew.service import apparmor as aa

        monkeypatch.setattr(aa, "LAUNCHER_PROFILE_PATH", tmp_path / "absent")

        assert aa.installed_attachment() is None


@posix_only
class TestLauncherStatusTellsTheTruth:
    """A stale attachment must not be reported as a working setup."""

    @staticmethod
    def _restricted(monkeypatch, aa):
        monkeypatch.setattr(aa, "apparmor_is_active", lambda: True)
        monkeypatch.setattr(aa, "userns_restricted", lambda: True)

    def test_unaffected_host_is_reported_as_fine(self, monkeypatch):
        from kiro_crew.service import apparmor as aa

        monkeypatch.setattr(aa, "apparmor_is_active", lambda: True)
        monkeypatch.setattr(aa, "userns_restricted", lambda: False)

        ok, detail = aa.launcher_status("/home/u/Apps/kirocrew.AppImage")

        assert ok is True
        assert "does not restrict" in detail

    def test_missing_profile_on_an_appimage_launch_names_the_command(self, monkeypatch):
        from kiro_crew.service import apparmor as aa

        self._restricted(monkeypatch, aa)
        monkeypatch.setattr(aa, "installed_attachment", lambda: None)

        ok, detail = aa.launcher_status("/home/u/Apps/kirocrew.AppImage")

        assert ok is False
        assert "kirocrew sandbox install-profile" in detail

    def test_missing_profile_without_an_appimage_points_at_the_service(self, monkeypatch):
        """A foreground gateway has no safe path to attach to."""
        from kiro_crew.service import apparmor as aa

        self._restricted(monkeypatch, aa)
        monkeypatch.setattr(aa, "installed_attachment", lambda: None)
        monkeypatch.setattr(aa, "default_exec_path", lambda: None)

        ok, detail = aa.launcher_status(None)

        assert ok is False
        assert "kirocrew service install" in detail

    def test_a_moved_appimage_is_reported_as_not_covered(self, monkeypatch, durable_dir):
        """The kernel reports nothing here — the profile simply never matches."""
        from kiro_crew.service import apparmor as aa

        app = durable_dir / "kirocrew.AppImage"
        app.write_text("#!/bin/sh\n")
        self._restricted(monkeypatch, aa)
        monkeypatch.setattr(aa, "installed_attachment", lambda: "/old/place/kirocrew.AppImage")

        ok, detail = aa.launcher_status(str(app))

        assert ok is False
        assert "/old/place/kirocrew.AppImage" in detail
        assert "does not apply" in detail
        assert "re-point" in detail

    def test_a_matching_attachment_is_reported_as_covered(self, monkeypatch, durable_dir):
        from kiro_crew.service import apparmor as aa

        app = durable_dir / "kirocrew.AppImage"
        app.write_text("#!/bin/sh\n")
        self._restricted(monkeypatch, aa)
        monkeypatch.setattr(aa, "installed_attachment", lambda: str(app.resolve()))

        ok, detail = aa.launcher_status(str(app))

        assert ok is True
        assert str(app.resolve()) in detail


@posix_only
class TestLauncherInstallIsFailSoftAndHonest:
    """Same contract as the service profile: never raise, never overclaim."""

    @staticmethod
    def _app(durable_dir):
        app = durable_dir / "kirocrew.AppImage"
        app.write_text("#!/bin/sh\n")
        return app

    @staticmethod
    def _writers():
        writes: list[tuple[str, str]] = []
        runs: list[tuple[str, ...]] = []
        return writes, runs, (lambda t, d: writes.append((t, str(d)))), (
            lambda *a: runs.append(a)
        )

    def _ready(self, monkeypatch, aa):
        monkeypatch.setattr(aa, "should_install", lambda: (True, "restricted"))
        monkeypatch.setattr(aa, "parser_path", lambda: "/usr/sbin/apparmor_parser")
        monkeypatch.setattr(aa, "detect_abi", lambda: "4.0")
        monkeypatch.setattr(aa, "validate", lambda _p, _t: (True, ""))
        monkeypatch.setattr(aa, "conflicting_attachment", lambda _p: None)
        monkeypatch.setattr(aa, "verify_enforcement", lambda *_a: (True, None))

    def test_skips_cleanly_on_a_host_that_does_not_need_it(self, monkeypatch, durable_dir):
        from kiro_crew.service import apparmor as aa

        writes, runs, write, run = self._writers()
        monkeypatch.setattr(aa, "should_install", lambda: (False, "no restriction here"))

        outcome = aa.install_launcher(
            write, run, lambda *_a: (0, ""), 1000, 1000, str(self._app(durable_dir))
        )

        assert outcome.changed is False
        assert outcome.ok is True, "a skip is not a failure"
        assert writes == [] and runs == []

    def test_explains_itself_when_there_is_nothing_to_attach_to(self, monkeypatch):
        from kiro_crew.service import apparmor as aa

        writes, runs, write, run = self._writers()
        monkeypatch.setattr(aa, "should_install", lambda: (True, "restricted"))
        monkeypatch.setattr(aa, "default_exec_path", lambda: None)

        outcome = aa.install_launcher(write, run, lambda *_a: (0, ""), 1000, 1000, None)

        assert outcome.ok is False
        assert "$APPIMAGE" in outcome.message
        assert "service install" in outcome.message
        assert writes == []

    def test_refuses_an_unsafe_path_without_touching_the_host(self, monkeypatch):
        from kiro_crew.service import apparmor as aa

        writes, runs, write, run = self._writers()
        monkeypatch.setattr(aa, "should_install", lambda: (True, "restricted"))

        outcome = aa.install_launcher(
            write, run, lambda *_a: (0, ""), 1000, 1000, "/bin/sh"
        )

        assert outcome.ok is False
        assert outcome.changed is False
        assert writes == [] and runs == []

    def test_refuses_a_profile_that_does_not_compile(self, monkeypatch, durable_dir):
        from kiro_crew.service import apparmor as aa

        writes, runs, write, run = self._writers()
        self._ready(monkeypatch, aa)
        monkeypatch.setattr(aa, "validate", lambda _p, _t: (False, "syntax error"))

        outcome = aa.install_launcher(
            write, run, lambda *_a: (0, ""), 1000, 1000, str(self._app(durable_dir))
        )

        assert outcome.ok is False
        assert "did NOT compile" in outcome.message
        assert writes == [] and runs == []

    def test_a_sudo_failure_warns_and_never_raises(self, monkeypatch, durable_dir):
        from kiro_crew.service import apparmor as aa

        self._ready(monkeypatch, aa)

        def boom(*_a, **_k):
            raise RuntimeError("sudo: a password is required")

        outcome = aa.install_launcher(
            boom, lambda *_a: None, lambda *_a: (0, ""), 1000, 1000,
            str(self._app(durable_dir)),
        )

        assert outcome.ok is False
        assert "fail closed" in outcome.message

    def test_does_not_claim_success_when_enforcement_is_unconfirmed(
        self, monkeypatch, durable_dir
    ):
        from kiro_crew.service import apparmor as aa

        writes, runs, write, run = self._writers()
        self._ready(monkeypatch, aa)
        monkeypatch.setattr(aa, "verify_enforcement", lambda *_a: (False, "probe still fails"))

        outcome = aa.install_launcher(
            write, run, lambda *_a: (0, ""), 1000, 1000, str(self._app(durable_dir))
        )

        assert outcome.changed is True, "the file WAS written"
        assert outcome.ok is False
        assert "Not claiming" in outcome.message

    def test_verifies_enforcement_against_the_launcher_profile_by_name(
        self, monkeypatch, durable_dir
    ):
        """Verifying the service profile instead would prove the wrong thing."""
        from kiro_crew.service import apparmor as aa

        writes, runs, write, run = self._writers()
        self._ready(monkeypatch, aa)
        seen: list[str] = []
        monkeypatch.setattr(
            aa,
            "verify_enforcement",
            lambda _c, _u, _g, name: (seen.append(name), (True, None))[1],
        )

        aa.install_launcher(
            write, run, lambda *_a: (0, ""), 1000, 1000, str(self._app(durable_dir))
        )

        assert seen == [aa.LAUNCHER_PROFILE_NAME]

    def test_success_writes_the_profile_loads_it_and_says_to_restart(
        self, monkeypatch, durable_dir
    ):
        from kiro_crew.service import apparmor as aa

        writes, runs, write, run = self._writers()
        self._ready(monkeypatch, aa)
        app = self._app(durable_dir)

        outcome = aa.install_launcher(
            write, run, lambda *_a: (0, ""), 1000, 1000, str(app)
        )

        assert outcome.ok is True and outcome.changed is True
        assert len(writes) == 1
        assert writes[0][1] == str(aa.LAUNCHER_PROFILE_PATH)
        assert f'"{app.resolve()}"' in writes[0][0]
        assert runs == [("/usr/sbin/apparmor_parser", "-r", "-W", str(aa.LAUNCHER_PROFILE_PATH))]
        assert "Restart the app" in outcome.message

    def test_warns_about_a_conflicting_hand_written_profile(self, monkeypatch, durable_dir):
        """The workaround people find first attaches to the same AppImage."""
        from kiro_crew.service import apparmor as aa

        writes, runs, write, run = self._writers()
        self._ready(monkeypatch, aa)
        monkeypatch.setattr(
            aa, "conflicting_attachment", lambda _p: "/etc/apparmor.d/kirocrew"
        )

        outcome = aa.install_launcher(
            write, run, lambda *_a: (0, ""), 1000, 1000, str(self._app(durable_dir))
        )

        assert outcome.ok is True, "a conflict is a warning, not a failure"
        assert "/etc/apparmor.d/kirocrew" in outcome.message
        assert "ambiguous" in outcome.message

    def test_uninstall_is_a_silent_noop_when_nothing_is_installed(
        self, monkeypatch, durable_dir
    ):
        from kiro_crew.service import apparmor as aa

        monkeypatch.setattr(aa, "LAUNCHER_PROFILE_PATH", durable_dir / "absent")

        outcome = aa.uninstall_launcher(lambda *_a: None)

        assert outcome.changed is False
        assert outcome.message == ""

    def test_uninstall_unloads_then_removes(self, monkeypatch, durable_dir):
        from kiro_crew.service import apparmor as aa

        profile = durable_dir / aa.LAUNCHER_PROFILE_NAME
        profile.write_text("profile\n")
        monkeypatch.setattr(aa, "LAUNCHER_PROFILE_PATH", profile)
        monkeypatch.setattr(aa, "parser_path", lambda: "/usr/sbin/apparmor_parser")
        runs: list[tuple[str, ...]] = []

        outcome = aa.uninstall_launcher(lambda *a: runs.append(a))

        assert outcome.changed is True
        assert runs[0] == ("/usr/sbin/apparmor_parser", "-R", str(profile))
        assert runs[1] == ("rm", "-f", str(profile))

    def test_default_exec_path_reads_appimage(self, monkeypatch):
        from kiro_crew.service import apparmor as aa

        monkeypatch.setenv("APPIMAGE", "/home/u/Apps/kirocrew.AppImage")
        assert aa.default_exec_path() == "/home/u/Apps/kirocrew.AppImage"

        monkeypatch.setenv("APPIMAGE", "   ")
        assert aa.default_exec_path() is None

        monkeypatch.delenv("APPIMAGE", raising=False)
        assert aa.default_exec_path() is None


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

        with patch.object(controller, "current_platform", return_value=Platform.SYSTEMD), \
             patch.object(
                 linux,
                 "install_launcher_profile",
                 return_value=apparmor.ProfileOutcome(False, "nope", ok=False),
             ):
            rc = controller.install_launcher_profile("/x")

        assert rc == 1
        assert "⚠️" in capsys.readouterr().out

    def test_status_exit_code_is_the_answer(self):
        from kiro_crew.service import apparmor, controller

        with patch.object(controller, "current_platform", return_value=Platform.SYSTEMD), \
             patch.object(apparmor, "launcher_status", return_value=(False, "not covered")):
            assert controller.sandbox_profile_status(None) == 1

        with patch.object(controller, "current_platform", return_value=Platform.SYSTEMD), \
             patch.object(apparmor, "launcher_status", return_value=(True, "covered")):
            assert controller.sandbox_profile_status(None) == 0
