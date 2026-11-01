"""The subagent force-stop path records a failed kill as a failure, on each of its callers.

The reaper never audits ``reaped`` for a process its kill left alive, the run's own teardown
follows the reaper's rules, the Windows tree drain's failure stays a failure, the restart's
orphan reconcile never says ``killed`` for a survivor, a leaked process is ended without a
second notice and without stalling the loop, and the kill failure stays inside the shared
error bound.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
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
    _mock_ctx_builder,
    _mock_sessions,
    _overdue_run,
)

from kiro_crew import platform_compat
from kiro_crew.process_identity import MAX_ERROR_DETAIL_LEN
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.subagent_persistence import (
    create_agent_folder,
    list_leaked_processes,
    mark_delivered,
    prune_stale_tombstones,
    read_tombstone,
    update_state,
)
from kiro_crew.testing.wait import await_until, default_timeout, finished

# The shared fixtures, bound here by name so pytest registers them for this module.
_kill_seam = subagent_force_stop_audit_helpers._kill_seam
_leads_its_own_group = subagent_force_stop_audit_helpers._leads_its_own_group
_no_tree_behind_the_leader = subagent_force_stop_audit_helpers._no_tree_behind_the_leader


# ── A refused or failed SIGKILL is a kill failure, not a reap ──


class TestReaperRecordsAFailedKill:
    """``reaper_force_kill`` never says ``reaped`` for a process the kill left alive.

    ``_sigkill_session`` raises nothing -- the reap must still finish the
    teardown it owns -- but it REPORTS what stopped the kill: the broadcast
    guard's refusal of the pid, or the error the kill raised. ``_force_reap``
    carries that into the run's error text (``…; kill failed: <reason>``) and
    audits ``failed``. A swallowed refusal leaves only a log line and an audit
    that reads ``reaped`` while the process keeps running.
    """

    @pytest.mark.asyncio
    async def test_a_refused_pid_is_audited_as_a_failed_kill_not_reaped(self) -> None:
        manager, info, _key = _overdue_run("refused1", pid=4242)
        _hanging_reset(manager)
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch("kiro_crew.subagent._RESET_TIMEOUT", 0.05),
            children,
            alive,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group",
                MagicMock(side_effect=_REFUSAL),
            ),
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock()) as pid_kill,
            patch("kiro_crew.subagent.write_tombstone") as tombstones,
        ):
            await manager._force_reap("refused1", info, 7200.0)

        # The guard refused the pid outright: nothing was safe to signal, and
        # nothing was.
        pid_kill.assert_not_awaited()
        assert (
            _audit(mock_sel, "reaper_force_kill")["outcome"] == "failed"
        ), "the SEL audit says the process was reaped while it is still alive"
        assert info.error.startswith("Reaped after")
        assert "; kill failed: ValueError: kill_process_group: refusing" in info.error
        assert "4242" in info.error, "the record does not name the refused pid"
        # The run still ended for the record: done, reaped, the slot released --
        # the failure is added, not substituted.
        assert info.done and info.reaped
        assert manager._running_count == 0
        # The process the kill left standing is flagged for the next start.
        assert tombstones.call_args.kwargs["leaked_process"] is True

    @pytest.mark.asyncio
    async def test_a_group_kill_that_raises_with_no_pid_fallback_is_a_failed_kill(self) -> None:
        """EPERM on the group and on the pid: the process is there and unsignalled."""
        manager, info, _key = _overdue_run("eperm1", pid=4343)
        manager._sessions.reset = AsyncMock(side_effect=RuntimeError("reset failed"))
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
                MagicMock(side_effect=PermissionError("[Errno 1] Operation not permitted")),
            ),
            patch(
                "kiro_crew.platform_compat.kill_pid_async",
                AsyncMock(side_effect=PermissionError("[Errno 1] Operation not permitted")),
            ),
        ):
            await manager._force_reap("eperm1", info, 7200.0)

        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "failed"
        assert "; kill failed: PermissionError: " in info.error

    @pytest.mark.asyncio
    async def test_a_kill_path_error_before_the_signal_is_a_failed_kill(self) -> None:
        """The catch-all that only logged: a probe that raises left the process unsignalled."""
        manager, info, _key = _overdue_run("probe1", pid=4444)
        _hanging_reset(manager)

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch("kiro_crew.subagent._RESET_TIMEOUT", 0.05),
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=_START_ID),
            patch(
                "kiro_crew.acp.client._get_child_pids",
                side_effect=RuntimeError("cannot schedule new futures after shutdown"),
            ),
            patch("kiro_crew.platform_compat.kill_process_group") as group_kill,
        ):
            await manager._force_reap("probe1", info, 7200.0)

        group_kill.assert_not_called()
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "failed"
        assert "; kill failed: RuntimeError: cannot schedule" in info.error

    @pytest.mark.asyncio
    async def test_a_delivered_sigkill_is_still_audited_as_reaped(self) -> None:
        manager, info, _key = _overdue_run("killed1", pid=4545)
        _hanging_reset(manager)
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
            await manager._force_reap("killed1", info, 7200.0)

        group_kill.assert_called_once_with(4545, platform_compat.SIGKILL)
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "reaped"
        assert "kill failed" not in info.error

    @pytest.mark.asyncio
    async def test_a_group_that_is_already_gone_is_nothing_to_kill(self) -> None:
        """ProcessLookupError on the group AND the pid: the run's process exited on its own."""
        manager, info, _key = _overdue_run("gone1", pid=4646)
        _hanging_reset(manager)
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch("kiro_crew.subagent._RESET_TIMEOUT", 0.05),
            children,
            alive,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group",
                MagicMock(side_effect=ProcessLookupError("[Errno 3] No such process")),
            ),
            patch(
                "kiro_crew.platform_compat.kill_pid_async",
                AsyncMock(side_effect=ProcessLookupError("[Errno 3] No such process")),
            ),
        ):
            await manager._force_reap("gone1", info, 7200.0)

        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "reaped"
        assert "kill failed" not in info.error

    @pytest.mark.asyncio
    async def test_a_user_stop_whose_kill_failed_stays_stopped_but_names_the_failure(
        self,
    ) -> None:
        """A neutral stop keeps its outcome; the process it left alive is still on the record."""
        manager, info, _key = _overdue_run("stop1", pid=4747, user_stopped=True)
        _hanging_reset(manager)
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch("kiro_crew.subagent._RESET_TIMEOUT", 0.05),
            children,
            alive,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group",
                MagicMock(side_effect=_REFUSAL),
            ),
        ):
            await manager._force_reap("stop1", info, 60.0, reason="user_stop")

        assert info.outcome == "stopped", "the failed kill must not turn a user stop into a failure"
        assert info.error == (
            "kill failed: ValueError: kill_process_group: refusing broadcast/self process group 4242"
        )
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "failed"


