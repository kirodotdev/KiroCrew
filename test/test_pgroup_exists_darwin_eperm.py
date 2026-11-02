"""``pgroup_exists`` on macOS: EPERM from an all-zombie group means empty.

macOS answers ``killpg(pgid, 0)`` with EPERM, not ESRCH, for a group whose only
members are zombies. The probe settles that EPERM with the kernel's group
listing. These tests stub ``killpg`` and the listing, so they run on any host.
"""

from __future__ import annotations

import errno

import pytest

from kiro_crew import platform_compat_kill as kill
from kiro_crew.platform_compat_proctable import DarwinKinfoProc

PGID = 4242


def _member(pid: int, *, zombie: bool) -> DarwinKinfoProc:
    return DarwinKinfoProc(pid=pid, zombie=zombie, start_id=f"start-{pid}")


@pytest.fixture
def eperm(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """``killpg`` raises EPERM; returns the list of listing calls made."""

    def killpg(pgid: int, sig: int) -> None:
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(kill, "IS_POSIX", True)
    # raising=False: Windows has no os.killpg, and these tests stub it anyway.
    monkeypatch.setattr(kill.os, "killpg", killpg, raising=False)
    return []


def _listing(monkeypatch: pytest.MonkeyPatch, calls: list[int], members):
    def fake(pgid: int):
        calls.append(pgid)
        return members

    monkeypatch.setattr(kill, "darwin_pgroup_members", fake)


def test_macos_all_zombie_group_reads_empty(monkeypatch, eperm):
    monkeypatch.setattr(kill, "IS_MACOS", True)
    _listing(monkeypatch, eperm, [_member(PGID, zombie=True), _member(PGID + 1, zombie=True)])
    assert kill.pgroup_exists(PGID) is False
    assert eperm == [PGID]


def test_macos_group_with_no_listed_member_reads_empty(monkeypatch, eperm):
    monkeypatch.setattr(kill, "IS_MACOS", True)
    _listing(monkeypatch, eperm, [])
    assert kill.pgroup_exists(PGID) is False


def test_macos_group_with_one_running_member_reads_alive(monkeypatch, eperm):
    monkeypatch.setattr(kill, "IS_MACOS", True)
    _listing(monkeypatch, eperm, [_member(PGID, zombie=True), _member(PGID + 1, zombie=False)])
    assert kill.pgroup_exists(PGID) is True


def test_macos_unreadable_listing_reads_alive(monkeypatch, eperm):
    monkeypatch.setattr(kill, "IS_MACOS", True)
    _listing(monkeypatch, eperm, None)
    assert kill.pgroup_exists(PGID) is True


def test_non_macos_eperm_reads_alive_without_listing(monkeypatch, eperm):
    monkeypatch.setattr(kill, "IS_MACOS", False)
    _listing(monkeypatch, eperm, [_member(PGID, zombie=True)])
    assert kill.pgroup_exists(PGID) is True
    assert eperm == []


def test_macos_esrch_still_reads_gone_without_listing(monkeypatch):
    calls: list[int] = []

    def killpg(pgid: int, sig: int) -> None:
        raise ProcessLookupError(errno.ESRCH, "No such process")

    monkeypatch.setattr(kill, "IS_POSIX", True)
    monkeypatch.setattr(kill, "IS_MACOS", True)
    monkeypatch.setattr(kill.os, "killpg", killpg, raising=False)
    _listing(monkeypatch, calls, [_member(PGID, zombie=False)])
    assert kill.pgroup_exists(PGID) is False
    assert calls == []
