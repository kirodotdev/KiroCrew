"""Restart keeps --headless: it reads the live gateway's argv with the gateway parser."""

import argparse
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew import cli_restart_headless, cli_server, platform_compat, platform_compat_identity
from kiro_crew.config.paths import config_dir
from kiro_crew.gateway_lock import LockProbeError
from kiro_crew.testing.ids import UNALLOCATABLE_PID

_WARNING = (
    "⚠️  Could not read how the running gateway was started, so this restart "
    "starts it with the dashboard. If it was headless, run "
    "kirocrew stop --port 7000 and then kirocrew gateway --headless --port 7000.\n"
)

#: What ``lock_holder`` raises when the lock is held and no pid can be named: a
#: Windows mandatory lock hiding the stamp, or a macOS lock file with a second
#: hard link (``gateway_lock._read_pid`` reads no stamp, and macOS has no
#: ``/proc/locks``).
_HELD_UNNAMED = LockProbeError(
    Path("gateway.lock"), OSError("no live holder pid can be established"), held=True
)
#: What ``lock_holder`` raises when it cannot tell whether the lock is held: an
#: unopenable lock file, or on macOS ``gateway.lock`` deleted under a running
#: gateway (``_anchor_holder_or_nobody``: the home is anchored, nobody named).
_NOT_HELD = {
    "unopenable": LockProbeError(Path("gateway.lock"), PermissionError(13, "denied")),
    "deleted_lock_file": LockProbeError(
        Path("gateway.lock"), OSError("still held by a gateway that gateway.lock no longer names")
    ),
}


@pytest.fixture
def holder(monkeypatch):
    """This home's lock holder (alive, named by the kernel), its run record and its argv."""
    state = SimpleNamespace(
        holder=SimpleNamespace(pid=UNALLOCATABLE_PID, alive=True, source="flock_owner"),
        record=None,
        ours=True,
        lookup=True,
        argv=["kirocrew", "gateway"],
        reads=[],
        probes=[],
    )

    def lock_holder(home):
        state.probes.append(home)
        if isinstance(state.holder, Exception):
            raise state.holder
        return state.holder

    def process_argv(pid):
        state.reads.append(pid)
        return state.argv

    monkeypatch.setattr(cli_restart_headless, "lock_holder", lock_holder)
    monkeypatch.setattr(cli_restart_headless, "_recorded_gateway_pid", lambda port: state.record)
    monkeypatch.setattr(cli_restart_headless, "_is_kirocrew_process", lambda pid: state.ours)
    monkeypatch.setattr(platform_compat, "listening_pid_tool_available", lambda: state.lookup)
    monkeypatch.setattr(platform_compat, "process_argv", process_argv)
    return state


@pytest.fixture
def restart(monkeypatch, tmp_path, holder):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setenv("KIROCREW_BIND", "0.0.0.0")
    monkeypatch.setattr(cli_server, "resolve_client_port", lambda port: port)
    monkeypatch.setattr(cli_server, "_print_running_cron_runs", lambda: None)
    monkeypatch.setattr(cli_server, "_refuse_desktop_supervised", lambda *a, **kw: False)
    # One answer for both readers: ``cli_server`` and ``cli_restart_headless`` bind the
    # same ``port_resolution._is_kirocrew_process``, so they cannot disagree on a pid.
    monkeypatch.setattr(cli_server, "_is_kirocrew_process", lambda pid: holder.ours)
    monkeypatch.setattr(
        cli_server.platform_compat, "find_listening_pids", lambda port: [UNALLOCATABLE_PID]
    )
    monkeypatch.setattr(cli_server, "_wait_for_pids_exit", lambda *a, **kw: [])
    # ``_refuse_if_lock_held`` is the real one: this home (``tmp_path``) has no
    # ``gateway.lock`` holder once the faked stop returns, as after a real stop.
    monkeypatch.setattr(cli_server, "_wait_gateway_ready", lambda *a: (cli_server._READY_OK, None))
    monkeypatch.setattr(cli_server, "read_local_secret", MagicMock(return_value="local-secret"))
    response = MagicMock()
    response.__enter__.return_value.read.return_value = b'{"token":"test-session"}'
    monkeypatch.setattr(
        cli_server, "_owner_verified_secret_urlopen", MagicMock(return_value=response)
    )
    monkeypatch.setattr(cli_server, "resolve_dashboard_host", lambda **kw: "localhost")
    monkeypatch.setattr(
        cli_server.KiroCrewConfig,
        "load",
        lambda: SimpleNamespace(dashboard=SimpleNamespace(url="http://localhost:7000")),
    )
    monkeypatch.setattr(cli_server, "sel", MagicMock())
    monkeypatch.setattr(cli_server, "_own_console_script", lambda: "/fake/kirocrew")
    # ``_stop`` is faked: the real one signals processes and calls the gateway's
    # authenticated shutdown endpoint. That is sound here because the mode is read
    # before ``_stop`` is reached, and these tests assert on exactly that: what the
    # replacement is started with, and that a refusal never reaches the stop.
    stopped = MagicMock()
    monkeypatch.setattr(cli_server, "_stop", stopped)
    spawned = MagicMock(return_value=SimpleNamespace(pid=UNALLOCATABLE_PID + 1))
    monkeypatch.setattr(cli_server.subprocess, "Popen", spawned)
    return stopped, spawned


