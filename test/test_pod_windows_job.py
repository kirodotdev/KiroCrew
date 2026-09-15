"""Pod lifetime Job contracts: injected failures and self-owned Windows processes."""

from __future__ import annotations

import ctypes as C
import subprocess
import sys
from types import SimpleNamespace

import pytest

from kiro_crew import platform_compat as pc
from kiro_crew.pod import _windows_job as jobs


class Kernel:
    """No host process or native library is consulted by failure tests."""

    def __init__(self):
        self.calls = []
        self.fail = ""
        self.flags = 0
        self.counts = [0]
        self.member = True
        self.short = False
        self.sddl = ""

    def ConvertStringSecurityDescriptorToSecurityDescriptorW(self, sddl, revision, out, size):
        self.sddl = sddl
        C.cast(out, C.POINTER(C.c_void_p))[0] = 123
        return self.fail != "acl"

    def CreateJobObjectW(self, attributes, name):
        attributes = C.cast(attributes, C.POINTER(jobs._SecurityAttributes)).contents
        assert attributes.descriptor == 123 and not attributes.inherit
        self.calls.append(("create", name))
        return 0 if self.fail == "create" else 8001

    def LocalFree(self, pointer):
        self.calls.append(("free", pointer.value))

    def OpenJobObjectW(self, access, inherit, name):
        assert access == jobs._JOB_QUERY | jobs._JOB_TERMINATE
        assert not inherit
        self.calls.append(("open", name))
        return 0 if self.fail == "open" else 8002

    def SetInformationJobObject(self, handle, kind, pointer, size):
        limits = C.cast(pointer, C.POINTER(pc._JobObjectExtendedLimitInformation)).contents
        assert limits.BasicLimitInformation.LimitFlags == 0
        self.calls.append(("policy", handle.value))
        return self.fail != "policy"

    def QueryInformationJobObject(self, handle, kind, pointer, size, returned):
        self.calls.append(("query", kind))
        if kind == jobs._EXTENDED_LIMITS:
            result = C.cast(pointer, C.POINTER(pc._JobObjectExtendedLimitInformation)).contents
            result.BasicLimitInformation.LimitFlags = self.flags
        else:
            result = C.cast(pointer, C.POINTER(jobs._Accounting)).contents
            result.active_processes = self.counts[0]
            if len(self.counts) > 1:
                self.counts.pop(0)
        C.cast(returned, C.POINTER(jobs._DWORD))[0] = size - 1 if self.short else size
        return self.fail != "query"

    def AssignProcessToJobObject(self, job, process):
        self.calls.append(("assign", process.value))
        return self.fail != "assign"

    def IsProcessInJob(self, process, job, member):
        self.calls.append(("membership", process.value))
        C.cast(member, C.POINTER(jobs._BOOL))[0] = self.member
        return self.fail != "membership"

    def TerminateJobObject(self, job, code):
        self.calls.append(("terminate", job.value))
        return self.fail != "terminate"

    def CloseHandle(self, handle):
        self.calls.append(("close", handle.value))
        return self.fail != "close"


@pytest.fixture
def kernel(monkeypatch):
    api = Kernel()
    monkeypatch.setattr(jobs, "_load", lambda: (api, api))
    monkeypatch.setattr(pc, "current_user_sid", lambda: "S-1-5-21-123")
    monkeypatch.setattr(jobs, "_last_error", lambda: 0)
    return api


def test_create_owner_only_unique_and_open_without_create(kernel):
    with jobs.PodJob.create() as first, jobs.PodJob.create() as second:
        assert first.name != second.name
        assert kernel.sddl == "O:S-1-5-21-123D:P(A;;0x001f003f;;;S-1-5-21-123)"
        kernel.calls.clear()
        with jobs.PodJob.open_existing(first.name):
            pass
        assert not any(call[0] == "create" for call in kernel.calls)
        assert ("open", first.name) in kernel.calls


@pytest.mark.parametrize("sid", [None, "", "not-a-sid", "S-1-5-21)(A;;GA;;;WD)"])
def test_unknown_owner_refuses_before_creation(kernel, monkeypatch, sid):
    monkeypatch.setattr(pc, "current_user_sid", lambda: sid)
    with pytest.raises(OSError, match="SID"):
        jobs.PodJob.create()
    assert kernel.calls == []


@pytest.mark.parametrize("failure", ["acl", "create", "policy", "query", "collision"])
def test_creation_failure_releases_owned_resources(kernel, monkeypatch, failure):
    kernel.fail = failure
    if failure == "collision":
        monkeypatch.setattr(jobs, "_last_error", lambda: jobs._ERROR_ALREADY_EXISTS)
    with pytest.raises(OSError):
        jobs.PodJob.create()
    assert (("free", 123) in kernel.calls) is (failure != "acl")
    assert (("close", 8001) in kernel.calls) is (failure in {"policy", "query", "collision"})
    if failure == "collision":
        assert ("policy", 8001) not in kernel.calls


