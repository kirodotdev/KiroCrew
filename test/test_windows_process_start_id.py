"""The live Windows arm of ``platform_compat.get_process_start_id``.

``test_platform_compat_coverage.py`` simulates both platform branches from one
host and deliberately skips itself on real Windows, so the mocked Windows-arm
tests it carries never run here. This file is the complement: it exercises the
arm through the REAL query seams on the one host that has them, and pins the
two properties a loop caller depends on -- the identity is the process's own
creation FILETIME, and reading it never sleeps.
"""

from __future__ import annotations

import os
import types

import pytest

from kiro_crew import platform_compat as pc

pytestmark = pytest.mark.skipif(not pc.IS_WINDOWS, reason="exercises the real Windows query seams")


def _fake_kernel32(*, pid: int = 4242, creation: int, exit_code: int, exit_time: int) -> object:
    """A kernel32 returning one scripted identity read."""

    def _get_times(_handle, creation_ref, exit_ref, _kernel_ref, _user_ref) -> int:
        creation_ref._obj.dwHighDateTime = creation >> 32
        creation_ref._obj.dwLowDateTime = creation & 0xFFFFFFFF
        exit_ref._obj.dwHighDateTime = exit_time >> 32
        exit_ref._obj.dwLowDateTime = exit_time & 0xFFFFFFFF
        return 1

    def _get_exit_code(_handle, out) -> int:
        out._obj.value = exit_code
        return 1

    return types.SimpleNamespace(
        GetProcessId=lambda _handle: pid,
        GetProcessTimes=_get_times,
        GetExitCodeProcess=_get_exit_code,
    )


def test_live_identity_is_the_creation_filetime_of_this_process():
    token = pc.get_process_start_id(os.getpid())
    assert token is not None and token.isdigit() and ":" not in token
    # Stable for the process lifetime -- the property every recycle guard
    # persists and re-compares from a different process.
    assert pc.get_process_start_id(os.getpid()) == token


def test_reports_unknown_for_a_pid_that_cannot_be_opened():
    # "Unknown" must not be read as a mismatch by callers.
    assert pc.get_process_start_id(2_000_000_000) is None


def test_sweep_entry_form_carries_the_token():
    # session_pid's spawn tracker writes ``<gw>:<pid>:<start_token>`` when the
    # token is readable and the 2-field legacy form only when it is not.
    from kiro_crew import session_pid

    token = session_pid._pid_start_token(os.getpid())
    assert token is not None and ":" not in token


def test_identity_read_never_sleeps_for_a_just_exited_process(monkeypatch):
    """The event-loop contract: the identity read must not poll.

    ``_track_session_pid`` can run on the asyncio loop with a PID whose runtime
    just exited (the object still referenced, exit FILETIME not yet published).
    The exit bound's read sleeps up to 250ms for that publication; the identity
    read only needs the CREATION time, so it must skip that poll entirely.
    """
    sleeps: list[float] = []
    monkeypatch.setattr(pc, "_open_process_query_handle", lambda _pid: 5)
    monkeypatch.setattr(pc, "_close_process_handle", lambda _handle: None)
    monkeypatch.setattr(
        pc.ctypes,
        "WinDLL",
        lambda _name, **_kwargs: _fake_kernel32(creation=100, exit_code=0, exit_time=0),
    )
    monkeypatch.setattr(pc.time, "sleep", lambda seconds: sleeps.append(seconds))

    assert pc.get_process_start_id(4242) == "100"
    assert sleeps == []


def test_zero_creation_filetime_is_unknown_not_a_token(monkeypatch):
    """A degenerate ``GetProcessTimes`` result (creation FILETIME of zero)
    must read as unknown, never as the token ``"0"``: two ``"0"`` tokens would
    compare equal and pass for the same process."""
    sleeps: list[float] = []
    monkeypatch.setattr(pc, "_open_process_query_handle", lambda _pid: 5)
    monkeypatch.setattr(pc, "_close_process_handle", lambda _handle: None)
    monkeypatch.setattr(
        pc.ctypes,
        "WinDLL",
        lambda _name, **_kwargs: _fake_kernel32(creation=0, exit_code=0, exit_time=0),
    )
    monkeypatch.setattr(pc.time, "sleep", lambda seconds: sleeps.append(seconds))

    assert pc.get_process_start_id(4242) is None
    assert sleeps == []
