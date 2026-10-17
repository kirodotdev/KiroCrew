"""Process identity on the reap's kill path.

On Windows a gone root drains its pending tree cleanup and a live root is killed
through a handle pinned on its creation time, never by pid; ``process_survived`` says
False only on evidence the process is gone.
"""

from __future__ import annotations

import threading
from typing import Any
from unittest.mock import AsyncMock, call, patch

import cron_reaper_helpers
import pytest
from cron_reaper_helpers import (
    _START_ID,
    _audited_outcome,
    _group_gone,
    _handle_of,
    _kill_path_stubs,
    _mock_sessions,
    _overdue_reap_fixture,
    _session_with_pid,
    _start_id_reads,
)

from kiro_crew import platform_compat
from kiro_crew.cron import _JOB_TIMEOUT_SECS, CronService
from kiro_crew.process_identity import ProcessHandle, process_survived

#: Every cron reaper suite pins a healthy free-memory reading (see
#: ``ample_host_free_memory``): ``test_cron_reaper.py`` fires cron jobs through the
#: due-scan, which defers firings while the host's memory posture is critical.
pytestmark = pytest.mark.usefixtures("ample_host_free_memory")


# The shared fixtures, bound here by name so pytest registers them for this module.
_kill_seam = cron_reaper_helpers._kill_seam


def _child_helpers() -> Any:
    """The client's child helpers, resolved the way cron resolves them (so patches apply)."""
    from kiro_crew.session import child_process_helpers

    return child_process_helpers()


