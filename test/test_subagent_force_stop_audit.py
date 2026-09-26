"""The subagent force-stop path records what actually happened to the process.

Twin of the cron reaper's contract (``test_cron_reaper.py``): ``_sigkill_session``
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
tree cases themselves are the shared module's, proven in ``test_cron_reaper.py``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import platform_compat
from kiro_crew.acp.client import AcpProcessDied
from kiro_crew.config import KiroCrewConfig
from kiro_crew.process_identity import MAX_ERROR_DETAIL_LEN, ProcessHandle
from kiro_crew.session import SessionManager
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.subagent_persistence import create_agent_folder, update_state

# The start id the fake client records at spawn (``platform_compat.get_process_start_id``
# reads ``/proc/<pid>/stat`` field 22 on Linux); the kill re-reads it before signalling.
_START_ID = "4821903"
_REFUSAL = ValueError("kill_process_group: refusing broadcast/self process group 4242")


@pytest.fixture(autouse=True)
def _no_tree_behind_the_leader() -> Iterator[None]:
    """The leader's process group is empty and no Windows tree cleanup is pending.

    Pinned for every test: the shared kill asks a gone or recycled leader's
    tree before calling it gone, and the probes are real otherwise. A test that
    asserts on either probe patches it again inside its own ``with`` (the inner
    patch wins).
    """
    with (
        patch("kiro_crew.platform_compat.pgroup_exists", return_value=False),
        patch("kiro_crew.platform_compat.windows_tree_cleanup_pending", return_value=False),
    ):
        yield


@pytest.fixture(autouse=True)
def _leads_its_own_group() -> Iterator[None]:
    """The fabricated root leads its own POSIX group: ``getpgid(pid) == pid``, ours is 1.

    The same pin the cron reaper's tests hold (``_isolated_leader``): the capture
    site ``isolated_group_of`` reads both names through ``getattr`` and they are
    CREATED on a runner that lacks them, so the handle captures the group on
    every platform the POSIX-shaped tests run on, and the kill then addresses
    THAT captured id through ``kill_process_group`` -- which these tests pin --
    never a group resolved from the pid at signal time. A Windows-shaped test
    (``IS_WINDOWS`` patched) captures no group and goes through the pinned tree
    drain instead.
    """
    with (
        patch("os.getpgid", side_effect=lambda pid: pid, create=True),
        patch("os.getpgrp", return_value=1, create=True),
    ):
        yield


def _mock_sessions() -> MagicMock:
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.release = MagicMock()
    sessions.reset = AsyncMock(return_value=True)
    sessions.tearing_down = MagicMock(return_value=[])
    sessions._sessions = {}
    return sessions


def _mock_ctx_builder() -> MagicMock:
    ctx = MagicMock()
    ctx.build_message = MagicMock(return_value=("built_message", None))
    ctx.hooks.on_tool_call = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = False
    return ctx


def _session_with_pid(
    manager: SubagentManager,
    session_key: str,
    pid: int | None,
    *,
    start_id: str | None = _START_ID,
    child_pids: dict[Any, Any] | None = None,
) -> MagicMock:
    """Register a session under ``session_key`` whose ACP client reports ``pid``.

    The client carries the start id the recycled-pid check compares, so a test
    whose start-id read answers the same value drives ``_sigkill_session`` all
    the way to the group kill.
    """
    client = MagicMock()
    client._pid = pid
    client._child_pids = dict(child_pids or {})
    client._start_time = start_id
    session = MagicMock()
    session.provider._client = client
    manager._sessions._sessions[session_key] = session
    return client


def _overdue_run(
    agent_id: str, *, pid: int | None = 4242, user_stopped: bool = False
) -> tuple[SubagentManager, SubagentInfo, str]:
    """A manager with one overdue run whose session process reports ``pid``."""
    manager = SubagentManager(
        sessions=_mock_sessions(),
        ctx_builder=_mock_ctx_builder(),
        on_done=AsyncMock(),
        on_event=AsyncMock(),
        is_yolo=lambda: True,
    )
    info = SubagentInfo(
        id=agent_id,
        task="stuck task",
        parent_session_key="dashboard:test-slot",
        started=time.time() - 7200,
        user_stopped=user_stopped,
    )
    manager._agents[agent_id] = info
    manager._running_count = 1
    session_key = f"subagent:{agent_id}"
    if pid is not None:
        _session_with_pid(manager, session_key, pid)
    return manager, info, session_key


def _hanging_reset(manager: SubagentManager) -> None:
    """A reset that never returns: the caller's ``wait_for`` times out."""

    async def _hang(session_key: str, **_: Any) -> bool:
        await asyncio.sleep(999)
        return True

    manager._sessions.reset = AsyncMock(side_effect=_hang)


def _kill_path_stubs() -> tuple[Any, Any, Any, Any]:
    """The child-tree probe, the liveness probe, the start-id read and the sweep stubbed.

    Only the kill decides: the pid exists and its start id reads back as the
    recorded one, so the root is this run's and the signal is reached.
    """
    return (
        patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
        patch("kiro_crew.platform_compat.pid_exists", return_value=True),
        patch("kiro_crew.platform_compat.get_process_start_id", return_value=_START_ID),
        patch("kiro_crew.acp.client._kill_escaped_children"),
    )


def _audit(mock_sel: MagicMock, tool_name: str) -> dict[str, Any]:
    """The kwargs of the one SEL audit row written under ``tool_name``."""
    rows = [
        call.kwargs
        for call in mock_sel().log_tool_invocation.call_args_list
        if call.kwargs.get("tool_name") == tool_name
    ]
    assert len(rows) == 1, f"expected one {tool_name} audit row, saw {len(rows)}"
    return rows[0]


