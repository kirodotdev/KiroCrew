"""Launching and tearing down a child as a process *group*.

The container runs a small tree of long-lived children. One of them, the
Kiro Crew backend, itself launches ``kiro-cli`` workers, and each worker is a
two-process tree: a launcher and the child that actually runs the turn.
Signalling the launcher's pid alone orphans that child, which goes on to finish
its turn against a data home the container is trying to shut down.

So every child here is started in its own session with ``start_new_session``,
which makes the child a session and group leader whose process-group id equals
its pid. Grandchildren inherit that group, and a single ``killpg`` reaches the
whole tree. Teardown *drains*: it asks the group to stop with SIGTERM, waits for
the group leader to exit, and only escalates to SIGKILL if the drain window
elapses.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from typing import IO


class ProcessGroup:
    """A launched child and every process in its group.

    ``leader`` is the ``Popen`` for the group-leader process. Because the child
    was started with ``start_new_session=True`` its pid is also its process
    group id, so ``os.killpg(leader.pid, ...)`` signals the whole tree.
    """

    def __init__(self, name: str, leader: subprocess.Popen) -> None:
        self.name = name
        self.leader = leader
        # With start_new_session=True the child is a group leader: pgid == pid.
        # Capture the pid rather than calling os.getpgid(), which can race the
        # child's setsid() and briefly return the parent's group.
        self.pgid = leader.pid

    @property
    def pid(self) -> int:
        return self.leader.pid

    def poll(self) -> int | None:
        """Return the leader's exit code, or None while it is still running."""
        return self.leader.poll()

    def returncode(self) -> int | None:
        return self.leader.returncode

    def _signal_group(self, sig: int) -> bool:
        """Send ``sig`` to the whole group. Return False if the group is gone."""
        try:
            os.killpg(self.pgid, sig)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # The group outlived our permission to signal it (should not happen
            # for our own children); fall back to the leader pid.
            try:
                self.leader.send_signal(sig)
                return True
            except ProcessLookupError:
                return False

    def terminate(self, drain_timeout: float, poll_interval: float = 0.05) -> int | None:
        """Stop the whole group, draining before it is killed.

        Sends SIGTERM to the group, waits up to ``drain_timeout`` for the leader
        to exit, and escalates to SIGKILL only if the drain window elapses. The
        leader is always reaped, so no zombie is left behind. Returns the
        leader's exit code (negative if it was signalled).
        """
        if self.leader.poll() is not None:
            return self.leader.returncode

        alive = self._signal_group(signal.SIGTERM)
        if not alive:
            # Group already gone; still reap the leader handle.
            return self._reap()

        deadline = time.monotonic() + max(0.0, drain_timeout)
        while time.monotonic() < deadline:
            if self.leader.poll() is not None:
                # Leader drained; sweep the group in case a grandchild lingers.
                self._signal_group(signal.SIGKILL)
                return self.leader.returncode
            time.sleep(poll_interval)

        # Drain window elapsed: kill the whole group hard.
        self._signal_group(signal.SIGKILL)
        return self._reap()

    def _reap(self) -> int | None:
        try:
            return self.leader.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # Extremely unlikely after SIGKILL; report unknown rather than hang.
            return self.leader.poll()


def spawn_process_group(
    name: str,
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
    stdout: IO | int | None = None,
    stderr: IO | int | None = None,
) -> ProcessGroup:
    """Launch ``argv`` as a new session/group leader and return its handle.

    ``start_new_session=True`` is the load-bearing argument: it detaches the
    child into its own process group so the whole tree, including any
    grandchild workers, can be drained together.
    """
    leader = subprocess.Popen(
        list(argv),
        env=dict(env) if env is not None else None,
        cwd=cwd,
        stdout=stdout,
        stderr=stderr,
        start_new_session=True,
    )
    return ProcessGroup(name, leader)
