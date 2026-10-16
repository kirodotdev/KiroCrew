"""Every process the run owns is answered by the reap.

A leaderless POSIX group is killed by its captured group id, a recorded child the
sweep left standing is named, a torn-down session and a live successor under the same
key are both read, and the session the reset pops is captured into the kill set.
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import cron_reaper_helpers
import pytest
from cron_reaper_helpers import (
    _START_ID,
    _audited_outcome,
    _group_gone,
    _handle_of,
    _held_or_landed,
    _isolated_leader,
    _kill_path_stubs,
    _live_task,
    _make_job,
    _mock_sessions,
    _overdue_reap_fixture,
    _parks_at_the_fence_door,
    _retain_torn_down,
    _session_with_pid,
    _start_id_reads,
    _torn_down_by_the_run,
    _within_the_ceiling,
)
from test_update_provider import _UNALLOCATABLE_PID

from kiro_crew import platform_compat
from kiro_crew.cron import _JOB_TIMEOUT_SECS, CronJob, CronService, _RunClaim
from kiro_crew.cron_history import CronHistoryStore
from kiro_crew.process_identity import ProcessHandle, kill_set

#: Every cron reaper suite pins a healthy free-memory reading (see
#: ``ample_host_free_memory``): ``test_cron_reaper.py`` fires cron jobs through the
#: due-scan, which defers firings while the host's memory posture is critical.
pytestmark = pytest.mark.usefixtures("ample_host_free_memory")


# The shared fixtures, bound here by name so pytest registers them for this module.
_kill_seam = cron_reaper_helpers._kill_seam


def _vouching_child(client: MagicMock, cpid: int, start: str = "5150001") -> int:
    """Record ``cpid`` on the client as a descendant whose identity a test can make read back."""
    client._child_pids = {cpid: (start, b"node")}
    return cpid


class TestALeaderlessGroupIsNeverReaped:
    """A dead group leader is not an empty process group.

    A late child spawn followed by the leader's exit leaves members under the
    group id while every pid-addressed lookup of the leader fails, and a kill
    that resolved the group from the pid -- or read the leader's absence as the
    group's -- audited ``reaped`` over processes still running. The handle keeps
    the group id captured while the leader was alive and identity-checked
    (``ProcessHandle.pgid``), and that captured id is the ONLY group the kill
    ever signals (``platform_compat.kill_process_group``, alive leader or gone):
    once the leader is gone the group decides -- members are signalled by the
    captured id once a member verified as this run's vouches for it,
    ``ProcessLookupError`` from that signal means "empty" only for a verified
    group, and a live group that cannot be verified as this run's is a kill
    failure. The POSIX branch is exercised on EVERY platform the suite runs on:
    ``IS_WINDOWS`` is False (the module's autouse kill seam), and the two group
    primitives the capture site reads through ``getattr`` -- ``os.getpgid``,
    supplied per test, and ``os.getpgrp`` -- are patched into existence where the
    runner lacks them, so no shard skips these assertions. The Windows kill
    branch has its own expectations in ``TestReaperRecordsAFailedSigkill`` --
    nothing sweeps descendants there.
    """

    @pytest.fixture(autouse=True)
    def _posix_group_primitives(self) -> Iterator[None]:
        """``os.getpgrp`` exists (our own group is 1) on every runner; each test supplies ``os.getpgid``."""
        with patch("os.getpgrp", return_value=1, create=True):
            yield

    @pytest.mark.asyncio
    async def test_a_leader_that_exits_after_a_late_child_spawn_is_a_failed_kill_not_reaped(
        self, tmp_path: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Leader gone, group alive and vouched, group signal refused (EPERM): a failure, not ``reaped`` -- and no pid fallback for a gone leader."""
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        svc, job, claim = _overdue_reap_fixture(tmp_path, "leaderless1")
        client = _session_with_pid(svc, "cron:leaderless1", 7373)
        child = _vouching_child(client, 8373)

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            # Alive at the snapshot (the bracketed group read), exited by the kill;
            # the late-spawned recorded child still reads back in the group.
            patch(
                "kiro_crew.platform_compat.get_process_start_id",
                side_effect=_start_id_reads({7373: [_START_ID, _START_ID, None], child: "5150001"}),
            ),
            patch("os.getpgid", return_value=7373, create=True),
            patch("kiro_crew.platform_compat.pgroup_of", return_value=7373),
            patch("kiro_crew.platform_compat.pid_exists", side_effect=lambda pid: pid == child),
            patch("kiro_crew.platform_compat.pid_is_zombie", return_value=False),
            patch("kiro_crew.platform_compat.pgroup_exists", return_value=True),
            patch(
                "kiro_crew.platform_compat.kill_process_group",
                side_effect=PermissionError("[Errno 1] Operation not permitted"),
            ) as group_kill,
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock()) as pid_kill,
        ):
            await svc._force_reap("leaderless1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        group_kill.assert_called_once_with(7373, platform_compat.SIGKILL)
        assert (
            _audited_outcome(mock_sel) == "failed"
        ), "the audit says the run was reaped while its leaderless process group is still running"
        assert "; kill failed: PermissionError: " in (job.last_error or "")
        pid_kill.assert_not_awaited()  # a gone leader's pid may be another process's by now
        assert "leaderless1" not in svc._claims

    @pytest.mark.asyncio
    async def test_a_live_group_is_signalled_by_the_group_id_retained_while_the_leader_lived(
        self, tmp_path: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The leader's pid was recycled before the kill; the recorded child still in the group vouches for it; the CAPTURED id is signalled -- nothing is resolved from the recycled pid."""
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        svc, job, claim = _overdue_reap_fixture(tmp_path, "leaderless2")
        client = _session_with_pid(svc, "cron:leaderless2", 7474)
        child = _vouching_child(client, 8474)

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            # The leader's start id reads back at the snapshot (the bracketed group
            # read), then another process owns the pid; the recorded child's reads
            # back throughout.
            patch(
                "kiro_crew.platform_compat.get_process_start_id",
                side_effect=_start_id_reads(
                    {7474: [_START_ID, _START_ID, "9999999"], child: "5150001"}
                ),
            ),
            # ``getpgid`` answers the pid at the snapshot; by the kill the number
            # belongs to a stranger whose group is a different one -- a kill that
            # resolved the group from the pid now would signal THAT group.
            patch("os.getpgid", side_effect=[7474, 7474, 31337, 31337, 31337], create=True),
            patch("kiro_crew.platform_compat.pgroup_of", return_value=7474),
            patch("kiro_crew.platform_compat.pgroup_exists", return_value=True),
            # After the delivered group kill the vouching child sits in the zombie
            # state (its start id still reads back): a finished process, not a
            # survivor of the sweep -- pinned, since the post-sweep recheck reads
            # the child's liveness and a made-up pid answers whatever the host runs.
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.platform_compat.pid_is_zombie", return_value=True),
            patch("kiro_crew.platform_compat.kill_process_group") as group_kill,
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock()) as pid_kill,
        ):
            await svc._force_reap("leaderless2", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert group_kill.call_args_list == [call(7474, platform_compat.SIGKILL)], (
            "the group kill did not address the group id captured with the handle "
            f"(signalled {[c.args[0] for c in group_kill.call_args_list]}; a group resolved "
            "from the recycled pid would be 31337, a stranger's)"
        )
        pid_kill.assert_not_awaited()
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_live_group_whose_id_was_not_verified_is_a_failed_kill_not_reaped(
        self, tmp_path: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No pgid captured while the leader lived, leader gone: the group cannot be told from a stranger's -- not signalled."""
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        svc, job, claim = _overdue_reap_fixture(tmp_path, "leaderless3")
        _session_with_pid(svc, "cron:leaderless3", 7575)

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            # Unreadable at snapshot time: no verified group id on the handle. By
            # the kill the leader has exited.
            patch(
                "os.getpgid",
                side_effect=ProcessLookupError("[Errno 3] No such process"),
                create=True,
            ),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=None),
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            patch("kiro_crew.platform_compat.pgroup_exists", return_value=True),
            patch("kiro_crew.platform_compat.kill_process_group") as group_kill,
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock()) as pid_kill,
        ):
            await svc._force_reap("leaderless3", _JOB_TIMEOUT_SECS + 60, claim=claim)

        group_kill.assert_not_called()
        pid_kill.assert_not_awaited()
        assert _audited_outcome(mock_sel) == "failed"
        assert (
            "; kill failed: pid 7575 exited but its process group 7575 still has members and "
            "could not be verified as this run's; not signalled"
        ) in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_live_group_no_verified_member_vouches_for_is_not_signalled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only a late, unrecorded spawn keeps the group alive: nothing verified is in it -- reported, not signalled.

        Once every verified member is gone the number is free for another tree
        (a stranger that took the leader's old pid, ``setsid``-ed, spawned and
        exited leaves a leaderless group under the same number), so a group the
        run cannot prove is its own is never signalled.
        """
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        client = _session_with_pid(svc, "cron:held", 7676)
        # The one recorded child has exited (its start id does not read back).
        child = _vouching_child(client, 8676)

        with (
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            patch("os.getpgid", return_value=7676, create=True),
            patch("kiro_crew.platform_compat.pgroup_of", return_value=7676),
            # Snapshot bracket (2), then the leader is gone; the child gone too.
            patch(
                "kiro_crew.platform_compat.get_process_start_id",
                side_effect=_start_id_reads({7676: [_START_ID, _START_ID, None], child: None}),
            ),
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            patch("kiro_crew.platform_compat.pgroup_exists", return_value=True),
            patch("kiro_crew.platform_compat.kill_process_group") as group_kill,
        ):
            handle = _handle_of(svc, "cron:held")
            assert handle.pgid == 7676, "the snapshot did not verify the group id"
            failure = await svc._sigkill_session("cron:held", handle)

        group_kill.assert_not_called()
        assert failure == (
            "pid 7676 exited and its process group 7676 still has members, but none could be "
            "verified as this run's; not signalled"
        )

    def test_on_macos_the_root_s_zombie_vouches_through_the_kernel_group_listing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``getpgid`` refuses a darwin zombie; the group listing matched on (pid, start id) stands in."""
        from types import SimpleNamespace

        from kiro_crew.process_identity import ProcessHandle, _group_member_vouches

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(sys, "platform", "darwin")
        pid = _UNALLOCATABLE_PID
        handle = ProcessHandle(pid=pid, start_id=_START_ID, pgid=pid, child_pids={})

        with (
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=_START_ID),
            patch("kiro_crew.platform_compat.pgroup_of", return_value=None),
            patch(
                "kiro_crew.platform_compat.darwin_pgroup_members",
                return_value=[SimpleNamespace(pid=pid, start_id=_START_ID)],
            ) as listing,
        ):
            assert _group_member_vouches(handle) is True
        listing.assert_called_once_with(pid)

        # A listed member under a recycled number does not pass: the start id differs.
        with (
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=_START_ID),
            patch("kiro_crew.platform_compat.pgroup_of", return_value=None),
            patch(
                "kiro_crew.platform_compat.darwin_pgroup_members",
                return_value=[SimpleNamespace(pid=pid, start_id="9999999")],
            ),
        ):
            assert _group_member_vouches(handle) is False

    @pytest.mark.asyncio
    async def test_an_empty_group_after_the_leader_exit_is_nothing_to_kill(
        self, tmp_path: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Control: the leader took its whole group with it -- ``reaped``, nothing signalled."""
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        svc, job, claim = _overdue_reap_fixture(tmp_path, "emptied1")
        _session_with_pid(svc, "cron:emptied1", 7777)

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            # Alive at the snapshot, gone by the kill; the captured group probes empty.
            patch(
                "kiro_crew.platform_compat.get_process_start_id",
                side_effect=_start_id_reads({7777: [_START_ID, _START_ID, None]}),
            ),
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            patch("os.getpgid", return_value=7777, create=True),
            _group_gone(),
            patch("kiro_crew.platform_compat.kill_process_group") as group_kill,
        ):
            await svc._force_reap("emptied1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        group_kill.assert_not_called()
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_leader_gone_after_a_completed_reset_with_a_live_group_still_gets_the_kill(
        self, tmp_path: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``process_survived`` asks a gone leader's group before calling it gone."""
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        svc, job, claim = _overdue_reap_fixture(tmp_path, "survivors1")
        client = _session_with_pid(svc, "cron:survivors1", 7878)
        child = _vouching_child(client, 8878)
        svc._sessions.reset = AsyncMock(return_value=True)

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            # Alive at the snapshot (the bracketed group read), gone ever after;
            # the recorded child reads back throughout.
            patch(
                "kiro_crew.platform_compat.get_process_start_id",
                side_effect=_start_id_reads({7878: [_START_ID, _START_ID, None], child: "5150001"}),
            ),
            patch("os.getpgid", return_value=7878, create=True),
            patch("kiro_crew.platform_compat.pgroup_of", return_value=7878),
            # The leader is gone; the recorded child is a zombie still in the group
            # -- it vouches for the group (a zombie reads its group) and is not a
            # running survivor. Both liveness reads pinned per pid.
            patch("kiro_crew.platform_compat.pid_exists", side_effect=lambda pid: pid == child),
            patch("kiro_crew.platform_compat.pid_is_zombie", return_value=True),
            patch("kiro_crew.platform_compat.pgroup_exists", return_value=True),
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock()) as pid_kill,
            patch("kiro_crew.platform_compat.kill_process_group") as group_kill,
        ):
            await svc._force_reap("survivors1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        pid_kill.assert_not_awaited()
        group_kill.assert_called_once_with(7878, platform_compat.SIGKILL)
        assert _audited_outcome(mock_sel) == "reaped"


class TestARecordedChildTheSweepLeftStandingIsNamed:
    """A recorded child the escaped-children sweep left running is a kill failure, not a reap.

    The client's sweep signals a recorded descendant only once it is verified by
    start id AND basename, and reports nothing: a child that kept its pid and
    start id but ``exec``'d into another name is refused by that guard and stays
    alive with no return path, so a kill that returned the group signal's outcome
    alone recorded ``reaped`` over it. After the sweep every recorded child whose
    start id reads back and that is still running -- not a zombie -- is named
    (``process_identity._sweep_children``), reported, not signalled: the guard
    refused it, and this record does not overrule the guard. The liveness reads
    (``pid_exists`` / ``pid_is_zombie``) are pinned everywhere: a made-up pid
    answers whatever the host runs otherwise.
    """

    @staticmethod
    def _fixture(
        tmp_path: object, job_id: str, pid: int, child: int
    ) -> tuple[CronService, CronJob, _RunClaim]:
        svc, job, claim = _overdue_reap_fixture(tmp_path, job_id)
        client = _session_with_pid(svc, f"cron:{job_id}", pid)
        _vouching_child(client, child)
        return svc, job, claim

    @staticmethod
    @contextmanager
    def _reads(pid: int, child: int, child_start: str | None = "5150001") -> Iterator[None]:
        """The root reads back throughout and leads its own group; the child's start id as given."""
        with (
            patch(
                "kiro_crew.platform_compat.get_process_start_id",
                side_effect=_start_id_reads({pid: _START_ID, child: child_start}),
            ),
            _isolated_leader(),
        ):
            yield

    @pytest.mark.asyncio
    async def test_a_child_that_exec_d_into_another_name_survives_the_sweep_as_a_failed_kill(
        self, tmp_path: object
    ) -> None:
        """Group kill delivered, sweep ran and refused the child (basename), child still running: ``failed``."""
        svc, job, claim = self._fixture(tmp_path, "execd1", 7171, 8171)

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children") as sweep,
            self._reads(7171, 8171),
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.platform_compat.pid_is_zombie", return_value=False),
            _group_gone(),
            patch("kiro_crew.process_identity._SWEEP_SETTLE_SECS", 0, create=True),
            patch("kiro_crew.platform_compat.kill_process_group", return_value=True) as group_kill,
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock()) as pid_kill,
        ):
            await svc._force_reap("execd1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        group_kill.assert_called_once_with(7171, platform_compat.SIGKILL)
        sweep.assert_called_once_with({8171: ("5150001", b"node")})
        pid_kill.assert_not_awaited()  # reported, not signalled: the client's guard refused it
        assert (
            _audited_outcome(mock_sel) == "failed"
        ), "the audit says the run was reaped while a recorded child it verified is still running"
        assert (
            "; kill failed: recorded child pid 8171 survived the escaped-children sweep, identity "
            "verified; not signalled"
        ) in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_child_the_sweep_signalled_is_a_zombie_not_a_survivor(
        self, tmp_path: object
    ) -> None:
        """Control: the child's start id still reads back but it sits in the zombie state -- finished, ``reaped``."""
        svc, job, claim = self._fixture(tmp_path, "execd2", 7272, 8272)

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            self._reads(7272, 8272),
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.platform_compat.pid_is_zombie", return_value=True),
            _group_gone(),
            patch("kiro_crew.platform_compat.kill_process_group", return_value=True),
        ):
            await svc._force_reap("execd2", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_child_that_settles_after_the_signal_is_not_a_survivor(
        self, tmp_path: object
    ) -> None:
        """SIGKILL lands asynchronously: a child still running on the first read and a zombie on the next is settled."""
        svc, job, claim = self._fixture(tmp_path, "execd3", 7373, 8373)

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            self._reads(7373, 8373),
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.platform_compat.pid_is_zombie", side_effect=[False, True]) as zombie,
            _group_gone(),
            patch("kiro_crew.process_identity._SWEEP_SETTLE_SECS", 0, create=True),
            patch("kiro_crew.platform_compat.kill_process_group", return_value=True),
        ):
            await svc._force_reap("execd3", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert zombie.call_count == 2, "the post-sweep read was not retried"
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_child_whose_start_id_does_not_read_back_is_not_the_run_s(
        self, tmp_path: object
    ) -> None:
        """Control: a recorded child gone or recycled (start id differs) is another process's now -- ``reaped``."""
        svc, job, claim = self._fixture(tmp_path, "execd4", 7474, 8474)

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            self._reads(7474, 8474, child_start="9999999"),
            # Alive and running under the recorded pid -- but not the process the
            # client recorded, so nothing this run answers for.
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.platform_compat.pid_is_zombie", return_value=False),
            _group_gone(),
            patch("kiro_crew.platform_compat.kill_process_group", return_value=True),
        ):
            await svc._force_reap("execd4", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_surviving_child_is_named_beside_the_group_kill_s_own_failure(self) -> None:
        """Both failures ride one record: the refused group kill first, the surviving child after it."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        client = _session_with_pid(svc, "cron:both", 7575)
        _vouching_child(client, 8575)

        with (
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            self._reads(7575, 8575),
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.platform_compat.pid_is_zombie", return_value=False),
            _group_gone(),
            patch("kiro_crew.process_identity._SWEEP_SETTLE_SECS", 0, create=True),
            patch(
                "kiro_crew.platform_compat.kill_process_group",
                side_effect=PermissionError("[Errno 1] Operation not permitted"),
            ),
            patch(
                "kiro_crew.platform_compat.kill_pid_async",
                AsyncMock(side_effect=PermissionError("[Errno 1] Operation not permitted")),
            ),
        ):
            failure = await svc._sigkill_session("cron:both", _handle_of(svc, "cron:both"))

        assert failure == (
            "PermissionError: [Errno 1] Operation not permitted; recorded child pid 8575 survived "
            "the escaped-children sweep, identity verified; not signalled"
        )


class TestEveryProcessUnderTheKeyIsAnswered:
    """A live successor under the key does not hide the run's torn-down process, or the reverse.

    The run's own teardown reset pops session A and stalls in an await; a
    completion cold-starts session B under the same key. The reap reads BOTH
    handles (the torn-down one first), resets the key -- which is B's teardown
    -- and then verifies and kills every candidate on its own handle. Before,
    the live session won the snapshot, so the reap killed B and its cancel
    abandoned A mid-hung-shutdown while the audit recorded ``reaped``.
    """

    def _two_sessions(self, svc: CronService, key: str, run_pid: int, successor_pid: int) -> None:
        _session_with_pid(svc, key, run_pid)
        _torn_down_by_the_run(svc, key)
        _session_with_pid(svc, key, successor_pid)  # the live successor

    def test_both_are_candidates_the_torn_down_one_first(self) -> None:
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        self._two_sessions(svc, "cron:both", 7979, 8979)

        handles = svc._session_process_handles("cron:both")

        assert [h.pid for h in handles] == [7979, 8979], "the torn-down run process is not first"

    def test_a_successor_under_a_recycled_pid_is_its_own_handle(self) -> None:
        """The dedup key is the handle -- ``(pid, start id)`` -- never the pid.

        The run's torn-down process exited and the kernel handed its number to
        the successor a late cold start registered under the key. A snapshot that
        dropped the successor as "the same pid" would kill nothing (the dead
        predecessor) and record ``reaped`` over the successor's tree.
        """
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        _session_with_pid(svc, "cron:recycled-succ", 8282)
        _torn_down_by_the_run(svc, "cron:recycled-succ")
        successor = _session_with_pid(svc, "cron:recycled-succ", 8282)
        successor._start_time = "7777777"

        handles = svc._session_process_handles("cron:recycled-succ")

        assert [(h.pid, h.start_id) for h in handles] == [(8282, _START_ID), (8282, "7777777")], (
            "the successor under the recycled pid was dropped as a duplicate of the dead "
            "predecessor's handle"
        )
        # And the same incarnation named twice -- the torn-down table and the live
        # map holding the one session -- is one handle.
        same_svc = CronService(base_dir=None, on_job=AsyncMock())
        same_svc._sessions = _mock_sessions()
        _session_with_pid(same_svc, "cron:same", 8383)
        same_session = same_svc._sessions._sessions["cron:same"]
        _retain_torn_down(same_svc, "cron:same", same_session)
        assert len(same_svc._session_process_handles("cron:same")) == 1

    @pytest.mark.asyncio
    async def test_a_successor_under_a_recycled_pid_is_killed_on_its_own_handle(
        self, tmp_path: object
    ) -> None:
        """Red-first for the pid-keyed dedup: the successor's group is signalled; the predecessor's recycled pid is not."""
        svc, job, claim = _overdue_reap_fixture(tmp_path, "recycled-both")
        _session_with_pid(svc, "cron:recycled-both", 8484)
        _torn_down_by_the_run(svc, "cron:recycled-both")
        successor = _session_with_pid(svc, "cron:recycled-both", 8484)
        successor._start_time = "7777777"
        svc._sessions.reset = AsyncMock(return_value=True)

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            # The pid now reads the successor's start id: the predecessor is
            # recycled (nothing to kill, its group probes empty), the successor is
            # alive and verified.
            patch("kiro_crew.platform_compat.get_process_start_id", return_value="7777777"),
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            _isolated_leader(),
            _group_gone(),
            patch("kiro_crew.platform_compat.kill_process_group", return_value=True) as group_kill,
        ):
            await svc._force_reap("recycled-both", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert group_kill.call_args_list == [call(8484, platform_compat.SIGKILL)], (
            "the successor under the recycled pid was dropped as a duplicate of the dead "
            "predecessor's handle: no kill reached it"
        )
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_the_run_s_torn_down_process_and_the_live_successor_are_both_killed(
        self, tmp_path: object
    ) -> None:
        svc, job, claim = _overdue_reap_fixture(tmp_path, "both1")
        self._two_sessions(svc, "cron:both1", 8080, 9080)
        svc._sessions.reset = AsyncMock(return_value=True)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_group", return_value=True) as group_kill,
        ):
            await svc._force_reap("both1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert [called.args[0] for called in group_kill.call_args_list] == [
            8080,
            9080,
        ], "the run's own (torn-down) process must be killed, and the successor beside it"
        assert _audited_outcome(mock_sel) == "reaped"

    @pytest.mark.asyncio
    async def test_a_refused_kill_of_the_torn_down_process_is_a_failed_kill_whatever_the_successor(
        self, tmp_path: object
    ) -> None:
        svc, job, claim = _overdue_reap_fixture(tmp_path, "both2")
        self._two_sessions(svc, "cron:both2", 8181, 9181)
        svc._sessions.reset = AsyncMock(return_value=True)
        children, start_id, sweep = _kill_path_stubs()

        def _refuse_the_run_s_process(pgid: int, _sig: int) -> bool:
            if pgid == 8181:
                raise ValueError("kill_process_group: refusing broadcast/self process group 8181")
            return True

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group",
                side_effect=_refuse_the_run_s_process,
            ),
        ):
            await svc._force_reap("both2", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert _audited_outcome(mock_sel) == "failed"
        assert "; kill failed: ValueError: kill_process_group: refusing" in (job.last_error or "")
        assert "8181" in (job.last_error or "")


class TestTheSessionTheResetPopsIsCaptured:
    """A registration that lands after the snapshot is what the reset pops -- and what the fallback must kill.

    The pre-reset snapshot is keyed by name. A cold start can register a new
    session under the key between that snapshot and the reset's pop; the reset
    pops THAT session and hangs on it, and a fallback fed the snapshot alone finds
    nothing to kill and records ``reaped`` over a live process. The reset now runs
    under a scope whose ``on_pop`` hook takes the popped session's handle in the
    same lock hold as the pop, and the fallback kills it; a popped session with
    no recorded pid is a named kill failure, never ``reaped``.
    """

    @staticmethod
    def _registering_reset(svc: CronService, key: str, pid: int | None) -> Any:
        """A reset that cold-starts a session under ``key`` after the snapshot, pops it, then hangs."""

        async def _reset(session_key: str, **kwargs: Any) -> bool:
            assert session_key == key
            client = _session_with_pid(svc, key, pid)
            session = svc._sessions._sessions.pop(key)
            scope = kwargs.get("scope")
            if scope is not None:
                scope.note_pop(key, session)
            del client
            raise asyncio.TimeoutError

        return _reset

    @pytest.mark.asyncio
    async def test_a_session_registered_after_the_snapshot_is_killed_through_the_pop(
        self, tmp_path: object
    ) -> None:
        svc, job, claim = _overdue_reap_fixture(tmp_path, "late1")
        assert svc._session_process_handles("cron:late1") == [], "the snapshot must see nothing"
        svc._sessions.reset = AsyncMock(
            side_effect=self._registering_reset(svc, "cron:late1", 8787)
        )
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_group", return_value=True) as group_kill,
        ):
            await svc._force_reap("late1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        group_kill.assert_called_once_with(8787, platform_compat.SIGKILL)
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_popped_session_with_no_process_handle_yet_is_a_failed_kill_not_reaped(
        self, tmp_path: object
    ) -> None:
        """The cold start had not spawned yet: nothing names its process, so nothing can verify it."""
        svc, job, claim = _overdue_reap_fixture(tmp_path, "late2")
        svc._sessions.reset = AsyncMock(
            side_effect=self._registering_reset(svc, "cron:late2", None)
        )

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
        ):
            await svc._force_reap("late2", _JOB_TIMEOUT_SECS + 60, claim=claim)

        tree_kill.assert_not_awaited()
        assert (
            _audited_outcome(mock_sel) == "failed"
        ), "the audit says the run was reaped while the session the reset popped names no process"
        assert (
            "; kill failed: the session the reset popped had no process handle yet; not signalled"
        ) in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_the_popped_handle_joins_the_snapshot_not_replaces_it(
        self, tmp_path: object
    ) -> None:
        """A torn-down run process seen at the snapshot is still killed beside the popped successor."""
        svc, job, claim = _overdue_reap_fixture(tmp_path, "late3")
        _session_with_pid(svc, "cron:late3", 8888)
        _torn_down_by_the_run(svc, "cron:late3")
        svc._sessions.reset = AsyncMock(
            side_effect=self._registering_reset(svc, "cron:late3", 9888)
        )
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_group", return_value=True) as group_kill,
        ):
            await svc._force_reap("late3", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert [called.args[0] for called in group_kill.call_args_list] == [8888, 9888]
        assert _audited_outcome(mock_sel) == "reaped"

    def test_the_kill_set_is_keyed_by_the_handle_not_the_pid(self) -> None:
        """A popped session under a snapshot handle's pid but another start id is a second process; the same incarnation is one."""
        snapshot = ProcessHandle(pid=8989, start_id=_START_ID, pgid=8989, child_pids={})
        recycled = ProcessHandle(pid=8989, start_id="7777777", pgid=8989, child_pids={})
        same = ProcessHandle(pid=8989, start_id=_START_ID, pgid=None, child_pids={1: ("x", b"n")})

        targets, missing = kill_set([snapshot], [(object(), recycled), (object(), same)])

        assert missing is None
        assert targets == [snapshot, recycled], (
            "the popped session under the recycled pid was dropped as a duplicate of the "
            "snapshot handle"
        )
        assert snapshot == same and hash(snapshot) == hash(
            same
        ), "the handle's identity is (pid, start id); group id and child records are not part of it"
        assert snapshot != recycled

    @pytest.mark.asyncio
    async def test_through_the_real_manager_the_popped_session_s_process_is_killed(
        self, tmp_path: object
    ) -> None:
        """End to end: a cold start attempted inside the reap's pass is held at the door -- the fence, not the pop capture, answers it -- and lands after the record.

        Through the real manager the pop capture is the net under the fence: a
        cold start under the key between the reap's snapshot and the reset's pop
        (the late completion's ``get_or_create``) never registers under the run,
        because ``_end_run_processes`` raised the key's ending fence before the
        snapshot. Nothing lands during the passes, nothing is killed, the record
        is ``reaped`` over an empty key -- and the held call is not dropped: it
        lands once the fence lifts, a new life under the recorded key.
        """
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager

        def _factory(
            session_key: Any = None, agent: Any = None, channel_id: Any = None, **_: Any
        ) -> Any:
            provider = AsyncMock()
            provider.start = AsyncMock()
            provider.memory_mode = "persistent"
            provider.is_process_alive = lambda: True
            provider.context_usage_pct = lambda: 0.0
            provider.context_window_tokens = lambda: 0
            provider.has_active_turn = lambda: False
            provider.runtime_abort_target = lambda: None
            provider.shutdown = AsyncMock()
            return provider

        mgr = SessionManager(KiroCrewConfig(), provider_factory=_factory)
        parked, door = _parks_at_the_fence_door(mgr)
        racing: list[asyncio.Task[Any]] = []
        held_during_reset: list[str] = []

        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = mgr
        job = _make_job("late4")
        svc._jobs = [job]
        claim = svc._claims["late4"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60, task=_live_task()
        )
        children, start_id, sweep = _kill_path_stubs()
        real_pass = svc._reset_and_kill_once

        async def _cold_start_then_pass(session_key: str, pairs: Any, **kwargs: Any) -> Any:
            # The late completion's cold start, after the reap's snapshot and
            # before the reset's pop: held at the door while the fence is up.
            # Observed here, outside the reset's own bound, so no reset timeout
            # can cut the observation short.
            racing.append(asyncio.create_task(mgr.get_or_create(session_key)))
            held_during_reset.append(await _held_or_landed(parked, racing[0]))
            return await real_pass(session_key, pairs, **kwargs)

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch.object(svc, "_reset_and_kill_once", _cold_start_then_pass),
            door,
            patch("kiro_crew.cron._REAPER_RESET_TIMEOUT", 0.2),
            children,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            patch("kiro_crew.platform_compat.kill_process_group", return_value=True) as group_kill,
        ):
            assert svc._session_process_handles("cron:late4") == []
            await svc._force_reap("late4", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert held_during_reset == ["held"], (
            "the cold start attempted between the snapshot and the pop was answered under the "
            f"fenced key: {racing[0].exception() if racing and racing[0].done() and racing[0].exception() else held_during_reset}"
        )
        group_kill.assert_not_called()
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")
        # The fence lifted with the passes: the held call lands -- the completion
        # racing the reap is delivered after the record, not dropped.
        _, is_new, _ = await _within_the_ceiling(racing[0], "the held cold start landing")
        assert is_new and mgr.has_session("cron:late4")
        mgr.release("cron:late4")
        await mgr.reset("cron:late4")