# ── The run's own finally-reset: the other caller ──


class TestTheRunsOwnTeardownRecordsTheKill:
    """``run_finally_force_kill`` follows the same rules as the reaper's audit."""

    @pytest.mark.asyncio
    async def test_a_refused_kill_in_the_runs_finally_is_audited_failed_not_sigkill(
        self,
    ) -> None:
        manager, info, key = _overdue_run("fin1", pid=7070)
        _hanging_reset(manager)
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch("kiro_crew.subagent._RESET_TIMEOUT", 0.05),
            children,
            alive,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group",
                MagicMock(side_effect=_REFUSAL),
            ),
        ):
            await manager._teardown_run_session(info, key)

        row = _audit(mock_sel, "run_finally_force_kill")
        assert row["outcome"] == "failed", "the audit says SIGKILL while the process is alive"
        assert "ValueError: kill_process_group: refusing" in row["error"]
        assert manager._process_handles == {}, "the retained handle outlived its decision"

    @pytest.mark.asyncio
    async def test_a_delivered_kill_in_the_runs_finally_is_audited_sigkill(self) -> None:
        manager, info, key = _overdue_run("fin2", pid=7171)
        _hanging_reset(manager)
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch("kiro_crew.subagent._RESET_TIMEOUT", 0.05),
            children,
            alive,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_group") as group_kill,
        ):
            await manager._teardown_run_session(info, key)

        group_kill.assert_called_once_with(7171, platform_compat.SIGKILL)
        row = _audit(mock_sel, "run_finally_force_kill")
        assert row["outcome"] == "sigkill"
        assert row["error"] == ""

    @pytest.mark.asyncio
    async def test_a_process_that_survives_the_runs_own_reset_gets_the_fallback(self) -> None:
        manager, info, key = _overdue_run("fin3", pid=7272)
        manager._sessions.reset = AsyncMock(return_value=True)
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.sel") as mock_sel,
            children,
            alive,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_group") as group_kill,
        ):
            await manager._teardown_run_session(info, key)

        group_kill.assert_called_once_with(7272, platform_compat.SIGKILL)
        assert _audit(mock_sel, "run_finally_force_kill")["outcome"] == "sigkill"

    @pytest.mark.asyncio
    async def test_a_reset_that_ends_the_process_writes_no_kill_audit(self) -> None:
        """Control: the reset did its job, so there is no fallback to audit."""
        manager, info, key = _overdue_run("fin4", pid=7373)
        manager._sessions.reset = AsyncMock(return_value=True)

        with (
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=None),
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            patch("kiro_crew.platform_compat.kill_process_group") as group_kill,
        ):
            await manager._teardown_run_session(info, key)

        group_kill.assert_not_called()
        mock_sel().log_tool_invocation.assert_not_called()
        assert manager._process_handles == {}


