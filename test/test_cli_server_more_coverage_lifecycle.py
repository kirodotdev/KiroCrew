"""The process commands of ``kiro_crew.cli_server``: stop, restart, service, logs and runs.

This file pins ``_stop`` (the service manager first, the gateway-lock holder a port
probe misses, the whole-tree kill on Windows) and ``restart`` on an indeterminate lock,
the service and sandbox commands and what each audits, ``_logs_cmd`` across the system
and user journals, launchd's stdout file and a plain log file, ``_gateway``'s start-up
reconciliation and flags, and ``_run_task``'s spec validation, embedding degradation
and terminal statuses. It is one of three files on ``cli_server``'s command
dispatchers; the family's seams are described in ``test_cli_server_more_coverage.py``.
"""

import argparse
import asyncio
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest
from cli_server_more_coverage_helpers import (  # noqa: F401 -- the fixture is requested by name
    sel_rec_fixture,
)

from kiro_crew import cli_server, platform_compat
from kiro_crew.gateway_lock import LockHolder, LockProbeError
from kiro_crew.service import linux as svc_linux
from kiro_crew.service import macos as svc_macos
from kiro_crew.service.common import Platform, RestartReport

# --------------------------------------------------------------------------
# _stop
# --------------------------------------------------------------------------


class TestStopViaService:
    """No ``--port`` means the service manager gets first refusal."""

    def test_service_stop_short_circuits_port_scan(self, monkeypatch, sel_rec, capsys) -> None:
        monkeypatch.setattr(cli_server, "resolve_client_port", lambda p: 5476)
        monkeypatch.setattr(cli_server.service_controller, "stop_service", lambda: True)

        def unreachable(port):  # pragma: no cover - asserts the short-circuit
            raise AssertionError("port scan must not run when the service stopped")

        monkeypatch.setattr(platform_compat, "find_listening_pids", unreachable)
        cli_server._stop(None)
        assert "Stopped kirocrew service" in capsys.readouterr().out
        assert sel_rec.calls[0]["resources"].endswith("via=service")

    def test_explicit_port_bypasses_the_service(self, monkeypatch, sel_rec, capsys) -> None:
        monkeypatch.setattr(cli_server, "resolve_client_port", lambda p: 8123)

        def unreachable():  # pragma: no cover - asserts the bypass
            raise AssertionError("service must not be consulted for an explicit port")

        monkeypatch.setattr(cli_server.service_controller, "stop_service", unreachable)
        monkeypatch.setattr(platform_compat, "find_listening_pids", lambda port: [])
        monkeypatch.setattr(
            cli_server, "lock_holder", lambda home: LockHolder(pid=None, alive=False, source="none")
        )
        with pytest.raises(SystemExit) as exc:
            cli_server._stop(8123)
        assert exc.value.code == 1
        assert "No Kiro Crew gateway currently running on port 8123" in capsys.readouterr().out
        assert sel_rec.calls[-1]["outcome"] == "no_target"


