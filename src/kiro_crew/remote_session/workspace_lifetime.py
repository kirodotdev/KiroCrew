"""Guest-side: keep a connected computer's commands within the tool server's lifetime.

macOS and Windows have no PID namespace to end what a command leaves running, so
``workspace_tools.py --end-commands-with-server`` keeps every command with the
server: on POSIX each command runs in a process group of its own, which the
server tracks and ends on a timeout and when it stops (:class:`CommandGroups`),
and on Windows every command is in a job that ends every member when the server
exits (:func:`end_children_on_exit`). On macOS a process that moves to a session
of its own (``setsid``) leaves its tracked group and outlives the server; that
weaker guarantee is documented in providers.md.

Loaded beside ``workspace_tools.py`` from its own folder; stdlib-only and Python
3.9 compatible, like it.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Dict, Iterator, List

#: Seconds between checks that the process which started this server still runs.
_PARENT_POLL_SECS = 1.0
#: Seconds a command's process group gets to exit on SIGTERM before SIGKILL; well
#: inside the connector's own grace, so its stop finds this server done in time.
GROUP_GRACE_SECS = 1.0
_GROUP_POLL_SECS = 0.05
#: How long reaping a signalled command's leader may take.
_REAP_SECS = 5.0
#: Windows job limit: end every process in the job when its last handle closes.
_JOB_KILL_ON_CLOSE = 0x2000
#: Windows ``JobObjectExtendedLimitInformation``.
_JOB_EXTENDED_LIMITS = 9


def end_children_on_exit() -> bool:
    """Windows: put this process in a job that ends every member when it exits.

    Commands, and whatever they start, join the job when they start, and stay in
    it after their own parent exits, so none outlives this server. Only this
    process holds the job's handle, which the system closes however it exits.
    False when the job cannot be made; nothing is changed then.
    """
    try:
        import ctypes
        from ctypes import wintypes

        class _Basic(ctypes.Structure):
            _fields_ = [
                ("user_time", ctypes.c_int64),
                ("job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD),
                ("min_set", ctypes.c_size_t),
                ("max_set", ctypes.c_size_t),
                ("processes", wintypes.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class _Extended(ctypes.Structure):
            _fields_ = [
                ("basic", _Basic),
                ("io", ctypes.c_uint64 * 6),
                ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t),
                ("peak_process", ctypes.c_size_t),
                ("peak_job", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        # Unnamed and not inheritable: nothing else can open or hold it open.
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return False
        info = _Extended()
        info.basic.flags = _JOB_KILL_ON_CLOSE
        if not kernel32.SetInformationJobObject(
            job, _JOB_EXTENDED_LIMITS, ctypes.byref(info), ctypes.sizeof(info)
        ) or not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
            kernel32.CloseHandle(wintypes.HANDLE(job))
            return False
    except (AttributeError, ImportError, OSError, ValueError):
        return False
    # Never closed: the handle must live as long as this process does.
    _JOB_HANDLES.append(job)
    return True


#: The job :func:`end_children_on_exit` made, held for this process's life.
_JOB_HANDLES: List[Any] = []

#: Exit status of :func:`run_in_job` when the job cannot be made; nothing ran.
JOB_UNAVAILABLE_EXIT = 125


def run_in_job(argv: List[str]) -> int:
    """Windows: run *argv* in a kill-on-close job this process holds, and wait.

    A shared program and everything it starts, detached helpers included, join
    the job, so all of them end when this process exits or is killed, even when
    the program itself exited first. The child gets this process's own standard
    handles. Nothing runs when the job cannot be made.
    """
    if not argv:
        return 2
    if not end_children_on_exit():
        sys.stderr.write("could not tie the shared program to its connection's lifetime\n")
        return JOB_UNAVAILABLE_EXIT
    return subprocess.call(
        argv,
        stdin=sys.stdin.fileno(),
        stdout=sys.stdout.fileno(),
        stderr=sys.stderr.fileno(),
    )


def _signal_group(pgid: int, sig: int) -> bool:
    """Send *sig* to process group *pgid*; False when no member is left."""
    if sys.platform == "win32":
        return False
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return False
    except PermissionError:
        # A member changed its user (a setuid program): alive, but out of reach.
        return True
    except OSError:
        return False
    return True


def end_groups(procs: List[Any], grace: float = GROUP_GRACE_SECS) -> None:
    """POSIX: end every member of each command's process group, then reap the leaders.

    Each group gets SIGTERM, then up to *grace* seconds (shared) to empty, then
    SIGKILL. A group is signalled by the id read when its leader started, which
    cannot name another group while any member of this one is alive: one that
    has emptied answers "no such group" and is left alone from then on.
    """
    if sys.platform == "win32":
        return
    alive = [proc for proc in procs if _signal_group(proc.pid, signal.SIGTERM)]
    deadline = time.monotonic() + grace
    while alive and time.monotonic() < deadline:
        time.sleep(_GROUP_POLL_SECS)
        for proc in alive:
            # Reap an exited leader, so only live members keep the group answering.
            with contextlib.suppress(OSError):
                proc.poll()
        alive = [proc for proc in alive if _signal_group(proc.pid, 0)]
    for proc in alive:
        _signal_group(proc.pid, signal.SIGKILL)
    for proc in procs:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            proc.wait(timeout=_REAP_SECS)


class CommandGroups:
    """POSIX: the process groups this server's commands run in, until each empties.

    A command runs in a group of its own (``start_new_session``), so a timeout
    ends it and everything it started (:func:`end_groups`). A group stays
    tracked after its command returns while anything it backgrounded still
    runs, and is ended with the rest when this server stops (:meth:`end_all`):
    on its input's end, on SIGTERM, or when the process that started it is gone
    (:meth:`watch`).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._procs: Dict[int, Any] = {}
        # Held while a command starts and while every group is ended, so no
        # stop, from any thread, runs between a ``Popen`` and its :meth:`add`.
        self._start_lock = threading.Lock()
        self._end_lock = threading.Lock()
        self._closed = False
        self._spawning = False
        self._retire_pending = False

    @contextlib.contextmanager
    def spawning(self) -> Iterator[None]:
        """Start one command so that every stop sees it tracked.

        Every stop path meets this block. :meth:`end_all` (input end, the
        parent watcher) waits for it to finish, and once it has run nothing
        starts again. The SIGTERM handler runs on this thread between any two
        lines, so inside the block it only notes the stop (:meth:`defer_retire`),
        which is raised here once the command is tracked.
        """
        with self._start_lock:
            if self._closed:
                raise SystemExit(0)
            self._spawning = True
            try:
                yield
            finally:
                self._spawning = False
        if self._retire_pending:
            self._retire_pending = False
            raise SystemExit(0)

    def defer_retire(self) -> bool:
        """Whether a stop arriving now is held for :meth:`spawning` to raise."""
        if not self._spawning:
            return False
        self._retire_pending = True
        return True

    def add(self, proc: Any) -> None:
        self.prune()
        with self._lock:
            self._procs[proc.pid] = proc

    def prune(self) -> None:
        """Forget each group with no member left, so its id is never signalled again."""
        with self._lock:
            procs = list(self._procs.values())
        for proc in procs:
            with contextlib.suppress(OSError):
                proc.poll()
            if proc.returncode is not None and not _signal_group(proc.pid, 0):
                with self._lock:
                    self._procs.pop(proc.pid, None)

    def end(self, proc: Any) -> None:
        """End one command's whole group now (a timeout or an unwind)."""
        end_groups([proc])

    def end_all(self) -> None:
        """End every tracked group: TERM, a shared grace, then KILL.

        A command that is starting finishes being tracked first (:meth:`spawning`),
        and none starts afterwards.
        """
        # Held for the whole stop: a second caller (the main thread after the
        # parent watcher, or the reverse) waits for this TERM-grace-KILL to
        # finish rather than finding no groups and exiting under it.
        with self._end_lock:
            with self._start_lock:
                self._closed = True
            self.prune()
            with self._lock:
                procs = list(self._procs.values())
                self._procs.clear()
            end_groups(procs)

    def watch(self, parent: int) -> threading.Thread:
        """Forget emptied groups each second; if *parent* is gone, end every group and exit.

        The parent is the connector. With it gone no stop is coming, and the
        input may stay open (a command holding it, or a pipe another process
        still has), so this server ends what its commands left and exits itself.
        """

        def run() -> None:
            while os.getppid() == parent:
                time.sleep(_PARENT_POLL_SECS)
                self.prune()
            self.end_all()
            os._exit(0)

        thread = threading.Thread(target=run, name="command-groups", daemon=True)
        thread.start()
        return thread


#: The flag that runs this file as a job launcher: ``--run-in-job -- <argv>``.
RUN_IN_JOB_FLAG = "--run-in-job"


if __name__ == "__main__":
    _args = sys.argv[1:]
    if len(_args) < 2 or _args[0] != RUN_IN_JOB_FLAG or _args[1] != "--":
        sys.stderr.write(f"usage: workspace_lifetime.py {RUN_IN_JOB_FLAG} -- <program> [args]\n")
        sys.exit(2)
    sys.exit(run_in_job(_args[2:]))
