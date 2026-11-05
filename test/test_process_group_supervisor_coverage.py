"""``_process_group_supervisor.py``: the paths its subprocess-driven suites leave unmeasured.

Those suites run the supervisor as ``python -I -c <source>``, so ``main()`` executes where this
process's coverage cannot see it. Here every OS seam the code reaches (``fork``, ``waitpid``,
``execve``, ``signal.signal``, ``/proc``, the clock) is replaced on the module under test: nothing
forks, signals or sleeps, no limit of the pytest worker changes, and each test runs on Windows too.
"""

from __future__ import annotations

import errno
import os
import signal
import sys
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

from kiro_crew import _process_group_supervisor as supervisor
from kiro_crew.testing.clock import ManualClock

_COMMAND = os.path.realpath(sys.executable)  # absolute on every platform
_ERR = OSError(errno.EMFILE, "no fds")
# The wait(2) layout: signal in the low 7 bits, exit status in bits 8-15, 0x7F when stopped.
# Supplied here because Windows has no such macros.
_STATUS = {
    "WIFEXITED": lambda s: s & 0x7F == 0,
    "WEXITSTATUS": lambda s: (s >> 8) & 0xFF,
    "WIFSIGNALED": lambda s: 0 < s & 0x7F < 0x7F,
    "WTERMSIG": lambda s: s & 0x7F,
}


def _fake(monkeypatch: pytest.MonkeyPatch, name: str, **fakes: Any) -> None:
    """Give the supervisor its own copy of module ``name``, with these attributes replaced."""
    real = getattr(supervisor, name)
    monkeypatch.setattr(supervisor, name, SimpleNamespace(**{**vars(real), **fakes}))


def _use(monkeypatch: pytest.MonkeyPatch, host: mock.Mock, names: str) -> None:
    """Make the mocks of ``host`` the supervisor's ``os`` functions of those names."""
    _fake(monkeypatch, "os", **{n: getattr(host, n) for n in names.split()})


def _raise_or(outcome: Any) -> Any:
    if isinstance(outcome, Exception):
        raise outcome
    return outcome


def _proc(stats: dict[str, Any] | None = None) -> Any:
    """A ``Path`` for the supervisor: ``/proc`` exists, and each stat file reads as told."""
    return lambda text: SimpleNamespace(
        is_dir=lambda: True, read_bytes=lambda: _raise_or((stats or {})[text])
    )


def test_rlimits_are_clamped_to_the_hard_limit_set_soft_and_hard_and_skip_what_fails(monkeypatch):
    res = SimpleNamespace(RLIM_INFINITY=-1, RLIMIT_NOFILE=7, RLIMIT_AS=9, RLIMIT_CPU=0)
    res.getrlimit = lambda which: (0, {7: 100, 9: -1, 0: 5}[which])  # NOFILE, AS (unlimited), CPU
    res.setrlimit = mock.Mock(side_effect=[None, None, OSError(errno.EPERM, "denied"), None])
    monkeypatch.setattr(supervisor, "_resource", res)
    supervisor._apply_rlimits("RLIMIT_NOFILE:500,RLIMIT_AS:4096,RLIMIT_CPU:1,RLIMIT_NOFILE:50")
    expected = [(7, (100, 100)), (9, (4096, 4096)), (0, (1, 1)), (7, (50, 50))]
    assert [c.args for c in res.setrlimit.call_args_list] == expected


_OOM = [mock.call.open("/proc/self/oom_score_adj", os.O_WRONLY), mock.call.write(99, b"1000")]


@pytest.mark.parametrize(
    ("platform", "open_err", "write_err", "expected"),
    [
        ("darwin", None, None, []),
        ("linux", None, None, [*_OOM, mock.call.close(99)]),
        ("linux", None, _ERR, [*_OOM, mock.call.close(99)]),  # a failed write still closes
        ("linux", _ERR, None, _OOM[:1]),
    ],
)
def test_oom_bias_is_written_on_linux_only_and_never_raises(
    monkeypatch, platform, open_err, write_err, expected
):
    host = mock.Mock()
    host.open.return_value = 99
    host.open.side_effect, host.write.side_effect = open_err, write_err
    _fake(monkeypatch, "sys", platform=platform)
    _use(monkeypatch, host, "open write close")
    supervisor._bias_oom_score()
    assert host.mock_calls == expected


