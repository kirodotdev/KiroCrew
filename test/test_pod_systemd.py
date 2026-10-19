"""The systemd backend: the unit file on disk, the platform guard and the per-user bus.

A unit file never outlives a failed load, its path follows the pinned home, a start
reinstalls a stale unit or one whose ``ExecStart`` binary is gone, and the verbs refuse with
the remedy that applies off Linux, without ``systemctl`` or without a reachable per-user
manager. The shared fixtures come from ``pod_helpers.py``; ``TestPlatformGuard`` adds its own.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import pytest
from pod_helpers import (  # noqa: F401 -- the fixtures register by import
    _cp,
    _fake_node_toolchain,
    _isolate_pod_host_state,
    _systemd_backend,
    cfg_fixture,
    requires_posix_pod_lifecycle,
)

from kiro_crew import platform_compat
from kiro_crew.pod import cli as pod_cli
from kiro_crew.pod import runtime as rt
from kiro_crew.pod import unit as unit_mod
from kiro_crew.pod.config import PodConfig

# The invoking user's REAL home, captured at import time — before the autouse
# ``_isolate_pod_host_state`` fixture pins HOME to a tmp dir. Used only to assert
# that the fixture moved HOME away from it (see TestHostStateIsFenced).
_REAL_HOME = Path.home()


@requires_posix_pod_lifecycle
class TestTheUnitFileNeverOutlivesAFailedLoad:
    """The invariant behind three separate defects: a unit file present on disk has
    been loaded by systemd. Every writer must uphold it — fixing only the lifecycle
    path left `pod install` able to strand a hookless file in front of a cached
    destructive definition, which is what made the on-disk freshness check lie."""

    def _plane(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        unit_file = tmp_path / "pod@.service"
        monkeypatch.setattr(rt.unit_mod, "unit_path", lambda c: unit_file)
        monkeypatch.setattr(rt.unit_mod, "_kirocrew_argv", lambda: (sys.executable,))
        monkeypatch.setattr(rt, "require_backend", lambda: None)
        return unit_file

    def test_install_removes_the_unit_when_the_reload_fails(
        self, cfg: PodConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        unit_file = self._plane(tmp_path, monkeypatch)
        monkeypatch.setattr(
            rt, "systemctl", lambda *a, **k: _cp(returncode=1, stderr="reload failed")
        )
        msg, cp = rt.install_backend(cfg)
        assert cp is not None and cp.returncode != 0
        assert not unit_file.exists(), "an unloaded unit must not be left on disk"
        assert rt.unit_mod.unit_is_current(cfg) is False
        assert "removed" in msg

    def test_install_keeps_the_unit_when_the_reload_succeeds(
        self, cfg: PodConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        unit_file = self._plane(tmp_path, monkeypatch)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp())
        msg, cp = rt.install_backend(cfg)
        assert cp is not None and cp.returncode == 0
        assert unit_file.exists()
        assert rt.unit_mod.unit_is_current(cfg) is True
        assert "installed pod template unit" in msg

    def test_a_unit_missing_a_required_directive_is_stale(
        self, cfg: PodConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The upgrade path for a directive that was ADDED rather than removed.

        `RestartPreventExitStatus` is what makes a fail-closed boot refusal
        terminal instead of a 5s restart loop. Every machine with an existing pod
        unit predates it, and a removal-only staleness test reports such a unit
        "current" -- so the refresh is skipped and the directive never lands,
        leaving the refusal looping. Caught in live pod acceptance: the rendered
        template carried the directive while the INSTALLED unit did not, and it is
        the installed one systemd executes."""
        unit_file = self._plane(tmp_path, monkeypatch)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp())
        rt.install_backend(cfg)
        assert rt.unit_mod.unit_is_current(cfg) is True

        stripped = "\n".join(
            line
            for line in unit_file.read_text().splitlines()
            if not line.startswith("RestartPreventExitStatus=")
        )
        unit_file.write_text(stripped + "\n")

        assert rt.unit_mod.unit_is_current(cfg) is False, (
            "a unit predating the restart-prevention directive must be refreshed, "
            "not accepted as current"
        )

    def test_the_rendered_template_satisfies_its_own_required_directives(
        self, cfg: PodConfig
    ) -> None:
        """Guards the pair from drifting apart: a directive added to the required
        set but not to the template would make every unit permanently stale."""
        rendered = unit_mod.render_unit(cfg).splitlines()
        for required in unit_mod._REQUIRED_DIRECTIVES:
            assert any(line.startswith(required) for line in rendered), required


class TestHostStateIsFenced:
    """A pod test must not be able to write the machine's own systemd unit.

    Found on a real host: a suite run rewrote `~/.config/systemd/user/
    kirocrew-pod@.service` with a test's tmpdir as
    `Environment=KIROCREW_POD_ROOT=`, after which every `pod up` died with "no
    pinned checkout" until someone re-ran `pod install`. `unit_path` reads
    `Path.home()`, which no test patched, and `_up` -> `install_backend` ->
    `install_unit` reaches it. Asserting the PATH rather than the write keeps this
    guard from having to clobber the real unit to prove itself.
    """

    def test_the_home_is_pinned_away_from_the_real_one(self) -> None:
        """The fixture must actually redirect. Asserted separately from the paths
        below because on Windows a pytest tmp dir lives UNDER `Path.home()`
        (`C:/Users/<u>/AppData/Local/Temp/...`), so "not under the real home" is
        false there by construction and cannot carry the guard."""
        assert Path.home() != _REAL_HOME

    def test_the_unit_path_follows_the_pinned_home(self, cfg: PodConfig) -> None:
        assert Path.home() in unit_mod.unit_path(cfg).parents

    def test_pod_host_dirs_follow_the_pinned_home(self, cfg: PodConfig) -> None:
        # pods_dir carries the lifecycle lock file; pod_root carries pod HOMEs.
        for path in (cfg.pods_dir, cfg.pod_root):
            assert Path.home() in path.parents, path