def replacement_argv(restart):
    stopped, spawned = restart
    cli_server._restart(7000)
    stopped.assert_called_once_with(7000)
    spawned.assert_called_once()
    spawned.call_args.kwargs["stdout"].close()
    return spawned.call_args.args[0]


def assert_warned_and_restarted_ordinary(restart, capsys):
    """The unreadable mode costs one stderr line; the stop and the spawn still run."""
    assert "--headless" not in replacement_argv(restart)
    assert capsys.readouterr().err == _WARNING
    (audit,) = cli_server.sel.return_value.log_api_access.call_args_list
    assert audit.kwargs["outcome"] == "allowed"


_WIN_PY = '"C:\\Program Files\\Python312\\python.exe"'


@pytest.mark.parametrize(
    "argv",
    [
        ["kirocrew", "gateway", "--headless"],
        ["kirocrew", "gateway", "--headl"],
        ["kirocrew", "-v", "gateway", "--no-crons", "--headless"],
        ["/usr/bin/python3", "-P", "-m", "kiro_crew", "gateway", "--port", "7000", "--headless"],
        ["/opt/kc/.venv/bin/python3", "/opt/kc/.venv/bin/kirocrew", "gateway", "--headless"],
        ["/home/a b/.kiro/crew/launcher", "gateway", "--headless"],
        [_WIN_PY, '"C:\\Users\\a b\\kc\\Scripts\\kirocrew.exe"', "gateway", "--headless"],
        # A `gateway` element after the subcommand is a value, not the subcommand.
        ["kirocrew", "gateway", "--seed", "gateway", "--headless"],
        # Another build's arguments, which this parser does not know.
        ["kirocrew", "gateway", "--future-flag", "--headless"],
        ["kirocrew", "gateway", "--headless", "--future-flag"],
        ["kirocrew", "gateway", "--future-option", "value", "--headless"],
        ["kirocrew", "gateway", "--headless", "--future-option", "value"],
    ],
)
def test_a_headless_holder_is_restarted_headless(restart, holder, argv):
    holder.argv = argv
    assert replacement_argv(restart)[-1] == "--headless"


@pytest.mark.parametrize(
    "argv",
    [
        ["kirocrew", "gateway"],
        ["kirocrew", "gateway", "--slack-only"],
        ["kirocrew", "gateway", "--no-crons", "--port", "7000"],
        # The flag's text inside another argument's value is not the flag: the
        # Linux and macOS reads keep argv elements apart.
        ["kirocrew", "gateway", "--seed", "x --headless"],
        ["kirocrew", "gateway", "--seed", "gateway"],
        # After a bare `--` nothing is an option.
        ["kirocrew", "gateway", "--", "--headless"],
        # Another build's arguments, which this parser does not know.
        ["kirocrew", "gateway", "--future-flag"],
        ["kirocrew", "gateway", "--future-flag", "--no-crons"],
        ["kirocrew", "gateway", "--future-option", "value", "--no-crons"],
        ["kirocrew", "gateway", "--no-crons", "--future-option", "value"],
        # A holder that is not a gateway start (the app bridge's short lock).
        ["kirocrew", "app", "uninstall", "notes"],
    ],
)
def test_an_ordinary_holder_is_restarted_ordinary(restart, holder, argv):
    holder.argv = argv
    assert "--headless" not in replacement_argv(restart)


