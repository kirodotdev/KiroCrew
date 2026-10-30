"""Tests for the macOS Mach memory probe (subagent._macos_vm_reclaimable_pages).

Regression guard for the Mach send-right leak: ``mach_host_self`` returns a send
right that MUST be released with ``mach_port_deallocate`` on every probe — on the
success path AND the ``host_statistics64`` failure path — or each probe leaks a
port reference. The Mach path cannot run on the Linux CI fleet, so we inject a
fake ``libSystem`` via ``ctypes.CDLL`` and drive both paths directly.
"""

from __future__ import annotations

import ctypes

import pytest

import kiro_crew.subagent as subagent

# This file replaces ctypes.CDLL, find_library or a cached libc handle for a test,
# so the shared macOS handle caches are put back after each one (see conftest.py).
pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")

_SENTINEL_PORT = 0x1111
_SENTINEL_TASK = 0x2222


class _FakeFn:
    """A stand-in for a ctypes function pointer: callable, and tolerant of the
    ``.restype`` / ``.argtypes`` assignments the probe performs."""

    def __init__(self, fn):
        self._fn = fn
        self.restype = None
        self.argtypes = None

    def __call__(self, *args):
        return self._fn(*args)


class _FakeLibc:
    def __init__(self, kern_return: int = 0):
        self.kern_return = kern_return
        self.dealloc_calls: list[tuple[int, int]] = []
        self.mach_host_self = _FakeFn(lambda: _SENTINEL_PORT)
        self.mach_task_self = _FakeFn(lambda: _SENTINEL_TASK)
        self.mach_port_deallocate = _FakeFn(self._deallocate)
        self.host_statistics64 = _FakeFn(self._host_statistics64)

    def _deallocate(self, task, port):
        self.dealloc_calls.append((task, port))
        return 0

    def _host_statistics64(self, host_port, flavor, stats_ref, count_ref):
        # Populate the caller's vm_statistics64 struct through the byref target.
        obj = getattr(stats_ref, "_obj", None)
        if obj is not None:
            obj.free_count = 100
            obj.inactive_count = 50
            obj.speculative_count = 10
            obj.purgeable_count = 5
        return self.kern_return


def test_deallocates_send_right_on_success(monkeypatch):
    fake = _FakeLibc(kern_return=0)
    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **k: fake)

    result = subagent._macos_vm_reclaimable_pages()

    assert result == 165  # free + inactive + speculative + purgeable
    assert fake.dealloc_calls == [(_SENTINEL_TASK, _SENTINEL_PORT)]


def test_deallocates_send_right_on_failure(monkeypatch):
    fake = _FakeLibc(kern_return=1)  # non-zero kern_return_t -> failure
    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **k: fake)

    result = subagent._macos_vm_reclaimable_pages()

    assert result is None
    # The send right must still be released once even when the stats call fails.
    assert fake.dealloc_calls == [(_SENTINEL_TASK, _SENTINEL_PORT)]


#: 4 KiB pages: 256 pages == 1 MiB, so the figures below read in MiB.
_PAGE = 4096
_PPM = 1024 * 1024 // _PAGE


class _LoadedMacLibc(_FakeLibc):
    """A loaded Mac in the measured shape: more inactive pages than
    file-backed ones, and speculative pages that ``free_count`` already holds."""

    FIELDS = {
        "free_count": 1000 * _PPM,
        "speculative_count": 100 * _PPM,
        "inactive_count": 900 * _PPM,
        "external_page_count": 600 * _PPM,
        "purgeable_count": 50 * _PPM,
    }

    def _host_statistics64(self, host_port, flavor, stats_ref, count_ref):
        obj = getattr(stats_ref, "_obj", None)
        if obj is not None:
            for name, value in self.FIELDS.items():
                setattr(obj, name, value)
        return self.kern_return


def _present_as_loaded_mac(monkeypatch) -> None:
    from kiro_crew import platform_compat

    for name in ("LINUX", "WINDOWS", "MACOS"):
        monkeypatch.setattr(platform_compat, "IS_" + name, name == "MACOS")
    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **k: _LoadedMacLibc())
    real_sysconf = getattr(platform_compat.os, "sysconf", None)

    def fake_sysconf(name: str) -> int:
        if name == "SC_PAGE_SIZE":
            return _PAGE
        if real_sysconf is not None:
            return real_sysconf(name)
        raise ValueError(name)

    # Scoped to SC_PAGE_SIZE: ``os`` is process-wide; raising=False for Windows.
    monkeypatch.setattr(platform_compat.os, "sysconf", fake_sysconf, raising=False)


def test_spawn_floor_reads_host_available_mib_not_the_loose_sum(monkeypatch):
    """The floor compares against what a new process can really take.

    Tight figure (``host_available_mib``): free + purgeable + min(inactive,
    external) = 1000 + 50 + 600 = 1650 MiB. The loose sum adds speculative
    again and all of inactive: 1000 + 900 + 100 + 50 = 2050 MiB. A 1.75 GB
    floor sits between them, so a floor reading the loose sum admits and the
    floor reading the tight figure defers.
    """
    from kiro_crew import platform_compat

    _present_as_loaded_mac(monkeypatch)
    assert platform_compat.host_available_mib() == 1650

    ok, avail = subagent.check_memory_available(min_gb=1.75)

    assert avail == round(1650 / 1024, 2)
    assert ok is False


def test_only_the_floor_moved_the_cap_and_posture_keep_the_loose_sum(monkeypatch):
    """Pins the residual: the auto cap's memory-sized fallback, the TaskRunner's
    auto value and the memory posture still read the looser sum on macOS."""
    _present_as_loaded_mac(monkeypatch)

    assert subagent._available_memory_gb() == round(2050 / 1024, 2)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
