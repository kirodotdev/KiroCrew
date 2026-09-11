"""Lost ancestry cannot turn an isolated process into a host owner.

All process trees and kernel responses here are synthetic. No process detaches,
enters a namespace, starts a gateway or mints an owner token.
"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from kiro_crew import member_memory_auth as auth
from kiro_crew import platform_compat as pc


@pytest.fixture
def provenance(tmp_path, monkeypatch):
    parent = {42: 1, 45: 42}
    namespaces = {os.getpid(): "host", 42: "runtime", 45: "runtime"}
    monkeypatch.setattr(auth, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(auth, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(auth, "private_memory_boundaries_active", lambda: True)
    monkeypatch.setattr(auth, "_request_peer_pid", lambda request: 45)
    monkeypatch.setattr(pc, "get_ppid", lambda pid: parent.get(pid, 1))
    monkeypatch.setattr(pc, "get_process_start_id", lambda pid: f"start-{pid}")
    monkeypatch.setattr(
        pc, "process_namespaces_match", lambda pid, other: namespaces[pid] == namespaces[other]
    )
    request = {"internal_auth": True}
    request = SimpleNamespace(get=request.get, headers={})
    return SimpleNamespace(parent=parent, namespaces=namespaces, request=request, home=tmp_path)


def test_private_descendant_losing_ancestry_remains_unverified(provenance):
    auth.publish_member_session_pid(42, "member:alice", memory_store="member-alice")
    assert auth.memory_request_identity(provenance.request) == ("member:alice", True)
    assert not auth.local_owner_bootstrap_allowed(provenance.request)

    provenance.parent[45] = 1
    assert auth.protected_member_session_for_pid(45) is None
    assert auth.memory_request_identity(provenance.request) == (None, False)
    assert not auth.local_owner_bootstrap_allowed(provenance.request)
    assert auth.issue_member_session_proof("member:alice", 45) == ""

    # No in-process ancestry cache or surviving launcher is needed for refusal.
    auth._binding_path(42, provenance.home).unlink()
    assert auth.memory_request_identity(provenance.request) == (None, False)
    assert not auth.local_owner_bootstrap_allowed(provenance.request)


def test_unowned_host_keeps_v1_and_owner_bootstrap(provenance):
    provenance.parent[45] = 1
    provenance.namespaces[45] = "host"
    assert auth.memory_request_identity(provenance.request) == (None, True)
    assert auth.local_owner_bootstrap_allowed(provenance.request)


def test_published_v1_runtime_keeps_v1_without_owner_authority(provenance):
    auth.publish_member_session_pid(42, "dashboard:one", memory_store="")
    assert auth.protected_member_session_for_pid(45) is None
    assert auth.memory_request_identity(provenance.request) == (None, True)
    assert not auth.local_owner_bootstrap_allowed(provenance.request)
    assert auth.issue_member_session_proof("dashboard:one", 45) == ""

    auth.publish_member_session_pid(42, "dashboard:two", memory_store="")
    assert auth.memory_request_identity(provenance.request) == (None, True)


def test_nested_namespace_cannot_borrow_a_v1_ancestor(provenance):
    auth.publish_member_session_pid(42, "dashboard:one", memory_store="")
    provenance.namespaces[45] = "different-runtime"
    assert auth.memory_request_identity(provenance.request) == ("", False)
    assert not auth.local_owner_bootstrap_allowed(provenance.request)


@pytest.mark.parametrize("published", [False, True])
def test_unknown_namespace_identity_never_means_global(provenance, monkeypatch, published):
    if published:
        auth.publish_member_session_pid(42, "dashboard:one", memory_store="")
    monkeypatch.setattr(pc, "process_namespaces_match", lambda *args: None)
    assert auth.memory_request_identity(provenance.request)[1] is False
    assert not auth.local_owner_bootstrap_allowed(provenance.request)


@pytest.mark.parametrize("sandboxed,expected", [(True, False), (False, True), (None, False)])
def test_macos_unknown_ancestry_needs_positive_unsandboxed_state(
    provenance, monkeypatch, sandboxed, expected
):
    monkeypatch.setattr(auth, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(pc, "process_is_sandboxed", lambda pid: sandboxed)
    provenance.parent[45] = 1
    assert auth.memory_request_identity(provenance.request) == (None, expected)
    assert auth.local_owner_bootstrap_allowed(provenance.request) is expected


@pytest.mark.parametrize("allowed", [True, False, None])
def test_macos_global_ancestor_requires_current_peer_global_permission(
    provenance, monkeypatch, allowed
):
    monkeypatch.setattr(auth, "sys", SimpleNamespace(platform="darwin"))
    # V1 and private peers are both sandboxed. Sandbox presence cannot be used
    # as either a Global grant or a blanket refusal of an ordinary V1 runtime.
    monkeypatch.setattr(pc, "process_is_sandboxed", lambda pid: True)
    permission = Mock(return_value=allowed)
    monkeypatch.setattr(pc, "process_can_read_under_sandbox", permission)
    auth.publish_member_session_pid(42, "dashboard:global", memory_store="")

    expected = (None, True) if allowed else ("", False)
    assert auth.memory_request_identity(provenance.request) == expected
    assert permission.call_args.args == (45, provenance.home.resolve() / "memory.db")
    assert not auth.local_owner_bootstrap_allowed(provenance.request)
    assert not auth.issue_member_session_proof("dashboard:global", 45)


def test_macos_private_binding_precedes_global_permission_and_lost_record_refuses(
    provenance, monkeypatch
):
    monkeypatch.setattr(auth, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(pc, "process_is_sandboxed", lambda pid: True)
    permission = Mock(return_value=False)
    monkeypatch.setattr(pc, "process_can_read_under_sandbox", permission)
    provenance.parent[42] = 46
    auth.publish_member_session_pid(46, "dashboard:global", memory_store="")
    auth.publish_member_session_pid(42, "member:alice", memory_store="member-alice")

    assert auth.memory_request_identity(provenance.request) == ("member:alice", True)
    permission.assert_not_called()
    auth._binding_path(42, provenance.home).unlink()
    assert auth.memory_request_identity(provenance.request) == ("", False)
    assert not auth.local_owner_bootstrap_allowed(provenance.request)


@pytest.mark.parametrize("platform,expected", [("win32", True), ("unsupported", False)])
def test_native_windows_v1_and_unknown_platform_contract(
    provenance, monkeypatch, platform, expected
):
    monkeypatch.setattr(auth, "sys", SimpleNamespace(platform=platform))
    provenance.parent[45] = 1
    assert auth.memory_request_identity(provenance.request) == (None, expected)
    assert auth.local_owner_bootstrap_allowed(provenance.request) is expected
    if platform == "win32":
        assert not auth.private_memory_execution_supported()


@pytest.mark.parametrize("different", [None, "user", "mnt"])
def test_namespace_comparison_checks_both_kernel_identities(monkeypatch, different):
    monkeypatch.setattr(pc, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(pc, "get_process_start_id", lambda pid: f"start-{pid}")
    reads = []

    def path(name):
        reads.append(name)
        inode = 2 if name == f"/proc/45/ns/{different}" else 1
        return SimpleNamespace(stat=lambda: SimpleNamespace(st_dev=4, st_ino=inode))

    monkeypatch.setattr(pc, "Path", path)
    assert pc.process_namespaces_match(45, 42) is (different is None)
    assert set(reads) == {f"/proc/{pid}/ns/{ns}" for pid in (45, 42) for ns in ("user", "mnt")}


@pytest.mark.parametrize("failure", ["denied", "missing", "recycled"])
def test_namespace_probe_failure_is_unknown(monkeypatch, failure):
    monkeypatch.setattr(pc, "sys", SimpleNamespace(platform="linux"))
    starts = iter(["before", "reference", "after", "reference"])
    monkeypatch.setattr(
        pc, "get_process_start_id", lambda pid: next(starts) if failure == "recycled" else "start"
    )

    def stat():
        if failure == "denied":
            raise PermissionError
        if failure == "missing":
            raise FileNotFoundError
        return SimpleNamespace(st_dev=4, st_ino=1)

    monkeypatch.setattr(pc, "Path", lambda name: SimpleNamespace(stat=stat))
    assert pc.process_namespaces_match(45, 42) is None


@pytest.mark.parametrize("platform", ["win32", "darwin", "unsupported"])
def test_namespace_probe_is_unknown_on_other_platforms(monkeypatch, platform):
    monkeypatch.setattr(pc, "sys", SimpleNamespace(platform=platform))
    assert pc.process_namespaces_match(45, 42) is None


@pytest.mark.parametrize("result,expected", [(0, False), (1, True), (-1, None), (2, None)])
def test_seatbelt_probe_queries_only_and_checks_its_result(monkeypatch, result, expected):
    monkeypatch.setattr(pc, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(pc, "get_process_start_id", lambda pid: "start")
    check = Mock(return_value=result)
    load = Mock(return_value=SimpleNamespace(sandbox_check=check))
    monkeypatch.setattr(pc.ctypes, "CDLL", load)
    assert pc.process_is_sandboxed(45) is expected
    load.assert_called_once_with("/usr/lib/libsandbox.dylib", use_errno=True)
    check.assert_called_once_with(45, None, 0)


@pytest.mark.parametrize("failure", ["unavailable", "recycled"])
def test_seatbelt_probe_unavailability_is_unknown(monkeypatch, failure):
    monkeypatch.setattr(pc, "sys", SimpleNamespace(platform="darwin"))
    starts = iter(["before", "after"])
    monkeypatch.setattr(pc, "get_process_start_id", lambda pid: next(starts))
    load = Mock(return_value=SimpleNamespace(sandbox_check=Mock(return_value=0)))
    if failure == "unavailable":
        load.side_effect = OSError
    monkeypatch.setattr(pc.ctypes, "CDLL", load)
    assert pc.process_is_sandboxed(45) is None


@pytest.mark.parametrize("platform", ["win32", "linux", "unsupported"])
def test_seatbelt_probe_is_unknown_on_other_platforms(monkeypatch, platform):
    monkeypatch.setattr(pc, "sys", SimpleNamespace(platform=platform))
    assert pc.process_is_sandboxed(45) is None


@pytest.fixture
def seatbelt_read_probe(monkeypatch):
    monkeypatch.setattr(pc, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(pc, "get_process_start_id", lambda pid: "start")
    check = Mock(return_value=0)
    library = SimpleNamespace(sandbox_check=check)
    load = Mock(return_value=library)
    integer = Mock(in_dll=Mock(return_value=SimpleNamespace(value=0x100)))
    # Replace this module's ctypes binding, not the interpreter-wide ctypes
    # classes that other fixtures and real process helpers may be using.
    monkeypatch.setattr(
        pc, "ctypes", SimpleNamespace(CDLL=load, c_int=integer, c_char_p=ctypes.c_char_p)
    )
    return SimpleNamespace(check=check, library=library, load=load, integer=integer)


@pytest.mark.parametrize("result,expected", [(0, True), (1, False), (-1, None), (2, None)])
def test_seatbelt_read_probe_uses_path_filter_and_variadic_abi(
    tmp_path, seatbelt_read_probe, result, expected
):
    probe = seatbelt_read_probe
    probe.check.return_value = result
    target = tmp_path.resolve() / "memory.db"
    assert pc.process_can_read_under_sandbox(45, target) is expected
    probe.load.assert_called_once_with("/usr/lib/libsandbox.dylib", use_errno=True)
    probe.integer.in_dll.assert_called_once_with(probe.library, "SANDBOX_CHECK_NO_REPORT")
    assert probe.check.argtypes == [probe.integer, ctypes.c_char_p, probe.integer]
    assert probe.check.restype is probe.integer
    pid, operation, flags, path = probe.check.call_args.args
    assert (pid, operation, flags, path.value) == (
        45,
        b"file-read-data",
        0x101,
        os.fsencode(target),
    )
    assert not target.exists()


@pytest.mark.parametrize("failure", ["library", "symbol", "query", "recycled", "no_start"])
def test_seatbelt_read_probe_unknown_never_grants(
    tmp_path, monkeypatch, seatbelt_read_probe, failure
):
    probe = seatbelt_read_probe
    if failure == "library":
        probe.load.side_effect = OSError
    elif failure == "symbol":
        probe.integer.in_dll.side_effect = ValueError
    elif failure == "query":
        probe.check.side_effect = OSError
    else:
        starts = iter([None, None] if failure == "no_start" else ["before", "after"])
        monkeypatch.setattr(pc, "get_process_start_id", lambda pid: next(starts))
    assert pc.process_can_read_under_sandbox(45, tmp_path.resolve() / "memory.db") is None


@pytest.mark.parametrize("platform", ["linux", "win32", "unsupported"])
def test_seatbelt_read_probe_other_platforms_are_unknown(
    tmp_path, monkeypatch, seatbelt_read_probe, platform
):
    monkeypatch.setattr(pc, "sys", SimpleNamespace(platform=platform))
    assert pc.process_can_read_under_sandbox(45, tmp_path.resolve() / "memory.db") is None
    seatbelt_read_probe.load.assert_not_called()


@pytest.mark.parametrize("pid", [True, 0, 1, -1, "45"])
def test_seatbelt_read_probe_rejects_invalid_pid(tmp_path, seatbelt_read_probe, pid):
    assert pc.process_can_read_under_sandbox(pid, tmp_path.resolve() / "memory.db") is None
    seatbelt_read_probe.load.assert_not_called()


def test_seatbelt_read_probe_requires_absolute_path(seatbelt_read_probe):
    assert pc.process_can_read_under_sandbox(45, Path("memory.db")) is None
    seatbelt_read_probe.load.assert_not_called()
