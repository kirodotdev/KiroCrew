"""Tests for the dashboard auto-nudge fire path.

The defect these pin: the fire path resolved its slot with a bare in-memory
dict lookup and, on a miss, deleted the loop from ``autonudge.json``. A miss is
not evidence of a dead session — the registry is empty for any tab the user has
navigated away from, and empty for EVERY slot immediately after a gateway
restart, because ``AutoNudgeService.start()`` re-arms timers before the
dashboard has restored its slots. So closing a browser tab or restarting the
gateway permanently destroyed a babysit loop, silently abandoning the pull
request it was watching.

The cron origin-injection path already had the correct behaviour (rehydrate from
persisted history, and respect a tab the user explicitly closed); this brings
the nudge path onto the same contract.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.autonudge import NudgeLoop
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.monitoring.completion import MonitorCompletionHook
from kiro_crew.monitoring.models import MonitorState
from kiro_crew.slack import gateway as gw


def _loop(slot_key: str = "chat-1-1785") -> NudgeLoop:
    return NudgeLoop(
        id="loop-abc",
        slot_key=slot_key,
        message="check the PR",
        idle_secs=300,
        max_cycles=24,
        cycle_count=3,
    )


def _slot(key: str = "chat-1-1785", *, running: bool = False, in_stage: bool = False) -> MagicMock:
    slot = MagicMock()
    slot.key = key
    slot.running = running
    # Real _ChatSlot defaults this False; a bare MagicMock would return a truthy
    # Mock and trip the busy guard, so model the default explicitly.
    slot._in_stage_execution = in_stage
    slot._closing = False
    slot.mode = ""
    slot.memory_mode = "persistent"
    return slot


def _orchestrator() -> gw.GatewayOrchestrator:
    cfg = KiroCrewConfig()
    with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U_OWNER"}):
        orch = gw.GatewayOrchestrator(cfg, no_dashboard=True, no_crons=True, no_open=True)
    orch.dashboard_state = SimpleNamespace(
        get_slot=MagicMock(return_value=None),
        push_slots_update=MagicMock(),
        _background_tasks=set(),
        # FIX 2 seam: the real method awaits the turn under the unattended-turn
        # semaphore. Here it is a plain passthrough returning the inner
        # coroutine, so ``_fake_spawn`` can still close exactly one coroutine.
        run_background_turn=MagicMock(side_effect=lambda _slot, coro: coro),
    )
    orch.autonudge_svc = MagicMock()
    orch.autonudge_svc.remove = AsyncMock()
    orch.autonudge_svc.monitor_dispatch_is_authorized = AsyncMock(return_value=True)
    orch._session_tasks = {}
    return orch


def _fake_spawn():
    """Stand-in for ``spawn_guarded_turn`` that does not run the turn.

    Closes the coroutine it is handed so the test never leaks a pending
    coroutine (which would surface as a RuntimeWarning rather than a failure).
    """
    calls: list[object] = []

    def _spawn(state, slot, coro, **kwargs):
        coro.close()
        calls.append(slot)
        return MagicMock(name="turn-task")

    _spawn.calls = calls  # type: ignore[attr-defined]
    return _spawn


async def _run_chat_through_monitor_boundary(*_args, **kwargs) -> None:
    """Model the runner's final structured-claim gate for gateway unit tests."""
    hook = kwargs.get("monitor_completion")
    if hook is None or not await hook.authorize():
        return
    hook.mark_accepted()