class TestStopLockHolderFallback:
    """The port probe is not how single-instance is enforced -- gateway.lock
    is (gateway_lock.py). A gateway the port probe misses (unix-socket-only,
    or a probe blind spot) must still be VISIBLE through the lock, or
    `kirocrew stop` reports "nothing running" the same moment `kirocrew
    gateway` refuses to start for a live pid -- the split-brain this fixes.
    The lock is an oracle, not a signalling path: a live holder is refused
    and named (lock path, pid, manual command) with one ``denied`` event, and
    nothing is signalled, whoever holds it.
    """

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing class-scoped autouse fixture moved verbatim and no test in the cli_server coverage files calls monkeypatch.undo()
    def _arrange(self, monkeypatch):
        monkeypatch.setattr(cli_server, "resolve_client_port", lambda p: 5476)
        monkeypatch.setattr(cli_server.service_controller, "stop_service", lambda: False)
        monkeypatch.setattr(platform_compat, "find_listening_pids", lambda port: [])
        monkeypatch.setattr(platform_compat, "listening_pid_tool_available", lambda: True)
        monkeypatch.setattr(cli_server, "_report_authenticated_shutdown", lambda port: False)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(cli_server, "_pid_exited", lambda pid: True)
        # flake-ok: pre-existing time.sleep stub moved verbatim; the stop path's exit poll returns at once and no test waits on real time
        # nosemgrep: kirocrew.test-stdlib-clock-rebound -- pre-existing time.sleep stub moved verbatim so the stop path's exit poll returns at once
        monkeypatch.setattr("time.sleep", lambda s: None)

    @pytest.fixture
    def signals(self, monkeypatch) -> list[int]:
        """Record every pid the command would signal, on either platform."""
        sent: list[int] = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append(pid))
        monkeypatch.setattr(platform_compat, "kill_process_tree", lambda pid, sig: sent.append(pid))
        return sent

    def test_live_kirocrew_lock_holder_is_refused_and_named(
        self, monkeypatch, sel_rec, capsys, signals
    ) -> None:
        monkeypatch.setattr(
            cli_server,
            "lock_holder",
            lambda home: LockHolder(pid=4242, alive=True, source="flock_owner"),
        )
        monkeypatch.setattr(cli_server, "_is_kirocrew_process", lambda pid: True)
        daemon_stops: list[int] = []
        monkeypatch.setattr(cli_server, "_stop_mcp_gateway_daemon", lambda: daemon_stops.append(1))
        with pytest.raises(SystemExit) as exc:
            cli_server._stop(None)
        assert exc.value.code == 1
        assert signals == []
        assert daemon_stops == []
        out = capsys.readouterr().out
        assert "gateway.lock" in out
        assert "pid 4242" in out
        assert "could not reach it" in out
        assert "kill -TERM 4242" in out
        assert len(sel_rec.calls) == 1
        assert sel_rec.calls[0]["operation"] == "gateway_stop"
        assert sel_rec.calls[0]["outcome"] == "denied"
        assert "pids=[4242]" in sel_rec.calls[0]["resources"]
        assert "reason=lock_holder_kirocrew" in sel_rec.calls[0]["resources"]

    def test_live_kirocrew_lock_holder_on_windows_names_taskkill(
        self, monkeypatch, sel_rec, capsys, signals
    ) -> None:
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(
            cli_server,
            "lock_holder",
            lambda home: LockHolder(pid=4242, alive=True, source="flock_owner"),
        )
        monkeypatch.setattr(cli_server, "_is_kirocrew_process", lambda pid: True)
        with pytest.raises(SystemExit) as exc:
            cli_server._stop(None)
        assert exc.value.code == 1
        assert signals == []
        assert "taskkill /PID 4242 /T" in capsys.readouterr().out
        assert [c["outcome"] for c in sel_rec.calls] == ["denied"]

    def test_dead_lock_holder_still_reports_no_target(self, monkeypatch, sel_rec, capsys) -> None:
        monkeypatch.setattr(
            cli_server,
            "lock_holder",
            lambda home: LockHolder(pid=4242, alive=False, source="flock_owner"),
        )
        with pytest.raises(SystemExit) as exc:
            cli_server._stop(None)
        assert exc.value.code == 1
        assert "No Kiro Crew gateway currently running" in capsys.readouterr().out
        assert sel_rec.calls[-1]["outcome"] == "no_target"

    def test_live_lock_holder_is_refused_without_a_port_tool(
        self, monkeypatch, sel_rec, capsys, signals
    ) -> None:
        """The lock file needs no `lsof`: with the port tool unavailable and a
        live Kiro Crew holder, stop names the holder instead of exiting on the
        tool diagnostic. The diagnostic is for the case where nothing found a
        gateway, not a gate in front of the fallbacks that need no tool."""
        monkeypatch.setattr(platform_compat, "listening_pid_tool_available", lambda: False)
        monkeypatch.setattr(platform_compat, "listening_pid_tool", lambda: "lsof")
        monkeypatch.setattr(platform_compat, "tool_outside_trusted_dirs", lambda tool: None)
        monkeypatch.setattr(
            cli_server,
            "lock_holder",
            lambda home: LockHolder(pid=4242, alive=True, source="flock_owner"),
        )
        monkeypatch.setattr(cli_server, "_is_kirocrew_process", lambda pid: True)
        daemon_stops: list[int] = []
        monkeypatch.setattr(cli_server, "_stop_mcp_gateway_daemon", lambda: daemon_stops.append(1))
        with pytest.raises(SystemExit) as exc:
            cli_server._stop(None)
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert signals == []
        assert daemon_stops == []
        assert "not found" not in out and "Install lsof" not in out
        assert "kill -TERM 4242" in out
        assert len(sel_rec.calls) == 1
        assert sel_rec.calls[0]["outcome"] == "denied"
        assert "reason=lock_holder_kirocrew" in sel_rec.calls[0]["resources"]

    def test_no_holder_and_no_port_tool_keeps_the_tool_diagnostic(
        self, monkeypatch, sel_rec, capsys
    ) -> None:
        """Both tool-free fallbacks find nothing: the tool-absent exit is still
        what the operator sees, because "nothing running" cannot be proven
        without the port lookup."""
        monkeypatch.setattr(platform_compat, "listening_pid_tool_available", lambda: False)
        monkeypatch.setattr(platform_compat, "listening_pid_tool", lambda: "lsof")
        monkeypatch.setattr(platform_compat, "tool_outside_trusted_dirs", lambda tool: None)
        monkeypatch.setattr(
            cli_server, "lock_holder", lambda home: LockHolder(pid=None, alive=False, source="none")
        )
        with pytest.raises(SystemExit) as exc:
            cli_server._stop(None)
        assert exc.value.code == 1
        assert "Install lsof and retry." in capsys.readouterr().out
        assert "reason=lsof_not_found" in sel_rec.calls[-1]["resources"]

    def test_non_kirocrew_lock_holder_is_refused_not_stopped(
        self, monkeypatch, sel_rec, capsys, signals
    ) -> None:
        monkeypatch.setattr(
            cli_server,
            "lock_holder",
            lambda home: LockHolder(pid=99, alive=True, source="flock_owner"),
        )
        monkeypatch.setattr(cli_server, "_is_kirocrew_process", lambda pid: False)
        with pytest.raises(SystemExit) as exc:
            cli_server._stop(None)
        assert exc.value.code == 1
        assert signals == []
        out = capsys.readouterr().out
        assert "does not look like a Kiro Crew gateway" in out
        assert "kill -TERM 99" in out
        assert len(sel_rec.calls) == 1
        assert sel_rec.calls[0]["operation"] == "gateway_stop"
        assert sel_rec.calls[0]["outcome"] == "denied"
        assert "pids=[99]" in sel_rec.calls[0]["resources"]
        assert "reason=lock_holder_foreign" in sel_rec.calls[0]["resources"]

    def test_indeterminate_probe_signals_nothing_and_exits_non_zero(
        self, monkeypatch, sel_rec, capsys, signals
    ) -> None:
        # The probe could not say held-or-free. Signalling the pid the file
        # records could hit an unrelated process that reused the number, so
        # `stop` names the problem and exits 1 without touching any pid.
        lock_path = Path("/var/lib/kirocrew/gateway.lock")

        def indeterminate(home):
            raise LockProbeError(lock_path, OSError("flock unsupported"))

        monkeypatch.setattr(cli_server, "lock_holder", indeterminate)
        with pytest.raises(SystemExit) as exc:
            cli_server._stop(None)
        assert exc.value.code == 1
        assert signals == []
        out = capsys.readouterr().out
        assert f"could not determine whether a gateway holds the lock at {lock_path}" in out
        assert "flock unsupported" in out
        assert sel_rec.calls[-1]["outcome"] == "denied"
        assert "lock_probe_indeterminate" in sel_rec.calls[-1]["resources"]


class TestRestartIndeterminateLock:
    """``restart`` on an indeterminate lock probe must neither signal the
    recorded pid nor spawn a replacement the lock may refuse."""

    def test_indeterminate_probe_signals_nothing_spawns_nothing(
        self, monkeypatch, sel_rec, capsys
    ) -> None:
        monkeypatch.setattr(cli_server, "resolve_client_port", lambda p: 5476)
        monkeypatch.setattr(
            cli_server.service_controller, "restart_service", lambda: RestartReport()
        )
        monkeypatch.setattr(cli_server.service_controller, "is_service_active", lambda: False)
        monkeypatch.setattr(cli_server.service_controller, "installed_unit_path", lambda: None)
        monkeypatch.setattr(platform_compat, "find_listening_pids", lambda port: [])
        monkeypatch.setattr(platform_compat, "listening_pid_tool_available", lambda: True)
        monkeypatch.setattr(cli_server, "_report_authenticated_shutdown", lambda port: False)
        monkeypatch.setattr(cli_server.run_marker, "read_pid", lambda port: None)
        lock_path = Path("/var/lib/kirocrew/gateway.lock")

        def indeterminate(home):
            raise LockProbeError(lock_path, OSError("flock unsupported"))

        monkeypatch.setattr(cli_server, "lock_holder", indeterminate)
        stopped: list[int] = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: stopped.append(pid))
        spawned: list[int] = []
        monkeypatch.setattr(
            cli_server, "_spawn_detached_gateway", lambda port: spawned.append(port)
        )
        with pytest.raises(SystemExit) as exc:
            cli_server._restart(None)
        assert exc.value.code == 1
        assert stopped == []
        assert spawned == []
        out = capsys.readouterr().out
        assert f"could not determine whether a gateway holds the lock at {lock_path}" in out
        assert "not starting a replacement" in out
        assert sel_rec.calls[-1]["operation"] == "gateway_restart"
        assert sel_rec.calls[-1]["outcome"] == "denied"


