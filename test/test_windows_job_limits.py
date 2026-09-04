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

The enforcement tests are real rather than mocked: they spawn a child, put it in
a job with a low ``ActiveProcessLimit``, and have the child itself try to fork
past that limit. Mocking ctypes here would only assert that we call the functions
we call, not that Windows honours them. The POSIX-inertness tests are
deliberately NOT Windows-gated — on the Linux/macOS runners they are the
guarantee that this change cannot affect those platforms.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time

import pytest

from kiro_crew import platform_compat, sandbox

_WINDOWS_ONLY = pytest.mark.skipif(
    not platform_compat.IS_WINDOWS, reason="Job objects are Windows-only"
)

# The job member waits for a go-signal (so the job is applied before it forks),
# then reports how many child spawns succeeded and the error that stopped it.
_MEMBER_SRC = textwrap.dedent("""
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
        try:
            k.wait(timeout=5)
        except Exception:
            pass
    """)

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
            creationflags=platform_compat._SUBPROCESS_NO_WINDOW,
        )
        try:
            assert platform_compat.apply_job_limits(
                child.pid, max_procs=4, max_memory_bytes=256 * 1024 * 1024
            )
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
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
            creationflags=platform_compat._SUBPROCESS_NO_WINDOW,
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
            # Tree kill, not a bare kill: this member deliberately spawns
            # `sleep(20)` grandchildren and only reaps them on its own normal
            # exit path, so killing just the member on the timeout path would
            # orphan them for 20s. They are in the job, but KILL_ON_JOB_CLOSE is
            # deliberately unset, so nothing else collects them. Then wait, so
            # teardown cannot outrun the termination it asked for.
            if child.poll() is None:
                try:
                    platform_compat.kill_process_tree(child.pid, platform_compat.SIGKILL)
                except Exception:
                    child.kill()
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                pass
        assert "ERR=OSError" in out, f"fork past the ceiling was NOT refused: {out!r}"
        assert (
            str(_ERROR_NOT_ENOUGH_QUOTA) in out
        ), f"expected WinError {_ERROR_NOT_ENOUGH_QUOTA} (not enough quota), got: {out!r}"

    @pytest.mark.parametrize("procs,mem", [(0, 1024), (-1, 1024), (4, 0), (4, -1)])
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
class TestSuspendedSpawnHandshake:
    """CREATE_SUSPENDED + assign + resume makes job assignment race-free.

    Job membership covers a member's FUTURE descendants only, so attaching a job
    to an already-running child leaves a window in which it could fork something
    that escapes the ceiling. A process created suspended has executed no
    instructions and therefore provably has no descendants, which closes the
    window by construction rather than making it merely small.
    """

    def test_suspended_child_does_not_run_until_resumed(self) -> None:
        """The negative control: suspension is real, and resume is load-bearing.

        Without this, a passing "resume worked" test would prove nothing — the
        child might simply have been running the whole time.
        """
        child = subprocess.Popen(
            [sys.executable, "-c", "print('RAN', flush=True)"],
            stdout=subprocess.PIPE,
            text=True,
            creationflags=(
                platform_compat.CREATE_NEW_PROCESS_GROUP | platform_compat.CREATE_SUSPENDED
            ),
        )
        try:
            time.sleep(1.0)
            assert child.poll() is None, "child exited despite CREATE_SUSPENDED"
        finally:
            child.kill()
            child.wait(timeout=15)

    def test_assign_while_suspended_then_resume_runs_the_child(self) -> None:
        child = subprocess.Popen(
            [sys.executable, "-c", "print('RAN', flush=True)"],
            stdout=subprocess.PIPE,
            text=True,
            creationflags=(
                platform_compat.CREATE_NEW_PROCESS_GROUP | platform_compat.CREATE_SUSPENDED
            ),
        )
        try:
            assert platform_compat.apply_job_limits(
                child.pid, max_procs=8, max_memory_bytes=256 * 1024 * 1024
            ), "job assignment failed on a suspended child"
            assert platform_compat.resume_process_main_thread(child.pid)
            out, _ = child.communicate(timeout=30)
            assert "RAN" in out
            assert child.returncode == 0
        finally:
            if child.poll() is None:
                child.kill()
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                pass

    def test_resume_of_unknown_pid_reports_failure(self) -> None:
        """A pid with no threads resumes nothing -> False, not a silent success.

        The caller treats False as fatal (kill the child rather than let a frozen
        process masquerade as a live agent), so a false positive here would
        reintroduce exactly the hang this guards against.
        """
        assert platform_compat.resume_process_main_thread(0x7FFFFFFF) is False