# ── Windows shape ──


class TestWindowsShape:
    """On win32 the pinned tree drain is the only tree walker, so a raised drain stays a failure."""

    @pytest.mark.asyncio
    async def test_on_windows_a_raised_tree_drain_is_a_failure_no_pid_signal_clears(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager, _info, key = _overdue_run("win1", pid=9090)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            children,
            alive,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_pinned",
                side_effect=OSError("taskkill /T failed (exit 128)"),
            ),
            patch(
                "kiro_crew.platform_compat.kill_pid_async", AsyncMock(return_value=True)
            ) as pid_kill,
        ):
            failure = await manager._sigkill_session(key, _handle_of(manager, "win1"))

        assert failure == (
            "Windows tree cleanup incomplete for pid 9090: OSError: taskkill /T failed (exit 128)"
        )
        pid_kill.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_on_windows_a_tree_that_is_already_gone_is_nothing_to_kill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pin finds the identity gone (not drained) and no cleanup is pending: nothing to kill."""
        manager, _info, key = _overdue_run("win2", pid=9191)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            children,
            alive,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_tree_pinned", return_value=False),
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock(return_value=True)),
        ):
            assert await manager._sigkill_session(key, _handle_of(manager, "win2")) is None

    @pytest.mark.asyncio
    async def test_on_posix_a_root_only_fallback_that_lands_is_a_delivered_kill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Control: on POSIX a group signal that fails falls back to the leader's pid and lands."""
        manager, _info, key = _overdue_run("posix1", pid=9292)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            children,
            alive,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group",
                MagicMock(side_effect=PermissionError("[Errno 1] Operation not permitted")),
            ),
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock(return_value=True)),
        ):
            assert await manager._sigkill_session(key, _handle_of(manager, "posix1")) is None


@pytest.fixture()
def agent_root(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Point persistence at a temp directory."""
    monkeypatch.setattr("kiro_crew.subagent_persistence._SUBAGENTS_DIR", tmp_path)
    return tmp_path


def _orphan_on_disk(agent_id: str, pid: int) -> SubagentManager:
    """A prior gateway run's folder with a live-looking pid and no tombstone."""
    manager = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx_builder())
    create_agent_folder(agent_id, task="stuck task")
    update_state(agent_id, pid=pid)
    return manager