def _handle_of(manager: SubagentManager, agent_id: str) -> Any:
    """The kill handle the teardown paths take before their reset and hand to the kill."""
    handles = manager._retain_process_handles(agent_id, f"subagent:{agent_id}")
    assert handles, f"no session registered for {agent_id}"
    return handles[0]


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
        back -- and finds the identity no longer answers (not drained). The
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


# ── The reap names the run's own session ──


class TestTheReapNamesTheRunsOwnSession:
    """A continuation lives under its conversation's key, not ``subagent:<run id>``.

    ``spawn_continue`` mints a new run id on the ORIGINAL run's conversation key
    (``info.conversation_key``), and the run's own teardown resets that key. A
    reap that derives the key from the run id resets and releases a key no
    session is under: the retain misses, the fallback has nothing to signal,
    and the audit reads ``reaped`` for a process -- and a session lease -- the
    stop never touches.
    """

    @pytest.mark.asyncio
    async def test_a_continuation_run_is_stopped_under_its_conversation_key(self) -> None:
        manager, info, _run_key = _overdue_run("cont-run", pid=None)
        info.conversation_key = "subagent:cont-orig"
        _session_with_pid(manager, "subagent:cont-orig", 7373)
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
            await manager._force_reap("cont-run", info, 7200.0)

        # The reset, the kill and the release all name the conversation's key.
        manager._sessions.reset.assert_awaited_once()
        assert manager._sessions.reset.await_args.args == ("subagent:cont-orig",)
        group_kill.assert_called_once_with(7373, platform_compat.SIGKILL)
        row = _audit(mock_sel, "reaper_force_kill")
        assert row["outcome"] == "reaped"
        assert row["session_key"] == "subagent:cont-orig"
        manager._sessions.release.assert_called_once_with("subagent:cont-orig", cleanup=False)
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
            await asyncio.wait_for(popped.wait(), timeout=5)
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
            await asyncio.wait_for(popped.wait(), timeout=5)
            assert manager._process_handles["race2"][0].pid == 8282

            await manager._teardown_run_session(info, key)
            group_kill.assert_called_once_with(8282, platform_compat.SIGKILL)

            hang.set()
            await asyncio.wait_for(reap, timeout=5)

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
            await asyncio.wait_for(popped.wait(), timeout=5)
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
            await asyncio.wait_for(popped.wait(), timeout=5)
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
        manager._sessions.tearing_down = MagicMock(
            side_effect=lambda session_key: [torn] if session_key == key else []
        )
        children, alive, start_id, sweep = _kill_path_stubs()
        _hanging_reset(manager)

        with (
            patch("kiro_crew.subagent.Stats"),
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
        manager._sessions.tearing_down = MagicMock(
            side_effect=lambda session_key: [first, successor] if session_key == key else []
        )
        children, alive, _start_id, sweep = _kill_path_stubs()
        _hanging_reset(manager)

        with (
            patch("kiro_crew.subagent.Stats"),
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


def _reset_pops_a_late_successor(
    manager: SubagentManager, successor: Any
) -> tuple[list[Any], asyncio.Event]:
    """A session manager whose reset pops ``successor`` -- registered AFTER the snapshot -- and hangs.

    ``teardown_scope(on_pop=...)`` records the hook a teardown path hands it;
    the reset then runs that hook with the session it pops, in the same lock
    hold as the pop, exactly as ``SessionLifecycle.reset`` does through the
    scope, and hangs. Returns the recorded hooks (empty when the path never
    asked for a scope) and the event set once the pop happened.
    """
    hooks: list[Any] = []
    popped = asyncio.Event()

    def _scope(on_pop: Any = None) -> Any:
        hooks.append(on_pop)
        return MagicMock(name="teardown_scope")

    async def _reset(session_key: str, **kwargs: Any) -> bool:
        for hook in hooks:
            if hook is not None:
                hook(successor)
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


# ── The record is persisted before it is published ──


class TestTheRecordCarriesTheKillBeforeItIsPublished:
    """A failed kill reaches the ONE terminal report and the tombstone, not memory.

    The common shape of a reap on a live run: the reap's reset closes the ACP
    pipes, the run's in-flight stream raises ``AcpProcessDied``, and the run's
    own arm writes the record (``done``, the tombstone) while the reap is still
    awaiting that reset or the fallback kill that follows it. A run ``finally``
    that claimed and published the terminal report then would send the
    ``subagent_done`` event and the parent's completion before the kill has
    decided, and a failure the fallback reports afterwards would reach only the
    in-memory error text -- the tombstone on disk and the report the parent
    reads would both say the run was simply reaped. Once a reap has started,
    it owns the report: it claims after its kill has decided, with the failure
    appended and the tombstone re-written.
    """

    @pytest.mark.asyncio
    async def test_a_failed_kill_reaches_the_report_when_the_stream_dies_under_the_reset(
        self,
    ) -> None:
        manager, info, _key = _overdue_run("pub1", pid=9191)
        info._session_sharing = False
        reset_started = asyncio.Event()
        hang = asyncio.Event()

        async def _reset(session_key: str, **_: Any) -> bool:
            if manager._sessions._sessions.pop(session_key, None) is not None:
                # The reap's reset: pops the session, then hangs in the
                # provider shutdown; the run's stream dies under it (below).
                reset_started.set()
                await hang.wait()
                return True
            # The run's own teardown, arriving second: the key is already gone.
            return False

        async def _stream(_info: SubagentInfo, _session_key: str) -> None:
            await reset_started.wait()
            raise AcpProcessDied("Runtime process died during prompt — killed (provider shutdown)")

        manager._sessions.reset = AsyncMock(side_effect=_reset)
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch("kiro_crew.subagent._RESET_TIMEOUT", 0.05),
            patch("kiro_crew.subagent.write_tombstone") as tombstones,
            patch.object(manager, "_run_inner", AsyncMock(side_effect=_stream)),
            children,
            alive,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group",
                MagicMock(side_effect=_REFUSAL),
            ),
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock()),
        ):
            run_task = asyncio.create_task(manager._run(info))
            manager._tasks["pub1"] = run_task
            await asyncio.sleep(0)

            await manager._force_reap("pub1", info, 7200.0)

            hang.set()
            await asyncio.wait_for(asyncio.gather(run_task, return_exceptions=True), timeout=5)

        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "failed"
        # ONE terminal report, published after the kill decided, naming the failure.
        done_events = [
            call.args
            for call in manager._on_event.await_args_list
            if call.args[0] == "subagent_done"
        ]
        assert len(done_events) == 1, f"expected one subagent_done event, saw {len(done_events)}"
        published_error = done_events[0][2]["error"] or ""
        assert "; kill failed: ValueError: kill_process_group: refusing" in published_error, (
            "the parent was told the run was reaped while its process is still alive: "
            f"{published_error!r}"
        )
        assert manager._on_done.await_count == 1
        # The tombstone on disk carries the failure too: the record was
        # re-written after the kill decided, not left as the run's arm wrote it.
        assert tombstones.call_args is not None, "no tombstone was written"
        assert "; kill failed: ValueError: kill_process_group: refusing" in (
            tombstones.call_args.kwargs["detail"]
        ), "the tombstone says the run was reaped while its process is still alive"
        assert info.done and info.reaped
        assert manager._process_handles == {}