@pytest.mark.parametrize(
    "argv",
    [
        None,  # the read failed
        ["kirocrew", "gateway", "--h"],  # ambiguous: --help or --headless
        ["kirocrew", "gateway", "--he"],
        ["kirocrew", "gateway", "--help"],
    ],
)
def test_an_unreadable_holder_is_restarted_ordinary_with_a_warning(restart, holder, capsys, argv):
    holder.argv = argv
    assert_warned_and_restarted_ordinary(restart, capsys)
    assert holder.reads == [UNALLOCATABLE_PID]  # read once, no retry


@pytest.mark.parametrize(
    "argv",
    [
        # The PowerShell read failed, timed out, or gave a line that does not split
        # (``test_process_argv_splits_the_windows_command_line``).
        None,
        # A quote after ``gateway`` in a Windows split.
        ["kirocrew.exe", "gateway", "--seed", '"a', 'b"', "--headless"],
    ],
)
def test_a_windows_gateway_whose_command_line_is_unreadable_still_restarts(
    restart, holder, monkeypatch, capsys, argv
):
    """Windows: the lock hides the stamp, the run record names the pid, the read fails."""
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    holder.holder = _HELD_UNNAMED
    holder.record = UNALLOCATABLE_PID + 5
    holder.argv = argv
    assert_warned_and_restarted_ordinary(restart, capsys)
    assert holder.reads == [UNALLOCATABLE_PID + 5]


def test_a_readable_ordinary_gateway_restarts_without_a_warning(restart, holder, capsys):
    replacement_argv(restart)
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("windows", [True, False])
def test_a_quote_is_unreadable_only_in_a_windows_split(monkeypatch, windows):
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", windows)
    quoted = cli_restart_headless.argv_headless(["kirocrew", "gateway", "--seed", '"x --headless"'])
    headless = cli_restart_headless.argv_headless(
        ["kirocrew", "gateway", "--seed", 'a"b', "--headless"]
    )
    if windows:
        assert (quoted, headless) == (None, None)
    else:
        assert (quoted, headless) == (False, True)


def test_no_live_holder_restarts_as_before(restart, holder):
    holder.holder = SimpleNamespace(pid=None, alive=False)
    assert "--headless" not in replacement_argv(restart)
    assert holder.reads == []


@pytest.mark.parametrize("headless", [True, False])
def test_a_held_unnamed_lock_reads_the_fresh_run_record(restart, holder, headless):
    """Windows, and a macOS lock file with a second hard link: the record names the gateway."""
    holder.holder = _HELD_UNNAMED
    holder.record = UNALLOCATABLE_PID + 5
    holder.argv = ["kirocrew", "gateway", *(["--headless"] if headless else [])]
    assert ("--headless" in replacement_argv(restart)) is headless
    assert holder.reads == [UNALLOCATABLE_PID + 5]


def test_a_held_unnamed_lock_without_a_fresh_record_restarts_ordinary(restart, holder, capsys):
    """Windows before the gateway has written its run record, or when it never does."""
    holder.holder = _HELD_UNNAMED
    holder.record = None
    assert_warned_and_restarted_ordinary(restart, capsys)
    assert holder.reads == []


@pytest.mark.parametrize("state", sorted(_NOT_HELD))
def test_an_indeterminate_probe_beside_our_listener_restarts_ordinary(
    restart, holder, capsys, state
):
    holder.holder = _NOT_HELD[state]
    holder.ours = True
    assert_warned_and_restarted_ordinary(restart, capsys)
    assert holder.reads == []


def test_an_indeterminate_probe_without_a_listener_lookup_restarts_ordinary(
    restart, holder, capsys
):
    holder.holder = _NOT_HELD["unopenable"]
    holder.lookup = False
    assert_warned_and_restarted_ordinary(restart, capsys)