class TestOrphanReconcileRecordsAFailedKill:
    """``orphan_reconcile_kill`` never says ``killed`` for a process the kill left standing.

    The restart reconciliation is the third caller of a best-effort kill (after
    the reaper and the run's own teardown); the defect shape these tests pin is
    a ``_kill_orphan_pid`` that returns nothing and swallows every error while
    its caller audits ``killed`` regardless -- the same shape the two other
    paths had.
    """

    @pytest.mark.asyncio
    async def test_a_refused_kill_is_audited_failed_with_its_reason(self, agent_root: Any) -> None:
        manager = _orphan_on_disk("orphan-refused", pid=4242)

        with (
            patch.object(manager, "_is_pid_alive", return_value=True),
            patch.object(manager, "_is_orphan_process", return_value=True),
            patch.object(
                platform_compat,
                "kill_pid",
                side_effect=PermissionError(1, "Operation not permitted"),
            ),
            patch("kiro_crew.subagent.sel") as mock_sel,
        ):
            await manager._reconcile_orphans()

        row = _audit(mock_sel, "orphan_reconcile_kill")
        assert (
            row["outcome"] == "failed"
        ), f"orphan_reconcile_kill audited {row['outcome']!r} for a process the kill left standing"
        assert row["error"].startswith("PermissionError")
        # The folder is still reconciled: the record ends, the audit says the process did not.
        assert (agent_root / "orphan-refused" / "tombstone.json").exists()

    @pytest.mark.asyncio
    async def test_a_process_already_gone_at_the_signal_keeps_killed(self, agent_root: Any) -> None:
        """Nothing to kill is not a failure: the process the kill was after is gone."""
        manager = _orphan_on_disk("orphan-gone", pid=4243)

        with (
            patch.object(manager, "_is_pid_alive", return_value=True),
            patch.object(manager, "_is_orphan_process", return_value=True),
            patch.object(platform_compat, "kill_pid", side_effect=ProcessLookupError),
            patch("kiro_crew.subagent.sel") as mock_sel,
        ):
            await manager._reconcile_orphans()

        row = _audit(mock_sel, "orphan_reconcile_kill")
        assert row["outcome"] == "killed"
        assert row["error"] == ""

    @pytest.mark.asyncio
    async def test_a_delivered_kill_is_audited_killed(self, agent_root: Any) -> None:
        manager = _orphan_on_disk("orphan-killed", pid=4244)

        with (
            patch.object(manager, "_is_pid_alive", return_value=True),
            patch.object(manager, "_is_orphan_process", return_value=True),
            patch.object(platform_compat, "kill_pid") as kill,
            patch("kiro_crew.subagent.sel") as mock_sel,
        ):
            await manager._reconcile_orphans()

        assert kill.call_args[0][0] == 4244
        assert _audit(mock_sel, "orphan_reconcile_kill")["outcome"] == "killed"