def test_ps_output_parsing_skips_malformed_rows_zombies_and_ps_itself() -> None:
    rows = ["100 42 S", "no", "1 2", "x 42 S", "101 y S", "102 42 Z+", "999 42 R", "103 7 S"]
    output = "\n".join([*rows, "104 42 SN"])
    assert supervisor._parse_ps_group_members(output, 42, 999) == {100, 104}


def _ps_host(fork: int) -> mock.Mock:
    host = mock.Mock()
    host.pipe.return_value = (10, 11)
    host.fork.return_value = fork
    return host


def test_ps_listing_reads_the_pipe_to_the_end_and_reaps_ps(monkeypatch) -> None:
    host = _ps_host(4321)
    host.read.side_effect = [b"100 42 S\n101 42 Z\n43", b"21 42 S\n102 42 R\n", b""]
    _use(monkeypatch, host, "pipe fork close read waitpid")
    assert supervisor._ps_group_members(42) == {100, 102}  # not the zombie 101, nor ps itself
    assert [c[0] for c in host.mock_calls] == "pipe fork close read read read close waitpid".split()
    assert host.read.call_args_list == [mock.call(10, 64 * 1024)] * 3
    assert host.close.call_args_list == [mock.call(11), mock.call(10)]
    host.waitpid.assert_called_once_with(4321, 0)


def test_the_ps_child_wires_stdout_to_the_pipe_and_exits_127_when_exec_fails(monkeypatch):
    host = _ps_host(0)
    host.execv.side_effect, host._exit.side_effect = OSError(errno.ENOENT, "no ps"), SystemExit
    _use(monkeypatch, host, "pipe fork close dup2 execv _exit")
    with pytest.raises(SystemExit):
        supervisor._ps_group_members(42)
    assert [c[0] for c in host.mock_calls] == "pipe fork close dup2 close execv _exit".split()
    assert host.close.call_args_list == [mock.call(10), mock.call(11)]
    host.dup2.assert_called_once_with(11, 1)
    host.execv.assert_called_once_with("/bin/ps", ["ps", "-axo", "pid=,pgid=,state="])
    host._exit.assert_called_once_with(127)


def test_pidfd_helpers_use_the_interpreter_else_libc_and_can_reap_needs_a_pidfd(monkeypatch):
    sent: list[tuple[int, int]] = []
    libc = mock.Mock()
    libc.syscall.side_effect = [66, 0, -1, -1]
    monkeypatch.setattr(supervisor, "_libc", libc)
    _fake(monkeypatch, "ctypes", get_errno=lambda: errno.ESRCH)
    _fake(monkeypatch, "os", pidfd_open=lambda pid: 55)
    _fake(monkeypatch, "signal", pidfd_send_signal=lambda fd, sig: sent.append((fd, sig)))
    assert supervisor._pidfd_open(123) == 55
    assert supervisor._pidfd_send_signal(55, 9) is None
    assert (sent, libc.syscall.called) == ([(55, 9)], False)

    _fake(monkeypatch, "os", pidfd_open=None)
    _fake(monkeypatch, "signal", pidfd_send_signal=None)
    assert supervisor._pidfd_open(123) == 66
    supervisor._pidfd_send_signal(66, 15)
    with pytest.raises(OSError) as raised:
        supervisor._pidfd_open(1)
    assert raised.value.errno == errno.ESRCH
    # 434 and 424 are pidfd_open(2) and pidfd_send_signal(2) on every Linux architecture.
    syscalls = [mock.call(434, 123, 0), mock.call(424, 66, 15, None, 0), mock.call(434, 1, 0)]
    assert libc.syscall.call_args_list == syscalls

    monkeypatch.setattr(supervisor, "Path", _proc())
    for platform in ("linux", "darwin"):  # the kernel refuses a pidfd; there is no pidfd at all
        _fake(monkeypatch, "sys", platform=platform)
        assert supervisor.can_reap() is False
    assert libc.syscall.call_count == 4  # darwin never asked


