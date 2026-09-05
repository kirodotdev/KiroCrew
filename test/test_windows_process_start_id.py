"""The live Windows arm of ``platform_compat.get_process_start_id``.

``test_platform_compat_coverage.py`` simulates both platform branches from one
host and deliberately skips itself on real Windows, so the mocked Windows-arm
tests it carries never run here. This file is the complement: it exercises the
arm through the REAL query seams on the one host that has them, pinning the
contract issue #8473 asks for — an identity every pid-incarnation consumer
(sweep entries, the signed mapping, gatewayd claims, metrics crumbs) picks up
with zero changes.
"""

from __future__ import annotations

import os
import types

import pytest

from kiro_crew import platform_compat as pc

pytestmark = pytest.mark.skipif(not pc.IS_WINDOWS, reason="exercises the real Windows query seams")


def _identity_kernel32(*, pid: int = 4242, creation: int = 100, exit_code: int = 0):
    """A kernel32 whose process has exited with the exit FILETIME unpublished.

    That state is the one ``_windows_process_handle_identity`` would poll for:
    ``GetExitCodeProcess`` already says exited, but the kernel has not published
    the exit time yet, so the exit-bound read sleeps for it.
    """

    def _get_times(_handle, creation_ref, exit_ref, _kernel_ref, _user_ref) -> int:
        creation_ref._obj.dwHighDateTime = creation >> 32
        creation_ref._obj.dwLowDateTime = creation & 0xFFFFFFFF
        exit_ref._obj.dwHighDateTime = 0
        exit_ref._obj.dwLowDateTime = 0
        return 1

    def _get_exit_code(_handle, out) -> int:
        out._obj.value = exit_code
        return 1

    return types.SimpleNamespace(
        GetProcessId=lambda _handle: pid,
        GetProcessTimes=_get_times,
        GetExitCodeProcess=_get_exit_code,
    )


def test_identity_read_never_sleeps_even_for_a_just_exited_process(monkeypatch):
    """The event-loop contract: the identity read must not poll.

    ``_track_session_pid`` can run on the asyncio loop with a PID whose runtime
    just exited (the object still referenced, exit FILETIME not yet published).
    The exit-bound read sleeps up to 250ms for that publication; the identity
    read only needs the CREATION time, so it must skip that poll entirely.
    """

    sleeps: list[float] = []

    def _record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(pc, "_open_process_query_handle", lambda _pid: 5)
    monkeypatch.setattr(pc, "_close_process_handle", lambda _handle: None)
    monkeypatch.setattr(
        pc.ctypes, "WinDLL", lambda _name, **_kwargs: _identity_kernel32(exit_code=0)
    )
    monkeypatch.setattr(pc.time, "sleep", _record_sleep)

    token = pc.get_process_start_id(4242)

    assert token == "100"
    assert sleeps == []


def test_live_identity_matches_process_start_time():
    # The two identities read the same creation FILETIME through the same
    # seams, so a recycle guard persisted by either caller compares equal to
    # the other's value for the same process.
    first = pc.get_process_start_id(os.getpid())
    assert first is not None and first.isdigit() and ":" not in first
    assert first == pc.process_start_time(os.getpid())
    # Stable for the process lifetime -- the property every recycle guard
    # persists and re-compares from a different process.
    assert pc.get_process_start_id(os.getpid()) == first


def test_reports_unknown_for_a_pid_that_cannot_be_opened():
    # "Unknown" must not be read as a mismatch by callers.
    assert pc.get_process_start_id(2_000_000_000) is None


def test_sweep_entry_form_flips_to_the_token_bearing_shape():
    # session_pid's spawn tracker writes ``<gw>:<pid>:<start_token>`` when the
    # token is readable and the 2-field legacy form only when it is not -- the
    # guard the issue files under is that Windows entries stop being legacy.
    from kiro_crew import session_pid

    token = session_pid._pid_start_token(os.getpid())
    assert token is not None and ":" not in token
