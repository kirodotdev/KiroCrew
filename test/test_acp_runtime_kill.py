"""Tests for AcpRuntime.kill() liveness verification.

The kill escalation swallows signal-delivery errors by design (racing a
normal exit is common), so kill() must verify the process actually died
before untracking its PID. A survivor left untracked would be invisible to
every sweep and leak until reboot.
"""

from __future__ import annotations

import asyncio
from collections import deque
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp import runtime as rt


def _bare_runtime(pid: int = 54321) -> rt.AcpRuntime:
    """Construct an AcpRuntime with just the state kill() touches."""
    r = rt.AcpRuntime.__new__(rt.AcpRuntime)
    r._dead = False
    r._pending_requests = {}
    r._pending_init_notifications = deque()
    r._routed_requests = {}
    r._session_queues = {}
    r._stderr_lines = []
    r._pid = pid
    r._reader_task = None
    r._stderr_task = None
    r._sandbox_cleanup = None
    r._process_group = None

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


def test_spawned_process_group_returns_the_captured_group(monkeypatch):
    """The captured group is the handle teardown escalates against."""
    r = _bare_runtime()
    group = rt.platform_compat.SpawnedProcessGroup(54321, "77123")
    r._process_group = group

    assert r.spawned_process_group() == group
    assert r._process_group == group


def test_spawned_process_group_is_a_pure_read(monkeypatch):
    """It reports what was CAPTURED, and never gates or releases.

    ``None`` has to keep meaning "no group was ever captured": on POSIX the leader
    is its own group leader, so a caller told ``None`` for a group that merely
    failed its gate would fall back to killing that same recyclable number.
    Authorization is `_signal_teardown_target`'s job, immediately before each
    signal."""
    r = _bare_runtime()
    group = rt.platform_compat.SpawnedProcessGroup(54321, "77123")
    r._process_group = group

    def _explode(_group):  # pragma: no cover — must never be reached
        raise AssertionError("the accessor must not gate")

    monkeypatch.setattr(rt.platform_compat, "pgroup_matches_incarnation", _explode)

    assert r.spawned_process_group() == group
    assert r._process_group == group


def test_spawned_process_group_reports_none_only_when_nothing_was_captured():
    """No capture at all is the one state that answers None."""
    r = _bare_runtime()
    r._process_group = None

    assert r.spawned_process_group() is None


def test_witness_group_members_records_the_runtime_tree(monkeypatch):
    """The runtime keeps no descendant records, so this is its only witness source.

    ``AcpRuntime._child_pids`` is declared and never populated, so the shared
    runtime has nothing else to offer a teardown that finds the leader reaped --
    and without a witness that group is refused rather than signalled.
    """
    from kiro_crew.acp.client import _witness_group_members

    group = rt.platform_compat.SpawnedProcessGroup(54321, "77123")
    monkeypatch.setattr(rt.platform_compat, "IS_POSIX", True)
    monkeypatch.setattr("kiro_crew.acp.client._get_child_pids", lambda pid: [54400])
    monkeypatch.setattr(rt.platform_compat, "pid_exists", lambda pid: True)
    monkeypatch.setattr(rt.platform_compat, "pgroup_of", lambda pid: 54321)
    monkeypatch.setattr(
        rt.platform_compat,
        "get_process_start_id",
        lambda pid: "77123" if pid == 54321 else "88456",
    )

    assert _witness_group_members(group, 54321) == rt.platform_compat.SpawnedProcessGroup(
        54321, "77123", (rt.platform_compat.ProcessGroupMember(54400, "88456"),)
    )


def test_witness_group_members_leaves_an_uncaptured_group_alone(monkeypatch):
    """``None`` means nothing was captured, and the scan must not invent one."""
    from kiro_crew.acp.client import _witness_group_members

    def _explode(pid):  # pragma: no cover — must never be reached
        raise AssertionError("no scan without a captured group")

    monkeypatch.setattr("kiro_crew.acp.client._get_child_pids", _explode)

    assert _witness_group_members(None, 54321) is None


@pytest.mark.asyncio
async def test_kill_refreshes_group_witnesses_before_the_term(monkeypatch):
    """A child forked after initialize is only witnessable during teardown.

    The runtime records witnesses once, right after the handshake. A descendant
    that appears later -- and ignores SIGTERM -- is not in that set, so a pool
    discard would find the group unauthorized and leave it running. The refresh
    happens here, while the leader is still alive to prove the group is ours, and
    therefore strictly BEFORE the signal that reaps it.
    """
    r = _bare_runtime()
    captured = rt.platform_compat.SpawnedProcessGroup(54321, "77123")
    refreshed = captured._replace(members=(rt.platform_compat.ProcessGroupMember(54400, "88456"),))
    r._process_group = captured
    order: list[str] = []

    def _witness(group, pid):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            order.append("witness")
            return refreshed
        raise AssertionError("must not run on the event loop")

    monkeypatch.setattr(rt, "_witness_group_members", _witness)
    monkeypatch.setattr(rt.platform_compat, "pgroup_matches_incarnation", lambda g: True)
    monkeypatch.setattr(
        rt.platform_compat,
        "kill_pgroup",
        lambda *a, **k: order.append("kill") or True,
    )
    monkeypatch.setattr(rt.platform_compat, "pid_exists", lambda pid: False)
    monkeypatch.setattr(rt, "_untrack_pid", lambda pid: None)
    monkeypatch.setattr(rt, "_untrack_session_pid", lambda pid: None)

    await r.kill()

    assert order[:2] == ["witness", "kill"]
    assert r.spawned_process_group() == refreshed