class TestALeakedProcessIsEndedWithoutASecondNotice:
    """A completed run whose kill failed is announced once, and its process is still ended.

    The run's own report already handed the parent its completion (naming the
    survivor). Recorded as an orphan, the next start would end the process and
    announce the same completion again; recorded as a leaked process, it ends the
    process and says nothing.
    """

    @staticmethod
    def _completed_run_on_disk(
        agent_id: str, pid: int
    ) -> tuple[SubagentManager, SubagentInfo, str]:
        create_agent_folder(agent_id, task="finish, and leave the process standing")
        update_state(agent_id, pid=pid, result_complete=True)
        manager, info, key = _overdue_run(agent_id, pid=pid)
        info.result = "the answer"
        info._pid = pid
        return manager, info, key

    @staticmethod
    async def _restart(
        agent_id: str, *, kill: Any, verified: bool = True
    ) -> tuple[MagicMock, AsyncMock]:
        """Run the next start's reconciliation over the folder; return the audit and the notifier.

        ``verified`` False is a live pid the identity check cannot vouch for:
        no ``/proc`` (macOS, Windows), or a ``/proc`` entry newer than the spawn.
        """
        restarted = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx_builder())
        notify = AsyncMock(return_value=None)
        with (
            patch.object(restarted, "_is_pid_alive", return_value=True),
            patch.object(restarted, "_is_orphan_process", return_value=verified),
            patch.object(restarted, "_notify_orphan", notify),
            patch.object(platform_compat, "kill_pid", **kill),
            patch("kiro_crew.subagent.sel") as mock_sel,
        ):
            await restarted._reconcile_orphans()
        return mock_sel, notify

    @staticmethod
    async def _fail_the_kill(
        manager: SubagentManager,
        info: SubagentInfo,
        key: str,
        *,
        queued: bool = False,
        undelivered: bool = False,
    ) -> None:
        """The run's teardown with a refused kill, then its terminal report to the parent.

        ``queued`` is a parent that only parked the completion (``_on_done``
        returned with ``_delivery_queued`` set), so nothing acknowledged it yet.
        """
        _hanging_reset(manager)
        children, alive, start_id, sweep = _kill_path_stubs()
        with (
            patch("kiro_crew.subagent.sel"),
            patch("kiro_crew.subagent._RESET_TIMEOUT", 0.05),
            children,
            alive,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group",
                MagicMock(side_effect=_REFUSAL),
            ),
        ):
            await manager._teardown_run_session(info, key)

        async def _parent_takes_it(run: SubagentInfo) -> None:
            run._delivery_queued = queued
            run._report_undelivered = undelivered

        manager._on_done = AsyncMock(side_effect=_parent_takes_it)
        await manager._report_terminal(
            info,
            source="Subagent",
            injection_timeout_reason="test",
            mark_delivered_on_success=True,
        )

    @pytest.mark.asyncio
    async def test_the_next_start_ends_the_survivor_and_does_not_announce_it_again(
        self, agent_root: Any
    ) -> None:
        manager, info, key = self._completed_run_on_disk("leak-once", pid=5151)
        await self._fail_the_kill(manager, info, key)
        # A completed run keeps its clean record; the survivor is the flag's.
        assert not info.error and info._leaked_process is True

        mock_sel, notify = await self._restart("leak-once", kill={})

        notify.assert_not_awaited()  # the parent already holds this completion
        row = _audit(mock_sel, "orphan_reconcile_kill")
        assert row["outcome"] == "killed" and row["metadata"]["pid"] == 5151
        tombstone = read_tombstone("leak-once")
        assert tombstone is not None
        assert "leaked_process" not in tombstone, "a process already ended is retried forever"
        assert tombstone["outcome"] == "completed", "clearing the flag lost the record"

    @pytest.mark.asyncio
    async def test_a_kill_that_fails_again_keeps_the_flag_for_the_start_after(
        self, agent_root: Any
    ) -> None:
        manager, info, key = self._completed_run_on_disk("leak-again", pid=5252)
        await self._fail_the_kill(manager, info, key)

        mock_sel, notify = await self._restart(
            "leak-again", kill={"side_effect": PermissionError(1, "Operation not permitted")}
        )

        notify.assert_not_awaited()
        assert _audit(mock_sel, "orphan_reconcile_kill")["outcome"] == "failed"
        assert (read_tombstone("leak-again") or {}).get("leaked_process") is True

    @pytest.mark.asyncio
    async def test_a_live_pid_it_cannot_verify_is_reported_as_possibly_running(
        self, agent_root: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        manager, info, key = self._completed_run_on_disk("leak-unverified", pid=5757)
        await self._fail_the_kill(manager, info, key)
        kill = MagicMock()

        with caplog.at_level(logging.INFO, logger="kiro_crew"):
            mock_sel, notify = await self._restart(
                "leak-unverified", kill={"new": kill}, verified=False
            )

        notify.assert_not_awaited()
        kill.assert_not_called()
        warned = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any(
            "5757" in r.getMessage() and "may still be running" in r.getMessage() for r in warned
        ), "a process left standing was logged as gone"
        assert not any(
            "not running" in r.getMessage() and "leak-unverified" in r.getMessage()
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_a_delivery_acknowledgement_does_not_erase_the_flag(
        self, agent_root: Any
    ) -> None:
        manager, info, key = self._completed_run_on_disk("leak-ack", pid=5353)
        await self._fail_the_kill(manager, info, key)

        mark_delivered("leak-ack", elapsed=1.0, credits=0.0)

        tombstone = read_tombstone("leak-ack") or {}
        assert tombstone.get("leaked_process") is True
        assert [state["id"] for state in list_leaked_processes()] == ["leak-ack"]

    @pytest.mark.asyncio
    async def test_a_flagged_folder_with_no_readable_state_drops_its_flag(
        self, agent_root: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        manager, info, key = self._completed_run_on_disk("leak-nostate", pid=5858)
        await self._fail_the_kill(manager, info, key)
        (agent_root / "leak-nostate" / "state.json").unlink()

        with caplog.at_level(logging.INFO, logger="kiro_crew"):
            _mock_sel, notify = await self._restart("leak-nostate", kill={})

        notify.assert_not_awaited()
        assert "leaked_process" not in (
            read_tombstone("leak-nostate") or {}
        ), "a flagged folder with no state was kept, and the pruner keeps it forever"
        assert any(
            "leak-nostate" in r.getMessage() and "names no pid" in r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING
        )

    @pytest.mark.asyncio
    async def test_a_refused_kill_is_not_logged_as_ended(
        self, agent_root: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        manager, info, key = self._completed_run_on_disk("leak-refused", pid=5959)
        await self._fail_the_kill(manager, info, key)
        kill = MagicMock()

        with (
            caplog.at_level(logging.INFO, logger="kiro_crew"),
            patch("kiro_crew.runtime_ownership.authorize_runtime_kill", return_value=False),
        ):
            mock_sel, _notify = await self._restart("leak-refused", kill={"new": kill})

        kill.assert_not_called()
        assert _audit(mock_sel, "orphan_reconcile_kill")["outcome"] == "refused"
        mine = [r.getMessage() for r in caplog.records if "leak-refused" in r.getMessage()]
        assert not any(m.startswith("Ended") for m in mine), mine
        assert any("refused" in m for m in mine), mine

    @pytest.mark.asyncio
    async def test_an_acknowledged_leaked_completion_cleans_the_workspace_copy(
        self, agent_root: Any, tmp_path: Any
    ) -> None:
        manager, info, key = self._completed_run_on_disk("leak-ws", pid=6060)
        ws_copy = tmp_path / "agent-leak-ws.md"
        ws_copy.write_text("the answer")

        with patch("kiro_crew.subagent._ws_result_path", return_value=ws_copy):
            await self._fail_the_kill(manager, info, key)

        assert (read_tombstone("leak-ws") or {}).get("leaked_process") is True
        assert not ws_copy.exists(), "the delivered leaked run kept the parent's workspace copy"

    @pytest.mark.asyncio
    async def test_a_completion_still_queued_at_restart_is_delivered_again(
        self, agent_root: Any
    ) -> None:
        """A parent that only queued the completion has not received it: restart re-delivers."""
        manager, info, key = self._completed_run_on_disk("leak-queued", pid=5555)
        await self._fail_the_kill(manager, info, key, queued=True)

        assert (
            read_tombstone("leak-queued") is None
        ), "a queued completion was excluded from recovery"
        _mock_sel, notify = await self._restart("leak-queued", kill={})

        notify.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_completion_whose_injection_was_given_up_is_delivered_again(
        self, agent_root: Any
    ) -> None:
        """An injection the gateway gave up on returns normally; the parent still lacks it."""
        manager, info, key = self._completed_run_on_disk("leak-undelivered", pid=6161)
        await self._fail_the_kill(manager, info, key, undelivered=True)

        assert (
            read_tombstone("leak-undelivered") is None
        ), "an undelivered completion was excluded from recovery"
        _mock_sel, notify = await self._restart("leak-undelivered", kill={})

        notify.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_the_pruner_keeps_a_flagged_folder_past_its_window(self, agent_root: Any) -> None:
        manager, info, key = self._completed_run_on_disk("leak-old", pid=5656)
        await self._fail_the_kill(manager, info, key)

        # Aged on disk rather than by moving the clock: the tombstone's own
        # ``died`` is what the pruner compares against its windows.
        path = agent_root / "leak-old" / "tombstone.json"
        tombstone = json.loads(path.read_text())
        tombstone["died"] = time.time() - 30 * 86400
        path.write_text(json.dumps(tombstone))

        prune_stale_tombstones(max_age_days=7, delivered_ttl_secs=3600)

        assert [state["id"] for state in list_leaked_processes()] == ["leak-old"]

    @pytest.mark.asyncio
    async def test_a_plain_orphan_is_still_announced(self, agent_root: Any) -> None:
        """Control: a run with no tombstone is an undelivered result and keeps its notice."""
        _orphan_on_disk("orphan-plain", pid=5454)

        _mock_sel, notify = await self._restart("orphan-plain", kill={})

        notify.assert_awaited_once()
        assert list_leaked_processes() == []


class TestTheOrphanKillLeavesTheLoopRunning:
    """The orphan kill is awaited through ``kill_pid_async``: its Windows arm runs off the loop.

    The reconciliation is a coroutine on the gateway's event loop, and the
    Windows kill is a ``taskkill`` spawn that waits up to five seconds for the
    target. Signalling it synchronously from the coroutine stalled the loop for
    that wait, once per live orphan, during startup.
    """

    @pytest.mark.asyncio
    async def test_the_reconciliation_awaits_the_kill_off_the_loop(self, agent_root: Any) -> None:
        manager = _orphan_on_disk("orphan-awaited", pid=4245)
        loop = asyncio.get_running_loop()
        served = asyncio.Event()

        async def slow_kill(pid: int, sig: int) -> bool:
            # The taskkill wait, as the executor hop presents it: the kill stays in
            # flight until the loop has served a callback queued after it began.
            loop.call_soon(served.set)
            await await_until(served, what="a loop callback queued during the kill")
            return True

        with (
            patch.object(manager, "_is_pid_alive", return_value=True),
            patch.object(manager, "_is_orphan_process", return_value=True),
            patch.object(platform_compat, "kill_pid_async", side_effect=slow_kill) as kill,
            patch("kiro_crew.subagent.sel") as mock_sel,
        ):
            await finished(manager._reconcile_orphans(), "the orphan reconciliation")

        kill.assert_awaited_once_with(4245, platform_compat.SIGKILL)
        assert served.is_set(), "the loop served nothing while the orphan kill waited"
        assert _audit(mock_sel, "orphan_reconcile_kill")["outcome"] == "killed"
        assert (agent_root / "orphan-awaited" / "tombstone.json").exists()

    @pytest.mark.asyncio
    async def test_a_slow_windows_kill_does_not_stall_the_loop(self) -> None:
        """The Windows arm's blocking ``taskkill`` wait runs on the subprocess executor."""
        loop = asyncio.get_running_loop()
        served = threading.Event()
        waited_off_the_loop: list[bool] = []

        def taskkill_wait(pid: int, sig: int) -> bool:
            # taskkill waiting on the target, on whichever thread runs it: it returns
            # once the loop has served a callback queued after the wait began. On the
            # loop thread that callback can never run, so the wait reaches its bound.
            loop.call_soon_threadsafe(served.set)
            waited_off_the_loop.append(served.wait(timeout=default_timeout()))
            return True

        with (
            patch.object(platform_compat, "IS_POSIX", False),
            patch.object(platform_compat, "kill_pid", side_effect=taskkill_wait) as kill,
        ):
            await finished(SubagentManager._kill_orphan_pid(4246), "the orphan kill")

        kill.assert_called_once_with(4246, platform_compat.SIGKILL)
        assert waited_off_the_loop == [
            True
        ], "the loop served nothing during the taskkill wait: the kill ran on the loop thread"

    @pytest.mark.asyncio
    async def test_a_failure_off_the_loop_is_still_named(self) -> None:
        with (
            patch.object(platform_compat, "IS_POSIX", False),
            patch.object(
                platform_compat,
                "kill_pid",
                side_effect=PermissionError(1, "Operation not permitted"),
            ),
        ):
            failure = await SubagentManager._kill_orphan_pid(4247)

        assert failure is not None and failure.startswith("PermissionError")


class TestTheKillFailureStaysInsideTheErrorBound:
    """The reap retains a bounded ``info.error`` through the shared ``with_kill_failure``.

    Every writer of ``info.error`` holds it to ``MAX_ERROR_DETAIL_LEN`` -- the
    one bound in ``kiro_crew.process_identity`` the sub-agent record and the cron
    job's ``last_error`` share -- at the point of retention, and the record, the
    tombstone and the report all carry the string as is. The kill's reason is
    not this code's to size: a Windows tree drain that fails carries the tree's
    own detail, one line per process the run spawned, and the error it is
    appended to may already sit at the bound. The joining is the shared home's
    (its own tests pin the helper's arithmetic: the reserve, the trim, the
    verbatim short case); what is pinned HERE is that the sub-agent reap goes
    through it.
    """

    @pytest.mark.asyncio
    async def test_a_kill_reason_the_size_of_a_tree_does_not_push_the_record_past_the_bound(
        self,
    ) -> None:
        manager, info, _key = _overdue_run("bounded1", pid=4343)
        manager._sessions.reset = AsyncMock(side_effect=RuntimeError("reset failed"))
        blob = "\n".join(
            f"ERROR: The process with PID {4343 + n} could not be terminated." for n in range(200)
        )
        assert len(blob) > MAX_ERROR_DETAIL_LEN
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
                MagicMock(side_effect=PermissionError(blob)),
            ),
            patch(
                "kiro_crew.platform_compat.kill_pid_async",
                AsyncMock(side_effect=PermissionError(blob)),
            ),
        ):
            await manager._force_reap("bounded1", info, 7200.0)

        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "failed"
        assert len(info.error) <= MAX_ERROR_DETAIL_LEN, (
            f"the kill-failure suffix pushed the retained error to {len(info.error)} chars, "
            f"past the {MAX_ERROR_DETAIL_LEN} bound"
        )
        # The failure is what the record exists to name: it survives the trim,
        # head first, next to the run's own error.
        assert info.error.startswith("Reaped after")
        assert "; kill failed: PermissionError: ERROR: The process with PID 4343" in info.error
