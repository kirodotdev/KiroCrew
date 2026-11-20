"""Which live processes still use a script cron's scratch tree, read from a Job object.

The scratch sweep (:func:`kiro_crew.agent_scratch.sweep_dead_scratch`) keeps a
tree while a process group its owner marker names is alive, and on Linux while
a process outside those groups still uses it
(:mod:`kiro_crew.agent_scratch_users`). Windows has no ``/proc`` to read and no
process groups, so a script cron's run is judged by the recorded script pid
alone. A script that starts a background process (a watcher, a dev server, a
detached helper) with the run's temp dir as its working directory and returns
leaves that process behind a dead recorded owner, and an hour later the sweep
deletes every file the process does not hold open.

Windows can answer the question directly. Each run's script child joins a named
Job object (:func:`join`), and a Windows process's descendants join its job
automatically, so the job lists exactly the live processes the run started,
whether or not their parent is still alive. The sweep opens the job by the
tree's name and reads its process list (:func:`scratch_users`): a non-empty list
is a live user, and a job the system has freed (its last member exited) is none.

The job carries no limits and no ``KILL_ON_JOB_CLOSE``, matching the agent Job
(:func:`kiro_crew.platform_compat.apply_job_limits`): it only names the group, so
background work a script started on purpose keeps running. It is named from the
tree's directory name, which the sweep already has, so ``.owner`` is unchanged.
A job object outlives the handle that created it while a process is assigned to
it, which is why :func:`join` closes its handle at once and why a gateway that
restarts still finds the job of a run it did not make.

Windows only. Every function answers "nothing" elsewhere.
"""

from __future__ import annotations

import ctypes
import logging
import os
from ctypes import wintypes
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat

logger = logging.getLogger(__name__)

_JOB_NAME_PREFIX = "Local\\kirocrew-scratch-"
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_JOB_OBJECT_QUERY = 0x0004
_JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
_ERROR_MORE_DATA = 234

#: Process ids read per query. One is enough to name a user; the rest only makes
#: the call fill more of the list.
_LISTED_PIDS = 8


class _BasicProcessIdList(ctypes.Structure):
    """``JOBOBJECT_BASIC_PROCESS_ID_LIST`` with room for :data:`_LISTED_PIDS` ids."""

    _fields_ = [
        ("NumberOfAssignedProcesses", wintypes.DWORD),
        ("NumberOfProcessIdsInList", wintypes.DWORD),
        ("ProcessIdList", ctypes.c_size_t * _LISTED_PIDS),
    ]


def job_name(tree_name: str) -> str | None:
    """The Job object name for the scratch tree *tree_name*, or None for one with no legal name.

    A tree name is one directory name under the scratch root, so a separator in
    it means the caller handed over something else, and a Job name takes a
    backslash as its namespace separator.
    """
    if not tree_name or "\\" in tree_name or "/" in tree_name:
        return None
    return _JOB_NAME_PREFIX + tree_name


def join(tree_name: str, pid: int) -> bool:
    """Put the process *pid* into the Job object of the scratch tree *tree_name*.

    Called for a script child right after it is spawned and before it has run
    any of the script, so every process it starts afterwards is a member. Returns
    False, and logs, when the job could not be made or the process not assigned:
    the run goes on and the sweep falls back to the owner marker alone.
    """
    name = job_name(tree_name)
    if name is None or not platform_compat.IS_WINDOWS:
        return False
    job = process = None
    kernel32 = None
    try:  # pragma: no cover - Win32 calls; exercised by test_agent_scratch_job.py on Windows
        kernel32 = _kernel32()
        job = kernel32.CreateJobObjectW(None, name)
        if not job:
            logger.warning(
                "agent-scratch: could not create the job for %r (err=%s)",
                tree_name,
                platform_compat._windows_last_error(),
            )
            return False
        process = kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not process:
            logger.warning(
                "agent-scratch: could not open pid %d for the job of %r (err=%s)",
                pid,
                tree_name,
                platform_compat._windows_last_error(),
            )
            return False
        if not kernel32.AssignProcessToJobObject(job, process):
            logger.warning(
                "agent-scratch: could not put pid %d in the job of %r (err=%s)",
                pid,
                tree_name,
                platform_compat._windows_last_error(),
            )
            return False
        return True
    except Exception:
        logger.warning("agent-scratch: joining the job of %r failed", tree_name, exc_info=True)
        return False
    finally:
        for handle in (process, job):
            if handle and kernel32 is not None:
                kernel32.CloseHandle(handle)


def job_pids(tree_name: str) -> list[int]:
    """The ids of the live processes in the Job object of *tree_name*, possibly a few of many.

    Empty when the tree has no job (none was made, or its last member exited and
    the system freed it), when the job has no live member, and when the job
    cannot be read: this is a lower bound, like the ``/proc`` read it stands
    beside.
    """
    name = job_name(tree_name)
    if name is None or not platform_compat.IS_WINDOWS:
        return []
    job = None
    kernel32 = None
    try:  # pragma: no cover - Win32 calls; exercised by test_agent_scratch_job.py on Windows
        kernel32 = _kernel32()
        job = kernel32.OpenJobObjectW(_JOB_OBJECT_QUERY, False, name)
        if not job:
            return []
        listing = _BasicProcessIdList()
        ok = kernel32.QueryInformationJobObject(
            job,
            _JOB_OBJECT_BASIC_PROCESS_ID_LIST,
            ctypes.byref(listing),
            ctypes.sizeof(listing),
            None,
        )
        # More members than the buffer holds still fills the buffer.
        if not ok and platform_compat._windows_last_error() != _ERROR_MORE_DATA:
            return []
        return [int(pid) for pid in listing.ProcessIdList[: listing.NumberOfProcessIdsInList]]
    except Exception:
        logger.debug("agent-scratch: reading the job of %r failed", tree_name, exc_info=True)
        return []
    finally:
        if job and kernel32 is not None:
            kernel32.CloseHandle(job)


def scratch_users(root: Path) -> dict[str, int]:
    """Map each direct child of *root* whose job has a live member to one member's pid."""
    try:
        names = os.listdir(root)
    except OSError:
        return {}
    users: dict[str, int] = {}
    for name in names:
        pids = job_pids(name)
        if pids:
            users[name] = pids[0]
    return users


def _kernel32() -> Any:  # pragma: no cover - Windows only
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.OpenJobObjectW.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
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
