"""SubagentManager live config: every constructor-captured limit follows config.json.

``SubagentManager`` copies nine ``agent.*`` limits (plus ``session.pool_size``
through the auto-sized cap) at construction. ``reconfigure`` re-derives every
copy from a reloaded config, and the manager subscribes itself on the process
watcher so a write from ANY writer reaches it. These tests pin: each derived
field, the ``0`` timeout / stall sentinels, that a raised cap admits a queued
spawn through the real pump, that a lowered cap cancels nothing in flight, and
that an unrelated change is ignored.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from _hot_reload_helpers import change
from _hot_reload_helpers import write_config as _write

from kiro_crew.config import live
from kiro_crew.config.live import ConfigWatch
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.subagent import _STALL_IDLE_SECS, _TIMEOUT_SECS, SubagentInfo, SubagentManager


def _mgr(max_concurrent: int = 4) -> SubagentManager:
    mgr = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        max_concurrent=max_concurrent,
        default_turn_limit=100,
        default_timeout=1800,
        stall_idle_secs=300,
    )
    mgr._fire_event = AsyncMock()
    mgr._on_done = AsyncMock()
    return mgr


async def _dispatch(new: KiroCrewConfig, *paths: str) -> None:
    """Run one reload through the watcher's own prefix filter."""
    live.watch().prime(new)
    await live.watch()._dispatch(change(new, *paths, old=KiroCrewConfig()))


class TestReconfigureDerivesEveryField:
    @pytest.mark.asyncio
    async def test_dispatched_change_updates_each_captured_value(self) -> None:
        mgr = _mgr(max_concurrent=4)
        mgr._global_approval_mode = "interactive"  # the value cached at boot
        fresh = KiroCrewConfig()
        fresh.agent.max_subagents = 7
        fresh.agent.subagent_max_turns = 42
        fresh.agent.subagent_timeout_secs = 900
        fresh.agent.subagent_stall_idle_secs = 77
        fresh.agent.subagent_spawn_stagger_secs = 0.25
        fresh.agent.subagent_result_ttl_secs = 123
        fresh.agent.approval_mode = "auto"  # boot-only: must NOT be adopted
        fresh.agent.completion_keep = "tail"
        fresh.agent.completion_keep_chars = 1234

        await _dispatch(
            fresh,
            "agent.max_subagents",
            "agent.subagent_max_turns",
            "agent.subagent_timeout_secs",
            "agent.subagent_stall_idle_secs",
            "agent.subagent_spawn_stagger_secs",
            "agent.subagent_result_ttl_secs",
            "agent.approval_mode",  # dispatched, ignored by the manager
            "agent.completion_keep",
            "agent.completion_keep_chars",
        )

        assert mgr.max_concurrent == 7
        assert mgr._default_turn_limit == 42
        assert mgr._default_timeout == 900
        assert mgr._stall_idle_secs == 77
        assert mgr._spawn_stagger_secs == 0.25
        assert mgr._result_ttl_secs == 123
        # ``agent.approval_mode`` is restart-marked: every channel dispatcher
        # resolves it once at start, so the manager keeps its boot value too.
        assert mgr._global_approval_mode == "interactive"
        assert mgr._completion_keep == "tail"
        assert mgr._completion_keep_chars == 1234

    def test_zero_timeout_and_stall_keep_the_builtin_sentinel(self) -> None:
        mgr = _mgr()
        fresh = KiroCrewConfig()
        fresh.agent.subagent_timeout_secs = 0
        fresh.agent.subagent_stall_idle_secs = 0
        mgr.apply_limits(fresh, max_concurrent=4)
        assert mgr._default_timeout == _TIMEOUT_SECS
        assert mgr._stall_idle_secs == _STALL_IDLE_SECS

    def test_negative_stagger_floors_at_zero(self) -> None:
        mgr = _mgr()
        fresh = KiroCrewConfig()
        fresh.agent.subagent_spawn_stagger_secs = -3.0
        mgr.apply_limits(fresh, max_concurrent=4)
        assert mgr._spawn_stagger_secs == 0.0

    def test_auto_cap_is_resolved_through_resolve_max_subagents(self) -> None:
        mgr = _mgr(max_concurrent=4)
        fresh = KiroCrewConfig()
        fresh.agent.max_subagents = 0  # auto sentinel
        with patch("kiro_crew.subagent.resolve_max_subagents", return_value=11) as resolve:
            mgr.apply_limits(fresh)
        resolve.assert_called_once_with(fresh)
        assert mgr.max_concurrent == 11

    def test_cap_resolution_failure_keeps_the_current_cap(self) -> None:
        mgr = _mgr(max_concurrent=4)
        with patch("kiro_crew.subagent.resolve_max_subagents", side_effect=RuntimeError("x")):
            mgr.apply_limits(KiroCrewConfig())
        assert mgr.max_concurrent == 4

    @pytest.mark.asyncio
    async def test_untouched_change_is_ignored(self) -> None:
        mgr = _mgr()
        mgr.reconfigure = AsyncMock()  # type: ignore[method-assign]
        await _dispatch(KiroCrewConfig(), "agent.model")
        mgr.reconfigure.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pool_size_change_recomputes_the_cap(self) -> None:
        """``session.pool_size`` is an input to the auto-sized cap."""
        mgr = _mgr(max_concurrent=4)
        fresh = KiroCrewConfig()
        fresh.session.pool_size = 5
        with patch("kiro_crew.subagent.resolve_max_subagents", return_value=6):
            await _dispatch(fresh, "session.pool_size")
        assert mgr.max_concurrent == 6

    @pytest.mark.asyncio
    async def test_a_non_sizing_change_skips_the_resolve_thread_hop(self) -> None:
        """``resolve_max_subagents`` probes host memory/cgroups off-thread. A
        reload that only touches e.g. ``agent.completion_keep`` cannot move the
        resolved cap, so it must not pay for that probe."""
        mgr = _mgr(max_concurrent=4)
        # First reconfigure establishes the sizing snapshot.
        baseline = KiroCrewConfig()
        with patch("kiro_crew.subagent.resolve_max_subagents", return_value=4) as resolve:
            await _dispatch(baseline, "agent.completion_keep")
        resolve.assert_called_once()

        # Second reload changes only a non-sizing path -- same sizing fields.
        changed = KiroCrewConfig()
        changed.agent.completion_keep = "tail"
        with patch("kiro_crew.subagent.resolve_max_subagents") as resolve2:
            await _dispatch(changed, "agent.completion_keep")
        resolve2.assert_not_called()
        assert mgr.max_concurrent == 4
        assert mgr._completion_keep == "tail"

    @pytest.mark.asyncio
    async def test_a_sizing_change_still_calls_resolve(self) -> None:
        mgr = _mgr(max_concurrent=4)
        baseline = KiroCrewConfig()
        with patch("kiro_crew.subagent.resolve_max_subagents", return_value=4):
            await _dispatch(baseline, "agent.completion_keep")

        sized = KiroCrewConfig()
        sized.agent.max_subagents = 9
        with patch("kiro_crew.subagent.resolve_max_subagents", return_value=9) as resolve:
            await _dispatch(sized, "agent.max_subagents")
        resolve.assert_called_once_with(sized)
        assert mgr.max_concurrent == 9