class TestStopOnWindows:
    """The Windows branch kills the whole tree via ``platform_compat``."""

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing class-scoped autouse fixture moved verbatim and no test in the cli_server coverage files calls monkeypatch.undo()
    def _windows(self, monkeypatch):
        monkeypatch.setattr(cli_server, "resolve_client_port", lambda p: 5476)
        monkeypatch.setattr(cli_server.service_controller, "stop_service", lambda: False)
        monkeypatch.setattr(platform_compat, "find_listening_pids", lambda port: [4242])
        monkeypatch.setattr(cli_server, "_is_kirocrew_process", lambda pid: True)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(cli_server, "_pid_exited", lambda pid: True)
        # flake-ok: pre-existing time.sleep stub moved verbatim; the stop path's exit poll returns at once and no test waits on real time
        # nosemgrep: kirocrew.test-stdlib-clock-rebound -- pre-existing time.sleep stub moved verbatim so the stop path's exit poll returns at once
        monkeypatch.setattr("time.sleep", lambda s: None)

    def test_tree_kill_reports_terminated(self, monkeypatch, sel_rec, capsys) -> None:
        seen: list[int] = []
        monkeypatch.setattr(platform_compat, "kill_process_tree", lambda pid, sig: seen.append(pid))
        cli_server._stop(None)
        out = capsys.readouterr().out
        assert seen == [4242]
        assert "Terminated gateway (pid 4242)" in out
        assert sel_rec.calls[-1]["outcome"] == "allowed"

    def test_already_gone_pid_is_not_reported_as_stopped(
        self, monkeypatch, sel_rec, capsys
    ) -> None:
        def gone(pid, sig):
            raise ProcessLookupError

        monkeypatch.setattr(platform_compat, "kill_process_tree", gone)
        with pytest.raises(SystemExit) as exc:
            cli_server._stop(None)
        assert exc.value.code == 1
        assert "process already exited" in capsys.readouterr().out

    def test_permission_error_asks_for_sudo_and_exits(self, monkeypatch, sel_rec, capsys) -> None:
        def denied(pid, sig):
            raise PermissionError

        monkeypatch.setattr(platform_compat, "kill_process_tree", denied)
        with pytest.raises(SystemExit) as exc:
            cli_server._stop(None)
        assert exc.value.code == 1
        assert "No permission to stop pid 4242" in capsys.readouterr().out
        assert sel_rec.calls[-1]["outcome"] == "denied"

    def test_generic_taskkill_failure_rechecks_liveness(self, monkeypatch, capsys) -> None:
        """An OSError from taskkill is only "denied" if the pid is still alive."""

        def oserr(pid, sig):
            raise OSError("taskkill exit 1")

        monkeypatch.setattr(platform_compat, "kill_process_tree", oserr)
        monkeypatch.setattr(platform_compat, "pid_exists", lambda pid: True)
        with pytest.raises(SystemExit) as exc:
            cli_server._stop(None)
        assert exc.value.code == 1
        assert "No permission to stop pid 4242" in capsys.readouterr().out

    def test_generic_taskkill_failure_on_dead_pid_is_not_denied(self, monkeypatch, capsys) -> None:
        def oserr(pid, sig):
            raise OSError("taskkill exit 1")

        monkeypatch.setattr(platform_compat, "kill_process_tree", oserr)
        monkeypatch.setattr(platform_compat, "pid_exists", lambda pid: False)
        with pytest.raises(SystemExit) as exc:
            cli_server._stop(None)
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "process already exited" in out
        assert "No permission" not in out


# --------------------------------------------------------------------------
# _service_cmd / _sandbox_cmd
# --------------------------------------------------------------------------


class TestServiceCmd:
    """Each action forwards to the controller and audits the resulting rc."""

    @pytest.mark.parametrize(
        "action,fn,operation",
        [
            ("install", "install_service", "service_install"),
            ("uninstall", "uninstall_service", "service_uninstall"),
            ("status", "service_status", "service_status"),
        ],
    )
    def test_action_forwards_and_audits(self, monkeypatch, sel_rec, action, fn, operation) -> None:
        monkeypatch.setattr(cli_server.service_controller, fn, lambda: 0)
        rc = cli_server._service_cmd(argparse.Namespace(service_action=action))
        assert rc == 0
        assert sel_rec.operations == [operation]
        assert sel_rec.calls[0]["outcome"] == "allowed"

    def test_nonzero_rc_is_audited_as_error(self, monkeypatch, sel_rec) -> None:
        monkeypatch.setattr(cli_server.service_controller, "install_service", lambda: 3)
        assert cli_server._service_cmd(argparse.Namespace(service_action="install")) == 3
        assert sel_rec.calls[0]["outcome"] == "error"
        assert "rc=3" in sel_rec.calls[0]["resources"]

    def test_unknown_action_prints_usage_and_returns_2(self, sel_rec, capsys) -> None:
        assert cli_server._service_cmd(argparse.Namespace(service_action=None)) == 2
        assert "Usage: kirocrew service" in capsys.readouterr().err
        assert sel_rec.calls == []


class TestSandboxCmd:
    """Profile writes are audited; the read-only status probe is not."""

    def test_install_profile_forwards_path_and_audits(self, monkeypatch, sel_rec) -> None:
        seen: list[str | None] = []
        monkeypatch.setattr(
            cli_server.service_controller,
            "install_launcher_profile",
            lambda p: seen.append(p) or 0,
        )
        rc = cli_server._sandbox_cmd(
            argparse.Namespace(sandbox_action="install-profile", path="/opt/app.AppImage")
        )
        assert rc == 0
        assert seen == ["/opt/app.AppImage"]
        assert sel_rec.operations == ["sandbox_profile_install"]
        assert "/opt/app.AppImage" in sel_rec.calls[0]["resources"]

    def test_install_profile_without_path_records_appimage_placeholder(
        self, monkeypatch, sel_rec
    ) -> None:
        monkeypatch.setattr(cli_server.service_controller, "install_launcher_profile", lambda p: 1)
        assert (
            cli_server._sandbox_cmd(argparse.Namespace(sandbox_action="install-profile", path=None))
            == 1
        )
        assert sel_rec.calls[0]["outcome"] == "error"
        assert "$APPIMAGE" in sel_rec.calls[0]["resources"]

    def test_remove_profile_audits(self, monkeypatch, sel_rec) -> None:
        monkeypatch.setattr(cli_server.service_controller, "remove_launcher_profile", lambda: 0)
        assert cli_server._sandbox_cmd(argparse.Namespace(sandbox_action="remove-profile")) == 0
        assert sel_rec.operations == ["sandbox_profile_remove"]

    def test_status_is_not_audited(self, monkeypatch, sel_rec) -> None:
        monkeypatch.setattr(cli_server.service_controller, "sandbox_profile_status", lambda p: 7)
        rc = cli_server._sandbox_cmd(argparse.Namespace(sandbox_action="status", path=None))
        assert rc == 7
        assert sel_rec.calls == []

    def test_unknown_action_prints_usage_and_returns_2(self, sel_rec, capsys) -> None:
        assert cli_server._sandbox_cmd(argparse.Namespace(sandbox_action="bogus")) == 2
        assert "Usage: kirocrew sandbox" in capsys.readouterr().err
        assert sel_rec.calls == []