class TestUnitExecSelfHeal:
    """The unit bakes an absolute kirocrew path; a pruned worktree leaves it
    dangling and every start fails EXEC. unit_exec_ok detects that."""

    def _cfg_with_unit(self, tmp_path, monkeypatch, exec_line):
        from kiro_crew.pod import unit as unit_mod
        from kiro_crew.pod.config import PodConfig

        monkeypatch.setattr(unit_mod, "unit_path", lambda cfg: tmp_path / "pod@.service")
        # Carries every directive this build REQUIRES (see `_REQUIRED_DIRECTIVES`),
        # so a test built on this helper isolates the axis it is actually about
        # rather than tripping the required-directive staleness check. Rendered
        # from the real tuple, so adding a required directive cannot silently
        # invalidate every baseline here.
        required = "".join(f"{name}0\n" for name in unit_mod._REQUIRED_DIRECTIVES)
        (tmp_path / "pod@.service").write_text(
            f"[Service]\n{exec_line}\nRestart=on-failure\n{required}"
        )
        return PodConfig.load()

    def test_dangling_binary_detected(self, tmp_path, monkeypatch):
        from kiro_crew.pod import unit as unit_mod

        cfg = self._cfg_with_unit(
            tmp_path,
            monkeypatch,
            f"ExecStart={(tmp_path / 'gone/.venv/bin/kirocrew').as_posix()} pod _run %i",
        )
        assert unit_mod.unit_exec_ok(cfg) is False

    def test_valid_binary_passes(self, tmp_path, monkeypatch):
        from kiro_crew.pod import unit as unit_mod

        exe = tmp_path / "kirocrew"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        cfg = self._cfg_with_unit(tmp_path, monkeypatch, f"ExecStart={exe.as_posix()} pod _run %i")
        assert unit_mod.unit_exec_ok(cfg) is True

    def test_missing_unit_file_detected(self, tmp_path, monkeypatch):
        from kiro_crew.pod import unit as unit_mod
        from kiro_crew.pod.config import PodConfig

        monkeypatch.setattr(unit_mod, "unit_path", lambda cfg: tmp_path / "absent@.service")
        assert unit_mod.unit_exec_ok(PodConfig.load()) is False

    def test_a_unit_carrying_the_removed_teardown_hook_is_not_current(self, tmp_path, monkeypatch):
        """Units are written once by `pod install`, so on UPGRADE a machine keeps
        whatever it installed. Without this, an older unit's ExecStopPost would go
        on racing the pod's own subprocesses (and wiping the HOME on the stop half
        of a Restart=) until someone reinstalled by hand."""
        from kiro_crew.pod import unit as unit_mod

        exe = tmp_path / "kirocrew"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        cfg = self._cfg_with_unit(tmp_path, monkeypatch, f"ExecStart={exe.as_posix()} pod _run %i")
        unit_path = tmp_path / "pod@.service"
        assert unit_mod.unit_is_current(cfg) is True  # what this build renders
        unit_path.write_text(unit_path.read_text() + f"ExecStopPost={exe} pod _cleanup %i\n")
        assert unit_mod.unit_is_current(cfg) is False

    @requires_posix_pod_lifecycle
    def test_what_this_build_renders_is_current(self, cfg, monkeypatch, tmp_path):
        """Guard against the reverse failure: a check that flagged the CURRENT
        template would re-render and daemon-reload on every single `pod up`."""
        from kiro_crew.pod import unit as unit_mod

        monkeypatch.setattr(unit_mod, "unit_path", lambda c: tmp_path / "pod@.service")
        # An executable that exists on every platform the suite runs on — a POSIX
        # path here made unit_exec_ok report "stale" on Windows.
        monkeypatch.setattr(unit_mod, "_kirocrew_argv", lambda: (sys.executable,))
        unit_mod.install_unit(cfg)
        assert unit_mod.unit_is_current(cfg) is True

    @requires_posix_pod_lifecycle
    def test_spaced_executable_is_quoted_and_current(self, cfg, monkeypatch, tmp_path):
        from kiro_crew.pod import unit as unit_mod

        exe = tmp_path / "venv with spaces" / "kirocrew"
        exe.parent.mkdir()
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        monkeypatch.setattr(unit_mod, "unit_path", lambda c: tmp_path / "pod@.service")
        monkeypatch.setattr(unit_mod, "_kirocrew_argv", lambda: (str(exe),))
        rendered = unit_mod.install_unit(cfg).read_text()
        assert 'ExecStart="' in rendered
        assert unit_mod.unit_is_current(cfg) is True

    def test_malformed_execstart_is_stale(self, tmp_path, monkeypatch):
        from kiro_crew.pod import unit as unit_mod

        cfg = self._cfg_with_unit(tmp_path, monkeypatch, 'ExecStart="unterminated')
        assert unit_mod.unit_exec_ok(cfg) is False

    def test_start_pod_reinstalls_a_stale_unit_before_booting_it(self, cfg, monkeypatch):
        steps: list[str] = []
        monkeypatch.setattr(rt.unit_mod, "unit_is_current", lambda c: False)
        monkeypatch.setattr(rt.unit_mod, "install_unit", lambda c: steps.append("reinstall"))
        monkeypatch.setattr(
            rt.unit_mod, "install_dropin", lambda c, n, co: steps.append("pin-checkout")
        )
        monkeypatch.setattr(rt, "read_env_file", lambda c, n: {"CHECKOUT": "/checkouts/demo"})

        def _systemctl(*args, **kwargs):
            steps.append(args[0])
            return _cp()

        monkeypatch.setattr(rt, "systemctl", _systemctl)
        assert rt.start_pod(cfg, "demo").returncode == 0
        assert steps == [
            "reinstall",
            "daemon-reload",
            "pin-checkout",
            "daemon-reload",
            "start",
        ]

    def test_module_invocation_form_passes(self, tmp_path, monkeypatch):
        from kiro_crew.pod import unit as unit_mod

        cfg = self._cfg_with_unit(
            tmp_path,
            monkeypatch,
            "ExecStart=python3 -m kiro_crew pod _run %i",
        )
        assert unit_mod.unit_exec_ok(cfg) is True


