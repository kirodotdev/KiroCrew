"""Ending a run ends every key it registered, under the ending fence.

A registration after the reset's pop is named instead of chased, every key the run
registered is ended, and a cold start still inside ``provider.start()`` when the run
ends is fenced and named.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import cron_reaper_helpers
import pytest
from cron_reaper_helpers import (
    _FENCE_SETTLE_CEILING_SECS,
    _START_ID,
    _audited_outcome,
    _held_or_landed,
    _kill_path_stubs,
    _live_task,
    _make_job,
    _mock_sessions,
    _overdue_reap_fixture,
    _parks_at_the_fence_door,
    _session_with_pid,
    _torn_down_by_the_run,
    _within_the_ceiling,
)

from kiro_crew import platform_compat
from kiro_crew.cron import _JOB_TIMEOUT_SECS, CronService, _RunClaim
from kiro_crew.cron_history import CronHistoryStore
from kiro_crew.testing.wait import async_wait_until, finished

#: Every cron reaper suite pins a healthy free-memory reading (see
#: ``ample_host_free_memory``): ``test_cron_reaper.py`` fires cron jobs through the
#: due-scan, which defers firings while the host's memory posture is critical.
pytestmark = pytest.mark.usefixtures("ample_host_free_memory")


# The shared fixtures, bound here by name so pytest registers them for this module.
_kill_seam = cron_reaper_helpers._kill_seam


#: ``_REAPER_RESET_TIMEOUT`` in a test whose reset COMPLETES. The bound only picks
#: a production branch -- the completed reset or "reset hung, attempting SIGKILL"
#: -- so in these tests it is a lost-run ceiling, never the race. The real reset of
#: a live session makes worker hops (the end-record unlink, the queue unlink, the
#: child probe), milliseconds on an idle host, and 0.2 s let a loop stall pick the
#: hung branch. It is the production default and a quarter of ``--timeout``.
_COMPLETED_RESET_CEILING_SECS = 30.0


def _assert_the_reset_completed(caplog: pytest.LogCaptureFixture, elapsed: float) -> None:
    """Fail by name when a reset outlived ``_COMPLETED_RESET_CEILING_SECS``.

    Production answers that by taking its hung branch, which the test would
    otherwise report only through a later, unrelated assertion. *elapsed* is how
    long the reap took, quoted in the failure.
    """
    hung = [
        r.getMessage()
        for r in caplog.records
        if r.name == "kiro_crew.cron" and "reset hung" in r.getMessage()
    ]
    assert hung == [], (
        f"the reset outlived the {_COMPLETED_RESET_CEILING_SECS:.0f}s lost-run ceiling "
        f"(_COMPLETED_RESET_CEILING_SECS); the reap took {elapsed:.2f}s: {hung}"
    )


async def _settle(predicate: Callable[[], bool], what: str) -> None:
    """Poll until ``predicate()`` holds; at the ceiling raise naming ``what`` and the time."""
    await async_wait_until(predicate, timeout=_FENCE_SETTLE_CEILING_SECS, describe=lambda: what)


class TestARegistrationAfterThePopIsNamedNotChased:
    """A session registered under the key AFTER the reset's pop is a named kill failure; there is no second pass.

    The snapshot and the pop capture together name every session the key held up
    to the pop, and the fence is why nothing lands under it afterwards: every
    door into the live map either meets the fence -- ``get_or_create`` at its
    front door, after its busy-turn wait and at registration, cold start and
    warm-pool claim alike -- or never publishes under a cron key
    (``open_task_session`` publishes ``taskrunner:`` keys and refuses a key being
    ended; the background session publishes its own key). So a reset-and-kill
    pass is run ONCE per key, and the post-pass read is a net that names, not a
    chase: a session under the key the pass did not handle -- a manager without
    the fence, or a door this reasoning missed -- is reported as a kill failure,
    audited ``failed``, never reset, never signalled, and a session the pass
    already answered is never reported.
    """

    @staticmethod
    def _late_registering_reset(svc: CronService, key: str, late_pids: list[int]) -> Any:
        """A reset that pops the session under ``key`` and then cold-starts the next of ``late_pids`` under it.

        The registration lands AFTER the pop -- a door without the fence -- so
        neither the snapshot nor the pop capture of the pass names it. Completes
        (``True``) every time.
        """
        remaining = list(late_pids)

        async def _reset(session_key: str, **kwargs: Any) -> bool:
            assert session_key == key
            session = svc._sessions._sessions.pop(key, None)
            scope = kwargs.get("scope")
            if session is not None and scope is not None:
                scope.note_pop(key, session)
            if remaining:
                _session_with_pid(svc, key, remaining.pop(0))
            return True

        return _reset

    @pytest.mark.asyncio
    async def test_a_session_registered_after_the_pop_is_a_failed_kill_not_reaped_and_not_chased(
        self, tmp_path: object
    ) -> None:
        svc, job, claim = _overdue_reap_fixture(tmp_path, "chase1")
        _session_with_pid(svc, "cron:chase1", 8787)
        svc._sessions.reset = AsyncMock(
            side_effect=self._late_registering_reset(svc, "cron:chase1", [9797])
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
            await svc._force_reap("chase1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert [called.args[0] for called in group_kill.call_args_list] == [
            8787
        ], "a session that landed under the key past the fence was reset and signalled: a second pass ran"
        assert svc._sessions.reset.await_count == 1, "the key was reset more than once"
        assert (
            _audited_outcome(mock_sel) == "failed"
        ), "the audit says the run was reaped while a session registered after the pop still runs"
        assert (
            "; kill failed: a session registered under the key after the reset's pop (pid 9797); "
            "not reset, not signalled"
        ) in (job.last_error or "")
        assert 9797 in {s.provider._client._pid for s in svc._sessions._sessions.values()}

    @pytest.mark.asyncio
    async def test_a_session_the_pass_already_answered_is_not_reported(
        self, tmp_path: object
    ) -> None:
        """The run's own torn-down session still sits in the table after the pass: handled, so one reset and no failure."""
        svc, job, claim = _overdue_reap_fixture(tmp_path, "chase3")
        _session_with_pid(svc, "cron:chase3", 8888)
        _torn_down_by_the_run(svc, "cron:chase3")
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
            await svc._force_reap("chase3", _JOB_TIMEOUT_SECS + 60, claim=claim)

        group_kill.assert_called_once_with(8888, platform_compat.SIGKILL)
        assert svc._sessions.reset.await_count == 1
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_through_the_real_manager_a_cold_start_after_the_pop_is_held_by_the_fence(
        self, tmp_path: object, caplog: pytest.LogCaptureFixture
    ) -> None:
        """End to end: the first reset completes and the late cold start after its pop is held by the fence -- nothing lands, nothing is dropped.

        Through the real manager the fence is why one pass is enough: the
        completion's ``get_or_create`` after the pop meets the key's ending fence
        at the door and waits there, so nothing lands after the pop for a
        post-pass read to name; it lands once the fence lifts, under a key whose
        run is recorded.
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
        first_pid = 2**22 + 9090

        provider, _, _ = await mgr.get_or_create("cron:chase4")
        mgr.release("cron:chase4")
        client = MagicMock()
        client._pid = first_pid
        client._child_pids = {}
        client._start_time = _START_ID
        provider._client = client
        real_reset = mgr.reset
        parked, door = _parks_at_the_fence_door(mgr)
        resets = 0
        racing: list[asyncio.Task[Any]] = []
        held_after_pop: list[str] = []

        async def _reset_then_late_cold_start(key: str, **kwargs: Any) -> bool:
            nonlocal resets
            resets += 1
            result = await real_reset(key, **kwargs)
            if resets == 1:
                # The late completion's cold start after the pop: held at the door.
                racing.append(asyncio.create_task(mgr.get_or_create(key)))
                held_after_pop.append(await _held_or_landed(parked, racing[0]))
            return result

        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = mgr
        job = _make_job("chase4")
        svc._jobs = [job]
        claim = svc._claims["chase4"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60, task=_live_task()
        )
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch.object(mgr, "reset", side_effect=_reset_then_late_cold_start),
            door,
            patch("kiro_crew.cron._REAPER_RESET_TIMEOUT", _COMPLETED_RESET_CEILING_SECS),
            children,
            start_id,
            sweep,
            # The first process is gone once its reset completed: no survivor.
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            patch("kiro_crew.platform_compat.kill_process_group", return_value=True) as group_kill,
        ):
            reap_started = asyncio.get_running_loop().time()
            await svc._force_reap("chase4", _JOB_TIMEOUT_SECS + 60, claim=claim)
            # flake-ok: the reap's duration is only quoted in the failure message, never asserted
            reap_took = asyncio.get_running_loop().time() - reap_started
        _assert_the_reset_completed(caplog, reap_took)

        assert held_after_pop == ["held"], (
            "the late cold start after the pop was answered under the fenced key: "
            f"{racing[0].exception() if racing and racing[0].done() and racing[0].exception() else held_after_pop}"
        )
        assert resets == 1, "the key was reset more than once although nothing landed after the pop"
        group_kill.assert_not_called()
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")
        # The held call lands after the record: the completion is delivered, not dropped.
        _, is_new, _ = await _within_the_ceiling(racing[0], "the held cold start landing")
        assert is_new and mgr.has_session("cron:chase4")
        mgr.release("cron:chase4")
        await real_reset("cron:chase4")


class TestEveryKeyOfTheRunIsEnded:
    """A reap or cancel ends EVERY key the run registered, not the newest one.

    A sequential job registers one key per agent inside one run, and an earlier
    agent's session is kept alive -- its reset deferred -- while its sub-agents
    are pending and a later agent runs. A reap that ended only the newest key
    reset the hung later agent, recorded ``reaped``, and left the earlier agent's
    session and its sub-agents running behind that record. Each registered key
    is attributed to the run that registered it (``register_active_session_key``
    reads the job's stored claim); the reap and cancel end every key of the run
    they took (``_run_session_keys``, newest first), each fenced through the
    record, and leave an older run's key -- a finished run's session retained
    for its pending sub-agents -- alone. A key the run registers while its
    others are being ended (its task is cancelled only after the passes) is
    ended by a bounded second round; one that lands after the last round is a
    named kill failure, so the audit says ``failed``.
    """

    @staticmethod
    def _registers_next_key_on_reset(
        svc: CronService, job_id: str, next_keys: list[tuple[str, int]]
    ) -> Any:
        """A reset that pops the session under the key and then registers the run's next agent key with a live session.

        The shape of a sequential run whose hung agent's stream ends when its
        session is reset: the run's task, not yet cancelled, moves on to the next
        agent and registers that key while the reap is still ending the others.
        """
        remaining = list(next_keys)

        async def _reset(session_key: str, **kwargs: Any) -> bool:
            session = svc._sessions._sessions.pop(session_key, None)
            scope = kwargs.get("scope")
            if session is not None and scope is not None:
                scope.note_pop(session_key, session)
            if remaining:
                key, pid = remaining.pop(0)
                svc.register_active_session_key(job_id, key)
                _session_with_pid(svc, key, pid)
            return True

        return _reset

    @pytest.mark.asyncio
    async def test_a_sequential_run_s_deferred_earlier_agent_is_ended_beside_the_hung_newest(
        self, tmp_path: object
    ) -> None:
        svc, job, claim = _overdue_reap_fixture(tmp_path, "seq1")
        svc.register_active_session_key("seq1", "cron:seq1:agentA")
        _session_with_pid(svc, "cron:seq1:agentA", 7001)
        svc.register_active_session_key("seq1", "cron:seq1:agentB")
        _session_with_pid(svc, "cron:seq1:agentB", 7002)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_group", return_value=True) as group_kill,
        ):
            await svc._force_reap("seq1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert [called.args[0] for called in group_kill.call_args_list] == [7002, 7001], (
            "the reap ended only the newest agent's key and recorded reaped while the earlier "
            "agent's session, kept alive for its pending sub-agents, still runs"
        )
        assert [called.args[0] for called in svc._sessions.reset.await_args_list] == [
            "cron:seq1:agentB",
            "cron:seq1:agentA",
        ]
        assert _audited_outcome(mock_sel) == "reaped"
        audit = mock_sel().log_tool_invocation.call_args.kwargs
        assert audit["session_key"] == "cron:seq1:agentB"
        assert audit["metadata"]["session_keys"] == ["cron:seq1:agentB", "cron:seq1:agentA"]
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_an_older_run_s_key_kept_alive_for_its_subagents_is_not_ended(
        self, tmp_path: object
    ) -> None:
        """The run's reap is not the job's: a finished run's session lives on for its pending sub-agents."""
        svc, job, claim = _overdue_reap_fixture(tmp_path, "older1")
        finished = _RunClaim(trigger="scheduled", claimed_at=time.time() - 3600)
        svc._claims["older1"] = finished
        svc.register_active_session_key("older1", "cron:older1:finished")
        _session_with_pid(svc, "cron:older1:finished", 7010)
        svc._claims["older1"] = claim
        svc.register_active_session_key("older1", "cron:older1:current")
        _session_with_pid(svc, "cron:older1:current", 7011)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_group", return_value=True) as group_kill,
        ):
            await svc._force_reap("older1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert [called.args[0] for called in group_kill.call_args_list] == [7011]
        svc._sessions.reset.assert_awaited_once()
        assert svc._sessions.reset.await_args.args[0] == "cron:older1:current"
        assert (
            "cron:older1:finished" in svc._sessions._sessions
        ), "the reap of the current run ended the finished run's session"
        assert _audited_outcome(mock_sel) == "reaped"
        assert svc.active_session_keys() == frozenset(
            {"cron:older1:finished"}
        ), "the key the reap answered was not retired, or the finished run's key was"

    @pytest.mark.asyncio
    async def test_a_key_the_run_registers_while_its_others_are_ended_is_ended_by_a_second_round(
        self, tmp_path: object
    ) -> None:
        svc, job, claim = _overdue_reap_fixture(tmp_path, "late1")
        svc.register_active_session_key("late1", "cron:late1:agentA")
        _session_with_pid(svc, "cron:late1:agentA", 7020)
        svc._sessions.reset = AsyncMock(
            side_effect=self._registers_next_key_on_reset(
                svc, "late1", [("cron:late1:agentB", 7021)]
            )
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

        assert [called.args[0] for called in group_kill.call_args_list] == [
            7020,
            7021,
        ], "the key the run registered while its first key was being ended was not ended"
        assert _audited_outcome(mock_sel) == "reaped"
        audit = mock_sel().log_tool_invocation.call_args.kwargs
        assert audit["metadata"]["session_keys"] == ["cron:late1:agentA", "cron:late1:agentB"]
        assert svc._sessions._sessions == {}
        assert svc.active_session_keys() == frozenset()

    @pytest.mark.asyncio
    async def test_a_key_registered_after_the_last_round_is_a_failed_kill_not_reaped(
        self, tmp_path: object
    ) -> None:
        svc, job, claim = _overdue_reap_fixture(tmp_path, "late2")
        svc.register_active_session_key("late2", "cron:late2:agentA")
        _session_with_pid(svc, "cron:late2:agentA", 7030)
        svc._sessions.reset = AsyncMock(
            side_effect=self._registers_next_key_on_reset(
                svc, "late2", [("cron:late2:agentB", 7031), ("cron:late2:agentC", 7032)]
            )
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
            await svc._force_reap("late2", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert [called.args[0] for called in group_kill.call_args_list] == [7030, 7031]
        assert (
            _audited_outcome(mock_sel) == "failed"
        ), "the audit says reaped while a key the run registered after the last round still runs"
        assert (
            "; kill failed: the run registered cron:late2:agentC after 2 rounds of resets; "
            "not reset, not signalled"
        ) in (job.last_error or "")
        assert "cron:late2:agentC" in svc._sessions._sessions
        assert svc.active_session_keys() == frozenset(
            {"cron:late2:agentC"}
        ), "a key the reap did not end must stay registered as live"

    @pytest.mark.asyncio
    async def test_the_run_s_keys_are_ended_together_not_one_after_another(
        self, tmp_path: object
    ) -> None:
        """The fences are up for one key's bound, not the sum of every key's.

        Each key's ending is bounded on its own (the reset timeout, the verified
        kill); ended one after another, a run of many agent keys holds its fences
        for the SUM of those bounds -- past ``ENDING_FENCE_WAIT_SECS``, so a
        completion held at the door is refused for a teardown that was merely
        long. Pinned by a handshake: the newest key's reset (ended first in a
        serial order) waits until the OLDER key's reset has started; a serial
        ending never starts it and hangs, a concurrent one completes.
        """
        svc, job, claim = _overdue_reap_fixture(tmp_path, "par1")
        svc.register_active_session_key("par1", "cron:par1:agentA")
        _session_with_pid(svc, "cron:par1:agentA", 7050)
        svc.register_active_session_key("par1", "cron:par1:agentB")
        _session_with_pid(svc, "cron:par1:agentB", 7051)
        older_started = asyncio.Event()

        async def _reset(session_key: str, **kwargs: Any) -> bool:
            if session_key == "cron:par1:agentA":
                older_started.set()
            else:
                # The newest key's reset outlasts the older key's start.
                # flake-ok: the newest key's fake reset parks until the older key's reset starts; the reap's 5 s wait_for bounds it
                await older_started.wait()
            session = svc._sessions._sessions.pop(session_key, None)
            scope = kwargs.get("scope")
            if session is not None and scope is not None:
                scope.note_pop(session_key, session)
            return True

        svc._sessions.reset = AsyncMock(side_effect=_reset)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_group", return_value=True) as group_kill,
        ):
            # The 5 s bound only names a serial reap: the concurrent resets finish
            # in a few loop steps, while ending the keys one after another keeps the
            # fences up for the sum of every key's bound.
            await finished(
                svc._force_reap("par1", _JOB_TIMEOUT_SECS + 60, claim=claim),
                "the run's keys ending concurrently (the older key's reset starting while "
                "the newest key's is in flight)",
                timeout=5,
            )

        assert sorted(called.args[0] for called in group_kill.call_args_list) == [7050, 7051]
        assert _audited_outcome(mock_sel) == "reaped"
        audit = mock_sel().log_tool_invocation.call_args.kwargs
        assert audit["metadata"]["session_keys"] == ["cron:par1:agentB", "cron:par1:agentA"]

    @pytest.mark.asyncio
    async def test_a_key_whose_kill_failed_stays_registered(self, tmp_path: object) -> None:
        """Only a key every session of which was answered is retired from the registry."""
        svc, job, claim = _overdue_reap_fixture(tmp_path, "keep1")
        svc.register_active_session_key("keep1", "cron:keep1:agentA")
        _session_with_pid(svc, "cron:keep1:agentA", 7040)
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
                    "kill_process_group: refusing broadcast/self process group 7040"
                ),
            ),
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock()),
        ):
            await svc._force_reap("keep1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert _audited_outcome(mock_sel) == "failed"
        assert svc.active_session_keys() == frozenset({"cron:keep1:agentA"})
        audit = mock_sel().log_tool_invocation.call_args.kwargs
        assert audit["metadata"]["session_keys"] == [], (
            "the audit lists a key as ended whose kill was refused and which is still registered: "
            f"{audit['metadata']['session_keys']}"
        )

    @pytest.mark.asyncio
    async def test_a_refused_key_is_not_listed_as_ended_beside_a_key_that_was(
        self, tmp_path: object
    ) -> None:
        """Two keys, one kill refused: ``session_keys`` names the ended one only, the refused one stays registered and is not chased as a late registration."""
        svc, job, claim = _overdue_reap_fixture(tmp_path, "mixed1")
        svc.register_active_session_key("mixed1", "cron:mixed1:agentA")
        _session_with_pid(svc, "cron:mixed1:agentA", 7051)
        svc.register_active_session_key("mixed1", "cron:mixed1:agentB")
        _session_with_pid(svc, "cron:mixed1:agentB", 7052)
        children, start_id, sweep = _kill_path_stubs()

        def _refuse_agent_a(pgid: int, sig: int) -> bool:
            if pgid == 7051:
                raise ValueError("kill_process_group: refusing broadcast/self process group 7051")
            return True

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.kill_process_group", side_effect=_refuse_agent_a),
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock()),
        ):
            await svc._force_reap("mixed1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert _audited_outcome(mock_sel) == "failed"
        audit = mock_sel().log_tool_invocation.call_args.kwargs
        assert audit["metadata"]["session_keys"] == ["cron:mixed1:agentB"], (
            "the audit's ended keys do not match what was ended: "
            f"{audit['metadata']['session_keys']}"
        )
        assert svc.active_session_keys() == frozenset({"cron:mixed1:agentA"})
        assert [called.args[0] for called in svc._sessions.reset.await_args_list] == [
            "cron:mixed1:agentB",
            "cron:mixed1:agentA",
        ], "the refused key was ended a second time as a late registration"
        assert "registered cron:mixed1:agentA after" not in (job.last_error or ""), (
            "the still-registered refused key was reported as a registration the run made "
            f"while it was being ended: {job.last_error}"
        )


class TestAColdStartInFlightWhenTheRunEndsIsFenced:
    """The case no pass can see: a cold start inside ``provider.start()`` when the run is ended.

    ``get_or_create`` takes its allocation reservation, then awaits
    ``provider.start()`` BEFORE publishing into the map. A reap or cancel that
    runs then finds nothing under the key -- no snapshot, no pop, no post-pass
    read can name it -- and without the fence recorded ``reaped`` while that
    provider published afterwards, its process and the completion's injection
    running on. ``_end_run_processes`` now raises the key's ending fence
    (``SessionManager.ending_key``) before its first snapshot: the in-flight
    reservation is invalidated, so the registration is refused when the start
    returns (fence up or lifted) and the started provider is hard-killed by the
    closing manager's own path; the passes report the allocation still in flight
    as a named kill failure, so the audit says ``failed``. The completion's call
    is not dropped: refused there, it waits for the lift and allocates again, a
    new life under the recorded key.
    """

    @pytest.mark.asyncio
    async def test_through_the_real_manager_a_start_in_flight_is_named_refused_hard_killed_and_lands_after(
        self, tmp_path: object
    ) -> None:
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager

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
            provider.runtime_abort_target = lambda: None
            provider.shutdown = AsyncMock()

            async def _gated_start(*_args: Any, **_kwargs: Any) -> None:
                started.append(provider)
                inside_start.set()
                # flake-ok: the fake start parks until the test sets the gate, then the cold start is awaited under the lost-run ceiling
                await gate.wait()

            provider.start = AsyncMock(side_effect=_gated_start)
            return provider

        mgr = SessionManager(KiroCrewConfig(), provider_factory=_factory)
        cold_start = asyncio.create_task(mgr.get_or_create("cron:fence1"))
        await _settle(inside_start.is_set, "the cold start reached the provider start")
        assert not mgr.has_session("cron:fence1"), "nothing is published while start() runs"

        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = mgr
        job = _make_job("fence1")
        svc._jobs = [job]
        claim = svc._claims["fence1"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60, task=_live_task()
        )
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch.object(mgr, "_dispatch_hard_kill") as hard_kill,
            patch("kiro_crew.cron._REAPER_RESET_TIMEOUT", 0.2),
            children,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            patch("kiro_crew.platform_compat.kill_process_group", return_value=True) as group_kill,
        ):
            assert svc._session_process_handles("cron:fence1") == []
            await svc._force_reap("fence1", _JOB_TIMEOUT_SECS + 60, claim=claim)
            assert _audited_outcome(mock_sel) == "failed", (
                "the audit says the run was reaped while a cold start under the key was still "
                "inside provider.start()"
            )
            # The record is written and the fence has lifted; only now does the
            # start return: refused at registration, its provider hard-killed,
            # and the call allocates again -- the completion lands after the record.
            gate.set()
            provider, is_new, _ = await _within_the_ceiling(cold_start, "the refused cold start")

        group_kill.assert_not_called()  # nothing was published for the passes to kill
        assert (
            "kill failed: a cold start under the key was past its spawn door when the run was ended"
            in (job.last_error or "")
        )
        assert len(started) == 2, "the refused call did not allocate again after the lift"
        hard_kill.assert_called_once_with(started[0])
        assert is_new and provider is started[1] and mgr.has_session("cron:fence1"), (
            "the completion whose start the fence caught was dropped instead of landing after "
            "the record"
        )
        assert not mgr._has_allocation_reservation("cron:fence1")
        mgr.release("cron:fence1")
        await mgr.reset("cron:fence1")

    @pytest.mark.asyncio
    async def test_through_the_real_manager_a_start_refused_during_the_passes_is_still_named(
        self, tmp_path: object
    ) -> None:
        """The start returns after the pass, before the post-pass read: refused, hard-killed by dispatch, its reservation gone -- and still named.

        Without the receipt the reservation cleanup erased every trace of that
        start before the post-pass read, so the record said ``reaped`` on the
        strength of a hard kill dispatched off the loop whose outcome nothing
        read back -- a kill the dispatch could not land left the process alive
        behind that record. The receipt survives until the fence lifts, so the
        audit says ``failed`` and the record names the refusal.
        """
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager

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
            provider.runtime_abort_target = lambda: None
            provider.shutdown = AsyncMock()

            async def _gated_start(*_args: Any, **_kwargs: Any) -> None:
                started.append(provider)
                inside_start.set()
                if len(started) == 1:
                    # flake-ok: the fake start parks until the test sets the gate, then the cold start is awaited under the lost-run ceiling
                    await gate.wait()

            provider.start = AsyncMock(side_effect=_gated_start)
            return provider

        mgr = SessionManager(KiroCrewConfig(), provider_factory=_factory)
        cold_start = asyncio.create_task(mgr.get_or_create("cron:fence7"))
        await _settle(inside_start.is_set, "the cold start reached the provider start")
        refused_during_passes: list[bool] = []
        settle_failures: list[str] = []

        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = mgr
        job = _make_job("fence7")
        svc._jobs = [job]
        claim = svc._claims["fence7"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60, task=_live_task()
        )
        children, start_id, sweep = _kill_path_stubs()
        real_pass = svc._reset_and_kill_once

        async def _pass_then_let_the_start_return(
            session_key: str, pairs: Any, **kwargs: Any
        ) -> Any:
            result = await real_pass(session_key, pairs, **kwargs)
            # The pass is over and the post-pass read not yet run: the start
            # returns now, fence up -- refused at registration, its provider
            # hard-killed, its reservation removed. Waited for OUTSIDE the
            # reset's own bound, so no reset timeout can cut it short.
            gate.set()
            try:
                await _settle(
                    lambda: not mgr._has_allocation_reservation(session_key),
                    "the start released after the pass was not refused with its reservation "
                    "removed",
                )
            except AssertionError as exc:
                # Recorded, not raised: the test reports it by name below, ahead
                # of whatever the reap does with an exception from its pass.
                settle_failures.append(str(exc))
            refused_during_passes.append(not mgr._has_allocation_reservation(session_key))
            return result

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch.object(mgr, "_dispatch_hard_kill") as hard_kill,
            patch.object(svc, "_reset_and_kill_once", _pass_then_let_the_start_return),
            patch("kiro_crew.cron._REAPER_RESET_TIMEOUT", 0.2),
            children,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            patch("kiro_crew.platform_compat.kill_process_group", return_value=True) as group_kill,
        ):
            await svc._force_reap("fence7", _JOB_TIMEOUT_SECS + 60, claim=claim)
            assert settle_failures == [], settle_failures
            assert refused_during_passes == [True], (
                "the test did not get the start refused and its reservation removed before the "
                "post-pass read"
            )
            assert _audited_outcome(mock_sel) == "failed", (
                "the audit says the run was reaped while a cold start under the key had just "
                "been refused at registration with its hard kill unconfirmed"
            )
            provider, is_new, _ = await _within_the_ceiling(cold_start, "the refused cold start")

        group_kill.assert_not_called()
        assert (
            "kill failed: a cold start under the key was past its spawn door when the run was "
            "ended (fenced: 1 refused at registration during the ending, the provider "
            "hard-killed there by the allocation path -- an outcome this record does not "
            "confirm); not signalled"
        ) in (job.last_error or "")
        hard_kill.assert_called_once_with(started[0])
        assert len(started) == 2 and is_new and provider is started[1]
        assert mgr.has_session("cron:fence7")
        # The receipt went with the fence: the recorded key carries nothing forward.
        assert mgr._spawn_in_flight("cron:fence7") is None
        mgr.release("cron:fence7")
        await mgr.reset("cron:fence7")

    @pytest.mark.asyncio
    async def test_through_the_real_manager_a_caller_held_at_the_door_lands_only_once_the_run_is_recorded(
        self, tmp_path: object, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The fence outlives the passes: a completion held at the door wakes to a RECORDED key.

        The run's terminal record -- the locked store merge, the history row --
        and its audit are written while the key is still fenced; only then does
        the fence lift and the held caller land. A fence that lifted when the
        passes ended let the caller allocate against a key that was neither
        being ended nor recorded: runtime and durable state disagreeing for the
        span of the write.
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
        provider, _, _ = await mgr.get_or_create("cron:fence5")
        mgr.release("cron:fence5")
        client = MagicMock()
        client._pid = 2**22 + 9191
        client._child_pids = {}
        client._start_time = _START_ID
        provider._client = client
        real_reset = mgr.reset
        parked, door = _parks_at_the_fence_door(mgr)
        events: list[str] = []
        racing: list[asyncio.Task[Any]] = []

        async def _land() -> tuple[Any, bool, bool]:
            result = await mgr.get_or_create("cron:fence5")
            events.append("landed")
            return result

        async def _reset_then_race(key: str, **kwargs: Any) -> bool:
            result = await real_reset(key, **kwargs)
            if not racing:
                # The completion's cold start after the pop: held at the door.
                racing.append(asyncio.create_task(_land()))
                events.append(await _held_or_landed(parked, racing[0]))
            return result

        boundary = mgr._allocation_boundary()
        real_end_ending = boundary.end_ending

        def _end_ending(key: str) -> None:
            events.append("fence down")
            real_end_ending(key)

        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = mgr
        job = _make_job("fence5")
        svc._jobs = [job]
        claim = svc._claims["fence5"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60, task=_live_task()
        )
        children, start_id, sweep = _kill_path_stubs()
        real_append = svc._history.append

        async def _append(record: Any) -> None:
            events.append("record")
            await real_append(record)

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch.object(svc._history, "append", AsyncMock(side_effect=_append)),
            patch.object(boundary, "end_ending", side_effect=_end_ending),
            patch.object(mgr, "reset", side_effect=_reset_then_race),
            door,
            patch("kiro_crew.cron._REAPER_RESET_TIMEOUT", _COMPLETED_RESET_CEILING_SECS),
            children,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            patch("kiro_crew.platform_compat.kill_process_group", return_value=True) as group_kill,
        ):
            mock_sel().log_tool_invocation.side_effect = lambda **_: events.append("audit")
            reap_started = asyncio.get_running_loop().time()
            await svc._force_reap("fence5", _JOB_TIMEOUT_SECS + 60, claim=claim)
            # flake-ok: the reap's duration is only quoted in the failure message, never asserted
            reap_took = asyncio.get_running_loop().time() - reap_started
            _assert_the_reset_completed(caplog, reap_took)
            _, is_new, _ = await _within_the_ceiling(racing[0], "the held caller landing")

        assert events == ["held", "record", "audit", "fence down", "landed"], (
            "the caller held at the door was let in against a key that was neither being ended "
            f"nor recorded: {events}"
        )
        group_kill.assert_not_called()
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")
        assert is_new and mgr.has_session("cron:fence5")
        mgr.release("cron:fence5")
        await real_reset("cron:fence5")

    @pytest.mark.asyncio
    async def test_through_the_real_manager_a_claim_waiting_on_the_busy_parent_is_not_named_and_lands_after_the_record(
        self, tmp_path: object, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A reservation is not a process: a completion's claim blocked on the busy parent's turn is held, not named.

        The sub-agent completion's ``get_or_create`` of the parent key has taken
        its reservation and waits on the live session's turn semaphore -- it
        started no provider, and the passes tore the run's process down. Naming
        that reservation as a start refused at registration recorded a kill
        failure that never happened: ``failed`` over a clean reap. Only a
        reservation past its spawn door is named; the waiter is woken by the
        reset, meets the fence at the door and lands once the run is recorded.
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
        # The run's own turn: the permit stays HELD -- the busy parent.
        provider, _, _ = await mgr.get_or_create("cron:fence6")
        client = MagicMock()
        client._pid = 2**22 + 9292
        client._child_pids = {}
        client._start_time = _START_ID
        provider._client = client

        at_the_turn = asyncio.Event()
        real_reacquire = mgr._reacquire_and_validate

        async def _reacquire(*args: Any, **kwargs: Any) -> bool:
            at_the_turn.set()  # past the worker hop, about to park on the held permit
            return await real_reacquire(*args, **kwargs)

        with patch.object(mgr, "_reacquire_and_validate", _reacquire):
            waiter = asyncio.create_task(mgr.get_or_create("cron:fence6"))
            await _settle(at_the_turn.is_set, "the claim did not reach the busy parent's turn")
        assert not waiter.done(), "the claim did not wait on the busy parent's turn"
        assert mgr._has_allocation_reservation(
            "cron:fence6"
        ), "the waiting claim holds a reservation"
        assert not mgr._spawn_in_flight("cron:fence6"), "a claim waiting on the turn is not a start"

        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = mgr
        job = _make_job("fence6")
        svc._jobs = [job]
        claim = svc._claims["fence6"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60, task=_live_task()
        )
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.cron._REAPER_RESET_TIMEOUT", _COMPLETED_RESET_CEILING_SECS),
            children,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            patch("kiro_crew.platform_compat.kill_process_group", return_value=True) as group_kill,
        ):
            reap_started = asyncio.get_running_loop().time()
            await svc._force_reap("fence6", _JOB_TIMEOUT_SECS + 60, claim=claim)
            # flake-ok: the reap's duration is only quoted in the failure message, never asserted
            reap_took = asyncio.get_running_loop().time() - reap_started
            _assert_the_reset_completed(caplog, reap_took)
            assert _audited_outcome(mock_sel) == "reaped", (
                "the audit says the run's kill failed over a claim that started nothing: "
                f"{job.last_error!r}"
            )
            # Woken by the reset, held at the door, landed after the record.
            _, is_new, _ = await _within_the_ceiling(waiter, "the woken claim landing")

        group_kill.assert_not_called()
        assert "kill failed" not in (job.last_error or "")
        assert is_new and mgr.has_session("cron:fence6"), "the waiting claim was dropped"
        assert not mgr._has_allocation_reservation("cron:fence6")
        await mgr.reset("cron:fence6")

    @pytest.mark.asyncio
    async def test_the_fence_is_raised_before_the_snapshot_and_lifted_only_after_the_record_and_audit(
        self, tmp_path: object
    ) -> None:
        """Order and report, on a manager double: fence up before the first read, the start past its spawn door named, the fence down only after the record and the audit."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = _mock_sessions()
        events: list[str] = []

        @contextmanager
        def _fence(key: str) -> Iterator[None]:
            events.append(f"fence up {key}")
            try:
                yield
            finally:
                events.append(f"fence down {key}")

        async def _reset(session_key: str, **kwargs: Any) -> bool:
            events.append("reset")
            return False

        real_append = svc._history.append

        async def _append(record: Any) -> None:
            events.append("record")
            await real_append(record)

        svc._sessions.ending_key = _fence
        svc._sessions.reset = AsyncMock(side_effect=_reset)
        svc._sessions._spawn_in_flight = MagicMock(
            side_effect=lambda key: events.append(f"spawn in flight? {key}")
            or "refused at registration, its provider hard-killed there"
        )
        job = _make_job("fence2")
        svc._jobs = [job]
        claim = svc._claims["fence2"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60, task=_live_task()
        )

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch.object(svc._history, "append", AsyncMock(side_effect=_append)),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
        ):
            mock_sel().log_tool_invocation.side_effect = lambda **_: events.append("audit")
            await svc._force_reap("fence2", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert events == [
            "fence up cron:fence2",
            "reset",
            "spawn in flight? cron:fence2",
            "record",
            "audit",
            "fence down cron:fence2",
        ], "the ending fence lifted before the run's terminal record and audit were written"
        tree_kill.assert_not_awaited()
        assert _audited_outcome(mock_sel) == "failed"
        assert (
            "kill failed: a cold start under the key was past its spawn door when the run was ended "
            "(fenced: refused at registration, its provider hard-killed there); not signalled"
        ) in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_manager_without_the_fence_is_not_fenced_and_reports_nothing(
        self, tmp_path: object
    ) -> None:
        """A double with neither ``ending_key`` nor a boolean ``_spawn_in_flight``: the passes are the whole answer."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = _mock_sessions()
        del svc._sessions.ending_key
        del svc._sessions._spawn_in_flight
        svc._sessions.reset = AsyncMock(return_value=False)
        job = _make_job("fence3")
        svc._jobs = [job]
        claim = svc._claims["fence3"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60, task=_live_task()
        )

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
        ):
            await svc._force_reap("fence3", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_without_the_fence_a_claim_after_the_post_pass_read_is_recorded_reaped_over(
        self, tmp_path: object
    ) -> None:
        """Why the fence, given the pre-reset handles, the pop capture and the post-pass read: the interval they cannot see.

        The three name every process under the key up to the post-pass read.
        A completion's claim that begins AFTER that read -- while the terminal
        record is being written, an interval of awaits -- is outside all three:
        no snapshot, no pop and no spawn read is taken again, and without the
        fence its cold start publishes under the key before the record lands,
        so the record says ``reaped`` over a live process nothing answered.
        The fence holds that claim at the door until the record and the audit
        are written, so it lands only under a recorded key. Red with the fence
        removed (``process_identity.ending_fence`` returning ``nullcontext()``): the
        claim lands before the record.
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
        provider, _, _ = await mgr.get_or_create("cron:fence7")
        mgr.release("cron:fence7")
        parked, door = _parks_at_the_fence_door(mgr)
        client = MagicMock()
        client._pid = 2**22 + 9393
        client._child_pids = {}
        client._start_time = _START_ID
        provider._client = client
        events: list[str] = []
        racing: list[asyncio.Task[Any]] = []

        async def _land() -> tuple[Any, bool, bool]:
            result = await mgr.get_or_create("cron:fence7")
            events.append("landed")
            return result

        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = mgr
        job = _make_job("fence7")
        svc._jobs = [job]
        claim = svc._claims["fence7"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60, task=_live_task()
        )
        children, start_id, sweep = _kill_path_stubs()
        real_append = svc._history.append

        async def _append(record: Any) -> None:
            # The passes and the post-pass read are over; the record is being
            # written. A completion's claim begins now and is followed until it
            # parks at the fence door or lands: unfenced, it lands here; fenced,
            # it is held at the door.
            racing.append(asyncio.create_task(_land()))
            events.append(await _held_or_landed(parked, racing[0]))
            events.append("record")
            await real_append(record)

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch.object(svc._history, "append", AsyncMock(side_effect=_append)),
            door,
            children,
            start_id,
            sweep,
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            patch("kiro_crew.platform_compat.kill_process_group", return_value=True) as group_kill,
        ):
            mock_sel().log_tool_invocation.side_effect = lambda **_: events.append("audit")
            await svc._force_reap("fence7", _JOB_TIMEOUT_SECS + 60, claim=claim)
            _, is_new, _ = await _within_the_ceiling(racing[0], "the held caller landing")

        assert events == ["held", "record", "audit", "landed"], (
            "the run was recorded reaped over a session that landed under the key after the "
            "post-pass read and before the record: no pass, no pop capture and no spawn read "
            f"named it, and only the fence orders it after the record: {events}"
        )
        group_kill.assert_not_called()
        assert _audited_outcome(mock_sel) == "reaped"
        assert is_new and mgr.has_session("cron:fence7")
        mgr.release("cron:fence7")
        await mgr.reset("cron:fence7")
