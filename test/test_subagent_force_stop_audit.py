"""The subagent force-stop path records what actually happened to the process.

Twin of ``test_cron_reaper_failed_kill.py``: a refused signal is audited ``failed``, and
the kill acts on a process handle taken BEFORE the reset, which can pop the session first.

Every signal seam is stubbed, so no test touches a real process. The tree probes are pinned
"gone" for the whole module (:func:`_no_tree_behind_the_leader`): an unpinned
``os.killpg(number, 0)`` on a made-up pid answers whatever the host happens to run.

This file covers the process handles. Records and reports are in
``test_subagent_force_stop_audit_reports.py``, failed kills per caller in
``test_subagent_force_stop_audit_failed_kills.py``, shared fakes in
``subagent_force_stop_audit_helpers.py``.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Mapping
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import subagent_force_stop_audit_helpers
from subagent_force_stop_audit_helpers import (
    _REFUSAL,
    _START_ID,
    _audit,
    _handle_of,
    _hanging_reset,
    _kill_path_stubs,
    _overdue_run,
    _session_with_pid,
)

from kiro_crew import platform_compat
from kiro_crew.config import KiroCrewConfig
from kiro_crew.process_identity import ProcessHandle, process_handle_of
from kiro_crew.session import SessionManager
from kiro_crew.session_lifecycle import TornDown, _TeardownScope
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.testing.wait import await_until

# The shared fixtures, bound here by name so pytest registers them for this module.
_kill_seam = subagent_force_stop_audit_helpers._kill_seam
_leads_its_own_group = subagent_force_stop_audit_helpers._leads_its_own_group
_no_tree_behind_the_leader = subagent_force_stop_audit_helpers._no_tree_behind_the_leader


# ── The kill acts on a handle taken before the reset ──


class TestForceStopActsAfterAResetHang:
    """The reset pops the session from the map before it can hang; the kill must not need it.

    ``SessionLifecycle.reset`` removes the map entry under its lock and only then
    awaits the shutdown that can hang. Without a handle taken before the reset,
    a fallback that looks the key up finds nothing and the run is audited
    ``reaped`` while its process keeps running.
    """

    @pytest.mark.asyncio
    async def test_a_reset_that_hangs_after_popping_the_session_still_gets_the_kill(
        self,
    ) -> None:
        manager, info, key = _overdue_run("popped1", pid=5151)

        async def _pop_then_hang(session_key: str, **_: Any) -> bool:
            manager._sessions._sessions.pop(session_key, None)
            raise asyncio.TimeoutError

        manager._sessions.reset = AsyncMock(side_effect=_pop_then_hang)
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            children,
            alive,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_group") as group_kill,
        ):
            await manager._force_reap("popped1", info, 7200.0)

        assert key not in manager._sessions._sessions, "the fixture did not pop the session"
        group_kill.assert_called_once_with(5151, platform_compat.SIGKILL)
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "reaped"
        assert "kill failed" not in info.error

    @pytest.mark.asyncio
    async def test_a_successor_under_the_key_is_not_the_run_s_process(self) -> None:
        """Only the pre-reset handle names the process; a session in the map now is a successor.

        The reset pops the run's session and awaits; a cold start (a queued turn,
        a continuation) can register a NEW session under the same key in that
        window. Reading the map at kill time would signal that successor and
        leave the run's own, hung process alive -- recorded reaped.
        """
        manager, info, key = _overdue_run("succ1", pid=5252)

        async def _pop_register_successor_then_hang(session_key: str, **_: Any) -> bool:
            manager._sessions._sessions.pop(session_key, None)
            _session_with_pid(manager, session_key, 8080)
            raise asyncio.TimeoutError

        manager._sessions.reset = AsyncMock(side_effect=_pop_register_successor_then_hang)
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            children,
            alive,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_group") as group_kill,
        ):
            await manager._force_reap("succ1", info, 7200.0)

        assert manager._sessions._sessions[key].provider._client._pid == 8080
        group_kill.assert_called_once_with(5252, platform_compat.SIGKILL)

    @pytest.mark.asyncio
    async def test_the_root_is_verified_by_its_recorded_start_id(self) -> None:
        """A root whose live start id matches the one the client recorded is ours: killed.

        The shared child verifier denies a pid with no recorded basename, and the
        root has none, so validating the root through it never let a real kill
        through. The root is compared by start id, the recycling detector itself.
        """
        manager, _info, key = _overdue_run("root1", pid=5353)

        with (
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            patch("kiro_crew.acp.client._is_our_child", return_value=False) as child_verifier,
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=_START_ID),
            patch("kiro_crew.platform_compat.kill_process_group") as group_kill,
        ):
            assert await manager._sigkill_session(key, _handle_of(manager, "root1")) is None

        child_verifier.assert_not_called()
        group_kill.assert_called_once_with(5353, platform_compat.SIGKILL)

    @pytest.mark.asyncio
    async def test_a_start_id_mismatch_never_signals(self) -> None:
        """A live start id that differs from the recorded one means another process owns the pid."""
        manager, _info, key = _overdue_run("recycled1", pid=5454)
        manager._sessions._sessions[key].provider._client._child_pids = {6161: ("111", b"node")}

        with (
            patch("kiro_crew.acp.client._get_child_pids", return_value=[7171]) as probe,
            patch("kiro_crew.acp.client._kill_escaped_children") as sweep,
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value="9999999"),
            patch("kiro_crew.platform_compat.kill_process_group") as group_kill,
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock()) as pid_kill,
        ):
            assert await manager._sigkill_session(key, _handle_of(manager, "recycled1")) is None

        group_kill.assert_not_called()
        pid_kill.assert_not_awaited()
        # Nothing is read through a pid that is not this run's any more; only
        # the children recorded before the reset are swept.
        probe.assert_not_called()
        sweep.assert_called_once_with({6161: ("111", b"node")})

    @pytest.mark.asyncio
    async def test_a_root_that_exits_during_the_child_walk_is_not_signalled_and_its_fresh_child_is_named(
        self,
    ) -> None:
        """A start id changed just before the signal is a gone root; a walked child is named.

        The walked child (7272) came through a pid that may be recycled, so it is not swept
        but reported unattributed. Only the child recorded before the reset (6262) is swept.
        """
        manager, _info, key = _overdue_run("walk1", pid=5555)
        manager._sessions._sessions[key].provider._client._child_pids = {6262: ("222", b"node")}
        walked = MagicMock(return_value={7272: ("333", b"x")})

        def _start_id(pid: int) -> str | None:
            if pid != 5555:
                return None  # the recorded child is gone by the time the sweep re-reads it
            # The root's identity holds at capture and ahead of the child walk;
            # the read immediately before the signal finds it changed.
            return "9999999" if walked.called else _START_ID

        with (
            patch("kiro_crew.acp.client._get_child_pids", return_value=[7272]),
            patch("kiro_crew.acp.client._capture_child_records", walked),
            patch("kiro_crew.acp.client._kill_escaped_children") as sweep,
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.platform_compat.get_process_start_id", side_effect=_start_id),
            patch("kiro_crew.platform_compat.kill_process_group") as group_kill,
        ):
            failure = await manager._sigkill_session(key, _handle_of(manager, "walk1"))

        group_kill.assert_not_called()
        sweep.assert_called_once_with({6262: ("222", b"node")})
        assert failure == (
            "1 child(ren) found during the walk (pid 7272) could not be attributed to the run "
            "after its leader exited; not signalled"
        ), f"the child the walk found under the exiting leader was dropped: {failure!r}"

    @pytest.mark.asyncio
    async def test_a_root_gone_at_the_signal_is_decided_by_its_tree_on_windows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A root that exits during the walk is nothing to kill once its tree is shown gone.

        On Windows a start id still reads back for an exited process while a handle is open,
        so the kill reaches ``kill_process_tree_pinned``, which finds nothing. With no
        cleanup pin pending there is nothing left to reach and no pid-scoped signal.
        """
        manager, _info, key = _overdue_run("walk2", pid=5757)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)

        with (
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=_START_ID),
            patch(
                "kiro_crew.platform_compat.windows_tree_cleanup_pending", return_value=False
            ) as pin,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_pinned", return_value=False
            ) as pinned_kill,
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock()) as pid_kill,
        ):
            assert await manager._sigkill_session(key, _handle_of(manager, "walk2")) is None

        pinned_kill.assert_called_once_with(5757, _START_ID, platform_compat.SIGKILL)
        pin.assert_called_once_with(5757, _START_ID)
        pid_kill.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_live_pid_whose_identity_cannot_be_confirmed_is_a_failed_kill(self) -> None:
        """No readable start id behind a pid that exists: not signalled, and not gone either."""
        manager, _info, key = _overdue_run("unverified1", pid=5656)

        with (
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=None),
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.platform_compat.kill_process_group") as group_kill,
        ):
            with patch("kiro_crew.platform_compat.get_process_start_id", return_value=_START_ID):
                handle = _handle_of(manager, "unverified1")
            failure = await manager._sigkill_session(key, handle)

        group_kill.assert_not_called()
        assert failure == "pid 5656 is alive but could not be verified as this run's; not signalled"

    @pytest.mark.asyncio
    async def test_no_handle_and_no_usable_pid_are_nothing_to_kill(self) -> None:
        """A session with no recorded pid names no process: no candidate, nothing to kill."""
        manager, _info, key = _overdue_run("nopid1", pid=None)
        _session_with_pid(manager, key, None)

        with patch("kiro_crew.platform_compat.kill_process_group") as group_kill:
            assert manager._retain_process_handles("nopid1", key) == []
            assert await manager._sigkill_session("subagent:absent", None) is None
            assert await manager._sigkill_sessions(key, []) is None

        group_kill.assert_not_called()
        assert manager._process_handles == {}