# --------------------------------------------------------------------------
# _logs_cmd
# --------------------------------------------------------------------------


class _ExecCalled(Exception):
    """Raised by the ``os.execvp`` stub — the real call never returns."""

    def __init__(self, file: str, argv: list[str]) -> None:
        super().__init__(file)
        self.file = file
        self.argv = argv


# One journal ENTRY as `journalctl -o short` prints it: a timestamp first. The
# probes below answer this where they mean "the journal has rows".
_ENTRY_LINE = "Sep 26 03:00:00 host kirocrew[4242]: gateway listening\n"


@pytest.fixture
def fake_execvp(monkeypatch):
    def _execvp(file, argv):
        raise _ExecCalled(file, list(argv))

    monkeypatch.setattr(os, "execvp", _execvp)


class TestLogsCmdSystemd:
    """Journal first, sudo only as a fallback, and never a blind sudo prompt."""

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing class-scoped autouse fixture moved verbatim and no test in the cli_server coverage files calls monkeypatch.undo()
    def _systemd(self, monkeypatch, tmp_path):
        unit = tmp_path / "kirocrew.service"
        unit.write_text("[Unit]\n", encoding="utf-8", newline="\n")
        monkeypatch.setattr(cli_server, "current_platform", lambda: Platform.SYSTEMD)
        monkeypatch.setattr(svc_linux, "UNIT_PATH", unit)

    def test_unprivileged_journal_is_execed_when_probe_returns_rows(
        self, monkeypatch, sel_rec, fake_execvp
    ) -> None:
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a[0], 0, _ENTRY_LINE, ""),
        )
        with pytest.raises(_ExecCalled) as exc:
            cli_server._logs_cmd(argparse.Namespace(follow=True, lines=42))
        assert exc.value.file == "journalctl"
        assert exc.value.argv[0] == "journalctl"
        assert "-f" in exc.value.argv
        assert "42" in exc.value.argv
        # Audited BEFORE the exec, or the record would never be written.
        assert sel_rec.operations == ["logs"]
        assert "follow=True lines=42" in sel_rec.calls[0]["resources"]

    def test_empty_probe_without_tty_refuses_instead_of_hanging_on_sudo(
        self, monkeypatch, sel_rec, capsys
    ) -> None:
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", "denied")
        )
        monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
        with pytest.raises(SystemExit) as exc:
            cli_server._logs_cmd(argparse.Namespace(follow=False, lines=10))
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "stdin is not a TTY" in err
        assert "systemd-journal" in err

    def test_empty_probe_with_tty_falls_back_to_sudo_journalctl(
        self, monkeypatch, sel_rec, fake_execvp
    ) -> None:
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "   \n", "")
        )
        monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
        with pytest.raises(_ExecCalled) as exc:
            cli_server._logs_cmd(argparse.Namespace(follow=True, lines=5))
        assert exc.value.file == "sudo"
        assert exc.value.argv[:2] == ["sudo", "journalctl"]
        assert exc.value.argv[-1] == "-f"
        assert "--no-pager" in exc.value.argv

    def test_rows_are_execed_unprivileged_even_with_the_access_hint_on_stderr(
        self, monkeypatch, sel_rec, fake_execvp
    ) -> None:
        """An operator outside `adm` / `systemd-journal` reading a unit that runs
        `User=<them>`: journalctl prints the access hint on stderr because the
        SYSTEM journal files are EACCES, yet still prints the unit's own rows
        from the ACL-readable `user-<uid>.journal`. Rows are rows — the hint
        alone must not push this user to a sudo prompt (or, with no TTY, an
        "Insufficient permissions" exit) for logs it can already read."""
        hint = (
            "Hint: You are currently not seeing messages from other users and the system.\n"
            "      Users in groups 'adm', 'systemd-journal' can see all messages.\n"
            "      Pass -q to turn off this notice.\n"
        )
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a[0], 0, _ENTRY_LINE, hint),
        )
        monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
        with pytest.raises(_ExecCalled) as exc:
            cli_server._logs_cmd(argparse.Namespace(follow=False, lines=3))
        assert exc.value.file == "journalctl"
        assert "sudo" not in exc.value.argv

    def test_a_notice_with_the_access_hint_is_execed_as_the_base_did(
        self, monkeypatch, sel_rec, fake_execvp
    ) -> None:
        """The base pin, kept: the sudo rung is for a probe that printed NOTHING.
        A probe that printed `-- No entries --` (with journalctl's access hint
        on stderr) is exec'd unprivileged and shows that notice plus the hint,
        which is what the base showed such a user; the hint is not read."""
        hint = "Hint: You are currently not seeing messages from other users and the system.\n"
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "-- No entries --\n", hint),
        )
        monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
        with pytest.raises(_ExecCalled) as exc:
            cli_server._logs_cmd(argparse.Namespace(follow=False, lines=3))
        assert exc.value.file == "journalctl"
        assert "sudo" not in exc.value.argv

    def test_missing_unit_falls_through_to_the_plain_log_file(
        self, monkeypatch, tmp_path, sel_rec, fake_execvp
    ) -> None:
        monkeypatch.setattr(svc_linux, "UNIT_PATH", tmp_path / "absent.service")
        log = tmp_path / "gateway.log"
        log.write_text("hi\n", encoding="utf-8", newline="\n")
        monkeypatch.setattr(cli_server, "config_dir", lambda: tmp_path)

        def only_the_user_scope_query(argv, *a, **k):
            # No system unit: the one spawn allowed is the user-scope
            # `systemctl --user show` that asks whether THAT scope has the unit
            # (answer: no). journalctl must not be probed without an installed
            # unit in either scope.
            if list(argv)[:3] == ["systemctl", "--user", "show"]:
                return subprocess.CompletedProcess(argv, 0, "LoadState=not-found\n", "")
            raise AssertionError(f"unexpected spawn without an installed unit: {argv}")

        monkeypatch.setattr(subprocess, "run", only_the_user_scope_query)
        with pytest.raises(_ExecCalled) as exc:
            cli_server._logs_cmd(argparse.Namespace(follow=False, lines=3))
        assert exc.value.file == "tail"
        assert exc.value.argv == ["tail", "-n", "3", str(log)]