@pytest.mark.parametrize("flags", [0x0800, 0x1000, 0x2000])
def test_open_rejects_noncontainment_policy_and_closes(kernel, flags):
    kernel.flags = flags
    with pytest.raises(OSError, match="breakaway or kill-on-close"):
        jobs.PodJob.open_existing(jobs._NAME_PREFIX + "a" * 32)
    assert ("close", 8002) in kernel.calls


def test_missing_job_is_not_recreated(kernel):
    kernel.fail = "open"
    with pytest.raises(OSError):
        jobs.PodJob.open_existing(jobs._NAME_PREFIX + "a" * 32)
    assert [call[0] for call in kernel.calls] == ["open"]


@pytest.mark.parametrize("name", ["", "Global\\other", jobs._NAME_PREFIX + "a" * 32 + "\n"])
def test_invalid_job_locator_never_reaches_kernel(kernel, name):
    with pytest.raises(ValueError):
        jobs.PodJob.open_existing(name)
    assert not kernel.calls


def test_assignment_and_membership_borrow_exact_process_handle(kernel):
    with jobs.PodJob.create() as job:
        job.assign_suspended(0x100000001)
        assert job.contains(0x100000001)
        kernel.member = False
        assert not job.contains(0x100000001)
    assert ("assign", 0x100000001) in kernel.calls
    assert ("close", 0x100000001) not in kernel.calls


@pytest.mark.parametrize("failure", ["assign", "membership", "not_member", "query"])
def test_assignment_never_swallows_an_incomplete_proof(kernel, failure):
    with jobs.PodJob.create() as job:
        kernel.fail = failure
        kernel.member = failure != "not_member"
        with pytest.raises(OSError):
            job.assign_suspended(9001)


@pytest.mark.parametrize("handle", [0, -1, True, "42", C.c_void_p(-1).value])
def test_invalid_process_handle_is_rejected(kernel, handle):
    with jobs.PodJob.create() as job:
        with pytest.raises(ValueError):
            job.assign_suspended(handle)
    assert not any(call[0] == "assign" for call in kernel.calls)