# ── A completed reset is not proof of death ──


class TestACompletedResetIsNotProofOfDeath:
    """After every completed reset the handle is asked, and a survivor gets the fallback."""

    @pytest.mark.asyncio
    async def test_a_process_that_survives_a_completed_reset_still_gets_the_kill(self) -> None:
        """A reset that returned True is not proof the process is gone.

        The reset's shutdown can fail without raising, so the handle is asked afterwards.
        """
        manager, info, _key = _overdue_run("survived1", pid=5959)
        manager._sessions.reset = AsyncMock(return_value=True)
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            children,
            alive,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_group") as group_kill,
        ):
            await manager._force_reap("survived1", info, 7200.0)

        group_kill.assert_called_once_with(5959, platform_compat.SIGKILL)
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "reaped"
        assert "kill failed" not in info.error

    @pytest.mark.asyncio
    async def test_a_survivor_the_kill_cannot_signal_is_a_failed_kill_not_reaped(self) -> None:
        """The survivor's fallback is audited on its own outcome: refused here, so ``failed``."""
        manager, info, _key = _overdue_run("survived2", pid=6060)
        manager._sessions.reset = AsyncMock(return_value=True)
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            children,
            alive,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group",
                MagicMock(side_effect=_REFUSAL),
            ),
        ):
            await manager._force_reap("survived2", info, 7200.0)

        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "failed"
        assert "; kill failed: ValueError: kill_process_group: refusing" in info.error

    @pytest.mark.asyncio
    async def test_a_process_gone_after_a_completed_reset_is_nothing_to_kill(self) -> None:
        """Control: the reset did its job -- no start id and no process behind the pid, no kill."""
        manager, info, _key = _overdue_run("gone2", pid=6161)
        manager._sessions.reset = AsyncMock(return_value=True)

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=None),
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            patch("kiro_crew.platform_compat.kill_process_group") as group_kill,
        ):
            await manager._force_reap("gone2", info, 7200.0)

        group_kill.assert_not_called()
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "reaped"
        assert "kill failed" not in info.error

    @pytest.mark.asyncio
    async def test_an_exited_process_whose_start_id_still_reads_back_is_gone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The Windows false survivor: identity alone said alive for a process the OS confirmed exited.

        A start id reads back for an exited Windows process while any handle is open
        (asyncio's Proactor transport keeps one until GC), so ``pid_exists`` is asked first.
        """
        manager, info, _key = _overdue_run("exited1", pid=6363)
        manager._sessions.reset = AsyncMock(return_value=True)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch("kiro_crew.platform_compat.pid_exists", return_value=False) as alive,
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=_START_ID),
            patch("kiro_crew.platform_compat.kill_process_group") as group_kill,
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock()) as pid_kill,
        ):
            await manager._force_reap("exited1", info, 7200.0)

        alive.assert_called_with(6363)
        group_kill.assert_not_called()
        pid_kill.assert_not_awaited()
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "reaped"
        assert "kill failed" not in info.error

    @pytest.mark.asyncio
    async def test_a_recycled_pid_after_a_completed_reset_is_nothing_to_kill(self) -> None:
        """A live pid whose start id differs from the recorded one is another process's: gone."""
        manager, info, _key = _overdue_run("recycled2", pid=6464)
        manager._sessions.reset = AsyncMock(return_value=True)

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value="9999999"),
            patch("kiro_crew.platform_compat.kill_process_group") as group_kill,
        ):
            await manager._force_reap("recycled2", info, 7200.0)

        group_kill.assert_not_called()
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "reaped"

    @pytest.mark.asyncio
    async def test_a_surviving_pid_whose_identity_cannot_be_read_is_a_failed_kill(self) -> None:
        """Exists, identity unreadable: not proven gone, so the kill decides -- and does not signal."""
        manager, info, _key = _overdue_run("unread1", pid=6565)
        manager._sessions.reset = AsyncMock(return_value=True)

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=None),
            patch("kiro_crew.platform_compat.kill_process_group") as group_kill,
        ):
            await manager._force_reap("unread1", info, 7200.0)

        group_kill.assert_not_called()
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "failed"
        assert "; kill failed: pid 6565 is alive but could not be verified" in info.error

    @pytest.mark.asyncio
    async def test_a_reset_that_finds_no_session_still_kills_through_the_handle(self) -> None:
        """``reset`` answers False when the key is already gone: it stopped nothing.

        A concurrent reset popped the entry first. The handle still says the process stands,
        so the kill goes through the handle.
        """
        manager, info, _key = _overdue_run("unmapped1", pid=6262)

        async def _already_popped(session_key: str, **_: Any) -> bool:
            manager._sessions._sessions.pop(session_key, None)
            return False

        manager._sessions.reset = AsyncMock(side_effect=_already_popped)
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            children,
            alive,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_group") as group_kill,
        ):
            await manager._force_reap("unmapped1", info, 7200.0)

        group_kill.assert_called_once_with(6262, platform_compat.SIGKILL)
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "reaped"

    @pytest.mark.asyncio
    async def test_a_reset_that_finds_no_session_and_no_handle_is_nothing_to_stop(self) -> None:
        """Control: no session before the reset either -- the run had no process to answer for."""
        manager, info, _key = _overdue_run("nosess1", pid=None)
        manager._sessions.reset = AsyncMock(return_value=False)

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch("kiro_crew.platform_compat.kill_process_group") as group_kill,
        ):
            await manager._force_reap("nosess1", info, 7200.0)

        group_kill.assert_not_called()
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "reaped"
        assert manager._process_handles == {}