@pytest.mark.parametrize("state", sorted(_NOT_HELD))
def test_an_indeterminate_probe_with_nothing_of_ours_serving_restarts_as_before(
    restart, holder, state
):
    holder.holder = _NOT_HELD[state]
    holder.ours = False
    assert "--headless" not in replacement_argv(restart)
    assert holder.reads == []


def test_the_mode_is_read_before_the_stop(restart, holder):
    """Once the stop has run the holder is gone; the replacement keeps what was read."""
    holder.argv = ["kirocrew", "gateway", "--headless"]
    restart[0].side_effect = lambda port: setattr(
        holder, "holder", SimpleNamespace(pid=None, alive=False)
    )
    assert replacement_argv(restart)[-1] == "--headless"


@pytest.mark.parametrize("headless", [True, False])
def test_another_homes_listener_on_the_port_does_not_set_the_mode(
    restart, holder, monkeypatch, headless
):
    """`restart --port` naming another home's gateway: the mode is this home's holder's."""
    monkeypatch.setattr(
        cli_server.platform_compat, "find_listening_pids", lambda port: [UNALLOCATABLE_PID + 2]
    )
    holder.argv = ["kirocrew", "gateway", *(["--headless"] if headless else [])]
    assert ("--headless" in replacement_argv(restart)) is headless
    assert holder.probes == [config_dir()]
    assert holder.reads == [UNALLOCATABLE_PID]


def test_the_restart_parser_is_mains_gateway_parser(monkeypatch):
    from kiro_crew import cli

    seen: list[argparse.ArgumentParser] = []
    real = cli.add_gateway_arguments

    def spy(parser):
        seen.append(parser)
        real(parser)

    monkeypatch.setattr(cli, "add_gateway_arguments", spy)
    monkeypatch.setattr(sys, "argv", ["kirocrew", "--version"])
    with pytest.raises(SystemExit):
        cli.main()
    (main_parser,) = seen

    def shape(parser):
        return (
            parser.allow_abbrev,
            parser.prefix_chars,
            [
                (a.option_strings, a.dest, a.nargs, a.const, a.default, a.choices, type(a))
                for a in parser._actions
            ],
        )

    assert shape(cli_restart_headless.gateway_parser()) == shape(main_parser)


@pytest.mark.parametrize("run_fails", [False, True])
def test_the_home_lock_is_held_for_the_whole_gateway_run(monkeypatch, run_fails):
    """The restart's read relies on this: the lock holder is the gateway itself.

    The gateway takes this home's lock before it runs and keeps it until the run
    ends, failed or not, so the process the lock names is the one whose argv
    says how the gateway was started.
    """
    from unittest.mock import patch

    from kiro_crew.cli_logging import _stop_log_queue_listener

    order = []
    lock_cls = MagicMock()
    held = MagicMock()
    held.release.side_effect = lambda: order.append("release")

    def acquire():
        order.append("acquire")
        return held

    def run(*args, **kwargs):
        order.append("run")
        if run_fails:
            raise RuntimeError("gateway run failed")

    lock_cls.return_value.acquire.side_effect = acquire
    monkeypatch.setattr(sys, "argv", ["kirocrew", "gateway"])
    with (
        patch("kiro_crew.cli.GatewayLock", lock_cls),
        patch("kiro_crew.cli_server._gateway"),
        patch("kiro_crew.cli.asyncio.run", side_effect=run),
        patch("kiro_crew.cli.faulthandler.enable"),
    ):
        from kiro_crew.cli import main

        try:
            main()
        except (SystemExit, RuntimeError):
            pass
        finally:
            _stop_log_queue_listener()

    assert order == ["acquire", "run", "release"]


@pytest.mark.skipif(sys.platform != "linux", reason="reads /proc")
def test_process_argv_reads_the_exact_vector_on_linux():
    raw = Path(f"/proc/{os.getpid()}/cmdline").read_bytes().split(b"\0")[:-1]
    assert platform_compat_identity.process_argv(os.getpid()) == [p.decode() for p in raw]
    assert platform_compat_identity.process_argv(UNALLOCATABLE_PID) is None