# ── A report the run already delivered is not contradicted by the reap ──


class TestADeliveredRecordIsNotRewrittenByTheReap:
    """A run that completed and reported under its own reap keeps the record it delivered.

    The ``done``-already-set arm of the reap's record exists for the run whose
    stream died UNDER the reap's reset: that arm writes the record and leaves
    the report to the reap, so the reap appends the kill failure and re-writes
    the tombstone before publishing. But ``done`` is also set by a run that
    finishes on its own at the deadline boundary -- a result, or an exception
    that was not the teardown's -- and ``_run`` then claims and publishes ITS
    OWN report. Appending the failure and re-writing the tombstone after that
    left the record on disk saying ``failed`` while the completion the parent
    received said the run succeeded, and nothing re-reconciled the two. The
    finalize token tells the two arms apart: once it is taken, the record
    stands as delivered and the failure is kept on the reap's audit row.
    """

    @pytest.mark.asyncio
    async def test_a_run_that_completed_and_reported_keeps_its_record(self) -> None:
        manager, info, _key = _overdue_run("late1", pid=9393)
        info._session_sharing = False
        # The run finished on its own inside the reap window and ``_run``
        # published: ``done`` set, a result, no error, the report claimed.
        info.done = True
        info.result = "the answer"
        info.error = ""
        assert manager._claim_finalize(info)
        _hanging_reset(manager)
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch("kiro_crew.subagent._RESET_TIMEOUT", 0.05),
            patch("kiro_crew.subagent.write_tombstone") as tombstones,
            children,
            alive,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group",
                MagicMock(side_effect=_REFUSAL),
            ),
        ):
            await manager._force_reap("late1", info, 7200.0)
            await asyncio.wait_for(
                asyncio.gather(*manager._report_tasks, return_exceptions=True), timeout=5
            )

        # The delivered record stands: no failure appended, no tombstone written
        # over a completion the parent already holds.
        assert info.error == "", (
            "the reap re-wrote a record whose report the run had already delivered: "
            f"{info.error!r}"
        )
        assert info.outcome == "completed"
        tombstones.assert_not_called()
        # The reap publishes nothing of its own: the claim was the run's.
        assert _done_events(manager) == []
        assert manager._on_done.await_count == 0
        # The failure is kept where it is still true: the audit row.
        audit = _audit(mock_sel, "reaper_force_kill")
        assert audit["outcome"] == "failed"
        assert "kill_process_group: refusing" in audit["metadata"]["kill_failed"], (
            "the audit row lost the only record of the kill failure: " f"{audit['metadata']!r}"
        )
        assert info.reaped and manager._reaps_in_flight == {}

    @pytest.mark.asyncio
    async def test_a_record_the_reap_still_owns_the_report_of_is_re_written(self) -> None:
        """Control: ``done`` set by the reap-echo arm (no claim) still gets the failure appended."""
        manager, info, _key = _overdue_run("late2", pid=9494)
        info._session_sharing = False
        # The reap-echo arm's record: ``done`` and an error, the report left to the reap.
        info.done = True
        info.error = "Runtime process died during prompt — killed (provider shutdown)"
        assert not info._finalized
        _hanging_reset(manager)
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch("kiro_crew.subagent._RESET_TIMEOUT", 0.05),
            patch("kiro_crew.subagent.write_tombstone") as tombstones,
            children,
            alive,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group",
                MagicMock(side_effect=_REFUSAL),
            ),
        ):
            await manager._force_reap("late2", info, 7200.0)
            await asyncio.wait_for(
                asyncio.gather(*manager._report_tasks, return_exceptions=True), timeout=5
            )

        assert "; kill failed: ValueError: kill_process_group: refusing" in info.error
        assert tombstones.call_count == 1
        assert "; kill failed:" in tombstones.call_args.kwargs["detail"]
        # The reap took the claim and published the one report, carrying the failure.
        done_events = _done_events(manager)
        assert len(done_events) == 1 and done_events[0]["error"] == info.error
        assert _audit(mock_sel, "reaper_force_kill")["metadata"]["kill_failed"].startswith(
            "ValueError: kill_process_group: refusing"
        )


# ── A stop cancelled mid-teardown still owes the report ──


