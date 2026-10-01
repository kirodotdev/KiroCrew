"""Whether the service manager could relaunch the gateway: the watchdog's last check."""

from __future__ import annotations

import errno
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew import dep_sync, gateway_restart, platform_compat
from kiro_crew.dashboard import stale_asset_watchdog
from kiro_crew.update_ownership import Reentry

pytestmark = pytest.mark.skipif(not platform_compat.IS_POSIX, reason="systemd/launchd hosts")


def _exec_start(command: str, argv: str | None = None) -> str:
    argv = f"{command} gateway --no-open" if argv is None else argv
    return (
        f"{{ path={command} ; argv[]={argv} ; ignore_errors=no ; "
        "start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }"
    )


class _Systemd:
    """What ``systemctl show`` answers for kirocrew.service, per scope."""

    def __init__(self) -> None:
        self.scopes: dict[bool, dict[str, list[str]]] = {
            False: {"LoadState": ["not-found"]},
            True: {"LoadState": ["not-found"]},
        }

    def runs(
        self,
        command: str,
        *,
        user: bool = False,
        environment: str = "",
        env_files: str = "",
        reload: str = "no",
        main_pid: int | None = None,
        argv: str | None = None,
    ) -> None:
        self.scopes[user] = {
            "LoadState": ["loaded"],
            "MainPID": [str(os.getpid() if main_pid is None else main_pid)],
            "NeedDaemonReload": [reload],
            "ExecStart": [_exec_start(command, argv)] if command else [""],
            "Environment": [environment],
            "EnvironmentFiles": [env_files],
        }

    def show(self, user: bool) -> dict[str, list[str]]:
        return self.scopes[user]


@pytest.fixture
def systemd(monkeypatch):
    """A gateway launched by a generated systemd unit; ``systemd.runs`` sets its ExecStart."""
    from kiro_crew.service import common, live_target

    monkeypatch.setenv("KIROCREW_SERVICE_MANAGED", "1")
    monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
    monkeypatch.setattr(common, "current_platform", lambda: common.Platform.SYSTEMD)
    monkeypatch.setattr(live_target, "read_target_reason", lambda: (None, None))
    fake = _Systemd()
    monkeypatch.setattr(gateway_restart, "_systemd_show", fake.show)
    return fake


@pytest.fixture
def probe(monkeypatch):
    """Record each import probe; answer with ``probe.answer``."""

    class _Probe:
        answer: object = True
        calls: list[tuple[str, dict[str, str]]] = []

        def __call__(self, interpreter, *, env, timeout):
            self.calls.append((str(interpreter), dict(env)))
            if isinstance(self.answer, BaseException):
                raise self.answer
            return self.answer

    recorder = _Probe()
    recorder.calls = []
    monkeypatch.setattr(dep_sync, "imports_gateway_entry_point", recorder)
    return recorder


def _script(path: Path, interpreter: str, body: str = "import kiro_crew.cli\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{interpreter}\n{body}", encoding="utf-8")
    path.chmod(0o755)
    return path


def _reentry():
    return gateway_restart.supervisor_reentry()


# --- whether there is a supervisor at all --------------------------------------


def test_without_a_service_manager_nothing_is_refused(monkeypatch):
    monkeypatch.delenv("KIROCREW_SERVICE_MANAGED", raising=False)
    assert _reentry().status is Reentry.REENTERABLE


def test_a_marker_the_dashboard_consumed_still_counts(systemd, tmp_path):
    """start_dashboard takes the marker out of the environment before the watchdog runs."""
    from kiro_crew.config.loader import consume_managed_service_launch_environment

    systemd.runs(str(tmp_path / "gone" / "kirocrew"))
    consume_managed_service_launch_environment()
    assert "KIROCREW_SERVICE_MANAGED" not in os.environ

    assert _reentry().status is Reentry.REFUSED


def test_an_exec_restart_hands_the_consumed_marker_to_its_successor(monkeypatch):
    """The successor of an in-app restart is the same service launch."""
    from kiro_crew.config.loader import consume_managed_service_launch_environment

    monkeypatch.setenv("KIROCREW_SERVICE_MANAGED", "1")
    consume_managed_service_launch_environment()
    seen = []
    monkeypatch.setattr(
        platform_compat.os,
        "execv",
        lambda *_a: seen.append(os.environ.get("KIROCREW_SERVICE_MANAGED")),
    )
    monkeypatch.setattr(platform_compat, "_disarm_process_alarm_before_exec", lambda: None)

    platform_compat.reexec_python_module("kiro_crew", ["gateway"], executable=sys.executable)

    assert seen == ["1"]