class TestLogsCmdUserScope:
    """A gateway running as the per-user unit (the SELinux remedy) has its logs in
    the USER journal, which `journalctl --user` reads without privilege."""

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing class-scoped autouse fixture moved verbatim and no test in the cli_server coverage files calls monkeypatch.undo()
    def _user_unit_only(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cli_server, "current_platform", lambda: Platform.SYSTEMD)
        monkeypatch.setattr(svc_linux, "UNIT_PATH", tmp_path / "absent.service")
        monkeypatch.setattr(svc_linux, "user_unit_installed", lambda: True)

    def test_user_journal_is_execed_when_only_the_user_unit_exists(
        self, monkeypatch, sel_rec, fake_execvp
    ) -> None:
        probes: list[list[str]] = []

        def probe(argv, *a, **k):
            probes.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, _ENTRY_LINE, "")

        monkeypatch.setattr(subprocess, "run", probe)
        with pytest.raises(_ExecCalled) as exc:
            cli_server._logs_cmd(argparse.Namespace(follow=True, lines=42))
        assert exc.value.file == "journalctl"
        assert exc.value.argv[:2] == ["journalctl", "--user"]
        assert "-u" in exc.value.argv and "kirocrew.service" in exc.value.argv
        assert "42" in exc.value.argv
        assert exc.value.argv[-1] == "-f"
        assert probes == [
            [
                "journalctl",
                "--user",
                "--quiet",
                "-u",
                "kirocrew.service",
                "-n",
                "1",
                "--no-pager",
            ]
        ]
        assert sel_rec.operations == ["logs"]

    def test_an_empty_user_journal_saying_no_entries_still_falls_through(
        self, monkeypatch, tmp_path, sel_rec, fake_execvp
    ) -> None:
        """A journal with no matching entries answers `-- No entries --` on STDOUT
        with exit 0 (real journalctl, systemd 252) — which is not a row. The probe
        asks quietly, so the notice is suppressed and the log file is tailed
        instead of exec-ing a journal that would show nothing."""
        log = tmp_path / "gateway.log"
        log.write_text("hi\n", encoding="utf-8", newline="\n")
        monkeypatch.setattr(cli_server, "config_dir", lambda: tmp_path)
        probes: list[list[str]] = []

        def journalctl(argv, *a, **k):
            probes.append(list(argv))
            quiet = "--quiet" in argv or "-q" in argv
            return subprocess.CompletedProcess(argv, 0, "" if quiet else "-- No entries --\n", "")

        monkeypatch.setattr(subprocess, "run", journalctl)
        monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
        with pytest.raises(_ExecCalled) as exc:
            cli_server._logs_cmd(argparse.Namespace(follow=False, lines=3))
        assert exc.value.file == "tail"
        assert exc.value.argv == ["tail", "-n", "3", str(log)]
        assert probes and all("--user" in p for p in probes), probes

    def test_an_empty_user_journal_falls_through_to_the_log_file_without_sudo(
        self, monkeypatch, tmp_path, sel_rec, fake_execvp
    ) -> None:
        # The user journal never needs sudo, so an empty probe is not a
        # permission problem to escalate past: the file is the next source.
        log = tmp_path / "gateway.log"
        log.write_text("hi\n", encoding="utf-8", newline="\n")
        monkeypatch.setattr(cli_server, "config_dir", lambda: tmp_path)
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", "")
        )
        monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
        with pytest.raises(_ExecCalled) as exc:
            cli_server._logs_cmd(argparse.Namespace(follow=False, lines=3))
        assert exc.value.file == "tail"
        assert exc.value.argv == ["tail", "-n", "3", str(log)]

    def test_a_running_user_unit_wins_over_a_leftover_system_unit_file(
        self, monkeypatch, tmp_path, sel_rec, fake_execvp
    ) -> None:
        """Both scopes have a unit (a stopped system unit from an earlier install,
        the gateway running as the user unit): the journal shown is the running
        unit's, not the dead one's."""
        unit = tmp_path / "kirocrew.service"
        unit.write_text("[Unit]\n", encoding="utf-8", newline="\n")
        monkeypatch.setattr(svc_linux, "UNIT_PATH", unit)
        monkeypatch.setattr(svc_linux, "user_unit_active", lambda: True)
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a[0], 0, _ENTRY_LINE, ""),
        )
        with pytest.raises(_ExecCalled) as exc:
            cli_server._logs_cmd(argparse.Namespace(follow=False, lines=7))
        assert exc.value.argv[:2] == ["journalctl", "--user"]

    def test_an_inactive_user_unit_leaves_the_system_journal_first(
        self, monkeypatch, tmp_path, sel_rec, fake_execvp
    ) -> None:
        unit = tmp_path / "kirocrew.service"
        unit.write_text("[Unit]\n", encoding="utf-8", newline="\n")
        monkeypatch.setattr(svc_linux, "UNIT_PATH", unit)
        monkeypatch.setattr(svc_linux, "user_unit_active", lambda: False)
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a[0], 0, _ENTRY_LINE, ""),
        )
        with pytest.raises(_ExecCalled) as exc:
            cli_server._logs_cmd(argparse.Namespace(follow=False, lines=7))
        assert exc.value.argv[0] == "journalctl"
        assert "--user" not in exc.value.argv


class TestLogsCmdOtherSources:
    """launchd stdout file, plain log file, and the "nothing to tail" refusal."""

    @pytest.fixture
    def launchd(self, monkeypatch, tmp_path):
        """An installed launchd agent: a plist on disk and a non-empty stdout log.

        Both paths are patched even when a test only cares about one of them,
        because the real ones are consulted otherwise and a CI runner has
        neither.
        """
        plist = tmp_path / "crew.plist"
        plist.write_text("<plist/>\n", encoding="utf-8", newline="\n")
        stdout_log = tmp_path / "launchd-gateway.log"
        stdout_log.write_text("x\n", encoding="utf-8", newline="\n")
        monkeypatch.setattr(cli_server, "current_platform", lambda: Platform.LAUNCHD)
        monkeypatch.setattr(svc_macos, "PLIST_PATH", plist)
        monkeypatch.setattr(svc_macos, "STDOUT_LOG", stdout_log)
        return types.SimpleNamespace(plist=plist, stdout_log=stdout_log)

    def test_launchd_stdout_log_is_tailed(self, sel_rec, fake_execvp, launchd) -> None:
        with pytest.raises(_ExecCalled) as exc:
            cli_server._logs_cmd(argparse.Namespace(follow=True, lines=8))
        assert exc.value.argv == ["tail", "-n", "8", "-f", str(launchd.stdout_log)]

    def test_launchd_without_an_installed_plist_falls_through(
        self, monkeypatch, tmp_path, sel_rec, fake_execvp, launchd
    ) -> None:
        """A foreground gateway on macOS reaches the config-dir log, not the agent's."""
        launchd.plist.unlink()
        fallback = tmp_path / "fallback" / "gateway.log"
        fallback.parent.mkdir()
        fallback.write_text("real\n", encoding="utf-8", newline="\n")
        monkeypatch.setattr(cli_server, "config_dir", lambda: fallback.parent)
        with pytest.raises(_ExecCalled) as exc:
            cli_server._logs_cmd(argparse.Namespace(follow=False, lines=4))
        assert exc.value.argv == ["tail", "-n", "4", str(fallback)]

    def test_launchd_with_an_empty_stdout_log_falls_through(
        self, monkeypatch, tmp_path, sel_rec, fake_execvp, launchd
    ) -> None:
        """A 0-byte agent log satisfies exists(), so size is what gates the branch."""
        launchd.stdout_log.write_text("", encoding="utf-8", newline="\n")
        fallback = tmp_path / "fallback" / "gateway.log"
        fallback.parent.mkdir()
        fallback.write_text("real\n", encoding="utf-8", newline="\n")
        monkeypatch.setattr(cli_server, "config_dir", lambda: fallback.parent)
        with pytest.raises(_ExecCalled) as exc:
            cli_server._logs_cmd(argparse.Namespace(follow=False, lines=4))
        assert exc.value.argv == ["tail", "-n", "4", str(fallback)]

    def test_no_log_source_at_all_exits_with_guidance(
        self, monkeypatch, tmp_path, sel_rec, capsys
    ) -> None:
        monkeypatch.setattr(cli_server, "current_platform", lambda: Platform.UNSUPPORTED)
        monkeypatch.setattr(cli_server, "config_dir", lambda: tmp_path)
        with pytest.raises(SystemExit) as exc:
            cli_server._logs_cmd(argparse.Namespace(follow=False, lines=100))
        assert exc.value.code == 1
        assert "No gateway logs found" in capsys.readouterr().err
        assert sel_rec.operations == ["logs"]

    def test_zero_lines_argument_falls_back_to_the_default(
        self, monkeypatch, tmp_path, sel_rec, capsys
    ) -> None:
        """``lines=0`` selects the default tail length on an unsupported service host."""
        monkeypatch.setattr(cli_server, "current_platform", lambda: Platform.UNSUPPORTED)
        monkeypatch.setattr(cli_server, "config_dir", lambda: tmp_path)
        (tmp_path / "gateway.log").write_text("x\n", encoding="utf-8", newline="\n")
        cli_server._logs_cmd(argparse.Namespace(follow=False, lines=0))
        assert capsys.readouterr().out == "x\n"