@pytest.mark.skipif(sys.platform != "linux", reason="reads /proc")
def test_process_argv_of_a_zombie_is_unreadable():
    child = subprocess.Popen([sys.executable, "-c", ""])
    try:
        # Waits for the exit without reaping it, so the child stays a zombie.
        os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
        assert Path(f"/proc/{child.pid}/cmdline").read_bytes() == b""
        assert platform_compat_identity.process_argv(child.pid) is None
    finally:
        child.wait()


@pytest.mark.parametrize("argv", [["/usr/bin/python3", "-m", "kiro_crew", "gateway"], None])
def test_process_argv_reads_the_darwin_vector(monkeypatch, argv):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(platform_compat, "darwin_process_argv", lambda pid: argv)
    assert platform_compat_identity.process_argv(UNALLOCATABLE_PID) == argv


@pytest.mark.parametrize(
    "line,argv",
    [
        ("", None),
        (
            f"{_WIN_PY} -P -m kiro_crew gateway --headless",
            [_WIN_PY, "-P", "-m", "kiro_crew", "gateway", "--headless"],
        ),
        ('kirocrew.exe gateway --seed "x', None),
    ],
)
def test_process_argv_splits_the_windows_command_line(monkeypatch, line, argv):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(platform_compat, "process_command_line", lambda pid: line)
    assert platform_compat_identity.process_argv(UNALLOCATABLE_PID) == argv


@pytest.mark.parametrize("terminal", [True, False])
@pytest.mark.parametrize("headless", [True, False])
def test_restart_access_output(restart, holder, monkeypatch, capsys, terminal, headless):
    holder.argv = ["kirocrew", "gateway", *(["--headless"] if headless else [])]
    monkeypatch.setattr(cli_server.sys.stdout, "isatty", lambda: terminal)
    replacement_argv(restart)
    headline = (
        f"✅ Started detached gateway (pid {UNALLOCATABLE_PID + 1}). Logs: kirocrew logs -f\n"
    )
    output = capsys.readouterr()
    assert output.err == ""
    if headless:
        assert output.out == (
            headline + "👻 Headless: API on http://127.0.0.1:7000/v1\n"
            "   Credential for a Crew API client: kirocrew token --bearer --port 7000\n"
        )
        cli_server.read_local_secret.assert_not_called()
        cli_server._owner_verified_secret_urlopen.assert_not_called()
    elif terminal:
        assert output.out == headline + "\n🔑 http://localhost:7000?token=test-session\n"
    else:
        assert output.out == headline + "\n🔑 Sign in: run kirocrew token for a sign-in link\n"


def test_service_restart_reads_no_argv(restart, holder, monkeypatch, capsys):
    """The unit's own command line starts the replacement, so nothing is read."""
    monkeypatch.setattr(cli_server, "resolve_client_port", lambda port: 7000)
    monkeypatch.setattr(cli_server.sys.stdout, "isatty", lambda: False)
    monkeypatch.setattr(cli_server.service_controller, "restart_service", lambda: True)
    holder.argv = ["kirocrew", "gateway", "--headless"]
    cli_server._restart(None)
    assert holder.reads == []
    restart[0].assert_not_called()
    restart[1].assert_not_called()
    assert capsys.readouterr().out == (
        "✅ Restarted kirocrew service.\n\n🔑 Sign in: run kirocrew token for a sign-in link\n"
    )


@pytest.mark.parametrize("headless", [True, False])
def test_restart_timeout_names_the_served_surface(restart, holder, monkeypatch, capsys, headless):
    holder.argv = ["kirocrew", "gateway", *(["--headless"] if headless else [])]
    monkeypatch.setattr(cli_server, "_resolve_restart_ready_timeout", lambda: 60)
    monkeypatch.setattr(
        cli_server, "_wait_gateway_ready", lambda *a: (cli_server._READY_TIMEOUT, None)
    )
    try:
        with pytest.raises(SystemExit) as exc:
            cli_server._restart(7000)
        assert exc.value.code == 1
    finally:
        restart[1].call_args.kwargs["stdout"].close()
    surface = "the API" if headless else "the dashboard"
    assert capsys.readouterr().out == (
        f"❌ Replacement gateway (pid {UNALLOCATABLE_PID + 1}) did not become ready within "
        "60s. It is still running but not serving port 7000.\n"
        f"   It may be slow to start or wedged during startup; nothing is serving {surface} yet.\n"
        "   To follow its startup, run: kirocrew logs -f\n"
    )