# --- what systemd would run ----------------------------------------------------


def test_a_stable_link_to_a_healthy_tree_relaunches_after_the_old_tree_is_pruned(
    systemd, probe, tmp_path, monkeypatch
):
    """The supervisor runs its own command, not this process's (pruned) interpreter."""
    new_tree = _script(tmp_path / "1.2.5" / "bin" / "kirocrew", sys.executable)
    stable = tmp_path / "bin" / "kirocrew"
    stable.parent.mkdir()
    stable.symlink_to(new_tree)
    systemd.runs(str(stable))
    healthy = sys.executable
    monkeypatch.setattr(sys, "executable", str(tmp_path / "1.2.4" / "bin" / "python"))

    assert _reentry().status is Reentry.REENTERABLE
    assert [interp for interp, _env in probe.calls] == [healthy]


def test_a_dangling_command_link_is_refused(systemd, tmp_path):
    """The venv the link pointed into is gone: the relaunch would fail to exec."""
    dangling = tmp_path / "bin" / "kirocrew"
    dangling.parent.mkdir()
    dangling.symlink_to(tmp_path / "venv" / "bin" / "kirocrew")
    systemd.runs(str(dangling))

    verdict = _reentry()
    assert verdict.status is Reentry.REFUSED
    assert "missing" in verdict.reason


def test_the_unit_that_runs_this_gateway_is_the_one_judged(systemd, probe, tmp_path):
    """A stale system unit beside the user unit that runs us says nothing about the relaunch."""
    systemd.runs(str(tmp_path / "stale" / "kirocrew"), user=False, main_pid=0)
    systemd.runs(str(_script(tmp_path / "bin" / "kirocrew", sys.executable)), user=True)

    assert _reentry().status is Reentry.REENTERABLE


def test_no_unit_running_this_gateway_is_inconclusive(systemd, tmp_path):
    systemd.runs(str(tmp_path / "gone"), main_pid=0)

    verdict = _reentry()
    assert verdict.status is Reentry.INCONCLUSIVE
    assert "runs this gateway" in verdict.reason


def test_a_unit_changed_since_it_was_loaded_is_inconclusive(systemd, tmp_path):
    """The relaunch could run either version, so neither is judged."""
    systemd.runs(str(tmp_path / "gone"), reload="yes")

    assert _reentry().status is Reentry.INCONCLUSIVE


def test_a_unit_with_no_exec_start_is_inconclusive(systemd):
    systemd.runs("")

    assert _reentry().status is Reentry.INCONCLUSIVE


def test_a_service_manager_that_does_not_answer_is_inconclusive(systemd, monkeypatch):
    def _wedged(_user):
        raise subprocess.TimeoutExpired("systemctl", gateway_restart._SYSTEMCTL_TIMEOUT_SECS)

    monkeypatch.setattr(gateway_restart, "_systemd_show", _wedged)

    verdict = _reentry()
    assert verdict.status is Reentry.INCONCLUSIVE and "did not answer" in verdict.reason


def test_systemctl_show_is_parsed_and_bounded(monkeypatch, tmp_path):
    """The real reader: drop-ins merged by systemd, one bounded query per scope."""
    from kiro_crew.service import common

    command = tmp_path / "bin" / "kirocrew"
    trusted = str(tmp_path / "trusted" / "systemctl")
    calls = []
    stdout = "\n".join(
        [
            "LoadState=loaded",
            f"MainPID={os.getpid()}",
            "NeedDaemonReload=no",
            f"ExecStart={_exec_start(str(command))}",
            'Environment=KIROCREW_SERVICE_MANAGED=1 "PYTHONPATH=/srv/a b"',
            "EnvironmentFiles=/nonexistent/kirocrew.env (ignore_errors=yes)",
        ]
    )

    def _run(argv, **kwargs):
        calls.append((argv, kwargs.get("timeout")))
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(common, "current_platform", lambda: common.Platform.SYSTEMD)
    monkeypatch.setattr(platform_compat, "trusted_system_bin", lambda name: trusted)
    monkeypatch.setattr(gateway_restart.subprocess, "run", _run)

    found, args, env = gateway_restart._supervisor_launch()

    assert found == str(command)
    assert args == ["gateway", "--no-open"]
    assert env["PYTHONPATH"] == "/srv/a b"
    [(argv, timeout)] = calls
    assert argv[0] == trusted and "--user" not in argv
    assert timeout == gateway_restart._SYSTEMCTL_TIMEOUT_SECS