def _shutdown_mid_reset(manager: SubagentManager) -> tuple[asyncio.Event, asyncio.Event]:
    """The reap's reset pops the session and then hangs until ``hang`` is set.

    Returns ``(reset_started, hang)``. The run's own teardown, arriving second
    under the popped key, returns False at once.
    """
    reset_started = asyncio.Event()
    hang = asyncio.Event()

    async def _reset(session_key: str, **_: Any) -> bool:
        if manager._sessions._sessions.pop(session_key, None) is not None:
            reset_started.set()
            await hang.wait()
            return True
        return False

    manager._sessions.reset = AsyncMock(side_effect=_reset)
    return reset_started, hang


def _done_events(manager: SubagentManager) -> list[dict[str, Any]]:
    """The payloads of every ``subagent_done`` event the manager fired."""
    return [
        call.args[2]
        for call in manager._on_event.await_args_list
        if call.args[0] == "subagent_done"
    ]


def _pending_reports_for(manager: SubagentManager, agent_id: str) -> list[Any]:
    """The live terminal-report tasks owned by ``agent_id`` (the ones ``cancel_all`` drains)."""
    return [
        task
        for task, owner in manager._report_owners.items()
        if owner.id == agent_id and not task.done()
    ]


class TestAStopCancelledMidTeardownStillReports:
    """A reap cancelled before its kill decided still publishes the ONE terminal report.

    A gateway shutdown runs ``cancel_all()``, which cancels the reaper task while
    ``_force_reap`` awaits the reset, or the fallback kill after it. By then the
    reset has usually already killed the run's runtime, so the run's own
    reap-echo arm has written the record and the tombstone and left the report
    to the reap. Two things keep that outcome, and both are pinned here: the
    report task exists -- strongly held and drained by ``cancel_all`` -- before
    the reap's first await, so every cancellation point inside the reset and
    kill window has a report to complete, and it publishes only once the record
    is final; and the reap's ``CancelledError`` arm records the kill it did not
    get to decide as a failure the record names (never ``reaped``), releases the
    report, and only then re-raises. Without the first, no cancellation point
    inside the window has a report to reach; without the second, the record
    stays unfinished and the report unreleased. The tombstone the run's arm
    wrote keeps excluding the folder from orphan recovery, because the
    completion did reach the parent.
    """

    @pytest.mark.asyncio
    async def test_a_reap_cancelled_at_shutdown_mid_reset_still_publishes_the_report(
        self,
    ) -> None:
        manager, info, _key = _overdue_run("shut1", pid=9292)
        info._session_sharing = False
        reset_started, _hang = _shutdown_mid_reset(manager)

        async def _stream(_info: SubagentInfo, _session_key: str) -> None:
            # The run's runtime dies under the reap's reset: the reap-echo arm
            # writes the record and leaves the report to the reap.
            await reset_started.wait()
            raise AcpProcessDied("Runtime process died during prompt — killed (provider shutdown)")

        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch("kiro_crew.subagent._REPORT_DRAIN_TIMEOUT", 5.0),
            patch("kiro_crew.subagent.write_tombstone") as tombstones,
            patch("kiro_crew.subagent.clear_tombstone") as cleared,
            patch.object(manager, "_run_inner", AsyncMock(side_effect=_stream)),
            children,
            alive,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_group"),
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock()),
        ):
            run_task = asyncio.create_task(manager._run(info))
            manager._tasks["shut1"] = run_task
            await asyncio.sleep(0)

            reap = asyncio.create_task(manager._force_reap("shut1", info, 7200.0))
            manager._reaper_task = reap
            # The run's arm has written its record and its own teardown has
            # finished; the reap is still parked in its reset.
            await asyncio.wait_for(asyncio.gather(run_task, return_exceptions=True), timeout=5)
            assert info.done and not reap.done()
            # The report already exists while the reap sits inside its window --
            # the task a cancellation anywhere in that window has to complete --
            # and has not published: the kill has not decided.
            assert _pending_reports_for(
                manager, "shut1"
            ), "no terminal report task exists while the reap sits inside its reset window"
            assert _done_events(manager) == []
            tombstones_before_shutdown = tombstones.call_count

            await asyncio.wait_for(manager.cancel_all(), timeout=5)

            # The cancellation still goes through the reap.
            with pytest.raises(asyncio.CancelledError):
                await reap
            await asyncio.wait_for(
                asyncio.gather(*manager._report_tasks, return_exceptions=True), timeout=5
            )

        # The parent still receives the ONE terminal report, and it names the
        # outcome: the deadline reap (the run's arm wrote that record when its
        # stream died under the reset), and the kill the cancellation cut short.
        done_events = _done_events(manager)
        assert len(done_events) == 1, f"expected one subagent_done event, saw {len(done_events)}"
        assert manager._on_done.await_count == 1
        published_error = done_events[0]["error"] or ""
        assert published_error.startswith(
            "reaped after 7200s (deadline) — the runtime was torn down before the run finished"
        ), published_error
        assert (
            "; kill failed: CancelledError: the stop was cancelled before its kill decided"
            in published_error
        ), f"the report does not say the kill was cut short: {published_error!r}"
        assert done_events[0]["outcome"] == "failed"
        # The kill never decided, so the audit never says ``reaped``.
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "failed"
        # The tombstone still excludes the run from orphan recovery -- re-written
        # with the failure, never cleared: the completion reached the parent.
        assert tombstones.call_count == tombstones_before_shutdown + 1
        assert tombstones.call_args.kwargs["cause"] == "reaped"
        assert (
            "; kill failed: CancelledError: the stop was cancelled before its kill decided"
            in tombstones.call_args.kwargs["detail"]
        )
        cleared.assert_not_called()
        assert info.done and info.reaped
        assert manager._running_count == 0
        assert manager._process_handles == {}
        assert manager._report_owners == {}

    @pytest.mark.asyncio
    async def test_a_reap_cancelled_inside_the_fallback_kill_still_publishes_the_report(
        self,
    ) -> None:
        """The other cancellation point in the window: the reset timed out, the kill is in flight."""
        manager, info, _key = _overdue_run("shut3", pid=9494)
        info._session_sharing = False
        _hanging_reset(manager)
        kill_started = asyncio.Event()
        never = asyncio.Event()

        async def _hanging_kill(*_: Any, **__: Any) -> None:
            kill_started.set()
            await never.wait()

        async def _stream(_info: SubagentInfo, _session_key: str) -> None:
            await never.wait()

        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch("kiro_crew.subagent._RESET_TIMEOUT", 0.05),
            patch("kiro_crew.subagent._REPORT_DRAIN_TIMEOUT", 5.0),
            patch("kiro_crew.subagent.write_tombstone") as tombstones,
            patch.object(manager, "_run_inner", AsyncMock(side_effect=_stream)),
            children,
            alive,
            start_id,
            sweep,
            # The group signal fails; the kill's pid-scoped fallback is the await
            # the cancellation lands in.
            patch(
                "kiro_crew.platform_compat.kill_process_group",
                MagicMock(side_effect=PermissionError("[Errno 1] Operation not permitted")),
            ),
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock(side_effect=_hanging_kill)),
        ):
            run_task = asyncio.create_task(manager._run(info))
            manager._tasks["shut3"] = run_task
            await asyncio.sleep(0)

            reap = asyncio.create_task(manager._force_reap("shut3", info, 7200.0))
            manager._reaper_task = reap
            await asyncio.wait_for(kill_started.wait(), timeout=5)
            assert not info.done and not reap.done()
            assert _pending_reports_for(
                manager, "shut3"
            ), "no terminal report task exists while the reap sits inside its kill"

            await asyncio.wait_for(manager.cancel_all(), timeout=5)

            with pytest.raises(asyncio.CancelledError):
                await reap
            await asyncio.wait_for(asyncio.gather(run_task, return_exceptions=True), timeout=5)
            await asyncio.wait_for(
                asyncio.gather(*manager._report_tasks, return_exceptions=True), timeout=5
            )

        done_events = _done_events(manager)
        assert len(done_events) == 1, f"expected one subagent_done event, saw {len(done_events)}"
        assert manager._on_done.await_count == 1
        published_error = done_events[0]["error"] or ""
        assert published_error.startswith("Reaped after"), published_error
        assert (
            "; kill failed: CancelledError: the stop was cancelled before its kill decided"
            in published_error
        )
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "failed"
        assert tombstones.call_count == 1
        assert info.done and info.reaped
        assert manager._process_handles == {}
        assert manager._report_owners == {}

    @pytest.mark.asyncio
    async def test_a_reap_cancelled_while_the_run_is_still_live_reports_exactly_once(
        self,
    ) -> None:
        """The cancel lands before the run's stream died: the reap's record wins, once."""
        manager, info, _key = _overdue_run("shut2", pid=9393)
        info._session_sharing = False
        reset_started, _hang = _shutdown_mid_reset(manager)
        never = asyncio.Event()

        async def _stream(_info: SubagentInfo, _session_key: str) -> None:
            await never.wait()

        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch("kiro_crew.subagent._REPORT_DRAIN_TIMEOUT", 5.0),
            patch("kiro_crew.subagent.write_tombstone") as tombstones,
            patch.object(manager, "_run_inner", AsyncMock(side_effect=_stream)),
            children,
            alive,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_group"),
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock()),
        ):
            run_task = asyncio.create_task(manager._run(info))
            manager._tasks["shut2"] = run_task
            await asyncio.sleep(0)

            reap = asyncio.create_task(manager._force_reap("shut2", info, 7200.0))
            manager._reaper_task = reap
            await asyncio.wait_for(reset_started.wait(), timeout=5)
            assert not info.done and not reap.done()

            await asyncio.wait_for(manager.cancel_all(), timeout=5)

            with pytest.raises(asyncio.CancelledError):
                await reap
            await asyncio.wait_for(asyncio.gather(run_task, return_exceptions=True), timeout=5)
            await asyncio.wait_for(
                asyncio.gather(*manager._report_tasks, return_exceptions=True), timeout=5
            )

        done_events = _done_events(manager)
        assert len(done_events) == 1, f"expected one subagent_done event, saw {len(done_events)}"
        assert manager._on_done.await_count == 1
        published_error = done_events[0]["error"] or ""
        assert published_error.startswith("Reaped after"), published_error
        assert (
            "; kill failed: CancelledError: the stop was cancelled before its kill decided"
            in published_error
        )
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "failed"
        # One record: the run's cancel arm deferred to the reap that owns it.
        assert tombstones.call_count == 1
        assert tombstones.call_args.kwargs["cause"] == "reaped"
        assert info.done and info.reaped
        assert manager._running_count == 0
        assert manager._tasks == {}

    @pytest.mark.asyncio
    async def test_a_user_stop_parked_in_its_reset_at_shutdown_still_publishes_the_report(
        self,
    ) -> None:
        """The reap a dashboard Stop runs lives in the REQUEST task, not the reaper task.

        ``cancel_all()`` cancels ``_reaper_task``; a Stop's reap it never touched
        sat in its hanging reset through the drain, its report waiting on a gate
        nobody released, until the gateway's own shutdown budget hard-exited the
        process -- and the tombstone the run's arm wrote excluded the folder
        from orphan recovery. Every in-flight reap is tracked and cancelled by
        ``cancel_all`` so its cancellation arm finishes the record and releases
        the report inside the drain.
        """
        manager, info, _key = _overdue_run("shut4", pid=9595)
        info._session_sharing = False
        reset_started, _hang = _shutdown_mid_reset(manager)

        async def _stream(_info: SubagentInfo, _session_key: str) -> None:
            await reset_started.wait()
            raise AcpProcessDied("Runtime process died during prompt — killed (provider shutdown)")

        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch("kiro_crew.subagent._REPORT_DRAIN_TIMEOUT", 0.5),
            patch("kiro_crew.subagent.write_tombstone") as tombstones,
            patch("kiro_crew.subagent.clear_tombstone") as cleared,
            patch.object(manager, "_run_inner", AsyncMock(side_effect=_stream)),
            children,
            alive,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_group"),
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock()),
        ):
            run_task = asyncio.create_task(manager._run(info))
            manager._tasks["shut4"] = run_task
            await asyncio.sleep(0)

            # The dashboard Stop: ``cancel()`` awaits the reap inside the request's
            # own task; ``_reaper_task`` is not involved.
            stop = asyncio.create_task(manager.cancel("shut4"))
            await asyncio.wait_for(asyncio.gather(run_task, return_exceptions=True), timeout=5)
            assert info.done and not stop.done()
            assert _pending_reports_for(
                manager, "shut4"
            ), "no terminal report task exists while the stop sits inside its reset window"
            assert _done_events(manager) == []
            tombstones_before_shutdown = tombstones.call_count

            await asyncio.wait_for(manager.cancel_all(), timeout=5)

            done_events = _done_events(manager)
            if not stop.done():
                stop.cancel()
            with pytest.raises(asyncio.CancelledError):
                await stop
            await asyncio.wait_for(
                asyncio.gather(*manager._report_tasks, return_exceptions=True), timeout=5
            )

        assert (
            len(done_events) == 1
        ), f"expected one subagent_done event after cancel_all, saw {len(done_events)}"
        assert manager._on_done.await_count == 1
        # A user stop stays neutral and still names the kill the shutdown cut short.
        assert done_events[0]["outcome"] == "stopped"
        assert "kill failed: CancelledError: the stop was cancelled before its kill decided" in (
            done_events[0]["error"] or ""
        ), done_events[0]["error"]
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "failed"
        # Re-written with the failure, never cleared: the completion reached the parent.
        assert tombstones.call_count == tombstones_before_shutdown + 1
        assert tombstones.call_args.kwargs["cause"] == "user_stop"
        cleared.assert_not_called()
        assert info.done and info.reaped
        assert manager._reap_tasks == set()
        assert manager._report_owners == {}


