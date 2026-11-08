"""Group kills on macOS: EPERM from an all-zombie group reads as an emptied group.

macOS answers ``killpg`` with EPERM, not ESRCH, for a group whose only members
are zombies. ``kill_process_tree`` and ``kill_process_group`` settle that EPERM
with the kernel's group listing and raise ``ProcessLookupError``, which every
caller already reads as "already gone". These tests stub ``killpg``,
``getpgid`` and the listing, so they run on any host.
"""

from __future__ import annotations

import errno

import pytest

from kiro_crew import platform_compat as pc
from kiro_crew import platform_compat_kill as kill
from kiro_crew.platform_compat_proctable import DarwinKinfoProc

PGID = kill._OWN_PGID + 1000
PID = PGID
assert PGID != kill._OWN_PGID and PGID > 1


def _member(pid: int, *, zombie: bool) -> DarwinKinfoProc:
    return DarwinKinfoProc(pid=pid, zombie=zombie, start_id=f"start-{pid}")


@pytest.fixture
def eperm(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """``killpg`` raises EPERM; returns the pgids the listing was asked for."""

    def killpg(pgid: int, sig: int) -> None:
        raise PermissionError(errno.EPERM, "Operation not permitted")

    def unexpected_kill(pid: int, sig: int) -> None:
        raise AssertionError(f"unexpected pid-scoped os.kill({pid}, {sig})")

    pc_calls: list[int] = []
    monkeypatch.setattr(pc, "IS_POSIX", True)
    monkeypatch.setattr(kill.os, "kill", unexpected_kill, raising=False)
    # raising=False: Windows has neither os.killpg nor os.getpgid; both are stubbed.
    monkeypatch.setattr(kill.os, "killpg", killpg, raising=False)
    monkeypatch.setattr(kill.os, "getpgid", lambda pid: PGID, raising=False)
    return pc_calls


def _listing(monkeypatch: pytest.MonkeyPatch, calls: list[int], members) -> None:
    def fake(pgid: int):
        calls.append(pgid)
        return members

    monkeypatch.setattr(pc, "darwin_pgroup_members", fake)


@pytest.mark.parametrize("kill_fn", ["tree", "group"])
def test_macos_all_zombie_group_raises_process_lookup(monkeypatch, eperm, kill_fn):
    monkeypatch.setattr(pc, "IS_MACOS", True)
    _listing(monkeypatch, eperm, [_member(PGID, zombie=True), _member(PGID + 1, zombie=True)])
    with pytest.raises(ProcessLookupError):
        if kill_fn == "tree":
            kill.kill_process_tree(PID, pc.SIGKILL)
        else:
            kill.kill_process_group(PGID, pc.SIGKILL)
    assert eperm == [PGID]


def test_macos_group_with_no_listed_member_raises_process_lookup(monkeypatch, eperm):
    monkeypatch.setattr(pc, "IS_MACOS", True)
    _listing(monkeypatch, eperm, [])
    with pytest.raises(ProcessLookupError):
        kill.kill_process_tree(PID, pc.SIGKILL)


def test_macos_group_with_a_running_member_keeps_the_eperm(monkeypatch, eperm):
    monkeypatch.setattr(pc, "IS_MACOS", True)
    _listing(monkeypatch, eperm, [_member(PGID, zombie=True), _member(PGID + 1, zombie=False)])
    with pytest.raises(PermissionError):
        kill.kill_process_tree(PID, pc.SIGKILL)


def test_macos_unreadable_listing_keeps_the_eperm(monkeypatch, eperm):
    monkeypatch.setattr(pc, "IS_MACOS", True)
    _listing(monkeypatch, eperm, None)
    with pytest.raises(PermissionError):
        kill.kill_process_group(PGID, pc.SIGKILL)


def test_non_macos_eperm_is_raised_without_listing(monkeypatch, eperm):
    monkeypatch.setattr(pc, "IS_MACOS", False)
    _listing(monkeypatch, eperm, [_member(PGID, zombie=True)])
    with pytest.raises(PermissionError):
        kill.kill_process_tree(PID, pc.SIGKILL)
    assert eperm == []
