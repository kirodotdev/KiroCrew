"""Which live processes still use a script cron's scratch tree, read from a Job object.

The scratch sweep (:func:`kiro_crew.agent_scratch.sweep_dead_scratch`) keeps a
tree while a process group its owner marker names is alive, and on Linux while
a process outside those groups still uses it
(:mod:`kiro_crew.agent_scratch_users`). Windows has no ``/proc`` to read and no
process groups, so a script cron's run is otherwise judged by the recorded
script pid alone. A script that starts a background process (a watcher, a dev
server, a detached helper) with the run's temp dir as its working directory and
returns leaves that process behind a dead recorded owner, and an hour later the
sweep would delete every file the process does not hold open.

Windows can answer the question directly. Each run's script child joins a Job
object of its own (:func:`join`) before it runs, and a Windows process's
descendants join its job automatically, so the job lists exactly the live
processes the run started, whether or not their parent is still alive. The
sweep reads each job's process list (:func:`scratch_users`): a non-empty list is
a live user.

This process keeps the job's handle, keyed by the tree's directory name, for as
long as the job has a live member, and closes it the first time it reads the job
empty. It has to: a job object outlives its last handle while members remain,
but nothing can open it again, by name or otherwise (``pod/_windows_job.py``
records the same rule for a named job), so a handle closed early would leave a
job no sweep can read. The job carries no limits and no ``KILL_ON_JOB_CLOSE``,
matching the agent Job (:func:`kiro_crew.platform_compat.apply_job_limits`): it
only lists the run's processes, so background work a script started on purpose
keeps running, and closing the handle ends nothing.

shortcut: the handles live in this process only, so after a gateway restart a
run from before it has no job to read and its tree is judged by the owner
marker alone, as on macOS. Upgrade when a restart-surviving locator exists
(a process that holds the handles across gateway lifetimes).

Windows only. Every function answers "nothing" elsewhere.
"""

from __future__ import annotations

import ctypes
import logging
import subprocess
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat

logger = logging.getLogger(__name__)

_JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
_ERROR_MORE_DATA = 234

#: Process ids read per query. One is enough to name a user; the rest only makes
#: the call fill more of the list.
_LISTED_PIDS = 8

#: The open job handle of each tree whose job may still have a live member.
_JOBS: dict[str, int] = {}
_JOBS_LOCK = threading.Lock()


class _BasicProcessIdList(ctypes.Structure):
    """``JOBOBJECT_BASIC_PROCESS_ID_LIST`` with room for :data:`_LISTED_PIDS` ids."""

    _fields_ = [
        ("NumberOfAssignedProcesses", wintypes.DWORD),
        ("NumberOfProcessIdsInList", wintypes.DWORD),
        ("ProcessIdList", ctypes.c_size_t * _LISTED_PIDS),
    ]


def join(tree_name: str, proc: subprocess.Popen[Any]) -> bool:
    """Put the child *proc* into a new Job object kept for the scratch tree *tree_name*.

    Called for a script child that was created ``CREATE_SUSPENDED``, before it
    has run any of the script, so every process it starts afterwards is a
    member. Returns False, and logs, when the job could not be made or the
    process not assigned: the run goes on and the sweep falls back to the owner
    marker alone.
    """
    if not platform_compat.IS_WINDOWS:
        return False
    try:  # pragma: no cover - Win32 calls; exercised by test_agent_scratch_job.py on Windows
        kernel32 = _kernel32()
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            logger.warning(
                "agent-scratch: could not create the job for %r (err=%s)",
                tree_name,
                platform_compat._windows_last_error(),
            )
            return False
        # Popen's own handle carries every right the assignment needs, and is
        # the exact process this spawn made, never a reused pid.
        if not kernel32.AssignProcessToJobObject(job, int(getattr(proc, "_handle"))):
            logger.warning(
                "agent-scratch: could not put pid %d in the job of %r (err=%s)",
                proc.pid,
                tree_name,
                platform_compat._windows_last_error(),
            )
            kernel32.CloseHandle(job)
            return False
    except Exception:  # pragma: no cover - Windows only
        logger.warning("agent-scratch: joining the job of %r failed", tree_name, exc_info=True)
        return False
    with _JOBS_LOCK:  # pragma: no cover - Windows only
        previous = _JOBS.get(tree_name)
        _JOBS[tree_name] = int(job)
    if previous is not None:  # pragma: no cover - a tree is allocated for one run
        _close(previous)
    return True  # pragma: no cover - Windows only


def scratch_users(root: Path) -> dict[str, int]:
    """Map each direct child of *root* whose job has a live member to one member's pid.

    A job read empty has no member left and none can join it, so its handle is
    closed and the tree is forgotten. A job that cannot be read is kept for the
    next sweep and reads as no user: this is a lower bound, like the ``/proc``
    read it stands beside.
    """
    with _JOBS_LOCK:
        held = list(_JOBS.items())
    users: dict[str, int] = {}
    for name, job in held:
        pids = job_pids(job)
        if pids is None:
            continue
        if not pids:
            with _JOBS_LOCK:
                if _JOBS.get(name) == job:
                    del _JOBS[name]
            _close(job)
            continue
        if (root / name).is_dir():
            users[name] = pids[0]
    return users


def job_pids(job: int) -> list[int] | None:
    """The ids of the live processes in *job*, possibly a few of many; None when unreadable."""
    try:  # pragma: no cover - Win32 calls; exercised by test_agent_scratch_job.py on Windows
        listing = _BasicProcessIdList()
        ok = _kernel32().QueryInformationJobObject(
            job,
            _JOB_OBJECT_BASIC_PROCESS_ID_LIST,
            ctypes.byref(listing),
            ctypes.sizeof(listing),
            None,
        )
        # More members than the buffer holds still fills the buffer.
        if not ok and platform_compat._windows_last_error() != _ERROR_MORE_DATA:
            return None
        return [int(pid) for pid in listing.ProcessIdList[: listing.NumberOfProcessIdsInList]]
    except Exception:  # pragma: no cover - Windows only
        logger.debug("agent-scratch: reading a scratch job failed", exc_info=True)
        return None


def _close(job: int) -> None:  # pragma: no cover - Windows only
    try:
        _kernel32().CloseHandle(job)
    except Exception:
        logger.debug("agent-scratch: closing a scratch job failed", exc_info=True)


def _kernel32() -> Any:  # pragma: no cover - Windows only
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
    ]
    kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32
