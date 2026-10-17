"""The subagent force-stop path records what actually happened to the process.

Twin of the cron reaper's contract (``test_cron_reaper_failed_kill.py``): ``_sigkill_session``
REPORTS a refused or failed signal instead of swallowing it, its callers audit
that as ``failed`` rather than ``reaped`` / ``sigkill`` and name it in the run's
error text, and both callers act on a process handle taken BEFORE the reset --
retained under the run's id -- so a stop that arrives after the reset has popped
the session from the map, or after a completed reset that did not end the
process, still names, verifies and signals the process.

Every signal seam is stubbed (``platform_compat.kill_process_group`` /
``kill_pid_async`` / ``kill_process_tree_pinned`` / ``get_process_start_id`` /
``pid_exists`` and the child helpers in ``acp.client``), so no test here touches
a real process. The kill
and the survival check are ``kiro_crew.process_identity``'s, shared with the
cron reaper; once a leader is gone they decide by the tree it led, and those
two probes -- the POSIX process-group probe and the Windows exact-tree cleanup
pin -- are pinned "gone" for this whole module (:func:`_no_tree_behind_the_leader`):
no test here builds a tree, and an unpinned ``os.killpg(number, 0)`` on a
made-up pid answers whatever the host running the suite happens to run. The
tree cases themselves are the shared module's, proven in ``test_cron_reaper_kill_set.py``
(the process group) and ``test_cron_reaper_process_identity.py`` (the Windows tree pin).

The suite is split by behaviour. This file keeps the process handles the kill acts on: a
handle taken before a reset that pops the session and hangs, a completed reset that is not
proof of death, equal handles merging their evidence, the handle retained across the pop,
every candidate under a shared key killed on its own handle, the session a reset pops, and
the live tree refreshed before either teardown takes its handles. The run's own record and
report are in ``test_subagent_force_stop_audit_reports.py``, a failed kill on each of its
three callers in ``test_subagent_force_stop_audit_failed_kills.py``, and the shared seams
and fakes in ``subagent_force_stop_audit_helpers.py``.
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
        """The start id is re-read immediately before the signal; a changed reading is a gone root, and a child the walk found is named.

        The child the walk FOUND (7272) was read through a pid that may already
        have been recycled when the walk ran, so it is not swept -- and not
        dropped either: the kill names it as a failure, unattributed and not
        signalled, so the reap records ``failed`` rather than ``reaped`` over a
        process that may be the run's. Only the child recorded before the reset
        (6262) is swept.
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

        On Windows a start id still reads back for an exited process while any
        handle to it is open, so the pre-signal identity read passes and the
        kill reaches the pinned tree drain (``kill_process_tree_pinned``), which
        opens the process object only if its creation time is the one just read
        back -- and finds the identity does not answer (not drained). The
        leader's absence hands the decision to its tree: with no exact-tree
        cleanup pin pending for the root there is nothing left to reach, no
        pid-scoped signal is attempted, and the kill reports no failure.
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

        The reset's own shutdown can fail without raising out of it. After every
        completed reset the handle is asked -- pid plus recorded start id -- and
        a process that still stands gets the fallback; its outcome, not the
        reset's boolean, is what the record says.
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

        A start id reads back for an EXITED Windows process as long as any handle
        to its kernel object is open (asyncio's Proactor transport keeps one until
        GC). The survivor check asks existence FIRST through the exit-code-confirmed
        probe (``pid_exists`` on win32), so the fallback never signals a dead pid
        and never records that error as a failed kill.
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

        A concurrent reset popped the entry between the reap's handle and the
        reset's lock. Whether that reset's shutdown lands is not this reap's to
        assume: the handle says the process is standing (its recorded start id
        reads back), so the kill goes through the handle and the record says
        reaped only once it has been signalled.
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

    Identity is ``(pid, start id)``; the group id and the child records are
    ``compare=False`` evidence read at different moments. The pre-reset snapshot
    names the children the client had recorded THEN; the handle read at the pop
    -- or the live session's, beside an entry the other path retained minutes
    earlier -- names the ones recorded since. A dedup that kept the first equal
    handle dropped the later records: a recorded child that escaped the group
    was never swept, never named as standing, and the record said ``reaped``
    over it. One shared rule (``process_identity.add_handle``) for the kill set,
    the sub-agent snapshot and the cron reaper's.
    """

    def test_the_kill_set_keeps_the_popped_readings_children(self) -> None:
        # This PR's shared definition: imported here so the module still imports
        # on the parent for the red-first count.
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
        # Imported here, not at the top: the module must still import on the
        # parent for the red-first count, and these two names are this fix's.
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
    """Two sessions can stand under one key at snapshot time: the run's own, popped
    and retained by the teardown that hangs, and a live successor a cold start
    registered under the key during that teardown's awaits. The reap ends the
    KEY -- its reset pops the successor too -- so both are candidates, each
    verified and signalled on its own handle; preferring either alone leaves
    the other's process unrecorded.
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

        The run's teardown retained its process; that process exited and a live
        successor under the same key was handed its pid. Neither handle stands
        in for the other. The successor is what is alive under the pid, so only
        its handle verifies and signals it. The stale handle is the only way
        back to what the exited root left behind: on Windows the exact-tree
        cleanup pin the spawn reserved under (pid, old start id) may still be
        pending -- pins are keyed by identity, so it co-exists with the
        successor holding the pid -- and on POSIX the group the old leader led
        may still hold a late child under the retained group id. De-duplicating
        by pid alone kept the stale handle and dropped the successor (it ran on,
        audited ``reaped``); replacing the stale handle with the later reading
        dropped the pin and the group instead (the old tree ran on, audited
        ``reaped``). Only an exact (pid, start id) match is one process.
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

        The pid reads back with the successor's start id, so the stale handle
        is the recycled case: its leader is gone, the exact-tree cleanup pin
        under its own identity is still pending, and the drain through that pin
        is the kill. The successor's handle verifies and ``taskkill /T``s the
        process now holding the pid. Both landed: the record says ``reaped``.
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

        Neither teardown path retained a handle (a dashboard reset or
        ``cancel_all`` popped the session), so the table and the live map are
        both empty; the session manager keeps the popped session readable
        through ``tearing_down`` for the life of its teardown, and the reap
        reads it there.
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

        ``SessionManager.tearing_down`` returns EVERY session a reset popped
        under the key whose teardown has not ended, in pop order: a cold start
        registered a successor while the first teardown hung, and the
        successor's own reset popped it and hung too. A reap that read the list
        as one session would find no pid on it and stop neither process, while
        its record said ``reaped``.
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
        """The snapshot keeps each ``TornDown`` entry's own ``handle``; it never re-derives one from the entry.

        ``tearing_down`` returns ``TornDown`` records -- the popped session and
        the process handle read AT THE POP -- not sessions. Reading a record as
        a session finds no provider on it, names no pid, and drops the
        teardown's process from the snapshot; re-reading the popped session
        instead finds the pid the hung teardown has since cleared. Only the
        captured handle names the process that is still standing.
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
        """A hung teardown whose wrapper names no pid any more is still killed, on the handle captured at its pop.

        The reset that popped the session hangs after clearing the client's pid
        (the ACP client's reset clears it after a kill it could not confirm, then
        hangs on the transport). The reap that reads the entry as a session, or
        re-reads the popped session, finds no process, signals nothing, and
        records ``reaped`` while the process runs on. Killing on the captured
        handle reaches it, and the record says what happened.
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
        """The snapshot's fail-open reads are met by the real ``SessionManager``, not only by these fakes.

        ``_retain_process_handles`` reads the live table through
        ``getattr(sessions, "_sessions")`` as a ``Mapping`` and the teardowns in
        flight through ``tearing_down``; a map exposing neither is a miss, not an
        error, because the read runs ahead of every reset. That tolerance is only
        safe while the real class satisfies both reads -- a rename there would
        turn every reap into "nothing to stop" without a test going red -- so
        the real manager is pinned here.
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
    """A session manager whose reset pops ``successor`` -- registered AFTER the snapshot -- and hangs.

    ``teardown_scope(on_pop=...)`` records the hook a teardown path hands it and
    answers the lifecycle's own scope (the only one ``teardown_capture`` hands
    on); the reset then reports the session it pops through that scope
    (``scope.note_pop``), in the same lock hold as the pop, exactly as
    ``SessionLifecycle.reset`` does, and hangs. Returns the recorded hooks
    (empty when the path never asked for a scope) and the event set once the
    pop happened.
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

    The snapshot (``_retain_process_handles``) is taken before the reset. A
    cold start -- a queued ``spawn_continue`` dispatching as the run's record
    flips -- can register a new session under the key between that snapshot
    and the reset's pop; it is THAT session the reset pops and hangs on, and a
    fallback fed the snapshot alone kills the stale process and records the
    run reaped while the successor runs on. Both teardown paths hand the reset
    a scope whose ``on_pop`` hook takes the popped session's handle atomically
    with the pop (``SessionManager.teardown_scope``, the cron reaper's shape);
    a popped session with no recorded pid is a process the run cannot name --
    a named kill failure, never ``reaped``.
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
        """The run's own session runs no process (an in-process provider): its pop names none, and that is not a failure.

        The snapshot read the live session and found no pid; the reset then pops
        that same session. Naming its pop a kill failure audited every such run's
        ordinary teardown as ``failed`` -- and, once the run's record carries the
        teardown's kill result, would have turned a clean completion into
        ``failed``.
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
        """A scope is a hold on the torn-down table; no table (``tearing_down`` answers no list), no scope.

        A double that answers every attribute produces a scope no reset fills, and
        its ``reset`` may take one positional argument only: handing it
        ``scope=`` failed the reset it was meant to observe and left the key
        live (``test_member_continuation_identity.py``).
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
