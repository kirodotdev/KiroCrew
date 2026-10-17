"""Coverage-raising unit tests for :mod:`kiro_crew.platform_compat`: the filesystem.

The filesystem helpers (forced rmtree, links, chmod), the Windows SIDs and the owner
lockdown, and executable and interpreter discovery.

Part of the suite ``test_platform_compat_coverage.py`` heads, and written its way: a
platform branch the Linux CI host never reaches is simulated by flipping
``IS_WINDOWS``/``IS_POSIX`` and faking the DLL entry points, every clock the product
consults is replaced, and nothing spawns a process or writes outside ``tmp_path``.
"""

from __future__ import annotations

import logging
import os
import stat
import subprocess
import sys
import types
from typing import Any

import pytest
from platform_compat_coverage_helpers import _POSIX_ONLY, _const, _fake_windows, _Fn

from kiro_crew import platform_compat as pc

# The whole-module skip ``test_platform_compat_coverage.py`` applies, for the reasons given
# there, and the libc-handle reset every test of that suite runs under (see conftest.py).
pytestmark = [
    _POSIX_ONLY,
    pytest.mark.usefixtures("fresh_darwin_libc_handles"),
]


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------


class TestRmtreeForce:
    def test_removes_a_tree_containing_read_only_files(self, tmp_path):
        # A git checkout is full of mode-444 loose objects, which Windows
        # refuses to unlink; the helper must clear the bit and still succeed.
        root = tmp_path / "tree"
        (root / "objects").mkdir(parents=True)
        obj = root / "objects" / "abc"
        obj.write_text("blob")
        obj.chmod(0o444)
        assert pc.rmtree_force(root) is True
        assert not root.exists()

    def test_reports_false_when_the_tree_survives(self, monkeypatch, tmp_path):
        # ignore_errors-style success reporting over a surviving tree is the bug
        # this return value exists to catch.
        root = tmp_path / "tree"
        root.mkdir()
        monkeypatch.setattr(pc.shutil, "rmtree", lambda *_a, **_k: None)
        assert pc.rmtree_force(root) is False

    def test_missing_tree_is_already_removed(self, tmp_path):
        assert pc.rmtree_force(tmp_path / "missing") is True

    def test_nested_missing_race_does_not_hide_a_surviving_root(self, monkeypatch, tmp_path):
        root = tmp_path / "tree"
        root.mkdir()

        def _nested_missing(*_args: Any, **_kwargs: Any) -> None:
            raise FileNotFoundError("nested entry disappeared")

        monkeypatch.setattr(pc.shutil, "rmtree", _nested_missing)
        assert pc.rmtree_force(root) is False
        assert root.exists()

    def test_delete_error_is_reported_without_raising(self, monkeypatch, tmp_path):
        root = tmp_path / "tree"
        root.mkdir()

        def _boom(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("denied")

        monkeypatch.setattr(pc.shutil, "rmtree", _boom)
        assert pc.rmtree_force(root) is False
        assert root.exists()

    def test_readonly_hook_retries_the_operation(self, tmp_path):
        victim = tmp_path / "ro.txt"
        victim.write_text("x")
        victim.chmod(0o444)
        pc._clear_readonly_and_retry(os.unlink, str(victim), OSError("denied"))
        assert not victim.exists()

    def test_readonly_hook_warns_when_the_retry_also_fails(self, tmp_path, caplog):
        victim = tmp_path / "ro.txt"
        victim.write_text("x")
        with caplog.at_level(logging.WARNING, logger=pc.logger.name):
            # rmdir on a file fails again on every platform.
            pc._clear_readonly_and_retry(os.rmdir, str(victim), OSError("denied"))
        assert any("Cannot remove" in r.getMessage() for r in caplog.records)

    def test_readonly_hook_does_not_retry_a_failed_open_or_listing(self, tmp_path, caplog):
        # rmtree reports a directory it could not open with func=os.open, which
        # takes more than a path: a blind retry raised TypeError out of the hook.
        directory = tmp_path / "locked"
        directory.mkdir()
        with caplog.at_level(logging.WARNING, logger=pc.logger.name):
            for func in (os.open, os.close, os.scandir, os.lstat):
                pc._clear_readonly_and_retry(func, str(directory), OSError("denied"))
        assert directory.is_dir()
        assert sum("Cannot remove" in r.getMessage() for r in caplog.records) == 4

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks and modes")
    def test_readonly_hook_never_chmods_through_a_link_on_posix(self, tmp_path):
        # rmtree hands the hook the failing entry's path; an agent-written tree
        # can make that a symlink to any file this process can reach.
        victim = tmp_path / "victim"
        victim.write_text("x")
        victim.chmod(0o640)
        link = tmp_path / "link"
        link.symlink_to(victim)

        # rmdir on a symlink fails, so only the hook's own handling is observed.
        pc._clear_readonly_and_retry(os.rmdir, str(link), OSError("denied"))
        assert stat.S_IMODE(victim.stat().st_mode) == 0o640

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
    def test_readonly_hook_leaves_a_failed_directory_listable_on_posix(self, tmp_path):
        directory = tmp_path / "busy"
        directory.mkdir(mode=0o700)
        (directory / "held").write_text("x")
        pc._clear_readonly_and_retry(os.rmdir, str(directory), OSError("not empty"))
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert [p.name for p in directory.iterdir()] == ["held"]


class TestLinkHelpers:
    def test_posix_symlink_round_trips(self, tmp_path):
        target = tmp_path / "target"
        target.mkdir()
        link = tmp_path / "link"
        pc.symlink_or_junction(target, link)
        assert pc.is_link_or_junction(link) is True
        pc.unlink_link_or_junction(link)
        assert not link.exists()
        # The target must survive removal of the link.
        assert target.is_dir()

    def test_windows_symlink_requests_a_directory_link(self, monkeypatch, tmp_path):
        # Without target_is_directory=True the link is a FILE-type symlink to a
        # directory, which is not traversable on Windows.
        seen: dict[str, Any] = {}

        def _symlink(src: str, dst: str, **kwargs: Any) -> None:
            seen.update({"src": src, "dst": dst, **kwargs})

        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc.os, "symlink", _symlink)
        pc.symlink_or_junction(tmp_path / "t", tmp_path / "l")
        assert seen["target_is_directory"] is True

    def test_junction_fallback_is_false_for_a_plain_directory(self, tmp_path):
        assert pc._is_junction_fallback(tmp_path) is False

    def test_junction_fallback_is_false_for_a_missing_path(self, tmp_path):
        assert pc._is_junction_fallback(tmp_path / "gone") is False

    def test_junction_fallback_recognises_a_mount_point_reparse_tag(self, monkeypatch, tmp_path):
        fake = types.SimpleNamespace(
            st_file_attributes=pc._FILE_ATTRIBUTE_REPARSE_POINT,
            st_reparse_tag=pc._IO_REPARSE_TAG_MOUNT_POINT,
        )
        monkeypatch.setattr(pc.os, "stat", lambda *_a, **_k: fake)
        assert pc._is_junction_fallback(tmp_path) is True

    def test_junction_fallback_rejects_another_reparse_tag(self, monkeypatch, tmp_path):
        fake = types.SimpleNamespace(
            st_file_attributes=pc._FILE_ATTRIBUTE_REPARSE_POINT,
            st_reparse_tag=0xA000000C,
        )
        monkeypatch.setattr(pc.os, "stat", lambda *_a, **_k: fake)
        assert pc._is_junction_fallback(tmp_path) is False

    def test_is_link_or_junction_uses_the_stdlib_probe_when_present(self, monkeypatch, tmp_path):
        monkeypatch.setattr(pc, "_ISJUNCTION", lambda _p: True)
        assert pc.is_link_or_junction(tmp_path) is True

    def test_is_link_or_junction_degrades_to_false_when_the_probe_raises(
        self, monkeypatch, tmp_path
    ):
        def _boom(_path: Any) -> bool:
            raise OSError("bad path")

        monkeypatch.setattr(pc, "_ISJUNCTION", _boom)
        assert pc.is_link_or_junction(tmp_path) is False

    def test_is_link_or_junction_uses_the_fallback_without_the_stdlib_probe(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(pc, "_ISJUNCTION", None)
        assert pc.is_link_or_junction(tmp_path) is False

    def test_unlink_removes_a_junction_with_rmdir(self, monkeypatch, tmp_path):
        # A junction is a directory reparse point: rmdir unlinks the junction
        # itself and never the target it points at.
        victim = tmp_path / "junction"
        victim.mkdir()
        calls: list[str] = []
        monkeypatch.setattr(pc, "_ISJUNCTION", lambda _p: True)
        # ``**_`` because this replaces the os module attribute for the whole test,
        # teardown included, and os.rmdir really takes dir_fd=.
        monkeypatch.setattr(pc.os, "rmdir", lambda p, **_: calls.append(str(p)))
        pc.unlink_link_or_junction(victim)
        assert calls == [str(victim)]

    def test_unlink_falls_through_to_unlink_for_a_real_file(self, tmp_path):
        plain = tmp_path / "plain.txt"
        plain.write_text("x")
        pc.unlink_link_or_junction(plain)
        assert not plain.exists()


class TestChmodHelpers:
    def test_chmod_safe_warns_instead_of_raising(self, monkeypatch, caplog, tmp_path):
        def _boom(*_a: Any, **_k: Any) -> None:
            raise OSError("read-only fs")

        monkeypatch.setattr(pc, "IS_POSIX", True)
        monkeypatch.setattr(pc.os, "chmod", _boom)
        with caplog.at_level(logging.WARNING, logger=pc.logger.name):
            pc.chmod_safe(tmp_path / "f", 0o600)
        assert any("Cannot set permissions" in r.getMessage() for r in caplog.records)

    def test_fchmod_safe_warns_instead_of_raising(self, monkeypatch, caplog):
        def _boom(*_a: Any, **_k: Any) -> None:
            raise OSError("bad fd")

        monkeypatch.setattr(pc, "IS_POSIX", True)
        monkeypatch.setattr(pc.os, "fchmod", _boom)
        with caplog.at_level(logging.WARNING, logger=pc.logger.name):
            pc.fchmod_safe(3, 0o600)
        assert any("Cannot set permissions" in r.getMessage() for r in caplog.records)

    def test_both_are_noops_on_windows(self, monkeypatch, tmp_path):
        monkeypatch.setattr(pc, "IS_POSIX", False)

        def _never(*_a: Any, **_k: Any) -> None:
            pytest.fail("POSIX mode bits must not be touched on Windows")

        monkeypatch.setattr(pc.os, "chmod", _never)
        monkeypatch.setattr(pc.os, "fchmod", _never)
        pc.chmod_safe(tmp_path, 0o600)
        pc.fchmod_safe(3, 0o600)


# ---------------------------------------------------------------------------
# Windows SIDs
# ---------------------------------------------------------------------------


def _token_dlls(
    *,
    sid: str = "S-1-5-21-1-2-3-1000",
    open_process: int = 55,
    open_token: bool = True,
    size: int = 64,
    read_info: bool = True,
    convert: bool = True,
) -> tuple[Any, Any, list[str]]:
    """Fake advapi32/kernel32 for the access-token SID read.

    Returns ``(kernel32, advapi32, closed)`` where ``closed`` records which
    handles the product closed — the pseudo-handle from ``GetCurrentProcess``
    must never be one of them.
    """

    closed: list[str] = []
    info_calls: list[int] = []

    def _open_process_token(_process: Any, _access: Any, token: Any) -> int:
        if not open_token:
            return 0
        token._obj.value = 4242
        return 1

    def _get_token_information(_token: Any, _cls: Any, buf: Any, _len: Any, out_size: Any) -> int:
        info_calls.append(1)
        if len(info_calls) == 1:
            # First call sizes the buffer and is expected to "fail".
            out_size._obj.value = size
            return 0
        return 1 if read_info else 0

    def _convert(_sid: Any, out: Any) -> int:
        if not convert:
            return 0
        out._obj.value = sid
        return 1

    kernel32 = types.SimpleNamespace(
        GetCurrentProcess=_const(1),
        OpenProcess=_const(open_process),
        CloseHandle=_Fn(lambda handle: closed.append(str(getattr(handle, "value", handle)))),
        LocalFree=_const(0),
    )
    advapi32 = types.SimpleNamespace(
        OpenProcessToken=_Fn(_open_process_token),
        GetTokenInformation=_Fn(_get_token_information),
        ConvertSidToStringSidW=_Fn(_convert),
    )
    return kernel32, advapi32, closed


class TestProcessTokenSid:
    def test_none_on_posix(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", True)
        assert pc._process_token_sid() is None

    def test_reads_the_sid_from_this_process_token(self, monkeypatch):
        kernel32, advapi32, closed = _token_dlls()
        _fake_windows(monkeypatch, kernel32=kernel32, advapi32=advapi32)
        assert pc._process_token_sid_unguarded() == "S-1-5-21-1-2-3-1000"
        # The GetCurrentProcess pseudo-handle must NOT be closed; only the token.
        assert closed == ["4242"]

    def test_reads_the_sid_of_another_pid_and_closes_its_handle(self, monkeypatch):
        kernel32, advapi32, closed = _token_dlls()
        _fake_windows(monkeypatch, kernel32=kernel32, advapi32=advapi32)
        assert pc._process_token_sid_unguarded(1234) == "S-1-5-21-1-2-3-1000"
        assert closed == ["4242", "55"]

    def test_unopenable_process_is_none(self, monkeypatch):
        kernel32, advapi32, _ = _token_dlls(open_process=0)
        _fake_windows(monkeypatch, kernel32=kernel32, advapi32=advapi32)
        assert pc._process_token_sid_unguarded(1234) is None

    def test_unopenable_token_is_none(self, monkeypatch):
        kernel32, advapi32, _ = _token_dlls(open_token=False)
        _fake_windows(monkeypatch, kernel32=kernel32, advapi32=advapi32)
        assert pc._process_token_sid_unguarded() is None

    def test_zero_sized_token_information_is_none(self, monkeypatch):
        kernel32, advapi32, _ = _token_dlls(size=0)
        _fake_windows(monkeypatch, kernel32=kernel32, advapi32=advapi32)
        assert pc._process_token_sid_unguarded() is None

    def test_failed_token_information_read_is_none(self, monkeypatch):
        kernel32, advapi32, _ = _token_dlls(read_info=False)
        _fake_windows(monkeypatch, kernel32=kernel32, advapi32=advapi32)
        assert pc._process_token_sid_unguarded() is None

    def test_failed_sid_conversion_is_none(self, monkeypatch):
        kernel32, advapi32, _ = _token_dlls(convert=False)
        _fake_windows(monkeypatch, kernel32=kernel32, advapi32=advapi32)
        assert pc._process_token_sid_unguarded() is None

    def test_a_non_sid_string_is_rejected(self, monkeypatch):
        kernel32, advapi32, _ = _token_dlls(sid="not-a-sid")
        _fake_windows(monkeypatch, kernel32=kernel32, advapi32=advapi32)
        assert pc._process_token_sid_unguarded() is None

    def test_missing_windll_is_none(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.delattr(pc.ctypes, "WinDLL", raising=False)
        assert pc._process_token_sid_unguarded() is None

    def test_guarded_wrapper_swallows_an_unexpected_error(self, monkeypatch):
        def _boom(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("unexpected")

        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "_process_token_sid_unguarded", _boom)
        assert pc._process_token_sid() is None

    def test_process_owner_sid_is_none_on_posix(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", True)
        assert pc.process_owner_sid(7) is None

    def test_process_owner_sid_reads_the_peer_token(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "_process_token_sid_unguarded", lambda pid: f"S-1-5-{pid}")
        assert pc.process_owner_sid(7) == "S-1-5-7"

    def test_process_owner_sid_is_unverifiable_on_failure(self, monkeypatch):
        def _boom(_pid: Any) -> Any:
            raise OSError("denied")

        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "_process_token_sid_unguarded", _boom)
        assert pc.process_owner_sid(7) is None


class TestRestrictToOwner:
    def test_posix_applies_owner_only_mode(self, tmp_path):
        secret = tmp_path / "token.key"
        secret.write_text("x")
        secret.chmod(0o644)
        pc.restrict_to_owner(secret)
        assert oct(secret.stat().st_mode)[-3:] == "600"

    def test_windows_refuses_a_half_configured_dacl(self, monkeypatch, tmp_path):
        # An Owner-Rights-only DACL would lock the caller out of a file created
        # by a different principal, so an unresolvable SID must fail loud.
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "current_user_sid", lambda: None)

        def _never(*_a: Any, **_k: Any) -> Any:
            pytest.fail("no DACL may be written without a resolved SID")

        monkeypatch.setattr(pc.windows_acl, "apply_owner_only", _never)
        with pytest.raises(OSError, match="cannot resolve current user SID"):
            pc.restrict_to_owner(tmp_path / "token.key")

    def test_windows_grants_both_owner_rights_and_the_caller(self, monkeypatch, tmp_path):
        seen: dict[str, Any] = {}

        def _apply(path: Any, *, inherit: bool, sids: Any, **_kw: Any) -> None:
            seen["path"] = path
            seen["inherit"] = inherit
            seen["sids"] = tuple(sids)

        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "current_user_sid", lambda: "S-1-5-21-3")
        monkeypatch.setattr(pc.windows_acl, "apply_owner_only", _apply)

        pc.restrict_to_owner(tmp_path / "token.key")

        # Bare SIDs, both grants present, file shape (no inheritance).
        assert seen["sids"] == ("S-1-3-4", "S-1-5-21-3"), seen
        assert seen["inherit"] is False, seen

    def test_windows_does_not_duplicate_the_owner_rights_grant(self, monkeypatch, tmp_path):
        seen: dict[str, Any] = {}

        def _apply(path: Any, *, inherit: bool, sids: Any, **_kw: Any) -> None:
            seen["sids"] = tuple(sids)

        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "current_user_sid", lambda: pc._OWNER_RIGHTS_SID.removeprefix("*"))
        monkeypatch.setattr(pc.windows_acl, "apply_owner_only", _apply)
        pc.restrict_to_owner(tmp_path / "token.key")
        assert seen["sids"].count("S-1-3-4") == 1, seen

    @_POSIX_ONLY
    def test_windows_raises_when_the_dacl_write_fails(self, monkeypatch, tmp_path):
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "current_user_sid", lambda: "S-1-5-21-3")

        def _boom(*_a: Any, **_k: Any) -> None:
            raise pc.windows_acl.AclWriteFailed("SetNamedSecurityInfoW failed (error 5)")

        monkeypatch.setattr(pc.windows_acl, "apply_owner_only", _boom)
        with pytest.raises(OSError, match="owner-only DACL could not be applied"):
            pc.restrict_to_owner(tmp_path / "token.key")

    @_POSIX_ONLY
    def test_windows_raises_when_the_security_api_is_unavailable(self, monkeypatch, tmp_path):
        def _boom(*_a: Any, **_k: Any) -> Any:
            raise pc.windows_acl.AclUnavailable("cannot load the Windows security API")

        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "current_user_sid", lambda: "S-1-5-21-3")
        monkeypatch.setattr(pc.windows_acl, "apply_owner_only", _boom)
        with pytest.raises(OSError, match="owner-only DACL could not be applied"):
            pc.restrict_to_owner(tmp_path / "token.key")

    def test_make_owner_only_dir_warns_instead_of_raising(self, monkeypatch, caplog, tmp_path):
        target = tmp_path / "secrets"

        def _boom(*_a: Any, **_k: Any) -> None:
            raise OSError("no perms")

        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        # The POSIX tightening now goes through restrict_dir_to_owner, which
        # calls os.chmod rather than Path.chmod -- patching the old seam would
        # intercept nothing, the raise would never fire, and this test would
        # pass without ever exercising the warn-and-continue handler.
        monkeypatch.setattr(pc.os, "chmod", _boom)
        with caplog.at_level(logging.WARNING, logger=pc.logger.name):
            pc.make_owner_only_dir(target)
        assert target.is_dir()
        assert any("owner-only" in r.getMessage() for r in caplog.records)

    def test_make_owner_only_dir_uses_the_dacl_on_windows(self, monkeypatch, tmp_path):
        called: list[Any] = []
        wrong: list[Any] = []
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        # Must be the DIRECTORY helper: restrict_to_owner's grants are
        # file-shaped (no (OI)(CI)), so files created inside would not inherit
        # the lockdown.
        monkeypatch.setattr(pc, "restrict_dir_to_owner", lambda p: called.append(p))
        monkeypatch.setattr(pc, "restrict_to_owner", lambda p: wrong.append(p))
        target = tmp_path / "secrets"
        pc.make_owner_only_dir(target)
        assert called and str(called[0]) == str(target)
        assert wrong == []