class TestCapChangesAndTheQueue:
    def test_raised_cap_admits_a_queued_spawn_through_the_pump(self) -> None:
        mgr = _mgr(max_concurrent=3)
        mgr._running_count = 3
        mgr._spawn_stagger_secs = 0.0
        mgr._last_spawn_ts = 0.0
        mgr.spawn = MagicMock(return_value=None)  # type: ignore[method-assign]
        mgr._emit_queue_depth = MagicMock()  # type: ignore[method-assign]
        mgr._queue.append({"task": "queued work", "parent_session_key": "p", "batch_id": ""})

        fresh = KiroCrewConfig()
        fresh.agent.max_subagents = 5
        mgr.apply_limits(fresh)

        assert mgr.max_concurrent == 5
        assert mgr._queue == []
        mgr.spawn.assert_called_once()
        assert mgr.spawn.call_args.kwargs["task"] == "queued work"
        assert mgr.spawn.call_args.kwargs["_from_queue"] is True

    def test_unchanged_or_lowered_cap_does_not_pump(self) -> None:
        mgr = _mgr(max_concurrent=8)
        mgr._drain_queue = MagicMock()  # type: ignore[method-assign]
        mgr._queue.append({"task": "queued"})
        fresh = KiroCrewConfig()
        fresh.agent.max_subagents = 3
        mgr.apply_limits(fresh)
        mgr._drain_queue.assert_not_called()

    def test_lowered_cap_cancels_nothing_in_flight(self) -> None:
        mgr = _mgr(max_concurrent=8)
        loop = asyncio.new_event_loop()
        try:
            tasks = [loop.create_task(asyncio.sleep(3600)) for _ in range(5)]
            for i, t in enumerate(tasks):
                info = SubagentInfo(id=f"agent{i:03d}", task="t", agent="")
                mgr._agents[info.id] = info
                mgr._tasks[info.id] = t
            mgr._running_count = 5

            fresh = KiroCrewConfig()
            fresh.agent.max_subagents = 3
            mgr.apply_limits(fresh)

            assert mgr.max_concurrent == 3
            assert mgr._running_count == 5
            assert not any(t.cancelled() for t in tasks)
            assert all(not info.done for info in mgr._agents.values())
            # Over the new cap: a new spawn is queued, not admitted, until the
            # running count drains below it on its own.
            should_queue, slot_free = mgr._admission._should_stagger_queue_impl(1e9)
            assert should_queue is True and slot_free is False
        finally:
            for t in tasks:
                t.cancel()
            loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
            loop.close()


class TestSubscription:
    def test_manager_registers_on_the_process_watcher(self) -> None:
        mgr = _mgr()
        subs = [s for s in live.watch().subscriptions() if s.name == "SubagentManager"]
        assert len(subs) == 1
        assert subs[0].prefixes == SubagentManager.LIVE_CONFIG_PATHS
        assert mgr._config_sub is subs[0]

    def test_watched_paths_are_under_the_subscribed_prefixes(self) -> None:
        for path in SubagentManager.LIVE_CONFIG_PATHS:
            assert path.startswith("agent.") or path == "session.pool_size"


class TestEndToEndThroughConfigWatch:
    @pytest.mark.asyncio
    async def test_a_file_write_reaches_the_manager(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.json"
        local = tmp_path / "config.local.json"
        _write(cfg_file, {"agent": {"max_subagents": 4, "subagent_max_turns": 50}})
        with (
            patch("kiro_crew.config.loader.config_path", return_value=cfg_file),
            patch("kiro_crew.config.loader.config_local_path", return_value=local),
            patch("kiro_crew.config.live._WATCH", ConfigWatch(poll_interval_secs=0.05)),
        ):
            watch = live.watch()
            mgr = _mgr(max_concurrent=4)
            watch.prime(KiroCrewConfig.load())
            _write(cfg_file, {"agent": {"max_subagents": 6, "subagent_max_turns": 9}})
            change = await watch.refresh_now()

        assert change is not None
        assert {"agent.max_subagents", "agent.subagent_max_turns"} <= change.changed
        assert mgr.max_concurrent == 6
        assert mgr._default_turn_limit == 9