# --------------------------------------------------------------------------
# _gateway
# --------------------------------------------------------------------------


class _FakeCfg:
    """Config double for ``_gateway`` — records ``save()`` instead of writing."""

    saved = 0

    def save(self) -> None:
        type(self).saved += 1

    @classmethod
    def load(cls) -> "_FakeCfg":
        return cls()


class TestGateway:
    """Start-up reconciliation runs, then ``run_gateway`` receives the flags verbatim."""

    @pytest.fixture
    def gw(self, monkeypatch, tmp_path):
        captured: dict = {}

        async def _run_gateway(cfg, **kw):
            captured["cfg"] = cfg
            captured["kw"] = kw

        monkeypatch.setattr(cli_server, "run_gateway", _run_gateway)
        monkeypatch.setattr(cli_server, "activate_mise", lambda: [])
        monkeypatch.setattr(cli_server, "ensure_dev_dist_symlink", lambda: tmp_path / "dist")
        monkeypatch.setattr(cli_server, "_should_reconcile_launchd_launcher", lambda: False)
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(cli_server, "config_path", lambda: cfg_file)

        class _Cfg(_FakeCfg):
            saved = 0

        monkeypatch.setattr(cli_server, "KiroCrewConfig", _Cfg)
        monkeypatch.setattr("kiro_crew.cli._node_ok", lambda: True)
        return captured, _Cfg

    def test_flags_are_forwarded_to_run_gateway(self, gw) -> None:
        captured, cfg_cls = gw
        asyncio.run(
            cli_server._gateway(
                no_dashboard=True, no_crons=True, no_open=True, port_override="9001"
            )
        )
        assert captured["kw"]["no_dashboard"] is True
        assert captured["kw"]["no_crons"] is True
        assert captured["kw"]["port_override"] == "9001"
        # Existing config file -> no default config written.
        assert cfg_cls.saved == 0

    def test_missing_config_is_created_before_load(self, gw, monkeypatch, tmp_path, capsys) -> None:
        captured, cfg_cls = gw
        monkeypatch.setattr(cli_server, "config_path", lambda: tmp_path / "absent.json")
        asyncio.run(cli_server._gateway())
        assert cfg_cls.saved == 1
        assert "Created default config" in capsys.readouterr().out

    def test_missing_dist_warns_but_still_starts(self, gw, monkeypatch, caplog) -> None:
        captured, _ = gw
        monkeypatch.setattr(cli_server, "ensure_dev_dist_symlink", lambda: None)
        with caplog.at_level("WARNING", logger="kiro_crew.cli_server"):
            asyncio.run(cli_server._gateway(no_dashboard=False))
        assert any("dist/ not found" in r.message for r in caplog.records)
        assert "kw" in captured

    def test_the_dist_is_resolved_off_the_event_loop(self, gw, monkeypatch) -> None:
        """An edition start waits for the staging lock; on the loop thread that wait is skipped."""
        from kiro_crew.atomic_write import on_event_loop

        seen: list[bool] = []
        monkeypatch.setattr(
            cli_server, "ensure_dev_dist_symlink", lambda: seen.append(on_event_loop()) or None
        )
        asyncio.run(cli_server._gateway(no_dashboard=False))
        assert seen == [False]

    def test_slack_only_skips_the_dist_check(self, gw, monkeypatch) -> None:
        captured, _ = gw

        def unreachable():  # pragma: no cover - proves the skip
            raise AssertionError("dist must not be resolved in slack-only mode")

        monkeypatch.setattr(cli_server, "ensure_dev_dist_symlink", unreachable)
        asyncio.run(cli_server._gateway(no_dashboard=True))
        assert captured["kw"]["no_dashboard"] is True

    def test_stale_node_triggers_ensure_node(self, gw, monkeypatch) -> None:
        calls: list[str] = []
        monkeypatch.setattr("kiro_crew.cli._node_ok", lambda: False)
        monkeypatch.setattr("kiro_crew.cli._ensure_node", lambda *a: calls.append("ensured"))
        asyncio.run(cli_server._gateway(no_dashboard=True))
        assert calls == ["ensured"]

    def test_mise_activation_is_logged_when_it_changes_the_env(
        self, gw, monkeypatch, caplog
    ) -> None:
        monkeypatch.setattr(cli_server, "activate_mise", lambda: ["PATH"])
        with caplog.at_level("INFO", logger="kiro_crew.cli_server"):
            asyncio.run(cli_server._gateway(no_dashboard=True))
        assert any("Activated mise" in r.message for r in caplog.records)

    def test_launchd_launcher_repair_failure_is_non_fatal(self, gw, monkeypatch, caplog) -> None:
        captured, _ = gw
        monkeypatch.setattr(cli_server, "_should_reconcile_launchd_launcher", lambda: True)

        def boom():
            raise OSError("read-only Application Support")

        monkeypatch.setattr(svc_macos, "ensure_live_program", boom)
        with caplog.at_level("WARNING", logger="kiro_crew.cli_server"):
            asyncio.run(cli_server._gateway(no_dashboard=True))
        assert any("live-gateway launcher" in r.message for r in caplog.records)
        assert "kw" in captured  # the gateway still started


# --------------------------------------------------------------------------
# _run_task
# --------------------------------------------------------------------------


class _FakeStore:
    def __init__(self, *a, **kw) -> None:
        self.kwargs = kw
        self.inited = False
        self.builtins_synced = False

    def init(self) -> None:
        self.inited = True

    def sync_builtins(self) -> None:
        # _run_task now syncs builtin skills through the explicit seam
        # (SkillsLoader(install_builtins=False) + to_thread(sync_builtins)).
        self.builtins_synced = True