class TestConcurrentStopsCoalesceIntoOneReap:
    """Two stop paths over one run are one reap: one kill, one suffix, one report.

    A dashboard Stop racing a deadline reap (or a parent-end cancel racing
    either) launched two reaps over the same run. Both retained handles, both
    reset, both killed; when the kill failed, both appended the ``; kill
    failed: …`` suffix to the persisted record while only the first published
    a report -- so the record on disk and the completion the parent received
    disagreed. The first caller runs the reap; a caller arriving while it is in
    flight joins it and returns once the record is final.
    """

    @pytest.mark.asyncio
    async def test_a_stop_racing_a_deadline_reap_joins_it(self) -> None:
        manager, info, _key = _overdue_run("race1", pid=7171)
        info._session_sharing = False
        _hanging_reset(manager)
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch("kiro_crew.subagent._RESET_TIMEOUT", 0.05),
            patch("kiro_crew.subagent.write_tombstone") as tombstones,
            children,
            alive,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group",
                MagicMock(side_effect=_REFUSAL),
            ) as group_kill,
        ):
            # The deadline reap starts first and parks in its reset.
            deadline = asyncio.create_task(manager._force_reap("race1", info, 7200.0))
            await asyncio.sleep(0)
            assert info._reap_started and not deadline.done()
            # The user presses Stop while it is in flight.
            stop = asyncio.create_task(manager.cancel("race1"))

            await asyncio.wait_for(asyncio.gather(deadline, stop), timeout=5)
            await asyncio.wait_for(
                asyncio.gather(*manager._report_tasks, return_exceptions=True), timeout=5
            )

        assert stop.result() is True
        # One kill decided once: the failure is on the record exactly once.
        suffixes = (info.error or "").count("kill failed:")
        assert suffixes == 1, f"the kill failure was recorded {suffixes} times: {info.error!r}"
        assert group_kill.call_count == 1
        # The record follows the FIRST stopper: the deadline, a failure.
        assert info.outcome == "failed"
        assert info.error.startswith("Reaped after 7200s")
        # One audit row, one tombstone, one published report.
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "failed"
        assert tombstones.call_count == 1
        done_events = _done_events(manager)
        assert len(done_events) == 1, f"expected one subagent_done event, saw {len(done_events)}"
        assert done_events[0]["error"] == info.error
        assert manager._on_done.await_count == 1
        assert manager._reaps_in_flight == {}
        assert manager._report_owners == {}

    @pytest.mark.asyncio
    async def test_a_joiner_returns_once_the_record_is_final_not_after_the_delivery(
        self,
    ) -> None:
        """The join waits for the record, not for the parent injection behind it.

        The first reap's last statement awaits its report's delivery -- an
        unbounded shield over an injection capped at ``_ON_DONE_TIMEOUT``
        (20 minutes). A second stop path that joined the reap -- another Stop
        request, or the reaper's own sweep calling ``_force_reap`` inline --
        stood behind that delivery, hanging the Stop and freezing every other
        run's deadline in the sweep, where before the coalescing the loser
        returned as soon as the finalize claim refused it. The join settles
        when the record is written, audited and the report released.
        """
        manager, info, _key = _overdue_run("race3", pid=7373)
        info._session_sharing = False
        _hanging_reset(manager)
        children, alive, start_id, sweep = _kill_path_stubs()
        delivering = asyncio.Event()
        delivered = asyncio.Event()

        async def _slow_delivery(report_task: Any) -> None:
            delivering.set()
            await delivered.wait()
            await report_task

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch("kiro_crew.subagent._RESET_TIMEOUT", 0.05),
            patch("kiro_crew.subagent.write_tombstone"),
            patch.object(manager, "_await_report", AsyncMock(side_effect=_slow_delivery)),
            children,
            alive,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group",
                MagicMock(side_effect=_REFUSAL),
            ),
        ):
            deadline = asyncio.create_task(manager._force_reap("race3", info, 7200.0))
            await asyncio.sleep(0)
            assert info._reap_started and not deadline.done()
            stop = asyncio.create_task(manager.cancel("race3"))
            # The first reap has written its record and is inside the delivery.
            await asyncio.wait_for(delivering.wait(), timeout=5)
            assert info.done and info.reaped and not deadline.done()

            try:
                await asyncio.wait_for(asyncio.shield(stop), timeout=1)
            except asyncio.TimeoutError:
                delivered.set()
                pytest.fail(
                    "the stop that joined the reap waited for the report's delivery, "
                    "not for the record"
                )
            assert stop.result() is True

            delivered.set()
            await asyncio.wait_for(deadline, timeout=5)
            await asyncio.wait_for(
                asyncio.gather(*manager._report_tasks, return_exceptions=True), timeout=5
            )

        assert (info.error or "").count("kill failed:") == 1
        assert manager._reaps_in_flight == {}

    @pytest.mark.asyncio
    async def test_a_reap_asked_for_again_after_it_finished_is_a_no_op(self) -> None:
        """Control: a late second caller finds the run reaped and touches nothing."""
        manager, info, _key = _overdue_run("race2", pid=7272)
        info._session_sharing = False
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
            ) as group_kill,
        ):
            await manager._force_reap("race2", info, 7200.0)
            await manager._force_reap("race2", info, 7200.0, reason="user_stop")

        assert group_kill.call_count == 1
        assert (info.error or "").count("kill failed:") == 1
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "failed"


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
    the reaper and the run's own teardown): ``_kill_orphan_pid`` used to return
    nothing and swallow every error, and its caller audited ``killed``
    regardless -- the same shape the two fixed paths had.
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


