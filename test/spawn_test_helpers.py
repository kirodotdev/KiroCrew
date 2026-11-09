"""Helpers for tests that assert on a spawn's argv, or on a real child's death or interrupt.

Resource limits are applied AFTER ``exec`` by ``kiro_crew._spawn_exec_shim``, so
every spawn routed through ``sandbox.create_subprocess_limited`` carries an argv
prefix (``<python> -I -S -c <shim source> [options] --``) ahead of the real
command. Tests that care about the command itself use :func:`strip_spawn_shim`
rather than hard-coding the prefix, which varies with the resource profile.
"""

from __future__ import annotations

import contextlib
import signal
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path

from kiro_crew.platform_compat import SIGKILL, kill_pid, pid_exists, pid_is_zombie
from kiro_crew.testing.wait import wait_until

_SHIM_FLAGS = ("-I", "-S", "-c")
_ARGV_SEPARATOR = "--"


def strip_spawn_shim(args: Sequence[str]) -> tuple[str, ...]:
    """Return *args* without the post-exec shim prefix, if one is present.

    Returns *args* unchanged when no shim was prepended -- on Windows, for a
    policy-free profile that also asks for no controlling terminal, or when the
    spawn was not routed through the wrapper -- so a test can use this
    unconditionally.

    The scan for the ``--`` terminator starts after the shim's source argument,
    so a ``--`` inside the command itself is never mistaken for the separator.
    """
    argv = tuple(args)
    if len(argv) < 6 or argv[0] != sys.executable or argv[1:4] != _SHIM_FLAGS:
        return argv
    index = 4  # argv[4] is the shim source string
    while index < len(argv) and argv[index] != _ARGV_SEPARATOR:
        index += 1
    return argv[index + 1 :]


#: A build-child stand-in for the update engine's kill tests. It starts a
#: grandchild in the child's own process group, so killing the child alone
#: leaves the grandchild running and only a GROUP kill reaches both. It writes
#: ``"<own pid> <grandchild pid>"`` to ``argv[1]`` (atomically), then sleeps far
#: past any test budget. A test that does not kill both fails :func:`await_gone`.
LONG_CHILD_WITH_GRANDCHILD = (
    "import os, subprocess, sys, time\n"
    "kid = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
    "tmp = sys.argv[1] + '.tmp'\n"
    "open(tmp, 'w').write(f'{os.getpid()} {kid.pid}')\n"
    "os.replace(tmp, sys.argv[1])\n"
    "time.sleep(120)\n"
)


def await_pids(pidfile: Path, budget: float = 30.0) -> tuple[int, ...]:
    """The pids :data:`LONG_CHILD_WITH_GRANDCHILD` wrote, once it wrote them."""
    wait_until(
        pidfile.exists,
        timeout=budget,
        describe=lambda: f"the build child never wrote {pidfile}",
    )
    return tuple(int(part) for part in pidfile.read_text(encoding="utf-8").split())


def await_gone(pid: int, budget: float = 10.0) -> bool:
    """True once *pid* is not running (gone, or a zombie its reaper has not reached).

    Returns ``False`` at *budget* rather than raising: a caller asserts on the
    result, and the kill tests name the pid that stayed in their own message.
    """
    try:
        wait_until(lambda: not pid_exists(pid) or pid_is_zombie(pid), timeout=budget)
    except AssertionError:
        return False
    return True


def kill_leftovers(pids: Sequence[int]) -> None:
    """Best-effort cleanup for a test that failed before its own kill ran."""
    for pid in pids:
        with contextlib.suppress(OSError, ValueError):
            if pid_exists(pid) and not pid_is_zombie(pid):
                kill_pid(pid, SIGKILL)


#: The signals a test interrupts its child with; a terminal hands both over at default.
_INTERRUPT_SIGNALS = ("SIGINT", "SIGTERM")


@contextlib.contextmanager
def unblocked_signals() -> Iterator[None]:
    """Spawn inside this with no signal blocked or ignored, as a terminal starts a command.

    A child inherits the spawning thread's signal mask and keeps it across
    exec, so a signal blocked in this thread never reaches a child the test
    means to interrupt (POSIX only). An ignored SIGINT or SIGTERM survives exec
    the same way (a handler does not), and a child that keeps an inherited
    ignore on purpose -- push_guard.py does, as ``nohup`` expects -- then never
    takes the interrupt at all. Both are put back right after the spawn.
    """
    ignored = []
    for name in _INTERRUPT_SIGNALS:
        number = getattr(signal, name)
        if signal.getsignal(number) is signal.SIG_IGN:
            ignored.append(number)
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, [])
    try:
        # shortcut: the worker takes these at default for the length of the spawn
        # (they were ignored, so one arriving now would end it); preexec_fn would
        # avoid that window but is unsafe in a threaded worker.
        for number in ignored:
            signal.signal(number, signal.SIG_DFL)
        signal.pthread_sigmask(signal.SIG_SETMASK, [])
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)
        for number in ignored:
            signal.signal(number, signal.SIG_IGN)


#: The /proc/<pid>/status lines that say why a process has not taken a signal.
_STATUS_KEYS = ("Name", "State", "PPid", "SigQ", "SigPnd", "ShdPnd", "SigBlk", "SigIgn", "SigCgt")


def _proc_text(pid: int, leaf: str) -> str:
    try:
        return Path(f"/proc/{pid}/{leaf}").read_bytes().decode("utf-8", "replace")
    except OSError as exc:
        return f"<unreadable: {exc.strerror or exc}>"


def process_group_report(pgid: int) -> str:
    """Describe every live process in group *pgid* from /proc, for a failure message.

    Per member: its command line, the status lines that show its state and its
    blocked, ignored, caught and pending signals, its wchan and its kernel stack
    (often unreadable without privilege; the report says so). Linux only;
    elsewhere it says /proc is absent. Read it before the test's own SIGKILL.
    """
    proc_root = Path("/proc")
    if not (proc_root / "self" / "status").exists():
        return f"process group {pgid}: no /proc on this platform"
    lines = [f"process group {pgid}:"]
    for entry in sorted(proc_root.iterdir(), key=lambda p: p.name):
        if not entry.name.isdigit():
            continue
        stat = _proc_text(int(entry.name), "stat")
        tail = stat[stat.rfind(")") + 2 :].split() if ")" in stat else []
        if len(tail) < 3 or tail[2] != str(pgid):
            continue
        pid = int(entry.name)
        cmdline = _proc_text(pid, "cmdline").replace("\0", " ").strip()
        lines.append(f"  pid {pid}: {cmdline[:200]}")
        for row in _proc_text(pid, "status").splitlines():
            key = row.split(":", 1)[0]
            if key in _STATUS_KEYS:
                lines.append(f"    {row}")
        lines.append(f"    wchan: {_proc_text(pid, 'wchan').strip()}")
        stack = _proc_text(pid, "stack").strip().splitlines()
        lines.extend(f"    stack: {frame}" for frame in stack[:12])
    if len(lines) == 1:
        lines.append("  no live member")
    return "\n".join(lines)