# ── Two readings of one incarnation are one handle carrying both readings ──


def _handle(
    pid: int,
    start_id: str = _START_ID,
    *,
    pgid: int | None = None,
    children: dict[Any, Any] | None = None,
) -> ProcessHandle:
    return ProcessHandle(pid=pid, start_id=start_id, pgid=pgid, child_pids=dict(children or {}))


class TestEqualHandlesMergeTheirEvidence:
    """A later reading of the same incarnation adds its evidence instead of being dropped.

    Identity is ``(pid, start id)``; group id and child records are ``compare=False``
    evidence. Keeping only the first equal handle would drop children recorded later, so
    an escaped child would never be swept. ``process_identity.add_handle`` is the one rule.
    """

    def test_the_kill_set_keeps_the_popped_readings_children(self) -> None:
        from kiro_crew.process_identity import kill_set

        snapshot = _handle(9901, pgid=9901)
        popped = _handle(9901, children={9905: ("start-9905", "node")})

        targets, missing = kill_set([snapshot], [(MagicMock(name="popped-session"), popped)])

        assert missing is None
        assert len(targets) == 1, f"one incarnation became {len(targets)} handles"
        assert targets[0] == snapshot
        assert targets[0].child_pids == {
            9905: ("start-9905", "node")
        }, "the popped reading's recorded child was dropped with its handle"
        assert targets[0].pgid == 9901, "the captured group was lost in the merge"

    def test_a_reading_that_adds_nothing_keeps_the_handle_object(self) -> None:
        from kiro_crew.process_identity import absorb_handle, add_handle

        kept = _handle(9901, pgid=9901, children={9905: ("start-9905", "node")})
        assert absorb_handle(kept, _handle(9901)) is kept
        handles = [kept]
        add_handle(handles, _handle(9901, children={9905: ("start-9905", "node")}))
        assert handles == [kept] and handles[0] is kept

    def test_a_recycled_child_pid_keeps_its_newer_record(self) -> None:
        """One child pid, two readings: the kernel reused the number, and the later record is the identity."""
        from kiro_crew.process_identity import absorb_handle

        kept = _handle(9901, pgid=9901, children={9905: ("start-9905", "node")})
        merged = absorb_handle(kept, _handle(9901, children={9905: ("start-9950", "sh")}))
        assert merged is not kept, "a changed child record was read as nothing new"
        assert merged.child_pids == {9905: ("start-9950", "sh")}
        assert merged.pgid == 9901 and merged == kept

    def test_the_same_pid_under_another_start_id_stays_two_handles(self) -> None:
        from kiro_crew.process_identity import add_handle

        handles: list[ProcessHandle] = []
        add_handle(handles, _handle(9901, "start-a"))
        add_handle(handles, _handle(9901, "start-b", children={9906: ("start-9906", "node")}))
        assert [h.start_id for h in handles] == ["start-a", "start-b"]
        assert handles[0].child_pids == {} and handles[1].child_pids == {
            9906: ("start-9906", "node")
        }

    def test_the_snapshot_merges_the_retained_entry_with_the_live_sessions_reading(self) -> None:
        manager, _info, key = _overdue_run("merge1", pid=9901)
        # The other path retained this incarnation before the client recorded a
        # child; the live session names that child now.
        manager._process_handles["merge1"] = [_handle(9901, pgid=9901)]
        manager._sessions._sessions[key].provider._client._child_pids = {
            9907: ("start-9907", "node")
        }

        with patch("kiro_crew.platform_compat.get_process_start_id", return_value=_START_ID):
            handles = manager._retain_process_handles("merge1", key)

        assert len(handles) == 1, f"one incarnation became {len(handles)} handles"
        assert handles[0].child_pids == {
            9907: ("start-9907", "node")
        }, "the snapshot kept the retained entry's stale child set"
        assert handles[0].pgid == 9901


