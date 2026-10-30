"""Coverage tests for ``SubagentManager`` control of a live run.

Session-sharing and provider probes, the intentional-cancel contract, steer and
follow-up refusals, the cross-platform clock-tick constant, and the reaper's run
and conversation ages on the monotonic clock. Every kill, release and sweep the
reaper makes is a recorder: nothing here launches a process or deletes a file.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from subagent_coverage_helpers import _info, _manager

from kiro_crew import subagent as sa
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.testing.wait import async_wait_until

# ── Manager: session sharing / provider probes ────────────────────────────


class TestShouldUseSessionSharing:
    @pytest.mark.parametrize(
        "enabled",
        [
            pytest.param(False, id="test_disabled_by_config"),
            pytest.param(True, id="test_eligible_parent_accepted"),
        ],
    )
    def test_config_flag_decides_for_an_eligible_parent(self, enabled: bool) -> None:
        mgr = _manager()
        cfg = MagicMock()
        cfg.agent.session_sharing = enabled
        with patch("kiro_crew.subagent.KiroCrewConfig.load", return_value=cfg):
            assert mgr._should_use_session_sharing(_info(parent_session_key="dash:1")) is enabled

    def test_config_failure_fails_closed(self) -> None:
        mgr = _manager()
        with patch("kiro_crew.subagent.KiroCrewConfig.load", side_effect=RuntimeError):
            assert mgr._should_use_session_sharing(_info(parent_session_key="dash:1")) is False

    @pytest.mark.parametrize(
        "override",
        [{"model": "sonnet"}, {"allowed_tools": ["read"]}, {"bare": True}],
    )
    def test_cc_specific_spawn_excluded(self, override: dict) -> None:
        mgr = _manager()
        cfg = MagicMock()
        cfg.agent.session_sharing = True
        info = _info(parent_session_key="dash:1", **override)
        with patch("kiro_crew.subagent.KiroCrewConfig.load", return_value=cfg):
            assert mgr._should_use_session_sharing(info) is False

    def test_parentless_spawn_excluded(self) -> None:
        mgr = _manager()
        cfg = MagicMock()
        cfg.agent.session_sharing = True
        with patch("kiro_crew.subagent.KiroCrewConfig.load", return_value=cfg):
            assert mgr._should_use_session_sharing(_info()) is False


class TestGetParentRuntime:
    def test_no_provider(self) -> None:
        mgr = _manager()
        mgr._sessions.get_provider.return_value = None
        assert mgr._get_parent_runtime("dash:1") is None

    def test_non_acp_session_provider(self) -> None:
        mgr = _manager()
        mgr._sessions.get_provider.return_value = SimpleNamespace(client=object())
        assert mgr._get_parent_runtime("dash:1") is None

    def test_acp_session_provider_yields_runtime(self) -> None:
        mgr = _manager()
        runtime = object()
        inner = MagicMock(spec=sa.AcpSessionProvider)
        inner._runtime = runtime
        mgr._sessions.get_provider.return_value = SimpleNamespace(client=inner)
        assert mgr._get_parent_runtime("dash:1") is runtime


class TestIsCcProvider:
    """Which HOME tree a session's files live in, asked as a capability.

    ``_is_cc_provider`` reads ``SessionCapabilities.provider_seam`` through
    ``capabilities_of``, so these tests hand it a provider carrying a real
    capability record instead of patching a module-level predicate. The third case
    is the one a bare attribute read gets wrong: a shape that is not a provider
    must answer False, which is what the ``isinstance`` gate in ``capabilities_of``
    buys.
    """

    @staticmethod
    def _provider(backend: str) -> SimpleNamespace:
        from kiro_crew.agent_sdk.capabilities import capabilities_for

        return SimpleNamespace(capabilities=capabilities_for(backend))

    def test_claude_seam_routes_to_the_claude_home(self) -> None:
        from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE

        assert SubagentManager._is_cc_provider(self._provider(ACP_BACKEND_CLAUDE)) is True

    @pytest.mark.parametrize("backend", ["", "kas", "codex"])
    def test_non_claude_backend(self, backend: str) -> None:
        assert SubagentManager._is_cc_provider(self._provider(backend)) is False

    def test_a_shape_that_is_not_a_provider_answers_false(self) -> None:
        assert SubagentManager._is_cc_provider(object()) is False


# ── Manager: intentional cancel contract ──────────────────────────────────


class TestCancelTaskIntentionally:
    @pytest.mark.asyncio
    async def test_marked_cancel_leaves_recovery_budget(self) -> None:
        mgr = _manager()
        info = _info(user_stopped=True)

        async def _sleep() -> None:
            # flake-ok: an hour-long placeholder body for a task the test cancels at once; moved verbatim
            # nosemgrep: kirocrew.test-sleep-as-barrier -- the sleep is the body of a task the test cancels at once, so the hour never elapses; moved verbatim
            await asyncio.sleep(3600)

        task = asyncio.ensure_future(_sleep())
        mgr._cancel_task_intentionally(task, info, reason="user_stop")
        # flake-ok: awaits a task the line above cancelled; moved verbatim
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled() is True
        assert info._cancel_retry_used is False

    @pytest.mark.asyncio
    async def test_unmarked_cancel_consumes_recovery_budget(self) -> None:
        mgr = _manager()
        info = _info()

        async def _sleep() -> None:
            # flake-ok: an hour-long placeholder body for a task the test cancels at once; moved verbatim
            # nosemgrep: kirocrew.test-sleep-as-barrier -- the sleep is the body of a task the test cancels at once, so the hour never elapses; moved verbatim
            await asyncio.sleep(3600)

        task = asyncio.ensure_future(_sleep())
        mgr._cancel_task_intentionally(task, info, reason="oops")
        # flake-ok: awaits a task the line above cancelled; moved verbatim
        await asyncio.gather(task, return_exceptions=True)
        assert info._cancel_retry_used is True

    @pytest.mark.asyncio
    async def test_shutdown_counts_as_a_marker(self) -> None:
        mgr = _manager()
        mgr._shutting_down = True

        async def _sleep() -> None:
            # flake-ok: an hour-long placeholder body for a task the test cancels at once; moved verbatim
            # nosemgrep: kirocrew.test-sleep-as-barrier -- the sleep is the body of a task the test cancels at once, so the hour never elapses; moved verbatim
            await asyncio.sleep(3600)

        task = asyncio.ensure_future(_sleep())
        mgr._cancel_task_intentionally(task, None, reason="shutdown")
        # flake-ok: awaits a task the line above cancelled; moved verbatim
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled() is True


class TestCancelLookup:
    @pytest.mark.asyncio
    async def test_finished_agent_is_false(self) -> None:
        mgr = _manager()
        mgr._agents["a"] = _info("a", done=True)
        assert await mgr.cancel("a") is False


# ── Manager: steer / follow-up refusals ───────────────────────────────────


class TestSteerRun:

    @pytest.mark.asyncio
    async def test_provider_rejection_reported(self) -> None:
        mgr = _manager()
        info = _info("a")
        info._session_sharing = True
        info._shared_provider = MagicMock(steer=AsyncMock(return_value=False))
        mgr._agents["a"] = info
        assert await mgr.steer_run("a", "hi") == (False, "steer rejected by provider")

    @pytest.mark.asyncio
    async def test_audit_failure_does_not_change_the_verdict(self) -> None:
        mgr = _manager()
        info = _info("a")
        info._session_sharing = True
        info._shared_provider = MagicMock(steer=AsyncMock(return_value=True))
        mgr._agents["a"] = info
        with patch.object(sa, "sel", side_effect=RuntimeError("sel down")):
            assert await mgr.steer_run("a", "hi") == (True, "ok")

    @pytest.mark.asyncio
    async def test_run_finishing_during_the_grace_wait_flips_to_not_running(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sa, "_STEER_STARTUP_WAIT_SECS", 1.0)
        monkeypatch.setattr(sa, "_STEER_STARTUP_POLL_SECS", 0.01)
        mgr = _manager()
        info = _info("a")
        mgr._agents["a"] = info

        async def _finish() -> None:
            # flake-ok: pre-existing 20 ms delay before the finisher flips done while steer_run polls; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 20 ms delay inside the finisher task, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0.02)
            info.done = True

        finisher = asyncio.ensure_future(_finish())
        ok, detail = await mgr.steer_run("a", "hi")
        # flake-ok: awaits the finisher task, which ends after its own 20 ms sleep; moved verbatim
        await finisher
        assert ok is False
        assert detail.startswith("not_running")


class TestFollowUpRun:
    @pytest.mark.asyncio
    async def test_unknown_run(self) -> None:
        assert await _manager().follow_up_run("nope", "hi") == (False, "not_found")

    @pytest.mark.asyncio
    async def test_finished_run_refused(self) -> None:
        mgr = _manager()
        mgr._agents["a"] = _info("a", done=True)
        ok, detail = await mgr.follow_up_run("a", "hi")
        assert ok is False
        assert detail.startswith("not_running")

    @pytest.mark.asyncio
    async def test_shutdown_refuses_rather_than_dropping(self) -> None:
        mgr = _manager()
        mgr._agents["a"] = _info("a")
        mgr._shutting_down = True
        ok, detail = await mgr.follow_up_run("a", "hi")
        assert ok is False
        assert detail.startswith("shutting_down")

    @pytest.mark.asyncio
    async def test_queued_and_watcher_armed_once(self) -> None:
        mgr = _manager()
        info = _info("a")
        mgr._agents["a"] = info
        with (
            patch.object(sa, "sel"),
            patch.object(mgr, "_arm_followup_watcher") as arm,
        ):
            assert await mgr.follow_up_run("a", "first") == (True, "queued")
            info._followup_watcher = True
            assert await mgr.follow_up_run("a", "second") == (True, "queued")
        assert info.pending_followups == ["first", "second"]
        assert arm.call_count == 1

    @pytest.mark.asyncio
    async def test_audit_failure_does_not_lose_the_message(self) -> None:
        mgr = _manager()
        info = _info("a")
        info._followup_watcher = True
        mgr._agents["a"] = info
        with patch.object(sa, "sel", side_effect=RuntimeError("sel down")):
            assert await mgr.follow_up_run("a", "hi") == (True, "queued")
        assert info.pending_followups == ["hi"]

    def test_audit_helper_swallows_failure(self) -> None:
        mgr = _manager()
        with patch.object(sa, "sel") as sel:
            sel.return_value.log_tool_invocation.side_effect = RuntimeError("sel down")
            mgr._audit_followup(_info("a", parent_session_key="dash:1"), "followup_expired")
        sel.return_value.log_tool_invocation.assert_called_once_with(
            session_key="dash:1",
            source="subagent",
            tool_name="spawn_steer",
            outcome="followup_expired",
            metadata={"subagent_id": "a"},
        )


# ── Cross-platform constants ──────────────────────────────────────────────


class TestPlatformConstants:
    def test_clock_ticks_positive_on_every_platform(self) -> None:
        """``_CLK_TCK`` divides a CPU delta, so a zero/absent value would make
        the cost sampler raise. Windows has no ``os.sysconf``; the module falls
        back to 100 there."""
        assert isinstance(sa._CLK_TCK, int)
        assert sa._CLK_TCK > 0


# ── run and conversation ages on the monotonic clock ──────────────────────
# The reaper resolves ``time`` through the ``kiro_crew.subagent`` facade, so the
# clock is stepped there. Every kill, release, sweep, prune and store call the
# reaper makes per tick is a recorder: nothing is killed and nothing is deleted.

_RUN_TIMEOUT = 600


class _SteppedClock:
    """The ``time`` module with its wall clock and its monotonic clock offset."""

    def __init__(self) -> None:
        self.wall = 0.0
        self.mono = 0.0

    def time(self) -> float:
        return time.time() + self.wall

    def monotonic(self) -> float:
        return time.monotonic() + self.mono

    def __getattr__(self, name: str) -> object:
        return getattr(time, name)


async def _reaper_ticks(mgr: SubagentManager, clock: _SteppedClock) -> tuple[AsyncMock, AsyncMock]:
    """Run the real reaper loop briefly on *clock*; return the reap and release recorders."""
    reap = AsyncMock()
    release = AsyncMock(return_value=(True, "released"))
    pump = MagicMock()
    with (
        patch.object(sa, "_REAPER_INTERVAL", 0),
        patch.object(sa, "compact_cost_log"),
        patch.object(sa, "prune_stale_tombstones", MagicMock(return_value=0)),
        patch.object(sa, "time", clock),
        patch.object(mgr, "_sample_live_costs"),
        patch.object(mgr, "_sweep_stuck_waves_async", new=AsyncMock()),
        patch.object(mgr, "_sweep_digest_holds_async", new=AsyncMock()),
        patch.object(mgr, "retry_owed_teardown_sweeps", new=AsyncMock()),
        patch.object(mgr, "_maybe_flag_stall", new=AsyncMock()),
        patch.object(mgr, "_taskq_pump", pump),
        patch.object(type(mgr._admission), "taskq_schedule_owed_replay", MagicMock()),
        patch.object(mgr, "_force_reap", reap),
        patch.object(mgr, "release_conversation_async", release),
    ):
        task = asyncio.ensure_future(mgr._reaper_loop())
        # The pump runs once per tick, before that tick's reap checks, so its
        # second call means a whole tick has run.
        await async_wait_until(
            lambda: pump.call_count >= 2 or task.done(),
            timeout=30,
            describe=lambda: f"pump calls={pump.call_count}, reaper done={task.done()}",
        )
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=30)
    assert pump.call_count >= 2, "the reaper loop did not complete a tick"
    return reap, release


def _called_for(recorder: AsyncMock, item_id: str) -> bool:
    return any(c.args and c.args[0] == item_id for c in recorder.await_args_list)


def _registered_run(mgr: SubagentManager, age_secs: float) -> SubagentInfo:
    """A run *age_secs* old on both clocks and past startup, so only the run
    timeout applies to it."""
    born = time.time() - age_secs
    info = _info("run01", started=born)
    info._started_mono = time.monotonic() - age_secs
    info.turns = 3
    info._exec_started = born
    info._first_stream_started = born
    mgr._agents[info.id] = info
    return info


class TestReaperClock:
    @pytest.mark.asyncio
    async def test_a_run_past_its_timeout_is_reaped(self) -> None:
        mgr = _manager(default_timeout=_RUN_TIMEOUT)
        mgr._conv_registry_rebuilt = True
        _registered_run(mgr, age_secs=_RUN_TIMEOUT + 60)
        reap, _release = await _reaper_ticks(mgr, _SteppedClock())
        assert _called_for(reap, "run01")

    @pytest.mark.asyncio
    async def test_a_young_run_is_not_reaped(self) -> None:
        mgr = _manager(default_timeout=_RUN_TIMEOUT)
        mgr._conv_registry_rebuilt = True
        _registered_run(mgr, age_secs=5)
        reap, _release = await _reaper_ticks(mgr, _SteppedClock())
        assert not _called_for(reap, "run01")

    @pytest.mark.asyncio
    async def test_a_forward_wall_clock_step_does_not_reap_a_healthy_run(self) -> None:
        mgr = _manager(default_timeout=_RUN_TIMEOUT)
        mgr._conv_registry_rebuilt = True
        _registered_run(mgr, age_secs=5)
        clock = _SteppedClock()
        clock.wall = 2 * 3600.0
        reap, _release = await _reaper_ticks(mgr, clock)
        assert not _called_for(
            reap, "run01"
        ), "a 5-second-old run was reaped as timed out after a 2 h forward wall-clock step"

    @pytest.mark.asyncio
    async def test_a_backward_wall_clock_step_still_reaps_a_run_past_its_timeout(self) -> None:
        mgr = _manager(default_timeout=_RUN_TIMEOUT)
        mgr._conv_registry_rebuilt = True
        _registered_run(mgr, age_secs=_RUN_TIMEOUT + 60)
        clock = _SteppedClock()
        clock.wall = -3600.0
        reap, _release = await _reaper_ticks(mgr, clock)
        assert _called_for(
            reap, "run01"
        ), "a run 60 s past its timeout was not reaped after a 1 h backward wall-clock step"


class TestConversationTtlClock:
    @pytest.mark.asyncio
    async def test_a_forward_wall_clock_step_keeps_a_recently_used_conversation(self) -> None:
        from kiro_crew.subagent_persistence import RetentionPromotionResult

        mgr = _manager()
        mgr._conv_registry_rebuilt = True
        with patch(
            "kiro_crew.subagent_persistence.promote_retention",
            return_value=RetentionPromotionResult.PROMOTED,
        ):
            mgr._promote_conversation("c1", "subagent:c1")
        clock = _SteppedClock()
        clock.wall = 7 * 3600.0
        _reap, release = await _reaper_ticks(mgr, clock)
        assert not _called_for(
            release, "c1"
        ), "a conversation used a moment ago was released after a 7 h forward wall-clock step"

    @pytest.mark.asyncio
    async def test_a_restart_carries_a_stored_conversations_age(self) -> None:
        # Stored ``updated_at`` past the TTL: the first pass after the restart
        # releases it.
        mgr = _manager()
        stored = time.time() - sa._CONVERSATION_TTL_SECS - 60.0
        scan = MagicMock(return_value=[("c1", "subagent:c1", "sid-1", "acp", "", stored)])
        with patch.object(mgr, "_scan_keep_states", scan):
            _reap, release = await _reaper_ticks(mgr, _SteppedClock())
        assert _called_for(release, "c1")

    @pytest.mark.asyncio
    async def test_a_restart_reads_a_stored_stamp_in_the_future_as_age_zero(self) -> None:
        # Stored ``updated_at`` 30 days ahead: the clock stepped back after it was
        # written. Age 0 keeps the conversation through the restart's first pass
        # and releases it one TTL later, not 30 days later.
        mgr = _manager()
        stored = time.time() + 30 * 86400.0
        scan = MagicMock(return_value=[("c1", "subagent:c1", "sid-1", "acp", "", stored)])
        clock = _SteppedClock()
        with patch.object(mgr, "_scan_keep_states", scan):
            _reap, release = await _reaper_ticks(mgr, clock)
        assert mgr._conv_registry_rebuilt
        assert not _called_for(release, "c1"), "a future stamp released the conversation"
        clock.wall = clock.mono = sa._CONVERSATION_TTL_SECS + 60.0  # one TTL later
        _reap, release = await _reaper_ticks(mgr, clock)
        assert _called_for(release, "c1"), (
            "a conversation whose stored stamp is 30 days in the future was not released "
            "one TTL after the restart"
        )


class _OneBaseClock(_SteppedClock):
    """Wall and monotonic clocks that read the same value until one is stepped,
    so a stamp taken before the step is valid on either clock."""

    def __init__(self) -> None:
        super().__init__()
        self.base = time.time()
        self._t0 = time.monotonic()

    def time(self) -> float:
        return self.base + self.wall + (time.monotonic() - self._t0)

    def monotonic(self) -> float:
        return self.base + self.mono + (time.monotonic() - self._t0)


def _starting_run(mgr: SubagentManager, clock: _OneBaseClock, into: float) -> SubagentInfo:
    """A run *into* seconds into its start: executing, no runtime PID, no answer
    on its session and no turn, so the startup watchdog applies to it."""
    born = clock.base - into
    info = _info("run02", started=born)
    info._started_mono = born
    info._exec_started = born
    info._exec_started_mono = born
    info.last_activity = born
    info._last_activity_mono = born
    mgr._agents[info.id] = info
    return info


class TestStartupWatchdogClock:
    @pytest.mark.asyncio
    async def test_a_start_past_its_deadline_is_reaped(self) -> None:
        mgr = _manager(default_timeout=_RUN_TIMEOUT)
        mgr._conv_registry_rebuilt = True
        clock = _OneBaseClock()
        _starting_run(mgr, clock, into=mgr._startup_deadline + 60.0)
        reap, _release = await _reaper_ticks(mgr, clock)
        assert _called_for(reap, "run02")

    @pytest.mark.asyncio
    async def test_a_starting_run_is_not_reaped_without_a_clock_step(self) -> None:
        mgr = _manager(default_timeout=_RUN_TIMEOUT)
        mgr._conv_registry_rebuilt = True
        clock = _OneBaseClock()
        _starting_run(mgr, clock, into=5.0)
        reap, _release = await _reaper_ticks(mgr, clock)
        assert not _called_for(reap, "run02")

    @pytest.mark.asyncio
    async def test_a_forward_wall_clock_step_does_not_reap_a_starting_run(self) -> None:
        mgr = _manager(default_timeout=_RUN_TIMEOUT)
        mgr._conv_registry_rebuilt = True
        clock = _OneBaseClock()
        _starting_run(mgr, clock, into=5.0)
        clock.wall = 2 * 3600.0
        reap, _release = await _reaper_ticks(mgr, clock)
        assert not _called_for(
            reap, "run02"
        ), "a run 5 s into its start was reaped after a 2 h forward wall-clock step"
