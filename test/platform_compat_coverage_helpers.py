"""Shared fakes for the ``platform_compat`` coverage suite.

The ctypes entry-point stand-in, the switch that makes ``platform_compat`` believe it
runs on Windows, the ``msvcrt`` and console-stream stand-ins, the Toolhelp snapshot
kernel32, and the platform skip marks, used by ``test_platform_compat_coverage.py`` and
its ``test_platform_compat_coverage_*.py`` parts.
"""

from __future__ import annotations

import io
import os
import sys
import types
from typing import Any, Callable

import pytest

from kiro_crew import platform_compat as pc

# Some tests below simulate the POSIX branch (forcing ``IS_POSIX = True``) or the
# Windows branch from a POSIX host, by monkeypatching attributes that only exist
# on one platform -- ``os.getpgid`` / ``os.killpg`` are absent on Windows, so
# ``monkeypatch.setattr`` itself raises there before the assertion runs.
#
# Skipping them on Windows costs ZERO coverage: ci.yml measures coverage on the
# ubuntu 3.12 shards only (the Windows lane runs --no-cov), so these lines are
# already counted on the lane that reports the number. Simulating the other
# platform's branch is only meaningful from the host that isn't that platform.
_POSIX_ONLY = pytest.mark.skipif(
    os.name == "nt",
    reason="simulates a platform branch via attributes absent on Windows; "
    "coverage is measured on the ubuntu 3.12 shards, so this loses none",
)


class _Fn:
    """Stand-in for a ctypes function pointer.

    The product assigns ``argtypes``/``restype`` on every entry point before
    calling it, so a plain lambda will not do — the attributes must be
    assignable. Declaring them on the class lets the assignment shadow them per
    instance exactly as ctypes would.
    """

    argtypes: list = []
    restype: Any = None

    def __init__(self, impl: Callable[..., Any]) -> None:
        self._impl = impl

    def __call__(self, *args: Any) -> Any:
        return self._impl(*args)


def _const(value: Any) -> _Fn:
    """A fake entry point that ignores its arguments and returns *value*."""

    return _Fn(lambda *_args: value)


def _fake_windows(monkeypatch: pytest.MonkeyPatch, **dlls: Any) -> None:
    """Pretend to be Windows, resolving *dlls* by name for both load styles.

    ``platform_compat`` reaches Win32 two ways — ``ctypes.windll.<name>`` and
    ``ctypes.WinDLL("<name>", use_last_error=True)`` — and neither attribute
    exists on POSIX, hence ``raising=False``.
    """

    monkeypatch.setattr(pc, "IS_WINDOWS", True)
    monkeypatch.setattr(pc, "IS_POSIX", False)
    namespace = types.SimpleNamespace(**dlls)
    monkeypatch.setattr(pc.ctypes, "windll", namespace, raising=False)
    monkeypatch.setattr(
        pc.ctypes,
        "WinDLL",
        lambda name, **_kwargs: getattr(namespace, str(name)),
        raising=False,
    )


class _FakeMsvcrt:
    """Minimal ``msvcrt`` stand-in: ``locking`` replays scripted outcomes."""

    LK_NBLCK = 1
    LK_UNLCK = 0

    def __init__(self, outcomes: list[bool]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[int] = []

    def locking(self, _fd: int, mode: int, _nbytes: int) -> None:
        self.calls.append(mode)
        if mode == self.LK_UNLCK:
            return
        allowed = self._outcomes.pop(0) if self._outcomes else True
        if not allowed:
            raise OSError(13, "locked")


class _StubbornStream:
    """A stream whose ``reconfigure`` is refused, as seen in the 3-layer spawn."""

    encoding = "cp1252"

    def __init__(self, *, with_buffer: bool = True) -> None:
        self.buffer: Any = io.BytesIO() if with_buffer else None

    def reconfigure(self, **_kwargs: Any) -> None:
        raise ValueError("cannot reconfigure a detached stream")


def _toolhelp_kernel32(entries: list[tuple[int, int, bytes]], *, snapshot: Any = 123) -> Any:
    """A kernel32 whose process snapshot replays *entries* (pid, ppid, image)."""

    remaining = list(entries)

    def _fill(entry: Any) -> bool:
        if not remaining:
            return False
        pid, ppid, image = remaining.pop(0)
        entry._obj.th32ProcessID = pid
        entry._obj.th32ParentProcessID = ppid
        entry._obj.szExeFile = image
        return True

    return types.SimpleNamespace(
        CreateToolhelp32Snapshot=_const(snapshot),
        Process32First=_Fn(lambda _snap, entry: _fill(entry)),
        Process32Next=_Fn(lambda _snap, entry: _fill(entry)),
        CloseHandle=_const(True),
    )


_LINUX_ONLY = pytest.mark.skipif(
    sys.platform != "linux", reason="reads Linux /proc; the shim returns None elsewhere"
)