@pytest.mark.asyncio
async def test_kill_keeps_the_captured_group_when_the_refresh_fails(monkeypatch):
    """A refresh that raises costs the late witness, never the whole group."""
    r = _bare_runtime()
    captured = rt.platform_compat.SpawnedProcessGroup(
        54321, "77123", (rt.platform_compat.ProcessGroupMember(54400, "88456"),)
    )
    r._process_group = captured

    def _boom(group, pid):
        raise OSError("proc unreadable")

    monkeypatch.setattr(rt, "_witness_group_members", _boom)
    monkeypatch.setattr(rt.platform_compat, "kill_process_tree", lambda *a, **k: None)
    monkeypatch.setattr(rt.platform_compat, "pid_exists", lambda pid: False)
    monkeypatch.setattr(rt, "_untrack_pid", lambda pid: None)
    monkeypatch.setattr(rt, "_untrack_session_pid", lambda pid: None)

    await r.kill()

    assert r.spawned_process_group() == captured


def _leaderless_group_world(monkeypatch, pgid: int, member):
    pc = rt.platform_compat
    monkeypatch.setattr(pc, "IS_POSIX", True)
    monkeypatch.setattr(pc, "pgroup_exists", lambda g: g == pgid)
    monkeypatch.setattr(pc, "pid_exists", lambda pid: pid == member.pid)
    monkeypatch.setattr(
        pc, "get_process_start_id", lambda pid: member.start_id if pid == member.pid else ""
    )
    monkeypatch.setattr(pc, "pgroup_of", lambda pid: pgid if pid == member.pid else None)


@pytest.mark.asyncio
async def test_kill_escalates_to_the_retained_group_when_the_leader_exits(monkeypatch):
    monkeypatch.setattr(rt.AcpRuntime, "_KILL_TERM_TIMEOUT", 5)
    r = _bare_runtime()
    member = rt.platform_compat.ProcessGroupMember(54400, "88456")
    captured = rt.platform_compat.SpawnedProcessGroup(54321, "77123", (member,))
    r._process_group = captured
    r._process.wait = AsyncMock(return_value=0)
    _leaderless_group_world(monkeypatch, captured.pgid, member)

    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(
        rt.platform_compat,
        "kill_pgroup",
        lambda pgid, sig: signalled.append((pgid, sig)) or True,
    )

    def _no_pid_fallback(*_a, **_kw):
        raise AssertionError("a captured group must never fall back to the pid tree")

    monkeypatch.setattr(rt.platform_compat, "kill_process_tree", _no_pid_fallback)
    monkeypatch.setattr(rt, "_witness_group_members", lambda group, pid: group)
    monkeypatch.setattr(rt, "_untrack_pid", lambda pid: None)
    monkeypatch.setattr(rt, "_untrack_session_pid", lambda pid: None)

    await r.kill()

    assert signalled == [
        (captured.pgid, rt.platform_compat.SIGTERM),
        (captured.pgid, rt.platform_compat.SIGKILL),
    ]


@pytest.mark.asyncio
async def test_kill_never_falls_back_to_the_pid_when_the_group_is_refused(monkeypatch):
    r = _bare_runtime()
    captured = rt.platform_compat.SpawnedProcessGroup(
        54321, "77123", (rt.platform_compat.ProcessGroupMember(54400, "88456"),)
    )
    r._process_group = captured
    monkeypatch.setattr(rt.platform_compat, "IS_POSIX", True)
    monkeypatch.setattr(rt.platform_compat, "pgroup_matches_incarnation", lambda g: False)

    def _forbidden(*_a, **_kw):
        raise AssertionError("nothing may be signalled once the gate refuses")

    monkeypatch.setattr(rt.platform_compat, "kill_pgroup", _forbidden)
    monkeypatch.setattr(rt.platform_compat, "kill_process_tree", _forbidden)
    monkeypatch.setattr(rt, "_witness_group_members", lambda group, pid: group)
    monkeypatch.setattr(rt.platform_compat, "pid_exists", lambda pid: False)
    monkeypatch.setattr(rt, "_untrack_pid", lambda pid: None)
    monkeypatch.setattr(rt, "_untrack_session_pid", lambda pid: None)

    await r.kill()


@pytest.mark.asyncio
async def test_kill_without_a_captured_group_still_signals_the_pid_tree(monkeypatch):
    r = _bare_runtime()
    r._process_group = None

    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(
        rt.platform_compat,
        "kill_process_tree",
        lambda pid, sig: signalled.append((pid, sig)) or True,
    )

    def _forbidden(*_a, **_kw):
        raise AssertionError("no group was captured, so nothing may reach killpg")

    monkeypatch.setattr(rt.platform_compat, "kill_pgroup", _forbidden)
    monkeypatch.setattr(rt.platform_compat, "pid_exists", lambda pid: False)
    monkeypatch.setattr(rt, "_untrack_pid", lambda pid: None)
    monkeypatch.setattr(rt, "_untrack_session_pid", lambda pid: None)

    await r.kill()

    assert signalled == [
        (54321, rt.platform_compat.SIGTERM),
        (54321, rt.platform_compat.SIGKILL),
    ]