def test_drain_waits_for_kernel_zero_not_termination_return(kernel, monkeypatch):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(jobs.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(
        jobs.time, "sleep", lambda seconds: setattr(clock, "now", clock.now + seconds)
    )
    with jobs.PodJob.create() as job:
        kernel.counts = [2, 1, 0]
        job.terminate_and_wait(timeout=1)
        assert clock.now == pytest.approx(2 * jobs._POLL_SECONDS)
        kernel.counts = [1]
        with pytest.raises(TimeoutError):
            job.terminate_and_wait(timeout=0.1)
        assert clock.now == pytest.approx(0.2)


@pytest.mark.parametrize("failure", ["terminate", "query", "short"])
def test_drain_cannot_turn_api_failure_into_zero(kernel, failure):
    with jobs.PodJob.create() as job:
        kernel.fail = failure
        kernel.short = failure == "short"
        with pytest.raises(OSError):
            job.terminate_and_wait(timeout=0)


@pytest.mark.parametrize("timeout", [-1, float("inf"), float("nan")])
def test_invalid_timeout_cannot_start_termination(kernel, timeout):
    with jobs.PodJob.create() as job:
        with pytest.raises(ValueError):
            job.terminate_and_wait(timeout=timeout)
    assert not any(call[0] == "terminate" for call in kernel.calls)


def test_close_is_idempotent_and_operations_reject_closed_handle(kernel):
    job = jobs.PodJob.create()
    kernel.fail = "close"
    with pytest.raises(OSError):
        job.close()
    assert job._handle == 8001
    kernel.fail = ""
    job.close()
    kernel.calls.clear()
    job.close()
    with pytest.raises(ValueError, match="closed"):
        job.active_count()
    assert not kernel.calls


def test_nonwindows_load_fails_closed(monkeypatch):
    monkeypatch.setattr(pc, "IS_WINDOWS", False)
    with pytest.raises(OSError, match="require Windows"):
        jobs.PodJob.create()


@pytest.mark.skipif(not pc.IS_WINDOWS, reason="requires native Windows Job objects")
def test_native_job_survives_parent_exit_and_drains_descendant(tmp_path):
    """The parent disappears before observation; the kernel retains its child."""
    child_record = tmp_path / "child.pid"
    source = (
        "import subprocess,sys; from pathlib import Path; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(15)']); "
        "Path(sys.argv[1]).write_text(str(child.pid),encoding='utf-8')"
    )
    with jobs.PodJob.create() as owner:
        parent = subprocess.Popen(
            [getattr(sys, "_base_executable", sys.executable), "-c", source, str(child_record)],
            creationflags=pc.CREATE_SUSPENDED | pc.CREATE_NEW_PROCESS_GROUP,
            cwd=tmp_path,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        assigned = False
        try:
            owner.assign_suspended(int(parent._handle))
            assigned = True
            assert owner.contains(int(parent._handle))
            assert pc.resume_process_main_thread(parent.pid)
            parent.wait(timeout=10)
            assert child_record.exists()
            # Retain an independent recovery handle before closing the publisher's
            # handle, so an assertion or reopen failure cannot orphan the child.
            with jobs.PodJob.open_existing(owner.name) as recovery:
                try:
                    name = owner.name
                    owner.close()
                    with jobs.PodJob.open_existing(name) as consumer:
                        assert consumer.active_count() >= 1
                        consumer.terminate_and_wait(timeout=10)
                        assert consumer.active_count() == 0
                finally:
                    recovery.terminate_and_wait(timeout=10)
        finally:
            try:
                if assigned and owner._handle:
                    owner.terminate_and_wait(timeout=10)
            finally:
                if parent.poll() is None:
                    parent.kill()
                parent.wait(timeout=10)


@pytest.mark.skipif(not pc.IS_WINDOWS, reason="requires native Windows Job objects")
def test_native_membership_separates_jobs_and_nested_resource_limits(tmp_path):
    with jobs.PodJob.create() as owner, jobs.PodJob.create() as other:
        child = subprocess.Popen(
            [getattr(sys, "_base_executable", sys.executable), "-c", "import time; time.sleep(15)"],
            creationflags=pc.CREATE_SUSPENDED | pc.CREATE_NEW_PROCESS_GROUP,
            cwd=tmp_path,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            owner.assign_suspended(int(child._handle))
            assert owner.contains(int(child._handle))
            assert not other.contains(int(child._handle))
            assert pc.apply_job_limits(child.pid, max_procs=4, max_memory_bytes=256 * 1024 * 1024)
            assert pc.resume_process_main_thread(child.pid)
            with jobs.PodJob.open_existing(owner.name) as opened:
                assert opened.contains(int(child._handle))
                opened.terminate_and_wait(timeout=10)
            child.wait(timeout=10)
        finally:
            try:
                owner.terminate_and_wait(timeout=10)
            finally:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=10)


@pytest.mark.skipif(not pc.IS_WINDOWS, reason="requires native Windows security descriptors")
def test_native_job_dacl_grants_only_current_owner():
    kernel, advapi = jobs._load()
    advapi.GetSecurityInfo.argtypes = [
        C.c_void_p,
        C.c_int,
        jobs._DWORD,
        C.c_void_p,
        C.c_void_p,
        C.c_void_p,
        C.c_void_p,
        C.POINTER(C.c_void_p),
    ]
    advapi.GetSecurityInfo.restype = jobs._DWORD
    advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        C.c_void_p,
        jobs._DWORD,
        jobs._DWORD,
        C.POINTER(C.c_wchar_p),
        C.c_void_p,
    ]
    advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = jobs._BOOL
    with jobs.PodJob.create() as job:
        descriptor = C.c_void_p()
        # SE_KERNEL_OBJECT = 6; OWNER_SECURITY_INFORMATION | DACL = 5.
        assert (
            advapi.GetSecurityInfo(
                job._native_handle(), 6, 5, None, None, None, None, C.byref(descriptor)
            )
            == 0
        )
        try:
            text = C.c_wchar_p()
            assert advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                descriptor, 1, 5, C.byref(text), None
            )
            try:
                sddl = text.value
                sid = pc.current_user_sid()
                assert sid
                # Windows may serialize the current SID as an alias (e.g. LA).
                # Round-trip the exact owner-only expectation through the same API.
                expected_descriptor = C.c_void_p()
                assert advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                    f"O:{sid}D:P(A;;0x1f003f;;;{sid})",
                    1,
                    C.byref(expected_descriptor),
                    None,
                )
                try:
                    expected_text = C.c_wchar_p()
                    assert advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                        expected_descriptor, 1, 5, C.byref(expected_text), None
                    )
                    try:
                        assert sddl == expected_text.value
                    finally:
                        kernel.LocalFree(C.cast(expected_text, C.c_void_p))
                finally:
                    kernel.LocalFree(expected_descriptor)
            finally:
                kernel.LocalFree(C.cast(text, C.c_void_p))
        finally:
            kernel.LocalFree(descriptor)