@pytest.mark.parametrize(
    ("open_error", "stat", "send_error", "attempted", "delivered"),
    [
        (OSError(errno.ESRCH, "gone"), b"", None, False, False),  # exited before the pin
        (None, OSError(errno.ENOENT, "gone"), None, False, False),  # exited after it
        (None, b"77 (x) S 1 999 0", None, False, False),  # a stranger now holds the pid
        (None, b"77 (x) S 1 42 0", OSError(errno.ESRCH, "gone"), True, False),
        (None, b"77 (x) S 1 42 0", None, True, True),
    ],
)
def test_signal_member_signals_only_a_pinned_process_still_in_the_group(
    monkeypatch, open_error, stat, send_error, attempted, delivered
):
    closed: list[int] = []
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(supervisor, "_pidfd_open", lambda pid: _raise_or(open_error) or 31)
    monkeypatch.setattr(
        supervisor, "_pidfd_send_signal", lambda *a: sent.append(a) or _raise_or(send_error)
    )
    monkeypatch.setattr(supervisor, "Path", _proc({"/proc/77/stat": stat}))
    _fake(monkeypatch, "os", close=closed.append)
    assert supervisor._signal_member(77, 42, 15) is delivered
    assert sent == ([(31, 15)] if attempted else [])
    assert closed == ([] if open_error else [31])  # a pinned process is always released


def test_signal_members_signals_each_member_but_this_process_and_returns_them(monkeypatch) -> None:
    sent: list[tuple[int, int, int]] = []
    monkeypatch.setattr(supervisor, "_group_members", lambda pgid: {10, 11, 12})
    monkeypatch.setattr(supervisor, "_signal_member", lambda *args: sent.append(args) or True)
    assert supervisor._signal_members(42, 10, 15) == {11, 12}
    assert sorted(sent) == [(11, 42, 15), (12, 42, 15)]


@pytest.mark.parametrize(
    ("term", "poll", "kill", "signals", "polls"),
    [
        (set(), {10}, set(), [15], 0),  # nothing was in the group
        (_ERR, {10}, set(), [15], 0),  # the listing itself failed
        ({5}, {10}, set(), [15], 1),  # everything left within the first poll
        ({5}, {10, 5}, {5}, [15, 9], 20),  # still there when the grace ends
        ({5}, _ERR, _ERR, [15, 9], 20),  # unreadable membership never counts as empty
    ],
)
def test_terminate_survivors_asks_with_sigterm_then_forces_after_the_grace(
    monkeypatch, term, poll, kill, signals, polls
):
    clock = ManualClock(monotonic_start=0.0)
    clock.install(monkeypatch, supervisor)
    _fake(monkeypatch, "signal", SIGTERM=15, SIGKILL=9)
    sent: list[int] = []
    asked: list[int] = []
    outcome = {15: term, 9: kill}
    monkeypatch.setattr(
        supervisor,
        "_signal_members",
        lambda g, own, sig: sent.append(sig) or _raise_or(outcome[sig]),
    )
    monkeypatch.setattr(supervisor, "_group_members", lambda g: asked.append(g) or _raise_or(poll))
    supervisor._terminate_survivors(42, 10)
    assert (sent, asked) == (signals, [42] * polls)
    # Each poll that found survivors waited one interval; 20 of them make the 1 s grace.
    assert clock.sleeps == (supervisor._POLL_SECONDS,) * (polls if signals == [15, 9] else 0)


@pytest.mark.parametrize(
    ("status", "code"), [(0, 0), (3 << 8, 3), (9, 137), (15, 143), ((19 << 8) | 0x7F, 1)]
)
def test_exit_code_is_the_status_or_128_plus_the_signal_or_1_if_stopped(monkeypatch, status, code):
    _fake(monkeypatch, "os", **_STATUS)
    assert supervisor._exit_code(status) == code


_SEAMS = "_bias_oom_score _apply_rlimits can_reap _terminate_survivors _group_members"


