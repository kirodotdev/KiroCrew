"""``fd_real_path`` reads a descriptor's real path on each platform, and fails closed.

The primitive lives in ``kiro_crew.pinned_fs_primitives`` and is reached through the
``kiro_crew.pinned_fs`` facade. Linux answers through ``/proc/self/fd``, which the rest of
the suite exercises for real. These tests drive the other two routes on any host:
``GetFinalPathNameByHandleW`` on Windows, with its ``\\\\?\\`` and ``\\\\?\\UNC\\``
prefixes, and ``fcntl.F_GETPATH`` on macOS. They stand in only the platform calls,
through ``sys.modules`` and the facade's ``os`` binding, so the function body runs as
written. Every failure must come back as ``None`` and never as a guessed path.
"""

from __future__ import annotations

import os
import sys
import types

import pytest

from kiro_crew import pinned_fs

_DOS_PREFIX = "\\\\?\\"
_UNC_PREFIX = "\\\\?\\UNC\\"


class _PlatformOs:
    """``os`` as seen on another platform: its own ``name``, and the rest from ``os``."""

    def __init__(self, name: str, *, readlink_error: bool = False) -> None:
        self.name = name
        self._readlink_error = readlink_error

    def readlink(self, path: str) -> str:
        if self._readlink_error:
            raise OSError("no /proc on this platform")
        return os.readlink(path)

    def __getattr__(self, attr: str) -> object:
        return getattr(os, attr)


def _fake_ctypes(answer: str | None, *, length: int | None = None, has_windll: bool = True):
    """A ``ctypes`` whose ``GetFinalPathNameByHandleW`` writes *answer* and returns *length*.

    *length* defaults to the answer's length; ``None`` as *answer* reports a failure (0).
    """
    module = types.ModuleType("ctypes")
    calls: list[tuple[object, ...]] = []

    class _Buffer:
        def __init__(self, size: int) -> None:
            self.size = size
            self.value = ""

        def __len__(self) -> int:
            return self.size

    def get_final_path(handle, buffer, size, flags):
        calls.append((handle.value, size, flags))
        if answer is None:
            return 0
        buffer.value = answer
        return len(answer) if length is None else length

    class _Kernel32:
        GetFinalPathNameByHandleW = staticmethod(get_final_path)

    module.c_void_p = lambda value: types.SimpleNamespace(value=value)
    module.c_wchar_p = object()
    module.c_uint32 = object()
    module.create_unicode_buffer = _Buffer
    if has_windll:
        module.WinDLL = lambda name, use_last_error=False: _Kernel32()
    module.calls = calls
    return module


def _fake_msvcrt(*, handle: int = 4242, fails: bool = False) -> types.ModuleType:
    module = types.ModuleType("msvcrt")

    def get_osfhandle(fd: int) -> int:
        if fails:
            raise OSError("bad file descriptor")
        return handle + fd

    module.get_osfhandle = get_osfhandle
    return module


@pytest.fixture
def on_windows(monkeypatch: pytest.MonkeyPatch):
    """Run ``fd_real_path`` as on Windows; return a setter for the platform modules."""
    monkeypatch.setattr(pinned_fs, "os", _PlatformOs("nt"))

    def install(ctypes_module: types.ModuleType, msvcrt_module: types.ModuleType) -> None:
        monkeypatch.setitem(sys.modules, "ctypes", ctypes_module)
        monkeypatch.setitem(sys.modules, "msvcrt", msvcrt_module)

    return install


@pytest.mark.parametrize(
    ("final_path", "expected"),
    [
        (_DOS_PREFIX + "D:\\data\\note.txt", "D:\\data\\note.txt"),
        (_UNC_PREFIX + "server\\share\\note.txt", "\\\\server\\share\\note.txt"),
        ("D:\\data\\note.txt", "D:\\data\\note.txt"),
    ],
    ids=["dos-prefix", "unc-prefix", "no-prefix"],
)
def test_windows_returns_the_handles_final_path_without_its_prefix(
    on_windows, final_path: str, expected: str
) -> None:
    ctypes_module = _fake_ctypes(final_path)
    on_windows(ctypes_module, _fake_msvcrt())
    assert pinned_fs.fd_real_path(7) == expected
    # The handle is the descriptor's own, asked for once, with no flags.
    assert ctypes_module.calls == [(4242 + 7, 32768, 0)]


@pytest.mark.parametrize(
    ("ctypes_module", "msvcrt_module"),
    [
        (_fake_ctypes(None), _fake_msvcrt()),
        (_fake_ctypes("D:\\x", length=32768), _fake_msvcrt()),
        (_fake_ctypes("D:\\x", has_windll=False), _fake_msvcrt()),
        (_fake_ctypes("D:\\x"), _fake_msvcrt(fails=True)),
    ],
    ids=["call-failed", "truncated", "no-windll", "no-handle"],
)
def test_windows_fails_closed_when_the_final_path_cannot_be_read(
    on_windows, ctypes_module: types.ModuleType, msvcrt_module: types.ModuleType
) -> None:
    on_windows(ctypes_module, msvcrt_module)
    assert pinned_fs.fd_real_path(7) is None


def test_macos_reads_the_path_fcntl_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    fcntl_module = types.ModuleType("fcntl")
    seen: list[tuple[int, int, int]] = []

    def fcntl(fd: int, cmd: int, buf: bytes) -> bytes:
        seen.append((fd, cmd, len(buf)))
        return b"/Volumes/data/note.txt\x00" + b"\x00" * 8

    fcntl_module.F_GETPATH = 50
    fcntl_module.fcntl = fcntl
    monkeypatch.setattr(pinned_fs, "os", _PlatformOs("posix", readlink_error=True))
    monkeypatch.setitem(sys.modules, "fcntl", fcntl_module)
    assert pinned_fs.fd_real_path(9) == "/Volumes/data/note.txt"
    assert seen == [(9, 50, 1024)]


def _fcntl_without_getpath() -> types.ModuleType:
    return types.ModuleType("fcntl")


def _fcntl_that_refuses() -> types.ModuleType:
    module = types.ModuleType("fcntl")

    def fcntl(fd: int, cmd: int, buf: bytes) -> bytes:
        raise OSError("bad file descriptor")

    module.F_GETPATH = 50
    module.fcntl = fcntl
    return module


@pytest.mark.parametrize(
    "fcntl_module",
    [_fcntl_without_getpath(), _fcntl_that_refuses()],
    ids=["no-f-getpath", "f-getpath-refused"],
)
def test_a_posix_host_with_no_route_that_answers_gets_none(
    monkeypatch: pytest.MonkeyPatch, fcntl_module: types.ModuleType
) -> None:
    monkeypatch.setattr(pinned_fs, "os", _PlatformOs("posix", readlink_error=True))
    monkeypatch.setitem(sys.modules, "fcntl", fcntl_module)
    assert pinned_fs.fd_real_path(9) is None


def test_the_default_skip_reporter_records_nothing() -> None:
    """The reporter a caller gets when it passes none: a skip is accepted and dropped."""
    assert pinned_fs._noop_skip("symlink", "/srv/data/link") is None
