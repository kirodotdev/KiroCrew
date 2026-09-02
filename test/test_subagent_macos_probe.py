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


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