@pytest.mark.asyncio
async def test_dev_fleet_foreground_restart_keeps_headless(restart, holder, monkeypatch):
    from kiro_crew.apps.builtins.dev_fleet.gateway_service import ForegroundBackend

    holder.argv = ["kirocrew", "gateway", "--headless"]
    monkeypatch.setattr(ForegroundBackend, "_usable_launcher", staticmethod(lambda path: path))

    def dispatch(argv):
        assert argv == ["/fake/kirocrew", "restart", "--port", "7000"]
        cli_server._restart(int(argv[-1]))

    backend = ForegroundBackend(
        marker_ports=lambda: [7000],
        read_pid=lambda port: UNALLOCATABLE_PID,
        read_launcher=lambda port: "/fake/kirocrew",
        pid_exists=lambda pid: True,
        spawn=dispatch,
        confinement=lambda: None,
        desktop_supervised=lambda port: False,
    )
    assert await backend.restart_detached() == (True, "")
    stopped, spawned = restart
    stopped.assert_called_once_with(7000)
    spawned.call_args.kwargs["stdout"].close()
    assert spawned.call_args.args[0][-1] == "--headless"


def test_bearer_help_names_the_paths_that_read_it():
    from kiro_crew.cli_parser_gateway import register_token_parser

    parser = argparse.ArgumentParser()
    register_token_parser(parser.add_subparsers())
    token = parser._subparsers._group_actions[0].choices["token"]
    (bearer,) = [a for a in token._actions if "--bearer" in a.option_strings]
    assert "Crew API paths under /v1/" in bearer.help
    assert "POST /v1/chat/completions does not read that header" in bearer.help


# --- The run record decides where the lock probe does not prove the holder -------------------
# Two states in which the lock does not name the gateway with certainty: the probe
# cannot answer, or it names the pid stamped in the lock file. A restart of a headless
# gateway either carries ``--headless`` or prints the warning; it never comes back
# with the dashboard silently. No state here refuses a restart.

_STAMP, _GATEWAY = UNALLOCATABLE_PID, UNALLOCATABLE_PID + 5


def _stamped(holder, monkeypatch, record, argv):
    """The lock names the stamped pid; *argv* maps each pid to its command line."""
    holder.holder = SimpleNamespace(pid=_STAMP, alive=True, source="recorded_pid")
    holder.record = record

    def process_argv(pid):
        holder.reads.append(pid)
        return argv[pid]

    monkeypatch.setattr(platform_compat, "process_argv", process_argv)


def _replacement_dropped_headless(restart) -> bool:
    """Whether the restart stopped the gateway and spawned one without ``--headless``."""
    stopped, spawned = restart
    try:
        cli_server._restart(7000)
    except SystemExit:
        return False
    if not spawned.called:
        return False
    spawned.call_args.kwargs["stdout"].close()
    return stopped.called and "--headless" not in spawned.call_args.args[0]


@pytest.mark.parametrize(
    "windows",
    [
        True,
        # The lock guard after the stop is the real one. Told it is not on Windows,
        # it opens the home directory, which a Windows host refuses.
        pytest.param(
            False,
            marks=pytest.mark.skipif(
                sys.platform == "win32", reason="the real lock guard needs a POSIX host here"
            ),
        ),
    ],
)
@pytest.mark.parametrize("ours", [True, False])
@pytest.mark.parametrize("headless", [True, False])
@pytest.mark.parametrize("state", sorted(_NOT_HELD))
def test_an_indeterminate_probe_reads_the_fresh_run_record(
    restart, holder, monkeypatch, capsys, state, headless, ours, windows
):
    """Whatever the argv classifier says of the listener, the record names the gateway."""
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", windows)
    holder.holder = _NOT_HELD[state]
    holder.ours = ours
    holder.record = _GATEWAY
    holder.argv = ["kirocrew", "-v", "gateway", *(["--headless"] if headless else [])]
    assert ("--headless" in replacement_argv(restart)) is headless
    assert holder.reads == [_GATEWAY]
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("ours", [True, False])
@pytest.mark.parametrize("state", sorted(_NOT_HELD))
def test_an_indeterminate_probe_with_an_unreadable_recorded_gateway_warns(
    restart, holder, capsys, state, ours
):
    holder.holder = _NOT_HELD[state]
    holder.ours = ours
    holder.record = _GATEWAY
    holder.argv = None
    assert_warned_and_restarted_ordinary(restart, capsys)
    assert holder.reads == [_GATEWAY]