class TestPlatformGuard:
    """Pods are Linux `systemd --user` only (pod/README.md → Platform).

    Off-Linux the verbs must report the refusal, NOT dump a bare
    ``FileNotFoundError`` traceback from the first ``subprocess.run(["systemctl",
    ...])``. The gate lives in ``require_systemd()``, which every systemd/
    journalctl call funnels through.
    """

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse systemctl seams moved verbatim; no test in this class calls monkeypatch.undo()
    def _linux_systemctl_seams(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Keep Linux-shaped probe tests deterministic on Windows and macOS."""
        monkeypatch.setattr(rt, "IS_LINUX", True)
        monkeypatch.setattr(rt, "IS_MACOS", False)
        monkeypatch.setattr(rt, "IS_WINDOWS", False)
        monkeypatch.setattr(rt.sys, "platform", "linux")
        monkeypatch.setattr(rt.shutil, "which", lambda _n: "/usr/bin/systemctl")
        monkeypatch.setattr(
            platform_compat,
            "trusted_system_bin",
            lambda name: "/usr/bin/systemctl" if name == "systemctl" else None,
        )

    def test_require_systemd_refuses_off_linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rt, "IS_LINUX", False)
        monkeypatch.setattr(rt.sys, "platform", "darwin")
        with pytest.raises(rt.PodError) as exc:
            rt.require_systemd()
        # Names the platform AND the supported alternative, so the message is actionable.
        assert "darwin" in str(exc.value)
        assert "dev-backend.sh" in str(exc.value)

    def test_require_systemd_refuses_when_systemctl_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rt, "IS_LINUX", True)
        monkeypatch.setattr(rt.shutil, "which", lambda _n: None)
        with pytest.raises(rt.PodError, match="systemctl"):
            rt.require_systemd()

    def test_require_systemd_passes_on_linux_with_systemctl(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(rt, "IS_LINUX", True)
        monkeypatch.setattr(rt.shutil, "which", lambda _n: "/usr/bin/systemctl")
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
        (tmp_path / "bus").touch()
        monkeypatch.setattr(
            rt,
            "probe_user_bus",
            lambda: pytest.fail("require_systemd must not spawn the user-bus probe"),
        )

        rt.require_systemd()

    def test_user_bus_probe_classifies_reachable_degraded_manager(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        (tmp_path / "bus").touch()
        monkeypatch.setattr(
            rt,
            "_run",
            lambda _cmd, **_kwargs: _cp(stdout="degraded\n", returncode=1),
        )

        result = rt.probe_user_bus()

        assert result.status == rt.USER_BUS_REACHABLE
        assert result.detail == "degraded"

    def test_user_bus_probe_classifies_outer_sandbox_denial(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        (tmp_path / "bus").touch()
        monkeypatch.setattr(
            rt,
            "_run",
            lambda _cmd, **_kwargs: _cp(
                returncode=1,
                stderr="Failed to connect to bus: Permission denied\n",
            ),
        )

        result = rt.probe_user_bus()

        assert result.status == rt.USER_BUS_SANDBOXED_AWAY
        assert result.detail == "Failed to connect to bus: Permission denied"
        message = rt.user_bus_failure_message(result)
        assert "outer layer" in message
        assert "container" in message
        assert "launcher shim" in message
        assert "host shell" in message

    def test_stale_explicit_bus_address_is_operational_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stale = "unix:path=/run/user/4242/stale-bus"
        detail = "Failed to connect to bus: No such file or directory"
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", stale)
        monkeypatch.setattr(
            rt,
            "_run",
            lambda _cmd, **_kwargs: _cp(returncode=1, stderr=detail),
        )

        result = rt.probe_user_bus()

        assert result.status == rt.USER_BUS_ERROR
        assert result.detail == detail
        with pytest.raises(rt.PodError) as exc:
            rt.require_backend()
        assert not isinstance(exc.value, rt.PodBackendAbsent)
        assert detail in str(exc.value)

    def test_stale_explicit_bus_address_still_gets_the_manager_remedy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Operationally unknown, yet the remedy names what a stale address needs.

        The class must stay a plain :class:`rt.PodError` — a stale address is
        never proof that no backend exists, and that proof is what authorizes
        destructive worktree removal. Only the text changes: the old one told the
        caller to rerun the command that had just failed.
        """
        stale = "unix:path=/run/user/4242/stale-bus"
        detail = "Failed to get D-Bus connection: Connection refused"
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", stale)
        monkeypatch.setenv("USER", "tester")
        monkeypatch.setattr(rt.os, "getuid", lambda: 4242, raising=False)
        monkeypatch.setattr(
            rt,
            "_run",
            lambda _cmd, **_kwargs: _cp(returncode=1, stderr=detail),
        )

        result = rt.probe_user_bus()
        message = rt.user_bus_failure_message(result)

        assert result.status == rt.USER_BUS_ERROR
        assert "stale" in message
        assert "loginctl enable-linger tester" in message
        assert "sudo loginctl enable-linger 4242" in message
        assert "./dev-backend.sh" in message
        # The circular instruction is gone.
        assert "fix that error before using pod commands" not in message
        assert detail in message

    def test_a_present_socket_keeps_the_generic_probe_remedy(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The socket exists, so a stopped per-user manager is not the diagnosis."""
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
        (tmp_path / "bus").touch()
        detail = "Failed to connect to bus: Connection reset by peer"
        monkeypatch.setattr(
            rt,
            "_run",
            lambda _cmd, **_kwargs: _cp(returncode=1, stderr=detail),
        )

        message = rt.user_bus_failure_message(rt.probe_user_bus())

        assert "fix that error before using pod commands" in message
        assert "enable-linger" not in message

    def test_user_bus_probe_preserves_an_unclassified_failure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        (tmp_path / "bus").touch()
        monkeypatch.setattr(
            rt,
            "_run",
            lambda _cmd, **_kwargs: _cp(
                returncode=1,
                stderr="Failed to connect to bus: Connection reset by peer\n",
            ),
        )

        result = rt.probe_user_bus()

        assert result.status == rt.USER_BUS_ERROR
        assert result.detail == "Failed to connect to bus: Connection reset by peer"

    def test_user_bus_probe_classifies_exec_failure_as_operational_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(rt, "IS_MACOS", False)
        monkeypatch.setattr(rt, "IS_WINDOWS", False)
        monkeypatch.setattr(rt, "IS_LINUX", True)
        monkeypatch.setattr(rt.shutil, "which", lambda _n: "/usr/bin/systemctl")
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
        (tmp_path / "bus").touch()

        def broken_systemctl(_cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
            raise FileNotFoundError("systemctl shim interpreter is missing")

        monkeypatch.setattr(rt, "_run", broken_systemctl)

        result = rt.probe_user_bus()

        assert result.status == rt.USER_BUS_ERROR
        with pytest.raises(rt.PodError) as exc:
            rt.require_backend()
        assert not isinstance(exc.value, rt.PodBackendAbsent)
        assert "systemctl shim interpreter is missing" in str(exc.value)

    def test_user_bus_probe_does_not_treat_generic_enoent_as_no_session(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(rt, "IS_MACOS", False)
        monkeypatch.setattr(rt, "IS_WINDOWS", False)
        monkeypatch.setattr(rt, "IS_LINUX", True)
        monkeypatch.setattr(rt.shutil, "which", lambda _n: "/usr/bin/systemctl")
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
        (tmp_path / "bus").touch()
        detail = (
            "systemctl: error while loading shared libraries: "
            "libsystemd-shared.so: No such file or directory"
        )
        monkeypatch.setattr(
            rt,
            "_run",
            lambda _cmd, **_kwargs: _cp(returncode=127, stderr=detail),
        )

        result = rt.probe_user_bus()

        assert result.status == rt.USER_BUS_ERROR
        with pytest.raises(rt.PodError) as exc:
            rt.require_backend()
        assert not isinstance(exc.value, rt.PodBackendAbsent)
        assert detail in str(exc.value)

    def test_pod_up_reports_bus_denial_before_worktree_resolution(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr(rt, "IS_LINUX", True)
        monkeypatch.setattr(rt.shutil, "which", lambda _n: "/usr/bin/systemctl")
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/4242/bus")
        monkeypatch.setattr(
            rt,
            "_run",
            lambda _cmd, **_kwargs: _cp(
                returncode=1,
                stderr="Failed to connect to bus: Permission denied\n",
            ),
        )
        monkeypatch.setattr(
            pod_cli,
            "_resolve_or_die",
            lambda *_args, **_kwargs: pytest.fail("worktree resolution ran before bus probe"),
        )
        audits: list[tuple[str, str, str, str]] = []
        monkeypatch.setattr(
            pod_cli,
            "_audit",
            lambda operation, outcome, resources, *, error="": audits.append(
                (operation, outcome, resources, error)
            ),
        )

        with pytest.raises(SystemExit) as exc:
            pod_cli.dispatch(argparse.Namespace(pod_action="up", name="busdiag-live-smoke"))

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "outer layer" in err
        assert "host shell" in err
        assert "Failed to connect to bus: Permission denied" in err
        assert len(audits) == 1
        assert audits[0][:3] == (
            "pod.up",
            "failure",
            "name=busdiag-live-smoke",
        )
        assert "Permission denied" in audits[0][3]

    def test_systemctl_gated_before_spawn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The guard runs BEFORE the subprocess, so no spawn is attempted."""
        monkeypatch.setattr(rt, "IS_LINUX", False)
        spawned: list[list[str]] = []
        monkeypatch.setattr(rt, "_run", lambda cmd, **k: spawned.append(cmd))
        with pytest.raises(rt.PodError):
            rt.systemctl("list-units")
        assert spawned == []

    def test_unit_queries_do_not_run_user_bus_probe(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(rt, "IS_MACOS", False)
        monkeypatch.setattr(rt, "IS_WINDOWS", False)
        monkeypatch.setattr(rt, "IS_LINUX", True)
        monkeypatch.setattr(rt.shutil, "which", lambda _n: "/usr/bin/systemctl")
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
        (tmp_path / "bus").touch()
        monkeypatch.setattr(
            rt,
            "probe_user_bus",
            lambda: pytest.fail("unit queries must not run the user-bus probe"),
        )
        spawned: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs) -> subprocess.CompletedProcess:
            spawned.append(cmd)
            if "show" in cmd:
                return _cp(stdout="MainPID=4242\n")
            return _cp(returncode=0)

        monkeypatch.setattr(rt, "_run", fake_run)

        assert rt.systemctl("daemon-reload").returncode == 0
        assert rt.is_active(cfg, "alpha") is True
        assert rt.main_pid(cfg, "alpha") == 4242
        assert len(spawned) == 3
        assert all("is-system-running" not in cmd for cmd in spawned)

    def test_systemctl_spawns_only_the_trusted_absolute_binary(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(rt, "IS_MACOS", False)
        monkeypatch.setattr(rt, "IS_WINDOWS", False)
        monkeypatch.setattr(rt, "IS_LINUX", True)
        impostor_dir = tmp_path / "bin"
        impostor_dir.mkdir()
        impostor = impostor_dir / "systemctl"
        impostor.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        impostor.chmod(0o755)
        monkeypatch.setenv("PATH", str(impostor_dir))
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
        (tmp_path / "bus").touch()
        monkeypatch.setattr(
            platform_compat,
            "trusted_system_bin",
            lambda name: "/usr/bin/systemctl" if name == "systemctl" else None,
        )
        spawned: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs) -> subprocess.CompletedProcess:
            spawned.append(cmd)
            return _cp(stdout="running\n")

        monkeypatch.setattr(rt, "_run", fake_run)

        assert rt.probe_user_bus().status == rt.USER_BUS_REACHABLE
        assert rt.systemctl("is-active", rt.pod_unit(cfg, "alpha")).returncode == 0
        assert [cmd[0] for cmd in spawned] == ["/usr/bin/systemctl", "/usr/bin/systemctl"]
        assert all(cmd[0] != str(impostor) for cmd in spawned)

    def test_missing_trusted_systemctl_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(rt, "IS_MACOS", False)
        monkeypatch.setattr(rt, "IS_WINDOWS", False)
        monkeypatch.setattr(rt, "IS_LINUX", True)
        monkeypatch.setattr(rt.shutil, "which", lambda _n: str(tmp_path / "systemctl"))
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
        (tmp_path / "bus").touch()
        monkeypatch.setattr(platform_compat, "trusted_system_bin", lambda _name: None)
        monkeypatch.setattr(
            rt,
            "_run",
            lambda _cmd, **_kwargs: pytest.fail("an untrusted systemctl was spawned"),
        )

        result = rt.probe_user_bus()

        assert result.status == rt.USER_BUS_ERROR
        assert "trusted system" in result.detail
        with pytest.raises(rt.PodError) as exc:
            rt.require_backend()
        assert not isinstance(exc.value, rt.PodBackendAbsent)
        with pytest.raises(rt.PodError, match="trusted system"):
            rt.systemctl("list-units")

    def test_recent_journal_gated(self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch) -> None:
        """journalctl is a sibling of systemctl, not routed through it — gate it too."""
        monkeypatch.setattr(rt, "IS_LINUX", False)
        with pytest.raises(rt.PodError):
            rt.recent_journal(cfg, "alpha")

    def test_ls_reports_cleanly_off_linux(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """`pod ls` exits 1 with a one-liner — the dispatch layer converts PodError."""
        monkeypatch.setattr(rt, "IS_LINUX", False)
        args = argparse.Namespace(pod_action="ls", json=False)
        with pytest.raises(SystemExit) as exc:
            pod_cli.dispatch(args)
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "pod:" in err and "Linux" in err
        assert "Traceback" not in err

    def test_install_writes_no_unit_off_linux(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The gate precedes install_unit(), so no unit file is left behind."""
        monkeypatch.setattr(rt, "IS_LINUX", False)
        wrote: list[object] = []
        monkeypatch.setattr(unit_mod, "install_unit", lambda c: wrote.append(c))
        with pytest.raises(rt.PodError):
            pod_cli._install(cfg, argparse.Namespace())
        assert wrote == []


class TestSessionBus:
    """``systemctl --user`` needs the per-user systemd instance's bus pointers.

    A process descended from a systemd SYSTEM unit — how ``kirocrew service
    install`` runs the gateway — inherits no login-session environment, so
    neither ``XDG_RUNTIME_DIR`` nor ``DBUS_SESSION_BUS_ADDRESS`` is set and every
    pod verb died with "Failed to connect to bus: No medium found" even though
    the bus socket existed. ``_systemctl_env()`` backfills them; when the socket
    is genuinely absent ``require_systemd()`` explains the fix instead.
    """

    @staticmethod
    def _bus(monkeypatch: pytest.MonkeyPatch, runtime_dir: Path, *, exists: bool) -> Path:
        """Point at *runtime_dir* as the session runtime dir, with no inherited bus."""
        runtime_dir.mkdir(parents=True, exist_ok=True)
        sock = runtime_dir / "bus"
        if exists:
            sock.touch()
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
        monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
        return sock

    def test_backfills_both_when_socket_present(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        sock = self._bus(monkeypatch, tmp_path / "run", exists=True)
        env = rt._systemctl_env()
        assert env["XDG_RUNTIME_DIR"] == str(tmp_path / "run")
        assert env["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={sock}"

    def test_derives_runtime_dir_from_uid_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No ``XDG_RUNTIME_DIR`` → systemd's conventional ``/run/user/<uid>``."""
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
        monkeypatch.setattr(rt.os, "getuid", lambda: 4242, raising=False)
        assert rt._systemctl_env()["XDG_RUNTIME_DIR"] == "/run/user/4242"

    def test_leaves_bus_unset_when_socket_absent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """No socket → no invented address, so systemctl emits its own diagnostic
        instead of failing against a path we made up."""
        self._bus(monkeypatch, tmp_path / "run", exists=False)
        env = rt._systemctl_env()
        assert "DBUS_SESSION_BUS_ADDRESS" not in env
        assert env["XDG_RUNTIME_DIR"] == str(tmp_path / "run")

    def test_never_clobbers_an_explicit_bus(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A caller pointing at another bus is left alone — this only ever ADDS."""
        self._bus(monkeypatch, tmp_path / "run", exists=True)
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/elsewhere/bus")
        assert rt._systemctl_env()["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/elsewhere/bus"

    def test_never_clobbers_an_explicit_runtime_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._bus(monkeypatch, tmp_path / "run", exists=True)
        assert rt._systemctl_env()["XDG_RUNTIME_DIR"] == str(tmp_path / "run")

    def test_systemctl_diagnostics_use_the_c_locale(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._bus(monkeypatch, tmp_path / "run", exists=True)
        monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")

        assert rt._systemctl_env()["LC_ALL"] == "C"

    def test_has_session_bus_trusts_an_explicit_address(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An explicit address may not be a filesystem path at all — take it as given."""
        self._bus(monkeypatch, tmp_path / "run", exists=False)
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:abstract=/tmp/dbus-abc")
        assert rt.has_session_bus() is True

    def test_uncheckable_transports_are_never_called_stale(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Only ``unix:path=`` names something to stat, so nothing else is judged.

        ``session_bus_socket()`` falls back to the conventional path for an
        address it cannot resolve, and that path existing is what keeps the
        generic remedy. A transport with no filesystem identity must not be
        described as a stopped per-user manager.
        """
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        (tmp_path / "bus").touch()
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:abstract=/tmp/dbus-abc")
        monkeypatch.setattr(
            rt,
            "_run",
            lambda _cmd, **_kwargs: _cp(
                returncode=1, stderr="Failed to connect to bus: Connection refused"
            ),
        )

        assert rt.has_session_bus() is True
        assert rt.session_bus_socket() == str(tmp_path / "bus")
        assert "stale" not in rt.user_bus_failure_message(rt.probe_user_bus())

    def test_present_socket_named_by_the_address_is_the_path_reported(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A refusal names the path actually judged, not a conventional one."""
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir(parents=True, exist_ok=True)
        sock = elsewhere / "bus"
        sock.touch()
        self._bus(monkeypatch, tmp_path / "run", exists=False)
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", f"unix:path={sock}")
        assert rt.session_bus_socket() == str(sock)

    def test_percent_escaped_address_path_is_decoded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """D-Bus percent-escapes address values, so a raw compare would miss."""
        spaced = tmp_path / "run dir"
        spaced.mkdir(parents=True, exist_ok=True)
        sock = spaced / "bus"
        sock.touch()
        self._bus(monkeypatch, tmp_path / "run", exists=False)
        escaped = str(sock).replace(" ", "%20")
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", f"unix:path={escaped}")
        assert rt.session_bus_socket() == str(sock)

    def test_no_session_remedy_is_actionable_for_the_actor_who_can_act(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``loginctl`` needs the SYSTEM bus, so it is not always self-service.

        A caller that cannot reach a bus cannot run ``loginctl`` either, and
        ``sudo loginctl enable-linger <name>`` fails outright where root's name
        lookup does not resolve the account. The message therefore also names the
        privileged uid form and a preview path that needs no systemd at all.
        """
        sock = self._bus(monkeypatch, tmp_path / "run", exists=False)
        monkeypatch.setenv("USER", "tester")
        monkeypatch.setattr(rt.os, "getuid", lambda: 4242, raising=False)

        message = rt.user_bus_failure_message(
            rt.UserBusProbe(rt.USER_BUS_NO_SESSION, str(sock), "")
        )
        assert "loginctl enable-linger tester" in message
        assert "sudo loginctl enable-linger 4242" in message
        assert "./dev-backend.sh" in message

    def test_require_systemd_explains_a_missing_bus(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The raw systemctl message names neither cause nor fix — ours does."""
        monkeypatch.setattr(rt, "IS_LINUX", True)
        monkeypatch.setattr(rt.shutil, "which", lambda _n: "/usr/bin/systemctl")
        sock = self._bus(monkeypatch, tmp_path / "run", exists=False)
        monkeypatch.setenv("USER", "tester")
        # Pin the per-user manager as PRESENT so this stays the "instance is not
        # running" case. Left unpinned the assertion would depend on whether the
        # runner ships user@.service, and on a host without it the refusal is
        # deliberately a different one (see the test below). Pinned on the public
        # probe `require_systemd` consults, not on a private helper behind it.
        monkeypatch.setattr(
            rt, "user_manager_unit", lambda _uid=None: "/usr/lib/systemd/system/user@.service"
        )
        with pytest.raises(rt.PodError) as exc:
            rt.require_systemd()
        msg = str(exc.value)
        assert str(sock) in msg
        assert "loginctl enable-linger tester" in msg
        # Keyed on the socket's absence, not on matching systemctl's stderr.
        assert "No medium found" not in msg

    def test_require_systemd_refuses_without_a_per_user_manager(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """No ``user@.service`` means no pod is ever possible — say so, don't misdirect.

        A missing bus has two causes with different remedies. When systemd ships
        the per-user manager template the instance merely is not running, and
        ``enable-linger`` starts it. When the template is absent (RHEL 7, CentOS 7,
        Amazon Linux 2 — systemd 219 with the per-user manager left out) linger has
        nothing to instantiate, so recommending it sends the reader after a fix
        that provably cannot work.
        """
        monkeypatch.setattr(rt, "IS_LINUX", True)
        monkeypatch.setattr(rt.shutil, "which", lambda _n: "/usr/bin/systemctl")
        self._bus(monkeypatch, tmp_path / "run", exists=False)
        monkeypatch.setenv("USER", "tester")
        # Pin the WIDER predicate, not just the template: `user_manager_unit` falls
        # through to a real-filesystem probe for `user@<uid>.service`, so pinning
        # only the template would leave this test dependent on the runner again.
        monkeypatch.setattr(rt, "user_manager_unit", lambda _uid=None: None)
        with pytest.raises(rt.PodBackendAbsent) as exc:
            rt.require_systemd()
        msg = str(exc.value)
        # The message may NAME the remedy in order to rule it out — that is the
        # point, since a host can have Linger=yes and still be unable to run a
        # pod. What it must not do is RECOMMEND it as the fix.
        assert "Fix: loginctl enable-linger" not in msg
        assert "cannot help" in msg
        # Names the limit and the way forward, like the non-Linux gate does.
        assert "user@.service" in msg
        assert "dev-backend.sh" in msg

    def test_user_manager_template_reports_the_path_it_found(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Probed by unit path, so a stray dbus socket cannot fake support.

        The bus socket is the wrong predicate: a session ``dbus-daemon`` can
        create it on a host with no per-user manager, which is why this probe
        looks for the template instead.
        """
        present = tmp_path / "user@.service"
        present.touch()
        monkeypatch.setattr(
            rt, "_SYSTEMD_SYSTEM_UNIT_DIRS", (str(tmp_path / "absent"), str(tmp_path))
        )
        assert rt._user_manager_template() == f"{tmp_path}/user@.service"

        monkeypatch.setattr(rt, "_SYSTEMD_SYSTEM_UNIT_DIRS", (str(tmp_path / "absent"),))
        assert rt._user_manager_template() is None

    def test_unit_search_covers_systemd_load_path_in_precedence_order(self) -> None:
        """The search path must be systemd.unit(5) Table 1, complete and in order.

        A short search path is a false "absent": the probe reports no per-user
        manager on a host that HAS one, and :func:`require_systemd` then names a
        platform limit instead of telling the reader to start the manager. Missing
        any one entry can give that false "absent" on a host whose unit lives there,
        so this pins the whole documented table rather than a chosen subset.

        Asserted on the DIRECTORY list, which is what both unit shapes derive
        from. A per-shape assertion would let one shape regress while the other
        passed, and the two shapes would drift apart unseen.

        Order is asserted because the found path is the one a message names, and
        naming a shadowed lower-precedence unit would mislead the reader.
        """
        # systemd.unit(5) Table 1, "Load path when running in system mode",
        # highest precedence first (verified against the systemd 261 manual).
        table_1 = (
            "/etc/systemd/system.control",
            "/run/systemd/system.control",
            "/run/systemd/transient",
            "/run/systemd/generator.early",
            "/etc/systemd/system",
            "/etc/systemd/system.attached",
            "/run/systemd/system",
            "/run/systemd/system.attached",
            "/run/systemd/generator",
            "/usr/local/lib/systemd/system",
            "/usr/lib/systemd/system",
            "/run/systemd/generator.late",
        )
        dirs = rt._SYSTEMD_SYSTEM_UNIT_DIRS

        found_at = []
        for directory in table_1:
            assert directory in dirs, f"systemd searches {directory} but the probe does not"
            found_at.append(dirs.index(directory))
        assert found_at == sorted(found_at), f"not in systemd precedence order: {dirs}"

        # `/lib` is the pre-usr-merge alias of `/usr/lib` and is not in Table 1,
        # so it is asserted separately rather than loosening the check above.
        assert "/lib/systemd/system" in dirs

        # Both shapes resolve against that one list, so neither can search a
        # directory the other does not.
        for name in (rt._USER_MANAGER_TEMPLATE_NAME, "user@1000.service"):
            assert rt._unit_paths(name) == tuple(f"{d}/{name}" for d in dirs)

    def test_user_manager_unit_finds_a_hand_installed_uid_unit(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A template-less host can still have a manager, so don't refuse it.

        ``docs/guides/remote-and-mobile.md`` tells the reader to write
        ``/etc/systemd/system/user@$(id -u).service`` by hand and enable it. systemd
        loads that concrete unit without consulting the template, so a host that
        followed this repo's own guide has a working per-user instance and no
        template. Keying the platform gate on the template alone refused pods there.
        """
        vendor = tmp_path / "vendor"
        etc = tmp_path / "etc"
        vendor.mkdir()
        etc.mkdir()
        monkeypatch.setattr(rt, "_SYSTEMD_SYSTEM_UNIT_DIRS", (str(etc), str(vendor)))
        assert rt.user_manager_unit(4242) is None

        # A uid unit in a NON-`/etc` directory must count. Searching a narrower
        # list for this shape than for the template is what made the probe report
        # "absent" on a host with a working manager, which is the bug this PR fixes.
        (vendor / "user@4242.service").touch()
        # Build the expectation the way the probe does. These are systemd unit
        # paths, so the probe joins with "/" on every platform; rendering the
        # expectation through `Path` would emit a backslash on Windows and fail
        # this assertion on the separator rather than on the behaviour under test.
        assert rt.user_manager_unit(4242) == f"{vendor}/user@4242.service"
        # Another uid's unit says nothing about this one.
        assert rt.user_manager_unit(99) is None

    def test_a_uid_unit_gets_enable_linger_not_the_platform_refusal(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """With a manager unit present the instance is merely stopped — linger works.

        This is the other side of the split: the platform refusal must be reserved
        for hosts that genuinely cannot run a pod, or it becomes the same misdirection
        in the opposite direction.
        """
        monkeypatch.setattr(rt, "IS_LINUX", True)
        monkeypatch.setattr(rt.shutil, "which", lambda _n: "/usr/bin/systemctl")
        self._bus(monkeypatch, tmp_path / "run", exists=False)
        monkeypatch.setenv("USER", "tester")
        monkeypatch.setattr(
            rt, "user_manager_unit", lambda _uid=None: "/etc/systemd/system/user@7.service"
        )
        with pytest.raises(rt.PodBackendAbsent) as exc:
            rt.require_systemd()
        msg = str(exc.value)
        assert "Fix: loginctl enable-linger" in msg
        assert "cannot help" not in msg

    def test_missing_bus_is_reported_without_spawning(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(rt, "IS_MACOS", False)
        monkeypatch.setattr(rt, "IS_WINDOWS", False)
        monkeypatch.setattr(rt, "IS_LINUX", True)
        monkeypatch.setattr(rt.shutil, "which", lambda _n: "/usr/bin/systemctl")
        self._bus(monkeypatch, tmp_path / "run", exists=False)
        spawned: list[list[str]] = []
        monkeypatch.setattr(rt, "_run", lambda cmd, **_kwargs: spawned.append(cmd))

        result = rt.probe_user_bus()
        assert result.status == rt.USER_BUS_NO_SESSION
        assert result.detail == ""
        with pytest.raises(rt.PodBackendAbsent):
            rt.require_systemd()
        with pytest.raises(rt.PodBackendAbsent):
            rt.require_backend()
        assert spawned == []

    def test_systemctl_env_is_the_only_env_source_for_systemd_calls(self) -> None:
        """Anti-regression: a future direct ``subprocess.run(["systemctl", ...])``
        that forgets ``env=_systemctl_env()`` would silently reintroduce the bug,
        so pin every systemd/journalctl spawn in the pod runtime -- ``runtime.py``
        and every ``runtime_*.py`` owner beside it -- to that one source.
        """
        import ast

        tree = ast.parse(Path(rt.__file__).read_text(encoding="utf-8"))
        owner_trees = [
            ast.parse(source.read_text(encoding="utf-8"))
            for source in sorted(Path(rt.__file__).parent.glob("runtime_*.py"))
        ]
        assert owner_trees, "no pod runtime owner found beside runtime.py; the scan is mis-aimed"

        # An owner reaches the core as ``runtime.<name>``, so both helpers also
        # accept that spelling: ``runtime.subprocess.run`` and
        # ``env=runtime._systemctl_env()``.
        def _is_subprocess_run(node: ast.Call) -> bool:
            fn = node.func
            if not (isinstance(fn, ast.Attribute) and fn.attr == "run"):
                return False
            receiver = fn.value
            if isinstance(receiver, ast.Name):
                return receiver.id == "subprocess"
            return (
                isinstance(receiver, ast.Attribute)
                and receiver.attr == "subprocess"
                and isinstance(receiver.value, ast.Name)
                and receiver.value.id == "runtime"
            )

        def _uses_systemctl_env(node: ast.Call) -> bool:
            for kw in node.keywords:
                if kw.arg != "env":
                    continue
                val = kw.value
                if not isinstance(val, ast.Call):
                    return False
                fn = val.func
                if isinstance(fn, ast.Name):
                    return fn.id == "_systemctl_env"
                return (
                    isinstance(fn, ast.Attribute)
                    and fn.attr == "_systemctl_env"
                    and isinstance(fn.value, ast.Name)
                    and fn.value.id == "runtime"
                )
            return False

        literal_systemd = 0
        for node in (n for t in (tree, *owner_trees) for n in ast.walk(t)):
            if not isinstance(node, ast.Call) or not _is_subprocess_run(node):
                continue
            argv = node.args[0] if node.args else None
            if not isinstance(argv, ast.List):
                continue
            head = argv.elts[0] if argv.elts else None
            if not isinstance(head, ast.Constant) or head.value not in (
                "systemctl",
                "journalctl",
            ):
                continue
            literal_systemd += 1
            assert _uses_systemctl_env(
                node
            ), f"line {node.lineno}: {head.value} spawned without env=_systemctl_env()"

        # journalctl in recent_journal is the one literal systemd spawn; if that
        # drops to zero the scan above has stopped covering anything.
        assert literal_systemd >= 1, "no literal systemd spawn found — scan is inert"
        # The variable-argv chokepoint every systemctl() call funnels through.
        chokepoint = next(
            n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_run"
        )
        assert any(
            _is_subprocess_run(n) and _uses_systemctl_env(n)
            for n in ast.walk(chokepoint)
            if isinstance(n, ast.Call)
        ), "_run no longer passes env=_systemctl_env()"