def test_a_systemctl_on_path_is_never_run(monkeypatch, tmp_path):
    """The check runs inside the gateway, whose PATH can lead with a writable directory."""
    from kiro_crew.service import common

    planted = tmp_path / "planted"
    ran = tmp_path / "shim-ran"
    _script(planted / "systemctl", "/bin/sh", f"touch {shlex.quote(str(ran))}\n")
    monkeypatch.setenv("PATH", f"{planted}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("KIROCREW_SERVICE_MANAGED", "1")
    monkeypatch.setattr(common, "current_platform", lambda: common.Platform.SYSTEMD)
    monkeypatch.setattr(platform_compat, "trusted_system_bin", lambda name: None)

    verdict = _reentry()

    assert verdict.status is Reentry.INCONCLUSIVE
    assert "trusted system directory" in verdict.reason
    assert not ran.exists()


def test_the_probe_runs_under_the_units_own_environment(systemd, probe, tmp_path):
    """Environment= then EnvironmentFile= (which wins), as systemd applies them."""
    env_file = tmp_path / "kirocrew.env"
    env_file.write_text(
        "# comment\nPYTHONPATH=/from/file\nPYTHONUSERBASE='/home/u/.local'\n", encoding="utf-8"
    )
    systemd.runs(
        str(_script(tmp_path / "bin" / "kirocrew", sys.executable)),
        environment="PYTHONPATH=/from/unit HOME=/home/u",
        env_files=f"{env_file} (ignore_errors=no)",
    )

    assert _reentry().status is Reentry.REENTERABLE
    [(_interp, env)] = probe.calls
    assert env["PYTHONPATH"] == "/from/file"
    assert env["HOME"] == "/home/u"
    assert env["PYTHONUSERBASE"] == "/home/u/.local"


def test_a_required_environment_file_that_is_missing_is_inconclusive(systemd, tmp_path):
    systemd.runs(
        str(_script(tmp_path / "bin" / "kirocrew", sys.executable)),
        env_files=f"{tmp_path / 'gone.env'} (ignore_errors=no)",
    )

    assert _reentry().status is Reentry.INCONCLUSIVE


# --- the command itself --------------------------------------------------------


def test_a_venv_killed_mid_install_is_refused_with_the_installer_as_its_repair(
    systemd, probe, tmp_path
):
    """Interpreter present, gateway not importable: the relaunch would die at import."""
    systemd.runs(str(_script(tmp_path / "venv" / "bin" / "kirocrew", sys.executable)))
    probe.answer = False

    verdict = _reentry()
    assert verdict.status is Reentry.REFUSED
    assert verdict.reason.endswith("repair it with: re-run the installer")


def test_an_editable_checkouts_own_venv_is_told_to_reinstall_itself(
    systemd, probe, tmp_path, monkeypatch
):
    proj = tmp_path / "my$checkout"  # pasted into a shell: must come out quoted
    venv_py = dep_sync.project_venv_python(proj)
    venv_py.parent.mkdir(parents=True)
    os.symlink(sys.executable, venv_py)
    (proj / ".install-method").write_text("pip\n", encoding="utf-8")
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(proj))
    systemd.runs(str(_script(venv_py.parent / "kirocrew", str(venv_py))))
    probe.answer = False

    verdict = _reentry()
    expected = f"{shlex.quote(str(venv_py))} -m pip install -e {shlex.quote(str(proj))}"
    assert verdict.reason.endswith(f"repair it with: {expected}")


def test_another_installs_interpreter_is_never_told_to_install_this_checkout(
    systemd, probe, tmp_path, monkeypatch
):
    """A live target or a stale project_dir names a tree the supervisor's venv is not."""
    proj = tmp_path / "worktree"
    proj.mkdir()
    (proj / ".install-method").write_text("pip\n", encoding="utf-8")
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(proj))
    systemd.runs(str(_script(tmp_path / "managed" / "bin" / "kirocrew", sys.executable)))
    probe.answer = False

    assert _reentry().reason.endswith("repair it with: re-run the installer")


