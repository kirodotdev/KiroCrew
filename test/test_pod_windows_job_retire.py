"""Retiring a pod publisher whose own exit has already begun is not a refusal.

``retire_identity`` ends the exact publisher with ``TerminateProcess`` and waits for
its process object to signal. The kernel refuses a terminate aimed at a process
already in its exit path with ERROR_ACCESS_DENIED, and that refusal starts before
the object signals: the exit runs the process down first. A zero-time look at the
object then still finds it unsignalled, so the refusal must be judged by the bounded
retirement wait that follows, and raised only when the object never signals there.
"""

from __future__ import annotations

import ctypes
from types import SimpleNamespace

import pytest

from kiro_crew.pod import _windows_job as jobs

_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_ERROR_ACCESS_DENIED = 5


class _Publisher:
    """kernel32 for ONE publisher object, on a virtual clock.

    ``WaitForSingleObject`` answers as the kernel does for an object that signals
    ``signals_at_ms`` after the terminate (``None``: never): signalled when that
    instant falls inside the wait, otherwise a timeout once the whole wait has
    elapsed. So the outcome depends on how long a caller asks to wait, never on how
    fast this host runs. ``TerminateProcess`` is always refused, with *refusal*.
    """

    def __init__(self, signals_at_ms, refusal=_ERROR_ACCESS_DENIED):
        self.signals_at_ms = signals_at_ms
        self.refusal = refusal
        self.terminated = False
        self.now_ms = 0
        self.last_error = 0
        self.waits: list[int] = []

    def TerminateProcess(self, _handle, _code):
        self.terminated = True
        self.last_error = self.refusal
        return 0

    def WaitForSingleObject(self, _handle, millis):
        self.waits.append(int(millis))
        self.last_error = 0
        if not self.terminated:
            return _WAIT_TIMEOUT  # Still running: the liveness look before the terminate.
        if self.signals_at_ms is not None and self.signals_at_ms <= self.now_ms + millis:
            self.now_ms = max(self.now_ms, self.signals_at_ms)
            return _WAIT_OBJECT_0
        self.now_ms += millis
        return _WAIT_TIMEOUT


def _install(monkeypatch, publisher):
    monkeypatch.setattr(jobs, "_load", lambda: (publisher, publisher))
    # The module reads the saved last error through its own ctypes binding.
    facade = SimpleNamespace(**{**vars(ctypes), "get_last_error": lambda: publisher.last_error})
    monkeypatch.setattr(jobs, "C", facade)


def test_a_publisher_already_exiting_retires_once_its_object_signals(monkeypatch):
    # Refused, unsignalled at that instant, signalled 50 ms later.
    publisher = _Publisher(signals_at_ms=50)
    _install(monkeypatch, publisher)

    jobs.retire_identity(9100, timeout=1.0)

    assert publisher.waits == [0, 1000]


def test_a_live_publisher_that_refuses_still_raises_after_the_bounded_wait(monkeypatch):
    publisher = _Publisher(signals_at_ms=None)
    _install(monkeypatch, publisher)

    with pytest.raises(OSError, match="terminate publisher failed") as raised:
        jobs.retire_identity(9100, timeout=1.0)

    # The terminate's own error, not the last error the wait left.
    assert raised.value.errno == _ERROR_ACCESS_DENIED
    assert publisher.waits == [0, 1000]
    assert publisher.now_ms == 1000


def test_any_other_refusal_of_a_live_publisher_raises_without_the_bounded_wait(monkeypatch):
    publisher = _Publisher(signals_at_ms=None, refusal=6)  # ERROR_INVALID_HANDLE
    _install(monkeypatch, publisher)

    with pytest.raises(OSError, match="terminate publisher failed") as raised:
        jobs.retire_identity(9100, timeout=1.0)

    assert raised.value.errno == 6
    assert publisher.waits == [0, 0]
