"""Tests for AcpRuntime.kill() liveness verification.

The kill escalation swallows signal-delivery errors by design (racing a
normal exit is common), so kill() must verify the process actually died
before untracking its PID. A survivor left untracked would be invisible to
every sweep and leak until reboot.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import os
import signal
import sys
import threading
from collections import deque
from unittest.mock import MagicMock

import pytest

from kiro_crew import platform_compat
from kiro_crew.acp import runtime as rt
from kiro_crew.process_identity import ProcessHandle, process_survived
from kiro_crew.testing.wait import async_wait_until


def _bare_runtime(pid: int = 54321) -> rt.AcpRuntime:
    """Construct an AcpRuntime with just the state kill() touches."""
    r = rt.AcpRuntime.__new__(rt.AcpRuntime)
    r.recording_allowed = True
    r._dead = False
    r._pending_requests = {}
    r._pending_init_notifications = deque()
    r._kas_load_recaps = {}
    r._routed_requests = {}
    r._session_queues = {}
    r._stderr_lines = []
    r._pid = pid
    # No identity read at spawn: these tests pin the prefix-matched fallback.
    r._spawn_start_token = None
    r._child_pids = {}
    r._reader_task = None
    r._exit_watch_task = None
    r._stderr_task = None
    r._sandbox_cleanup = None
    r._process_instance = "inst-abc"
    r._start_time = "root-start"

    proc = MagicMock()
    proc.pid = pid
    proc.returncode = None

    async def _never_exits() -> None:
        await asyncio.sleep(3600)

    proc.wait = _never_exits
    r._process = proc
    return r


class _StubbedPlatformCompat:
    """The real ``platform_compat``, with only what a kill test must pin replaced.

    ``IS_WINDOWS`` is False so the POSIX teardown runs on any host; other reads delegate
    so no attribute goes missing. Destructive entry points are ``None`` so a missing stub
    fails loudly instead of killing a fabricated pid. ``monkeypatch.setattr`` wins.
    """

    #: Anything that can signal, terminate or reap a process on this host.
    _UNREACHABLE = (
        "kill_process_tree",
        "terminate_windows_asyncio_tree",
        "create_windows_cleanup_owned_process",
        "finish_windows_cleanup_owned_spawn",
        "pid_exists",
    )

    def __init__(self) -> None:
        self.IS_WINDOWS = False
        for name in self._UNREACHABLE:
            setattr(self, name, None)

    def kill_process_tree_pinned(self, pid: int, expected_start_time: str, sig: int) -> bool:
        """The POSIX half of the real function, spelled out rather than delegated.

        The real one reads its own ``IS_WINDOWS``, which this class cannot pin, and on
        Windows would open a real process for an invented pid.
        """
        return self.kill_process_tree(pid, sig)

    def __getattr__(self, name: str):
        # Reached only for names not set on the instance, i.e. neither a pin nor
        # a test's own override.
        return getattr(platform_compat, name)


@pytest.fixture(autouse=True)
def _fast_kill_windows(monkeypatch):
    """Make the two escalation waits time out without waiting for a real clock.

    Zero makes both `wait_for`s raise on the first check, so the SIGKILL escalation is
    reached deterministically; a small timeout depends on scheduler timing.
    """
    monkeypatch.setattr(rt, "platform_compat", _StubbedPlatformCompat())
    monkeypatch.setattr(rt.AcpRuntime, "_KILL_TERM_TIMEOUT", 0)
    # The unreachable-teardown line asks the kernel whether the group still holds
    # anything. Several tests here pin `IS_POSIX` True to exercise the POSIX
    # routing on every host, and that read then takes a POSIX branch a Windows
    # host cannot run. Default it to the same answer the real function gives when
    # it cannot see: something may still be there. Tests that care override it.
    monkeypatch.setattr(rt, "_pgroup_has_member_besides", lambda pgid, root: True)
    monkeypatch.setattr(rt.AcpRuntime, "_KILL_REAP_TIMEOUT", 0)


@pytest.mark.asyncio
async def test_kill_keeps_pid_tracked_when_process_survives(monkeypatch):
    """Signal delivery failures are swallowed upstream — a surviving PID must
    NOT be untracked, so the startup/periodic sweeps keep a handle on it."""
    r = _bare_runtime()
    monkeypatch.setattr(rt.platform_compat, "kill_process_tree", lambda *a, **k: None)
    monkeypatch.setattr(rt.platform_compat, "pid_exists", lambda pid: True)
    untrack = MagicMock()
    monkeypatch.setattr(rt, "_untrack_pid", untrack)
    monkeypatch.setattr(rt, "_untrack_session_pid", untrack)

    await r.kill()

    untrack.assert_not_called()
    assert r._process is None
    assert r._dead is True


@pytest.mark.asyncio
async def test_kill_untracks_only_the_descendants_that_died(monkeypatch):
    """A descendant that escaped the group kill keeps its entry.

    That entry is the only handle the periodic sweep and the next startup
    cleanup have on it; untracking a survivor is the leak the tracking exists
    to close. Pruning is by the descendant's own liveness, not the root's.
    """
    r = _bare_runtime()
    r._child_pids = {700: ("s700", b"agent-chat"), 800: ("s800", b"node")}
    monkeypatch.setattr(rt.platform_compat, "kill_process_tree", lambda *a, **k: None)
    monkeypatch.setattr(rt.platform_compat, "pid_exists", lambda pid: False)
    monkeypatch.setattr(rt, "_untrack_pid", lambda pid: None)
    monkeypatch.setattr(rt, "_untrack_session_pid", lambda pid: None)
    # 700 escaped the killpg (its own setsid); 800 went down with the group.
    monkeypatch.setattr(rt, "_pid_gone_or_unmanaged", lambda pid: pid == 800)
    untracked: list[dict] = []
    monkeypatch.setattr(rt, "_untrack_child_pids", lambda d, **k: untracked.append(d))

    await r.kill()

    assert [sorted(d) for d in untracked] == [[800]]
    assert r._child_pids == {}


@pytest.mark.asyncio
async def test_kill_prunes_descendants_even_when_the_root_survives(monkeypatch):
    """The root's fate says nothing about a child that left the process group."""
    r = _bare_runtime()
    r._child_pids = {900: ("s900", b"node")}
    monkeypatch.setattr(rt.platform_compat, "kill_process_tree", lambda *a, **k: None)
    monkeypatch.setattr(rt.platform_compat, "pid_exists", lambda pid: True)
    monkeypatch.setattr(rt, "_pid_gone_or_unmanaged", lambda pid: True)
    untracked: list[dict] = []
    monkeypatch.setattr(rt, "_untrack_child_pids", lambda d, **k: untracked.append(d))

    await r.kill()

    assert [sorted(d) for d in untracked] == [[900]]


