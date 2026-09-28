"""Process-group-safe subprocess helper for the installer test suites.

The installer tests run the real ``cli.sh``, which probes interpreters and
package managers as grandchildren. ``subprocess.run(timeout=...)`` kills only
the process it spawned, and pytest-timeout's signal only unwinds the Python
frame, so a wedged grandchild survives either one: it is reparented to init and
keeps burning a core until someone kills it by hand. Running the child as a
session leader makes the whole tree killable as one process group.

Not every caller is POSIX-only: the PowerShell tests skip only when no
``pwsh``/``powershell`` binary exists, so they DO run on Windows CI. There the
child is wrapped in a kill-on-close Job object at spawn, the tree kill
terminates the job (falling back to ``taskkill /T /F`` via
``platform_compat.kill_process_tree``), and the timeout path re-raises
``TimeoutExpired`` so callers' skip logic keeps working.
"""

from __future__ import annotations

import contextlib
import subprocess

from kiro_crew import platform_compat

#: A hermetic installer run (fake curl, fake package managers) finishes in
#: seconds, so this bound only ever trips on a wedge. It sits well under
#: pytest-timeout's ceiling so that this helper, rather than pytest-timeout, is
#: what reaps the tree.
INSTALLER_TIMEOUT = 60.0

#: Bound on draining the pipes after the kill. A reaped tree closes its pipe
#: write handles immediately, so this only trips if a descendant survived the
#: kill; without it, ``communicate()`` would block until that survivor exits
#: and the caller's skip path would never run.
_POST_KILL_DRAIN_TIMEOUT = 5.0

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001


class _KillOnCloseJob:
    """A Windows Job object that guarantees the child's tree cannot leak.

    ``taskkill /T`` walks the live process list and can fail (access denied on
    a descendant, binary not resolvable), and a direct ``proc.kill()`` reaps
    only the immediate child. A Job with ``KILL_ON_JOB_CLOSE`` closes both
    gaps: ``terminate()`` kills every member with creator rights, and if this
    process dies without ever calling it, the OS kills the members when the
    last job handle is destroyed. The child must be spawned with
    ``CREATE_SUSPENDED`` so it is assigned before it can fork anything that
    would escape the job (same handshake ``platform_compat.apply_job_limits``
    documents). This lifecycle semantic is exactly what production code must
    NOT do — ``apply_job_limits`` deliberately omits ``KILL_ON_JOB_CLOSE`` —
    which is why this small test-only wrapper exists instead of a
    ``platform_compat`` helper.
    """

    def __init__(self) -> None:
        self._kernel32 = None
        self._job = None

    def assign(self, pid: int) -> bool:
        """Create the job and put *pid* in it. False (never raises) on failure."""
        try:
            import ctypes
            from ctypes import wintypes

            from kiro_crew.platform_compat import (
                _JOBOBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                _JobObjectExtendedLimitInformation,
            )

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
            kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            kernel32.SetInformationJobObject.argtypes = [
                wintypes.HANDLE,
                ctypes.c_int,
                wintypes.LPVOID,
                wintypes.DWORD,
            ]
            kernel32.SetInformationJobObject.restype = wintypes.BOOL
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
            kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, ctypes.c_uint]
            kernel32.TerminateJobObject.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL

            job = kernel32.CreateJobObjectW(None, None)
            if not job:
                return False
            info = _JobObjectExtendedLimitInformation()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel32.SetInformationJobObject(
                job,
                _JOBOBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                ctypes.byref(info),
                ctypes.sizeof(info),
            ):
                kernel32.CloseHandle(job)
                return False
            proc_handle = kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
            if not proc_handle:
                kernel32.CloseHandle(job)
                return False
            assigned = bool(kernel32.AssignProcessToJobObject(job, proc_handle))
            kernel32.CloseHandle(proc_handle)
            if not assigned:
                kernel32.CloseHandle(job)
                return False
            self._kernel32 = kernel32
            self._job = job
            return True
        except Exception:
            return False

    def terminate(self) -> bool:
        """Kill every process in the job. False (never raises) on failure."""
        if self._kernel32 is None or self._job is None:
            return False
        try:
            return bool(self._kernel32.TerminateJobObject(self._job, 1))
        except Exception:
            return False

    def close(self) -> None:
        """Release the handle; the OS reaps any remaining members."""
        if self._kernel32 is not None and self._job is not None:
            with contextlib.suppress(Exception):
                self._kernel32.CloseHandle(self._job)
        self._kernel32 = None
        self._job = None


def run_bounded(
    argv: list[str],
    env: dict[str, str],
    timeout: float = INSTALLER_TIMEOUT,
    cwd: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` bounded by *timeout*; on timeout kill its whole tree.

    On POSIX ``start_new_session`` makes the child a session leader, so its
    pid is also its process-group id and ``kill_process_tree`` reaps every
    descendant with one ``killpg``. On Windows the child starts suspended in
    its own process group, is assigned to a kill-on-close Job object, and is
    then resumed; the tree kill terminates the job, falling back to
    ``taskkill /T /F`` and then to killing the direct child. Either way the
    ``subprocess.TimeoutExpired`` is re-raised once the kill is done — callers
    rely on catching it to skip.

    ``cwd`` matters when the thing under test resolves a RELATIVE path: an
    installer that bakes one into a generated wrapper can only be caught by
    running it from a known directory.
    """
    is_posix = platform_compat.IS_POSIX
    creationflags = 0
    if not is_posix:
        creationflags = platform_compat.CREATE_NEW_PROCESS_GROUP | platform_compat.CREATE_SUSPENDED
    job = _KillOnCloseJob()
    try:
        with subprocess.Popen(
            argv,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=cwd,
            start_new_session=is_posix,
            creationflags=creationflags,
        ) as proc:
            if not is_posix:
                # Suspended-spawn handshake: assign to the job while the child
                # provably has no descendants, then let it run. On a host where
                # either step fails the child simply stays subject to the
                # taskkill/direct-kill ladder below (and a never-resumed child
                # trips the timeout and is reaped the same way).
                job.assign(proc.pid)
                platform_compat.resume_process_main_thread(proc.pid)
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                if not job.terminate():
                    try:
                        platform_compat.kill_process_tree(proc.pid, platform_compat.SIGKILL)
                    except OSError:
                        # Already exited (ProcessLookupError), or the platform
                        # shim cannot run: reap the direct child so the drain
                        # below cannot block on its own pipe handles.
                        proc.kill()
                try:
                    proc.communicate(timeout=_POST_KILL_DRAIN_TIMEOUT)
                except subprocess.TimeoutExpired:
                    pass  # A surviving descendant holds the pipes; do not block on it.
                # Bound the context manager's exit too: Popen.__exit__ calls an
                # UNBOUNDED wait(), and the raise below runs through it.
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=_POST_KILL_DRAIN_TIMEOUT)
                raise
    finally:
        # KILL_ON_JOB_CLOSE: dropping the last handle makes the OS reap any
        # member that survived every kill above — and, on the success path, any
        # straggler the child left behind.
        job.close()
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)