class TestAGoneWindowsRootDrainsItsPendingTreeCleanup:
    """Windows: a root that exited is not a tree that finished.

    The spawn reserves an exact-handle cleanup for the tree; when the root exits
    but descendants (or cleanup debt) outlive it, the pin for the root's identity
    stays pending. A gone root with a pin pending is drained through
    ``kill_process_tree_pinned`` (off the loop), and only a completed drain is a
    kill; no pin pending is nothing this code can reach. ``IS_WINDOWS`` pinned;
    the platform primitives are stubbed, so this runs on every runner.
    """

    def _windows_gone_root(
        self, svc: CronService, key: str, pid: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        _session_with_pid(svc, key, pid)

    @pytest.mark.asyncio
    async def test_a_pending_cleanup_is_drained_and_a_completed_drain_is_the_kill(
        self, tmp_path: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The root reads back, exits before the pin (``False``), its pending pin is drained: the kill."""
        svc, job, claim = _overdue_reap_fixture(tmp_path, "win-pin1")
        self._windows_gone_root(svc, "cron:win-pin1", 8282, monkeypatch)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
            patch("kiro_crew.platform_compat.windows_tree_cleanup_pending", return_value=True),
            patch(
                "kiro_crew.platform_compat.kill_process_tree_pinned", side_effect=[False, True]
            ) as drain,
        ):
            await svc._force_reap("win-pin1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert drain.call_args_list == [call(8282, _START_ID, platform_compat.SIGKILL)] * 2
        tree_kill.assert_not_awaited()
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_drain_that_does_not_complete_is_a_failed_kill_not_reaped(
        self, tmp_path: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        svc, job, claim = _overdue_reap_fixture(tmp_path, "win-pin2")
        self._windows_gone_root(svc, "cron:win-pin2", 8383, monkeypatch)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()),
            patch("kiro_crew.platform_compat.windows_tree_cleanup_pending", return_value=True),
            patch(
                "kiro_crew.platform_compat.kill_process_tree_pinned",
                side_effect=[False, OSError("Windows tree cleanup incomplete: 2 handles retained")],
            ),
        ):
            await svc._force_reap("win-pin2", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert _audited_outcome(mock_sel) == "failed"
        assert "; kill failed: Windows tree cleanup incomplete for exited pid 8383: OSError: " in (
            job.last_error or ""
        )

    @pytest.mark.asyncio
    async def test_an_identity_that_cannot_be_pinned_is_a_failed_kill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        self._windows_gone_root(svc, "cron:win-pin3", 8484, monkeypatch)
        children, start_id, sweep = _kill_path_stubs()

        with (
            children,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()),
            patch("kiro_crew.platform_compat.windows_tree_cleanup_pending", return_value=True),
            patch("kiro_crew.platform_compat.kill_process_tree_pinned", return_value=False),
        ):
            failure = await svc._sigkill_session("cron:win-pin3", _handle_of(svc, "cron:win-pin3"))

        assert failure == (
            "pid 8484 exited with Windows tree cleanup pending and its identity could not be "
            "pinned; not drained"
        )

    @pytest.mark.asyncio
    async def test_a_pending_cleanup_with_no_recorded_identity_is_a_failed_kill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pin answers by root pid alone; without a creation time nothing can be drained safely."""
        from kiro_crew.process_identity import (
            ProcessHandle,
            kill_verified_process,
            process_survived,
        )

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        handle = ProcessHandle(pid=8686, start_id=None, pgid=None, child_pids={})

        with (
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=None),
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            patch(
                "kiro_crew.platform_compat.windows_tree_cleanup_pending", return_value=True
            ) as pin,
            patch("kiro_crew.platform_compat.kill_process_tree_pinned") as drain,
            patch("kiro_crew.acp.client._kill_escaped_children"),
        ):
            assert process_survived(handle) is True
            failure = await kill_verified_process(
                handle, who="Reaper", key="cron:win-pin5", child_helpers=_child_helpers()
            )

        pin.assert_called_with(8686, None)
        drain.assert_not_called()
        assert failure == (
            "pid 8686 exited with Windows tree cleanup pending and no recorded identity to pin "
            "it by; not drained"
        )

    @pytest.mark.asyncio
    async def test_a_gone_root_with_a_pending_cleanup_survived_a_completed_reset(
        self, tmp_path: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``process_survived`` reads the pin: a completed reset that left cleanup pending is not a reap."""
        svc, job, claim = _overdue_reap_fixture(tmp_path, "win-pin4")
        self._windows_gone_root(svc, "cron:win-pin4", 8585, monkeypatch)
        svc._sessions.reset = AsyncMock(return_value=True)

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            # The creation time still reads back through the held handle; the exit
            # code says gone; the pin says the tree's cleanup is still owed.
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=_START_ID),
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            patch("kiro_crew.platform_compat.windows_tree_cleanup_pending", return_value=True),
            patch("kiro_crew.platform_compat.kill_process_tree_pinned", return_value=True) as drain,
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
        ):
            await svc._force_reap("win-pin4", _JOB_TIMEOUT_SECS + 60, claim=claim)

        # The survivor's identity still reads back, so the live-root path pins
        # and drains it -- never ``taskkill /T`` by number.
        tree_kill.assert_not_awaited()
        drain.assert_called_once_with(8585, _START_ID, platform_compat.SIGKILL)
        assert _audited_outcome(mock_sel) == "reaped"


class TestALiveWindowsRootIsKilledThroughItsPinnedHandle:
    """Windows: a live root's tree is terminated through a handle pinned on its creation time, never by pid.

    ``taskkill /T /PID`` resolves the pid by number when ``taskkill.exe`` runs --
    an executor hop and a process spawn after the identity read that found the
    root alive -- so a pid recycled in that window hands a stranger's tree to
    the kill. The live root goes through ``kill_process_tree_pinned``: the
    process object is opened only when its creation time matches the recorded
    start id and held across the terminate, the descendants are terminated
    through their own verified handles, and only a completed drain is the kill.
    A root whose identity cannot be pinned any more is gone (its pending cleanup
    pin decides, as on POSIX the retained group does), and a drain that raises
    is a failure with no root-only fallback. ``IS_WINDOWS`` pinned, the platform
    primitives stubbed, so this runs on every runner.
    """

    def _windows_live_root(
        self, svc: CronService, key: str, pid: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        _session_with_pid(svc, key, pid)

    @pytest.mark.asyncio
    async def test_a_pid_recycled_after_the_last_identity_read_is_never_signalled_by_number(
        self, tmp_path: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both identity reads pass, the pid is recycled before the signal: the pin refuses and ``taskkill /T`` is never spawned."""
        svc, job, claim = _overdue_reap_fixture(tmp_path, "win-live1")
        self._windows_live_root(svc, "cron:win-live1", 8787, monkeypatch)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_group", return_value=True) as group_kill,
            patch(
                "kiro_crew.platform_compat.kill_pid_async", AsyncMock(return_value=True)
            ) as pid_kill,
            # The recycled pid: no process object with the recorded creation time
            # can be opened any more, and the old identity owes no cleanup.
            patch(
                "kiro_crew.platform_compat.kill_process_tree_pinned", return_value=False
            ) as drain,
            patch("kiro_crew.platform_compat.windows_tree_cleanup_pending", return_value=False),
        ):
            await svc._force_reap("win-live1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert group_kill.call_count == 0, (
            "the Windows kill signalled pid 8787 by number after its identity read: a pid "
            "recycled in between hands a stranger's process tree to taskkill /T"
        )
        pid_kill.assert_not_awaited()
        drain.assert_called_once_with(8787, _START_ID, platform_compat.SIGKILL)
        # The run's root is gone and its tree owes nothing: nothing to kill.
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_recycled_pid_whose_old_tree_still_owes_cleanup_is_a_failed_kill(
        self, tmp_path: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        svc, job, claim = _overdue_reap_fixture(tmp_path, "win-live2")
        self._windows_live_root(svc, "cron:win-live2", 8888, monkeypatch)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
            patch("kiro_crew.platform_compat.kill_process_tree_pinned", return_value=False),
            patch("kiro_crew.platform_compat.windows_tree_cleanup_pending", return_value=True),
        ):
            await svc._force_reap("win-live2", _JOB_TIMEOUT_SECS + 60, claim=claim)

        tree_kill.assert_not_awaited()
        assert _audited_outcome(mock_sel) == "failed"
        assert (
            "; kill failed: pid 8888 exited with Windows tree cleanup pending and its identity "
            "could not be pinned; not drained"
        ) in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_completed_pinned_drain_is_the_kill(
        self, tmp_path: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        svc, job, claim = _overdue_reap_fixture(tmp_path, "win-live3")
        self._windows_live_root(svc, "cron:win-live3", 8989, monkeypatch)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
            patch("kiro_crew.platform_compat.kill_process_tree_pinned", return_value=True) as drain,
            patch("kiro_crew.platform_compat.windows_tree_cleanup_pending") as pin,
        ):
            await svc._force_reap("win-live3", _JOB_TIMEOUT_SECS + 60, claim=claim)

        drain.assert_called_once_with(8989, _START_ID, platform_compat.SIGKILL)
        tree_kill.assert_not_awaited()
        pin.assert_not_called()  # a drained tree asks nothing more
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_pinned_drain_that_raises_is_a_failed_kill_with_no_root_only_fallback(
        self, tmp_path: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        svc, job, claim = _overdue_reap_fixture(tmp_path, "win-live4")
        self._windows_live_root(svc, "cron:win-live4", 9090, monkeypatch)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_pid_async", AsyncMock(return_value=True)
            ) as pid_kill,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_pinned",
                side_effect=OSError("Windows process tree did not drain before the deadline"),
            ),
        ):
            await svc._force_reap("win-live4", _JOB_TIMEOUT_SECS + 60, claim=claim)

        pid_kill.assert_not_awaited()
        assert _audited_outcome(mock_sel) == "failed"
        assert (
            "; kill failed: Windows tree cleanup incomplete for pid 9090: OSError: Windows process "
            "tree did not drain before the deadline"
        ) in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_the_pinned_drain_runs_off_the_event_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The drain waits on the tree's handles, so it must not run on the caller's loop thread."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        self._windows_live_root(svc, "cron:win-live5", 9191, monkeypatch)
        children, start_id, sweep = _kill_path_stubs()
        loop_thread = threading.get_ident()
        drained_on: list[int] = []

        def _drain(pid: int, start: str, sig: int) -> bool:
            drained_on.append(threading.get_ident())
            return True

        with (
            children,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_tree_pinned", side_effect=_drain),
        ):
            assert (
                await svc._sigkill_session("cron:win-live5", _handle_of(svc, "cron:win-live5"))
                is None
            )

        assert drained_on and all(
            ident != loop_thread for ident in drained_on
        ), "the pinned drain ran on the event loop"