def test_a_script_whose_interpreter_is_gone_is_refused(systemd, tmp_path):
    systemd.runs(str(_script(tmp_path / "bin" / "kirocrew", str(tmp_path / "venv" / "python"))))

    verdict = _reentry()
    assert verdict.status is Reentry.REFUSED and verdict.reason.endswith("which is missing")


@pytest.mark.parametrize(
    "header",
    [
        '#!/bin/sh\nexec /opt/kirocrew/bin/kirocrew "$@"\n',
        "#!/bin/sh\n'''exec' \"/opt/a b/python3\" \"$0\" \"$@\"\n' '''\n",
        "#!/usr/bin/env python3\n",
    ],
    ids=["shell-wrapper", "pip-trampoline", "env-lookup"],
)
def test_a_wrapper_this_cannot_follow_is_inconclusive_not_refused(systemd, probe, tmp_path, header):
    """A shell is not a Python; probing it as one would refuse a working wrapper."""
    wrapper = tmp_path / "bin" / "kirocrew"
    wrapper.parent.mkdir()
    wrapper.write_text(header, encoding="utf-8")
    wrapper.chmod(0o755)
    systemd.runs(str(wrapper))

    assert _reentry().status is Reentry.INCONCLUSIVE
    assert probe.calls == []


def test_a_native_binary_that_exists_and_executes_relaunches(systemd, probe, tmp_path):
    binary = tmp_path / "bin" / "kirocrew"
    binary.parent.mkdir()
    binary.write_bytes(b"\x7fELF\x02\x01\x01")
    binary.chmod(0o755)
    systemd.runs(str(binary))

    assert _reentry().status is Reentry.REENTERABLE
    assert probe.calls == []


def _native(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x7fELF\x02\x01\x01")
    path.chmod(0o755)
    return path


def test_a_native_binary_not_run_as_the_gateway_is_a_wrapper(systemd, probe, tmp_path):
    """``bash -c ...`` always starts; what it runs is in its arguments."""
    shell = _native(tmp_path / "bin" / "bash")
    systemd.runs(str(shell), argv=f"{shell} -c /gone/kirocrew gateway")

    assert _reentry().status is Reentry.INCONCLUSIVE
    assert probe.calls == []


@pytest.mark.parametrize("body", [b"", b"import kiro_crew.cli\n"], ids=["empty", "no-shebang"])
def test_a_command_the_kernel_will_not_run_is_refused(systemd, probe, tmp_path, body):
    """A console script truncated mid-write: executable, but its exec fails."""
    command = tmp_path / "bin" / "kirocrew"
    command.parent.mkdir()
    command.write_bytes(body)
    command.chmod(0o755)
    systemd.runs(str(command))

    verdict = _reentry()
    assert verdict.status is Reentry.REFUSED
    assert "not a script or a binary" in verdict.reason
    assert probe.calls == []


def test_an_env_that_is_gone_is_refused_before_its_program_is_judged(systemd, probe, tmp_path):
    env = tmp_path / "usr" / "bin" / "env"
    command = _script(tmp_path / "venv" / "bin" / "kirocrew", sys.executable)
    systemd.runs(str(env), argv=f"{env} {command} gateway --no-open")

    verdict = _reentry()
    assert verdict.status is Reentry.REFUSED
    assert str(env) in verdict.reason
    assert probe.calls == []


def test_env_is_followed_to_a_program_that_is_gone(systemd, tmp_path):
    env = _native(tmp_path / "usr" / "bin" / "env")
    gone = tmp_path / "removed" / "kirocrew"
    systemd.runs(str(env), argv=f"{env} {gone} gateway --no-open")

    verdict = _reentry()
    assert verdict.status is Reentry.REFUSED
    assert str(gone) in verdict.reason


def test_env_is_followed_to_the_script_it_runs_with_its_assignments(systemd, probe, tmp_path):
    env = _native(tmp_path / "usr" / "bin" / "env")
    command = _script(tmp_path / "venv" / "bin" / "kirocrew", sys.executable)
    systemd.runs(str(env), argv=f"{env} KIROCREW_PROBE_MARK=1 {command} gateway --no-open")

    verdict = _reentry()
    assert verdict.status is Reentry.REENTERABLE
    assert verdict.probed is True
    assert [(interpreter, e.get("KIROCREW_PROBE_MARK")) for interpreter, e in probe.calls] == [
        (sys.executable, "1")
    ]


@pytest.mark.parametrize(
    "rest",
    ["kirocrew gateway", "-i /venv/bin/kirocrew gateway", "A=1"],
    ids=["path-lookup", "option", "no-program"],
)
def test_env_that_this_check_cannot_follow_is_inconclusive(systemd, probe, tmp_path, rest):
    env = _native(tmp_path / "usr" / "bin" / "env")
    systemd.runs(str(env), argv=f"{env} {rest}")

    assert _reentry().status is Reentry.INCONCLUSIVE
    assert probe.calls == []


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads through a 000 directory"
)
def test_an_unsearchable_command_directory_is_refused(systemd, tmp_path):
    """The service runs as this user, so its exec cannot reach the command either."""
    command = _script(tmp_path / "locked" / "kirocrew", sys.executable)
    systemd.runs(str(command))
    command.parent.chmod(0)
    try:
        assert _reentry().status is Reentry.REFUSED
    finally:
        command.parent.chmod(0o755)


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads an exec-only file"
)
def test_an_exec_only_script_is_refused(systemd, tmp_path):
    """Its interpreter would have to read it, and cannot."""
    command = _script(tmp_path / "bin" / "kirocrew", sys.executable)
    command.chmod(0o111)
    systemd.runs(str(command))

    assert _reentry().status is Reentry.REFUSED