@pytest.mark.asyncio
async def test_kill_signals_the_group_when_the_root_is_already_gone(monkeypatch):
    """The leak that survives descendant tracking: a root that died before any
    descendant was recorded.

    getpgid raises once the root exits, but the root was a session leader, so the group
    id is known and must still be signalled once a live member vouches for it.
    """
    r = _bare_runtime()

    # The autouse fixture zeroes the grace, and wait_for(..., 0) times out before
    # even a finished wait() is read; a dead root's wait() must be SEEN to return,
    # so give this test the real ordering with a small non-zero grace.
    monkeypatch.setattr(rt.AcpRuntime, "_KILL_TERM_TIMEOUT", 1.0)
    # The group fallback is POSIX-shaped (process groups); the group function is
    # stubbed below, so the routing can be exercised on every host.
    monkeypatch.setattr(rt.platform_compat, "IS_POSIX", True)

    async def _already_exited() -> int:
        return -9  # a dead root's wait() returns at once

    r._process.wait = _already_exited
    r._process.returncode = -9  # reaped by asyncio's child watcher already

    def _root_gone(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(rt.platform_compat, "kill_process_tree", _root_gone)
    monkeypatch.setattr(rt.platform_compat, "pid_exists", lambda pid: False)
    monkeypatch.setattr(rt, "_untrack_pid", lambda pid: None)
    monkeypatch.setattr(rt, "_untrack_session_pid", lambda pid: None)
    signalled: list[tuple[int, int, str, object]] = []
    vouched = {101: "s101", 102: "s102"}

    def _group(pgid, sig, instance, *, expected=None):
        signalled.append((pgid, sig, instance, expected))
        return dict(vouched)

    monkeypatch.setattr(rt, "_signal_orphaned_runtime_group", _group)
    slept: list[float] = []

    async def _sleep(secs):
        slept.append(secs)

    monkeypatch.setattr(rt.asyncio, "sleep", _sleep)

    await r.kill()

    # SIGTERM to the group, the same grace a live tree gets, then SIGKILL aimed
    # by the members the SIGTERM vouched -- never by the root's number alone.
    # Both passes carry the incarnation this process was spawned as -- read
    # before the kill cleared it -- so a fresh runtime on the recycled root pid
    # cannot vouch for this group.
    assert signalled == [
        (54321, rt.platform_compat.SIGTERM, "inst-abc", None),
        (54321, rt.platform_compat.SIGKILL, "inst-abc", vouched),
    ]
    assert slept == [rt.AcpRuntime._KILL_TERM_TIMEOUT]


@pytest.mark.asyncio
async def test_kill_never_resolves_the_group_from_a_reaped_root(monkeypatch):
    """asyncio reaps a dead root in the background, freeing its number.

    A fresh session leader on that number makes getpgid SUCCEED, so the tree
    kill would land on it. Once the root is reaped and its start id does not
    match, the tree kill is skipped and only the vouched path runs.
    """
    r = _bare_runtime()
    monkeypatch.setattr(rt.AcpRuntime, "_KILL_TERM_TIMEOUT", 1.0)
    monkeypatch.setattr(rt.platform_compat, "IS_POSIX", True)

    async def _already_exited() -> int:
        return -9

    r._process.wait = _already_exited
    r._process.returncode = -9
    # The number now reads as a different process.
    monkeypatch.setattr(rt.platform_compat, "get_process_start_id", lambda pid: "someone-else")
    # The pinned call is the only tree kill this teardown makes.
    tree_kill = MagicMock()
    monkeypatch.setattr(rt.platform_compat, "kill_process_tree_pinned", tree_kill)
    monkeypatch.setattr(rt.platform_compat, "pid_exists", lambda pid: True)
    group: list = []
    monkeypatch.setattr(
        rt,
        "_signal_orphaned_runtime_group",
        lambda pgid, sig, inst, **k: group.append((pgid, sig, inst)) or {},
    )

    await r.kill()

    tree_kill.assert_not_called()
    assert group[0] == (54321, rt.platform_compat.SIGTERM, "inst-abc")


@pytest.mark.asyncio
async def test_kill_uses_the_tree_kill_while_the_root_identity_holds(monkeypatch):
    """The live start id matching the recorded one is the proof the number is
    still ours; with it the tree is signalled from that number.

    Through the PINNED call, which is the only form this teardown uses: the
    unpinned one delegates through on POSIX but takes its own branch on Windows,
    so asserting on it would pass on one host and describe nothing on the other.
    """
    r = _bare_runtime()
    monkeypatch.setattr(rt.platform_compat, "get_process_start_id", lambda pid: "root-start")
    tree_kill = MagicMock()
    monkeypatch.setattr(rt.platform_compat, "kill_process_tree_pinned", tree_kill)
    monkeypatch.setattr(rt.platform_compat, "pid_exists", lambda pid: False)
    monkeypatch.setattr(rt, "_untrack_pid", lambda pid: None)
    monkeypatch.setattr(rt, "_untrack_session_pid", lambda pid: None)

    await r.kill()

    assert tree_kill.call_count >= 1
    assert tree_kill.call_args[0][1] == "root-start"  # the identity recorded at spawn


@pytest.mark.asyncio
async def test_kill_does_not_trust_an_unset_returncode(monkeypatch):
    """asyncio reaps in the background and propagates returncode a callback
    later, so an unset returncode does not prove the pid is still held."""
    r = _bare_runtime()  # returncode None
    monkeypatch.setattr(rt.platform_compat, "IS_POSIX", True)
    monkeypatch.setattr(rt.platform_compat, "get_process_start_id", lambda pid: "someone-else")
    tree_kill = MagicMock()
    monkeypatch.setattr(rt.platform_compat, "kill_process_tree_pinned", tree_kill)
    monkeypatch.setattr(rt.platform_compat, "pid_exists", lambda pid: True)
    group: list = []
    monkeypatch.setattr(
        rt,
        "_signal_orphaned_runtime_group",
        lambda pgid, sig, inst, **k: group.append((pgid, sig, inst)) or {},
    )

    await r.kill()

    tree_kill.assert_not_called()
    assert group[0] == (54321, rt.platform_compat.SIGTERM, "inst-abc")


@pytest.mark.parametrize("platform", ["win32", "linux", "darwin"])
def test_root_identity_separates_a_failed_read_from_a_mismatch(monkeypatch, platform, caplog):
    """ "could not measure" is not "measured false", on every host.

    Both refuse authorization, and the caller must treat them alike when it
    decides. Only the measured mismatch may DESCRIBE the root, because
    ``get_process_start_id`` also returns None for a pid that is simply gone -
    so the mismatch warning on that branch would name a cause nobody checked.
    """
    monkeypatch.setattr(rt.sys, "platform", platform)
    r = _bare_runtime()  # _start_time == "root-start"

    monkeypatch.setattr(rt.platform_compat, "get_process_start_id", lambda pid: "root-start")
    assert r._root_identity() == "holds"

    monkeypatch.setattr(rt.platform_compat, "get_process_start_id", lambda pid: "someone-else")
    assert r._root_identity() == "mismatch"

    monkeypatch.setattr(rt.platform_compat, "get_process_start_id", lambda pid: None)
    assert r._root_identity() == "unknown"

    # Nothing was recorded at spawn: also unmeasured, never a mismatch.
    r._start_time = None
    monkeypatch.setattr(rt.platform_compat, "get_process_start_id", lambda pid: "anything")
    assert r._root_identity() == "unknown"

    # The boolean every caller uses refuses on both, and only the measured
    # branch warns about the root.
    r._start_time = "root-start"
    with caplog.at_level("WARNING", logger=rt.logger.name):
        monkeypatch.setattr(rt.platform_compat, "get_process_start_id", lambda pid: None)
        assert r._root_identity_holds() is False
    assert "no longer the process spawned" not in caplog.text

    caplog.clear()
    with caplog.at_level("WARNING", logger=rt.logger.name):
        monkeypatch.setattr(rt.platform_compat, "get_process_start_id", lambda pid: "someone-else")
        assert r._root_identity_holds() is False
    assert "no longer the process spawned" in caplog.text


@pytest.mark.parametrize(
    "vouching,is_posix",
    [(False, True), (False, False)],
    ids=["posix-host-without-the-environ-read", "non-posix-host"],
)
@pytest.mark.asyncio
async def test_kill_says_so_when_neither_teardown_path_can_run(
    monkeypatch, caplog, vouching, is_posix
):
    """A teardown that reaches nothing must be audible, not silent.

    The trade is deliberate - a leak over a signal to a stranger - but returning
    an empty dict without a word made it read in the field as a teardown that
    worked. The tree kill needs the root's identity, which is unreadable here,
    and the vouched path needs the Linux-only environ read.
    """
    r = _bare_runtime()
    monkeypatch.setattr(rt, "group_vouching_available", lambda: vouching)
    monkeypatch.setattr(rt.platform_compat, "IS_POSIX", is_posix)
    monkeypatch.setattr(rt.platform_compat, "get_process_start_id", lambda pid: None)
    # Something is still in the group, so the abandonment is real.
    monkeypatch.setattr(rt, "_pgroup_has_member_besides", lambda pgid, root: True)
    monkeypatch.setattr(rt, "_signal_orphaned_runtime_group", lambda pgid, sig, inst, **k: {})
    tree_kill = MagicMock()
    monkeypatch.setattr(rt.platform_compat, "kill_process_tree", tree_kill)
    monkeypatch.setattr(rt.platform_compat, "pid_exists", lambda pid: False)
    monkeypatch.setattr(rt, "_untrack_pid", lambda pid: None)
    monkeypatch.setattr(rt, "_untrack_session_pid", lambda pid: None)

    with caplog.at_level("WARNING", logger=rt.logger.name):
        await r.kill()

    # The unreadable identity still bars the tree kill: this is a leak, not a
    # restored unconditional killpg on a number that may be somebody else's.
    tree_kill.assert_not_called()
    assert "could not be reached with signal" in caplog.text
    assert str(r._pid) in caplog.text
    assert "its identity could not be read" in caplog.text
    assert "no longer the process spawned" not in caplog.text


@pytest.mark.parametrize(
    "group_holds_members,expect_line",
    [(True, True), (False, False)],
    ids=["group-still-holds-members", "group-already-empty"],
)
@pytest.mark.asyncio
async def test_kill_reports_an_unreached_tree_on_a_vouching_host_too(
    monkeypatch, caplog, group_holds_members, expect_line
):
    """On Linux, reaching no member is not proof the tree is gone.

    Members may not vouch (scrubbed token, unreadable `/proc/<pid>/environ`). A
    non-empty group must be reported; an empty group stays quiet.
    """
    r = _bare_runtime()
    monkeypatch.setattr(rt, "group_vouching_available", lambda: True)
    monkeypatch.setattr(rt.platform_compat, "IS_POSIX", True)
    monkeypatch.setattr(rt.platform_compat, "get_process_start_id", lambda pid: None)
    monkeypatch.setattr(rt.platform_compat, "pid_exists", lambda pid: False)
    monkeypatch.setattr(rt, "_pgroup_has_member_besides", lambda pgid, root: group_holds_members)
    monkeypatch.setattr(rt, "_signal_orphaned_runtime_group", lambda pgid, sig, inst, **k: {})
    monkeypatch.setattr(rt, "_untrack_pid", lambda pid: None)
    monkeypatch.setattr(rt, "_untrack_session_pid", lambda pid: None)

    with caplog.at_level("WARNING", logger=rt.logger.name):
        await r.kill()

    if expect_line:
        assert "could not be reached with signal" in caplog.text
        # The reason must name the vouch, not the platform: this host CAN read the
        # token, so "cannot vouch on this host" would be the wrong cause.
        assert "no member of its group vouched" in caplog.text
    else:
        assert "could not be reached with signal" not in caplog.text


@pytest.mark.asyncio
async def test_kill_pins_the_root_identity_across_the_tree_kill(monkeypatch):
    """The tree kill goes through the pinned variant, with the recorded identity.

    Checking the identity in-process and then letting a deferred `taskkill`
    resolve the pid leaves the window a bare number always leaves. The pinned
    call holds the handle that verified it across the terminate.
    """
    r = _bare_runtime()
    monkeypatch.setattr(rt.platform_compat, "get_process_start_id", lambda pid: "root-start")
    monkeypatch.setattr(rt.platform_compat, "pid_exists", lambda pid: False)
    monkeypatch.setattr(rt, "_untrack_pid", lambda pid: None)
    monkeypatch.setattr(rt, "_untrack_session_pid", lambda pid: None)
    monkeypatch.setattr(
        rt.platform_compat,
        "kill_process_tree",
        lambda pid, sig: pytest.fail("unpinned tree kill used"),
    )
    calls: list[tuple[int, str, int]] = []

    def _pinned(pid, start, sig):
        calls.append((pid, start, sig))
        return True

    monkeypatch.setattr(rt.platform_compat, "kill_process_tree_pinned", _pinned)

    await r.kill()

    assert calls and calls[0][0] == r._pid
    assert calls[0][1] == "root-start"  # the identity recorded at spawn


@pytest.mark.asyncio
async def test_kill_falls_back_to_the_vouched_path_when_the_root_cannot_be_pinned(monkeypatch):
    """An unpinnable identity is "do not reap", not "reap by number".

    `kill_process_tree_pinned` returns False WITHOUT signalling, so the teardown
    treats it exactly as an identity that does not hold and goes through the
    token-vouched path instead of the root's number.
    """
    r = _bare_runtime()
    monkeypatch.setattr(rt.platform_compat, "IS_POSIX", True)
    monkeypatch.setattr(rt.platform_compat, "get_process_start_id", lambda pid: "root-start")
    monkeypatch.setattr(rt.platform_compat, "pid_exists", lambda pid: False)
    monkeypatch.setattr(rt, "_untrack_pid", lambda pid: None)
    monkeypatch.setattr(rt, "_untrack_session_pid", lambda pid: None)
    monkeypatch.setattr(
        rt.platform_compat,
        "kill_process_tree",
        lambda pid, sig: pytest.fail("unpinned tree kill used"),
    )
    monkeypatch.setattr(
        rt.platform_compat, "kill_process_tree_pinned", lambda pid, start, sig: False
    )
    vouched: list[int] = []

    def _group(pgid, sig, inst, **kw):
        vouched.append(sig)
        return {}

    monkeypatch.setattr(rt, "_signal_orphaned_runtime_group", _group)

    await r.kill()

    assert vouched, "an unpinnable root never reached the vouched path"


@pytest.mark.asyncio
async def test_kill_cancelled_inside_the_grace_still_escalates(monkeypatch):
    """A shutdown that cancels the teardown mid-grace still owes the SIGKILL.

    The members were vouched and SIGTERMed; ones that ignore SIGTERM would
    otherwise outlive the gateway. The escalation runs on the way out.
    """
    r = _bare_runtime()
    monkeypatch.setattr(rt.AcpRuntime, "_KILL_TERM_TIMEOUT", 1.0)
    monkeypatch.setattr(rt.platform_compat, "IS_POSIX", True)

    async def _already_exited() -> int:
        return -9

    r._process.wait = _already_exited
    r._process.returncode = -9

    def _root_gone(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(rt.platform_compat, "kill_process_tree", _root_gone)
    monkeypatch.setattr(rt.platform_compat, "pid_exists", lambda pid: False)
    signalled: list[int] = []
    vouched = {101: "s101"}

    def _group(pgid, sig, instance, *, expected=None):
        signalled.append(sig)
        return dict(vouched)

    monkeypatch.setattr(rt, "_signal_orphaned_runtime_group", _group)

    async def _cancelled_sleep(secs):
        raise asyncio.CancelledError

    monkeypatch.setattr(rt.asyncio, "sleep", _cancelled_sleep)

    with pytest.raises(asyncio.CancelledError):
        await r.kill()

    assert signalled == [rt.platform_compat.SIGTERM, rt.platform_compat.SIGKILL]


@pytest.mark.asyncio
async def test_kill_does_not_escalate_when_no_member_vouches(monkeypatch):
    """A reaped root whose group has nothing of ours left is simply gone.

    No vouching member means no signal was sent, so no grace is owed and the
    SIGKILL escalation must not run against a number that may now be a
    stranger's.
    """
    r = _bare_runtime()

    # The autouse fixture zeroes the grace, and wait_for(..., 0) times out before
    # even a finished wait() is read; a dead root's wait() must be SEEN to return,
    # so give this test the real ordering with a small non-zero grace.
    monkeypatch.setattr(rt.AcpRuntime, "_KILL_TERM_TIMEOUT", 1.0)
    # The group fallback is POSIX-shaped (process groups); the group function is
    # stubbed below, so the routing can be exercised on every host.
    monkeypatch.setattr(rt.platform_compat, "IS_POSIX", True)

    async def _already_exited() -> int:
        return -9

    r._process.wait = _already_exited
    r._process.returncode = -9

    def _root_gone(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(rt.platform_compat, "kill_process_tree", _root_gone)
    monkeypatch.setattr(rt.platform_compat, "pid_exists", lambda pid: False)
    monkeypatch.setattr(rt, "_untrack_pid", lambda pid: None)
    monkeypatch.setattr(rt, "_untrack_session_pid", lambda pid: None)
    calls: list[int] = []
    monkeypatch.setattr(
        rt, "_signal_orphaned_runtime_group", lambda pgid, sig, inst, **k: calls.append(sig) or {}
    )
    slept: list[float] = []

    async def _sleep(secs):
        slept.append(secs)

    monkeypatch.setattr(rt.asyncio, "sleep", _sleep)

    await r.kill()

    assert calls == [rt.platform_compat.SIGTERM]
    assert slept == []


@pytest.mark.asyncio
async def test_kill_treats_a_denied_signal_as_final(monkeypatch):
    """Only a reaped root reaches the group fallback.

    An OSError that is not ProcessLookupError -- EPERM through a launcher
    wrapper -- says the root is THERE and we may not signal it; guessing at its
    group from the pid would be signalling something we were just refused.
    """
    r = _bare_runtime()
    denied: list = []

    def _denied(pid, recorded, sig):
        denied.append((pid, recorded))
        raise PermissionError

    # The root is still ours -- the tree kill is what gets refused.
    monkeypatch.setattr(rt.platform_compat, "get_process_start_id", lambda pid: "root-start")
    monkeypatch.setattr(rt.platform_compat, "kill_process_tree_pinned", _denied)
    monkeypatch.setattr(rt.platform_compat, "pid_exists", lambda pid: True)
    group = MagicMock(return_value={101: "s101"})
    monkeypatch.setattr(rt, "_signal_orphaned_runtime_group", group)

    await r.kill()

    assert denied and denied[0] == (54321, "root-start")
    group.assert_not_called()


@pytest.mark.asyncio
async def test_kill_untracks_pid_when_process_died(monkeypatch):
    """The normal path: process is gone after escalation, PID is untracked."""
    r = _bare_runtime()
    monkeypatch.setattr(rt.platform_compat, "kill_process_tree", lambda *a, **k: None)
    monkeypatch.setattr(rt.platform_compat, "pid_exists", lambda pid: False)
    untracked_pids: list[int] = []
    monkeypatch.setattr(rt, "_untrack_pid", untracked_pids.append)
    monkeypatch.setattr(rt, "_untrack_session_pid", untracked_pids.append)

    await r.kill()

    assert untracked_pids == [54321, 54321]
    assert r._process is None


@pytest.mark.asyncio
async def test_kill_untracks_root_off_the_event_loop(monkeypatch):
    """The root's PID-registry untrack runs off the event loop.

    It locks and fsyncs the PID file; on the loop a slow disk would stall the gateway
    and block the warm-pool discard's ``wait_for``. Asserted on the recorded thread.
    """
    r = _bare_runtime()
    # A recorded identity so the retire goes through _untrack_root_by_identity,
    # the call that holds the pid-file lock.
    r._spawn_start_token = "root-start"
    monkeypatch.setattr(rt.platform_compat, "kill_process_tree", lambda *a, **k: None)
    monkeypatch.setattr(rt.platform_compat, "pid_exists", lambda pid: False)

    loop_thread_id = threading.get_ident()
    ran_on: list[int] = []

    def _record_thread(pid, token):
        ran_on.append(threading.get_ident())
        return True  # a line of ours was retired; no bare fallback needed

    monkeypatch.setattr(rt, "_untrack_root_by_identity", _record_thread)

    await r.kill()

    assert ran_on, "the root untrack never ran"
    assert (
        ran_on[0] != loop_thread_id
    ), "the root PID untrack ran on the event-loop thread; it must be offloaded"


#: Prints its pid, then blocks on stdin until EOF: the agent below a launcher, which
#: exits only when the gateway's end of its stdin closes.
_EOF_BOUND_CHILD = (
    "import os, sys\n"
    "sys.stdout.write(str(os.getpid()) + chr(10))\n"
    "sys.stdout.flush()\n"
    "sys.stdin.read()\n"
)
#: Starts that child on the inherited stdio and exits at once: a root that dies first.
_ROOT_THAT_DIES_FIRST = (
    "import subprocess, sys\n" f"subprocess.Popen([sys.executable, '-c', {_EOF_BOUND_CHILD!r}])\n"
)


#: One worker runs the tests that start a real interpreter tree: each kill ends its
#: own tree, so the trees cannot be one module-scoped child, and cold-starting them on
#: several workers at once stalls a shard.
_REAL_TREE_GROUP = "acp_runtime_kill_real_tree"

#: The liveness oracle's own reads, bound at import, before any fixture or test patch
#: runs: whatever a test stubs on the kill path, the oracle still asks the kernel.
_kernel_start_id = platform_compat.get_process_start_id
_kernel_pid_is_zombie = platform_compat.pid_is_zombie


def _still_running(pid: int, start: str | None) -> bool:
    """Whether *pid* is still the process recorded as *start*, and not a zombie."""
    if start is None or _kernel_start_id(pid) != start:
        return False
    return _kernel_pid_is_zombie(pid) is not True


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX teardown; Windows drains by handle")
@pytest.mark.xdist_group(name=_REAL_TREE_GROUP)
@pytest.mark.asyncio
async def test_kill_releases_stdin_so_the_tree_a_dead_root_left_reads_eof(monkeypatch):
    """A tree no signal reaches still ends when the kill releases the root's stdin.

    The macOS shape of a leader killed first: the root has exited, its child holds
    stdout and stderr open and blocks on stdin, and neither teardown path can reach
    the leaderless group (the root identity is unreadable and no member vouches).
    asyncio's transport then never finishes on its own, so only the kill closing
    the gateway's end of stdin lets that child read EOF and exit.
    """
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _ROOT_THAT_DIES_FIRST,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    assert proc.stdout is not None
    child = int(await asyncio.wait_for(proc.stdout.readline(), timeout=60))
    child_start = _kernel_start_id(child)
    try:
        await async_wait_until(
            lambda: proc.returncode is not None, describe=lambda: f"root rc={proc.returncode}"
        )
        assert _still_running(child, child_start), "the child must outlive its root"
        r = _bare_runtime(pid=proc.pid)
        r._process = proc
        monkeypatch.setattr(rt, "group_vouching_available", lambda: False)
        monkeypatch.setattr(rt.platform_compat, "IS_POSIX", True)
        monkeypatch.setattr(rt.platform_compat, "get_process_start_id", lambda pid: None)
        monkeypatch.setattr(rt, "_signal_orphaned_runtime_group", lambda pgid, sig, inst, **k: {})
        monkeypatch.setattr(rt.platform_compat, "pid_exists", lambda pid: False)
        monkeypatch.setattr(rt, "_untrack_pid", lambda pid: None)
        monkeypatch.setattr(rt, "_untrack_session_pid", lambda pid: None)

        await r.kill()

        await async_wait_until(
            lambda: not _still_running(child, child_start),
            describe=lambda: f"child {child} still running after kill()",
        )
    finally:
        if _still_running(child, child_start):
            # The child still holds the group the root led, so its number is still ours.
            os.killpg(proc.pid, signal.SIGKILL)


#: On SIGTERM, closes its stdio -- the pipes the root's transport waits on -- and
#: holds its exit until a byte (or EOF) arrives on the gate fd in argv[1]: the agent
#: below the sandbox launcher, whose exit closes those pipes before the init it was
#: reparented to has reaped it.
_AGENT_SLOW_TO_STOP = (
    "import os, signal, sys\n"
    "gate = int(sys.argv[1])\n"
    "def _stop(*_):\n"
    "    for fd in (0, 1, 2):\n"
    "        os.close(fd)\n"
    "    os.read(gate, 1)\n"
    "    os._exit(0)\n"
    "signal.signal(signal.SIGTERM, _stop)\n"
    "print('ready', flush=True)\n"
    "while True:\n"
    "    signal.pause()\n"
)
#: The sandbox launcher's shape: forks that agent into its own process group and
#: waits for it under the default SIGTERM disposition, so a group SIGTERM ends the
#: launcher at once while the agent is still stopping.
_LAUNCHER_ROOT = (
    "import os, sys\n"
    "pid = os.fork()\n"
    "if pid == 0:\n"
    f"    os.execv(sys.executable, [sys.executable, '-c', {_AGENT_SLOW_TO_STOP!r}, sys.argv[1]])\n"
    "print(pid, flush=True)\n"
    "os.waitpid(pid, 0)\n"
)

#: ``prctl`` option that makes a process the reaper of the orphans in its subtree.
_PR_SET_CHILD_SUBREAPER = 36


def _set_child_subreaper(on: bool) -> None:
    """Make this process the reaper of its subtree's orphans (Linux), or stop being it.

    The test that sets it reaps the one orphan its own tree leaves, so the group it
    waits on empties without help from whatever init the host runs.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_CHILD_SUBREAPER, int(on), 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_CHILD_SUBREAPER)")


def _reap_group(pgid: int) -> None:
    """Reap each child of this process left in process group *pgid*, until none is left."""
    with contextlib.suppress(ChildProcessError):
        while True:
            os.waitpid(-pgid, 0)


@pytest.mark.skipif(
    sys.platform != "linux", reason="the sandbox launcher that forks the agent is Linux's"
)
@pytest.mark.xdist_group(name=_REAL_TREE_GROUP)
@pytest.mark.asyncio
async def test_kill_waits_for_the_group_a_root_that_dies_at_once_leaves(monkeypatch):
    """A root that dies on the SIGTERM at once does not end the kill for its group.

    The sandbox launcher is that root: the group's SIGTERM ends it immediately, and
    the agent it forked into the same group closes the root's pipes as it exits, so
    the root's ``wait()`` returns while that agent is still in the group -- exiting,
    or a zombie the init it was reparented to has not reaped yet. A kill that returns
    there hands every check after it -- the descendant prune, a sub-agent's survivor
    check -- a process the SIGTERM already ended, which they read as one that
    survived the teardown and force-kill. The kill waits the group out for the rest
    of the grace instead. The agent's exit is held on a gate the group probe opens,
    so the wait is observed, not timed, and this test is the subreaper that reaps
    the orphaned agent, so the group empties whatever init the host runs.
    """
    proc: asyncio.subprocess.Process | None = None
    agent: int | None = None
    agent_start: str | None = None
    reaped: list[int] = []
    subreaper = False
    gate_w = -1
    try:
        _set_child_subreaper(True)
        subreaper = True
        gate_r, gate_w = os.pipe()
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                _LAUNCHER_ROOT,
                str(gate_r),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                start_new_session=True,
                pass_fds=(gate_r,),
            )
        finally:
            os.close(gate_r)
        assert proc.stdout is not None
        lines = [await asyncio.wait_for(proc.stdout.readline(), timeout=60) for _ in range(2)]
        agent = int(next(line for line in lines if line.strip().isdigit()))
        agent_start = _kernel_start_id(agent)
        asked: list[bool] = []

        def _pgroup_exists(pgid: int) -> bool:
            if asked:
                # The root is gone, so the agent is this subreaper's: reap it once it exits.
                with contextlib.suppress(ChildProcessError):
                    if os.waitpid(agent, os.WNOHANG)[0] == agent:
                        reaped.append(agent)
            alive = platform_compat.pgroup_exists(pgid)
            if alive and not asked:
                os.write(gate_w, b"x")  # the kill is waiting on the group: let the agent stop
            asked.append(alive)
            return alive

        stub = rt.platform_compat
        monkeypatch.setattr(stub, "kill_process_tree", platform_compat.kill_process_tree)
        monkeypatch.setattr(stub, "pid_exists", platform_compat.pid_exists)
        monkeypatch.setattr(stub, "pgroup_exists", _pgroup_exists, raising=False)
        monkeypatch.setattr(rt.AcpRuntime, "_KILL_TERM_TIMEOUT", 60.0)
        monkeypatch.setattr(rt, "_untrack_pid", lambda pid: None)
        monkeypatch.setattr(rt, "_untrack_session_pid", lambda pid: None)
        r = _bare_runtime(pid=proc.pid)
        r._process = proc
        r._start_time = _kernel_start_id(proc.pid)
        r._child_pids = {agent: (agent_start, b"python")}
        # The handle a sub-agent reset takes of this runtime before it resets.
        handle = ProcessHandle(
            pid=proc.pid, start_id=r._start_time, pgid=proc.pid, child_pids=dict(r._child_pids)
        )

        await r.kill()

        assert proc.returncode == -signal.SIGTERM
        assert not process_survived(handle), "a reset's survivor check would force-kill it"
        assert not _still_running(agent, agent_start), "kill() returned with the agent stopping"
        assert asked and asked[-1] is False, asked
        assert reaped == [agent], "the group emptied without this subreaper reaping the agent"
        assert r._process_tree_confirmed_dead is True
    finally:
        try:
            if gate_w >= 0:
                os.close(gate_w)  # EOF releases an agent the kill never waited for
            if proc is not None:
                if proc.returncode is None or (
                    agent is not None and _still_running(agent, agent_start)
                ):
                    # A member is still in the group, so its number is still the root's.
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(proc.pid, signal.SIGKILL)
                await asyncio.wait_for(proc.wait(), timeout=60)
                # The root is reaped, so what is left of its group is this subreaper's.
                await asyncio.wait_for(asyncio.to_thread(_reap_group, proc.pid), timeout=60)
        finally:
            if subreaper:
                _set_child_subreaper(False)