@pytest.mark.parametrize("headless", [True, False])
@pytest.mark.parametrize(
    "stamp_argv",
    [
        ["/usr/bin/sleep", "3600"],
        # Another home's gateway now carries the stamped number.
        ["kirocrew", "gateway"],
        ["kirocrew", "gateway", "--headless"],
        None,
    ],
)
def test_a_fresh_run_record_outranks_the_lock_stamp(
    restart, holder, monkeypatch, capsys, stamp_argv, headless
):
    gateway_argv = ["kirocrew", "gateway", *(["--headless"] if headless else [])]
    _stamped(holder, monkeypatch, _GATEWAY, {_STAMP: stamp_argv, _GATEWAY: gateway_argv})
    assert ("--headless" in replacement_argv(restart)) is headless
    assert holder.reads == [_GATEWAY]
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("headless", [True, False])
@pytest.mark.parametrize("record", [None, _STAMP])
def test_a_stamped_gateway_is_read_with_or_without_its_record(
    restart, holder, monkeypatch, capsys, record, headless
):
    """macOS, btrfs and overlayfs in the ordinary case: the stamp names the gateway."""
    argv = ["kirocrew", "gateway", *(["--headless"] if headless else [])]
    _stamped(holder, monkeypatch, record, {_STAMP: argv})
    assert ("--headless" in replacement_argv(restart)) is headless
    assert holder.reads == [_STAMP]
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "argv", [["/usr/bin/sleep", "3600"], ["kirocrew", "app", "uninstall", "notes"], None]
)
def test_a_stamp_that_names_no_gateway_and_no_record_warns(
    restart, holder, monkeypatch, capsys, argv
):
    """Nothing shows the stamped process is the gateway, so the mode is unknown."""
    _stamped(holder, monkeypatch, None, {_STAMP: argv})
    assert_warned_and_restarted_ordinary(restart, capsys)
    assert holder.reads == [_STAMP]


def test_a_holder_the_kernel_names_outranks_the_run_record(restart, holder, capsys):
    """Linux ext4 and xfs: ``/proc/locks`` proves the holder, so nothing else is read."""
    holder.record = _GATEWAY
    holder.argv = ["kirocrew", "app", "uninstall", "notes"]
    assert "--headless" not in replacement_argv(restart)
    assert holder.reads == [UNALLOCATABLE_PID]
    assert capsys.readouterr().err == ""


def test_pinned_an_unrecognised_headless_listener_behind_an_unresolved_lock(restart, holder):
    """``gateway.lock`` deleted under ``kirocrew -v gateway --headless``.

    The probe cannot tell whether the lock is held, the argv classifier does not
    recognise the listener, and the fresh run record still names the gateway that
    the stop then ends through the authenticated endpoint.
    """
    holder.holder = _NOT_HELD["deleted_lock_file"]
    holder.ours = False
    holder.record = UNALLOCATABLE_PID
    holder.argv = ["kirocrew", "-v", "gateway", "--headless"]
    assert not _replacement_dropped_headless(restart)


def test_pinned_a_stale_lock_stamp_naming_another_live_process(restart, holder, monkeypatch):
    """A stamp trusted again after an extra hard link is removed names an unrelated pid.

    The fresh run record names the real, headless gateway; the lock names another
    live process whose argv has no ``gateway`` element.
    """
    argv = {_STAMP: ["/usr/bin/sleep", "3600"], _GATEWAY: ["kirocrew", "gateway", "--headless"]}
    _stamped(holder, monkeypatch, _GATEWAY, argv)
    assert not _replacement_dropped_headless(restart)
    restart[0].assert_called_once_with(7000)  # carried, not refused
