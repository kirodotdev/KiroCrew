"""Windows Job object resource ceilings — the cgroup-v2-scope analogue.

``sandbox.cgroup_scope_argv`` bounds an agent subprocess and all its descendants
via ``systemd-run --user --scope`` (``TasksMax`` = fork-bomb ceiling,
``MemoryMax`` = RSS-balloon ceiling). It is a no-op on Windows, so agent
subprocesses there ran with NO ceiling at all — the gateway logs
``SECURITY: cgroup v2 scope enforcement unavailable (not Linux)`` on every boot.

``platform_compat.apply_job_limits`` is the native equivalent. Because a Job
object cannot be expressed as an argv prefix it is applied to a live pid after
the spawn, wrapped by ``sandbox.apply_windows_resource_ceiling`` which reads the
SAME ``resource_limits`` config as the cgroup path.

The enforcement test is real rather than mocked: it spawns a child, puts it in a
job with a low ``ActiveProcessLimit``, and has the child itself try to fork past
that limit. Mocking ctypes here would only assert that we call the functions we
call, not that Windows honours them.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from kiro_crew import platform_compat, sandbox

pytestmark: list = []  # per-class gating below; POSIX no-op tests must run everywhere

_WINDOWS_ONLY = pytest.mark.skipif(
    not platform_compat.IS_WINDOWS, reason="Job objects are Windows-only"
)

# The job member waits for a go-signal (so the job is applied before it forks),
# then reports how many child spawns succeeded and the error that stopped it.
_MEMBER_SRC = textwrap.dedent(
    """
    import subprocess, sys
    sys.stdin.readline()
    ok, err, kids = 0, "", []
    for _ in range(6):
        try:
            kids.append(subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"]))
            ok += 1
        except OSError as e:
            err = f"{type(e).__name__}:{e.winerror if hasattr(e, 'winerror') else ''}"
            break
    print(f"SPAWNED={ok} ERR={err}", flush=True)
    for k in kids:
        k.kill()
    """
)

_ERROR_NOT_ENOUGH_QUOTA = 1816


@_WINDOWS_ONLY
class TestApplyJobLimits:
    def test_process_joins_a_job_and_limits_outlive_our_handle(self) -> None:
        """Assignment sticks after we close the job handle.

        ``apply_job_limits`` deliberately omits
        ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` and closes both handles before
        returning: a job object stays alive while processes are assigned, so the
        limits keep applying with no handle registry to manage AND no change to
        process lifecycle. If that assumption were wrong, the process would not
        report as being in a job here.
        """
        import ctypes
        from ctypes import wintypes

        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            assert platform_compat.apply_job_limits(
                child.pid, max_procs=4, max_memory_bytes=256 * 1024 * 1024
            )
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.IsProcessInJob.argtypes = [
                wintypes.HANDLE,
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.BOOL),
            ]
            k32.IsProcessInJob.restype = wintypes.BOOL
            handle = k32.OpenProcess(0x1000, False, child.pid)  # QUERY_LIMITED_INFORMATION
            assert handle
            try:
                in_job = wintypes.BOOL()
                assert k32.IsProcessInJob(handle, None, ctypes.byref(in_job))
                assert in_job.value, "process was not assigned to a job"
            finally:
                k32.CloseHandle(handle)
        finally:
            child.kill()
            child.wait(timeout=15)

    def test_active_process_limit_actually_refuses_a_fork_bomb(self) -> None:
        """The ceiling is enforced by the kernel, from inside the job.

        Asserts the spawn is REFUSED with the quota error rather than asserting
        an exact successful-spawn count: the job's process accounting includes
        transient/exiting members, so the precise cutoff is not something to
        pin. The security property is that unbounded forking stops.
        """
        child = subprocess.Popen(
            [sys.executable, "-c", _MEMBER_SRC],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            assert platform_compat.apply_job_limits(
                child.pid, max_procs=3, max_memory_bytes=512 * 1024 * 1024
            )
            assert child.stdin is not None
            child.stdin.write("go\n")
            child.stdin.flush()
            out, _ = child.communicate(timeout=90)
        finally:
            if child.poll() is None:
                child.kill()
        assert "ERR=OSError" in out, f"fork past the ceiling was NOT refused: {out!r}"
        assert (
            str(_ERROR_NOT_ENOUGH_QUOTA) in out
        ), f"expected WinError {_ERROR_NOT_ENOUGH_QUOTA} (not enough quota), got: {out!r}"

    @pytest.mark.parametrize(
        "procs,mem",
        [(0, 1024), (-1, 1024), (4, 0), (4, -1)],
    )
    def test_non_positive_limits_are_refused(self, procs: int, mem: int) -> None:
        """A zero/negative limit means "unset", never "unlimited job"."""
        assert platform_compat.apply_job_limits(1, max_procs=procs, max_memory_bytes=mem) is False

    def test_unknown_pid_fails_soft(self) -> None:
        """A dead/absent pid returns False rather than raising.

        A missing ceiling must never fail the spawn — same contract as an
        unavailable cgroup scope.
        """
        assert (
            platform_compat.apply_job_limits(
                0x7FFFFFFF, max_procs=4, max_memory_bytes=64 * 1024 * 1024
            )
            is False
        )


@_WINDOWS_ONLY
class TestApplyWindowsResourceCeiling:
    def test_reads_the_shared_resource_limits_config(self, monkeypatch) -> None:
        """The sandbox wrapper forwards the cgroup path's configured limits.

        One ``resource_limits`` setting must govern both platforms — a separate
        Windows knob would drift.
        """
        seen: dict[str, int] = {}

        def _fake_limits():
            return (7, 321, 100, 0)  # max_procs, max_mem_mb, cpu_weight, max_cpu_pct

        def _fake_apply(pid, *, max_procs, max_memory_bytes):
            seen.update(pid=pid, max_procs=max_procs, max_memory_bytes=max_memory_bytes)
            return True

        monkeypatch.setattr(sandbox, "_cgroup_limits_from_config", _fake_limits)
        monkeypatch.setattr(sandbox.platform_compat, "apply_job_limits", _fake_apply)
        assert sandbox.apply_windows_resource_ceiling(4242) is True
        assert seen == {
            "pid": 4242,
            "max_procs": 7,
            "max_memory_bytes": 321 * 1024 * 1024,
        }


class TestPosixIsUnaffected:
    """The POSIX cgroup path must not be touched by any of this.

    Deliberately NOT Windows-gated — these must also hold on the Linux/macOS CI
    runners, where they are the guarantee that this change is inert.
    """

    def test_apply_job_limits_is_a_noop_on_posix(self, monkeypatch) -> None:
        monkeypatch.setattr(platform_compat, "IS_POSIX", True)
        assert (
            platform_compat.apply_job_limits(1, max_procs=4, max_memory_bytes=1024 * 1024) is False
        )

    def test_ceiling_wrapper_is_a_noop_on_posix(self, monkeypatch) -> None:
        monkeypatch.setattr(sandbox.platform_compat, "IS_WINDOWS", False)
        called = False

        def _boom(*a, **k):
            nonlocal called
            called = True
            return True

        monkeypatch.setattr(sandbox.platform_compat, "apply_job_limits", _boom)
        assert sandbox.apply_windows_resource_ceiling(1) is False
        assert not called, "POSIX must never reach the Job object path"
