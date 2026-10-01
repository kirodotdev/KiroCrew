"""Helpers for tests that assert on a spawn's argv, or on a real child's death.

Resource limits are applied AFTER ``exec`` by ``kiro_crew._spawn_exec_shim``, so
every spawn routed through ``sandbox.create_subprocess_limited`` carries an argv
prefix (``<python> -I -S -c <shim source> [options] --``) ahead of the real
command. Tests that care about the command itself use :func:`strip_spawn_shim`
rather than hard-coding the prefix, which varies with the resource profile.
"""

from __future__ import annotations

import contextlib
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from kiro_crew.platform_compat import SIGKILL, kill_pid, pid_exists, pid_is_zombie

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
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        if pidfile.exists():
            return tuple(int(part) for part in pidfile.read_text(encoding="utf-8").split())
        time.sleep(0.02)
    raise AssertionError(f"the build child never wrote {pidfile}")


def await_gone(pid: int, budget: float = 10.0) -> bool:
    """True once *pid* is not running (gone, or a zombie its reaper has not reached)."""
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        if not pid_exists(pid) or pid_is_zombie(pid):
            return True
        time.sleep(0.02)
    return False


def kill_leftovers(pids: Sequence[int]) -> None:
    """Best-effort cleanup for a test that failed before its own kill ran."""
    for pid in pids:
        with contextlib.suppress(OSError, ValueError):
            if pid_exists(pid) and not pid_is_zombie(pid):
                kill_pid(pid, SIGKILL)
