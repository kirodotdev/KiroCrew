"""A refused or failed SIGKILL is a kill failure, never a reap.

The reap still finishes the claim it took, but the run's terminal record and the
``reaper_force_kill`` audit carry what stopped the kill: the broadcast guard's refusal
of the pid, the error the kill raised, a recycled or unverifiable root, a hung
teardown, or a process still standing after a completed reset.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import cron_reaper_helpers
import pytest
from cron_reaper_helpers import (
    _START_ID,
    _audited_outcome,
    _group_gone,
    _handle_of,
    _isolated_leader,
    _kill_path_stubs,
    _make_job,
    _mock_sessions,
    _overdue_reap_fixture,
    _session_with_pid,
    _start_id_reads,
    _torn_down_by_the_run,
)

from kiro_crew import platform_compat
from kiro_crew.cron import _JOB_TIMEOUT_SECS, CronService, _RunClaim
from kiro_crew.cron_history import CronHistoryStore
from kiro_crew.process_identity import ProcessHandle
from kiro_crew.testing.wait import async_wait_until

#: Every cron reaper suite pins a healthy free-memory reading (see
#: ``ample_host_free_memory``): ``test_cron_reaper.py`` fires cron jobs through the
#: due-scan, which defers firings while the host's memory posture is critical.
pytestmark = pytest.mark.usefixtures("ample_host_free_memory")


# The shared fixtures, bound here by name so pytest registers them for this module.
_kill_seam = cron_reaper_helpers._kill_seam


# ── A refused or failed SIGKILL is a kill failure, not a reap ──


class TestReaperRecordsAFailedSigkill:
    """The audit never says ``reaped`` for a run whose process group was not killed.

    ``_sigkill_session`` raises nothing -- the reap must still finish the claim
    it took -- but it REPORTS what stopped the kill: the broadcast guard's
    refusal of the pid, or the error the kill raised. ``_force_reap`` carries
    that into the run's terminal record (``…; kill failed: <reason>``) and
    audits ``reaper_force_kill`` as ``failed``, the same outcome a kill await
    that raised gets. Before this, both were a log line and the audit read
    ``reaped`` while the process group kept running.
    """

    @pytest.mark.asyncio
    async def test_a_refused_pid_is_audited_as_a_failed_kill_not_reaped(
        self, tmp_path: object
    ) -> None:
        svc, job, claim = _overdue_reap_fixture(tmp_path, "refused1")
        _session_with_pid(svc, "cron:refused1", 4242)
        refusal = ValueError("kill_process_group: refusing broadcast/self process group 4242")
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group",
                side_effect=refusal,
            ),
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock()) as pid_kill,
        ):
            await svc._force_reap("refused1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        # The guard refused the pid outright: nothing was safe to signal, and
        # nothing was.
        pid_kill.assert_not_awaited()
        assert (
            _audited_outcome(mock_sel) == "failed"
        ), "the SEL audit says the process group was reaped while it is still alive"
        assert (job.last_error or "").startswith("Reaped after")
        assert "; kill failed: ValueError: kill_process_group: refusing" in (job.last_error or "")
        assert "4242" in (job.last_error or ""), "the record does not name the refused pid"
        # The run still ended for the record: a terminal row, the claim
        # released, the reap marked -- the failure is added, not substituted.
        runs, total = await svc._history.get_job_history("refused1")
        assert total == 1 and runs[0]["status"] == "timeout"
        assert "; kill failed: " in runs[0]["error"]
        assert "refused1" not in svc._claims
        assert claim.reaped
        assert job.last_status == "error"

    @pytest.mark.asyncio
    async def test_the_kill_failure_suffix_is_bounded_at_retention(self, tmp_path: object) -> None:
        """A kill reason the size of a tree's stderr cannot push ``last_error`` past the record bound.

        A Windows tree drain that fails carries one line per process the run
        spawned; joined onto the run's own text it is held to
        ``MAX_ERROR_DETAIL_LEN`` by the shared ``with_kill_failure`` (the same
        bound the sub-agent record applies), the reason keeping at least
        ``KILL_FAILURE_RESERVE_LEN`` of it and the run's text trimmed to make room.
        """
        from kiro_crew.process_identity import (
            KILL_FAILURE_RESERVE_LEN,
            MAX_ERROR_DETAIL_LEN,
            with_kill_failure,
        )

        svc, job, claim = _overdue_reap_fixture(tmp_path, "bounded1")
        _session_with_pid(svc, "cron:bounded1", 4343)
        blob = "\n".join(
            f"ERROR: The process with PID {4343 + n} could not be terminated." for n in range(200)
        )
        assert len(blob) > MAX_ERROR_DETAIL_LEN
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group",
                side_effect=PermissionError(blob),
            ),
            patch(
                "kiro_crew.platform_compat.kill_pid_async",
                AsyncMock(side_effect=PermissionError(blob)),
            ),
        ):
            await svc._force_reap("bounded1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert _audited_outcome(mock_sel) == "failed"
        last_error = job.last_error or ""
        assert (
            len(last_error) <= MAX_ERROR_DETAIL_LEN
        ), f"last_error is {len(last_error)} chars: the kill failure was appended unbounded"
        assert last_error.startswith("Reaped after")
        assert "; kill failed: PermissionError: ERROR: The process with PID 4343" in last_error
        runs, _ = await svc._history.get_job_history("bounded1")
        assert len(runs[0]["error"]) <= MAX_ERROR_DETAIL_LEN
        # The helper's rule: the reason keeps its reserve even when the run's error fills the bound.
        bounded = with_kill_failure("x" * MAX_ERROR_DETAIL_LEN, blob)
        assert len(bounded) == MAX_ERROR_DETAIL_LEN
        assert bounded.endswith(blob[:KILL_FAILURE_RESERVE_LEN])
        assert bounded.startswith(
            "x" * (MAX_ERROR_DETAIL_LEN - KILL_FAILURE_RESERVE_LEN - len("; kill failed: "))
        )
        assert with_kill_failure("", "gone") == "kill failed: gone"

    @pytest.mark.asyncio
    async def test_a_group_kill_that_raises_with_no_pid_fallback_is_a_failed_kill(
        self, tmp_path: object
    ) -> None:
        """EPERM on the group and on the pid: the process is there and unsignalled."""
        svc, job, claim = _overdue_reap_fixture(tmp_path, "eperm1")
        _session_with_pid(svc, "cron:eperm1", 4343)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group",
                side_effect=PermissionError("[Errno 1] Operation not permitted"),
            ),
            patch(
                "kiro_crew.platform_compat.kill_pid_async",
                AsyncMock(side_effect=PermissionError("[Errno 1] Operation not permitted")),
            ),
        ):
            await svc._force_reap("eperm1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert _audited_outcome(mock_sel) == "failed"
        assert "; kill failed: PermissionError: " in (job.last_error or "")
        assert "eperm1" not in svc._claims

    @pytest.mark.asyncio
    async def test_a_kill_path_error_before_the_signal_is_a_failed_kill(
        self, tmp_path: object
    ) -> None:
        """The catch-all that only logged: a probe that raises left the group unsignalled."""
        svc, job, claim = _overdue_reap_fixture(tmp_path, "probe1")
        _session_with_pid(svc, "cron:probe1", 4444)

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=_START_ID),
            patch(
                "kiro_crew.acp.client._get_child_pids",
                side_effect=RuntimeError("cannot schedule new futures after shutdown"),
            ),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
        ):
            await svc._force_reap("probe1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        tree_kill.assert_not_awaited()
        assert _audited_outcome(mock_sel) == "failed"
        assert "; kill failed: RuntimeError: cannot schedule" in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_delivered_sigkill_is_still_audited_as_reaped(self, tmp_path: object) -> None:
        svc, job, claim = _overdue_reap_fixture(tmp_path, "killed1")
        _session_with_pid(svc, "cron:killed1", 4545)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_group", return_value=True) as group_kill,
        ):
            await svc._force_reap("killed1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        group_kill.assert_called_once()
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_group_that_is_already_gone_is_nothing_to_kill(self, tmp_path: object) -> None:
        """ProcessLookupError on the group AND the pid: the run's process exited on its own."""
        svc, job, claim = _overdue_reap_fixture(tmp_path, "gone1")
        _session_with_pid(svc, "cron:gone1", 4646)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group",
                side_effect=ProcessLookupError("[Errno 3] No such process"),
            ),
            patch(
                "kiro_crew.platform_compat.kill_pid_async",
                AsyncMock(side_effect=ProcessLookupError("[Errno 3] No such process")),
            ),
        ):
            await svc._force_reap("gone1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_group_error_followed_by_a_gone_pid_keeps_the_group_error(self) -> None:
        """EPERM on the group names members it could not signal; a gone pid does not clear it."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        _session_with_pid(svc, "cron:mixed", 4747)
        children, start_id, sweep = _kill_path_stubs()

        with (
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group",
                side_effect=PermissionError("[Errno 1] Operation not permitted"),
            ),
            patch(
                "kiro_crew.platform_compat.kill_pid_async",
                AsyncMock(side_effect=ProcessLookupError("[Errno 3] No such process")),
            ),
        ):
            failure = await svc._sigkill_session("cron:mixed", _handle_of(svc, "cron:mixed"))

        assert failure is not None and failure.startswith("PermissionError: ")

    @pytest.mark.asyncio
    async def test_a_pid_scoped_fallback_that_lands_is_a_delivered_kill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """POSIX: the escaped-children sweep covers what the group signal missed, so the kill counts.

        The platform seam is pinned to POSIX: this rule is the one the two Windows
        tests below invert (there the root-only fallback cannot stand in for the
        tree walk), so left to the runner's own platform the same fixture reads
        as a failed kill on a Windows shard.
        """
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        _session_with_pid(svc, "cron:fallback", 4848)
        children, start_id, sweep = _kill_path_stubs()
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)

        with (
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group",
                side_effect=PermissionError("[Errno 1] Operation not permitted"),
            ),
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock(return_value=True)),
        ):
            assert (
                await svc._sigkill_session("cron:fallback", _handle_of(svc, "cron:fallback"))
                is None
            )

    @pytest.mark.asyncio
    async def test_on_windows_a_drain_that_raises_is_a_failure_and_no_root_only_fallback_stands_in(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing sweeps on Windows, so a pinned drain that raised is the failure -- no ``taskkill /PID`` is tried.

        A root-only kill that landed would leave the descendants standing; the
        drain either terminates the tree through its verified handles or reports.
        """
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        _session_with_pid(svc, "cron:win-fallback", 4949)
        children, start_id, sweep = _kill_path_stubs()
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)

        with (
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_pinned",
                side_effect=PermissionError("Access is denied."),
            ),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
            patch(
                "kiro_crew.platform_compat.kill_pid_async", AsyncMock(return_value=True)
            ) as pid_kill,
        ):
            failure = await svc._sigkill_session(
                "cron:win-fallback", _handle_of(svc, "cron:win-fallback")
            )

        assert failure == (
            "Windows tree cleanup incomplete for pid 4949: PermissionError: Access is denied."
        )
        tree_kill.assert_not_awaited()
        pid_kill.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_on_windows_a_tree_that_is_already_gone_is_nothing_to_kill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The Windows rule keeps only real errors: a root the pin cannot find is a gone tree.

        The no-pin variant of the Windows gone-root rule: the object with the
        recorded creation time cannot be opened at the pin (the root exited
        between the last identity read and the pin), and with no exact-tree
        cleanup pending for its identity there is nothing this code can
        enumerate, verify or signal (the pinned drain walked the tree while it
        was alive), and Windows has no process groups to probe -- a gone root
        stays "nothing to kill", the documented platform limitation. A pending
        pin is the other variant (``TestAGoneWindowsRootDrainsItsPendingTreeCleanup``).
        """
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        _session_with_pid(svc, "cron:win-gone", 5050)
        children, start_id, sweep = _kill_path_stubs()
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)

        with (
            children,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock(return_value=True)),
            patch("kiro_crew.platform_compat.pgroup_exists", return_value=True) as group,
            patch("kiro_crew.platform_compat.windows_tree_cleanup_pending", return_value=False),
            patch(
                "kiro_crew.platform_compat.kill_process_tree_pinned", return_value=False
            ) as drain,
        ):
            assert (
                await svc._sigkill_session("cron:win-gone", _handle_of(svc, "cron:win-gone"))
                is None
            )

        drain.assert_called_once_with(5050, _START_ID, platform_compat.SIGKILL)
        tree_kill.assert_not_awaited()
        group.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_handle_and_no_usable_pid_are_nothing_to_kill(self) -> None:
        """The early returns are not failures: there is no process group to answer for.

        No pre-reset handle (no session was live under the key before the
        reset), or a handle with no usable pid -- nothing names a process, so
        nothing is signalled. A live session whose client has no usable pid
        (never spawned, or already reset) is not even a candidate: the key names
        no process through it.
        """
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        _session_with_pid(svc, "cron:nopid", None)
        _session_with_pid(svc, "cron:pid1", 1)
        no_pid = ProcessHandle(pid=None, start_id=_START_ID, pgid=None, child_pids={})

        assert svc._session_process_handles("cron:nopid") == []
        assert svc._session_process_handles("cron:pid1") == []
        with patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill:
            assert await svc._sigkill_session("cron:absent", None) is None
            assert await svc._sigkill_session("cron:absent", no_pid) is None
            assert await svc._sigkill_sessions("cron:nopid", []) is None

        tree_kill.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_successor_under_the_key_is_not_the_run_s_process(self) -> None:
        """Only the pre-reset handle names the process; a session in the map now is a successor.

        The reset pops the run's session and awaits; a cold start (a sub-agent
        completion delivering into the key) can register a NEW session under the
        same key in that window. Reading the map at kill time would signal that
        successor and leave the run's own, hung process alive -- recorded reaped.
        """
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        _session_with_pid(svc, "cron:succ", 5151)
        children, start_id, sweep = _kill_path_stubs()

        with (
            children,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_group", return_value=True) as group_kill,
        ):
            handle = _handle_of(svc, "cron:succ")
            # The reset popped the run's session; a successor process now holds the key.
            svc._sessions._sessions.pop("cron:succ")
            _session_with_pid(svc, "cron:succ", 8080)
            assert await svc._sigkill_session("cron:succ", handle) is None

        group_kill.assert_called_once_with(5151, platform_compat.SIGKILL)

    @pytest.mark.asyncio
    async def test_a_reset_that_hangs_after_popping_the_session_still_gets_the_kill(
        self, tmp_path: object
    ) -> None:
        """The reset pops the session from the map before it can hang; the kill must not need it.

        ``SessionLifecycle.reset`` removes the map entry under its lock and only
        then awaits the shutdown that can hang. Without a handle taken before the
        reset, the fallback looked the key up, found nothing, and the run was
        audited ``reaped`` while its process kept running.
        """
        svc, job, claim = _overdue_reap_fixture(tmp_path, "popped1")
        _session_with_pid(svc, "cron:popped1", 5151)

        async def _pop_then_hang(session_key: str, **_: Any) -> bool:
            svc._sessions._sessions.pop(session_key, None)
            raise asyncio.TimeoutError

        svc._sessions.reset = AsyncMock(side_effect=_pop_then_hang)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_group", return_value=True) as group_kill,
        ):
            await svc._force_reap("popped1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert "cron:popped1" not in svc._sessions._sessions, "the fixture did not pop the session"
        group_kill.assert_called_once_with(5151, platform_compat.SIGKILL)
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_the_root_is_verified_by_its_recorded_start_id(self) -> None:
        """A root whose live start id matches the one the client recorded is ours: killed.

        The shared child verifier denies a pid with no recorded basename, and the
        root has none, so validating the root through it never let a real kill
        through. The root is compared by start id, the recycling detector itself.
        """
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        _session_with_pid(svc, "cron:root", 5252)

        with (
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=_START_ID),
            _isolated_leader(),
            patch("kiro_crew.platform_compat.kill_process_group", return_value=True) as group_kill,
        ):
            assert await svc._sigkill_session("cron:root", _handle_of(svc, "cron:root")) is None

        group_kill.assert_called_once_with(5252, platform_compat.SIGKILL)

    @pytest.mark.asyncio
    async def test_a_recycled_pid_is_nothing_to_kill_and_only_recorded_children_are_swept(
        self,
    ) -> None:
        """A live start id that differs from the recorded one means another process owns the pid now."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        client = _session_with_pid(svc, "cron:recycled", 5353)
        client._child_pids = {6161: ("111", b"node")}

        with (
            patch("kiro_crew.acp.client._get_child_pids", return_value=[7171]) as probe,
            patch("kiro_crew.acp.client._kill_escaped_children") as sweep,
            patch("kiro_crew.platform_compat.get_process_start_id", return_value="9999999"),
            _group_gone(),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
        ):
            assert (
                await svc._sigkill_session("cron:recycled", _handle_of(svc, "cron:recycled"))
                is None
            )

        tree_kill.assert_not_awaited()
        # Never read through a pid that is not ours: no fresh child probe, and
        # the sweep gets only the children the client had recorded.
        probe.assert_not_called()
        sweep.assert_called_once_with({6161: ("111", b"node")})

    @pytest.mark.asyncio
    async def test_a_live_pid_whose_identity_cannot_be_confirmed_is_a_failed_kill(self) -> None:
        """Alive but unverifiable is not gone: not signalled, and recorded as a kill failure."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        _session_with_pid(svc, "cron:unread", 5454)
        client = _session_with_pid(svc, "cron:norecord", 5555)
        client._start_time = None

        with (
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=None),
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
        ):
            unread = await svc._sigkill_session("cron:unread", _handle_of(svc, "cron:unread"))
            with patch("kiro_crew.platform_compat.get_process_start_id", return_value=_START_ID):
                no_record = await svc._sigkill_session(
                    "cron:norecord", _handle_of(svc, "cron:norecord")
                )

        tree_kill.assert_not_awaited()
        assert unread == "pid 5454 is alive but could not be verified as this run's; not signalled"
        assert (
            no_record == "pid 5555 is alive but could not be verified as this run's; not signalled"
        )

    @pytest.mark.asyncio
    async def test_a_pid_that_has_exited_is_nothing_to_kill(self) -> None:
        """No start id AND no process behind the pid: it exited; only the children are swept."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        _session_with_pid(svc, "cron:exited", 5656)

        with (
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]) as probe,
            patch("kiro_crew.acp.client._kill_escaped_children") as sweep,
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=None),
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            _group_gone(),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
        ):
            assert await svc._sigkill_session("cron:exited", _handle_of(svc, "cron:exited")) is None

        tree_kill.assert_not_awaited()
        probe.assert_not_called()
        sweep.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_root_that_exits_during_the_child_walk_is_not_signalled_and_its_fresh_child_is_named(
        self,
    ) -> None:
        """The start id is read again right before the signal; a changed one is a gone root, and a child the walk found is named.

        The child walk awaits, and the root can exit -- and its pid be handed to
        another process -- while it does. A ``killpg`` through the pid then would
        signal that process's group. A child the walk FOUND (7272, in its own
        group, so the retained group's probe never sees it) was read through a
        pid that may already have been recycled when the walk ran, so it is not
        swept -- and it is not dropped either: the record names it as a kill
        failure, unattributed and not signalled, never ``reaped`` over it. Only
        the children recorded before the reset (6262) are swept.
        """
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        client = _session_with_pid(svc, "cron:walk", 5757)
        client._child_pids = {6262: ("222", b"node")}
        # Snapshotted before the mocked reads: the kill's own two identity reads
        # are the two values below, so the SECOND is the one that differs.
        handle = _handle_of(svc, "cron:walk")

        with (
            patch("kiro_crew.acp.client._get_child_pids", return_value=[7272]) as probe,
            patch(
                "kiro_crew.acp.client._capture_child_records", return_value={7272: ("333", b"sh")}
            ),
            patch("kiro_crew.acp.client._kill_escaped_children") as sweep,
            # The root's two reads, then the post-sweep read of the recorded
            # child: gone (its start id does not read back).
            patch(
                "kiro_crew.platform_compat.get_process_start_id",
                side_effect=_start_id_reads({5757: [_START_ID, "9999999"], 6262: None}),
            ),
            _group_gone(),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
        ):
            failure = await svc._sigkill_session("cron:walk", handle)

        probe.assert_called_once_with(
            5757
        )  # the walk was entered; the second read stopped the signal
        tree_kill.assert_not_awaited()
        sweep.assert_called_once_with({6262: ("222", b"node")})
        assert failure is not None and "7272" in failure, (
            "the child the walk found under the exiting leader was dropped and the kill reported "
            f"success over it: {failure!r}"
        )
        assert failure == (
            "1 child(ren) found during the walk (pid 7272) could not be attributed to the run "
            "after its leader exited; not signalled"
        )

    @pytest.mark.asyncio
    async def test_a_root_that_exits_during_a_walk_that_found_nothing_new_is_nothing_to_kill(
        self,
    ) -> None:
        """The control: no fresh child, the recorded child gone, an empty group -- nothing to name."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        client = _session_with_pid(svc, "cron:walk2", 5858)
        client._child_pids = {6363: ("222", b"node")}
        handle = _handle_of(svc, "cron:walk2")

        with (
            patch("kiro_crew.acp.client._get_child_pids", return_value=[6363]),
            patch("kiro_crew.acp.client._capture_child_records") as capture,
            patch("kiro_crew.acp.client._kill_escaped_children") as sweep,
            patch(
                "kiro_crew.platform_compat.get_process_start_id",
                side_effect=_start_id_reads({5858: [_START_ID, "9999999"], 6363: None}),
            ),
            _group_gone(),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
        ):
            assert await svc._sigkill_session("cron:walk2", handle) is None

        capture.assert_not_called()
        tree_kill.assert_not_awaited()
        sweep.assert_called_once_with({6363: ("222", b"node")})

    @pytest.mark.asyncio
    async def test_a_reset_that_finds_no_session_still_kills_through_the_handle(
        self, tmp_path: object
    ) -> None:
        """``reset`` answers False when the key is already gone: it stopped nothing.

        A concurrent reset popped the entry between the reap's snapshot and the
        reset's lock. Whether that reset's shutdown lands is not this reap's to
        assume: the handle says the process is standing (its recorded start id
        reads back), so the kill goes through the handle and the record says
        reaped only once it has been signalled.
        """
        svc, job, claim = _overdue_reap_fixture(tmp_path, "unmapped1")
        _session_with_pid(svc, "cron:unmapped1", 5858)

        async def _already_popped(session_key: str, **_: Any) -> bool:
            svc._sessions._sessions.pop(session_key, None)
            return False

        svc._sessions.reset = AsyncMock(side_effect=_already_popped)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_group", return_value=True) as group_kill,
        ):
            await svc._force_reap("unmapped1", elapsed=_JOB_TIMEOUT_SECS + 60, claim=claim)

        group_kill.assert_called_once_with(5858, platform_compat.SIGKILL)
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_session_the_run_s_own_teardown_popped_before_the_snapshot_still_gets_the_kill(
        self, tmp_path: object
    ) -> None:
        """The run's own finally reset pops the session BEFORE the reap looks; the kill still lands.

        The ordinary shape of a run that hangs in its teardown: the run body's
        ``finally`` resets its session, the reset pops the map entry under the
        registry lock and then hangs in the provider shutdown, and only later does
        the reaper measure the run over its deadline. Its live-map lookup misses,
        its own reset answers False for the already-popped key, and without the
        torn-down table there was no handle -- nothing to verify, ``reaped``
        recorded, while the hung reset (which this reap's cancel of the run task
        is about to interrupt) still held the process. The handle is read from
        the session the manager retains for the life of that teardown, and the
        kill goes to the pre-pop pid.
        """
        svc, job, claim = _overdue_reap_fixture(tmp_path, "torn1")
        _session_with_pid(svc, "cron:torn1", 6363)
        _torn_down_by_the_run(svc, "cron:torn1")
        svc._sessions.reset = AsyncMock(return_value=False)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_group", return_value=True) as group_kill,
        ):
            await svc._force_reap("torn1", elapsed=_JOB_TIMEOUT_SECS + 60, claim=claim)

        assert "cron:torn1" not in svc._sessions._sessions, "the fixture left the session live"
        group_kill.assert_called_once_with(6363, platform_compat.SIGKILL)
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_torn_down_session_whose_pid_the_hung_teardown_cleared_is_killed_on_the_pop_time_handle(
        self, tmp_path: object
    ) -> None:
        """The retained handle is the one captured at the pop, not a re-read of the session.

        The run's own teardown pops the session, its ACP kill fails, the client's
        reset clears the recorded pid anyway, and the teardown hangs on the
        transport -- the process still standing. A reap that re-read the retained
        session found no pid, dropped the only process under the key as "names
        nothing", and recorded ``reaped``. The table captures the handle in the
        pop's own lock hold, and the kill goes to that identity.
        """
        svc, job, claim = _overdue_reap_fixture(tmp_path, "torn3")
        client = _session_with_pid(svc, "cron:torn3", 6565)
        _torn_down_by_the_run(svc, "cron:torn3")
        # What the client's reset does after a kill it could not confirm.
        client._pid = None
        svc._sessions.reset = AsyncMock(return_value=False)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_group", return_value=True) as group_kill,
        ):
            await svc._force_reap("torn3", elapsed=_JOB_TIMEOUT_SECS + 60, claim=claim)

        assert group_kill.call_args_list == [call(6565, platform_compat.SIGKILL)], (
            "the reap discarded the live process whose pid the hung teardown had cleared "
            f"and recorded {_audited_outcome(mock_sel)} over it"
        )
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_refused_kill_of_a_session_the_run_s_own_teardown_popped_is_a_failed_kill(
        self, tmp_path: object
    ) -> None:
        """Same pop-before-the-snapshot shape, kill refused: ``failed``, never ``reaped``.

        Before the torn-down table this run was audited ``reaped`` with a clean
        ``last_error`` and no kill attempted at all -- the audit this PR corrects,
        on the path a hung teardown takes every time.
        """
        svc, job, claim = _overdue_reap_fixture(tmp_path, "torn2")
        _session_with_pid(svc, "cron:torn2", 6464)
        _torn_down_by_the_run(svc, "cron:torn2")
        svc._sessions.reset = AsyncMock(return_value=False)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group",
                side_effect=ValueError(
                    "kill_process_group: refusing broadcast/self process group 6464"
                ),
            ),
        ):
            await svc._force_reap("torn2", elapsed=_JOB_TIMEOUT_SECS + 60, claim=claim)

        assert _audited_outcome(mock_sel) == "failed"
        assert "; kill failed: ValueError: kill_process_group: refusing" in (job.last_error or "")
        assert "6464" in (job.last_error or "")
        assert "torn2" not in svc._claims

    @pytest.mark.asyncio
    async def test_the_reap_reaches_the_process_of_a_teardown_the_real_manager_holds(
        self, tmp_path: object
    ) -> None:
        """End to end through ``SessionManager``: the run's reset hangs, the reap kills, the table empties.

        The run task resets its own session and hangs in the provider shutdown;
        the reaper arrives after that pop. Through the real manager the handle
        comes from the torn-down table, the kill goes to the pre-pop pid, the
        audit says ``reaped`` for a delivered kill -- and the reap's cancel of the
        run task ends the hung teardown, whose scope releases the entry: the
        table is empty once the teardown is over.
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
            return provider

        mgr = SessionManager(KiroCrewConfig(), provider_factory=_factory)
        provider, _, _ = await mgr.get_or_create("cron:real1")
        mgr.release("cron:real1")
        # Above the kernel's pid ceiling: even an unpatched probe cannot meet a
        # real process under it.
        pid = 2**22 + 6565
        client = MagicMock()
        client._pid = pid
        client._child_pids = {}
        client._start_time = _START_ID
        provider._client = client
        hang = asyncio.Event()

        async def _hung_shutdown() -> None:
            # flake-ok: the fake shutdown hangs until the reap cancels it or the test's finally sets the event
            await hang.wait()

        provider.shutdown = AsyncMock(side_effect=_hung_shutdown)

        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = mgr
        job = _make_job("real1")
        svc._jobs = [job]
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_group", return_value=True) as group_kill,
            # The run's own teardown (``SessionLifecycle.reset``), resumed by the
            # reap's cancel, signals the pid it holds through its own seams.
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async", AsyncMock(return_value=True)
            ) as teardown_tree_kill,
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock(return_value=True)),
        ):
            # The run's own finally reset: pops the session, then hangs in the
            # provider shutdown. Reap only once it is IN the shutdown: the reap's
            # cancel of the run task must land there (deferred by ``reset`` past
            # its kill-and-sweep, then re-raised); a cancel landing one await
            # earlier, at the end-record crumb hop, is absorbed by design and the
            # teardown runs on into the hang.
            run_teardown = asyncio.create_task(mgr.reset("cron:real1"))
            try:
                await async_wait_until(
                    lambda: not mgr.has_session("cron:real1") and provider.shutdown.await_count,
                    describe=lambda: (
                        f"session held: {mgr.has_session('cron:real1')}, "
                        f"shutdowns: {provider.shutdown.await_count}"
                    ),
                )
                assert provider.shutdown.await_count == 1, "the teardown never reached the shutdown"
                assert mgr.tearing_down("cron:real1") != []
                # The client's own reset, after a kill it could not confirm,
                # clears the recorded pid with the process still standing: the
                # reap must kill on the identity the table captured at the pop.
                client._pid = None
                claim = svc._claims["real1"] = _RunClaim(
                    trigger="scheduled",
                    claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60,
                    task=run_teardown,
                )

                await svc._force_reap("real1", _JOB_TIMEOUT_SECS + 60, claim=claim)

                # The reap's kill went through the torn-down handle to the pre-pop
                # pid's CAPTURED group. Its cancel of the run task
                # (``RunClaims.finish_taken``) lands while the reap persists its
                # record: ``reset`` defers the cancellation past its own
                # kill-and-sweep, so the resumed teardown may signal the same pid
                # once more through its own seams before re-raising -- every kill
                # here names the run's own process, none a successor's.
                assert group_kill.call_args_list == [call(pid, platform_compat.SIGKILL)]
                assert {c.args[0] for c in teardown_tree_kill.await_args_list} <= {pid}
                assert _audited_outcome(mock_sel) == "reaped"
                assert "kill failed" not in (job.last_error or "")
                assert "real1" not in svc._claims
                # The cancel ends the hung teardown, and the scope the facade
                # opened around it releases the entry.
                with pytest.raises(asyncio.CancelledError):
                    # flake-ok: collects the CancelledError of the cancel the reap issues; a missing cancel is a hang the suite's 120 s timeout names
                    await run_teardown
            finally:
                # A failed assertion must not leave the hung teardown pending
                # past the patches (its deferred kill-and-sweep would then run
                # unstubbed): release the hang, cancel, and let it finish here.
                hang.set()
                run_teardown.cancel()
                # flake-ok: the event is set and the task cancelled just above; the gather only collects the teardown
                await asyncio.gather(run_teardown, return_exceptions=True)

        assert mgr.tearing_down("cron:real1") == [], "the torn-down entry outlived its teardown"

    @pytest.mark.asyncio
    async def test_the_reap_reaches_every_teardown_the_real_manager_holds_under_the_key(
        self, tmp_path: object
    ) -> None:
        """Two hung teardowns under one key -- the run's own and a successor's -- and both processes are answered.

        The run's reset pops its session and hangs; a late completion cold-starts
        a successor under the key; the successor's own reset pops it and hangs as
        well. A table that retained the first popper alone let the reap kill the
        run's process, record ``reaped``, and leave the successor's hung process
        standing with nothing left to find it by. Every teardown in flight is
        retained, so the reap's snapshot names both handles and kills both.
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
            return provider

        mgr = SessionManager(KiroCrewConfig(), provider_factory=_factory)
        hang = asyncio.Event()

        async def _hung_shutdown() -> None:
            # flake-ok: the fake shutdown hangs until the reap cancels it or the test's finally sets the event
            await hang.wait()

        async def _torn_down_and_hung(pid: int) -> tuple[Any, asyncio.Task[Any]]:
            provider, _, _ = await mgr.get_or_create("cron:real2")
            mgr.release("cron:real2")
            client = MagicMock()
            client._pid = pid
            client._child_pids = {}
            client._start_time = _START_ID
            provider._client = client
            provider.shutdown = AsyncMock(side_effect=_hung_shutdown)
            teardown = asyncio.create_task(mgr.reset("cron:real2"))
            await async_wait_until(
                lambda: not mgr.has_session("cron:real2") and provider.shutdown.await_count,
                describe=lambda: (
                    f"session held: {mgr.has_session('cron:real2')}, "
                    f"shutdowns: {provider.shutdown.await_count}"
                ),
            )
            assert provider.shutdown.await_count == 1, "the teardown never reached the shutdown"
            return provider, teardown

        run_pid, successor_pid = 2**22 + 6666, 2**22 + 6767
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = mgr
        job = _make_job("real2")
        svc._jobs = [job]
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_group", return_value=True) as group_kill,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async", AsyncMock(return_value=True)
            ),
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock(return_value=True)),
        ):
            # The run's own finally reset pops its session and hangs; then a late
            # completion's successor is popped by ITS reset and hangs too.
            run_provider, run_teardown = await _torn_down_and_hung(run_pid)
            successor_provider, successor_teardown = await _torn_down_and_hung(successor_pid)
            try:
                claim = svc._claims["real2"] = _RunClaim(
                    trigger="scheduled",
                    claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60,
                    task=run_teardown,
                )

                await svc._force_reap("real2", _JOB_TIMEOUT_SECS + 60, claim=claim)

                signalled = sorted(c.args[0] for c in group_kill.call_args_list)
                assert signalled == [run_pid, successor_pid], (
                    "the successor's hung teardown was invisible to the reap: only "
                    f"{signalled} signalled"
                )
                assert _audited_outcome(mock_sel) == "reaped"
                assert "kill failed" not in (job.last_error or "")
                # The reap's cancel ends the run's teardown; the successor's is not
                # the run task and keeps its entry until it ends.
                with pytest.raises(asyncio.CancelledError):
                    # flake-ok: collects the CancelledError of the cancel the reap issues; a missing cancel is a hang the suite's 120 s timeout names
                    await run_teardown
                assert [entry.session.provider for entry in mgr.tearing_down("cron:real2")] == [
                    successor_provider
                ]
            finally:
                hang.set()
                for teardown in (run_teardown, successor_teardown):
                    teardown.cancel()
                # flake-ok: the event is set and both tasks cancelled just above; the gather only collects the teardowns
                await asyncio.gather(run_teardown, successor_teardown, return_exceptions=True)

        assert mgr.tearing_down("cron:real2") == []

    @pytest.mark.asyncio
    async def test_a_process_that_survives_a_completed_reset_still_gets_the_kill(
        self, tmp_path: object
    ) -> None:
        """A reset that returned True is not proof the process is gone.

        The reset's own shutdown can fail without raising out of it. After every
        completed reset the handle is asked -- pid plus recorded start id -- and
        a process that still stands gets the fallback; its outcome, not the
        reset's boolean, is what the record says.
        """
        svc, job, claim = _overdue_reap_fixture(tmp_path, "survived1")
        _session_with_pid(svc, "cron:survived1", 5959)
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
            await svc._force_reap("survived1", elapsed=_JOB_TIMEOUT_SECS + 60, claim=claim)

        group_kill.assert_called_once_with(5959, platform_compat.SIGKILL)
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_survivor_the_kill_cannot_signal_is_a_failed_kill_not_reaped(
        self, tmp_path: object
    ) -> None:
        """The survivor's fallback is audited on its own outcome: refused here, so ``failed``."""
        svc, job, claim = _overdue_reap_fixture(tmp_path, "survived2")
        _session_with_pid(svc, "cron:survived2", 6060)
        svc._sessions.reset = AsyncMock(return_value=True)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group",
                side_effect=ValueError(
                    "kill_process_group: refusing broadcast/self process group 6060"
                ),
            ),
        ):
            await svc._force_reap("survived2", elapsed=_JOB_TIMEOUT_SECS + 60, claim=claim)

        assert _audited_outcome(mock_sel) == "failed"
        assert "; kill failed: ValueError: kill_process_group: refusing" in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_process_gone_after_a_completed_reset_is_nothing_to_kill(
        self, tmp_path: object
    ) -> None:
        """Control: the reset did its job -- no start id and no process behind the pid, no kill."""
        svc, job, claim = _overdue_reap_fixture(tmp_path, "gone1")
        _session_with_pid(svc, "cron:gone1", 6161)
        svc._sessions.reset = AsyncMock(return_value=True)

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=None),
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            _group_gone(),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
        ):
            await svc._force_reap("gone1", elapsed=_JOB_TIMEOUT_SECS + 60, claim=claim)

        tree_kill.assert_not_awaited()
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_an_exited_pid_whose_start_id_still_reads_back_is_nothing_to_kill(
        self, tmp_path: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Control: identity alone is not liveness -- an exited pid the platform confirms is no survivor.

        On Windows the creation time reads back through a query handle for as long
        as any handle to the exited process object is held (the transport's, until
        GC), so a just-exited child answers the recorded start id while
        ``pid_exists`` (which confirms the exit code) says gone. Without the
        liveness read every completed reset there logged a survivor and spawned
        two ``taskkill`` runs against a dead pid before recording ``reaped``.
        """
        svc, job, claim = _overdue_reap_fixture(tmp_path, "exited1")
        _session_with_pid(svc, "cron:exited1", 6767)
        svc._sessions.reset = AsyncMock(return_value=True)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=_START_ID),
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock()) as pid_kill,
        ):
            await svc._force_reap("exited1", elapsed=_JOB_TIMEOUT_SECS + 60, claim=claim)

        tree_kill.assert_not_awaited()
        pid_kill.assert_not_awaited()
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_pid_recycled_after_a_completed_reset_is_nothing_to_kill(
        self, tmp_path: object
    ) -> None:
        """Control: a different start id behind the pid means the run's process is gone."""
        svc, job, claim = _overdue_reap_fixture(tmp_path, "recycled1")
        _session_with_pid(svc, "cron:recycled1", 6262)
        svc._sessions.reset = AsyncMock(return_value=True)

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value="9999999"),
            _group_gone(),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
        ):
            await svc._force_reap("recycled1", elapsed=_JOB_TIMEOUT_SECS + 60, claim=claim)

        tree_kill.assert_not_awaited()
        assert _audited_outcome(mock_sel) == "reaped"

    @pytest.mark.asyncio
    async def test_a_reset_that_finds_no_session_and_no_handle_is_nothing_to_kill(
        self, tmp_path: object
    ) -> None:
        """Control: no session before the reset either -- the run had no process to answer for."""
        svc, job, claim = _overdue_reap_fixture(tmp_path, "nosess1")
        svc._sessions.reset = AsyncMock(return_value=False)

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
        ):
            await svc._force_reap("nosess1", elapsed=_JOB_TIMEOUT_SECS + 60, claim=claim)

        tree_kill.assert_not_awaited()
        assert _audited_outcome(mock_sel) == "reaped"

    @pytest.mark.asyncio
    async def test_a_reset_that_succeeds_never_reaches_the_kill(self, tmp_path: object) -> None:
        """Control: the SIGKILL is the fallback, so a reset that returns keeps ``reaped``."""
        svc, job, claim = _overdue_reap_fixture(tmp_path, "reset1")
        svc._sessions.reset = AsyncMock()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
        ):
            await svc._force_reap("reset1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        tree_kill.assert_not_awaited()
        assert _audited_outcome(mock_sel) == "reaped"