class TestProcessSurvived:
    """``process_survived`` says False only on evidence the process is gone.

    Liveness comes from ``pid_exists`` (the exit-code-confirmed predicate); the
    recorded start id guards only a LIVE pid against reuse. The platform seam is
    pinned both ways: the primitives' shapes are stubbed, so the verdict must be
    the same rule on win32 -- where the identity read alone lies about a
    just-exited child -- and on linux, where nothing changes.
    """

    def _handle(
        self,
        pid: int | None = 7070,
        start_id: str | None = _START_ID,
        pgid: int | None = None,
    ) -> ProcessHandle:
        return ProcessHandle(pid=pid, start_id=start_id, pgid=pgid, child_pids={})

    @pytest.mark.parametrize("windows", [True, False], ids=["win32", "linux"])
    def test_a_live_pid_whose_start_id_reads_back_survived(
        self, monkeypatch: pytest.MonkeyPatch, windows: bool
    ) -> None:
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", windows)
        with (
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=_START_ID),
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
        ):
            assert process_survived(self._handle()) is True

    @pytest.mark.parametrize("windows", [True, False], ids=["win32", "linux"])
    def test_an_exited_pid_whose_start_id_still_reads_back_did_not_survive(
        self, monkeypatch: pytest.MonkeyPatch, windows: bool
    ) -> None:
        """Identity is not liveness.

        On win32 the creation FILETIME reads back through a query handle for as
        long as any handle to the exited process object is still held (the
        Proactor transport's, until GC), so a just-exited child answers the
        recorded start id while ``pid_exists`` -- which confirms the exit code --
        says gone: not a survivor. Same rule on linux, where a dead pid has no
        readable start id to begin with.
        """
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", windows)
        with (
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=_START_ID),
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            _group_gone(),
        ):
            assert process_survived(self._handle()) is False

    @pytest.mark.parametrize("windows", [True, False], ids=["win32", "linux"])
    def test_a_live_pid_with_a_different_start_id_is_not_our_process(
        self, monkeypatch: pytest.MonkeyPatch, windows: bool
    ) -> None:
        """A live pid is guarded by identity: a different start id is another process's pid now."""
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", windows)
        with (
            patch("kiro_crew.platform_compat.get_process_start_id", return_value="9999999"),
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            _group_gone(),
        ):
            assert process_survived(self._handle()) is False

    def test_a_recycled_pid_is_decided_by_identity_before_the_platform_is_asked(self) -> None:
        with (
            patch("kiro_crew.platform_compat.get_process_start_id", return_value="9999999"),
            patch("kiro_crew.platform_compat.pid_exists") as exists,
            _group_gone(),
        ):
            assert process_survived(self._handle()) is False
        exists.assert_not_called()

    @pytest.mark.parametrize("windows", [True, False], ids=["win32", "linux"])
    def test_a_gone_leader_whose_group_still_has_members_survived_on_posix_only(
        self, monkeypatch: pytest.MonkeyPatch, windows: bool
    ) -> None:
        """The leader's absence is not the group's: members a late child spawn left keep the run alive.

        On linux the retained group id is probed and a live group is a survivor
        the kill has to answer for. On win32 there are no process groups and
        nothing this code can reach once the root is gone, so the gone root
        stays "nothing to kill" -- the documented platform limitation.
        """
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", windows)
        with (
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=None),
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            patch("kiro_crew.platform_compat.pgroup_exists", return_value=True) as group,
        ):
            assert process_survived(self._handle(pgid=7070)) is (not windows)
        if windows:
            group.assert_not_called()
        else:
            group.assert_called_once_with(7070)

    def test_a_gone_leader_with_no_verified_group_id_is_probed_by_its_pid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """POSIX: a ``start_new_session`` leader's pid IS its group id; with no verified id it is probed, never signalled."""
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        with (
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=None),
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            patch("kiro_crew.platform_compat.pgroup_exists", return_value=True) as group,
        ):
            assert process_survived(self._handle(pgid=None)) is True
        group.assert_called_once_with(7070)

    def test_a_gone_windows_root_whose_tree_cleanup_is_pending_survived(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Windows asks the exact-tree cleanup pin: pending means descendants or debt outlived the root."""
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        with (
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=_START_ID),
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            patch(
                "kiro_crew.platform_compat.windows_tree_cleanup_pending", return_value=True
            ) as pin,
            patch("kiro_crew.platform_compat.pgroup_exists") as group,
        ):
            assert process_survived(self._handle()) is True
        pin.assert_called_once_with(7070, _START_ID)
        group.assert_not_called()

    def test_a_live_pid_with_no_readable_start_id_survived(self) -> None:
        """Alive but unverifiable is the kill's to decide (a named failure), not a gone process."""
        with (
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=None),
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
        ):
            assert process_survived(self._handle()) is True

    @pytest.mark.parametrize("windows", [True, False], ids=["win32", "linux"])
    def test_a_verified_running_recorded_child_means_the_run_s_process_is_standing(
        self, monkeypatch: pytest.MonkeyPatch, windows: bool
    ) -> None:
        """Leader gone, tree gone, but a recorded child whose start id reads back is still running: a survivor.

        The escaped-children sweep refuses a child that ``exec``'d into another
        name (basename check) and reports nothing, so unless the recorded children
        are asked here such a child survives every teardown unreported. A zombie
        child -- signalled, waiting to be reaped -- is a finished process, not a
        survivor; a child whose start id does not read back is another process's.
        """
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", windows)
        child = 8070
        handle = ProcessHandle(
            pid=7070, start_id=_START_ID, pgid=None, child_pids={child: ("5150001", b"node")}
        )
        reads = _start_id_reads({7070: None, child: "5150001"})
        with (
            patch("kiro_crew.platform_compat.get_process_start_id", side_effect=reads),
            patch("kiro_crew.platform_compat.pid_exists", side_effect=lambda pid: pid == child),
            patch("kiro_crew.platform_compat.pid_is_zombie", return_value=False),
            _group_gone(),
            patch("kiro_crew.platform_compat.windows_tree_cleanup_pending", return_value=False),
        ):
            assert process_survived(handle) is True
        with (
            patch("kiro_crew.platform_compat.get_process_start_id", side_effect=reads),
            patch("kiro_crew.platform_compat.pid_exists", side_effect=lambda pid: pid == child),
            patch("kiro_crew.platform_compat.pid_is_zombie", return_value=True),
            _group_gone(),
            patch("kiro_crew.platform_compat.windows_tree_cleanup_pending", return_value=False),
        ):
            assert process_survived(handle) is False, "a zombie child is finished, not standing"
        with (
            patch(
                "kiro_crew.platform_compat.get_process_start_id",
                side_effect=_start_id_reads({7070: None, child: "9999999"}),
            ),
            patch("kiro_crew.platform_compat.pid_exists", side_effect=lambda pid: pid == child),
            patch("kiro_crew.platform_compat.pid_is_zombie", return_value=False),
            _group_gone(),
            patch("kiro_crew.platform_compat.windows_tree_cleanup_pending", return_value=False),
        ):
            assert process_survived(handle) is False, "a recycled child pid is another process's"

    def test_no_usable_pid_did_not_survive(self) -> None:
        with patch("kiro_crew.platform_compat.get_process_start_id") as start_id:
            assert process_survived(self._handle(pid=None)) is False
        start_id.assert_not_called()