class _FakeVectorStore(_FakeStore):
    embed_fn = None
    embed_fn_factory = None


class _FakeSessions:
    def __init__(self, cfg, provider_factory=None) -> None:
        self.cfg = cfg
        self.pool_started = False
        self.closed = False

    async def start_pool(self) -> None:
        self.pool_started = True

    async def close_all(self) -> None:
        self.closed = True


class _Result:
    def __init__(self, status: str, error: str = "") -> None:
        self.status = status
        self.error = error
        self.name = "demo"
        self.task_id = "t-1"
        self.tasks = [1, 2, 3]


@pytest.fixture
def taskrunner_env(monkeypatch, tmp_path):
    """Replace every collaborator ``_run_task`` constructs, and expose the spies."""
    from kiro_crew.config import KiroCrewConfig

    state: dict = {"vector": None, "sessions": None, "runner_kwargs": None, "observed": []}

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cls()))
    monkeypatch.setattr(cli_server, "KiroCrewConfig", KiroCrewConfig)
    monkeypatch.setattr(cli_server, "build_provider_factory", lambda cfg: object())

    def _sessions(cfg, provider_factory=None):
        state["sessions"] = _FakeSessions(cfg, provider_factory)
        return state["sessions"]

    monkeypatch.setattr(cli_server, "SessionManager", _sessions)
    monkeypatch.setattr(cli_server, "MemoryStore", _FakeStore)

    def _vector(**kw):
        state["vector"] = _FakeVectorStore(**kw)
        return state["vector"]

    monkeypatch.setattr(cli_server, "VectorMemoryStore", _vector)
    monkeypatch.setattr(cli_server, "ConversationLog", _FakeStore)
    monkeypatch.setattr(cli_server, "LessonStore", _FakeStore)
    monkeypatch.setattr(cli_server, "SkillsLoader", _FakeStore)
    monkeypatch.setattr(cli_server, "HistoryConsolidator", lambda **kw: object())
    monkeypatch.setattr(cli_server, "hooks_config_from_config_dict", lambda h: {})
    monkeypatch.setattr(cli_server, "HookManager", lambda hc: object())
    monkeypatch.setattr(cli_server, "ContextBuilder", lambda **kw: object())
    monkeypatch.setattr(
        cli_server, "register_skill_read_observer", lambda ctx: state["observed"].append(ctx)
    )
    monkeypatch.setattr(cli_server, "_session_work_dir", lambda key: tmp_path)
    monkeypatch.setattr(cli_server, "make_sync_embed_fn", lambda: (lambda text: [0.0]))
    monkeypatch.setattr(cli_server, "model_file_present", lambda: True)
    monkeypatch.setattr(cli_server, "store_embedding_space_is_stale", lambda vs: False)

    def _install_runner(result: _Result) -> None:
        class _Runner:
            def __init__(self, **kw) -> None:
                state["runner_kwargs"] = kw

            async def run(self, spec_path, name=""):
                state["ran"] = (spec_path, name)
                return result

        monkeypatch.setattr(cli_server, "TaskRunner", _Runner)

    state["install_runner"] = _install_runner
    return state


def _spec(tmp_path: Path) -> Path:
    spec = tmp_path / "task.md"
    spec.write_text("# Task\n\n- step one\n", encoding="utf-8", newline="\n")
    return spec