@pytest.mark.skipif(not pc.IS_WINDOWS, reason="requires native Windows breakaway enforcement")
def test_native_job_refuses_breakaway_spawn(tmp_path):
    record = tmp_path / "breakaway.txt"
    source = """
import subprocess, sys
from pathlib import Path
try:
    child = subprocess.Popen(
        [sys.executable, '-c', 'import time; time.sleep(10)'],
        creationflags=subprocess.CREATE_BREAKAWAY_FROM_JOB,
    )
except OSError:
    Path(sys.argv[1]).write_text('blocked', encoding='utf-8')
else:
    try:
        Path(sys.argv[1]).write_text('escaped', encoding='utf-8')
    finally:
        child.kill()
        child.wait(timeout=5)
"""
    with jobs.PodJob.create() as job:
        child = subprocess.Popen(
            [getattr(sys, "_base_executable", sys.executable), "-c", source, str(record)],
            creationflags=pc.CREATE_SUSPENDED | pc.CREATE_NEW_PROCESS_GROUP,
            cwd=tmp_path,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            job.assign_suspended(int(child._handle))
            assert pc.resume_process_main_thread(child.pid)
            child.wait(timeout=10)
            assert record.read_text(encoding="utf-8") == "blocked"
        finally:
            try:
                job.terminate_and_wait(timeout=10)
            finally:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=10)


@pytest.mark.parametrize("failure", ["query", "short"])
def test_open_query_failure_closes_the_new_handle(kernel, failure):
    kernel.fail = failure
    kernel.short = failure == "short"
    with pytest.raises(OSError):
        jobs.PodJob.open_existing(jobs._NAME_PREFIX + "b" * 32)
    assert ("close", 8002) in kernel.calls


def test_native_prototypes_preserve_pointer_sized_handles(monkeypatch):
    kernel_names = (
        "CreateJobObjectW",
        "OpenJobObjectW",
        "AssignProcessToJobObject",
        "IsProcessInJob",
        "SetInformationJobObject",
        "QueryInformationJobObject",
        "TerminateJobObject",
        "OpenProcess",
        "WaitForSingleObject",
        "TerminateProcess",
        "CloseHandle",
        "LocalFree",
    )
    kernel = SimpleNamespace(**{name: SimpleNamespace() for name in kernel_names})
    advapi = SimpleNamespace(ConvertStringSecurityDescriptorToSecurityDescriptorW=SimpleNamespace())
    monkeypatch.setattr(pc, "IS_WINDOWS", True)
    monkeypatch.setattr(
        C, "WinDLL", lambda name, **_kw: kernel if name == "kernel32" else advapi, raising=False
    )
    assert jobs._load() == (kernel, advapi)
    assert kernel.CreateJobObjectW.restype is C.c_void_p
    assert kernel.OpenJobObjectW.restype is C.c_void_p
    assert kernel.AssignProcessToJobObject.argtypes == [C.c_void_p, C.c_void_p]
    assert kernel.QueryInformationJobObject.argtypes[-1] == C.POINTER(jobs._DWORD)
    assert all(hasattr(getattr(kernel, name), "restype") for name in kernel_names)


@pytest.mark.parametrize("outcome", ["owned", "reused", "unknown", "denied", "gone"])
def test_publisher_identity_never_confuses_access_failure_with_death(kernel, monkeypatch, outcome):
    monkeypatch.setattr(
        kernel,
        "OpenProcess",
        lambda *_a: 0 if outcome in {"denied", "gone"} else 9100,
        raising=False,
    )
    monkeypatch.setattr(jobs, "_last_error", lambda: 87 if outcome == "gone" else 5)
    monkeypatch.setattr(
        pc,
        "_windows_process_handle_identity",
        lambda *_a: (
            None if outcome == "unknown" else (4242, 200 if outcome == "reused" else 100, None)
        ),
    )
    if outcome in {"denied", "unknown"}:
        with pytest.raises(OSError):
            jobs.open_identity(4242, "100")
    else:
        handle = jobs.open_identity(4242, "100")
        assert handle == (9100 if outcome == "owned" else None)
        if handle is not None:
            jobs.close_identity(handle)
    assert (("close", 9100) in kernel.calls) is (outcome not in {"denied", "gone"})


@pytest.mark.parametrize("outcome", ["exited", "retired", "timeout", "query_error", "kill_error"])
def test_retirement_requires_a_signaled_exact_handle(kernel, monkeypatch, outcome):
    results = iter(
        {
            "exited": [0],
            "retired": [258, 0],
            "timeout": [258, 258],
            "query_error": [0xFFFFFFFF],
            "kill_error": [258, 258],
        }[outcome]
    )
    monkeypatch.setattr(kernel, "WaitForSingleObject", lambda *_a: next(results), raising=False)
    monkeypatch.setattr(
        kernel, "TerminateProcess", lambda *_a: outcome != "kill_error", raising=False
    )
    if outcome in {"timeout", "query_error", "kill_error"}:
        with pytest.raises(OSError):
            jobs.retire_identity(9100, timeout=0)
    else:
        jobs.retire_identity(9100, timeout=0)