# ── The handle is RETAINED across the pop, for the other path ──


class TestTheRetainedHandleOutlivesThePop:
    """The common shape: the run's own ``finally`` resets, that reset hangs after
    popping the session, and the reaper (a deadline, a user Stop) then arrives.
    A handle the reaper took before ITS reset would see an empty map; the entry
    the run's teardown retained before its reset is what the reaper acts on.
    """

    @pytest.mark.asyncio
    async def test_the_reaper_acts_on_the_handle_the_runs_finally_retained(self) -> None:
        manager, info, key = _overdue_run("race1", pid=8181)
        popped = asyncio.Event()
        hang = asyncio.Event()
        resets = 0

        async def _reset(session_key: str, **_: Any) -> bool:
            nonlocal resets
            resets += 1
            if resets == 1:
                # The run's own teardown: pops the session, then hangs in the
                # provider shutdown that follows the pop.
                manager._sessions._sessions.pop(session_key, None)
                popped.set()
                await hang.wait()
                return True
            # The reaper's reset: the key is already gone, so it stops nothing.
            return False

        manager._sessions.reset = AsyncMock(side_effect=_reset)
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            children,
            alive,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_group") as group_kill,
        ):
            teardown = asyncio.create_task(manager._teardown_run_session(info, key))
            manager._tasks["race1"] = teardown
            await await_until(popped, what="the pop landing")
            assert key not in manager._sessions._sessions
            assert manager._process_handles["race1"][0].pid == 8181, "the pop site retained nothing"

            await manager._force_reap("race1", info, 7200.0)

            # The reaper cancels the run's task after its kill; the teardown's
            # own clear then finds the entry already consumed.
            with pytest.raises(asyncio.CancelledError):
                await teardown

        assert resets == 2
        group_kill.assert_called_once_with(8181, platform_compat.SIGKILL)
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "reaped"
        assert manager._process_handles == {}
        assert info.done and info.reaped

    @pytest.mark.asyncio
    async def test_the_runs_finally_acts_on_the_handle_the_reaper_retained(self) -> None:
        """Roles reversed: the reaper's reset pops and hangs, the run's finally arrives."""
        manager, info, key = _overdue_run("race2", pid=8282)
        popped = asyncio.Event()
        hang = asyncio.Event()
        resets = 0

        async def _reset(session_key: str, **_: Any) -> bool:
            nonlocal resets
            resets += 1
            if resets == 1:
                manager._sessions._sessions.pop(session_key, None)
                popped.set()
                await hang.wait()
                return True
            return False

        manager._sessions.reset = AsyncMock(side_effect=_reset)
        group_kill = MagicMock()

        def _alive(pid: int) -> bool:
            # The process stands until the kill lands; then it is gone.
            return not group_kill.call_count

        def _start_id(pid: int) -> str | None:
            return None if group_kill.call_count else _START_ID

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            patch("kiro_crew.platform_compat.pid_exists", side_effect=_alive),
            patch("kiro_crew.platform_compat.get_process_start_id", side_effect=_start_id),
            patch("kiro_crew.platform_compat.kill_process_group", group_kill),
        ):
            reap = asyncio.create_task(manager._force_reap("race2", info, 7200.0))
            await await_until(popped, what="the pop landing")
            assert manager._process_handles["race2"][0].pid == 8282

            await manager._teardown_run_session(info, key)
            group_kill.assert_called_once_with(8282, platform_compat.SIGKILL)

            hang.set()
            await await_until(reap, what="the reap finishing")

        # The reaper's own survivor check found the process gone: one kill, not two.
        group_kill.assert_called_once()
        assert _audit(mock_sel, "run_finally_force_kill")["outcome"] == "sigkill"
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "reaped"
        assert manager._process_handles == {}


# ── Every candidate under a shared key, each on its own handle ──


def _finally_popped_and_hung(
    manager: SubagentManager, info: SubagentInfo, key: str
) -> tuple[asyncio.Event, asyncio.Event]:
    """The run's own ``finally`` reset has popped the session and hangs; the reaper's reset completes.

    Returns the ``popped`` event (set once the pop happened) and the ``hang``
    event the test sets to let the run's teardown finish.
    """
    popped = asyncio.Event()
    hang = asyncio.Event()
    resets = 0

    async def _reset(session_key: str, **_: Any) -> bool:
        nonlocal resets
        resets += 1
        if resets == 1:
            manager._sessions._sessions.pop(session_key, None)
            popped.set()
            await hang.wait()
            return True
        # The reaper's reset pops whatever is live under the key and returns.
        manager._sessions._sessions.pop(session_key, None)
        return True

    manager._sessions.reset = AsyncMock(side_effect=_reset)
    return popped, hang