class TestFinishSuspendedSpawn:
    """The shared policy both ACP spawn sites apply after creating the child.

    Runs on every platform by faking the Windows branch, because the POSIX fleet
    is where regressions in this policy would otherwise go unnoticed.
    """

    @staticmethod
    def _fake_process() -> object:
        class _Proc:
            def __init__(self) -> None:
                self.killed = False

            def kill(self) -> None:
                self.killed = True

        return _Proc()

    @staticmethod
    def _claim_ownership(monkeypatch, acp_client) -> None:
        """Make the fake pid look like a confirmed child of this process.

        The destructive steps (ceiling, kill) are gated on
        ``get_ppid(pid) == os.getpid()``, so a test that wants to exercise them
        has to say the pid is ours. Without this the real ``get_ppid`` reports
        ``-1`` for an invented pid and the policy correctly declines to act.
        """
        monkeypatch.setattr(acp_client.platform_compat, "get_ppid", lambda pid: os.getpid())

    def test_posix_applies_the_ceiling_and_never_resumes(self, monkeypatch) -> None:
        """CREATE_SUSPENDED is 0 on POSIX, so nothing may be resumed there."""
        from kiro_crew.acp import client as acp_client

        calls: list[int] = []
        monkeypatch.setattr(acp_client.platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(
            acp_client, "apply_windows_resource_ceiling", lambda pid: calls.append(pid) or False
        )

        def _boom(pid: int) -> bool:
            raise AssertionError("POSIX must never call resume_process_main_thread")

        monkeypatch.setattr(acp_client.platform_compat, "resume_process_main_thread", _boom)
        proc = self._fake_process()
        acp_client.finish_suspended_spawn(proc, 1234, label="kiro-cli acp")  # type: ignore[arg-type]
        assert calls == [1234]
        assert not proc.killed  # type: ignore[attr-defined]

    def test_a_frozen_child_is_killed_and_the_spawn_fails_loudly(self, monkeypatch) -> None:
        """Resume failure on a LIVE pid must not be swallowed.

        An alive-but-frozen child would otherwise masquerade as a running agent
        and hang the session on the ACP handshake with no diagnosis.
        """
        from kiro_crew.acp import client as acp_client

        monkeypatch.setattr(acp_client.platform_compat, "IS_WINDOWS", True)
        self._claim_ownership(monkeypatch, acp_client)
        monkeypatch.setattr(acp_client, "apply_windows_resource_ceiling", lambda pid: True)
        monkeypatch.setattr(
            acp_client.platform_compat, "resume_process_main_thread", lambda pid: False
        )
        monkeypatch.setattr(acp_client.platform_compat, "pid_exists", lambda pid: True)
        proc = self._fake_process()
        with pytest.raises(acp_client.AcpError, match="failed to resume"):
            acp_client.finish_suspended_spawn(proc, 99, label="kiro-cli acp")  # type: ignore[arg-type]
        assert proc.killed, "a frozen child must be killed, not left running"  # type: ignore[attr-defined]

    def test_a_foreign_pid_is_neither_bounded_nor_killed(self, monkeypatch) -> None:
        """A pid that is not our child must survive this untouched.

        Both destructive steps would land on a stranger: the ceiling would impose
        a process and memory limit on it, and the unresumable branch would kill
        it. A pid arrives here straight from our own spawn, so in production it
        is always ours — but a mocked spawn (or a recycled pid) can put a live
        FOREIGN process in front of this code, and the only acceptable outcome is
        that nothing happens to it.
        """
        from kiro_crew.acp import client as acp_client

        ceilinged: list[int] = []
        monkeypatch.setattr(acp_client.platform_compat, "IS_WINDOWS", True)
        # Someone else's child, and it is alive and unresumable — the exact shape
        # that previously produced a kill.
        monkeypatch.setattr(acp_client.platform_compat, "get_ppid", lambda pid: os.getpid() + 1)
        monkeypatch.setattr(
            acp_client, "apply_windows_resource_ceiling", lambda pid: bool(ceilinged.append(pid))
        )
        monkeypatch.setattr(
            acp_client.platform_compat, "resume_process_main_thread", lambda pid: False
        )
        monkeypatch.setattr(acp_client.platform_compat, "pid_exists", lambda pid: True)
        proc = self._fake_process()
        acp_client.finish_suspended_spawn(proc, 4321, label="kiro-cli acp")  # type: ignore[arg-type]
        assert ceilinged == [], "a foreign process must never be put in our job object"
        assert not proc.killed, "a foreign process must never be killed"  # type: ignore[attr-defined]

    def test_an_already_exited_child_does_not_raise(self, monkeypatch) -> None:
        """Nothing to unfreeze when the pid is gone; the handshake reports why."""
        from kiro_crew.acp import client as acp_client

        monkeypatch.setattr(acp_client.platform_compat, "IS_WINDOWS", True)
        self._claim_ownership(monkeypatch, acp_client)
        monkeypatch.setattr(acp_client, "apply_windows_resource_ceiling", lambda pid: True)
        monkeypatch.setattr(
            acp_client.platform_compat, "resume_process_main_thread", lambda pid: False
        )
        monkeypatch.setattr(acp_client.platform_compat, "pid_exists", lambda pid: False)
        proc = self._fake_process()
        acp_client.finish_suspended_spawn(proc, 99, label="kiro-cli acp")  # type: ignore[arg-type]
        assert not proc.killed  # type: ignore[attr-defined]

    def test_a_failing_ceiling_still_resumes_the_child(self, monkeypatch) -> None:
        """The ceiling fails SOFT; the resume must happen regardless.

        This is why the resume lives in a ``finally``: a raising ceiling must
        never leave the child frozen.
        """
        from kiro_crew.acp import client as acp_client

        resumed: list[int] = []
        monkeypatch.setattr(acp_client.platform_compat, "IS_WINDOWS", True)
        self._claim_ownership(monkeypatch, acp_client)

        def _raising_ceiling(pid: int) -> bool:
            raise RuntimeError("ceiling exploded")

        monkeypatch.setattr(acp_client, "apply_windows_resource_ceiling", _raising_ceiling)
        monkeypatch.setattr(
            acp_client.platform_compat,
            "resume_process_main_thread",
            lambda pid: bool(resumed.append(pid)) or True,
        )
        proc = self._fake_process()
        with pytest.raises(RuntimeError, match="ceiling exploded"):
            acp_client.finish_suspended_spawn(proc, 77, label="kiro-cli acp")  # type: ignore[arg-type]
        assert resumed == [77], "the child must be resumed even when the ceiling raises"


@_WINDOWS_ONLY
class TestApplyWindowsResourceCeiling:
    def test_reads_the_shared_resource_limits_config(self, monkeypatch) -> None:
        """The sandbox wrapper forwards the cgroup path's configured limits.

        One ``resource_limits`` setting must govern both platforms — a separate
        Windows knob would drift.
        """
        seen: dict[str, int] = {}

        def _fake_limits() -> tuple[int, int, int, int]:
            return (7, 321, 100, 0)  # max_procs, max_mem_mb, cpu_weight, max_cpu_pct

        def _fake_apply(pid: int, *, max_procs: int, max_memory_bytes: int) -> bool:
            seen.update(pid=pid, max_procs=max_procs, max_memory_bytes=max_memory_bytes)
            return True

        monkeypatch.setattr(sandbox, "_cgroup_limits_from_config", _fake_limits)
        monkeypatch.setattr(sandbox.platform_compat, "apply_job_limits", _fake_apply)
        assert sandbox.apply_windows_resource_ceiling(4242) is True
        assert seen == {"pid": 4242, "max_procs": 7, "max_memory_bytes": 321 * 1024 * 1024}


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

    def test_memory_default_scales_with_the_host_without_sysconf(self, monkeypatch) -> None:
        """A Windows-shaped probe (no ``os.sysconf``) must still scale with RAM.

        ``os.sysconf`` does not exist on Windows, so the POSIX probe raises and
        the flat fallback would stand in for a real ceiling. That is not cosmetic
        now that the Job object consumes this value: on an 8 GB host the fallback
        EQUALS physical RAM, so the memory limit could never engage. Stubbing
        ``system_memory`` keeps this ungated, so the Linux runners prove the
        derivation too.
        """
        monkeypatch.delattr(os, "sysconf", raising=False)
        monkeypatch.setattr(
            sandbox.platform_compat, "system_memory", lambda: (16 * 1024**3, 8 * 1024**3)
        )
        mb = sandbox._default_max_memory_mb()
        assert mb == int(16 * 1024**3 * sandbox._CGROUP_MEMORY_FRACTION) // (1024 * 1024)
        assert mb != sandbox._CGROUP_FALLBACK_MAX_MEMORY_MB

    def test_memory_default_falls_back_only_when_both_probes_fail(self, monkeypatch) -> None:
        """The flat constant is the LAST resort, not the Windows default."""
        monkeypatch.delattr(os, "sysconf", raising=False)
        monkeypatch.setattr(sandbox.platform_compat, "system_memory", lambda: None)
        assert sandbox._default_max_memory_mb() == sandbox._CGROUP_FALLBACK_MAX_MEMORY_MB

    def test_resume_is_noop_on_posix(self, monkeypatch) -> None:
        monkeypatch.setattr(platform_compat, "IS_POSIX", True)
        assert platform_compat.resume_process_main_thread(1) is False

    def test_create_suspended_flag_is_zero_on_posix(self) -> None:
        # A non-zero flag leaking into a POSIX creationflags= would be ignored by
        # subprocess, but the constant must still be 0 so the ORed value at the
        # ACP spawn sites is exactly CREATE_NEW_PROCESS_GROUP | NO_WINDOW there.
        if os.name != "nt":
            assert platform_compat.CREATE_SUSPENDED == 0

    def test_ceiling_wrapper_is_a_noop_on_posix(self, monkeypatch) -> None:
        monkeypatch.setattr(sandbox.platform_compat, "IS_WINDOWS", False)
        called = False

        def _boom(*a: object, **k: object) -> bool:
            nonlocal called
            called = True
            return True

        monkeypatch.setattr(sandbox.platform_compat, "apply_job_limits", _boom)
        assert sandbox.apply_windows_resource_ceiling(1) is False
        assert not called, "POSIX must never reach the Job object path"