class TestRunTask:
    """Spec validation, embedding degradation, and the three terminal statuses."""

    def test_missing_spec_exits_before_building_anything(self, tmp_path, capsys) -> None:
        args = argparse.Namespace(spec=str(tmp_path / "nope.md"))
        with pytest.raises(SystemExit) as exc:
            asyncio.run(cli_server._run_task(args))
        assert exc.value.code == 1
        assert "Spec file not found" in capsys.readouterr().err

    def test_completed_task_wires_runner_and_closes_sessions(
        self, taskrunner_env, tmp_path, capsys
    ) -> None:
        taskrunner_env["install_runner"](_Result("completed"))
        spec = _spec(tmp_path)
        args = argparse.Namespace(
            spec=str(spec), no_test=True, fresh=False, timeout=90, name="my-run"
        )
        asyncio.run(cli_server._run_task(args))
        kw = taskrunner_env["runner_kwargs"]
        assert kw["auto_test"] is False  # --no-test inverts into auto_test
        assert kw["fresh"] is False
        assert kw["global_timeout"] == 90.0
        assert taskrunner_env["ran"] == (spec.resolve(), "my-run")
        assert taskrunner_env["sessions"].pool_started is True
        assert taskrunner_env["sessions"].closed is True
        assert taskrunner_env["observed"]  # skill-read observer registered
        out = capsys.readouterr().out
        assert "Task completed" in out and "(3 steps)" in out

    def test_configured_test_command_reaches_the_runner(
        self, taskrunner_env, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.config import KiroCrewConfig

        cfg = KiroCrewConfig()
        cfg.taskrunner.test_command = ["python", "-m", "pytest", "-q"]
        cfg.taskrunner.require_verification = True
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))
        taskrunner_env["install_runner"](_Result("completed"))
        args = argparse.Namespace(
            spec=str(_spec(tmp_path)), no_test=False, fresh=False, timeout=0, name=""
        )
        asyncio.run(cli_server._run_task(args))
        kw = taskrunner_env["runner_kwargs"]
        assert kw["test_cmd"] == ["python", "-m", "pytest", "-q"]
        assert kw["require_verification"] is True

    def test_builtin_sync_runs_through_the_explicit_seam(
        self, taskrunner_env, tmp_path, monkeypatch
    ) -> None:
        # _run_task runs on a loop, where construction-time sync skips
        # itself: the loader must be built with install_builtins=False and
        # have sync_builtins driven through the explicit off-loop seam.
        created: list[_FakeStore] = []

        class _SpyLoader(_FakeStore):
            def __init__(self, *a, **kw) -> None:
                super().__init__(*a, **kw)
                created.append(self)

        monkeypatch.setattr(cli_server, "SkillsLoader", _SpyLoader)
        taskrunner_env["install_runner"](_Result("completed"))
        args = argparse.Namespace(
            spec=str(_spec(tmp_path)), no_test=True, fresh=False, timeout=90, name=""
        )
        asyncio.run(cli_server._run_task(args))
        assert any(
            inst.kwargs.get("install_builtins") is False and inst.builtins_synced
            for inst in created
        )

    def test_consolidator_gets_the_configured_migrated_flag(
        self, taskrunner_env, tmp_path, monkeypatch
    ) -> None:
        # The live config watcher only fires on change, so the boot value
        # must reach the consolidator, or markdown memory is rewritten on a
        # migrated install.
        from kiro_crew.config import KiroCrewConfig

        cfg = KiroCrewConfig()
        cfg.memory.migrated = True
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))
        seen: list[dict] = []
        monkeypatch.setattr(
            cli_server, "HistoryConsolidator", lambda **kw: seen.append(kw) or object()
        )
        taskrunner_env["install_runner"](_Result("completed"))
        args = argparse.Namespace(
            spec=str(_spec(tmp_path)), no_test=True, fresh=False, timeout=90, name=""
        )
        asyncio.run(cli_server._run_task(args))
        assert [kw.get("migrated") for kw in seen] == [True]
        # A migrated consolidator writes only to the vector store, so it must get one.
        assert seen[0].get("vector_store") is taskrunner_env["vector"]

    def test_failed_builtin_sync_does_not_gate_the_task(
        self, taskrunner_env, tmp_path, monkeypatch
    ) -> None:
        # A read-only skills dir (or any sync error) must degrade to the
        # skills already on disk, not kill the run before it starts.
        class _BrokenLoader(_FakeStore):
            def sync_builtins(self) -> None:
                raise OSError("skills dir unavailable")

        monkeypatch.setattr(cli_server, "SkillsLoader", _BrokenLoader)
        taskrunner_env["install_runner"](_Result("completed"))
        spec = _spec(tmp_path)
        args = argparse.Namespace(spec=str(spec), no_test=True, fresh=False, timeout=90, name="")
        asyncio.run(cli_server._run_task(args))  # must not raise
        assert taskrunner_env["ran"] == (spec.resolve(), "")

    def test_fresh_flag_is_forwarded_and_announced(self, taskrunner_env, tmp_path, capsys) -> None:
        taskrunner_env["install_runner"](_Result("completed"))
        args = argparse.Namespace(
            spec=str(_spec(tmp_path)), no_test=False, fresh=True, timeout=0, name=""
        )
        asyncio.run(cli_server._run_task(args))
        assert taskrunner_env["runner_kwargs"]["fresh"] is True
        assert taskrunner_env["runner_kwargs"]["auto_test"] is True
        assert "Running spec (fresh)" in capsys.readouterr().out

    def test_failed_task_exits_one_with_the_error(self, taskrunner_env, tmp_path, capsys) -> None:
        taskrunner_env["install_runner"](_Result("failed", error="step 2 blew up"))
        args = argparse.Namespace(
            spec=str(_spec(tmp_path)), no_test=False, fresh=False, timeout=0, name=""
        )
        with pytest.raises(SystemExit) as exc:
            asyncio.run(cli_server._run_task(args))
        assert exc.value.code == 1
        assert "step 2 blew up" in capsys.readouterr().err

    def test_cancelled_task_exits_one(self, taskrunner_env, tmp_path, capsys) -> None:
        taskrunner_env["install_runner"](_Result("cancelled"))
        args = argparse.Namespace(
            spec=str(_spec(tmp_path)), no_test=False, fresh=False, timeout=0, name=""
        )
        with pytest.raises(SystemExit) as exc:
            asyncio.run(cli_server._run_task(args))
        assert exc.value.code == 1
        assert "cancelled" in capsys.readouterr().out

    def test_embed_fn_is_bound_when_the_model_is_present(self, taskrunner_env, tmp_path) -> None:
        taskrunner_env["install_runner"](_Result("completed"))
        args = argparse.Namespace(
            spec=str(_spec(tmp_path)), no_test=False, fresh=False, timeout=0, name=""
        )
        asyncio.run(cli_server._run_task(args))
        assert taskrunner_env["vector"].embed_fn is not None
        assert taskrunner_env["vector"].embed_fn_factory is not None

    def test_absent_model_degrades_to_keyword_search_without_downloading(
        self, taskrunner_env, tmp_path, monkeypatch, capsys
    ) -> None:
        monkeypatch.setattr(cli_server, "model_file_present", lambda: False)

        def unreachable(vs):  # pragma: no cover - staleness needs an embed_fn
            raise AssertionError("staleness must not be probed without an embed_fn")

        monkeypatch.setattr(cli_server, "store_embedding_space_is_stale", unreachable)
        taskrunner_env["install_runner"](_Result("completed"))
        args = argparse.Namespace(
            spec=str(_spec(tmp_path)), no_test=False, fresh=False, timeout=0, name=""
        )
        asyncio.run(cli_server._run_task(args))
        assert taskrunner_env["vector"].embed_fn is None
        # The factory stays wired even without the model.
        assert taskrunner_env["vector"].embed_fn_factory is not None
        assert "keyword search for this run" in capsys.readouterr().err

    def test_stale_vector_space_clears_both_embed_hooks(
        self, taskrunner_env, tmp_path, monkeypatch, capsys
    ) -> None:
        monkeypatch.setattr(cli_server, "store_embedding_space_is_stale", lambda vs: True)
        taskrunner_env["install_runner"](_Result("completed"))
        args = argparse.Namespace(
            spec=str(_spec(tmp_path)), no_test=False, fresh=False, timeout=0, name=""
        )
        asyncio.run(cli_server._run_task(args))
        vector = taskrunner_env["vector"]
        assert vector.embed_fn is None
        assert vector.embed_fn_factory is None
        assert "Embedding model changed" in capsys.readouterr().err

    def test_vector_init_is_offloaded_off_the_event_loop(
        self, taskrunner_env, tmp_path, monkeypatch
    ) -> None:
        """``VectorMemoryStore.init()`` must honour its caller contract:
        it is blocking file IO whose Windows DACL writes can block on a
        network volume round-trip, so an async caller offloads it
        via ``asyncio.to_thread`` instead of freezing the loop. Ordering is
        asserted too: init must COMPLETE before ``_run_task`` wires the embed
        hooks (a fire-and-forget offload would reorder them). The sibling
        ``MemoryStore.init()`` (cheap mkdir+seed) carries no contract and is
        deliberately not pinned to either thread here."""
        import threading

        events: list = []
        init_threads: dict = {}

        class _ThreadRecordingVector(_FakeVectorStore):
            def init(self) -> None:
                init_threads["vector"] = threading.current_thread()
                events.append("init")
                super().init()

            @property
            def embed_fn_factory(self):
                return self.__dict__.get("_eff")

            @embed_fn_factory.setter
            def embed_fn_factory(self, value) -> None:
                # First post-init wiring step in _run_task: recording it turns
                # "init completed before first use" into an observed ordering.
                events.append("wired")
                self.__dict__["_eff"] = value

        def _vector(**kw):
            store = _ThreadRecordingVector(**kw)
            taskrunner_env["vector"] = store
            return store

        monkeypatch.setattr(cli_server, "VectorMemoryStore", _vector)
        taskrunner_env["install_runner"](_Result("completed"))
        args = argparse.Namespace(
            spec=str(_spec(tmp_path)), no_test=False, fresh=False, timeout=0, name=""
        )
        asyncio.run(cli_server._run_task(args))

        # Offloaded: init ran in a worker thread, not on the loop thread.
        assert init_threads["vector"] is not threading.current_thread()
        assert taskrunner_env["vector"].inited is True
        # Awaited, not fire-and-forget: init completed BEFORE the embed hooks
        # were wired (the first use that follows it in _run_task).
        assert events.index("init") < events.index("wired")