class TestEveryCandidateUnderTheKeyIsKilledOnItsOwnHandle:
    """The run's popped session and a successor registered under the key are both killed.

    The reap ends the KEY, so each candidate is verified and signalled on its own handle.
    """

    @pytest.mark.asyncio
    async def test_a_live_successor_at_snapshot_time_is_killed_alongside_the_runs_process(
        self,
    ) -> None:
        manager, info, key = _overdue_run("shared1", pid=9191)
        popped, hang = _finally_popped_and_hung(manager, info, key)
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            children,
            alive,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_group") as group_kill,
        ):
            teardown = asyncio.create_task(manager._teardown_run_session(info, key))
            manager._tasks["shared1"] = teardown
            await await_until(popped, what="the pop landing")
            # A successor registered under the key while the run's teardown hangs.
            _session_with_pid(manager, key, 9292)

            await manager._force_reap("shared1", info, 7200.0)
            with pytest.raises(asyncio.CancelledError):
                await teardown

        # The reaper's reset completed (it popped the successor) and both
        # processes survived it: both are killed, each on its own handle.
        assert sorted(call.args[0] for call in group_kill.call_args_list) == [9191, 9292]
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "reaped"
        assert "kill failed" not in info.error
        assert manager._process_handles == {}

    @pytest.mark.asyncio
    async def test_one_refused_kill_among_several_is_named_and_the_rest_are_still_signalled(
        self,
    ) -> None:
        manager, info, key = _overdue_run("shared2", pid=9393)
        popped, hang = _finally_popped_and_hung(manager, info, key)
        children, alive, start_id, sweep = _kill_path_stubs()

        def _kill(pgid: int, sig: int) -> None:
            if pgid == 9393:
                raise ValueError(
                    f"kill_process_group: refusing broadcast/self process group {pgid}"
                )

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            children,
            alive,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_group", side_effect=_kill) as group_kill,
        ):
            teardown = asyncio.create_task(manager._teardown_run_session(info, key))
            manager._tasks["shared2"] = teardown
            await await_until(popped, what="the pop landing")
            _session_with_pid(manager, key, 9494)

            await manager._force_reap("shared2", info, 7200.0)
            with pytest.raises(asyncio.CancelledError):
                await teardown

        assert sorted(call.args[0] for call in group_kill.call_args_list) == [9393, 9494]
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "failed"
        assert "; kill failed: ValueError: kill_process_group: refusing" in info.error
        assert "9393" in info.error
        assert manager._process_handles == {}

    @pytest.mark.asyncio
    async def test_the_same_process_under_two_sources_is_one_handle(self) -> None:
        """The retained entry and the live session naming one pid is one candidate, one kill."""
        manager, info, key = _overdue_run("shared3", pid=9595)
        children, alive, start_id, sweep = _kill_path_stubs()
        # The run's teardown retained the handle but its pop has not happened yet
        # (the session is still live under the key): same process on both sources.
        with start_id:
            assert manager._retain_process_handles("shared3", key) != []
        _hanging_reset(manager)

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent._RESET_TIMEOUT", 0.05),
            patch("kiro_crew.subagent.sel") as mock_sel,
            children,
            alive,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_group") as group_kill,
        ):
            await manager._force_reap("shared3", info, 7200.0)

        group_kill.assert_called_once_with(9595, platform_compat.SIGKILL)
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "reaped"

    def test_a_recycled_pid_under_the_key_keeps_the_stale_handle_and_the_live_successor(
        self,
    ) -> None:
        """One pid, two start ids: two processes, each reached only through its own handle.

        The stale handle is the only way back to the exited root's Windows tree pin or POSIX
        group; the successor's handle is the only one that verifies the live pid. Only an
        exact (pid, start id) match is one process.
        """
        manager, _info, key = _overdue_run("recycled4", pid=None)
        manager._process_handles["recycled4"] = [
            ProcessHandle(pid=6161, start_id="1111", pgid=6161, child_pids={})
        ]
        _session_with_pid(manager, key, 6161, start_id="2222")

        handles = manager._retain_process_handles("recycled4", key)

        assert [h.start_id for h in handles] == ["1111", "2222"], (
            "the recycled pid's stale handle was dropped with its tree cleanup pending: "
            f"{[h.start_id for h in handles]}"
        )
        assert manager._process_handles["recycled4"] == handles

    @pytest.mark.asyncio
    async def test_on_windows_the_exited_roots_pending_tree_is_drained_beside_the_successors_kill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The stale handle drains the old tree's pin; the successor is tree-killed; one ``reaped``.

        The stale handle's leader is gone but its cleanup pin is pending, so draining the pin
        is its kill; the successor's handle ``taskkill /T``s the process now holding the pid.
        """
        manager, info, key = _overdue_run("recycled6", pid=None)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        manager._process_handles["recycled6"] = [
            ProcessHandle(pid=6363, start_id="1111", pgid=None, child_pids={})
        ]
        _session_with_pid(manager, key, 6363, start_id="2222")
        _hanging_reset(manager)

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent._RESET_TIMEOUT", 0.05),
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value="2222"),
            patch(
                "kiro_crew.platform_compat.windows_tree_cleanup_pending",
                side_effect=lambda pid, start_id: start_id == "1111",
            ),
            patch(
                "kiro_crew.platform_compat.kill_process_tree_pinned", return_value=True
            ) as pinned_drain,
            patch("kiro_crew.platform_compat.kill_process_group") as group_kill,
        ):
            await manager._force_reap("recycled6", info, 7200.0)

        # Two handles, two pinned kills: the successor under its own identity,
        # and the exited root's tree under the identity its pin was reserved by.
        assert sorted(call.args[:2] for call in pinned_drain.call_args_list) == [
            (6363, "1111"),
            (6363, "2222"),
        ], "the exited root's pending tree was not drained beside the successor's kill"
        group_kill.assert_not_called()
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "reaped"
        assert "kill failed" not in info.error
        assert manager._process_handles == {}

    def test_the_same_start_id_under_two_sources_stays_one_handle(self) -> None:
        """Control: equal pid AND start id is the same process, however many sources name it."""
        manager, _info, key = _overdue_run("shared5", pid=6262)
        manager._process_handles["shared5"] = [
            ProcessHandle(pid=6262, start_id=_START_ID, pgid=6262, child_pids={})
        ]

        handles = manager._retain_process_handles("shared5", key)

        assert len(handles) == 1
        assert handles[0].start_id == _START_ID

    @pytest.mark.asyncio
    async def test_a_session_torn_down_by_another_path_is_reached_through_tearing_down(
        self,
    ) -> None:
        """A reset started outside the two teardown paths still leaves the process reachable.

        A dashboard reset or ``cancel_all`` popped the session, so only ``tearing_down``
        still names it.
        """
        manager, info, key = _overdue_run("shared4", pid=9696)
        torn = manager._sessions._sessions.pop(key)
        with patch("kiro_crew.platform_compat.get_process_start_id", return_value=_START_ID):
            entry = TornDown(session=torn, handle=process_handle_of(torn))
        manager._sessions.tearing_down = MagicMock(
            side_effect=lambda session_key: [entry] if session_key == key else []
        )
        children, alive, start_id, sweep = _kill_path_stubs()
        _hanging_reset(manager)

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent._RESET_TIMEOUT", 0.05),
            patch("kiro_crew.subagent.sel") as mock_sel,
            children,
            alive,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_group") as group_kill,
        ):
            await manager._force_reap("shared4", info, 7200.0)

        group_kill.assert_called_once_with(9696, platform_compat.SIGKILL)
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "reaped"
        assert manager._process_handles == {}

    @pytest.mark.asyncio
    async def test_every_teardown_in_flight_under_the_key_is_reached(self) -> None:
        """Two hung teardowns under one key are two processes, and both are killed.

        ``SessionManager.tearing_down`` returns a list, in pop order; read as one session
        it names no pid and stops neither process.
        """
        manager, info, key = _overdue_run("shared5", pid=9797)
        first = manager._sessions._sessions.pop(key)
        successor = MagicMock()
        successor.provider._client._pid = 9798
        successor.provider._client._start_time = "start-9798"
        successor.provider._client._child_pids = {}
        with patch(
            "kiro_crew.platform_compat.get_process_start_id",
            side_effect=lambda pid: {9797: _START_ID, 9798: "start-9798"}[pid],
        ):
            entries = [
                TornDown(session=first, handle=process_handle_of(first)),
                TornDown(session=successor, handle=process_handle_of(successor)),
            ]
        manager._sessions.tearing_down = MagicMock(
            side_effect=lambda session_key: entries if session_key == key else []
        )
        children, alive, _start_id, sweep = _kill_path_stubs()
        _hanging_reset(manager)

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent._RESET_TIMEOUT", 0.05),
            patch("kiro_crew.subagent.sel") as mock_sel,
            children,
            alive,
            patch(
                "kiro_crew.platform_compat.get_process_start_id",
                side_effect=lambda pid: {9797: _START_ID, 9798: "start-9798"}[pid],
            ),
            sweep,
            patch("kiro_crew.platform_compat.kill_process_group") as group_kill,
        ):
            await manager._force_reap("shared5", info, 7200.0)

        killed = sorted(call.args[0] for call in group_kill.call_args_list)
        assert killed == [
            9797,
            9798,
        ], f"the reap reached {killed} of the two processes torn down under the key"
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "reaped"
        assert manager._process_handles == {}

    def test_a_torn_down_entry_is_retained_on_the_handle_captured_at_its_pop(self) -> None:
        """The snapshot keeps each ``TornDown`` entry's own ``handle``, read at the pop.

        Re-reading the popped session finds the pid the hung teardown has since cleared.
        """
        manager, _info, key = _overdue_run("shared6", pid=9696)
        torn = manager._sessions._sessions.pop(key)
        with patch("kiro_crew.platform_compat.get_process_start_id", return_value=_START_ID):
            captured = process_handle_of(torn)  # read at the pop, the process alive
        # The hung teardown's own awaits cleared the wrapper's pid; the process
        # it named at the pop is still standing.
        torn.provider._client._pid = None
        torn.provider._client._start_time = None
        manager._sessions.tearing_down = MagicMock(
            side_effect=lambda session_key: (
                [TornDown(session=torn, handle=captured)] if session_key == key else []
            )
        )

        handles = manager._retain_process_handles("shared6", key)

        assert handles == [captured], (
            f"the snapshot retained {handles}; the torn-down entry's captured handle "
            f"(pid {captured.pid}) is the process to kill"
        )
        assert handles[0] is captured

    @pytest.mark.asyncio
    async def test_a_torn_down_process_is_killed_on_its_captured_handle(self) -> None:
        """A hung teardown whose wrapper has cleared its pid is killed on the captured handle.

        The ACP client's reset clears the pid after a kill it could not confirm, then hangs.
        """
        manager, info, key = _overdue_run("shared7", pid=9696)
        torn = manager._sessions._sessions.pop(key)
        with patch("kiro_crew.platform_compat.get_process_start_id", return_value=_START_ID):
            captured = process_handle_of(torn)  # read at the pop, the process alive
        torn.provider._client._pid = None
        torn.provider._client._start_time = None
        manager._sessions.tearing_down = MagicMock(
            side_effect=lambda session_key: (
                [TornDown(session=torn, handle=captured)] if session_key == key else []
            )
        )
        children, alive, start_id, sweep = _kill_path_stubs()
        _hanging_reset(manager)

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent._RESET_TIMEOUT", 0.05),
            patch("kiro_crew.subagent.sel") as mock_sel,
            children,
            alive,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_group") as group_kill,
        ):
            await manager._force_reap("shared7", info, 7200.0)

        signalled = [call.args for call in group_kill.call_args_list]
        assert signalled == [(9696, platform_compat.SIGKILL)], (
            "the reap did not kill the torn-down process on the handle captured at its pop: "
            f"signalled {signalled}"
        )
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "reaped"
        assert manager._process_handles == {}

    @pytest.mark.asyncio
    async def test_the_real_session_map_satisfies_the_reads_the_snapshot_makes(self) -> None:
        """The real ``SessionManager`` satisfies the snapshot's fail-open reads.

        The reads treat a missing ``_sessions`` or ``tearing_down`` as a miss, so a rename
        there would turn every reap into "nothing to stop" without a test going red.
        """
        real = SessionManager(KiroCrewConfig(), provider_factory=MagicMock())

        assert isinstance(real._sessions, Mapping)
        assert real._sessions.get("never-registered") is None
        assert callable(real.tearing_down)
        assert real.tearing_down("never-registered") == []
        # The entries that list carries are read fail-open too (``entry.session``,
        # ``entry.handle``): the record must carry exactly those two fields, and
        # stay immutable, or a hung teardown's process silently leaves the snapshot.
        assert {field.name for field in dataclasses.fields(TornDown)} == {"session", "handle"}
        assert TornDown.__dataclass_params__.frozen


def _reset_pops_a_late_successor(
    manager: SubagentManager, successor: Any
) -> tuple[list[Any], asyncio.Event]:
    """A session manager whose reset pops ``successor`` (registered after the snapshot) and hangs.

    The reset reports the pop through ``scope.note_pop``, as ``SessionLifecycle.reset`` does.
    Returns the recorded ``on_pop`` hooks and the event set once the pop happened.
    """
    hooks: list[Any] = []
    popped = asyncio.Event()

    def _scope(on_pop: Any = None) -> Any:
        hooks.append(on_pop)
        return _TeardownScope({}, on_pop)

    async def _reset(session_key: str, *, scope: Any = None) -> bool:
        assert scope is not None, "the teardown path handed its reset no scope"
        scope.note_pop(session_key, successor)
        popped.set()
        await asyncio.sleep(999)
        return True

    manager._sessions.teardown_scope = MagicMock(side_effect=_scope)
    manager._sessions.reset = AsyncMock(side_effect=_reset)
    return hooks, popped


def _late_successor(pid: int | None, start_id: str | None = None) -> MagicMock:
    """A session a cold start registered under the key after the snapshot was taken."""
    successor = MagicMock(name="late-successor")
    successor.provider._client._pid = pid
    successor.provider._client._start_time = start_id
    successor.provider._client._child_pids = {}
    return successor


def _reset_pops_the_live_session(manager: SubagentManager) -> None:
    """A session manager whose reset pops the LIVE session under the key -- the ordinary pop -- through the scope it is handed, and completes."""

    async def _reset(session_key: str, *, scope: Any = None) -> bool:
        session = manager._sessions._sessions.pop(session_key, None)
        if scope is not None and session is not None:
            scope.note_pop(session_key, session)
        return True

    manager._sessions.teardown_scope = lambda on_pop=None: _TeardownScope({}, on_pop)
    manager._sessions.reset = AsyncMock(side_effect=_reset)


class TestTheSessionTheResetPopsIsKilledToo:
    """The exact session a reset pops joins the kill set, however late it registered.

    A queued ``spawn_continue`` can register a session between the snapshot and the pop.
    The scope's ``on_pop`` hook takes the popped session's handle atomically with the pop;
    a popped session with no recorded pid is a named kill failure, never ``reaped``.
    """

    @pytest.mark.asyncio
    async def test_the_reap_kills_the_successor_its_reset_popped(self) -> None:
        manager, info, _key = _overdue_run("late1", pid=9901)
        successor = _late_successor(9902, "start-9902")
        _hooks, popped = _reset_pops_a_late_successor(manager, successor)
        children, alive, _start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch("kiro_crew.subagent._RESET_TIMEOUT", 0.05),
            children,
            alive,
            patch(
                "kiro_crew.platform_compat.get_process_start_id",
                side_effect=lambda pid: {9901: _START_ID, 9902: "start-9902"}[pid],
            ),
            sweep,
            patch("kiro_crew.platform_compat.kill_process_group") as group_kill,
        ):
            await manager._force_reap("late1", info, 7200.0)

        assert popped.is_set()
        killed = sorted(call.args[0] for call in group_kill.call_args_list)
        assert killed == [
            9901,
            9902,
        ], f"the reap killed {killed}: the session its own reset popped was not in the kill set"
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "reaped"
        assert "kill failed" not in info.error
        assert manager._process_handles == {}

    @pytest.mark.asyncio
    async def test_the_runs_finally_kills_the_successor_its_reset_popped(self) -> None:
        manager, info, key = _overdue_run("late2", pid=9903)
        successor = _late_successor(9904, "start-9904")
        _hooks, popped = _reset_pops_a_late_successor(manager, successor)
        children, alive, _start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch("kiro_crew.subagent._RESET_TIMEOUT", 0.05),
            children,
            alive,
            patch(
                "kiro_crew.platform_compat.get_process_start_id",
                side_effect=lambda pid: {9903: _START_ID, 9904: "start-9904"}[pid],
            ),
            sweep,
            patch("kiro_crew.platform_compat.kill_process_group") as group_kill,
        ):
            await manager._teardown_run_session(info, key)

        assert popped.is_set()
        killed = sorted(call.args[0] for call in group_kill.call_args_list)
        assert killed == [
            9903,
            9904,
        ], f"the run's finally killed {killed}: the session its reset popped was not in the kill set"
        assert _audit(mock_sel, "run_finally_force_kill")["outcome"] == "sigkill"
        assert manager._process_handles == {}

    @pytest.mark.asyncio
    async def test_a_popped_session_with_no_pid_is_a_named_kill_failure(self) -> None:
        """A cold start still spawning: nothing names its process, so nothing verifies what it leaves."""
        manager, info, _key = _overdue_run("late3", pid=9905)
        _hooks, popped = _reset_pops_a_late_successor(manager, _late_successor(None))
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch("kiro_crew.subagent._RESET_TIMEOUT", 0.05),
            children,
            alive,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_group") as group_kill,
        ):
            await manager._force_reap("late3", info, 7200.0)

        assert popped.is_set()
        group_kill.assert_called_once_with(9905, platform_compat.SIGKILL)
        assert (
            _audit(mock_sel, "reaper_force_kill")["outcome"] == "failed"
        ), "the audit says reaped over a popped session whose process nothing could name"
        assert "; kill failed: the session the reset popped had no process handle yet" in info.error

    @pytest.mark.asyncio
    async def test_a_popped_session_the_snapshot_saw_with_no_pid_is_nothing_to_kill(self) -> None:
        """A session the snapshot saw with no pid (an in-process provider) is not a kill failure.

        Calling it one would turn every such run's clean completion into ``failed``.
        """
        manager, info, key = _overdue_run("seen1", pid=None)
        _session_with_pid(manager, key, None)
        _reset_pops_the_live_session(manager)

        with patch("kiro_crew.subagent.sel") as mock_sel:
            await manager._teardown_run_session(info, key)

        assert not [
            call
            for call in mock_sel().log_tool_invocation.call_args_list
            if call.kwargs.get("tool_name") == "run_finally_force_kill"
        ], "a session the snapshot saw, with no process, was audited as a failed kill"
        assert not info.error

    @pytest.mark.asyncio
    async def test_the_reap_over_a_seen_pid_less_session_is_a_reap(self) -> None:
        manager, info, key = _overdue_run("seen2", pid=None)
        _session_with_pid(manager, key, None)
        _reset_pops_the_live_session(manager)

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch("kiro_crew.platform_compat.kill_process_group") as group_kill,
        ):
            await manager._force_reap("seen2", info, 7200.0)

        group_kill.assert_not_called()
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "reaped"
        assert "kill failed" not in (info.error or "")

    def test_a_manager_without_the_torn_down_table_gets_the_plain_reset_call(self) -> None:
        """No torn-down table (``tearing_down`` answers no list) means no scope is passed.

        A double's ``reset`` may take one positional argument only, so ``scope=`` would fail
        it and leave the key live (``test_member_continuation_identity.py``).
        """
        from kiro_crew.process_identity import teardown_capture

        plain = MagicMock()
        assert teardown_capture(plain, "subagent:plain") == ({}, [])
        plain.teardown_scope.assert_not_called()

        tabled = MagicMock()
        tabled.tearing_down = MagicMock(return_value=[])
        tabled.teardown_scope = lambda on_pop=None: _TeardownScope({}, on_pop)
        kwargs, popped = teardown_capture(tabled, "subagent:tabled")
        assert isinstance(kwargs.get("scope"), _TeardownScope) and popped == []

    @pytest.mark.asyncio
    async def test_the_popped_session_that_is_already_in_the_snapshot_is_one_kill(self) -> None:
        """Control: the run's own session popped by its own reset is not killed twice."""
        manager, info, key = _overdue_run("late4", pid=9906)
        own = manager._sessions._sessions[key]
        _hooks, popped = _reset_pops_a_late_successor(manager, own)
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch("kiro_crew.subagent._RESET_TIMEOUT", 0.05),
            children,
            alive,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_group") as group_kill,
        ):
            await manager._force_reap("late4", info, 7200.0)

        assert popped.is_set()
        group_kill.assert_called_once_with(9906, platform_compat.SIGKILL)
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "reaped"