def _run_main(monkeypatch, argv, *, fork=4321, pgid=900, pinnable=True, members=(set(),)):
    """Run ``main()`` over scripted syscalls; return the recorder, the clock and the SystemExit."""
    host = mock.Mock()
    host.fork.return_value = fork
    host.waitpid.return_value = (fork, 3 << 8)  # the command exited with status 3
    host.getpid.return_value, host.getpgrp.return_value = 900, pgid
    host.can_reap.return_value = pinnable
    host._group_members.side_effect = list(members)
    host.execve.side_effect, host._exit.side_effect = OSError(errno.ENOEXEC, "bad"), SystemExit
    _use(monkeypatch, host, "fork waitpid getpid getpgrp execve _exit")
    _fake(monkeypatch, "os", **_STATUS)
    _fake(monkeypatch, "signal", signal=host.signal)
    _fake(monkeypatch, "sys", argv=["supervisor", *argv])
    for name in _SEAMS.split():
        monkeypatch.setattr(supervisor, name, getattr(host, name))
    clock = ManualClock(monotonic_start=0.0)
    clock.install(monkeypatch, supervisor)
    with pytest.raises(SystemExit) as raised:
        supervisor.main()
    return host, clock, raised.value


def test_main_waits_for_the_whole_group_then_exits_with_the_commands_status(monkeypatch) -> None:
    members = ({900, 901}, _ERR, {900})  # a listing error keeps the group anchored
    host, clock, done = _run_main(monkeypatch, [_COMMAND, "-c", "pass"], members=members)
    assert done.code == 3
    host.waitpid.assert_called_once_with(4321, 0)
    assert host._group_members.call_args_list == [mock.call(900)] * 3
    assert clock.sleeps == (supervisor._POLL_SECONDS,) * 2
    signum, handler = host.signal.call_args.args  # TERM is ignored so the leader outlives the group
    assert (host.signal.call_count, signum, handler(signum, None)) == (1, signal.SIGTERM, None)


_LIMITS = ["--rlimits=NOFILE:64", "--rlimits=AS:1"]


@pytest.mark.parametrize(
    ("flags", "pgid", "pinnable", "reaps"),
    [
        ([*_LIMITS, "--reap-survivors"], 900, True, True),
        (["--reap-survivors"], 1, True, False),  # does not lead its own group
        (["--reap-survivors"], 900, False, False),  # no pidfd to pin a member with
        ([], 900, True, False),  # not asked to
    ],
)
def test_main_applies_leading_flags_before_it_forks_and_reaps_only_where_it_can(
    monkeypatch, flags, pgid, pinnable, reaps
):
    specs = [f.split("=", 1)[1] for f in flags if f.startswith("--rlimits=")]
    host, _, done = _run_main(monkeypatch, [*flags, _COMMAND], pgid=pgid, pinnable=pinnable)
    assert done.code == 3
    first = [c[0] for c in host.mock_calls][: len(specs) + 3]
    assert first == ["_bias_oom_score", *["_apply_rlimits"] * len(specs), "signal", "fork"]
    assert host._apply_rlimits.call_args_list == [mock.call(s) for s in specs]
    assert host._terminate_survivors.call_args_list == ([mock.call(900, 900)] if reaps else [])


@pytest.mark.parametrize(
    "argv",
    [[], ["--no-such-flag", _COMMAND], ["--reap-survivors"], ["relative/command"]],
    ids=["no-command", "unknown-flag", "flag-only", "relative-command"],
)
def test_main_refuses_a_bad_command_line_with_127_before_it_forks(monkeypatch, argv):
    host, _, done = _run_main(monkeypatch, argv)
    assert done.code == 127
    host._bias_oom_score.assert_called_once_with()  # the OOM bias comes first, whatever the flags
    host.fork.assert_not_called()
    host.signal.assert_not_called()


def test_main_child_restores_sigterm_and_execs_the_command_or_exits_127(monkeypatch) -> None:
    host, _, _ = _run_main(monkeypatch, [_COMMAND, "-c", "pass"], fork=0)
    assert host.signal.call_args_list[1] == mock.call(signal.SIGTERM, signal.SIG_DFL)
    host.execve.assert_called_once_with(_COMMAND, [_COMMAND, "-c", "pass"], os.environ)
    host._exit.assert_called_once_with(127)
    host.waitpid.assert_not_called()