# ---------------------------------------------------------------------------
# Executable / interpreter discovery
# ---------------------------------------------------------------------------


class TestExecutableDiscovery:
    def test_windows_target_accepts_known_script_suffixes(self, tmp_path):
        hook = tmp_path / "pre.ps1"
        hook.write_text("")
        # No execute bit is set: on Windows there is none, so an X_OK check
        # would silently disable every hook.
        assert pc.is_executable_file(hook, platform_name="win32") is True

    def test_windows_target_rejects_an_unknown_suffix(self, tmp_path):
        hook = tmp_path / "notes.md"
        hook.write_text("")
        assert pc.is_executable_file(hook, platform_name="win32") is False

    def test_posix_target_accepts_any_regular_file_from_a_windows_host(self, monkeypatch, tmp_path):
        hook = tmp_path / "pre.sh"
        hook.write_text("")
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        assert pc.is_executable_file(hook, platform_name="linux") is True

    def test_a_directory_is_not_executable(self, tmp_path):
        assert pc.is_executable_file(tmp_path) is False

    def test_an_os_error_degrades_to_false(self, monkeypatch, tmp_path):
        def _boom(*_a: Any, **_k: Any) -> Any:
            raise OSError("stat failed")

        monkeypatch.setattr(pc.os.path, "isfile", _boom)
        assert pc.is_executable_file(tmp_path / "x") is False

    def test_store_stub_detection_is_windows_only(self, monkeypatch):
        stub = r"C:\Users\u\AppData\Local\Microsoft\WindowsApps\python.exe"
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        assert pc._is_windows_store_python_stub(stub) is False
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        assert pc._is_windows_store_python_stub(stub) is True
        assert pc._is_windows_store_python_stub(r"C:\Python312\python.exe") is False