class TestDashboardNudgeSlotResolution:
    @pytest.mark.asyncio
    async def test_cold_slot_is_rehydrated_and_the_turn_runs(self) -> None:
        """The headline fix: a loop must survive a closed browser tab / restart."""
        orch = _orchestrator()
        loop = _loop()
        restored = _slot()
        spawn = _fake_spawn()
        with (
            patch.object(
                gw,
                "rehydrate_slot_from_history_async",
                new=AsyncMock(return_value=restored),
            ) as rehydrate,
            patch.object(gw, "spawn_guarded_turn", spawn),
            patch("kiro_crew.dashboard.chat._run_chat", new=AsyncMock()),
        ):
            assert await orch._fire_dashboard_nudge(loop) is True
        rehydrate.assert_awaited_once_with(orch.dashboard_state, loop.slot_key, adopt_closed=True)
        orch.autonudge_svc.remove.assert_not_awaited()
        assert spawn.calls == [restored], "the nudge turn did not run in the restored slot"
        assert orch._session_tasks[restored.key] is restored.task

    @pytest.mark.asyncio
    async def test_rehydration_uses_the_loop_affine_async_form(self) -> None:
        """Reads go off-loop; slot construction must stay ON the loop.

        Wrapping the whole rehydration in ``asyncio.to_thread`` looks equivalent
        but is not: slot construction broadcasts through
        ``asyncio.Queue.put_nowait`` / ``Event.set`` and ``ensure_future``, none
        of which are thread-safe. Off-loop that raises inside a broad ``except``
        that marks every connected dashboard client dead and drops it without a
        close frame, so browsers stop receiving frames — including the output of
        the very nudge turn this fix exists to run.
        """
        orch = _orchestrator()
        loop = _loop()
        restored = _slot()
        spawn = _fake_spawn()
        with (
            patch.object(
                gw, "rehydrate_slot_from_history_async", new=AsyncMock(return_value=restored)
            ) as rehydrate,
            patch.object(gw, "spawn_guarded_turn", spawn),
            patch("kiro_crew.dashboard.chat._run_chat", new=AsyncMock()),
        ):
            assert await orch._fire_dashboard_nudge(loop) is True
        rehydrate.assert_awaited_once_with(orch.dashboard_state, loop.slot_key, adopt_closed=True)
        assert spawn.calls == [restored]

    @pytest.mark.asyncio
    async def test_fire_does_not_touch_the_startup_restore_flag(self) -> None:
        """``restoring_open_slots`` is owned by the startup restore.

        A nudge fire that set and then unconditionally cleared it could clear it
        mid-restore — re-enabling the periodic flush and letting a partial slot
        snapshot be persisted, which loses open tabs. The flag only existed to
        fence the thread hop, so it goes with the hop.
        """
        orch = _orchestrator()
        orch.dashboard_state.restoring_open_slots = True  # pretend a restore is running
        with (
            patch.object(
                gw, "rehydrate_slot_from_history_async", new=AsyncMock(return_value=_slot())
            ),
            patch.object(gw, "spawn_guarded_turn", _fake_spawn()),
            patch("kiro_crew.dashboard.chat._run_chat", new=AsyncMock()),
        ):
            await orch._fire_dashboard_nudge(_loop())
        assert (
            orch.dashboard_state.restoring_open_slots is True
        ), "the nudge fire cleared a flag the startup restore owns"

    @pytest.mark.asyncio
    async def test_hot_slot_skips_rehydration(self) -> None:
        """get_slot stays the fast path; rehydration is only the miss fallback."""
        orch = _orchestrator()
        live = _slot()
        orch.dashboard_state.get_slot = MagicMock(return_value=live)
        spawn = _fake_spawn()
        with (
            patch.object(gw, "rehydrate_slot_from_history_async", new=AsyncMock()) as rehydrate,
            patch.object(gw, "spawn_guarded_turn", spawn),
            patch("kiro_crew.dashboard.chat._run_chat", new=AsyncMock()),
        ):
            assert await orch._fire_dashboard_nudge(_loop()) is True
        rehydrate.assert_not_awaited()
        assert spawn.calls == [live]

    @pytest.mark.asyncio
    async def test_structured_monitor_passes_completion_hook_only_to_its_turn(self) -> None:
        """A dashboard action reports raw completion without changing legacy turns."""
        orch = _orchestrator()
        live = _slot()
        orch.dashboard_state.get_slot = MagicMock(return_value=live)
        structured = _loop()
        structured.monitor = MonitorState(
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            created_ts=1_000.0,
            last_wake_fingerprint="failure-a",
            wake_in_flight=True,
        )
        spawned: list[asyncio.Task] = []

        def _spawn(_state, _slot, coro):
            task = asyncio.create_task(coro)
            spawned.append(task)
            return task

        run_chat = AsyncMock(side_effect=_run_chat_through_monitor_boundary)
        with (
            patch.object(gw, "spawn_guarded_turn", _spawn),
            patch("kiro_crew.dashboard.chat._run_chat", new=run_chat),
        ):
            assert await orch._fire_dashboard_nudge(structured) is True
            assert await orch._fire_dashboard_nudge(_loop()) is True
            await asyncio.gather(*spawned)

        first, second = run_chat.call_args_list
        assert isinstance(first.kwargs["monitor_completion"], MonitorCompletionHook)
        assert first.kwargs["_prompt_depth"] == 1
        assert "monitor_completion" not in second.kwargs
        assert "_prompt_depth" not in second.kwargs

    @pytest.mark.asyncio
    async def test_queued_monitor_rechecks_claim_after_background_permit(self) -> None:
        orch = _orchestrator()
        live = _slot()
        live.unattended = True
        orch.dashboard_state.get_slot = MagicMock(return_value=live)
        structured = _loop()
        structured.monitor = MonitorState(
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            created_ts=1_000.0,
            last_wake_fingerprint="failure-a",
            wake_in_flight=True,
        )
        queued = asyncio.Event()
        permit = asyncio.Event()

        async def _run_after_permit(_slot, coro):
            queued.set()
            await permit.wait()
            return await coro

        spawned: list[asyncio.Task] = []

        def _spawn(_state, _slot, coro):
            task = asyncio.create_task(coro)
            spawned.append(task)
            return task

        orch.dashboard_state.run_background_turn = _run_after_permit
        run_chat = AsyncMock(side_effect=_run_chat_through_monitor_boundary)

        with (
            patch.object(gw, "spawn_guarded_turn", _spawn),
            patch("kiro_crew.dashboard.chat._run_chat", new=run_chat),
        ):
            fire = asyncio.create_task(orch._fire_dashboard_nudge(structured, "[Monitor wake]"))
            await queued.wait()
            assert not fire.done()
            orch.autonudge_svc.monitor_dispatch_is_authorized.assert_not_awaited()
            live.append.assert_not_called()
            orch.autonudge_svc.monitor_dispatch_is_authorized.return_value = False
            permit.set()
            result = await fire
            await spawned[0]

        assert result is gw.MonitorDispatchResult.UNAVAILABLE
        orch.autonudge_svc.monitor_dispatch_is_authorized.assert_awaited_once_with(
            structured.id, "failure-a"
        )
        live.append.assert_not_called()
        run_chat.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_monitor_stop_during_runner_setup_returns_unavailable(self) -> None:
        """Dashboard dispatch is not accepted until the runner reaches provider entry."""
        orch = _orchestrator()
        live = _slot()
        orch.dashboard_state.get_slot = MagicMock(return_value=live)
        structured = _loop()
        structured.monitor = MonitorState(
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            created_ts=1_000.0,
            last_wake_fingerprint="failure-a",
            wake_in_flight=True,
        )
        setup_entered = asyncio.Event()
        finish_setup = asyncio.Event()

        async def _run_chat_at_real_boundary(*_args, **kwargs):
            setup_entered.set()
            await finish_setup.wait()
            hook = kwargs["monitor_completion"]
            if not await hook.authorize():
                return
            hook.mark_accepted()

        spawned: list[asyncio.Task] = []

        def _spawn(_state, _slot, coro):
            task = asyncio.create_task(coro)
            spawned.append(task)
            return task

        orch.autonudge_svc.monitor_dispatch_is_authorized.return_value = True
        with (
            patch.object(gw, "spawn_guarded_turn", _spawn),
            patch("kiro_crew.dashboard.chat._run_chat", new=_run_chat_at_real_boundary),
        ):
            fire = asyncio.create_task(orch._fire_dashboard_nudge(structured, "[Monitor wake]"))
            await setup_entered.wait()
            orch.autonudge_svc.monitor_dispatch_is_authorized.return_value = False
            finish_setup.set()
            result = await fire
            await spawned[0]

        assert result is gw.MonitorDispatchResult.UNAVAILABLE

    @pytest.mark.asyncio
    async def test_monitor_stop_during_rehydration_never_spawns_as_ordinary(self) -> None:
        """A revoked structured claim cannot lose its hook and enter the legacy path."""
        orch = _orchestrator()
        live = _slot()
        structured = _loop()
        structured.monitor = MonitorState(
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            created_ts=1_000.0,
            last_wake_fingerprint="failure-a",
            wake_in_flight=True,
        )
        spawn = _fake_spawn()

        async def _stop_during_rehydration(*_args, **_kwargs):
            assert structured.monitor is not None
            structured.monitor.wake_in_flight = False
            return live

        with (
            patch.object(
                gw,
                "rehydrate_slot_from_history_async",
                new=_stop_during_rehydration,
            ),
            patch.object(gw, "spawn_guarded_turn", spawn),
            patch("kiro_crew.dashboard.chat._run_chat", new=AsyncMock()),
        ):
            result = await orch._fire_dashboard_nudge(structured, "[Monitor wake]")

        assert result is gw.MonitorDispatchResult.UNAVAILABLE
        live.append.assert_not_called()
        assert spawn.calls == []

    @pytest.mark.asyncio
    async def test_monitor_shutdown_refusal_returns_busy_without_appending(self) -> None:
        """Admission remains pending until the runner crosses the shutdown gate."""
        orch = _orchestrator()
        live = _slot()
        orch.dashboard_state.get_slot = MagicMock(return_value=live)
        structured = _loop()
        structured.monitor = MonitorState(
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            created_ts=1_000.0,
            last_wake_fingerprint="failure-a",
            wake_in_flight=True,
        )
        spawned: list[asyncio.Task] = []

        async def _run_chat_refused_by_shutdown(*_args, **kwargs):
            hook = kwargs["monitor_completion"]
            assert await hook.authorize()
            # SessionManager.begin_turn refuses before mark_accepted.

        def _spawn(_state, _slot, coro):
            task = asyncio.create_task(coro)
            spawned.append(task)
            return task

        with (
            patch.object(gw, "spawn_guarded_turn", _spawn),
            patch("kiro_crew.dashboard.chat._run_chat", new=_run_chat_refused_by_shutdown),
        ):
            result = await orch._fire_dashboard_nudge(structured, "[Monitor wake]")
            await spawned[0]

        assert result is gw.MonitorDispatchResult.BUSY
        live.append.assert_not_called()

    @pytest.mark.asyncio
    async def test_monitor_rechecks_dashboard_mode_before_provider_entry(self) -> None:
        orch = _orchestrator()
        live = _slot()
        orch.dashboard_state.get_slot = MagicMock(return_value=live)
        structured = _loop()
        structured.monitor = MonitorState(
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            created_ts=1_000.0,
            last_wake_fingerprint="failure-a",
            wake_in_flight=True,
        )
        spawned: list[asyncio.Task] = []

        async def _switch_mode_before_authorization(*_args, **kwargs):
            live.mode = "crew"
            assert not await kwargs["monitor_completion"].authorize()

        def _spawn(_state, _slot, coro):
            task = asyncio.create_task(coro)
            spawned.append(task)
            return task

        with (
            patch.object(gw, "spawn_guarded_turn", _spawn),
            patch(
                "kiro_crew.dashboard.chat._run_chat",
                new=_switch_mode_before_authorization,
            ),
        ):
            result = await orch._fire_dashboard_nudge(structured, "[Monitor wake]")
            await spawned[0]

        assert result is gw.MonitorDispatchResult.UNAVAILABLE
        orch.autonudge_svc.monitor_dispatch_is_authorized.assert_awaited_once_with(
            structured.id, "failure-a"
        )
        live.append.assert_not_called()

    @pytest.mark.asyncio
    async def test_monitor_reports_busy_when_background_admission_times_out(self) -> None:
        orch = _orchestrator()
        live = _slot()
        live.unattended = True
        orch.dashboard_state.get_slot = MagicMock(return_value=live)
        structured = _loop()
        structured.monitor = MonitorState(
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            created_ts=1_000.0,
            last_wake_fingerprint="failure-a",
            wake_in_flight=True,
        )

        async def _reject_at_capacity(_slot, coro):
            coro.close()
            raise TimeoutError("background queue remained full")

        spawned: list[asyncio.Task] = []

        def _spawn(_state, _slot, coro):
            task = asyncio.create_task(coro)
            spawned.append(task)
            return task

        orch.dashboard_state.run_background_turn = _reject_at_capacity
        run_chat = AsyncMock()

        with (
            patch.object(gw, "spawn_guarded_turn", _spawn),
            patch("kiro_crew.dashboard.chat._run_chat", new=run_chat),
        ):
            result = await orch._fire_dashboard_nudge(structured, "[Monitor wake]")
            await spawned[0]

        assert result is gw.MonitorDispatchResult.BUSY
        orch.autonudge_svc.monitor_dispatch_is_authorized.assert_not_awaited()
        live.append.assert_not_called()
        run_chat.assert_not_called()

    @pytest.mark.asyncio
    async def test_unreachable_session_retires_the_loop_once_with_a_reason(self, caplog) -> None:
        """A genuinely gone session (no history, or deleted) still retires.

        A tab the user dismissed with ✕ is retired by the close handler itself
        now (api_chat_slot_delete removes the loop), not by this miss — the fire
        path adopts a ``closed`` session so idle archival cannot destroy a loop.
        """
        orch = _orchestrator()
        loop = _loop()
        spawn = _fake_spawn()
        with (
            patch.object(gw, "rehydrate_slot_from_history_async", new=AsyncMock(return_value=None)),
            patch.object(gw, "spawn_guarded_turn", spawn),
            caplog.at_level(logging.WARNING, logger=gw.logger.name),
        ):
            assert await orch._fire_dashboard_nudge(loop) is False
        orch.autonudge_svc.remove.assert_awaited_once_with(loop.id)
        assert spawn.calls == []
        assert loop.slot_key in caplog.text
        assert "unreachable" in caplog.text

    @pytest.mark.asyncio
    async def test_running_slot_skips_without_retiring_the_loop(self) -> None:
        """A turn in flight defers the cycle; it must not count or destroy."""
        orch = _orchestrator()
        loop = _loop()
        before = loop.cycle_count
        orch.dashboard_state.get_slot = MagicMock(return_value=_slot(running=True))
        spawn = _fake_spawn()
        with (
            patch.object(gw, "spawn_guarded_turn", spawn),
            patch("kiro_crew.dashboard.chat._run_chat", new=AsyncMock()),
        ):
            assert await orch._fire_dashboard_nudge(loop) is False
        orch.autonudge_svc.remove.assert_not_awaited()
        assert spawn.calls == []
        assert loop.cycle_count == before

    @pytest.mark.asyncio
    async def test_stage_execution_slot_skips_without_retiring_the_loop(self) -> None:
        """A multi-stage plan mid-flight defers the cycle; it must not clobber it.

        Between stages the plan sets ``slot.task = None`` (chat_orchestrator), so
        ``slot.running`` reads False even though the plan is still executing. The
        nudge must still defer on ``_in_stage_execution`` — firing here would start
        a concurrent turn that scatters the plan's output.
        """
        orch = _orchestrator()
        loop = _loop()
        before = loop.cycle_count
        orch.dashboard_state.get_slot = MagicMock(return_value=_slot(running=False, in_stage=True))
        spawn = _fake_spawn()
        with (
            patch.object(gw, "spawn_guarded_turn", spawn),
            patch("kiro_crew.dashboard.chat._run_chat", new=AsyncMock()),
        ):
            assert await orch._fire_dashboard_nudge(loop) is False
        orch.autonudge_svc.remove.assert_not_awaited()
        assert spawn.calls == []
        assert loop.cycle_count == before

    @pytest.mark.asyncio
    async def test_dashboard_not_ready_skips_without_retiring_the_loop(self) -> None:
        """_init_autonudge can run before the dashboard exists, or with none."""
        orch = _orchestrator()
        orch.dashboard_state = None
        assert await orch._fire_dashboard_nudge(_loop()) is False
        orch.autonudge_svc.remove.assert_not_awaited()