# ── Both teardown paths refresh the live tree before taking their handles ──


class TestTeardownRefreshesTheTreeBeforeTheHandle:
    """Children a run's turns started are on the handle both teardown paths take.

    The client's startup record misses them, and only a recorded member may
    vouch for a leaderless group, so each path awaits the live client's
    ``refresh_process_tree`` before ``_sessions_under`` reads the handle.
    """

    _LATE = 5151

    def _late_child_on_refresh(self, manager: SubagentManager, key: str) -> AsyncMock:
        client = manager._sessions._sessions[key].provider._client

        async def _refresh() -> None:
            client._child_pids[self._LATE] = (_START_ID, b"sleep")

        client.refresh_process_tree = AsyncMock(side_effect=_refresh)
        return client.refresh_process_tree

    @staticmethod
    def _handles_taken(spy: MagicMock) -> list[ProcessHandle]:
        return [handle for call in spy.call_args_list for _s, handle in call.args[2]]

    @pytest.mark.asyncio
    async def test_the_reaper_records_a_late_child_before_its_reset(self) -> None:
        manager, info, key = _overdue_run("late-reap", pid=4646)
        refresh = self._late_child_on_refresh(manager, key)
        _hanging_reset(manager)
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch.object(
                manager, "_retain_process_handles", wraps=manager._retain_process_handles
            ) as spy,
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch("kiro_crew.subagent._RESET_TIMEOUT", 0.05),
            children,
            alive,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_group"),
        ):
            await manager._force_reap("late-reap", info, 7200.0)

        refresh.assert_awaited()
        taken = self._handles_taken(spy)
        assert taken and all(
            self._LATE in h.child_pids for h in taken
        ), "the reaper's handle missed the child the run started"

    @pytest.mark.asyncio
    async def test_the_runs_own_teardown_records_a_late_child_before_its_reset(self) -> None:
        manager, info, key = _overdue_run("late-fin", pid=4747)
        refresh = self._late_child_on_refresh(manager, key)
        _hanging_reset(manager)
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch.object(
                manager, "_retain_process_handles", wraps=manager._retain_process_handles
            ) as spy,
            patch("kiro_crew.subagent.sel"),
            patch("kiro_crew.subagent._RESET_TIMEOUT", 0.05),
            children,
            alive,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_group"),
        ):
            await manager._teardown_run_session(info, key)

        refresh.assert_awaited()
        taken = self._handles_taken(spy)
        assert taken and all(
            self._LATE in h.child_pids for h in taken
        ), "the run's own teardown handle missed the child the run started"

    @pytest.mark.asyncio
    async def test_a_hanging_refresh_does_not_stop_the_reap(self) -> None:
        manager, info, key = _overdue_run("late-hang", pid=4848)
        client = manager._sessions._sessions[key].provider._client

        async def _hang() -> None:
            await asyncio.Event().wait()  # never set: only the bound ends it

        client.refresh_process_tree = AsyncMock(side_effect=_hang)
        _hanging_reset(manager)
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.process_identity.TREE_REFRESH_TIMEOUT", 0.05),
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch("kiro_crew.subagent._RESET_TIMEOUT", 0.05),
            children,
            alive,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_group") as group_kill,
        ):
            await manager._force_reap("late-hang", info, 7200.0)

        group_kill.assert_called_once_with(4848, platform_compat.SIGKILL)
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "reaped"

    @pytest.mark.asyncio
    async def test_a_cancel_during_the_refresh_is_recorded_as_an_undecided_kill(self) -> None:
        manager, info, key = _overdue_run("late-cancel", pid=4949)
        client = manager._sessions._sessions[key].provider._client
        entered = asyncio.Event()

        async def _hang() -> None:
            entered.set()
            await asyncio.Event().wait()  # never set: only the cancel ends it

        client.refresh_process_tree = AsyncMock(side_effect=_hang)

        with patch("kiro_crew.subagent.sel"):
            task = asyncio.ensure_future(manager._teardown_run_session(info, key))
            await await_until(entered, what="the refresh to start")
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert "the teardown was cancelled before its kill decided" in (
            info.error or ""
        ), "a cancel during the refresh skipped the undecided-kill record"
        assert manager._process_handles == {}

    @pytest.mark.asyncio
    async def test_the_runs_own_teardown_refreshes_before_it_releases_the_lease(self) -> None:
        """A walk after ``release()`` would leave the key claimable while it runs."""
        manager, info, key = _overdue_run("late-order", pid=5050)
        client = manager._sessions._sessions[key].provider._client
        order: list[str] = []

        async def _refresh() -> None:
            order.append("refresh")

        client.refresh_process_tree = AsyncMock(side_effect=_refresh)
        manager._sessions.release = MagicMock(side_effect=lambda *_a, **_k: order.append("release"))
        manager._sessions.reset = AsyncMock(return_value=True)
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.sel"),
            children,
            alive,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_group"),
        ):
            await manager._teardown_run_session(info, key)

        assert order[:2] == ["refresh", "release"], order