class TestTheOrphanKillLeavesTheLoopRunning:
    """The orphan kill is awaited through ``kill_pid_async``: its Windows arm runs off the loop.

    The reconciliation is a coroutine on the gateway's event loop, and the
    Windows kill is a ``taskkill`` spawn that waits up to five seconds for the
    target. Signalling it synchronously from the coroutine stalled the loop for
    that wait, once per live orphan, during startup.
    """

    @staticmethod
    async def _ticks_during(work: Any) -> int:
        """How many 10 ms ticks the loop served while *work* ran."""
        ticks = 0

        async def clock() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        ticking = asyncio.create_task(clock())
        try:
            await work
        finally:
            ticking.cancel()
        return ticks

    @pytest.mark.asyncio
    async def test_the_reconciliation_awaits_the_kill_off_the_loop(self, agent_root: Any) -> None:
        manager = _orphan_on_disk("orphan-awaited", pid=4245)

        async def slow_kill(pid: int, sig: int) -> bool:
            await asyncio.sleep(0.2)  # the taskkill wait, as the executor hop presents it
            return True

        with (
            patch.object(manager, "_is_pid_alive", return_value=True),
            patch.object(manager, "_is_orphan_process", return_value=True),
            patch.object(platform_compat, "kill_pid_async", side_effect=slow_kill) as kill,
            patch("kiro_crew.subagent.sel") as mock_sel,
        ):
            ticks = await self._ticks_during(manager._reconcile_orphans())

        kill.assert_awaited_once_with(4245, platform_compat.SIGKILL)
        assert ticks >= 5, (
            f"the loop served {ticks} ticks while the orphan kill waited: the reconciliation "
            "did not await the kill"
        )
        assert _audit(mock_sel, "orphan_reconcile_kill")["outcome"] == "killed"
        assert (agent_root / "orphan-awaited" / "tombstone.json").exists()

    @pytest.mark.asyncio
    async def test_a_slow_windows_kill_does_not_stall_the_loop(self) -> None:
        """The Windows arm's blocking ``taskkill`` wait runs on the subprocess executor."""

        def taskkill_wait(pid: int, sig: int) -> bool:
            time.sleep(0.3)  # taskkill waiting on the target, on whichever thread runs it
            return True

        with (
            patch.object(platform_compat, "IS_POSIX", False),
            patch.object(platform_compat, "kill_pid", side_effect=taskkill_wait) as kill,
        ):
            ticks = await self._ticks_during(SubagentManager._kill_orphan_pid(4246))

        kill.assert_called_once_with(4246, platform_compat.SIGKILL)
        assert ticks >= 5, (
            f"the loop served {ticks} ticks during a 300 ms taskkill wait: the kill ran on the "
            "loop thread"
        )

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


