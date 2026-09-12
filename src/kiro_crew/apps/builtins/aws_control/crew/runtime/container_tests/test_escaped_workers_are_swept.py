"""A worker that ESCAPED the backend's process group still has to be stopped.

``ProcessGroup.terminate`` sweeps the group, and a kiro-cli worker is not in it: it spawns
with ``start_new_session`` (``acp/runtime.py:1321``), so it ``setsid``s into a group of its own
and no ``killpg`` of the backend's group can reach it. That is deliberate and the drain is the
mechanism -- ``BACKEND_DRAIN_SECS`` is sized so the backend's own SIGTERM handler can reap its
workers, and killing one mid-turn is exactly what the long drain exists to prevent.

What the drain cannot cover is the backend NOT running that handler: it crashed, or it was
still reaping when the drain window elapsed and got SIGKILLed. Then the worker is orphaned, no
one will reap it, and it keeps running and writing to the container filesystem after the task
is meant to be gone -- a leak the teardown exists to close.

The supervisor is PID 1 in this image (``CMD ["python", "-m", "container.supervisor"]``), so
such an orphan is reparented to it. That is the only reason it is findable: the supervisor never
learns the worker's pid, because the backend spawns it and tells nobody.

Real processes here, not mocks. What is under test is whether a process is alive afterwards,
and a mock of ``os.kill`` would only prove the call was made with the argument the test supplied.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest
from container.supervisor.__main__ import (
    _our_live_children,
    _sweep_orphans_the_backend_cannot_reap,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")

# Escapes into its own session, then outlives anything this file waits for.
_ESCAPER = "import os, time; os.setsid(); print(os.getpid(), flush=True); time.sleep(120)"


def _spawn_escaper() -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-c", _ESCAPER],
        stdout=subprocess.PIPE,
    )
    assert proc.stdout is not None
    proc.stdout.readline()  # wait until setsid has happened
    return proc


def _alive(pid: int) -> bool:
    """A zombie counts as gone: it cannot write anymore.

    Existence goes through ``platform_compat.pid_exists`` rather than ``os.kill(pid, 0)``.
    The repo audits for that pattern and the audit is right: on Windows ``os.kill`` with
    signal 0 does not probe, it calls TerminateProcess on the pid being asked about. This is
    host-side test code, not container image source, so the shim is importable here -- the
    suite's own ``pid_alive`` helper takes the same route for the same reason.
    """
    from kiro_crew.platform_compat import pid_exists

    if not pid_exists(pid):
        return False
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def _await_gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.02)
    return False


def test_an_escaped_process_is_found_though_it_left_the_group() -> None:
    """The discovery half: it is in another session, so only parentage can find it."""
    proc = _spawn_escaper()
    try:
        assert os.getpgid(proc.pid) != os.getpgid(0), "the fixture did not escape the group"
        assert proc.pid in _our_live_children(set())
    finally:
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)


def test_the_sweep_kills_a_process_the_backend_could_not_reap() -> None:
    """The whole point: an orphan the backend could not reap must be gone at teardown."""
    proc = _spawn_escaper()
    try:
        _sweep_orphans_the_backend_cannot_reap(set())
        assert _await_gone(proc.pid), "the escaped process outlived the sweep"
    finally:
        try:
            os.kill(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_an_excluded_child_is_left_alone() -> None:
    """Non-vacuity: a pid in the exclude set must survive the sweep.

    The teardown passes the front and backend pids as excluded while it drains them in order,
    so a sweep that killed every child would take a process still being drained. This pins
    that an excluded pid is left running.
    """
    proc = _spawn_escaper()
    try:
        _sweep_orphans_the_backend_cannot_reap({proc.pid})
        assert _alive(proc.pid), "an excluded child was killed"
        assert proc.pid not in _our_live_children({proc.pid})
    finally:
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)


def test_the_sweep_is_a_no_op_with_nothing_to_sweep() -> None:
    """The ordinary shutdown: the backend reaped its own workers, so there is nothing here."""
    _sweep_orphans_the_backend_cannot_reap(set(_our_live_children(set())))


def test_a_zombie_is_not_reported_as_live() -> None:
    """A dead-but-unreaped child cannot write, so it is not something to kill.

    Reported separately because an existence probe succeeds for a zombie: a liveness check
    built on existence alone would list every unreaped child forever and log a kill each
    shutdown.
    """
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and proc.poll() is None:
            time.sleep(0.02)
        assert proc.poll() is not None
        # Not waited on yet, so it is a zombie until this process reaps it.
        assert proc.pid not in _our_live_children(set())
    finally:
        proc.wait(timeout=5)


# Leader setsid()s into its own session, forks a child that STAYS in that session, prints
# both pids, and both sleep. The child is the grandchild the single-pass sweep missed: it is
# not a direct child of the supervisor until the leader dies, so killing only the discovered
# leader leaves it writing. killpg of the leader's own group reaches it in one signal; the
# discovery loop is the belt-and-braces if it reparents before the group signal lands.
_LEADER_WITH_CHILD = (
    "import os, time, sys\n"
    "os.setsid()\n"
    "pid = os.fork()\n"
    "if pid == 0:\n"
    "    time.sleep(120)\n"  # the child (grandchild of PID 1): stays in the leader's group
    "else:\n"
    "    print(os.getpid(), pid, flush=True)\n"
    "    time.sleep(120)\n"
)


def test_the_sweep_takes_the_whole_process_group_not_just_the_leader() -> None:
    """F1: an escaped worker's own child must also be gone at teardown.

    Signalling only the discovered leader pid leaves its child -- a process still writing to
    the container filesystem after the task is meant to be gone -- alive. The sweep signals
    the leader's process GROUP and repeats discovery until nothing of ours is live, so the
    child dies too.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", _LEADER_WITH_CHILD],
        stdout=subprocess.PIPE,
    )
    assert proc.stdout is not None
    leader_pid, child_pid = (int(x) for x in proc.stdout.readline().split())
    try:
        _sweep_orphans_the_backend_cannot_reap(set())
        assert _await_gone(leader_pid), "the escaped leader outlived the sweep"
        assert _await_gone(child_pid), "the leader's child survived: only the leader was killed"
    finally:
        for pid in (child_pid, leader_pid, proc.pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.wait(timeout=5)