class TestFindPythonInterpreter:
    @staticmethod
    def _resolve(monkeypatch: pytest.MonkeyPatch, table: dict[str, str | None]) -> None:
        monkeypatch.setattr(pc.shutil, "which", lambda name: table.get(name))

    def test_skips_build_and_brazil_interpreters(self, monkeypatch):
        self._resolve(
            monkeypatch,
            {
                "python3.12": "/x/build/private/python3.12",
                "python3": "/brazil-path/python3",
                "python3.13": "/usr/bin/python3.13",
            },
        )
        monkeypatch.setattr(pc.subprocess, "check_output", lambda *_a, **_k: "3.13\n")
        assert pc.find_python_interpreter() == "/usr/bin/python3.13"

    def test_skips_the_microsoft_store_stub(self, monkeypatch):
        stub = r"C:\Users\u\AppData\Local\Microsoft\WindowsApps\python.exe"
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        self._resolve(monkeypatch, {"python": stub, "python3": r"C:\Python312\python.exe"})
        monkeypatch.setattr(pc.subprocess, "check_output", lambda *_a, **_k: "3.12\n")
        assert pc.find_python_interpreter() == r"C:\Python312\python.exe"

    def test_rejects_an_interpreter_below_the_floor(self, monkeypatch):
        self._resolve(monkeypatch, {"python3": "/usr/bin/python3"})
        monkeypatch.setattr(pc.subprocess, "check_output", lambda *_a, **_k: "3.9\n")
        assert pc.find_python_interpreter() is None

    def test_a_probe_failure_falls_through(self, monkeypatch):
        def _boom(*_a: Any, **_k: Any) -> Any:
            raise subprocess.SubprocessError("timeout")

        self._resolve(monkeypatch, {"python3": "/usr/bin/python3"})
        monkeypatch.setattr(pc.subprocess, "check_output", _boom)
        assert pc.find_python_interpreter() is None

    def test_the_reject_predicate_falls_through_rather_than_aborting(self, monkeypatch):
        # A single unusable interpreter must not short-circuit the whole search.
        # POSIX order is 3.12, python3, 3.13 — vetoing 3.12 must land on
        # python3, not give up.
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        self._resolve(
            monkeypatch,
            {"python3.12": "/usr/bin/python3.12", "python3": "/usr/bin/python3"},
        )
        monkeypatch.setattr(pc.subprocess, "check_output", lambda *_a, **_k: "3.12\n")
        picked = pc.find_python_interpreter(reject=lambda p: p.endswith("3.12"))
        assert picked == "/usr/bin/python3"

    def test_nothing_resolvable_is_none(self, monkeypatch):
        self._resolve(monkeypatch, {})
        assert pc.find_python_interpreter() is None