@pytest.mark.parametrize("code", [errno.EIO, errno.ESTALE], ids=["eio", "estale"])
def test_a_command_that_cannot_be_checked_is_inconclusive(systemd, tmp_path, monkeypatch, code):
    """An I/O error is a check that could not run, not a missing command."""
    command = _script(tmp_path / "bin" / "kirocrew", sys.executable)
    systemd.runs(str(command))
    real_stat = os.stat

    def _stat(path, *args, **kwargs):
        if os.fspath(path) == str(command):
            raise OSError(code, os.strerror(code))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", _stat)

    assert _reentry().status is Reentry.INCONCLUSIVE


@pytest.mark.parametrize(
    "error, status",
    [
        (OSError(errno.EIO, "I/O error"), Reentry.INCONCLUSIVE),
        (PermissionError(errno.EACCES, "denied"), Reentry.REFUSED),
        (OSError(errno.ENOEXEC, "Exec format error"), Reentry.REFUSED),
        (subprocess.TimeoutExpired("python", 10), Reentry.INCONCLUSIVE),
    ],
    ids=["io-error", "eacces", "enoexec", "probe-timeout"],
)
def test_only_a_probe_failure_that_proves_the_exec_would_fail_refuses(
    systemd, probe, tmp_path, error, status
):
    systemd.runs(str(_script(tmp_path / "bin" / "kirocrew", sys.executable)))
    probe.answer = error

    assert _reentry().status is status


# --- the hops after the supervisor's command ------------------------------------


def test_a_live_target_that_starts_but_cannot_import_is_refused(systemd, tmp_path, monkeypatch):
    """The hop execs, so a target venv killed mid-install is what the relaunch runs."""
    from kiro_crew.service import live_target

    systemd.runs(str(_script(tmp_path / "bin" / "kirocrew", sys.executable)))
    checkout = tmp_path / "worktree"
    target_python = checkout / ".venv" / "bin" / "python"
    target_python.parent.mkdir(parents=True)
    target_python.symlink_to(sys.executable)
    _script(live_target.target_bin(checkout), str(target_python))
    monkeypatch.setattr(live_target, "read_target_reason", lambda: (checkout, None))
    monkeypatch.setattr(
        dep_sync,
        "imports_gateway_entry_point",
        lambda interpreter, *, env, timeout: str(interpreter) != str(target_python),
    )

    verdict = _reentry()
    assert verdict.status is Reentry.REFUSED
    assert "its live target" in verdict.reason


@pytest.mark.parametrize("broken", ["dangling-interpreter", "missing-entry-point"])
def test_a_live_target_whose_exec_fails_falls_back_to_the_supervisors_command(
    systemd, probe, tmp_path, monkeypatch, broken
):
    """``maybe_reexec`` swallows a failed exec and boots the supervisor's own build."""
    from kiro_crew.service import live_target

    systemd.runs(str(_script(tmp_path / "bin" / "kirocrew", sys.executable)))
    checkout = tmp_path / "worktree"
    if broken == "dangling-interpreter":
        dangling = checkout / ".venv" / "bin" / "python"
        dangling.parent.mkdir(parents=True)
        dangling.symlink_to(tmp_path / "base-python-removed")
        _script(live_target.target_bin(checkout), str(dangling))
    monkeypatch.setattr(live_target, "read_target_reason", lambda: (checkout, None))

    verdict = _reentry()
    assert verdict.status is Reentry.REENTERABLE
    assert [interp for interp, _env in probe.calls] == [sys.executable]


