"""The Windows pinned-directory containment check, driven through stand-ins.

``PinnedDirectory`` deletes and descends on Windows only after the kernel's final path
of the opened entry proves it sits in the pinned directory. These tests drive the three
helpers behind that with stand-ins for ``kernel32`` and ``msvcrt``, so they run on every
host, including the ones coverage is measured on; the real Windows behaviour is
exercised in ``test_trash_pinned_sibling_removal.py``.
"""

from __future__ import annotations

import errno
import os
import types
from pathlib import Path
from typing import Any

import pytest
from platform_compat_coverage_helpers import _Fn

from kiro_crew import platform_compat as pc
from kiro_crew import platform_compat_links as pcl


def _kernel32(monkeypatch: pytest.MonkeyPatch, **entry_points: Any) -> None:
    """Make ``ctypes.WinDLL("kernel32")`` answer with *entry_points*."""
    namespace = types.SimpleNamespace(**entry_points)
    monkeypatch.setattr(pcl.ctypes, "WinDLL", lambda name, **_kw: namespace, raising=False)
    # Through the facade: ``msvcrt`` is bound by more than one owner module.
    monkeypatch.setattr(
        pc, "msvcrt", types.SimpleNamespace(get_osfhandle=lambda fd: fd), raising=False
    )


def _final_path_returning(path: str) -> _Fn:
    def impl(_handle: Any, buffer: Any, _size: Any, _flags: Any) -> int:
        if not path:
            return 0
        buffer.value = path
        return len(path)

    return _Fn(impl)


class TestFinalPath:
    @pytest.mark.parametrize(
        ("kernel", "expected"),
        [
            ("\\\\?\\C:\\store\\trash", "C:\\store\\trash"),
            ("\\\\?\\UNC\\host\\share\\trash", "\\\\host\\share\\trash"),
            ("\\Device\\HarddiskVolume3\\store", "\\Device\\HarddiskVolume3\\store"),
            ("", None),
        ],
    )
    def test_the_prefix_is_stripped_and_a_failure_reads_as_none(
        self, monkeypatch: pytest.MonkeyPatch, kernel: str, expected: str | None
    ) -> None:
        _kernel32(monkeypatch, GetFinalPathNameByHandleW=_final_path_returning(kernel))
        assert pcl._win_final_path(3) == expected


class TestCheckWithin:
    def _finals(self, monkeypatch: pytest.MonkeyPatch, table: dict[tuple[int, int], str]) -> None:
        monkeypatch.setattr(pcl, "_win_final_path", lambda fd, flags=0: table.get((fd, flags)))

    def test_a_direct_child_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._finals(monkeypatch, {(1, 0): r"C:\t", (2, 0): r"C:\t\a"})
        pcl._win_check_within(1, 2, "a")

    def test_an_entry_reached_through_a_redirect_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._finals(monkeypatch, {(1, 0): r"C:\t", (2, 0): r"C:\outside\a"})
        with pytest.raises(OSError) as raised:
            pcl._win_check_within(1, 2, "a")
        assert raised.value.errno == errno.EXDEV

    def test_case_is_not_folded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._finals(monkeypatch, {(1, 0): r"C:\Trash", (2, 0): r"C:\trash\a"})
        with pytest.raises(OSError):
            pcl._win_check_within(1, 2, "a")

    def test_the_nt_name_is_used_where_there_is_no_dos_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._finals(monkeypatch, {(1, 2): r"\Device\V\t", (2, 2): r"\Device\V\t\a"})
        pcl._win_check_within(1, 2, "a")

    def test_an_unreadable_pair_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._finals(monkeypatch, {})
        with pytest.raises(OSError):
            pcl._win_check_within(1, 2, "a")


class TestUnlinkWithin:
    @pytest.fixture
    def entry(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[int]:
        """Open a real file as the 'entry handle' and record every disposition call."""
        target = tmp_path / "entry"
        target.write_text("x")
        monkeypatch.setattr(
            pcl, "_win_open_without_following", lambda path, access: os.open(target, os.O_RDONLY)
        )
        monkeypatch.setattr(pcl, "_win_check_within", lambda directory_fd, fd, path: None)
        monkeypatch.setattr(pcl, "win_fd_is_link", lambda fd: False)
        monkeypatch.setattr(pcl.ctypes, "get_last_error", lambda: 5, raising=False)
        monkeypatch.setattr(
            pcl.ctypes, "WinError", lambda code: OSError(code, "refused"), raising=False
        )
        calls: list[int] = []

        def disposition(_handle: Any, info_class: int, *_rest: Any) -> int:
            calls.append(info_class)
            return 1

        _kernel32(monkeypatch, SetFileInformationByHandle=_Fn(disposition))
        return calls

    def _unlink_with_attributes(
        self, monkeypatch: pytest.MonkeyPatch, value: int, *, directory: bool = False
    ) -> None:
        """Call the helper while the entry handle reports *value* as its attributes.

        Scoped to the call: a patched ``os.fstat`` left in place would also answer for
        pytest's own teardown of ``tmp_path``.
        """
        with monkeypatch.context() as patched:
            patched.setattr(
                pcl.os, "fstat", lambda fd: types.SimpleNamespace(st_file_attributes=value)
            )
            pcl._win_unlink_within(1, "a", directory=directory)

    def test_a_file_is_deleted_through_its_handle(self, entry: list[int]) -> None:
        pcl._win_unlink_within(1, "a")
        assert entry == [4], "FileDispositionInfo, issued once"

    def test_a_real_directory_is_not_unlinked(
        self, entry: list[int], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(IsADirectoryError):
            self._unlink_with_attributes(monkeypatch, 0x10)
        assert entry == []

    def test_rmdir_refuses_a_link(self, entry: list[int], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pcl, "win_fd_is_link", lambda fd: True)
        with pytest.raises(NotADirectoryError):
            self._unlink_with_attributes(monkeypatch, 0x10 | 0x400, directory=True)
        assert entry == []

    def test_rmdir_takes_a_non_link_reparse_directory(
        self, entry: list[int], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._unlink_with_attributes(monkeypatch, 0x10 | 0x400, directory=True)
        assert entry == [4]

    def test_a_refused_disposition_raises(
        self, entry: list[int], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _kernel32(monkeypatch, SetFileInformationByHandle=_Fn(lambda *_args: 0))
        with pytest.raises(OSError):
            pcl._win_unlink_within(1, "a")