# ── The reap fences the key: a start racing it is held, refused, or named ──


class TestTheReapFencesTheKey:
    """The reap holds the key's ending fence from before its first read through the record and the audit.

    Twin of the cron reaper's fence (``test_cron_reaper.py``): a claim or a cold
    start under the key while the reap runs is HELD at the door of
    ``get_or_create`` and lands only once the run is recorded; a start already
    inside ``provider.start()`` when the fence went up -- nothing published for
    the snapshot to see -- is refused at registration, its provider hard-killed,
    its call allocating again after the lift, and the record NAMES it as a kill
    failure, never ``reaped`` over it. Without the fence that start registered
    after the passes and ran on under a ``reaped`` record, holding its turn
    permit, with nothing left to reclaim it: the run's own teardown is skipped
    once ``reaped`` is set and the idle sweep skips a held session.
    """

    @pytest.mark.asyncio
    async def test_the_fence_is_up_before_the_snapshot_and_down_only_after_the_record_and_audit(
        self,
    ) -> None:
        """Order and report, on a manager double: fence up before the first read, the start past its spawn door named, the fence down only after the record, the audit and the release."""
        manager, info, key = _overdue_run("fence1", pid=4242)
        events: list[str] = []

        @contextmanager
        def _fence(session_key: str) -> Iterator[None]:
            events.append(f"fence up {session_key}")
            try:
                yield
            finally:
                events.append(f"fence down {session_key}")

        async def _reset(session_key: str, **_: Any) -> bool:
            events.append("reset")
            return True

        real_retain = manager._retain_process_handles

        def _snapshot(agent_id: str, session_key: str) -> list[Any]:
            events.append("snapshot")
            return real_retain(agent_id, session_key)

        manager._sessions.ending_key = _fence
        manager._sessions.reset = AsyncMock(side_effect=_reset)
        manager._sessions._spawn_in_flight = MagicMock(
            side_effect=lambda session_key: events.append(f"spawn in flight? {session_key}")
            or "refused at registration, its provider hard-killed there"
        )
        manager._sessions.release = MagicMock(
            side_effect=lambda *_a, **_k: events.append("release")
        )
        children, _alive, _start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch.object(manager, "_retain_process_handles", side_effect=_snapshot),
            patch.object(
                manager, "_write_tombstone", side_effect=lambda *_a, **_k: events.append("record")
            ),
            children,
            sweep,
            # The reset stopped the process: gone by identity and by liveness,
            # its group empty (the autouse pin), so nothing survives to kill.
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=None),
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            patch("kiro_crew.platform_compat.kill_process_group") as group_kill,
        ):
            mock_sel().log_tool_invocation.side_effect = lambda **_: events.append("audit")
            await manager._force_reap("fence1", info, 7200.0)

        assert events == [
            f"fence up {key}",
            "snapshot",
            "reset",
            f"spawn in flight? {key}",
            "record",
            "audit",
            "release",
            f"fence down {key}",
        ], "the ending fence was not up from before the snapshot until after the record and audit"
        group_kill.assert_not_called()
        assert (
            _audit(mock_sel, "reaper_force_kill")["outcome"] == "failed"
        ), "the audit says reaped while a cold start under the key was past its spawn door"
        assert (
            "; kill failed: a cold start under the key was past its spawn door when the run was "
            "ended (fenced: refused at registration, its provider hard-killed there); not signalled"
        ) in info.error

    @pytest.mark.asyncio
    async def test_a_manager_without_the_fence_is_not_fenced_and_reports_nothing(self) -> None:
        """A double with neither ``ending_key`` nor a string ``_spawn_in_flight``: the passes are the whole answer."""
        manager, info, _key = _overdue_run("fence3", pid=4242)
        del manager._sessions.ending_key
        del manager._sessions._spawn_in_flight
        children, _alive, _start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            children,
            sweep,
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=None),
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
        ):
            await manager._force_reap("fence3", info, 7200.0)

        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "reaped"
        assert "kill failed" not in (info.error or "")

    @pytest.mark.asyncio
    async def test_through_the_real_manager_a_start_in_flight_is_named_refused_hard_killed_and_lands_after(
        self,
    ) -> None:
        """A cold start inside ``provider.start()`` under the run's key when the reap fires: named, refused at registration, hard-killed, and its call lands after the record."""
        inside_start = asyncio.Event()
        gate = asyncio.Event()
        started: list[Any] = []

        def _factory(
            session_key: Any = None, agent: Any = None, channel_id: Any = None, **_: Any
        ) -> Any:
            provider = AsyncMock()
            provider.memory_mode = "persistent"
            provider.is_process_alive = lambda: True
            provider.context_usage_pct = lambda: 0.0
            provider.context_window_tokens = lambda: 0
            provider.has_active_turn = lambda: False
            provider.runtime_info = lambda: (None, None)
            provider.shutdown = AsyncMock()

            async def _gated_start(*_args: Any, **_kwargs: Any) -> None:
                started.append(provider)
                inside_start.set()
                await gate.wait()

            provider.start = AsyncMock(side_effect=_gated_start)
            return provider

        mgr = SessionManager(KiroCrewConfig(), provider_factory=_factory)
        key = "subagent:fence2"
        cold_start = asyncio.create_task(mgr.get_or_create(key))
        await asyncio.wait_for(inside_start.wait(), timeout=2)
        assert not mgr.has_session(key), "nothing is published while start() runs"

        manager = SubagentManager(
            sessions=mgr,
            ctx_builder=_mock_ctx_builder(),
            on_done=AsyncMock(),
            on_event=AsyncMock(),
            is_yolo=lambda: True,
        )
        info = SubagentInfo(
            id="fence2",
            task="stuck task",
            parent_session_key="dashboard:test-slot",
            started=time.time() - 7200,
        )
        manager._agents["fence2"] = info
        manager._running_count = 1
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch.object(mgr, "_dispatch_hard_kill") as hard_kill,
            children,
            alive,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_group", return_value=True) as group_kill,
        ):
            assert manager._retain_process_handles("fence2", key) == []
            await manager._force_reap("fence2", info, 7200.0)
            assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "failed", (
                "the audit says the run was reaped while a cold start under the key was still "
                "inside provider.start()"
            )
            # The record is written and the fence has lifted; only now does the
            # start return: refused at registration, its provider hard-killed,
            # and the call allocates again -- the caller lands after the record.
            gate.set()
            provider, is_new, _ = await asyncio.wait_for(cold_start, timeout=5)

        group_kill.assert_not_called()  # nothing was published for the passes to kill
        assert (
            "; kill failed: a cold start under the key was past its spawn door when the run was "
            "ended"
        ) in (info.error or "")
        assert len(started) == 2, "the refused call did not allocate again after the lift"
        hard_kill.assert_called_once_with(started[0])
        assert is_new and provider is started[1] and mgr.has_session(key), (
            "the caller whose start the fence caught was dropped instead of landing after the "
            "record"
        )
        assert not mgr._has_allocation_reservation(key)
        mgr.release(key)
        await mgr.reset(key)