@pytest.mark.parametrize("mode", [0o700, 0o600], ids=["executable", "not-executable"])
def test_the_launchd_launcher_is_followed_to_its_target(monkeypatch, tmp_path, probe, mode):
    """Only once launchd could exec the launcher itself."""
    from kiro_crew.service import common, live_target, macos

    target = _script(tmp_path / "venv" / "bin" / "kirocrew", sys.executable)
    launcher = tmp_path / "live-gateway"
    launcher.write_text(macos.render_live_program(str(target)), encoding="utf-8")
    launcher.chmod(mode)
    plist = tmp_path / "agent.plist"
    plist.write_text("unused", encoding="utf-8")
    monkeypatch.setenv("KIROCREW_SERVICE_MANAGED", "1")
    monkeypatch.setattr(common, "current_platform", lambda: common.Platform.LAUNCHD)
    monkeypatch.setattr(live_target, "read_target_reason", lambda: (None, None))
    monkeypatch.setattr("kiro_crew.service.controller.installed_unit_path", lambda: plist)
    monkeypatch.setattr(macos, "LIVE_PROGRAM", launcher)
    monkeypatch.setattr(
        macos,
        "_plist_payload",
        lambda _p: {
            "ProgramArguments": [str(launcher), "gateway"],
            "EnvironmentVariables": {"PYTHONPATH": "/from/plist"},
        },
    )

    verdict = _reentry()
    if mode == 0o600:
        assert verdict.status is Reentry.REFUSED
        assert "not executable" in verdict.reason
        assert probe.calls == []
        return
    assert verdict.status is Reentry.REENTERABLE
    [(interp, env)] = probe.calls
    assert interp == sys.executable
    assert env["PYTHONPATH"] == "/from/plist"


def test_the_whole_check_fits_inside_the_watchdogs_bound():
    """Two scope queries and two probes (the command, then a live target) at their bounds."""
    worst = (
        2 * gateway_restart._SYSTEMCTL_TIMEOUT_SECS + 2 * gateway_restart._IMPORT_PROBE_TIMEOUT_SECS
    )
    assert worst < stale_asset_watchdog._REENTRY_CHECK_TIMEOUT_SECS


# --- the probe itself ------------------------------------------------------------


def test_the_probe_honours_the_units_pythonpath(tmp_path):
    """Not isolated: a relaunch runs without -I, so PYTHONPATH reaches it too."""
    shadow = tmp_path / "shadow" / "kiro_crew"
    shadow.mkdir(parents=True)
    (shadow / "__init__.py").write_text("raise ImportError('shadowed')\n", encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(shadow.parent)}

    assert dep_sync.imports_gateway_entry_point(Path(sys.executable), env=env, timeout=60) is False


def test_the_probe_ignores_the_callers_working_directory(tmp_path, monkeypatch):
    shadow = tmp_path / "kiro_crew"
    shadow.mkdir()
    (shadow / "__init__.py").write_text("raise ImportError('shadowed')\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    src = str(Path(gateway_restart.__file__).resolve().parents[1])
    env = {**os.environ, "PYTHONPATH": src}

    assert dep_sync.imports_gateway_entry_point(Path(sys.executable), env=env, timeout=60) is True


def test_the_probe_keeps_none_of_its_output(monkeypatch):
    """Only the exit status answers, so nothing the import prints is buffered."""
    seen = {}

    def _run(argv, **kwargs):
        seen.update(kwargs, argv=argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(dep_sync.subprocess, "run", _run)
    assert dep_sync.imports_gateway_entry_point(Path(sys.executable), env={}, timeout=1) is True
    assert "capture_output" not in seen
    assert seen["stdout"] is subprocess.DEVNULL
    assert seen["stderr"] is subprocess.DEVNULL
    # What ``gateway`` dispatches into too, not only the CLI's own module.
    assert seen["argv"][-1] == "import kiro_crew.cli, kiro_crew.cli_server"
