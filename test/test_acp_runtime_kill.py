"""Tests for AcpRuntime.kill() liveness verification.

The kill escalation swallows signal-delivery errors by design (racing a
normal exit is common), so kill() must verify the process actually died
before untracking its PID. A survivor left untracked would be invisible to
every sweep and leak until reboot.
"""

from __future__ import annotations

import asyncio
from collections import deque
from unittest.mock import MagicMock

import pytest

from kiro_crew.acp import runtime as rt


def _bare_runtime(pid: int = 54321) -> rt.AcpRuntime:
    """Construct an AcpRuntime with just the state kill() touches."""
    r = rt.AcpRuntime.__new__(rt.AcpRuntime)
    r.recording_allowed = True
    r._dead = False
    r._pending_requests = {}
    r._pending_init_notifications = deque()
    r._routed_requests = {}
    r._session_queues = {}
    r._stderr_lines = []
    r._pid = pid
    r._child_pids = {}
    r._reader_task = None
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


@pytest.fixture(autouse=True)
def _fast_kill_windows(monkeypatch):
    """Make the two escalation waits time out without waiting for a real clock.

    `kill()` only reaches the SIGKILL escalation and the liveness probe after both
    `wait_for`s expire. At 0.05s that depended on the scheduler resuming a coroutine
    inside 50ms, which a loaded runner (and Windows, ~15.6ms timer granularity) does
    not promise. Zero makes `wait_for` raise on its first check: same code path,
    reached deterministically with no sleeping.
    """
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

    kill_process_tree is killpg(getpgid(root)); getpgid raises once the root has
    exited, and swallowing that leaves the launcher, agent and chat process in
    the group unsignalled. The group id is known without the root -- it was a
    session leader -- so the teardown must still reach it, and escalate, once a
    live member vouches for it.
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
    tree_kill = MagicMock()
    monkeypatch.setattr(rt.platform_compat, "kill_process_tree", tree_kill)
    monkeypatch.setattr(rt.platform_compat, "pid_exists", lambda pid: True)
    monkeypatch.setattr(rt, "_signal_orphaned_runtime_group", lambda pgid, sig, inst, **k: {})

    await r.kill()

    tree_kill.assert_not_called()


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
    monkeypatch.setattr(rt.platform_compat, "kill_process_tree", tree_kill)
    monkeypatch.setattr(rt.platform_compat, "pid_exists", lambda pid: True)
    monkeypatch.setattr(rt, "_signal_orphaned_runtime_group", lambda pgid, sig, inst, **k: {})

    await r.kill()

    tree_kill.assert_not_called()


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

    The members can be there and simply not vouch -- a sandbox that scrubbed the
    incarnation token, an unreadable `/proc/<pid>/environ`, a missing argv
    identity -- which is the reported leak's own shape. So the group read is the
    guard, not the platform: something still in the group means the teardown
    reached nothing that was there and must say so, while an empty group is the
    ordinary teardown and stays quiet.
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

    def _denied(pid, sig):
        raise PermissionError

    # The root is still ours -- the tree kill is what gets refused.
    monkeypatch.setattr(rt.platform_compat, "get_process_start_id", lambda pid: "root-start")
    monkeypatch.setattr(rt.platform_compat, "kill_process_tree", _denied)
    monkeypatch.setattr(rt.platform_compat, "pid_exists", lambda pid: True)
    group = MagicMock(return_value={101: "s101"})
    monkeypatch.setattr(rt, "_signal_orphaned_runtime_group", group)

    await r.kill()

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
